"""逐模型统计“选股后第二天上涨”的概率。

回答的问题：31 个模型各自每天选 Top20 / Top100，选中的票第二天上涨的
概率有多高？比当天同池所有股票的平均上涨概率高多少？

两个口径必须分开看：

1. 信号次日（sig）：close[t+1] / close[t] - 1。
   t 日收盘出分、假设 t 日收盘就能买，衡量“分数对次日涨跌的预测力”。
   包含 t+1 涨停/停牌的票，因为这些票确实涨了，只是买不到。
2. 可执行首日（exec）：close[t+2] / close[t+1] - 1。
   按现行策略 t 日收盘决策、t+1 日收盘成交，买入后的第一个持有日。
   剔除 t+1 日 blocked_long（涨停/停牌买不进）的票。

基准：同一天、同一模型可投池（ADV>=0.2 亿）里所有有有效收益的股票。
超额 = TopK 命中率 - 基准命中率。模型之间比“超额”而不是原始命中率，
因为原始命中率随大盘涨跌漂移。

统计显著性：
- 日频 t 值会被“同一批票连续入选”带来的自相关放大，只能参考；
- 月频 t 值（先按月求平均，再对月份做 t 检验）更保守；
- 同时给出月胜率（多少个月超额为正），避免只看一个总数。

用法：
    python model_next_day_hit.py \
      --model-map /tmp/model_map_31.json \
      --out-dir /tmp/model_next_day_hit
"""

from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path

import numpy as np
import pandas as pd

WINDOWS: dict[str, tuple[int, int]] = {
    "full": (20250205, 20261019),
    "2025": (20250101, 20251231),
    "2026": (20260101, 20261019),
    "recent": (20260701, 20261019),
}


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def tstat(values: pd.Series) -> float:
    """单样本 t 值；样本不足或方差为 0 时返回 NaN。"""
    x = values.dropna().to_numpy(dtype=float)
    if x.size < 2:
        return float("nan")
    sd = float(np.std(x, ddof=1))
    if not math.isfinite(sd) or sd <= 0:
        return float("nan")
    return float(np.mean(x) / sd * math.sqrt(x.size))


