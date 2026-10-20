"""集合竞价取数（实盘持仓 ∪ 当日候选池）→ Redis + 归档（trade 常驻任务）。

数据源：TdxAiData SDK v1.2.2 新增的批量集合竞价接口 ``tqs.get_call_auction_batch``
（worker 请求通道 op ``get_call_auction_batch``，2026-10-15 接入；官方接口文档
见 SDK 包 ``docs/tdxaidata使用文档.md`` §8.2）。每只返回当日**逐笔虚拟撮合序列**：
``Time`` 撮合时间 / ``Price`` 虚拟匹配价 / ``Volume`` 虚拟匹配量 / ``LeaveQty``
未匹配量（可为负=卖压）。``type=1`` 仅早盘（09:15:00–09:24:5x）；``type=0``
早盘+尾盘（含 14:57–15:00 收盘竞价）。

采样档（北京时间，见 :data:`SAMPLE_TIMES`）：每档给 :data:`SAMPLE_GRACE_S` 秒
宽限补采（进程晚起/单次失败在同一档内可重试，最多 :data:`MAX_ATTEMPTS_PER_SAMPLE`
次防配额风暴）。**每次调用返回的都是从 09:15:00 起的全序列**——后档天然包含
前档，最坏情况（进程 09:25 后才起来）只错失「竞价演化」的即时视图，不丢数据本体。

**收盘档例外（2026-10-15 实测）**：``type=0`` 的尾盘段（14:57–15:00）并非收盘
当刻即发布——15:00:05 拿到的仍只是早盘序列（与 09:25:05 档逐字节相同）。收盘档
以 :func:`has_tail_segment` 内容门判定：尾盘段未出现前不落盘、不判完成，在
:data:`SAMPLE_GRACE_BY_SAMPLE` 宽限内按 :data:`SAMPLE_RETRY_INTERVAL_S` 节流
重试；早盘本体已由 09:25:05 档存档，重复落盘只会添噪声。

**日期盲防御**（:func:`guard_stale_rows`，2026-10-15 08:18 盘前实测）：竞价开启
前 SDK 返回**上一交易日**的完整序列且记录无日期字段；守卫按「末帧时间晚于
当前时刻+裕量」的时间不可能性判陈旧并**显式丢弃**（证据进 summary）。
残差：09:25:05 档对**停牌股**若 SDK 仍给昨日序列，时间上不可判别（昨日末帧
09:24:5x 与今日重合）——观测面 = 该票 09:20:30 档是否被守卫判陈旧。

名单口径（2026-10-15 用户定）：**实盘持仓 ∪ 当日候选池**
- 实盘持仓：``shared.real_positions.load_real_positions``（多券商并集，唯一实现），
  租户 ``QM_HOT_SET_REAL_TENANT``（默认 default）、用户 ``QM_HOT_SET_REAL_USER``
  （默认 10000001，与热集/账户后台同一解析，见 ``simulation_account_keys``）。
- 当日候选池：``shared.decision_context_source.load_pool_doc``（决策轮同源）。

落点（每档一次）：
- Redis（交易库 db2）：``qm:call_auction:{YYYYMMDD}`` Hash，field=前缀码
  （平台 Redis 键约定），value=该票本档摘要 JSON；``qm:call_auction:status``
  为该任务心跳/体检（档期、名单数、错误）。
- 归档：``{QM_CALL_AUCTION_ARCHIVE_DIR:-/data/call_auction}/date=YYYYMMDD/
  sample-HHMMSS.json``（逐笔全量 + 摘要，原子写；与 L0.5 落盘同款 date= 分区）。

开关：``QM_CALL_AUCTION_WATCH_ENABLED``（默认开）。任务由 ``trade/main.py``
lifespan 起停；手工单跑：``python -m backend.services.live_trading.services.
call_auction_watch --once [--sample 09:25:05]``。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from collections.abc import Mapping

from backend.services.live_trading.services.trading_session import TZ
from backend.services.trade.services.daily_pnl_report_task import (
    _env_bool,
    _is_trading_day,
    _redis_client,
)

logger = logging.getLogger(__name__)

#: 采样档（北京时间）：早盘三段（15:30 开场 / 20:30 撤单闸门关闭后 / 25:05 定稿）
#: + 收盘档（15:00:05 拿尾盘 14:57–15:00 竞价，type=0 一并含早盘）。收盘档注意
#: **尾盘段发布滞后**（2026-10-15 实测收盘当刻仍只回早盘）：以
#: :func:`has_tail_segment` 内容门 + :data:`SAMPLE_GRACE_BY_SAMPLE` 宽限重试取全。
SAMPLE_TIMES: tuple[str, ...] = ("09:15:30", "09:20:30", "09:25:05", "15:00:05")

#: 每档宽限（秒）：到点后窗口内仍可触发（进程晚起/单次失败重试）。
SAMPLE_GRACE_S = 120

#: 单档宽限覆盖（秒）：收盘档尾盘段发布滞后（2026-10-15 实测 15:00:05 当刻
#: SDK ``type=0`` 仍只回早盘段，见 :func:`has_tail_segment`）——宽限内以
#: :data:`SAMPLE_RETRY_INTERVAL_S` 节流重试，直至尾盘段出现或窗口耗尽。
SAMPLE_GRACE_BY_SAMPLE: dict[str, int] = {"15:00:05": 2100}


def _grace_s(sample: str) -> int:
    """档宽限：默认 :data:`SAMPLE_GRACE_S`，收盘档按覆盖表。"""
    return SAMPLE_GRACE_BY_SAMPLE.get(sample, SAMPLE_GRACE_S)


#: 单档最多尝试次数（fetch 失败后重试上限，防配额风暴）。
MAX_ATTEMPTS_PER_SAMPLE = 3

#: 单档尝试上限覆盖：收盘档要覆盖整个宽限窗（2100s/60s≈35 次，留余量）。
MAX_ATTEMPTS_BY_SAMPLE: dict[str, int] = {"15:00:05": 40}


def _attempts_cap(sample: str) -> int:
    """档尝试上限：默认 :data:`MAX_ATTEMPTS_PER_SAMPLE`，收盘档按覆盖表。"""
    return MAX_ATTEMPTS_BY_SAMPLE.get(sample, MAX_ATTEMPTS_PER_SAMPLE)


#: 重试节流（秒）：0=每轮询轮即试（默认，5s 节奏）；收盘档 60s 节流
#: （等尾盘段发布，不拿 5s 轮询空转白烧 SDK 配额）。
SAMPLE_RETRY_INTERVAL_S: dict[str, int] = {"15:00:05": 60}

#: 日期盲防御裕量（秒）：竞价前 SDK 返回**上一交易日**完整序列且记录无日期字段；
#: 末帧时间晚于「当前时刻+本裕量」的记录在时间上不可能是今日实时序列 → 判陈旧。
FUTURE_TIME_SLACK_S = 30

#: 采样档 → SDK type（0=早盘+尾盘，1=仅早盘）。收盘档必须 0 才含尾盘竞价。
SAMPLE_TYPES: dict[str, int] = {
    "09:15:30": 1,
    "09:20:30": 1,
    "09:25:05": 1,
    "15:00:05": 0,
}

#: 单次请求的代码数上限（保守取订阅侧同值 100；超限拆批，批次各计一次请求预算）。
BATCH_MAX = 100

#: 轮询间隔（秒）：档期粒度是秒级，5s 足够且配额无感。
DEFAULT_INTERVAL_S = 5.0

#: Redis 落点（交易库 db2）。
BOARD_KEY_TPL = "qm:call_auction:{day}"
STATUS_KEY = "qm:call_auction:status"
BOARD_TTL_S = 7 * 86400
STATUS_TTL_S = 2 * 86400

#: 持仓行里股票名的候选键（real_positions 的 payload 两代字段名都容忍）。
_NAME_KEYS = ("stock_name", "stockName", "name", "名称")


def archive_base() -> Path:
    """归档根目录（env 可覆写；默认与 L0.5 落盘同级的 /data 分区）。"""
    env = str(os.getenv("QM_CALL_AUCTION_ARCHIVE_DIR") or "").strip()
    return Path(env) if env else Path("/data/call_auction")


# ---------- 纯函数 ----------


def _hms_to_seconds(text: str) -> int | None:
    """``HH:MM:SS`` / ``HHMMSS`` → 当日秒数；解析不动返回 None。"""
    raw = str(text or "").strip()
    digits = raw.replace(":", "")
    if len(digits) != 6 or not digits.isdigit():
        return None
    h, m, s = int(digits[0:2]), int(digits[2:4]), int(digits[4:6])
    if h > 23 or m > 59 or s > 59:
        return None
    return h * 3600 + m * 60 + s


def due_sample(now: datetime, done: set[str]) -> str | None:
    """当前时刻应采的档（对点后宽限内、且未完成）。

    ``now`` 必须是市场本地钟（北京时间）的 aware datetime；``done`` 为当日
    已完成档集合。宽限默认 :data:`SAMPLE_GRACE_S` 秒，收盘档按
    :data:`SAMPLE_GRACE_BY_SAMPLE` 覆盖。返回第一个到期的档名，无档返回 None。
    """
    now_s = now.hour * 3600 + now.minute * 60 + now.second
    for sample in SAMPLE_TIMES:
        if sample in done:
            continue
        at = _hms_to_seconds(sample)
        if at is None:
            continue
        if 0 <= now_s - at < _grace_s(sample):
            return sample
    return None


def _fmt_time(value: Any) -> str:
    """SDK 时间值 → ``HH:MM:SS``（兼容 ``091500`` 与 ``09:15:00`` 两种写法）。"""
    raw = str(value or "").strip()
    digits = raw.replace(":", "")
    if len(digits) == 6 and digits.isdigit():
        return f"{digits[0:2]}:{digits[2:4]}:{digits[4:6]}"
    return raw


def _to_float(value: Any) -> float | None:
    try:
        out = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return out


def _to_int(value: Any) -> int | None:
    f = _to_float(value)
    return None if f is None else int(f)


def normalize_record(record: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """SDK 单只记录（``Time/Price/Volume/LeaveQty`` 四列嵌套列表）→ 逐笔行。

    四列按下标对齐（SDK 契约）；缺失列按空处理，长短不一以 Time 为准截断。
    任何一列缺失的单元格记 None（**不填 0**——0 有真实语义，缺失必须可区分）。
    """
    if not isinstance(record, Mapping):
        return []
    times = record.get("Time") or []
    prices = record.get("Price") or []
    volumes = record.get("Volume") or []
    leaves = record.get("LeaveQty") or []
    if not isinstance(times, list):
        return []
    rows: list[dict[str, Any]] = []
    for i in range(len(times)):
        rows.append(
            {
                "time": _fmt_time(times[i]),
                "price": _to_float(prices[i]) if i < len(prices) else None,
                "volume": _to_int(volumes[i]) if i < len(volumes) else None,
                "leave_qty": _to_int(leaves[i]) if i < len(leaves) else None,
            }
        )
    return rows


def guard_stale_rows(
    rows: list[dict[str, Any]], now: datetime
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """日期盲防御：丢弃末帧时间「晚于当前时刻+裕量」的行（=上一交易日残留）。

    竞价开启前（及停牌等无今日序列场景）SDK 会把上一交易日的完整序列当作
    结果返回，记录里没有日期字段——唯一的判别面就是时间先后不可能性：
    今日实时序列的末帧不可能晚于现在。返回 ``(保留行, 丢弃证据)``，
    证据显式进 summary（**绝不静默丢**）。
    """
    if not rows:
        return rows, None
    now_s = now.hour * 3600 + now.minute * 60 + now.second
    times = [t for t in (_hms_to_seconds(r.get("time")) for r in rows) if t is not None]
    if times and max(times) > now_s + FUTURE_TIME_SLACK_S:
        return [], {
            "stale_dropped_rows": len(rows),
            "stale_last_time": rows[-1].get("time"),
        }
    return rows, None


#: 尾盘段起始时刻（收盘集合竞价 14:57–15:00；HH:MM:SS 字符串字典序可比较）。
TAIL_SEGMENT_FROM = "14:57:00"


def has_tail_segment(per_symbol: Mapping[str, Mapping[str, Any]]) -> bool:
    """内容门：任一标的含尾盘段行即视尾盘数据已发布（停牌股无尾盘行属正常）。

    2026-10-15 实测：15:00:05 当刻（及收盘后数分钟）SDK ``type=0`` 仍只回早盘段，
    尾盘段发布滞后——收盘档据此判「未完成」在宽限内重试，绝不以早盘冒充全量
    （早盘本体已由 09:25:05 档存档，重复落盘只添噪声）。
    """
    for item in per_symbol.values():
        for row in item.get("rows") or []:
            if str(row.get("time") or "") >= TAIL_SEGMENT_FROM:
                return True
    return False


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """逐笔行 → 摘要（末帧即该档定稿参考价/量：早盘档=09:24:5x；收盘档=14:59:5x
    尾盘定稿参考价/量；不做累计口径推断）。"""
    if not rows:
        return {"rows": 0}
    first, last = rows[0], rows[-1]
    prices = [r["price"] for r in rows if r["price"] is not None]
    return {
        "rows": len(rows),
        "first_time": first["time"],
        "last_time": last["time"],
        "first_price": first["price"],
        "last_price": last["price"],
        "last_volume": last["volume"],
        "last_leave_qty": last["leave_qty"],
        "price_min": min(prices) if prices else None,
        "price_max": max(prices) if prices else None,
    }


# ---------- 取数 ----------


async def collect_targets(day: str) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """实盘持仓 ∪ 当日候选池 → ``({后缀码: {symbol, name, roles}}, errors)``。

    两源失败互不阻断（errors 如实报出）；返回码一律后缀式（SDK/池原生口径）。
    """
    from backend.shared.simulation_account_keys import resolve_db_account_user
    from backend.shared.stock_utils import StockCodeUtil

    targets: dict[str, dict[str, Any]] = {}
    errors: list[str] = []

    def _add(code: str, name: str, role: str) -> None:
        suffix = StockCodeUtil.to_suffix(code)
        if not suffix:
            return
        row = targets.setdefault(suffix, {"symbol": suffix, "name": "", "roles": []})
        if name and not row["name"]:
            row["name"] = name
        if role not in row["roles"]:
            row["roles"].append(role)

    # 实盘持仓（多券商并集；停更源由 real_positions 口径剔除）
    try:
        from backend.shared.real_positions import load_real_positions

        tenant = (os.getenv("QM_HOT_SET_REAL_TENANT", "default") or "default").strip()
        user = resolve_db_account_user("QM_HOT_SET_REAL_USER")
        positions, _meta = await load_real_positions(tenant, user)
        for sym, prow in (positions or {}).items():
            name = ""
            if isinstance(prow, Mapping):
                for key in _NAME_KEYS:
                    if prow.get(key):
                        name = str(prow.get(key))
                        break
            _add(str(sym), name, "holding")
    except Exception as exc:  # noqa: BLE001 - 实盘源失败不阻断池
        errors.append(f"实盘持仓取数失败: {exc}")

    # 当日候选池（决策轮同源；文件不在=当天还没产出，不是错误）
    try:
        from backend.shared.decision_context_source import load_pool_doc

        doc = load_pool_doc(day)
        if doc is not None:
            for row in doc.rows:
                _add(str(row.code), str(getattr(row, "name", "") or ""), "candidate")
    except Exception as exc:  # noqa: BLE001 - 池读失败不阻断持仓
        errors.append(f"候选池读取失败: {exc}")

    # 名字兜底（持仓 payload 可能无名字段；未收录返回空串，展示方回退代码）
    missing = [k for k, v in targets.items() if not v["name"]]
    if missing:
        try:
            from backend.shared.stock_name_mapper import resolve_name

            for suffix in missing:
                targets[suffix]["name"] = resolve_name(suffix) or ""
        except Exception:  # noqa: BLE001 - 名字只是妆点，失败不记错
            pass

    return targets, errors


async def fetch_records(symbols: list[str], auction_type: int) -> dict[str, Any]:
    """经 worker 请求通道批量取竞价记录；超 :data:`BATCH_MAX` 拆批合并。"""
    from backend.shared.tdx_aidata.client import default_cluster

    client = default_cluster().primary
    out: dict[str, Any] = {}
    for i in range(0, len(symbols), BATCH_MAX):
        chunk = symbols[i : i + BATCH_MAX]
        data = await client.get_call_auction_batch(
            chunk, type=auction_type, timeout=60.0
        )
        if isinstance(data, dict):
            for key, value in data.items():
                out[str(key)] = value
    return out


def _record_for(
    records: Mapping[str, Any], suffix: str, prefix_index: Mapping[str, Any]
) -> Mapping[str, Any] | None:
    """按后缀直取；SDK 若回前缀/异形键，用归一索引兜底。"""
    raw = records.get(suffix)
    if raw is None:
        raw = prefix_index.get(suffix)
    return raw if isinstance(raw, Mapping) else None


# ---------- 落点 ----------


def write_outputs(
    *,
    day: str,
    sample: str,
    auction_type: int,
    per_symbol: dict[str, dict[str, Any]],
    errors: list[str],
    now: datetime,
) -> dict[str, Any]:
    """归档 JSON（原子写）+ Redis（摘要 Hash + 心跳）；返回状态 payload。"""
    from backend.shared.stock_utils import StockCodeUtil

    generated_at = now.isoformat()
    payload = {
        "generated_at": generated_at,
        "sample": sample,
        "auction_type": auction_type,
        "errors": errors,
        "symbols": per_symbol,
    }

    # 归档：date= 分区 + 原子写（与 L0.5 落盘同款目录纪律）
    archive_written = ""
    try:
        day_dir = archive_base() / f"date={day}"
        day_dir.mkdir(parents=True, exist_ok=True)
        target = day_dir / f"sample-{sample.replace(':', '')}.json"
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(target)
        archive_written = str(target)
    except Exception as exc:  # noqa: BLE001 - 归档失败不阻断 Redis 落点
        errors.append(f"归档写入失败: {exc}")
        logger.warning("[CallAuction] 归档写入失败: %s", exc)

    # Redis（交易库）：摘要 Hash + 心跳
    rows_total = sum(int(v["summary"].get("rows") or 0) for v in per_symbol.values())
    status = {
        "sample": sample,
        "auction_type": auction_type,
        "at": generated_at,
        "day": day,
        "targets": len(per_symbol),
        "holdings": sum(1 for v in per_symbol.values() if "holding" in v["roles"]),
        "candidates": sum(1 for v in per_symbol.values() if "candidate" in v["roles"]),
        "rows_total": rows_total,
        "errors": errors,
        "archive": archive_written,
    }
    try:
        from backend.services.trade_shared.deps import get_redis

        client = _redis_client(get_redis())
        if client is None:
            raise RuntimeError("交易 Redis 不可用")
        board_key = BOARD_KEY_TPL.format(day=day)
        pipe = client.pipeline(transaction=False)
        for suffix, item in per_symbol.items():
            field = StockCodeUtil.to_prefix(suffix)
            pipe.hset(
                board_key,
                field,
                json.dumps(
                    {
                        "name": item["name"],
                        "roles": item["roles"],
                        "summary": item["summary"],
                        "sample": sample,
                        "at": generated_at,
                    },
                    ensure_ascii=False,
                ),
            )
        pipe.expire(board_key, BOARD_TTL_S)
        pipe.set(STATUS_KEY, json.dumps(status, ensure_ascii=False), ex=STATUS_TTL_S)
        pipe.execute()
    except Exception as exc:  # noqa: BLE001 - Redis 失败如实进状态（归档仍在）
        status["redis_error"] = str(exc)
        logger.warning("[CallAuction] Redis 写入失败: %s", exc)

    return status


# ---------- 单档执行 ----------


def write_failure_status(
    *, day: str, sample: str, auction_type: int, error: str, now: datetime
) -> dict[str, Any]:
    """取数失败也写状态键：读侧不能继续拿上一次成功档当「当前状态」。

    2026-10-20 实测：Token 配额耗竭后 09:15/09:20/09:25 三档全失败，任务按
    重试上限放弃；状态键仍停留在 10-16 的成功档，``/call-auction/status``
    看起来一切正常。失败必须覆盖心跳（``failed=true`` + 错误原文）。
    """
    status = {
        "sample": sample,
        "auction_type": auction_type,
        "at": now.isoformat(),
        "day": day,
        "targets": None,
        "holdings": None,
        "candidates": None,
        "rows_total": 0,
        "errors": [str(error)],
        "archive": "",
        "failed": True,
    }
    try:
        from backend.services.trade_shared.deps import get_redis

        client = _redis_client(get_redis())
        if client is None:
            raise RuntimeError("交易 Redis 不可用")
        client.set(STATUS_KEY, json.dumps(status, ensure_ascii=False), ex=STATUS_TTL_S)
    except Exception as exc:  # noqa: BLE001 - 状态写入失败只告警
        logger.warning("[CallAuction] 失败状态写入失败: %s", exc)
    return status


async def run_sample(now: datetime, sample: str) -> bool:
    """执行一个采样档；返回 True=本档已完成（无论数据空否），False=可重试失败。"""
    day = now.strftime("%Y%m%d")
    auction_type = SAMPLE_TYPES.get(sample, 1)
    targets, errors = await collect_targets(day)
    if not targets:
        status = write_outputs(
            day=day,
            sample=sample,
            auction_type=auction_type,
            per_symbol={},
            errors=errors,
            now=now,
        )
        logger.warning("[CallAuction] %s 名单为空，本档作罢: %s", sample, status)
        return True

    symbols = sorted(targets)
    try:
        records = await fetch_records(symbols, auction_type)
    except Exception as exc:  # noqa: BLE001 - 交回循环按宽限重试
        logger.warning("[CallAuction] %s 取数失败（将在宽限内重试）: %s", sample, exc)
        write_failure_status(
            day=day,
            sample=sample,
            auction_type=auction_type,
            error=f"{type(exc).__name__}: {exc}",
            now=now,
        )
        return False

    from backend.shared.stock_utils import StockCodeUtil

    index = {StockCodeUtil.to_suffix(k): v for k, v in records.items()}
    per_symbol: dict[str, dict[str, Any]] = {}
    for suffix, target in targets.items():
        rows = normalize_record(_record_for(records, suffix, index))
        rows, stale = guard_stale_rows(rows, now)
        summary = summarize(rows)
        if stale:
            summary = {"rows": 0, **stale}
        per_symbol[suffix] = {
            "name": target["name"],
            "roles": target["roles"],
            "summary": summary,
            "rows": rows,
        }

    # 内容门（收盘档）：type=0 承诺早盘+尾盘；尾盘段未发布前不落盘、不判完成，
    # 交回循环按 :data:`SAMPLE_RETRY_INTERVAL_S` 节流重试（2026-10-15 实测：
    # 15:00:05 当刻 SDK 仍只回早盘段，尾盘段滞后发布）。
    if auction_type == 0 and not has_tail_segment(per_symbol):
        logger.info(
            "[CallAuction] %s 尾盘段未发布（SDK type=0 暂只回早盘），宽限内重试",
            sample,
        )
        return False

    status = write_outputs(
        day=day,
        sample=sample,
        auction_type=auction_type,
        per_symbol=per_symbol,
        errors=errors,
        now=now,
    )
    logger.info(
        "[CallAuction] %s 完成: targets=%s rows=%s archive=%s",
        sample,
        status["targets"],
        status["rows_total"],
        status.get("archive") or "—",
    )
    return True


# ---------- 常驻任务 ----------


def _poll_interval_s() -> float:
    try:
        raw = os.getenv("QM_CALL_AUCTION_WATCH_INTERVAL_S")
        return max(2.0, float(raw)) if raw else DEFAULT_INTERVAL_S
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_S


async def run_call_auction_watch_task() -> None:
    """常驻循环：到点采样（宽限补采、单档限次重试）；非交易日秒过。"""
    if not _env_bool("QM_CALL_AUCTION_WATCH_ENABLED", True):
        logger.info("[CallAuction] 任务关闭（QM_CALL_AUCTION_WATCH_ENABLED=0）")
        return
    interval = _poll_interval_s()
    logger.info(
        "[CallAuction] 任务启动 interval=%.1fs samples=%s", interval, SAMPLE_TIMES
    )
    done_day = ""
    done: set[str] = set()
    attempts: dict[str, int] = {}
    next_try: dict[str, datetime] = {}
    while True:
        try:
            now = datetime.now(TZ)
            day = now.strftime("%Y%m%d")
            if day != done_day:
                done_day, done, attempts, next_try = day, set(), {}, {}
            sample = due_sample(now, done)
            if sample is not None and now < next_try.get(sample, now):
                sample = None  # 节流窗内（收盘档等尾盘段发布，别 5s 空转）
            if sample is not None:
                attempts[sample] = attempts.get(sample, 0) + 1
                if await _is_trading_day(now.date()):
                    if await run_sample(now, sample):
                        done.add(sample)
                    elif attempts[sample] >= _attempts_cap(sample):
                        logger.warning(
                            "[CallAuction] %s 重试 %s 次仍失败，本档放弃",
                            sample,
                            attempts[sample],
                        )
                        done.add(sample)
                    else:
                        wait_s = SAMPLE_RETRY_INTERVAL_S.get(sample, 0)
                        if wait_s > 0:
                            next_try[sample] = now + timedelta(seconds=wait_s)
                else:
                    done.add(sample)  # 非交易日：本档作罢
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 常驻循环不因单圈异常退出
            logger.warning("[CallAuction] 循环异常: %s", exc, exc_info=True)
        await asyncio.sleep(interval)


# ---------- 运维手跑 ----------


async def _manual_once(sample: str | None) -> None:
    now = datetime.now(TZ)
    chosen = str(sample or "").strip() or now.strftime("%H:%M:%S")
    ok = await run_sample(now, chosen)
    print(json.dumps({"sample": chosen, "ok": ok}, ensure_ascii=False))


if __name__ == "__main__":  # pragma: no cover - 运维入口
    import argparse

    parser = argparse.ArgumentParser(description="集合竞价取数（手工单跑）")
    parser.add_argument("--once", action="store_true", help="立即执行一档")
    parser.add_argument("--sample", default="", help="档名（默认当前时刻）")
    args = parser.parse_args()
    if args.once:
        asyncio.run(_manual_once(args.sample))
