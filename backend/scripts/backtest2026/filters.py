"""涨跌停 / 停牌 / 次新 / ST 的可交易性判定。

涨跌幅规则不在这里复制：板别与 ST 折减一律调用
``backend.services.simulation.services.local_market_data.limit_pct``（唯一事实源）。
本模块只把规则**向量化**，并用 ``compute_limits`` 做逐样本校验。
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from backend.services.simulation.services.local_market_data import limit_pct

from .config import (
    MIN_LISTING_DAYS,
    PRICE_EPS_YUAN,
    ST_LIMIT_RELAXED_FROM,
)


def limit_pct_vector(symbols: pd.Index, is_st: pd.Series, asof: date) -> np.ndarray:
    """每个标的在 asof 当日的涨跌幅限制（百分点整数：5/10/20/30）。"""
    values = [
        int(
            limit_pct(str(sym), is_st=bool(is_st.get(sym, False)), trade_date=asof)
            * 100
        )
        for sym in symbols
    ]
    return np.asarray(values, dtype=np.int64)


def limit_price_cents(
    pre_close_cents: np.ndarray, pct_cents: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """由昨收（分）与涨跌幅（百分点）算涨跌停价（分）。

    与 ``compute_limits`` 的 SHSZ 分支同口径：四舍五入到分（正数 half-up）。
    北交所不在本回测股票池内。
    """
    base = np.asarray(pre_close_cents, dtype=np.float64)
    pct = np.asarray(pct_cents, dtype=np.float64)
    valid = np.isfinite(base) & (base > 0)
    safe_base = np.where(valid, base, 0.0)
    up = np.floor((safe_base * (100.0 + pct) + 50.0) / 100.0)
    down = np.floor((safe_base * (100.0 - pct) + 50.0) / 100.0)
    return np.where(valid, up, np.inf), np.where(valid, down, 0.0)


def compute_limit_matrices(
    close_raw: pd.DataFrame,
    volume: pd.DataFrame,
    is_st: pd.Series,
    eps_yuan: float = PRICE_EPS_YUAN,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """返回 (涨停, 跌停, 停牌) 三个布尔矩阵，索引为交易日、列为标的。

    - 涨停/跌停按不复权价与 ``compute_limits`` 限价比较（绝对容差 0.004 元）。
    - 停牌 := 成交量为 0、收盘价缺失或非正。
    """
    dates = close_raw.index
    pre_close = close_raw.shift(1)
    before = limit_pct_vector(
        close_raw.columns, is_st, date.fromisoformat("2026-01-05")
    )
    after = limit_pct_vector(
        close_raw.columns, is_st, date.fromisoformat(ST_LIMIT_RELAXED_FROM)
    )
    relax_at = pd.Timestamp(ST_LIMIT_RELAXED_FROM)

    up_limit = pd.DataFrame(
        np.inf, index=dates, columns=close_raw.columns, dtype=np.float64
    )
    down_limit = pd.DataFrame(
        0.0, index=dates, columns=close_raw.columns, dtype=np.float64
    )
    for day in dates:
        pct = before if day < relax_at else after
        base_cents = np.rint(pre_close.loc[day].to_numpy(dtype=np.float64) * 100.0)
        up_cents, down_cents = limit_price_cents(base_cents, pct)
        up_limit.loc[day] = up_cents / 100.0
        down_limit.loc[day] = down_cents / 100.0

    raw = close_raw.to_numpy(dtype=np.float64)
    limit_up = np.isfinite(up_limit.to_numpy()) & (
        raw >= up_limit.to_numpy() - eps_yuan
    )
    limit_down = (down_limit.to_numpy() > 0) & (raw <= down_limit.to_numpy() + eps_yuan)
    suspended = (
        (volume.fillna(0.0).to_numpy(dtype=np.float64) <= 0)
        | ~np.isfinite(raw)
        | (raw <= 0)
    )
    return (
        pd.DataFrame(limit_up, index=dates, columns=close_raw.columns),
        pd.DataFrame(limit_down, index=dates, columns=close_raw.columns),
        pd.DataFrame(suspended, index=dates, columns=close_raw.columns),
    )


def listing_age_days(
    first_seen: pd.Series,
    dates: pd.DatetimeIndex,
    symbols: pd.Index,
    full_dates: pd.DatetimeIndex | None = None,
) -> pd.DataFrame:
    """每只股票在每个交易日的上市交易日数（以预测面板首次出现日为代理）。

    ``full_dates`` 传全历史交易日历，``dates`` 传回测窗口；否则 2016 年就上市的
    股票会被错误地算成“窗口首日才上市”。
    """
    full = full_dates if full_dates is not None else dates
    position = {day: i for i, day in enumerate(full)}
    first_idx = first_seen.reindex(symbols).map(position)
    day_idx = np.asarray([position[d] for d in dates], dtype=np.float64)
    age = day_idx[:, None] - first_idx.to_numpy(dtype=np.float64)[None, :]
    return pd.DataFrame(age, index=dates, columns=symbols)


def signal_eligible_mask(
    pred_row: pd.Series,
    day: pd.Timestamp,
    limit_up: pd.DataFrame,
    limit_down: pd.DataFrame,
    suspended: pd.DataFrame,
    age_days: pd.DataFrame,
    is_st: pd.Series,
    min_listing_days: int = MIN_LISTING_DAYS,
) -> pd.Series:
    """信号日可买入池：有预测、非 ST、非停牌、非涨跌停、上市够久。"""
    symbols = pred_row.index
    mask = pred_row.notna()
    mask &= ~is_st.reindex(symbols).fillna(False).astype(bool)
    mask &= ~suspended.loc[day].reindex(symbols).fillna(True).astype(bool)
    mask &= ~limit_up.loc[day].reindex(symbols).fillna(True).astype(bool)
    mask &= ~limit_down.loc[day].reindex(symbols).fillna(True).astype(bool)
    mask &= age_days.loc[day].reindex(symbols).fillna(-1) >= min_listing_days
    return mask.astype(bool)


def entry_blocked_mask(
    symbols: pd.Index,
    day: pd.Timestamp,
    limit_up: pd.DataFrame,
    limit_down: pd.DataFrame,
    suspended: pd.DataFrame,
) -> pd.Series:
    """入场日不可成交：停牌 / 涨停（买不到）/ 跌停（按用户规则一并剔除）。"""
    blocked = suspended.loc[day].reindex(symbols).fillna(True).astype(bool)
    blocked |= limit_up.loc[day].reindex(symbols).fillna(True).astype(bool)
    blocked |= limit_down.loc[day].reindex(symbols).fillna(True).astype(bool)
    return blocked


def exit_blocked_mask(
    symbol: str,
    day: pd.Timestamp,
    limit_down: pd.DataFrame,
    suspended: pd.DataFrame,
) -> bool:
    """退出日不可卖出：停牌或跌停（涨停可以卖）。"""
    if symbol not in limit_down.columns:
        return True
    return bool(suspended.at[day, symbol] or limit_down.at[day, symbol])
