"""滚动建仓窗口的收盘价损益口径（纯函数，无 IO）。

口径（2026-10-21 用户拍板）：

- 买入 = 入选当日**收盘价**（尾盘买入）；卖出 = 退出当日收盘价；
- 一笔交易 = 从窗口内首次在榜到首次不在榜；断榜后重进按新一笔计；
- 窗口首列的标的看不到窗口外更早的入场日，按首列收盘价买入并标记
  ``entry_from_window_start=True``；
- 缺失/非法收盘价 → ``return_pct=None``（展示 `—`，绝不写 0），且不计入
  胜率/均值分母；
- 组合口径：每日等权持有当日 TopK，收盘换仓，次日收盘结算，
  ``cycle_return_pct = Π(1 + r_t) − 1``（未计费用）。

机构级统计（2026-10-21）：``simulate_daily_rotation`` 给出每日批次的
已实现/浮动拆分、基准与超额、风险指标；窗口最后一个批次只估值不结算
（计入浮动），末日入选只作为下一批次候选，不建仓。``cell_stats`` 给出
每只标的在窗口内的在榜表现与每个格子的当日涨跌幅/当日盈亏。

本模块只做计算；名称/行业/市值的补全与日期序列化在 service 层完成。
"""

from __future__ import annotations

import math
import statistics
from datetime import date
from typing import Any
from collections.abc import Mapping, Sequence

from backend.services.engine.inference import portfolio_metrics as metrics

RETURN_NDIGITS = 4
WIN_RATE_NDIGITS = 2
AMOUNT_NDIGITS = 2
DEFAULT_CAPITAL = 10_000_000.0


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


def _sym_closes(
    closes: Mapping[str, Mapping[date, float]], symbol: str
) -> Mapping[date, float]:
    return closes.get(symbol) or {}


def simulate_daily_rotation(
    *,
    days: Sequence[date],
    picks_by_day: Mapping[date, Sequence[str]],
    closes: Mapping[str, Mapping[date, float]],
    capital: float = DEFAULT_CAPITAL,
    benchmark_closes: Mapping[date, float] | None = None,
) -> dict[str, Any]:
    """每日等权换仓的组合账本（批次口径）。

    - 入选日收盘按全部权益等权建立批次，次一交易日收盘结算；
    - 窗口最后一个批次只在末日收盘估值（未结算，计入浮动），末日入选
      作为下一批次候选、不建仓；
    - 缺价标的不计价并减少批次分母；批次无任何有价标的时保持现金；
    - ``realized + unrealized == final_equity - initial_capital``（恒等式）。
    """
    ordered_days = list(days)
    if capital <= 0:
        raise ValueError("capital 必须为正数")
    if not ordered_days:
        return _empty_rotation(float(capital), benchmark_closes)

    equity = float(capital)
    equity_by_day: dict[date, float] = {ordered_days[0]: equity}
    batches: list[dict[str, Any]] = []
    daily: list[dict[str, Any]] = []
    exposures: list[float] = [0.0]
    turnover_amount = 0.0

    for index in range(len(ordered_days) - 1):
        entry_day = ordered_days[index]
        exit_day = ordered_days[index + 1]
        picks = _day_picks(picks_by_day, entry_day)
        priced: list[float] = []
        for symbol in picks:
            series = _sym_closes(closes, symbol)
            value = _return_pct(
                _close(series.get(entry_day)), _close(series.get(exit_day))
            )
            if value is not None:
                priced.append(value)

        notional = equity
        batch_return = (
            round(statistics.fmean(priced), RETURN_NDIGITS) if priced else None
        )
        pnl_amount = (
            round(notional * batch_return / 100.0, AMOUNT_NDIGITS)
            if batch_return is not None
            else None
        )
        if batch_return is not None:
            equity = notional * (1.0 + batch_return / 100.0)
        settled = index < len(ordered_days) - 2
        equity_by_day[exit_day] = equity
        if picks:
            batches.append(
                {
                    "entry_date": entry_day,
                    "exit_date": exit_day,
                    "positions": len(picks),
                    "priced": len(priced),
                    "return_pct": batch_return,
                    "notional_amount": round(notional, AMOUNT_NDIGITS),
                    "pnl_amount": pnl_amount,
                    "equity": round(equity, AMOUNT_NDIGITS),
                    "settled": settled,
                    "holding_days": 1,
                }
            )
        if priced:
            turnover_amount += notional
        exposures.append(100.0 if priced else 0.0)
        daily.append(
            {
                "date": entry_day,
                "exit_date": exit_day,
                "return_pct": batch_return,
                "equity": round(equity, AMOUNT_NDIGITS),
                "market_value": round(equity, AMOUNT_NDIGITS) if priced else 0.0,
                "cash": 0.0 if priced else round(equity, AMOUNT_NDIGITS),
                "exposure_pct": 100.0 if priced else 0.0,
                "benchmark_return_pct": None,
                "excess_return_pct": None,
                "settled": settled,
            }
        )

    benchmark = metrics.benchmark_stats(ordered_days, benchmark_closes or {})
    benchmark_daily = benchmark["daily_returns"]
    for index, row in enumerate(daily):
        value = benchmark_daily[index + 1] if index + 1 < len(benchmark_daily) else None
        if value is None:
            continue
        row["benchmark_return_pct"] = round(value * 100.0, RETURN_NDIGITS)
        if row["return_pct"] is not None:
            row["excess_return_pct"] = round(
                float(row["return_pct"]) - value * 100.0, RETURN_NDIGITS
            )

    summary = _rotation_summary(
        ordered_days=ordered_days,
        capital=float(capital),
        equity_by_day=equity_by_day,
        batches=batches,
        daily=daily,
        benchmark=benchmark,
        exposures=exposures,
        turnover_amount=turnover_amount,
    )
    return {
        "batches": batches,
        "daily": daily,
        "equity_by_day": equity_by_day,
        "summary": summary,
    }


