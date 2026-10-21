"""滚动建仓窗口的收盘价损益口径（纯函数，无 IO）。

口径（2026-10-21 用户拍板）：

- 买入 = 入选当日**收盘价**（尾盘买入）；卖出 = 退出当日收盘价；
- 一笔交易 = 从窗口内首次在榜到首次不在榜；断榜后重进按新一笔计；
- 窗口首列的标的看不到窗口外更早的入场日，按首列收盘价买入并标记
  ``entry_from_window_start=True``；
- 缺失/非法收盘价 → ``return_pct=None``（展示 `—`，绝不写 0），且不计入
  胜率/均值分母；
- 组合口径：每日等权持有当日 TopK，收盘买入、次日收盘换仓，
  ``cycle_return_pct = Π(1 + r_t) − 1``（未计费用）。

本模块只做计算；名称/行业/市值的补全与日期序列化在 service 层完成。
"""

from __future__ import annotations

import math
import statistics
from datetime import date
from typing import Any
from collections.abc import Mapping, Sequence

RETURN_NDIGITS = 4
WIN_RATE_NDIGITS = 2


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _close(value: Any) -> float | None:
    """收盘价必须是正的有限数；否则视为缺失。"""
    if not _finite(value):
        return None
    number = float(value)
    return number if number > 0 else None


def _return_pct(entry_price: float | None, exit_price: float | None) -> float | None:
    if entry_price is None or exit_price is None:
        return None
    return round((exit_price / entry_price - 1.0) * 100.0, RETURN_NDIGITS)


def _day_picks(picks_by_day: Mapping[date, Sequence[str]], day: date) -> list[str]:
    """当日标的（去重、保持顺序）；空日按空列表处理。"""
    return list(dict.fromkeys(str(symbol) for symbol in (picks_by_day.get(day) or [])))


def compute_rolling_performance(
    days: Sequence[date],
    picks_by_day: Mapping[date, Sequence[str]],
    closes: Mapping[str, Mapping[date, float]],
) -> dict[str, Any]:
    """计算窗口内每笔退出损益、在场浮动与组合周期收益。"""
    ordered_days = list(days)
    exits_by_day: dict[date, list[dict[str, Any]]] = {day: [] for day in ordered_days}
    closed: list[dict[str, Any]] = []

    def close_of(symbol: str, day: date) -> float | None:
        return _close((closes.get(symbol) or {}).get(day))

    entry_dates: dict[str, date] = {}
    streak: dict[str, int] = {}
    previous: set[str] = set()
    for day in ordered_days:
        picks = _day_picks(picks_by_day, day)
        today = set(picks)
        for symbol in picks:
            if symbol not in entry_dates:
                entry_dates[symbol] = day
                streak[symbol] = 0
            streak[symbol] += 1
        for symbol in sorted(previous - today):
            entry_day = entry_dates.pop(symbol)
            entry_price = close_of(symbol, entry_day)
            exit_price = close_of(symbol, day)
            row = {
                "symbol": symbol,
                "entry_date": entry_day,
                "exit_date": day,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "return_pct": _return_pct(entry_price, exit_price),
                "held_days": streak.pop(symbol, 0),
                "entry_from_window_start": entry_day == ordered_days[0],
            }
            exits_by_day[day].append(row)
            closed.append(row)
        previous = today

    open_rows: list[dict[str, Any]] = []
    last_day = ordered_days[-1] if ordered_days else None
    if last_day is not None:
        for symbol in sorted(entry_dates, key=lambda s: (entry_dates[s], s)):
            entry_day = entry_dates[symbol]
            entry_price = close_of(symbol, entry_day)
            mark_price = close_of(symbol, last_day)
            open_rows.append(
                {
                    "symbol": symbol,
                    "entry_date": entry_day,
                    "mark_date": last_day,
                    "entry_price": entry_price,
                    "mark_price": mark_price,
                    "return_pct": _return_pct(entry_price, mark_price),
                    "held_days": streak.get(symbol, 0),
                    "entry_from_window_start": entry_day == ordered_days[0],
                }
            )

    daily: list[dict[str, Any]] = []
    for index in range(max(0, len(ordered_days) - 1)):
        day = ordered_days[index]
        next_day = ordered_days[index + 1]
        returns: list[float] = []
        for symbol in _day_picks(picks_by_day, day):
            value = _return_pct(close_of(symbol, day), close_of(symbol, next_day))
            if value is not None:
                returns.append(value)
        daily.append(
            {
                "date": day,
                "next_date": next_day,
                "return_pct": round(sum(returns) / len(returns), RETURN_NDIGITS)
                if returns
                else None,
                "priced": len(returns),
            }
        )

    return {
        "exits_by_day": exits_by_day,
        "closed": closed,
        "open": open_rows,
        "daily": daily,
        "stats": _stats(closed, open_rows, daily),
    }


def _stats(
    closed: Sequence[Mapping[str, Any]],
    open_rows: Sequence[Mapping[str, Any]],
    daily: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    priced_rows = [row for row in closed if row["return_pct"] is not None]
    priced = [float(row["return_pct"]) for row in priced_rows]
    wins = sum(1 for value in priced if value > 0)
    losses = sum(1 for value in priced if value < 0)
    flats = sum(1 for value in priced if value == 0)
    best = (
        max(priced_rows, key=lambda row: float(row["return_pct"]))
        if priced_rows
        else None
    )
    worst = (
        min(priced_rows, key=lambda row: float(row["return_pct"]))
        if priced_rows
        else None
    )

    open_priced = [
        float(row["return_pct"]) for row in open_rows if row["return_pct"] is not None
    ]
    cycle_values = [
        float(row["return_pct"]) for row in daily if row["return_pct"] is not None
    ]
    cycle_return: float | None = None
    if cycle_values:
        growth = math.prod(1.0 + value / 100.0 for value in cycle_values)
        cycle_return = round((growth - 1.0) * 100.0, RETURN_NDIGITS)

    return {
        "closed_trades": len(closed),
        "priced_trades": len(priced),
        "unpriced_trades": len(closed) - len(priced),
        "wins": wins,
        "losses": losses,
        "flats": flats,
        "win_rate_pct": round(wins / len(priced) * 100.0, WIN_RATE_NDIGITS)
        if priced
        else None,
        "avg_return_pct": round(sum(priced) / len(priced), RETURN_NDIGITS)
        if priced
        else None,
        "median_return_pct": round(statistics.median(priced), RETURN_NDIGITS)
        if priced
        else None,
        "best_symbol": best["symbol"] if best else None,
        "best_return_pct": best["return_pct"] if best else None,
        "worst_symbol": worst["symbol"] if worst else None,
        "worst_return_pct": worst["return_pct"] if worst else None,
        "open_positions": len(open_rows),
        "open_priced": len(open_priced),
        "open_avg_return_pct": round(
            sum(open_priced) / len(open_priced), RETURN_NDIGITS
        )
        if open_priced
        else None,
        "cycle_return_pct": cycle_return,
        "cycle_days": len(daily),
        "cycle_priced_days": len(cycle_values),
        "cycle_win_days": sum(1 for value in cycle_values if value > 0),
        "cycle_loss_days": sum(1 for value in cycle_values if value < 0),
    }
