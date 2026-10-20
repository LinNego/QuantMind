"""桥持仓回执 → 落库持仓（``normalize_bridge_positions``）。

被守护的缺陷（2026-10-20 实况）：账户全部清仓后，桥 ``/api/v1/account/query``
仍把 9 个代码留成 ``total_volume=0`` 的模板行。`sync_account_to_pg` 原样计数 →
日终账本写入 ``position_count=9``，收盘收益报表显示「持仓 9 只 ｜ 市值 ¥0.00」。
0 股不是持仓，必须在落库前丢掉。
"""

from __future__ import annotations

import pytest

from backend.services.live_trading.services.tdx_push_service import (
    normalize_bridge_positions,
)


class TestNormalizeBridgePositions:
    def test_drops_zero_volume_template_rows(self):
        raw = [
            {"stock_code": "601857.SH", "total_volume": 0, "cost_price": 0.0},
            {
                "stock_code": "601000.SH",
                "total_volume": 200,
                "available_volume": 200,
                "cost_price": 4.961,
                "current_price": 4.8,
            },
        ]

        positions = normalize_bridge_positions(raw)

        assert [p["symbol"] for p in positions] == ["601000.SH"]
        assert positions[0]["volume"] == 200
        assert positions[0]["market_value"] == pytest.approx(960.0)

    def test_fills_missing_price_from_quantdb_map(self):
        raw = [{"stock_code": "601000.SH", "total_volume": 200, "cost_price": 4.961}]

        positions = normalize_bridge_positions(raw, {"601000.SH": 4.87})

        assert positions[0]["price"] == pytest.approx(4.87)
        assert positions[0]["market_value"] == pytest.approx(974.0)

    def test_skips_rows_without_symbol(self):
        positions = normalize_bridge_positions(
            [{"stock_code": "", "total_volume": 100}, {"stock_code": "   "}]
        )

        assert positions == []

    def test_none_or_empty_payload_is_empty(self):
        assert normalize_bridge_positions(None) == []
        assert normalize_bridge_positions([]) == []
