"""自有因子库每日物化：尾窗产物的列级合并与状态工具。

设计边界：
- 生产者（factor1300/factor_defs/…）统一写到 **staging 根**，不直接碰正式库；
- 合并时以**正式库当日分区的行集为准**，只新增/覆盖列，绝不改变行数与标的集合；
- 正式分区不存在时才整日落盘（新交易日）；
- 所有写入走 ``.tmp-*.parquet`` → ``Path.replace`` 原子替换。

这样多组 ``--only`` 子集运行、长窗专项运行可以写入同一交易日而互不毁列
（``alpha_library_factors.merge_partials`` 是整日覆盖语义，不能直接用于正式库）。
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path

import pandas as pd

METADATA_COLUMNS = ("symbol", "time", "date")
#: 临时文件名必须**不以 .parquet 结尾**：读侧按 ``dt=*/*.parquet`` glob，
#: 以 .parquet 结尾的临时文件会被当成第二个分区读到（重复行/错 schema）。
TMP_NAME = ".data.parquet.tmp"


def partition_path(root: Path, dt: str) -> Path:
    return Path(root) / f"dt={dt}" / "data.parquet"


def _atomic_write(frame: pd.DataFrame, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(TMP_NAME)
    try:
        frame.to_parquet(tmp, index=False)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def backup_partition(root: Path, dt: str, backup_root: Path) -> Path | None:
    """合并前的正式分区快照（修复可回滚）；已有快照不覆盖，保留首份。"""
    source = partition_path(root, dt)
    if not source.is_file():
        return None
    target = partition_path(Path(backup_root), dt)
    if target.is_file():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(TMP_NAME)
    try:
        shutil.copy2(source, tmp)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    return target


def template_partition(template_root: Path, dt: str) -> Path | None:
    """挑一个已存在分区作为「官方 schema 模板」：优先 ``dt`` 当日或之前最近的一天。

    正式库在 dt 之前尚无分区（新库首日）时退回最早分区——模板只提供列名与
    dtype，不提供任何数值，跨日复用不会引入前视。
    """
    directories = sorted(
        p for p in Path(template_root).glob("dt=*") if p.is_dir() and p.name[3:]
    )
    if not directories:
        return None
    before = [p for p in directories if p.name[3:] <= dt]
    # 最新目录可能是 SIGKILL 留下的空壳（无 data.parquet）：继续往前找，
    # 不能因为一个残骸目录就退回 staging 的窄 schema 建新分区。
    candidates = [*reversed(before), *directories]
    for directory in candidates:
        path = directory / "data.parquet"
        if path.is_file():
            return path
    return None


def _template_frame(
    incoming: pd.DataFrame,
    template: Path,
    dt: str,
    symbols: Sequence[str] | None,
) -> pd.DataFrame:
    """按模板 schema 生成整宽分区：缺列补空、元数据列按分区日落值。

    行集优先取 ``symbols``（调用方给的当日全市场标的），staging 里多出来的
    标的追加在尾部而不是丢弃——合并只增不减。
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    schema = pq.ParquetFile(template).schema_arrow
    index = [str(s) for s in (symbols or [])]
    seen = set(index)
    for symbol in incoming.index:
        text = str(symbol)
        if text not in seen:
            seen.add(text)
            index.append(text)
    if not index:
        raise ValueError("模板建分区缺行集：symbols 与 staging 均为空")

    ts = datetime.strptime(dt, "%Y%m%d")
    arrays: list[pa.Array] = []
    for field in schema:
        if field.name == "symbol":
            arrays.append(pa.array(index, type=field.type))
        elif field.name in METADATA_COLUMNS:
            arrays.append(pa.array([ts] * len(index), type=field.type))
        elif field.name in incoming.columns:
            column = incoming[field.name].reindex(index)
            if pa.types.is_integer(field.type) and column.isna().any():
                # 整型模板列 + reindex 引入的 NaN：先转 pandas 可空整型，
                # 否则 ArrowInvalid（今无实例，防新列类型踩坑）
                arrays.append(
                    pa.array(pd.array(column, dtype="Int64"), type=field.type)
                )
            else:
                arrays.append(pa.array(column.to_numpy(), type=field.type))
        else:
            arrays.append(pa.nulls(len(index), type=field.type))

    frame = pa.Table.from_arrays(arrays, schema=schema).to_pandas()
    extras = [c for c in incoming.columns if c not in schema.names]
    for column in extras:
        # Table 的 RangeIndex 与 index 一一对应；按位置赋值保持行序一致
        frame[column] = incoming[column].reindex(index).to_numpy()
    # symbol 列与索引同值：置为索引（不能同名并存）
    frame = frame.drop(columns=["symbol"])
    frame.index = pd.Index(index, name="symbol")
    return frame


