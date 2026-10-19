"""Top10 / Top20 / 滚动 Top5 组合评估测试。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backend.scripts.backtest2026.data import MarketPanel
from backend.scripts.backtest2026.multi_portfolio import (
    build_spec,
    common_end_date,
    evaluate_portfolio,
    run_family,
)
from backend.scripts.backtest2026.strategies import StrategyResult, StrategySpec


def _panel(
    n_days: int = 10,
    symbols: tuple[str, ...] = ("SH600000", "SH600001", "SH600002"),
) -> MarketPanel:
    dates = pd.bdate_range("2026-01-05", periods=n_days)
    close = pd.DataFrame(10.0, index=dates, columns=list(symbols))
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
        close_qfq_ffill=close.copy(),
    )


def _fake_result(
    dates: pd.DatetimeIndex, daily_return: float = 0.001
) -> StrategyResult:
    equity = 2_000_000.0 * np.power(1.0 + daily_return, np.arange(len(dates)))
    frame = pd.DataFrame(
        {
            "equity": equity,
            "cash": equity,
            "market_value": np.zeros(len(dates)),
            "buckets": np.zeros(len(dates)),
        },
        index=dates,
    )
    spec = StrategySpec(
        mode="cohort",
        k=10,
        horizon=5,
        initial_capital=2_000_000.0,
        per_name=200_000.0,
        commission=0.0003,
    )
    return StrategyResult(
        spec=spec,
        equity=frame,
        trades=pd.DataFrame(),
        skips=pd.DataFrame(),
        signal_days=len(dates),
        selected_total=0,
    )


@pytest.mark.unit
def test_build_spec_sets_capital_and_lot_budget_per_family() -> None:
    top10 = build_spec("top10", 5)
    top20 = build_spec("top20", 10)
    rolling = build_spec("rolling", 5)

    assert (top10.mode, top10.k, top10.per_name) == ("cohort", 10, 200_000.0)
    assert (top20.mode, top20.k, top20.per_name) == ("cohort", 20, 100_000.0)
    assert (rolling.mode, rolling.k, rolling.per_name) == ("rolling", 5, None)
    assert top10.initial_capital == pytest.approx(2_000_000.0)
    assert top10.commission == pytest.approx(0.0003)


@pytest.mark.unit
def test_build_spec_rejects_unknown_family() -> None:
    with pytest.raises(ValueError, match="未知组合类型"):
        build_spec("top3", 5)


@pytest.mark.unit
def test_common_end_date_uses_two_day_lag_after_last_signal() -> None:
    panel = _panel()
    pred = {
        panel.dates[0]: pd.Series({"SH600000": 1.0}),
        panel.dates[1]: pd.Series({"SH600000": 1.0}),
    }

    assert common_end_date(panel, pred, lag=2) == panel.dates[3]

    with pytest.raises(ValueError, match="价格面板不足"):
        common_end_date(panel, {panel.dates[-1]: pred[panel.dates[0]]}, lag=2)


@pytest.mark.unit
def test_evaluate_portfolio_aligns_benchmark_and_reports_excess() -> None:
    dates = pd.bdate_range("2026-01-05", periods=10)
    result = _fake_result(dates)
    benchmark = pd.Series(0.0, index=dates)

    evaluation = evaluate_portfolio(
        result,
        family="top10",
        horizon=5,
        model="LGB",
        benchmark_returns=benchmark,
        common_end=dates[-1],
    )

    total = 1.001**9 - 1.0
    assert evaluation.performance["total_return"] == pytest.approx(total)
    assert evaluation.stats["benchmark_total"] == pytest.approx(0.0)
    assert evaluation.stats["active_return"] == pytest.approx(total)
    assert evaluation.stats["open_positions"] == 0
    assert evaluation.round_trip["n"] == 0
    assert len(evaluation.daily_returns) == 9
    assert evaluation.equity_common.index[-1] == dates[-1]
    assert evaluation.equity_full.index[-1] == dates[-1]


@pytest.mark.unit
def test_evaluate_portfolio_truncates_common_window() -> None:
    dates = pd.bdate_range("2026-01-05", periods=10)
    result = _fake_result(dates)
    benchmark = pd.Series(0.0, index=dates)

    evaluation = evaluate_portfolio(
        result,
        family="top10",
        horizon=5,
        model="LGB",
        benchmark_returns=benchmark,
        common_end=dates[5],
    )

    assert evaluation.equity_common.index[-1] == dates[5]
    assert evaluation.equity_full.index[-1] == dates[-1]
    assert (
        evaluation.full_performance["total_return"]
        > evaluation.performance["total_return"]
    )
    assert len(evaluation.daily_returns) == 5


@pytest.mark.unit
def test_evaluate_portfolio_rejects_short_common_window() -> None:
    dates = pd.bdate_range("2026-01-05", periods=10)
    result = _fake_result(dates)

    with pytest.raises(ValueError, match="共同窗口过短"):
        evaluate_portfolio(
            result,
            family="top10",
            horizon=5,
            model="LGB",
            benchmark_returns=pd.Series(0.0, index=dates),
            common_end=dates[1],
        )


@pytest.mark.unit
def test_run_family_produces_cost_sensitivity_variants() -> None:
    panel = _panel()
    symbols = panel.symbols
    pred = {}
    for day in panel.dates[:7]:
        values = {
            symbol: float(len(symbols) - position)
            for position, symbol in enumerate(symbols)
        }
        pred[day] = pd.Series(values, dtype=float)
    benchmark = pd.Series(0.0, index=panel.dates)
    common_end = common_end_date(panel, pred)

    evaluation = run_family(
        panel,
        pd.DataFrame(),
        family="top10",
        horizon=2,
        model="LGB",
        benchmark_returns=benchmark,
        common_end=common_end,
        pred_by_date=pred,
    )

    assert set(evaluation.variants) == {
        "baseline",
        "stamp_duty_5bp",
        "slippage_5bp",
    }
    baseline = evaluation.variants["baseline"]
    assert baseline["total_return"] == pytest.approx(
        evaluation.performance["total_return"]
    )
    assert (
        evaluation.variants["stamp_duty_5bp"]["total_return"] < baseline["total_return"]
    )
    assert (
        evaluation.variants["slippage_5bp"]["total_return"] < baseline["total_return"]
    )
    assert evaluation.trades_frame is not None
    assert not evaluation.trades_frame.empty
