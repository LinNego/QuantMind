"""训练日志流留存运维（Redis db4 `qm:training:run:logs:*`）。

**为什么需要**：`append_log` 曾经只设 `maxlen`（限条目数）不设过期，每个训练
run 的 stream 永久驻留。2026-10-23 实测累积 405 个 stream / 201.8MB，把
`quantmind-redis` 顶到 `maxmemory`（noeviction）→ 全实例写拒绝 → 盘中行情快照
写不进去 → 决策轮 `槽位认领失败（本轮不跑）`。生产者侧 TTL 已补
（`TRAINING_LOG_STREAM_TTL_SECONDS`，默认 7 天），存量需本脚本一次性回收。

**纪律**：删除必须先备份、留痕可追溯 —— `prune` 默认 **dry-run**，只有显式
`--apply` 才真删；建议先 `backup` 落 JSONL.gz 再 `prune --apply`。

用法（容器内）：
    python3 backend/scripts/training_stream_retention.py report
    python3 backend/scripts/training_stream_retention.py backup --out /data/redis_backup
    python3 backend/scripts/training_stream_retention.py prune --older-than-days 7
    python3 backend/scripts/training_stream_retention.py prune --older-than-days 7 --apply
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import gzip
import json
import os
import re
import sys

import redis

PREFIX = "qm:training:run:logs:"
DB = 4
BATCH = 1000
_RUN_DATE = re.compile(r"train_(\d{8})")


def _client() -> redis.Redis:
    return redis.Redis(
        host=os.getenv("REDIS_HOST", "redis"),
        port=int(os.getenv("REDIS_PORT", "6379")),
        password=os.getenv("REDIS_PASSWORD") or None,
        db=DB,
        decode_responses=True,
    )


def _keys(client: redis.Redis) -> list[str]:
    return sorted(client.scan_iter(f"{PREFIX}*", count=BATCH))


def _run_date(key: str) -> dt.date | None:
    m = _RUN_DATE.search(key)
    if not m:
        return None
    try:
        return dt.datetime.strptime(m.group(1), "%Y%m%d").date()
    except ValueError:
        return None


def cmd_report(args: argparse.Namespace) -> int:
    client = _client()
    by_day: collections.Counter[str] = collections.Counter()
    mem_by_day: collections.Counter[str] = collections.Counter()
    no_ttl = 0
    for key in _keys(client):
        day = _run_date(key)
        label = day.isoformat() if day else "unknown"
        by_day[label] += 1
        mem_by_day[label] += client.memory_usage(key) or 0
        if client.ttl(key) == -1:
            no_ttl += 1
    total_mb = sum(mem_by_day.values()) / 1048576
    print(f"合计 {sum(by_day.values())} 键 / {total_mb:.1f}MB，无 TTL {no_ttl} 键")
    print(f"{'日期':<12}{'键数':>6}{'MB':>10}")
    for label in sorted(by_day):
        print(f"{label:<12}{by_day[label]:>6}{mem_by_day[label] / 1048576:>10.1f}")
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    client = _client()
    keys = _keys(client)
    if not keys:
        print("没有可备份的键")
        return 0
    os.makedirs(args.out, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(args.out, f"training_log_streams_{stamp}.jsonl.gz")

    total_entries = 0
    with gzip.open(path, "wt", encoding="utf-8", compresslevel=6) as fh:
        for key in keys:
            entries: list[list] = []
            start = "-"
            while True:
                chunk = client.xrange(key, min=start, max="+", count=BATCH)
                if not chunk:
                    break
                entries.extend([eid, fields] for eid, fields in chunk)
                last_id = chunk[-1][0]
                if last_id == start:
                    break
                start = f"({last_id}"  # 开区间，避免重复取同一条
            total_entries += len(entries)
            fh.write(
                json.dumps(
                    {"key": key, "ttl": client.ttl(key), "entries": entries},
                    ensure_ascii=False,
                )
                + "\n"
            )

    size_mb = os.path.getsize(path) / 1048576
    print(
        f"备份完成: keys={len(keys)} entries={total_entries} file={path} size={size_mb:.1f}MB"
    )
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    client = _client()
    cutoff = dt.date.today() - dt.timedelta(days=int(args.older_than_days))
    victims: list[str] = []
    freed = 0
    for key in _keys(client):
        day = _run_date(key)
        # 无法解析日期的键不动（宁可留着，也不误删）
        if day is None or day >= cutoff:
            continue
        victims.append(key)
        freed += client.memory_usage(key) or 0

    print(
        f"cutoff={cutoff.isoformat()} 待删 {len(victims)} 键 / {freed / 1048576:.1f}MB"
        f"（{'APPLY' if args.apply else 'DRY-RUN'}）"
    )
    if not args.apply:
        for key in victims[:10]:
            print(f"  将删除 {key}")
        if len(victims) > 10:
            print(f"  … 其余 {len(victims) - 10} 键")
        print("未执行删除；确认后加 --apply")
        return 0

    deleted = 0
    for i in range(0, len(victims), 200):
        batch = victims[i : i + 200]
        client.delete(*batch)
        deleted += len(batch)
        print(f"  已删 {deleted}/{len(victims)}", flush=True)
    print(f"删除完成: {deleted} 键，预计释放 {freed / 1048576:.1f}MB")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("report", help="按日期汇总键数与内存").set_defaults(func=cmd_report)

    p_backup = sub.add_parser("backup", help="导出 JSONL.gz（删除前留痕）")
    p_backup.add_argument("--out", default="/data/redis_backup")
    p_backup.set_defaults(func=cmd_backup)

    p_prune = sub.add_parser("prune", help="删除早于 N 天的 stream（默认 dry-run）")
    p_prune.add_argument("--older-than-days", type=int, default=7)
    p_prune.add_argument("--apply", action="store_true", help="真正执行删除")
    p_prune.set_defaults(func=cmd_prune)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
