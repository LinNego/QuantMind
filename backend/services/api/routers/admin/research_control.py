"""研究总控（market-research）配置与状态 API。

GET  /api/v1/admin/research-control/config          独立 LLM 配置现状（key 掩码）
PUT  /api/v1/admin/research-control/config          保存配置（key 只写不回，留空=沿用）
POST /api/v1/admin/research-control/config/test     发一次最小请求验证端点/key/模型
POST /api/v1/admin/research-control/config/reset    清空独立配置（回落供应商变量）
GET  /api/v1/admin/research-control/status          当日五档运行状态 / 报告 / 次日 brief

**为什么单独一套 key**：研究总控写长报告，与盘中决策模型的供应商/预算不同；
共用 ``GLM_*`` / ``OPENAI_*`` 会出现「换研究模型把交易模型一起换掉」。

**只读不回 key**：``describe()`` 只给掩码；保存与测试都不把 key 写日志/响应。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from backend.services.api.user_app.middleware.auth import require_admin
from backend.shared.research_control.config import (
    SLOTS,
    clear_config,
    describe,
    save_config,
    test_connection,
)

router = APIRouter(dependencies=[Depends(require_admin)])

#: 五档的建议触发时间（北京时间）与宿主 crontab（JST = 北京 + 1）。
#: 这里只是**展示**，真正调度唯一来源是宿主 crontab；改时间要改 crontab 再重启。
SLOT_SCHEDULE: dict[str, dict[str, str]] = {
    "midday": {"time": "11:40", "cron": "40 12 * * 1-5", "label": "午间复盘"},
    "close": {"time": "16:20", "cron": "20 17 * * 1-5", "label": "收盘复盘"},
    "inference": {"time": "18:20", "cron": "20 19 * * 1-5", "label": "推理数据复盘"},
    "news": {"time": "22:45", "cron": "45 23 * * 1-5", "label": "新闻复盘"},
    "night": {"time": "02:10（次日）", "cron": "10 3 * * 2-6", "label": "夜间综合总结"},
}


class ResearchConfigRequest(BaseModel):
    base_url: str | None = Field(None, description="OpenAI 兼容端点（含 /v1）")
    model: str | None = Field(None, description="模型名，如 glm-5.3-flash")
    provider: str | None = Field(None, description="glm / openai / deepseek / custom")
    api_key: str | None = Field(None, description="留空 = 沿用已保存的 key（只写不回）")
    clear_api_key: bool = Field(False, description="显式清空独立 key，回落到供应商变量")


def _day(value: str | None) -> str:
    from backend.shared.research_control.store import CN_TZ
    from datetime import datetime

    if not value:
        return datetime.now(CN_TZ).date().isoformat()
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise HTTPException(status_code=400, detail="date 必须是 YYYY-MM-DD") from None


@router.get("/config")
async def get_config(current_user: dict = Depends(require_admin)):
    data = describe()
    data["schedule"] = SLOT_SCHEDULE
    return {"success": True, "data": data}


@router.put("/config")
async def put_config(
    payload: ResearchConfigRequest,
    current_user: dict = Depends(require_admin),
):
    body: dict[str, Any] = {}
    for key in ("base_url", "model", "provider", "api_key"):
        value = getattr(payload, key)
        if value is not None:
            body[key] = value
    body["clear_api_key"] = payload.clear_api_key
    if not any(k in body for k in ("base_url", "model", "provider", "api_key")):
        raise HTTPException(status_code=400, detail="没有要保存的字段")
    try:
        data = save_config(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    data["schedule"] = SLOT_SCHEDULE
    return {"success": True, "data": data}


@router.post("/config/test")
async def post_config_test(
    payload: ResearchConfigRequest | None = None,
    current_user: dict = Depends(require_admin),
):
    overrides: dict[str, Any] = {}
    if payload is not None:
        overrides = {
            k: getattr(payload, k)
            for k in ("base_url", "model", "provider", "api_key")
            if getattr(payload, k)
        }
    result = test_connection(overrides)
    return {"success": bool(result.get("ok")), "data": result}


@router.post("/config/reset")
async def post_config_reset(current_user: dict = Depends(require_admin)):
    data = clear_config()
    data["schedule"] = SLOT_SCHEDULE
    return {"success": True, "data": data}


@router.get("/status")
async def get_status(
    day: str | None = Query(None, description="复盘日 YYYY-MM-DD，默认今天"),
    current_user: dict = Depends(require_admin),
):
    from backend.shared.research_control.dossier import next_trading_day
    from backend.shared.research_control.store import (
        brief_meta_path,
        list_reports,
        read_state,
    )

    review_day = _day(day)
    try:
        target = next_trading_day(date.fromisoformat(review_day)).isoformat()
        target_key = target.replace("-", "")
    except ValueError:
        target = ""
        target_key = ""
    slots = []
    state = read_state(review_day).get("slots") or {}
    reports = {row["slot"]: row for row in list_reports(review_day)}
    for slot in SLOTS:
        meta = reports.get(slot) or {}
        slots.append(
            {
                "slot": slot,
                "label": SLOT_SCHEDULE[slot]["label"],
                "time": SLOT_SCHEDULE[slot]["time"],
                "report": bool(meta),
                "ts": meta.get("ts", ""),
                "bytes": meta.get("bytes", 0),
                "state": state.get(slot) or {},
            }
        )
    brief_path = brief_meta_path(target_key) if target_key else None
    return {
        "success": True,
        "data": {
            "day": review_day,
            "target": target,
            "slots": slots,
            "brief_ready": bool(brief_path and brief_path.is_file()),
        },
    }
