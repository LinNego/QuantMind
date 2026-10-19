"""把 26 组外推预测对齐到规范模型的 (trade_date, symbol) 网格。

为什么需要对齐
--------------
``extend_matrix`` 逐组合调用各模型目录里的生产 ``inference.py``。树/线性
模型在单日路径上先做不可交易过滤（停牌、零成交、ST），再把当天全部标的
打分；深度序列模型走多日窗口路径：

- 窗口不足 ``step_len`` 的标的分数为 NaN，写盘时被剔除；
- 窗口读取不复用单日的不可交易过滤，会多出当天停牌/ST 的标的。

于是同一交易日的股票域在不同模型间相差几只（2026-08-28 实测：LGB 5194
只，TFT/GRU 5192 只，交集之外各自多出 4~6 只）。``multi_model`` 的配对
签名要求 26 个组合的日期 × 标的 × 每标的日数完全一致，否则拒绝评估——
这是正确行为，不能靠放宽签名绕过。

对齐口径
--------
规范网格取 LGB T+5（第一个组合）在评估窗内的完整面板：训练产物到
08-27，外推到 ``--end``。对每个组合：

- 网格外的行丢弃（深度模型多出的停牌标的，回测资格掩码本来也会排除）；
- 网格内缺失的 (date, symbol) 补 ``pred=NaN``（该模型当日无观点，TopK
  选择自动跳过，不冒充 0 分）；
- 原始逐日推理 JSON 保留在 ``_ext_tmp_matrix``，本脚本只改写派生的
  ``pred_extend_{h}_{m}.parquet``，可随时从 JSON 重建。

默认 dry-run，只打印/写报告；``--apply`` 才原子替换外推产物。
"""

from __future__ import annotations

import argparse
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from .config import OUT_DIR
from .data import load_extended_predictions, read_matrix_extensions
from .extend_predictions import _last_stored_date
from .model_registry import HORIZONS, MODEL_ORDER, resolve_model_run
from .runner import _write_json

LOG = logging.getLogger("backtest2026.align_extensions")

_KEY = ["trade_date", "symbol"]
_COLUMNS = ["symbol", "trade_date", "pred", "label_return", "source"]


@dataclass(frozen=True)
class AlignSettings:
    extension_dir: Path
    out_report: Path
    end: str
    start: str = "2026-08-01"
    canonical_model: str = "LGB"
    canonical_horizon: int = 5
    models: tuple[str, ...] = MODEL_ORDER
    horizons: tuple[int, ...] = HORIZONS
    apply: bool = False


def align_extension_frame(
    frame: pd.DataFrame, grid: pd.DataFrame
) -> tuple[pd.DataFrame, dict[str, int]]:
    """按规范网格对齐单组合外推预测。

    ``grid`` 只需含 ``trade_date`` / ``symbol`` 两列；重复网格行会去重。
    返回值第二项是审计计数（新增、丢弃、NaN 等）。
    """
    grid_keys = grid[_KEY].drop_duplicates()
    clean = frame.copy()
    if not clean.empty:
        clean["trade_date"] = pd.to_datetime(clean["trade_date"])
    for column in ("pred", "label_return", "source"):
        if column not in clean.columns:
            clean[column] = pd.NA if column != "source" else "extended"
    clean = clean[_COLUMNS].drop_duplicates(subset=_KEY, keep="last")

    before_keys = clean[_KEY]
    added = int(
        (
            grid_keys.merge(before_keys, on=_KEY, how="left", indicator=True)["_merge"]
            == "left_only"
        ).sum()
    )
    dropped = int(
        (
            before_keys.merge(grid_keys, on=_KEY, how="left", indicator=True)["_merge"]
            == "left_only"
        ).sum()
    )

    aligned = grid_keys.merge(clean, on=_KEY, how="left")
    aligned["source"] = aligned["source"].fillna("aligned_missing")
    aligned = aligned.sort_values(_KEY).reset_index(drop=True)
    stats = {
        "rows_before": int(len(clean)),
        "rows_after": int(len(aligned)),
        "added": added,
        "dropped": dropped,
        "pred_nan": int(aligned["pred"].isna().sum()),
    }
    return aligned[_COLUMNS], stats


def _canonical_grid(settings: AlignSettings) -> pd.DataFrame:
    run = resolve_model_run(settings.canonical_horizon, settings.canonical_model)
    if run.run_dir is None:
        raise RuntimeError(
            f"缺少规范模型 {settings.canonical_model} "
            f"T+{settings.canonical_horizon} 的运行目录"
        )
    frame = load_extended_predictions(
        run.run_dir,
        settings.canonical_horizon,
        settings.canonical_model,
        settings.start,
        settings.end,
        settings.extension_dir,
    )
    if frame.empty:
        raise RuntimeError("规范模型面板为空，无法构建外推网格")
    grid = frame[_KEY].drop_duplicates().sort_values(_KEY).reset_index(drop=True)
    return grid


