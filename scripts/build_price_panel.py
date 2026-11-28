#!/usr/bin/env python3
"""重建大文件价格面板（不入库，放 refdata_big/）

从 quantdb 的日线宽表拼成「日期 × 股票」矩阵，供研究脚本使用：
    close_daily_backward.parquet   后复权收盘（全 A，10.55M 行）
    open/high/low_adj.parquet      后复权开高低
    amt.parquet                    成交额（千元）

耗时约 3-5 分钟，产出约 350MB，所以放在 refdata_big/（.gitignore 忽略）。

用法：
    python scripts/build_price_panel.py
"""
from __future__ import annotations

import argparse
import glob
import os
from pathlib import Path

import pandas as pd

DB = Path("/opt/quantmind/data/quantdb/1_kline_data")
OUT = Path(__file__).resolve().parent.parent / "refdata_big"


def build(kind: str, fields: list[str], force: bool = False) -> None:
    OUT.mkdir(exist_ok=True)
    files = sorted(glob.glob(str(DB / kind / "dt=*" / "data.parquet")))
    print(f"{kind}: {len(files)} 个交易日文件")
    frames = {f: [] for f in fields}
    for i, f in enumerate(files):
        d = pd.read_parquet(f, columns=["symbol", "time"] + fields)
        d["time"] = pd.to_datetime(d["time"])
        for fld in fields:
            frames[fld].append(d[["symbol", "time", fld]])
        if (i + 1) % 500 == 0:
            print(f"  已读 {i + 1}/{len(files)}")
    names = {"close": "close_daily_backward", "open": "open_adj",
             "high": "high_adj", "low": "low_adj", "amount": "amt"}
    for fld, parts in frames.items():
        path = OUT / f"{names[fld]}.parquet"
        if path.exists() and not force:
            print(f"  跳过 {path.name}（已存在）")
            continue
        x = pd.concat(parts, ignore_index=True)
        panel = x.pivot_table(index="time", columns="symbol", values=fld).sort_index()
        panel.to_parquet(path)
        print(f"  {path.name}  {panel.shape}  {path.stat().st_size / 1e6:.0f}MB")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--kind", default="daily_backward", help="daily_backward / daily_unadjusted")
    args = ap.parse_args()
    build(args.kind, ["close", "open", "high", "low", "amount"], args.force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
