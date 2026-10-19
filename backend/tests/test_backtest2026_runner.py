"""回测报告辅助函数测试。"""

from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd
import pytest

from backend.scripts.backtest2026.data import MarketPanel
from backend.scripts.backtest2026.runner import (
    _benchmark_daily,
    _jsonable,
    _md_table,
    _num,
    _pct,
    _per_stock_table,
    _signal_gate_counts,
    _strategy_block,
    _summary_frame,
    _variants,
    _write_json,
)
from backend.scripts.backtest2026.strategies import StrategyResult, StrategySpec

SYMBOLS = ("SH600000", "SH600001", "SH600002")


def _panel(close: list[list[float]] | None = None) -> MarketPanel:
    n_days = len(close) if close else 3
    dates = pd.bdate_range("2026-01-05", periods=n_days)
    values = close or [[10.0, 10.0, 10.0]] * n_days
    close_qfq = pd.DataFrame(values, index=dates, columns=list(SYMBOLS))
    false_matrix = pd.DataFrame(False, index=dates, columns=list(SYMBOLS))
    return MarketPanel(
        dates=dates,
        full_calendar=dates,
        symbols=pd.Index(SYMBOLS),
        close_raw=close_qfq.copy(),
        close_qfq=close_qfq,
        volume=pd.DataFrame(1_000_000.0, index=dates, columns=list(SYMBOLS)),
        limit_up=false_matrix.copy(),
        limit_down=false_matrix.copy(),
        suspended=false_matrix.copy(),
        is_st=pd.Series(False, index=list(SYMBOLS)),
        age_days=pd.DataFrame(200.0, index=dates, columns=list(SYMBOLS)),
        close_qfq_ffill=close_qfq.ffill(),
    )


@pytest.mark.unit
def test_jsonable_converts_numpy_nan_timestamp_and_frames() -> None:
    payload = _jsonable(
        {
            "int": np.int64(3),
            "float": np.float64(1.5),
            "nan": np.float64(np.nan),
            "nan_float": float("nan"),
            "bool": np.bool_(True),
            "date": pd.Timestamp("2026-01-05"),
            "list": (np.int64(1),),
            "frame": pd.DataFrame({"a": [1]}),
            "series": pd.Series({"b": 2}),
        }
    )
    assert payload["int"] == 3
    assert payload["float"] == 1.5
    assert payload["nan"] is None
    assert payload["nan_float"] is None
    assert payload["bool"] is True
    assert payload["date"] == "2026-01-05"
    assert payload["list"] == [1]
    assert payload["frame"] == [{"a": 1}]
    assert payload["series"] == {"b": 2}


@pytest.mark.unit
def test_write_json_roundtrip(tmp_path) -> None:
    path = tmp_path / "out.json"
    _write_json(path, {"value": np.float64(np.inf)})
    assert json.loads(path.read_text(encoding="utf-8")) == {"value": None}


@pytest.mark.unit
def test_pct_num_and_md_table_format_missing_values() -> None:
    assert _pct(0.1234) == "12.34%"
    assert _pct(float("nan")) == "—"
    assert _num(1.23456, 2) == "1.23"
    assert _num(None) == "—"
    frame = pd.DataFrame(
        {"name": ["a"], "value": [0.5], "missing": [np.nan], "count": [3.0]}
    )
    table = _md_table(frame, percent_cols={"value"})
    assert "| name | value | missing | count |" in table
    assert "| a | 50.00% | — | 3.000 |" in table


@pytest.mark.unit
def test_benchmark_daily_holds_next_day_equal_weight() -> None:
    panel = _panel([[10.0, 10.0, 10.0], [11.0, 11.0, 11.0], [12.0, 12.0, 12.0]])
    pred = pd.Series(dict.fromkeys(SYMBOLS, 1.0))
    benchmark = _benchmark_daily(
        panel, {panel.dates[0]: pred, panel.dates[1]: pred}, initial_capital=100.0
    )
    assert benchmark["return"].tolist() == pytest.approx([0.1, 12.0 / 11.0 - 1.0])
    assert benchmark["equity"].iloc[-1] == pytest.approx(120.0)


