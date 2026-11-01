#!/usr/bin/env python3
"""D04 · 脏数据

找出「不可能存在」的行：收盘价 ≤ 0、成交量 ≤ 0、最高价 < 最低价。

用法：
    python research/stage0-数据与环境/day04-脏数据/code.py

产出：
    output.md（写在本目录）
"""
import os
from pathlib import Path

import duckdb

DATA = os.getenv("QM_QUANTDB_DATA_DIR", "/opt/quantmind/data/quantdb")
KU = f"{DATA}/1_kline_data/daily_unadjusted/*/data.parquet"
OUT = Path(__file__).resolve().parent / "output.md"

con = duckdb.connect()
con.execute("SET enable_progress_bar=false")
con.execute(f"""
    CREATE VIEW ku AS
    SELECT symbol, dt, open, high, low, close, volume, amount
    FROM read_parquet('{KU}', hive_partitioning = true)
""")

n = con.execute("""
    SELECT count(*) FILTER (WHERE close <= 0)                    AS 收盘非正,
           count(*) FILTER (WHERE volume <= 0)                   AS 量非正,
           count(*) FILTER (WHERE high < low)                    AS 高低倒挂,
           count(*) FILTER (WHERE amount <= 0)                   AS 额非正,
           count(*) FILTER (WHERE open <= 0 OR high <= 0 OR low <= 0) AS 其它非正
    FROM ku
""").fetchone()

by_year = con.execute("""
    SELECT dt // 10000 AS yr, count(*) AS n, count(DISTINCT symbol) AS s
    FROM ku WHERE volume <= 0 GROUP BY 1 ORDER BY 1
""").fetchall()

lines = ["# D04 · 脏数据", "",
         f"- 数据：`{DATA}/1_kline_data/daily_unadjusted`", "",
         "## 1. 全表检查", "",
         "| 检查 | 条数 | 解释 |", "|---|---|---|",
         f"| 收盘价 ≤ 0 | **{n[0]:,}** | — |",
         f"| 成交量 ≤ 0 | **{n[1]:,}** | 临时停牌：有行、有价格（停牌前收盘价），但没有成交 |",
         f"| 最高价 < 最低价 | **{n[2]:,}** | — |",
         f"| 成交额 ≤ 0 | **{n[3]:,}** | 与成交量同源 |",
         f"| 开/高/低 非正 | **{n[4]:,}** | — |",
         "", "## 2. 成交量 ≤ 0 的年份分布", "",
         "| 年份 | 行数 | 涉及股票数 |", "|---|---|---|"]
for yr, c, s in by_year:
    lines.append(f"| {yr} | {c:,} | {s:,} |")

lines += ["",
          "## 3. 结论", "",
          "**这 254 行必须从股票池里滤掉。** 按收盘价「成交」一笔根本不存在的交易，是最典型的假收益。",
          "",
          "⚠️ 但这不是停牌的全部。**停牌有两种形态：**", "",
          "| 停牌时长 | 数据长什么样 | 怎么滤 |", "|---|---|---|",
          "| 短期 | 有行，但 `volume = 0` | `volume > 0` |",
          "| 长期（一年以上） | **整行不存在**（深深房A 缺 4 年） | 「近 N 天有数据」 |",
          "",
          "**两条缺一不可。** 只写 `volume > 0` 滤不掉长期停牌。详见 D09。", ""]

OUT.write_text("\n".join(lines), encoding="utf-8")
print(f"[写入] {OUT}")
