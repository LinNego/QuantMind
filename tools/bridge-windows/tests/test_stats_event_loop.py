"""`/stats` 的缓存计数**必须出事件循环** —— 否则会把整座桥一起按住。

被守护的缺陷（2026-10-13 实测，真钱账户整条交易通道）
----------------------------------------------------
`/api/v1/stats` 的处理器当时这样写：

```python
stats_data["cache"] = cache_db.get_stats()      # 7 × COUNT(*)
```

`get_stats()` 对 7 张表逐个 `SELECT COUNT(*)`，其中 `tdx_log` 在生产库里有
**512 万行 / 6.9 GB** 且**没有任何清理/保留策略**，单次实测 3–52s。而它跑在
aiohttp 的**事件循环里**——于是每一次控制台轮询都把整座桥（含免鉴权的
`/api/v1/health`）按住同样久。这不是"页面慢"，是自毁循环：

1. `ui.html` 的 `setInterval(loadStats, 15000)` 每 15s 打一次 `/stats`；桥
   `ui.auto_open: true`，每被重启一次就**再开一个**控制台标签。
2. `/stats` 按住循环 3–52s ⇒ `/health` 一起停摆。
3. Windows 侧 `watchdog.ps1` 用 `Invoke-WebRequest -TimeoutSec 5` 探 `/health`，
   连续 2 次失败即**杀掉桥重启** ⇒ 回到第 1 步（又多一个标签）。

当日证据：15:00 之后 31 次"桥疑似崩溃/假活，正在重启"，**每一次**都紧跟在一记
桥内耗时 8–52s 的 `/stats` 之后（`bridge.log` 无任何崩溃 traceback，桥是被杀的）；
全天重启 378 次。QuantMind 侧 REAL 启动闸门的行情探针同步超时 ⇒ 策略启不动。

怎么量「事件循环被按住」
------------------------
不能用「探 `health` 花了多久」：请求要经过 TCP 建连、客户端连接池、以及**测试自己
的 `await` 也会被同一个循环挡住**——在旧码上，测试协程在阻塞期间根本醒不过来，
反而量到 0 延迟（首版就栽在这里：旧码上延迟断言竟然通过了）。

改成**心跳间隔**：另起一个每 10ms 醒一次的协程记录相邻两次醒来的间隔。事件循环
一旦被同步调用按住，间隔里就会出现一个宽峰——这是对"循环是否被占住"的直接测量，
与 HTTP 建连时序、客户端池化全都无关。

两条断言各有分工，缺一条都会漏：
* 心跳最大间隔 < 0.15s —— 证明计数**不再**占着循环。旧码上是 SLOW_COUNT_SEC 那一档。
* `get_stats` 调用次数 == 1 —— 证明 60s TTL 缓存生效。少了它，把 `to_thread` 写成
  "每次都重算"也能过前者（线程里慢不占循环），但控制台每 15s 白烧一次 3–52s 的
  COUNT(*) 依然不该接受。
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from src.api.routes import build_app  # noqa: E402

SLOW_COUNT_SEC = 0.5
HEARTBEAT_SEC = 0.01
#: 循环被按住的上界。旧码上会看到 ~SLOW_COUNT_SEC 的宽峰，新码上恒在毫秒级。
MAX_LOOP_GAP_SEC = 0.15
TOKEN = "t" * 64


class _SlowCacheDb:
    """`get_stats()` 慢得像真的（生产实测 3–52s，这里取 0.5s 保证测试快）。"""

    def __init__(self):
        self.calls = 0

    def get_stats(self) -> dict:
        self.calls += 1
        time.sleep(SLOW_COUNT_SEC)
        return {"kline": 1, "tdx_log": 5128125}

    def get_cache_disk(self) -> int:
        return 6_909_087_744


class _Tdx:
    def health_check_fast(self, timeout: float = 1.0) -> bool:
        return True


class _OrderGuard:
    def stats(self) -> dict:
        return {"day_count": 0, "day_limit": 1999}


class _Executor:
    """/stats 会读 executor.order_guard.stats()；其余字段本测试不关心。"""

    order_guard = _OrderGuard()


def _app(cache_db):
    return build_app(
        TOKEN,
        _Tdx(),
        _Executor(),       # executor: /stats 只读 order_guard
        object(),          # tracker
        object(),          # sltp
        cache_db=cache_db,
        memory_cache=None,
    )


async def _run():
    own = _SlowCacheDb()
    gaps: list[float] = []
    stop = asyncio.Event()

    async def heartbeat():
        last = time.monotonic()
        while not stop.is_set():
            await asyncio.sleep(HEARTBEAT_SEC)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    async with TestClient(TestServer(_app(own))) as client:
        hb = asyncio.create_task(heartbeat())
        await asyncio.sleep(0.05)          # 让心跳先跑起来，拿到基线间隔

        stats = await client.get("/api/v1/stats")   # 这一记会（短暂）卡在计数上
        payload = await stats.json()
        await client.get("/api/v1/stats")           # 第二击：TTL 内应复用

        stop.set()
        hb.cancel()

    worst = max(gaps) if gaps else 0.0
    assert stats.status == 200, f"/stats 应 200，实际 {stats.status}"
    assert payload["data"]["cache"]["tdx_log"] == 5128125, "计数要原样带出"
    assert payload["data"]["cache"]["disk_bytes"] == 6_909_087_744

    assert worst < MAX_LOOP_GAP_SEC, (
        f"事件循环被按住 {worst:.2f}s（上界 {MAX_LOOP_GAP_SEC}s）——"
        f"缓存计数又回到循环里了。必须经 asyncio.to_thread(_cache_stats_sync, ...) "
        f"出循环，否则 Windows 看门狗 5s 超时会判「桥假活」并杀桥重启。"
    )
    assert own.calls == 1, (
        f"60s TTL 内 /stats 共算了 {own.calls} 次计数，应为 1 次"
        f"（控制台每 15s 轮询，不该每次都烧 3-52s 的 COUNT(*)）"
    )
    print(
        f"   心跳 {len(gaps)} 次，最大间隔 {worst*1000:.0f}ms"
        f"（基线 {HEARTBEAT_SEC*1000:.0f}ms）；计数只算了 {own.calls} 次"
    )


if __name__ == "__main__":
    asyncio.run(_run())
    print("✅ /stats 计数出事件循环 + 60s TTL 缓存：通过")
