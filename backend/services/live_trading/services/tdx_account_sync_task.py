"""
TDX Account Sync Task - 定期把通达信桥账户/持仓落库到 real_account_snapshots

供前端 REAL 模式 /account 接口读取通达信实盘持仓。

本任务同时是「桥掉线」告警的探测端：每次同步结论喂给
:class:`~backend.services.live_trading.services.bridge_health_watch.BridgeHealthTracker`，
连续失败/持续空账户即投递告警（站内 + QQ 旁路），恢复再报一条。
"""

import asyncio
import logging
import time

from backend.shared.bridge_outage import (
    build_marker,
    qualifies,
    record_probe,
    write_marker,
)
from backend.shared.simulation_account_keys import resolve_db_account_user
from backend.services.live_trading.services.bridge_health_watch import (
    VERDICT_HARD,
    VERDICT_OK,
    BridgeHealthTracker,
    classify_sync_result,
    notify_bridge_event,
)
from backend.services.live_trading.services.tdx_push_service import tdx_pusher

logger = logging.getLogger(__name__)


def _track_outage(verdict: str, *, now: float | None = None) -> None:
    """把一次账户探测喂给「桥掉线窗口」；够格恢复则登记全名册补跑义务。

    与告警状态机（``BridgeHealthTracker``）分工：那边管**人**（边沿告警），这里管
    **决策轮**（恢复后补跑）。窗口写 Redis（交易库 db2），进程重启不丢掉线起点；
    任何失败只记日志——观测面不许影响账户同步本身。
    """
    try:
        from backend.services.trade_shared.redis_client import get_redis as _get_redis

        wrapper = _get_redis()
        if getattr(wrapper, "client", None) is None:
            wrapper.connect()
        client = getattr(wrapper, "client", None)
        if client is None:
            logger.warning("[TdxSync] Redis 不可用，桥掉线窗口本轮不记")
            return
        outage = record_probe(client, ok=(verdict == VERDICT_OK), now=now)
        if outage is None or not qualifies(outage):
            return
        marker = build_marker(outage)
        write_marker(client, marker)
        logger.warning(
            "[TdxSync] 桥掉线 %.0f 分钟已恢复（%s）：已登记恢复补跑义务（%s）",
            outage.down_for_s / 60.0,
            "跨重启合成" if outage.synthesized else "在线探测",
            marker.day,
        )
    except Exception as exc:  # noqa: BLE001 状态面坏掉不许影响同步循环
        logger.warning("[TdxSync] 桥掉线窗口记录失败: %s", exc)


async def run_tdx_account_sync_task(interval_seconds: int = 30):
    """定期拉取通达信桥账户并落库 PG。默认每 30 秒同步一次。"""
    if not tdx_pusher.enabled:
        logger.info("[TdxSync] TDX_BRIDGE_URL/TOKEN 未配置，通达信账户同步任务跳过")
        return

    logger.info(
        "[TdxSync] 通达信账户同步任务启动, interval=%ss, bridge=%s",
        interval_seconds,
        tdx_pusher.bridge_url,
    )
    tracker = BridgeHealthTracker()
    while True:
        now_ts = time.time()
        verdict, detail = VERDICT_HARD, ""
        try:
            result = await tdx_pusher.sync_account_to_pg(
                tenant_id="default",
                # 账户名唯一口径：规范名 10000001（老口径 00000001 曾致委托落库
                # 唯一键冲突、账户快照写到前端看不见的账户下）
                user_id=resolve_db_account_user("TDX_ACCOUNT_USER_ID"),
            )
            verdict, detail = classify_sync_result(result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            detail = f"账户通道异常: {exc}"
            logger.warning("[TdxSync] 通达信账户同步失败: %s", exc)
        _track_outage(verdict, now=now_ts)
        event = tracker.record(verdict, detail=detail, now=now_ts)
        if event is not None:
            await notify_bridge_event(
                event,
                bridge_url=tdx_pusher.bridge_url,
                interval_seconds=int(interval_seconds),
            )
        await asyncio.sleep(max(10, float(interval_seconds)))
