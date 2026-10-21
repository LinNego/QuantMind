"""模型组合（blend）与纸面调仓台账的持久化。

四张表（设计稿 §4.2）：

- ``qm_model_blends``          组合定义（成员、权重、门槛、调仓参数）
- ``qm_model_blend_members``   成员与权重（复合主键）
- ``qm_model_blend_plans``     每日计划快照（不可变；确认后写 confirmed_at）
- ``qm_model_blend_positions`` 纸面持仓台账（确认计划时开/平仓）

**边界**：这里只写纸面台账，不触碰 ``orders`` / ``sim_trades`` / 任何实盘表。
所有查询按 ``(tenant_id, user_id)`` 归属隔离；组合 ID 不透明（``bl_`` 前缀），
越权访问返回 None → 路由层统一 404，不泄露存在性。
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime
from typing import Any
from collections.abc import Mapping, Sequence

from sqlalchemy import text

from backend.shared.database_manager_v2 import get_session

BLEND_ID_PREFIX = "bl_"
PLAN_ID_PREFIX = "bp_"


def new_blend_id() -> str:
    return f"{BLEND_ID_PREFIX}{uuid.uuid4().hex[:24]}"


def new_plan_id() -> str:
    return f"{PLAN_ID_PREFIX}{uuid.uuid4().hex[:24]}"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _loads(value: Any, fallback: Any = None) -> Any:
    if value is None:
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _is_unique_violation(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "unique" in text or "duplicate" in text


class ModelBlendPersistence:
    """组合定义 / 计划 / 持仓的读写。"""

    async def ensure_tables(self) -> None:
        statements = [
            """
            CREATE TABLE IF NOT EXISTS qm_model_blends (
                blend_id            TEXT PRIMARY KEY,
                tenant_id           TEXT NOT NULL,
                user_id             TEXT NOT NULL,
                name                TEXT NOT NULL,
                market              TEXT NOT NULL DEFAULT 'CN',
                purpose             TEXT NOT NULL DEFAULT 'rebalance',
                source              TEXT NOT NULL DEFAULT 'manual',
                template_id         TEXT,
                horizon_days        INTEGER NOT NULL DEFAULT 10,
                weighting           TEXT NOT NULL DEFAULT 'equal',
                method              TEXT NOT NULL DEFAULT 'mean',
                trim_count          INTEGER NOT NULL DEFAULT 2,
                min_coverage        INTEGER NOT NULL DEFAULT 8,
                consensus_min_count INTEGER NOT NULL DEFAULT 0,
                entry_per_day       INTEGER NOT NULL DEFAULT 5,
                rolling_window      INTEGER NOT NULL DEFAULT 10,
                target_positions    INTEGER NOT NULL DEFAULT 50,
                feature_signature   TEXT NOT NULL,
                data_source         TEXT NOT NULL DEFAULT 'quantdb_factors',
                status              TEXT NOT NULL DEFAULT 'active',
                created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (tenant_id, user_id, name)
            );
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_qm_model_blends_owner
                ON qm_model_blends (tenant_id, user_id, status, updated_at DESC);
            """,
            """
            CREATE TABLE IF NOT EXISTS qm_model_blend_members (
                blend_id     TEXT NOT NULL REFERENCES qm_model_blends(blend_id) ON DELETE CASCADE,
                model_id     TEXT NOT NULL,
                model_source TEXT NOT NULL DEFAULT 'user',
                weight       REAL NOT NULL DEFAULT 1.0,
                PRIMARY KEY (blend_id, model_id)
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS qm_model_blend_plans (
                plan_id        TEXT PRIMARY KEY,
                blend_id       TEXT NOT NULL REFERENCES qm_model_blends(blend_id) ON DELETE CASCADE,
                tenant_id      TEXT NOT NULL,
                user_id        TEXT NOT NULL,
                data_trade_date DATE NOT NULL,
                target_date     DATE NOT NULL,
                config_hash     TEXT NOT NULL,
                positions_hash  TEXT NOT NULL,
                params          JSONB NOT NULL,
                summary         JSONB NOT NULL,
                entries         JSONB NOT NULL,
                holds           JSONB NOT NULL,
                exits           JSONB NOT NULL,
                confirmed_at    TIMESTAMPTZ,
                created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (blend_id, data_trade_date, config_hash)
            );
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_qm_model_blend_plans_blend_date
                ON qm_model_blend_plans (blend_id, data_trade_date DESC);
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_blend_plan_confirmed
                ON qm_model_blend_plans (blend_id, data_trade_date)
                WHERE confirmed_at IS NOT NULL;
            """,
            """
            CREATE TABLE IF NOT EXISTS qm_model_blend_positions (
                blend_id     TEXT NOT NULL REFERENCES qm_model_blends(blend_id) ON DELETE CASCADE,
                symbol       TEXT NOT NULL,
                entry_date   DATE NOT NULL,
                entry_score  REAL,
                entry_price  REAL,
                shares       BIGINT NOT NULL DEFAULT 0,
                target_value REAL,
                status       TEXT NOT NULL DEFAULT 'open',
                exit_date    DATE,
                exit_reason  TEXT,
                created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (blend_id, symbol, entry_date)
            );
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_qm_model_blend_positions_open
                ON qm_model_blend_positions (blend_id, status, entry_date);
            """,
        ]
        async with get_session() as session:
            for statement in statements:
                await session.execute(text(statement))

    # ── 组合定义 ────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_blend(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "blend_id": str(row["blend_id"]),
            "tenant_id": str(row["tenant_id"]),
            "user_id": str(row["user_id"]),
            "name": str(row["name"]),
            "market": str(row["market"]),
            "purpose": str(row["purpose"]),
            "source": str(row["source"]),
            "template_id": row.get("template_id"),
            "horizon_days": int(row["horizon_days"]),
            "weighting": str(row["weighting"]),
            "method": str(row["method"]),
            "trim_count": int(row["trim_count"]),
            "min_coverage": int(row["min_coverage"]),
            "consensus_min_count": int(row["consensus_min_count"]),
            "entry_per_day": int(row["entry_per_day"]),
            "rolling_window": int(row["rolling_window"]),
            "target_positions": int(row["target_positions"]),
            "feature_signature": str(row["feature_signature"]),
            "data_source": str(row["data_source"]),
            "status": str(row["status"]),
            "created_at": _iso(row.get("created_at")),
            "updated_at": _iso(row.get("updated_at")),
        }

    async def create_blend(
        self,
        *,
        tenant_id: str,
        user_id: str,
        name: str,
        market: str,
        purpose: str,
        source: str,
        template_id: str | None,
        horizon_days: int,
        weighting: str,
        method: str,
        trim_count: int,
        min_coverage: int,
        consensus_min_count: int,
        entry_per_day: int,
        rolling_window: int,
        target_positions: int,
        feature_signature: str,
        data_source: str,
        members: Sequence[tuple[str, str, float]],
    ) -> dict[str, Any]:
        blend_id = new_blend_id()
        params = {
            "blend_id": blend_id,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "name": name,
            "market": market,
            "purpose": purpose,
            "source": source,
            "template_id": template_id,
            "horizon_days": horizon_days,
            "weighting": weighting,
            "method": method,
            "trim_count": trim_count,
            "min_coverage": min_coverage,
            "consensus_min_count": consensus_min_count,
            "entry_per_day": entry_per_day,
            "rolling_window": rolling_window,
            "target_positions": target_positions,
            "feature_signature": feature_signature,
            "data_source": data_source,
        }
        try:
            async with get_session() as session:
                await session.execute(
                    text(
                        """
                        INSERT INTO qm_model_blends (
                          blend_id, tenant_id, user_id, name, market, purpose, source,
                          template_id, horizon_days, weighting, method, trim_count,
                          min_coverage, consensus_min_count, entry_per_day, rolling_window,
                          target_positions, feature_signature, data_source, status
                        ) VALUES (
                          :blend_id, :tenant_id, :user_id, :name, :market, :purpose, :source,
                          :template_id, :horizon_days, :weighting, :method, :trim_count,
                          :min_coverage,
                          :consensus_min_count, :entry_per_day, :rolling_window,
                          :target_positions, :feature_signature, :data_source, 'active'
                        )
                        """
                    ),
                    params,
                )
                await self._replace_members(session, blend_id, members)
        except Exception as exc:  # noqa: BLE001 - 只有唯一键冲突才走重激活
            if not _is_unique_violation(exc):
                raise
            existing = await self._find_by_name(
                tenant_id=tenant_id, user_id=user_id, name=name
            )
            if existing is None or str(existing.get("status") or "") != "archived":
                raise
            # 归档组合仍占 (tenant,user,name) 唯一键。默认组合被归档后再次自动创建
            # 若直接 409 就永久失败（列表又不展示归档行）；这里原地重激活并覆盖配置。
            blend_id = str(existing["blend_id"])
            params["blend_id"] = blend_id
            async with get_session() as session:
                await session.execute(
                    text(
                        """
                        UPDATE qm_model_blends SET
                          market = :market, purpose = :purpose, source = :source,
                          template_id = :template_id, horizon_days = :horizon_days,
                          weighting = :weighting, method = :method,
                          trim_count = :trim_count, min_coverage = :min_coverage,
                          consensus_min_count = :consensus_min_count,
                          entry_per_day = :entry_per_day,
                          rolling_window = :rolling_window,
                          target_positions = :target_positions,
                          feature_signature = :feature_signature,
                          data_source = :data_source,
                          status = 'active', updated_at = NOW()
                        WHERE blend_id = :blend_id
                          AND tenant_id = :tenant_id AND user_id = :user_id
                        """
                    ),
                    params,
                )
                await self._replace_members(session, blend_id, members)
        blend = await self.get_blend(blend_id, tenant_id=tenant_id, user_id=user_id)
        if blend is None:  # pragma: no cover - 插入后同事务必可见
            raise RuntimeError("组合创建后读取失败")
        return blend

    @staticmethod
    async def _replace_members(
        session, blend_id: str, members: Sequence[tuple[str, str, float]]
    ) -> None:
        await session.execute(
            text("DELETE FROM qm_model_blend_members WHERE blend_id = :blend_id"),
            {"blend_id": blend_id},
        )
        for model_id, model_source, weight in members:
            await session.execute(
                text(
                    """
                    INSERT INTO qm_model_blend_members
                      (blend_id, model_id, model_source, weight)
                    VALUES (:blend_id, :model_id, :model_source, :weight)
                    """
                ),
                {
                    "blend_id": blend_id,
                    "model_id": model_id,
                    "model_source": model_source,
                    "weight": float(weight),
                },
            )

    async def _find_by_name(
        self, *, tenant_id: str, user_id: str, name: str
    ) -> dict[str, Any] | None:
        async with get_session(read_only=True) as session:
            row = (
                (
                    await session.execute(
                        text(
                            """
                            SELECT blend_id, status FROM qm_model_blends
                            WHERE tenant_id = :tenant_id AND user_id = :user_id
                              AND name = :name
                            """
                        ),
                        {"tenant_id": tenant_id, "user_id": user_id, "name": name},
                    )
                )
                .mappings()
                .first()
            )
        return dict(row) if row is not None else None

    async def get_blend(
        self, blend_id: str, *, tenant_id: str, user_id: str
    ) -> dict[str, Any] | None:
        async with get_session(read_only=True) as session:
            row = (
                (
                    await session.execute(
                        text(
                            """
                            SELECT * FROM qm_model_blends
                            WHERE blend_id = :blend_id
                              AND tenant_id = :tenant_id AND user_id = :user_id
                            """
                        ),
                        {
                            "blend_id": blend_id,
                            "tenant_id": tenant_id,
                            "user_id": user_id,
                        },
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        blend = self._row_to_blend(dict(row))
        blend["members"] = await self.get_members(blend_id)
        return blend

    async def list_blends(
        self, *, tenant_id: str, user_id: str, include_archived: bool = False
    ) -> list[dict[str, Any]]:
        where = "" if include_archived else "AND status = 'active'"
        async with get_session(read_only=True) as session:
            rows = (
                (
                    await session.execute(
                        text(
                            f"""
                            SELECT * FROM qm_model_blends
                            WHERE tenant_id = :tenant_id AND user_id = :user_id {where}
                            ORDER BY source DESC, updated_at DESC
                            """
                        ),
                        {"tenant_id": tenant_id, "user_id": user_id},
                    )
                )
                .mappings()
                .all()
            )
            members = (
                (
                    await session.execute(
                        text(
                            """
                            SELECT blend_id, model_id, model_source, weight
                            FROM qm_model_blend_members
                            WHERE blend_id = ANY(:ids)
                            """
                        ),
                        {"ids": [row["blend_id"] for row in rows] or [""]},
                    )
                )
                .mappings()
                .all()
            )
        by_blend: dict[str, list[dict[str, Any]]] = {}
        for member in members:
            by_blend.setdefault(str(member["blend_id"]), []).append(
                {
                    "model_id": str(member["model_id"]),
                    "model_source": str(member["model_source"]),
                    "weight": float(member["weight"]),
                }
            )
        result = []
        for row in rows:
            blend = self._row_to_blend(dict(row))
            blend["members"] = sorted(
                by_blend.get(blend["blend_id"], []), key=lambda m: m["model_id"]
            )
            result.append(blend)
        return result

    async def get_members(self, blend_id: str) -> list[dict[str, Any]]:
        async with get_session(read_only=True) as session:
            rows = (
                (
                    await session.execute(
                        text(
                            """
                            SELECT model_id, model_source, weight
                            FROM qm_model_blend_members
                            WHERE blend_id = :blend_id
                            ORDER BY model_id
                            """
                        ),
                        {"blend_id": blend_id},
                    )
                )
                .mappings()
                .all()
            )
        return [
            {
                "model_id": str(row["model_id"]),
                "model_source": str(row["model_source"]),
                "weight": float(row["weight"]),
            }
            for row in rows
        ]

    async def update_blend(
        self,
        blend_id: str,
        *,
        tenant_id: str,
        user_id: str,
        fields: Mapping[str, Any],
        members: Sequence[tuple[str, str, float]] | None = None,
    ) -> None:
        allowed = {
            "name",
            "purpose",
            "horizon_days",
            "weighting",
            "method",
            "trim_count",
            "min_coverage",
            "consensus_min_count",
            "entry_per_day",
            "rolling_window",
            "target_positions",
        }
        assignments = [f"{key} = :{key}" for key in fields if key in allowed]
        params: dict[str, Any] = dict(fields)
        params.update(
            {"blend_id": blend_id, "tenant_id": tenant_id, "user_id": user_id}
        )
        async with get_session() as session:
            if assignments:
                await session.execute(
                    text(
                        f"""
                        UPDATE qm_model_blends
                        SET {", ".join(assignments)}, updated_at = NOW()
                        WHERE blend_id = :blend_id
                          AND tenant_id = :tenant_id AND user_id = :user_id
                        """
                    ),
                    params,
                )
            if members is not None:
                await self._replace_members(session, blend_id, members)
                await session.execute(
                    text(
                        """
                        UPDATE qm_model_blends SET updated_at = NOW()
                        WHERE blend_id = :blend_id
                          AND tenant_id = :tenant_id AND user_id = :user_id
                        """
                    ),
                    params,
                )

    async def archive_blend(
        self, blend_id: str, *, tenant_id: str, user_id: str
    ) -> None:
        async with get_session() as session:
            await session.execute(
                text(
                    """
                    UPDATE qm_model_blends
                    SET status = 'archived', updated_at = NOW()
                    WHERE blend_id = :blend_id
                      AND tenant_id = :tenant_id AND user_id = :user_id
                    """
                ),
                {"blend_id": blend_id, "tenant_id": tenant_id, "user_id": user_id},
            )

    # ── 计划 ────────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_plan(row: Mapping[str, Any]) -> dict[str, Any]:
        summary = _loads(row.get("summary"), {})
        return {
            "plan_id": str(row["plan_id"]),
            "blend_id": str(row["blend_id"]),
            "data_trade_date": _iso(row["data_trade_date"]),
            "target_date": _iso(row["target_date"]),
            "config_hash": str(row["config_hash"]),
            "positions_hash": str(row["positions_hash"]),
            "params": _loads(row.get("params"), {}),
            "summary": summary,
            # 提升到顶层，读接口与即时生成返回同一形状；旧计划无此键为 None
            "data_ok": (
                summary.get("data_ok") if isinstance(summary, Mapping) else None
            ),
            "entries": _loads(row.get("entries"), []),
            "holds": _loads(row.get("holds"), []),
            "exits": _loads(row.get("exits"), []),
            "confirmed_at": _iso(row.get("confirmed_at")),
            "created_at": _iso(row.get("created_at")),
        }

    async def upsert_plan(
        self,
        *,
        blend_id: str,
        tenant_id: str,
        user_id: str,
        data_trade_date: date,
        target_date: date,
        config_hash: str,
        positions_hash: str,
        params: Mapping[str, Any],
        summary: Mapping[str, Any],
        entries: Sequence[Mapping[str, Any]],
        holds: Sequence[Mapping[str, Any]],
        exits: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """同一组合 + 数据日 + 配置指纹只保留一份计划；重复生成覆盖未确认版本。"""
        async with get_session() as session:
            row = (
                (
                    await session.execute(
                        text(
                            """
                            INSERT INTO qm_model_blend_plans (
                              plan_id, blend_id, tenant_id, user_id, data_trade_date,
                              target_date, config_hash, positions_hash, params, summary,
                              entries, holds, exits
                            ) VALUES (
                              :plan_id, :blend_id, :tenant_id, :user_id, :data_trade_date,
                              :target_date, :config_hash, :positions_hash,
                              CAST(:params AS JSONB), CAST(:summary AS JSONB),
                              CAST(:entries AS JSONB), CAST(:holds AS JSONB),
                              CAST(:exits AS JSONB)
                            )
                            ON CONFLICT (blend_id, data_trade_date, config_hash)
                            DO UPDATE SET
                              target_date = EXCLUDED.target_date,
                              positions_hash = EXCLUDED.positions_hash,
                              params = EXCLUDED.params,
                              summary = EXCLUDED.summary,
                              entries = EXCLUDED.entries,
                              holds = EXCLUDED.holds,
                              exits = EXCLUDED.exits
                            WHERE qm_model_blend_plans.confirmed_at IS NULL
                            RETURNING *
                            """
                        ),
                        {
                            "plan_id": new_plan_id(),
                            "blend_id": blend_id,
                            "tenant_id": tenant_id,
                            "user_id": user_id,
                            "data_trade_date": data_trade_date,
                            "target_date": target_date,
                            "config_hash": config_hash,
                            "positions_hash": positions_hash,
                            "params": _json(params),
                            "summary": _json(summary),
                            "entries": _json(entries),
                            "holds": _json(holds),
                            "exits": _json(exits),
                        },
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            # 已有确认版本：读回该版本（不覆盖已确认台账）
            existing = await self.get_plan_by_date(
                blend_id, data_trade_date=data_trade_date
            )
            if existing is None:  # pragma: no cover - 冲突必有行
                raise RuntimeError("计划写入冲突且读回失败")
            return existing
        return self._row_to_plan(dict(row))

    async def get_plan(
        self, plan_id: str, *, tenant_id: str, user_id: str
    ) -> dict[str, Any] | None:
        async with get_session(read_only=True) as session:
            row = (
                (
                    await session.execute(
                        text(
                            """
                            SELECT * FROM qm_model_blend_plans
                            WHERE plan_id = :plan_id
                              AND tenant_id = :tenant_id AND user_id = :user_id
                            """
                        ),
                        {
                            "plan_id": plan_id,
                            "tenant_id": tenant_id,
                            "user_id": user_id,
                        },
                    )
                )
                .mappings()
                .first()
            )
        return self._row_to_plan(dict(row)) if row else None

    async def get_plan_by_date(
        self, blend_id: str, *, data_trade_date: date
    ) -> dict[str, Any] | None:
        async with get_session(read_only=True) as session:
            row = (
                (
                    await session.execute(
                        text(
                            """
                            SELECT * FROM qm_model_blend_plans
                            WHERE blend_id = :blend_id AND data_trade_date = :date
                            ORDER BY confirmed_at DESC NULLS LAST, created_at DESC
                            LIMIT 1
                            """
                        ),
                        {"blend_id": blend_id, "date": data_trade_date},
                    )
                )
                .mappings()
                .first()
            )
        return self._row_to_plan(dict(row)) if row else None

    async def list_plans(
        self,
        blend_id: str,
        *,
        limit: int = 20,
        data_trade_date: date | None = None,
    ) -> list[dict[str, Any]]:
        where = "AND data_trade_date = :date" if data_trade_date else ""
        async with get_session(read_only=True) as session:
            rows = (
                (
                    await session.execute(
                        text(
                            f"""
                            SELECT * FROM qm_model_blend_plans
                            WHERE blend_id = :blend_id {where}
                            ORDER BY data_trade_date DESC, created_at DESC
                            LIMIT :limit
                            """
                        ),
                        {
                            "blend_id": blend_id,
                            "date": data_trade_date,
                            "limit": max(1, min(200, limit)),
                        },
                    )
                )
                .mappings()
                .all()
            )
        return [self._row_to_plan(dict(row)) for row in rows]

    async def confirm_plan(
        self,
        *,
        plan: Mapping[str, Any],
        target_date: date,
    ) -> bool:
        """确认计划：先抢占 confirmed_at，再关 exits、开 entries（单事务）。

        返回 False = 计划已被并发确认（rowcount 0），调用方映射 409；
        先抢计划行再做台账写入，避免「没确认成功却写入了持仓」。
        """
        async with get_session() as session:
            claimed = await session.execute(
                text(
                    """
                    UPDATE qm_model_blend_plans
                    SET confirmed_at = NOW()
                    WHERE plan_id = :plan_id AND confirmed_at IS NULL
                    """
                ),
                {"plan_id": plan["plan_id"]},
            )
            if claimed.rowcount == 0:
                return False
            for row in plan.get("exits") or []:
                await session.execute(
                    text(
                        """
                        UPDATE qm_model_blend_positions
                        SET status = 'closed', exit_date = :exit_date,
                            exit_reason = :reason, updated_at = NOW()
                        WHERE blend_id = :blend_id AND symbol = :symbol
                          AND status = 'open'
                        """
                    ),
                    {
                        "blend_id": plan["blend_id"],
                        "symbol": row["symbol"],
                        "exit_date": target_date,
                        "reason": row.get("exit_reason") or "计划退出",
                    },
                )
            for row in plan.get("entries") or []:
                await session.execute(
                    text(
                        """
                        INSERT INTO qm_model_blend_positions (
                          blend_id, symbol, entry_date, entry_score, entry_price,
                          shares, target_value, status
                        ) VALUES (
                          :blend_id, :symbol, :entry_date, :entry_score, :entry_price,
                          :shares, :target_value, 'open'
                        )
                        ON CONFLICT (blend_id, symbol, entry_date) DO NOTHING
                        """
                    ),
                    {
                        "blend_id": plan["blend_id"],
                        "symbol": row["symbol"],
                        "entry_date": target_date,
                        "entry_score": row.get("score"),
                        "entry_price": row.get("ref_price"),
                        "shares": int(row.get("shares") or 0),
                        "target_value": row.get("target_value"),
                    },
                )
        return True

    # ── 持仓 ────────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_position(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "symbol": str(row["symbol"]),
            "entry_date": _iso(row["entry_date"]),
            "entry_score": row.get("entry_score"),
            "entry_price": row.get("entry_price"),
            "shares": int(row["shares"] or 0),
            "target_value": row.get("target_value"),
            "status": str(row["status"]),
            "exit_date": _iso(row.get("exit_date")),
            "exit_reason": row.get("exit_reason"),
        }

    async def list_position_history(self, blend_id: str) -> list[dict[str, Any]]:
        """全部开/平仓记录（历史标注与再入选次数用）。"""
        async with get_session(read_only=True) as session:
            rows = (
                (
                    await session.execute(
                        text(
                            """
                            SELECT * FROM qm_model_blend_positions
                            WHERE blend_id = :blend_id
                            ORDER BY entry_date, symbol
                            """
                        ),
                        {"blend_id": blend_id},
                    )
                )
                .mappings()
                .all()
            )
        return [self._row_to_position(dict(row)) for row in rows]

    async def list_open_positions(
        self, blend_id: str, *, as_of: date | None = None
    ) -> list[dict[str, Any]]:
        """as_of 日收盘后的 open 持仓（回看用；缺省=当前）。"""
        if as_of is None:
            async with get_session(read_only=True) as session:
                rows = (
                    (
                        await session.execute(
                            text(
                                """
                                SELECT * FROM qm_model_blend_positions
                                WHERE blend_id = :blend_id AND status = 'open'
                                ORDER BY entry_date, symbol
                                """
                            ),
                            {"blend_id": blend_id},
                        )
                    )
                    .mappings()
                    .all()
                )
        else:
            async with get_session(read_only=True) as session:
                rows = (
                    (
                        await session.execute(
                            text(
                                """
                                SELECT * FROM qm_model_blend_positions
                                WHERE blend_id = :blend_id
                                  AND entry_date <= :as_of
                                  AND (exit_date IS NULL OR exit_date > :as_of)
                                ORDER BY entry_date, symbol
                                """
                            ),
                            {"blend_id": blend_id, "as_of": as_of},
                        )
                    )
                    .mappings()
                    .all()
                )
        return [self._row_to_position(dict(row)) for row in rows]
