"""严格统计工具的单元测试。"""

from __future__ import annotations

import numpy as np
import pytest

from backend.scripts.backtest2026.strict_stats import (
    bh_qvalues,
    block_bootstrap_mean,
    deflated_sharpe_ratio,
    friedman_kendall,
    newey_west_mean,
    pairwise_wilcoxon_bh,
    pbo_cscv,
    sharpe_ratio,
    spearman,
    white_reality_check,
)


def test_newey_west_lag_zero_matches_population_t():
    values = np.array([0.02, -0.01, 0.03, 0.00, -0.02, 0.01])
    result = newey_west_mean(values, lag=0)
    expected_se = values.std(ddof=0) / np.sqrt(len(values))
    assert result["mean"] == pytest.approx(values.mean())
    assert result["se"] == pytest.approx(expected_se)
    assert result["t"] == pytest.approx(values.mean() / expected_se)


def test_newey_west_with_overlap_returns_finite_t():
    rng = np.random.default_rng(11)
    values = rng.normal(0.01, 0.05, 200)
    result = newey_west_mean(values, lag=9)
    assert np.isfinite(result["t"])
    assert result["n"] == 200


def test_newey_west_empty_returns_nan():
    result = newey_west_mean([], lag=5)
    assert np.isnan(result["t"])
    assert result["n"] == 0


def test_bh_qvalues_matches_hand_calculation():
    pvalues = [0.01, 0.04, 0.03, 0.002]
    expected = [0.02, 0.04, 0.04, 0.008]
    assert bh_qvalues(pvalues) == pytest.approx(expected)


def test_bh_qvalues_preserves_nan():
    result = bh_qvalues([0.01, np.nan, 1.0])
    assert np.isnan(result[1])
    assert result[0] <= 1.0
    assert result[2] == pytest.approx(1.0)


def test_block_bootstrap_is_deterministic_and_contains_mean():
    rng = np.random.default_rng(7)
    values = rng.normal(0.03, 0.1, 157)
    first = block_bootstrap_mean(values, block=20, n_boot=800, seed=99)
    second = block_bootstrap_mean(values, block=20, n_boot=800, seed=99)
    assert first["ci_low"] == pytest.approx(second["ci_low"])
    assert first["ci_high"] == pytest.approx(second["ci_high"])
    assert first["ci_low"] < first["mean"] < first["ci_high"]
    assert 0.0 <= first["p_positive"] <= 1.0


def test_block_bootstrap_all_zero_has_p_positive_one():
    result = block_bootstrap_mean(np.zeros(50), block=5, n_boot=100, seed=1)
    assert result["p_positive"] == pytest.approx(1.0)


def test_spearman_perfect_rank_correlation():
    left = np.arange(20, dtype=float)
    assert spearman(left, left**3) == pytest.approx(1.0)
    assert spearman(left, -left) == pytest.approx(-1.0)


def test_sharpe_ratio_positive_for_positive_drift():
    rng = np.random.default_rng(3)
    value = sharpe_ratio(rng.normal(0.01, 0.01, 252))
    assert value > 0
    assert np.isnan(sharpe_ratio([0.01, 0.01, 0.01]))


def test_friedman_detects_model_differences():
    pytest.importorskip("scipy")
    matrix = np.column_stack(
        [np.arange(12.0), np.arange(12.0) * 2.0, np.arange(12.0) * 3.0]
    )
    result = friedman_kendall(matrix)
    assert result["p"] < 0.01
    assert result["kendall_w"] > 0.5
    assert result["n_models"] == 3


def test_friedman_identical_columns_not_significant():
    pytest.importorskip("scipy")
    matrix = np.column_stack([np.arange(12.0)] * 3)
    result = friedman_kendall(matrix)
    assert result["p"] == pytest.approx(1.0)
    assert result["kendall_w"] == pytest.approx(0.0)


def test_deflated_sharpe_separates_positive_and_negative_drift():
    pytest.importorskip("scipy")
    noise = np.tile([-0.01, 0.01], 126)
    positive = 0.0012 + noise
    negative = -0.0012 + noise
    good = deflated_sharpe_ratio(positive, n_trials=26, trial_sr_std=0.001)
    bad = deflated_sharpe_ratio(negative, n_trials=26, trial_sr_std=0.001)
    assert good["dsr"] > 0.8
    assert bad["dsr"] < 0.2
    assert good["sr0"] > 0


def test_white_reality_check_detects_best_model():
    pytest.importorskip("scipy")
    rng = np.random.default_rng(5)
    matrix = rng.normal(0.0, 0.01, size=(160, 4))
    matrix[:, 0] += 0.004
    result = white_reality_check(matrix, block=10, n_boot=1000, seed=17)
    assert result["best_index"] == 0
    assert result["p"] < 0.05


def test_white_reality_check_zero_matrix_not_significant():
    pytest.importorskip("scipy")
    result = white_reality_check(np.zeros((100, 4)), block=10, n_boot=200, seed=1)
    assert result["p"] == pytest.approx(1.0)


def test_pbo_prefers_consistently_best_model():
    pytest.importorskip("scipy")
    rng = np.random.default_rng(9)
    matrix = rng.normal(0.0, 0.01, size=(160, 4))
    matrix[:, 0] += 0.003
    result = pbo_cscv(matrix, n_blocks=10)
    assert 0.0 <= result["pbo"] <= 0.5
    assert result["n_combos"] > 0


def test_pbo_requires_enough_models():
    pytest.importorskip("scipy")
    result = pbo_cscv(np.zeros((100, 1)))
    assert np.isnan(result["pbo"])


def test_pairwise_wilcoxon_flags_clear_difference():
    pytest.importorskip("scipy")
    rng = np.random.default_rng(13)
    base = rng.normal(0.001, 0.01, 150)
    rows = pairwise_wilcoxon_bh(
        base,
        {
            "worse": base - 0.004,
            "same": base.copy(),
        },
    )
    worse = next(row for row in rows if row["model"] == "worse")
    same = next(row for row in rows if row["model"] == "same")
    assert worse["p"] < 0.01
    assert worse["q"] < 0.05
    assert same["p"] == pytest.approx(1.0)