def _empty_rotation(
    capital: float, benchmark_closes: Mapping[date, float] | None
) -> dict[str, Any]:
    benchmark = metrics.benchmark_stats([], benchmark_closes or {})
    return {
        "batches": [],
        "daily": [],
        "equity_by_day": {},
        "summary": _rotation_summary(
            ordered_days=[],
            capital=capital,
            equity_by_day={},
            batches=[],
            daily=[],
            benchmark=benchmark,
            exposures=[],
            turnover_amount=0.0,
        ),
    }


def _rotation_summary(
    *,
    ordered_days: Sequence[date],
    capital: float,
    equity_by_day: Mapping[date, float],
    batches: Sequence[Mapping[str, Any]],
    daily: Sequence[Mapping[str, Any]],
    benchmark: Mapping[str, Any],
    exposures: Sequence[float],
    turnover_amount: float,
) -> dict[str, Any]:
    final_equity = (
        float(equity_by_day[ordered_days[-1]]) if ordered_days else float(capital)
    )
    settled_equity = (
        float(equity_by_day[ordered_days[-2]])
        if len(ordered_days) >= 2
        else float(capital)
    )
    realized = settled_equity - capital
    unrealized = final_equity - settled_equity
    total_return_pct = round((final_equity / capital - 1.0) * 100.0, RETURN_NDIGITS)

    equity_series = [float(equity_by_day[day]) for day in ordered_days]
    returns = metrics.daily_returns(equity_series)
    risk = metrics.risk_metrics(returns)
    drawdown = metrics.drawdown_stats(ordered_days, equity_series)
    excess = metrics.excess_metrics(returns, benchmark["daily_returns"])

    settled_batches = [batch for batch in batches if batch["settled"]]
    trade = metrics.trade_stats(
        [batch["return_pct"] for batch in settled_batches],
        [batch["pnl_amount"] for batch in settled_batches],
        [int(batch["holding_days"]) for batch in settled_batches],
    )

    coverage = benchmark["coverage_pct"]
    benchmark_ok = bool(
        coverage is not None and float(coverage) >= metrics.BENCHMARK_MIN_COVERAGE_PCT
    )
    benchmark_return = benchmark["return_pct"] if benchmark_ok else None
    excess_return = (
        round(total_return_pct - float(benchmark_return), RETURN_NDIGITS)
        if benchmark_return is not None
        else None
    )

    trading_days = max(0, len(ordered_days) - 1)
    annualized = metrics.annualized_return_pct(total_return_pct, trading_days)
    max_drawdown = drawdown["max_drawdown_pct"]
    calmar = (
        round(float(annualized) / abs(float(max_drawdown)), metrics.RATIO_NDIGITS)
        if annualized is not None and max_drawdown not in (None, 0)
        else None
    )
    cycle_values = [
        float(row["return_pct"]) for row in daily if row["return_pct"] is not None
    ]

    return {
        "initial_capital": round(capital, AMOUNT_NDIGITS),
        "final_equity": round(final_equity, AMOUNT_NDIGITS),
        "profit_amount": round(final_equity - capital, AMOUNT_NDIGITS),
        "total_return_pct": total_return_pct,
        "cycle_return_pct": total_return_pct,
        "realized_pnl_amount": round(realized, AMOUNT_NDIGITS),
        "realized_return_pct": round(realized / capital * 100.0, RETURN_NDIGITS),
        "unrealized_pnl_amount": round(unrealized, AMOUNT_NDIGITS),
        "unrealized_return_pct": round(unrealized / capital * 100.0, RETURN_NDIGITS),
        "benchmark_return_pct": benchmark_return,
        "benchmark_coverage_pct": coverage,
        "excess_return_pct": excess_return,
        "annualized_return_pct": annualized,
        "volatility_pct": risk["volatility_pct"],
        "sharpe_ratio": risk["sharpe_ratio"],
        "sortino_ratio": risk["sortino_ratio"],
        "calmar_ratio": calmar,
        "max_drawdown_pct": max_drawdown,
        "drawdown_peak_date": drawdown["peak_date"],
        "drawdown_trough_date": drawdown["trough_date"],
        "drawdown_recovery_date": drawdown["recovery_date"],
        "drawdown_recovery_days": drawdown["recovery_days"],
        "tracking_error_pct": excess["tracking_error_pct"] if benchmark_ok else None,
        "information_ratio": excess["information_ratio"] if benchmark_ok else None,
        "avg_daily_excess_pct": excess["avg_daily_excess_pct"]
        if benchmark_ok
        else None,
        "trades": trade["trades"],
        "wins": trade["wins"],
        "losses": trade["losses"],
        "flats": trade["flats"],
        "win_rate_pct": trade["win_rate_pct"],
        "avg_return_pct": trade["avg_return_pct"],
        "avg_win_pct": trade["avg_win_pct"],
        "avg_loss_pct": trade["avg_loss_pct"],
        "payoff_ratio": trade["payoff_ratio"],
        "profit_factor": trade["profit_factor"],
        "gross_profit_amount": trade["gross_profit_amount"],
        "gross_loss_amount": trade["gross_loss_amount"],
        "avg_holding_days": trade["avg_holding_days"],
        "turnover_x": round(turnover_amount / capital, metrics.RATIO_NDIGITS),
        "avg_exposure_pct": round(statistics.fmean(exposures), 2)
        if exposures
        else None,
        "final_cash_pct": (
            round(float(daily[-1]["cash"]) / final_equity * 100.0, 2)
            if daily and final_equity > 0
            else 100.0
        ),
        "trading_days": trading_days,
        "batches": len(batches),
        "settled_batches": len(settled_batches),
        "open_batches": len(batches) - len(settled_batches),
        "priced_batches": sum(
            1 for batch in batches if batch["return_pct"] is not None
        ),
        "unpriced_batches": sum(1 for batch in batches if batch["return_pct"] is None),
        "partial_batches": sum(
            1 for batch in batches if 0 < batch["priced"] < batch["positions"]
        ),
        "cycle_days": len(daily),
        "cycle_priced_days": len(cycle_values),
        "cycle_win_days": sum(1 for value in cycle_values if value > 0),
        "cycle_loss_days": sum(1 for value in cycle_values if value < 0),
        "entry_convention": "close",
    }


