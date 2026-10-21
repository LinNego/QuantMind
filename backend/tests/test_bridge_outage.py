"""桥掉线窗口 / 恢复补跑义务（纯逻辑 + 状态编解码 + 账户探测接线）。

钉住的行为：

1. 掉线起点只写第一次（进程重启不丢时长），恢复返回完整窗口；
2. 正常恢复的 ``recovered_at`` = 恢复探测时刻；跨重启合成时退到 ``last_down_at``；
3. 判据 = 时长 ≥30 分钟 **且** 窗口压到连续竞价时段（午休/盘后断流不算）；
4. 义务 Hash 编解码可往返、坏值不炸；
5. ``_track_outage`` 接账户探测：够格才写义务，跨重启合成也能补登。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from backend.services.live_trading.services import tdx_account_sync_task as SYNC
from backend.services.live_trading.services.bridge_health_watch import (
    VERDICT_HARD,
    VERDICT_OK,
)
from backend.services.live_trading.services.trading_session import CONTINUOUS_SESSIONS
from backend.shared import bridge_outage as BO

CST = timezone(timedelta(hours=8))


def _ts(*args: int) -> float:
    return datetime(2026, 10, 21, *args, tzinfo=CST).timestamp()


class FakeRedis:
    """原生 redis 客户端的最小替身（哈希/字符串面与 redis-py 同形）。"""

    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.store: dict[str, str] = {}
        self.expires: dict[str, int] = {}

    def hset(self, key, mapping=None, **kw):
        bucket = self.hashes.setdefault(key, {})
        bucket.update({str(k): str(v) for k, v in dict(mapping or {}, **kw).items()})
        return len(bucket)

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def set(self, key, value, ex=None):
        self.store[key] = value
        return True

    def get(self, key):
        return self.store.get(key)

    def delete(self, key):
        return 1 if self.hashes.pop(key, None) is not None else 0

    def expire(self, key, seconds):
        self.expires[key] = int(seconds)
        return True


class _Wrapper:
    def __init__(self, client: FakeRedis) -> None:
        self.client = client

    def connect(self):
        return None


# ── 掉线窗口 ─────────────────────────────────────────────────────────


@pytest.mark.unit
def test_open_window_keeps_earliest_down_since_and_recovery_returns_window():
    client = FakeRedis()
    assert BO.record_probe(client, ok=False, now=_ts(9, 35)) is None
    assert BO.record_probe(client, ok=False, now=_ts(10, 9, 30)) is None
    outage = BO.record_probe(client, ok=True, now=_ts(10, 10))
    assert outage is not None
    assert outage.down_since == pytest.approx(_ts(9, 35))
    assert outage.last_down_at == pytest.approx(_ts(10, 9, 30))
    assert outage.observed_at == pytest.approx(_ts(10, 10))
    assert outage.recovered_at == pytest.approx(_ts(10, 10))
    assert outage.down_for_s == pytest.approx(35 * 60)
    assert not outage.synthesized
    assert BO.OUTAGE_KEY not in client.hashes, "恢复后开放窗口要清掉"


@pytest.mark.unit
def test_ok_probe_without_open_window_returns_none():
    assert BO.record_probe(FakeRedis(), ok=True, now=_ts(10, 0)) is None


@pytest.mark.unit
def test_restart_synthesis_uses_last_down_at_as_recovery_floor():
    """停服期间桥回来了：恢复时刻退到 last_down_at（保守下界），时长按探测时刻计。"""
    client = FakeRedis()
    BO.record_probe(client, ok=False, now=_ts(9, 35))
    outage = BO.record_probe(client, ok=True, now=_ts(14, 0))
    assert outage is not None and outage.synthesized
    assert outage.recovered_at == pytest.approx(_ts(9, 35))
    assert outage.down_for_s == pytest.approx((14 * 60 - 9 * 60 - 35) * 60)


# ── 判据 ─────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_session_overlap_counts_continuous_sessions_only():
    assert BO.session_overlap_s(_ts(9, 35), _ts(10, 5)) == pytest.approx(30 * 60)
    assert BO.session_overlap_s(_ts(12, 0), _ts(12, 40)) == pytest.approx(0.0)
    assert BO.session_overlap_s(_ts(11, 20), _ts(12, 10)) == pytest.approx(10 * 60)


@pytest.mark.unit
def test_session_overlap_skips_weekend():
    saturday = datetime(2026, 10, 24, 10, 0, tzinfo=CST).timestamp()
    assert BO.session_overlap_s(saturday - 1800, saturday + 1800) == pytest.approx(0.0)


@pytest.mark.unit
def test_qualifies_needs_both_duration_and_session_overlap():
    def _outage(start: float, end: float) -> BO.BridgeOutage:
        return BO.BridgeOutage(
            down_since=start,
            last_down_at=end,
            observed_at=end,
            recovered_at=end,
        )

    assert not BO.qualifies(_outage(_ts(9, 35), _ts(10, 4))), "29 分钟不够"
    assert BO.qualifies(_outage(_ts(9, 35), _ts(10, 5))), "30 分钟且盘中断流"
    assert not BO.qualifies(_outage(_ts(12, 0), _ts(12, 30))), "午休断流没有决策可补"


@pytest.mark.unit
def test_continuous_session_constant_matches_trading_session():
    assert BO.CONTINUOUS_SESSIONS == CONTINUOUS_SESSIONS


# ── 义务编解码 ───────────────────────────────────────────────────────


@pytest.mark.unit
def test_marker_codec_round_trip():
    marker = BO.RecoveryMarker(
        day="20261021",
        status=BO.MARKER_PENDING,
        down_since=_ts(9, 35),
        recovered_at=_ts(10, 10),
        down_for_s=2100.0,
        observed_at=_ts(10, 10),
        synthesized=True,
        missing=("deepseek-v4-pro",),
        attempts={"deepseek-v4-pro": 2},
        last_attempt_at={"deepseek-v4-pro": 123.0},
        compensated={"glm-5.3-flash": "rnd-20261021-1011"},
        note="测试",
    )
    raw = BO.encode_marker(marker)
    assert all(isinstance(v, str) for v in raw.values())
    back = BO.decode_marker(raw, day="20261021")
    assert back == marker


@pytest.mark.unit
def test_marker_decode_tolerates_garbage():
    back = BO.decode_marker(
        {
            "status": BO.MARKER_PENDING,
            "down_since": "abc",
            "attempts": "{bad json",
            "missing": "not-json",
            "compensated": '{"a": ""}',
        },
        day="20261021",
    )
    assert back is not None
    assert back.down_since == 0.0
    assert back.attempts == {}
    assert back.missing == ()
    assert back.compensated == {}
    assert BO.decode_marker({}, day="20261021") is None


@pytest.mark.unit
def test_write_and_read_marker_round_trip():
    client = FakeRedis()
    marker = BO.build_marker(
        BO.BridgeOutage(
            down_since=_ts(9, 35),
            last_down_at=_ts(10, 5),
            observed_at=_ts(10, 10),
            recovered_at=_ts(10, 10),
        )
    )
    BO.write_marker(client, marker)
    assert client.expires[BO.RECOVERY_KEY_TPL.format(day=marker.day)] == BO.STATE_TTL_S
    assert BO.read_marker(client, marker.day) == marker


# ── 账户探测接线 ─────────────────────────────────────────────────────


@pytest.mark.unit
def test_track_outage_registers_marker_after_session_outage(monkeypatch):
    client = FakeRedis()
    monkeypatch.setattr(
        "backend.services.trade_shared.redis_client.get_redis",
        lambda: _Wrapper(client),
    )
    SYNC._track_outage(VERDICT_HARD, now=_ts(9, 35))
    SYNC._track_outage(VERDICT_OK, now=_ts(10, 10))
    marker = BO.read_marker(client, "20261021")
    assert marker is not None and marker.status == BO.MARKER_PENDING
    assert marker.down_for_s == pytest.approx(35 * 60)


@pytest.mark.unit
def test_track_outage_ignores_lunch_only_outage(monkeypatch):
    client = FakeRedis()
    monkeypatch.setattr(
        "backend.services.trade_shared.redis_client.get_redis",
        lambda: _Wrapper(client),
    )
    SYNC._track_outage(VERDICT_HARD, now=_ts(12, 0))
    SYNC._track_outage(VERDICT_OK, now=_ts(12, 40))
    assert BO.read_marker(client, "20261021") is None


@pytest.mark.unit
def test_track_outage_synthesizes_after_restart(monkeypatch):
    """服务停到桥已恢复：第一次成功探测也要能按 last_down_at 补登义务。"""
    client = FakeRedis()
    monkeypatch.setattr(
        "backend.services.trade_shared.redis_client.get_redis",
        lambda: _Wrapper(client),
    )
    SYNC._track_outage(VERDICT_HARD, now=_ts(9, 35))
    SYNC._track_outage(VERDICT_OK, now=_ts(14, 0))
    marker = BO.read_marker(client, "20261021")
    assert marker is not None and marker.synthesized
    assert marker.recovered_at == pytest.approx(_ts(9, 35))


@pytest.mark.unit
def test_track_outage_survives_redis_failure(monkeypatch):
    class _Broken:
        client = None

        def connect(self):
            raise RuntimeError("redis down")

    monkeypatch.setattr(
        "backend.services.trade_shared.redis_client.get_redis", lambda: _Broken()
    )
    SYNC._track_outage(VERDICT_HARD, now=_ts(9, 35))  # 不许抛


@pytest.mark.unit
def test_marker_is_json_serializable_note_only():
    """义务 Hash 里除 note 外全部结构化；note 保留人能读的一句话。"""
    marker = BO.build_marker(
        BO.BridgeOutage(
            down_since=_ts(9, 35),
            last_down_at=_ts(10, 5),
            observed_at=_ts(10, 10),
            recovered_at=_ts(10, 10),
        )
    )
    raw = BO.encode_marker(marker)
    assert json.loads(raw["missing"]) == []
    assert "已恢复" in raw["note"]
