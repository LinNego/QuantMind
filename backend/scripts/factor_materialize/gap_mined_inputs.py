"""gap_mined 物化的输入装载与 PIT 快照。

只负责「从 QuantDB parquet 读数据并按口径组装」：

- ``load_l2_window``：L2 基列 + close 的宽表（``symbol`` 为列）；
- ``load_backward_close``：daily_backward 收盘宽表（行业/概念指数与个股收益源）；
- ``load_sector_members``：行业（一级）/概念成员表；
- ``load_margin_balance``：两融余额宽表（市场日历）；
- ``holder_snapshot`` / ``balance_snapshot``：股东户数 / 资产负债表的 PIT 月末快照。

所有宽表索引为 ``YYYYMMDD`` 字符串日期，列为后缀式代码（``600036.SH``）。
"""

from __future__ import annotations

import glob
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pyarrow.parquet as pq

LOG = logging.getLogger("factor_materialize.gap_inputs")

L2_DIR = "6_ml_datasets/l2_factors"
BACKWARD_DIR = "1_kline_data/daily_backward"
MARGIN_DIR = "2_base_sector/margin_trading"
SECTOR_FILE = "2_base_sector/sector_concept/sector_members.parquet"
HOLDER_GLOB = "3_financial_data/holder_num/*.parquet"
BALANCE_GLOB = "3_financial_data/balance/*.parquet"

HOLDER_COLUMNS = (
    "holder_num",
    "top1_holder_shares",
    "top10_holder_shares",
    "institution_holdings",
    "fund_holdings",
)


def _dt_bounds(start: str, end: str) -> tuple[int, int]:
    return int(start), int(end)


def _connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("SET threads TO 4")
    return con


def _pivot(long: pd.DataFrame, value: str) -> pd.DataFrame:
    wide = long.pivot(index="dt", columns="symbol", values=value)
    return wide.sort_index().astype("float64")


def load_l2_window(
    quantdb_root: Path, start: str, end: str, base_columns: list[str]
) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    """装载 L2 基列与 close 宽表（``start``/``end`` 含端点，YYYYMMDD）。"""
    path = Path(quantdb_root) / L2_DIR / "dt=*/data.parquet"
    lo, hi = _dt_bounds(start, end)
    select = ", ".join(f'"{c}"' for c in base_columns)
    query = f"""
        SELECT symbol, CAST(dt AS VARCHAR) AS dt, close, {select}
        FROM read_parquet('{path}', hive_partitioning=true)
        WHERE dt BETWEEN {lo} AND {hi}
    """
    con = _connect()
    try:
        long = con.execute(query).fetchdf()
    finally:
        con.close()
    LOG.info(
        "L2 窗口装载 %d 行（%s..%s，%d 基列）", len(long), start, end, len(base_columns)
    )
    close = _pivot(long, "close")
    frames = {column: _pivot(long, column) for column in base_columns}
    # 宽表列集合可能因停牌日不同而略有差异；统一到 close 的列轴，
    # 使所有变换共用同一标的集合与同一 traded 掩码。
    symbols = close.columns
    for column, frame in frames.items():
        frames[column] = frame.reindex(columns=symbols)
    return frames, close


def load_l2_ohlcv(quantdb_root: Path, dates: list[str]) -> pd.DataFrame:
    path = Path(quantdb_root) / L2_DIR / "dt=*/data.parquet"
    lo, hi = _dt_bounds(dates[0], dates[-1])
    query = f"""
        SELECT symbol, CAST(dt AS VARCHAR) AS dt,
               open, high, low, close, volume, amount
        FROM read_parquet('{path}', hive_partitioning=true)
        WHERE dt BETWEEN {lo} AND {hi}
    """
    con = _connect()
    try:
        return con.execute(query).fetchdf()
    finally:
        con.close()


