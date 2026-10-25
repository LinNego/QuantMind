"""CnExchange 的 PIT ST 判定与 5% 参与率硬约束。

背景（master d449a0a54）：
1. 撮合层原先 ``is_st=False`` 写死 —— ST 主板 5% 保护期内，+5% 的真涨停
   被当成可交易，回测在涨停价上成交。
2. 参与率只进冲击成本（>10% 仅 warning），75% 一口吃仍全额成交。

本文件的 ST 用例注入临时稀疏表，不依赖工作区数据；真实表解析器由
``test_load_st_pit_parses_sparse_table`` 单独覆盖。
"""

import os
import sys
from datetime import date
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "../../")
sys.path.append(PROJECT_ROOT)

from qlib.backtest.decision import Order, OrderDir

from backend.services.engine.qlib_app.utils.cn_exchange import CnExchange


@pytest.fixture(autouse=True)
def _reset_st_cache(monkeypatch):
    """每个用例从干净缓存开始，避免首个用例把真实表钉进进程缓存。"""
    monkeypatch.setattr(CnExchange, "_ST_PIT_LOADED", False)
    monkeypatch.setattr(CnExchange, "_ST_PIT_LOOKUP", None)


def _inject_st_table(monkeypatch, mapping: dict[str, list[int]]):
    lookup = {symbol: np.sort(np.asarray(days, dtype=np.int64)) for symbol, days in mapping.items()}
    monkeypatch.setattr(CnExchange, "_ST_PIT_LOADED", True)
    monkeypatch.setattr(CnExchange, "_ST_PIT_LOOKUP", lookup)


# ---------------------------------------------------------------------------
# PIT ST 解析
# ---------------------------------------------------------------------------


def test_resolve_is_st_uses_pit_sparse_table(monkeypatch):
    _inject_st_table(monkeypatch, {"600000.SH": [20220506, 20220509]})

    assert CnExchange._resolve_is_st("SH600000", date(2022, 5, 6)) is True
    assert CnExchange._resolve_is_st("600000", date(2022, 5, 6)) is True
    assert CnExchange._resolve_is_st("SH600000", date(2022, 5, 5)) is False
    assert CnExchange._resolve_is_st("SZ000001", date(2022, 5, 6)) is False


def test_resolve_is_st_is_false_when_table_missing(monkeypatch):
    """表缺失 = 非 ST（乐观口径），绝不回退到 instrument_detail 静态快照。"""
    monkeypatch.setattr(CnExchange, "_ST_PIT_LOADED", True)
    monkeypatch.setattr(CnExchange, "_ST_PIT_LOOKUP", None)

    assert CnExchange._resolve_is_st("SH600000", date(2022, 5, 6)) is False


def test_load_st_pit_parses_sparse_table(monkeypatch):
    import backend.services.engine.factor_research.data as factor_data

    table = pd.DataFrame(
        {
            "symbol": ["600000.SH", "600000.SH", "000001.SZ"],
            "time": ["20220506", "20220505", "20220506"],
            "is_st": [True, True, True],
        }
    )
    monkeypatch.setattr(factor_data, "load_st_pit", lambda market="CN": table)

    lookup = CnExchange._load_st_pit()

    assert lookup is not None
    assert lookup["600000.SH"].tolist() == [20220505, 20220506]  # 升序
    assert lookup["000001.SZ"].tolist() == [20220506]


def test_limit_threshold_uses_pit_st_flag(monkeypatch):
    _inject_st_table(monkeypatch, {"600000.SH": [20220506]})

    # ST 主板 5% 保护期（<2026-07-06）内 → 0.05 - 0.005 容差
    assert CnExchange._get_limit_threshold("SH600000", date(2022, 5, 6)) == pytest.approx(0.045)
    # 同票前一日非 ST → 主板 10% - 容差
    assert CnExchange._get_limit_threshold("SH600000", date(2022, 5, 5)) == pytest.approx(0.095)


def test_limit_threshold_explicit_st_flag_overrides_pit(monkeypatch):
    _inject_st_table(monkeypatch, {"600000.SH": [20220506]})

    assert CnExchange._get_limit_threshold(
        "SH600000", date(2022, 5, 6), is_st=False
    ) == pytest.approx(0.095)
    assert CnExchange._get_limit_threshold(
        "SH600000", date(2022, 5, 5), is_st=True
    ) == pytest.approx(0.045)


class _ChangeQuote:
    def __init__(self, change: float):
        self.change = change

    def get_data(self, stock_id, start_time, end_time, field, method="ts_data_last"):
        return self.change

    def get_all_stock(self):
        return {"SH600000"}


def _make_limit_exchange(change: float) -> CnExchange:
    exchange = CnExchange.__new__(CnExchange)
    exchange.quote = _ChangeQuote(change)
    exchange.buy_price = "$close"
    exchange.sell_price = "$close"
    exchange.quote_fallback_lookback_days = 3
    exchange.backtest_id = "test"
    exchange.has_price_limits = True
    return exchange


