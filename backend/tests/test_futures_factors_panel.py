"""主力连续面板、合约面板与移仓复权。"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from backend.shared.futures_factors.expiry import TradingCalendar
from backend.shared.futures_factors.panel import (
    attach_dominant_contract,
    build_roll_adjusted_prices,
    combine_contract_panels,
    normalize_contract_panel,
    normalize_main_continuous,
    parse_contract_symbol,
    variety_of_main_symbol,
)


def _main_frame() -> pd.DataFrame:
    rows = [
        ("CU0.CN", "2026-01-05", 100.0, 101.0, 99.0, 100.0, 1000, 1_000_000),
        ("CU0.CN", "2026-01-06", 102.0, 103.0, 101.0, 102.0, 1100, 1_100_000),
        ("CU0.CN", "2026-01-07", 111.0, 112.0, 110.0, 111.0, 1200, 1_200_000),
        ("CL.FUT", "2026-01-05", 70.0, 71.0, 69.0, 70.0, 0, 0),
        ("CU0.CN", "2026-01-05", 100.0, 101.0, 99.0, 100.0, 1000, 1_000_000),
    ]
    df = pd.DataFrame(
        rows,
        columns=[
            "symbol",
            "time",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "amount",
        ],
    )
    df["time"] = pd.to_datetime(df["time"])
    return df


def _contract_frame() -> pd.DataFrame:
    rows = [
        # symbol, time, close, open_interest, last_trade_date
        ("CU2601.CN", "2026-01-05", 100.0, 1000, "2026-01-15"),
        ("CU2602.CN", "2026-01-05", 110.0, 500, "2026-02-13"),
        ("CU2601.CN", "2026-01-06", 102.0, 900, "2026-01-15"),
        ("CU2602.CN", "2026-01-06", 111.0, 1200, "2026-02-13"),
        ("CU2602.CN", "2026-01-07", 112.0, 1300, "2026-02-13"),
        ("CU2603.CN", "2026-01-07", 120.0, 100, "2026-03-16"),
    ]
    df = pd.DataFrame(
        rows,
        columns=["symbol", "time", "close", "open_interest", "last_trade_date"],
    )
    df["time"] = pd.to_datetime(df["time"])
    df["last_trade_date"] = pd.to_datetime(df["last_trade_date"])
    return df


class TestNormalizeMainContinuous:
    def test_filters_foreign_and_dedupes(self):
        out = normalize_main_continuous(_main_frame())
        assert set(out["symbol"]) == {"CU0.CN"}
        assert len(out) == 3
        assert out["variety"].tolist() == ["CU", "CU", "CU"]

    def test_requires_ohlcv_columns(self):
        with pytest.raises(ValueError, match="缺少列"):
            normalize_main_continuous(pd.DataFrame({"symbol": ["CU0.CN"]}))

    def test_variety_of_main_symbol(self):
        assert variety_of_main_symbol("CU0.CN") == "CU"
        assert variety_of_main_symbol("A0.CN") == "A"

    def test_drops_rows_forward_filled_across_holidays(self):
        # 2005 国庆休市，数据源把 09-30 整行复制到 10-03。
        # 假交易日会让 1~5 日标签凭空多出 0 收益样本，必须剔除。
        df = pd.DataFrame(
            [
                (
                    "CF0.CN",
                    "2005-09-30",
                    14835.0,
                    14995.0,
                    14790.0,
                    14780.0,
                    36596,
                    5.4e8,
                ),
                (
                    "CF0.CN",
                    "2005-10-03",
                    14835.0,
                    14995.0,
                    14790.0,
                    14780.0,
                    36596,
                    5.4e8,
                ),
                (
                    "CF0.CN",
                    "2005-10-13",
                    14835.0,
                    15630.0,
                    15430.0,
                    14780.0,
                    42300,
                    6.2e8,
                ),
            ],
            columns=[
                "symbol",
                "time",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "amount",
            ],
        )
        df["time"] = pd.to_datetime(df["time"])
        out = normalize_main_continuous(df)
        assert out["time"].dt.day.tolist() == [30, 13]

    def test_keeps_quiet_day_when_volume_differs(self):
        df = pd.DataFrame(
            [
                ("CU0.CN", "2026-01-05", 100.0, 100.0, 100.0, 100.0, 0, 0.0),
                ("CU0.CN", "2026-01-06", 100.0, 100.0, 100.0, 100.0, 5, 500.0),
            ],
            columns=[
                "symbol",
                "time",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "amount",
            ],
        )
        df["time"] = pd.to_datetime(df["time"])
        assert len(normalize_main_continuous(df)) == 2

    def test_repairs_ohlc_out_of_bounds(self):
        # 源数据 high < close / low > open 的越界行收敛到 OHLC 自洽区间。
        df = pd.DataFrame(
            [
                (
                    "CF0.CN",
                    "2005-10-13",
                    14835.0,
                    15630.0,
                    15430.0,
                    14780.0,
                    42300,
                    6.2e8,
                ),
                ("AL0.CN", "2006-01-04", 19766.0, 20002.0, 19797.0, 19961.0, 100, 2e6),
            ],
            columns=[
                "symbol",
                "time",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "amount",
            ],
        )
        df["time"] = pd.to_datetime(df["time"])
        out = normalize_main_continuous(df).set_index("symbol")
        assert out.loc["CF0.CN", "low"] == pytest.approx(14780.0)
        assert out.loc["AL0.CN", "low"] == pytest.approx(19766.0)
        assert out.loc["AL0.CN", "high"] == pytest.approx(20002.0)
        assert (out["high"] >= out[["open", "close"]].max(axis=1)).all()
        assert (out["low"] <= out[["open", "close"]].min(axis=1)).all()

    def test_missing_high_stays_missing(self):
        # 不把缺失的高点填成收盘价——那会伪造出零振幅交易日。
        df = pd.DataFrame(
            [
                ("CU0.CN", "2026-01-05", 100.0, np.nan, 99.0, 101.0, 10, 1000.0),
            ],
            columns=[
                "symbol",
                "time",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "amount",
            ],
        )
        df["time"] = pd.to_datetime(df["time"])
        out = normalize_main_continuous(df)
        assert np.isnan(out["high"].iloc[0])


class TestParseContractSymbol:
    def test_parses_domestic_contract(self):
        cc = parse_contract_symbol("CU2706.CN")
        assert (cc.variety, cc.year, cc.month) == ("CU", 2027, 6)

    def test_rejects_foreign_contract(self):
        with pytest.raises(ValueError):
            parse_contract_symbol("CL.FUT")


class TestNormalizeContractPanel:
    def test_adds_variety_and_drops_bad_prices(self):
        raw = _contract_frame()
        raw.loc[0, "close"] = np.nan
        out = normalize_contract_panel(raw)
        assert "variety" in out.columns
        assert out["close"].notna().all()
        assert set(out["variety"]) == {"CU"}


class TestDominantContract:
    def test_picks_max_open_interest(self):
        out = attach_dominant_contract(normalize_contract_panel(_contract_frame()))
        dom = out[out["is_dominant"]].sort_values("time")
        assert dom["symbol"].tolist() == ["CU2601.CN", "CU2602.CN", "CU2602.CN"]

    def test_tie_breaks_toward_nearer_expiry(self):
        df = _contract_frame()
        df.loc[df["symbol"] == "CU2602.CN", "open_interest"] = 1000
        df.loc[df["symbol"] == "CU2601.CN", "open_interest"] = 1000
        out = attach_dominant_contract(normalize_contract_panel(df))
        first = out[(out["time"] == pd.Timestamp("2026-01-05")) & out["is_dominant"]]
        assert first["symbol"].tolist() == ["CU2601.CN"]


class TestRollAdjustment:
    def test_adjusts_roll_jumps_and_flags(self):
        main = normalize_main_continuous(_main_frame())
        contracts = normalize_contract_panel(_contract_frame())
        out = build_roll_adjusted_prices(main, contracts).sort_values("time")

        # 复权后 d2 收益 = B 合约自身收益 111/110 - 1
        r1 = out["close"].iloc[1] / out["close"].iloc[0] - 1
        r2 = out["close"].iloc[2] / out["close"].iloc[1] - 1
        assert r1 == pytest.approx(111 / 110 - 1)
        assert r2 == pytest.approx(112 / 111 - 1)
        # 最后一日复权价 = 原始主力价
        assert out["close"].iloc[-1] == pytest.approx(111.0)
        assert out["roll_flag"].tolist() == [0, 1, 0]
        assert out["meta_roll_adjusted"].tolist() == [1, 1, 1]

    def test_splices_pre_contract_history_without_jump(self):
        main = normalize_main_continuous(_main_frame())
        extra = pd.DataFrame(
            [
                {
                    "symbol": "CU0.CN",
                    "time": pd.Timestamp("2026-01-02"),
                    "open": 90.0,
                    "high": 91.0,
                    "low": 89.0,
                    "close": 90.0,
                    "volume": 900,
                    "amount": 900_000,
                    "variety": "CU",
                }
            ]
        )
        main = pd.concat([extra, main], ignore_index=True)
        out = build_roll_adjusted_prices(
            main, normalize_contract_panel(_contract_frame())
        ).sort_values("time")
        # 跨边界收益应等于原始主力收益 100/90-1，而不是复权因子造成的假跳
        assert out["close"].iloc[1] / out["close"].iloc[0] - 1 == pytest.approx(
            100 / 90 - 1
        )
        assert out["meta_roll_adjusted"].iloc[0] == 0
        assert out["meta_roll_adjusted"].iloc[1] == 1

    def test_missing_contract_data_leaves_prices_untouched(self):
        main = normalize_main_continuous(_main_frame())
        empty = pd.DataFrame(
            columns=["symbol", "time", "close", "open_interest", "last_trade_date"]
        )
        out = build_roll_adjusted_prices(main, empty).sort_values("time")
        pd.testing.assert_series_equal(
            out["close"], main.sort_values("time")["close"], check_names=False
        )
        assert out["meta_roll_adjusted"].sum() == 0


class TestCombineContractPanels:
    def test_dedupes_by_symbol_and_time(self):
        a = normalize_contract_panel(_contract_frame())
        b = a.copy()
        out = combine_contract_panels([a, b])
        assert len(out) == len(a)
        assert not out.duplicated(["symbol", "time"]).any()


class TestCalendarFromMain:
    def test_calendar_comes_from_traded_dates(self):
        from backend.shared.futures_factors.panel import calendar_from_main

        cal = calendar_from_main(normalize_main_continuous(_main_frame()))
        assert isinstance(cal, TradingCalendar)
        assert cal.dates[0] == date(2026, 1, 5)
        assert len(cal.dates) == 3
