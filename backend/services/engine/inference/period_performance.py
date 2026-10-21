"""固定持有期滚动组合的周期收益模拟（纯函数，无 IO）。

口径（2026-10-21 用户拍板）：

- 建仓 = 评分日**收盘价**（尾盘口径）；到期 = 建仓后第 ``hold_days`` 个
  交易日收盘价退出（建仓日不算一天，``D`` → ``D+H``）；
- 每笔目标金额 = ``capital / positions``（初始资金按持仓上限等分），
  允许小数股；**不计手续费、印花税、过户费与滑点**；
- 已持有标的不加仓；退出后再次入选按新的一笔计；
- 到期日无有效收盘价（停牌/缺数据）→ 顺延到下一个有收盘价的交易日，
  绝不拿旧价假装成交；期末未到期持仓按最后有效收盘价计入总收益；
- 缺失价格绝不写 0：缺价不建仓，估值缺价保留上一有效价并标 ``stale``。

本模块只做计算；名称补全、行情读取、日期序列化与 HTTP 映射在 service 层。
"""

from __future__ import annotations

import calendar
import math
import statistics
from datetime import date
from typing import Any
from collections.abc import Mapping, Sequence

from backend.services.engine.inference import portfolio_metrics as metrics

RETURN_NDIGITS = 4
AMOUNT_NDIGITS = 2
PRICE_NDIGITS = 4
SHARE_NDIGITS = 4

#: 组合分日的浮点容差（现金判定用）
CASH_EPSILON = 1e-9


def _finite(value: Any) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _close(value: Any) -> float | None:
    """收盘价必须是正的有限数；否则视为缺失。"""
    if not _finite(value):
        return None
    number = float(value)
    return number if number > 0 else None


def _return_pct(entry_price: float | None, exit_price: float | None) -> float | None:
    if entry_price is None or exit_price is None or entry_price <= 0:
        return None
    return round((exit_price / entry_price - 1.0) * 100.0, RETURN_NDIGITS)


def _picks(picks_by_day: Mapping[date, Sequence[str]], day: date) -> list[str]:
    """当日候选（去重、保持榜单顺序）；空日按空列表处理。"""
    return list(dict.fromkeys(str(symbol) for symbol in (picks_by_day.get(day) or [])))


def _validate(
    days: Sequence[date],
    *,
    capital: float,
    positions: int,
    entry_per_day: int,
    hold_days: int,
) -> None:
    if positions < 1:
        raise ValueError("positions 必须 ≥1")
    if entry_per_day < 1:
        raise ValueError("entry_per_day 必须 ≥1")
    if hold_days < 1:
        raise ValueError("hold_days 必须 ≥1")
    if not _finite(capital) or float(capital) <= 0:
        raise ValueError("capital 必须是正的有限数")
    previous: date | None = None
    for day in days:
        if previous is not None and day <= previous:
            raise ValueError("days 必须是严格递增的交易日序列")
        previous = day


