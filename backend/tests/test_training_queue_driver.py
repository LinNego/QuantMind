"""训练队列驱动的认证分支：无口令时必须能走内部调用密钥。"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import urllib.request
from pathlib import Path
from types import ModuleType

import pytest

_DRIVER_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "training" / "queue_driver.py"
)


def _load_driver() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "quantmind_training_queue_driver", _DRIVER_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载队列驱动：{_DRIVER_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def driver_module() -> ModuleType:
    return _load_driver()


def _driver(driver_module: ModuleType, tmp_path: Path, **kwargs):
    return driver_module.QueueDriver(
        tmp_path / "manifest.json",
        tmp_path,
        None,
        **kwargs,
    )


@pytest.mark.unit
def test_internal_secret_builds_headers_without_password(
    driver_module: ModuleType, tmp_path: Path
) -> None:
    driver = _driver(
        driver_module,
        tmp_path,
        internal_secret="s3cret",
        internal_user_id="10000001",
        internal_tenant_id="default",
    )

    assert driver._internal_headers == {
        "X-Internal-Call": "s3cret",
        "X-User-Id": "10000001",
        "X-Tenant-Id": "default",
    }


@pytest.mark.unit
def test_http_json_sends_internal_headers_and_no_bearer(
    driver_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _driver(driver_module, tmp_path, internal_secret="s3cret")
    captured: dict[str, object] = {}

    class _Response(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def _fake_urlopen(request: urllib.request.Request, timeout: float):
        captured["request"] = request
        captured["timeout"] = timeout
        return _Response(json.dumps({"ok": True}).encode("utf-8"))

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    status, body = driver._http_json("http://example.invalid/api", body={"a": 1})

    assert (status, body) == (200, {"ok": True})
    request = captured["request"]
    headers = {key.lower(): value for key, value in request.header_items()}
    assert headers["x-internal-call"] == "s3cret"
    assert headers["x-user-id"] == "10000001"
    assert headers["x-tenant-id"] == "default"
    assert "authorization" not in headers


@pytest.mark.unit
def test_submit_skips_login_when_internal_auth_is_configured(
    driver_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _driver(driver_module, tmp_path, internal_secret="s3cret")

    def _unexpected_login(password: str) -> str:
        raise AssertionError("内部认证模式不应调用登录接口")

    monkeypatch.setattr(driver, "_login", _unexpected_login)
    monkeypatch.setattr(
        driver,
        "_http_json",
        lambda *args, **kwargs: (202, {"runId": "train_test"}),
    )

    assert driver._submit({"job_name": "x"}, "") == "train_test"


@pytest.mark.unit
def test_main_requires_any_credential(
    driver_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("QM_PASSWORD", raising=False)
    monkeypatch.delenv("QM_INTERNAL_CALL_SECRET", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "queue_driver.py",
            "--manifest",
            str(tmp_path / "manifest.json"),
            "--state-dir",
            str(tmp_path),
        ],
    )

    with pytest.raises(SystemExit, match="未提供认证"):
        driver_module.main()
