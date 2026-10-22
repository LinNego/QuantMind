"""数据集注册表**市场分层**契约（2026-10-16）。

锁死四件事：
1. CN 零行为变化：``DATASETS`` 仍是 CN 注册表、缺省访问器解析与分层前逐字一致；
2. 非 CN 市场各接本市场 l1_factors、HK 另接 ccass/south（donor→HK l1）；
3. 未接入市场（CRYPTO/CUSTOM/未知）**抛 ValueError 而非静默回退 CN**——路由层
   据此转 422；
4. HK/US/FUT 的 l1 meta_cols 剔除 10 个非因子原始列（否则市值霸榜）。
"""
from __future__ import annotations

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from backend.services.engine.factor_report import datasets as ds


def test_cn_registry_unchanged_and_alias_locked():
    assert ds.DATASETS is ds.DATASETS_BY_MARKET["CN"]
    assert ds.DEFAULT_DATASET_BY_MARKET == {
        "CN": "alpha_library",
        "HK": "l1_factors",
        "US": "l1_factors",
        "FUTURES": "l1_factors",
    }
    assert "alpha_library" in ds.dataset_names()
    assert "l2_factors" in ds.dataset_names("CN")


def test_supports_market_only_the_four_layered_markets():
    for m in ("CN", "HK", "US", "FUTURES", "hk", " futures "):
        assert ds.supports_market(m), m
    # 空值按 CN 缺省（与访问器缺省一致）；CRYPTO/CUSTOM/未知一律未接入
    assert ds.supports_market("") and ds.supports_market(None)
    for m in ("CRYPTO", "CUSTOM", "XX"):
        assert not ds.supports_market(m), m


def test_market_registries_have_expected_datasets():
    assert set(ds.datasets_for_market("HK")) == {"l1_factors", "ccass_factors", "south_factors"}
    assert set(ds.datasets_for_market("US")) == {"l1_factors"}
    assert set(ds.datasets_for_market("FUTURES")) == {
        "l1_factors",
        "futures_factors",
    }


def test_unregistered_market_raises_instead_of_falling_back_to_cn():
    with pytest.raises(ValueError, match="未接入市场"):
        ds.dataset_dir("l1_factors", "CRYPTO")
    with pytest.raises(ValueError, match="未接入市场"):
        ds.dataset_names("CRYPTO")
    with pytest.raises(ValueError, match="未接入市场"):
        ds.disk_state("l1_factors", "XX")


def test_cn_dataset_dir_matches_legacy_resolution(tmp_path, monkeypatch):
    """CN 目录 = resolve_quantdb_dir()/<dir_parts>（与分层前的 resolve_quantdb_subdir 同值）。"""
    root = tmp_path / "quantdb"
    (root / "6_ml_datasets" / "l1_factors").mkdir(parents=True)
    monkeypatch.setenv("QM_QUANTDB_DATA_DIR", str(root))

    assert ds.dataset_dir("l1_factors") == root / "6_ml_datasets" / "l1_factors"
    assert ds.dataset_dir("alpha_library", "CN") == root / "6_ml_datasets" / "alpha_library"
    assert ds.dataset_dir("rd_mined").name == "rd_mined"


def test_non_cn_dataset_dir_uses_market_root(tmp_path, monkeypatch):
    hk = tmp_path / "quanthk"
    monkeypatch.setenv("QM_QUANTHK_DATA_DIR", str(hk))

    assert ds.dataset_dir("l1_factors", "HK") == hk / "6_ml_datasets" / "l1_factors"
    assert ds.dataset_dir("ccass_factors", "HK") == hk / "6_ml_datasets" / "ccass_factors"
    assert ds.report_dir("l1_factors", "HK") == hk / "6_ml_datasets" / "l1_factors" / "report"


