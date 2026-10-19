"""逐股预测长表导出测试。"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from backend.scripts.backtest2026.export_predictions import (
    ExportSettings,
    collect,
    write_outputs,
)
from backend.scripts.backtest2026.model_registry import ModelRun


def _run(
    key: str, horizon: int, run_dir: Path | None, *, ready: bool = True
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
        reason="" if ready else "训练中",
    )


def _write_predictions(
    run_dir: Path, *, label_return: bool, days: int = 2
) -> None:
    dates = pd.bdate_range("2026-01-05", periods=days)
    records = []
    for day in dates:
        for index, symbol in enumerate(["600000.SH", "000001.SZ"]):
            record = {
                "symbol": symbol,
                "trade_date": day,
                "pred": float(index + 1),
            }
            if label_return:
                record["label_return"] = 0.01
            records.append(record)
    pd.DataFrame(records).to_parquet(run_dir / "pred.parquet", index=False)


@pytest.mark.unit
def test_collect_exports_ready_runs_and_skips_pending(tmp_path: Path) -> None:
    t5_dir = tmp_path / "t5"
    t5_dir.mkdir()
    _write_predictions(t5_dir, label_return=True)
    t10_dir = tmp_path / "t10"
    t10_dir.mkdir()
    _write_predictions(t10_dir, label_return=False)

    registry = {
        5: {"LGB": _run("LGB", 5, t5_dir), "XGB": _run("XGB", 5, None, ready=False)},
        10: {"LGB": _run("LGB", 10, t10_dir), "XGB": _run("XGB", 10, None, ready=False)},
    }
    settings = ExportSettings(
        start="2026-01-05",
        end="2026-01-06",
        out_dir=tmp_path / "out",
        models=("LGB", "XGB"),
        horizons=(5, 10),
    )

    frame, diagnostics = collect(registry, settings)

    assert len(frame) == 8
    assert set(frame["model"]) == {"LGB"}
    assert set(frame["horizon"]) == {5, 10}
    assert frame["trade_date"].nunique() == 2
    skipped = [row for row in diagnostics if row["status"] == "skipped"]
    assert len(skipped) == 2
    assert all(row["reason"] == "训练中" for row in skipped)
    exported = [row for row in diagnostics if row["status"] == "exported"]
    assert exported[0]["label_return_available"] is True
    assert exported[1]["label_return_available"] is False
    assert exported[0]["label_return_complete"] is True
    assert exported[1]["label_return_complete"] is False


@pytest.mark.unit
def test_collect_fills_model_labels_from_same_horizon(tmp_path: Path) -> None:
    lgb_dir = tmp_path / "lgb5"
    lgb_dir.mkdir()
    _write_predictions(lgb_dir, label_return=True)
    mlp5_dir = tmp_path / "mlp5"
    mlp5_dir.mkdir()
    _write_predictions(mlp5_dir, label_return=False)
    mlp10_dir = tmp_path / "mlp10"
    mlp10_dir.mkdir()
    _write_predictions(mlp10_dir, label_return=False)

    registry = {
        5: {"LGB": _run("LGB", 5, lgb_dir), "MLP": _run("MLP", 5, mlp5_dir)},
        10: {
            "LGB": _run("LGB", 10, None, ready=False),
            "MLP": _run("MLP", 10, mlp10_dir),
        },
    }
    settings = ExportSettings(
        start="2026-01-05",
        end="2026-01-06",
        out_dir=tmp_path / "out",
        models=("LGB", "MLP"),
        horizons=(5, 10),
    )

    frame, diagnostics = collect(registry, settings)

    t5_mlp = frame[(frame["horizon"] == 5) & (frame["model"] == "MLP")]
    t10_mlp = frame[(frame["horizon"] == 10) & (frame["model"] == "MLP")]
    assert t5_mlp["label_return"].notna().all()
    assert (t5_mlp["label_return"] == 0.01).all()
    assert t10_mlp["label_return"].isna().all()
    by_key = {(row["horizon"], row["model"]): row for row in diagnostics}
    assert by_key[(5, "MLP")]["label_return_complete"] is True
    assert by_key[(10, "MLP")]["label_return_complete"] is False


@pytest.mark.unit
def test_write_outputs_roundtrip(tmp_path: Path) -> None:
    frame = pd.DataFrame(
        {
            "symbol": ["SH600000", "SH600000"],
            "trade_date": pd.to_datetime(["2026-01-05", "2026-01-06"]),
            "pred": [0.1, 0.2],
            "label_return": [0.01, 0.02],
            "horizon": [5, 5],
            "model": ["LGB", "LGB"],
        }
    )
    diagnostics = [
        {
            "horizon": 5,
            "model": "LGB",
            "run_id": "run",
            "rows": 2,
            "days": 2,
            "symbols": 1,
            "pred_nan": 0,
            "label_return_available": True,
            "label_nan": 0,
            "status": "exported",
            "reason": "",
        }
    ]

    summary = write_outputs(frame, diagnostics, tmp_path)

    saved = pd.read_parquet(tmp_path / "all_predictions_2026.parquet")
    assert list(saved.columns) == [
        "symbol",
        "trade_date",
        "pred",
        "label_return",
        "horizon",
        "model",
    ]
    assert summary["rows"] == 2
    assert summary["symbols"] == 1
    assert (tmp_path / "prediction_inventory.csv").is_file()
    assert (tmp_path / "export_summary.json").is_file()


@pytest.mark.unit
def test_collect_without_any_ready_run_raises(tmp_path: Path) -> None:
    registry = {
        5: {"LGB": _run("LGB", 5, None, ready=False)},
        10: {"LGB": _run("LGB", 10, None, ready=False)},
    }
    settings = ExportSettings(
        out_dir=tmp_path,
        models=("LGB",),
        horizons=(5, 10),
    )

    with pytest.raises(RuntimeError, match="没有可导出的模型"):
        collect(registry, settings)
