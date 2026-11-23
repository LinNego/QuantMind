"""A 股宽基指数「深跌分批买入 + 分批止盈」回测。

口径
----
- 标的：国内宽基指数（不含跨境/港股），数据来自 QuantDB index_daily。
- 信号：5 年（1250 交易日）滚动最高收盘价，dd = close / roll_max - 1。
- 建仓（ladder）：dd <= -40% 投 50%，<= -50% 投 30%，<= -60% 投 20%。
                 （full40：dd <= -40% 一次性投满）
- 止盈（tp）：浮盈 +30% 卖 1/3，+50% 再卖剩余的一半；剩余仓位在
             「收盘跌破 20 月均线」或「自持仓最高点回撤 25%」时清仓，
             且趋势止盈只在浮盈超过 15% 后才武装。
- 不止盈（hold）：建仓后一直持有到数据末尾。
- 费用：单边 5bp（佣金 + 滑点），ETF 无印花税。
- 现金：年化 2%，按自然日计息。
- 全部清仓后需要回撤修复到 -20% 以内，才允许开启下一轮。

用法
----
    python scripts/research/broad_index_drawdown_tp.py [--rf 0.02]
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

DATA_DIR = Path("data/quantdb/1_kline_data/index_daily")
OUT_DIR = Path("data/reports/research")

INDEXES = {
    "000300.SH": "沪深300",
    "000905.SH": "中证500",
    "000852.SH": "中证1000",
    "399006.SZ": "创业板指",
    "000016.SH": "上证50",
    "000688.SH": "科创50",
    "899050.BJ": "北证50",
    "399303.SZ": "国证2000",
    "399330.SZ": "深证100",
    "000906.SH": "中证800",
}

LADDER = ((-0.40, 0.50), (-0.50, 0.30), (-0.60, 0.20))
FULL40 = ((-0.40, 1.00),)
TP_LADDER = ((0.30, 1 / 3), (0.50, 0.50))
TRAIL_MA = 420
TRAIL_DD = 0.25
REARM_DD = -0.20
TRAIL_ARM_GAIN = 0.15
FEE = 0.0005

CONFIGS = {
    "A_full40_tp": ("跌40%满仓 + 止盈30/50", FULL40, "tp"),
    "B_ladder_tp": ("三档-40/50/60 + 止盈30/50", LADDER, "tp"),
    "C_full40_hold": ("跌40%满仓 + 不止盈", FULL40, "hold"),
    "D_ladder_hold": ("三档-40/50/60 + 不止盈", LADDER, "hold"),
}

_CACHE: dict[str, pd.DataFrame] = {}


def _load_all() -> pd.DataFrame:
    if "all" not in _CACHE:
        frames = []
        for f in sorted(DATA_DIR.glob("dt=*")):
            p = f / "data.parquet"
            if p.exists():
                frames.append(pd.read_parquet(p, columns=["symbol", "time", "close"]))
        _CACHE["all"] = pd.concat(frames, ignore_index=True)
    return _CACHE["all"]


def load_index(symbol: str) -> pd.Series:
    df = _load_all()
    return (
        df[df["symbol"] == symbol]
        .assign(time=lambda x: pd.to_datetime(x["time"]))
        .sort_values("time")
        .set_index("time")["close"]
        .dropna()
    )


def run_index(s: pd.Series, rf: float, ladder, exit_mode: str) -> dict:
    close = s.values.astype(float)
    dates = s.index
    n = len(close)

    roll_max = s.rolling(1250, min_periods=1).max().values
    ma = s.rolling(TRAIL_MA, min_periods=TRAIL_MA).mean().values
    dd = close / roll_max - 1.0

    cash, shares, avg_cost, peak_since = 1.0, 0.0, 0.0, 0.0
    entry_done = [False] * len(ladder)
    tp_done = [False] * len(TP_LADDER)
    armed, trail_armed = True, False
    episode_in = episode_out = 0.0
    trades: list[float] = []
    equity = np.empty(n)
    days_in = 0

    for i in range(n):
        p = close[i]
        if i > 0:
            cash *= (1.0 + rf) ** ((dates[i] - dates[i - 1]).days / 365.0)

        if shares == 0.0 and dd[i] > REARM_DD:
            armed = True

        entered_today = False
        if armed:
            for k, (lvl, w) in enumerate(ladder):
                if entry_done[k] or dd[i] > lvl:
                    continue
                amt = min(w * 1.0, cash)
                if amt <= 1e-9:
                    continue
                bought = (amt * (1.0 - FEE)) / p
                shares += bought
                avg_cost = (avg_cost * (shares - bought) + amt) / shares
                cash -= amt
                episode_in += amt
                entry_done[k] = True
                entered_today = True
                peak_since = max(peak_since, p)

        if shares > 1e-12 and not entered_today:
            peak_since = max(peak_since, p)

            if exit_mode == "tp":
                for k, (gain, frac) in enumerate(TP_LADDER):
                    if tp_done[k] or p < avg_cost * (1.0 + gain):
                        continue
                    gross = shares * frac * p
                    cash += gross * (1.0 - FEE)
                    episode_out += gross * (1.0 - FEE)
                    shares *= 1.0 - frac
                    tp_done[k] = True

                if peak_since >= avg_cost * (1.0 + TRAIL_ARM_GAIN):
                    trail_armed = True

                if shares > 1e-12 and trail_armed:
                    hit = ((not np.isnan(ma[i])) and p < ma[i]) or (
                        p < peak_since * (1.0 - TRAIL_DD)
                    )
                    if hit:
                        gross = shares * p
                        cash += gross * (1.0 - FEE)
                        episode_out += gross * (1.0 - FEE)
                        trades.append(episode_out / episode_in - 1.0)
                        shares, avg_cost, peak_since = 0.0, 0.0, 0.0
                        entry_done = [False] * len(ladder)
                        tp_done = [False] * len(TP_LADDER)
                        episode_in = episode_out = 0.0
                        trail_armed, armed = False, False

        equity[i] = cash + shares * p
        if shares > 0.0:
            days_in += 1

    if shares > 1e-12:  # 未平仓，按市值记一笔未实现收益
        trades.append((episode_out + shares * close[-1]) / episode_in - 1.0)

    return {
        "equity": pd.Series(equity, index=dates),
        "trades": trades,
        "days_in": days_in,
        "n_days": n,
    }


def metrics(eq: pd.Series, rf: float) -> dict:
    ret = eq.pct_change().dropna()
    years = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1
    vol = ret.std() * np.sqrt(252)
    mdd = (eq / eq.cummax() - 1).min()
    return {
        "cagr": cagr,
        "vol": vol,
        "sharpe": (cagr - rf) / vol if vol > 0 else np.nan,
        "mdd": mdd,
        "calmar": cagr / abs(mdd) if mdd < 0 else np.nan,
    }


def equal_weight_benchmark(
    series: dict[str, pd.Series], index: pd.DatetimeIndex
) -> pd.Series:
    rets = [s.pct_change() for s in series.values()]
    df = pd.concat(rets, axis=1).mean(axis=1, skipna=True)
    eq = (1 + df.fillna(0)).cumprod().reindex(index).ffill()
    return eq / eq.dropna().iloc[0]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rf", type=float, default=0.02, help="现金年化收益率")
    args = ap.parse_args()

    print("加载指数日线 ...")
    series = {name: load_index(code) for code, name in INDEXES.items()}
    for name, s in series.items():
        print(f"  {name:8s} {s.index[0].date()} ~ {s.index[-1].date()}  {len(s)} 根")

    all_dates = pd.DatetimeIndex(
        sorted(set().union(*[set(s.index) for s in series.values()]))
    )
    bench_eq = equal_weight_benchmark(series, all_dates)
    cs300 = series["沪深300"]
    cs300_eq = cs300 / cs300.iloc[0]

    summary, equity_book = [], {}
    for key, (label, ladder, mode) in CONFIGS.items():
        sub_eqs, all_tr, hold_ratio = {}, [], []
        for name, s in series.items():
            r = run_index(s, args.rf, ladder, mode)
            sub_eqs[name] = r["equity"]
            all_tr += r["trades"]
            hold_ratio.append(r["days_in"] / r["n_days"])

        port = pd.Series(0.0, index=all_dates)
        for e in sub_eqs.values():
            port = port.add(
                e.reindex(all_dates).ffill().fillna(1.0) / len(sub_eqs), fill_value=0.0
            )

        m = metrics(port, args.rf)
        tr = np.array(all_tr) if all_tr else np.array([0.0])
        summary.append(
            {
                "方案": label,
                "年化": m["cagr"],
                "波动": m["vol"],
                "夏普": m["sharpe"],
                "最大回撤": m["mdd"],
                "卡玛": m["calmar"],
                "持仓占比": float(np.mean(hold_ratio)),
                "平仓笔数": len(all_tr),
                "胜率": float((tr > 0).mean()),
                "单笔中位": float(np.median(tr)),
            }
        )
        equity_book[key] = port

    mb = metrics(bench_eq, args.rf)
    mc = metrics(cs300_eq.reindex(all_dates).ffill(), args.rf)
    for label, m in [("等权买入持有(10指数)", mb), ("沪深300买入持有", mc)]:
        summary.append(
            {
                "方案": label,
                "年化": m["cagr"],
                "波动": m["vol"],
                "夏普": m["sharpe"],
                "最大回撤": m["mdd"],
                "卡玛": m["calmar"],
                "持仓占比": 1.0,
                "平仓笔数": 0,
                "胜率": np.nan,
                "单笔中位": np.nan,
            }
        )

    out = pd.DataFrame(summary)
    pretty = out.copy()
    for c in ["年化", "波动", "最大回撤", "持仓占比", "胜率", "单笔中位"]:
        pretty[c] = pretty[c].map(lambda v: "—" if pd.isna(v) else f"{v:.1%}")
    pretty["夏普"] = pretty["夏普"].map(lambda v: "—" if pd.isna(v) else f"{v:.2f}")
    pretty["卡玛"] = pretty["卡玛"].map(lambda v: "—" if pd.isna(v) else f"{v:.2f}")

    print("\n=== 组合对比（等权 10 个子账户，现金年化 2%，单边 5bp） ===")
    print(pretty.to_string(index=False))

    print("\n=== 方案 B 分指数明细 ===")
    detail = []
    for name, s in series.items():
        r = run_index(s, args.rf, LADDER, "tp")
        h = s / s.iloc[0]
        mb_ = metrics(r["equity"], args.rf)
        mh_ = metrics(h, args.rf)
        detail.append(
            {
                "指数": name,
                "策略年化": f"{mb_['cagr']:.1%}",
                "策略回撤": f"{mb_['mdd']:.1%}",
                "持有年化": f"{mh_['cagr']:.1%}",
                "持有回撤": f"{mh_['mdd']:.1%}",
                "持仓占比": f"{r['days_in'] / r['n_days']:.0%}",
                "平仓次数": len(r["trades"]),
            }
        )
    print(pd.DataFrame(detail).to_string(index=False))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out.to_csv(
        OUT_DIR / "broad_index_drawdown_tp.csv", index=False, encoding="utf-8-sig"
    )
    eq_df = pd.DataFrame(equity_book)
    eq_df["equal_weight"] = bench_eq
    eq_df["csi300"] = cs300_eq.reindex(all_dates).ffill()
    eq_df.to_csv(OUT_DIR / "broad_index_drawdown_tp_equity.csv", encoding="utf-8-sig")

    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, (ax1, ax2) = plt.subplots(
            2, 1, figsize=(12, 8), sharex=True, height_ratios=[3, 1]
        )
        styles = {
            "A_full40_tp": ("Full position at -40% + TP", "#d62728", 1.6),
            "B_ladder_tp": ("Ladder -40/50/60 + TP", "#1f77b4", 2.0),
            "C_full40_hold": ("Full position at -40%, hold", "#ff7f0e", 1.2),
            "D_ladder_hold": ("Ladder, hold", "#9467bd", 1.2),
        }
        for k, (lab, color, lw) in styles.items():
            ax1.plot(
                equity_book[k].index,
                equity_book[k].values,
                label=lab,
                color=color,
                lw=lw,
            )
        ax1.plot(
            bench_eq.index,
            bench_eq.values,
            label="Equal-weight buy & hold",
            color="gray",
            lw=1.3,
            alpha=0.8,
        )
        ax1.plot(
            cs300_eq.index,
            cs300_eq.values,
            label="CSI300 buy & hold",
            color="black",
            lw=1.0,
            alpha=0.5,
        )
        ax1.set_yscale("log")
        ax1.set_ylabel("Net value (log)")
        ax1.legend(loc="upper left", fontsize=9)
        ax1.grid(alpha=0.3)
        ax1.set_title(
            "A-share broad indices: drawdown ladder buy + staged take-profit (fees & 2% cash included)"
        )

        for k, (lab, color, lw) in styles.items():
            e = equity_book[k]
            d = e / e.cummax() - 1
            ax2.plot(d.index, d.values, color=color, lw=lw, label=lab)
        db = bench_eq / bench_eq.cummax() - 1
        ax2.plot(
            db.index,
            db.values,
            color="gray",
            lw=1.0,
            alpha=0.7,
            label="Equal-weight DD",
        )
        ax2.set_ylabel("Drawdown")
        ax2.legend(loc="lower left", fontsize=8)
        ax2.grid(alpha=0.3)
        fig.tight_layout()
        fig.savefig(OUT_DIR / "broad_index_drawdown_tp.png", dpi=140)
        print(f"\n图表: {OUT_DIR / 'broad_index_drawdown_tp.png'}")
    except Exception as exc:  # pragma: no cover
        print(f"绘图跳过: {exc}")

    print(f"CSV : {OUT_DIR}")


if __name__ == "__main__":
    main()
