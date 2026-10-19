"""深度评估报告数据层的分段与排名口径测试。

报告把外推区间拆成「训练产物期 / 干净期 / 降级期」三段，分段边界必须与
特征覆盖率盘点一致（09-15 起跌破 300 维）；否则干净期与降级期的 IC 会
被混在一起，得出「外推后模型变强」的错误结论。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backend.scripts.backtest2026.deep_report_data import (
    DeepBundle,
    quarter_rank_correlation,
    rank_correlation_between,
    strength_weakness_labels,
)


def _daily(values: list[tuple[str, float]], *, excess: float = 0.0) -> pd.DataFrame:
    """按日期构造最小质量表；IC 与 Top20 超额一一对应。"""
    dates = pd.to_datetime([row[0] for row in values])
    ic = [row[1] for row in values]
    return pd.DataFrame(
        {
            "ic_eligible": ic,
            "top20_ret": [value + 0.01 for value in ic],
            "top20_excess": [value + excess for value in ic],
            "top20_hit": [1.0 if value > 0 else 0.0 for value in ic],
            "ls20": ic,
        },
        index=dates,
    )


def _bundle(frame: pd.DataFrame) -> DeepBundle:
    return DeepBundle(
        run_dir=Path("."),
        quality_daily={5: {"LGB": frame}, 10: {}},
    )


@pytest.mark.unit
def test_period_metrics_splits_stored_clean_and_degraded() -> None:
    frame = _daily(
        [
            ("2026-08-26", 0.10),
            ("2026-08-31", 0.20),
            ("2026-09-15", 0.60),
            ("2026-09-30", 0.80),
        ]
    )
    bundle = _bundle(frame)

    from backend.scripts.backtest2026.deep_report_data import period_metrics

    result = period_metrics(bundle, 5).set_index(["model", "period"])

    assert result.loc[("LGB", "stored"), "days"] == 1
    assert result.loc[("LGB", "stored"), "ic_mean"] == pytest.approx(0.10)
    assert result.loc[("LGB", "clean"), "days"] == 1
    assert result.loc[("LGB", "clean"), "ic_mean"] == pytest.approx(0.20)
    assert result.loc[("LGB", "degraded"), "days"] == 2
    assert result.loc[("LGB", "degraded"), "ic_mean"] == pytest.approx(0.70)
    assert result.loc[("LGB", "full"), "days"] == 4


@pytest.mark.unit
def test_period_metrics_keeps_empty_windows_missing() -> None:
    frame = _daily([("2026-09-20", 0.3)])
    bundle = _bundle(frame)

    from backend.scripts.backtest2026.deep_report_data import period_metrics

    result = period_metrics(bundle, 5).set_index(["model", "period"])

    assert ("LGB", "stored") not in result.index
    assert ("LGB", "clean") not in result.index
    assert result.loc[("LGB", "degraded"), "days"] == 1


@pytest.mark.unit
def test_strength_weakness_labels_pick_extreme_ranks() -> None:
    row = pd.Series(
        {
            "rank_ic_mean": 1.0,
            "rank_icir": 2.0,
            "rank_top20_excess": 12.0,
            "rank_sharpe_rolling": 13.0,
        }
    )

    strengths, weaknesses = strength_weakness_labels(row)

    assert strengths == ["IC 均值（第1）", "ICIR（第2）"]
    assert weaknesses == ["滚动 Top5 夏普（第13）", "Top20 超额（第12）"]


@pytest.mark.unit
def test_model_profiles_exposes_top20_excess_from_quality_daily() -> None:
    from backend.scripts.backtest2026.deep_report_data import model_profiles

    frame = _daily([("2026-08-26", 0.10), ("2026-09-01", 0.20)])
    quality = pd.DataFrame(
        [
            {
                "rank": 1,
                "model": "LGB",
                "ic_mean": 0.15,
                "icir": 1.0,
                "top20_hit": 0.6,
                "top20_ret": 0.011,
                "top_bottom_spread": 0.01,
                "decile_monotonicity": 1.0,
            }
        ]
    )
    bundle = DeepBundle(
        run_dir=Path("."), quality={5: quality}, quality_daily={5: {"LGB": frame}}
    )

    result = model_profiles(bundle, 5)

    assert result.loc[0, "top20_excess"] == pytest.approx(0.15)


@pytest.mark.unit
def test_quarter_rank_correlation_detects_rank_reversal() -> None:
    matrix = pd.DataFrame(
        {"A": [1.0, 3.0], "B": [2.0, 2.0], "C": [3.0, 1.0]},
        index=["2025Q1", "2025Q2"],
    )

    result = quarter_rank_correlation(matrix)

    assert result.loc[0, "rank_corr"] == pytest.approx(-1.0)


@pytest.mark.unit
def test_rank_correlation_between_drops_missing_pairs() -> None:
    left = pd.Series({"A": 1.0, "B": 2.0, "C": 3.0, "D": 4.0})
    right = pd.Series({"A": 2.0, "B": 4.0, "C": 5.0, "D": np.nan})

    corr = rank_correlation_between(left, right)

    assert corr == pytest.approx(1.0)
