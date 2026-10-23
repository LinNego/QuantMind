"""同花顺旁路数据客户端（桥 → 本机 ths-worker）。

职责边界
--------
- **只读**：本模块只做 HTTP GET/POST 到本机 worker，不含任何交易动作；
- **隔离**：worker 是独立进程（默认 127.0.0.1:8765），桥崩溃/重启不影响它，
  它挂了也只影响 `/api/v1/ths/*`；
- **可审计**：每个方法都过白名单（路径模板 + TTL），调用结果由路由层写入
  桥的调用日志（`tdx_log`，method=`ths.<name>`）；
- **韧性**：TTL 缓存 + 单飞（并发同类请求只打一次上游）+ 熔断（连续失败后
  直接 503，避免每个请求都等超时）。

机构级约束
----------
- 上游超时必须显式（aiohttp total timeout），绝不无限等；
- 熔断打开期间不发任何上游请求，状态可观测（`status()`）；
- 缓存键包含全部查询参数，避免不同参数串数据；
- 代码/日期等入参在白名单层做正则校验，杜绝路径注入。
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Optional

import aiohttp

log = logging.getLogger(__name__)

_CODE_RE = re.compile(r"^(?:[0-9]{6})(?:\.(?:SH|SZ|BJ))?$", re.IGNORECASE)
_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")

#: 方法白名单：name -> (HTTP 方法, 路径模板, TTL 秒)。TTL=0 表示不缓存。
#: 覆盖 worker 全部只读端点（2026-10-23 对照 app.py 路由表）。上游改名时
#: **只改这一张表**即可，桥的其他代码不用动。
THS_METHODS: dict[str, tuple[str, str, float]] = {
    # 连接/状态
    "status": ("GET", "/api/status", 0.0),
    "connect": ("POST", "/api/connect", 0.0),
    # 行情
    "quote": ("GET", "/api/quote", 1.0),
    "quotes_ext": ("GET", "/api/quotes_ext", 5.0),
    "depth": ("GET", "/api/depth/{code}", 1.5),
    "market_view": ("GET", "/api/market_view/{code}", 1.0),
    "market_view_fast": ("GET", "/api/market_view_fast/{code}", 1.0),
    "stock_ready": ("GET", "/api/stock-ready/{code}", 5.0),
    "market_snapshot": ("GET", "/api/market_snapshot", 10.0),
    # K 线 / 分时 / 竞价
    "kline": ("GET", "/api/kline/{code}", 30.0),
    "timeline": ("GET", "/api/timeline/{code}", 2.0),
    "history_timeline": ("GET", "/api/history_timeline/{code}", 30.0),
    "auction": ("GET", "/api/auction/{code}", 30.0),
    "closing_auction": ("GET", "/api/closing_auction/{code}", 30.0),
    "intraday": ("GET", "/api/intraday/{code}", 2.0),
    "intraday_auctions": ("GET", "/api/intraday_auctions/{code}", 30.0),
    # 逐笔 / 超级盘口 / 委托队列
    "superorder": ("GET", "/api/superorder/{code}", 2.0),
    "superorder_bse": ("GET", "/api/superorder-bse/{code}", 2.0),
    "superorder_window": ("GET", "/api/superorder-window/{code}", 2.0),
    "superorder_replay": ("GET", "/api/superorder-replay/{code}", 2.0),
    "superorder_replay_snapshot": ("GET", "/api/superorder-replay/{code}/snapshot", 2.0),
    "order_details": ("GET", "/api/order-details/{code}", 2.0),
    "order_queues": ("GET", "/api/order-queues/{code}", 2.0),
    # 异动
    "dxjl": ("GET", "/api/dxjl", 2.0),
    "dxjl_latest": ("GET", "/api/dxjl/latest", 1.0),
    # 板块 / 榜单 / 自选
    "boards": ("GET", "/api/boards", 60.0),
    "board_categories": ("GET", "/api/board_categories", 300.0),
    "hot_boards": ("GET", "/api/hot_boards", 30.0),
    "dynamic_plates": ("GET", "/api/dynamic_plates", 30.0),
    "dynamic_plate_refresh": ("GET", "/api/dynamic_plate_refresh", 0.0),
    "board_constituents": ("GET", "/api/board/{code}/constituents", 60.0),
    "board_quotes": ("GET", "/api/board/{code}/quotes", 5.0),
    "board_timeline": ("GET", "/api/board/{code}/timeline", 30.0),
    "board_kline": ("GET", "/api/board/{code}/kline", 60.0),
    "board_auction": ("GET", "/api/board/{code}/auction", 60.0),
    "stocks": ("GET", "/api/stocks", 60.0),
    "stocks2": ("GET", "/api/stocks2", 60.0),
    "stock_list_ranked": ("GET", "/api/stock_list_ranked", 30.0),
    "hot": ("GET", "/api/hot", 10.0),
    "dde_rank": ("GET", "/api/dde_rank", 30.0),
    "self_stocks": ("GET", "/api/self_stocks", 10.0),
    "groups": ("GET", "/api/groups", 60.0),
    "group": ("GET", "/api/groups/{name}", 60.0),
}


class ThsError(RuntimeError):
    """同花顺旁路的基础异常。"""


class ThsDisabled(ThsError):
    """配置里 ths.enabled=false。"""


class ThsUnavailable(ThsError):
    """worker 不可达 / 上游报错。"""


class ThsCircuitOpen(ThsError):
    """熔断打开：短时间内不再打上游。"""


class ThsBadArgument(ValueError):
    """入参不合法（代码/日期格式）。"""


class ThsClient:
    """到本机 ths-worker 的只读客户端。线程/协程安全（单事件循环内）。"""

    def __init__(self, base_url: str = "http://127.0.0.1:8765",
                 enabled: bool = False, timeout_s: float = 8.0,
                 cache_enabled: bool = True, circuit_failures: int = 5,
                 circuit_cooldown_s: float = 30.0):
        self.base_url = (base_url or "").rstrip("/")
        self.enabled = bool(enabled)
        self.timeout_s = float(timeout_s or 8.0)
        self.cache_enabled = bool(cache_enabled)
        self.circuit_failures = max(1, int(circuit_failures or 5))
        self.circuit_cooldown_s = float(circuit_cooldown_s or 30.0)
        self._session: Optional[aiohttp.ClientSession] = None
        self._cache: dict[str, tuple[float, Any]] = {}
        self._inflight: dict[str, asyncio.Future] = {}
        self._stats = {"calls": 0, "errors": 0, "cache_hits": 0,
                       "last_ms": 0.0, "last_error": ""}
        self._failures = 0
        self._open_until = 0.0

    # ---- 生命周期 ----

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout_s))
        return self._session

    # ---- 状态 ----

    def status(self) -> dict:
        now = time.monotonic()
        return {
            "enabled": self.enabled,
            "base_url": self.base_url,
            "ready": self.enabled and self._open_until <= now,
            "circuit": {
                "open": self._open_until > now,
                "open_for_s": round(max(self._open_until - now, 0.0), 1),
                "consecutive_failures": self._failures,
                "threshold": self.circuit_failures,
                "cooldown_s": self.circuit_cooldown_s,
            },
            "stats": dict(self._stats),
            "cache_entries": len(self._cache),
            "methods": sorted(THS_METHODS),
        }

    # ---- 入参校验 ----

    @staticmethod
    def _check_code(code: str) -> str:
        code = str(code or "").strip().upper()
        if not _CODE_RE.match(code):
            raise ThsBadArgument(f"股票代码不合法: {code!r}")
        return code

    @staticmethod
    def _check_date(value: str) -> str:
        value = str(value or "").strip()
        if not _DATE_RE.match(value):
            raise ThsBadArgument(f"日期不合法（需 YYYY-MM-DD）: {value!r}")
        return value

    def _build(self, method: str, params: dict) -> tuple[str, dict]:
        """按白名单生成 (path, query)。params 里 code/date 会被校验并用于路径。"""
        spec = THS_METHODS.get(method)
        if spec is None:
            raise ThsBadArgument(f"方法不在白名单: {method}")
        http_method, template, _ttl = spec
        path = template
        query = dict(params or {})
        if "{code}" in path:
            code = self._check_code(query.pop("code", ""))
            path = path.replace("{code}", code)
        if "{name}" in path:
            name = str(query.pop("name", "") or "").strip()
            if not name or len(name) > 64 or "/" in name or "\\" in name:
                raise ThsBadArgument(f"板块名不合法: {name!r}")
            path = path.replace("{name}", name)
        if "date" in query and query["date"] is not None:
            query["date"] = self._check_date(query["date"])
        if "trade_date" in query and query["trade_date"] is not None:
            query["trade_date"] = self._check_date(query["trade_date"])
        return path, {k: v for k, v in query.items() if v is not None}

    # ---- 调用 ----

    async def call(self, method: str, params: Optional[dict] = None,
                   use_cache: bool = True) -> Any:
        if not self.enabled:
            raise ThsDisabled("同花顺旁路未启用（config: ths.enabled=false）")
        now = time.monotonic()
        if self._open_until > now:
            raise ThsCircuitOpen(
                f"同花顺熔断中，{self._open_until - now:.0f}s 后重试")
        path, query = self._build(method, params or {})
        http_method, _template, ttl = THS_METHODS[method]
        key = f"{method}|{path}|{sorted(query.items())!r}"
        if self.cache_enabled and use_cache and ttl > 0:
            hit = self._cache.get(key)
            if hit and now - hit[0] < ttl:
                self._stats["cache_hits"] += 1
                return hit[1]
        # 单飞：并发同类请求只打一次上游
        inflight = self._inflight.get(key)
        if inflight is not None:
            return await asyncio.shield(inflight)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._inflight[key] = fut
        try:
            value = await self._do_call(http_method, path, query)
            if self.cache_enabled and ttl > 0:
                self._cache[key] = (time.monotonic(), value)
                if len(self._cache) > 512:  # 简单容量上限
                    oldest = min(self._cache, key=lambda k: self._cache[k][0])
                    self._cache.pop(oldest, None)
            if not fut.done():
                fut.set_result(value)
            return value
        except Exception as exc:
            if not fut.done():
                fut.set_exception(exc)
                # 标记异常已读取：没有并发等待者时，避免 asyncio 打
                # "Future exception was never retrieved" 噪音
                fut.exception()
            raise
        finally:
            self._inflight.pop(key, None)

    async def _do_call(self, http_method: str, path: str, query: dict) -> Any:
        url = self.base_url + path
        t0 = time.monotonic()
        self._stats["calls"] += 1
        try:
            session = self._get_session()
            async with session.request(http_method, url, params=query) as resp:
                body = await resp.text()
                if resp.status >= 400:
                    raise ThsUnavailable(
                        f"worker HTTP {resp.status}: {body[:200]}")
                try:
                    value = await resp.json(content_type=None)
                except Exception:  # noqa: BLE001
                    raise ThsUnavailable(f"worker 返回非 JSON: {body[:200]}")
        except ThsError:
            self._fail(f"{http_method} {path}")
            raise
        except asyncio.TimeoutError:
            self._fail(f"{http_method} {path} 超时 {self.timeout_s:.0f}s")
            raise ThsUnavailable(f"worker 超时（{self.timeout_s:.0f}s）") from None
        except aiohttp.ClientError as exc:
            self._fail(f"{http_method} {path}: {exc}")
            raise ThsUnavailable(f"worker 不可达: {exc}") from None
        self._stats["last_ms"] = round((time.monotonic() - t0) * 1000.0, 1)
        self._failures = 0
        return value

    def _fail(self, message: str) -> None:
        self._stats["errors"] += 1
        self._stats["last_error"] = message[:200]
        self._failures += 1
        if self._failures >= self.circuit_failures:
            self._open_until = time.monotonic() + self.circuit_cooldown_s
            log.warning("同花顺旁路熔断打开 %ss：%s", self.circuit_cooldown_s, message)

    # ---- 归一化（给 TDX↔THS 同尺对照用） ----

    @staticmethod
    def normalize_quote(raw: dict, source: str = "ths") -> dict:
        """把 worker 的报价压成与 TDX 可比较的最小集合（缺失为 None，不填 0）。"""
        def pick(*keys):
            for k in keys:
                if k in raw and raw[k] is not None:
                    return raw[k]
            return None
        return {
            "code": pick("code", "Code"),
            "source": source,
            "last": pick("price", "now", "Now"),
            "prev_close": pick("pre_close", "preclose", "LastClose"),
            "open": pick("open", "Open"),
            "high": pick("high", "High"),
            "low": pick("low", "Low"),
            "volume": pick("volume", "Volume"),
            "amount": pick("amount", "Amount"),
        }
