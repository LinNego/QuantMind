"""组合选股服务：成员解析、榜单、历史标注、纸面调仓计划。

分层：

- 纯计算在 ``blend_engine``（打分）与 ``blend_plan``（调仓裁定），本模块不重复实现；
- 本模块只做 IO 编排：取 run → 取分 → 折叠符号 → 调纯函数 → 补名称/行情 → 落库。

**符号口径**：``engine_signal_scores.symbol`` 实测并存三种写法（``600519`` /
``SH600519`` / ``600519.SH``，不同训练链写入）。组合必须先把成员分数折叠到
统一的前缀式再聚合，否则跨成员覆盖率会被写成 0（「同票不同码」）。
折叠规则与 ``StockCodeUtil.to_prefix`` 逐条对齐，SQL 侧表达式由
``test_blend_service`` 的集成用例对拍。

**边界**：只读写 ``qm_model_blend_*`` 纸面台账，不触碰 orders/sim_trades。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import time
from datetime import date, timedelta
from typing import Any
from collections.abc import Mapping, Sequence

from sqlalchemy import text

from backend.services.engine.inference.blend_engine import (
    BlendResult,
    BlendRow,
    combine_member_ranks,
    dispersion_band,
    position_of,
)
from backend.services.engine.inference.blend_plan import (
    DEFAULT_LOOKBACK,
    PaperPosition,
    PlanConfig,
    build_rebalance_plan,
    config_hash,
    consecutive_days_in_topk,
    mean_score,
    positions_hash,
)
from backend.services.engine.services import combo_auto
from backend.services.engine.services.model_blend_persistence import (
    ModelBlendPersistence,
)
from backend.shared.database_manager_v2 import get_session
from backend.shared.stock_utils import StockCodeUtil

logger = logging.getLogger(__name__)

MIN_MEMBERS = 2
#: T+5 全组模板实有 24 个同签名模型，上限必须容得下整组模板
MAX_MEMBERS = 30
CN_MARKET = "CN"
#: 行业/市值快照缓存（秒）：快照是最新截面，不按日分区
PROFILE_CACHE_TTL_SECONDS = 300.0
_PROFILE_CACHE: dict[str, tuple[float, dict[str, dict[str, Any]]]] = {}
#: ST/退市风险标记缓存：``market → (ts, flags, error)``；失败也缓存，避免读盘风暴
_RISK_CACHE: dict[str, tuple[float, dict[str, str], str | None]] = {}


def _prefix_symbol_sql(source: str = "symbol") -> str:
    """生成「任意写入形态 → 前缀式」的 SQL 表达式。

    与 ``StockCodeUtil.to_prefix`` 逐条对齐：先 ``upper(btrim(...))``（Python 侧
    同样先 ``upper().strip()``），再按后缀式 / 反向后缀 / 前缀式 / 纯 6 位识别。
    ``source`` 只在测试里换成 ``VALUES`` 列名，生产恒为 ``symbol``。
    """
    value = f"upper(btrim({source}))"
    return f"""
CASE
  WHEN {value} ~ '^[0-9]{{6}}\\.(SH|SZ|BJ)$' THEN
    substr({value}, 8, 2) || substr({value}, 1, 6)
  WHEN {value} ~ '^(SH|SZ|BJ)\\.[0-9]{{6}}$' THEN
    substr({value}, 1, 2) || substr({value}, 4, 6)
  WHEN {value} ~ '^(SH|SZ|BJ)[0-9]{{6}}$' THEN {value}
  WHEN {value} ~ '^[0-9]{{6}}$' THEN
    CASE
      WHEN substr({value}, 1, 2) IN ('60', '68', '90') THEN 'SH' || {value}
      WHEN substr({value}, 1, 2) IN ('00', '30', '20') THEN 'SZ' || {value}
      WHEN substr({value}, 1, 2) IN ('83', '43', '87', '88', '92') THEN 'BJ' || {value}
      ELSE {value}
    END
  ELSE {value}
