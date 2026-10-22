"""归档模型恢复（restore）回归：状态回退、产物门槛与审计留痕。

口径（与 model_retirement 文档的「撤销 = 重训或从备份恢复」互补）：

- 归档时把归档前状态写入 ``metadata_json.archived_from_status``，恢复按原状返回
  （ready / candidate），不把候选模型偷偷抬成 ready；
- 旧行（归档早于本字段）按质量门禁口径回退：有 ``quality_warnings`` → candidate，
  否则 ready；
- ``artifacts_purged`` 墓碑 / 产物目录缺失 / 模型文件缺失 → 明确拒绝，绝不把
  坏模型恢复到「使用中」；
- 恢复写 ``user_audit_logs``（action=model.restore），**不动默认位**——是否接管
  推理由用户显式「设为默认」决定。

测试行随用随建随删，租户前缀 ``unittest_restore_``；``close_database`` 兜底
（连接池与事件循环绑定——不关掉会连累同文件下一个真库测试）。
"""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

import pytest

from backend.shared.model_registry import (
    _restore_target_status,
    model_registry_service,
)


# ── 纯函数：恢复目标状态 ──────────────────────────────────────────────


def test_restore_target_status_prefers_recorded_prior_status() -> None:
    """新行：按归档时记录的原状态恢复（candidate 不被抬成 ready）。"""
    assert _restore_target_status({"archived_from_status": "candidate"}) == "candidate"
    assert _restore_target_status({"archived_from_status": "ready"}) == "ready"
    # active 是旧口径的「使用中」，恢复归一到 ready
    assert _restore_target_status({"archived_from_status": "active"}) == "ready"


def test_restore_target_status_rejects_unrecoverable_prior_status() -> None:
    """记录为 failed/syncing/archived/空 → None（拒绝），绝不落回 ready 复活坏模型。"""
    for raw in ("failed", "syncing", "archived", "", "  "):
        assert _restore_target_status({"archived_from_status": raw}) is None
    assert _restore_target_status({"archived_from_status": None}) is None


def test_restore_target_status_legacy_rows_use_quality_warnings() -> None:
    """旧行（无该键）：有质量门禁告警回 candidate，其余 ready（ready 另需权重文件）。"""
    assert _restore_target_status({}) == "ready"
    assert _restore_target_status({"quality_warnings": []}) == "ready"
    assert (
        _restore_target_status({"quality_warnings": ["样本外质量门禁"]}) == "candidate"
    )


# ── 真库集成 ─────────────────────────────────────────────────────────


async def _db_available() -> bool:
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    try:
        async with get_session(read_only=True) as probe:
            await probe.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        return False


def _make_model_dir(tenant: str, user: str, model_id: str) -> Path:
    """在用户模型根内造一个带 model.lgb 的最小产物目录。"""
    root = Path(model_registry_service.user_models_root)
    path = root / tenant / user / model_id
    path.mkdir(parents=True, exist_ok=True)
    (path / "model.lgb").write_text("stub", encoding="utf-8")
    return path


async def _insert_model(
    *,
    tenant: str,
    user: str,
    model_id: str,
    status: str,
    storage_path: str,
    metadata: dict,
    model_file: str = "model.lgb",
) -> None:
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    async with get_session() as session:
        await session.execute(
            text(
                """
                INSERT INTO qm_user_models (
                    tenant_id, user_id, model_id, status, storage_path, model_file,
                    metadata_json
                ) VALUES (
                    :tenant, :user, :model_id, :status, :storage_path, :model_file,
                    CAST(:metadata AS JSONB)
                )
                """
            ),
            {
                "tenant": tenant,
                "user": user,
                "model_id": model_id,
                "status": status,
                "storage_path": storage_path,
                "model_file": model_file,
                "metadata": json.dumps(metadata, ensure_ascii=False),
            },
        )


async def _cleanup(tenant: str) -> None:
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    async with get_session() as session:
        await session.execute(
            text("DELETE FROM user_audit_logs WHERE tenant_id = :t"), {"t": tenant}
        )
        await session.execute(
            text("DELETE FROM qm_user_models WHERE tenant_id = :t"), {"t": tenant}
        )


