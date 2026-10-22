"""期货因子数据集构建脚本。"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from backend.scripts.build_futures_factor_dataset import (
    build_futures_factor_dataset,
    write_factor_dataset,
)
from backend.shared.futures_factors.labels import DEFAULT_HORIZONS, label_columns
from backend.shared.futures_factors.panel import DataPaths
from backend.shared.futures_factors.registry import FEATURE_COLS, feature_hash

N_DAYS = 40
MINI_FEATURES = ("ret_1d", "rv_20")


def _mini_panel() -> pd.DataFrame:
    rows = []
    for symbol, base in (("CU0.CN", 70000.0), ("AL0.CN", 20000.0)):
        for i, day in enumerate(pd.bdate_range("2026-01-05", periods=3)):
            close = base + i
            rows.append(
                {
                    "symbol": symbol,
                    "date": day,
                    "time": day,
                    "dt": day.strftime("%Y%m%d"),
                    "open": close,
                    "high": close + 1,
                    "low": close - 1,
                    "close": close,
                    "volume": 10.0,
                    "amount": 10.0 * close,
                    "ret_1d": 0.01,
                    "rv_20": 0.02,
                }
            )
    return pd.DataFrame(rows)


def _mini_labels(panel: pd.DataFrame) -> pd.DataFrame:
    labels = panel[["symbol", "date", "time", "dt"]].copy()
    labels["fwd_ret_1d"] = 0.01
    labels["fwd_xs_1d"] = 0.0
    return labels


def _days() -> pd.DatetimeIndex:
    return pd.bdate_range("2025-01-02", periods=N_DAYS)


def _main() -> pd.DataFrame:
    frames = []
    for symbol, base in (("CU0.CN", 70000.0), ("AL0.CN", 20000.0)):
        for i, day in enumerate(_days()):
            close = base + i * 10.0
            frames.append(
                {
                    "symbol": symbol,
                    "time": day,
                    "open": close - 5.0,
                    "high": close + 15.0,
                    "low": close - 15.0,
                    "close": close,
                    "volume": 1000.0 + i,
                    "amount": (1000.0 + i) * close,
                }
            )
    return pd.DataFrame(frames)


def _contracts() -> pd.DataFrame:
    frames = []
    for variety, base in (("CU", 70000.0), ("AL", 20000.0)):
        for i, day in enumerate(_days()):
            for month, oi, offset in ((6, 1000.0 + i, 0.0), (9, 500.0, 100.0)):
                close = base + i * 10.0 + offset
                frames.append(
                    {
                        "symbol": f"{variety}26{month:02d}.CN",
                        "time": day,
                        "open": close - 5.0,
                        "high": close + 15.0,
                        "low": close - 15.0,
                        "close": close,
                        "volume": 500.0,
                        "amount": 500.0 * close,
                        "open_interest": oi,
                        "last_trade_date": pd.Timestamp(f"2026-{month:02d}-10"),
                        "contract_year": 2026,
                        "contract_month": month,
                        "variety": variety,
                    }
                )
    return pd.DataFrame(frames)


def _basis() -> pd.DataFrame:
    rows = []
    for variety in ("CU", "AL"):
        for i, day in enumerate(_days()):
            rows.append(
                {
                    "variety": variety,
                    "time": day,
                    "dom_basis_rate": 0.01 + i * 0.0001,
                    "near_basis_rate": 0.005 + i * 0.0001,
                    "dom_basis": 10.0 + i,
                    "near_basis": 5.0 + i,
                    "spot_price": 1000.0,
                }
            )
    return pd.DataFrame(rows)


class TestWriteFactorDataset:
    def test_writes_daily_partitions_and_manifest(self, tmp_path):
        result = write_factor_dataset(
            _mini_panel(), tmp_path, feature_cols=MINI_FEATURES
        )
        partitions = sorted(tmp_path.glob("dt=*/data.parquet"))
        assert len(partitions) == 3
        assert result["rows"] == 6
        first = pd.read_parquet(partitions[0])
        assert len(first) == 2
        assert set(MINI_FEATURES) <= set(first.columns)
        manifest = json.loads((tmp_path / "manifest.json").read_text())
        assert manifest["rows"] == 6
        assert manifest["symbols"] == 2
        assert manifest["partitions"] == 3
        assert manifest["feature_hash"] == feature_hash()

    def test_writes_symbol_meta(self, tmp_path):
        write_factor_dataset(_mini_panel(), tmp_path, feature_cols=MINI_FEATURES)
        meta = pd.read_parquet(tmp_path / "symbol_meta.parquet")
        assert set(meta["symbol"]) == {"CU0.CN", "AL0.CN"}
        assert {"sector", "exchange", "multiplier"} <= set(meta.columns)

    def test_writes_labels_to_separate_root(self, tmp_path):
        panel = _mini_panel()
        write_factor_dataset(
            panel,
            tmp_path / "features",
            labels=_mini_labels(panel),
            labels_root=tmp_path / "labels",
            feature_cols=MINI_FEATURES,
        )
        label_files = sorted((tmp_path / "labels").glob("dt=*/data.parquet"))
        assert len(label_files) == 3
        labels = pd.read_parquet(label_files[0])
        assert "fwd_ret_1d" in labels.columns
        assert "close" not in labels.columns
        assert "ret_1d" not in labels.columns

    def test_labels_require_labels_root(self, tmp_path):
        panel = _mini_panel()
        with pytest.raises(ValueError, match="labels_root"):
            write_factor_dataset(
                panel,
                tmp_path,
                labels=_mini_labels(panel),
                feature_cols=MINI_FEATURES,
            )

    def test_rejects_duplicate_keys(self, tmp_path):
        panel = pd.concat([_mini_panel(), _mini_panel().iloc[[0]]], ignore_index=True)
        with pytest.raises(ValueError, match="重复"):
            write_factor_dataset(panel, tmp_path, feature_cols=MINI_FEATURES)

    def test_rejects_missing_required_column(self, tmp_path):
        panel = _mini_panel().drop(columns=["close"])
        with pytest.raises(ValueError, match="close"):
            write_factor_dataset(panel, tmp_path, feature_cols=MINI_FEATURES)

    def test_infinities_are_written_as_nan(self, tmp_path):
        panel = _mini_panel()
        panel.loc[0, "ret_1d"] = np.inf
        write_factor_dataset(panel, tmp_path, feature_cols=MINI_FEATURES)
        stored = pd.concat(
            [pd.read_parquet(p) for p in sorted(tmp_path.glob("dt=*/data.parquet"))]
        )
        assert not np.isinf(stored["ret_1d"]).any()

    def test_full_rebuild_prunes_partitions_no_longer_produced(self, tmp_path):
        # 源数据清洗会让整日分区消失（例如全是假期填充行）。全量重建必须删掉
        # 这些旧分区，否则盘上行数 > manifest、假交易日继续进训练。
        panel = _mini_panel()
        write_factor_dataset(panel, tmp_path, feature_cols=MINI_FEATURES)
        keep = panel["dt"].unique()[0]
        write_factor_dataset(
            panel[panel["dt"] == keep], tmp_path, feature_cols=MINI_FEATURES
        )
        partitions = sorted(tmp_path.glob("dt=*/data.parquet"))
        assert len(partitions) == 1
        assert f"dt={keep}" in str(partitions[0])
        manifest = json.loads((tmp_path / "manifest.json").read_text())
        assert manifest["partitions"] == 1
        assert manifest["rows"] == 2

    def test_full_rebuild_prunes_stale_label_partitions(self, tmp_path):
        panel = _mini_panel()
        write_factor_dataset(
            panel,
            tmp_path / "features",
            labels=_mini_labels(panel),
            labels_root=tmp_path / "labels",
            feature_cols=MINI_FEATURES,
        )
        keep = panel["dt"].unique()[0]
        subset = panel[panel["dt"] == keep]
        write_factor_dataset(
            subset,
            tmp_path / "features",
            labels=_mini_labels(subset),
            labels_root=tmp_path / "labels",
            feature_cols=MINI_FEATURES,
        )
        assert len(list((tmp_path / "labels").glob("dt=*/data.parquet"))) == 1
        assert len(list((tmp_path / "features").glob("dt=*/data.parquet"))) == 1

    def test_skip_existing_does_not_prune(self, tmp_path):
        panel = _mini_panel()
        write_factor_dataset(panel, tmp_path, feature_cols=MINI_FEATURES)
        keep = panel["dt"].unique()[0]
        write_factor_dataset(
            panel[panel["dt"] == keep],
            tmp_path,
            feature_cols=MINI_FEATURES,
            skip_existing=True,
        )
        assert len(list(tmp_path.glob("dt=*/data.parquet"))) == 3

    def test_skip_existing_keeps_existing_partition(self, tmp_path):
        panel = _mini_panel()
        write_factor_dataset(panel, tmp_path, feature_cols=MINI_FEATURES)
        target = sorted(tmp_path.glob("dt=*/data.parquet"))[0]
        pd.DataFrame({"marker": [1]}).to_parquet(target, index=False)
        result = write_factor_dataset(
            panel, tmp_path, feature_cols=MINI_FEATURES, skip_existing=True
        )
        assert result["skipped_partitions"] == 3
        assert result["partitions"] == 0
        assert list(pd.read_parquet(target).columns) == ["marker"]


class TestBuildFuturesFactorDataset:
    @pytest.fixture
    def synthetic(self, monkeypatch):
        import backend.scripts.build_futures_factor_dataset as module

        empty = pd.DataFrame()
        monkeypatch.setattr(
            module, "load_main_continuous", lambda paths, varieties=None: _main()
        )
        monkeypatch.setattr(
            module,
            "load_contracts",
            lambda paths, start=None, end=None, varieties=None: _contracts(),
        )
        monkeypatch.setattr(module, "load_spot_basis", lambda paths: _basis())
        monkeypatch.setattr(module, "load_inventory", lambda paths: empty)
        monkeypatch.setattr(module, "load_warehouse", lambda paths: empty)
        monkeypatch.setattr(module, "load_member_positions", lambda paths: empty)
        monkeypatch.setattr(module, "load_contract_meta", lambda paths: pd.DataFrame())
        return module

    def test_end_to_end_outputs(self, tmp_path, synthetic):
        result = synthetic.build_futures_factor_dataset(
            DataPaths(tmp_path / "source"), tmp_path / "out"
        )
        assert result["rows"] == 2 * N_DAYS
        out = tmp_path / "out"
        assert len(list(out.glob("dt=*/data.parquet"))) == N_DAYS
        assert (out / "manifest.json").is_file()
        assert (out / "symbol_meta.parquet").is_file()

        manifest = json.loads((out / "manifest.json").read_text())
        assert manifest["feature_version"] == result["feature_version"]
        assert manifest["feature_count"] == len(FEATURE_COLS)
        assert manifest["rows"] == 2 * N_DAYS

        labels_dir = tmp_path / "out_labels"
        assert len(list(labels_dir.glob("dt=*/data.parquet"))) == N_DAYS
        labels = pd.read_parquet(sorted(labels_dir.glob("dt=*/data.parquet"))[0])
        expected = {"symbol", "date", "time", "dt", *label_columns(DEFAULT_HORIZONS)}
        assert set(labels.columns) == expected

    def test_labels_have_no_lookahead_on_tail(self, tmp_path, synthetic):
        synthetic.build_futures_factor_dataset(
            DataPaths(tmp_path / "source"), tmp_path / "out"
        )
        last_day = sorted((tmp_path / "out_labels").glob("dt=*/data.parquet"))[-1]
        labels = pd.read_parquet(last_day)
        for horizon in DEFAULT_HORIZONS:
            assert labels[f"fwd_ret_{horizon}d"].isna().all()

    def test_start_end_filter(self, tmp_path, synthetic):
        start = _days()[10].date().isoformat()
        end = _days()[20].date().isoformat()
        result = synthetic.build_futures_factor_dataset(
            DataPaths(tmp_path / "source"),
            tmp_path / "out",
            start=start,
            end=end,
            write_labels=False,
        )
        assert result["rows"] == 2 * 11
        assert len(list((tmp_path / "out").glob("dt=*/data.parquet"))) == 11
        assert not (tmp_path / "out_labels").exists()

    def test_manifest_records_verification(self, tmp_path, synthetic):
        synthetic.build_futures_factor_dataset(
            DataPaths(tmp_path / "source"), tmp_path / "out"
        )
        manifest = json.loads((tmp_path / "out" / "manifest.json").read_text())
        verification = manifest["verification"]
        assert verification["ok"] is True
        assert verification["duplicate_keys"] == 0
        assert verification["coverage"]["ret_1d"] > 0.9
