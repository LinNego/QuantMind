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
所有门槛都能用命令行关掉（传 0），`--board all` 则不限制板块。

两种典型用法
------------
低价 + 小市值（默认）：
    python scripts/backtest_low_price_small_cap.py
    python scripts/backtest_low_price_small_cap.py --price-max 8 --mv-max 3e9 --topk 30

沪深主板「市值最小 N 只」纯小市值轮动（不加任何门槛）：
    python scripts/backtest_low_price_small_cap.py --board main --topk 10 \
        --price-max 0 --mv-max 0 --min-price 0 --min-amount 0 --min-mv 0 --min-listed-days 0

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
        "ku": ("1_kline_data/daily_unadjusted", "symbol, dt, close"),
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
    px = con.execute(f"SELECT symbol, dt, close AS px FROM ku WHERE dt IN ({sig})").df()
    op = con.execute(
        f"SELECT symbol, dt, open, volume FROM kb WHERE dt IN ({ent}) OR dt IN ({exi})"
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
    )
    panel["fwd_ret"] = panel["o_out"] / panel["o_in"] - 1.0
    # 可成交性：T+1 停牌（volume=0）买不进；T+1 开盘一字涨停也买不进。
    # 创业板 300/301 是 20% 涨跌幅，其余主板 10%（ST 已在股票池剔除）。
    limit = np.where(panel.symbol.str.startswith(("300", "301")), 0.195, 0.095)
    gap = panel["o_in"] / panel["adj_close"] - 1.0
    untradable = (panel["v_in"].fillna(0) == 0) | (gap >= limit)
    panel.loc[untradable, "fwd_ret"] = np.nan
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
    mask = (is_st == 0) & (quit_risk == 0) & panel.fwd_ret.notna()
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

    ``pool_fn`` 接收单月截面返回候选池；``exposure_fn(dt)`` 返回目标仓位
    （1.0=满仓，0.5=半仓），未配置时恒为满仓。未投入的现金按 0 收益处理。
    """
    w_old = pd.Series(dtype=float)
    nav = 1.0
    rows = []
    for d in sorted(base.dt.unique()):
        if start and d < start:
            continue
        snap = base[base.dt == d]
        sel = pool_fn(snap)
        entry = int(snap.entry_dt.iloc[0]) if "entry_dt" in snap.columns else int(d)
        if sel.empty:
            rows.append(
                {
                    "dt": d,
                    "entry_dt": entry,
                    "nav": nav,
                    "ret": 0.0,
                    "cost": 0.0,
                    "turn": 0.0,
                    "n": 0,
                }
            )
            w_old = pd.Series(dtype=float)
            continue
        sel = sel.nsmallest(topk, "total_mv")
        pos = 1.0 if exposure_fn is None else float(exposure_fn(d))
        if pos <= 0:
            rows.append(
                {
                    "dt": d,
                    "entry_dt": entry,
                    "nav": nav,
                    "ret": 0.0,
                    "cost": 0.0,
                    "turn": 0.0,
                    "n": 0,
                }
            )
            w_old = pd.Series(dtype=float)
            continue
        w = pd.Series(pos / len(sel), index=sel.symbol.values)
        if len(w_old):
            grown = w_old * (
                1 + snap.set_index("symbol").fwd_ret.reindex(w_old.index).fillna(0)
            )
            grown = grown / grown.sum()
            keys = w.index.union(grown.index)
            turn = (
                0.5
                * (w.reindex(keys).fillna(0) - grown.reindex(keys).fillna(0))
                .abs()
                .sum()
            )
        else:
            turn = 1.0
        gross = (w * sel.set_index("symbol").fwd_ret.reindex(w.index)).sum()
        fee = turn * 2 * cost
        nav *= 1 + gross - fee
        rows.append(
            {
                "dt": d,
                "entry_dt": entry,
                "nav": nav,
                "ret": gross,
                "cost": fee,
                "turn": turn,
                "n": len(w),
                "med_px": sel.px.median(),
                "med_mv": sel.total_mv.median(),
                "med_amt": sel.amount_ma_5.median(),
            }
        )
        w_old = w
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

    def pick(snap: pd.DataFrame) -> pd.DataFrame:
        out = snap
        if args.price_max > 0:
            out = out[out.px <= args.price_max]
        if args.mv_max > 0:
            out = out[out.total_mv <= args.mv_max]
        return out

    weak = {"none": 1.0, "half": 0.5, "flat": 0.0}[args.timing]
    exposure = None
    if args.timing != "none":
        table = timing_exposure(con, args.timing_symbol, args.timing_ma, weak)
        exposure = lambda d: table.get(int(d), 1.0)  # noqa: E731

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
            "params": vars(args)
            | {
                "data_dir": str(args.data_dir),
                "charts": str(args.charts),
                "json": str(args.json),
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
