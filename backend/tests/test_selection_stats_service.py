"""截面选股历史统计 · 服务层测试（假 hub，真 DuckDB 读 pred.parquet）。"""

from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta

import pandas as pd
import pytest

from backend.services.engine.services.model_selection_stats_service import (
    ModelSelectionStatsService,
    SelectionStatsError,
)
from backend.shared.model_registry import ResolvedModel


def _business_days(start: str, count: int) -> list[date]:
    day = date.fromisoformat(start)
    out: list[date] = []
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


class FakeHub:
    def __init__(self, closes: dict[str, dict[date, float]], index: dict[date, float]):
        self.closes = closes
        self.index = index
        self.price_calls: list[dict] = []
        self.index_calls: list[dict] = []

    def fetch_daily_close_batch(self, symbols, start, end, *, adjust="qfq"):
        self.price_calls.append(
            {"symbols": list(symbols), "start": start, "end": end, "adjust": adjust}
        )
        rows = []
        for symbol in symbols:
            for day, price in self.closes.get(symbol, {}).items():
                if start <= day <= end:
                    rows.append({"symbol": symbol, "trade_date": day, "close": price})
        return pd.DataFrame(rows, columns=["symbol", "trade_date", "close"])

    def fetch_index_kline(self, symbol, start, end):
        self.index_calls.append({"symbol": symbol, "start": start, "end": end})
        # 真实 hub 的 trade_date 是 datetime64；这里保持同型，防止
        # datetime 子类被当成 date 放行、基准查询全 miss。
        rows = [
            {"symbol": symbol, "trade_date": pd.Timestamp(day), "close": price}
            for day, price in self.index.items()
            if start <= day <= end
        ]
        return pd.DataFrame(rows, columns=["symbol", "trade_date", "close"])


class FakeResolver:
    def __init__(self, storage_path):
        self.storage_path = str(storage_path)
        self.calls: list[dict] = []

    async def resolve_effective_model(self, **kwargs):
        self.calls.append(kwargs)
        return ResolvedModel(
            effective_model_id=kwargs.get("model_id") or "m1",
            model_source="user",
            fallback_used=False,
            fallback_reason="",
            storage_path=self.storage_path,
            model_file="model.pkl",
            status="ready",
        )


DAYS = _business_days("2026-01-05", 10)