def simulate_period(
    *,
    days: Sequence[date],
    picks_by_day: Mapping[date, Sequence[str]],
    closes: Mapping[str, Mapping[date, float]],
    capital: float,
    positions: int,
    entry_per_day: int,
    hold_days: int,
    benchmark_closes: Mapping[date, float] | None = None,
) -> dict[str, Any]:
    """模拟固定持有期滚动组合，返回摘要 / 月度 / 日权益 / 交易明细。

    ``days`` 是区间内全部交易日（升序）；``picks_by_day`` 只在**评分日**
    有键，值是该日组合榜单前 ``positions`` 名（按榜单顺序）；
    ``closes`` 是 ``symbol → {date: close}``，前复权收盘价；
    ``benchmark_closes`` 是基准指数收盘价，缺失时基准/超额一律 ``None``。
    """
    ordered_days = list(days)
    capital = float(capital)
    positions = int(positions)
    entry_per_day = int(entry_per_day)
    hold_days = int(hold_days)
    _validate(
        ordered_days,
        capital=capital,
        positions=positions,
        entry_per_day=entry_per_day,
        hold_days=hold_days,
    )

    def close_of(symbol: str, day: date) -> float | None:
        return _close((closes.get(symbol) or {}).get(day))

    cash = capital
    open_rows: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    daily: list[dict[str, Any]] = []
    target_budget = capital / positions
    entries_total = 0
    entries_notional = 0.0
    realized_cum = 0.0
    missing_entry_price = 0
    deferred_exit = 0
    peak_equity = capital
    max_drawdown = 0.0

    for index, day in enumerate(ordered_days):
        exited_today = 0

        # 1) 到期退出：只认当日有效收盘价；缺价顺延，不猜价
        remaining: list[dict[str, Any]] = []
        for row in open_rows:
            if index < int(row["due_index"]):
                remaining.append(row)
                continue
            exit_price = close_of(row["symbol"], day)
            if exit_price is None:
                if not row["deferred"]:
                    row["deferred"] = True
                    deferred_exit += 1
                remaining.append(row)
                continue
            proceeds = float(row["shares"]) * exit_price
            cash += proceeds
            pnl = proceeds - float(row["notional"])
            realized_cum += pnl
            trades.append(
                {
                    "symbol": row["symbol"],
                    "entry_date": row["entry_date"],
                    "exit_date": day,
                    "entry_price": round(float(row["entry_price"]), PRICE_NDIGITS),
                    "exit_price": round(exit_price, PRICE_NDIGITS),
                    "shares": round(float(row["shares"]), SHARE_NDIGITS),
                    "notional": round(float(row["notional"]), AMOUNT_NDIGITS),
                    "return_pct": _return_pct(float(row["entry_price"]), exit_price),
                    "pnl_amount": round(pnl, AMOUNT_NDIGITS),
                    "held_days": index - int(row["entry_index"]),
                    "exit_reason": "到期",
                    "deferred": bool(row["deferred"]),
                }
            )
            exited_today += 1
        open_rows = remaining

        # 2) 建仓：已持有跳过；满仓/现金不足即停
        added_today = 0
        held = {str(row["symbol"]) for row in open_rows}
        for symbol in _picks(picks_by_day, day):
            if added_today >= entry_per_day or len(open_rows) >= positions:
                break
            if symbol in held:
                continue
            entry_price = close_of(symbol, day)
            if entry_price is None:
                missing_entry_price += 1
                continue
            budget = min(target_budget, cash)
            if budget <= CASH_EPSILON:
                break
            shares = budget / entry_price
            cash -= budget
            entries_notional += budget
            open_rows.append(
                {
                    "symbol": symbol,
                    "entry_date": day,
                    "entry_index": index,
                    "entry_price": entry_price,
                    "shares": shares,
                    "notional": budget,
                    "due_index": index + hold_days,
                    "mark_price": entry_price,
                    "mark_date": day,
                    "deferred": False,
                    "stale": False,
                }
            )
            held.add(symbol)
            added_today += 1
            entries_total += 1

        # 3) 收盘估值：缺价保留上一有效价并标记 stale
        for row in open_rows:
            mark = close_of(row["symbol"], day)
            if mark is not None:
                row["mark_price"] = mark
                row["mark_date"] = day
                row["stale"] = False
            else:
                row["stale"] = True
        market_value = sum(
            float(row["shares"]) * float(row["mark_price"]) for row in open_rows
        )
        unrealized = sum(
            float(row["shares"]) * float(row["mark_price"]) - float(row["notional"])
            for row in open_rows
        )
        equity = cash + market_value
        peak_equity = max(peak_equity, equity)
        if peak_equity > 0:
            max_drawdown = min(max_drawdown, equity / peak_equity - 1.0)
        previous_equity = daily[-1]["equity"] if daily else capital
        daily.append(
            {
                "date": day,
                "equity": round(equity, AMOUNT_NDIGITS),
                "cash": round(cash, AMOUNT_NDIGITS),
                "market_value": round(market_value, AMOUNT_NDIGITS),
                "exposure_pct": round(market_value / equity * 100.0, 2)
                if equity > 0
                else None,
                "positions": len(open_rows),
                "entries": added_today,
                "exits": exited_today,
                "realized_pnl_amount": round(realized_cum, AMOUNT_NDIGITS),
                "unrealized_pnl_amount": round(unrealized, AMOUNT_NDIGITS),
                "return_pct": round(
                    (equity / previous_equity - 1.0) * 100.0, RETURN_NDIGITS
                )
                if previous_equity > 0
                else None,
            }
        )

    final_equity = daily[-1]["equity"] if daily else round(capital, AMOUNT_NDIGITS)
    last_index = len(ordered_days) - 1
    open_payload: list[dict[str, Any]] = []
    for row in open_rows:
        mark_price = float(row["mark_price"])
        pnl = float(row["shares"]) * mark_price - float(row["notional"])
        open_payload.append(
            {
                "symbol": row["symbol"],
                "entry_date": row["entry_date"],
                "mark_date": row["mark_date"],
                "entry_price": round(float(row["entry_price"]), PRICE_NDIGITS),
                "mark_price": round(mark_price, PRICE_NDIGITS),
                "shares": round(float(row["shares"]), SHARE_NDIGITS),
                "notional": round(float(row["notional"]), AMOUNT_NDIGITS),
                "return_pct": _return_pct(float(row["entry_price"]), mark_price),
                "pnl_amount": round(pnl, AMOUNT_NDIGITS),
                "held_days": max(0, last_index - int(row["entry_index"])),
                "stale": bool(row["stale"]),
            }
        )

    portfolio_returns = metrics.daily_returns([float(row["equity"]) for row in daily])
    benchmark = metrics.benchmark_stats(ordered_days, benchmark_closes or {})
    benchmark_daily = benchmark["daily_returns"]
    for index, row in enumerate(daily):
        value = benchmark_daily[index] if index < len(benchmark_daily) else None
        row["benchmark_return_pct"] = (
            round(value * 100.0, RETURN_NDIGITS) if value is not None else None
        )
        row["excess_return_pct"] = (
            round(float(row["return_pct"]) - value * 100.0, RETURN_NDIGITS)
            if row["return_pct"] is not None and value is not None
            else None
        )

    summary = _summary(
        capital=capital,
        final_equity=final_equity,
        daily=daily,
        trades=trades,
        open_payload=open_payload,
        entries=entries_total,
        scoring_days=sum(1 for day in ordered_days if day in picks_by_day),
        max_drawdown=max_drawdown,
    )
    drawdown = metrics.drawdown_stats(
        ordered_days, [float(row["equity"]) for row in daily]
    )
    risk = metrics.risk_metrics(portfolio_returns)
    excess = metrics.excess_metrics(portfolio_returns, benchmark_daily)
    trades_stats = metrics.trade_stats(
        [trade["return_pct"] for trade in trades],
        [trade["pnl_amount"] for trade in trades],
        [trade["held_days"] for trade in trades],
    )
    total_return_pct = summary["total_return_pct"]
    annualized = metrics.annualized_return_pct(total_return_pct, len(daily))
    max_drawdown_pct = float(summary["max_drawdown_pct"] or 0.0)
    exposures = [
        float(row["exposure_pct"])
        for row in daily
        if row.get("exposure_pct") is not None
    ]
    benchmark_ok = bool(
        benchmark["coverage_pct"] is not None
        and float(benchmark["coverage_pct"]) >= metrics.BENCHMARK_MIN_COVERAGE_PCT
    )
    benchmark_total = benchmark["return_pct"] if benchmark_ok else None
    summary.update(
        {
            "realized_return_pct": round(
                float(summary["realized_pnl_amount"]) / capital * 100.0,
                RETURN_NDIGITS,
            )
            if capital > 0
            else None,
            "unrealized_return_pct": round(
                float(summary["unrealized_pnl_amount"]) / capital * 100.0,
                RETURN_NDIGITS,
            )
            if capital > 0
            else None,
            "benchmark_return_pct": benchmark_total,
            "benchmark_coverage_pct": benchmark["coverage_pct"],
            "excess_return_pct": round(
                total_return_pct - benchmark_total, RETURN_NDIGITS
            )
            if total_return_pct is not None and benchmark_total is not None
            else None,
            "annualized_return_pct": annualized,
            "volatility_pct": risk["volatility_pct"],
            "sharpe_ratio": risk["sharpe_ratio"],
            "sortino_ratio": risk["sortino_ratio"],
            "calmar_ratio": round(annualized / abs(max_drawdown_pct), 4)
            if annualized is not None and max_drawdown_pct < 0
            else None,
            "tracking_error_pct": excess["tracking_error_pct"]
            if benchmark_ok
            else None,
            "information_ratio": excess["information_ratio"] if benchmark_ok else None,
            "profit_factor": trades_stats["profit_factor"],
            "payoff_ratio": trades_stats["payoff_ratio"],
            "avg_win_pct": trades_stats["avg_win_pct"],
            "avg_loss_pct": trades_stats["avg_loss_pct"],
            "gross_profit_amount": trades_stats["gross_profit_amount"],
            "gross_loss_amount": trades_stats["gross_loss_amount"],
            "avg_holding_days": trades_stats["avg_holding_days"],
            "turnover_x": round(entries_notional / capital, 4) if capital > 0 else None,
            "avg_exposure_pct": round(statistics.fmean(exposures), 2)
            if exposures
            else None,
            "final_cash_pct": round(
                float(daily[-1]["cash"]) / float(daily[-1]["equity"]) * 100.0, 2
            )
            if daily and float(daily[-1]["equity"]) > 0
            else None,
            "drawdown_peak_date": drawdown["peak_date"],
            "drawdown_trough_date": drawdown["trough_date"],
            "drawdown_recovery_date": drawdown["recovery_date"],
            "drawdown_recovery_days": drawdown["recovery_days"],
        }
    )
    months = _months(
        capital, daily, trades, benchmark_monthly=benchmark["monthly_returns"]
    )
    stale_marks = sum(1 for row in open_payload if row["stale"])
    warnings = {
        "missing_entry_price": missing_entry_price,
        "deferred_exit": deferred_exit,
        "stale_mark": stale_marks,
        "positions_cap": positions < entry_per_day * hold_days,
        "underfill": positions > entry_per_day * hold_days,
    }
    return {
        "summary": summary,
        "months": months,
        "daily": daily,
        "trades": trades,
        "open": open_payload,
        "warnings": warnings,
    }


