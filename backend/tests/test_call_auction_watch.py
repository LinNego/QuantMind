"""集合竞价取数任务测试（2026-10-15 接入 SDK get_call_auction_batch 通道）。

覆盖：
1. U：due_sample 档期门（对点+宽限、已完成跳过、窗外不触发、前一档错过不误触）；
2. U：normalize_record 时间两种格式 / 缺失单元格记 None / 非 mapping 空；
3. U：summarize 空行与非空行口径（末帧价/量、极值）；
4. U：collect_targets 持仓∪候选合并（roles 并集、后缀归一、名称兜底）、
   单源失败不阻断另一源（errors 如实记录）；
5. U：fetch_records 超 BATCH_MAX 拆批合并；
6. U：write_outputs 归档原子写 + Redis 前缀码字段 + 状态键（fake redis）；
7. I：run_sample 端到端（fake 名单/client/redis + tmp 归档目录）。
"""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

import pytest

MOD = "backend.services.live_trading.services.call_auction_watch"


def _tz_now(hms: str) -> datetime:
    from backend.services.live_trading.services.trading_session import TZ

    h, m, s = (int(x) for x in hms.split(":"))
    return datetime(2026, 10, 15, h, m, s, tzinfo=TZ)


# ── 1. 档期门 ───────────────────────────────────────────────────────


@pytest.mark.unit
def test_due_sample_window_and_done():
    from backend.services.live_trading.services.call_auction_watch import (
        SAMPLE_GRACE_S,
        due_sample,
    )

    # 对点即触发
    assert due_sample(_tz_now("09:15:30"), set()) == "09:15:30"
    # 宽限内仍触发（进程晚起）
    assert due_sample(_tz_now("09:16:29"), set()) == "09:15:30"
    # 恰好超出宽限不再触发（下一档未到）
    late = 30 + SAMPLE_GRACE_S
    assert (
        due_sample(_tz_now(f"09:{15 + late // 60:02d}:{late % 60:02d}"), set()) is None
    )
    # 已完成跳过，落到已到期的下一档
    assert due_sample(_tz_now("09:20:31"), {"09:15:30", "09:20:30"}) is None
    assert (
        due_sample(_tz_now("15:00:10"), {"09:15:30", "09:20:30", "09:25:05"})
        == "15:00:05"
    )
    # 窗外（开盘前/两档之间）
    assert due_sample(_tz_now("09:00:00"), set()) is None
    assert due_sample(_tz_now("09:18:00"), set()) is None


@pytest.mark.unit
def test_due_sample_closing_slot_extended_grace():
    """收盘档宽限显著长于默认 120s：尾盘段发布滞后，宽限内持续重试。"""
    from backend.services.live_trading.services.call_auction_watch import due_sample

    done_morning = {"09:15:30", "09:20:30", "09:25:05"}
    assert due_sample(_tz_now("15:10:00"), done_morning) == "15:00:05"
    assert due_sample(_tz_now("15:30:00"), done_morning) == "15:00:05"
    # 傍晚远超宽限 → 不再触发
    assert due_sample(_tz_now("16:30:00"), done_morning) is None
    # 早盘档宽限不受影响（默认 120s）
    assert due_sample(_tz_now("09:17:59"), {"09:20:30", "09:25:05", "15:00:05"}) is None


@pytest.mark.unit
def test_closing_retry_tuning_tables():
    """收盘档重试节流/尝试上限与宽限配套（防高频空转白烧配额）。"""
    from backend.services.live_trading.services.call_auction_watch import (
        MAX_ATTEMPTS_PER_SAMPLE,
        SAMPLE_GRACE_BY_SAMPLE,
        SAMPLE_GRACE_S,
        SAMPLE_RETRY_INTERVAL_S,
        _attempts_cap,
        _grace_s,
    )

    assert _grace_s("15:00:05") == SAMPLE_GRACE_BY_SAMPLE["15:00:05"] > 120
    assert _grace_s("09:15:30") == SAMPLE_GRACE_S == 120  # 默认不变
    throttled = SAMPLE_RETRY_INTERVAL_S["15:00:05"]
    assert throttled >= 30  # 收盘档节流（分钟级），不是 5s 轮询节奏
    assert _attempts_cap("15:00:05") >= _grace_s("15:00:05") // throttled
    assert _attempts_cap("09:15:30") == MAX_ATTEMPTS_PER_SAMPLE == 3  # 默认不变


