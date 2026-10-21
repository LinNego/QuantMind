"""截面选股历史统计 API 路由测试（服务替身，不碰数据库/行情）。"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.services.api.routers import model_selection_stats as ms
from backend.services.api.routers.model_training import router as model_training_router
from backend.services.api.user_app.middleware.auth import get_current_user
from backend.services.engine.services.model_selection_stats_service import (
    SelectionStatsError,
)

BASE = "/api/v1/models/m1/selection-stats"
USER = {"tenant_id": "default", "user_id": "10000001", "sub": "10000001"}


class _FakeService:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.error: Exception | None = None
        self.payload: dict = {
            "model_id": "m1",
            "model_name": "测试模型",
            "market": "CN",
            "split": "test",
            "requested": {},
            "effective": {},
            "params": {},
            "summary": {"total_return_pct": 12.3},
            "months": [],
            "cohorts": [],
            "points": [],
            "warnings": [],
            "quality": {},
        }

    async def compute(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.payload


def _client(
    monkeypatch, service: _FakeService, *, authenticated: bool = True
) -> TestClient:
    monkeypatch.setattr(ms, "ModelSelectionStatsService", lambda: service)
    app = FastAPI()
    app.include_router(ms.router)
    # 同时挂载带 GET /{model_id} 兜底的真实路由，验证两段路径不被遮蔽
    app.include_router(model_training_router, prefix="/api/v1/models")
    if authenticated:
        app.dependency_overrides[get_current_user] = lambda: dict(USER)
    return TestClient(app)


def test_requires_authentication(monkeypatch):
    service = _FakeService()
    client = _client(monkeypatch, service, authenticated=False)
    resp = client.get(BASE)
    assert resp.status_code in (401, 403)
    assert service.calls == []


def test_passes_owner_and_params_to_service(monkeypatch):
    service = _FakeService()
    client = _client(monkeypatch, service)

    resp = client.get(
        BASE,
        params={
            "start": "2026-01-05",
            "end": "2026-03-31",
            "top_k": 20,
            "hold_days": 5,
            "capital": 30000000,
        },
    )

    assert resp.status_code == 200
    assert resp.json()["summary"]["total_return_pct"] == pytest.approx(12.3)
    call = service.calls[0]
    assert call["tenant_id"] == "default"
    assert call["user_id"] == "10000001"
    assert call["model_id"] == "m1"
    assert call["start"] == date(2026, 1, 5)
    assert call["end"] == date(2026, 3, 31)
    assert call["top_k"] == 20
    assert call["hold_days"] == 5
    assert call["capital"] == pytest.approx(30_000_000)


def test_defaults_leave_hold_days_to_model(monkeypatch):
    service = _FakeService()
    client = _client(monkeypatch, service)
    resp = client.get(BASE)
    assert resp.status_code == 200
    call = service.calls[0]
    assert call["top_k"] == 50
    assert call["hold_days"] is None
    assert call["start"] is None
    assert call["end"] is None
    assert call["capital"] == pytest.approx(10_000_000)


@pytest.mark.parametrize(
    "params",
    [
        {"top_k": 0},
        {"top_k": 51},
        {"hold_days": 0},
        {"hold_days": 61},
        {"capital": 0},
        {"capital": -1},
    ],
)
def test_query_bounds_reject_before_service(monkeypatch, params):
    service = _FakeService()
    client = _client(monkeypatch, service)
    resp = client.get(BASE, params=params)
    assert resp.status_code == 422
    assert service.calls == []


def test_error_maps_to_code_and_message(monkeypatch):
    service = _FakeService()
    service.error = SelectionStatsError(
        "pred_unavailable", "模型未找到 pred.parquet", status_code=422
    )
    client = _client(monkeypatch, service)
    resp = client.get(BASE)
    assert resp.status_code == 422
    assert resp.json()["detail"] == {
        "code": "pred_unavailable",
        "message": "模型未找到 pred.parquet",
    }


def test_not_shadowed_by_model_training_catchall(monkeypatch):
    """真实 model_training 路由同挂时，两段路径仍命中本路由。"""
    service = _FakeService()
    client = _client(monkeypatch, service)
    resp = client.get(BASE)
    assert resp.status_code == 200
    assert len(service.calls) == 1
