"""模拟账户持仓未实现盈亏（浮动盈亏）的唯一口径。

为什么单独一层：``/simulation/account`` 的 ``positions`` 是
``dict[symbol, position]``（实盘/组合快照是数组），而快照、EOD、顶栏三处都要
按同一公式算未实现盈亏。公式分叉过一次的代价是实测事故：模拟盘 78 只持仓、
市值 100 万，顶栏「浮动盈亏」恒显示 ``0.00 / 0.00%``。

公式（多空同一式，符号按 ``side`` 翻转）：

* long：``(现价 - 成本) * 数量``
* short：``(成本 - 现价) * 数量``

缺现价时用 ``market_value / volume`` 反推（存量行可能只带市值）；缺现价也缺
市值、或成本 ≤ 0 的行**不参与**合计。一本账里一行都定不了价时返回 ``None``
（缺失），不返回 ``0.0``（假装没有浮动盈亏）。
"""

from __future__ import annotations

from typing import Any, Mapping


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _first_number(row: Mapping[str, Any], keys: tuple[str, ...]) -> float:
    for key in keys:
        value = row.get(key)
        if value is None:
            continue
        number = _to_float(value)
        if number != 0.0:
            return number
    return 0.0


def _iter_positions(positions: Any) -> list[Mapping[str, Any]]:
    if isinstance(positions, Mapping):
        return [row for row in positions.values() if isinstance(row, Mapping)]
    if isinstance(positions, (list, tuple)):
        return [row for row in positions if isinstance(row, Mapping)]
    return []


def position_floating_pnl(position: Mapping[str, Any]) -> float | None:
    """单行未实现盈亏；定不了价返回 ``None``。"""
    if not isinstance(position, Mapping):
        return None
    volume = _first_number(position, ("volume", "qty", "quantity", "total_volume"))
    if volume <= 0:
        return None
    price = _first_number(position, ("last_price", "price", "current_price"))
    if price <= 0:
        market_value = _to_float(position.get("market_value"))
        price = market_value / volume if market_value > 0 else 0.0
    cost = _first_number(position, ("cost_price", "avg_cost", "avg_price", "cost"))
    if price <= 0 or cost <= 0:
        return None
    side = str(position.get("side") or "long").strip().lower()
    return (cost - price) * volume if side == "short" else (price - cost) * volume


def compute_floating_pnl(positions: Any) -> float | None:
    """持仓集合（dict 或 list）的未实现盈亏合计。

    * 空持仓 → ``0.0``（空仓的浮动盈亏就是 0）；
    * 有持仓但一行都定不了价 → ``None``（缺失）。
    """
    rows = _iter_positions(positions)
    if not rows:
        return 0.0
    total = 0.0
    priced = 0
    for row in rows:
        value = position_floating_pnl(row)
        if value is None:
            continue
        total += value
        priced += 1
    if priced == 0:
        return None
    return round(total, 2)
