"""跨模型共识、Top20 重叠与逐日分数筛选。

回答三类问题：
1. 每个模型每日 Top20 里有多少只是其它模型也选中的「共识股」，重叠部分与
   独有部分各自的未来收益如何；
2. 把 13 个模型当投票器，按共识票数分层，收益是否随票数单调；
3. 分数本身能否再筛：Top20 阈值、阈值与第 21 名的差距、均值+1σ/+2σ、
   分数前 1%、以及 13 模型 z 分数等权融合后的 TopK。

所有收益口径与主回测一致：信号日 t 收盘出分，t+1 收盘买入，
t+1+H 收盘卖出；重叠日样本用 Newey-West（lag=H-1）修正。
"""

from __future__ import annotations

import argparse
import logging
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from .config import OUT_DIR
from .data import (
    attach_listing_age,
    build_market_panel,
    forward_return_matrix,
    load_extended_predictions,
    load_stored_predictions,
    read_first_seen,
)
from .deep_common import TEST_END, TEST_START, load_equity
from .model_registry import HORIZONS, MODEL_ORDER, resolve_model_run
from .runner import _write_json
from .strict_stats import newey_west_mean, sharpe_ratio

LOG = logging.getLogger("backtest2026.consensus")

FILTERS = ("top10", "top20", "top50", "z1", "z2", "top1pct")


@dataclass(frozen=True)
class ConsensusSettings:
    run_dir: Path
    out_dir: Path
    start: str = TEST_START
    end: str = TEST_END
    price_start: str = "2024-11-01"
    price_end: str = "2026-10-16"
    models: tuple[str, ...] = MODEL_ORDER
    horizons: tuple[int, ...] = HORIZONS
    top_k: int = 20
    block: int = 20
    extension_dir: Path | None = None


def _eligible_base(panel, day: pd.Timestamp, symbols: pd.Index) -> np.ndarray:
    """与 signal_eligible_mask 相同，只是先按面板列对齐。"""
    valid = panel.close_qfq.loc[day].reindex(symbols).notna().to_numpy()
    valid &= ~panel.suspended.loc[day].reindex(symbols).fillna(True).to_numpy(
        dtype=bool
    )
    valid &= ~panel.limit_up.loc[day].reindex(symbols).fillna(True).to_numpy(dtype=bool)
    valid &= ~panel.limit_down.loc[day].reindex(symbols).fillna(True).to_numpy(
        dtype=bool
    )
    valid &= panel.age_days.loc[day].reindex(symbols).fillna(-1).to_numpy() >= 60
    valid &= ~panel.is_st.reindex(symbols).fillna(False).to_numpy(dtype=bool)
    return valid


def _basket(forward_row: np.ndarray, indices: np.ndarray) -> tuple[float, int]:
    if len(indices) == 0:
        return float("nan"), 0
    values = forward_row[indices]
    values = values[np.isfinite(values)]
    if len(values) == 0:
        return float("nan"), 0
    return float(values.mean()), int(len(values))


def _summary_row(
    daily: pd.DataFrame,
    *,
    horizon: int,
    name: str,
    threshold: str,
) -> dict[str, object]:
    excess = daily["excess"].dropna().astype(float)
    ret = daily["ret"].dropna().astype(float)
    nw = newey_west_mean(excess, lag=max(1, horizon - 1))
    return {
        "name": name,
        "threshold": threshold,
        "days": int(len(daily)),
        "avg_names": float(daily["n"].mean()) if len(daily) else float("nan"),
        "mean_ret": float(ret.mean()) if len(ret) else float("nan"),
        "mean_excess": float(excess.mean()) if len(excess) else float("nan"),
        "nw_t": nw["t"],
        "hit_rate": float((excess > 0).mean()) if len(excess) else float("nan"),
        "cum_excess": float(excess.sum()) if len(excess) else float("nan"),
        "sharpe": sharpe_ratio(excess),
    }


def _load_prediction(
    run_dir: Path,
    start: str,
    end: str,
    *,
    horizon: int | None = None,
    model: str | None = None,
    extension_dir: Path | None = None,
) -> pd.DataFrame:
    if extension_dir is None:
        frame = load_stored_predictions(run_dir, start, end)
    else:
        if horizon is None or model is None:
            raise ValueError("使用外推预测时必须提供 horizon 与 model")
        frame = load_extended_predictions(
            run_dir, horizon, model, start, end, extension_dir
        )
    frame = frame[["symbol", "trade_date", "pred"]]
    return frame[np.isfinite(frame["pred"].to_numpy(dtype=float))]


