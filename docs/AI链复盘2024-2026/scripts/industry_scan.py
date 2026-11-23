"""跨行业景气/估值/筹码扫描：用同一把尺子量所有行业。"""
import glob
import os
import sys
import time

import numpy as np
import pandas as pd

QDB = "/home/quantmind/quantdb"
DST = "/data/ljc/vigap/quant/data"
CUR, PREV = "20260630", "20250630"


def load_income():
    rows = []
    files = glob.glob(f"{QDB}/3_financial_data/income/*.parquet")
    for i, f in enumerate(files):
        try:
            d = pd.read_parquet(f, columns=["m_timetag", "revenue", "net_profit_excl_min_int_inc",
                                            "oper_profit", "Symbol"])
        except Exception:
            continue
        d = d[d.m_timetag.isin([CUR, PREV])]
        if len(d) < 2:
            continue
        cur = d[d.m_timetag == CUR].iloc[-1]
        prev = d[d.m_timetag == PREV].iloc[-1]
        rows.append({"symbol": cur.Symbol, "rev": cur.revenue, "rev0": prev.revenue,
                     "np": cur.net_profit_excl_min_int_inc, "np0": prev.net_profit_excl_min_int_inc,
                     "op": cur.oper_profit, "op0": prev.oper_profit})
        if i % 1500 == 0:
            print("  income", i, flush=True)
    return pd.DataFrame(rows)


def monthly_valuation(inst):
    files = sorted(glob.glob(f"{QDB}/5_technical_derived/valuation/dt=*/data.parquet"))
    keep = []
    seen = set()
    for f in files:
        d = f.split("dt=")[1][:8]
        if d < "20200101" or d > "20260930":
            continue
        ym = d[:6]
        keep.append((ym, f))
    # 每月取最后一个交易日
    last = {}
    for ym, f in keep:
        last[ym] = f
    out = []
    for ym, f in sorted(last.items()):
        v = pd.read_parquet(f, columns=["symbol", "pe_ttm", "pb", "total_mv", "net_profit_ttm"])
        v = v.merge(inst[["Symbol", "rs_hyname"]], left_on="symbol", right_on="Symbol", how="inner")
        pe = v.pe_ttm.where((v.pe_ttm > 0) & (v.pe_ttm < 300))
        g = v.assign(pe=pe).groupby("rs_hyname").agg(
            pe_med=("pe", "median"), pb_med=("pb", lambda s: s[(s > 0)].median()),
            mv=("total_mv", "sum"), n=("symbol", "size"))
        g["ym"] = ym
        out.append(g.reset_index())
    return pd.concat(out, ignore_index=True)


def main():
    t = time.time()
    inst = pd.read_parquet(f"{QDB}/2_base_sector/instrument_detail/instrument_list.parquet",
                           columns=["Symbol", "Name", "rs_hyname", "J_start", "IsSTGP", "IsQuitGP"])
    print("inst", len(inst), f"{time.time()-t:.0f}s", flush=True)

    inc = load_income()
    print("income done", inc.shape, f"{time.time()-t:.0f}s", flush=True)
    inc = inc.merge(inst, left_on="symbol", right_on="Symbol", how="left")
    inc["rev_yoy"] = inc.rev / inc.rev0 - 1
    inc["np_yoy"] = inc.np / inc.np0 - 1
    inc["opm"] = inc.op / inc.rev
    inc["opm0"] = inc.op0 / inc.rev0
    inc["opm_chg"] = (inc.opm - inc.opm0) * 100
    inc.loc[(inc.rev0 <= 0) | (inc.rev0.isna()), "rev_yoy"] = np.nan
    inc.to_parquet(f"{DST}/stock_fin.parquet", index=False)
    print("fin yoy median", round(inc.rev_yoy.median(), 3), f"{time.time()-t:.0f}s", flush=True)

    mv = monthly_valuation(inst)
    mv.to_parquet(f"{DST}/ind_valuation.parquet", index=False)
    print("valuation panel", mv.shape, f"{time.time()-t:.0f}s", flush=True)

    # 成交占比（近一年）与价格位置
    px = pd.read_parquet(f"{DST}/px.parquet")
    px = px.merge(inst[["Symbol", "rs_hyname"]], left_on="symbol", right_on="Symbol", how="left")
    last_day = px.time.max()
    year = px[px.time >= last_day - pd.Timedelta(days=365)]
    share = year.groupby(["rs_hyname", "time"]).amount.sum().reset_index()
    tot = year.groupby("time").amount.sum()
    share["share"] = share.amount / share.time.map(tot)
    cur_share = share[share.time == last_day].set_index("rs_hyname")["share"]
    # 价格位置：行业等权指数距近一年高点
    idx = year.groupby(["rs_hyname", "time"]).apply(
        lambda d: (1 + d.close.pct_change().fillna(0)).prod(), include_groups=False).reset_index(name="pv")
    pos = idx.groupby("rs_hyname").pv.apply(lambda s: s.iloc[-1] / s.max() - 1)
    out = pd.DataFrame({"share_now": cur_share, "share_pct": share.groupby("rs_hyname").share.mean(),
                        "pos": pos}).reset_index()
    out = out.merge(inc.groupby("rs_hyname").agg(
        rev_yoy=("rev_yoy", "median"), np_yoy=("np_yoy", "median"),
        opm_chg=("opm_chg", "median"), n=("symbol", "size")), on="rs_hyname", how="left")
    latest = mv[mv.ym == mv.ym.max()].set_index("rs_hyname")
    out = out.merge(latest[["pe_med", "pb_med", "mv"]], left_on="rs_hyname", right_index=True, how="left")
    # 估值分位
    pct = {}
    for ind, g in mv.groupby("rs_hyname"):
        s = g.sort_values("ym").pe_med.dropna()
        if len(s) > 24:
            pct[ind] = (s.iloc[:-1] < s.iloc[-1]).mean()
    out["pe_pct"] = out.rs_hyname.map(pct)
    out.to_parquet(f"{DST}/industry_panel.parquet", index=False)
    print("industry panel", out.shape, f"{time.time()-t:.0f}s")
    print(out.sort_values("rev_yoy", ascending=False).head(20).to_string(index=False))


if __name__ == "__main__":
    main()
