"""评估读 API（FE-E 数据出口）：`eval_scores` 表的唯一对外读取面。

用途（前端评估中心/评分卡/体检档案，T-FE-14/15/16）：
- `GET /api/v1/eval/scores`        最新快照列表（评分卡网格；可选 latest_only 取每对象最新一条）
- `GET /api/v1/eval/scores/history` 单对象历史序列（评分卡历史曲线）
- `GET /api/v1/eval/health/{strategy_id}` 策略体检档案（最新 + 历史 + **晋级门禁预演**
  + **启动资格清单**：SIMULATION / REAL 两模式的逐项资格，启动前可见）

纪律：
- 只读；前端禁止直连表（契约收口在本路由）；
- 可见性：`tenant_id = 当前租户 AND (user_id = 当前用户 OR user_id = '')`——因子/模型等
  全租户共享行（写侧 user_id 为空）对所有用户可见，用户私有行（策略/账户/选股）仅本人可见；
- 行数据 → 前端契约的转换纯函数化（可单测），SQL 与转换分层；
- 列表行附 `display_name`（人话名，尽力而为；查不到为 None，前端回退展示 object_id）。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from backend.services.api.user_app.middleware.auth import get_current_user, require_admin
from backend.shared import eval_trigger
from backend.shared.database_manager_v2 import get_session
from backend.shared.market_labels import MARKET_LABELS

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/eval", tags=["Eval"])

# 允许的评分卡类型（与写入侧 object_type 唯一一致；strategy_health 为体检留档）
ALLOWED_OBJECT_TYPES = (
    "factor",
    "model",
    "strategy",
    "account",
    "live_account",
    "daily_selection",
    "strategy_health",
)


def _row_to_score(row: Any) -> dict[str, Any]:
    """eval_scores 行 → 前端评分卡契约（纯函数）。"""
    return {
        "object_type": row["object_type"],
        "object_id": row["object_id"],
        "snapshot_date": row["snapshot_date"].isoformat() if row["snapshot_date"] else None,
        "score": row["score"],
        "grade": row["grade"],
        "low_confidence": bool(row["low_confidence"]),
        "red_line_failed": list(row["red_line_failed"] or []),
        "dimensions": row["dimensions"] or {},
        "inputs_version": row["inputs_version"] or {},
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }


def build_gate_preview(health: dict[str, Any] | None, *, mode: str = "SIMULATION") -> dict[str, Any]:
    """晋级门禁预演（纯函数）：与执行点 `promotion_gate` 同源——前端展示即真实口径。"""
    from backend.shared.backtest_health import promotion_gate

    allowed, note = promotion_gate(health, mode=mode)
    return {"mode": mode, "allowed": allowed, "note": note}


def _resolve_mode(mode: Any) -> str:
    """`mode` 取参（纯函数）：FastAPI 走 HTTP 时收到字符串；测试直调端点时拿到的是
    `Query(...)` 默认对象，故取 `.default` 兜底（与 `limit` 同一历史处理方式）。"""
    value = getattr(mode, "default", mode)
    return str(value or "SIMULATION").strip().upper()


async def _strategy_lifecycle_status(strategy_id: str) -> Any:
    """策略当前生命周期状态（尽力而为；读不到 → None，由 `normalize_status` 保守回落 DRAFT）。

    `strategies` 表**没有 tenant_id 列**（策略 id 全库唯一），故与
    `_resolve_display_names` 的 `strategy_health` 分支同口径不做租户过滤；
    行不存在（体检留档早于策略落库等）不报错，返回 None。
    """
    from sqlalchemy import text as _text

    try:
        async with get_session(read_only=True) as session:
            row = (
                await session.execute(
                    _text("SELECT status FROM strategies WHERE id = :sid"),
                    {"sid": int(strategy_id)},
                )
            ).first()
    except Exception as exc:  # noqa: BLE001 - 展示面读失败按「无记录」处理
        logger.warning("[Eval] 策略状态读取失败 strategy=%s: %s", strategy_id, exc)
        return None
    return row[0] if row else None


def _read_start_eligibility_inputs(
    tenant_id: str, user_id: str, strategy_id: str
) -> tuple[int | None, float | None]:
    """SIM 交易日 / 跟踪误差（尽力而为：任何失败 → None，按既有「无数据只提示不拦」口径）。

    身份经 `normalize_runtime_user` 收口——与启动侧 `stamp_sim_start` 写入
    `sim_start_key` 用的 `resolved_user_id` 同一口径，否则键对不上恒读空。
    """
    from backend.shared.backtest_health import (
        latest_user_tracking_error,
        sim_trading_days_since,
    )
    from backend.shared.simulation_account_keys import normalize_runtime_user

    try:
        from backend.services.trade_shared.redis_client import get_redis as get_trade_redis

        redis = get_trade_redis()
        runtime_user = normalize_runtime_user(user_id)
        return (
            sim_trading_days_since(redis, tenant_id, runtime_user, strategy_id),
            latest_user_tracking_error(redis, runtime_user),
        )
    except Exception as exc:  # noqa: BLE001 - 取数失败即「无记录」，绝不拦启动
        logger.warning("[Eval] 启动资格取数失败（按无记录处理）: %s", exc)
        return None, None


async def build_start_eligibility_map(
    strategy_id: str,
    health: dict[str, Any] | None,
    *,
    tenant_id: str,
    user_id: str,
) -> dict[str, Any]:
    """两模式启动资格清单（`build_start_eligibility` 的取数壳，判据全在执行点模块）。

    取数口径与 `/real-trading/start` 的 400 `gate_items` 完全对齐：状态机 + 体检
    结论 +（REAL）SIM 交易日 / 跟踪误差；`HEALTH_GATE_ENABLED=false` 时体检项同步
    放行——展示面不夸大也不隐瞒门禁。
    """
    from backend.shared.backtest_health import gate_enabled
    from backend.shared.start_eligibility import build_start_eligibility

    gate_on = gate_enabled()
    status = await _strategy_lifecycle_status(strategy_id)
    sim_days: int | None = None
    tracking_error: float | None = None
    if gate_on:
        sim_days, tracking_error = _read_start_eligibility_inputs(
            tenant_id, user_id, strategy_id
        )
    return {
        mode: build_start_eligibility(
            status=status,
            health=health,
            mode=mode,
            sim_trading_days=sim_days,
            tracking_error=tracking_error,
            health_gate_enabled=gate_on,
        )
        for mode in ("SIMULATION", "REAL")
    }


def _validate_object_type(object_type: str) -> str:
    ot = str(object_type or "").strip()
    if ot not in ALLOWED_OBJECT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"object_type 非法: {object_type}（允许: {', '.join(ALLOWED_OBJECT_TYPES)}）",
        )
    return ot


# ── 展示名解析（评分卡列表 → 人话名；纯函数优先，可单测） ─────────────

# 无 metadata 展示名的系统/存量模型（人工标签）
SYSTEM_MODEL_LABELS: dict[str, str] = {
    "model_qlib": "Qlib 集成模型（系统内置）",
    "alpha158": "Alpha158 系统模型（系统内置）",
    "ensemble_cn": "多模型融合（CN）",
}

# 市场中文名收敛到 shared/market_labels（唯一实现）——本文件曾自带一份，
# 告警文案再自带一份就成三份；别名保留以免改动调用点。
_MARKET_LABELS = MARKET_LABELS

# 用户模型 id 前缀 → 中文（mdl_<market>_<kind>_<ts>_...；训练元数据缺 display_name 时的兜底名）
_MODEL_MARKET_LABELS = {"cn": "A股", "hk": "港股", "us": "美股", "cust": "自定义", "crypto": "加密"}
_MODEL_KIND_LABELS = {"train": "训练模型", "ensemble": "融合模型"}


def fallback_model_label(model_id: str) -> str | None:
    """用户模型 id 解析兜底名（训练元数据无 display_name 时）：

    ``mdl_cn_train_20260906064130_<hash>`` → 「A股 训练模型 · 2026-09-06 06:41」；
    带算法段的 ``mdl_us_train_..._xgboost_<hash>`` → 「美股 训练模型 · xgboost · …」。
    解析不出（非 mdl_ 结构）→ None，由前端回退原始 id。
    """
    parts = str(model_id or "").split("_")
    if len(parts) < 4 or parts[0] != "mdl":
        return None
    market = _MODEL_MARKET_LABELS.get(parts[1].lower(), parts[1].upper())
    kind = _MODEL_KIND_LABELS.get(parts[2].lower(), parts[2])
    ts = parts[3]
    when = ""
    if len(ts) >= 12 and ts.isdigit():
        when = f" · {ts[:4]}-{ts[4:6]}-{ts[6:8]} {ts[8:10]}:{ts[10:12]}"
    # 时间戳后的段里找算法名（跳过 hex 段；连续字母段合并，如 random forest）
    words: list[str] = []
    for seg in parts[4:]:
        is_hex = len(seg) in (8, 16, 32, 64) and all(c in "0123456789abcdef" for c in seg.lower())
        if is_hex:
            if words:
                break
            continue
        if seg.isalpha():
            words.append(seg)
        else:
            break
    algo = f" · {' '.join(words)}" if words else ""
    return f"{market} {kind}{algo}{when}"


def factor_display_name(code: str) -> str | None:
    """因子代码 → 中文名（复用引擎因子词典唯一实现；词典不可用 → None）。"""
    if not code:
        return None
    try:
        from backend.services.engine.data_platform.quantdb_factor_dictionary import (
            definition_for,
        )

        name = str(definition_for(str(code)).get("display_name") or "").strip()
        return name or None
    except Exception:  # noqa: BLE001 — 词典不可用不拖垮读接口
        logger.warning("因子词典解析失败: %s", code, exc_info=True)
        return None


def backtest_display_label(config: dict[str, Any] | None) -> str | None:
    """回测配置 → 可读标签（策略名优先，其次 标的·区间）；无信息 → None。"""
    cfg = config or {}
    for key in ("strategy_name", "name", "title"):
        value = str(cfg.get(key) or "").strip()
        if value:
            return value
    symbol = str(cfg.get("symbol") or "").strip()
    start = str(cfg.get("start_date") or "").strip()
    end = str(cfg.get("end_date") or "").strip()
    if symbol and start and end:
        return f"回测 · {symbol} · {start} ~ {end}"
    return None


def resolve_strategy_labels(
    rows_by_id: dict[str, Any], ids: list[str]
) -> dict[str, str | None]:
    """策略卡展示名（回落链）：**策略表名** → config_json 名 → 标的·区间 → None。

    策略卡 ``object_id`` 是 ``backtest_id``，而回测配置里通常没有策略名
    （实测解析 4/5 为 None）。配置的 ``strategy_id`` 指向 ``strategies`` 表
    （数值型才有；``standard_topk`` 这类内置标识无表行），由此取名。
    ``rows_by_id`` 为 SQL 行映射（含 ``strategy_name`` / ``config_json`` 键）。
    """
    out: dict[str, str | None] = {}
    for i in ids:
        row = rows_by_id.get(i) or {}
        name = str(row.get("strategy_name") or "").strip()
        out[i] = name or backtest_display_label(row.get("config_json"))
    return out


def account_display_label(
    object_id: str, username: str | None = None, *, account_kind: str = "模拟"
) -> str | None:
    """账户 object_id（`{user}:{market}` 口径）→ 可读名（`account_kind`：模拟/实盘）。"""
    raw = str(object_id or "").strip()
    if not raw:
        return None
    user_part, _, market_part = raw.partition(":")
    market = _MARKET_LABELS.get(market_part.upper(), market_part.upper())
    base = f"{market} {account_kind}账户" if market else f"{account_kind}账户"
    who = str(username or "").strip() or (f"用户 {user_part}" if user_part else "")
    return f"{base}（{who}）" if who else base


def strategy_health_display_name(name: Any) -> str | None:
    """体检留档展示名 = 策略名（空名/查无此策略 → None，前端回退「策略 #id」）。"""
    if name is None:
        return None
    cleaned = str(name).strip()
    return cleaned or None


