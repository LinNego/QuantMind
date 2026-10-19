"""26 组合矩阵外推：把各模型预测补到最新可得交易日。

与单模型 ``extend_predictions`` 同一口径——逐日调用模型目录里的生产
``inference.py``，不复制推理逻辑。区别有三点：

1. 覆盖 ``MODEL_ORDER × HORIZONS`` 全部钉扎运行，而不是只有 T5/T10 两个样本；
2. 产物文件名带 horizon（``pred_extend_{horizon}_{model}.parquet``），
   同一模型两个 horizon 不会互相覆盖；
3. 支持按模型并发、按 (模型, 日期) 复用临时 JSON，中断后可续跑。

外推起点是各模型 ``pred.parquet`` 自己的最后交易日（T+5 与 T+10 相差一天），
终点由 ``--end`` 统一控制。逐模型日期数可能差一天，横向比较时仍应截到共同末日。

注意：训练时钉扎的 ``quantdb_dir=/tmp/quantdb_data`` 已不存在，推理会回落到
当前 ``--data-dir``。树模型在存储末日的重叠复现接近 1，深度模型会因因子数据
版本差异出现漂移；每个模型的复现校验写入 ``extend_validation_{h}_{m}.json``，
评估时以新增窗口做样本外切片，不把扩展段与训练段拼接后做跨期比较。
"""

from __future__ import annotations

import argparse
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

from .config import OUT_DIR, PRICE_END
from .data import load_calendar
from .extend_predictions import (
    _last_stored_date,
    _read_scores,
    _run_inference,
    _validate_overlap,
)
from .model_registry import HORIZONS, MODEL_ORDER, resolve_model_run
from .runner import _write_json

LOG = logging.getLogger("backtest2026.extend_matrix")

#: 并发推理时每个进程的线程预算；容器 20 核，6×3 留出余量。
DEFAULT_WORKERS = 6
INFERENCE_THREADS = 3


@dataclass(frozen=True)
class MatrixExtensionSettings:
    out_dir: Path
    end: str = PRICE_END
    models: tuple[str, ...] = MODEL_ORDER
    horizons: tuple[int, ...] = HORIZONS
    runner: str = "docker"
    workers: int = DEFAULT_WORKERS
    force: bool = False

    @property
    def tmp_dir(self) -> Path:
        return self.out_dir / "_ext_tmp_matrix"


def extension_dates(
    stored_max: pd.Timestamp, end: str, calendar: pd.DatetimeIndex
) -> list[str]:
    """严格晚于训练产物末日、且不晚于 ``end`` 的交易日。"""
    days = calendar[(calendar > stored_max) & (calendar <= pd.Timestamp(end))]
    return [day.strftime("%Y-%m-%d") for day in days]


def artifact_paths(out_dir: Path, horizon: int, model: str) -> tuple[Path, Path]:
    pred = out_dir / f"pred_extend_{horizon}_{model}.parquet"
    validation = out_dir / f"extend_validation_{horizon}_{model}.json"
    return pred, validation


def _cached_scores(path: Path, trade_date: str) -> pd.DataFrame | None:
    """读取续跑缓存的单日分数；损坏/空文件视为未缓存。"""
    if not path.is_file():
        return None
    try:
        frame = _read_scores(path, trade_date)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return frame if not frame.empty else None


def _covers(pred_path: Path, dates: list[str]) -> bool:
    if not dates or not pred_path.is_file():
        return False
    try:
        frame = pq.read_table(pred_path, columns=["trade_date"]).to_pandas()
    except (OSError, ValueError):
        return False
    available = set(pd.to_datetime(frame["trade_date"]).dt.strftime("%Y-%m-%d"))
    return set(dates).issubset(available)


