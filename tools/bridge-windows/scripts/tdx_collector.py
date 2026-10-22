"""通达信行情采集器入口（Windows 独立进程）。

用法（在 bridge-windows 目录下）：
    python scripts/tdx_collector.py
    python scripts/tdx_collector.py --port 8560 --db data/market_1m.db

环境变量：BRIDGE_URL / BRIDGE_TOKEN / COLLECTOR_TOKEN / COLLECTOR_HOT_SET 等，
见 src/collector/config.py。
"""
import argparse
import asyncio
import logging
import os
import sys
from dataclasses import replace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.collector.config import VERSION, load_config  # noqa: E402
from src.collector.service import CollectorService  # noqa: E402
from src.utils.log_setup import build_handlers  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="通达信行情采集器")
    ap.add_argument("--config", default=os.path.join(ROOT, "config.yaml"))
    ap.add_argument("--host", default="")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--db", default="")
    ap.add_argument("--hot-set", default="",
                    help="热集代码（逗号分隔，≤100），如 600519.SH,000001.SZ")
    ap.add_argument("--version", action="store_true")
    args = ap.parse_args()

    if args.version:
        print(f"tdx-collector {VERSION}")
        return 0

    cfg = load_config(args.config, ROOT)
    cli_hot = tuple(x.strip().upper() for x in args.hot_set.split(",") if x.strip())
    cfg = replace(
        cfg,
        host=args.host or cfg.host,
        port=args.port or cfg.port,
        db_path=args.db or cfg.db_path,
        hot_set=cli_hot or cfg.hot_set,
    )

    os.makedirs(cfg.log_dir, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=build_handlers(cfg.log_dir, filename="collector.log"),
    )
    # 采集器自己的访问日志没有价值；桥/采集业务日志已足够
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)

    logging.getLogger("collector").info(
        "启动配置: bridge=%s db=%s http=%s:%s hot=%d interval=%.1fs",
        cfg.bridge_url, cfg.db_path, cfg.host, cfg.port,
        len(cfg.hot_set), cfg.snapshot_interval)

    service = CollectorService(cfg)
    try:
        asyncio.run(service.run())
    except KeyboardInterrupt:
        logging.getLogger("collector").info("收到 Ctrl+C，退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
