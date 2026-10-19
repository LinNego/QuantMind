"""跨持有期对比表：只读 multi summary，不重算收益。"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from backend.scripts.backtest2026.horizon_compare import (
    delta_table,
    load_summary,
    portfolio_table,
    quality_table,
    rank_correlations,
    run,
    spearman_rank_corr,
)

MODELS = ("LGB", "XGB", "CAT")
HORIZONS = (5, 10, 20)


def _summary() -> dict:
    quality = {}
    portfolios = {}
    for horizon in HORIZONS:
        quality[str(horizon)] = {
            model: {
                "ic_eligible_mean": 0.10 + offset + horizon / 1000.0,
                "icir": 0.60 + offset,
                "ic_nw_t": 2.5 + offset,
                "top10_hit_mean": 0.50 + offset,
                "top20_ret_mean": 0.005 + offset,
                "top_bottom_spread": 0.01 + offset,
            }
            for model, offset in zip(MODELS, (0.03, 0.02, 0.01), strict=True)
        }
        portfolios[str(horizon)] = {
            family: {
                model: {
                    "performance": {
                        "total_return": 0.05 + offset + horizon / 1000.0,
                        "annual_return": 0.10 + offset,
                        "sharpe": 0.50 + offset,
                        "max_drawdown": -0.10,
                        "n_round_trips": 100,
                    },
                    "stats": {
                        "active_return": 0.06 + offset,
                        "information_ratio": 0.80 + offset,
                    },
                }
                for model, offset in zip(MODELS, (0.03, 0.02, 0.01), strict=True)
            }
            for family in ("top10", "top20", "rolling")
        }
    return {"quality": quality, "portfolios": portfolios}


@pytest.mark.unit
def test_quality_table_keeps_model_order_and_labels() -> None:
    table = quality_table(_summary(), MODELS, HORIZONS)

    assert len(table) == len(MODELS) * len(HORIZONS)
    assert list(table["model"].drop_duplicates()) == list(MODELS)
    row = table[(table["model"] == "LGB") & (table["horizon"] == 20)].iloc[0]
    assert row["ic_mean"] == pytest.approx(0.10 + 0.03 + 0.02)
    assert row["top10_hit"] == pytest.approx(0.53)


@pytest.mark.unit
def test_portfolio_table_reads_performance_and_stats() -> None:
    table = portfolio_table(_summary(), "top10", MODELS, HORIZONS)

    row = table[(table["model"] == "XGB") & (table["horizon"] == 10)].iloc[0]
    assert row["family"] == "top10"
    assert row["total_return"] == pytest.approx(0.05 + 0.02 + 0.01)
    assert row["active_return"] == pytest.approx(0.08)
    assert row["max_drawdown"] == pytest.approx(-0.10)


@pytest.mark.unit
def test_spearman_rank_corr_perfect_and_reversed() -> None:
    assert spearman_rank_corr([1, 2, 3], [10, 20, 30]) == pytest.approx(1.0)
    assert spearman_rank_corr([1, 2, 3], [30, 20, 10]) == pytest.approx(-1.0)
    assert math.isnan(spearman_rank_corr([1, 2], [1, 2]))


@pytest.mark.unit
def test_rank_correlations_cover_all_horizon_pairs() -> None:
    summary = _summary()
    quality = quality_table(summary, MODELS, HORIZONS)
    portfolios = {
        family: portfolio_table(summary, family, MODELS, HORIZONS)
        for family in ("top10", "top20", "rolling")
    }

    correlations = rank_correlations(quality, portfolios, HORIZONS)

    assert set(correlations) == {"5v10", "5v20", "10v20"}
    for block in correlations.values():
        assert block["quality"]["ic_mean"] == pytest.approx(1.0)
        assert block["portfolio"]["top10"]["total_return"] == pytest.approx(1.0)


@pytest.mark.unit
def test_delta_table_reports_direction() -> None:
    summary = _summary()
    quality = quality_table(summary, MODELS, HORIZONS)
    portfolios = {
        family: portfolio_table(summary, family, MODELS, HORIZONS)
        for family in ("top10", "top20", "rolling")
    }

    deltas = delta_table(quality, portfolios, HORIZONS)

    row = deltas[
        (deltas["model"] == "LGB")
        & (deltas["group"] == "quality")
        & (deltas["metric"] == "ic_mean")
        & (deltas["from_horizon"] == 5)
        & (deltas["to_horizon"] == 20)
    ].iloc[0]
    assert row["delta"] == pytest.approx(0.015)
    top10 = deltas[
        (deltas["model"] == "LGB")
        & (deltas["group"] == "top10")
        & (deltas["metric"] == "total_return")
        & (deltas["from_horizon"] == 5)
        & (deltas["to_horizon"] == 20)
    ].iloc[0]
    assert top10["delta"] == pytest.approx(0.015)


@pytest.mark.unit
def test_run_writes_artifacts(tmp_path: Path) -> None:
    (tmp_path / "summary.json").write_text(json.dumps(_summary()), encoding="utf-8")
    out = tmp_path / "out"

    run(tmp_path, models=MODELS, horizons=HORIZONS, out_dir=out)

    for name in (
        "horizon_quality.csv",
        "horizon_portfolio_top10.csv",
        "horizon_portfolio_top20.csv",
        "horizon_portfolio_rolling.csv",
        "horizon_deltas.csv",
        "horizon_rank_corr.json",
    ):
        assert (out / name).is_file(), name


@pytest.mark.unit
def test_load_summary_reports_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="summary.json"):
        load_summary(tmp_path)
