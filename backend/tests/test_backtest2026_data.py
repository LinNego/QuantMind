"""QuantDB 面板装载测试：分区读取、代码归一、预测合并。"""

from __future__ import annotations

import pandas as pd
import pytest

from backend.scripts.backtest2026 import data as data_module
from backend.scripts.backtest2026.data import (
    attach_listing_age,
    build_market_panel,
    load_calendar,
    load_index_closes,
    load_prediction_panel,
    load_price_matrices,
    load_st_symbols,
    normalize_symbol,
)


def _write(dataset: str, dt: str, rows: list[dict], root) -> None:
    path = root / "1_kline_data" / dataset / f"dt={dt}"
    path.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(path / "data.parquet", index=False)


@pytest.fixture()
def quantdb_root(tmp_path):
    root = tmp_path / "quantdb"
    _write(
        "daily_unadjusted",
        "20260105",
        [
            {
                "symbol": "600000.SH",
                "time": "2026-01-05",
                "close": 10.0,
                "volume": 1000.0,
            },
            {
                "symbol": "000001.SZ",
                "time": "2026-01-05",
                "close": 20.0,
                "volume": 2000.0,
            },
            {
                "symbol": "430001.BJ",
                "time": "2026-01-05",
                "close": 5.0,
                "volume": 500.0,
            },
        ],
        root,
    )
    _write(
        "daily_unadjusted",
        "20260106",
        [
            {
                "symbol": "600000.SH",
                "time": "2026-01-06",
                "close": 10.3,
                "volume": 1100.0,
            },
            {
                "symbol": "000001.SZ",
                "time": "2026-01-06",
                "close": 19.0,
                "volume": 2100.0,
            },
        ],
        root,
    )
    _write(
        "daily_forward",
        "20260105",
        [
            {"symbol": "600000.SH", "time": "2026-01-05", "close": 9.5},
            {"symbol": "000001.SZ", "time": "2026-01-05", "close": 19.5},
        ],
        root,
    )
    _write(
        "daily_forward",
        "20260106",
        [
            {"symbol": "600000.SH", "time": "2026-01-06", "close": 10.2},
            {"symbol": "000001.SZ", "time": "2026-01-06", "close": 18.5},
        ],
        root,
    )
    _write(
        "index_daily",
        "20260105",
        [{"symbol": "000300.SH", "time": "2026-01-05", "close": 4000.0}],
        root,
    )
    _write(
        "index_daily",
        "20260106",
        [{"symbol": "000300.SH", "time": "2026-01-06", "close": 4040.0}],
        root,
    )
    calendar_dir = root / "2_base_sector" / "trading_calendar"
    calendar_dir.mkdir(parents=True)
    pd.DataFrame(
        {
            "TradingDate": [20260105, 20260106, 20260107],
            "IsTradingDay": [1, 1, 0],
        }
    ).to_parquet(calendar_dir / "trading_days.parquet", index=False)
    st_dir = root / "6_ml_datasets" / "features_daily" / "dt=20261016"
    st_dir.mkdir(parents=True)
    pd.DataFrame({"symbol": ["600000.SH", "000001.SZ"], "is_st": [1, 0]}).to_parquet(
        st_dir / "data.parquet", index=False
    )
    return root


@pytest.mark.unit
def test_normalize_symbol_handles_suffix_prefix_and_case() -> None:
    values = pd.Series(["600036.SH", "000001.sz", "SH600036", "430001.BJ"])
    assert normalize_symbol(values).tolist() == [
        "SH600036",
        "SZ000001",
        "SH600036",
        "430001.BJ",
    ]


