"""止损触发时「柜台没有这只票」的第二份证据：最近一张账户快照。

**为什么需要它**：``TdxSltpClient.get_positions`` 在桥账户通道掉线时返回空表
（查询异常也被吞成空表），和「真的清仓了」在形状上一模一样。2026-10-21 实录：
账户已清仓，601728.SH 的 auto 规则（``entry_price`` 已烘进规则，不再回查持仓）
触发移动止损，执行器把空持仓当成了「有仓但可用 0」，推出一条
「T+1 锁定或已被挂单占用」的假告警。

第二份证据来自 ``real_account_snapshots``：tdx / qmt 两条同步链每 30s 落一张，
且**零资产快照被写侧守卫挡在库外**（桥掉线时不会把空账户覆盖进来）。判据见
:func:`judge_snapshot`：

* 新鲜快照 + 该标的缺席 ⇒ ``absent``（可信空仓：安静跳过）；
* 新鲜快照 + 该标的在列 ⇒ ``held``（两条通道打架：必须告警）；
* 没有快照 / 快照过期 / 资产全 0 / 持仓与市值矛盾 ⇒ ``unknown``（判不了，按异常告警）。

本模块**只判不写**，任何异常都收成 ``unknown``——证据面坏掉不许把执行器带崩，
也不许把「读不到」伪装成「没有持仓」。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

CST = ZoneInfo("Asia/Shanghai")

#: 快照超过这个年龄就不作证（同步链 30s 一张，6 个周期 = 3 分钟）。
SNAPSHOT_MAX_AGE_S = 180.0
#: 快照时间比本机还「未来」超过这个量 ⇒ 时钟可疑，判 unknown。
SNAPSHOT_FUTURE_TOLERANCE_S = 60.0

EVIDENCE_HELD = "held"
EVIDENCE_ABSENT = "absent"
EVIDENCE_UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class PositionEvidence:
    """一份快照对「柜台有没有这只票」的证词。``reason`` 直接用于告警文案。"""

    verdict: str
    reason: str
    snapshot_at: datetime | None = None
    volume: float = 0.0


def _now_naive_utc() -> datetime:
    """快照列是 naive UTC（两条同步链都写 ``datetime.now(utc).replace(tzinfo=None)``）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _num(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    # NaN / inf 不是有效数字：判据按「读不到」处理，绝不参与比较。
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return out


def _normalize(symbol: Any) -> str:
    """与规则表/状态键同一个归一函数（后缀式 ``601728.SH``）。"""
    raw = str(symbol or "").strip().upper()
    if not raw:
        return ""
    if "." in raw:
        return raw
    try:
        from backend.services.live_trading.services.sltp_executor import (
            normalize_symbol,
        )

        return normalize_symbol(raw)
    except Exception:  # noqa: BLE001 - 兜底不阻断证据面
        return raw


def _position_volume(position: Any) -> float | None:
    if not isinstance(position, Mapping):
        return None
    value = _num(position.get("volume"))
    if value is None:
        value = _num(position.get("can_use_volume"))
    return value


def _position_symbol(position: Any) -> str:
    if not isinstance(position, Mapping):
        return ""
    return _normalize(position.get("symbol") or position.get("stock_code") or "")


def judge_snapshot(
    *,
    symbol: str,
    snapshot_at: datetime | None,
    total_asset: Any,
    market_value: Any,
    positions: Iterable[Any] | None,
    now: datetime | None = None,
    max_age_s: float = SNAPSHOT_MAX_AGE_S,
) -> PositionEvidence:
    """纯判据：一份账户快照能不能作证「柜台真的没有这只票」。**绝不抛。**"""
    try:
        return _judge(
            symbol=symbol,
            snapshot_at=snapshot_at,
            total_asset=total_asset,
            market_value=market_value,
            positions=positions,
            now=now if now is not None else _now_naive_utc(),
            max_age_s=max_age_s,
        )
    except Exception as exc:  # noqa: BLE001 证据面不许带崩执行器
        logger.warning("[SltpEvidence] 快照判据异常（按 unknown 处理）: %s", exc)
        return PositionEvidence(EVIDENCE_UNKNOWN, f"账户快照判据异常: {exc}")


def _judge(
    *,
    symbol: str,
    snapshot_at: datetime | None,
    total_asset: Any,
    market_value: Any,
    positions: Iterable[Any] | None,
    now: datetime,
    max_age_s: float,
) -> PositionEvidence:
    if snapshot_at is None or not isinstance(snapshot_at, datetime):
        return PositionEvidence(EVIDENCE_UNKNOWN, "没有可用的账户快照")
    age = (now - snapshot_at.replace(tzinfo=None)).total_seconds()
    if age < -SNAPSHOT_FUTURE_TOLERANCE_S:
        return PositionEvidence(EVIDENCE_UNKNOWN, "账户快照时间在未来（时钟可疑）")
    if age > max_age_s:
        return PositionEvidence(EVIDENCE_UNKNOWN, f"账户快照已过期（{age:.0f} 秒前）")

    asset = _num(total_asset)
    if asset is None or asset <= 0:
        # 零资产快照是「桥账户通道掉线」的典型形态，不能拿来证明空仓。
        return PositionEvidence(
            EVIDENCE_UNKNOWN, "最近账户快照资产为 0（账户通道可能未就绪）"
        )

    target = _normalize(symbol)
    if not target:
        return PositionEvidence(EVIDENCE_UNKNOWN, "标的代码为空，无法比对快照")

    held_volume = 0.0
    for position in positions or ():
        if _position_symbol(position) != target:
            continue
        volume = _position_volume(position) or 0.0
        if volume > 0:
            held_volume = volume
            break

    if held_volume > 0:
        return PositionEvidence(
            EVIDENCE_HELD,
            f"最近账户快照仍显示持有 {held_volume:g} 股",
            snapshot_at=snapshot_at,
            volume=held_volume,
        )

    market_value_num = _num(market_value) or 0.0
    if market_value_num > 0:
        # 持仓列表为空但市值 > 0：持仓查询失灵（桥假活第三种形态）。
        return PositionEvidence(
            EVIDENCE_UNKNOWN,
            f"最近账户快照市值为 {market_value_num:.2f} 但持仓列表为空（持仓查询可能失灵）",
            snapshot_at=snapshot_at,
        )
    return PositionEvidence(
        EVIDENCE_ABSENT,
        f"最近账户快照为空仓（{age:.0f} 秒前，总资产 {asset:.2f}）",
        snapshot_at=snapshot_at,
    )


async def position_evidence(
    *,
    tenant_id: str,
    user_id: str,
    symbol: str,
    now: datetime | None = None,
    max_age_s: float = SNAPSHOT_MAX_AGE_S,
) -> PositionEvidence:
    """读最近一张账户快照并给出证词。读失败 ⇒ ``unknown``（不抛）。"""
    try:
        snapshot = await _latest_snapshot(tenant_id=tenant_id, user_id=user_id)
    except Exception as exc:  # noqa: BLE001 证据面读失败按 unknown，绝不带崩执行器
        logger.warning("[SltpEvidence] 账户快照读取失败: %s", exc)
        return PositionEvidence(EVIDENCE_UNKNOWN, f"账户快照读取失败: {exc}")
    if snapshot is None:
        return PositionEvidence(EVIDENCE_UNKNOWN, "库里没有该账户的账户快照")
    return judge_snapshot(
        symbol=symbol,
        snapshot_at=snapshot.get("snapshot_at"),
        total_asset=snapshot.get("total_asset"),
        market_value=snapshot.get("market_value"),
        positions=snapshot.get("positions"),
        now=now,
        max_age_s=max_age_s,
    )


async def _latest_snapshot(*, tenant_id: str, user_id: str) -> dict[str, Any] | None:
    from sqlalchemy import select

    from backend.services.trade_shared.models.real_account_snapshot import (
        RealAccountSnapshot,
    )
    from backend.shared.database_manager_v2 import get_session

    async with get_session(read_only=True) as db:
        row = (
            await db.execute(
                select(
                    RealAccountSnapshot.snapshot_at,
                    RealAccountSnapshot.total_asset,
                    RealAccountSnapshot.market_value,
                    RealAccountSnapshot.payload_json,
                )
                .where(
                    RealAccountSnapshot.tenant_id == str(tenant_id or "default"),
                    RealAccountSnapshot.user_id == str(user_id or "0"),
                )
                .order_by(
                    RealAccountSnapshot.snapshot_at.desc(),
                    RealAccountSnapshot.id.desc(),
                )
                .limit(1)
            )
        ).first()
    if row is None:
        return None
    payload = row.payload_json if isinstance(row.payload_json, Mapping) else {}
    return {
        "snapshot_at": row.snapshot_at,
        "total_asset": row.total_asset,
        "market_value": row.market_value,
        "positions": payload.get("positions") or [],
    }
