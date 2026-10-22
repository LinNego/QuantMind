"""桥 HTTP 客户端：只走 /api/v1/tdx/call 与 /api/v1/health，带重试与统计。"""
from __future__ import annotations

import asyncio
import json
import logging
import time

import aiohttp

log = logging.getLogger(__name__)


class BridgeError(Exception):
    """桥/通达信调用失败。status=HTTP 状态；code=桥错误码或 TDX_ERROR。"""

    def __init__(self, message: str, status: int = 0, code: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code


class BridgeClient:
    def __init__(self, base_url: str, token: str, timeout: float = 20.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._session: aiohttp.ClientSession | None = None
        self.stats = {
            "calls": 0,
            "errors": 0,
            "tdx_errors": 0,
            "last_error": "",
            "last_latency_ms": 0.0,
        }

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                connector=aiohttp.TCPConnector(limit=8, ttl_dns_cache=300),
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    async def _request(self, method: str, path: str, *, payload: dict | None = None,
                       timeout: float | None = None, retries: int = 2) -> dict:
        session = await self._get_session()
        url = f"{self.base_url}{path}"
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            t0 = time.monotonic()
            try:
                kwargs = {"headers": self._headers()}
                if timeout is not None:
                    kwargs["timeout"] = aiohttp.ClientTimeout(total=timeout)
                if payload is not None:
                    kwargs["json"] = payload
                async with session.request(method, url, **kwargs) as resp:
                    text = await resp.text()
                    elapsed_ms = (time.monotonic() - t0) * 1000.0
                    self.stats["last_latency_ms"] = round(elapsed_ms, 1)
                    if resp.status != 200:
                        # 429/5xx 可重试；4xx 直接失败
                        if resp.status in (429, 502, 503, 504) and attempt < retries:
                            await asyncio.sleep(0.3 * (2 ** attempt))
                            continue
                        self.stats["errors"] += 1
                        self.stats["last_error"] = f"HTTP {resp.status}: {text[:200]}"
                        raise BridgeError(
                            f"桥返回 HTTP {resp.status}: {text[:200]}", status=resp.status)
                    try:
                        return json.loads(text)
                    except ValueError as exc:
                        self.stats["errors"] += 1
                        self.stats["last_error"] = f"非 JSON 响应: {text[:120]}"
                        raise BridgeError(f"桥返回非 JSON: {text[:120]}") from exc
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_exc = exc
                if attempt < retries:
                    await asyncio.sleep(0.3 * (2 ** attempt))
                    continue
                self.stats["errors"] += 1
                self.stats["last_error"] = str(exc)[:200]
                raise BridgeError(f"桥连接失败: {exc}") from exc
        raise BridgeError(f"桥请求失败: {last_exc}")

    async def health(self, timeout: float = 5.0) -> dict:
        """GET /api/v1/health（免鉴权）。失败抛 BridgeError。"""
        return await self._request("GET", "/api/v1/health", timeout=timeout, retries=0)

    async def call(self, method: str, params: dict | None = None,
                   timeout: float = 20.0) -> dict:
        """POST /api/v1/tdx/call，做 success/ErrorId 双重检查。"""
        self.stats["calls"] += 1
        data = await self._request(
            "POST", "/api/v1/tdx/call",
            payload={"method": method, "params": params or {}},
            timeout=timeout,
        )
        if not data.get("success"):
            err = data.get("error") or {}
            self.stats["errors"] += 1
            self.stats["last_error"] = f"{err.get('code', '')}: {err.get('message', '')}"[:200]
            raise BridgeError(err.get("message", "桥调用失败"),
                              status=err.get("status", 0), code=err.get("code", ""))
        result = data.get("result")
        if isinstance(result, dict):
            error_id = str(result.get("ErrorId", "0"))
            if error_id not in ("0", "-1", "None"):
                self.stats["tdx_errors"] += 1
                msg = str(result.get("Error") or result.get("Msg") or result)
                self.stats["last_error"] = f"TDX ErrorId={error_id}: {msg[:160]}"
                raise BridgeError(
                    f"{method} 失败 ErrorId={error_id}: {msg[:240]}", code="TDX_ERROR")
        return result if isinstance(result, dict) else {"Value": result}

    async def get_stock_list(self, market: str = "5") -> list[str]:
        result = await self.call("get_stock_list", {"market": market, "list_type": 1})
        value = result.get("Value")
        if not isinstance(value, list):
            return []
        return [str(x.get("Code") or x.get("code")) for x in value
                if isinstance(x, dict) and (x.get("Code") or x.get("code"))]

    async def get_pricevol(self, codes: list[str]) -> dict[str, dict]:
        result = await self.call("get_pricevol", {"stock_list": codes})
        value = result.get("Value")
        return value if isinstance(value, dict) else {}

    async def get_market_snapshot(self, code: str) -> dict:
        return await self.call("get_market_snapshot", {"stock_code": code})

    async def get_trading_dates(self, count: int = 10) -> list[str]:
        result = await self.call("get_trading_dates", {"market": "5", "count": count})
        value = result.get("Value")
        if isinstance(value, list):
            out = []
            for item in value:
                if isinstance(item, dict):
                    out.append(str(item.get("Date") or item.get("date") or ""))
                else:
                    out.append(str(item))
            return [x for x in out if x]
        dates = result.get("Date")
        if isinstance(dates, list):
            return [str(x) for x in dates if x]
        return []
