"""Celery worker 内存预算回归守卫。

2026-10-16/17 的 CUSTOM 重建在 8G worker 内连续被 cgroup OOM SIGKILL。
2026-10-18 隔离实测同一作业峰值 RSS 7820MB（alpha360+tdxgs+jq110 全窗，
925 交易日 × 5578 只；基线 274MB），8g 上限只剩约 370MB，worker 基线与任何
并发任务都会顶破。本测试锁定 celery-worker 的 mem_limit 不得低于 12g，且
memswap_limit 必须大于 mem_limit。

只读 docker-compose.yml，不启动容器；宿主侧运行（pytest scripts/tests）。
"""
from __future__ import annotations

from pathlib import Path

import yaml

COMPOSE = Path(__file__).resolve().parents[2] / "docker-compose.yml"
MIN_LIMIT_BYTES = 12 * 1024**3


def _parse_size(value: str) -> int:
    """Docker 内存尺寸（12g / 10240m / 纯字节）→ 字节。"""
    text = str(value).strip().lower()
    units = {"k": 1024, "m": 1024**2, "g": 1024**3}
    if text[-1:] in units:
        return int(float(text[:-1]) * units[text[-1]])
    return int(text)


def test_celery_worker_memory_limit_covers_custom_rebuild_peak():
    services = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]
    worker = services["celery-worker"]
    limit = _parse_size(worker["mem_limit"])
    swap = _parse_size(worker["memswap_limit"])

    assert limit >= MIN_LIMIT_BYTES, (
        f"celery-worker mem_limit={worker['mem_limit']} 低于 CUSTOM 重建实测所需的 12g"
    )
    assert swap > limit, "memswap_limit 必须大于 mem_limit（留出交换余量）"
