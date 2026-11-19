"""QuantDB 读取助手（plan/n01 专用）。

只封装「路径 + 常用过滤」，不藏逻辑；每天的任务脚本直接 import 它，避免反复写
read_parquet('data/quantdb/...dt=*/data.parquet')。

字段口径（实测，2026-11）：
  - 日线：symbol, time, open, high, low, close, volume, amount, dt
    amount 单位=万元，volume 单位=股；close*volume/amount ≈ 1e4（不复权）
  - L1：symbol, date, open..amount + 112 个因子列
  - 财务：每股一个 parquet，字段 m_timetag(报告期) / m_anntime(公告日)
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]  # plan/n01/tools -> 仓库根目录
DATA = ROOT / "data" / "quantdb"

KLINE_COLS = ["symbol", "time", "open", "high", "low", "close", "volume", "amount"]


def _where(start, end, symbols):
    conds, params = [], []
    if start:
        conds.append("dt >= ?")
        params.append(int(str(start).replace("-", "")))
    if end:
        conds.append("dt <= ?")
        params.append(int(str(end).replace("-", "")))
    if symbols:
        conds.append(f"symbol in ({','.join(['?'] * len(symbols))})")
        params.extend(symbols)
    return (" where " + " and ".join(conds)) if conds else "", params


def kline(adj: str = "forward", start=None, end=None, symbols=None,
          columns: list[str] | None = None) -> pd.DataFrame:
    """读日线。adj: forward / backward / unadjusted。"""
    if adj not in ("forward", "backward", "unadjusted"):
        raise ValueError(f"adj 只能是 forward/backward/unadjusted，收到 {adj!r}")
    cols = columns or KLINE_COLS
    path = DATA / "1_kline_data" / f"daily_{adj}" / "dt=*" / "data.parquet"
    where, params = _where(start, end, symbols)
    sql = f"select {', '.join(cols)} from read_parquet('{path}'){where} order by dt, symbol"
    df = duckdb.connect().execute(sql, params).df()
    if "time" in df.columns:
        df["time"] = pd.to_datetime(df["time"])
    return df


def trading_days(start=None, end=None) -> pd.Series:
    """交易日序列（来自日线 dt 去重）。"""
    where, params = _where(start, end, None)
    sql = f"select distinct dt from read_parquet('{DATA}/1_kline_data/daily_forward/dt=*/data.parquet'){where} order by dt"
    return duckdb.connect().execute(sql, params).df()["dt"]


def l1(start=None, end=None, symbols=None, columns: list[str] | None = None) -> pd.DataFrame:
    """读 L1 预计算因子（120 列）。"""
    cols = ", ".join(columns) if columns else "*"
    where, params = _where(start, end, symbols)
    sql = f"select {cols} from read_parquet('{DATA}/6_ml_datasets/l1_factors/dt=*/data.parquet'){where}"
    return duckdb.connect().execute(sql, params).df()


def instrument_detail() -> pd.DataFrame:
    """个股详情快照（5581 行，含 IsQuitGP/IsSTGP/J_start 等）。"""
    return pd.read_parquet(DATA / "2_base_sector" / "instrument_detail" / "instrument_list.parquet")


def financial(statement: str, symbol: str) -> pd.DataFrame:
    """读单只票的财报表。statement: income / balance / cashflow / capital ..."""
    path = DATA / "3_financial_data" / statement / f"{symbol}.parquet"
    return pd.read_parquet(path)


def symbol_file(statement: str, symbol: str) -> Path:
    return DATA / "3_financial_data" / statement / f"{symbol}.parquet"
