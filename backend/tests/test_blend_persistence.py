"""组合持久化的重激活路径（归档名占用唯一键）。

``(tenant_id, user_id, name)`` 唯一键不区分 status：归档组合会继续占名。
默认组合的自动创建只在「列表为空」时触发，若同名行已归档就会永远 409。
本文件钉住：唯一冲突且已有行是 archived → 原地重激活并覆盖配置；
同名行仍 active → 原样抛出（路由层映射 409）。
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from backend.services.engine.services import model_blend_persistence as mbp


class _FakeResult:
    def __init__(self, row=None):
        self._row = row

    def mappings(self):
        return self

    def first(self):
        return self._row


class _UniqueConflictSession:
    """INSERT 必撞唯一键；SELECT blend_id,status 返回既有行。"""

    def __init__(self, existing_row):
        self.existing_row = existing_row
        self.statements: list[str] = []

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.statements.append(sql)
        if "INSERT INTO qm_model_blends" in sql:
            raise Exception(
                'duplicate key value violates unique constraint '
                '"qm_model_blends_tenant_id_user_id_name_key"'
            )
        if "SELECT blend_id, status" in sql:
            return _FakeResult(self.existing_row)
        return _FakeResult(None)


def _create_kwargs() -> dict:
    return {
        "tenant_id": "t1",
        "user_id": "u1",
        "name": "13-t10-mean",
        "market": "CN",
        "purpose": "rebalance",
        "source": "auto",
        "template_id": "c0",
        "horizon_days": 10,
        "weighting": "equal",
        "method": "mean",
        "trim_count": 2,
        "min_coverage": 8,
        "consensus_min_count": 0,
        "entry_per_day": 5,
        "rolling_window": 10,
        "target_positions": 50,
        "feature_signature": "sig",
        "data_source": "quantdb_factors",
        "members": [("m1", "user", 1.0), ("m2", "user", 1.0)],
    }


def _wire(monkeypatch, session):
    @asynccontextmanager
    async def fake_get_session(*_args, **_kwargs):
        yield session

    monkeypatch.setattr(mbp, "get_session", fake_get_session)
    persistence = mbp.ModelBlendPersistence()

    async def fake_get_blend(blend_id, *, tenant_id, user_id):
        return {"blend_id": blend_id, "status": "active"}

    monkeypatch.setattr(persistence, "get_blend", fake_get_blend)
    return persistence


def test_is_unique_violation_matches_postgres_and_sqlite_text():
    assert mbp._is_unique_violation(
        Exception('duplicate key value violates unique constraint "x"')
    )
    assert mbp._is_unique_violation(
        Exception("UNIQUE constraint failed: qm_model_blends.name")
    )
    assert not mbp._is_unique_violation(Exception("connection reset by peer"))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_blend_reactivates_archived_name(monkeypatch):
    session = _UniqueConflictSession({"blend_id": "bl_old", "status": "archived"})
    persistence = _wire(monkeypatch, session)

    created = await persistence.create_blend(**_create_kwargs())

    # 复用归档行 ID（纸面台账/计划挂在同一 blend 上，换新 ID 会断链）
    assert created["blend_id"] == "bl_old"
    updates = [sql for sql in session.statements if sql.strip().startswith("UPDATE")]
    assert len(updates) == 1
    assert "status = 'active'" in updates[0]
    assert any(
        "DELETE FROM qm_model_blend_members" in sql for sql in session.statements
    )
    member_inserts = [
        sql for sql in session.statements if "INSERT INTO qm_model_blend_members" in sql
    ]
    assert len(member_inserts) == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_blend_reraises_unique_conflict_for_active_name(monkeypatch):
    session = _UniqueConflictSession({"blend_id": "bl_live", "status": "active"})
    persistence = _wire(monkeypatch, session)

    with pytest.raises(Exception, match="duplicate key"):
        await persistence.create_blend(**_create_kwargs())

    assert not any(sql.strip().startswith("UPDATE") for sql in session.statements)
