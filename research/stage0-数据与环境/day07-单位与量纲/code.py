#!/usr/bin/env python3
"""D07 · 单位与量纲

用一行数字证明「我的单位是对的」：close * volume / amount 应 ≈ 10000。

用法：
    python research/stage0-数据与环境/day07-单位与量纲/code.py

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

rows = con.execute(f"""
    SELECT dt // 10000 AS yr,
           median(close * volume / amount)                     AS 中位数,
           round(avg(abs(close*volume/amount - 10000) / 10000 * 100), 3) AS 平均偏离百分比,
           count(*) AS 样本
    FROM read_parquet('{KU}', hive_partitioning = true)
    WHERE amount > 0 GROUP BY 1 ORDER BY 1
""").fetchall()

lines = ["# D07 · 单位与量纲", "",
         f"- 数据：`{DATA}/1_kline_data/daily_unadjusted`", "",
         "## 1. 逐年 close × volume ÷ amount 的中位数", "",
         "| 年份 | 中位数 | 平均偏离 10000 的幅度 | 样本 |", "|---|---|---|---|"]
for yr, med, dev, n in rows:
    lines.append(f"| {yr} | {med:,.0f} | {dev}% | {n:,} |")

lines += ["",
          "## 2. 为什么是 10000", "",
          "因为：",
          "",
          "```",
          "volume 单位 = 股",
          "amount 单位 = 万元",
          "",
          "元 × 股 ÷ 万元 = 元 × 股 ÷ (元 / 10000) = 10000",
          "```",
          "",
          "**结论：volume 是股、amount 是万元。**", "",
          "## 3. 这条错了会怎样", "",
          "所有换手率、资金流、成交额类因子会差 **1 万倍**，",
          "而代码**不会报错**——你只会得到一条看起来很正常的曲线。", ""]

OUT.write_text("\n".join(lines), encoding="utf-8")
print(f"[写入] {OUT}")
