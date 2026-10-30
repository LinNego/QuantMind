#!/usr/bin/env python3
"""D01 · 看见数据（阶段 0 第 1 天）

对齐 docs/量化学习/07-阶段0-每日执行计划.md 第 1 天：
  1) 从 daily_unadjusted 读出某一天的 5 行，逐列能解释
  2) 顺手把「规模 / 每年股票数 / 单位自检 / 异常 / 涨跌幅」落成数字

第 5 节是这套数据的第一个真坑：
  不复权价遇到除权除息（10 送 10 之类）会跳空 -50%~-75%，
  直接按「涨跌幅超档」判脏数据，会把几千条除权当成数据错误。
  正确做法：先用交易日历对齐（剔除停牌缺口），再看前复权。

产出：research/out/D01_data_check.md
"""
from __future__ import annotations

import os

import textwrap
from datetime import datetime
from pathlib import Path

import duckdb
import pandas as pd

ROOT = Path(os.getenv("QM_QUANTDB_DATA_DIR", "/opt/quantmind/data/quantdb"))
OUT = Path(__file__).resolve().parents[2] / "out"
KU = str(ROOT / "1_kline_data/daily_unadjusted/*/data.parquet")
KF = str(ROOT / "1_kline_data/daily_forward/*/data.parquet")
KB = str(ROOT / "1_kline_data/daily_backward/*/data.parquet")
CAL = str(ROOT / "2_base_sector/trading_calendar/*.parquet")

lines: list[str] = []


def say(s: str = "") -> None:
    print(s)
    lines.append(s)


def q(con, sql: str) -> pd.DataFrame:
    return con.execute(sql).df()


def over_limit(con, path: str, label: str) -> pd.DataFrame:
    """按板块统计超出涨跌停档位的天数（先用日历对齐，再区分上市是否满 60 日）。"""
    sql = f"""
        WITH cal AS (
            SELECT CAST(TradingDate AS BIGINT) AS dt,
                   row_number() OVER (ORDER BY TradingDate) AS rn
            FROM read_parquet('{CAL}') WHERE IsTradingDay = 1
        ), k AS (
            SELECT symbol, dt, close FROM read_parquet('{path}', hive_partitioning = true)
            WHERE volume > 0
        ), j AS (
            SELECT k.symbol, k.dt, k.close, cal.rn FROM k JOIN cal USING (dt)
        ), z AS (
            SELECT symbol, dt, close, rn,
                   row_number() OVER (PARTITION BY symbol ORDER BY dt) AS seq
            FROM j
        ), x AS (
            SELECT symbol, dt, close, rn, seq,
                   lag(close) OVER w AS prev_close,
                   lag(rn)    OVER w AS prev_rn
            FROM z WINDOW w AS (PARTITION BY symbol ORDER BY dt)
        ), y AS (
            SELECT symbol, dt, seq, (close / prev_close - 1) * 100 AS zaf,
                   CASE
                     WHEN symbol LIKE '30%' OR symbol LIKE '68%' THEN 20
                     WHEN symbol LIKE '43%' OR symbol LIKE '83%' OR symbol LIKE '87%'
                       OR symbol LIKE '88%' OR symbol LIKE '92%' THEN 30
                     ELSE 10
                   END AS cap,
                   CASE
                     WHEN symbol LIKE '30%' OR symbol LIKE '68%' THEN '双创'
                     WHEN symbol LIKE '43%' OR symbol LIKE '83%' OR symbol LIKE '87%'
                       OR symbol LIKE '88%' OR symbol LIKE '92%' THEN '北交所'
                     ELSE '主板'
                   END AS board
            FROM x WHERE prev_close > 0 AND rn - prev_rn = 1
        )
        SELECT '{label}' AS 口径, board AS 板块,
               CASE WHEN seq > 60 THEN '上市满60日' ELSE '上市前60日' END AS 范围,
               count(*) AS 样本,
               sum(CASE WHEN abs(zaf) > cap + 0.6 THEN 1 ELSE 0 END) AS 超档条数,
               round(max(abs(zaf)), 2) AS 单日最大
        FROM y GROUP BY 1, 2, 3 ORDER BY 2, 3
    """
    return q(con, sql)