# ── 2/3. 归一与摘要 ─────────────────────────────────────────────────


@pytest.mark.unit
def test_normalize_record_formats_and_missing_cells():
    from backend.services.live_trading.services.call_auction_watch import (
        normalize_record,
    )

    rows = normalize_record(
        {
            "Time": ["091500", "09:16:03"],
            "Price": ["10.5", "10.6"],
            "Volume": ["300", "500"],
            "LeaveQty": ["-100", None],
        }
    )
    assert [r["time"] for r in rows] == ["09:15:00", "09:16:03"]
    assert rows[0] == {
        "time": "09:15:00",
        "price": 10.5,
        "volume": 300,
        "leave_qty": -100,
    }
    # 缺失单元格记 None（0 有真实语义，绝不填 0）
    assert rows[1]["leave_qty"] is None
    # 列短于 Time 以 Time 为准，缺格 None
    short = normalize_record({"Time": ["091500", "091501"], "Price": ["1.0"]})
    assert short[1]["price"] is None and short[1]["volume"] is None
    # 非法输入
    assert normalize_record(None) == []
    assert normalize_record({}) == []
    assert normalize_record("x") == []  # type: ignore[arg-type]


@pytest.mark.unit
def test_summarize_empty_and_values():
    from backend.services.live_trading.services.call_auction_watch import summarize

    assert summarize([]) == {"rows": 0}
    rows = [
        {"time": "09:15:00", "price": 10.0, "volume": 100, "leave_qty": -5},
        {"time": "09:24:57", "price": 10.8, "volume": 400, "leave_qty": 20},
    ]
    s = summarize(rows)
    assert s["rows"] == 2
    assert s["last_time"] == "09:24:57" and s["last_price"] == 10.8
    assert s["last_volume"] == 400 and s["last_leave_qty"] == 20
    assert s["price_min"] == 10.0 and s["price_max"] == 10.8


# ── 3b. 日期盲防御 ──────────────────────────────────────────────────


@pytest.mark.unit
def test_guard_stale_rows_drops_future_sequence():
    from backend.services.live_trading.services.call_auction_watch import (
        FUTURE_TIME_SLACK_S,
        guard_stale_rows,
    )

    now = _tz_now("09:15:30")

    def rows(last_time):
        return [
            {"time": "09:15:00", "price": 10.0, "volume": 1, "leave_qty": 0},
            {"time": last_time, "price": 10.5, "volume": 2, "leave_qty": -1},
        ]

    # 昨日残留（末帧 09:24:57 晚于现在）→ 全丢 + 证据
    kept, stale = guard_stale_rows(rows("09:24:57"), now)
    assert kept == []
    assert stale == {"stale_dropped_rows": 2, "stale_last_time": "09:24:57"}
    # 今日实时（末帧早于现在）→ 原样保留
    kept, stale = guard_stale_rows(rows("09:15:28"), now)
    assert len(kept) == 2 and stale is None
    # 边界：恰在裕量内保留、超裕量丢弃
    from datetime import timedelta

    boundary = (now + timedelta(seconds=FUTURE_TIME_SLACK_S)).strftime("%H:%M:%S")
    kept, _ = guard_stale_rows(rows(boundary), now)
    assert len(kept) == 2
    after = (now + timedelta(seconds=FUTURE_TIME_SLACK_S + 1)).strftime("%H:%M:%S")
    kept, stale = guard_stale_rows(rows(after), now)
    assert kept == [] and stale is not None
    # 空行 / 时间无法解析 → 不误杀
    assert guard_stale_rows([], now) == ([], None)
    kept, stale = guard_stale_rows([{"time": "", "price": None}], now)
    assert len(kept) == 1 and stale is None


