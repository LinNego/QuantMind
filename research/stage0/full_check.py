#!/usr/bin/env python3
"""00 · 数据体检报告（阶段 0 交付物）

**只依赖 duckdb**（不 import pandas），所以在宿主机直接跑就行，不需要任何容器：

    python research/stage0/full_check.py

目标不是"把数据跑一遍"，而是**回答 7 个具体问题**，
任何一个答案是"我不确定"，就说明还没过关（Gate 1）。
对应 docs/量化学习/1-开始/05-学习计划.md 阶段 0 的清单：

  1. 多少只股票、多少交易日、时间范围
  2. 每天有数据的股票数
  3. 各字段缺失率 + **这一列从哪天开始有值**
  4. 异常：收盘价 <= 0、成交量 <= 0、最高价 < 最低价
  5. 异常：涨跌幅超过所在板块的涨跌停（主板 10 / 双创 20 / 北交所 30）
  6. 每年「消失」的股票数（幸存者偏差）
  7. 单位自检 close*volume/amount ≈ 1e4（个股 volume=股、amount=万元）

外加本项目特有的四个暗坑：

  A. features_daily 自带 future_return_* 六列 —— 它是标签，不是特征
  B. features_daily 新增 30 列里有 VARCHAR 字符串 —— 排序/比较会静默出错
  C. **features_daily.close 是后复权价** —— 不是可成交价，不能和 zt_price（不复权）比
  D. features_daily 不含北交所 —— 和它 join 会静默丢行
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

import duckdb

# --------------------------------------------------------------------------
# 0. 找数据目录（宿主机 /opt/quantmind/data/quantdb，容器内 /data/quantdb）
# --------------------------------------------------------------------------
CANDIDATES = [
    os.getenv("QM_QUANTDB_DATA_DIR"),
    "/data/quantdb",
    "/opt/quantmind/data/quantdb",
    str(Path(__file__).resolve().parents[2] / "data" / "quantdb"),
]


def find_root() -> Path:
    for c in CANDIDATES:
        if c and Path(c).is_dir() and any(Path(c).iterdir()):
            return Path(c)
    sys.exit("找不到 quantdb 数据目录，用 QM_QUANTDB_DATA_DIR 显式指定")


ROOT = find_root()
KLINE = ROOT / "1_kline_data" / "daily_unadjusted"
ADJ = ROOT / "1_kline_data" / "daily_forward"
BWD = ROOT / "1_kline_data" / "daily_backward"
FEAT = ROOT / "6_ml_datasets" / "features_daily"

con = duckdb.connect()
con.execute("SET enable_progress_bar = false")
lines: list[str] = []


def p(s: str = "") -> None:
    print(s)
    lines.append(s)


def rows(sql: str) -> list[tuple]:
    return con.execute(sql).fetchall()


def one(sql: str) -> tuple:
    return con.execute(sql).fetchone()


def glob(d: Path) -> str:
    return f"{d}/*/data.parquet"


def cols_of(path: str) -> list[tuple[str, str]]:
    return [(r[0], r[1]) for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{path}')").fetchall()]


# 上游列名不统一（symbol/wind_code、volume/vol_in_stock），先探测再拼 SQL
_kc = dict(cols_of(glob(KLINE)))
SYM = "symbol" if "symbol" in _kc else "wind_code"
VOL = "volume" if "volume" in _kc else ("vol_in_stock" if "vol_in_stock" in _kc else "volinstock")

p("# QuantDB 数据体检报告")
p()
p(f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}")
p(f"- 数据目录：`{ROOT}`")
p(f"- 列名口径：symbol=`{SYM}`，volume=`{VOL}`")
p()

# --------------------------------------------------------------------------
# 1. 规模
# --------------------------------------------------------------------------
p("## 1. 规模")
days, d0, d1, n_sym, n_rows = one(f"""
    SELECT count(DISTINCT dt), min(dt), max(dt), count(DISTINCT "{SYM}"), count(*)
    FROM read_parquet('{glob(KLINE)}', hive_partitioning = true)
