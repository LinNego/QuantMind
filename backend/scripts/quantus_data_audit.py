#!/usr/bin/env python3
"""QuantUS 快照数据体检 / 受控修复工具。

用途（2026-10-15 数据事故后建立）：
  - **体检（默认）**：逐个校验消费方实际读取的那一份快照，报告
    「退化文件数」（上游降级窗口写出的空载荷）与「陈旧天数」。
  - **修复（--repair）**：对体检不合格的字段重拉全市场快照，写后回读复验，
    残留以非零退出码上报。幂等可重跑。

退化判据与写入侧闸门同源（`global_market_sync._is_degraded_snapshot`）：
信号列全空/空串 = 该文件为降级载荷，消费方（市场分析/个股终端）读到的是废数据。

用法:
  python backend/scripts/quantus_data_audit.py --market US                # 只体检
  python backend/scripts/quantus_data_audit.py --market US --repair       # 体检+修复+复验
  python backend/scripts/quantus_data_audit.py --market US --repair --fields sector,f10
  python backend/scripts/quantus_data_audit.py --market US --json         # JSON 输出
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.scripts.global_market_sync import (  # noqa: E402
    SNAPSHOT_FIELDS,
    _data_dir,
    _is_degraded_snapshot,
    _resolve_symbols,
    sync_snapshot,
)

STALE_DAYS_WARN = 3  # 快照陈旧告警阈值（自然日）

# 体检字段 = 写入侧登记的全部快照字段（valuation 为分区格式，单独处理）
_AUDIT_FIELDS = [f for f in SNAPSHOT_FIELDS if f != "valuation"]


def _field_dir(market: str, field: str) -> Path:
    return _data_dir(market) / SNAPSHOT_FIELDS[field]


def _audit_symbol_field(market: str, field: str) -> dict:
    """逐文件体检一个「每股一文件」快照字段。"""
    root = _field_dir(market, field)
    files = sorted(root.glob("*.parquet")) if root.is_dir() else []
    degraded: list[str] = []
    newest = 0.0
    for f in files:
        try:
            df = pd.read_parquet(f)
        except Exception:  # noqa: BLE001 — 读不出即视为坏文件
            degraded.append(f.stem)
            continue
        if _is_degraded_snapshot(field, df):
            degraded.append(f.stem)
        newest = max(newest, f.stat().st_mtime)
    stale_days = (
        (time.time() - newest) / 86400 if newest else None
    )
    return {
        "field": field,
        "files": len(files),
        "degraded": len(degraded),
        "degraded_samples": degraded[:5],
        "fresh_days": round(stale_days, 1) if stale_days is not None else None,
    }


def _audit_valuation(market: str) -> dict:
    """估值分区体检：末分区日期 + 核心列覆盖率。"""
    root = _data_dir(market) / SNAPSHOT_FIELDS["valuation"]
    parts = sorted(
        os.path.basename(p).replace("dt=", "")
        for p in glob.glob(str(root / "dt=*"))
    )
    if not parts:
        return {"field": "valuation", "partitions": 0, "latest": None}
    latest = parts[-1]
    f = root / f"dt={latest}" / "data.parquet"
    rows = pe_ok = mv_ok = 0
    try:
        df = pd.read_parquet(f, columns=["pe_ttm", "total_mv"])
        rows = len(df)
        pe_ok = int(df["pe_ttm"].notna().sum())
        mv_ok = int(df["total_mv"].notna().sum())
    except Exception:  # noqa: BLE001
        pass
    return {
        "field": "valuation",
        "partitions": len(parts),
        "latest": latest,
        "latest_rows": rows,
        "latest_pe_ttm": pe_ok,
        "latest_total_mv": mv_ok,
        # 末分区两列都空 = 末分区是废分区
        "degraded": 1 if rows and pe_ok == 0 and mv_ok == 0 else 0,
    }


def audit(market: str) -> dict:
    """全体检：快照字段 + 估值分区 + sector 分类覆盖。"""
    report: dict = {
        "market": market,
        "audited_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fields": [],
    }
    for field in _AUDIT_FIELDS:
        report["fields"].append(_audit_symbol_field(market, field))
    report["valuation"] = _audit_valuation(market)

    # sector 分类覆盖（消费方视角：非空 sector 的标的数，正常应为 11 类 / ~487 只）
    sroot = _data_dir(market) / "2_base_sector" / "sector"
    sectors: set[str] = set()
    nonsector = 0
    for f in sroot.glob("*.parquet") if sroot.is_dir() else []:
        try:
            df = pd.read_parquet(f, columns=["sector"])
        except Exception:  # noqa: BLE001
            continue
        vals = {str(v).strip() for v in df["sector"].dropna() if str(v).strip()}
        if vals:
            sectors |= vals
            nonsector += 1
    report["sector_classes"] = len(sectors)
    report["sector_covered"] = nonsector

    problems = [
        f["field"]
        for f in report["fields"]
        if f["degraded"] > 0
        or (f["fresh_days"] is not None and f["fresh_days"] > STALE_DAYS_WARN)
    ]
    if report["valuation"].get("degraded"):
        problems.append("valuation")
    if report.get("sector_classes", 0) <= 1:
        problems.append("sector")
    report["problems"] = sorted(set(problems))
    return report


def _print_report(report: dict) -> None:
    print(f"== QuantUS 快照体检 [{report['market']}] {report['audited_at']}")
    print(f"{'field':<22}{'files':>6}{'degraded':>9}{'fresh_days':>11}")
    for f in report["fields"]:
        fresh = "-" if f["fresh_days"] is None else f"{f['fresh_days']}"
        flag = " ❌" if f["degraded"] else ""
        print(f"{f['field']:<22}{f['files']:>6}{f['degraded']:>9}{fresh:>11}{flag}")
    v = report["valuation"]
    print(
        f"{'valuation':<22}{v.get('latest_rows', 0):>6}"
        f"{v.get('latest_pe_ttm', 0):>9}  latest={v.get('latest')}"
        f" partitions={v.get('partitions')}"
    )
    print(
        f"sector: {report['sector_classes']} 类覆盖 {report['sector_covered']} 只"
    )
    print(f"问题字段: {report['problems'] or '无 ✅'}")


def repair(market: str, fields: list[str], verbose: bool = True) -> dict:
    """对指定字段受控重拉（写后回读复验）。"""
    syms = _resolve_symbols(market, None)
    print(f"[repair] 标的池 {len(syms)} 只，字段: {fields}")
    results: dict = {}
    for i, field in enumerate(fields):
        t0 = time.time()
        try:
            r = sync_snapshot(market, syms, field)
        except Exception as exc:  # noqa: BLE001
            r = {"field": field, "error": str(exc)[:200]}
        r["seconds"] = round(time.time() - t0, 1)
        results[field] = r
        print(f"[repair] {field}: {json.dumps(r, ensure_ascii=False)}", flush=True)
        if i < len(fields) - 1:
            time.sleep(8)  # 字段间冷却，防连续高频请求触发限流
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="QuantUS 快照体检/修复")
    parser.add_argument("--market", default="US", choices=["US", "HK"])
    parser.add_argument("--repair", action="store_true", help="对问题字段重拉")
    parser.add_argument(
        "--fields", default=None, help="逗号分隔，显式指定要修复的字段"
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON 报告")
    args = parser.parse_args()

    before = audit(args.market)
    if not args.json:
        _print_report(before)

    if not args.repair:
        if args.json:
            print(json.dumps(before, ensure_ascii=False, indent=2, default=str))
        return 0

    fields = (
        [f.strip() for f in args.fields.split(",") if f.strip()]
        if args.fields
        else before["problems"]
    )
    if not fields:
        print("[repair] 无需修复 ✅")
        return 0
    repair(args.market, fields)

    # 写后回读复验（修复纪律 #3：复验落盘文件，不是内存计划）
    after = audit(args.market)
    if not args.json:
        print()
        _print_report(after)
    else:
        print(
            json.dumps(
                {"before": before, "after": after}, ensure_ascii=False, indent=2,
                default=str,
            )
        )
    if after["problems"]:
        print(f"[repair] ❌ 残留问题字段: {after['problems']}")
        return 1
    print("[repair] ✅ 全部字段复验通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
