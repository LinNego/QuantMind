"""编排器判定：日期归一、步骤终态（生产者失败/探针未过 → failed）。"""

from __future__ import annotations

import pytest

from backend.scripts.materialize_library_tail import _normalize_dt_text, _step_status


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
