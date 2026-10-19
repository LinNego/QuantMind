"""列级合并：只动列、不动行；staging 覆盖同名列且保留正式库标的集。"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from backend.scripts.factor_materialize.merge import (
    backup_partition,
    merge_day_columns,
    template_partition,
)

DT = "20261016"


def _write(root: Path, frame: pd.DataFrame, dt: str = DT) -> Path:
    target = root / f"dt={dt}" / "data.parquet"
    target.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(target, index=False)
    return target


def _read(root: Path, dt: str = DT) -> pd.DataFrame:
    return pd.read_parquet(root / f"dt={dt}" / "data.parquet").set_index("symbol")


@pytest.mark.unit
def test_merge_adds_columns_and_overwrites_existing(tmp_path: Path) -> None:
    target = tmp_path / "target"
    staging = tmp_path / "staging"
    _write(
        target,
        pd.DataFrame({"symbol": ["A", "B"], "time": ["t", "t"], "x": [1.0, 2.0]}),
    )
    _write(
        staging,
        pd.DataFrame(
            {
                "symbol": ["A", "B", "C"],
                "time": ["t", "t", "t"],
                "x": [10.0, 20.0, 30.0],
                "y": [0.1, 0.2, 0.3],
            }
        ),
    )

    stats = merge_day_columns(staging, target, DT)

    merged = _read(target)
    assert list(merged.index) == ["A", "B"]
    assert merged["x"].tolist() == [10.0, 20.0]
    assert merged["y"].tolist() == [0.1, 0.2]
    assert stats["added_columns"] == 1
    assert stats["replaced_columns"] == 1
    assert stats["incoming_symbols_missing_in_target"] == 1


@pytest.mark.unit
def test_merge_column_subset_leaves_other_columns_untouched(tmp_path: Path) -> None:
    # Arrange：正式库 x 是旧值，staging 的 x 是新值、y 是修复列
    target = tmp_path / "target"
    staging = tmp_path / "staging"
    _write(target, pd.DataFrame({"symbol": ["A"], "time": ["t"], "x": [1.0]}))
    _write(
        staging,
        pd.DataFrame({"symbol": ["A"], "time": ["t"], "x": [9.0], "y": [2.0]}),
    )

    # Act：只合并 y
    stats = merge_day_columns(staging, target, DT, columns=["y"])

    # Assert
    merged = _read(target)
    assert merged.loc["A", "x"] == pytest.approx(1.0)
    assert merged.loc["A", "y"] == pytest.approx(2.0)
    assert stats["added_columns"] == 1
    assert stats["replaced_columns"] == 0


@pytest.mark.unit
def test_merge_column_subset_rejects_unknown_or_metadata_column(tmp_path: Path) -> None:
    target = tmp_path / "target"
    staging = tmp_path / "staging"
    _write(target, pd.DataFrame({"symbol": ["A"], "time": ["t"], "x": [1.0]}))
    _write(staging, pd.DataFrame({"symbol": ["A"], "time": ["t"], "x": [9.0]}))

    with pytest.raises(ValueError, match="staging 分区缺列"):
        merge_day_columns(staging, target, DT, columns=["nope"])
    with pytest.raises(ValueError, match="元数据列不可合并"):
        merge_day_columns(staging, target, DT, columns=["symbol"])


@pytest.mark.unit
def test_merge_creates_missing_day_from_staging(tmp_path: Path) -> None:
    target = tmp_path / "target"
    staging = tmp_path / "staging"
    _write(
        staging,
        pd.DataFrame({"symbol": ["A"], "time": ["t"], "x": [1.0]}),
    )

    merge_day_columns(staging, target, DT)

    merged = _read(target)
    assert merged.loc["A", "x"] == pytest.approx(1.0)


@pytest.mark.unit
def test_merge_rejects_missing_staging_partition(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="staging"):
        merge_day_columns(tmp_path / "staging", tmp_path / "target", DT)


@pytest.mark.unit
def test_merge_creates_missing_day_from_template_schema(tmp_path: Path) -> None:
    # Arrange：官方库已有前一交易日分区（列为 symbol/time/a/b），staging 只有 a
    target = tmp_path / "target"
    staging = tmp_path / "staging"
    _write(
        target,
        pd.DataFrame(
            {
                "symbol": ["A", "B"],
                "time": pd.to_datetime(["2026-10-15"] * 2),
                "a": [1.0, 2.0],
                "b": [3.0, 4.0],
            }
        ),
        dt="20261015",
    )
    _write(staging, pd.DataFrame({"symbol": ["B"], "time": [None], "a": [9.0]}))

    # Act：新交易日按模板建整宽分区，行集用当日全市场
    stats = merge_day_columns(
        staging, target, DT, template_root=target, symbols=["A", "B", "C"]
    )

    # Assert
    created = pd.read_parquet(target / f"dt={DT}" / "data.parquet")
    assert list(created.columns) == ["symbol", "time", "a", "b"]
    assert created["symbol"].tolist() == ["A", "B", "C"]
    assert created["time"].tolist() == [pd.Timestamp("2026-10-16")] * 3
    indexed = created.set_index("symbol")
    assert pd.isna(indexed.loc["A", "a"])
    assert indexed.loc["B", "a"] == pytest.approx(9.0)
    assert pd.isna(indexed.loc["C", "a"])
    assert created["b"].isna().all()
    assert stats["created"] is True
    assert stats["template"] is not None


@pytest.mark.unit
def test_merge_template_keeps_partition_full_width_for_subset_run(
    tmp_path: Path,
) -> None:
    # 只补必需列的回退运行：staging 只带 x，也不许建出窄分区
    target = tmp_path / "target"
    staging = tmp_path / "staging"
    _write(
        target,
        pd.DataFrame(
            {
                "symbol": ["A"],
                "time": pd.to_datetime(["2026-10-15"]),
                "x": [1.0],
                "y": [2.0],
            }
        ),
        dt="20261015",
    )
    _write(staging, pd.DataFrame({"symbol": ["A"], "x": [7.0], "z": [8.0]}))

    merge_day_columns(
        staging, target, DT, columns=["x"], template_root=target, symbols=["A"]
    )

    created = pd.read_parquet(target / f"dt={DT}" / "data.parquet")
    # 列子集模式只认请求列：staging 的 z 不并入（与已存在分区分支同一语义）
    assert list(created.columns) == ["symbol", "time", "x", "y"]
    assert created.loc[0, "x"] == pytest.approx(7.0)
    assert pd.isna(created.loc[0, "y"])


@pytest.mark.unit
def test_merge_template_falls_back_to_earliest_partition(tmp_path: Path) -> None:
    target = tmp_path / "target"
    staging = tmp_path / "staging"
    _write(
        target,
        pd.DataFrame(
            {"symbol": ["A"], "time": pd.to_datetime(["2026-10-17"]), "x": [1.0]}
        ),
        dt="20261017",
    )
    _write(staging, pd.DataFrame({"symbol": ["A"], "x": [3.0]}))

    merge_day_columns(staging, target, DT, template_root=target, symbols=["A"])

    created = pd.read_parquet(target / f"dt={DT}" / "data.parquet")
    assert list(created.columns) == ["symbol", "time", "x"]
    assert created.loc[0, "x"] == pytest.approx(3.0)


@pytest.mark.unit
def test_merge_skip_empty_columns_keeps_existing_values(tmp_path: Path) -> None:
    # Arrange：尾窗补数里长窗列 x 全空，正式库 x 有存量值
    target = tmp_path / "target"
    staging = tmp_path / "staging"
    _write(
        target,
        pd.DataFrame(
            {"symbol": ["A", "B"], "time": ["t", "t"], "x": [1.0, 2.0], "y": [3.0, 4.0]}
        ),
    )
    _write(
        staging,
        pd.DataFrame(
            {
                "symbol": ["A", "B"],
                "time": ["t", "t"],
                "x": [None, None],
                "y": [None, 9.0],
            }
        ),
    )

    # Act
    stats = merge_day_columns(staging, target, DT, skip_empty_columns=True)

    # Assert
    merged = _read(target)
    assert merged["x"].tolist() == [1.0, 2.0]
    assert pd.isna(merged.loc["A", "y"])
    assert merged.loc["B", "y"] == pytest.approx(9.0)
    assert stats["skipped_empty_columns"] == ["x"]
    assert stats["replaced_columns"] == 1


@pytest.mark.unit
def test_merge_without_skip_flag_overwrites_with_empty_column(tmp_path: Path) -> None:
    # 默认语义不变：整列 NaN 的 staging 列照常覆盖（显式重算/修复用）
    target = tmp_path / "target"
    staging = tmp_path / "staging"
    _write(target, pd.DataFrame({"symbol": ["A"], "time": ["t"], "x": [1.0]}))
    _write(staging, pd.DataFrame({"symbol": ["A"], "time": ["t"], "x": [None]}))

    merge_day_columns(staging, target, DT)

    merged = _read(target)
    assert pd.isna(merged.loc["A", "x"])


@pytest.mark.unit
def test_merge_skip_empty_columns_does_not_create_all_nan_new_column(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    staging = tmp_path / "staging"
    _write(target, pd.DataFrame({"symbol": ["A"], "time": ["t"], "x": [1.0]}))
    _write(
        staging,
        pd.DataFrame({"symbol": ["A"], "time": ["t"], "x": [5.0], "z": [None]}),
    )

    merge_day_columns(staging, target, DT, skip_empty_columns=True)

    merged = _read(target)
    assert merged.loc["A", "x"] == pytest.approx(5.0)
    assert "z" not in merged.columns


@pytest.mark.unit
def test_merge_is_atomic_and_leaves_no_tmp_file(tmp_path: Path) -> None:
    target = tmp_path / "target"
    staging = tmp_path / "staging"
    _write(target, pd.DataFrame({"symbol": ["A"], "time": ["t"], "x": [1.0]}))
    _write(staging, pd.DataFrame({"symbol": ["A"], "time": ["t"], "y": [2.0]}))

    merge_day_columns(staging, target, DT)

    day_dir = target / f"dt={DT}"
    assert (day_dir / "data.parquet").is_file()
    # 读侧按 dt=*/*.parquet glob：临时文件绝不能再以 .parquet 结尾
    assert [p.name for p in day_dir.glob("*.parquet")] == ["data.parquet"]
    assert not list(day_dir.glob("*.tmp"))


@pytest.mark.unit
def test_merge_partial_universe_preserves_official_values(tmp_path: Path) -> None:
    # Arrange：staging 只覆盖 A（单日补数/单批 --only），正式库 A、B 都有值
    target = tmp_path / "target"
    staging = tmp_path / "staging"
    _write(
        target,
        pd.DataFrame({"symbol": ["A", "B"], "time": ["t", "t"], "x": [1.0, 2.0]}),
    )
    _write(staging, pd.DataFrame({"symbol": ["A"], "time": ["t"], "x": [5.0]}))

    # Act
    stats = merge_day_columns(staging, target, DT)

    # Assert：未覆盖标的保留旧值，绝不被 reindex 抹成 NaN
    merged = _read(target)
    assert merged["x"].tolist() == [5.0, 2.0]
    assert stats["preserved_official_symbols"] == 1


@pytest.mark.unit
def test_merge_partial_universe_keeps_integer_dtype(tmp_path: Path) -> None:
    target = tmp_path / "target"
    staging = tmp_path / "staging"
    _write(
        target,
        pd.DataFrame(
            {
                "symbol": ["A", "B"],
                "time": ["t", "t"],
                "n": pd.Series([1, 2], dtype="int64"),
            }
        ),
    )
    _write(
        staging,
        pd.DataFrame(
            {"symbol": ["A"], "time": ["t"], "n": pd.Series([5], dtype="int64")}
        ),
    )

    merge_day_columns(staging, target, DT)

    merged = _read(target)
    assert merged["n"].tolist() == [5, 2]
    assert str(merged["n"].dtype) == "int64"


@pytest.mark.unit
def test_template_partition_skips_stale_empty_directory(tmp_path: Path) -> None:
    # Arrange：最新目录是 SIGKILL 残骸（无 data.parquet）
    root = tmp_path / "lib"
    _write(
        root,
        pd.DataFrame({"symbol": ["A"], "time": pd.to_datetime(["2026-10-14"])}),
        dt="20261014",
    )
    (root / "dt=20261015").mkdir()

    # Act
    chosen = template_partition(root, DT)

    # Assert：退回上一份有效分区，而不是 None（否则新日会建出窄 schema）
    assert chosen == root / "dt=20261014" / "data.parquet"


@pytest.mark.unit
def test_template_frame_integer_column_with_missing_symbols(tmp_path: Path) -> None:
    target = tmp_path / "target"
    staging = tmp_path / "staging"
    _write(
        target,
        pd.DataFrame(
            {
                "symbol": ["A"],
                "time": pd.to_datetime(["2026-10-15"]),
                "n": pd.Series([1], dtype="int64"),
            }
        ),
        dt="20261015",
    )
    _write(staging, pd.DataFrame({"symbol": ["B"], "n": [9]}))

    # Act：B 有值、A 无值；整型模板列 + NaN 走可空整型，不能抛 ArrowInvalid
    merge_day_columns(staging, target, DT, template_root=target, symbols=["A", "B"])

    # Assert
    created = pd.read_parquet(target / f"dt={DT}" / "data.parquet").set_index("symbol")
    assert pd.isna(created.loc["A", "n"])
    assert created.loc["B", "n"] == 9


@pytest.mark.unit
def test_backup_partition_keeps_first_snapshot(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    backup_root = tmp_path / "backup"
    _write(root, pd.DataFrame({"symbol": ["A"], "time": ["t"], "x": [1.0]}))

    first = backup_partition(root, DT, backup_root)
    _write(root, pd.DataFrame({"symbol": ["A"], "time": ["t"], "x": [2.0]}))
    second = backup_partition(root, DT, backup_root)

    assert first == second
    snapshot = pd.read_parquet(backup_root / f"dt={DT}" / "data.parquet")
    assert snapshot["x"].tolist() == [1.0]
