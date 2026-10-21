"""退市股日线回补的纯函数与编排测试。

背景：QuantDB 日线/估值只含当前在市标的，2016 年后退市的 250 只 A 股不在
历史面板里，因子精选 K0-a 幸存者偏差硬闸因此失败。回补脚本必须做到：

- 只新增缺失 symbol，绝不覆盖既有行（写盘前逐分区校验 symbol 集合）；
- baostock 单位换算正确（成交额元→万元；只保留 tradestatus=1 且有成交量的行）；
- 后复权序列在参考日与不复权价对齐；
- 默认 dry-run，`--apply` 才落盘，且每个分区留 manifest 证据。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backend.scripts import backfill_delisted_daily as mod

FIELDS = [
    "date",
    "open",
    "high",
    "low",
    "close",
    "preclose",
    "volume",
    "amount",
    "turn",
    "tradestatus",
    "peTTM",
    "pbMRQ",
    "psTTM",
    "pcfNcfTTM",
    "isST",
]


def _rows():
    return [
        [
            "2016-01-04",
            "12.0",
            "12.03",
            "11.23",
            "11.33",
            "11.30",
            "56349700",
            "660376160.0",
            "0.477377",
            "1",
            "7.42",
            "1.03",
            "1.80",
            "0.74",
            "0",
        ],
        [
            "2016-01-05",
            "11.27",
            "11.57",
            "11.15",
            "11.40",
            "11.33",
            "66326995",
            "755531360.0",
            "0.5619",
            "1",
            "7.46",
            "1.03",
            "1.81",
            "0.75",
            "0",
        ],
        ["2016-01-06", "", "", "", "", "", "", "", "", "0", "", "", "", "", "0"],
        [
            "2016-01-07",
            "11.40",
            "11.50",
            "11.20",
            "11.30",
            "11.40",
            "0",
            "0.0",
            "0",
            "1",
            "7.40",
            "1.02",
            "1.79",
            "0.73",
            "0",
        ],
    ]


def test_baostock_code_to_symbol():
    assert mod.baostock_code_to_symbol("sh.600005") == "600005.SH"
    assert mod.baostock_code_to_symbol("sz.000001") == "000001.SZ"
    assert mod.baostock_code_to_symbol("bj.430047") == "430047.BJ"


def test_normalize_history_filters_suspended_and_zero_volume():
    frame = mod.normalize_history(_rows(), FIELDS, "000001.SZ")
    # 只有 2016-01-04 / 01-05 两行：01-06 停牌、01-07 volume=0
    assert len(frame) == 2
    assert list(frame["time"].dt.strftime("%Y-%m-%d")) == ["2016-01-04", "2016-01-05"]


def test_normalize_history_converts_amount_yuan_to_wan():
    frame = mod.normalize_history(_rows(), FIELDS, "000001.SZ")
    # baostock amount 单位元；QuantDB amount 单位万元
    assert frame.loc[0, "amount"] == pytest.approx(66037.616)
    assert frame.loc[0, "volume"] == pytest.approx(56349700.0)
    assert frame.loc[0, "turn"] == pytest.approx(0.477377)
    assert frame.loc[0, "pe_ttm"] == pytest.approx(7.42)
    assert frame.loc[0, "pb"] == pytest.approx(1.03)
    assert frame.loc[0, "symbol"] == "000001.SZ"


def test_normalize_history_empty_returns_typed_frame():
    frame = mod.normalize_history([], FIELDS, "000001.SZ")
    assert frame.empty
    assert {"symbol", "time", *mod.OHLCV_COLUMNS} <= set(frame.columns)


def test_build_backward_anchors_reference_date_to_raw_close():
    raw = pd.DataFrame(
        {
            "symbol": "000001.SZ",
            "time": pd.to_datetime(["2016-01-04", "2024-01-02"]),
            "open": [12.0, 9.21],
            "high": [12.03, 9.30],
            "low": [11.23, 9.10],
            "close": [11.33, 9.21],
            "volume": [100.0, 200.0],
            "amount": [1000.0, 2000.0],
        }
    )
    qfq = raw.copy()
    qfq["close"] = [6.8659076626, 7.5715670644]
    qfq[["open", "high", "low"]] = (
        qfq[["open", "high", "low"]] * (qfq["close"] / raw["close"]).to_numpy()[:, None]
    )

    backward = mod.build_backward(raw, qfq)
    assert len(backward) == 2
    # 参考日（2016-01-04）后复权价与不复权价对齐
    assert backward.loc[backward["time"] == "2016-01-04", "close"].iloc[
        0
    ] == pytest.approx(11.33)
    # 其余日期按 qfq × (raw_ref/qfq_ref) 缩放
    expected = 7.5715670644 * (11.33 / 6.8659076626)
    assert backward.loc[backward["time"] == "2024-01-02", "close"].iloc[
        0
    ] == pytest.approx(expected)


def test_build_backward_empty_returns_typed_frame():
    empty = mod.empty_ohlcv_frame()
    backward = mod.build_backward(empty, empty)
    assert backward.empty
    assert {"symbol", "time", *mod.OHLCV_COLUMNS} <= set(backward.columns)


def test_build_valuation_derives_float_mv():
    raw = mod.normalize_history(_rows(), FIELDS, "000001.SZ")
    valuation = mod.build_valuation(raw)
    row = valuation.iloc[0]
    expected_shares = 56349700 / (0.477377 / 100.0)
    assert row["circulating_capital"] == pytest.approx(expected_shares)
    assert row["float_mv"] == pytest.approx(11.33 * expected_shares)
    assert row["pe_ttm"] == pytest.approx(7.42)
    assert row["pb"] == pytest.approx(1.03)
    assert row["ps_ttm"] == pytest.approx(1.80)
    assert np.isnan(row["total_mv"])


def test_build_valuation_zero_turn_leaves_float_mv_null():
    raw = pd.DataFrame(
        {
            "symbol": "600005.SH",
            "time": pd.to_datetime(["2017-01-03"]),
            "close": [3.23],
            "volume": [1000.0],
            "turn": [0.0],
            "pe_ttm": [np.nan],
            "pb": [np.nan],
            "ps_ttm": [np.nan],
        }
    )
    valuation = mod.build_valuation(raw)
    assert np.isnan(valuation.loc[0, "circulating_capital"])
    assert np.isnan(valuation.loc[0, "float_mv"])


def test_merge_partition_adds_only_missing_symbols():
    existing = pd.DataFrame(
        {
            "symbol": ["000001.SZ", "000002.SZ"],
            "time": pd.to_datetime(["2016-01-04"] * 2),
            "close": [1.0, 2.0],
        }
    )
    new = pd.DataFrame(
        {
            "symbol": ["000001.SZ", "600005.SH"],
            "time": pd.to_datetime(["2016-01-04"] * 2),
            "close": [9.0, 3.0],
        }
    )
    merged, added = mod.merge_partition(existing, new)
    assert added == 1
    assert merged["symbol"].tolist() == ["000001.SZ", "000002.SZ", "600005.SH"]
    # 既有行绝不被新行覆盖
    assert merged.loc[merged["symbol"] == "000001.SZ", "close"].iloc[0] == 1.0


def test_merge_partition_is_idempotent():
    existing = pd.DataFrame(
        {
            "symbol": ["000001.SZ"],
            "time": pd.to_datetime(["2016-01-04"]),
            "close": [1.0],
        }
    )
    new = pd.DataFrame(
        {
            "symbol": ["000001.SZ"],
            "time": pd.to_datetime(["2016-01-04"]),
            "close": [9.0],
        }
    )
    merged, added = mod.merge_partition(existing, new)
    assert added == 0
    assert merged.equals(existing)


def _write_partition(root: Path, dataset: str, dt: str, frame: pd.DataFrame) -> Path:
    path = root / mod.DATASET_PARTS[dataset] / f"dt={dt}" / "data.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(path, index=False)
    return path


def _fake_pair(*_args, **_kwargs):
    raw = mod.normalize_history(_rows(), FIELDS, "000001.SZ")
    qfq = raw.copy()
    qfq[["open", "high", "low", "close"]] = (
        qfq[["open", "high", "low", "close"]] * 0.606
    )
    return raw, qfq


def _fake_universe(*_args, **_kwargs):
    return pd.DataFrame(
        {
            "symbol": ["000001.SZ"],
            "code": ["sz.000001"],
            "name": ["平安银行"],
            "ipo": pd.to_datetime(["1991-04-03"]),
            "out": pd.to_datetime(["2020-01-02"]),
        }
    )


def test_run_backfill_dry_run_does_not_touch_files(tmp_path, monkeypatch):
    existing = pd.DataFrame(
        {
            "symbol": ["000002.SZ"],
            "time": pd.to_datetime(["2016-01-04"]),
            "close": [2.0],
            "volume": [1.0],
            "amount": [1.0],
            "open": [2.0],
            "high": [2.0],
            "low": [2.0],
        }
    )
    path = _write_partition(tmp_path, "daily_unadjusted", "20160104", existing)
    before = path.read_bytes()

    monkeypatch.setattr(mod, "load_delisted_universe", _fake_universe)
    monkeypatch.setattr(mod, "fetch_stock_pair", _fake_pair)
    summary = mod.run_backfill(
        tmp_path,
        start="2016-01-01",
        end="2020-12-31",
        datasets=("daily_unadjusted",),
        apply=False,
    )
    assert summary["added_rows"] > 0
    assert path.read_bytes() == before
    assert summary["applied"] is False


def test_run_backfill_apply_adds_rows_and_manifest(tmp_path, monkeypatch):
    existing = pd.DataFrame(
        {
            "symbol": ["000002.SZ"],
            "time": pd.to_datetime(["2016-01-04"]),
            "close": [2.0],
            "volume": [1.0],
            "amount": [1.0],
            "open": [2.0],
            "high": [2.0],
            "low": [2.0],
        }
    )
    path = _write_partition(tmp_path, "daily_unadjusted", "20160104", existing)

    monkeypatch.setattr(mod, "load_delisted_universe", _fake_universe)
    monkeypatch.setattr(mod, "fetch_stock_pair", _fake_pair)
    summary = mod.run_backfill(
        tmp_path,
        start="2016-01-01",
        end="2020-12-31",
        datasets=("daily_unadjusted",),
        apply=True,
    )
    assert summary["applied"] is True
    merged = pd.read_parquet(path)
    assert set(merged["symbol"]) == {"000002.SZ", "000001.SZ"}
    assert merged.loc[merged["symbol"] == "000002.SZ", "close"].iloc[0] == 2.0
    manifest = Path(summary["manifest_path"])
    records = [
        json.loads(line) for line in manifest.read_text().splitlines() if line.strip()
    ]
    assert any(
        r["dataset"] == "daily_unadjusted" and r["dt"] == "20160104" for r in records
    )


def test_run_backfill_skips_missing_partition(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "load_delisted_universe", _fake_universe)
    monkeypatch.setattr(mod, "fetch_stock_pair", _fake_pair)
    summary = mod.run_backfill(
        tmp_path,
        start="2016-01-01",
        end="2020-12-31",
        datasets=("daily_unadjusted",),
        apply=True,
    )
    assert summary["added_rows"] == 0
    assert summary["skipped_missing_partition"] > 0


# ------------------------------------------------------------------ 边界与 IO


def test_baostock_code_to_symbol_rejects_unknown():
    with pytest.raises(ValueError):
        mod.baostock_code_to_symbol("xx.123456")
    with pytest.raises(ValueError):
        mod.baostock_code_to_symbol("600005")


def test_normalize_history_tolerates_missing_optional_columns():
    rows = [["2016-01-04", "1.0", "1.0", "1.0", "1.0", "100", "1000"]]
    fields = ["date", "open", "high", "low", "close", "volume", "amount"]
    frame = mod.normalize_history(rows, fields, "600005.SH")
    assert len(frame) == 1
    assert np.isnan(frame.loc[0, "turn"])
    assert np.isnan(frame.loc[0, "pe_ttm"])


def test_normalize_history_all_filtered_returns_typed_empty():
    rows = [["2016-01-04", "1.0", "1.0", "1.0", "1.0", "0", "0.0", "0", "1"]]
    fields = [
        "date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
        "turn",
        "tradestatus",
    ]
    frame = mod.normalize_history(rows, fields, "600005.SH")
    assert frame.empty
    assert {"symbol", "time", *mod.OHLCV_COLUMNS} <= set(frame.columns)


def test_build_backward_disjoint_dates_returns_empty():
    raw = pd.DataFrame(
        {
            "symbol": "600005.SH",
            "time": pd.to_datetime(["2016-01-04"]),
            "open": [1.0],
            "high": [1.0],
            "low": [1.0],
            "close": [1.0],
            "volume": [1.0],
            "amount": [1.0],
        }
    )
    qfq = raw.copy()
    qfq["time"] = pd.to_datetime(["2020-01-02"])
    assert mod.build_backward(raw, qfq).empty


def test_build_valuation_empty_returns_typed_frame():
    frame = mod.build_valuation(mod.empty_ohlcv_frame())
    assert frame.empty
    assert list(frame.columns) == list(mod.VALUATION_COLUMNS)


def test_merge_partition_into_empty_existing():
    new = pd.DataFrame(
        {
            "symbol": ["600005.SH"],
            "time": pd.to_datetime(["2016-01-04"]),
            "close": [3.0],
        }
    )
    merged, added = mod.merge_partition(pd.DataFrame(), new)
    assert added == 1
    assert merged.equals(new)


def test_write_partition_atomic_preserves_existing_symbols(tmp_path):
    target = tmp_path / "data.parquet"
    first = pd.DataFrame({"symbol": ["000001.SZ"], "close": [1.0]})
    mod.write_partition_atomic(target, first, existing_symbols={"000001.SZ"})
    assert pd.read_parquet(target)["close"].tolist() == [1.0]

    with pytest.raises(RuntimeError):
        mod.write_partition_atomic(
            target, first, existing_symbols={"000001.SZ", "600005.SH"}
        )
    # 校验失败时原文件保持不动
    assert pd.read_parquet(target)["close"].tolist() == [1.0]
    assert not list(tmp_path.glob("*.tmp.parquet"))


class _FakeResult:
    def __init__(self, rows, fields, *, data_list=True, error_code="0", error_msg=""):
        self.fields = fields
        self.error_code = error_code
        self.error_msg = error_msg
        self.data = list(rows) if data_list else None
        self._rows = list(rows)
        self._cursor = 0

    def next(self):
        if self._cursor >= len(self._rows):
            return False
        self._cursor += 1
        return True

    def get_row_data(self):
        return self._rows[self._cursor - 1]


def _fake_baostock(monkeypatch, *, basic_result=None, history_result=None):
    import sys
    import types

    module = types.SimpleNamespace(
        login=lambda: types.SimpleNamespace(error_code="0", error_msg=""),
        logout=lambda: None,
        query_stock_basic=lambda: basic_result,
        query_history_k_data_plus=lambda *args, **kwargs: history_result,
    )
    monkeypatch.setitem(sys.modules, "baostock", module)
    monkeypatch.setattr(mod, "_LOGGED_IN", False)
    return module


def test_load_delisted_universe_filters_non_a_share_and_ranges(monkeypatch):
    basic = [
        ["sh.600005", "武钢股份", "1999-08-03", "2016-06-14", "1", "1"],
        ["sz.000001", "平安银行", "1991-04-03", "", "1", "1"],
        ["sh.900901", "云赛B股", "1992-02-24", "2020-01-02", "1", "1"],
        ["sh.600001", "邯郸钢铁", "1998-01-22", "2015-12-31", "1", "1"],
        ["sz.000002", "万科A", "1991-01-29", "2019-06-30", "2", "1"],
    ]
    fields = ["code", "code_name", "ipoDate", "outDate", "type", "status"]
    _fake_baostock(
        monkeypatch, basic_result=_FakeResult(basic, fields, data_list=False)
    )

    universe = mod.load_delisted_universe("2016-01-01", "2020-12-31")
    assert universe["symbol"].tolist() == ["600005.SH"]
    assert universe.loc[0, "code"] == "sh.600005"


def test_load_delisted_universe_iterates_all_pages(monkeypatch):
    # result.data 只含当前页；第二页必须靠 next() 取回，不能只看 data
    basic = [
        ["sh.600005", "武钢股份", "1999-08-03", "2017-02-14", "1", "0"],
        ["sz.000003", "PT金田A", "1991-07-03", "2016-06-14", "1", "0"],
    ]
    fields = ["code", "code_name", "ipoDate", "outDate", "type", "status"]
    result = _FakeResult(basic, fields)
    result.data = basic[:1]  # 模拟首页只有一只
    _fake_baostock(monkeypatch, basic_result=result)

    universe = mod.load_delisted_universe("2016-01-01", "2020-12-31")
    assert universe["symbol"].tolist() == ["000003.SZ", "600005.SH"]


def test_fetch_history_normalizes_rows(monkeypatch):
    fields = FIELDS
    _fake_baostock(
        monkeypatch,
        history_result=_FakeResult(_rows(), fields, data_list=False),
    )
    frame = mod._fetch_history(
        "sz.000001", "000001.SZ", "2016-01-01", "2016-12-31", "3"
    )
    assert len(frame) == 2
    assert frame["amount"].iloc[0] == pytest.approx(66037.616)


def test_fetch_history_retries_then_succeeds(monkeypatch):
    import types

    fields = FIELDS
    calls = {"n": 0}

    def query(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("temporary")
        return _FakeResult(_rows(), fields)

    module = types.SimpleNamespace(
        login=lambda: types.SimpleNamespace(error_code="0", error_msg=""),
        logout=lambda: None,
        query_history_k_data_plus=query,
    )
    import sys

    monkeypatch.setitem(sys.modules, "baostock", module)
    monkeypatch.setattr(mod, "_LOGGED_IN", False)
    monkeypatch.setattr(mod.time, "sleep", lambda _seconds: None)

    frame = mod._fetch_history(
        "sz.000001", "000001.SZ", "2016-01-01", "2016-12-31", "3"
    )
    assert len(frame) == 2
    assert calls["n"] == 3


def test_fetch_history_raises_after_three_failures(monkeypatch):
    import sys
    import types

    def query(*args, **kwargs):
        raise RuntimeError("boom")

    module = types.SimpleNamespace(
        login=lambda: types.SimpleNamespace(error_code="0", error_msg=""),
        logout=lambda: None,
        query_history_k_data_plus=query,
    )
    monkeypatch.setitem(sys.modules, "baostock", module)
    monkeypatch.setattr(mod, "_LOGGED_IN", False)
    monkeypatch.setattr(mod.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="拉取失败"):
        mod._fetch_history("sz.000001", "000001.SZ", "2016-01-01", "2016-12-31", "3")


def test_ensure_login_failure_raises(monkeypatch):
    import sys
    import types

    module = types.SimpleNamespace(
        login=lambda: types.SimpleNamespace(error_code="1", error_msg="denied"),
        logout=lambda: None,
    )
    monkeypatch.setitem(sys.modules, "baostock", module)
    monkeypatch.setattr(mod, "_LOGGED_IN", False)
    with pytest.raises(RuntimeError, match="登录失败"):
        mod._ensure_login()


def test_logout_only_when_logged_in(monkeypatch):
    import sys
    import types

    logged_out = {"n": 0}
    module = types.SimpleNamespace(
        login=lambda: types.SimpleNamespace(error_code="0", error_msg=""),
        logout=lambda: logged_out.__setitem__("n", logged_out["n"] + 1),
    )
    monkeypatch.setitem(sys.modules, "baostock", module)
    monkeypatch.setattr(mod, "_LOGGED_IN", True)
    mod._logout()
    assert logged_out["n"] == 1
    assert mod._LOGGED_IN is False

    mod._logout()
    assert logged_out["n"] == 1


class _FakeSocket:
    def __init__(self, recv_chunks=None):
        self.timeouts = []
        self.closed = False
        self.sent = []
        self._recv_chunks = list(recv_chunks or [])

    def settimeout(self, value):
        self.timeouts.append(value)

    def close(self):
        self.closed = True

    def send(self, data):
        self.sent.append(data)
        return len(data)

    def recv(self, bufsize):
        if not self._recv_chunks:
            return b""
        return self._recv_chunks.pop(0)


class _FakeSocketContext:
    def __init__(self, sock=None):
        self.default_socket = sock


def test_apply_socket_timeout_arms_default_socket(monkeypatch):
    sock = _FakeSocket()
    context = _FakeSocketContext(sock)
    monkeypatch.setattr(mod, "_socket_context", lambda: context)

    mod._apply_socket_timeout(30.0)
    assert sock.timeouts == [30.0]
    # 原始 socket 被包上空包守卫后写回 context
    assert isinstance(context.default_socket, mod._GuardedSocket)


def test_apply_socket_timeout_is_idempotent(monkeypatch):
    sock = _FakeSocket()
    context = _FakeSocketContext(sock)
    monkeypatch.setattr(mod, "_socket_context", lambda: context)

    mod._apply_socket_timeout(30.0)
    guarded = context.default_socket
    mod._apply_socket_timeout(45.0)
    assert context.default_socket is guarded
    assert sock.timeouts == [30.0, 45.0]


def test_guarded_socket_delegates_and_rejects_empty_recv():
    inner = _FakeSocket(recv_chunks=[b"payload<![CDATA[]]>\n"])
    sock = mod._GuardedSocket(inner)

    assert sock.send(b"msg") == 3
    assert sock.recv(8192) == b"payload<![CDATA[]]>\n"
    sock.settimeout(5.0)
    assert inner.timeouts == [5.0]

    # 对端关闭 → recv 返回 b""：原版 baostock 会 100% CPU 死循环
    with pytest.raises(ConnectionError, match="对端关闭"):
        sock.recv(8192)
    assert isinstance(sock.closed_error, ConnectionError)

    sock.close()
    assert inner.closed is True


def test_raise_if_socket_closed_surfaces_truncation_and_clears(monkeypatch):
    guard = mod._GuardedSocket(_FakeSocket())
    guard.closed_error = ConnectionError("对端关闭")
    monkeypatch.setattr(mod, "_current_socket_guard", lambda: guard)

    with pytest.raises(ConnectionError, match="对端关闭"):
        mod._raise_if_socket_closed()
    assert guard.closed_error is None

    mod._raise_if_socket_closed()  # 已清空：不抛


def test_raise_if_socket_closed_without_guard_is_noop(monkeypatch):
    monkeypatch.setattr(mod, "_current_socket_guard", lambda: None)
    mod._raise_if_socket_closed()


def test_fetch_history_detects_truncated_pagination(monkeypatch):
    import sys
    import types

    fields = FIELDS
    calls = {"query": 0}

    def query(*args, **kwargs):
        calls["query"] += 1
        # 第一次：翻页被截断（守卫记录断开）→ 第二次：完整返回
        return _FakeResult(_rows(), fields)

    module = types.SimpleNamespace(
        login=lambda: types.SimpleNamespace(error_code="0", error_msg=""),
        logout=lambda: None,
        query_history_k_data_plus=query,
    )
    monkeypatch.setitem(sys.modules, "baostock", module)
    monkeypatch.setattr(mod, "_LOGGED_IN", False)
    monkeypatch.setattr(mod.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(mod, "_apply_socket_timeout", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_reset_connection", lambda: None)

    def guarded_raise():
        if calls["query"] == 1:
            raise ConnectionError("baostock socket 已被对端关闭")

    monkeypatch.setattr(mod, "_raise_if_socket_closed", guarded_raise)

    frame = mod._fetch_history(
        "sz.000001", "000001.SZ", "2016-01-01", "2016-12-31", "3"
    )
    assert len(frame) == 2
    assert calls["query"] == 2


def test_apply_socket_timeout_tolerates_missing_socket(monkeypatch):
    monkeypatch.setattr(mod, "_socket_context", lambda: _FakeSocketContext(None))
    mod._apply_socket_timeout()  # 不抛异常


def test_ensure_login_applies_socket_timeout(monkeypatch):
    import sys
    import types

    sock = _FakeSocket()
    module = types.SimpleNamespace(
        login=lambda: types.SimpleNamespace(error_code="0", error_msg=""),
        logout=lambda: None,
    )
    monkeypatch.setitem(sys.modules, "baostock", module)
    monkeypatch.setattr(mod, "_LOGGED_IN", False)
    monkeypatch.setattr(mod, "_socket_context", lambda: _FakeSocketContext(sock))

    mod._ensure_login()
    assert mod._LOGGED_IN is True
    assert sock.timeouts == [mod.BAOSTOCK_SOCKET_TIMEOUT_SECONDS]


def test_reset_connection_closes_socket_and_forces_relogin(monkeypatch):
    sock = _FakeSocket()
    context = _FakeSocketContext(sock)
    monkeypatch.setattr(mod, "_socket_context", lambda: context)
    monkeypatch.setattr(mod, "_LOGGED_IN", True)

    mod._reset_connection()
    assert sock.closed is True
    assert context.default_socket is None
    assert mod._LOGGED_IN is False


def test_reset_connection_without_socket_only_clears_login(monkeypatch):
    monkeypatch.setattr(mod, "_socket_context", lambda: None)
    monkeypatch.setattr(mod, "_LOGGED_IN", True)

    mod._reset_connection()
    assert mod._LOGGED_IN is False


def test_fetch_history_reconnects_between_attempts(monkeypatch):
    import sys
    import types

    fields = FIELDS
    calls = {"query": 0, "login": 0, "reset": 0}

    def login():
        calls["login"] += 1
        return types.SimpleNamespace(error_code="0", error_msg="")

    def query(*args, **kwargs):
        calls["query"] += 1
        if calls["query"] == 1:
            raise TimeoutError("recv timed out")
        return _FakeResult(_rows(), fields)

    module = types.SimpleNamespace(
        login=login, logout=lambda: None, query_history_k_data_plus=query
    )
    monkeypatch.setitem(sys.modules, "baostock", module)
    monkeypatch.setattr(mod, "_LOGGED_IN", False)
    monkeypatch.setattr(mod.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(mod, "_apply_socket_timeout", lambda *a, **k: None)

    def fake_reset():
        calls["reset"] += 1
        mod._LOGGED_IN = False

    monkeypatch.setattr(mod, "_reset_connection", fake_reset)

    frame = mod._fetch_history(
        "sz.000001", "000001.SZ", "2016-01-01", "2016-12-31", "3"
    )
    assert len(frame) == 2
    assert calls["query"] == 2
    assert calls["reset"] == 1
    assert calls["login"] == 2  # 第一次登录 + 断开后重连


def test_fetch_stock_pair_caches_and_refreshes(tmp_path, monkeypatch):
    calls = []

    def fake_fetch(code, symbol, start, end, adjustflag):
        calls.append(adjustflag)
        return mod.normalize_history(_rows(), FIELDS, symbol)

    monkeypatch.setattr(mod, "_fetch_history", fake_fetch)

    first_raw, first_qfq = mod.fetch_stock_pair(
        "sz.000001", "000001.SZ", "2016-01-01", "2020-12-31", cache_dir=tmp_path
    )
    assert calls == ["3", "2"]
    assert (tmp_path / "000001.SZ_raw.parquet").exists()

    calls.clear()
    cached_raw, cached_qfq = mod.fetch_stock_pair(
        "sz.000001", "000001.SZ", "2016-01-01", "2020-12-31", cache_dir=tmp_path
    )
    assert calls == []
    assert cached_raw.equals(first_raw)
    assert cached_qfq.equals(first_qfq)

    calls.clear()
    mod.fetch_stock_pair(
        "sz.000001",
        "000001.SZ",
        "2016-01-01",
        "2020-12-31",
        cache_dir=tmp_path,
        refresh=True,
    )
    assert calls == ["3", "2"]


def test_fetch_stock_pair_without_cache(tmp_path, monkeypatch):
    calls = []

    def fake_fetch(code, symbol, start, end, adjustflag):
        calls.append(adjustflag)
        return mod.normalize_history(_rows(), FIELDS, symbol)

    monkeypatch.setattr(mod, "_fetch_history", fake_fetch)
    mod.fetch_stock_pair("sz.000001", "000001.SZ", "2016-01-01", "2020-12-31")
    assert calls == ["3", "2"]


def test_run_backfill_rejects_unknown_dataset(tmp_path):
    with pytest.raises(ValueError, match="未知数据集"):
        mod.run_backfill(tmp_path, datasets=("not_a_dataset",))


def test_run_backfill_empty_universe_returns_empty_summary(tmp_path, monkeypatch):
    monkeypatch.setattr(
        mod,
        "load_delisted_universe",
        lambda *a, **k: pd.DataFrame(columns=["symbol", "code", "name", "ipo", "out"]),
    )
    summary = mod.run_backfill(tmp_path, datasets=("daily_unadjusted",), apply=True)
    assert summary["stocks_requested"] == 0
    assert summary["applied"] is False
    assert summary["added_rows"] == 0


def test_run_backfill_codes_filter_and_idempotent_apply(tmp_path, monkeypatch):
    universe = pd.DataFrame(
        {
            "symbol": ["000001.SZ", "600005.SH"],
            "code": ["sz.000001", "sh.600005"],
            "name": ["平安银行", "武钢股份"],
            "ipo": pd.to_datetime(["1991-04-03", "1999-08-03"]),
            "out": pd.to_datetime(["2020-01-02", "2020-06-01"]),
        }
    )
    monkeypatch.setattr(mod, "load_delisted_universe", lambda *a, **k: universe)
    monkeypatch.setattr(mod, "fetch_stock_pair", _fake_pair)

    for dt, day in (("20160104", "2016-01-04"), ("20160105", "2016-01-05")):
        _write_partition(
            tmp_path,
            "daily_unadjusted",
            dt,
            pd.DataFrame(
                {
                    "symbol": ["600005.SH"],
                    "time": pd.to_datetime([day]),
                    "open": [3.0],
                    "high": [3.0],
                    "low": [3.0],
                    "close": [3.0],
                    "volume": [1.0],
                    "amount": [1.0],
                }
            ),
        )

    summary = mod.run_backfill(
        tmp_path,
        datasets=("daily_unadjusted",),
        codes=("000001.SZ",),
        apply=True,
    )
    assert summary["stocks_requested"] == 1
    assert summary["added_rows"] == 2  # 2016-01-04 + 2016-01-05
    assert summary["failed_stocks"] == []

    summary2 = mod.run_backfill(
        tmp_path,
        datasets=("daily_unadjusted",),
        codes=("000001.SZ",),
        apply=True,
    )
    assert summary2["added_rows"] == 0
    records = Path(summary["manifest_path"]).read_text().splitlines()
    assert len(records) == 2


def test_run_backfill_records_fetch_failures(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "load_delisted_universe", _fake_universe)

    def boom(*_args, **_kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(mod, "fetch_stock_pair", boom)
    summary = mod.run_backfill(tmp_path, datasets=("daily_unadjusted",))
    assert summary["stocks_fetched"] == 0
    assert summary["failed_stocks"] == [
        {"symbol": "000001.SZ", "error": "network down"}
    ]


def test_main_parses_cli_and_prints_summary(tmp_path, monkeypatch, capsys):
    captured = {}

    def fake_run(data_root, **kwargs):
        captured["data_root"] = data_root
        captured.update(kwargs)
        return {"added_rows": 0, "applied": False, "manifest_path": "x"}

    monkeypatch.setattr(mod, "run_backfill", fake_run)
    monkeypatch.setattr(mod, "_logout", lambda: None)

    code = mod.main(
        [
            "--data-root",
            str(tmp_path),
            "--start",
            "2016-01-01",
            "--end",
            "2020-12-31",
            "--codes",
            "600005.SH,sh.600006",
            "--datasets",
            "daily_unadjusted",
        ]
    )
    assert code == 0
    assert captured["codes"] == ("600005.SH", "sh.600006")
    assert captured["datasets"] == ("daily_unadjusted",)
    assert captured["apply"] is False
    assert json.loads(capsys.readouterr().out)["added_rows"] == 0