def test_unknown_dataset_falls_back_within_market_not_to_cn(tmp_path, monkeypatch):
    """未知数据集回落到**本市场**缺省 —— HK 上问 alpha_library 不得读到 A 股目录。"""
    hk = tmp_path / "quanthk"
    monkeypatch.setenv("QM_QUANTHK_DATA_DIR", str(hk))

    got = ds.dataset_dir("alpha_library", "HK")
    assert got == hk / "6_ml_datasets" / "l1_factors"
    assert "quantdb" not in str(got)


def test_hk_donor_datasets_point_to_l1_and_cn_behavior_stays_none():
    p = ds.donor_dir("ccass_factors", "HK")
    assert p is not None and p.parts[-2:] == ("6_ml_datasets", "l1_factors")
    p = ds.donor_dir("south_factors", "HK")
    assert p is not None and p.parts[-2:] == ("6_ml_datasets", "l1_factors")
    # HK l1 自身声明 close_fwd，不得有 donor（否则构建器会启动 donor 分支）
    assert ds.donor_dir("l1_factors", "HK") is None
    assert ds.donor_dir("l1_factors") is None


def test_l1_meta_cols_exclude_raw_market_columns():
    cn_meta = set(ds.DATASETS["l1_factors"]["meta_cols"])
    assert "total_mv" not in cn_meta  # CN l1 本来无这些列
    for market in ("HK", "US", "FUTURES"):
        cfg = ds.DATASETS_BY_MARKET[market]["l1_factors"]
        meta = set(cfg["meta_cols"])
        for col in ("open", "close", "amount"):
            assert col in meta
        for col in ("adj_factor", "turnover_rate", "pe_ttm", "pb", "roe", "bp",
                    "ep_ttm", "float_mv", "total_mv", "ln_mv_total"):
            assert col in meta, f"{market} l1 未剔除原始列 {col}"


def test_futures_institutional_meta_flags_are_not_factors():
    cfg = ds.DATASETS_BY_MARKET["FUTURES"]["futures_factors"]
    meta = set(cfg["meta_cols"])
    # 可得性标记进因子轴会让 IC 榜单变成「谁有库存数据」。
    for col in ("meta_has_oi", "meta_has_basis", "meta_roll_adjusted"):
        assert col in meta
    for col in ("symbol", "date", "dt", "open", "close", "amount"):
        assert col in meta
    assert ds.library_of("futures_factors", "carry", market="FUTURES") == (
        "futures_institutional"
    )
    # 缺省数据集保持 l1_factors，避免改变既有期货因子报告入口。
    assert ds.DEFAULT_DATASET_BY_MARKET["FUTURES"] == "l1_factors"


def test_disk_state_counts_only_factor_columns(tmp_path, monkeypatch):
    """假分区：symbol/close/10 原始列 + 2 真因子 → n_factors 必须 = 2。"""
    root = tmp_path / "quanthk"
    part = root / "6_ml_datasets" / "l1_factors" / "dt=20261014"
    part.mkdir(parents=True)
    cols = {
        "symbol": pa.array(["0001.HK"]),
        "close": pa.array([10.0]),
        "adj_factor": pa.array([1.0]),
        "total_mv": pa.array([1e9]),
        "mom_ret_5d": pa.array([0.01]),
        "vol_std_20": pa.array([0.2]),
    }
    pq.write_table(pa.table(cols), part / "data.parquet")
    monkeypatch.setenv("QM_QUANTHK_DATA_DIR", str(root))

    st = ds.disk_state("l1_factors", "HK")
    assert st == {"n_factors": 2, "first_dt": "20261014", "last_dt": "20261014"}
    assert ds.disk_state("ccass_factors", "HK")["n_factors"] is None  # 无分区


def test_library_of_market_param_keeps_cn_rules():
    assert ds.library_of("l1_factors", "anything") == "L1"
    assert ds.library_of("l1_factors", "anything", market="HK") == "L1"
    assert ds.library_of("ccass_factors", "ca_south_pct", market="HK") == "ccass"
    assert ds.library_of("rd_mined", "rd_x") == "rd_mined"