def load_backward_ohlcv(quantdb_root: Path, dates: list[str]) -> pd.DataFrame:
    path = Path(quantdb_root) / BACKWARD_DIR / "dt=*/data.parquet"
    lo, hi = _dt_bounds(dates[0], dates[-1])
    query = f"""
        SELECT symbol, CAST(dt AS VARCHAR) AS dt,
               open, high, low, close, volume, amount
        FROM read_parquet('{path}', hive_partitioning=true)
        WHERE dt BETWEEN {lo} AND {hi}
    """
    con = _connect()
    try:
        return con.execute(query).fetchdf()
    finally:
        con.close()


def load_backward_close(quantdb_root: Path, start: str, end: str) -> pd.DataFrame:
    path = Path(quantdb_root) / BACKWARD_DIR / "dt=*/data.parquet"
    lo, hi = _dt_bounds(start, end)
    query = f"""
        SELECT symbol, CAST(dt AS VARCHAR) AS dt, close
        FROM read_parquet('{path}', hive_partitioning=true)
        WHERE dt BETWEEN {lo} AND {hi}
    """
    con = _connect()
    try:
        long = con.execute(query).fetchdf()
    finally:
        con.close()
    return _pivot(long, "close")


@dataclass(frozen=True)
class SectorMembers:
    industry: pd.DataFrame  # index=SectorCode, columns=symbol, values=bool
    concept: pd.DataFrame


def load_sector_members(quantdb_root: Path) -> SectorMembers:
    path = Path(quantdb_root) / SECTOR_FILE
    frame = pd.read_parquet(path, columns=["SectorCode", "SectorType", "Symbol"])
    frame = frame.drop_duplicates(["SectorType", "SectorCode", "Symbol"])
    frame["Symbol"] = frame["Symbol"].astype(str).str.upper()

    def _matrix(sector_type: str) -> pd.DataFrame:
        part = frame[frame["SectorType"] == sector_type]
        matrix = pd.crosstab(part["SectorCode"], part["Symbol"]) > 0
        LOG.info("%s 成员矩阵 %s", sector_type, matrix.shape)
        return matrix

    return SectorMembers(
        industry=_matrix("行业板块(一级)"),
        concept=_matrix("概念板块"),
    )


def has_margin_partition(quantdb_root: Path, date: str) -> bool:
    """当日两融分区是否存在（缺失=上游未同步，不是物化缺陷）。"""
    return (Path(quantdb_root) / MARGIN_DIR / f"dt={date}" / "data.parquet").is_file()


def load_margin_balance(quantdb_root: Path, start: str, end: str) -> pd.DataFrame:
    path = Path(quantdb_root) / MARGIN_DIR / "dt=*/data.parquet"
    lo, hi = _dt_bounds(start, end)
    query = f"""
        SELECT symbol, CAST(dt AS VARCHAR) AS dt, finance_balance
        FROM read_parquet('{path}', hive_partitioning=true)
        WHERE dt BETWEEN {lo} AND {hi}
    """
    con = _connect()
    try:
        long = con.execute(query).fetchdf()
    finally:
        con.close()
    wide = _pivot(long, "finance_balance")
    LOG.info("两融余额窗口 %s..%s：%d 日 × %d 票", start, end, *wide.shape)
    return wide


# ------------------------------------------------------------------ PIT 快照


@lru_cache(maxsize=8)
def _read_financial_glob(pattern: str, columns: tuple[str, ...]) -> pd.DataFrame:
    need = ["Symbol", "m_timetag", "m_anntime", *columns]
    frames = []
    for file in glob.glob(pattern):
        if set(need) <= set(pq.ParquetFile(file).schema_arrow.names):
            frames.append(pd.read_parquet(file, columns=need))
    if not frames:
        raise FileNotFoundError(f"未找到可用财务文件：{pattern}")
    frame = pd.concat(frames, ignore_index=True)
    frame["symbol"] = frame["Symbol"].astype(str).str.upper()
    for column in ("m_anntime", "m_timetag"):
        frame[column] = frame[column].astype(str).str.replace(r"\.0$", "", regex=True)
    frame = frame[frame["m_anntime"].str.fullmatch(r"\d{8}")]
    frame = frame.drop_duplicates(["symbol", "m_timetag"]).sort_values(
        ["symbol", "m_timetag"]
    )
    return frame


