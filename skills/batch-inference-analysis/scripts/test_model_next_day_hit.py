"""model_next_day_hit 的纯函数测试（不碰数据库）。"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import model_next_day_hit as H  # noqa: E402


def _synthetic_long() -> pd.DataFrame:
    rows = [
        # d, symbol, pct, ret_sig, ret_exec, blocked_next
        (20250102, "A.SH", 0.9, 0.10, 0.05, False),
        (20250102, "B.SZ", 0.5, -0.02, -0.01, False),
        (20250102, "C.SZ", 0.1, 0.03, 0.02, True),
        (20250103, "A.SH", 0.9, -0.01, 0.01, False),
        (20250103, "B.SZ", 0.5, 0.02, -0.02, True),
        (20250103, "C.SZ", 0.1, 0.03, 0.03, False),
    ]
    return pd.DataFrame(
        rows,
        columns=["d", "symbol", "pct", "ret_sig", "ret_exec", "blocked_next"],
    )


def test_daily_topk_stats_separates_signal_and_executable_universes() -> None:
    # Arrange
    long = _synthetic_long()

    # Act
    daily = H.daily_topk_stats(long, top_ks=(1,))

    # Assert：信号口径包含涨停票；可执行口径剔除 t+1 买不进的票。
    day1 = daily.loc[20250102]
    assert day1["sig_base_hit"] == pytest.approx(2 / 3)
    assert day1["sig_base_ret"] == pytest.approx((0.10 - 0.02 + 0.03) / 3)
    assert day1["sig_top1_hit"] == 1.0
    assert day1["sig_top1_ret"] == pytest.approx(0.10)
    assert day1["exec_base_hit"] == pytest.approx(0.5)
    assert day1["exec_base_ret"] == pytest.approx((0.05 - 0.01) / 2)
    assert day1["exec_top1_hit"] == 1.0
    assert day1["exec_top1_ret"] == pytest.approx(0.05)

    day2 = daily.loc[20250103]
    assert day2["sig_top1_hit"] == 0.0
    assert day2["exec_base_hit"] == 1.0
    assert day2["exec_top1_hit"] == 1.0


def test_summarize_daily_reports_daily_and_pooled_lift() -> None:
    # Arrange
    daily = H.daily_topk_stats(_synthetic_long(), top_ks=(1,))

    # Act
    row = H.summarize_daily(daily, 1, 20250101, 20250131)

    # Assert：日频等权，两天各占一半。
    assert row["sig_days"] == 2
    assert row["sig_top_hit"] == pytest.approx(0.5)
    assert row["sig_base_hit"] == pytest.approx(2 / 3)
    assert row["sig_lift"] == pytest.approx(-1 / 6)
    assert row["sig_lift_t"] == pytest.approx(-1 / 3)
    assert row["sig_top_ret"] == pytest.approx(0.045)
    assert row["sig_base_ret"] == pytest.approx(0.025)
    assert row["sig_excess"] == pytest.approx(0.02)
    assert row["sig_top_hit_pooled"] == pytest.approx(0.5)
    assert row["sig_base_hit_pooled"] == pytest.approx(2 / 3)

    assert row["exec_top_hit"] == pytest.approx(1.0)
    assert row["exec_base_hit"] == pytest.approx(0.75)
    assert row["exec_lift"] == pytest.approx(0.25)
    assert row["exec_lift_t"] == pytest.approx(1.0)
    assert row["exec_excess"] == pytest.approx(0.01)


def test_summarize_daily_filters_window() -> None:
    # Arrange
    daily = H.daily_topk_stats(_synthetic_long(), top_ks=(1,))

    # Act：只留第二天（信号 Top1 当日下跌）。
    row = H.summarize_daily(daily, 1, 20250103, 20250131)

    # Assert
    assert row["sig_days"] == 1
    assert row["sig_top_hit"] == 0.0
    assert row["sig_lift"] == pytest.approx(-2 / 3)
    assert row["sig_n_months"] == 1
    assert math.isnan(row["sig_month_t"])


def test_monthly_mean_groups_by_year_month() -> None:
    # Arrange
    values = pd.Series([1.0, 3.0, 5.0], index=[20250102, 20250103, 20250204])

    # Act
    monthly = H.monthly_mean(values)

    # Assert
    assert monthly.to_dict() == {202501: 2.0, 202502: 5.0}


def test_tstat_handles_small_and_constant_samples() -> None:
    # Arrange / Act / Assert
    assert math.isnan(H.tstat(pd.Series([1.0])))
    assert math.isnan(H.tstat(pd.Series([1.0, 1.0])))
    assert H.tstat(pd.Series([1.0, 2.0, 3.0])) == pytest.approx(2.0 * math.sqrt(3.0))


def test_bh_qvalues_matches_hand_computed_example() -> None:
    # Arrange
    p = pd.Series([0.01, 0.04, 0.03, 0.002], index=list("abcd"))

    # Act
    q = H.bh_qvalues(p)

    # Assert
    assert q["d"] == pytest.approx(0.008)
    assert q["a"] == pytest.approx(0.02)
    assert q["c"] == pytest.approx(0.04)
    assert q["b"] == pytest.approx(0.04)


def test_bh_qvalues_preserves_nan() -> None:
    # Arrange
    p = pd.Series([0.01, np.nan, 0.02], index=list("abc"))

    # Act
    q = H.bh_qvalues(p)

    # Assert
    assert math.isnan(q["b"])
    assert q["a"] == pytest.approx(0.02)
    assert q["c"] == pytest.approx(0.02)


def test_load_returns_aligns_next_day_and_first_holding_day() -> None:
    # Arrange
    dates = [20250101, 20250102, 20250103, 20250104]
    close = pd.DataFrame(
        {
            "A.SH": [10.0, 11.0, 12.0, 13.0],
            "B.SZ": [10.0, 9.0, 9.9, 10.89],
        },
        index=dates,
    )
    blocked_long = np.array(
        [
            [False, False],
            [False, True],  # 2025-01-02 B 买不进
            [False, False],
            [False, False],
        ]
    )

    # Act
    long = H.load_returns(close, blocked_long)
    row_a = long.loc[(long["d"] == 20250101) & (long["symbol"] == "A.SH")].iloc[0]
    row_b = long.loc[(long["d"] == 20250101) & (long["symbol"] == "B.SZ")].iloc[0]
    row_a2 = long.loc[(long["d"] == 20250102) & (long["symbol"] == "A.SH")].iloc[0]

    # Assert
    assert row_a["ret_sig"] == pytest.approx(0.10)
    assert row_a["ret_exec"] == pytest.approx(12.0 / 11.0 - 1.0)
    assert not bool(row_a["blocked_next"])
    assert row_b["ret_sig"] == pytest.approx(-0.10)
    assert bool(row_b["blocked_next"])
    assert row_a2["ret_exec"] == pytest.approx(13.0 / 12.0 - 1.0)
