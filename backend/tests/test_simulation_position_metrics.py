"""模拟账户持仓未实现盈亏口径（``compute_floating_pnl``）的金样。

事故背景（2026-10-20）：模拟交易顶栏「浮动盈亏」恒显示 0.00 / 0.00%——
``/simulation/account`` 的 ``positions`` 是 ``dict[symbol, position]``，且不返回
顶层 ``floating_pnl``；前端派生只认数组，整本持仓被当成空仓。
"""

from __future__ import annotations

from backend.services.simulation.services.position_metrics import (
    compute_floating_pnl,
    position_floating_pnl,
)


def test_dict_positions_long_sum():
    positions = {
        "002552.SZ": {
            "symbol": "002552.SZ",
            "volume": 500.0,
            "last_price": 41.66,
            "cost_price": 54.61,
            "side": "long",
        },
        "300489.SZ": {
            "symbol": "300489.SZ",
            "volume": 100.0,
            "last_price": 231.53,
            "cost_price": 222.0,
            "side": "long",
        },
    }

    assert compute_floating_pnl(positions) == round(
        (41.66 - 54.61) * 500 + (231.53 - 222.0) * 100, 2
    )


def test_short_position_inverts_sign():
    positions = {
        "AAPL": {
            "volume": 100.0,
            "last_price": 90.0,
            "cost_price": 100.0,
            "side": "short",
        }
    }

    assert compute_floating_pnl(positions) == 1000.0


def test_missing_last_price_falls_back_to_market_value():
    positions = {
        "600000.SH": {
            "volume": 1000.0,
            "cost_price": 9.0,
            "market_value": 10000.0,
            "side": "long",
        }
    }

    assert compute_floating_pnl(positions) == 1000.0


def test_unpriced_rows_are_skipped():
    positions = {
        "600000.SH": {"volume": 1000.0, "last_price": 10.0, "cost_price": 9.0},
        "600001.SH": {"volume": 1000.0, "last_price": 0.0, "cost_price": 0.0},
        "600002.SH": {"volume": 0.0, "last_price": 10.0, "cost_price": 9.0},
    }

    assert compute_floating_pnl(positions) == 1000.0


def test_array_positions_supported():
    assert compute_floating_pnl(
        [
            {"volume": 1000.0, "last_price": 10.0, "cost_price": 9.0},
            {"volume": 500.0, "last_price": 8.0, "cost_price": 9.0},
        ]
    ) == 500.0


def test_empty_positions_is_zero_not_missing():
    assert compute_floating_pnl({}) == 0.0
    assert compute_floating_pnl([]) == 0.0
    assert compute_floating_pnl(None) == 0.0


def test_no_priceable_row_returns_none():
    """一行都定不了价 → None（缺失），不是 0.00（假装没有浮盈亏）。"""
    assert compute_floating_pnl(
        {"600000.SH": {"volume": 1000.0, "last_price": 0.0, "cost_price": 0.0}}
    ) is None


def test_position_floating_pnl_single_row():
    assert position_floating_pnl(
        {"volume": 200.0, "price": 4.0, "cost": 3.5, "side": "long"}
    ) == 100.0
    assert position_floating_pnl({"volume": 0.0}) is None
