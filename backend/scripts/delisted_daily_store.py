#!/usr/bin/env python3
"""退市股日线独立仓与夜间写回。

背景
----
``daily_forward/daily_backward/daily_unadjusted`` 属于
``quantdb_daily_sync.FULL_REWRITE_V2_DATASETS``，每晚从上游整分区重写，
只含当前在市 universe。2026-10-21 回补的 250 只退市股因此只能存活一晚
（次日 02:01 重写后 dt=20160104 从 2,549 行回到 2,355 行）。

本模块把退市股日线放进**独立仓**（``daily_*_delisted/dt=*/data.parquet``），
同步器永不触碰；每次全量重写后，把独立仓按 symbol 补回主表：

- **只增不改**：主表已有 symbol 一律保留主表行（上游优先）；
- **幂等**：重复执行行数不变；
- **原子写**：临时文件 + ``os.replace``，写后逐行校验上游行未被改动；
- **失败隔离**：同步钩子捕获异常，不打断整条同步；
- **缺失不造**：主表分区不存在时跳过（避免掩盖上游下载失败）。

复权口径
--------
退市股 qfq/hfq 全部来自同一来源（baostock），单只股票内部自洽；
qfq 锚点在各自退市日，不能与在市股做跨股票价格水平比较。
禁止同一 symbol 同日混源——本模块按 symbol 整段取独立仓，天然满足。

用法
----
::

    # 1) 由回补缓存构建独立仓（默认 dry-run）
    python backend/scripts/delisted_daily_store.py --build-store --apply

    # 2) 手动写回（默认 dry-run；夜间同步会自动调用）
    python backend/scripts/delisted_daily_store.py --inject --apply
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from collections.abc import Iterable, Sequence

import pandas as pd

try:  # 允许脚本方式直接执行（python backend/scripts/xxx.py）
    from backend.scripts.backfill_delisted_daily import (
        DATASET_PARTS,
        _frames_by_dataset,
    )
except ModuleNotFoundError:  # pragma: no cover - 容器内以模块方式导入
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from backend.scripts.backfill_delisted_daily import (  # type: ignore[no-redef]
        DATASET_PARTS,
        _frames_by_dataset,
    )

log = logging.getLogger("delisted_daily_store")

DELISTED_DATASETS: tuple[str, ...] = (
    "daily_unadjusted",
    "daily_forward",
    "daily_backward",
)
STORE_SUFFIX = "_delisted"
MAIN_COLUMNS: tuple[str, ...] = (
    "symbol",
    "time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
)
DEFAULT_CACHE_REL = "factor_research/selection_v2_20261021/delisted_raw"
STORE_META_REL = "factor_research/delisted_store"


# ------------------------------------------------------------------ 路径


def _validate_datasets(datasets: Iterable[str]) -> tuple[str, ...]:
    names = tuple(datasets)
    unknown = sorted(set(names) - set(DELISTED_DATASETS))
    if unknown:
        raise ValueError(f"未知数据集: {unknown}")
    if not names:
        raise ValueError("datasets 不能为空")
    return names


def default_cache_dir(data_root: Path) -> Path:
    return data_root / DEFAULT_CACHE_REL


def store_dataset_dir(data_root: str | Path, dataset: str) -> Path:
    """独立仓中某数据集的目录（与主表同构的分区布局）。"""
    _validate_datasets((dataset,))
    return Path(data_root) / f"{DATASET_PARTS[dataset]}{STORE_SUFFIX}"


def main_dataset_dir(data_root: str | Path, dataset: str) -> Path:
    _validate_datasets((dataset,))
    return Path(data_root) / DATASET_PARTS[dataset]


def _meta_dir(data_root: Path) -> Path:
    return data_root / STORE_META_REL


# ------------------------------------------------------------------ 通用


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _datetime_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _partition_date(path: Path) -> str | None:
    name = path.name
    if not name.startswith("dt=") or len(name) != 11 or not name[3:].isdigit():
        return None
    return name[3:]


def _sort_frame(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.sort_values(["symbol", "time"], kind="stable").reset_index(drop=True)


def _frames_equal(left: pd.DataFrame, right: pd.DataFrame) -> bool:
    if list(left.columns) != list(right.columns) or len(left) != len(right):
        return False
    return _sort_frame(left).equals(_sort_frame(right))


def _write_atomic(target: Path, frame: pd.DataFrame) -> str:
    """临时文件 + 读回校验 + 原子替换；返回写后 sha256。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.stem}.delisted.tmp.parquet")
    try:
        frame.to_parquet(tmp, index=False)
        check = pd.read_parquet(tmp)
        if len(check) != len(frame):
            raise RuntimeError(f"{target}: 写后行数不一致 {len(check)} != {len(frame)}")
        if not check["symbol"].is_unique:
            raise RuntimeError(f"{target}: 写后 symbol 不唯一")
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    return _sha256(target)


