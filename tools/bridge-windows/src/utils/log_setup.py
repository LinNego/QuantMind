"""日志落盘配置——按大小滚动，防止 bridge.log 无限增长。

历史：生产 `bridge.log` 曾达到 1.5 GB（每个 HTTP 请求一行 INFO access log，
热集轮转每分钟几百行），Windows 盘被慢慢吃掉，打开/grep 也很慢。
改为 50 MB × 5 份滚动，总量封顶 ~300 MB。

单独成模块是为了可测：`tests/test_all.py` 用很小的 max_bytes 验证确实会滚动。
"""
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

LOG_MAX_BYTES = 50 * 1024 * 1024
LOG_BACKUP_COUNT = 5


def build_handlers(log_dir: str, max_bytes: int = LOG_MAX_BYTES,
                   backup_count: int = LOG_BACKUP_COUNT,
                   filename: str = "bridge.log") -> list[logging.Handler]:
    """构造桥/采集器的日志 handler 列表（控制台 + 滚动文件）。

    windowed/exe 模式没有 stderr，用 NullHandler 占位；源码模式输出到控制台。
    ``filename`` 供采集器复用（collector.log），桥默认 bridge.log。
    """
    os.makedirs(log_dir, exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.StreamHandler() if sys.stderr else logging.NullHandler()
    ]
    handlers.append(RotatingFileHandler(
        os.path.join(log_dir, filename),
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    ))
    return handlers
