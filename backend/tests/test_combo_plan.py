"""纸面调仓计划（`blend_plan`）纯函数测试。

口径（与 P1 研究 10 日闸门一致）：
- 到期按「持有交易日数 ≥ hold_days」退出，不按自然日；
- 分数退出线 = mean20(S) − 0.03，mean20 只取 T 之前 20 个交易日；
- 新进每日最多 5 只、总持仓不超过 target_positions，**不降门槛凑数**；
- 数据不足/无参考价只标注，不静默跳过；
- 计划只产纸面台账，不产生任何订单字段。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from backend.services.engine.inference.blend_engine import BlendRow
from backend.services.engine.inference.blend_plan import (
    PaperPosition,
    PlanConfig,
    build_rebalance_plan,
    config_hash,
    consecutive_days_in_topk,
    positions_hash,
)

pytestmark = pytest.mark.unit


def _row(symbol: str, score: float, *, rank: int | None = None) -> BlendRow:
    return BlendRow(
        symbol=symbol,
        score=score,
        rank=rank or 1,
        coverage=8,
        dispersion=None,
        pct=score,
        research_score=score * 100,
        position="HOLD",
        contributions={},
        missing_members=(),
    )


def _trading_days(start: date, count: int) -> list[date]:
    return [start + timedelta(days=i) for i in range(count)]


def _history(
    days: list[date], symbol: str, value: float
) -> dict[date, dict[str, float]]:
    return {day: {symbol: value} for day in days}


def _config(**overrides) -> PlanConfig:
    base = {
        "capital": 1_000_000.0,
        "top_k": 5,
        "hold_days": 10,
        "entry_per_day": 5,
        "target_positions": 5,
        "cash_buffer": 0.02,
        "adv_participation": 0.01,
        "exit_delta": 0.03,
        "lookback": 5,
        "min_lookback_days": 3,
    }
    base.update(overrides)
    return PlanConfig(**base)


def _plan_days() -> tuple[list[date], date, date]:
    days = _trading_days(date(2026, 1, 5), 30)
    return days, days[-1], days[-1] + timedelta(days=1)


def test_maturity_exit_counts_trading_days_not_calendar_days():
    days, today, target = _plan_days()
    entry = PaperPosition(symbol="A", entry_date=days[-11], shares=100)
    plan = build_rebalance_plan(
        data_trade_date=today,
        target_date=target,
        ranking=[_row("A", 0.8, rank=1)],
        history=_history(days[:-1], "A", 0.9),
        trading_days=days,
        positions=[entry],
        config=_config(hold_days=10),
        prices={"A": 10.0},
        amounts={"A": 1e8},
    )
    assert plan.exits and plan.exits[0]["symbol"] == "A"
    assert plan.exits[0]["exit_reason"] == "到期"
    assert plan.exits[0]["held_days"] == 10
    assert plan.entries == []


def test_score_exit_when_below_mean20_minus_delta():
    days, today, target = _plan_days()
    entry = PaperPosition(symbol="A", entry_date=days[-3], shares=100)
    plan = build_rebalance_plan(
        data_trade_date=today,
        target_date=target,
        ranking=[_row("A", 0.50, rank=1)],
        history=_history(days[:-1], "A", 0.60),
        trading_days=days,
        positions=[entry],
        config=_config(),
        prices={"A": 10.0},
        amounts={"A": 1e8},
    )
    assert plan.exits and plan.exits[0]["exit_reason"] == "分数退出"
    assert plan.exits[0]["exit_line"] == pytest.approx(0.60 - 0.03)
    assert plan.exits[0]["held_days"] == 2


def test_missing_score_keeps_position_with_review_note():
    days, today, target = _plan_days()
    entry = PaperPosition(symbol="A", entry_date=days[-3], shares=100)
    plan = build_rebalance_plan(
        data_trade_date=today,
        target_date=target,
        ranking=[],
        history=_history(days[:-1], "A", 0.60),
        trading_days=days,
        positions=[entry],
        config=_config(),
        prices={"A": 10.0},
        amounts={"A": 1e8},
    )
    assert plan.exits == []
    assert [row["symbol"] for row in plan.holds] == ["A"]
    assert "复核" in plan.holds[0]["note"]


def test_entry_limit_five_per_day_and_no_threshold_relaxation():
    days, today, target = _plan_days()
    ranking = [_row(f"S{i}", 0.9 - i * 0.01, rank=i + 1) for i in range(12)]
    history = {day: {f"S{i}": 0.5 for i in range(12)} for day in days[:-1]}
    prices = {f"S{i}": 10.0 for i in range(12)}
    amounts = {f"S{i}": 1e8 for i in range(12)}
    plan = build_rebalance_plan(
        data_trade_date=today,
        target_date=target,
        ranking=ranking,
        history=history,
        trading_days=days,
        positions=[],
        config=_config(entry_per_day=5, target_positions=5),
        prices=prices,
        amounts=amounts,
    )
    assert len(plan.entries) == 5
    assert [row["symbol"] for row in plan.entries] == ["S0", "S1", "S2", "S3", "S4"]
    # 只取排名 ≤ top_k 的候选
    assert all(row["rank"] <= 5 for row in plan.entries)


def test_entry_requires_score_at_or_above_mean20():
    days, today, target = _plan_days()
    plan = build_rebalance_plan(
        data_trade_date=today,
        target_date=target,
        ranking=[_row("A", 0.40, rank=1), _row("B", 0.60, rank=2)],
        history={**_history(days[:-1], "A", 0.50), **_history(days[:-1], "B", 0.50)},
        trading_days=days,
        positions=[],
        config=_config(),
        prices={"A": 10.0, "B": 10.0},
        amounts={"A": 1e8, "B": 1e8},
    )
    assert [row["symbol"] for row in plan.entries] == ["B"]


def test_history_insufficient_blocks_entry_and_marks_hold():
    days, today, target = _plan_days()
    short_history = _history(days[-2:-1], "A", 0.9)
    plan = build_rebalance_plan(
        data_trade_date=today,
        target_date=target,
        ranking=[_row("A", 0.9, rank=1)],
        history=short_history,
        trading_days=days,
        positions=[PaperPosition(symbol="B", entry_date=days[-3], shares=100)],
        config=_config(),
        prices={"A": 10.0, "B": 10.0},
        amounts={"A": 1e8, "B": 1e8},
    )
    assert plan.entries == []
    assert any("历史不足" in warning for warning in plan.warnings)
    # B 无分且历史不足 → 仍留存，附复核提示
    assert [row["symbol"] for row in plan.holds] == ["B"]


def test_missing_price_skips_entry_and_records_note():
    days, today, target = _plan_days()
    plan = build_rebalance_plan(
        data_trade_date=today,
        target_date=target,
        ranking=[_row("A", 0.9, rank=1)],
        history=_history(days[:-1], "A", 0.5),
        trading_days=days,
        positions=[],
        config=_config(),
        prices={},
        amounts={"A": 1e8},
    )
    assert plan.entries == []
    assert any("无参考价" in warning for warning in plan.warnings)


def test_target_value_capped_by_adv_and_capital():
    days, today, target = _plan_days()
    plan = build_rebalance_plan(
        data_trade_date=today,
        target_date=target,
        ranking=[_row("A", 0.9, rank=1)],
        history=_history(days[:-1], "A", 0.5),
        trading_days=days,
        positions=[],
        # 本例只验「容量上限」与「资金上限」的取小，不验最低成交额门槛
        config=_config(target_positions=1, capital=1_000_000.0, min_adv_yi=0.0),
        prices={"A": 10.0},
        amounts={"A": 500_000.0},
    )
    row = plan.entries[0]
    # 资金上限 98 万，但 ADV 1% × 50 万 = 5000 元
    assert row["target_value"] == pytest.approx(5_000.0)
    assert row["shares"] == 500  # 100 股整手
    assert row["adv_yi"] == pytest.approx(0.005)


def test_missing_adv_blocks_entry_instead_of_passing_unchecked():
    days, today, target = _plan_days()
    plan = build_rebalance_plan(
        data_trade_date=today,
        target_date=target,
        ranking=[_row("A", 0.9, rank=1)],
        history=_history(days[:-1], "A", 0.5),
        trading_days=days,
        positions=[],
        config=_config(),
        prices={"A": 10.0},
        avg_amounts={},
    )
    assert plan.entries == []
    assert any("成交额缺失" in warning for warning in plan.warnings)


def test_low_adv_blocks_entry():
    days, today, target = _plan_days()
    plan = build_rebalance_plan(
        data_trade_date=today,
        target_date=target,
        ranking=[_row("A", 0.9, rank=1)],
        history=_history(days[:-1], "A", 0.5),
        trading_days=days,
        positions=[],
        config=_config(),
        prices={"A": 10.0},
        avg_amounts={"A": 5e7},  # 0.5 亿 < 1 亿门槛
    )
    assert plan.entries == []
    assert any("成交额不足" in warning for warning in plan.warnings)


def test_st_blocks_entry_and_forces_existing_position_exit():
    days, today, target = _plan_days()
    plan = build_rebalance_plan(
        data_trade_date=today,
        target_date=target,
        ranking=[_row("A", 0.9, rank=1), _row("B", 0.8, rank=2)],
        history={day: {"A": 0.5, "B": 0.5} for day in days[:-1]},
        trading_days=days,
        positions=[PaperPosition(symbol="B", entry_date=days[-3], shares=100)],
        config=_config(),
        prices={"A": 10.0, "B": 10.0},
        avg_amounts={"A": 2e8, "B": 2e8},
        risk_flags={"A": "ST", "B": "退市"},
    )
    assert plan.entries == []
    assert [row["symbol"] for row in plan.exits] == ["B"]
    assert plan.exits[0]["exit_reason"] == "风险警示(退市)"
    assert any("风险警示:A:ST" in warning for warning in plan.warnings)


def test_remaining_capital_caps_entries_and_stops_when_exhausted():
    days, today, target = _plan_days()
    held = PaperPosition(symbol="H", entry_date=days[-2], shares=90_000)
    plan = build_rebalance_plan(
        data_trade_date=today,
        target_date=target,
        ranking=[_row("A", 0.9, rank=1), _row("B", 0.8, rank=2)],
        history={day: {"A": 0.5, "B": 0.5, "H": 0.5} for day in days[:-1]},
        trading_days=days,
        positions=[held],
        config=_config(
            capital=1_000_000.0,
            cash_buffer=0.0,
            target_positions=3,
            entry_per_day=2,
            min_adv_yi=0.0,
        ),
        prices={"A": 10.0, "B": 10.0, "H": 10.0},
        avg_amounts={"A": 1e10, "B": 1e10, "H": 1e10},
    )
    # 持仓现值 90 万，只剩 10 万：首笔吃满剩余，第二笔资金用尽
    assert [row["symbol"] for row in plan.entries] == ["A"]
    assert plan.entries[0]["shares"] == 10_000
    assert plan.summary["capital_used"] == pytest.approx(1_000_000.0)
    assert plan.summary["capital_remaining"] == pytest.approx(0.0)
    assert any("资金用尽" in warning for warning in plan.warnings)


def test_positions_hash_is_order_insensitive_and_value_sensitive():
    left = [
        PaperPosition(symbol="A", entry_date=date(2026, 1, 5), shares=100),
        PaperPosition(symbol="B", entry_date=date(2026, 1, 6), shares=200),
    ]
    right = list(reversed(left))
    assert positions_hash(left) == positions_hash(right)
    changed = [left[0], PaperPosition("B", date(2026, 1, 6), 300)]
    assert positions_hash(left) != positions_hash(changed)


def test_config_hash_covers_members_weights_and_params():
    base = {
        "members": [("m1", 1.0), ("m2", 1.0)],
        "min_coverage": 8,
        "consensus_min_count": 0,
        "top_k": 50,
        "hold_days": 10,
        "entry_per_day": 5,
        "target_positions": 50,
    }
    assert config_hash(**base) == config_hash(**base)
    assert config_hash(**base) != config_hash(
        **{**base, "members": [("m1", 2.0), ("m2", 1.0)]}
    )
    assert config_hash(**base) != config_hash(**{**base, "min_coverage": 9})
    assert config_hash(**base) != config_hash(**{**base, "entry_per_day": 4})
    assert config_hash(**base) != config_hash(**{**base, "min_adv_yi": 2.0})
    assert config_hash(**base) != config_hash(**{**base, "exclude_st": False})
    assert config_hash(**base) != config_hash(**{**base, "adv_lookback_days": 10})


def test_consecutive_days_in_topk_counts_backwards_until_gap():
    days = _trading_days(date(2026, 1, 5), 6)
    top = {
        days[0]: {"A"},
        days[1]: {"A", "B"},
        days[2]: {"B"},
        days[3]: {"A"},
        days[4]: {"A"},
        days[5]: {"A"},
    }
    assert consecutive_days_in_topk(top, symbol="A", date=days[5]) == 3
    assert consecutive_days_in_topk(top, symbol="B", date=days[5]) == 0
    assert consecutive_days_in_topk(top, symbol="A", date=days[3]) == 1