# ------------------------------------------------------------------ 独立仓


def _iter_cache_frames(
    cache_dir: Path,
    datasets: Sequence[str],
) -> tuple[
    dict[str, dict[str, list[pd.DataFrame]]],
    list[str],
    list[str],
]:
    """读取缓存中的 (raw, qfq) 对，按数据集/日期分组。"""
    per_dataset: dict[str, dict[str, list[pd.DataFrame]]] = {
        name: {} for name in datasets
    }
    symbols: list[str] = []
    skipped: list[str] = []
    raw_paths = sorted(cache_dir.glob("*_raw.parquet"))
    for raw_path in raw_paths:
        symbol = raw_path.name.removesuffix("_raw.parquet")
        qfq_path = cache_dir / f"{symbol}_qfq.parquet"
        if not qfq_path.exists():
            skipped.append(symbol)
            log.warning("%s: 缺 qfq 缓存，跳过", symbol)
            continue
        try:
            raw = pd.read_parquet(raw_path)
            qfq = pd.read_parquet(qfq_path)
        except Exception as exc:  # noqa: BLE001 - 单个缓存损坏不应拖垮整仓
            skipped.append(symbol)
            log.warning("%s: 缓存读取失败，跳过: %s", symbol, exc)
            continue
        frames = _frames_by_dataset(raw, qfq, datasets)
        contributed = False
        for name, frame in frames.items():
            if frame is None or frame.empty:
                continue
            frame = frame[list(MAIN_COLUMNS)]
            for dt, group in frame.groupby(
                frame["time"].dt.strftime("%Y%m%d"), sort=True
            ):
                per_dataset[name].setdefault(str(dt), []).append(group)
            contributed = True
        if contributed:
            symbols.append(symbol)
        else:
            skipped.append(symbol)
            log.warning("%s: 无可用日线，跳过", symbol)
    return per_dataset, symbols, skipped


def build_store(
    data_root: str | Path,
    *,
    cache_dir: str | Path | None = None,
    datasets: Sequence[str] = DELISTED_DATASETS,
    apply: bool = False,
    manifest_path: str | Path | None = None,
) -> dict:
    """由回补缓存构建独立仓。默认 dry-run，``apply=True`` 才落盘。

    写盘规则：每个分区按 ``symbol`` 去重、排序后原子写出；内容未变则跳过，
    因此重复执行是幂等的。
    """
    root = Path(data_root)
    names = _validate_datasets(datasets)
    cache = Path(cache_dir) if cache_dir else default_cache_dir(root)
    if not cache.exists():
        raise FileNotFoundError(f"回补缓存不存在: {cache}")

    per_dataset, symbols, skipped = _iter_cache_frames(cache, names)
    summary: dict[str, dict[str, int | str]] = {}
    written = unchanged = 0
    manifest_path = (
        Path(manifest_path)
        if manifest_path
        else _meta_dir(root) / "store_manifest.json"
    )

    for name in names:
        dates = sorted(per_dataset[name])
        rows = 0
        written_here = 0
        seen_symbols: set[str] = set()
        for dt in dates:
            merged = pd.concat(per_dataset[name][dt], ignore_index=True)
            merged = merged.drop_duplicates("symbol", keep="last")
            merged = _sort_frame(merged)
            rows += len(merged)
            seen_symbols.update(merged["symbol"].astype(str))
            target = store_dataset_dir(root, name) / f"dt={dt}" / "data.parquet"
            if apply:
                if target.exists():
                    try:
                        existing = pd.read_parquet(target)
                    except Exception:  # noqa: BLE001 - 坏文件按需重写
                        existing = None
                    if existing is not None and _frames_equal(existing, merged):
                        unchanged += 1
                        continue
                _write_atomic(target, merged)
                written += 1
                written_here += 1
        summary[name] = {
            "partitions": len(dates),
            "partitions_written": written_here if apply else 0,
            "rows": rows,
            "symbols": len(seen_symbols),
            "first_dt": dates[0] if dates else "",
            "last_dt": dates[-1] if dates else "",
        }
        log.info(
            "%s: %d 个分区 / %d 行（%s 只标的）",
            name,
            len(dates),
            rows,
            len(seen_symbols),
        )

    result = {
        "built_at": _datetime_now(),
        "applied": bool(apply),
        "data_root": str(root),
        "cache_dir": str(cache),
        "datasets": summary,
        "symbols": symbols,
        "skipped_symbols": skipped,
        "partitions_written": written,
        "partitions_unchanged": unchanged,
        "manifest_path": str(manifest_path),
    }
    if apply:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = manifest_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(tmp, manifest_path)
    log.info(
        "独立仓构建完成: applied=%s written=%d unchanged=%d skipped=%d",
        apply,
        written,
        unchanged,
        len(skipped),
    )
    return result


