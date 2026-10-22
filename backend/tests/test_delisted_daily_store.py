"""退市股日线独立仓与写回测试。

背景：``daily_forward/daily_backward/daily_unadjusted`` 属
``FULL_REWRITE_V2_DATASETS``，夜间整分区重写只含当前在市 universe，
2026-10-21 回补的 250 只退市股次日即被清空。独立仓 ``daily_*_delisted``
不在同步清单内，每次重写后按 symbol 幂等补回。

本文件锁定四条纪律：

- 独立仓构建默认 dry-run，``apply`` 才落盘，重复构建幂等；
- 写回只增不改、上游优先、symbol 唯一、主表缺失时不新建；
- 主表 schema 不符时拒绝写入，失败不破坏原文件；
- 同步钩子只在请求三张 K 线表时触发，且失败隔离。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from backend.scripts import delisted_daily_store as store

DATES = ("2016-01-04", "2016-01-05")


def _history(
    symbol: str,
    close_base: float = 10.0,
    dates: tuple[str, ...] = (DATES[0],),
) -> pd.DataFrame:
    times = pd.to_datetime(list(dates))
    n = len(times)
    return pd.DataFrame(
        {
            "symbol": [symbol] * n,
            "time": times,
            "open": [close_base + i for i in range(n)],
            "high": [close_base + i + 0.5 for i in range(n)],
            "low": [close_base + i - 0.5 for i in range(n)],
            "close": [close_base + i for i in range(n)],
            "volume": [1000.0 + i for i in range(n)],
            "amount": [10000.0 + i for i in range(n)],
            "turn": [1.0] * n,
            "pe_ttm": [10.0] * n,
            "pb": [1.0] * n,
            "ps_ttm": [2.0] * n,
            "pcf_ncf_ttm": [3.0] * n,
        }
    )


def _write_cache(cache_dir: Path, symbol: str, close_base: float = 10.0) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    raw = pd.concat(
        [_history(symbol, close_base, (dt,)) for dt in DATES], ignore_index=True
    )
    raw.to_parquet(cache_dir / f"{symbol}_raw.parquet", index=False)
    qfq = raw.copy()
    qfq[["open", "high", "low", "close"]] = qfq[["open", "high", "low", "close"]] * 1.1
    qfq.to_parquet(cache_dir / f"{symbol}_qfq.parquet", index=False)


def _write_main(data_root: Path, dataset: str, dt: str, symbols: list[str]) -> Path:
    frames = [_history(symbol) for symbol in symbols]
    frame = pd.concat(frames, ignore_index=True) if frames else _history("X")
    frame = frame[list(store.MAIN_COLUMNS)]
    path = data_root / store.DATASET_PARTS[dataset] / f"dt={dt}" / "data.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(path, index=False)
    return path


def _write_store(data_root: Path, dataset: str, dt: str, symbols: list[str]) -> Path:
    frame = pd.concat([_history(s) for s in symbols], ignore_index=True)
    frame = frame[list(store.MAIN_COLUMNS)]
    path = store.store_dataset_dir(data_root, dataset) / f"dt={dt}" / "data.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(path, index=False)
    return path


# ------------------------------------------------------------------ 构建独立仓


def test_build_store_dry_run_writes_nothing(tmp_path):
    _write_cache(tmp_path / "cache", "000001.SZ")
    _write_cache(tmp_path / "cache", "000002.SZ", 20.0)

    summary = store.build_store(tmp_path, cache_dir=tmp_path / "cache", apply=False)

    assert summary["applied"] is False
    assert summary["partitions_written"] == 0
    for dataset in store.DELISTED_DATASETS:
        assert not store.store_dataset_dir(tmp_path, dataset).exists()
    assert not Path(summary["manifest_path"]).exists()
    assert summary["symbols"] == ["000001.SZ", "000002.SZ"]
    assert summary["datasets"]["daily_unadjusted"]["partitions"] == 2
    assert summary["datasets"]["daily_unadjusted"]["rows"] == 4


def test_build_store_apply_writes_partitions_and_manifest(tmp_path):
    _write_cache(tmp_path / "cache", "000001.SZ")

    summary = store.build_store(tmp_path, cache_dir=tmp_path / "cache", apply=True)

    assert summary["partitions_written"] == 6  # 3 datasets × 2 dates
    for dataset in store.DELISTED_DATASETS:
        for dt in ("20160104", "20160105"):
            path = (
                store.store_dataset_dir(tmp_path, dataset) / f"dt={dt}" / "data.parquet"
            )
            assert path.exists()
            frame = pd.read_parquet(path)
            assert list(frame.columns) == list(store.MAIN_COLUMNS)
            assert frame["symbol"].is_unique
            assert len(frame) == 1

    manifest = json.loads(Path(summary["manifest_path"]).read_text())
    assert manifest["applied"] is True
    assert manifest["symbols"] == ["000001.SZ"]
    assert manifest["datasets"]["daily_backward"]["rows"] == 2


def test_build_store_second_apply_is_idempotent(tmp_path):
    _write_cache(tmp_path / "cache", "000001.SZ")
    first = store.build_store(tmp_path, cache_dir=tmp_path / "cache", apply=True)
    path = (
        store.store_dataset_dir(tmp_path, "daily_unadjusted")
        / "dt=20160104"
        / "data.parquet"
    )
    before = path.read_bytes()

    second = store.build_store(tmp_path, cache_dir=tmp_path / "cache", apply=True)

    assert second["partitions_written"] == 0
    assert second["partitions_unchanged"] == first["partitions_written"]
    assert path.read_bytes() == before


def test_build_store_skips_symbol_without_qfq(tmp_path):
    cache = tmp_path / "cache"
    _write_cache(cache, "000001.SZ")
    _history("000002.SZ", 20.0).to_parquet(cache / "000002.SZ_raw.parquet", index=False)

    summary = store.build_store(tmp_path, cache_dir=cache, apply=True)

    assert summary["skipped_symbols"] == ["000002.SZ"]
    assert summary["symbols"] == ["000001.SZ"]
    frame = pd.read_parquet(
        store.store_dataset_dir(tmp_path, "daily_unadjusted")
        / "dt=20160104"
        / "data.parquet"
    )
    assert set(frame["symbol"]) == {"000001.SZ"}


def test_build_store_rejects_unknown_dataset(tmp_path):
    with pytest.raises(ValueError, match="未知数据集"):
        store.build_store(
            tmp_path, cache_dir=tmp_path, datasets=("daily_forward", "index_daily")
        )


# ------------------------------------------------------------------ 写回分区


def test_inject_adds_missing_symbols_preserving_upstream(tmp_path):
    main_path = _write_main(tmp_path, "daily_unadjusted", "20160104", ["000001.SZ"])
    # 主表 000001.SZ 的 close=10，独立仓同 symbol 用 99，必须保留主表值
    _write_store(tmp_path, "daily_unadjusted", "20160104", ["000001.SZ", "000002.SZ"])
    store_path = (
        store.store_dataset_dir(tmp_path, "daily_unadjusted")
        / "dt=20160104"
        / "data.parquet"
    )
    frame = pd.read_parquet(store_path)
    frame.loc[frame["symbol"] == "000001.SZ", "close"] = 99.0
    frame.loc[frame["symbol"] == "000002.SZ", "close"] = 88.0
    frame.to_parquet(store_path, index=False)

    result = store.inject_partition(main_path, store_path)

    assert result["action"] == "added"
    assert result["added_symbols"] == 1
    assert result["added_rows"] == 1
    merged = pd.read_parquet(main_path)
    assert merged["symbol"].is_unique
    assert merged.loc[merged["symbol"] == "000001.SZ", "close"].tolist() == [10.0]
    assert merged.loc[merged["symbol"] == "000002.SZ", "close"].tolist() == [88.0]


def test_inject_partition_is_idempotent(tmp_path):
    main_path = _write_main(tmp_path, "daily_unadjusted", "20160104", ["000001.SZ"])
    _write_store(tmp_path, "daily_unadjusted", "20160104", ["000002.SZ"])
    store_path = (
        store.store_dataset_dir(tmp_path, "daily_unadjusted")
        / "dt=20160104"
        / "data.parquet"
    )

    store.inject_partition(main_path, store_path)
    rows_after_first = len(pd.read_parquet(main_path))
    result = store.inject_partition(main_path, store_path)

    assert result["action"] == "unchanged"
    assert result["added_rows"] == 0
    assert len(pd.read_parquet(main_path)) == rows_after_first


def test_inject_partition_missing_main_does_not_create(tmp_path):
    store_path = _write_store(tmp_path, "daily_unadjusted", "20160104", ["000002.SZ"])
    main_path = (
        tmp_path
        / store.DATASET_PARTS["daily_unadjusted"]
        / "dt=20160104"
        / "data.parquet"
    )

    result = store.inject_partition(main_path, store_path)

    assert result["action"] == "missing_main"
    assert not main_path.exists()


def test_inject_partition_rejects_unexpected_main_schema(tmp_path):
    main_path = _write_main(tmp_path, "daily_unadjusted", "20160104", ["000001.SZ"])
    frame = pd.read_parquet(main_path).drop(columns=["amount"])
    frame.to_parquet(main_path, index=False)
    before = main_path.read_bytes()
    store_path = _write_store(tmp_path, "daily_unadjusted", "20160104", ["000002.SZ"])

    with pytest.raises(ValueError, match="主表列不符合契约"):
        store.inject_partition(main_path, store_path)

    assert main_path.read_bytes() == before


def test_inject_partition_dry_run_does_not_write(tmp_path):
    main_path = _write_main(tmp_path, "daily_unadjusted", "20160104", ["000001.SZ"])
    before = main_path.read_bytes()
    store_path = _write_store(tmp_path, "daily_unadjusted", "20160104", ["000002.SZ"])

    result = store.inject_partition(main_path, store_path, dry_run=True)

    assert result["action"] == "added"
    assert result["added_rows"] == 1
    assert main_path.read_bytes() == before


def test_inject_partition_failure_leaves_main_intact(tmp_path, monkeypatch):
    main_path = _write_main(tmp_path, "daily_unadjusted", "20160104", ["000001.SZ"])
    before = main_path.read_bytes()
    store_path = _write_store(tmp_path, "daily_unadjusted", "20160104", ["000002.SZ"])

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(store, "_write_atomic", _boom)
    with pytest.raises(OSError, match="disk full"):
        store.inject_partition(main_path, store_path)

    assert main_path.read_bytes() == before


# ------------------------------------------------------------------ 写回全仓


def test_inject_store_since_filter(tmp_path):
    for dt in ("20160104", "20160105"):
        _write_main(tmp_path, "daily_unadjusted", dt, ["000001.SZ"])
        _write_store(tmp_path, "daily_unadjusted", dt, ["000002.SZ"])

    summary = store.inject_store(
        tmp_path,
        datasets=("daily_unadjusted",),
        since=date(2016, 1, 5),
    )

    stats = summary["datasets"]["daily_unadjusted"]
    assert stats["partitions_scanned"] == 1
    assert stats["partitions_added"] == 1
    first = pd.read_parquet(
        tmp_path
        / store.DATASET_PARTS["daily_unadjusted"]
        / "dt=20160104"
        / "data.parquet"
    )
    assert set(first["symbol"]) == {"000001.SZ"}


def test_inject_store_counts_missing_main(tmp_path):
    _write_main(tmp_path, "daily_unadjusted", "20160104", ["000001.SZ"])
    _write_store(tmp_path, "daily_unadjusted", "20160104", ["000002.SZ"])
    _write_store(tmp_path, "daily_unadjusted", "20160105", ["000002.SZ"])

    summary = store.inject_store(tmp_path, datasets=("daily_unadjusted",))

    stats = summary["datasets"]["daily_unadjusted"]
    assert stats["partitions_added"] == 1
    assert stats["missing_main"] == 1
    assert stats["errors"] == 0
    assert summary["rows_added_total"] == 1


def test_inject_store_isolates_partition_errors(tmp_path):
    _write_main(tmp_path, "daily_unadjusted", "20160104", ["000001.SZ"])
    _write_store(tmp_path, "daily_unadjusted", "20160104", ["000002.SZ"])
    # 20160105 主表 schema 坏掉：该分区记 error，不阻断前面的成功分区
    bad_main = _write_main(tmp_path, "daily_unadjusted", "20160105", ["000001.SZ"])
    pd.read_parquet(bad_main).drop(columns=["amount"]).to_parquet(bad_main, index=False)
    _write_store(tmp_path, "daily_unadjusted", "20160105", ["000002.SZ"])

    summary = store.inject_store(tmp_path, datasets=("daily_unadjusted",))

    stats = summary["datasets"]["daily_unadjusted"]
    assert stats["partitions_added"] == 1
    assert stats["errors"] == 1
    assert summary["rows_added_total"] == 1


def test_inject_store_manifest_and_rollback_restores(tmp_path):
    main_path = _write_main(tmp_path, "daily_unadjusted", "20160104", ["000001.SZ"])
    before = main_path.read_bytes()
    _write_store(tmp_path, "daily_unadjusted", "20160104", ["000002.SZ"])

    summary = store.inject_store(tmp_path, datasets=("daily_unadjusted",))

    assert summary["run_id"]
    manifest = [
        json.loads(line)
        for line in Path(summary["manifest_path"]).read_text().splitlines()
        if line.strip()
    ]
    assert len(manifest) == 1
    record = manifest[0]
    assert record["run_id"] == summary["run_id"]
    assert record["added_symbol_list"] == ["000002.SZ"]
    assert record["rows_before"] == 1
    assert record["rows_after"] == 2
    assert record["sha256_before"] and record["sha256_after"]

    preview = store.rollback_run(tmp_path, summary["run_id"], dry_run=True)
    assert preview["would_rollback"] == 1
    assert len(pd.read_parquet(main_path)) == 2

    rolled = store.rollback_run(tmp_path, summary["run_id"], dry_run=False)
    assert rolled["rolled_back"] == 1
    assert rolled["removed_rows"] == 1
    assert main_path.read_bytes() == before


def test_rollback_refuses_partition_changed_after_inject(tmp_path):
    main_path = _write_main(tmp_path, "daily_unadjusted", "20160104", ["000001.SZ"])
    _write_store(tmp_path, "daily_unadjusted", "20160104", ["000002.SZ"])
    summary = store.inject_store(tmp_path, datasets=("daily_unadjusted",))

    # 写回后模拟上游重写（内容变化）→ 回滚必须拒绝
    frame = pd.read_parquet(main_path)
    frame.loc[frame["symbol"] == "000001.SZ", "close"] = 77.0
    frame.to_parquet(main_path, index=False)
    after = main_path.read_bytes()

    stats = store.rollback_run(tmp_path, summary["run_id"], dry_run=False)

    assert stats["conflicts"] == 1
    assert stats["rolled_back"] == 0
    assert main_path.read_bytes() == after


def test_rollback_run_unknown_id_raises(tmp_path):
    _write_main(tmp_path, "daily_unadjusted", "20160104", ["000001.SZ"])
    _write_store(tmp_path, "daily_unadjusted", "20160104", ["000002.SZ"])
    store.inject_store(tmp_path, datasets=("daily_unadjusted",))

    with pytest.raises(ValueError, match="无 run_id"):
        store.rollback_run(tmp_path, "does-not-exist")


def test_inject_store_reports_store_missing(tmp_path):
    summary = store.inject_store(tmp_path, datasets=("daily_unadjusted",))

    stats = summary["datasets"]["daily_unadjusted"]
    assert stats["status"] == "store_missing"
    assert summary["rows_added_total"] == 0


def test_inject_after_sync_no_store_is_noop(tmp_path):
    summary = store.inject_after_sync(tmp_path, datasets=("daily_unadjusted",))

    assert summary["status"] == "skipped"
    assert summary["reason"] == "store_missing"
    assert not (tmp_path / store.STORE_META_REL).exists()


def test_inject_after_sync_writes_audit_history(tmp_path):
    _write_main(tmp_path, "daily_unadjusted", "20160104", ["000001.SZ"])
    _write_store(tmp_path, "daily_unadjusted", "20160104", ["000002.SZ"])

    summary = store.inject_after_sync(tmp_path, datasets=("daily_unadjusted",))

    assert summary["status"] == "ok"
    assert summary["rows_added_total"] == 1
    meta = tmp_path / store.STORE_META_REL
    last = json.loads((meta / "last_inject.json").read_text())
    assert last["rows_added_total"] == 1
    lines = [
        json.loads(line)
        for line in (meta / "inject_history.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert len(lines) == 1
    assert lines[0]["status"] == "ok"


# ------------------------------------------------------------------ 同步钩子


def _reset_sync_module(monkeypatch):
    from backend.scripts import quantdb_daily_sync as qds

    captured: dict = {}

    def _fake_sync_parquet(datasets=None, **kwargs):
        captured["datasets"] = datasets
        return {"synced": 0, "up_to_date": 0, "errors": [], "total_downloaded": 0}

    def _fake_extra(**kwargs):
        return {}

    def _fake_inject(root, datasets=None, since=None, **kwargs):
        captured["inject"] = {
            "root": root,
            "datasets": list(datasets or []),
            "since": since,
        }
        return {"status": "ok", "rows_added_total": 0}

    monkeypatch.setattr(qds, "sync_parquet", _fake_sync_parquet)
    monkeypatch.setattr(qds, "_sync_extra_sources", _fake_extra)
    # 钩子是函数内 import，必须替换源模块属性才能生效
    monkeypatch.setattr(
        "backend.scripts.delisted_daily_store.inject_after_sync", _fake_inject
    )
    return qds, captured


def test_sync_hook_runs_when_daily_dataset_requested(tmp_path, monkeypatch):
    qds, captured = _reset_sync_module(monkeypatch)
    monkeypatch.setattr(qds, "QUANTDB_DATA_DIR", tmp_path)

    result = qds.run_daily_sync(
        parquet_only=True, datasets=["daily_forward"], since=date(2026, 10, 1)
    )

    assert [d["sub_category"] for d in captured["datasets"]] == [
        "daily_forward"
    ]  # sync 收到请求数据集
    assert captured["inject"]["datasets"] == ["daily_forward"]  # 写回只处理该表
    assert captured["inject"]["since"] == date(2026, 10, 1)
    assert result["delisted_inject"] == {"status": "ok", "rows_added_total": 0}


def test_sync_hook_skips_when_no_daily_dataset_requested(tmp_path, monkeypatch):
    qds, captured = _reset_sync_module(monkeypatch)
    monkeypatch.setattr(qds, "QUANTDB_DATA_DIR", tmp_path)

    result = qds.run_daily_sync(parquet_only=True, datasets=["l1_factors"])

    assert "inject" not in captured
    assert result["delisted_inject"] is None


def test_sync_hook_failure_is_isolated(tmp_path, monkeypatch):
    qds, captured = _reset_sync_module(monkeypatch)

    def _boom(*args, **kwargs):
        raise RuntimeError("inject failed")

    monkeypatch.setattr("backend.scripts.delisted_daily_store.inject_after_sync", _boom)
    monkeypatch.setattr(qds, "QUANTDB_DATA_DIR", tmp_path)

    result = qds.run_daily_sync(parquet_only=True, datasets=["daily_forward"])

    assert result["delisted_inject"]["status"] == "error"
    assert "inject failed" in result["delisted_inject"]["reason"]


def test_sync_hook_skipped_on_dry_run(tmp_path, monkeypatch):
    qds, captured = _reset_sync_module(monkeypatch)
    monkeypatch.setattr(qds, "QUANTDB_DATA_DIR", tmp_path)

    result = qds.run_daily_sync(
        parquet_only=True, datasets=["daily_forward"], dry_run=True
    )

    assert "inject" not in captured
    assert result["delisted_inject"] is None
