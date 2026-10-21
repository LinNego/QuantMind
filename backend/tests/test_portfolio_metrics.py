"""组合风险收益指标纯函数测试（机构级统计口径）。"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from backend.services.engine.inference import portfolio_metrics as pm

D0 = date(2026, 1, 5)


def _days(count: int) -> list[date]:
    return [D0 + timedelta(days=offset) for offset in range(count)]


@pytest.mark.unit
def test_daily_returns_first_day_is_none_and_missing_previous_is_none():
    assert pm.daily_returns([100, 110, 99]) == [
        None,
        pytest.approx(0.1),
        pytest.approx(-0.1),
    ]
    assert pm.daily_returns([0, 10]) == [None, None]


@pytest.mark.unit
def test_annualized_return_requires_minimum_sample():
    assert pm.annualized_return_pct(10.0, 19) is None
    value = pm.annualized_return_pct(10.0, 244)
    assert value == pytest.approx(10.0)


@pytest.mark.unit
def test_drawdown_marks_peak_trough_and_recovery():
    days = _days(5)
    equity = [100, 110, 99, 108, 115]
    out = pm.drawdown_stats(days, equity)

    assert out["max_drawdown_pct"] == pytest.approx(-10.0)
    assert out["peak_date"] == days[1].isoformat()
    assert out["trough_date"] == days[2].isoformat()
    assert out["recovery_date"] == days[4].isoformat()
    assert out["recovery_days"] == 2


@pytest.mark.unit
def test_drawdown_without_recovery_keeps_recovery_null():
    out = pm.drawdown_stats(_days(3), [100, 110, 99])
    assert out["max_drawdown_pct"] == pytest.approx(-10.0)
    assert out["recovery_date"] is None
    assert out["recovery_days"] is None


@pytest.mark.unit
def test_risk_metrics_golden_values():
    returns = [0.1, -0.1, 0.1]
    out = pm.risk_metrics(returns)

    assert out["sample_days"] == 3
    # 样本标准差 0.11547 × √244 ≈ 1.8035
    assert out["volatility_pct"] == pytest.approx(180.38, abs=0.02)
    assert out["sharpe_ratio"] == pytest.approx(4.5092, abs=0.001)
    assert out["downside_deviation_pct"] is not None
    assert out["positive_days"] == 2
    assert out["negative_days"] == 1
    assert out["best_day_pct"] == pytest.approx(10.0)
    assert out["worst_day_pct"] == pytest.approx(-10.0)


@pytest.mark.unit
def test_risk_metrics_insufficient_sample_returns_none():
    out = pm.risk_metrics([0.01])
    assert out["sample_days"] == 1
    assert out["volatility_pct"] is None
    assert out["sharpe_ratio"] is None


@pytest.mark.unit
def test_benchmark_stats_total_daily_and_monthly():
    days = [date(2026, 1, 29), date(2026, 1, 30), date(2026, 2, 2), date(2026, 2, 3)]
    closes = {days[0]: 100.0, days[1]: 102.0, days[2]: 101.0, days[3]: 104.0}

    out = pm.benchmark_stats(days, closes)

    assert out["coverage_pct"] == pytest.approx(100.0)
    assert out["return_pct"] == pytest.approx(4.0)
    assert out["daily_returns"][0] is None
    assert out["daily_returns"][1] == pytest.approx(0.02)
    assert out["monthly_returns"]["2026-01"] == pytest.approx(2.0)
    # 2 月基准 = 1 月末 102 → 104
    assert out["monthly_returns"]["2026-02"] == pytest.approx(
        104 / 102 * 100 - 100, abs=1e-4
    )


@pytest.mark.unit
def test_benchmark_stats_missing_day_is_not_zero():
    days = _days(3)
    out = pm.benchmark_stats(days, {days[0]: 100.0, days[2]: 110.0})

    assert out["coverage_pct"] == pytest.approx(66.67, abs=0.01)
    assert out["daily_returns"] == [None, None, None]
    # 首末有效价仍可算区间收益
    assert out["return_pct"] == pytest.approx(10.0)


@pytest.mark.unit
def test_excess_metrics_information_ratio():
    portfolio = [0.01, 0.02, -0.005, 0.0]
    benchmark = [0.0, 0.01, -0.01, 0.0]
    out = pm.excess_metrics(portfolio, benchmark)

    assert out["sample_days"] == 4
    assert out["avg_daily_excess_pct"] == pytest.approx(0.625)
    assert out["tracking_error_pct"] is not None
    assert out["information_ratio"] is not None


@pytest.mark.unit
def test_excess_metrics_ignores_missing_pairs():
    out = pm.excess_metrics([0.01, None, 0.02], [0.0, 0.01, None])
    assert out["sample_days"] == 1
    assert out["information_ratio"] is None


@pytest.mark.unit
def test_trade_stats_profit_factor_and_payoff():
    out = pm.trade_stats([10, -5, -2, 0], [100, -50, -20, 0], [2, 3, 4, 1])

    assert out["trades"] == 4
    assert out["wins"] == 1
    assert out["losses"] == 2
    assert out["flats"] == 1
    assert out["win_rate_pct"] == pytest.approx(25.0)
    assert out["profit_factor"] == pytest.approx(100 / 70, abs=1e-4)
    assert out["avg_win_pct"] == pytest.approx(10.0)
    assert out["avg_loss_pct"] == pytest.approx(-3.5)
    assert out["payoff_ratio"] == pytest.approx(10 / 3.5, abs=1e-4)
    assert out["avg_holding_days"] == pytest.approx(2.5)


@pytest.mark.unit
def test_market_day_stats_change_and_ma20_position():
    days = [D0 + timedelta(days=i) for i in range(21)]
    closes = {day: 100.0 + i for i, day in enumerate(days)}
    out = pm.market_day_stats(days, closes)

    last = out[days[-1]]
    assert last["change_pct"] == pytest.approx(1 / 119 * 100, abs=0.01)
    # 最后 20 日均线 = 101..120 的均值 110.5，收盘 120
    assert last["ma20_deviation_pct"] == pytest.approx(
        (120 / 110.5 - 1) * 100, abs=0.01
    )
    assert last["position"] == "above_ma20"
    assert out[days[0]]["ma20_deviation_pct"] is None
    assert out[days[0]]["position"] == "unknown"


@pytest.mark.unit
def test_change_pct_uses_previous_valid_close():
    days = _days(3)
    closes = {days[0]: 10.0, days[2]: 11.0}
    assert pm.change_pct(closes, days[2]) == pytest.approx(10.0)
    assert pm.change_pct(closes, days[1]) is None
