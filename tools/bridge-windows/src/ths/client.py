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
from urllib.parse import quote

import aiohttp

log = logging.getLogger(__name__)

_CODE_RE = re.compile(r"^(?:[0-9]{6})(?:\.(?:SH|SZ|BJ))?$", re.IGNORECASE)
_BOARD_RE = re.compile(r"^[0-9A-Z]{2,8}$")
_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")

#: 方法白名单：name -> (HTTP 方法, 路径模板, TTL 秒)。TTL=0 表示不缓存。
#: 覆盖 worker 全部只读端点（2026-10-23 对照 app.py 路由表）。上游改名时
#: **只改这一张表**即可，桥的其他代码不用动。
THS_METHODS: dict[str, tuple[str, str, float]] = {
    # 连接/状态
    "status": ("GET", "/api/status", 0.0),
    "qr_status": ("GET", "/api/qr/status", 0.0),
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


#: 慢接口：worker 侧允许 45s（板块成分/大 K 线/超级盘口），桥侧必须放宽，
#: 否则会被 8s 全局超时误杀成 502。
THS_SLOW_METHODS = frozenset({
    "kline", "history_timeline", "auction", "closing_auction", "intraday_auctions",
    "board_constituents", "board_quotes", "board_timeline", "board_kline",
    "board_auction", "superorder", "superorder_bse", "superorder_window",
    "superorder_replay", "superorder_replay_snapshot", "order_details",
    "order_queues", "stocks", "stocks2", "stock_list_ranked", "hot", "dde_rank",
    "market_snapshot", "boards", "group",
})
THS_SLOW_TIMEOUT_S = 30.0


class ThsError(RuntimeError):
    """同花顺旁路的基础异常。"""


class ThsDisabled(ThsError):
    """配置里 ths.enabled=false。"""


class ThsUnavailable(ThsError):
    """worker 不可达 / 上游报错。"""


class ThsBusy(ThsError):
    """worker 饱和 / 桥侧闸门满。

    这不是上游故障：**不计入熔断**（2026-10-23 压测：忙 503 连打 5 次就把
    整条 THS 旁路熔断 30s，全部流量跌回 TDX）。调用方按「本次源失败」处理，
    由聚合层回落即可。
    """


class ThsCircuitOpen(ThsError):
    """熔断打开：短时间内不再打上游。"""


class ThsBadArgument(ValueError):
    """入参不合法（代码/日期格式）。"""


class ThsClient:
    """到本机 ths-worker 的只读客户端。线程/协程安全（单事件循环内）。"""

    def __init__(self, base_url: str = "http://127.0.0.1:8765",
                 enabled: bool = False, timeout_s: float = 8.0,
                 cache_enabled: bool = True, circuit_failures: int = 5,
                 circuit_cooldown_s: float = 30.0,
                 max_concurrency: int = 4, gate_wait_s: float = 0.3,
                 circuit_timeout_failures: int = 10):
        self.base_url = (base_url or "").rstrip("/")
        self.enabled = bool(enabled)
        self.timeout_s = float(timeout_s or 8.0)
        self.cache_enabled = bool(cache_enabled)
        self.circuit_failures = max(1, int(circuit_failures or 5))
        self.circuit_cooldown_s = float(circuit_cooldown_s or 30.0)
        # 出站并发闸门（2026-10-23 压测）：worker 的 MAIN 连接是串行的，
        # 无限并发只会把请求堆在 worker 线程池里造成队头阻塞与断线。
        self.max_concurrency = max(1, int(max_concurrency or 4))
        self.gate_wait_s = max(0.0, float(gate_wait_s or 0.0))
        # 超时单独计权：收盘后 THS 偶发 1-4s 慢响应，和「连不上」同权会把
        # 整条旁路误熔断 30s（实测 2026-10-23）。传输错误 5 次熔断，超时 10 次。
        self.circuit_timeout_failures = max(1, int(circuit_timeout_failures or 10))
        self._gate: Optional[asyncio.Semaphore] = None
        self._gate_loop = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._cache: dict[str, tuple[float, Any]] = {}
        self._inflight: dict[str, asyncio.Task] = {}
        self._stats = {"calls": 0, "errors": 0, "cache_hits": 0,
                       "gate_rejects": 0, "last_ms": 0.0, "last_error": ""}
        self._failures = 0
        self._timeout_failures = 0
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

    def _get_gate(self) -> asyncio.Semaphore:
        """出站并发闸门；按事件循环懒创建（测试会在不同 loop 里复用客户端）。"""
        loop = asyncio.get_running_loop()
        if self._gate is None or self._gate_loop is not loop:
            self._gate = asyncio.Semaphore(self.max_concurrency)
            self._gate_loop = loop
        return self._gate

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
                "consecutive_timeouts": self._timeout_failures,
                "threshold": self.circuit_failures,
                "timeout_threshold": self.circuit_timeout_failures,
                "cooldown_s": self.circuit_cooldown_s,
            },
            "stats": dict(self._stats),
            "cache_entries": len(self._cache),
            "methods": sorted(THS_METHODS),
        }

    # ---- 入参校验 ----

    @staticmethod
    def _check_code(code: str) -> str:
        """接受 600519 / 600519.SH / SH600519 三种写法，返回后缀式。"""
        raw = str(code or "").strip().upper()
        for prefix in ("SH", "SZ", "BJ"):
            if raw.startswith(prefix) and raw[2:].isdigit():
                raw = f"{raw[2:]}.{prefix}"
                break
        if not _CODE_RE.match(raw):
            raise ThsBadArgument(f"股票代码不合法: {code!r}")
        return raw

    @staticmethod
    def _bare(code: str) -> str:
        """worker 的行情接口要 6 位裸码（实测 600519 11ms 有数据、
        600519.SH 32s 空列表）；板块码同理（880081.SH → 880081）。

        例外：**后缀与代码前缀推断出的市场矛盾时保留完整代码**。
        worker 只按裸码前缀推断市场（000001 → 深市平安银行），若把
        `000001.SH`（上证指数）去后缀会被静默当成平安银行返回——宁可不
        命中（worker 报空/错），也不能返回另一只标的的数据（审计 HIGH-2）。
        板块码（88xxxx）是例外中的例外：worker 只认裸码。
        """
        raw = str(code or "").strip().upper()
        num, _, suffix = raw.partition(".")
        if not suffix:
            return num
        if num.startswith("88"):  # 板块码：880081.SH → 880081
            return num
        inferred = ("SH" if num[:1] in ("6", "9")
                    else "BJ" if num[:1] in ("4", "8")
                    else "SZ")
        return num if suffix == inferred else raw

    @staticmethod
    def _check_board(code: str) -> str:
        """板块 ID（如 BC9E）不是股票代码，单独校验。"""
        raw = str(code or "").strip().upper()
        if not _BOARD_RE.match(raw):
            raise ThsBadArgument(f"板块代码不合法: {code!r}")
        return raw

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
        if "codes" in query and query["codes"]:
            query["codes"] = ",".join(
                self._bare(c) for c in str(query["codes"]).split(",") if c.strip())
        if "{code}" in path:
            if path.startswith("/api/board/"):
                # 板块端点：{code} 是 block_id（BC9E），不是股票代码
                path = path.replace("{code}", self._check_board(query.pop("code", "")))
            else:
                code = self._check_code(query.pop("code", ""))
                path = path.replace("{code}", self._bare(code))
        if "{name}" in path:
            name = str(query.pop("name", "") or "").strip()
            if (not name or len(name) > 64 or "/" in name or "\\" in name
                    or name in (".", "..")):
                raise ThsBadArgument(f"板块名不合法: {name!r}")
            # 路径段必须编码：`?`/`#`/`%` 未编码会被当成 query/fragment 拆开
            path = path.replace("{name}", quote(name, safe=""))
        if "date" in query and query["date"] is not None:
            query["date"] = self._check_date(query["date"])
        if "trade_date" in query and query["trade_date"] is not None:
            query["trade_date"] = self._check_date(query["trade_date"])
        return path, {k: v for k, v in query.items() if v is not None}

    # ---- 调用 ----

    async def call(self, method: str, params: Optional[dict] = None,
                   use_cache: bool = True,
                   timeout_s: Optional[float] = None) -> Any:
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
        # 单飞：并发同类请求只打一次上游。共享的是 **Task** 而不是 Future：
        # 任一等待者被取消（客户端断开/上层超时）后任务本身继续跑完，其他
        # 等待者照常拿结果；旧实现由首调用者取消共享 future，会连带毒死所有
        # 等待者（审计 HIGH-1）。
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.get_running_loop().create_task(
                self._call_shared(key, method, http_method, path, query, ttl,
                                  timeout_s))
            self._inflight[key] = task
            task.add_done_callback(lambda t, k=key: self._drop_inflight(k, t))
        return await asyncio.shield(task)

    async def _call_shared(self, key: str, method: str, http_method: str,
                           path: str, query: dict, ttl: float,
                           timeout_s: Optional[float]) -> Any:
        slow = method in THS_SLOW_METHODS
        eff_timeout = timeout_s or (THS_SLOW_TIMEOUT_S if slow else None)
        gate = self._get_gate()
        try:
            await asyncio.wait_for(gate.acquire(), timeout=self.gate_wait_s)
        except asyncio.TimeoutError:
            self._stats["gate_rejects"] += 1
            raise ThsBusy(
                f"同花顺并发已满（{self.max_concurrency} 路上限），请稍后重试"
            ) from None
        try:
            value = await self._do_call(http_method, path, query,
                                        timeout_s=eff_timeout)
        finally:
            gate.release()
        if self.cache_enabled and ttl > 0:
            self._cache[key] = (time.monotonic(), value)
            if len(self._cache) > 512:  # 简单容量上限
                oldest = min(self._cache, key=lambda k: self._cache[k][0])
                self._cache.pop(oldest, None)
        return value

    def _drop_inflight(self, key: str, task: asyncio.Task) -> None:
        if self._inflight.get(key) is task:
            self._inflight.pop(key, None)
        if not task.cancelled():
            # 读取一次异常：所有等待者都取消时避免 asyncio 打
            # "Task exception was never retrieved" 噪音。
            task.exception()

    async def _do_call(self, http_method: str, path: str, query: dict,
                       timeout_s: Optional[float] = None) -> Any:
        url = self.base_url + path
        t0 = time.monotonic()
        self._stats["calls"] += 1
        try:
            session = self._get_session()
            # 注意：aiohttp 里 timeout=None = **关闭超时**（不是用 session 默认值）。
            # 所以只有显式给了 timeout_s 才传该参数，否则沿用 session 的 8s。
            kwargs = {"params": query}
            if timeout_s:
                kwargs["timeout"] = aiohttp.ClientTimeout(total=timeout_s)
            async with session.request(http_method, url, **kwargs) as resp:
                body = await resp.text()
                # 4xx 是调用方参数问题，**不能**算进熔断（否则几次错参就把整条
                # 旁路熔断 30s）；只有 5xx/传输异常才是上游故障。
                if resp.status >= 500:
                    if resp.status == 503 and (
                            '"worker_busy"' in body
                            or '"worker_timeout"' in body):
                        # 饱和/自愈超时：本请求回落，不污染熔断计数
                        raise ThsBusy(f"worker 饱和: {body[:160]}")
                    raise ThsUnavailable(
                        f"worker HTTP {resp.status}: {body[:200]}")
                if resp.status >= 400:
                    raise ThsBadArgument(
                        f"worker HTTP {resp.status}: {body[:200]}")
                try:
                    value = await resp.json(content_type=None)
                except Exception:  # noqa: BLE001
                    raise ThsUnavailable(
                        f"worker 返回非 JSON: {body[:200]}") from None
        except ThsBusy:
            raise
        except ThsError:
            self._fail(f"{http_method} {path}")
            raise
        except asyncio.TimeoutError:
            self._fail(f"{http_method} {path} 超时 {self.timeout_s:.0f}s",
                       timeout=True)
            raise ThsUnavailable(f"worker 超时（{self.timeout_s:.0f}s）") from None
        except aiohttp.ClientError as exc:
            self._fail(f"{http_method} {path}: {exc}")
            raise ThsUnavailable(f"worker 不可达: {exc}") from None
        self._stats["last_ms"] = round((time.monotonic() - t0) * 1000.0, 1)
        self._failures = 0
        self._timeout_failures = 0
        return value

    async def request_raw(self, http_method: str, path: str,
                          params: Optional[dict] = None,
                          timeout_s: Optional[float] = None) -> tuple[int, str, bytes]:
        """原始透传（只给固定路径的旁路端点用：扫码 PNG / JSON）。

        与 `call()` 共享熔断与统计，但不走方法白名单——调用方必须是桥内部的
        固定路径，绝不接受用户传入的 path。
        """
        if not self.enabled:
            raise ThsDisabled("同花顺旁路未启用（config: ths.enabled=false）")
        now = time.monotonic()
        if self._open_until > now:
            raise ThsCircuitOpen(
                f"同花顺熔断中，{self._open_until - now:.0f}s 后重试")
        t0 = time.monotonic()
        self._stats["calls"] += 1
        try:
            session = self._get_session()
            kwargs = {"params": params}
            if timeout_s:
                kwargs["timeout"] = aiohttp.ClientTimeout(total=timeout_s)
            async with session.request(http_method, self.base_url + path,
                                       **kwargs) as resp:
                body = await resp.read()
                ctype = resp.headers.get("Content-Type", "application/octet-stream")
                if resp.status >= 500:
                    if resp.status == 503 and (
                            b'"worker_busy"' in body
                            or b'"worker_timeout"' in body):
                        raise ThsBusy(
                            f"worker 饱和: {body[:160]!r}")
                    raise ThsUnavailable(
                        f"worker HTTP {resp.status}: {body[:200]!r}")
                if resp.status >= 400:
                    raise ThsBadArgument(
                        f"worker HTTP {resp.status}: {body[:200]!r}")
        except ThsBusy:
            raise
        except ThsError:
            self._fail(f"{http_method} {path}")
            raise
        except asyncio.TimeoutError:
            self._fail(f"{http_method} {path} 超时", timeout=True)
            raise ThsUnavailable(f"worker 超时（{self.timeout_s:.0f}s）") from None
        except aiohttp.ClientError as exc:
            self._fail(f"{http_method} {path}: {exc}")
            raise ThsUnavailable(f"worker 不可达: {exc}") from None
        self._stats["last_ms"] = round((time.monotonic() - t0) * 1000.0, 1)
        self._failures = 0
        self._timeout_failures = 0
        return resp.status, ctype, body

    def _fail(self, message: str, timeout: bool = False) -> None:
        self._stats["errors"] += 1
        self._stats["last_error"] = message[:200]
        if timeout:
            self._timeout_failures += 1
            if self._timeout_failures >= self.circuit_timeout_failures:
                self._open_until = time.monotonic() + self.circuit_cooldown_s
                log.warning("同花顺旁路熔断打开 %ss（连续超时 %d 次）：%s",
                            self.circuit_cooldown_s, self._timeout_failures,
                            message)
            return
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