def _vote(
    panel,
    days: list[pd.Timestamp],
    symbols: pd.Index,
    base: np.ndarray,
    forward: pd.DataFrame,
    pred: pd.DataFrame,
    model: str,
    top_k: int,
    *,
    min_names: int = 30,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[dict], list[dict], dict]:
    """单模型逐日投票、筛选和分数统计。"""
    day_pos = {day: i for i, day in enumerate(days)}
    sym_pos = {symbol: i for i, symbol in enumerate(symbols)}
    n_days, n_symbols = len(days), len(symbols)
    votes = np.zeros((n_days, n_symbols), dtype=np.int16)
    z_sum = np.zeros((n_days, n_symbols), dtype=np.float32)
    z_count = np.zeros((n_days, n_symbols), dtype=np.int16)
    score_rows: list[dict] = []
    screen_rows: list[dict] = []
    top_sets: dict[pd.Timestamp, set[str]] = {}

    for day, group in pred.groupby("trade_date", sort=True):
        day = pd.Timestamp(day)
        if day not in day_pos:
            continue
        i = day_pos[day]
        scores = np.full(n_symbols, np.nan, dtype=np.float64)
        group = group.drop_duplicates(subset=["symbol"], keep="last")
        idx = [sym_pos[s] for s in group["symbol"] if s in sym_pos]
        if not idx:
            continue
        idx_array = np.asarray(idx, dtype=np.int64)
        aligned = (
            group.set_index("symbol")["pred"]
            .reindex(symbols[idx_array])
            .to_numpy(dtype=np.float64)
        )
        scores[idx_array] = aligned
        mask = base[i] & np.isfinite(scores)
        n = int(mask.sum())
        if n < min_names:
            continue
        values = scores[mask]
        mean = float(values.mean())
        std = float(values.std(ddof=1)) if n > 1 else float("nan")
        order = np.argsort(-values, kind="stable")
        top_n = min(top_k, n)
        top_idx = np.flatnonzero(mask)[order[:top_n]]
        threshold = float(values[order[top_n - 1]])
        next_score = float(values[order[top_n]]) if n > top_n else float("nan")
        top_values = values[order[:top_n]]
        z = (scores - mean) / std if std and std > 0 else np.zeros_like(scores)
        z1_idx = np.flatnonzero(mask & (z >= 1.0))
        z2_idx = np.flatnonzero(mask & (z >= 2.0))
        one_pct = max(1, int(math.ceil(n * 0.01)))
        top1_idx = np.flatnonzero(mask)[order[:one_pct]]
        top50_idx = np.flatnonzero(mask)[order[: min(50, n)]]

        forward_row = forward.loc[day].reindex(symbols).to_numpy(dtype=np.float64)
        univ = forward_row[mask]
        univ = univ[np.isfinite(univ)]
        univ_ret = float(univ.mean()) if len(univ) else float("nan")
        for filter_name, indices in (
            ("top10", top_idx[:10]),
            ("top20", top_idx),
            ("top50", top50_idx),
            ("z1", z1_idx),
            ("z2", z2_idx),
            ("top1pct", top1_idx),
        ):
            ret, count = _basket(forward_row, indices)
            screen_rows.append(
                {
                    "date": day,
                    "model": model,
                    "filter": filter_name,
                    "n": count,
                    "ret": ret,
                    "excess": ret - univ_ret if np.isfinite(ret) else np.nan,
                }
            )

        score_rows.append(
            {
                "date": day,
                "model": model,
                "n": n,
                "score_mean": mean,
                "score_std": std,
                "top20_mean": float(top_values.mean()),
                "top20_std": float(top_values.std(ddof=1)),
                "top20_threshold": threshold,
                "rank21_score": next_score,
                "rank20_gap": threshold - next_score
                if np.isfinite(next_score)
                else np.nan,
                "top1pct_threshold": float(values[order[one_pct - 1]]),
                "z1_count": int(len(z1_idx)),
                "z2_count": int(len(z2_idx)),
                "top1pct_count": int(one_pct),
                "top20_share": top_n / n,
            }
        )
        votes[i, top_idx] += 1
        z_sum[i, mask] = z[mask].astype(np.float32)
        z_count[i, mask] += 1
        top_sets[day] = {str(symbols[j]) for j in top_idx}

    return votes, z_sum, z_count, score_rows, screen_rows, top_sets


