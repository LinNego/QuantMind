#!/usr/bin/env python3
"""day1 · 把 QuantDB 每张表翻一遍：有哪些列、什么类型、多少行、长什么样。

用法（在仓库根目录 /root/quant/QuantMind 下执行）：

    /root/quant/.venv/bin/python research/01-week1/day1/inspect_tables.py

产出：
    research/01-week1/day1/tables_schema.md     ← 数据字典（每张表的全部列 + 类型 + 样本行）
    research/01-week1/day1/tables_overview.csv  ← 一张表一行：表名/文件数/行数/列数/体积

只做三件事，不做分析：
    1. DESCRIBE  → 全部列名 + 类型
    2. count(*)  → 行数（parquet 元数据 + 一次扫描）
    3. LIMIT 3   → 三行真实数据，用来看值长什么样

设计约束：
    - 不把整表读进内存（2623 个分区、1000 万行，全读会吃掉几 GB 内存）
    - 分区表和单文件表自动识别（看有没有 dt= 子目录）
    - 单张表出错不中断整体，错误写进 md
"""
from __future__ import annotations

import csv
import os
import sys
import time
import traceback
from pathlib import Path

import duckdb

ROOT = Path(os.getenv("QM_QUANTDB_DATA_DIR") or "/opt/quantmind/data/quantdb")
HERE = Path(__file__).resolve().parent
MD = HERE / "tables_schema.md"
CSV = HERE / "tables_overview.csv"

# 表名 → 中文名（来自 backend/shared/quantdb_datasets.py 的 29 条 DatasetSpec）
CN = {
    "daily_forward": "日线前复权",
    "daily_backward": "日线后复权",
    "daily_unadjusted": "日线不复权",
    "index_daily": "指数日线",
    "min5_kline": "5分钟线",
    "min1_kline": "1分钟线",
    "tick_data": "逐笔成交",
    "instrument_detail": "个股详情",
    "sector_concept": "板块概念",
    "index_weights": "指数权重",
    "trading_calendar": "交易日历",
    "margin_trading": "融资融券",
    "balance": "资产负债表",
    "income": "利润表",
    "cashflow": "现金流量表",
    "capital": "股本结构",
    "pershare_index": "每股财务指标",
    "per_share": "每股指标TTM",
    "dividend_factors": "分红送转因子",
    "holder_num": "股东户数",
    "etf_pcf": "ETF申赎清单",
    "convertible_bond": "可转债",
    "valuation": "估值",
    "technical_indicators": "技术指标",
    "market_sentiment": "市场情绪",
    "features_daily": "日频特征",
    "l1_factors": "一级因子",
    "l2_factors": "二级因子",
    "l1_l2_factors": "L1+L2合并",
}

# 单位提示：只写文档里明确验证过的，不猜
UNIT_HINT = {
    "volume": "股（个股）；指数表是手",
    "amount": "万元",
    "open": "元",
    "high": "元",
    "low": "元",
    "close": "元（注意各表复权口径不同）",
    "dt": "YYYYMMDD 整数",
    "time": "时间戳",
    "symbol": "后缀式 600519.SH",
    "TurnoverVolume": "",
}


def find_tables() -> list[tuple[str, Path, str]]:
    """返回 [(类别, 表路径, 布局)]，布局 = partition / single。"""
    out: list[tuple[str, Path, str]] = []
    for cat in sorted(ROOT.iterdir()):
        if not cat.is_dir():
            continue
        for tbl in sorted(cat.iterdir()):
            if not tbl.is_dir():
                continue
            has_dt = any(p.is_dir() and p.name.startswith("dt=") for p in tbl.iterdir())
            out.append((cat.name, tbl, "partition" if has_dt else "single"))
    return out


def parquet_glob(tbl: Path, layout: str) -> str:
    if layout == "partition":
        return str(tbl / "**" / "*.parquet")
    return str(tbl / "*.parquet")


