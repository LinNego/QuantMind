"""季节性与日历因子。

``same_month_ret_20`` 用**往年同月**的 20 日收益均值，明确排除当年数据，
避免把未来收益带进特征。第一年同月没有历史，留空。
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

from backend.shared.futures_factors.expiry import TradingCalendar

__all__ = ["SEASONALITY_FEATURES", "compute_seasonality_features"]

SEASONALITY_FEATURES: tuple[str, ...] = (
    "month_sin",
    "month_cos",
    "same_month_ret_20",
    "days_to_month_end",
    "is_quarter_end",
    "day_of_week",
)

_QUARTER_MONTHS = frozenset({3, 6, 9, 12})


def _weekdays_to_month_end(day: date) -> int:
    if day.month == 12:
        month_end = date(day.year, 12, 31)
    else:
        month_end = date(day.year, day.month + 1, 1) - timedelta(days=1)
    count = 0
    cursor = day + timedelta(days=1)
    while cursor <= month_end:
        if cursor.weekday() < 5:
            count += 1
        cursor += timedelta(days=1)
    return count


def _last_trading_day(
    day: date, calendar: TradingCalendar | None, panel_last: date | None
) -> date | None:
    if calendar is not None:
        return calendar.last_trading_day_of_month(day.year, day.month)
    return panel_last


def compute_seasonality_features(
    df: pd.DataFrame, calendar: TradingCalendar | None = None
) -> pd.DataFrame:
    columns = ["variety", "time", *SEASONALITY_FEATURES]
    if df is None or df.empty:
        return pd.DataFrame(columns=columns)
    required = {"variety", "time"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"日历因子缺少列: {', '.join(sorted(missing))}")

    out = df.copy()
    out["time"] = pd.to_datetime(out["time"]).dt.normalize()
    out["year"] = out["time"].dt.year
    out["month"] = out["time"].dt.month
    month = out["month"].astype(float)
    out["month_sin"] = np.sin(2.0 * np.pi * month / 12.0)
    out["month_cos"] = np.cos(2.0 * np.pi * month / 12.0)
    out["day_of_week"] = out["time"].dt.weekday.astype(float)

    if "ret_20d" in out.columns:
        ret = pd.to_numeric(out["ret_20d"], errors="coerce")
        yearly = (
            out.assign(_ret=ret)
            .groupby(["variety", "year", "month"], as_index=False)["_ret"]
            .mean()
            .sort_values(["variety", "month", "year"])
        )
        yearly["same_month_ret_20"] = yearly.groupby(["variety", "month"], sort=False)[
            "_ret"
        ].transform(lambda s: s.shift(1).expanding().mean())
        out = out.merge(
            yearly[["variety", "year", "month", "same_month_ret_20"]],
            on=["variety", "year", "month"],
            how="left",
        )
    else:
        out["same_month_ret_20"] = np.nan

    unique_days = pd.Index(out["time"].unique()).sort_values()
    day_values = [ts.date() for ts in unique_days]
    if calendar is not None:
        days_map = {d: calendar.days_to_month_end(d) for d in day_values}
    else:
        days_map = {d: _weekdays_to_month_end(d) for d in day_values}
    out["days_to_month_end"] = out["time"].dt.date.map(days_map).astype(float)

    if calendar is not None:
        last_map = {
            (d.year, d.month): calendar.last_trading_day_of_month(d.year, d.month)
            for d in day_values
        }
    else:
        panel_last = out.groupby(["year", "month"])["time"].max().dt.date.to_dict()
        last_map = panel_last
    last_days = pd.Series(
        [last_map.get((d.year, d.month)) for d in out["time"].dt.date],
        index=out.index,
    )
    is_quarter = out["month"].isin(_QUARTER_MONTHS)
    out["is_quarter_end"] = (is_quarter & (out["time"].dt.date == last_days)).astype(
        float
    )

    return out[columns].reset_index(drop=True)
