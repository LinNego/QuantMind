"""止盈止损执行器的**通达信柜台**适配器。

为什么需要这一层：``sltp_executor`` 的 ``deps.client`` 是按 QMT ``xtquant`` 形状
写的（``configured`` / ``get_full_tick`` / ``get_positions`` /
``get_instrument_detail``），而本部署选定券商是通达信
（``broker:selected:CN=tdx``）。没有适配层时 ``_build_default_deps`` 拿到的是
未配置的 QMT 客户端，``configured=False`` 让 ``run_sltp_cycle`` 在评估前直接
返回——止损止盈永远停在「只提醒不下单」。

口径：
* **行情优先读标准键** ``market:snapshot:{prefix}``（持仓馈送与热集轮转写，
  source=tdx_bridge）：执行器 3s 节拍直查桥会与热集/采集/账户同步抢 600/min
  的共享预算，桥限流后表现为「行情缺失」而不是「没配置」。快照年龄超过
  ``CACHED_QUOTE_MAX_AGE_SEC`` 或缺失时才直查桥。
* **持仓用桥的实时可用量**：``available_volume`` → ``can_use_volume``。T+1
  当日买入该值为 0，执行器据此走「下一交易日自动继续评估」而不是当日终态。
* **合约详情只回 ``PreClose``**：通达信快照没有涨跌停字段，执行器在通达信下
  强制 ``market`` 保护价模式，不依赖跌停价。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from backend.shared.logging_config import get_logger
from backend.shared.stock_utils import StockCodeUtil

logger = get_logger(__name__)

#: 标准键快照可接受的年龄（秒）。持仓馈送按 ``max(3, n×1.5)`` 秒轮转，
#: 9 只持仓约 13.5s 一轮；超过该年龄说明馈送停了或该票不在馈送范围，转直查。
CACHED_QUOTE_MAX_AGE_SEC = 20.0

#: 直查桥的超时/并发：桥活着但通达信客户端挂起时不能拖死执行器轮询。
BRIDGE_CALL_TIMEOUT_SEC = 2.0
BRIDGE_CONCURRENCY = 4

SNAPSHOT_KEY_TPL = "market:snapshot:{}"


def _to_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None  # 过滤 NaN


def _suffix(raw: str) -> str:
    code = str(raw or "").strip().upper()
    if not code:
        return ""
    if "." in code:
        return code
    return StockCodeUtil.to_suffix(code) or code


def _prefix(raw: str) -> str:
    code = str(raw or "").strip().upper()
    if not code:
        return ""
    if "." in code:
        return StockCodeUtil.to_prefix(code) or code
    return code


class TdxSltpClient:
    """``SltpDeps.client`` 的通达信实现（懒加载单例依赖由调用方注入）。"""

    def __init__(self, pusher: Any = None) -> None:
        self._pusher = pusher
        self._snapshot_redis: Any = None
        self._snapshot_redis_broken = False

    # -- 依赖 -----------------------------------------------------------
    @property
    def pusher(self) -> Any:
        if self._pusher is None:
            from backend.services.live_trading.services.tdx_push_service import (
                tdx_pusher,
            )

            self._pusher = tdx_pusher
        return self._pusher

    def _snapshot_client(self) -> Any:
        """标准键 Redis（与 ``tdx_quote_feed._write_snapshot`` 同一个客户端口径）。"""
        if self._snapshot_redis is not None or self._snapshot_redis_broken:
            return self._snapshot_redis
        try:
            from backend.services.live_trading.routers.real_trading_utils import (
                _get_stream_series_redis_client,
            )

            client, _, _ = _get_stream_series_redis_client()
            self._snapshot_redis = client
        except Exception as exc:  # noqa: BLE001
            self._snapshot_redis_broken = True
            logger.warning("[SltpTdx] 行情标准键 Redis 不可用，全部直查桥: %s", exc)
        return self._snapshot_redis

    @property
    def configured(self) -> bool:
        return bool(getattr(self.pusher, "enabled", False))

    # -- 行情 -----------------------------------------------------------
    async def get_full_tick(self, codes: list[str]) -> dict[str, Any]:
        """批量行情 → ``{suffix 代码: {lastPrice, lastClose, ...}}``。

        先读标准键（新鲜则直接用），缺的再直查桥。**绝不抛异常**：执行器按
        「取不到价」计 miss 并告警，不能让一次桥抖动中断整轮。
        """
        wanted = [(_suffix(code), code) for code in codes or () if str(code).strip()]
        if not wanted:
            return {}
        result: dict[str, Any] = {}
        missing: list[str] = []
        cached = await self._read_cached([suffix for suffix, _ in wanted])
        for suffix, raw_key in wanted:
            tick = cached.get(suffix)
            if tick is not None:
                # 两个口径都覆盖：调用方可能按后缀查，也可能按传入的原样码查
                # （``get_instrument_detail`` 就是这样用 ``_suffix(code)`` 回查）。
                result[raw_key] = tick
                result[suffix] = tick
            else:
                missing.append(suffix)
        if missing:
            direct = await self._fetch_bridge(missing)
            for suffix in missing:
                tick = direct.get(suffix)
                if tick is not None:
                    # 执行器用传入的 code 作键；两端口径在这里都覆盖上。
                    result[suffix] = tick
                    for same_suffix, raw_key in wanted:
                        if same_suffix == suffix:
                            result[raw_key] = tick
        return result

    async def _read_cached(self, suffixes: list[str]) -> dict[str, Any]:
        client = self._snapshot_client()
        if client is None or not suffixes:
            return {}

        def _do() -> dict[str, Any]:
            out: dict[str, Any] = {}
            pipe = client.pipeline(transaction=False)
            for suffix in suffixes:
                pipe.hgetall(SNAPSHOT_KEY_TPL.format(_prefix(suffix).lower()))
            now = time.time()
            for suffix, raw in zip(suffixes, pipe.execute(), strict=False):
                if not isinstance(raw, dict):
                    continue
                ts = _to_float(raw.get("timestamp"))
                price = _to_float(raw.get("Now"))
                if ts is None or price is None or price <= 0:
                    continue
                if now - ts > CACHED_QUOTE_MAX_AGE_SEC:
                    continue
                out[suffix] = {
                    "lastPrice": price,
                    "lastClose": _to_float(raw.get("PreClose")),
                    "open": _to_float(raw.get("Open")),
                    "high": _to_float(raw.get("High")),
                    "low": _to_float(raw.get("Low")),
                    "volume": _to_float(raw.get("Volume")),
                    "amount": _to_float(raw.get("Amount")),
                    "quote_ts": ts,
                    "source": "cache",
                }
            return out

        try:
            return await asyncio.to_thread(_do)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[SltpTdx] 读行情标准键失败，转直查桥: %s", exc)
            return {}

    async def _fetch_bridge(self, suffixes: list[str]) -> dict[str, Any]:
        sem = asyncio.Semaphore(BRIDGE_CONCURRENCY)

        async def _one(suffix: str) -> tuple[str, dict[str, Any] | None]:
            async with sem:
                try:
                    raw = await asyncio.wait_for(
                        self.pusher.tdx_call(
                            "get_market_snapshot", {"stock_code": suffix}
                        ),
                        timeout=BRIDGE_CALL_TIMEOUT_SEC,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.debug("[SltpTdx] 直查 %s 失败: %s", suffix, exc)
                    return suffix, None
            price = _to_float((raw or {}).get("Now"))
            if price is None or price <= 0:
                return suffix, None
            return suffix, {
                "lastPrice": price,
                "lastClose": _to_float(raw.get("LastClose")),
                "open": _to_float(raw.get("Open")),
                "high": _to_float(raw.get("Max")),
                "low": _to_float(raw.get("Min")),
                "volume": _to_float(raw.get("Volume")),
                "amount": _to_float(raw.get("Amount")),
                "quote_ts": time.time(),
                "source": "bridge",
            }

        pairs = await asyncio.gather(*(_one(suffix) for suffix in suffixes))
        return {suffix: tick for suffix, tick in pairs if tick is not None}

    async def get_instrument_detail(self, code: str) -> dict[str, Any]:
        """合约详情（通达信只有 ``LastClose``，涨跌停字段如实缺省）。"""
        ticks = await self.get_full_tick([code])
        tick = ticks.get(_suffix(code)) or {}
        if not tick:
            return {}
        return {
            "InstrumentID": _suffix(code),
            "PreClose": tick.get("lastClose"),
            "LastPrice": tick.get("lastPrice"),
        }

    # -- 持仓 -----------------------------------------------------------
    async def get_positions(self) -> list[dict[str, Any]]:
        """柜台持仓（桥实时；失败返回空列表，由执行器按「无持仓」告警）。"""
        raw = await self.pusher.pull_positions()
        out: list[dict[str, Any]] = []
        for item in raw or ():
            code = str(item.get("stock_code") or "").strip()
            volume = _to_float(item.get("total_volume") or item.get("volume")) or 0.0
            if not code or volume <= 0:
                continue
            available = _to_float(
                item.get("available_volume")
                if item.get("available_volume") is not None
                else item.get("can_use_volume")
            )
            cost = _to_float(item.get("cost_price")) or 0.0
            out.append(
                {
                    "stock_code": _suffix(code),
                    "symbol": _prefix(code),
                    "instrument_name": str(item.get("stock_name") or "").strip(),
                    "volume": volume,
                    # 桥缺该字段时按 0 处理：宁可「T+1 暂不可卖」告警，
                    # 也不要在当日买入的仓上报一张必被柜台拒绝的卖单。
                    "can_use_volume": available if available is not None else 0.0,
                    "open_price": cost,
                    "avg_price": cost,
                    "cost_price": cost,
                    "market_value": _to_float(item.get("market_value")),
                }
            )
        return out