def md_table(df) -> str:
    """把 DataFrame 渲染成 markdown 表格，不依赖 tabulate。"""
    cols = list(df.columns)
    lines = ["| " + " | ".join(str(c) for c in cols) + " |",
             "|" + "|".join(["---"] * len(cols)) + "|"]
    for _, row in df.iterrows():
        cells = []
        for c in cols:
            v = row[c]
            s = "" if v is None else str(v)
            s = s.replace("\n", " ").replace("|", "\\|")
            if len(s) > 40:
                s = s[:40] + "…"
            cells.append(s)
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def main() -> int:
    if not ROOT.is_dir():
        print(f"[错误] 数据目录不存在：{ROOT}")
        return 2

    con = duckdb.connect()
    con.execute("SET enable_progress_bar=false")
    con.execute("SET threads=8")

    tables = find_tables()
    print(f"数据目录：{ROOT}")
    print(f"发现 {len(tables)} 个表目录，开始扫描…\n")

    md: list[str] = []
    md.append("# QuantDB 数据字典（day1 自动生成）")
    md.append(f"\n- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    md.append(f"- 数据目录：`{ROOT}`")
    md.append(f"- 引擎：DuckDB {duckdb.__version__}")
    md.append(f"- 扫描表数：{len(tables)}")
    md.append("\n> 本文件由 `inspect_tables.py` 生成，只含结构（列名/类型/行数）与 3 行样本，不含分析结论。")

    rows_for_csv: list[list] = []

    for cat, tbl, layout in tables:
        name = tbl.name
        cn = CN.get(name, "")
        glob = parquet_glob(tbl, layout)
        t0 = time.time()
        try:
            files = list(tbl.rglob("*.parquet")) if layout == "partition" else list(tbl.glob("*.parquet"))
            size = sum(f.stat().st_size for f in files)

            read = (
                f"read_parquet('{glob}', hive_partitioning = true, union_by_name = true)"
                if layout == "partition"
                else f"read_parquet('{glob}', union_by_name = true)"
            )
            desc = con.execute(f"DESCRIBE SELECT * FROM {read}").df()
            n_rows = con.execute(f"SELECT count(*) AS n FROM {read}").fetchone()[0]
            sample = con.execute(f"SELECT * FROM {read} LIMIT 3").df()

            md.append(f"\n## {name}（{cn}）")
            md.append(f"\n- 类别：`{cat}` ｜ 布局：`{layout}` ｜ 文件数：{len(files)} ｜ 体积：{human(size)}")
            md.append(f"- 行数：**{n_rows:,}** ｜ 列数：**{len(desc)}** ｜ 扫描耗时：{time.time()-t0:.1f}s")
            md.append("\n### 列")
            md.append("")
            md.append(md_table(desc))
            md.append("\n### 前 3 行")
            md.append("")
            md.append(md_table(sample) if len(sample) else "（空表）")

            hints = [(c, UNIT_HINT[c]) for c in desc["column_name"] if c in UNIT_HINT]
            if hints:
                md.append("\n### 单位（来自 skills/quantdb-fields）")
                md.append("")
                for c, h in hints:
                    if h:
                        md.append(f"- `{c}`：{h}")

            rows_for_csv.append([cat, name, cn, layout, len(files), human(size), n_rows, len(desc)])
            print(f"  ✓ {cat}/{name}: {n_rows:,} 行 × {len(desc)} 列 ({time.time()-t0:.1f}s)")

        except Exception as e:
            md.append(f"\n## {name}（{cn}）")
            md.append(f"\n- 类别：`{cat}` ｜ 布局：`{layout}`")
            md.append(f"\n**读取失败**：`{e}`")
            rows_for_csv.append([cat, name, cn, layout, 0, "0", -1, -1])
            print(f"  ✗ {cat}/{name}: {e}")
            traceback.print_exc(limit=1)

    md.append("\n---\n")
    md.append("## 总览")
    md.append("")
    md.append("| 类别 | 表 | 中文 | 布局 | 文件数 | 体积 | 行数 | 列数 |")
    md.append("|---|---|---|---|---|---|---|---|")
    for r in rows_for_csv:
        md.append("| " + " | ".join(str(x) for x in r) + " |")

    MD.write_text("\n".join(md) + "\n", encoding="utf-8")

    with CSV.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["类别", "表", "中文", "布局", "文件数", "体积", "行数", "列数"])
        w.writerows(rows_for_csv)

    print(f"\n[写入] {MD}")
    print(f"[写入] {CSV}")
    print(f"共 {len(rows_for_csv)} 张表，失败 {sum(1 for r in rows_for_csv if r[6] < 0)} 张")
    return 0


if __name__ == "__main__":
    sys.exit(main())
