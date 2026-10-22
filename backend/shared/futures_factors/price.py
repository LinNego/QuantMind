"""价格、波动率与流动性因子。

输入一行 = 一个品种一个交易日，至少含 ``variety/time/open/high/low/close/
volume/amount``。所有滚动窗口都在品种内计算（``groupby(variety)``），
不会跨品种串行。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from backend.shared.futures_universe import symbol_meta

__all__ = [
    "LIQUIDITY_FEATURES",
    "PRICE_FEATURES",
    "VOLATILITY_FEATURES",
    "compute_liquidity_features",
    "compute_price_features",
    "compute_volatility_features",
]

PRICE_FEATURES: tuple[str, ...] = (
    "ret_1d",
    "ret_3d",
    "ret_5d",
    "ret_10d",
    "ret_20d",
    "ret_60d",
    "ma_gap_5",
    "ma_gap_20",
    "ma_gap_60",
    "rsi_6",
    "rsi_14",
    "macd_hist",
    "bb_pos_20",
    "brk_20",
    "clv",
    "gap_open",
    "intraday_ret",
    "range_pct",
    "up_days",
)

VOLATILITY_FEATURES: tuple[str, ...] = (
    "rv_5",
    "rv_20",
    "rv_60",
    "park_20",
    "gk_20",
    "atr_14",
    "downvol_20",
    "mdd_20",
    "skew_20",
    "vol_pct_252",
    "rv_ratio_5_20",
)

LIQUIDITY_FEATURES: tuple[str, ...] = (
    "log_notional",
    "notional_z_20",
    "notional_ratio_5_20",
    "vol_ratio_5_20",
    "amihud_20",
)

_AMIHUD_SCALE = 1e8
_EPS = 1e-12


def _grouped(df: pd.DataFrame, series: pd.Series):
    return series.groupby(df["variety"], sort=False)


def _roll(
    df: pd.DataFrame,
    series: pd.Series,
    window: int,
    method: str = "mean",
    **kwargs,
) -> pd.Series:
    return _grouped(df, series).transform(
        lambda s: getattr(s.rolling(window, min_periods=window), method)(**kwargs)
    )


def _ema(df: pd.DataFrame, series: pd.Series, span: int) -> pd.Series:
    return _grouped(df, series).transform(
        lambda s: s.ewm(span=span, adjust=False).mean()
    )


def _shift(df: pd.DataFrame, series: pd.Series, periods: int) -> pd.Series:
    return _grouped(df, series).shift(periods)


def _rsi(df: pd.DataFrame, close: pd.Series, window: int) -> pd.Series:
    delta = _grouped(df, close).diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = _roll(df, gain, window)
    avg_loss = _roll(df, loss, window)
    rs = avg_gain / avg_loss.where(avg_loss > _EPS)
    rsi = 100.0 - 100.0 / (1.0 + rs)
    rsi = rsi.mask((avg_loss <= _EPS) & (avg_gain > _EPS), 100.0)
    return rsi.mask((avg_loss <= _EPS) & (avg_gain <= _EPS), 50.0)


def compute_price_features(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    close = pd.to_numeric(out["close"], errors="coerce")
    high = pd.to_numeric(out["high"], errors="coerce")
    low = pd.to_numeric(out["low"], errors="coerce")
    open_ = pd.to_numeric(out["open"], errors="coerce")

    for window in (1, 3, 5, 10, 20, 60):
        out[f"ret_{window}d"] = close / _shift(out, close, window) - 1.0

    for window in (5, 20, 60):
        mean = _roll(out, close, window)
        out[f"ma_gap_{window}"] = close / mean - 1.0

    out["rsi_6"] = _rsi(out, close, 6)
    out["rsi_14"] = _rsi(out, close, 14)

    dif = _ema(out, close, 12) - _ema(out, close, 26)
    dea = _ema(out, dif, 9)
    out["macd_hist"] = (dif - dea) / close

    ma20 = _roll(out, close, 20)
    std20 = _roll(out, close, 20, method="std", ddof=0)
    out["bb_pos_20"] = (close - ma20) / (2.0 * std20.where(std20 > _EPS))

    prev_high_20 = _roll(out, _shift(out, high, 1), 20, method="max")
    out["brk_20"] = close / prev_high_20 - 1.0

    span = (high - low).where(high > low)
    out["clv"] = ((close - low) - (high - close)) / span
    out["clv"] = out["clv"].fillna(0.0).clip(-1.0, 1.0)

    prev_close = _shift(out, close, 1)
    out["gap_open"] = open_ / prev_close - 1.0
    out["intraday_ret"] = close / open_.where(open_ > 0) - 1.0
    out["range_pct"] = (high - low) / prev_close
    up = (out["ret_1d"] > 0).astype(float).where(out["ret_1d"].notna())
    out["up_days"] = _roll(out, up, 10)
    return out


def compute_volatility_features(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    close = pd.to_numeric(out["close"], errors="coerce")
    high = pd.to_numeric(out["high"], errors="coerce")
    low = pd.to_numeric(out["low"], errors="coerce")
    open_ = pd.to_numeric(out["open"], errors="coerce")

    ret = (
        out["ret_1d"]
        if "ret_1d" in out.columns
        else close / _shift(out, close, 1) - 1.0
    )
    ret = pd.to_numeric(ret, errors="coerce")

    out["rv_5"] = _roll(out, ret, 5, method="std", ddof=0)
    out["rv_20"] = _roll(out, ret, 20, method="std", ddof=0)
    out["rv_60"] = _roll(out, ret, 60, method="std", ddof=0)

    hl = np.log(high / low.where(low > 0))
    out["park_20"] = np.sqrt(_roll(out, hl.pow(2), 20) / (4.0 * np.log(2.0)))

    co = np.log(close / open_.where(open_ > 0))
    gk_var = 0.5 * _roll(out, hl.pow(2), 20) - (2.0 * np.log(2.0) - 1.0) * _roll(
        out, co.pow(2), 20
    )
    out["gk_20"] = np.sqrt(gk_var.clip(lower=0.0))

    prev_close = _shift(out, close, 1)
    true_range = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    out["atr_14"] = _roll(out, true_range, 14) / close

    downside = ret.clip(upper=0.0)
    out["downvol_20"] = np.sqrt(_roll(out, downside.pow(2), 20))
    out["mdd_20"] = close / _roll(out, close, 20, method="max") - 1.0
    out["skew_20"] = _roll(out, ret, 20, method="skew")

    out["vol_pct_252"] = _grouped(out, out["rv_20"]).transform(
        lambda s: s.rolling(252, min_periods=60).rank(pct=True)
    )
    out["rv_ratio_5_20"] = out["rv_5"] / out["rv_20"].where(out["rv_20"] > _EPS)
    return out


def _contract_multiplier(variety: str) -> float:
    value = symbol_meta(f"{variety}0.CN").get("multiplier")
    return float(value) if value else 1.0


def compute_liquidity_features(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    close = pd.to_numeric(out["close"], errors="coerce")
    volume = pd.to_numeric(out["volume"], errors="coerce")
    amount = pd.to_numeric(out["amount"], errors="coerce")
    ret = (
        out["ret_1d"]
        if "ret_1d" in out.columns
        else close / _shift(out, close, 1) - 1.0
    )
    ret = pd.to_numeric(ret, errors="coerce")

    multiplier = out["variety"].map(_contract_multiplier).astype(float)
    out["notional"] = amount * multiplier
    out["log_notional"] = np.log1p(out["notional"].clip(lower=0.0))

    mean20 = _roll(out, out["log_notional"], 20)
    std20 = _roll(out, out["log_notional"], 20, method="std", ddof=0)
    out["notional_z_20"] = (out["log_notional"] - mean20) / std20.where(std20 > _EPS)
    out["notional_ratio_5_20"] = _roll(out, out["log_notional"], 5) / mean20.where(
        mean20.abs() > _EPS
    )

    vol_mean5 = _roll(out, volume, 5)
    vol_mean20 = _roll(out, volume, 20)
    out["vol_ratio_5_20"] = vol_mean5 / vol_mean20.where(vol_mean20 > _EPS)

    illiq = (ret.abs() / out["notional"].where(out["notional"] > 0)).replace(
        [np.inf, -np.inf], np.nan
    )
    out["amihud_20"] = _roll(out, illiq, 20) * _AMIHUD_SCALE
    return out