def _write_model(
    tmp_path,
    *,
    market: str = "CN",
    include_pred: bool = True,
    test_days: list[date] | None = None,
    horizon: int = 2,
) -> str:
    days = test_days or DAYS[:6]
    meta = {
        "display_name": "测试模型",
        "market": market,
        "target_horizon_days": horizon,
        "execution_lag_days": 1,
        "test_start": days[0].isoformat(),
        "test_end": days[-1].isoformat(),
    }
    (tmp_path / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    if not include_pred:
        return str(tmp_path)

    rows = []
    for day in days:
        rows.append(
            {
                "symbol": "SH600000",
                "trade_date": day,
                "label": 0.1,
                "pred": 0.9,
                "split": "test",
            }
        )
        rows.append(
            {
                "symbol": "SH600001",
                "trade_date": day,
                "label": 0.0,
                "pred": 0.5,
                "split": "test",
            }
        )
        rows.append(
            {
                "symbol": "SH600002",
                "trade_date": day,
                "label": -0.1,
                "pred": 0.1,
                "split": "test",
            }
        )
        # 训练行：pred 最高，若被误用会挤掉 test 的入选
        rows.append(
            {
                "symbol": "SH600099",
                "trade_date": day,
                "label": 0.0,
                "pred": 999.0,
                "split": "train",
            }
        )
        # 空 pred 行：必须被过滤
        rows.append(
            {
                "symbol": "SH600098",
                "trade_date": day,
                "label": 0.0,
                "pred": None,
                "split": "test",
            }
        )
    pd.DataFrame(rows).to_parquet(tmp_path / "pred.parquet", index=False)
    return str(tmp_path)


def _closes_for(symbols: list[str], days: list[date], *, start_price: float = 10.0):
    closes: dict[str, dict[date, float]] = {}
    for offset, symbol in enumerate(symbols):
        price = start_price + offset
        series = {}
        for step, day in enumerate(days):
            series[day] = round(price * (1 + 0.01 * step), 4)
        closes[symbol] = series
    return closes


def _index_for(days: list[date]):
    return {day: 100.0 + step for step, day in enumerate(days)}


def _service(tmp_path, hub: FakeHub, resolver: FakeResolver | None = None):
    ModelSelectionStatsService.clear_cache()
    return ModelSelectionStatsService(
        hub=hub,
        resolver=resolver or FakeResolver(tmp_path),
    )


def _run(service, **kwargs):
    return asyncio.run(service.compute(**kwargs))


def test_uses_only_test_split_and_honours_top_k(tmp_path):
    storage = _write_model(tmp_path)
    hub = FakeHub(
        closes=_closes_for(["600000.SH", "600001.SH", "600002.SH"], DAYS),
        index=_index_for(DAYS),
    )

    result = _run(
        _service(storage, hub),
        tenant_id="default",
        user_id="u1",
        model_id="m1",
        start=DAYS[0],
        end=DAYS[5],
        top_k=2,
        hold_days=2,
        capital=10_000_000,
    )

    # 训练行（SH600099）与空 pred 行绝不参与
    assert hub.price_calls, "应读取收盘价"
    requested = set(hub.price_calls[0]["symbols"])
    assert "600099.SH" not in requested
    assert "600098.SH" not in requested
    assert {"600000.SH", "600001.SH"}.issubset(requested)
    assert hub.price_calls[0]["adjust"] == "qfq"

    assert result["split"] == "test"
    assert result["requested"]["top_k"] == 2
    assert result["summary"]["cohorts"] >= 2
    assert all(row["held"] == 2 for row in result["cohorts"])
    assert result["params"]["execution_lag_days"] == 1
    assert result["summary"]["price_coverage_pct"] == pytest.approx(100.0)
    # 基准必须真正接上：datetime 键未归一化时这里会是 None
    assert result["summary"]["benchmark_return_pct"] is not None
    assert "benchmark_unavailable" not in result["warnings"]


def test_range_is_clamped_to_available_test_days(tmp_path):
    storage = _write_model(tmp_path)
    hub = FakeHub(
        closes=_closes_for(["600000.SH", "600001.SH"], DAYS),
        index=_index_for(DAYS),
    )

    result = _run(
        _service(storage, hub),
        tenant_id="default",
        user_id="u1",
        model_id="m1",
        start=date(2025, 12, 1),
        end=date(2026, 2, 1),
        top_k=2,
        hold_days=2,
        capital=1_000_000,
    )

    assert result["effective"]["start"] == DAYS[0].isoformat()
    assert result["effective"]["end"] == DAYS[5].isoformat()
    assert "effective_start_clamped" in result["warnings"]
    assert "effective_end_clamped" in result["warnings"]


def test_capital_scales_money_only_and_cache_avoids_recompute(tmp_path):
    storage = _write_model(tmp_path)
    hub = FakeHub(
        closes=_closes_for(["600000.SH", "600001.SH"], DAYS),
        index=_index_for(DAYS),
    )
    service = _service(storage, hub)

    first = _run(
        service,
        tenant_id="default",
        user_id="u1",
        model_id="m1",
        start=DAYS[0],
        end=DAYS[5],
        top_k=2,
        hold_days=2,
        capital=10_000_000,
    )
    calls_after_first = len(hub.price_calls)

    second = _run(
        service,
        tenant_id="default",
        user_id="u1",
        model_id="m1",
        start=DAYS[0],
        end=DAYS[5],
        top_k=2,
        hold_days=2,
        capital=30_000_000,
    )

    assert len(hub.price_calls) == calls_after_first  # 缓存命中
    assert second["summary"]["total_return_pct"] == pytest.approx(
        first["summary"]["total_return_pct"]
    )
    assert second["summary"]["final_equity"] == pytest.approx(
        first["summary"]["final_equity"] * 3
    )
    assert second["summary"]["profit_amount"] == pytest.approx(
        first["summary"]["profit_amount"] * 3
    )
    assert second["summary"]["initial_capital"] == pytest.approx(30_000_000)


def test_as_date_normalizes_datetime_subclass():
    from datetime import datetime

    from backend.services.engine.services.model_selection_stats_service import _as_date

    assert _as_date(datetime(2026, 1, 5, 15, 30)) == date(2026, 1, 5)
    assert _as_date(pd.Timestamp("2026-01-05 15:30")) == date(2026, 1, 5)
    assert _as_date(date(2026, 1, 5)) == date(2026, 1, 5)


def test_unsupported_market_is_rejected(tmp_path):
    storage = _write_model(tmp_path, market="HK")
    hub = FakeHub(closes={}, index={})

    with pytest.raises(SelectionStatsError) as exc:
        _run(
            _service(storage, hub),
            tenant_id="default",
            user_id="u1",
            model_id="m1",
            top_k=50,
            hold_days=None,
            capital=1_000_000,
        )
    assert exc.value.code == "unsupported_market"
    assert exc.value.status_code == 422


def test_missing_pred_parquet(tmp_path):
    storage = _write_model(tmp_path, include_pred=False)
    hub = FakeHub(closes={}, index={})

    with pytest.raises(SelectionStatsError) as exc:
        _run(
            _service(storage, hub),
            tenant_id="default",
            user_id="u1",
            model_id="m1",
            top_k=50,
            hold_days=None,
            capital=1_000_000,
        )
    assert exc.value.code == "pred_unavailable"


def test_no_test_rows(tmp_path):
    storage = _write_model(tmp_path)
    frame = pd.read_parquet(tmp_path / "pred.parquet")
    frame["split"] = "valid"
    frame.to_parquet(tmp_path / "pred.parquet", index=False)
    hub = FakeHub(closes={}, index={})

    with pytest.raises(SelectionStatsError) as exc:
        _run(
            _service(storage, hub),
            tenant_id="default",
            user_id="u1",
            model_id="m1",
            top_k=50,
            hold_days=None,
            capital=1_000_000,
        )
    assert exc.value.code == "split_unavailable"


def test_price_coverage_gate_blocks_untrusted_quotes(tmp_path):
    storage = _write_model(tmp_path)
    # 只给 600000 价格：覆盖率 50% < 90%
    hub = FakeHub(closes=_closes_for(["600000.SH"], DAYS), index=_index_for(DAYS))

    with pytest.raises(SelectionStatsError) as exc:
        _run(
            _service(storage, hub),
            tenant_id="default",
            user_id="u1",
            model_id="m1",
            start=DAYS[0],
            end=DAYS[5],
            top_k=2,
            hold_days=2,
            capital=1_000_000,
        )
    assert exc.value.code == "quote_source_unavailable"
    assert exc.value.status_code == 422


def test_invalid_params(tmp_path):
    storage = _write_model(tmp_path)
    hub = FakeHub(closes={}, index={})
    service = _service(storage, hub)

    for kwargs in (
        {"top_k": 0},
        {"top_k": 51},
        {"hold_days": 0},
        {"hold_days": 61},
        {"capital": 0},
        {"start": DAYS[3], "end": DAYS[1]},
    ):
        with pytest.raises(SelectionStatsError) as exc:
            _run(
                service,
                tenant_id="default",
                user_id="u1",
                model_id="m1",
                top_k=kwargs.pop("top_k", 50),
                hold_days=kwargs.pop("hold_days", None),
                capital=kwargs.pop("capital", 1_000_000),
                start=kwargs.pop("start", None),
                end=kwargs.pop("end", None),
            )
        assert exc.value.code == "invalid_params"
