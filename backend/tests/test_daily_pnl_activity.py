"""收盘收益报表的当日成交 / 清仓明细（``daily_pnl_activity``）。

钉住的口径：
  1. 成交按**北京墙钟**当日区间过滤（桥的 ``executed_at`` 是桥报 HHMM 贴日期，
     QMT 对账写 ``datetime.now()``）；
  2. 期初持仓 = 北京 00:00 前最后一帧；``snapshot_at`` 是 **naive UTC**，
     故北京 00:00 = UTC 前一日 16:00；
  3. 清仓盈亏按桥的**摊薄成本**移动平均结转，费用不计（毛口径）；
  4. 成本缺失不许显示成 0 盈亏（``realized_pnl=None``）；
  5. 桥的 0 股模板行不是持仓，不得进期初帧。

运行（容器）：python3 -m pytest backend/tests/test_daily_pnl_activity.py -q
"""

from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace

import pytest

from backend.services.trade.services import daily_pnl_activity as activity


def _trade(
    symbol: str,
    side: str,
    quantity: float,
    price: float,
    *,
    at: str = "2026-10-20 09:35:00",
    exchange: str = "tdx_bridge",
) -> dict:
    return {
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
        "price": price,
        "executed_at": datetime.fromisoformat(at),
        "exchange_name": exchange,
    }


def _opening(volume: float, cost: float) -> dict:
    return {"volume": volume, "cost_price": cost}


# ── 纯函数：聚合 / 期初帧 / 成本结转 ──────────────────────────────────


class TestAggregateDayTrades:
    def test_groups_fills_by_symbol_and_side(self):
        rows = [
            _trade("601857.SH", "sell", 100, 12.75),
            _trade("601857.SH", "sell", 100, 12.65),
            _trade("601000.SH", "buy", 300, 4.54),
        ]

        out = activity.aggregate_day_trades(rows)

        assert out["601857.SH"]["sell_qty"] == 200
        assert out["601857.SH"]["sell_amount"] == pytest.approx(2540.0)
        assert out["601857.SH"]["buy_qty"] == 0
        assert len(out["601857.SH"]["fills"]) == 2
        assert out["601000.SH"]["buy_qty"] == 300

    def test_ignores_unknown_sides_and_zero_quantity(self):
        rows = [
            _trade("601857.SH", "cancel", 100, 12.75),
            _trade("601857.SH", "buy", 0, 12.75),
            _trade("", "buy", 100, 12.75),
        ]

        assert activity.aggregate_day_trades(rows) == {}


class TestPositionsFromPayload:
    def test_drops_zero_volume_template_rows(self):
        """桥清仓后把代码留成 0 股模板行（2026-10-20 实况：9 条 0 股）。"""
        payload = {
            "positions": [
                {"symbol": "601857.SH", "volume": 200, "cost_price": 4.961},
                {"symbol": "601000.SH", "volume": 0, "cost_price": 0.0},
                {"symbol": "", "volume": 100, "cost_price": 1.0},
            ]
        }

        out = activity.positions_from_payload(payload)

        assert out == {
            "601857.SH": {"volume": 200.0, "cost_price": 4.961},
        }

    def test_accepts_plain_list_and_ignores_garbage(self):
        out = activity.positions_from_payload(
            [{"symbol": "002074.SZ", "volume": 100, "cost_price": 21.07}, "oops"]
        )

        assert list(out) == ["002074.SZ"]


class TestComputeClosedPositions:
    def test_full_liquidation_uses_diluted_cost(self):
        """601857：期初 200@4.961（桥摊薄成本），卖 200@12.70。"""
        aggregated = activity.aggregate_day_trades(
            [_trade("601857.SH", "sell", 200, 12.70)]
        )

        closed = activity.compute_closed_positions(
            {"601857.SH": _opening(200, 4.961)}, aggregated
        )

        assert len(closed) == 1
        item = closed[0]
        assert item["quantity"] == 200
        assert item["realized_pnl"] == pytest.approx(1547.8, abs=0.01)
        assert item["pnl_pct"] == pytest.approx(156.0, abs=0.01)
        assert item["cost_known"] is True

    def test_partial_sell_is_not_a_close(self):
        aggregated = activity.aggregate_day_trades(
            [_trade("601985.SH", "sell", 100, 9.45)]
        )

        assert (
            activity.compute_closed_positions(
                {"601985.SH": _opening(2100, 9.429)}, aggregated
            )
            == []
        )

    def test_same_day_round_trip_uses_buy_cost(self):
        aggregated = activity.aggregate_day_trades(
            [
                _trade("600036.SH", "buy", 100, 10.0),
                _trade("600036.SH", "sell", 100, 11.0, at="2026-10-20 10:00:00"),
            ]
        )

        closed = activity.compute_closed_positions({}, aggregated)

        assert closed[0]["realized_pnl"] == pytest.approx(100.0)
        assert closed[0]["pnl_pct"] == pytest.approx(10.0)

    def test_intraday_buy_blends_with_opening_cost(self):
        """期初 100@10，盘中买 100@20 → 均价 15；卖 200@18 ⇒ +600。"""
        aggregated = activity.aggregate_day_trades(
            [
                _trade("600000.SH", "buy", 100, 20.0),
                _trade("600000.SH", "sell", 200, 18.0, at="2026-10-20 10:00:00"),
            ]
        )

        closed = activity.compute_closed_positions(
            {"600000.SH": _opening(100, 10.0)}, aggregated
        )

        assert closed[0]["realized_pnl"] == pytest.approx(600.0)
        assert closed[0]["pnl_pct"] == pytest.approx(20.0)

    def test_missing_cost_is_not_zero_pnl(self):
        """期初成本 0（桥缺成本）→ 盈亏必须是 None，不是 +¥0.00。"""
        aggregated = activity.aggregate_day_trades(
            [_trade("688543.SH", "sell", 100, 50.0)]
        )

        closed = activity.compute_closed_positions(
            {"688543.SH": _opening(100, 0.0)}, aggregated
        )

        assert closed[0]["realized_pnl"] is None
        assert closed[0]["pnl_pct"] is None
        assert closed[0]["cost_known"] is False

    def test_oversell_beyond_holding_is_unknown_but_not_fabricated(self):
        """卖出量大于持仓（数据缺口）：已知部分照算，但整只标的不报确定盈亏。"""
        aggregated = activity.aggregate_day_trades(
            [_trade("600000.SH", "sell", 200, 12.0)]
        )

        closed = activity.compute_closed_positions(
            {"600000.SH": _opening(100, 10.0)}, aggregated
        )

        assert closed[0]["realized_pnl"] is None