def _summary(
    *,
    capital: float,
    final_equity: float,
    daily: Sequence[Mapping[str, Any]],
    trades: Sequence[Mapping[str, Any]],
    open_payload: Sequence[Mapping[str, Any]],
    entries: int,
    scoring_days: int,
    max_drawdown: float,
) -> dict[str, Any]:
    returns = [
        float(row["return_pct"]) for row in trades if row["return_pct"] is not None
    ]
    wins = sum(1 for value in returns if value > 0)
    losses = sum(1 for value in returns if value < 0)
    flats = sum(1 for value in returns if value == 0)
    positions = [int(row["positions"]) for row in daily]
    realized = sum(float(row["pnl_amount"]) for row in trades)
    unrealized = sum(float(row["pnl_amount"]) for row in open_payload)
    return {
        "initial_capital": round(capital, AMOUNT_NDIGITS),
        "final_equity": round(final_equity, AMOUNT_NDIGITS),
        "total_return_pct": round(
            (final_equity / capital - 1.0) * 100.0, RETURN_NDIGITS
        )
        if capital > 0
        else None,
        "profit_amount": round(final_equity - capital, AMOUNT_NDIGITS),
        "realized_pnl_amount": round(realized, AMOUNT_NDIGITS),
        "unrealized_pnl_amount": round(unrealized, AMOUNT_NDIGITS),
        "max_drawdown_pct": round(max_drawdown * 100.0, RETURN_NDIGITS),
        "trading_days": len(daily),
        "scoring_days": scoring_days,
        "entries": entries,
        "exits": len(trades),
        "closed_trades": len(trades),
        "open_positions": len(open_payload),
        "wins": wins,
        "losses": losses,
        "flats": flats,
        "win_rate_pct": round(wins / len(returns) * 100.0, 2) if returns else None,
        "avg_trade_return_pct": round(statistics.fmean(returns), RETURN_NDIGITS)
        if returns
        else None,
        "avg_positions": round(statistics.fmean(positions), 2) if positions else 0.0,
        "entry_convention": "close",
        "fee_convention": "none",
        "data_quality": "complete",
    }