# ------------------------------------------------------------------ 写回


def _new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def inject_partition(
    main_path: str | Path,
    store_path: str | Path,
    *,
    dry_run: bool = False,
) -> dict:
    """把一个独立仓分区补进主表分区。

    - 主表已有 symbol 保留主表行（上游优先）；
    - 主表分区不存在 → ``missing_main``，不新建；
    - 主表 schema 不是预期 8 列 → 报错且不写（避免静默丢列）。
    """
    main_path = Path(main_path)
    store_path = Path(store_path)
    if not main_path.exists():
        return {"action": "missing_main", "added_rows": 0, "added_symbols": 0}
    if not store_path.exists():
        return {"action": "missing_main", "added_rows": 0, "added_symbols": 0}

    import pyarrow.parquet as pq

    main_columns = list(pq.read_schema(main_path).names)
    if main_columns != list(MAIN_COLUMNS):
        raise ValueError(
            f"{main_path}: 主表列不符合契约 {main_columns} != {list(MAIN_COLUMNS)}"
        )
    store_columns = list(pq.read_schema(store_path).names)
    missing = [col for col in MAIN_COLUMNS if col not in store_columns]
    if missing:
        raise ValueError(f"{store_path}: 独立仓缺列 {missing}")

    # 快路径：只读 symbol 列。绝大多数夜间分区已包含全部退市 symbol，
    # 不需要把 2.6k 个分区的全量行读进内存。
    main_symbols = pd.read_parquet(main_path, columns=["symbol"])["symbol"].astype(str)
    existing_symbols = set(main_symbols)
    store_symbols = pd.read_parquet(store_path, columns=["symbol"])["symbol"].astype(
        str
    )
    if not (set(store_symbols) - existing_symbols):
        rows = len(main_symbols)
        return {
            "action": "unchanged",
            "added_rows": 0,
            "added_symbols": 0,
            "rows_before": rows,
            "rows_after": rows,
        }

    main = pd.read_parquet(main_path)
    store = pd.read_parquet(store_path)[list(MAIN_COLUMNS)].copy()
    for col in MAIN_COLUMNS:
        if store[col].dtype != main[col].dtype:
            store[col] = store[col].astype(main[col].dtype)
    add = store[~store["symbol"].astype(str).isin(existing_symbols)]
    before_rows = len(main)
    if add.empty:
        return {
            "action": "unchanged",
            "added_rows": 0,
            "added_symbols": 0,
            "rows_before": before_rows,
            "rows_after": before_rows,
        }

    merged = pd.concat([main, add], ignore_index=True)
    if not merged["symbol"].is_unique:
        raise RuntimeError(f"{main_path}: 合并后 symbol 不唯一")
    if len(merged) != before_rows + len(add):
        raise RuntimeError(f"{main_path}: 合并行数异常")
    if (
        not merged.iloc[:before_rows]
        .reset_index(drop=True)
        .equals(main.reset_index(drop=True))
    ):
        raise RuntimeError(f"{main_path}: 合并改动了上游行")

    stats = {
        "action": "added",
        "added_rows": int(len(add)),
        "added_symbols": int(add["symbol"].nunique()),
        "added_symbol_list": sorted(add["symbol"].astype(str).unique()),
        "rows_before": before_rows,
        "rows_after": int(len(merged)),
    }
    if dry_run:
        return stats

    before_sha = _sha256(main_path)
    after_sha = _write_atomic(main_path, merged)
    check = pd.read_parquet(main_path)
    if (
        not check.iloc[:before_rows]
        .reset_index(drop=True)
        .equals(main.reset_index(drop=True))
    ):
        raise RuntimeError(f"{main_path}: 写后上游行被改动")
    stats["sha256_before"] = before_sha
    stats["sha256_after"] = after_sha
    return stats


