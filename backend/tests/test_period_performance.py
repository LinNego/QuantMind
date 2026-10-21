"""周期收益模拟（固定持有期滚动桶）纯函数测试。

口径（2026-10-21 用户拍板）：
- 建仓 = 评分日**收盘价**；到期 = 建仓后第 H 个交易日收盘价；停牌顺延；
- 每笔目标金额 = 初始资金 / 持仓上限，允许小数股；
- 不计手续费/滑点/印花税；期末未到期按最后有效收盘价计入总收益；
- 缺失价格绝不写 0：缺价不建仓、停牌顺延、无价估值标 stale。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from backend.services.engine.inference.period_performance import simulate_period

D0 = date(2026, 1, 5)


def _days(count: int, start: date = D0) -> list[date]:
    return [start + timedelta(days=offset) for offset in range(count)]


def _closes(symbol: str, values: list[float | None], days: list[date]) -> dict:
    return {
        symbol: {
            day: value
            for day, value in zip(days, values, strict=True)
            if value is not None
        }
    }


def _run(
    *,
    days: list[date],
    picks: dict,
    closes: dict,
    capital: float = 1000.0,
    positions: int = 1,
    entry_per_day: int = 1,
    hold_days: int = 2,
) -> dict:
    return simulate_period(
        days=days,
        picks_by_day=picks,
        closes=closes,
        capital=capital,
        positions=positions,
        entry_per_day=entry_per_day,
        hold_days=hold_days,
    )


@pytest.mark.unit
def test_fixed_hold_exits_at_hth_trading_day_close():
    days = _days(5)
    closes = _closes("SH600000", [10, 11, 12, 13, 14], days)
    result = _run(
        days=days,
        picks={days[0]: ["SH600000"]},
        closes=closes,
        positions=1,
        entry_per_day=1,
        hold_days=2,
    )

    summary = result["summary"]
    assert summary["entries"] == 1
    assert summary["closed_trades"] == 1
    assert summary["open_positions"] == 0
    assert summary["final_equity"] == pytest.approx(1200.0)
    assert summary["total_return_pct"] == pytest.approx(20.0)
    assert summary["profit_amount"] == pytest.approx(200.0)
    assert summary["win_rate_pct"] == pytest.approx(100.0)

    trade = result["trades"][0]
    assert trade["entry_date"] == days[0]
    assert trade["exit_date"] == days[2]
    assert trade["entry_price"] == pytest.approx(10.0)
    assert trade["exit_price"] == pytest.approx(12.0)
    assert trade["held_days"] == 2
    assert trade["exit_reason"] == "到期"
    assert trade["return_pct"] == pytest.approx(20.0)
    # 无费用：收益金额 = 份额 × 价差，分毫不差
    assert trade["pnl_amount"] == pytest.approx(200.0)


@pytest.mark.unit
def test_slot_budget_is_capital_over_positions_cap():
    days = _days(3)
    closes = _closes("SH600000", [10, 12, 12], days)
    result = _run(
        days=days,
        picks={days[0]: ["SH600000"]},
        closes=closes,
        capital=1000,
        positions=4,
        entry_per_day=1,
        hold_days=10,
    )
    # 每仓 1000/4 = 250 元 → 25 股；期末 25×12=300，现金 750 → 1050
    assert result["summary"]["final_equity"] == pytest.approx(1050.0)
    assert result["summary"]["total_return_pct"] == pytest.approx(5.0)
    assert result["open"][0]["shares"] == pytest.approx(25.0)


@pytest.mark.unit
def test_positions_cap_blocks_new_entries_and_warns():
    days = _days(5)
    closes = {
        **_closes("SH600000", [10, 10, 10, 13, 13], days),
        **_closes("SZ000001", [10, 10, 10, 13, 13], days),
        **_closes("SZ000002", [10, 10, 10, 13, 13], days),
    }
    result = _run(
        days=days,
        picks={days[0]: ["SH600000", "SZ000001"], days[1]: ["SZ000002"]},
        closes=closes,
        positions=1,
        entry_per_day=2,
        hold_days=3,
    )
    assert result["summary"]["entries"] == 1
    assert result["trades"][0]["symbol"] == "SH600000"
    assert result["warnings"]["positions_cap"] is True


@pytest.mark.unit
def test_skips_symbols_already_held():
    days = _days(3)
    closes = {
        **_closes("SH600000", [10, 11, 12], days),
        **_closes("SZ000001", [20, 21, 22], days),
    }
    result = _run(
        days=days,
        picks={days[0]: ["SH600000"], days[1]: ["SH600000", "SZ000001"]},
        closes=closes,
        positions=5,
        entry_per_day=2,
        hold_days=5,
    )
    assert result["summary"]["entries"] == 2
    assert result["summary"]["open_positions"] == 2
    assert {row["symbol"] for row in result["open"]} == {"SH600000", "SZ000001"}
    # 同一标的不会出现两笔在建仓
    assert len({row["symbol"] for row in result["open"]}) == len(result["open"])


@pytest.mark.unit
def test_missing_entry_price_skips_instead_of_fabricating_zero():
    days = _days(3)
    closes = _closes("SH600000", [10, 11, 12], days)
    result = _run(
        days=days,
        picks={days[0]: ["SH600000", "SZ000001"]},
        closes=closes,
        positions=5,
        entry_per_day=2,
        hold_days=2,
    )
    assert result["summary"]["entries"] == 1
    assert result["warnings"]["missing_entry_price"] == 1
    assert all(row["symbol"] == "SH600000" for row in result["open"] + result["trades"])


@pytest.mark.unit
def test_suspended_exit_is_deferred_to_next_valid_close():
    days = _days(3)
    closes = _closes("SH600000", [10, None, 12], days)
    result = _run(
        days=days,
        picks={days[0]: ["SH600000"]},
        closes=closes,
        positions=1,
        entry_per_day=1,
        hold_days=1,
    )
    trade = result["trades"][0]
    assert trade["exit_date"] == days[2]
    assert trade["exit_price"] == pytest.approx(12.0)
    assert trade["held_days"] == 2
    assert result["warnings"]["deferred_exit"] == 1
    assert result["summary"]["total_return_pct"] == pytest.approx(20.0)


@pytest.mark.unit
def test_open_position_is_marked_at_last_valid_close():
    days = _days(3)
    closes = _closes("SH600000", [10, 11, 12], days)
    result = _run(
        days=days,
        picks={days[0]: ["SH600000"]},
        closes=closes,
        positions=1,
        entry_per_day=1,
        hold_days=5,
    )
    assert result["summary"]["open_positions"] == 1
    assert result["summary"]["final_equity"] == pytest.approx(1200.0)
    assert result["summary"]["total_return_pct"] == pytest.approx(20.0)
    row = result["open"][0]
    assert row["mark_date"] == days[2]
    assert row["return_pct"] == pytest.approx(20.0)
    assert row["stale"] is False


@pytest.mark.unit
def test_monthly_breakdown_uses_previous_month_end_equity():
    days = [
        date(2026, 1, 29),
        date(2026, 1, 30),
        date(2026, 2, 2),
        date(2026, 2, 3),
    ]
    closes = _closes("SH600000", [10, 11, 12, 13], days)
    result = _run(
        days=days,
        picks={days[0]: ["SH600000"]},
        closes=closes,
        positions=1,
        entry_per_day=1,
        hold_days=1,
    )
    months = {row["month"]: row for row in result["months"]}
    assert list(months) == ["2026-01", "2026-02"]
    jan = months["2026-01"]
    assert jan["start_equity"] == pytest.approx(1000.0)
    assert jan["end_equity"] == pytest.approx(1100.0)
    assert jan["return_pct"] == pytest.approx(10.0)
    assert jan["profit_amount"] == pytest.approx(100.0)
    assert jan["entries"] == 1
    assert jan["closed_trades"] == 1
    assert jan["win_rate_pct"] == pytest.approx(100.0)
    assert jan["partial"] is True
    feb = months["2026-02"]
    assert feb["start_equity"] == pytest.approx(1100.0)
    assert feb["end_equity"] == pytest.approx(1100.0)
    assert feb["return_pct"] == pytest.approx(0.0)
    assert feb["entries"] == 0
    assert feb["partial"] is True
    assert result["summary"]["total_return_pct"] == pytest.approx(10.0)


@pytest.mark.unit
def test_max_drawdown_tracks_peak_to_trough():
    days = _days(3)
    closes = _closes("SH600000", [10, 9, 11], days)
    result = _run(
        days=days,
        picks={days[0]: ["SH600000"]},
        closes=closes,
        positions=1,
        entry_per_day=1,
        hold_days=10,
    )
    assert result["summary"]["max_drawdown_pct"] == pytest.approx(-10.0)
    assert result["summary"]["final_equity"] == pytest.approx(1100.0)


@pytest.mark.unit
def test_no_picks_is_flat_cash_not_zero_return_trap():
    days = _days(3)
    result = _run(days=days, picks={}, closes={}, positions=5)
    summary = result["summary"]
    assert summary["entries"] == 0
    assert summary["final_equity"] == pytest.approx(1000.0)
    assert summary["total_return_pct"] == pytest.approx(0.0)
    assert summary["max_drawdown_pct"] == pytest.approx(0.0)
    assert result["trades"] == []
    assert result["open"] == []
    assert [row["equity"] for row in result["daily"]] == [1000.0, 1000.0, 1000.0]
    assert result["months"][0]["return_pct"] == pytest.approx(0.0)


@pytest.mark.unit
def test_underfill_warning_when_cap_exceeds_steady_state():
    days = _days(2)
    closes = _closes("SH600000", [10, 10], days)
    result = _run(
        days=days,
        picks={days[0]: ["SH600000"]},
        closes=closes,
        positions=10,
        entry_per_day=1,
        hold_days=2,
    )
    assert result["warnings"]["underfill"] is True
    assert result["warnings"]["positions_cap"] is False


@pytest.mark.unit
def test_entry_on_last_day_is_open_with_zero_pnl():
    days = _days(2)
    closes = _closes("SH600000", [10, 10], days)
    result = _run(
        days=days,
        picks={days[1]: ["SH600000"]},
        closes=closes,
        positions=1,
        entry_per_day=1,
        hold_days=5,
    )
    assert result["summary"]["entries"] == 1
    assert result["summary"]["final_equity"] == pytest.approx(1000.0)
    # 建仓当日收盘建仓：已持有交易日按 0 计（尚无隔夜）
    assert result["open"][0]["held_days"] == 0
