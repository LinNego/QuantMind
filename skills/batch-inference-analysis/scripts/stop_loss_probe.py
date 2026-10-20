#!/usr/bin/env python3
"""固定止损 + 移动止盈叠加回测（容器内运行）。

口径
----
- 选股结果来自 consensus_threshold_probe.py 落盘的 picks_*.csv：
  signal 日 T 收盘出信号，T+1 收盘买入（buy_d），原计划持有 h 个交易日到
  buy_d+h 收盘。每 h 个交易日换仓，等权。
- 本脚本只改“卖出”规则，不改选股：
  1. 固定止损：买入价 * (1 - sl)。日内 low 触发按止损价成交；
     若 open 已低于止损价，按 open 成交（跳空）。
  2. 移动止盈：最高价 high-water 达到 entry*(1+activate) 后激活，
     之后按 high-water*(1-trail) 回撤止盈；同样处理跳空。
     激活只影响下一交易日，避免“当天先冲高再回落”的路径歧义。
  3. 止损与移动止盈同时存在时，取两者中更高的卖出线。
  4. 未触发任何规则则持有到 buy_d+h 收盘。
- 提前卖出后现金留到下一个换仓日，不补票。
- 成本沿用原口径：单次换手往返 0.2%，按 turnover*0.002 从组合期收益扣除。

输出
----
- stop_trail_grid.csv：所有配置 × 全期/2025/2026 的年化、回撤、退出结构
- stop_trail_yearly.csv：同上，按年拆开
- stop_trail_picks_<strategy>_<config>.csv：逐笔诊断（主配置）
- stop_trail_mfe_mae_<strategy>.csv：按是否触发 -7% / +10% 分组的事后收益
- REPORT.md：可直接阅读的结论
"""

from __future__ import annotations

import argparse
import glob
import re
from dataclasses import dataclass
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

import top20_model_eval as E

FWD_DIR = "/data/quantdb/1_kline_data/daily_forward"
COST = E.COST_ROUND_TRIP
DEFAULT_HORIZON = 10


@dataclass(frozen=True)
class TrailConfig:
    name: str
    sl: float | None
    activate: float | None
    trail: float | None
    label: str
    mode: str = "intraday"


@dataclass(frozen=True)
class PickPath:
    d: int
    symbol: str
    buy_d: int
    entry: float
    open_: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    tradable: np.ndarray
    fwd: float
    mfe: float
    mae: float


