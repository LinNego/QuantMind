"""回测指标口径测试：IC、方向胜率、组合绩效与台账。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backend.scripts.backtest2026.data import MarketPanel
from backend.scripts.backtest2026.metrics import (
    index_summary,
    max_drawdown,
    monthly_equity,
    monthly_quality,
    performance_summary,
    quality_by_day,
    summarize_quality,
    trade_summary,
)

SYMBOLS = ("SH600000", "SH600001", "SH600002", "SH600003", "SH600004")


def _panel(n_days: int = 3) -> MarketPanel:
    dates = pd.bdate_range("2026-01-05", periods=n_days)
    close = pd.DataFrame(10.0, index=dates, columns=list(SYMBOLS))
    false_matrix = pd.DataFrame(False, index=dates, columns=list(SYMBOLS))
    return MarketPanel(
        dates=dates,
        full_calendar=dates,
        symbols=pd.Index(SYMBOLS),
        close_raw=close,
        close_qfq=close.copy(),
        volume=pd.DataFrame(1_000_000.0, index=dates, columns=list(SYMBOLS)),
        limit_up=false_matrix.copy(),
        limit_down=false_matrix.copy(),
        suspended=false_matrix.copy(),
        is_st=pd.Series(False, index=list(SYMBOLS)),
        age_days=pd.DataFrame(200.0, index=dates, columns=list(SYMBOLS)),
        close_qfq_ffill=close.copy(),
    )


@pytest.mark.unit
def test_quality_by_day_ranks_returns_and_applies_filters() -> None:
    panel = _panel()
    pred = pd.Series({symbol: 5.0 - index for index, symbol in enumerate(SYMBOLS)})
    forward = pd.DataFrame(
        [
            {symbol: 0.05 - index * 0.01 for index, symbol in enumerate(SYMBOLS)}
            for _ in panel.dates
        ],
        index=panel.dates,
    )
    pred_by_date = dict.fromkeys(panel.dates, pred)
    panel.is_st.loc["SH600002"] = True

    daily = quality_by_day(pred_by_date, forward, panel, topk=2, label_matrix=forward)

    assert daily["ic_eligible"].iloc[0] == pytest.approx(1.0)
    assert daily["ic_label"].iloc[0] == pytest.approx(1.0)
    assert daily["n_eligible"].iloc[0] == 4
    assert daily["top_ret"].iloc[0] == pytest.approx(0.045)
    assert daily["bottom_ret"].iloc[0] == pytest.approx(0.015)
    assert daily["long_short"].iloc[0] == pytest.approx(0.03)
    assert daily["top_hit"].iloc[0] == 1.0
    assert daily["hit_all"].iloc[0] == 1.0


@pytest.mark.unit
def test_quality_by_day_drops_suspended_and_missing_predictions() -> None:
    panel = _panel(n_days=2)
    pred = pd.Series(
        {symbol: float(len(SYMBOLS) - index) for index, symbol in enumerate(SYMBOLS)}
    )
    pred["SH600004"] = np.nan
    panel.suspended.loc[:, "SH600003"] = True
    forward = pd.DataFrame(0.01, index=panel.dates, columns=list(SYMBOLS))
    daily = quality_by_day(
        {panel.dates[0]: pred},
        forward,
        panel,
        topk=2,
        exclude_new=False,
    )
    assert daily["n_eligible"].iloc[0] == 3


@pytest.mark.unit
def test_summarize_quality_icir_is_unannualized() -> None:
    daily = pd.DataFrame(
        {
            "ic_eligible": [0.1, 0.2, 0.3],
            "ic_all": [0.1, 0.2, 0.3],
            "ic_label": [0.1, 0.2, 0.3],
            "hit_all": [0.5, 0.5, 0.5],
            "top_ret": [0.01, 0.02, 0.03],
            "top_hit": [0.5, 0.5, 0.5],
            "bottom_ret": [0.0, 0.0, 0.0],
            "long_short": [0.01, 0.02, 0.03],
        }
    )
    summary = summarize_quality(daily)
    assert summary["ic_eligible_mean"] == pytest.approx(0.2)
    assert summary["icir"] == pytest.approx(2.0)
    assert summary["ic_positive_ratio"] == 1.0
    assert summary["days"] == 3


@pytest.mark.unit
def test_monthly_quality_and_monthly_equity() -> None:
    dates = pd.to_datetime(["2026-01-05", "2026-01-30", "2026-02-02", "2026-02-27"])
    daily = pd.DataFrame(
        {
            "ic_eligible": [0.1, 0.3, 0.2, 0.4],
            "hit_all": [0.5] * 4,
            "top_ret": [0.01, 0.03, 0.02, 0.04],
            "top_hit": [0.5] * 4,
            "long_short": [0.01, 0.03, 0.02, 0.04],
            "n_eligible": [100, 100, 100, 100],
        },
        index=dates,
    )
    quality = monthly_quality(daily)
    assert quality.loc[pd.Period("2026-01", "M"), "ic_eligible"] == pytest.approx(0.2)

    equity = pd.Series([100.0, 110.0, 105.0, 115.0], index=dates)
    monthly = monthly_equity(equity)
    assert monthly.loc[pd.Period("2026-01", "M")] == pytest.approx(0.10)
    assert monthly.loc[pd.Period("2026-02", "M")] == pytest.approx(115.0 / 110.0 - 1.0)


@pytest.mark.unit
def test_performance_summary_handles_short_series() -> None:
    assert performance_summary(pd.Series(dtype=float)) == {}
    summary = performance_summary(pd.Series([100.0]))
    assert summary == {}
    assert np.isnan(max_drawdown(pd.Series(dtype=float)))


@pytest.mark.unit
def test_trade_summary_round_trips_fees_and_deferral() -> None:
    trades = pd.DataFrame(
        [
            {
                "bucket": 1,
                "label": "20260105",
                "side": "buy",
                "signal_date": pd.Timestamp("2026-01-05"),
                "date": pd.Timestamp("2026-01-06"),
                "symbol": "SH600000",
                "shares": 1000,
                "price": 10.0,
                "amount": 10_000.0,
                "fee": 3.0,
                "net_pnl": np.nan,
                "exit_reason": "",
                "scheduled_exit": pd.Timestamp("2026-01-09"),
            },
            {
                "bucket": 1,
                "label": "20260105",
                "side": "sell",
                "signal_date": pd.Timestamp("2026-01-05"),
                "date": pd.Timestamp("2026-01-12"),
                "symbol": "SH600000",
                "shares": 1000,
                "price": 11.0,
                "amount": 11_000.0,
                "fee": 3.3,
                "net_pnl": 993.7,
                "exit_reason": "due",
                "scheduled_exit": pd.Timestamp("2026-01-09"),
            },
        ]
    )
    summary = trade_summary(trades, initial_capital=10_000.0)
    assert summary["n_round_trips"] == 1
    assert summary["total_fees"] == pytest.approx(6.3)
    assert summary["avg_trade_return"] == pytest.approx(993.7 / 10_003.0)
    assert summary["trade_win_rate"] == 1.0
    assert summary["deferred_exits"] == 1
    assert summary["forced_exits"] == 0


@pytest.mark.unit
def test_index_summary_clips_window() -> None:
    dates = pd.bdate_range("2026-01-05", periods=5)
    closes = pd.DataFrame({"SH000300": [100, 101, 102, 103, 104]}, index=dates)
    summary = index_summary(closes, dates[1], dates[3])
    assert summary["SH000300"] == pytest.approx(103.0 / 101.0 - 1.0)
