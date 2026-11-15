#!/usr/bin/env python3
"""第 2 步：最小事件研究 —— 事件发生后，股价有没有「超额收益」？

这是判断「事件驱动有没有信息」的第一道检验。

方法（事件研究法 Event Study）：
    1. 事件日 T 公告 → 保守起见，从 T+1 开始计（避免盘中信息前视）
    2. 计算 T+1 之后 1/5/20 日的个股收益
    3. 减去同日「全市场等权」收益 → 得到超额收益 AR
    4. 按事件类型汇总：均值、胜率、t 值

    5. 安慰剂：同一批股票，在「非事件日」随机取样，算同样的超额收益。
       如果事件日的超额收益和随机日差不多，说明事件没用。

输出：控制台表格 + data/events/event_study_result.parquet
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
EVENTS = ROOT / "data/events/event_library.parquet"
FEAT = "/opt/quantmind/data/quantdb/6_ml_datasets/features_daily"
OUT = ROOT / "data/events/event_study_result.parquet"

WINDOWS = {"r1": "T+1", "r5": "T+5", "r20": "T+20"}


def load_market(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """全市场等权基准：每个交易日的平均未来收益。"""
    return con.execute(f"""
        SELECT time,
               avg(future_return_1d)  AS m1,
               avg(future_return_5d)  AS m5,
               avg(future_return_20d) AS m20
        FROM read_parquet('{FEAT}/*/data.parquet')
        WHERE time >= DATE '2016-01-01'
        GROUP BY time ORDER BY time
    """).df()


def load_calendar(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    return con.execute(f"""
        SELECT DISTINCT time FROM read_parquet('{FEAT}/*/data.parquet')
        WHERE time >= DATE '2016-01-01' ORDER BY time
    """).df()


def main() -> None:
    con = duckdb.connect()

    ev = pd.read_parquet(EVENTS)
    ev = ev[ev["is_first"]].copy()          # 只保留首次事件
    ev["dt"] = pd.to_datetime(ev["event_date"].astype(str), format="%Y%m%d").astype("datetime64[ns]")
    print(f"首次事件：{len(ev):,} 条")

    # ---- 事件日 → 下一个交易日（T+1）----
    cal = load_calendar(con).rename(columns={"time": "trade_date"}).sort_values("trade_date")
    cal["trade_date"] = pd.to_datetime(cal["trade_date"]).astype("datetime64[ns]")

    ev = ev.sort_values("dt")
    ev = pd.merge_asof(
        ev, cal, left_on="dt", right_on="trade_date",
        direction="forward", allow_exact_matches=False,
    )
    ev = ev.dropna(subset=["trade_date"]).rename(columns={"trade_date": "base_date"})
    print(f"能对齐到交易日：{len(ev):,} 条")

    # ---- 取事件股的未来收益 ----
    con.register("ev_reg", ev[["event_id", "symbol", "base_date", "category", "category_name"]])
    feat = con.execute(f"""
        SELECT e.event_id, f.future_return_1d AS r1,
               f.future_return_5d AS r5, f.future_return_20d AS r20
        FROM ev_reg e
        JOIN read_parquet('{FEAT}/*/data.parquet') f
          ON f.symbol = e.symbol AND f.time = e.base_date
    """).df()
    print(f"取到收益：{len(feat):,} 条")

    df = ev.merge(feat, on="event_id", how="inner")

    # ---- 减基准，得到超额收益 ----
    mkt = load_market(con)
    mkt["base_date"] = pd.to_datetime(mkt["time"]).astype("datetime64[ns]")
    mkt = mkt[["base_date", "m1", "m5", "m20"]]
    df = df.merge(mkt, on="base_date", how="left")
    for w, m in [("r1", "m1"), ("r5", "m5"), ("r20", "m20")]:
        df[f"ar_{w}"] = df[w] - df[m]
    df = df.dropna(subset=["ar_r1", "ar_r5", "ar_r20"])

    # ---- 安慰剂：同批股票随机取非事件日 ----
    rng = np.random.default_rng(42)
    n_placebo = len(df)
    symbols = df["symbol"].unique().tolist()
    pick = rng.choice(symbols, size=n_placebo, replace=True)
    all_dates = cal["trade_date"].values
    pick_dates = rng.choice(all_dates, size=n_placebo, replace=True)
    ph = pd.DataFrame({"symbol": pick, "base_date": pick_dates})
    ph["event_id"] = "placebo_" + ph.index.astype(str)
    con.register("ph_reg", ph)
    ph_feat = con.execute(f"""
        SELECT p.event_id, f.future_return_1d AS r1,
               f.future_return_5d AS r5, f.future_return_20d AS r20
        FROM ph_reg p
        JOIN read_parquet('{FEAT}/*/data.parquet') f
          ON f.symbol = p.symbol AND f.time = p.base_date
    """).df()
    ph = ph.merge(ph_feat, on="event_id").merge(mkt, on="base_date", how="left")
    for w, m in [("r1", "m1"), ("r5", "m5"), ("r20", "m20")]:
        ph[f"ar_{w}"] = ph[w] - ph[m]
    ph = ph.dropna(subset=["ar_r1", "ar_r5", "ar_r20"])

    # ---- 汇总 ----
    def summarize(g: pd.DataFrame, label: str) -> dict:
        row = {"类型": label, "样本": len(g)}
        for w in WINDOWS:
            v = g[f"ar_{w}"].astype(float)
            row[f"{WINDOWS[w]}超额%"] = round(v.mean(), 2)
            row[f"{WINDOWS[w]}胜率%"] = round((v < 0).mean() * 100, 1)
            se = v.std() / np.sqrt(len(v)) if len(v) else np.nan
            row[f"{WINDOWS[w]}t"] = round(v.mean() / se, 2) if se and se > 0 else np.nan
        return row

    rows = []
    for cat, g in df.groupby("category_name"):
        rows.append(summarize(g, cat))
    rows.append(summarize(ph, "【安慰剂】随机日"))
    rows.append(summarize(df, "全部事件"))
    res = pd.DataFrame(rows)

    pd.set_option("display.width", 200)
    print("\n" + "=" * 100)
    print("事件后超额收益（相对全市场等权，负 = 跑输市场）")
    print("=" * 100)
    print(res.to_string(index=False))

    res.to_parquet(OUT, index=False)
    df.to_parquet(ROOT / "data/events/event_study_detail.parquet", index=False)
    print(f"\n结果 → {OUT}")
    print(f"明细 → {ROOT / 'data/events/event_study_detail.parquet'}")


if __name__ == "__main__":
    main()