def build_configs() -> list[TrailConfig]:
    configs = [
        TrailConfig("baseline", None, None, None, "不止损/不止盈"),
        TrailConfig("sl5", 0.05, None, None, "-5% 固定止损"),
        TrailConfig("sl7", 0.07, None, None, "-7% 固定止损"),
        TrailConfig("sl10", 0.10, None, None, "-10% 固定止损"),
        TrailConfig("sl15", 0.15, None, None, "-15% 固定止损"),
    ]
    for activate in (0.05, 0.07, 0.10, 0.15):
        for trail in (0.03, 0.05, 0.07):
            configs.append(
                TrailConfig(
                    f"trail_a{int(activate * 100)}_t{int(trail * 100)}",
                    None,
                    activate,
                    trail,
                    f"纯移动止盈 +{int(activate * 100)}% 激活 / 回撤 {int(trail * 100)}%",
                )
            )
    for activate in (0.05, 0.07, 0.10, 0.15):
        for trail in (0.03, 0.05, 0.07):
            configs.append(
                TrailConfig(
                    f"sl7_a{int(activate * 100)}_t{int(trail * 100)}",
                    0.07,
                    activate,
                    trail,
                    f"-7% 止损 + +{int(activate * 100)}% 激活 / 回撤 {int(trail * 100)}%",
                )
            )
    configs.extend(
        [
            TrailConfig(
                "sl5_a10_t5", 0.05, 0.10, 0.05, "-5% 止损 + +10% 激活 / 回撤 5%"
            ),
            TrailConfig(
                "sl10_a10_t5", 0.10, 0.10, 0.05, "-10% 止损 + +10% 激活 / 回撤 5%"
            ),
            TrailConfig("pure_trail7", None, 0.0, 0.07, "纯移动止损 7%"),
            TrailConfig("pure_trail10", None, 0.0, 0.10, "纯移动止损 10%"),
            # 收盘价口径：只在收盘跌破/回撤时退出，避免日内插针。
            TrailConfig(
                "sl7_close",
                0.07,
                None,
                None,
                "-7% 收盘止损",
                mode="close",
            ),
            TrailConfig(
                "sl10_close",
                0.10,
                None,
                None,
                "-10% 收盘止损",
                mode="close",
            ),
            TrailConfig(
                "sl7_close_a10_t5",
                0.07,
                0.10,
                0.05,
                "-7% 收盘止损 + +10% 激活 / 回撤 5%（收盘）",
                mode="close",
            ),
            TrailConfig(
                "sl7_close_a15_t5",
                0.07,
                0.15,
                0.05,
                "-7% 收盘止损 + +15% 激活 / 回撤 5%（收盘）",
                mode="close",
            ),
            TrailConfig(
                "trail_close_a10_t5",
                None,
                0.10,
                0.05,
                "纯移动止盈 +10% 激活 / 回撤 5%（收盘）",
                mode="close",
            ),
            TrailConfig(
                "trail_close_a15_t3",
                None,
                0.15,
                0.03,
                "纯移动止盈 +15% 激活 / 回撤 3%（收盘）",
                mode="close",
            ),
        ]
    )
    return configs


def load_picks(path: str, start: int | None, end: int | None) -> pd.DataFrame:
    picks = pd.read_csv(path)
    picks = picks[~picks["blocked"].fillna(False)].copy()
    picks = picks.dropna(subset=["fwd"])
    picks["d"] = picks["d"].astype("int64")
    picks["buy_d"] = picks["buy_d"].astype("int64")
    picks["symbol"] = picks["symbol"].astype(str).str.strip()
    if start is not None:
        picks = picks[picks["d"] >= start]
    if end is not None:
        picks = picks[picks["d"] <= end]
    return picks.drop_duplicates(subset=["d", "symbol"])


def partition_files(pattern: str, lo: int, hi: int) -> list[str]:
    files: list[str] = []
    for path in glob.glob(pattern):
        match = re.search(r"dt=(\d{8})", path)
        if match and lo <= int(match.group(1)) <= hi:
            files.append(path)
    return sorted(files)


def load_panel(
    con: duckdb.DuckDBPyConnection, column: str, lo: int, hi: int
) -> pd.DataFrame:
    files = partition_files(f"{FWD_DIR}/dt=*/data.parquet", lo, hi)
    if not files:
        raise RuntimeError(f"daily_forward 无分区: {lo}..{hi}")
    sql = (
        f"SELECT symbol, CAST(dt AS INTEGER) AS d, {column} AS v "
        f"FROM read_parquet({files!r}) WHERE {column} IS NOT NULL"
    )
    frame = con.execute(sql).fetchdf()
    frame = frame.drop_duplicates(subset=["symbol", "d"], keep="last")
    return frame.pivot(index="d", columns="symbol", values="v").sort_index()


def load_arrays(
    con: duckdb.DuckDBPyConnection, lo: int, hi: int
) -> tuple[pd.DataFrame, dict[str, np.ndarray]]:
    close = load_panel(con, "close", lo, hi)
    fields = {"close": close}
    for field in ("open", "high", "low"):
        panel = load_panel(con, field, lo, hi)
        panel = panel.reindex(index=close.index, columns=close.columns)
        fields[field] = panel
    arrays = {
        "open": fields["open"].to_numpy(dtype=np.float64),
        "high": fields["high"].to_numpy(dtype=np.float64),
        "low": fields["low"].to_numpy(dtype=np.float64),
        "close": fields["close"].to_numpy(dtype=np.float64),
    }
    return close, arrays


