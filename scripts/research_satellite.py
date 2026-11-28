#!/usr/bin/env python3
"""卫星仓位回测：把 ①②③ 叠加到「微盘 + 全天候」核心上

核心 NAV 由 research_takeprofit.py 的 simulate() 产出（微盘 e + 广度 + 全天候记账）。
卫星的记账口径（重要）：
    卫星只从「真实现金」出钱，**不加杠杆**
    w = min(目标仓位, 当日现金占比)
    组合日收益 = 核心日收益 + w × (卫星日收益 − 债日收益)

前置缓存（首次跑或数据更新后）：
    python scripts/research_satellite.py --rebuild-panel   # 建 /tmp/val_month2.parquet + /tmp/sat_fin_pit.pkl
    python scripts/research_satellite.py --rebuild-core    # 建 /tmp/sat_core_nav.parquet（约 2 分钟）
    # 还依赖 /tmp/close_daily_backward.parquet（全 A 后复权收盘）和 /tmp/创业板指.parquet

用法：
    python scripts/research_satellite.py                # 三个卫星全跑，出总表
    python scripts/research_satellite.py --only 2       # 只跑卫星②
    python scripts/research_satellite.py --hold 60 --cap 0.30 --filter ocf,debt
    python scripts/research_satellite.py --sweep-hold   # 持有期平台检验
    python scripts/research_satellite.py --sweep-cap    # 仓位上限曲线

结论（2026-11-28）：
    核心                                   26.69% / -19.68% / Calmar 1.36
    + ② 行业击球（60日 / 30% / 现金流+负债率） 28.59% / -19.68% / Calmar 1.45
    + ① 恐慌反弹                            26.88% / -19.87% / Calmar 1.35
    + ③ 深跌反弹                            26.7-27.1%  → 已否决（标的太差）
"""
from __future__ import annotations

import argparse
import glob
import importlib.util
import os
import pickle
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DB = Path("/opt/quantmind/data/quantdb")
BOND = 0.042 / 243          # 债腿日收益（与手册 4.2% 一致）
REF = REPO / "refdata"          # 入库的小文件（<15MB）
BIG = REPO / "refdata_big"      # 不入库的大面板（build_price_panel.py 重建）


def _p(name: str) -> Path:
    """优先 refdata/，其次 refdata_big/，最后 /tmp/（兼容旧环境）。"""
    for base in (REF, BIG, Path("/tmp")):
        f = base / name
        if f.exists():
            return f
    return REF / name


CORE_NAV = REF / "core_nav.parquet"
EXPO = REF / "exposure.parquet"
FIN = REF / "fin_pit.pkl"


# ---------------------------------------------------------------------------
# 面板缓存
# ---------------------------------------------------------------------------
def rebuild_panels() -> None:
    """重建 /tmp/val_month2.parquet（估值月末面板）和 /tmp/sat_fin_pit.pkl（点时财报）。"""
    fs = sorted(glob.glob(str(DB / "5_technical_derived/valuation/dt=*")))
    by_m = {os.path.basename(f)[3:9]: f for f in fs}
    rows = []
    for m in sorted(by_m):
        f = by_m[m]
        d = pd.read_parquet(f, columns=["symbol", "pb", "net_profit_ttm", "revenue_ttm", "total_mv"])
        d["dt"] = pd.to_datetime(os.path.basename(f)[3:11])
        rows.append(d)
    val = pd.concat(rows, ignore_index=True)
    val.to_parquet(str(_p("val_month2.parquet")))
    print(f"估值面板 → /tmp/val_month2.parquet  {val.shape}")

    ins = pd.read_parquet(DB / "2_base_sector/instrument_detail/instrument_list.parquet")
    lab = ins.set_index("Symbol")["rs_hyname"].astype(str)
    cnt = lab.value_counts()
    keep = set(cnt[cnt >= 10].index)
    val["ind"] = val.symbol.map(lab)
    val = val[val.ind.isin(keep)]
    val["pm"] = val.dt.dt.to_period("M")
    gs, _, _ = industry_panel_from(val, lab)
    sig = gs[(gs.pb_pos < 0.20) & (gs.px_pos < 0.25) & (gs.nps < 0)]
    cand: set[str] = set()
    for pm in sig.pm.unique():
        for ind in sig[sig.pm == pm].nsmallest(2, "pb_pos").ind:
            cand |= set(val[(val.pm == pm) & (val.ind == ind)].nlargest(6, "total_mv").symbol)
    print(f"候选股票 {len(cand)} 只，读财报…")

    bal_rows, cf_rows = [], []
    for sym in sorted(cand):
        for kind, bucket in (("balance", bal_rows), ("cashflow", cf_rows)):
            f = DB / "3_financial_data" / kind / f"{sym}.parquet"
            if not f.exists():
                continue
            d = pd.read_parquet(f)
            d["m_anntime"] = pd.to_datetime(d.m_anntime, errors="coerce")
            d = d.dropna(subset=["m_anntime"]).sort_values("m_anntime")
            for _, r in d.iterrows():
                if kind == "balance":
                    bucket.append((sym, r.m_anntime, r.get("tot_liab"), r.get("tot_assets"),
                                   r.get("goodwill"), r.get("tot_shrhldr_eqy_excl_min_int"),
                                   r.get("inventories"), r.m_timetag))
                elif pd.notna(r.get("net_cash_flows_oper_act")):
                    bucket.append((sym, r.m_anntime, r.get("net_cash_flows_oper_act"), r.m_timetag))
    bal = pd.DataFrame(bal_rows, columns=["sym", "anntime", "a", "b", "goodwill", "equity", "inv", "tt"])
    cf = pd.DataFrame(cf_rows, columns=["sym", "anntime", "ocf", "tt"])
    cf["isFY"] = cf.tt.astype(str).str.endswith("1231")
    pickle.dump({"bal": bal, "cf": cf[cf.isFY]}, open(FIN, "wb"))
    print(f"点时财报 → {FIN}  bal={len(bal)} cf(FY)={len(cf[cf.isFY])}")


