"""截面选股市值标注回归（features_daily 精确分区读取）。

2026-10-21 实测缺陷：市值标注沿用 ``features_daily/**/*.parquet`` +
hive_partitioning，把同目录下 ``report/factor_series.parquet``（无 dt 列）也扫进来，
DuckDB 抛 ``Hive partition mismatch ... key "dt" not found`` → 每条推理结果都没有
``market_cap_yi`` / ``market_cap_tier``，连带依赖分层的负向标签（做空候选/错杀候选）
全部消失，前端「市值 / 分层 / 负向标记」三列全空。

跑法（依赖在容器里）：
    docker exec -w /app/backend quantmind python -m pytest \
        tests/test_inference_market_cap_annotation.py -q --no-cov
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from backend.services.api.routers.model_training import (
    _load_features_daily_market_cap,
    _market_cap_tier_zh,
    _symbol_code6,
)


def _write_partition(root: Path, dt: int, rows: list[dict]) -> None:
    part = root / f"dt={dt}"
    part.mkdir(parents=True)
    pd.DataFrame(rows).to_parquet(part / "data.parquet", index=False)


def _write_report_artifact(root: Path) -> None:
    """features_daily/report/ 下的非分区产物（没有 dt 列）。"""
    report = root / "report"
    report.mkdir()
    pd.DataFrame({"factor": ["momentum_5"], "value": [0.1]}).to_parquet(
        report / "factor_series.parquet", index=False
    )


def test_loader_ignores_report_artifacts_without_dt_column(tmp_path: Path):
    """report/factor_series.parquet 无 dt 列，读取不得被它拖垮。"""
    fd = tmp_path / "features_daily"
    _write_partition(
        fd,
        20261020,
        [
            {"symbol": "600036.SH", "total_mv": 5.0e11, "float_mv": 4.9e11},
            {"symbol": "000001.SZ", "total_mv": 2.22e11, "float_mv": 2.22e11},
        ],
    )
    _write_report_artifact(fd)

    mv_map = _load_features_daily_market_cap(fd, 20261020)

    assert mv_map["600036"]["total_mv"] == pytest.approx(5.0e11)
    assert mv_map["600036"]["float_mv"] == pytest.approx(4.9e11)
    assert mv_map["000001"]["total_mv"] == pytest.approx(2.22e11)


def test_loader_returns_empty_when_partition_missing(tmp_path: Path):
    """目标交易日无分区时返回空映射，不抛异常（端点照常出榜单）。"""
    fd = tmp_path / "features_daily"
    _write_partition(
        fd, 20261020, [{"symbol": "600036.SH", "total_mv": 5.0e11, "float_mv": 4.9e11}]
    )
    _write_report_artifact(fd)

    assert _load_features_daily_market_cap(fd, 20261017) == {}
    assert _load_features_daily_market_cap(tmp_path / "missing", 20261020) == {}


def test_loader_keeps_missing_market_cap_as_none(tmp_path: Path):
    """空值/Nan 转 None（展示层据此显示 —，不能变成 0）。"""
    fd = tmp_path / "features_daily"
    _write_partition(
        fd,
        20261020,
        [
            {"symbol": "600036.SH", "total_mv": None, "float_mv": None},
            {"symbol": "000001.SZ", "total_mv": float("nan"), "float_mv": 2.22e11},
        ],
    )

    mv_map = _load_features_daily_market_cap(fd, 20261020)

    assert mv_map["600036"]["total_mv"] is None
    assert mv_map["600036"]["float_mv"] is None
    assert mv_map["000001"]["total_mv"] is None
    assert mv_map["000001"]["float_mv"] == pytest.approx(2.22e11)


@pytest.mark.parametrize(
    ("yiyi", "tier"),
    [
        (0.0, "微盘"),
        (29.99, "微盘"),
        (30.0, "小盘"),
        (99.99, "小盘"),
        (100.0, "中盘"),
        (299.99, "中盘"),
        (300.0, "大盘"),
        (999.99, "大盘"),
        (1000.0, "超大盘"),
        (5000.0, "超大盘"),
    ],
)
def test_market_cap_tier_boundaries(yiyi: float, tier: str):
    """分档边界：微盘<30亿 / 小盘30-100 / 中盘100-300 / 大盘300-1000 / 超大盘≥1000。"""
    assert _market_cap_tier_zh(yiyi) == tier


@pytest.mark.parametrize(
    ("raw", "code6"),
    [
        ("600036.SH", "600036"),
        ("000001.SZ", "000001"),
        ("830799.BJ", "830799"),
        ("SH600036", "600036"),
        ("sz000001", "000001"),
        (" 600036.sh ", "600036"),
        ("600036", "600036"),
        ("", ""),
        (None, ""),
    ],
)
def test_symbol_code6_normalizes_all_supported_forms(raw, code6):
    """信号表是纯数字，features_daily 是后缀式，统一折成 6 位纯数字。"""
    assert _symbol_code6(raw) == code6
