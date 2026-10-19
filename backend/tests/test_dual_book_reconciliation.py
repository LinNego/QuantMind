"""双轨对账 + 收盘核对 + 镜像跳过记录 单测（无真库/真机）。

覆盖 Phase 3.1/3.3：
* ``dual_book_reconciliation_task.build_reconciliation_report``（纯函数）
* ``close_cleanup_audit_task.classify_close_state``（纯函数）
* ``real_mirror_service.record_skip / load_skips``（Redis 哈希）
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from backend.services.live_trading.services import real_mirror_service as mirror
from backend.services.trade.services import (
    close_cleanup_audit_task as audit,
    dual_book_reconciliation_task as reconcile,
)


# --------------------------------------------------------------------------
# 替身
# --------------------------------------------------------------------------
class FakePipeline:
    def __init__(self, client: FakeRedisClient) -> None:
        self.client = client
        self.ops: list[tuple] = []

    def hincrby(self, key, field, amount):
        self.ops.append(("hincrby", key, field, amount))
        return self

    def hset(self, key, field, value):
        self.ops.append(("hset", key, field, value))
        return self

    def expire(self, key, ttl):
        self.ops.append(("expire", key, ttl))
        return self

    def execute(self):
        for op in self.ops:
            if op[0] == "hincrby":
                _, key, field, amount = op
                store = self.client.hashes.setdefault(key, {})
                store[field] = int(store.get(field, 0)) + int(amount)
            elif op[0] == "hset":
                _, key, field, value = op
                self.client.hashes.setdefault(key, {})[field] = value
        self.ops = []
        return []


class FakeRedisClient:
    """最小 Redis 替身：哈希（跳过/失败台账）+ 字符串键（报表/去重）。

    ``set/get/exists`` 不是装饰：报表落盘（``_save_report``）与通知去重键
    （``_notify_report`` 的 ``set(nx=True)``）都走字符串键；少了它们，那些
    分支会被自己的 ``except Exception`` 吞掉，测试就再也看不见「有没有落盘」。
    """

    def __init__(self) -> None:
        self.hashes: dict[str, dict] = {}
        self.ttls: dict[str, int] = {}
        self.kv: dict[str, str] = {}
        self.set_calls: list[tuple] = []

    def pipeline(self) -> FakePipeline:
        return FakePipeline(self)

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def set(self, key, value, nx: bool = False, ex: int | None = None):
        self.set_calls.append((key, value, nx, ex))
        if nx and key in self.kv:
            return None
        self.kv[key] = value
        return True

    def get(self, key):
        return self.kv.get(key)

    def exists(self, key) -> bool:
        return key in self.kv


class FakeRedis:
    def __init__(self) -> None:
        self.client = FakeRedisClient()


# --------------------------------------------------------------------------
# 镜像跳过记录
# --------------------------------------------------------------------------
class TestSkipRecording:
    def test_record_and_load_roundtrip(self) -> None:
        redis = FakeRedis()
        mirror.record_skip(
            redis, symbol="600036.SH", side="SELL", quantity=100, reason="price_drift", source="t"
        )
        mirror.record_skip(
            redis, symbol="600036.SH", side="SELL", quantity=100, reason="price_drift", source="t"
        )
        mirror.record_skip(
            redis, symbol="000001.SZ", side="BUY", quantity=200, reason="whitelist", source="t"
        )
        # record_skip 写"当天"键（trade_date_str）：读取必须同一日期口径，
        # 写死日期曾在隔日全红（时间冻结型测试缺陷，T-P2-06 修复）
        from backend.services.live_trading.services.trading_session import trade_date_str

        counts = mirror.load_skips(redis, trade_date_str())
        assert counts["600036.SH:price_drift"] == 2
        assert counts["000001.SZ:whitelist"] == 1
        # detail 字段不参与计数
        assert all(not key.endswith(":detail") for key in counts)

    def test_record_skip_tolerates_broken_client(self) -> None:
        class Broken:
            class client:  # noqa: N801
                @staticmethod
                def pipeline():
                    raise RuntimeError("redis down")

        mirror.record_skip(
            Broken, symbol="600036.SH", side="SELL", quantity=1, reason="x", source="t"
        )  # 不抛异常
        assert mirror.load_skips(FakeRedis(), "20260911") == {}


class TestSubmitPayloadSkipLedger:
    """``_submit_payload`` 里**两条裸返回**的跳过路径也必须进跳过台账。

    限额拒绝（``_reserve_quota`` → max_order_value 等）与下发前复检拦截
    （``_dispatch_gate_blocked`` → 急停/开关/名单）原先直接 return 不 record_skip：
    停在 15:10 对账报表里就是 unexplained 缺口 → 假告警（2026-10-14 实测：
    3 笔 max_order_value 跳过全部隐形）。
    """

    _PAYLOAD = {
        "symbol": "600338.SH",
        "side": "BUY",
        "quantity": 800.0,
        "price": 10.0,
        "client_order_id": "mir-test-1",
        "source": "llm_decision",
    }

    def _run(self, redis, *, reserve, blocked=None, released=None):
        async def _ref_price(symbol):
            return 10.0

        async def _acct(redis_):
            return {"cash": 1e9, "available_volume": {}}

        patches = [
            patch.object(mirror, "_reference_price", _ref_price),
            patch.object(mirror, "_account_snapshot", _acct),
            patch.object(mirror, "_reserve_quota", reserve),
        ]
        if blocked is not None:
            patches.append(
                patch.object(mirror, "_dispatch_gate_blocked", lambda *a, **k: blocked)
            )
            patches.append(
                patch.object(
                    mirror, "_release_quota", released or (lambda *a, **k: None)
                )
            )
        return patches

    def _invoke(self, redis):
        return asyncio.run(
            mirror._submit_payload(
                db=None,
                redis=redis,
                cfg=mirror.MirrorConfig(),
                payload=dict(self._PAYLOAD),
            )
        )

    def test_quota_reject_records_skip_with_source(self) -> None:
        redis = FakeRedis()

        def _reject(redis_, cfg_, *, symbol, value):
            return (
                False,
                "max_order_value",
                {
                    "date": "20261014",
                    "daily_value": 99999.0,
                    "daily_orders": 3,
                    "daily_symbols": 2,
                },
            )

        patches = self._run(redis, reserve=_reject)
        with patches[0], patches[1], patches[2]:
            result = self._invoke(redis)

        assert result["status"] == "skipped"
        assert result["reason"] == "max_order_value"
        counts = mirror.load_skips(redis)
        assert counts["600338.SH:max_order_value"] == 1
        # detail 里带得出 source（排障要能分清哪条链跳的）
        key = next(k for k in redis.client.hashes if k.startswith("mirror:skipped:"))
        detail = redis.client.hashes[key]["600338.SH:max_order_value:detail"]
        assert '"source": "llm_decision"' in detail

    def test_dispatch_gate_block_records_skip_and_releases_quota(self) -> None:
        redis = FakeRedis()
        released: list[dict] = []

        def _reserve(redis_, cfg_, *, symbol, value):
            return (
                True,
                "",
                {
                    "date": "20261014",
                    "new_symbol": 1,
                    "daily_value": 8160.0,
                    "daily_orders": 1,
                    "daily_symbols": 1,
                },
            )

        def _release(redis_, cfg_, **kwargs):
            released.append(kwargs)

        patches = self._run(
            redis, reserve=_reserve, blocked="kill_switch", released=_release
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            result = self._invoke(redis)

        assert result["reason"] == "kill_switch"
        assert mirror.load_skips(redis)["600338.SH:kill_switch"] == 1
        # 额度回滚仍发生，且带回预留时的日期键
        assert released and released[0]["symbol"] == "600338.SH"
        assert released[0]["was_new_symbol"] is True
        assert released[0]["date_str"] == "20261014"


# --------------------------------------------------------------------------
# 双轨对账
# --------------------------------------------------------------------------
class TestReconciliationReport:
    def test_matched_books_ok(self) -> None:
        report = reconcile.build_reconciliation_report(
            date_str="20260911",
            sim_rows=[("600036.SH", "BUY", 100), ("000001.SZ", "SELL", 200)],
            real_rows=[("600036.SH", "BUY", 100), ("000001.SZ", "SELL", 200)],
            skips={},
        )
        assert report["ok"] is True
        assert report["diffs"] == []

    def test_shortfall_explained_by_skip(self) -> None:
        report = reconcile.build_reconciliation_report(
            date_str="20260911",
            sim_rows=[("600036.SH", "SELL", 1000)],
            real_rows=[],
            skips={"600036.SH:price_drift": 1},
        )
        assert report["ok"] is True
        diff = report["diffs"][0]
        assert diff["kind"] == "shortfall"
        assert diff["explained"] is True
        assert diff["delta"] == -1000
        assert diff["skip_reasons"] == {"price_drift": 1}

    def test_shortfall_without_skip_is_unexplained(self) -> None:
        report = reconcile.build_reconciliation_report(
            date_str="20260911",
            sim_rows=[("600036.SH", "SELL", 1000)],
            real_rows=[("600036.SH", "SELL", 400)],
            skips={},
        )
        assert report["ok"] is False
        assert report["unexplained"][0]["delta"] == -600

    def test_excess_always_unexplained(self) -> None:
        report = reconcile.build_reconciliation_report(
            date_str="20260911",
            sim_rows=[("600036.SH", "BUY", 100)],
            real_rows=[("600036.SH", "BUY", 300)],
            skips={"600036.SH:whitelist": 2},
        )
        assert report["ok"] is False
        diff = report["diffs"][0]
        assert diff["kind"] == "excess"
        assert diff["explained"] is False

    def test_real_only_symbol_counted(self) -> None:
        report = reconcile.build_reconciliation_report(
            date_str="20260911",
            sim_rows=[],
            real_rows=[("600036.SH", "SELL", 100)],
            skips={},
        )
        assert report["diffs"][0]["kind"] == "excess"
        assert report["sim_symbols"] == 0
        assert report["real_symbols"] == 1


class TestSymbolNormalization:
    """跨栈符号口径必须归一后再 join：模拟账前缀式（SH600338）、真单/台账键后缀式
    （600338.SH、``mirror:skipped`` 键）。

    不归一化时同一标的在两账里是「两个代码」：既报一笔假 excess 又报一笔假
    shortfall，且跳过记录永远挂不上缺口 → 全判 unexplained（2026-10-14 15:10
    实测假告警：600338 被拆成 600338.SH 多 800 + SH600338 缺 2800）。
    """

    def test_prefix_sim_and_suffix_real_join_into_one_shortfall(self) -> None:
        report = reconcile.build_reconciliation_report(
            date_str="20261014",
            sim_rows=[("SH600338", "BUY", 2800)],
            real_rows=[("600338.SH", "BUY", 800)],
            skips={},
        )
        assert len(report["diffs"]) == 1
        diff = report["diffs"][0]
        assert diff["symbol"] == "600338.SH"
        assert diff["kind"] == "shortfall"
        assert diff["delta"] == -2000
        assert report["sim_symbols"] == 1
        assert report["real_symbols"] == 1

    def test_suffix_skip_key_explains_prefix_sim_row(self) -> None:
        report = reconcile.build_reconciliation_report(
            date_str="20261014",
            sim_rows=[("SH600338", "BUY", 2800)],
            real_rows=[("600338.SH", "BUY", 800)],
            skips={"600338.SH:max_order_value": 3},
        )
        diff = report["diffs"][0]
        assert diff["explained"] is True
        assert diff["skip_reasons"] == {"max_order_value": 3}
        assert report["ok"] is True

    def test_bare_digit_sim_row_joins_suffix_real_row(self) -> None:
        report = reconcile.build_reconciliation_report(
            date_str="20261014",
            sim_rows=[("000681", "BUY", 900)],
            real_rows=[("000681.SZ", "BUY", 900)],
            skips={},
        )
        assert report["diffs"] == []
        assert report["ok"] is True

    def test_mixed_forms_of_same_symbol_aggregate_once(self) -> None:
        report = reconcile.build_reconciliation_report(
            date_str="20261014",
            sim_rows=[("SH600338", "BUY", 100), ("600338.SH", "BUY", 200)],
            real_rows=[],
            skips={"sh600338:price_drift": 1},  # 小写台账键同样要能挂上
        )
        assert report["sim_symbols"] == 1
        diff = report["diffs"][0]
        assert diff["symbol"] == "600338.SH"
        assert diff["sim_quantity"] == 300
        assert diff["explained"] is True


class TestFailureAwareness:
    """真单**提交失败**台账（``mirror:failed:*``）接进对账报表。

    跳过与失败是两回事：「我们决定不发」vs「发了没成」。失败**不解释**缺口——
    缺口照旧是 unexplained（照样告警），但要把原因写在缺口旁边，否则 15:10 的报表
    只会说「缺口 1000 股」，而原因在另一个键里躺着。
    """

    @staticmethod
    def _report(*, sim_rows, real_rows, skips=None, failures=None):
        return reconcile.build_reconciliation_report(
            date_str="20260911",
            sim_rows=sim_rows,
            real_rows=real_rows,
            skips=skips,
            failures=failures,
        )

    def test_a_failure_does_not_explain_the_gap_away(self) -> None:
        report = self._report(
            sim_rows=[("600036.SH", "SELL", 1000)],
            real_rows=[],
            failures={"600036.SH:TDX_UNAVAILABLE": 1},
        )

        assert report["ok"] is False
        diff = report["diffs"][0]
        assert diff["explained"] is False
        assert [d["symbol"] for d in report["unexplained"]] == ["600036.SH"]

    def test_failure_reasons_ride_along_on_the_diff(self) -> None:
        report = self._report(
            sim_rows=[("600036.SH", "SELL", 1000)],
            real_rows=[],
            failures={"600036.SH:TDX_UNAVAILABLE": 1},
        )

        assert report["diffs"][0]["failure_reasons"] == {"TDX_UNAVAILABLE": 1}

    def test_failures_do_not_bleed_across_symbols_or_into_skips(self) -> None:
        report = self._report(
            sim_rows=[("600036.SH", "SELL", 1000), ("000001.SZ", "BUY", 100)],
            real_rows=[],
            skips={"600036.SH:price_drift": 1},
            failures={"000001.SZ:rejected": 1},
        )

        by_symbol = {d["symbol"]: d for d in report["diffs"]}
        assert by_symbol["600036.SH"]["skip_reasons"] == {"price_drift": 1}
        assert by_symbol["600036.SH"]["failure_reasons"] == {}
        assert by_symbol["600036.SH"]["explained"] is True
        assert by_symbol["000001.SZ"]["failure_reasons"] == {"rejected": 1}
        assert by_symbol["000001.SZ"]["skip_reasons"] == {}
        assert by_symbol["000001.SZ"]["explained"] is False

    def test_failure_events_are_counted_even_without_a_gap(self) -> None:
        """失败一笔、重试成功 ⇒ 没有缺口，但计数要在（面板读得出「今天失败过」）。"""
        recovered = self._report(
            sim_rows=[("600036.SH", "SELL", 1000)],
            real_rows=[("600036.SH", "SELL", 1000)],
            failures={"600036.SH:TDX_UNAVAILABLE": 2},
        )
        assert recovered["failure_events"] == 2
        assert recovered["ok"] is True

        clean = self._report(sim_rows=[], real_rows=[])
        assert clean["failure_events"] == 0

    def test_a_report_without_failures_keeps_the_old_shape(self) -> None:
        """滚动升级：老调用方不传 failures，报表与旧版逐字段相同。"""
        report = self._report(
            sim_rows=[("600036.SH", "SELL", 1000)],
            real_rows=[],
            skips={"600036.SH:price_drift": 1},
        )

        assert report["ok"] is True
        assert report["diffs"][0]["failure_reasons"] == {}
        assert report["failure_events"] == 0


class TestReconcileNotification:
    def test_the_alert_names_the_failure_reason(self) -> None:
        published: list[dict] = []

        async def _fake_publish(**kwargs):
            published.append(kwargs)

        report = reconcile.build_reconciliation_report(
            date_str="20260911",
            sim_rows=[("600036.SH", "SELL", 1000)],
            real_rows=[],
            failures={"600036.SH:TDX_UNAVAILABLE": 1},
        )
        with patch(
            "backend.shared.notification_publisher.publish_notification_async",
            _fake_publish,
        ):
            asyncio.run(reconcile._notify_report(FakeRedis(), report))

        assert published, "正向对照：有缺口时通知必须发得出去"
        content = published[0]["content"]
        assert "TDX_UNAVAILABLE" in content
        assert "真单提交失败" in content


class TestFailureLedgerWiring:
    """参数没人传就是死代码 —— 真跑一次任务，看失败台账有没有进报表。"""

    @staticmethod
    def _patch_pipeline(*, failures, skips=None, real_rows=None, direct_real_events=0):
        async def _sim_rows(*_a, **_k):
            return [("600036.SH", "SELL", 1000.0)]

        async def _real_rows(*_a, **_k):
            return real_rows or []

        async def _direct_rows(*_a, **_k):
            return direct_real_events

        return (
            patch.object(reconcile, "collect_sim_rows", _sim_rows),
            patch.object(reconcile, "collect_real_rows", _real_rows),
            # 函数体内 import ⇒ 只有打**源模块**才生效（打在 reconcile 上静默无效）
            patch.object(mirror, "load_skips", lambda *_a, **_k: skips or {}),
            patch.object(mirror, "load_failures", failures),
            patch.object(mirror, "load_config", lambda *_a, **_k: {}),
            patch.object(mirror, "mirror_enabled", lambda *_a, **_k: False),
            patch.object(reconcile, "count_direct_real_orders", _direct_rows),
        )

    def test_the_task_reads_the_failure_ledger(self) -> None:
        patches = self._patch_pipeline(
            failures=lambda *_a, **_k: {"600036.SH:TDX_UNAVAILABLE": 1}
        )
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
        ):
            report = asyncio.run(
                reconcile.run_dual_book_reconciliation(FakeRedis(), "20260911")
            )

        assert report["failure_events"] == 1
        assert report["diffs"][0]["failure_reasons"] == {"TDX_UNAVAILABLE": 1}

    def test_an_unreadable_failure_ledger_is_reported_not_swallowed(self) -> None:
        def _boom(*_a, **_k):
            raise RuntimeError("redis down")

        patches = self._patch_pipeline(failures=_boom)
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
        ):
            report = asyncio.run(
                reconcile.run_dual_book_reconciliation(FakeRedis(), "20260911")
            )

        assert report["ok"] is False
        assert any("failures_query_failed" in e for e in report["errors"])

    def test_direct_real_events_ride_along_without_changing_the_verdict(self) -> None:
        """真实轮直连腿只计数、不解释缺口：sim 侧仍有 shortfall 时照旧不 ok。"""
        patches = self._patch_pipeline(
            failures=lambda *_a, **_k: {}, direct_real_events=4
        )
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
        ):
            report = asyncio.run(
                reconcile.run_dual_book_reconciliation(FakeRedis(), "20260919")
            )

        assert report["direct_real_events"] == 4
        assert report["ok"] is False
        assert report["unexplained"][0]["symbol"] == "600036.SH"


class TestDirectRealExclusion:
    """真实轮直连腿（``mir-lld-…``）不得进入双轨真单侧。

    背景（2026-10-19 实测假告警）：真实轮卖出腿经 ``_submit_direct_real`` 直连
    实盘，订单号是 ``mir-lld-…``；sim 侧按设计没有对应成交。旧查询把所有
    ``mir-%`` 当真单，于是 601857.SH 卖 1300 股、603213.SH 卖 700 股全被判
    ``excess``（「疑似重复下单，严重」）。
    """

    @staticmethod
    def _sql(select_stmt) -> str:
        return str(select_stmt.compile(compile_kwargs={"literal_binds": True}))

    def test_mirror_rows_query_excludes_direct_real_prefix(self) -> None:
        sql = self._sql(reconcile._real_rows_select("20261019")).upper()
        assert "NOT LIKE 'MIR-LLD-%'" in sql
        # 正向对照：仍只取 mir- 前缀的镜像单
        assert "LIKE 'MIR-%'" in sql

    def test_direct_real_count_query_uses_the_same_prefix(self) -> None:
        sql = self._sql(reconcile._direct_real_count_select("20261019")).upper()
        assert "LIKE 'MIR-LLD-%'" in sql
        assert "COUNT" in sql

    def test_notification_names_direct_real_count(self) -> None:
        published: list[dict] = []

        async def _fake_publish(**kwargs):
            published.append(kwargs)

        report = reconcile.build_reconciliation_report(
            date_str="20261019",
            sim_rows=[],
            real_rows=[("601857.SH", "SELL", 1300)],
            skips={},
        )
        report["direct_real_events"] = 4
        with patch(
            "backend.shared.notification_publisher.publish_notification_async",
            _fake_publish,
        ):
            asyncio.run(reconcile._notify_report(FakeRedis(), report))

        assert published
        assert "4 笔真实轮直连实盘" in published[0]["content"]


class TestReconcileDayWindow:
    def test_cst_window_bounds(self) -> None:
        start, end = reconcile.cst_day_window("20260911")
        assert start.hour == 0 and start.minute == 0
        assert (end - start).total_seconds() == 86400
        assert start.tzinfo is not None


# --------------------------------------------------------------------------
# 收盘核对
# --------------------------------------------------------------------------
class TestCloseClassification:
    @staticmethod
    def _counter(order_id: str, status: str, **over) -> dict:
        item = {"order_id": order_id, "status": status, "symbol": "600036.SH", "side": "SELL"}
        item.update(over)
        return item

    def test_all_terminal_ok(self) -> None:
        result = audit.classify_close_state(
            counter_orders=[self._counter("C1", "FILLED"), self._counter("C2", "CANCELLED")],
            local_orders=[{"order_id": "L1", "status": "filled", "exchange_order_id": "C1"}],
        )
        assert result["ok"] is True

    def test_counter_open_flagged(self) -> None:
        result = audit.classify_close_state(
            counter_orders=[self._counter("C1", "SUBMITTED")], local_orders=[]
        )
        assert result["ok"] is False
        assert result["counter_open"][0]["order_id"] == "C1"

    def test_local_stale_when_counter_terminal(self) -> None:
        result = audit.classify_close_state(
            counter_orders=[self._counter("C1", "FILLED")],
            local_orders=[{"order_id": "L1", "status": "submitted", "exchange_order_id": "C1"}],
        )
        assert result["local_stale"][0]["reason"] == "counter_terminal"
        assert result["local_stale"][0]["counter_status"] == "FILLED"

    def test_local_stale_when_counter_missing(self) -> None:
        result = audit.classify_close_state(
            counter_orders=[],
            local_orders=[{"order_id": "L1", "status": "submitted", "exchange_order_id": "CX"}],
        )
        assert result["local_stale"][0]["reason"] == "counter_order_missing"

    def test_local_pending_without_exchange_id(self) -> None:
        result = audit.classify_close_state(
            counter_orders=[],
            local_orders=[{"order_id": "L1", "status": "pending", "exchange_order_id": ""}],
        )
        assert result["local_stale"][0]["reason"] == "no_exchange_order_id"

    def test_local_terminal_not_flagged(self) -> None:
        result = audit.classify_close_state(
            counter_orders=[],
            local_orders=[{"order_id": "L1", "status": "filled", "exchange_order_id": "C1"}],
        )
        assert result["ok"] is True

    def test_parse_audit_time_fallback(self) -> None:
        assert audit.parse_audit_time("15:05") == (15, 5)
        assert audit.parse_audit_time("bad") == (15, 5)
        assert reconcile.parse_reconcile_time("15:10") == (15, 10)
        assert reconcile.parse_reconcile_time("") == (15, 10)

    def test_local_orders_restricted_to_qmt_channel(self) -> None:
        """本地残留只对照 QMT 通道单：桥单拿 QMT 柜台比对全是假残留。"""
        clause = str(
            audit._qmt_channel_clause().compile(compile_kwargs={"literal_binds": True})
        )
        for prefix in audit._QMT_CHANNEL_CID_PREFIXES:
            assert f"'{prefix}%'" in clause
        assert "'mirror:%'" in clause

    def test_run_audit_skips_when_channel_unconfigured(self) -> None:
        class FakeClient:
            configured = False

            async def refresh_settings(self) -> dict:
                return {}

        with patch(
            "backend.services.live_trading.services.qmt_exec_client.get_qmt_exec_client",
            return_value=FakeClient(),
        ):
            report = asyncio.run(audit.run_close_audit(FakeRedis(), "20260911"))
        assert report["skipped"] == "qmt_not_configured"
