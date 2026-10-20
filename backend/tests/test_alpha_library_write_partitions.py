"""alpha_library 分区写盘回归测试。

2026-10-20 生产事故：``a101_084`` 等因子的有限 float64 值超过 float32 上限
（3.4e38），``astype(np.float32)`` 时溢出成 ±inf；写盘前的 ``replace`` 只看得见
原始 float64（有限值），拦不住 cast 后的 inf，最终分区里落进 inf（实测 26 格）。
训练侧对 inf 的处理因模型而异（LGB 掩蔽 / XGB 炸矩阵），必须在写盘处清成 NaN。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backend.scripts.alpha_library_factors import write_partitions


def _factor_frame(
    dates: list[str], symbols: list[str], values: list[list[float]]
) -> pd.DataFrame:
    """构造与因子计算一致的宽表：index=交易日，columns=symbol。"""
    return pd.DataFrame(
        values, index=pd.DatetimeIndex(dates), columns=symbols, dtype=float
    )


def test_write_partitions_scrubs_float32_overflow_to_nan(tmp_path):
    # Arrange: 1e300 是有限 float64，cast 到 float32 必然溢出成 inf
    dates = ["2026-10-19"]
    factors = {
        "overflow": _factor_frame(dates, ["000001.SZ"], [[1e300]]),
        "normal": _factor_frame(dates, ["000001.SZ"], [[1.5]]),
    }

    # Act
    written = write_partitions(factors, start_dt="20261019", out_root=tmp_path)

    # Assert
    assert written == 1
    out = pd.read_parquet(tmp_path / "dt=20261019" / "data.parquet")
    numeric = out.select_dtypes(include=[np.number]).to_numpy()
    assert not np.isinf(numeric).any()
    assert pd.isna(out.loc[0, "overflow"])
    assert out.loc[0, "normal"] == pytest.approx(1.5)


def test_write_partitions_skips_dates_before_start_dt(tmp_path):
    # Arrange
    dates = ["2026-10-16", "2026-10-19"]
    factors = {"f": _factor_frame(dates, ["000001.SZ"], [[1.0], [2.0]])}

    # Act
    written = write_partitions(factors, start_dt="20261019", out_root=tmp_path)

    # Assert
    assert written == 1
    assert not (tmp_path / "dt=20261016").exists()
    out = pd.read_parquet(tmp_path / "dt=20261019" / "data.parquet")
    assert out.loc[0, "f"] == pytest.approx(2.0)


def test_write_partitions_does_not_overwrite_existing_partition(tmp_path):
    # Arrange: 已有分区（上次运行残留）必须原样保留，增量语义依赖此行为
    existing = tmp_path / "dt=20261019"
    existing.mkdir(parents=True)
    pd.DataFrame(
        {"symbol": ["000001.SZ"], "time": [pd.Timestamp("2026-10-19")], "f": [7.0]}
    ).to_parquet(existing / "data.parquet", index=False)
    factors = {
        "f": _factor_frame(["2026-10-16", "2026-10-19"], ["000001.SZ"], [[1.0], [2.0]])
    }

    # Act
    written = write_partitions(factors, start_dt="20261016", out_root=tmp_path)

    # Assert
    assert written == 1  # 只写了 20261016，20261019 被跳过
    kept = pd.read_parquet(existing / "data.parquet")
    assert kept.loc[0, "f"] == pytest.approx(7.0)