def build_paths(
    picks: pd.DataFrame,
    dates: pd.Index,
    arrays: dict[str, np.ndarray],
    horizon: int,
) -> tuple[dict[int, list[PickPath]], int]:
    date_pos = {int(d): i for i, d in enumerate(dates)}
    sym_pos: dict[str, int] = {}
    if "symbols" not in arrays:
        raise RuntimeError("arrays 缺少 symbols")
    for j, symbol in enumerate(arrays["symbols"]):
        sym_pos[str(symbol)] = j
    open_, high = arrays["open"], arrays["high"]
    low, close = arrays["low"], arrays["close"]
    n_dates = len(dates)
    by_d: dict[int, list[PickPath]] = {}
    dropped = 0
    for row in picks.itertuples(index=False):
        i = date_pos.get(int(row.buy_d))
        j = sym_pos.get(str(row.symbol))
        if i is None or j is None or i + horizon >= n_dates:
            dropped += 1
            continue
        entry = close[i, j]
        if not np.isfinite(entry) or entry <= 0:
            dropped += 1
            continue
        sl = slice(i + 1, i + 1 + horizon)
        cc = close[sl, j].astype(np.float64, copy=True)
        if not np.isfinite(cc[-1]):
            dropped += 1
            continue
        tradable = np.isfinite(cc)
        # 停牌日 close 为 NaN：按上一有效收盘价延续，且当天不可交易，
        # 止损/止盈不触发（真实持仓也卖不出）。
        last = float(entry)
        for t in range(horizon):
            if tradable[t]:
                last = float(cc[t])
            else:
                cc[t] = last
        oo = open_[sl, j].astype(np.float64, copy=True)
        hh = high[sl, j].astype(np.float64, copy=True)
        ll = low[sl, j].astype(np.float64, copy=True)
        oo = np.where(np.isfinite(oo), oo, cc)
        hh = np.where(np.isfinite(hh), hh, np.maximum(oo, cc))
        ll = np.where(np.isfinite(ll), ll, np.minimum(oo, cc))
        hh = np.maximum(hh, np.maximum(oo, cc))
        ll = np.minimum(ll, np.minimum(oo, cc))
        oo = np.where(tradable, oo, cc)
        hh = np.where(tradable, hh, cc)
        ll = np.where(tradable, ll, cc)
        fwd = float(row.fwd)
        path = PickPath(
            d=int(row.d),
            symbol=str(row.symbol),
            buy_d=int(row.buy_d),
            entry=float(entry),
            open_=oo,
            high=hh,
            low=ll,
            close=cc,
            tradable=tradable,
            fwd=fwd,
            mfe=float(np.max(hh) / entry - 1.0),
            mae=float(np.min(ll) / entry - 1.0),
        )
        by_d.setdefault(path.d, []).append(path)
    return by_d, dropped


def effective_stop(
    entry: float,
    config: TrailConfig,
    trail_active: bool,
    high_water: float,
) -> tuple[float, str]:
    stop_level = entry * (1.0 - config.sl) if config.sl is not None else -np.inf
    trail_level = high_water * (1.0 - config.trail) if trail_active else -np.inf
    if trail_level > stop_level:
        return trail_level, "trail"
    return stop_level, "stop" if config.sl is not None else "time"


def simulate_pick(path: PickPath, config: TrailConfig) -> tuple[float, int, str]:
    entry = path.entry
    trail_active = config.activate is not None and config.activate <= 0.0
    high_water = entry
    horizon = len(path.close)
    for t in range(horizon):
        if not path.tradable[t]:
            continue
        effective, reason = effective_stop(entry, config, trail_active, high_water)
        if config.mode == "close":
            if path.close[t] <= effective:
                return path.close[t] / entry - 1.0, t + 1, reason
            high_water = max(high_water, path.close[t])
        else:
            if path.open_[t] <= effective:
                return path.open_[t] / entry - 1.0, t + 1, reason
            if path.low[t] <= effective:
                return effective / entry - 1.0, t + 1, reason
            high_water = max(high_water, path.high[t])
        if config.activate is not None and not trail_active:
            if high_water >= entry * (1.0 + config.activate):
                trail_active = True
    return path.close[-1] / entry - 1.0, horizon, "time"


