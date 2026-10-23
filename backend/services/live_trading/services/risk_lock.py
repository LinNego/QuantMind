"""Intraday risk locks: after a trigger fires, strategy must not buy back."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from collections.abc import Iterable
from typing import Any
from zoneinfo import ZoneInfo

from backend.shared.redis_unwrap import raw_redis_client
from backend.shared.stock_utils import StockCodeUtil

_SH_TZ = ZoneInfo("Asia/Shanghai")
ACCOUNT_LOCK_PREFIX = "risk:lock:account:"
SYMBOL_LOCK_PREFIX = "risk:lock:symbol:"
FIRED_PREFIX = "risk:fired:"


def normalize_lock_user(user_id: object) -> str:
    raw = str(user_id or "").strip()
    if raw.isdigit():
        return str(int(raw))
    return raw or "0"


def normalize_lock_symbol(symbol: str) -> str:
    suffix = StockCodeUtil.to_suffix(str(symbol or "").strip())
    return suffix or str(symbol or "").strip().upper()


def _trade_date_str(trade_date: date | str) -> str:
    if isinstance(trade_date, date):
        return trade_date.isoformat()
    return str(trade_date)


def account_lock_key(tenant_id: str, user_id: object, trade_date: date | str) -> str:
    return (
        f"{ACCOUNT_LOCK_PREFIX}{tenant_id or 'default'}:"
        f"{normalize_lock_user(user_id)}:{_trade_date_str(trade_date)}"
    )


def symbol_lock_key(
    tenant_id: str, user_id: object, trade_date: date | str, symbol: str
) -> str:
    return (
        f"{SYMBOL_LOCK_PREFIX}{tenant_id or 'default'}:"
        f"{normalize_lock_user(user_id)}:{_trade_date_str(trade_date)}:"
        f"{normalize_lock_symbol(symbol)}"
    )


def fired_key(
    rule_id: int | None,
    tenant_id: str,
    user_id: object,
    trade_date: date | str,
    symbol: str,
) -> str:
    return (
        f"{FIRED_PREFIX}{int(rule_id or 0)}:{tenant_id or 'default'}:"
        f"{normalize_lock_user(user_id)}:{_trade_date_str(trade_date)}:"
        f"{normalize_lock_symbol(symbol)}"
    )


def lock_ttl_seconds(now: datetime | None = None) -> int:
    current = now or datetime.now(_SH_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=_SH_TZ)
    else:
        current = current.astimezone(_SH_TZ)
    end_of_day = current.replace(hour=23, minute=59, second=59, microsecond=0)
    return max(60, int((end_of_day + timedelta(hours=4) - current).total_seconds()))


@dataclass
class RiskLocks:
    account_frozen: bool = False
    symbols: set[str] = field(default_factory=set)

    def is_symbol_locked(self, symbol: str) -> bool:
        if self.account_frozen:
            return True
        suffix = normalize_lock_symbol(symbol)
        prefix = StockCodeUtil.to_prefix(symbol)
        return suffix in self.symbols or prefix in self.symbols or symbol in self.symbols


def _raw_redis(redis: Any):
    # redis-py 8 原生 client 自带 client() 方法，`getattr(..., redis)` 会返回
    # bound method（2026-10-23 评分 worker 同款故障）；统一走共享解包。
    return raw_redis_client(redis)


def load_risk_locks(redis: Any, tenant_id: str, user_id: object, trade_date: date | str) -> RiskLocks:
    client = _raw_redis(redis)
    locks = RiskLocks()
    if client is None:
        return locks
    try:
        if client.get(account_lock_key(tenant_id, user_id, trade_date)):
            locks.account_frozen = True
        pattern = (
            f"{SYMBOL_LOCK_PREFIX}{tenant_id or 'default'}:"
            f"{normalize_lock_user(user_id)}:{_trade_date_str(trade_date)}:*"
        )
        for raw_key in client.scan_iter(match=pattern, count=200):
            key = str(raw_key)
            symbol = key.rsplit(":", 1)[-1]
            if symbol:
                locks.symbols.add(symbol)
    except Exception:
        return locks
    return locks


def write_account_lock(redis: Any, tenant_id: str, user_id: object, trade_date: date | str) -> None:
    client = _raw_redis(redis)
    if client is None:
        return
    client.set(account_lock_key(tenant_id, user_id, trade_date), "1", ex=lock_ttl_seconds())


def write_symbol_lock(
    redis: Any, tenant_id: str, user_id: object, trade_date: date | str, symbol: str
) -> None:
    client = _raw_redis(redis)
    if client is None:
        return
    client.set(
        symbol_lock_key(tenant_id, user_id, trade_date, symbol),
        "1",
        ex=lock_ttl_seconds(),
    )


def mark_fired(
    redis: Any,
    rule_id: int | None,
    tenant_id: str,
    user_id: object,
    trade_date: date | str,
    symbol: str,
    cooldown_seconds: int | None = None,
) -> None:
    client = _raw_redis(redis)
    if client is None:
        return
    ttl = cooldown_seconds if cooldown_seconds and cooldown_seconds > 0 else lock_ttl_seconds()
    client.set(
        fired_key(rule_id, tenant_id, user_id, trade_date, symbol),
        "1",
        ex=max(60, int(ttl)),
    )


def already_fired(
    redis: Any,
    rule_id: int | None,
    tenant_id: str,
    user_id: object,
    trade_date: date | str,
    symbol: str,
) -> bool:
    client = _raw_redis(redis)
    if client is None:
        return False
    try:
        return bool(client.get(fired_key(rule_id, tenant_id, user_id, trade_date, symbol)))
    except Exception:
        return False


def filter_buy_orders(orders: Iterable[Any], locks: RiskLocks) -> list[Any]:
    kept: list[Any] = []
    for order in orders:
        side = str(getattr(order, "side", "") or "").upper()
        symbol = str(getattr(order, "symbol", "") or "")
        if side == "BUY" and locks.is_symbol_locked(symbol):
            continue
        kept.append(order)
    return kept
