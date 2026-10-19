"""多模型组合评估：Top10 / Top20 / 滚动 Top5。

统一评估窗口到「最后一个信号日 + 2 个交易日」：所有模型、所有组合与等权基准
覆盖完全相同的日期。完整持有期（最后一批到期卖出）单独保留，用于交易级统计。
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np
import pandas as pd

from .config import COMMISSION_RATE, INITIAL_CAPITAL, ROLLING_K
from .data import MarketPanel
from .metrics import (
    monthly_equity,
    performance_summary,
    trade_summary,
)
from .report_pdf_data import (
    drawdown_stats,
    round_trip_stats,
    round_trips,
    symbol_attribution,
)
from .strategies import StrategyResult, StrategySpec, build_pred_by_date, run_strategy
from .strict_stats import block_bootstrap_mean

FAMILY_TITLES = {
    "top10": "Top10（每只 20 万，合计 200 万）",
    "top20": "Top20（每只 10 万，合计 200 万）",
    "rolling": "滚动 Top5（每日 5 只）",
}
FAMILY_ORDER = ("top10", "top20", "rolling")
TOP10_K = 10
TOP10_PER_NAME = 200_000.0


def build_spec(family: str, horizon: int) -> StrategySpec:
    if family == "top10":
        return StrategySpec(
            mode="cohort",
            k=TOP10_K,
            horizon=horizon,
            initial_capital=INITIAL_CAPITAL,
            per_name=TOP10_PER_NAME,
            commission=COMMISSION_RATE,
        )
    if family == "top20":
        return StrategySpec(
            mode="cohort",
            k=20,
            horizon=horizon,
            initial_capital=INITIAL_CAPITAL,
            per_name=INITIAL_CAPITAL / 20.0,
            commission=COMMISSION_RATE,
        )
    if family == "rolling":
        return StrategySpec(
            mode="rolling",
            k=ROLLING_K,
            horizon=horizon,
            initial_capital=INITIAL_CAPITAL,
            per_name=None,
            commission=COMMISSION_RATE,
        )
    raise ValueError(f"未知组合类型: {family}")


def common_end_date(
    panel: MarketPanel, pred_by_date: dict[pd.Timestamp, pd.Series], lag: int = 2
) -> pd.Timestamp:
    last_signal = max(pred_by_date)
    position = {day: i for i, day in enumerate(panel.dates)}
    idx = position[last_signal] + lag
    if idx >= len(panel.dates):
        raise ValueError("价格面板不足以覆盖最后一个信号日的持有期")
    return panel.dates[idx]


def cost_variants(
    panel: MarketPanel,
    pred_by_date: dict[pd.Timestamp, pd.Series],
    base_spec: StrategySpec,
    baseline: dict[str, float],
    *,
    common_end: pd.Timestamp,
    stamp: float,
    slippage: float,
) -> dict[str, dict[str, float]]:
    """成本敏感性：基准、卖出印花税 5bp、双边滑点 5bp。"""
    out: dict[str, dict[str, float]] = {"baseline": baseline}
    for name, variant in (
        ("stamp_duty_5bp", replace(base_spec, stamp=stamp)),
        ("slippage_5bp", replace(base_spec, slippage=slippage)),
    ):
        result = run_strategy(panel, pred_by_date, variant)
        equity = result.equity["equity"].dropna().loc[:common_end]
        performance = performance_summary(equity)
        out[name] = {
            "total_return": performance.get("total_return", float("nan")),
            "annual_return": performance.get("annual_return", float("nan")),
            "sharpe": performance.get("sharpe", float("nan")),
            "max_drawdown": performance.get("max_drawdown", float("nan")),
        }
    return out


def _downside_deviation(returns: pd.Series) -> float:
    values = returns.dropna().to_numpy(dtype=np.float64)
    if len(values) == 0:
        return float("nan")
    downside = np.minimum(values, 0.0)
    return float(np.sqrt(np.mean(downside**2)))


@dataclass
class PortfolioEvaluation:
    family: str
    horizon: int
    model: str
    performance: dict[str, object]
    full_performance: dict[str, object]
    trades: dict[str, object]
    round_trip: dict[str, float]
    drawdown: dict[str, object]
    stats: dict[str, object]
    monthly: dict[str, float]
    daily_returns: pd.Series
    excess: pd.Series
    equity_common: pd.Series
    equity_full: pd.Series
    attribution: pd.DataFrame
    trades_frame: pd.DataFrame
    variants: dict[str, dict[str, float]]

    def summary(self) -> dict[str, object]:
        return {
            "family": self.family,
            "horizon": self.horizon,
            "model": self.model,
            "performance": self.performance,
            "full_performance": self.full_performance,
            "trades": self.trades,
            "round_trip": self.round_trip,
            "drawdown": self.drawdown,
            "stats": self.stats,
            "monthly": self.monthly,
            "variants": self.variants,
        }


def evaluate_portfolio(
    result: StrategyResult,
    *,
    family: str,
    horizon: int,
    model: str,
    benchmark_returns: pd.Series,
    common_end: pd.Timestamp,
) -> PortfolioEvaluation:
    equity_full = result.equity["equity"].astype(float).dropna()
    equity_common = equity_full.loc[:common_end]
    if len(equity_common) < 3:
        raise ValueError(f"{model} {family} T+{horizon} 的共同窗口过短")
    performance = performance_summary(equity_common)
    full_performance = performance_summary(equity_full)
    trades = trade_summary(result.trades, INITIAL_CAPITAL)
    paired = round_trips(result.trades)
    trip_stats = round_trip_stats(paired)
    attribution = symbol_attribution(paired)
    drawdown = drawdown_stats(equity_common)

    daily = equity_common.pct_change().dropna()
    benchmark = benchmark_returns.reindex(daily.index).fillna(0.0)
    excess = daily - benchmark
    benchmark_total = float((1.0 + benchmark).prod() - 1.0)
    active_return = float(performance["total_return"] - benchmark_total)
    tracking_error = (
        float(excess.std(ddof=1) * np.sqrt(252.0)) if len(excess) > 1 else float("nan")
    )
    information_ratio = (
        float(excess.mean() / excess.std(ddof=1) * np.sqrt(252.0))
        if len(excess) > 1 and excess.std(ddof=1) > 0
        else float("nan")
    )
    downside = _downside_deviation(daily)
    sortino = (
        float(daily.mean() / downside * np.sqrt(252.0))
        if downside > 0
        else float("nan")
    )
    max_drawdown = float(performance.get("max_drawdown", float("nan")))
    calmar = (
        float(performance["annual_return"] / abs(max_drawdown))
        if np.isfinite(max_drawdown) and max_drawdown < 0
        else float("nan")
    )
    bootstrap = block_bootstrap_mean(excess, block=20, n_boot=5000, seed=20261019)
    monthly = monthly_equity(equity_common)

    invested = (
        result.equity.loc[:common_end, "market_value"]
        / (
            result.equity.loc[:common_end, "cash"]
            + result.equity.loc[:common_end, "market_value"]
        )
    ).replace([np.inf, -np.inf], np.nan)
    mid = len(daily) // 2
    first_half = float((1.0 + daily.iloc[:mid]).prod() - 1.0)
    second_half = float((1.0 + daily.iloc[mid:]).prod() - 1.0)

    buys = int((result.trades["side"] == "buy").sum()) if not result.trades.empty else 0
    sells = (
        int((result.trades["side"] == "sell").sum()) if not result.trades.empty else 0
    )
    stats = {
        "benchmark_total": benchmark_total,
        "active_return": active_return,
        "excess_mean_daily": float(excess.mean()),
        "excess_annual": float(excess.mean() * 252.0),
        "excess_total_compound": float((1.0 + excess).prod() - 1.0),
        "tracking_error": tracking_error,
        "information_ratio": information_ratio,
        "excess_ci_low": bootstrap["ci_low"],
        "excess_ci_high": bootstrap["ci_high"],
        "excess_p_positive": bootstrap["p_positive"],
        "sortino": sortino,
        "calmar": calmar,
        "monthly_win_rate": float((monthly > 0).mean())
        if len(monthly)
        else float("nan"),
        "n_months": int(len(monthly)),
        "invested_ratio": float(invested.mean()) if len(invested) else float("nan"),
        "first_half_return": first_half,
        "second_half_return": second_half,
        "open_positions": buys - sells,
        "n_buys": buys,
        "n_sells": sells,
    }
    return PortfolioEvaluation(
        family=family,
        horizon=horizon,
        model=model,
        performance=performance,
        full_performance=full_performance,
        trades=trades,
        round_trip=trip_stats,
        drawdown=drawdown,
        stats=stats,
        monthly={str(period): float(value) for period, value in monthly.items()},
        daily_returns=daily,
        excess=excess,
        equity_common=equity_common,
        equity_full=equity_full,
        attribution=attribution,
        trades_frame=result.trades,
        variants={},
    )


def run_family(
    panel: MarketPanel,
    pred_panel: pd.DataFrame,
    *,
    family: str,
    horizon: int,
    model: str,
    benchmark_returns: pd.Series,
    common_end: pd.Timestamp,
    stamp: float = 0.0005,
    slippage: float = 0.0005,
    pred_by_date: dict[pd.Timestamp, pd.Series] | None = None,
) -> PortfolioEvaluation:
    if pred_by_date is None:
        pred_by_date = build_pred_by_date(pred_panel)
    spec = build_spec(family, horizon)
    result = run_strategy(panel, pred_by_date, spec)
    evaluation = evaluate_portfolio(
        result,
        family=family,
        horizon=horizon,
        model=model,
        benchmark_returns=benchmark_returns,
        common_end=common_end,
    )
    baseline = {
        "total_return": evaluation.performance["total_return"],
        "annual_return": evaluation.performance["annual_return"],
        "sharpe": evaluation.performance["sharpe"],
        "max_drawdown": evaluation.performance["max_drawdown"],
    }
    evaluation.variants = cost_variants(
        panel,
        pred_by_date,
        spec,
        baseline,
        common_end=common_end,
        stamp=stamp,
        slippage=slippage,
    )
    return evaluation
