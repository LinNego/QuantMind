"""组合选股研究：纯函数（分位 / 组合分 / 次日命中 / bootstrap / BH / 10 日闸门）。

设计约束：
- 不训练新模型；组合分只由成员当日的截面分位加权得到。
- 分位口径与 `backend.shared.signal_contract.compute_rank_pct` 一致：
  并列取最小名次，``pct = (rank - 1) / (n - 1)``，n==1 → 0，非有限值 → NaN。
- 缺失成员不按 0 分处理：只在可用成员间重新归一。
- 次日命中分两套：理论口径（t+1 收盘对 t 收盘）与可执行口径
  （t+1 收盘成交、t+2 收盘卖出）。
- 10 日闸门与生产调仓算法同参：Top50、每日新进 5、持有 10 日、20 日均分门槛、
  分数退出 -0.03、2% 现金、单票 ADV 1%、双边万 3。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import numpy as np
import pandas as pd

TRADING_DAYS_PER_YEAR = 252
COST_RATE_DEFAULT = 0.0003
LOT_SIZE = 100


class MarketView(Protocol):
    """combo_eval 需要的最小行情视图（MarketPanel 满足该协议）。"""

    dates: pd.DatetimeIndex
    symbols: pd.Index
    close_qfq: pd.DataFrame
    limit_up: pd.DataFrame
    limit_down: pd.DataFrame
    suspended: pd.DataFrame
    is_st: pd.Series
    age_days: pd.DataFrame
    amount: pd.DataFrame | None


# ── 分位与组合分 ─────────────────────────────────────────────────────────


def rank_frame(raw: pd.DataFrame) -> pd.DataFrame:
    """逐行（逐交易日）截面分位，与 compute_rank_pct 同口径。

    分母固定为 ``列数 - 1``（非有限值也算进 n，与 PG percent_rank 一致）；
    NaN/±inf 本身不参与排名且输出 NaN；整行无有效值 → 全 NaN；
    只有一个有效值（或只有一列）→ 0.0。
    """
    values = raw.astype("float64")
    values = values.where(np.isfinite(values))
    rank = values.rank(axis=1, method="min", na_option="keep")
    columns = values.shape[1]
    if columns <= 1:
        pct = pd.DataFrame(0.0, index=values.index, columns=values.columns)
    else:
        pct = rank.sub(1.0) / float(columns - 1)
    return pct.where(values.notna())


def combine_ranks(
    ranks: pd.DataFrame,
    members: list[str],
    *,
    method: str = "mean",
    weights: dict[str, float] | None = None,
    min_coverage: int = 1,
    trim: int = 2,
    consensus_gate: dict[str, float] | None = None,
) -> pd.Series:
    """把成员分位合成一个组合分。

    - mean：加权平均；缺失成员在可用成员间重新归一。
    - median：可用成员中位数。
    - trimmed：去掉最高/最低各 ``trim`` 个后的均值（可用成员 < 2*trim+1 → 缺失）。
    - consensus_gate：在 mean 基础上要求 ≥ min_count 个成员分位 ≥ threshold。
    """
    columns = [member for member in members if member in ranks.columns]
    if not columns:
        return pd.Series(np.nan, index=ranks.index, dtype="float64")
    values = ranks.reindex(columns=columns)
    valid = values.notna()
    coverage = valid.sum(axis=1)
    score = pd.Series(np.nan, index=values.index, dtype="float64")
    eligible = coverage >= max(int(min_coverage), 1)

    if method == "median":
        score.loc[eligible] = values.loc[eligible].median(axis=1)
    elif method == "trimmed":
        arr = values.to_numpy(dtype="float64", na_value=np.nan)
        counts = coverage.to_numpy()
        trimmed = np.full_like(arr, np.nan)
        keep_rows = eligible.to_numpy()
        for row in np.where(keep_rows)[0]:
            count = int(counts[row])
            if count < 2 * trim + 1:
                continue
            ordered = np.sort(arr[row][np.isfinite(arr[row])])
            trimmed[row, : count - 2 * trim] = ordered[trim : count - trim]
        kept = np.isfinite(trimmed).sum(axis=1)
        totals = np.nansum(trimmed, axis=1)
        means = np.divide(
            totals, kept, out=np.full_like(totals, np.nan), where=kept > 0
        )
        score.iloc[:] = means
    else:
        weight_map = {
            member: float((weights or {}).get(member, 1.0)) for member in columns
        }
        weight_series = pd.Series(weight_map, dtype="float64").clip(lower=0.0)
        if float(weight_series.sum()) <= 0:
            weight_series[:] = 1.0
        weighted = valid.mul(weight_series, axis=1)
        weight_sum = weighted.sum(axis=1)
        filled = values.fillna(0.0).mul(weighted).sum(axis=1)
        numerator = filled.where(weight_sum > 0)
        score.loc[eligible] = (numerator / weight_sum.where(weight_sum > 0)).loc[
            eligible
        ]

    gate = consensus_gate or {}
    if gate:
        threshold = float(gate.get("threshold", 0.7))
        min_count = int(gate.get("min_count", 1))
        high_count = (values >= threshold).sum(axis=1)
        score[high_count < min_count] = np.nan
    return score


# ── 次日命中 ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class HitDay:
    date: pd.Timestamp
    n_scored: int
    n_top: int
    n_entry: int
    n_theory: int
    n_exec: int
    hit_theory: float
    hit_exec: float
    ret_theory: float
    ret_exec: float
    ret_exec_net: float


def topk_hit_day(
    scores: pd.Series,
    day: pd.Timestamp,
    panel: MarketView,
    k: int,
    *,
    min_listing_days: int = 60,
    cost_bps: float = 6.0,
) -> HitDay | None:
    """单日 TopK 命中。t 收盘出分；t+1 收盘入场；t+2 收盘可执行卖出。"""
    if day not in panel.dates:
        return None
    day_idx = int(panel.dates.get_loc(day))
    if day_idx + 2 >= len(panel.dates):
        return None
    entry_day = panel.dates[day_idx + 1]
    exit_day = panel.dates[day_idx + 2]

    valid = scores.dropna()
    valid = valid[valid.index.isin(panel.symbols)]
    if valid.empty:
        return None
    symbols = valid.index
    known = ~panel.is_st.reindex(symbols).fillna(False).astype(bool)
    known &= panel.age_days.loc[day].reindex(symbols).fillna(-1) >= min_listing_days
    known &= ~panel.suspended.loc[day].reindex(symbols).fillna(True).astype(bool)
    top = valid[known].nlargest(k)
    if top.empty:
        return None
    names = top.index

    signal_price = panel.close_qfq.loc[day].reindex(names)
    entry_price = panel.close_qfq.loc[entry_day].reindex(names)
    exit_price = panel.close_qfq.loc[exit_day].reindex(names)
    entry_blocked = (
        panel.suspended.loc[entry_day].reindex(names).fillna(True).astype(bool)
    )
    entry_blocked |= (
        panel.limit_up.loc[entry_day].reindex(names).fillna(True).astype(bool)
    )
    entry_ok = (~entry_blocked) & np.isfinite(signal_price) & (signal_price > 0)
    entry_ok &= np.isfinite(entry_price) & (entry_price > 0)

    theory_ok = (
        np.isfinite(signal_price)
        & (signal_price > 0)
        & np.isfinite(entry_price)
        & (entry_price > 0)
    )
    theory_ret = (entry_price / signal_price - 1.0)[theory_ok]
    exit_blocked = (
        panel.suspended.loc[exit_day].reindex(names).fillna(True).astype(bool)
    )
    exit_blocked |= (
        panel.limit_down.loc[exit_day].reindex(names).fillna(True).astype(bool)
    )
    exec_ok = entry_ok & (~exit_blocked) & np.isfinite(exit_price) & (exit_price > 0)
    exec_ret = (exit_price / entry_price - 1.0)[exec_ok]
    cost = float(cost_bps) / 10_000.0

    return HitDay(
        date=day,
        n_scored=int(valid.size),
        n_top=int(top.size),
        n_entry=int(entry_ok.sum()),
        n_theory=int(theory_ret.size),
        n_exec=int(exec_ret.size),
        hit_theory=_positive_ratio(theory_ret),
        hit_exec=_positive_ratio(exec_ret),
        ret_theory=_mean(theory_ret),
        ret_exec=_mean(exec_ret),
        ret_exec_net=_mean(exec_ret - cost),
    )


def daily_hits(
    scores_by_date: dict[pd.Timestamp, pd.Series],
    panel: MarketView,
    k: int,
    *,
    start: str,
    end: str,
    min_listing_days: int = 60,
    cost_bps: float = 6.0,
) -> pd.DataFrame:
    """逐日命中表，索引为信号日。"""
    rows = []
    for day in sorted(scores_by_date):
        if day < pd.Timestamp(start) or day > pd.Timestamp(end):
            continue
        hit = topk_hit_day(
            scores_by_date[day],
            day,
            panel,
            k,
            min_listing_days=min_listing_days,
            cost_bps=cost_bps,
        )
        if hit is not None:
            rows.append(hit.__dict__)
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    return frame.set_index("date").sort_index()


def _positive_ratio(values: pd.Series) -> float:
    return float((values > 0).mean()) if len(values) else float("nan")


def _mean(values: pd.Series) -> float:
    return float(values.mean()) if len(values) else float("nan")


# ── 统计 ─────────────────────────────────────────────────────────────────


def paired_diff(
    candidate: pd.DataFrame, baseline: pd.DataFrame, column: str
) -> pd.Series:
    """逐日配对差（组合 − C0），只保留两边都有值的日期。"""
    left = candidate[column].dropna()
    right = baseline[column].dropna()
    common = left.index.intersection(right.index)
    return (left.loc[common] - right.loc[common]).sort_index()


def monthly_mean(diff: pd.Series) -> pd.Series:
    if diff.empty:
        return pd.Series(dtype="float64")
    return diff.groupby(diff.index.to_period("M")).mean()


def block_bootstrap_ci(
    diff: pd.Series,
    *,
    n_boot: int = 2000,
    block: str = "M",
    seed: int = 20261021,
) -> dict[str, float]:
    """按月分块 bootstrap 逐日配对差；返回均值、95% CI 与单侧 p 值。"""
    clean = diff.dropna()
    if clean.empty:
        return _empty_ci()
    groups = [
        g.to_numpy(dtype="float64")
        for _, g in clean.groupby(clean.index.to_period(block))
    ]
    groups = [group for group in groups if group.size > 0]
    if not groups:
        return _empty_ci()
    rng = np.random.default_rng(seed)
    means = np.empty(n_boot, dtype="float64")
    for index in range(n_boot):
        picks = rng.integers(0, len(groups), size=len(groups))
        means[index] = np.concatenate([groups[pick] for pick in picks]).mean()
    low, high = np.percentile(means, [2.5, 97.5])
    return {
        "mean": float(clean.mean()),
        "ci_low": float(low),
        "ci_high": float(high),
        "ci_width": float(high - low),
        "p_value": float((means <= 0).mean()),
        "n_days": int(clean.size),
        "n_blocks": int(len(groups)),
    }


def _empty_ci() -> dict[str, float]:
    return {
        "mean": float("nan"),
        "ci_low": float("nan"),
        "ci_high": float("nan"),
        "ci_width": float("nan"),
        "p_value": 1.0,
        "n_days": 0,
        "n_blocks": 0,
    }


def benjamini_hochberg(pvalues: pd.Series) -> pd.Series:
    """BH q 值（单调化）；NaN 原样保留。"""
    out = pd.Series(np.nan, index=pvalues.index, dtype="float64")
    clean = pvalues.dropna()
    if clean.empty:
        return out
    values = clean.to_numpy(dtype="float64")
    order = np.argsort(values)
    ranked = values[order]
    n = len(ranked)
    q = ranked * n / np.arange(1, n + 1)
    q = np.minimum.accumulate(q[::-1])[::-1]
    out.loc[clean.index[order]] = np.clip(q, 0.0, 1.0)
    return out


# ── 10 日调仓闸门 ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RebalanceConfig:
    capital: float = 10_000_000.0
    top_k: int = 50
    hold_days: int = 10
    entry_per_day: int = 5
    target_positions: int = 50
    cash_buffer: float = 0.02
    adv_participation: float = 0.01
    exit_delta: float = 0.03
    lookback: int = 20
    min_lookback_days: int = 15
    fee_rate: float = COST_RATE_DEFAULT
    min_listing_days: int = 60


@dataclass
class _OpenPosition:
    symbol: str
    entry_idx: int
    entry_price: float
    shares: int
    buy_fee: float


@dataclass
class RebalanceResult:
    equity: pd.Series
    performance: dict[str, float]
    avg_trade_return: float
    n_entries: int
    n_exits: int
    deferred_exits: int
    capacity_unchecked: int
    open_positions: int
    trades: list[dict] = field(default_factory=list)


def simulate_rebalance(
    scores_by_date: dict[pd.Timestamp, pd.Series],
    panel: MarketView,
    *,
    start: str,
    end: str,
    config: RebalanceConfig | None = None,
    dates: list[pd.Timestamp] | None = None,
) -> RebalanceResult:
    """纸面滚动调仓：信号 t 收盘生成，t+1 收盘执行。

    与生产调仓算法同参：Top50 / 新进 5 / 持有 10 / S≥20 日均分 / S<均分-0.03 退出。
    返回的绩效只统计 ``[start, end]`` 内的权益曲线。
    """
    cfg = config or RebalanceConfig()
    start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
    signal_days = dates or [
        day for day in sorted(scores_by_date) if start_ts <= day <= end_ts
    ]
    if not signal_days:
        return _empty_rebalance()

    first_idx = int(panel.dates.get_loc(signal_days[0]))
    last_idx = int(panel.dates.get_loc(signal_days[-1]))
    lookback_idx = max(0, first_idx - max(cfg.lookback, cfg.min_lookback_days) - 2)
    wide = pd.DataFrame.from_dict(scores_by_date, orient="index")
    wide = wide.reindex(
        index=panel.dates[lookback_idx : last_idx + 1], columns=panel.symbols
    )
    mean20 = (
        wide.rolling(cfg.lookback, min_periods=cfg.min_lookback_days).mean().shift(1)
    )
    close = panel.close_qfq.reindex(columns=panel.symbols)
    close_ffill = close.ffill()
    amount = (
        panel.amount.reindex(columns=panel.symbols)
        if panel.amount is not None
        else None
    )

    cash = float(cfg.capital)
    held: dict[str, _OpenPosition] = {}
    equity_rows: dict[pd.Timestamp, float] = {signal_days[0]: cash}
    trades: list[dict] = []
    deferred = 0
    capacity_unchecked = 0

    for day in signal_days:
        idx = int(panel.dates.get_loc(day))
        if idx + 1 >= len(panel.dates):
            continue
        exec_day = panel.dates[idx + 1]
        scores = wide.loc[day]
        mean_scores = mean20.loc[day]
        ranked = _ranked_candidates(scores, day, panel, cfg)
        top = set(ranked.head(cfg.top_k).index)

        planned_exits = _planned_exits(held, scores, mean_scores, idx, cfg)
        slots = cfg.target_positions - (len(held) - len(planned_exits))
        entries = _pick_entries(
            ranked, held, planned_exits, scores, mean_scores, slots, cfg
        )

        cash = _execute_exits(
            held, planned_exits, exec_day, close, panel, cash, cfg, trades
        )
        deferred += sum(1 for symbol in planned_exits if symbol in held)
        slots = cfg.target_positions - len(held)
        cash, unchecked = _execute_entries(
            held,
            entries[: max(0, slots)],
            top,
            exec_day,
            day,
            close,
            amount,
            panel,
            cash,
            cfg,
            trades,
        )
        capacity_unchecked += unchecked
        equity_rows[exec_day] = _mark_equity(cash, held, exec_day, close_ffill)

    equity = pd.Series(equity_rows).sort_index()
    window = equity[(equity.index >= start_ts) & (equity.index <= end_ts)]
    performance = _equity_performance(window)
    closed = [trade for trade in trades if trade.get("exit_price") is not None]
    avg_trade = (
        float(np.mean([trade["net_return"] for trade in closed]))
        if closed
        else float("nan")
    )
    return RebalanceResult(
        equity=window,
        performance=performance,
        avg_trade_return=avg_trade,
        n_entries=sum(1 for trade in trades if trade["side"] == "entry"),
        n_exits=len(closed),
        deferred_exits=deferred,
        capacity_unchecked=capacity_unchecked,
        open_positions=len(held),
        trades=trades,
    )


def _ranked_candidates(
    scores: pd.Series, day: pd.Timestamp, panel: MarketView, cfg: RebalanceConfig
) -> pd.Series:
    valid = scores.dropna()
    valid = valid[valid.index.isin(panel.symbols)]
    if valid.empty:
        return valid
    symbols = valid.index
    mask = ~panel.is_st.reindex(symbols).fillna(False).astype(bool)
    mask &= panel.age_days.loc[day].reindex(symbols).fillna(-1) >= cfg.min_listing_days
    mask &= ~panel.suspended.loc[day].reindex(symbols).fillna(True).astype(bool)
    return valid[mask].sort_values(ascending=False, kind="mergesort")


def _planned_exits(
    held: dict[str, _OpenPosition],
    scores: pd.Series,
    mean_scores: pd.Series,
    idx: int,
    cfg: RebalanceConfig,
) -> set[str]:
    exits: set[str] = set()
    for symbol, position in held.items():
        held_days = idx - position.entry_idx + 1
        if held_days >= cfg.hold_days:
            exits.add(symbol)
            continue
        score = scores.get(symbol, np.nan)
        mean_score = mean_scores.get(symbol, np.nan)
        if np.isfinite(score) and np.isfinite(mean_score):
            if score < mean_score - cfg.exit_delta:
                exits.add(symbol)
    return exits


def _pick_entries(
    ranked: pd.Series,
    held: dict[str, _OpenPosition],
    planned_exits: set[str],
    scores: pd.Series,
    mean_scores: pd.Series,
    slots: int,
    cfg: RebalanceConfig,
) -> list[str]:
    limit = min(cfg.entry_per_day, max(0, slots))
    if limit <= 0:
        return []
    entries: list[str] = []
    for symbol in ranked.index:
        if symbol in held:
            continue
        score = scores.get(symbol, np.nan)
        mean_score = mean_scores.get(symbol, np.nan)
        if not np.isfinite(mean_score) or not np.isfinite(score):
            continue
        if score < mean_score:
            continue
        entries.append(symbol)
        if len(entries) >= limit:
            break
    return entries


def _execute_exits(
    held: dict[str, _OpenPosition],
    planned_exits: set[str],
    exec_day: pd.Timestamp,
    close: pd.DataFrame,
    panel: MarketView,
    cash: float,
    cfg: RebalanceConfig,
    trades: list[dict],
) -> float:
    for symbol in list(planned_exits):
        position = held.get(symbol)
        if position is None:
            continue
        if _exit_blocked(symbol, exec_day, panel):
            continue
        price = float(close.at[exec_day, symbol])
        if not np.isfinite(price) or price <= 0:
            continue
        proceeds = position.shares * price
        fee = proceeds * cfg.fee_rate
        cash += proceeds - fee
        net_return = (price * (1 - cfg.fee_rate)) / (
            position.entry_price * (1 + cfg.fee_rate)
        ) - 1.0
        trades.append(
            {
                "side": "exit",
                "symbol": symbol,
                "date": exec_day,
                "shares": position.shares,
                "exit_price": price,
                "net_return": net_return,
            }
        )
        del held[symbol]
    return cash


def _execute_entries(
    held: dict[str, _OpenPosition],
    entries: list[str],
    top: set[str],
    exec_day: pd.Timestamp,
    signal_day: pd.Timestamp,
    close: pd.DataFrame,
    amount: pd.DataFrame | None,
    panel: MarketView,
    cash: float,
    cfg: RebalanceConfig,
    trades: list[dict],
) -> tuple[float, int]:
    unchecked = 0
    base_target = cfg.capital * (1.0 - cfg.cash_buffer) / cfg.target_positions
    for symbol in entries:
        if symbol not in top or symbol in held:
            continue
        if _entry_blocked(symbol, exec_day, panel):
            continue
        price = float(close.at[exec_day, symbol])
        if not np.isfinite(price) or price <= 0:
            continue
        target = base_target
        if amount is None:
            unchecked += 1
        else:
            cap = float(amount.at[signal_day, symbol])
            if not np.isfinite(cap) or cap <= 0:
                unchecked += 1
            else:
                target = min(target, cap * cfg.adv_participation)
        shares = int(target // (price * LOT_SIZE)) * LOT_SIZE
        floor_cash = cfg.capital * cfg.cash_buffer
        spendable = max(0.0, cash - floor_cash)
        affordable = (
            int(spendable // (price * (1.0 + cfg.fee_rate) * LOT_SIZE)) * LOT_SIZE
        )
        shares = min(shares, affordable)
        if shares <= 0:
            continue
        cost = shares * price
        fee = cost * cfg.fee_rate
        cash -= cost + fee
        held[symbol] = _OpenPosition(
            symbol=symbol,
            entry_idx=int(panel.dates.get_loc(exec_day)),
            entry_price=price,
            shares=shares,
            buy_fee=fee,
        )
        trades.append(
            {
                "side": "entry",
                "symbol": symbol,
                "date": exec_day,
                "shares": shares,
                "entry_price": price,
                "net_return": None,
                "exit_price": None,
            }
        )
    return cash, unchecked


def _entry_blocked(symbol: str, day: pd.Timestamp, panel: MarketView) -> bool:
    return bool(
        panel.suspended.at[day, symbol]
        or panel.limit_up.at[day, symbol]
        or panel.limit_down.at[day, symbol]
    )


def _exit_blocked(symbol: str, day: pd.Timestamp, panel: MarketView) -> bool:
    return bool(panel.suspended.at[day, symbol] or panel.limit_down.at[day, symbol])


def _mark_equity(
    cash: float,
    held: dict[str, _OpenPosition],
    day: pd.Timestamp,
    close_ffill: pd.DataFrame,
) -> float:
    value = cash
    for symbol, position in held.items():
        value += position.shares * float(close_ffill.at[day, symbol])
    return float(value)


def _equity_performance(equity: pd.Series) -> dict[str, float]:
    clean = equity.dropna().astype("float64")
    if len(clean) < 2:
        return {}
    daily = clean.pct_change().dropna()
    total = float(clean.iloc[-1] / clean.iloc[0] - 1.0)
    days = len(clean) - 1
    annual = float((1.0 + total) ** (TRADING_DAYS_PER_YEAR / days) - 1.0)
    peak = clean.cummax()
    drawdown = float((clean / peak - 1.0).min())
    sharpe = (
        float(daily.mean() / daily.std(ddof=1) * np.sqrt(TRADING_DAYS_PER_YEAR))
        if len(daily) > 1 and daily.std(ddof=1) > 0
        else float("nan")
    )
    return {
        "total_return": total,
        "annual_return": annual,
        "max_drawdown": drawdown,
        "sharpe": sharpe,
        "trading_days": int(days),
    }


def _empty_rebalance() -> RebalanceResult:
    return RebalanceResult(
        equity=pd.Series(dtype="float64"),
        performance={},
        avg_trade_return=float("nan"),
        n_entries=0,
        n_exits=0,
        deferred_exits=0,
        capacity_unchecked=0,
        open_positions=0,
    )


# ── 元模型（C16，仅研究） ────────────────────────────────────────────────


def meta_features(
    ranks: pd.DataFrame, members: list[str], min_coverage: int
) -> pd.DataFrame:
    """成员分位 + 覆盖 + 分歧；缺失成员以 0.5 填充并保留覆盖列。"""
    columns = [member for member in members if member in ranks.columns]
    values = ranks.reindex(columns=columns)
    valid = values.notna()
    coverage = valid.sum(axis=1)
    filled = values.fillna(0.5)
    features = filled.copy()
    features.columns = [f"r_{column}" for column in columns]
    features["coverage"] = coverage.astype("float64")
    features["dispersion"] = values.std(axis=1).fillna(0.0)
    features["high_count"] = (values >= 0.7).sum(axis=1).astype("float64")
    return features[coverage >= min_coverage]


@dataclass
class MetaModel:
    model: object
    feature_names: list[str]


def fit_meta_logit(
    features: pd.DataFrame, labels: pd.Series, *, l2: float = 1.0, max_iter: int = 500
) -> MetaModel:
    """L2 逻辑回归；scikit-learn 缺失时显式报错（C16 标 unavailable）。"""
    from sklearn.linear_model import LogisticRegression

    joined = features.join(labels.rename("label"), how="inner").dropna()
    if joined["label"].nunique() < 2:
        raise ValueError("元模型标签单一，无法训练")
    model = LogisticRegression(
        C=1.0 / max(l2, 1e-6), max_iter=max_iter, solver="lbfgs", random_state=20261021
    )
    model.fit(joined[features.columns], joined["label"].astype(int))
    return MetaModel(model=model, feature_names=list(features.columns))


def predict_meta(model: MetaModel, features: pd.DataFrame) -> pd.Series:
    values = features.reindex(columns=model.feature_names).fillna(0.5)
    return pd.Series(
        model.model.predict_proba(values)[:, 1], index=features.index, dtype="float64"
    )
