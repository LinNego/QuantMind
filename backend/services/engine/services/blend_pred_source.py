"""组合历史 · pred 测试集数据源（DuckDB 聚合）。

把「组合成员 → 每日组合分 TopK 榜单」从各模型目录的 ``pred.parquet``
``split='test'`` 分片还原出来，作为组合周期收益/滚动建仓的统一历史端口。

口径与 ``ModelBlendService._window_stats`` 对齐：

1. 每模型按日做 ``PERCENT_RANK()``（与 ``engine_signal_scores.rank_pct``
   在重叠日逐值相等，实测最大绝对差 0.0）；
2. 按成员权重聚合（mean 加权 / median / trimmed）；
3. 覆盖率 ``COUNT(*)`` 与共识票数 ``pct >= 0.70`` 与 SQL 侧同源；
4. 排序 ``score DESC, coverage DESC, symbol ASC``，分位用升序名次。

整组合选源：只有**全部成员**都解析到非空 ``test`` 分片才可用，
否则由服务层整体降级 runs，绝不混源（细案 D1）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from collections.abc import Mapping, Sequence

import duckdb

from backend.services.engine.inference.blend_engine import (
    position_of,
    rank_pct_from_rank,
)
from backend.services.engine.inference.model_backfill import resolve_pred_candidates
from backend.shared import model_registry as model_registry_module
from backend.shared.stock_utils import build_prefix_symbol_sql

logger = logging.getLogger(__name__)

PREFIX_SYMBOL_SQL = build_prefix_symbol_sql()
__all__ = [
    "BlendPredError",
    "BlendPredSource",
    "PredMember",
    "PredReadiness",
    "PRED_SPLIT",
]

PRED_SPLIT = "test"
CONSENSUS_PCT = 0.70
READINESS_CACHE_TTL_SECONDS = 60.0
TOP_CACHE_TTL_SECONDS = 120.0
TOP_CACHE_MAX_ENTRIES = 32
DUCKDB_MEMORY_LIMIT = "2GB"
_READINESS_CACHE: OrderedDict[tuple, tuple[float, PredReadiness]] = OrderedDict()
_TOP_CACHE: OrderedDict[tuple, tuple[float, dict[date, list[dict[str, Any]]]]] = (
    OrderedDict()
)
_CACHE_LOCK = threading.Lock()


class BlendPredError(Exception):
    """带机读 code 的 pred 数据源错误。"""

    def __init__(self, code: str, message: str, status_code: int = 422):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class PredMember:
    """一个已解析到 pred 文件、且带测试集边界的组合成员。"""

    model_id: str
    weight: float
    path: Path
    mtime_ns: int
    size: int
    start: date
    end: date


@dataclass(frozen=True)
class PredReadiness:
    """整组合的 pred 可用性（全部成员可用才可切 pred）。"""

    members: tuple[PredMember, ...]
    members_total: int
    start: date | None
    end: date | None
    trading_days: int
    warnings: tuple[str, ...]

    @property
    def ready_members(self) -> int:
        return len(self.members)

    @property
    def all_ready(self) -> bool:
        return self.members_total > 0 and self.ready_members == self.members_total


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.fromisoformat(str(value)[:10]).date()


def _escape(value: Any) -> str:
    return str(value).replace("'", "''")


def _temp_directory() -> str:
    path = os.environ.get("QM_DUCKDB_TEMP_DIR", "/tmp/quantmind_duckdb")
    Path(path).mkdir(parents=True, exist_ok=True)
    return path


def _connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute(f"SET memory_limit='{DUCKDB_MEMORY_LIMIT}'")
    con.execute(f"SET temp_directory='{_escape(_temp_directory())}'")
    return con


def _cache_get(cache: OrderedDict, key: tuple, ttl: float):
    with _CACHE_LOCK:
        item = cache.get(key)
        if item is None:
            return None
        expires, payload = item
        if expires < time.monotonic():
            cache.pop(key, None)
            return None
        cache.move_to_end(key)
        return payload


def _cache_put(cache: OrderedDict, key: tuple, payload, ttl: float, max_entries: int):
    with _CACHE_LOCK:
        cache[key] = (time.monotonic() + ttl, payload)
        cache.move_to_end(key)
        while len(cache) > max_entries:
            cache.popitem(last=False)


class BlendPredSource:
    """pred 测试集读取与聚合（进程内缓存 + 工作线程执行）。"""

    def __init__(self, *, resolver: Any | None = None):
        self._resolver = resolver or model_registry_module.model_registry_service

    # ── 解析与边界 ────────────────────────────────────────────────────
    async def readiness(
        self,
        members: Sequence[Mapping[str, Any]],
        *,
        tenant_id: str,
        user_id: str,
        market: str | None = None,
    ) -> PredReadiness:
        cache_key = (
            "readiness",
            str(tenant_id),
            str(user_id),
            str(market or ""),
            tuple(
                (str(m["model_id"]), str(m.get("storage_path") or "")) for m in members
            ),
        )
        cached = _cache_get(_READINESS_CACHE, cache_key, READINESS_CACHE_TTL_SECONDS)
        if cached is not None:
            return cached

        total = len(members)
        warnings: list[str] = []
        resolved: list[tuple[str, float, Path]] = []
        for member in members:
            model_id = str(member["model_id"])
            weight = float(member.get("weight") or 1.0)
            storage = str(member.get("storage_path") or "").strip()
            if not storage:
                try:
                    effective = await self._resolver.resolve_effective_model(
                        tenant_id=tenant_id,
                        user_id=user_id,
                        model_id=model_id,
                        market=market,
                    )
                except Exception as exc:  # noqa: BLE001 - 单成员失败只降级
                    logger.warning("解析组合成员 %s 失败: %s", model_id, exc)
                    warnings.append(f"member_unavailable:{model_id}")
                    continue
                source = str(getattr(effective, "model_source", "") or "")
                if getattr(effective, "fallback_used", False) and source in {
                    "user_default",
                    "system_fallback",
                }:
                    warnings.append(f"member_unavailable:{model_id}")
                    continue
                storage = str(getattr(effective, "storage_path", "") or "")
            pred_path = self._pred_path(storage) if storage else None
            if pred_path is None:
                warnings.append(f"member_pred_unavailable:{model_id}")
                continue
            resolved.append((model_id, weight, pred_path))

        if not resolved:
            readiness = PredReadiness(
                members=(),
                members_total=total,
                start=None,
                end=None,
                trading_days=0,
                warnings=tuple(warnings),
            )
            _cache_put(
                _READINESS_CACHE,
                cache_key,
                readiness,
                READINESS_CACHE_TTL_SECONDS,
                64,
            )
            return readiness

        bounds, failed = await asyncio.to_thread(self._read_bounds, resolved)
        warnings.extend(f"member_pred_empty:{model_id}" for model_id in failed)
        ready_members = [
            PredMember(
                model_id=model_id,
                weight=weight,
                path=path,
                mtime_ns=path.stat().st_mtime_ns,
                size=path.stat().st_size,
                start=first,
                end=last,
            )
            for model_id, weight, path in resolved
            if model_id in bounds
            for first, last in [bounds[model_id]]
        ]
        start = max((m.start for m in ready_members), default=None)
        end = min((m.end for m in ready_members), default=None)
        trading_days = 0
        if ready_members and start is not None and end is not None and start <= end:
            trading_days = await asyncio.to_thread(
                self._count_days, ready_members, start, end
            )
        readiness = PredReadiness(
            members=tuple(ready_members),
            members_total=total,
            start=start if trading_days else None,
            end=end if trading_days else None,
            trading_days=trading_days,
            warnings=tuple(warnings),
        )
        _cache_put(
            _READINESS_CACHE, cache_key, readiness, READINESS_CACHE_TTL_SECONDS, 64
        )
        return readiness

    @staticmethod
    def _pred_path(storage_path: str) -> Path | None:
        for candidate in resolve_pred_candidates(storage_path):
            if candidate.is_file():
                return candidate
        return None

    @staticmethod
    def _read_bounds(
        members: Sequence[tuple[str, float, Path]],
    ) -> tuple[dict[str, tuple[date, date]], list[str]]:
        files = ", ".join(f"'{_escape(path)}'" for _, _, path in members)
        sql = f"""
            SELECT p.filename,
                   MIN(CAST(p.trade_date AS DATE)) AS first_day,
                   MAX(CAST(p.trade_date AS DATE)) AS last_day
            FROM read_parquet([{files}], union_by_name=true, filename=true) AS p
            WHERE p.split = ? AND p.pred IS NOT NULL
            GROUP BY p.filename
        """
        by_file = {str(path): model_id for model_id, _, path in members}
        try:
            con = _connect()
            try:
                rows = con.execute(sql, [PRED_SPLIT]).fetchall()
            finally:
                con.close()
        except Exception as exc:  # noqa: BLE001 - 整体失败时逐文件重试定位坏文件
            logger.warning("pred 边界聚合失败，逐文件重试: %s", exc)
            return BlendPredSource._read_bounds_per_file(members)

        bounds: dict[str, tuple[date, date]] = {}
        for filename, first_day, last_day in rows:
            model_id = by_file.get(str(filename))
            if model_id is None or first_day is None or last_day is None:
                continue
            bounds[model_id] = (_as_date(first_day), _as_date(last_day))
        failed = [model_id for model_id, _, _ in members if model_id not in bounds]
        return bounds, failed

    @staticmethod
    def _read_bounds_per_file(
        members: Sequence[tuple[str, float, Path]],
    ) -> tuple[dict[str, tuple[date, date]], list[str]]:
        bounds: dict[str, tuple[date, date]] = {}
        failed: list[str] = []
        con = _connect()
        try:
            for model_id, _, path in members:
                try:
                    row = con.execute(
                        "SELECT MIN(CAST(trade_date AS DATE)), "
                        "MAX(CAST(trade_date AS DATE)) "
                        f"FROM read_parquet('{_escape(path)}') "
                        "WHERE split = ? AND pred IS NOT NULL",
                        [PRED_SPLIT],
                    ).fetchone()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("读取 %s 测试集边界失败: %s", path, exc)
                    failed.append(model_id)
                    continue
                if not row or row[0] is None or row[1] is None:
                    failed.append(model_id)
                    continue
                bounds[model_id] = (_as_date(row[0]), _as_date(row[1]))
        finally:
            con.close()
        return bounds, failed

    @staticmethod
    def _count_days(members: Sequence[PredMember], start: date, end: date) -> int:
        files = ", ".join(f"'{_escape(member.path)}'" for member in members)
        sql = f"""
            SELECT COUNT(DISTINCT CAST(trade_date AS DATE))
            FROM read_parquet([{files}], union_by_name=true)
            WHERE split = ? AND pred IS NOT NULL
              AND CAST(trade_date AS DATE) BETWEEN ? AND ?
        """
        con = _connect()
        try:
            row = con.execute(sql, [PRED_SPLIT, start, end]).fetchone()
        finally:
            con.close()
        return int(row[0] or 0) if row else 0

    # ── 聚合读取 ──────────────────────────────────────────────────────
    async def load_top_rows(
        self,
        members: Sequence[PredMember],
        *,
        method: str,
        trim_count: int,
        min_coverage: int,
        consensus_min_count: int,
        start: date,
        end: date,
        top_k: int,
    ) -> dict[date, list[dict[str, Any]]]:
        if method not in {"mean", "median", "trimmed"}:
            raise BlendPredError("invalid_request", f"未知组合方法：{method}")
        if not members:
            raise BlendPredError("pred_unavailable", "组合成员没有可用的 pred 测试集")
        clamped_start = max(start, max(member.start for member in members))
        clamped_end = min(end, min(member.end for member in members))
        if clamped_start > clamped_end:
            return {}

        cache_key = (
            "top",
            tuple(
                (
                    member.model_id,
                    round(member.weight, 12),
                    str(member.path),
                    member.mtime_ns,
                    member.size,
                )
                for member in members
            ),
            method,
            int(trim_count),
            int(min_coverage),
            int(consensus_min_count),
            clamped_start,
            clamped_end,
            int(top_k),
        )
        cached = _cache_get(_TOP_CACHE, cache_key, TOP_CACHE_TTL_SECONDS)
        if cached is not None:
            return {day: [dict(row) for row in rows] for day, rows in cached.items()}

        payload = await asyncio.to_thread(
            self._query_top_rows,
            members,
            method=method,
            trim_count=int(trim_count),
            min_coverage=max(1, int(min_coverage)),
            consensus_min_count=max(0, int(consensus_min_count)),
            start=clamped_start,
            end=clamped_end,
            top_k=max(1, int(top_k)),
        )
        _cache_put(
            _TOP_CACHE, cache_key, payload, TOP_CACHE_TTL_SECONDS, TOP_CACHE_MAX_ENTRIES
        )
        return {day: [dict(row) for row in rows] for day, rows in payload.items()}

    @staticmethod
    def _aggregate_sql(method: str, trim_count: int) -> tuple[str, str]:
        if method == "median":
            return (
                "percentile_cont(0.5) WITHIN GROUP (ORDER BY s.pct)",
                "",
            )
        if method == "trimmed":
            trim = max(1, int(trim_count))
            aggregate = (
                "(SELECT AVG(v) FROM ("
                "  SELECT v, ROW_NUMBER() OVER (ORDER BY v) AS rn, COUNT(*) OVER () AS c"
                "  FROM unnest(array_agg(s.pct)) AS u(v)"
                f") z WHERE z.rn > {trim} AND z.rn <= z.c - {trim})"
            )
            return aggregate, f"AND COUNT(*) >= {max(2 * trim + 1, 1)}"
        return "SUM(s.pct * w.weight) / SUM(w.weight)", ""

    def _query_top_rows(
        self,
        members: Sequence[PredMember],
        *,
        method: str,
        trim_count: int,
        min_coverage: int,
        consensus_min_count: int,
        start: date,
        end: date,
        top_k: int,
    ) -> dict[date, list[dict[str, Any]]]:
        files = ", ".join(f"'{_escape(member.path)}'" for member in members)
        file_map = ", ".join(
            f"('{_escape(member.path)}', '{_escape(member.model_id)}')"
            for member in members
        )
        weights = ", ".join(
            f"('{_escape(member.model_id)}', CAST({float(member.weight)!r} AS float8))"
            for member in members
        )
        aggregate, extra_having = self._aggregate_sql(method, trim_count)
        consensus_having = (
            f"AND COUNT(*) FILTER (WHERE s.pct >= {CONSENSUS_PCT}) "
            f">= {consensus_min_count}"
            if consensus_min_count > 0
            else ""
        )
        sql = f"""
            WITH files(filename, model_id) AS (VALUES {file_map}),
            raw AS (
              SELECT f.model_id AS model_id,
                     CAST(p.trade_date AS DATE) AS day,
                     {PREFIX_SYMBOL_SQL} AS symbol,
                     p.pred AS pred
              FROM read_parquet([{files}], union_by_name=true, filename=true) AS p
              JOIN files f ON f.filename = p.filename
              WHERE p.split = '{PRED_SPLIT}' AND p.pred IS NOT NULL
                AND CAST(p.trade_date AS DATE)
                    BETWEEN DATE '{start.isoformat()}' AND DATE '{end.isoformat()}'
            ),
            scored AS (
              SELECT model_id, day, symbol,
                     PERCENT_RANK() OVER (
                       PARTITION BY model_id, day ORDER BY pred
                     ) AS pct
              FROM raw
            ),
            weighted AS (
              SELECT day, symbol,
                     {aggregate} AS score,
                     COUNT(*) AS coverage,
                     COUNT(*) FILTER (WHERE s.pct >= {CONSENSUS_PCT}) AS consensus
              FROM scored s
              JOIN (VALUES {weights}) AS w(model_id, weight)
                ON w.model_id = s.model_id
              GROUP BY day, symbol
              HAVING COUNT(*) >= {int(min_coverage)}
                {extra_having}
                {consensus_having}
            ),
            ranked AS (
              SELECT day, symbol, score, coverage,
                     ROW_NUMBER() OVER (
                       PARTITION BY day ORDER BY score DESC, coverage DESC, symbol ASC
                     ) AS rn,
                     RANK() OVER (PARTITION BY day ORDER BY score DESC) AS score_rank,
                     RANK() OVER (PARTITION BY day ORDER BY score ASC) AS pct_rank,
                     COUNT(*) OVER (PARTITION BY day) AS n_total
              FROM weighted
            )
            SELECT day, symbol, score, coverage, rn, score_rank, pct_rank, n_total
            FROM ranked
            WHERE rn <= {int(top_k)}
            ORDER BY day, rn
        """
        con = _connect()
        try:
            rows = con.execute(sql).fetchall()
        except Exception as exc:  # noqa: BLE001 - 统一映射为数据不可用
            logger.warning("pred 组合聚合失败: %s", exc)
            raise BlendPredError(
                "pred_unavailable", "pred.parquet 聚合失败，无法生成组合历史"
            ) from exc
        finally:
            con.close()

        out: dict[date, list[dict[str, Any]]] = {}
        for day, symbol, score, coverage, rn, score_rank, pct_rank, n_total in rows:
            key = _as_date(day)
            pct = rank_pct_from_rank(int(pct_rank), int(n_total))
            out.setdefault(key, []).append(
                {
                    "symbol": str(symbol),
                    "score": float(score) if score is not None else None,
                    "rank": int(score_rank),
                    "row_rank": int(rn),
                    "coverage": int(coverage),
                    "pct": pct,
                    "position": position_of(pct),
                }
            )
        return out
