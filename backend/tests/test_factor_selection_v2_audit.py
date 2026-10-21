"""K0-a 幸存者偏差审计的纯函数与数据集覆盖测试。

判据（细案 §3.5）：退市样本缺失率 >10% 且无法量化 → 暂停；可量化则记录并降级
相关因子。本审计以退市股在 baostock 的真实交易日为分母，逐数据集统计：
哪些退市股在哪些交易日应出现、实际出现了多少。
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from backend.scripts import factor_selection_v2_audit as mod


def _write_panel(root: Path, parts: str, dt: str, symbols: list[str]) -> Path:
    path = root / parts / f"dt={dt}" / "data.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"symbol": symbols, "close": [1.0] * len(symbols)}).to_parquet(
        path, index=False
    )
    return path


def _write_history(cache: Path, symbol: str, dates: list[str]) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / f"{symbol}_raw.parquet"
    pd.DataFrame(
        {
            "symbol": [symbol] * len(dates),
            "time": pd.to_datetime(dates),
            "close": [1.0] * len(dates),
        }
    ).to_parquet(path, index=False)
    return path


def test_normalize_date_accepts_common_forms():
    assert mod.normalize_date("2016-01-04") == "20160104"
    assert mod.normalize_date("20160104") == "20160104"
    assert mod.normalize_date(pd.Timestamp("2016-01-04")) == "20160104"
    assert mod.normalize_date(None) is None


def test_load_delisted_histories_reads_raw_cache_only(tmp_path):
    _write_history(tmp_path, "600005.SH", ["2016-01-04", "2016-01-05"])
    _write_history(tmp_path, "000748.SZ", ["2016-03-10"])
    # qfq 缓存不能被当成历史交易日
    (tmp_path / "000748.SZ_qfq.parquet").write_bytes(b"not parquet")

    histories = mod.load_delisted_histories(tmp_path)
    assert set(histories) == {"600005.SH", "000748.SZ"}
    assert histories["600005.SH"] == {"20160104", "20160105"}


def test_expected_delisted_by_date_inverts_histories():
    histories = {
        "600005.SH": {"20160104", "20160105"},
        "000748.SZ": {"20160105", "20160310"},
    }
    expected = mod.expected_delisted_by_date(histories)
    assert expected["20160104"] == {"600005.SH"}
    assert expected["20160105"] == {"600005.SH", "000748.SZ"}
    assert expected["20160310"] == {"000748.SZ"}


def test_audit_dataset_full_coverage(tmp_path):
    histories = {"600005.SH": {"20160104", "20160105"}}
    _write_panel(
        tmp_path,
        "1_kline_data/daily_unadjusted",
        "20160104",
        ["600005.SH", "000001.SZ"],
    )
    _write_panel(tmp_path, "1_kline_data/daily_unadjusted", "20160105", ["600005.SH"])

    result = mod.audit_dataset(tmp_path, "1_kline_data/daily_unadjusted", histories)
    assert result["partitions"] == 2
    assert result["delisted_expected_stock_days"] == 2
    assert result["delisted_present_stock_days"] == 2
    assert result["delisted_missing_rate"] == 0.0
    assert result["delisted_stocks_captured"] == 1


def test_audit_dataset_reports_missing_half(tmp_path):
    histories = {"600005.SH": {"20160104", "20160105"}}
    _write_panel(tmp_path, "1_kline_data/daily_unadjusted", "20160104", ["600005.SH"])
    _write_panel(tmp_path, "1_kline_data/daily_unadjusted", "20160105", ["000001.SZ"])

    result = mod.audit_dataset(tmp_path, "1_kline_data/daily_unadjusted", histories)
    assert result["delisted_expected_stock_days"] == 2
    assert result["delisted_present_stock_days"] == 1
    assert result["delisted_missing_rate"] == 0.5
    assert result["worst_stocks"][0]["symbol"] == "600005.SH"
    assert result["worst_stocks"][0]["missing_days"] == 1


def test_audit_dataset_respects_start_end(tmp_path):
    histories = {"600005.SH": {"20151231", "20160104", "20160105"}}
    for dt in ("20151231", "20160104", "20160105"):
        _write_panel(tmp_path, "1_kline_data/daily_unadjusted", dt, ["600005.SH"])

    result = mod.audit_dataset(
        tmp_path,
        "1_kline_data/daily_unadjusted",
        histories,
        start="20160104",
        end="20160104",
    )
    assert result["partitions"] == 1
    assert result["delisted_expected_stock_days"] == 1
    assert result["delisted_missing_rate"] == 0.0


def test_audit_dataset_by_year_and_empty_expected(tmp_path):
    histories = {"600005.SH": {"20160104", "20170104"}}
    _write_panel(tmp_path, "1_kline_data/daily_unadjusted", "20160104", ["600005.SH"])
    _write_panel(tmp_path, "1_kline_data/daily_unadjusted", "20170104", [])

    result = mod.audit_dataset(tmp_path, "1_kline_data/daily_unadjusted", histories)
    assert result["by_year"]["2016"]["missing_rate"] == 0.0
    assert result["by_year"]["2017"]["missing_rate"] == 1.0


def test_audit_dataset_without_expected_rows_passes(tmp_path):
    histories = {"600005.SH": {"20160104"}}
    _write_panel(tmp_path, "1_kline_data/daily_unadjusted", "20200102", ["000001.SZ"])

    result = mod.audit_dataset(tmp_path, "1_kline_data/daily_unadjusted", histories)
    assert result["delisted_expected_stock_days"] == 0
    assert result["delisted_missing_rate"] is None
    assert mod.k0a_verdict(result)["pass"] is True


def test_k0a_verdict_threshold():
    entry = {"delisted_expected_stock_days": 100, "delisted_missing_rate": 0.11}
    assert mod.k0a_verdict(entry)["quantifiable"] is True
    assert mod.k0a_verdict(entry)["pass"] is False
    entry["delisted_missing_rate"] = 0.10
    assert mod.k0a_verdict(entry)["pass"] is True


def test_audit_all_and_write_report(tmp_path):
    histories = {"600005.SH": {"20160104"}}
    _write_panel(tmp_path, "1_kline_data/daily_unadjusted", "20160104", ["600005.SH"])
    _write_panel(tmp_path, "6_ml_datasets/l1_factors", "20160104", ["000001.SZ"])

    result = mod.audit_all(
        tmp_path,
        histories,
        datasets=("1_kline_data/daily_unadjusted", "6_ml_datasets/l1_factors"),
    )
    assert (
        result["datasets"]["1_kline_data/daily_unadjusted"]["delisted_missing_rate"]
        == 0.0
    )
    assert (
        result["datasets"]["6_ml_datasets/l1_factors"]["delisted_missing_rate"] == 1.0
    )
    assert result["overall"]["pass"] is False

    json_path, md_path = mod.write_report(result, tmp_path / "audit")
    payload = json.loads(json_path.read_text())
    assert payload["overall"]["pass"] is False
    text = md_path.read_text()
    assert "K0-a" in text
    assert "l1_factors" in text
    assert "FAIL" in text


def test_discover_factor_datasets(tmp_path):
    _write_panel(tmp_path, "6_ml_datasets/l1_factors", "20160104", ["000001.SZ"])
    _write_panel(tmp_path, "6_ml_datasets/alpha360", "20160104", ["000001.SZ"])
    (tmp_path / "6_ml_datasets" / "_staging_l1").mkdir(parents=True)
    (tmp_path / "6_ml_datasets" / "empty_lib").mkdir(parents=True)

    found = mod.discover_factor_datasets(tmp_path)
    assert found == ("6_ml_datasets/alpha360", "6_ml_datasets/l1_factors")


def test_main_requires_cache(tmp_path, capsys):
    with pytest.raises(SystemExit, match="delisted_raw"):
        mod.main(
            ["--data-root", str(tmp_path), "--cache-dir", str(tmp_path / "missing")]
        )


def test_main_writes_report(tmp_path, capsys):
    cache = tmp_path / "delisted_raw"
    _write_history(cache, "600005.SH", ["2016-01-04"])
    _write_panel(
        tmp_path,
        "1_kline_data/daily_unadjusted",
        "20160104",
        ["600005.SH"],
    )
    out = tmp_path / "audit"
    code = mod.main(
        [
            "--data-root",
            str(tmp_path),
            "--cache-dir",
            str(cache),
            "--datasets",
            "1_kline_data/daily_unadjusted",
            "--start",
            "2016-01-01",
            "--end",
            "2016-12-31",
            "--out",
            str(out),
        ]
    )
    assert code == 0
    assert (out / "k0a_survivorship.json").exists()
    assert (out / "K0a_survivorship.md").exists()
    printed = capsys.readouterr().out
    assert "json:" in printed
    assert "PASS" in printed or '"pass": true' in printed
