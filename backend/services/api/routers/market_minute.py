"""实时分钟 K 线 API（1m/5m/15m/30m/60m，A 股）。

**数据链路**：``market:series:{PREFIX}``（各行情写侧的快照 ZSET）→ 本接口按需
重放当日样本 → :mod:`backend.shared.minute_bars` 聚合。不落库、不维护游标：
每次请求从当日样本重算，任务重启/接口多进程天然一致（同一组样本 → 同一组 Bar）。

**为什么不做「分钟表落库」**：TDX 桥的 ``get_market_data`` 分钟 K 线在本部署返回
空（客户端本地无分钟数据）；实时量的唯一来源是快照里的**当日累计** Volume/Amount。
分钟量只能由相邻样本差分得到，必须与样本同源、同生命周期重算——落一份派生表
反而会与 ``market:series`` 漂移。

**样本陈旧时就地补一针**：请求的票若最新样本超过 ``MIN_REFRESH_AGE_S``，接口
调用桥现查一次（``refresh_symbols_now``，best-effort，不抛）再重算；桥未配置 /
盘外 / 限流时静默回落已有样本。

**质量字段必须透传**：``span_s``（Bar 覆盖的会话秒数）与 ``samples`` 说明这根
Bar 是否真的被完整采样——热集轮转 ≈100s/只，1m Bar 的量可能是跨分钟累计差分，
消费方不得把它当逐分钟成交额。``volume=None`` + ``volume_unknown=true`` 表示该
分钟内跨源/累计倒挂/源累计长时间停滞（零差分不可信），量不可加（绝不填 0）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import replace
from datetime import date as date_cls
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.services.api.user_app.middleware.auth import get_current_user
from backend.shared.minute_bars import (
    CST,
    DEFAULT_PERIOD,
    PERIOD_MINUTES,
    Sample,
    aggregate_period,
    bars_by_source,
    normalize_period,
    sample_from_payload,
)
from backend.shared.stock_utils import StockCodeUtil

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/market", tags=["Market"])

#: 最新样本超过该秒数才触发一次桥现查（桥服务端本身有 3s 缓存）
MIN_REFRESH_AGE_S = 5.0

_A_SHARE_PREFIX = re.compile(r"^(SH|SZ|BJ)\d{6}$")


def _series_key(prefix: str) -> str:
    return f"market:series:{prefix}"


def _day_start_epoch(day: str) -> int:
    y, m, d = (int(x) for x in day.split("-"))
    return int(datetime(y, m, d, tzinfo=CST).timestamp())


def _parse_day(raw: str | None) -> str:
    if not raw:
        return datetime.now(CST).strftime("%Y-%m-%d")
    try:
        return date_cls.fromisoformat(raw).isoformat()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid date: {raw}") from exc


def _normalize_symbol(raw: str) -> str:
    prefix = StockCodeUtil.to_prefix(str(raw or "").strip().upper())
    if not _A_SHARE_PREFIX.match(prefix):
        raise HTTPException(
            status_code=400,
            detail=f"仅支持 A 股实时分钟（{raw!r} 无法归一为 SH/SZ/BJ 前缀码）",
        )
    return prefix


def _read_series(prefix: str, start_epoch: int, end_epoch: int) -> list[Sample]:
    """读 ``market:series`` 指定区间 → 样本列表（坏 member 跳过，不抛）。"""
    from backend.shared.remote_quote_config import make_sync_client

    client = make_sync_client()
    if client is None:
        raise RuntimeError(
            "行情 Redis 未配置（REMOTE_QUOTE_DISABLED/REMOTE_QUOTE_REDIS_HOST）"
        )
    try:
        members = client.zrangebyscore(
            _series_key(prefix), start_epoch, end_epoch, withscores=True
        )
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass

    samples: list[Sample] = []
    for member, score in members:
        try:
            payload = json.loads(member)
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        sample = sample_from_payload(payload, score=score)
        if sample is not None:
            samples.append(sample)
    samples.sort(key=lambda s: s.ts)
    return samples


def _latest_sample_in_key(prefix: str) -> Sample | None:
    """读 ZSET 最后一条（用于「今天还没样本时回退到最近交易日」）。"""
    from backend.shared.remote_quote_config import make_sync_client

    client = make_sync_client()
    if client is None:
        return None
    try:
        rows = client.zrevrange(_series_key(prefix), 0, 0, withscores=True)
    except Exception:  # noqa: BLE001
        return None
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
    if not rows:
        return None
    member, score = rows[0]
    try:
        payload = json.loads(member)
    except (TypeError, ValueError):
        return None
    return (
        sample_from_payload(payload, score=score) if isinstance(payload, dict) else None
    )


def _sample_meta(sample: Sample | None, now: int) -> dict[str, Any] | None:
    if sample is None:
        return None
    return {
        "ts": sample.ts,
        "datetime": datetime.fromtimestamp(sample.ts, tz=CST).isoformat(),
        "price": sample.price,
        "volume": sample.volume,
        "amount": sample.amount,
        "source": sample.source,
        "age_s": max(0, now - sample.ts),
    }


def build_payload(
    prefix: str,
    samples: list[Sample],
    period: str,
    *,
    day: str,
    now: int | None = None,
    limit: int = 240,
) -> dict[str, Any]:
    """样本 → 分钟 Bar 响应体（纯函数，便于单测；不含任何 I/O）。"""
    now = int(time.time()) if now is None else int(now)
    bars = bars_by_source(samples, symbol=prefix)

    # 最后一根若是当前分钟（或未来时间戳），不能标 complete
    bars = [replace(b, complete=b.complete and b.ts + 60 <= now) for b in bars]
    aggregated = aggregate_period(bars, period, symbol=prefix)[-limit:]

    sources = sorted({s.source for s in samples if s.source})
    last_sample = _sample_meta(samples[-1], now) if samples else None
    return {
        "symbol": prefix,
        "date": day,
        "period": period,
        "count": len(aggregated),
        "bars": [b.to_dict() for b in aggregated],
        "samples": len(samples),
        "sources": sources,
        "last_sample": last_sample,
        "quality": {
            "complete_bars": sum(1 for b in aggregated if b.complete),
            "unknown_volume_bars": sum(1 for b in aggregated if b.volume_unknown),
            # 热集轮转 ≈100s/只：span_s 远大于周期秒数说明这根 Bar 不是逐分钟采样
            "max_span_s": max((b.span_s for b in aggregated), default=0),
        },
        "source_used": "market:series",
    }


async def _maybe_refresh(
    prefix: str, last_sample: Sample | None, *, is_today: bool
) -> dict[str, Any]:
    """最新样本陈旧且在交易时段 → 桥现查一针（best-effort，绝不抛）。"""
    if not is_today:
        return {"attempted": False, "reason": "history"}
    age_s = None if last_sample is None else max(0, int(time.time()) - last_sample.ts)
    if age_s is not None and age_s < MIN_REFRESH_AGE_S:
        return {"attempted": False, "reason": "fresh", "age_s": age_s}
    try:
        from backend.services.live_trading.services.tdx_quote_feed import (
            is_trading_time,
            refresh_symbols_now,
        )

        if not is_trading_time():
            return {"attempted": False, "reason": "off_hours", "age_s": age_s}
        result = await refresh_symbols_now([prefix])
        return {"attempted": True, "ok": any(result.values()), "age_s": age_s}
    except Exception as exc:  # noqa: BLE001 - 补针失败不挡读取
        logger.warning("[MarketMinute] 现查快照失败 %s: %s", prefix, exc)
        return {
            "attempted": False,
            "reason": "refresh_error",
            "age_s": age_s,
            "error": str(exc)[:120],
        }


@router.get("/minute")
async def get_minute_kline(
    symbol: str = Query(..., description="600036.SH / SH600036 / 600036"),
    period: str = Query(
        DEFAULT_PERIOD,
        description="1m / 5m / 15m / 30m / 1h（=60m；返回 period 为规范键）",
    ),
    date: str | None = Query(
        None, description="交易日 YYYY-MM-DD，缺省=今天（无样本时回退最近交易日）"
    ),
    limit: int = Query(240, ge=1, le=1000, description="返回最近 N 根（聚合后）"),
    refresh: bool = Query(True, description="样本陈旧时是否现查桥快照补一针"),
    current_user: dict = Depends(get_current_user),
):
    """实时分钟 K 线（会话对齐；午休不跨桶，集合竞价并入 09:30）。"""
    _ = current_user
    period = normalize_period(period)
    if period not in PERIOD_MINUTES:
        raise HTTPException(
            status_code=400,
            detail=f"period {period} 非法，可选 {' / '.join(PERIOD_MINUTES)}",
        )
    prefix = _normalize_symbol(symbol)
    explicit_date = date is not None
    day = _parse_day(date)

    try:
        start = _day_start_epoch(day)
        samples = await asyncio.to_thread(_read_series, prefix, start, start + 86400)
        if not samples and not explicit_date:
            # 开盘前/周末：回退到 ZSET 里最近一条样本所在交易日，避免空图
            latest = await asyncio.to_thread(_latest_sample_in_key, prefix)
            if latest is not None:
                latest_day = datetime.fromtimestamp(latest.ts, tz=CST).strftime(
                    "%Y-%m-%d"
                )
                if latest_day != day:
                    day = latest_day
                    start = _day_start_epoch(day)
                    samples = await asyncio.to_thread(
                        _read_series, prefix, start, start + 86400
                    )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.warning("[MarketMinute] 读取行情序列失败 %s: %s", prefix, exc)
        raise HTTPException(status_code=503, detail=f"行情序列读取失败: {exc}") from exc

    refresh_meta: dict[str, Any] = {"attempted": False, "reason": "disabled"}
    today = datetime.now(CST).strftime("%Y-%m-%d")
    if refresh:
        refresh_meta = await _maybe_refresh(
            prefix, samples[-1] if samples else None, is_today=(day == today)
        )
        if refresh_meta.get("ok"):
            try:
                samples = await asyncio.to_thread(
                    _read_series, prefix, start, start + 86400
                )
            except Exception as exc:  # noqa: BLE001 - 补针后的重读失败按已有样本返回
                logger.warning("[MarketMinute] 补针后重读失败 %s: %s", prefix, exc)

    payload = build_payload(prefix, samples, period, day=day, limit=limit)
    payload["refresh"] = refresh_meta
    return {"success": True, "data": payload}
