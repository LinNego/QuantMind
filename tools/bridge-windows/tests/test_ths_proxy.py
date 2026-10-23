"""同花顺旁路 HTTP 面测试（mock worker，不连真同花顺）。

守护点：
- 新端点必须在桥鉴权之后（无 token 401）；
- 入参白名单/正则校验（代码注入、非法方法）；
- TTL 缓存生效（同 key 第二次不打上游）；
- 上游 5xx → 502，连续失败 → 熔断 503，且熔断期间不再打上游；
- `ths.enabled=false` → 503 THS_DISABLED（TDX 路径不受影响）。

运行: cd bridge-windows && python3 tests/test_ths_proxy.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from src.api.routes import build_app  # noqa: E402
from src.ths.client import ThsClient  # noqa: E402

TOKEN = "t" * 64
AUTH = {"Authorization": "Bearer " + TOKEN}


class _FakeTdx:
    def get_market_snapshot(self, code):
        return {"Now": "1338.00", "LastClose": "1318.49", "Open": "1311.84",
                "Max": "1340.00", "Min": "1310.00", "Volume": "2005",
                "Amount": "26354.92"}

    def call(self, method, params):
        # /tdx/call 的实际派发口（真实 TdxClient 的 call 会做参数适配）
        return self.get_market_snapshot(params.get("stock_code", ""))

    def health_check_fast(self, timeout: float = 1.0) -> bool:
        return True


class _FakeExecutor:
    class _Guard:
        def stats(self):
            return {"day_count": 0, "day_limit": 1999}

    order_guard = _Guard()


def _bridge_app(ths):
    return build_app(TOKEN, _FakeTdx(), _FakeExecutor(), object(), object(),
                     cache_db=None, ths=ths)


def _worker_app(state: dict) -> web.Application:
    app = web.Application()

    async def quote(request):
        if state["busy"]:
            return web.json_response(
                {"error": "worker_busy", "detail": "busy"}, status=503)
        if state["fail"]:
            return web.json_response({"error": "boom"}, status=500)
        state["quote_calls"] += 1
        codes = (request.query.get("codes") or "").split(",")
        return web.json_response([
            {"code": c, "price": 1337.9, "pre_close": 1318.49, "volume": 100}
            for c in codes if c
        ])

    async def quotes_ext(request):
        if state["fail"]:
            return web.json_response({"error": "boom"}, status=500)
        state["quote_calls"] += 1
        codes = (request.query.get("codes") or "").split(",")
        return web.json_response([
            {"code": c, "price": 1337.9, "pre_close": 1318.49, "volume": 100}
            for c in codes if c
        ])

    async def depth(request):
        state["depth_calls"] += 1
        return web.json_response({
            "code": request.match_info["code"],
            "bids": [{"level": 1, "price": 1337.5, "volume": 10}],
            "asks": [{"level": 1, "price": 1337.9, "volume": 12}],
        })

    async def status(request):
        return web.json_response({"connected": True, "kind": "L2"})

    app.router.add_get("/api/quote", quote)
    app.router.add_get("/api/quotes_ext", quotes_ext)
    app.router.add_get("/api/depth/{code}", depth)
    app.router.add_get("/api/status", status)
    return app


async def _run():
    state = {"quote_calls": 0, "depth_calls": 0, "fail": False, "busy": False}
    worker = TestServer(_worker_app(state))
    await worker.start_server()
    base = str(worker.make_url("")).rstrip("/")
    ths = ThsClient(base_url=base, enabled=True, circuit_failures=3,
                    circuit_cooldown_s=30)

    try:
        async with TestClient(TestServer(_bridge_app(ths))) as client:
            # 1. 鉴权
            resp = await client.get("/api/v1/ths/status")
            assert resp.status == 401, resp.status

            # 2. 状态（含 worker 探测）
            resp = await client.get("/api/v1/ths/status", headers=AUTH)
            body = await resp.json()
            assert resp.status == 200 and body["data"]["enabled"] is True
            assert body["data"]["worker"]["connected"] is True

            # 3. 报价 + TTL 缓存（第二次不打上游）
            resp = await client.get("/api/v1/ths/quote?codes=600519.SH", headers=AUTH)
            body = await resp.json()
            assert resp.status == 200 and body["data"][0]["price"] == 1337.9
            resp = await client.get("/api/v1/ths/quote?codes=600519.SH", headers=AUTH)
            assert resp.status == 200
            assert state["quote_calls"] == 1, f"TTL 缓存失效: {state}"

            # 4. 入参校验：代码注入/非法方法
            resp = await client.get("/api/v1/ths/depth/..%2Fetc%2Fpasswd", headers=AUTH)
            assert resp.status == 400, resp.status
            resp = await client.post("/api/v1/ths/call", headers=AUTH,
                                     json={"method": "order", "params": {}})
            assert resp.status == 400, resp.status

            # 4b. /tdx/call 只读白名单：写操作一律 403；数据方法仍放行
            for bad in ("exec_to_tdx", "send_file", "formula_set_data",
                        "create_sector"):
                resp = await client.post("/api/v1/tdx/call", headers=AUTH,
                                         json={"method": bad, "params": {}})
                body_bad = await resp.json()
                assert resp.status == 403 and \
                    body_bad["error"]["code"] == "METHOD_NOT_ALLOWED", \
                    (bad, resp.status, body_bad)
            resp = await client.post("/api/v1/tdx/call", headers=AUTH,
                                     json={"method": "get_market_snapshot",
                                           "params": {"stock_code": "600519.SH"}})
            assert resp.status == 200, (resp.status, await resp.json())

            # 4c. worker 饱和（503 worker_busy）不计入熔断：连打 6 次后仍可恢复
            state["busy"] = True
            for i in range(6):
                resp = await client.get(f"/api/v1/ths/quote?codes=60080{i}.SH",
                                        headers=AUTH)
                body = await resp.json()
                assert resp.status == 503 and \
                    body["error"]["code"] == "THS_BUSY", (i, resp.status, body)
            state["busy"] = False
            resp = await client.get("/api/v1/ths/quote?codes=600519.SH", headers=AUTH)
            assert resp.status == 200, (resp.status, await resp.json())

            # 5. 10 档盘口透传
            resp = await client.get("/api/v1/ths/depth/600519.SH?levels=10", headers=AUTH)
            body = await resp.json()
            assert resp.status == 200 and body["data"]["bids"][0]["level"] == 1
            assert state["depth_calls"] == 1

            # 6. TDX ↔ THS 同尺对照（两源都过 normalize：金额=元、价格=float）
            resp = await client.get("/api/v1/ths/compare?code=600519.SH", headers=AUTH)
            body = await resp.json()
            assert resp.status == 200, (resp.status, body)
            assert body["data"]["tdx"]["last"] == 1338.0, body["data"]["tdx"]
            assert body["data"]["ths"]["last"] == 1337.9, body["data"]["ths"]
            assert body["data"]["tdx"]["amount"] == 263549200.0  # 万元 → 元
            assert body["data"]["diff_pct"]["last"] is not None

            # 7. 上游失败 → 502；连续 3 次 → 熔断 503
            state["fail"] = True
            # 每次换一组 codes，绕开 1s TTL 缓存（缓存键含全部查询参数）
            for i in range(3):
                resp = await client.get(f"/api/v1/ths/quote?codes=60051{i}.SH",
                                        headers=AUTH)
                body = await resp.json()
                assert resp.status == 502 and body["error"]["code"] == "THS_UNAVAILABLE", \
                    (i, resp.status, body)
            resp = await client.get("/api/v1/ths/quote?codes=600519.SZ", headers=AUTH)
            body = await resp.json()
            assert resp.status == 503 and body["error"]["code"] == "THS_CIRCUIT_OPEN", \
                (resp.status, body)
    finally:
        await ths.close()
        await worker.close()

    # 8. 未启用 → 503 THS_DISABLED
    ths_off = ThsClient(base_url=base, enabled=False)
    try:
        async with TestClient(TestServer(_bridge_app(ths_off))) as client:
            resp = await client.get("/api/v1/ths/quote?codes=600519.SH", headers=AUTH)
            body = await resp.json()
            assert resp.status == 503 and body["error"]["code"] == "THS_DISABLED"
    finally:
        await ths_off.close()

    print("✅ 同花顺旁路：鉴权 / 缓存 / 白名单 / 熔断 / 同尺对照 全部通过")


if __name__ == "__main__":
    asyncio.run(_run())
