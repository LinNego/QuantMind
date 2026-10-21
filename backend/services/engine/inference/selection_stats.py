"""截面选股 · 单模型 TopK 历史统计（纯函数，无 IO）。

口径（与 ``docs/截面选股_Top50历史统计_细案_20261021.md`` 一致）：

- 信号日 T → T 之后第 ``execution_lag_days`` 个交易日收盘入选；
- 入选后第 ``hold_days`` 个交易日收盘退出（入选日不计）；
- 每期把当期全部权益等权投入，期收益 = 有效标等权平均，权益逐期复利；
- 期与期首尾相接（下一期入选日 = 上一期退出日），不重叠、不留空档；
- 缺失价格：入选日缺 → 剔除；退出日缺 → 用窗口内最后有效收盘价；
  全无 → 剔除；整期 0 只 → 跳过且权益不变。**任何缺失绝不写 0**。

本模块只做数学，不读 parquet、不碰数据库。价格由调用方传入（单位资金）。
"""

from __future__ import annotations

import math
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from statistics import fmean, median

MIN_PARTIAL_RATIO = 0.8
"""单期有效标的低于请求数的该比例时标记 ``partial``。"""


@dataclass(frozen=True)
class CohortWindow:
    """一期的时间窗（索引均指向 ``calendar``）。"""

    signal_date: date
    entry_index: int
    exit_index: int | None
    mark_index: int


def plan_cohorts(
    calendar: Sequence[date],
    signal_days: Sequence[date],
    *,
    hold_days: int,
    execution_lag_days: int = 1,
) -> list[CohortWindow]:
    """把信号日切成不重叠的期。

    仅当本期入选日不早于上一期退出日时接受；入选日必须是行情日历里
    非最后一天（至少留一天持有）。行情末尾未到期的期以 ``exit_index=None``
    表示，按行情末日估值。
    """
    if hold_days < 1:
        raise ValueError("hold_days must be >= 1")
    if execution_lag_days < 0:
        raise ValueError("execution_lag_days must be >= 0")
    if not calendar:
        return []

    index_of = {d: i for i, d in enumerate(calendar)}
    last_index = len(calendar) - 1
    windows: list[CohortWindow] = []
    last_exit = -1

    for signal_day in sorted(set(signal_days)):
        signal_index = index_of.get(signal_day)
        if signal_index is None:
            continue
        entry_index = signal_index + execution_lag_days
        # 入选日必须是最后一天之前，否则没有任何持有时间。
        if entry_index >= last_index:
            continue
        if last_exit >= 0 and entry_index < last_exit:
            continue
        scheduled_exit = entry_index + hold_days
        if scheduled_exit < len(calendar):
            exit_index: int | None = scheduled_exit
            mark_index = scheduled_exit
            last_exit = scheduled_exit
        else:
            exit_index = None
            mark_index = last_index
            last_exit = last_index
        windows.append(
            CohortWindow(
                signal_date=signal_day,
                entry_index=entry_index,
                exit_index=exit_index,
                mark_index=mark_index,
            )
        )
    return windows


def _is_price(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and float(value) > 0
    )


def _pct(value: float | None) -> float | None:
    return None if value is None else round(value * 100.0, 4)


def _round(value: float | None, digits: int = 6) -> float | None:
    return None if value is None else round(value, digits)


def _find_last_close(
    series: Mapping[date, float],
    calendar: Sequence[date],
    *,
    start_index: int,
    end_index: int,
) -> tuple[float | None, date | None]:
    """在 ``(start_index, end_index]`` 内从后向前找第一个有效收盘价。"""
    for index in range(end_index, start_index, -1):
        price = series.get(calendar[index])
        if _is_price(price):
            return float(price), calendar[index]
    return None, None


