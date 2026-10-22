"""横截面与板块因子。

* ``xs_*``：同一交易日、全部品种内的分位（0~1）。只能用于同日截面模型，
  不能跨日比较；
* ``sec_*``：品种相对所属板块的强弱；
* ``beta_sec_60``：品种日收益对板块等权日收益的 60 日滚动 beta；
* ``resid_mom_20``：20 日收益中扣掉板块 beta 暴露后的残差动量。

板块来自 ``futures_universe.symbol_meta``（9 大板块），不在数据文件里。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from backend.shared.futures_universe import symbol_meta

__all__ = [
    "CROSS_SECTION_FEATURES",
    "SECTOR_FEATURES",
    "compute_cross_section_features",
]

CROSS_SECTION_FEATURES: tuple[str, ...] = (
    "xs_rank_ret_5",
    "xs_rank_ret_20",
    "xs_rank_carry",
    "xs_rank_basis",
    "xs_rank_oi_chg_5",
    "xs_rank_vol_20",
)

SECTOR_FEATURES: tuple[str, ...] = (
    "sec_rel_ret_5",
    "sec_rel_ret_20",
    "sec_mom_20",
    "beta_sec_60",
    "resid_mom_20",
)

_XS_SOURCES: dict[str, str] = {
    "xs_rank_ret_5": "ret_5d",
    "xs_rank_ret_20": "ret_20d",
    "xs_rank_carry": "carry",
    "xs_rank_basis": "basis_dom",
    "xs_rank_oi_chg_5": "oi_chg_5",
    "xs_rank_vol_20": "rv_20",
}

_EPS = 1e-12
_BETA_WINDOW = 60
_BETA_MIN = 40
_SECTOR_WINDOW = 20


def _sector_of(variety: str) -> str:
    return symbol_meta(f"{variety}0.CN").get("sector") or "other"


def _column(df: pd.DataFrame, name: str) -> pd.Series:
    if name in df.columns:
        return pd.to_numeric(df[name], errors="coerce")
    return pd.Series(np.nan, index=df.index)


def compute_cross_section_features(df: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "variety",
        "time",
        *CROSS_SECTION_FEATURES,
        *SECTOR_FEATURES,
    ]
    if df is None or df.empty:
        return pd.DataFrame(columns=columns)
    required = {"variety", "time", "ret_1d"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"横截面因子缺少列: {', '.join(sorted(missing))}")

    out = df.copy()
    out["time"] = pd.to_datetime(out["time"]).dt.normalize()
    if "sector" not in out.columns:
        out["sector"] = out["variety"].map(_sector_of)
    else:
        out["sector"] = out["sector"].fillna(out["variety"].map(_sector_of))
    out = out.sort_values(["variety", "time"]).reset_index(drop=True)

    out["ret_1d"] = _column(out, "ret_1d")
    out["ret_5d"] = _column(out, "ret_5d")
    out["ret_20d"] = _column(out, "ret_20d")

    for target, source in _XS_SOURCES.items():
        values = _column(out, source)
        out[target] = values.groupby(out["time"], sort=False).rank(pct=True)

    sector_daily = (
        out.groupby(["sector", "time"], as_index=False)["ret_1d"]
        .mean()
        .rename(columns={"ret_1d": "sec_ret_1d"})
        .sort_values(["sector", "time"])
    )
    sector_daily["sec_ret_20d"] = sector_daily.groupby("sector", sort=False)[
        "sec_ret_1d"
    ].transform(
        lambda s: s.rolling(_SECTOR_WINDOW, min_periods=_SECTOR_WINDOW).apply(
            lambda values: float(np.prod(1.0 + values) - 1.0), raw=True
        )
    )
    out = out.merge(sector_daily, on=["sector", "time"], how="left")

    out["sec_rel_ret_5"] = out["ret_5d"] - out.groupby(["sector", "time"], sort=False)[
        "ret_5d"
    ].transform("mean")
    out["sec_rel_ret_20"] = out["ret_20d"] - out.groupby(
        ["sector", "time"], sort=False
    )["ret_20d"].transform("mean")
    out["sec_mom_20"] = out.groupby(["sector", "time"], sort=False)[
        "ret_20d"
    ].transform("mean")

    out["_xy"] = out["ret_1d"] * out["sec_ret_1d"]
    out["_x2"] = out["sec_ret_1d"].pow(2)
    grouped = out.groupby("variety", sort=False)

    def _rolling_mean(col: str) -> pd.Series:
        return grouped[col].transform(
            lambda s: s.rolling(_BETA_WINDOW, min_periods=_BETA_MIN).mean()
        )

    mean_x = _rolling_mean("sec_ret_1d")
    mean_y = _rolling_mean("ret_1d")
    mean_xy = _rolling_mean("_xy")
    mean_x2 = _rolling_mean("_x2")
    variance = mean_x2 - mean_x.pow(2)
    out["beta_sec_60"] = (mean_xy - mean_x * mean_y) / variance.where(variance > _EPS)
    out["resid_mom_20"] = out["ret_20d"] - out["beta_sec_60"] * out["sec_ret_20d"]
    return out[columns].reset_index(drop=True)
