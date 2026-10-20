"""Top100 打分探针：2026-09-01 ~ 2026-10-19。

打分口径（与 rolling_portfolio_sim 一致）：
- 每个模型在 ADV>=2000 万池内做当日截面百分位 pct∈[0,1]；
- 综合分 S = 所选模型 pct 的等权均值（至少 8 个模型有分）；
- TopN = 每日 S 降序前 N；
- 平均分 = TopN 的 S 均值（同时给 0-100 分）。

交易规则沿用滚动模拟：
- S_t >= 过去 20 个交易日均值 A_t 才买；
- T+5 / T+10 到期，或分数走弱（S_t < A_t - 0.03）提前退出；
- t 收盘决策、t+1 收盘成交；单边成本 0.1%；涨跌停/停牌按 trade_mask；
- 2026-09-01 冷启动，按 ceil(N/H) 每日建仓；2026-10-19 期末清仓。

模型集（全部只看 2026-08-26 之前的模型能力表，9-10 月不参与选模型）：
- all13：T+10 全部 13 个模型；
- ic10：能力表 IC 均值前 10；
- tail10：能力表 Top20 前向超额前 10。

用法：
    python top100_score_probe.py \
      --model-map /tmp/model_map_31.json \
      --capability-csv /tmp/capability_prereg.csv \
      --out-dir /tmp/top100_probe
"""

from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

import rolling_portfolio_sim as R


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


@dataclass(frozen=True)
class Variant:
    label: str
    model_set: str
    top_n: int
    hold: int
    score_exit: bool = True
    use_avg_filter: bool = True


def load_percentiles(
    con,
    models: list[dict],
    start: str,
    end: str,
) -> dict[str, pd.DataFrame]:
    """逐模型截面百分位；同一模型只读一次，供不同模型集复用。"""
    frames: dict[str, pd.DataFrame] = {}
    for model in models:
        frame = R.model_score_frame(con, model, start, end)
        pv = frame.pivot(index="d", columns="symbol", values="pct")
        frames[model["model_id"]] = pv
        log(
            f"pct {model['model_type']:>14} T+{model['horizon']} "
            f"{pv.shape[0]}d x {pv.shape[1]}s"
        )
    return frames


def composite_from_frames(
    frames: list[pd.DataFrame],
    min_models: int = R.MIN_MODELS,
) -> pd.DataFrame:
    """等权百分位均值；不足 min_models 的股票当日无分。"""
    total: pd.DataFrame | None = None
    count: pd.DataFrame | None = None
    for pv in frames:
        total = pv if total is None else total.add(pv, fill_value=0.0)
        c = pv.notna().astype("int16")
        count = c if count is None else count.add(c, fill_value=0)
    if total is None or count is None:
        raise RuntimeError("没有可用模型")
    return total / count.where(count >= min_models)


def topn_daily(
    score: pd.DataFrame,
    avg: pd.DataFrame,
    top_n: int,
) -> pd.DataFrame:
    """每日 TopN 平均分，以及通过 S>=A 入场过滤后的平均分。"""
    s_arr = score.to_numpy(dtype=float)
    a_arr = avg.to_numpy(dtype=float)
    rows: list[dict] = []
    for i, d in enumerate(score.index):
        row = s_arr[i]
        finite = np.isfinite(row)
        vals = row[finite]
        if vals.size == 0:
            continue
        k = min(top_n, vals.size)
        top = np.partition(vals, -k)[-k:]
        eligible = finite & np.isfinite(a_arr[i]) & (row >= a_arr[i])
        ev = row[eligible]
        ek = min(top_n, ev.size)
        etop = np.partition(ev, -ek)[-ek:] if ek else np.array([])
        rows.append(
            {
                "date": int(d),
                "n_eligible": int(vals.size),
                "n_top": int(k),
                "top_avg": float(top.mean()),
                "top_median": float(np.median(top)),
                "top_min": float(top.min()),
                "top_p90": float(np.percentile(top, 90)),
                "n_entry_ok": int(ev.size),
                "entry_top_avg": float(etop.mean()) if ek else float("nan"),
            }
        )
    return pd.DataFrame(rows)


def holdings_score_curve(
    trades: pd.DataFrame,
    score: pd.DataFrame,
) -> pd.Series:
    """按持仓区间回填每日持仓平均分（仅用于诊断）。"""
    s_arr = score.to_numpy(dtype=float)
    col = {symbol: j for j, symbol in enumerate(score.columns)}
    sums = np.zeros(len(score), dtype=float)
    counts = np.zeros(len(score), dtype=int)
    for t in trades.itertuples():
        j = col.get(t.symbol)
        if j is None:
            continue
        lo = max(int(t.entry_i), 0)
        hi = min(int(t.exit_i), len(score) - 1)
        for i in range(lo, hi + 1):
            v = s_arr[i, j]
            if math.isfinite(v):
                sums[i] += v
                counts[i] += 1
    avg = np.divide(
        sums,
        counts,
        out=np.full_like(sums, np.nan),
        where=counts > 0,
    )
    return pd.Series(avg, index=score.index, name="holdings_avg_score")


