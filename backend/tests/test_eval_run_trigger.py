"""评估中心手动重评 Redis 契约（`shared/eval_trigger.py`）。

盯四件事：
1. **日期校验**：只认 `YYYY-MM-DD`，未来日拒绝（快照还不存在）；
2. **去重**：已有待消费请求 → `EvalRunBusy`，不覆盖、不排队两条；
3. **原子消费**：`GETDEL` 取走一次，第二次为 None；
4. **锁**：只有持锁 token 能释放，防止误删别人的锁。
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from backend.shared import eval_trigger as t


class FakeRedis:
    """最小 Redis 语义：set(nx/ex)/get/getdel/delete/eval。"""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttl: dict[str, int | None] = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[str(key)] = str(value)
        self.ttl[str(key)] = ex
        return True

    def get(self, key):
        return self.store.get(str(key))

    def getdel(self, key):
        return self.store.pop(str(key), None)

    def delete(self, key):
        self.store.pop(str(key), None)
        return 1

    def eval(self, script, numkeys, key, arg):
        if self.store.get(str(key)) == str(arg):
            self.store.pop(str(key), None)
            return 1
        return 0


@pytest.mark.unit
def test_validate_run_date_accepts_today_and_past_only():
    today = date(2026, 10, 21)
    assert t.validate_run_date(None, today=today) is None
    assert t.validate_run_date("", today=today) is None
    assert t.validate_run_date("2026-10-21", today=today) == "2026-10-21"
    assert t.validate_run_date("2026-10-20", today=today) == "2026-10-20"
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        t.validate_run_date("20261021", today=today)
    with pytest.raises(ValueError, match="不能晚于今天"):
        t.validate_run_date("2026-10-22", today=today)


@pytest.mark.unit
def test_market_today_uses_plus_eight_boundary():
    # UTC 2026-10-20 17:30 = 北京时间 2026-10-21 01:30
    now = datetime(2026, 10, 20, 17, 30, tzinfo=timezone.utc)
    assert t.market_today(now) == date(2026, 10, 21)


@pytest.mark.unit
def test_request_run_queues_and_rejects_second_request():
    redis = FakeRedis()
    req = t.request_run(
        redis,
        requested_by=7,
        tenant_id="default",
        date_str="2026-10-21",
        now=datetime(2026, 10, 21, 6, 0, tzinfo=timezone.utc),
    )
    assert req["requested_by"] == "7" and req["date"] == "2026-10-21"
    status = t.read_run_status(redis)
    assert status["state"] == "queued"
    assert status["request_id"] == req["request_id"]
    assert t.EVAL_RUN_REQUEST_KEY in redis.store
    assert redis.ttl[t.EVAL_RUN_REQUEST_KEY] == t.EVAL_RUN_REQUEST_TTL_SECONDS

    with pytest.raises(t.EvalRunBusy) as exc:
        t.request_run(redis, requested_by=8)
    assert exc.value.status["state"] == "queued"
    # 没被第二条覆盖
    assert t.consume_run_request(redis)["request_id"] == req["request_id"]


@pytest.mark.unit
def test_consume_run_request_is_one_shot_and_ignores_bad_json():
    redis = FakeRedis()
    redis.store[t.EVAL_RUN_REQUEST_KEY] = "not-json"
    assert t.consume_run_request(redis) is None
    assert t.EVAL_RUN_REQUEST_KEY not in redis.store

    req = t.build_run_request(requested_by=1)
    redis.store[t.EVAL_RUN_REQUEST_KEY] = __import__("json").dumps(req)
    assert t.consume_run_request(redis)["request_id"] == req["request_id"]
    assert t.consume_run_request(redis) is None


@pytest.mark.unit
def test_lock_only_release_with_own_token():
    redis = FakeRedis()
    assert t.acquire_run_lock(redis, "a") is True
    assert t.acquire_run_lock(redis, "b") is False
    assert t.release_run_lock(redis, "b") is False
    assert redis.store[t.EVAL_RUN_LOCK_KEY] == "a"
    assert t.release_run_lock(redis, "a") is True
    assert t.EVAL_RUN_LOCK_KEY not in redis.store


@pytest.mark.unit
def test_status_done_carries_counts_and_compact_backfill():
    req = t.build_run_request(requested_by=1, date_str="2026-10-21")
    started = "2026-10-21T07:01:00+00:00"
    status = t.build_run_status(
        "done",
        request=req,
        started_at=started,
        summary={
            "total_scored": 12,
            "total_errors": 1,
            "errors": ["model:x: boom", "model:y: boom"],
            "elapsed_sec": 88.5,
            "backfill": {
                "planned": 2,
                "rerun": 2,
                "still_pending": 0,
                "errors": [],
                "note": "回填 2 条",
                "huge": {"not": "carried"},
            },
        },
        now=datetime(2026, 10, 21, 7, 3, tzinfo=timezone.utc),
    )
    assert status["state"] == "done"
    assert status["started_at"] == started
    assert status["finished_at"] is not None
    assert status["total_scored"] == 12 and status["total_errors"] == 1
    assert status["errors"][0].startswith("model:x")
    assert status["backfill"] == {
        "planned": 2,
        "rerun": 2,
        "still_pending": 0,
        "disabled": False,
        "note": "回填 2 条",
        "errors": [],
    }
    assert "huge" not in status["backfill"]


@pytest.mark.unit
def test_status_error_carries_single_reason():
    status = t.build_run_status("error", request={"request_id": "r1"}, error=RuntimeError("boom"))
    assert status["state"] == "error"
    assert status["errors"] == ["boom"]
    assert status["total_scored"] is None


@pytest.mark.unit
def test_read_status_idle_when_missing_or_corrupt():
    redis = FakeRedis()
    assert t.read_run_status(redis)["state"] == "idle"
    redis.store[t.EVAL_RUN_STATUS_KEY] = "{bad"
    assert t.read_run_status(redis)["state"] == "idle"
    redis.store[t.EVAL_RUN_STATUS_KEY] = "[]"
    assert t.read_run_status(redis)["state"] == "idle"


@pytest.mark.unit
def test_raw_client_wrapper_is_supported():
    class Wrapper:
        def __init__(self):
            self.client = FakeRedis()

    wrapper = Wrapper()
    req = t.request_run(wrapper, requested_by=1)
    assert t.consume_run_request(wrapper)["request_id"] == req["request_id"]


@pytest.mark.unit
def test_unavailable_redis_raises_explicitly():
    with pytest.raises(t.EvalRunUnavailable):
        t.request_run(None)
    with pytest.raises(t.EvalRunUnavailable):
        t.read_run_status(None)


@pytest.mark.unit
def test_status_is_active_states():
    assert t.status_is_active({"state": "queued"}) is True
    assert t.status_is_active({"state": "running"}) is True
    assert t.status_is_active({"state": "done"}) is False
    assert t.status_is_active({"state": "error"}) is False
    assert t.status_is_active(None) is False
