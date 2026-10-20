"""top100_score_probe 的确定性单元测试（合成数据，不碰数据库）。

锁住三个最容易写错、且直接影响“平均分”结论的点：

1. 综合分是“百分位等权平均”，且少于 min_models 时必须为缺失；
2. 每日 TopN 平均分只取当日分数最高的 N 只；
3. 持仓平均分按持仓区间回填，不把未持有日算进去。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import top100_score_probe as P  # noqa: E402


def test_composite_averages_percentiles_and_requires_min_models() -> None:
    # Arrange
    frames = [
        pd.DataFrame({"A": [1.0, 0.5], "B": [0.0, np.nan]}, index=[1, 2]),
        pd.DataFrame({"A": [0.0, 0.5], "B": [1.0, np.nan]}, index=[1, 2]),
    ]

    # Act
    out = P.composite_from_frames(frames, min_models=2)

    # Assert
    assert out.loc[1, "A"] == 0.5
    assert out.loc[1, "B"] == 0.5
    assert out.loc[2, "A"] == 0.5
    assert pd.isna(out.loc[2, "B"])


def test_topn_daily_averages_only_the_highest_n_scores() -> None:
    # Arrange
    dates = [20260101, 20260102]
    score = pd.DataFrame(
        {
            "A": [0.9, 0.8],
            "B": [0.8, 0.7],
            "C": [0.7, 0.6],
            "D": [0.1, 0.05],
        },
        index=dates,
    )
    avg = pd.DataFrame(0.5, index=dates, columns=score.columns)

    # Act
    out = P.topn_daily(score, avg, top_n=2)

    # Assert
    assert out["n_eligible"].tolist() == [4, 4]
    assert out["top_avg"].tolist() == pytest.approx([0.85, 0.75])
    assert out["entry_top_avg"].tolist() == pytest.approx([0.85, 0.75])


def test_holdings_score_curve_only_counts_held_days() -> None:
    # Arrange
    score = pd.DataFrame(
        {"A": [0.9, 0.8, 0.7], "B": [0.1, 0.2, 0.3]},
        index=[1, 2, 3],
    )
    trades = pd.DataFrame({"symbol": ["A"], "entry_i": [0], "exit_i": [2]})

    # Act
    curve = P.holdings_score_curve(trades, score)

    # Assert
    assert curve.tolist() == [0.9, 0.8, 0.7]
