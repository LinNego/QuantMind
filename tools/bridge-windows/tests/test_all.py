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
import logging
import os
import sys
import tempfile
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
    assert p["stock_list"] == ["600519.SH"] and p["field_list"] == ["FN1"]
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
    assert "asyncio.to_thread(\n                _cache_stats_sync" in _routes_src or \
        "asyncio.to_thread(_cache_stats_sync" in _routes_src, \
        "stats 必须把缓存计数丢进线程（_cache_stats_sync）"
    assert "CACHE_STATS_TTL_S = 60.0" in _routes_src, \
        "_cache_stats_sync 的 TTL 缓存（60s）也一并守着——计数本身就该廉价，别每 15s 重算一遍"
    print("  ✅ 缓存计数走线程 + 60s TTL 缓存")

    # 控制台自动打开必须节流：每重启一次开一个标签 = 每重启一次多一份 /stats 负载。
    print("\n=== 控制台自动打开去重（每重启一次不再多开一个标签）===")
    assert ".ui_opened_at" in _main_src, "main.py 的 _auto_open_ui 缺少去重节流时间戳"
    assert "min_interval_sec" in _main_src, "main.py 的 _auto_open_ui 缺少节流间隔参数"
    print("  ✅ 自动打开控制台已节流")

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

    srv.shutdown()
    print(f"\n=== 结果: {passed} 通过, {failed} 失败 ===")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
