"""滚动建仓损益口径（收盘价买入 / 收盘价卖出）纯函数回归。

2026-10-21 用户拍板口径：

- 买入 = 入选当日**收盘价**（尾盘买入），卖出 = 退出当日收盘价；
- 一次持有一笔交易：从窗口内首次在榜，到首次不在榜；断榜后重进按新一笔计；
- 窗口首列的标的无法知道窗口外更早的入场日，按首列收盘价买入并标记
  ``entry_from_window_start=True``（展示层如实说明「按窗口起点计」）；
- 缺收盘价的交易不计入胜率/均值，``return_pct=None``（展示 `—`，绝不写 0）；
- 组合口径：每日等权持有当日 TopK，收盘买入、次日收盘换仓，
  ``cycle_return_pct = Π(1 + r_t) − 1``（未计费用）。

跑法：
    docker exec -w /app/backend quantmind python -m pytest \
        tests/test_rolling_performance.py -q --no-cov
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from backend.services.engine.inference.blend_engine import rank_pct_from_rank
from backend.services.engine.inference.rolling_performance import (
    compute_rolling_performance,
)
from backend.shared.signal_contract import compute_rank_pct

D0 = date(2026, 10, 8)
D1 = D0 + timedelta(days=1)
D2 = D0 + timedelta(days=2)
D3 = D0 + timedelta(days=3)


def _closes(**series: dict[date, float]) -> dict[str, dict[date, float]]:
    return dict(series)


@pytest.mark.unit
def test_exit_return_uses_close_to_close_and_marks_window_start_entry():
    days = [D0, D1, D2]
    picks = {D0: ["SH600000", "SZ000001"], D1: ["SH600000"], D2: ["SH600000"]}
    closes = _closes(
        **{
            "SH600000": {D0: 10.0, D1: 11.0, D2: 12.0},
            "SZ000001": {D0: 20.0, D1: 18.0, D2: 19.0},
        }
    )

    result = compute_rolling_performance(days, picks, closes)

    assert result["exits_by_day"][D0] == []
    exits = result["exits_by_day"][D1]
    assert len(exits) == 1
    trade = exits[0]
    assert trade["symbol"] == "SZ000001"
    assert trade["entry_date"] == D0
    assert trade["exit_date"] == D1
    assert trade["entry_price"] == pytest.approx(20.0)
    assert trade["exit_price"] == pytest.approx(18.0)
    assert trade["return_pct"] == pytest.approx(-10.0)
    assert trade["held_days"] == 1
    assert trade["entry_from_window_start"] is True
    assert result["exits_by_day"][D2] == []


@pytest.mark.unit
def test_streak_entry_and_exit_dates_use_actual_days():
    days = [D0, D1, D2, D3]
    picks = {
        D0: ["SH600000"],
        D1: ["SH600000", "SZ000001"],
        D2: ["SH600000", "SZ000001"],
        D3: ["SH600000"],
    }
    closes = _closes(
        **{
            "SH600000": {D0: 10.0, D1: 10.5, D2: 11.0, D3: 11.5},
            "SZ000001": {D1: 20.0, D2: 21.0, D3: 22.0},
        }
    )

    result = compute_rolling_performance(days, picks, closes)

    trade = result["exits_by_day"][D3][0]
    assert trade["symbol"] == "SZ000001"
    assert trade["entry_date"] == D1
    assert trade["exit_date"] == D3
    assert trade["held_days"] == 2
    assert trade["entry_from_window_start"] is False
    assert trade["return_pct"] == pytest.approx(10.0)


@pytest.mark.unit
def test_missing_close_yields_none_and_is_excluded_from_stats():
    days = [D0, D1]
    picks = {D0: ["SH600000", "SZ000001"], D1: ["SH600000"]}
    # SZ000001 在退出日没有收盘价 → 该笔不可定价
    closes = _closes(
        **{
            "SH600000": {D0: 10.0, D1: 11.0},
            "SZ000001": {D0: 20.0},
        }
    )

    result = compute_rolling_performance(days, picks, closes)

    trade = result["exits_by_day"][D1][0]
    assert trade["return_pct"] is None
    stats = result["stats"]
    assert stats["closed_trades"] == 1
    assert stats["priced_trades"] == 0
    assert stats["unpriced_trades"] == 1
    assert stats["win_rate_pct"] is None
    assert stats["avg_return_pct"] is None


@pytest.mark.unit
def test_reentry_after_gap_counts_as_a_second_trade():
    days = [D0, D1, D2, D3]
    picks = {
        D0: ["SH600000"],
        D1: ["SH600000"],
        D2: ["SZ000001"],
        D3: ["SH600000"],
    }
    closes = _closes(
        **{
            "SH600000": {D0: 10.0, D1: 11.0, D2: 12.0, D3: 13.0},
            "SZ000001": {D2: 20.0, D3: 21.0},
        }
    )

    result = compute_rolling_performance(days, picks, closes)

    first = result["exits_by_day"][D2][0]
    assert first["symbol"] == "SH600000"
    assert first["entry_date"] == D0
    assert first["exit_date"] == D2
    assert first["return_pct"] == pytest.approx(20.0)
    # 断榜重进：窗口结束时仍在榜 → 记为在场（新的一笔），不并入上一笔
    open_symbols = [row["symbol"] for row in result["open"]]
    assert open_symbols == ["SH600000"]
    assert result["open"][0]["entry_date"] == D3
    assert result["open"][0]["held_days"] == 1


@pytest.mark.unit
def test_open_positions_are_marked_at_last_close():
    days = [D0, D1, D2]
    picks = {D0: ["SH600000"], D1: ["SH600000"], D2: ["SH600000"]}
    closes = _closes(**{"SH600000": {D0: 10.0, D1: 10.5, D2: 12.0}})

    result = compute_rolling_performance(days, picks, closes)

    assert result["exits_by_day"][D1] == []
    assert len(result["open"]) == 1
    row = result["open"][0]
    assert row["mark_date"] == D2
    assert row["mark_price"] == pytest.approx(12.0)
    assert row["return_pct"] == pytest.approx(20.0)
    assert row["held_days"] == 3
    stats = result["stats"]
    assert stats["open_positions"] == 1
    assert stats["open_priced"] == 1
    assert stats["open_avg_return_pct"] == pytest.approx(20.0)


@pytest.mark.unit
def test_daily_cycle_return_compounds_equal_weight_picks():
    days = [D0, D1, D2]
    picks = {D0: ["SH600000"], D1: ["SZ000001"], D2: []}
    closes = _closes(
        **{
            "SH600000": {D0: 10.0, D1: 11.0},
            "SZ000001": {D1: 20.0, D2: 19.0},
        }
    )

    result = compute_rolling_performance(days, picks, closes)

    daily = result["daily"]
    assert [row["return_pct"] for row in daily] == pytest.approx([10.0, -5.0])
    # (1+10%) × (1−5%) − 1 = +4.5%
    stats = result["stats"]
    assert stats["cycle_return_pct"] == pytest.approx(4.5)
    assert stats["cycle_priced_days"] == 2
    assert stats["cycle_win_days"] == 1
    assert stats["cycle_loss_days"] == 1


@pytest.mark.unit
def test_win_loss_flat_and_average_stats():
    days = [D0, D1]
    picks = {D0: ["A", "B", "C", "D"], D1: []}
    closes = _closes(
        **{
            "A": {D0: 10.0, D1: 11.0},  # +10%
            "B": {D0: 10.0, D1: 9.0},  # −10%
            "C": {D0: 10.0, D1: 10.0},  # 0%
            "D": {D0: 10.0, D1: 10.5},  # +5%
        }
    )

    result = compute_rolling_performance(days, picks, closes)

    stats = result["stats"]
    assert (stats["wins"], stats["losses"], stats["flats"]) == (2, 1, 1)
    assert stats["win_rate_pct"] == pytest.approx(50.0)
    assert stats["avg_return_pct"] == pytest.approx(1.25)
    assert stats["median_return_pct"] == pytest.approx(2.5)
    assert stats["best_symbol"] == "A"
    assert stats["best_return_pct"] == pytest.approx(10.0)
    assert stats["worst_symbol"] == "B"
    assert stats["worst_return_pct"] == pytest.approx(-10.0)


@pytest.mark.unit
def test_empty_window_returns_null_stats_not_zeros():
    result = compute_rolling_performance([], {}, {})

    stats = result["stats"]
    assert stats["closed_trades"] == 0
    assert stats["win_rate_pct"] is None
    assert stats["avg_return_pct"] is None
    assert stats["cycle_return_pct"] is None
    assert result["exits_by_day"] == {}
    assert result["open"] == []


@pytest.mark.unit
def test_rank_pct_from_rank_matches_signal_contract():
    """SQL 侧升序名次 → 分位必须与 ``compute_rank_pct`` 同口径（含并列）。"""
    scores = [5.0, 10.0, 10.0, 20.0, 20.0]
    expected = compute_rank_pct(scores)
    # 与 compute_rank_pct 相同的升序名次（并列取最小名次）
    ascending_ranks = [1, 2, 2, 4, 4]
    got = [rank_pct_from_rank(rank, len(scores)) for rank in ascending_ranks]
    assert got == pytest.approx(expected)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("rank", "n_total", "pct"),
    [(1, 1, 0.0), (1, 10, 0.0), (10, 10, 1.0), (5, 11, 0.4)],
)
def test_rank_pct_from_rank_bounds(rank: int, n_total: int, pct: float):
    assert rank_pct_from_rank(rank, n_total) == pytest.approx(pct)


# ── 每日等权换仓批次口径（2026-10-21 机构级统计）────────────────────────


def _rotation(**kwargs):
    from backend.services.engine.inference.rolling_performance import (
        simulate_daily_rotation,
    )

    base = {"capital": 1000.0}
    base.update(kwargs)
    return simulate_daily_rotation(**base)


@pytest.mark.unit
def test_daily_rotation_splits_realized_and_open_batch_float():
    days = [D0, D1, D2]
    picks = {D0: ["A"], D1: ["B"]}
    closes = {"A": {D0: 10.0, D1: 11.0}, "B": {D1: 20.0, D2: 19.0}}

    result = _rotation(days=days, picks_by_day=picks, closes=closes)

    batches = result["batches"]
    assert len(batches) == 2
    first, second = batches
    assert first["entry_date"] == D0
    assert first["exit_date"] == D1
    assert first["positions"] == 1
    assert first["priced"] == 1
    assert first["return_pct"] == pytest.approx(10.0)
    assert first["notional_amount"] == pytest.approx(1000.0)
    assert first["pnl_amount"] == pytest.approx(100.0)
    assert first["equity"] == pytest.approx(1100.0)
    assert first["settled"] is True

    assert second["entry_date"] == D1
    assert second["exit_date"] == D2
    assert second["return_pct"] == pytest.approx(-5.0)
    assert second["pnl_amount"] == pytest.approx(-55.0)
    assert second["equity"] == pytest.approx(1045.0)
    assert second["settled"] is False

    summary = result["summary"]
    assert summary["initial_capital"] == pytest.approx(1000.0)
    assert summary["final_equity"] == pytest.approx(1045.0)
    assert summary["profit_amount"] == pytest.approx(45.0)
    assert summary["total_return_pct"] == pytest.approx(4.5)
    assert summary["cycle_return_pct"] == pytest.approx(4.5)
    assert summary["realized_pnl_amount"] == pytest.approx(100.0)
    assert summary["realized_return_pct"] == pytest.approx(10.0)
    assert summary["unrealized_pnl_amount"] == pytest.approx(-55.0)
    assert summary["unrealized_return_pct"] == pytest.approx(-5.5)
    assert summary["realized_pnl_amount"] + summary[
        "unrealized_pnl_amount"
    ] == pytest.approx(summary["final_equity"] - summary["initial_capital"])
    assert summary["batches"] == 2
    assert summary["settled_batches"] == 1
    assert summary["open_batches"] == 1
    assert summary["trades"] == 1
    assert summary["win_rate_pct"] == pytest.approx(100.0)
    assert summary["profit_factor"] is None
    assert summary["turnover_x"] == pytest.approx(2.1)
    assert summary["avg_exposure_pct"] == pytest.approx(66.67, abs=0.01)
    assert summary["trading_days"] == 2

    assert [row["equity"] for row in result["daily"]] == pytest.approx([1100.0, 1045.0])


@pytest.mark.unit
def test_daily_rotation_benchmark_and_excess():
    days = [D0, D1, D2]
    picks = {D0: ["A"], D1: ["B"]}
    closes = {"A": {D0: 10.0, D1: 11.0}, "B": {D1: 20.0, D2: 19.0}}
    benchmark = {D0: 100.0, D1: 99.0, D2: 101.0}

    result = _rotation(
        days=days, picks_by_day=picks, closes=closes, benchmark_closes=benchmark
    )

    summary = result["summary"]
    assert summary["benchmark_return_pct"] == pytest.approx(1.0)
    assert summary["benchmark_coverage_pct"] == pytest.approx(100.0)
    assert summary["excess_return_pct"] == pytest.approx(3.5)
    assert summary["information_ratio"] is not None

    daily = result["daily"]
    assert daily[0]["benchmark_return_pct"] == pytest.approx(-1.0)
    assert daily[0]["excess_return_pct"] == pytest.approx(11.0)
    assert daily[1]["benchmark_return_pct"] == pytest.approx(2.0202, abs=1e-4)
    assert daily[1]["excess_return_pct"] == pytest.approx(-7.0202, abs=1e-4)


@pytest.mark.unit
def test_daily_rotation_missing_exit_price_reduces_denominator():
    days = [D0, D1]
    picks = {D0: ["A", "B"]}
    closes = {"A": {D0: 10.0, D1: 11.0}, "B": {D0: 20.0}}

    result = _rotation(days=days, picks_by_day=picks, closes=closes)

    batch = result["batches"][0]
    assert batch["positions"] == 2
    assert batch["priced"] == 1
    assert batch["return_pct"] == pytest.approx(10.0)
    assert batch["pnl_amount"] == pytest.approx(100.0)
    assert batch["settled"] is False
    summary = result["summary"]
    assert summary["unrealized_pnl_amount"] == pytest.approx(100.0)
    assert summary["realized_pnl_amount"] == pytest.approx(0.0)
    assert summary["priced_batches"] == 1
    assert summary["unpriced_batches"] == 0
    assert summary["partial_batches"] == 1


@pytest.mark.unit
def test_daily_rotation_all_skipped_is_not_counted_as_missing_price():
    """全部标的被跳过 = 无建仓批次，不得计成「缺价」或「部分缺价」。"""
    days = [D0, D1]
    picks = {D0: ["A", "B"]}
    closes = {"A": {D0: 10.0, D1: 11.0}, "B": {D0: 20.0, D1: 21.0}}

    result = _rotation(
        days=days,
        picks_by_day=picks,
        closes=closes,
        risk_flags={"A": "ST", "B": "退市"},
    )

    batch = result["batches"][0]
    assert batch["positions"] == 2
    assert batch["bought"] == 0
    assert batch["priced"] == 0
    assert batch["return_pct"] is None
    summary = result["summary"]
    assert summary["priced_batches"] == 0
    assert summary["unpriced_batches"] == 0
    assert summary["partial_batches"] == 0
    assert summary["no_position_batches"] == 1
    assert summary["final_equity"] == pytest.approx(1000.0)
    assert summary["final_cash_pct"] == pytest.approx(100.0)


@pytest.mark.unit
def test_daily_rotation_partial_batches_compare_against_bought():
    """部分缺价的分母是实际建仓只数：有跳过但建仓标的都有价，不算部分缺价。"""
    days = [D0, D1]
    picks = {D0: ["A", "B", "C"]}
    closes = {
        "A": {D0: 10.0, D1: 11.0},
        "B": {D0: 20.0, D1: 19.0},
        "C": {D0: 30.0, D1: 33.0},
    }

    result = _rotation(
        days=days,
        picks_by_day=picks,
        closes=closes,
        risk_flags={"C": "ST"},
    )

    batch = result["batches"][0]
    assert batch["bought"] == 2
    assert batch["priced"] == 2
    summary = result["summary"]
    assert summary["partial_batches"] == 0
    assert summary["unpriced_batches"] == 0
    assert summary["no_position_batches"] == 0
    assert summary["skipped_st"] == 1


@pytest.mark.unit
def test_daily_rotation_without_picks_stays_in_cash():
    days = [D0, D1, D2]

    result = _rotation(days=days, picks_by_day={}, closes={})

    summary = result["summary"]
    assert summary["final_equity"] == pytest.approx(1000.0)
    assert summary["total_return_pct"] == pytest.approx(0.0)
    assert summary["realized_pnl_amount"] == pytest.approx(0.0)
    assert summary["unrealized_pnl_amount"] == pytest.approx(0.0)
    assert summary["batches"] == 0
    assert summary["sharpe_ratio"] is None
    assert summary["final_cash_pct"] == pytest.approx(100.0)
    assert summary["max_drawdown_pct"] == pytest.approx(0.0)


@pytest.mark.unit
def test_cell_stats_per_symbol_and_per_day_pnl():
    from backend.services.engine.inference.rolling_performance import cell_stats

    days = [D0, D1, D2]
    picks = {D0: ["A"], D1: ["A", "B"], D2: ["A", "B"]}
    closes = {
        "A": {D0: 10.0, D1: 11.0, D2: 12.0},
        "B": {D1: 20.0, D2: 18.0},
    }
    equity_by_day = {D0: 1000.0, D1: 1100.0, D2: 1095.0}

    out = cell_stats(days, picks, closes, equity_by_day=equity_by_day)

    a = out["by_symbol"]["A"]
    assert a["appearances"] == 3
    assert a["streak_days"] == 3
    assert a["first_seen"] == D0
    assert a["since_first_pct"] == pytest.approx(20.0)
    assert a["next_day_samples"] == 2
    assert a["next_day_avg_pct"] == pytest.approx(9.5455, abs=1e-4)
    assert a["next_day_win_rate_pct"] == pytest.approx(100.0)
    # 样本 < 3 不给标准差（细案 §3.5.1）
    assert a["next_day_std_pct"] is None
    assert a["latest_close"] == pytest.approx(12.0)
    assert a["change_pct"] == pytest.approx(9.0909, abs=1e-4)

    b = out["by_symbol"]["B"]
    assert b["appearances"] == 2
    assert b["streak_days"] == 2
    assert b["first_seen"] == D1
    assert b["since_first_pct"] == pytest.approx(-10.0)
    assert b["next_day_samples"] == 1
    assert b["next_day_avg_pct"] == pytest.approx(-10.0)
    assert b["next_day_std_pct"] is None
    assert b["latest_close"] == pytest.approx(18.0)
    assert b["change_pct"] == pytest.approx(-10.0)

    day0 = out["by_day"][D0]["A"]
    assert day0["change_pct"] is None
    assert day0["pnl_amount"] is None
    assert day0["weight_pct"] is None

    day1_a = out["by_day"][D1]["A"]
    assert day1_a["change_pct"] == pytest.approx(10.0)
    assert day1_a["weight_pct"] == pytest.approx(100.0)
    assert day1_a["pnl_amount"] == pytest.approx(100.0)
    assert out["by_day"][D1]["B"]["pnl_amount"] is None

    day2_a = out["by_day"][D2]["A"]
    assert day2_a["weight_pct"] == pytest.approx(50.0)
    assert day2_a["pnl_amount"] == pytest.approx(50.0)
    day2_b = out["by_day"][D2]["B"]
    assert day2_b["weight_pct"] == pytest.approx(50.0)
    assert day2_b["pnl_amount"] == pytest.approx(-55.0)


# ── 可交易性：ST/退市剔除 + ADV20 容量约束（2026-10-21）─────────────────


def _amount_days(count: int, *, amount: float, end: date = D0) -> dict[date, float]:
    return {
        date.fromordinal(end.toordinal() - offset): amount for offset in range(count)
    }


@pytest.mark.unit
def test_trailing_adv_uses_last_20_days_up_to_entry_and_requires_15():
    from backend.services.engine.inference.rolling_performance import trailing_adv

    full = {
        date.fromordinal(D0.toordinal() - offset): float(21 - offset)
        for offset in range(20)
    }
    # 入选日之后的数据不得计入（防未来函数）
    full[date.fromordinal(D0.toordinal() + 1)] = 999.0
    short = _amount_days(14, amount=500_000_000.0)
    amounts = {"A": full, "B": short}

    # 21..2 的均值 = 11.5；入选日之后的 999 不得参与
    assert trailing_adv(amounts, "A", D0) == pytest.approx(11.5)
    assert trailing_adv(amounts, "B", D0) is None
    assert trailing_adv(amounts, "C", D0) is None


@pytest.mark.unit
def test_daily_rotation_skips_st_and_keeps_cash():
    from backend.services.engine.inference.rolling_performance import (
        simulate_daily_rotation,
    )

    days = [D0, D1]
    picks = {D0: ["A", "B"]}
    closes = {"A": {D0: 10.0, D1: 11.0}, "B": {D0: 10.0, D1: 12.0}}

    result = simulate_daily_rotation(
        days=days,
        picks_by_day=picks,
        closes=closes,
        capital=1000.0,
        risk_flags={"A": "ST"},
    )

    batch = result["batches"][0]
    assert batch["positions"] == 2
    assert batch["bought"] == 1
    assert batch["priced"] == 1
    # 预算 = 1000/2 = 500；A 被剔除后不替补，500 留现金
    assert batch["notional_amount"] == pytest.approx(500.0)
    assert batch["return_pct"] == pytest.approx(10.0)
    assert batch["pnl_amount"] == pytest.approx(100.0)
    assert batch["equity"] == pytest.approx(1100.0)
    daily = result["daily"][0]
    assert daily["cash"] == pytest.approx(500.0)
    assert daily["exposure_pct"] == pytest.approx(50.0)
    plan = result["entry_plans"][D0]
    assert plan["A"]["status"] == "skip_st"
    assert plan["A"]["weight"] == 0.0
    assert plan["B"]["status"] == "bought"
    assert plan["B"]["weight"] == pytest.approx(0.5)

    summary = result["summary"]
    assert summary["st_filter_applied"] is True
    assert summary["capacity_applied"] is False
    assert summary["evaluated_picks"] == 2
    assert summary["bought_positions"] == 1
    assert summary["skipped_st"] == 1
    assert summary["final_cash_pct"] == pytest.approx(45.45, abs=0.01)


@pytest.mark.unit
def test_daily_rotation_skips_low_adv_and_missing_amount():
    from backend.services.engine.inference.rolling_performance import (
        simulate_daily_rotation,
    )

    days = [D0, D1]
    picks = {D0: ["A", "B", "C"]}
    closes = {
        "A": {D0: 10.0, D1: 11.0},
        "B": {D0: 10.0, D1: 12.0},
        "C": {D0: 10.0, D1: 13.0},
    }
    amounts = {
        # 5000 万 < 1 亿门槛
        "A": _amount_days(20, amount=50_000_000.0),
        "B": _amount_days(20, amount=500_000_000.0),
    }

    result = simulate_daily_rotation(
        days=days,
        picks_by_day=picks,
        closes=closes,
        capital=1000.0,
        amounts=amounts,
    )

    plan = result["entry_plans"][D0]
    assert plan["A"]["status"] == "skip_low_adv"
    assert plan["C"]["status"] == "skip_no_amount"
    assert plan["B"]["status"] == "bought"
    assert plan["B"]["adv"] == pytest.approx(500_000_000.0)
    assert plan["B"]["value"] == pytest.approx(1000.0 / 3.0)
    batch = result["batches"][0]
    assert batch["bought"] == 1
    assert batch["return_pct"] == pytest.approx(6.6667, abs=1e-4)
    summary = result["summary"]
    assert summary["capacity_applied"] is True
    assert summary["skipped_low_adv"] == 1
    assert summary["skipped_no_amount"] == 1


@pytest.mark.unit
def test_daily_rotation_caps_position_at_participation_of_adv():
    from backend.services.engine.inference.rolling_performance import (
        simulate_daily_rotation,
    )

    days = [D0, D1]
    picks = {D0: ["A"]}
    closes = {"A": {D0: 10.0, D1: 20.0}}
    amounts = {"A": _amount_days(20, amount=50_000.0)}

    result = simulate_daily_rotation(
        days=days,
        picks_by_day=picks,
        closes=closes,
        capital=1000.0,
        amounts=amounts,
        # 关闭 1 亿门槛，单独验证参与率上限
        min_adv_yi=0.0,
        adv_participation=0.01,
    )

    plan = result["entry_plans"][D0]["A"]
    assert plan["status"] == "bought"
    # 等权预算 1000，ADV×1% = 500 → 只能建 500
    assert plan["value"] == pytest.approx(500.0)
    batch = result["batches"][0]
    assert batch["notional_amount"] == pytest.approx(500.0)
    assert batch["return_pct"] == pytest.approx(50.0)
    assert batch["pnl_amount"] == pytest.approx(500.0)
    assert batch["equity"] == pytest.approx(1500.0)
    assert result["summary"]["final_cash_pct"] == pytest.approx(33.33, abs=0.01)


@pytest.mark.unit
def test_daily_rotation_without_amounts_falls_back_to_equal_weight():
    from backend.services.engine.inference.rolling_performance import (
        simulate_daily_rotation,
    )

    days = [D0, D1]
    picks = {D0: ["A", "B"]}
    closes = {"A": {D0: 10.0, D1: 11.0}, "B": {D0: 10.0, D1: 9.0}}

    result = simulate_daily_rotation(
        days=days, picks_by_day=picks, closes=closes, capital=1000.0
    )

    batch = result["batches"][0]
    assert batch["return_pct"] == pytest.approx(0.0)  # 等权均值 (+10% −10%) / 2
    assert batch["notional_amount"] == pytest.approx(1000.0)
    summary = result["summary"]
    assert summary["capacity_applied"] is False
    assert summary["st_filter_applied"] is False
    assert summary["bought_positions"] == 2


@pytest.mark.unit
def test_daily_rotation_skips_pick_without_entry_price():
    from backend.services.engine.inference.rolling_performance import (
        simulate_daily_rotation,
    )

    days = [D0, D1]
    picks = {D0: ["A", "B"]}
    closes = {"A": {D0: 10.0, D1: 11.0}}

    result = simulate_daily_rotation(
        days=days, picks_by_day=picks, closes=closes, capital=1000.0
    )

    plan = result["entry_plans"][D0]
    assert plan["B"]["status"] == "skip_no_price"
    assert plan["B"]["value"] == 0.0
    batch = result["batches"][0]
    assert batch["bought"] == 1
    # 不替补：A 只拿等权预算 500，其余留现金
    assert batch["notional_amount"] == pytest.approx(500.0)
    assert batch["return_pct"] == pytest.approx(5.0)
    assert result["summary"]["skipped_no_price"] == 1


@pytest.mark.unit
def test_last_day_picks_are_next_day_candidates_not_bought():
    from backend.services.engine.inference.rolling_performance import (
        simulate_daily_rotation,
    )

    days = [D0, D1]
    picks = {D0: ["A"], D1: ["B"]}
    closes = {"A": {D0: 10.0, D1: 11.0}, "B": {D1: 20.0}}

    result = simulate_daily_rotation(
        days=days, picks_by_day=picks, closes=closes, capital=1000.0
    )

    assert result["entry_plans"][D1]["B"]["status"] == "next_day"
    assert result["entry_plans"][D1]["B"]["value"] == 0.0
    summary = result["summary"]
    # 末列候选不计入批次口径
    assert summary["evaluated_picks"] == 1
    assert summary["bought_positions"] == 1


@pytest.mark.unit
def test_cell_stats_carries_buy_status_and_capacity_weights():
    from backend.services.engine.inference.rolling_performance import (
        cell_stats,
        simulate_daily_rotation,
    )

    days = [D0, D1, D2]
    picks = {D0: ["A", "B"], D1: ["A", "B"], D2: ["A", "B"]}
    closes = {
        "A": {D0: 10.0, D1: 11.0, D2: 12.0},
        "B": {D0: 20.0, D1: 22.0, D2: 22.0},
    }
    rotation = simulate_daily_rotation(
        days=days,
        picks_by_day=picks,
        closes=closes,
        capital=1000.0,
        risk_flags={"B": "ST"},
    )

    out = cell_stats(
        days,
        picks,
        closes,
        equity_by_day=rotation["equity_by_day"],
        entry_plans=rotation["entry_plans"],
    )

    day0_a = out["by_day"][D0]["A"]
    assert day0_a["buy_status"] == "bought"
    assert day0_a["entry_weight_pct"] == pytest.approx(50.0)
    assert day0_a["weight_pct"] is None
    day0_b = out["by_day"][D0]["B"]
    assert day0_b["buy_status"] == "skip_st"
    assert day0_b["entry_weight_pct"] == pytest.approx(0.0)
    assert day0_b["weight_pct"] is None

    # 当日权重取前一交易日计划：A 建仓 500/1000 = 50%
    day1_a = out["by_day"][D1]["A"]
    assert day1_a["weight_pct"] == pytest.approx(50.0)
    assert day1_a["pnl_amount"] == pytest.approx(50.0)
    day1_b = out["by_day"][D1]["B"]
    assert day1_b["weight_pct"] is None
    assert day1_b["pnl_amount"] is None

    day2_a = out["by_day"][D2]["A"]
    assert day2_a["buy_status"] == "next_day"
    assert day2_a["entry_weight_pct"] == pytest.approx(50.0)


@pytest.mark.unit
def test_cell_stats_next_day_std_needs_three_samples():
    from backend.services.engine.inference.rolling_performance import cell_stats

    days = [D0, D1, D2, D3]
    picks = {day: ["A"] for day in days}
    closes = {"A": {D0: 10.0, D1: 11.0, D2: 12.0, D3: 12.6}}

    out = cell_stats(days, picks, closes)

    a = out["by_symbol"]["A"]
    assert a["next_day_samples"] == 3
    assert a["next_day_std_pct"] == pytest.approx(2.6634, abs=1e-4)
    assert a["since_first_pct"] == pytest.approx(26.0)
