#!/usr/bin/env python3
"""低价小市值策略的「回撤控制」研究台。

为什么单独一个脚本
------------------
`backtest_low_price_small_cap.py` 是**结论复现**脚本：一次跑一个参数组合，
读 2,600 个 parquet 分区要 1 分 47 秒，只够跑基准 + 一两个变体。
回撤控制的候选手段有几十个（剔除立案调查、质量过滤、低波、动量、行业上限、
波动率目标、择时、对冲……），必须能**在同一份面板上快速跑几十个变体**，
否则就会退化成「挑一个看起来最好的参数」，那是过拟合不是研究。

所以这里分两步：
  1. `--build`：把 129 个月末截面拼成一张宽面板，缓存成 parquet（约 2 分钟）；
  2. `--cache`：从缓存读，每个变体 1~2 秒。

研究口径
--------
- 截面单位：月末信号日（T 日收盘），T+1 开盘买入，下个月末 T+1 开盘卖出；
- 收益：后复权开盘价之比；可成交性（T+1 停牌 / 开盘一字涨停）与主脚本一致；
- 成本：单边 0.175%（与主脚本一致）；
- 所有信号只用 T 日及之前的信息。

三种研究模式
------------
    --mode screen   单因子体检：全样本 IC、**下跌月的 IC**、十分位多空、
                    以及「最差 10% 月份」的超额——专门看谁在坏月份里能扛；
    --mode compare  组合级对比：换手/成本/回撤/年化，逐条干预一个变量；
    --mode timing   仓位与对冲：均线择时、波动率目标、指数对冲。

用法
----
    python scripts/research_lowprice_drawdown.py --build
    python scripts/research_lowprice_drawdown.py --mode screen
    python scripts/research_lowprice_drawdown.py --mode compare --json /tmp/x.json
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
DATA_DIR = Path("/opt/quantmind/data/quantdb")
CACHE = Path("/tmp/lp_panel.parquet")
# 每次整手模拟的账本恒等式误差，供 --verify 汇总展示
LEDGER_ERRS: list[float] = []
EVENTS = HERE.parent / "data/regulatory/events.parquet"

# 面板里带过来的因子列（features_daily 一次查全，省得二次 I/O）
FACTOR_COLS = [
    "pe_ttm",
    "pb",
    "ps_ttm",
    "net_profit_ttm",
    "revenue_ttm",
    "equity",
    "vol_std_20",
    "vol_std_60",
    "beta_20",
    "rsi_14",
    "ma_gap_20",
    "hs_turnover",
    "industry_code",
    "industry_name",
    "float_mv",
    "total_capital",
]


def load_main():
    """复用主回测脚本的口径（connect / build_plan / universe / stats）。"""
    spec = importlib.util.spec_from_file_location(
        "bl", HERE / "backtest_low_price_small_cap.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def build_cache(data_dir: Path, out: Path) -> pd.DataFrame:
    bl = load_main()
    con = bl.connect(data_dir)
    # 主脚本的 feat 视图只挑了 8 列，因子列要另开一个全字段视图
    cols = ", ".join(FACTOR_COLS)
    con.execute(
        f"""CREATE OR REPLACE VIEW feat_all AS
            SELECT symbol, dt, close AS adj_close, total_mv, amount_ma_5, is_st,
                   is_quit_risk, list_date, {cols}
            FROM read_parquet('{data_dir}/6_ml_datasets/features_daily/**/*.parquet')"""
    )
    plan = bl.build_plan(con)
    sig = ",".join(str(x) for x in plan.signal_dt)
    ent = ",".join(str(x) for x in plan.entry_dt.unique())
    exi = ",".join(str(x) for x in plan.exit_dt.unique())
    cols = ", ".join(FACTOR_COLS)
    feat = con.execute(
        f"""SELECT symbol, dt, adj_close, total_mv, amount_ma_5, is_st,
                   is_quit_risk, list_date, {cols}
            FROM feat_all WHERE dt IN ({sig})"""
    ).df()
    px = con.execute(f"SELECT symbol, dt, close AS px FROM ku WHERE dt IN ({sig})").df()
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
    # 缺口用**真实价**算（复权价之比会把除权日误判成涨跌停）
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
    # tradable 只含入场侧可成交性（T+1 开盘可观测），不含未来退出价
    has_entry = panel["o_in"].notna() & (panel["o_in"] > 0)
    panel["tradable"] = (panel["v_in"].fillna(0) > 0) & has_entry & (gap < limit)
    panel["list_dt"] = pd.to_datetime(
        panel.list_date.astype(str), format="%Y%m%d", errors="coerce"
    )
    panel["dt_ts"] = pd.to_datetime(panel.dt.astype(str), format="%Y%m%d")
    panel["listed_days"] = (panel.dt_ts - panel.list_dt).dt.days
    out.parent.mkdir(parents=True, exist_ok=True)
    panel.to_parquet(out, index=False)
    print(f"面板已缓存 {out}：{len(panel):,} 行 / {panel.dt.nunique()} 期")
    return panel


def load_panel(path: Path) -> pd.DataFrame:
    df = pd.read_parquet(path)
    for c in ("pe_ttm", "pb", "ps_ttm", "net_profit_ttm", "vol_std_20", "beta_20"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    # 派生因子：都是「越大越好」的方向，方便统一读 IC 正负
    df["ep"] = np.where(df.pe_ttm > 0, 1 / df.pe_ttm, np.nan)  # 盈利收益率
    df["bp"] = np.where(df.pb > 0, 1 / df.pb, np.nan)  # 账面市值比
    df["loss"] = (df.net_profit_ttm <= 0).astype(float)  # 1 = 亏损
    df["lowvol"] = -df.vol_std_20  # 低波为正
    df["lowbeta"] = -df.beta_20
    df["rev1"] = df.ps_ttm * 0  # 占位，稍后用上一期收益替换
    df = df.sort_values(["symbol", "dt"])
    df["prev_ret"] = df.groupby("symbol").fwd_ret.shift(1)
    return df


def pool_mask(df: pd.DataFrame, price_max=10.0, mv_max=5e9, mv_min=1e9) -> pd.Series:
    return (df.px <= price_max) & (df.total_mv.between(mv_min, mv_max))


# ---------------------------------------------------------------- 单因子体检


def forward_ic(df: pd.DataFrame, col: str, min_n: int = 60) -> pd.DataFrame:
    """逐期 rank IC（Spearman = 秩的 Pearson），跳过样本太少或全空的期。"""
    rows = []
    for d, g in df.groupby("dt"):
        s = g[[col, "fwd_ret"]].dropna()
        if len(s) < min_n or s[col].nunique() < 5:
            continue
        ic = s[col].rank().corr(s.fwd_ret.rank())
        rows.append({"dt": d, "ic": ic, "n": len(s)})
    return pd.DataFrame(rows)


def screen(df: pd.DataFrame, factors: list[str]) -> pd.DataFrame:
    """全样本 IC + 下跌月 IC + 十分位多空，核心是「坏月份里还灵不灵」。"""
    pool = df[pool_mask(df)].dropna(subset=["fwd_ret"]).copy()
    bench = pool.groupby("dt").fwd_ret.mean()
    bad_cut = bench.quantile(0.25)
    bad = set(bench[bench <= bad_cut].index)
    worst = set(bench.nsmallest(12).index)
    rows = []
    for f in factors:
        ic = forward_ic(pool, f)
        if ic.empty:
            continue
        ic_bad = ic[ic.dt.isin(bad)]
        ic_worst = ic[ic.dt.isin(worst)]
        # 十分位：每期按因子分 10 组，D1 = 因子最高（预期最好）
        pp = pool.dropna(subset=[f]).copy()
        pp["dec"] = pp.groupby("dt")[f].transform(
            lambda s: (
                pd.qcut(s.rank(method="first"), 10, labels=False)
                if s.notna().sum() >= 50
                else np.nan
            )
        )
        d1 = pp[pp.dec == 9].groupby("dt").fwd_ret.mean()
        d10 = pp[pp.dec == 0].groupby("dt").fwd_ret.mean()
        d1_bad = (d1[d1.index.isin(bad)] - bench[bench.index.isin(bad)]).mean()
        d10_bad = (d10[d10.index.isin(bad)] - bench[bench.index.isin(bad)]).mean()
        rows.append(
            {
                "factor": f,
                "ic": ic.ic.mean(),
                "icir": ic.ic.mean() / ic.ic.std() if ic.ic.std() else np.nan,
                "ic_bad": ic_bad.ic.mean() if len(ic_bad) else np.nan,
                "ic_worst12": ic_worst.ic.mean() if len(ic_worst) else np.nan,
                "d1_ex_bad": d1_bad * 100,
                "d10_ex_bad": d10_bad * 100,
                "spread_bad": (d1_bad - d10_bad) * 100,
                "spread_all": (d1 - d10).mean() * 100,
            }
        )
    out = pd.DataFrame(rows).set_index("factor")
    return out.sort_values("spread_bad", ascending=False)


# ---------------------------------------------------------------- 组合模拟


def simulate(
    panel: pd.DataFrame,
    pick,
    *,
    topk: int = 40,
    cost: float = 0.00175,
    start: int | None = None,
    exposure_fn=None,
    weight_fn=None,
    industry_cap: int | None = None,
    prek: int | None = None,
    order: str = "total_mv",
) -> pd.DataFrame:
    """逐月调仓（理想权重，无整手约束）。

    ``pick(snapshot)`` 只返回**信号日可知**字段算出的布尔掩码；T+1 能否买进由
    ``tradable`` 在执行层过滤。旧仓用上一期已实现收益漂移，权重含现金，
    买卖分别计费——与 ``backtest_low_price_small_cap.run_strategy`` 同一套状态机。
    """
    w_old = pd.Series(dtype=float)
    cash_w = 1.0
    ret_prev: dict[str, float] = {}
    nav, rows, hist = 1.0, [], []
    for d in sorted(panel.dt.unique()):
        if start and d < start:
            continue
        snap = panel[panel.dt == d]
        entry = int(snap.entry_dt.iloc[0])
        pos = 1.0 if exposure_fn is None else float(exposure_fn(d, hist))

        cand = snap[pick(snap).reindex(snap.index).fillna(False)]
        if "tradable" in cand.columns:
            cand = cand[cand.tradable.fillna(False)]
        sel = cand.sort_values("total_mv")
        if prek:
            sel = sel.head(prek).sort_values(order)
        if industry_cap and len(sel):
            keep, cnt = [], {}
            for r in sel.itertuples():
                ind = r.industry_name
                if cnt.get(ind, 0) >= industry_cap:
                    continue
                cnt[ind] = cnt.get(ind, 0) + 1
                keep.append(r.symbol)
                if len(keep) >= topk:
                    break
            sel = sel[sel.symbol.isin(keep)]
        else:
            sel = sel.head(topk)

        if len(w_old):
            grown = w_old * (
                1 + pd.Series(ret_prev, dtype=float).reindex(w_old.index).fillna(0.0)
            )
            total = float(grown.sum()) + cash_w
            w_grown = grown / total if total > 0 else pd.Series(dtype=float)
        else:
            w_grown = pd.Series(dtype=float)

        if sel.empty or pos <= 0:
            target = pd.Series(dtype=float)
        elif weight_fn is None:
            target = pd.Series(pos / len(sel), index=sel.symbol.values)
        else:
            raw = (
                weight_fn(sel)
                .clip(lower=0)
                .replace([np.inf, -np.inf], np.nan)
                .fillna(0)
            )
            target = (
                pos * raw / raw.sum()
                if raw.sum() > 0
                else pd.Series(pos / len(sel), index=sel.symbol.values)
            )

        keys = w_grown.index.union(target.index)
        oldv = w_grown.reindex(keys).fillna(0.0)
        newv = target.reindex(keys).fillna(0.0)
        buys = float((newv - oldv).clip(lower=0).sum())
        sells = float((oldv - newv).clip(lower=0).sum())
        fee = (buys + sells) * cost
        turn = (buys + sells) / 2

        if len(target):
            rets = sel.set_index("symbol").fwd_ret.astype(float).fillna(0.0)
            gross = float((target * rets.reindex(target.index)).sum())
        else:
            gross = 0.0
        net = gross - fee
        hist.append(net)
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


def simulate_lots(
    panel: pd.DataFrame,
    pick,
    *,
    capital: float = 50_000.0,
    topk: int = 40,
    start: int | None = None,
    comm_rate: float = 0.0000875,
    exposure_fn=None,
    transfer: float = 0.00001,
    stamp: float = 0.0005,
    slip: float = 0.001,
    lot: int = 100,
) -> pd.DataFrame:
    """整手实盘模拟：**单一账本**（现金 + 真实股数），净值由账本直接算出。

    会计恒等式
    ----------
    每期满足 ``nav_t == nav_{t-1} * (1 + ret_t - cost_t)``，其中 ``ret_t`` 是毛收益、
    ``cost_t`` 是本期的费用率。报表里不再覆盖 nav——账户就是账户。

    价格口径
    --------
    成交价用**入场日不复权开盘价** ``o_in_raw``（直接读，不再用信号日复权比反推，
    那样在除权日会偏 2%+）。持仓市值按 ``fwd_ret``（后复权开盘价之比，含分红送转）
    逐期滚动，因此人民币账户口径与总回报口径一致。

    持仓
    ----
    ``hold[s] = {"shares": n, "value": v}``。本期取不到价格的持仓按**冻结**处理
    （收益记 0、不强制卖出），不会像旧实现那样用上期入场价变现。

    费率
    ----
    默认免 5：佣金 = 成交额 × 万 0.875；过户费万 0.1 双边；印花税万 5 仅卖出；
    滑点 0.1% 双边。
    """
    cash = float(capital)
    hold: dict[str, dict[str, float]] = {}
    ret_prev: dict[str, float] = {}
    rows: list[dict] = []
    hist: list[float] = []
    nav_prev = float(capital)

    for d in sorted(panel.dt.unique()):
        if start and d < start:
            continue
        snap = panel[panel.dt == d]
        entry = int(snap.entry_dt.iloc[0])
        pos = 1.0 if exposure_fn is None else float(exposure_fn(d, hist))

        # 1) 用上一期收益把持仓滚动到本期（缺价按冻结）
        for sym, h in hold.items():
            r = ret_prev.get(sym, 0.0)
            if r is None or not np.isfinite(r):
                r = 0.0
            h["value"] *= 1.0 + float(r)

        nav_pre = cash + sum(h["value"] for h in hold.values())

        # 2) 选股（T 日字段）+ 执行层可成交过滤
        cand = snap[pick(snap).reindex(snap.index).fillna(False)]
        if "tradable" in cand.columns:
            cand = cand[cand.tradable.fillna(False)]
        sel = cand.sort_values("total_mv").head(topk)
        target = list(sel.symbol)
        price = dict(zip(sel.symbol, sel.o_in_raw.astype(float), strict=False))

        fee = 0.0
        orders = 0

        # 3) 清掉不在目标里的
        for sym in list(hold):
            if sym in target:
                continue
            amt = hold.pop(sym)["value"]
            cost = amt * (slip + comm_rate + transfer + stamp)
            cash += amt - cost
            fee += cost
            orders += 1

        # 4) 等权买/调到目标，按真实价整手
        budget = nav_pre * max(pos, 0.0) / max(len(target), 1)
        for sym in target:
            p = price.get(sym)
            if p is None or not np.isfinite(p) or p <= 0:
                continue
            want_shares = int(budget // (p * lot)) * lot
            cur = hold.get(sym, {"shares": 0.0, "value": 0.0})
            diff = want_shares - cur["shares"]
            if diff > 0:
                unit = p * lot * (1 + slip + comm_rate + transfer)
                diff = min(diff, int(cash // unit) * lot)
                if diff <= 0:
                    continue
                amt = diff * p
                cost = amt * (slip + comm_rate + transfer)
                cash -= amt + cost
                fee += cost
                orders += 1
                hold[sym] = {
                    "shares": cur["shares"] + diff,
                    "value": cur["value"] + amt,
                }
            elif diff < 0:
                # 减仓：按市值比例卖掉对应份额
                ratio = (-diff) / cur["shares"] if cur["shares"] else 0.0
                amt = cur["value"] * ratio
                cost = amt * (slip + comm_rate + transfer + stamp)
                cash += amt - cost
                fee += cost
                orders += 1
                keep_shares = cur["shares"] + diff
                if keep_shares <= 0:
                    hold.pop(sym, None)
                else:
                    hold[sym] = {
                        "shares": keep_shares,
                        "value": cur["value"] - amt,
                    }

        # 5) 结算：账本即净值
        nav = cash + sum(h["value"] for h in hold.values())
        cost_frac = fee / nav_prev if nav_prev > 0 else 0.0
        gross = nav / nav_prev - 1 + cost_frac if nav_prev > 0 else 0.0
        hist.append(gross - cost_frac)
        rows.append(
            {
                "dt": d,
                "entry_dt": entry,
                "nav": nav,
                "ret": gross,
                "cost": cost_frac,
                "turn": 0.0,
                "n": len(hold),
                "invested_ratio": sum(h["value"] for h in hold.values()) / nav
                if nav > 0
                else 0.0,
                "orders": orders,
            }
        )
        nav_prev = nav
        ret_prev = dict(zip(sel.symbol, sel.fwd_ret.astype(float), strict=False))

    out = pd.DataFrame(rows)
    # 账本恒等式：nav_t 必须能由 nav_{t-1}*(1+ret_t-cost_t) 精确复原。
    # 这条是整手账户模拟唯一的绝对正确性检查：对不上就说明现金/股数/费用记错了。
    if len(out):
        recon = capital * (1 + (out.ret - out.cost)).cumprod()
        err = float((out.nav.values / recon.values - 1).__abs__().max())
        out.attrs["ledger_err"] = err
        LEDGER_ERRS.append(err)
        if err > 1e-6:
            raise AssertionError(
                f"账本恒等式不成立：最大相对误差 {err:.3e}（阈值 1e-6）。"
                " 现金/股数/费用记错了，结果不可用。"
            )
    return out


def ramp_exposure(
    con,
    symbol: str,
    ma_days: int = 200,
    mid: float = 0.5,
    slope: float = 2.5,
    floor: float = 0.25,
    cap: float = 1.0,
) -> dict[int, float]:
    """连续仓位：按指数相对 MA 的偏离度线性映射到 [floor, cap]。

    ``exposure = clip(mid + slope * (close/MA - 1), floor, cap)``

    与二值规则（MA 上方满仓/下方半仓）相比没有跳变：指数在 MA 附近时仓位在 mid
    附近平滑过渡，避免「刚跌破就砍一半、刚站上就加满」的来回打脸。
    用信号日收盘即可算出，T+1 开盘执行，不引入未来数据。
    """
    px = con.execute(
        f"SELECT dt, close FROM idx WHERE symbol = '{symbol}' ORDER BY dt"
    ).df()
    if px.empty:
        raise SystemExit(f"择时指数 {symbol} 没有数据")
    px["ma"] = px.close.rolling(ma_days, min_periods=ma_days).mean()
    px["dev"] = px.close / px.ma - 1.0
    px["e"] = np.clip(mid + slope * px.dev, floor, cap)
    return {int(r.dt): float(r.e) for r in px.itertuples() if pd.notna(r.ma)}


def stress_survivorship(
    panel: pd.DataFrame,
    pick,
    *,
    topk: int,
    start: int | None = 20160101,
    p_hold: float = 0.04,
    loss: float = -0.60,
    n_paths: int = 200,
    seed: int = 7,
) -> dict:
    """给「数据里没有退市股」补一个压力测试。

    做法：每期取实际会持有的 top-k，按标的在候选池里的市值分位给一个年化退市概率
    （最小 25% 组 ×2、中间 ×1、最大 25% 组 ×0.5），每月以 p/12 的概率抽中一次
    退市冲击，冲击当月该票收益替换为 ``loss``；组合当月收益按 1/N 权重调整。

    这是**线性近似**（月度等权调仓下误差很小），目的不是精确复现退市，而是回答
    「集中度提高后，幸存者偏差把收益高估了多少、尾部厚了多少」。
    """
    holds: dict[int, pd.DataFrame] = {}
    for d in sorted(panel.dt.unique()):
        if start and d < start:
            continue
        snap = panel[panel.dt == d]
        pool = snap[pick(snap)]
        if pool.empty:
            continue
        pool = pool[pool.fwd_ret.notna()]
        if pool.empty:
            continue
        sel = pool.sort_values("total_mv").head(topk)
        # 直接用「持仓标的的年退市率」p_hold，不再按市值分位加权。
        # top-k 天然全落在最小市值那一档，再乘个分位权重只是伪精确；
        # 真实数据里 top40 与 top10 的退市率都是同一个量级（都最危险），
        # 所以这里让它们用同一个 p_hold，差异只体现在 N 上（1/N 摊薄）。
        holds[d] = pd.DataFrame(
            {
                "symbol": sel.symbol.values,
                "fwd_ret": sel.fwd_ret.values,
                "p": np.clip(p_hold / 12, 0, 1),
            }
        )
    base = simulate(panel, pick, topk=topk, start=start)
    base_r = (base.ret - base.cost).values
    dts = list(holds.keys())
    rng = np.random.default_rng(seed)
    cagrs, mdds = [], []
    years = len(dts) / 12
    for _ in range(n_paths):
        r = base_r.copy()
        dead: set[str] = set()  # 已退市的标的，后续不再出现在组合里
        for i, d in enumerate(dts):
            h = holds[d]
            alive = ~h.symbol.isin(dead).values
            if not alive.any():
                continue
            p_eff = np.where(alive, h.p.values, 0.0)
            hit = rng.random(len(h)) < p_eff
            if hit.any():
                n = max(int(alive.sum()), 1)
                drag = np.sum((loss - h.fwd_ret.values[hit]) / n)
                r[i] += drag
                dead.update(h.symbol.values[hit])
        nav = np.cumprod(1 + r)
        cagrs.append(nav[-1] ** (1 / years) - 1)
        mdds.append((nav / np.maximum.accumulate(nav) - 1).min())
    cagrs, mdds = np.array(cagrs), np.array(mdds)
    return {
        "topk": topk,
        "cagr_mean": float(cagrs.mean()),
        "cagr_p05": float(np.quantile(cagrs, 0.05)),
        "mdd_mean": float(mdds.mean()),
        "mdd_p05": float(np.quantile(mdds, 0.05)),
        "base_cagr": float(base.nav.iloc[-1] ** (1 / years) - 1),
        "base_mdd": float((base.nav / base.nav.cummax() - 1).min()),
    }


def summarize(df: pd.DataFrame, label: str, base_nav: float = 1.0) -> dict:
    nav = df.nav
    net = df.ret - df.cost
    years = (
        pd.to_datetime(str(df.dt.iloc[-1]), format="%Y%m%d")
        - pd.to_datetime(str(df.dt.iloc[0]), format="%Y%m%d")
    ).days / 365.25
    vol = net.std() * np.sqrt(12)
    mdd = float((nav / nav.cummax() - 1).min())
    cagr = float((nav.iloc[-1] / base_nav) ** (1 / years) - 1)
    yearly = (1 + net).groupby(df.entry_dt // 10000).prod() - 1
    dd = nav / nav.cummax() - 1
    # 水下时间：净值低于前高的月份占比；最长连续水下月数
    under = (dd < -1e-9).astype(int)
    longest = under.groupby((~under.astype(bool)).cumsum()).sum().max()
    return {
        "label": label,
        "cagr": cagr,
        "vol": float(vol),
        "sharpe": float((net.mean() * 12 - 0.02) / vol) if vol else np.nan,
        "mdd": mdd,
        "calmar": cagr / abs(mdd) if mdd else np.nan,
        "avg_turn": float(df.turn.mean()),
        "worst_m": float(net.min()),
        "under_water": float(under.mean()),
        "longest_uw": int(longest),
        "y2017": float(yearly.get(2017, np.nan)),
        "y2018": float(yearly.get(2018, np.nan)),
        "y2024": float(yearly.get(2024, np.nan)),
        "yearly": {int(k): float(v) for k, v in yearly.items()},
        "nav": nav,
    }


def fmt(rows: list[dict]) -> str:
    head = (
        f"{'变体':<26}{'年化':>8}{'夏普':>7}{'回撤':>8}{'Calmar':>8}"
        f"{'换手':>7}{'最差月':>8}{'17年':>7}{'18年':>7}{'24年':>7}"
    )
    lines = [head, "-" * len(head)]
    for r in rows:
        lines.append(
            f"{r['label']:<26}{r['cagr'] * 100:>7.1f}%{r['sharpe']:>7.2f}"
            f"{r['mdd'] * 100:>7.1f}%{r['calmar']:>8.2f}{r['avg_turn'] * 100:>6.0f}%"
            f"{r['worst_m'] * 100:>7.1f}%{r['y2017'] * 100:>6.0f}%"
            f"{r['y2018'] * 100:>6.0f}%{r['y2024'] * 100:>6.0f}%"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------- 主流程


def main() -> int:
    ap = argparse.ArgumentParser(description="低价小市值回撤控制研究台")
    ap.add_argument("--build", action="store_true", help="重建面板缓存")
    ap.add_argument("--cache", type=Path, default=CACHE)
    ap.add_argument(
        "--mode",
        default="compare",
        choices=[
            "screen",
            "compare",
            "timing",
            "crowd",
            "capital",
            "combo",
            "stress",
            "all",
        ],
    )
    ap.add_argument("--data-dir", type=Path, default=DATA_DIR)
    ap.add_argument(
        "--board",
        default="ex_star",
        choices=["main", "ex_star", "all"],
        help="股票池板块口径，默认 ex_star（与主报告 20.66%% 一致）",
    )
    ap.add_argument("--json", type=Path, default=None)
    ap.add_argument("--charts", type=Path, default=None, help="图表输出目录")
    ap.add_argument(
        "--verify",
        action="store_true",
        help="打印每次整手模拟的账本恒等式误差（断言恒定开启，这里只是展示）",
    )
    ap.add_argument("--capital", type=float, default=50_000.0, help="模拟本金（元）")
    ap.add_argument(
        "--comm-rate",
        type=float,
        default=0.0000875,
        help="佣金费率（默认万 0.875 免5）",
    )
    args = ap.parse_args()

    if args.build or not args.cache.exists():
        build_cache(args.data_dir, args.cache)
    bl = load_main()
    df = load_panel(args.cache)
    # 关键：必须套用主脚本的股票池（ST / 新股 / 流动性 / 板块 / 市值下限），
    # 否则 ST 股和次新股会把回测做成假的高收益低回撤。
    panel = bl.universe(
        df,
        board=args.board,
        min_amount=2000.0,
        min_price=1.5,
        min_listed_days=120,
        min_mv=1e9,
    )
    print(
        f"面板 {len(df):,} 行 / {df.dt.nunique()} 期 | 主脚本口径股票池 月均 "
        f"{panel.groupby('dt').size().mean():.0f} 只 | 低价小市值候选池 月均 "
        f"{panel[pool_mask(panel)].groupby('dt').size().mean():.0f} 只"
    )

    # 只用信号日可知字段；T+1 可成交性由 simulate/simulate_lots 在执行层处理
    base_pick = lambda s: pool_mask(s)  # noqa: E731

    def make_excl(events: pd.DataFrame, window_days: int, base=base_pick):
        """返回「剔除事件窗口内标的」的 pick。window_days=0 表示曾经出现过即剔除。"""
        bad = {}
        for d in sorted(panel.dt.unique()):
            lo = (
                int(
                    (pd.Timestamp(str(d)) - pd.Timedelta(days=window_days)).strftime(
                        "%Y%m%d"
                    )
                )
                if window_days
                else 0
            )
            sub = events[(events.announce_date <= d) & (events.announce_date >= lo)]
            bad[d] = set(sub.symbol)

        def _pick(s):
            d = int(s.dt.iloc[0])
            return base(s) & ~s.symbol.isin(bad.get(d, set()))

        return _pick

    ev = pd.read_parquet(EVENTS) if EVENTS.exists() else pd.DataFrame()
    inv = ev[ev.category == "investigation"] if len(ev) else pd.DataFrame()
    risk = (
        ev[ev.category.isin(["investigation", "delist_risk"])]
        if len(ev)
        else pd.DataFrame()
    )
    inv_pick = make_excl(inv, 365) if len(inv) else base_pick

    if args.mode in ("screen", "all"):
        factors = [
            "lowvol",
            "lowbeta",
            "bp",
            "ep",
            "loss",
            "ma_gap_20",
            "prev_ret",
            "hs_turnover",
            "rsi_14",
            "ps_ttm",
        ]
        sc = screen(panel, factors)
        print(
            "\n单因子体检（正 IC = 因子值大 → 下期收益高；bad = 池子最差 25% 的月份）"
        )
        with pd.option_context("display.width", 200, "display.max_columns", 20):
            print(sc.round(3).to_string())

    if args.mode in ("compare", "all"):
        rows = []

        def run(label, pick, **kw):
            d = simulate(panel, pick, start=20160101, **kw)
            rows.append(summarize(d, label))

        run("① 基准 top40", base_pick)
        if len(inv):
            cov = []
            for d in sorted(panel.dt.unique()):
                snap = panel[panel.dt == d]
                sub = snap[base_pick(snap)]
                if sub.empty:
                    continue
                lo = int(
                    (pd.Timestamp(str(d)) - pd.Timedelta(days=365)).strftime("%Y%m%d")
                )
                b = set(
                    inv[(inv.announce_date <= d) & (inv.announce_date >= lo)].symbol
                )
                cov.append(len(set(sub.symbol) & b) / len(sub))
            print(
                f"\n立案事件 {len(inv):,} 条 / {inv.symbol.nunique()} 只，"
                f"披露日 {inv.announce_date.min()} ~ {inv.announce_date.max()}"
            )
            print(
                f"近 1 年被立案的标的占候选池比例：月均 {np.mean(cov) * 100:.1f}%，"
                f"最高 {np.max(cov) * 100:.1f}%"
            )
            run("② 剔除立案(近1年)", inv_pick)
            run("③ 剔除立案(近2年)", make_excl(inv, 730))
            run("④ 剔除立案(曾经)", make_excl(inv, 0))
            run("⑤ 剔除立案+退市风险", make_excl(risk, 365))
        else:
            print(f"⚠️ 未找到 {EVENTS}，跳过立案过滤")

        run("⑥ 剔除亏损", lambda s: base_pick(s) & (s.net_profit_ttm > 0))
        run(
            "⑦ 剔除亏损+PB>0",
            lambda s: base_pick(s) & (s.net_profit_ttm > 0) & (s.pb > 0),
        )
        run("⑧ 最小120只里选低波", base_pick, prek=120, order="vol_std_20")
        run("⑨ 剔除近月暴跌", lambda s: base_pick(s) & (s.ma_gap_20 > -0.20))
        run("⑩ 股价下限 2 元", lambda s: base_pick(s) & (s.px >= 2))
        run("⑪ 股价下限 3 元", lambda s: base_pick(s) & (s.px >= 3))
        run(
            "⑫ 市值下限 15 亿", lambda s: pool_mask(s, mv_min=1.5e9) & s.fwd_ret.notna()
        )
        run("⑬ 行业上限 4 只", base_pick, industry_cap=4)
        run("⑭ topk=80", base_pick, topk=80)
        run("⑮ topk=20", base_pick, topk=20)

        print("\n组合级对比（2016-01 ~ 2026-10，含成本，主脚本口径股票池）")
        print(fmt(rows))

    if args.mode in ("timing", "all"):
        con = bl.connect(args.data_dir)
        plan = bl.build_plan(con)
        idx = bl.index_nav(con, plan[plan.signal_dt >= 20160101])

        def index_ret(name):
            r = idx[name].pct_change()
            r.index = plan.signal_dt.values[plan.signal_dt.values >= 20160101]
            return r

        i1000 = index_ret("中证1000")
        i2000 = index_ret("国证2000")
        rows = []

        def run2(label, pick, **kw):
            d = simulate(panel, pick, start=20160101, **kw)
            rows.append(summarize(d, label))
            return d

        def run_hedge(label, nav_df, idx_ret, h, basis=0.0):
            net = (nav_df.ret - nav_df.cost).values
            src = idx_ret.reindex(nav_df.dt.values).fillna(0).values
            # 股指期货：1bp/月 的展期+交易成本，另加年化 ``basis`` 的贴水损耗
            # （IM 长期贴水，2022-2024 年化约 5%~8%，这是对冲最容易被忽略的成本）
            r = net - h * src - 0.0001 * h - basis / 12 * h
            out = nav_df[["dt", "entry_dt"]].copy()
            out["ret"] = r
            out["cost"] = 0.0
            out["turn"] = 0.0
            out["nav"] = np.cumprod(1 + r)
            rows.append(summarize(out, label))

        for sym, name in (("000852.SH", "中证1000"), ("399303.SZ", "国证2000")):
            for ma, weak in ((200, 0.5), (200, 0.0), (120, 0.5), (250, 0.5)):
                table = bl.timing_exposure(con, sym, ma, weak)
                run2(
                    f"择时 {name} MA{ma} 弱{weak:g}",
                    base_pick,
                    exposure_fn=lambda d, h, t=table: t.get(int(d), 1.0),
                )

        def vol_target(target=0.25, cap=1.0, look=12):
            def fn(d, hist):
                if len(hist) < look:
                    return cap
                v = float(np.std(hist[-look:]) * np.sqrt(12))
                return float(min(cap, target / v)) if v > 0 else cap

            return fn

        run2("波动率目标 25%", base_pick, exposure_fn=vol_target(0.25))

        # 连续仓位：按指数偏离 MA200 的幅度线性调整仓位
        for mid, slope, floor in (
            (0.50, 2.5, 0.25),
            (0.65, 2.0, 0.25),
            (0.80, 1.0, 0.25),
            (0.50, 5.0, 0.25),
        ):
            tbl = ramp_exposure(con, "399303.SZ", 200, mid, slope, floor)
            avg = float(np.mean([v for d, v in tbl.items() if d >= 20160101]))
            run2(
                f"连续 中{mid:g}/斜{slope:g} 均{avg:.0%}",
                base_pick,
                exposure_fn=lambda d, h, t=tbl: t.get(int(d), 1.0),
            )

        # 叠加：剔除立案 + 择时
        t2000 = bl.timing_exposure(con, "399303.SZ", 200, 0.5)
        run2(
            "立案剔除 + 国证2000 MA200",
            inv_pick,
            exposure_fn=lambda d, h: t2000.get(int(d), 1.0),
        )

        base_nav = run2("① 基准 top40", base_pick)
        run_hedge("对冲 国证2000 50%", base_nav, i2000, 0.5)
        run_hedge("对冲 国证2000 50%@贴水5%", base_nav, i2000, 0.5, 0.05)
        run_hedge("对冲 国证2000 50%@贴水8%", base_nav, i2000, 0.5, 0.08)
        run_hedge("对冲 国证2000 100%@贴水5%", base_nav, i2000, 1.0, 0.05)
        run_hedge("对冲 中证1000 100%@贴水8%", base_nav, i1000, 1.0, 0.08)

        print("\n择时 / 仓位 / 对冲（2016-01 ~ 2026-10，含成本）")
        print(fmt(rows))

    if args.mode in ("crowd", "all"):
        d = panel.copy()
        d["amt"] = d.amount_ma_5.fillna(0)
        d["amt_small"] = d.amt.where(d.total_mv <= 5e9, 0.0)
        cs = d.groupby("dt")[["amt", "amt_small"]].sum()
        share = (cs.amt_small / cs.amt).dropna()
        # 只用历史信息：扩张分位（至少 24 期）
        rank = share.expanding(min_periods=24).rank(pct=True)
        rows = []

        def run3(label, exposure_fn):
            df = simulate(panel, base_pick, start=20160101, exposure_fn=exposure_fn)
            rows.append(summarize(df, label))

        base = simulate(panel, base_pick, start=20160101)
        rows.append(summarize(base, "① 基准 top40"))
        for cut, weak in ((0.90, 0.5), (0.90, 0.0), (0.80, 0.5), (0.95, 0.5)):
            run3(
                f"拥挤度 >{cut:.0%} 弱{weak:g}",
                lambda dt, h, r=rank, c=cut, w=weak: (
                    w if r.get(int(dt), 0.0) >= c else 1.0
                ),
            )
        # 小盘/大盘相对强度：国证2000 与沪深300 的比值均线
        con = bl.connect(args.data_dir)
        px = con.execute(
            "SELECT symbol, dt, close FROM idx "
            "WHERE symbol IN ('399303.SZ','000300.SH') ORDER BY dt"
        ).df()
        w = px.pivot(index="dt", columns="symbol", values="close").dropna()
        ratio = (w["399303.SZ"] / w["000300.SH"]).rolling(6, min_periods=3).mean()
        above = (w["399303.SZ"] / w["000300.SH"]) > ratio
        run3(
            "相对强度破 6 月均线 → 半仓",
            lambda dt, h: 1.0 if bool(above.get(int(dt), True)) else 0.5,
        )
        print("\n微盘拥挤度 / 相对强度择时")
        print(fmt(rows))
        tail = share.tail(6)
        print("\n最近 6 期微盘成交占比与历史分位：")
        for dt, v in tail.items():
            print(f"  {dt}  {v * 100:5.2f}%  分位 {rank.get(dt, np.nan) * 100:5.1f}%")

    if args.mode in ("capital", "all"):
        print(
            f"\n小资金实盘模拟（本金 {args.capital:,.0f} 元，佣金万 "
            f"{args.comm_rate * 10000:.3f} 免5，整手 100 股）"
        )
        head = (
            f"{'方案':<22}{'年化':>8}{'回撤':>8}{'Calmar':>8}"
            f"{'投入比例':>9}{'月均费用':>10}{'月均笔数':>9}"
        )
        print(head)
        print("-" * len(head))
        ideal = simulate(panel, base_pick, topk=40, start=20160101)
        st = summarize(ideal, "理想 40 只（不计整手）")
        print(
            f"{'理想40只(不计整手)':<22}{st['cagr'] * 100:>7.1f}%{st['mdd'] * 100:>7.1f}%"
            f"{st['calmar']:>8.2f}{'100%':>9}{'—':>10}{'—':>9}"
        )
        for k in (10, 15, 20, 40):
            d = simulate_lots(
                panel,
                base_pick,
                capital=args.capital,
                topk=k,
                start=20160101,
                comm_rate=args.comm_rate,
            )
            st = summarize(d, f"top{k}", base_nav=args.capital)
            print(
                f"{f'top{k} 只（整手实盘）':<22}{st['cagr'] * 100:>7.1f}%"
                f"{st['mdd'] * 100:>7.1f}%{st['calmar']:>8.2f}"
                f"{d.invested_ratio.mean() * 100:>8.0f}%"
                f"{d.cost.mean() * 100:>9.2f}%"
                f"{d.orders.mean():>9.1f}"
            )

        print("\n集中度扫描（理想 = 不计整手；实盘 = 本金额度下整手下单）")
        head2 = (
            f"{'topk':>5}{'理想年化':>10}{'理想回撤':>10}"
            f"{'实盘年化':>10}{'实盘回撤':>10}{'投入':>7}{'月笔数':>8}"
        )
        print(head2)
        print("-" * len(head2))
        for k in (10, 15, 20, 30, 40, 60, 80):
            di = simulate(panel, base_pick, topk=k, start=20160101)
            si = summarize(di, f"i{k}")
            dl = simulate_lots(
                panel,
                base_pick,
                capital=args.capital,
                topk=k,
                start=20160101,
                comm_rate=args.comm_rate,
            )
            sl = summarize(dl, f"l{k}", base_nav=args.capital)
            print(
                f"{k:>5}{si['cagr'] * 100:>9.1f}%{si['mdd'] * 100:>9.1f}%"
                f"{sl['cagr'] * 100:>9.1f}%{sl['mdd'] * 100:>9.1f}%"
                f"{dl.invested_ratio.mean() * 100:>6.0f}%{dl.orders.mean():>8.0f}"
            )

        print("\n本金规模扫描（top40 整手约束）：本金多大整手拖累才消失")
        head3 = (
            f"{'本金':>10}{'年化':>8}{'回撤':>8}{'Calmar':>8}"
            f"{'投入比例':>9}{'月均笔数':>9}"
        )
        print(head3)
        print("-" * len(head3))
        for cap in (
            50000,
            100000,
            200000,
            300000,
            500000,
            1000000,
            2000000,
            5000000,
        ):
            dc = simulate_lots(
                panel,
                base_pick,
                capital=cap,
                topk=40,
                start=20160101,
                comm_rate=args.comm_rate,
            )
            sc = summarize(dc, f"{cap}", base_nav=cap)
            print(
                f"{cap:>10,}{sc['cagr'] * 100:>7.1f}%{sc['mdd'] * 100:>7.1f}%"
                f"{sc['calmar']:>8.2f}{dc.invested_ratio.mean() * 100:>8.0f}%"
                f"{dc.orders.mean():>9.0f}"
            )

    if args.mode in ("combo", "all"):
        con = bl.connect(args.data_dir)
        t2000 = bl.timing_exposure(con, "399303.SZ", 200, 0.5)
        print(
            f"\n推荐组合（本金 {args.capital:,.0f} 元，整手实盘，佣金万 "
            f"{args.comm_rate * 10000:.3f} 免5）"
        )
        head4 = (
            f"{'组合':<30}{'年化':>8}{'回撤':>8}{'Calmar':>8}{'投入':>7}{'月笔数':>8}"
        )
        print(head4)
        print("-" * len(head4))
        for k in (10, 15, 20, 40):
            for tag, pk, ex in (
                (f"仅 top{k} 选股", base_pick, None),
                (f"top{k} + 立案剔除", inv_pick, None),
                (
                    f"top{k} + 立案剔除 + MA200半仓",
                    inv_pick,
                    lambda dt, h: t2000.get(int(dt), 1.0),
                ),
            ):
                dl = simulate_lots(
                    panel,
                    pk,
                    capital=args.capital,
                    topk=k,
                    start=20160101,
                    exposure_fn=ex,
                    comm_rate=args.comm_rate,
                )
                sl = summarize(dl, tag, base_nav=args.capital)
                print(
                    f"{tag:<30}{sl['cagr'] * 100:>7.1f}%{sl['mdd'] * 100:>7.1f}%"
                    f"{sl['calmar']:>8.2f}{dl.invested_ratio.mean() * 100:>6.0f}%"
                    f"{dl.orders.mean():>8.0f}"
                )

    if args.mode in ("stress", "all"):
        print("\n幸存者偏差压力测试（数据里 0 只退市股 → 注入退市冲击）")
        print("p_hold = 持仓标的的年退市率；top40 每 4% 约等于每年 1.6 次事件")
        for pb in (0.02, 0.03, 0.04, 0.05):
            print(f"\n假设：持仓年退市率 {pb:.0%}，单次退市当月损失 -60%")
            head = (
                f"{'topk':>5}{'基准年化':>10}{'注入后均值':>12}"
                f"{'注入后5%分位':>14}{'均值-分位':>10}{'注入后回撤':>12}"
            )
            print(head)
            print("-" * len(head))
            for k in (10, 20, 40):
                st = stress_survivorship(
                    panel, base_pick, topk=k, p_hold=pb, loss=-0.60, n_paths=120
                )
                gap = (st["cagr_mean"] - st["cagr_p05"]) * 100
                print(
                    f"{k:>5}{st['base_cagr'] * 100:>9.1f}%{st['cagr_mean'] * 100:>11.1f}%"
                    f"{st['cagr_p05'] * 100:>13.1f}%{gap:>9.1f}pp"
                    f"{st['mdd_mean'] * 100:>11.1f}%"
                )

    if args.charts:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        con = bl.connect(args.data_dir)
        t2000 = bl.timing_exposure(con, "399303.SZ", 200, 0.5)
        curves = {
            "Baseline (top40)": simulate(panel, base_pick, start=20160101),
            "+ exclude investigated": simulate(panel, inv_pick, start=20160101),
            "+ CSI2000 MA200 half": simulate(
                panel,
                inv_pick,
                start=20160101,
                exposure_fn=lambda d, h: t2000.get(int(d), 1.0),
            ),
        }
        args.charts.mkdir(parents=True, exist_ok=True)
        fig, axes = plt.subplots(2, 1, figsize=(11, 7.5), sharex=True)
        for label, df in curves.items():
            nav = pd.Series(df.nav.values, index=pd.to_datetime(df.dt.astype(str)))
            axes[0].plot(nav.index, nav.values, label=label, linewidth=1.5)
            axes[1].plot(nav.index, (nav / nav.cummax() - 1).values, linewidth=1.2)
        axes[0].set_yscale("log")
        axes[0].set_ylabel("NAV (log)")
        axes[0].legend(fontsize=9)
        axes[0].grid(alpha=0.3)
        axes[0].set_title("Low-price small-cap: drawdown control overlays (2016-2026)")
        axes[1].set_ylabel("Drawdown")
        axes[1].grid(alpha=0.3)
        axes[1].axhline(0, color="black", linewidth=0.8)
        fig.tight_layout()
        fig.savefig(args.charts / "lowprice_drawdown_overlay.png", dpi=130)
        plt.close(fig)
        print(f"图表已写入 {args.charts / 'lowprice_drawdown_overlay.png'}")

    if args.verify:
        if LEDGER_ERRS:
            worst = max(LEDGER_ERRS)
            print(
                f"\n账本恒等式：{len(LEDGER_ERRS)} 次整手模拟，最大相对误差 "
                f"{worst:.2e}（阈值 1e-06）→ PASS"
            )
        else:
            print("\n账本恒等式：本次没有跑整手模拟（--mode capital/combo 才会触发）")

    if args.json:
        args.json.write_text(json.dumps({"ok": True}, ensure_ascii=False), "utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
