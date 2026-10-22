"""期限结构、持仓量与价仓组合因子。

只用逐合约日线（``contracts_daily``），2022-09 起可用。核心量与方向约定：

* ``carry``：近远月价差年化，**正值 = backwardation（近月贵）**；
* ``term_slope``：对 ln(价格) ~ 到期天数 做持仓量加权回归的年化斜率，
  **正值 = contango**（与 carry 符号相反，两者互为印证）；
* ``poi_quad``：价仓组合四象限 —— 3=价涨仓增、2=价涨仓减、
  1=价跌仓增、0=价跌仓减。

主力合约 = 当日持仓量最大；并列时取更近到期。近月 = 有成交且持仓的
最近到期合约；若当日只有一个月合约有成交，则退回持仓合约里最近到期。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = [
    "OI_FEATURES",
    "TERM_FEATURES",
    "compute_term_features",
]

OI_FEATURES: tuple[str, ...] = (
    "oi_total",
    "oi_chg_1",
    "oi_chg_5",
    "oi_chg_20",
    "oi_z_20",
    "oi_vol_ratio",
    "poi_quad",
    "front_oi_share",
    "top3_oi_share",
    "n_contracts",
    "dom_oi_share",
)

TERM_FEATURES: tuple[str, ...] = (
    "carry",
    "carry_3m",
    "term_slope",
    "term_curv",
    "near_far_spread",
    "carry_z_252",
    "carry_chg_5",
    "dte_front",
    "dte_dom",
    "dom_month",
    "roll_flag",
)

_CURVE_POINTS = 6
_CARRY_DAYS = 365.0
_EPS = 1e-12
_DAYS_3M = 90.0


def _empty_snapshot() -> dict[str, float]:
    return {
        "total_oi": np.nan,
        "total_volume": np.nan,
        "dom_symbol": None,
        "dom_oi": np.nan,
        "dom_close": np.nan,
        "dom_dte": np.nan,
        "dom_month": np.nan,
        "front_oi": np.nan,
        "top3_oi": np.nan,
        "n_contracts": 0.0,
        "carry": np.nan,
        "carry_3m": np.nan,
        "term_slope": np.nan,
        "term_curv": np.nan,
        "near_far_spread": np.nan,
        "dte_front": np.nan,
    }


def _weighted_slope(curve: pd.DataFrame) -> float:
    if len(curve) < 2:
        return np.nan
    x = curve["dte"].to_numpy(dtype=float)
    y = np.log(curve["close"].to_numpy(dtype=float))
    w = curve["open_interest"].to_numpy(dtype=float)
    sw = w.sum()
    if sw <= _EPS:
        return np.nan
    swx = (w * x).sum()
    swy = (w * y).sum()
    swxx = (w * x * x).sum()
    swxy = (w * x * y).sum()
    denominator = sw * swxx - swx * swx
    if abs(denominator) <= _EPS:
        return np.nan
    beta = (sw * swxy - swx * swy) / denominator
    return float(beta * _CARRY_DAYS)


def _snapshot(group: pd.DataFrame) -> dict[str, float]:
    g = group[(group["open_interest"] > 0) & (group["dte"] >= 0)]
    if g.empty:
        return _empty_snapshot()

    ordered = g.sort_values(["open_interest", "dte"], ascending=[False, True])
    dominant = ordered.iloc[0]
    total_oi = float(g["open_interest"].sum())
    total_volume = float(g["volume"].fillna(0).sum())
    top3_oi = float(g["open_interest"].nlargest(3).sum())

    liquid = g[g["volume"].fillna(0) > 0]
    pool = liquid if len(liquid) >= 2 else g
    pool = pool.sort_values("dte")
    front = pool.iloc[0]
    after_front = pool[pool["dte"] > front["dte"]]

    near_far_spread = np.nan
    carry = np.nan
    if not after_front.empty:
        second = after_front.iloc[0]
        gap = float(second["dte"] - front["dte"])
        if gap > 0 and front["close"] > 0:
            near_far_spread = float((front["close"] - second["close"]) / front["close"])
            carry = near_far_spread * _CARRY_DAYS / gap

    carry_3m = np.nan
    beyond = g[g["dte"] > dominant["dte"]]
    if not beyond.empty:
        target = dominant["dte"] + _DAYS_3M
        far3 = beyond.iloc[(beyond["dte"] - target).abs().argmin()]
        gap3 = float(far3["dte"] - dominant["dte"])
        if gap3 > 0 and dominant["close"] > 0:
            carry_3m = float(
                (dominant["close"] - far3["close"])
                / dominant["close"]
                * _CARRY_DAYS
                / gap3
            )

    curve = g.sort_values("dte").drop_duplicates("dte").head(_CURVE_POINTS)
    term_slope = _weighted_slope(curve)

    term_curv = np.nan
    three = curve.head(3)
    if len(three) == 3:
        near, mid, far = three.iloc[0], three.iloc[1], three.iloc[2]
        if mid["close"] > 0:
            term_curv = float(
                (far["close"] + near["close"] - 2.0 * mid["close"]) / mid["close"]
            )

    return {
        "total_oi": total_oi,
        "total_volume": total_volume,
        "dom_symbol": dominant["symbol"],
        "dom_oi": float(dominant["open_interest"]),
        "dom_close": float(dominant["close"]),
        "dom_dte": float(dominant["dte"]),
        "dom_month": float(dominant["contract_month"]),
        "front_oi": float(front["open_interest"]),
        "top3_oi": top3_oi,
        "n_contracts": float(len(g)),
        "carry": carry,
        "carry_3m": carry_3m,
        "term_slope": term_slope,
        "term_curv": term_curv,
        "near_far_spread": near_far_spread,
        "dte_front": float(front["dte"]),
    }


def _prepare_contracts(contracts: pd.DataFrame) -> pd.DataFrame:
    required = {"variety", "time", "symbol", "close", "open_interest"}
    missing = required - set(contracts.columns)
    if missing:
        raise ValueError(f"contracts 缺少列: {', '.join(sorted(missing))}")
    work = contracts.copy()
    work["time"] = pd.to_datetime(work["time"]).dt.normalize()
    if "last_trade_date" not in work.columns:
        raise ValueError("contracts 缺少列: last_trade_date")
    work["last_trade_date"] = pd.to_datetime(work["last_trade_date"], errors="coerce")
    work = work.dropna(subset=["last_trade_date"])
    work["dte"] = (work["last_trade_date"] - work["time"]).dt.days.astype(float)
    if "volume" not in work.columns:
        work["volume"] = 0.0
    work["volume"] = pd.to_numeric(work["volume"], errors="coerce").fillna(0.0)
    work["close"] = pd.to_numeric(work["close"], errors="coerce")
    work["open_interest"] = pd.to_numeric(work["open_interest"], errors="coerce")
    if "contract_month" not in work.columns:
        work["contract_month"] = np.nan
    return work


def _add_time_series(daily: pd.DataFrame, main: pd.DataFrame | None) -> pd.DataFrame:
    daily = daily.sort_values(["variety", "time"]).reset_index(drop=True)
    grouped = daily.groupby("variety", sort=False)

    daily["oi_total"] = daily["total_oi"]
    prev_oi = grouped["total_oi"].shift(1)
    daily["oi_chg_1"] = daily["total_oi"] / prev_oi.where(prev_oi > 0) - 1.0
    for window in (5, 20):
        past = grouped["total_oi"].shift(window)
        daily[f"oi_chg_{window}"] = daily["total_oi"] / past.where(past > 0) - 1.0

    mean20 = grouped["total_oi"].transform(
        lambda s: s.rolling(20, min_periods=20).mean()
    )
    std20 = grouped["total_oi"].transform(
        lambda s: s.rolling(20, min_periods=20).std(ddof=0)
    )
    daily["oi_z_20"] = (daily["total_oi"] - mean20) / std20.where(std20 > _EPS)
    daily["oi_vol_ratio"] = daily["total_oi"] / daily["total_volume"].where(
        daily["total_volume"] > 0
    )

    daily["front_oi_share"] = daily["front_oi"] / daily["total_oi"].where(
        daily["total_oi"] > 0
    )
    daily["top3_oi_share"] = daily["top3_oi"] / daily["total_oi"].where(
        daily["total_oi"] > 0
    )
    daily["dom_oi_share"] = daily["dom_oi"] / daily["total_oi"].where(
        daily["total_oi"] > 0
    )
    daily["dte_dom"] = daily["dom_dte"]

    prev_dom = grouped["dom_symbol"].shift(1)
    daily["roll_flag"] = ((daily["dom_symbol"] != prev_dom) & prev_dom.notna()).astype(
        float
    )

    carry_mean = grouped["carry"].transform(
        lambda s: s.rolling(252, min_periods=60).mean()
    )
    carry_std = grouped["carry"].transform(
        lambda s: s.rolling(252, min_periods=60).std(ddof=0)
    )
    daily["carry_z_252"] = (daily["carry"] - carry_mean) / carry_std.where(
        carry_std > _EPS
    )
    daily["carry_chg_5"] = daily["carry"] - grouped["carry"].shift(5)

    daily["poi_quad"] = np.nan
    if main is not None and not main.empty and "ret_1d" in main.columns:
        ret = main[["variety", "time", "ret_1d"]].copy()
        ret["time"] = pd.to_datetime(ret["time"]).dt.normalize()
        daily = daily.merge(ret, on=["variety", "time"], how="left")
        price_sign = np.sign(daily["ret_1d"])
        oi_sign = np.sign(daily["oi_chg_1"])
        quad = 2.0 * (price_sign > 0).astype(float) + (oi_sign > 0).astype(float)
        quad = quad.where((price_sign != 0) & (oi_sign != 0))
        daily["poi_quad"] = quad
    return daily


def compute_term_features(
    contracts: pd.DataFrame, main: pd.DataFrame | None = None
) -> pd.DataFrame:
    """逐品种逐日计算期限结构与持仓因子。

    返回列：``variety``、``time`` + ``OI_FEATURES`` + ``TERM_FEATURES``。
    """
    if contracts is None or contracts.empty:
        return pd.DataFrame(columns=["variety", "time", *OI_FEATURES, *TERM_FEATURES])

    work = _prepare_contracts(contracts)
    records: list[dict[str, object]] = []
    for (variety, time), group in work.groupby(["variety", "time"], sort=True):
        snapshot = _snapshot(group)
        snapshot["variety"] = variety
        snapshot["time"] = time
        records.append(snapshot)

    daily = pd.DataFrame.from_records(records)
    daily = _add_time_series(daily, main)
    keep = ["variety", "time", *OI_FEATURES, *TERM_FEATURES]
    return daily[keep].reset_index(drop=True)
