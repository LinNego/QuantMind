"""当日成交 / 清仓盈亏（收盘收益报表的明细段）。

报表原本只有「总资产 / 当日盈亏 / 持仓」三行，用户看不出**当天到底买了什么、
卖了什么、清掉的仓位赚没赚**。本模块把这些明细从两张表里拼出来：

* **成交**：``trades`` 表当日 REAL 行。``executed_at`` 是北京墙钟 naive——
  桥报 ``HHMM`` 贴到 ``now`` 的日期上（``tdx_push_service``），QMT 对账写
  ``datetime.now()``；两者在交易时段内都是北京日历日，故按
  ``[当日 00:00, 次日 00:00)`` 过滤；
* **期初持仓**：``real_account_snapshots`` 里**当日 00:00（北京）之前**的最后
  一帧。该表 ``snapshot_at`` 是 naive UTC（两条链路都用
  ``datetime.now(timezone.utc).replace(tzinfo=None)``），故北京 00:00 =
  UTC 前一日 16:00——这个 8 小时换算是本模块最容易错的地方；
* **清仓盈亏**：期初成本价 + 当日成交的移动平均成本（买入摊薄、卖出按卖出时
  点的均价结转）。清仓 = 当日卖完之后期末数量为 0。**成本是柜面/桥的摊薄
  成本口径**（桥在盈利减仓后会下调成本价），费用不计（与成交回执同为毛口径）。

桥在清仓后仍会把代码留成 ``total_volume=0`` 的模板行——这里
:func:`positions_from_payload` 一律丢弃 0 股行；否则「持仓 9 只 / 市值 ¥0.00」
会从快照一路带进日终账本（2026-10-20 实况）。

纯函数（聚合/成本结转/汇总）与 DB 读取分开：前者可单测，后者只做三件事——
查当日成交、按期初帧、按通道分发。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from datetime import date, datetime, time, timedelta
from typing import Any

from backend.shared.stock_utils import StockCodeUtil

logger = logging.getLogger(__name__)

#: 与 ``decision_round_core`` / 实盘账户页同源的账户 user 选择环境变量。
ENV_ACCOUNT_USER = "QM_DECISION_ACCOUNT_USER_ID"

#: 期初快照回看窗口：桥 30s 一帧，查询按 ``snapshot_at`` 倒序 LIMIT 1，
#: 窗口只防「极端断档时摸到一条几个月前的假帧」。
_SNAPSHOT_LOOKBACK_DAYS = 10

#: 北京时间相对 UTC 的固定偏移（中国无夏令时）。
_BEIJING_OFFSET = timedelta(hours=8)

#: 数量容差：桥回 0 股模板行，浮点只做防抖。
_QTY_EPS = 1e-6

#: ``_row_to_dict`` 需要从 SQLAlchemy Row / 替身对象里取的列。
_ROW_KEYS = (
    "symbol",
    "side",
    "quantity",
    "price",
    "executed_at",
    "exchange_name",
    "payload_json",
)


def _as_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _side_value(side: Any) -> str:
    return str(getattr(side, "value", side) or "").strip().lower()


def _row_to_dict(row: Any) -> dict[str, Any]:
    """SQLAlchemy Row / dict / SimpleNamespace → dict（只取本模块用到的列）。"""
    mapping = getattr(row, "_mapping", None)
    if mapping is not None:
        return dict(mapping)
    if isinstance(row, Mapping):
        return dict(row)
    return {key: getattr(row, key, None) for key in _ROW_KEYS}


def day_bounds(day: date) -> tuple[datetime, datetime]:
    """北京墙钟口径的当日区间 ``[00:00, 次日 00:00)``（naive）。"""
    start = datetime.combine(day, time.min)
    return start, start + timedelta(days=1)


def channel_for_exchange(exchange_name: Any) -> str:
    """成交行 → 通道键：桥成交记 ``tdx_bridge``，其余（QMT 对账/执行流）归 qmt。"""
    return "tdx" if str(exchange_name or "").strip().lower() == "tdx_bridge" else "qmt"


def positions_from_payload(payload: Any) -> dict[str, dict[str, float]]:
    """快照 ``payload_json`` → ``{后缀码: {volume, cost_price}}``（0 股行丢弃）。

    ``payload`` 既可以是整份 payload（含 ``positions``），也可以直接是列表。
    """
    if isinstance(payload, Mapping):
        payload = payload.get("positions")
    if not isinstance(payload, (list, tuple)):
        return {}
    out: dict[str, dict[str, float]] = {}
    for item in payload:
        if not isinstance(item, Mapping):
            continue
        symbol = str(item.get("symbol") or "").strip()
        volume = _as_float(item.get("volume"))
        if not symbol or volume <= _QTY_EPS:
            continue
        out[symbol] = {
            "volume": volume,
            "cost_price": _as_float(item.get("cost_price")),
        }
    return out


def aggregate_day_trades(
    rows: Iterable[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """当日成交行 → ``{后缀码: {buy_qty, buy_amount, sell_qty, ..., fills}}``。

    ``fills`` 保留逐笔（``executed_at`` 原样），供移动平均成本结转。
    """
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "").strip()
        side = _side_value(row.get("side"))
        quantity = _as_float(row.get("quantity"))
        if not symbol or side not in ("buy", "sell") or quantity <= _QTY_EPS:
            continue
        price = _as_float(row.get("price"))
        item = out.setdefault(
            symbol,
            {
                "symbol": symbol,
                "buy_qty": 0.0,
                "buy_amount": 0.0,
                "sell_qty": 0.0,
                "sell_amount": 0.0,
                "fills": [],
            },
        )
        item[f"{side}_qty"] += quantity
        item[f"{side}_amount"] += quantity * price
        item["fills"].append(
            {
                "side": side,
                "quantity": quantity,
                "price": price,
                "executed_at": row.get("executed_at"),
            }
        )
    return out


def compute_closed_positions(
    opening: Mapping[str, Mapping[str, Any]],
    aggregated: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """按移动平均成本结转当日卖出，返回**当日清仓**的标的与已实现盈亏。

    成本口径与桥一致（柜面摊薄成本），费用不计。成本缺失时
    ``realized_pnl``/``pnl_pct`` 为 ``None``——不许把缺成本显示成 0 盈亏。
    """
    closed: list[dict[str, Any]] = []
    for symbol, item in aggregated.items():
        sold_qty = _as_float(item.get("sell_qty"))
        if sold_qty <= _QTY_EPS:
            continue
        held = opening.get(symbol) or {}
        volume = _as_float(held.get("volume"))
        cost = _as_float(held.get("cost_price"))
        has_cost = volume > _QTY_EPS and cost > 0
        realized = basis = known_sold = unknown_sold = 0.0
        fills = sorted(
            item.get("fills") or [],
            key=lambda f: str(f.get("executed_at") or ""),
        )
        for fill in fills:
            quantity = _as_float(fill.get("quantity"))
            price = _as_float(fill.get("price"))
            if fill.get("side") == "buy":
                if volume <= _QTY_EPS:
                    volume, cost, has_cost = quantity, price, True
                elif has_cost:
                    cost = (cost * volume + price * quantity) / (volume + quantity)
                    volume += quantity
                else:
                    # 期初成本缺失：买入补不出期初成本，只能继续记未知
                    volume += quantity
                continue
            sold = min(quantity, volume) if volume > _QTY_EPS else 0.0
            if has_cost and sold > 0:
                realized += (price - cost) * sold
                basis += cost * sold
                known_sold += sold
                unknown_sold += max(0.0, quantity - sold)
            else:
                unknown_sold += quantity
            volume = max(0.0, volume - quantity)
        if volume > _QTY_EPS:
            continue
        known = has_cost and unknown_sold <= _QTY_EPS
        closed.append(
            {
                "symbol": symbol,
                "quantity": round(sold_qty, 2),
                "cost_known": known,
                "realized_pnl": round(realized, 2) if known else None,
                "avg_cost": (
                    round(basis / known_sold, 4) if known_sold > _QTY_EPS else None
                ),
                "pnl_pct": (
                    round(realized / basis * 100.0, 2) if known and basis > 0 else None
                ),
            }
        )
    return closed


def _resolve_name(symbol: str) -> str:
    """后缀码 → 中文名；映射器不可用只降级为空串（名称是装饰，不得拖垮报表）。"""
    try:
        from backend.shared.stock_name_mapper import resolve_name

        return str(resolve_name(symbol) or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def summarize_day_trades(
    aggregated: Mapping[str, Mapping[str, Any]],
    opening: Mapping[str, Mapping[str, Any]],
    *,
    name_resolver: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """聚合成交 + 期初持仓 → 报表可直接渲染的买卖/清仓结构。"""
    resolver = name_resolver or (lambda _symbol: "")

    def _label(symbol: str) -> dict[str, str]:
        return {
            "symbol": symbol,
            "code": StockCodeUtil.to_prefix(symbol),
            "name": resolver(symbol),
        }

    buys: list[dict[str, Any]] = []
    sells: list[dict[str, Any]] = []
    for symbol, item in aggregated.items():
        buy_qty = _as_float(item.get("buy_qty"))
        sell_qty = _as_float(item.get("sell_qty"))
        fills = item.get("fills") or []
        if buy_qty > _QTY_EPS:
            buys.append(
                {
                    **_label(symbol),
                    "quantity": round(buy_qty, 2),
                    "avg_price": round(_as_float(item.get("buy_amount")) / buy_qty, 2),
                    "fills": sum(1 for f in fills if f.get("side") == "buy"),
                }
            )
        if sell_qty > _QTY_EPS:
            sells.append(
                {
                    **_label(symbol),
                    "quantity": round(sell_qty, 2),
                    "avg_price": round(
                        _as_float(item.get("sell_amount")) / sell_qty, 2
                    ),
                    "fills": sum(1 for f in fills if f.get("side") == "sell"),
                }
            )

    closed = compute_closed_positions(opening, aggregated)
    for item in closed:
        item.update(_label(item["symbol"]))
    closed.sort(
        key=lambda x: (
            x["realized_pnl"] is None,
            -abs(float(x["realized_pnl"] or 0.0)),
        )
    )
    known = [x for x in closed if x["realized_pnl"] is not None]
    return {
        "buys": buys,
        "sells": sells,
        "closed": closed,
        "closed_pnl_total": (
            round(sum(float(x["realized_pnl"]) for x in known), 2) if known else None
        ),
        "closed_cost_unknown": len(closed) - len(known),
    }


def _payload_from_row(row: Any) -> Any:
    if row is None:
        return None
    payload = getattr(row, "payload_json", None)
    if payload is not None:
        return payload
    try:
        return row[0]
    except (KeyError, IndexError, TypeError):
        return None


async def _load_opening_positions(
    db,
    *,
    tenant_id: str,
    user_id: str,
    account_id: str,
    cutoff: datetime,
) -> dict[str, dict[str, float]]:
    """当日 00:00（北京）之前最后一帧快照的持仓。"""
    from sqlalchemy import select

    from backend.services.trade_shared.models.real_account_snapshot import (
        RealAccountSnapshot,
    )

    stmt = (
        select(RealAccountSnapshot.payload_json)
        .where(
            RealAccountSnapshot.tenant_id == tenant_id,
            RealAccountSnapshot.user_id == user_id,
            RealAccountSnapshot.account_id == account_id,
            RealAccountSnapshot.snapshot_at < cutoff,
            RealAccountSnapshot.snapshot_at
            >= cutoff - timedelta(days=_SNAPSHOT_LOOKBACK_DAYS),
        )
        .order_by(
            RealAccountSnapshot.snapshot_at.desc(),
            RealAccountSnapshot.id.desc(),
        )
        .limit(1)
    )
    row = (await db.execute(stmt)).first()
    return positions_from_payload(_payload_from_row(row))


async def collect_day_activity(
    db, day: date, channels: Iterable[Mapping[str, Any]]
) -> dict[str, dict[str, Any]]:
    """当日成交/清仓明细，按通道键（``tdx``/``qmt``）返回。

    读失败由调用方兜底（报表照发，明细只是增量信息）。
    """
    from sqlalchemy import select

    from backend.services.trade_shared.models.order import TradingMode
    from backend.services.trade_shared.models.trade import Trade
    from backend.shared.simulation_account_keys import resolve_db_account_user

    channel_keys = [str(c.get("key") or "") for c in channels]
    if not channel_keys:
        return {}

    user = resolve_db_account_user(ENV_ACCOUNT_USER)
    start, end = day_bounds(day)
    result = await db.execute(
        select(
            Trade.symbol,
            Trade.side,
            Trade.quantity,
            Trade.price,
            Trade.executed_at,
            Trade.exchange_name,
        )
        .where(
            Trade.tenant_id == "default",
            Trade.user_id == user,
            Trade.trading_mode == TradingMode.REAL,
            Trade.executed_at >= start,
            Trade.executed_at < end,
        )
        .order_by(Trade.executed_at.asc(), Trade.id.asc())
    )
    rows = [_row_to_dict(row) for row in result.all()]

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(channel_for_exchange(row.get("exchange_name")), []).append(
            row
        )

    # 北京 00:00 = UTC 前一日 16:00（snapshot_at 是 naive UTC）
    cutoff = start - _BEIJING_OFFSET
    activity: dict[str, dict[str, Any]] = {}
    for key in channel_keys:
        aggregated = aggregate_day_trades(grouped.get(key) or [])
        if not aggregated:
            continue
        opening = await _load_opening_positions(
            db,
            tenant_id="default",
            user_id=user,
            account_id=f"{key}-default-{user}",
            cutoff=cutoff,
        )
        activity[key] = summarize_day_trades(
            aggregated, opening, name_resolver=_resolve_name
        )
    return activity


__all__ = [
    "ENV_ACCOUNT_USER",
    "aggregate_day_trades",
    "channel_for_exchange",
    "collect_day_activity",
    "compute_closed_positions",
    "day_bounds",
    "positions_from_payload",
    "summarize_day_trades",
]