async def _resolve_display_names(
    session: Any, object_type: str, ids: list[str]
) -> dict[str, str | None]:
    """object_id → display_name（尽力而为；任何子查询失败 → {}，绝不影响读接口）。"""
    from sqlalchemy import text as _text

    unique = sorted({str(i) for i in ids if str(i)})
    if not unique:
        return {}
    try:
        if object_type == "factor":
            return {i: factor_display_name(i) for i in unique}
        if object_type == "model":
            names: dict[str, str | None] = {i: SYSTEM_MODEL_LABELS.get(i) for i in unique}
            missing = [i for i in unique if names.get(i) is None]
            if missing:
                rows = (
                    await session.execute(
                        _text(
                            "SELECT model_id, MAX(COALESCE(metadata_json->>'display_name', "
                            "metadata_json->'model_info'->>'name')) AS label "
                            "FROM qm_user_models WHERE model_id = ANY(:ids) GROUP BY model_id"
                        ),
                        {"ids": missing},
                    )
                ).fetchall()
                for model_id, label in rows:
                    if label:
                        names[str(model_id)] = str(label).strip() or None
            # 元数据无 display_name 的训练模型 → 按 id 解析兜底名
            for i in missing:
                if not names.get(i):
                    names[i] = fallback_model_label(i)
            return names
        if object_type == "strategy":
            # 策略名经 config_json->>'strategy_id' 关联 strategies 表（文本比较，
            # 免 cast：'standard_topk' 这类非数值标识自然无匹配 → NULL 走回落链）
            rows = (
                (
                    await session.execute(
                        _text(
                            "SELECT r.backtest_id, r.config_json, "
                            "(SELECT s.name FROM strategies s "
                            " WHERE CAST(s.id AS TEXT) = r.config_json->>'strategy_id') "
                            "AS strategy_name "
                            "FROM qlib_backtest_runs r "
                            "WHERE r.backtest_id = ANY(:ids)"
                        ),
                        {"ids": unique},
                    )
                )
                .mappings()
                .all()
            )
            by_id = {str(r["backtest_id"]): r for r in rows}
            return resolve_strategy_labels(by_id, unique)
        if object_type == "strategy_health":
            # object_id = 策略 id（整型）；展示名取策略名，查无/非数字 → None 由前端回退
            int_ids = [int(i) for i in unique if i.isdigit()]
            if not int_ids:
                return dict.fromkeys(unique)
            rows = (
                await session.execute(
                    _text(
                        "SELECT CAST(id AS TEXT), name FROM strategies "
                        "WHERE id = ANY(:ids)"
                    ),
                    {"ids": int_ids},
                )
            ).fetchall()
            names = {str(r[0]): strategy_health_display_name(r[1]) for r in rows}
            return {i: names.get(i) for i in unique}
        if object_type in ("account", "live_account"):
            # 实盘账户卡 object_id 与模拟账户同形（`{user}:{market}`），取数共用；
            # 只有展示词的「模拟/实盘」不同
            heads = {i: i.partition(":")[0] for i in unique}
            int_ids = [int(h) for h in heads.values() if h.isdigit()]
            unames: dict[str, str] = {}
            if int_ids:
                rows = (
                    await session.execute(
                        _text(
                            "SELECT CAST(id AS TEXT), username FROM users "
                            "WHERE id = ANY(:uids) OR user_id = ANY(:utexts)"
                        ),
                        {"uids": int_ids, "utexts": [str(i) for i in int_ids]},
                    )
                ).fetchall()
                unames = {str(r[0]): str(r[1]) for r in rows if r[1]}
            kind = "实盘" if object_type == "live_account" else "模拟"
            return {
                i: account_display_label(i, unames.get(heads[i]), account_kind=kind)
                for i in unique
            }
    except Exception:  # noqa: BLE001
        logger.warning("评估展示名解析失败（object_type=%s）", object_type, exc_info=True)
        return {}
    # daily_selection 等：object_id 本身可读（日期），无需替换
    return dict.fromkeys(unique)


