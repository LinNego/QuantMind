#!/usr/bin/env python3
"""最简月度反转回测器（n01 的 B2–B5 共用）。

固定口径（先写死，改口径就改代码并在报告里注明）：
  - 调仓：每月最后一个交易日收盘出信号，默认次日开盘成交（--exec next_open）
  - 选股：过去 lookback 日收益【最低】的 N 只（20 日反转）
  - 权重：等权；不可交易的票（涨停/停牌）不买，那部分权重留现金
  - 成本：单边 cost_rate（买卖各收一次），按成交金额计
  - 停牌：持仓沿用上一可用收盘价估值；调仓日停牌则不交易
  - 涨停：不复权开盘价相对前收 ≥ --limit-up-pct 视为涨停，不买

用法示例：
  .venv/bin/python plan/n01/tools/backtest.py --cost 0 --out plan/n01/out/b2_nav.csv
  .venv/bin/python plan/n01/tools/backtest.py --cost 0.001 --out plan/n01/out/b3_nav_10bp.csv
  .venv/bin/python plan/n01/tools/backtest.py --cost 0.003 --out plan/n01/out/b3_nav_30bp.csv
  .venv/bin/python plan/n01/tools/backtest.py --skip-limit-up --skip-suspend
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
import pandas as pd

import qm


def load_market(start: str | None, end: str | None):
    fwd = qm.kline(adj="forward", start=start, end=end,
                   columns=["symbol", "time", "open", "close"])
    raw = qm.kline(adj="unadjusted", start=start, end=end,
                   columns=["symbol", "time", "open", "close", "volume"])

    def pv(df, col):
        return df.pivot(index="time", columns="symbol", values=col).sort_index()

    px_close = pv(fwd, "close")
    return {
        "close": px_close.ffill(),
        "open": pv(fwd, "open"),
        "raw_open": pv(raw, "open"),
        "raw_close": pv(raw, "close"),
        "volume": pv(raw, "volume"),
        "signal": px_close / px_close.shift(1) - 1.0,  # 占位，下面重建
    }


def month_end_dates(index: pd.DatetimeIndex) -> list[pd.Timestamp]:
    s = pd.Series(index, index=index)
    return [ts for ts in s.groupby([index.year, index.month]).last().values]


def run(args) -> pd.DataFrame:
    mkt = load_market(args.start, args.end)
    px_close = mkt["close"]
    dates = px_close.index
    cols = px_close.columns
    pos = {d: i for i, d in enumerate(dates)}
    close_np = px_close.to_numpy()
    open_np = mkt["open"].to_numpy()
    raw_open_np = mkt["raw_open"].to_numpy()
    raw_close_np = mkt["raw_close"].to_numpy()
    vol_np = mkt["volume"].to_numpy()
    n_sym = len(cols)

    # 信号：用未停牌（有真实收盘）的复权价算 lookback 收益
    signal = px_close / px_close.shift(args.lookback) - 1.0
    sig_np = signal.to_numpy()

    rebal = month_end_dates(dates)
    exec_pairs: list[tuple[int, int]] = []
    for d in rebal:
        i = pos[d]
        if args.exec == "next_open":
            if i + 1 < len(dates):
                exec_pairs.append((i, i + 1))
        else:
            exec_pairs.append((i, i))

    capital = 1_000_000.0
    cash = capital
    shares = np.zeros(n_sym)
    nav_rows: list[tuple[pd.Timestamp, float]] = []
    turnover_total = 0.0
    trade_count = 0
    last_t = -1

    def value_at(t: int) -> float:
        marks = close_np[t]
        marks = np.where(np.isnan(marks), 0.0, marks)
        return cash + float(np.dot(shares, marks))

    for n, (sig_i, exec_i) in enumerate(exec_pairs):
        # 补记执行日之前的逐日净值
        for t in range(last_t + 1, exec_i):
            nav_rows.append((dates[t], value_at(t)))

        p = open_np[exec_i] if args.exec == "next_open" else close_np[exec_i]

        total = cash + float(np.nansum(shares * np.where(np.isnan(p), 0.0, p)))
        p_safe = np.where(np.isnan(p), 0.0, p)
        old_w = np.where(total > 0, shares * p_safe / total, 0.0)

        scores = sig_np[sig_i]
        valid = ~np.isnan(scores)
        k = min(args.top_n, int(valid.sum()))
        picked = np.argsort(np.where(valid, scores, np.inf))[:k]

        tradable = np.zeros(n_sym, dtype=bool)
        for j in picked:
            j = int(j)
            ok = (not np.isnan(p[j])) and (not np.isnan(vol_np[exec_i, j])) and vol_np[exec_i, j] > 0
            if ok and args.skip_limit_up and exec_i > 0:
                prev_c = raw_close_np[exec_i - 1, j]
                if not (np.isnan(prev_c) or prev_c <= 0 or np.isnan(raw_open_np[exec_i, j])):
                    if raw_open_np[exec_i, j] / prev_c - 1.0 >= args.limit_up_pct:
                        ok = False
            tradable[j] = ok

        target_w = np.zeros(n_sym)
        if tradable.sum() > 0:
            target_w[tradable] = 1.0 / k

        trade_value = np.zeros(n_sym)
        for j in range(n_sym):
            if target_w[j] <= 0:
                # 停牌（执行价缺失）卖不掉：保留持仓，否则会按 0 元清仓、凭空亏钱
                if shares[j] > 0 and p_safe[j] > 0:
                    trade_value[j] += shares[j] * p_safe[j]
                    cash += shares[j] * p_safe[j]
                    shares[j] = 0.0
            elif p_safe[j] > 0:
                v_now = shares[j] * p_safe[j]
                target = target_w[j] * total
                delta = target - v_now
                if delta < 0:
                    cash += -delta
                    trade_value[j] += -delta
                    shares[j] += delta / p_safe[j]
                elif delta > 0 and cash > 0:
                    delta = min(delta, cash)
                    cash -= delta
                    trade_value[j] += delta
                    shares[j] += delta / p_safe[j]

        cash -= trade_value.sum() * args.cost
        turnover_total += 0.5 * float(np.abs(target_w - old_w).sum())
        trade_count += 1

        next_exec = exec_pairs[n + 1][1] if n + 1 < len(exec_pairs) else len(dates)
        for t in range(exec_i, next_exec):
            nav_rows.append((dates[t], value_at(t)))
        last_t = next_exec - 1

    nav = pd.DataFrame(nav_rows, columns=["time", "nav"]).drop_duplicates("time").sort_values("time")
    nav = nav.set_index("time")
    years = max((nav.index[-1] - nav.index[0]).days / 365.25, 1e-9)
    total_ret = nav["nav"].iloc[-1] / capital - 1
    ann = (1 + total_ret) ** (1 / years) - 1
    dd = float((nav["nav"] / nav["nav"].cummax() - 1).min())

    print(f"区间      : {nav.index[0].date()} → {nav.index[-1].date()}（{years:.2f} 年，{trade_count} 次调仓）")
    print(f"年化收益  : {ann*100:.2f}%")
    print(f"最大回撤  : {dd*100:.2f}%")
    print(f"年换手    : {turnover_total/years:.2f}x（单边）")
    print(f"期末净值  : {nav['nav'].iloc[-1]/capital:.4f}")
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        nav.to_csv(out)
        print(f"净值已写   : {out}")
    return nav


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--top-n", type=int, default=20)
    ap.add_argument("--lookback", type=int, default=20)
    ap.add_argument("--cost", type=float, default=0.0, help="单边成本率，如 0.001 = 0.1%%")
    ap.add_argument("--exec", choices=["close", "next_open"], default="next_open")
    ap.add_argument("--skip-limit-up", action="store_true")
    ap.add_argument("--skip-suspend", action="store_true")
    ap.add_argument("--limit-up-pct", type=float, default=0.095)
    ap.add_argument("--out", default="")
    run(ap.parse_args())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
