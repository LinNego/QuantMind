#!/usr/bin/env python3
"""D03 玩具回测：四种跑法并列，让你亲眼看见「哪里在骗自己」

目的**不是**找一个好策略，是让同一条信号在四种实现下跑出四个不同结果：

    ① naive        每天换仓 + 当天收盘价成交   ← 未来函数，曲线好得离谱
    ② fixed        每 20 日换仓 + 次日收益      ← 时序对了
    ③ fixed+cost   再加真实成本（佣金/印花税/过户费/滑点）
    ④ fixed+cost3  成本翻到 0.3%/边（保守假设）

看 ①→② 掉多少，是"时序"的价值；②→③ 掉多少，是"成本"的价值。

用法（仓库根目录）：
    python research/stage1/toy_backtest.py
    python research/stage1/toy_backtest.py --topk 50 --rebal 40

产出：
    research/out/tbl/toy_backtest_metrics.csv
    research/out/fig/toy_nav.png        （装不了 matplotlib 就跳过）

⚠️ 三条禁令：不看收益高低 / 不下结论 / 不调参数。
   这个脚本的结果不可信，它只是让你看见管道。
"""

from __future__ import annotations

import os

import argparse
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]          # 仓库根
OUT_TBL = ROOT / "research" / "out" / "tbl"
OUT_FIG = ROOT / "research" / "out" / "fig"
DATA_DIR = os.getenv("QM_QUANTDB_DATA_DIR", "/opt/quantmind/data/quantdb")
DATA = f"{DATA_DIR}/1_kline_data/daily_forward/*/data.parquet"

# 成本拆分（详见 docs/回测费用配置说明.md）
COMMISSION = 0.00025   # 佣金 万 2.5，双边
TRANSFER = 0.00001     # 过户费 万 0.1，双边
STAMP = 0.0005         # 印花税 万 5，仅卖出
SLIPPAGE = 0.001       # 滑点 0.1%/边（撮合器默认 5bp，这里更保守）

BUY_COST = COMMISSION + TRANSFER + SLIPPAGE                 # 0.126%
SELL_COST = COMMISSION + TRANSFER + STAMP + SLIPPAGE        # 0.176%


def load_close(start: int, end: int) -> pd.DataFrame:
    """长表 → 日期×股票 的宽表（前复权）。"""
    con = duckdb.connect()
    con.execute("SET threads=8")
    df = con.execute(
        f"""
        SELECT dt, symbol, close FROM read_parquet('{DATA}', hive_partitioning = true)
        WHERE dt BETWEEN {start} AND {end}
        """
    ).df()
    wide = df.pivot(index="dt", columns="symbol", values="close").sort_index()
    print(f"宽表：{wide.shape[0]} 个交易日 × {wide.shape[1]} 只股票")
    return wide


def make_weights(
    close: pd.DataFrame, topk: int, rebal: int | None, factor: str = "momentum"
) -> pd.DataFrame:
    """20 日因子 → 等权持仓权重。rebal=None 表示每天换仓。

    factor="momentum"（默认）：买过去 20 日涨得多的。
        未来函数版会"看起来很赚"，这是最经典的假回测形态。
    factor="reversal"：买过去 20 日跌得多的。
        未来函数版反而"看起来很亏"，因为是在它当天正在跌的时候买进去。
        两种都是假的，只是方向不同，值得亲自跑一遍看。
    """
    mom = close / close.shift(20) - 1
    score = mom if factor == "momentum" else -mom
    top = score.rank(axis=1, ascending=False) <= topk      # 当天分数最高的 topk 只

    w = top.astype(float)
    w = w.div(w.sum(axis=1).replace(0, np.nan), axis=0).fillna(0.0)

    if rebal:
        idx = close.index
        rebal_days = idx[::rebal]                          # 每 rebal 个交易日调一次
        mask = pd.Series(False, index=idx)
        mask.loc[rebal_days] = True
        w = w.where(mask).ffill().fillna(0.0)              # 中间的日保持仓位不动
    return w


