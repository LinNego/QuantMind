"""周一买入 / 周五清仓的周度选股回测，对比 T+5 与 T+10 模型。

口径：
- 信号日 = 上一交易周最后一个交易日收盘；
- 买入 = 下一交易周第一个交易日收盘；
- 周五清仓 = 该交易周最后一个交易日收盘（遇节假日提前）；
- 等权持有该信号日的全部 picks，逐周转仓，往返成本 0.2% * 换手率；
- 另给出同一批 picks 持有 5 日 / 10 日的对照，用于区分“模型周期”与“持有周期”。

用法：
    python weekly_schedule_probe.py --picks-t5 ... --picks-t10 ... --out-dir /tmp/weekly
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

import stop_loss_probe as S
import top20_model_eval as E

COST = E.COST_ROUND_TRIP


@dataclass(frozen=True)
class WeekWindow:
    signal_d: int
    buy_d: int
    sell_d: int


@dataclass(frozen=True)
class StrategyRow:
    d: int
    buy_d: int
    sell_d: int
    n: int
    gross: float
    turnover: float
    net: float
    names: frozenset[str]


def week_windows(dates: list[int]) -> list[WeekWindow]:
    """按 ISO 周切分交易日，返回 (上周最后一日, 本周第一日, 本周最后一日)。"""
    dt = pd.to_datetime(pd.Series(dates).astype(str), format="%Y%m%d")
    iso = dt.dt.isocalendar()
    frame = pd.DataFrame(
        {"d": dates, "iy": iso["year"].to_numpy(), "iw": iso["week"].to_numpy()}
    )
    weeks: list[tuple[int, int]] = []
    for _, part in frame.groupby(["iy", "iw"], sort=True):
        weeks.append((int(part["d"].iloc[0]), int(part["d"].iloc[-1])))
    return [
        WeekWindow(signal_d=weeks[i - 1][1], buy_d=weeks[i][0], sell_d=weeks[i][1])
        for i in range(1, len(weeks))
    ]


def window_return(close: np.ndarray, ib: int, ie: int, j: int) -> float:
    """区间内第一个可交易收盘买入、最后一个可交易收盘卖出。"""
    segment = close[ib : ie + 1, j]
    valid = np.isfinite(segment)
    if valid.sum() < 2:
        return float("nan")
    first = int(np.argmax(valid))
    last = len(segment) - 1 - int(np.argmax(valid[::-1]))
    if last <= first:
        return float("nan")
    return float(segment[last] / segment[first] - 1.0)


def build_rows(
    picks: pd.DataFrame,
    windows: list[WeekWindow],
    close_dates: list[int],
    date_pos: dict[int, int],
    close: np.ndarray,
    sym_pos: dict[str, int],
    hold: int,
) -> list[StrategyRow]:
    """hold=0 表示本周最后一日卖出；hold>0 表示买入后第 hold 个交易日卖出。"""
    by_signal = {
        int(d): group["symbol"].astype(str).tolist() for d, group in picks.groupby("d")
    }
    rows: list[StrategyRow] = []
    previous: set[str] | None = None
    for window in windows:
        symbols = by_signal.get(window.signal_d)
        if not symbols:
            continue
        ib = date_pos.get(window.buy_d)
        if ib is None:
            continue
        if hold == 0:
            ie = date_pos.get(window.sell_d)
        else:
            ie = ib + hold
        if ie is None or ie >= close.shape[0]:
            continue
        returns = []
        kept = []
        for symbol in symbols:
            j = sym_pos.get(symbol)
            if j is None:
                continue
            value = window_return(close, ib, ie, j)
            if not np.isfinite(value):
                continue
            returns.append(value)
            kept.append(symbol)
        if not returns:
            continue
        names = frozenset(kept)
        gross = float(np.mean(returns))
        turnover = (
            1.0 if previous is None else 1.0 - len(names & previous) / float(len(names))
        )
        rows.append(
            StrategyRow(
                d=window.signal_d,
                buy_d=window.buy_d,
                sell_d=close_dates[ie],
                n=len(returns),
                gross=gross,
                turnover=turnover,
                net=gross - turnover * COST,
                names=names,
            )
        )
        previous = set(names)
    return rows


def summarize(rows: list[StrategyRow], years_per_period: float) -> dict:
    if not rows:
        return {}
    gross = np.array([r.gross for r in rows])
    net = np.array([r.net for r in rows])
    equity_gross = np.cumprod(1.0 + gross)
    equity_net = np.cumprod(1.0 + net)
    years = max(len(rows) * years_per_period, 1e-9)

    def annualize(total: float) -> float:
        return float((1.0 + total) ** (1.0 / years) - 1.0)

    peak = np.maximum.accumulate(np.concatenate(([1.0], equity_net)))
    maxdd = float((np.concatenate(([1.0], equity_net)) / peak - 1.0).min())
    return {
        "periods": len(rows),
        "avg_names": float(np.mean([r.n for r in rows])),
        "turnover": float(np.mean([r.turnover for r in rows])),
        "total_gross": float(equity_gross[-1] - 1.0),
        "total_net": float(equity_net[-1] - 1.0),
        "ann_gross": annualize(float(equity_gross[-1] - 1.0)),
        "ann_net": annualize(float(equity_net[-1] - 1.0)),
        "maxdd_net": maxdd,
        "win_week": float((net > 0).mean()),
    }


def yearly_rows(rows: list[StrategyRow], years_per_period: float) -> list[dict]:
    out = []
    frame = pd.DataFrame(
        {
            "year": [int(r.buy_d) // 10000 for r in rows],
            "gross": [r.gross for r in rows],
            "net": [r.net for r in rows],
            "turnover": [r.turnover for r in rows],
            "n": [r.n for r in rows],
        }
    )
    for year, part in frame.groupby("year"):
        net = part["net"].to_numpy()
        equity = np.cumprod(1.0 + net)
        peak = np.maximum.accumulate(np.concatenate(([1.0], equity)))
        dd = float((np.concatenate(([1.0], equity)) / peak - 1.0).min())
        years = max(len(part) * years_per_period, 1e-9)
        out.append(
            {
                "year": year,
                "periods": len(part),
                "avg_names": float(part["n"].mean()),
                "turnover": float(part["turnover"].mean()),
                "total_net": float(equity[-1] - 1.0),
                "ann_net": float(
                    (1.0 + float(equity[-1] - 1.0)) ** (1.0 / years) - 1.0
                ),
                "maxdd_net": dd,
                "win_week": float((net > 0).mean()),
            }
        )
    return out


def run_file(
    label: str,
    picks_path: str,
    windows: list[WeekWindow],
    close_dates: list[int],
    date_pos: dict[int, int],
    close: np.ndarray,
    sym_pos: dict[str, int],
) -> tuple[list[dict], list[dict]]:
    picks = S.load_picks(picks_path, None, None)
    summary_rows = []
    year_rows = []
    variants = [
        ("周一买/周五卖", 0),  # 每周一次，周期跨度 5 个交易日
        ("周一买/持5日", 5),
        ("双周买/持10日", 10),
    ]
    for variant, hold in variants:
        if variant.startswith("双周"):
            # 每两周取一次信号，持有 10 个交易日，保证资金不重叠。
            subset = windows[::2]
        else:
            subset = windows
        rows = build_rows(
            picks, subset, close_dates, date_pos, close, sym_pos, hold=hold
        )
        # 每周期跨度：周度=5 个交易日；双周=10 个交易日。
        years_per_period = (5.0 if variant != "双周买/持10日" else 10.0) / 252.0
        stats = summarize(rows, years_per_period)
        if not stats:
            continue
        summary_rows.append(
            {
                "strategy": label,
                "variant": variant,
                "hold": hold,
                **stats,
            }
        )
        for row in yearly_rows(rows, years_per_period):
            year_rows.append(
                {"strategy": label, "variant": variant, "hold": hold, **row}
            )
    return summary_rows, year_rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--picks-t5", required=True)
    parser.add_argument("--picks-t10", required=True)
    parser.add_argument("--picks-t5-cat", default=None)
    parser.add_argument("--picks-t10-cat", default=None)
    parser.add_argument("--start", type=int, default=None)
    parser.add_argument("--end", type=int, default=None)
    parser.add_argument("--out-dir", default="/tmp/weekly_schedule")
    parser.add_argument("--memory", default="4GB")
    parser.add_argument("--threads", type=int, default=4)
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 日期窗口取两个 picks 的并集。
    frames = [
        S.load_picks(args.picks_t5, None, None),
        S.load_picks(args.picks_t10, None, None),
    ]
    all_dates = sorted(set().union(*[set(f["d"].astype(int)) for f in frames]))
    lo = int(
        (pd.Timestamp(str(min(all_dates))) - pd.Timedelta(days=10)).strftime("%Y%m%d")
    )
    hi = int(
        (pd.Timestamp(str(max(all_dates))) + pd.Timedelta(days=30)).strftime("%Y%m%d")
    )
    con = E.connect(args.memory, args.threads)
    close_panel = E.load_panel(con, "close", lo, hi)
    close_dates = [int(d) for d in close_panel.index]
    date_pos = {d: i for i, d in enumerate(close_dates)}
    close = close_panel.to_numpy(dtype=np.float64, copy=False)
    sym_pos = {str(s): j for j, s in enumerate(close_panel.columns)}
    windows = week_windows(close_dates)

    summary_rows: list[dict] = []
    year_rows: list[dict] = []
    jobs = [
        ("T+5 共识", args.picks_t5),
        ("T+10 共识", args.picks_t10),
    ]
    if args.picks_t5_cat:
        jobs.append(("T+5 CAT", args.picks_t5_cat))
    if args.picks_t10_cat:
        jobs.append(("T+10 CAT", args.picks_t10_cat))
    for label, path in jobs:
        part_summary, part_years = run_file(
            label, path, windows, close_dates, date_pos, close, sym_pos
        )
        summary_rows.extend(part_summary)
        year_rows.extend(part_years)

    summary = pd.DataFrame(summary_rows)
    years = pd.DataFrame(year_rows)
    summary.to_csv(out_dir / "weekly_summary.csv", index=False)
    years.to_csv(out_dir / "weekly_yearly.csv", index=False)
    print(summary.to_string(index=False))
    print()
    print(years.to_string(index=False))


if __name__ == "__main__":
    main()
