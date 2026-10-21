"""EOD 六卡评分 worker（T-P4-05b）——交易日 16:00 后跑一次 ``run_all``，心跳入调度注册表。

职责：
- 每 60s 轮询：交易日且到点且当日未成功 → ``scripts/eval/run_all.py`` 的 ``run_all()``
  顺序评 因子/模型/策略/账户/实盘账户/每日选股 → ``eval_scores`` 落表；
- 单卡异常在 ``run_all`` 内部隔离（不阻塞整轮）；整轮无异常才写当日完成标记
  （``eval:scores:done:{date}``，TTL 3 天）；有异常下一轮重试，但**累计尝试达到
  预算（默认 5 轮）后带错落标记**——持续性单点错误（重试治不好的那类）不再让
  整轮每分钟重跑整晚（2026-10-15 实锤：一个模型 pred 缺列烧了 411 轮）；
- 六卡之后接着跑一次**回填**（``backfill_recent``）——把最近「待回填 ∧ 前向窗口
  已闭合」的每日选股快照重跑，否则这些天会永久停在 pending †；
- 心跳写 ``scheduler_registry``（体检 C07 可见）。

环境变量：
  EVAL_SCORES_WORKER_ENABLED      默认 "1"
  EVAL_SCORES_TIME                默认 "16:00"（上海时区）
  EVAL_SCORES_CHECK_INTERVAL_SEC  默认 60
  EVAL_SCORES_MAX_ATTEMPTS        默认 5（有异常时的重试轮数上限，到时带错落标记）
  EVAL_BACKFILL_DAYS              默认 21（回填回看自然日；0 = 关闭回填）
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone

from backend.shared import eval_trigger

logger = logging.getLogger(__name__)

_DONE_KEY = "eval:scores:done:{date}"
_ATTEMPTS_KEY = "eval:scores:attempts:{date}"
_DONE_TTL_SECONDS = 3 * 24 * 3600
_MAX_ATTEMPTS_DEFAULT = 5

# 北京时间与 UTC 的固定偏移（A 股无夏令时）——与影子对照/双轨对账同口径
_CST_OFFSET = timedelta(hours=8)


def _env_bool(name: str, default: bool) -> bool:
    raw = str(os.getenv(name, "")).strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = str(os.getenv(name, "")).strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("%s=%s 非法，回落 %s", name, raw, default)
        return default


def _config() -> dict:
    return {
        "enabled": _env_bool("EVAL_SCORES_WORKER_ENABLED", True),
        "time": str(os.getenv("EVAL_SCORES_TIME", "16:00")),
        "interval": max(10, int(os.getenv("EVAL_SCORES_CHECK_INTERVAL_SEC", "60"))),
        "max_attempts": max(
            1, _env_int("EVAL_SCORES_MAX_ATTEMPTS", _MAX_ATTEMPTS_DEFAULT)
        ),
    }


def parse_eval_time(raw: str) -> tuple[int, int]:
    """解析 ``HH:MM``，非法值回落 16:00。"""
    try:
        hour, minute = str(raw).strip().split(":", 1)
        h, m = int(hour), int(minute)
        if 0 <= h < 24 and 0 <= m < 60:
            return h, m
    except (TypeError, ValueError):
        pass
    return 16, 0


def _raw_client(redis) -> object | None:
    """兼容 RedisClient 包装器（``.client``）与原生 client（同影子对照纪律）。"""
    if redis is None:
        return None
    client = getattr(redis, "client", None)
    if client is not None:
        return client
    return redis if hasattr(redis, "set") else None


def _settle_done_marker(
    client, date_str: str, summary: dict, max_attempts: int
) -> str:
    """按本轮结果决定当日完成标记，返回 ``done`` / ``done_with_errors`` / ``retry``。

    - 六卡无异常 → 直接落标记（值 ``"1"``）；
    - 有异常 → 计一次尝试；未到预算返回 ``retry``（下一轮照常重试，暂时性错误
      仍会被自愈）；达到预算 K 轮 → **带错落标记**（值为 JSON：attempts + errors
      摘要），由调用方 WARNING 并接告警面。

    预算的意义是给「持续性单点错误」封顶（重试治不好，只是每分钟重烧全量六卡）；
    期间任何一轮转好都会走成功分支清盘。
    """
    errors = int(summary.get("total_errors") or 0)
    done_key = _DONE_KEY.format(date=date_str)
    if errors == 0:
        client.set(done_key, "1", ex=_DONE_TTL_SECONDS)
        return "done"
    attempts_key = _ATTEMPTS_KEY.format(date=date_str)
    attempts = int(client.incr(attempts_key))
    client.expire(attempts_key, _DONE_TTL_SECONDS)
    if attempts < max(1, int(max_attempts)):
        return "retry"
    client.set(
        done_key,
        json.dumps(
            {
                "done": "with_errors",
                "attempts": attempts,
                "errors": [str(e) for e in (summary.get("errors") or [])[:10]],
            },
            ensure_ascii=False,
        ),
        ex=_DONE_TTL_SECONDS,
    )
    return "done_with_errors"


async def run_eval_scores_once(
    *, save: bool = True, trade_date: str | None = None
) -> dict:
    """执行一次六卡评分（手动重跑与 worker 共用同一入口），随后回填近期 pending。

    `trade_date` 只影响每日选股卡（按指定交易日评），其余卡仍是当日快照；
    手动重跑传历史日期时不会把别的卡回退到过去。
    """
    from backend.scripts.eval.run_all import run_all

    summary = await run_all(
        trade_date,
        save=save,
        factor_top=_env_int("EVAL_FACTOR_TOP", 20),
        strategy_limit=_env_int("EVAL_STRATEGY_LIMIT", 20),
    )
    logger.info(
        "[EvalScores] 六卡完成：落分=%s 异常=%s 耗时=%ss",
        summary.get("total_scored"),
        summary.get("total_errors"),
        summary.get("elapsed_sec"),
    )
    if save:
        summary["backfill"] = await run_backfill_once()
    return summary


async def run_backfill_once() -> dict:
    """回填近期「待回填 ∧ 前向窗口已闭合」的每日选股快照（设计 §1.4）。

    单独成函数：手动重跑只补历史时不必再跑一遍六卡。失败只记 warning，**不阻塞
    当日完成标记**——回填自带 21 天回看窗口，明天的评分轮会把漏掉的一起补上；
    反过来（回填失败就不写标记）会让整轮每 60s 重跑一次六卡，代价远大于收益。
    """
    days = _env_int("EVAL_BACKFILL_DAYS", 21)
    if days <= 0:
        return {"disabled": True, "note": "EVAL_BACKFILL_DAYS=0：回填已关闭"}
    try:
        from backend.scripts.eval.backfill_recent import backfill_recent

        result = await backfill_recent(days=days)
    except Exception as exc:  # noqa: BLE001 - 回填失败不拖垮评分轮次
        logger.error("[EvalScores] 回填异常: %s", exc, exc_info=True)
        return {
            "errors": [f"backfill: {type(exc).__name__}: {exc}"],
            "note": "回填异常",
        }
    logger.info("[EvalScores] 回填：%s", result.get("note"))
    return result


async def handle_manual_run(client, request: dict) -> dict | None:
    """消费一个手动重评请求：状态机（running → done/error）+ 跑批。

    返回六卡 summary（失败 → None）。**不写当日 done 标记**：手动跑批可能发生在
    16:00 前（数据未收盘），把它当定时跑会吞掉当天真正的 EOD。是否补写标记由
    worker 循环按「已过定时点 + 六卡无异常」决定。
    """
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        eval_trigger.write_run_status(
            client,
            eval_trigger.build_run_status(
                "running",
                request=request,
                started_at=started_at,
                note="trade worker 已接手，正在重算六卡",
            ),
        )
    except Exception as exc:  # noqa: BLE001 - 状态写失败不阻止跑批
        logger.warning("[EvalScores] 手动重评状态写入失败（running）: %s", exc)

    logger.info(
        "[EvalScores] 手动重评开始：request=%s date=%s",
        request.get("request_id"),
        request.get("date"),
    )
    try:
        summary = await run_eval_scores_once(
            save=True, trade_date=request.get("date")
        )
    except Exception as exc:  # noqa: BLE001 - 手动跑批失败落状态，不杀 worker
        logger.error("[EvalScores] 手动重评失败: %s", exc, exc_info=True)
        try:
            eval_trigger.write_run_status(
                client,
                eval_trigger.build_run_status(
                    "error",
                    request=request,
                    error=f"{type(exc).__name__}: {exc}",
                    started_at=started_at,
                ),
            )
        except Exception:  # noqa: BLE001
            pass
        return None

    backfill_errors = (summary.get("backfill") or {}).get("errors") or []
    note = "六卡评分完成" + (
        f"；回填 {len(backfill_errors)} 条异常（不阻塞六卡）" if backfill_errors else ""
    )
    try:
        eval_trigger.write_run_status(
            client,
            eval_trigger.build_run_status(
                "done",
                request=request,
                summary=summary,
                started_at=started_at,
                note=note,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[EvalScores] 手动重评状态写入失败（done）: %s", exc)
    logger.info(
        "[EvalScores] 手动重评完成：落分=%s 异常=%s 耗时=%ss",
        summary.get("total_scored"),
        summary.get("total_errors"),
        summary.get("elapsed_sec"),
    )
    return summary


def after_scheduled_time(now: datetime, hour: int, minute: int) -> bool:
    """当前是否已过当日定时点（纯函数，便于单测）。"""
    return (now.hour, now.minute) >= (int(hour), int(minute))


async def run_eval_scores_worker() -> None:
    """常驻循环：每交易日到点后跑一次，成功写 Redis 当日标记；心跳入调度注册表。"""
    cfg = _config()
    if not cfg["enabled"]:
        logger.info("[EvalScores] 评分任务关闭（EVAL_SCORES_WORKER_ENABLED=0）")
        return

    from backend.services.live_trading.services.trading_session import TZ
    from backend.services.trade_shared.redis_client import get_redis as get_trade_redis
    from backend.shared.scheduler_registry import heartbeat as _sched_heartbeat

    target_h, target_m = parse_eval_time(cfg["time"])
    logger.info(
        "[EvalScores] 评分任务启动：每交易日 %02d:%02d 执行", target_h, target_m
    )
    while True:
        try:
            _sched_heartbeat("eval_scores")
        except Exception:  # noqa: BLE001
            pass
        try:
            now = datetime.now(TZ)
            date_str = now.strftime("%Y%m%d")
            client = _raw_client(get_trade_redis())
            if client is None:
                # 没 Redis 既不能加锁也不能读写请求/完成标记；硬跑会在每轮循环里
                # 重复重算六卡（旧实现的隐式行为）。明确跳过，下一轮再试。
                logger.warning("[EvalScores] trade Redis 不可用：本轮跳过")
            else:
                lock_token = uuid.uuid4().hex
                if not eval_trigger.acquire_run_lock(client, lock_token):
                    logger.info("[EvalScores] 另一评分轮次持锁中，本轮跳过")
                else:
                    try:
                        manual = None
                        try:
                            manual = eval_trigger.consume_run_request(client)
                        except Exception as exc:  # noqa: BLE001 - 坏请求不杀循环
                            logger.warning("[EvalScores] 读取手动重评请求失败: %s", exc)

                        if manual:
                            summary = await handle_manual_run(client, manual)
                            # 手动跑批只有「已过定时点 + 六卡无异常」才补当日完成
                            # 标记；16:00 前的手动跑批绝不能吞掉真正的 EOD。
                            if (
                                summary is not None
                                and int(summary.get("total_errors") or 0) == 0
                                and after_scheduled_time(now, target_h, target_m)
                            ):
                                try:
                                    _settle_done_marker(
                                        client,
                                        date_str,
                                        summary,
                                        cfg["max_attempts"],
                                    )
                                except Exception:  # noqa: BLE001
                                    pass
                        else:
                            already = False
                            try:
                                already = bool(
                                    client.exists(_DONE_KEY.format(date=date_str))
                                )
                            except Exception:  # noqa: BLE001
                                already = False
                            if (
                                now.weekday() < 5
                                and after_scheduled_time(now, target_h, target_m)
                                and not already
                            ):
                                summary = await run_eval_scores_once(save=True)
                                # 完成标记只看**六卡**异常：回填失败不该让整轮每分钟
                                # 重跑一次六卡。回填自带 21 天回看窗口，明天那轮会补上。
                                backfill_errors = (
                                    summary.get("backfill") or {}
                                ).get("errors") or []
                                if backfill_errors:
                                    logger.warning(
                                        "[EvalScores] 回填未全部成功：%s",
                                        backfill_errors,
                                    )
                                try:
                                    outcome = _settle_done_marker(
                                        client,
                                        date_str,
                                        summary,
                                        cfg["max_attempts"],
                                    )
                                    if outcome == "done_with_errors":
                                        logger.warning(
                                            "[EvalScores] 连续 %s 轮存在异常，已达尝试"
                                            "预算：带错落当日完成标记，不再重跑"
                                            "（errors=%s）",
                                            cfg["max_attempts"],
                                            summary.get("errors"),
                                        )
                                except Exception:  # noqa: BLE001
                                    pass
                    finally:
                        try:
                            eval_trigger.release_run_lock(client, lock_token)
                        except Exception:  # noqa: BLE001
                            pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("[EvalScores] 评分任务异常: %s", exc, exc_info=True)
        await asyncio.sleep(cfg["interval"])
