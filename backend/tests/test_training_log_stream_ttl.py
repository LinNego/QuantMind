"""训练日志流 Redis Stream 必须带 TTL（2026-10-23 事故根因之一）。

背景：`append_log` 只用 `maxlen` 限条目数，**键本身不设过期** → 每次训练留下的
`qm:training:run:logs:*` 永久驻留。实测 db4 累积 405 个 stream、201.8MB，把
`quantmind-redis` 顶到 `maxmemory`（512M / noeviction）→ 全实例写拒绝 →
盘中行情快照写不进去 → 决策轮拿不到现价。

本测试守卫：每写一条日志就刷新一次 stream TTL，活跃任务不因过期丢日志，
结束后的任务按窗口自然回收。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import backend.services.engine.training.training_log_stream as tls  # noqa: E402
from backend.services.engine.training.training_log_stream import (  # noqa: E402
    TrainingRunLogStream,
)


class _RecordingPipe:
    """记录 pipeline 内发出的命令（不连真 Redis）。"""

    def __init__(self, calls: list[tuple]) -> None:
        self._calls = calls

    def xadd(self, key, fields, maxlen=None, approximate=None):  # noqa: ANN001
        self._calls.append(("xadd", key, maxlen))

    def expire(self, key, ttl):  # noqa: ANN001
        self._calls.append(("expire", key, ttl))

    def execute(self) -> list:
        return [1] * len(self._calls)


class _RecordingRedis:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def pipeline(self, transaction: bool = False):  # noqa: ANN201
        return _RecordingPipe(self.calls)

    def get(self, key: str):  # noqa: ANN201
        return None

    def set(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return True

    def setex(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return True


def _make_stream(fake: _RecordingRedis) -> TrainingRunLogStream:
    stream = TrainingRunLogStream()
    stream.enabled = True
    stream._client = fake
    tls.redis_lib = object()  # type: ignore[attr-defined]
    return stream


def test_append_log_sets_stream_ttl() -> None:
    """写日志必须同时给 stream 键续过期，否则键永久驻留。"""
    fake = _RecordingRedis()
    stream = _make_stream(fake)

    stream.append_log(
        run_id="train_test_ttl",
        tenant_id="default",
        user_id="1",
        line="epoch 1/10",
    )

    kinds = [c[0] for c in fake.calls]
    assert "xadd" in kinds
    assert "expire" in kinds, f"未设置 stream TTL，实际命令: {fake.calls}"

    expire = next(c for c in fake.calls if c[0] == "expire")
    assert expire[1] == "qm:training:run:logs:train_test_ttl"
    assert expire[2] == stream.stream_ttl_sec
    assert stream.stream_ttl_sec > 0


def test_stream_ttl_env_override(monkeypatch) -> None:
    """TTL 可由环境变量配置（运维调窗口无需改码）。"""
    monkeypatch.setenv("TRAINING_LOG_STREAM_TTL_SECONDS", "3600")
    stream = TrainingRunLogStream()
    assert stream.stream_ttl_sec == 3600
