"""RedisStopLossStrategy / RedisMomentumStrategy 无前视回归。

背景（报告 3183b3d5 的 1696% 收益）：两条同 bar 前视 ——
1. 止损用 **T 日收盘** 判断，却按 T 日开盘成交（deal_price=open）；
2. 动量用 **T 日收盘** 算，选出的票在 T 日开盘买入。

契约（与 master d449a0a54 同源）：
- 止损信号只能读 T-1 收盘，强制卖单在 T 日执行；
- 动量只能使用 T-1 及更早收盘，T 日 bar 必须被剔除。
"""

import os
import sys
from types import SimpleNamespace

import pandas as pd
import pytest

PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "../../")
sys.path.append(PROJECT_ROOT)

from qlib.backtest.decision import OrderDir
from qlib.contrib.strategy.signal_strategy import TopkDropoutStrategy

import backend.services.engine.qlib_app.utils.extended_strategies as es


# ---------------------------------------------------------------------------
# 动量：T 日收盘必须剔除
# ---------------------------------------------------------------------------


def _price_frame(prices: dict[str, list[float]], dates: list[str]) -> pd.DataFrame:
    tuples: list[tuple[str, pd.Timestamp]] = []
    values: list[float] = []
    for symbol, series in prices.items():
        for dt, price in zip(dates, series, strict=True):
            tuples.append((symbol, pd.Timestamp(dt)))
            values.append(price)
    index = pd.MultiIndex.from_tuples(tuples, names=["instrument", "datetime"])
    return pd.DataFrame({"$close": values}, index=index)


def _make_momentum_strategy(period: int = 2) -> es.RedisMomentumStrategy:
    strategy = object.__new__(es.RedisMomentumStrategy)
    strategy.momentum_period = period
    strategy.momentum_weight = 0.5
    strategy.backtest_id = "test"
    return strategy


def test_momentum_ignores_signal_day_close(monkeypatch):
    """T 日 A 暴涨 10 倍也不得进入动量：只能用 01-02→01-03。"""
    dates = ["2024-01-02", "2024-01-03", "2024-01-04"]
    frame = _price_frame(
        {"A": [100.0, 110.0, 1000.0], "B": [100.0, 100.0, 100.0]}, dates
    )
    monkeypatch.setattr(es, "D", SimpleNamespace(features=lambda *args, **kwargs: frame))

    strategy = _make_momentum_strategy(period=2)
    momentum = strategy._compute_momentum(["A", "B"], pd.Timestamp("2024-01-04"))

    # 只用 01-02→01-03：A +10%、B 0%，截面 z-score ±0.7071
    assert momentum["A"] == pytest.approx(0.7071, abs=1e-4)
    assert momentum["B"] == pytest.approx(-0.7071, abs=1e-4)


def test_momentum_falls_back_to_zero_when_only_signal_day_available(monkeypatch):
    """窗口内只有 T 日 bar → 拒绝计算（返回全 0），绝不拿 T 日收盘凑数。"""
    frame = _price_frame({"A": [1000.0], "B": [1000.0]}, ["2024-01-04"])
    monkeypatch.setattr(es, "D", SimpleNamespace(features=lambda *args, **kwargs: frame))

    strategy = _make_momentum_strategy(period=2)
    momentum = strategy._compute_momentum(["A", "B"], pd.Timestamp("2024-01-04"))

    assert (momentum == 0.0).all()


# ---------------------------------------------------------------------------
# 止损：T-1 收盘判断，T 日开盘执行
# ---------------------------------------------------------------------------


class FakePosition:
    def __init__(self, amounts: dict[str, float]):
        self._amounts = amounts

    def get_stock_list(self) -> list[str]:
        return list(self._amounts)

    def get_stock_amount(self, stock: str) -> float:
        return self._amounts.get(stock, 0)


class FakeCalendar:
    """模拟 qlib TradeCalendarManager：shift>0 取更早 bar，负索引回绕。"""

    def __init__(self, dates: list[str], step: int):
        self._dates = [pd.Timestamp(d) for d in dates]
        self._step = step

    def get_trade_step(self) -> int:
        return self._step

    def get_step_time(self, trade_step: int | None = None, shift: int = 0):
        index = (self._step if trade_step is None else trade_step) - shift
        return self._dates[index], self._dates[index]


class ListCalendar:
    """没有 get_step_time 的日历（走 else 分支）。"""

    def __init__(self, dates: list[str]):
        self._dates = [pd.Timestamp(d) for d in dates]

    def __getitem__(self, index: int) -> pd.Timestamp:
        return self._dates[index]


