"""训练产物 pred.parquet 的闭区间读取测试。"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from backend.scripts.backtest2026.data import load_stored_predictions


def _write_predictions(run_dir: Path, *, label_return: bool = True) -> None:
    dates = pd.bdate_range("2026-01-05", periods=5)
    symbols = ["600000.SH", "000001.SZ"]
    records = []
    for day in dates:
        for position, symbol in enumerate(symbols):
            record = {
                "symbol": symbol,
                "trade_date": day,
                "pred": float(position),
                "extra": "ignored",
            }
            if label_return:
                record["label_return"] = 0.01
            records.append(record)
    pd.DataFrame(records).to_parquet(run_dir / "pred.parquet", index=False)


@pytest.mark.unit
def test_load_stored_predictions_applies_closed_interval(tmp_path: Path) -> None:
    _write_predictions(tmp_path)

    frame = load_stored_predictions(tmp_path, "2026-01-06", "2026-01-08")

    assert len(frame) == 6
    assert frame["trade_date"].min() == pd.Timestamp("2026-01-06")
    assert frame["trade_date"].max() == pd.Timestamp("2026-01-08")
    assert set(frame["symbol"]) == {"SH600000", "SZ000001"}
    assert list(frame.columns) == [
        "symbol",
        "trade_date",
        "pred",
        "label_return",
        "source",
    ]
    assert (frame["source"] == "stored").all()
    assert frame.attrs["label_return_available"] is True


@pytest.mark.unit
def test_load_stored_predictions_without_end_reads_to_file_end(
    tmp_path: Path,
) -> None:
    _write_predictions(tmp_path)

    frame = load_stored_predictions(tmp_path, "2026-01-08")

    assert len(frame) == 4
    assert frame["trade_date"].nunique() == 2


@pytest.mark.unit
def test_load_stored_predictions_marks_missing_label_return(tmp_path: Path) -> None:
    _write_predictions(tmp_path, label_return=False)

    frame = load_stored_predictions(tmp_path, "2026-01-05", "2026-01-09")

    assert "label_return" in frame.columns
    assert frame["label_return"].isna().all()
    assert frame.attrs["label_return_available"] is False
    assert len(frame) == 10
