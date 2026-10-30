#!/usr/bin/env python3
"""下载 QuantDB 全量数据（默认排除 L1 / L2 / L1+L2 三张因子表）。

默认行为 = 「L0 + 所有非因子表」，共 23 张，约 10.1 GB：

    1_kline_data      日线 3 套 + 指数（min1/min5/tick 魔搭上没有，跳过）
    2_base_sector     个股详情 / 板块概念 / 指数权重 / 交易日历 / 两融
    3_financial_data  三表 + 股本 + 每股 + 分红 + 股东户数（8 张）
    4_bond_etf        ETF 申赎 + 可转债
    5_technical       估值 / 技术指标 / 市场情绪
    6_ml_datasets     features_daily（不含 l1/l2/l1_l2 三张）

用法（手动执行，在仓库根目录）：

    # 默认落盘目录：/home/quantmind/quantdb（/ 分区只剩十几 G，装不下 L0）

    # 1) 只算不下：看清单、体积、磁盘够不够
    python research/tools/download_quantdb.py --dry-run

    # 2) 真正下载（默认跳过本地已有的表，断点续传）
    python research/tools/download_quantdb.py

    # 3) 只要某几张
    python research/tools/download_quantdb.py --only features_daily

    # 4) 连因子表一起下（L1+L2，约 47 GB，确认磁盘再跑）
    python research/tools/download_quantdb.py --include-factors

设计说明：
    - 远端清单来自魔搭仓库 qusong0627/LightGBM_Alpha300，逐文件带 sha256/size
    - 下载由仓库自带的 modelscope_dataset_sync.init_from_modelscope 完成（断点续传）
    - 默认「按表跳过」：本地目录已有 parquet 就整表跳过，避免状态库按路径区分导致的重复下载
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from collections import defaultdict
from pathlib import Path

# 默认排除：L1/L2/L1+L2 三张因子表（合计 47 GB，占全量 82%）
FACTOR_DATASETS = ["l1_factors", "l2_factors", "l1_l2_factors"]

DEFAULT_DATA_DIR = "/home/quantmind/quantdb"  # 数据落在 /home（/ 分区只剩十几 G）
DEFAULT_PROXY = "http://192.168.23.103:7897"


def ensure_proxy() -> None:
    """没设代理就用默认的（魔搭在国内，直连通常也能通，代理更稳）。"""
    for key in ("https_proxy", "http_proxy", "HTTPS_PROXY", "HTTP_PROXY"):
        if os.getenv(key):
            return
    os.environ["https_proxy"] = DEFAULT_PROXY
    os.environ["http_proxy"] = DEFAULT_PROXY


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} PB"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="下载 QuantDB（默认排除 L1/L2/L1+L2）")
    p.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    p.add_argument("--only", default="", help="只下这些表，逗号分隔")
    p.add_argument("--exclude", default="", help="额外排除这些表，逗号分隔")
    p.add_argument("--include-factors", action="store_true", help="把 L1/L2/L1+L2 也下（约 47 GB）")
    p.add_argument("--no-skip-existing", action="store_true", help="不跳过本地已有的表")
    p.add_argument("--dry-run", action="store_true", help="只打印计划，不下载")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    ensure_proxy()

    data_dir = Path(args.data_dir)
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

    from backend.services.engine.data_platform.modelscope_dataset_sync import (
        list_remote_files,
    )

    # ---------- 1. 确定要哪些表 ----------
    if args.only:
        wanted = {s.strip() for s in args.only.split(",") if s.strip()}
    else:
        wanted = None  # None = 全部

    excluded = {s.strip() for s in args.exclude.split(",") if s.strip()}
    if not args.include_factors:
        excluded |= set(FACTOR_DATASETS)

    # ---------- 2. 枚举远端清单 ----------
    print(f"数据目录：{data_dir}")
    print("枚举魔搭远端清单…")
    remote = list_remote_files()

    agg: dict[str, list[int]] = defaultdict(lambda: [0, 0])  # dataset -> [files, bytes]
    for f in remote:
        if not f.dataset:
            continue
        agg[f.dataset][0] += 1
        agg[f.dataset][1] += f.size

    targets = sorted(agg)
    if wanted is not None:
        unknown = wanted - set(targets)
        if unknown:
            print(f"[错误] 魔搭上没有这些表：{sorted(unknown)}")
            return 2
        targets = [t for t in targets if t in wanted]
    targets = [t for t in targets if t not in excluded]

    if not targets:
        print("[错误] 排除完之后没有要下的表")
        return 2

    # ---------- 3. 打印计划 ----------
    todo: list[str] = []
    skipped: list[str] = []
    need = 0
    print(f"\n{'表':<24}{'文件数':>8}{'体积':>12}  状态")
    print("-" * 62)
    for t in targets:
        n, b = agg[t]
        exists = data_dir / t
        has_data = exists.is_dir() and any(exists.rglob("*.parquet"))
        if has_data and not args.no_skip_existing:
            skipped.append(t)
            state = "已有，跳过"
        else:
            todo.append(t)
            need += b
            state = "待下载"
        print(f"{t:<24}{n:>8}{human(b):>12}  {state}")

    total_files = sum(agg[t][0] for t in todo)
    print("-" * 62)
    print(f"待下载：{len(todo)} 张 / {total_files:,} 个文件 / {human(need)}")
    if skipped:
        print(f"跳过：  {len(skipped)} 张（本地已有）")

    free = shutil.disk_usage(data_dir if data_dir.exists() else data_dir.parent).free
    print(f"磁盘剩余：{human(free)}　需求：{human(need)}")
    if need > free * 0.95:
        print("[警告] 空间不足或余量太小，建议先清理或换 --data-dir")
        if not args.dry_run:
            return 3

    if not todo:
        print("\n没有需要下载的表，收工。")
        return 0
    if args.dry_run:
        print("\n--dry-run：只打印计划，没有下载。去掉该参数即开始。")
        return 0

    # ---------- 4. 下载 ----------
    from backend.services.engine.data_platform.modelscope_dataset_sync import (
        init_from_modelscope,
    )

    os.environ["QM_QUANTDB_DATA_DIR"] = str(data_dir)

    def progress(event: str, **kw):
        if event in ("phase", "dataset_done", "done", "error"):
            print(f"  [{event}] {kw}", flush=True)

    print(f"\n开始下载 {len(todo)} 张表 → {data_dir}\n")
    result = init_from_modelscope(todo, progress_cb=progress)

    print("\n===== 结果 =====")
    ok = 0
    for name, info in sorted(result.get("datasets", {}).items()):
        err = info.get("errors", 0)
        ok += 1
        print(f"  {name:<24} 下载 {info.get('downloaded', 0):>7}  "
              f"跳过 {info.get('skipped', 0):>7}  错误 {err}")
    print(f"\n完成 {ok} 张，用时 {result.get('elapsed_sec', 0):.0f}s，"
          f"共 {human(result.get('downloaded_bytes', 0))}")
    if result.get("error_samples"):
        print("错误样例：")
        for e in result["error_samples"][:5]:
            print("  -", e)
    print("\n校验一次（单文件直查）：")
    print(f"  ls {data_dir}/6_ml_datasets/features_daily | tail -3")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