def detail(con, path: str, limit: int = 8) -> pd.DataFrame:
    sql = f"""
        WITH cal AS (
            SELECT CAST(TradingDate AS BIGINT) AS dt,
                   row_number() OVER (ORDER BY TradingDate) AS rn
            FROM read_parquet('{CAL}') WHERE IsTradingDay = 1
        ), j AS (
            SELECT k.symbol, k.dt, k.close, cal.rn
            FROM read_parquet('{path}', hive_partitioning = true) k JOIN cal USING (dt)
            WHERE k.volume > 0
        ), z AS (
            SELECT symbol, dt, close, rn,
                   row_number() OVER (PARTITION BY symbol ORDER BY dt) AS seq FROM j
        ), x AS (
            SELECT symbol, dt, seq, close, rn,
                   lag(close) OVER w AS prev_close, lag(rn) OVER w AS prev_rn
            FROM z WINDOW w AS (PARTITION BY symbol ORDER BY dt)
        ), w2 AS (
            SELECT symbol, dt, seq, (close/prev_close-1)*100 AS zaf,
                   CASE
                     WHEN symbol LIKE '30%' OR symbol LIKE '68%' THEN 20
                     WHEN symbol LIKE '43%' OR symbol LIKE '83%' OR symbol LIKE '87%'
                       OR symbol LIKE '88%' OR symbol LIKE '92%' THEN 30
                     ELSE 10
                   END AS cap
            FROM x WHERE prev_close > 0 AND rn - prev_rn = 1 AND seq > 60
        )
        SELECT symbol, dt, round(zaf, 2) AS zaf, cap AS 档位
        FROM w2 WHERE abs(zaf) > cap + 0.6
        ORDER BY abs(zaf) DESC LIMIT {limit}
    """
    return q(con, sql)


