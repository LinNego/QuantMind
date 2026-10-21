"""组合选股 API（``/api/v1/models/blends``）路由测试。

锁死四件事：

- JWT 鉴权：无凭证 401；归属人只从 ``current_user`` 取，不读请求体；
- 错误映射：``BlendError`` → ``detail={code,message}``，名称冲突 → 409；
- 参数透传：日期解析、权重、persist、annotate 与台账归属；
- 路由顺序：``/api/v1/models/blends`` 不得被 ``model_training`` 的
  ``GET /{model_id}`` 兜底遮蔽（真实 app 上打一次请求）。

服务与持久化全部替身化——本文件只测 HTTP 边界，不碰数据库。
"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.services.api.routers import model_blends as mb
from backend.services.api.user_app.middleware.auth import get_current_user
from backend.services.engine.services.model_blend_service import BlendError

BASE = "/api/v1/models/blends"
USER = {"tenant_id": "default", "user_id": "10000001", "sub": "10000001"}


def _last_plan_call(service: _FakeService) -> dict:
    """计划端点之后还会调 annotate_confirmable，取最后一次 plan 调用的参数。"""
    return [kwargs for name, kwargs in service.calls if name == "plan"][-1]


class _FakeService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.errors: dict[str, Exception] = {}
        self.ranking_payload: dict = {"ranking": [], "members": [], "warnings": []}
        self.plan_payload: dict = {"plan_id": None, "entries": [], "warnings": []}

    def _record(self, action: str, **kwargs):
        self.calls.append((action, kwargs))
        if action in self.errors:
            raise self.errors[action]

    async def eligible(self, **kwargs):
        self._record("eligible", **kwargs)
        return {"groups": [], "excluded": [], "total_eligible": 0}

    async def auto_candidates(self, **kwargs):
        self._record("auto_candidates", **kwargs)
        return {"items": [], "warnings": []}

    async def preview(self, **kwargs):
        self._record("preview", **kwargs)
        return {"ranking": [], "warnings": [], "data_trade_date": "2026-10-19"}

    async def create_blend(self, **kwargs):
        self._record("create_blend", **kwargs)
        return {"blend_id": "bl_new", **kwargs}

    async def update_blend(self, blend, **kwargs):
        self._record("update_blend", blend=blend, **kwargs)
        return blend

    async def members_from_blend(self, blend):
        self._record("members_from_blend", blend=blend)
        return [{"model_id": "m1"}, {"model_id": "m2"}]

    async def latest_common_date(self, **kwargs):
        self._record("latest_common_date", **kwargs)
        return self.dates

    dates = None

    async def ranking(self, **kwargs):
        self._record("ranking", **kwargs)
        return dict(self.ranking_payload)

    async def available_dates(self, blend, **kwargs):
        self._record("available_dates", blend=blend, **kwargs)
        return ["2026-10-19", "2026-10-16"]

    async def rolling(self, blend, **kwargs):
        self._record("rolling", blend=blend, **kwargs)
        return {
            "blend_id": "bl_x",
            "columns": [],
            "days": [],
            "summary": {"unique_symbols": 0, "new_today": 0, "exited_today": 0},
        }

    async def period(self, blend, **kwargs):
        self._record("period", blend=blend, **kwargs)
        return {
            "blend_id": "bl_x",
            "requested": {},
            "effective": {},
            "summary": {"total_return_pct": 0.0},
            "months": [],
            "daily": [],
            "trades": [],
            "open": [],
            "warnings": [],
        }

    async def plan(self, blend, **kwargs):
        self._record("plan", blend=blend, **kwargs)
        return dict(self.plan_payload)

    async def annotate_confirmable(self, plans):
        self._record("annotate_confirmable")
        for plan in plans:
            if plan is None:
                continue
            plan["can_confirm"] = bool(plan.get("plan_id")) and not plan.get(
                "confirmed_at"
            )

    async def confirm_plan(self, blend, plan, **kwargs):
        self._record("confirm_plan", blend=blend, plan=plan, **kwargs)
        return {"plan_id": plan["plan_id"], "confirmed_at": "2026-10-19T08:00:00Z"}


class _FakePersistence:
    def __init__(self) -> None:
        self.blends: dict[str, dict] = {}
        self.plans: dict[str, dict] = {}
        self.plans_by_date: dict[str, dict] = {}
        self.archived: list[str] = []

    async def list_blends(self, **kwargs):
        return list(self.blends.values())

    async def get_blend(self, blend_id, *, tenant_id, user_id):
        return self.blends.get(blend_id)

    async def archive_blend(self, blend_id, *, tenant_id, user_id):
        self.archived.append(blend_id)
        self.blends.get(blend_id, {})["status"] = "archived"

    async def list_plans(self, blend_id, *, limit=20):
        return [p for p in self.plans.values() if p["blend_id"] == blend_id][:limit]

    async def get_plan(self, plan_id, *, tenant_id, user_id):
        return self.plans.get(plan_id)

    async def get_plan_by_date(self, blend_id, *, data_trade_date):
        return self.plans_by_date.get(blend_id)


def _blend(blend_id: str = "bl_x", **overrides) -> dict:
    payload = {
        "blend_id": blend_id,
        "tenant_id": "default",
        "user_id": "10000001",
        "name": "13 模型等权",
        "status": "active",
        "weighting": "equal",
        "method": "mean",
        "min_coverage": 8,
        "trim_count": 2,
        "consensus_min_count": 0,
        "members": [
            {"model_id": "m1", "model_source": "user", "weight": 1.0},
            {"model_id": "m2", "model_source": "user", "weight": 1.0},
        ],
    }
    payload.update(overrides)
    return payload


@pytest.fixture()
def api(monkeypatch):
    service = _FakeService()
    persistence = _FakePersistence()
    monkeypatch.setattr(mb, "ModelBlendService", lambda: service)
    monkeypatch.setattr(mb, "ModelBlendPersistence", lambda: persistence)
    app = FastAPI()
    app.include_router(mb.router)
    app.dependency_overrides[get_current_user] = lambda: dict(USER)
    return TestClient(app), service, persistence


def test_endpoints_require_auth(monkeypatch):
    app = FastAPI()
    app.include_router(mb.router)
    client = TestClient(app)
    for method, path in [
        ("get", BASE),
        ("get", f"{BASE}/eligible"),
        ("post", f"{BASE}/preview"),
    ]:
        resp = getattr(client, method)(path)
        assert resp.status_code in {401, 403}, (method, path, resp.text)


def test_owner_rejects_missing_user(monkeypatch):
    app = FastAPI()
    app.include_router(mb.router)
    app.dependency_overrides[get_current_user] = lambda: {"tenant_id": "default"}
    resp = TestClient(app).get(BASE)
    assert resp.status_code == 401


def test_list_blends_returns_items_envelope(api):
    client, _service, persistence = api
    persistence.blends["bl_x"] = _blend()
    resp = client.get(BASE)
    assert resp.status_code == 200
    assert resp.json() == {"items": [_blend()]}


def test_eligible_and_auto_candidates_pass_market(api):
    client, service, _ = api
    assert client.get(f"{BASE}/eligible", params={"market": "CN"}).status_code == 200
    assert (
        client.get(f"{BASE}/auto-candidates", params={"market": "CN"}).status_code
        == 200
    )
    assert service.calls[0][0] == "eligible"
    assert service.calls[0][1]["market"] == "CN"
    assert service.calls[1][0] == "auto_candidates"


def test_preview_parses_date_and_passes_weights(api):
    client, service, _ = api
    resp = client.post(
        f"{BASE}/preview",
        json={
            "model_ids": ["m1", "m2"],
            "data_trade_date": "2026-10-19",
            "purpose": "next_day",
            "method": "trimmed",
            "trim_count": 1,
            "top_k": 30,
            "top": 40,
            "min_coverage": 2,
            "consensus_min_count": 1,
            "weights": {"m1": 2.0, "m2": 1.0},
        },
    )
    assert resp.status_code == 200
    name, kwargs = service.calls[0]
    assert name == "preview"
    assert kwargs["data_trade_date"].isoformat() == "2026-10-19"
    assert kwargs["purpose"] == "next_day"
    assert kwargs["method"] == "trimmed"
    assert kwargs["weights"] == {"m1": 2.0, "m2": 1.0}
    assert kwargs["top"] == 40


def test_preview_rejects_bad_date_and_single_member(api):
    client, _service, _ = api
    bad_date = client.post(
        f"{BASE}/preview",
        json={"model_ids": ["m1", "m2"], "data_trade_date": "2026/10/19"},
    )
    assert bad_date.status_code == 400
    # 极端年份会让交易日历兜底 day+1 溢出成 500，必须在入参处拦成 400
    for extreme in ("9999-12-31", "0001-01-01"):
        resp = client.post(
            f"{BASE}/preview",
            json={"model_ids": ["m1", "m2"], "data_trade_date": extreme},
        )
        assert resp.status_code == 400, extreme
    too_few = client.post(f"{BASE}/preview", json={"model_ids": ["m1"]})
    assert too_few.status_code == 422


def test_create_maps_business_error_and_unique_conflict(api):
    client, service, _ = api
    service.errors["create_blend"] = BlendError("签名不一致", code="signature_mismatch")
    resp = client.post(f"{BASE}", json={"name": "x", "model_ids": ["m1", "m2"]})
    assert resp.status_code == 400
    assert resp.json()["detail"] == {
        "code": "signature_mismatch",
        "message": "签名不一致",
    }

    service.errors["create_blend"] = RuntimeError(
        "duplicate key value violates unique constraint"
    )
    resp = client.post(f"{BASE}", json={"name": "x", "model_ids": ["m1", "m2"]})
    assert resp.status_code == 409
    assert "已存在" in resp.json()["detail"]


def test_patch_returns_refreshed_and_404s_unknown(api):
    client, service, persistence = api
    persistence.blends["bl_x"] = _blend()
    resp = client.patch(f"{BASE}/bl_x", json={"name": " 新名字 ", "min_coverage": 9})
    assert resp.status_code == 200
    name, kwargs = service.calls[0]
    assert name == "update_blend"
    # 名称规整属服务层（test_blend_service 已锁），路由只负责原样透传
    assert kwargs["fields"] == {"name": " 新名字 ", "min_coverage": 9}
    assert kwargs["model_ids"] is None
    assert resp.json()["blend_id"] == "bl_x"

    missing = client.patch(f"{BASE}/bl_missing", json={"name": "y"})
    assert missing.status_code == 404

    # 改名撞唯一约束与新建同口径 → 409，而不是未处理的 500
    service.errors["update_blend"] = Exception(
        'duplicate key value violates unique constraint "uq_blend_name"'
    )
    conflict = client.patch(f"{BASE}/bl_x", json={"name": "重复"})
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "组合名称已存在"


def test_delete_archives_and_404s_unknown(api):
    client, _service, persistence = api
    persistence.blends["bl_x"] = _blend()
    resp = client.delete(f"{BASE}/bl_x")
    assert resp.status_code == 200
    assert resp.json() == {"blend_id": "bl_x", "status": "archived"}
    assert persistence.archived == ["bl_x"]
    assert client.delete(f"{BASE}/bl_y").status_code == 404


def test_dates_returns_available_list(api):
    client, service, persistence = api
    persistence.blends["bl_x"] = _blend()
    resp = client.get(f"{BASE}/bl_x/dates", params={"limit": 5})
    assert resp.status_code == 200
    assert resp.json() == {
        "blend_id": "bl_x",
        "dates": ["2026-10-19", "2026-10-16"],
    }
    assert service.calls[0][1]["limit"] == 5


def test_ranking_defaults_to_latest_common_date_and_manual_weights(api):
    client, service, persistence = api
    persistence.blends["bl_x"] = _blend(
        weighting="manual",
        members=[
            {"model_id": "m1", "model_source": "user", "weight": 2.0},
            {"model_id": "m2", "model_source": "user", "weight": 0.5},
        ],
    )
    service.dates = date(2026, 10, 19)
    resp = client.get(f"{BASE}/bl_x/ranking", params={"top": 20})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["blend_id"] == "bl_x"
    name, kwargs = service.calls[-1]
    assert name == "ranking"
    assert kwargs["data_trade_date"] == service.dates
    assert kwargs["weights"] == {"m1": 2.0, "m2": 0.5}
    assert kwargs["annotate"] is True
    assert kwargs["top"] == 20


def test_ranking_explicit_date_skips_common_date_lookup(api):
    client, service, persistence = api
    persistence.blends["bl_x"] = _blend()
    service.dates = None
    resp = client.get(f"{BASE}/bl_x/ranking", params={"date": "2026-10-18"})
    assert resp.status_code == 200
    names = [name for name, _ in service.calls]
    assert "latest_common_date" not in names
    assert service.calls[-1][1]["data_trade_date"].isoformat() == "2026-10-18"


def test_ranking_404_when_no_common_date(api):
    client, service, persistence = api
    persistence.blends["bl_x"] = _blend()
    service.dates = None
    resp = client.get(f"{BASE}/bl_x/ranking")
    assert resp.status_code == 404


def test_rolling_passes_window_per_day_and_date(api):
    client, service, persistence = api
    persistence.blends["bl_x"] = _blend()
    resp = client.get(
        f"{BASE}/bl_x/rolling",
        params={"date": "2026-10-19", "window": 10, "per_day": 5},
    )
    assert resp.status_code == 200
    name, kwargs = service.calls[-1]
    assert name == "rolling"
    assert kwargs["end_date"] == date(2026, 10, 19)
    assert kwargs["window"] == 10
    assert kwargs["per_day"] == 5
    assert kwargs["blend"]["blend_id"] == "bl_x"


def test_rolling_defaults_and_bounds(api):
    client, service, persistence = api
    persistence.blends["bl_x"] = _blend()
    assert client.get(f"{BASE}/bl_x/rolling").status_code == 200
    _, kwargs = service.calls[-1]
    assert kwargs["end_date"] is None
    assert kwargs["window"] == 10
    assert kwargs["per_day"] == 5
    # 越界由 Query 约束在进服务前拦下
    assert client.get(f"{BASE}/bl_x/rolling", params={"window": 0}).status_code == 422
    assert client.get(f"{BASE}/bl_x/rolling", params={"per_day": 51}).status_code == 422


def test_period_passes_range_capital_positions_and_dates(api):
    client, service, persistence = api
    persistence.blends["bl_x"] = _blend()
    resp = client.get(
        f"{BASE}/bl_x/period",
        params={
            "start": "2026-08-01",
            "end": "2026-10-19",
            "capital": 5000000,
            "positions": 20,
        },
    )
    assert resp.status_code == 200
    name, kwargs = service.calls[-1]
    assert name == "period"
    assert kwargs["start_date"] == date(2026, 8, 1)
    assert kwargs["end_date"] == date(2026, 10, 19)
    assert kwargs["capital"] == 5000000
    assert kwargs["positions"] == 20
    assert kwargs["blend"]["blend_id"] == "bl_x"
    assert kwargs["tenant_id"] == "default"
    assert kwargs["user_id"] == "10000001"


def test_period_defaults_and_query_bounds(api):
    client, service, persistence = api
    persistence.blends["bl_x"] = _blend()
    assert client.get(f"{BASE}/bl_x/period").status_code == 200
    _, kwargs = service.calls[-1]
    assert kwargs["start_date"] is None
    assert kwargs["end_date"] is None
    assert kwargs["capital"] == 10_000_000.0
    assert kwargs["positions"] is None
    # 越界由 Query 在进服务前拦下；坏日期由 _parse_date 拦成 400
    for params in (
        {"positions": 0},
        {"positions": 51},
        {"capital": 0},
        {"capital": 1e13},
    ):
        assert client.get(f"{BASE}/bl_x/period", params=params).status_code == 422, (
            params
        )
    assert (
        client.get(f"{BASE}/bl_x/period", params={"start": "2026-13-01"}).status_code
        == 400
    )


def test_period_maps_business_error_to_detail_code(api):
    client, service, persistence = api
    persistence.blends["bl_x"] = _blend()
    service.errors["period"] = BlendError(
        "评分日不足", status_code=422, code="insufficient_scoring_days"
    )
    resp = client.get(f"{BASE}/bl_x/period")
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "insufficient_scoring_days"


def test_period_404s_unknown_blend(api):
    client, _service, _persistence = api
    assert client.get(f"{BASE}/bl_unknown/period").status_code == 404


def test_create_plan_without_overrides_leaves_config_to_service(api):
    client, service, persistence = api
    persistence.blends["bl_x"] = _blend()
    resp = client.post(f"{BASE}/bl_x/plans", json={})
    assert resp.status_code == 200
    assert _last_plan_call(service)["plan_config"] is None


def test_create_plan_overrides_merge_with_blend_defaults(api):
    client, service, persistence = api
    persistence.blends["bl_x"] = _blend(
        rolling_window=8, entry_per_day=3, target_positions=60
    )
    resp = client.post(
        f"{BASE}/bl_x/plans",
        json={"capital": 20000000.0, "top_k": 30},
    )
    assert resp.status_code == 200
    cfg = _last_plan_call(service)["plan_config"]
    assert cfg.capital == 20_000_000.0
    assert cfg.top_k == 30
    # 未显式给的字段沿用组合自身配置，不静默回默认
    assert cfg.hold_days == 8
    assert cfg.entry_per_day == 3
    assert cfg.target_positions == 60


def test_create_plan_rejects_out_of_range_overrides(api):
    client, _service, persistence = api
    persistence.blends["bl_x"] = _blend()
    for body in (
        {"top_k": 0},
        {"capital": 0},
        {"hold_days": 61},
        {"entry_per_day": 51},
    ):
        assert client.post(f"{BASE}/bl_x/plans", json=body).status_code == 422, body


def test_create_plan_passes_persist_flag(api):
    client, service, persistence = api
    persistence.blends["bl_x"] = _blend()
    resp = client.post(
        f"{BASE}/bl_x/plans",
        json={"data_trade_date": "2026-10-19", "persist": False},
    )
    assert resp.status_code == 200
    plan_calls = [(name, kwargs) for name, kwargs in service.calls if name == "plan"]
    assert plan_calls
    assert plan_calls[-1][1]["persist"] is False
    assert plan_calls[-1][1]["data_trade_date"].isoformat() == "2026-10-19"


def test_list_plans_returns_stored_or_recomputes(api):
    client, service, persistence = api
    persistence.blends["bl_x"] = _blend()
    persistence.plans["bp_1"] = {
        "plan_id": "bp_1",
        "blend_id": "bl_x",
        "data_trade_date": "2026-10-19",
    }
    persistence.plans_by_date["bl_x"] = persistence.plans["bp_1"]
    stored = client.get(f"{BASE}/bl_x/plans", params={"date": "2026-10-19"})
    assert stored.status_code == 200
    stored_item = stored.json()["items"][0]
    assert stored_item["plan_id"] == "bp_1"
    # 确认窗口由服务端标注（前端不再自行比日期）
    assert stored_item["can_confirm"] is True
    assert all(name != "plan" for name, _ in service.calls)

    persistence.plans_by_date.clear()
    recomputed = client.get(f"{BASE}/bl_x/plans", params={"date": "2026-10-18"})
    assert recomputed.status_code == 200
    assert recomputed.json()["recomputed"] is True
    recomputed_item = recomputed.json()["items"][0]
    assert {k: v for k, v in recomputed_item.items() if k != "can_confirm"} == (
        service.plan_payload
    )
    # 未落台账的预览计划不可确认
    assert recomputed_item["can_confirm"] is False
    assert _last_plan_call(service)["persist"] is False


def test_get_plan_checks_blend_membership(api):
    client, _service, persistence = api
    persistence.blends["bl_x"] = _blend()
    persistence.plans["bp_1"] = {"plan_id": "bp_1", "blend_id": "bl_other"}
    assert client.get(f"{BASE}/bl_x/plans/bp_1").status_code == 404
    persistence.plans["bp_1"]["blend_id"] = "bl_x"
    assert client.get(f"{BASE}/bl_x/plans/bp_1").status_code == 200


def test_confirm_plan_calls_service(api):
    client, service, persistence = api
    persistence.blends["bl_x"] = _blend()
    persistence.plans["bp_1"] = {
        "plan_id": "bp_1",
        "blend_id": "bl_x",
        "confirmed_at": None,
    }
    resp = client.post(f"{BASE}/bl_x/plans/bp_1/confirm")
    assert resp.status_code == 200
    assert resp.json()["confirmed_at"]
    assert resp.json()["can_confirm"] is False
    name, kwargs = service.calls[-1]
    assert name == "confirm_plan"
    assert kwargs["plan"]["plan_id"] == "bp_1"


def test_evidence_endpoint_reports_available_and_missing(api, monkeypatch, tmp_path):
    client, _service, _persistence = api
    path = tmp_path / "evidence.json"
    monkeypatch.setattr(mb.combo_auto, "evidence_path", lambda: path)
    monkeypatch.setattr(mb.combo_auto, "load_evidence", lambda: None)
    missing = client.get(f"{BASE}/evidence")
    assert missing.status_code == 200
    assert missing.json()["available"] is False

    monkeypatch.setattr(
        mb.combo_auto,
        "load_evidence",
        lambda: {"generated_at": "2026-10-21T00:00:00Z", "candidates": []},
    )
    found = client.get(f"{BASE}/evidence")
    assert found.status_code == 200
    assert found.json()["available"] is True
    assert found.json()["candidates"] == []


def test_blend_routes_not_shadowed_by_model_id_catchall(monkeypatch):
    """真实 app：``GET /api/v1/models/blends`` 不得落进 ``GET /{model_id}``。"""
    from backend.services.api.main import app

    async def fake_list(_self, **kwargs):
        return []

    monkeypatch.setattr(mb.ModelBlendPersistence, "list_blends", fake_list)
    app.dependency_overrides[get_current_user] = lambda: dict(USER)
    try:
        resp = TestClient(app).get(BASE)
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"items": []}
