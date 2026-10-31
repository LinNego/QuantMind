import os
#!/usr/bin/env python3
"""第 1 天练习：改数字玩（07-阶段0-每日执行计划.md 第 1 天原话）

改下面两个变量，再跑一次，直到不看笔记也能写出来：
    DATE   交易日，格式 YYYYMMDD，必须落在 20160104 ~ 20261023
    SYMBOL 股票代码，带后缀，如 600519.SH / 000001.SZ / 301139.SZ

用法：
    cd /data/ljc/vigap/quant/QuantMind
    python research/practice.py
"""
DATE = 20261023
SYMBOL = "600519.SH"

import duckdb
import pandas as pd

pd.set_option("display.width", 200)
pd.set_option("display.max_columns", 30)

DATA_DIR = os.getenv("QM_QUANTDB_DATA_DIR", "/opt/quantmind/data/quantdb")
KU = f"{DATA_DIR}/1_kline_data/daily_unadjusted/*/data.parquet"
con = duckdb.connect()
con.execute("SET threads=8")

print(f"=== 1. {DATE} 这一天的前 5 行（不复权）===")
print(con.execute(f"""
    SELECT symbol, dt, open, high, low, close, volume, amount
    FROM read_parquet('{KU}', hive_partitioning = true)
    WHERE dt = {DATE}
    LIMIT 5
""").df().to_string(index=False))

print(f"\n=== 2. {DATE} 这一天有数据的股票数 ===")
print(con.execute(f"""
    SELECT count(DISTINCT symbol) AS 股票数, round(sum(amount)/1e4, 2) AS 总成交额_亿元
    FROM read_parquet('{KU}', hive_partitioning = true)
    WHERE dt = {DATE}
""").df().to_string(index=False))

print(f"\n=== 3. {SYMBOL} 最近 5 个交易日 ===")
print(con.execute(f"""
    SELECT dt, open, high, low, close, volume, amount
    FROM read_parquet('{KU}', hive_partitioning = true)
    WHERE symbol = '{SYMBOL}' ORDER BY dt DESC LIMIT 5
""").df().to_string(index=False))

print(f"\n=== 4. 这张表到底有哪些列 ===")
print(con.execute(f"DESCRIBE SELECT * FROM read_parquet('{KU}', hive_partitioning = true)").df().to_string(index=False))
