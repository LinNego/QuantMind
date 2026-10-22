"""Mock TQ 下单/撤单极限压测（绝不连真实券商/通达信柜台）。

用法（bridge-windows 目录下）:
    python tests/load_trade_path.py

两个场景：
  A. 原始吞吐：替换掉硬上限 guard（仅测试进程内），测桥 + mock TQ 的处理上限；
  B. 硬上限：真实 299/秒、1999/日 guard + 落盘，统计提交/硬限拒绝分布。

注意：这是 mock TQ；不会产生任何真实委托。
"""
import asyncio
import json
import os
import statistics
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiohttp  # noqa: E402

from src.channel.http_server import start_http_server  # noqa: E402
from src.core.trade_plan import Order, TradePlan  # noqa: E402
from src.executor.order_tracker import OrderTracker  # noqa: E402
from src.executor.plan_executor import PlanExecutor  # noqa: E402
from src.security.trade_limits import (  # noqa: E402
    HARD_MAX_TRADES_PER_DAY, HARD_MAX_TRADES_PER_SEC, TradeRateGuard)
from src.tdx.client import TdxClient  # noqa: E402

TOKEN = "a" * 64
AUTH = {"Authorization": "Bearer " + TOKEN}


class MockTdx:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            return

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            method = body.get("method", "")
            result = {"ErrorId": "0"}
            srv = self.server
            with srv.lock:
                srv.calls[method] = srv.calls.get(method, 0) + 1
            if method == "stock_account":
                result["Value"] = 0
            elif method == "query_stock_asset":
                result.update({"Currency": "人民币", "Balance": "100000.00",
                               "Cash": "80000.00", "Asset": "200000.00",
                               "MarketValue": "120000.00"})
            elif method == "query_stock_positions":
                result["Value"] = []
            elif method == "query_stock_orders":
                result["Value"] = []
            elif method == "order_stock":
                with srv.lock:
                    srv.orders += 1
                    oid = srv.orders
                result.update({"Value": "2", "Wtbh": f"W{oid}", "Msg": "ok"})
            elif method == "cancel_order_stock":
                with srv.lock:
                    srv.cancels += 1
                result.update({"Value": "2", "Msg": "撤单成功"})
            elif method == "send_message":
                result["Value"] = "1"
            else:
                result["ErrorId"] = "-1"
            payload = json.dumps({"id": body.get("id", 1), "result": result}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    def __init__(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self.Handler)
        self.httpd.daemon_threads = True
        self.httpd.lock = threading.Lock()
        self.httpd.orders = 0
        self.httpd.cancels = 0
        self.httpd.calls = {}
        self.port = self.httpd.server_address[1]

    def start(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class NoLimit:
    def allow(self, *a, **k):
        return True, ""

    def consume_warning(self):
        return ""

    def stats(self):
        return {}


def pct(values, p):
    if not values:
        return 0.0
    values = sorted(values)
    idx = min(len(values) - 1, max(0, int(round((p / 100.0) * (len(values) - 1)))))
    return round(values[idx] * 1000.0, 1)


def classify_order(body: dict) -> str:
    orders = body.get("orders") or []
    if orders:
        status = str(orders[0].get("status") or "")
        message = str(orders[0].get("message") or "")
    else:
        status = str(body.get("status") or "")
        message = str(body.get("message") or "")
    if status == "rejected" and "硬上限" in message:
        if "笔/日" in message:
            return "hard_day"
        if "笔/秒" in message:
            return "hard_sec"
        return "hard_limit"
    if status in ("submitted", "needs_confirm", "filled"):
        return "submitted"
    if body.get("status") == "duplicate":
        return "duplicate"
    return status or "unknown"


async def run_order_load(session, base, guard, total, concurrency=64,
                         rate_per_s: float | None = None):
    import src.api.routes as routes
    executor = routes_executor[0]
    executor.order_guard = guard
    sem = asyncio.Semaphore(concurrency)
    lat = []
    outcomes = {}
    lock = asyncio.Lock()
    start = time.monotonic()

    async def one(i):
        if rate_per_s:
            delay = start + i / rate_per_s - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
        async with sem:
            plan = TradePlan(plan_id=f"load_{time.time_ns()}_{i}",
                             orders=[Order(stock_code="600519.SH", side="buy",
                                           volume=100, price=10.0)])
            t0 = time.monotonic()
            try:
                async with session.post(base + "/api/v1/plans/execute",
                                        json=plan.to_dict(), headers=AUTH) as resp:
                    body = await resp.json()
                    status = resp.status
            except Exception as exc:  # noqa: BLE001
                body, status = {"error": str(exc)}, "EXC"
            async with lock:
                lat.append(time.monotonic() - t0)
                key = classify_order(body) if status == 200 else str(status)
                outcomes[key] = outcomes.get(key, 0) + 1

    await asyncio.gather(*(one(i) for i in range(total)))
    elapsed = time.monotonic() - start
    return {"total": total, "elapsed_s": round(elapsed, 2),
            "rate_s": round(total / elapsed, 1),
            "p50_ms": pct(lat, 50), "p95_ms": pct(lat, 95),
            "outcomes": outcomes}


async def run_cancel_load(session, base, guard, total, concurrency=64):
    import src.api.routes as routes
    routes._cancel_guard = guard
    sem = asyncio.Semaphore(concurrency)
    lat = []
    outcomes = {}
    lock = asyncio.Lock()

    async def one(i):
        async with sem:
            t0 = time.monotonic()
            try:
                async with session.post(
                        base + "/api/v1/orders/cancel",
                        json={"stock_code": "600519.SH", "order_id": f"W{i}"},
                        headers=AUTH) as resp:
                    status = resp.status
                    body = await resp.json()
            except Exception as exc:  # noqa: BLE001
                status, body = "EXC", {"error": str(exc)}
            async with lock:
                lat.append(time.monotonic() - t0)
                if status == 429:
                    key = "hard_limit"
                elif status == 200 and body.get("success"):
                    key = "cancelled"
                else:
                    key = f"http_{status}"
                outcomes[key] = outcomes.get(key, 0) + 1

    t0 = time.monotonic()
    await asyncio.gather(*(one(i) for i in range(total)))
    elapsed = time.monotonic() - t0
    return {"total": total, "elapsed_s": round(elapsed, 2),
            "rate_s": round(total / elapsed, 1),
            "p50_ms": pct(lat, 50), "p95_ms": pct(lat, 95),
            "outcomes": outcomes}


routes_executor = [None]


async def main():
    mock = MockTdx().start()
    tmp = tempfile.mkdtemp()
    shared = os.path.join(tmp, "shared")
    state = os.path.join(tmp, "state")
    os.makedirs(shared, exist_ok=True)
    os.makedirs(state, exist_ok=True)
    tdx = TdxClient(f"http://127.0.0.1:{mock.port}/", timeout=5, max_retries=0)
    executor = PlanExecutor(tdx, shared, state_dir=state)
    routes_executor[0] = executor
    tracker = OrderTracker(tdx, os.path.join(tmp, "active_orders.json"))
    port = 18560
    runner = await start_http_server(
        "127.0.0.1", port, TOKEN, tdx, executor, tracker, None,
        cache_db=None, rate_limiter=None, trade_limits_dir=state)
    base = f"http://127.0.0.1:{port}"
    print(f"mock TQ :{mock.port}  bridge :{port}  state={state}")
    try:
        async with aiohttp.ClientSession() as session:
            raw = await run_order_load(session, base, NoLimit(), 1000, 64)
            print("A 原始下单吞吐:", raw)

            hard = await run_order_load(
                session, base, TradeRateGuard(
                    state_path=os.path.join(state, "orders_load.json")), 2100, 64)
            print("B 硬上限下单 (burst):", hard)
            print("   硬限常量:", HARD_MAX_TRADES_PER_SEC, "/秒", HARD_MAX_TRADES_PER_DAY, "/日")

            paced = await run_order_load(
                session, base, TradeRateGuard(
                    state_path=os.path.join(state, "orders_paced.json")),
                2100, 64, rate_per_s=250)
            print("B2 硬上限下单 (250/s 节流):", paced)

            raw_c = await run_cancel_load(session, base, NoLimit(), 1000, 64)
            print("C 原始撤单吞吐:", raw_c)

            hard_c = await run_cancel_load(
                session, base, TradeRateGuard(
                    state_path=os.path.join(state, "cancels_load.json")), 2100, 64)
            print("D 硬上限撤单:", hard_c)
            print("mock 计数:", {"orders": mock.httpd.orders,
                                 "cancels": mock.httpd.cancels,
                                 "calls": mock.httpd.calls})
    finally:
        await runner.cleanup()
        mock.stop()


if __name__ == "__main__":
    asyncio.run(main())