# ---------------------------------------------------------------------------
# 核心 NAV
# ---------------------------------------------------------------------------
def build_core(rebuild: bool = False) -> pd.DataFrame:
    """跑 research_takeprofit 的 simulate()，拿日频 NAV（含现金）。"""
    if CORE_NAV.exists() and not rebuild:
        return pd.read_parquet(CORE_NAV)
    spec = importlib.util.spec_from_file_location("tp", HERE / "research_takeprofit.py")
    tp = importlib.util.module_from_spec(spec)
    sys.modules["tp"] = tp
    spec.loader.exec_module(tp)

    bl = tp.load_bl()
    panel = tp.ensure_cache(bl, False)
    con = bl.connect(tp.DATA_DIR)
    plan = bl.build_plan(con)
    pool = tp.build_pool(bl, panel)
    picks = tp.build_picks(pool)
    rank = tp.build_rank(pool)
    exp = tp.build_exposure(bl, con, plan)
    breadth = tp.build_breadth(con, 20)
    exp = {d: (min(e, 0.3) if breadth.get(int(d), 100.0) < 30.0 else e) for d, e in exp.items()}
    aw = tp.load_aw(Path("/tmp/aw_synth.csv"), {}) if Path("/tmp/aw_synth.csv").exists() else None
    daily = tp.build_daily(con, sorted(picks.symbol.unique()),
                           int(plan.entry_dt.min()), int(plan.exit_dt.max()))
    nav, _ = tp.simulate(plan, picks, daily, exp, tp=0.20, start=20170101, end=20261030,
                         rebuy=False, rank=rank, refill=False, cost=None, aw=aw)
    nav["dt"] = pd.to_datetime(nav.dt.astype(str))
    nav = nav.set_index("dt").sort_index()
    nav.to_parquet(CORE_NAV)
    pd.Series(exp, name="e").rename_axis("dt").to_frame().to_parquet(EXPO)
    return nav


# ---------------------------------------------------------------------------
# 行业面板
# ---------------------------------------------------------------------------
def industry_panel_from(val: pd.DataFrame, lab: pd.Series) -> tuple[pd.DataFrame, None, None]:
    """给定估值面板 + 行业标签，算行业月度指标（pb_pos / px_pos / 净利TTM）。"""
    close = pd.read_parquet(str(_p("close_daily_backward.parquet"))).sort_index()
    close.index = pd.to_datetime(close.index)
    cnt = lab.value_counts()
    keep = cnt[cnt >= 10].index
    val = val.copy()
    val["ind"] = val.symbol.map(lab)
    val["pm"] = val.dt.dt.to_period("M")
    gs = (val.groupby(["dt", "ind"]).agg(pb=("pb", "median"), nps=("net_profit_ttm", "sum"))
          .reset_index().sort_values(["ind", "dt"]))
    gs["pm"] = gs.dt.dt.to_period("M")

    def _pos(s, w):
        mn = s.rolling(w, min_periods=int(w * 0.6)).min()
        mx = s.rolling(w, min_periods=int(w * 0.6)).max()
        return (s - mn) / (mx - mn).replace(0, np.nan)

    gs["pb_pos"] = gs.groupby("ind")["pb"].transform(lambda s: _pos(s, 36))

    cols = [c for c in close.columns if c in lab.index and lab[c] in keep]
    mr = close[cols].resample("ME").last().pct_change()
    ipx = (1 + mr.T.groupby(lab.reindex(cols)).mean().T.fillna(0)).cumprod()
    ipx.index = ipx.index.to_period("M")
    px = ipx.stack().rename("px").reset_index()
    px.columns = ["pm", "ind", "px"]
    gs = gs.merge(px, on=["pm", "ind"], how="left").sort_values(["ind", "pm"])
    gs["px_pos"] = gs.groupby("ind")["px"].transform(lambda s: _pos(s, 12))
    return gs, lab, cols


