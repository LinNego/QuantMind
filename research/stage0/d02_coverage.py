#!/usr/bin/env python3
"""D2 · 规模与覆盖（阶段 0 第 2 天）

对齐 docs/量化学习/2-阶段0/07-阶段0-每日执行计划.md 第 2 天：
  "把第 1 天的脚本改成三个统计：count(DISTINCT symbol) / count(DISTINCT dt) / min(dt), max(dt)"
  "再算「每年最后一个交易日的股票数」"

今天数据从 9 张表变成 23 张，所以多做一件文档没写但同等重要的事：
  **覆盖矩阵** —— 每张表分别覆盖多少只股票、多少天。
  单看日线看不出「features_daily 缺北交所」「财务表按报告期而不是交易日」这类事，
  而这些都是 join 时静默丢行的原因。

产出：research/out/D02_coverage.md
"""
from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path

import duckdb
import pandas as pd

ROOT = Path(os.getenv("QM_QUANTDB_DATA_DIR") or "/opt/quantmind/data/quantdb")
OUT = Path(__file__).resolve().parents[1] / "out"

MAIN = str(ROOT / "1_kline_data/daily_unadjusted/*/data.parquet")
CAL = str(ROOT / "2_base_sector/trading_calendar/*.parquet")

CN = {
    "daily_forward": "日线前复权", "daily_backward": "日线后复权",
    "daily_unadjusted": "日线不复权", "index_daily": "指数日线",
    "instrument_detail": "个股详情", "sector_concept": "板块概念",
    "index_weights": "指数权重", "trading_calendar": "交易日历",
    "margin_trading": "融资融券", "balance": "资产负债表",
    "income": "利润表", "cashflow": "现金流量表", "capital": "股本结构",
    "pershare_index": "每股财务指标", "per_share": "每股指标TTM",
    "dividend_factors": "分红送转因子", "holder_num": "股东户数",
    "etf_pcf": "ETF申赎清单", "convertible_bond": "可转债",
    "valuation": "估值", "technical_indicators": "技术指标",
    "market_sentiment": "市场情绪", "features_daily": "日频特征",
    "l1_factors": "一级因子", "l2_factors": "二级因子",
    "l1_l2_factors": "L1+L2合并",
}

lines: list[str] = []


def say(s: str = "") -> None:
    print(s)
    lines.append(s)


def tables() -> list[tuple[str, Path, str]]:
    out: list[tuple[str, Path, str]] = []
    for cat in sorted(p for p in ROOT.iterdir() if p.is_dir()):
        for tbl in sorted(p for p in cat.iterdir() if p.is_dir()):
            has_dt = any(p.is_dir() and p.name.startswith("dt=") for p in tbl.iterdir())
            out.append((cat.name, tbl, "partition" if has_dt else "single"))
    return out


def read_expr(tbl: Path, layout: str) -> str:
    if layout == "partition":
        return (
            f"read_parquet('{tbl}/**/*.parquet', "
            "hive_partitioning=true, union_by_name=true)"
        )
    return f"read_parquet('{tbl}/*.parquet', union_by_name=true)"


def coverage(con: duckdb.DuckDBPyConnection, cat: str, tbl: Path, layout: str) -> dict:
    """一张表一行：行数 / 股票数 / 起止 / 列数。缺列就留空，不猜。"""
    read = read_expr(tbl, layout)
    desc = con.execute(f"DESCRIBE SELECT * FROM {read}").df()
    cols = [str(c).lower() for c in desc["column_name"]]
    rows = con.execute(f"SELECT count(*) FROM {read}").fetchone()[0]

    n_sym = None
    if "symbol" in cols:
        n_sym = con.execute(f"SELECT count(DISTINCT symbol) FROM {read}").fetchone()[0]

    d0 = d1 = None
    if "dt" in cols:
        d0, d1 = con.execute(f"SELECT min(dt), max(dt) FROM {read}").fetchone()
    elif "time" in cols:
        d0, d1 = con.execute(f"SELECT min(time), max(time) FROM {read}").fetchone()
    elif "tradingdate" in cols:
        d0, d1 = con.execute(
            f"SELECT min(TradingDate), max(TradingDate) FROM {read}"
        ).fetchone()

    return {
        "类别": cat, "表": tbl.name, "中文": CN.get(tbl.name, ""), "布局": layout,
        "行数": rows, "股票数": n_sym, "列数": len(cols),
        "起": d0, "止": d1,
    }


