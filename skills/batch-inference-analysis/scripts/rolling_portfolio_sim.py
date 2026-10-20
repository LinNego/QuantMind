"""滚动持仓组合模拟：分数均值过滤 + TopN + T+5/T+10 + 每日监控替换。

预注册口径（2025 设计 / 2026 OOS，先定规则再跑，不用 2026 调参）：

- 综合分 S：同周期 13 个模型每日截面百分位（在 ADV>=0.2 亿池内）的均值；
  至少 8 个模型有分才有效。
- 入场：S_t >= A_t，A_t = 该股过去 20 个交易日的 S 均值（不含当日，至少 10 个观测）。
- 排序：空位按 S 降序取 TopN（Top20 / Top50）。
- 执行：t 收盘决策，t+1 收盘成交；卖出与买入同一时点。
- 持有：H 个交易日（T+5 用 T+5 综合分，T+10 用 T+10 综合分）。
- 提前退出（可关）：S_t < A_t - score_buffer（分数走弱）；
  或收盘价较入场价 <= -price_stop（价格止损）。
- 替补：退出后的空位用当日符合条件的下一名候选补上，继续滚动。
- 资金：初始 1000 万，N 个等权槽位；单票目标 = NAV*(1-cash_buffer)/N；
  1% ADV 上限（ADV 为 20 日均额，单位万元）；现金缓冲 2%。
- 成本：单边 0.1%，按成交名义金额扣。
- 涨跌停：买入日 blocked_long 跳过；卖出日 blocked_short 延迟到下一日。
- 建仓：预热后每日买 ceil(N/H) 只，H 个交易日铺满，避免一次性全仓。

用法：
    python rolling_portfolio_sim.py --mode main --out-dir /tmp/rolling
    python rolling_portfolio_sim.py --mode ext --out-dir /tmp/rolling
"""

from __future__ import annotations

import argparse
import json
import math
import re
import time
from dataclasses import dataclass, replace
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
from scipy import stats

import top20_model_eval as E

ADV_FLOOR_WAN = 2000.0  # 0.2 亿，宽口径可投池
MIN_MODELS = 8
CASH_BUFFER = 0.02
COST_SIDE = 0.001
CAPITAL = 10_000_000.0
TRADING_DAYS = 252


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def normalize_symbol_sql(column: str = "symbol") -> str:
    return (
        f"CASE WHEN substr({column}, 1, 2) IN ('SH', 'SZ', 'BJ') "
        f"THEN substr({column}, 3) || '.' || substr({column}, 1, 2) "
        f"ELSE {column} END"
    )


# ---------------------------------------------------------------------------
# 数据与分数
# ---------------------------------------------------------------------------


def load_panels(
    con: duckdb.DuckDBPyConnection, lo: int, hi: int
) -> tuple[pd.DataFrame, pd.DataFrame, np.ndarray, np.ndarray]:
    """收盘价、20 日均额（万元）、买入/卖出 blocked 面板。"""
    close = E.load_panel(con, "close", lo, hi)
    amount = E.load_panel(con, "amount", lo, hi)
    adv = (
        amount.rolling(20, min_periods=5)
        .mean()
        .reindex(index=close.index, columns=close.columns)
    )

    mask = con.execute(
        f"SELECT CAST(dt AS INTEGER) AS d, symbol, blocked_long, blocked_short "
        f"FROM read_parquet('{E.MASK_FILE}') WHERE dt BETWEEN {lo} AND {hi}"
    ).fetchdf()
    blocked: dict[str, np.ndarray] = {}
    for col in ("blocked_long", "blocked_short"):
        pv = mask.pivot_table(index="d", columns="symbol", values=col, aggfunc="last")
        pv = pv.reindex(index=close.index, columns=close.columns)
        blocked[col] = pv.fillna(False).to_numpy(dtype=bool)
    return close, adv, blocked["blocked_long"], blocked["blocked_short"]


def register_adv(con: duckdb.DuckDBPyConnection, adv: pd.DataFrame) -> None:
    long = adv.stack().rename("adv").reset_index()
    long.columns = ["d", "symbol", "adv"]
    long["d"] = long["d"].astype("int64")
    con.register("adv_long_sim", long)
    con.execute(
        "CREATE OR REPLACE TABLE adv_tbl_sim AS "
        "SELECT symbol, d, adv FROM adv_long_sim "
        "WHERE adv IS NOT NULL AND adv >= ?",
        [ADV_FLOOR_WAN],
    )


def register_fwd(
    con: duckdb.DuckDBPyConnection, close: pd.DataFrame, horizon: int
) -> None:
    fwd = E.build_fwd_long(close, horizon)
    name = f"fwd_{horizon}_tbl"
    con.register(name + "_reg", fwd)
    con.execute(f"CREATE OR REPLACE TABLE {name} AS SELECT * FROM {name}_reg")


