"""评估中心手动重评指令（Redis 契约，API 与 trade worker 共用）。

**为什么不让 API 直接跑评分**：六卡评分是 pandas/parquet 重活（模型卡实测近 100s，
因子 PFS 取数更久）。API 进程里跑会把交易 HTTP 全部卡住，重演 2026-10-20 的
看门狗杀进程崩溃循环。正确形态是 **API 只写指令，trade worker 消费**。

键全部落在 **trade 库（db2）**——与 `eval:scores:done:*` 同一个库；API 侧必须用
`trade_shared.redis_client.get_redis()`，拿别的库写指令 = 结构性空转（worker 永远
读不到，且全链无报错）。

契约：
- `eval:run:request`：最多一个待消费请求，`SET NX` 去重，TTL 1h；
- `eval:run:status`：最近一次手动跑批状态，TTL 24h；
- `eval:run:lock`：防重复 worker 并发跑批，token 比较后释放，TTL 1h。

状态体是前端稳定契约（`state` 五态 + 计数 + 错误摘要），新增字段只增不改。
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

from backend.shared.redis_unwrap import raw_redis_client

EVAL_RUN_REQUEST_KEY = "eval:run:request"
EVAL_RUN_STATUS_KEY = "eval:run:status"
EVAL_RUN_LOCK_KEY = "eval:run:lock"

EVAL_RUN_REQUEST_TTL_SECONDS = 3600
EVAL_RUN_STATUS_TTL_SECONDS = 24 * 3600
EVAL_RUN_LOCK_TTL_SECONDS = 3600

RUN_STATES = ("idle", "queued", "running", "done", "error")
_ACTIVE_STATES = frozenset({"queued", "running"})

# A 股无夏令时：日期边界固定 +08:00（与 eval worker / 影子对照同口径）
_CST = timezone(timedelta(hours=8))
_MAX_STATUS_ERRORS = 20


class EvalRunUnavailable(RuntimeError):
    """Redis 不可用，指令无法写入/读取。"""


class EvalRunBusy(RuntimeError):
    """已有排队或执行中的手动重评。`status` 是当前状态快照（给 409 用）。"""

    def __init__(self, status: dict[str, Any]):
        super().__init__("已有评估重评在排队或执行")
        self.status = status


def _raw(redis: Any) -> Any:
    """兼容 `RedisClient` 包装器（`.client`）与原生 client（同 worker 既有纪律）。

    统一走 `redis_unwrap.raw_redis_client`：redis-py 8 的原生 client 自带
    `client()` 方法，旧 `hasattr(redis, "client")` 会把它误判成包装器并返回
    bound method（2026-10-23 `'function' object has no attribute 'set'` 故障）。

    包装器 `.client is None` 表示连接失败，必须返回 None 让调用方报「不可用」；
    不能回退到包装器自己的 `set`——它的签名是 `(key, value, ttl=None)`，
    收到 `nx/ex` 会 TypeError 而不是明确失败。
    """
    return raw_redis_client(redis)


def _now_utc(now: datetime | None = None) -> datetime:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _iso(now: datetime | None = None) -> str:
    return _now_utc(now).isoformat(timespec="seconds")


def market_today(now: datetime | None = None) -> date:
    """市场自然日（+08:00）。日期校验用它，不用宿主 UTC/本地时区。"""
    return _now_utc(now).astimezone(_CST).date()


def validate_run_date(value: Any, *, today: date | None = None) -> str | None:
    """请求日期归一：空 → None；必须 `YYYY-MM-DD` 且不晚于今天。

    拒绝未来日：每日选股的快照还不存在，跑了只会落空卡或覆盖错误对象。
    """
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = date.fromisoformat(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("date 必须是 YYYY-MM-DD") from exc
    if parsed > (today or market_today()):
        raise ValueError("date 不能晚于今天")
    return parsed.isoformat()


def eval_worker_enabled() -> bool:
    """与 `eval_scores_service._config()` 同一 env（API 只做「关了就明确拒绝」）。"""
    raw = str(os.getenv("EVAL_SCORES_WORKER_ENABLED", "")).strip().lower()
    if not raw:
        return True
    return raw in {"1", "true", "yes", "on"}


def idle_status() -> dict[str, Any]:
    """无手动跑批记录时的稳定空状态（前端直接渲染，不必判空）。"""
    return {
        "state": "idle",
        "request_id": None,
        "date": None,
        "trigger": "manual",
        "requested_by": None,
        "tenant_id": None,
        "requested_at": None,
        "started_at": None,
        "finished_at": None,
        "total_scored": None,
        "total_errors": None,
        "errors": [],
        "backfill": None,
        "elapsed_sec": None,
        "note": "",
    }


def build_run_request(
    *,
    requested_by: Any = "",
    tenant_id: Any = "default",
    date_str: str | None = None,
    now: datetime | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    return {
        "request_id": str(request_id or uuid.uuid4().hex),
        "date": date_str,
        "tenant_id": str(tenant_id or "default"),
        "requested_by": str(requested_by or ""),
        "requested_at": _iso(now),
    }


def _compact_backfill(backfill: Any) -> dict[str, Any] | None:
    """回填结果摘要：只留计数/说明/前几条错误（状态体不塞整份回填明细）。"""
    if not isinstance(backfill, dict):
        return None
    return {
        "planned": backfill.get("planned"),
        "rerun": backfill.get("rerun"),
        "still_pending": backfill.get("still_pending"),
        "disabled": bool(backfill.get("disabled")),
        "note": str(backfill.get("note") or ""),
        "errors": [str(e) for e in (backfill.get("errors") or [])[:5]],
    }


def build_run_status(
    state: str,
    *,
    request: dict[str, Any] | None = None,
    summary: dict[str, Any] | None = None,
    error: Any = None,
    note: str = "",
    started_at: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """状态快照。`running` 记 started_at；`done/error` 记 finished_at。"""
    if state not in RUN_STATES:
        raise ValueError(f"未知评估重评状态: {state}")
    req = request or {}
    stamp = _iso(now)
    errors: list[str] = []
    if error is not None:
        errors = [str(error)]
    elif summary:
        errors = [str(e) for e in (summary.get("errors") or [])[:_MAX_STATUS_ERRORS]]
    return {
        "state": state,
        "request_id": req.get("request_id"),
        "date": req.get("date"),
        "trigger": "manual",
        "requested_by": req.get("requested_by") or None,
        "tenant_id": req.get("tenant_id") or None,
        "requested_at": req.get("requested_at"),
        "started_at": started_at or (stamp if state == "running" else None),
        "finished_at": stamp if state in ("done", "error") else None,
        "total_scored": int(summary["total_scored"]) if summary and summary.get("total_scored") is not None else None,
        "total_errors": int(summary["total_errors"]) if summary and summary.get("total_errors") is not None else None,
        "errors": errors,
        "backfill": _compact_backfill((summary or {}).get("backfill")),
        "elapsed_sec": summary.get("elapsed_sec") if summary else None,
        "note": note,
    }


def read_run_status(redis: Any) -> dict[str, Any]:
    """读最近一次手动跑批状态；无记录/坏 JSON → idle（不把读失败伪装成运行中）。"""
    client = _raw(redis)
    if client is None:
        raise EvalRunUnavailable("trade Redis 不可用")
    raw = client.get(EVAL_RUN_STATUS_KEY)
    if not raw:
        return idle_status()
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return idle_status()
    if not isinstance(data, dict):
        return idle_status()
    return {**idle_status(), **data}


def write_run_status(redis: Any, status: dict[str, Any]) -> None:
    client = _raw(redis)
    if client is None:
        raise EvalRunUnavailable("trade Redis 不可用")
    client.set(
        EVAL_RUN_STATUS_KEY,
        json.dumps(status, ensure_ascii=False, default=str),
        ex=EVAL_RUN_STATUS_TTL_SECONDS,
    )


def request_run(
    redis: Any,
    *,
    requested_by: Any = "",
    tenant_id: Any = "default",
    date_str: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """写一个手动重评请求（幂等去重：已有待消费请求 → `EvalRunBusy`）。"""
    client = _raw(redis)
    if client is None:
        raise EvalRunUnavailable("trade Redis 不可用")
    req = build_run_request(
        requested_by=requested_by, tenant_id=tenant_id, date_str=date_str, now=now
    )
    queued = client.set(
        EVAL_RUN_REQUEST_KEY,
        json.dumps(req, ensure_ascii=False),
        nx=True,
        ex=EVAL_RUN_REQUEST_TTL_SECONDS,
    )
    if not queued:
        raise EvalRunBusy(read_run_status(client))
    write_run_status(
        client,
        build_run_status(
            "queued",
            request=req,
            note="已排队，等待 trade worker 消费",
            now=now,
        ),
    )
    return req


def consume_run_request(redis: Any) -> dict[str, Any] | None:
    """原子取走待消费请求；没有 → None。坏 JSON 也取走（避免毒请求卡住队列）。"""
    client = _raw(redis)
    if client is None:
        raise EvalRunUnavailable("trade Redis 不可用")
    if hasattr(client, "getdel"):
        raw = client.getdel(EVAL_RUN_REQUEST_KEY)
    else:  # 老 redis-py：get+del（单 worker 消费，可接受）
        raw = client.get(EVAL_RUN_REQUEST_KEY)
        if raw is not None:
            client.delete(EVAL_RUN_REQUEST_KEY)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def acquire_run_lock(
    redis: Any, token: str, *, ttl: int = EVAL_RUN_LOCK_TTL_SECONDS
) -> bool:
    client = _raw(redis)
    if client is None:
        raise EvalRunUnavailable("trade Redis 不可用")
    return bool(client.set(EVAL_RUN_LOCK_KEY, str(token), nx=True, ex=max(1, int(ttl))))


_RELEASE_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('del', KEYS[1]) else return 0 end"
)


def release_run_lock(redis: Any, token: str) -> bool:
    """只释放自己持有的锁（token 比较后删除），避免误删别人的锁。"""
    client = _raw(redis)
    if client is None:
        return False
    try:
        return bool(client.eval(_RELEASE_LUA, 1, EVAL_RUN_LOCK_KEY, str(token)))
    except Exception:  # noqa: BLE001 - fake/老客户端无 eval：退化为 get+比较+del
        current = client.get(EVAL_RUN_LOCK_KEY)
        if current == str(token):
            client.delete(EVAL_RUN_LOCK_KEY)
            return True
        return False


def status_is_active(status: dict[str, Any] | None) -> bool:
    return str((status or {}).get("state") or "") in _ACTIVE_STATES
