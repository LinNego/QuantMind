"""组合选股打分引擎（`blend_engine`）纯函数测试。

锁死四件事：
- 成员分位与 ``signal_contract.compute_rank_pct`` 完全同口径（含并列/缺失/单值）；
- 缺失成员重新归一，不按 0 分（否则「没出分」会被当成「看空」）；
- 覆盖门槛与共识门槛先于排序生效；
- 排序/位置词确定性且不含方向字眼。

这些断言同时是 P1 研究脚本（``combo_eval.rank_frame``）的线上对拍基准：
研究口径与线上口径只能有一份。
"""

from __future__ import annotations

import math
from dataclasses import FrozenInstanceError

import pytest

from backend.shared.signal_contract import compute_rank_pct
from backend.services.engine.inference.blend_engine import (
    BlendRow,
    combine_member_ranks,
    dispersion_band,
    member_percent_ranks,
    percent_rank_map,
    position_of,
)

pytestmark = pytest.mark.unit


# ── 分位 ─────────────────────────────────────────────────────────────────


def test_percent_rank_map_matches_compute_rank_pct_with_ties_and_non_finite():
    scores = {"A": 0.9, "B": 0.9, "C": 0.1, "D": float("nan"), "E": float("inf")}
    expected = compute_rank_pct([scores[s] for s in sorted(scores)])
    got = percent_rank_map(scores)
    for symbol, want in zip(sorted(scores), expected, strict=True):
        if want is None:
            assert symbol not in got
        else:
            assert got[symbol] == pytest.approx(want)


def test_percent_rank_map_single_symbol_is_zero_and_missing_stays_missing():
    assert percent_rank_map({"A": 0.5}) == {"A": 0.0}
    assert percent_rank_map({"A": float("nan")}) == {}
    assert percent_rank_map({}) == {}


def test_member_percent_ranks_keeps_each_member_cross_section_separate():
    ranks = member_percent_ranks(
        {
            "mA": {"X": 3.0, "Y": 1.0, "Z": 2.0},
            "mB": {"X": 1.0, "Y": 2.0},
        }
    )
    assert ranks["mA"]["X"] == pytest.approx(1.0)
    assert ranks["mA"]["Y"] == pytest.approx(0.0)
    assert ranks["mA"]["Z"] == pytest.approx(0.5)
    assert ranks["mB"]["X"] == pytest.approx(0.0)
    assert ranks["mB"]["Y"] == pytest.approx(1.0)
    assert "Z" not in ranks["mB"]


# ── 组合分 ───────────────────────────────────────────────────────────────


def _ranks():
    return {
        "A": {"X": 0.9, "Y": 0.1},
        "B": {"X": 0.7, "Z": 0.5},
        "C": {"Y": 0.3},
    }


def _combine(**kwargs):
    return combine_member_ranks(_ranks(), ["A", "B", "C"], **kwargs).rows


def test_combine_renormalizes_over_available_members():
    by_symbol = {row.symbol: row for row in _combine(min_coverage=1)}
    assert by_symbol["X"].score == pytest.approx(0.8)
    assert by_symbol["X"].coverage == 2
    assert by_symbol["Y"].score == pytest.approx(0.2)
    assert by_symbol["Y"].coverage == 2
    assert by_symbol["Z"].score == pytest.approx(0.5)
    assert by_symbol["Z"].coverage == 1
    # 缺失成员不是 0 分：X 的缺失成员 C 不计入分母
    assert by_symbol["X"].missing_members == ("C",)


def test_combine_honours_manual_weights():
    by_symbol = {
        row.symbol: row
        for row in _combine(weights={"A": 3.0, "B": 1.0}, min_coverage=1)
    }
    assert by_symbol["X"].score == pytest.approx((3 * 0.9 + 1 * 0.7) / 4)
    assert by_symbol["Y"].score == pytest.approx((3 * 0.1 + 1 * 0.3) / 4)


def test_combine_rejects_invalid_weights():
    with pytest.raises(ValueError):
        combine_member_ranks(_ranks(), ["A"], weights={"A": 0.0}, min_coverage=1)
    with pytest.raises(ValueError):
        combine_member_ranks(_ranks(), ["A"], weights={"A": math.inf}, min_coverage=1)


def test_min_coverage_excludes_before_ranking():
    rows = _combine(min_coverage=2)
    assert {row.symbol for row in rows} == {"X", "Y"}


def test_consensus_gate_requires_enough_members_above_threshold():
    rows = _combine(min_coverage=1, consensus_min_count=2, consensus_threshold=0.7)
    assert [row.symbol for row in rows] == ["X"]


