#!/usr/bin/env python3
"""K0-a 幸存者偏差审计：逐数据集核对退市股覆盖。

判据（细案 §3.5）：退市样本缺失率 >10% **且无法量化** → 暂停。本脚本把
「无法量化」变成可复核数字：以 ``delisted_raw/<symbol>_raw.parquet`` 中
baostock 的真实交易日为分母，统计每个数据集在每个交易日应出现/实际出现的
退市股股日。

输出
----
``<out>/k0a_survivorship.json``  机器可读（逐数据集 + by_year + worst_stocks）
``<out>/K0a_survivorship.md``    人读摘要

用法::

    python3 backend/scripts/factor_selection_v2_audit.py \
        --data-root /data/quantdb \
        --start 2016-01-04 --end 2024-12-31
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from pathlib import Path

import pandas as pd

log = logging.getLogger("factor_selection_v2_audit")

BASE_DATASETS: tuple[str, ...] = (
    "1_kline_data/daily_unadjusted",
    "1_kline_data/daily_forward",
    "1_kline_data/daily_backward",
    "5_technical_derived/valuation",
)
EXCLUDED_LIBRARIES = {"alpha_library_labels", "l1_l2_factors"}
WORST_STOCKS_LIMIT = 20
K0A_MISSING_THRESHOLD = 0.10


def normalize_date(value: object) -> str | None:
    """``2016-01-04`` / ``20160104`` / Timestamp → ``20160104``。"""
    if value is None:
        return None
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y%m%d")
    text = str(value).strip().replace("-", "")
    if not text:
        return None
    return text[:8]


def load_delisted_histories(cache_dir: str | Path) -> dict[str, set[str]]:
    """读取 ``delisted_raw/<symbol>_raw.parquet`` → {symbol: {交易日}}。"""
    root = Path(cache_dir)
    histories: dict[str, set[str]] = {}
    for path in sorted(root.glob("*_raw.parquet")):
        symbol = path.name.removesuffix("_raw.parquet")
        frame = pd.read_parquet(path, columns=["symbol", "time"])
        if frame.empty:
            continue
        dates = {
            dt for dt in (normalize_date(v) for v in frame["time"]) if dt is not None
        }
        if dates:
            histories[symbol] = dates
    return histories


def expected_delisted_by_date(
    histories: Mapping[str, Iterable[str]],
) -> dict[str, set[str]]:
    """反向索引：{交易日: {该日有真实交易的退市股}}。"""
    expected: dict[str, set[str]] = defaultdict(set)
    for symbol, dates in histories.items():
        for dt in dates:
            normalized = normalize_date(dt)
            if normalized:
                expected[normalized].add(str(symbol))
    return dict(expected)


def _partition_dirs(root: Path, parts: str | Path, start: str | None, end: str | None):
    base = root / parts
    if not base.is_dir():
        return []
    out = []
    for path in sorted(base.glob("dt=*")):
        if not path.is_dir():
            continue
        dt = normalize_date(path.name[3:])
        if dt is None or (start and dt < start) or (end and dt > end):
            continue
        if (path / "data.parquet").exists():
            out.append((dt, path / "data.parquet"))
    return out


def audit_dataset(
    data_root: str | Path,
    parts: str | Path,
    histories: Mapping[str, Iterable[str]],
    *,
    start: str | None = None,
    end: str | None = None,
) -> dict:
    """单个数据集的分区级退市股覆盖统计。"""
    root = Path(data_root)
    start_dt = normalize_date(start)
    end_dt = normalize_date(end)
    expected_by_date = expected_delisted_by_date(histories)

    partitions = 0
    rows_total = 0
    symbols_seen: set[str] = set()
    expected_days: dict[str, int] = defaultdict(int)
    present_days: dict[str, int] = defaultdict(int)
    by_year: dict[str, dict[str, int]] = defaultdict(
        lambda: {"expected": 0, "present": 0}
    )

    for dt, path in _partition_dirs(root, parts, start_dt, end_dt):
        partitions += 1
        frame = pd.read_parquet(path, columns=["symbol"])
        symbols = set(frame["symbol"].astype(str))
        rows_total += len(frame)
        symbols_seen |= symbols

        expected = expected_by_date.get(dt, set())
        present = symbols & expected
        year = dt[:4]
        by_year[year]["expected"] += len(expected)
        by_year[year]["present"] += len(present)
        for symbol in expected:
            expected_days[symbol] += 1
        for symbol in present:
            present_days[symbol] += 1

    expected_total = sum(expected_days.values())
    present_total = sum(present_days.values())
    missing_total = expected_total - present_total
    missing_rate = (missing_total / expected_total) if expected_total else None

    worst = []
    for symbol, expected in expected_days.items():
        present = present_days.get(symbol, 0)
        worst.append(
            {
                "symbol": symbol,
                "expected_days": expected,
                "present_days": present,
                "missing_days": expected - present,
                "coverage": present / expected if expected else None,
            }
        )
    worst.sort(key=lambda item: (-item["missing_days"], item["symbol"]))

    stock_coverage = [
        item["coverage"] for item in worst if item["coverage"] is not None
    ]
    year_payload = {}
    for year, values in sorted(by_year.items()):
        expected = values["expected"]
        present = values["present"]
        year_payload[year] = {
            "expected_stock_days": expected,
            "present_stock_days": present,
            "missing_rate": (expected - present) / expected if expected else None,
        }

    entry = {
        "dataset": str(parts),
        "partitions": partitions,
        "rows_total": rows_total,
        "symbols_total": len(symbols_seen),
        "delisted_expected_stock_days": expected_total,
        "delisted_present_stock_days": present_total,
        "delisted_missing_stock_days": missing_total,
        "delisted_missing_rate": missing_rate,
        "delisted_stocks_expected": len(expected_days),
        "delisted_stocks_captured": sum(1 for v in present_days.values() if v > 0),
        "stock_coverage_mean": (
            sum(stock_coverage) / len(stock_coverage) if stock_coverage else None
        ),
        "stock_coverage_min": min(stock_coverage) if stock_coverage else None,
        "by_year": year_payload,
        "worst_stocks": worst[:WORST_STOCKS_LIMIT],
    }
    entry["k0a"] = k0a_verdict(entry)
    return entry


def k0a_verdict(
    entry: Mapping[str, object], threshold: float = K0A_MISSING_THRESHOLD
) -> dict:
    """可量化 + 缺失率 ≤ 阈值 → PASS；无退市样本可审 → PASS（记为空集）。"""
    expected = int(entry.get("delisted_expected_stock_days") or 0)
    rate = entry.get("delisted_missing_rate")
    quantifiable = expected > 0 and rate is not None
    if expected == 0:
        return {
            "quantifiable": True,
            "missing_rate": None,
            "threshold": threshold,
            "pass": True,
        }
    return {
        "quantifiable": bool(quantifiable),
        "missing_rate": rate,
        "threshold": threshold,
        "pass": bool(quantifiable and float(rate) <= threshold),
    }


def discover_factor_datasets(data_root: str | Path) -> tuple[str, ...]:
    """``6_ml_datasets`` 下有 dt 分区、非 staging/标签的因子库目录。"""
    base = Path(data_root) / "6_ml_datasets"
    if not base.is_dir():
        return ()
    found = []
    for path in sorted(base.iterdir()):
        if not path.is_dir() or path.name.startswith("_"):
            continue
        if path.name in EXCLUDED_LIBRARIES:
            continue
        if any((child / "data.parquet").exists() for child in path.glob("dt=*")):
            found.append(str(path.relative_to(Path(data_root))))
    return tuple(found)


def audit_all(
    data_root: str | Path,
    histories: Mapping[str, Iterable[str]],
    *,
    datasets: Sequence[str] | None = None,
    start: str | None = None,
    end: str | None = None,
    threshold: float = K0A_MISSING_THRESHOLD,
) -> dict:
    """逐数据集审计并汇总。``datasets=None`` = 基础表 + 全部因子库。"""
    root = Path(data_root)
    selected = (
        tuple(datasets) if datasets else BASE_DATASETS + discover_factor_datasets(root)
    )
    results: dict[str, dict] = {}
    for parts in selected:
        entry = audit_dataset(root, parts, histories, start=start, end=end)
        entry["k0a"] = k0a_verdict(entry, threshold)
        results[parts] = entry

    expected = sum(v["delisted_expected_stock_days"] for v in results.values())
    present = sum(v["delisted_present_stock_days"] for v in results.values())
    failed = [name for name, v in results.items() if not v["k0a"]["pass"]]
    overall = {
        "datasets": len(results),
        "delisted_expected_stock_days": expected,
        "delisted_present_stock_days": present,
        "delisted_missing_stock_days": expected - present,
        "delisted_missing_rate": (expected - present) / expected if expected else None,
        "threshold": threshold,
        "failed_datasets": failed,
        "pass": not failed,
    }
    return {
        "window": {"start": normalize_date(start), "end": normalize_date(end)},
        "histories": {
            "stocks": len(histories),
            "stock_days": sum(len(v) for v in histories.values()),
        },
        "datasets": results,
        "overall": overall,
    }


def _fmt_rate(value: object) -> str:
    return "—" if value is None else f"{float(value):.2%}"


def write_report(
    result: Mapping[str, object], out_dir: str | Path
) -> tuple[Path, Path]:
    root = Path(out_dir)
    root.mkdir(parents=True, exist_ok=True)
    json_path = root / "k0a_survivorship.json"
    md_path = root / "K0a_survivorship.md"
    json_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    window = result.get("window", {})
    lines = [
        "# K0-a 幸存者偏差审计",
        "",
        f"- 窗口：{window.get('start') or '全部'} .. {window.get('end') or '全部'}",
        f"- 退市股：{result['histories']['stocks']} 只 / {result['histories']['stock_days']} 股日",
        f"- 判定阈值：缺失率 ≤ {_fmt_rate(result['overall']['threshold'])}",
        "",
        "| 数据集 | 分区 | 退市应有股日 | 实有股日 | 缺失率 | 判定 |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for name, entry in result["datasets"].items():
        verdict = entry["k0a"]
        lines.append(
            f"| {name} | {entry['partitions']} | "
            f"{entry['delisted_expected_stock_days']} | "
            f"{entry['delisted_present_stock_days']} | "
            f"{_fmt_rate(entry['delisted_missing_rate'])} | "
            f"{'PASS' if verdict['pass'] else 'FAIL'} |"
        )
    overall = result["overall"]
    lines += [
        "",
        f"**总体：{'PASS' if overall['pass'] else 'FAIL'}**"
        f"（缺失率 {_fmt_rate(overall['delisted_missing_rate'])}，"
        f"失败数据集 {len(overall['failed_datasets'])} 个）",
        "",
    ]
    if overall["failed_datasets"]:
        lines.append("失败数据集：" + "、".join(overall["failed_datasets"]))
        lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, md_path


def _parse_datasets(raw: str | None) -> tuple[str, ...] | None:
    if not raw:
        return None
    values = tuple(part.strip() for part in raw.split(",") if part.strip())
    return values or None


def default_data_root() -> Path:
    return Path(os.environ.get("QM_QUANTDB_DATA_DIR", "/data/quantdb"))


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    parser = argparse.ArgumentParser(description="K0-a 幸存者偏差审计")
    parser.add_argument("--data-root", default=str(default_data_root()))
    parser.add_argument("--cache-dir", default=None, help="delisted_raw 缓存目录")
    parser.add_argument("--start", default="2016-01-04")
    parser.add_argument("--end", default=date.today().isoformat())
    parser.add_argument(
        "--datasets", default=None, help="逗号分隔；默认基础表 + 全部因子库"
    )
    parser.add_argument("--out", default=None)
    parser.add_argument("--threshold", type=float, default=K0A_MISSING_THRESHOLD)
    args = parser.parse_args(argv)

    root = Path(args.data_root)
    cache = (
        Path(args.cache_dir)
        if args.cache_dir
        else (root / "factor_research" / "selection_v2_20261021" / "delisted_raw")
    )
    if not cache.is_dir():
        raise SystemExit(f"delisted_raw 缓存不存在: {cache}（先跑退市股回补）")
    histories = load_delisted_histories(cache)
    if not histories:
        raise SystemExit(f"delisted_raw 缓存为空: {cache}")

    out = (
        Path(args.out)
        if args.out
        else (root / "factor_research" / "selection_v2_20261021" / "audit")
    )
    result = audit_all(
        root,
        histories,
        datasets=_parse_datasets(args.datasets),
        start=args.start,
        end=args.end,
        threshold=args.threshold,
    )
    json_path, md_path = write_report(result, out)
    log.info(
        "K0-a %s：缺失率 %s；失败数据集 %d 个；报告 %s",
        "PASS" if result["overall"]["pass"] else "FAIL",
        _fmt_rate(result["overall"]["delisted_missing_rate"]),
        len(result["overall"]["failed_datasets"]),
        md_path,
    )
    print(json.dumps(result["overall"], ensure_ascii=False, indent=2))
    print(f"json: {json_path}")
    print(f"markdown: {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