def model_score_frame(
    con: duckdb.DuckDBPyConnection,
    model: dict,
    start: str,
    end: str,
    with_fwd: int | None = None,
) -> pd.DataFrame:
    """单模型截面百分位；with_fwd 时顺带带出该周期的前向收益（诊断用）。"""
    path = f"{E.PRED_ROOT}/{model['model_id']}/pred.parquet"
    fwd_join = ""
    fwd_col = ""
    if with_fwd is not None:
        fwd_join = (
            f"LEFT JOIN fwd_{with_fwd}_tbl f ON r.d = f.d AND r.symbol = f.symbol"
        )
        fwd_col = ", f.fwd"
    sql = f"""
    WITH p AS (
      SELECT {normalize_symbol_sql()} AS symbol,
             CAST(strftime(trade_date, '%Y%m%d') AS INTEGER) AS d,
             max(CAST(pred AS DOUBLE)) AS pred
      FROM read_parquet('{path}')
      WHERE split = 'test' AND pred IS NOT NULL
        AND trade_date BETWEEN DATE '{start}' AND DATE '{end}'
      GROUP BY 1, 2
    ), j AS (
      SELECT p.symbol, p.d, p.pred
      FROM p JOIN adv_tbl_sim a ON p.symbol = a.symbol AND p.d = a.d
    ), r AS (
      SELECT symbol, d,
             percent_rank() OVER (PARTITION BY d ORDER BY pred) AS pct
      FROM j
    )
    SELECT r.d, r.symbol, r.pct{fwd_col}
    FROM r {fwd_join}
    """
    return con.execute(sql).fetchdf()