def monthly_mean(values: pd.Series) -> pd.Series:
    """按 YYYYMM 求均值；index 是 YYYYMMDD 整数。"""
    idx = pd.Index(values.index.astype("int64") // 100, name="month")
    return values.groupby(idx).mean()


def month_tstat(values: pd.Series) -> tuple[float, int, int]:
    """月频 t 值、为正的月数、总月数。"""
    monthly = monthly_mean(values).dropna()
    n = int(len(monthly))
    t = tstat(monthly) if n >= 2 else float("nan")
    pos = int((monthly > 0).sum())
    return t, pos, n


def bh_qvalues(pvalues: pd.Series) -> pd.Series:
    """Benjamini-Hochberg q 值；NaN 原样保留。"""
    p = pvalues.to_numpy(dtype=float)
    out = np.full(p.shape, np.nan, dtype=float)
    ok = np.isfinite(p)
    if not ok.any():
        return pd.Series(out, index=pvalues.index)
    vals = p[ok]
    order = np.argsort(vals)
    ranked = vals[order]
    n = float(len(ranked))
    q = ranked * n / np.arange(1, len(ranked) + 1, dtype=float)
    q = np.minimum.accumulate(q[::-1])[::-1]
    q = np.clip(q, 0.0, 1.0)
    restored = np.empty_like(q)
    restored[order] = q
    out[ok] = restored
    return pd.Series(out, index=pvalues.index)


def daily_topk_stats(
    long: pd.DataFrame,
    top_ks: tuple[int, ...] = (20, 100),
) -> pd.DataFrame:
    """按日计算基准与 TopK 的次日/可执行首日命中率。

    long 列：d, symbol, pct, ret_sig, ret_exec, blocked_next。
    - ret_sig  = close[t+1]/close[t]-1
    - ret_exec = close[t+2]/close[t+1]-1
    - blocked_next = t+1 日 blocked_long（True 表示买不进）

    返回以 d 为索引的日频表。命中率是当日等权股票占比，不是跨日汇总。
    """
    df = long.loc[long["pct"].notna()].copy()
    df = df.sort_values(["d", "pct"], ascending=[True, False], kind="stable")
    df["rk"] = df.groupby("d", sort=False).cumcount() + 1

    sig = df.loc[df["ret_sig"].notna()].copy()
    sig["hit"] = (sig["ret_sig"] > 0).astype("float64")
    out = sig.groupby("d", sort=True).agg(
        sig_base_hit=("hit", "mean"),
        sig_base_ret=("ret_sig", "mean"),
        sig_base_n=("ret_sig", "size"),
    )

    exec_ok = df["ret_exec"].notna() & ~df["blocked_next"].fillna(True).astype(bool)
    ex = df.loc[exec_ok].copy()
    ex["hit"] = (ex["ret_exec"] > 0).astype("float64")
    ex_base = ex.groupby("d", sort=True).agg(
        exec_base_hit=("hit", "mean"),
        exec_base_ret=("ret_exec", "mean"),
        exec_base_n=("ret_exec", "size"),
    )
    out = out.join(ex_base, how="outer")

    for k in top_ks:
        sig_top = (
            sig.loc[sig["rk"] <= k]
            .groupby("d", sort=True)
            .agg(
                **{
                    f"sig_top{k}_hit": ("hit", "mean"),
                    f"sig_top{k}_ret": ("ret_sig", "mean"),
                    f"sig_top{k}_n": ("ret_sig", "size"),
                }
            )
        )
        ex_top = (
            ex.loc[ex["rk"] <= k]
            .groupby("d", sort=True)
            .agg(
                **{
                    f"exec_top{k}_hit": ("hit", "mean"),
                    f"exec_top{k}_ret": ("ret_exec", "mean"),
                    f"exec_top{k}_n": ("ret_exec", "size"),
                }
            )
        )
        out = out.join(sig_top, how="left").join(ex_top, how="left")
    return out.sort_index()


def summarize_daily(
    daily: pd.DataFrame,
    k: int,
    start: int,
    end: int,
) -> dict[str, float]:
    """把一个窗口的日频表压成一行摘要。"""
    d = daily.loc[(daily.index >= start) & (daily.index <= end)]
    row: dict[str, float] = {"window_days": float(len(d))}
    for mode in ("sig", "exec"):
        top_hit = d[f"{mode}_top{k}_hit"]
        base_hit = d[f"{mode}_base_hit"]
        top_ret = d[f"{mode}_top{k}_ret"]
        base_ret = d[f"{mode}_base_ret"]
        top_n = d[f"{mode}_top{k}_n"]
        base_n = d[f"{mode}_base_n"]

        ok = top_hit.notna() & base_hit.notna()
        row[f"{mode}_days"] = float(ok.sum())
        if not ok.any():
            for name in (
                "top_hit",
                "base_hit",
                "lift",
                "lift_t",
                "month_t",
                "pos_months",
                "n_months",
                "top_ret",
                "base_ret",
                "excess",
                "excess_t",
                "n_picks",
                "base_n_avg",
                "top_hit_pooled",
                "base_hit_pooled",
                "lift_pooled",
            ):
                row[f"{mode}_{name}"] = float("nan")
            continue

        lift = (top_hit - base_hit)[ok]
        ret_ok = top_ret.notna() & base_ret.notna()
        excess = (top_ret - base_ret)[ret_ok]

        mt, pos, n_months = month_tstat(lift)
        row[f"{mode}_top_hit"] = float(top_hit[ok].mean())
        row[f"{mode}_base_hit"] = float(base_hit[ok].mean())
        row[f"{mode}_lift"] = float(lift.mean())
        row[f"{mode}_lift_t"] = tstat(lift)
        row[f"{mode}_month_t"] = mt
        row[f"{mode}_pos_months"] = float(pos)
        row[f"{mode}_n_months"] = float(n_months)
        row[f"{mode}_top_ret"] = float(top_ret[ret_ok].mean())
        row[f"{mode}_base_ret"] = float(base_ret[ret_ok].mean())
        row[f"{mode}_excess"] = float(excess.mean())
        row[f"{mode}_excess_t"] = tstat(excess)
        row[f"{mode}_n_picks"] = float(top_n[ok].sum())
        row[f"{mode}_base_n_avg"] = float(base_n[ok].mean())
        # 跨日池化命中率：按票加权，和日频等权命中率互为稳健性检查。
        tn = top_n[ok].to_numpy(dtype=float)
        bn = base_n[ok].to_numpy(dtype=float)
        row[f"{mode}_top_hit_pooled"] = float(
            np.average(top_hit[ok].to_numpy(dtype=float), weights=tn)
        )
        row[f"{mode}_base_hit_pooled"] = float(
            np.average(base_hit[ok].to_numpy(dtype=float), weights=bn)
        )
        row[f"{mode}_lift_pooled"] = (
            row[f"{mode}_top_hit_pooled"] - row[f"{mode}_base_hit_pooled"]
        )
    return row


def load_returns(close: pd.DataFrame, blocked_long: np.ndarray) -> pd.DataFrame:
    """把宽表收益转成长表：d, symbol, ret_sig, ret_exec, blocked_next。"""
    ret_sig = close.shift(-1) / close - 1.0
    ret_exec = close.shift(-2) / close.shift(-1) - 1.0
    blocked_next = pd.DataFrame(
        blocked_long, index=close.index, columns=close.columns
    ).shift(-1)

    parts = []
    for name, frame in (("ret_sig", ret_sig), ("ret_exec", ret_exec)):
        long = frame.stack().rename(name).reset_index()
        long.columns = ["d", "symbol", name]
        parts.append(long)
    blk = blocked_next.stack().rename("blocked_next").reset_index()
    blk.columns = ["d", "symbol", "blocked_next"]
    parts.append(blk)

    out = parts[0]
    for part in parts[1:]:
        out = out.merge(part, on=["d", "symbol"], how="outer")
    out["d"] = out["d"].astype("int64")
    return out


def model_label(model: dict) -> str:
    return f"T+{model['horizon']} {model['model_type']}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-map", default="/tmp/model_map_31.json")
    parser.add_argument("--start", default="2025-02-05")
    parser.add_argument("--end", default="2026-10-19")
    parser.add_argument("--top-ks", default="20,100")
    parser.add_argument("--out-dir", default="/tmp/model_next_day_hit")
    parser.add_argument("--memory", default="6GB")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument(
        "--model-types",
        default="",
        help="只跑这些 model_type，逗号分隔；空=全部",
    )
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 个模型")
    args = parser.parse_args()

    import rolling_portfolio_sim as R
    import top20_model_eval as E

    top_ks = tuple(int(x) for x in args.top_ks.split(",") if x.strip())
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    models = json.loads(Path(args.model_map).read_text(encoding="utf-8"))
    if args.model_types:
        wanted = {x.strip() for x in args.model_types.split(",") if x.strip()}
        models = [m for m in models if m["model_type"] in wanted]
    if args.limit:
        models = models[: args.limit]
    log(f"模型数 {len(models)}；TopK={top_ks}")

    lo, hi = 20250101, 20261029
    con = E.connect(args.memory, args.threads)
    close, adv, blocked_long, blocked_short = R.load_panels(con, lo, hi)
    R.register_adv(con, adv)
    log(f"panel close={close.shape} adv={adv.shape}")

    returns = load_returns(close, blocked_long)
    con.register("ret_long_hit", returns)
    log(f"returns long={returns.shape}")

    rows: list[dict] = []
    for n, model in enumerate(models, start=1):
        label = model_label(model)
        try:
            frame = R.model_score_frame(con, model, args.start, args.end)
        except Exception as exc:  # noqa: BLE001 - 单模型缺失不应中断全批
            log(f"[{n}/{len(models)}] {label} 读取失败: {exc}")
            continue
        if frame.empty:
            log(f"[{n}/{len(models)}] {label} 无预测")
            continue
        con.register("model_frame_hit", frame)
        long = con.execute(
            "SELECT m.d, m.symbol, m.pct, r.ret_sig, r.ret_exec, "
            "       r.blocked_next "
            "FROM model_frame_hit m "
            "LEFT JOIN ret_long_hit r ON m.d = r.d AND m.symbol = r.symbol"
        ).fetchdf()
        con.unregister("model_frame_hit")
        daily = daily_topk_stats(long, top_ks)
        windows = dict(WINDOWS)
        windows["full"] = (
            int(pd.Timestamp(args.start).strftime("%Y%m%d")),
            int(pd.Timestamp(args.end).strftime("%Y%m%d")),
        )
        for k in top_ks:
            for window, (start, end) in windows.items():
                row = summarize_daily(daily, k, start, end)
                row.update(
                    {
                        "model_type": model["model_type"],
                        "horizon": int(model["horizon"]),
                        "model_id": model["model_id"],
                        "display_name": model.get("display_name", label),
                        "label": label,
                        "k": k,
                        "window": window,
                        "start": start,
                        "end": end,
                    }
                )
                rows.append(row)
        if 20 in top_ks:
            top20_mean = float((daily["sig_top20_hit"] - daily["sig_base_hit"]).mean())
        else:
            top20_mean = float("nan")
        log(
            f"[{n}/{len(models)}] {label} days={len(daily)} "
            f"Top20 sig lift={top20_mean:+.2%}"
        )

    if not rows:
        raise RuntimeError("没有任何模型产出结果")
    result = pd.DataFrame(rows)
    front = [
        "model_type",
        "horizon",
        "label",
        "k",
        "window",
        "window_days",
        "sig_days",
        "exec_days",
        "sig_top_hit",
        "sig_base_hit",
        "sig_lift",
        "sig_lift_t",
        "sig_month_t",
        "sig_pos_months",
        "sig_n_months",
        "sig_excess",
        "exec_top_hit",
        "exec_base_hit",
        "exec_lift",
        "exec_lift_t",
        "exec_month_t",
        "exec_pos_months",
        "exec_n_months",
        "exec_excess",
    ]
    other = [c for c in result.columns if c not in front]
    result = result[front + other]
    result.to_csv(out_dir / "model_next_day_hit.csv", index=False)
    log(f"写出 {out_dir / 'model_next_day_hit.csv'}（{len(result)} 行）")

    # 多重检验：对 Top20 信号、2026 窗口做 BH 校正。
    primary = result.loc[(result["k"] == 20) & (result["window"] == "2026")].copy()
    if not primary.empty:
        from scipy import stats

        df_t = primary["sig_n_months"].fillna(0).to_numpy(dtype=float) - 1.0
        tvals = primary["sig_month_t"].to_numpy(dtype=float)
        pvals = np.full_like(tvals, np.nan)
        ok = np.isfinite(tvals) & np.isfinite(df_t) & (df_t >= 1)
        pvals[ok] = 2.0 * stats.t.sf(np.abs(tvals[ok]), df_t[ok])
        primary["sig_month_p"] = pvals
        primary["sig_month_q"] = bh_qvalues(primary["sig_month_p"]).to_numpy()
        primary.sort_values("sig_lift", ascending=False).to_csv(
            out_dir / "top20_signal_2026_ranked.csv", index=False
        )

    # 稳定性：全期超额 vs 2026 超额、2026 vs 近 3 月的 Spearman 秩相关。
    wide = result.loc[result["k"] == 20].pivot(
        index=["label", "horizon"], columns="window", values="sig_lift"
    )
    corr_rows = []
    for a, b in (("full", "2026"), ("2026", "recent"), ("2025", "2026")):
        if a in wide and b in wide:
            pair = wide[[a, b]].dropna()
            rho = float(pair[a].corr(pair[b], method="spearman"))
            corr_rows.append({"pair": f"{a}_vs_{b}", "n": len(pair), "spearman": rho})
    corr = pd.DataFrame(corr_rows)
    corr.to_csv(out_dir / "rank_stability.csv", index=False)

    print("\n=== Top20 信号次日（全期，按超额排序）===")
    show = result.loc[(result["k"] == 20) & (result["window"] == "full")].copy()
    show = show.sort_values("sig_lift", ascending=False)
    show["月胜率"] = show["sig_pos_months"] / show["sig_n_months"]
    cols = [
        "label",
        "sig_top_hit",
        "sig_base_hit",
        "sig_lift",
        "sig_month_t",
        "月胜率",
        "exec_top_hit",
        "exec_base_hit",
        "exec_lift",
    ]
    print(show[cols].to_string(index=False, float_format=lambda x: f"{x:.3f}"))

    print("\n=== 稳定性（Top20 信号超额的跨窗口秩相关）===")
    print(corr.to_string(index=False, float_format=lambda x: f"{x:.3f}"))


if __name__ == "__main__":
    main()
