#!/usr/bin/env python3
"""用真实退市数据校准「幸存者偏差」压力测试的参数。

为什么需要这个脚本
------------------
`research_lowprice_drawdown.py --mode stress` 里的两个参数一直是**拍的**：

    p_hold  持仓标的的年退市率（默认 2%~5%）
    loss    单次退市当月的损失（默认 -60%）

而 QuantDB 里**没有一只退市股**，所以这两个数无法从主数据估出来。
`fetch_delisted_universe.py` 拉到的 `data/delisted/daily.parquet` 正好补上这块，
本脚本把它换算成压力测试能直接用的参数，并给出组合层面的拖累区间。

产出（全部打到 stdout，不写文件）
-------------------------------
1. 退市前 20/60/120/250 个交易日的收益分布 —— 替换 `loss`
2. 退市前逐月收益 —— 判断损失是「一次性砸盘」还是「长期阴跌」
3. 各年退市数量 vs 当年股票池 —— 替换 `p_hold`
4. 组合拖累区间 —— 回答「20.7% 该按多少规划」

用法
----
    python scripts/analyze_delisted_impact.py
    python scripts/analyze_delisted_impact.py --data-dir data/delisted --topk 15
"""

from __future__ import annotations

import argparse
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

DELISTED_DIR = Path("data/delisted")
QUANTDB_DIR = Path("/opt/quantmind/data/quantdb")


