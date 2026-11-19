#!/usr/bin/env python3
"""数据与环境自检：确认 plan/n01 每天要用的数据都在、能读、口径对。

用法：
  cd /root/QuantMind && .venv/bin/python plan/n01/tools/env_check.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import duckdb
import pandas as pd

import qm

EXPECTED = {
    "1_kline_data/daily_forward": (2638, "日线前复权"),
    "1_kline_data/daily_backward": (2638, "日线后复权"),
    "1_kline_data/daily_unadjusted": (2638, "日线不复权"),
    "1_kline_data/index_daily": (2638, "指数日线"),
    "2_base_sector": (2647, "基础板块"),
    "3_financial_data": (44363, "财务数据"),
    "6_ml_datasets/l1_factors": (2638, "L1 因子"),
}


def main() -> int:
    print(f"仓库根目录: {qm.ROOT}")
    print(f"数据目录  : {qm.DATA}")
    ok = True

    total = 0
    for rel, (expect, name) in EXPECTED.items():
        n = len(list((qm.DATA / rel).rglob("*.parquet")))
        total += n
        flag = "OK " if n >= expect * 0.9 else "!! "
        if n < expect * 0.9:
            ok = False
        print(f"  {flag}{name:<10} {rel:<38} {n:>6} / {expect}")

    print(f"  文件合计  : {total}")

    # 口径检查：不复权 close*volume/amount 应 ≈ 1e4
    df = qm.kline(adj="unadjusted", start=20261113, end=20261113)
    ratio = (df["close"] * df["volume"] / df["amount"]).median()
    print(f"  A3 口径   : close*volume/amount 中位数 = {ratio:.0f}（预期 ≈ 9500–10100）")
    if not 9000 <= ratio <= 11000:
        ok = False
        print("    !! 口径异常：检查 amount 单位是否仍是万元")

    # L1 因子列数
    l1 = qm.l1(start=20261113, end=20261113)
    print(f"  L1 因子   : {l1.shape[0]} 行 × {l1.shape[1]} 列（预期 ≈ 120 列）")

    # 财报可用时点
    inc = qm.financial("income", "000001.SZ")
    lag = (pd.to_datetime(inc["m_anntime"]) - pd.to_datetime(inc["m_timetag"])).dt.days
    print(f"  财报披露  : 报告期→公告日 中位滞后 {lag.median():.0f} 天（预期 ≈ 27 天）")

    print("结论:", "环境就绪 ✅" if ok else "有缺项，先补数据 ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
