"""串行补推任务端点 · ``/api/v1/models/inference/backfill-jobs``。

设计稿 §5.6：预览 → 创建（单用户单活跃任务）→ 轮询 → 取消。
旧单模型补推端点（``/{model_id}/inference/backfill``）内部也创建这里
的 job，两套入口共用同一张表与唯一索引。

路由注册顺序硬约束：必须早于 ``model_training_router``（带
``GET /{model_id}`` 兜底），见 ``main.py`` 的注册注释。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from backend.services.api.user_app.middleware.auth import get_current_user
from backend.services.engine.services.model_inference_job_service import (
    MAX_REQUESTED_MODELS,
    REAP_TIMEOUT_SECONDS,
    JobValidationError,
    build_preview,
    create_job,
    job_payload,
    model_inference_job_persistence,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/models/inference/backfill-jobs",
    tags=["Model Inference Jobs"],
)


class PreviewRequest(BaseModel):
    # 与 service 层同一上限：请求可携带全选原始勾选（含归档），可补推上限在预览里再判
    model_ids: list[str] = Field(default_factory=list, max_length=MAX_REQUESTED_MODELS)


class CreateJobRequest(PreviewRequest):
    mode: str = Field(default="gaps", pattern="^(gaps|range)$")


def _owner(current_user: Mapping[str, Any]) -> tuple[str, str]:
    tenant_id = str(current_user.get("tenant_id") or "default")
    user_id = str(current_user.get("user_id") or current_user.get("sub") or "")
    if not user_id:
        raise HTTPException(status_code=401, detail="用户身份无效")
    return tenant_id, user_id


def _error(exc: JobValidationError) -> HTTPException:
    status_code = 409 if exc.code == "active_job_exists" else 400
    return HTTPException(
        status_code=status_code,
        detail={"code": exc.code, "message": exc.message},
    )


@router.post("/preview", summary="预览多模型补推缺口与预计耗时")
async def preview_backfill_jobs(
    payload: PreviewRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    tenant_id, user_id = _owner(current_user)
    try:
        preview = await build_preview(
            tenant_id=tenant_id, user_id=user_id, model_ids=payload.model_ids
        )
    except JobValidationError as exc:
        raise _error(exc) from exc
    return preview.as_dict()


@router.post("", summary="创建串行补推任务")
async def create_backfill_job(
    payload: CreateJobRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    tenant_id, user_id = _owner(current_user)
    try:
        job = await create_job(
            tenant_id=tenant_id,
            user_id=user_id,
            model_ids=payload.model_ids,
            persistence=model_inference_job_persistence,
            mode=payload.mode,
        )
    except JobValidationError as exc:
        raise _error(exc) from exc
    return job_payload(job)


@router.get("", summary="我的补推任务列表")
async def list_backfill_jobs(
    limit: int = Query(default=20, ge=1, le=100),
    current_user: dict[str, Any] = Depends(get_current_user),
):
    tenant_id, user_id = _owner(current_user)
    await model_inference_job_persistence.reap_stale(
        tenant_id=tenant_id,
        user_id=user_id,
        heartbeat_timeout_seconds=REAP_TIMEOUT_SECONDS,
    )
    jobs = await model_inference_job_persistence.list_jobs(
        tenant_id=tenant_id, user_id=user_id, limit=limit
    )
    items = [job_payload(job) for job in jobs]
    active = next(
        (
            item["job_id"]
            for item in items
            if item["status"] in {"queued", "running", "cancelling"}
        ),
        None,
    )
    return {"items": items, "active_job_id": active}


@router.get("/{job_id}", summary="补推任务状态")
async def get_backfill_job(
    job_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    tenant_id, user_id = _owner(current_user)
    await model_inference_job_persistence.reap_stale(
        tenant_id=tenant_id,
        user_id=user_id,
        heartbeat_timeout_seconds=REAP_TIMEOUT_SECONDS,
    )
    job = await model_inference_job_persistence.get_job(
        job_id, tenant_id=tenant_id, user_id=user_id
    )
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return job_payload(job)


@router.post("/{job_id}/cancel", summary="请求取消补推任务")
async def cancel_backfill_job(
    job_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    tenant_id, user_id = _owner(current_user)
    job = await model_inference_job_persistence.request_cancel(
        job_id, tenant_id=tenant_id, user_id=user_id
    )
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return job_payload(job)
