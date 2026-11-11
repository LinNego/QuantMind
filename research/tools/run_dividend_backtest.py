"""送转抢权：从事件研究走到可交易回测（可成交性 + 成本 + 组合层 + 安慰剂）。

跑法：python scratch/run_dividend_backtest.py
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
pd.set_option("display.width", 220)

# ---------------------------------------------------------------- 1. 事件
ev = con.execute(f"""
SELECT Symbol AS symbol, CAST(time AS DATE) AS ed,
       coalesce(stockBonus,0) + coalesce(stockGift,0) AS bonus
FROM read_parquet('{P}/3_financial_data/dividend_factors/**/*.parquet', union_by_name=true)
WHERE time >= DATE '2016-01-01' AND time < DATE '2026-10-01'
  AND (coalesce(stockBonus,0) + coalesce(stockGift,0)) > 0
""").df()
print(f"送转事件 {len(ev)} 条，{ev.symbol.nunique()} 只股票")

w = es.window(con, ev, pre=25, post=10)
print(f"窗口 {len(w):,} 行；有行情的 {w.eid.nunique():,} 个事件（缺行情的多为退市/停牌）")

# ---------------------------------------------------------------- 2. 可成交性
k = con.execute("SELECT symbol, d, close AS cje_close, high, low, amount FROM c.kline").df()
w = w.merge(k, on=["symbol", "d"], how="left")

def limit_pct(sym: str) -> float:
    code = sym.split(".")[0]
    if code.startswith(("300", "301", "302", "688", "689")):
        return 0.20
    if code.startswith(("43", "83", "87", "88", "92")):
        return 0.30
    return 0.10

w["lim"] = w.symbol.map(limit_pct)
w = w.sort_values(["eid", "off"]).reset_index(drop=True)
w["pct"] = w.cje_close / w.groupby("eid")["cje_close"].shift(1) - 1
w["bg"] = ((w.pct >= w.lim - 0.005) & (w.cje_close >= w.high - 1e-6)).fillna(False)
w["sl"] = ((w.pct <= -(w.lim - 0.005)) & (w.cje_close <= w.low + 1e-6)).fillna(False)
w["halt"] = w.cje_close.isna()

amt20 = w[w.off.between(-20, -1)].groupby("eid")["amount"].mean().rename("amt20")
w = w.merge(amt20, on="eid", how="left")

# ---------------------------------------------------------------- 3. 交易表
def build_trades(K: int, EXIT: int, min_amt20: float = 3000):
    e_in = w[w.off == -K].set_index("eid")
    e_out = w[w.off == EXIT].set_index("eid")
    hold = w[(w.off > -K) & (w.off <= EXIT)].copy()

    tr = pd.DataFrame({
        "bad_entry": (e_in.halt | e_in.bg),
        "bad_exit": (e_out.halt | e_out.sl).reindex(e_in.index),
        "amt20": e_in.amt20,
        "entry_d": e_in.d, "exit_d": e_out.d,
        "symbol": e_in.symbol, "ed": e_in.ed,
    })
    tr[["bad_entry", "bad_exit"]] = tr[["bad_entry", "bad_exit"]].fillna(True)
    tr[["bad_entry", "bad_exit"]] = tr[["bad_entry", "bad_exit"]].astype(bool)
    tr["ok"] = (~tr.bad_entry) & (~tr.bad_exit) & (tr.amt20 >= min_amt20)
    tr["ok"] = tr.ok & tr.entry_d.notna() & tr.exit_d.notna()

    hold["g"] = 1 + hold.r.fillna(0.0)
    tr["ret"] = hold.groupby("eid").g.prod() - 1
    m = hold.groupby("eid").apply(lambda x: (1 + x.mkt_r.fillna(0)).prod() - 1)
    tr["ret_ex"] = (1 + tr.ret) / (1 + m) - 1
    return tr.dropna(subset=["ret"])

def portfolio(tr: pd.DataFrame, cost: float = 0.0, maxpos: int = 12):
    """资金约束：每个仓位 1/maxpos；信号多于 maxpos 时按比例缩小（满仓）。"""
    ok = tr.index[tr.ok]
    h = w[(w.eid.isin(ok)) & (w.off > -K_FIX) & (w.off <= EXIT_FIX)].copy()
    h["r"] = h.r.fillna(0.0)
    npos = h.groupby("d").eid.nunique().reindex(cal).fillna(0)
    rs = h.groupby("d").r.sum().reindex(cal).fillna(0.0)
    denom = np.maximum(npos.values, maxpos)
    daily = 1 + rs.values / denom
    invested = npos.values / denom
    net = daily - cost / 5.0 * invested
    npos = pd.Series(npos.values, index=cal)
    return pd.Series(daily, index=cal), pd.Series(net, index=cal), npos, pd.Series(invested, index=cal)

def stats(net: pd.Series):
    r = net - 1
    eq = net.cumprod()
    ann = eq.iloc[-1] ** (252 / len(r)) - 1
    return {"年化": ann, "夏普": r.mean() / r.std() * np.sqrt(252),
            "最大回撤": (eq / eq.cummax() - 1).min(), "净值": eq.iloc[-1]}

cal = con.execute("SELECT d FROM c.cal WHERE d BETWEEN DATE '2016-01-01' AND DATE '2026-09-30' ORDER BY d").df().d
mkt = con.execute("SELECT d, mkt_r FROM c.mkt").df().set_index("d").mkt_r.reindex(cal).fillna(0.0)

# ---------------------------------------------------------------- 4. 漏斗
print("\n=== 过滤漏斗（K=5，卖在除权日） ===")
tr5 = build_trades(5, 0)
K_FIX, EXIT_FIX = 5, 0
n = len(tr5)
print(f"有窗口数据的事件        {n:>6}")
print(f"  买不进（涨停/停牌）   {int(tr5.bad_entry.sum()):>6}  ({tr5.bad_entry.mean():.1%})")
print(f"  卖不出（跌停/停牌）   {int(tr5.bad_exit.sum()):>6}  ({tr5.bad_exit.mean():.1%})")
print(f"  日均成交额 < 3000 万  {int((tr5.amt20 < 3000).sum()):>6}  ({(tr5.amt20 < 3000).mean():.1%})")
print(f"可交易                  {int(tr5.ok.sum()):>6}  ({tr5.ok.mean():.1%})")

ok5 = tr5[tr5.ok]
print(f"\n=== 交易层 ===\n笔数 {len(ok5)}  平均毛收益 {ok5.ret.mean()*100:.2f}%  "
      f"中位 {ok5.ret.median()*100:.2f}%  胜率 {(ok5.ret > 0).mean():.1%}")
print(f"平均板块超额 {ok5.ret_ex.mean()*100:.2f}%  中位 {ok5.ret_ex.median()*100:.2f}%")

# ---------------------------------------------------------------- 5. 配置对比
print("\n=== 组合层（等权、有仓即满仓、空仓收益 0） ===")
rows = {}
for K, EXIT in [(2, 0), (3, 0), (5, 0), (5, -1), (10, 0)]:
    K_FIX, EXIT_FIX = K, EXIT
    tr = build_trades(K, EXIT)
    _, net, npos, inv = portfolio(tr, cost=0.0025)
    s = stats(net)
    rows[f"K={K:>2},卖={EXIT:+d}"] = {
        "笔数": int(tr.ok.sum()), "均持仓": round(npos[npos > 0].mean(), 1),
        "资金占用": f"{inv.mean():.0%}", "净年化": f"{s['年化']:.1%}",
        "夏普": round(s["夏普"], 2), "回撤": f"{s['最大回撤']:.0%}",
    }
print(pd.DataFrame(rows).T.to_string())

bm = stats(1 + mkt)
print(f"基准（全市场等权）：年化 {bm['年化']:.1%}  夏普 {bm['夏普']:.2f}  回撤 {bm['最大回撤']:.0%}")

# ---------------------------------------------------------------- 6. 逐年
K_FIX, EXIT_FIX = 5, 0
tr = build_trades(5, 0)
_, net, npos, inv = portfolio(tr, cost=0.0025)
r = net - 1
y = r.groupby(pd.to_datetime(r.index).year).apply(lambda s: (1 + s).prod() - 1)
yb = mkt.groupby(pd.to_datetime(mkt.index).year).apply(lambda s: (1 + s).prod() - 1)
print("\n=== 逐年净收益 vs 全市场等权 ===")
print(pd.DataFrame({"策略": (y * 100).round(1), "市场": (yb * 100).round(1)}).to_string())

print("\n=== 分段（净年化，扣 0.25%） ===")
for a, b, name in [("2016", "2020", "2016-2020"), ("2021", "2022", "2021-2022"), ("2023", "2026", "2023-2026")]:
    seg = (net - 1)[(pd.to_datetime(net.index) >= a) & (pd.to_datetime(net.index) <= b + "-12-31")]
    ann = (1 + seg).prod() ** (252 / len(seg)) - 1
    bseg = mkt[(pd.to_datetime(mkt.index) >= a) & (pd.to_datetime(mkt.index) <= b + "-12-31")]
    bann = (1 + bseg).prod() ** (252 / len(bseg)) - 1
    print(f"  {name}: 策略 {ann:>7.1%}   市场 {bann:>7.1%}")

print("\n=== 按送转力度分组（每 10 股送转股数） ===")
tr_b = build_trades(5, 0)
tr_b = tr_b.join(ev.set_index(ev.index).assign(ed2=pd.to_datetime(ev.ed))[["bonus"]], how="left") if False else tr_b
bb = w.drop_duplicates("eid").set_index("eid")["bonus"]
tr_b["bonus"] = bb.reindex(tr_b.index)
tr_b = tr_b[tr_b.ok]
tr_b["档"] = pd.cut(tr_b.bonus, [0, 3, 6, 10, 100], labels=["≤3", "3-6", "6-10", ">10"])
print(tr_b.groupby("档", observed=True).agg(
    笔数=("ret", "size"), 平均毛收益=("ret", lambda x: round(x.mean() * 100, 2)),
    板块超额=("ret_ex", lambda x: round(x.mean() * 100, 2)),
    胜率=("ret", lambda x: f"{(x > 0).mean():.0%}")).to_string())

# ---------------------------------------------------------------- 7. 成本
print("\n=== 成本敏感性（K=5，卖除权日） ===")
daily, _, _, inv = portfolio(tr, cost=0.0)
base = daily - 1
for c in [0.0, 0.001, 0.0025, 0.005, 0.01]:
    s_ = stats(1 + base - c / 5 * inv)
    print(f"  往返 {c*100:>4.2f}%  →  年化 {s_['年化']:>7.1%}   夏普 {s_['夏普']:>5.2f}")
print(f"  盈亏平衡 ≈ {base.mean()*5/inv.mean()*100:.2f}%（平均每笔 {ok5.ret.mean()*100:.2f}%，资金占用 {inv.mean():.0%}）")

# ---------------------------------------------------------------- 8. 容量
print("\n=== 容量（单笔金额 / 20 日均成交额） ===")
a = ok5.amt20.dropna()
for cap in [100, 300, 1000]:
    p = cap / a
    print(f"  单笔 {cap:>4}万 → 中位 {p.median():.3%}  95分位 {p.quantile(.95):.3%}  >1% 占比 {(p > 0.01).mean():.0%}")

# ---------------------------------------------------------------- 9. 安慰剂
print("\n=== 安慰剂：同一批股票，随机日期，同样持有 5 天 ===")
rng = np.random.default_rng(7)
pl = pd.DataFrame({"symbol": ok5.symbol.values, "ed": rng.choice(cal, size=len(ok5))})
wp = es.window(con, pl, pre=30, post=5)
sub = wp[(wp.off > -5) & (wp.off <= 0)]
g = sub.groupby("eid").apply(lambda x: (1 + x.r.fillna(0)).prod() - 1)
mg = sub.groupby("eid").apply(lambda x: (1 + x.mkt_r.fillna(0)).prod() - 1)
print(f"  随机入场 5 天：平均毛收益 {g.mean()*100:.2f}%  板块超额 {(g - mg).mean()*100:.2f}%")
print(f"  对照真事件：  {ok5.ret.mean()*100:.2f}% / {ok5.ret_ex.mean()*100:.2f}%")

# ---------------------------------------------------------------- 10. 图
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

daily, net, npos, inv = portfolio(tr, cost=0.0025)
eq_s = net.cumprod()
eq_b = (1 + mkt).cumprod()

fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 7), sharex=True,
                               gridspec_kw={"height_ratios": [3, 1]})
ax1.plot(eq_s.index, eq_s.values, color="#8e44ad", lw=1.4, label="bonus/split capture (net 0.25%)")
ax1.plot(eq_b.index, eq_b.values, color="#7f8c8d", lw=1.1, label="equal-weight market")
ax1.set_yscale("log")
ax1.set_ylabel("NAV (log)")
ax1.set_title(f"Bonus/split pre-event capture: K=5, exit at ex-date, n={int(tr.ok.sum())} trades")
ax1.legend(loc="upper left", fontsize=8, frameon=False)
ax1.grid(alpha=0.25)
ax2.fill_between(npos.index, 0, npos.values, color="#8e44ad", alpha=0.35)
ax2.set_ylabel("open positions")
ax2.set_xlabel("date")
ax2.grid(alpha=0.25)
fig.tight_layout()
fig.savefig(os.path.join(OUT, "bt_bonus.png"), dpi=150)
ok5.to_csv(os.path.join(OUT, "out_bt_trades.csv"), index=False)
print("\n已画出 bt_bonus.png，写出 out_bt_trades.csv")
