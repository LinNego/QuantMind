"""submit_crosslib_training 的内部调用认证分支（无 admin 口令时的提交路径）。

覆盖三条约束：
- 缺 ``INTERNAL_CALL_SECRET`` 一律 fail-closed，绝不回落到公开默认值；
- 缺代表用户（模型产物按用户目录落盘）一律 fail-closed；
- 正常路径产出三枚身份头，tenant 缺省 ``default``。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "submit_crosslib_training.py"
)


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "quantmind_submit_crosslib_training", _SCRIPT_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载提交脚本：{_SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def script() -> ModuleType:
    return _load_script()


def test_missing_secret_fails_closed(
    script: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    monkeypatch.delenv("INTERNAL_CALL_SECRET", raising=False)

    # Act / Assert
    with pytest.raises(SystemExit):
        script._build_internal_headers("10000001", "default")


def test_blank_secret_fails_closed(
    script: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    monkeypatch.setenv("INTERNAL_CALL_SECRET", "   ")

    # Act / Assert
    with pytest.raises(SystemExit):
        script._build_internal_headers("10000001", "default")


def test_missing_user_id_fails_closed(
    script: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    monkeypatch.setenv("INTERNAL_CALL_SECRET", "s3cret")

    # Act / Assert
    with pytest.raises(SystemExit):
        script._build_internal_headers("", "default")


def test_builds_three_headers_with_default_tenant(
    script: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    monkeypatch.setenv("INTERNAL_CALL_SECRET", "s3cret")

    # Act
    headers = script._build_internal_headers("10000001", "")

    # Assert
    assert headers == {
        "X-Internal-Call": "s3cret",
        "X-User-Id": "10000001",
        "X-Tenant-Id": "default",
    }


def test_keeps_explicit_tenant(
    script: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    monkeypatch.setenv("INTERNAL_CALL_SECRET", "s3cret")

    # Act
    headers = script._build_internal_headers("10000001", "tenant-b")

    # Assert
    assert headers["X-Tenant-Id"] == "tenant-b"