def run(w: pd.DataFrame, ret: pd.DataFrame, buy: float, sell: float, use_shift: bool) -> dict:
    """给定权重与收益，返回净值与指标。use_shift=False 就是未来函数版。"""
    w_eff = w.shift(1).fillna(0.0) if use_shift else w       # 当天持有的仓位
    gross = (w_eff * ret).sum(axis=1, min_count=1).fillna(0.0)

    turnover = 0.5 * (w_eff - w_eff.shift(1)).abs().sum(axis=1)   # 单边换手
    cost = turnover * (buy + sell)
    net = gross - cost

    nav = (1 + net).cumprod()
    days = len(nav)
    ann = nav.iloc[-1] ** (252 / days) - 1 if nav.iloc[-1] > 0 else -1.0
    vol = net.std() * np.sqrt(252)
    dd = (nav / nav.cummax() - 1).min()
    monthly = nav.groupby(nav.index // 100).last().pct_change().dropna()  # 按年月收盘
    return {
        "年化": ann,
        "年化波动": vol,
        "夏普": ann / vol if vol else np.nan,
        "最大回撤": dd,
        "卡玛": ann / abs(dd) if dd else np.nan,
        "月胜率": (monthly > 0).mean() if len(monthly) else np.nan,
        "年换手": turnover.sum() / (days / 252),
        "最终净值": nav.iloc[-1],
        "nav": nav,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--topk", type=int, default=30)
    ap.add_argument("--rebal", type=int, default=20)
    ap.add_argument("--factor", choices=["momentum", "reversal"], default="momentum")
    ap.add_argument("--start", type=int, default=20160101)
    ap.add_argument("--end", type=int, default=20241231, help="2025 以后留作 holdout，别动")
    args = ap.parse_args()

    close = load_close(args.start, args.end)
    ret = close.pct_change(fill_method=None).fillna(0.0)   # 停牌/新股首日记 0 收益（简化）

    w_daily = make_weights(close, args.topk, None, args.factor)
    w_rebal = make_weights(close, args.topk, args.rebal, args.factor)

    runs = {
        "① naive 每天换+当天成交": run(w_daily, ret, 0.0, 0.0, use_shift=False),
        "② fixed 每20日换+次日": run(w_rebal, ret, 0.0, 0.0, use_shift=True),
        "③ fixed + 真实成本": run(w_rebal, ret, BUY_COST, SELL_COST, use_shift=True),
        "④ fixed + 成本×1.7": run(w_rebal, ret, BUY_COST * 1.7, SELL_COST * 1.7, use_shift=True),
    }

    tbl = pd.DataFrame({k: {x: y for x, y in v.items() if x != "nav"} for k, v in runs.items()}).T
    show = tbl.copy()
    for c in ["年化", "年化波动", "最大回撤", "月胜率"]:
        show[c] = (show[c] * 100).round(2).astype(str) + "%"
    for c in ["夏普", "卡玛", "年换手", "最终净值"]:
        show[c] = show[c].round(2)

    print("\n" + "=" * 78)
    print(f"20日{args.factor} · top{args.topk} · 每{args.rebal}日换仓 · {args.start}~{args.end}")
    print("=" * 78)
    print(show.to_string())
    print("\n单边成本：买 %.3f%% ｜ 卖 %.3f%%" % (BUY_COST * 100, SELL_COST * 100))
    print("\n怎么读这张表：")
    print("  ①→② 的落差 = 未来函数的价值（这部分收益在实盘一分钱都拿不到）")
    print("  ②→③ 的落差 = 成本的价值（这才是你真正会遇到的衰减）")
    print("  ③ 和 ④ 差不多 = 成本假设不敏感；差很多 = 你对滑点的估计很关键")

    OUT_TBL.mkdir(parents=True, exist_ok=True)
    tbl.to_csv(OUT_TBL / "toy_backtest_metrics.csv", encoding="utf-8-sig")
    print(f"\n指标已存：{OUT_TBL / 'toy_backtest_metrics.csv'}")

    # 每年收益（只看 ③，训练阶段用来发现"只在某一年好"）
    net3 = runs["③ fixed + 真实成本"]["nav"].pct_change(fill_method=None).fillna(0.0)
    yearly = (1 + net3).groupby(net3.index // 10000).prod() - 1
    print("\n③ 的分年收益：")
    for yr, v in yearly.items():
        bar = "█" * int(abs(v) * 50)
        print(f"  {yr}  {v * 100:>7.1f}%  {bar}")

    # 画图（matplotlib 装不上就跳过，不影响上面的结果）
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        OUT_FIG.mkdir(parents=True, exist_ok=True)
        fig, ax = plt.subplots(figsize=(11, 5.5))
        for k, v in runs.items():
            ax.plot(v["nav"].index.astype(str), v["nav"].values, label=k, lw=1.4)
        ax.set_yscale("log")
        ax.set_title("Toy backtest: same signal, four implementations (log scale)")
        ax.set_ylabel("NAV (log)")
        ax.legend(fontsize=9)
        ax.grid(alpha=0.3)
        for t in ax.get_xticklabels()[::max(1, len(ax.get_xticklabels()) // 12)]:
            t.set_rotation(45)
        fig.tight_layout()
        fig.savefig(OUT_FIG / "toy_nav.png", dpi=130)
        print(f"净值曲线已存：{OUT_FIG / 'toy_nav.png'}")
    except Exception as e:                                  # noqa: BLE001
        print(f"跳过画图（{type(e).__name__}: {e}）—— 指标不受影响")

    return 0


if __name__ == "__main__":
    sys.exit(main())