def _consensus_outputs(
    settings: ConsensusSettings,
    horizon: int,
    days: list[pd.Timestamp],
    symbols: pd.Index,
    base: np.ndarray,
    forward: pd.DataFrame,
    votes: np.ndarray,
    model_names: tuple[str, ...],
    top_sets: dict[str, dict[pd.Timestamp, set[str]]],
) -> dict[str, object]:
    n_models = len(model_names)
    half = int(math.ceil(n_models / 2))
    forward_values = forward.reindex(index=days, columns=symbols).to_numpy(
        dtype=np.float64
    )
    base_forward = np.where(base, forward_values, np.nan)
    universe = np.nanmean(base_forward, axis=1)
    universe = np.where(np.isfinite(universe), universe, 0.0)

    # 1) 票数分布：只看至少被一个模型选中的股票-日。
    selected = votes > 0
    distribution_rows: list[dict] = []
    total_selected = int(selected.sum())
    day_index_grid = np.broadcast_to(np.arange(len(days))[:, None], votes.shape)
    for level in range(1, n_models + 1):
        mask = votes == level
        n = int(mask.sum())
        values = forward_values[mask]
        day_index = day_index_grid[mask]
        valid = np.isfinite(values)
        values = values[valid]
        excess = values - universe[day_index[valid]]
        distribution_rows.append(
            {
                "consensus": level,
                "n_stock_days": n,
                "share": n / total_selected if total_selected else float("nan"),
                "mean_ret": float(values.mean()) if len(values) else float("nan"),
                "mean_excess": float(excess.mean()) if len(excess) else float("nan"),
                "hit_rate": float((values > 0).mean()) if len(values) else float("nan"),
                "n_days": int((mask.any(axis=1)).sum()),
            }
        )
    distribution = pd.DataFrame(distribution_rows)
    distribution.to_csv(
        settings.out_dir / f"consensus_distribution_{horizon}.csv", index=False
    )

    # 2) 逐日池规模。
    pool_half = votes >= half
    pool_all = votes == n_models
    daily_rows = []
    for i, day in enumerate(days):
        per_model_in = []
        for model in model_names:
            selection = top_sets[model].get(day, set())
            if not selection:
                continue
            positions = [symbols.get_loc(s) for s in selection if s in symbols]
            in_pool = int(np.asarray(votes[i, positions] >= half).sum())
            per_model_in.append(in_pool / len(selection))
        daily_rows.append(
            {
                "date": day,
                "union": int((votes[i] > 0).sum()),
                "ge2": int((votes[i] >= 2).sum()),
                "ge_half": int(pool_half[i].sum()),
                "all_models": int(pool_all[i].sum()),
                "avg_model_in_half": float(np.mean(per_model_in))
                if per_model_in
                else float("nan"),
            }
        )
    daily = pd.DataFrame(daily_rows).set_index("date")
    daily.to_csv(settings.out_dir / f"consensus_daily_{horizon}.csv")

    # 3) 共识票数策略。
    strategy_rows: list[dict] = []
    thresholds = sorted({2, 4, 6, half, 8, 10, n_models})
    for threshold in thresholds:
        if threshold < 2 or threshold > n_models:
            continue
        mask = votes >= threshold
        rows = []
        for i, day in enumerate(days):
            indices = np.flatnonzero(mask[i])
            ret, count = _basket(forward_values[i], indices)
            if count == 0:
                continue
            rows.append(
                {
                    "date": day,
                    "threshold": threshold,
                    "n": count,
                    "ret": ret,
                    "excess": ret - universe[i],
                }
            )
        # 高阈值可能一天都没有满足条件的股票，此时 rows 为空、无 date 列。
        frame = pd.DataFrame(rows)
        if frame.empty:
            continue
        frame = frame.set_index("date")
        frame.to_csv(
            settings.out_dir / f"consensus_threshold_{threshold}_{horizon}.csv"
        )
        strategy_rows.append(
            _summary_row(
                frame.reset_index(),
                horizon=horizon,
                name="consensus_ge",
                threshold=str(threshold),
            )
        )
    strategies = pd.DataFrame(strategy_rows)
    strategies.to_csv(
        settings.out_dir / f"consensus_strategy_summary_{horizon}.csv", index=False
    )

    # 4) 两两重叠：Jaccard 与平均重叠只数。
    pair_rows = []
    for a in range(n_models):
        for b in range(a + 1, n_models):
            jaccards, overlaps = [], []
            for day in days:
                left = top_sets[model_names[a]].get(day, set())
                right = top_sets[model_names[b]].get(day, set())
                if not left or not right:
                    continue
                union = len(left | right)
                overlaps.append(len(left & right))
                jaccards.append(len(left & right) / union if union else np.nan)
            pair_rows.append(
                {
                    "model_a": model_names[a],
                    "model_b": model_names[b],
                    "avg_jaccard": float(np.nanmean(jaccards)) if jaccards else np.nan,
                    "avg_overlap": float(np.mean(overlaps)) if overlaps else np.nan,
                    "overlap_share": float(np.mean(overlaps)) / settings.top_k
                    if overlaps
                    else np.nan,
                }
            )
    pd.DataFrame(pair_rows).to_csv(
        settings.out_dir / f"consensus_pairwise_{horizon}.csv", index=False
    )

    # 5) 每个模型 Top20 的「池内 / 池外 / 独有」收益。
    model_rows = []
    for model in model_names:
        in_half, out_half, in_all, unique, shared, unique_share = [], [], [], [], [], []
        for i, day in enumerate(days):
            selection = top_sets[model].get(day, set())
            if not selection:
                continue
            positions = np.asarray(
                [symbols.get_loc(s) for s in selection if s in symbols], dtype=np.int64
            )
            if not len(positions):
                continue
            forward_day = forward_values[i]
            in_half_mask = votes[i, positions] >= half
            out_half_mask = ~in_half_mask
            flags = {
                "half": in_half_mask,
                "all": votes[i, positions] == n_models,
                "unique": votes[i, positions] == 1,
                "shared": votes[i, positions] >= 2,
            }
            unique_share.append(float(flags["unique"].mean()))
            for name, member in flags.items():
                values = forward_day[positions[member]]
                values = values[np.isfinite(values)]
                if not len(values):
                    continue
                if name == "half":
                    in_half.append(float(values.mean()))
                    complement = forward_day[positions[out_half_mask]]
                    complement = complement[np.isfinite(complement)]
                    if len(complement):
                        out_half.append(float(complement.mean()))
                elif name == "all":
                    in_all.append(float(values.mean()))
                elif name == "unique":
                    unique.append(float(values.mean()))
                else:
                    shared.append(float(values.mean()))
        model_rows.append(
            {
                "model": model,
                "days": int(len(in_half)),
                "in_half_ret": float(np.mean(in_half)) if in_half else np.nan,
                "out_half_ret": float(np.mean(out_half)) if out_half else np.nan,
                "in_out_diff": (
                    float(np.mean(in_half) - np.mean(out_half))
                    if in_half and out_half
                    else np.nan
                ),
                "in_all_ret": float(np.mean(in_all)) if in_all else np.nan,
                "unique_ret": float(np.mean(unique)) if unique else np.nan,
                "shared_ret": float(np.mean(shared)) if shared else np.nan,
                "unique_share": float(np.mean(unique_share))
                if unique_share
                else np.nan,
            }
        )
    model_frame = pd.DataFrame(model_rows)
    model_frame.to_csv(
        settings.out_dir / f"consensus_model_overlap_{horizon}.csv", index=False
    )
    return {
        "distribution": distribution,
        "daily": daily,
        "strategies": strategies,
        "model_overlap": model_frame,
        "pairwise": pd.DataFrame(pair_rows),
    }


