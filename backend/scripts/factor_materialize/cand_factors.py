"""cand_factors 派生：factor_defs 因子子集 + daily_backward 行情。

口径已核（2026-09-18，5566 行 × 239 因子列逐列 max|diff|=0）：
- 因子列 = factor_defs 的精确子集（列集合由 cand_factors 最新分区 schema 决定）；
- 行情列（OHLCV/amount）= ``1_kline_data/daily_backward`` 当日分区原值；
- 元数据列 ``date`` = 分区日。

库本身没有独立公式，所以不复制 factor_defs 的生产者：每天从 factor_defs
官方分区取列即可，天然与 factor_defs 同步。
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import pandas as pd

from backend.scripts.factor_materialize import library_ops as ops
from backend.scripts.factor_materialize.merge import merge_day_columns

LOG = logging.getLogger("factor_materialize.cand_factors")

STAGING_NAME = "_staging_cand_factors"


def _read(path: Path, columns: list[str] | None) -> pd.DataFrame | None:
    import pyarrow.parquet as pq

    if not path.is_file():
        return None
    present = set(pq.ParquetFile(path).schema_arrow.names)
    wanted = [c for c in (columns or ["symbol"]) if c in present]
    return pd.read_parquet(path, columns=wanted)


def build_dates(
    quantdb: Path,
    dates: list[str],
    *,
    dry_run: bool = False,
    keep_staging: bool = False,
) -> dict[str, object]:
    """逐日派生 cand_factors；返回统计（missing 源分区 / 已写入 / 缺列）。"""
    root = ops.library_root(quantdb, "cand_factors")
    defs_root = ops.library_root(quantdb, "factor_defs")
    staging = Path(quantdb) / "6_ml_datasets" / STAGING_NAME
    factor_columns = ops.factor_columns(root)
    if not factor_columns:
        raise RuntimeError(f"cand_factors 无分区，无法确定因子列：{root}")

    stats: dict[str, object] = {
        "dates": list(dates),
        "written": [],
        "planned": [],
        "missing_source": {},
        "absent_in_defs": [],
        "dry_run": dry_run,
    }
    absent: list[str] = []
    for dt in dates:
        defs_path = defs_root / f"dt={dt}" / "data.parquet"
        bw_path = (
            Path(quantdb)
            / "1_kline_data"
            / "daily_backward"
            / f"dt={dt}"
            / "data.parquet"
        )
        if not defs_path.is_file() or not bw_path.is_file():
            stats["missing_source"][dt] = {
                "factor_defs": defs_path.is_file(),
                "daily_backward": bw_path.is_file(),
            }
            continue
        if dry_run:
            stats["planned"].append(dt)
            continue

        import pyarrow.parquet as pq

        defs_schema = set(pq.ParquetFile(defs_path).schema_arrow.names)
        present = [c for c in factor_columns if c in defs_schema]
        absent = [c for c in factor_columns if c not in defs_schema]
        if absent and not stats["absent_in_defs"]:
            stats["absent_in_defs"] = absent
        factors = _read(defs_path, ["symbol", *present])
        quotes = _read(bw_path, ["symbol", *ops.OHLCV_COLUMNS])
        if factors is None or quotes is None:
            continue
        # 只合并行情分区里真实存在的列：把缺列也写进 columns 会直接抛
        # ValueError，整步失败；缺的必需列交由覆盖闸门判定。
        quote_columns = [c for c in ops.OHLCV_COLUMNS if c in quotes.columns]
        frame = quotes.merge(factors, on="symbol", how="outer")
        frame = frame.dropna(subset=["symbol"]).drop_duplicates("symbol")
        frame["date"] = pd.to_datetime(dt, format="%Y%m%d")

        staging_day = staging / f"dt={dt}"
        staging_day.mkdir(parents=True, exist_ok=True)
        frame.to_parquet(staging_day / "data.parquet", index=False)

        merge_day_columns(
            staging,
            root,
            dt,
            columns=[*quote_columns, *present],
            template_root=root,
            symbols=ops.symbols_for_date(quantdb, dt),
            skip_empty_columns=True,
        )
        stats["written"].append(dt)
        LOG.info("cand_factors %s 已写入（%d 因子列）", dt, len(present))
        if not keep_staging:
            shutil.rmtree(staging_day, ignore_errors=True)
    return stats
