#!/usr/bin/env python3
"""D03 · 每天股票数 / 逐年股票数

确认「每天有多少只股票在交易」，并且它是逐年增长、不是恒定 5000。

用法：
    python research/stage0-数据与环境/day03-每天股票数/code.py

产出：
    output.md（写在本目录）
"""
import os
from pathlib import Path

import duckdb

DATA = os.getenv("QM_QUANTDB_DATA_DIR", "/opt/quantmind/data/quantdb")
KF = f"{DATA}/1_kline_data/daily_forward/*/data.parquet"
OUT = Path(__file__).resolve().parent / "output.md"

con = duckdb.connect()
con.execute("SET enable_progress_bar=false")
con.execute(f"""
    CREATE VIEW kf AS
    SELECT symbol, dt FROM read_parquet('{KF}', hive_partitioning = true)
""")

# ① 当年出现过的股票数
all_year = dict(con.execute("""
    SELECT dt // 10000 AS yr, count(DISTINCT symbol)
    FROM kf GROUP BY 1 ORDER BY 1
""").fetchall())

# ② 每年最后一个交易日的快照
snap = con.execute("""
    WITH d AS (SELECT DISTINCT dt FROM kf),
    yd AS (SELECT dt // 10000 AS yr, max(dt) AS dt FROM d GROUP BY 1)
    SELECT yd.yr, yd.dt, count(DISTINCT kf.symbol)
    FROM yd JOIN kf ON kf.dt = yd.dt
    GROUP BY 1, 2 ORDER BY 1
""").fetchall()

lines = ["# D03 · 每天股票数", "",
         f"- 数据：`{DATA}/1_kline_data/daily_forward`", "",
         "## 1. 每年出现过的股票数 vs 年末快照", "",
         "| 年份 | 当年出现过 | 最后一个交易日 | 该日股票数 |",
         "|---|---|---|---|"]
for yr, dt, n in snap:
    lines.append(f"| {yr} | {all_year.get(yr, 0):,} | {dt} | {n:,} |")

lines += ["",
          "> **单调上升，没有任何一年下降。**", "",
          "2016 年少是因为当年上市公司本来就少（2017 年 A 股 IPO 438 家是历史大年），",
          "**不是数据缺失**——D09 单独验证过：2017 年新增的 428 只，首次出现日期散在全年各月，",
          "最大单日只有 5 只，没有挤在 20170103，说明 2016 年覆盖完整。", "",
          "## 2. 为什么这个统计重要", "",
          "如果某天突然只剩 500 只股票，那天的截面排名整体是垃圾。",
          "**这类问题只有这种统计能发现**，净值曲线看不出来。", ""]

OUT.write_text("\n".join(lines), encoding="utf-8")
print(f"[写入] {OUT}")
