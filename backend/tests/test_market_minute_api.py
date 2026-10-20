"""实时分钟 K 线 API 纯逻辑测试（``market_minute.py``）。

只测无 I/O 的归一化与响应体构建；Redis/桥补针由任务层集成验证。
"""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi import HTTPException

from backend.services.api.routers.market_minute import (
    _normalize_symbol,
    _parse_day,
    build_payload,
)
from backend.shared.minute_bars import CST, Sample

DAY = "2026-10-20"


def epoch(hh: int, mm: int, ss: int = 0) -> int:
    return int(datetime(2026, 10, 20, hh, mm, ss, tzinfo=CST).timestamp())


def _samples() -> list[Sample]:
    return [
        Sample(epoch(9, 30, 5), 10.0, 1_000, 100.0, "tdx_bridge"),
        Sample(epoch(9, 30, 35), 10.2, 1_200, 120.0, "tdx_bridge"),
        Sample(epoch(9, 31, 10), 10.1, 1_300, 130.0, "tdx_bridge"),
        Sample(epoch(9, 31, 40), 9.9, 1_500, 150.0, "tdx_bridge"),
    ]


def test_build_payload_returns_one_minute_bars_with_quality_meta() -> None:
    # Act
    data = build_payload(
        "SH600036", _samples(), "1m", day=DAY, now=epoch(9, 32), limit=240
    )

    # Assert
    assert data["symbol"] == "SH600036"
    assert data["date"] == DAY
    assert data["count"] == 2
    assert [b["time"] for b in data["bars"]] == ["09:30", "09:31"]
    assert all(b["complete"] for b in data["bars"])
    assert data["samples"] == 4
    assert data["sources"] == ["tdx_bridge"]
    assert data["last_sample"]["price"] == 9.9
    assert data["last_sample"]["age_s"] == 20
    assert data["quality"]["complete_bars"] == 2


def test_build_payload_marks_in_progress_bar_incomplete() -> None:
    # Arrange：now 落在 09:31 分钟内
    data = build_payload("SH600036", _samples(), "1m", day=DAY, now=epoch(9, 31, 45))

    # Assert
    assert data["bars"][0]["complete"] is True
    assert data["bars"][1]["complete"] is False
    assert data["quality"]["complete_bars"] == 1


def test_build_payload_aggregates_session_aligned_period() -> None:
    # Arrange
    samples = [
        Sample(
            epoch(9, 30 + i, 10), 10.0 + i * 0.01, 1_000 + i * 100, 100.0, "tdx_bridge"
        )
        for i in range(6)
    ]

    # Act
    data = build_payload("SH600036", samples, "5m", day=DAY, now=epoch(9, 37))

    # Assert
    assert data["period"] == "5m"
    assert [b["time"] for b in data["bars"]] == ["09:30", "09:35"]
    assert data["bars"][0]["open"] == 10.0
    assert data["bars"][0]["close"] == pytest.approx(10.04)
    assert data["bars"][0]["complete"] is True
    assert data["bars"][1]["complete"] is False


def test_build_payload_limit_applies_after_aggregation() -> None:
    # Arrange
    samples = [
        Sample(epoch(9, 30 + i, 10), 10.0, 1_000 + i * 100, 100.0, "tdx_bridge")
        for i in range(10)
    ]

    # Act
    data = build_payload("SH600036", samples, "5m", day=DAY, now=epoch(9, 45), limit=1)

    # Assert
    assert data["count"] == 1
    assert data["bars"][0]["time"] == "09:35"


def test_build_payload_empty_samples_is_empty_not_error() -> None:
    data = build_payload("SH600036", [], "1m", day=DAY, now=epoch(10, 0))

    assert data["count"] == 0
    assert data["bars"] == []
    assert data["last_sample"] is None
    assert data["quality"]["max_span_s"] == 0


def test_normalize_symbol_accepts_prefix_suffix_and_bare_code() -> None:
    assert _normalize_symbol("600036.SH") == "SH600036"
    assert _normalize_symbol("SH600036") == "SH600036"
    assert _normalize_symbol("600036") == "SH600036"
    assert _normalize_symbol("000001.SZ") == "SZ000001"


def test_normalize_symbol_rejects_non_a_share() -> None:
    with pytest.raises(HTTPException) as exc:
        _normalize_symbol("AAPL")
    assert exc.value.status_code == 400


def test_parse_day_rejects_invalid_date() -> None:
    with pytest.raises(HTTPException) as exc:
        _parse_day("2026/10/20")
    assert exc.value.status_code == 400