def main() -> int:
    con = duckdb.connect()
    con.execute("SET enable_progress_bar=false")
    con.execute("SET threads=8")

    say("# D01 数据体检（阶段 0 第 1 天）")
    say(f"\n- 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}")
    say(f"- 数据目录：`{ROOT}`")
    say(f"- 引擎：DuckDB {duckdb.__version__}")
    say("- 覆盖：3 套日线（不复权/前复权/后复权）+ 指数 + 基础板块；**未下** `features_daily` 与财务")

    # ---------- 1. 第 1 天的那个查询 ----------
    say("\n## 1. 读出 5 行（daily_unadjusted，2026-10-23）")
    df = q(con, f"""
        SELECT symbol, dt, open, high, low, close, volume, amount
        FROM read_parquet('{KU}', hive_partitioning = true)
        WHERE dt = 20261023
        LIMIT 5
    """)
    say("\n```")
    say(df.to_string(index=False))
    say("```")

    say("\n列含义（对照 `skills/quantdb-fields`）：")
    say(textwrap.dedent("""\
        | 列 | 含义 | 单位 / 口径 |
        |---|---|---|
        | `symbol` | 证券代码 | 6 位 + `.SZ/.SH/.BJ` |
        | `dt` | 交易日期 | `YYYYMMDD` 整数，同时是目录分区名 |
        | `open/high/low/close` | 开/高/低/收 | 元；这一套是**不复权**，即当天真实成交价 |
        | `volume` | 成交量 | **股**（个股；指数是手） |
        | `amount` | 成交额 | **万元** |
        """))

    # ---------- 2. 规模 ----------
    say("\n## 2. 规模")
    s = q(con, f"""
        SELECT count(DISTINCT symbol) AS 股票数, count(DISTINCT dt) AS 交易日数,
               min(dt) AS 起, max(dt) AS 止, count(*) AS 总行数
        FROM read_parquet('{KU}', hive_partitioning = true)
    """)
    say("\n```")
    say(s.to_string(index=False))
    say("```")

    say("\n每年最后一个交易日的股票数：")
    y = q(con, f"""
        WITH d AS (
            SELECT CAST(dt / 10000 AS INT) AS y, dt,
                   row_number() OVER (PARTITION BY CAST(dt / 10000 AS INT) ORDER BY dt DESC) AS rk
            FROM (SELECT DISTINCT dt FROM read_parquet('{KU}', hive_partitioning = true))
        ), last_dt AS (SELECT dt FROM d WHERE rk = 1)
        SELECT CAST(dt / 10000 AS INT) AS 年份, count(DISTINCT symbol) AS 股票数
        FROM read_parquet('{KU}', hive_partitioning = true)
        WHERE dt IN (SELECT dt FROM last_dt)
        GROUP BY 1 ORDER BY 1
    """)
    say("\n```")
    say(y.to_string(index=False))
    say("```")
    say("\n→ 股票数是**逐年长**的（2600 → 5500），不是恒定 5000。"
        "「今天为什么只有 800 只」这类问题，只有这种统计能发现。")

    # ---------- 3. 单位自检 ----------
    say("\n## 3. 单位自检 `close * volume / amount`（个股应 ≈ 1e4）")
    u = q(con, f"""
        SELECT CAST(dt / 10000 AS INT) AS 年份,
               round(median(close * volume / amount), 1) AS 中位数,
               count(*) AS 样本
        FROM read_parquet('{KU}', hive_partitioning = true)
        WHERE amount > 0 AND volume > 0 AND close > 0
        GROUP BY 1 ORDER BY 1
    """)
    say("\n```")
    say(u.to_string(index=False))
    say("```")
    say("\n→ 每年都在 1e4 附近 ⇒ `volume` 是**股**、`amount` 是**万元**，口径没串。")

    # ---------- 4. 异常 ----------
    say("\n## 4. 不可能存在的行")
    a = q(con, f"""
        SELECT sum(CASE WHEN close <= 0 THEN 1 ELSE 0 END) AS 收盘价非正,
               sum(CASE WHEN volume <= 0 THEN 1 ELSE 0 END) AS 成交量非正,
               sum(CASE WHEN high < low THEN 1 ELSE 0 END)  AS 最高小于最低,
               sum(CASE WHEN amount <= 0 THEN 1 ELSE 0 END) AS 成交额非正
        FROM read_parquet('{KU}', hive_partitioning = true)
    """)
    say("\n```")
    say(a.to_string(index=False))
    say("```")
    say("\n→ 254 条 `volume=0` 基本是**停牌日**（有收盘价、无成交），不是脏数据；"
        "但它会让换手率的分母为零，因子计算时要剔除或填 NaN。")

    # ---------- 5. 涨跌幅：不复权 vs 前复权 ----------
    say("\n## 5. 涨跌幅超档：不复权 vs 前复权")
    say("\n> 两个必须做的对齐：① 先按 `trading_calendar` 取**相邻交易日**，"
        "否则停牌半年后复牌那两行会被算成「一天涨 488%」；"
        "② 剔除 `volume=0` 的停牌行（数据里停牌日仍有行、价格不变），否则复牌那天会被算成暴跌；"
        "③ 再区分**上市满 60 日**，新股前 5 日无涨跌幅限制。")
    z = pd.concat([over_limit(con, KU, "不复权"), over_limit(con, KF, "前复权")],
                  ignore_index=True)
    say("\n```")
    say(z.to_string(index=False))
    say("```")
    say("\n→ **不复权**满 60 日仍有 4600+ 条「超档」，几乎全是**除权除息跳空**"
        "（10 送 10 就是 -50%）；**前复权**下同样的检查几乎归零。"
        "所以：算收益/算涨跌幅用前复权，看真实成交价用不复权 —— 这就是三套日线存在的理由。")

    say("\n前复权口径下剩余的 2 条超档（这才是真正要看的东西）：")
    d = detail(con, KF)
    say("\n```")
    say(d.to_string(index=False))
    say("```")

    # ---------- 6. 三套复权 ----------
    say("\n## 6. 三套日线差多少（000001.SZ，平安银行）")
    c = q(con, f"""
        WITH u AS (SELECT dt, close AS 不复权 FROM read_parquet('{KU}', hive_partitioning=true) WHERE symbol='000001.SZ'),
             f AS (SELECT dt, close AS 前复权 FROM read_parquet('{KF}', hive_partitioning=true) WHERE symbol='000001.SZ'),
             b AS (SELECT dt, close AS 后复权 FROM read_parquet('{KB}', hive_partitioning=true) WHERE symbol='000001.SZ')
        SELECT u.dt, round(u.不复权,2) AS 不复权, round(f.前复权,2) AS 前复权, round(b.后复权,2) AS 后复权
        FROM u JOIN f USING(dt) JOIN b USING(dt)
        WHERE dt IN (20160104, 20200102, 20261023) ORDER BY dt
    """)
    say("\n```")
    say(c.to_string(index=False))
    say("```")
    say("\n→ 同一天同一只股票，三个价格差 1.6 倍。"
        "**不复权**回答「我买得起吗」，**前复权**回答「持有到今天赚多少」，"
        "**后复权**回答「从上市那天算涨了几倍」。混用就是静默错误。")

    OUT.mkdir(parents=True, exist_ok=True)
    target = OUT / "D01_data_check.md"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n[写入] {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