async def _audit_actions(tenant: str, model_id: str) -> list[dict]:
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    async with get_session(read_only=True) as session:
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT action, description, success FROM user_audit_logs "
                        "WHERE tenant_id = :t AND resource_id = :m "
                        "ORDER BY id"
                    ),
                    {"t": tenant, "m": model_id},
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_restore_roundtrip_preserves_status_and_writes_audit():
    """ready 归档 → 恢复 ready；默认位不动；审计落 model.archive/model.restore。"""
    from backend.shared.database_manager_v2 import close_database

    if not await _db_available():
        pytest.skip("DB 不可用")

    unique = uuid.uuid4().hex[:6]
    tenant = f"unittest_restore_{unique}"
    user = "u"
    mid = f"mdl_restore_{unique}"
    path = _make_model_dir(tenant, user, mid)
    try:
        await _insert_model(
            tenant=tenant,
            user=user,
            model_id=mid,
            status="ready",
            storage_path=str(path),
            metadata={"market": "CN"},
        )

        archived = await model_registry_service.archive_model(
            tenant_id=tenant, user_id=user, model_id=mid
        )
        assert archived["status"] == "archived"
        assert archived["metadata_json"]["archived_from_status"] == "ready"

        restored = await model_registry_service.restore_model(
            tenant_id=tenant, user_id=user, model_id=mid
        )
        assert restored["status"] == "ready"
        assert restored["is_default"] is False, "恢复不动默认位，接管需显式设为默认"

        actions = await _audit_actions(tenant, mid)
        assert [a["action"] for a in actions] == ["model.archive", "model.restore"]
        restore_audit = actions[-1]
        assert restore_audit["success"] is True
        assert "恢复" in str(restore_audit["description"])
    finally:
        try:
            await _cleanup(tenant)
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)
            await close_database()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_restore_candidate_stays_candidate():
    """候选模型归档再恢复仍是候选，不绕过质量门禁。"""
    from backend.shared.database_manager_v2 import close_database

    if not await _db_available():
        pytest.skip("DB 不可用")

    unique = uuid.uuid4().hex[:6]
    tenant = f"unittest_restore_{unique}"
    user = "u"
    mid = f"mdl_candidate_{unique}"
    path = _make_model_dir(tenant, user, mid)
    try:
        await _insert_model(
            tenant=tenant,
            user=user,
            model_id=mid,
            status="candidate",
            storage_path=str(path),
            metadata={"market": "CN", "quality_warnings": ["样本外质量门禁"]},
        )
        await model_registry_service.archive_model(
            tenant_id=tenant, user_id=user, model_id=mid
        )
        restored = await model_registry_service.restore_model(
            tenant_id=tenant, user_id=user, model_id=mid
        )
        assert restored["status"] == "candidate"
    finally:
        try:
            await _cleanup(tenant)
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)
            await close_database()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_restore_legacy_rows_fall_back_by_quality_gate():
    """旧归档行（无 archived_from_status）：有告警回 candidate，无告警回 ready。"""
    from backend.shared.database_manager_v2 import close_database

    if not await _db_available():
        pytest.skip("DB 不可用")

    unique = uuid.uuid4().hex[:6]
    tenant = f"unittest_restore_{unique}"
    user = "u"
    ready_id = f"mdl_legacy_ready_{unique}"
    cand_id = f"mdl_legacy_cand_{unique}"
    ready_path = _make_model_dir(tenant, user, ready_id)
    cand_path = _make_model_dir(tenant, user, cand_id)
    try:
        await _insert_model(
            tenant=tenant,
            user=user,
            model_id=ready_id,
            status="archived",
            storage_path=str(ready_path),
            metadata={"market": "CN"},
        )
        await _insert_model(
            tenant=tenant,
            user=user,
            model_id=cand_id,
            status="archived",
            storage_path=str(cand_path),
            metadata={"market": "CN", "quality_warnings": ["样本外质量门禁"]},
        )

        restored_ready = await model_registry_service.restore_model(
            tenant_id=tenant, user_id=user, model_id=ready_id
        )
        restored_cand = await model_registry_service.restore_model(
            tenant_id=tenant, user_id=user, model_id=cand_id
        )
        assert restored_ready["status"] == "ready"
        assert restored_cand["status"] == "candidate"
    finally:
        try:
            await _cleanup(tenant)
        finally:
            shutil.rmtree(ready_path.parent, ignore_errors=True)
            await close_database()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_restore_rejects_non_archived_model():
    """使用中的模型不能走恢复（避免误改状态）。"""
    from backend.shared.database_manager_v2 import close_database

    if not await _db_available():
        pytest.skip("DB 不可用")

    unique = uuid.uuid4().hex[:6]
    tenant = f"unittest_restore_{unique}"
    user = "u"
    mid = f"mdl_ready_{unique}"
    path = _make_model_dir(tenant, user, mid)
    try:
        await _insert_model(
            tenant=tenant,
            user=user,
            model_id=mid,
            status="ready",
            storage_path=str(path),
            metadata={"market": "CN"},
        )
        with pytest.raises(ValueError, match="not archived"):
            await model_registry_service.restore_model(
                tenant_id=tenant, user_id=user, model_id=mid
            )
    finally:
        try:
            await _cleanup(tenant)
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)
            await close_database()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_restore_rejects_purged_or_missing_artifacts():
    """产物已清退 / 目录缺失 → 明确拒绝，不恢复到坏状态。"""
    from backend.shared.database_manager_v2 import close_database

    if not await _db_available():
        pytest.skip("DB 不可用")

    unique = uuid.uuid4().hex[:6]
    tenant = f"unittest_restore_{unique}"
    user = "u"
    purged_id = f"mdl_purged_{unique}"
    missing_id = f"mdl_missing_{unique}"
    purged_path = _make_model_dir(tenant, user, purged_id)
    # missing 目录刻意不创建
    missing_path = (
        Path(model_registry_service.user_models_root) / tenant / user / missing_id
    )
    try:
        await _insert_model(
            tenant=tenant,
            user=user,
            model_id=purged_id,
            status="archived",
            storage_path=str(purged_path),
            metadata={
                "market": "CN",
                "archived_from_status": "ready",
                "artifacts_purged": True,
            },
        )
        await _insert_model(
            tenant=tenant,
            user=user,
            model_id=missing_id,
            status="archived",
            storage_path=str(missing_path),
            metadata={"market": "CN", "archived_from_status": "ready"},
        )

        with pytest.raises(ValueError, match="清退"):
            await model_registry_service.restore_model(
                tenant_id=tenant, user_id=user, model_id=purged_id
            )
        with pytest.raises(ValueError, match="目录"):
            await model_registry_service.restore_model(
                tenant_id=tenant, user_id=user, model_id=missing_id
            )

        # 拒绝 = 状态不变 + 不写恢复审计（恢复与审计同事务；校验失败整段不落库）
        for mid in (purged_id, missing_id):
            row = await model_registry_service.get_model(
                tenant_id=tenant, user_id=user, model_id=mid
            )
            assert row is not None and row["status"] == "archived"
            assert await _audit_actions(tenant, mid) == []
    finally:
        try:
            await _cleanup(tenant)
        finally:
            shutil.rmtree(purged_path.parent, ignore_errors=True)
            await close_database()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_restore_rejects_failed_prior_status():
    """归档前是 failed 的行不得恢复成 ready（产物文件在也不行）。"""
    from backend.shared.database_manager_v2 import close_database

    if not await _db_available():
        pytest.skip("DB 不可用")

    unique = uuid.uuid4().hex[:6]
    tenant = f"unittest_restore_{unique}"
    user = "u"
    mid = f"mdl_failed_{unique}"
    path = _make_model_dir(tenant, user, mid)
    try:
        await _insert_model(
            tenant=tenant,
            user=user,
            model_id=mid,
            status="archived",
            storage_path=str(path),
            metadata={"market": "CN", "archived_from_status": "failed"},
        )
        with pytest.raises(ValueError, match="不支持恢复"):
            await model_registry_service.restore_model(
                tenant_id=tenant, user_id=user, model_id=mid
            )
        row = await model_registry_service.get_model(
            tenant_id=tenant, user_id=user, model_id=mid
        )
        assert row is not None and row["status"] == "archived"
        assert await _audit_actions(tenant, mid) == []
    finally:
        try:
            await _cleanup(tenant)
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)
            await close_database()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_restore_rejects_system_model():
    """readonly/系统模型不允许恢复（不把系统资产变成用户可改状态）。"""
    from backend.shared.database_manager_v2 import close_database

    if not await _db_available():
        pytest.skip("DB 不可用")

    unique = uuid.uuid4().hex[:6]
    tenant = f"unittest_restore_{unique}"
    user = "u"
    mid = f"mdl_system_{unique}"
    path = _make_model_dir(tenant, user, mid)
    try:
        await _insert_model(
            tenant=tenant,
            user=user,
            model_id=mid,
            status="archived",
            storage_path=str(path),
            metadata={
                "market": "CN",
                "readonly": True,
                "archived_from_status": "ready",
            },
        )
        with pytest.raises(ValueError, match="system model"):
            await model_registry_service.restore_model(
                tenant_id=tenant, user_id=user, model_id=mid
            )
    finally:
        try:
            await _cleanup(tenant)
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)
            await close_database()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_restore_ready_requires_model_file():
    """恢复为 ready 必须声明且存在权重文件；只有目录不算可推理模型。"""
    from backend.shared.database_manager_v2 import close_database

    if not await _db_available():
        pytest.skip("DB 不可用")

    unique = uuid.uuid4().hex[:6]
    tenant = f"unittest_restore_{unique}"
    user = "u"
    empty_id = f"mdl_nofile_{unique}"
    missing_id = f"mdl_badfile_{unique}"
    empty_path = _make_model_dir(tenant, user, empty_id)
    missing_path = _make_model_dir(tenant, user, missing_id)
    try:
        # 空 model_file：目录在但不声明权重 → 拒绝
        await _insert_model(
            tenant=tenant,
            user=user,
            model_id=empty_id,
            status="archived",
            storage_path=str(empty_path),
            model_file="",
            metadata={"market": "CN", "archived_from_status": "ready"},
        )
        # 声明了不存在的权重文件 → 拒绝
        await _insert_model(
            tenant=tenant,
            user=user,
            model_id=missing_id,
            status="archived",
            storage_path=str(missing_path),
            model_file="model.pth",
            metadata={"market": "CN", "archived_from_status": "ready"},
        )

        with pytest.raises(ValueError, match="未声明模型文件"):
            await model_registry_service.restore_model(
                tenant_id=tenant, user_id=user, model_id=empty_id
            )
        with pytest.raises(ValueError, match="模型文件缺失"):
            await model_registry_service.restore_model(
                tenant_id=tenant, user_id=user, model_id=missing_id
            )
    finally:
        try:
            await _cleanup(tenant)
        finally:
            shutil.rmtree(empty_path.parent, ignore_errors=True)
            await close_database()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_archive_merges_metadata_and_rejects_double_archive():
    """归档只增量写归档字段，不覆盖展示名；重复归档明确拒绝。"""
    from backend.shared.database_manager_v2 import close_database

    if not await _db_available():
        pytest.skip("DB 不可用")

    unique = uuid.uuid4().hex[:6]
    tenant = f"unittest_restore_{unique}"
    user = "u"
    mid = f"mdl_merge_{unique}"
    path = _make_model_dir(tenant, user, mid)
    try:
        await _insert_model(
            tenant=tenant,
            user=user,
            model_id=mid,
            status="ready",
            storage_path=str(path),
            metadata={"market": "CN", "display_name": "原展示名"},
        )
        archived = await model_registry_service.archive_model(
            tenant_id=tenant, user_id=user, model_id=mid
        )
        meta = archived["metadata_json"]
        assert meta["display_name"] == "原展示名"
        assert meta["market"] == "CN"
        assert meta["archived_from_status"] == "ready"

        with pytest.raises(ValueError, match="already archived"):
            await model_registry_service.archive_model(
                tenant_id=tenant, user_id=user, model_id=mid
            )

        again = await model_registry_service.get_model(
            tenant_id=tenant, user_id=user, model_id=mid
        )
        assert again is not None and again["status"] == "archived"
        assert again["metadata_json"]["archived_from_status"] == "ready"
        assert [a["action"] for a in await _audit_actions(tenant, mid)] == [
            "model.archive"
        ]
    finally:
        try:
            await _cleanup(tenant)
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)
            await close_database()


