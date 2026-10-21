"""桥恢复补跑（消费端）行为测试。

钉住五组行为：

1. **证据销账**：恢复后已 ``ok`` 跑过的家不重复补跑；
2. **只补漏的家**：名册里缺证据的家逐个派发，schema=intraday；
3. **时段闸**：午休等待、收盘后作废（盘外补跑会挂次日单）；
4. **失败自愈**：退避期内不重试、每家至多 3 次、超限置 gave_up；
5. **绝不抛**：名册坏掉不许拖累轮次循环。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from backend.services.trade.services import decision_round_bridge_recovery as REC
from backend.services.trade.services.decision_round_core import (
    LAST_KEY,
    STATUS_OK,
    AgentRun,
    RoundDeps,
    RoundResult,
)
from backend.shared.bridge_outage import (
    MARKER_DONE,
    MARKER_EXPIRED,
    MARKER_GAVE_UP,
    MARKER_PENDING,
    RECOVERY_SETTLE_S,
    RecoveryMarker,
    write_marker,
)
from backend.shared.decision.contract import SCHEMA_INTRADAY

CST = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 21, 10, 0, tzinfo=CST)  # 周三盘中
AGENTS = ("deepseek-v4-flash", "deepseek-v4-pro", "glm-5.3-flash")


class FakeNative:
    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.store: dict[str, str] = {}

    def hset(self, key, mapping=None, **kw):
        bucket = self.hashes.setdefault(key, {})
        bucket.update({str(k): str(v) for k, v in dict(mapping or {}, **kw).items()})
        return len(bucket)

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ex=None):
        self.store[key] = value
        return True

    def expire(self, key, seconds):
        return True

    def close(self):
        return None


def _evidence(client: FakeNative, agent: str, *, at: datetime, status: str) -> None:
    key = f"{LAST_KEY}:{agent}" if agent else LAST_KEY
    client.set(key, json.dumps({"ts": at.isoformat(), "status": status}))


def _deps(
    agents: tuple[str, ...] = AGENTS,
    *,
    continuous: bool = True,
    trading_day: bool = True,
    roster_error: bool = False,
) -> RoundDeps:
    async def _none_async(*args, **kwargs):
        return None

    async def _empty_tuple(*args, **kwargs):
        return {}, {}

    def _roster():
        if roster_error:
            raise RuntimeError("roster 配置写错")
        return tuple(AgentRun(agent=a, load_llm=lambda: None) for a in agents)

    return RoundDeps(
        account_user=lambda: "10000001",
        load_positions=_empty_tuple,
        load_account=_none_async,
        load_agent_ledger=_none_async,
        load_pool=lambda day: None,
        load_excluded=lambda: None,
        quote_client=lambda: None,
        read_snaps=lambda client, codes: {},
        load_tier=lambda: None,
        load_llm=lambda: None,
        open_db=lambda: None,
        run_exec=_none_async,
        write_watch=lambda agent, plan: None,
        write_ledger=_none_async,
        is_trading_day=lambda day: _ready(trading_day),
        is_trading_time=lambda now: continuous,
        is_continuous_trading=lambda now: continuous,
        real_enabled=lambda: False,
        now=lambda: NOW,
        roster=_roster,
    )


async def _ready(value: bool) -> bool:
    return value


def _marker(*, recovered_at: datetime, **kw) -> RecoveryMarker:
    base = {
        "day": "20261021",
        "status": MARKER_PENDING,
        "down_since": (recovered_at - timedelta(minutes=40)).timestamp(),
        "recovered_at": recovered_at.timestamp(),
        "down_for_s": 40 * 60.0,
        "observed_at": recovered_at.timestamp(),
    }
    base.update(kw)
    return RecoveryMarker(**base)


@pytest.fixture
def calls(monkeypatch):
    """替身 round_tick：记录 (agent, slot)，并写入恢复后成功的 last 证据。"""
    seen: list[tuple[str, str]] = []

    async def fake_round_tick(*, deps, native, now, slot, agent="", notify=None):
        seen.append((agent, slot.schema))
        _evidence(native, agent, at=now, status=STATUS_OK)
        return (
            RoundResult(
                status=STATUS_OK,
                day=now.date(),
                slot=slot,
                round_id=f"rnd-{now:%Y%m%d-%H%M}-{agent}",
                agent=agent,
            ),
        )

    monkeypatch.setattr(REC, "round_tick", fake_round_tick)
    return seen


@pytest.fixture
def published(monkeypatch):
    seen: list[tuple[str, str]] = []

    async def fake_publish(title, content, level):
        seen.append((title, level))

    monkeypatch.setattr(REC, "_publish", fake_publish)
    return seen


@pytest.mark.asyncio
async def test_dispatches_only_missing_agents_then_marks_done(calls, published):
    client = FakeNative()
    recovered = NOW - timedelta(minutes=5)
    marker = _marker(recovered_at=recovered)
    write_marker(client, marker)
    _evidence(client, AGENTS[0], at=NOW - timedelta(minutes=1), status=STATUS_OK)

    results = await REC.bridge_recovery_tick(deps=_deps(), native=client, now=NOW)

    assert [agent for agent, _ in calls] == list(AGENTS[1:])
    assert {schema for _, schema in calls} == {SCHEMA_INTRADAY}
    assert len(results) == 2
    final = REC.read_marker(client, "20261021")
    assert final is not None and final.status == MARKER_DONE
    assert set(final.compensated) == set(AGENTS[1:])
    assert published and published[0][1] == "success"


@pytest.mark.asyncio
async def test_no_dispatch_when_every_agent_has_post_recovery_evidence(
    calls, published
):
    client = FakeNative()
    recovered = NOW - timedelta(minutes=5)
    write_marker(client, _marker(recovered_at=recovered))
    for agent in AGENTS:
        _evidence(client, agent, at=NOW - timedelta(minutes=1), status=STATUS_OK)

    await REC.bridge_recovery_tick(deps=_deps(), native=client, now=NOW)

    assert calls == []
    final = REC.read_marker(client, "20261021")
    assert final is not None and final.status == MARKER_DONE


@pytest.mark.asyncio
async def test_failed_evidence_inside_settle_window_does_not_count(calls, published):
    """恢复前完成、或恢复后 60s 内完成的轮次不算「恢复后已分析」。"""
    client = FakeNative()
    recovered = NOW - timedelta(seconds=30)  # 还在 settle 窗口内
    write_marker(client, _marker(recovered_at=recovered))
    for agent in AGENTS:
        _evidence(client, agent, at=NOW - timedelta(seconds=10), status=STATUS_OK)

    await REC.bridge_recovery_tick(deps=_deps(), native=client, now=NOW)
    assert calls == [], "settle 未到，连证据都不该采信"
    assert REC.read_marker(client, "20261021").status == MARKER_PENDING


@pytest.mark.asyncio
async def test_waits_during_lunch_break(calls, published):
    client = FakeNative()
    write_marker(client, _marker(recovered_at=NOW - timedelta(minutes=10)))
    noon = NOW.replace(hour=12, minute=10)

    await REC.bridge_recovery_tick(
        deps=_deps(continuous=False), native=client, now=noon
    )
    assert calls == []
    assert REC.read_marker(client, "20261021").status == MARKER_PENDING


@pytest.mark.asyncio
async def test_expires_after_close(calls, published):
    client = FakeNative()
    write_marker(client, _marker(recovered_at=NOW.replace(hour=14, minute=40)))
    after_close = NOW.replace(hour=15, minute=1)

    await REC.bridge_recovery_tick(deps=_deps(), native=client, now=after_close)
    assert calls == []
    final = REC.read_marker(client, "20261021")
    assert final is not None and final.status == MARKER_EXPIRED
    assert "收盘" in final.note
    assert published and published[0][1] == "warning"


@pytest.mark.asyncio
async def test_retry_backoff_skips_recent_attempt(calls, published):
    client = FakeNative()
    marker = _marker(
        recovered_at=NOW - timedelta(minutes=10),
        attempts={AGENTS[1]: 1},
        last_attempt_at={AGENTS[1]: NOW.timestamp() - 60},
    )
    write_marker(client, marker)
    _evidence(client, AGENTS[0], at=NOW - timedelta(minutes=1), status=STATUS_OK)
    _evidence(client, AGENTS[2], at=NOW - timedelta(minutes=1), status=STATUS_OK)

    await REC.bridge_recovery_tick(deps=_deps(), native=client, now=NOW)
    assert calls == []
    assert REC.read_marker(client, "20261021").status == MARKER_PENDING


@pytest.mark.asyncio
async def test_gives_up_after_attempt_cap(calls, published):
    client = FakeNative()
    marker = _marker(
        recovered_at=NOW - timedelta(minutes=40),
        attempts=dict.fromkeys(AGENTS, 3),
    )
    write_marker(client, marker)

    await REC.bridge_recovery_tick(deps=_deps(), native=client, now=NOW)
    assert calls == []
    final = REC.read_marker(client, "20261021")
    assert final is not None and final.status == MARKER_GAVE_UP
    assert published and published[0][1] == "error"


@pytest.mark.asyncio
async def test_compensated_agent_is_never_redone(calls, published):
    client = FakeNative()
    marker = _marker(
        recovered_at=NOW - timedelta(minutes=5),
        compensated={AGENTS[0]: "rnd-20261021-0956-deepseek-v4-flash"},
    )
    write_marker(client, marker)
    for agent in AGENTS[1:]:
        _evidence(client, agent, at=NOW - timedelta(minutes=1), status=STATUS_OK)

    await REC.bridge_recovery_tick(deps=_deps(), native=client, now=NOW)
    assert calls == []
    assert REC.read_marker(client, "20261021").status == MARKER_DONE


@pytest.mark.asyncio
async def test_single_path_uses_global_last_key(calls, published):
    client = FakeNative()
    write_marker(client, _marker(recovered_at=NOW - timedelta(minutes=5)))
    _evidence(client, "", at=NOW - timedelta(minutes=1), status=STATUS_OK)

    await REC.bridge_recovery_tick(deps=_deps(agents=()), native=client, now=NOW)
    assert calls == []
    assert REC.read_marker(client, "20261021").status == MARKER_DONE


@pytest.mark.asyncio
async def test_done_marker_is_ignored(calls, published):
    client = FakeNative()
    write_marker(
        client, _marker(recovered_at=NOW - timedelta(minutes=5), status=MARKER_DONE)
    )
    await REC.bridge_recovery_tick(deps=_deps(), native=client, now=NOW)
    assert calls == []


@pytest.mark.asyncio
async def test_roster_failure_never_raises(calls, published):
    client = FakeNative()
    write_marker(client, _marker(recovered_at=NOW - timedelta(minutes=5)))
    out = await REC.bridge_recovery_tick(
        deps=_deps(roster_error=True), native=client, now=NOW
    )
    assert out == ()
    assert calls == []


@pytest.mark.asyncio
async def test_no_marker_no_work(calls, published):
    assert (
        await REC.bridge_recovery_tick(deps=_deps(), native=FakeNative(), now=NOW) == ()
    )
    assert calls == []


@pytest.mark.unit
def test_settle_constant_is_one_minute():
    """恢复后先等一拍：这个数变了要有人知道。"""
    assert RECOVERY_SETTLE_S == 60.0
