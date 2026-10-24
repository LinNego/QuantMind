"""大盘分析指数叠加：信号日 T 与指数 T+offset 的对齐（2026-10-24）。

背景：均分是**信号日 T** 的分数（用 T 日及之前数据算出），行情收益从 T+1 才开始；
同日期叠加会让两条曲线整体错开一天。默认 offset=1（次日可交易），T+N 模型可传 horizon。
"""

from __future__ import annotations

import pytest

from backend.services.api.routers.model_training import _align_index_points

_CLOSES = {
    "2026-09-01": 100.0,
    "2026-09-02": 101.0,
    "2026-09-03": 102.0,
    "2026-09-04": 103.0,
}


@pytest.mark.unit
def test_offset_zero_uses_same_day_close():
    points = _align_index_points(["2026-09-01", "2026-09-02"], _CLOSES, 0)

    assert points[0] == {
        "trade_date": "2026-09-01",
        "index_date": "2026-09-01",
        "close": 100.0,
    }
    assert points[1]["index_date"] == "2026-09-02"
    assert points[1]["close"] == 101.0


@pytest.mark.unit
def test_offset_one_shifts_index_to_next_trading_day():
    points = _align_index_points(["2026-09-01", "2026-09-02"], _CLOSES, 1)

    assert points[0] == {
        "trade_date": "2026-09-01",
        "index_date": "2026-09-02",
        "close": 101.0,
    }
    assert points[1]["index_date"] == "2026-09-03"


@pytest.mark.unit
def test_tail_without_next_day_is_skipped_not_guessed():
    points = _align_index_points(["2026-09-03", "2026-09-04"], _CLOSES, 1)

    assert [p["trade_date"] for p in points] == ["2026-09-03"]
    assert points[0]["index_date"] == "2026-09-04"


@pytest.mark.unit
def test_offset_beyond_window_is_skipped():
    assert _align_index_points(["2026-09-04"], _CLOSES, 1) == []


@pytest.mark.unit
def test_score_date_missing_from_index_calendar_is_skipped():
    assert _align_index_points(["2026-08-31"], _CLOSES, 0) == []
    assert _align_index_points(["2026-09-01"], {}, 1) == []
