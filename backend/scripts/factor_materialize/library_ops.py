"""自有因子库尾窗物化的库级运行时：日历、分区、标的集与备份。

只做「读目录状态 / 读当日标的」，不执行任何生产者；执行与合并编排见
``backend/scripts/materialize_library_tail.py``。
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

import pandas as pd

LOG = logging.getLogger("factor_materialize.library_ops")

#: 分区元数据列（不同库用 time 或 date 二选一）
METADATA_COLUMNS = ("symbol", "time", "date")
#: 行情列（gap_mined / cand_factors 自带；合并时只并因子列，行情以正式库为准）
OHLCV_COLUMNS = ("open", "high", "low", "close", "volume", "amount")
REPAIR_BACKUP_DIR = "_repair_backup"


def quantdb_root() -> Path:
    return Path(os.environ.get("QM_QUANTDB_DATA_DIR") or "/data/quantdb")


def custom_root() -> Path:
    return Path(os.environ.get("QM_QUANTCUSTOM_DATA_DIR") or "/data/quantcustom")


def library_root(base: Path, library: str) -> Path:
    return Path(base) / "6_ml_datasets" / library


def partition_dates(root: Path) -> list[str]:
    """有 ``data.parquet`` 的分区日期（升序）。空壳目录不算（SIGKILL 残骸）。"""
    root = Path(root)
    if not root.is_dir():
        return []
    return sorted(
        p.name[3:]
        for p in root.glob("dt=*")
        if p.is_dir() and (p / "data.parquet").is_file()
    )


def latest_partition(root: Path) -> str | None:
    dates = partition_dates(root)
    return dates[-1] if dates else None


def previous_partition(root: Path, dt: str) -> str | None:
    """``dt`` 之前最近的正式分区（标的完整性对照用）。"""
    dates = [date for date in partition_dates(root) if date < dt]
    return dates[-1] if dates else None


def calendar(quantdb: Path, *, target: str | None = None) -> list[str]:
    """交易日历 = daily_forward 分区日期；``target`` 给定时截到该日（含）。"""
    dates = partition_dates(Path(quantdb) / "1_kline_data" / "daily_forward")
    if target:
        dates = [dt for dt in dates if dt <= target]
    return dates


def target_dates(
    root: Path,
    trading_days: list[str],
    *,
    target: str,
    since: str | None = None,
) -> list[str]:
    """需要（重）物化的日期。

    默认只补「最新正式分区之后」的缺口；``since`` 给定时改为闭区间内全部
    交易日——这是显式回填/修复语义（已有分区也重算，调用方决定是否备份）。
    """
    if since:
        return [dt for dt in trading_days if since <= dt <= target]
    latest = latest_partition(root)
    if latest is None:
        return [dt for dt in trading_days if dt <= target]
    return [dt for dt in trading_days if latest < dt <= target]


def symbols_for_date(quantdb: Path, dt: str) -> list[str]:
    """当日全市场标的（suffix 式，来自 daily_backward 分区）。"""
    path = (
        Path(quantdb) / "1_kline_data" / "daily_backward" / f"dt={dt}" / "data.parquet"
    )
    if not path.is_file():
        return []
    frame = pd.read_parquet(path, columns=["symbol"])
    return [str(s) for s in frame["symbol"].dropna().unique()]


def factor_columns(root: Path, *, dt: str | None = None) -> list[str]:
    """库的因子列（去掉元数据与行情列）：默认取最新分区 schema。"""
    dates = partition_dates(root)
    if not dates:
        return []
    chosen = dt if dt in dates else dates[-1]
    import pyarrow.parquet as pq

    names = pq.ParquetFile(
        Path(root) / f"dt={chosen}" / "data.parquet"
    ).schema_arrow.names
    excluded = set(METADATA_COLUMNS) | set(OHLCV_COLUMNS)
    return [str(name) for name in names if name not in excluded]


def stash_partition(root: Path, dt: str) -> Path | None:
    """把正式分区移到 ``_repair_backup``（原子改名），供生产者重建该日。

    生产者「已有分区跳过」无法强制重算；重建前先备份原分区，失败可人工
    回滚。同一天重复备份时追加时间戳后缀，绝不覆盖上一份备份。
    """
    src = Path(root) / f"dt={dt}"
    if not src.is_dir():
        return None
    backup_root = Path(root) / REPAIR_BACKUP_DIR
    backup_root.mkdir(parents=True, exist_ok=True)
    dst = backup_root / f"dt={dt}"
    if dst.exists():
        stamp = pd.Timestamp.now("UTC").strftime("%Y%m%dT%H%M%S")
        dst = backup_root / f"dt={dt}.{stamp}"
    shutil.move(str(src), str(dst))
    LOG.info("已备份既有分区 %s → %s", src, dst)
    return dst


def clear_staging(staging: Path, dates: list[str]) -> None:
    """清掉 staging 中本次要重算的日期，避免生产者「已有分区跳过」。

    清理失败必须抛出：残留分区会让生产者跳过该日，旧列被原样合并进正式库。
    """
    for dt in dates:
        target = Path(staging) / f"dt={dt}"
        if target.is_dir():
            shutil.rmtree(target)


def verify_dates(
    root: Path,
    dates: list[str],
    *,
    required_columns: list[str] | None = None,
) -> dict[str, object]:
    """覆盖率检查：日期分区是否存在、必需列是否至少有一个非空值。"""
    import pyarrow.parquet as pq

    missing = []
    empty_columns: dict[str, list[str]] = {}
    for dt in dates:
        path = Path(root) / f"dt={dt}" / "data.parquet"
        if not path.is_file():
            missing.append(dt)
            continue
        if not required_columns:
            continue
        present = [
            c
            for c in required_columns
            if c in set(pq.ParquetFile(path).schema_arrow.names)
        ]
        if not present:
            continue
        frame = pd.read_parquet(path, columns=present)
        empties = [c for c in present if frame[c].isna().all()]
        if empties:
            empty_columns[dt] = empties
    return {
        "missing_dates": missing,
        "empty_columns": empty_columns,
        "ok": not missing,
    }
