"""组合风险收益指标（纯函数，无 IO）。

口径（2026-10-21 机构级统计）：

- 年化按 244 个交易日；样本少于 ``MIN_ANNUALIZE_DAYS`` 不年化，返回 ``None``；
- 收益率一律用小数参与计算，输出百分比；缺失值返回 ``None``，绝不写 0；
- 波动率/夏普用样本标准差（ddof=1）；Sortino 的下行差用全样本、目标 0；
- 基准缺失的交易日不参与超额统计，覆盖率由服务层判断是否可用。
"""

from __future__ import annotations

import math
import statistics
from datetime import date
from typing import Any
from collections.abc import Mapping, Sequence

PERIODS_PER_YEAR = 244
MIN_ANNUALIZE_DAYS = 20
# 基准覆盖率低于此值：整段基准/超额/信息比率一律按缺失处理（细案 D4）
BENCHMARK_MIN_COVERAGE_PCT = 95.0
RETURN_NDIGITS = 4
RATIO_NDIGITS = 4


def _finite(value: Any) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _close(value: Any) -> float | None:
    if not _finite(value):
        return None
    number = float(value)
    return number if number > 0 else None


def _round(value: float | None, digits: int = RETURN_NDIGITS) -> float | None:
    return None if value is None else round(float(value), digits)


def daily_returns(equity: Sequence[float]) -> list[float | None]:
    """日收益序列（小数）：首日无前值，返回 ``None``。"""
    values = [float(value) for value in equity]
    returns: list[float | None] = [None]
    for previous, current in zip(values, values[1:], strict=False):
        if previous > 0 and _finite(current):
            returns.append(current / previous - 1.0)
        else:
            returns.append(None)
    return returns


def annualized_return_pct(
    total_return_pct: float | None,
    trading_days: int,
    *,
    min_days: int = MIN_ANNUALIZE_DAYS,
    periods: int = PERIODS_PER_YEAR,
) -> float | None:
    """年化收益；样本太短或本金归零时不年化（``None``）。"""
    if total_return_pct is None or trading_days < min_days:
        return None
    growth = 1.0 + float(total_return_pct) / 100.0
    if growth <= 0:
        return None
    return _round((growth ** (periods / trading_days) - 1.0) * 100.0)


def drawdown_stats(days: Sequence[date], equity: Sequence[float]) -> dict[str, Any]:
    """最大回撤、峰值日、谷底日与修复日（交易日口径）。"""
    if not days or len(days) != len(equity):
        return {
            "max_drawdown_pct": None,
            "peak_date": None,
            "trough_date": None,
            "recovery_date": None,
            "recovery_days": None,
        }

    peak = None
    peak_index = 0
    trough_index: int | None = None
    drawdown_peak_index: int | None = None
    worst = 0.0
    for index, value in enumerate(equity):
        current = float(value)
        if not _finite(current):
            continue
        if peak is None or current > peak:
            peak = current
            peak_index = index
        if peak and peak > 0:
            drawdown = current / peak - 1.0
            if drawdown < worst:
                worst = drawdown
                trough_index = index
                drawdown_peak_index = peak_index

    recovery_index: int | None = None
    if trough_index is not None and drawdown_peak_index is not None:
        peak_value = float(equity[drawdown_peak_index])
        for index in range(trough_index + 1, len(equity)):
            if float(equity[index]) >= peak_value:
                recovery_index = index
                break

    return {
        "max_drawdown_pct": _round(worst * 100.0),
        "peak_date": days[drawdown_peak_index].isoformat()
        if drawdown_peak_index is not None
        else None,
        "trough_date": days[trough_index].isoformat()
        if trough_index is not None
        else None,
        "recovery_date": days[recovery_index].isoformat()
        if recovery_index is not None
        else None,
        "recovery_days": (recovery_index - trough_index)
        if recovery_index is not None and trough_index is not None
        else None,
    }