def segment_stats(nav: pd.Series) -> dict[str, float]:
    """段内总收益/回撤/日胜率；年化只作参考，29 天外推没有意义。"""
    ret = nav.pct_change().dropna()
    eq = nav / nav.iloc[0]
    dd = float((eq / eq.cummax() - 1.0).min())
    sd = float(ret.std(ddof=1))
    return {
        "days": int(len(ret)),
        "total_ret": float(nav.iloc[-1] / nav.iloc[0] - 1.0),
        "maxdd": dd,
        "win_daily": float((ret > 0).mean()) if len(ret) else float("nan"),
        "sharpe": (
            float(ret.mean() / sd * math.sqrt(R.TRADING_DAYS))
            if sd > 0
            else float("nan")
        ),
    }


def run_variant(
    variant: Variant,
    score: pd.DataFrame,
    avg: pd.DataFrame,
    close: pd.DataFrame,
    adv: pd.DataFrame,
    blocked_long: np.ndarray,
    blocked_short: np.ndarray,
    start_i: int,
    end_i: int,
    market20: pd.Series,
    capital: float,
) -> tuple[R.SimResult, pd.DataFrame, pd.Series]:
    cfg = R.Config(
        label=variant.label,
        top_n=variant.top_n,
        hold=variant.hold,
        score_exit=variant.score_exit,
        use_avg_filter=variant.use_avg_filter,
        score_buffer=0.03,
        capital=capital,
    )
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
    )
    daily_top = topn_daily(score, avg, variant.top_n)
    seg_dates = {int(d) for d in close.index[start_i : end_i + 1]}
    daily_top = daily_top[daily_top["date"].isin(seg_dates)].reset_index(drop=True)
    hold_curve = (
        holdings_score_curve(res.trades, score)
        if not res.trades.empty
        else pd.Series(dtype=float, name="holdings_avg_score")
    )
    return res, daily_top, hold_curve


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-map", default="/tmp/model_map_31.json")
    parser.add_argument("--capability-csv", default="/tmp/capability_prereg.csv")
    parser.add_argument("--start", default="2026-09-01")
    parser.add_argument("--end", default="2026-10-19")
    parser.add_argument("--score-start", default="2026-06-01")
    parser.add_argument("--out-dir", default="/tmp/top100_probe")
    parser.add_argument("--memory", default="4GB")
    parser.add_argument("--threads", type=int, default=3)
    parser.add_argument("--capital", type=float, default=R.CAPITAL)
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    models_all = json.loads(Path(args.model_map).read_text(encoding="utf-8"))
    models_10 = [m for m in models_all if int(m["horizon"]) == 10]
    models_5 = [m for m in models_all if int(m["horizon"]) == 5]
    if len(models_10) != 13:
        raise RuntimeError(f"T+10 应有 13 个模型，实际 {len(models_10)}")

    cap = pd.read_csv(args.capability_csv)
    cap10 = cap[cap["horizon"] == 10].copy()
    if len(cap10) != 13:
        raise RuntimeError(f"能力表 T+10 应有 13 行，实际 {len(cap10)}")
    ic10 = cap10.nlargest(10, "ic_mean")["model_type"].tolist()
    tail10 = cap10.nlargest(10, "top20_excess_ann")["model_type"].tolist()
    sets = {
        "all13": [m["model_type"] for m in models_10],
        "ic10": ic10,
        "tail10": tail10,
    }
    log(f"all13={sets['all13']}")
    log(f"ic10={ic10}")
    log(f"tail10={tail10}")

    lo = int(
        (pd.Timestamp(args.score_start) - pd.Timedelta(days=45)).strftime("%Y%m%d")
    )
    hi = int((pd.Timestamp(args.end) + pd.Timedelta(days=10)).strftime("%Y%m%d"))
    con = R.E.connect(args.memory, args.threads)
    close, adv, blocked_long, blocked_short = R.load_panels(con, lo, hi)
    log(f"panel close={close.shape} adv={adv.shape}")
    R.register_adv(con, adv)

    frames_10 = load_percentiles(con, models_10, args.score_start, args.end)
    frames_5 = load_percentiles(con, models_5, args.score_start, args.end)
    by_type_10 = {m["model_type"]: frames_10[m["model_id"]] for m in models_10}

    score_lo = int(pd.Timestamp(args.score_start).strftime("%Y%m%d"))
    scores: dict[tuple[str, int], pd.DataFrame] = {}
    avgs: dict[tuple[str, int], pd.DataFrame] = {}
    for set_name, types in sets.items():
        frames = [by_type_10[t] for t in types]
        score = composite_from_frames(frames).reindex(
            index=close.index, columns=close.columns
        )
        scores[(set_name, 10)] = score
        avgs[(set_name, 10)] = R.score_average(score)
        cov = float(score.loc[score_lo:].notna().sum(axis=1).mean())
        log(f"composite {set_name} T+10 coverage/day={cov:.0f}")
    score5 = composite_from_frames(list(frames_5.values())).reindex(
        index=close.index, columns=close.columns
    )
    scores[("all13", 5)] = score5
    avgs[("all13", 5)] = R.score_average(score5)
    cov = float(score5.loc[score_lo:].notna().sum(axis=1).mean())
    log(f"composite all13 T+5 coverage/day={cov:.0f}")

    market = R.market_daily(close, adv)
    market20 = (1.0 + market).rolling(20, min_periods=15).apply(np.prod, raw=True) - 1.0
    dates = close.index.to_numpy(dtype="int64")
    start_i = int(
        np.searchsorted(dates, int(pd.Timestamp(args.start).strftime("%Y%m%d")))
    )
    end_i = int(np.searchsorted(dates, int(pd.Timestamp(args.end).strftime("%Y%m%d"))))
    if start_i >= len(dates) or dates[start_i] != int(
        pd.Timestamp(args.start).strftime("%Y%m%d")
    ):
        raise RuntimeError(f"起始日不在交易日历：{args.start}")
    if end_i >= len(dates) or dates[end_i] != int(
        pd.Timestamp(args.end).strftime("%Y%m%d")
    ):
        raise RuntimeError(f"结束日不在交易日历：{args.end}")
    log(f"sim {dates[start_i]} .. {dates[end_i]}")

    variants = [
        Variant("Top50 all13 H10 score", "all13", 50, 10),
        Variant("Top100 all13 H10 score", "all13", 100, 10),
        Variant("Top50 ic10 H10 score", "ic10", 50, 10),
        Variant("Top100 ic10 H10 score", "ic10", 100, 10),
        Variant("Top50 tail10 H10 score", "tail10", 50, 10),
        Variant("Top100 tail10 H10 score", "tail10", 100, 10),
        Variant("Top100 all13 H5 score", "all13", 100, 5),
        Variant("Top100 all13 H10 time", "all13", 100, 10, score_exit=False),
        Variant(
            "Top50 all13 H10 noavg",
            "all13",
            50,
            10,
            score_exit=False,
            use_avg_filter=False,
        ),
        Variant(
            "Top100 all13 H10 noavg",
            "all13",
            100,
            10,
            score_exit=False,
            use_avg_filter=False,
        ),
    ]

    summary_rows: list[dict] = []
    for variant in variants:
        key = (variant.model_set, variant.hold)
        res, daily_top, hold_curve = run_variant(
            variant,
            scores[key],
            avgs[key],
            close,
            adv,
            blocked_long,
            blocked_short,
            start_i,
            end_i,
            market20,
            args.capital,
        )
        seg = segment_stats(res.nav)
        name = R.slug(variant.label)
        res.nav.rename("nav").to_csv(out_dir / f"nav_{name}.csv")
        res.daily.to_csv(out_dir / f"daily_{name}.csv")
        if not res.trades.empty:
            res.trades.to_csv(out_dir / f"trades_{name}.csv", index=False)
        daily_top.to_csv(out_dir / f"daily_top_{name}.csv", index=False)
        hold_curve.rename("holdings_avg_score").to_csv(
            out_dir / f"holdings_avg_{name}.csv"
        )
        avg_nav = float(res.daily["nav"].mean())
        seg_turnover = (
            float(res.daily["buy_notional"].sum() / avg_nav)
            if avg_nav > 0
            else float("nan")
        )
        steady = (
            res.daily.iloc[variant.hold : -1]
            if len(res.daily) > variant.hold + 1
            else res.daily
        )
        steady_nav = float(steady["nav"].mean())
        steady_names = float(steady["n_pos"].mean())
        steady_exposure = (
            float(steady["invested"].mean() / steady_nav)
            if steady_nav > 0
            else float("nan")
        )
        summary_rows.append(
            {
                "label": variant.label,
                "model_set": variant.model_set,
                "top_n": variant.top_n,
                "hold": variant.hold,
                "score_exit": variant.score_exit,
                **seg,
                "avg_names": float(res.daily["n_pos"].mean()),
                "steady_names": steady_names,
                "steady_exposure": steady_exposure,
                "avg_exposure": float(res.daily["invested"].mean() / avg_nav)
                if avg_nav > 0
                else float("nan"),
                "seg_turnover": seg_turnover,
                "n_trades": int(len(res.trades)),
                "trade_win": float((res.trades["net_ret"] > 0).mean())
                if not res.trades.empty
                else float("nan"),
                "trade_avg_ret": float(res.trades["net_ret"].mean())
                if not res.trades.empty
                else float("nan"),
                "trade_med_ret": float(res.trades["net_ret"].median())
                if not res.trades.empty
                else float("nan"),
                "trade_avg_hold": float(res.trades["hold_days"].mean())
                if not res.trades.empty
                else float("nan"),
                "exit_time": int((res.trades["exit_reason"] == "time").sum())
                if not res.trades.empty
                else 0,
                "exit_score": int((res.trades["exit_reason"] == "score").sum())
                if not res.trades.empty
                else 0,
                "exit_end": int((res.trades["exit_reason"] == "end").sum())
                if not res.trades.empty
                else 0,
                "top_avg_mean": float(daily_top["top_avg"].mean()),
                "top_avg_min": float(daily_top["top_avg"].min()),
                "top_avg_max": float(daily_top["top_avg"].max()),
                "top_avg_last": float(daily_top["top_avg"].iloc[-1]),
                "entry_ok_mean": float(daily_top["n_entry_ok"].mean()),
                "holdings_avg_mean": float(hold_curve.mean())
                if len(hold_curve)
                else float("nan"),
            }
        )
        log(
            f"{variant.label:<30} total={seg['total_ret']:>7.2%} "
            f"dd={seg['maxdd']:>7.2%} win={seg['win_daily']:>5.1%} "
            f"names={summary_rows[-1]['avg_names']:>5.1f} "
            f"top_avg={summary_rows[-1]['top_avg_mean']:.3f}"
        )

    summary = pd.DataFrame(summary_rows)
    summary.to_csv(out_dir / "summary.csv", index=False)

    # 基准：与策略同一起点到结束日，等权可投池。
    bench = market.iloc[start_i + 1 : end_i + 1].dropna()
    bench_nav = (1.0 + bench).cumprod()
    bench_nav.rename("nav").to_csv(out_dir / "nav_benchmark.csv")
    bench_stats = segment_stats(
        pd.concat([pd.Series([1.0]), bench_nav]).reset_index(drop=True)
    )
    pd.DataFrame([bench_stats]).to_csv(out_dir / "benchmark.csv", index=False)

    # 最后一天 Top100 明细（含单模型百分位），用于人工核对打分。
    last_d = int(dates[end_i])
    top = scores[("all13", 10)].loc[last_d].dropna().sort_values(ascending=False)
    top100 = top.head(100)
    detail = pd.DataFrame(
        {
            "rank": np.arange(1, len(top100) + 1),
            "symbol": top100.index,
            "score": top100.to_numpy(),
            "score100": top100.to_numpy() * 100.0,
        }
    )
    per_model = pd.DataFrame(
        {m["model_type"]: by_type_10[m["model_type"]].loc[last_d] for m in models_10}
    ).reindex(top100.index)
    per_model.columns = [f"pct_{c}" for c in per_model.columns]
    detail = detail.join(per_model.reset_index(drop=True))
    detail.to_csv(out_dir / f"top100_{last_d}.csv", index=False)
    detail.head(20).to_csv(out_dir / f"top20_{last_d}.csv", index=False)

    print(f"\n汇总（{args.start} ~ {args.end}）")
    show = summary[
        [
            "label",
            "total_ret",
            "maxdd",
            "win_daily",
            "steady_names",
            "steady_exposure",
            "seg_turnover",
            "n_trades",
            "trade_win",
            "trade_avg_ret",
            "top_avg_mean",
            "holdings_avg_mean",
        ]
    ].copy()
    for c in ("total_ret", "maxdd", "win_daily", "trade_win", "trade_avg_ret"):
        show[c] = show[c].map(lambda x: f"{x:.2%}")
    for c in ("top_avg_mean", "holdings_avg_mean", "seg_turnover"):
        show[c] = show[c].map(lambda x: f"{x:.3f}")
    show["steady_names"] = show["steady_names"].map(lambda x: f"{x:.1f}")
    show["steady_exposure"] = show["steady_exposure"].map(lambda x: f"{x:.1%}")
    print(show.to_string(index=False))
    print(f"\n基准（等权可投池，{args.start} ~ {args.end}）")
    print(
        f"total={bench_stats['total_ret']:.2%} dd={bench_stats['maxdd']:.2%} "
        f"win={bench_stats['win_daily']:.1%}"
    )
    print(f"\n输出目录：{out_dir}")


if __name__ == "__main__":
    main()