class TestSummarizeDayTrades:
    def test_labels_and_sorts_by_absolute_pnl(self):
        aggregated = activity.aggregate_day_trades(
            [
                _trade("601000.SH", "sell", 1700, 4.8),
                _trade("600338.SH", "sell", 800, 11.08),
            ]
        )
        opening = {
            "601000.SH": _opening(1700, 4.541),
            "600338.SH": _opening(800, 11.834),
        }

        out = activity.summarize_day_trades(
            aggregated, opening, name_resolver=lambda s: "名称"
        )

        assert [x["symbol"] for x in out["closed"]] == ["600338.SH", "601000.SH"]
        assert out["closed"][0]["code"] == "SH600338"
        assert out["closed"][0]["name"] == "名称"
        assert out["closed_pnl_total"] == pytest.approx(-162.9, abs=0.01)
        assert out["closed_cost_unknown"] == 0

    def test_unknown_cost_counted_separately(self):
        aggregated = activity.aggregate_day_trades(
            [
                _trade("601000.SH", "sell", 100, 4.8),
                _trade("688543.SH", "sell", 100, 50.0),
            ]
        )
        opening = {
            "601000.SH": _opening(100, 4.541),
            "688543.SH": _opening(100, 0.0),
        }

        out = activity.summarize_day_trades(aggregated, opening)

        assert out["closed_cost_unknown"] == 1
        assert out["closed_pnl_total"] == pytest.approx(25.9, abs=0.01)


# ── DB 读取：当日成交 + 期初帧（8 小时换算）────────────────────────────


class _Result:
    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeSession:
    """按调用顺序返回预置结果：第 1 次=成交查询，第 2 次起=各通道期初帧。"""

    def __init__(self, results):
        self._results = list(results)
        self.calls = 0

    async def execute(self, stmt, *args, **kwargs):
        index = min(self.calls, len(self._results) - 1)
        self.calls += 1
        return self._results[index]


def _trade_row(**over):
    base = {
        "symbol": "601857.SH",
        "side": "sell",
        "quantity": 200,
        "price": 12.70,
        "executed_at": datetime(2026, 10, 20, 13, 15),
        "exchange_name": "tdx_bridge",
    }
    base.update(over)
    return SimpleNamespace(**base)


def _snapshot_row(payload):
    return SimpleNamespace(payload_json=payload)


class TestCollectDayActivity:
    @pytest.mark.asyncio
    async def test_attaches_tdx_activity_from_trades_and_opening_frame(self):
        db = _FakeSession(
            [
                _Result([_trade_row()]),
                _Result(
                    [
                        _snapshot_row(
                            {
                                "positions": [
                                    {
                                        "symbol": "601857.SH",
                                        "volume": 200,
                                        "cost_price": 4.961,
                                    }
                                ]
                            }
                        )
                    ]
                ),
            ]
        )

        out = await activity.collect_day_activity(
            db, date(2026, 10, 20), [{"key": "tdx"}, {"key": "qmt"}]
        )

        assert list(out) == ["tdx"]
        assert out["tdx"]["closed"][0]["realized_pnl"] == pytest.approx(1547.8)
        assert out["tdx"]["sells"][0]["code"] == "SH601857"

    @pytest.mark.asyncio
    async def test_no_trades_yields_no_activity(self):
        db = _FakeSession([_Result([])])

        out = await activity.collect_day_activity(
            db, date(2026, 10, 20), [{"key": "tdx"}]
        )

        assert out == {}
        assert db.calls == 1, "无成交不得白查期初帧"

    def test_cutoff_is_utc_16_00_previous_day(self):
        """北京 00:00 = UTC 前一日 16:00（snapshot_at 是 naive UTC）。"""
        start, end = activity.day_bounds(date(2026, 10, 20))

        assert start == datetime(2026, 10, 20, 0, 0)
        assert end == datetime(2026, 10, 21, 0, 0)
        assert start - activity._BEIJING_OFFSET == datetime(2026, 10, 19, 16, 0)