def cell_stats(
    days: Sequence[date],
    picks_by_day: Mapping[date, Sequence[str]],
    closes: Mapping[str, Mapping[date, float]],
    *,
    equity_by_day: Mapping[date, float] | None = None,
) -> dict[str, Any]:
    """每只标的的窗口内表现 + 每个格子的当日涨跌幅/当日盈亏。

    - ``by_symbol``：在榜天数、当前连续在榜、首次入选、首次入选至今涨跌、
      在榜日次日收益均值/胜率/样本/标准差、末日收盘与当日涨跌；
    - ``by_day``：日期 × 标的 → 当日涨跌幅、当日持仓权重与当日盈亏金额
      （仅统计前一交易日已在榜、即当日实际持有的标的；未持有为 ``None``）。
    """
    ordered_days = list(days)
    last_day = ordered_days[-1] if ordered_days else None
    appearances: dict[str, list[date]] = {}
    for day in ordered_days:
        for symbol in _day_picks(picks_by_day, day):
            appearances.setdefault(symbol, []).append(day)

    streak: dict[str, int] = {}
    for day in ordered_days:
        today = set(_day_picks(picks_by_day, day))
        for symbol in today:
            streak[symbol] = streak.get(symbol, 0) + 1
        streak = {symbol: count for symbol, count in streak.items() if symbol in today}

    by_symbol: dict[str, dict[str, Any]] = {}
    for symbol, days_in_list in appearances.items():
        series = _sym_closes(closes, symbol)
        listed = set(days_in_list)
        next_returns: list[float] = []
        for index in range(len(ordered_days) - 1):
            day = ordered_days[index]
            if day not in listed:
                continue
            value = _return_pct(
                _close(series.get(day)), _close(series.get(ordered_days[index + 1]))
            )
            if value is not None:
                next_returns.append(value)
        first_day = days_in_list[0]
        latest_close = _close(series.get(last_day)) if last_day is not None else None
        latest_change = (
            metrics.change_pct(series, last_day) if last_day is not None else None
        )
        by_symbol[symbol] = {
            "appearances": len(days_in_list),
            "streak_days": streak.get(symbol, 0),
            "first_seen": first_day,
            "since_first_pct": _return_pct(_close(series.get(first_day)), latest_close),
            "next_day_avg_pct": round(statistics.fmean(next_returns), RETURN_NDIGITS)
            if next_returns
            else None,
            "next_day_win_rate_pct": round(
                sum(1 for value in next_returns if value > 0)
                / len(next_returns)
                * 100.0,
                WIN_RATE_NDIGITS,
            )
            if next_returns
            else None,
            "next_day_samples": len(next_returns),
            "next_day_std_pct": round(statistics.stdev(next_returns), RETURN_NDIGITS)
            if len(next_returns) >= 3
            else None,
            "latest_close": round(latest_close, RETURN_NDIGITS)
            if latest_close is not None
            else None,
            "change_pct": round(latest_change, RETURN_NDIGITS)
            if latest_change is not None
            else None,
        }

    by_day: dict[date, dict[str, dict[str, Any]]] = {day: {} for day in ordered_days}
    for index, day in enumerate(ordered_days):
        picks = _day_picks(picks_by_day, day)
        previous_day = ordered_days[index - 1] if index > 0 else None
        previous_picks = (
            _day_picks(picks_by_day, previous_day) if previous_day is not None else []
        )
        previous_equity = (
            float(equity_by_day[previous_day])
            if equity_by_day is not None and previous_day in equity_by_day
            else None
        )
        priced_previous: list[str] = []
        if previous_day is not None:
            priced_previous = [
                symbol
                for symbol in previous_picks
                if _close(_sym_closes(closes, symbol).get(previous_day)) is not None
                and _close(_sym_closes(closes, symbol).get(day)) is not None
            ]
        previous_set = set(previous_picks)
        for symbol in picks:
            series = _sym_closes(closes, symbol)
            change = metrics.change_pct(series, day)
            cell: dict[str, Any] = {
                "change_pct": round(change, RETURN_NDIGITS)
                if change is not None
                else None,
                "weight_pct": None,
                "pnl_amount": None,
            }
            if (
                previous_day is not None
                and symbol in previous_set
                and previous_equity is not None
                and priced_previous
                and symbol in priced_previous
            ):
                value = _return_pct(
                    _close(series.get(previous_day)), _close(series.get(day))
                )
                if value is not None:
                    weight = 1.0 / len(priced_previous)
                    cell["weight_pct"] = round(weight * 100.0, RETURN_NDIGITS)
                    cell["pnl_amount"] = round(
                        previous_equity * weight * value / 100.0, AMOUNT_NDIGITS
                    )
            by_day[day][symbol] = cell

    return {"by_symbol": by_symbol, "by_day": by_day}
