import asyncio
import json
import logging
import os
import queue
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from aiohttp import web

from ..core.trade_plan import TradePlan
from ..core.types import OrderStatus, Side
from ..executor.order_tracker import OrderTracker
from ..executor.plan_executor import PlanExecutor
from ..security.trade_limits import TradeRateGuard
from ..tdx.client import TdxClient, TdxError, TdxTimeoutError
from ..tdx import parser
from ..utils.auth import verify_token
from ..utils.system_metrics import SystemMetrics

log = logging.getLogger(__name__)

# 专用线程池：绝不让 TQ 长调用/孤儿线程占满默认池，把 /health/账户/撤单拖死。
# TQ 4 个 worker 与 order=2 + trade=1 + data=1 的并发上限对齐；health/cache 各自独立。
_TQ_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="tq-call")
_HEALTH_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tq-health")
_CACHE_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="cache-write")
_PROBE_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="probe")
_system_metrics = SystemMetrics()
_BRIDGE_STARTED_AT = datetime.now().astimezone().isoformat(timespec="seconds")
_collector_cache = {"ts": 0.0, "data": None}

# 通达信调用超时 (秒) - 防止单个阻塞调用挂死整个桥
TDX_CALL_TIMEOUT = 8.0

# 交易/数据分槽（2026-10-22 实测：4 个并发全市场 pricevol 会把全局 Semaphore(4)
# 占满，快照/下单要等 3.6s；TQ 17709 本身支持并发，堵点是桥槽）。
# trade=2 保证下单/撤单/账户/委托查询永远有槽；data=1 只放一个数据调用进 TQ，
# 配合采集器分块 500（~70ms/块），把数据对交易的影响压到单块级别。
# TQ 并发深度实测约 4；2+1 留 1 个余量给健康检查/热集。
ORDER_SEM_SLOTS = 2   # 下单/撤单：独立高优先，绝不被账户/行情查询占用
TRADE_SEM_SLOTS = 1   # 账户/持仓/委托查询（含 /stats）
DATA_SEM_SLOTS = 1    # 行情/采集/推送
ORDER_CALL_TIMEOUT = 20.0
TRADE_CALL_TIMEOUT = 20.0
DATA_CALL_TIMEOUT = 15.0

# 只读/写入类交易方法：走 trade 槽，绝不被行情数据挤占。
TRADE_METHODS = frozenset({
    "order_stock", "cancel_order_stock", "query_stock_asset",
    "query_stock_positions", "query_stock_orders", "stock_account",
})

_order_semaphore = None
_trade_semaphore = None
_data_semaphore = None

# 信号量观测（全部在事件循环内更新，无需加锁）
_sem_stats = {
    k: {"inflight": 0, "waiting": 0, "calls": 0, "timeouts": 0,
        "wait_ms_last": 0.0, "wait_ms_max": 0.0,
        "exec_ms_last": 0.0, "exec_ms_max": 0.0}
    for k in ("order", "trade", "data")
}
_latencies = {k: deque(maxlen=200) for k in ("order", "trade", "data")}
_health_stats = {"calls": 0, "failures": 0, "last_ms": 0.0, "max_ms": 0.0}
_loop_lag = {"last_ms": 0.0, "max_ms": 0.0}


async def _loop_lag_task() -> None:
    """事件循环 lag 心跳：0.1s 睡眠实际晚了多少。持续 >200ms 说明循环被同步代码按住。"""
    while True:
        t0 = time.monotonic()
        await asyncio.sleep(0.1)
        lag = max(0.0, (time.monotonic() - t0 - 0.1) * 1000.0)
        _loop_lag["last_ms"] = round(lag, 1)
        _loop_lag["max_ms"] = max(_loop_lag["max_ms"], round(lag, 1))


# 缓存库维护：每日收盘后清理 tdx_log；盘中超过 2GB 也强制清一次。
# 生产 2026-10-23：tdx_cache.db 长到 9.7GB 后页损坏（malformed），
# 缓存与调用审计全部写失败且每笔调用刷一条 traceback。缓存必须自我设限。
CACHE_PRUNE_HOUR = 15
CACHE_PRUNE_MINUTE = 5
CACHE_MAX_BYTES = 2 * 1024 ** 3
CACHE_MAINTENANCE_INTERVAL_S = 60
CACHE_FORCE_PRUNE_COOLDOWN_S = 3600


async def _cache_maintenance_task(cache_db) -> None:
    """缓存库日清 + 超限强制清理（在独立线程跑，绝不占用事件循环）。"""
    last_prune_day = ""
    last_force = 0.0
    integrity_checked = False
    while True:
        try:
            await asyncio.sleep(CACHE_MAINTENANCE_INTERVAL_S)
            if not integrity_checked:
                integrity_checked = True
                if await asyncio.to_thread(cache_db.heal_if_corrupt):
                    log.critical("缓存库自检发现损坏，已隔离重建")
            now = datetime.now()
            day = now.strftime("%Y%m%d")
            after_close = (now.hour > CACHE_PRUNE_HOUR or
                           (now.hour == CACHE_PRUNE_HOUR
                            and now.minute >= CACHE_PRUNE_MINUTE))
            size = cache_db.get_cache_disk()
            if after_close and last_prune_day != day:
                reason = "每日收盘清理"
            elif (size > CACHE_MAX_BYTES
                  and time.time() - last_force > CACHE_FORCE_PRUNE_COOLDOWN_S):
                reason = f"缓存超限 {size / 1024 ** 3:.2f}GB"
            else:
                continue
            deleted = await asyncio.to_thread(cache_db.prune_logs)
            last_prune_day = day
            last_force = time.time()
            log.info("缓存清理(%s): 删除 %s，剩余 %.2fGB", reason, deleted,
                     cache_db.get_cache_disk() / 1024 ** 3)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("缓存维护失败(下轮重试)", exc_info=True)


def _pct(vals, p: float) -> float:
    if not vals:
        return 0.0
    v = sorted(vals)
    idx = min(len(v) - 1, max(0, int(round((p / 100.0) * (len(v) - 1)))))
    return round(v[idx], 1)


def _get_semaphores():
    global _order_semaphore, _trade_semaphore, _data_semaphore
    if _order_semaphore is None:
        _order_semaphore = asyncio.Semaphore(ORDER_SEM_SLOTS)
    if _trade_semaphore is None:
        _trade_semaphore = asyncio.Semaphore(TRADE_SEM_SLOTS)
    if _data_semaphore is None:
        _data_semaphore = asyncio.Semaphore(DATA_SEM_SLOTS)
    return {"order": _order_semaphore, "trade": _trade_semaphore,
            "data": _data_semaphore}