def risk_metrics(
    returns: Sequence[float | None],
    *,
    periods: int = PERIODS_PER_YEAR,
) -> dict[str, Any]:
    """年化波动、夏普、Sortino、日胜率等。"""
    values = [float(value) for value in returns if value is not None]
    empty = {
        "sample_days": 0,
        "volatility_pct": None,
        "sharpe_ratio": None,
        "sortino_ratio": None,
        "downside_deviation_pct": None,
        "positive_days": 0,
        "negative_days": 0,
        "best_day_pct": None,
        "worst_day_pct": None,
    }
    if len(values) < 2:
        empty["sample_days"] = len(values)
        return empty

    mean = statistics.fmean(values)
    stdev = statistics.stdev(values)
    downside = [min(0.0, value) for value in values]
    downside_deviation = math.sqrt(
        sum(value * value for value in downside) / len(downside)
    )
    volatility = stdev * math.sqrt(periods)
    sharpe = mean / stdev * math.sqrt(periods) if stdev > 0 else None
    sortino = (
        mean / downside_deviation * math.sqrt(periods)
        if downside_deviation > 0
        else None
    )
    return {
        "sample_days": len(values),
        "volatility_pct": _round(volatility * 100.0, 2),
        "sharpe_ratio": _round(sharpe, RATIO_NDIGITS),
        "sortino_ratio": _round(sortino, RATIO_NDIGITS),
        "downside_deviation_pct": _round(
            downside_deviation * math.sqrt(periods) * 100.0, 2
        ),
        "positive_days": sum(1 for value in values if value > 0),
        "negative_days": sum(1 for value in values if value < 0),
        "best_day_pct": _round(max(values) * 100.0),
        "worst_day_pct": _round(min(values) * 100.0),
    }


def benchmark_stats(
    days: Sequence[date], closes: Mapping[date, float] | None
) -> dict[str, Any]:
    """基准区间收益、日收益（与 ``days`` 对齐）、月度收益与覆盖率。"""
    levels = [_close((closes or {}).get(day)) for day in days]
    valid_days = sum(1 for level in levels if level is not None)
    # 完全没给基准 → 覆盖率未知（None）；给了但缺日 → 真实覆盖率
    coverage_pct = valid_days / len(days) * 100.0 if days and closes else None

    daily = _levels_to_returns(levels)
    first = next((level for level in levels if level is not None), None)
    last = next((level for level in reversed(levels) if level is not None), None)
    total = (
        (last / first - 1.0) * 100.0
        if first is not None and last is not None and first > 0
        else None
    )

    monthly: dict[str, float | None] = {}
    prior_month_last: float | None = None
    month_first_close: float | None = None
    month_last_close: float | None = None
    current_month: str | None = None
    for day, level in zip(days, levels, strict=False):
        key = f"{day.year:04d}-{day.month:02d}"
        if key != current_month:
            if current_month is not None:
                base = (
                    prior_month_last
                    if prior_month_last is not None
                    else month_first_close
                )
                monthly[current_month] = (
                    (month_last_close / base - 1.0) * 100.0
                    if base and month_last_close is not None
                    else None
                )
                prior_month_last = month_last_close
            current_month = key
            month_first_close = level
            month_last_close = level
        elif level is not None:
            if month_first_close is None:
                month_first_close = level
            month_last_close = level
    if current_month is not None:
        base = prior_month_last if prior_month_last is not None else month_first_close
        monthly[current_month] = (
            (month_last_close / base - 1.0) * 100.0
            if base and month_last_close is not None
            else None
        )

    return {
        "return_pct": _round(total),
        "coverage_pct": _round(coverage_pct, 2),
        "daily_returns": daily,
        "monthly_returns": {month: _round(value) for month, value in monthly.items()},
        "levels": levels,
    }


def _levels_to_returns(levels: Sequence[float | None]) -> list[float | None]:
    returns: list[float | None] = [None]
    for previous, current in zip(levels, levels[1:], strict=False):
        if previous is not None and current is not None and previous > 0:
            returns.append(current / previous - 1.0)
        else:
            returns.append(None)
    return returns


def excess_metrics(
    portfolio_returns: Sequence[float | None],
    benchmark_returns: Sequence[float | None],
    *,
    periods: int = PERIODS_PER_YEAR,
) -> dict[str, Any]:
    """日超额序列（组合 − 基准）的跟踪误差与信息比率。"""
    pairs = [
        (float(p), float(b))
        for p, b in zip(portfolio_returns, benchmark_returns, strict=False)
        if p is not None and b is not None
    ]
    if len(pairs) < 2:
        return {
            "sample_days": len(pairs),
            "tracking_error_pct": None,
            "information_ratio": None,
            "avg_daily_excess_pct": None,
        }
    excess = [p - b for p, b in pairs]
    mean = statistics.fmean(excess)
    stdev = statistics.stdev(excess)
    return {
        "sample_days": len(pairs),
        "tracking_error_pct": _round(stdev * math.sqrt(periods) * 100.0, 2),
        "information_ratio": _round(
            mean / stdev * math.sqrt(periods) if stdev > 0 else None
        ),
        "avg_daily_excess_pct": _round(mean * 100.0),
    }


