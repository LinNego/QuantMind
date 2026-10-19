"""用模型目录自带的生产推理脚本补齐 2026-08-28 ~ 2026-09-14 的逐股预测。

宿主执行（默认）：``python -m backend.scripts.backtest2026.extend_predictions``
容器内执行：``python -m backend.scripts.backtest2026.extend_predictions --runner local``

不复制推理逻辑：逐日调用 ``<model_dir>/inference.py``，与线上推理同一份代码。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from .config import (
    DATA_ROOT,
    EXTEND_END,
    EXTEND_START,
    MODELS,
    OUT_DIR,
    STORED_PRED_END,
)
from .data import load_calendar

CONTAINER = "quantmind"
CONTAINER_DATA_ROOT = "/data"
CONTAINER_REPO = "/app"


def _container_path(path: Path) -> str:
    """宿主仓库 ``data/...`` → 容器 ``/data/...``（compose 卷是 ``./data:/data``）。"""
    repo_data = (Path(__file__).resolve().parents[3] / "data").resolve()
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(repo_data)
    except ValueError as exc:
        raise ValueError(f"路径不在仓库 data/ 卷内: {path}") from exc
    return f"{CONTAINER_DATA_ROOT}/{relative.as_posix()}"


def _model_dir_in_container(model_dir: Path) -> str:
    repo = Path(__file__).resolve().parents[3]
    relative = model_dir.resolve().relative_to((repo / "data").resolve())
    return f"{CONTAINER_DATA_ROOT}/{relative.as_posix()}"


def _run_inference(model_dir: Path, trade_date: str, output: Path, runner: str) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    if runner == "docker":
        model_path = _model_dir_in_container(model_dir)
        output_path = _container_path(output)
        cmd = [
            "docker",
            "exec",
            CONTAINER,
            "bash",
            "-lc",
            f"cd {CONTAINER_REPO} && python {model_path}/inference.py "
            f"--date {trade_date} --output {output_path} --data-dir {CONTAINER_DATA_ROOT}/quantdb",
        ]
    else:
        cmd = [
            sys.executable,
            str(model_dir / "inference.py"),
            "--date",
            trade_date,
            "--output",
            str(output),
            "--data-dir",
            str(DATA_ROOT),
        ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        raise RuntimeError(
            f"推理失败 date={trade_date} exit={result.returncode}\n"
            f"stdout={result.stdout[-2000:]}\nstderr={result.stderr[-2000:]}"
        )


def _read_scores(path: Path, trade_date: str) -> pd.DataFrame:
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    frame = pd.DataFrame(payload)
    frame["trade_date"] = pd.Timestamp(trade_date)
    return frame.rename(columns={"score": "pred"})[["symbol", "trade_date", "pred"]]


def _last_stored_date(run_dir: Path) -> pd.Timestamp:
    table = pq.read_table(run_dir / "pred.parquet", columns=["trade_date"]).to_pandas()
    return pd.to_datetime(table["trade_date"]).max()


def _validate_overlap(
    model_name: str,
    run_dir: Path,
    extension: pd.DataFrame,
    validation_date: pd.Timestamp,
) -> dict:
    stored_path = run_dir / "pred.parquet"
    stored = pq.read_table(
        stored_path,
        columns=["symbol", "trade_date", "pred"],
        filters=[("trade_date", "==", validation_date.to_pydatetime())],
    ).to_pandas()
    stored["trade_date"] = pd.to_datetime(stored["trade_date"])
    merged = stored.merge(
        extension, on=["symbol", "trade_date"], suffixes=("_stored", "_ext")
    )
    if merged.empty:
        return {"model": model_name, "overlap_rows": 0, "warning": "no overlap rows"}
    corr = float(merged["pred_stored"].corr(merged["pred_ext"]))
    rank_corr = float(merged["pred_stored"].rank().corr(merged["pred_ext"].rank()))
    max_diff = float((merged["pred_stored"] - merged["pred_ext"]).abs().max())
    top100_stored = set(merged.nlargest(100, "pred_stored")["symbol"])
    top100_ext = set(merged.nlargest(100, "pred_ext")["symbol"])
    return {
        "model": model_name,
        "overlap_rows": int(len(merged)),
        "corr": corr,
        "rank_corr": rank_corr,
        "max_abs_diff": max_diff,
        "top100_overlap": len(top100_stored & top100_ext),
    }


def extend_model(model_name: str, horizon: int, run_dir: Path, runner: str) -> dict:
    stored_max = _last_stored_date(run_dir)
    calendar = load_calendar(STORED_PRED_END, EXTEND_END)
    dates = [day.strftime("%Y-%m-%d") for day in calendar if day > stored_max]
    tmp_dir = OUT_DIR / "_ext_tmp"
    frames = []
    for trade_date in dates:
        output = tmp_dir / f"{model_name}_{trade_date}.json"
        _run_inference(run_dir, trade_date, output, runner)
        frames.append(_read_scores(output, trade_date))
    extension = pd.concat(frames, ignore_index=True)

    # 用训练 pred 的最后一日做重叠复现校验（T5=08-27，T10=08-26）。
    validation_day = stored_max.strftime("%Y-%m-%d")
    validation_output = tmp_dir / f"{model_name}_{validation_day}.json"
    _run_inference(run_dir, validation_day, validation_output, runner)
    validation_frame = _read_scores(validation_output, validation_day)
    validation = _validate_overlap(model_name, run_dir, validation_frame, stored_max)

    out_path = OUT_DIR / f"pred_extend_{model_name}.parquet"
    extension.to_parquet(out_path, index=False)
    with open(
        OUT_DIR / f"extend_validation_{model_name}.json", "w", encoding="utf-8"
    ) as handle:
        json.dump(validation, handle, ensure_ascii=False, indent=2)
    return {
        "model": model_name,
        "horizon": horizon,
        "dates": len(dates),
        "rows": int(len(extension)),
        "symbols": int(extension["symbol"].nunique()),
        "first": dates[0],
        "last": dates[-1],
        "validation_date": validation_day,
        "validation": validation,
        "output": str(out_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner", choices=["docker", "local"], default="docker")
    parser.add_argument("--models", nargs="*", default=[spec.name for spec in MODELS])
    args = parser.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    summary = []
    for spec in MODELS:
        if spec.name not in args.models:
            continue
        info = extend_model(spec.name, spec.horizon, spec.run_dir, args.runner)
        summary.append(info)
        print(json.dumps(info, ensure_ascii=False))
    with open(OUT_DIR / "extend_summary.json", "w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
