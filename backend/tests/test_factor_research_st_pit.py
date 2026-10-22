"""PIT ST 口径在因子研究数据层的测试（2026-10-22 审计 → B3）。

背景：``instrument_detail.IsSTGP`` 是**当日快照**。把它当历史过滤条件会同时
产生两种污染：
- 今天是 ST 的票 → 整段历史被剔除（本该只在 ST 期间剔除）；
- 曾经 ST、现在摘帽的票 → 整段历史被保留（本该在 ST 期间剔除）。
审计实测：预测 top decile 里 ST 占 8.5%，全市场 4.0%。

契约：
- ``st_mask_over`` 是全仓唯一的稀疏 PIT 掩码实现，缺失 = 非 ST；
- ``build_universe`` 的**默认**仍是静态快照口径（旧口径对照，不静默改动）；
- 显式传 ``st_pit`` 才切 PIT，且此模式下 ``is_quit`` 静态列不再参与
  （否则「现在已退市」会把退市票整段剔除 = 幸存者偏差）；
- 显式传 ``st_pit=None``（表缺失）必须抛错，不许静默退回有前视的口径。
"""

from __future__ import annotations

import pandas as pd
import pytest

from backend.services.engine.factor_research import data as frdata

DATES = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"])


def _close(n_syms: int = 2, dates=DATES, value: float = 1.0) -> pd.DataFrame:
    return pd.DataFrame(
        value, index=dates, columns=[f"{600000 + i}.SH" for i in range(n_syms)]
    )


def _sparse(pairs: list[tuple[str, str]]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": [p[0] for p in pairs],
            "time": [p[1] for p in pairs],
            "is_st": [True] * len(pairs),
        }
    )


# ───────────────────────────── st_mask_over ─────────────────────────────


def test_st_mask_over_marks_only_sparse_days():
    mask = frdata.st_mask_over(
        DATES, ["600000.SH", "000001.SZ"], _sparse([("600000.SH", "20240103")])
    )
    assert mask["600000.SH"].tolist() == [False, True, False]
    assert not mask["000001.SZ"].any()
    assert mask.to_numpy().dtype == bool


def test_st_mask_over_none_or_empty_is_all_false():
    assert not frdata.st_mask_over(DATES, ["600000.SH"], None).to_numpy().any()
    empty = pd.DataFrame(columns=["symbol", "time", "is_st"])
    assert not frdata.st_mask_over(DATES, ["600000.SH"], empty).to_numpy().any()


def test_st_mask_over_accepts_yyyymmdd_string_index():
    mask = frdata.st_mask_over(
        pd.Index(["20240102", "20240103"]),
        ["600000.SH"],
        _sparse([("600000.SH", "20240103")]),
    )
    assert mask["600000.SH"].tolist() == [False, True]


def test_st_mask_over_ignores_symbols_and_days_outside_the_panel():
    mask = frdata.st_mask_over(
        DATES,
        ["600000.SH"],
        _sparse(
            [
                ("999999.SH", "20240103"),
                ("600000.SH", "20240109"),
            ]
        ),
    )
    assert not mask.to_numpy().any()


# ─────────────────────────── build_universe ────────────────────────────


def test_build_universe_pit_excludes_only_the_st_window():
    close = _close()
    mask = frdata.build_universe(
        None,
        close,
        min_hist=1,
        st_pit=_sparse([("600000.SH", "20240103")]),
    )
    assert mask["600000.SH"].tolist() == [True, False, True]
    assert mask["600001.SH"].all()


def test_build_universe_pit_ignores_static_snapshot_flags():
    """PIT 模式下不再看静态 is_st/is_quit——退市票必须整段留在历史里。"""
    close = _close(n_syms=1)
    instr = pd.DataFrame({"symbol": ["600000.SH"], "is_st": ["1"], "is_quit": ["1"]})
    empty = pd.DataFrame(columns=["symbol", "time", "is_st"])
    mask = frdata.build_universe(instr, close, min_hist=1, st_pit=empty)
    assert mask["600000.SH"].all()


def test_build_universe_legacy_default_still_static():
    """默认口径不变（旧口径对照组；改动必须显式）。"""
    close = _close(n_syms=2)
    instr = pd.DataFrame(
        {
            "symbol": ["600000.SH", "600001.SH"],
            "is_st": ["1", "0"],
            "is_quit": ["0", "0"],
        }
    )
    mask = frdata.build_universe(instr, close, min_hist=1)
    assert not mask["600000.SH"].any()


def test_build_universe_pit_none_raises_instead_of_falling_back():
    with pytest.raises(FileNotFoundError, match="build_st_pit"):
        frdata.build_universe(None, _close(), min_hist=1, st_pit=None)


def test_build_universe_pit_mode_keeps_empty_instr_guard():
    empty_instr = pd.DataFrame(columns=["symbol", "is_st", "is_quit"])
    mask = frdata.build_universe(empty_instr, _close(), min_hist=1, st_pit=_sparse([]))
    assert not mask.to_numpy().any()


def test_build_universe_pit_does_not_resurrect_missing_history():
    """PIT 的 ST 掩码只能做减法：全无价格的票不得因「非 ST」进池。"""
    close = _close(n_syms=1)
    close.iloc[:, 0] = float("nan")
    mask = frdata.build_universe(None, close, min_hist=3, st_pit=_sparse([]))
    assert not mask["600000.SH"].any()


# ────────────────────────────── load_st_pit ────────────────────────────


def _cn_root(tmp_path, rows: list[tuple[str, str]]) -> None:
    root = tmp_path / "quantdb"
    d = root / "2_base_sector" / "st_pit"
    d.mkdir(parents=True)
    pd.DataFrame(
        {
            "symbol": [r[0] for r in rows],
            "time": [r[1] for r in rows],
            "is_st": [True] * len(rows),
        }
    ).to_parquet(d / "st_pit.parquet", index=False)
    (root / "keep").mkdir()  # 目录非空是 CN 解析的可用性判据


def test_load_st_pit_reads_sparse_table(tmp_path, monkeypatch):
    _cn_root(tmp_path, [("600000.SH", "20240103")])
    monkeypatch.setenv("QM_QUANTDB_DATA_DIR", str(tmp_path / "quantdb"))
    df = frdata.load_st_pit("CN")
    assert list(df.columns) == ["symbol", "time", "is_st"]
    assert df["symbol"].tolist() == ["600000.SH"]


def test_load_st_pit_missing_table_returns_none(tmp_path, monkeypatch):
    root = tmp_path / "quantdb"
    (root / "keep").mkdir(parents=True)
    monkeypatch.setenv("QM_QUANTDB_DATA_DIR", str(root))
    assert frdata.load_st_pit("CN") is None


def test_load_st_pit_non_cn_is_none(tmp_path, monkeypatch):
    monkeypatch.setenv("QM_QUANTHK_DATA_DIR", str(tmp_path / "quanthk"))
    assert frdata.load_st_pit("HK") is None
