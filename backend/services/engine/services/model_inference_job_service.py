"""串行补推任务编排：预览、创建、后台执行、取消与状态汇总。

职责边界：

- 缺口/资格判定全部复用 ``model_backfill``（与旧 coverage 端点同一口径）；
- 任务状态落 ``qm_model_inference_jobs``（``ModelInferenceJobPersistence``）；
- 本模块只做编排与 ETA，不直接读 parquet、不直接跑推理脚本。

``asyncio.create_task`` 的强引用保存在模块级 ``_RUNNING_TASKS``，防止任务
被 GC（设计稿 §5.3）。
"""

from __future__ import annotations

import asyncio
import logging
import statistics
import time
import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import text

from backend.services.engine.inference.batch_orchestrator import (
    batch_inference_orchestrator,
)
from backend.services.engine.inference.model_backfill import (
    DEFAULT_DAY_SECONDS,
    MAX_GAP_DAYS_PER_MODEL,
    BackfillCoverage,
    BackfillModel,
    SerialResult,
    compute_coverage,
    ineligibility_reason,
    resolve_backfill_model,
    run_serial,
    to_backfill_model,
    create_redis_client,
)
from backend.services.engine.services.model_inference_job_persistence import (
    ActiveJobExistsError,
    ModelInferenceJobPersistence,
)
from backend.shared.database_manager_v2 import get_session
from backend.shared.model_registry import model_registry_service
from backend.shared.utc_datetime import to_utc_iso, utc_now

logger = logging.getLogger(__name__)

MIN_MODELS = 2
MAX_MODELS = 20
HEARTBEAT_SECONDS = 30
PROGRESS_WRITE_INTERVAL_SECONDS = 1.0
REAP_TIMEOUT_SECONDS = 600

_RUNNING_TASKS: set[asyncio.Task] = set()

#: 任务表唯一实例（API 路由与旧补推端点共用；表在应用启动时自愈创建）。
model_inference_job_persistence = ModelInferenceJobPersistence()


