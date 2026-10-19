"""多模型预测质量与等权基准的单元测试。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backend.scripts.backtest2026.data import MarketPanel
from backend.scripts.backtest2026.multi_quality import (
    benchmark_daily,
    decile_frame,
    monthly_quality_multi,
    quality_by_day_multi,
    summarize_quality_multi,
)


def _panel(
    dates: pd.DatetimeIndex,
    symbols: tuple[str, ...],
    close: pd.DataFrame,
) -> MarketPanel:
    false_matrix = pd.DataFrame(False, index=dates, columns=list(symbols))
    return MarketPanel(
        dates=dates,
        full_calendar=dates,
        symbols=pd.Index(symbols),
        close_raw=close.copy(),
        close_qfq=close.copy(),
        volume=pd.DataFrame(1_000_000.0, index=dates, columns=list(symbols)),
        limit_up=false_matrix.copy(),
        limit_down=false_matrix.copy(),
        suspended=false_matrix.copy(),
        is_st=pd.Series(False, index=list(symbols)),
        age_days=pd.DataFrame(200.0, index=dates, columns=list(symbols)),
        close_qfq_ffill=close.ffill(),
    )


@pytest.mark.unit
def test_benchmark_matches_strategy_execution_timing() -> None:
    dates = pd.bdate_range("2026-01-05", periods=4)
    symbols = ("SH600000", "SH600001", "SH600002")
    close = pd.DataFrame(
        {
            "SH600000": [10.0, 10.0, 11.0, 11.0],
            "SH600001": [20.0, 20.0, 22.0, 22.0],
            "SH600002": [30.0, 30.0, 33.0, 33.0],
        },
        index=dates,
    )
    panel = _panel(dates, symbols, close)
    pred = {dates[0]: pd.Series({"SH600000": 1.0, "SH600001": 2.0, "SH600002": 3.0})}

    frame = benchmark_daily(panel, pred, 2_000_000.0, lag=2)

    assert list(frame.index) == [dates[1], dates[2]]
    assert frame.loc[dates[1], "return"] == pytest.approx(0.0)
    assert frame.loc[dates[2], "return"] == pytest.approx(0.10)
    assert frame.loc[dates[2], "n"] == 3
    assert frame.loc[dates[2], "equity"] == pytest.approx(2_200_000.0)


@pytest.mark.unit
def test_benchmark_drops_entry_day_blocked_symbols() -> None:
    dates = pd.bdate_range("2026-01-05", periods=4)
    symbols = ("SH600000", "SH600001", "SH600002")
    close = pd.DataFrame(
        {
            "SH600000": [10.0, 10.0, 11.0, 11.0],
            "SH600001": [20.0, 20.0, 22.0, 22.0],
            "SH600002": [30.0, 30.0, 33.0, 33.0],
        },
        index=dates,
    )
    panel = _panel(dates, symbols, close)
    panel.limit_up.at[dates[1], "SH600000"] = True
    pred = {dates[0]: pd.Series({"SH600000": 1.0, "SH600001": 2.0, "SH600002": 3.0})}

    frame = benchmark_daily(panel, pred, 2_000_000.0, lag=2)

    assert frame.loc[dates[2], "n"] == 2
    assert frame.loc[dates[2], "return"] == pytest.approx(0.10)


@pytest.mark.unit
def test_quality_topk_and_deciles_are_monotone() -> None:
    dates = pd.bdate_range("2026-01-05", periods=2)
    symbols = tuple(f"SH{600000 + index}" for index in range(40))
    close = pd.DataFrame(10.0, index=dates, columns=list(symbols))
    panel = _panel(dates, symbols, close)
    pred = pd.Series(np.arange(40, dtype=float), index=list(symbols))
    forward = pd.DataFrame(
        [np.arange(40, dtype=float) * 0.001], index=[dates[0]], columns=symbols
    )

    daily = quality_by_day_multi({dates[0]: pred}, forward, panel)

    row = daily.loc[dates[0]]
    assert row["n_eligible"] == 40
    assert row["ic_eligible"] == pytest.approx(1.0)
    assert row["top10_hit"] == pytest.approx(1.0)
    assert row["top10_dir"] == pytest.approx(1.0)
    assert row["top10_ret"] > row["top20_ret"] > 0
    assert row["ls10"] > 0
    assert row["d10"] > row["d1"]


@pytest.mark.unit
def test_quality_summary_reports_ic_and_monotonicity() -> None:
    dates = pd.bdate_range("2026-01-05", periods=2)
    symbols = tuple(f"SH{600000 + index}" for index in range(40))
    close = pd.DataFrame(10.0, index=dates, columns=list(symbols))
    panel = _panel(dates, symbols, close)
    pred = pd.Series(np.arange(40, dtype=float), index=list(symbols))
    forward = pd.DataFrame(
        [np.arange(40, dtype=float) * 0.001], index=[dates[0]], columns=symbols
    )
    daily = quality_by_day_multi({dates[0]: pred}, forward, panel)

    summary = summarize_quality_multi(daily, horizon=5, n_boot=200)

    assert summary["days"] == 1
    assert summary["ic_eligible_mean"] == pytest.approx(1.0)
    assert summary["ic_positive_ratio"] == pytest.approx(1.0)
    assert summary["decile_monotonicity"] == pytest.approx(1.0)
    assert summary["top_bottom_spread"] > 0
    assert len(summary["decile_returns"]) == 10
    assert "2026-01" in summary["monthly"]


@pytest.mark.unit
def test_quality_monthly_and_decile_frame_shapes() -> None:
    dates = pd.bdate_range("2026-01-05", periods=2)
    symbols = tuple(f"SH{600000 + index}" for index in range(40))
    close = pd.DataFrame(10.0, index=dates, columns=list(symbols))
    panel = _panel(dates, symbols, close)
    pred = pd.Series(np.arange(40, dtype=float), index=list(symbols))
    forward = pd.DataFrame(
        [np.arange(40, dtype=float) * 0.001], index=[dates[0]], columns=symbols
    )
    daily = quality_by_day_multi({dates[0]: pred}, forward, panel)

    monthly = monthly_quality_multi(daily)
    deciles = decile_frame({"LGB": daily})

    assert list(monthly.index) == ["2026-01"]
    assert "top10_ret" in monthly.columns
    assert list(deciles.columns) == [f"D{number}" for number in range(1, 11)]
    assert deciles.loc["LGB", "D10"] > deciles.loc["LGB", "D1"]


@pytest.mark.unit
def test_quality_rejects_days_with_too_few_eligible_names() -> None:
    dates = pd.bdate_range("2026-01-05", periods=2)
    symbols = tuple(f"SH{600000 + index}" for index in range(40))
    close = pd.DataFrame(10.0, index=dates, columns=list(symbols))
    panel = _panel(dates, symbols, close)
    panel.is_st = pd.Series(True, index=list(symbols))
    pred = pd.Series(np.arange(40, dtype=float), index=list(symbols))
    forward = pd.DataFrame(
        [np.arange(40, dtype=float) * 0.001], index=[dates[0]], columns=symbols
    )

    with pytest.raises(RuntimeError, match="没有可评估"):
        quality_by_day_multi({dates[0]: pred}, forward, panel)
