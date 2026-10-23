"""通达信桥全量方法测试.

测试 TdxClient 的全部方法 (直接调用, 不依赖线上桥).
用 mock JSON-RPC 服务模拟通达信 17709, 验证:
  1. 所有方法能正确构造请求
  2. 参数映射正确
  3. 返回值解析正确
  4. codec 代码格式校验
  5. SQLite 缓存层

运行: cd bridge-windows && python3 tests/test_all.py
"""
import json
import glob
import logging
import os
import sys
import tempfile
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Windows 控制台默认 GBK，打印 ✅/❌ 会 UnicodeEncodeError；与 main.py 同样收口 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

# ---- mock 通达信 17709 ----
CAPTURED = []


class MockTdx(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n))
        method = body.get("method", "")
        params = body.get("params", {})
        CAPTURED.append((method, params))
        result = {"ErrorId": "0"}
        if method == "get_match_stkinfo":
            result["Value"] = [{"Code": "600519.SH", "Name": "贵州茅台"}]
        elif method == "get_stock_info":
            result.update({"Name": "贵州茅台", "Zsz": "21000"})
        elif method == "get_stock_list":
            result["Value"] = [{"Code": "600519.SH", "Name": "贵州茅台"}]
        elif method == "get_more_info":
            result.update({"ZAF": "2.5", "Zsz": "21000"})
        elif method == "get_sector_list":
            result["Value"] = [{"Code": "880301", "Name": "软件服务"}]
        elif method == "get_stock_list_in_sector":
            result["Value"] = [{"Code": "600519.SH"}]
        elif method == "get_relation":
            result["Value"] = [{"BlockCode": "880301", "BlockName": "软件服务"}]
        elif method == "get_divid_factors":
            result["Value"] = [{"Date": "20260101", "Type": "1"}]
        elif method == "get_gb_info":
            result["Value"] = {"Date": ["20260101"], "Zgb": ["100000"]}
        elif method == "get_kzz_info":
            result.update({"KZZCode": "113527", "HSCode": "600519"})
        elif method == "get_ipo_info":
            result["Value"] = [{"code": "001234", "name": "测试新股"}]
        elif method == "get_trackzs_etf_info":
            result["Value"] = [{"Code": "510300", "Name": "沪深300ETF"}]
        elif method == "get_pricevol":
            result.update({"LastClose": "10.0", "Now": "10.5"})
        elif method == "get_exday_data":
            result["Value"] = [{"Date": "20260101"}]
        elif method == "get_zdt_data":
            result["Value"] = [{"Code": "600519.SH", "ZDTStatusNow": "1"}]
        elif method == "get_trading_dates":
            result["Value"] = ["20260101", "20260102"]
        elif method == "get_financial_data":
            result["Value"] = {"600519.SH": {"FN1": "100", "FN2": "200"}}
        elif method == "get_gpjy_value":
            result["Value"] = {"600519.SH": {"GP01": "1"}}
        elif method == "get_bkjy_value":
            result["Value"] = {"880301": {"BK5": "10.0"}}
        elif method == "get_scjy_value":
            result["Value"] = {"SC01": "100"}
        elif method == "get_gp_one_data":
            result["Value"] = {"600519.SH": {"GO1": "1"}}
        elif method == "get_user_sector":
            result["Value"] = [{"Code": "ZXG", "Name": "自选股"}]
        elif method == "create_sector":
            result["Value"] = {"success": True}
        elif method == "delete_sector":
            result["Value"] = {"success": True}
        elif method == "rename_sector":
            result["Value"] = {"success": True}
        elif method == "clear_sector":
            result["Value"] = {"success": True}
        elif method == "refresh_cache":
            result["Value"] = {"success": True}
        elif method == "refresh_kline":
            result["Value"] = {"success": True}
        elif method == "download_file":
            result["Value"] = {"success": True}
        elif method == "exec_to_tdx":
            result["Value"] = {"success": True}
        elif method == "send_file":
            result["Value"] = {"success": True}
        elif method == "send_bt_data":
            result["Value"] = {"success": True}
        elif method == "formula_zb":
            result["Value"] = {"Data": {"DIF": [1.0, 2.0]}}
        elif method == "formula_xg":
            result["Value"] = {"Data": {"UP3": ["0", "1"]}}
        elif method == "formula_exp":
            result["Value"] = {"Data": {}}
        elif method == "formula_get_data":
            result["Value"] = {"Code": "600519.SH"}
        elif method == "formula_set_data":
            result["Value"] = {"success": True}
        elif method == "formula_set_data_info":
            result["Value"] = {"success": True}
        elif method == "formula_process_mul_xg":
            result["Value"] = {"600519.SH": {"Value": "1"}}
        elif method == "formula_process_mul_zb":
            result["Value"] = {"600519.SH": {"Value": "1"}}
        elif method == "formula_process_mul_exp":
            result["Value"] = {"600519.SH": {"Value": "1"}}
        else:
            result["ErrorId"] = "-1"
            result["Msg"] = f"unknown {method}"
        resp = json.dumps({"id": body.get("id", 1), "result": result}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)


