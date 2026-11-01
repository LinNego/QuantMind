#!/usr/bin/env python3
"""D08 · 复权三兄弟

把同一天的 前复权 / 不复权 / 后复权 并排放，看清它们差多少、各用在哪。

用法：
    python research/stage0-数据与环境/day08-复权三兄弟/code.py

产出：
    output.md（写在本目录）
"""
import os
from pathlib import Path

import duckdb

DATA = os.getenv("QM_QUANTDB_DATA_DIR", "/opt/quantmind/data/quantdb")
KF = f"{DATA}/1_kline_data/daily_forward/*/data.parquet"
KU = f"{DATA}/1_kline_data/daily_unadjusted/*/data.parquet"
KB = f"{DATA}/1_kline_data/daily_backward/*/data.parquet"
FEAT = f"{DATA}/6_ml_datasets/features_daily/*/data.parquet"
OUT = Path(__file__).resolve().parent / "output.md"
D = 20191231

con = duckdb.connect()
con.execute("SET enable_progress_bar=false")

avg = con.execute(f"""
    SELECT avg(close) FROM read_parquet('{KF}', hive_partitioning=true) WHERE dt={D}
""").fetchone()[0]
avgu = con.execute(f"""
    SELECT avg(close) FROM read_parquet('{KU}', hive_partitioning=true) WHERE dt={D}
""").fetchone()[0]
avgb = con.execute(f"""
    SELECT avg(close) FROM read_parquet('{KB}', hive_partitioning=true) WHERE dt={D}
""").fetchone()[0]

cz = con.execute(f"""
    SELECT symbol, a.close AS 前复权, b.close AS 不复权, c.close AS 后复权
    FROM (SELECT symbol, close FROM read_parquet('{KF}', hive_partitioning=true) WHERE dt={D}) a
    JOIN (SELECT symbol, close FROM read_parquet('{KU}', hive_partitioning=true) WHERE dt={D}) b USING (symbol)
    JOIN (SELECT symbol, close FROM read_parquet('{KB}', hive_partitioning=true) WHERE dt={D}) c USING (symbol)
    WHERE symbol IN ('600519.SH','000001.SZ','601398.SH')
    ORDER BY symbol
""").fetchall()

feat_diff = con.execute(f"""
    SELECT count(*) FILTER (WHERE f.close <> b.close) AS 不同,
           count(*) FILTER (WHERE f.close > 0)        AS 有值
    FROM (SELECT symbol, close FROM read_parquet('{FEAT}', hive_partitioning=true) WHERE dt={D}) f
    JOIN (SELECT symbol, close FROM read_parquet('{KB}', hive_partitioning=true) WHERE dt={D}) b USING (symbol)
""").fetchone()

lines = ["# D08 · 复权三兄弟", "",
         f"- 数据：三套日线 + `features_daily`，取 `{D}` 这一天", "",
         f"## 1. 全市场平均价（{D}）", "",
         "| 口径 | 平均收盘价 |", "|---|---|",
         f"| `daily_forward` 前复权 | **{avg:.2f}** |",
         f"| `daily_unadjusted` 不复权 | **{avgu:.2f}** |",
         f"| `daily_backward` 后复权 | **{avgb:.2f}** |", "",
         "同一天、同一批股票，三个数。**用错不报错，只是结论全错。**", "",
         "## 2. 个股对照", "",
         "| 代码 | 前复权 | 不复权 | 后复权 |", "|---|---|---|---|"]
for sym, a, b, c in cz:
    lines.append(f"| `{sym}` | {a:.2f} | {b:.2f} | {c:.2f} |")

lines += ["",
          "## 3. `features_daily.close` 是哪个口径？", "",
          f"- 与 `daily_backward` 不同的行：**{feat_diff[0]:,}** / {feat_diff[1]:,}",
          "",
          "→ **`features_daily.close` 就是后复权**，不是成交价。", "",
          "## 4. 三个口径各用在哪", "",
          "| 序列 | 锚点 | 谁被改写 | 用在哪 |",
          "|---|---|---|---|",
          "| 前复权 | 以**今天**为基准 | **历史**被改写 | 算收益率、画净值、算因子 |",
          "| 后复权 | 以**上市首日**为基准 | 今天被改写，历史永不变 | 长期存档、结果要可复现 |",
          "| 不复权 | 交易所真实价 | 谁都不改 | **成交价**、市值、成交额、涨跌停 |",
          "",
          "**一句话判据：这个数字是「钱」还是「涨跌幅」？**",
          "是钱 → 不复权；是涨跌幅 → 复权。", "",
          "## 5. 最容易犯的错", "",
          f"拿前复权价当成交价：{avg:.2f} 对 {avgu:.2f}，**少算 {(1-avg/avgu)*100:.0f}% 的钱**。",
          "回测里就变成「我凭空能多买一截股票」，净值凭空变好。", ""]

OUT.write_text("\n".join(lines), encoding="utf-8")
print(f"[写入] {OUT}")
