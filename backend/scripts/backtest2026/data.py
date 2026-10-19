"""行情 / 预测 / ST / 交易日历的面板装载。

价格一律直读 QuantDB 的 ``dt=YYYYMMDD`` 分区：
- ``daily_unadjusted``：不复权价，用于涨跌停判定；
- ``daily_forward``：前复权价，用于组合收益（含分红再投资）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.dataset as ds_mod
import pyarrow.parquet as pq

from .config import DATA_ROOT, PRICE_END, PRICE_START, ST_ASOF_DT
from .filters import compute_limit_matrices, listing_age_days

_SUFFIX_RE = r"^(\d{6})\.(SH|SZ)$"
_PREFIX_RE = r"^(SH|SZ)(\d{6})$"


def normalize_symbol(values: pd.Series) -> pd.Series:
    """后缀式 ``600036.SH`` → 前缀式 ``SH600036``；已是前缀式则原样返回。"""
    text = values.astype(str).str.upper().str.strip()
    suffix = text.str.replace(
        _SUFFIX_RE, lambda m: f"{m.group(2)}{m.group(1)}", regex=True
    )
    return suffix


def _partition_filter(start: str, end: str):
    start_i = int(start.replace("-", ""))
    end_i = int(end.replace("-", ""))
    return (ds_mod.field("dt") >= start_i) & (ds_mod.field("dt") <= end_i)


def _read_partitions(
    dataset: str, start: str, end: str, columns: list[str]
) -> pd.DataFrame:
    root = DATA_ROOT / "1_kline_data" / dataset
    if not root.is_dir():
        raise FileNotFoundError(f"QuantDB 行情目录不存在: {root}")
    table = ds_mod.dataset(str(root), format="parquet", partitioning="hive").to_table(
        columns=columns, filter=_partition_filter(start, end)
    )
    frame = table.to_pandas()
    frame["symbol"] = normalize_symbol(frame["symbol"])
    frame["trade_date"] = pd.to_datetime(frame["time"])
    frame = frame[frame["symbol"].str.match(_PREFIX_RE)]
    frame = frame.drop_duplicates(subset=["symbol", "trade_date"], keep="last")
    return frame


def _pivot(frame: pd.DataFrame, value: str) -> pd.DataFrame:
    return (
        frame.pivot(index="trade_date", columns="symbol", values=value)
        .sort_index()
        .sort_index(axis=1)
    )


def load_calendar(start: str | None = None, end: str | None = None) -> pd.DatetimeIndex:
    path = DATA_ROOT / "2_base_sector" / "trading_calendar" / "trading_days.parquet"
    table = pq.read_table(path, columns=["TradingDate", "IsTradingDay"]).to_pandas()
    table = table[table["IsTradingDay"] == 1]
    days = pd.to_datetime(table["TradingDate"].astype(str), format="%Y%m%d")
    days = pd.DatetimeIndex(sorted(days.unique()))
    if start is not None:
        days = days[days >= pd.Timestamp(start)]
    if end is not None:
        days = days[days <= pd.Timestamp(end)]
    return days


def load_price_matrices(
    start: str = PRICE_START, end: str = PRICE_END
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    raw = _read_partitions(
        "daily_unadjusted", start, end, ["symbol", "time", "close", "volume"]
    )
    forward = _read_partitions("daily_forward", start, end, ["symbol", "time", "close"])
    close_raw = _pivot(raw, "close")
    volume = _pivot(raw, "volume")
    close_qfq = _pivot(forward, "close").reindex(
        index=close_raw.index, columns=close_raw.columns
    )
    return close_raw, close_qfq, volume


def load_st_symbols(asof_dt: str = ST_ASOF_DT) -> pd.Series:
    """最新可得 ST 名单（静态回填；QuantDB 无更早的逐日 ST 标记）。"""
    path = (
        DATA_ROOT
        / "6_ml_datasets"
        / "features_daily"
        / f"dt={asof_dt}"
        / "data.parquet"
    )
    frame = pq.read_table(path, columns=["symbol", "is_st"]).to_pandas()
    frame["symbol"] = normalize_symbol(frame["symbol"])
    st = frame.loc[frame["is_st"].fillna(0) == 1, "symbol"]
    return pd.Series(True, index=pd.Index(sorted(st.unique())), dtype=bool)


def _read_stored_predictions(run_dir: Path, start: str) -> pd.DataFrame:
    path = run_dir / "pred.parquet"
    frame = pq.read_table(
        path,
        columns=["symbol", "trade_date", "pred", "label_return"],
        filters=[("trade_date", ">=", pd.Timestamp(start))],
    ).to_pandas()
    frame["symbol"] = normalize_symbol(frame["symbol"])
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    frame["source"] = "stored"
    return frame


def read_first_seen(run_dir: Path) -> pd.Series:
    """每只股票在全历史预测面板中的首次出现日（上市日代理）。"""
    frame = pq.read_table(
        run_dir / "pred.parquet", columns=["symbol", "trade_date"]
    ).to_pandas()
    frame["symbol"] = normalize_symbol(frame["symbol"])
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    return frame.groupby("symbol")["trade_date"].min()


def read_extension_predictions(out_dir: Path, model_name: str) -> pd.DataFrame:
    path = out_dir / f"pred_extend_{model_name}.parquet"
    if not path.exists():
        raise FileNotFoundError(f"缺少外推预测: {path}（先运行 extend_predictions）")
    frame = pq.read_table(path).to_pandas()
    frame["symbol"] = normalize_symbol(frame["symbol"])
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    if "label_return" not in frame.columns:
        frame["label_return"] = np.nan
    frame["source"] = "extended"
    return frame[["symbol", "trade_date", "pred", "label_return", "source"]]


def load_prediction_panel(
    run_dir: Path, out_dir: Path, model_name: str, start: str
) -> tuple[pd.DataFrame, pd.Series]:
    """训练 pred + 外推 pred 合并；返回 (面板, 全历史首次出现日)。"""
    stored = _read_stored_predictions(run_dir, start)
    first_seen = read_first_seen(run_dir)
    extension = read_extension_predictions(out_dir, model_name)
    # 外推脚本会用 08-27 做重叠校验；同一 (symbol, date) 以训练 pred 为准。
    extension = (
        extension.merge(
            stored[["symbol", "trade_date"]],
            on=["symbol", "trade_date"],
            how="left",
            indicator=True,
        )
        .query("_merge == 'left_only'")
        .drop(columns="_merge")
    )
    panel = pd.concat([stored, extension], ignore_index=True)
    panel = panel.sort_values(["trade_date", "symbol"]).reset_index(drop=True)
    first_seen = (
        pd.concat([first_seen, extension.groupby("symbol")["trade_date"].min()])
        .groupby(level=0)
        .min()
    )
    return panel, first_seen


def load_index_closes(start: str, end: str) -> pd.DataFrame:
    frame = _read_partitions("index_daily", start, end, ["symbol", "time", "close"])
    return frame.pivot(
        index="trade_date", columns="symbol", values="close"
    ).sort_index()


@dataclass
class MarketPanel:
    dates: pd.DatetimeIndex
    full_calendar: pd.DatetimeIndex
    symbols: pd.Index
    close_raw: pd.DataFrame
    close_qfq: pd.DataFrame
    volume: pd.DataFrame
    limit_up: pd.DataFrame
    limit_down: pd.DataFrame
    suspended: pd.DataFrame
    is_st: pd.Series
    age_days: pd.DataFrame
    close_qfq_ffill: pd.DataFrame

    def price(self, symbol: str, day: pd.Timestamp) -> float:
        value = self.close_qfq.at[day, symbol]
        return float(value) if pd.notna(value) else float("nan")

    def mark_price(self, symbol: str, day: pd.Timestamp) -> float:
        value = self.close_qfq_ffill.at[day, symbol]
        return float(value) if pd.notna(value) else float("nan")


def build_market_panel(
    start: str = PRICE_START, end: str = PRICE_END, st_asof: str = ST_ASOF_DT
) -> MarketPanel:
    close_raw, close_qfq, volume = load_price_matrices(start, end)
    is_st_index = load_st_symbols(st_asof)
    symbols = close_raw.columns
    is_st = is_st_index.reindex(symbols).fillna(False).astype(bool)
    limit_up, limit_down, suspended = compute_limit_matrices(close_raw, volume, is_st)
    full_calendar = load_calendar()
    return MarketPanel(
        dates=close_raw.index,
        full_calendar=full_calendar,
        symbols=symbols,
        close_raw=close_raw,
        close_qfq=close_qfq,
        volume=volume,
        limit_up=limit_up,
        limit_down=limit_down,
        suspended=suspended,
        is_st=is_st,
        age_days=pd.DataFrame(index=close_raw.index, columns=symbols, dtype=float),
        close_qfq_ffill=close_qfq.ffill(),
    )


def attach_listing_age(panel: MarketPanel, first_seen: pd.Series) -> MarketPanel:
    panel.age_days = listing_age_days(
        first_seen, panel.dates, panel.symbols, full_dates=panel.full_calendar
    )
    return panel


def forward_return_matrix(
    close_qfq: pd.DataFrame, horizon: int, lag: int = 1
) -> pd.DataFrame:
    """信号日 t 对应的实测收益：close[t+lag+horizon] / close[t+lag] - 1。"""
    values = close_qfq.to_numpy(dtype=np.float64)
    out = np.full_like(values, np.nan)
    out[: len(values) - lag - horizon] = (
        values[lag + horizon :] / values[lag:-horizon] - 1.0
    )
    out[~np.isfinite(out)] = np.nan
    return pd.DataFrame(out, index=close_qfq.index, columns=close_qfq.columns)


def extension_dates(panel: MarketPanel, start: str, end: str) -> list[str]:
    days = panel.dates[
        (panel.dates >= pd.Timestamp(start)) & (panel.dates <= pd.Timestamp(end))
    ]
    return [day.strftime("%Y-%m-%d") for day in days]


__all__ = [
    "MarketPanel",
    "attach_listing_age",
    "build_market_panel",
    "extension_dates",
    "forward_return_matrix",
    "load_calendar",
    "load_index_closes",
    "load_prediction_panel",
    "load_price_matrices",
    "load_st_symbols",
    "normalize_symbol",
    "read_first_seen",
]
