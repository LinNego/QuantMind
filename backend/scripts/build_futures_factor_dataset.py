#!/usr/bin/env python3
"""构建机构级期货因子数据集（供模型训练直接读取）。

产物（默认 ``<QM_QUANTFUTURES_DATA_DIR>/6_ml_datasets/``）：

* ``futures_factors/dt=YYYYMMDD/data.parquet``：一行 = 一个品种一个交易日，
  列 = ``symbol,date,time,dt`` + OHLCV + 111 个因子（含 ``meta_*`` 标记）；
* ``futures_factors/symbol_meta.parquet``：品种板块/交易所/合约乘数；
* ``futures_factors/manifest.json``：版本、覆盖区间、行数、自检报告；
* ``futures_factors_labels/dt=YYYYMMDD/data.parquet``：前视收益标签，
  **与特征分开落盘**，避免训练脚本误把标签当特征。

口径要点：

* 主力连续按主力合约自身收益链复权，2022-09 之前用拼接乘数；
* ``basis_dom`` = 期货 - 现货（正 = 期货升水），沿用 QuantFutures 口径；
* ``carry`` > 0 = backwardation；``term_slope`` > 0 = contango；
* 库存/仓单/会员持仓收盘后公布，统一滞后 1 个交易日；
* 标签执行价 = 信号次日收盘（``execution_lag=1``）。

用法（项目根目录，必须用 ``-m`` 才能解析 ``backend`` 包）：:

    python -m backend.scripts.build_futures_factor_dataset \
        --data-root data/quantfutures \
        --out-root data/quantfutures/6_ml_datasets/futures_factors

    # 只构建某段区间（特征仍用全历史计算，避免滚动窗口开头的空值）
    ... --start 2016-01-01 --end 2026-10-21

    # 只补新分区，不重写已有文件
    ... --skip-existing
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from backend.shared.futures_factors.compute import (
    PANEL_COLUMNS,
    compute_factor_panel,
)
from backend.shared.futures_factors.labels import (
    DEFAULT_HORIZONS,
    compute_forward_labels,
    label_columns,
)
from backend.shared.futures_factors.panel import (
    DataPaths,
    calendar_from_main,
    expiry_overrides_from_meta,
    load_contract_meta,
    load_contracts,
    load_inventory,
    load_main_continuous,
    load_member_positions,
    load_spot_basis,
    load_warehouse,
)
from backend.shared.futures_factors.registry import (
    FEATURE_COLS,
    FEATURE_VERSION,
    OHLCV_COLS,
    feature_hash,
)
from backend.shared.futures_factors.verify import verify_factor_panel
from backend.shared.futures_universe import symbol_meta

__all__ = ["build_futures_factor_dataset", "write_factor_dataset"]

log = logging.getLogger("build_futures_factor_dataset")

_KEYS = ("symbol", "date", "time", "dt")
_CONVENTIONS = {
    "row": "one variety x one trading day",
    "basis_dom": "futures - spot; positive = futures premium (QuantFutures convention)",
    "carry": "annualized near-far spread; positive = backwardation",
    "term_slope": "annualized OI-weighted slope of ln(price) vs days-to-expiry; "
    "positive = contango",
    "poi_quad": "3=price up & OI up, 2=price up & OI down, "
    "1=price down & OI up, 0=price down & OI down",
    "roll_adjustment": "dominant-contract return chain anchored to the latest raw close; "
    "pre-2022-09 spliced with the first available multiplier",
    "aux_lag": "inventory / warehouse receipts / member positions are shifted 1 day",
}


def _load_optional(loader, paths: DataPaths, name: str, **kwargs) -> pd.DataFrame:
    try:
        frame = loader(paths, **kwargs)
    except FileNotFoundError:
        log.warning("%s 数据不存在，相关因子记为缺失", name)
        return pd.DataFrame()
    return frame if frame is not None else pd.DataFrame()


def _symbol_meta_frame(symbols: Sequence[str]) -> pd.DataFrame:
    records = []
    for symbol in sorted(set(symbols)):
        meta = symbol_meta(symbol)
        multiplier = meta.get("multiplier")
        records.append(
            {
                "symbol": symbol,
                "name": meta.get("name", symbol),
                "sector": meta.get("sector", "other"),
                "exchange": meta.get("exchange", ""),
                "multiplier": float(multiplier) if multiplier else np.nan,
                "unit": meta.get("unit", ""),
                "has_oi": bool(meta.get("has_oi", False)),
                "has_volume": bool(meta.get("has_volume", False)),
            }
        )
    return pd.DataFrame.from_records(records)


def _write_parquet(frame: pd.DataFrame, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f".tmp-{target.name}"
    try:
        frame.to_parquet(tmp, index=False)
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)


def _write_json(payload: dict, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f".tmp-{target.name}"
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)


def _prune_partitions(root: Path, keep: set[str]) -> int:
    """删除本次未产出的 ``dt=`` 分区（全量重建的替换语义）。

    源数据清洗会让整个日期消失（例如全天都是假期填充行）；只写不删会留下
    旧分区，盘上行数大于 manifest，假交易日继续进训练。只遍历 ``dt=*``
    目录，不碰 ``manifest.json`` / ``symbol_meta.parquet`` 等其它文件。
    """
    if not root.is_dir():
        return 0
    removed = 0
    for entry in root.iterdir():
        if not entry.is_dir() or not entry.name.startswith("dt="):
            continue
        if entry.name.split("=", 1)[1] in keep:
            continue
        shutil.rmtree(entry)
        removed += 1
    return removed


def _prepare_labels(labels: pd.DataFrame) -> pd.DataFrame:
    work = labels.copy()
    if "dt" not in work.columns:
        work["dt"] = pd.to_datetime(work["date"]).dt.strftime("%Y%m%d")
    if "time" not in work.columns:
        work["time"] = pd.to_datetime(work["date"])
    label_cols = [col for col in work.columns if col.startswith("fwd_")]
    if not label_cols:
        raise ValueError("标签表没有 fwd_* 列")
    return work[[*_KEYS, *label_cols]]


def write_factor_dataset(
    panel: pd.DataFrame,
    out_root: str | Path,
    *,
    labels: pd.DataFrame | None = None,
    labels_root: str | Path | None = None,
    feature_cols: Sequence[str] = FEATURE_COLS,
    skip_existing: bool = False,
    manifest_extra: dict | None = None,
) -> dict:
    """把已算好的面板写成按日分区，并生成 manifest。"""
    out = Path(out_root)
    required = [*_KEYS, *OHLCV_COLS]
    missing_required = [col for col in required if col not in panel.columns]
    if missing_required:
        raise ValueError(f"因子面板缺少列: {', '.join(missing_required)}")
    if panel.empty:
        raise ValueError("因子面板为空")

    clean = panel.replace([np.inf, -np.inf], np.nan).copy()
    clean["dt"] = clean["dt"].astype(str)
    report = verify_factor_panel(clean, list(feature_cols))
    if report["duplicate_keys"]:
        raise ValueError(f"因子面板主键重复 {report['duplicate_keys']} 行")
    if report["missing_columns"]:
        raise ValueError(f"因子面板缺少列: {', '.join(report['missing_columns'])}")

    written = 0
    skipped = 0
    for dt_key, group in clean.groupby("dt", sort=True):
        target = out / f"dt={dt_key}" / "data.parquet"
        if skip_existing and target.is_file():
            skipped += 1
            continue
        _write_parquet(group.reset_index(drop=True), target)
        written += 1
    pruned = 0
    if not skip_existing:
        pruned = _prune_partitions(out, set(clean["dt"].unique()))

    meta = _symbol_meta_frame(clean["symbol"].unique())
    _write_parquet(meta, out / "symbol_meta.parquet")

    labels_written = 0
    labels_skipped = 0
    labels_pruned = 0
    label_frame = None
    if labels is not None:
        if labels_root is None:
            raise ValueError("写入标签时必须提供 labels_root")
        label_frame = _prepare_labels(labels)
        labels_out = Path(labels_root)
        for dt_key, group in label_frame.groupby("dt", sort=True):
            target = labels_out / f"dt={dt_key}" / "data.parquet"
            if skip_existing and target.is_file():
                labels_skipped += 1
                continue
            _write_parquet(group.reset_index(drop=True), target)
            labels_written += 1
        if not skip_existing:
            labels_pruned = _prune_partitions(
                labels_out, set(label_frame["dt"].unique())
            )

    dates = pd.to_datetime(clean["date"])
    manifest = {
        "dataset": "futures_factors",
        "feature_version": FEATURE_VERSION,
        "feature_hash": feature_hash(),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rows": int(len(clean)),
        "symbols": int(clean["symbol"].nunique()),
        "start": dates.min().date().isoformat(),
        "end": dates.max().date().isoformat(),
        "partitions": written,
        "skipped_partitions": skipped,
        "pruned_partitions": pruned,
        "feature_count": len(feature_cols),
        "columns": list(PANEL_COLUMNS),
        "feature_cols": list(feature_cols),
        "verification": report,
        "conventions": _CONVENTIONS,
    }
    if label_frame is not None:
        horizons = sorted(
            {
                int(col.split("_")[-1].rstrip("d"))
                for col in label_frame.columns
                if col.startswith("fwd_ret_")
            }
        )
        manifest["labels"] = {
            "dir": str(labels_root),
            "rows": int(len(label_frame)),
            "partitions": labels_written,
            "skipped_partitions": labels_skipped,
            "pruned_partitions": labels_pruned,
            "horizons": horizons,
            "execution_lag": 1,
            "columns": [col for col in label_frame.columns if col.startswith("fwd_")],
            "definition": "fwd_ret_hd = close[t+1+h]/close[t+1] - 1; "
            "fwd_xs_hd = fwd_ret_hd - same-day cross-sectional mean",
        }
    if manifest_extra:
        manifest.update(manifest_extra)
    _write_json(manifest, out / "manifest.json")

    return {
        "rows": int(len(clean)),
        "symbols": int(clean["symbol"].nunique()),
        "partitions": written,
        "skipped_partitions": skipped,
        "pruned_partitions": pruned,
        "labels_rows": int(len(label_frame)) if label_frame is not None else 0,
        "labels_partitions": labels_written,
        "feature_version": FEATURE_VERSION,
        "manifest": str(out / "manifest.json"),
        "verification": report,
    }


def _slice_dates(
    frame: pd.DataFrame, start: str | None, end: str | None
) -> pd.DataFrame:
    dates = pd.to_datetime(frame["date"])
    mask = pd.Series(True, index=frame.index)
    if start is not None:
        mask &= dates >= pd.Timestamp(start)
    if end is not None:
        mask &= dates <= pd.Timestamp(end)
    return frame[mask].reset_index(drop=True)


def build_futures_factor_dataset(
    paths: DataPaths,
    out_root: str | Path,
    *,
    labels_root: str | Path | None = None,
    start: str | None = None,
    end: str | None = None,
    varieties: Sequence[str] | None = None,
    write_labels: bool = True,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
    skip_existing: bool = False,
) -> dict:
    """从 QuantFutures canonical 目录构建因子数据集。"""
    log.info("读取主力连续: %s", paths.main_continuous)
    main = load_main_continuous(paths, varieties)
    if main.empty:
        raise ValueError("daily_forward 没有可用数据")
    log.info("主力连续 %s 行 / %s 个品种", len(main), main["symbol"].nunique())

    contracts = _load_optional(
        load_contracts, paths, "contracts_daily", varieties=varieties
    )
    log.info("逐合约日线 %s 行", len(contracts))
    calendar = calendar_from_main(main)
    overrides = expiry_overrides_from_meta(load_contract_meta(paths))

    panel = compute_factor_panel(
        main,
        contracts=contracts,
        spot_basis=_load_optional(load_spot_basis, paths, "spot_basis"),
        inventory=_load_optional(load_inventory, paths, "inventory_em"),
        warehouse=_load_optional(load_warehouse, paths, "warehouse_receipts"),
        member_positions=_load_optional(
            load_member_positions, paths, "member_rank_sina"
        ),
        calendar=calendar,
        expiry_overrides=overrides,
    )
    log.info("因子面板 %s 行 x %s 列", len(panel), len(panel.columns))

    labels = None
    if write_labels:
        labels = compute_forward_labels(
            panel[["symbol", "date", "time", "close"]], horizons=horizons
        )
        log.info("前视标签 %s 行", len(labels))

    panel = _slice_dates(panel, start, end)
    if labels is not None:
        labels = _slice_dates(labels, start, end)
    if panel.empty:
        raise ValueError(f"区间 {start}..{end} 内没有因子数据")

    if labels_root is None:
        labels_root = Path(str(out_root) + "_labels")

    return write_factor_dataset(
        panel,
        out_root,
        labels=labels,
        labels_root=labels_root,
        skip_existing=skip_existing,
        manifest_extra={
            "sources": {
                "main_continuous": str(paths.main_continuous),
                "contracts_daily": str(paths.contracts_daily),
                "spot_basis": str(paths.spot_basis),
                "inventory_em": str(paths.inventory),
                "warehouse_receipts": str(paths.warehouse),
                "member_rank_sina": str(paths.member_positions),
                "contract_meta": str(paths.contract_meta),
            },
            "requested_range": {"start": start, "end": end},
            "varieties": list(varieties) if varieties else "all",
        },
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="构建期货机构级因子数据集")
    parser.add_argument(
        "--data-root",
        default=os.environ.get("QM_QUANTFUTURES_DATA_DIR", "data/quantfutures"),
        help="QuantFutures canonical 数据根目录",
    )
    parser.add_argument(
        "--out-root",
        default=None,
        help="输出目录（默认 <data-root>/6_ml_datasets/futures_factors）",
    )
    parser.add_argument(
        "--labels-root",
        default=None,
        help="标签目录（默认 <out-root>_labels）",
    )
    parser.add_argument("--start", default=None, help="起始日期 YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="结束日期 YYYY-MM-DD")
    parser.add_argument(
        "--varieties", default=None, help="只构建指定品种，逗号分隔（如 CU,AL,RB）"
    )
    parser.add_argument("--no-labels", action="store_true", help="不写前视标签")
    parser.add_argument(
        "--skip-existing", action="store_true", help="已有分区不重写（只补新日期）"
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    paths = DataPaths.from_env(args.data_root)
    out_root = (
        Path(args.out_root)
        if args.out_root
        else paths.root / "6_ml_datasets" / "futures_factors"
    )
    varieties = (
        [part.strip().upper() for part in args.varieties.split(",") if part.strip()]
        if args.varieties
        else None
    )
    result = build_futures_factor_dataset(
        paths,
        out_root,
        labels_root=args.labels_root,
        start=args.start,
        end=args.end,
        varieties=varieties,
        write_labels=not args.no_labels,
        skip_existing=args.skip_existing,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