def industry_panel() -> tuple[pd.DataFrame, pd.Series, list[str]]:
    val = pd.read_parquet(str(_p("val_month2.parquet")))
    ins = pd.read_parquet(DB / "2_base_sector/instrument_detail/instrument_list.parquet")
    lab = ins.set_index("Symbol")["rs_hyname"].astype(str)
    return industry_panel_from(val, lab)


# ---------------------------------------------------------------------------
# 卫星① 恐慌反弹
# ---------------------------------------------------------------------------
def sat1(days: pd.DatetimeIndex, close: pd.DataFrame, weight=0.10) -> tuple[pd.Series, pd.Series]:
    ma20 = close.rolling(20).mean()
    breadth = ((close > ma20).sum(axis=1) / ma20.notna().sum(axis=1)).reindex(days)
    cyb = pd.read_parquet(str(_p("index_cyb.parquet"))).set_index("date").sort_index()
    cyb.index = pd.to_datetime(cyb.index)
    ret = cyb["close"].pct_change().reindex(days).fillna(0)
    trig = (breadth < 0.15).fillna(False)
    pos = pd.Series(0.0, index=days)
    last = -999
    for i, v in enumerate(trig.values):
        if v and i - last >= 20:
            pos.iloc[i + 1:i + 6] = weight
            last = i
    return pos, pos * ret


# ---------------------------------------------------------------------------
# 卫星② 行业击球
# ---------------------------------------------------------------------------
def _load_fin() -> pd.DataFrame | None:
    if not FIN.exists():
        return None
    d = pickle.load(open(FIN, "rb"))
    bal = d["bal"].sort_values(["sym", "anntime"]).copy()
    bal["inv_yoy"] = bal.groupby("sym")["inv"].pct_change(4)
    bal["debt"] = bal.a / bal.b
    bal["gw"] = bal.goodwill / bal.equity.replace(0, np.nan)
    return {"bal": bal.sort_values("anntime"), "cf": d["cf"].sort_values("anntime")}


def sat2(days: pd.DatetimeIndex, gs: pd.DataFrame, lab: pd.Series, cols: list[str],
         hold=60, cap=0.30, filters=("ocf", "debt"), dynamic=True):
    close = pd.read_parquet(str(_p("close_daily_backward.parquet"))).sort_index()
    close.index = pd.to_datetime(close.index)
    R = close[cols].reindex(days).ffill().pct_change().fillna(0)
    val = pd.read_parquet(str(_p("val_month2.parquet")))
    val = val[val.symbol.isin(lab.index)].copy()
    val["ind"] = val.symbol.map(lab)
    val["pm"] = val.dt.dt.to_period("M")

    sig = gs[(gs.pb_pos < 0.20) & (gs.px_pos < 0.25) & (gs.nps < 0)]
    n_by_month = sig.groupby("pm").size()

    fin = _load_fin()

    def ok(sym, when) -> bool:
        if not filters or fin is None:
            return True
        if "ocf" in filters:
            s = fin["cf"][(fin["cf"].sym == sym) & (fin["cf"].anntime <= when)]
            if len(s) == 0 or not (s.iloc[-1].ocf > 0):
                return False
        for key, col, bad in (("debt", "debt", lambda v: v < 0.70),
                              ("gw", "gw", lambda v: v < 0.30),
                              ("inv", "inv_yoy", lambda v: v < 0.20)):
            if key in filters:
                s = fin["bal"][(fin["bal"].sym == sym) & (fin["bal"].anntime <= when)]
                if len(s) == 0 or not bad(s.iloc[-1][col]):
                    return False
        return True

    w = np.zeros(len(days))
    ct = np.zeros(len(days))
    last: dict[str, int] = {}
    for pm in sorted(sig.pm.unique()):
        i0 = int(np.searchsorted(days.values, np.datetime64(pm.to_timestamp("M"))))
        if i0 >= len(days) - hold - 5:
            continue
        n = int(n_by_month.get(pm, 1))
        unit = 0.05 * (min(6.0, 1 + 0.15 * n) if dynamic else 1.0)
        when = days[i0]
        for ind in sig[sig.pm == pm].nsmallest(2, "pb_pos").ind:
            if i0 - last.get(ind, -999) < hold:
                continue
            pool = [x for x in val[(val.pm == pm) & (val.ind == ind)]
                    .nlargest(8, "total_mv").symbol if x in R.columns]
            st = [x for x in pool if ok(x, when)][:3]
            if not st or w[i0] + unit > cap + 1e-9:
                continue
            rr = R[st].iloc[i0:i0 + hold].mean(axis=1).values
            w[i0:i0 + hold] += unit
            ct[i0:i0 + hold] += unit * rr
            last[ind] = i0
    return pd.Series(w, index=days), pd.Series(ct, index=days)


