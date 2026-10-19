"""2026 TopK 回测核心口径测试。

覆盖：涨跌停向量化与 canonical 规则一致、费用/整手现金流、滚动桶时序、
信号到成交的一日滞后（无前视）、前向收益矩阵。
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from backend.scripts.backtest2026.data import MarketPanel, forward_return_matrix
from backend.scripts.backtest2026.filters import (
    compute_limit_matrices,
    limit_pct_vector,
    limit_price_cents,
    listing_age_days,
    signal_eligible_mask,
)
from backend.scripts.backtest2026.metrics import performance_summary
from backend.scripts.backtest2026.strategies import StrategySpec, run_strategy
from backend.services.simulation.services.local_market_data import (
    compute_limits,
    limit_pct,
)


def _panel(
    n_days: int = 8,
    symbols: tuple[str, ...] = ("SH600000", "SH600001", "SH600002"),
    price: float = 10.0,
) -> MarketPanel:
    dates = pd.bdate_range("2026-01-05", periods=n_days)
    close = pd.DataFrame(price, index=dates, columns=list(symbols), dtype=float)
    volume = pd.DataFrame(1_000_000.0, index=dates, columns=list(symbols))
    false_matrix = pd.DataFrame(False, index=dates, columns=list(symbols))
    return MarketPanel(
        dates=dates,
        full_calendar=dates,
        symbols=pd.Index(symbols),
        close_raw=close,
        close_qfq=close.copy(),
        volume=volume,
        limit_up=false_matrix.copy(),
        limit_down=false_matrix.copy(),
        suspended=false_matrix.copy(),
        is_st=pd.Series(False, index=list(symbols)),
        age_days=pd.DataFrame(200.0, index=dates, columns=list(symbols)),
        close_qfq_ffill=close.copy(),
    )


@pytest.mark.unit
def test_limit_pct_vector_matches_canonical_across_boards() -> None:
    symbols = pd.Index(
        ["SH600000", "SZ000001", "SZ300001", "SH688001", "SZ301001", "SH689009"]
    )
    is_st = pd.Series([False, True, False, False, True, False], index=symbols)
    for asof in (date(2026, 1, 5), date(2026, 7, 6)):
        vector = limit_pct_vector(symbols, is_st, asof)
        expected = [
            int(
                round(
                    float(limit_pct(sym, is_st=bool(is_st[sym]), trade_date=asof)) * 100
                )
            )
            for sym in symbols
        ]
        assert vector.tolist() == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("symbol", "pre_close", "is_st", "trade_date"),
    [
        ("SH600000", 10.0, False, date(2026, 5, 6)),
        ("SZ000001", 10.0, True, date(2026, 5, 6)),
        ("SZ300001", 10.0, False, date(2026, 5, 6)),
        ("SH688001", 7.77, False, date(2026, 9, 14)),
        ("SZ301001", 2.35, True, date(2026, 9, 14)),
        ("SH600000", 10.0, True, date(2026, 7, 6)),
    ],
)
def test_limit_price_cents_matches_canonical(
    symbol: str, pre_close: float, is_st: bool, trade_date: date
) -> None:
    canonical_up, canonical_down = compute_limits(
        symbol, pre_close, is_st=is_st, trade_date=trade_date
    )
    up, down = limit_price_cents(
        np.array([pre_close * 100.0]),
        limit_pct_vector(
            pd.Index([symbol]), pd.Series([is_st], index=[symbol]), trade_date
        ),
    )
    assert up[0] / 100.0 == pytest.approx(canonical_up)
    assert down[0] / 100.0 == pytest.approx(canonical_down)


@pytest.mark.unit
def test_compute_limit_matrices_flags_both_sides_and_suspension() -> None:
    dates = pd.to_datetime(["2026-05-06", "2026-05-07", "2026-05-08"])
    close = pd.DataFrame({"SH600000": [10.0, 11.0, 9.9]}, index=dates)
    volume = pd.DataFrame({"SH600000": [1000.0, 0.0, 1000.0]}, index=dates)
    limit_up, limit_down, suspended = compute_limit_matrices(
        close, volume, pd.Series(False, index=close.columns)
    )
    assert bool(limit_up.at[dates[1], "SH600000"])
    assert bool(limit_down.at[dates[2], "SH600000"])
    assert bool(suspended.at[dates[1], "SH600000"])
    assert not bool(limit_up.at[dates[0], "SH600000"])


@pytest.mark.unit
def test_listing_age_uses_full_calendar_not_window_start() -> None:
    full = pd.bdate_range("2016-01-04", "2026-01-09")
    window = full[-5:]
    first_seen = pd.Series({"SH600000": pd.Timestamp("2016-01-04")})
    age = listing_age_days(first_seen, window, pd.Index(["SH600000"]), full_dates=full)
    assert age.iloc[0, 0] > 2000


@pytest.mark.unit
def test_signal_eligible_mask_applies_four_gates() -> None:
    panel = _panel(n_days=3)
    day = panel.dates[0]
    pred = pd.Series({"SH600000": 1.0, "SH600001": 2.0, "SH600002": 3.0})
    panel.is_st = pd.Series({"SH600000": True, "SH600001": False, "SH600002": False})
    panel.limit_up.at[day, "SH600001"] = True
    panel.age_days.at[day, "SH600002"] = 10
    mask = signal_eligible_mask(
        pred,
        day,
        panel.limit_up,
        panel.limit_down,
        panel.suspended,
        panel.age_days,
        panel.is_st,
    )
    assert mask.tolist() == [False, False, False]


@pytest.mark.unit
def test_forward_return_matrix_uses_lag_and_horizon() -> None:
    dates = pd.bdate_range("2026-01-05", periods=6)
    close = pd.DataFrame(
        {"SH600000": [10.0, 11.0, 12.0, 13.0, 14.0, 15.0]}, index=dates
    )
    forward = forward_return_matrix(close, horizon=2, lag=1)
    assert forward.iloc[0, 0] == pytest.approx(13.0 / 11.0 - 1.0)
    assert forward.iloc[1, 0] == pytest.approx(14.0 / 12.0 - 1.0)
    assert np.isnan(forward.iloc[3, 0])


@pytest.mark.unit
def test_cohort_portfolio_lot_rounding_and_round_trip_fees() -> None:
    panel = _panel(n_days=4)
    pred = {day: pd.Series({"SH600000": 1.0}, dtype=float) for day in panel.dates}
    spec = StrategySpec(
        mode="cohort",
        k=1,
        horizon=2,
        initial_capital=100_000.0,
        per_name=100_000.0,
        commission=0.0003,
    )
    result = run_strategy(panel, pred, spec)
    trades = result.trades
    buys = trades[trades["side"] == "buy"]
    sells = trades[trades["side"] == "sell"]
    assert buys.iloc[0]["date"] == panel.dates[1]
    assert sells.iloc[0]["date"] == panel.dates[3]
    assert buys.iloc[0]["shares"] == 9_900
    assert buys.iloc[0]["fee"] == pytest.approx(29.7)
    assert sells.iloc[0]["fee"] == pytest.approx(29.7)
    assert result.equity["equity"].iloc[-1] == pytest.approx(100_000.0 - 59.4)


@pytest.mark.unit
def test_rolling_sells_oldest_bucket_and_signal_has_one_day_lag() -> None:
    symbols = ("SH600000", "SH600001", "SH600002", "SH600003")
    panel = _panel(n_days=8, symbols=symbols)
    pred = {}
    for index, day in enumerate(panel.dates):
        winner = symbols[index % len(symbols)]
        pred[day] = pd.Series(
            {symbol: (1.0 if symbol == winner else 0.0) for symbol in symbols}
        )
    spec = StrategySpec(
        mode="rolling",
        k=1,
        horizon=2,
        initial_capital=200_000.0,
        per_name=None,
        commission=0.0003,
    )
    result = run_strategy(panel, pred, spec)
    buys = result.trades[result.trades["side"] == "buy"].reset_index(drop=True)
    sells = result.trades[result.trades["side"] == "sell"].reset_index(drop=True)
    # 信号日 t 的票在 t+1 买入；t=0 的桶在 t+3 卖出。
    assert buys.loc[0, "symbol"] == symbols[0]
    assert buys.loc[0, "date"] == panel.dates[1]
    assert sells.loc[0, "symbol"] == symbols[0]
    assert sells.loc[0, "date"] == panel.dates[3]
    # 第 4 天买入的应是第 3 天信号选出的票（而不是当天信号）。
    fourth = buys[buys["date"] == panel.dates[4]].iloc[0]
    assert fourth["symbol"] == symbols[3]


@pytest.mark.unit
def test_performance_summary_reports_drawdown() -> None:
    dates = pd.bdate_range("2026-01-05", periods=4)
    equity = pd.Series([100.0, 110.0, 99.0, 105.0], index=dates)
    summary = performance_summary(equity)
    assert summary["total_return"] == pytest.approx(0.05)
    assert summary["max_drawdown"] == pytest.approx(-0.1)
