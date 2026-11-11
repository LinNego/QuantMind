"""事件研究工具：把一张「事件表」变成事件时间的 CAR / 持有收益。

用法见文件末尾的 __main__ 与 scratch/run_dividend.py。

口径约定（全仓库统一，别各写各的）：
  - 事件时间 off = 事件日 与 窗口日 之间相隔的「交易日」个数（用交易日历算，不是自然日）
  - 异常收益 ar = 个股日收益 − 全市场等权日收益
  - CAR = ar 的累加；持有收益 = 复利比值（不是 ar 相加）
  - t 值一律按「事件日」聚类：先把同一事件日的所有事件取均值，再对日期序列做 t 检验
"""
from __future__ import annotations

import os

import duckdb
import numpy as np
import pandas as pd

CACHE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "scratch", "cache.duckdb"
)


def connect(cache: str = CACHE) -> duckdb.DuckDBPyConnection:
    """内存库 + 只读挂载缓存，临时表写在内存里，不动缓存。"""
    con = duckdb.connect()
    con.execute("SET threads=8")
    con.execute(f"ATTACH '{os.path.abspath(cache)}' AS c (READ_ONLY)")
    return con


def window(
    con: duckdb.DuckDBPyConnection,
    events: pd.DataFrame,
    pre: int = 20,
    post: int = 40,
) -> pd.DataFrame:
    """events 至少要有 symbol / ed 两列；其余列原样带回。

    返回长表：eid, symbol, ed, off, d, r, close, mkt_r, ar, car
    """
    ev = events.copy()
    ev["ed"] = pd.to_datetime(ev["ed"]).dt.date
    con.register("ev_df", ev)
    pad = int(post * 1.8) + 15  # 自然日缓冲，覆盖长假
    con.execute("CREATE OR REPLACE TEMP TABLE ev_t AS SELECT row_number() OVER () AS eid, * FROM ev_df")
    con.execute(f"""
    CREATE OR REPLACE TEMP TABLE win AS
    SELECT e.eid, e.symbol, e.ed, CAST(c.d AS DATE) AS d, c.seq - c0.seq AS off,
           p.r, p.close, m.mkt_r
    FROM ev_t e
    JOIN c.cal c0 ON c0.d = e.ed
    JOIN c.cal c  ON c.d BETWEEN e.ed - INTERVAL {pad} DAY AND e.ed + INTERVAL {pad} DAY
    JOIN c.px  p  ON p.symbol = e.symbol AND p.d = c.d
    JOIN c.mkt m  ON m.d = c.d
    WHERE c.seq - c0.seq BETWEEN -{pre} AND {post}
    """)
    w = con.execute("SELECT * FROM win").df()
    w = w.sort_values(["eid", "off"]).reset_index(drop=True)
    w["ar"] = w["r"] - w["mkt_r"]
    w["car"] = w.groupby("eid")["ar"].cumsum()
    # 复利用：个股与市场的累计净值
    w["_g"] = 1.0 + w["r"].fillna(0.0)
    w["_gm"] = 1.0 + w["mkt_r"].fillna(0.0)
    w["gc"] = w.groupby("eid")["_g"].cumprod()
    w["gmc"] = w.groupby("eid")["_gm"].cumprod()
    # 把事件原始列挂回去（股息率、类型等）
    extra = [c for c in ev.columns if c not in ("symbol", "ed")]
    if extra:
        m = con.execute("SELECT * FROM ev_t").df()
        w = w.merge(m[["eid"] + extra], on="eid", how="left")
    return w


def curve(w: pd.DataFrame) -> pd.DataFrame:
    """事件时间 CAR 曲线 + 按事件日聚类的 t 值。"""
    g = w.groupby("off")["car"]
    out = pd.DataFrame({"mean": g.mean(), "n": g.count()})
    by_day = w.groupby(["ed", "off"])["car"].mean().unstack()
    se = by_day.std() / np.sqrt(by_day.notna().sum())
    out["t"] = by_day.mean() / se
    return out


def held(w: pd.DataFrame, entry: int, h: int) -> pd.Series:
    """entry 日收盘买入、持有 h 个交易日，返回每个事件的市场调整后收益。"""
    a = w[w.off == entry][["eid", "gc", "gmc"]].set_index("eid")
    b = w[w.off == entry + h][["eid", "gc", "gmc"]].set_index("eid")
    j = a.join(b, lsuffix="_in", rsuffix="_out", how="inner")
    return j.gc_out / j.gc_in / (j.gmc_out / j.gmc_in) - 1


def stat(s: pd.Series, dates: pd.Series) -> tuple[float, float, int]:
    """均值(%)、按事件日聚类的 t、样本数。dates 与 s 同索引。"""
    d = pd.DataFrame({"x": s, "ed": dates.reindex(s.index)}).dropna()
    if d.empty:
        return np.nan, np.nan, 0
    byday = d.groupby("ed")["x"].mean()
    t = byday.mean() / (byday.std() / np.sqrt(len(byday)))
    return d.x.mean() * 100, t, len(d)


def hold_table(w: pd.DataFrame, entries=(-10, -5, -1, 0), holds=(5, 10, 20)) -> pd.DataFrame:
    dates = w.drop_duplicates("eid").set_index("eid")["ed"]
    rows = {}
    for e in entries:
        for h in holds:
            m, t, n = stat(held(w, e, h), dates)
            rows[(e, h)] = (m, t, n)
    return pd.DataFrame(rows, index=["mean%", "t", "n"]).T


def group_curve(w: pd.DataFrame, col: str, offsets=(-10, -5, 0, 5, 10, 20)) -> pd.DataFrame:
    """按某列分组，在给定 offset 上看 CAR 均值与聚类 t。"""
    dates = w.drop_duplicates("eid").set_index("eid")["ed"]
    out = {}
    for g, x in w.groupby(col, observed=True):
        row = {}
        for off in offsets:
            m, t, _ = stat(x[x.off == off].set_index("eid")["car"], dates)
            row[f"off{off:+d}"] = f"{m:+.2f}% (t={t:.1f})"
        out[g] = row
    return pd.DataFrame(out).T
