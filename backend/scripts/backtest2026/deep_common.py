"""深度评估的共用数据层：行业、市值、持仓重建与逐日因子收益。

只在深度评估脚本里使用，不改变主回测口径。所有价格/成交额仍直读 QuantDB，
行业与股本来自 QuantDB ``instrument_list.parquet``（同花顺行业、自由流通股本）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from .config import DATA_ROOT
from .data import MarketPanel, normalize_symbol
from .filters import signal_eligible_mask

#: 真正 OOS 的完整测试期（训练切分：test 2025-02-01 ~ 2026-08-28）。
TEST_START = "2025-02-03"
TEST_END = "2026-08-26"
#: 验证期：模型早停用过，不是纯 OOS，只作为跨行情对照。
VALID_START = "2023-07-03"
VALID_END = "2025-01-21"

UNKNOWN_INDUSTRY = "未分类"


@dataclass(frozen=True)
class FactorDay:
    """某一天的行业/市值因子收益与残差。"""

    beta_industry: pd.Series
    gamma_size: float
    residual: pd.Series
    universe_return: float
    universe_industry_weight: pd.Series
    universe_size: float
    n_universe: int


def load_industry_map() -> pd.Series:
    """前缀式代码 → 同花顺行业名。缺失归入「未分类」。"""
    path = DATA_ROOT / "2_base_sector" / "instrument_detail" / "instrument_list.parquet"
    frame = pd.read_parquet(path, columns=["Symbol", "rs_hyname"])
    frame["symbol"] = normalize_symbol(frame["Symbol"])
    frame["industry"] = frame["rs_hyname"].fillna(UNKNOWN_INDUSTRY).astype(str)
    return (
        frame.drop_duplicates(subset=["symbol"])
        .set_index("symbol")["industry"]
        .sort_index()
    )


def load_float_shares() -> pd.Series:
    """前缀式代码 → 自由流通股本（股）。缺失/非正时退回总股本。"""
    path = DATA_ROOT / "2_base_sector" / "instrument_detail" / "instrument_list.parquet"
    frame = pd.read_parquet(path, columns=["Symbol", "FreeLtgb", "J_zgb"])
    frame["symbol"] = normalize_symbol(frame["Symbol"])
    free = pd.to_numeric(frame["FreeLtgb"], errors="coerce")
    total = pd.to_numeric(frame["J_zgb"], errors="coerce")
    shares = free.where(free > 0, total)
    return (
        frame.assign(shares=shares)
        .drop_duplicates(subset=["symbol"])
        .set_index("symbol")["shares"]
        .sort_index()
    )


def log_size_matrix(panel: MarketPanel) -> pd.DataFrame:
    """逐日 log(自由流通市值)；缺失股本为 NaN，不做插补。"""
    shares = load_float_shares().reindex(panel.symbols)
    value = panel.close_raw.mul(shares, axis=1)
    value = value.where(value > 0)
    return np.log(value)


def cross_sectional_z(frame: pd.DataFrame, *, clip: float = 5.0) -> pd.DataFrame:
    """逐日截面标准化（均值 0、标准差 1），仅使用当日有效值。"""
    mean = frame.mean(axis=1)
    std = frame.std(axis=1, ddof=1)
    z = frame.sub(mean, axis=0).div(std.replace(0.0, np.nan), axis=0)
    return z.clip(-clip, clip)


def daily_returns(panel: MarketPanel) -> pd.DataFrame:
    """前复权日收益；停牌日保持 NaN，不用 0 填充。"""
    return panel.close_qfq.pct_change(fill_method=None)


def eligible_universe(
    panel: MarketPanel,
    day: pd.Timestamp,
    *,
    min_listing_days: int = 60,
) -> pd.Index:
    """归因用可投资域：有价格、非停牌、非 ST、上市够久。"""
    valid = panel.close_qfq.loc[day].notna()
    valid &= ~panel.suspended.loc[day].fillna(True).astype(bool)
    valid &= ~panel.is_st.reindex(panel.symbols).fillna(False).astype(bool)
    valid &= panel.age_days.loc[day].fillna(-1) >= min_listing_days
    return panel.symbols[valid.to_numpy()]


def factor_day(
    day: pd.Timestamp,
    returns: pd.DataFrame,
    size_z: pd.DataFrame,
    industry: pd.Series,
    panel: MarketPanel,
    *,
    min_names: int = 100,
) -> FactorDay | None:
    """单日截面回归：r_i = β_industry(i) + γ·size_z_i + ε_i。

    行业哑变量 + 一个连续市值暴露；用组内去均值（Frisch-Waugh）一次算完。
    """
    universe = eligible_universe(panel, day)
    if len(universe) < min_names:
        return None
    r = returns.loc[day].reindex(universe)
    z = size_z.loc[day].reindex(universe)
    ind = industry.reindex(universe).fillna(UNKNOWN_INDUSTRY)
    valid = r.notna() & z.notna()
    if int(valid.sum()) < min_names:
        return None
    r = r[valid]
    z = z[valid]
    ind = ind[valid].astype(str)

    grouped = pd.DataFrame({"r": r, "z": z, "ind": ind}).groupby("ind", sort=False)
    mean_r = grouped["r"].transform("mean")
    mean_z = grouped["z"].transform("mean")
    r_tilde = r - mean_r
    z_tilde = z - mean_z
    denom = float(z_tilde @ z_tilde)
    gamma = float(z_tilde @ r_tilde / denom) if denom > 0 else 0.0
    industry_means = grouped[["r", "z"]].mean()
    beta = industry_means["r"] - gamma * industry_means["z"]
    residual = r - beta.reindex(ind).to_numpy() - gamma * z

    weights = pd.Series(1.0 / len(r), index=r.index)
    universe_ind_weight = weights.groupby(ind).sum()
    return FactorDay(
        beta_industry=beta.astype(float),
        gamma_size=gamma,
        residual=residual.astype(float),
        universe_return=float(r.mean()),
        universe_industry_weight=universe_ind_weight.astype(float),
        universe_size=float(weights @ z),
        n_universe=int(len(r)),
    )


def factor_panel(
    panel: MarketPanel,
    *,
    start: str | None = None,
    end: str | None = None,
    min_names: int = 100,
) -> tuple[dict[pd.Timestamp, FactorDay], pd.DataFrame]:
    """逐日因子收益与残差；返回 (按日字典, 残差矩阵)。"""
    dates = panel.dates
    if start is not None:
        dates = dates[dates >= pd.Timestamp(start)]
    if end is not None:
        dates = dates[dates <= pd.Timestamp(end)]
    industry = load_industry_map()
    size_z = cross_sectional_z(log_size_matrix(panel))
    returns = daily_returns(panel)
    days: dict[pd.Timestamp, FactorDay] = {}
    residuals = pd.DataFrame(
        np.nan, index=dates, columns=panel.symbols, dtype=np.float64
    )
    for day in dates:
        result = factor_day(day, returns, size_z, industry, panel, min_names=min_names)
        if result is None:
            continue
        days[day] = result
        residuals.loc[day, result.residual.index] = result.residual.to_numpy()
    return days, residuals


def top_symbols(
    pred_row: pd.Series,
    day: pd.Timestamp,
    panel: MarketPanel,
    k: int,
) -> list[str]:
    """按主回测同一闸门取 TopK（ST/涨跌停/停牌/次新）。"""
    mask = signal_eligible_mask(
        pred_row,
        day,
        panel.limit_up,
        panel.limit_down,
        panel.suspended,
        panel.age_days,
        panel.is_st,
    )
    scores = pred_row[mask].dropna().sort_values(ascending=False, kind="mergesort")
    return list(scores.index[:k])


def position_weights(
    trades: pd.DataFrame,
    equity: pd.DataFrame,
    panel: MarketPanel,
    *,
    start: str | None = None,
    end: str | None = None,
) -> pd.DataFrame:
    """由成交重建逐日「开盘前持仓权重」。

    买卖都在收盘执行，因此 d 日收益由 d-1 收盘后的持仓赚取。权重分母用
    d-1 日总权益（含现金），权重和 = 当日仓位比例，现金部分单列。
    """
    if trades.empty:
        return pd.DataFrame()
    frame = trades.copy()
    frame["date"] = pd.to_datetime(frame["date"])
    frame["symbol"] = frame["symbol"].astype(str)
    by_date: dict[pd.Timestamp, list[tuple[str, int]]] = {}
    for row in frame.itertuples(index=False):
        sign = 1 if str(row.side) == "buy" else -1
        by_date.setdefault(pd.Timestamp(row.date), []).append(
            (str(row.symbol), sign * int(row.shares))
        )

    dates = panel.dates
    if start is not None:
        dates = dates[dates >= pd.Timestamp(start)]
    if end is not None:
        dates = dates[dates <= pd.Timestamp(end)]
    equity = equity.copy()
    equity.index = pd.to_datetime(equity.index)
    symbols = panel.symbols
    current = pd.Series(0.0, index=symbols, dtype=np.float64)
    rows: dict[pd.Timestamp, pd.Series] = {}
    position = {day: i for i, day in enumerate(panel.dates)}
    for day in dates:
        idx = position[day]
        if idx == 0:
            continue
        prev = panel.dates[idx - 1]
        if prev in equity.index and float(equity.loc[prev, "equity"]) > 0:
            price = panel.close_qfq_ffill.loc[prev].reindex(symbols)
            value = current * price
            rows[day] = value / float(equity.loc[prev, "equity"])
        for symbol, delta in by_date.get(day, []):
            if symbol not in current.index:
                continue
            current[symbol] += delta
            if abs(current[symbol]) < 1e-9:
                current[symbol] = 0.0
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).T.reindex(index=dates, columns=symbols)


def decompose_day(
    weights: pd.Series,
    factors: FactorDay,
    industry: pd.Series,
    size_z: pd.Series,
) -> dict[str, object]:
    """完整版单日归因，需要当日标准化市值暴露。"""
    invested = float(weights.sum())
    if invested <= 0:
        return {
            "invested": 0.0,
            "industry": 0.0,
            "size": 0.0,
            "selection": 0.0,
            "cash_drag": -factors.universe_return,
            "component_sum": -factors.universe_return,
            "size_exposure": 0.0,
            "industry_hhi": float("nan"),
            "industry_detail": pd.Series(dtype=float),
        }
    held = weights[weights > 0]
    w = held / invested
    ind = industry.reindex(w.index).fillna(UNKNOWN_INDUSTRY).astype(str)
    portfolio_ind = w.groupby(ind).sum()
    # 行业差值必须在「组合行业 ∪ 基准行业」并集上算：只按组合行业取基准权重，
    # 会漏掉未持有行业的基准暴露，崩盘日残差可达 10 个百分点。
    all_ind = portfolio_ind.index.union(factors.universe_industry_weight.index)
    portfolio_ind = portfolio_ind.reindex(all_ind).fillna(0.0)
    universe_ind = factors.universe_industry_weight.reindex(all_ind).fillna(0.0)
    beta_ind = factors.beta_industry.reindex(all_ind).fillna(0.0)
    industry_effect = float((portfolio_ind - universe_ind) @ beta_ind)

    # 缺市值的持仓按截面均值（z=0）处理，与 _held_residual 的补齐口径一致；
    # 不再对有效市值权重重新归一，否则残差与基准项无法精确相加。
    z = size_z.reindex(w.index).fillna(0.0)
    size_exposure = float(w @ z)
    size_effect = (size_exposure - factors.universe_size) * factors.gamma_size

    residual = factors.residual.reindex(w.index)
    r_valid = residual.notna()
    selection_effect = (
        float(w[r_valid] @ residual[r_valid]) / float(w[r_valid].sum())
        if r_valid.any()
        else 0.0
    )

    industry_contrib = invested * industry_effect
    size_contrib = invested * size_effect
    selection_contrib = invested * selection_effect
    cash_drag = -(1.0 - invested) * factors.universe_return
    component_sum = industry_contrib + size_contrib + selection_contrib + cash_drag
    industry_detail = (portfolio_ind - universe_ind) * beta_ind * invested
    return {
        "invested": invested,
        "industry": industry_contrib,
        "size": size_contrib,
        "selection": selection_contrib,
        "cash_drag": cash_drag,
        "component_sum": component_sum,
        "size_exposure": size_exposure,
        "industry_hhi": float((portfolio_ind**2).sum()),
        "industry_detail": industry_detail,
    }


def load_equity(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path, parse_dates=["date"])
    return frame.set_index("date").sort_index()


def load_quality(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path, parse_dates=["date"])
    return frame.set_index("date").sort_index()


def quarter_label(day: pd.Timestamp) -> str:
    return f"{day.year}Q{day.quarter}"


def add_period_labels(index: pd.DatetimeIndex) -> pd.DataFrame:
    frame = pd.DataFrame(index=index)
    frame["quarter"] = [quarter_label(day) for day in index]
    frame["month"] = index.to_period("M").astype(str)
    frame["year"] = [str(day.year) for day in index]
    return frame