def call_kind(method: str) -> str:
    """按方法名分类：交易类走 trade 槽，其余走 data 槽。唯一出处。"""
    return "trade" if method in TRADE_METHODS else "data"


def semaphores_stats() -> dict:
    """给 /stats 用的信号量快照（含配额与在途/等待）。"""
    sems = _get_semaphores()
    slots = {"order": ORDER_SEM_SLOTS, "trade": TRADE_SEM_SLOTS,
             "data": DATA_SEM_SLOTS}
    out = {}
    for k, n in slots.items():
        lats = list(_latencies[k])
        out[k] = {**_sem_stats[k], "slots": n, "available": sems[k]._value,
                  "exec_ms_p50": _pct(lats, 50), "exec_ms_p95": _pct(lats, 95)}
    return out


async def _call_tdx(func, *args, kind: str = "data", timeout: float | None = None):
    """在后台线程跑通达信调用, 带超时 + 交易/数据分槽, 防止阻塞事件循环.

    kind="trade"：下单/撤单/账户/委托查询，2 个专用槽；
    kind="data" ：行情/财务/板块/采集，2 个槽（与热集/采集任务共享）。
    数据调用再忙，也不会占掉交易槽。
    """
    if kind not in ("order", "trade", "data"):
        raise ValueError(f"非法 kind: {kind}")
    sems = _get_semaphores()
    sem = sems[kind]
    stat = _sem_stats[kind]
    stat["calls"] += 1
    queued = sem.locked()
    acquired = False
    if queued:
        stat["waiting"] += 1
    t0 = time.monotonic()
    try:
        async with sem:
            acquired = True
            waited_ms = (time.monotonic() - t0) * 1000.0
            if queued:
                stat["waiting"] -= 1
            stat["wait_ms_last"] = waited_ms
            stat["wait_ms_max"] = max(stat["wait_ms_max"], waited_ms)
            if waited_ms > 1000.0:
                log.error("[TQ] %s 槽等待 %.0fms（可能被数据面挤占）", kind, waited_ms)
            elif waited_ms > 200.0:
                log.warning("[TQ] %s 槽等待 %.0fms", kind, waited_ms)
            stat["inflight"] += 1
            t_exec = time.monotonic()
            try:
                loop = asyncio.get_running_loop()
                return await asyncio.wait_for(
                    loop.run_in_executor(_TQ_EXECUTOR, func, *args),
                    timeout=timeout if timeout is not None else (
                        ORDER_CALL_TIMEOUT if kind == "order"
                        else TRADE_CALL_TIMEOUT if kind == "trade"
                        else DATA_CALL_TIMEOUT),
                )
            except asyncio.TimeoutError:
                stat["timeouts"] += 1
                raise TdxError("通达信响应超时")
            finally:
                stat["inflight"] -= 1
                exec_ms = (time.monotonic() - t_exec) * 1000.0
                stat["exec_ms_last"] = round(exec_ms, 1)
                stat["exec_ms_max"] = max(stat["exec_ms_max"], round(exec_ms, 1))
                _latencies[kind].append(exec_ms)
    except BaseException:
        # 取消/异常发生在拿到槽之前：把等待计数还回去
        if queued and not acquired and stat["waiting"] > 0:
            stat["waiting"] -= 1
        raise

# stats 账户缓存 (避免网页高频轮询压垮通达信)
_stats_cache = {"ts": 0, "account": None}

# stats 缓存行数缓存。COUNT(*) 必须 **出事件循环**（见 `_cache_stats_sync` 注释），
# 而它本身又慢（生产库 6.9GB / tdx_log 512 万行，单次 COUNT 3-52s），所以再加一层
# TTL：控制台每 15s 轮询一次，60s 内复用同一份计数，把每分钟 4 次重算降到 1 次。
CACHE_STATS_TTL_S = 60.0
_cache_stats_cache = {"ts": 0.0, "data": None}

# 交易方法只能走 /api/v1/plans/execute 与 /api/v1/orders/cancel。
# 透传路径（/api/v1/tdx/call）不做幂等去重、不做卖前持仓校验，且被归入
# **读**限流桶（600/min）——放行等于给每个 token 一条绕过交易闸的旁路。
# 更危险的是：`order_stock` 的 `side` 参数经 JSON 传进来构造不出 Side 枚举，
# `TdxClient.call` 会一律落到 STOCK_SELL（买变卖）。这里直接封死。
FORBIDDEN_CALL_METHODS = frozenset({"order_stock", "cancel_order_stock", "call"})

# 高频数据方法的审计只存摘要（不存 400KB 的 params/result 全文），
# 否则 30-40 万行/日 × 2-3KB 会把 tdx_log 重新撑到 8-12GB。
HIGH_FREQ_DATA_METHODS = frozenset(
    {"get_pricevol", "get_market_snapshot", "get_market_data"})


# 撤单硬上限：与下单独立计数（撤单是减风险动作，不被下单额度连坐）。
_cancel_guard = TradeRateGuard()


def json_error(status: int, code: str, message: str, details=None):
    return web.json_response({"success": False,
                              "error": {"code": code, "message": message,
                                        "details": details or {}}},
                             status=status)


def _cache_result(cache_db, method: str, symbol: str, params: dict, result: dict):
    """把通达信查询结果写入 SQLite 缓存 (尽力而为, 失败不影响主流程)."""
    try:
        if method == "get_market_data":
            # 兼容新旧结构: 新版 Value[symbol], 旧版顶层 Close
            if isinstance(result.get("Value"), dict) and symbol in result["Value"]:
                kline = result["Value"][symbol]
            else:
                kline = result
            closes = kline.get("Close") or []
            if closes:
                dates = kline.get("Date") or [""] * len(closes)
                bars = [{"date": str(dates[i]) if i < len(dates) else "",
                         "open": _at(kline.get("Open"), i), "high": _at(kline.get("High"), i),
                         "low": _at(kline.get("Low"), i), "close": closes[i],
                         "volume": _at(kline.get("Volume"), i), "amount": _at(kline.get("Amount"), i)}
                        for i in range(len(closes))]
                cache_db.save_kline(symbol, params.get("period", "1d"), bars)
        elif method == "get_stock_info":
            cache_db.save_stock_info(symbol, result)
        elif method == "get_market_snapshot":
            cache_db.save_snapshot(symbol, result)
        elif method == "get_financial_data":
            cache_db.save_financial(symbol, params.get("report_type", "report_time"), result)
    except Exception:
        log.warning("缓存写入失败(忽略)", exc_info=True)