def _extend_one(
    horizon: int, model: str, settings: MatrixExtensionSettings
) -> dict[str, object]:
    run_spec = resolve_model_run(horizon, model)
    if run_spec.run_dir is None:
        raise RuntimeError(f"缺少 {model} T+{horizon} 训练目录")
    run_dir = run_spec.run_dir
    stored_max = _last_stored_date(run_dir)
    calendar = load_calendar(None, settings.end)
    dates = extension_dates(stored_max, settings.end, calendar)
    pred_path, validation_path = artifact_paths(settings.out_dir, horizon, model)

    if not settings.force and _covers(pred_path, dates):
        LOG.info("%s T+%d 已覆盖至 %s，跳过", model, horizon, settings.end)
        report = _read_json(validation_path)
        return {
            "horizon": horizon,
            "model": model,
            "run_id": run_dir.name,
            "status": "skipped_existing",
            "dates": len(dates),
            "first": dates[0] if dates else None,
            "last": dates[-1] if dates else None,
            "validation": report,
            "output": str(pred_path),
        }

    frames: list[pd.DataFrame] = []
    for trade_date in dates:
        output = settings.tmp_dir / f"{horizon}_{model}_{trade_date}.json"
        cached = _cached_scores(output, trade_date)
        if cached is None:
            _run_inference(
                run_dir,
                trade_date,
                output,
                settings.runner,
                env={"OMP_NUM_THREADS": str(INFERENCE_THREADS)},
            )
            cached = _read_scores(output, trade_date)
        frames.append(cached)
    if not frames:
        raise RuntimeError(f"{model} T+{horizon} 没有可外推的交易日")
    extension = pd.concat(frames, ignore_index=True)

    validation_day = stored_max.strftime("%Y-%m-%d")
    validation_output = (
        settings.tmp_dir / f"validation_{horizon}_{model}_{validation_day}.json"
    )
    validation_frame = _cached_scores(validation_output, validation_day)
    if validation_frame is None:
        _run_inference(
            run_dir,
            validation_day,
            validation_output,
            settings.runner,
            env={"OMP_NUM_THREADS": str(INFERENCE_THREADS)},
        )
        validation_frame = _read_scores(validation_output, validation_day)
    validation = _validate_overlap(model, run_dir, validation_frame, stored_max)

    extension.to_parquet(pred_path, index=False)
    _write_json(validation_path, validation)
    return {
        "horizon": horizon,
        "model": model,
        "run_id": run_dir.name,
        "status": "ok",
        "dates": len(dates),
        "rows": int(len(extension)),
        "symbols": int(extension["symbol"].nunique()),
        "first": dates[0],
        "last": dates[-1],
        "validation_date": validation_day,
        "validation": validation,
        "output": str(pred_path),
    }


def _read_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _task_key(horizon: int, model: str) -> str:
    return f"{horizon}:{model}"


def run(settings: MatrixExtensionSettings) -> dict[str, object]:
    """并发跑完全部组合；单模型失败不阻断其余任务。"""
    settings.out_dir.mkdir(parents=True, exist_ok=True)
    settings.tmp_dir.mkdir(parents=True, exist_ok=True)
    tasks = [
        (horizon, model) for horizon in settings.horizons for model in settings.models
    ]
    LOG.info(
        "矩阵外推 %d 个组合，终点 %s，并发 %d",
        len(tasks),
        settings.end,
        settings.workers,
    )
    reports: dict[str, dict[str, object]] = {}
    failures: list[tuple[str, str]] = []
    with ThreadPoolExecutor(max_workers=max(1, settings.workers)) as pool:
        futures = {
            pool.submit(_extend_one, horizon, model, settings): (horizon, model)
            for horizon, model in tasks
        }
        for future in as_completed(futures):
            horizon, model = futures[future]
            key = _task_key(horizon, model)
            try:
                reports[key] = future.result()
            except Exception as exc:  # noqa: BLE001 - 批处理需记录全部失败
                LOG.exception("%s T+%d 外推失败", model, horizon)
                failures.append((key, str(exc)))
                reports[key] = {
                    "horizon": horizon,
                    "model": model,
                    "status": "failed",
                    "error": str(exc),
                }

    ordered = [reports[_task_key(h, m)] for h, m in tasks]
    manifest = {
        "end": settings.end,
        "runner": settings.runner,
        "models": list(settings.models),
        "horizons": list(settings.horizons),
        "tasks": ordered,
        "ok": sum(
            1 for row in ordered if row.get("status") in {"ok", "skipped_existing"}
        ),
        "failed": len(failures),
        "failures": [{"task": key, "error": error} for key, error in failures],
    }
    _write_json(settings.out_dir / "extend_matrix_manifest.json", manifest)
    LOG.info("矩阵外推完成：成功 %d，失败 %d", manifest["ok"], manifest["failed"])
    return manifest


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=OUT_DIR / "extend_matrix_20261019")
    parser.add_argument("--end", default=PRICE_END)
    parser.add_argument("--models", nargs="*", default=list(MODEL_ORDER))
    parser.add_argument("--horizons", nargs="*", type=int, default=list(HORIZONS))
    parser.add_argument("--runner", choices=["docker", "local"], default="docker")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--force", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    args = parse_args(argv)
    settings = MatrixExtensionSettings(
        out_dir=args.out,
        end=args.end,
        models=tuple(args.models),
        horizons=tuple(args.horizons),
        runner=args.runner,
        workers=args.workers,
        force=args.force,
    )
    manifest = run(settings)
    return 1 if manifest["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
