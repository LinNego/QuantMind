"""截面选股 TopK 历史统计 · 纯函数引擎测试（无 IO）。

口径：信号日 T → T+lag 收盘入选 → 入选后第 hold_days 个交易日收盘退出；
每期全部权益等权投入、逐期复利；缺失价格一律剔除/顺延，绝不写 0。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from backend.services.engine.inference.selection_stats import (
    plan_cohorts,
    simulate_selection,
)


def _days(start: str, count: int) -> list[date]:
    d0 = date.fromisoformat(start)
    return [d0 + timedelta(days=i) for i in range(count)]


CAL = _days("2026-01-05", 10)  # 十个「交易日」占位（引擎不关心周末）


def _closes(series: dict[str, dict[date, float]]) -> dict[str, dict[date, float]]:
    return series


def test_single_cohort_equal_weight_return_and_month() -> None:
    calendar = CAL[:5]  # 01-05 .. 01-09
    picks = {calendar[0]: ["A", "B"]}
    closes = _closes(
        {
            "A": {calendar[1]: 10.0, calendar[3]: 11.0},
            "B": {calendar[1]: 20.0, calendar[3]: 22.0},
        }
    )

    result = simulate_selection(
        calendar=calendar,
        picks_by_signal=picks,
        closes=closes,
        hold_days=2,
        execution_lag_days=1,
        benchmark_closes={calendar[1]: 100.0, calendar[3]: 105.0},
        top_k=2,
    )

    summary = result["summary"]
    assert summary["cohorts"] == 1
    assert summary["closed_cohorts"] == 1
    assert summary["open_cohorts"] == 0
    assert summary["total_return_pct"] == pytest.approx(10.0)
    assert summary["benchmark_return_pct"] == pytest.approx(5.0)
    assert summary["excess_return_pct"] == pytest.approx(5.0)
    assert summary["cohort_win_rate_pct"] == pytest.approx(100.0)
    assert summary["pick_win_rate_pct"] == pytest.approx(100.0)
    assert summary["price_coverage_pct"] == pytest.approx(100.0)

    cohort = result["cohorts"][0]
    assert cohort["signal_date"] == calendar[0].isoformat()
    assert cohort["entry_date"] == calendar[1].isoformat()
    assert cohort["exit_date"] == calendar[3].isoformat()
    assert cohort["exit_kind"] == "closed"
    assert cohort["held"] == 2
    assert cohort["priced"] == 2
    assert cohort["return_pct"] == pytest.approx(10.0)
    assert cohort["equity"] == pytest.approx(1.1)
    assert [p["equity"] for p in result["points"]] == pytest.approx([1.0, 1.1])

    month = result["months"][0]
    assert month["month"] == "2026-01"
    assert month["return_pct"] == pytest.approx(10.0)
    assert month["profit_amount"] == pytest.approx(0.1)
    assert month["cohorts"] == 1
    assert month["benchmark_return_pct"] == pytest.approx(5.0)
    assert month["excess_return_pct"] == pytest.approx(5.0)


def test_period_mode_cohorts_never_overlap_and_chain_compounds() -> None:
    calendar = CAL[:6]
    picks = {
        calendar[0]: ["A"],
        calendar[1]: ["A"],
        calendar[2]: ["A"],
        calendar[3]: ["A"],
        calendar[4]: ["A"],
        calendar[5]: ["A"],
    }
    closes = _closes(
        {
            "A": {
                calendar[1]: 10.0,
                calendar[2]: 9.0,
                calendar[3]: 12.0,
                calendar[4]: 13.0,
                calendar[5]: 11.7,
            }
        }
    )

    result = simulate_selection(
        calendar=calendar,
        picks_by_signal=picks,
        closes=closes,
        hold_days=2,
        execution_lag_days=1,
        top_k=1,
    )

    rows = result["cohorts"]
    # 期 1: 入选 01-06 → 退出 01-08；期 2: 入选 01-08 → 退出 01-10(索引 5)
    assert [r["signal_date"] for r in rows] == [
        calendar[0].isoformat(),
        calendar[2].isoformat(),
    ]
    assert rows[0]["exit_date"] == rows[1]["entry_date"] == calendar[3].isoformat()
    # 期 1: 12/10-1 = +20%; 期 2: 11.7/12-1 = -2.5%
    assert rows[0]["return_pct"] == pytest.approx(20.0)
    assert rows[1]["return_pct"] == pytest.approx(-2.5)
    assert rows[1]["equity"] == pytest.approx(1.2 * 0.975)
    assert result["summary"]["total_return_pct"] == pytest.approx(17.0)


def test_daily_rolling_mode_hands_off_at_same_close() -> None:
    calendar = CAL[:4]
    picks = {calendar[0]: ["A"], calendar[1]: ["A"], calendar[2]: ["A"]}
    closes = _closes(
        {
            "A": {
                calendar[1]: 100.0,
                calendar[2]: 110.0,
                calendar[3]: 99.0,
            }
        }
    )

    result = simulate_selection(
        calendar=calendar,
        picks_by_signal=picks,
        closes=closes,
        hold_days=1,
        execution_lag_days=1,
        top_k=1,
    )

    assert [r["return_pct"] for r in result["cohorts"]] == pytest.approx([10.0, -10.0])
    assert result["summary"]["total_return_pct"] == pytest.approx(-1.0)
    # 每日滚动：入选日 = 上一期退出日
    assert result["cohorts"][1]["entry_date"] == result["cohorts"][0]["exit_date"]


def test_missing_entry_price_drops_name_and_marks_partial() -> None:
    calendar = CAL[:5]
    picks = {calendar[0]: ["A", "B"]}
    closes = _closes(
        {
            "A": {},  # 入选日无价
            "B": {calendar[1]: 10.0, calendar[3]: 12.0},
        }
    )

    result = simulate_selection(
        calendar=calendar,
        picks_by_signal=picks,
        closes=closes,
        hold_days=2,
        execution_lag_days=1,
        top_k=2,
    )

    row = result["cohorts"][0]
    assert row["held"] == 2
    assert row["priced"] == 1
    assert row["missing_entry"] == 1
    assert row["partial"] is True
    assert row["return_pct"] == pytest.approx(20.0)  # 只剩 B，等权归一
    assert "missing_entry_price" in result["warnings"]
    assert result["summary"]["priced_picks"] == 1
    assert result["summary"]["price_coverage_pct"] == pytest.approx(50.0)


def test_missing_exit_uses_last_close_inside_window_then_drops() -> None:
    calendar = CAL[:6]
    picks = {calendar[0]: ["A", "B"]}
    closes = _closes(
        {
            # A 退出日(索引 4)缺价，窗口内 01-08(索引 3) 有价 → 沿用
            "A": {calendar[1]: 10.0, calendar[3]: 11.0},
            # B 入选后再无价 → 剔除
            "B": {calendar[1]: 10.0},
        }
    )

    result = simulate_selection(
        calendar=calendar,
        picks_by_signal=picks,
        closes=closes,
        hold_days=3,
        execution_lag_days=1,
        top_k=2,
    )

    row = result["cohorts"][0]
    assert row["exit_date"] == calendar[3].isoformat()
    assert row["stale_exit"] == 1
    assert row["missing_exit"] == 1
    assert row["priced"] == 1
    assert row["return_pct"] == pytest.approx(10.0)
    assert "stale_exit" in result["warnings"]
    assert "missing_exit_price" in result["warnings"]


def test_zero_or_nan_price_is_missing_never_zero_return() -> None:
    calendar = CAL[:5]
    picks = {calendar[0]: ["A", "B"]}
    closes = _closes(
        {
            "A": {calendar[1]: 0.0, calendar[3]: 12.0},  # 0 价不得当成交价
            "B": {calendar[1]: float("nan"), calendar[3]: 12.0},
        }
    )

    result = simulate_selection(
        calendar=calendar,
        picks_by_signal=picks,
        closes=closes,
        hold_days=2,
        execution_lag_days=1,
        top_k=2,
    )

    row = result["cohorts"][0]
    assert row["priced"] == 0
    assert row["return_pct"] is None
    assert row["skipped"] is True
    assert result["summary"]["total_return_pct"] is None
    assert "skipped_cohorts" in result["warnings"]


def test_open_cohort_marks_at_last_calendar_day() -> None:
    calendar = CAL[:3]
    picks = {calendar[0]: ["A"]}
    closes = _closes({"A": {calendar[1]: 10.0, calendar[2]: 12.0}})

    result = simulate_selection(
        calendar=calendar,
        picks_by_signal=picks,
        closes=closes,
        hold_days=5,
        execution_lag_days=1,
        top_k=1,
    )

    row = result["cohorts"][0]
    assert row["exit_kind"] == "open"
    assert row["exit_date"] is None
    assert row["mark_date"] == calendar[2].isoformat()
    assert row["return_pct"] == pytest.approx(20.0)
    assert result["summary"]["open_cohorts"] == 1
    assert result["summary"]["total_return_pct"] == pytest.approx(20.0)
    assert "open_cohort" in result["warnings"]


def test_missing_benchmark_is_null_not_zero() -> None:
    calendar = CAL[:5]
    picks = {calendar[0]: ["A"]}
    closes = _closes({"A": {calendar[1]: 10.0, calendar[3]: 11.0}})

    result = simulate_selection(
        calendar=calendar,
        picks_by_signal=picks,
        closes=closes,
        hold_days=2,
        execution_lag_days=1,
        benchmark_closes={calendar[1]: 100.0},  # 退出日缺
        top_k=1,
    )

    assert result["cohorts"][0]["benchmark_return_pct"] is None
    assert result["summary"]["benchmark_return_pct"] is None
    assert result["summary"]["excess_return_pct"] is None
    assert "benchmark_unavailable" in result["warnings"]


def test_max_drawdown_uses_cohort_equity_path() -> None:
    calendar = CAL[:10]
    picks = {calendar[0]: ["A"], calendar[2]: ["A"], calendar[4]: ["A"]}
    # 期1 +10%（01-06→01-08），期2 -20%（01-08→01-10），期3 +5%
    closes = _closes(
        {
            "A": {
                calendar[1]: 100.0,
                calendar[3]: 110.0,
                calendar[5]: 88.0,
                calendar[7]: 92.4,
            }
        }
    )

    result = simulate_selection(
        calendar=calendar,
        picks_by_signal=picks,
        closes=closes,
        hold_days=2,
        execution_lag_days=1,
        top_k=1,
    )

    assert [r["equity"] for r in result["cohorts"]] == pytest.approx([1.1, 0.88, 0.924])
    assert result["summary"]["max_drawdown_pct"] == pytest.approx(-20.0)


def test_months_break_down_chained_equity_and_benchmark() -> None:
    calendar = _days("2026-01-27", 6)  # 跨月：01-27..02-01
    picks = {calendar[0]: ["A"], calendar[2]: ["A"]}
    closes = _closes(
        {
            "A": {
                calendar[1]: 100.0,
                calendar[3]: 110.0,  # 期1 +10%
                calendar[5]: 99.0,  # 期2 -10%
            }
        }
    )

    result = simulate_selection(
        calendar=calendar,
        picks_by_signal=picks,
        closes=closes,
        hold_days=2,
        execution_lag_days=1,
        benchmark_closes={
            calendar[1]: 100.0,
            calendar[3]: 102.0,
            calendar[5]: 102.0,
        },
        top_k=1,
    )

    months = {m["month"]: m for m in result["months"]}
    assert months["2026-01"]["return_pct"] == pytest.approx(10.0)
    assert months["2026-01"]["partial"] is True
    assert months["2026-02"]["start_equity"] == pytest.approx(1.1)
    assert months["2026-02"]["end_equity"] == pytest.approx(0.99)
    assert months["2026-02"]["return_pct"] == pytest.approx(-10.0)
    assert months["2026-02"]["excess_return_pct"] == pytest.approx(-10.0)


def test_unsorted_signals_are_sorted_and_top_k_truncates() -> None:
    calendar = CAL[:5]
    picks = {calendar[2]: ["X", "A", "B"], calendar[0]: ["A", "B", "C"]}
    closes = _closes(
        {
            "A": {calendar[1]: 10.0, calendar[3]: 11.0},
            "B": {calendar[1]: 10.0, calendar[3]: 11.0},
            "C": {calendar[1]: 10.0, calendar[3]: 11.0},
        }
    )

    result = simulate_selection(
        calendar=calendar,
        picks_by_signal=picks,
        closes=closes,
        hold_days=2,
        execution_lag_days=1,
        top_k=2,
    )

    assert result["cohorts"][0]["signal_date"] == calendar[0].isoformat()
    assert result["cohorts"][0]["held"] == 2  # C 被 top_k 截掉
    assert result["cohorts"][0]["priced"] == 2


def test_empty_picks_returns_none_not_zero() -> None:
    result = simulate_selection(
        calendar=CAL[:3],
        picks_by_signal={},
        closes={},
        hold_days=1,
        execution_lag_days=1,
        top_k=50,
    )

    assert result["cohorts"] == []
    assert result["summary"]["total_return_pct"] is None
    assert result["summary"]["max_drawdown_pct"] is None
    assert result["summary"]["cohorts"] == 0


def test_plan_requires_minimum_holding_time() -> None:
    calendar = CAL[:4]
    # 入选日落在最后一天 → 没有可持有的时间，不成期
    plan = plan_cohorts(
        calendar,
        [calendar[2]],
        hold_days=1,
        execution_lag_days=1,
    )
    assert plan == []
