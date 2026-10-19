"""深度评估：归因分解、容量约束、共识投票与季度统计的口径测试。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backend.scripts.backtest2026.attribution import (
    _held_residual,
    _merge_residuals,
)
from backend.scripts.backtest2026.capacity_eval import (
    _require_listing_age,
    build_capacity_spec,
)
from backend.scripts.backtest2026.consensus import (
    ConsensusSettings,
    _consensus_outputs,
    _vote,
)
from backend.scripts.backtest2026 import data as data_mod
from backend.scripts.backtest2026.data import MarketPanel
from backend.scripts.backtest2026.deep_common import FactorDay, decompose_day
from backend.scripts.backtest2026.extend_matrix import (
    MatrixExtensionSettings,
    artifact_paths,
    extension_dates,
)
from backend.scripts.backtest2026.quarterly import QuarterlySettings, _quarter_stats
from backend.scripts.backtest2026.strategies import StrategySpec, run_strategy


def _panel(
    n_days: int = 8,
    symbols: tuple[str, ...] = ("SH600000", "SH600001", "SH600002", "SH600003"),
    price: float = 10.0,
    amount: float | None = None,
) -> MarketPanel:
    dates = pd.bdate_range("2026-01-05", periods=n_days)
    close = pd.DataFrame(price, index=dates, columns=list(symbols), dtype=float)
    volume = pd.DataFrame(1_000_000.0, index=dates, columns=list(symbols))
    false_matrix = pd.DataFrame(False, index=dates, columns=list(symbols))
    amount_matrix = (
        pd.DataFrame(amount, index=dates, columns=list(symbols), dtype=float)
        if amount is not None
        else None
    )
    return MarketPanel(
        dates=dates,
        full_calendar=dates,
        symbols=pd.Index(symbols),
        close_raw=close,
        close_qfq=close.copy(),
        volume=volume,
        limit_up=false_matrix.copy(),
        limit_down=false_matrix.copy(),
        suspended=false_matrix.copy(),
        is_st=pd.Series(False, index=list(symbols)),
        age_days=pd.DataFrame(200.0, index=dates, columns=list(symbols)),
        close_qfq_ffill=close.copy(),
        amount=amount_matrix,
    )


@pytest.mark.unit
def test_decompose_day_components_sum_to_gross_excess() -> None:
    weights = pd.Series({"A": 0.3, "B": 0.2, "C": 0.1, "D": 0.0}, dtype=float)
    industry = pd.Series({"A": "X", "B": "X", "C": "Y", "D": "Y"})
    size_z = pd.Series({"A": 1.0, "B": -1.0, "C": 0.5, "D": 0.0})
    factors = FactorDay(
        beta_industry=pd.Series({"X": 0.01, "Y": -0.005}),
        gamma_size=0.02,
        residual=pd.Series({"A": 0.003, "B": -0.003, "C": 0.001, "D": -0.001}),
        universe_return=0.0025,
        universe_industry_weight=pd.Series({"X": 0.5, "Y": 0.5}),
        universe_size=0.0,
        n_universe=4,
    )
    result = decompose_day(weights, factors, industry, size_z)

    # 日收益按因子结构重建：r_i = beta_ind + gamma*z_i + eps_i。
    returns = (
        factors.beta_industry.reindex(industry).to_numpy()
        + factors.gamma_size * size_z.to_numpy()
        + factors.residual.to_numpy()
    )
    gross = float((weights.to_numpy() * returns).sum())
    expected_excess = gross - factors.universe_return

    assert result["component_sum"] == pytest.approx(expected_excess, abs=1e-12)
    assert float(result["industry_detail"].sum()) == pytest.approx(
        result["industry"], abs=1e-12
    )
    assert result["cash_drag"] == pytest.approx(-(1.0 - weights.sum()) * 0.0025)
    assert result["size_exposure"] == pytest.approx(0.25)
    assert result["invested"] == pytest.approx(0.6)


@pytest.mark.unit
def test_decompose_day_keeps_unheld_universe_industries_in_industry_effect() -> None:
    weights = pd.Series({"A": 0.4, "B": 0.1, "C": 0.2}, dtype=float)
    industry = pd.Series({"A": "X", "B": "X", "C": "Y", "D": "Z"})
    size_z = pd.Series({"A": 1.0, "B": -1.0, "C": 0.5, "D": 0.0})
    factors = FactorDay(
        beta_industry=pd.Series({"X": 0.01, "Y": -0.005, "Z": 0.02}),
        gamma_size=0.02,
        # 每个行业的残差均值为 0（与 OLS 拟合后的口径一致）。
        residual=pd.Series({"A": 0.003, "B": -0.003, "C": 0.0, "D": 0.0}),
        universe_return=0.0,
        # 基准行业权重与四只股票的实际行业分布一致（X 两只、Y/Z 各一只）。
        universe_industry_weight=pd.Series({"X": 0.5, "Y": 0.25, "Z": 0.25}),
        universe_size=0.125,
        n_universe=4,
    )
    returns = (
        factors.beta_industry.reindex(industry).to_numpy()
        + factors.gamma_size * size_z.to_numpy()
        + factors.residual.to_numpy()
    )
    factors = FactorDay(
        beta_industry=factors.beta_industry,
        gamma_size=factors.gamma_size,
        residual=factors.residual,
        universe_return=float(returns.mean()),
        universe_industry_weight=factors.universe_industry_weight,
        universe_size=factors.universe_size,
        n_universe=factors.n_universe,
    )
    result = decompose_day(weights, factors, industry, size_z)
    return_series = pd.Series(returns, index=industry.index)
    gross = float(return_series.reindex(weights.index) @ weights)
    assert result["component_sum"] == pytest.approx(
        gross - factors.universe_return, abs=1e-12
    )
    # 未持有的 Z 行业仍须以负的基准暴露进入行业明细。
    detail = result["industry_detail"]
    assert detail["Z"] == pytest.approx(-0.25 * 0.02 * weights.sum())
    assert float(detail.sum()) == pytest.approx(result["industry"], abs=1e-12)


@pytest.mark.unit
def test_merge_residuals_keeps_held_names_outside_factor_universe() -> None:
    factors_residual = pd.Series({"A": 0.001, "B": 0.002})
    held_residual = pd.Series({"B": 0.002, "C": -0.004})
    merged = _merge_residuals(factors_residual, held_residual)
    assert set(merged.index) == {"A", "B", "C"}
    assert merged["A"] == pytest.approx(0.001)
    assert merged["C"] == pytest.approx(-0.004)


@pytest.mark.unit
def test_held_residual_fills_names_missing_from_attribution_universe() -> None:
    factors_residual = pd.Series({"A": 0.001})
    weights = pd.Series({"A": 0.5, "C": 0.5})
    returns_day = pd.Series({"A": 0.02, "C": 0.05})
    size_z_day = pd.Series({"A": 0.0, "C": 1.0})
    beta = pd.Series({"X": 0.01, "Y": -0.01})
    industry = pd.Series({"A": "X", "C": "Y"})

    residual = _held_residual(
        factors_residual, weights, returns_day, size_z_day, beta, 0.02, industry
    )

    assert residual["A"] == pytest.approx(0.001)
    # C 不在域内：残差 = 0.05 - (-0.01) - 0.02*1.0 = 0.04。
    assert residual["C"] == pytest.approx(0.04)
    merged = _merge_residuals(factors_residual, residual)
    assert merged["C"] == pytest.approx(0.04)


@pytest.mark.unit
def test_capacity_constraint_caps_shares_and_records_fill() -> None:
    panel = _panel(n_days=8, amount=10_000_000.0)
    day = panel.dates[0]
    pred = {day: pd.Series({"SH600000": 3.0, "SH600001": 2.0, "SH600002": 1.0})}
    spec = StrategySpec(
        mode="cohort",
        k=2,
        horizon=2,
        initial_capital=2_000_000.0,
        per_name=1_000_000.0,
        max_participation=0.01,
    )
    result = run_strategy(panel, pred, spec)
    buys = result.trades[result.trades["side"] == "buy"]
    assert len(buys) == 2
    assert (buys["shares"] == 10_000).all()
    assert (buys["requested_shares"] == 100_000).all()
    assert np.allclose(buys["cap_value"], 100_000.0)
    assert np.allclose(buys["fill_ratio"], 0.1)
    assert bool(buys["cap_binding"].all())
    assert set(buys["symbol"]) == {"SH600000", "SH600001"}


@pytest.mark.unit
def test_capacity_none_fills_full_request() -> None:
    panel = _panel(n_days=8)
    day = panel.dates[0]
    pred = {day: pd.Series({"SH600000": 3.0, "SH600001": 2.0, "SH600002": 1.0})}
    spec = build_capacity_spec(
        "top10", horizon=2, capital=2_000_000.0, participation=None
    )
    result = run_strategy(panel, pred, spec)
    buys = result.trades[result.trades["side"] == "buy"]
    assert not buys.empty
    assert np.allclose(buys["fill_ratio"], 1.0)
    assert not buys["cap_binding"].any()


@pytest.mark.unit
def test_load_amount_matrix_converts_wan_yuan_to_yuan(monkeypatch) -> None:
    frame = pd.DataFrame(
        {
            "symbol": ["SH600000"],
            "trade_date": [pd.Timestamp("2026-01-05")],
            "amount": [100.0],
        }
    )
    monkeypatch.setattr(data_mod, "_read_partitions", lambda *a, **k: frame)
    matrix = data_mod.load_amount_matrix("2026-01-01", "2026-01-31")
    # 100 万元 = 1,000,000 元；容量上限与 per_name 必须同量纲。
    assert matrix.iloc[0, 0] == pytest.approx(1_000_000.0)


@pytest.mark.unit
def test_capacity_requires_listing_age() -> None:
    panel = _panel()
    _require_listing_age(panel)
    empty = _panel()
    empty.age_days[:] = np.nan
    with pytest.raises(RuntimeError, match="上市天数未附加"):
        _require_listing_age(empty)


@pytest.mark.unit
def test_capacity_spec_scales_per_name_with_capital() -> None:
    spec = build_capacity_spec("top10", 5, 100_000_000.0, 0.01)
    assert spec.per_name == pytest.approx(10_000_000.0)
    assert spec.max_participation == pytest.approx(0.01)
    rolling = build_capacity_spec("rolling", 5, 50_000_000.0, None)
    assert rolling.per_name is None
    assert rolling.mode == "rolling"


@pytest.mark.unit
def test_consensus_vote_selects_daily_topk() -> None:
    panel = _panel(n_days=3)
    days = [panel.dates[0], panel.dates[1]]
    symbols = panel.symbols
    base = np.ones((len(days), len(symbols)), dtype=bool)
    forward = pd.DataFrame(
        [[0.10, 0.20, -0.10, 0.00], [0.05, 0.00, -0.05, 0.20]],
        index=days,
        columns=symbols,
    )
    pred = pd.DataFrame(
        {
            "symbol": ["SH600000", "SH600001", "SH600002", "SH600003"] * 2,
            "trade_date": [days[0]] * 4 + [days[1]] * 4,
            "pred": [4.0, 3.0, 2.0, 1.0, 1.0, 2.0, 3.0, 4.0],
        }
    )
    votes, z_sum, z_count, scores, screens, top_sets = _vote(
        panel, days, symbols, base, forward, pred, "m1", 2, min_names=2
    )
    assert votes[0].tolist() == [1, 1, 0, 0]
    assert votes[1].tolist() == [0, 0, 1, 1]
    assert top_sets[days[0]] == {"SH600000", "SH600001"}
    assert top_sets[days[1]] == {"SH600002", "SH600003"}
    assert z_count[0].tolist() == [1, 1, 1, 1]
    assert len(scores) == 2
    assert scores[0]["top20_threshold"] == pytest.approx(3.0)
    assert scores[0]["rank20_gap"] == pytest.approx(1.0)
    assert {row["filter"] for row in screens} == {
        "top10",
        "top20",
        "top50",
        "z1",
        "z2",
        "top1pct",
    }


@pytest.mark.unit
def test_consensus_distribution_and_model_overlap() -> None:
    panel = _panel(n_days=2)
    days = [panel.dates[0], panel.dates[1]]
    symbols = panel.symbols
    base = np.ones((len(days), len(symbols)), dtype=bool)
    forward = pd.DataFrame(
        [[0.10, 0.20, -0.10, 0.00], [0.05, 0.00, -0.05, 0.20]],
        index=days,
        columns=symbols,
    )
    votes = np.zeros((2, 4), dtype=np.int16)
    votes[0, [0, 1]] = 2
    votes[1, 3] = 2
    votes[1, [0, 2]] = 1
    top_sets = {
        "m1": {
            days[0]: {"SH600000", "SH600001"},
            days[1]: {"SH600002", "SH600003"},
        },
        "m2": {
            days[0]: {"SH600000", "SH600001"},
            days[1]: {"SH600003", "SH600000"},
        },
    }
    # 用临时目录承载 CSV 产物。
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        settings = ConsensusSettings(
            run_dir=Path(tmp),
            out_dir=Path(tmp),
            start="2026-01-01",
            end="2026-01-31",
        )
        outputs = _consensus_outputs(
            settings,
            5,
            days,
            symbols,
            base,
            forward,
            votes,
            ("m1", "m2"),
            top_sets,
        )
    distribution = outputs["distribution"].set_index("consensus")
    assert distribution.loc[2, "n_stock_days"] == 3
    assert distribution.loc[2, "mean_ret"] == pytest.approx(0.5 / 3.0)
    assert distribution.loc[2, "mean_excess"] == pytest.approx(
        ((0.10 - 0.05) + (0.20 - 0.05) + (0.20 - 0.05)) / 3.0
    )
    daily = outputs["daily"]
    assert daily.loc[days[0], "union"] == 2
    assert daily.loc[days[1], "union"] == 3
    assert daily.loc[days[1], "all_models"] == 1
    model = outputs["model_overlap"].set_index("model")
    assert model.loc["m1", "in_half_ret"] == pytest.approx((0.15 + 0.075) / 2.0)
    assert model.loc["m1", "unique_share"] == pytest.approx(0.25)


@pytest.mark.unit
def test_consensus_outputs_skips_threshold_without_any_stock() -> None:
    panel = _panel(n_days=2)
    days = [panel.dates[0], panel.dates[1]]
    symbols = panel.symbols
    base = np.ones((2, len(symbols)), dtype=bool)
    forward = pd.DataFrame(
        [[0.10, 0.20, -0.10, 0.00], [0.05, 0.00, -0.05, 0.20]],
        index=days,
        columns=symbols,
    )
    votes = np.zeros((2, len(symbols)), dtype=np.int16)
    votes[0, [0, 1]] = 2
    votes[1, 3] = 2
    model_names = ("m1", "m2", "m3", "m4", "m5")
    top_sets = {
        model: {
            days[0]: {"SH600000", "SH600001"},
            days[1]: {"SH600002"},
        }
        for model in model_names
    }
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        settings = ConsensusSettings(
            run_dir=Path(tmp),
            out_dir=Path(tmp),
            start="2026-01-01",
            end="2026-01-31",
        )
        outputs = _consensus_outputs(
            settings,
            5,
            days,
            symbols,
            base,
            forward,
            votes,
            model_names,
            top_sets,
        )
    # 3/4/5 票阈值下没有任何股票，必须跳过而不是崩溃。
    assert set(outputs["strategies"]["threshold"]) == {"2"}


@pytest.mark.unit
def test_dynamic_selection_waits_for_forward_label_to_mature() -> None:
    """选模只能用已到期的 IC：d 日最多看到 d-H-1 行。"""
    from backend.scripts.backtest2026.consensus import _dynamic_selection

    days = pd.bdate_range("2026-01-05", periods=40)
    ic = pd.DataFrame({"A": 0.1, "B": 0.0}, index=days)
    ic.loc[days[:11], "B"] = 0.9
    frames = {}
    for model, excess in (("A", 0.01), ("B", 0.02)):
        frames[model] = pd.DataFrame(
            {
                "ic_eligible": ic[model],
                "top20_excess": excess,
                "top20_ret": excess,
            },
            index=days,
        )
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp)
        for model, frame in frames.items():
            frame.to_csv(run_dir / f"quality_daily_5_{model}.csv", index_label="date")
        settings = ConsensusSettings(run_dir=run_dir, out_dir=run_dir)
        result = _dynamic_selection(settings, 5, ("A", "B"))
        lag20 = pd.read_csv(
            run_dir / "dynamic_selection_lag20_5.csv",
            index_col="date",
            parse_dates=["date"],
        )
    # shift(1) 会在第 20 行就选中 B（窗口已含前 11 个高 IC 行），
    # 正确口径要等到第 25 行（row=19 才是第一个满 20 行且不含未来标签的窗口）。
    assert lag20.loc[days[20], "picked"] == "equal"
    assert lag20.loc[days[25], "picked"] == "B"
    assert set(result["name"]) == {
        "dynamic_ic_lag20",
        "ic_weighted_lag20",
        "dynamic_ic_lag60",
        "ic_weighted_lag60",
        "dynamic_ic_lag120",
        "ic_weighted_lag120",
        "equal_weight",
    }


def _write_prediction(path, dates, pred_value: float = 1.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"symbol": "SH600000", "trade_date": pd.Timestamp(day), "pred": pred_value}
        for day in dates
    ]
    pd.DataFrame(rows).to_parquet(path, index=False)


@pytest.mark.unit
def test_load_extended_predictions_prefers_stored_on_overlap(tmp_path) -> None:
    run_dir = tmp_path / "run"
    _write_prediction(run_dir / "pred.parquet", ["2026-08-26", "2026-08-27"], 1.0)
    ext_dir = tmp_path / "ext"
    _write_prediction(
        ext_dir / "pred_extend_5_LGB.parquet",
        ["2026-08-27", "2026-08-28"],
        9.0,
    )

    frame = data_mod.load_extended_predictions(
        run_dir, 5, "LGB", "2026-08-01", "2026-08-28", ext_dir
    )

    assert frame["trade_date"].dt.strftime("%Y-%m-%d").tolist() == [
        "2026-08-26",
        "2026-08-27",
        "2026-08-28",
    ]
    overlap = frame.loc[frame["trade_date"] == pd.Timestamp("2026-08-27"), "pred"]
    assert overlap.tolist() == [1.0]
    assert frame["source"].tolist() == ["stored", "stored", "extended"]


@pytest.mark.unit
def test_load_extended_predictions_respects_end(tmp_path) -> None:
    run_dir = tmp_path / "run"
    _write_prediction(run_dir / "pred.parquet", ["2026-08-26"], 1.0)
    ext_dir = tmp_path / "ext"
    _write_prediction(
        ext_dir / "pred_extend_10_TFT.parquet", ["2026-08-27", "2026-08-28"], 2.0
    )

    frame = data_mod.load_extended_predictions(
        run_dir, 10, "TFT", "2026-08-01", "2026-08-27", ext_dir
    )

    assert frame["trade_date"].dt.strftime("%Y-%m-%d").tolist() == [
        "2026-08-26",
        "2026-08-27",
    ]


@pytest.mark.unit
def test_extension_dates_skip_stored_day_and_respect_end() -> None:
    calendar = pd.bdate_range("2026-08-24", "2026-09-04")
    dates = extension_dates(pd.Timestamp("2026-08-27"), "2026-09-02", calendar)
    assert dates == [
        "2026-08-28",
        "2026-08-31",
        "2026-09-01",
        "2026-09-02",
    ]


@pytest.mark.unit
def test_extension_dates_empty_when_stored_day_is_latest() -> None:
    calendar = pd.bdate_range("2026-08-24", "2026-08-28")
    assert extension_dates(pd.Timestamp("2026-08-28"), "2026-10-16", calendar) == []


@pytest.mark.unit
def test_extension_artifact_paths_carry_horizon_and_model() -> None:
    pred, validation = artifact_paths(Path("/tmp/ext"), 10, "TFT")
    assert str(pred).endswith("pred_extend_10_TFT.parquet")
    assert str(validation).endswith("extend_validation_10_TFT.json")


@pytest.mark.unit
def test_matrix_extension_defaults_cover_all_models() -> None:
    settings = MatrixExtensionSettings(out_dir=Path("/tmp/ext"))
    assert settings.end == "2026-10-16"
    assert len(settings.models) == 13
    assert settings.horizons == (5, 10)


@pytest.mark.unit
def test_quarter_stats_identifies_champion_and_ranks() -> None:
    rng = np.random.default_rng(20261019)
    days = pd.bdate_range("2025-01-06", periods=40)
    models = ["A", "B", "C"]
    ic = pd.DataFrame(
        {
            "A": 0.10 + rng.normal(0, 0.01, len(days)),
            "B": 0.05 + rng.normal(0, 0.01, len(days)),
            "C": 0.02 + rng.normal(0, 0.01, len(days)),
        },
        index=days,
    )
    excess = pd.DataFrame(
        {
            "A": 0.010 + rng.normal(0, 0.002, len(days)),
            "B": 0.005 + rng.normal(0, 0.002, len(days)),
            "C": 0.001 + rng.normal(0, 0.002, len(days)),
        },
        index=days,
    )
    top_ret = excess + 0.002
    pool = pd.Series(0.0, index=days)
    settings = QuarterlySettings(
        run_dir=pd.Timestamp("2026-01-01"),
        out_dir=pd.Timestamp("2026-01-01"),
        min_days=20,
        block=5,
        n_boot=100,
    )
    row, table = _quarter_stats(
        "2025Q1", ic, excess, top_ret, pool, models, settings, 5
    )
    assert row["best_ic_model"] == "A"
    assert row["best_excess_model"] == "A"
    assert row["days"] == 40
    assert np.isfinite(row["ic_kendall_w"])
    assert np.isfinite(row["excess_kendall_w"])
    assert np.isfinite(row["white_stat"])
    assert table.set_index("model").loc["A", "ic_rank"] == 1
    assert table.set_index("model").loc["A", "excess_rank"] == 1
