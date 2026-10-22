"""alpha360 VWAP 名义口径（2026-10-22 审计修复）。

问题：``VWAP{d} = vwap[t−d] / close[t]``，分子 ``vwap = amount(名义)/volume``
是**名义价**，分母却是 ``daily_forward`` 的**前复权 close**。两者不同基 →
VWAP{d} 额外携带 ``1/adj(t)``（adj = qfq/raw）。训练期 adj 随分红单调下行，
该因子于是退化成「分红/复权暴露」代理（实测 VWAP55 与 adj 的秩相关
train −0.63 / test −0.29）。

修复：VWAP 一律用**名义/名义**（分母取 ``daily_unadjusted`` close），
CLOSE/OPEN/HIGH/LOW 仍是 qfq/qfq，VOLUME 仍是 vol/vol。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backend.services.engine.factor_library import alpha360
from backend.services.engine.factor_research import data as frdata

DATES = pd.to_datetime(["2024-01-02", "2024-01-03"])


def _frame(values: list[float]) -> pd.DataFrame:
    return pd.DataFrame({"S": values}, index=DATES, dtype=float)


def _daily(close: list[float]) -> dict[str, pd.DataFrame]:
    return {
        "close": _frame(close),
        "open": _frame(close),
        "high": _frame(close),
        "low": _frame(close),
        "volume": _frame([100.0, 100.0]),
    }


def _last_partition(daily, vwap, close_nominal=None) -> pd.DataFrame:
    out = None
    for _, frame in alpha360.iter_partitions(
        daily, vwap, close_nominal=close_nominal
    ):
        out = frame
    assert out is not None
    return out


def test_vwap_uses_nominal_close_as_denominator():
    """名义 vwap 10 / 名义 close 20 = 0.5；不是 10/10（qfq close）。"""
    daily = _daily([10.0, 10.0])
    vwap = _frame([10.0, 10.0])
    close_nominal = _frame([20.0, 20.0])

    day = _last_partition(daily, vwap, close_nominal)

    assert day.loc["S", "VWAP0"] == pytest.approx(0.5)
    assert day.loc["S", "VWAP1"] == pytest.approx(0.5)


def test_vwap_without_nominal_close_keeps_legacy_mixed_basis():
    """未传名义价时维持旧口径（兼容历史调用方）——分子名义、分母 qfq。"""
    daily = _daily([10.0, 10.0])
    vwap = _frame([10.0, 10.0])

    day = _last_partition(daily, vwap)

    assert day.loc["S", "VWAP0"] == pytest.approx(1.0)


def test_close_block_stays_on_adjusted_basis():
    """CLOSE{d} 必须继续用前复权 close / 前复权 close（不受名义价影响）。"""
    daily = _daily([10.0, 8.0])
    vwap = _frame([10.0, 10.0])
    close_nominal = _frame([20.0, 16.0])

    day = _last_partition(daily, vwap, close_nominal)

    assert day.loc["S", "CLOSE0"] == pytest.approx(1.0)      # 当日 8/8
    assert day.loc["S", "CLOSE1"] == pytest.approx(1.25)     # 10/8（qfq，非名义 20/16）
    # 分母恒为**当日**名义 close（16）：VWAP0=10/16、VWAP1=10/16
    assert day.loc["S", "VWAP0"] == pytest.approx(0.625)
    assert day.loc["S", "VWAP1"] == pytest.approx(0.625)


def test_nominal_close_aligned_by_index_and_columns():
    """名义价表按日期/标的对齐；缺日 → NaN，绝不回填复用别的日子。"""
    daily = _daily([10.0, 10.0])
    vwap = _frame([10.0, 10.0])
    close_nominal = pd.DataFrame({"S": [20.0]}, index=DATES[:1], dtype=float)

    parts = [f for _, f in alpha360.iter_partitions(daily, vwap, close_nominal)]

    assert parts[0].loc["S", "VWAP0"] == pytest.approx(0.5)  # 首日有名义价
    # 次日名义价缺失 → 基准 NaN → VWAP0 当日为 NaN
    assert np.isnan(parts[1].loc["S", "VWAP1"])


# ---------- 加载器：parts 路由 ----------


def test_load_daily_panel_reads_unadjusted_when_parts_given(tmp_path, monkeypatch):
    root = tmp_path / "cnroot"
    monkeypatch.setenv("QM_QUANTDB_DATA_DIR", str(root))
    for dataset, close in (("daily_forward", 10.0), ("daily_unadjusted", 20.0)):
        p = root / "1_kline_data" / dataset / "dt=20240102" / "data.parquet"
        p.parent.mkdir(parents=True)
        pd.DataFrame(
            {"symbol": ["600000.SH"], "time": ["20240102"], "close": [close]}
        ).to_parquet(p, index=False)

    qfq = frdata.load_daily_panel("20240102", "20240102", fields=("close",))
    raw = frdata.load_daily_panel(
        "20240102", "20240102", fields=("close",),
        parts=("1_kline_data", "daily_unadjusted"),
    )

    assert float(qfq["close"].iloc[0, 0]) == 10.0
    assert float(raw["close"].iloc[0, 0]) == 20.0


def test_load_daily_panel_missing_parts_names_the_dataset(tmp_path, monkeypatch):
    root = tmp_path / "cnroot"
    monkeypatch.setenv("QM_QUANTDB_DATA_DIR", str(root))
    (root / "1_kline_data").mkdir(parents=True)
    with pytest.raises(FileNotFoundError, match="daily_unadjusted"):
        frdata.load_daily_panel(
            "20240102", "20240102",
            parts=("1_kline_data", "daily_unadjusted"),
        )
