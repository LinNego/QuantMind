"""回归闸：全局 async 引擎必须按事件循环隔离。

事故（2026-10-18 11:47 日志实录）：``script_runner`` 在同步线程里用
``asyncio.run`` 推送通达信，而 ``database_manager_v2`` 只有一个全局
``AsyncEngine``；asyncpg 连接绑定**创建它的 loop**，于是第二个
``asyncio.run`` 复用第一个循环留下的池连接时必然报
``got Future attached to a different loop``——滚动买卖推送整条失效，
同一进程里 uvicorn 主循环的普通请求也会被污染成 ``Master session error``。

修法：每个事件循环一份引擎/会话工厂（``WeakKeyDictionary`` 键为 loop），
并在临时循环收尾时只释放**本循环**的池。本文件不连真库：``create_async_engine``
被替换成假引擎，只验证「同循环复用、跨循环隔离、释放只清本循环」。
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from backend.shared import database_manager_v2 as dbmod


class _FakeEngine:
    def __init__(self, url: str, **kwargs):
        self.url = url
        self.kwargs = kwargs
        self.disposed = 0

    async def dispose(self):
        self.disposed += 1


@pytest.fixture()
def fake_engines(monkeypatch):
    created: list[_FakeEngine] = []

    def _create(url: str, **kwargs):
        engine = _FakeEngine(url, **kwargs)
        created.append(engine)
        return engine

    monkeypatch.setattr(dbmod, "create_async_engine", _create)
    # 事件监听需要真实 Engine 的 dispatch 表，假引擎上跳过
    monkeypatch.setattr(
        dbmod.DatabaseManager, "_setup_engine_events", lambda *a, **kw: None
    )
    return created


def _run_in_new_loop(fn) -> None:
    """在**另一个线程的新事件循环**里执行 ``fn``（模拟 asyncio.run 现场）。"""
    error: list[BaseException] = []

    def _target() -> None:
        try:
            asyncio.run(fn())
        except BaseException as exc:  # noqa: BLE001 - 线程里原样抛回主线程
            error.append(exc)

    thread = threading.Thread(target=_target)
    thread.start()
    thread.join()
    if error:
        raise error[0]


class TestLoopLocalPools:
    def test_same_loop_reuses_pools(self, fake_engines):
        """同一循环内多次取池 = 同一份（否则每条 SQL 都新建引擎）。"""
        manager = dbmod.DatabaseManager()

        async def _main() -> None:
            first = manager._pools_for_current_loop()
            second = manager._pools_for_current_loop()
            assert first is second
            assert first.master_engine is second.master_engine

        asyncio.run(_main())
        assert len(fake_engines) == 1

    def test_different_loops_get_different_engines(self, fake_engines):
        """跨循环必须换引擎——asyncpg 连接不能跨 loop 复用（事故根因）。"""
        manager = dbmod.DatabaseManager()
        engine_ids: list[int] = []

        async def _main() -> None:
            pools = manager._pools_for_current_loop()
            engine_ids.append(id(pools.master_engine))

        asyncio.run(_main())
        _run_in_new_loop(_main)

        assert len(set(engine_ids)) == 2
        assert len(fake_engines) == 2

    def test_dispose_current_loop_frees_only_that_loops_engine(self, fake_engines):
        """临时循环收尾只释放自己的池：不能把主循环的引擎 dispose 掉。"""
        manager = dbmod.DatabaseManager()

        async def _primary() -> None:
            manager._pools_for_current_loop()

        asyncio.run(_primary())
        primary_engine = fake_engines[0]

        async def _transient() -> None:
            manager._pools_for_current_loop()
            transient_engine = fake_engines[1]
            await manager.dispose_current_loop()
            assert transient_engine.disposed == 1
            assert primary_engine.disposed == 0

        _run_in_new_loop(_transient)
        assert primary_engine.disposed == 0

    def test_dispose_without_pools_is_a_noop(self, fake_engines):
        manager = dbmod.DatabaseManager()

        async def _main() -> None:
            await manager.dispose_current_loop()

        asyncio.run(_main())
        assert fake_engines == []
