"""PIT ST 表构建（backend/scripts/build_st_pit.py）的纯函数与编排测试。

口径要点（2026-10-22 审计驱动）：
- 稀疏表只存 is_st=True 的行；未命中 = 非 ST（与「不做 ST 过滤」等价，绝不误杀）；
- 覆盖水印单独走 journal：全期非 ST 的票与退市票的 parquet 不足以判断
  「查到哪天了」，缺了它每轮都会重抓；
- 单符号抓取失败只记录，不中断全量；
- 代码格式在边界转换：缓存/产物用 QuantDB 后缀式，baostock 调用用 ``sh.600000``。
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from backend.scripts import build_st_pit as mod


# ------------------------------------------------------------------ 代码转换


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("SH600000", "sh.600000"),
        ("600000.SH", "sh.600000"),
        ("SZ000001", "sz.000001"),
        ("000001.SZ", "sz.000001"),
        ("BJ430047", "bj.430047"),
    ],
)
def test_symbol_to_baostock_normalizes_both_forms(given, expected):
    assert mod.symbol_to_baostock(given) == expected


def test_symbol_to_baostock_rejects_bare_digits():
    with pytest.raises(ValueError, match="无法识别"):
        mod.symbol_to_baostock("600000")


def test_baostock_to_symbol_roundtrip():
    assert mod.baostock_to_symbol("sh.600000") == "600000.SH"
    assert mod.baostock_to_symbol(mod.symbol_to_baostock("300750.SZ")) == "300750.SZ"


# ------------------------------------------------------------------ 行解析


def test_rows_to_frame_keeps_only_st_days_and_normalizes_time():
    rows = [
        ["2024-01-02", "sh.600000", "0"],
        ["2024-01-03", "sh.600000", "1"],
        ["2024-01-04", "sh.600000", "1"],
    ]
    frame = mod.rows_to_frame(rows)
    assert list(frame.columns) == ["symbol", "time", "is_st"]
    assert frame["symbol"].tolist() == ["600000.SH", "600000.SH"]
    assert frame["time"].tolist() == ["20240103", "20240104"]
    assert frame["is_st"].all()


def test_rows_to_frame_drops_unknown_flags_and_short_rows():
    rows = [
        ["2024-01-02", "sh.600000", ""],
        ["2024-01-03", "sh.600000", "2"],
        ["2024-01-04"],
    ]
    assert mod.rows_to_frame(rows).empty


def test_rows_to_frame_dedups_repeated_days():
    rows = [["2024-01-03", "sh.600000", "1"], ["2024-01-03", "sh.600000", "1"]]
    assert len(mod.rows_to_frame(rows)) == 1


def test_rows_to_frame_falls_back_to_passed_symbol():
    frame = mod.rows_to_frame([["2024-01-03", "", "1"]], symbol="600000.SH")
    assert frame["symbol"].tolist() == ["600000.SH"]


# ------------------------------------------------------------------ 稀疏帧合并


def test_merge_frames_unions_and_new_wins():
    old = pd.DataFrame({"symbol": ["600000.SH"], "time": ["20240103"], "is_st": [True]})
    new = pd.DataFrame(
        {
            "symbol": ["600000.SH", "600000.SH"],
            "time": ["20240103", "20240104"],
            "is_st": [True, True],
        }
    )
    merged = mod.merge_frames(old, new)
    assert merged["time"].tolist() == ["20240103", "20240104"]


def test_merge_frames_handles_empty_inputs():
    empty = pd.DataFrame(columns=["symbol", "time", "is_st"])
    assert mod.merge_frames(None, empty).empty
    assert list(mod.merge_frames(empty, empty).columns) == ["symbol", "time", "is_st"]


# ------------------------------------------------------------------ 覆盖水印


def test_journal_last_entry_wins_and_bad_lines_skipped(tmp_path):
    mod.append_journal(tmp_path, {"600000.SH": {"queried_to": "20240101"}})
    mod.append_journal(tmp_path, {"600000.SH": {"queried_to": "20261020"}})
    with (tmp_path / mod.JOURNAL_NAME).open("a", encoding="utf-8") as fh:
        fh.write('{"symbol": "600000.SH", "queried_to": "20')  # 崩溃半行
    journal = mod.read_journal(tmp_path)
    assert journal["600000.SH"]["queried_to"] == "20261020"


def test_read_journal_without_file_is_empty(tmp_path):
    assert mod.read_journal(tmp_path) == {}


def test_covered_until_reads_watermark(tmp_path):
    mod.append_journal(tmp_path, {"600000.SH": {"queried_to": "20261020"}})
    assert mod.covered_until(tmp_path, "600000.SH") == "20261020"
    assert mod.covered_until(tmp_path, "000001.SZ") is None


def test_write_symbol_keeps_empty_result_watermark(tmp_path):
    """全期非 ST 的票（A 股多数）必须凭水印跳过，否则每轮重抓。"""
    empty = pd.DataFrame(columns=["symbol", "time", "is_st"])
    mod.write_symbol(
        tmp_path, "000001.SZ", empty, queried_to="20261020", last_data="20261020"
    )
    assert mod.covered_until(tmp_path, "000001.SZ") == "20261020"
    assert pd.read_parquet(tmp_path / "000001.SZ.parquet").empty


# ------------------------------------------------------------------ 抓取编排


def test_crawl_skips_covered_symbols_and_isolates_failures(tmp_path):
    cache = tmp_path.joinpath(*mod.CACHE_REL)
    cache.mkdir(parents=True)
    mod.append_journal(cache, {"600000.SH": {"queried_to": "20261020"}})

    calls: list[str] = []

    def fake_fetch(symbol, start, end):
        calls.append(symbol)
        if symbol == "000002.SZ":
            raise RuntimeError("boom")
        return mod.rows_to_frame([["2026-10-20", "", "1"]], symbol=symbol), "20261020"

    stats = mod.crawl(
        ["600000.SH", "000001.SZ", "000002.SZ"],
        data_root=tmp_path,
        end="20261020",
        fetch=fake_fetch,
        log_fn=lambda *_: None,
    )
    assert calls == ["000001.SZ", "000002.SZ"]
    assert stats["fetched"] == 1
    assert set(stats["errors"]) == {"000002.SZ"}
    assert (cache / "000001.SZ.parquet").exists()
    assert (cache / "000002.SZ.parquet").exists() is False


class _FakeRs:
    def __init__(self, rows, code="0", msg="success"):
        self._rows = list(rows)
        self._i = 0
        self.error_code = code
        self.error_msg = msg

    def next(self):
        if self._i >= len(self._rows):
            return False
        self._i += 1
        return True

    def get_row_data(self):
        return self._rows[self._i - 1]


def _stub_baostock(monkeypatch, rs):
    import sys
    import types

    module = types.ModuleType("baostock")
    calls: dict = {}

    def fake_query(*args, **kwargs):
        calls["args"] = args
        calls["kwargs"] = kwargs
        return rs

    module.query_history_k_data_plus = fake_query
    monkeypatch.setitem(sys.modules, "baostock", module)
    return calls


def test_fetch_baostock_passes_code_positionally_and_parses_rows(monkeypatch):
    """注入式测试绕过了真实调用，这里守住 query 的实参形状（曾重复传 symbol）。"""
    calls = _stub_baostock(
        monkeypatch,
        _FakeRs(
            [
                ["2024-01-02", "sh.600000", "0"],
                ["2024-01-03", "sh.600000", "1"],
            ]
        ),
    )
    frame, last = mod._fetch_baostock("600000.SH", "2024-01-01", "2024-01-05")
    assert calls["args"] == ("sh.600000", mod.BAOSTOCK_FIELDS)
    assert calls["kwargs"]["start_date"] == "2024-01-01"
    assert calls["kwargs"]["end_date"] == "2024-01-05"
    assert last == "20240103"
    assert frame["time"].tolist() == ["20240103"]


def test_fetch_baostock_raises_on_error_code(monkeypatch):
    _stub_baostock(
        monkeypatch, _FakeRs([], code="10004011", msg="股票代码未标识sh或sz")
    )
    with pytest.raises(RuntimeError, match="10004011"):
        mod._fetch_baostock("600000.SH", "2024-01-01", "2024-01-05")


def test_fetch_baostock_rejects_beijing_before_touching_network():
    """baostock 只认 sh./sz.；北交所必须显式判「不支持」而不是当作抓取失败。"""
    with pytest.raises(mod.UnsupportedSymbol, match="北交所"):
        mod._fetch_baostock("430047.BJ", "2016-01-01", "2026-10-21")


def test_crawl_records_unsupported_symbols_with_watermark(tmp_path):
    cache = tmp_path.joinpath(*mod.CACHE_REL)

    def fake_fetch(symbol, start, end):
        raise mod.UnsupportedSymbol("baostock 不覆盖北交所")

    stats = mod.crawl(
        ["430047.BJ"],
        data_root=tmp_path,
        end="20261020",
        fetch=fake_fetch,
        log_fn=lambda *_: None,
    )
    assert stats["unsupported"] == ["430047.BJ"]
    assert stats["errors"] == {}
    assert mod.covered_until(cache, "430047.BJ") == "20261020"
    assert not (cache / "430047.BJ.parquet").exists()


def test_worker_one_marks_beijing_unsupported(tmp_path):
    status = mod._worker_one(("430047.BJ", "2016-01-01", "2026-10-21", str(tmp_path)))
    assert status == ("430047.BJ", "unsupported")
    assert mod.read_journal(tmp_path)["430047.BJ"]["unsupported"]


def test_rebuild_sparse_includes_journal_only_symbols(tmp_path):
    cache = tmp_path.joinpath(*mod.CACHE_REL)
    cache.mkdir(parents=True)
    mod.record_unsupported(
        cache, "430047.BJ", queried_to="20261020", reason="baostock 不覆盖北交所"
    )
    manifest = mod.rebuild_sparse(tmp_path)
    coverage = json.loads(
        (tmp_path.joinpath(*mod.ST_PIT_REL) / "coverage.json").read_text()
    )
    assert coverage["430047.BJ"]["unsupported"] == "baostock 不覆盖北交所"
    assert manifest["unsupported_symbols"] == 1
    assert manifest["rows"] == 0


def test_crawl_refetches_when_cache_is_stale(tmp_path):
    cache = tmp_path.joinpath(*mod.CACHE_REL)
    cache.mkdir(parents=True)
    mod.append_journal(cache, {"600000.SH": {"queried_to": "20261001"}})

    calls: list[str] = []
    mod.crawl(
        ["600000.SH"],
        data_root=tmp_path,
        end="20261020",
        fetch=lambda s, a, b: (
            calls.append(s)
            or (mod.rows_to_frame([["2026-10-20", "", "1"]], symbol=s), "20261020")
        ),
        log_fn=lambda *_: None,
    )
    assert calls == ["600000.SH"]
    assert mod.covered_until(cache, "600000.SH") == "20261020"


def test_crawl_accepts_dashed_end(tmp_path):
    seen: list[tuple[str, str]] = []

    def fake_fetch(symbol, start, end):
        seen.append((start, end))
        return pd.DataFrame(columns=["symbol", "time", "is_st"]), None

    mod.crawl(
        ["600000.SH"],
        data_root=tmp_path,
        end="2026-10-20",
        fetch=fake_fetch,
        log_fn=lambda *_: None,
    )
    assert seen == [("2016-01-01", "2026-10-20")]


# ------------------------------------------------------------------ 稀疏表重建


def _write_cache(cache: Path, symbol: str, st_days: list[str]) -> None:
    frame = pd.DataFrame(
        {
            "symbol": [symbol] * len(st_days),
            "time": st_days,
            "is_st": [True] * len(st_days),
        }
    )
    if not st_days:
        frame = pd.DataFrame(columns=["symbol", "time", "is_st"])
    frame.to_parquet(cache / f"{symbol}.parquet", index=False)


def test_rebuild_sparse_writes_only_st_rows_and_coverage(tmp_path):
    cache = tmp_path.joinpath(*mod.CACHE_REL)
    cache.mkdir(parents=True)
    _write_cache(cache, "600000.SH", ["20240103", "20240104"])
    _write_cache(cache, "000001.SZ", [])
    mod.append_journal(
        cache,
        {
            "600000.SH": {"queried_to": "20261020", "last_data": "20261020"},
            "000001.SZ": {"queried_to": "20261020", "last_data": "20261020"},
        },
    )

    manifest = mod.rebuild_sparse(tmp_path)

    out = tmp_path.joinpath(*mod.ST_PIT_REL)
    sparse = pd.read_parquet(out / "st_pit.parquet")
    assert sparse["symbol"].tolist() == ["600000.SH", "600000.SH"]
    assert sparse["is_st"].all()
    coverage = json.loads((out / "coverage.json").read_text(encoding="utf-8"))
    assert coverage["000001.SZ"]["st_days"] == 0
    assert coverage["000001.SZ"]["queried_to"] == "20261020"
    assert coverage["600000.SH"]["first_st"] == "20240103"
    assert coverage["600000.SH"]["last_data"] == "20261020"
    assert manifest["rows"] == 2
    assert manifest["symbols"] == 1
    assert manifest["cached_symbols"] == 2


def test_rebuild_sparse_with_empty_cache_is_a_noop(tmp_path):
    manifest = mod.rebuild_sparse(tmp_path)
    assert manifest["rows"] == 0
    assert (tmp_path.joinpath(*mod.ST_PIT_REL) / "st_pit.parquet").exists()


# ------------------------------------------------------------------ 掩码读取


def _write_sparse(root: Path, rows: list[tuple[str, str]]) -> None:
    out = root.joinpath(*mod.ST_PIT_REL)
    out.mkdir(parents=True)
    pd.DataFrame(
        {
            "symbol": [r[0] for r in rows],
            "time": [r[1] for r in rows],
            "is_st": [True] * len(rows),
        }
    ).to_parquet(out / "st_pit.parquet", index=False)


def test_load_st_mask_marks_st_days_and_defaults_false(tmp_path):
    _write_sparse(tmp_path, [("600000.SH", "20240103")])
    dates = pd.to_datetime(["2024-01-02", "2024-01-03"])
    mask = mod.load_st_mask(tmp_path, dates, ["600000.SH", "000001.SZ"])
    assert mask.loc[dates[0], "600000.SH"] == False  # noqa: E712
    assert mask.loc[dates[1], "600000.SH"] == True  # noqa: E712
    assert not mask["000001.SZ"].any()


def test_load_st_mask_accepts_yyyymmdd_string_index(tmp_path):
    _write_sparse(tmp_path, [("600000.SH", "20240103")])
    mask = mod.load_st_mask(tmp_path, pd.Index(["20240102", "20240103"]), ["600000.SH"])
    assert mask["600000.SH"].tolist() == [False, True]


def test_load_st_mask_without_table_is_all_false(tmp_path):
    mask = mod.load_st_mask(tmp_path, pd.to_datetime(["2024-01-02"]), ["600000.SH"])
    assert not mask.to_numpy().any()