def simulate_config(
    paths_by_d: dict[int, list[PickPath]], config: TrailConfig
) -> pd.DataFrame:
    rows = []
    for d, paths in paths_by_d.items():
        for path in paths:
            ret, hold, reason = simulate_pick(path, config)
            rows.append(
                {
                    "d": d,
                    "symbol": path.symbol,
                    "buy_d": path.buy_d,
                    "fwd": path.fwd,
                    "ret": ret,
                    "hold": hold,
                    "reason": reason,
                    "mfe": path.mfe,
                    "mae": path.mae,
                }
            )
    return pd.DataFrame(rows)


def period_frame(sim: pd.DataFrame, dates: list[int], horizon: int) -> pd.DataFrame:
    selected = dates[::horizon]
    grouped = {int(d): g for d, g in sim.groupby("d")}
    previous: set[str] | None = None
    rows = []
    for d in selected:
        group = grouped.get(int(d))
        if group is None or group.empty:
            continue
        names = set(group["symbol"])
        turnover = (
            1.0 if previous is None else 1.0 - len(names & previous) / float(len(names))
        )
        previous = names
        gross = float(group["ret"].mean())
        rows.append(
            {
                "d": int(d),
                "year": int(d) // 10000,
                "gross": gross,
                "net": gross - turnover * COST,
                "turnover": turnover,
                "n": len(group),
                "avg_hold": float(group["hold"].mean()),
                "stop_share": float((group["reason"] == "stop").mean()),
                "trail_share": float((group["reason"] == "trail").mean()),
                "time_share": float((group["reason"] == "time").mean()),
                "win_pick": float((group["ret"] > 0).mean()),
            }
        )
    return pd.DataFrame(rows)


def summarize(periods: pd.DataFrame, horizon: int) -> dict[str, float]:
    if periods.empty:
        return {}
    gross = periods["gross"].to_numpy(dtype=float)
    net = periods["net"].to_numpy(dtype=float)
    total_gross = float(np.prod(1.0 + gross) - 1.0)
    total_net = float(np.prod(1.0 + net) - 1.0)
    years = max(len(periods) * horizon, 1) / 252.0

    def annualize(total: float) -> float:
        return float((1.0 + total) ** (1.0 / years) - 1.0)

    equity = np.concatenate(([1.0], np.cumprod(1.0 + gross)))
    peak = np.maximum.accumulate(equity)
    maxdd = float((equity / peak - 1.0).min())
    equity_net = np.concatenate(([1.0], np.cumprod(1.0 + net)))
    peak_net = np.maximum.accumulate(equity_net)
    maxdd_net = float((equity_net / peak_net - 1.0).min())
    ann_net = annualize(total_net)
    return {
        "periods": len(periods),
        "avg_names": float(periods["n"].mean()),
        "avg_hold": float(periods["avg_hold"].mean()),
        "total_gross": total_gross,
        "total_net": total_net,
        "ann_gross": annualize(total_gross),
        "ann_net": ann_net,
        "maxdd": maxdd,
        "maxdd_net": maxdd_net,
        "ann_per_dd": ann_net / abs(maxdd) if maxdd < 0 else float("nan"),
        "avg_turnover": float(periods["turnover"].mean()),
        "stop_share": float(periods["stop_share"].mean()),
        "trail_share": float(periods["trail_share"].mean()),
        "time_share": float(periods["time_share"].mean()),
        "win_pick": float(periods["win_pick"].mean()),
        "win_period": float((periods["gross"] > 0).mean()),
    }


