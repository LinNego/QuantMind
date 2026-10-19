"""单轮委托上限（``max_orders_per_cycle``）回归：配置必须真的截断。

2026-10-19 前该键只被读进配置字典，从未被执行——一轮算出 122–158 条腿全部出链，
镜像开着时就是逐腿真单。价值最高的断言是**接线**：上限从调度配置一路传到引擎，
只测纯函数不能防「函数写了但没人调」。

纪律：退出规则单不截断（风控动作不能被上限吞掉）；调仓腿先卖后买，截断先保卖出。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from backend.services.simulation.engine import apply_order_cap

_BACKEND = Path(__file__).resolve().parents[1]


class _Order:
    """Order 合同的最小替身（本文件只关心列表切分）。"""

    def __init__(self, symbol: str, side: str = "buy", quantity: int = 100) -> None:
        self.symbol = symbol
        self.side = side
        self.quantity = quantity


# --- 纯函数 -----------------------------------------------------------------


@pytest.mark.unit
def test_keeps_all_exit_orders_and_caps_rebalance_legs():
    orders = [_Order("EXIT1", "sell"), _Order("EXIT2", "sell")] + [
        _Order(f"S{i}", "sell" if i < 3 else "buy") for i in range(8)
    ]

    kept, dropped = apply_order_cap(orders, exit_order_count=2, max_orders=5)

    assert [o.symbol for o in kept] == ["EXIT1", "EXIT2", "S0", "S1", "S2"]
    assert dropped == 5


@pytest.mark.unit
def test_exit_orders_are_never_truncated_even_beyond_cap():
    orders = [_Order(f"EXIT{i}", "sell") for i in range(4)] + [_Order("S0", "buy")]

    kept, dropped = apply_order_cap(orders, exit_order_count=4, max_orders=2)

    assert [o.symbol for o in kept] == ["EXIT0", "EXIT1", "EXIT2", "EXIT3"]
    assert dropped == 1


@pytest.mark.unit
def test_sell_legs_survive_before_buy_legs():
    orders = [_Order("SELL1", "sell"), _Order("SELL2", "sell"), _Order("BUY1", "buy")]

    kept, _ = apply_order_cap(orders, exit_order_count=0, max_orders=2)

    assert [o.symbol for o in kept] == ["SELL1", "SELL2"]


@pytest.mark.unit
@pytest.mark.parametrize("max_orders", [None, 0, -1])
def test_no_cap_passes_everything_through(max_orders):
    orders = [_Order("A", "buy"), _Order("B", "buy")]

    kept, dropped = apply_order_cap(orders, exit_order_count=0, max_orders=max_orders)

    assert len(kept) == 2 and dropped == 0
    assert kept is not orders  # 返回副本，调用方改列表不污染入参


# --- 接线：run_cycle 真的执行上限 -------------------------------------------


@pytest.mark.asyncio
async def test_run_cycle_caps_executed_orders(monkeypatch):
    from backend.services.simulation.engine import SimulationEngine
    from backend.services.simulation.services.rebalance_calculator import StrategyConfig
    from backend.services.simulation.services.signal_loader import SignalScore

    symbols = ["600036", "000001", "600519"]
    signals = [
        SignalScore(
            symbol=s,
            score=0.9 - i * 0.1,
            trade_date=date(2026, 10, 19),
            run_id="r",
            tenant_id="default",
            user_id="1",
        )
        for i, s in enumerate(symbols)
    ]

    class _Loader:
        async def load_latest_signals(self, **kw):  # noqa: ARG002
            return list(signals)

    class _Bar:
        close = 10.0
        limit_up = 11.0
        limit_down = 9.0
        suspended = False
        pre_close = 10.0

    engine = SimulationEngine(loader=_Loader())

    async def _cfg(db, strategy_id, user_id, params_override=None, market=None):  # noqa: ARG001
        return StrategyConfig(topk=3, lot_size=100)

    async def _acct(user_id, tenant_id="default", market="CN"):  # noqa: ARG001
        return {"cash": 100000.0, "total_asset": 100000.0, "positions": {}}

    async def _bars(symbols, as_of=None, market=None):  # noqa: ARG001
        return {s: _Bar() for s in symbols}

    async def _no_inflight(db, tenant_id, user_id):  # noqa: ARG001
        return []

    async def _none(a, b):
        return None

    monkeypatch.setattr(engine, "_load_strategy_config", _cfg)
    monkeypatch.setattr(engine.account_manager, "get_account", _acct)
    monkeypatch.setattr(engine, "_load_bars", _bars)
    monkeypatch.setattr(engine, "_load_inflight_orders", _no_inflight)
    monkeypatch.setattr(engine, "_load_exit_ruleset", _none)
    monkeypatch.setattr(engine, "_apply_risk_buy_locks", lambda orders, **kw: orders)

    report = await engine.run_cycle(
        tenant_id="default",
        user_id="1",
        strategy_id="99",
        dry_run=True,
        max_orders=2,
    )

    assert len(report.planned_orders) == 2
    assert report.order_count == 2
    assert report.capped_order_count == 1


# --- 接线：调度侧把配置送进引擎（预演同口径） --------------------------------


@pytest.mark.unit
def test_scheduler_passes_cap_to_engine_and_preview():
    src = (
        _BACKEND / "services/simulation/services/simulation_hosted_scheduler.py"
    ).read_text(encoding="utf-8")
    exec_block = src.split("async def run_simulation_cycle_for_active")[1].split(
        "async def preview_simulation_plan_for_active"
    )[0]
    preview_block = src.split("async def preview_simulation_plan_for_active")[1].split(
        "async def execute_simulation_plan_for_active"
    )[0]
    assert 'max_orders=cfg.get("max_orders_per_cycle")' in exec_block
    assert 'max_orders=cfg.get("max_orders_per_cycle")' in preview_block
    assert '"capped_count": getattr(report, "capped_order_count", 0)' in src
