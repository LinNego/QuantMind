"""止损触发时「柜台没有这只票」的第二份证据（账户快照）判据测试。

钉住四种判决与它们的失败方向：

* 新鲜快照 + 该标的缺席 ⇒ ``absent``（可信空仓，执行器安静跳过）；
* 新鲜快照 + 该标的在列 ⇒ ``held``（两条通道打架，必须告警）；
* 快照过期 / 资产全 0 / 持仓与市值自相矛盾 ⇒ ``unknown``（判不了，按异常处理）；
* 任何输入都不许抛。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from backend.services.live_trading.services import sltp_position_evidence as EV

NOW = datetime(2026, 10, 21, 2, 20, 0)  # naive UTC（快照列口径）


_FRESH = object()


def _snapshot(*, at=_FRESH, total_asset=918788.17, market_value=0.0, positions=()):
    return {
        "snapshot_at": NOW - timedelta(seconds=15) if at is _FRESH else at,
        "total_asset": total_asset,
        "market_value": market_value,
        "positions": list(positions),
    }


def _judge(snapshot, symbol="601728.SH", **kw):
    return EV.judge_snapshot(symbol=symbol, now=NOW, **snapshot, **kw)


@pytest.mark.unit
def test_fresh_flat_snapshot_proves_absent():
    evidence = _judge(_snapshot(positions=()))
    assert evidence.verdict == EV.EVIDENCE_ABSENT


@pytest.mark.unit
def test_fresh_snapshot_with_symbol_proves_held():
    evidence = _judge(_snapshot(positions=[{"symbol": "601728.SH", "volume": 1000}]))
    assert evidence.verdict == EV.EVIDENCE_HELD
    assert evidence.volume == 1000


@pytest.mark.unit
def test_prefix_symbol_in_snapshot_still_matches():
    """快照里可能是前缀式/其它口径，比较必须走同一个归一函数。"""
    evidence = _judge(_snapshot(positions=[{"symbol": "SH601728", "volume": 500}]))
    assert evidence.verdict == EV.EVIDENCE_HELD


@pytest.mark.unit
def test_zero_volume_template_row_is_not_a_position():
    """桥清仓后仍回 0 股模板行——0 股不是持仓。"""
    evidence = _judge(_snapshot(positions=[{"symbol": "601728.SH", "volume": 0}]))
    assert evidence.verdict == EV.EVIDENCE_ABSENT


@pytest.mark.unit
def test_stale_snapshot_is_unknown():
    evidence = _judge(_snapshot(at=NOW - timedelta(seconds=EV.SNAPSHOT_MAX_AGE_S + 1)))
    assert evidence.verdict == EV.EVIDENCE_UNKNOWN
    assert "过期" in evidence.reason


@pytest.mark.unit
def test_zero_asset_snapshot_is_unknown_never_absent():
    """桥账户通道掉线时资产恒 0：这类快照不能作证空仓（否则会静默吞告警）。"""
    evidence = _judge(_snapshot(total_asset=0.0, positions=()))
    assert evidence.verdict == EV.EVIDENCE_UNKNOWN


@pytest.mark.unit
def test_market_value_without_positions_is_unknown():
    """持仓列表为空但市值 > 0 = 持仓查询失灵（第三种桥假活形态）。"""
    evidence = _judge(_snapshot(market_value=45670.0, positions=()))
    assert evidence.verdict == EV.EVIDENCE_UNKNOWN


@pytest.mark.unit
def test_missing_snapshot_is_unknown():
    evidence = _judge(_snapshot(at=None))
    assert evidence.verdict == EV.EVIDENCE_UNKNOWN


@pytest.mark.unit
def test_future_snapshot_clock_skew_is_unknown():
    evidence = _judge(_snapshot(at=NOW + timedelta(minutes=10)))
    assert evidence.verdict == EV.EVIDENCE_UNKNOWN


@pytest.mark.unit
def test_judge_never_raises_on_garbage_payload():
    evidence = EV.judge_snapshot(
        symbol="601728.SH",
        snapshot_at=NOW,
        total_asset="not-a-number",
        market_value=None,
        positions=["garbage", None, {"volume": "x"}],
        now=NOW,
    )
    assert evidence.verdict == EV.EVIDENCE_UNKNOWN