@pytest.mark.unit
def test_benchmark_daily_skips_last_day_nan_and_empty_selection() -> None:
    pred = pd.Series(dict.fromkeys(SYMBOLS, 1.0))
    panel = _panel([[10.0, 10.0, 10.0], [np.nan, np.nan, np.nan], [12.0, 12.0, 12.0]])
    assert _benchmark_daily(panel, {panel.dates[2]: pred}, 100.0).empty
    assert _benchmark_daily(panel, {panel.dates[0]: pred}, 100.0).empty
    empty_pred = pd.Series(dict.fromkeys(SYMBOLS, np.nan))
    good_panel = _panel()
    assert _benchmark_daily(good_panel, {good_panel.dates[0]: empty_pred}, 100.0).empty


@pytest.mark.unit
def test_summary_frame_maps_labels_to_values() -> None:
    frame = _summary_frame({"a": 1.0}, [("a", "指标 A"), ("missing", "指标 B")])
    assert frame["指标"].tolist() == ["指标 A", "指标 B"]
    assert frame["数值"].iloc[0] == 1.0
    assert pd.isna(frame["数值"].iloc[1])


@pytest.mark.unit
def test_per_stock_table_marks_never_predicted_symbols() -> None:
    panel = _panel()
    pred_panel = pd.DataFrame(
        {
            "symbol": ["SH600000", "SH600000"],
            "trade_date": [panel.dates[0], panel.dates[1]],
            "pred": [0.1, 0.2],
        }
    )
    table = _per_stock_table(pred_panel, panel)
    assert table.loc["SH600000", "days"] == 2
    assert table.loc["SH600000", "status"] == "predicted"
    assert table.loc["SH600001", "status"] == "never_predicted"
    assert table.loc["SH600001", "days"] == 0


@pytest.mark.unit
def test_variants_toggle_filters_and_costs() -> None:
    base = StrategySpec(mode="cohort", k=5, horizon=5, initial_capital=1_000_000.0)
    variants = _variants(base)
    assert variants["no_st_filter"].exclude_st is False
    assert variants["no_limit_filter"].exclude_limits is False
    assert variants["no_new_filter"].exclude_new is False
    assert variants["no_filters"].exclude_st is False
    assert variants["stamp_duty_5bp"].stamp == pytest.approx(0.0005)
    assert variants["slippage_5bp"].slippage == pytest.approx(0.0005)


@pytest.mark.unit
def test_signal_gate_counts_counts_top20_rows() -> None:
    panel = _panel()
    day = panel.dates[0]
    pred = pd.Series({"SH600000": 3.0, "SH600001": 2.0, "SH600002": 1.0})
    panel.is_st.loc["SH600000"] = True
    panel.limit_up.at[day, "SH600001"] = True
    panel.age_days.at[day, "SH600002"] = 10
    counts = _signal_gate_counts({day: pred}, panel)
    assert counts == {
        "top20_rows": 3,
        "st": 1,
        "limit_up": 1,
        "limit_down": 0,
        "suspended": 0,
        "new": 1,
    }


@pytest.mark.unit
def test_strategy_block_summarizes_equity_and_trades() -> None:
    dates = pd.bdate_range("2026-01-05", periods=3)
    equity = pd.DataFrame({"equity": [100.0, 101.0, 102.0]}, index=dates)
    empty_trades = pd.DataFrame(
        columns=[
            "bucket",
            "label",
            "side",
            "signal_date",
            "date",
            "symbol",
            "shares",
            "price",
            "amount",
            "fee",
            "net_pnl",
            "exit_reason",
            "scheduled_exit",
        ]
    )
    skips = pd.DataFrame({"reason": ["no_cash", "no_cash"]})
    result = StrategyResult(
        spec=StrategySpec(mode="cohort", k=1, horizon=1, initial_capital=100.0),
        equity=equity,
        trades=empty_trades,
        skips=skips,
        signal_days=2,
        selected_total=3,
    )
    block = _strategy_block(result, initial_capital=100.0)
    assert block["performance"]["total_return"] == pytest.approx(0.02)
    assert block["n_signals"] == 2
    assert block["n_selected"] == 3
    assert block["n_skips"] == 2
    assert block["skip_reasons"] == {"no_cash": 2}
    assert math.isfinite(block["monthly"]["2026-01"])