def build_composite(
    con: duckdb.DuckDBPyConnection,
    models: list[dict],
    start: str,
    end: str,
    min_models: int = MIN_MODELS,
    diag_horizon: int | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """综合分矩阵 + 覆盖数矩阵 + 单模型诊断（IC / Top20 超额）。"""
    total: pd.DataFrame | None = None
    count: pd.DataFrame | None = None
    diag_rows: list[dict] = []
    for model in models:
        frame = model_score_frame(con, model, start, end, with_fwd=diag_horizon)
        pv = frame.pivot(index="d", columns="symbol", values="pct")
        total = pv if total is None else total.add(pv, fill_value=0.0)
        c = pv.notna().astype("int16")
        count = c if count is None else count.add(c, fill_value=0)
        if diag_horizon is not None:
            diag_rows.append(model_diag(frame, model, diag_horizon))
        log(
            f"score {model['model_type']:>14} T+{model['horizon']} "
            f"{pv.shape[0]}d x {pv.shape[1]}s"
        )
    if total is None or count is None:
        raise RuntimeError("没有可用模型")
    score = total / count.where(count >= min_models)
    diag = pd.DataFrame(diag_rows) if diag_rows else pd.DataFrame()
    return score, count, diag


def model_diag(frame: pd.DataFrame, model: dict, horizon: int) -> dict:
    """单模型日频 rank IC 与 Top20 前向超额（按周期非重叠采样年化）。"""
    df = frame.dropna(subset=["pct", "fwd"]).copy()
    if df.empty:
        return {
            "model_type": model["model_type"],
            "horizon": horizon,
            "model_id": model["model_id"],
        }
    df["rp"] = df.groupby("d")["pct"].rank(method="average")
    df["rf"] = df.groupby("d")["fwd"].rank(method="average")
    # 分组 Pearson 相关（对秩即 Spearman），纯向量化，避免 groupby.apply 版本差异。
    grp = df.groupby("d")
    mx = grp["rp"].transform("mean")
    my = grp["rf"].transform("mean")
    cov = ((df["rp"] - mx) * (df["rf"] - my)).groupby(df["d"]).sum()
    vx = ((df["rp"] - mx) ** 2).groupby(df["d"]).sum()
    vy = ((df["rf"] - my) ** 2).groupby(df["d"]).sum()
    denom = np.sqrt(vx * vy)
    ic = (cov / denom.where(denom > 0)).dropna()
    df = df.sort_values(["d", "pct"], ascending=[True, False])
    df["rk"] = df.groupby("d").cumcount()
    top = df[df["rk"] < 20].groupby("d")["fwd"].mean()
    uni = df.groupby("d")["fwd"].mean()
    excess = (top - uni).dropna()
    sel = excess.iloc[::horizon]
    ann_excess = (
        float((1.0 + sel.mean()) ** (TRADING_DAYS / horizon) - 1.0)
        if len(sel)
        else float("nan")
    )
    ic_std = float(ic.std(ddof=1)) if len(ic) > 1 else float("nan")
    return {
        "model_type": model["model_type"],
        "horizon": horizon,
        "model_id": model["model_id"],
        "ic_mean": float(ic.mean()) if len(ic) else float("nan"),
        "ic_ir": (
            float(ic.mean() / ic_std * math.sqrt(TRADING_DAYS))
            if ic_std and math.isfinite(ic_std) and ic_std > 0
            else float("nan")
        ),
        "ic_t": (
            float(ic.mean() / ic_std * math.sqrt(len(ic)))
            if ic_std and math.isfinite(ic_std) and ic_std > 0
            else float("nan")
        ),
        "ic_pos_rate": float((ic > 0).mean()) if len(ic) else float("nan"),
        "top20_excess_ann": ann_excess,
        "days": int(len(ic)),
    }


def score_average(
    score: pd.DataFrame, window: int = 20, min_periods: int = 10
) -> pd.DataFrame:
    """过去 window 个交易日均值，不含当日（避免当日分数抬高自己的基准）。"""
    return score.shift(1).rolling(window, min_periods=min_periods).mean()


# ---------------------------------------------------------------------------
# 市场状态
# ---------------------------------------------------------------------------


def market_daily(
    close: pd.DataFrame, adv: pd.DataFrame, floor_wan: float = ADV_FLOOR_WAN
) -> pd.Series:
    """宽口径等权市场日收益（只用当日可投池，ADV 前一日已知）。"""
    ret = close.pct_change()
    ok = (adv.shift(1) >= floor_wan).to_numpy()
    vals = ret.to_numpy(dtype=float)
    out = np.full(len(close), np.nan)
    for i in range(1, len(close)):
        row = vals[i][ok[i]]
        row = row[np.isfinite(row)]
        if row.size:
            out[i] = float(row.mean())
    return pd.Series(out, index=close.index)


# ---------------------------------------------------------------------------
# 模拟
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    label: str
    top_n: int
    hold: int
    score_exit: bool = True
    score_buffer: float = 0.03
    price_stop: float | None = None
    min_hold: int = 1
    use_avg_filter: bool = True
    extend: bool = False
    max_extend: int = 1
    entry_margin: float = 0.0
    entry_floor: float | None = None
    market_filter: bool = False
    market_dd: float = -0.05
    market_exposure: float = 0.5
    cash_buffer: float = CASH_BUFFER
    cost_side: float = COST_SIDE
    capital: float = CAPITAL
    use_blocked_short: bool = True


@dataclass
class Position:
    j: int
    symbol: str
    entry_i: int
    first_entry_i: int
    entry_price: float
    notional: float
    buy_cost: float
    entry_score: float
    entry_avg: float
    last_price: float
    extensions: int = 0
    mfe: float = 0.0
    mae: float = 0.0


@dataclass
class SimResult:
    config: Config
    nav: pd.Series
    daily: pd.DataFrame
    trades: pd.DataFrame
    stats: dict
    yearly: pd.DataFrame
    ic: pd.Series | None = None


def simulate(
    cfg: Config,
    close: pd.DataFrame,
    adv: pd.DataFrame,
    score: pd.DataFrame,
    avg: pd.DataFrame,
    blocked_long: np.ndarray,
    blocked_short: np.ndarray,
    start_i: int,
    end_i: int,
    market20: pd.Series,
) -> SimResult:
    dates = close.index.to_numpy(dtype="int64")
    symbols = close.columns.to_numpy()
    px = close.to_numpy(dtype=np.float64, copy=False)
    adv_arr = adv.to_numpy(dtype=np.float64, copy=False)
    s_arr = score.to_numpy(dtype=np.float64, copy=False)
    a_arr = avg.to_numpy(dtype=np.float64, copy=False)
    mkt20 = market20.to_numpy(dtype=np.float64, copy=False)
    T, S = px.shape

    cash = cfg.capital
    positions: dict[int, Position] = {}
    nav_rows: list[dict] = [
        {
            "date": int(dates[start_i - 1]),
            "nav": float(cfg.capital),
            "cash": float(cfg.capital),
            "invested": 0.0,
            "n_pos": 0,
            "buy_notional": 0.0,
            "sell_notional": 0.0,
            "cost": 0.0,
            "risk_off": False,
        }
    ]
    trades: list[dict] = []
    ramp_start: int | None = None
    last_date = int(dates[end_i])

    for i in range(start_i, end_i + 1):
        d = i - 1
        # 决策日 NAV（持仓按上一交易日收盘盯市）
        nav_d = cash + sum(
            p.notional * p.last_price / p.entry_price for p in positions.values()
        )
        target = nav_d * (1.0 - cfg.cash_buffer) / cfg.top_n
        adv_gate = target / 100.0 if target > 0 else float("inf")
        risk_off = bool(
            cfg.market_filter and math.isfinite(mkt20[d]) and mkt20[d] < cfg.market_dd
        )

        # 当日可投候选（含已持有；用于展期判定与排序）
        s_row = s_arr[d]
        a_row = a_arr[d]
        with np.errstate(invalid="ignore"):
            ok = np.isfinite(s_row) & (adv_arr[d] >= adv_gate)
            if cfg.use_avg_filter:
                ok &= np.isfinite(a_row) & (s_row >= a_row + cfg.entry_margin)
        if cfg.entry_floor is not None:
            ok &= s_row >= cfg.entry_floor
        js = np.flatnonzero(ok)
        order = js[np.argsort(-s_row[js], kind="stable")] if js.size else js
        top_set = set(order[: cfg.top_n].tolist())

        # 卖出决策
        sells: list[tuple[int, bool, bool, bool]] = []
        for j, pos in positions.items():
            hold_days = d - pos.entry_i
            due = hold_days >= cfg.hold - 1
            s = s_arr[d, j]
            a = a_arr[d, j]
            weak = bool(
                cfg.score_exit
                and cfg.use_avg_filter
                # hold_days 是决策日相对入场执行日的偏移；实际持有天数 = hold_days + 1。
                and hold_days >= cfg.min_hold - 1
                and math.isfinite(s)
                and math.isfinite(a)
                and s < a - cfg.score_buffer
            )
            p = px[d, j]
            stop = bool(
                cfg.price_stop is not None
                and math.isfinite(p)
                and p <= pos.entry_price * (1.0 - cfg.price_stop)
            )
            if (
                due
                and cfg.extend
                and pos.extensions < cfg.max_extend
                and not weak
                and j in top_set
            ):
                pos.entry_i = d
                pos.extensions += 1
                due = False
            if due or weak or stop:
                sells.append((j, due, weak, stop))

        sold_js = {j for j, _, _, _ in sells}
        buy_js = [int(j) for j in order if j not in positions and j not in sold_js]

        # 执行卖出（t+1 收盘；跌停/停牌延迟）
        sold_today: set[int] = set()
        sell_cost_total = 0.0
        for j, due, weak, stop in sells:
            pos = positions.get(j)
            if pos is None:
                continue
            p = px[i, j]
            if not math.isfinite(p):
                continue
            if cfg.use_blocked_short and blocked_short[i, j]:
                continue
            proceeds = pos.notional * p / pos.entry_price
            sell_cost = proceeds * cfg.cost_side
            sell_cost_total += sell_cost
            cash += proceeds - sell_cost
            gross_ret = p / pos.entry_price - 1.0
            net_ret = (proceeds - sell_cost - pos.notional - pos.buy_cost) / (
                pos.notional + pos.buy_cost
            )
            if stop and weak:
                reason = "score+stop"
            elif stop:
                reason = "stop"
            elif weak:
                reason = "score"
            elif due:
                reason = "time"
            else:
                reason = "other"
            trades.append(
                {
                    "symbol": pos.symbol,
                    "entry_d": int(dates[pos.first_entry_i]),
                    "exit_d": int(dates[i]),
                    "entry_i": pos.first_entry_i,
                    "exit_i": i,
                    "hold_days": i - pos.first_entry_i,
                    "entry_price": pos.entry_price,
                    "exit_price": p,
                    "gross_ret": gross_ret,
                    "net_ret": net_ret,
                    "notional": pos.notional,
                    "pnl": proceeds - sell_cost - pos.notional - pos.buy_cost,
                    "entry_score": pos.entry_score,
                    "entry_avg": pos.entry_avg,
                    "exit_score": float(s_arr[d, j]),
                    "exit_avg": float(a_arr[d, j]),
                    "mfe": pos.mfe,
                    "mae": pos.mae,
                    "exit_reason": reason,
                    "due": due,
                    "score_weak": weak,
                    "stop_hit": stop,
                    "extensions": pos.extensions,
                }
            )
            del positions[j]
            sold_today.add(j)

        # 执行买入（t+1 收盘；涨停/停牌跳过）
        vacancies = cfg.top_n - len(positions)
        if ramp_start is None and buy_js and vacancies > 0:
            ramp_start = i
        if ramp_start is not None and i - ramp_start < cfg.hold:
            max_buys = min(vacancies, math.ceil(cfg.top_n / cfg.hold))
        else:
            max_buys = vacancies
        if risk_off:
            max_buys = min(
                max_buys,
                max(0, math.ceil(cfg.top_n * cfg.market_exposure) - len(positions)),
            )
        bought = 0
        buy_notional = 0.0
        buy_cost_total = 0.0
        for j in buy_js:
            if bought >= max_buys:
                break
            p = px[i, j]
            if not math.isfinite(p):
                continue
            if blocked_long[i, j]:
                continue
            # 现金不够整槽时按可用现金买部分仓，而不是整天放弃（首个买点尤其常见）。
            notional = min(target, cash / (1.0 + cfg.cost_side))
            if notional <= 1.0:
                break
            cost = notional * cfg.cost_side
            cash -= notional + cost
            positions[j] = Position(
                j=j,
                symbol=str(symbols[j]),
                entry_i=i,
                first_entry_i=i,
                entry_price=p,
                notional=notional,
                buy_cost=cost,
                entry_score=float(s_arr[d, j]),
                entry_avg=float(a_arr[d, j]),
                last_price=p,
            )
            buy_notional += notional
            buy_cost_total += cost
            bought += 1

        # 盯市
        for pos in positions.values():
            p = px[i, pos.j]
            if math.isfinite(p):
                pos.last_price = p
                r = p / pos.entry_price - 1.0
                pos.mfe = max(pos.mfe, r)
                pos.mae = min(pos.mae, r)
        invested = sum(
            p.notional * p.last_price / p.entry_price for p in positions.values()
        )
        nav = cash + invested
        # 卖出成本在交易记录里已扣；这里只统计当日卖出名义额
        sell_notional = sum(
            t["notional"] * t["exit_price"] / t["entry_price"]
            for t in trades
            if t["exit_i"] == i
        )
        nav_rows.append(
            {
                "date": int(dates[i]),
                "nav": float(nav),
                "cash": float(cash),
                "invested": float(invested),
                "n_pos": len(positions),
                "buy_notional": float(buy_notional),
                "sell_notional": float(sell_notional),
                "cost": float(buy_cost_total + sell_cost_total),
                "risk_off": risk_off,
            }
        )

    # 期末清仓（按最后收盘；停牌用最后有效价）
    for j, pos in list(positions.items()):
        p = px[end_i, j]
        if not math.isfinite(p):
            p = pos.last_price
        proceeds = pos.notional * p / pos.entry_price
        sell_cost = proceeds * cfg.cost_side
        cash += proceeds - sell_cost
        net_ret = (proceeds - sell_cost - pos.notional - pos.buy_cost) / (
            pos.notional + pos.buy_cost
        )
        trades.append(
            {
                "symbol": pos.symbol,
                "entry_d": int(dates[pos.first_entry_i]),
                "exit_d": last_date,
                "entry_i": pos.first_entry_i,
                "exit_i": end_i,
                "hold_days": end_i - pos.first_entry_i,
                "entry_price": pos.entry_price,
                "exit_price": p,
                "gross_ret": p / pos.entry_price - 1.0,
                "net_ret": net_ret,
                "notional": pos.notional,
                "pnl": proceeds - sell_cost - pos.notional - pos.buy_cost,
                "entry_score": pos.entry_score,
                "entry_avg": pos.entry_avg,
                "exit_score": float(s_arr[end_i, j]),
                "exit_avg": float(a_arr[end_i, j]),
                "mfe": pos.mfe,
                "mae": pos.mae,
                "exit_reason": "end",
                "due": False,
                "score_weak": False,
                "stop_hit": False,
                "extensions": pos.extensions,
            }
        )
        del positions[j]
    if nav_rows:
        nav_rows[-1]["nav"] = float(cash)
        nav_rows[-1]["cash"] = float(cash)
        nav_rows[-1]["invested"] = 0.0
        nav_rows[-1]["n_pos"] = 0

    daily = pd.DataFrame(nav_rows).set_index("date").sort_index()
    nav = daily["nav"].astype(float)
    trades_df = pd.DataFrame(trades)
    stats = perf_stats(nav, daily, trades_df, cfg)
    yearly = yearly_stats(nav)
    return SimResult(
        config=cfg,
        nav=nav,
        daily=daily,
        trades=trades_df,
        stats=stats,
        yearly=yearly,
    )


# ---------------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------------


def perf_stats(
    nav: pd.Series, daily: pd.DataFrame, trades: pd.DataFrame, cfg: Config
) -> dict:
    ret = nav.pct_change().dropna()
    n = len(ret)
    if n == 0:
        return {"label": cfg.label, "days": 0}
    years = n / TRADING_DAYS
    total = float(nav.iloc[-1] / nav.iloc[0] - 1.0)
    ann = float((1.0 + total) ** (1.0 / years) - 1.0) if years > 0 else float("nan")
    eq = nav / nav.iloc[0]
    dd = float((eq / eq.cummax() - 1.0).min())
    sd = float(ret.std(ddof=1))
    sharpe = (
        float(ret.mean() / sd * math.sqrt(TRADING_DAYS)) if sd > 0 else float("nan")
    )
    down = ret[ret < 0]
    dsd = float(down.std(ddof=1)) if len(down) > 1 else float("nan")
    sortino = (
        float(ret.mean() / dsd * math.sqrt(TRADING_DAYS))
        if dsd and math.isfinite(dsd) and dsd > 0
        else float("nan")
    )
    avg_nav = float(daily["nav"].mean())
    turnover = (
        float(daily["buy_notional"].sum() / avg_nav / years)
        if avg_nav > 0 and years > 0
        else float("nan")
    )
    cost_drag = (
        float(daily["cost"].sum() / avg_nav / years)
        if avg_nav > 0 and years > 0
        else float("nan")
    )
    avg_exposure = (
        float(daily["invested"].mean() / avg_nav) if avg_nav > 0 else float("nan")
    )
    out = {
        "label": cfg.label,
        "days": n,
        "total_ret": total,
        "ann_ret": ann,
        "maxdd": dd,
        "sharpe": sharpe,
        "sortino": sortino,
        "calmar": float(ann / abs(dd)) if dd < 0 else float("nan"),
        "avg_names": float(daily["n_pos"].mean()),
        "avg_exposure": avg_exposure,
        "turnover_ann": turnover,
        "cost_drag_ann": cost_drag,
        "win_daily": float((ret > 0).mean()),
        "final_nav": float(nav.iloc[-1]),
    }
    if not trades.empty:
        out.update(
            {
                "n_trades": int(len(trades)),
                "trade_win": float((trades["net_ret"] > 0).mean()),
                "trade_avg_ret": float(trades["net_ret"].mean()),
                "trade_med_ret": float(trades["net_ret"].median()),
                "trade_avg_hold": float(trades["hold_days"].mean()),
                "exit_time": int((trades["exit_reason"] == "time").sum()),
                "exit_score": int((trades["exit_reason"] == "score").sum()),
                "exit_stop": int((trades["exit_reason"] == "stop").sum()),
                "exit_both": int((trades["exit_reason"] == "score+stop").sum()),
                "exit_end": int((trades["exit_reason"] == "end").sum()),
            }
        )
    return out


def yearly_stats(nav: pd.Series) -> pd.DataFrame:
    ret = nav.pct_change().dropna()
    rows = []
    for year, part in ret.groupby(ret.index // 10000):
        eq = (1.0 + part).cumprod()
        peak = np.maximum.accumulate(np.concatenate(([1.0], eq.to_numpy())))
        curve = np.concatenate(([1.0], eq.to_numpy()))
        dd = float((curve / peak - 1.0).min())
        n = len(part)
        total = float(eq.iloc[-1] - 1.0)
        ann = float((1.0 + total) ** (TRADING_DAYS / n) - 1.0) if n else float("nan")
        sd = float(part.std(ddof=1))
        rows.append(
            {
                "year": int(year),
                "days": n,
                "total_ret": total,
                "ann_ret": ann,
                "maxdd": dd,
                "sharpe": (
                    float(part.mean() / sd * math.sqrt(TRADING_DAYS))
                    if sd > 0
                    else float("nan")
                ),
                "win_daily": float((part > 0).mean()),
            }
        )
    return pd.DataFrame(rows)


def block_bootstrap(
    ret: np.ndarray, block: int = 20, n_boot: int = 1000, seed: int = 42
) -> tuple[list[float], list[float]]:
    """循环块 bootstrap：年化收益与 Sharpe 的 95% 区间。"""
    rng = np.random.default_rng(seed)
    t = len(ret)
    if t < block:
        return [float("nan"), float("nan")], [float("nan"), float("nan")]
    n_blocks = math.ceil(t / block)
    anns = np.empty(n_boot)
    srs = np.empty(n_boot)
    for k in range(n_boot):
        starts = rng.integers(0, t, n_blocks)
        idx = np.concatenate([(np.arange(s, s + block) % t) for s in starts])[:t]
        sample = ret[idx]
        anns[k] = float(np.prod(1.0 + sample) ** (TRADING_DAYS / t) - 1.0)
        sd = float(sample.std(ddof=1))
        srs[k] = float(sample.mean() / sd * math.sqrt(TRADING_DAYS)) if sd > 0 else 0.0
    return (
        [float(np.percentile(anns, 2.5)), float(np.percentile(anns, 97.5))],
        [float(np.percentile(srs, 2.5)), float(np.percentile(srs, 97.5))],
    )


def deflated_sharpe(ret: np.ndarray, trial_srs: list[float]) -> float:
    """Bailey & López de Prado 的 DSR；trial_srs 为同频（日频）Sharpe 列表。"""
    sr = float(ret.mean() / ret.std(ddof=1))
    t = len(ret)
    n = len(trial_srs)
    if n < 2 or t < 3:
        return float("nan")
    var_sr = float(np.var(trial_srs, ddof=1))
    if var_sr <= 0:
        return float("nan")
    gamma = 0.5772156649015329
    z1 = stats.norm.ppf(1.0 - 1.0 / n)
    z2 = stats.norm.ppf(1.0 - 1.0 / (n * math.e))
    sr0 = math.sqrt(var_sr) * ((1.0 - gamma) * z1 + gamma * z2)
    skew = float(stats.skew(ret, bias=False))
    # Bailey 公式里的 γ4 是非超额峰度；stats.kurtosis(fisher=True) 给的是超额峰度。
    kurt_excess = float(stats.kurtosis(ret, fisher=True, bias=False))
    kurt = kurt_excess + 3.0
    denom = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr
    if denom <= 0:
        return float("nan")
    z = (sr - sr0) * math.sqrt(t - 1.0) / math.sqrt(denom)
    return float(stats.norm.cdf(z))


# ---------------------------------------------------------------------------
# 网格与主流程
# ---------------------------------------------------------------------------


def main_grid(top_ns: list[int], holds: list[int]) -> list[Config]:
    configs: list[Config] = []
    exits = [
        ("time", False, None),
        ("score", True, None),
        ("score+stop8", True, 0.08),
    ]
    for n in top_ns:
        for h in holds:
            for name, use_score, stop in exits:
                configs.append(
                    Config(
                        label=f"Top{n} H{h} {name}",
                        top_n=n,
                        hold=h,
                        score_exit=use_score,
                        score_buffer=0.03,
                        price_stop=stop,
                    )
                )
    return configs


def ext_grid(base: Config) -> list[Config]:
    """在 2025 最优配置上做单维度预注册扩展，2026 只做 OOS 验证。"""
    out: list[Config] = []
    if base.score_exit:
        for buf in (0.0, 0.06):
            out.append(
                replace(
                    base,
                    label=f"{base.label} | buf{buf:g}",
                    score_buffer=buf,
                    price_stop=None,
                )
            )
    else:
        for buf in (0.0, 0.03, 0.06):
            out.append(
                replace(
                    base,
                    label=f"{base.label} | score{buf:g}",
                    score_exit=True,
                    score_buffer=buf,
                    price_stop=None,
                )
            )
    out.extend(
        [
            replace(
                base,
                label=f"{base.label} | extend",
                extend=True,
                max_extend=1,
            ),
            replace(
                base,
                label=f"{base.label} | mkt",
                market_filter=True,
            ),
            replace(
                base,
                label=f"{base.label} | margin0.05",
                entry_margin=0.05,
            ),
            replace(
                base,
                label=f"{base.label} | floor0.5",
                entry_floor=0.5,
            ),
            replace(
                base,
                label=f"{base.label} | floor0.8",
                entry_floor=0.8,
            ),
            replace(
                base,
                label=f"{base.label} | N10",
                top_n=10,
            ),
            replace(
                base,
                label=f"{base.label} | N30",
                top_n=30,
            ),
            replace(
                base,
                label=f"{base.label} | cost0.15%",
                cost_side=0.0015,
            ),
        ]
    )
    for stop in (0.06, 0.10, 0.12):
        out.append(
            replace(
                base,
                label=f"{base.label} | stop{int(stop * 100)}",
                price_stop=stop,
            )
        )
    return out


def supp_configs() -> list[Config]:
    """补充对照：最小持有、Top20 止损敏感性、去掉分数均值过滤的归因。"""
    out = [
        Config(label="Top20 H10 score", top_n=20, hold=10),
        Config(
            label="Top20 H10 score noavg",
            top_n=20,
            hold=10,
            score_exit=False,
            use_avg_filter=False,
        ),
        Config(
            label="Top50 H10 score noavg",
            top_n=50,
            hold=10,
            score_exit=False,
            use_avg_filter=False,
        ),
        Config(label="Top20 H10 score buf0.06", top_n=20, hold=10, score_buffer=0.06),
    ]
    for stop in (0.06, 0.10, 0.12):
        out.append(
            Config(
                label=f"Top20 H10 score stop{int(stop * 100)}",
                top_n=20,
                hold=10,
                price_stop=stop,
            )
        )
    for mh in (2, 3, 5):
        out.append(
            Config(
                label=f"Top20 H10 score mh{mh}",
                top_n=20,
                hold=10,
                min_hold=mh,
            )
        )
        out.append(
            Config(
                label=f"Top50 H10 score mh{mh}",
                top_n=50,
                hold=10,
                min_hold=mh,
            )
        )
    return out


def capacity_grid(base: Config) -> list[Config]:
    """同一规则的资金容量扫描：1% ADV 约束随资金放大收紧。"""
    return [
        replace(base, label=f"{base.label} | 资金{int(cap / 1e4)}万", capital=cap)
        for cap in (
            500_000.0,
            5_000_000.0,
            10_000_000.0,
            20_000_000.0,
            50_000_000.0,
            100_000_000.0,
        )
    ]


def slug(label: str) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "_", label).strip("_")


def run_configs(
    configs: list[Config],
    close: pd.DataFrame,
    adv: pd.DataFrame,
    scores: dict[int, pd.DataFrame],
    avgs: dict[int, pd.DataFrame],
    blocked_long: np.ndarray,
    blocked_short: np.ndarray,
    start_i: int,
    end_i: int,
    market20: pd.Series,
) -> list[SimResult]:
    results: list[SimResult] = []
    for cfg in configs:
        score = scores[cfg.hold]
        avg = avgs[cfg.hold]
        t0 = time.time()
        res = simulate(
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
        results.append(res)
        log(
            f"{cfg.label:<34} ann={res.stats['ann_ret']:>7.1%} "
            f"dd={res.stats['maxdd']:>7.1%} sr={res.stats['sharpe']:>5.2f} "
            f"names={res.stats['avg_names']:>5.1f} "
            f"turn={res.stats['turnover_ann']:>5.1f}x "
            f"({time.time() - t0:.1f}s)"
        )
    return results


def save_results(
    results: list[SimResult], out_dir: Path
) -> tuple[pd.DataFrame, pd.DataFrame]:
    summary = pd.DataFrame([r.stats for r in results])
    yearly = pd.concat(
        [r.yearly.assign(label=r.config.label) for r in results if not r.yearly.empty],
        ignore_index=True,
    )
    summary.to_csv(out_dir / "rolling_summary.csv", index=False)
    yearly.to_csv(out_dir / "rolling_yearly.csv", index=False)
    for r in results:
        name = slug(r.config.label)
        if not r.trades.empty:
            r.trades.to_csv(out_dir / f"trades_{name}.csv", index=False)
        r.nav.rename("nav").to_csv(out_dir / f"nav_{name}.csv")
    return summary, yearly


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-map", default="/tmp/model_map_31.json")
    parser.add_argument("--start", default="2025-02-05")
    parser.add_argument("--end", default="2026-08-26")
    parser.add_argument("--design-end", default="2025-12-31")
    parser.add_argument(
        "--mode",
        choices=["main", "ext", "all", "pilot", "supp"],
        default="main",
    )
    parser.add_argument("--top-ns", default="20,50")
    parser.add_argument("--holds", default="5,10")
    parser.add_argument("--out-dir", default="/tmp/rolling_sim")
    parser.add_argument("--memory", default="5GB")
    parser.add_argument("--threads", type=int, default=3)
    parser.add_argument("--capital", type=float, default=CAPITAL)
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    models_all = json.loads(Path(args.model_map).read_text(encoding="utf-8"))
    models_by_h = {
        h: [m for m in models_all if int(m["horizon"]) == h] for h in (5, 10)
    }
    for h, ms in models_by_h.items():
        log(f"T+{h} 模型数 {len(ms)}")

    # 多取 45 天，让 ADV 的 20 日均值在首个信号日已经热起来。
    lo = int((pd.Timestamp(args.start) - pd.Timedelta(days=45)).strftime("%Y%m%d"))
    hi = int((pd.Timestamp(args.end) + pd.Timedelta(days=40)).strftime("%Y%m%d"))
    con = E.connect(args.memory, args.threads)
    close, adv, blocked_long, blocked_short = load_panels(con, lo, hi)
    log(f"panel close={close.shape} adv={adv.shape}")
    register_adv(con, adv)
    for h in (5, 10):
        register_fwd(con, close, h)

    scores: dict[int, pd.DataFrame] = {}
    avgs: dict[int, pd.DataFrame] = {}
    diags: list[pd.DataFrame] = []
    for h in (5, 10):
        score, count, diag = build_composite(
            con, models_by_h[h], args.start, args.end, diag_horizon=h
        )
        score = score.reindex(index=close.index, columns=close.columns)
        avg = score_average(score)
        scores[h] = score
        avgs[h] = avg
        if not diag.empty:
            diags.append(diag)
        log(
            f"T+{h} composite coverage/day="
            f"{float(score.notna().sum(axis=1).loc[20250219:].mean()):.0f}, "
            f"first_avg={avg.dropna(how='all').index.min()}"
        )
    if diags:
        pd.concat(diags, ignore_index=True).to_csv(
            out_dir / "model_capability.csv", index=False
        )

    market = market_daily(close, adv)
    market20 = (1.0 + market).rolling(20, min_periods=15).apply(np.prod, raw=True) - 1.0
    dates = close.index.to_numpy(dtype="int64")
    avg_rows = (
        avgs[10].notna().sum(axis=1) if 10 in avgs else avgs[5].notna().sum(axis=1)
    )
    first_avg_i = int(np.argmax(avg_rows.to_numpy() >= 100))
    start_i = first_avg_i + 1
    end_i = int(np.searchsorted(dates, int(pd.Timestamp(args.end).strftime("%Y%m%d"))))
    end_i = min(end_i, len(dates) - 1)
    log(f"sim {dates[start_i]} .. {dates[end_i]} (start_i={start_i})")

    if args.mode == "pilot":
        mains = [
            Config(
                label="Top20 H10 time",
                top_n=20,
                hold=10,
                score_exit=False,
            ),
            Config(label="Top20 H10 score", top_n=20, hold=10),
            Config(
                label="Top20 H10 score+stop8",
                top_n=20,
                hold=10,
                price_stop=0.08,
            ),
            Config(
                label="Top20 H10 score mkt",
                top_n=20,
                hold=10,
                market_filter=True,
            ),
        ]
    elif args.mode == "supp":
        mains = supp_configs()
    else:
        top_ns = [int(x) for x in args.top_ns.split(",")]
        holds = [int(x) for x in args.holds.split(",")]
        mains = main_grid(top_ns, holds)
    mains = [replace(c, capital=args.capital) for c in mains]

    def run(cfgs: list[Config]) -> list[SimResult]:
        return run_configs(
            cfgs,
            close,
            adv,
            scores,
            avgs,
            blocked_long,
            blocked_short,
            start_i,
            end_i,
            market20,
        )

    results = run(mains)

    if args.mode == "all":
        # 设计年（2025）按 Calmar 选基准；2026 完全不参与选参。
        yearly0 = pd.concat(
            [r.yearly.assign(label=r.config.label) for r in results],
            ignore_index=True,
        )
        y25 = yearly0[yearly0["year"] == 2025].set_index("label")
        calmar = (y25["ann_ret"] / y25["maxdd"].abs()).replace(
            [np.inf, -np.inf], np.nan
        )
        base_label = str(calmar.dropna().idxmax())
        base = next(c for c in mains if c.label == base_label)
        log(
            f"2025 设计年最优（Calmar）: {base_label} "
            f"calmar={float(calmar.loc[base_label]):.2f} "
            f"ann={float(y25.loc[base_label, 'ann_ret']):.1%} "
            f"dd={float(y25.loc[base_label, 'maxdd']):.1%}"
        )
        extras = ext_grid(base) + capacity_grid(base)
        results.extend(run(extras))

    summary, yearly = save_results(results, out_dir)

    # 2025 / 2026 年度表
    wide = yearly.pivot_table(
        index="label", columns="year", values="ann_ret", aggfunc="first"
    )
    wide.columns = [f"ann_{c}" for c in wide.columns]
    print("\n年度年化（ann_ret）")
    print(wide.to_string())

    # 2025→2026 跨年秩相关：只看主网格，检验参数排序是否稳定。
    main_labels = {c.label for c in mains}
    mm = yearly[yearly["label"].isin(main_labels)]
    piv = mm.pivot(index="label", columns="year", values="ann_ret").dropna()
    if {2025, 2026}.issubset(piv.columns) and len(piv) > 2:
        rho = float(piv[[2025, 2026]].corr(method="spearman").iloc[0, 1])
        log(f"主网格 2025→2026 年化秩相关 rho={rho:.2f}")
        pd.Series({"rank_corr_2025_2026": rho, "n_configs": len(piv)}).to_csv(
            out_dir / "rolling_rank_stability.csv"
        )

    # 全期块 bootstrap + DSR（以全部试验数为惩罚）
    srs = []
    for r in results:
        rr = r.nav.pct_change().dropna()
        srs.append(
            float(rr.mean() / rr.std(ddof=1))
            if len(rr) > 2 and float(rr.std(ddof=1)) > 0
            else 0.0
        )
    rows = []
    for r in results:
        ret = r.nav.pct_change().dropna().to_numpy()
        ci_ann, ci_sr = block_bootstrap(ret)
        rows.append(
            {
                "label": r.config.label,
                "ann_ci_lo": ci_ann[0],
                "ann_ci_hi": ci_ann[1],
                "sr_ci_lo": ci_sr[0],
                "sr_ci_hi": ci_sr[1],
                "dsr": deflated_sharpe(ret, srs),
            }
        )
    robust = pd.DataFrame(rows)
    robust.to_csv(out_dir / "rolling_robustness.csv", index=False)
    print("\n稳健性")
    print(robust.to_string(index=False))


if __name__ == "__main__":
    main()
