"""Redis 包装器/原生客户端的统一解包（redis-py 8 兼容）。

背景（2026-10-23 故障）：redis-py 8.1.0 给原生 ``redis.Redis`` 增加了
``client()`` 方法（连接工厂）。历史上用
``getattr(redis, "client", None)`` / ``hasattr(redis, "client")`` 判断
「是不是 ``RedisClient`` 包装器」，会把原生 client 误判成包装器，返回
**bound method**；调用方在方法对象上执行 ``.set`` → ``'function' object
has no attribute 'set'``。

正确判据：包装器的 ``.client`` 是**实例属性**（raw client 或 ``None``，不可调用）；
原生 client 的 ``.client`` 是 **callable 方法**。据此区分，且：
- 包装器 ``.client is None``（未连上）→ 返回 ``None``，让调用方按「不可用」处理，
  绝不回退到包装器的 ``set``（它的签名是 ``(key, value, ttl=None)``，吞异常）；
- 原生 client → 返回自身；
- 其他带 ``set`` 的鸭子类型 → 返回自身。
"""

from __future__ import annotations

from typing import Any


def raw_redis_client(value: Any) -> Any | None:
    """解包出底层 redis-py 客户端；未连接/非法对象返回 ``None``。"""
    if value is None:
        return None
    inner = getattr(value, "client", None)
    if inner is not None and not callable(inner):
        # RedisClient 包装器：.client 是 raw client 实例
        return inner
    if hasattr(value, "client") and not callable(inner):
        # RedisClient 包装器但未连上（.client is None）
        return None
    # 原生 redis-py 8（.client 是 callable 方法）或没有 .client 的鸭子客户端
    return value if hasattr(value, "set") else None


def redis_like_client(value: Any) -> Any | None:
    """兼容位（鸭子客户端优先）：有 live ``.client`` 就解包，否则返回对象自身。

    用于「接受任意 redis 形状」的调用点（风控/缓存/脚本；测试会传
    ``SimpleNamespace(client=None, hgetall=...)`` 这类桩）。与
    :func:`raw_redis_client` 的差异只有一处：**包装器未连上时返回包装器自身**
    （历史语义，调用方自己决定失败姿态），而不是 ``None``。
    原生 redis-py 8 的 ``.client`` 是 callable 方法，这里不会被误拆。
    """
    if value is None:
        return None
    inner = getattr(value, "client", None)
    if inner is not None and not callable(inner):
        return inner
    return value
