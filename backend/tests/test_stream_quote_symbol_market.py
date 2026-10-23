"""行情端点市场识别（`QuoteService._symbol_market`）回归。

2026-10-23 实测缺陷：大写前缀码（``SH600036``/``SZ000429``——本项目 PG/Redis/
前端/多数 API 的标准形态）既不 ``isdigit()`` 也不以后缀 ``.SH``/``.SZ`` 结尾，
一路落到兜底的 ``return "US"`` → 走腾讯源 → A 股码取不到价 → ``/api/v1/quotes``
返回 404。同一只标的仅因大小写不同就走到不同数据源，属口径分裂。
"""

from __future__ import annotations

import pytest

from backend.services.stream.market_app.services.quote_service import QuoteService


class TestSymbolMarket:
    """符号形态 → 市场，大小写与前后缀形态必须同口径。"""

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "symbol",
        [
            "600036",  # 裸码
            "600036.SH",  # 后缀式（写侧/订单台账形态）
            "SH600036",  # 大写前缀式（API/前端标准形态）
            "sh600036",  # 小写前缀式（快照键形态）
            "SZ000429",
            "sz000429",
            "BJ920950",
        ],
    )
    def test_a_share_forms_classified_as_cn(self, symbol: str) -> None:
        assert QuoteService._symbol_market(symbol) == "CN"

    @pytest.mark.unit
    def test_hk_suffix(self) -> None:
        assert QuoteService._symbol_market("00700.HK") == "HK"

    @pytest.mark.unit
    @pytest.mark.parametrize("symbol", ["AAPL", "TSLA", "BRK.B"])
    def test_us_tickers_stay_us(self, symbol: str) -> None:
        assert QuoteService._symbol_market(symbol) == "US"

    @pytest.mark.unit
    def test_empty_falls_back_to_us(self) -> None:
        assert QuoteService._symbol_market("") == "US"
