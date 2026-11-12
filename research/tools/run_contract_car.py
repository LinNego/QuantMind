"""合同公告事件研究（重大合同）：L1 → L5 的第一遍。

跑法：
    python tools/pull_announcements.py 2023-01-01 2025-12-31   # 先拉公告
    python tools/run_contract_car.py

口径：
- 事件日 = 公告日之后的**第一个交易日**（公告多在盘后发，当天买不到）
- off=0 = 第一个可交易日，买入用其**收盘价**（放弃首日跳空，只算真正能拿到的部分）
- 收益用前复权，基准为全市场等权；t 值按事件日聚类
"""
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from tools import event_study as es  # noqa: E402

ANN = os.path.normpath(os.path.join(HERE, "..", "scratch", "announcements"))
pd.set_option("display.width", 200)

con = es.connect()

# ---------------------------------------------------------------- L1 事件表
files = sorted(f for f in os.listdir(ANN) if f.endswith(".parquet")) if os.path.isdir(ANN) else []
if not files:
    raise SystemExit("没有公告数据，先跑 tools/pull_announcements.py")
ann = pd.concat([pd.read_parquet(os.path.join(ANN, f)) for f in files], ignore_index=True)
print(f"公告 {len(ann):,} 条，覆盖 {ann.dt.nunique()} 个交易日（{ann.dt.min()} ~ {ann.dt.max()}）")

def to_suffix(code: str) -> str | None:
    c = str(code).zfill(6)
    if c.startswith(("600", "601", "603", "605", "688", "689")):
        return c + ".SH"
    if c.startswith(("000", "001", "002", "003", "300", "301", "302")):
        return c + ".SZ"
    if c.startswith(("43", "83", "87", "88", "92")):
        return c + ".BJ"
    return None

heavy = ann[ann["ann_type"] == "重大合同"].copy()
heavy["symbol"] = heavy["code"].map(to_suffix)
heavy = heavy.dropna(subset=["symbol"])
print(f"重大合同 {len(heavy):,} 条，{heavy.symbol.nunique()} 只股票；按年：")
print(heavy.dt.str[:4].value_counts().sort_index().to_string())

# 公告日 → 下一个交易日
cal = con.execute("SELECT d, seq FROM c.cal ORDER BY d").df()
next_day = {cal.d.iloc[i]: cal.d.iloc[i + 1] for i in range(len(cal) - 1)}
heavy["ann_d"] = pd.to_datetime(heavy.dt, format="%Y%m%d").dt.date
heavy["ed"] = heavy.ann_d.map(next_day)
ev = (heavy.dropna(subset=["ed"])[["symbol", "ed", "title", "dt"]]
      .drop_duplicates(["symbol", "ed"]))
print(f"去重后事件 {len(ev):,} 个")

# ---------------------------------------------------------------- L2 CAR
w = es.window(con, ev, pre=10, post=20)
w["year"] = pd.to_datetime(w.ed).dt.year
print(f"窗口 {len(w):,} 行")

# 首日跳空（公告次日开盘 / 公告日收盘 - 1），用不复权
k = con.execute("SELECT symbol, d, open, close FROM c.kline").df()
k["prev_close"] = k.groupby("symbol")["close"].shift(1)
gap = ev.merge(k, left_on=["symbol", "ed"], right_on=["symbol", "d"], how="left")
gap["gap"] = gap.open / gap.prev_close - 1
print(f"\n公告次日开盘跳空：中位 {gap.gap.median()*100:+.2f}%  均值 {gap.gap.mean()*100:+.2f}%  "
      f"|跳空|>5% 占比 {(gap.gap.abs() > 0.05).mean():.1%}")

print("\n=== 事件时间 CAR（%） ===")
c = es.curve(w)
print(c.loc[[o for o in (-10, -5, -3, -1, 0, 1, 2, 3, 5, 10, 20) if o in c.index]]
      .assign(mean=lambda d: (d["mean"] * 100).round(2), t=lambda d: d["t"].round(2)).to_string())

print("\n=== 持有窗口（买入 = off0 收盘） ===")
ht = es.hold_table(w, entries=(0, 1, 2), holds=(3, 5, 10, 20))
print(ht.round(2).to_string())

print("\n=== 逐年（off=0 之后 5 日） ===")
dates = w.drop_duplicates("eid").set_index("eid")["ed"]
rows = {}
for y, x in w.groupby("year"):
    d = x[x.off == 5].set_index("eid")["car"]
    m, t, n = es.stat(d, dates)
    rows[y] = (round(m, 2), round(t, 2), n)
print(pd.DataFrame(rows, index=["CAR%", "t", "n"]).T.to_string())
