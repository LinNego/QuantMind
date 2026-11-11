"""构建事件研究用的本地缓存（跑一次，后面复用）。

cache.duckdb 里三张表：
  cal  : 交易日序列 d + seq（事件时间「第几个交易日」的唯一口径）
  px   : 个股前复权收盘价与日收益
  mkt  : 全市场等权日收益（事件研究的基准）
"""
import os
import time

import duckdb

P = "/opt/quantmind/data/quantdb"
CACHE = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scratch", "cache.duckdb"))

con = duckdb.connect(CACHE)
con.execute("SET threads=8")
con.execute("SET memory_limit='8GB'")


def has(name):
    return con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name=?", [name]
    ).fetchone()[0] > 0


t0 = time.time()

if not has("cal"):
    con.execute(f"""
    CREATE TABLE cal AS
    SELECT DISTINCT strptime(TradingDate, '%Y%m%d')::DATE AS d
    FROM read_parquet('{P}/2_base_sector/trading_calendar/**/*.parquet', union_by_name=true)
    WHERE IsTradingDay = 1
    """)
    con.execute("ALTER TABLE cal ADD COLUMN seq BIGINT")
    con.execute("UPDATE cal SET seq = t.seq FROM (SELECT d, row_number() OVER (ORDER BY d)-1 AS seq FROM cal) t WHERE cal.d = t.d")
    print("cal done", time.time() - t0)

if not has("px"):
    con.execute(f"""
    CREATE TABLE px AS
    SELECT symbol, CAST(time AS DATE) AS d, close,
           close / lag(close) OVER (PARTITION BY symbol ORDER BY time) - 1 AS r
    FROM read_parquet('{P}/1_kline_data/daily_forward/**/*.parquet', union_by_name=true)
    WHERE time >= DATE '2015-06-01'
    """)
    print("px done", time.time() - t0)

if not has("mkt"):
    con.execute("CREATE TABLE mkt AS SELECT d, avg(r) AS mkt_r, count(*) AS n FROM px GROUP BY d")
    print("mkt done", time.time() - t0)

print(con.execute("SELECT (SELECT count(*) FROM cal), (SELECT count(*) FROM px), (SELECT count(*) FROM mkt)").fetchone())
print("总耗时 %.0fs" % (time.time() - t0))

# ---- kline：不复权 OHLC + 成交额，用于涨停/停牌/流动性判定 ----
import duckdb as _dd  # noqa: E402
if not has("kline"):
    con.execute(f"""
    CREATE TABLE kline AS
    SELECT symbol, CAST(time AS DATE) AS d, open, high, low, close, volume, amount,
           lag(close) OVER (PARTITION BY symbol ORDER BY time) AS prev_close
    FROM read_parquet('{P}/1_kline_data/daily_unadjusted/**/*.parquet', union_by_name=true)
    WHERE time >= DATE '2015-06-01'
    """)
    print("kline done", time.time() - t0)
print(con.execute("SELECT count(*) FROM kline").fetchone())
