"""契约汇总与尾窗探针：必需列归库、重叠日比对、回补日识别。"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from backend.scripts.factor_materialize import contract, probe


def _model(root: Path, name: str, payload: dict) -> Path:
    path = root / "models" / "production" / name / "metadata.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _partition(root: Path, dt: str, frame: pd.DataFrame) -> Path:
    path = root / f"dt={dt}" / "data.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(path, index=False)
    return path


@pytest.mark.unit
def test_contract_maps_qualified_and_bare_sources(tmp_path: Path) -> None:
    # Arrange：副库写 "库:列"，裸名归锚库
    _model(
        tmp_path,
        "m1",
        {
            "factor_source": "cand_factors",
            "feature_columns": ["a", "b", "c", "d"],
            "factor_field_sources": {
                "a": "factor_defs:a",
                "b": "gap_mined:MG2_BALCHG_3M",
                "c": "l1_factors:some_l1",
                "d": "bare_d",
            },
        },
    )

    # Act
    result = contract.load_contract(tmp_path, include_users=False)

    # Assert
    assert result.required["factor_defs"] == ("a",)
    assert result.required["gap_mined"] == ("MG2_BALCHG_3M",)
    assert result.required["cand_factors"] == ("bare_d",)
    assert result.external["l1_factors"] == ("some_l1",)
    assert result.models == 1


@pytest.mark.unit
def test_contract_marks_features_without_source_as_unmapped(tmp_path: Path) -> None:
    _model(tmp_path, "m1", {"feature_columns": ["KMID", "KLEN"]})

    result = contract.load_contract(tmp_path, include_users=False)

    assert set(result.unmapped) == {"KMID", "KLEN"}
    assert result.required == {}


@pytest.mark.unit
def test_contract_skips_broken_metadata(tmp_path: Path) -> None:
    path = tmp_path / "models" / "production" / "bad" / "metadata.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    _model(tmp_path, "ok", {"feature_columns": ["x"], "factor_source": "factor_defs"})

    result = contract.load_contract(tmp_path, include_users=False)

    assert result.required["factor_defs"] == ("x",)


@pytest.mark.unit
def test_contract_gate_columns_drops_known_empty(tmp_path: Path) -> None:
    _model(
        tmp_path,
        "m1",
        {
            "factor_source": "factor_defs",
            "feature_columns": ["feat_idio_to_vol", "feat_ok"],
        },
    )
    result = contract.load_contract(tmp_path, include_users=False)

    assert contract.gate_columns("factor_defs", result) == ("feat_ok",)


@pytest.mark.unit
def test_coverage_columns_separates_known_lag(tmp_path: Path) -> None:
    # 两融 T+1 列只上报不设闸，避免每个交易日误报
    _model(
        tmp_path,
        "m1",
        {
            "factor_source": "gap_mined",
            "feature_columns": ["MG2_BALCHG_3M", "flow_buy_amount_D3"],
        },
    )
    result = contract.load_contract(tmp_path, include_users=False)

    strict, deferred = contract.coverage_columns("gap_mined", result)

    assert strict == ("flow_buy_amount_D3",)
    assert deferred == ("MG2_BALCHG_3M",)
    # 回补检测仍要包含滞后列（次日重算依赖它）
    assert "MG2_BALCHG_3M" in contract.gate_columns("gap_mined", result)


@pytest.mark.unit
def test_compare_partitions_passes_on_identical_frames(tmp_path: Path) -> None:
    frame = pd.DataFrame(
        {"symbol": ["A", "B"], "time": [1, 1], "x": [1.0, 2.0], "y": [3.0, 4.0]}
    )
    official = _partition(tmp_path / "official", "20261015", frame)
    staged = _partition(tmp_path / "staged", "20261015", frame.copy())

    report = probe.compare_partitions(official, staged, required=["x"])

    assert report["ok"] is True
    assert report["issues"] == []


@pytest.mark.unit
def test_compare_partitions_flags_required_column_gone_empty(tmp_path: Path) -> None:
    frame = pd.DataFrame({"symbol": ["A", "B"], "x": [1.0, 2.0]})
    empty = pd.DataFrame({"symbol": ["A", "B"], "x": [None, None]})
    official = _partition(tmp_path / "official", "20261015", frame)
    staged = _partition(tmp_path / "staged", "20261015", empty)

    report = probe.compare_partitions(official, staged, required=["x"])

    assert report["ok"] is False
    kinds = {issue["kind"] for issue in report["issues"]}
    assert "required_empty" in kinds


@pytest.mark.unit
def test_compare_partitions_ignores_non_required_empty_as_info(
    tmp_path: Path,
) -> None:
    # 长窗非必需列在短预热下整列空：只报信息，不阻断
    frame = pd.DataFrame({"symbol": ["A"], "req": [1.0], "long": [2.0]})
    staged_frame = pd.DataFrame({"symbol": ["A"], "req": [1.0], "long": [None]})
    official = _partition(tmp_path / "official", "20261015", frame)
    staged = _partition(tmp_path / "staged", "20261015", staged_frame)

    report = probe.compare_partitions(official, staged, required=["req"])

    assert report["ok"] is True
    assert report["informational"][0]["column"] == "long"


@pytest.mark.unit
def test_compare_partitions_flags_large_required_drift(tmp_path: Path) -> None:
    n = 120
    frame = pd.DataFrame({"symbol": [f"S{i}" for i in range(n)], "x": [1.0] * n})
    drifted = pd.DataFrame({"symbol": [f"S{i}" for i in range(n)], "x": [2.0] * n})
    official = _partition(tmp_path / "official", "20261015", frame)
    staged = _partition(tmp_path / "staged", "20261015", drifted)

    report = probe.compare_partitions(official, staged, required=["x"])

    assert report["ok"] is False
    kinds = {issue["kind"] for issue in report["issues"]}
    assert "required_drift" in kinds


@pytest.mark.unit
def test_compare_partitions_handles_datetime_metadata_columns(tmp_path: Path) -> None:
    # 真实分区 time/date 是 timestamp：漂移比对必须跳过非数值列（曾必抛 TypeError）
    frame = pd.DataFrame(
        {
            "symbol": ["A", "B"],
            "time": pd.to_datetime(["2026-10-15"] * 2),
            "x": [1.0, 2.0],
        }
    )
    official = _partition(tmp_path / "official", "20261015", frame)
    staged = _partition(tmp_path / "staged", "20261015", frame.copy())

    report = probe.compare_partitions(official, staged, required=["x"])

    assert report["ok"] is True
    assert report["compared_columns"] == 2


@pytest.mark.unit
def test_emptiness_regressions_flags_absent_column(tmp_path: Path) -> None:
    # 前一日有值、当日整列缺列（schema 掉列）同样要进回补窗口
    root = tmp_path / "lib"
    _partition(root, "20261015", pd.DataFrame({"symbol": ["A"], "x": [1.0]}))
    _partition(root, "20261016", pd.DataFrame({"symbol": ["A"], "y": [2.0]}))

    issues = probe.emptiness_regressions(root, ["20261015", "20261016"], required=["x"])

    assert issues["20261016"]["all_empty"] == ["x"]
    assert issues["20261016"]["absent"] == ["x"]


@pytest.mark.unit
def test_compare_partitions_flags_universe_drop(tmp_path: Path) -> None:
    frame = pd.DataFrame({"symbol": ["A", "B", "C", "D"], "x": [1.0] * 4})
    staged_frame = pd.DataFrame({"symbol": ["A"], "x": [1.0]})
    official = _partition(tmp_path / "official", "20261015", frame)
    staged = _partition(tmp_path / "staged", "20261015", staged_frame)

    report = probe.compare_partitions(official, staged, required=["x"])

    kinds = {issue["kind"] for issue in report["issues"]}
    assert "universe" in kinds


@pytest.mark.unit
def test_repair_dates_detects_previous_nonempty_then_empty(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    _partition(root, "20261014", pd.DataFrame({"symbol": ["A"], "x": [1.0]}))
    _partition(root, "20261015", pd.DataFrame({"symbol": ["A"], "x": [1.0]}))
    _partition(root, "20261016", pd.DataFrame({"symbol": ["A"], "x": [None]}))

    dates = probe.repair_dates(
        root,
        ["20261014", "20261015", "20261016"],
        target="20261016",
        required=["x"],
        window=3,
    )

    assert dates == ["20261016"]


@pytest.mark.unit
def test_repair_dates_ignores_columns_empty_on_both_days(tmp_path: Path) -> None:
    # 结构性全空列不触发回补，否则每天都会重复重算
    root = tmp_path / "lib"
    for dt in ("20261015", "20261016"):
        _partition(root, dt, pd.DataFrame({"symbol": ["A"], "x": [None]}))

    dates = probe.repair_dates(
        root,
        ["20261015", "20261016"],
        target="20261016",
        required=["x"],
        window=2,
    )

    assert dates == []


@pytest.mark.unit
def test_coverage_reports_empty_absent_and_missing_partition(
    tmp_path: Path,
) -> None:
    root = tmp_path / "lib"
    _partition(
        root,
        "20261015",
        pd.DataFrame({"symbol": ["A"], "x": [1.0], "y": [None]}),
    )

    report = probe.coverage(root, ["20261015", "20261016"], ["x", "y", "z"])

    assert report["ok"] is False
    assert report["gaps"]["20261015"]["all_empty"] == ["y"]
    assert report["gaps"]["20261015"]["absent"] == ["z"]
    assert report["gaps"]["20261016"]["partition"] == ""