@pytest.mark.unit
def test_load_price_matrices_normalizes_and_drops_non_shsz(
    quantdb_root, monkeypatch
) -> None:
    monkeypatch.setattr(data_module, "DATA_ROOT", quantdb_root)
    close_raw, close_qfq, volume = load_price_matrices("2026-01-05", "2026-01-06")
    assert list(close_raw.columns) == ["SH600000", "SZ000001"]
    assert close_raw.loc[pd.Timestamp("2026-01-06"), "SH600000"] == 10.3
    assert close_qfq.loc[pd.Timestamp("2026-01-06"), "SH600000"] == 10.2
    assert volume.loc[pd.Timestamp("2026-01-05"), "SZ000001"] == 2000.0


@pytest.mark.unit
def test_load_calendar_filters_non_trading_days(quantdb_root, monkeypatch) -> None:
    monkeypatch.setattr(data_module, "DATA_ROOT", quantdb_root)
    calendar = load_calendar("2026-01-05", "2026-01-07")
    assert calendar.tolist() == [pd.Timestamp("2026-01-05"), pd.Timestamp("2026-01-06")]


@pytest.mark.unit
def test_load_st_symbols_returns_true_index(quantdb_root, monkeypatch) -> None:
    monkeypatch.setattr(data_module, "DATA_ROOT", quantdb_root)
    st = load_st_symbols("20261016")
    assert st.index.tolist() == ["SH600000"]
    assert bool(st["SH600000"])


@pytest.mark.unit
def test_load_index_closes_uses_prefix_symbols(quantdb_root, monkeypatch) -> None:
    monkeypatch.setattr(data_module, "DATA_ROOT", quantdb_root)
    closes = load_index_closes("2026-01-05", "2026-01-06")
    assert list(closes.columns) == ["SH000300"]
    assert closes.iloc[-1, 0] == 4040.0


@pytest.mark.unit
def test_load_prediction_panel_stored_wins_on_duplicate_date(
    tmp_path, quantdb_root, monkeypatch
) -> None:
    monkeypatch.setattr(data_module, "DATA_ROOT", quantdb_root)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    pd.DataFrame(
        {
            "symbol": ["600000.SH", "600000.SH"],
            "trade_date": [pd.Timestamp("2026-01-05"), pd.Timestamp("2026-01-06")],
            "pred": [0.1, 0.2],
            "label_return": [0.01, 0.02],
        }
    ).to_parquet(run_dir / "pred.parquet", index=False)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    pd.DataFrame(
        {
            "symbol": ["600000.SH", "600001.SH"],
            "trade_date": [pd.Timestamp("2026-01-06"), pd.Timestamp("2026-01-07")],
            "pred": [9.9, 0.3],
        }
    ).to_parquet(out_dir / "pred_extend_T5.parquet", index=False)

    panel, first_seen = load_prediction_panel(run_dir, out_dir, "T5", "2026-01-05")

    assert panel["source"].tolist().count("stored") == 2
    duplicate = panel[
        (panel["symbol"] == "SH600000")
        & (panel["trade_date"] == pd.Timestamp("2026-01-06"))
    ]
    assert duplicate["pred"].iloc[0] == pytest.approx(0.2)
    assert first_seen["SH600001"] == pd.Timestamp("2026-01-07")


@pytest.mark.unit
def test_build_market_panel_flags_and_listing_age(quantdb_root, monkeypatch) -> None:
    monkeypatch.setattr(data_module, "DATA_ROOT", quantdb_root)
    panel = build_market_panel("2026-01-05", "2026-01-06", "20261016")
    assert list(panel.dates) == [pd.Timestamp("2026-01-05"), pd.Timestamp("2026-01-06")]
    assert bool(panel.is_st["SH600000"])
    assert not bool(panel.limit_up.to_numpy().any())
    assert not bool(panel.suspended.to_numpy().any())

    first_seen = pd.Series(
        {"SH600000": pd.Timestamp("2026-01-05"), "SZ000001": pd.Timestamp("2026-01-05")}
    )
    attach_listing_age(panel, first_seen)
    assert panel.age_days.loc[pd.Timestamp("2026-01-06"), "SH600000"] == 1.0
