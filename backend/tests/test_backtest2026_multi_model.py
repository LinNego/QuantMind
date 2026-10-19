"""多模型评估编排器的纯函数与落盘测试。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from backend.scripts.backtest2026 import multi_model as mm
from backend.scripts.backtest2026.data import MarketPanel
from backend.scripts.backtest2026.model_registry import (
    HORIZONS,
    MODEL_ORDER,
    ModelRun,
)
from backend.scripts.backtest2026.multi_model import (
    ModelOutput,
    RunSettings,
    _benchmark_block,
    _build_per_stock,
    _first_seen_signature,
    _inventory,
    _load_frame,
    _missing_runs,
    _monthly_wide,
    _prediction_signature,
    _quality_monthly_wide,
    _write_equity,
    _write_per_stock,
    main,
    parse_args,
)


def _run(
    key: str,
    horizon: int,
    *,
    run_dir: Path | None = None,
    ready: bool = True,
    reason: str = "",
) -> ModelRun:
    return ModelRun(
        key=key,
        name=key,
        family="树模型",
        horizon=horizon,
        run_dir=run_dir,
        job_name=f"T{horizon}_Cross300_{key}_Split20261018",
        model_type=key.lower(),
        ready=ready,
        reason=reason,
    )


def _panel(dates: pd.DatetimeIndex, symbols: tuple[str, ...]) -> MarketPanel:
    close = pd.DataFrame(10.0, index=dates, columns=list(symbols))
    false_matrix = pd.DataFrame(False, index=dates, columns=list(symbols))
    return MarketPanel(
        dates=dates,
        full_calendar=dates,
        symbols=pd.Index(symbols),
        close_raw=close.copy(),
        close_qfq=close.copy(),
        volume=pd.DataFrame(1_000_000.0, index=dates, columns=list(symbols)),
        limit_up=false_matrix.copy(),
        limit_down=false_matrix.copy(),
        suspended=false_matrix.copy(),
        is_st=pd.Series(False, index=list(symbols)),
        age_days=pd.DataFrame(200.0, index=dates, columns=list(symbols)),
        close_qfq_ffill=close.copy(),
    )


def _frame(label_return: bool = True) -> pd.DataFrame:
    dates = pd.bdate_range("2026-01-05", periods=2)
    frame = pd.DataFrame(
        {
            "symbol": ["SH600000", "SH600001", "SH600000", "SH600001"],
            "trade_date": [dates[0], dates[0], dates[1], dates[1]],
            "pred": [0.1, 0.2, 0.3, 0.4],
            "label_return": [0.01, 0.02, 0.03, np.nan]
            if label_return
            else [np.nan] * 4,
            "source": "stored",
        }
    )
    frame.attrs["label_return_available"] = label_return
    return frame


@pytest.mark.unit
def test_prediction_signature_tracks_coverage_not_pred_values() -> None:
    first = _prediction_signature(_frame())
    second_frame = _frame()
    second_frame["pred"] = [9.0, 8.0, 7.0, 6.0]
    second = _prediction_signature(second_frame)

    assert first["rows"] == 4
    assert first["days"] == 2
    assert first["symbols"] == 2
    assert first["pred_nan"] == 0
    assert first["pred_valid"] == 4
    assert first["label_return_available"] is True
    assert first["label_nan"] == 1
    assert first["hash"] == second["hash"]


@pytest.mark.unit
def test_prediction_signature_marks_absent_label_return() -> None:
    signature = _prediction_signature(_frame(label_return=False))
    assert signature["label_return_available"] is False
    assert signature["label_nan"] is None


@pytest.mark.unit
def test_first_seen_signature_reads_run_artifacts(tmp_path: Path) -> None:
    dates = pd.bdate_range("2016-01-04", periods=3)
    pd.DataFrame(
        {
            "symbol": ["600000.SH", "600000.SH", "000001.SZ"],
            "trade_date": [dates[0], dates[1], dates[2]],
        }
    ).to_parquet(tmp_path / "pred.parquet", index=False)

    signature = _first_seen_signature(tmp_path)
    repeat = _first_seen_signature(tmp_path)

    assert signature["symbols"] == 2
    assert signature["hash"] == repeat["hash"]


@pytest.mark.unit
def test_monthly_wide_and_quality_monthly_wide_align_columns() -> None:
    monthly = _monthly_wide(
        {
            "A": {"2026-02": 0.02, "2026-01": 0.01},
            "B": {"2026-01": 0.03, "2026-02": 0.04},
        },
        ["A", "B"],
    )
    assert list(monthly.index) == ["2026-01", "2026-02"]
    assert list(monthly.columns) == ["A", "B"]

    quality = _quality_monthly_wide(
        {
            "A": {"monthly": {"2026-01": {"ic": 0.1, "top10_hit": 0.5}}},
            "B": {"monthly": {"2026-01": {"ic": 0.2, "top10_hit": 0.6}}},
        },
        ["A", "B"],
        "ic",
    )
    assert quality.loc["2026-01"].to_dict() == {"A": 0.1, "B": 0.2}


@pytest.mark.unit
def test_write_equity_aligns_benchmark_returns(tmp_path: Path) -> None:
    dates = pd.bdate_range("2026-01-05", periods=4)
    index = dates[1:]
    evaluation = SimpleNamespace(
        equity_common=pd.Series([2_000_000.0, 2_020_000.0, 2_010_000.0], index=index),
        daily_returns=pd.Series([0.01, -0.005, 0.0], index=index),
        excess=pd.Series([0.005, -0.01, -0.005], index=index),
    )
    benchmark = pd.DataFrame({"return": [0.0, 0.005, 0.005, 0.005]}, index=dates)

    path = tmp_path / "equity.csv"
    _write_equity(evaluation, benchmark, path)

    frame = pd.read_csv(path, index_col=0)
    assert list(frame.columns) == [
        "equity",
        "daily_return",
        "benchmark_return",
        "benchmark_equity",
        "excess",
    ]
    assert frame["benchmark_return"].tolist() == pytest.approx([0.005, 0.005, 0.005])


@pytest.mark.unit
def test_build_per_stock_marks_predicted_and_missing(tmp_path: Path) -> None:
    dates = pd.bdate_range("2026-01-05", periods=2)
    symbols = ("SH600000", "SH600001")
    panel = _panel(dates, symbols)
    coverage = pd.DataFrame(
        {
            "rows": [2, 0],
            "days": [2, 0],
            "first_date": [dates[0], pd.NaT],
            "last_date": [dates[1], pd.NaT],
            "mean_pred": [0.15, np.nan],
        },
        index=list(symbols),
    )
    latest = pd.Series([0.3, np.nan], index=list(symbols))
    outputs = [
        ModelOutput(
            run=_run("LGB", 5),
            quality_daily=pd.DataFrame(),
            quality_summary={},
            portfolios={},
            coverage=coverage,
            latest=latest,
            signature={},
        )
    ]

    frame = _build_per_stock(outputs, panel, RunSettings(out_dir=tmp_path))

    assert frame.loc["SH600000", "status"] == "predicted"
    assert frame.loc["SH600001", "status"] == "never_predicted"
    assert frame.loc["SH600000", "valid_days_5_LGB"] == 2
    assert frame.loc["SH600000", "latest_pred_5_LGB"] == pytest.approx(0.3)


@pytest.mark.unit
def test_write_per_stock_writes_horizon_latest_tables(tmp_path: Path) -> None:
    frame = pd.DataFrame(
        {
            "latest_pred_5_LGB": [0.1],
            "latest_pred_5_XGB": [0.2],
            "latest_pred_10_LGB": [0.3],
        },
        index=pd.Index(["SH600000"], name="symbol"),
    )
    outputs = [
        ModelOutput(_run("LGB", 5), None, {}, {}, None, None, {}),
        ModelOutput(_run("XGB", 5), None, {}, {}, None, None, {}),
        ModelOutput(_run("LGB", 10), None, {}, {}, None, None, {}),
    ]

    _write_per_stock(frame, outputs, RunSettings(out_dir=tmp_path))

    assert (tmp_path / "per_stock_coverage.csv").is_file()
    latest = pd.read_csv(tmp_path / "per_stock_latest_5.csv", index_col=0)
    assert list(latest.columns) == ["LGB", "XGB"]
    assert latest.loc["SH600000", "LGB"] == pytest.approx(0.1)


@pytest.mark.unit
def test_inventory_filters_models_and_horizons() -> None:
    registry = {
        horizon: {key: _run(key, horizon) for key in MODEL_ORDER}
        for horizon in HORIZONS
    }

    frame = _inventory(registry, ("LGB", "XGB"), (5,))

    assert len(frame) == 2
    assert set(frame["key"]) == {"LGB", "XGB"}
    assert set(frame["horizon"]) == {5}


@pytest.mark.unit
def test_missing_runs_reports_not_ready_combinations() -> None:
    registry = {
        5: {
            "LGB": _run("LGB", 5, ready=True),
            "XGB": _run("XGB", 5, ready=False, reason="训练中"),
        },
        10: {
            "LGB": _run("LGB", 10, ready=False, reason="未开始"),
            "XGB": _run("XGB", 10, ready=True),
        },
    }
    settings = RunSettings(models=("LGB", "XGB"), horizons=(5, 10))

    missing = _missing_runs(registry, settings)

    assert [(run.key, run.horizon) for run in missing] == [("XGB", 5), ("LGB", 10)]


@pytest.mark.unit
def test_benchmark_block_reports_pool_and_indices() -> None:
    dates = pd.bdate_range("2026-01-05", periods=4)
    benchmark = pd.DataFrame(
        {
            "return": [0.0, 0.01, -0.02, 0.03],
            "n": [0, 100, 100, 100],
            "equity": 2_000_000.0 * np.cumprod([1.0, 1.01, 0.98, 1.03]),
        },
        index=dates,
    )
    index_closes = pd.DataFrame({"SH000300": [100.0, 101.0, 102.0, 103.0]}, index=dates)

    performance, indices = _benchmark_block(
        benchmark, index_closes, dates[0], dates[-1]
    )

    assert performance["total_return"] > 0
    assert performance["n_pool_mean"] == pytest.approx(100.0)
    assert indices["SH000300"] == pytest.approx(0.03)


@pytest.mark.unit
def test_parse_args_defaults_and_overrides() -> None:
    defaults = parse_args([])
    assert defaults.start == "2026-01-05"
    assert defaults.end == "2026-08-26"
    assert defaults.allow_missing is False

    custom = parse_args(
        ["--models", "LGB", "XGB", "--horizons", "5", "--allow-missing"]
    )
    assert custom.models == ["LGB", "XGB"]
    assert custom.horizons == [5]
    assert custom.allow_missing is True


@pytest.mark.unit
def test_main_rejects_unknown_model() -> None:
    with pytest.raises(SystemExit, match="未知模型"):
        main(["--models", "NOPE"])


@pytest.mark.unit
def test_load_frame_rejects_missing_run_dir() -> None:
    with pytest.raises(FileNotFoundError, match="没有运行目录"):
        _load_frame(_run("LGB", 5), RunSettings())


@pytest.mark.unit
def test_load_frame_rejects_duplicate_rows(tmp_path: Path) -> None:
    frame = _frame()
    duplicated = pd.concat([frame, frame.iloc[[0]]], ignore_index=True)
    duplicated.to_parquet(tmp_path / "pred.parquet", index=False)

    with pytest.raises(RuntimeError, match="重复"):
        _load_frame(_run("LGB", 5, run_dir=tmp_path), RunSettings())


def _synthetic_panel(
    dates: pd.DatetimeIndex, symbols: tuple[str, ...]
) -> tuple[MarketPanel, np.ndarray]:
    rng = np.random.default_rng(42)
    returns = rng.normal(0.0, 0.008, size=(len(dates), len(symbols)))
    close = pd.DataFrame(
        10.0 * np.cumprod(1.0 + returns, axis=0),
        index=dates,
        columns=list(symbols),
    )
    false_matrix = pd.DataFrame(False, index=dates, columns=list(symbols))
    panel = MarketPanel(
        dates=dates,
        full_calendar=dates,
        symbols=pd.Index(symbols),
        close_raw=close.copy(),
        close_qfq=close.copy(),
        volume=pd.DataFrame(1_000_000.0, index=dates, columns=list(symbols)),
        limit_up=false_matrix.copy(),
        limit_down=false_matrix.copy(),
        suspended=false_matrix.copy(),
        is_st=pd.Series(False, index=list(symbols)),
        age_days=pd.DataFrame(400.0, index=dates, columns=list(symbols)),
        close_qfq_ffill=close.ffill(),
    )
    return panel, returns


def _write_run_predictions(
    run_dir: Path,
    dates: pd.DatetimeIndex,
    symbols: tuple[str, ...],
    returns: np.ndarray,
    *,
    label_return: bool,
) -> None:
    rows = []
    for day_index, day in enumerate(dates):
        for symbol_index, symbol in enumerate(symbols):
            record = {
                "symbol": symbol,
                "trade_date": day,
                "pred": float(
                    returns[day_index + 1, symbol_index] * 100.0
                    if day_index + 1 < len(dates)
                    else 0.0
                ),
            }
            if label_return:
                record["label_return"] = float(returns[day_index, symbol_index])
            rows.append(record)
    pd.DataFrame(rows).to_parquet(run_dir / "pred.parquet", index=False)


@pytest.mark.unit
def test_run_end_to_end_with_synthetic_panel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dates = pd.bdate_range("2026-01-05", periods=24)
    symbols = tuple(f"SH{600000 + index}" for index in range(40))
    panel, returns = _synthetic_panel(dates, symbols)

    registry: dict[int, dict[str, ModelRun]] = {}
    for horizon in HORIZONS:
        registry[horizon] = {}
        for key in MODEL_ORDER:
            if key not in ("LGB", "XGB"):
                registry[horizon][key] = _run(
                    key, horizon, ready=False, reason="synthetic"
                )
                continue
            run_dir = tmp_path / f"run_{horizon}_{key}"
            run_dir.mkdir()
            _write_run_predictions(
                run_dir,
                dates,
                symbols,
                returns,
                label_return=key == "LGB",
            )
            registry[horizon][key] = _run(key, horizon, run_dir=run_dir)

    index_closes = pd.DataFrame(
        {
            "SH000300": np.linspace(4000.0, 4100.0, len(dates)),
            "SH000905": np.linspace(6000.0, 6150.0, len(dates)),
            "SH000852": np.linspace(7000.0, 7200.0, len(dates)),
        },
        index=dates,
    )
    monkeypatch.setattr(mm, "resolve_registry", lambda: registry)
    monkeypatch.setattr(mm, "build_market_panel", lambda *args, **kwargs: panel)
    monkeypatch.setattr(mm, "attach_listing_age", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        mm, "load_index_closes", lambda *args, **kwargs: index_closes
    )

    out_dir = tmp_path / "out"
    settings = RunSettings(
        start=dates[0].strftime("%Y-%m-%d"),
        end=dates[-1].strftime("%Y-%m-%d"),
        out_dir=out_dir,
        models=("LGB", "XGB"),
        horizons=HORIZONS,
        block=10,
        n_boot=200,
        seed=7,
        n_trials=4,
    )

    summary = mm.run(settings)

    assert summary["missing"] == []
    assert summary["panel"]["signature"]["rows"] == len(dates) * len(symbols)
    assert summary["panel"]["signature"]["days"] == len(dates)
    assert summary["panel"]["signature"]["symbols"] == len(symbols)
    assert summary["panel"]["signature"]["pred_nan"] == 0
    assert summary["panel"]["signature"]["label_return_available"] is True
    assert len(summary["panel"]["first_seen_hashes"]) == 4
    assert len(summary["inventory"]) == 4
    assert summary["per_stock"] == {"rows": len(symbols), "missing": 0}
    assert summary["stats"]["5"]["quality"]["models"] == ["LGB", "XGB"]
    assert set(summary["portfolios"]["5"]) == {"top10", "top20", "rolling"}
    assert summary["cross_horizon"]["quality_rank_corr"]

    for name in (
        "summary.json",
        "inventory.csv",
        "benchmark.csv",
        "indices.csv",
        "quality_league_5.csv",
        "portfolio_league_top10_5.csv",
        "monthly_returns_rolling_10.csv",
        "deciles_5.csv",
        "cross_horizon.json",
        "per_stock_coverage.csv",
        "per_stock_latest_5.csv",
        "per_stock_latest_10.csv",
        "equity_top10_5_LGB.csv",
        "trades_top20_10_XGB.csv",
    ):
        assert (out_dir / name).is_file(), name

    per_stock = pd.read_csv(out_dir / "per_stock_coverage.csv", index_col=0)
    assert "valid_days_5_LGB" in per_stock.columns
    assert "latest_pred_10_XGB" in per_stock.columns
    assert per_stock["status"].eq("predicted").all()

    saved = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
    assert saved["manifest"]["models"] == ["LGB", "XGB"]
