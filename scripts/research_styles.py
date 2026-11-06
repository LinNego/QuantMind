#!/usr/bin/env python3
"""风格选股对照回测 —— 不用指数，直接用全市场面板选股。

和 `research_takeprofit.py` 用同一份面板缓存（每期全市场约 4,000 只），
区别是：这里**不选微盘**，而是按不同风格因子选股，再比较：

    ① 各自的风险收益（年化 / 回撤 / 夏普）
    ② 与「微盘 15 只」的月度相关性
    ③ **下行相关性** —— 微盘下跌的月份里，它还跌不跌
    ④ 微盘最差 20% 月份里，它的平均表现

相关性才是决定「能不能分散」的东西。全期相关性会骗人，
真正决定你活不活得下来的是「你最需要它的时候，它涨不涨」。

用法：
    python scripts/research_styles.py --build     # 首次重建面板缓存
    python scripts/research_styles.py             # 默认 topk=30
    python scripts/research_styles.py --topk 50
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
DATA_DIR = Path("/opt/quantmind/data/quantdb")
CACHE = Path("/tmp/lp_panel.parquet")
COST = 0.00135          # 单边费率（和手册 CLI 对齐）
MONTH_COST = COST * 2   # 每期近似一次完整换手


def load_panel(force: bool) -> pd.DataFrame:
    if force or not CACHE.exists():
        spec = importlib.util.spec_from_file_location(
            "rd", HERE / "research_lowprice_drawdown.py"
        )
        rd = importlib.util.module_from_spec(spec)
        sys.modules["rd"] = rd
        spec.loader.exec_module(rd)
        rd.build_cache(DATA_DIR, CACHE)
    return pd.read_parquet(CACHE)


def prepare(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["is_st"] = pd.to_numeric(df.is_st, errors="coerce").fillna(0)
    df["is_quit_risk"] = pd.to_numeric(df.is_quit_risk, errors="coerce").fillna(0)
    df = df.sort_values(["symbol", "dt"])
    # 成长：营收 TTM 同比（约 12 个期末）
    df["rev_yoy"] = df.groupby("symbol").revenue_ttm.pct_change(12)
    # 质量：ROE(TTM)
    df["roe"] = df.net_profit_ttm / df.equity.replace(0, np.nan)
    # 价值：盈利收益率（只认 PE 0~500 的正常区间）
    df["ep"] = np.where((df.pe_ttm > 0) & (df.pe_ttm < 500), 1.0 / df.pe_ttm, np.nan)

    # ---- 边界约束：不裁掉尾部，排名会选中垃圾 ----
    # 低波会选到「停牌不动」的票（vol 0.007），动量会选到异常复权的假涨幅（gap −72）
    def clip(col, lo, hi):
        return pd.to_numeric(df[col], errors="coerce").where(
            pd.to_numeric(df[col], errors="coerce").between(lo, hi))

    df["vol_std_20"] = clip("vol_std_20", 0.2, 20)
    df["beta_20"] = clip("beta_20", 0.0, 3.0)
    df["ma_gap_20"] = clip("ma_gap_20", -0.5, 2.0)
    df["dividend_rate"] = clip("dividend_rate", 0.0, 0.30)
    df["roe"] = clip("roe", -1.0, 1.0)
    df["rev_yoy"] = clip("rev_yoy", -0.9, 5.0)
    return df


def backtest(df, score, asc, topk, base_mask):
    per = []
    for d, g in df.groupby("dt", sort=True):
        s = g[base_mask(g) & g.fwd_ret.notna() & g[score].notna()]
        s = s.sort_values(score, ascending=asc).head(topk)
        per.append((d, s.fwd_ret.mean() if len(s) else np.nan))
    r = pd.Series(dict(per)).dropna().sort_index()
    return r, (1 + r - MONTH_COST).cumprod()


def metrics(r, nav, label):
    years = len(r) / 12
    total = nav.iloc[-1] - 1
    ann = (1 + total) ** (1 / years) - 1 if years > 0 else np.nan
    dd = (nav / nav.cummax() - 1).min()
    vol = r.std() * np.sqrt(12)
    return {
        "label": label,
        "annual": round(ann * 100, 2),
        "mdd": round(dd * 100, 2),
        "sharpe": round(ann / vol, 2) if vol > 0 else np.nan,
        "final": round(nav.iloc[-1], 2),
        "ret": r,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--topk", type=int, default=30)
    ap.add_argument("--start", default="20170101")
    args = ap.parse_args()

    panel = load_panel(args.build)
    df = prepare(panel)
    df = df[df.dt >= int(args.start)]

    inv = lambda g: (
        (g.is_st == 0) & (g.is_quit_risk == 0) & (g.listed_days >= 120)
        & (g.amount_ma_5 >= 2000) & (g.total_mv >= 1e9)
    )
    micro = lambda g: inv(g) & (g.px >= 1.5) & (g.px <= 10) & (g.total_mv <= 5e9)

    k = args.topk
    STYLES = [
        ("微盘15（你的策略）", "total_mv", True, 15, micro),
        (f"微盘{k}",            "total_mv", True, k,  inv),
        ("红利（股息率）",       "dividend_rate", False, k, inv),
        ("价值（1/PE）",         "ep", False, k, inv),
        ("低波（20日波动）",     "vol_std_20", True, k, inv),
        ("低Beta",              "beta_20", True, k, inv),
        ("动量（20日偏离）",     "ma_gap_20", False, k, inv),
        ("反转（RSI14 低）",     "rsi_14", True, k, inv),
        ("质量（ROE）",          "roe", False, k, inv),
        ("成长（营收同比）",     "rev_yoy", False, k, inv),
    ]

    res = []
    for name, col, asc, kk, mask in STYLES:
        r, nav = backtest(df, col, asc, kk, mask)
        res.append(metrics(r, nav, name))

    bench = df[inv(df) & df.fwd_ret.notna()].groupby("dt").fwd_ret.mean()
    res.append(metrics(bench, (1 + bench - MONTH_COST).cumprod(), "全市场等权"))

    base = res[0]["ret"]
    down = base < 0
    worst = base <= base.quantile(0.20)

    print(f"\n样本 {len(base)} 个月（{base.index[0]} ~ {base.index[-1]}）"
          f"｜微盘下跌 {down.sum()} 期｜最差 20% 共 {worst.sum()} 期")
    print(f"每期扣成本 {MONTH_COST*100:.2f}%\n")
    print(f"{'风格':<20}{'年化':>8}{'回撤':>9}{'夏普':>7}{'末值':>7}"
          f"{'与微盘相关':>10}{'下行相关':>9}{'微盘最差20%月均':>15}")
    print("-" * 92)
    for x in res:
        r = x["ret"].reindex(base.index)
        c1 = r.corr(base)
        c2 = r[down].corr(base[down])
        w = r[worst].mean() * 100
        print(f"{x['label']:<20}{x['annual']:>7.2f}%{x['mdd']:>8.2f}%{x['sharpe']:>7.2f}"
              f"{x['final']:>7.2f}{c1:>10.2f}{c2:>9.2f}{w:>14.2f}%")

    print("\n【月度相关矩阵】")
    names = [x["label"] for x in res]
    mat = pd.DataFrame({x["label"]: x["ret"].reindex(base.index) for x in res}).corr()
    print(mat.round(2).to_string())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
