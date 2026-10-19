"""多模型统一窗口的预测质量与等权基准。

与单模型 ``metrics.quality_by_day`` 的区别：
- 一次算出 Top5 / Top10 / Top20 三档，避免重复筛选；
- 同时给「上涨比例」与「方向命中率」（sign(pred)=sign(ret)）；
- 给十分位收益与单调性，用来看模型是否只是押中一个风格；
- 等权基准按策略同一成交时点（t+1 收盘买入，t+2 收盘卖出）计算。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .data import MarketPanel
from .filters import entry_blocked_mask, signal_eligible_mask
from .strict_stats import (
    block_bootstrap_mean,
    newey_west_mean,
    spearman,
)

TOPK_LIST = (5, 10, 20)
DECILES = tuple(range(1, 11))


def benchmark_daily(
    panel: MarketPanel,
    pred_by_date: dict[pd.Timestamp, pd.Series],
    initial_capital: float,
    *,
    lag: int = 2,
) -> pd.DataFrame:
    """等权可交易池基准，成交时点与策略一致。

    信号日 t 收盘选池；t+1 收盘买入（剔除当日停牌/涨跌停）；t+2 收盘卖出。
    收益记在 t+2，首个入场日 t+1 记 0 收益，使日收益序列与组合完全对齐。
    """
    position = {day: i for i, day in enumerate(panel.dates)}
    signal_days = sorted(pred_by_date)
    if not signal_days:
        raise ValueError("预测面板为空")
    rows: list[dict[str, object]] = []
    for day in signal_days:
        idx = position.get(day)
        if idx is None or idx + lag >= len(panel.dates):
            continue
        pred = pred_by_date[day]
        mask = signal_eligible_mask(
            pred,
            day,
            panel.limit_up,
            panel.limit_down,
            panel.suspended,
            panel.age_days,
            panel.is_st,
        )
        selected = mask[mask].index
        if len(selected) == 0:
            continue
        entry_idx = idx + 1
        entry_day = panel.dates[entry_idx]
        blocked = entry_blocked_mask(
            selected, entry_day, panel.limit_up, panel.limit_down, panel.suspended
        )
        selected = selected[~blocked.reindex(selected).fillna(True).to_numpy()]
        if len(selected) == 0:
            continue
        entry = panel.close_qfq.iloc[entry_idx].reindex(selected)
        exit_price = panel.close_qfq.iloc[idx + lag].reindex(selected)
        returns = (exit_price / entry - 1.0).replace([np.inf, -np.inf], np.nan).dropna()
        if returns.empty:
            continue
        rows.append(
            {
                "date": panel.dates[idx + lag],
                "return": float(returns.mean()),
                "n": int(len(returns)),
            }
        )

    first_signal_idx = position[signal_days[0]]
    first_entry = panel.dates[first_signal_idx + 1]
    anchor = pd.DataFrame(
        {"return": [0.0], "n": [0]},
        index=pd.DatetimeIndex([first_entry], name="date"),
    )
    if rows:
        frame = pd.DataFrame(rows).set_index("date").sort_index()
        frame = pd.concat([anchor, frame])
        frame = frame[~frame.index.duplicated(keep="first")].sort_index()
    else:
        frame = anchor
    frame["equity"] = initial_capital * (1.0 + frame["return"]).cumprod()
    return frame


def _decile_returns(pred: pd.Series, ret: pd.Series) -> pd.Series:
    ranked = pred.rank(method="first")
    buckets = pd.qcut(ranked, 10, labels=False, duplicates="drop")
    means = ret.groupby(buckets).mean().reindex(range(10))
    means.index = list(DECILES)
    return means


def quality_by_day_multi(
    pred_by_date: dict[pd.Timestamp, pd.Series],
    forward_returns: pd.DataFrame,
    panel: MarketPanel,
) -> pd.DataFrame:
    """逐日质量：IC、TopK 收益/上涨率/方向命中、十分位收益。"""
    rows: list[dict[str, object]] = []
    for day in sorted(pred_by_date):
        if day not in forward_returns.index:
            continue
        pred = pred_by_date[day]
        ret = forward_returns.loc[day].reindex(pred.index)
        mask = signal_eligible_mask(
            pred,
            day,
            panel.limit_up,
            panel.limit_down,
            panel.suspended,
            panel.age_days,
            panel.is_st,
        )
        mask &= ret.notna()
        eligible = mask[mask].index
        if len(eligible) < 30:
            continue
        pred_e = pred.reindex(eligible)
        ret_e = ret.reindex(eligible)

        directional = ret_e[ret_e != 0]
        pred_dir = pred_e.reindex(directional.index)
        row: dict[str, object] = {
            "date": day,
            "n_eligible": int(len(eligible)),
            "ic_eligible": spearman(pred_e, ret_e),
            "hit_all": float((np.sign(pred_dir) == np.sign(directional)).mean())
            if len(directional)
            else np.nan,
            "pool_ret": float(ret_e.mean()),
        }

        deciles = _decile_returns(pred_e, ret_e)
        for number, value in deciles.items():
            row[f"d{number}"] = float(value) if pd.notna(value) else np.nan

        for k in TOPK_LIST:
            top = pred_e.nlargest(k)
            bottom = pred_e.nsmallest(k)
            top_ret = ret_e.reindex(top.index).dropna()
            bottom_ret = ret_e.reindex(bottom.index).dropna()
            row[f"top{k}_ret"] = float(top_ret.mean())
            row[f"top{k}_hit"] = float((top_ret > 0).mean())
            row[f"top{k}_dir"] = float(
                (np.sign(pred_e.reindex(top_ret.index)) == np.sign(top_ret)).mean()
            )
            row[f"top{k}_excess"] = float(top_ret.mean() - ret_e.mean())
            row[f"bottom{k}_ret"] = float(bottom_ret.mean())
            row[f"ls{k}"] = float(top_ret.mean() - bottom_ret.mean())

        rows.append(row)

    frame = pd.DataFrame(rows)
    if frame.empty:
        raise RuntimeError("统一窗口内没有可评估的交易日")
    return frame.set_index("date").sort_index()


def summarize_quality_multi(
    daily: pd.DataFrame,
    *,
    horizon: int,
    block: int = 20,
    n_boot: int = 5000,
    seed: int = 20261019,
) -> dict[str, object]:
    """质量摘要：均值/标准差、ICIR、Newey-West t、块自助区间、十分位单调性。"""
    summary: dict[str, object] = {"days": int(len(daily))}
    columns = ["ic_eligible", "hit_all", "pool_ret"]
    for k in TOPK_LIST:
        columns.extend(
            [f"top{k}_ret", f"top{k}_hit", f"top{k}_dir", f"top{k}_excess", f"ls{k}"]
        )
    for column in columns:
        series = daily[column].dropna().astype(float)
        summary[f"{column}_mean"] = (
            float(series.mean()) if len(series) else float("nan")
        )
        summary[f"{column}_std"] = (
            float(series.std(ddof=1)) if len(series) > 1 else float("nan")
        )

    ic = daily["ic_eligible"].dropna().astype(float)
    std = float(ic.std(ddof=1)) if len(ic) > 1 else float("nan")
    summary["icir"] = (
        float(ic.mean() / std) if len(ic) > 1 and std > 0 else float("nan")
    )
    summary["ic_positive_ratio"] = float((ic > 0).mean()) if len(ic) else float("nan")
    nw = newey_west_mean(ic, lag=max(1, horizon - 1))
    summary["ic_nw_t"] = nw["t"]
    summary["ic_nw_se"] = nw["se"]
    boot = block_bootstrap_mean(ic, block=block, n_boot=n_boot, seed=seed)
    summary["ic_ci_low"] = boot["ci_low"]
    summary["ic_ci_high"] = boot["ci_high"]
    summary["ic_p_positive"] = boot["p_positive"]

    decile_means = [float(daily[f"d{number}"].mean()) for number in DECILES]
    summary["decile_returns"] = decile_means
    summary["decile_monotonicity"] = spearman(list(DECILES), decile_means)
    summary["top_bottom_spread"] = (
        decile_means[-1] - decile_means[0] if len(decile_means) == 10 else float("nan")
    )
    summary["monthly"] = monthly_quality_multi(daily).to_dict(orient="index")
    return summary


def monthly_quality_multi(daily: pd.DataFrame) -> pd.DataFrame:
    """月度聚合：IC、全池方向、TopK 上涨率/收益。"""
    grouped = daily.groupby(daily.index.to_period("M"))
    columns = {
        "ic": ("ic_eligible", "mean"),
        "hit_all": ("hit_all", "mean"),
        "top5_ret": ("top5_ret", "mean"),
        "top5_hit": ("top5_hit", "mean"),
        "top10_ret": ("top10_ret", "mean"),
        "top10_hit": ("top10_hit", "mean"),
        "top20_ret": ("top20_ret", "mean"),
        "top20_hit": ("top20_hit", "mean"),
        "ls10": ("ls10", "mean"),
        "n_eligible": ("n_eligible", "mean"),
    }
    frame = grouped.agg(**columns)
    frame.index = frame.index.astype(str)
    return frame


def decile_frame(daily_by_model: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """各模型的十分位平均收益，行=模型、列=D1..D10。"""
    rows = {}
    for model, daily in daily_by_model.items():
        rows[model] = {
            f"D{number}": float(daily[f"d{number}"].mean()) for number in DECILES
        }
    return pd.DataFrame(rows).T