def load_delisted(path: Path) -> pd.DataFrame:
    """读退市日线，返回长表（symbol / dt / close_hfq / close_raw）。"""
    df = pd.read_parquet(path)
    need = {"symbol", "dt", "close_hfq", "close_raw"}
    missing = need - set(df.columns)
    if missing:
        raise SystemExit(
            f"{path} 缺列 {sorted(missing)}，先跑 fetch_delisted_universe.py"
        )
    for c in ("close_hfq", "close_raw"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df[df.close_hfq > 0].sort_values(["symbol", "dt"]).reset_index(drop=True)
    return df


def horizon_stats(df: pd.DataFrame, windows=(20, 60, 120, 250)) -> pd.DataFrame:
    """每只退市股在退市前 N 个交易日的累计收益分布。"""
    rows = []
    for sym, g in df.groupby("symbol"):
        g = g.reset_index(drop=True)
        last = g.close_hfq.iloc[-1]
        rec = {"symbol": sym, "last_px": g.close_raw.iloc[-1], "n_days": len(g)}
        for w in windows:
            rec[f"ret_{w}"] = (
                last / g.close_hfq.iloc[-w - 1] - 1 if len(g) > w else np.nan
            )
        # 退市前 3 个月（约 60 个交易日）的累计：压力测试真正该用的损失
        rec["ret_q"] = last / g.close_hfq.iloc[-61] - 1 if len(g) > 61 else np.nan
        rows.append(rec)
    out = pd.DataFrame(rows)
    stat = []
    for w in list(windows) + ["q"]:
        col = f"ret_{w}"
        s = out[col].dropna()
        stat.append(
            {
                "窗口": "退市前 60 日(≈3月)" if w == "q" else f"退市前 {w} 日",
                "样本": len(s),
                "中位": s.median() * 100,
                "均值": s.mean() * 100,
                "跌超50%": (s < -0.5).mean() * 100,
                "跌超80%": (s < -0.8).mean() * 100,
            }
        )
    return out, pd.DataFrame(stat)


def monthly_path(df: pd.DataFrame, months: int = 12) -> pd.DataFrame:
    """退市前逐月收益（判断是单月砸盘还是长期阴跌）。"""
    rows = []
    for sym, g in df.groupby("symbol"):
        g = g.reset_index(drop=True)
        g["m"] = pd.to_datetime(g.dt.astype(str)).dt.to_period("M")
        m = g.groupby("m").close_hfq.last()
        r = m.pct_change()
        tail = r.dropna().tail(months).reset_index(drop=True)
        for i, v in enumerate(tail[::-1].values):  # i=0 是退市前最后一个月
            rows.append({"symbol": sym, "months_before": i + 1, "ret": v})
    x = pd.DataFrame(rows)
    agg = x.groupby("months_before").ret.agg(
        样本="size", 中位=lambda s: s.median() * 100, 均值=lambda s: s.mean() * 100
    )
    agg["跌超20%占比"] = x.groupby("months_before").ret.apply(
        lambda s: (s < -0.2).mean() * 100
    )
    return agg.reset_index().sort_values("months_before")


def delist_by_year(lst: pd.DataFrame, con) -> pd.DataFrame:
    """各年退市数量 + 当年股票池规模 → 年退市率。"""
    lst = lst.copy()
    lst["y"] = pd.to_datetime(lst.delist_date).dt.year
    cnt = lst.groupby("y").size().rename("退市数")
    pool = con.execute(
        f"""SELECT dt // 10000 AS y, count(DISTINCT symbol) AS pool
            FROM read_parquet('{QUANTDB_DIR}/6_ml_datasets/features_daily/**/*.parquet')
            WHERE dt >= 20160101 GROUP BY y ORDER BY y"""
    ).df()
    # 用每年最后一个交易日的池子规模做分母
    pool = pool.groupby("y").pool.max()
    out = pd.concat([cnt, pool], axis=1).dropna()
    out["年退市率%"] = out.退市数 / out.pool * 100
    return out.reset_index().rename(columns={"index": "年份"})


def pool_size_by_year(con, quantdb: Path) -> float:
    """主板「低价小市值」候选池的日均规模（1.5–10 元、10–50 亿、非 ST、额 ≥2000 万）。

    这是 p_stock 的分母——不是全市场 5,000 只，而是真正符合选股条件的几百只。
    """
    df = con.execute(
        f"""WITH f AS (
              SELECT symbol, dt, total_mv, amount_ma_5,
                     TRY_CAST(is_st AS DOUBLE) AS is_st
              FROM read_parquet('{quantdb}/6_ml_datasets/features_daily/**/*.parquet')
              WHERE dt >= 20160101
            ),
            u AS (
              SELECT symbol, dt, close AS px
              FROM read_parquet('{quantdb}/1_kline_data/daily_unadjusted/**/*.parquet')
              WHERE dt >= 20160101
            )
            SELECT f.dt, count(*) AS n
            FROM f JOIN u USING(symbol, dt)
            WHERE f.is_st = 0 AND u.px BETWEEN 1.5 AND 10
              AND f.total_mv BETWEEN 1e9 AND 5e9 AND f.amount_ma_5 >= 2000
              AND substr(f.symbol, 1, 3) IN
                  ('600','601','603','605','000','001','002','003')
            GROUP BY f.dt"""
    ).df()
    return float(df.n.median())


def pool_eligible_share(daily: pd.DataFrame, days: int = 250) -> float:
    """退市前 ``days`` 个交易日，股价落在 1.5–10 元的比例（近似「会不会进池子」）。"""
    vals = []
    for _, g in daily.groupby("symbol"):
        g = g.reset_index(drop=True)
        if len(g) > days:
            vals.append(g.close_raw.iloc[-days - 1])
    s = pd.Series(vals)
    return float(s.between(1.5, 10).mean())


def drag_estimate(p_stock: float, loss: float, years: int) -> dict:
    """组合层面的年化拖累。

    ``p_stock`` 是「一只持仓标的在某年内退市」的概率。等权 topk 下：
        单次冲击 = |loss| / topk
        每年期望事件数 = topk × p_stock
        年化拖累 = p_stock × |loss|      ← 与 topk 无关（N 在分子分母抵消）
    """
    annual = p_stock * abs(loss)
    return {
        "单次冲击": abs(loss) * 100,
        "年化拖累": annual * 100,
        "累计拖累": (1 - (1 - annual) ** years) * 100,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="用真实退市数据校准压力测试参数")
    ap.add_argument("--data-dir", type=Path, default=DELISTED_DIR)
    ap.add_argument("--quantdb", type=Path, default=QUANTDB_DIR)
    ap.add_argument("--topk", type=int, default=15)
    args = ap.parse_args()

    daily = load_delisted(args.data_dir / "daily.parquet")
    lst = pd.read_parquet(args.data_dir / "delist_list.parquet")
    print(
        f"退市名单 {len(lst)} 只；有日线 {daily.symbol.nunique()} 只 / {len(daily):,} 行"
    )
    if daily.empty:
        print("没有日线数据，先跑 fetch_delisted_universe.py")
        return 1

    raw, stat = horizon_stats(daily)
    print("\n=== 1. 退市前收益分布（用后复权价）===")
    print(stat.round(1).to_string(index=False))
    print(
        f"\n退市前最后收盘价：中位 {raw.last_px.median():.2f} 元，"
        f"最低 {raw.last_px.min():.2f} 元（面值退市是 1 元以下）"
    )

    mp = monthly_path(daily)
    print("\n=== 2. 退市前逐月收益（1 = 退市前最后一个月）===")
    print(mp.round(1).to_string(index=False))
    m1 = mp.loc[mp.months_before == 1, "中位"].iloc[0]
    m2 = mp.loc[mp.months_before == 2, "中位"].iloc[0]
    worst = mp.nsmallest(1, "中位").iloc[0]
    print(
        f"\n→ 最后一个月中位 {m1:.1f}%（多数已停牌/退市整理，价格不动），"
        f"**真正的杀跌在退市前第 2~3 个月**：第 2 个月中位 {m2:.1f}%，"
        f"最惨的是第 {int(worst.months_before)} 个月（{worst.中位:.1f}%）。"
    )
    print(
        "   压力测试的 `loss` 建议用「退市前 60 日累计」的中位，"
        "而不是单月——因为策略会在下跌过程中一直持有（越跌市值越小，反而越符合条件）。"
    )

    con = duckdb.connect()
    by_year = delist_by_year(lst, con)
    print("\n=== 3. 各年退市数量与退市率 ===")
    print(by_year.round(2).to_string(index=False))

    # 落到我们持仓里的退市概率：退市股 × 进池比例 ÷ 池子规模
    avg_delist = by_year.退市数.mean()
    share = pool_eligible_share(daily)
    pool_n = pool_size_by_year(con, args.quantdb)
    p_stock = avg_delist * share / pool_n
    print("\n=== 4. 落到持仓里的退市概率 ===")
    print(
        f"  年均退市 {avg_delist:.1f} 只 × 价格上会进池的比例 {share:.0%} "
        f"÷ 候选池规模 {pool_n:.0f} 只 = **{p_stock * 100:.2f}%/年**"
    )

    print("\n=== 5. 组合拖累（单次损失取退市前 60 日累计中位）===")
    loss_q = raw.ret_q.median()
    years = len(by_year)
    d = drag_estimate(p_stock, loss_q, years)
    print(
        f"  单次冲击 {d['单次冲击']:.1f}%（退市前 60 日累计中位）"
        f" × 年退市概率 {p_stock * 100:.2f}% → **年化拖累 {d['年化拖累']:.2f}pp**"
        f"（{years} 年累计 {d['累计拖累']:.1f}%）"
    )
    print(
        "\n注：等权 topk 下年化拖累 ≈ p_stock × |loss|，与 topk 无关——"
        "N 在「单次冲击 ÷ N」和「事件数 × N」里正好抵消。"
        "所以「多买几只对冲退市」在期望上无效，只影响尾部宽度。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
