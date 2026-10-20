"""rolling_portfolio_sim 的确定性单元测试（合成数据，不碰数据库）。

覆盖三个最容易写错、且一旦写错就会让回测虚高的点：

1. 时间退出的成交日与净收益（收盘决策、次日成交、持有 H 日）；
2. blocked_long 涨停买不进时的顺延；
3. 分数跌破均值缓冲时的提前卖出。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import rolling_portfolio_sim as R  # noqa: E402


def _synthetic() -> tuple[
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    np.ndarray,
    np.ndarray,
    pd.Series,
]:
    dates = np.arange(20250101, 20250111, dtype="int64")
    symbols = ["A.SH", "B.SZ"]
    close = pd.DataFrame(
        {
            "A.SH": [10, 10, 11, 12, 12, 12, 12, 12, 12, 12],
            "B.SZ": [10] * 10,
        },
        index=dates,
        dtype=float,
    )
    adv = pd.DataFrame(100_000.0, index=dates, columns=symbols)
    score = pd.DataFrame({"A.SH": 0.9, "B.SZ": 0.1}, index=dates, dtype=float)
    avg = pd.DataFrame({"A.SH": 0.5, "B.SZ": 0.5}, index=dates, dtype=float)
    blocked_long = np.zeros((len(dates), 2), dtype=bool)
    blocked_short = np.zeros((len(dates), 2), dtype=bool)
    market20 = pd.Series(-0.20, index=dates)
    return close, adv, score, avg, blocked_long, blocked_short, market20


def test_time_exit_buys_next_close_and_holds_exactly_two_days() -> None:
    # Arrange
    close, adv, score, avg, blocked_long, blocked_short, market20 = _synthetic()
    cfg = R.Config(
        label="time-exit",
        top_n=1,
        hold=2,
        score_exit=False,
        capital=1_000_000.0,
        cash_buffer=0.0,
        cost_side=0.001,
    )

    # Act
    res = R.simulate(
        cfg,
        close,
        adv,
        score,
        avg,
        blocked_long,
        blocked_short,
        start_i=1,
        end_i=6,
        market20=market20,
    )

    # Assert
    trades = res.trades
    assert len(trades) == 2, trades
    first = trades.iloc[0]
    assert first["entry_d"] == 20250102
    assert first["exit_d"] == 20250104
    assert abs(first["gross_ret"] - 0.20) < 1e-12
    assert abs(first["net_ret"] - (1.20 * 0.999 / 1.001 - 1.0)) < 1e-9
    second = trades.iloc[1]
    assert second["entry_d"] == 20250105
    assert second["exit_d"] == 20250107


def test_blocked_long_delays_entry_to_next_tradable_close() -> None:
    # Arrange
    close, adv, score, avg, blocked_long, blocked_short, market20 = _synthetic()
    blocked_long = blocked_long.copy()
    blocked_long[1, 0] = True  # 2025-01-02 涨停买不进
    cfg = R.Config(
        label="blocked-long",
        top_n=1,
        hold=2,
        score_exit=False,
        capital=1_000_000.0,
        cash_buffer=0.0,
        cost_side=0.001,
    )

    # Act
    res = R.simulate(
        cfg,
        close,
        adv,
        score,
        avg,
        blocked_long,
        blocked_short,
        start_i=1,
        end_i=6,
        market20=market20,
    )

    # Assert
    assert res.trades.iloc[0]["entry_d"] == 20250103


def test_score_exit_sells_when_score_falls_below_average_buffer() -> None:
    # Arrange
    close, adv, score, avg, blocked_long, blocked_short, market20 = _synthetic()
    avg = avg.copy()
    avg.loc[20250102:, "A.SH"] = 1.0  # S=0.9 < A-0.03=0.97
    cfg = R.Config(
        label="score-exit",
        top_n=1,
        hold=5,
        score_exit=True,
        score_buffer=0.03,
        capital=1_000_000.0,
        cash_buffer=0.0,
        cost_side=0.0,
    )

    # Act
    res = R.simulate(
        cfg,
        close,
        adv,
        score,
        avg,
        blocked_long,
        blocked_short,
        start_i=1,
        end_i=6,
        market20=market20,
    )

    # Assert
    first = res.trades.iloc[0]
    assert first["exit_reason"] == "score"
    assert first["exit_d"] == 20250103


def test_rank_exit_sells_when_score_drops_out_of_top_k() -> None:
    # Arrange
    close, adv, score, avg, blocked_long, blocked_short, market20 = _synthetic()
    score = score.copy()
    score.loc[20250103:, "A.SH"] = 0.2
    score.loc[20250103:, "B.SZ"] = 0.9
    cfg = R.Config(
        label="rank-exit",
        top_n=1,
        hold=5,
        score_exit=False,
        exit_rank=1,
        capital=1_000_000.0,
        cash_buffer=0.0,
        cost_side=0.0,
    )

    # Act
    res = R.simulate(
        cfg,
        close,
        adv,
        score,
        avg,
        blocked_long,
        blocked_short,
        start_i=1,
        end_i=6,
        market20=market20,
    )

    # Assert
    first = res.trades.iloc[0]
    assert first["exit_reason"] == "rank"
    assert first["exit_d"] == 20250104


def test_entry_confirm_two_delays_entry_by_one_day() -> None:
    # Arrange
    close, adv, score, avg, blocked_long, blocked_short, market20 = _synthetic()
    cfg = R.Config(
        label="entry-confirm",
        top_n=1,
        hold=2,
        score_exit=False,
        entry_confirm=2,
        capital=1_000_000.0,
        cash_buffer=0.0,
        cost_side=0.0,
    )

    # Act
    res = R.simulate(
        cfg,
        close,
        adv,
        score,
        avg,
        blocked_long,
        blocked_short,
        start_i=1,
        end_i=6,
        market20=market20,
    )

    # Assert
    assert res.trades.iloc[0]["entry_d"] == 20250103


def test_exit_confirm_two_waits_for_second_weak_day() -> None:
    # Arrange
    close, adv, score, avg, blocked_long, blocked_short, market20 = _synthetic()
    avg = avg.copy()
    avg.loc[20250102:, "A.SH"] = 1.0
    cfg = R.Config(
        label="exit-confirm",
        top_n=1,
        hold=5,
        score_exit=True,
        score_buffer=0.03,
        exit_confirm=2,
        capital=1_000_000.0,
        cash_buffer=0.0,
        cost_side=0.0,
    )

    # Act
    res = R.simulate(
        cfg,
        close,
        adv,
        score,
        avg,
        blocked_long,
        blocked_short,
        start_i=1,
        end_i=6,
        market20=market20,
    )

    # Assert
    first = res.trades.iloc[0]
    assert first["exit_reason"] == "score"
    assert first["exit_d"] == 20250104


def test_industry_cap_skips_extra_name_in_same_industry() -> None:
    # Arrange
    dates = np.arange(20250101, 20250111, dtype="int64")
    symbols = ["A.SH", "B.SZ", "C.SH"]
    close = pd.DataFrame(10.0, index=dates, columns=symbols)
    adv = pd.DataFrame(100_000.0, index=dates, columns=symbols)
    score = pd.DataFrame(
        {"A.SH": 0.9, "B.SZ": 0.8, "C.SH": 0.7}, index=dates, dtype=float
    )
    avg = pd.DataFrame(0.5, index=dates, columns=symbols)
    blocked_long = np.zeros((len(dates), 3), dtype=bool)
    blocked_short = np.zeros((len(dates), 3), dtype=bool)
    market20 = pd.Series(-0.20, index=dates)
    industry = np.array([0, 0, 1])
    cfg = R.Config(
        label="industry-cap",
        top_n=2,
        hold=2,
        score_exit=False,
        max_industry_pct=0.5,
        capital=1_000_000.0,
        cash_buffer=0.0,
        cost_side=0.0,
    )

    # Act
    res = R.simulate(
        cfg,
        close,
        adv,
        score,
        avg,
        blocked_long,
        blocked_short,
        start_i=1,
        end_i=6,
        market20=market20,
        industry=industry,
    )

    # Assert：A 先买入；B 与 A 同行业被跳过，第二个仓位买 C（而不是 B）。
    assert res.trades.loc[res.trades["symbol"] == "A.SH", "entry_i"].iloc[0] == 1
    assert res.trades.loc[res.trades["symbol"] == "C.SH", "entry_i"].iloc[0] == 2
    a_exit = res.trades.loc[res.trades["symbol"] == "A.SH", "exit_i"].iloc[0]
    b_entry = res.trades.loc[res.trades["symbol"] == "B.SZ", "entry_i"].iloc[0]
    assert b_entry >= a_exit


def test_industry_cap_requires_industry_array() -> None:
    # Arrange
    close, adv, score, avg, blocked_long, blocked_short, market20 = _synthetic()
    cfg = R.Config(label="industry-cap", top_n=1, hold=2, max_industry_pct=0.5)

    # Act / Assert
    with pytest.raises(ValueError, match="industry"):
        R.simulate(
            cfg,
            close,
            adv,
            score,
            avg,
            blocked_long,
            blocked_short,
            start_i=1,
            end_i=6,
            market20=market20,
        )


def test_open_fill_uses_next_open_for_entry_and_exit() -> None:
    # Arrange
    close, adv, score, avg, blocked_long, blocked_short, market20 = _synthetic()
    open_px = close.copy()
    open_px["A.SH"] = [10, 10.5, 11.5, 12.5, 12, 12, 12, 12, 12, 12]
    cfg = R.Config(
        label="open-fill",
        top_n=1,
        hold=2,
        score_exit=False,
        fill="open",
        capital=1_000_000.0,
        cash_buffer=0.0,
        cost_side=0.0,
    )

    # Act
    res = R.simulate(
        cfg,
        close,
        adv,
        score,
        avg,
        blocked_long,
        blocked_short,
        start_i=1,
        end_i=6,
        market20=market20,
        open_px=open_px,
    )

    # Assert
    first = res.trades.iloc[0]
    assert first["entry_price"] == 10.5
    assert first["exit_price"] == 12.5