def simulate_selection(
    *,
    calendar: Sequence[date],
    picks_by_signal: Mapping[date, Sequence[str]],
    closes: Mapping[str, Mapping[date, float]],
    hold_days: int,
    execution_lag_days: int = 1,
    benchmark_closes: Mapping[date, float] | None = None,
    top_k: int = 50,
) -> dict:
    """执行 TopK 历史组合模拟，返回可序列化字典（权益为倍数）。"""
    if top_k < 1:
        raise ValueError("top_k must be >= 1")
    calendar = list(calendar)
    windows = plan_cohorts(
        calendar,
        list(picks_by_signal),
        hold_days=hold_days,
        execution_lag_days=execution_lag_days,
    )

    equity = 1.0
    rows: list[dict] = []
    name_returns: list[float] = []
    points: list[dict] = []
    counters = {
        "missing_entry_price": 0,
        "missing_exit_price": 0,
        "stale_exit": 0,
        "underfill_signals": 0,
        "partial_cohorts": 0,
        "skipped_cohorts": 0,
        "open_cohorts": 0,
    }
    if windows:
        first_entry = calendar[windows[0].entry_index]
        points.append({"date": first_entry.isoformat(), "equity": 1.0})

    for order, window in enumerate(windows, start=1):
        signal_day = window.signal_date
        entry_day = calendar[window.entry_index]
        picks = list(picks_by_signal.get(signal_day, ()))[:top_k]
        held = len(picks)
        if held < top_k:
            counters["underfill_signals"] += 1

        returns: list[float] = []
        exit_days: list[date] = []
        missing_entry = 0
        missing_exit = 0
        stale = 0
        for symbol in picks:
            series = closes.get(symbol) or {}
            entry_price = series.get(entry_day)
            if not _is_price(entry_price):
                missing_entry += 1
                continue

            exit_price: float | None = None
            exit_day: date | None = None
            if window.exit_index is not None:
                scheduled = series.get(calendar[window.exit_index])
                if _is_price(scheduled):
                    exit_price = float(scheduled)
                    exit_day = calendar[window.exit_index]
            if exit_price is None:
                exit_price, exit_day = _find_last_close(
                    series,
                    calendar,
                    start_index=window.entry_index,
                    end_index=window.mark_index,
                )
                if exit_price is not None and window.exit_index is not None:
                    stale += 1
            if exit_price is None or exit_day is None:
                missing_exit += 1
                continue
            returns.append(exit_price / float(entry_price) - 1.0)
            exit_days.append(exit_day)

        counters["missing_entry_price"] += missing_entry
        counters["missing_exit_price"] += missing_exit
        counters["stale_exit"] += stale
        name_returns.extend(returns)

        priced = len(returns)
        is_open = window.exit_index is None
        skipped = priced == 0
        partial = held > 0 and priced < held * MIN_PARTIAL_RATIO
        if is_open:
            counters["open_cohorts"] += 1
        if skipped:
            counters["skipped_cohorts"] += 1
        if partial:
            counters["partial_cohorts"] += 1

        cohort_return = fmean(returns) if returns else None
        if cohort_return is not None:
            equity *= 1.0 + cohort_return

        mark_day = max(exit_days) if exit_days else calendar[window.mark_index]
        benchmark_return: float | None = None
        if benchmark_closes:
            base = benchmark_closes.get(entry_day)
            marked = benchmark_closes.get(mark_day)
            if _is_price(base) and _is_price(marked):
                benchmark_return = float(marked) / float(base) - 1.0

        rows.append(
            {
                "index": order,
                "signal_date": signal_day.isoformat(),
                "entry_date": entry_day.isoformat(),
                "exit_date": None if is_open else mark_day.isoformat(),
                "mark_date": mark_day.isoformat(),
                "exit_kind": "open" if is_open else "closed",
                "held": held,
                "priced": priced,
                "missing_entry": missing_entry,
                "missing_exit": missing_exit,
                "stale_exit": stale,
                "return_pct": _pct(cohort_return),
                "benchmark_return_pct": _pct(benchmark_return),
                "excess_return_pct": _pct(
                    None
                    if cohort_return is None or benchmark_return is None
                    else cohort_return - benchmark_return
                ),
                "equity": _round(equity),
                "partial": bool(partial),
                "skipped": bool(skipped),
            }
        )

        if not points or points[-1]["date"] != mark_day.isoformat():
            points.append({"date": mark_day.isoformat(), "equity": _round(equity)})

    priced_rows = [row for row in rows if row["return_pct"] is not None]
    total_return = (equity - 1.0) if priced_rows else None

    benchmark_chain: float | None = 1.0 if priced_rows else None
    for row in priced_rows:
        value = row["benchmark_return_pct"]
        if value is None:
            benchmark_chain = None
            break
        if benchmark_chain is not None:
            benchmark_chain *= 1.0 + value / 100.0
    benchmark_total = benchmark_chain - 1.0 if benchmark_chain is not None else None
    excess_total = (
        None
        if total_return is None or benchmark_total is None
        else total_return - benchmark_total
    )

    wins = sum(1 for row in priced_rows if (row["return_pct"] or 0.0) > 0)
    losses = sum(1 for row in priced_rows if (row["return_pct"] or 0.0) < 0)
    flats = len(priced_rows) - wins - losses
    cohort_returns = [row["return_pct"] / 100.0 for row in priced_rows]

    requested_picks = sum(row["held"] for row in rows)
    priced_picks = sum(row["priced"] for row in rows)
    pick_wins = sum(1 for value in name_returns if value > 0)

    peak = points[0]["equity"] if points else 1.0
    worst_drawdown = 0.0
    for point in points:
        peak = max(peak, point["equity"])
        if peak > 0:
            worst_drawdown = min(worst_drawdown, point["equity"] / peak - 1.0)

    if not priced_rows:
        data_quality = "unavailable"
    elif (
        counters["missing_entry_price"]
        or counters["missing_exit_price"]
        or counters["stale_exit"]
    ):
        data_quality = "partial"
    else:
        data_quality = "complete"

    summary = {
        "total_return_pct": _pct(total_return),
        "max_drawdown_pct": _pct(worst_drawdown) if points else None,
        "benchmark_return_pct": _pct(benchmark_total),
        "excess_return_pct": _pct(excess_total),
        "cohorts": len(rows),
        "closed_cohorts": len(rows) - counters["open_cohorts"],
        "open_cohorts": counters["open_cohorts"],
        "win_cohorts": wins,
        "loss_cohorts": losses,
        "flat_cohorts": flats,
        "cohort_win_rate_pct": _pct(wins / len(priced_rows)) if priced_rows else None,
        "avg_cohort_return_pct": _pct(fmean(cohort_returns))
        if cohort_returns
        else None,
        "median_cohort_return_pct": _pct(median(cohort_returns))
        if cohort_returns
        else None,
        "picks": requested_picks,
        "priced_picks": priced_picks,
        "pick_win_rate_pct": _pct(pick_wins / priced_picks) if priced_picks else None,
        "avg_pick_return_pct": _pct(fmean(name_returns)) if name_returns else None,
        "price_coverage_pct": _pct(priced_picks / requested_picks)
        if requested_picks
        else None,
        "data_quality": data_quality,
    }

    warnings = _warnings(
        counters,
        has_benchmark_gap=any(
            row["benchmark_return_pct"] is None for row in priced_rows
        ),
    )

    return {
        "summary": summary,
        "months": _months(rows),
        "cohorts": rows,
        "points": points,
        "warnings": warnings,
        "quality": {
            **counters,
            "signals": len(picks_by_signal),
            "requested_picks": requested_picks,
            "priced_picks": priced_picks,
        },
    }


