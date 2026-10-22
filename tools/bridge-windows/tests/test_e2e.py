"""端到端集成测试: 用独立线程的 mock TDX 服务验证 HTTP 桥、执行器、文件通道、止损守护全链路.

运行: cd ~/trading-bridge/bridge-windows && python3 tests/test_e2e.py
"""
import asyncio
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TOKEN = "a" * 64


# ---- mock TDX (独立线程, 标准库) ----

class MockTdx:
    def __init__(self, price=11.00, sell_value="1", cancel_value="1",
                 cancel_msg="撤单成功"):
        self.price = price
        self.sell_value = sell_value
        self.cancel_value = cancel_value
        self.cancel_msg = cancel_msg
        self.orders_list = []  # query_stock_orders 返回的当日委托
        self.cancels = []      # cancel_order_stock 收到的参数
        self.orders_placed = []
        self.port = None
        self.pricevol_delay = 0.0
        self.messages = []
        self._httpd = None
        self._thread = None

    def start(self):
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._httpd.shutdown()
        self._httpd.server_close()

    def _handler(self, *a, **kw):
        handler = _MockHandler
        handler.mock = self
        return handler(*a, **kw)


class _MockHandler(BaseHTTPRequestHandler):
    mock = None

    def log_message(self, *a):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        method = body.get("method", "")
        result = {"ErrorId": "0"}

        if method == "get_match_stkinfo":
            result["Value"] = [{"Code": "600519.SH", "Name": "贵州茅台"}]
        elif method == "stock_account":
            result["Value"] = 0
        elif method == "query_stock_asset":
            result.update({"Currency": "人民币", "Balance": "100000.00", "Cash": "80000.00",
                           "Asset": "200000.00", "MarketValue": "120000.00"})
        elif method == "query_stock_positions":
            result["Value"] = [{"Code": "000001.SZ", "Cbj": "10.50", "TotalVol": "1000",
                                "CanUseVol": "1000"},
                               {"Code": "600519.SH", "Cbj": "10.00", "TotalVol": "100",
                                "CanUseVol": "100"}]
        elif method == "query_stock_orders":
            result["Value"] = list(self.mock.orders_list)
        elif method == "order_stock":
            params = body.get("params", {})
            side = "sell" if params.get("order_type") == 1 else "buy"
            self.mock.orders_placed.append({"stock": params.get("stock_code"), "side": side,
                                            "vol": params.get("order_volume")})
            result.update({"Value": self.mock.sell_value, "Wtbh": f"W{int(time.time())}",
                           "Msg": "已发送信号至客户端，待用户确认！"})
        elif method == "cancel_order_stock":
            self.mock.cancels.append(body.get("params", {}))
            result.update({"Value": self.mock.cancel_value, "Msg": self.mock.cancel_msg})
        elif method == "get_market_snapshot":
            result.update({"Now": f"{self.mock.price}", "Max": "12.00", "Min": "10.00"})
        elif method == "get_market_data":
            result.update({"Close": [f"{self.mock.price}"], "Date": ["20260812"]})
        elif method == "get_pricevol":
            # 模拟全市场快照的耗时（用于验证 trade/data 分槽）
            time.sleep(self.mock.pricevol_delay)
            codes = (body.get("params") or {}).get("stock_list", [])
            result["Value"] = {c: {"Now": f"{self.mock.price}", "LastClose": "10.00",
                                   "Volume": "1000"} for c in codes}
        elif method == "send_message":
            self.mock.messages.append((body.get("params") or {}).get("msg_str", ""))
            result["Value"] = "1"
        else:
            result["ErrorId"] = "-1"
            result["Msg"] = f"unknown {method}"

        resp = json.dumps({"id": body.get("id", 1), "result": result}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)


