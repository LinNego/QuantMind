"""会员持仓因子（交易所前 20 名会员口径）。

注意：这是**会员排名口径**，不是全市场总持仓。前 20 名多空合计通常只占
总持仓的一部分，因此 ``member_net_ratio`` 衡量的是头部会员的净头寸倾向，
不能当成全市场净多。

排名在收盘后公布，统一后移一日（``lag=1``）再进入特征。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = ["POSITION_FEATURES", "compute_position_features"]

POSITION_FEATURES: tuple[str, ...] = (
    "member_net_ratio",
    "member_net_chg_1",
    "member_net_chg_5",
    "member_net_z_20",
    "member_ls_ratio",
    "member_top5_conc",
    "member_hhi",
)

_EPS = 1e-12
_TOP_N = 5


def _aggregate(df: pd.DataFrame) -> pd.DataFrame:
    work = df.copy()
    work["side"] = work["side"].astype(str).str.lower()
    work["size"] = pd.to_numeric(work["size"], errors="coerce")
    work = work.dropna(subset=["size"])
    work = work[work["side"].isin(["long", "short"])]

    rows: list[dict[str, object]] = []
    for (variety, time), group in work.groupby(["variety", "time"], sort=True):
        long_rows = group[group["side"] == "long"]
        short_rows = group[group["side"] == "short"]
        long_size = float(long_rows["size"].sum())
        short_size = float(short_rows["size"].sum())
        total = long_size + short_size
        top5 = float(
            long_rows.nlargest(_TOP_N, "size")["size"].sum()
            + short_rows.nlargest(_TOP_N, "size")["size"].sum()
        )
        if total > _EPS:
            hhi = float(((group["size"] / total) ** 2).sum())
        else:
            hhi = np.nan
        rows.append(
            {
                "variety": variety,
                "time": time,
                "long_size": long_size,
                "short_size": short_size,
                "total_size": total,
                "top5_size": top5,
                "hhi": hhi,
            }
        )
    return pd.DataFrame.from_records(rows)


def compute_position_features(df: pd.DataFrame, lag: int = 1) -> pd.DataFrame:
    columns = ["variety", "time", *POSITION_FEATURES]
    if df is None or df.empty:
        return pd.DataFrame(columns=columns)
    required = {"variety", "time", "side", "size"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"member_positions 缺少列: {', '.join(sorted(missing))}")

    work = df.copy()
    work["time"] = pd.to_datetime(work["time"]).dt.normalize()
    daily = _aggregate(work)
    if daily.empty:
        return pd.DataFrame(columns=columns)
    daily = daily.sort_values(["variety", "time"]).reset_index(drop=True)

    total = daily["total_size"]
    long_size = daily["long_size"]
    short_size = daily["short_size"]
    daily["member_net_ratio"] = (long_size - short_size) / total.where(total > _EPS)
    daily["member_ls_ratio"] = long_size / short_size.where(short_size > _EPS)
    daily["member_top5_conc"] = daily["top5_size"] / total.where(total > _EPS)
    daily["member_hhi"] = daily["hhi"]

    grouped = daily.groupby("variety", sort=False)
    daily["member_net_chg_1"] = grouped["member_net_ratio"].diff(1)
    daily["member_net_chg_5"] = grouped["member_net_ratio"].diff(5)
    mean20 = grouped["member_net_ratio"].transform(
        lambda s: s.rolling(20, min_periods=10).mean()
    )
    std20 = grouped["member_net_ratio"].transform(
        lambda s: s.rolling(20, min_periods=10).std(ddof=0)
    )
    daily["member_net_z_20"] = (daily["member_net_ratio"] - mean20) / std20.where(
        std20 > _EPS
    )

    if lag > 0:
        for col in POSITION_FEATURES:
            daily[col] = daily.groupby("variety", sort=False)[col].shift(lag)
    return daily[columns].reset_index(drop=True)
