#!/usr/bin/env python3
"""D06 + D10：缺失率、起始日、两个暗坑

一次回答四件事：
  1. features_daily 每列缺失多少
  2. 每列「第一份非空数据」是哪天（新增列在历史区间天然为空，不是坏数据）
  3. 暗坑 A：future_return_* 六列是标签，不是特征
  4. 暗坑 B：29 个数值列以字符串存储，排序/比较会静默出错

用法：
    qm
    python research/01-week1/day5/d06_missing.py
产出：
    research/out/D06_missing.md
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "research" / "out" / "D06_missing.md"
DATA_DIR = os.getenv("QM_QUANTDB_DATA_DIR", "/opt/quantmind/data/quantdb")
FEAT = f"{DATA_DIR}/6_ml_datasets/features_daily/*/data.parquet"

con = duckdb.connect()
con.execute("SET enable_progress_bar = false")
lines: list[str] = []


def p(s: str = "") -> None:
    print(s)
    lines.append(s)


cols = [
    (r[0], r[1])
    for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{FEAT}')").fetchall()
]
names = [c for c, _ in cols if c != "dt"]
n_rows = con.execute(f"SELECT count(*) FROM read_parquet('{FEAT}')").fetchone()[0]

p("# D06 缺失率与起始日 + D10 两个暗坑")
p()
p(f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}")
p(f"- 表：`features_daily` ｜ {len(cols)} 列 ｜ {n_rows:,} 行")
p()

# ---------------------------------------------------------------- 缺失率
agg = ", ".join(
    f'count("{c}") AS c{i}, min(dt) FILTER (WHERE "{c}" IS NOT NULL) AS d{i}'
    for i, c in enumerate(names)
)
row = con.execute(f"SELECT {agg} FROM read_parquet('{FEAT}')").fetchone()
stat = [(c, n_rows - row[2 * i], row[2 * i + 1]) for i, c in enumerate(names)]

p("## 1. 全列缺失率（按缺失从多到少）")
p()
p("| # | 列 | 类型 | 缺失率 | 第一份非空日期 |")
p("|---|---|---|---|---|")
for i, (c, miss, first) in enumerate(sorted(stat, key=lambda x: -x[1]), 1):
    t = dict(cols)[c]
    p(f"| {i} | `{c}` | {t} | {miss / n_rows:.1%} | {first if first else '全空'} |")
p()

# ---------------------------------------------------------------- 分组解释
new_2026 = [c for c, m, f in stat if f and str(f).startswith("2026") and m / n_rows > 0.5]
all_ok = [c for c, m, _ in stat if m == 0]
p("## 2. 怎么读这张表")
p()
p(f"**完全没缺的列：{len(all_ok)} 个** —— 主力数据（close / pe_ttm / total_mv / is_st …）都在其中。")
p()
if new_2026:
    p(f"**2026 年才开始的列：{len(new_2026)} 个**，2016–2025 天然全空，**不是坏数据**：")
    p()
    p("```")
    for c in new_2026:
        p(f"  {c}")
    p("```")
    p()
    p("→ 这些列只能用在 2026 之后。做 2016–2024 回测时，")
    p("  `zt_price` / `hs_turnover` / `zaf` 一律不可用，要自己按板块算涨跌停、自己算换手。")
    p()

p("## 3. 哪些列是「中途才开始」的（真正要小心的）")
p()
# 真正的「中途才开始」：比数据起点晚 4 个月以上（排除指标自身的预热期）
mid = [
    (c, m, f)
    for c, m, f in stat
    if f and str(f) > "20160501" and not str(f).startswith("2026")
]
if mid:
    p("| 列 | 缺失率 | 起始日 | 意味着 |")
    p("|---|---|---|---|")
    for c, m, f in mid:
        p(f"| `{c}` | {m / n_rows:.1%} | {f} | {f} 之前用不了 |")
else:
    p("无")
p()
p("> 注意：`ma5` / `ma20` / `rsi_14` 这类指标在 2016 年 1–4 月才陆续有值，")
p("> 那是**指标自己的预热期**（要攒够 N 天才能算），不是数据缺失。")
p("> 它们从 2016 年 5 月起就全有了，不用担心。")
p()

# ---------------------------------------------------------------- 暗坑 A
fut = [c for c in names if c.startswith("future_return")]
p("## 4. 暗坑 A：标签泄漏列")
p()
p(f"共 **{len(fut)}** 列：`{'`, `'.join(fut)}`")
p()
p("```python")
p("# ❌ 这样写，未来数据直接进模型/进打分")
p("df = con.execute(\"SELECT * FROM read_parquet('.../features_daily/*/data.parquet')\").df()")
p()
p("# ✅ 显式列举你要的列")
p("COLS = ['symbol', 'dt', 'close', 'pe_ttm', 'total_mv']   # 不含 future_*")
p("```")
p()
p("**约定：读 features_daily 一律写显式列清单。**")
p()

# ---------------------------------------------------------------- 暗坑 B
strs = [c for c, t in cols if t == "VARCHAR"]
p("## 5. 暗坑 B：字符串存储的数值列")
p()
p(f"共 **{len(strs)}** 列：")
p()
p("```")
for c in strs:
    p(f"  {c}")
p("```")
p()

demo = con.execute(f"""
    WITH x AS (
        SELECT symbol, hs_turnover FROM read_parquet('{FEAT}')
        WHERE dt = (SELECT max(dt) FROM read_parquet('{FEAT}')) AND hs_turnover IS NOT NULL
    )
    SELECT '按字符串排' AS 方式, symbol, hs_turnover AS 值 FROM x ORDER BY hs_turnover DESC LIMIT 3
""").fetchall()
demo2 = con.execute(f"""
    WITH x AS (
        SELECT symbol, TRY_CAST(hs_turnover AS DOUBLE) AS v FROM read_parquet('{FEAT}')
        WHERE dt = (SELECT max(dt) FROM read_parquet('{FEAT}')) AND TRY_CAST(hs_turnover AS DOUBLE) IS NOT NULL
    )
    SELECT '转数值后排' AS 方式, symbol, v AS 值 FROM x ORDER BY v DESC LIMIT 3
""").fetchall()
p("实测换手率 Top3（最新一天）：")
p()
p("| 方式 | 第1 | 第2 | 第3 |")
p("|---|---|---|---|")
for tag, rows in (("按字符串排（错）", demo), ("转数值后排（对）", demo2)):
    p(f"| {tag} | " + " | ".join(f"{s} {v}" for _, s, v in rows) + " |")
p()
p("→ 字典序里 `\"9.96\" > \"79.79\"`。**不报错，静默排错。**")
p("→ 凡是 `hs_turnover` / `zt_price` / `zaf` / `total_cap_yi` 这类列，")
p("   排序、比较、求和之前必须 `CAST(... AS DOUBLE)`。")
p()

# ---------------------------------------------------------------- 落盘
OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text("\n".join(lines), encoding="utf-8")
print(f"\n报告已写入：{OUT}")