def run(settings: AlignSettings) -> dict[str, object]:
    grid = _canonical_grid(settings)
    LOG.info(
        "规范网格 %s ~ %s：%d 个 (date, symbol)，%d 个交易日",
        grid["trade_date"].min().date(),
        grid["trade_date"].max().date(),
        len(grid),
        grid["trade_date"].nunique(),
    )
    entries: list[dict[str, object]] = []
    totals = {"added": 0, "dropped": 0, "pred_nan": 0, "files": 0, "missing": 0}
    for horizon in settings.horizons:
        for model in settings.models:
            run_spec = resolve_model_run(horizon, model)
            if run_spec.run_dir is None:
                entries.append(
                    {
                        "horizon": horizon,
                        "model": model,
                        "status": "missing_run_dir",
                    }
                )
                totals["missing"] += 1
                continue
            path = settings.extension_dir / f"pred_extend_{horizon}_{model}.parquet"
            if not path.is_file():
                entries.append(
                    {"horizon": horizon, "model": model, "status": "missing_file"}
                )
                totals["missing"] += 1
                continue
            stored_max = _last_stored_date(run_spec.run_dir)
            model_grid = grid[grid["trade_date"] > stored_max]
            if model_grid.empty:
                entries.append(
                    {
                        "horizon": horizon,
                        "model": model,
                        "status": "no_extension_dates",
                        "stored_max": stored_max.strftime("%Y-%m-%d"),
                    }
                )
                continue
            frame = read_matrix_extensions(settings.extension_dir, horizon, model)
            aligned, stats = align_extension_frame(frame, model_grid)
            entry = {
                "horizon": horizon,
                "model": model,
                "status": "applied" if settings.apply else "dry_run",
                "stored_max": stored_max.strftime("%Y-%m-%d"),
                "grid_rows": int(len(model_grid)),
                **stats,
            }
            entries.append(entry)
            totals["added"] += stats["added"]
            totals["dropped"] += stats["dropped"]
            totals["pred_nan"] += stats["pred_nan"]
            totals["files"] += 1
            if settings.apply:
                tmp = path.with_suffix(".parquet.tmp")
                aligned.to_parquet(tmp, index=False)
                os.replace(tmp, path)
            LOG.info(
                "%s T+%d 对齐：行 %d→%d，新增 %d，丢弃 %d，NaN %d%s",
                model,
                horizon,
                stats["rows_before"],
                stats["rows_after"],
                stats["added"],
                stats["dropped"],
                stats["pred_nan"],
                "" if settings.apply else "（dry-run）",
            )
    manifest = {
        "extension_dir": str(settings.extension_dir),
        "start": settings.start,
        "end": settings.end,
        "canonical": (f"{settings.canonical_model} T+{settings.canonical_horizon}"),
        "grid_rows": int(len(grid)),
        "grid_days": int(grid["trade_date"].nunique()),
        "applied": settings.apply,
        "totals": totals,
        "entries": entries,
    }
    _write_json(settings.out_report, manifest)
    LOG.info(
        "对齐完成：%d 个文件，新增 %d，丢弃 %d，NaN %d，缺失 %d",
        totals["files"],
        totals["added"],
        totals["dropped"],
        totals["pred_nan"],
        totals["missing"],
    )
    return manifest


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dir",
        type=Path,
        required=True,
        help="extend_matrix 产物目录",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="审计报告路径；默认写到产物目录 align_extensions_report.json",
    )
    parser.add_argument("--start", default="2026-08-01")
    parser.add_argument("--end", default="2026-10-16")
    parser.add_argument("--canonical-model", default="LGB")
    parser.add_argument("--canonical-horizon", type=int, default=5)
    parser.add_argument("--models", nargs="*", default=list(MODEL_ORDER))
    parser.add_argument("--horizons", nargs="*", type=int, default=list(HORIZONS))
    parser.add_argument(
        "--apply",
        action="store_true",
        help="原子替换外推 parquet；缺省只做 dry-run",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    args = parse_args(argv)
    report = args.out or args.dir / "align_extensions_report.json"
    settings = AlignSettings(
        extension_dir=args.dir,
        out_report=report,
        end=args.end,
        start=args.start,
        canonical_model=args.canonical_model,
        canonical_horizon=args.canonical_horizon,
        models=tuple(args.models),
        horizons=tuple(args.horizons),
        apply=args.apply,
    )
    run(settings)


if __name__ == "__main__":
    main()
