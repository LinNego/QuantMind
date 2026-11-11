"""除权除息事件研究 v2：分组 + 逐年 + 安慰剂对照。

跑法：python scratch/run_dividend.py
输出：控制台 + scratch/out_dividend_*.csv + scratch/car_dividend.png
"""
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.normpath(os.path.join(HERE, "..", "scratch"))  # 产物落 scratch（不入库）
sys.path.insert(0, os.path.join(HERE, ".."))

from tools import event_study as es  # noqa: E402

P = "/opt/quantmind/data/quantdb"
con = es.connect()

ev = con.execute(f"""
SELECT Symbol AS symbol,
       CAST(time AS DATE) AS ed,
       coalesce(interest,0) AS cash,
       coalesce(stockBonus,0) + coalesce(stockGift,0) AS bonus
FROM read_parquet('{P}/3_financial_data/dividend_factors/**/*.parquet', union_by_name=true)
WHERE time >= DATE '2016-01-01' AND time < DATE '2026-10-01'
  AND (coalesce(interest,0) > 0 OR coalesce(stockBonus,0)+coalesce(stockGift,0) > 0)
""").df()
print(f"事件 {len(ev)} 条，股票 {ev.symbol.nunique()} 只，{ev.ed.min()} ~ {ev.ed.max()}")

w = es.window(con, ev, pre=20, post=40)
w["kind"] = np.where(w.bonus > 0, "含送转", "纯现金")
px0 = w[w.off == -1][["eid", "close"]].rename(columns={"close": "p_prev"})
w = w.merge(px0, on="eid", how="left")
w["yld"] = w.cash / w.p_prev
w["year"] = pd.to_datetime(w.ed).dt.year
print(f"窗口 {len(w):,} 行，事件 {w.eid.nunique():,} 个  "
      f"（含送转 {w[w.kind=='含送转'].eid.nunique():,} / 纯现金 {w[w.kind=='纯现金'].eid.nunique():,}）")

print("\n=== 1. 事件时间 CAR（全样本，%） ===")
c = es.curve(w)
print(c.loc[[o for o in (-20, -10, -5, -3, -1, 0, 1, 5, 10, 20, 30, 40) if o in c.index]]
      .assign(mean=lambda d: (d["mean"] * 100).round(2), t=lambda d: d["t"].round(2)).to_string())

print("\n=== 2. 持有窗口（市场调整后, %；t 按事件日聚类） ===")
for name, x in [("全样本", w), ("含送转", w[w.kind == "含送转"]), ("纯现金", w[w.kind == "纯现金"])]:
    print(f"\n-- {name}")
    print(es.hold_table(x).round(3).to_string())

print("\n=== 3. 含送转 vs 纯现金 ===")
print(es.group_curve(w, "kind", offsets=(-10, -5, -3, 0, 5, 10, 20)).to_string())

print("\n=== 4. 股息率分组（仅纯现金，去掉送转污染） ===")
cash_only = w[(w.kind == "纯现金") & w.yld.between(0.001, 0.15)].copy()
cash_only["yld_grp"] = pd.qcut(cash_only.yld, 3, labels=["低", "中", "高"])
print(es.group_curve(cash_only, "yld_grp").to_string())

print("\n=== 5. 含送转逐年（事件后 0 / +10 日 CAR, %） ===")
dates = w.drop_duplicates("eid").set_index("eid")["ed"]
rows = {}
for y, x in w[w.kind == "含送转"].groupby("year"):
    r = {}
    for off in (-5, 0, 10):
        m, t, n = es.stat(x[x.off == off].set_index("eid")["car"], dates)
        r[f"off{off:+d}"] = f"{m:+.2f}% (t={t:+.1f})"
    r["n"] = int(x.eid.nunique())
    rows[y] = r
print(pd.DataFrame(rows).T.to_string())

print("\n=== 6. 安慰剂：同一批股票、随机日期 ===")
cal_dates = con.execute(
    "SELECT d FROM c.cal WHERE d BETWEEN DATE '2016-02-01' AND DATE '2026-08-31' ORDER BY d"
).df().d.values
rng = np.random.default_rng(42)
plac = ev[["symbol"]].copy()
plac["ed"] = rng.choice(cal_dates, size=len(plac))
wp = es.window(con, plac, pre=20, post=20)
cp = es.curve(wp)
print(cp.loc[[o for o in (-10, -5, -1, 0, 1, 5, 10, 20) if o in cp.index]]
      .assign(mean=lambda d: (d["mean"] * 100).round(2), t=lambda d: d["t"].round(2)).to_string())

# 落盘
c.to_csv(os.path.join(OUT, "out_dividend_car.csv"))
es.hold_table(w).to_csv(os.path.join(OUT, "out_dividend_hold.csv"))
print("\n已写出 out_dividend_car.csv / out_dividend_hold.csv")

# 图：全样本 / 含送转 / 纯现金 / 安慰剂
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

fig, ax = plt.subplots(figsize=(9.5, 4.8))
for name, x, color, lw in [
    ("all events", w, "#c0392b", 1.8),
    ("bonus/split events", w[w.kind == "含送转"], "#8e44ad", 1.6),
    ("cash-only events", w[w.kind == "纯现金"], "#2980b9", 1.4),
    ("placebo (random dates)", wp, "#95a5a6", 1.2),
]:
    cc = es.curve(x)
    ax.plot(cc.index, cc["mean"] * 100, color=color, lw=lw, label=name)
ax.axvline(0, color="#333", lw=0.8, ls="--")
ax.axhline(0, color="#999", lw=0.6)
ax.set_xlabel("event time (trading days, 0 = ex-dividend date)")
ax.set_ylabel("CAR vs equal-weight market (%)")
ax.set_title("A-share ex-dividend events, 2016-2026")
ax.legend(loc="upper left", fontsize=8, frameon=False)
ax.grid(alpha=0.25)
fig.tight_layout()
fig.savefig(os.path.join(OUT, "car_dividend.png"), dpi=150)
print("已画出 car_dividend.png")