async def run_tests():
    from aiohttp import web, ClientSession
    from src.tdx.client import TdxClient
    from src.executor.plan_executor import PlanExecutor
    from src.executor.stop_loss_daemon import StopLossDaemon
    from src.channel.http_server import start_http_server
    from src.channel.file_sync import FileSyncChannel
    from src.core.trade_plan import TradePlan, Order

    tmp = tempfile.mkdtemp()
    shared = os.path.join(tmp, "shared")

    # 1. mock TDX
    mock = MockTdx(price=11.00).start()
    tdx = TdxClient(f"http://127.0.0.1:{mock.port}/", timeout=5, max_retries=0)
    assert tdx.health_check(), "mock TDX 健康检查失败"
    print("[PASS] TDX 客户端连接 mock 服务成功")

    executor = PlanExecutor(tdx, shared)
    from src.executor.order_tracker import OrderTracker
    tracker = OrderTracker(tdx, os.path.join(tmp, "active_orders.json"))

    # 2. HTTP 桥 (固定端口, 测试专用)
    HTTP_PORT = 18550
    runner = await start_http_server("127.0.0.1", HTTP_PORT, TOKEN, tdx, executor, tracker, None)
    base = f"http://127.0.0.1:{HTTP_PORT}"

    async with ClientSession() as session:
        async def api(path, body, token=TOKEN):
            async with session.post(base + path, json=body,
                                    headers={"Authorization": f"Bearer {token}"}) as resp:
                if resp.status == 200:
                    return resp.status, await resp.json()
                try:
                    return resp.status, await resp.json()
                except Exception:
                    return resp.status, {"error": await resp.text()}

        # 3. execute_plan (卖先买后)
        plan = TradePlan(
            plan_id="plan_e2e_001",
            orders=[Order(stock_code="600519.SH", side="buy", volume=100, price=1650.0),
                    Order(stock_code="000001.SZ", side="sell", volume=500)])
        report = executor.execute_plan(plan)
        assert report["status"] == "executed", f"执行失败: {report}"
        assert os.path.exists(os.path.join(shared, "trade_log.jsonl")), "交易记录未同步"
        print("[PASS] execute_plan 成功 + 交易记录已同步")

        # 卖先买后: 第一个下单应是卖出
        assert mock.orders_placed[0]["side"] == "sell", "卖出应先于买入"
        print("[PASS] 卖先买后顺序正确:", [o["side"] for o in mock.orders_placed])

        # 4. 幂等
        dup = executor.execute_plan(plan)
        assert dup["status"] == "duplicate"
        print("[PASS] 幂等去重生效")

        # 5. HTTP 鉴权 + 查账号
        status, acct = await api("/api/v1/account/query", {"account_type": "stock"})
        assert status == 200 and acct["asset"]["cash"] == 80000.0, f"{status} {acct}"
        print(f"[PASS] HTTP 查询账号: {acct['asset']}")

        status, err = await api("/api/v1/account/query", {}, token="wrong")
        assert status == 401
        print("[PASS] 错误 token 返回 401")

        status, healthy = await api("/api/v1/health", {}, token="x")
        assert status == 405  # health 是 GET, POST 返回 405 (路由已生效)
        print("[PASS] 健康检查路由已挂载 (POST 正确返回 405)")

        async with session.get(base + "/api/v1/health") as resp:
            h = await resp.json()
            assert h["status"] == "ok"
        print("[PASS] 健康检查 GET OK")

        async with session.get(base + "/api/v1/system") as resp:
            _sys = await resp.json()
            assert resp.status == 200 and _sys.get("success"), _sys
            assert _sys["data"]["semaphores"]["order"]["slots"] == 2, _sys
            assert "system" in _sys["data"] and "trade_limits" in _sys["data"]
        print("[PASS] /api/v1/system 系统资源 + 拥挤度")

        # 交易/数据分槽：4 个慢数据调用（data 槽只有 1）不能堵住账户查询（trade 槽）。
        # 旧实现是全局 Semaphore(4)：4 个 pricevol 会占满，账户查询要等 0.8s。
        mock.pricevol_delay = 0.8
        slow = [asyncio.create_task(api(
                    "/api/v1/tdx/call",
                    {"method": "get_pricevol",
                     "params": {"stock_list": ["600519.SH"]}}))
                for _ in range(4)]
        await asyncio.sleep(0.15)
        t0 = time.monotonic()
        status, _acct = await api("/api/v1/account/query", {"account_type": "stock"})
        trade_ms = (time.monotonic() - t0) * 1000.0
        assert status == 200 and trade_ms < 500, f"交易被数据堵住: {trade_ms:.0f}ms"
        slow_results = await asyncio.gather(*slow)
        assert all(r[0] == 200 for r in slow_results), slow_results
        mock.pricevol_delay = 0.0
        async with session.get(base + "/api/v1/health") as resp:
            h = await resp.json()
            assert h["semaphores"]["order"]["slots"] == 2, h
            assert h["semaphores"]["trade"]["slots"] == 1, h
            assert h["semaphores"]["data"]["slots"] == 1, h
        print(f"[PASS] 交易/数据分槽：数据慢调用期间账户查询 {trade_ms:.0f}ms")

        # 透传端点不得放行交易方法：side 经 JSON 构造不出 Side 枚举会买变卖，
        # 且该路径被归入读限流桶，绕过计划级幂等与卖前持仓校验。
        status, denied = await api("/api/v1/tdx/call",
                                   {"method": "order_stock", "params": {}})
        assert status == 403 and denied["error"]["code"] == "METHOD_FORBIDDEN", \
            f"{status} {denied}"
        print("[PASS] 透传 order_stock 被 403 拒绝")
        status, denied = await api("/api/v1/tdx/call",
                                   {"method": "cancel_order_stock", "params": {}})
        assert status == 403, f"{status} {denied}"
        print("[PASS] 透传 cancel_order_stock 被 403 拒绝")

        # 调用/成交日志属敏感数据：必须带 token（e2e 未启用缓存 → 鉴权通过后 501）
        async with session.get(base + "/api/v1/cache/logs?limit=1") as resp:
            assert resp.status == 401, f"cache/logs 未鉴权应为 401, 实际 {resp.status}"
        async with session.get(base + "/api/v1/cache/logs?limit=1",
                               headers={"Authorization": f"Bearer {TOKEN}"}) as resp:
            assert resp.status == 501, \
                f"cache/logs 带 token 应通过鉴权(501=未启用缓存), 实际 {resp.status}"
        async with session.get(base + "/api/v1/cache/trades?limit=1") as resp:
            assert resp.status == 401, f"cache/trades 未鉴权应为 401, 实际 {resp.status}"
        print("[PASS] cache/logs 与 cache/trades 需要 token")

        # 6. HTTP 下单 (新 plan_id 避免幂等冲突)
        http_plan = TradePlan(plan_id="plan_http_001",
                              orders=[Order(stock_code="600519.SH", side="buy", volume=100, price=1650.0)])
        status, placed = await api("/api/v1/plans/execute", http_plan.to_dict())
        assert status == 200 and placed["status"] == "executed", f"{status} {placed}"
        print(f"[PASS] HTTP 桥下单: {placed['status']}")

        # 6.5 撤单: 直连成功码 Value=2 不得误报失败
        # (2026-10-12 实盘假失败根因: 撤单路由只认 "1", 而直连账号成功码为 "2"——
        #  下单路径 plan_executor 早已把 "2" 当成功, 撤单路径漏了)
        mock.cancel_value, mock.cancel_msg = "2", "撤单成功"
        status, can2 = await api("/api/v1/orders/cancel",
                                 {"stock_code": "600519.SH", "order_id": "W9001"})
        assert status == 200 and can2["success"] is True, f"Value=2 被误判失败: {status} {can2}"
        assert mock.cancels[-1]["order_id"] == "W9001", "order_id 未透传到通达信"
        print("[PASS] 撤单 Value=2(直连成功码) 判定为成功")

        # 6.6 撤单: 握手报失败但当日委托终态已是撤单(BSFlag=-1) → 复核纠正为成功
        mock.cancel_value, mock.cancel_msg = "0", "撤单提交失败"
        mock.orders_list = [{"Wtbh": "W9002", "Code": "600519.SH", "BSFlag": "-1",
                             "Status": "0", "WtPrice": "1650.00", "CjPrice": "0",
                             "CjVol": "0", "WtVol": "100"}]
        status, can3 = await api("/api/v1/orders/cancel",
                                 {"stock_code": "600519.SH", "order_id": "W9002"})
        assert status == 200 and can3["success"] is True, f"{status} {can3}"
        assert can3.get("verified") is True, f"未标记为终态复核确认: {can3}"
        print("[PASS] 握手失败但终态已撤 → 复核纠正为成功 (verified)")

        # 6.7 撤单: 握手失败且委托仍在途 → 如实报失败 (不搞"永远成功")
        mock.cancel_value, mock.cancel_msg = "0", "撤单提交失败"
        mock.orders_list = [{"Wtbh": "W9003", "Code": "600519.SH", "BSFlag": "0",
                             "Status": "1", "WtPrice": "1650.00", "CjPrice": "0",
                             "CjVol": "0", "WtVol": "100"}]
        status, can4 = await api("/api/v1/orders/cancel",
                                 {"stock_code": "600519.SH", "order_id": "W9003"})
        assert status == 200 and can4["success"] is False, f"在途委托被误报撤单成功: {status} {can4}"
        print("[PASS] 握手失败且委托仍在途 → 如实报失败")
        mock.orders_list = []

        # C4: 同 plan_id 并发只执行一次（check-then-add 原子）
        race_plan = TradePlan(plan_id="plan_race_001",
                              orders=[Order(stock_code="600036.SH", side="buy",
                                            volume=100, price=10.0)])
        race_results = []

        def _race():
            race_results.append(executor.execute_plan(race_plan))

        _t1 = threading.Thread(target=_race)
        _t2 = threading.Thread(target=_race)
        _t1.start(); _t2.start(); _t1.join(); _t2.join()
        assert len(race_results) == 2
        assert sum(1 for r in race_results if r["status"] != "duplicate") == 1, race_results
        assert len([o for o in mock.orders_placed if o["stock"] == "600036.SH"]) == 1
        print("[PASS] 同 plan_id 并发只执行一次")

        # C3: 下单超时 → submitted（结果未知，绝不报 error 让上游重试）
        from src.tdx.client import TdxTimeoutError
        _orig_order = tdx.order_stock
        tdx.order_stock = lambda *a, **k: (_ for _ in ()).throw(
            TdxTimeoutError("timed out"))
        try:
            timeout_plan = TradePlan(plan_id="plan_timeout_001",
                                     orders=[Order(stock_code="000001.SZ", side="buy",
                                                   volume=100, price=10.0)])
            timeout_report = executor.execute_plan(timeout_plan)
            assert timeout_report["status"] == "executed", timeout_report
            assert timeout_report["orders"][0]["status"] == "submitted", timeout_report
        finally:
            tdx.order_stock = _orig_order
        print("[PASS] 下单超时映射为 submitted（勿重试）")

        # 硬上限：预填 299 个本秒时间戳 → 下一笔拒绝且不落 TQ
        import src.security.trade_limits as _tl
        _before = len(mock.orders_placed)
        executor.order_guard._recent.extend(
            [time.monotonic()] * _tl.HARD_MAX_TRADES_PER_SEC)
        cap_plan = TradePlan(plan_id="plan_cap_001",
                             orders=[Order(stock_code="600519.SH", side="buy",
                                           volume=100, price=10.0)])
        cap_report = executor.execute_plan(cap_plan)
        executor.order_guard._recent.clear()
        assert cap_report["orders"][0]["status"] == "rejected", cap_report
        assert "硬上限" in cap_report["orders"][0].get("message", ""), cap_report
        assert len(mock.orders_placed) == _before
        print("[PASS] 下单硬上限 299/秒拒绝且不落 TQ")

        # 撤单硬上限：预填 299 → 429
        import src.api.routes as _routes
        _routes._cancel_guard._recent.extend(
            [time.monotonic()] * _tl.HARD_MAX_TRADES_PER_SEC)
        status, body = await api("/api/v1/orders/cancel",
                                 {"stock_code": "600519.SH", "order_id": "x"})
        _routes._cancel_guard._recent.clear()
        assert status == 429, (status, body)
        print("[PASS] 撤单硬上限 299/秒返回 429")

        # 150 笔/日预警：预置 149，下一笔触发 send_message
        mock.messages = []
        executor.order_guard._day_count = 149
        executor.order_guard._warned_150 = False
        executor.order_guard._warning_pending = False
        warn_plan = TradePlan(plan_id="plan_warn_001",
                              orders=[Order(stock_code="600519.SH", side="buy",
                                            volume=100, price=10.0)])
        warn_report = executor.execute_plan(warn_plan)
        assert warn_report["orders"][0]["status"] != "error", warn_report
        assert any("150" in m for m in mock.messages), mock.messages
        print("[PASS] 150 笔/日预警已弹出")

        # 7. 止损守护 (高价不触发)
        sltp = StopLossDaemon(tdx, os.path.join(tmp, "stop_loss_state.json"), poll_interval=0.1)
        sltp.register_items([{"stock_code": "600519.SH", "entry_price": 10.0, "volume": 100,
                              "stop_loss_pct": 0.05}])
        task = asyncio.create_task(sltp.run())
        await asyncio.sleep(0.2)
        assert sltp.state_items()[0]["enabled"], "高价不应触发止损"
        print("[PASS] 止损未误触发 (现价 11.0 > 止损线 9.5)")

        # 切换 mock 为低价触发
        mock.price = 9.40
        mock.sell_value = "2"
        await asyncio.sleep(0.2)
        assert not sltp.state_items()[0]["enabled"], "止损未触发"
        assert any(o["side"] == "sell" and o["stock"] == "600519.SH" for o in mock.orders_placed), "未提交卖出"
        print("[PASS] 止损触发, 已自动提交卖出")
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # 8. 文件通道 (Windows 侧消费 pending)
    executor2 = PlanExecutor(tdx, shared)
    fs = FileSyncChannel(shared, executor2, poll_interval=0.05)
    fs_task = asyncio.create_task(fs.run())
    plan2 = TradePlan(plan_id="plan_fs_001",
                      orders=[Order(stock_code="000001.SZ", side="buy", volume=200)])
    # Windows 侧没有 write_plan, 模拟 Linux 写入 pending 文件
    os.makedirs(os.path.join(shared, "trade_plans", "pending"), exist_ok=True)
    with open(os.path.join(shared, "trade_plans", "pending", "plan_test.json"),
              "w", encoding="utf-8") as f:
        json.dump(plan2.to_dict(), f, ensure_ascii=False, indent=2)
    await asyncio.sleep(0.3)
    assert not fs._list_pending(), "文件通道未处理计划"
    reports = [f for f in os.listdir(os.path.join(shared, "execution_reports"))
               if f.endswith(".json")]
    assert reports, "未生成执行报告"
    print("[PASS] 文件通道: pending 计划被消费, 已生成执行报告")
    fs_task.cancel()
    try:
        await fs_task
    except asyncio.CancelledError:
        pass

    # 清理
    await runner.cleanup()
    mock.stop()
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    print("\n=== 全部测试通过 ===")


if __name__ == "__main__":
    asyncio.run(run_tests())
