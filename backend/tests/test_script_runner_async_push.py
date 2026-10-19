"""``InferenceScriptRunner._dispatch_push`` 的循环生命周期闸。

2026-10-18 事故：同步线程里两次 ``asyncio.run``（选股推送、滚动买卖）各自
新建事件循环，却共用同一个全局 asyncpg 连接池 → ``got Future attached to a
different loop``，滚动买卖整条失效。回归要求：

* 同步上下文：协程跑完后**在同一循环内**释放本循环的 DB 池，再关闭循环；
* 同步上下文里推送抛错：异常照抛，但池仍要释放（否则循环关闭后才 GC）；
* 异步上下文：只 ``create_task``，**绝不** dispose 主循环的池。
"""

from __future__ import annotations

import asyncio

import pytest

from backend.services.engine.inference.script_runner import InferenceScriptRunner
from backend.shared import database_manager_v2 as dbmod


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.disposed = 0

    async def push(self) -> None:
        self.calls.append("push")

    async def failing_push(self) -> None:
        self.calls.append("push")
        raise RuntimeError("推送失败")

    async def dispose(self) -> None:
        self.disposed += 1


@pytest.fixture()
def recorder(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(dbmod, "dispose_current_loop_pools", rec.dispose)
    return rec


class TestSyncContext:
    def test_push_is_awaited_then_pools_disposed(self, recorder):
        InferenceScriptRunner._dispatch_push(recorder.push(), label="test")

        assert recorder.calls == ["push"]
        assert recorder.disposed == 1  # 循环关闭前释放，连接不会跨循环残留

    def test_push_failure_still_disposes_pools(self, recorder):
        with pytest.raises(RuntimeError, match="推送失败"):
            InferenceScriptRunner._dispatch_push(recorder.failing_push(), label="test")

        assert recorder.disposed == 1


class TestAsyncContext:
    def test_in_loop_context_only_schedules_a_task(self, recorder):
        async def _main() -> None:
            InferenceScriptRunner._dispatch_push(recorder.push(), label="test")
            # 让 create_task 至少跑一步；任务本身是假协程，两个 yield 足够
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        asyncio.run(_main())

        assert recorder.calls == ["push"]
        # 主循环还活着：绝不能把它自己的池 dispose 掉
        assert recorder.disposed == 0