def _at(lst, i):
    try:
        return lst[i] if lst and i < len(lst) else None
    except Exception:
        return None


def _run_tdx_sync(tdx, method: str, params: dict) -> dict:
    """同步执行通达信调用 (供缓存 get_or_fetch 的 fetch 回调用)."""
    return tdx.call(method, params or {})


def _extract_symbol(method: str, params: dict) -> str:
    """从参数提取股票代码 (兼容 stock_code/symbol/stock_list)."""
    if not isinstance(params, dict):
        return ""
    symbol = params.get("stock_code") or params.get("symbol") or ""
    if not symbol:
        stock_list = params.get("stock_list") or []
        symbol = stock_list[0] if stock_list else ""
    return symbol


class _LogWriter:
    """调用/交易日志的后台批量写入器（实盘事件循环绝不能被 SQLite 按住）。

    事件循环只做 put_nowait；SQLite 写入在独立线程里批量 commit。
    交易台账优先于调用日志：数据洪峰下先保证 trade_log 落库。
    队列满时：调用日志可丢（计数），交易日志记 ERROR（需立即排查）。
    """

    def __init__(self, cache_db, max_calls: int = 20000, max_trades: int = 10000):
        self.cache_db = cache_db
        self._calls: queue.Queue = queue.Queue(maxsize=max_calls)
        self._trades: queue.Queue = queue.Queue(maxsize=max_trades)
        self._stop = threading.Event()
        self.dropped_calls = 0
        self.dropped_trades = 0
        self._thread = threading.Thread(
            target=self._run, name="bridge-log-writer", daemon=True)
        self._thread.start()

    def put_call(self, record: dict) -> None:
        try:
            self._calls.put_nowait(record)
        except queue.Full:
            self.dropped_calls += 1

    def put_trade(self, record: dict) -> None:
        try:
            self._trades.put_nowait(record)
        except queue.Full:
            self.dropped_trades += 1
            log.error("trade_log 队列已满，交易日志丢弃（需立即排查）")

    def _run(self) -> None:
        while not self._stop.is_set():
            trades = self._drain(self._trades, 100)
            calls = self._drain(self._calls, 200)
            if not trades and not calls:
                self._stop.wait(0.2)
                continue
            try:
                if trades:
                    self.cache_db.log_trades_bulk(trades)
                if calls:
                    self.cache_db.log_calls_bulk(calls)
            except Exception:  # noqa: BLE001 - 日志线程不能死
                log.warning("日志批量写入失败（本批丢弃）", exc_info=True)
                self._stop.wait(0.5)

    @staticmethod
    def _drain(q: queue.Queue, max_items: int) -> list:
        out = []
        while len(out) < max_items:
            try:
                out.append(q.get_nowait())
            except queue.Empty:
                break
        return out

    def stats(self) -> dict:
        return {
            "dropped_calls": self.dropped_calls,
            "dropped_trades": self.dropped_trades,
            "queue_calls": self._calls.qsize(),
            "queue_trades": self._trades.qsize(),
        }

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        # 收尾：把剩余记录补写一次（尽力而为）
        trades = self._drain(self._trades, 10000)
        calls = self._drain(self._calls, 20000)
        try:
            if trades:
                self.cache_db.log_trades_bulk(trades)
            if calls:
                self.cache_db.log_calls_bulk(calls)
        except Exception:  # noqa: BLE001
            log.warning("日志收尾写入失败", exc_info=True)


# 由 build_app 设置；事件循环里的记录函数只往队列里塞。
_log_writer: _LogWriter | None = None


def _compact_audit(method: str, params, result):
    """高频数据方法只保留条数，避免审计表被大 JSON 撑爆。"""
    if method not in HIGH_FREQ_DATA_METHODS:
        return params, result
    n = 0
    if isinstance(params, dict):
        sl = params.get("stock_list")
        n = len(sl) if isinstance(sl, list) else (1 if params.get("stock_code") else 0)
    count = 0
    if isinstance(result, dict):
        value = result.get("Value")
        if isinstance(value, (list, dict)):
            count = len(value)
        elif isinstance(value, (int, float)):
            count = int(value)
    return {"n": n}, {"count": count}


def _record_call(cache_db, method: str, params: dict, result=None, duration_ms=None,
                 error: str = "", category: str = "", side: str = "", volume=None,
                 price=None, order_id="", status=""):
    """记录一次桥调用：入后台队列，绝不在这里写 SQLite。"""
    if not cache_db:
        return
    symbol = _extract_symbol(method, params)
    params, result = _compact_audit(method, params, result)
    record = {
        "method": method, "symbol": symbol,
        "params": params, "result": result, "duration_ms": duration_ms,
        "error": error, "category": category, "side": side, "volume": volume,
        "price": price, "order_id": order_id, "status": status,
        "created_at": datetime.now().isoformat(timespec="milliseconds"),
    }
    writer = _log_writer
    if writer is not None and writer.cache_db is cache_db:
        writer.put_call(record)
        return
    # 启动早期/测试直连：走一次同步写（极少）
    try:
        cache_db.log_call(method, record["symbol"], params, result, category=category,
                          side=side, volume=volume, price=price,
                          order_id=order_id, status=status,
                          duration_ms=duration_ms, error=error)
    except Exception:
        log.warning("记录调用日志失败(忽略)", exc_info=True)


async def _send_trade_warning_async(tdx, msg: str) -> None:
    """撤单侧的 150 笔/日预警：日志 + 通达信弹消息；走 data 槽，不占交易槽。"""
    log.warning(msg)
    try:
        await _call_tdx(tdx.send_message, "MSG," + msg, kind="data")
    except Exception:
        log.warning("发送撤单硬上限预警失败(忽略)", exc_info=True)


def _record_trade(cache_db, plan_id, event, symbol, side, volume, price,
                  order_id, status, message="", detail=None):
    """记录一次交易事件：入交易优先队列，绝不在这里写 SQLite。"""
    if not cache_db:
        return
    record = {
        "plan_id": plan_id, "event": event, "symbol": symbol, "side": side,
        "volume": volume, "price": price, "order_id": order_id,
        "status": status, "message": message, "detail": detail or {},
        "created_at": datetime.now().isoformat(timespec="milliseconds"),
    }
    writer = _log_writer
    if writer is not None and writer.cache_db is cache_db:
        writer.put_trade(record)
        return
    try:
        cache_db.log_trade(plan_id, event, symbol, side, volume, price,
                           order_id, status, message, detail)
    except Exception:
        log.warning("记录交易日志失败(忽略)", exc_info=True)