def trade_stats(
    returns_pct: Sequence[float | None],
    pnl_amounts: Sequence[float | None],
    held_days: Sequence[int],
) -> dict[str, Any]:
    """已结算交易/批次的胜率、盈利因子、盈亏比与平均持仓天数。"""
    rows = [
        (float(ret), float(pnl), int(days))
        for ret, pnl, days in zip(returns_pct, pnl_amounts, held_days, strict=False)
        if ret is not None and pnl is not None
    ]
    if not rows:
        return {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "flats": 0,
            "win_rate_pct": None,
            "avg_return_pct": None,
            "avg_win_pct": None,
            "avg_loss_pct": None,
            "payoff_ratio": None,
            "gross_profit_amount": None,
            "gross_loss_amount": None,
            "profit_factor": None,
            "avg_holding_days": None,
        }
    returns = [row[0] for row in rows]
    wins = [row for row in rows if row[1] > 0]
    losses = [row for row in rows if row[1] < 0]
    flats = [row for row in rows if row[1] == 0]
    gross_profit = sum(row[1] for row in wins)
    gross_loss = sum(row[1] for row in losses)
    avg_win = statistics.fmean(row[0] for row in wins) if wins else None
    avg_loss = statistics.fmean(row[0] for row in losses) if losses else None
    return {
        "trades": len(rows),
        "wins": len(wins),
        "losses": len(losses),
        "flats": len(flats),
        "win_rate_pct": _round(len(wins) / len(rows) * 100.0, 2),
        "avg_return_pct": _round(statistics.fmean(returns)),
        "avg_win_pct": _round(avg_win),
        "avg_loss_pct": _round(avg_loss),
        "payoff_ratio": _round(avg_win / abs(avg_loss))
        if avg_win is not None and avg_loss not in (None, 0)
        else None,
        "gross_profit_amount": _round(gross_profit, 2),
        "gross_loss_amount": _round(gross_loss, 2),
        "profit_factor": _round(gross_profit / abs(gross_loss))
        if gross_loss < 0
        else None,
        "avg_holding_days": _round(statistics.fmean(row[2] for row in rows), 2),
    }


def market_day_stats(
    days: Sequence[date], index_closes: Mapping[date, float]
) -> dict[date, dict[str, Any]]:
    """每个交易日的大盘情况：涨跌幅、收盘、相对 20 日均线位置。"""
    ordered = sorted(index_closes.items())
    out: dict[date, dict[str, Any]] = {}
    for day in days:
        previous = _previous_close(ordered, day)
        close = _close(index_closes.get(day))
        change = (
            (close / previous - 1.0) * 100.0
            if close is not None and previous is not None and previous > 0
            else None
        )
        ma20 = _moving_average(ordered, day, window=20)
        deviation = (
            (close / ma20 - 1.0) * 100.0
            if close is not None and ma20 is not None and ma20 > 0
            else None
        )
        out[day] = {
            "change_pct": _round(change, 2),
            "close": _round(close, 2),
            "ma20_deviation_pct": _round(deviation, 2),
            "position": "unknown"
            if deviation is None
            else ("above_ma20" if deviation >= 0 else "below_ma20"),
            "tone": _tone(change),
        }
    return out


def _previous_close(ordered: Sequence[tuple[date, float]], day: date) -> float | None:
    previous = None
    for item_day, value in ordered:
        if item_day >= day:
            break
        previous = _close(value)
    return previous


def _moving_average(
    ordered: Sequence[tuple[date, float]], day: date, *, window: int
) -> float | None:
    values = [
        value
        for item_day, value in ordered
        if item_day <= day and _close(value) is not None
    ]
    if len(values) < window:
        return None
    return statistics.fmean(values[-window:])


def _tone(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value > 0:
        return "up"
    if value < 0:
        return "down"
    return "flat"


def change_pct(
    closes: Mapping[date, float] | Sequence[tuple[date, float]],
    day: date,
) -> float | None:
    """某标的当日涨跌幅（前一有效收盘 → 当日收盘）。"""
    ordered = sorted(closes.items()) if isinstance(closes, Mapping) else list(closes)
    close = _close(dict(ordered).get(day))
    previous = _previous_close(ordered, day)
    if close is None or previous is None or previous <= 0:
        return None
    return (close / previous - 1.0) * 100.0