def _make_stop_loss_strategy(monkeypatch, calendar, *, cost: float = 10.0):
    position = FakePosition({"SH600000": 1000})
    # trade_calendar / trade_position 是 BaseStrategy 的只读 property，只能在类上覆盖
    monkeypatch.setattr(
        es.RedisStopLossStrategy, "trade_calendar", property(lambda self: calendar)
    )
    monkeypatch.setattr(
        es.RedisStopLossStrategy, "trade_position", property(lambda self: position)
    )

    strategy = object.__new__(es.RedisStopLossStrategy)
    strategy.holding_cost = {"SH600000": cost}
    strategy.stop_loss = -0.08
    strategy.take_profit = 0.15
    strategy.account_stop_loss = 0.0
    strategy._is_account_stopped = False
    strategy.backtest_id = "test"
    return strategy


class _FakeTradeDecision:
    """TradeDecisionWO 的最小替身（真身构造时要读 trade_calendar.get_step_time）。"""

    def __init__(self, orders, strategy):
        self._orders = list(orders)
        self.strategy = strategy

    def get_decision(self):
        return list(self._orders)


def _patch_parent_decision(monkeypatch):
    monkeypatch.setattr(es, "TradeDecisionWO", _FakeTradeDecision)
    monkeypatch.setattr(
        TopkDropoutStrategy,
        "generate_trade_decision",
        lambda self, execute_result=None: _FakeTradeDecision([], self),
    )


def _patch_features(monkeypatch, prices: dict[str, float], calls: list[tuple[str, str]]):
    def fake_features(instruments, fields, start_time=None, end_time=None, freq="day"):
        calls.append((str(start_time)[:10], str(end_time)[:10]))
        index = pd.MultiIndex.from_tuples(
            [(symbol, pd.Timestamp(end_time)) for symbol in instruments],
            names=["instrument", "datetime"],
        )
        return pd.DataFrame(
            {"$close": [prices[symbol] for symbol in instruments]}, index=index
        )

    monkeypatch.setattr(es, "D", SimpleNamespace(features=fake_features))


@pytest.mark.parametrize("price", [9.0, 11.6], ids=["stop_loss", "take_profit"])
def test_stop_loss_reads_previous_close_and_sells_at_t_open(monkeypatch, price):
    dates = ["2024-01-02", "2024-01-03", "2024-01-04"]
    calendar = FakeCalendar(dates, step=2)  # T = 2024-01-04
    strategy = _make_stop_loss_strategy(monkeypatch, calendar)
    _patch_parent_decision(monkeypatch)
    calls: list[tuple[str, str]] = []
    _patch_features(monkeypatch, {"SH600000": price}, calls)

    decision = strategy.generate_trade_decision()
    orders = decision.get_decision()

    # 信号只读 T-1（01-03），绝不读 T（01-04）收盘
    assert calls == [("2024-01-03", "2024-01-03")]
    assert len(orders) == 1
    order = orders[0]
    assert order.direction == OrderDir.SELL
    assert order.stock_id == "SH600000"
    assert order.amount == pytest.approx(1000)
    # 强制卖单在 T 日执行（deal_price=open → 开盘价成交）
    assert pd.Timestamp(order.start_time) == pd.Timestamp("2024-01-04")
    assert pd.Timestamp(order.end_time) == pd.Timestamp("2024-01-04")


def test_stop_loss_keeps_topk_decision_when_not_triggered(monkeypatch):
    dates = ["2024-01-02", "2024-01-03", "2024-01-04"]
    calendar = FakeCalendar(dates, step=2)
    strategy = _make_stop_loss_strategy(monkeypatch, calendar)
    _patch_parent_decision(monkeypatch)
    calls: list[tuple[str, str]] = []
    _patch_features(monkeypatch, {"SH600000": 10.5}, calls)  # +5% 不触发

    decision = strategy.generate_trade_decision()

    assert calls == [("2024-01-03", "2024-01-03")]
    assert decision.get_decision() == []


def test_stop_loss_skips_first_bar_where_previous_step_wraps_to_future(monkeypatch):
    """trade_step=0 时 shift=1 会回绕到未来 bar —— 必须丢弃，不能前视。"""
    dates = ["2024-01-02", "2024-01-03"]
    calendar = FakeCalendar(dates, step=0)  # shift=1 → 负索引 → 2024-01-03（未来）
    strategy = _make_stop_loss_strategy(monkeypatch, calendar)
    _patch_parent_decision(monkeypatch)
    calls: list[tuple[str, str]] = []
    _patch_features(monkeypatch, {"SH600000": 1.0}, calls)

    decision = strategy.generate_trade_decision()

    assert calls == []
    assert decision.get_decision() == []


def test_stop_loss_list_calendar_uses_previous_entry(monkeypatch):
    dates = ["2024-01-02", "2024-01-03", "2024-01-04"]
    strategy = _make_stop_loss_strategy(monkeypatch, ListCalendar(dates))
    strategy.trade_step = 2
    _patch_parent_decision(monkeypatch)
    calls: list[tuple[str, str]] = []
    _patch_features(monkeypatch, {"SH600000": 9.0}, calls)

    decision = strategy.generate_trade_decision()

    assert calls == [("2024-01-03", "2024-01-03")]
    assert [order.direction for order in decision.get_decision()] == [OrderDir.SELL]
