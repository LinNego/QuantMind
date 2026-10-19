"""预测外推测试：重叠复现校验与逐日调度（不调用真实推理）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from backend.scripts.backtest2026 import extend_predictions as ext

SYMBOLS = ["SH600000", "SH600001", "SH600002"]


def _write_stored(run_dir, dates: list[str]) -> None:
    rows = []
    for trade_date in dates:
        for index, symbol in enumerate(SYMBOLS):
            rows.append(
                {
                    "symbol": symbol,
                    "trade_date": pd.Timestamp(trade_date),
                    "pred": float(index),
                    "label_return": 0.01,
                }
            )
    run_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(run_dir / "pred.parquet", index=False)


@pytest.mark.unit
def test_read_scores_maps_score_column_to_pred(tmp_path) -> None:
    path = tmp_path / "scores.json"
    path.write_text(
        json.dumps([{"symbol": "SH600000", "score": 1.5}]), encoding="utf-8"
    )
    frame = ext._read_scores(path, "2026-08-27")
    assert frame["pred"].tolist() == [1.5]
    assert frame["trade_date"].iloc[0] == pd.Timestamp("2026-08-27")


@pytest.mark.unit
def test_last_stored_date_uses_max_trade_date(tmp_path) -> None:
    run_dir = tmp_path / "run"
    _write_stored(run_dir, ["2026-08-25", "2026-08-27", "2026-08-26"])
    assert ext._last_stored_date(run_dir) == pd.Timestamp("2026-08-27")


@pytest.mark.unit
def test_validate_overlap_reports_full_top100_overlap(tmp_path) -> None:
    run_dir = tmp_path / "run"
    _write_stored(run_dir, ["2026-08-26"])
    extension = pd.DataFrame(
        {
            "symbol": SYMBOLS,
            "trade_date": pd.Timestamp("2026-08-26"),
            "pred": [0.0, 1.0, 2.0],
        }
    )
    report = ext._validate_overlap("T5", run_dir, extension, pd.Timestamp("2026-08-26"))
    assert report["overlap_rows"] == 3
    assert report["corr"] == pytest.approx(1.0)
    assert report["top100_overlap"] == 3


@pytest.mark.unit
def test_container_path_maps_repo_data_volume() -> None:
    repo = Path(__file__).resolve().parents[2]
    assert ext._container_path(repo / "data" / "backtest_2026" / "x.parquet") == (
        "/data/backtest_2026/x.parquet"
    )
    with pytest.raises(ValueError):
        ext._container_path(repo / "backend" / "x.py")


@pytest.mark.unit
def test_extend_model_schedules_dates_after_stored_max_and_validates(
    tmp_path, monkeypatch
) -> None:
    run_dir = tmp_path / "run"
    _write_stored(run_dir, ["2026-08-26"])
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    monkeypatch.setattr(ext, "OUT_DIR", out_dir)
    monkeypatch.setattr(
        ext,
        "load_calendar",
        lambda start, end: pd.DatetimeIndex(["2026-08-26", "2026-08-27", "2026-08-28"]),
    )
    calls: list[str] = []

    def fake_run_inference(model_dir, trade_date, output, runner):
        calls.append(trade_date)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(
                [
                    {"symbol": symbol, "score": float(index)}
                    for index, symbol in enumerate(SYMBOLS)
                ]
            ),
            encoding="utf-8",
        )

    monkeypatch.setattr(ext, "_run_inference", fake_run_inference)
    info = ext.extend_model("T5", 5, run_dir, "docker")

    assert calls == ["2026-08-27", "2026-08-28", "2026-08-26"]
    assert info["dates"] == 2
    assert info["rows"] == 6
    assert info["validation"]["corr"] == pytest.approx(1.0)
    assert (out_dir / "pred_extend_T5.parquet").exists()


@pytest.mark.unit
def test_validate_overlap_warns_when_no_matching_rows(tmp_path) -> None:
    run_dir = tmp_path / "run"
    _write_stored(run_dir, ["2026-08-26"])
    extension = pd.DataFrame(
        {
            "symbol": ["SZ000001"],
            "trade_date": pd.Timestamp("2026-08-26"),
            "pred": [1.0],
        }
    )
    report = ext._validate_overlap("T5", run_dir, extension, pd.Timestamp("2026-08-26"))
    assert report["overlap_rows"] == 0
    assert "warning" in report


@pytest.mark.unit
def test_run_inference_builds_docker_and_local_commands(tmp_path, monkeypatch) -> None:
    repo = Path(__file__).resolve().parents[2]
    model_dir = repo / "data" / "training_jobs" / "run"
    output = repo / "data" / "backtest_2026" / "scores.json"
    captured: list[list[str]] = []

    def fake_run(cmd, capture_output, text, timeout):
        captured.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(ext.subprocess, "run", fake_run)
    ext._run_inference(model_dir, "2026-08-27", output, "docker")
    ext._run_inference(model_dir, "2026-08-27", output, "local")

    assert captured[0][:3] == ["docker", "exec", ext.CONTAINER]
    assert "/data/training_jobs/run/inference.py" in captured[0][-1]
    assert captured[1][1].endswith("inference.py")
    assert "--data-dir" in captured[1]


@pytest.mark.unit
def test_run_inference_passes_thread_env_to_docker_and_local(
    tmp_path, monkeypatch
) -> None:
    repo = Path(__file__).resolve().parents[2]
    model_dir = repo / "data" / "training_jobs" / "run"
    output = repo / "data" / "backtest_2026" / "scores.json"
    captured: list[tuple[list[str], dict | None]] = []

    def fake_run(cmd, capture_output, text, timeout, env=None):
        captured.append((cmd, env))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(ext.subprocess, "run", fake_run)
    ext._run_inference(
        model_dir, "2026-08-27", output, "docker", env={"OMP_NUM_THREADS": "3"}
    )
    ext._run_inference(
        model_dir, "2026-08-27", output, "local", env={"OMP_NUM_THREADS": "3"}
    )

    docker_cmd = captured[0][0]
    assert docker_cmd[:2] == ["docker", "exec"]
    assert "OMP_NUM_THREADS=3" in docker_cmd
    assert captured[0][1] is None
    assert captured[1][1]["OMP_NUM_THREADS"] == "3"


@pytest.mark.unit
def test_run_inference_raises_with_stderr_on_failure(tmp_path, monkeypatch) -> None:
    repo = Path(__file__).resolve().parents[2]
    monkeypatch.setattr(
        ext.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1, stdout="out", stderr="boom"
        ),
    )
    with pytest.raises(RuntimeError, match="boom"):
        ext._run_inference(
            repo / "data" / "training_jobs" / "run",
            "2026-08-27",
            repo / "data" / "backtest_2026" / "scores.json",
            "local",
        )


@pytest.mark.unit
def test_main_writes_summary(tmp_path, monkeypatch) -> None:
    model = SimpleNamespace(name="T5", horizon=5, run_dir=tmp_path / "run")
    monkeypatch.setattr(ext, "OUT_DIR", tmp_path)
    monkeypatch.setattr(ext, "MODELS", (model,))
    monkeypatch.setattr(ext, "extend_model", lambda *args: {"model": "T5"})
    monkeypatch.setattr(sys, "argv", ["extend", "--models", "T5"])

    ext.main()

    summary = json.loads((tmp_path / "extend_summary.json").read_text(encoding="utf-8"))
    assert summary == [{"model": "T5"}]