def _zscore_ensemble(
    settings: ConsensusSettings,
    horizon: int,
    days: list[pd.Timestamp],
    symbols: pd.Index,
    base: np.ndarray,
    forward: pd.DataFrame,
    z_sum: np.ndarray,
    z_count: np.ndarray,
    model_names: tuple[str, ...],
) -> pd.DataFrame:
    half = int(math.ceil(len(model_names) / 2))
    ensemble = np.divide(
        z_sum,
        z_count,
        out=np.full_like(z_sum, np.nan, dtype=np.float64),
        where=z_count > 0,
    )
    forward_values = forward.reindex(index=days, columns=symbols).to_numpy(
        dtype=np.float64
    )
    universe = np.nanmean(np.where(base, forward_values, np.nan), axis=1)
    valid = base & (z_count >= half) & np.isfinite(ensemble)
    rows = []
    for i, day in enumerate(days):
        idx = np.flatnonzero(valid[i])
        if len(idx) < 30:
            idx = np.flatnonzero(base[i] & np.isfinite(ensemble[i]))
        if len(idx) < 30:
            continue
        values = ensemble[i, idx]
        order = np.argsort(-values, kind="stable")
        forward_day = forward_values[i]
        for k in (10, 20):
            selected = idx[order[:k]]
            ret, count = _basket(forward_day, selected)
            if count == 0:
                continue
            rows.append(
                {
                    "date": day,
                    "k": k,
                    "n": count,
                    "ret": ret,
                    "excess": ret - universe[i],
                    "coverage": float(np.mean(z_count[i, selected]) / len(model_names)),
                }
            )
    return pd.DataFrame(rows)


