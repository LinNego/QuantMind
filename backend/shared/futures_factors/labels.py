"""训练标签：前视收益与横截面超额收益。

口径与 ``build_ml_l1_dataset`` 保持一致：

* 信号在 ``t`` 日收盘后生成，``t+1`` 日收盘执行，
  ``execution_close = close.shift(-execution_lag)``；
* ``horizon`` 日后的收盘价为 ``future_close = close.shift(-(lag + horizon))``；
* ``fwd_ret_hd = future_close / execution_close - 1``。

标签**不进入**因子数据集，单独落盘，避免特征文件被误当训练目标。
"""

from __future__ import annotations

import pandas as pd

from backend.shared.futures_factors.panel import variety_of_main_symbol

__all__ = ["DEFAULT_HORIZONS", "label_columns", "compute_forward_labels"]

DEFAULT_HORIZONS: tuple[int, ...] = (1, 3, 5, 10, 20)

_REQUIRED = ("close",)


def label_columns(horizons: tuple[int, ...] = DEFAULT_HORIZONS) -> tuple[str, ...]:
    cols: list[str] = []
    for horizon in horizons:
        cols.append(f"fwd_ret_{horizon}d")
        cols.append(f"fwd_xs_{horizon}d")
    return tuple(cols)


def _resolve_keys(df: pd.DataFrame) -> tuple[pd.DataFrame, str, str]:
    work = df.copy()
    if "symbol" in work.columns:
        symbol_col = "symbol"
        if "variety" not in work.columns:
            work["variety"] = work["symbol"].map(variety_of_main_symbol)
    elif "variety" in work.columns:
        symbol_col = "variety"
        work["symbol"] = work["variety"]
    else:
        raise ValueError("标签需要 symbol 或 variety 列")
    return work, symbol_col, "variety"


def compute_forward_labels(
    df: pd.DataFrame,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
    execution_lag: int = 1,
) -> pd.DataFrame:
    """计算前视收益标签（按 symbol 分组，尾部 horizon+lag 行为空）。"""
    if df is None or df.empty:
        return pd.DataFrame(
            columns=["symbol", "variety", "date", "time", *label_columns(horizons)]
        )

    missing = [col for col in _REQUIRED if col not in df.columns]
    if missing:
        raise ValueError(f"标签缺少列: {', '.join(missing)}")
    if "time" not in df.columns:
        raise ValueError("标签缺少 time 列")
    if execution_lag < 0:
        raise ValueError("execution_lag 不能为负")
    if any(horizon < 1 for horizon in horizons):
        raise ValueError("horizon 必须为正整数")

    work, symbol_col, _ = _resolve_keys(df)
    work["time"] = pd.to_datetime(work["time"]).dt.normalize()
    work["date"] = work["time"]
    work = work.sort_values([symbol_col, "time"]).reset_index(drop=True)
    work["close"] = pd.to_numeric(work["close"], errors="coerce")

    grouped = work.groupby(symbol_col, sort=False)["close"]
    execution = grouped.shift(-execution_lag)
    for horizon in horizons:
        future = grouped.shift(-(execution_lag + horizon))
        work[f"fwd_ret_{horizon}d"] = future / execution.where(execution > 0) - 1.0

    for horizon in horizons:
        raw = work[f"fwd_ret_{horizon}d"]
        mean = raw.groupby(work["time"], sort=False).transform("mean")
        work[f"fwd_xs_{horizon}d"] = raw - mean

    return work[
        ["symbol", "variety", "date", "time", *label_columns(horizons)]
    ].reset_index(drop=True)
