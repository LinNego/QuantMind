"""构建复盘面板并计算个股收益结构（2024-09-23 基线 ~ 2026-10-23）。"""
import os
import time

import numpy as np
import pandas as pd

SRC = "/home/quantmind/quantdb/1_kline_data"
DST = "/data/ljc/vigap/quant/data"
START, END = "20240801", "20261023"
T0, T1 = "2024-09-23", "2026-10-23"


def build(ds, cols):
    dirs = sorted(d for d in os.listdir(f"{SRC}/{ds}")
                  if d.startswith("dt=") and START <= d[3:] <= END)
    parts = [pd.read_parquet(f"{SRC}/{ds}/{d}/data.parquet", columns=cols) for d in dirs]
    return pd.concat(parts, ignore_index=True).sort_values(["symbol", "time"])


def main():
    t = time.time()
    px = build("daily_backward", ["symbol", "time", "open", "high", "low", "close", "volume", "amount"])
    raw = build("daily_unadjusted", ["symbol", "time", "close"])
    px.to_parquet(f"{DST}/px.parquet", index=False)
    raw.rename(columns={"close": "close_raw"}).to_parquet(f"{DST}/px_raw.parquet", index=False)
    idx = build("index_daily", ["symbol", "time", "close", "amount"])
    idx.to_parquet(f"{DST}/index.parquet", index=False)
    print("panel", px.shape, px.time.min().date(), px.time.max().date(), f"{time.time()-t:.1f}s")

    px = px.sort_values(["symbol", "time"])
    g = px.groupby("symbol", sort=False)
    px["ret"] = g["close"].pct_change()
    px["amt20"] = g["amount"].transform(lambda s: s.rolling(20).mean())

    base = px[px.time == T0].set_index("symbol")["close"]
    px_after = px[(px.time >= T0) & (px.time <= T1)]

    rows = []
    for sym, d in px_after.groupby("symbol", sort=False):
        c = d["close"].values
        if len(c) < 100 or sym not in base.index or not np.isfinite(base[sym]):
            continue
        b = base[sym]
        ret = c[-1] / b - 1
        imin = int(np.argmin(c))
        imax_after = imin + int(np.argmax(c[imin:]))
        drawup = c[imax_after] / c[imin] - 1
        imax = int(np.argmax(c))
        dd = 1 - c[imax:].min() / c[imax] if imax < len(c) - 1 else 0.0
        mult_dates = {}
        for m in (2, 3, 5, 10):
            hit = np.where(c >= b * m)[0]
            mult_dates[m] = d["time"].values[hit[0]] if len(hit) else np.datetime64("NaT")
        rows.append({
            "symbol": sym, "ret": ret, "drawup": drawup,
            "low_date": d["time"].values[imin], "high_date": d["time"].values[imax_after],
            "peak_date": d["time"].values[imax], "maxdd": dd,
            "px_base": b, "px_last": c[-1],
            "amt20": np.nanmean(d["amt20"].values[-60:]),
            **{f"d{m}x": mult_dates[m] for m in (2, 3, 5, 10)},
        })
    res = pd.DataFrame(rows)
    res.to_parquet(f"{DST}/stock_stats.parquet", index=False)
    print(res.shape, "median ret", round(res.ret.median(), 3),
          "n>5x", int((res.ret > 4).sum()), "n>10x", int((res.ret > 9).sum()))
    print("drawup>5x", int((res.drawup > 4).sum()), "drawup>10x", int((res.drawup > 9).sum()))


if __name__ == "__main__":
    main()