@router.get("/scores")
async def list_scores(
    object_type: str = Query(..., description="评分卡类型"),
    object_id: str | None = Query(None, description="对象 ID（可选）"),
    latest_only: bool = Query(True, description="每个对象只取最新一条（网格视图）"),
    limit: int = Query(50, ge=1, le=500),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """评分卡最新快照列表（最新优先）。"""
    from sqlalchemy import text as _text

    # 直接以 Python 调用（测试/脚本）时，未传的 Query 默认值是 Query 对象而非 None——
    # 归一化，防止 `if object_id:` 恒真导致静默空集（HTTP 路径不受影响）。
    if not isinstance(object_id, str):
        object_id = None

    ot = _validate_object_type(object_type)
    tenant_id = str(current_user.get("tenant_id") or "default")
    user_id = str(current_user.get("user_id") or "")

    params: dict[str, Any] = {"t": tenant_id, "u": user_id, "n": limit}
    where = "object_type = :ot AND tenant_id = :t AND (user_id = :u OR user_id = '')"
    params["ot"] = ot
    if object_id:
        where += " AND object_id = :oid"
        params["oid"] = str(object_id)

    if latest_only:
        sql = (
            "SELECT DISTINCT ON (object_id) * FROM eval_scores "
            f"WHERE {where} "
            "ORDER BY object_id, snapshot_date DESC, created_at DESC"
        )
        sql = f"SELECT * FROM ({sql}) latest ORDER BY score DESC NULLS LAST LIMIT :n"
    else:
        sql = (
            f"SELECT * FROM eval_scores WHERE {where} "
            "ORDER BY snapshot_date DESC, created_at DESC LIMIT :n"
        )

    async with get_session(read_only=True) as session:
        rows = (await session.execute(_text(sql), params)).mappings().all()
        # U3：被 limit 截断时才补一次 COUNT 说明真实总量（未截断 total=count，一眼即知）
        truncated = len(rows) >= limit
        total: int | None = len(rows)
        if truncated:
            count_sql = (
                f"SELECT COUNT(DISTINCT object_id) FROM eval_scores WHERE {where}"
                if latest_only
                else f"SELECT COUNT(*) FROM eval_scores WHERE {where}"
            )
            count_params = {k: v for k, v in params.items() if k != "n"}
            total = int(
                (await session.execute(_text(count_sql), count_params)).scalar() or 0
            )
        data = [_row_to_score(r) for r in rows]
        names = await _resolve_display_names(session, ot, [d["object_id"] for d in data])
    for item in data:
        item["display_name"] = names.get(item["object_id"])
    return {
        "success": True,
        "data": data,
        "meta": {
            "count": len(data),
            "object_type": ot,
            "total": total,
            "truncated": truncated,
        },
    }


@router.get("/scores/history")
async def score_history(
    object_type: str = Query(...),
    object_id: str = Query(...),
    limit: int = Query(180, ge=2, le=2000),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """单对象评分历史（按日期升序，供历史曲线）。"""
    from sqlalchemy import text as _text

    ot = _validate_object_type(object_type)
    tenant_id = str(current_user.get("tenant_id") or "default")
    user_id = str(current_user.get("user_id") or "")

    async with get_session(read_only=True) as session:
        rows = (
            (
                await session.execute(
                    _text(
                        "SELECT * FROM eval_scores WHERE object_type = :ot AND object_id = :oid "
                        "AND tenant_id = :t AND (user_id = :u OR user_id = '') "
                        "ORDER BY snapshot_date DESC, created_at DESC LIMIT :n"
                    ),
                    {
                        "ot": ot,
                        "oid": str(object_id),
                        "t": tenant_id,
                        "u": user_id,
                        "n": limit,
                    },
                )
            )
            .mappings()
            .all()
        )
    data = [_row_to_score(r) for r in reversed(rows)]  # 升序返回
    return {
        "success": True,
        "data": data,
        "meta": {"count": len(data), "object_type": ot, "object_id": object_id},
    }


def parse_nav_payload(content: str, *, max_points: int = 5000) -> list[dict[str, Any]]:
    """自助体检输入解析（纯函数）：CSV / JSON 两族 → [{date,value}]。

    支持形态：
    - JSON: [v1, v2, ...] 或 [{date,value}] 或 {"equity_curve":[{"date","value"}]}
      （兼容回测结果文件直接上传）；
    - CSV/TSV：首列日期（YYYY-MM-DD/YYYYMMDD 均可，可缺省 → 序号日期）+ 含 value/nav/close 的一列；
      或纯数值两列。
    解析失败抛 ValueError（调用方转 400，附可读原因）。
    """
    import csv as _csv
    import io as _io
    import json as _json
    import re as _re

    text = (content or "").strip()
    if not text:
        raise ValueError("内容为空")
    rows: list[dict[str, Any]] = []

    if text[:1] in "[{":
        try:
            data = _json.loads(text)
        except ValueError as exc:
            raise ValueError(f"JSON 解析失败: {exc}") from exc
        if isinstance(data, dict):
            data = data.get("equity_curve") or data.get("nav_curve") or data.get("nav") or []
        if not isinstance(data, list):
            raise ValueError("JSON 顶层须为数组或含 equity_curve 的对象")
        for item in data:
            if isinstance(item, (int, float)):
                rows.append({"date": None, "value": float(item)})
            elif isinstance(item, dict):
                v = item.get("value", item.get("nav", item.get("close")))
                if v is None:
                    continue
                rows.append({"date": str(item.get("date") or "")[:10] or None, "value": float(v)})
    else:
        reader = _csv.reader(_io.StringIO(text))
        date_re = _re.compile(r"^\d{4}-?\d{2}-?\d{2}$")
        for raw in reader:
            cells = [c.strip() for c in raw if c.strip()]
            if not cells or cells[0].lower() in {"date", "日期", "时间", "trade_date"}:
                continue
            date_val: str | None = None
            nums: list[float] = []
            for cell in cells:
                if date_val is None and date_re.match(cell):
                    date_val = (
                        f"{cell[:4]}-{cell[4:6]}-{cell[6:8]}" if "-" not in cell else cell
                    )
                    continue
                try:
                    nums.append(float(cell))
                except ValueError:
                    continue
            if nums:
                rows.append({"date": date_val, "value": nums[-1]})

    rows = [r for r in rows if r["value"] is not None and r["value"] > 0]
    if len(rows) > max_points:
        raise ValueError(f"点位过多（{len(rows)} > {max_points}），请压缩后重试")
    if len(rows) < 30:
        raise ValueError(f"有效净值点位不足（{len(rows)} < 30），无法体检——至少提供 30 个交易日")
    return rows


@router.post("/health/upload")
async def upload_health_check(
    payload: dict[str, Any] | None = None,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """自助体检（T-FE-15）：上传/粘贴净值曲线 → 九项检验 + 四分类报告。

    纪律：**只读自查**——不落 eval_scores、不参与晋级门禁（门禁数据仅来自
    回测自动体检/月度复检的策略留档）；样本不足/口径不符如实 400。
    """
    _ = current_user
    body = payload if isinstance(payload, dict) else {}
    content = str(body.get("content") or "")
    try:
        rows = parse_nav_payload(content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    from backend.shared.backtest_health import evaluate_for_window

    report = await evaluate_for_window(rows, n_trials=int(body.get("trials") or 1))
    if report is None:
        raise HTTPException(status_code=400, detail="净值序列无法体检（样本或口径不足）")

    from backend.scripts.eval.health_check import render_report

    return {
        "success": True,
        "data": {
            "report": report,
            "report_text": render_report(report),
            "points": len(rows),
            "disclaimer": "自助体检仅供自查，不写入评估档案、不参与策略晋级门禁",
            "source": "scripts/eval/health_check.py（与自动体检同一实现；基准=沪深300 窗口口径）",
        },
    }


@router.get("/health/{strategy_id}")
async def strategy_health(
    strategy_id: str,
    limit: int = Query(24, ge=1, le=240),
    mode: str = Query("SIMULATION", description="启动资格模式：SIMULATION / REAL"),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """策略体检档案：最新报告 + 历史结论 + 晋级门禁预演 + 两模式启动资格清单。

    `mode` 只影响返回的 `eligibility[mode]`（默认 SIMULATION，向后兼容）；
    `gate` 恒为 SIMULATION 预演，既有消费方（体检档案面板）零改动。
    """
    from sqlalchemy import text as _text

    sid = str(strategy_id or "").strip()
    if not sid.isdigit():
        raise HTTPException(status_code=400, detail="strategy_id 须为数字")
    mode_key = _resolve_mode(mode)
    if mode_key not in ("SIMULATION", "REAL"):
        raise HTTPException(status_code=400, detail="mode 须为 SIMULATION 或 REAL")
    tenant_id = str(current_user.get("tenant_id") or "default")
    user_id = str(current_user.get("user_id") or "")

    async with get_session(read_only=True) as session:
        rows = (
            (
                await session.execute(
                    _text(
                        "SELECT * FROM eval_scores WHERE object_type='strategy_health' "
                        "AND object_id=:sid AND tenant_id=:t AND (user_id=:u OR user_id='') "
                        "ORDER BY snapshot_date DESC, created_at DESC LIMIT :n"
                    ),
                    {"sid": sid, "t": tenant_id, "u": user_id, "n": limit},
                )
            )
            .mappings()
            .all()
        )
    history = [_row_to_score(r) for r in rows]
    latest = history[0] if history else None
    latest_health = None
    if latest:
        dims = latest["dimensions"] or {}
        latest_health = {
            "verdict": latest["grade"],
            "verdict_label": dims.get("verdict_label"),
            "confidence": latest["score"],
            "reasons": dims.get("reasons") or [],
            "suggestions": dims.get("suggestions") or [],
            "items": dims.get("items") or {},
            "backtest_id": (latest["inputs_version"] or {}).get("backtest_id"),
            "evidence_source": (latest["inputs_version"] or {}).get("evidence_source"),
            "snapshot_date": latest["snapshot_date"],
        }
    eligibility = await build_start_eligibility_map(
        sid, latest_health, tenant_id=tenant_id, user_id=user_id
    )
    return {
        "success": True,
        "data": {
            "strategy_id": sid,
            "latest": latest_health,
            "history": [
                {
                    "snapshot_date": h["snapshot_date"],
                    "verdict": h["grade"],
                    "confidence": h["score"],
                    "evidence_source": (h["inputs_version"] or {}).get("evidence_source"),
                }
                for h in history
            ],
            "gate": build_gate_preview(latest_health, mode="SIMULATION"),
            "eligibility": eligibility,
            "eligibility_mode": mode_key,
        },
        "meta": {"count": len(history)},
    }


@router.get("/series")
async def object_series(
    object_type: str = Query(..., description="评分卡类型"),
    object_id: str = Query(..., description="对象 ID"),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """单对象长序列侧车（逐日 IC / 分位线等，设计 §1.6）。

    列表接口（`/scores`）只带标量 detail，长序列按需取——选中对象时才读这一个
    文件，列表刷新不必拖着几十条序列走。

    `available=false` 是**正常返回**（该对象尚未产出序列 / 侧车口径过期），
    前端据此显示「暂无序列」而不是报错；只有非法 `object_id`（路径穿越）
    与「本租户看不见这个对象」才是 400/404。
    """
    from sqlalchemy import text as _text

    from backend.shared.eval_series import is_series_id, load_series

    ot = _validate_object_type(object_type)
    tenant_id = str(current_user.get("tenant_id") or "default")
    user_id = str(current_user.get("user_id") or "")
    oid = str(object_id)

    # 先验输入再碰库：`../` 这种 id 不该换来一次 DB 往返，而且它是个输入错误，
    # 不该混进「看不见 → 404」那条路（否则穿越尝试与不存在的对象无法区分）。
    # 用 is_series_id 而非 safe_object_id：账户 id 是「用户:市场」（`10000001:CN`），
    # 冒号经 series_filename 编码后可以落盘，原样形态判据会把它误判成非法。
    if not is_series_id(oid):
        raise HTTPException(status_code=400, detail=f"object_id 非法（禁止路径穿越）: {oid!r}")

    # 可见性：侧车在盘上没有租户/用户维度，先按 eval_scores 的同一谓词确认调用方
    # 看得见这个对象，否则「知道 id 就能读别人策略的序列」。
    async with get_session(read_only=True) as session:
        visible = (
            await session.execute(
                _text(
                    "SELECT 1 FROM eval_scores WHERE object_type = :ot AND object_id = :oid "
                    "AND tenant_id = :t AND (user_id = :u OR user_id = '') LIMIT 1"
                ),
                {"ot": ot, "oid": oid, "t": tenant_id, "u": user_id},
            )
        ).first()
    if visible is None:
        raise HTTPException(status_code=404, detail=f"评分对象不存在或不可见: {ot}/{oid}")

    loaded = load_series(ot, oid)
    if loaded["reason"] == "unsafe_object_id":
        # 上面已拦过，正常到不了这里。保底是因为 load_series 的失败态是
        # `available=false`（前端读作「暂无序列」）——输入错误不该被化妆成「没有数据」。
        raise HTTPException(status_code=400, detail=loaded["note"])
    return {
        "success": True,
        "data": loaded["data"],
        "meta": {
            "object_type": ot,
            "object_id": oid,
            "available": loaded["available"],
            "reason": loaded["reason"],
            "note": loaded["note"],
            "generated_at": loaded["generated_at"],
            "version": loaded["version"],
        },
    }


class EvalRunRequest(BaseModel):
    """手动重评请求体；`date` 只影响每日选股卡（其余卡仍是当日快照）。"""

    date: str | None = None


def _trade_redis():
    """trade 库（db2）客户端——与 worker 读请求键必须同库，否则结构性空转。"""
    from backend.services.trade_shared.redis_client import get_redis as get_trade_redis

    return get_trade_redis()


@router.post("/run")
async def trigger_eval_run(
    payload: EvalRunRequest | None = None,
    current_user: dict = Depends(require_admin),
) -> dict[str, Any]:
    """手动重评（管理员）：写 Redis 指令，由 trade worker 消费。

    不在 API 进程跑评分：模型卡/因子 PFS 是百秒级 pandas/parquet 重活，会把交易
    HTTP 全部卡死（2026-10-20 看门狗崩溃循环的根因）。这里只做三件事：参数校验、
    去重、写 `eval:run:request`。
    """
    if not eval_trigger.eval_worker_enabled():
        raise HTTPException(
            status_code=503,
            detail={
                "code": "eval_worker_disabled",
                "message": "评分 worker 已关闭（EVAL_SCORES_WORKER_ENABLED=0），手动重评不会被消费",
            },
        )
    raw_date = payload.date if payload is not None else None
    try:
        date_str = eval_trigger.validate_run_date(raw_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        req = eval_trigger.request_run(
            _trade_redis(),
            requested_by=current_user.get("user_id"),
            tenant_id=current_user.get("tenant_id") or "default",
            date_str=date_str,
        )
    except eval_trigger.EvalRunBusy as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "eval_run_busy",
                "message": "已有评估重评在排队或执行，请等待完成",
                "status": exc.status,
            },
        ) from exc
    except eval_trigger.EvalRunUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "eval_trigger_unavailable",
                "message": "trade Redis 不可用，无法排队；请检查 Redis 连接",
            },
        ) from exc

    return {
        "success": True,
        "data": {
            "request": req,
            "status": eval_trigger.build_run_status("queued", request=req),
        },
    }


@router.get("/run/status")
async def eval_run_status(
    current_user: dict = Depends(require_admin),
) -> dict[str, Any]:
    """最近一次手动重评状态；无记录 → `idle`（不是 404，前端不必判空）。"""
    try:
        status = eval_trigger.read_run_status(_trade_redis())
    except eval_trigger.EvalRunUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "eval_trigger_unavailable",
                "message": "trade Redis 不可用，无法读取重评状态",
            },
        ) from exc
    return {"success": True, "data": status}


@router.get("/object-types")
async def object_types() -> dict[str, Any]:
    """支持的评分卡类型（前端页签枚举的唯一来源）。"""
    labels = {
        "factor": "因子评分卡",
        "model": "模型评分卡",
        "strategy": "策略评分卡",
        "account": "账户评分卡",
        "live_account": "实盘账户评分卡",
        "daily_selection": "每日选股",
        "strategy_health": "体检留档",
    }
    # U6：页签短名后端直给——label 全称怎么改都不再牵连前端页签
    short_labels = {
        "factor": "因子",
        "model": "模型",
        "strategy": "策略",
        "account": "账户",
        "live_account": "实盘账户",
        "daily_selection": "每日选股",
        "strategy_health": "体检",
    }
    return {
        "success": True,
        "data": [
            {"object_type": ot, "label": labels[ot], "short_label": short_labels[ot]}
            for ot in ALLOWED_OBJECT_TYPES
        ],
        "meta": {"count": len(ALLOWED_OBJECT_TYPES)},
    }
