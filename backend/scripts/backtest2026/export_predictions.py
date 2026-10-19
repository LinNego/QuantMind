"""把多模型 2026 预测拼成一张逐股长表。

训练产物分散在每个 run 目录的 ``pred.parquet``。本脚本按登记表读取统一窗口，
输出 ``all_predictions_2026.parquet``（symbol, trade_date, horizon, model,
pred, label_return），供报告读者按股票、日期或模型自行下钻。

只导出已就绪组合；尚未训练完成的组合在清单里标记为 skipped，不伪造空预测。
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from .data import load_stored_predictions
from .model_registry import (
    HORIZONS,
    MODEL_ORDER,
    ModelRun,
    resolve_registry,
)

LOG = logging.getLogger("backtest2026.export_predictions")

DEFAULT_START = "2026-01-05"
DEFAULT_END = "2026-08-26"


@dataclass(frozen=True)
class ExportSettings:
    start: str = DEFAULT_START
    end: str = DEFAULT_END
    out_dir: Path = Path("/data/backtest_2026/multi_export")
    models: tuple[str, ...] = MODEL_ORDER
    horizons: tuple[int, ...] = HORIZONS


def _run_frame(
    run: ModelRun, settings: ExportSettings
) -> tuple[pd.DataFrame, bool]:
    if run.run_dir is None:
        raise FileNotFoundError(f"{run.label} 没有运行目录")
    frame = load_stored_predictions(run.run_dir, settings.start, settings.end)
    label_available = bool(frame.attrs.get("label_return_available", True))
    selected = frame[["symbol", "trade_date", "pred", "label_return"]].copy()
    selected["horizon"] = run.horizon
    selected["model"] = run.key
    return selected, label_available


def fill_label_returns(frame: pd.DataFrame) -> pd.DataFrame:
    """用同周期已落真实收益的模型补齐深度模型的标签列。

    ``label_return`` 只由 symbol / trade_date / horizon 决定，与模型无关。
    深度模型的训练产物常不落这一列；只要同一周期有任一模型（当前为树模型）
    存了真实收益，就按 (horizon, symbol, trade_date) 组内首个非空值补齐。
    不覆盖已有值，也不跨周期借标签。
    """
    if frame.empty or "label_return" not in frame.columns:
        return frame
    keys = ["horizon", "symbol", "trade_date"]
    enriched = frame.copy()
    enriched["label_return"] = enriched.groupby(keys, sort=False)[
        "label_return"
    ].transform("first")
    return enriched


def collect(
    registry: dict[int, dict[str, ModelRun]], settings: ExportSettings
) -> tuple[pd.DataFrame, list[dict[str, object]]]:
    frames = []
    diagnostics = []
    for horizon in settings.horizons:
        for key in settings.models:
            run = registry[horizon][key]
            if not run.ready:
                diagnostics.append(
                    {
                        "horizon": horizon,
                        "model": key,
                        "run_id": "",
                        "rows": 0,
                        "days": 0,
                        "symbols": 0,
                        "pred_nan": 0,
                        "label_return_available": False,
                        "label_nan": None,
                        "status": "skipped",
                        "reason": run.reason,
                    }
                )
                continue
            frame, label_available = _run_frame(run, settings)
            diagnostics.append(
                {
                    "horizon": horizon,
                    "model": key,
                    "run_id": run.run_dir.name if run.run_dir else "",
                    "rows": int(len(frame)),
                    "days": int(frame["trade_date"].nunique()),
                    "symbols": int(frame["symbol"].nunique()),
                    "pred_nan": int(frame["pred"].isna().sum()),
                    "label_return_available": label_available,
                    "label_nan": int(frame["label_return"].isna().sum()),
                    "status": "exported",
                    "reason": "",
                }
            )
            frames.append(frame)
    if not frames:
        raise RuntimeError("没有可导出的模型")
    combined = pd.concat(frames, ignore_index=True)
    combined = combined.sort_values(
        ["horizon", "model", "trade_date", "symbol"]
    ).reset_index(drop=True)
    combined = fill_label_returns(combined)
    missing = (
        combined.assign(_missing=combined["label_return"].isna())
        .groupby(["horizon", "model"], sort=False)["_missing"]
        .sum()
    )
    for row in diagnostics:
        if row["status"] != "exported":
            continue
        count = int(missing.get((row["horizon"], row["model"]), 0))
        row["label_nan"] = count
        row["label_return_complete"] = count == 0
    return combined, diagnostics


def write_outputs(
    frame: pd.DataFrame, diagnostics: list[dict[str, object]], out_dir: Path
) -> dict[str, object]:
    out_dir.mkdir(parents=True, exist_ok=True)
    parquet_path = out_dir / "all_predictions_2026.parquet"
    frame.to_parquet(parquet_path, index=False, compression="zstd")
    inventory_path = out_dir / "prediction_inventory.csv"
    pd.DataFrame(diagnostics).to_csv(inventory_path, index=False)
    summary = {
        "parquet": str(parquet_path),
        "inventory": str(inventory_path),
        "rows": int(len(frame)),
        "symbols": int(frame["symbol"].nunique()),
        "days": int(frame["trade_date"].nunique()),
        "models": sorted(frame["model"].unique().tolist()),
        "horizons": sorted(int(value) for value in frame["horizon"].unique()),
        "pred_missing": int(frame["pred"].isna().sum()),
        "label_return_missing": int(frame["label_return"].isna().sum()),
        "size_bytes": int(parquet_path.stat().st_size),
    }
    (out_dir / "export_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def run(settings: ExportSettings) -> dict[str, object]:
    registry = resolve_registry()
    frame, diagnostics = collect(registry, settings)
    summary = write_outputs(frame, diagnostics, settings.out_dir)
    LOG.info(
        "导出 %s 行、%s 只股票、%s 个模型 → %s",
        f"{summary['rows']:,}",
        f"{summary['symbols']:,}",
        len(summary["models"]),
        summary["parquet"],
    )
    return summary


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default=DEFAULT_END)
    parser.add_argument(
        "--out", type=Path, default=Path("/data/backtest_2026/multi_export")
    )
    parser.add_argument("--models", nargs="*", default=list(MODEL_ORDER))
    parser.add_argument("--horizons", nargs="*", type=int, default=list(HORIZONS))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    args = parse_args(argv)
    unknown = [key for key in args.models if key not in MODEL_ORDER]
    if unknown:
        raise SystemExit(f"未知模型: {', '.join(unknown)}")
    settings = ExportSettings(
        start=args.start,
        end=args.end,
        out_dir=args.out,
        models=tuple(args.models),
        horizons=tuple(args.horizons),
    )
    print(json.dumps(run(settings), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