def _fetch_collector_health() -> dict:
    """本机采集器（127.0.0.1:8560）健康；未启动返回 down。"""
    import urllib.request
    try:
        with urllib.request.urlopen(
                "http://127.0.0.1:8560/health", timeout=0.8) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return payload.get("data") or {"status": "unknown"}
    except Exception as exc:  # noqa: BLE001
        return {"status": "down", "error": str(exc)[:120]}


async def _collector_health_cached() -> dict:
    now = time.monotonic()
    if now - _collector_cache["ts"] < 3.0 and _collector_cache["data"] is not None:
        return _collector_cache["data"]
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(_PROBE_EXECUTOR, _fetch_collector_health)
    _collector_cache["ts"] = now
    _collector_cache["data"] = data
    return data


_cache_write_sem = asyncio.Semaphore(2)


async def _cache_result_async(*args) -> None:
    """把 cache 写入丢进专用线程池 + 限并发；失败不影响主流程。"""
    async with _cache_write_sem:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(_CACHE_EXECUTOR, _cache_result, *args)


async def _run_cache(fn, *args):
    """cache 读也出事件循环：GET /cache/* 每 15s 被 UI 轮询，等 CacheDb 锁会冻桥。"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_CACHE_EXECUTOR, fn, *args)


_local_ips_cache: list | None = None


def _compute_local_ips() -> list:
    import socket
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.append(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except Exception:
        pass
    return ips or ["127.0.0.1"]


async def _local_ips_cached() -> list:
    global _local_ips_cache
    if _local_ips_cache is None:
        loop = asyncio.get_running_loop()
        _local_ips_cache = await loop.run_in_executor(
            _HEALTH_EXECUTOR, _compute_local_ips)
    return _local_ips_cache


async def _health_probe(tdx) -> bool:
    loop = asyncio.get_running_loop()
    t0 = time.monotonic()
    ok = False
    try:
        ok = await asyncio.wait_for(
            loop.run_in_executor(_HEALTH_EXECUTOR, tdx.health_check_fast, 1.0),
            timeout=2.0)
    except asyncio.TimeoutError:
        ok = False
    finally:
        ms = (time.monotonic() - t0) * 1000.0
        _health_stats["calls"] += 1
        _health_stats["last_ms"] = round(ms, 1)
        _health_stats["max_ms"] = max(_health_stats["max_ms"], round(ms, 1))
        if not ok:
            _health_stats["failures"] += 1
    return ok


def _cache_stats_sync(cache_db, memory_cache) -> dict | None:
    """取缓存计数 —— **同步函数，由调用方丢进线程跑**。

    .. warning::
       本函数**绝不可**在事件循环里直接调用。`cache_db.get_stats()` 是 7 个
       ``COUNT(*)``，其中一个打在 ``tdx_log`` 上（生产实测 512 万行 / 6.9 GB
       SQLite），单次实测 3–52s；一旦在循环里同步执行，整个桥（含免鉴权的
       ``/api/v1/health``）会被按住同样久。后果不是"页面慢"，是**自毁循环**：

       1. 控制台页面（``ui.html`` 的 ``setInterval(loadStats, 15000)``）每 15s 打一次
          ``/stats``；桥自身 ``ui.auto_open: true``，每重启一次就**再开一个**标签。
       2. ``/stats`` 把事件循环按住 3–52s ⇒ ``/health`` 一起停摆。
       3. Windows 侧 ``watchdog.ps1`` 用 ``Invoke-WebRequest -TimeoutSec 5`` 探
          ``/health``，连续 2 次失败即**杀掉桥重启**——于是又开一个标签，回到第 1 步。

       2026-10-13 实测：15:00 之后 31 次"桥疑似崩溃/假活，正在重启"，**每一次**都
       紧跟在一记桥内耗时 8–52s 的 ``/stats`` 之后（bridge.log 无任何进程崩溃
       traceback，桥是被杀的）。当日重启 378 次。而 QuantMind 侧启动闸门的行情探针
       同步超时，于是策略启不动。

    另外计数本身不值这个价：控制台只是展示，60s 的陈旧度完全够用（见
    :data:`CACHE_STATS_TTL_S`），故外层再加 TTL 缓存，命中时<b>连线程都不进</b>。
    """
    if not cache_db:
        return None
    now = time.monotonic()
    cached = _cache_stats_cache.get("data")
    if cached is not None and now - _cache_stats_cache.get("ts", 0.0) < CACHE_STATS_TTL_S:
        # 浅拷贝：调用方还要往里塞 mem_hit_rate 等键，别污染缓存
        return dict(cached)
    try:
        data = cache_db.get_stats()
        data["disk_bytes"] = cache_db.get_cache_disk()
    except Exception:
        log.warning("缓存统计读取失败(忽略)", exc_info=True)
        return None
    _cache_stats_cache["ts"] = now
    _cache_stats_cache["data"] = dict(data)
    if memory_cache:
        try:
            data["mem_hit_rate"] = memory_cache.hit_rate()
            stats = memory_cache.stats()
            data["mem_hits"] = stats["hits"]
            data["mem_entries"] = stats["cache_entries"]
        except Exception:
            pass
    return data


def build_app(token: str, tdx: TdxClient, executor: PlanExecutor,
              tracker: OrderTracker, sltp, cache_db=None, port: int = 8550,
              extra_tokens: list = None, rate_limiter=None,
              memory_cache=None, trade_limits_dir: str = "") -> web.Application:
    app = web.Application()
    if trade_limits_dir:
        # 撤单日计数独立持久化；桥重启不清零
        global _cancel_guard
        _cancel_guard = TradeRateGuard(
            state_path=os.path.join(trade_limits_dir, "trade_limits_cancels.json"))
    # 后台日志写入器：事件循环只入队，SQLite 在独立线程批量 commit。
    global _log_writer
    writer = _LogWriter(cache_db) if cache_db is not None else None
    _log_writer = writer
    if writer is not None:
        async def _stop_writer(_app):
            writer.stop()

        app.on_cleanup.append(_stop_writer)
    # 事件循环 lag 心跳（UI/探针直接量化“循环被按住”）
    try:
        lag_task = asyncio.get_running_loop().create_task(_loop_lag_task())

        async def _cancel_lag(_app):
            lag_task.cancel()

        app.on_cleanup.append(_cancel_lag)
    except RuntimeError:
        pass
    # 缓存库日清（后台线程，收盘后清理 tdx_log；超 2GB 盘中也会清）
    if cache_db is not None:
        try:
            maint_task = asyncio.get_running_loop().create_task(
                _cache_maintenance_task(cache_db))

            async def _cancel_maint(_app):
                maint_task.cancel()

            app.on_cleanup.append(_cancel_maint)
        except RuntimeError:
            pass
    # 支持多 token: 主 token + 扩展 tokens
    valid_tokens = [token] + [t for t in (extra_tokens or []) if t]

    @web.middleware
    async def auth_middleware(request: web.Request, handler):
        path = request.path
        client_ip = request.remote or "unknown"

        # 请求体大小限制 (防超大 payload 攻击)
        if request.headers.get("Content-Length"):
            try:
                if int(request.headers["Content-Length"]) > 512 * 1024:
                    return json_error(413, "PAYLOAD_TOO_LARGE", "请求体过大 (限 512KB)")
            except ValueError:
                return json_error(400, "INVALID_CONTENT_LENGTH", "Content-Length 非法")

        # Content-Type 白名单 (POST 请求必须 JSON)
        if request.method == "POST":
            ct = (request.headers.get("Content-Type", "") or "").lower()
            if ct and not ct.startswith("application/json"):
                return json_error(415, "UNSUPPORTED_MEDIA_TYPE", "仅支持 application/json")

        # 只读状态/页面豁免鉴权 (仅展示, 不含写操作)
        # ⚠️ `/api/v1/cache/logs` 与 `/api/v1/cache/trades` **不在此列**：
        # 它们返回调用明细与交易台账（含标的/方向/数量/委托号），属于敏感数据，
        # 必须带 token；仅行情缓存 kline/get 保持匿名可读。
        read_only = ("/api/v1/health", "/api/v1/stats", "/api/v1/system",
                     "/ui", "/favicon.ico", "/api/v1/cache/kline",
                     "/api/v1/cache/get")
        if path in read_only:
            if rate_limiter:
                # 只读接口也限流 (防 DDoS)
                err = rate_limiter.check(client_ip, path, True)
                if err:
                    return json_error(429, "RATE_LIMITED", err)
            return await handler(request)

        # 鉴权: 主 token + extra_tokens + 数据库动态 token
        auth = request.headers.get("Authorization", "")
        provided = auth.removeprefix("Bearer ").strip() if auth else ""
        auth_ok = any(verify_token(t, provided) for t in valid_tokens)
        if not auth_ok and cache_db:
            # 数据库 token 校验 (动态增删实时生效)
            auth_ok = await asyncio.to_thread(cache_db.is_token_valid, provided)

        # 限流 (含鉴权失败计数 → 封 IP)
        if rate_limiter:
            err = rate_limiter.check(client_ip, path, auth_ok)
            if err:
                return json_error(429, "RATE_LIMITED", err)

        if not auth_ok:
            return json_error(401, "AUTH_FAILED", "无效或缺失 token")
        return await handler(request)

    app.middlewares.append(auth_middleware)

    async def health(request: web.Request):
        tdx_connected = await _health_probe(tdx)
        return web.json_response({
            "status": "ok",
            "tdx_connected": tdx_connected,
            "server_time": datetime.now().isoformat(timespec="seconds"),
            # 交易/数据分槽的占用与等待：采集器/看门狗可用它判断是否该让路
            "semaphores": semaphores_stats(),
            "loop_lag": dict(_loop_lag),
            # 写死的交易硬上限余量（299/秒、1999/日）
            "trade_limits": {
                "orders": executor.order_guard.stats(),
                "cancels": _cancel_guard.stats(),
            },
        })

    async def execute_plan(request: web.Request):
        try:
            data = await request.json()
            plan = TradePlan.from_dict(data)
        except (ValueError, KeyError, TypeError) as e:
            return json_error(400, "PLAN_VALIDATION_ERROR", f"非法计划: {e}")
        if not plan.orders:
            return json_error(400, "PLAN_VALIDATION_ERROR", "计划为空")
        for o in plan.orders:
            if o.volume <= 0 or not o.stock_code:
                return json_error(400, "PLAN_VALIDATION_ERROR",
                                  f"非法订单: {o.stock_code} 数量 {o.volume}")
            if o.side not in ("buy", "sell"):
                return json_error(400, "PLAN_VALIDATION_ERROR", f"非法方向: {o.side}")
        try:
            report = await _call_tdx(executor.execute_plan, plan, kind="order")
            if report["status"] == "duplicate":
                return json_error(409, "DUPLICATE_PLAN", report.get("message", "重复计划"))
            report["channel_used"] = "http"
            # 精确记录每笔订单到 SQLite
            for o in report.get("orders", []):
                _record_trade(cache_db, plan.plan_id, "order_execute",
                              o.get("stock_code", ""), o.get("side", ""),
                              o.get("volume"), None, o.get("order_id", ""),
                              o.get("status", ""), o.get("message", ""), detail=o)
            return web.json_response(report)
        except TdxError as e:
            return json_error(502, "TDX_UNAVAILABLE", str(e))

    async def query_account(request: web.Request):
        data = await request.json()
        account_id = await _call_tdx(executor.resolve_account_id,
                                     data.get("account", ""), data.get("account_type", "stock"), kind="trade")
        try:
            def _do():
                asset = parser.parse_asset(tdx.query_stock_asset(account_id))
                positions = [parser.parse_position(p)
                             for p in tdx.query_stock_positions(account_id)]
                return asset, positions
            asset, positions = await _call_tdx(_do, kind="trade")
            return web.json_response({"account_id": account_id, "asset": asset,
                                      "positions": positions, "channel_used": "http"})
        except TdxError as e:
            return json_error(502, "TDX_UNAVAILABLE", str(e))

    async def query_orders(request: web.Request):
        data = await request.json()
        account_id = await _call_tdx(executor.resolve_account_id,
                                     data.get("account", ""), data.get("account_type", "stock"), kind="trade")
        try:
            def _do():
                return [parser.parse_order(o)
                        for o in tdx.query_stock_orders(
                            account_id, data.get("stock_code", ""),
                            data.get("cancelable_only", False))]
            orders = await _call_tdx(_do, kind="trade")
            return web.json_response({"orders": orders, "channel_used": "http"})
        except TdxError as e:
            return json_error(502, "TDX_UNAVAILABLE", str(e))

    async def _verify_cancel_effective(account_id, stock_code: str, order_id: str,
                                       attempts: int = 3, delay: float = 0.6) -> bool:
        """撤单握手报失败后, 按当日委托终态复核撤单是否实际已生效.

        通达信撤单异步落定 (2026-10-12 实测 ≤2s): 握手 Value 只代表受理瞬时结果,
        报失败≠没撤成. 委托记录 BSFlag=-1 (parser 归一为 side="cancel") 或状态
        已成/部成撤 即已撤; 查询失败视作"未确认", 不改变原结论 (不搞"永远成功").
        """
        for _ in range(attempts):
            await asyncio.sleep(delay)
            try:
                orders = await _call_tdx(
                    lambda: [parser.parse_order(o) for o in tdx.query_stock_orders(
                        account_id, stock_code, False)], kind="trade")
            except Exception:
                continue
            for o in orders or []:
                if str(o.get("order_id", "")) != str(order_id):
                    continue
                return o.get("side") == "cancel" or o.get("status") in (
                    "cancelled", "partial_cancelled")
        return False

    async def cancel_order(request: web.Request):
        data = await request.json()
        stock_code = data.get("stock_code", "")
        order_id = data.get("order_id", "")
        allowed, reason = _cancel_guard.allow()
        if not allowed:
            return json_error(429, "HARD_LIMIT", reason)
        warning = _cancel_guard.consume_warning()
        if warning:
            asyncio.create_task(_send_trade_warning_async(tdx, warning))
        account_id = await _call_tdx(executor.resolve_account_id,
                                     data.get("account", ""), data.get("account_type", "stock"), kind="trade")
        try:
            res = await _call_tdx(tdx.cancel_order_stock, account_id,
                                  stock_code, order_id, kind="order")
            # 直连账号成功码为 "2" (下单路径 plan_executor 同样把 "2" 当成功),
            # 撤单只认 "1" 会把已受理的撤单报成失败 (2026-10-12 实盘假失败根因)
            ok = str(res.get("Value", "0")) in ("1", "2")
            verified = False
            if not ok:
                # 握手失败不等于没撤成: 按当日委托终态复核 (撤单是减风险动作,
                # 假失败会被上层当"未撤"重试或放弃, 两头都是通道风险)
                verified = await _verify_cancel_effective(account_id, stock_code, order_id)
                ok = verified
            # 共享盘写可能卡顿（CIFS）：撤单日志异步落，不阻塞事件循环
            asyncio.create_task(asyncio.to_thread(executor.log_trade, {
                "event": "cancel_order", "stock_code": stock_code,
                "order_id": order_id, "ok": ok, "verified": verified,
                "msg": res.get("Msg", "")}))
            resp = {"success": ok, "message": res.get("Msg", ""),
                    "channel_used": "http"}
            if verified:
                resp["verified"] = True
                resp["message"] = "撤单已生效（握手报失败系误报, 经委托终态复核确认）"
            return web.json_response(resp)
        except TdxTimeoutError as e:
            # 撤单超时=结果未知：按当日委托终态复核；绝不返回 502 让上游当失败重试
            verified = await _verify_cancel_effective(account_id, stock_code, order_id)
            executor.log_trade({"event": "cancel_timeout_unknown",
                                "stock_code": stock_code, "order_id": order_id,
                                "verified": verified, "error": str(e)})
            if verified:
                return web.json_response({
                    "success": True, "verified": True,
                    "message": "撤单已生效（超时后经委托终态复核确认）",
                    "channel_used": "http"})
            return web.json_response({
                "success": False, "status": "unknown",
                "message": "撤单请求超时，结果未知（已复核未确认；勿当失败重试）",
                "channel_used": "http"})
        except TdxError as e:
            return json_error(502, "TDX_UNAVAILABLE", str(e))

    async def configure_sltp(request: web.Request):
        data = await request.json()
        items = data.get("items", [])
        registered = sltp.register_items(items, data.get("account_id", 0))
        return web.json_response({"success": True, "items_configured": registered,
                                  "channel_used": "http"})

    async def sltp_state(request: web.Request):
        return web.json_response({"items": sltp.state_items(), "channel_used": "http"})

    # ---- 推送接口 (信号/消息/板块/源码) ----

    async def push_block(request: web.Request):
        data = await request.json()
        try:
            res = await _call_tdx(tdx.send_user_block,
                                  data.get("block_code", ""), data.get("stocks", []),
                                  bool(data.get("show", False)))
            return web.json_response({"success": True, "result": res, "channel_used": "http"})
        except TdxError as e:
            return json_error(502, "TDX_UNAVAILABLE", str(e))

    async def push_message(request: web.Request):
        data = await request.json()
        try:
            res = await _call_tdx(tdx.send_message, data.get("msg", ""))
            return web.json_response({"success": True, "result": res, "channel_used": "http"})
        except TdxError as e:
            return json_error(502, "TDX_UNAVAILABLE", str(e))

    async def push_warnings(request: web.Request):
        data = await request.json()
        try:
            def _do():
                return tdx.send_warn(
                    stock_list=data.get("stock_list", []),
                    price_list=data.get("price_list"),
                    close_list=data.get("close_list"),
                    volum_list=data.get("volum_list"),
                    bs_flag_list=data.get("bs_flag_list"),
                    warn_type_list=data.get("warn_type_list"),
                    reason_list=data.get("reason_list"),
                    count=data.get("count", 1))
            res = await _call_tdx(_do)
            return web.json_response({"success": True, "result": res, "channel_used": "http"})
        except TdxError as e:
            return json_error(502, "TDX_UNAVAILABLE", str(e))

    async def push_source(request: web.Request):
        data = await request.json()
        try:
            res = await _call_tdx(tdx.send_source, data.get("py_code", ""),
                                  int(data.get("handle_type", 0)))
            return web.json_response({"success": True, "result": res, "channel_used": "http"})
        except TdxError as e:
            return json_error(502, "TDX_UNAVAILABLE", str(e))

    app.router.add_get("/api/v1/health", health)
    app.router.add_post("/api/v1/plans/execute", execute_plan)
    app.router.add_post("/api/v1/account/query", query_account)
    app.router.add_post("/api/v1/orders/query", query_orders)
    app.router.add_post("/api/v1/orders/cancel", cancel_order)
    app.router.add_post("/api/v1/sltp/configure", configure_sltp)
    app.router.add_get("/api/v1/sltp/state", sltp_state)
    app.router.add_post("/api/v1/push/block", push_block)
    app.router.add_post("/api/v1/push/message", push_message)
    app.router.add_post("/api/v1/push/warnings", push_warnings)
    app.router.add_post("/api/v1/push/source", push_source)

    # ---- 通用透传路由 (覆盖全部 tqcenter 方法) ----

    async def tdx_call(request: web.Request):
        """通用 JSON-RPC 透传: POST /api/v1/tdx/call
        body: {"method": "get_stock_info", "params": {"stock_code": "600519.SH"}}
        """
        try:
            data = await request.json()
        except Exception:
            return json_error(400, "INVALID_JSON", "请求体必须是合法 JSON")
        method = str(data.get("method", "")).strip()
        params = data.get("params", {}) or {}
        if not method:
            return json_error(400, "MISSING_METHOD", "缺少 method 字段")
        if method in FORBIDDEN_CALL_METHODS:
            return json_error(
                403, "METHOD_FORBIDDEN",
                f"{method} 只能经 /api/v1/plans/execute 或 /api/v1/orders/cancel 调用")
        # 白名单: 只允许已定义的方法, 防止任意调用
        allowed = [m for m in dir(tdx) if not m.startswith("_") and callable(getattr(tdx, m))]
        if method not in allowed:
            return json_error(400, "UNKNOWN_METHOD", f"不支持的方法: {method}")
        try:
            import time as _time
            _t0 = _time.monotonic()
            # 内存缓存: 高并发场景拦截高频查询, 保护通达信
            cache_ttl = {"get_market_snapshot": 3, "get_market_data": 300,
                         "get_stock_info": 86400, "get_more_info": 86400,
                         "get_financial_data": 86400, "get_stock_list": 3600,
                         "get_sector_list": 3600}.get(method, 0)
            cache_key = f"{method}:{json.dumps(params, ensure_ascii=False, sort_keys=True)}" if cache_ttl else None

            if memory_cache and cache_key:
                # 内存缓存路径也必须走 data 槽 + 线程：get_or_fetch 内部是
                # 同步 urllib + Condition.wait，直接在事件循环里跑会把桥按住。
                def _fetch_cached():
                    return memory_cache.get_or_fetch(
                        cache_key, cache_ttl, lambda: tdx.call(method, params))
                result = await _call_tdx(_fetch_cached,
                                         kind=call_kind(method))
            else:
                result = await _call_tdx(tdx.call, method, params,
                                         kind=call_kind(method))
            _dur = round((_time.monotonic() - _t0) * 1000, 1)
            # 自动缓存常见查询到 SQLite (若启用缓存)
            if cache_db and method in ("get_market_data", "get_stock_info",
                                       "get_market_snapshot", "get_financial_data"):
                symbol = _extract_symbol(method, params)
                if symbol:
                    # best-effort 落 SQLite：绝不能在事件循环里同步写
                    asyncio.create_task(_cache_result_async(
                        cache_db, method, symbol, params, result))
            # 精确记录调用
            _record_call(cache_db, method, params, result=result, duration_ms=_dur,
                         category="query", status="ok")
            return web.json_response({"success": True, "method": method,
                                      "result": result, "channel_used": "http"})
        except TdxError as e:
            _record_call(cache_db, method, params, error=str(e),
                         duration_ms=round((_time.monotonic() - _t0) * 1000, 1),
                         category="query", status="error")
            return json_error(502, "TDX_UNAVAILABLE", str(e))

    # ---- 缓存查询路由 ----

    async def cache_query(request: web.Request):
        """查询 SQLite 缓存. GET /api/v1/cache/kline?symbol=600519.SH&period=1d&limit=100"""
        if not cache_db:
            return json_error(501, "CACHE_DISABLED", "缓存未启用")
        query = request.query
        symbol = query.get("symbol", "")
        period = query.get("period", "1d")
        limit = int(query.get("limit", 500))
        rows = await _run_cache(cache_db.get_kline, symbol, period, limit)
        return web.json_response({"success": True, "symbol": symbol, "period": period,
                                  "count": len(rows), "data": rows})

    async def cache_logs(request: web.Request):
        """查询桥调用日志. GET /api/v1/cache/logs?method=get_market_data&limit=100"""
        if not cache_db:
            return json_error(501, "CACHE_DISABLED", "缓存未启用")
        query = request.query
        method = query.get("method", "")
        limit = int(query.get("limit", 100))
        rows = await _run_cache(cache_db.get_logs, method, limit)
        return web.json_response({"success": True, "count": len(rows), "data": rows})

    async def cache_trades(request: web.Request):
        """查询交易日志. GET /api/v1/cache/trades?symbol=600519.SH&limit=100"""
        if not cache_db:
            return json_error(501, "CACHE_DISABLED", "缓存未启用")
        query = request.query
        symbol = query.get("symbol", "")
        limit = int(query.get("limit", 100))
        rows = await _run_cache(cache_db.get_trade_logs, symbol, limit)
        return web.json_response({"success": True, "count": len(rows), "data": rows})

    async def cache_get(request: web.Request):
        """查询缓存数据. GET /api/v1/cache/get?type=snapshot|stock_info|financial|sector&symbol=600519.SH"""
        if not cache_db:
            return json_error(501, "CACHE_DISABLED", "缓存未启用")
        query = request.query
        ctype = query.get("type", "")
        symbol = query.get("symbol", "")
        block = query.get("block", "")
        data = None
        if ctype == "snapshot":
            data = await _run_cache(cache_db.get_snapshot, symbol)
        elif ctype == "stock_info":
            data = await _run_cache(cache_db.get_stock_info, symbol)
        elif ctype == "financial":
            data = await _run_cache(cache_db.get_financial, symbol)
        elif ctype == "sector":
            data = await _run_cache(cache_db.get_sector_stocks, block or symbol)
        return web.json_response({"success": data is not None, "type": ctype,
                                  "symbol": symbol, "data": data})

    app.router.add_post("/api/v1/tdx/call", tdx_call)
    app.router.add_get("/api/v1/cache/kline", cache_query)
    app.router.add_get("/api/v1/cache/logs", cache_logs)
    app.router.add_get("/api/v1/cache/trades", cache_trades)
    app.router.add_get("/api/v1/cache/get", cache_get)

    # ---- 桥状态 / 网页控制台 ----

    async def system(request: web.Request):
        """系统资源 + 交易/数据分槽 + 采集器状态（UI 5s 轮询，轻量）。"""
        loop = asyncio.get_running_loop()
        sysinfo = await loop.run_in_executor(_PROBE_EXECUTOR, _system_metrics.sample)
        collector = await _collector_health_cached()
        return web.json_response({"success": True, "data": {
            "system": sysinfo,
            "semaphores": semaphores_stats(),
            "trade_limits": {
                "orders": executor.order_guard.stats(),
                "cancels": _cancel_guard.stats(),
            },
            "log_writer": _log_writer.stats() if _log_writer is not None else None,
            "health_probe": dict(_health_stats),
            "loop_lag": dict(_loop_lag),
            "collector": collector,
            "bridge": {"version": "1.0.0", "pid": os.getpid(),
                        "started_at": _BRIDGE_STARTED_AT},
            "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        }})

    async def stats(request: web.Request):
        """桥状态总览: 本机IP/共享路径/连接状态/账户/缓存统计."""
        import socket
        # 本机 IP 缓存 + 线程池：getaddrinfo 在 Windows 可能阻塞数秒，绝不能在循环里
        local_ips = await _local_ips_cached()
        hostname = socket.gethostname()
        tdx_connected = await _health_probe(tdx)
        stats_data = {
            "hostname": socket.gethostname(),
            "local_ips": local_ips,
            "bridge_url": f"http://{local_ips[0] if local_ips else 'localhost'}:{port}",
            "tdx_connected": tdx_connected,
            "server_time": datetime.now().isoformat(timespec="seconds"),
            "shared_dir": os.environ.get("SHARED_DIR", ""),
            "token_configured": bool(token),
            "port": port,
        }
        # 账户概览 (带缓存, 避免网页高频轮询压垮通达信)
        import time as _time
        _now = _time.monotonic()
        if _now - _stats_cache.get("ts", 0) < 10:
            stats_data["account"] = _stats_cache.get("account")
        else:
            try:
                account_id = await _call_tdx(executor.resolve_account_id, "", "stock", kind="trade")
                asset = await _call_tdx(tdx.query_stock_asset, account_id, kind="trade")
                positions = await _call_tdx(tdx.query_stock_positions, account_id, kind="trade")
                stats_data["account"] = {
                    "currency": asset.get("Currency", ""),
                    "balance": float(asset.get("Balance", 0) or 0),
                    "cash": float(asset.get("Cash", 0) or 0),
                    "asset": float(asset.get("Asset", 0) or 0),
                    "market_value": float(asset.get("MarketValue", 0) or 0),
                    "position_count": len(positions or []),
                }
                _stats_cache["ts"] = _now
                _stats_cache["account"] = stats_data["account"]
            except Exception:
                stats_data["account"] = None
        # 缓存统计：**必须转线程**。这条曾经在事件循环里同步跑 `cache_db.get_stats()`
        # （7 个 COUNT(*)，其一打在 512 万行 / 6.9GB 的 tdx_log 上，实测 3–52s），
        # 把 `/health` 一起按住 → Windows 看门狗 5s 超时 → 判「桥假活」杀桥重启 →
        # 桥自带控制台又开一个标签 → 更多 /stats。2026-10-13 的启动失败即此自毁循环。
        # 详见 `_cache_stats_sync` 的 docstring。
        if cache_db:
            loop = asyncio.get_running_loop()
            stats_data["cache"] = await loop.run_in_executor(
                _CACHE_EXECUTOR, _cache_stats_sync, cache_db, memory_cache
            )
        # 限流状态
        if rate_limiter:
            try:
                stats_data["security"] = rate_limiter.stats()
            except Exception:
                stats_data["security"] = None
        # 交易/数据分槽观测：交易槽等待 >1s 已打 ERROR，这里给控制台/探针看
        stats_data["semaphores"] = semaphores_stats()
        stats_data["trade_limits"] = {
            "orders": executor.order_guard.stats(),
            "cancels": _cancel_guard.stats(),
        }
        if _log_writer is not None:
            stats_data["log_writer"] = _log_writer.stats()
        return web.json_response({"success": True, "data": stats_data})

    async def reset_token(request: web.Request):
        """重置 BRIDGE_AUTH_TOKEN (生成新 hex, 更新环境变量)."""
        import secrets
        new_token = secrets.token_hex(32)
        os.environ["BRIDGE_AUTH_TOKEN"] = new_token
        return web.json_response({"success": True, "new_token": new_token,
                                  "message": "新 token 已生成, 请同步到 Linux .env"})

    # ---- Token 管理 API (动态增删查, SQLite 存储) ----

    async def list_tokens(request: web.Request):
        """列出所有 token. GET /api/v1/auth/tokens"""
        if not cache_db:
            return json_error(501, "CACHE_DISABLED", "token 管理需启用缓存")
        try:
            tokens = await asyncio.to_thread(cache_db.list_tokens)
            return web.json_response({"success": True, "tokens": tokens})
        except Exception as e:
            return json_error(500, "INTERNAL", str(e))

    async def add_token(request: web.Request):
        """新增 token. POST /api/v1/auth/tokens {name?, count?}"""
        if not cache_db:
            return json_error(501, "CACHE_DISABLED", "token 管理需启用缓存")
        import secrets
        try:
            data = await request.json() or {}
        except Exception:
            data = {}
        name = str(data.get("name", "") or "")[:50]
        count = max(1, min(int(data.get("count", 1)), 100))  # 批量生成 1-100 个
        tokens = []
        for i in range(count):
            new_token = secrets.token_hex(32)
            ok = await asyncio.to_thread(cache_db.add_token, new_token,
                                         f"{name or 'client'}_{i+1}")
            if ok:
                tokens.append({"token": new_token, "name": f"{name or 'client'}_{i+1}"})
        return web.json_response({"success": True, "count": len(tokens), "tokens": tokens,
                                  "message": f"已生成 {len(tokens)} 个 token, 请立即保存"})

    async def delete_token_route(request: web.Request):
        """删除 token. DELETE /api/v1/auth/tokens/{token_hash}"""
        if not cache_db:
            return json_error(501, "CACHE_DISABLED", "token 管理需启用缓存")
        token_hash = request.match_info.get("token_hash", "")
        if not token_hash:
            return json_error(400, "MISSING_HASH", "缺少 token_hash")
        ok = await asyncio.to_thread(cache_db.delete_token, token_hash)
        if not ok:
            return json_error(404, "NOT_FOUND", "token 不存在")
        return web.json_response({"success": True, "message": "token 已删除"})

    async def ui_page(request: web.Request):
        """网页控制台 HTML."""
        from aiohttp import web as _web
        # 兼容 PyInstaller 打包: 资源在 sys._MEIPASS 下
        base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        html_path = os.path.join(base, "static", "ui.html")
        try:
            with open(html_path, "r", encoding="utf-8") as f:
                html = f.read()
            return _web.Response(text=html, content_type="text/html", charset="utf-8")
        except OSError:
            return _web.Response(text="<h1>ui.html 未找到</h1>", content_type="text/html")

    app.router.add_get("/api/v1/stats", stats)
    app.router.add_get("/api/v1/system", system)
    app.router.add_post("/api/v1/auth/reset-token", reset_token)
    app.router.add_get("/api/v1/auth/tokens", list_tokens)
    app.router.add_post("/api/v1/auth/tokens", add_token)
    app.router.add_delete("/api/v1/auth/tokens/{token_hash}", delete_token_route)
    app.router.add_get("/ui", ui_page)

    return app
