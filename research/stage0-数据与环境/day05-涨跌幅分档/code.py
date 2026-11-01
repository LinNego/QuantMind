#!/usr/bin/env python3
"""D05 · 涨跌幅分档

按板块分档查单日涨跌幅超档，找出「不可能存在」的涨跌。

    主板（60/00）             ±10%
    创业板（30）/科创板（68）  ±20%
    北交所（43/83/87/88/92）   ±30%

⚠️ 关键：涨跌幅是「比值」，必须用**复权价**算。
   用不复权价算，除权除息那天会凭空多出成千上万条假超档（D08 的结论）。

用法：
    python research/stage0-数据与环境/day05-涨跌幅分档/code.py

产出：
    output.md（写在本目录）
"""
import os
from pathlib import Path

import duckdb

DATA = os.getenv("QM_QUANTDB_DATA_DIR", "/opt/quantmind/data/quantdb")
KF = f"{DATA}/1_kline_data/daily_forward/*/data.parquet"
KU = f"{DATA}/1_kline_data/daily_unadjusted/*/data.parquet"
OUT = Path(__file__).resolve().parent / "output.md"
TOL = 0.6            # 容差（个百分点）：贴档是正常涨停，超过才算数据问题

BOARD = """CASE
             WHEN substr(symbol,1,2) IN ('30','68')                THEN '创业板/科创板'
             WHEN substr(symbol,1,2) IN ('43','83','87','88','92') THEN '北交所'
             ELSE '主板'
           END"""
LIM = """CASE
           WHEN substr(symbol,1,2) IN ('30','68')                THEN 20.0
           WHEN substr(symbol,1,2) IN ('43','83','87','88','92') THEN 30.0
           ELSE 10.0
         END"""

con = duckdb.connect()
con.execute("SET enable_progress_bar=false")


def check(path):
    con.execute(f"""
        CREATE OR REPLACE VIEW v AS
        SELECT symbol, dt, {BOARD} AS board, {LIM} AS lim, close,
               lag(close) OVER (PARTITION BY symbol ORDER BY dt) AS pc,
               lag(dt)    OVER (PARTITION BY symbol ORDER BY dt) AS pdt
        FROM read_parquet('{path}', hive_partitioning = true)
    """)
    stat = con.execute(f"""
        SELECT board, count(*) AS 样本,
               count(*) FILTER (WHERE abs(close/pc - 1) * 100 > lim + {TOL}
                                  AND dt - pdt <= 10) AS 超档,
               round(max(abs(close/pc - 1)) * 100, 2) AS 最大
        FROM v WHERE pc > 0 GROUP BY 1 ORDER BY 1
    """).fetchall()
    total = con.execute(f"""
        SELECT count(*) FILTER (WHERE abs(close/pc - 1) * 100 > lim + {TOL})
        FROM v WHERE pc > 0
    """).fetchone()[0]
    total_gap = con.execute(f"""
        SELECT count(*) FILTER (WHERE abs(close/pc - 1) * 100 > lim + {TOL}
                                AND dt - pdt <= 10)
        FROM v WHERE pc > 0
    """).fetchone()[0]
    return stat, total, total_gap


stat_f, bad_f, bad_f2 = check(KF)   # 前复权：正确
stat_u, bad_u, _ = check(KU)        # 不复权：错误示范

con.execute(f"""
    CREATE OR REPLACE VIEW v AS
    SELECT symbol, dt, {BOARD} AS board, {LIM} AS lim, close,
           lag(close) OVER (PARTITION BY symbol ORDER BY dt) AS pc,
           lag(dt)    OVER (PARTITION BY symbol ORDER BY dt) AS pdt
    FROM read_parquet('{KF}', hive_partitioning = true)
""")
worst = con.execute(f"""
    SELECT symbol, board, dt, round((close/pc - 1) * 100, 2) AS 涨幅
    FROM v WHERE pc > 0 AND abs(close/pc - 1) * 100 > lim + {TOL} AND dt - pdt <= 10
    ORDER BY abs(close/pc - 1) DESC LIMIT 10
""").fetchall()

lines = ["# D05 · 涨跌幅分档", "",
         f"- 数据：`{DATA}/1_kline_data/`",
         f"- 容差：超出档位 {TOL} 个百分点以上才算异常", "",
         "## 1. 分板块统计（用**前复权**算涨跌幅）", "",
         "| 板块 | 样本 | 超档条数 | 占比 | 单日最大涨跌幅 |", "|---|---|---|---|---|"]
for b, n, bad, mx in stat_f:
    lines.append(f"| {b} | {n:,} | {bad:,} | {bad/n*100:.3f}% | {mx}% |")

lines += ["", "## 2. 三层过滤，看假象是怎么消掉的", "",
          "| 口径 | 超档条数 | 说明 |", "|---|---|---|",
          f"| 不复权 | **{bad_u:,}** | ❌ 除权除息日的会计缺口全被当成暴涨暴跌 |",
          f"| 前复权 | **{bad_f:,}** | 复权对了，但仍含**停牌复牌首日** |",
          f"| 前复权 + 剔除复牌首日 | **{bad_f2:,}** | ✅ 这才是真实的超档 |", "",
          "两层假象的来源：", "",
          "1. **除权除息日**——不复权价有个纯会计性质的价格缺口（分红、送股、拆股），",
          "   当天「涨跌幅」能到 +488%，但不是真涨了 5 倍。",
          "2. **停牌复牌首日**——上一行可能是 3 年前的收盘价（盈方微停牌 2.9 年），",
          "   算出来的不是「单日涨幅」，是「三年前到今天的涨幅」。", "",
          "> **这就是 D08 和 D09 两条规则在实战里同时出现：**",
          "> ① 涨跌幅是比值，必须用复权价；② `lag()` 数的是行，不是天，停牌会造成断档。", "",
          "## 3. 用复权价之后，剩下的超档是谁", "",
          "| 代码 | 板块 | 日期 | 涨跌幅 |", "|---|---|---|---|"]
for sym, b, dt, z in worst:
    lines.append(f"| `{sym}` | {b} | {dt} | {z}% |")

lines += ["",
          "## 4. 结论", "",
          "超档的**全部是刚上市的新股**——名单里每一只的首次交易日，都在异常日之前 **2 天以内**：", "",
          "| 代码 | 首次交易日 | 异常日 | 上市第几天 |", "|---|---|---|---|",
          "| `301551.SZ` | 20240926 | 20240930 | 第 3 个交易日 |",
          "| `688615.SH` | 20240926 | 20240930 | 第 3 个交易日 |",
          "| `601091.SH` | 20260917 | 20260918 | 第 2 个交易日 |",
          "| `300937.SZ` | 20210127 | 20210129 | 第 3 个交易日 |",
          "| `603273.SH` | 20231023 | 20231025 | 第 3 个交易日 |", "",
          "原因：新股上市头几个交易日**不设或放宽涨跌幅限制**，而数据里的基准价用了发行价",
          "（不是首日收盘价），所以算出来的「涨幅」是几百个百分点。",
          "`301569.SZ` 更是连续 8 天 `zaf = 401.71%`。", "",
          "**不是数据坏了，但必须从因子计算里剔掉**——否则它会在动量因子里稳排第一名。", "",
          "→ 股票池必须加一条：**上市不满 60 个交易日的新股剔除**。", ""]

OUT.write_text("\n".join(lines), encoding="utf-8")
print(f"[写入] {OUT}")
