"""现货基差因子。

口径（与 QuantFutures ``spot_basis`` 一致，**不要翻转符号**）：

    dom_basis = 主力合约价 - 现货价
    dom_basis_rate = dom_basis / 现货价

因此 ``basis_dom > 0`` 表示期货升水（contango 的现货侧表现），
``basis_dom < 0`` 表示期货贴水。国内很多报告用“现货 - 期货”，
符号正好相反；本库统一用上面的口径并在 manifest 里注明。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = ["BASIS_FEATURES", "compute_basis_features"]

BASIS_FEATURES: tuple[str, ...] = (
    "basis_dom",
    "basis_near",
    "basis_curve",
    "basis_z_252",
    "basis_chg_1",
    "basis_chg_5",
    "basis_mom_20",
)

_EPS = 1e-12


def _grouped(df: pd.DataFrame, series: pd.Series):
    return series.groupby(df["variety"], sort=False)


def _roll(df: pd.DataFrame, series: pd.Series, window: int, method: str) -> pd.Series:
    return _grouped(df, series).transform(
        lambda s: getattr(s.rolling(window, min_periods=60), method)()
    )


def compute_basis_features(df: pd.DataFrame) -> pd.DataFrame:
    columns = ["variety", "time", *BASIS_FEATURES]
    if df is None or df.empty:
        return pd.DataFrame(columns=columns)

    required = {"variety", "time", "dom_basis_rate", "near_basis_rate"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"spot_basis 缺少列: {', '.join(sorted(missing))}")

    out = df.copy()
    out["time"] = pd.to_datetime(out["time"]).dt.normalize()
    out = out.sort_values(["variety", "time"]).reset_index(drop=True)
    out["basis_dom"] = pd.to_numeric(out["dom_basis_rate"], errors="coerce")
    out["basis_near"] = pd.to_numeric(out["near_basis_rate"], errors="coerce")

    spot = (
        pd.to_numeric(out["spot_price"], errors="coerce")
        if "spot_price" in out.columns
        else pd.Series(np.nan, index=out.index)
    )
    dom_abs = (
        pd.to_numeric(out["dom_basis"], errors="coerce")
        if "dom_basis" in out.columns
        else pd.Series(np.nan, index=out.index)
    )
    near_abs = (
        pd.to_numeric(out["near_basis"], errors="coerce")
        if "near_basis" in out.columns
        else pd.Series(np.nan, index=out.index)
    )
    out["basis_curve"] = (dom_abs - near_abs) / spot.where(spot > 0)

    mean = _roll(out, out["basis_dom"], 252, "mean")
    std = _roll(out, out["basis_dom"], 252, "std")
    out["basis_z_252"] = (out["basis_dom"] - mean) / std.where(std > _EPS)
    out["basis_chg_1"] = _grouped(out, out["basis_dom"]).diff(1)
    out["basis_chg_5"] = _grouped(out, out["basis_dom"]).diff(5)
    out["basis_mom_20"] = _grouped(out, out["basis_dom"]).diff(20)
    return out[columns].reset_index(drop=True)
