"""13 模型 × 2 周期的运行登记表测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.scripts.backtest2026.model_registry import (
    HORIZONS,
    MODEL_ORDER,
    PINNED_RUN_IDS,
    _ready_reason,
    inventory_rows,
    pins_for_horizons,
    resolve_model_run,
    resolve_registry,
)


def _make_run(
    root: Path,
    run_id: str,
    horizon: int,
    key: str,
    *,
    metadata: bool = True,
    predictions: bool = True,
    job_name: str | None = None,
    target: int | None = None,
) -> Path:
    run_dir = root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    if metadata:
        payload = {
            "job_name": job_name or f"T{horizon}_Cross300_{key}_Split20261018",
            "target_horizon_days": horizon if target is None else target,
            "model_type": key.lower(),
        }
        (run_dir / "metadata.json").write_text(json.dumps(payload), encoding="utf-8")
    if predictions:
        (run_dir / "pred.parquet").write_bytes(b"")
    return run_dir


@pytest.mark.unit
def test_discovery_selects_latest_completed_run(tmp_path: Path) -> None:
    _make_run(tmp_path, "train_20260101000000_aaaaaaaa", 5, "LGB")
    latest = _make_run(tmp_path, "train_20260102000000_bbbbbbbb", 5, "LGB")

    run = resolve_model_run(5, "LGB", root=tmp_path, use_pins=False)

    assert run.run_dir == latest
    assert run.ready is True
    assert run.reason == ""
    assert run.label == "LGB · T+5"


@pytest.mark.unit
def test_discovery_ignores_other_horizon_and_other_model(tmp_path: Path) -> None:
    _make_run(tmp_path, "train_20260101000000_aaaaaaaa", 10, "LGB")
    _make_run(tmp_path, "train_20260102000000_bbbbbbbb", 5, "XGB")

    run = resolve_model_run(5, "LGB", root=tmp_path, use_pins=False)

    assert run.run_dir is None
    assert run.ready is False
    assert run.reason == "尚未找到训练任务"


@pytest.mark.unit
def test_ready_requires_metadata(tmp_path: Path) -> None:
    run_dir = _make_run(
        tmp_path, "train_20260101000000_aaaaaaaa", 5, "LGB", metadata=False
    )
    ready, reason, metadata = _ready_reason(run_dir, 5, "LGB")
    assert ready is False
    assert metadata == {}
    assert "metadata" in reason


@pytest.mark.unit
def test_ready_requires_predictions(tmp_path: Path) -> None:
    _make_run(
        tmp_path,
        "train_20260101000000_aaaaaaaa",
        5,
        "LGB",
        predictions=False,
    )
    run = resolve_model_run(5, "LGB", root=tmp_path, use_pins=False)
    assert run.ready is False
    assert "pred.parquet" in run.reason


@pytest.mark.unit
def test_ready_rejects_horizon_mismatch(tmp_path: Path) -> None:
    run_dir = _make_run(
        tmp_path,
        "train_20260101000000_aaaaaaaa",
        5,
        "LGB",
        target=10,
    )
    ready, reason, _ = _ready_reason(run_dir, 5, "LGB")
    assert ready is False
    assert "target_horizon_days" in reason


@pytest.mark.unit
def test_ready_rejects_job_name_mismatch(tmp_path: Path) -> None:
    run_dir = _make_run(
        tmp_path,
        "train_20260101000000_aaaaaaaa",
        5,
        "LGB",
        job_name="T5_Cross300_XGB_Split20261018",
    )
    ready, reason, _ = _ready_reason(run_dir, 5, "LGB")
    assert ready is False
    assert "job_name" in reason


@pytest.mark.unit
def test_registry_with_all_runs_is_complete(tmp_path: Path) -> None:
    for horizon in HORIZONS:
        for index, key in enumerate(MODEL_ORDER):
            run_id = f"train_2026{horizon:02d}0100{index:04d}_{key.lower():0>8}"
            _make_run(tmp_path, run_id, horizon, key)

    registry = resolve_registry(root=tmp_path, require_all=True, use_pins=False)

    assert set(registry) == set(HORIZONS)
    for horizon in HORIZONS:
        assert tuple(registry[horizon]) == MODEL_ORDER
        assert all(run.ready for run in registry[horizon].values())
    rows = inventory_rows(registry)
    assert len(rows) == len(HORIZONS) * len(MODEL_ORDER)
    assert {row["horizon"] for row in rows} == set(HORIZONS)


@pytest.mark.unit
def test_registry_require_all_raises_when_incomplete(tmp_path: Path) -> None:
    _make_run(tmp_path, "train_20260101000000_aaaaaaaa", 5, "LGB")

    with pytest.raises(RuntimeError, match="模型尚未就绪"):
        resolve_registry(root=tmp_path, require_all=True, use_pins=False)


@pytest.mark.unit
def test_registry_accepts_extra_horizon_bucket(tmp_path: Path) -> None:
    for horizon in (5, 10, 20):
        _make_run(tmp_path, f"train_2026100{horizon}000000_aaaaaaaa", horizon, "LGB")

    registry = resolve_registry(
        root=tmp_path,
        use_pins=False,
        horizons=(5, 10, 20),
    )

    assert set(registry) == {5, 10, 20}
    assert registry[20]["LGB"].ready is True
    assert registry[20]["LGB"].label == "LGB · T+20"
    rows = inventory_rows(registry)
    assert {row["horizon"] for row in rows} == {5, 10, 20}


@pytest.mark.unit
def test_pins_for_default_horizons_does_not_pull_t20_subset() -> None:
    assert pins_for_horizons((5, 10)) == PINNED_RUN_IDS


@pytest.mark.unit
def test_pinned_registry_covers_all_combinations() -> None:
    assert set(PINNED_RUN_IDS) == {
        (horizon, key) for horizon in HORIZONS for key in MODEL_ORDER
    }
    assert all(
        isinstance(run_id, str) and run_id.startswith("train_")
        for run_id in PINNED_RUN_IDS.values()
    )
