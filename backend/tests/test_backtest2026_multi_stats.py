"""多模型差异检验（Friedman / DSR / White RC / PBO）测试。"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from backend.scripts.backtest2026.multi_portfolio import evaluate_portfolio
from backend.scripts.backtest2026.multi_stats import (
    QUALITY_METRICS,
    StatsSettings,
    best_first_rows,
    cross_horizon_stats,
    newey_west_ic,
    portfolio_stats,
    quality_stats,
)
from backend.scripts.backtest2026.strategies import StrategyResult, StrategySpec

MODELS = ("A", "B", "C")


def _settings() -> StatsSettings:
    return StatsSettings(block=10, n_boot=300, seed=20261019, n_trials=26)


def _quality_daily(offset: float, *, drop_days: int = 0) -> pd.DataFrame:
    dates = pd.bdate_range("2026-01-05", periods=60)
    base = np.linspace(0.10, 0.16, len(dates)) + offset
    frame = pd.DataFrame(
        {
            "ic_eligible": base,
            "top5_ret": base * 0.05,
            "top10_ret": base * 0.04,
            "top20_ret": base * 0.03,
            "top5_hit": np.clip(0.55 + base, 0.0, 1.0),
            "top10_hit": np.clip(0.52 + base, 0.0, 1.0),
            "top20_hit": np.clip(0.50 + base, 0.0, 1.0),
            "top10_dir": np.clip(0.51 + base, 0.0, 1.0),
        },
        index=dates,
    )
    if drop_days:
        frame.iloc[:drop_days, frame.columns.get_loc("ic_eligible")] = np.nan
    return frame


def _quality_summary(offset: float) -> dict[str, object]:
    ic_mean = 0.13 + offset
    return {
        "days": 60,
        "ic_eligible_mean": ic_mean,
        "ic_eligible_std": 0.05,
        "icir": ic_mean / 0.05,
        "ic_positive_ratio": 0.7,
        "ic_nw_t": 3.5,
        "ic_nw_se": 0.02,
        "ic_ci_low": ic_mean - 0.02,
        "ic_ci_high": ic_mean + 0.02,
        "ic_p_positive": 0.001,
        "top5_hit_mean": 0.55 + offset,
        "top10_hit_mean": 0.52 + offset,
        "top20_hit_mean": 0.50 + offset,
        "top10_dir_mean": 0.51 + offset,
        "top10_ret_mean": 0.006 + offset,
        "top20_ret_mean": 0.005 + offset,
        "top_bottom_spread": 0.012 + offset,
        "decile_monotonicity": 0.9,
        "decile_returns": [-0.004 + index * 0.001 + offset for index in range(10)],
    }


def _evaluation(model: str, daily: np.ndarray) -> object:
    dates = pd.bdate_range("2026-01-05", periods=len(daily) + 1)
    equity = 2_000_000.0 * np.cumprod(np.concatenate([[1.0], 1.0 + daily]))
    frame = pd.DataFrame(
        {
            "equity": equity,
            "cash": equity,
            "market_value": np.zeros(len(equity)),
            "buckets": np.zeros(len(equity)),
        },
        index=dates,
    )
    spec = StrategySpec(
        mode="cohort",
        k=10,
        horizon=5,
        initial_capital=2_000_000.0,
        per_name=200_000.0,
        commission=0.0003,
    )
    result = StrategyResult(
        spec=spec,
        equity=frame,
        trades=pd.DataFrame(),
        skips=pd.DataFrame(),
        signal_days=len(dates),
        selected_total=0,
    )
    return evaluate_portfolio(
        result,
        family="top10",
        horizon=5,
        model=model,
        benchmark_returns=pd.Series(0.0, index=dates),
        common_end=dates[-1],
    )


@pytest.mark.unit
def test_quality_stats_ranks_models_and_runs_friedman() -> None:
    pytest.importorskip("scipy")
    daily = {
        "A": _quality_daily(0.03),
        "B": _quality_daily(0.01),
        "C": _quality_daily(-0.01),
    }
    summaries = {
        "A": _quality_summary(0.03),
        "B": _quality_summary(0.01),
        "C": _quality_summary(-0.01),
    }

    stats = quality_stats(daily, summaries, MODELS, settings=_settings())

    assert stats["best_ic_model"] == "A"
    assert [row["model"] for row in stats["ranking"]] == ["A", "B", "C"]
    assert stats["friedman"]["ic_eligible"]["p"] < 0.01
    assert len(stats["pairwise_ic"]) == 2
    assert stats["days"] == 60
    assert len(stats["deciles"]["A"]) == 10
    assert all(np.isfinite(row["q_bootstrap"]) for row in stats["ranking"])


@pytest.mark.unit
def test_quality_stats_drops_days_missing_any_model() -> None:
    pytest.importorskip("scipy")
    daily = {
        "A": _quality_daily(0.03),
        "B": _quality_daily(0.01),
        "C": _quality_daily(-0.01, drop_days=5),
    }
    summaries = {
        "A": _quality_summary(0.03),
        "B": _quality_summary(0.01),
        "C": _quality_summary(-0.01),
    }

    stats = quality_stats(daily, summaries, MODELS, settings=_settings())

    assert stats["days"] == 55


@pytest.mark.unit
def test_quality_stats_without_models_returns_empty_block() -> None:
    stats = quality_stats({}, {}, MODELS, settings=_settings())
    assert stats == {"models": [], "friedman": {}, "ranking": []}


@pytest.mark.unit
def test_portfolio_stats_reports_friedman_dsr_white_and_pbo() -> None:
    pytest.importorskip("scipy")
    rng = np.random.default_rng(2026)
    means = {"A": 0.0012, "B": 0.0004, "C": -0.0004}
    evaluations = {
        model: _evaluation(model, rng.normal(means[model], 0.001, 59))
        for model in MODELS
    }

    stats = portfolio_stats(evaluations, MODELS, settings=_settings())

    assert stats["best_total_return_model"] == "A"
    assert [row["model"] for row in stats["ranking"]] == ["A", "B", "C"]
    assert stats["friedman_returns"]["p"] < 0.01
    assert stats["white_reality_check"]["best_model"] == "A"
    assert stats["white_reality_check"]["p"] < 0.05
    assert np.isfinite(stats["pbo"]["pbo"])
    assert stats["ranking"][0]["dsr"] > stats["ranking"][-1]["dsr"]
    assert stats["stability"]["first_second_half_rank_corr"] == pytest.approx(1.0)
    assert stats["spread"]["best_worst_total_return"] > 0
    assert len(stats["pairwise_returns"]) == 2


@pytest.mark.unit
def test_portfolio_stats_without_models_returns_empty_block() -> None:
    stats = portfolio_stats({}, MODELS, settings=_settings())
    assert stats == {"models": [], "friedman": {}, "ranking": []}


@pytest.mark.unit
def test_cross_horizon_rank_correlation_and_paired_differences() -> None:
    quality = {
        5: {
            model: _quality_summary(offset)
            for model, offset in zip(MODELS, (0.03, 0.01, -0.01), strict=True)
        },
        10: {
            model: _quality_summary(offset + 0.01)
            for model, offset in zip(MODELS, (0.03, 0.01, -0.01), strict=True)
        },
    }
    evaluations = {
        5: {
            family: {
                model: SimpleNamespace(
                    performance={
                        "total_return": 0.05 + offset,
                        "sharpe": 0.5 + offset,
                    }
                )
                for model, offset in zip(MODELS, (0.03, 0.01, -0.01), strict=True)
            }
            for family in ("top10", "top20", "rolling")
        },
        10: {
            family: {
                model: SimpleNamespace(
                    performance={
                        "total_return": 0.07 + offset,
                        "sharpe": 0.6 + offset,
                    }
                )
                for model, offset in zip(MODELS, (0.03, 0.01, -0.01), strict=True)
            }
            for family in ("top10", "top20", "rolling")
        },
    }

    stats = cross_horizon_stats(quality, evaluations, MODELS)

    assert stats["quality_rank_corr"]["ic_mean"] == pytest.approx(1.0)
    assert stats["portfolio_rank_corr"]["top10"]["total_return"] == pytest.approx(1.0)
    assert stats["portfolio_rank_corr"]["top20"]["n_models"] == 3
    rows = {row["model"]: row for row in stats["paired_differences"]}
    assert rows["A"]["delta_ic"] == pytest.approx(0.01)
    assert rows["A"]["delta_top10_return"] == pytest.approx(0.02)


@pytest.mark.unit
def test_best_first_rows_inserts_family_column() -> None:
    frame = best_first_rows(
        {"ranking": [{"rank": 1, "model": "A", "total_return": 0.1}]},
        "top10",
    )
    assert list(frame.columns)[:3] == ["family", "rank", "model"]
    assert frame.loc[0, "family"] == "top10"


@pytest.mark.unit
def test_newey_west_ic_uses_horizon_lag() -> None:
    daily = {"A": _quality_daily(0.03)}
    result = newey_west_ic(daily, MODELS, horizon=5)
    assert set(result) == {"A"}
    assert np.isfinite(result["A"]["t"])
    assert result["A"]["n"] == 60


@pytest.mark.unit
def test_quality_metric_contract_lists_required_columns() -> None:
    assert "ic_eligible" in QUALITY_METRICS
    assert "top10_ret" in QUALITY_METRICS
    assert "top20_hit" in QUALITY_METRICS
