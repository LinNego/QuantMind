"""桥恢复后的**全名册补跑**（2026-10-21 用户要求：断 ≥30 分钟、恢复后不能漏）。

生产端（``tdx_account_sync_task``）在桥恢复且掉线窗口压到连续竞价时段时，写一份
``trade:decision-round:bridge-recovery:{day}`` 义务（见 ``backend.shared.bridge_outage``）。
本模块是消费端，挂在决策轮 runner 的轮询循环里：

1. **销账靠证据，不靠事件**：每家 agent 的 ``last:{agent}`` 若在
   ``recovered_at + RECOVERY_SETTLE_S`` 之后以 ``ok`` 完成过，即视为「恢复后已分析」。
   正点槽/唤醒轮跑过的家不会被重复补跑。
2. **只补漏的家**：名册里没证据的家逐个派发一轮 ``SCHEMA_INTRADAY``（与唤醒轮同
   schema——补跑是「恢复后再看一次」，不是第二次 rebalance，不会开第二批建仓）。
3. **时段闸**：只在连续竞价时段补跑（午休/报单缓冲等待，收盘 15:00 后置
   ``expired`` 并告警——盘外跑出来的腿会被拒或挂成次日单）。
4. **失败自愈**：非 ``ok`` 不销账，5 分钟退避、每家至多 ``MAX_ATTEMPTS`` 次；
   超限置 ``gave_up`` 并告警。义务键按日、TTL 两天。

与正点轮/唤醒轮**同任务串行**（runner 依次 await），天然没有并发下单窗口。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from backend.services.trade.services.decision_round_core import (
    LAST_KEY,
    STATUS_OK,
    RoundDeps,
    RoundResult,
    RoundSlot,
)
from backend.services.trade.services.decision_round_io import (
    default_round_deps,
    native_redis_client,
)
from backend.services.trade.services.decision_round_tick import round_tick
from backend.shared.bridge_outage import (
    MARKER_DONE,
    MARKER_EXPIRED,
    MARKER_GAVE_UP,
    MARKER_PENDING,
    MAX_ATTEMPTS,
    RECOVERY_SETTLE_S,
    RETRY_BACKOFF_S,
    RecoveryMarker,
    read_marker,
    update_marker,
    write_marker,
)
from backend.shared.decision.contract import SCHEMA_INTRADAY

logger = logging.getLogger(__name__)

#: 收盘（连续竞价结束）之后不再补跑。
_LAST_SESSION_END = (15, 0)


async def bridge_recovery_tick(
    *,
    deps: RoundDeps | None = None,
    native: Any = None,
    now: datetime | None = None,
    notify: Any = None,
) -> tuple[RoundResult, ...]:
    """一次轮询：有未销账的桥恢复义务就补跑漏掉的家。**契约：绝不抛**。"""
    try:
        return await _bridge_recovery_tick(
            deps=deps, native=native, now=now, notify=notify
        )
    except Exception as exc:  # noqa: BLE001 补跑不许拖累轮次循环
        logger.error("[DecisionRound] 桥恢复补跑异常: %s", exc, exc_info=True)
        return ()


async def _bridge_recovery_tick(
    *,
    deps: RoundDeps | None,
    native: Any,
    now: datetime | None,
    notify: Any,
) -> tuple[RoundResult, ...]:
    deps = deps or default_round_deps()
    now = now or deps.now()
    day_str = f"{now:%Y%m%d}"
    client = native
    owns = False
    if client is None:
        try:
            client = native_redis_client()
            owns = True
        except Exception as exc:  # noqa: BLE001 没有 Redis 就没有义务面
            logger.error("[DecisionRound] 桥恢复补跑：Redis 客户端构造失败: %s", exc)
            return ()
    try:
        marker = read_marker(client, day_str)
        if marker is None or marker.status != MARKER_PENDING:
            return ()
        return await _run_recovery(
            deps=deps, client=client, now=now, marker=marker, notify=notify
        )
    finally:
        if owns and client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass


async def _run_recovery(
    *,
    deps: RoundDeps,
    client: Any,
    now: datetime,
    marker: RecoveryMarker,
    notify: Any,
) -> tuple[RoundResult, ...]:
    if _after_last_session(now):
        await _finish(
            client,
            marker,
            MARKER_EXPIRED,
            note=(
                f"桥掉线 {marker.down_for_s / 60.0:.0f} 分钟、恢复时已收盘"
                f"（{now:%H:%M}）：盘外补跑会被拒或挂次日单，本义务作废"
            ),
            level="warning",
            title="桥恢复补跑未执行（已收盘）",
        )
        return ()
    if not _is_continuous(deps, now):
        return ()  # 午休/报单缓冲：等下一个连续竞价时段
    try:
        trading_day = await deps.is_trading_day(now.date())
    except Exception as exc:  # noqa: BLE001 判不了就不跑（与 round_tick 同 fail-closed）
        logger.warning("[DecisionRound] 补跑：交易日判定失败，跳过: %s", exc)
        return ()
    if not trading_day:
        return ()
    if now.timestamp() < marker.recovered_at + RECOVERY_SETTLE_S:
        return ()  # 刚恢复，等行情席刷新一拍

    agents = _agents(deps)
    missing = _missing_agents(client, marker, agents)
    if not missing:
        await _finish(
            client,
            marker,
            MARKER_DONE,
            missing=(),
            note=f"恢复后全名册已分析：{'、'.join(a or '单家' for a in agents)}",
            level="success",
            title="桥恢复补跑完成",
        )
        return ()

    dispatch: list[str] = []
    capped = False
    for agent in missing:
        attempts = int(marker.attempts.get(agent, 0) or 0)
        if attempts >= MAX_ATTEMPTS:
            capped = True
            continue
        last_at = float(marker.last_attempt_at.get(agent, 0.0) or 0.0)
        if last_at and now.timestamp() - last_at < RETRY_BACKOFF_S:
            continue
        dispatch.append(agent)
    if not dispatch:
        if capped and all(
            int(marker.attempts.get(a, 0) or 0) >= MAX_ATTEMPTS for a in missing
        ):
            await _finish(
                client,
                marker,
                MARKER_GAVE_UP,
                missing=tuple(missing),
                note=(
                    f"补跑失败达 {MAX_ATTEMPTS} 次：{'、'.join(a or '单家' for a in missing)}"
                ),
                level="error",
                title="桥恢复补跑失败",
            )
        return ()

    attempts = dict(marker.attempts)
    last_attempt_at = dict(marker.last_attempt_at)
    compensated = dict(marker.compensated)
    results: list[RoundResult] = []
    for agent in dispatch:
        attempts[agent] = int(attempts.get(agent, 0) or 0) + 1
        last_attempt_at[agent] = now.timestamp()
        slot = RoundSlot(f"{now:%H%M}", SCHEMA_INTRADAY)
        logger.warning(
            "[DecisionRound] 桥恢复补跑：%s 派发一轮（第 %d 次，槽 %s，schema=%s）",
            agent or "单家",
            attempts[agent],
            slot.label,
            slot.schema,
        )
        res = await round_tick(
            deps=deps, native=client, now=now, slot=slot, agent=agent, notify=notify
        )
        results.extend(res)
        for item in res:
            if item.ok:
                compensated[agent] = item.round_id
                break

    updated = update_marker(
        marker,
        attempts=attempts,
        last_attempt_at=last_attempt_at,
        compensated=compensated,
    )
    still_missing = _missing_agents(client, updated, agents)
    if not still_missing:
        await _finish(
            client,
            updated,
            MARKER_DONE,
            missing=(),
            note=f"恢复后全名册已分析：{'、'.join(a or '单家' for a in agents)}",
            level="success",
            title="桥恢复补跑完成",
        )
    elif all(
        int(updated.attempts.get(a, 0) or 0) >= MAX_ATTEMPTS for a in still_missing
    ):
        await _finish(
            client,
            updated,
            MARKER_GAVE_UP,
            missing=tuple(still_missing),
            note=(
                f"补跑失败达 {MAX_ATTEMPTS} 次：{'、'.join(a or '单家' for a in still_missing)}"
            ),
            level="error",
            title="桥恢复补跑失败",
        )
    else:
        write_marker(
            client,
            update_marker(
                updated,
                missing=tuple(still_missing),
                note=(
                    f"补跑进行中：待 {'、'.join(a or '单家' for a in still_missing)}"
                    f"（已补 {len(compensated)} 家）"
                ),
            ),
        )
    return tuple(results)


def _agents(deps: RoundDeps) -> tuple[str, ...]:
    """名册家列表；单家路径返回 ``("",)``（键形不带家段）。名册坏了上抛给外层。"""
    runs = (deps.roster or (lambda: ()))() or ()
    agents = tuple(str(r.agent) for r in runs if getattr(r, "agent", ""))
    return agents or ("",)


def _is_continuous(deps: RoundDeps, now: datetime) -> bool:
    fn = deps.is_continuous_trading or deps.is_trading_time
    return bool(fn(now))


def _after_last_session(now: datetime) -> bool:
    return (now.hour, now.minute) >= _LAST_SESSION_END


def _last_evidence(client: Any, agent: str) -> tuple[float, str] | None:
    key = f"{LAST_KEY}:{agent}" if agent else LAST_KEY
    try:
        raw = client.get(key)
    except Exception as exc:  # noqa: BLE001 读不到 → 当作没证据（宁可多跑）
        logger.warning("[DecisionRound] 补跑：%s 状态读取失败: %s", key, exc)
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
        ts = datetime.fromisoformat(str(data.get("ts") or "")).timestamp()
    except (TypeError, ValueError):
        return None
    return ts, str(data.get("status") or "")


def _missing_agents(
    client: Any, marker: RecoveryMarker, agents: tuple[str, ...]
) -> tuple[str, ...]:
    """恢复后没有成功轮次的家。已在义务里记过 ``compensated`` 的家直接销账。"""
    threshold = marker.recovered_at + RECOVERY_SETTLE_S
    missing: list[str] = []
    for agent in agents:
        if marker.compensated.get(agent):
            continue
        evidence = _last_evidence(client, agent)
        if (
            evidence is not None
            and evidence[0] >= threshold
            and evidence[1] == STATUS_OK
        ):
            continue
        missing.append(agent)
    return tuple(missing)


async def _finish(
    client: Any,
    marker: RecoveryMarker,
    status: str,
    *,
    missing: tuple[str, ...] | None = None,
    note: str,
    level: str,
    title: str,
) -> None:
    changes: dict[str, Any] = {"status": status, "note": note}
    if missing is not None:
        changes["missing"] = missing
    final = update_marker(marker, **changes)
    try:
        write_marker(client, final)
    except Exception as exc:  # noqa: BLE001 义务写失败：下个 tick 会再判一次
        logger.warning("[DecisionRound] 补跑义务收尾写入失败: %s", exc)
        return
    logger.info("[DecisionRound] %s：%s", title, note)
    await _publish(title, note, level)


async def _publish(title: str, content: str, level: str) -> None:
    """管理员可见性（站内；QQ 旁路按等级转发）。失败只记日志。"""
    try:
        from backend.shared.notification_publisher import (
            publish_notification_to_admins_async,
        )

        await publish_notification_to_admins_async(
            title=title, content=content, type="health", level=level
        )
    except Exception as exc:  # noqa: BLE001 可见性不许掀翻补跑
        logger.warning("[DecisionRound] 补跑通知投递失败: %s", exc)