def test_check_stock_limit_blocks_st_limit_up(monkeypatch):
    """+6% 在 ST 5% 板是涨停、在普通 10% 板不是 —— 同一涨幅结论由 ST 状态决定。"""
    _inject_st_table(monkeypatch, {"600000.SH": [20220506]})
    exchange = _make_limit_exchange(0.06)

    blocked_st_day = exchange.check_stock_limit(
        "SH600000", pd.Timestamp("2022-05-06"), pd.Timestamp("2022-05-07")
    )
    blocked_non_st_day = exchange.check_stock_limit(
        "SH600000", pd.Timestamp("2022-05-05"), pd.Timestamp("2022-05-06")
    )

    assert (blocked_st_day, blocked_non_st_day) == (True, False)


# ---------------------------------------------------------------------------
# 参与率硬约束
# ---------------------------------------------------------------------------


class _FakeQuote:
    def __init__(self, volume: float, price: float = 10.0, factor: float = 1.0):
        self.volume = volume
        self.price = price
        self.factor = factor

    def get_data(self, stock_id, start_time, end_time, field, method="ts_data_last"):
        return {
            "$close": self.price,
            "$volume": self.volume,
            "$factor": self.factor,
        }[field]

    def get_all_stock(self):
        return {"SH600000"}


def _make_exchange(
    volume: float | None,
    *,
    rate: float = 0.05,
    price: float = 10.0,
    factor: float = 1.0,
) -> CnExchange:
    exchange = CnExchange.__new__(CnExchange)
    exchange.quote = _FakeQuote(volume if volume is not None else 0.0, price, factor)
    exchange.buy_price = "$close"
    exchange.sell_price = "$close"
    exchange.commission = 0.00025
    exchange.min_commission = 5.0
    exchange.stamp_duty = 0.0005
    exchange.stamp_duty_on_sell_only = True
    exchange.transfer_fee = 0.00001
    exchange.min_transfer_fee = 0.01
    exchange.impact_cost_coefficient = 0.0
    exchange.allow_short_selling = False
    exchange.max_participation_rate = rate
    exchange.trade_unit = 100
    exchange.trade_w_adj_price = False
    exchange.buy_vol_limit = None
    exchange.sell_vol_limit = None
    exchange.quote_fallback_lookback_days = 3
    exchange.backtest_id = "test"
    exchange.has_price_limits = True
    return exchange


def _make_order(direction: OrderDir, amount: float) -> Order:
    return Order(
        stock_id="SH600000",
        amount=amount,
        direction=direction,
        start_time=pd.Timestamp("2024-01-04"),
        end_time=pd.Timestamp("2024-01-04"),
    )


def test_participation_cap_clips_buy_order():
    exchange = _make_exchange(volume=1_000_000, rate=0.05)
    order = _make_order(OrderDir.BUY, 200_000)
    position = SimpleNamespace(get_cash=lambda: 1e9)

    price, trade_val, _ = exchange._calc_trade_info_by_order(order, position, {})

    assert order.deal_amount == pytest.approx(50_000)  # 5% × 1,000,000
    assert trade_val == pytest.approx(50_000 * price)


def test_participation_cap_normalizes_by_adjustment_factor():
    """$volume 是真实股数，deal_amount 是复权股数：factor=2 时上限减半。"""
    exchange = _make_exchange(volume=1_000_000, rate=0.05, factor=2.0)
    order = _make_order(OrderDir.BUY, 200_000)  # = 400,000 真实股
    position = SimpleNamespace(get_cash=lambda: 1e9)

    exchange._calc_trade_info_by_order(order, position, {})

    # 5% × 1,000,000 真实股 = 50,000 真实股 = 25,000 复权股
    assert order.deal_amount == pytest.approx(25_000)


def test_participation_cap_rounds_down_to_trade_unit():
    exchange = _make_exchange(volume=41_000, rate=0.05)  # cap = 2,050
    order = _make_order(OrderDir.BUY, 10_000)
    position = SimpleNamespace(get_cash=lambda: 1e9)

    exchange._calc_trade_info_by_order(order, position, {})

    assert order.deal_amount == pytest.approx(2_000)  # 向下取整到 100 股


def test_participation_cap_never_upsizes_small_order():
    exchange = _make_exchange(volume=10_000_000, rate=0.05)
    order = _make_order(OrderDir.BUY, 1_000)
    position = SimpleNamespace(get_cash=lambda: 1e9)

    exchange._calc_trade_info_by_order(order, position, {})

    assert order.deal_amount == pytest.approx(1_000)


def test_participation_cap_applies_to_sell_orders():
    exchange = _make_exchange(volume=1_000_000, rate=0.05)
    order = _make_order(OrderDir.SELL, 200_000)
    position = SimpleNamespace(
        check_stock=lambda stock: True,
        get_stock_amount=lambda stock: 200_000,
        get_cash=lambda: 0.0,
    )

    exchange._calc_trade_info_by_order(order, position, {})

    assert order.deal_amount == pytest.approx(50_000)


def test_participation_cap_can_be_disabled():
    exchange = _make_exchange(volume=1_000_000, rate=0.0)
    order = _make_order(OrderDir.BUY, 200_000)
    position = SimpleNamespace(get_cash=lambda: 1e9)

    exchange._calc_trade_info_by_order(order, position, {})

    assert order.deal_amount == pytest.approx(200_000)


def test_participation_cap_ignores_zero_volume():
    exchange = _make_exchange(volume=0, rate=0.05)
    order = _make_order(OrderDir.BUY, 200_000)
    position = SimpleNamespace(get_cash=lambda: 1e9)

    exchange._calc_trade_info_by_order(order, position, {})

    assert order.deal_amount == pytest.approx(200_000)