END
""".strip()


#: 把任意写入形态折叠成前缀式（与 StockCodeUtil.to_prefix 对齐）。
PREFIX_SYMBOL_SQL = _prefix_symbol_sql()

_RUNS_CTE = """
runs AS (
  SELECT model_id, run_id, data_trade_date FROM (
    SELECT model_id, run_id, data_trade_date,
           ROW_NUMBER() OVER (
             PARTITION BY model_id, data_trade_date
             ORDER BY signals_count DESC, created_at DESC
           ) AS rn
    FROM qm_model_inference_runs
    WHERE tenant_id = :tenant_id AND user_id = :user_id
      AND model_id = ANY(:model_ids)
      AND data_trade_date BETWEEN :start AND :end
      AND status = 'completed' AND signals_count > 0
      AND fallback_used = FALSE
  ) x WHERE rn = 1
)
""".strip()

_SCORED_CTE = f"""
scored AS (
  SELECT r.data_trade_date, r.model_id, {PREFIX_SYMBOL_SQL} AS symbol,
         e.rank_pct AS pct
  FROM runs r
  JOIN engine_signal_scores e ON e.run_id = r.run_id
  WHERE e.tenant_id = :tenant_id AND e.user_id = :user_id
    AND e.rank_pct IS NOT NULL
)
""".strip()


class BlendError(Exception):
    """可映射为 HTTP 的业务错误（路由层统一转换）。"""

    def __init__(
        self, message: str, *, status_code: int = 400, code: str = "invalid_request"
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


def _parse_date(value: str | date | None) -> date | None:
    if value is None or isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise BlendError(f"日期格式必须是 YYYY-MM-DD：{value}") from exc


def _finite(value: Any) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _required_coverage(model_ids: Sequence[str], required: int | None) -> int:
    """共同数据日要求的最少成员数：``min(成员数, 指定门槛)``，至少 1。"""
    total = len({str(model_id) for model_id in model_ids})
    if total <= 0:
        return 1
    wanted = total if required is None else int(required)
    return max(1, min(total, wanted))


#: 窗口统计缓存：历史日的组合分与 TopK 在推理结果落定后不再变化，
#: 60s 内重复打开榜单/切换视图直接复用，避免每次重扫 ~70 万行。
#: 进程内小缓存（最多 32 个键）——OSS 单容器单进程；不写 Redis，避免占用
#: 已顶格的 maxmemory，也杜绝缓存故障拖慢主路径。
_WINDOW_CACHE_TTL_SECONDS = 60.0
_WINDOW_CACHE_MAX_ENTRIES = 32
_window_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _window_cache_key(
    *,
    tenant_id: str,
    user_id: str,
    members: Sequence[tuple[str, float]],
    method: str,
    trim_count: int,
    min_coverage: int,
    consensus_min_count: int,
    end_date: date,
    lookback: int,
    top_k: int,
    symbols: Sequence[str],
) -> str:
    raw = json.dumps(
        {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "members": sorted(
                (str(mid), round(float(weight), 12)) for mid, weight in members
            ),
            "method": method,
            "trim_count": int(trim_count),
            "min_coverage": int(min_coverage),
            "consensus_min_count": int(consensus_min_count),
            "end_date": end_date.isoformat(),
            "lookback": int(lookback),
            "top_k": int(top_k),
            "symbols": sorted(str(symbol) for symbol in symbols),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _clone_window(payload: Mapping[str, Any]) -> dict[str, Any]:
    """缓存命中返回副本：调用方可能把 ``history`` 挂到行上，防止串改。"""
    return {
        "scores": {
            day: dict(values) for day, values in (payload.get("scores") or {}).items()
        },
        "top": {day: set(values) for day, values in (payload.get("top") or {}).items()},
        "top_rows": {
            day: [dict(row) for row in rows]
            for day, rows in (payload.get("top_rows") or {}).items()
        },
        "days": list(payload.get("days") or []),
    }


def _window_cache_get(key: str) -> dict[str, Any] | None:
    entry = _window_cache.get(key)
    if entry is None:
        return None
    expires_at, payload = entry
    if expires_at <= time.monotonic():
        _window_cache.pop(key, None)
        return None
    return _clone_window(payload)


def _window_cache_put(key: str, payload: Mapping[str, Any]) -> None:
    now = time.monotonic()
    for stale in [
        cached for cached, (expires_at, _) in _window_cache.items() if expires_at <= now
    ]:
        _window_cache.pop(stale, None)
    while len(_window_cache) >= _WINDOW_CACHE_MAX_ENTRIES:
        _window_cache.pop(next(iter(_window_cache)))
    _window_cache[key] = (now + _WINDOW_CACHE_TTL_SECONDS, _clone_window(payload))


class ModelBlendService:
    """组合选股编排（每个请求新建即可，内部无状态）。"""

    def __init__(
        self,
        persistence: ModelBlendPersistence | None = None,
    ) -> None:
        self.persistence = persistence or ModelBlendPersistence()

    # ── 模型清单 ────────────────────────────────────────────────────────

    async def _normalized_models(
        self, *, tenant_id: str, user_id: str, market: str = CN_MARKET
    ) -> list[dict[str, Any]]:
        from backend.shared.model_registry import model_registry_service

        user_models = await model_registry_service.list_models(
            tenant_id=tenant_id, user_id=user_id, market=market
        )
        models = [combo_auto.normalize_model(row, source="user") for row in user_models]
        models.extend(
            combo_auto.normalize_model(row, source="system")
            for row in combo_auto.load_production_models()
        )
        return models

    async def eligible(
        self, *, tenant_id: str, user_id: str, market: str = CN_MARKET
    ) -> dict[str, Any]:
        models = await self._normalized_models(
            tenant_id=tenant_id, user_id=user_id, market=market
        )
        eligible, excluded = combo_auto.eligible_models(models)
        groups = combo_auto.group_models(eligible)
        payload_groups = []
        for group in groups:
            payload_groups.append(
                {
                    "key": group["key"],
                    "signature": group["signature"],
                    "models": [
                        {
                            "model_id": m["model_id"],
                            "display_name": m["display_name"],
                            "model_type": m["model_type"],
                            "model_source": m["model_source"],
                            "horizon_days": m["target_horizon_days"],
                            "feature_count": m["feature_count"],
                            "status": m["status"],
                        }
                        for m in group["models"]
                    ],
                }
            )
        return {
            "market": market.upper(),
            "groups": payload_groups,
            "excluded": excluded,
            "total_eligible": len(eligible),
        }

    async def auto_candidates(
        self, *, tenant_id: str, user_id: str, market: str = CN_MARKET
    ) -> dict[str, Any]:
        models = await self._normalized_models(
            tenant_id=tenant_id, user_id=user_id, market=market
        )
        runnable = await self._runnable_model_ids(
            tenant_id=tenant_id,
            user_id=user_id,
            model_ids=[str(m["model_id"]) for m in models],
        )
        evidence = combo_auto.load_evidence()
        return combo_auto.build_auto_candidates(models, evidence, runnable_ids=runnable)

    async def _runnable_model_ids(
        self, *, tenant_id: str, user_id: str, model_ids: Sequence[str]
    ) -> set[str] | None:
        """至少有过一次有效推理 run（completed / signals>0 / 非 fallback）的模型。

        模板只列「能产出信号」的成员：从未推理过的模型放进模板，榜单永远
        缺成员且日期被拖旧（K2）。查询失败不阻断清单（返回空集会让模板
        全部消失，反而更糟），降级为不过滤。
        """
        if not model_ids:
            return set()
        try:
            async with get_session(read_only=True) as session:
                rows = (
                    await session.execute(
                        text(
                            """
                            SELECT DISTINCT model_id FROM qm_model_inference_runs
                            WHERE tenant_id = :tenant_id AND user_id = :user_id
                              AND model_id = ANY(:model_ids)
                              AND status = 'completed' AND signals_count > 0
                              AND fallback_used = FALSE
                            """
                        ),
                        {
                            "tenant_id": tenant_id,
                            "user_id": user_id,
                            "model_ids": list(model_ids),
                        },
                    )
                ).all()
            return {str(row[0]) for row in rows}
        except Exception as exc:  # pragma: no cover - 查询失败降级为不过滤
            logger.warning("读取模型推理可用性失败，模板不做 run 过滤: %s", exc)
            return None

    # ── 成员解析与校验 ──────────────────────────────────────────────────

    async def _resolve_members(
        self,
        *,
        tenant_id: str,
        user_id: str,
        model_ids: Sequence[str],
        market: str,
        purpose: str,
    ) -> list[dict[str, Any]]:
        ordered = list(
            dict.fromkeys(str(m).strip() for m in model_ids if str(m).strip())
        )
        if len(ordered) < MIN_MEMBERS:
            raise BlendError(f"组合至少需要 {MIN_MEMBERS} 个模型")
        if len(ordered) > MAX_MEMBERS:
            raise BlendError(f"组合最多 {MAX_MEMBERS} 个模型")

        models = await self._normalized_models(
            tenant_id=tenant_id, user_id=user_id, market=market
        )
        by_id = {m["model_id"]: m for m in models}
        unknown = [model_id for model_id in ordered if model_id not in by_id]
        if unknown:
            raise BlendError(
                f"模型不存在或无权访问：{', '.join(unknown[:5])}", code="unknown_model"
            )
        eligible, excluded = combo_auto.eligible_models(models)
        eligible_ids = {m["model_id"] for m in eligible}
        blocked = {
            row["model_id"]: row["reason"]
            for row in excluded
            if row["model_id"] in ordered
        }
        if blocked:
            detail = "、".join(f"{k}({v})" for k, v in list(blocked.items())[:5])
            raise BlendError(f"模型不可参与组合：{detail}", code="model_not_eligible")
        if any(model_id not in eligible_ids for model_id in ordered):
            raise BlendError("存在不可参与组合的模型", code="model_not_eligible")

        members = [by_id[model_id] for model_id in ordered]
        signature = combo_auto.signature_of(members[0])
        for member in members[1:]:
            other = combo_auto.signature_of(member)
            # 周期不参与「同特征」判定：next_day 允许跨周期（附 mixed_horizon
            # 警告），rebalance 由下面的同周期校验单独拦。
            if any(
                other[key] != signature[key]
                for key in combo_auto.SIGNATURE_KEYS
                if key != "target_horizon_days"
            ):
                raise BlendError(
                    "所选模型不属于同一特征签名（factor_schema_hash/data_source/"
                    "feature_count/market 必须一致）",
                    code="signature_mismatch",
                )
        ModelBlendService._ensure_rebalance_horizon(members, purpose)
        return members

    @staticmethod
    def _ensure_rebalance_horizon(
        members: Sequence[Mapping[str, Any]], purpose: str
    ) -> None:
        """调仓组合允许任意周期（T+1…T+20），但成员必须同周期。

        新建、换成员、改用途三处共用同一口径；跨周期组合只允许
        ``next_day``（由 ``preview`` 附 ``mixed_horizon`` 警告）。
        """
        if purpose != "rebalance":
            return
        unknown = [
            str(m["model_id"]) for m in members if m.get("target_horizon_days") is None
        ]
        if unknown:
            raise BlendError(
                "调仓组合成员缺少预测周期：" + "、".join(unknown[:5]),
                code="horizon_unknown",
            )
        horizons = {int(m["target_horizon_days"]) for m in members}
        if len(horizons) > 1:
            raise BlendError(
                "调仓组合成员必须同预测周期："
                + "、".join(f"T+{h}" for h in sorted(horizons)),
                code="mixed_horizon",
            )

    @staticmethod
    def _validate_params(
        members: Sequence[Mapping[str, Any]],
        *,
        min_coverage: int,
        consensus_min_count: int,
    ) -> None:
        n = len(members)
        if not 2 <= int(min_coverage) <= n:
            raise BlendError(f"min_coverage 必须在 [2, {n}]")
        if not 0 <= int(consensus_min_count) <= n:
            raise BlendError(f"consensus_min_count 必须在 [0, {n}]")

    # ── 取数 ────────────────────────────────────────────────────────────

    async def latest_common_date(
        self,
        *,
        tenant_id: str,
        user_id: str,
        model_ids: Sequence[str],
        required: int | None = None,
    ) -> date | None:
        """最近一个「至少 ``required`` 个成员有可用 run」的交易日。

        缺省 ``required = len(model_ids)``（全员）；调仓组合按
        ``min_coverage`` 传入，避免 24 成员 T+5 全组因个别模型没补推而永远无日期。
        缺成员由 ``_combine_runs`` 继续产出 ``缺少当日推理`` 警告。
        """
        required = _required_coverage(model_ids, required)
        async with get_session(read_only=True) as session:
            row = (
                await session.execute(
                    text(
                        f"""
                        WITH {_RUNS_CTE}
                        SELECT data_trade_date,
                               COUNT(DISTINCT model_id) AS n_models
                        FROM runs
                        GROUP BY data_trade_date
                        HAVING COUNT(DISTINCT model_id) >= :required
                        ORDER BY data_trade_date DESC
                        LIMIT 1
                        """
                    ),
                    {
                        "tenant_id": tenant_id,
                        "user_id": user_id,
                        "model_ids": list(model_ids),
                        "start": date(2000, 1, 1),
                        "end": date(2100, 1, 1),
                        "required": required,
                    },
                )
            ).first()
        return row[0] if row else None

    async def _runs_for_date(
        self,
        *,
        tenant_id: str,
        user_id: str,
        model_ids: Sequence[str],
        data_trade_date: date,
    ) -> dict[str, str]:
        async with get_session(read_only=True) as session:
            rows = (
                await session.execute(
                    text(
                        f"""
                        WITH {_RUNS_CTE}
                        SELECT model_id, run_id FROM runs
                        """
                    ),
                    {
                        "tenant_id": tenant_id,
                        "user_id": user_id,
                        "model_ids": list(model_ids),
                        "start": data_trade_date,
                        "end": data_trade_date,
                    },
                )
            ).all()
        return {str(model_id): str(run_id) for model_id, run_id in rows}

    async def _scores_for_runs(
        self,
        *,
        tenant_id: str,
        user_id: str,
        runs: Mapping[str, str],
    ) -> dict[str, dict[str, float]]:
        """``model_id → {prefix_symbol: pct}``；同票多写法保留最高分那条。"""
        if not runs:
            return {}
        run_to_model = {run_id: model_id for model_id, run_id in runs.items()}
        async with get_session(read_only=True) as session:
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT run_id, symbol, fusion_score,
                               PERCENT_RANK() OVER (
                                 PARTITION BY run_id ORDER BY fusion_score
                               ) AS pct
                        FROM engine_signal_scores
                        WHERE run_id = ANY(:run_ids)
                          AND tenant_id = :tenant_id AND user_id = :user_id
                          AND fusion_score <> 'NaN'::float8
                          AND fusion_score <> 'Infinity'::float8
                          AND fusion_score <> '-Infinity'::float8
                        """
                    ),
                    {
                        "run_ids": list(runs.values()),
                        "tenant_id": tenant_id,
                        "user_id": user_id,
                    },
                )
            ).all()
        ranked: dict[str, dict[str, tuple[float, float]]] = {}
        for run_id, symbol, fusion_score, pct in rows:
            model_id = run_to_model.get(str(run_id))
            if model_id is None or not _finite(pct):
                continue
            prefix = StockCodeUtil.to_prefix(str(symbol)) or str(symbol)
            current = ranked.setdefault(model_id, {}).get(prefix)
            score = float(fusion_score if fusion_score is not None else 0.0)
            if current is None or score > current[1]:
                ranked[model_id][prefix] = (float(pct), score)
        return {
            model_id: {symbol: value[0] for symbol, value in scores.items()}
            for model_id, scores in ranked.items()
        }

    async def _combine_runs(
        self,
        *,
        tenant_id: str,
        user_id: str,
        members: Sequence[Mapping[str, Any]],
        data_trade_date: date,
        method: str,
        weights: Mapping[str, float] | None,
        min_coverage: int,
        trim_count: int,
        consensus_min_count: int,
    ) -> tuple[BlendResult, dict[str, str], list[str]]:
        model_ids = [str(m["model_id"]) for m in members]
        runs = await self._runs_for_date(
            tenant_id=tenant_id,
            user_id=user_id,
            model_ids=model_ids,
            data_trade_date=data_trade_date,
        )
        missing = [model_id for model_id in model_ids if model_id not in runs]
        ranks = await self._scores_for_runs(
            tenant_id=tenant_id, user_id=user_id, runs=runs
        )
        result = combine_member_ranks(
            ranks,
            model_ids,
            method=method,
            weights=weights,
            min_coverage=min_coverage,
            trim=trim_count,
            consensus_min_count=consensus_min_count,
        )
        warnings = [f"缺少当日推理:{model_id}" for model_id in missing]
        return result, runs, warnings

    # ── 榜单 ────────────────────────────────────────────────────────────

    def _row_payload(
        self,
        row: BlendRow,
        names: Mapping[str, str],
        profiles: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        profile = (profiles or {}).get(row.symbol) or {}
        return {
            "code": row.symbol,
            "symbol": row.symbol,
            "name": names.get(row.symbol) or "",
            # 缺失一律 None（前端显示 —），绝不把未知市值写成 0
            "industry": profile.get("industry"),
            "market_cap_yi": profile.get("market_cap_yi"),
            "S": row.score,
            "S_pct": row.pct,
            "score": row.score,
            "pct": row.pct,
            "rank": row.rank,
            "research_score": row.research_score,
            "position": row.position,
            "coverage": row.coverage,
            "dispersion": row.dispersion,
            # 分歧档位由后端唯一口径给出（前端不再自带阈值，避免两套算法漂移）
            "dispersion_band": dispersion_band(row.dispersion),
            "contributions": dict(row.contributions),
            "missing_members": list(row.missing_members),
        }

    def _member_payload(
        self,
        members: Sequence[Mapping[str, Any]],
        runs: Mapping[str, str],
        weights: Mapping[str, float] | None,
    ) -> list[dict[str, Any]]:
        return [
            {
                "model_id": m["model_id"],
                "display_name": m["display_name"],
                "model_source": m["model_source"],
                "model_type": m["model_type"],
                "horizon_days": m["target_horizon_days"],
                "weight": float((weights or {}).get(m["model_id"], 1.0)),
                "available": m["model_id"] in runs,
                "run_id": runs.get(m["model_id"]),
            }
            for m in members
        ]

    async def ranking(
        self,
        *,
        tenant_id: str,
        user_id: str,
        members: Sequence[Mapping[str, Any]],
        data_trade_date: date,
        method: str = "mean",
        weights: Mapping[str, float] | None = None,
        min_coverage: int = 2,
        trim_count: int = 2,
        consensus_min_count: int = 0,
        top: int = 500,
        blend_id: str | None = None,
        annotate: bool = False,
    ) -> dict[str, Any]:
        result, runs, warnings = await self._combine_runs(
            tenant_id=tenant_id,
            user_id=user_id,
            members=members,
            data_trade_date=data_trade_date,
            method=method,
            weights=weights,
            min_coverage=min_coverage,
            trim_count=trim_count,
            consensus_min_count=consensus_min_count,
        )
        names = await self._stock_names()
        market = str(members[0].get("market") or CN_MARKET) if members else CN_MARKET
        profiles = await self._stock_profiles(market)
        rows = [
            self._row_payload(row, names, profiles)
            for row in result.rows[: max(1, top)]
        ]
        if annotate:
            window = await self._window_stats(
                tenant_id=tenant_id,
                user_id=user_id,
                members=members,
                weights=weights,
                method=method,
                trim_count=trim_count,
                min_coverage=min_coverage,
                consensus_min_count=consensus_min_count,
                end_date=data_trade_date,
                lookback=10,
                top_k=50,
                symbols=[row["symbol"] for row in rows],
            )
            annotations = await self._annotations(
                blend_id=blend_id,
                rows=rows,
                window=window,
                data_trade_date=data_trade_date,
            )
            for row in rows:
                row["history"] = annotations.get(row["symbol"], {})
        return {
            "data_trade_date": data_trade_date.isoformat(),
            "members": self._member_payload(members, runs, weights),
            "ranking": rows,
            "stats": dict(result.stats),
            "warnings": warnings,
        }

    async def preview(
        self,
        *,
        tenant_id: str,
        user_id: str,
        model_ids: Sequence[str],
        market: str = CN_MARKET,
        data_trade_date: date | None = None,
        purpose: str = "rebalance",
        method: str = "mean",
        trim_count: int = 2,
        weights: Mapping[str, float] | None = None,
        top_k: int = 50,
        min_coverage: int = 2,
        consensus_min_count: int = 0,
        top: int = 200,
    ) -> dict[str, Any]:
        members = await self._resolve_members(
            tenant_id=tenant_id,
            user_id=user_id,
            model_ids=model_ids,
            market=market,
            purpose=purpose,
        )
        self._validate_params(
            members,
            min_coverage=min_coverage,
            consensus_min_count=consensus_min_count,
        )
        _validate_weights(members, weights)
        target = data_trade_date or await self.latest_common_date(
            tenant_id=tenant_id,
            user_id=user_id,
            model_ids=[str(m["model_id"]) for m in members],
            required=min_coverage,
        )
        if target is None:
            raise BlendError(
                "所选模型没有共同数据日：请先对成员做串行补推，或调低最低覆盖数",
                code="no_common_date",
            )
        payload = await self.ranking(
            tenant_id=tenant_id,
            user_id=user_id,
            members=members,
            data_trade_date=target,
            method=method,
            weights=weights,
            min_coverage=min_coverage,
            trim_count=trim_count,
            consensus_min_count=consensus_min_count,
            top=max(top, top_k),
        )
        payload["data_trade_date"] = target.isoformat()
        payload["signature"] = combo_auto.signature_of(members[0])
        payload["target_date"] = (await self._next_trading_day(target)).isoformat()
        payload["purpose"] = purpose
        if (
            purpose == "next_day"
            and len({m["target_horizon_days"] for m in members}) > 1
        ):
            payload["warnings"].append("mixed_horizon")
        payload["ranking"] = payload["ranking"][: max(1, top)]
        return payload

    async def available_dates(
        self,
        blend: Mapping[str, Any],
        *,
        tenant_id: str,
        user_id: str,
        limit: int = 60,
    ) -> list[str]:
        members = await self.members_from_blend(blend)
        model_ids = [str(m["model_id"]) for m in members]
        required = _required_coverage(model_ids, int(blend.get("min_coverage") or 2))
        async with get_session(read_only=True) as session:
            rows = (
                await session.execute(
                    text(
                        f"""
                        WITH {_RUNS_CTE}
                        SELECT data_trade_date, COUNT(DISTINCT model_id) AS n_models
                        FROM runs
                        GROUP BY data_trade_date
                        HAVING COUNT(DISTINCT model_id) >= :required
                        ORDER BY data_trade_date DESC
                        LIMIT :limit
                        """
                    ),
                    {
                        "tenant_id": tenant_id,
                        "user_id": user_id,
                        "model_ids": model_ids,
                        "start": date(2000, 1, 1),
                        "end": date(2100, 1, 1),
                        "required": required,
                        "limit": max(1, min(365, limit)),
                    },
                )
            ).all()
        return [str(row[0]) for row in rows]

    async def rolling(
        self,
        blend: Mapping[str, Any],
        *,
        tenant_id: str,
        user_id: str,
        end_date: date | None = None,
        window: int = 10,
        per_day: int = 5,
    ) -> dict[str, Any]:
        """滚动建仓视图：最近 ``window+1`` 个交易日，每日 Top ``per_day``。

        选中口径与榜单完全同源（组合分排序），不另算一套；
        ``held_days`` 只统计**本窗口内**连续在榜天数（超出窗口的历史见榜单
        ``history``/纸面台账，不在这里猜）。
        """
        if not 1 <= int(window) <= 60:
            raise BlendError("滚窗必须在 [1, 60]")
        if not 1 <= int(per_day) <= 50:
            raise BlendError("每日只数必须在 [1, 50]")
        members = await self.members_from_blend(blend)
        if len(members) < MIN_MEMBERS:
            raise BlendError(f"组合至少需要 {MIN_MEMBERS} 个模型")
        weights = _weights_from_members(blend, members)
        member_ids = [str(m["model_id"]) for m in members]
        min_coverage = int(blend.get("min_coverage") or 2)
        target = end_date or await self.latest_common_date(
            tenant_id=tenant_id,
            user_id=user_id,
            model_ids=member_ids,
            required=min_coverage,
        )
        if target is None:
            raise BlendError(
                "所选模型没有共同数据日：请先对成员做串行补推，或调低最低覆盖数",
                code="no_common_date",
            )

        stats = await self._window_stats(
            tenant_id=tenant_id,
            user_id=user_id,
            members=members,
            weights=weights,
            method=str(blend.get("method") or "mean"),
            trim_count=int(blend.get("trim_count") or 2),
            min_coverage=min_coverage,
            consensus_min_count=int(blend.get("consensus_min_count") or 0),
            end_date=target,
            lookback=int(window),
            top_k=int(per_day),
            symbols=[],
        )
        names = await self._stock_names()
        profiles = await self._stock_profiles(str(blend.get("market") or CN_MARKET))
        rows_by_day = stats.get("top_rows") or {}
        days = sorted(day for day in rows_by_day if day <= target)[-(int(window) + 1) :]

        streak: dict[str, int] = {}
        previous: set[str] = set()
        columns: list[dict[str, Any]] = []
        for day in days:
            picks: list[dict[str, Any]] = []
            today: set[str] = set()
            for row in (rows_by_day.get(day) or [])[: int(per_day)]:
                symbol = str(row["symbol"])
                today.add(symbol)
                held = streak.get(symbol, 0) + 1
                streak[symbol] = held
                profile = profiles.get(symbol) or {}
                picks.append(
                    {
                        **row,
                        "code": symbol,
                        "name": names.get(symbol) or "",
                        "industry": profile.get("industry"),
                        "market_cap_yi": profile.get("market_cap_yi"),
                        "held_days": held,
                        "is_new": held == 1,
                    }
                )
            columns.append(
                {
                    "date": day.isoformat(),
                    "picks": picks,
                    "exits": sorted(previous - today),
                }
            )
            # 断榜后再次入选按「新进」计，不伪造连续持有
            streak = {
                symbol: count for symbol, count in streak.items() if symbol in today
            }
            previous = today

        latest = columns[-1] if columns else {"picks": [], "exits": []}
        return {
            "blend_id": str(blend["blend_id"]),
            "data_trade_date": target.isoformat(),
            "target_date": (await self._next_trading_day(target)).isoformat(),
            "window": int(window),
            "per_day": int(per_day),
            "days": [day.isoformat() for day in days],
            "columns": columns,
            "members": self._member_payload(members, {}, weights),
            "summary": {
                "unique_symbols": len(
                    {pick["symbol"] for column in columns for pick in column["picks"]}
                ),
                "new_today": sum(1 for pick in latest["picks"] if pick["is_new"]),
                "exited_today": len(latest["exits"]),
            },
            "warnings": [],
        }

    async def create_blend(
        self,
        *,
        tenant_id: str,
        user_id: str,
        name: str,
        model_ids: Sequence[str],
        market: str = CN_MARKET,
        purpose: str = "rebalance",
        method: str = "mean",
        trim_count: int = 2,
        weights: Mapping[str, float] | None = None,
        min_coverage: int = 8,
        consensus_min_count: int = 0,
        entry_per_day: int = 5,
        rolling_window: int = 10,
        target_positions: int = 50,
        source: str = "manual",
        template_id: str | None = None,
    ) -> dict[str, Any]:
        clean_name = combo_auto.template_slug(name)
        if not clean_name:
            raise BlendError("组合名称不能为空")
        members = await self._resolve_members(
            tenant_id=tenant_id,
            user_id=user_id,
            model_ids=model_ids,
            market=market,
            purpose=purpose,
        )
        self._validate_params(
            members,
            min_coverage=min_coverage,
            consensus_min_count=consensus_min_count,
        )
        _validate_weights(members, weights)
        weighting = "equal" if weights is None else "manual"
        signature = combo_auto.signature_of(members[0])
        return await self.persistence.create_blend(
            tenant_id=tenant_id,
            user_id=user_id,
            name=clean_name,
            market=str(signature["market"]),
            purpose=purpose,
            source=source,
            template_id=template_id,
            horizon_days=int(signature["target_horizon_days"] or 10),
            weighting=weighting,
            method=method,
            trim_count=int(trim_count),
            min_coverage=int(min_coverage),
            consensus_min_count=int(consensus_min_count),
            entry_per_day=int(entry_per_day),
            rolling_window=int(rolling_window),
            target_positions=int(target_positions),
            feature_signature=str(signature["factor_schema_hash"]),
            data_source=str(signature["data_source"]),
            members=[
                (
                    str(m["model_id"]),
                    str(m["model_source"]),
                    float((weights or {}).get(m["model_id"], 1.0)),
                )
                for m in members
            ],
        )

    async def update_blend(
        self,
        blend: Mapping[str, Any],
        *,
        tenant_id: str,
        user_id: str,
        fields: Mapping[str, Any],
        model_ids: Sequence[str] | None,
        weights: Mapping[str, float] | None,
    ) -> dict[str, Any]:
        clean: dict[str, Any] = {}
        if "name" in fields:
            name = combo_auto.template_slug(str(fields["name"]))
            if not name:
                raise BlendError("组合名称不能为空")
            clean["name"] = name
        for key in (
            "purpose",
            "method",
            "trim_count",
            "min_coverage",
            "consensus_min_count",
            "entry_per_day",
            "rolling_window",
            "target_positions",
        ):
            if key in fields and fields[key] is not None:
                clean[key] = fields[key]
        if weights is not None and model_ids is None:
            # 权重只在重算成员时落库；不拦就会静默丢弃并返回 200
            raise BlendError(
                "修改权重必须同时提交 model_ids",
                code="weights_require_members",
            )
        members_payload = None
        resolved_members: list[dict[str, Any]] | None = None
        if model_ids is not None:
            members = await self._resolve_members(
                tenant_id=tenant_id,
                user_id=user_id,
                model_ids=model_ids,
                market=str(blend.get("market") or CN_MARKET),
                purpose=str(
                    clean.get("purpose") or blend.get("purpose") or "rebalance"
                ),
            )
            _validate_weights(members, weights)
            resolved_members = list(members)
            members_payload = [
                (
                    str(m["model_id"]),
                    str(m["model_source"]),
                    float((weights or {}).get(m["model_id"], 1.0)),
                )
                for m in members
            ]
            clean["weighting"] = "equal" if weights is None else "manual"
            signature = combo_auto.signature_of(members[0])
            clean["feature_signature"] = str(signature["factor_schema_hash"])
            clean["data_source"] = str(signature["data_source"])
            clean["horizon_days"] = int(signature["target_horizon_days"] or 10)
        elif clean.get("purpose") == "rebalance":
            # 只改用途不换成员：存量成员同样要过「同周期」校验
            self._ensure_rebalance_horizon(
                await self.members_from_blend(blend), "rebalance"
            )
        merged_min = int(clean.get("min_coverage", blend.get("min_coverage") or 2))
        merged_consensus = int(
            clean.get("consensus_min_count", blend.get("consensus_min_count") or 0)
        )
        member_count = len(resolved_members or blend.get("members") or [])
        if resolved_members is not None:
            self._validate_params(
                resolved_members,
                min_coverage=merged_min,
                consensus_min_count=merged_consensus,
            )
        elif (
            not 2 <= merged_min <= member_count
            or not 0 <= merged_consensus <= member_count
        ):
            raise BlendError("门槛参数超出成员数范围")
        await self.persistence.update_blend(
            str(blend["blend_id"]),
            tenant_id=tenant_id,
            user_id=user_id,
            fields=clean,
            members=members_payload,
        )
        return (
            await self.persistence.get_blend(
                str(blend["blend_id"]), tenant_id=tenant_id, user_id=user_id
            )
            or {}
        )

    @staticmethod
    async def _stock_names() -> dict[str, str]:
        try:
            from backend.services.api.routers.model_training import (
                _load_quantdb_stock_names,
            )

            return _load_quantdb_stock_names()
        except Exception as exc:  # pragma: no cover - 名称缺失不影响主流程
            logger.warning("组合榜单读取股票简称失败: %s", exc)
            return {}

    @staticmethod
    async def _stock_profiles(
        market: str = CN_MARKET,
    ) -> dict[str, dict[str, Any]]:
        """``前缀式 symbol → {industry, market_cap_yi}``（5 分钟进程内缓存）。

        口径：``stock_snapshot`` 是**当前截面**，不是按日快照；历史日期榜单的
        行业/市值只能标「当前」，不得用于回测归因。缺失/NaN 一律 None。
        """
        key = str(market or CN_MARKET).upper()
        now = time.monotonic()
        cached = _PROFILE_CACHE.get(key)
        if cached is not None and now - cached[0] < PROFILE_CACHE_TTL_SECONDS:
            return cached[1]
        try:
            from backend.services.engine.factor_research import store

            # parquet 读取是阻塞 IO：放到线程池，避免卡住 API/engine 事件循环
            frame = await asyncio.to_thread(store.stock_snapshot, key)
        except Exception as exc:  # pragma: no cover - 缺失不影响榜单主流程
            logger.warning("组合榜单读取行业/市值失败(%s): %s", key, exc)
            # 失败也缓存，短暂窗口内不再反复读盘/刷日志
            _PROFILE_CACHE[key] = (now, {})
            return {}
        profiles: dict[str, dict[str, Any]] = {}
        if frame is not None and not getattr(frame, "empty", True):
            has_industry = "industry" in frame.columns
            has_mv = "total_mv_yi" in frame.columns
            for _, row in frame.iterrows():
                prefix = StockCodeUtil.to_prefix(str(row.get("symbol") or ""))
                if not prefix:
                    continue
                industry = row.get("industry") if has_industry else None
                market_cap = row.get("total_mv_yi") if has_mv else None
                profiles[prefix] = {
                    "industry": industry.strip()
                    if isinstance(industry, str) and industry.strip()
                    else None,
                    "market_cap_yi": float(market_cap) if _finite(market_cap) else None,
                }
        _PROFILE_CACHE[key] = (now, profiles)
        if len(_PROFILE_CACHE) > 8:  # 市场数量有限；防御性上限
            oldest = min(_PROFILE_CACHE.items(), key=lambda item: item[1][0])[0]
            _PROFILE_CACHE.pop(oldest, None)
        return profiles

    # ── 历史标注 ────────────────────────────────────────────────────────

    async def _recent_run_dates(
        self,
        *,
        tenant_id: str,
        user_id: str,
        model_ids: Sequence[str],
        end_date: date,
        limit: int,
    ) -> list[date]:
        """最近 ``limit`` 个有可用推理的交易日（降序）。

        只用于给 ``_window_stats`` 定左界。旧实现按自然日外推 40 天，
        会白扫 2~4 倍的 run/score 行；这里精确截到第 ``lookback + 1`` 个交易日。
        """
        async with get_session(read_only=True) as session:
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT DISTINCT data_trade_date
                        FROM qm_model_inference_runs
                        WHERE tenant_id = :tenant_id AND user_id = :user_id
                          AND model_id = ANY(:model_ids)
                          AND data_trade_date <= :end
                          AND status = 'completed' AND signals_count > 0
                          AND fallback_used = FALSE
                        ORDER BY data_trade_date DESC
                        LIMIT :limit
                        """
                    ),
                    {
                        "tenant_id": tenant_id,
                        "user_id": user_id,
                        "model_ids": list(model_ids),
                        "end": end_date,
                        "limit": max(1, int(limit)),
                    },
                )
            ).all()
        return [
            row[0] if isinstance(row[0], date) else date.fromisoformat(str(row[0]))
            for row in rows
        ]

    async def _window_stats(
        self,
        *,
        tenant_id: str,
        user_id: str,
        members: Sequence[Mapping[str, Any]],
        weights: Mapping[str, float] | None,
        method: str,
        trim_count: int,
        min_coverage: int,
        consensus_min_count: int,
        end_date: date,
        lookback: int,
        top_k: int,
        symbols: Sequence[str],
    ) -> dict[str, Any]:
        """窗口内每日组合分与 TopK 集合（只回候选与榜首，控制传输量）。

        分位与聚合都在 SQL 内完成；``method`` 只影响聚合表达式，不拼接用户输入。
        """
        model_ids = [str(m["model_id"]) for m in members]
        ordered_weights = [
            float((weights or {}).get(model_id, 1.0)) for model_id in model_ids
        ]
        equal_weights = all(
            abs(weight - ordered_weights[0]) < 1e-12 for weight in ordered_weights
        )
        weight_join = ""
        weight_params: dict[str, Any] = {}
        if method == "median":
            aggregate = "percentile_cont(0.5) WITHIN GROUP (ORDER BY s.pct)"
            extra_having = ""
        elif method == "trimmed":
            aggregate = (
                "(SELECT AVG(v) FROM ("
                "  SELECT v, ROW_NUMBER() OVER (ORDER BY v) AS rn, COUNT(*) OVER () AS c"
                "  FROM unnest(array_agg(s.pct)) AS u(v)"
                ") z WHERE z.rn > :trim_count AND z.rn <= z.c - :trim_count)"
            )
            extra_having = "AND COUNT(*) >= :min_rows"
        else:
            extra_having = ""
            if equal_weights:
                # 等权是默认面：直接用 AVG，省掉每行 13 次的 jsonb 展开。
                aggregate = "AVG(s.pct)"
            else:
                aggregate = (
                    "SUM(s.pct * CAST(w.weight AS float8))"
                    " / SUM(CAST(w.weight AS float8))"
                )
                rows = ", ".join(
                    f"(:wm{i}, CAST(:ww{i} AS float8))" for i in range(len(model_ids))
                )
                weight_join = (
                    f"JOIN (VALUES {rows}) AS w(model_id, weight)"
                    " ON w.model_id = s.model_id"
                )
                for index, (model_id, weight) in enumerate(
                    zip(model_ids, ordered_weights, strict=True)
                ):
                    weight_params[f"wm{index}"] = model_id
                    weight_params[f"ww{index}"] = weight
        consensus_having = (
            "AND COUNT(*) FILTER (WHERE s.pct >= 0.70) >= :consensus_min_count"
            if consensus_min_count > 0
            else ""
        )
        cache_key = _window_cache_key(
            tenant_id=tenant_id,
            user_id=user_id,
            members=list(zip(model_ids, ordered_weights, strict=True)),
            method=method,
            trim_count=trim_count,
            min_coverage=min_coverage,
            consensus_min_count=consensus_min_count,
            end_date=end_date,
            lookback=lookback,
            top_k=top_k,
            symbols=symbols,
        )
        cached = _window_cache_get(cache_key)
        if cached is not None:
            return cached
        sql = f"""
            WITH {_RUNS_CTE},
            {_SCORED_CTE},
            weighted AS (
              SELECT s.data_trade_date, s.symbol,
                     {aggregate} AS score,
                     COUNT(*) AS coverage
              FROM scored s
              {weight_join}
              GROUP BY s.data_trade_date, s.symbol
              HAVING COUNT(*) >= :min_coverage
                {extra_having}
                {consensus_having}
            ),
            ranked AS (
              SELECT data_trade_date, symbol, score, coverage,
                     ROW_NUMBER() OVER (
                       PARTITION BY data_trade_date
                       ORDER BY score DESC, coverage DESC, symbol ASC
                     ) AS rn,
                     RANK() OVER (
                       PARTITION BY data_trade_date ORDER BY score DESC
                     ) AS score_rank,
                     COUNT(*) OVER (PARTITION BY data_trade_date) AS n_total
              FROM weighted
            )
            SELECT data_trade_date, symbol, score, coverage, rn, score_rank, n_total
            FROM ranked
            WHERE rn <= :top_k OR symbol = ANY(:symbols)
            ORDER BY data_trade_date, rn
        """
        run_dates = await self._recent_run_dates(
            tenant_id=tenant_id,
            user_id=user_id,
            model_ids=model_ids,
            end_date=end_date,
            limit=lookback + 1,
        )
        if not run_dates:
            return {"scores": {}, "top": {}, "top_rows": {}, "days": []}
        start = min(run_dates)
        async with get_session(read_only=True) as session:
            rows = (
                await session.execute(
                    text(sql),
                    {
                        "tenant_id": tenant_id,
                        "user_id": user_id,
                        "model_ids": model_ids,
                        "start": start,
                        "end": end_date,
                        "min_coverage": max(1, int(min_coverage)),
                        "min_rows": max(int(min_coverage), 2 * trim_count + 1),
                        "trim_count": int(trim_count),
                        "consensus_min_count": int(consensus_min_count),
                        "top_k": int(top_k),
                        "symbols": list(symbols),
                        **weight_params,
                    },
                )
            ).all()

        by_date: dict[date, dict[str, float]] = {}
        top_by_date: dict[date, set[str]] = {}
        top_rows_by_date: dict[date, list[dict[str, Any]]] = {}
        for day, symbol, score, coverage, rn, score_rank, n_total in rows:
            key = day if isinstance(day, date) else date.fromisoformat(str(day))
            symbol = str(symbol)
            if _finite(score):
                by_date.setdefault(key, {})[symbol] = float(score)
            if int(rn) <= top_k:
                top_by_date.setdefault(key, set()).add(symbol)
                # pct 与 blend_engine.percent_rank_map 同口径：(最小名次-1)/(n-1)；
                # 缺总数为 1 时按 0.0（与 compute_rank_pct 的 n==1 分支一致）。
                pct = (
                    0.0
                    if int(n_total) <= 1
                    else (int(score_rank) - 1) / (int(n_total) - 1)
                )
                top_rows_by_date.setdefault(key, []).append(
                    {
                        "symbol": symbol,
                        "score": float(score) if _finite(score) else None,
                        "rank": int(score_rank),
                        "row_rank": int(rn),
                        "coverage": int(coverage),
                        "pct": pct,
                        "position": position_of(pct),
                    }
                )
        score_days = sorted(day for day in by_date if day < end_date)[-lookback:]
        top_days = sorted(day for day in top_by_date if day <= end_date)[
            -(lookback + 1) :
        ]
        payload = {
            "scores": {day: by_date[day] for day in score_days},
            "top": {day: top_by_date[day] for day in top_days},
            "top_rows": {
                day: sorted(
                    top_rows_by_date.get(day, []), key=lambda row: row["row_rank"]
                )
                for day in top_days
            },
            "days": score_days,
        }
        _window_cache_put(cache_key, payload)
        return _clone_window(payload)

    async def _annotations(
        self,
        *,
        blend_id: str | None,
        rows: Sequence[Mapping[str, Any]],
        window: Mapping[str, Any] | None,
        data_trade_date: date,
    ) -> dict[str, dict[str, Any]]:
        positions = (
            await self.persistence.list_position_history(blend_id) if blend_id else []
        )
        trading_days = await self._trading_days(data_trade_date, need=60)
        by_symbol: dict[str, list[dict[str, Any]]] = {}
        for position in positions:
            by_symbol.setdefault(str(position["symbol"]), []).append(position)
        top = (window or {}).get("top") or {}
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            symbol = str(row["symbol"])
            history = sorted(
                by_symbol.get(symbol, []), key=lambda p: str(p["entry_date"])
            )
            open_rows = [
                p
                for p in history
                if p["entry_date"] <= data_trade_date.isoformat()
                and (
                    p.get("exit_date") is None
                    or p["exit_date"] > data_trade_date.isoformat()
                )
            ]
            held_days = 0
            if open_rows:
                try:
                    entry = date.fromisoformat(str(open_rows[0]["entry_date"]))
                    held_days = sum(
                        1 for day in trading_days if entry < day <= data_trade_date
                    )
                except ValueError:
                    held_days = 0
            out[symbol] = {
                "first_entry_date": history[0]["entry_date"] if history else None,
                "last_entry_date": history[-1]["entry_date"] if history else None,
                "reentry_count": max(0, len(history) - 1),
                "held_days": held_days,
                "consecutive_days_in_top50": consecutive_days_in_topk(
                    top, symbol=symbol, date=data_trade_date
                ),
            }
        return out

    # ── 调仓计划 ────────────────────────────────────────────────────────

    @staticmethod
    async def _trading_days(end: date, *, need: int) -> list[date]:
        start = end - timedelta(days=max(30, need * 3))
        try:
            from backend.shared.trading_calendar import trading_days_xcal

            days = trading_days_xcal("CN", start, end + timedelta(days=1))
        except Exception:  # pragma: no cover - 日历不可用时退化为自然日
            days = None
        if not days:
            days = [
                start + timedelta(days=offset)
                for offset in range((end - start).days + 1)
            ]
        return [day for day in days if day <= end]

    @staticmethod
    async def _quotes(
        symbols: Sequence[str],
        data_trade_date: date,
        *,
        lookback_days: int = 20,
        min_adv_days: int = 15,
    ) -> tuple[dict[str, float], dict[str, float], list[str]]:
        """T 日参考价 + ADV20（元）。

        返回 ``(prices, avg_amounts, warnings)``：``prices`` 只取 T 日收盘价；
        ``avg_amounts`` 是最近 ``lookback_days`` 个**有成交额**交易日的均值，
        有效日不足 ``min_adv_days`` 的标的不进映射（计划层会按「成交额缺失」拦截）。
        """
        suffix_to_prefix = {}
        for symbol in symbols:
            suffix = StockCodeUtil.to_suffix(symbol)
            if suffix:
                suffix_to_prefix[suffix] = symbol
        if not suffix_to_prefix:
            return {}, {}, []
        start = data_trade_date - timedelta(days=max(45, int(lookback_days) * 3))
        try:
            from backend.services.engine.data_platform.quantdb_hub import (
                QuantDBDataHub,
            )

            hub = QuantDBDataHub.get_instance()
            if hub is None or not hub.available:
                return {}, {}, ["行情源不可用，计划未校验价格与容量"]
            frame = hub.fetch_daily_kline_batch(
                list(suffix_to_prefix),
                start,
                data_trade_date,
                adjust="none",
            )
        except Exception as exc:  # pragma: no cover - 行情缺失按警告处理
            logger.warning("组合计划读取行情失败: %s", exc)
            return {}, {}, [f"行情读取失败：{exc}"]
        if frame is None or frame.empty:
            return {}, {}, ["当日行情缺失，计划未校验价格与容量"]
        history: dict[str, list[tuple[date, float, float]]] = {}
        for _, row in frame.iterrows():
            prefix = suffix_to_prefix.get(str(row.get("symbol")))
            if not prefix:
                continue
            close = row.get("close")
            amount = row.get("amount")
            # QuantDB daily_unadjusted.amount 单位为万元（实测 2026-10-20）。
            if not (_finite(amount) and float(amount) > 0):
                continue
            raw_day = row.get("trade_date")
            day = raw_day.date() if hasattr(raw_day, "date") else _parse_date(raw_day)
            if day is None or day > data_trade_date:
                continue
            history.setdefault(prefix, []).append(
                (
                    day,
                    float(close) if _finite(close) and float(close) > 0 else 0.0,
                    float(amount) * 10_000.0,
                )
            )
        prices: dict[str, float] = {}
        avg_amounts: dict[str, float] = {}
        for prefix, rows in history.items():
            rows.sort(key=lambda item: item[0])
            window = rows[-max(1, int(lookback_days)) :]
            if len(window) >= max(1, int(min_adv_days)):
                avg_amounts[prefix] = sum(item[2] for item in window) / len(window)
            today = next(
                (item for item in reversed(rows) if item[0] == data_trade_date),
                None,
            )
            if today is not None and today[1] > 0:
                prices[prefix] = today[1]
        return prices, avg_amounts, []

    @staticmethod
    async def _risk_flags(
        symbols: Sequence[str], market: str = CN_MARKET
    ) -> tuple[dict[str, str], str | None]:
        """``前缀式 symbol → "ST" / "退市"``（当前截面）+ 数据源错误原因。

        数据源 ``factor_research.data.load_instrument("CN")``（``is_st`` /
        ``is_quit``，名称含 ST 兜底）。截面无时点性：历史重算时只能说明
        「按当前截面判定」，这一点由计划层 warnings 明示。

        **失败不再静默放行**：返回 ``(flags, reason)``，``reason`` 非 None 时
        计划层必须告警并标记 ``data_ok=False``（不可确认），否则「已剔除 ST」
        的硬条件会在数据源故障时悄悄失效。
        """
        if not symbols:
            return {}, None
        key = str(market or CN_MARKET).upper()
        now = time.monotonic()
        cached = _RISK_CACHE.get(key)
        if cached is not None and now - cached[0] < PROFILE_CACHE_TTL_SECONDS:
            all_flags, reason = cached[1], cached[2]
        else:
            all_flags, reason = {}, None
            if key != "CN":
                reason = f"暂不支持 {key} 的风险标记"
            else:
                try:
                    from backend.services.engine.factor_research.data import (
                        load_instrument,
                    )

                    # 全表 parquet 读取是阻塞 IO：放线程池，避免卡事件循环
                    frame = await asyncio.to_thread(load_instrument, "CN")
                except Exception as exc:  # pragma: no cover - 数据源故障路径
                    logger.warning("组合计划读取风险标记失败: %s", exc)
                    frame, reason = None, str(exc) or type(exc).__name__
                if reason is None and (frame is None or getattr(frame, "empty", True)):
                    reason = "风险标记源为空"
                if reason is None:
                    all_flags = ModelBlendService._flags_from_instruments(frame)
            _RISK_CACHE[key] = (now, all_flags, reason)
            if len(_RISK_CACHE) > 8:  # 市场数量有限；防御性上限
                oldest = min(_RISK_CACHE.items(), key=lambda item: item[1][0])[0]
                _RISK_CACHE.pop(oldest, None)
        wanted = {
            StockCodeUtil.to_prefix(str(symbol)) or str(symbol) for symbol in symbols
        }
        return {sym: flag for sym, flag in all_flags.items() if sym in wanted}, reason

    @staticmethod
    def _flags_from_instruments(frame: Any) -> dict[str, str]:
        """从全量证券主表提取 ST/退市标记（纯函数，便于单测）。"""
        has_st = "is_st" in frame.columns
        has_quit = "is_quit" in frame.columns
        flags: dict[str, str] = {}

        def truthy(value: Any) -> bool:
            if value is True:
                return True
            if value is False or value is None:
                return False
            try:
                number = float(value)
            except (TypeError, ValueError):
                return False
            # NaN 必须视为「未知」：bool(float('nan')) 是 True，
            # 不拦会把整个候选池误标成 ST/退市，计划直接清空。
            if math.isnan(number):
                return False
            return bool(number)

        for _, row in frame.iterrows():
            prefix = StockCodeUtil.to_prefix(str(row.get("symbol") or ""))
            if not prefix:
                continue
            name = str(row.get("name") or "")
            is_st = truthy(row.get("is_st")) if has_st else False
            if not is_st and "ST" in name.upper():
                is_st = True
            if has_quit and truthy(row.get("is_quit")):
                flags[prefix] = "退市"
            elif is_st:
                flags[prefix] = "ST"
        return flags

    async def plan(
        self,
        blend: Mapping[str, Any],
        *,
        tenant_id: str,
        user_id: str,
        data_trade_date: date | None = None,
        plan_config: PlanConfig | None = None,
        persist: bool = True,
    ) -> dict[str, Any]:
        cfg = plan_config or PlanConfig(
            top_k=int(blend.get("target_positions") or 50),
            entry_per_day=int(blend.get("entry_per_day") or 5),
            hold_days=int(blend.get("rolling_window") or 10),
            target_positions=int(blend.get("target_positions") or 50),
        )
        blend_id = str(blend["blend_id"])
        members = await self.members_from_blend(blend)
        member_ids = [str(m["model_id"]) for m in members]
        min_coverage = int(blend.get("min_coverage") or 2)
        target = data_trade_date or await self.latest_common_date(
            tenant_id=tenant_id,
            user_id=user_id,
            model_ids=member_ids,
            required=min_coverage,
        )
        if target is None:
            raise BlendError(
                "所选模型没有共同数据日：请先对成员做串行补推，或调低最低覆盖数",
                code="no_common_date",
            )

        weights = _weights_from_members(blend, members)
        ranking = await self.ranking(
            tenant_id=tenant_id,
            user_id=user_id,
            members=members,
            data_trade_date=target,
            method=str(blend.get("method") or "mean"),
            weights=weights,
            min_coverage=min_coverage,
            trim_count=int(blend.get("trim_count") or 2),
            consensus_min_count=int(blend.get("consensus_min_count") or 0),
            top=max(cfg.top_k, cfg.target_positions),
        )
        rows = _rows_from_payload(ranking["ranking"])
        positions_payload = await self.persistence.list_open_positions(
            blend_id, as_of=target
        )
        positions = [
            PaperPosition(
                symbol=str(p["symbol"]),
                entry_date=date.fromisoformat(str(p["entry_date"])),
                shares=int(p.get("shares") or 0),
                target_value=p.get("target_value"),
            )
            for p in positions_payload
        ]
        candidate_symbols = [row.symbol for row in rows] + [p.symbol for p in positions]
        window = await self._window_stats(
            tenant_id=tenant_id,
            user_id=user_id,
            members=members,
            weights=weights,
            method=str(blend.get("method") or "mean"),
            trim_count=int(blend.get("trim_count") or 2),
            min_coverage=min_coverage,
            consensus_min_count=int(blend.get("consensus_min_count") or 0),
            end_date=target,
            lookback=cfg.lookback,
            top_k=max(cfg.top_k, cfg.target_positions),
            symbols=candidate_symbols,
        )
        prices, avg_amounts, quote_warnings = await self._quotes(
            candidate_symbols,
            target,
            lookback_days=cfg.adv_lookback_days,
            min_adv_days=cfg.min_adv_days,
        )
        risk_flags: dict[str, str] = {}
        risk_error: str | None = None
        if cfg.exclude_st:
            risk_flags, risk_error = await self._risk_flags(
                candidate_symbols, market=str(blend.get("market") or CN_MARKET)
            )
        target_date = await self._next_trading_day(target)
        paper = build_rebalance_plan(
            data_trade_date=target,
            target_date=target_date,
            ranking=rows,
            history=window["scores"],
            trading_days=await self._trading_days(target, need=cfg.lookback + 1),
            positions=positions,
            config=cfg,
            prices=prices,
            avg_amounts=avg_amounts,
            risk_flags=risk_flags,
        )
        weight_map = weights or {}
        config_digest = config_hash(
            members=[
                (str(m["model_id"]), float(weight_map.get(m["model_id"], 1.0)))
                for m in members
            ],
            min_coverage=min_coverage,
            method=str(blend.get("method") or "mean"),
            trim_count=int(blend.get("trim_count") or 2),
            consensus_min_count=int(blend.get("consensus_min_count") or 0),
            top_k=cfg.top_k,
            hold_days=cfg.hold_days,
            entry_per_day=cfg.entry_per_day,
            target_positions=cfg.target_positions,
            cash_buffer=cfg.cash_buffer,
            adv_participation=cfg.adv_participation,
            exit_delta=cfg.exit_delta,
            lookback=cfg.lookback,
            min_lookback_days=cfg.min_lookback_days,
            capital=cfg.capital,
            min_adv_yi=cfg.min_adv_yi,
            exclude_st=cfg.exclude_st,
            adv_lookback_days=cfg.adv_lookback_days,
            min_adv_days=cfg.min_adv_days,
        )
        digest_positions = positions_hash(positions)
        payload = paper.to_dict()
        warnings = list(payload.get("warnings") or []) + quote_warnings
        # data_ok=False 表示行情/风险标记缺失：计划可以看，但**不可确认**，
        # 否则「已剔除 ST / 已按成交额匹配」的硬条件会在故障时静默失效。
        data_ok = not quote_warnings
        if cfg.exclude_st:
            if risk_error:
                warnings.append(f"风险标记不可用，未剔除 ST/退市：{risk_error}")
                data_ok = False
            else:
                warnings.append("ST/退市按当前截面判定，历史重算无时点性")
        payload["warnings"] = warnings
        payload["data_ok"] = data_ok
        payload["summary"]["data_ok"] = data_ok
        # 名称/行业/市值与榜单同源；计划行缺失时仍保持 None（前端显示 —）
        names = await self._stock_names()
        profiles = await self._stock_profiles(str(blend.get("market") or CN_MARKET))
        for section in ("entries", "holds", "exits"):
            for row in payload.get(section) or []:
                symbol = str(row.get("symbol"))
                profile = profiles.get(symbol) or {}
                row.setdefault("name", names.get(symbol) or None)
                row.setdefault("industry", profile.get("industry"))
                row.setdefault("market_cap_yi", profile.get("market_cap_yi"))
        if not persist:
            payload.update(
                {
                    "plan_id": None,
                    "blend_id": blend_id,
                    "config_hash": config_digest,
                    "positions_hash": digest_positions,
                    "confirmed_at": None,
                    "created_at": None,
                    "blend": {
                        "blend_id": blend_id,
                        "name": blend.get("name"),
                        "method": blend.get("method"),
                        "horizon_days": blend.get("horizon_days"),
                    },
                }
            )
            return payload
        stored = await self.persistence.upsert_plan(
            blend_id=blend_id,
            tenant_id=tenant_id,
            user_id=user_id,
            data_trade_date=target,
            target_date=target_date,
            config_hash=config_digest,
            positions_hash=digest_positions,
            params=payload["params"],
            summary=payload["summary"],
            entries=payload["entries"],
            holds=payload["holds"],
            exits=payload["exits"],
        )
        stored["warnings"] = payload["warnings"]
        stored["data_ok"] = data_ok
        stored["blend"] = {
            "blend_id": blend_id,
            "name": blend.get("name"),
            "method": blend.get("method"),
            "horizon_days": blend.get("horizon_days"),
        }
        return stored

    async def confirm_plan(
        self,
        blend: Mapping[str, Any],
        plan: Mapping[str, Any],
        *,
        tenant_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        if plan.get("confirmed_at"):
            raise BlendError("计划已确认", status_code=409, code="plan_confirmed")
        summary = (
            plan.get("summary") if isinstance(plan.get("summary"), Mapping) else {}
        )
        if plan.get("data_ok") is False or summary.get("data_ok") is False:
            raise BlendError(
                "计划数据不完整（行情或 ST/退市标记缺失），请重新生成后再确认",
                status_code=409,
                code="plan_data_degraded",
            )
        plan_date = date.fromisoformat(str(plan["data_trade_date"]))
        allowed = await self._recent_trading_days()
        if plan_date not in allowed:
            raise BlendError(
                "计划已过期（仅允许最近 2 个交易日）",
                status_code=409,
                code="plan_expired",
            )
        blend_id = str(blend["blend_id"])
        current = await self.persistence.list_open_positions(blend_id)
        current_hash = positions_hash(
            [
                PaperPosition(
                    symbol=str(p["symbol"]),
                    entry_date=date.fromisoformat(str(p["entry_date"])),
                    shares=int(p.get("shares") or 0),
                )
                for p in current
            ]
        )
        if current_hash != plan.get("positions_hash"):
            raise BlendError(
                "纸面台账已变化，请重新生成计划",
                status_code=409,
                code="stale_positions",
            )
        try:
            confirmed = await self.persistence.confirm_plan(
                plan=plan,
                target_date=date.fromisoformat(str(plan["target_date"])),
            )
        except Exception as exc:  # noqa: BLE001 - 并发确认靠唯一索引兜底
            if "uq_blend_plan_confirmed" in str(exc) or "unique" in str(exc).lower():
                raise BlendError(
                    "同一数据日已有确认计划", status_code=409, code="plan_confirmed"
                ) from exc
            raise
        if confirmed is False:
            # 并发确认：另一请求先抢到了 plan 行（rowcount 0）
            raise BlendError("计划已确认", status_code=409, code="plan_confirmed")
        refreshed = await self.persistence.get_plan(
            str(plan["plan_id"]), tenant_id=tenant_id, user_id=user_id
        )
        return refreshed or {}

    async def annotate_confirmable(
        self, plans: Sequence[dict[str, Any] | None]
    ) -> None:
        """给计划响应补 ``can_confirm``，与 confirm_plan 共用同一份日历口径。

        前端原先按 ``/dates`` 序位判断过期；缺评分日时会与后端「最近 2 个交易日」
        错位，且浏览器时钟（宿主 JST）与容器时钟不一致。时间判定统一放服务端。
        """
        allowed = {day.isoformat() for day in await self._recent_trading_days()}
        for plan in plans:
            if plan is None:
                continue
            plan_date = str(plan.get("data_trade_date") or "")[:10]
            summary = (
                plan.get("summary") if isinstance(plan.get("summary"), Mapping) else {}
            )
            plan["can_confirm"] = (
                bool(plan.get("plan_id"))
                and not plan.get("confirmed_at")
                and plan_date in allowed
                and plan.get("data_ok") is not False
                and summary.get("data_ok") is not False
            )

    @staticmethod
    async def _recent_trading_days() -> set[date]:
        today = date.today()
        try:
            from backend.shared.trading_calendar import calendar_service

            latest = await calendar_service.prev_trading_day(
                market=CN_MARKET,
                trade_date=today + timedelta(days=1),
                tenant_id="default",
                user_id="system",
            )
            previous = await calendar_service.prev_trading_day(
                market=CN_MARKET,
                trade_date=latest,
                tenant_id="default",
                user_id="system",
            )
            return {latest, previous}
        except Exception:  # pragma: no cover - 日历不可用退化为自然日
            return {today, today - timedelta(days=1)}

    async def members_from_blend(
        self, blend: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        models = await self._normalized_models(
            tenant_id=str(blend["tenant_id"]),
            user_id=str(blend["user_id"]),
            market=str(blend.get("market") or CN_MARKET),
        )
        by_id = {m["model_id"]: m for m in models}
        members = []
        for member in blend.get("members") or []:
            model_id = str(member["model_id"])
            if model_id not in by_id:
                raise BlendError(
                    f"组合成员已不可用：{model_id}", code="member_unavailable"
                )
            members.append(by_id[model_id])
        return members

    @staticmethod
    async def _next_trading_day(day: date) -> date:
        try:
            from backend.shared.trading_calendar import calendar_service

            return await calendar_service.next_trading_day(
                market=CN_MARKET, trade_date=day, tenant_id="default", user_id="system"
            )
        except Exception:  # pragma: no cover - 日历不可用时退化为次日
            return day + timedelta(days=1)


# ── 小工具（纯函数） ─────────────────────────────────────────────────────


def _validate_weights(
    members: Sequence[Mapping[str, Any]], weights: Mapping[str, float] | None
) -> None:
    if weights is None:
        return
    allowed = {str(m["model_id"]) for m in members}
    unknown = set(weights) - allowed
    if unknown:
        raise BlendError(f"权重包含非成员模型：{', '.join(sorted(unknown)[:5])}")
    for model_id in allowed:
        value = float(weights.get(model_id, 1.0))
        if not math.isfinite(value) or value <= 0:
            raise BlendError(f"成员权重必须是正的有限数：{model_id}={value}")


def _weights_from_members(
    blend: Mapping[str, Any], members: Sequence[Mapping[str, Any]]
) -> dict[str, float] | None:
    stored = {
        str(m["model_id"]): float(m.get("weight") or 1.0)
        for m in blend.get("members") or []
    }
    if str(blend.get("weighting") or "equal") == "equal":
        return None
    return {str(m["model_id"]): stored.get(str(m["model_id"]), 1.0) for m in members}


def _rows_from_payload(payload: Sequence[Mapping[str, Any]]) -> list[BlendRow]:
    """API 行 → 纯函数行（计划需要 rank/score/pct/position）。"""
    rows = []
    for item in payload:
        rows.append(
            BlendRow(
                symbol=str(item["symbol"]),
                score=float(item["score"]),
                rank=int(item["rank"]),
                coverage=int(item["coverage"]),
                dispersion=item.get("dispersion"),
                pct=item.get("pct"),
                research_score=item.get("research_score"),
                position=str(item.get("position") or position_of(item.get("pct"))),
                contributions=dict(item.get("contributions") or {}),
                missing_members=tuple(item.get("missing_members") or ()),
            )
        )
    return rows
