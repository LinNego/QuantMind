#!/usr/bin/env python3
"""D02 · 规模与覆盖（阶段 0 第 2 天）

对齐 docs/量化学习/07-阶段0-每日执行计划.md 第 2 天：
  "把第 1 天的脚本改成三个统计：count(DISTINCT symbol)/count(DISTINCT dt)/min(dt),max(dt)"
  "再算「每年最后一个交易日的股票数」"

这里多做一件文档没写但必须做的事：
  **交易日历 vs 落地数据的天数对账**。日历说有多少个交易日，
  数据里实际有多少天，差的那些天是「还没发布」还是「漏了」，不看就会算错。

产出：research/out/D02_coverage.md
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import duckdb

ROOT = Path("/opt/quantmind/data/quantdb")
OUT = Path("/root/QuantMind/research/out")
KU = str(ROOT / "1_kline_data/daily_unadjusted/*/data.parquet")
CAL = str(ROOT / "2_base_sector/trading_calendar/*.parquet")

lines: list[str] = []


def say(s: str = "") -> None:
    print(s)
    lines.append(s)


def main() -> int:
    con = duckdb.connect()
    con.execute("SET enable_progress_bar=false")
    con.execute("SET threads=8")

    say("# D02 规模与覆盖（阶段 0 第 2 天）")
    say(f"\n- 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}")
    say(f"- 数据目录：`{ROOT}`")
    say("- 口径：`daily_unadjusted`（不复权日线）")

    # ---------- 1. 三个数 ----------
    say("\n## 1. 一句话说清这份数据")
    s = con.execute(f"""
        SELECT count(DISTINCT symbol) AS 股票数, count(DISTINCT dt) AS 交易日数,
               min(dt) AS 起, max(dt) AS 止, count(*) AS 总行数
        FROM read_parquet('{KU}', hive_partitioning = true)
    """).df()
    say("\n```")
    say(s.to_string(index=False))
    say("```")
    r = s.iloc[0]
    say(f"\n→ 我的数据是 **{r['股票数']:,} 只股票、{r['交易日数']:,} 个交易日、"
        f"{r['起']} 到 {r['止']}**，共 {r['总行数']:,} 行。")

    # ---------- 2. 每天股票数：按年 min/median/max ----------
    say("\n## 2. 每个交易日的股票数，按年汇总")
    say("\n> 只看年初年末会漏掉「某天突然塌到 800 只」这种事，所以每年都要看 min。")
    y = con.execute(f"""
        WITH d AS (
            SELECT dt, count(DISTINCT symbol) AS n
            FROM read_parquet('{KU}', hive_partitioning = true) GROUP BY dt
        )
        SELECT CAST(dt/10000 AS INT) AS 年份, count(*) AS 交易日,
               min(n) AS 最少, CAST(median(n) AS INT) AS 中位, max(n) AS 最多,
               round(100.0 * (min(n) - median(n)) / median(n), 1) AS 最差偏离百分比
        FROM d GROUP BY 1 ORDER BY 1
    """).df()
    say("\n```")
    say(y.to_string(index=False))
    say("```")

    worst = y.sort_values("最差偏离百分比").head(3) if "最差偏离百分比" in y else y.head(0)
    if len(worst):
        say("\n偏离最大的三年（min 相对中位掉得最狠）：")
        say("\n```")
        say(worst.to_string(index=False))
        say("```")

    # ---------- 3. 逐日抽样 ----------
    say("\n## 3. 逐日抽样（每 500 个交易日取一天）")
    z = con.execute(f"""
        WITH d AS (
            SELECT dt, count(DISTINCT symbol) AS n,
                   row_number() OVER (ORDER BY dt) AS rn
            FROM read_parquet('{KU}', hive_partitioning = true) GROUP BY dt
        )
        SELECT dt AS 交易日, n AS 股票数 FROM d
        WHERE rn % 500 = 1 ORDER BY dt
    """).df()
    say("\n```")
    say(z.to_string(index=False))
    say("```")

    # ---------- 4. 日历对账 ----------
    say("\n## 4. 交易日历 vs 落地数据（对账）")
    c = con.execute(f"""
        WITH cal AS (
            SELECT CAST(TradingDate AS BIGINT) AS dt
            FROM read_parquet('{CAL}') WHERE IsTradingDay = 1
        ), data_dt AS (
            SELECT DISTINCT dt FROM read_parquet('{KU}', hive_partitioning = true)
        )
        SELECT (SELECT count(*) FROM cal) AS 日历交易日,
               (SELECT count(*) FROM data_dt) AS 数据有数据的天,
               (SELECT count(*) FROM cal WHERE dt NOT IN (SELECT dt FROM data_dt)) AS 日历有数据无,
               (SELECT count(*) FROM data_dt WHERE dt NOT IN (SELECT dt FROM cal)) AS 数据有日历无
    """).df()
    say("\n```")
    say(c.to_string(index=False))
    say("```")

    gap = con.execute(f"""
        WITH cal AS (
            SELECT CAST(TradingDate AS BIGINT) AS dt
            FROM read_parquet('{CAL}') WHERE IsTradingDay = 1
        ), data_dt AS (
            SELECT DISTINCT dt FROM read_parquet('{KU}', hive_partitioning = true)
        )
        SELECT dt AS 日历有但数据缺, CAST(dt/10000 AS INT) AS 年份
        FROM cal WHERE dt NOT IN (SELECT dt FROM data_dt) ORDER BY dt
    """).df()
    if len(gap):
        say("\n缺失的交易日（全部列出）：")
        say("\n```")
        say(gap.to_string(index=False))
        say("```")
        say(f"\n→ 共 {len(gap)} 天：日历上有、数据里没有。"
            "这可能是「当天全市场无交易」（极少）或「数据未覆盖」，**必须逐条确认**，"
            "否则你在做时间序列时会默认这些天不存在，日历对齐就会错位。")

    OUT.mkdir(parents=True, exist_ok=True)
    target = OUT / "D02_coverage.md"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n[写入] {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
