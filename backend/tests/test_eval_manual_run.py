"""手动重评 worker 消费（`eval_scores_service.handle_manual_run`）。

关键纪律：
- 手动跑批**不写** `eval:scores:done:{date}`（可能发生在 16:00 前，不能吞掉 EOD）；
- 成功 → status=done（带计数/回填摘要）；失败 → status=error（带原因），worker 不退出。
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from backend.shared import eval_trigger


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[str(key)] = str(value)
        return True

    def get(self, key):
        return self.store.get(str(key))

    def delete(self, key):
        self.store.pop(str(key), None)

    def getdel(self, key):
        return self.store.pop(str(key), None)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_manual_run_done_and_does_not_touch_done_marker(monkeypatch):
    import backend.services.trade.services.eval_scores_service as svc

    calls: list[dict] = []

    async def fake_once(*, save=True, trade_date=None):
        calls.append({"save": save, "trade_date": trade_date})
        return {
            "total_scored": 9,
            "total_errors": 0,
            "errors": [],
            "elapsed_sec": 12.5,
            "backfill": {"planned": 1, "rerun": 1, "still_pending": 0, "errors": [], "note": "回填 1 条"},
        }

    monkeypatch.setattr(svc, "run_eval_scores_once", fake_once)
    redis = FakeRedis()

    summary = await svc.handle_manual_run(
        redis, {"request_id": "r1", "date": "2026-10-20"}
    )

    assert summary is not None and summary["total_scored"] == 9
    assert calls == [{"save": True, "trade_date": "2026-10-20"}]
    status = json.loads(redis.store[eval_trigger.EVAL_RUN_STATUS_KEY])
    assert status["state"] == "done"
    assert status["request_id"] == "r1"
    assert status["total_scored"] == 9 and status["total_errors"] == 0
    assert status["backfill"]["rerun"] == 1
    assert status["started_at"] and status["finished_at"]
    # 手动跑批绝不能落当日完成标记：16:00 前跑会吞掉 EOD
    assert all(not key.startswith("eval:scores:done:") for key in redis.store)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_manual_run_error_sets_status_and_returns_none(monkeypatch):
    import backend.services.trade.services.eval_scores_service as svc

    async def fake_once(*, save=True, trade_date=None):
        raise RuntimeError("model card boom")

    monkeypatch.setattr(svc, "run_eval_scores_once", fake_once)
    redis = FakeRedis()

    assert await svc.handle_manual_run(redis, {"request_id": "r2"}) is None
    status = json.loads(redis.store[eval_trigger.EVAL_RUN_STATUS_KEY])
    assert status["state"] == "error"
    assert status["request_id"] == "r2"
    assert status["errors"] == ["RuntimeError: model card boom"]
    assert status["finished_at"] is not None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_manual_run_survives_status_write_failure(monkeypatch):
    """状态写失败不能阻止跑批（跑批结果比状态面重要）。"""
    import backend.services.trade.services.eval_scores_service as svc

    async def fake_once(*, save=True, trade_date=None):
        return {"total_scored": 3, "total_errors": 0, "errors": [], "elapsed_sec": 1.0}

    class BrokenStatusRedis(FakeRedis):
        def set(self, key, value, nx=False, ex=None):
            if key == eval_trigger.EVAL_RUN_STATUS_KEY:
                raise RuntimeError("redis write boom")
            return super().set(key, value, nx=nx, ex=ex)

    monkeypatch.setattr(svc, "run_eval_scores_once", fake_once)
    summary = await svc.handle_manual_run(BrokenStatusRedis(), {"request_id": "r3"})
    assert summary is not None and summary["total_scored"] == 3


@pytest.mark.unit
def test_after_scheduled_time_boundary():
    import backend.services.trade.services.eval_scores_service as svc

    assert svc.after_scheduled_time(datetime(2026, 10, 21, 15, 59), 16, 0) is False
    assert svc.after_scheduled_time(datetime(2026, 10, 21, 16, 0), 16, 0) is True
    assert svc.after_scheduled_time(datetime(2026, 10, 21, 0, 5), 16, 0) is False