def merge_day_columns(
    staging_root: Path,
    target_root: Path,
    dt: str,
    *,
    overwrite_existing: bool = True,
    columns: list[str] | tuple[str, ...] | None = None,
    template_root: Path | None = None,
    symbols: Iterable[str] | None = None,
    skip_empty_columns: bool = False,
) -> dict[str, object]:
    """把 staging 当日分区的列合并进正式库当日分区。

    ``columns`` 给定时只合并这些列（列级修复）：正式库其余列保持原值，
    避免用当天数据商改版后的重算值覆盖历史口径。

    ``skip_empty_columns`` 为真时，staging 里整列全空的列**不参与合并**：
    尾窗补数只带有限预热天数，长窗因子（lookback 超过预热窗）在尾窗里
    结构性全空；照常合并会用 NaN 抹掉正式库里的存量值。全空列本就无可
    更新，跳过既保住历史值、也不影响新分区（模板建列同样是 NaN）。

    ``template_root`` / ``symbols`` 只在**正式分区不存在**（新交易日）时生效：
    当天分区按 ``template_root`` 里最近的官方分区生成整宽 schema（缺列补
    空、元数据列按 dt 落值），行集取 ``symbols``（当日全市场标的）并追加
    staging 多出的标的。缺这两个参数时沿用旧行为（staging 原样落盘）——
    只跑必需列的补数任务若不建模板分区，会造出窄 schema 分区，之后
    DuckDB glob 读取可能整列错位或 schema mismatch。

    返回统计：新增列数 / 覆盖列数 / 行数 / 标的中缺失的 staging 标的数。
    """
    source = partition_path(staging_root, dt)
    if not source.is_file():
        raise FileNotFoundError(f"staging 分区不存在：{source}")

    incoming = pd.read_parquet(source)
    if "symbol" not in incoming.columns:
        raise ValueError(f"staging 分区缺 symbol 列：{source}")
    if columns is not None:
        requested = list(dict.fromkeys(columns))
        metadata = [column for column in requested if column in METADATA_COLUMNS]
        if metadata:
            raise ValueError(f"元数据列不可合并：{metadata}")
        absent = [column for column in requested if column not in incoming.columns]
        if absent:
            raise ValueError(f"staging 分区缺列：{absent}（{source}）")
        incoming = incoming[["symbol", *requested]]
    incoming = incoming.drop_duplicates(subset=["symbol"], keep="last")
    incoming = incoming.set_index("symbol")
    skipped_empty: list[str] = []
    if skip_empty_columns:
        skipped_empty = [
            column
            for column in incoming.columns
            if column not in METADATA_COLUMNS and incoming[column].isna().all()
        ]
        if skipped_empty:
            incoming = incoming.drop(columns=skipped_empty)

    target = partition_path(target_root, dt)
    target_existed = target.is_file()
    template_used: Path | None = None
    if target_existed:
        current = pd.read_parquet(target)
        if "symbol" not in current.columns:
            raise ValueError(f"正式分区缺 symbol 列：{target}")
        current = current.drop_duplicates(subset=["symbol"], keep="last")
        current = current.set_index("symbol")
        base_rows = len(current)
        base_symbols = set(current.index)
        added = [c for c in incoming.columns if c not in current.columns]
        replaced = [
            c
            for c in incoming.columns
            if c in current.columns and c not in METADATA_COLUMNS
        ]
        merged = current.copy()
        # staging 未覆盖的标的**保留正式库原值**：部分标的的重算（单日补数、
        # 单批 --only）不能把其余标的整列抹成 NaN（曾复现 [1,2] + {A:5} →
        # [5, NaN]）。staging 覆盖到的标的取新值（含合法 NaN），未覆盖的取旧值。
        covered = current.index.isin(incoming.index)
        for column in replaced:
            values = (
                incoming[column].reindex(current.index).where(covered, current[column])
            )
            if values.dtype != current[column].dtype and not values.isna().any():
                try:
                    values = values.astype(current[column].dtype)
                except (TypeError, ValueError):
                    pass
            merged[column] = values
        for column in added:
            merged[column] = incoming[column].reindex(current.index)
        missing_symbols = int((~incoming.index.isin(current.index)).sum())
        if len(merged) != base_rows:
            raise AssertionError(
                f"合并改变了行数：{base_rows} -> {len(merged)}（{target}）"
            )
        if set(merged.index) != base_symbols:
            raise AssertionError(f"合并改变了标的集合：{target}")
    else:
        if template_root is not None:
            template_used = template_partition(Path(template_root), dt)
        if template_used is not None:
            merged = _template_frame(
                incoming, template_used, dt, list(symbols) if symbols else None
            )
            added = list(merged.columns)
            replaced = []
            base_rows = len(merged)
            missing_symbols = 0
        else:
            merged = incoming.copy()
            if symbols:
                universe = list(dict.fromkeys(str(s) for s in symbols))
                known = set(universe)
                universe.extend(s for s in merged.index if str(s) not in known)
                merged = merged.reindex(universe)
            added = [c for c in merged.columns if c != "symbol"]
            replaced = []
            base_rows = len(merged)
            missing_symbols = 0

    if not overwrite_existing and target.is_file():
        # 只补缺列模式：已存在列保持原值（当前未用，保留给保守回填）
        original = pd.read_parquet(target).set_index("symbol")
        for column in replaced:
            merged[column] = original[column].reindex(merged.index)

    merged = merged.reset_index()

    _atomic_write(merged, target)
    return {
        "dt": dt,
        "rows": int(base_rows),
        "added_columns": len(added),
        "replaced_columns": len(replaced),
        "incoming_symbols_missing_in_target": missing_symbols,
        "preserved_official_symbols": int((~covered).sum()) if target_existed else 0,
        "created": not target_existed,
        "template": None if template_used is None else str(template_used),
        "skipped_empty_columns": skipped_empty,
    }


def merge_days(
    staging_root: Path,
    target_root: Path,
    dts: list[str] | tuple[str, ...],
    *,
    overwrite_existing: bool = True,
) -> list[dict[str, object]]:
    return [
        merge_day_columns(
            staging_root,
            target_root,
            dt,
            overwrite_existing=overwrite_existing,
        )
        for dt in dts
    ]
