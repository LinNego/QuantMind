"""按指定起始日模拟一次真实建仓，并输出可执行的下单清单。

用途：回答“如果我 2026-10-08 用 200 万按 Top20 建仓，会买什么、买多少、
现在是什么状态”。所有信号与成交口径完全复用 rolling_portfolio_sim：

- 决策日收盘出综合分 S，下一交易日收盘成交；
- Top20 / T+10 / 13 个模型 / S>=20 日均值入场 / S<均值-0.03 提前退出；
- 每日买 ceil(N/ramp_days) 只，默认 ramp_days=hold=10（即 2 只/日）；
- 单票目标 = NAV*(1-2%)/20，1% ADV 上限，单边 0.1% 成本。

价格来自 daily_forward（前复权）。下单股数按 100 股整手向下取整；
若一手金额已超过单槽（高价股），该笔标记为“一手超槽”，实盘应跳过。

用法：
    python recent_build_plan.py \
      --model-map /tmp/model_map_31.json \
      --start 2026-10-08 --end 2026-10-19 \
      --capital 2000000 --top-n 20 --hold 10 \
      --out-dir /tmp/recent_build_plan
"""

from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path

import numpy as np
import pandas as pd

import rolling_portfolio_sim as R
import top20_model_eval as E


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def board_lot_shares(notional: float, price: float) -> int:
    """按 A 股 100 股整手向下取整；价格无效时返回 0。"""
    if not math.isfinite(price) or price <= 0:
        return 0
    return int(notional // (price * 100.0)) * 100


def decision_rank(
    score_row: np.ndarray,
    avg_row: np.ndarray,
    adv_row: np.ndarray,
    target: float,
    symbol_j: int,
    top_n: int = 20,
) -> tuple[int | None, float | None, int]:
    """返回该票在决策日的“可投候选”排名、第 top_n 名分数线、候选数。"""
    adv_gate = target / 100.0 if target > 0 else float("inf")
    ok = np.isfinite(score_row) & (adv_row >= adv_gate)
    ok &= np.isfinite(avg_row) & (score_row >= avg_row)
    js = np.flatnonzero(ok)
    if js.size == 0 or symbol_j not in js:
        return None, None, int(js.size)
    js = js[np.argsort(-score_row[js], kind="stable")]
    rank = int(np.flatnonzero(js == symbol_j)[0]) + 1
    cutoff = float(score_row[js[min(len(js), top_n) - 1]])
    return rank, cutoff, int(js.size)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-map", default="/tmp/model_map_31.json")
    parser.add_argument("--start", default="2026-10-08")
    parser.add_argument("--end", default="2026-10-19")
    parser.add_argument("--capital", type=float, default=2_000_000.0)
    parser.add_argument("--top-n", type=int, default=20)
    parser.add_argument("--hold", type=int, default=10)
    parser.add_argument(
        "--ramp-days",
        type=int,
        default=None,
        help="建仓摊几天；None=hold（默认 2 只/日），1=首日一次买满",
    )
    parser.add_argument("--out-dir", default="/tmp/recent_build_plan")
    parser.add_argument("--memory", default="4GB")
    parser.add_argument("--threads", type=int, default=3)
    parser.add_argument(
        "--no-print-buys",
        action="store_true",
        help="长窗口模拟时只打印摘要，不逐行打印买入清单",
    )
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    models_all = json.loads(Path(args.model_map).read_text(encoding="utf-8"))
    models = [m for m in models_all if int(m["horizon"]) == args.hold]
    if not models:
        raise RuntimeError(f"model map 里没有 T+{args.hold} 模型")
    log(f"T+{args.hold} 模型数 {len(models)}；资金 {args.capital:,.0f}")

    lo, hi = 20250101, 20261029
    con = E.connect(args.memory, args.threads)
    close, adv, blocked_long, blocked_short = R.load_panels(con, lo, hi)
    R.register_adv(con, adv)
    log(f"panel close={close.shape} adv={adv.shape}")

    score, _, _ = R.build_composite(
        con, models, "2025-02-01", args.end, diag_horizon=None
    )
    score = score.reindex(index=close.index, columns=close.columns)
    avg = R.score_average(score)
    market = R.market_daily(close, adv)
    market20 = (1.0 + market).rolling(20, min_periods=15).apply(np.prod, raw=True) - 1.0

    dates = close.index.to_numpy(dtype="int64")
    start_d = int(pd.Timestamp(args.start).strftime("%Y%m%d"))
    end_d = int(pd.Timestamp(args.end).strftime("%Y%m%d"))
    start_i = int(np.searchsorted(dates, start_d))
    end_i = int(np.searchsorted(dates, end_d))
    if start_i >= len(dates) or int(dates[start_i]) != start_d:
        raise RuntimeError(f"start={args.start} 不是面板内交易日")
    if end_i >= len(dates) or int(dates[end_i]) != end_d:
        raise RuntimeError(f"end={args.end} 不是面板内交易日")
    log(f"模拟 {dates[start_i]}..{dates[end_i]}（{end_i - start_i + 1} 个交易日）")

    cfg = R.Config(
        label=f"Top{args.top_n} H{args.hold} {args.capital / 1e4:.0f}万",
        top_n=args.top_n,
        hold=args.hold,
        capital=args.capital,
        ramp_days=args.ramp_days,
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

    trades = res.trades.copy()
    if trades.empty:
        raise RuntimeError("模拟没有产生任何交易，检查信号窗口")

    # 期末强制清仓的“end”单还原成当前持仓；净值加回假设的卖出成本。
    end_trades = trades[trades["exit_reason"] == "end"].copy()
    closed = trades[trades["exit_reason"] != "end"].copy()
    end_sell_cost = float(
        (
            end_trades["notional"]
            * end_trades["exit_price"]
            / end_trades["entry_price"]
            * cfg.cost_side
        ).sum()
    )
    nav = res.nav.copy()
    nav.iloc[-1] = float(nav.iloc[-1] + end_sell_cost)
    daily = res.daily.copy()
    daily.loc[daily.index[-1], "nav"] = float(nav.iloc[-1])
    daily.loc[daily.index[-1], "cash"] = float(
        daily.loc[daily.index[-1], "cash"]
        - (
            end_trades["notional"]
            * end_trades["exit_price"]
            / end_trades["entry_price"]
        ).sum()
        + end_sell_cost
    )
    daily.loc[daily.index[-1], "invested"] = float(
        (
            end_trades["notional"]
            * end_trades["exit_price"]
            / end_trades["entry_price"]
        ).sum()
    )
    daily.loc[daily.index[-1], "n_pos"] = int(len(end_trades))

    # 每笔买入的可执行清单：决策日排名 + 整手股数。
    rows = []
    s_arr = score.to_numpy(dtype=np.float64, copy=False)
    a_arr = avg.to_numpy(dtype=np.float64, copy=False)
    adv_arr = adv.to_numpy(dtype=np.float64, copy=False)
    sym_to_j = {str(s): j for j, s in enumerate(close.columns)}
    for _, t in trades.sort_values(["entry_d", "symbol"]).iterrows():
        j = sym_to_j[str(t["symbol"])]
        di = int(t["entry_i"]) - 1
        nav_d = float(daily.loc[dates[di], "nav"])
        target = nav_d * (1.0 - cfg.cash_buffer) / cfg.top_n
        rank, cutoff, n_ok = decision_rank(
            s_arr[di], a_arr[di], adv_arr[di], target, j, top_n=args.top_n
        )
        shares = board_lot_shares(float(t["notional"]), float(t["entry_price"]))
        actual = shares * float(t["entry_price"])
        rows.append(
            {
                "entry_date": int(t["entry_d"]),
                "decision_date": int(dates[di]),
                "symbol": str(t["symbol"]),
                "eligible_rank": rank,
                "eligible_n": n_ok,
                "top_n_cutoff_score": cutoff,
                "score": float(t["entry_score"]),
                "avg20": float(t["entry_avg"]),
                "entry_price": float(t["entry_price"]),
                "slot_notional": target,
                "shares": shares,
                "lot_notional": actual,
                "lot_weight_pct": actual / args.capital * 100.0,
                "one_lot_over_slot": shares == 0,
            }
        )
    buy_plan = pd.DataFrame(rows).sort_values(
        ["entry_date", "eligible_rank"], na_position="last"
    )
    buy_plan.to_csv(out_dir / "buy_plan.csv", index=False)

    # 当前持仓（10/19 收盘）。
    hold_rows = []
    for _, t in end_trades.iterrows():
        j = sym_to_j[str(t["symbol"])]
        shares = board_lot_shares(float(t["notional"]), float(t["entry_price"]))
        mv = shares * float(t["exit_price"])
        cost = shares * float(t["entry_price"])
        s_now = float(s_arr[end_i, j])
        a_now = float(a_arr[end_i, j])
        hold_rows.append(
            {
                "symbol": str(t["symbol"]),
                "entry_date": int(t["entry_d"]),
                "entry_price": float(t["entry_price"]),
                "last_price": float(t["exit_price"]),
                "shares": shares,
                "market_value": mv,
                "cost": cost,
                "unrealized_pnl": mv - cost,
                "ret_pct": (float(t["exit_price"]) / float(t["entry_price"]) - 1.0)
                * 100.0,
                "score_last": s_now,
                "avg20_last": a_now,
                "weak_today": bool(
                    math.isfinite(s_now)
                    and math.isfinite(a_now)
                    and s_now < a_now - cfg.score_buffer
                ),
                "days_held": int(end_i - t["entry_i"]),
                "time_due": bool(end_i - t["entry_i"] >= cfg.hold - 1),
            }
        )
    holdings = pd.DataFrame(hold_rows)
    if not holdings.empty:
        holdings = holdings.sort_values(["entry_date", "symbol"])
        holdings.to_csv(out_dir / "holdings_end.csv", index=False)
    if not closed.empty:
        closed.to_csv(out_dir / "closed_trades.csv", index=False)

    # 下一交易日预览：只按最后一个已知交易日的分数推演，价格用该日收盘参考。
    preview_rows = []
    if end_i == len(dates) - 1 and not holdings.empty:
        held = set(holdings["symbol"])
        weak_syms = set(holdings.loc[holdings["weak_today"], "symbol"])
        remaining = held - weak_syms
        vacancies = cfg.top_n - len(remaining)
        ramp_start_i = int(trades["entry_i"].min())
        ramp_days = cfg.ramp_days if cfg.ramp_days is not None else cfg.hold
        i_next = end_i + 1
        max_buys = (
            min(vacancies, math.ceil(cfg.top_n / ramp_days))
            if i_next - ramp_start_i < ramp_days
            else vacancies
        )
        target_next = float(nav.iloc[-1]) * (1.0 - cfg.cash_buffer) / cfg.top_n
        adv_gate = target_next / 100.0
        ok = (
            np.isfinite(s_arr[end_i])
            & np.isfinite(a_arr[end_i])
            & (s_arr[end_i] >= a_arr[end_i])
            & (adv_arr[end_i] >= adv_gate)
        )
        for sym in weak_syms:
            preview_rows.append(
                {
                    "action": "sell",
                    "symbol": sym,
                    "score_last": float(s_arr[end_i, sym_to_j[sym]]),
                    "avg20_last": float(a_arr[end_i, sym_to_j[sym]]),
                    "close_last": float(close.iloc[end_i][sym]),
                    "note": "分数走弱，下一交易日收盘卖出",
                }
            )
        js = [j for j in np.flatnonzero(ok) if str(close.columns[j]) not in held]
        js = sorted(js, key=lambda j: -s_arr[end_i, j])
        for rank, j in enumerate(js[:max_buys], start=1):
            sym = str(close.columns[j])
            preview_rows.append(
                {
                    "action": "buy",
                    "symbol": sym,
                    "score_last": float(s_arr[end_i, j]),
                    "avg20_last": float(a_arr[end_i, j]),
                    "close_last": float(close.iloc[end_i][sym]),
                    "note": f"下一交易日收盘买入（候选第 {rank}/{max_buys}）",
                }
            )
    preview = pd.DataFrame(preview_rows)
    if not preview.empty:
        preview.to_csv(out_dir / "next_day_preview.csv", index=False)

    nav.rename("nav").to_csv(out_dir / "daily_nav.csv")
    daily.to_csv(out_dir / "daily.csv")

    total_ret = float(nav.iloc[-1] / args.capital - 1.0)
    eq = nav / args.capital
    maxdd = float((eq / eq.cummax() - 1.0).min())
    bench = float(
        (1.0 + market.loc[dates[start_i] : dates[end_i]].iloc[1:]).prod() - 1.0
    )
    # 现金拖累校正：用“前一日收盘仓位 × 当日市场收益”构造同暴露基准。
    exposure_prev = (daily["invested"].shift(1) / daily["nav"].shift(1)).fillna(0.0)
    mkt_on_daily = market.reindex(daily.index).fillna(0.0)
    matched_ret = exposure_prev * mkt_on_daily
    matched_nav = args.capital * (1.0 + matched_ret).cumprod()
    matched_total = float(matched_nav.iloc[-1] / args.capital - 1.0)
    avg_exposure = float(daily["invested"].mean() / daily["nav"].mean())

    # 逐笔“选股超额”：同一持有窗口内，个股毛收益 − 等权市场收益。
    excess_rows = []
    for _, t in trades.iterrows():
        ei, xi = int(t["entry_i"]), int(t["exit_i"])
        m = market.iloc[ei + 1 : xi + 1]
        mret = float((1.0 + m).prod() - 1.0) if len(m) else 0.0
        excess_rows.append(
            {
                "symbol": str(t["symbol"]),
                "notional": float(t["notional"]),
                "gross_ret": float(t["gross_ret"]),
                "market_ret": mret,
                "gross_excess": float(t["gross_ret"]) - mret,
                "net_excess": float(t["net_ret"]) - mret,
            }
        )
    excess = pd.DataFrame(excess_rows)
    excess.to_csv(out_dir / "trade_excess.csv", index=False)
    w = excess["notional"]
    gross_excess_w = float((excess["gross_excess"] * w).sum() / w.sum())
    net_excess_w = float((excess["net_excess"] * w).sum() / w.sum())
    excess_win = float((excess["gross_excess"] > 0).mean())

    invested = float(daily.loc[daily.index[-1], "invested"])
    cash = float(daily.loc[daily.index[-1], "cash"])
    log(
        f"买入 {len(buy_plan)} 笔；当前持仓 {len(holdings)} 只；"
        f"期末净值 {nav.iloc[-1]:,.0f}"
    )
    last_label = pd.Timestamp(str(dates[end_i])).strftime("%Y-%m-%d")
    print("\n=== 摘要 ===")
    print(f"起始资金        {args.capital:,.0f}")
    print(f"期末净值        {nav.iloc[-1]:,.0f}（{total_ret:+.2%}）")
    print(f"同期等权基准    {bench:+.2%}")
    print(f"平均仓位        {avg_exposure:.1%}")
    print(f"同暴露基准      {matched_total:+.2%}（前一日仓位 × 当日市场收益）")
    print(f"现金校正超额    {total_ret - matched_total:+.2%}")
    print(
        f"逐笔选股超额    {gross_excess_w:+.2%}（毛，等权窗口；净 {net_excess_w:+.2%}）"
    )
    print(
        f"逐笔胜率        {excess_win:.1%}（{int((excess['gross_excess'] > 0).sum())}/{len(excess)}）"
    )
    print(f"区间最大回撤    {maxdd:.2%}")
    print(
        f"当前持仓        {len(holdings)} 只，市值 {invested:,.0f}（{invested / nav.iloc[-1]:.1%}）"
    )
    print(f"当前现金        {cash:,.0f}（{cash / nav.iloc[-1]:.1%}）")
    print(
        f"买入笔数        {len(buy_plan)}，其中整手 0 股 {int(buy_plan['one_lot_over_slot'].sum())} 笔"
    )
    if args.no_print_buys:
        return
    print("\n=== 逐日买入 ===")
    show = buy_plan.copy()
    show["entry_date"] = show["entry_date"].astype(str)
    show["decision_date"] = show["decision_date"].astype(str)
    cols = [
        "entry_date",
        "decision_date",
        "symbol",
        "eligible_rank",
        "score",
        "avg20",
        "entry_price",
        "shares",
        "lot_notional",
    ]
    print(show[cols].to_string(index=False))
    if not holdings.empty:
        print(f"\n=== {last_label} 收盘持仓 ===")
        h = holdings.copy()
        h["entry_date"] = h["entry_date"].astype(str)
        print(
            h[
                [
                    "symbol",
                    "entry_date",
                    "entry_price",
                    "last_price",
                    "shares",
                    "market_value",
                    "unrealized_pnl",
                    "ret_pct",
                    "weak_today",
                    "days_held",
                ]
            ].to_string(index=False)
        )
    if not preview.empty:
        print(
            f"\n=== 下一交易日预览（按 {last_label} 分数，"
            f"价格以 {last_label} 收盘参考；未计次日涨跌停/停牌）==="
        )
        print(preview.to_string(index=False))


if __name__ == "__main__":
    main()