def test_ranking_is_deterministic_and_positions_use_neutral_words():
    rows = _combine(min_coverage=1)
    assert [row.symbol for row in rows] == ["X", "Z", "Y"]
    assert [row.rank for row in rows] == [1, 2, 3]
    by_symbol = {row.symbol: row for row in rows}
    assert by_symbol["X"].position == "BUY"
    assert by_symbol["X"].research_score == pytest.approx(100.0)
    assert by_symbol["Z"].position == "HOLD"
    assert by_symbol["Y"].position == "SELL"
    assert {row.position for row in rows} <= {"BUY", "HOLD", "SELL"}


def test_tie_breaks_by_coverage_then_symbol():
    ranks = {
        "A": {"S1": 0.5, "S2": 0.4},
        "B": {"S1": 0.5, "S2": 0.6},
        "C": {"S2": 0.5},
    }
    rows = combine_member_ranks(ranks, ["A", "B", "C"], min_coverage=1).rows
    # S1/S2 同分 0.5，但覆盖不同：S1 覆盖 2、S2 覆盖 3 → S2 在前
    assert [row.symbol for row in rows][:2] == ["S2", "S1"]


def test_dispersion_is_sample_std_and_none_for_single_member():
    by_symbol = {row.symbol: row for row in _combine(min_coverage=1)}
    assert by_symbol["X"].dispersion == pytest.approx(
        math.sqrt(((0.9 - 0.8) ** 2 + (0.7 - 0.8) ** 2) / 1)
    )
    assert by_symbol["Z"].dispersion is None


def test_contributions_use_normalized_weights():
    by_symbol = {
        row.symbol: row
        for row in _combine(weights={"A": 3.0, "B": 1.0}, min_coverage=1)
    }
    assert by_symbol["X"].contributions["A"] == pytest.approx(0.75 * 0.9)
    assert by_symbol["X"].contributions["B"] == pytest.approx(0.25 * 0.7)
    assert sum(by_symbol["X"].contributions.values()) == pytest.approx(
        by_symbol["X"].score
    )


def test_median_method_matches_research_definition():
    ranks = {
        "A": {"X": 0.1, "Y": 0.2},
        "B": {"X": 0.5, "Y": 0.4},
        "C": {"X": 0.9, "Y": 0.6},
    }
    rows = combine_member_ranks(
        ranks, ["A", "B", "C"], method="median", min_coverage=1
    ).rows
    by_symbol = {row.symbol: row for row in rows}
    assert by_symbol["X"].score == pytest.approx(0.5)
    assert by_symbol["Y"].score == pytest.approx(0.4)
    assert by_symbol["X"].contributions == {}


def test_trimmed_method_needs_enough_members_and_drops_extremes():
    ranks = {
        "A": {"X": 0.05},
        "B": {"X": 0.20},
        "C": {"X": 0.60},
        "D": {"X": 0.80},
        "E": {"X": 0.95},
    }
    rows = combine_member_ranks(
        ranks, ["A", "B", "C", "D", "E"], method="trimmed", trim=1, min_coverage=1
    ).rows
    # 去掉 0.05/0.95 → (0.20+0.60+0.80)/3
    assert rows[0].score == pytest.approx((0.20 + 0.60 + 0.80) / 3)
    # 可用成员 < 2*trim+1 → 不参与排序
    assert (
        combine_member_ranks(
            ranks, ["A", "B", "C"], method="trimmed", trim=2, min_coverage=1
        ).rows
        == ()
    )


def test_unknown_method_is_rejected():
    with pytest.raises(ValueError):
        combine_member_ranks(_ranks(), ["A"], method="mode", min_coverage=1)


def test_stats_count_coverage_and_bands():
    result = combine_member_ranks(_ranks(), ["A", "B", "C"], min_coverage=1)
    assert result.stats["n_ranked"] == 3
    assert result.stats["n_symbols"] == 3
    assert result.stats["coverage_mean"] == pytest.approx(5 / 3)
    assert result.member_counts == {"A": 2, "B": 2, "C": 1}


# ── 展示刻度 ─────────────────────────────────────────────────────────────


def test_position_thresholds_are_inclusive_on_both_edges():
    assert position_of(0.70) == "BUY"
    assert position_of(0.6999) == "HOLD"
    assert position_of(0.30) == "SELL"
    assert position_of(0.3001) == "HOLD"
    assert position_of(None) == "HOLD"


def test_dispersion_band_boundaries():
    assert dispersion_band(0.0) == "low"
    assert dispersion_band(0.119) == "low"
    assert dispersion_band(0.12) == "medium"
    assert dispersion_band(0.219) == "medium"
    assert dispersion_band(0.22) == "high"
    assert dispersion_band(None) == "unknown"


def test_blend_row_is_immutable():
    row = BlendRow(
        symbol="SH600000",
        score=0.5,
        rank=1,
        coverage=2,
        dispersion=None,
        pct=0.5,
        research_score=50.0,
        position="HOLD",
        contributions={},
        missing_members=(),
    )
    with pytest.raises(FrozenInstanceError):
        row.score = 0.9  # type: ignore[misc]