def summarize_all(sim: pd.DataFrame, dates: list[int], horizon: int) -> list[dict]:
    rows = []
    full = period_frame(sim, dates, horizon)
    rows.append({"scope": "全期", **summarize(full, horizon)})
    for year in sorted({int(d) // 10000 for d in dates}):
        year_dates = [d for d in dates if int(d) // 10000 == year]
        frame = period_frame(sim, year_dates, horizon)
        rows.append({"scope": str(year), **summarize(frame, horizon)})
    return rows


def mfe_mae_table(sim: pd.DataFrame) -> pd.DataFrame:
    data = sim.copy()
    data["hit_sl7"] = data["mae"] <= -0.07
    data["hit_trail10"] = data["mfe"] >= 0.10
    rows = []
    for label, mask in [
        ("全部", pd.Series(True, index=data.index)),
        ("曾跌破-7%", data["hit_sl7"]),
        ("未跌破-7%", ~data["hit_sl7"]),
        ("曾涨过+10%", data["hit_trail10"]),
        ("未涨过+10%", ~data["hit_trail10"]),
    ]:
        part = data[mask]
        rows.append(
            {
                "group": label,
                "n": len(part),
                "share": len(part) / len(data) if len(data) else float("nan"),
                "fwd_mean": float(part["fwd"].mean()),
                "fwd_median": float(part["fwd"].median()),
                "fwd_win": float((part["fwd"] > 0).mean()),
                "mfe_mean": float(part["mfe"].mean()),
                "mae_mean": float(part["mae"].mean()),
            }
        )
    return pd.DataFrame(rows)


def exit_diagnostics(sim: pd.DataFrame) -> pd.DataFrame:
    data = sim.copy()
    data["year"] = data["d"] // 10000
    scopes = [("全期", data)]
    scopes.extend((str(year), part) for year, part in data.groupby("year"))
    rows = []
    for scope, frame in scopes:
        groups = [("all", frame)]
        groups.extend((reason, part) for reason, part in frame.groupby("reason"))
        for group, part in groups:
            rows.append(
                {
                    "scope": scope,
                    "group": group,
                    "n": len(part),
                    "share": len(part) / len(frame),
                    "ret_mean": float(part["ret"].mean()),
                    "fwd_mean": float(part["fwd"].mean()),
                    "delta_mean": float((part["ret"] - part["fwd"]).mean()),
                    "hold_mean": float(part["hold"].mean()),
                }
            )
    return pd.DataFrame(rows)


def breach_counterfactual(sim: pd.DataFrame) -> pd.DataFrame:
    """按“是否曾跌破某阈值”分组，比较持有到期收益与止损价。"""
    data = sim.copy()
    data["year"] = data["d"] // 10000
    scopes = [("全期", data)]
    scopes.extend((str(year), part) for year, part in data.groupby("year"))
    rows = []
    for scope, frame in scopes:
        for threshold in (0.05, 0.07, 0.10, 0.15, 0.20):
            part = frame[frame["mae"] <= -threshold]
            if part.empty:
                continue
            rows.append(
                {
                    "scope": scope,
                    "threshold": threshold,
                    "n": len(part),
                    "share": len(part) / len(frame),
                    "fwd_mean": float(part["fwd"].mean()),
                    "fwd_median": float(part["fwd"].median()),
                    "fwd_win": float((part["fwd"] > 0).mean()),
                    "stop_minus_hold": float(part["fwd"].mean() + threshold),
                }
            )
    return pd.DataFrame(rows)


def md_table(frame: pd.DataFrame, floatfmt: str = ".2f") -> str:
    columns = list(frame.columns)
    lines = [
        "| " + " | ".join(str(c) for c in columns) + " |",
        "|" + "|".join("---" for _ in columns) + "|",
    ]
    for row in frame.itertuples(index=False):
        cells = []
        for value in row:
            if isinstance(value, (float, np.floating)):
                cells.append(format(float(value), floatfmt))
            else:
                cells.append(str(value))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def build_report(
    strategy: str,
    picks: pd.DataFrame,
    grid: pd.DataFrame,
    main_sim: pd.DataFrame,
    diagnostics: pd.DataFrame,
    mfe: pd.DataFrame,
    breach: pd.DataFrame,
    horizon: int,
    dropped: int,
    baseline_diff: float,
) -> str:
    main = grid[
        grid["config"].isin(
            [
                "baseline",
                "sl7",
                "sl5",
                "sl10",
                "sl7_a10_t5",
                "sl7_a10_t3",
                "sl7_a15_t5",
                "sl7_a7_t5",
                "sl7_a5_t5",
                "pure_trail7",
                "pure_trail10",
                "sl7_close",
                "sl7_close_a10_t5",
                "sl7_close_a15_t5",
                "trail_close_a10_t5",
                "trail_close_a15_t3",
            ]
        )
    ].copy()
    main = main[main["scope"].isin(["全期", "2025", "2026"])]
    pivot_rows = []
    for config in main["config"].unique():
        part = main[main["config"] == config].set_index("scope")
        if not {"全期", "2025", "2026"}.issubset(part.index):
            continue
        pivot_rows.append(
            {
                "配置": part["label"].iloc[0],
                "全期年化": pct(float(part.at["全期", "ann_net"])),
                "全期回撤": pct(float(part.at["全期", "maxdd"])),
                "2025年化": pct(float(part.at["2025", "ann_net"])),
                "2025回撤": pct(float(part.at["2025", "maxdd"])),
                "2026年化": pct(float(part.at["2026", "ann_net"])),
                "2026回撤": pct(float(part.at["2026", "maxdd"])),
                "年化/回撤": format(float(part.at["全期", "ann_per_dd"]), ".2f"),
                "止损占比": pct(float(part.at["全期", "stop_share"])),
                "移动止盈占比": pct(float(part.at["全期", "trail_share"])),
            }
        )
    main_table = pd.DataFrame(pivot_rows)

    diag_table = diagnostics.copy()
    diag_table["ret_mean"] = diag_table["ret_mean"].map(pct)
    diag_table["fwd_mean"] = diag_table["fwd_mean"].map(pct)
    diag_table["delta_mean"] = diag_table["delta_mean"].map(pct)
    diag_table["hold_mean"] = diag_table["hold_mean"].map(lambda x: f"{x:.1f}天")

    mfe_table = mfe.copy()
    for column in ("share", "fwd_mean", "fwd_win", "mfe_mean", "mae_mean"):
        mfe_table[column] = mfe_table[column].map(pct)
    mfe_table["fwd_median"] = mfe_table["fwd_median"].map(pct)

    breach_table = breach.copy()
    for column in ("share", "fwd_mean", "fwd_median", "fwd_win", "stop_minus_hold"):
        breach_table[column] = breach_table[column].map(pct)
    breach_table["threshold"] = breach_table["threshold"].map(
        lambda value: f"-{value * 100:.0f}%"
    )

    lines = [
        "# 单票 -7% 止损 + 移动止盈叠加回测",
        "",
        f"- 策略：{strategy}",
        f"- 持仓期：{horizon} 个交易日；signal 日 T 收盘，T+1 收盘买入，每 {horizon} 日换仓",
        f"- 样本：{len(picks)} 笔持仓、{picks['d'].nunique()} 个信号日；"
        f"有效价格路径 {len(main_sim)} 笔（丢弃 {dropped} 笔）",
        "- 成本：往返 0.2%，按换手率扣除；提前卖出后现金留到下一换仓日",
        f"- 基线校验：模拟持有到期收益与原 fwd 最大绝对误差 {baseline_diff:.2e}",
        "",
        "## 结论",
        "",
    ]
    baseline = main[main["config"] == "baseline"].set_index("scope")
    sl7 = main[main["config"] == "sl7"].set_index("scope")
    both = main[main["config"] == "sl7_a10_t5"].set_index("scope")
    close_both = main[main["config"] == "sl7_close_a10_t5"].set_index("scope")
    if not baseline.empty and not both.empty:
        lines.extend(
            [
                f"- 不止损基线：全期年化 {pct(float(baseline.at['全期', 'ann_net']))}、"
                f"回撤 {pct(float(baseline.at['全期', 'maxdd']))}；"
                f"2025 {pct(float(baseline.at['2025', 'ann_net']))} / "
                f"{pct(float(baseline.at['2025', 'maxdd']))}；"
                f"2026 {pct(float(baseline.at['2026', 'ann_net']))} / "
                f"{pct(float(baseline.at['2026', 'maxdd']))}。",
                f"- 仅 -7% 止损：全期年化 {pct(float(sl7.at['全期', 'ann_net']))}、"
                f"回撤 {pct(float(sl7.at['全期', 'maxdd']))}；"
                f"2025 {pct(float(sl7.at['2025', 'ann_net']))}；"
                f"2026 {pct(float(sl7.at['2026', 'ann_net']))}。",
                f"- -7% 止损 + +10% 激活 / 回撤 5%（日内）：全期年化 "
                f"{pct(float(both.at['全期', 'ann_net']))}、回撤 "
                f"{pct(float(both.at['全期', 'maxdd']))}；"
                f"2025 {pct(float(both.at['2025', 'ann_net']))}；"
                f"2026 {pct(float(both.at['2026', 'ann_net']))}。",
            ]
        )
        if not close_both.empty:
            lines.append(
                f"- 同配置改为收盘价触发：全期年化 "
                f"{pct(float(close_both.at['全期', 'ann_net']))}、回撤 "
                f"{pct(float(close_both.at['全期', 'maxdd']))}；"
                f"2025 {pct(float(close_both.at['2025', 'ann_net']))}；"
                f"2026 {pct(float(close_both.at['2026', 'ann_net']))}。"
            )
        lines.extend(
            [
                "",
                "判断标准：只有在 2025 和 2026 两个年份都改善（或至少不恶化）"
                "年化/回撤，才算稳定有效；只在某一年有效、或参数邻域不连续，"
                "视为过拟合。",
                "",
            ]
        )
    lines.extend(
        [
            "## 主表（全期 / 2025 / 2026）",
            "",
            md_table(main_table),
            "",
            "## 逐笔退出诊断（主配置：-7% 止损 + +10% 激活 / 回撤 5%）",
            "",
            md_table(diag_table),
            "",
            "说明：`fwd_mean` 是同一批票如果一直持有到期的平均收益，"
            "`ret_mean` 是叠加止损/止盈后的实际平均收益，`delta_mean` 为两者之差。"
            "止损组的 delta 若为正，说明止损躲开了后续下跌；若为负，说明卖在反弹前。",
            "",
            "## 事后 MFE / MAE 分组",
            "",
            md_table(mfe_table),
            "",
            "说明：MFE=max(high)/entry-1，MAE=min(low)/entry-1，"
            "按整个持有窗口统计；用于回答“-7% 和 +10% 到底有多少票会碰到”。",
            "",
            "## 按跌破阈值的事后反事实",
            "",
            md_table(breach_table),
            "",
            "说明：挑出持有期内最低价曾跌破某阈值的票，看它们如果不止损、"
            "一直持有到期的平均收益 `fwd_mean`。`stop_minus_hold` = "
            "`fwd_mean` + 阈值；若为正，说明在阈值处止损比持有到期更差"
            "（卖在反弹前），若为负则止损有效。",
            "",
            "## 执行口径与限制",
            "",
            "1. 以日线 open/high/low 模拟：日内触发按止损/止盈价成交；"
            "跳空低开按 open 成交。没有分钟级路径，无法判断同一天先高后低还是先低后高。",
            "2. 移动止盈的 high-water 只影响下一交易日，避免当天冲高回落被同日误判。",
            "3. A 股 T+1、跌停/停牌可能卖不出；本回测未模拟跌停排队，"
            "真实止损滑点可能更大。",
            "4. 提前卖出后的现金不补票，收益按等权组合中该仓位转为现金计算；"
            "这通常会拉低年化，属于保守处理。",
            "5. 换手成本沿用原口径，未额外收取止损带来的冲击成本。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--picks", required=True, help="picks_*.csv")
    parser.add_argument("--strategy", default="Top20投票 k=3")
    parser.add_argument("--horizon", type=int, default=DEFAULT_HORIZON)
    parser.add_argument("--start", type=int, default=None)
    parser.add_argument("--end", type=int, default=None)
    parser.add_argument("--out-dir", default="/tmp/stop_trail_out")
    parser.add_argument("--memory", default="4GB")
    parser.add_argument("--threads", type=int, default=4)
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    picks = load_picks(args.picks, args.start, args.end)
    dates = sorted(int(d) for d in picks["d"].unique())
    first_ts = pd.Timestamp(str(min(dates)))
    last_ts = pd.Timestamp(str(max(int(d) for d in picks["buy_d"])))
    lo = int((first_ts - pd.Timedelta(days=10)).strftime("%Y%m%d"))
    hi = int((last_ts + pd.Timedelta(days=60)).strftime("%Y%m%d"))

    con = E.connect(args.memory, args.threads)
    close_panel, arrays = load_arrays(con, lo, hi)
    arrays["symbols"] = close_panel.columns.to_numpy()
    paths_by_d, dropped = build_paths(picks, close_panel.index, arrays, args.horizon)

    configs = build_configs()
    grid_rows = []
    main_sim: pd.DataFrame | None = None
    for config in configs:
        sim = simulate_config(paths_by_d, config)
        for row in summarize_all(sim, dates, args.horizon):
            grid_rows.append(
                {
                    "strategy": args.strategy,
                    "config": config.name,
                    "label": config.label,
                    "horizon": args.horizon,
                    **row,
                }
            )
        if config.name == "sl7_a10_t5":
            main_sim = sim
    grid = pd.DataFrame(grid_rows)
    grid.to_csv(out_dir / "stop_trail_grid.csv", index=False)
    yearly = grid[grid["scope"].isin(["2025", "2026"])].copy()
    yearly.to_csv(out_dir / "stop_trail_yearly.csv", index=False)

    if main_sim is None:
        raise RuntimeError("主配置 sl7_a10_t5 未运行")
    main_sim.to_csv(
        out_dir / f"stop_trail_picks_{args.strategy.replace(' ', '_')}.csv",
        index=False,
    )
    diagnostics = exit_diagnostics(main_sim)
    diagnostics.to_csv(out_dir / "stop_trail_exit_diagnostics.csv", index=False)
    mfe = mfe_mae_table(main_sim)
    mfe.to_csv(out_dir / "stop_trail_mfe_mae.csv", index=False)

    # 用 baseline 配置重算一次，确保与 fwd 对齐（主配置不是 baseline）。
    base_config = next(c for c in configs if c.name == "baseline")
    base_sim = simulate_config(paths_by_d, base_config)
    baseline_diff = float(
        np.max(np.abs(base_sim["ret"].to_numpy() - base_sim["fwd"].to_numpy()))
    )
    breach = breach_counterfactual(base_sim)
    breach.to_csv(out_dir / "stop_trail_breach_counterfactual.csv", index=False)
    report = build_report(
        args.strategy,
        picks,
        grid,
        main_sim,
        diagnostics,
        mfe,
        breach,
        args.horizon,
        dropped,
        baseline_diff,
    )
    (out_dir / "REPORT.md").write_text(report, encoding="utf-8")
    print(f"[done] {out_dir}")
    print(f"picks={len(picks)} paths={len(main_sim)} dropped={dropped}")
    print(f"baseline fwd max abs diff={baseline_diff:.3e}")


if __name__ == "__main__":
    main()
