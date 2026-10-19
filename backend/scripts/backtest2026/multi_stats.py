"""多模型差异的严格统计检验。

回答的核心问题不是「哪个模型第一名」，而是：
1. 13 个模型在同一批日期上的差异是否超过随机波动（Friedman / Kendall W）；
2. 第一名与其余模型的配对差异是否经得起多重检验（Wilcoxon + BH）；
3. 在 26 个模型-周期组合里挑第一名，是否存在数据窥探（DSR / White RC / PBO）；
4. 上半段与下半段、T+5 与 T+10 的排名是否稳定。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .multi_portfolio import PortfolioEvaluation
from .strict_stats import (
    bh_qvalues,
    deflated_sharpe_ratio,
    friedman_kendall,
    newey_west_mean,
    pairwise_wilcoxon_bh,
    pbo_cscv,
    sharpe_ratio,
    spearman,
    white_reality_check,
)

QUALITY_METRICS = (
    "ic_eligible",
    "top5_ret",
    "top10_ret",
    "top20_ret",
    "top5_hit",
    "top10_hit",
    "top20_hit",
    "top10_dir",
)


@dataclass(frozen=True)
class StatsSettings:
    block: int = 20
    n_boot: int = 5000
    seed: int = 20261019
    n_trials: int = 26


def _matrix(
    series_by_model: Mapping[str, pd.Series],
    models: Iterable[str],
) -> pd.DataFrame:
    columns = {
        model: series_by_model[model].astype(float)
        for model in models
        if model in series_by_model
    }
    frame = pd.DataFrame(columns)
    return frame.dropna(how="any")


def _p_two_sided(t_stat: float) -> float:
    from scipy.stats import norm

    if not np.isfinite(t_stat):
        return float("nan")
    return float(2.0 * norm.sf(abs(t_stat)))


def quality_stats(
    daily_by_model: Mapping[str, pd.DataFrame],
    summaries: Mapping[str, Mapping[str, object]],
    models: Iterable[str],
    *,
    settings: StatsSettings,
) -> dict[str, object]:
    available = [model for model in models if model in daily_by_model]
    if not available:
        return {"models": [], "friedman": {}, "ranking": []}

    matrices = {
        metric: _matrix(
            {model: daily_by_model[model][metric] for model in available},
            available,
        )
        for metric in QUALITY_METRICS
    }
    friedman = {
        metric: friedman_kendall(frame.to_numpy())
        for metric, frame in matrices.items()
        if not frame.empty
    }

    ordered = sorted(
        available,
        key=lambda model: float(summaries[model]["ic_eligible_mean"]),
        reverse=True,
    )
    ic_matrix = matrices["ic_eligible"]
    best = ordered[0]
    pairwise = pairwise_wilcoxon_bh(
        ic_matrix[best],
        {model: ic_matrix[model] for model in available if model != best},
    )
    pvalues = [float(summaries[model]["ic_p_positive"]) for model in ordered]
    qvalues = bh_qvalues(pvalues)
    ranking = []
    for rank, (model, qvalue) in enumerate(zip(ordered, qvalues, strict=True), 1):
        summary = summaries[model]
        nw_t = float(summary["ic_nw_t"])
        ranking.append(
            {
                "rank": rank,
                "model": model,
                "ic_mean": float(summary["ic_eligible_mean"]),
                "ic_std": float(summary["ic_eligible_std"]),
                "icir": float(summary["icir"]),
                "ic_positive_ratio": float(summary["ic_positive_ratio"]),
                "nw_t": nw_t,
                "nw_p": _p_two_sided(nw_t),
                "ic_ci_low": float(summary["ic_ci_low"]),
                "ic_ci_high": float(summary["ic_ci_high"]),
                "q_bootstrap": float(qvalue),
                "top5_hit": float(summary["top5_hit_mean"]),
                "top10_hit": float(summary["top10_hit_mean"]),
                "top20_hit": float(summary["top20_hit_mean"]),
                "top10_dir": float(summary["top10_dir_mean"]),
                "top10_ret": float(summary["top10_ret_mean"]),
                "top20_ret": float(summary["top20_ret_mean"]),
                "top_bottom_spread": float(summary["top_bottom_spread"]),
                "decile_monotonicity": float(summary["decile_monotonicity"]),
            }
        )

    monotonicity = {
        model: float(summaries[model]["decile_monotonicity"]) for model in available
    }
    return {
        "models": available,
        "days": int(len(ic_matrix)),
        "friedman": friedman,
        "ranking": ranking,
        "best_ic_model": best,
        "pairwise_ic": pairwise,
        "deciles": {
            model: list(summaries[model]["decile_returns"]) for model in available
        },
        "decile_monotonicity": monotonicity,
    }


def _ranking_row(evaluation: PortfolioEvaluation) -> dict[str, object]:
    performance = evaluation.performance
    stats = evaluation.stats
    return {
        "model": evaluation.model,
        "total_return": float(performance["total_return"]),
        "annual_return": float(performance["annual_return"]),
        "annual_vol": float(performance["annual_vol"]),
        "sharpe": float(performance["sharpe"]),
        "max_drawdown": float(performance["max_drawdown"]),
        "daily_win_rate": float(performance["daily_win_rate"]),
        "benchmark_total": float(stats["benchmark_total"]),
        "active_return": float(stats["active_return"]),
        "information_ratio": float(stats["information_ratio"]),
        "tracking_error": float(stats["tracking_error"]),
        "excess_ci_low": float(stats["excess_ci_low"]),
        "excess_ci_high": float(stats["excess_ci_high"]),
        "excess_p_positive": float(stats["excess_p_positive"]),
        "sortino": float(stats["sortino"]),
        "calmar": float(stats["calmar"]),
        "monthly_win_rate": float(stats["monthly_win_rate"]),
        "invested_ratio": float(stats["invested_ratio"]),
        "first_half_return": float(stats["first_half_return"]),
        "second_half_return": float(stats["second_half_return"]),
        "trade_win_rate": float(evaluation.round_trip["net_win"]),
        "trade_net_avg": float(evaluation.round_trip["net_avg"]),
        "payoff": float(evaluation.round_trip["payoff"]),
        "profit_factor": float(evaluation.round_trip["profit_factor"]),
        "n_round_trips": float(evaluation.round_trip["n"]),
        "open_positions": int(stats["open_positions"]),
    }


def portfolio_stats(
    evaluations: Mapping[str, PortfolioEvaluation],
    models: Iterable[str],
    *,
    settings: StatsSettings,
) -> dict[str, object]:
    available = [model for model in models if model in evaluations]
    if not available:
        return {"models": [], "friedman": {}, "ranking": []}
    returns = _matrix(
        {model: evaluations[model].daily_returns for model in available},
        available,
    )
    excess = _matrix(
        {model: evaluations[model].excess for model in available},
        available,
    )
    common = [model for model in available if model in returns.columns]
    returns = returns[common]
    excess = excess[common]

    ranked = sorted(
        common,
        key=lambda model: float(evaluations[model].performance["total_return"]),
        reverse=True,
    )
    best = ranked[0]
    pairwise = pairwise_wilcoxon_bh(
        returns[best],
        {model: returns[model] for model in common if model != best},
    )

    daily_sharpe = {model: sharpe_ratio(returns[model], periods=1) for model in common}
    finite_sharpe = [value for value in daily_sharpe.values() if np.isfinite(value)]
    trial_sr_std = (
        float(np.std(finite_sharpe, ddof=1)) if len(finite_sharpe) > 1 else 0.0
    )
    dsr = {
        model: deflated_sharpe_ratio(
            returns[model],
            n_trials=settings.n_trials,
            trial_sr_std=trial_sr_std,
        )
        for model in common
    }

    pvalues = [float(evaluations[model].stats["excess_p_positive"]) for model in ranked]
    qvalues = bh_qvalues(pvalues)
    ranking = []
    for rank, (model, qvalue) in enumerate(zip(ranked, qvalues, strict=True), 1):
        row = _ranking_row(evaluations[model])
        row["rank"] = rank
        row["q_excess"] = float(qvalue)
        row["dsr"] = float(dsr[model]["dsr"])
        row["sr0_annual"] = float(dsr[model]["sr0"])
        ranking.append(row)

    white = white_reality_check(
        excess.to_numpy(),
        block=settings.block,
        n_boot=settings.n_boot,
        seed=settings.seed,
    )
    best_index = int(white["best_index"]) if np.isfinite(white["best_index"]) else -1
    white_model = common[best_index] if 0 <= best_index < len(common) else ""
    pbo = pbo_cscv(returns.to_numpy(), n_blocks=10)

    first_half = [
        float(evaluations[model].stats["first_half_return"]) for model in common
    ]
    second_half = [
        float(evaluations[model].stats["second_half_return"]) for model in common
    ]
    total_returns = [
        float(evaluations[model].performance["total_return"]) for model in common
    ]
    sharpes = [float(evaluations[model].performance["sharpe"]) for model in common]
    return {
        "models": common,
        "days": int(len(returns)),
        "friedman_returns": friedman_kendall(returns.to_numpy()),
        "friedman_excess": friedman_kendall(excess.to_numpy()),
        "white_reality_check": {
            **white,
            "best_model": white_model,
        },
        "pbo": pbo,
        "dsr_trial_sr_std_daily": trial_sr_std,
        "best_total_return_model": best,
        "pairwise_returns": pairwise,
        "ranking": ranking,
        "stability": {
            "first_second_half_rank_corr": spearman(first_half, second_half),
            "first_half_positive_ratio": float(np.mean(np.array(first_half) > 0)),
            "second_half_positive_ratio": float(np.mean(np.array(second_half) > 0)),
            "both_halves_positive_ratio": float(
                np.mean((np.array(first_half) > 0) & (np.array(second_half) > 0))
            ),
        },
        "spread": {
            "best_worst_total_return": float(max(total_returns) - min(total_returns)),
            "best_worst_sharpe": float(max(sharpes) - min(sharpes)),
            "median_total_return": float(np.median(total_returns)),
        },
    }


def cross_horizon_stats(
    quality_summaries: Mapping[int, Mapping[str, Mapping[str, object]]],
    evaluations: Mapping[int, Mapping[str, Mapping[str, PortfolioEvaluation]]],
    models: Iterable[str],
) -> dict[str, object]:
    """T+5 与 T+10 的排名是否一致，以及各模型两周期差值。"""
    quality_metrics = {
        "ic_mean": ("ic_eligible_mean",),
        "top10_hit": ("top10_hit_mean",),
        "top10_ret": ("top10_ret_mean",),
        "top20_ret": ("top20_ret_mean",),
    }
    out: dict[str, object] = {"quality_rank_corr": {}, "portfolio_rank_corr": {}}
    common_models = [
        model
        for model in models
        if model in quality_summaries.get(5, {})
        and model in quality_summaries.get(10, {})
    ]
    for label, (metric,) in quality_metrics.items():
        left = [float(quality_summaries[5][model][metric]) for model in common_models]
        right = [float(quality_summaries[10][model][metric]) for model in common_models]
        out["quality_rank_corr"][label] = spearman(left, right)

    for family in ("top10", "top20", "rolling"):
        family_models = [
            model
            for model in common_models
            if model in evaluations.get(5, {}).get(family, {})
            and model in evaluations.get(10, {}).get(family, {})
        ]
        left = [
            float(evaluations[5][family][model].performance["total_return"])
            for model in family_models
        ]
        right = [
            float(evaluations[10][family][model].performance["total_return"])
            for model in family_models
        ]
        sharpes_left = [
            float(evaluations[5][family][model].performance["sharpe"])
            for model in family_models
        ]
        sharpes_right = [
            float(evaluations[10][family][model].performance["sharpe"])
            for model in family_models
        ]
        out["portfolio_rank_corr"][family] = {
            "total_return": spearman(left, right),
            "sharpe": spearman(sharpes_left, sharpes_right),
            "n_models": len(family_models),
        }

    out["paired_differences"] = _paired_differences(
        quality_summaries, evaluations, common_models
    )
    return out


def _paired_differences(
    quality_summaries: Mapping[int, Mapping[str, Mapping[str, object]]],
    evaluations: Mapping[int, Mapping[str, Mapping[str, PortfolioEvaluation]]],
    models: Iterable[str],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for model in models:
        row: dict[str, object] = {"model": model}
        if model in quality_summaries.get(5, {}) and model in quality_summaries.get(
            10, {}
        ):
            row["delta_ic"] = float(
                quality_summaries[10][model]["ic_eligible_mean"]
                - quality_summaries[5][model]["ic_eligible_mean"]
            )
            row["delta_top10_hit"] = float(
                quality_summaries[10][model]["top10_hit_mean"]
                - quality_summaries[5][model]["top10_hit_mean"]
            )
        for family in ("top10", "top20", "rolling"):
            try:
                t5 = evaluations[5][family][model].performance["total_return"]
                t10 = evaluations[10][family][model].performance["total_return"]
            except KeyError:
                continue
            row[f"delta_{family}_return"] = float(t10 - t5)
        rows.append(row)
    return rows


def best_first_rows(stats: Mapping[str, object], label: str) -> pd.DataFrame:
    """把某个组合族的统计块展开成「模型 × 指标」表，便于写 CSV。"""
    ranking = stats.get("ranking", [])
    frame = pd.DataFrame(ranking)
    if not frame.empty:
        frame.insert(0, "family", label)
    return frame


def newey_west_ic(
    daily_by_model: Mapping[str, pd.DataFrame],
    models: Iterable[str],
    *,
    horizon: int,
) -> dict[str, dict[str, float]]:
    out = {}
    for model in models:
        if model not in daily_by_model:
            continue
        out[model] = newey_west_mean(
            daily_by_model[model]["ic_eligible"], lag=max(1, horizon - 1)
        )
    return out
