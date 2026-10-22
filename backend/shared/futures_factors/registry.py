"""因子清单与版本号 —— 因子列名的唯一出处。

因子分 12 族，共 111 列。命名约定：

* ``meta_*``：可得性/口径标记，取值 0/1，供模型区分缺失原因，不当作 alpha；
* ``xs_*``：同日横截面分位（0~1），只能用于同日截面模型；
* 其余为品种时序因子，单位尽量无量纲（收益率、比率、z-score）。

改动任何因子口径都必须提升 ``FEATURE_VERSION``；``feature_hash`` 写进
数据集 manifest，训练侧据此判断是否需要重跑特征。
"""

from __future__ import annotations

import hashlib

FEATURE_VERSION = "futures_institutional_v1"

ID_COLS: tuple[str, ...] = ("symbol", "date")
OHLCV_COLS: tuple[str, ...] = (
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
)

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

OPEN_INTEREST_FEATURES: tuple[str, ...] = (
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

BASIS_FEATURES: tuple[str, ...] = (
    "basis_dom",
    "basis_near",
    "basis_curve",
    "basis_z_252",
    "basis_chg_1",
    "basis_chg_5",
    "basis_mom_20",
)

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

POSITION_FEATURES: tuple[str, ...] = (
    "member_net_ratio",
    "member_net_chg_1",
    "member_net_chg_5",
    "member_net_z_20",
    "member_ls_ratio",
    "member_top5_conc",
    "member_hhi",
)

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

SEASONALITY_FEATURES: tuple[str, ...] = (
    "month_sin",
    "month_cos",
    "same_month_ret_20",
    "days_to_month_end",
    "is_quarter_end",
    "day_of_week",
)

FLAG_COLS: tuple[str, ...] = (
    "meta_tradable",
    "meta_is_financial",
    "meta_has_oi",
    "meta_has_term",
    "meta_has_basis",
    "meta_has_inventory",
    "meta_has_warehouse",
    "meta_has_position",
    "meta_roll_adjusted",
)

# 别名：早期设计稿里把可得性标记叫 META_COLS
META_COLS = FLAG_COLS

FEATURE_FAMILIES: dict[str, tuple[str, ...]] = {
    "price": PRICE_FEATURES,
    "volatility": VOLATILITY_FEATURES,
    "liquidity": LIQUIDITY_FEATURES,
    "open_interest": OPEN_INTEREST_FEATURES,
    "term": TERM_FEATURES,
    "basis": BASIS_FEATURES,
    "inventory": INVENTORY_FEATURES,
    "warehouse": WAREHOUSE_FEATURES,
    "position": POSITION_FEATURES,
    "cross_section": CROSS_SECTION_FEATURES,
    "sector": SECTOR_FEATURES,
    "seasonality": SEASONALITY_FEATURES,
    "meta": FLAG_COLS,
}

FEATURE_COLS: tuple[str, ...] = tuple(
    col for cols in FEATURE_FAMILIES.values() for col in cols
)

FAMILY_OF: dict[str, str] = {
    col: family for family, cols in FEATURE_FAMILIES.items() for col in cols
}


def feature_hash() -> str:
    """因子口径指纹（版本 + 列顺序），写入 manifest。"""
    payload = FEATURE_VERSION + "|" + "|".join(FEATURE_COLS)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def family_columns(family: str) -> tuple[str, ...]:
    try:
        return FEATURE_FAMILIES[family]
    except KeyError as exc:
        raise ValueError(f"未知因子族: {family}") from exc
