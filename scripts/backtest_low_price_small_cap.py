"""小市值月度轮动策略回测（独立于 qlib/PG，直接读 QuantDB parquet）。

支持两种口径：①「低价 + 小市值」组合过滤；② 沪深主板「市值最小 N 只」纯小市值轮动。

口径
----
- 信号：月末最后一个交易日收盘（用当日可知的未复权价、总市值、5 日均成交额）
- 成交：T+1 开盘买入，持有到下一次调仓的 T+1 开盘卖出（不做日内）
- 收益：后复权开盘价之比，已含分红送转
- 成本：单边换手 × 0.35%（佣金万 2.5（最低 5 元）+ 印花税万 5（仅卖出）+
  过户费万 0.1 + 滑点 0.1%/边，合并近似单边 0.175%）
- 基准：同股票池等权；沪深 300 / 中证 1000 / 国证 2000 指数同口径

股票池（每月重算）
------------------
默认：非 ST、非退市风险、沪深主板、上市满 120 天、股价 ≥ 1.5 元、
5 日均成交额 ≥ 2000 万元、总市值 ≥ 10 亿元。
另外默认剔除**近 365 天内被证监会立案调查**的标的（`--no-exclude-investigation`
关闭），事件来自 `data/regulatory/events.parquet`（见 fetch_regulatory_events.py）。
所有门槛都能用命令行关掉（传 0），`--board all` 则不限制板块。

两种典型用法
------------
低价 + 小市值（默认）：
    python scripts/backtest_low_price_small_cap.py
    python scripts/backtest_low_price_small_cap.py --price-max 8 --mv-max 3e9 --topk 30

沪深主板「市值最小 N 只」纯小市值轮动（不加任何门槛）：
    python scripts/backtest_low_price_small_cap.py --board main --topk 10 \
        --price-max 0 --mv-max 0 --min-price 0 --min-amount 0 --min-mv 0 --min-listed-days 0

按月规避 1/4 月（按买入月，flat=整月空仓；half=减半）：
    python scripts/backtest_low_price_small_cap.py --skip-months 1,4
    python scripts/backtest_low_price_small_cap.py --skip-months 1,4 --skip-mode half
    python scripts/backtest_low_price_small_cap.py --skip-months 1:half,4:flat

出图 / 落 JSON：
    python scripts/backtest_low_price_small_cap.py --charts docs/images --json /tmp/lp.json

注意：QuantDB 数据集中没有退市股（见 research/stage0-数据与环境/day09-幸存者偏差），
低价小市值是受幸存者偏差影响最大的一类策略，本脚本的输出应视为收益上限。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

DATA_DIR = Path("/opt/quantmind/data/quantdb")
EVENTS_PATH = Path(__file__).resolve().parent.parent / "data/regulatory/events.parquet"
# 保守口径：佣金万2.5 + 印花税万5（仅卖）+ 过户费万0.1 + 滑点0.1%/边 ≈ 0.175%/边。
# 实际佣金万0.875免5：买入约0.10975%、卖出约0.15975%，平均约0.135%/边，
# 可用 --cost 0.00135 跑实际口径；默认仍用 0.175% 保守值。
COST_ONE_WAY = 0.00175  # 买卖各 0.175%，一次完整换手 0.35%
LABEL_EN = {
    "策略": "10 smallest main-board",
    "低价+小市值": "Low-price small-cap",
    "全市场等权": "Universe equal-weight",
    "沪深300": "CSI 300",
    "中证1000": "CSI 1000",
    "国证2000": "CNI 2000",
}
# 沪深主板：沪市 600/601/603/605，深市 000/001/002/003（002 中小板 2021 年并入主板）。
# 不含创业板 300/301、科创板 688/689、北交所 43/83/87/88/92、B 股 200/900。
BOARD_PREFIXES = {
    "main": ("600", "601", "603", "605", "000", "001", "002", "003"),
    "all": None,
    "ex_star": None,  # 全板块但剔除科创板
}
# 只排除、不枚举的板块（科创板 688/689 有 50 万门槛且小盘流动性极差）
BOARD_EXCLUDES = {"ex_star": ("688", "689")}
INDEX_BENCHMARKS = {
    "000300.SH": "沪深300",
    "000852.SH": "中证1000",
    "399303.SZ": "国证2000",
}


def connect(data_dir: Path) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    columns = {
        "kb": ("1_kline_data/daily_backward", "symbol, dt, open, volume"),
        "ku": ("1_kline_data/daily_unadjusted", "symbol, dt, open, close"),
        "feat": (
            "6_ml_datasets/features_daily",
            "symbol, dt, close, total_mv, amount_ma_5, is_st, is_quit_risk, list_date",
        ),
        "idx": ("1_kline_data/index_daily", "symbol, dt, open, close"),
    }
    for view, (rel, cols) in columns.items():
        con.execute(
            f"CREATE VIEW {view} AS SELECT {cols} FROM "
            f"read_parquet('{data_dir}/{rel}/**/*.parquet')"
        )
    return con


def build_plan(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """月末信号日 → 次日开盘买入 → 下次调仓次日开盘卖出。"""
    dates = [
        int(r[0])
        for r in con.execute("SELECT DISTINCT dt FROM kb ORDER BY dt").fetchall()
    ]
    ts = pd.Series(pd.to_datetime([str(d) for d in dates]))
    month_end = ts.groupby([ts.dt.year, ts.dt.month]).max()
    signal = sorted(int(pd.Timestamp(x).strftime("%Y%m%d")) for x in month_end.values)
    pos = {d: i for i, d in enumerate(dates)}
    rows = []
    for i, d in enumerate(signal):
        if pos[d] + 1 >= len(dates):
            continue
        entry = dates[pos[d] + 1]
        if i + 1 < len(signal) and pos[signal[i + 1]] + 1 < len(dates):
            exit_ = dates[pos[signal[i + 1]] + 1]
        else:
            exit_ = dates[-1]
        rows.append((d, entry, exit_))
    return pd.DataFrame(rows, columns=["signal_dt", "entry_dt", "exit_dt"])


def build_panel(con: duckdb.DuckDBPyConnection, plan: pd.DataFrame) -> pd.DataFrame:
    sig = ",".join(str(x) for x in plan.signal_dt)
    ent = ",".join(str(x) for x in plan.entry_dt.unique())
    exi = ",".join(str(x) for x in plan.exit_dt.unique())
    feat = con.execute(
        f"""SELECT symbol, dt, close AS adj_close, total_mv, amount_ma_5, is_st,
                   is_quit_risk, list_date
            FROM feat WHERE dt IN ({sig})"""
    ).df()
    # 不复权价：px = 信号日真实收盘，o_in_raw = 买入日真实开盘（成交价必须用真实价）
    px = con.execute(
        f"SELECT symbol, dt, close AS px, open AS px_open FROM ku WHERE dt IN ({sig})"
    ).df()
    op = con.execute(
        f"SELECT symbol, dt, open, volume FROM kb WHERE dt IN ({ent}) OR dt IN ({exi})"
    ).df()
    raw_in = con.execute(
        f"SELECT symbol, dt, open AS o_in_raw FROM ku WHERE dt IN ({ent})"
    ).df()
    panel = (
        feat.merge(px, on=["symbol", "dt"], how="left")
        .merge(plan, left_on="dt", right_on="signal_dt", how="left")
        .merge(
            op.rename(columns={"dt": "entry_dt", "open": "o_in", "volume": "v_in"}),
            on=["symbol", "entry_dt"],
            how="left",
        )
        .merge(
            op.rename(columns={"dt": "exit_dt", "open": "o_out"}),
            on=["symbol", "exit_dt"],
            how="left",
        )
        .merge(
            raw_in.rename(columns={"dt": "entry_dt"}),
            on=["symbol", "entry_dt"],
            how="left",
        )
    )
    panel["fwd_ret"] = panel["o_out"] / panel["o_in"] - 1.0
    # 可成交性判定：用**真实价**算隔夜缺口（复权价之比会把除权日误判成涨跌停）。
    # 创业板 300/301 是 20% 涨跌幅、北交所 30%，其余主板 10%（ST 已在股票池剔除）。
    pre = panel.symbol.str[:3]
    limit = np.where(
        pre.isin(["300", "301"]),
        0.195,
        np.where(
            panel.symbol.str[:2].isin(["43", "83", "87", "88", "92"]), 0.295, 0.095
        ),
    )
    gap = panel["o_in_raw"] / panel["px"] - 1.0
    panel["gap"] = gap
    # tradable 只描述**入场侧**能否成交（T+1 开盘时可观测：停牌/一字涨停）。
    # 不能把「未来退出价是否存在」放进来——那是持有期事件，用它筛选股就是未来函数。
    has_entry = panel["o_in"].notna() & (panel["o_in"] > 0)
    panel["tradable"] = (panel["v_in"].fillna(0) > 0) & has_entry & (gap < limit)
    panel["list_dt"] = pd.to_datetime(
        panel.list_date.astype(str), format="%Y%m%d", errors="coerce"
    )
    panel["dt_ts"] = pd.to_datetime(panel.dt.astype(str), format="%Y%m%d")
    panel["listed_days"] = (panel.dt_ts - panel.list_dt).dt.days
    return panel


def universe(
    panel: pd.DataFrame,
    *,
    board: str = "main",
    min_amount: float = 2000.0,
    min_price: float = 1.5,
    min_listed_days: int = 120,
    min_mv: float = 1e9,
) -> pd.DataFrame:
    """按给定门槛过滤股票池。所有门槛都设 0 时即「只用数据可成交性」的裸口径。"""
    pre = panel.symbol.str[:3]
    # 注意：parquet 分区之间列类型会漂移（is_quit_risk 在 2026 分区是字符串 '0'），
    # 直接 == 0 会把整段 2026 数据静默丢光，必须先转数值。
    is_st = pd.to_numeric(panel.is_st, errors="coerce").fillna(0)
    quit_risk = pd.to_numeric(panel.is_quit_risk, errors="coerce").fillna(0)
    # 注意：这里**只能**用信号日可知的字段。fwd_ret / tradable 依赖 T+1 及之后
    # 的信息，属于执行层，必须在 run_strategy() 里处理，不能参与选股。
    mask = (is_st == 0) & (quit_risk == 0)
    prefixes = BOARD_PREFIXES[board]
    if prefixes is not None:
        mask &= pre.isin(prefixes)
    excludes = BOARD_EXCLUDES.get(board)
    if excludes is not None:
        mask &= ~pre.isin(excludes)
    if min_price > 0:
        mask &= panel.px >= min_price
    if min_amount > 0:
        mask &= panel.amount_ma_5 >= min_amount
    if min_listed_days > 0:
        mask &= panel.listed_days >= min_listed_days
    if min_mv > 0:
        mask &= panel.total_mv >= min_mv
    return panel[mask].copy()


def run_strategy(
    base: pd.DataFrame,
    pool_fn,
    topk: int = 40,
    cost: float = COST_ONE_WAY,
    start: int | None = None,
    exposure_fn=None,
) -> pd.DataFrame:
    """逐月调仓，返回净值/收益/换手序列。

    时序约定
    --------
    - ``pool_fn`` 只用**信号日可知**的字段筛选（universe 已保证不含未来信息）；
    - T+1 能否成交（停牌 / 开盘一字涨停 / 取不到退出价）在执行层用 ``tradable``
      过滤，买不进的标的直接跳过、对应的钱留在现金里，**不参与选股**；
    - 旧仓按**上一期**已实现收益漂移到本期，权重向量包含现金（``cash_w``），
      因此半仓/空仓的买卖都能被正确计费。

    ``exposure_fn(dt)`` 返回目标仓位（1.0=满仓，0.5=半仓，0=空仓）。
    """
    w_old = pd.Series(dtype=float)  # 上期末各标的权重（占 NAV 比例）
    cash_w = 1.0  # 上期末现金权重
    ret_prev: dict[str, float] = {}  # 上一期各标的的已实现收益
    nav = 1.0
    rows = []
    for d in sorted(base.dt.unique()):
        if start and d < start:
            continue
        snap = base[base.dt == d]
        entry = int(snap.entry_dt.iloc[0]) if "entry_dt" in snap.columns else int(d)
        pos = 1.0 if exposure_fn is None else float(exposure_fn(d))

        cand = pool_fn(snap)
        if "tradable" in cand.columns:
            # 执行层：T+1 开盘买不进的跳过（此时可观测，不算未来函数），
            # 由后一名补位。**不**用未来退出价是否存在来筛。
            cand = cand[cand.tradable.fillna(False)]
        sel = cand.nsmallest(topk, "total_mv")

        # 1) 旧仓用「上一期」收益漂移，现金按 0 收益，一起归一到当前总资产
        if len(w_old):
            grown = w_old * (
                1 + pd.Series(ret_prev, dtype=float).reindex(w_old.index).fillna(0.0)
            )
            total = float(grown.sum()) + cash_w
            w_grown = grown / total if total > 0 else pd.Series(dtype=float)
        else:
            w_grown = pd.Series(dtype=float)

        # 2) 目标权重；sel 为空或目标仓位为 0 时目标为空（全部清仓）
        if sel.empty or pos <= 0:
            target = pd.Series(dtype=float)
        else:
            target = pd.Series(pos / len(sel), index=sel.symbol.values)

        # 3) 交易：买卖分别按成交额计费（现金变动 = -(买卖净额)，无需单独算）
        keys = w_grown.index.union(target.index)
        oldv = w_grown.reindex(keys).fillna(0.0)
        newv = target.reindex(keys).fillna(0.0)
        buys = float((newv - oldv).clip(lower=0).sum())
        sells = float((oldv - newv).clip(lower=0).sum())
        fee = (buys + sells) * cost
        turn = (buys + sells) / 2

        # 4) 本期收益只用已成交标的
        if len(target):
            # 退出价缺失（停牌/数据缺口）按「冻结」处理：本期收益记 0。
            # 这是保守假设，且不把「有没有退出价」回灌到选股。
            rets = sel.set_index("symbol").fwd_ret.astype(float).fillna(0.0)
            gross = float((target * rets.reindex(target.index)).sum())
        else:
            gross = 0.0
        net = gross - fee
        nav *= 1 + net

        rows.append(
            {
                "dt": d,
                "entry_dt": entry,
                "nav": nav,
                "ret": gross,
                "cost": fee,
                "turn": turn,
                "n": len(target),
                "med_px": sel.px.median() if len(sel) else np.nan,
                "med_mv": sel.total_mv.median() if len(sel) else np.nan,
                "med_amt": sel.amount_ma_5.median() if len(sel) else np.nan,
            }
        )
        w_old = target
        cash_w = max(0.0, 1.0 - float(target.sum()))
        ret_prev = (
            dict(zip(sel.symbol, sel.fwd_ret.astype(float), strict=False))
            if len(sel)
            else {}
        )
    return pd.DataFrame(rows)


def stats(nav_df: pd.DataFrame, label: str) -> dict:
    nav = pd.Series(nav_df.nav.values, index=nav_df.dt.values)
    net = nav_df.ret - nav_df.cost
    years = (
        pd.to_datetime(str(nav_df.dt.iloc[-1]), format="%Y%m%d")
        - pd.to_datetime(str(nav_df.dt.iloc[0]), format="%Y%m%d")
    ).days / 365.25
    vol = net.std() * np.sqrt(12)
    if "entry_dt" in nav_df.columns:
        # 按「实际持仓月份」归年：净值序列按信号日打点，直接按信号日分组会把
        # 12 月那一期（1 月才结算）算进上一年，逐年数字整体错位一个月。
        yearly = (1 + net).groupby(nav_df.entry_dt // 10000).prod() - 1
    else:
        year_nav = nav.groupby(nav.index // 10000).last()
        yearly = year_nav / year_nav.shift(1) - 1
        yearly.iloc[0] = year_nav.iloc[0] - 1
    return {
        "label": label,
        "cagr": float(nav.iloc[-1] ** (1 / years) - 1),
        "vol": float(vol),
        "sharpe": float((net.mean() * 12 - 0.02) / vol) if vol else float("nan"),
        "mdd": float((nav / nav.cummax() - 1).min()),
        "avg_turn": float(nav_df.turn.mean()),
        "avg_n": float(nav_df.n.mean()),
        "yearly": {int(k): float(v) for k, v in yearly.items()},
        "nav": nav,
    }


def timing_exposure(
    con: duckdb.DuckDBPyConnection, symbol: str, ma_days: int, weak: float
) -> dict[int, float]:
    """指数收盘在 MA 之上满仓、之下按 ``weak`` 计仓（写死的规则，不含参数寻优）。

    信号日当天收盘即可判定，因此不引入未来数据。基准指数自身在回测窗口内
    不足 ``ma_days`` 根 K 线时，取到多少算多少。
    """
    px = con.execute(
        f"SELECT dt, close FROM idx WHERE symbol = '{symbol}' ORDER BY dt"
    ).df()
    if px.empty:
        raise SystemExit(f"择时指数 {symbol} 没有数据")
    px["ma"] = px.close.rolling(ma_days, min_periods=ma_days).mean()
    px["above"] = px.close > px.ma
    out = {
        int(r.dt): (1.0 if r.above else weak) for r in px.itertuples() if pd.notna(r.ma)
    }
    return out


def breadth_table(
    con: duckdb.DuckDBPyConnection, ma_days: int = 20
) -> dict[int, float]:
    """全 A 股「收盘价 > MA20」占比（%），按交易日。

    口径与 docs/广度择时_过拟合检验.md 一致：不复权收盘、按 symbol 滚动
    ``ma_days`` 日均线、只统计当日已满 ``ma_days`` 根 K 线的股票。
    信号日当天收盘即可算出，因此不引入未来数据。
    """
    if ma_days < 2:
        raise SystemExit(f"--breadth-ma 至少为 2：{ma_days}")
    df = con.execute(
        f"""
        WITH d AS (
            SELECT symbol, dt, close,
                   avg(close) OVER (PARTITION BY symbol ORDER BY dt
                                    ROWS BETWEEN {ma_days - 1} PRECEDING
                                    AND CURRENT ROW) AS ma,
                   row_number() OVER (PARTITION BY symbol ORDER BY dt) AS rn
            FROM ku
        )
        SELECT dt, avg(CASE WHEN close > ma THEN 1.0 ELSE 0.0 END) * 100 AS breadth
        FROM d WHERE rn >= {ma_days}
        GROUP BY dt ORDER BY dt
        """
    ).df()
    return {int(r.dt): float(r.breadth) for r in df.itertuples()}


def apply_month_skip(prev, entry_of: dict[int, int], skip_months: dict[int, str]):
    """按「买入月」空仓 / 减半，叠在已有的 exposure 规则之上。"""

    def f(d: int) -> float:
        e = float(prev(int(d)))
        m = int(str(entry_of.get(int(d), int(d)))[4:6])
        mode = skip_months.get(m)
        if mode == "flat":
            return 0.0
        if mode == "half":
            return min(e, 0.5)
        return e

    return f


def apply_breadth(prev, breadth: dict[int, float], threshold: float, cap: float):
    """广度低于阈值时把仓位压到 ``cap``（只压不抬，且排在月份规则之后）。"""

    def f(d: int) -> float:
        e = float(prev(int(d)))
        b = breadth.get(int(d))
        if b is not None and b < threshold:
            e = min(e, cap)
        return e

    return f


def investigation_blacklist(
    path: Path, signal_dates, window_days: int
) -> dict[int, set[str]]:
    """每个信号日 → 近 ``window_days`` 天内被立案调查的标的集合。

    只用 ``announce_date <= 信号日`` 的公告，公告日即信息可见日，不构成未来函数。
    事件表缺列/缺文件时返回空表，脚本退化为「不剔除」。
    """
    if window_days <= 0:
        return {}
    if not path.exists():
        raise FileNotFoundError(f"监管事件表不存在：{path}")
    ev = pd.read_parquet(path)
    if "category" not in ev.columns:
        raise ValueError(f"事件表缺 category 列：{path}")
    ev = ev[ev.category == "investigation"]
    if ev.empty:
        raise ValueError(f"事件表里没有 investigation 行：{path}")
    ev = ev.assign(
        announce_date=pd.to_numeric(ev.announce_date, errors="coerce")
    ).dropna(subset=["announce_date", "symbol"])
    bad: dict[int, set[str]] = {}
    for d in signal_dates:
        lo = int(
            (pd.Timestamp(str(d)) - pd.Timedelta(days=window_days)).strftime("%Y%m%d")
        )
        sub = ev[(ev.announce_date <= d) & (ev.announce_date >= lo)]
        bad[int(d)] = set(sub.symbol)
    return bad


def drawdown_guard(
    con: duckdb.DuckDBPyConnection, symbol: str, threshold: float
) -> dict[int, bool]:
    """指数距 250 日高点的回撤是否超过 ``threshold``（True = 本期空仓）。"""
    if threshold <= 0:
        return {}
    px = con.execute(
        f"SELECT dt, close FROM idx WHERE symbol = '{symbol}' ORDER BY dt"
    ).df()
    px["peak"] = px.close.rolling(250, min_periods=60).max()
    px["dd"] = px.close / px.peak - 1.0
    return {
        int(r.dt): bool(r.dd < -threshold) for r in px.itertuples() if pd.notna(r.dd)
    }


def index_nav(
    con: duckdb.DuckDBPyConnection, plan: pd.DataFrame
) -> dict[str, pd.Series]:
    ent = ",".join(str(x) for x in plan.entry_dt.unique())
    exi = ",".join(str(x) for x in plan.exit_dt.unique())
    op = con.execute(
        f"SELECT symbol, dt, open FROM idx WHERE dt IN ({ent}) OR dt IN ({exi})"
    ).df()
    out = {}
    for sym, name in INDEX_BENCHMARKS.items():
        one = op[op.symbol == sym]
        merged = plan.merge(
            one.rename(columns={"dt": "entry_dt", "open": "o_in"})[
                ["entry_dt", "o_in"]
            ],
            on="entry_dt",
        ).merge(
            one.rename(columns={"dt": "exit_dt", "open": "o_out"})[
                ["exit_dt", "o_out"]
            ],
            on="exit_dt",
        )
        ret = (merged.o_out / merged.o_in - 1).values
        out[name] = pd.Series(np.cumprod(1 + ret), index=plan.signal_dt.values)
    return out


def price_size_heatmap(base: pd.DataFrame) -> pd.DataFrame:
    """价格 × 市值 的月均超额收益（相对同池等权），单位 %。"""
    bench = base.groupby("dt").fwd_ret.transform("mean")
    df = base.assign(ex=base.fwd_ret - bench)
    px_bins = [1.5, 3, 5, 8, 12, 20, 50, np.inf]
    px_lab = ["1.5-3", "3-5", "5-8", "8-12", "12-20", "20-50", "50+"]
    mv_bins = [0, 3e9, 5e9, 1e10, 2e10, 5e10, 1e11, np.inf]
    mv_lab = ["<30", "30-50", "50-100", "100-200", "200-500", "500-1000", ">1000"]
    df["pxb"] = pd.cut(df.px, px_bins, labels=px_lab, right=False)
    df["mvb"] = pd.cut(df.total_mv, mv_bins, labels=mv_lab, right=False)
    return (
        df.pivot_table(
            index="pxb", columns="mvb", values="ex", aggfunc="mean", observed=True
        )
        * 100
    )


def ic_series(base: pd.DataFrame) -> dict:
    """价格 / 市值与下期收益的秩相关（Spearman），按截面计算。"""
    out = {}
    for col, sign in (("px", -1), ("total_mv", -1)):
        per = []
        for _, g in base.groupby("dt"):
            if len(g) < 50:
                continue
            r = g[col].rank().corr(g.fwd_ret.rank())
            per.append(r * sign)  # 方向统一成「越小越好」为正
        s = pd.Series(per).dropna()
        out[col] = {
            "mean_ic": float(s.mean()),
            "icir": float(s.mean() / s.std()),
            "n": len(s),
        }
    return out


def make_charts(
    navs: dict[str, pd.Series], yearly: dict, heat: pd.DataFrame, out_dir: Path
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    out_dir.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(11, 5.5))
    for label, nav in navs.items():
        ax.plot(
            pd.to_datetime(nav.index.astype(str)),
            nav.values,
            label=LABEL_EN.get(label, label),
            linewidth=1.6,
        )
    ax.set_yscale("log")
    ax.set_title("Monthly rotation (10 smallest cap) vs benchmarks (log NAV)")
    ax.set_ylabel("NAV (log)")
    ax.grid(alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(out_dir / "lowprice_nav.png", dpi=130)
    plt.close(fig)

    years = sorted(yearly["策略"])
    fig, ax = plt.subplots(figsize=(10, 4.5))
    width = 0.4
    ax.bar(
        [y - width / 2 for y in years],
        [yearly["策略"][y] * 100 for y in years],
        width,
        label="Strategy (10 smallest cap)",
    )
    ax.bar(
        [y + width / 2 for y in years],
        [yearly["全市场等权"][y] * 100 for y in years],
        width,
        label="Universe equal-weight",
    )
    ax.axhline(0, color="black", linewidth=0.8)
    ax.set_title("Yearly return (%)")
    ax.grid(axis="y", alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(out_dir / "lowprice_yearly.png", dpi=130)
    plt.close(fig)

    import matplotlib.cm as cm

    fig, ax = plt.subplots(figsize=(9, 4.8))
    data = heat.values.astype(float)
    im = ax.imshow(data, cmap="RdYlGn", vmin=-2.5, vmax=2.5)
    ax.set_xticks(
        range(len(heat.columns)), heat.columns, rotation=30, ha="right", fontsize=9
    )
    ax.set_xlabel("Market cap (100M CNY)")
    ax.set_ylabel("Price (CNY)")
    ax.set_yticks(range(len(heat.index)), heat.index, fontsize=9)
    for i in range(data.shape[0]):
        for j in range(data.shape[1]):
            if np.isfinite(data[i, j]):
                ax.text(j, i, f"{data[i, j]:.1f}", ha="center", va="center", fontsize=8)
    ax.set_title("Monthly excess return (%) by price x market cap")
    fig.colorbar(im, ax=ax, shrink=0.8)
    fig.tight_layout()
    fig.savefig(out_dir / "lowprice_heatmap.png", dpi=130)
    plt.close(fig)


def main() -> int:
    ap = argparse.ArgumentParser(description="低价股 / 小市值 月度轮动回测")
    ap.add_argument("--data-dir", type=Path, default=DATA_DIR)
    ap.add_argument("--start", default="2016-01-01")
    ap.add_argument("--end", default="2026-10-23")
    ap.add_argument(
        "--board",
        default="main",
        choices=sorted(BOARD_PREFIXES),
        help="main=沪深主板（默认）；all=不限制板块",
    )
    ap.add_argument(
        "--price-max", type=float, default=10.0, help="股价上限（元），0=不限"
    )
    ap.add_argument(
        "--mv-max", type=float, default=5e9, help="总市值上限（元），默认 50 亿，0=不限"
    )
    ap.add_argument(
        "--min-price", type=float, default=1.5, help="股价下限（元），0=不限"
    )
    ap.add_argument(
        "--min-amount",
        type=float,
        default=2000.0,
        help="5 日均成交额下限（万元），0=不限",
    )
    ap.add_argument(
        "--min-mv", type=float, default=1e9, help="总市值下限（元），0=不限"
    )
    ap.add_argument(
        "--min-listed-days", type=int, default=120, help="上市天数下限，0=不限"
    )
    ap.add_argument("--topk", type=int, default=40)
    ap.add_argument("--cost", type=float, default=COST_ONE_WAY, help="单边费率（小数）")
    ap.add_argument(
        "--timing",
        default="none",
        choices=["none", "half", "flat"],
        help="择时：none=满仓；half=指数在 MA 下方降半仓；flat=指数在 MA 下方空仓",
    )
    ap.add_argument(
        "--timing-symbol", default="000852.SH", help="择时指数，默认中证 1000"
    )
    ap.add_argument("--timing-ma", type=int, default=200, help="择时均线天数")
    ap.add_argument(
        "--timing-dd",
        type=float,
        default=0.0,
        help="指数距 250 日高点回撤超过该比例时清仓（0.3=跌超30%%空仓，0=不启用）",
    )
    ap.add_argument(
        "--skip-months",
        default="",
        help="按买入月空仓/减半的月份，逗号分隔；可写 1:half,4:flat（不写模式用 --skip-mode）",
    )
    ap.add_argument(
        "--skip-mode",
        default="flat",
        choices=["flat", "half"],
        help="skip-months 的处理：flat=整月空仓；half=仓位减半",
    )
    ap.add_argument(
        "--exclude-investigation",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="剔除近 --investigation-window 天内被立案调查的标的（默认开）",
    )
    ap.add_argument(
        "--investigation-window", type=int, default=365, help="立案剔除回看天数"
    )
    ap.add_argument("--events-path", type=Path, default=EVENTS_PATH, help="监管事件表")
    ap.add_argument(
        "--allow-missing-events",
        action="store_true",
        help="事件表缺失时仍继续（默认 fail-closed，直接报错）",
    )
    ap.add_argument(
        "--breadth",
        type=float,
        default=0.0,
        help="广度阈值（%%）：信号日全 A 股收盘>MA20 占比低于该值时压低仓位，0=关闭",
    )
    ap.add_argument(
        "--breadth-e",
        type=float,
        default=0.3,
        help="广度低于阈值时的仓位上限（默认 0.3，即三成）",
    )
    ap.add_argument(
        "--breadth-ma", type=int, default=20, help="广度均线天数（默认 20）"
    )
    ap.add_argument("--charts", type=Path, default=None, help="图表输出目录")
    ap.add_argument("--json", type=Path, default=None, help="指标 JSON 输出路径")
    args = ap.parse_args()

    con = connect(args.data_dir)
    plan = build_plan(con)
    panel = build_panel(con, plan)
    base = universe(
        panel,
        board=args.board,
        min_amount=args.min_amount,
        min_price=args.min_price,
        min_listed_days=args.min_listed_days,
        min_mv=args.min_mv,
    )
    start = int(str(args.start).replace("-", ""))
    end = int(str(args.end).replace("-", ""))
    base = base[(base.dt >= start) & (base.dt <= end)]

    blacklist: dict[int, set[str]] = {}
    if args.exclude_investigation:
        try:
            blacklist = investigation_blacklist(
                args.events_path, sorted(base.dt.unique()), args.investigation_window
            )
        except (FileNotFoundError, ValueError, OSError) as exc:
            if not args.allow_missing_events:
                raise SystemExit(
                    f"立案剔除已开启，但事件表不可用：{exc}\n"
                    "  先跑 `python scripts/fetch_regulatory_events.py`，"
                    "或加 `--no-exclude-investigation` / `--allow-missing-events`。"
                ) from exc
            print(f"⚠️ 立案剔除未生效（{exc}），按 --allow-missing-events 继续")

    def pick(snap: pd.DataFrame) -> pd.DataFrame:
        out = snap
        if args.price_max > 0:
            out = out[out.px <= args.price_max]
        if args.mv_max > 0:
            out = out[out.total_mv <= args.mv_max]
        if blacklist:
            bad = blacklist.get(int(snap.dt.iloc[0]), set())
            if bad:
                out = out[~out.symbol.isin(bad)]
        return out

    weak = {"none": 1.0, "half": 0.5, "flat": 0.0}[args.timing]
    exposure = None
    if args.timing != "none" or args.timing_dd > 0:
        table = (
            timing_exposure(con, args.timing_symbol, args.timing_ma, weak)
            if args.timing != "none"
            else {}
        )
        crash = drawdown_guard(con, args.timing_symbol, args.timing_dd)

        def exposure(d):  # noqa: F811
            if crash.get(int(d), False):
                return 0.0
            return table.get(int(d), 1.0)

    skip_months: dict[int, str] = {}
    for item in args.skip_months.split(","):
        item = item.strip()
        if not item:
            continue
        month, _, mode = item.partition(":")
        mode = mode or args.skip_mode
        if mode not in {"flat", "half"}:
            raise SystemExit(f"--skip-months 的模式只能是 flat/half：{item}")
        skip_months[int(month)] = mode
    if skip_months:
        # 按「买入月」判定：12 月信号、1 月持有的那期算 1 月。
        entry_of = dict(
            zip(base.dt.astype(int), base.entry_dt.astype(int), strict=False)
        )
        exposure = apply_month_skip(
            exposure if exposure is not None else (lambda d: 1.0),
            entry_of,
            skip_months,
        )

    breadth: dict[int, float] = {}
    if args.breadth > 0:
        breadth = breadth_table(con, args.breadth_ma)
        exposure = apply_breadth(
            exposure if exposure is not None else (lambda d: 1.0),
            breadth,
            args.breadth,
            args.breadth_e,
        )

    label = "策略"
    runs = {
        "全市场等权": run_strategy(
            base, lambda s: s, topk=10_000, cost=args.cost, start=start
        ),
        label: run_strategy(
            base,
            pick,
            topk=args.topk,
            cost=args.cost,
            start=start,
            exposure_fn=exposure,
        ),
    }
    res = {k: stats(v, k) for k, v in runs.items()}
    plan_w = plan[plan.signal_dt >= start].reset_index(drop=True)
    bench = index_nav(con, plan_w)
    idx_stats = {}
    for name, nav in bench.items():
        df = pd.DataFrame(
            {
                "dt": plan_w.signal_dt.values,
                # 带上 entry_dt：stats() 按「实际持仓年」分年，指数也要用同一口径，
                # 否则指数按信号年、策略按持仓年，跨 1 月大跌的年份会错位（2024 差 20pp+）。
                "entry_dt": plan_w.entry_dt.values,
                "nav": nav.values,
                "ret": nav.pct_change().fillna(0),
                "cost": 0.0,
                "turn": 0.0,
                "n": 0,
            }
        )
        idx_stats[name] = stats(df, name)

    print(
        f"股票池：月均 {base.groupby('dt').size().mean():.0f} 只（板块 {args.board}）"
    )
    print(
        f"参数：股价 ≤ {args.price_max:g}（下限 {args.min_price:g}）元，"
        f"市值 ≤ {args.mv_max / 1e8:g} 亿（下限 {args.min_mv / 1e8:g} 亿），"
        f"成交额 ≥ {args.min_amount:g} 万，上市 ≥ {args.min_listed_days} 天，"
        f"topk={args.topk}，单边费率 {args.cost * 100:.3f}%"
    )
    if skip_months:
        spec = ",".join(f"{m}:{skip_months[m]}" for m in sorted(skip_months))
        print(f"月份跳过：{spec}（按买入月）")
    if args.breadth > 0:
        n_hit = sum(
            1
            for d in plan.signal_dt
            if breadth.get(int(d), 100.0) < args.breadth
        )
        print(
            f"广度择时：全 A 股 <{args.breadth:g}% → 仓位 ≤ {args.breadth_e:g}"
            f"（MA{args.breadth_ma}，触发 {n_hit}/{len(plan)} 期）"
        )
    if args.exclude_investigation:
        nbad = sum(len(v) for v in blacklist.values()) / max(len(blacklist), 1)
        print(
            f"立案剔除：开（回看 {args.investigation_window} 天，"
            f"每期平均拉黑 {nbad:.0f} 只，占股票池 {nbad / max(len(base) / base.dt.nunique(), 1) * 100:.1f}%）"
        )
    else:
        print("立案剔除：关")
    for name, r in {**res, **idx_stats}.items():
        print(
            f"{name:<8} 年化 {r['cagr'] * 100:6.2f}%  夏普 {r['sharpe']:5.2f}  "
            f"最大回撤 {r['mdd'] * 100:7.2f}%  波动 {r['vol'] * 100:5.1f}%  "
            f"月均换手 {r['avg_turn'] * 100:5.1f}%  均持仓 {r['avg_n']:.0f}"
        )
    print("\n逐年收益：")
    for name in res:
        print(
            f"  {name:<6}",
            "  ".join(f"{y}:{v * 100:+.0f}%" for y, v in res[name]["yearly"].items()),
        )
    worst = pd.DataFrame({"ret": res[label]["nav"].pct_change()}).nsmallest(6, "ret")
    best = pd.DataFrame({"ret": res[label]["nav"].pct_change()}).nlargest(4, "ret")
    print(
        "\n最差 6 个月：",
        "  ".join(f"{d}:{r.ret * 100:+.1f}%" for d, r in worst.iterrows()),
    )
    print(
        "最好 4 个月：",
        "  ".join(f"{d}:{r.ret * 100:+.1f}%" for d, r in best.iterrows()),
    )
    nav = res[label]["nav"]
    print(
        f"末值 {nav.iloc[-1]:.2f}  月胜率 {(nav.pct_change() > 0).mean() * 100:.1f}%  "
        f"最长连亏 {(nav.pct_change() < 0).astype(int).groupby((nav.pct_change() > 0).cumsum()).sum().max()} 个月"
    )

    if args.json:
        payload = {
            # vars(args) 里 data_dir / events_path 等都是 Path，直接 json.dumps 会崩
            "params": {
                k: (str(v) if isinstance(v, Path) else v) for k, v in vars(args).items()
            },
            "universe_size": float(base.groupby("dt").size().mean()),
            "strategy": res[label]
            | {"nav": {str(k): float(v) for k, v in nav.items()}},
            "baseline": res["全市场等权"]
            | {"nav": {str(k): float(v) for k, v in res["全市场等权"]["nav"].items()}},
            "index": {
                k: {kk: vv for kk, vv in v.items() if kk != "nav"}
                for k, v in idx_stats.items()
            },
        }
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n指标已写入 {args.json}")

    if args.charts:
        navs = {label: nav}
        for name, ser in bench.items():
            navs[name] = pd.Series(ser.values, index=plan_w.signal_dt.values)
        navs["全市场等权"] = res["全市场等权"]["nav"]
        make_charts(
            navs,
            {k: v["yearly"] for k, v in res.items()},
            price_size_heatmap(base),
            args.charts,
        )
        print(f"图表已写入 {args.charts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