# ── 3c. 尾盘段内容门（收盘档） ──────────────────────────────────────


@pytest.mark.unit
def test_has_tail_segment():
    from backend.services.live_trading.services.call_auction_watch import (
        has_tail_segment,
    )

    morning = {"600036.SH": {"rows": [{"time": "09:24:57"}]}}
    assert has_tail_segment(morning) is False
    with_tail = {"600036.SH": {"rows": [{"time": "09:24:57"}, {"time": "14:59:57"}]}}
    assert has_tail_segment(with_tail) is True
    assert has_tail_segment({}) is False
    # 停牌股 rows 空不影响判据：任一标的有尾盘行即视发布
    mixed = {
        "600036.SH": {"rows": []},
        "300750.SZ": {"rows": [{"time": "14:57:00"}]},
    }
    assert has_tail_segment(mixed) is True


# ── 4. 名单合并 ─────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_collect_targets_merges_holdings_and_pool(monkeypatch):
    import backend.shared.decision_context_source as dcs
    import backend.shared.real_positions as rp
    import backend.shared.simulation_account_keys as sak
    import backend.shared.stock_name_mapper as snm

    async def fake_positions(tenant, user):
        assert user == "10000001"
        return (
            {
                "SH600036": {"stock_name": "招商银行", "volume": 100},
                "SZ000001": {"volume": 200},
            },  # 无名字段 → 兜底补齐
            {},
        )

    monkeypatch.setattr(
        sak, "resolve_db_account_user", lambda env, default=None: "10000001"
    )
    monkeypatch.setattr(rp, "load_real_positions", fake_positions)
    monkeypatch.setattr(
        dcs,
        "load_pool_doc",
        lambda day: SimpleNamespace(
            rows=[
                SimpleNamespace(code="600036.SH", name="招商银行"),
                SimpleNamespace(code="300750.SZ", name="宁德时代"),
            ]
        ),
    )
    monkeypatch.setattr(
        snm, "resolve_name", lambda s: {"000001.SZ": "平安银行"}.get(s, "")
    )

    from backend.services.live_trading.services.call_auction_watch import (
        collect_targets,
    )

    targets, errors = await collect_targets("20261015")
    assert errors == []
    assert set(targets) == {"600036.SH", "000001.SZ", "300750.SZ"}
    # 600036 两源都命中：roles 并集 + 名字取真实值
    assert targets["600036.SH"]["roles"] == ["holding", "candidate"]
    assert targets["600036.SH"]["name"] == "招商银行"
    # 仅持仓：名字兜底
    assert targets["000001.SZ"]["roles"] == ["holding"]
    assert targets["000001.SZ"]["name"] == "平安银行"
    # 仅候选
    assert targets["300750.SZ"]["roles"] == ["candidate"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_collect_targets_one_source_failure_does_not_block(monkeypatch):
    import backend.shared.decision_context_source as dcs
    import backend.shared.real_positions as rp
    import backend.shared.simulation_account_keys as sak

    async def boom(tenant, user):
        raise RuntimeError("快照表不可达")

    monkeypatch.setattr(
        sak, "resolve_db_account_user", lambda env, default=None: "10000001"
    )
    monkeypatch.setattr(rp, "load_real_positions", boom)
    monkeypatch.setattr(
        dcs,
        "load_pool_doc",
        lambda day: SimpleNamespace(
            rows=[SimpleNamespace(code="600036.SH", name="招商银行")]
        ),
    )

    from backend.services.live_trading.services.call_auction_watch import (
        collect_targets,
    )

    targets, errors = await collect_targets("20261015")
    assert set(targets) == {"600036.SH"}  # 池仍在
    assert len(errors) == 1 and "实盘持仓取数失败" in errors[0]


# ── 5. 拆批 ─────────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetch_records_splits_batches(monkeypatch):
    import backend.shared.tdx_aidata.client as tdx_client

    calls: list[list[str]] = []

    async def fake_batch(chunk, *, type, timeout):
        calls.append(list(chunk))
        return {
            c: {"Time": [], "Price": [], "Volume": [], "LeaveQty": []} for c in chunk
        }

    monkeypatch.setattr(
        tdx_client,
        "default_cluster",
        lambda: SimpleNamespace(
            primary=SimpleNamespace(get_call_auction_batch=fake_batch)
        ),
    )

    import backend.services.live_trading.services.call_auction_watch as caw

    monkeypatch.setattr(caw, "BATCH_MAX", 2)
    out = await caw.fetch_records(["600000.SH", "600001.SH", "600002.SH"], 1)
    assert sorted(out) == ["600000.SH", "600001.SH", "600002.SH"]
    assert [len(c) for c in calls] == [2, 1]  # 拆批顺序稳定


# ── 6. 落点 ─────────────────────────────────────────────────────────


class _FakePipe:
    def __init__(self):
        self.ops: list[tuple] = []

    def hset(self, key, field, value):
        self.ops.append(("hset", key, field, value))
        return self

    def expire(self, key, ttl):
        self.ops.append(("expire", key, ttl))
        return self

    def set(self, key, value, ex=None):
        self.ops.append(("set", key, value, ex))
        return self

    def execute(self):
        return []


@pytest.mark.unit
def test_write_outputs_archive_and_redis(monkeypatch, tmp_path):
    import backend.services.trade_shared.deps as deps

    fake_pipe = _FakePipe()
    monkeypatch.setattr(
        deps,
        "get_redis",
        lambda: SimpleNamespace(
            client=SimpleNamespace(pipeline=lambda transaction=False: fake_pipe)
        ),
    )
    monkeypatch.setenv("QM_CALL_AUCTION_ARCHIVE_DIR", str(tmp_path))

    from backend.services.live_trading.services.call_auction_watch import write_outputs

    per_symbol = {
        "600036.SH": {
            "name": "招商银行",
            "roles": ["holding"],
            "summary": {"rows": 3, "last_price": 10.5},
            "rows": [
                {"time": "09:15:00", "price": 10.5, "volume": 100, "leave_qty": -1}
            ],
        }
    }
    status = write_outputs(
        day="20261015",
        sample="09:25:05",
        auction_type=1,
        per_symbol=per_symbol,
        errors=[],
        now=_tz_now("09:25:05"),
    )

    # 归档：date= 分区 + 原子写（tmp 不残留）+ 全量 rows
    archive = tmp_path / "date=20261015" / "sample-092505.json"
    assert archive.exists() and status["archive"] == str(archive)
    assert not list(tmp_path.rglob("*.tmp"))
    doc = json.loads(archive.read_text(encoding="utf-8"))
    assert doc["symbols"]["600036.SH"]["rows"][0]["leave_qty"] == -1

    # Redis：hash field 为前缀码；状态键带汇总
    hset = [op for op in fake_pipe.ops if op[0] == "hset"]
    assert (
        hset and hset[0][1] == "qm:call_auction:20261015" and hset[0][2] == "SH600036"
    )
    status_op = [op for op in fake_pipe.ops if op[0] == "set"]
    assert status_op and status_op[0][1] == "qm:call_auction:status"
    saved = json.loads(status_op[0][2])
    assert saved["targets"] == 1 and saved["holdings"] == 1 and saved["rows_total"] == 3


# ── 7. 端到端单档 ───────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_run_sample_end_to_end(monkeypatch, tmp_path):
    import backend.services.trade_shared.deps as deps

    fake_pipe = _FakePipe()
    monkeypatch.setattr(
        deps,
        "get_redis",
        lambda: SimpleNamespace(
            client=SimpleNamespace(pipeline=lambda transaction=False: fake_pipe)
        ),
    )
    monkeypatch.setenv("QM_CALL_AUCTION_ARCHIVE_DIR", str(tmp_path))

    import backend.services.live_trading.services.call_auction_watch as caw

    async def fake_targets(day):
        return (
            {
                "600036.SH": {
                    "symbol": "600036.SH",
                    "name": "招商银行",
                    "roles": ["holding"],
                },
                "300750.SZ": {
                    "symbol": "300750.SZ",
                    "name": "宁德时代",
                    "roles": ["candidate"],
                },
            },
            [],
        )

    async def fake_fetch(symbols, auction_type):
        assert auction_type == 1 and symbols == ["300750.SZ", "600036.SH"]
        # 键故意用异形（前缀式）→ 索引兜底必须仍能匹配
        return {
            "SH600036": {
                "Time": ["091500", "092457"],
                "Price": ["10.0", "10.5"],
                "Volume": ["100", "300"],
                "LeaveQty": ["-5", "-1"],
            }
        }

    monkeypatch.setattr(caw, "collect_targets", fake_targets)
    monkeypatch.setattr(caw, "fetch_records", fake_fetch)

    ok = await caw.run_sample(_tz_now("09:25:05"), "09:25:05")
    assert ok is True
    hset = [op for op in fake_pipe.ops if op[0] == "hset"]
    fields = {op[2] for op in hset}
    assert fields == {"SH600036", "SZ300750"}
    zsyh = json.loads(next(op[3] for op in hset if op[2] == "SH600036"))
    assert zsyh["summary"]["rows"] == 2 and zsyh["summary"]["last_price"] == 10.5
    ndsd = json.loads(next(op[3] for op in hset if op[2] == "SZ300750"))
    assert ndsd["summary"] == {"rows": 0}  # 无记录=合法空，不是错误

    # 取数抛异常 → 返回 False（交回循环宽限重试），不写半截数据
    async def boom(symbols, auction_type):
        raise RuntimeError("SDK 断线")

    monkeypatch.setattr(caw, "fetch_records", boom)
    assert await caw.run_sample(_tz_now("09:26:00"), "09:25:05") is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_run_sample_drops_stale_future_records(monkeypatch, tmp_path):
    """09:15:30 档拿到昨日残留全序列（末帧 09:24:57）→ 守卫丢弃 + 证据进摘要。"""
    import backend.services.trade_shared.deps as deps

    fake_pipe = _FakePipe()
    monkeypatch.setattr(
        deps,
        "get_redis",
        lambda: SimpleNamespace(
            client=SimpleNamespace(pipeline=lambda transaction=False: fake_pipe)
        ),
    )
    monkeypatch.setenv("QM_CALL_AUCTION_ARCHIVE_DIR", str(tmp_path))

    import backend.services.live_trading.services.call_auction_watch as caw

    async def fake_targets(day):
        return (
            {
                "600036.SH": {
                    "symbol": "600036.SH",
                    "name": "招商银行",
                    "roles": ["holding"],
                }
            },
            [],
        )

    async def fake_fetch(symbols, auction_type):
        return {
            "600036.SH": {
                "Time": ["091500", "092457"],
                "Price": ["10.0", "10.5"],
                "Volume": ["100", "300"],
                "LeaveQty": ["-5", "-1"],
            }
        }

    monkeypatch.setattr(caw, "collect_targets", fake_targets)
    monkeypatch.setattr(caw, "fetch_records", fake_fetch)

    ok = await caw.run_sample(_tz_now("09:15:30"), "09:15:30")
    assert ok is True
    hset = [op for op in fake_pipe.ops if op[0] == "hset"]
    doc = json.loads(hset[0][3])
    assert doc["summary"]["rows"] == 0
    assert doc["summary"]["stale_dropped_rows"] == 2
    assert doc["summary"]["stale_last_time"] == "09:24:57"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_run_sample_closing_slot_waits_for_tail(monkeypatch, tmp_path):
    """收盘档（type=0）内容门：尾盘段未发布 → False 不落盘；发布后 → True 含尾盘。

    2026-10-15 实测背景：15:00:05 当刻 SDK type=0 仍只回早盘段（尾盘发布滞后），
    早盘本体已由 09:25:05 档存档——收盘档未拿到尾盘前绝不写盘冒充全量。
    """
    import backend.services.trade_shared.deps as deps

    fake_pipe = _FakePipe()
    monkeypatch.setattr(
        deps,
        "get_redis",
        lambda: SimpleNamespace(
            client=SimpleNamespace(pipeline=lambda transaction=False: fake_pipe)
        ),
    )
    monkeypatch.setenv("QM_CALL_AUCTION_ARCHIVE_DIR", str(tmp_path))

    import backend.services.live_trading.services.call_auction_watch as caw

    async def fake_targets(day):
        return (
            {
                "600036.SH": {
                    "symbol": "600036.SH",
                    "name": "招商银行",
                    "roles": ["holding"],
                }
            },
            [],
        )

    async def fetch_morning_only(symbols, auction_type):
        assert auction_type == 0
        return {
            "600036.SH": {
                "Time": ["091500", "092457"],
                "Price": ["10.0", "10.5"],
                "Volume": ["100", "300"],
                "LeaveQty": ["-5", "-1"],
            }
        }

    monkeypatch.setattr(caw, "collect_targets", fake_targets)
    monkeypatch.setattr(caw, "fetch_records", fetch_morning_only)

    ok = await caw.run_sample(_tz_now("15:00:07"), "15:00:05")
    assert ok is False  # 交回循环按节流间隔重试
    assert fake_pipe.ops == []  # 不写 Redis
    assert not list(tmp_path.rglob("*.json"))  # 不写归档

    async def fetch_full(symbols, auction_type):
        return {
            "600036.SH": {
                "Time": ["091500", "092457", "145700", "145957"],
                "Price": ["10.0", "10.5", "10.8", "10.8"],
                "Volume": ["100", "300", "50", "120"],
                "LeaveQty": ["-5", "-1", "10", "0"],
            }
        }

    monkeypatch.setattr(caw, "fetch_records", fetch_full)
    ok = await caw.run_sample(_tz_now("15:12:00"), "15:00:05")
    assert ok is True
    doc = json.loads(
        (tmp_path / "date=20261015" / "sample-150005.json").read_text(encoding="utf-8")
    )
    assert doc["symbols"]["600036.SH"]["summary"]["last_time"] == "14:59:57"
    hset = [op for op in fake_pipe.ops if op[0] == "hset"]
    assert hset and json.loads(hset[0][3])["sample"] == "15:00:05"


@pytest.mark.unit
def test_write_failure_status_overwrites_stale_success_heartbeat(monkeypatch):
    """取数失败必须覆盖状态键——否则读侧继续显示上一次成功档（2026-10-20）。"""
    from datetime import datetime
    from types import SimpleNamespace

    import backend.services.trade_shared.deps as deps

    ops: list[tuple] = []
    fake_client = SimpleNamespace(
        set=lambda key, value, ex=None: ops.append((key, value, ex))
    )
    monkeypatch.setattr(deps, "get_redis", lambda: SimpleNamespace(client=fake_client))

    from backend.services.live_trading.services.call_auction_watch import (
        STATUS_KEY,
        STATUS_TTL_S,
        write_failure_status,
    )

    status = write_failure_status(
        day="20261020",
        sample="09:25:05",
        auction_type=1,
        error="TdxAiDataError: Token Insufficient",
        now=datetime(2026, 10, 20, 9, 25, 5),
    )

    assert status["failed"] is True
    assert status["targets"] is None
    key, value, ex = ops[0]
    assert key == STATUS_KEY and ex == STATUS_TTL_S
    import json

    saved = json.loads(value)
    assert saved["failed"] is True
    assert "Token Insufficient" in saved["errors"][0]
