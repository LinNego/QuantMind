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