def inject_store(
    data_root: str | Path,
    *,
    datasets: Sequence[str] = DELISTED_DATASETS,
    since: date | None = None,
    dry_run: bool = False,
    max_errors: int = 20,
    manifest_path: str | Path | None = None,
    run_id: str | None = None,
) -> dict:
    """遍历独立仓，把所有可写分区补进主表。

    每个新增分区追加一行 manifest（含 added_symbol_list 与前后 sha256），
    用于审计与精确回滚；dry-run 不写 manifest。
    """
    root = Path(data_root)
    names = _validate_datasets(datasets)
    since_dt = since.strftime("%Y%m%d") if since else None
    run_id = run_id or _new_run_id()
    manifest = (
        Path(manifest_path)
        if manifest_path
        else _meta_dir(root) / "inject_manifest.jsonl"
    )
    records: list[dict] = []
    summary: dict[str, dict] = {}
    total_added = 0
    for name in names:
        store_dir = store_dataset_dir(root, name)
        main_dir = main_dataset_dir(root, name)
        stats = {
            "partitions_scanned": 0,
            "partitions_added": 0,
            "partitions_unchanged": 0,
            "missing_main": 0,
            "errors": 0,
            "rows_added": 0,
            "symbols_added": 0,
        }
        if not store_dir.exists():
            stats["status"] = "store_missing"
            summary[name] = stats
            continue
        for part in sorted(store_dir.glob("dt=*")):
            dt = _partition_date(part)
            if dt is None:
                continue
            if since_dt and dt < since_dt:
                continue
            stats["partitions_scanned"] += 1
            store_path = part / "data.parquet"
            main_path = main_dir / part.name / "data.parquet"
            try:
                result = inject_partition(main_path, store_path, dry_run=dry_run)
            except Exception as exc:  # noqa: BLE001 - 单分区失败隔离
                stats["errors"] += 1
                log.warning("%s/%s 写回失败: %s", name, dt, exc)
                if stats["errors"] >= max_errors:
                    stats["status"] = "aborted_too_many_errors"
                    break
                continue
            action = result["action"]
            if action == "added":
                stats["partitions_added"] += 1
                stats["rows_added"] += result["added_rows"]
                stats["symbols_added"] += result["added_symbols"]
                total_added += result["added_rows"]
                records.append(
                    {
                        "run_id": run_id,
                        "ts": _datetime_now(),
                        "dataset": name,
                        "dt": dt,
                        "path": str(main_path),
                        "added_rows": result["added_rows"],
                        "added_symbol_list": result["added_symbol_list"],
                        "rows_before": result["rows_before"],
                        "rows_after": result["rows_after"],
                        "sha256_before": result.get("sha256_before"),
                        "sha256_after": result.get("sha256_after"),
                    }
                )
            elif action == "unchanged":
                stats["partitions_unchanged"] += 1
            else:
                stats["missing_main"] += 1
        stats["status"] = stats.get("status", "ok")
        summary[name] = stats
        log.info(
            "%s: 扫描 %d / 新增 %d 分区 / %d 行 / 缺主表 %d / 错误 %d",
            name,
            stats["partitions_scanned"],
            stats["partitions_added"],
            stats["rows_added"],
            stats["missing_main"],
            stats["errors"],
        )
    if records and not dry_run:
        manifest.parent.mkdir(parents=True, exist_ok=True)
        with manifest.open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return {
        "injected_at": _datetime_now(),
        "run_id": run_id,
        "dry_run": bool(dry_run),
        "since": since.isoformat() if since else None,
        "datasets": summary,
        "rows_added_total": total_added,
        "manifest_path": str(manifest) if records and not dry_run else None,
    }


def inject_after_sync(
    data_root: str | Path,
    *,
    datasets: Sequence[str] = DELISTED_DATASETS,
    since: date | None = None,
    dry_run: bool = False,
    history_path: str | Path | None = None,
) -> dict:
    """同步钩子入口：独立仓不存在时 no-op，运行后写一份审计摘要。"""
    root = Path(data_root)
    names = _validate_datasets(datasets)
    missing = [name for name in names if not store_dataset_dir(root, name).exists()]
    if len(missing) == len(names):
        return {
            "status": "skipped",
            "reason": "store_missing",
            "datasets": list(names),
            "rows_added_total": 0,
        }
    result = inject_store(root, datasets=names, since=since, dry_run=dry_run)
    result["status"] = "ok"
    result["store_missing"] = missing
    if not dry_run:
        meta = _meta_dir(root)
        meta.mkdir(parents=True, exist_ok=True)
        last_path = meta / "last_inject.json"
        tmp = last_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(tmp, last_path)
        history = Path(history_path) if history_path else meta / "inject_history.jsonl"
        with history.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")
    return result