""")
p(f"- 交易日：**{days:,}** 个（{d0} ~ {d1}）")
p(f"- 股票：**{n_sym:,}** 只；总行数 **{n_rows:,}**")
p()

# --------------------------------------------------------------------------
# 2. 每年最后一个交易日的股票数
# --------------------------------------------------------------------------
p("## 2. 每天有数据的股票数（每年最后一个交易日）")
p("| 交易日 | 股票数 |")
p("|---|---|")
for dt, n in rows(f"""
    WITH last AS (
        SELECT dt // 10000 AS yr, max(dt) AS dt
        FROM read_parquet('{glob(KLINE)}', hive_partitioning = true) GROUP BY yr
    )
    SELECT k.dt, count(DISTINCT k."{SYM}")
    FROM read_parquet('{glob(KLINE)}', hive_partitioning = true) k
    JOIN last ON k.dt = last.dt
    GROUP BY k.dt ORDER BY k.dt
"""):
    p(f"| {dt} | {n:,} |")
p()

# --------------------------------------------------------------------------
# 3. 缺失率 + 起始日
# --------------------------------------------------------------------------
p("## 3. 字段缺失率（features_daily）")
fc = [c for c, _ in cols_of(glob(FEAT)) if c != "dt"]
n_feat = one(f"SELECT count(*) FROM read_parquet('{glob(FEAT)}', hive_partitioning=true)")[0]
aggs = ", ".join(
    f'count("{c}") AS c{i}, min(dt) FILTER (WHERE "{c}" IS NOT NULL) AS d{i}'
    for i, c in enumerate(fc)
)
row = one(f"SELECT {aggs} FROM read_parquet('{glob(FEAT)}', hive_partitioning = true)")
stat = [(c, n_feat - row[2 * i], row[2 * i + 1]) for i, c in enumerate(fc)]
stat.sort(key=lambda x: -x[1])
p(f"共 {len(fc) + 1} 列、{n_feat:,} 行。缺失最严重的 12 列（**注意「第一份非空日期」**）：")
p()
p("| 列 | 缺失率 | 第一份非空日期 |")
p("|---|---|---|")
for c, m, first in stat[:12]:
    p(f"| `{c}` | {m / n_feat:.1%} | {first if first else '全空'} |")
p()
p("> 缺失率高不一定是坏数据：**2026 年新增的那批列在 2016-2025 天然是空的**。")
p("> 看「第一份非空日期」才知道它能在哪段区间用。老列如果中段整片缺失，才是真问题。")
p()

# --------------------------------------------------------------------------
# 4. 价格/成交量异常
# --------------------------------------------------------------------------
p("## 4. 价格与成交量异常（daily_unadjusted）")
for name, v in zip(
    ["收盘价 <= 0", "成交量 <= 0", "最高价 < 最低价", "成交额 <= 0"],
    one(f"""
        SELECT count(*) FILTER (WHERE close <= 0),
               count(*) FILTER (WHERE "{VOL}" <= 0),
               count(*) FILTER (WHERE high < low),
               count(*) FILTER (WHERE amount <= 0)
        FROM read_parquet('{glob(KLINE)}', hive_partitioning = true)
    """),
):
    p(f"- {name}：**{v:,}** 条")
p()

# --------------------------------------------------------------------------
# 5. 涨跌幅超档（按板块分档）
# --------------------------------------------------------------------------
p("## 5. 涨跌幅超档检查（按板块分档，容差 0.6%）")
p()
p("> 主板 ±10%、创业板/科创板 ±20%、北交所 ±30%。贴着档位是正常涨停；")
p("> 超过档位 0.6 个点以上，才说明是数据问题（或未复权/新股首日）。")
p()
p("| 板块 | 样本 | 超档条数 | 占比 | 单日最大绝对涨跌幅 |")
p("|---|---|---|---|---|")
for board, n, over, mx in rows(f"""
    WITH x AS (
        SELECT TRY_CAST(zaf AS DOUBLE) AS zaf,
               CASE WHEN symbol LIKE '30%' OR symbol LIKE '68%' THEN 20.0
                    WHEN symbol LIKE '43%' OR symbol LIKE '83%' OR symbol LIKE '87%'
                      OR symbol LIKE '88%' OR symbol LIKE '92%' THEN 30.0
                    ELSE 10.0 END AS lim
        FROM read_parquet('{glob(FEAT)}', hive_partitioning = true)
    )
    SELECT CASE WHEN lim = 20.0 THEN '创业板/科创板'
                WHEN lim = 30.0 THEN '北交所' ELSE '主板' END,
           count(*), count(*) FILTER (WHERE abs(zaf) > lim + 0.6), max(abs(zaf))
    FROM x WHERE zaf IS NOT NULL GROUP BY 1 ORDER BY 2 DESC
