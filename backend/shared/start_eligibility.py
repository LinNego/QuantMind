"""启动资格清单（展示面唯一实现，与执行点同源）。

背景（细案 2026-10-13）：策略「不能启动」原先只在 `/start` 抛一个 400 纯字符串，
前端只能弹一次转瞬即逝的 toast，用户看不出**差哪一项、下一步做什么**。
本模块把「能不能启动」拆成逐项清单，供两处消费：

1. **执行面**：`real_trading_lifecycle.start_trading` 的 400 detail 带 `gate_items`；
2. **展示面**：`GET /api/v1/eval/health/{strategy_id}` 的 `eligibility`（启动前可见）。

纪律：判据**全部复用执行点**——`can_start`（生命周期）、`promotion_gate`（回测体检
晋级）、`SIM_REQUIRED_VERDICTS` / `LIVE_SIM_MIN_TRADING_DAYS` / `LIVE_MAX_TRACKING_ERROR`
（T-P3-05 门槛总表）。本模块**不新增阈值、不另写判断**，否则展示面会与执行面漂移。
纯函数、无 IO —— 取数（体检留档 / SIM 天数 / 跟踪误差）由调用方负责。
"""

from __future__ import annotations

from typing import Any

from backend.shared.backtest_health import (
    LIVE_MAX_TRACKING_ERROR,
    LIVE_SIM_MIN_TRADING_DAYS,
    live_sim_days_blocked_note,
    live_tracking_error_blocked_note,
    promotion_gate,
)
from backend.shared.strategy_lifecycle import can_start, normalize_status

# 展示词：值缺省时的占位（**缺失一律说「无记录」，绝不显示成 0**）
_NO_RECORD = "无记录"

_LABELS = {
    "lifecycle": "生命周期状态",
    "health": "回测体检",
    "sim_days": "模拟交易日",
    "tracking_error": "跟踪误差",
}


def _lifecycle_next_step(status: str, mode: str) -> str:
    """按当前状态与目标模式给**可执行**的下一步（不是复述拒绝理由）。"""
    if mode == "SIMULATION":
        if status == "DRAFT":
            return "先完成回测验证，使策略状态变为「已验证」"
        if status == "LIVE":
            return "先在交易台停止实盘，再切回模拟盘启动"
        return "检查策略状态：启动模拟须「已验证」或「模拟中」"
    if status == "VERIFIED":
        return "先切到模拟盘启动一次，完成模拟验证后再进实盘"
    if status == "DRAFT":
        return "先完成回测验证，再切模拟盘启动一次"
    return "检查策略状态：启动实盘须「模拟中」或「实盘中」"


def _build_item(
    *,
    key: str,
    passed: bool,
    value: str,
    reason: str = "",
    next_step: str = "",
) -> dict[str, Any]:
    return {
        "key": key,
        "label": _LABELS[key],
        "value": value,
        "passed": passed,
        "reason": reason if not passed else "",
        "next_step": next_step if not passed else "",
    }


def _health_item(
    health: dict[str, Any] | None,
    *,
    mode: str,
    gate_on: bool,
) -> dict[str, Any]:
    """体检项：判据直接取 `promotion_gate(..., mode="SIMULATION")`（结论门槛两模式相同）。"""
    if not gate_on:
        return _build_item(
            key="health",
            passed=True,
            value="体检门禁已关闭（HEALTH_GATE_ENABLED=false）",
        )
    ok, note = promotion_gate(health, mode="SIMULATION")
    if health is None:
        value = "尚无体检留档"
    else:
        verdict = str(health.get("verdict") or "").strip().upper() or "—"
        confidence = health.get("confidence")
        value = f"{verdict} · 可信度 {confidence}/100" if confidence is not None else verdict
    return _build_item(
        key="health",
        passed=ok,
        value=value,
        reason=note,
        next_step="到「回测中心 → 策略管理 → 回测验证」重跑一次该策略的回测，体检结论 A/B 后方可晋级",
    )


def _sim_days_item(sim_trading_days: int | None) -> dict[str, Any]:
    if sim_trading_days is None:
        return _build_item(
            key="sim_days",
            passed=True,
            value=f"{_NO_RECORD}（只提示不拦）",
        )
    ok = sim_trading_days >= LIVE_SIM_MIN_TRADING_DAYS
    # 理由直接取本项专属措辞（唯一出处 backtest_health）——不再借道 promotion_gate：
    # 它按门槛总表顺序短路返回，前序门未过时会把别人的结论当成本项理由（2026-10-15 回归）
    reason = "" if ok else live_sim_days_blocked_note(sim_trading_days)
    return _build_item(
        key="sim_days",
        passed=ok,
        value=f"{sim_trading_days} / {LIVE_SIM_MIN_TRADING_DAYS} 交易日",
        reason=reason,
        next_step=f"继续模拟运行，累计满 {LIVE_SIM_MIN_TRADING_DAYS} 个交易日后再进实盘",
    )


def _tracking_error_item(tracking_error: float | None) -> dict[str, Any]:
    if tracking_error is None:
        return _build_item(
            key="tracking_error",
            passed=True,
            value=f"{_NO_RECORD}（只提示不拦）",
        )
    ok = tracking_error <= LIVE_MAX_TRACKING_ERROR
    reason = "" if ok else live_tracking_error_blocked_note(tracking_error)
    return _build_item(
        key="tracking_error",
        passed=ok,
        value=f"{tracking_error:.2%} / ≤ {LIVE_MAX_TRACKING_ERROR:.0%}",
        reason=reason,
        next_step="排查模拟与真单的执行偏差（成交价/滑点/费率），把跟踪误差压入门槛内",
    )


def build_start_eligibility(
    *,
    status: Any,
    health: dict[str, Any] | None,
    mode: Any,
    sim_trading_days: int | None = None,
    tracking_error: float | None = None,
    health_gate_enabled: bool = True,
    source: str = "user_strategy",
) -> dict[str, Any]:
    """启动资格清单（纯函数）。

    - `source != "user_strategy"`（`sys_` 模板 / 文件上传）→ 直接放行：与 `/start`
      一致，模板不吃状态机与体检门禁；
    - SIMULATION：生命周期 + 体检；
    - REAL：生命周期 + 体检 + 模拟交易日 + 跟踪误差；
    - 返回 `{allowed, mode, items, next_step}`，`items[i] = {key, label, value,
      passed, reason, next_step}`；`next_step`（顶层）= 首个未过项的下一步。
    """
    mode_key = str(mode or "").strip().upper()

    if source != "user_strategy":
        return {"allowed": True, "mode": mode_key, "items": [], "next_step": ""}

    items: list[dict[str, Any]] = []

    lifecycle_ok, lifecycle_reason = can_start(status, mode)
    normalized = normalize_status(status)
    items.append(
        _build_item(
            key="lifecycle",
            passed=lifecycle_ok,
            value=normalized,
            reason=lifecycle_reason,
            next_step=_lifecycle_next_step(normalized, mode_key),
        )
    )

    items.append(_health_item(health, mode=mode_key, gate_on=health_gate_enabled))

    if mode_key == "REAL" and health_gate_enabled:
        items.append(_sim_days_item(sim_trading_days))
        items.append(_tracking_error_item(tracking_error))

    allowed = all(item["passed"] for item in items)
    next_step = ""
    if not allowed:
        next_step = next((i["next_step"] for i in items if not i["passed"]), "")
        if not next_step:
            next_step = "请检查上述未通过项后重试"

    return {"allowed": allowed, "mode": mode_key, "items": items, "next_step": next_step}