# ── 端点面（服务替身，不碰数据库） ───────────────────────────────────


def _restore_client(monkeypatch, fake):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend.services.api.routers import model_training as mt

    monkeypatch.setattr(mt, "model_registry_service", fake)
    app = FastAPI()
    app.include_router(mt.router, prefix="/api/v1/models")
    app.dependency_overrides[mt.get_current_user] = lambda: {
        "tenant_id": "tenant-x",
        "user_id": "user-9",
        "sub": "user-9",
    }
    return TestClient(app)


class _FakeRestoreRegistry:
    def __init__(self, *, result=None, error=None):
        self.calls: list[dict] = []
        self.result = result
        self.error = error

    async def restore_model(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.result


def test_restore_endpoint_requires_authentication():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend.services.api.routers import model_training as mt

    app = FastAPI()
    app.include_router(mt.router, prefix="/api/v1/models")
    resp = TestClient(app).post("/api/v1/models/mdl_x/restore")
    assert resp.status_code in (401, 403)


def test_restore_endpoint_passes_jwt_identity_and_returns_model(monkeypatch):
    fake = _FakeRestoreRegistry(
        result={"model_id": "mdl_x", "status": "ready", "is_default": False}
    )
    resp = _restore_client(monkeypatch, fake).post("/api/v1/models/mdl_x/restore")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ready"
    assert fake.calls == [
        {"tenant_id": "tenant-x", "user_id": "user-9", "model_id": "mdl_x"}
    ]


def test_restore_endpoint_maps_refusal_to_422_with_detail(monkeypatch):
    fake = _FakeRestoreRegistry(
        error=ValueError("模型产物已清退，无法恢复；请从训练记录重新训练或从备份恢复")
    )
    resp = _restore_client(monkeypatch, fake).post("/api/v1/models/mdl_x/restore")
    assert resp.status_code == 422
    assert "清退" in resp.json()["detail"]