def _dynamic_selection(
    settings: ConsensusSettings,
    horizon: int,
    model_names: tuple[str, ...],
) -> pd.DataFrame:
    ic_frames, excess_frames, ret_frames = {}, {}, {}
    for model in model_names:
        path = settings.run_dir / f"quality_daily_{horizon}_{model}.csv"
        if not path.is_file():
            continue
        frame = load_equity(path)
        ic_frames[model] = frame["ic_eligible"]
        excess_frames[model] = frame["top20_excess"]
        ret_frames[model] = frame["top20_ret"]
    if not ic_frames:
        return pd.DataFrame()
    ic = pd.DataFrame(ic_frames).sort_index()
    excess = pd.DataFrame(excess_frames).reindex(ic.index)
    top_ret = pd.DataFrame(ret_frames).reindex(ic.index)
    equal_excess = excess.mean(axis=1)
    equal_ret = top_ret.mean(axis=1)
    rows = []
    lags = (20, 60, 120)
    for lag in lags:
        # IC 行 t 的标签是 t+1 收盘入场、t+1+H 收盘出场的收益，
        # 到 t+1+H 收盘才可知。选模发生在 d 日收盘，只能用到 d-H-1 及更早的行，
        # 因此滞后 H+1 行；只 shift(1) 会偷看 H 天的未来标签。
        signal = ic.rolling(lag, min_periods=min(20, lag)).mean().shift(horizon + 1)
        picked = []
        dynamic_excess = []
        dynamic_ret = []
        weighted_excess = []
        weighted_ret = []
        usage: dict[str, int] = {}
        for day in ic.index:
            candidates = signal.loc[day].dropna()
            if candidates.empty:
                picked.append("equal")
                dynamic_excess.append(float(equal_excess.loc[day]))
                dynamic_ret.append(float(equal_ret.loc[day]))
                weighted_excess.append(float(equal_excess.loc[day]))
                weighted_ret.append(float(equal_ret.loc[day]))
                continue
            best = str(candidates.idxmax())
            picked.append(best)
            usage[best] = usage.get(best, 0) + 1
            dynamic_excess.append(float(excess.at[day, best]))
            dynamic_ret.append(float(top_ret.at[day, best]))
            weights = candidates.clip(lower=0.0)
            if weights.sum() <= 0:
                weights = pd.Series(1.0, index=candidates.index)
            row = excess.loc[day, weights.index]
            valid = row.notna()
            valid_weights = weights[valid]
            if not valid.any() or valid_weights.sum() <= 0:
                weighted_excess.append(float(equal_excess.loc[day]))
                weighted_ret.append(float(equal_ret.loc[day]))
            else:
                weighted_excess.append(
                    float((row[valid] * valid_weights).sum() / valid_weights.sum())
                )
                ret_row = top_ret.loc[day, weights.index][valid]
                weighted_ret.append(
                    float((ret_row * valid_weights).sum() / valid_weights.sum())
                )
        dynamic_frame = pd.DataFrame(
            {
                "picked": picked,
                "ret": dynamic_ret,
                "excess": dynamic_excess,
                "n": 1,
            },
            index=ic.index,
        )
        weighted_frame = pd.DataFrame(
            {
                "ret": weighted_ret,
                "excess": weighted_excess,
                "n": 1,
            },
            index=ic.index,
        )
        dynamic_frame.to_csv(
            settings.out_dir / f"dynamic_selection_lag{lag}_{horizon}.csv"
        )
        for name, frame in (
            (f"dynamic_ic_lag{lag}", dynamic_frame),
            (f"ic_weighted_lag{lag}", weighted_frame),
        ):
            summary = _summary_row(
                frame.reset_index(),
                horizon=horizon,
                name=name,
                threshold=str(lag),
            )
            summary["usage"] = usage if name.startswith("dynamic") else {}
            rows.append(summary)
    equal_summary = _summary_row(
        pd.DataFrame({"ret": equal_ret, "excess": equal_excess, "n": 1}),
        horizon=horizon,
        name="equal_weight",
        threshold="0",
    )
    rows.append(equal_summary)
    return pd.DataFrame(rows)