def holder_snapshot(quantdb_root: Path, cutoff: str) -> pd.DataFrame:
    """股东户数月度快照：``m_anntime <= cutoff`` 的最新一期报告。

    返回列：``HD_NUMCHG_Q`` / ``HD_NUMCHG_YOY`` / ``HD_NUM_Z`` /
    ``HD_PERNUM_LN`` / ``HD_TOP1_CONC`` / ``HD_ACTIVE_SHARE``。

    - 季度网格按 ``m_timetag`` 的季度对齐（缺季 NaN），
      ``HD_NUMCHG_Q`` = ``q / q.shift(1) - 1``、``HD_NUMCHG_YOY`` =
      ``q / q.shift(4) - 1``；
    - ``HD_NUM_Z`` = 最近 8 个季度的时序 z-score（ddof=1）。
    """
    frame = _read_financial_glob(str(Path(quantdb_root) / HOLDER_GLOB), HOLDER_COLUMNS)
    frame = frame[frame["m_anntime"] <= cutoff].copy()
    if frame.empty:
        return pd.DataFrame(
            columns=[
                "HD_NUMCHG_Q",
                "HD_NUMCHG_YOY",
                "HD_NUM_Z",
                "HD_PERNUM_LN",
                "HD_TOP1_CONC",
                "HD_ACTIVE_SHARE",
            ]
        )
    frame["quarter"] = pd.PeriodIndex(
        pd.to_datetime(frame["m_timetag"], format="%Y%m%d"), freq="Q"
    )
    # 环比按「报告序列」取上一期（有缺季时不是上一季度）；同比与 z 按季度网格对齐。
    frame["prev_hn"] = frame.groupby("symbol", sort=False)["holder_num"].shift(1)
    last_rows = frame.groupby("symbol", sort=True).tail(1).set_index("symbol")

    out = pd.DataFrame(index=last_rows.index)
    out["HD_PERNUM_LN"] = np.log(
        last_rows["holder_num"] / last_rows["top10_holder_shares"]
    )
    out["HD_TOP1_CONC"] = (
        last_rows["top1_holder_shares"] / last_rows["top10_holder_shares"]
    )
    out["HD_ACTIVE_SHARE"] = (
        last_rows["fund_holdings"] / last_rows["institution_holdings"]
    )
    out["HD_NUMCHG_Q"] = (
        last_rows["holder_num"].astype(float) / last_rows["prev_hn"].astype(float) - 1.0
    )

    yoy = pd.Series(np.nan, index=out.index)
    z8 = pd.Series(np.nan, index=out.index)
    for symbol, group in frame.groupby("symbol", sort=False):
        series = group.set_index("quarter")["holder_num"].astype(float)
        series = series[~series.index.duplicated(keep="last")]
        grid = pd.period_range(series.index.min(), series.index.max(), freq="Q")
        series = series.reindex(grid)
        yoy[symbol] = series.iloc[-1] / series.shift(4).iloc[-1] - 1.0
        window = series.tail(8)
        if len(window) == 8 and not window.isna().any() and window.std(ddof=1) > 0:
            z8[symbol] = (window.iloc[-1] - window.mean()) / window.std(ddof=1)
    out["HD_NUMCHG_YOY"] = yoy
    out["HD_NUM_Z"] = z8
    return out


def balance_snapshot(quantdb_root: Path, cutoff: str) -> pd.DataFrame:
    """资产负债表月度快照：``m_anntime <= cutoff`` 最新一期 ``fix_assets / tot_assets``。"""
    frame = _read_financial_glob(
        str(Path(quantdb_root) / BALANCE_GLOB), ("fix_assets", "tot_assets")
    )
    frame = frame[frame["m_anntime"] <= cutoff]
    latest = frame.groupby("symbol", sort=True).tail(1).set_index("symbol")
    ratio = latest["fix_assets"].astype(float) / latest["tot_assets"].astype(float)
    return pd.DataFrame({"FIN_FIXASSET_R": ratio})
