"""编排器判定：日期归一、步骤终态（生产者失败/探针未过 → failed）。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from backend.scripts.materialize_library_tail import (
    WARMUP_MARGIN_DAYS,
    Context,
    _normalize_dt_text,
    _step_status,
    _worst_status,
    producer_env,
    rd_mined_target_ready,
    split_long_factors,
)


def _context(tmp_path: Path) -> Context:
    return Context(
        repo=tmp_path,
        quantdb=tmp_path / "quantdb",
        custom=tmp_path / "quantcustom",
        target="20261016",
        since=None,
        dry_run=False,
        keep_staging=False,
        python=sys.executable,
        docker="docker",
        container="quantmind",
        timeout_s=60,
        repair_window=10,
        l1_workers=1,
        bank_size=10,
        f1300_script=tmp_path / "f1300.py",
    )


@pytest.mark.unit
def test_producer_env_puts_repo_on_pythonpath(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 直接以脚本路径执行 build_production.py 时 sys.path[0] 是脚本目录，
    # 必须由外层把仓库根注入 PYTHONPATH，否则 import backend 必崩
    monkeypatch.delenv("PYTHONPATH", raising=False)

    env = producer_env(_context(tmp_path))

    assert env["PYTHONPATH"] == str(tmp_path)
    assert env["QM_QUANTDB_DATA_DIR"] == str(tmp_path / "quantdb")
    assert env["QM_QUANTCUSTOM_DATA_DIR"] == str(tmp_path / "quantcustom")


@pytest.mark.unit
def test_producer_env_prepends_repo_to_existing_pythonpath(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYTHONPATH", "/opt/existing")

    env = producer_env(_context(tmp_path))

    assert env["PYTHONPATH"] == f"{tmp_path}{os.pathsep}/opt/existing"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("20261016", "20261016"),
        ("2026-10-16", "20261016"),
        ("2026/10/16", "20261016"),
        ("2026.10.16", "20261016"),
        (None, None),
    ],
)
def test_normalize_dt_text(raw: str | None, expected: str | None) -> None:
    assert _normalize_dt_text(raw) == expected


@pytest.mark.unit
def test_normalize_dt_text_rejects_short_date() -> None:
    with pytest.raises(SystemExit, match="YYYYMMDD"):
        _normalize_dt_text("2026-1-6")


@pytest.mark.unit
def test_step_status_failed_when_producer_fails() -> None:
    assert _step_status({"returncode": 1}, {}) == "failed"


@pytest.mark.unit
def test_step_status_failed_when_probe_rejects() -> None:
    # 探针判预热不足：即使生产者 rc=0 也必须判失败（且调用方不合并）
    assert _step_status({"returncode": 0}, {"missing": []}, {"ok": False}) == "failed"


@pytest.mark.unit
def test_step_status_partial_when_contract_factor_failed() -> None:
    manifest = {"contract_failures": ["MG2_BALCHG_3M"]}
    assert _step_status({"returncode": 0}, {"missing": []}, None, manifest) == "partial"


@pytest.mark.unit
def test_step_status_partial_when_universe_short() -> None:
    merged = {"universe_short": [{"dt": "20261016", "symbols": 100}]}
    assert _step_status({"returncode": 0}, merged) == "partial"


@pytest.mark.unit
def test_step_status_skipped_in_dry_run() -> None:
    assert _step_status({"dry_run": True}, {}) == "skipped"


@pytest.mark.unit
def test_split_long_factors_puts_over_cap_into_deferred() -> None:
    windows = {"fast": 300, "long": 1008, "deep": 2420}

    daily, deferred, long_warmup = split_long_factors(windows, 907, required=[])

    assert daily == ["long"]
    assert deferred == ["deep"]
    assert long_warmup == 1008 + WARMUP_MARGIN_DAYS


@pytest.mark.unit
def test_split_long_factors_never_defers_contract_columns() -> None:
    # 契约列推理要读：即使超过日更上限也必须当晚上，内存风险由告警/日志暴露
    windows = {"deep": 2420}

    daily, deferred, long_warmup = split_long_factors(
        windows, 907, required=["deep"], cap=1300
    )

    assert daily == ["deep"]
    assert deferred == []
    assert long_warmup == 2420 + WARMUP_MARGIN_DAYS


@pytest.mark.unit
def test_split_long_factors_without_long_factors_keeps_fast_warmup() -> None:
    assert split_long_factors({"fast": 300}, 907, required=[]) == ([], [], 907)


@pytest.mark.unit
def test_worst_status_orders_failed_over_partial() -> None:
    assert _worst_status(["ok", "partial"]) == "partial"
    assert _worst_status(["partial", "failed"]) == "failed"
    assert _worst_status(["ok", "skipped"]) == "ok"
    assert _worst_status([]) == "skipped"


@pytest.mark.unit
def test_rd_mined_target_ready_requires_nonempty_partition(tmp_path: Path) -> None:
    partition = tmp_path / "6_ml_datasets" / "rd_mined" / "dt=20261019" / "data.parquet"
    assert rd_mined_target_ready(tmp_path, "20261019") is False

    partition.parent.mkdir(parents=True)
    partition.write_bytes(b"")
    assert rd_mined_target_ready(tmp_path, "20261019") is False

    partition.write_bytes(b"parquet")
    assert rd_mined_target_ready(tmp_path, "20261019") is True