def run(settings: ConsensusSettings) -> dict[str, object]:
    settings.out_dir.mkdir(parents=True, exist_ok=True)
    canonical = resolve_model_run(5, "LGB")
    if canonical.run_dir is None:
        raise RuntimeError("缺少规范模型运行目录")
    panel = build_market_panel(settings.price_start, settings.price_end)
    attach_listing_age(panel, read_first_seen(canonical.run_dir))
    symbols = panel.symbols
    LOG.info("共识窗口 %s ~ %s，股票池 %d", settings.start, settings.end, len(symbols))

    manifest: dict[str, object] = {
        "run_dir": str(settings.run_dir),
        "start": settings.start,
        "end": settings.end,
        "models": list(settings.models),
        "horizons": list(settings.horizons),
        "top_k": settings.top_k,
    }
    for horizon in settings.horizons:
        forward = forward_return_matrix(panel.close_qfq, horizon, lag=1)
        forward = forward.loc[
            (forward.index >= pd.Timestamp(settings.start))
            & (forward.index <= pd.Timestamp(settings.end))
        ]
        days = [pd.Timestamp(day) for day in forward.index]
        base = (
            np.vstack([_eligible_base(panel, day, symbols) for day in days])
            if days
            else np.zeros((0, len(symbols)), dtype=bool)
        )
        votes = np.zeros((len(days), len(symbols)), dtype=np.int16)
        z_sum = np.zeros((len(days), len(symbols)), dtype=np.float32)
        z_count = np.zeros((len(days), len(symbols)), dtype=np.int16)
        score_rows: list[dict] = []
        screen_rows: list[dict] = []
        top_sets: dict[str, dict[pd.Timestamp, set[str]]] = {}
        available: list[str] = []
        for model in settings.models:
            run_spec = resolve_model_run(horizon, model)
            if run_spec.run_dir is None:
                LOG.warning("缺少 %s T+%d 训练目录", model, horizon)
                continue
            pred_path = run_spec.run_dir / "pred.parquet"
            if not pred_path.is_file():
                LOG.warning("缺少 %s", pred_path)
                continue
            LOG.info("读取 %s T+%d 预测", model, horizon)
            pred = _load_prediction(
                run_spec.run_dir,
                settings.start,
                settings.end,
                horizon=horizon,
                model=model,
                extension_dir=settings.extension_dir,
            )
            model_votes, model_zsum, model_zcount, scores, screens, sets = _vote(
                panel,
                days,
                symbols,
                base,
                forward,
                pred,
                model,
                settings.top_k,
            )
            votes += model_votes
            z_sum += model_zsum
            z_count += model_zcount
            score_rows.extend(scores)
            screen_rows.extend(screens)
            top_sets[model] = sets
            available.append(model)

        if not available:
            raise RuntimeError(f"T+{horizon} 没有可用模型")
        model_names = tuple(available)
        score_frame = pd.DataFrame(score_rows)
        score_frame.to_csv(
            settings.out_dir / f"score_stats_daily_{horizon}.csv", index=False
        )
        screen_frame = pd.DataFrame(screen_rows)
        screen_frame.to_csv(
            settings.out_dir / f"screening_daily_{horizon}.csv", index=False
        )
        summary_rows = []
        for (model, filter_name), group in screen_frame.groupby(["model", "filter"]):
            summary = _summary_row(
                group, horizon=horizon, name=model, threshold=filter_name
            )
            summary["filter"] = filter_name
            summary["model"] = model
            summary_rows.append(summary)
        screening = pd.DataFrame(summary_rows)
        screening.to_csv(
            settings.out_dir / f"screening_summary_{horizon}.csv", index=False
        )
        score_summary = (
            score_frame.groupby("model")
            .agg(
                days=("date", "nunique"),
                n=("n", "mean"),
                score_std=("score_std", "mean"),
                top20_mean=("top20_mean", "mean"),
                top20_threshold=("top20_threshold", "mean"),
                rank20_gap=("rank20_gap", "mean"),
                z1_count=("z1_count", "mean"),
                z2_count=("z2_count", "mean"),
                top20_share=("top20_share", "mean"),
            )
            .reset_index()
        )
        score_summary.to_csv(
            settings.out_dir / f"score_stats_summary_{horizon}.csv", index=False
        )

        consensus = _consensus_outputs(
            settings,
            horizon,
            days,
            symbols,
            base,
            forward,
            votes,
            model_names,
            top_sets,
        )
        ensemble_daily = _zscore_ensemble(
            settings,
            horizon,
            days,
            symbols,
            base,
            forward,
            z_sum,
            z_count,
            model_names,
        )
        if not ensemble_daily.empty:
            ensemble_daily.to_csv(
                settings.out_dir / f"consensus_ensemble_daily_{horizon}.csv",
                index=False,
            )
            ensemble_summary = []
            for k, group in ensemble_daily.groupby("k"):
                summary = _summary_row(
                    group, horizon=horizon, name="zscore_ensemble", threshold=f"top{k}"
                )
                summary["avg_coverage"] = float(group["coverage"].mean())
                ensemble_summary.append(summary)
            pd.DataFrame(ensemble_summary).to_csv(
                settings.out_dir / f"consensus_ensemble_summary_{horizon}.csv",
                index=False,
            )
        dynamic = _dynamic_selection(settings, horizon, model_names)
        if not dynamic.empty:
            dynamic.to_csv(
                settings.out_dir / f"dynamic_selection_summary_{horizon}.csv",
                index=False,
            )
        manifest[f"T{horizon}"] = {
            "days": len(days),
            "models": list(model_names),
            "selected_stock_days": int((votes > 0).sum()),
            "avg_union": float(consensus["daily"]["union"].mean()),
            "avg_ge_half": float(consensus["daily"]["ge_half"].mean()),
            "avg_all_models": float(consensus["daily"]["all_models"].mean()),
        }
        LOG.info(
            "T+%d 完成：%d 个模型，日均并集 %.1f，共识≥半数 %.1f，全票 %.1f",
            horizon,
            len(model_names),
            manifest[f"T{horizon}"]["avg_union"],
            manifest[f"T{horizon}"]["avg_ge_half"],
            manifest[f"T{horizon}"]["avg_all_models"],
        )
    _write_json(settings.out_dir / "consensus_manifest.json", manifest)
    return manifest


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=OUT_DIR / "deep_consensus")
    parser.add_argument("--start", default=TEST_START)
    parser.add_argument("--end", default=TEST_END)
    parser.add_argument("--price-start", default="2024-11-01")
    parser.add_argument("--price-end", default="2026-10-16")
    parser.add_argument("--models", nargs="*", default=list(MODEL_ORDER))
    parser.add_argument("--horizons", nargs="*", type=int, default=list(HORIZONS))
    parser.add_argument("--top-k", type=int, default=20)
    parser.add_argument("--extension-dir", type=Path, default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    args = parse_args(argv)
    settings = ConsensusSettings(
        run_dir=args.run_dir,
        out_dir=args.out,
        start=args.start,
        end=args.end,
        price_start=args.price_start,
        price_end=args.price_end,
        models=tuple(args.models),
        horizons=tuple(args.horizons),
        top_k=args.top_k,
        extension_dir=args.extension_dir,
    )
    run(settings)


if __name__ == "__main__":
    main()