def main():
    srv = HTTPServer(("127.0.0.1", 0), MockTdx)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    from src.tdx.client import TdxClient
    tdx = TdxClient(f"http://127.0.0.1:{port}/", timeout=5, max_retries=0)

    passed = 0
    failed = 0

    def test(name, fn):
        nonlocal passed, failed
        try:
            fn()
            print(f"  ✅ {name}")
            passed += 1
        except Exception as e:
            print(f"  ❌ {name}: {e}")
            failed += 1

    print("\n=== 行情数据类 ===")
    test("get_stock_info", lambda: tdx.get_stock_info("600519.SH", ["Name"]))
    test("get_more_info", lambda: tdx.get_more_info("600519.SH"))
    test("get_stock_list", lambda: tdx.get_stock_list("5"))
    test("get_sector_list", lambda: tdx.get_sector_list())
    test("get_stock_list_in_sector", lambda: tdx.get_stock_list_in_sector("880301"))
    test("get_user_sector", lambda: tdx.get_user_sector())
    test("get_match_stkinfo", lambda: tdx.get_match_stkinfo("茅台"))
    test("get_relation", lambda: tdx.get_relation("600519.SH"))
    test("get_divid_factors", lambda: tdx.get_divid_factors("600519.SH"))
    test("get_gb_info", lambda: tdx.get_gb_info("600519.SH", ["20260101"], 1))
    test("get_kzz_info", lambda: tdx.get_kzz_info("600519.SH"))
    test("get_ipo_info", lambda: tdx.get_ipo_info(0, 0))
    test("get_trackzs_etf_info", lambda: tdx.get_trackzs_etf_info("000300.CSI"))
    test("get_pricevol", lambda: tdx.get_pricevol(["600519.SH"]))
    test("get_exday_data", lambda: tdx.get_exday_data("600519.SH", 1))
    test("get_zdt_data", lambda: tdx.get_zdt_data(["600519.SH"]))
    test("get_trading_dates", lambda: tdx.get_trading_dates("5", "20260101", "20260131"))

    print("\n=== 专业数据类 ===")
    test("get_financial_data", lambda: tdx.get_financial_data(["600519.SH"], ["FN1"]))
    test("get_financial_data_by_date", lambda: tdx.get_financial_data_by_date(["600519.SH"], ["FN1"]))
    test("get_gpjy_value", lambda: tdx.get_gpjy_value(["600519.SH"], ["GP01"]))
    test("get_bkjy_value", lambda: tdx.get_bkjy_value(["880301"], ["BK5"]))
    test("get_scjy_value", lambda: tdx.get_scjy_value(["SC01"]))
    test("get_gp_one_data", lambda: tdx.get_gp_one_data(["600519.SH"], ["GO1"]))

    print("\n=== 板块管理类 ===")
    test("create_sector", lambda: tdx.create_sector("MYBLOCK", "我的板块"))
    test("delete_sector", lambda: tdx.delete_sector("MYBLOCK"))
    test("rename_sector", lambda: tdx.rename_sector("MYBLOCK", "新名字"))
    test("clear_sector", lambda: tdx.clear_sector("MYBLOCK"))

    print("\n=== 缓存/刷新类 ===")
    test("refresh_cache", lambda: tdx.refresh_cache("AG"))
    test("refresh_kline", lambda: tdx.refresh_kline(["600519.SH"], "1d"))
    test("download_file", lambda: tdx.download_file(1, "600519.SH"))

    print("\n=== 客户端控制类 ===")
    test("exec_to_tdx", lambda: tdx.exec_to_tdx("http://www.treeid"))
    test("send_file", lambda: tdx.send_file("test.txt"))
    test("send_bt_data", lambda: tdx.send_bt_data("600519.SH", ["20260101"], [["1"]], 1))

    print("\n=== 公式接口类 ===")
    test("formula_zb", lambda: tdx.formula_zb("MA", "5"))
    test("formula_xg", lambda: tdx.formula_xg("CROSS"))
    test("formula_exp", lambda: tdx.formula_exp("MACD"))
    test("formula_get_data", lambda: tdx.formula_get_data())
    test("formula_set_data", lambda: tdx.formula_set_data("600519.SH", []))
    test("formula_set_data_info", lambda: tdx.formula_set_data_info("600519.SH"))
    test("formula_process_mul_xg", lambda: tdx.formula_process_mul_xg("CROSS", ["600519.SH"]))
    test("formula_process_mul_zb", lambda: tdx.formula_process_mul_zb("MA", ["600519.SH"]))
    test("formula_process_mul_exp", lambda: tdx.formula_process_mul_exp("MACD", ["600519.SH"]))

    # 验证请求参数映射
    print("\n=== 参数映射验证 ===")
    CAPTURED.clear()
    tdx.get_stock_info("600519.SH", ["Name"])
    m, p = CAPTURED[-1]
    assert m == "get_stock_info" and p["stock_code"] == "600519.SH"
    CAPTURED.clear()
    tdx.get_financial_data(["600519.SH"], ["FN1"], "20260101", "20261231")
    m, p = CAPTURED[-1]
    assert p["stock_list"] == ["600519.SH"] and p["table_list"] == ["FN1"], p
    assert "field_list" not in p, "pro-data 必须发 table_list（DLL 只认这个键）"
    CAPTURED.clear()
    tdx.get_scjy_value(["SC01"])
    m, p = CAPTURED[-1]
    assert p["table_list"] == ["SC01"] and "field_list" not in p, p
    CAPTURED.clear()
    tdx.get_gp_one_data(["600519.SH"], ["GO1"])
    m, p = CAPTURED[-1]
    assert p["table_list"] == ["GO1"] and "field_list" not in p, p
    CAPTURED.clear()
    tdx.formula_process_mul_xg("CROSS", ["600519.SH"], return_count=3)
    m, p = CAPTURED[-1]
    assert p["formula_name"] == "CROSS" and p["return_count"] == 3
    print("  ✅ 参数映射正确")

    # codec 验证
    print("\n=== codec 代码格式校验 ===")
    from src.tdx.codec import check_stock_code_format, normalize_stock_code
    assert check_stock_code_format("600519.SH")
    assert not check_stock_code_format("600519")
    assert normalize_stock_code("600519") == "600519.SH"
    print("  ✅ codec 校验正确")

    # parser 方向解析回归 (BSFlag=0 是合法买入值, 不能被 falsy 吞掉)
    print("\n=== parser 委托方向解析 ===")
    from src.tdx.parser import parse_order
    assert parse_order({"BSFlag": 0, "Status": 3, "WtVol": "217", "CjVol": "217",
                        "Code": "688256.SH", "WtPrice": "1147.08",
                        "CjPrice": "1147.08", "Wtbh": "1", "Time": "103542"})["side"] == "buy"
    assert parse_order({"BSFlag": 1, "Status": 1, "WtVol": "100", "Code": "600664.SH",
                        "Wtbh": "2", "Time": "142720"})["side"] == "sell"
    assert parse_order({"Code": "600664.SH", "WtVol": "100", "Wtbh": "3"})["side"] == "cancel"
    assert parse_order({"BSFlag": 0, "Code": "x", "WtVol": "100", "Wtbh": "4"})["status"] == "submitted"
    print("  ✅ parser 方向解析正确")

    # SQLite 缓存验证
    print("\n=== SQLite 缓存层 ===")
    import tempfile
    from src.db.cache_db import CacheDb
    db = CacheDb(os.path.join(tempfile.mkdtemp(), "t.db"))
    db.save_kline("600519.SH", "1d", [{"date": "20260812", "close": 1850.0}])
    assert len(db.get_kline("600519.SH")) == 1
    db.log_call("get_stock_info", "600519.SH", {}, {"Name": "茅台"}, category="query",
                status="ok", duration_ms=5.0)
    assert len(db.get_logs()) == 1
    db.log_trade("plan_1", "order_execute", "600519.SH", "buy", 100, 1850.0,
                 "W123", "submitted", "已提交")
    assert len(db.get_trade_logs()) == 1
    print("  ✅ SQLite 缓存层正确")
    db.close()

    # 桥自带止损守护的默认值必须是「关」。
    # 它是个独立卖出者（触发即市价卖出），守护单已由 QuantMind 的 sltp_executor
    # 承担；两者同触发即对同一账户同一持仓超卖。
    # 注意**两处默认值各自独立生效**：Config.get(dotted, default) 在 config.yaml
    # 存在但缺该段时返回调用方给的 default —— 部署包通常自带 config.yaml，
    # 所以 main.py 那一处才是真正生效的门，只关 DEFAULT_CONFIG 等于没关。
    print("\n=== 止损守护默认关闭 ===")
    import yaml
    from src.utils.config import DEFAULT_CONFIG
    _d = yaml.safe_load(DEFAULT_CONFIG)
    assert _d["sltp_daemon"]["enabled"] is False, "DEFAULT_CONFIG 里 sltp_daemon.enabled 应为 false"
    _main_src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py"), encoding="utf-8").read()
    assert 'cfg.get("sltp_daemon.enabled", False)' in _main_src, "main.py 的 sltp_daemon.enabled 默认值应为 False"
    assert 'cfg.get("sltp_daemon.enabled", True)' not in _main_src, "main.py 仍有 True 默认值"
    print("  ✅ 两处默认值均为「关」")

    # /stats 的缓存计数绝不许在事件循环里同步跑。
    # `cache_db.get_stats()` = 7 个 COUNT(*)，其一打在 tdx_log（生产 512 万行 /
    # 6.9GB，实测 3-52s）。它会连 `/api/v1/health` 一起按住 ⇒ Windows 看门狗
    # `-TimeoutSec 5` 连续 2 次失败即杀桥重启 ⇒ 桥 ui.auto_open 再开一个控制台标签
    # ⇒ 标签每 15s 又打一次 /stats。2026-10-13 单日重启 378 次，每次 kill 之前
    # 3s 内都有一次桥内耗时 8-52s 的 /stats（bridge.log 无崩溃 traceback）。
    print("\n=== /stats 不得在事件循环里跑 COUNT(*) ===")
    import ast
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _routes_src = open(os.path.join(_root, "src", "api", "routes.py"), encoding="utf-8").read()
    _tree = ast.parse(_routes_src)
    _stats_fn = next(
        n for n in ast.walk(_tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "stats"
    )
    _sync_calls = [
        n for n in ast.walk(_stats_fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr in ("get_stats", "get_cache_disk")
    ]
    assert not _sync_calls, (
        "stats 处理器里仍有同步的 cache_db.get_stats()/get_cache_disk() 调用——"
        "COUNT(*) 会按住事件循环 3-52s，连 /health 一起停摆，触发看门狗杀桥的"
        "自毁循环。必须经 asyncio.to_thread(_cache_stats_sync, ...) 出循环。"
    )
    assert ("run_in_executor(\n                _CACHE_EXECUTOR, _cache_stats_sync"
            in _routes_src or
            "run_in_executor(_CACHE_EXECUTOR, _cache_stats_sync" in _routes_src or
            "asyncio.to_thread(_cache_stats_sync" in _routes_src), \
        "stats 必须把缓存计数丢出事件循环（_cache_stats_sync）"
    assert "CACHE_STATS_TTL_S = 60.0" in _routes_src, \
        "_cache_stats_sync 的 TTL 缓存（60s）也一并守着——计数本身就该廉价，别每 15s 重算一遍"
    print("  ✅ 缓存计数走线程 + 60s TTL 缓存")

    # 控制台自动打开必须节流：每重启一次开一个标签 = 每重启一次多一份 /stats 负载。
    print("\n=== 控制台自动打开去重（每重启一次不再多开一个标签）===")
    assert ".ui_opened_at" in _main_src, "main.py 的 _auto_open_ui 缺少去重节流时间戳"
    assert "min_interval_sec" in _main_src, "main.py 的 _auto_open_ui 缺少节流间隔参数"
    print("  ✅ 自动打开控制台已节流")

    # 访问日志降噪：每请求一行会把 bridge.log 撑到 GB 级。
    print("\n=== aiohttp.access 日志降噪 ===")
    assert 'logging.getLogger("aiohttp.access").setLevel(logging.WARNING)' in _main_src, \
        "aiohttp.access 未降到 WARNING，bridge.log 会被每请求一行撑爆"
    print("  ✅ 访问日志只保留 WARNING 以上")

    # 透传端点不得放行交易方法：绕过幂等/卖前校验/写限流，且 side 会买变卖。
    print("\n=== 透传端点不得放行交易方法 ===")
    assert "FORBIDDEN_CALL_METHODS" in _routes_src, \
        "routes.py 缺少交易方法禁用名单 FORBIDDEN_CALL_METHODS"
    _deny_block = _routes_src.split("FORBIDDEN_CALL_METHODS", 1)[1].split("}", 1)[0]
    for _m in ("order_stock", "cancel_order_stock"):
        assert _m in _deny_block, f"透传仍放行交易方法: {_m}"
    print("  ✅ order_stock / cancel_order_stock 已封")

    # 调用/成交日志含标的、方向、委托号——不能再匿名可读。
    print("\n=== 调用/成交日志需鉴权 ===")
    assert 'path.startswith("/api/v1/cache/")' not in _routes_src, \
        "cache/ 前缀仍被整体豁免鉴权"
    _ro = _routes_src.split("read_only = (", 1)[1].split(")", 1)[0]
    assert "/api/v1/cache/logs" not in _ro, "cache/logs 仍在匿名白名单"
    assert "/api/v1/cache/trades" not in _ro, "cache/trades 仍在匿名白名单"
    print("  ✅ cache/logs 与 cache/trades 已移出匿名白名单")

    # 交易/数据分槽：4 个并发数据调用不能占满交易槽。
    print("\n=== 交易/数据双信号量 ===")
    assert "TRADE_SEM_SLOTS = 1" in _routes_src, "缺少 TRADE_SEM_SLOTS=1"
    assert "DATA_SEM_SLOTS = 1" in _routes_src, "缺少 DATA_SEM_SLOTS=1"
    assert "ORDER_SEM_SLOTS = 2" in _routes_src, "缺少 ORDER_SEM_SLOTS=2"
    assert "TRADE_METHODS" in _routes_src, "缺少交易方法分类表"
    assert "kind=call_kind(method)" in _routes_src, "/tdx/call 未按方法分类"
    assert "semaphores_stats()" in _routes_src, "缺少信号量观测"
    print("  ✅ order=2 / trade=1 / data=1 + 方法分类 + /health 观测")

    # 调用/交易日志必须离开事件循环：后台队列 + 批量 commit。
    print("\n=== 日志后台批量写入 ===")
    assert "_LogWriter" in _routes_src and "put_nowait" in _routes_src
    assert "log_calls_bulk" in _routes_src and "log_trades_bulk" in _routes_src
    from src.db.cache_db import CacheDb as _CacheDb
    _ldb = _CacheDb(os.path.join(tempfile.mkdtemp(), "log.db"))
    _n = _ldb.log_calls_bulk([
        {"method": "get_pricevol", "symbol": "600519.SH",
         "params": {"stock_list": ["600519.SH"]}, "result": {"Value": {}},
         "duration_ms": 1.2, "created_at": "2026-10-22T10:00:00.000"}
        for _ in range(3)
    ])
    assert _n == 3 and _ldb.get_stats()["tdx_log"] == 3, _n
    _n2 = _ldb.log_trades_bulk([{
        "plan_id": "p1", "event": "order_execute", "symbol": "600519.SH",
        "side": "buy", "volume": 100, "price": 10.0, "order_id": "W1",
        "status": "submitted", "message": "", "detail": {},
    }])
    assert _n2 == 1 and _ldb.get_stats()["trade_log"] == 1, _n2
    _ldb.close()
    print("  ✅ 事件循环只入队，SQLite 批量提交")

    # 交易硬上限：299/秒、1999/日，150 笔预警；改了就停机。
    print("\n=== 交易硬上限 299/1999 + 150 预警 ===")
    from src.security.trade_limits import (
        HARD_MAX_TRADES_PER_SEC, HARD_MAX_TRADES_PER_DAY,
        WARN_AT_TRADES_PER_DAY, TradeRateGuard, HardLimitTampered,
        verify_hard_limits,
    )
    assert (HARD_MAX_TRADES_PER_SEC, HARD_MAX_TRADES_PER_DAY) == (299, 1999)
    assert WARN_AT_TRADES_PER_DAY == 150
    verify_hard_limits()
    _now = datetime(2026, 1, 5, 9, 30, 0)
    _g = TradeRateGuard()
    for _i in range(299):
        _ok, _ = _g.allow(_now, mono=1000.0)
        assert _ok, _i
    _ok, _reason = _g.allow(_now, mono=1000.0)
    assert not _ok and "299" in _reason, _reason
    _g2 = TradeRateGuard()
    for _i in range(150):
        _ok, _ = _g2.allow(_now, mono=2000.0)
        assert _ok, _i
    _warn = _g2.consume_warning()
    assert _warn and "150" in _warn, _warn
    assert _g2.consume_warning() == ""
    _g3 = TradeRateGuard(max_per_sec=300)
    try:
        _g3._check_immutable()
        raise AssertionError("篡改硬上限未被拒绝")
    except HardLimitTampered:
        pass
    assert "verify_hard_limits()" in _main_src
    _pe_src = open(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "src", "executor", "plan_executor.py"), encoding="utf-8").read()
    assert "_dedup_lock" in _pe_src and "TdxTimeoutError" in _pe_src
    assert "consume_warning" in _pe_src
    print("  ✅ 常量/启动自检/篡改停机/150 预警")

    # 日计数持久化：重启不清零；状态文件损坏 fail-closed
    _sp = os.path.join(tempfile.mkdtemp(), "limits.json")
    _today = datetime.now()
    _gp = TradeRateGuard(state_path=_sp)
    _ok, _ = _gp.allow(_today, mono=3000.0)
    assert _ok
    _gp2 = TradeRateGuard(state_path=_sp)
    assert _gp2.stats()["day_count"] == 1, _gp2.stats()
    with open(_sp, "w", encoding="utf-8") as _f:
        _f.write("not-json")
    _gp3 = TradeRateGuard(state_path=_sp)
    _ok, _reason = _gp3.allow(_today, mono=3001.0)
    assert not _ok and "fail-closed" in _reason, _reason
    print("  ✅ 日计数落盘/重启续计/损坏 fail-closed")

    # 系统资源指标（UI /api/v1/system 用）
    print("\n=== 系统资源指标 ===")
    from src.utils.system_metrics import SystemMetrics
    _sys = SystemMetrics().sample()
    for _k in ("cpu_percent", "process_cpu_percent", "mem_total", "mem_used",
               "process_rss", "disk_total", "disk_free", "threads", "pid",
               "uptime_s"):
        assert _k in _sys, _k
    assert "run_in_executor(_PROBE_EXECUTOR" in _routes_src
    assert "/api/v1/system" in _routes_src
    print("  ✅ SystemMetrics + /api/v1/system 已挂载")

    # 日志按大小滚动：bridge.log 曾无限增长到 1.5GB。
    print("\n=== bridge.log 按大小滚动 ===")
    from src.utils.log_setup import build_handlers
    _logdir = tempfile.mkdtemp()
    _handlers = build_handlers(_logdir, max_bytes=1024, backup_count=2)
    _lg = logging.getLogger("bridge-rotate-test")
    _lg.setLevel(logging.INFO)
    _lg.propagate = False
    for _h in _handlers:
        _lg.addHandler(_h)
    for _i in range(30):
        _lg.info("x" * 100)
    for _h in _handlers:
        _h.close()
        _lg.removeHandler(_h)
    assert os.path.exists(os.path.join(_logdir, "bridge.log")), "bridge.log 未生成"
    assert os.path.exists(os.path.join(_logdir, "bridge.log.1")), \
        "bridge.log 未滚动——RotatingFileHandler 没生效"
    print("  ✅ 超出 maxBytes 后已生成 bridge.log.1")

    # 缓存日志留存清理：按时间 + 行数上限分批删除。
    print("\n=== 缓存日志留存清理 ===")
    from src.db.cache_db import CacheDb
    _cdb = CacheDb(os.path.join(tempfile.mkdtemp(), "cache.db"))
    _old_ts = (datetime.now() - timedelta(days=60)).isoformat(timespec="milliseconds")
    for _i in range(20):
        _cdb._conn.execute(
            "INSERT INTO tdx_log (method, created_at) VALUES (?,?)", ("old", _old_ts))
    for _i in range(5):
        _cdb.log_call("new", "", {}, {})
    _cdb._conn.commit()
    _candidates = _cdb.count_prune_candidates(tdx_retention_days=30, tdx_max_rows=3,
                                              trade_retention_days=365)
    assert _candidates["tdx_log"] == 22, f"dry-run 候选数不对: {_candidates}"
    _deleted = _cdb.prune_logs(tdx_retention_days=30, tdx_max_rows=3,
                               trade_retention_days=365)
    _stats = _cdb.get_stats()
    assert _stats["tdx_log"] == 3, f"应只保留最新 3 行, 实际 {_stats['tdx_log']}"
    assert _deleted["tdx_log"] == 22, f"应删除 22 行, 实际 {_deleted['tdx_log']}"
    _cdb.close()
    print("  ✅ 超期 + 超额日志已分批清理")

    # 缓存库损坏自愈：malformed/非库文件时隔离旧文件并重建，绝不带病运行。
    print("\n=== 缓存库损坏自愈 ===")
    _dir = tempfile.mkdtemp()
    _dbp = os.path.join(_dir, "cache.db")
    _db1 = CacheDb(_dbp)
    _db1.log_call("probe", "", {}, {})
    _db1.close()
    with open(_dbp, "r+b") as _fh:  # 写坏文件头，模拟断电/页损坏
        _fh.seek(0)
        _fh.write(b"NOT-A-SQLITE-DB" * 8)
    _db2 = CacheDb(_dbp)
    _db2.log_call("after_heal", "", {}, {})
    _rows = _db2.get_logs(limit=10)
    assert any(r["method"] == "after_heal" for r in _rows), "重建后写入失败"
    assert glob.glob(_dbp + ".corrupt-*"), "损坏库未被隔离留证"
    _db2.close()
    print("  ✅ 损坏库已隔离重建，写入恢复")

    # token 优先级：BRIDGE_AUTH_TOKEN 环境变量 > config.yaml 明文。
    # 2026-10-23 实盘事故：启动会话里没有该环境变量 ⇒ 桥自动生成新 token
    # 并写回 config，平台/采集器全员 401。环境变量必须优先。
    print("\n=== token 环境变量优先 ===")
    import yaml as _yaml
    from src.utils.config import Config
    _tokdir = tempfile.mkdtemp()
    _cfgpath = os.path.join(_tokdir, "config.yaml")
    with open(_cfgpath, "w", encoding="utf-8") as _fh:
        _yaml.safe_dump({"auth": {"token": "${BRIDGE_AUTH_TOKEN}"}}, _fh)
    _old_env = os.environ.get("BRIDGE_AUTH_TOKEN")
    try:
        os.environ["BRIDGE_AUTH_TOKEN"] = "A" * 64
        assert Config(_cfgpath).token() == "A" * 64, "环境变量未优先"
        os.environ.pop("BRIDGE_AUTH_TOKEN", None)
        try:
            Config(_cfgpath).token()
            raise AssertionError("token 为空时应抛 ValueError")
        except ValueError:
            pass
    finally:
        if _old_env is None:
            os.environ.pop("BRIDGE_AUTH_TOKEN", None)
        else:
            os.environ["BRIDGE_AUTH_TOKEN"] = _old_env
    print("  ✅ 环境变量优先，缺失时 fail-fast")

    # TdxAiData 封装：包不可用时不炸；Token 写 ini 保留其余配置；方法白名单。
    print("\n=== TdxAiData 封装 ===")
    from src.tdx.aidata import AidataService, AidataUnavailable
    _adir = tempfile.mkdtemp()
    _aini = os.path.join(_adir, "TdxAiData.ini")
    with open(_aini, "w", encoding="utf-8") as _fh:
        _fh.write("[Token]\ntoken=\n\n[TcHqHost]\nHostNum=1\n")
    _svc = AidataService(vendor_dir=os.path.join(_adir, "nope"), ini_path=_aini)
    _ast = _svc.status()
    assert set(_ast) >= {"available", "token_configured", "ini_path", "methods"}
    assert _ast["token_configured"] is False
    _svc.set_token("K" * 32)
    assert _svc.read_token() == "K" * 32, "token 未写入"
    assert "HostNum=1" in open(_aini, encoding="utf-8").read(), "其余配置被覆盖"
    _svc.set_token("")
    assert _svc.read_token() == ""
    _off = AidataService(enabled=False, ini_path=_aini)
    assert _off.status()["available"] is False
    try:
        _off.call("get_minute_data", {})
        raise AssertionError("未启用时 call 应抛错")
    except AidataUnavailable:
        pass
    try:
        _off.call("order_stock", {})
        raise AssertionError("非白名单方法应被拒")
    except ValueError:
        pass
    print("  ✅ 状态 / Token 写入 / 白名单校验")

    # tqs 失败方式是「打印」不是抛异常：必须转成明确异常，不能只回 result:null
    class _PrintFailTqs:
        @staticmethod
        def get_minute_data(**kwargs):
            print("[错误] TokenKey Insufficient Or Key Error")
            return None

    _svc_err = AidataService(enabled=False, ini_path=_aini)
    _svc_err._tqs = _PrintFailTqs()
    try:
        _svc_err.call("get_minute_data", {})
        raise AssertionError("应抛 AidataTokenRequired")
    except Exception as _exc:  # noqa: BLE001
        assert type(_exc).__name__ == "AidataTokenRequired", type(_exc).__name__
    assert "TokenKey" in _svc_err.status()["last_error"]
    print("  ✅ tqs 打印式失败→明确异常（Key 被拒）")

    # 实时订阅：去重大写、100 上限、无 Key 拒绝 aidata 模式、启动失败置 error。
    print("\n=== 实时订阅管理 ===")
    from src.tdx.subscription import SubscriptionManager

    class _FakeAidata:
        def __init__(self, token=False):
            self.token = token
            self.subscribed = []

        def status(self):
            return {"available": True, "token_configured": self.token,
                    "subscribed": list(self.subscribed), "load_error": ""}

        def subscribe(self, codes, cb):
            self.subscribed = list(codes)

        def unsubscribe(self, codes):
            self.subscribed = []

    _sdir = tempfile.mkdtemp()
    _mgr = SubscriptionManager(
        aidata=_FakeAidata(token=False),
        state_file=os.path.join(_sdir, "sub.json"),
        snapshot_fetcher=None, max_symbols=100)
    _mgr.set_config(codes=["600519.SH", "600519.sh", "000001.sz"])
    assert _mgr.status()["codes"] == ["600519.SH", "000001.SZ"], "去重/大写失败"
    try:
        _mgr.set_config(enabled=True, mode="aidata")
        raise AssertionError("无 Key 启用 aidata 模式应被拒")
    except ValueError as _exc:
        assert "Key" in str(_exc)
    _mgr.set_config(codes=[f"{i:06d}.SZ" for i in range(150)])
    assert _mgr.status()["count"] == 100, "未截断到 100 只"
    try:
        _mgr.set_config(enabled=True, mode="tq")
    except Exception:
        pass
    assert _mgr.status()["state"] in ("error", "stopped")
    _mgr2 = SubscriptionManager(aidata=_FakeAidata(token=True),
                                state_file=os.path.join(_sdir, "sub2.json"))
    try:
        _mgr2.set_config(enabled=True)
        raise AssertionError("空列表启用应被拒")
    except ValueError:
        pass
    # 通道上限：免费 tq 硬限 100；付费 aidata 可配（默认 500），切换时自动截断
    _mgr3 = SubscriptionManager(aidata=_FakeAidata(token=True),
                                state_file=os.path.join(_sdir, "sub3.json"),
                                max_symbols=100, max_symbols_aidata=500)
    _mgr3.set_config(enabled=False, mode="aidata",
                     codes=[f"{i:06d}.SZ" for i in range(600)])
    assert _mgr3.status()["count"] == 500, _mgr3.status()["count"]
    assert _mgr3.status()["max_symbols"] == 500
    _mgr3.set_config(mode="tq")
    assert _mgr3.status()["count"] == 100, "切回 tq 未截断到 100"
    print("  ✅ 去重大写 / tq≤100 / aidata≤500 / 切换截断 / 无 Key 拒绝")

    # 限流：已鉴权走独立高预算（平台与管理端共用出口 IP，不能互相挤爆）
    print("\n=== 限流：已鉴权独立预算 ===")
    from src.security.rate_limiter import RateLimiter
    _rl = RateLimiter(per_minute=5, authed_per_minute=50,
                      local_per_minute=100, write_per_minute=2)
    for _ in range(30):
        assert _rl.check("1.2.3.4", "/api/v1/stats", True) is None, "已鉴权被 5/分钟掐了"
    for _ in range(5):
        _rl.check("5.6.7.8", "/api/v1/data", False)
    assert _rl.check("5.6.7.8", "/api/v1/data", False) is not None, "未鉴权第 6 次应被拒"
    for _ in range(50):
        assert _rl.check("127.0.0.1", "/api/v1/stats", True) is None, "本机高预算失效"
    print("  ✅ 已鉴权 6000 / 未鉴权 per_minute / 本机独立预算")

    srv.shutdown()
    print(f"\n=== 结果: {passed} 通过, {failed} 失败 ===")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
