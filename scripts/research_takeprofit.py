#!/usr/bin/env python3
"""止盈（+20% 触价单）研究：可复现模拟器 + 样本外分割 + 现金拖累核算。

背景
----
`docs/主板微盘15只_实盘手册.md` §2.5 的止盈结论（25.8% / -22.0% / Calmar 1.18）
来自一个「独立模拟器」，但那份代码**不在仓库里**，无法复现。本脚本把它重写出来，
并补齐三件没做的事：

    A. 样本外分割：2017-2021 设计期 / 2022-2026 验证期，分别看「加止盈」的提升
    B. 现金拖累：止盈后现金闲置多少、分布如何、止盈后当月末是否买回
    C. 口径核对：止盈触发笔数、胜率、触发价 vs 成交价

诚实声明
--------
* 本模拟器**只用不复权日线**记账，不计分红送转（约 -1~2pp/年）。
  因此绝对收益会**低于**手册，只用于**加止盈 vs 不加止盈**的相对比较。
* 触价判定用**当日最高价**（触价单是盘中触发的），成交价取 ``max(开盘价, 触发价)``
  —— 跳空跳过触发价时按开盘价成交（比触发价更好）。
* 整手、免 5 佣金、印花税仅卖出，与手册 §3 一致。

用法
----
    python scripts/research_takeprofit.py --build          # 首次：重建面板缓存（约 2 分钟）
    python scripts/research_takeprofit.py --tp 0.20        # 加 20% 止盈
    python scripts/research_takeprofit.py --no-tp          # 基线
    python scripts/research_takeprofit.py --split          # A：样本内 vs 样本外
    python scripts/research_takeprofit.py --cash           # B：现金拖累
    python scripts/research_takeprofit.py --rebuy          # 止盈后当月末是否买回
    python scripts/research_takeprofit.py --breadth 30     # 加广度择时（<30% → e≤0.3）
    python scripts/research_takeprofit.py --tp 0.20 --breadth 30 \
        --aw-file data/aw_8020.parquet                     # 闲钱按 80/20 全候记账
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
DATA_DIR = Path("/opt/quantmind/data/quantdb")
CACHE = Path("/tmp/lp_panel.parquet")
EVENTS = HERE.parent / "data/regulatory/events.parquet"

# 成本（手册 §3：佣金万 0.875 免 5）
COMM = 0.0000875
TRANSFER = 0.00001
STAMP = 0.0005
SLIP = 0.001
BUY_COST = SLIP + COMM + TRANSFER                # ≈ 0.11%
SELL_COST = SLIP + COMM + TRANSFER + STAMP       # ≈ 0.16%
LOT = 100

# 手册推荐版参数
TOPK = 15
PRICE_MAX = 10.0
MV_MAX = 5e9
MIN_MV = 1e9
MIN_PRICE = 1.5
MIN_AMOUNT = 2000.0
MIN_LISTED = 120
TIMING_SYMBOL = "399303.SZ"   # 国证 2000
TIMING_MA = 200
WEAK = 0.5                    # MA200 下方半仓
MONTH_RULE = {1: 0.5, 4: 0.0}  # 1 月半仓、4 月空仓（按**入场月**）


def load_bl():
    spec = importlib.util.spec_from_file_location(
        "bl", HERE / "backtest_low_price_small_cap.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["bl"] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# 数据
# ---------------------------------------------------------------------------
def ensure_cache(bl, force: bool) -> pd.DataFrame:
    if force or not CACHE.exists():
        print(f"重建面板缓存 → {CACHE}（约 2 分钟）")
        spec = importlib.util.spec_from_file_location(
            "rd", HERE / "research_lowprice_drawdown.py"
        )
        rd = importlib.util.module_from_spec(spec)
        sys.modules["rd"] = rd
        spec.loader.exec_module(rd)
        rd.build_cache(DATA_DIR, CACHE)
    return pd.read_parquet(CACHE)


def build_daily(con, symbols: list[str], start: int, end: int) -> dict[str, pd.DataFrame]:
    """每只股票一张按 dt 索引的日线表（不复权：执行价用真实价）。"""
    lst = ",".join(f"'{s}'" for s in sorted(set(symbols)))
    # 主脚本的 ku 视图只挑了 open/close 两列，这里直接读 parquet 拿 high/low
    rel = DATA_DIR / "1_kline_data/daily_unadjusted"
    df = con.execute(
        f"""SELECT symbol, dt, open, high, low, close
            FROM read_parquet('{rel}/**/*.parquet')
            WHERE symbol IN ({lst}) AND dt >= {start} AND dt <= {end}
            ORDER BY symbol, dt"""
    ).df()
    out = {}
    for sym, g in df.groupby("symbol", sort=False):
        out[sym] = g.set_index("dt")[["open", "high", "low", "close"]]
    return out


def build_pool(bl, panel: pd.DataFrame) -> pd.DataFrame:
    """按手册口径筛出候选池（信号日可知字段，不含未来）。"""
    base = bl.universe(
        panel, board="main", min_amount=MIN_AMOUNT, min_price=MIN_PRICE,
        min_listed_days=MIN_LISTED, min_mv=MIN_MV,
    )
    base = base[(base.px <= PRICE_MAX) & (base.total_mv <= MV_MAX)].copy()
    if EVENTS.exists():
        blz = bl.investigation_blacklist(EVENTS, base.dt.unique(), 120)
        keep = [r.symbol not in blz.get(int(r.dt), set()) for r in base.itertuples()]
        base = base[np.array(keep, dtype=bool)]
    return base.sort_values(["dt", "total_mv"])


def build_picks(pool: pd.DataFrame, k: int = TOPK) -> pd.DataFrame:
    return pool.groupby("dt", sort=True).head(k)[["dt", "symbol", "total_mv"]]


def build_rank(pool: pd.DataFrame, k: int = 60) -> dict[int, list[str]]:
    """每期按市值升序的候选序列（补仓时从这里挑「下一名」）。"""
    return {
        int(d): list(g.symbol)
        for d, g in pool.groupby("dt", sort=True).head(k).groupby("dt")
    }


def build_exposure(bl, con, plan: pd.DataFrame) -> dict[int, float]:
    ma = bl.timing_exposure(con, TIMING_SYMBOL, TIMING_MA, WEAK)
    out = {}
    for r in plan.itertuples():
        m = int(str(r.entry_dt)[4:6])           # 入场月
        e = float(ma.get(int(r.signal_dt), 1.0))
        if m == 4:                              # 4 月空仓，不管均线
            out[int(r.signal_dt)] = 0.0
        elif m == 1:                            # 1 月半仓：取 min(e, 0.5)，不是相乘
            out[int(r.signal_dt)] = min(e, 0.5)
        else:
            out[int(r.signal_dt)] = e
    return out


def build_breadth(con, ma_days: int = 20) -> dict[int, float]:
    """全 A 股「收盘 > MA20」占比（%），口径同 docs/广度择时_过拟合检验.md。"""
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


def parse_weights(spec: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        sym, _, w = part.partition(":")
        out[sym.strip()] = float(w)
    total = sum(out.values())
    if total <= 0:
        raise SystemExit(f"--aw-weights 权重必须为正：{spec}")
    return {k: v / total for k, v in out.items()}


def load_aw(path: Path, weights: dict[str, float]) -> dict[int, float]:
    """全天候「每日收益」表：dt,symbol,close（按权重重算）或 dt,aw_ret（直接用）。"""
    if not path.exists():
        raise SystemExit(f"全天候收益文件不存在：{path}")
    df = (
        pd.read_parquet(path)
        if path.suffix.lower() in {".parquet", ".pq"}
        else pd.read_csv(path)
    )
    cols = set(df.columns)
    if "aw_ret" in cols:
        return {int(r.dt): float(r.aw_ret) for r in df.itertuples()}
    if not {"symbol", "dt", "close"} <= cols:
        raise SystemExit("--aw-file 需要列 dt,symbol,close（或 dt,aw_ret）")
    df["dt"] = df["dt"].astype(int)
    px = df.pivot_table(index="dt", columns="symbol", values="close").sort_index()
    ret = px.pct_change().fillna(0.0)
    missing = [s for s in weights if s not in ret.columns]
    if missing:
        raise SystemExit(f"--aw-file 缺这些标的：{missing}")
    blend = sum(ret[s] * w for s, w in weights.items())
    return {int(d): float(v) for d, v in blend.items()}


# ---------------------------------------------------------------------------
# 模拟
# ---------------------------------------------------------------------------
def simulate(
    plan: pd.DataFrame,
    picks: pd.DataFrame,
    daily: dict[str, pd.DataFrame],
    exposure: dict[int, float],
    *,
    tp: float | None,
    capital: float = 300_000.0,
    start: int = 20170101,
    end: int = 20991231,
    rebuy: bool = True,
    topk: int = TOPK,
    rank: dict[int, list[str]] | None = None,
    refill: bool = False,
    cost: float | None = None,
    aw: dict[int, float] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """逐日账本。返回 (日净值, 止盈明细)。

    ``refill=True``：止盈卖出后，**次日开盘**用这笔钱买排名下一个的候选股
    （按信号日的市值升序，跳过已持有和本月刚卖过的），不再干等月末调仓。

    ``aw``：全天候每日收益（小数）。给了就把「闲钱」按全候副本记账——
    现金不再是 0 收益，而是随 80/20 组合每日复利；买卖股票时在两者之间划转。
    不给则等价于现金 0 收益（旧口径）。
    """
    buy_c = cost if cost is not None else BUY_COST
    sell_c = cost if cost is not None else SELL_COST
    by_dt = {int(d): g for d, g in picks.groupby("dt")}
    aw_dates = np.array(sorted(aw)) if aw else None
    aw_levels = (
        np.concatenate([[1.0], np.cumprod([1.0 + aw[d] for d in aw_dates])])
        if aw
        else None
    )

    def aw_level(d: int) -> float:
        """截至 d 收盘的全天候净值（起始 1.0）；无数据 = 1.0（现金口径）。"""
        if aw_dates is None:
            return 1.0
        return float(aw_levels[np.searchsorted(aw_dates, d, side="right")])

    cash_units = capital                      # 现金以「全天候份额」记
    hold: dict[str, dict[str, float]] = {}   # sym -> {shares, cost}
    nav_rows: list[dict] = []
    tp_rows: list[dict] = []
    sold_month: set[str] = set()
    just_sold: set[str] = set()              # 本期刚止盈的（补仓时跳过）
    pending: list[float] = []                # 待补仓金额
    cur_month = None
    n_refill = 0

    periods = plan[(plan.exit_dt >= start) & (plan.entry_dt <= end)]

    def px_open(sym, d):
        t = daily.get(sym)
        if t is None or d not in t.index:
            return None
        v = float(t.at[d, "open"])
        return v if np.isfinite(v) and v > 0 else None

    for p in periods.itertuples():
        e, x = int(p.entry_dt), int(p.exit_dt)
        expo = exposure.get(int(p.signal_dt), 1.0)
        sel = by_dt.get(int(p.signal_dt))
        target = [] if sel is None else list(sel.symbol)[:topk]
        cand = (rank or {}).get(int(p.signal_dt), [])
        m = int(str(e)[4:6])
        if m != cur_month:
            cur_month, sold_month, just_sold, pending = m, set(), set(), []
        if not rebuy:
            target = [s for s in target if s not in sold_month]

        # ---- 调仓日：按开盘价成交 ----
        for sym in list(hold):
            if sym in target:
                continue
            pr = px_open(sym, e)
            if pr is None or pr <= 0:
                continue
            cash_units += hold[sym]["shares"] * pr * (1 - sell_c) / aw_level(e)
            hold.pop(sym)

        nav_now = cash_units * aw_level(e) + sum(
            h["shares"] * (px_open(s, e) or 0.0) for s, h in hold.items()
        )
        if target and expo > 0:
            budget = nav_now * expo / len(target)
            for sym in target:
                pr = px_open(sym, e)
                if pr is None:
                    continue
                want = int(budget // (pr * LOT)) * LOT
                cur = int(hold.get(sym, {}).get("shares", 0))
                diff = want - cur
                if diff > 0:
                    unit = pr * LOT * (1 + buy_c)
                    diff = min(diff, int(cash_units * aw_level(e) // unit) * LOT)
                    if diff <= 0:
                        continue
                    cash_units -= diff * pr * (1 + buy_c) / aw_level(e)
                    hold[sym] = {"shares": cur + diff, "cost": pr}
                elif diff < 0:
                    cash_units += -diff * pr * (1 - sell_c) / aw_level(e)
                    keep = cur + diff
                    if keep <= 0:
                        hold.pop(sym, None)
                    else:
                        hold[sym]["shares"] = keep

        # ---- 持有期 ----
        days = sorted(set().union(*[set(daily[s].index) for s in hold]) if hold else set())
        days = [d for d in days if e <= d < x]

        for d in days:
            # 0) 补仓：用昨天止盈的钱，按今日开盘买「下一名」
            if refill and pending and d != e:
                for amt in pending:
                    for sym in cand:
                        if sym in hold or sym in just_sold:
                            continue
                        pr = px_open(sym, d)
                        if pr is None:
                            continue
                        lots = int(amt // (pr * LOT * (1 + buy_c)))
                        if lots <= 0:
                            continue
                        sh = lots * LOT
                        cost = sh * pr * (1 + buy_c)
                        if cost > cash_units * aw_level(d):
                            continue
                        cash_units -= cost / aw_level(d)
                        hold[sym] = {"shares": sh, "cost": pr}
                        n_refill += 1
                        break
                pending = []

            # 1) 触价单
            if tp is not None:
                for sym in list(hold):
                    t = daily.get(sym)
                    if t is None or d not in t.index:
                        continue
                    hi = float(t.at[d, "high"])
                    op = float(t.at[d, "open"])
                    if not np.isfinite(hi) or d == e:
                        continue
                    trigger = hold[sym]["cost"] * (1 + tp)
                    if hi < trigger:
                        continue
                    fill = max(op, trigger) if np.isfinite(op) else trigger
                    proceeds = hold[sym]["shares"] * fill * (1 - sell_c)
                    cash_units += proceeds / aw_level(d)
                    tp_rows.append({
                        "sym": sym, "sell_dt": d, "exit_dt": x,
                        "cost": hold[sym]["cost"], "trigger": trigger, "fill": fill,
                        "ret": fill / hold[sym]["cost"] - 1,
                    })
                    hold.pop(sym)
                    just_sold.add(sym)
                    if not rebuy:
                        sold_month.add(sym)
                    if refill:
                        pending.append(proceeds)

            # 2) 日终估值
            val = cash_units * aw_level(d)
            for sym, h in hold.items():
                t = daily.get(sym)
                pr = None
                if t is not None and d in t.index:
                    pr = float(t.at[d, "close"])
                    if not np.isfinite(pr) or pr <= 0:
                        pr = None
                val += h["shares"] * (pr if pr is not None else h["cost"])
            nav_rows.append(
                {"dt": d, "nav": val, "cash": cash_units * aw_level(d), "n_hold": len(hold)}
            )

    nav_df = pd.DataFrame(nav_rows)
    nav_df.attrs["n_refill"] = n_refill
    return nav_df, pd.DataFrame(tp_rows)


# ---------------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------------
def metrics(nav: pd.DataFrame, label: str) -> dict:
    if nav.empty:
        return {"label": label}
    s = nav.set_index("dt").nav
    s = s[~s.index.duplicated(keep="last")].sort_index()
    r = s.pct_change().dropna()
    years = len(r) / 243.0
    total = s.iloc[-1] / s.iloc[0] - 1
    ann = (1 + total) ** (1 / years) - 1 if years > 0 else np.nan
    dd = (s / s.cummax() - 1).min()
    vol = r.std() * np.sqrt(243)
    sharpe = ann / vol if vol > 0 else np.nan
    return {
        "label": label, "years": round(years, 2),
        "annual": round(ann * 100, 2), "mdd": round(dd * 100, 2),
        "sharpe": round(sharpe, 2),
        "calmar": round(ann / abs(dd), 2) if dd < 0 else np.nan,
        "final": round(s.iloc[-1] / s.iloc[0], 2),
        "cash_avg": round((nav.cash / nav.nav).mean() * 100, 1),
        "cash_p90": round((nav.cash / nav.nav).quantile(0.90) * 100, 1),
    }


def fmt(rows: list[dict]) -> str:
    head = f"{'配置':<28}{'年化':>8}{'回撤':>9}{'夏普':>7}{'Calmar':>8}{'末值':>7}"
    out = [head, "-" * 68]
    for r in rows:
        out.append(
            f"{r.get('label',''):<28}{r.get('annual',0):>7.2f}%{r.get('mdd',0):>8.2f}%"
            f"{r.get('sharpe',0):>7.2f}{r.get('calmar',0):>8.2f}{r.get('final',0):>7.2f}"
        )
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="止盈（触价单）研究")
    ap.add_argument("--build", action="store_true", help="强制重建面板缓存")
    ap.add_argument("--tp", type=float, default=None, help="止盈阈值，如 0.20")
    ap.add_argument("--no-tp", action="store_true", help="基线（不止盈）")
    ap.add_argument("--start", default="20170101")
    ap.add_argument("--end", default="20261030")
    ap.add_argument("--rebuy", action="store_true", help="止盈后当月末允许买回（默认不买回）")
    ap.add_argument("--cost", type=float, default=None,
                    help="统一的单边费率（如 0.00135）；不给则按佣金/过户费/印花税/滑点分项算")
    ap.add_argument("--refill", action="store_true",
                    help="止盈卖出后次日开盘补买「下一名」候选，不再干等月末")
    ap.add_argument("--split", action="store_true", help="A：样本内 / 样本外分别比较")
    ap.add_argument("--cash", action="store_true", help="B：现金拖累与触发明细")
    ap.add_argument(
        "--breadth", type=float, default=0.0,
        help="广度阈值（%%）：信号日全 A 股收盘>MA20 占比低于该值时压低仓位，0=关闭",
    )
    ap.add_argument("--breadth-e", type=float, default=0.3,
                    help="广度低于阈值时的仓位上限（默认 0.3）")
    ap.add_argument("--breadth-ma", type=int, default=20, help="广度均线天数（默认 20）")
    ap.add_argument(
        "--aw-file", type=Path, default=None,
        help="全天候日线（dt,symbol,close 或 dt,aw_ret）；给了就把闲钱按全候记账",
    )
    ap.add_argument("--aw-weights", default="511220.SH:0.8,512890.SH:0.2",
                    help="全候权重（默认 城投债 80%% + 红利低波 20%%）")
    ap.add_argument("--json", type=Path, default=None)
    args = ap.parse_args()

    bl = load_bl()
    panel = ensure_cache(bl, args.build)
    con = bl.connect(DATA_DIR)
    plan = bl.build_plan(con)
    pool = build_pool(bl, panel)
    picks = build_picks(pool)
    rank = build_rank(pool)
    exposure = build_exposure(bl, con, plan)
    if args.breadth > 0:
        breadth = build_breadth(con, args.breadth_ma)
        exposure = {
            d: (min(e, args.breadth_e) if breadth.get(int(d), 100.0) < args.breadth else e)
            for d, e in exposure.items()
        }
        n_hit = sum(1 for d in exposure if breadth.get(int(d), 100.0) < args.breadth)
        print(f"广度择时：<{args.breadth:g}% → 仓位 ≤ {args.breadth_e:g}（触发 {n_hit} 期）")
    aw = (
        load_aw(args.aw_file, parse_weights(args.aw_weights))
        if args.aw_file
        else None
    )
    if aw:
        print(f"全天候记账：{args.aw_weights}，{len(aw)} 个交易日收益")

    syms = sorted(picks.symbol.unique())
    print(f"候选股 {len(syms)} 只，调仓期 {len(plan)} 个，缓存 {CACHE}")

    start, end = int(args.start.replace("-", "")), int(args.end.replace("-", ""))
    daily = build_daily(con, syms, int(plan.entry_dt.min()), int(plan.exit_dt.max()))

    def run(tp, label, s=start, e=end, rebuy=args.rebuy):
        nav, tps = simulate(plan, picks, daily, exposure, tp=tp,
                            start=s, end=e, rebuy=rebuy,
                            rank=rank, refill=args.refill, cost=args.cost, aw=aw)
        m = metrics(nav, label)
        m["tp_count"] = int(len(tps))
        m["n_refill"] = int(nav.attrs.get("n_refill", 0))
        if len(tps):
            # 「卖早了还是卖对了」：拿止盈成交价 对 月末开盘价
            tail = []
            for r in tps.itertuples():
                t = daily.get(r.sym)
                if t is None or r.exit_dt not in t.index:
                    continue
                px = float(t.at[r.exit_dt, "open"])
                if np.isfinite(px) and px > 0:
                    tail.append(px / r.fill - 1)
            if tail:
                a = np.array(tail)
                m["sell_good"] = round(float((a <= 0).mean() * 100), 1)   # 卖掉之后没涨 = 卖对了
                m["gave_up_med"] = round(float(np.median(a) * 100), 2)   # 中位数放弃的涨幅
                m["gave_up_mean"] = round(float(a.mean() * 100), 2)      # 均值（被妖股拉高）
        return m, nav, tps

    rows = []

    if args.split:
        for s, e, tag in [(20170101, 20211231, "样本内 2017-2021"),
                          (20220101, 20261030, "样本外 2022-2026"),
                          (20170101, 20261030, "全段 2017-2026")]:
            m0, _, _ = run(None, f"{tag} · 不止盈", s, e)
            m1, _, _ = run(0.20, f"{tag} · +20% 止盈", s, e)
            rows += [m0, m1]
            d = (m1.get("annual") or 0) - (m0.get("annual") or 0)
            print(f"\n{tag}：止盈提升 {d:+.2f}pp ｜ Calmar "
                  f"{m0.get('calmar')} → {m1.get('calmar')}")
    else:
        if not args.no_tp:
            rows.append(run(args.tp if args.tp else 0.20,
                            f"+{int((args.tp or 0.20)*100)}% 止盈")[0])
        if args.no_tp or args.tp is None:
            rows.append(run(None, "不止盈（基线）")[0])
        if args.tp is not None and not args.no_tp:
            rows.append(run(None, "不止盈（基线）")[0])

    print()
    print(fmt(rows))

    if args.cash:
        print("\n=== 现金拖累 & 止盈明细 ===")
        for tp, lab in [(None, "不止盈"), (0.20, "+20% 止盈")]:
            m, nav, tps = run(tp, lab)
            print(f"\n{lab}")
            print(f"  平均现金占比 {m['cash_avg']}%   90分位 {m['cash_p90']}%")
            if len(tps):
                print(f"  触发 {len(tps)} 笔")
                print(f"  卖对了（卖后到月末不涨）{m.get('sell_good')}%")
                print(f"  卖后到月末：中位数 {m.get('gave_up_med')}% ｜ 均值 {m.get('gave_up_mean')}%")
                by_month = tps.groupby(tps.sell_dt // 100).size()
                print(f"  月均触发 {by_month.mean():.1f} 笔，最多 {by_month.max()} 笔")
            if m.get("n_refill"):
                print(f"  补仓 {m['n_refill']} 笔（止盈后次日开盘买下一名）")

    if args.json:
        args.json.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nJSON → {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