"""):
    p(f"| {board} | {n:,} | {over:,} | {over / n:.3%} | {mx:.2f}% |")
p()

# --------------------------------------------------------------------------
# 6. 每年「消失」的股票数（幸存者偏差）
# --------------------------------------------------------------------------
p("## 6. 每年消失的股票数（幸存者偏差检查）")
p()
p("> 正常形态：每年都有一批股票不再出现（退市/并购）。**如果一年都没少，说明数据里没有退市股。**")
p()
df = rows(f"""
    WITH y AS (
        SELECT dt // 10000 AS yr, "{SYM}" AS s
        FROM read_parquet('{glob(KLINE)}', hive_partitioning = true) GROUP BY yr, s
    ),
    a AS (SELECT yr, count(*) AS n FROM y GROUP BY yr),
    b AS (
        SELECT y1.yr, count(*) AS gone
        FROM y y1 LEFT JOIN y y2 ON y1.s = y2.s AND y2.yr = y1.yr + 1
        WHERE y2.s IS NULL AND y1.yr < (SELECT max(yr) FROM y) GROUP BY y1.yr
    )
    SELECT a.yr, a.n, coalesce(b.gone, 0) FROM a LEFT JOIN b USING (yr) ORDER BY a.yr
""")
p("| 年份 | 当年股票数 | 次年起消失 |")
p("|---|---|---|")
for yr, n, gone in df:
    p(f"| {yr} | {n:,} | {gone:,} |")
p()
if all(g == 0 for _, _, g in df):
    p("> ⚠️ **每年都是 0：这份数据里没有退市股。** 你的所有回测都含着幸存者偏差的水分，")
    p("> 实盘要打折。这条结论请写进实验日志。")
    p()

# --------------------------------------------------------------------------
# 7. 单位自检
# --------------------------------------------------------------------------
p("## 7. 单位自检 close*volume/amount（个股应 ≈ 1e4）")
p()
p("> 成立 = volume 是股、amount 是万元。偏离一个数量级，说明单位或复权口径错了。")
p()
p("| 年份 | 中位数比值 | 样本 |")
p("|---|---|---|")
for yr, ratio, n in rows(f"""
    SELECT dt // 10000 AS yr, median(close * "{VOL}" / amount), count(*)
    FROM read_parquet('{glob(KLINE)}', hive_partitioning = true)
    WHERE amount > 0 AND "{VOL}" > 0 AND close > 0 GROUP BY yr ORDER BY yr
"""):
    flag = "" if 1e3 < ratio < 1e5 else " ⚠️"
    p(f"| {yr} | {ratio:.0f}{flag} | {n:,} |")
p()

# --------------------------------------------------------------------------
# 8. 暗坑自检
# --------------------------------------------------------------------------
p("## 8. 暗坑自检")

fut = [c for c in fc if c.startswith("future_return")]
p()
p(f"**A. 标签泄漏列**：`{', '.join(fut) if fut else '无'}`")
p("→ 这些是**未来收益（标签）**。用 `SELECT *` 或整表 merge，未来数据就进模型了。")
p("→ 约定：读 features_daily 一律写显式列清单。")
p()

strs = [c for c, t in cols_of(glob(FEAT)) if t == "VARCHAR"]
p(f"**B. 字符串数值列（{len(strs)} 个）**：`{', '.join(strs)}`")
p('→ 直接 ORDER BY 会按字典序（"9.96" > "79.79"），必须 CAST 后才能比较/排序/求和。')
p()

# 复权口径：必须挑一个「历史日期」比，因为前复权在最新交易日必然等于不复权
d_hist = one(f"""
    SELECT max(dt) FROM read_parquet('{glob(ADJ)}', hive_partitioning = true) WHERE dt < 20200101
