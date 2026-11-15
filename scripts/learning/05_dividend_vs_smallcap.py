#!/usr/bin/env python3
"""送转抢权 vs 微盘策略：相关性 / 年化 / 回撤 / 组合效果

复用 research/tools/run_dividend_backtest.py 里已经写好的函数，
不去改他们的代码，只把净值取出来做对比。

默认用文档里最好的配置：K=5（提前 5 天买），EXIT=-1（卖在登记日）。
"""
from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path("/root/quant/QuantMind")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "research"))

print(">>> 正在执行送转回测脚本（较慢，1-3 分钟）...")
ns = runpy.run_path(str(ROOT / "research/tools/run_dividend_backtest.py"))
print(">>> 回测脚本执行完毕\n")

# ---- 用文档里的最优配置重跑一次组合层 ----
ns["K_FIX"] = 5
ns["EXIT_FIX"] = -1
tr = ns["build_trades"](5, -1)
daily, net, npos, inv = ns["portfolio"](tr, cost=0.0025)

eq = net.cumprod()                      # 日净值（已扣 0.25% 往返成本）
s = ns["stats"](net)

print("=" * 70)
print("送转抢权（K=5，卖登记日，扣 0.25% 往返）")
print("=" * 70)
print(f"  年化      {s['年化']*100:>7.2f}%")
print(f"  最大回撤  {s['最大回撤']*100:>7.2f}%")
print(f"  夏普      {s['夏普']:>7.2f}")
print(f"  平均资金占用 {inv.mean()*100:>5.1f}%")

# ---- 转月度收益 ----
m_eq = eq.resample("ME").last()
m_ret = m_eq.pct_change().dropna()
m_ret.index = m_ret.index.to_period("M")

# ---- 微盘策略月度收益（同样修正「信号日 vs 收益月」的错位）----
d = json.load(open("/tmp/sep25_base.json"))
mnav = pd.Series(d["strategy"]["nav"])
mnav.index = pd.to_datetime(mnav.index.astype(str), format="%Y%m%d")
mnav = mnav.sort_index()
m_small = mnav.pct_change().dropna()
m_small.index = m_small.index.to_period("M") + 1

common = m_ret.index.intersection(m_small.index)
print("\n" + "=" * 70)
print("相关性（月度，共同区间）")
print("=" * 70)
print(f"  区间：{common.min()} ~ {common.max()}，共 {len(common)} 个月")

rho = m_ret.reindex(common).corr(m_small.reindex(common))
print(f"  送转抢权 vs 微盘  相关性 = {rho:+.3f}")

# 分段相关性
for a, b in [("2017-01", "2020-12"), ("2021-01", "2023-12"), ("2024-01", "2026-09")]:
    seg = common[(common >= pd.Period(a, "M")) & (common <= pd.Period(b, "M"))]
    if len(seg) > 6:
        r = m_ret.reindex(seg).corr(m_small.reindex(seg))
        print(f"    {a} ~ {b}: {r:+.3f}（{len(seg)} 个月）")

# ---- 组合效果：50/50 月度再平衡 ----
print("\n" + "=" * 70)
print("组合效果（送转 50% + 微盘 50%，月度再平衡）")
print("=" * 70)

a = m_ret.reindex(common).fillna(0.0)
b = m_small.reindex(common).fillna(0.0)
comb = 0.5 * a + 0.5 * b


def perf(r: pd.Series, label: str) -> dict:
    eq_ = (1 + r).cumprod()
    yrs = len(r) / 12
    ann = eq_.iloc[-1] ** (1 / yrs) - 1
    vol = r.std() * np.sqrt(12)
    dd = (eq_ / eq_.cummax() - 1).min()
    return {
        "配置": label,
        "年化": round(ann * 100, 2),
        "波动": round(vol * 100, 2),
        "夏普": round(ann / vol, 2) if vol else np.nan,
        "最大回撤": round(dd * 100, 2),
        "Calmar": round(ann / abs(dd), 2) if dd else np.nan,
    }


rows = [perf(a, "送转抢权（单独）"), perf(b, "微盘（单独）"), perf(comb, "50/50 组合")]
print(pd.DataFrame(rows).to_string(index=False))

# ---- 最优权重扫描 ----
print("\n" + "=" * 70)
print("权重扫描（送转占比 → 组合夏普）")
print("=" * 70)
scan = []
for w_ in np.arange(0, 1.01, 0.1):
    r = w_ * a + (1 - w_) * b
    p = perf(r, f"{w_:.0%} 送转")
    scan.append({"送转权重": f"{w_:.0%}", "年化": p["年化"], "波动": p["波动"],
                 "夏普": p["夏普"], "最大回撤": p["最大回撤"]})
print(pd.DataFrame(scan).to_string(index=False))
