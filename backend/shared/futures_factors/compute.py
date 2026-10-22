"""把 13 族因子拼成训练用宽表。

数据流：

1. 主力连续按主力合约自身收益链复权（2022-09 前用拼接乘数）；
2. 价格/波动率/流动性在复权序列上计算；
3. 期限结构、基差、库存、仓单、会员持仓按 ``(variety, time)`` 左连接；
4. 横截面与板块因子最后算（它们需要全品种同日数据）；
5. 补 ``meta_*`` 可得性标记，输出固定列顺序。

所有辅助数据都只做左连接，缺失记 NaN 并把对应 ``meta_has_*`` 置 0，
让模型能区分“因子为 0”和“当天没有这类数据”。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from backend.shared.futures_factors.basis import compute_basis_features
from backend.shared.futures_factors.cross_section import (
    compute_cross_section_features,
)
from backend.shared.futures_factors.inventory import (
    compute_inventory_features,
    compute_warehouse_features,
)
from backend.shared.futures_factors.panel import (
    attach_last_trade_dates,
    build_roll_adjusted_prices,
    calendar_from_main,
    normalize_contract_panel,
    normalize_main_continuous,
)
from backend.shared.futures_factors.positioning import compute_position_features
from backend.shared.futures_factors.price import (
    compute_liquidity_features,
    compute_price_features,
    compute_volatility_features,
)
from backend.shared.futures_factors.registry import FEATURE_COLS, OHLCV_COLS
from backend.shared.futures_factors.seasonality import (
    compute_seasonality_features,
)
from backend.shared.futures_factors.term import compute_term_features
from backend.shared.futures_universe import symbol_meta

__all__ = ["PANEL_COLUMNS", "compute_factor_panel"]

PANEL_COLUMNS: tuple[str, ...] = (
    "symbol",
    "date",
    "time",
    "dt",
    *OHLCV_COLS,
    *FEATURE_COLS,
)

_KEYS = ["variety", "time"]


def _sector_of(variety: str) -> str:
    return symbol_meta(f"{variety}0.CN").get("sector") or "other"


def _merge_aux(base: pd.DataFrame, aux: pd.DataFrame | None) -> pd.DataFrame:
    """左连接辅助因子；右侧与基表重名的列以基表为准。"""
    if aux is None or aux.empty:
        return base
    right = aux.copy()
    overlap = [col for col in right.columns if col in base.columns and col not in _KEYS]
    if overlap:
        right = right.drop(columns=overlap)
    return base.merge(right, on=_KEYS, how="left", validate="m:1")


def compute_factor_panel(
    main: pd.DataFrame,
    contracts: pd.DataFrame | None = None,
    spot_basis: pd.DataFrame | None = None,
    inventory: pd.DataFrame | None = None,
    warehouse: pd.DataFrame | None = None,
    member_positions: pd.DataFrame | None = None,
    calendar=None,
    expiry_overrides: dict[tuple[str, int, int], object] | None = None,
) -> pd.DataFrame:
    """计算完整因子面板。

    参数都是 ``panel.load_*`` 的返回格式；辅助数据可传 None / 空表。
    """
    prices = normalize_main_continuous(main)
    if calendar is None:
        calendar = calendar_from_main(prices)

    if contracts is not None and not contracts.empty:
        work_contracts = contracts
        if "contract_year" not in work_contracts.columns:
            work_contracts = normalize_contract_panel(work_contracts)
        work_contracts = attach_last_trade_dates(
            work_contracts, calendar, expiry_overrides
        )
    else:
        work_contracts = None

    out = build_roll_adjusted_prices(prices, work_contracts)
    out = compute_price_features(out)
    out = compute_volatility_features(out)
    out = compute_liquidity_features(out)

    term = compute_term_features(work_contracts, main=out)
    if not term.empty:
        # 复权模块已经给出与主力合约切换一致的 roll_flag，保留它
        term = term.drop(columns=["roll_flag"], errors="ignore")
    out = _merge_aux(out, term)
    out = _merge_aux(out, compute_basis_features(spot_basis))
    out = _merge_aux(out, compute_inventory_features(inventory))
    out = _merge_aux(out, compute_warehouse_features(warehouse))
    out = _merge_aux(out, compute_position_features(member_positions))

    out["sector"] = out["variety"].map(_sector_of)
    out = _merge_aux(out, compute_cross_section_features(out))
    out = _merge_aux(out, compute_seasonality_features(out, calendar=calendar))

    out["date"] = out["time"]
    out["dt"] = out["time"].dt.strftime("%Y%m%d")

    volume = pd.to_numeric(out["volume"], errors="coerce")
    close = pd.to_numeric(out["close"], errors="coerce")
    out["meta_tradable"] = ((volume > 0) & close.notna()).astype(float)
    out["meta_is_financial"] = (out["sector"] == "financial").astype(float)
    for flag, source in (
        ("meta_has_oi", "oi_total"),
        ("meta_has_term", "carry"),
        ("meta_has_basis", "basis_dom"),
        ("meta_has_inventory", "inventory"),
        ("meta_has_warehouse", "wh_qty"),
        ("meta_has_position", "member_net_ratio"),
    ):
        if source in out.columns:
            values = pd.to_numeric(out[source], errors="coerce")
            out[flag] = values.notna().astype(float)
        else:
            out[flag] = 0.0

    for col in FEATURE_COLS:
        if col not in out.columns:
            out[col] = np.nan
        out[col] = pd.to_numeric(out[col], errors="coerce").astype(float)

    out = out.sort_values(["symbol", "date"]).reset_index(drop=True)
    return out[list(PANEL_COLUMNS)]
