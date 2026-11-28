#!/usr/bin/env python3
"""行业击球清单：回测 + 当前筛选

思路（对应 docs/个股分析 里的 8-10 分制）：
  在 10 个条件里，只有 3 个能用历史数据回测：
    ① PB 位置  = 行业中位数 PB 在过去 36 个月的分位   < 20%
    ② 股价位置 = 行业等权指数在过去 12 个月的分位     < 25%
    ③ 还在亏   = 行业净利润 TTM 合计 < 0
  其余 7 个（库存、产能退出、需求、成本曲线）缺历史数据，无法回测。

结论（2016-2026，111 个行业）：
  基准 12 个月 +10.7%
  ①+②        +12.4%（n=1590，胜率 69%）
  ①+②+③     +24.8%（n=58， 胜率 79%）  ← 边际全在"还在亏"这一条
  严格 15%/20% +27.5%（n=42， 胜率 79%）
  注意：n=58 很小且在时间上聚集，过拟合风险高；2023 年是唯一大亏年（-12.9%）。

用法：
  python scripts/research_industry_checklist.py            # 回测 + 当前清单
  python scripts/research_industry_checklist.py --screen   # 只出当前清单
"""
from __future__ import annotations

import argparse
import glob
import os
from pathlib import Path

import numpy as np
import pandas as pd

DB = Path("/opt/quantmind/data/quantdb")
VAL = DB / "5_technical_derived" / "valuation"
INST = DB / "2_base_sector" / "instrument_detail" / "instrument_list.parquet"
REF = Path(__file__).resolve().parent.parent / "refdata"
BIG = Path(__file__).resolve().parent.parent / "refdata_big"
CACHE = REF / "val_month.parquet"

# 阈值
PB_MAX = 0.20      # PB 36 个月分位
PX_MAX = 0.25      # 股价 12 个月分位
MIN_STOCKS = 10    # 行业至少这么多只


def load_val(force: bool = False) -> pd.DataFrame:
    """把每日 valuation 快照压成月末面板（约 1 分钟）。"""
    if CACHE.exists() and not force:
        return pd.read_parquet(CACHE)
    by_m: dict[str, str] = {}
    for f in sorted(glob.glob(str(VAL / "dt=*"))):
        by_m[os.path.basename(f)[3:9]] = f          # 每月保留最后一个交易日
    rows = []
    for f in (by_m[m] for m in sorted(by_m)):
        d = pd.read_parquet(f, columns=["symbol", "pb", "net_profit_ttm", "total_mv"])
        d["dt"] = pd.to_datetime(os.path.basename(f)[3:11])
        rows.append(d)
    out = pd.concat(rows, ignore_index=True)
    out.to_parquet(CACHE)
    return out


def industry_labels() -> pd.Series:
    ins = pd.read_parquet(INST)
    lab = ins.set_index("Symbol")["rs_hyname"].astype(str)
    cnt = lab.value_counts()
    return lab[lab.isin(cnt[cnt >= MIN_STOCKS].index)]


def industry_index(close: pd.DataFrame, lab: pd.Series) -> pd.DataFrame:
    """等权收益累乘出的行业指数（不要用平均股价，会被拼股/新股带偏）。"""
    cols = [c for c in close.columns if c in lab.index]
    mr = close[cols].resample("ME").last().pct_change()
    ind_ret = mr.T.groupby(lab.reindex(cols)).mean().T
    return (1 + ind_ret.fillna(0)).cumprod()


def _pos(s: pd.Series, w: int) -> pd.Series:
    mn = s.rolling(w, min_periods=int(w * 0.6)).min()
    mx = s.rolling(w, min_periods=int(w * 0.6)).max()
    return (s - mn) / (mx - mn).replace(0, np.nan)


def build_panel() -> tuple[pd.DataFrame, pd.DatetimeIndex]:
    val = load_val()
    lab = industry_labels()
    val = val[val.symbol.isin(lab.index)].copy()
    val["ind"] = val.symbol.map(lab)

    agg = (
        val.groupby(["dt", "ind"])
        .agg(pb=("pb", "median"), np_ttm=("net_profit_ttm", "sum"))
        .reset_index()
        .sort_values(["ind", "dt"])
    )

    price = next((p for p in (BIG / "close_daily_backward.parquet",
                              REF / "close_daily_backward.parquet",
                              Path("/tmp/close_daily_backward.parquet")) if p.exists()), None)
    if price is None:
        raise SystemExit("缺少价格面板：先跑 python scripts/build_price_panel.py")
    close = pd.read_parquet(price).sort_index()
    close.index = pd.to_datetime(close.index)
    ind_px = industry_index(close, lab)

    px = ind_px.stack().rename("px").reset_index()
    px.columns = ["dt", "ind", "px"]
    agg = agg.merge(px, on=["dt", "ind"], how="left").sort_values(["ind", "dt"])

    agg["pb_pos"] = agg.groupby("ind")["pb"].transform(lambda s: _pos(s, 36))
    agg["px_pos"] = agg.groupby("ind")["px"].transform(lambda s: _pos(s, 12))

    for h in (6, 12):
        fwd = (ind_px.shift(-h) / ind_px - 1).stack().rename(f"f{h}").reset_index()
        fwd.columns = ["dt", "ind", f"f{h}"]
        agg = agg.merge(fwd, on=["dt", "ind"], how="left")
    return agg, ind_px.index


