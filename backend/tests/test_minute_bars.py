"""实时分钟 K 线纯函数口径测试（``backend/shared/minute_bars.py``）。

覆盖：会话边界归并、午休丢弃、累计量差分、跨日/跨源基线重置、会话对齐聚合。
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from backend.shared.minute_bars import (
    CST,
    Bar,
    MinuteBarAccumulator,
    Sample,
    aggregate_period,
    bar_bucket,
    bar_to_json,
    bars_by_source,
    index_to_epoch,
    parse_bars,
    sample_from_payload,
    session_index,
    session_seconds_between,
)


def epoch(day: str, hh: int, mm: int, ss: int = 0) -> int:
    y, m, d = (int(x) for x in day.split("-"))
    return int(datetime(y, m, d, hh, mm, ss, tzinfo=CST).timestamp())


DAY = "2026-10-20"


# ── 会话边界 ─────────────────────────────────────────────────────────


def test_bar_bucket_merges_call_auction_into_first_minute() -> None:
    # Arrange / Act
    bucket = bar_bucket(epoch(DAY, 9, 25, 30))

    # Assert
    assert bucket == epoch(DAY, 9, 30)


def test_bar_bucket_drops_lunch_samples() -> None:
    assert bar_bucket(epoch(DAY, 12, 0)) is None
    assert bar_bucket(epoch(DAY, 11, 45)) is None


def test_bar_bucket_clamps_close_auction_to_last_minute() -> None:
    assert bar_bucket(epoch(DAY, 15, 0, 5)) == epoch(DAY, 14, 59)
    assert bar_bucket(epoch(DAY, 15, 30)) == epoch(DAY, 14, 59)


def test_bar_bucket_keeps_morning_close_in_last_bar() -> None:
    assert bar_bucket(epoch(DAY, 11, 30, 10)) == epoch(DAY, 11, 29)


def test_session_index_is_session_aligned() -> None:
    assert session_index(epoch(DAY, 9, 30)) == (DAY, 0)
    assert session_index(epoch(DAY, 11, 29)) == (DAY, 119)
    assert session_index(epoch(DAY, 13, 0)) == (DAY, 120)
    assert session_index(epoch(DAY, 14, 59)) == (DAY, 239)
    assert session_index(epoch(DAY, 12, 0)) is None


def test_session_seconds_between_excludes_lunch() -> None:
    # 11:29 → 13:00 的会话内间隔应为 60s（11:29→11:30），不是 91 分钟
    assert session_seconds_between(epoch(DAY, 11, 29), epoch(DAY, 13, 0)) == 60


# ── 增量聚合 ─────────────────────────────────────────────────────────


def test_accumulator_builds_ohlcv_and_volume_deltas() -> None:
    # Arrange
    acc = MinuteBarAccumulator("SH600036")
    samples = [
        Sample(
            epoch(DAY, 9, 30, 5), 10.0, volume=1_000, amount=100.0, source="tdx_bridge"
        ),
        Sample(
            epoch(DAY, 9, 30, 20), 10.2, volume=1_300, amount=130.0, source="tdx_bridge"
        ),
        Sample(
            epoch(DAY, 9, 30, 40), 9.9, volume=1_500, amount=150.0, source="tdx_bridge"
        ),
        Sample(
            epoch(DAY, 9, 30, 55), 10.1, volume=1_800, amount=180.0, source="tdx_bridge"
        ),
    ]

    # Act
    finalized = [acc.feed(s) for s in samples]
    last = acc.flush()

    # Assert
    assert finalized == [None, None, None, None]
    assert last is not None
    assert (last.open, last.high, last.low, last.close) == (10.0, 10.2, 9.9, 10.1)
    assert last.volume == 1_800  # 首根 from_open：累计值即开盘至今
    assert last.amount == 180.0
    assert last.samples == 4
    # 首根标 from_open：覆盖从 09:30:00（含集合竞价）到最后一个样本 09:30:55
    assert last.span_s == 55
    assert last.from_open is True
    assert last.complete is True


def test_accumulator_finalizes_previous_bar_on_minute_rollover() -> None:
    # Arrange
    acc = MinuteBarAccumulator("SH600036")
    acc.feed(Sample(epoch(DAY, 9, 30, 10), 10.0, 1_000, 100.0, "tdx_bridge"))
    acc.feed(Sample(epoch(DAY, 9, 30, 50), 10.1, 1_200, 120.0, "tdx_bridge"))

    # Act
    done = acc.feed(Sample(epoch(DAY, 9, 31, 5), 10.2, 1_300, 130.0, "tdx_bridge"))

    # Assert
    assert done is not None
    assert done.ts == epoch(DAY, 9, 30)
    assert done.complete is True
    # 开盘首根 from_open：量为当日累计值，不是本分钟差分
    assert done.volume == 1_200
    assert done.from_open is True
    assert acc.current is not None and acc.current.ts == epoch(DAY, 9, 31)
    # 新 Bar 的量 = 上一根末样本（1200）→ 本样本（1300）的差分
    assert acc.current.volume == 100


def test_accumulator_marks_volume_unknown_on_source_change_without_splitting() -> None:
    # Arrange
    acc = MinuteBarAccumulator("SH600036")
    acc.feed(Sample(epoch(DAY, 9, 30, 10), 10.0, 1_000, 100.0, "tdx_bridge"))

    # Act：同分钟内换源，不切 Bar（避免同 ts 两根互相覆盖）
    done = acc.feed(Sample(epoch(DAY, 9, 30, 30), 10.1, 50_000, 500.0, "qmt_big"))

    # Assert
    assert done is None
    current = acc.current
    assert current is not None
    assert current.ts == epoch(DAY, 9, 30)
    assert current.source == "qmt_big"
    assert current.volume_unknown is True
    assert current.volume is None
    assert current.samples == 2
    assert current.complete is False


def test_accumulator_recovers_volume_after_source_change_on_next_bar() -> None:
    # Arrange
    acc = MinuteBarAccumulator("SH600036")
    acc.feed(Sample(epoch(DAY, 9, 30, 10), 10.0, 1_000, 100.0, "tdx_bridge"))
    acc.feed(Sample(epoch(DAY, 9, 30, 30), 10.0, 50_000, 500.0, "qmt_big"))

    # Act
    done = acc.feed(Sample(epoch(DAY, 9, 31, 10), 10.0, 50_300, 503.0, "qmt_big"))

    # Assert：换源那根量未知，但下一根基线已重置，差分恢复正常
    assert done is not None
    assert done.volume_unknown is True
    assert acc.current is not None
    assert acc.current.volume == 300


def test_accumulator_marks_volume_unknown_when_first_seen_midday() -> None:
    # Arrange / Act
    acc = MinuteBarAccumulator("SH600036")
    acc.feed(Sample(epoch(DAY, 10, 30, 5), 10.0, 900_000, 90_000.0, "tdx_bridge"))
    bar = acc.flush()

    # Assert
    assert bar is not None
    assert bar.from_open is False
    assert bar.volume_unknown is True
    assert bar.volume is None


def test_accumulator_resets_baseline_on_new_day() -> None:
    # Arrange
    acc = MinuteBarAccumulator("SH600036")
    acc.feed(Sample(epoch(DAY, 14, 59, 30), 10.0, 8_000, 800.0, "tdx_bridge"))
    acc.feed(Sample(epoch(DAY, 15, 0, 5), 10.1, 8_200, 820.0, "tdx_bridge"))

    # Act
    done = acc.feed(
        Sample(epoch("2026-10-21", 9, 30, 5), 10.2, 500, 50.0, "tdx_bridge")
    )

    # Assert
    assert done is not None
    assert done.ts == epoch(DAY, 14, 59)
    assert acc.current is not None
    assert acc.current.date == "2026-10-21"
    assert acc.current.from_open is True
    assert acc.current.volume == 500  # 新交易日累计值从 0 起


def test_accumulator_ignores_late_sample_from_previous_minute() -> None:
    # Arrange
    acc = MinuteBarAccumulator("SH600036")
    acc.feed(Sample(epoch(DAY, 9, 31, 10), 10.0, 1_000, 100.0, "tdx_bridge"))

    # Act
    late = acc.feed(Sample(epoch(DAY, 9, 30, 50), 99.0, 900, 90.0, "tdx_bridge"))

    # Assert
    assert late is None
    assert acc.current is not None
    assert acc.current.ts == epoch(DAY, 9, 31)
    assert acc.current.samples == 1
    assert acc.current.high == 10.0


def test_accumulator_handles_cumulative_reset_as_unknown() -> None:
    # Arrange
    acc = MinuteBarAccumulator("SH600036")
    acc.feed(Sample(epoch(DAY, 9, 30, 10), 10.0, 1_000, 100.0, "tdx_bridge"))

    # Act
    acc.feed(Sample(epoch(DAY, 9, 30, 30), 10.1, 500, 50.0, "tdx_bridge"))
    bar = acc.flush()

    # Assert
    assert bar is not None
    assert bar.volume is None
    assert bar.volume_unknown is True


def test_accumulator_keeps_zero_volume_when_source_recently_advanced() -> None:
    # Arrange：盘中首见（首根量未知）→ 10:00:55 累计前进 → 10:01:05 十秒内持平
    acc = MinuteBarAccumulator("SH600036")
    acc.feed(Sample(epoch(DAY, 10, 0, 5), 10.0, 1_000_000, 90_000.0, "tdx_bridge"))
    acc.feed(Sample(epoch(DAY, 10, 0, 55), 10.0, 1_000_100, 90_100.0, "tdx_bridge"))

    # Act：10:01 根的首个差分距上次前进仅 10s
    acc.feed(Sample(epoch(DAY, 10, 1, 5), 10.0, 1_000_100, 90_100.0, "tdx_bridge"))

    # Assert：短暂的零差分可信（这一分钟确实没成交）
    bar = acc.current
    assert bar is not None
    assert bar.volume == 0.0
    assert bar.volume_unknown is False


def test_accumulator_marks_zero_volume_unknown_when_source_flat_too_long() -> None:
    # Arrange：累计量在 10:00:55 前进后再无变化
    acc = MinuteBarAccumulator("SH600036")
    acc.feed(Sample(epoch(DAY, 10, 0, 5), 10.0, 1_000_000, 90_000.0, "tdx_bridge"))
    acc.feed(Sample(epoch(DAY, 10, 0, 55), 10.0, 1_000_100, 90_100.0, "tdx_bridge"))
    acc.feed(Sample(epoch(DAY, 10, 1, 5), 10.0, 1_000_100, 90_100.0, "tdx_bridge"))

    # Act：10:02:20 距上次前进已 85s（>60s）
    acc.feed(Sample(epoch(DAY, 10, 2, 20), 10.0, 1_000_100, 90_100.0, "tdx_bridge"))

    # Assert：源累计长时间不动时零差分可能是滞后，绝不记成 0
    bar = acc.current
    assert bar is not None
    assert bar.volume is None
    assert bar.volume_unknown is True


def test_bars_by_source_never_diffs_across_sources() -> None:
    # Arrange：同一分钟两个源，累计量口径不同（桥滞后）
    samples = [
        Sample(epoch(DAY, 9, 30, 5), 10.0, 1_000, 100.0, "tdx_bridge"),
        Sample(epoch(DAY, 9, 30, 10), 10.1, 500, 50.0, "tdx_aidata_sub"),
        Sample(epoch(DAY, 9, 30, 20), 10.2, 600, 60.0, "tdx_aidata_sub"),
        Sample(epoch(DAY, 9, 30, 40), 10.3, 700, 70.0, "tdx_aidata_sub"),
        Sample(epoch(DAY, 9, 31, 5), 10.4, 1_000, 100.0, "tdx_bridge"),
        Sample(epoch(DAY, 9, 31, 5), 10.5, 800, 80.0, "tdx_aidata_sub"),
    ]

    # Act
    bars = bars_by_source(samples, symbol="SH600036")

    # Assert：09:30 订阅样本更密 → 选订阅；09:31 两个源各 1 样本，订阅 span 更小
    assert [b.time for b in bars] == ["09:30", "09:31"]
    assert [b.source for b in bars] == ["tdx_aidata_sub", "tdx_aidata_sub"]
    assert bars[0].samples == 3
    assert bars[0].volume == 700  # 当日首根 from_open：累计值（含竞价），不是跨源差分
    assert bars[1].volume == 100  # 订阅源内差分 700→800


def test_bars_by_source_prefers_known_volume_over_unknown() -> None:
    # Arrange：订阅在盘中首次出现（量未知）；桥有完整累计序列
    samples = [
        Sample(epoch(DAY, 10, 29, 50), 10.0, 900, 90.0, "tdx_bridge"),
        Sample(epoch(DAY, 10, 30, 10), 10.1, 920, 92.0, "tdx_bridge"),
        Sample(epoch(DAY, 10, 31, 10), 10.2, 940, 94.0, "tdx_bridge"),
        Sample(epoch(DAY, 10, 30, 30), 10.1, 5_000, 500.0, "tdx_aidata_sub"),
    ]

    # Act
    bars = bars_by_source(samples, symbol="SH600036")

    # Assert：10:30 桥的量已知，订阅首根量未知 → 选桥
    by_time = {b.time: b for b in bars}
    assert by_time["10:30"].source == "tdx_bridge"
    assert by_time["10:30"].volume == 20


# ── 多周期聚合 ───────────────────────────────────────────────────────


def _bar(ts: int, price: float, volume: float = 10.0) -> Bar:
    return Bar(
        symbol="SH600036",
        ts=ts,
        open=price,
        high=price + 0.1,
        low=price - 0.1,
        close=price,
        volume=volume,
        amount=volume * 10,
        samples=1,
        span_s=60,
        source="tdx_bridge",
        complete=True,
        from_open=False,
        volume_unknown=False,
    )


def test_aggregate_period_is_session_aligned_across_lunch() -> None:
    # Arrange：11:25–11:29 与 13:00–13:04 各 5 根
    bars = [_bar(epoch(DAY, 11, 25 + i), 10 + i * 0.01) for i in range(5)] + [
        _bar(epoch(DAY, 13, i), 11 + i * 0.01) for i in range(5)
    ]

    # Act
    out = aggregate_period(bars, "5m")

    # Assert
    assert [b.time for b in out] == ["11:25", "13:00"]
    assert [b.volume for b in out] == [50.0, 50.0]
    assert all(b.complete for b in out)


def test_aggregate_period_ohlc_uses_first_last_max_min() -> None:
    # Arrange
    bars = [
        _bar(epoch(DAY, 9, 30), 10.0, 1),
        _bar(epoch(DAY, 9, 31), 10.5, 2),
        _bar(epoch(DAY, 9, 32), 9.8, 3),
        _bar(epoch(DAY, 9, 33), 10.2, 4),
        _bar(epoch(DAY, 9, 34), 10.1, 5),
    ]

    # Act
    out = aggregate_period(bars, "5m")

    # Assert
    assert len(out) == 1
    assert out[0].open == 10.0
    assert out[0].close == 10.1
    assert out[0].high == pytest.approx(10.6)
    assert out[0].low == pytest.approx(9.7)
    assert out[0].volume == 15.0


def test_aggregate_period_marks_missing_minutes_incomplete_but_keeps_volume() -> None:
    # Arrange：5m 桶只有 3 根
    bars = [
        _bar(epoch(DAY, 9, 30), 10.0, 1),
        _bar(epoch(DAY, 9, 31), 10.1, 2),
        _bar(epoch(DAY, 9, 33), 10.2, 3),
    ]

    # Act
    out = aggregate_period(bars, "5m")

    # Assert
    assert out[0].complete is False
    assert out[0].volume == 6.0
    assert out[0].samples == 3


def test_aggregate_period_rejects_unknown_period() -> None:
    with pytest.raises(ValueError):
        aggregate_period([], "2m")


def test_aggregate_period_accepts_1h_alias_for_60m() -> None:
    # Arrange：09:30-10:29 满 60 分钟；14:00-14:29 只有 30 分钟
    bars = [_bar(epoch(DAY, 9, 30) + i * 60, 10.0) for i in range(60)] + [
        _bar(epoch(DAY, 14, 0) + i * 60, 10.0) for i in range(30)
    ]

    # Act
    out = aggregate_period(bars, "1h")

    # Assert：60m 桶按会话分钟对齐（09:30、10:30、13:00、14:00），不是整点对齐
    assert [b.time for b in out] == ["09:30", "14:00"]
    assert out[0].complete is True
    assert out[1].complete is False  # 只到 14:29，缺后 30 分钟


def test_index_to_epoch_roundtrip() -> None:
    for idx in (0, 119, 120, 239):
        assert session_index(index_to_epoch(DAY, idx)) == (DAY, idx)


# ── 序列化/解析 ──────────────────────────────────────────────────────


def test_bar_json_roundtrip_keeps_contract_fields() -> None:
    bar = _bar(epoch(DAY, 9, 30), 10.0)
    parsed = parse_bars([bar_to_json(bar)])

    assert len(parsed) == 1
    assert parsed[0].to_dict() == bar.to_dict()
    assert json.loads(bar_to_json(bar))["time"] == "09:30"


def test_parse_bars_skips_bad_members() -> None:
    assert parse_bars(["not-json", json.dumps({"symbol": "X"})]) == []


def test_sample_from_payload_requires_positive_price() -> None:
    base = {"timestamp": epoch(DAY, 9, 30), "price": 0, "volume": 1}
    assert sample_from_payload(base) is None
    assert sample_from_payload({**base, "price": "10.5"}) == Sample(
        ts=epoch(DAY, 9, 30), price=10.5, volume=1.0, amount=None, source=""
    )
