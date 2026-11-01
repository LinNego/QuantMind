#!/usr/bin/env python3
"""抓取 A 股「监管风险事件」公告，落成可回测的事件表。

为什么要单独建这张表
--------------------
平台库里只有 `news_article_enrichment`（Huntly RSS → 情绪/事件标签），
它**只覆盖平台运行期间抓到的新闻**，做不了 10 年回溯；而且新闻不是公告，
「收到立案告知书」这类硬事件在公告里才是最全的。

本脚本走**公告**：`akshare.stock_notice_report`（东方财富公告库，按日全市场），
按标题关键词归类，产出带**披露日期**的事件表——披露日就是信息可见日，
回测里只能在 T 日之后使用，不构成未来函数。

事件分类（对应 `backend/services/api/news/report_match.py` 的 EVENT_TAG_RULES）
------------------------------------------------------------------------------
    investigation  立案调查   立案 / 立案调查 / 立案告知
    penalty        监管处罚   处罚 / 警示函 / 监管措施 / 通报批评 / 公开谴责 / 违规
    inquiry        问询函     问询函（剔除「回复」类）
    delist_risk    退市风险   退市风险 / 终止上市 / 面值退市

产出
----
    data/regulatory/events.parquet
        symbol / announce_date / category / title

用法
----
    python scripts/fetch_regulatory_events.py --start 2023-01-01 --end 2026-10-23
    python scripts/fetch_regulatory_events.py --start 2016-01-01   # 全量（约 15 分钟）
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import pandas as pd

OUT = Path("data/regulatory")

RULES = [
    ("investigation", ("立案调查", "立案告知", "立案")),
    (
        "penalty",
        ("行政处罚", "警示函", "监管措施", "通报批评", "公开谴责", "纪律处分", "违规"),
    ),
    ("inquiry", ("问询函",)),
    ("delist_risk", ("退市风险", "终止上市", "面值退市")),
]
# 标题里出现这些词的多为「回复/IPO 审核」，不是新增风险事件
NOISE = ("回复", "审核问询", "审查问询", "保荐", "问核", "核查意见", "法律意见书")


def classify(title: str) -> str | None:
    if not title or any(n in title for n in NOISE):
        return None
    for cat, kws in RULES:
        if any(k in title for k in kws):
            return cat
    return None


def to_symbol(code: str) -> str | None:
    c = str(code).strip().zfill(6)
    if not c.isdigit() or len(c) != 6:
        return None
    if c[0] == "6":
        return f"{c}.SH"
    if c[0] in "03":
        return f"{c}.SZ"
    if c[:2] in ("43", "83", "87", "88", "92"):
        return f"{c}.BJ"
    return None


def trading_days(con, start: str, end: str) -> list[str]:
    rows = con.execute(
        "SELECT DISTINCT dt FROM kb WHERE dt >= ? AND dt <= ? ORDER BY dt",
        [int(start.replace("-", "")), int(end.replace("-", ""))],
    ).fetchall()
    return [str(r[0]) for r in rows]


def main() -> int:
    ap = argparse.ArgumentParser(description="抓取监管风险事件公告")
    ap.add_argument("--start", default="2016-01-01")
    ap.add_argument("--end", default="2026-10-23")
    ap.add_argument("--data-dir", default="/opt/quantmind/data/quantdb")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()

    import akshare as ak
    import duckdb

    con = duckdb.connect()
    con.execute(
        "CREATE VIEW kb AS SELECT dt FROM read_parquet("
        f"'{args.data_dir}/1_kline_data/daily_backward/**/*.parquet')"
    )
    days = trading_days(con, args.start, args.end)
    print(f"待扫描交易日 {len(days)} 个：{days[0]} ~ {days[-1]}")

    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / "events.parquet"
    done: set[str] = set()
    frames = []
    if path.exists():  # 断点续跑
        old = pd.read_parquet(path)
        frames.append(old)
        done = set(old.announce_date.astype(str).unique())
        print(f"已有 {len(old)} 条事件，跳过 {len(done)} 个交易日")

    try:
        for i, d in enumerate(days, 1):
            if d in done:
                continue
            try:
                df = ak.stock_notice_report(symbol="全部", date=d)
            except Exception as exc:  # noqa: BLE001 - 单日失败不应中断整轮
                print(f"  {d} 拉取失败：{type(exc).__name__}，跳过")
                time.sleep(1.0)
                continue
            if df is None or df.empty:
                continue
            df["category"] = df["公告标题"].map(classify)
            hit = df[df.category.notna()].copy()
            if hit.empty:
                continue
            hit["symbol"] = hit["代码"].map(to_symbol)
            hit = hit.dropna(subset=["symbol"])
            hit["announce_date"] = d
            frames.append(
                hit[["symbol", "announce_date", "category", "公告标题"]].rename(
                    columns={"公告标题": "title"}
                )
            )
            if i % 25 == 0:
                tmp = pd.concat(frames, ignore_index=True).drop_duplicates(
                    ["symbol", "announce_date", "category", "title"]
                )
                tmp.to_parquet(path, index=False)
                print(f"  {i}/{len(days)} 天，累计 {len(tmp)} 条", flush=True)
    finally:
        if frames:
            allx = pd.concat(frames, ignore_index=True).drop_duplicates(
                ["symbol", "announce_date", "category", "title"]
            )
            allx.to_parquet(path, index=False)
            print(
                f"事件表已写入 {path}：{len(allx)} 条 / "
                f"{allx.symbol.nunique()} 只 / {allx.announce_date.nunique()} 个交易日"
            )
            print(allx.category.value_counts().to_string())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
