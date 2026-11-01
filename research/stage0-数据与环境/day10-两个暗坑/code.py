#!/usr/bin/env python3
"""D10 · 两个暗坑

① features_daily 里的 future_return_* 标签泄漏列
② 以字符串存储的数值列（按字典序排会错）

用法：
    python research/stage0-数据与环境/day10-两个暗坑/code.py

产出：
    output.md（写在本目录）
"""
import os
from pathlib import Path

import duckdb

DATA = os.getenv("QM_QUANTDB_DATA_DIR", "/opt/quantmind/data/quantdb")
FEAT = f"{DATA}/6_ml_datasets/features_daily/*/data.parquet"
OUT = Path(__file__).resolve().parent / "output.md"

con = duckdb.connect()
con.execute("SET enable_progress_bar=false")
cols = con.execute(f"""
    DESCRIBE SELECT * FROM read_parquet('{FEAT}', hive_partitioning=true)
""").fetchall()
cols = [(r[0], r[1]) for r in cols]

leak = [c for c, _ in cols if c.startswith("future_return_")]
vnum = [c for c, t in cols if t == "VARCHAR"]

s_str = con.execute(f"""
    SELECT hs_turnover FROM read_parquet('{FEAT}', hive_partitioning=true)
    WHERE hs_turnover IS NOT NULL ORDER BY hs_turnover DESC LIMIT 3
""").fetchall()
s_num = con.execute(f"""
    SELECT hs_turnover FROM read_parquet('{FEAT}', hive_partitioning=true)
    WHERE hs_turnover IS NOT NULL ORDER BY CAST(hs_turnover AS DOUBLE) DESC LIMIT 3
""").fetchall()

lines = ["# D10 · 两个暗坑", "",
         f"- 数据：`{DATA}/6_ml_datasets/features_daily`（{len(cols)} 列）", "",
         "## 坑 A：标签泄漏列", "",
         f"共 **{len(leak)}** 个：`" + "`, `".join(leak) + "`", "",
         "这些是**未来收益（标签）**，不是特征。`SELECT *` 或整表 merge 会把它们带进特征矩阵，",
         "你会得到一个 IC ≈ 0.9 的「完美模型」，然后拿真钱去验证它。", "",
         "> **约定：读 `features_daily` 一律写显式列清单。**", "",
         "## 坑 B：字符串数值列", "",
         f"共 **{len(vnum)}** 个：`" + "`, `".join(vnum) + "`", "",
         "### 反例：`hs_turnover` 的排序", "",
         "| 排序方式 | Top 3 |", "|---|---|",
         "| 按字符串（字典序） | " + " / ".join(x[0] for x in s_str) + " |",
         "| CAST 成 DOUBLE 后 | " + " / ".join(x[0] for x in s_num) + " |", "",
         "因为字符串比较是**逐字符**的：`'9.96' > '79.79'`（`'9' > '7'`）。", "",
         "> **约定：凡这类列，一律先 `CAST(x AS DOUBLE)` 再参与任何计算。**", "",
         "## 为什么这两个坑最值钱", "",
         "它们**不会让你报错**，只会让你做出一个漂亮的假回测。", ""]

OUT.write_text("\n".join(lines), encoding="utf-8")
print(f"[写入] {OUT}")
