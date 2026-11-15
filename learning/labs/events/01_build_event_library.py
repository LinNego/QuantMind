#!/usr/bin/env python3
"""第 1 步：把监管事件整理成「标准事件库」。

输入：data/regulatory/events.parquet（29,798 条原始公告）
输出：data/events/event_library.parquet（结构化、去重、带事件强度）

标准字段：
    event_id      事件唯一 ID
    symbol        股票代码（后缀式）
    event_date    事件日（公告日，YYYYMMDD）
    category      事件类型
    title         原始标题
    is_first      是否为「该股该类事件」的首次（365 日内）
    days_since    距上次同类事件的天数（首次为 NaN）

为什么要 is_first：一个股票连续发 10 条「立案进展」，那是同一件事的后续，
不是 10 个独立事件。不去重会把统计结果严重放大。
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "data/regulatory/events.parquet"
OUT = ROOT / "data/events/event_library.parquet"

CATEGORY_NAME = {
    "investigation": "立案调查",
    "penalty": "监管处罚",
    "inquiry": "交易所问询",
    "delist_risk": "退市风险",
}


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)

    df = pd.read_parquet(SRC)
    print(f"原始事件：{len(df):,} 条")
    print(df.category.value_counts().to_string())

    # 1) 基础清洗
    df = df.dropna(subset=["symbol", "announce_date", "category"]).copy()
    df["event_date"] = df["announce_date"].astype(int)
    df["title"] = df.get("title", "").fillna("")
    df["category_name"] = df["category"].map(CATEGORY_NAME).fillna(df["category"])

    # 2) 同一股票 + 同一天 + 同类 → 合并（多条公告算一件事）
    df = df.sort_values(["symbol", "category", "event_date"])
    df = df.drop_duplicates(subset=["symbol", "category", "event_date"], keep="first")

    # 3) 计算「距上次同类事件的天数」和「是否首次」
    d = pd.to_datetime(df["event_date"].astype(str), format="%Y%m%d")
    df["_dt"] = d
    g = df.groupby(["symbol", "category"])["_dt"]
    prev = g.shift(1)
    df["days_since"] = (d - prev).dt.days
    # 365 天内发生过同类事件 → 不算首次
    df["is_first"] = df["days_since"].isna() | (df["days_since"] > 365)

    # 4) 生成 event_id
    df = df.reset_index(drop=True)
    df["event_id"] = (
        df["symbol"].str.replace(".", "", regex=False)
        + "_" + df["event_date"].astype(str)
        + "_" + df["category"].str[:4]
    )

    out = df[[
        "event_id", "symbol", "event_date", "category", "category_name",
        "title", "is_first", "days_since",
    ]].sort_values(["event_date", "symbol"])

    out.to_parquet(OUT, index=False)

    print(f"\n事件库：{len(out):,} 条 → {OUT}")
    print("\n按类型（全部 / 首次）：")
    stat = pd.DataFrame({
        "全部": out.groupby("category_name").size(),
        "首次": out[out.is_first].groupby("category_name").size(),
    })
    print(stat.to_string())

    print(f"\n时间跨度：{out.event_date.min()} ~ {out.event_date.max()}")
    print(f"涉及股票：{out.symbol.nunique():,} 只")
    print("\n前 5 条：")
    print(out.head(5)[["event_date", "symbol", "category_name", "title"]].to_string(index=False))


if __name__ == "__main__":
    main()
