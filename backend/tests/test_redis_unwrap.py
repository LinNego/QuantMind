"""Redis 解包的 redis-py 8 兼容回归（2026-10-23 线上故障）。

故障形态：``[EvalScores] 评分任务异常: 'function' object has no attribute 'set'``，
每分钟一次、每天数百次；``run_eval_scores_worker`` 每轮在
``eval_trigger.acquire_run_lock`` 就抛错，六卡评分/自动留档从未真正跑通。

根因：redis-py 8.1.0 的原生 ``redis.Redis`` 有 ``client()`` 方法；
``hasattr(redis, "client")`` 把它误判成 ``RedisClient`` 包装器，返回 bound method。
worker 先 ``_raw_client(get_trade_redis())`` 解包成原生 client，再传给
``acquire_run_lock`` → 第二次 ``_raw`` 二次解包，正好踩中。
"""

from __future__ import annotations

import pytest

from backend.shared import eval_trigger
from backend.shared.redis_unwrap import raw_redis_client


class _RawRedis8:
    """redis-py 8 形态：既有 client() 方法，也能直接 set/get/eval。"""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def client(self):  # noqa: D401 - 模拟 redis-py 8 的连接工厂方法
        raise AssertionError("原生 client 的 client() 方法不应被调用")

    def set(self, key, value, nx=False, ex=None):  # noqa: ARG002
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True

    def get(self, key):
        return self.store.get(key)

    def delete(self, key):
        self.store.pop(key, None)
        return 1

    def eval(self, *_args, **_kwargs):
        return 1


class _Wrapper:
    """``trade_shared.redis_client.RedisClient`` 形态。"""

    def __init__(self, client) -> None:
        self.client = client

    def set(self, key, value, ttl=None):  # noqa: ARG002
        raise AssertionError("未连接时不得回退到包装器的 set（会吞异常）")


def test_raw_redis_py8_is_not_mistaken_for_wrapper():
    raw = _RawRedis8()
    assert raw_redis_client(raw) is raw


def test_wrapper_unwraps_live_client():
    raw = _RawRedis8()
    assert raw_redis_client(_Wrapper(raw)) is raw


def test_unconnected_wrapper_returns_none():
    assert raw_redis_client(_Wrapper(None)) is None


def test_none_and_unknown_objects():
    assert raw_redis_client(None) is None
    assert raw_redis_client(object()) is None


def test_acquire_run_lock_accepts_already_unwrapped_raw_client():
    """复现线上二次解包：raw → acquire_run_lock 曾返回 bound method。"""
    raw = _RawRedis8()
    assert eval_trigger.acquire_run_lock(raw, "token-1") is True
    assert raw.store[eval_trigger.EVAL_RUN_LOCK_KEY] == "token-1"
    assert eval_trigger.release_run_lock(raw, "token-1") is True


def test_eval_scores_raw_client_accepts_wrapper_and_raw():
    from backend.services.trade.services.eval_scores_service import _raw_client

    raw = _RawRedis8()
    assert _raw_client(_Wrapper(raw)) is raw
    assert _raw_client(raw) is raw
    assert _raw_client(_Wrapper(None)) is None


def test_sltp_raw_client_accepts_already_unwrapped_raw_client():
    from backend.services.live_trading.services.sltp_executor import (
        _raw_client as sltp_raw_client,
    )

    raw = _RawRedis8()
    assert sltp_raw_client(raw) is raw
    assert sltp_raw_client(_Wrapper(raw)) is raw


def test_simulation_manager_ensure_client_accepts_raw_redis8():
    """模拟交易：把原生 redis-py 8 传进 SimulationAccountManager 时不得解包成方法。"""
    from backend.services.trade_shared.simulation_manager import (
        SimulationAccountManager,
    )

    raw = _RawRedis8()
    manager = SimulationAccountManager(raw)
    assert manager._ensure_client() is raw