# ---------------------------------------------------------------------------
# 卫星③ 深跌反弹（已否决，保留用于复核）
# ---------------------------------------------------------------------------
def sat3(days: pd.DatetimeIndex, weight=0.10) -> tuple[pd.Series, pd.Series]:
    cyb = pd.read_parquet(str(_p("index_cyb.parquet"))).set_index("date").sort_index()
    cyb.index = pd.to_datetime(cyb.index)
    px = cyb["close"].reindex(days).ffill()
    dd = px / px.cummax() - 1
    pos = (dd <= -0.50).astype(float) * weight
    return pos, pos * px.pct_change().fillna(0)


# ---------------------------------------------------------------------------
def evaluate(nav: pd.DataFrame, extra_w: pd.Series, extra_r: pd.Series, label: str) -> dict:
    core = nav.nav.pct_change().fillna(0)
    cash = (nav.cash / nav.nav).clip(0, 1)
    w = extra_w.clip(upper=cash)                      # 只能用真实现金，不加杠杆
    sat = (extra_r / extra_w.replace(0, np.nan)).fillna(0)
    port = core + w * (sat - BOND)
    eq = (1 + port).cumprod()
    yrs = len(port) / 243
    cagr = eq.iloc[-1] ** (1 / yrs) - 1
    mdd = (eq / eq.cummax() - 1).min()
    print(f"{label:<38}{cagr:>+8.2%}{mdd:>9.2%}{cagr / abs(mdd):>8.2f}{w.mean():>9.2%}")
    return {"label": label, "cagr": cagr, "mdd": mdd, "occupancy": w.mean()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild-core", action="store_true")
    ap.add_argument("--rebuild-panel", action="store_true")
    ap.add_argument("--only", type=int, choices=[1, 2, 3])
    ap.add_argument("--hold", type=int, default=60)
    ap.add_argument("--cap", type=float, default=0.30)
    ap.add_argument("--filter", default="ocf,debt", help="ocf,debt,gw,inv；空字符串=不过滤")
    ap.add_argument("--no-dynamic", action="store_true", help="关掉按触发数加权")
    ap.add_argument("--sweep-hold", action="store_true")
    ap.add_argument("--sweep-cap", action="store_true")
    args = ap.parse_args()

    if args.rebuild_panel:
        rebuild_panels()
        return 0
    nav = build_core(args.rebuild_core)
    days = nav.index
    close = pd.read_parquet(str(_p("close_daily_backward.parquet"))).sort_index()
    close.index = pd.to_datetime(close.index)
    gs, lab, cols = industry_panel()
    filters = tuple(f for f in args.filter.split(",") if f)

    print(f"{'组合':<38}{'年化':>8}{'回撤':>9}{'Calmar':>8}{'占用':>9}")
    print("-" * 72)
    evaluate(nav, pd.Series(0.0, index=days), pd.Series(0.0, index=days), "核心（微盘+广度+全天候）")

    if args.sweep_hold:
        for h in (20, 30, 40, 50, 60, 70, 80, 90, 120, 243):
            w, c = sat2(days, gs, lab, cols, hold=h, cap=args.cap, filters=filters,
                        dynamic=not args.no_dynamic)
            evaluate(nav, w, c, f" ② 持 {h} 日")
        return 0

    if args.sweep_cap:
        for cp in (0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50):
            w, c = sat2(days, gs, lab, cols, hold=args.hold, cap=cp, filters=filters,
                        dynamic=not args.no_dynamic)
            evaluate(nav, w, c, f" ② 上限 {cp:.0%}")
        return 0

    want = [args.only] if args.only else [1, 2, 3]
    if 1 in want:
        w, c = sat1(days, close)
        evaluate(nav, w, c, "+ ① 恐慌反弹 10%")
    if 2 in want:
        w, c = sat2(days, gs, lab, cols, hold=args.hold, cap=args.cap,
                    filters=filters, dynamic=not args.no_dynamic)
        evaluate(nav, w, c, f"+ ② 行业击球（{args.hold}日/{args.cap:.0%}/{args.filter or '无过滤'}）")
    if 3 in want:
        w, c = sat3(days)
        evaluate(nav, w, c, "+ ③ 深跌反弹 10%（已否决）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