def _months(
    capital: float,
    daily: Sequence[Mapping[str, Any]],
    trades: Sequence[Mapping[str, Any]],
    *,
    benchmark_monthly: Mapping[str, float | None] | None = None,
) -> list[dict[str, Any]]:
    """按自然月拆解；首月以上一期=初始资金为基准。"""
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in daily:
        day = row["date"]
        grouped.setdefault(f"{day.year:04d}-{day.month:02d}", []).append(row)
    if not grouped:
        return []

    trades_by_month: dict[str, list[Mapping[str, Any]]] = {}
    for trade in trades:
        day = trade["exit_date"]
        trades_by_month.setdefault(f"{day.year:04d}-{day.month:02d}", []).append(trade)

    out: list[dict[str, Any]] = []
    previous_end = float(capital)
    previous_realized = 0.0
    for month, rows in grouped.items():
        year, month_number = (int(part) for part in month.split("-"))
        first_day = rows[0]["date"]
        last_day = rows[-1]["date"]
        end_equity = float(rows[-1]["equity"])
        month_trades = trades_by_month.get(month, [])
        month_returns = [
            float(row["return_pct"])
            for row in month_trades
            if row["return_pct"] is not None
        ]
        month_start = date(year, month_number, 1)
        month_end = date(year, month_number, calendar.monthrange(year, month_number)[1])
        month_return = (
            round((end_equity / previous_end - 1.0) * 100.0, RETURN_NDIGITS)
            if previous_end > 0
            else None
        )
        month_realized = float(rows[-1].get("realized_pnl_amount") or 0.0)
        benchmark_return = (benchmark_monthly or {}).get(month)
        out.append(
            {
                "month": month,
                "start_date": first_day,
                "end_date": last_day,
                "start_equity": round(previous_end, AMOUNT_NDIGITS),
                "end_equity": round(end_equity, AMOUNT_NDIGITS),
                "return_pct": month_return,
                "profit_amount": round(end_equity - previous_end, AMOUNT_NDIGITS),
                "realized_pnl_amount": round(
                    month_realized - previous_realized, AMOUNT_NDIGITS
                ),
                "unrealized_pnl_amount": round(
                    float(rows[-1].get("unrealized_pnl_amount") or 0.0),
                    AMOUNT_NDIGITS,
                ),
                "benchmark_return_pct": benchmark_return,
                "excess_return_pct": round(
                    month_return - benchmark_return, RETURN_NDIGITS
                )
                if month_return is not None and benchmark_return is not None
                else None,
                "entries": sum(int(row["entries"]) for row in rows),
                "exits": sum(int(row["exits"]) for row in rows),
                "closed_trades": len(month_trades),
                "month_end_positions": int(rows[-1]["positions"]),
                "win_rate_pct": round(
                    sum(1 for value in month_returns if value > 0)
                    / len(month_returns)
                    * 100.0,
                    2,
                )
                if month_returns
                else None,
                "partial": month_start < first_day or month_end > last_day,
            }
        )
        previous_end = end_equity
        previous_realized = month_realized
    return out