class JobValidationError(ValueError):
    """请求参数/资格校验失败（路由映射为 400）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class PreviewEntry:
    model: BackfillModel
    coverage: BackfillCoverage
    eligible: bool
    reason: str | None
    seconds_per_day: float
    estimated_seconds: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model.model_id,
            "display_name": self.model.display_name,
            "model_type": self.model.model_type,
            "horizon_days": self.model.target_horizon_days,
            "market": self.model.market,
            "status": self.model.status,
            "latest_data_date": self.coverage.max_date,
            "gap_count": self.coverage.gap_count,
            "estimated_seconds": int(round(self.estimated_seconds)),
            "eligible": self.eligible,
            "reason": self.reason,
        }


@dataclass
class PreviewResult:
    entries: list[PreviewEntry] = field(default_factory=list)
    excluded: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def eligible_entries(self) -> list[PreviewEntry]:
        return [entry for entry in self.entries if entry.eligible]

    @property
    def total_days(self) -> int:
        return sum(entry.coverage.gap_count for entry in self.eligible_entries)

    @property
    def estimated_seconds(self) -> float:
        return sum(entry.estimated_seconds for entry in self.eligible_entries)

    def as_dict(self) -> dict[str, Any]:
        eligible = self.eligible_entries
        total_seconds = self.estimated_seconds
        return {
            "models": [entry.as_dict() for entry in self.entries],
            "excluded": list(self.excluded),
            "totals": {
                "models": len(self.entries) + len(self.excluded),
                "eligible_models": len(eligible),
                "days": self.total_days,
                "estimated_seconds": int(round(total_seconds)),
            },
            "warnings": list(self.warnings),
        }


def _format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)} 秒"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.0f} 分钟"
    return f"{minutes / 60:.1f} 小时"


async def estimate_seconds_by_model(
    tenant_id: str, user_id: str, model_ids: list[str]
) -> dict[str, float]:
    """每模型单日 ETA：最近 10 条 completed run 的 duration 中位数。"""
    if not model_ids:
        return {}
    try:
        async with get_session() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            """
                            SELECT model_id, duration_ms FROM (
                              SELECT model_id, duration_ms,
                                     ROW_NUMBER() OVER (
                                       PARTITION BY model_id ORDER BY created_at DESC
                                     ) AS rn
                              FROM qm_model_inference_runs
                              WHERE tenant_id = :tenant_id AND user_id = :user_id
                                AND model_id = ANY(:model_ids)
                                AND status = 'completed'
                                AND duration_ms IS NOT NULL AND duration_ms > 0
                            ) ranked
                            WHERE rn <= 10
                            """
                        ),
                        {
                            "tenant_id": tenant_id,
                            "user_id": user_id,
                            "model_ids": list(model_ids),
                        },
                    )
                )
                .mappings()
                .all()
            )
    except Exception as exc:  # noqa: BLE001 - ETA 失败不影响预览
        logger.warning("补推 ETA 查询失败: %s", exc)
        return {}
    grouped: dict[str, list[float]] = {}
    for row in rows:
        grouped.setdefault(str(row["model_id"]), []).append(
            float(row["duration_ms"]) / 1000.0
        )
    return {mid: float(statistics.median(values)) for mid, values in grouped.items()}


async def _entry_for(
    model: BackfillModel, seconds_per_day: float
) -> PreviewEntry:
    reason = ineligibility_reason(model)
    if reason is not None:
        empty = BackfillCoverage(
            min_date=None,
            max_date=None,
            gap_dates=(),
            latest_trade_date="",
            reason=reason,
        )
        return PreviewEntry(
            model=model,
            coverage=empty,
            eligible=False,
            reason=reason,
            seconds_per_day=seconds_per_day,
            estimated_seconds=0.0,
        )
    try:
        coverage = await asyncio.to_thread(compute_coverage, model)
    except Exception as exc:  # noqa: BLE001 - 单模型覆盖失败只影响自己
        logger.warning("补推覆盖计算失败 %s: %s", model.model_id, exc)
        empty = BackfillCoverage(
            min_date=None,
            max_date=None,
            gap_dates=(),
            latest_trade_date="",
            reason="coverage_failed",
        )
        return PreviewEntry(
            model=model,
            coverage=empty,
            eligible=False,
            reason="coverage_failed",
            seconds_per_day=seconds_per_day,
            estimated_seconds=0.0,
        )
    return PreviewEntry(
        model=model,
        coverage=coverage,
        eligible=True,
        reason=None,
        seconds_per_day=seconds_per_day,
        estimated_seconds=seconds_per_day * coverage.gap_count,
    )


def normalize_model_ids(
    model_ids: list[str] | None, *, min_models: int = MIN_MODELS
) -> list[str]:
    seen: set[str] = set()
    cleaned: list[str] = []
    for raw in model_ids or []:
        model_id = str(raw or "").strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        cleaned.append(model_id)
    if len(cleaned) < min_models:
        raise JobValidationError("too_few_models", f"至少选择 {min_models} 个模型")
    if len(cleaned) > MAX_MODELS:
        raise JobValidationError("too_many_models", f"最多选择 {MAX_MODELS} 个模型")
    return cleaned


async def build_preview(
    *,
    tenant_id: str,
    user_id: str,
    model_ids: list[str],
    min_models: int = MIN_MODELS,
) -> PreviewResult:
    ids = normalize_model_ids(model_ids, min_models=min_models)
    rows = await model_registry_service.list_models(
        tenant_id=tenant_id, user_id=user_id, include_archived=True
    )
    by_id = {str(row.get("model_id") or ""): row for row in rows}
    estimates = await estimate_seconds_by_model(tenant_id, user_id, ids)

    result = PreviewResult()
    tasks = []
    for model_id in ids:
        row = by_id.get(model_id)
        if row is None:
            result.excluded.append(
                {
                    "model_id": model_id,
                    "reason": "system_readonly"
                    if model_id.startswith("sys-")
                    else "not_found",
                }
            )
            continue
        model = to_backfill_model(row)
        seconds_per_day = estimates.get(model_id, DEFAULT_DAY_SECONDS)
        tasks.append((model, seconds_per_day))
    entries = await asyncio.gather(
        *(_entry_for(model, seconds) for model, seconds in tasks)
    )
    result.entries = list(entries)

    reasons = {entry.reason for entry in result.entries if entry.reason}
    if "system_readonly" in {
        item["reason"] for item in result.excluded
    }:
        result.warnings.append("系统模型只读，已排除")
    if reasons & {"missing_storage", "not_ready", "archived"}:
        result.warnings.append("部分模型未就绪或无产物，已排除")
    gap_too_large = [
        entry.model.model_id
        for entry in result.eligible_entries
        if entry.coverage.gap_count > MAX_GAP_DAYS_PER_MODEL
    ]
    if gap_too_large:
        result.warnings.append(
            f"缺口超过 {MAX_GAP_DAYS_PER_MODEL} 天：{', '.join(gap_too_large)}；"
            "请先按区间补推或拆分"
        )
    markets = {entry.model.market for entry in result.eligible_entries}
    if len(markets) > 1:
        result.warnings.append("所选模型跨市场，无法在同一任务内串行补推")
    if result.eligible_entries:
        total = result.estimated_seconds
        result.warnings.insert(
            0,
            f"串行执行，预计 {_format_duration(total * 0.7)}–"
            f"{_format_duration(total * 1.5)}（同一时间仅一个补推任务）",
        )
    return result


async def create_job(
    *,
    tenant_id: str,
    user_id: str,
    model_ids: list[str],
    persistence: ModelInferenceJobPersistence,
    mode: str = "gaps",
    min_models: int = MIN_MODELS,
) -> dict[str, Any]:
    preview = await build_preview(
        tenant_id=tenant_id,
        user_id=user_id,
        model_ids=model_ids,
        min_models=min_models,
    )
    eligible = preview.eligible_entries
    if not eligible:
        raise JobValidationError("no_eligible_models", "没有可补推的模型")
    markets = {entry.model.market for entry in eligible}
    if len(markets) > 1:
        raise JobValidationError("market_mismatch", "所选模型不在同一市场")
    too_large = [
        entry.model.model_id
        for entry in eligible
        if entry.coverage.gap_count > MAX_GAP_DAYS_PER_MODEL
    ]
    if too_large:
        raise JobValidationError(
            "gap_too_large",
            f"缺口超过 {MAX_GAP_DAYS_PER_MODEL} 天：{', '.join(too_large)}",
        )
    ordered_ids = [entry.model.model_id for entry in eligible]
    job_id = uuid.uuid4().hex
    try:
        job = await persistence.create_job(
            job_id=job_id,
            tenant_id=tenant_id,
            user_id=user_id,
            model_ids=ordered_ids,
            request={
                "model_ids": ordered_ids,
                "requested": list(model_ids),
                "mode": mode,
            },
            total_models=len(ordered_ids),
            total_days=preview.total_days,
            mode=mode,
        )
    except ActiveJobExistsError as exc:
        raise JobValidationError(
            "active_job_exists",
            f"已有补推任务在执行（job_id={exc.job_id or '未知'}）",
        ) from exc

    if preview.total_days == 0:
        now = utc_now()
        await persistence.update_job(
            job_id,
            status="completed",
            finished_at=now,
            heartbeat_at=now,
            completed_days=0,
        )
        refreshed = await persistence.get_job(
            job_id, tenant_id=tenant_id, user_id=user_id
        )
        return refreshed or job
    start_job_runner(job_id, tenant_id=tenant_id, user_id=user_id, persistence=persistence)
    return job


def start_job_runner(
    job_id: str,
    *,
    tenant_id: str,
    user_id: str,
    persistence: ModelInferenceJobPersistence,
) -> None:
    """启动后台任务并持有强引用（不可用 BackgroundTasks）。"""
    task = asyncio.create_task(
        _run_job(job_id, tenant_id=tenant_id, user_id=user_id, persistence=persistence)
    )
    _RUNNING_TASKS.add(task)
    task.add_done_callback(_RUNNING_TASKS.discard)


class _ProgressWriter:
    """把 ``run_serial`` 事件折算成任务行更新（节流 1s）。"""

    def __init__(
        self,
        *,
        job_id: str,
        tenant_id: str,
        user_id: str,
        persistence: ModelInferenceJobPersistence,
        summaries: list[dict[str, Any]],
    ) -> None:
        self.job_id = job_id
        self.tenant_id = tenant_id
        self.user_id = user_id
        self.persistence = persistence
        self.summaries = {item["model_id"]: item for item in summaries}
        self.order = [item["model_id"] for item in summaries]
        self.logs: list[str] = []
        self._last_write = 0.0

    def _counts(self) -> dict[str, int]:
        return {
            "completed_days": sum(
                int(item.get("days_completed", 0)) for item in self.summaries.values()
            ),
            "failed_days": sum(
                int(item.get("days_failed", 0)) for item in self.summaries.values()
            ),
            "skipped_days": sum(
                int(item.get("days_skipped", 0)) for item in self.summaries.values()
            ),
        }

    async def _write(self, force: bool, **extra: Any) -> None:
        now = time.monotonic()
        if not force and now - self._last_write < PROGRESS_WRITE_INTERVAL_SECONDS:
            return
        self._last_write = now
        await self.persistence.update_job(
            self.job_id,
            model_results=[self.summaries[mid] for mid in self.order],
            logs=self.logs[-200:],
            heartbeat_at=utc_now(),
            **self._counts(),
            **extra,
        )

    async def __call__(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == "model_started":
            model = event["model"]
            self.summaries[model["model_id"]] = model
            await self._write(
                True,
                current_model_id=model["model_id"],
                current_date=None,
            )
            return
        if kind == "model_finished":
            model = event["model"]
            self.summaries[model["model_id"]] = model
            self.logs.append(
                f"{model['model_id']} {model['status']}："
                f"成功 {model['days_completed']} / 失败 {model['days_failed']} / "
                f"跳过 {model['days_skipped']}"
            )
            await self._write(True)
            return
        if kind != "day":
            return
        model = event["model"]
        self.summaries[model["model_id"]] = model
        result = event.get("result") or {}
        day = str(event.get("day") or "")
        status = str(result.get("status") or "")
        if status == "completed":
            self.logs.append(
                f"{event.get('model_id')} {day} 完成（信号 {result.get('signals_count', 0)}，"
                f"合并 {result.get('merged_rows', 0)} 行）"
            )
        elif status == "skipped_lock":
            self.logs.append(f"{event.get('model_id')} {day} 跳过：同日推理锁被占用")
        else:
            self.logs.append(
                f"{event.get('model_id')} {day} 失败：{result.get('error') or '推理失败'}"
            )
        await self._write(
            status != "completed",
            current_model_id=str(event.get("model_id") or ""),
            current_date=date.fromisoformat(day) if day else None,
        )


def _job_status(result: SerialResult, total_days: int) -> tuple[str, str | None]:
    if result.cancelled:
        return "cancelled", None
    if total_days == 0:
        return "completed", None
    if (
        result.failed_days == 0
        and result.skipped_days == 0
        and result.completed_days == total_days
    ):
        return "completed", None
    if result.completed_days == 0 and result.failed_days > 0 and result.skipped_days == 0:
        return "failed", f"全部 {result.failed_days} 日补推失败"
    message = (
        f"{result.completed_days}/{total_days} 日成功，"
        f"{result.failed_days} 日失败，{result.skipped_days} 日跳过"
    )
    if result.skipped_days:
        message += "；有同日推理在别处执行，请稍后重跑"
    return "partial", message


async def _run_job(
    job_id: str,
    *,
    tenant_id: str,
    user_id: str,
    persistence: ModelInferenceJobPersistence,
) -> None:
    job = await persistence.get_job(job_id, tenant_id=tenant_id, user_id=user_id)
    if job is None or job.get("status") != "queued":
        return
    # 条件迁移：读与写之间用户可能已取消（queued → cancelled 是终态）
    if not await persistence.claim_running(job_id):
        return
    redis_client = None
    heartbeat_task: asyncio.Task | None = None
    writer: _ProgressWriter | None = None
    try:
        pairs: list[tuple[BackfillModel, list[str]]] = []
        for model_id in [str(mid) for mid in job.get("model_ids") or []]:
            model = await resolve_backfill_model(tenant_id, user_id, model_id)
            if model is None or ineligibility_reason(model) is not None:
                continue
            coverage = await asyncio.to_thread(compute_coverage, model)
            pairs.append((model, list(coverage.gap_dates)))
        if not pairs:
            await persistence.update_job(
                job_id,
                status="failed",
                error="没有可补推的模型",
                finished_at=utc_now(),
            )
            return
        total_days = sum(len(days) for _, days in pairs)
        await persistence.update_job(
            job_id,
            total_models=len(pairs),
            total_days=total_days,
            model_results=[
                {
                    "model_id": model.model_id,
                    "display_name": model.display_name,
                    "status": "queued",
                    "days_total": len(days),
                    "days_completed": 0,
                    "days_failed": 0,
                    "days_skipped": 0,
                    "days_merged": 0,
                    "errors": [],
                    "gap_dates": list(days),
                }
                for model, days in pairs
            ],
        )
        writer = _ProgressWriter(
            job_id=job_id,
            tenant_id=tenant_id,
            user_id=user_id,
            persistence=persistence,
            summaries=[
                {
                    "model_id": model.model_id,
                    "display_name": model.display_name,
                    "status": "queued",
                    "days_total": len(days),
                    "days_completed": 0,
                    "days_failed": 0,
                    "days_skipped": 0,
                    "days_merged": 0,
                    "errors": [],
                    "gap_dates": list(days),
                }
                for model, days in pairs
            ],
        )

        async def _heartbeat() -> None:
            while True:
                await asyncio.sleep(HEARTBEAT_SECONDS)
                await persistence.update_job(job_id, heartbeat_at=utc_now())

        heartbeat_task = asyncio.create_task(_heartbeat())
        redis_client = create_redis_client()

        async def _should_cancel() -> bool:
            current = await persistence.get_job(
                job_id, tenant_id=tenant_id, user_id=user_id
            )
            return bool(current and current.get("cancel_requested"))

        async def _should_skip_model(model_id: str) -> bool:
            # run_serial 按 Awaitable[bool] 调用（model_backfill.py 里 await）；
            # 这里必须是 async，写成同步函数会在第一天开跑前抛 await TypeError。
            return batch_inference_orchestrator.is_running(
                tenant_id, user_id, model_id
            )

        result = await run_serial(
            pairs,
            tenant_id,
            user_id,
            progress_cb=writer,
            should_cancel=_should_cancel,
            should_skip_model=_should_skip_model,
            redis_client=redis_client,
            task_id=job_id,
        )
        status, error = _job_status(result, total_days)
        await persistence.update_job(
            job_id,
            status=status,
            error=error,
            finished_at=utc_now(),
            heartbeat_at=utc_now(),
            current_model_id=None,
            current_date=None,
            completed_days=result.completed_days,
            failed_days=result.failed_days,
            skipped_days=result.skipped_days,
            model_results=[summary.as_dict() for summary in result.model_results],
            logs=writer.logs[-200:],
        )
    except Exception as exc:  # noqa: BLE001 - 任务级异常必须落终态
        logger.exception("补推任务 %s 执行异常", job_id)
        await persistence.update_job(
            job_id,
            status="failed",
            error=str(exc)[:2000],
            finished_at=utc_now(),
            heartbeat_at=utc_now(),
        )
    finally:
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if redis_client is not None:
            try:
                redis_client.close()
            except Exception:  # noqa: BLE001 - 关闭失败不影响任务终态
                pass


def job_payload(job: dict[str, Any]) -> dict[str, Any]:
    """对外形状：不返回 storage_path/model_ids 之外的内部分字段。"""
    total_days = int(job.get("total_days") or 0)
    completed = int(job.get("completed_days") or 0)
    failed = int(job.get("failed_days") or 0)
    skipped = int(job.get("skipped_days") or 0)
    done = completed + failed + skipped
    progress = round(done / total_days, 4) if total_days else (
        1.0 if job.get("status") in {"completed", "partial", "failed", "cancelled"} else 0.0
    )
    current = None
    if job.get("current_model_id"):
        current = {
            "model_id": job["current_model_id"],
            "date": str(job["current_date"]) if job.get("current_date") else None,
        }
    return {
        "job_id": job.get("job_id"),
        "status": job.get("status"),
        "mode": job.get("mode"),
        "progress": progress,
        "current": current,
        "totals": {
            "models": int(job.get("total_models") or 0),
            "days": total_days,
            "completed_days": completed,
            "failed_days": failed,
            "skipped_days": skipped,
        },
        "models": job.get("model_results") or [],
        "logs": job.get("logs") or [],
        "error": job.get("error"),
        "cancel_requested": bool(job.get("cancel_requested")),
        "created_at": to_utc_iso(job.get("created_at")),
        "started_at": to_utc_iso(job.get("started_at")),
        "finished_at": to_utc_iso(job.get("finished_at")),
        "heartbeat_at": to_utc_iso(job.get("heartbeat_at")),
    }


__all__ = [
    "HEARTBEAT_SECONDS",
    "JobValidationError",
    "MAX_MODELS",
    "MIN_MODELS",
    "PreviewResult",
    "REAP_TIMEOUT_SECONDS",
    "build_preview",
    "create_job",
    "estimate_seconds_by_model",
    "job_payload",
    "model_inference_job_persistence",
    "normalize_model_ids",
    "start_job_runner",
]
