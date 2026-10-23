"""T-P2-01 测试：OrderRouter 唯一入口（五路径收敛）。

覆盖：
1. OrderRequest 默认与 strict 解析（即时默认 strict=P0-5；托管允许降级；可显式覆盖）；
2. 参数校验早退（symbol/quantity/uid，不触 DB/Redis）；
3. 即时路径委托 SubmissionService（monkeypatch 捕获 kwargs：source/strict_market 透传、
   outcome 映射含 duplicate 判定）；
4. **五路径接线源断言**（含"引擎旧内联链已移除"防回退）；
5. 镜像收口条件（success 且非 duplicate）；RouterOutcome 形状。
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.services.simulation.services.order_router import (
    OrderRequest,
    RouterOutcome,
    is_duplicate_message,
    submit_order,
)

_BACKEND = Path(__file__).resolve().parents[1]


# --- 请求契约 ---------------------------------------------------------------


def test_request_strict_resolution():
    immediate = OrderRequest(tenant_id="t", user_id=1, symbol="600036.SH", side="buy", quantity=100)
    assert immediate.resolved_strict() is True  # 即时默认 strict（P0-5）
    hosted = OrderRequest(
        tenant_id="t", user_id=1, symbol="600036.SH", side="buy", quantity=100, bar=object()
    )
    assert hosted.resolved_strict() is False  # 托管允许如实降级
    explicit = OrderRequest(
        tenant_id="t", user_id=1, symbol="600036.SH", side="buy", quantity=100, strict_market=False
    )
    assert explicit.resolved_strict() is False


def test_is_duplicate_message():
    assert is_duplicate_message("duplicate client_order_id skipped") is True
    assert is_duplicate_message("duplicate client_order_id expired") is True
    assert is_duplicate_message("filled") is False
    assert is_duplicate_message(None) is False


@pytest.mark.asyncio
async def test_submit_order_validates_before_any_io():
    for req, needle in [
        (OrderRequest(tenant_id="t", user_id=1, symbol="", side="buy", quantity=100), "symbol"),
        (OrderRequest(tenant_id="t", user_id=1, symbol="600036.SH", side="buy", quantity=0), "quantity"),
        (OrderRequest(tenant_id="t", user_id=0, symbol="600036.SH", side="buy", quantity=100), "用户"),
    ]:
        out = await submit_order(db=None, redis=None, req=req)
        assert out.success is False and needle in out.message


# --- 即时路径委托（monkeypatch 捕获） ----------------------------------------


class _FakeSubmissionService:
    captured: dict = {}
    response: SimpleNamespace | None = None

    def __init__(self, db, manager):
        self.db = db

    async def submit_and_fill(self, **kwargs):
        _FakeSubmissionService.captured = kwargs
        return _FakeSubmissionService.response


class _RiskPassed:
    passed = True
    rule_id = None
    reason = ""


@pytest.fixture
def risk_gate_passes(monkeypatch):
    """风控桩：本文件的被测对象是 Router 的**委派契约**，不是风控本身。

    `submit_order` 在委派前一律过 `risk_gate_service.check_order`（T-RC-02），且它
    fail-closed —— 用例传 `redis=object()` 时配置读不出即拒单，于是这些用例在风控
    闸门合入后集体变红，而断言信息只显示 `success=False`、看不出是闸门拒的
    （实测报错：`风控拒单[l0.config]：'object' object has no attribute 'hgetall'`）。
    风控自身的覆盖在 `test_risk_gate_wiring.py`；此处显式桩掉，免得用例名义上测委派、
    实际上测风控。**反向钉子**见 `test_risk_gate_rejection_short_circuits_delegation`。
    """
    from backend.services.trade.services import risk_gate_service

    async def _ok(req, db=None, redis=None):  # noqa: ARG001
        return _RiskPassed()

    monkeypatch.setattr(risk_gate_service, "check_order", _ok)
    return _ok


@pytest.mark.asyncio
async def test_immediate_delegates_and_passes_source_strict(monkeypatch, risk_gate_passes):
    monkeypatch.setattr(
        "backend.services.simulation.services.order_submission_service."
        "SimulationOrderSubmissionService",
        _FakeSubmissionService,
    )
    _FakeSubmissionService.response = SimpleNamespace(
        success=True,
        order_id="o-1",
        trade_id="t-1",
        client_order_id="cid-1",
        fill_price=10.5,
        filled_quantity=100.0,
        commission=5.0,
        price_source="redis_series",
        message="filled",
    )
    req = OrderRequest(
        tenant_id="default",
        user_id=7,
        symbol="600036.SH",
        side="buy",
        quantity=100,
        source="sandbox",
        client_order_id="cid-1",
        strict_market=False,
    )
    out = await submit_order(db=object(), redis=object(), req=req)
    assert out.success and out.order_id == "o-1" and out.fill_price == 10.5
    assert out.duplicate is False
    captured = _FakeSubmissionService.captured
    assert captured["trigger_source"] == "sandbox"
    assert captured["strict_market"] is False
    assert captured["client_order_id"] == "cid-1"
    # bar 透传（2026-09-21 接回）：不是新增形参而是接上被掐断的线——此前引擎从不传
    # bar，取价链的降级分支不可达。此处钉住 Router 这一跳不吞 bar。
    assert "bar" in captured and captured["bar"] is req.bar


@pytest.mark.asyncio
async def test_immediate_duplicate_mapped(monkeypatch, risk_gate_passes):
    monkeypatch.setattr(
        "backend.services.simulation.services.order_submission_service."
        "SimulationOrderSubmissionService",
        _FakeSubmissionService,
    )
    _FakeSubmissionService.response = SimpleNamespace(
        success=True,
        order_id="o-1",
        trade_id=None,
        client_order_id="cid-1",
        fill_price=0.0,
        filled_quantity=0.0,
        commission=0.0,
        price_source=None,
        message="duplicate client_order_id skipped",
    )
    out = await submit_order(
        db=object(),
        redis=object(),
        req=OrderRequest(
            tenant_id="default", user_id=7, symbol="600036.SH", side="buy", quantity=100
        ),
    )
    assert out.success and out.duplicate is True  # 镜像将因 duplicate 跳过


@pytest.mark.asyncio
async def test_risk_gate_rejection_short_circuits_delegation(monkeypatch):
    """反向钉子：闸门拒单时**不得**触达委派，且拒因带规则号（可解释）。

    存在的意义是防止上面那个 `risk_gate_passes` 桩把「风控确实被咨询、且拦在执行之前」
    这条契约一并桩没 —— 只桩掉、不验证，就是「验收假通过」。
    """
    from backend.services.simulation.services import order_router
    from backend.services.trade.services import risk_gate_service

    class _Rejected:
        passed = False
        rule_id = "l1.position_limit"
        reason = "单票持仓超限"

    async def _reject(req, db=None, redis=None):  # noqa: ARG001
        return _Rejected()

    recorded: list[str] = []

    async def _record(db, req, message):  # noqa: ARG001
        recorded.append(message)

    monkeypatch.setattr(risk_gate_service, "check_order", _reject)
    monkeypatch.setattr(order_router, "_record_risk_rejection", _record)
    monkeypatch.setattr(
        "backend.services.simulation.services.order_submission_service."
        "SimulationOrderSubmissionService",
        _FakeSubmissionService,
    )
    _FakeSubmissionService.captured = {}

    out = await submit_order(
        db=object(),
        redis=object(),
        req=OrderRequest(
            tenant_id="default", user_id=7, symbol="600036.SH", side="buy", quantity=100
        ),
    )

    assert out.success is False
    assert _FakeSubmissionService.captured == {}, "风控拒单后仍走到了撮合"
    assert "l1.position_limit" in out.message and "单票持仓超限" in out.message
    # 拒单必须补记台账（否则只在运行日志里，用户翻历史记录看不到）
    assert recorded and "l1.position_limit" in recorded[0]


# --- 风控拒单落台账（2026-10-22 审计缺口） -----------------------------------


@pytest.fixture
def fake_order_service(monkeypatch):
    """桩掉建单/投影两条 IO，捕获落库形状；DDL 自愈也桩掉。"""
    from backend.services.simulation.services import order_service as order_service_mod

    captured: dict = {}

    class _FakeService:
        def __init__(self, db):
            self.db = db

        async def create_order(self, tenant_id, user_id, data, **kwargs):
            captured["create"] = {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "data": data,
                "kwargs": kwargs,
            }
            return SimpleNamespace(status=None, remarks=None)

        async def sync_order_projection(self, order, rejected_reason=None, **kwargs):
            captured["projection"] = {
                "order": order,
                "rejected_reason": rejected_reason,
                "kwargs": kwargs,
            }

    async def _ensure():
        return None

    monkeypatch.setattr(order_service_mod, "SimOrderService", _FakeService)
    monkeypatch.setattr(
        "backend.shared.order_contract.ensure_order_contract_columns_async", _ensure
    )
    return captured


@pytest.mark.asyncio
async def test_risk_rejection_is_written_to_order_history(fake_order_service):
    """2026-10-22 实证：托管模拟盘一轮 20 张卖单全被 l3.stale_quote 拒，
    sim_orders / simulation_orders 两表 0 行 —— 用户翻「历史记录」什么都看不到。
    风控前置拒单必须补记一条 rejected 台账（含拒因与策略归属）。"""
    from backend.services.simulation.services import order_router

    class _Db:
        committed = 0

        async def commit(self):
            self.committed += 1

    db = _Db()
    req = order_router.OrderRequest(
        tenant_id="default",
        user_id=7,
        symbol="600036.SH",
        side="sell",
        quantity=100,
        client_order_id="cid-risk-reject",
        strategy_id=13,
        source="rebalance",
    )

    await order_router._record_risk_rejection(
        db, req, "风控拒单[l3.stale_quote]：行情陈旧"
    )

    created = fake_order_service["create"]
    assert created["data"].client_order_id == "cid-risk-reject"
    assert created["data"].strategy_id == 13
    assert created["kwargs"]["trigger_source"] == "rebalance"

    order = fake_order_service["projection"]["order"]
    assert order.status.value == "rejected"
    assert "l3.stale_quote" in order.remarks
    assert (
        fake_order_service["projection"]["rejected_reason"]
        == "风控拒单[l3.stale_quote]：行情陈旧"
    )
    assert db.committed == 1


@pytest.mark.asyncio
async def test_risk_rejection_not_recorded_without_target_ledger(monkeypatch):
    """无 db（单元路径）或实盘直连（不建模拟腿）时不补记。"""
    from backend.services.simulation.services import order_router
    from backend.services.simulation.services import order_service as order_service_mod

    calls: list[str] = []

    class _ExplodingService:
        def __init__(self, db):  # noqa: ARG002
            calls.append("constructed")

    monkeypatch.setattr(order_service_mod, "SimOrderService", _ExplodingService)

    req = order_router.OrderRequest(
        tenant_id="default", user_id=7, symbol="600036.SH", side="sell", quantity=100
    )
    await order_router._record_risk_rejection(None, req, "风控拒单[x]：y")
    await order_router._record_risk_rejection(
        object(),
        order_router.OrderRequest(
            tenant_id="default",
            user_id=7,
            symbol="600036.SH",
            side="sell",
            quantity=100,
            direct_real=True,
        ),
        "风控拒单[x]：y",
    )
    assert calls == []


# --- 五路径接线源断言 --------------------------------------------------------


def test_five_paths_route_through_router():
    engine = (_BACKEND / "services/simulation/engine.py").read_text(encoding="utf-8")
    assert "from backend.services.simulation.services.order_router import" in engine
    assert "submit_order(" in engine and "mirror=True" in engine
    # 旧内联链已移除（防回退）
    assert "execute_from_bar(" not in engine
    assert "SimOrder(" not in engine

    sandbox = (_BACKEND / "services/trade/services/sandbox_signal_consumer.py").read_text(
        encoding="utf-8"
    )
    assert "submit_order(" in sandbox and "SOURCE_SANDBOX" in sandbox and "mirror=True" in sandbox

    tdx = (_BACKEND / "services/live_trading/services/tdx_rolling_trade_service.py").read_text(
        encoding="utf-8"
    )
    assert "submit_order(" in tdx and "SOURCE_TDX_ROLLING" in tdx

    dispatcher = (
        _BACKEND / "services/live_trading/services/internal_strategy_dispatcher.py"
    ).read_text(encoding="utf-8")
    assert "from backend.services.simulation.services.order_router import" in dispatcher
    assert "submit_order(" in dispatcher

    margin = (_BACKEND / "services/simulation/services/margin_monitor_service.py").read_text(
        encoding="utf-8"
    )
    assert "submit_order(" in margin and "SOURCE_FORCED_LIQUIDATION" in margin


def test_router_outcome_shape():
    r = RouterOutcome(success=True, order_id="o", duplicate=False, mirror={"status": "queued"})
    assert r.success and r.mirror["status"] == "queued"


# --- 实盘直连（真实轮无模拟腿） ----------------------------------------------


class _MirrorRecorder:
    captured: dict = {}
    response: dict = {}

    @classmethod
    async def __call__(cls, **kwargs):
        _MirrorRecorder.captured = kwargs
        return _MirrorRecorder.response


@pytest.fixture
def mirror_recorder(monkeypatch):
    from backend.services.live_trading.services import real_mirror_service

    recorder = _MirrorRecorder()
    _MirrorRecorder.captured = {}
    _MirrorRecorder.response = {"status": "submitted", "order_id": "real-1"}
    monkeypatch.setattr(real_mirror_service, "mirror_virtual_fill", recorder)
    return recorder


@pytest.mark.asyncio
async def test_direct_real_never_touches_simulation_leg(
    monkeypatch, risk_gate_passes, mirror_recorder
):
    """真实轮**不得**建模拟单：模拟台账只是镜子，镜子缺仓会假拒，挡住真单。

    2026-10-15 SH600338 即此形态——模拟腿成交、真单被 ``price_drift`` 跳过，
    两边账面越跑越歪。
    """
    from backend.services.simulation.services import order_submission_service

    class _Boom:
        def __init__(self, *a, **kw):
            raise AssertionError("direct_real 不许构造模拟提交服务")

    monkeypatch.setattr(
        order_submission_service, "SimulationOrderSubmissionService", _Boom
    )

    out = await submit_order(
        db=object(),
        redis=object(),
        req=OrderRequest(
            tenant_id="default",
            user_id=7,
            symbol="600338.SH",
            side="sell",
            quantity=800,
            price=11.21,
            real_limit_price=11.20,
            client_order_id="mir-lld-1",
            direct_real=True,
        ),
    )

    assert out.success is True and out.order_id == "real-1"
    assert out.duplicate is False
    captured = _MirrorRecorder.captured
    assert captured["sim_order_id"] == ""
    assert captured["quantity"] == 800 and captured["price"] == 11.20
    assert captured["client_order_id"] == "mir-lld-1"


@pytest.mark.asyncio
async def test_direct_real_sell_bypasses_price_gate(mirror_recorder, risk_gate_passes):
    """卖出是出逃意图：偏离闸门按盘口价豁免（仍有 20% 脏数据上界在 mirror 内）。"""
    await submit_order(
        db=object(),
        redis=object(),
        req=OrderRequest(
            tenant_id="default",
            user_id=7,
            symbol="600338.SH",
            side="sell",
            quantity=100,
            price=9.0,
            direct_real=True,
        ),
    )
    assert _MirrorRecorder.captured["bypass_price_gate"] is True


@pytest.mark.asyncio
async def test_direct_real_buy_does_not_bypass_price_gate(
    mirror_recorder, risk_gate_passes
):
    """追价不是出逃：买入腿照走偏离闸门。"""
    await submit_order(
        db=object(),
        redis=object(),
        req=OrderRequest(
            tenant_id="default",
            user_id=7,
            symbol="600036.SH",
            side="buy",
            quantity=100,
            price=35.8,
            direct_real=True,
        ),
    )
    assert _MirrorRecorder.captured["bypass_price_gate"] is False


@pytest.mark.asyncio
async def test_direct_real_without_price_does_not_guess(
    mirror_recorder, risk_gate_passes
):
    """没价不下单（不猜价）；mirror 不得被调用。"""
    out = await submit_order(
        db=object(),
        redis=object(),
        req=OrderRequest(
            tenant_id="default",
            user_id=7,
            symbol="600036.SH",
            side="buy",
            quantity=100,
            price=None,
            direct_real=True,
        ),
    )
    assert out.success is False and "价格" in out.message
    assert _MirrorRecorder.captured == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,accepted",
    [
        ("submitted", True),
        ("queued", True),
        ("duplicate", True),
        ("skipped", False),
        ("failed", False),
        ("error", False),
    ],
)
async def test_direct_real_status_mapping(
    status, accepted, mirror_recorder, risk_gate_passes
):
    """受理（submitted/queued/duplicate）≠ 成交，但都不是失败；其余一律如实失败。"""
    _MirrorRecorder.response = {"status": status, "reason": "price_drift"}
    out = await submit_order(
        db=object(),
        redis=object(),
        req=OrderRequest(
            tenant_id="default",
            user_id=7,
            symbol="600036.SH",
            side="buy",
            quantity=100,
            price=35.8,
            direct_real=True,
        ),
    )
    assert out.success is accepted
    assert out.duplicate is (status == "duplicate")
    if not accepted:
        assert "price_drift" in out.message