""")[0]
fwd, bwd, una, dtc = one(f"""
    SELECT round(avg(fw.close), 2), round(avg(bk.close), 2), round(avg(un.close), 2), count(*)
    FROM read_parquet('{glob(ADJ)}', hive_partitioning = true) fw
    JOIN read_parquet('{glob(BWD)}', hive_partitioning = true) bk ON fw.{SYM}=bk.{SYM} AND fw.dt=bk.dt
    JOIN read_parquet('{glob(KLINE)}', hive_partitioning = true) un ON fw.{SYM}=un.{SYM} AND fw.dt=un.dt
    WHERE fw.dt = {d_hist}
""")
_, n_feat_bad = one(f"""
    SELECT count(*), count(*) FILTER (WHERE abs(fc.close - bk.close) > 0.01)
    FROM read_parquet('{glob(FEAT)}', hive_partitioning = true) fc
    JOIN read_parquet('{glob(BWD)}', hive_partitioning = true) bk ON fc.{SYM}=bk.{SYM} AND fc.dt=bk.dt
""")
p(f"**C. 复权口径**（{d_hist} 那天 {dtc:,} 只股票的平均价）：")
p(f"前复权 **{fwd}** ｜ 不复权 **{una}** ｜ 后复权 **{bwd}**")
p(f"→ `features_daily.close` 与 `daily_backward.close` 相差的只有 **{n_feat_bad:,}** 行，")
p("   **说明它的 close 是后复权口径，不是可成交价。** 算收益率没问题（等比缩放），")
p("   但拿去和 `zt_price`（不复权）比大小、或做「股价低于 X 元」这类过滤，全错。")
p()

# --------------------------------------------------------------------------
# 9. 覆盖缺口 + 新股污染
# --------------------------------------------------------------------------
p("## 9. 覆盖缺口与新股污染")
p()
p("日线有股票在这些**在 features_daily 里完全没有**：")
p()
p("| 板块 | 缺失股票数 |")
p("|---|---|")
for board, n in rows(f"""
    WITH a AS (SELECT DISTINCT "{SYM}" AS s FROM read_parquet('{glob(KLINE)}', hive_partitioning=true)),
         b AS (SELECT DISTINCT symbol AS s FROM read_parquet('{glob(FEAT)}', hive_partitioning=true))
    SELECT CASE WHEN s LIKE '%.BJ' THEN '北交所' ELSE '沪深' END, count(*)
    FROM a WHERE s NOT IN (SELECT s FROM b) GROUP BY 1 ORDER BY 2 DESC
"""):
    p(f"| {board} | {n:,} |")
p()
p("→ `features_daily` 只覆盖沪深。和它 join 会**静默丢掉北交所**——")
p("  只做沪深就是好事，以为在做全市场就是错的。")
p()

df = rows(f"""
    WITH x AS (
        SELECT symbol, TRY_CAST(zaf AS DOUBLE) AS z
        FROM read_parquet('{glob(FEAT)}', hive_partitioning = true)
        WHERE symbol LIKE '30%' OR symbol LIKE '68%'
    )
    SELECT symbol, count(*), max(abs(z))
    FROM x WHERE abs(z) > 20.6 GROUP BY symbol ORDER BY 3 DESC LIMIT 5
""")
if df:
    p("**新股首日的连带污染**：以下股票**连续多日**的 `zaf` 都是同一个巨大值——")
    p("新股首日无涨跌幅限制，而 `zaf` 的基准价没跟着改，错误被一路沿用。")
    p()
    p("| 代码 | 异常行数 | 最大绝对涨跌幅 |")
    p("|---|---|---|")
    for s, n, mx in df:
        p(f"| {s} | {n} | {mx:.1f}% |")
    p()
    p("→ 阶段 2、3 必须**剔除上市不满 N 天的新股**（N=60 起步），否则会污染分层收益。")
    p()

# --------------------------------------------------------------------------
# 落盘
# --------------------------------------------------------------------------
for out_dir in (Path("/data/research"), ROOT.parent / "research", Path.cwd() / "research"):
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"00_data_check_{datetime.now():%Y%m%d_%H%M}.md"
        out.write_text("\n".join(lines), encoding="utf-8")
        print(f"\n报告已写入：{out}")
        break
    except OSError:
        continue
else:
    print("\n报告落盘失败：所有候选目录都不可写，内容见上")
