"""桥掉线窗口与「恢复后全名册补跑」义务（跨服务共享状态面）。

背景（2026-10-21 用户要求）：桥在**盘中**断掉半小时以上时，决策轮槽位要么在宽限
窗里空转（候选池缺现价 → 中止），要么直接错过；桥恢复后若没有一次**恢复后**的
全名册分析，那段时间等于没人看过盘。本模块只定义判据与状态编解码，I/O 由两侧
各自持有：

* 生产端：``tdx_account_sync_task`` 每 30s 的账户探测调用 :func:`record_probe`；
  掉线起点落在 Redis 里（进程重启不丢时长），恢复探测返回 :class:`BridgeOutage`，
  由 :func:`qualifies` 判定后写 :class:`RecoveryMarker`。
* 消费端：``decision_round_bridge_recovery.bridge_recovery_tick`` 读义务，按每家
  agent 的 ``last:{agent}`` 证据销账，只补漏的家。

判据（两条同时满足）：

1. 掉线时长 ≥ ``QM_BRIDGE_RECOVERY_MIN_S``（默认 1800s = 30 分钟）；
2. 掉线窗口与**连续竞价时段**（09:30–11:30 / 13:00–15:00）有交集——盘前/午休/
   收盘后的断流没有决策可补；收盘前恢复的，消费端会在 15:00 前跑完。

**恢复时刻的双口径**：正常探测（上次失败距今 ≤ ``PROBE_GAP_S``）用当前时刻作
``recovered_at``；跨进程重启的合成恢复用 ``last_down_at``（最后一次确证的失败）
作 ``recovered_at``——它是恢复时刻的**保守下界**，让停服期间之前已经跑过的轮次
仍能作为「恢复后已分析」的证据，避免重启后凭空多跑一轮。

本模块不 import 任何 service 层代码；连续竞价时段常量与
``backend.services.live_trading.services.trading_session.CONTINUOUS_SESSIONS``
同口径，由测试锁死一致（防止两处漂移）。
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

CST = ZoneInfo("Asia/Shanghai")

#: 「掉线中」开放窗口（Hash：``down_since`` / ``last_down_at``）。账户通道单一口径。
OUTAGE_KEY = "trade:bridge:outage:open"
#: 恢复补跑义务（按日 Hash；消费端在决策轮侧）。
RECOVERY_KEY_TPL = "trade:decision-round:bridge-recovery:{day}"

STATE_TTL_S = 2 * 86400
#: 恢复后先等一拍再补跑：账户探测成功≠行情席已开始写快照。
RECOVERY_SETTLE_S = 60.0
#: 上次失败距本次成功超过它 ⇒ 中间大概率进程重启过，恢复时刻取 ``last_down_at``。
PROBE_GAP_S = 120.0
#: 同一家模型补跑失败后的重试间隔与次数上限。
RETRY_BACKOFF_S = 300.0
MAX_ATTEMPTS = 3

#: 连续竞价时段（与 ``trading_session.CONTINUOUS_SESSIONS`` 同值；测试锁死）。
CONTINUOUS_SESSIONS: tuple[tuple[int, int, int, int], ...] = (
    (9, 30, 11, 30),
    (13, 0, 15, 0),
)

#: 义务状态。
MARKER_PENDING = "pending"
MARKER_DONE = "done"
MARKER_EXPIRED = "expired"
MARKER_GAVE_UP = "gave_up"


def min_outage_s() -> float:
    """判定「断得够久」的阈值（秒）。env 唯一读法：``QM_BRIDGE_RECOVERY_MIN_S``。"""
    raw = str(os.getenv("QM_BRIDGE_RECOVERY_MIN_S", "") or "").strip()
    if not raw:
        return 1800.0
    try:
        value = float(raw)
    except ValueError:
        logger.warning("[BridgeOutage] QM_BRIDGE_RECOVERY_MIN_S 不是数字: %r", raw)
        return 1800.0
    return value if value > 0 else 1800.0


def _num(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and out not in (float("inf"), float("-inf")) else None


def _json_list(raw: Any) -> tuple[str, ...]:
    if isinstance(raw, (list, tuple)):
        return tuple(str(x) for x in raw if str(x))
    if isinstance(raw, str) and raw.strip():
        try:
            value = json.loads(raw)
        except ValueError:
            return ()
        if isinstance(value, list):
            return tuple(str(x) for x in value if str(x))
    return ()


def _json_obj(raw: Any) -> Mapping[str, Any]:
    if isinstance(raw, Mapping):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            value = json.loads(raw)
        except ValueError:
            return {}
        return value if isinstance(value, Mapping) else {}
    return {}


@dataclass(frozen=True, slots=True)
class BridgeOutage:
    """一次已结束的掉线窗口。

    ``recovered_at`` 是**证据分界**（恢复后完成的轮次才算数）；``observed_at`` 是
    探测到恢复的时刻。正常探测两者相同；跨重启合成时 ``recovered_at`` 退到
    ``last_down_at``（保守下界），``down_for_s`` 仍按 ``observed_at`` 计。
    """

    down_since: float
    last_down_at: float
    observed_at: float
    recovered_at: float
    synthesized: bool = False

    @property
    def down_for_s(self) -> float:
        return max(0.0, self.observed_at - self.down_since)


@dataclass(frozen=True, slots=True)
class RecoveryMarker:
    """一次恢复补跑义务（按交易日一份，Redis Hash）。"""

    day: str
    status: str
    down_since: float
    recovered_at: float
    down_for_s: float
    observed_at: float = 0.0
    synthesized: bool = False
    missing: tuple[str, ...] = ()
    attempts: Mapping[str, int] = field(default_factory=dict)
    last_attempt_at: Mapping[str, float] = field(default_factory=dict)
    compensated: Mapping[str, str] = field(default_factory=dict)
    note: str = ""


# ── 掉线窗口（生产端） ───────────────────────────────────────────────


def record_probe(
    client: Any, *, ok: bool, now: float | None = None
) -> BridgeOutage | None:
    """喂入一次账户探测。

    * ``ok=False``：开/续开放窗口（``down_since`` 只写第一次，重启不重置）；
    * ``ok=True`` 且窗口存在：关闭窗口并返回 :class:`BridgeOutage`；
    * ``ok=True`` 且无窗口：``None``。

    Redis 读写失败只记日志（健康告警链不许被状态面拖死），返回 ``None``。
    """
    ts = float(now) if now is not None else time.time()
    try:
        if not ok:
            raw = client.hgetall(OUTAGE_KEY)
            down_since = _num(raw.get("down_since")) or ts
            client.hset(
                OUTAGE_KEY,
                mapping={
                    "down_since": f"{down_since:.3f}",
                    "last_down_at": f"{ts:.3f}",
                },
            )
            client.expire(OUTAGE_KEY, STATE_TTL_S)
            return None
    except Exception as exc:  # noqa: BLE001 状态面坏掉不许影响探测循环
        logger.warning("[BridgeOutage] 掉线窗口写入失败: %s", exc)
        return None
    try:
        raw = client.hgetall(OUTAGE_KEY)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[BridgeOutage] 掉线窗口读取失败: %s", exc)
        return None
    if not raw:
        return None
    down_since = _num(raw.get("down_since"))
    last_down_at = _num(raw.get("last_down_at")) or ts
    try:
        client.delete(OUTAGE_KEY)
    except Exception as exc:  # noqa: BLE001 删不掉最多下次再合成一次恢复
        logger.warning("[BridgeOutage] 掉线窗口清理失败: %s", exc)
    if down_since is None:
        return None
    synthesized = (ts - last_down_at) > PROBE_GAP_S
    return BridgeOutage(
        down_since=down_since,
        last_down_at=last_down_at,
        observed_at=ts,
        recovered_at=last_down_at if synthesized else ts,
        synthesized=synthesized,
    )


def session_overlap_s(down_since: float, recovered_at: float) -> float:
    """掉线窗口与连续竞价时段的重叠秒数（只算工作日；最多回看一天）。

    回看一天是为了覆盖「昨日尾盘断到今日开盘」的跨夜窗口；更早的日期对
    「盘中断流」语义没有增量。
    """
    if recovered_at <= down_since:
        return 0.0
    last_day = datetime.fromtimestamp(recovered_at, CST).date()
    first_day = max(
        datetime.fromtimestamp(down_since, CST).date(),
        last_day - timedelta(days=1),
    )
    total = 0.0
    day = first_day
    while day <= last_day:
        if day.weekday() < 5:
            for sh, sm, eh, em in CONTINUOUS_SESSIONS:
                start = datetime(day.year, day.month, day.day, sh, sm, tzinfo=CST)
                end = datetime(day.year, day.month, day.day, eh, em, tzinfo=CST)
                total += max(
                    0.0,
                    min(recovered_at, end.timestamp())
                    - max(down_since, start.timestamp()),
                )
        day += timedelta(days=1)
    return total


def qualifies(outage: BridgeOutage, *, min_s: float | None = None) -> bool:
    """掉线够久（≥阈值）且窗口压到连续竞价时段。"""
    threshold = min_outage_s() if min_s is None else float(min_s)
    if outage.down_for_s < threshold:
        return False
    return session_overlap_s(outage.down_since, outage.observed_at) > 0.0


# ── 恢复补跑义务（编解码 / 生产端写） ────────────────────────────────


def build_marker(outage: BridgeOutage) -> RecoveryMarker:
    """掉线窗口 → 待消费的补跑义务（``missing`` 由消费端按名册解析后回填）。"""
    day = datetime.fromtimestamp(outage.recovered_at, CST).strftime("%Y%m%d")
    return RecoveryMarker(
        day=day,
        status=MARKER_PENDING,
        down_since=outage.down_since,
        recovered_at=outage.recovered_at,
        down_for_s=outage.down_for_s,
        observed_at=outage.observed_at,
        synthesized=outage.synthesized,
        note=(
            f"桥掉线 {outage.down_for_s / 60.0:.0f} 分钟（"
            f"{datetime.fromtimestamp(outage.down_since, CST):%H:%M} → "
            f"{datetime.fromtimestamp(outage.observed_at, CST):%H:%M}）已恢复，"
            "待全名册补跑"
        ),
    )


def encode_marker(marker: RecoveryMarker) -> dict[str, str]:
    return {
        "day": marker.day,
        "status": marker.status,
        "down_since": f"{marker.down_since:.3f}",
        "recovered_at": f"{marker.recovered_at:.3f}",
        "down_for_s": f"{marker.down_for_s:.3f}",
        "observed_at": f"{marker.observed_at:.3f}",
        "synthesized": "1" if marker.synthesized else "0",
        "missing": json.dumps(list(marker.missing), ensure_ascii=False),
        "attempts": json.dumps(dict(marker.attempts), ensure_ascii=False),
        "last_attempt_at": json.dumps(dict(marker.last_attempt_at), ensure_ascii=False),
        "compensated": json.dumps(dict(marker.compensated), ensure_ascii=False),
        "note": marker.note,
    }


def decode_marker(raw: Mapping[str, Any] | None, *, day: str) -> RecoveryMarker | None:
    """Redis Hash → :class:`RecoveryMarker`；空/坏值按默认值，不炸。"""
    info = raw or {}
    status = str(info.get("status") or "")
    if not status:
        return None
    attempts: dict[str, int] = {}
    for key, value in _json_obj(info.get("attempts")).items():
        num = _num(value)
        if num is not None:
            attempts[str(key)] = int(num)
    last_attempt_at: dict[str, float] = {}
    for key, value in _json_obj(info.get("last_attempt_at")).items():
        num = _num(value)
        if num is not None:
            last_attempt_at[str(key)] = num
    compensated: dict[str, str] = {}
    for key, value in _json_obj(info.get("compensated")).items():
        if str(value):
            compensated[str(key)] = str(value)
    return RecoveryMarker(
        day=str(info.get("day") or day),
        status=status,
        down_since=_num(info.get("down_since")) or 0.0,
        recovered_at=_num(info.get("recovered_at")) or 0.0,
        down_for_s=_num(info.get("down_for_s")) or 0.0,
        observed_at=_num(info.get("observed_at")) or 0.0,
        synthesized=str(info.get("synthesized") or "") == "1",
        missing=_json_list(info.get("missing")),
        attempts=attempts,
        last_attempt_at=last_attempt_at,
        compensated=compensated,
        note=str(info.get("note") or ""),
    )


def write_marker(client: Any, marker: RecoveryMarker) -> None:
    """写义务（Hash + TTL）。异常上抛：调用侧决定是告警还是吞（生产端只记日志）。"""
    key = RECOVERY_KEY_TPL.format(day=marker.day)
    client.hset(key, mapping=encode_marker(marker))
    client.expire(key, STATE_TTL_S)


def read_marker(client: Any, day: str) -> RecoveryMarker | None:
    """按日读义务；没有/坏掉 → ``None``。"""
    try:
        raw = client.hgetall(RECOVERY_KEY_TPL.format(day=day))
    except Exception as exc:  # noqa: BLE001 读不到 = 本轮不补（下个 tick 再试）
        logger.warning("[BridgeOutage] 补跑义务读取失败: %s", exc)
        return None
    return decode_marker(raw, day=day)


def update_marker(marker: RecoveryMarker, **changes: Any) -> RecoveryMarker:
    """不可变更新（``dataclasses.replace`` 的薄包装，语义上强调不可变性）。"""
    return replace(marker, **changes)
