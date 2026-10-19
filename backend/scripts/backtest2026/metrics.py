"""回测指标：IC / 胜率 / 组合绩效。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .data import MarketPanel
from .filters import signal_eligible_mask


def _spearman(left: pd.Series, right: pd.Series) -> float:
    joined = pd.concat([left, right], axis=1, keys=["a", "b"]).dropna()
    if len(joined) < 3:
        return float("nan")
    return float(joined["a"].rank().corr(joined["b"].rank()))


def quality_by_day(
    pred_by_date: dict[pd.Timestamp, pd.Series],
    forward_returns: pd.DataFrame,
    panel: MarketPanel,
    *,
    topk: int = 20,
    exclude_st: bool = True,
    exclude_limits: bool = True,
    exclude_new: bool = True,
    label_matrix: pd.DataFrame | None = None,
) -> pd.DataFrame:
    rows = []
    for day in sorted(pred_by_date):
        if day not in forward_returns.index:
            continue
        pred = pred_by_date[day]
        ret = forward_returns.loc[day].reindex(pred.index)
        symbols = pred.index
        mask = pred.notna() & ret.notna()
        mask &= ~panel.suspended.loc[day].reindex(symbols).fillna(True).astype(bool)
        if exclude_st:
            mask &= ~panel.is_st.reindex(symbols).fillna(False).astype(bool)
        if exclude_limits:
            mask &= ~panel.limit_up.loc[day].reindex(symbols).fillna(True).astype(bool)
            mask &= ~panel.limit_down.loc[day].reindex(symbols).fillna(True).astype(
                bool
            )
        if exclude_new:
            mask &= panel.age_days.loc[day].reindex(symbols).fillna(-1) >= 60
        eligible = mask
        top = pred[eligible].nlargest(topk)
        bottom = pred[eligible].nsmallest(topk)
        top_ret = ret.reindex(top.index).dropna()
        bottom_ret = ret.reindex(bottom.index).dropna()
        directional = ret[eligible & (ret != 0)]
        pred_dir = pred.reindex(directional.index)
        ic_label = np.nan
        if label_matrix is not None and day in label_matrix.index:
            label = label_matrix.loc[day].reindex(pred.index)
            label_mask = eligible & label.notna()
            ic_label = _spearman(pred[label_mask], label[label_mask])
        rows.append(
            {
                "date": day,
                "n_eligible": int(eligible.sum()),
                "ic_eligible": _spearman(pred[eligible], ret[eligible]),
                "ic_all": _spearman(pred[ret.notna()], ret[ret.notna()]),
                "ic_label": ic_label,
                "hit_all": float((np.sign(pred_dir) == np.sign(directional)).mean())
                if len(directional)
                else np.nan,
                "top_ret": float(top_ret.mean()) if len(top_ret) else np.nan,
                "top_hit": float((top_ret > 0).mean()) if len(top_ret) else np.nan,
                "bottom_ret": float(bottom_ret.mean()) if len(bottom_ret) else np.nan,
                "long_short": float(top_ret.mean() - bottom_ret.mean())
                if len(top_ret) and len(bottom_ret)
                else np.nan,
            }
        )
    return pd.DataFrame(rows).set_index("date").sort_index()


def summarize_quality(daily: pd.DataFrame) -> dict:
    out: dict[str, float] = {}
    for column in (
        "ic_eligible",
        "ic_all",
        "ic_label",
        "hit_all",
        "top_ret",
        "top_hit",
        "bottom_ret",
        "long_short",
    ):
        series = daily[column].dropna()
        out[f"{column}_mean"] = float(series.mean()) if len(series) else float("nan")
        out[f"{column}_std"] = (
            float(series.std(ddof=1)) if len(series) > 1 else float("nan")
        )
    ic = daily["ic_eligible"].dropna()
    # 与训练报告同口径：ICIR = mean(IC) / std(IC)，**不年化**。
    out["icir"] = (
        float(ic.mean() / ic.std(ddof=1))
        if len(ic) > 1 and ic.std(ddof=1) > 0
        else float("nan")
    )
    out["ic_positive_ratio"] = float((ic > 0).mean()) if len(ic) else float("nan")
    out["days"] = int(len(daily))
    return out


def monthly_quality(daily: pd.DataFrame) -> pd.DataFrame:
    grouped = daily.groupby(daily.index.to_period("M"))
    return grouped.agg(
        ic_eligible=("ic_eligible", "mean"),
        hit_all=("hit_all", "mean"),
        top_ret=("top_ret", "mean"),
        top_hit=("top_hit", "mean"),
        long_short=("long_short", "mean"),
        n_eligible=("n_eligible", "mean"),
    )


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return float("nan")
    peak = equity.cummax()
    drawdown = equity / peak - 1.0
    return float(drawdown.min())


def performance_summary(equity: pd.Series) -> dict:
    equity = equity.dropna().astype(float)
    if len(equity) < 2:
        return {}
    daily_ret = equity.pct_change().dropna()
    total = float(equity.iloc[-1] / equity.iloc[0] - 1.0)
    n_days = len(equity) - 1
    annual = (
        float((1.0 + total) ** (252.0 / n_days) - 1.0) if n_days > 0 else float("nan")
    )
    vol = (
        float(daily_ret.std(ddof=1) * np.sqrt(252))
        if len(daily_ret) > 1
        else float("nan")
    )
    sharpe = (
        float(daily_ret.mean() / daily_ret.std(ddof=1) * np.sqrt(252))
        if len(daily_ret) > 1 and daily_ret.std(ddof=1) > 0
        else float("nan")
    )
    return {
        "start": equity.index[0].strftime("%Y-%m-%d"),
        "end": equity.index[-1].strftime("%Y-%m-%d"),
        "trading_days": int(n_days),
        "total_return": total,
        "annual_return": annual,
        "annual_vol": vol,
        "sharpe": sharpe,
        "max_drawdown": max_drawdown(equity),
        "daily_win_rate": float((daily_ret > 0).mean())
        if len(daily_ret)
        else float("nan"),
        "final_equity": float(equity.iloc[-1]),
    }


def trade_summary(trades: pd.DataFrame, initial_capital: float) -> dict:
    if trades.empty:
        return {}
    buys = trades[trades["side"] == "buy"]
    sells = trades[trades["side"] == "sell"]
    paired = sells.merge(
        buys[["bucket", "symbol", "amount", "fee"]],
        on=["bucket", "symbol"],
        suffixes=("", "_buy"),
    )
    paired["trade_ret"] = paired["net_pnl"] / (paired["amount_buy"] + paired["fee_buy"])
    return {
        "n_buy_orders": int(len(buys)),
        "n_sell_orders": int(len(sells)),
        "n_round_trips": int(len(paired)),
        "total_buy_amount": float(buys["amount"].sum()),
        "total_fees": float(trades["fee"].sum()),
        "turnover_x": float(buys["amount"].sum() / initial_capital),
        "avg_trade_return": float(paired["trade_ret"].mean())
        if len(paired)
        else float("nan"),
        "trade_win_rate": float((paired["net_pnl"] > 0).mean())
        if len(paired)
        else float("nan"),
        "deferred_exits": int(
            (
                pd.to_datetime(sells["date"]) > pd.to_datetime(sells["scheduled_exit"])
            ).sum()
        ),
        "forced_exits": int((sells["exit_reason"] == "forced_exit").sum()),
    }


def monthly_equity(equity: pd.Series) -> pd.Series:
    monthly = equity.groupby(equity.index.to_period("M")).last()
    first = equity.iloc[0]
    base = pd.Series(index=monthly.index, dtype=float)
    previous = first
    for period, value in monthly.items():
        base.loc[period] = value / previous - 1.0
        previous = value
    return base


def index_summary(
    index_closes: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp
) -> dict:
    window = index_closes[(index_closes.index >= start) & (index_closes.index <= end)]
    out = {}
    for column in window.columns:
        series = window[column].dropna()
        if len(series) > 1:
            out[column] = float(series.iloc[-1] / series.iloc[0] - 1.0)
    return out