def backtest(panel: pd.DataFrame) -> None:
    g = panel.dropna(subset=["pb_pos", "px_pos", "np_ttm", "f12"])
    base12, win12 = g.f12.mean(), (g.f12 > 0).mean()
    print(f"基准 12 个月 {base12:+.2%}（胜率 {win12:.0%}，n={len(g)}）\n")
    print(f"{'条件':<26}{'n':>6}{'6月':>9}{'12月':>9}{'胜率':>7}")
    print("-" * 57)
    rules = [
        ("①+②（PB低、股价低）", (g.pb_pos < PB_MAX) & (g.px_pos < PX_MAX)),
        ("①+②+③（加上还在亏）", (g.pb_pos < PB_MAX) & (g.px_pos < PX_MAX) & (g.np_ttm < 0)),
        ("严格 15%/20%/亏", (g.pb_pos < 0.15) & (g.px_pos < 0.20) & (g.np_ttm < 0)),
    ]
    for name, mask in rules:
        t = g[mask]
        if len(t) < 20:
            print(f"{name:<26}{len(t):>6}   样本太少")
            continue
        print(f"{name:<26}{len(t):>6}{t.f6.mean():>+8.2%}{t.f12.mean():>+9.2%}{(t.f12 > 0).mean():>7.0%}")
    t = g[(g.pb_pos < PB_MAX) & (g.px_pos < PX_MAX) & (g.np_ttm < 0)]
    if len(t) > 30:
        yr = t.assign(yr=t.dt.dt.year).groupby("yr").agg(
            n=("f12", "size"), 收益=("f12", "mean"), 胜率=("f12", lambda s: (s > 0).mean())
        )
        print("\n分年（12 个月）：")
        print((yr * [1, 100, 100]).round(1).to_string())


def screen(panel: pd.DataFrame) -> None:
    last_dt = panel.dt.max()
    last = panel[panel.dt == last_dt].dropna(subset=["pb_pos", "px_pos", "np_ttm"])
    score = (
        (last.pb_pos < PB_MAX).astype(int)
        + (last.px_pos < PX_MAX).astype(int)
        + (last.np_ttm < 0).astype(int)
    )
    last = last.assign(score=score)
    print(f"\n快照 {last_dt.date()}（行业 {len(last)} 个，阈值 PB<{PB_MAX:.0%} / 股价<{PX_MAX:.0%} / 净利<0）")
    hit = last[last.score == 3].sort_values("pb_pos")
    print(f"\n=== 三项全中：{len(hit)} 个 ===")
    if len(hit):
        print(
            hit[["ind", "pb", "pb_pos", "px_pos", "np_ttm"]]
            .assign(PB位置=lambda d: d.pb_pos.round(2), 股价位置=lambda d: d.px_pos.round(2),
                    净利亿=lambda d: (d.np_ttm / 1e8).round(1))
            .drop(columns=["pb_pos", "px_pos", "np_ttm"])
            .to_string(index=False)
        )
    else:
        print("  无")
    two = last[last.score == 2].nsmallest(15, "px_pos")
    print(f"\n=== 两项命中（共 {int((last.score == 2).sum())} 个，按股价位置取前 15） ===")
    print(
        two[["ind", "pb", "pb_pos", "px_pos", "np_ttm"]]
        .assign(PB位置=lambda d: d.pb_pos.round(2), 股价位置=lambda d: d.px_pos.round(2),
                净利亿=lambda d: (d.np_ttm / 1e8).round(0))
        .drop(columns=["pb_pos", "px_pos", "np_ttm"])
        .to_string(index=False)
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--screen", action="store_true", help="只出当前清单，不回测")
    ap.add_argument("--rebuild", action="store_true", help="强制重建 valuation 月末缓存")
    args = ap.parse_args()

    if args.rebuild:
        load_val(force=True)
    panel, _ = build_panel()
    if not args.screen:
        backtest(panel)
    screen(panel)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
