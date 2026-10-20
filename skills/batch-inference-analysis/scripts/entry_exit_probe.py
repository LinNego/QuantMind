"""买卖执行探针：只测“机制型”改法，不在同一窗口调参。

预注册（先写规则，再看结果）：

基线 B = Top50 / T+10 / 13 模型 / S>=20 日均值入场 / S<均值-0.03 退出 / 次日收盘成交。

单变量变体（每次只改一处）：
- rank75 / rank100 / rank150：把“分数退出”换成“跌出当日综合分前 K 名”退出。
  经济含义：S 本来就是截面排名，排名掉出前 K 比“跌破自己的 20 日均值”更直接；
  K 取 1.5N / 2N / 3N，同时看阈值方向是否单调，防止单点噪音。
- entry_confirm2：连续 2 天满足 S>=A 才买，过滤一日脉冲。
- exit_confirm2：连续 2 天跌破 A-0.03 才卖，过滤一日假摔。
- open_fill：信号日收盘决策、次日开盘成交（更保守的可执行口径）。
- open_sell_close_buy：卖出用次日开盘，买入用次日收盘（弱票早走、新票不追开盘）。

验收门槛（全部满足才算“更好”，否则只记录、不推荐）：
1. 全期年化 >= 基线 + 1.0pp；
2. 2025 与 2026 两个年度都不低于基线 0.5pp 以上；
3. 2026-09-01~10-19 前向窗口总收益不低于基线（不能变差）；
4. 全期 Sharpe 不低于基线 0.02，最大回撤不恶化超过 1.0pp
   （防止“多吃了半天敞口”被误判成更好的执行）；
5. 排名退出族里 rank100 与 rank150 必须同向（不能只有单点好）；
6. 不引入“看到结果后再改”的参数。

第 4 条是看到 open_buy_close_sell 用更早入场换来更高收益后补上的：
它全期收益 +2.6pp，但 Sharpe 1.48 < 1.56、回撤 -27.4% < -23.8%，
本质是多拿了半天敞口，不是更好的买卖点，按风险调整后否决。

用法：
    python entry_exit_probe.py \
      --model-map /tmp/model_map_31.json \
      --out-dir /tmp/entry_exit_probe \
      --memory 4GB --threads 3
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

import rolling_portfolio_sim as R
import top20_model_eval as E


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def load_open(con, lo: int, hi: int, close: pd.DataFrame) -> pd.DataFrame:
    open_px = E.load_panel(con, "open", lo, hi)
    return open_px.reindex(index=close.index, columns=close.columns)


def load_vwap(con, lo: int, hi: int, close: pd.DataFrame) -> pd.DataFrame:
    """日内 VWAP 近似：amount 单位万元、volume 单位股。"""
    amount = E.load_panel(con, "amount", lo, hi)
    volume = E.load_panel(con, "volume", lo, hi)
    vwap = (amount * 10_000.0).div(volume.where(volume > 0))
    vwap = vwap.replace([np.inf, -np.inf], np.nan)
    return vwap.reindex(index=close.index, columns=close.columns)


def configs(top_n: int = 50, hold: int = 10) -> list[R.Config]:
    base = R.Config(label="base", top_n=top_n, hold=hold)
    return [
        base,
        replace(base, label="rank75", score_exit=False, exit_rank=75),
        replace(base, label="rank100", score_exit=False, exit_rank=100),
        replace(base, label="rank150", score_exit=False, exit_rank=150),
        replace(base, label="entry_confirm2", entry_confirm=2),
        replace(base, label="exit_confirm2", exit_confirm=2),
        replace(base, label="open_fill", fill="open"),
        replace(base, label="vwap_fill", fill="vwap"),
        replace(
            base,
            label="open_sell_close_buy",
            fill="open_sell_close_buy",
        ),
        replace(
            base,
            label="open_buy_close_sell",
            fill="open_buy_close_sell",
        ),
        replace(
            base,
            label="open_buy_close_sell_h9",
            fill="open_buy_close_sell",
            hold=9,
        ),
        replace(base, label="hold8", hold=8),
        replace(base, label="hold15", hold=15),
        replace(base, label="hold20", hold=20),
    ]


def run_period(
    label: str,
    cfgs: list[R.Config],
    close: pd.DataFrame,
    open_px: pd.DataFrame,
    vwap_px: pd.DataFrame,
    adv: pd.DataFrame,
    score: pd.DataFrame,
    avg: pd.DataFrame,
    blocked_long: np.ndarray,
    blocked_short: np.ndarray,
    start_i: int,
    end_i: int,
    market: pd.Series,
    market20: pd.Series,
    out_dir: Path,
) -> pd.DataFrame:
    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[R.SimResult] = []
    for cfg in cfgs:
        t0 = time.time()
        res = R.simulate(
            cfg,
            close,
            adv,
            score,
            avg,
            blocked_long,
            blocked_short,
            start_i,
            end_i,
            market20,
            open_px=open_px,
            vwap_px=vwap_px,
        )
        results.append(res)
        s = res.stats
        log(
            f"{label:<8} {cfg.label:<22} ann={s['ann_ret']:>7.1%} "
            f"tot={s['total_ret']:>7.1%} dd={s['maxdd']:>7.1%} "
            f"sr={s['sharpe']:>5.2f} turn={s['turnover_ann']:>5.1f}x "
            f"win={s.get('trade_win', float('nan')):>5.1%} "
            f"({time.time() - t0:.1f}s)"
        )
    summary = pd.DataFrame([r.stats for r in results])
    summary.insert(0, "period", label)
    summary.to_csv(out_dir / "summary.csv", index=False)

    yearly = pd.concat(
        [r.yearly.assign(label=r.config.label) for r in results if not r.yearly.empty],
        ignore_index=True,
    )
    yearly.to_csv(out_dir / "yearly.csv", index=False)

    for r in results:
        name = R.slug(r.config.label)
        r.nav.rename("nav").to_csv(out_dir / f"nav_{name}.csv")
        if not r.trades.empty:
            r.trades.to_csv(out_dir / f"trades_{name}.csv", index=False)

    dates = close.index.to_numpy(dtype="int64")
    ret = market.iloc[start_i : end_i + 1].dropna()
    bench_nav = (1.0 + ret).cumprod()
    bench_total = float(bench_nav.iloc[-1] - 1.0) if len(bench_nav) else float("nan")
    bench_dd = (
        float((bench_nav / bench_nav.cummax() - 1.0).min())
        if len(bench_nav)
        else float("nan")
    )
    pd.DataFrame(
        [
            {
                "period": label,
                "start": int(dates[start_i]),
                "end": int(dates[end_i]),
                "days": int(len(ret)),
                "total_ret": bench_total,
                "maxdd": bench_dd,
            }
        ]
    ).to_csv(out_dir / "benchmark.csv", index=False)
    log(f"{label} 基准 total={bench_total:.2%} dd={bench_dd:.2%}")
    return summary


def decision_table(summary: pd.DataFrame, yearly: pd.DataFrame) -> pd.DataFrame:
    """按预注册门槛逐项判定；不满足就明确写 no。"""
    full = summary[summary["period"] == "full"].set_index("label")
    sep = summary[summary["period"] == "sep_oct"].set_index("label")
    y25 = yearly[yearly["year"] == 2025].set_index("label")
    y26 = yearly[yearly["year"] == 2026].set_index("label")
    base = full.loc["base"]
    rows = []
    for label in full.index:
        row = full.loc[label]
        d_full = float(row["ann_ret"] - base["ann_ret"])
        d_25 = float(y25.loc[label, "ann_ret"] - y25.loc["base", "ann_ret"])
        d_26 = float(y26.loc[label, "ann_ret"] - y26.loc["base", "ann_ret"])
        d_sep = float(sep.loc[label, "total_ret"] - sep.loc["base", "total_ret"])
        d_sharpe = float(row["sharpe"] - base["sharpe"])
        d_dd = float(row["maxdd"] - base["maxdd"])
        rank_family = label in {"rank100", "rank150"}
        rank_same_dir = True
        if rank_family:
            rank_same_dir = (
                full.loc["rank100", "ann_ret"] >= base["ann_ret"]
                and full.loc["rank150", "ann_ret"] >= base["ann_ret"]
            )
        passed = (
            label != "base"
            and d_full >= 0.01
            and d_25 >= -0.005
            and d_26 >= -0.005
            and d_sep >= 0.0
            and d_sharpe >= -0.02
            and d_dd >= -0.01
            and rank_same_dir
        )
        rows.append(
            {
                "label": label,
                "d_full_ann": d_full,
                "d_2025_ann": d_25,
                "d_2026_ann": d_26,
                "d_sep_oct": d_sep,
                "d_sharpe": d_sharpe,
                "d_maxdd": d_dd,
                "rank_same_dir": rank_same_dir,
                "pass": bool(passed),
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-map", default="/tmp/model_map_31.json")
    parser.add_argument("--out-dir", default="/tmp/entry_exit_probe")
    parser.add_argument("--memory", default="4GB")
    parser.add_argument("--threads", type=int, default=3)
    parser.add_argument("--top-n", type=int, default=50)
    parser.add_argument("--hold", type=int, default=10)
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    models_all = json.loads(Path(args.model_map).read_text(encoding="utf-8"))
    models = [m for m in models_all if int(m["horizon"]) == args.hold]
    if not models:
        raise RuntimeError(f"model map 里没有 T+{args.hold} 模型")
    log(f"T+{args.hold} 模型数 {len(models)}")

    lo, hi = 20250101, 20261029
    con = E.connect(args.memory, args.threads)
    close, adv, blocked_long, blocked_short = R.load_panels(con, lo, hi)
    open_px = load_open(con, lo, hi, close)
    vwap_px = load_vwap(con, lo, hi, close)
    log(f"panel close={close.shape} open={open_px.shape} adv={adv.shape}")
    R.register_adv(con, adv)

    score, _, _ = R.build_composite(
        con,
        models,
        "2025-02-01",
        "2026-10-19",
        diag_horizon=None,
    )
    score = score.reindex(index=close.index, columns=close.columns)
    avg = R.score_average(score)
    market = R.market_daily(close, adv)
    market20 = (1.0 + market).rolling(20, min_periods=15).apply(np.prod, raw=True) - 1.0

    dates = close.index.to_numpy(dtype="int64")
    avg_rows = avg.notna().sum(axis=1)
    first_avg_i = int(np.argmax(avg_rows.to_numpy() >= 100))
    full_start_i = first_avg_i + 1
    full_end_i = int(np.searchsorted(dates, 20260826))
    sep_start_i = int(np.searchsorted(dates, 20260901))
    sep_end_i = int(np.searchsorted(dates, 20261019))
    log(
        f"full {dates[full_start_i]}..{dates[full_end_i]} "
        f"sep_oct {dates[sep_start_i]}..{dates[sep_end_i]}"
    )

    cfgs = [replace(c, capital=R.CAPITAL) for c in configs(args.top_n, args.hold)]
    common = {
        "cfgs": cfgs,
        "close": close,
        "open_px": open_px,
        "vwap_px": vwap_px,
        "adv": adv,
        "score": score,
        "avg": avg,
        "blocked_long": blocked_long,
        "blocked_short": blocked_short,
        "market": market,
        "market20": market20,
    }
    full = run_period(
        "full",
        start_i=full_start_i,
        end_i=full_end_i,
        out_dir=out_dir / "full",
        **common,
    )
    sep = run_period(
        "sep_oct",
        start_i=sep_start_i,
        end_i=sep_end_i,
        out_dir=out_dir / "sep_oct",
        **common,
    )
    summary = pd.concat([full, sep], ignore_index=True)
    summary.to_csv(out_dir / "summary_all.csv", index=False)
    yearly = pd.concat(
        [
            pd.read_csv(out_dir / "full" / "yearly.csv"),
        ],
        ignore_index=True,
    )
    decision = decision_table(summary, yearly)
    decision.to_csv(out_dir / "decision.csv", index=False)
    print("\n预注册判定：")
    print(decision.to_string(index=False))


if __name__ == "__main__":
    main()