def rollback_run(
    data_root: str | Path,
    run_id: str,
    *,
    manifest_path: str | Path | None = None,
    dry_run: bool = True,
) -> dict:
    """按 manifest 精确撤销一次写回：只移除该 run 记录的 symbol。

    若分区在写回后又被改动（行数或 sha256 与 manifest 不符）则拒绝回滚，
    交人工处理——绝不在未知状态上做删除。
    """
    root = Path(data_root)
    manifest = (
        Path(manifest_path)
        if manifest_path
        else _meta_dir(root) / "inject_manifest.jsonl"
    )
    if not manifest.exists():
        raise FileNotFoundError(f"manifest 不存在: {manifest}")
    records = [
        json.loads(line)
        for line in manifest.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    run_records = [r for r in records if r.get("run_id") == run_id]
    if not run_records:
        raise ValueError(f"manifest 中无 run_id={run_id}")
    stats = {
        "run_id": run_id,
        "dry_run": bool(dry_run),
        "partitions": len(run_records),
        "would_rollback": 0,
        "rolled_back": 0,
        "already_clean": 0,
        "conflicts": 0,
        "removed_rows": 0,
    }
    for record in run_records:
        path = Path(record["path"])
        added = set(record.get("added_symbol_list") or [])
        if not path.exists() or not added:
            stats["conflicts"] += 1
            continue
        frame = pd.read_parquet(path)
        present = set(frame["symbol"].astype(str)) & added
        if not present:
            stats["already_clean"] += 1
            continue
        if len(frame) != record.get("rows_after") or _sha256(path) != record.get(
            "sha256_after"
        ):
            stats["conflicts"] += 1
            log.warning("%s: 分区在写回后被改动，拒绝回滚", path)
            continue
        if dry_run:
            stats["would_rollback"] += 1
            continue
        out = frame[~frame["symbol"].astype(str).isin(added)].reset_index(drop=True)
        if len(out) != record.get("rows_before"):
            raise RuntimeError(
                f"{path}: 回滚后行数 {len(out)} != 记录 rows_before {record.get('rows_before')}"
            )
        _write_atomic(path, out)
        stats["rolled_back"] += 1
        stats["removed_rows"] += len(frame) - len(out)
    log.info(
        "回滚完成: run=%s dry_run=%s rolled_back=%d would=%d conflicts=%d",
        run_id,
        dry_run,
        stats["rolled_back"],
        stats["would_rollback"],
        stats["conflicts"],
    )
    return stats


# ------------------------------------------------------------------ CLI


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="退市股日线独立仓与写回")
    parser.add_argument(
        "--data-root",
        default=os.getenv("QM_QUANTDB_DATA_DIR", "/data/quantdb"),
        help="QuantDB 根目录（默认 QM_QUANTDB_DATA_DIR 或 /data/quantdb）",
    )
    parser.add_argument(
        "--cache-dir",
        default=None,
        help=f"回补缓存目录（默认 <data-root>/{DEFAULT_CACHE_REL}）",
    )
    parser.add_argument(
        "--datasets",
        default=",".join(DELISTED_DATASETS),
        help="逗号分隔的数据集：" + ",".join(DELISTED_DATASETS),
    )
    parser.add_argument("--since", default=None, help="仅处理 dt >= YYYYMMDD")
    parser.add_argument("--apply", action="store_true", help="实际写盘（默认 dry-run）")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--build-store", action="store_true", help="由缓存构建独立仓")
    action.add_argument("--inject", action="store_true", help="把独立仓写回主表")
    action.add_argument(
        "--rollback-run",
        default=None,
        metavar="RUN_ID",
        help="按 manifest 撤销一次写回",
    )
    parser.add_argument("--manifest", default=None, help="构建/写回/回滚清单路径")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    datasets = tuple(part.strip() for part in args.datasets.split(",") if part.strip())
    since = datetime.strptime(args.since, "%Y%m%d").date() if args.since else None
    if args.build_store:
        summary = build_store(
            args.data_root,
            cache_dir=args.cache_dir,
            datasets=datasets,
            apply=args.apply,
            manifest_path=args.manifest,
        )
    elif args.rollback_run:
        summary = rollback_run(
            args.data_root,
            args.rollback_run,
            manifest_path=args.manifest,
            dry_run=not args.apply,
        )
    else:
        summary = inject_after_sync(
            args.data_root,
            datasets=datasets,
            since=since,
            dry_run=not args.apply,
        )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
