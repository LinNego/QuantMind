#!/usr/bin/env python3
"""交互式查数据（第 1 天用这个练手）

用法：
    cd /data/ljc/vigap/quant/QuantMind
    python research/repl.py

进去之后，直接对 `q(...)` 里塞 SQL：

    q("SELECT * FROM kf LIMIT 5")                      # 前复权 5 行
    q("SELECT count(*) FROM ku WHERE dt = 20261023")   # 某天多少只
    q("SELECT symbol, close FROM ku WHERE symbol='600519.SH' ORDER BY dt DESC LIMIT 3")

可用视图（只登记真正有数据的表）：
    ku = 不复权日线   kf = 前复权   kb = 后复权   idx = 指数
    cal = 交易日历    inst = 个股详情   sector = 概念板块
"""
from __future__ import annotations

import os

import re
import sys
from pathlib import Path

import duckdb

REPO = Path(__file__).resolve().parents[1]
DATA = Path(os.getenv("QM_QUANTDB_DATA_DIR", "/opt/quantmind/data/quantdb"))
SQL_FILE = REPO / "research" / "qdb.sql"

PREFIX = "SELECT * FROM read_parquet('"


def load_views(con: duckdb.DuckDBPyConnection) -> tuple[int, int]:
    """把 qdb.sql 里的视图建出来，跳过本地没下的数据集。"""
    text = SQL_FILE.read_text(encoding="utf-8")
    ok = fail = 0
    for stmt in re.split(r";\s*\n", text):
        stmt = stmt.strip()
        if not stmt or stmt.startswith("--"):
            continue
        # 只保留路径存在（有 parquet）的视图
        if PREFIX in stmt:
            path = stmt.split(PREFIX, 1)[1].split("'", 1)[0]
            base = path.split("/**")[0]
            if not Path(base).is_dir() or not any(Path(base).rglob("*.parquet")):
                fail += 1
                continue
        try:
            con.execute(stmt)
            ok += 1
        except Exception:
            fail += 1
    return ok, fail


def main() -> int:
    import pandas as pd

    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", 30)
    pd.set_option("display.max_colwidth", 20)

    con = duckdb.connect()
    con.execute("SET enable_progress_bar=false")
    con.execute("SET threads=8")
    ok, skip = load_views(con)

    def q(sql: str):
        """跑一条 SQL，返回 DataFrame。"""
        return con.execute(sql).df()

    print(f"已建视图 {ok} 个（跳过 {skip} 个本地没下的数据集）")
    print("试一试：")
    print('  q("SELECT * FROM kf LIMIT 5")')
    print('  q("SELECT count(*) AS 股票数 FROM ku WHERE dt = 20261023")')
    print('  q("SELECT * FROM cal LIMIT 3")')
    print("退出：exit() 或 Ctrl-D\n")

    import code

    code.interact(local={"q": q, "con": con})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
