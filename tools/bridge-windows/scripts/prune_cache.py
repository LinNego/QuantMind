"""缓存库维护：按保留策略清理 tdx_log / trade_log。

背景：生产 `data/tdx_cache.db` 到过 9.6 GB，其中 `tdx_log` 777 万行，
`/stats` 的 `COUNT(*)` 单次 12 秒。桥里已经把计数挪出事件循环 + 加 60s TTL，
但库本身仍要有留存上限，否则只会一直长。

用法（在 bridge-windows 目录下）：
    python scripts/prune_cache.py --dry-run    # 只看现状
    python scripts/prune_cache.py              # 命令行/收盘后执行
    python scripts/prune_cache.py --vacuum     # 回收磁盘，必须先停桥

安全说明：
- 清理是分批 DELETE，每批只短暂持有 SQLite 写锁，桥运行中可以跑；
- VACUUM 需要独占锁，**必须停桥**；
- `trade_log`（交易台账）默认保留 365 天，`tdx_log` 默认 30 天 / 300 万行。
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.db.cache_db import CacheDb  # noqa: E402


def main() -> int:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser(description="通达信桥缓存库留存清理")
    ap.add_argument("--db", default=os.path.join(root, "data", "tdx_cache.db"))
    ap.add_argument("--tdx-days", type=int, default=30,
                    help="tdx_log 保留天数（默认 30）")
    ap.add_argument("--tdx-max-rows", type=int, default=3_000_000,
                    help="tdx_log 最多保留行数（默认 300 万）")
    ap.add_argument("--trade-days", type=int, default=365,
                    help="trade_log 保留天数（默认 365）")
    ap.add_argument("--batch-size", type=int, default=5000)
    ap.add_argument("--dry-run", action="store_true", help="只打印现状，不删除")
    ap.add_argument("--vacuum", action="store_true",
                    help="清理后 VACUUM 回收磁盘（必须先停桥）")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"[prune] 数据库不存在: {args.db}")
        return 1

    db = CacheDb(args.db)
    try:
        before = db.get_stats()
        size_before = db.get_cache_disk()
        print(f"[prune] 清理前: {before} 文件 {size_before / 1024 ** 3:.2f} GB")
        print(f"[prune] 策略: tdx_log {args.tdx_days} 天 / 最多 {args.tdx_max_rows} 行；"
              f"trade_log {args.trade_days} 天")
        if args.dry_run:
            candidates = db.count_prune_candidates(
                tdx_retention_days=args.tdx_days,
                tdx_max_rows=args.tdx_max_rows,
                trade_retention_days=args.trade_days,
            )
            print(f"[prune] dry-run：将删除 tdx_log {candidates['tdx_log']} 行"
                  f"（总 {candidates['tdx_log_total']}）、trade_log "
                  f"{candidates['trade_log']} 行（总 {candidates['trade_log_total']}）")
            print("[prune] dry-run：未做任何删除")
            return 0

        deleted = db.prune_logs(
            tdx_retention_days=args.tdx_days,
            tdx_max_rows=args.tdx_max_rows,
            trade_retention_days=args.trade_days,
            batch_size=args.batch_size,
        )
        after = db.get_stats()
        print(f"[prune] 删除: {deleted}")
        print(f"[prune] 清理后: {after} 文件 {db.get_cache_disk() / 1024 ** 3:.2f} GB")

        if args.vacuum:
            print("[prune] VACUUM 中（需要独占锁，请确认桥已停止）...")
            db.vacuum()
            print(f"[prune] VACUUM 完成，文件 {db.get_cache_disk() / 1024 ** 3:.2f} GB")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
