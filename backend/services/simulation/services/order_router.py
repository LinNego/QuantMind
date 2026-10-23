"""OrderRouter（T-P2-01）：模拟盘**下单唯一入口**。

五条下单路径（托管引擎 / 手动·内部单 / 沙箱信号 / TDX 滚动 paper / 保证金平仓）
全部改经本入口；链内统一提供：参数校验 → 账户锁 → 幂等（client_order_id）→
建单（Order 契约列落 source/client_order_id）→ 撮合 → 落账（apply_filled→Ledger）→
（可选）真单镜像。

**架构决策（细案论证）**：组合而非重写——即时链委托给已被线上验证的
``SimulationOrderSubmissionService``（账户锁/幂等/会话窗/投影），本模块在其上补齐：
① 统一请求/结果契约；② from_bar 托管模式（周期调仓）；③ 镜像收口（修沙箱链路
"永不同步真单"）；④ strict_market 分级（手动=True 保 P0-5；自动化=False 允许
如实标注的降级，见取价契约 T-P2-03）。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from backend.shared.order_contract import SOURCE_MANUAL

logger = logging.getLogger(__name__)


@dataclass
class OrderRequest:
    tenant_id: str
    user_id: int  # 归一 int uid（调用方保证；0 视为无效）
    symbol: str
    side: str  # buy | sell
    quantity: float
    order_type: str = "market"  # market | limit
    price: float | None = None
    source: str = SOURCE_MANUAL
    client_order_id: str | None = None
    strategy_id: int | None = None
    portfolio_id: int = 0
    remarks: str | None = None
    trade_action: str | None = None
    position_side: str = "long"
    is_margin_trade: bool = False
    bar: Any = None  # 非空 → 托管 from_bar 撮合；空 → 即时撮合
    run_id: str = ""
    mirror: bool = False  # 成交后是否触发真单镜像
    mirror_source: str = ""
    #: 真单限价（仅约束镜像出去的那一笔）。``None`` = 由镜像按参考价 ± 滑点带派生。
    #: 模拟腿**不看**这个字段：模拟成交价仍取服务端快照价。
    real_limit_price: float | None = None
    strict_market: bool | None = None  # None=按模式自动
    #: P2.7 分账：这条腿属于哪家模型（LLM 决策腿由执行段传入）。落 ``sim_orders.agent``
    #: 并随镜像 payload 传给真单——成交回报只能从订单上读归属，别处都读不到。
    agent: str = ""
    #: **实盘直连**（真实轮专用）：不建模拟单、不落模拟账，直接走真单链。
    #: 2026-10-19 用户口径「所有操作也是下单到实盘、没有模拟盘」——真实轮的持仓与
    #: 可卖量已在执行段按**真实账户**校验过，模拟腿只会制造账面分叉与假拒单。
    direct_real: bool = False

    def resolved_strict(self) -> bool:
        if self.strict_market is not None:
            return bool(self.strict_market)
        return self.bar is None  # 即时默认 strict（P0-5）；托管允许如实降级


@dataclass
class RouterOutcome:
    success: bool
    order_id: str | None = None
    trade_id: str | None = None
    client_order_id: str | None = None
    fill_price: float = 0.0
    filled_quantity: float = 0.0
    commission: float = 0.0
    price_source: str | None = None
    message: str = ""
    duplicate: bool = False
    mirror: dict | None = None


def is_duplicate_message(message: str | None) -> bool:
    """幂等命中判定（纯函数，与 SubmissionService 的话术契约一致）。"""
    return str(message or "").startswith("duplicate client_order_id")


async def _record_risk_rejection(db, req: OrderRequest, message: str) -> None:
    """风控前置拒单补记台账（审计：拒单不能只活在运行日志里）。

    2026-10-22 实证：托管模拟盘一轮 20 张卖单全被 ``l3.stale_quote`` 拒，
    ``sim_orders`` / ``simulation_orders`` 两表 0 行——用户翻「历史记录」看不到
    任何拒绝记录。风控卡点在**建单之前**（T-RC-02），所以必须在这里补一条
    rejected 台账（v1 台账 + V2 投影同写）。

    边界：只记**风控**拒单（参数错误属调用方 bug，不产生业务事件）；实盘直连
    （``direct_real``）不建模拟腿；无 db 的单元路径直接跳过。best-effort——
    补记失败只告警，绝不影响拒单结论。
    """
    if db is None or getattr(req, "direct_real", False):
        return
    try:
        from backend.services.simulation.models.order import (
            OrderSide,
            OrderStatus,
            OrderType,
        )
        from backend.services.simulation.schemas.order import SimOrderCreate
        from backend.services.simulation.services.order_service import SimOrderService
        from backend.shared.order_contract import ensure_order_contract_columns_async

        await ensure_order_contract_columns_async()
        service = SimOrderService(db)
        order = await service.create_order(
            req.tenant_id,
            str(req.user_id),
            SimOrderCreate(
                portfolio_id=max(0, int(req.portfolio_id or 0)),
                strategy_id=req.strategy_id,
                client_order_id=req.client_order_id,
                symbol=req.symbol,
                side=OrderSide(str(req.side or "").strip().lower()),
                order_type=OrderType(str(req.order_type or "").strip().lower()),
                quantity=float(req.quantity),
                price=float(req.price) if req.price and float(req.price) > 0 else None,
                remarks=req.remarks,
                position_side=str(req.position_side or "long").strip().lower(),
                is_margin_trade=bool(req.is_margin_trade),
                agent=req.agent,
            ),
            trigger_source=req.source,
        )
        order.status = OrderStatus.REJECTED
        order.remarks = f"Execution rejected: {message}"[:500]
        await service.sync_order_projection(order, rejected_reason=message[:500])
        await db.commit()
    except Exception as exc:  # noqa: BLE001 - 审计补记失败不得改变拒单结论
        from backend.services.simulation.services.order_service import (
            DuplicateSimOrderError,
        )

        if isinstance(exc, DuplicateSimOrderError):
            # 同一幂等键已有台账行（含此前被接受/拒绝的那一笔）——不重复写、不覆盖
            return
        logger.warning(
            "[OrderRouter] 风控拒单落台账失败（不影响拒单） %s %s: %s",
            req.symbol,
            req.side,
            exc,
        )


async def submit_order(db, redis, req: OrderRequest) -> RouterOutcome:
    """唯一入口。db=AsyncSession；redis=交易 Redis 客户端（SimulationAccountManager 用）。

    **风控卡点（T-RC-02）**：参数校验后、任何建单动作前，一律过 `risk_gate_service.check_order`
    ——影子模式只留痕不拦单；强制模式 REJECT/HALT 即拒（消息含 rule_id，可解释）。
    判定异常/配置不可读 = fail-closed 拒单（见 risk_gate_service 文档）。

    **拒单留痕**：风控拒单在建单之前，必须显式补记 rejected 台账（`_record_risk_rejection`），
    否则历史记录里查不到（2026-10-22 托管模拟盘 20 张卖单全拒却 0 行）。
    """
    from backend.services.trade.services.risk_gate_service import check_order as _risk_check
    from backend.services.trade_shared.simulation_manager import SimulationAccountManager

    if not str(req.symbol or "").strip():
        return RouterOutcome(success=False, message="参数无效：symbol 为空")
    if float(req.quantity or 0) <= 0:
        return RouterOutcome(success=False, message="参数无效：quantity<=0")
    uid = int(req.user_id or 0)
    if uid <= 0:
        return RouterOutcome(success=False, message="无效用户 ID")

    risk = await _risk_check(req, db=db, redis=redis)
    if not risk.passed:
        message = f"风控拒单[{risk.rule_id or 'risk'}]：{risk.reason}"[:300]
        await _record_risk_rejection(db, req, message)
        return RouterOutcome(
            success=False,
            message=message,
        )

    if getattr(req, "direct_real", False):
        # 真实轮：**没有模拟腿**（用户 2026-10-19 口径）。风控照过，真单链直连；
        # 归属随 ``agent`` 落 ``orders.agent``，成交回报照旧记进分账账本。
        return await _submit_direct_real(redis, req)

    manager = SimulationAccountManager(redis)
    # bar **不再切换执行链**，只经即时链传给取价（`bar → submit_and_fill → execute_order
    # → _resolve_fill_price`）。
    #
    # 此前 `req.bar is not None` 会路由到 `_submit_from_bar`，而那条链**不调用
    # `assess_execution_window`** —— 即没有交易时段闸。让它可路由，等于让托管的每张
    # 调仓单在周末/深夜/任意时刻都能按日线 bar 成交；且它与即时链是两套平行实现
    # （会话窗、幂等、投影、申报数量校验各写一遍），正是取价口径悄悄分裂的温床。
    #
    # 降级取价不需要第二条链：`_resolve_fill_price(order, bar, strict_market=...)` 早已把
    # 「实时价优先、无则按 bar 如实标注降级」单点实现，即时链传入即可。
    routed = await _submit_immediate(db, manager, req)

    if req.mirror and routed.success and not routed.duplicate:
        routed.mirror = await _mirror_fill(redis, req, routed)
    return routed


async def cancel_all(
    db,
    redis,
    *,
    tenant_id: str,
    user_id: int,
    reason: str = "risk_halt",
    limit: int = 500,
) -> dict[str, Any]:
    """HALT 全撤（T-RC-02 首版）：撤销该账户全部**未成交模拟单**（pending/submitted）。

    复用 `SimOrderService.cancel_order`（状态守卫 + V2 投影镜像——否则 worker 重放已撤单）。
    实盘/影子环境的全撤走各自通道（QMT 撤单通道属 T-RC-03 批；此处 scope 限定模拟域）。
    返回 {cancelled, failed, orders:[...]}；单笔失败不中断其余（如实计数）。
    """
    from backend.services.simulation.models.order import OrderStatus
    from backend.services.simulation.services.order_service import SimOrderService

    service = SimOrderService(db)
    cancelled: list[str] = []
    failed: list[dict[str, str]] = []
    for status in (OrderStatus.PENDING.value, OrderStatus.SUBMITTED.value):
        orders = await service.list_orders(tenant_id, str(user_id), status=status, limit=limit)
        for order in orders:
            try:
                await service.cancel_order(order, reason=reason)
                cancelled.append(str(order.order_id))
            except Exception as exc:  # noqa: BLE001 - 单笔失败不中断
                failed.append({"order_id": str(order.order_id), "error": str(exc)[:120]})
    logger.warning(
        "[OrderRouter] cancel_all tenant=%s user=%s reason=%s → 撤 %d 失败 %d",
        tenant_id, user_id, reason, len(cancelled), len(failed),
    )
    return {"cancelled": len(cancelled), "failed": len(failed), "orders": cancelled, "errors": failed}


async def _submit_immediate(db, manager, req: OrderRequest) -> RouterOutcome:
    """即时路径：委托给统一提交链（锁/幂等/会话窗/投影/落账 全在其内）。"""
    from backend.services.simulation.services.order_submission_service import (
        SimulationOrderSubmissionService,
    )

    service = SimulationOrderSubmissionService(db, manager)
    outcome = await service.submit_and_fill(
        tenant_id=req.tenant_id,
        user_id=req.user_id,
        symbol=req.symbol,
        side=req.side,
        quantity=req.quantity,
        order_type=req.order_type,
        price=req.price,
        portfolio_id=req.portfolio_id,
        strategy_id=req.strategy_id,
        trade_action=req.trade_action,
        position_side=req.position_side,
        is_margin_trade=req.is_margin_trade,
        remarks=req.remarks,
        client_order_id=req.client_order_id,
        trigger_source=req.source,
        strict_market=req.resolved_strict(),
        bar=req.bar,
        agent=req.agent,
    )
    return RouterOutcome(
        success=bool(outcome.success),
        order_id=outcome.order_id,
        trade_id=outcome.trade_id,
        client_order_id=outcome.client_order_id,
        fill_price=float(outcome.fill_price or 0.0),
        filled_quantity=float(outcome.filled_quantity or 0.0),
        commission=float(outcome.commission or 0.0),
        price_source=outcome.price_source,
        message=str(outcome.message or ""),
        duplicate=is_duplicate_message(outcome.message),
    )


async def _submit_direct_real(redis, req: OrderRequest) -> RouterOutcome:
    """实盘直连：``mirror_virtual_fill`` 的真单链，**不经模拟台账**。

    为什么另开这一支（2026-10-19 卖出链诊断）：

    1. **历史/外部仓位不在共享模拟账上**。模型账本（``qm_agent_ledger_position``）
       与桥账户是真相，共享模拟账只是镜子；镜子缺这只票时，模拟腿会以
       ``Insufficient holdings`` 拒掉，镜像永远轮不到（10/13 SZ002074、10/15
       SH601857 即此）。
    2. **T+1 可卖量只有桥知道**。执行段已按真实账户的 ``available_volume`` 判过
       ``l1.t1_sellable``；模拟账的 T+1 状态是第二份口径，不该再拦一次。
    3. **卖出的价格偏离闸门以昨收为锚**。下跌日出逃单必然偏离 >2% 被
       ``price_drift`` 跳过，而模拟腿已经成交——真账没动、模拟账少了仓，越跑越歪
       （10/15 SH600338 三次都是这个形态）。卖出腿在这里走
       ``bypass_price_gate=True``：以**盘口价**为限价基准，另设 20% 脏数据上界。

    价格口径：优先 ``real_limit_price``（决策段按盘口算的限价），缺则用 ``price``。
    两者都不可用 → **不下**（不猜价）。买入腿不豁免偏离闸门：追价不是出逃。
    """
    from backend.services.live_trading.services.real_mirror_service import (
        mirror_virtual_fill,
    )
    from backend.services.simulation.services.market_rules import infer_market

    side = str(req.side or "").strip().lower()
    raw_price = req.real_limit_price if req.real_limit_price is not None else req.price
    try:
        price = float(raw_price) if raw_price is not None else 0.0
    except (TypeError, ValueError):
        price = 0.0
    if not math.isfinite(price) or price <= 0:
        return RouterOutcome(
            success=False,
            message="实盘直连缺少可用价格（不猜价，本腿不下）",
            client_order_id=req.client_order_id,
        )

    try:
        mirror = await mirror_virtual_fill(
            db=None,
            redis=redis,
            tenant_id=req.tenant_id,
            user_id=str(req.user_id),
            symbol=req.symbol,
            side=side,
            quantity=float(req.quantity or 0.0),
            price=price,
            client_order_id=str(req.client_order_id or ""),
            sim_order_id="",
            run_id=req.run_id,
            strategy_id=str(req.strategy_id or ""),
            market=infer_market(req.symbol).value,
            source=req.mirror_source or req.source,
            # 卖出 = 出逃意图：偏离闸门按盘口价豁免（仍有 20% 脏数据上界）。
            bypass_price_gate=side == "sell",
            trigger="LLM 决策腿直接下单到实盘（不经模拟台账）",
            limit_price=req.real_limit_price,
            agent=req.agent,
        )
    except Exception as exc:  # noqa: BLE001 - mirror 自身吞异常，这里是兜底
        logger.error("[OrderRouter] 实盘直连异常 %s %s: %s", req.symbol, side, exc)
        return RouterOutcome(
            success=False,
            message=f"实盘直连异常：{type(exc).__name__}: {exc}"[:300],
            client_order_id=req.client_order_id,
        )

    status = str(mirror.get("status") or "")
    # 「已提交/排队/重复」都是**受理**（真单已出门或不会重复出门），不是已成交；
    # 成交与否由 ``orders``/成交流水回答。跳过/失败/异常一律如实算失败。
    accepted = status in {"submitted", "queued", "duplicate"}
    message = ""
    if not accepted:
        reason = str(mirror.get("reason") or status or "unknown")
        prefix = "实盘闸门未放行" if status == "skipped" else "实盘真单未成功"
        message = f"{prefix}：{reason}"[:300]
    return RouterOutcome(
        success=accepted,
        order_id=str(mirror.get("order_id") or "") or None,
        client_order_id=req.client_order_id,
        message=message,
        duplicate=status == "duplicate",
        mirror=mirror,
    )


async def _submit_from_bar(db, manager, req: OrderRequest) -> RouterOutcome:
    """托管周期路径：锁→幂等→建单→execute_from_bar→落账（链内补齐）。"""
    from backend.services.simulation.models.order import (
        OrderSide,
        OrderStatus,
        OrderType,
    )
    from backend.services.simulation.schemas.order import SimOrderCreate
    from backend.services.simulation.services.execution_engine import (
        SimulationExecutionEngine,
    )
    from backend.services.simulation.services.order_service import SimOrderService
    from backend.shared.order_contract import ensure_order_contract_columns_async
    from backend.services.trade_shared.simulation_manager import (
        SimulationAccountManager,
    )

    from backend.services.simulation.services.order_service import (
        DuplicateSimOrderError,
    )

    order_service = SimOrderService(db)
    engine = SimulationExecutionEngine(db, manager)
    cid = str(req.client_order_id or "").strip() or None

    if cid:
        # T-P2-08：先查**台账本体**（sim_orders，投影可能为空——查错表是幂等断链的历史根因），
        # 投影作为旧数据的兜底探测
        existing_sim = await order_service.get_sim_order_by_client_order_id(
            tenant_id=req.tenant_id,
            user_id=req.user_id,
            client_order_id=cid,
        )
        if existing_sim is not None:
            return RouterOutcome(
                success=True,
                order_id=str(existing_sim.order_id),
                client_order_id=cid,
                duplicate=True,
                message="duplicate client_order_id skipped",
            )
        existing = await order_service.get_projection_order_by_client_order_id(
            tenant_id=req.tenant_id,
            user_id=req.user_id,
            client_order_id=cid,
        )
        if existing is not None:
            return RouterOutcome(
                success=True,
                order_id=str(existing.order_id),
                client_order_id=cid,
                duplicate=True,
                message="duplicate client_order_id skipped",
            )

    try:
        lock_cm = SimulationAccountManager.locked_execution(int(req.user_id), req.tenant_id)
    except Exception:
        lock_cm = None
    if lock_cm is None:
        return RouterOutcome(success=False, message="账户撮合繁忙，请稍后重试")

    async with lock_cm:
        await ensure_order_contract_columns_async()
        try:
            order = await order_service.create_order(
                req.tenant_id,
                str(req.user_id),
                SimOrderCreate(
                    portfolio_id=max(0, int(req.portfolio_id or 0)),
                    strategy_id=req.strategy_id,
                    client_order_id=cid,
                    symbol=req.symbol,
                    side=OrderSide(str(req.side or "").strip().lower()),
                    order_type=OrderType(str(req.order_type or "").strip().lower()),
                    quantity=float(req.quantity),
                    price=float(req.price) if req.price and float(req.price) > 0 else None,
                    remarks=req.remarks,
                    position_side=str(req.position_side or "long").strip().lower(),
                    is_margin_trade=bool(req.is_margin_trade),
                    agent=req.agent,
                ),
                trigger_source=req.source,
            )
        except DuplicateSimOrderError as exc:
            # T-P2-08：唯一索引竞态兜底——锁内并发/重放命中同一幂等键
            return RouterOutcome(
                success=True,
                order_id=str(exc.existing.order_id),
                client_order_id=cid,
                duplicate=True,
                message="duplicate client_order_id skipped",
            )
        order.status = OrderStatus.SUBMITTED
        order.submitted_at = order.submitted_at or datetime.now(timezone.utc)
        await order_service.sync_order_projection(order)
        await db.commit()

        result = await engine.execute_from_bar(order, req.bar)
        if not result.success:
            await engine.mark_rejected(order, result.message)
            # 终态镜像 V2 投影：否则投影滞留 pending 会被 pending worker 重放执行
            await order_service.sync_order_projection(
                order, rejected_reason=str(result.message or "")[:500] or None
            )
            await db.commit()
            return RouterOutcome(
                success=False,
                order_id=str(order.order_id),
                client_order_id=cid,
                price_source=result.price_source,
                message=str(result.message or ""),
            )
        trade = await engine.apply_filled(order, result)
        await order_service.sync_order_projection(order)
        return RouterOutcome(
            success=True,
            order_id=str(order.order_id),
            trade_id=str(trade.trade_id),
            client_order_id=cid,
            fill_price=round(float(result.price or 0.0), 4),
            filled_quantity=float(result.quantity or 0.0),
            commission=float(result.commission or 0.0),
            price_source=result.price_source,
            message="filled",
        )


async def _mirror_fill(redis, req: OrderRequest, routed: RouterOutcome) -> dict | None:
    """成交后真单镜像（收口在 Router；mirror_virtual_fill 自身吞异常）。"""
    try:
        from backend.services.live_trading.services.real_mirror_service import (
            mirror_virtual_fill,
        )
        from backend.services.simulation.services.market_rules import infer_market

        market = infer_market(req.symbol).value
        return await mirror_virtual_fill(
            db=None,
            redis=redis,
            tenant_id=req.tenant_id,
            user_id=str(req.user_id),
            symbol=req.symbol,
            side=req.side,
            quantity=routed.filled_quantity or req.quantity,
            price=routed.fill_price,
            sim_order_id=str(routed.order_id or ""),
            run_id=req.run_id,
            strategy_id=str(req.strategy_id or ""),
            market=market,
            source=req.mirror_source or req.source,
            limit_price=req.real_limit_price,
            agent=req.agent,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[OrderRouter] 镜像调用异常（不影响虚拟成交）: %s", exc)
        return None
