"""长窗口深度评估报告的数据层。

把 ``multi_model`` / ``quarterly`` / ``consensus`` / ``capacity_eval`` /
``attribution`` 的产物加载成报告可直接引用的表，并派生三类报告专用视图：

1. **分段质量**：训练产物覆盖期（stored）、外推干净期（clean）、外推降级期
   （degraded）分开统计，避免把特征覆盖不全的日期混进全窗口结论；
2. **模型画像**：每个模型在 5/10 日两档上的质量、组合、季度稳定性、共识
   重叠与外推稳健性，横向排名后生成优势/短板标签；
3. **审计表**：推理复现校验（extend_validation_*.json）与网格对齐统计。

本模块只读产物，不做任何回测计算；缺失的目录/文件按可选处理，报告侧用
``—`` 呈现，不静默填 0。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from .model_registry import HORIZONS, MODEL_ORDER

#: 外推分段。stored 的末端按模型训练产物日期固定在 2026-08-26（信号日）；
#: clean/degraded 的分界来自 300 维特征覆盖率盘点（09-15 起跌破 300 维）。
EXTENSION_WINDOWS: dict[int, dict[str, tuple[str, str]]] = {
    5: {
        "clean": ("2026-08-28", "2026-09-14"),
        "degraded": ("2026-09-15", "2026-10-08"),
    },
    10: {
        "clean": ("2026-08-27", "2026-09-14"),
        "degraded": ("2026-09-15", "2026-09-23"),
    },
}
STORED_END = "2026-08-26"
PERIODS = ("stored", "clean", "degraded")
PERIOD_LABELS = {
    "stored": "训练产物期",
    "clean": "外推·干净期",
    "degraded": "外推·降级期",
    "full": "全窗口",
}

#: 画像维度：列名 → (中文标签, 方向)。方向 +1 越大越好，-1 越小越好。
PROFILE_DIMENSIONS: dict[str, tuple[str, int]] = {
    "ic_mean": ("IC 均值", 1),
    "icir": ("ICIR", 1),
    "top20_excess": ("Top20 超额", 1),
    "sharpe_rolling": ("滚动 Top5 夏普", 1),
    "max_drawdown_rolling": ("滚动 Top5 最大回撤", 1),
    "quarter_ic_rank_std": ("季度 IC 排名波动", -1),
    "clean_ic": ("外推干净期 IC", 1),
}


@dataclass
class DeepBundle:
    """报告所需的全部产物；缺失项保持 None/空表。"""

    run_dir: Path
    summary: dict = field(default_factory=dict)
    quality: dict[int, pd.DataFrame] = field(default_factory=dict)
    quality_daily: dict[int, dict[str, pd.DataFrame]] = field(default_factory=dict)
    portfolios: dict[int, dict[str, pd.DataFrame]] = field(default_factory=dict)
    monthly: dict[int, dict[str, pd.DataFrame]] = field(default_factory=dict)
    deciles: dict[int, pd.DataFrame] = field(default_factory=dict)
    quarterly_stats: dict[int, pd.DataFrame] = field(default_factory=dict)
    quarterly_rank: dict[int, pd.DataFrame] = field(default_factory=dict)
    quarterly_champions: dict[int, pd.DataFrame] = field(default_factory=dict)
    consensus: dict[str, object] = field(default_factory=dict)
    capacity: dict[str, pd.DataFrame] = field(default_factory=dict)
    attribution: pd.DataFrame = field(default_factory=pd.DataFrame)
    validation: pd.DataFrame = field(default_factory=pd.DataFrame)
    alignment: dict = field(default_factory=dict)

    @property
    def models(self) -> tuple[str, ...]:
        return tuple(MODEL_ORDER)

    def quality_ranking(self, horizon: int) -> pd.DataFrame:
        frame = self.quality.get(horizon)
        if frame is None or frame.empty:
            return pd.DataFrame()
        return frame.sort_values("rank").reset_index(drop=True)


def _read_csv(path: Path, **kwargs) -> pd.DataFrame:
    if not path.is_file():
        return pd.DataFrame()
    return pd.read_csv(path, **kwargs)


def _read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _load_quality_daily(run_dir: Path) -> dict[int, dict[str, pd.DataFrame]]:
    result: dict[int, dict[str, pd.DataFrame]] = {}
    for horizon in HORIZONS:
        per_model: dict[str, pd.DataFrame] = {}
        for model in MODEL_ORDER:
            path = run_dir / f"quality_daily_{horizon}_{model}.csv"
            frame = _read_csv(path, parse_dates=["date"])
            if not frame.empty:
                per_model[model] = frame.set_index("date").sort_index()
        result[horizon] = per_model
    return result


def _load_consensus(consensus_dir: Path | None) -> dict[str, object]:
    if consensus_dir is None or not consensus_dir.is_dir():
        return {}
    bundle: dict[str, object] = {
        "manifest": _read_json(consensus_dir / "consensus_manifest.json")
    }
    for horizon in HORIZONS:
        bundle[f"distribution_{horizon}"] = _read_csv(
            consensus_dir / f"consensus_distribution_{horizon}.csv"
        )
        bundle[f"daily_{horizon}"] = _read_csv(
            consensus_dir / f"consensus_daily_{horizon}.csv", parse_dates=["date"]
        )
        bundle[f"overlap_{horizon}"] = _read_csv(
            consensus_dir / f"consensus_model_overlap_{horizon}.csv"
        )
        bundle[f"pairwise_{horizon}"] = _read_csv(
            consensus_dir / f"consensus_pairwise_{horizon}.csv"
        )
        bundle[f"strategy_{horizon}"] = _read_csv(
            consensus_dir / f"consensus_strategy_summary_{horizon}.csv"
        )
        bundle[f"score_stats_{horizon}"] = _read_csv(
            consensus_dir / f"score_stats_summary_{horizon}.csv"
        )
        bundle[f"score_daily_{horizon}"] = _read_csv(
            consensus_dir / f"score_stats_daily_{horizon}.csv",
            parse_dates=["date"],
        )
        bundle[f"screening_daily_{horizon}"] = _read_csv(
            consensus_dir / f"screening_daily_{horizon}.csv",
            parse_dates=["date"],
        )
        bundle[f"screening_summary_{horizon}"] = _read_csv(
            consensus_dir / f"screening_summary_{horizon}.csv"
        )
        bundle[f"ensemble_{horizon}"] = _read_csv(
            consensus_dir / f"consensus_ensemble_summary_{horizon}.csv"
        )
        bundle[f"dynamic_{horizon}"] = _read_csv(
            consensus_dir / f"dynamic_selection_summary_{horizon}.csv"
        )
        thresholds: dict[int, pd.DataFrame] = {}
        for threshold in (2, 4, 6, 7, 8, 10):
            frame = _read_csv(
                consensus_dir / f"consensus_threshold_{threshold}_{horizon}.csv",
                parse_dates=["date"],
            )
            if not frame.empty:
                thresholds[threshold] = frame
        bundle[f"thresholds_{horizon}"] = thresholds
    return bundle


def _load_capacity(capacity_dir: Path | None) -> dict[str, pd.DataFrame]:
    if capacity_dir is None or not capacity_dir.is_dir():
        return {}
    return {
        "runs": _read_csv(capacity_dir / "capacity_runs.csv"),
        "summary": _read_csv(capacity_dir / "capacity_summary.csv"),
        "top10_vs_top20": _read_csv(capacity_dir / "capacity_top10_vs_top20.csv"),
        "advantage": _read_csv(capacity_dir / "capacity_top10_advantage.csv"),
    }


def _load_validation(extension_dir: Path | None) -> pd.DataFrame:
    if extension_dir is None or not extension_dir.is_dir():
        return pd.DataFrame()
    rows = []
    for horizon in HORIZONS:
        for model in MODEL_ORDER:
            path = extension_dir / f"extend_validation_{horizon}_{model}.json"
            payload = _read_json(path)
            if not payload:
                continue
            rows.append(
                {
                    "horizon": horizon,
                    "model": model,
                    "overlap_rows": payload.get("overlap_rows"),
                    "corr": payload.get("corr"),
                    "rank_corr": payload.get("rank_corr"),
                    "max_abs_diff": payload.get("max_abs_diff"),
                    "top100_overlap": payload.get("top100_overlap"),
                }
            )
    return pd.DataFrame(rows)


def load_bundle(
    run_dir: Path,
    *,
    quarterly_dir: Path | None = None,
    consensus_dir: Path | None = None,
    capacity_dir: Path | None = None,
    attribution_dir: Path | None = None,
    extension_dir: Path | None = None,
) -> DeepBundle:
    bundle = DeepBundle(run_dir=run_dir)
    bundle.summary = _read_json(run_dir / "summary.json")
    for horizon in HORIZONS:
        bundle.quality[horizon] = _read_csv(run_dir / f"quality_league_{horizon}.csv")
        for family in ("top10", "top20", "rolling"):
            frame = _read_csv(run_dir / f"portfolio_league_{family}_{horizon}.csv")
            if not frame.empty:
                bundle.portfolios.setdefault(horizon, {})[family] = frame
            monthly = _read_csv(
                run_dir / f"monthly_returns_{family}_{horizon}.csv", index_col=0
            )
            if not monthly.empty:
                bundle.monthly.setdefault(horizon, {})[family] = monthly
        bundle.deciles[horizon] = _read_csv(run_dir / f"deciles_{horizon}.csv")
    bundle.quality_daily = _load_quality_daily(run_dir)

    if quarterly_dir is not None:
        for horizon in HORIZONS:
            bundle.quarterly_stats[horizon] = _read_csv(
                quarterly_dir / f"quarterly_stats_{horizon}.csv"
            )
            bundle.quarterly_rank[horizon] = _read_csv(
                quarterly_dir / f"quarterly_rank_{horizon}.csv"
            )
            bundle.quarterly_champions[horizon] = _read_csv(
                quarterly_dir / f"quarterly_champions_{horizon}.csv"
            )
    bundle.consensus = _load_consensus(consensus_dir)
    bundle.capacity = _load_capacity(capacity_dir)
    if attribution_dir is not None:
        bundle.attribution = _read_csv(
            attribution_dir / "attribution_decomp_summary.csv"
        )
    bundle.validation = _load_validation(extension_dir)
    if extension_dir is not None:
        bundle.alignment = _read_json(extension_dir / "align_extensions_report.json")
    return bundle


def period_metrics(bundle: DeepBundle, horizon: int) -> pd.DataFrame:
    """逐模型 × 分段的 IC / Top20 超额。"""
    rows: list[dict[str, object]] = []
    per_model = bundle.quality_daily.get(horizon, {})
    for model in MODEL_ORDER:
        frame = per_model.get(model)
        if frame is None or frame.empty:
            continue
        windows = {
            "stored": ("1900-01-01", STORED_END),
            "full": ("1900-01-01", "2100-01-01"),
        }
        windows.update(EXTENSION_WINDOWS[horizon])
        for period, (start, end) in windows.items():
            subset = frame.loc[str(start) : str(end)]
            if subset.empty:
                continue
            ic = subset["ic_eligible"].astype(float)
            excess = subset["top20_excess"].astype(float)
            rows.append(
                {
                    "horizon": horizon,
                    "model": model,
                    "period": period,
                    "days": int(len(subset)),
                    "ic_mean": float(ic.mean()),
                    "ic_std": float(ic.std(ddof=1)),
                    "icir": float(ic.mean() / ic.std(ddof=1))
                    if ic.std(ddof=1) > 0
                    else np.nan,
                    "ic_positive": float((ic > 0).mean()),
                    "top20_ret": float(subset["top20_ret"].astype(float).mean()),
                    "top20_excess": float(excess.mean()),
                    "top20_hit": float(subset["top20_hit"].astype(float).mean()),
                    "ls20": float(subset["ls20"].astype(float).mean()),
                }
            )
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    frame["period_label"] = frame["period"].map(PERIOD_LABELS)
    return frame


def _quarter_stats(bundle: DeepBundle, horizon: int, model: str) -> dict[str, float]:
    frame = bundle.quarterly_rank.get(horizon)
    if frame is None or frame.empty:
        return {}
    subset = frame[frame["model"] == model]
    if subset.empty:
        return {}
    return {
        "quarter_ic_rank_mean": float(subset["ic_rank"].mean()),
        "quarter_ic_rank_std": float(subset["ic_rank"].std(ddof=1)),
        "quarter_ic_min": float(subset["ic_mean"].min()),
        "quarter_ic_max": float(subset["ic_mean"].max()),
        "quarter_excess_min": float(subset["excess_mean"].min()),
        "quarter_excess_max": float(subset["excess_mean"].max()),
        "quarters": int(len(subset)),
    }


def model_profiles(bundle: DeepBundle, horizon: int) -> pd.DataFrame:
    """单模型画像：质量 + 组合 + 季度稳定性 + 共识重叠 + 外推稳健性。"""
    quality = bundle.quality_ranking(horizon)
    if quality.empty:
        return pd.DataFrame()
    rolling = bundle.portfolios.get(horizon, {}).get("rolling", pd.DataFrame())
    top20 = bundle.portfolios.get(horizon, {}).get("top20", pd.DataFrame())
    overlap = bundle.consensus.get(f"overlap_{horizon}", pd.DataFrame())
    periods = period_metrics(bundle, horizon)
    stored = (
        periods[periods["period"] == "stored"].set_index("model")
        if not periods.empty
        else pd.DataFrame()
    )
    clean = (
        periods[periods["period"] == "clean"].set_index("model")
        if not periods.empty
        else pd.DataFrame()
    )
    degraded = (
        periods[periods["period"] == "degraded"].set_index("model")
        if not periods.empty
        else pd.DataFrame()
    )
    full = (
        periods[periods["period"] == "full"].set_index("model")
        if not periods.empty
        else pd.DataFrame()
    )

    rows = []
    for record in quality.to_dict("records"):
        model = str(record["model"])
        row: dict[str, object] = {
            "model": model,
            "ic_mean": record.get("ic_mean"),
            "icir": record.get("icir"),
            "ic_positive_ratio": record.get("ic_positive_ratio"),
            "nw_t": record.get("nw_t"),
            "top20_hit": record.get("top20_hit"),
            "top20_ret": record.get("top20_ret"),
            "top_bottom_spread": record.get("top_bottom_spread"),
            "decile_monotonicity": record.get("decile_monotonicity"),
        }
        if not rolling.empty:
            match = rolling[rolling["model"] == model]
            if not match.empty:
                stats = match.iloc[0]
                row.update(
                    {
                        "sharpe_rolling": stats.get("sharpe"),
                        "annual_return_rolling": stats.get("annual_return"),
                        "max_drawdown_rolling": stats.get("max_drawdown"),
                        "calmar_rolling": stats.get("calmar"),
                        "first_half_rolling": stats.get("first_half_return"),
                        "second_half_rolling": stats.get("second_half_return"),
                        "round_trips": stats.get("n_round_trips"),
                        "invested_ratio": stats.get("invested_ratio"),
                    }
                )
        if not top20.empty:
            match = top20[top20["model"] == model]
            if not match.empty:
                stats = match.iloc[0]
                row.update(
                    {
                        "sharpe_top20": stats.get("sharpe"),
                        "max_drawdown_top20": stats.get("max_drawdown"),
                        "annual_return_top20": stats.get("annual_return"),
                    }
                )
        row.update(_quarter_stats(bundle, horizon, model))
        if not overlap.empty:
            match = overlap[overlap["model"] == model]
            if not match.empty:
                stats = match.iloc[0]
                row.update(
                    {
                        "unique_share": stats.get("unique_share"),
                        "unique_ret": stats.get("unique_ret"),
                        "shared_ret": stats.get("shared_ret"),
                        "in_half_ret": stats.get("in_half_ret"),
                        "out_half_ret": stats.get("out_half_ret"),
                        "in_out_diff": stats.get("in_out_diff"),
                    }
                )
        if not full.empty and model in full.index:
            row["top20_excess"] = float(full.loc[model, "top20_excess"])
        if not stored.empty and model in stored.index:
            row["stored_ic"] = float(stored.loc[model, "ic_mean"])
            row["stored_top20_excess"] = float(stored.loc[model, "top20_excess"])
        if not clean.empty and model in clean.index:
            row["clean_ic"] = float(clean.loc[model, "ic_mean"])
            row["clean_top20_excess"] = float(clean.loc[model, "top20_excess"])
        if not degraded.empty and model in degraded.index:
            row["degraded_ic"] = float(degraded.loc[model, "ic_mean"])
            row["degraded_top20_excess"] = float(degraded.loc[model, "top20_excess"])
        rows.append(row)
    frame = pd.DataFrame(rows)
    return _add_profile_ranks(frame)


def _add_profile_ranks(frame: pd.DataFrame) -> pd.DataFrame:
    ranked = frame.copy()
    for column, (_, direction) in PROFILE_DIMENSIONS.items():
        if column not in ranked.columns:
            continue
        values = pd.to_numeric(ranked[column], errors="coerce")
        ranked[f"rank_{column}"] = values.rank(
            ascending=direction < 0, method="average"
        )
    return ranked


def strength_weakness_labels(
    row: pd.Series, top_n: int = 2
) -> tuple[list[str], list[str]]:
    """按维度排名给出优势/短板标签；排名数字越小越强。"""
    entries: list[tuple[int, str]] = []
    for column, (label, _) in PROFILE_DIMENSIONS.items():
        rank_column = f"rank_{column}"
        if rank_column not in row or pd.isna(row[rank_column]):
            continue
        entries.append((int(row[rank_column]), label))
    if not entries:
        return [], []
    entries.sort(key=lambda item: item[0])
    strengths = [f"{label}（第{rank}）" for rank, label in entries[:top_n] if rank <= 4]
    weaknesses = [
        f"{label}（第{rank}）" for rank, label in entries[::-1][:top_n] if rank >= 10
    ]
    return strengths, weaknesses


def quarter_ic_matrix(bundle: DeepBundle, horizon: int) -> pd.DataFrame:
    """季度 × 模型的 IC 均值矩阵。"""
    frame = bundle.quarterly_rank.get(horizon)
    if frame is None or frame.empty:
        return pd.DataFrame()
    matrix = frame.pivot_table(
        index="quarter", columns="model", values="ic_mean", aggfunc="mean"
    )
    ordered = [model for model in MODEL_ORDER if model in matrix.columns]
    return matrix[ordered].sort_index()


def quarter_excess_matrix(bundle: DeepBundle, horizon: int) -> pd.DataFrame:
    frame = bundle.quarterly_rank.get(horizon)
    if frame is None or frame.empty:
        return pd.DataFrame()
    matrix = frame.pivot_table(
        index="quarter", columns="model", values="excess_mean", aggfunc="mean"
    )
    ordered = [model for model in MODEL_ORDER if model in matrix.columns]
    return matrix[ordered].sort_index()


def _spearman(left: pd.Series, right: pd.Series) -> float:
    """秩相关不依赖 scipy：先排名，再算 Pearson。"""
    pairs = pd.concat([left, right], axis=1, join="inner").dropna()
    if len(pairs) < 3:
        return float("nan")
    left_rank = pairs.iloc[:, 0].rank().to_numpy(dtype=float)
    right_rank = pairs.iloc[:, 1].rank().to_numpy(dtype=float)
    if left_rank.std() == 0 or right_rank.std() == 0:
        return float("nan")
    return float(np.corrcoef(left_rank, right_rank)[0, 1])


def quarter_rank_correlation(matrix: pd.DataFrame) -> pd.DataFrame:
    """相邻季度模型 IC 排名的秩相关。"""
    if matrix.empty or len(matrix) < 2:
        return pd.DataFrame()
    ranks = matrix.rank(axis=1, ascending=False, method="average")
    rows = []
    quarters = list(ranks.index)
    for previous, current in zip(quarters, quarters[1:], strict=False):
        corr = _spearman(ranks.loc[previous], ranks.loc[current])
        rows.append({"previous": previous, "current": current, "rank_corr": corr})
    return pd.DataFrame(rows)


def screening_table(bundle: DeepBundle, horizon: int) -> pd.DataFrame:
    """单模型 Top10/20/50 筛选的逐日明细。"""
    daily = bundle.consensus.get(f"screening_daily_{horizon}")
    return daily if isinstance(daily, pd.DataFrame) else pd.DataFrame()


def score_screen_summary(bundle: DeepBundle, horizon: int) -> pd.DataFrame:
    """模型 × 筛选档位的均值、NW t 与命中率（优先用共识脚本的汇总表）。"""
    summary = bundle.consensus.get(f"screening_summary_{horizon}")
    if isinstance(summary, pd.DataFrame) and not summary.empty:
        return summary.copy()
    daily_key = f"screening_daily_{horizon}"
    if daily_key not in bundle.consensus:
        return pd.DataFrame()
    frame = bundle.consensus[daily_key]
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return pd.DataFrame()
    grouped = frame.groupby(["model", "filter"], sort=True)
    summary = grouped.agg(
        days=("date", "nunique"),
        mean_ret=("ret", "mean"),
        mean_excess=("excess", "mean"),
        hit_rate=("ret", lambda values: float((values > 0).mean())),
        avg_names=("n", "mean"),
    ).reset_index()
    return summary


def capacity_advantage_table(bundle: DeepBundle, horizon: int) -> pd.DataFrame:
    frame = bundle.capacity.get("advantage", pd.DataFrame())
    if frame.empty:
        return frame
    return frame[frame["horizon"] == horizon].copy()


def capacity_summary_table(bundle: DeepBundle, horizon: int) -> pd.DataFrame:
    frame = bundle.capacity.get("summary", pd.DataFrame())
    if frame.empty:
        return frame
    return frame[frame["horizon"] == horizon].copy()


def validation_table(bundle: DeepBundle, horizon: int) -> pd.DataFrame:
    if bundle.validation.empty:
        return pd.DataFrame()
    return bundle.validation[bundle.validation["horizon"] == horizon].copy()


def rank_correlation_between(left: pd.Series, right: pd.Series) -> float:
    """两个模型索引的指标序列的秩相关；缺失值成对剔除。"""
    joined = pd.concat([left, right], axis=1, join="inner").dropna()
    if len(joined) < 3:
        return float("nan")
    return _spearman(joined.iloc[:, 0], joined.iloc[:, 1])


def year_slice_ranking(
    bundle: DeepBundle, horizon: int, start: str, end: str
) -> pd.Series:
    """按日质量表重算某时间片的模型 IC 均值，返回按值降序的 Series。"""
    per_model = bundle.quality_daily.get(horizon, {})
    rows = {}
    for model, frame in per_model.items():
        if frame.empty:
            continue
        subset = frame.loc[str(start) : str(end), "ic_eligible"].astype(float)
        if not subset.empty:
            rows[model] = float(subset.mean())
    series = pd.Series(rows, dtype=float)
    return series.sort_values(ascending=False)