def _warnings(counters: Mapping[str, int], *, has_benchmark_gap: bool) -> list[str]:
    warnings: list[str] = []
    if counters["underfill_signals"]:
        warnings.append("underfill_signals")
    if counters["missing_entry_price"]:
        warnings.append("missing_entry_price")
    if counters["missing_exit_price"]:
        warnings.append("missing_exit_price")
    if counters["stale_exit"]:
        warnings.append("stale_exit")
    if counters["partial_cohorts"]:
        warnings.append("partial_cohorts")
    if counters["skipped_cohorts"]:
        warnings.append("skipped_cohorts")
    if counters["open_cohorts"]:
        warnings.append("open_cohort")
    if has_benchmark_gap:
        warnings.append("benchmark_unavailable")
    return warnings


def _months(rows: Sequence[dict]) -> list[dict]:
    grouped: OrderedDict[str, list[dict]] = OrderedDict()
    for row in rows:
        grouped.setdefault(row["mark_date"][:7], []).append(row)

    months: list[dict] = []
    equity_before = 1.0
    month_keys = list(grouped)
    for position, month in enumerate(month_keys):
        group = grouped[month]
        priced = [row for row in group if row["return_pct"] is not None]
        end_equity = group[-1]["equity"]
        month_return = (
            end_equity / equity_before - 1.0 if priced and equity_before > 0 else None
        )

        benchmark_chain: float | None = 1.0 if priced else None
        for row in priced:
            value = row["benchmark_return_pct"]
            if value is None:
                benchmark_chain = None
                break
            if benchmark_chain is not None:
                benchmark_chain *= 1.0 + value / 100.0
        benchmark_return = (
            benchmark_chain - 1.0 if benchmark_chain is not None else None
        )

        wins = sum(1 for row in priced if (row["return_pct"] or 0.0) > 0)
        months.append(
            {
                "month": month,
                "start_date": group[0]["entry_date"],
                "end_date": group[-1]["mark_date"],
                "start_equity": _round(equity_before),
                "end_equity": _round(end_equity),
                "return_pct": _pct(month_return),
                "profit_amount": _round(
                    None if month_return is None else end_equity - equity_before
                ),
                "cohorts": len(group),
                "win_rate_pct": _pct(wins / len(priced)) if priced else None,
                "benchmark_return_pct": _pct(benchmark_return),
                "excess_return_pct": _pct(
                    None
                    if month_return is None or benchmark_return is None
                    else month_return - benchmark_return
                ),
                "partial": position == 0 or position == len(month_keys) - 1,
            }
        )
        equity_before = end_equity
    return months