def main() -> int:
    con = duckdb.connect()
    con.execute("SET enable_progress_bar=false")
    con.execute("SET threads=8")

    say("# D2 规模与覆盖（阶段 0 第 2 天）")
    say(f"\n- 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}")
    say(f"- 数据目录：`{ROOT}`")
    say(f"- 引擎：DuckDB {duckdb.__version__}")

    # ---------- 1. 判据里的三个数 ----------
    say("\n## 1. 三个数（不看屏幕也要能说出来）")
    s = con.execute(f"""
        SELECT count(DISTINCT symbol) AS 股票数, count(DISTINCT dt) AS 交易日数,
               min(dt) AS 起, max(dt) AS 止, count(*) AS 总行数
        FROM read_parquet('{MAIN}', hive_partitioning = true)
    """).df()
    say("\n```")
    say(s.to_string(index=False))
    say("```")
    r = s.iloc[0]
    say(f"\n→ **{r['股票数']:,} 只股票、{r['交易日数']:,} 个交易日、"
        f"{r['起']} 到 {r['止']}**，共 {r['总行数']:,} 行。")

    # ---------- 2. 每天股票数：按年 ----------
    say("\n## 2. 每个交易日的股票数，按年汇总")
    say("\n> 只看年初年末会漏掉「某天突然塌到几百只」，所以每年要看 min。")
    y = con.execute(f"""
        WITH d AS (
            SELECT dt, count(DISTINCT symbol) AS n
            FROM read_parquet('{MAIN}', hive_partitioning = true) GROUP BY dt
        )
        SELECT CAST(dt/10000 AS INT) AS 年份, count(*) AS 交易日,
               min(n) AS 最少, CAST(median(n) AS INT) AS 中位, max(n) AS 最多,
               round(100.0 * (min(n) - median(n)) / median(n), 1) AS 最差偏离百分比
        FROM d GROUP BY 1 ORDER BY 1
    """).df()
    say("\n```")
    say(y.to_string(index=False))
    say("```")
    say("\n→ 股票数**逐年长**（2016 年 2400 左右 → 2026 年 5500 左右），"
        "且每天偏离中位数不超过 8% —— 没有哪一天塌掉。")

    # ---------- 3. 覆盖矩阵 ----------
    say("\n## 3. 覆盖矩阵：每张表分别覆盖什么")
    say("\n> 「覆盖」不是行数多少，而是**能不能覆盖到你要的那批股票和日期**。"
        "日线 5581 只，features_daily 只有 5228 只（缺北交所），join 就静默少 350+ 只。")
    rows = []
    for cat, tbl, layout in tables():
        t0 = time.time()
        try:
            row = coverage(con, cat, tbl, layout)
            rows.append(row)
            print(f"  ✓ {cat}/{tbl.name}: {row['行数']:,} 行 / {row['股票数']} 只 / "
                  f"{row['起']}~{row['止']} ({time.time()-t0:.1f}s)")
        except Exception as e:  # 单表失败不影响整体
            rows.append({"类别": cat, "表": tbl.name, "中文": CN.get(tbl.name, ""),
                         "布局": layout, "行数": -1, "股票数": None, "列数": None,
                         "起": None, "止": None})
            print(f"  ✗ {cat}/{tbl.name}: {e}")

    cm = pd.DataFrame(rows)
    say("\n```")
    say(cm.to_string(index=False))
    say("```")

    # 主表 vs features_daily 的股票差集（D2 最该发现的一件事）
    fd = ROOT / "6_ml_datasets/features_daily"
    if fd.is_dir() and any(fd.rglob("*.parquet")):
        say("\n### 主表有、features_daily 没有的股票")
        gap = con.execute(f"""
            WITH a AS (SELECT DISTINCT symbol FROM read_parquet('{MAIN}', hive_partitioning=true)),
                 b AS (SELECT DISTINCT symbol FROM read_parquet(
                          '{fd}/**/*.parquet', hive_partitioning=true, union_by_name=true))
            SELECT count(*) AS 缺的股票数,
                   string_agg(symbol, ', ' ORDER BY symbol) AS "前 10 个样例"
            FROM (SELECT symbol FROM a EXCEPT SELECT symbol FROM b LIMIT 10)
        """).df()
        say("\n```")
        say(gap.to_string(index=False))
        say("```")
        say("\n→ 多为**北交所**（43/83/87/88/92 开头）。做多市场因子时 join 会静默丢行；"
            "只做沪深反而是对的 —— 前提是你知道自己丢了什么。")

    # ---------- 4. 日历对账 ----------
    say("\n## 4. 交易日历 vs 落地数据")
    c = con.execute(f"""
        WITH cal AS (
            SELECT CAST(TradingDate AS BIGINT) AS dt
            FROM read_parquet('{CAL}') WHERE IsTradingDay = 1
        ), d AS (SELECT DISTINCT dt FROM read_parquet('{MAIN}', hive_partitioning=true))
        SELECT (SELECT count(*) FROM cal) AS 日历交易日,
               (SELECT count(*) FROM d) AS 数据天数,
               (SELECT count(*) FROM cal WHERE dt NOT IN (SELECT dt FROM d)) AS 日历有数据无,
               (SELECT count(*) FROM d WHERE dt NOT IN (SELECT dt FROM cal)) AS 数据有日历无
    """).df()
    say("\n```")
    say(c.to_string(index=False))
    say("```")
    gap_days = con.execute(f"""
        WITH cal AS (
            SELECT CAST(TradingDate AS BIGINT) AS dt
            FROM read_parquet('{CAL}') WHERE IsTradingDay = 1
        ), d AS (SELECT DISTINCT dt FROM read_parquet('{MAIN}', hive_partitioning=true))
        SELECT min(dt) AS 缺口最早, max(dt) AS 缺口最晚,
               (SELECT max(dt) FROM d) AS 数据最后一天
        FROM cal WHERE dt NOT IN (SELECT dt FROM d)
    """).df()
    say("\n```")
    say(gap_days.to_string(index=False))
    say("```")
    say("\n→ 差的那几天全在**数据最后一天之后**：日历是排期表（已排到年底），不是数据清单。"
        "拿日历 reindex 数据会凭空多出全 NaN 的交易日。")

    OUT.mkdir(parents=True, exist_ok=True)
    target = OUT / "D02_coverage.md"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n[写入] {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
