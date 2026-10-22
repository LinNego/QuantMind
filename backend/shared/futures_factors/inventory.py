"""库存与仓单因子（统一做一日滞后）。

交易所库存/仓单在收盘后公布，t 日盘后拿到的数据最早只能在 t+1 收盘
决策时使用。为避免前视，本模块先按原始公布日计算，再把所有特征整体
后移 ``lag`` 个交易日（默认 1）。

库存水平取 ``log1p``；变化率用原始水平计算，避免对数后再差分的口径漂移。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = [
    "INVENTORY_FEATURES",
    "WAREHOUSE_FEATURES",
    "compute_inventory_features",
    "compute_warehouse_features",
]

INVENTORY_FEATURES: tuple[str, ...] = (
    "inventory",
    "inv_chg_1",
    "inv_chg_5",
    "inv_chg_20",
    "inv_z_20",
    "inv_pct_252",
    "inv_mom_20",
    "inv_vol_20",
)

WAREHOUSE_FEATURES: tuple[str, ...] = (
    "wh_qty",
    "wh_chg_1",
    "wh_chg_5",
    "wh_chg_20",
    "wh_z_20",
    "wh_pct_252",
)

_EPS = 1e-12


def _grouped(df: pd.DataFrame, series: pd.Series):
    return series.groupby(df["variety"], sort=False)


def _roll(df: pd.DataFrame, series: pd.Series, window: int, method: str) -> pd.Series:
    min_periods = max(2, window // 2)
    return _grouped(df, series).transform(
        lambda s: getattr(s.rolling(window, min_periods=min_periods), method)()
    )


def _pct_change(df: pd.DataFrame, level: pd.Series, periods: int) -> pd.Series:
    past = _grouped(df, level).shift(periods)
    return level / past.where(past.abs() > _EPS) - 1.0


def _lag(df: pd.DataFrame, columns: list[str], lag: int) -> pd.DataFrame:
    if lag <= 0:
        return df
    out = df.copy()
    for col in columns:
        out[col] = _grouped(out, out[col]).shift(lag)
    return out


def compute_inventory_features(df: pd.DataFrame, lag: int = 1) -> pd.DataFrame:
    columns = ["variety", "time", *INVENTORY_FEATURES]
    if df is None or df.empty:
        return pd.DataFrame(columns=columns)
    required = {"variety", "time", "inventory"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"inventory 缺少列: {', '.join(sorted(missing))}")

    out = df.copy()
    out["time"] = pd.to_datetime(out["time"]).dt.normalize()
    out["inventory_raw"] = pd.to_numeric(out["inventory"], errors="coerce")
    out = out.dropna(subset=["inventory_raw"])
    out = out.sort_values(["variety", "time"]).reset_index(drop=True)
    if out.empty:
        return pd.DataFrame(columns=columns)

    level = out["inventory_raw"]
    out["inventory"] = np.log1p(level.clip(lower=0.0))
    out["inv_chg_1"] = _pct_change(out, level, 1)
    out["inv_chg_5"] = _pct_change(out, level, 5)
    out["inv_chg_20"] = _pct_change(out, level, 20)

    mean20 = _roll(out, out["inventory"], 20, "mean")
    std20 = _roll(out, out["inventory"], 20, "std")
    out["inv_z_20"] = (out["inventory"] - mean20) / std20.where(std20 > _EPS)
    out["inv_pct_252"] = _grouped(out, out["inventory"]).transform(
        lambda s: s.rolling(252, min_periods=60).rank(pct=True)
    )
    mean20_level = _roll(out, out["inventory"], 20, "mean")
    out["inv_mom_20"] = (
        out["inventory"] / mean20_level.where(mean20_level.abs() > _EPS) - 1.0
    )
    out["inv_vol_20"] = _roll(out, out["inv_chg_1"], 20, "std")

    out = _lag(out, list(INVENTORY_FEATURES), lag)
    return out[columns].reset_index(drop=True)


def compute_warehouse_features(df: pd.DataFrame, lag: int = 1) -> pd.DataFrame:
    columns = ["variety", "time", *WAREHOUSE_FEATURES]
    if df is None or df.empty:
        return pd.DataFrame(columns=columns)
    required = {"variety", "time", "receipt_qty"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"warehouse 缺少列: {', '.join(sorted(missing))}")

    out = df.copy()
    out["time"] = pd.to_datetime(out["time"]).dt.normalize()
    out["qty_raw"] = pd.to_numeric(out["receipt_qty"], errors="coerce")
    out = out.dropna(subset=["qty_raw"])
    if out.empty:
        return pd.DataFrame(columns=columns)
    out = (
        out.groupby(["variety", "time"], as_index=False)["qty_raw"]
        .sum()
        .sort_values(["variety", "time"])
        .reset_index(drop=True)
    )

    level = out["qty_raw"]
    out["wh_qty"] = np.log1p(level.clip(lower=0.0))
    out["wh_chg_1"] = _pct_change(out, level, 1)
    out["wh_chg_5"] = _pct_change(out, level, 5)
    out["wh_chg_20"] = _pct_change(out, level, 20)

    mean20 = _roll(out, out["wh_qty"], 20, "mean")
    std20 = _roll(out, out["wh_qty"], 20, "std")
    out["wh_z_20"] = (out["wh_qty"] - mean20) / std20.where(std20 > _EPS)
    out["wh_pct_252"] = _grouped(out, out["wh_qty"]).transform(
        lambda s: s.rolling(252, min_periods=60).rank(pct=True)
    )

    out = _lag(out, list(WAREHOUSE_FEATURES), lag)
    return out[columns].reset_index(drop=True)
