"""托管调度 ↔ 调仓作业表的时间口径（2026-10-19 事故回归）。

事故形态：``simulation_rebalance_jobs`` 三列是 ``timestamp without time zone``，
调度却把 aware 的 ``planned_run_at`` 传进 ``ensure_job``，asyncpg 在 INSERT 前
抛 ``DataError: can't subtract offset-naive and offset-aware datetimes``；
每次扫描都崩、作业表始终为空，用户侧看到的是「调仓计划（预演）未执行」。

本文件钉两件事：
1. 口径函数 ``_job_naive_shanghai`` 的换算（naive 上海墙钟，微秒归零）；
2. ``_process_key`` **真的**把 naive 值送进 ``ensure_job``（只测换算函数不看接线，
   下一次有人改回 aware 也照样绿——正是「验收假通过」）。
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from backend.services.simulation.services import simulation_hosted_scheduler as sched
from backend.services.simulation.services.simulation_hosted_scheduler import (
    SimulationHostedScheduler,
    _job_naive_shanghai,
)

_SH = sched._SH_TZ
_ACTIVE_KEY = "trade:active_strategy:default:10000001"


# --- 口径换算 ---------------------------------------------------------------


@pytest.mark.unit
def test_aware_utc_converted_to_naive_shanghai_wall_clock():
    # Arrange：UTC 02:35:10 = 上海 10:35:10
    value = datetime(2026, 10, 19, 2, 35, 10, tzinfo=sched.ZoneInfo("UTC"))

    # Act
    out = _job_naive_shanghai(value)

    # Assert
    assert out.tzinfo is None
    assert out == datetime(2026, 10, 19, 10, 35, 10)


@pytest.mark.unit
def test_aware_shanghai_keeps_wall_clock_and_drops_microseconds():
    value = datetime(2026, 10, 19, 9, 35, 3, 987654, tzinfo=_SH)

    out = _job_naive_shanghai(value)

    assert out.tzinfo is None
    assert out == datetime(2026, 10, 19, 9, 35, 3)


@pytest.mark.unit
def test_naive_passthrough_only_strips_microseconds():
    value = datetime(2026, 10, 19, 9, 35, 3, 5)

    out = _job_naive_shanghai(value)

    assert out.tzinfo is None
    assert out == datetime(2026, 10, 19, 9, 35, 3)


# --- 接线：_process_key 必须把 naive 值交给 ensure_job ----------------------


class _FakeRawRedis:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.store: dict[str, str] = {}

    def get(self, key):
        return sched.json.dumps(self.payload) if key == _ACTIVE_KEY else None

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True


@pytest.fixture
def captured_job(monkeypatch):
    """桩掉作业服务与引擎，只留时间口径这一跳是真的。"""
    captured: dict = {}

    async def _ensure_job(**kwargs):
        captured["ensure_job"] = kwargs

    async def _noop(*args, **kwargs):  # noqa: ARG001
        return None

    async def _cycle(**kwargs):  # noqa: ARG001
        return {
            "status": "succeeded",
            "signal_count": 1,
            "order_count": 1,
            "filled_count": 1,
            "rejected_count": 0,
        }

    from backend.services.simulation.services import rebalance_job_service

    monkeypatch.setattr(
        rebalance_job_service.SimulationRebalanceJobService, "ensure_job", _ensure_job
    )
    monkeypatch.setattr(sched.SimulationRebalanceJobService, "mark_ready", _noop)
    monkeypatch.setattr(sched.SimulationRebalanceJobService, "mark_started", _noop)
    monkeypatch.setattr(sched.SimulationRebalanceJobService, "mark_finished", _noop)
    monkeypatch.setattr(sched, "run_simulation_cycle_for_active", _cycle)
    monkeypatch.setattr(sched, "hosted_sim_log", lambda **kwargs: None)
    monkeypatch.setattr(sched, "hosted_sim_skip_once", lambda **kwargs: None)
    monkeypatch.setattr(sched, "hosted_sim_clear_skip_marker", lambda **kwargs: None)
    return captured


@pytest.mark.asyncio
async def test_process_key_passes_naive_planned_run_at(captured_job):
    """2026-10-19 10:19 之前的形态：这里传 aware，每次轮询都在 INSERT 前炸掉。"""
    payload = {
        "mode": "SIMULATION",
        "strategy_id": "2",
        "runtime_tenant_id": "default",
        "runtime_user_id": "10000001",
        "live_trade_config": {
            "market": "CN",
            "schedule_type": "interval",
            "rebalance_days": 1,
            "enabled_sessions": ["AM"],
            "sell_time": "09:30",
            "buy_time": "09:35",
            "sell_first": True,
        },
    }
    scheduler = SimulationHostedScheduler(
        SimpleNamespace(client=_FakeRawRedis(payload))
    )
    now = datetime(2026, 10, 19, 9, 35, 10, tzinfo=_SH)  # 周一 09:35 → BUY 窗口

    triggered = await scheduler._process_key(_ACTIVE_KEY, now=now)

    assert triggered is True
    planned = captured_job["ensure_job"]["planned_run_at"]
    assert planned.tzinfo is None, "作业表列是 timestamp without time zone"
    assert planned == datetime(2026, 10, 19, 9, 35, 10)


@pytest.mark.asyncio
async def test_run_once_expires_with_naive_shanghai(monkeypatch):
    captured: dict = {}

    async def _expire(*, now):
        captured["now"] = now
        return 0

    from backend.services.simulation.services import rebalance_job_service

    monkeypatch.setattr(
        rebalance_job_service.SimulationRebalanceJobService,
        "expire_outdated_jobs",
        staticmethod(_expire),
    )
    raw = _FakeRawRedis({})
    raw.scan_iter = lambda **kwargs: iter(())  # noqa: ARG005
    scheduler = SimulationHostedScheduler(SimpleNamespace(client=raw))

    await scheduler.run_once(now=datetime(2026, 10, 19, 9, 35, 10, tzinfo=_SH))

    assert captured["now"].tzinfo is None
    assert captured["now"] == datetime(2026, 10, 19, 9, 35, 10)


# --- 作业状态机：窗口内第二次 tick 不得改写已开跑的作业 ---------------------


@pytest.mark.unit
def test_skip_transition_only_covers_pre_start_statuses():
    """2026-10-22 实证：触发窗口 90s、轮询 30s。

    第一次 tick 抢到锁 → running → succeeded；第二次 tick 抢锁失败走
    ``mark_skipped``，把已完成的同名作业改写成 skipped（台账写「跳过」，实际
    已经跑完 20 单）。跳过只对 pending/ready 有意义。
    """
    from backend.services.simulation.services.rebalance_job_service import (
        skip_allowed,
    )

    assert skip_allowed("pending") is True
    assert skip_allowed("ready") is True
    assert skip_allowed("running") is False
    assert skip_allowed("succeeded") is False
    assert skip_allowed("failed") is False
    assert skip_allowed("skipped") is False
