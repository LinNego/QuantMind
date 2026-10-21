"""组合选股服务（``model_blend_service``）测试。

锁死六件事（P2 交付验收）：

1. **符号折叠**：SQL 表达式与 ``StockCodeUtil.to_prefix`` 逐例对拍——成员分数
   必须先折叠到前缀式再聚合，否则同票不同码会被记成「缺成员」；
2. **成员闸门**：同特征签名（不含周期）、≥2/≤20、可推理、非 ensemble；
   ``rebalance`` 强制 T+10，``next_day`` 允许跨周期并告警；
3. **纯函数边界**：日期/权重/门槛校验，API 行 → 纯函数行的位置词回填；
4. **榜单口径**：分位在 SQL 内算、贡献和等于 S、符号全部前缀式；
5. **计划口径**：每日 ≤5 只新进、100 股整手、无订单语义、P95 < 15s；
6. **纸面台账**：计划幂等、确认开/平仓、过期/陈旧/重复确认拒绝。

真库用例在 DB 不可用时跳过；测试行按 ``unittest_blend`` 租户随建随删。
"""

from __future__ import annotations

import time
import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import text

from backend.services.engine.inference.blend_engine import BlendRow
from backend.services.engine.inference.blend_plan import positions_hash
from backend.services.engine.services.model_blend_persistence import (
    ModelBlendPersistence,
)
from backend.services.engine.services import model_blend_service as blend_service_module
from backend.services.engine.services.model_blend_service import (
    BlendError,
    ModelBlendService,
    _finite,
    _parse_date,
    _prefix_symbol_sql,
    _rows_from_payload,
    _validate_weights,
    _weights_from_members,
    _window_cache_get,
    _window_cache_key,
    _window_cache_put,
)
from backend.shared.stock_utils import StockCodeUtil

HASH = "e405bd8c8f9e36ab575008664313f653db840b1e9064d1f5527c794a0de5c3a4"
TEST_TENANT = "unittest_blend"
TEST_USER = "blend_user"
SAMPLE_DATE = date(2026, 10, 19)


def _model(
    model_id: str,
    *,
    horizon: int = 10,
    model_type: str = "lightgbm",
    feature_count: int = 300,
    schema_hash: str = HASH,
    data_source: str = "quantdb_factors",
    market: str = "CN",
    source: str = "user",
    status: str = "ready",
) -> dict:
    return {
        "model_id": model_id,
        "model_source": source,
        "display_name": model_id.upper(),
        "market": market,
        "model_type": model_type,
        "target_horizon_days": horizon,
        "feature_count": feature_count,
        "factor_schema_hash": schema_hash,
        "data_source": data_source,
        "factor_source": "l1_factors",
        "status": status,
    }


class _FakePersistence:
    def __init__(self) -> None:
        self.created: dict | None = None
        self.updated: dict | None = None
        self.blend: dict | None = None

    async def create_blend(self, **kwargs):
        self.created = kwargs
        return {"blend_id": "bl_fake", **kwargs}

    async def update_blend(self, blend_id, **kwargs):
        self.updated = {"blend_id": blend_id, **kwargs}

    async def get_blend(self, blend_id, *, tenant_id, user_id):
        if self.blend is None:
            return {
                "blend_id": blend_id,
                "tenant_id": tenant_id,
                "user_id": user_id,
                "status": "active",
            }
        return self.blend


def _service(models, persistence=None) -> ModelBlendService:
    svc = ModelBlendService(persistence=persistence or _FakePersistence())

    async def fake_models(*, tenant_id, user_id, market="CN"):
        return list(models)

    svc._normalized_models = fake_models
    return svc


# ── 纯函数 ───────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_parse_date_accepts_none_date_and_iso():
    assert _parse_date(None) is None
    assert _parse_date(SAMPLE_DATE) == SAMPLE_DATE
    assert _parse_date("2026-10-19") == SAMPLE_DATE


@pytest.mark.unit
def test_parse_date_rejects_bad_format_with_business_code():
    with pytest.raises(BlendError) as exc:
        _parse_date("2026/10/19")
    assert exc.value.code == "invalid_request"
    assert "YYYY-MM-DD" in exc.value.message


@pytest.mark.unit
def test_finite_handles_none_nan_inf_and_strings():
    assert _finite(1) is True
    assert _finite("1.5") is True
    assert _finite(None) is False
    assert _finite(float("nan")) is False
    assert _finite(float("inf")) is False
    assert _finite("abc") is False


@pytest.mark.unit
def test_window_cache_key_tracks_members_weights_date_and_symbols():
    base = {
        "tenant_id": "t",
        "user_id": "u",
        "members": [("a", 1.0), ("b", 1.0)],
        "method": "mean",
        "trim_count": 2,
        "min_coverage": 8,
        "consensus_min_count": 0,
        "end_date": SAMPLE_DATE,
        "lookback": 10,
        "top_k": 50,
        "symbols": ["SH600000", "SZ000001"],
    }
    key = _window_cache_key(**base)
    assert key == _window_cache_key(**{**base, "symbols": ["SZ000001", "SH600000"]})
    assert key != _window_cache_key(**{**base, "members": [("a", 2.0), ("b", 1.0)]})
    assert key != _window_cache_key(**{**base, "end_date": date(2026, 10, 16)})
    assert key != _window_cache_key(**{**base, "symbols": ["SH600000"]})
    assert key != _window_cache_key(**{**base, "method": "trimmed"})


@pytest.mark.unit
def test_window_cache_hit_returns_isolated_copy_and_expires(monkeypatch):
    day = date(2026, 10, 16)
    payload = {
        "scores": {day: {"SH600000": 0.5}},
        "top": {day: {"SH600000"}},
        "top_rows": {day: [{"symbol": "SH600000", "row_rank": 1}]},
        "days": [day],
    }
    blend_service_module._window_cache.clear()
    _window_cache_put("unit-key", payload)
    hit = _window_cache_get("unit-key")
    assert hit == payload
    hit["scores"][day]["SH600000"] = 9.9
    # 滚动视图会往行上挂名称/持有天数，缓存必须深拷贝
    hit["top_rows"][day][0]["row_rank"] = 99
    cached = _window_cache_get("unit-key")
    assert cached["scores"][day]["SH600000"] == 0.5
    assert cached["top_rows"][day][0]["row_rank"] == 1

    now = time.monotonic()
    monkeypatch.setattr(blend_service_module.time, "monotonic", lambda: now + 61.0)
    assert _window_cache_get("unit-key") is None
    blend_service_module._window_cache.clear()


@pytest.mark.unit
def test_window_cache_evicts_oldest_when_full(monkeypatch):
    blend_service_module._window_cache.clear()
    monkeypatch.setattr(blend_service_module, "_WINDOW_CACHE_MAX_ENTRIES", 2)
    for index in range(3):
        _window_cache_put(f"k{index}", {"scores": {}, "top": {}, "days": []})
    assert len(blend_service_module._window_cache) == 2
    assert _window_cache_get("k0") is None
    assert _window_cache_get("k2") is not None
    blend_service_module._window_cache.clear()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_window_stats_serves_cache_without_querying_db(monkeypatch):
    svc = ModelBlendService()
    members = [_model("a"), _model("b")]
    key = _window_cache_key(
        tenant_id="t",
        user_id="u",
        members=[("a", 1.0), ("b", 1.0)],
        method="mean",
        trim_count=2,
        min_coverage=2,
        consensus_min_count=0,
        end_date=SAMPLE_DATE,
        lookback=10,
        top_k=50,
        symbols=["SH600000"],
    )
    payload = {
        "scores": {date(2026, 10, 16): {"SH600000": 0.5}},
        "top": {date(2026, 10, 16): {"SH600000"}},
        "days": [date(2026, 10, 16)],
    }
    blend_service_module._window_cache.clear()
    _window_cache_put(key, payload)
    calls: list[dict] = []

    async def fake_recent_run_dates(**kwargs):
        calls.append(kwargs)
        return []

    monkeypatch.setattr(svc, "_recent_run_dates", fake_recent_run_dates)
    result = await svc._window_stats(
        tenant_id="t",
        user_id="u",
        members=members,
        weights=None,
        method="mean",
        trim_count=2,
        min_coverage=2,
        consensus_min_count=0,
        end_date=SAMPLE_DATE,
        lookback=10,
        top_k=50,
        symbols=["SH600000"],
    )
    assert calls == []
    assert result["scores"][date(2026, 10, 16)]["SH600000"] == 0.5
    blend_service_module._window_cache.clear()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("min_coverage", "consensus", "ok"),
    [(2, 0, True), (5, 5, True), (1, 0, False), (6, 0, False), (2, 6, False)],
)
def test_validate_params_bounds(min_coverage, consensus, ok):
    members = [_model(f"m{i}") for i in range(5)]
    if ok:
        ModelBlendService._validate_params(
            members, min_coverage=min_coverage, consensus_min_count=consensus
        )
    else:
        with pytest.raises(BlendError):
            ModelBlendService._validate_params(
                members, min_coverage=min_coverage, consensus_min_count=consensus
            )


@pytest.mark.unit
def test_validate_weights_allows_none_and_positive_only():
    members = [_model("a"), _model("b")]
    _validate_weights(members, None)
    _validate_weights(members, {"a": 2.0, "b": 0.5})
    with pytest.raises(BlendError):
        _validate_weights(members, {"a": 1.0, "zzz": 1.0})
    for bad in (0.0, -1.0, float("nan"), float("inf")):
        with pytest.raises(BlendError):
            _validate_weights(members, {"a": bad})


@pytest.mark.unit
def test_weights_from_members_equal_is_none_manual_defaults_to_one():
    members = [_model("a"), _model("b")]
    equal = {"weighting": "equal", "members": [{"model_id": "a", "weight": 9.0}]}
    assert _weights_from_members(equal, members) is None
    manual = {"weighting": "manual", "members": [{"model_id": "a", "weight": 2.0}]}
    assert _weights_from_members(manual, members) == {"a": 2.0, "b": 1.0}


@pytest.mark.unit
def test_rows_from_payload_derives_position_from_pct():
    payload = [
        {
            "symbol": "SZ000001",
            "score": 0.8,
            "rank": 1,
            "coverage": 8,
            "pct": 0.9,
            "research_score": 90.0,
        },
        {
            "symbol": "SH600000",
            "score": 0.2,
            "rank": 2,
            "coverage": 8,
            "pct": 0.1,
        },
        {
            "symbol": "SZ300750",
            "score": 0.5,
            "rank": 3,
            "coverage": 8,
            "pct": None,
        },
    ]
    rows = _rows_from_payload(payload)
    assert [row.position for row in rows] == ["BUY", "SELL", "HOLD"]
    assert all(isinstance(row, BlendRow) for row in rows)
    assert rows[0].research_score == 90.0


# ── 成员闸门 ─────────────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_members_dedupes_and_keeps_order():
    svc = _service([_model("a"), _model("b"), _model("c")])
    members = await svc._resolve_members(
        tenant_id="t",
        user_id="u",
        model_ids=["c", "a", "c"],
        market="CN",
        purpose="rebalance",
    )
    assert [m["model_id"] for m in members] == ["c", "a"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_members_rejects_below_min_and_above_max():
    svc = _service([_model("a")])
    with pytest.raises(BlendError):
        await svc._resolve_members(
            tenant_id="t",
            user_id="u",
            model_ids=["a"],
            market="CN",
            purpose="rebalance",
        )
    # 上限 30：T+5 全组模板实有 24 个成员，旧上限 20 会把整组模板挡在 422
    many = _service([_model(f"m{i}") for i in range(31)])
    with pytest.raises(BlendError):
        await many._resolve_members(
            tenant_id="t",
            user_id="u",
            model_ids=[f"m{i}" for i in range(31)],
            market="CN",
            purpose="rebalance",
        )
    ok = await _service([_model(f"m{i}") for i in range(24)])._resolve_members(
        tenant_id="t",
        user_id="u",
        model_ids=[f"m{i}" for i in range(24)],
        market="CN",
        purpose="rebalance",
    )
    assert len(ok) == 24


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_members_reports_unknown_and_ineligible_reasons():
    svc = _service([_model("a"), _model("b"), _model("bad", status="archived")])
    with pytest.raises(BlendError) as exc:
        await svc._resolve_members(
            tenant_id="t",
            user_id="u",
            model_ids=["a", "missing"],
            market="CN",
            purpose="rebalance",
        )
    assert exc.value.code == "unknown_model"
    with pytest.raises(BlendError) as exc:
        await svc._resolve_members(
            tenant_id="t",
            user_id="u",
            model_ids=["a", "bad"],
            market="CN",
            purpose="rebalance",
        )
    assert exc.value.code == "model_not_eligible"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_members_rejects_signature_mismatch_but_ignores_horizon():
    svc = _service(
        [
            _model("a", horizon=10),
            _model("b", horizon=10, schema_hash="other-hash"),
            _model("c", horizon=5),
        ]
    )
    with pytest.raises(BlendError) as exc:
        await svc._resolve_members(
            tenant_id="t",
            user_id="u",
            model_ids=["a", "b"],
            market="CN",
            purpose="next_day",
        )
    assert exc.value.code == "signature_mismatch"
    # next_day 允许跨周期
    members = await svc._resolve_members(
        tenant_id="t",
        user_id="u",
        model_ids=["a", "c"],
        market="CN",
        purpose="next_day",
    )
    assert [m["target_horizon_days"] for m in members] == [10, 5]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_members_rebalance_accepts_any_uniform_horizon():
    svc = _service(
        [
            _model("a10", horizon=10),
            _model("b10", horizon=10),
            _model("a5", horizon=5),
            _model("b5", horizon=5),
            _model("a20", horizon=20),
            _model("b20", horizon=20),
        ]
    )
    for pair, horizon in (
        (["a10", "b10"], 10),
        (["a5", "b5"], 5),
        (["a20", "b20"], 20),
    ):
        members = await svc._resolve_members(
            tenant_id="t",
            user_id="u",
            model_ids=pair,
            market="CN",
            purpose="rebalance",
        )
        assert {m["target_horizon_days"] for m in members} == {horizon}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_members_rebalance_rejects_mixed_horizon():
    svc = _service([_model("a"), _model("b", horizon=5)])
    with pytest.raises(BlendError) as exc:
        await svc._resolve_members(
            tenant_id="t",
            user_id="u",
            model_ids=["a", "b"],
            market="CN",
            purpose="rebalance",
        )
    assert exc.value.code == "mixed_horizon"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_members_rejects_ensemble_member():
    svc = _service(
        [_model("a"), _model("b"), _model("ensemble_1", model_type="stacking")]
    )
    with pytest.raises(BlendError) as exc:
        await svc._resolve_members(
            tenant_id="t",
            user_id="u",
            model_ids=["a", "ensemble_1"],
            market="CN",
            purpose="rebalance",
        )
    assert exc.value.code == "model_not_eligible"


# ── 行业 / 市值 ─────────────────────────────────────────────────────────


@pytest.mark.unit
def test_row_payload_carries_industry_and_market_cap_or_none():
    svc = ModelBlendService()
    row = BlendRow(
        symbol="SZ000001",
        score=0.8,
        rank=1,
        coverage=8,
        dispersion=None,
        pct=0.9,
        research_score=90.0,
        position="HOLD",
        contributions={},
        missing_members=(),
    )
    payload = svc._row_payload(
        row,
        {"SZ000001": "平安银行"},
        {"SZ000001": {"industry": "银行", "market_cap_yi": 1234.5}},
    )
    assert payload["industry"] == "银行"
    assert payload["market_cap_yi"] == pytest.approx(1234.5)
    # 缺档案 → None（前端显示 —），绝不落成 0
    missing = svc._row_payload(row, {}, {})
    assert missing["industry"] is None
    assert missing["market_cap_yi"] is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stock_profiles_maps_suffix_to_prefix_and_drops_nan(monkeypatch):
    import pandas as pd

    from backend.services.engine.factor_research import store

    fake = pd.DataFrame(
        {
            "symbol": ["000001.SZ", "600000.SH", "000002.SZ"],
            "industry": ["银行", "银行", None],
            "total_mv_yi": [1000.0, 2000.0, float("nan")],
        }
    )
    monkeypatch.setattr(store, "stock_snapshot", lambda market="CN": fake)
    blend_service_module._PROFILE_CACHE.clear()
    profiles = await ModelBlendService._stock_profiles("CN")
    assert profiles["SZ000001"]["industry"] == "银行"
    assert profiles["SZ000001"]["market_cap_yi"] == pytest.approx(1000.0)
    assert profiles["SH600000"]["market_cap_yi"] == pytest.approx(2000.0)
    assert profiles["SZ000002"] == {"industry": None, "market_cap_yi": None}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stock_profiles_returns_empty_on_source_failure(monkeypatch):
    from backend.services.engine.factor_research import store

    def boom(market="CN"):
        raise RuntimeError("parquet missing")

    monkeypatch.setattr(store, "stock_snapshot", boom)
    blend_service_module._PROFILE_CACHE.clear()
    assert await ModelBlendService._stock_profiles("CN") == {}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_risk_flags_maps_st_delisted_and_ignores_nan(monkeypatch):
    import pandas as pd

    from backend.services.engine.factor_research import data as factor_data

    fake = pd.DataFrame(
        {
            "symbol": ["000001.SZ", "600000.SH", "000002.SZ", "000003.SZ"],
            "name": ["平安银行", "*ST某某", "退市某某", "正常股份"],
            # NaN 必须是「未知」而不是 True：否则整池都会被误标 ST
            "is_st": [0.0, 1.0, float("nan"), float("nan")],
            "is_quit": [0.0, float("nan"), 1.0, float("nan")],
        }
    )
    monkeypatch.setattr(factor_data, "load_instrument", lambda market="CN": fake)
    blend_service_module._RISK_CACHE.clear()
    flags, reason = await ModelBlendService._risk_flags(
        ["SZ000001", "SH600000", "SZ000002", "SZ000003"], "CN"
    )
    assert reason is None
    assert flags == {"SH600000": "ST", "SZ000002": "退市"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_risk_flags_reports_source_failure_instead_of_failing_open(monkeypatch):
    from backend.services.engine.factor_research import data as factor_data

    def boom(market="CN"):
        raise RuntimeError("parquet missing")

    monkeypatch.setattr(factor_data, "load_instrument", boom)
    blend_service_module._RISK_CACHE.clear()
    flags, reason = await ModelBlendService._risk_flags(["SZ000001"], "CN")
    assert flags == {}
    assert reason and "parquet missing" in reason

    # 非 CN 市场：明确返回「不支持」，不得伪装成「无风险标的」
    flags, reason = await ModelBlendService._risk_flags(["00700.HK"], "HK")
    assert flags == {}
    assert reason and "HK" in reason


@pytest.mark.unit
@pytest.mark.asyncio
async def test_latest_common_date_uses_min_coverage_as_required(monkeypatch):
    captured: dict = {}

    class _Result:
        def first(self):
            return (SAMPLE_DATE,)

    class _Session:
        async def execute(self, statement, params):
            captured.update(params)
            return _Result()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(
        blend_service_module, "get_session", lambda **kwargs: _Session()
    )
    found = await ModelBlendService().latest_common_date(
        tenant_id="t",
        user_id="u",
        model_ids=["a", "b", "c", "d"],
        required=2,
    )
    assert found == SAMPLE_DATE
    # 不做「全员有 run」：24 成员 T+5 组只要求 min_coverage 个
    assert captured["required"] == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_auto_candidates_filters_templates_to_runnable_models():
    """模板只列有 run 的成员（K2），缺失数原样带到模板上。"""
    models = [_model(f"m{i}", horizon=5) for i in range(4)]
    svc = _service(models)

    async def fake_runnable(*, tenant_id, user_id, model_ids):
        assert set(model_ids) == {"m0", "m1", "m2", "m3"}
        return {"m0", "m1", "m2"}

    svc._runnable_model_ids = fake_runnable
    payload = await svc.auto_candidates(tenant_id="t", user_id="u")
    full = next(i for i in payload["items"] if i["kind"] == "full_equal")
    assert [m["model_id"] for m in full["members"]] == ["m0", "m1", "m2"]
    assert full["missing_members"] == 1
    assert full["min_coverage"] == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_runnable_model_ids_reads_completed_runs(monkeypatch):
    captured: dict = {}

    class _Result:
        def all(self):
            return [("m1",), ("m2",)]

    class _Session:
        async def execute(self, statement, params):
            captured.update(params)
            return _Result()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(
        blend_service_module, "get_session", lambda **kwargs: _Session()
    )
    ids = await ModelBlendService()._runnable_model_ids(
        tenant_id="t", user_id="u", model_ids=["m1", "m2", "m3"]
    )
    assert ids == {"m1", "m2"}
    assert captured["model_ids"] == ["m1", "m2", "m3"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_runnable_model_ids_degrades_to_none_on_query_failure(monkeypatch):
    """查询失败 → None（模板不过滤），而不是空集（模板全消失）。"""

    class _Session:
        async def execute(self, statement, params):
            raise RuntimeError("db down")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(
        blend_service_module, "get_session", lambda **kwargs: _Session()
    )
    ids = await ModelBlendService()._runnable_model_ids(
        tenant_id="t", user_id="u", model_ids=["m1"]
    )
    assert ids is None


# ── 创建 / 修改 ──────────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_blend_equal_weighting_and_slugged_name():
    persistence = _FakePersistence()
    svc = _service([_model("a"), _model("b"), _model("c")], persistence)
    created = await svc.create_blend(
        tenant_id="t",
        user_id="u",
        name="  我的   组合 \x00 ",
        model_ids=["a", "b", "c"],
        min_coverage=2,
    )
    assert persistence.created is not None
    assert persistence.created["name"] == "我的 组合"
    assert persistence.created["weighting"] == "equal"
    assert persistence.created["feature_signature"] == HASH
    assert persistence.created["horizon_days"] == 10
    assert created["blend_id"] == "bl_fake"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_blend_manual_weights_and_param_gate():
    persistence = _FakePersistence()
    svc = _service([_model("a"), _model("b")], persistence)
    await svc.create_blend(
        tenant_id="t",
        user_id="u",
        name="w",
        model_ids=["a", "b"],
        min_coverage=2,
        weights={"a": 2.0, "b": 0.5},
    )
    assert persistence.created["weighting"] == "manual"
    assert persistence.created["members"] == [("a", "user", 2.0), ("b", "user", 0.5)]
    with pytest.raises(BlendError):
        await svc.create_blend(
            tenant_id="t",
            user_id="u",
            name="w2",
            model_ids=["a", "b"],
            min_coverage=3,
        )
    with pytest.raises(BlendError):
        await svc.create_blend(
            tenant_id="t", user_id="u", name="   ", model_ids=["a", "b"]
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_update_blend_recomputes_membership_and_validates_merged_gates():
    persistence = _FakePersistence()
    svc = _service([_model("a"), _model("b"), _model("c")], persistence)
    base = {
        "blend_id": "bl_x",
        "tenant_id": "t",
        "user_id": "u",
        "market": "CN",
        "purpose": "rebalance",
        "min_coverage": 2,
        "consensus_min_count": 0,
        "members": [{"model_id": "a"}, {"model_id": "b"}],
    }
    await svc.update_blend(
        base,
        tenant_id="t",
        user_id="u",
        fields={"name": " 新 名 ", "min_coverage": 3},
        model_ids=["a", "b", "c"],
        weights=None,
    )
    assert persistence.updated["fields"] == {
        "name": "新 名",
        "min_coverage": 3,
        "weighting": "equal",
        "feature_signature": HASH,
        "data_source": "quantdb_factors",
        "horizon_days": 10,
    }
    with pytest.raises(BlendError):
        await svc.update_blend(
            base,
            tenant_id="t",
            user_id="u",
            fields={"min_coverage": 9},
            model_ids=None,
            weights=None,
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_update_blend_rejects_weights_without_members():
    """只改权重不带成员时不能静默成功（否则写入被丢弃、返回 200）。"""
    persistence = _FakePersistence()
    svc = _service([_model("a"), _model("b")], persistence)
    base = {
        "blend_id": "bl_x",
        "tenant_id": "t",
        "user_id": "u",
        "market": "CN",
        "purpose": "rebalance",
        "members": [{"model_id": "a"}, {"model_id": "b"}],
    }
    with pytest.raises(BlendError) as exc:
        await svc.update_blend(
            base,
            tenant_id="t",
            user_id="u",
            fields={},
            model_ids=None,
            weights={"a": 2.0},
        )
    assert exc.value.code == "weights_require_members"
    assert persistence.updated is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_update_blend_rechecks_horizon_when_purpose_becomes_rebalance():
    """改用途也要过「同周期」校验——否则存量混周期成员绕过 _resolve_members。"""
    persistence = _FakePersistence()
    svc = _service([_model("a"), _model("a5", horizon=5)], persistence)
    base = {
        "blend_id": "bl_x",
        "tenant_id": "t",
        "user_id": "u",
        "market": "CN",
        "purpose": "next_day",
        "members": [{"model_id": "a"}, {"model_id": "a5"}],
    }
    with pytest.raises(BlendError) as exc:
        await svc.update_blend(
            base,
            tenant_id="t",
            user_id="u",
            fields={"purpose": "rebalance"},
            model_ids=None,
            weights=None,
        )
    assert exc.value.code == "mixed_horizon"
    assert persistence.updated is None

    # 改回 next_day 允许跨周期，正常落库
    await svc.update_blend(
        base,
        tenant_id="t",
        user_id="u",
        fields={"purpose": "next_day"},
        model_ids=None,
        weights=None,
    )
    assert persistence.updated["fields"]["purpose"] == "next_day"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_update_blend_allows_uniform_t5_purpose():
    persistence = _FakePersistence()
    svc = _service([_model("a5", horizon=5), _model("b5", horizon=5)], persistence)
    base = {
        "blend_id": "bl_5",
        "tenant_id": "t",
        "user_id": "u",
        "market": "CN",
        "purpose": "next_day",
        "members": [{"model_id": "a5"}, {"model_id": "b5"}],
    }
    await svc.update_blend(
        base,
        tenant_id="t",
        user_id="u",
        fields={"purpose": "rebalance"},
        model_ids=None,
        weights=None,
    )
    assert persistence.updated["fields"]["purpose"] == "rebalance"


# ── 预览 ─────────────────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preview_defaults_latest_common_date_and_warns_on_mixed_horizon():
    svc = _service([_model("a", horizon=10), _model("b", horizon=5)])
    calls: dict = {}

    async def fake_latest(**kwargs):
        return SAMPLE_DATE

    async def fake_ranking(**kwargs):
        calls.update(kwargs)
        return {
            "ranking": [{"symbol": "SZ000001"}, {"symbol": "SH600000"}],
            "warnings": [],
            "members": [],
            "stats": {},
        }

    async def fake_next(day):
        return date(2026, 10, 20)

    svc.latest_common_date = fake_latest
    svc.ranking = fake_ranking
    svc._next_trading_day = fake_next
    payload = await svc.preview(
        tenant_id="t",
        user_id="u",
        model_ids=["a", "b"],
        purpose="next_day",
        top=1,
        top_k=50,
    )
    assert payload["data_trade_date"] == SAMPLE_DATE.isoformat()
    assert payload["target_date"] == "2026-10-20"
    assert payload["purpose"] == "next_day"
    assert payload["signature"]["factor_schema_hash"] == HASH
    assert payload["warnings"] == ["mixed_horizon"]
    assert len(payload["ranking"]) == 1
    assert calls["data_trade_date"] == SAMPLE_DATE


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preview_rebalance_has_no_mixed_warning():
    svc = _service([_model("a"), _model("b", model_type="xgboost")])

    async def fake_latest(**kwargs):
        return SAMPLE_DATE

    async def fake_ranking(**kwargs):
        return {"ranking": [], "warnings": [], "members": [], "stats": {}}

    async def fake_next(day):
        return date(2026, 10, 20)

    svc.latest_common_date = fake_latest
    svc.ranking = fake_ranking
    svc._next_trading_day = fake_next
    payload = await svc.preview(
        tenant_id="t", user_id="u", model_ids=["a", "b"], purpose="rebalance"
    )
    assert "mixed_horizon" not in payload["warnings"]


# ── 历史标注 / 确认闸门 ──────────────────────────────────────────────────


class _AnnotationPersistence(_FakePersistence):
    def __init__(self, positions):
        super().__init__()
        self.positions = positions

    async def list_position_history(self, blend_id):
        return self.positions


@pytest.mark.unit
@pytest.mark.asyncio
async def test_annotations_reentry_hold_days_and_consecutive_top():
    positions = [
        {
            "symbol": "SZ000001",
            "entry_date": "2026-10-01",
            "exit_date": "2026-10-10",
            "shares": 100,
        },
        {
            "symbol": "SZ000001",
            "entry_date": "2026-10-15",
            "exit_date": None,
            "shares": 200,
        },
    ]
    svc = ModelBlendService(persistence=_AnnotationPersistence(positions))

    async def fake_days(end, *, need):
        return [date(2026, 10, day) for day in range(12, 20)]

    svc._trading_days = fake_days
    window = {
        "top": {
            date(2026, 10, 18): {"SZ000001"},
            date(2026, 10, 19): {"SZ000001"},
            date(2026, 10, 17): {"SZ000001"},
        }
    }
    out = await svc._annotations(
        blend_id="bl_x",
        rows=[{"symbol": "SZ000001"}, {"symbol": "SH600000"}],
        window=window,
        data_trade_date=SAMPLE_DATE,
    )
    assert out["SZ000001"]["first_entry_date"] == "2026-10-01"
    assert out["SZ000001"]["last_entry_date"] == "2026-10-15"
    assert out["SZ000001"]["reentry_count"] == 1
    assert out["SZ000001"]["held_days"] == 4
    assert out["SZ000001"]["consecutive_days_in_top50"] == 3
    assert out["SH600000"] == {
        "first_entry_date": None,
        "last_entry_date": None,
        "reentry_count": 0,
        "held_days": 0,
        "consecutive_days_in_top50": 0,
    }


class _ConfirmPersistence(_FakePersistence):
    def __init__(self, positions=None, *, refreshed=None, conflict=None):
        super().__init__()
        self.positions = positions or []
        self.refreshed = refreshed or {}
        self.conflict = conflict
        self.confirmed = None

    async def list_open_positions(self, blend_id):
        return self.positions

    async def confirm_plan(self, *, plan, target_date):
        if self.conflict:
            raise RuntimeError(self.conflict)
        self.confirmed = (plan, target_date)

    async def get_plan(self, plan_id, *, tenant_id, user_id):
        return self.refreshed


def _plan(day: str = "2026-10-19", **overrides) -> dict:
    payload = {
        "plan_id": "bp_1",
        "blend_id": "bl_x",
        "data_trade_date": day,
        "target_date": "2026-10-20",
        "positions_hash": positions_hash([]),
        "confirmed_at": None,
        "entries": [],
        "exits": [],
    }
    payload.update(overrides)
    return payload


@pytest.mark.unit
@pytest.mark.asyncio
async def test_annotate_confirmable_marks_window_and_rejects_terminal_plans():
    svc = _service([])

    async def recent():
        return {date(2026, 10, 20), date(2026, 10, 19)}

    svc._recent_trading_days = recent
    plans = [
        {"plan_id": "p1", "data_trade_date": "2026-10-20", "confirmed_at": None},
        {"plan_id": "p2", "data_trade_date": "2026-10-16", "confirmed_at": None},
        {"plan_id": None, "data_trade_date": "2026-10-20", "confirmed_at": None},
        {
            "plan_id": "p3",
            "data_trade_date": "2026-10-19",
            "confirmed_at": "2026-10-19T08:00:00Z",
        },
        None,
    ]
    await svc.annotate_confirmable(plans)
    assert [p and p.get("can_confirm") for p in plans] == [
        True,
        False,
        False,
        False,
        None,
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_annotate_confirmable_blocks_data_degraded_plans():
    svc = _service([])

    async def recent():
        return {date(2026, 10, 19)}

    svc._recent_trading_days = recent
    plans = [
        {"plan_id": "p1", "data_trade_date": "2026-10-19", "data_ok": False},
        {
            "plan_id": "p2",
            "data_trade_date": "2026-10-19",
            "summary": {"data_ok": False},
        },
        {"plan_id": "p3", "data_trade_date": "2026-10-19", "summary": {}},
    ]
    await svc.annotate_confirmable(plans)
    assert [p.get("can_confirm") for p in plans] == [False, False, True]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_confirm_plan_rejects_data_degraded_plan():
    svc = _service([_model("a"), _model("b")])
    with pytest.raises(BlendError) as exc:
        await svc.confirm_plan(
            {"blend_id": "bl_x"},
            {
                "plan_id": "p1",
                "data_trade_date": SAMPLE_DATE.isoformat(),
                "target_date": "2026-10-20",
                "confirmed_at": None,
                "summary": {"data_ok": False},
            },
            tenant_id="t",
            user_id="u",
        )
    assert exc.value.code == "plan_data_degraded"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_confirm_plan_rejects_already_confirmed_and_expired():
    svc = _service([_model("a"), _model("b")], _ConfirmPersistence())
    with pytest.raises(BlendError) as exc:
        await svc.confirm_plan(
            {"blend_id": "bl_x"},
            _plan(confirmed_at="2026-10-19T00:00:00Z"),
            tenant_id="t",
            user_id="u",
        )
    assert exc.value.code == "plan_confirmed"

    async def recent():
        return {SAMPLE_DATE}

    svc._recent_trading_days = recent
    with pytest.raises(BlendError) as exc:
        await svc.confirm_plan(
            {"blend_id": "bl_x"}, _plan(day="2026-10-18"), tenant_id="t", user_id="u"
        )
    assert exc.value.code == "plan_expired"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_confirm_plan_rejects_stale_positions_and_maps_unique_conflict():
    existing = [{"symbol": "SH600000", "entry_date": "2026-09-01", "shares": 100}]
    svc = _service([_model("a"), _model("b")], _ConfirmPersistence(existing))

    async def recent():
        return {SAMPLE_DATE}

    svc._recent_trading_days = recent
    with pytest.raises(BlendError) as exc:
        await svc.confirm_plan(
            {"blend_id": "bl_x"}, _plan(), tenant_id="t", user_id="u"
        )
    assert exc.value.code == "stale_positions"

    persistence = _ConfirmPersistence(
        conflict="duplicate key value violates unique constraint uq_blend_plan_confirmed"
    )
    svc2 = _service([_model("a"), _model("b")], persistence)
    svc2._recent_trading_days = recent
    with pytest.raises(BlendError) as exc:
        await svc2.confirm_plan(
            {"blend_id": "bl_x"}, _plan(), tenant_id="t", user_id="u"
        )
    assert exc.value.code == "plan_confirmed"
    assert exc.value.status_code == 409


@pytest.mark.unit
@pytest.mark.asyncio
async def test_confirm_plan_success_passes_target_date_and_returns_refreshed():
    refreshed = {"plan_id": "bp_1", "confirmed_at": "2026-10-19T08:00:00Z"}
    persistence = _ConfirmPersistence(refreshed=refreshed)
    svc = _service([_model("a"), _model("b")], persistence)

    async def recent():
        return {SAMPLE_DATE}

    svc._recent_trading_days = recent
    result = await svc.confirm_plan(
        {"blend_id": "bl_x"}, _plan(), tenant_id="t", user_id="u"
    )
    assert persistence.confirmed[1] == date(2026, 10, 20)
    assert result == refreshed


# ── 真库：符号折叠 ───────────────────────────────────────────────────────


async def _probe_db() -> None:
    from backend.shared.database_manager_v2 import get_session

    try:
        async with get_session(read_only=True) as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"DB 不可用: {exc}")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_prefix_symbol_sql_matches_stock_code_util():
    from backend.shared.database_manager_v2 import get_session

    await _probe_db()
    cases = [
        "600519",
        "000001",
        "688981",
        "300750",
        "900901",
        "200001",
        "830799",
        "430047",
        "920001",
        "600519.SH",
        "000001.SZ",
        "SH600519",
        "SZ000001",
        "SH.600519",
        "sz000001",
        "  sh600519  ",
        "00700.HK",
        "AAPL",
    ]
    values = ", ".join(f"(:v{i})" for i in range(len(cases)))
    sql = "SELECT v, " + _prefix_symbol_sql("v") + f" FROM (VALUES {values}) AS t(v)"
    params = {f"v{i}": value for i, value in enumerate(cases)}
    async with get_session(read_only=True) as session:
        rows = (await session.execute(text(sql), params)).all()
    got = {str(raw): str(folded) for raw, folded in rows}
    for raw in cases:
        assert got[raw] == StockCodeUtil.to_prefix(raw), raw


# ── 滚动建仓视图（P5） ───────────────────────────────────────────────────


def _roll_row(symbol: str, rank: int) -> dict:
    return {
        "symbol": symbol,
        "score": 1.0 - rank * 0.01,
        "rank": rank,
        "row_rank": rank,
        "coverage": 2,
        "pct": 1.0 - rank * 0.01,
        "position": "BUY",
    }


def _rolling_service(rows_by_day, *, models=None, closes=None, profiles=None):
    """替身窗口：只喂表头三列（符号/分数/名次），其余口径由真库用例覆盖。"""
    members = models or [_model("a"), _model("b")]
    svc = _service(members)
    calls: dict = {}

    async def fake_window_stats(**kwargs):
        calls["window_stats"] = kwargs
        return {"scores": {}, "top": {}, "top_rows": rows_by_day, "days": []}

    async def fake_names():
        return {"SH600000": "浦发银行", "SZ000001": "平安银行"}

    async def fake_profiles(_market):
        return dict(profiles or {})

    async def fake_closes(symbols, start, end):
        calls["close_history"] = {"symbols": list(symbols), "start": start, "end": end}
        return dict(closes or {})

    async def fake_common_date(**_kwargs):
        calls["common_date"] = True
        return D_ROLL

    async def fake_next(day):
        return date.fromordinal(day.toordinal() + 1)

    async def fake_members(_blend):
        return list(members)

    svc._window_stats = fake_window_stats
    svc._stock_names = fake_names
    svc._stock_profiles = fake_profiles
    svc._close_history = fake_closes
    svc.latest_common_date = fake_common_date
    svc._next_trading_day = fake_next
    svc.members_from_blend = fake_members
    return svc, calls


D_ROLL = date(2026, 10, 19)


def _roll_days(*offsets: int) -> list[date]:
    return [date.fromordinal(D_ROLL.toordinal() - offset) for offset in offsets]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rolling_builds_streaks_new_flags_and_exits():
    d0, d1, d2 = _roll_days(2, 1, 0)
    rows = {
        d0: [_roll_row("SH600000", 1), _roll_row("SZ000001", 2)],
        d1: [_roll_row("SZ000001", 1), _roll_row("SZ000002", 2)],
        d2: [
            _roll_row("SH600000", 1),
            _roll_row("SZ000001", 2),
            _roll_row("SZ000003", 3),
            _roll_row("SZ000004", 4),
        ],
    }
    svc, calls = _rolling_service(rows)
    payload = await svc.rolling(
        {"blend_id": "bl_x", "members": []},
        tenant_id="t",
        user_id="u",
        window=10,
        per_day=5,
    )

    assert [column["date"] for column in payload["columns"]] == [
        d0.isoformat(),
        d1.isoformat(),
        d2.isoformat(),
    ]
    by_day = {column["date"]: column for column in payload["columns"]}
    assert [pick["symbol"] for pick in by_day[d2.isoformat()]["picks"]] == [
        "SH600000",
        "SZ000001",
        "SZ000003",
        "SZ000004",
    ]
    # 连续在榜 +1；断榜后归零（d1 掉出，d2 再入选 = 新进）
    held = {
        (column["date"], pick["symbol"]): pick["held_days"]
        for column in payload["columns"]
        for pick in column["picks"]
    }
    assert held[(d0.isoformat(), "SH600000")] == 1
    assert held[(d2.isoformat(), "SH600000")] == 1
    assert held[(d2.isoformat(), "SZ000001")] == 3
    assert held[(d2.isoformat(), "SZ000003")] == 1
    assert by_day[d0.isoformat()]["picks"][0]["is_new"] is True
    assert by_day[d0.isoformat()]["picks"][0]["name"] == "浦发银行"
    assert by_day[d1.isoformat()]["picks"][0]["is_new"] is False
    # 退出只在「昨列 → 今日」标注一次
    assert by_day[d0.isoformat()]["exits"] == []
    assert by_day[d1.isoformat()]["exits"] == ["SH600000"]
    assert by_day[d2.isoformat()]["exits"] == ["SZ000002"]
    summary = payload["summary"]
    assert summary["unique_symbols"] == 5
    assert summary["new_today"] == 3
    assert summary["exited_today"] == 1
    # 无行情时损益如实缺失（不写 0），但笔数按退出事件计数
    assert summary["performance"]["entry_convention"] == "close"
    assert summary["performance"]["closed_trades"] == 2
    assert summary["performance"]["priced_trades"] == 0
    assert summary["performance"]["win_rate_pct"] is None
    assert by_day[d1.isoformat()]["exit_rows"] == [
        {
            "symbol": "SH600000",
            "code": "SH600000",
            "name": "浦发银行",
            "industry": None,
            "market_cap_yi": None,
            "entry_date": d0.isoformat(),
            "exit_date": d1.isoformat(),
            "entry_price": None,
            "exit_price": None,
            "return_pct": None,
            "held_days": 1,
            "entry_from_window_start": True,
        }
    ]
    assert payload["data_trade_date"] == D_ROLL.isoformat()
    assert (
        payload["target_date"] == date.fromordinal(D_ROLL.toordinal() + 1).isoformat()
    )
    assert calls["window_stats"]["end_date"] == D_ROLL
    assert calls["window_stats"]["lookback"] == 10
    assert calls["window_stats"]["top_k"] == 5
    assert calls["window_stats"]["symbols"] == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rolling_exit_rows_carry_close_based_pnl_and_profile():
    """退出行 = 收盘价买入/卖出损益 + 名称/行业/市值，供前端弹窗直接展示。"""
    d0, d1 = _roll_days(1, 0)
    rows = {
        d0: [_roll_row("SH600000", 1), _roll_row("SZ000001", 2)],
        d1: [_roll_row("SH600000", 1)],
    }
    closes = {
        "SH600000": {d0: 10.0, d1: 11.0},
        "SZ000001": {d0: 20.0, d1: 18.0},
    }
    profiles = {"SZ000001": {"industry": "银行", "market_cap_yi": 2200.0}}
    svc, calls = _rolling_service(rows, closes=closes, profiles=profiles)

    payload = await svc.rolling(
        {"blend_id": "bl_x"}, tenant_id="t", user_id="u", window=10, per_day=5
    )

    column = payload["columns"][-1]
    assert column["exits"] == ["SZ000001"]
    assert len(column["exit_rows"]) == 1
    exit_row = column["exit_rows"][0]
    assert exit_row["symbol"] == "SZ000001"
    assert exit_row["name"] == "平安银行"
    assert exit_row["industry"] == "银行"
    assert exit_row["market_cap_yi"] == pytest.approx(2200.0)
    assert exit_row["entry_price"] == pytest.approx(20.0)
    assert exit_row["exit_price"] == pytest.approx(18.0)
    assert exit_row["return_pct"] == pytest.approx(-10.0)
    assert exit_row["held_days"] == 1
    assert exit_row["entry_from_window_start"] is True

    perf = payload["summary"]["performance"]
    assert perf["closed_trades"] == 1
    assert perf["wins"] == 0
    assert perf["losses"] == 1
    assert perf["win_rate_pct"] == pytest.approx(0.0)
    assert perf["avg_return_pct"] == pytest.approx(-10.0)
    assert perf["open_positions"] == 1
    assert perf["open_avg_return_pct"] == pytest.approx(10.0)
    assert calls["close_history"]["symbols"] == ["SH600000", "SZ000001"]
    assert calls["close_history"]["start"] == d0
    assert calls["close_history"]["end"] == d1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rolling_reentry_after_gap_is_new_not_continued():
    d0, d1, d2 = _roll_days(2, 1, 0)
    rows = {
        d0: [_roll_row("SH600000", 1)],
        d1: [_roll_row("SZ000001", 1)],
        d2: [_roll_row("SH600000", 1)],
    }
    svc, _ = _rolling_service(rows)
    payload = await svc.rolling(
        {"blend_id": "bl_x"}, tenant_id="t", user_id="u", window=10, per_day=5
    )
    last = payload["columns"][-1]["picks"][0]
    assert last["symbol"] == "SH600000"
    assert last["held_days"] == 1
    assert last["is_new"] is True
    assert payload["columns"][1]["exits"] == ["SH600000"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rolling_caps_columns_and_picks_per_day():
    days = [
        date.fromordinal(D_ROLL.toordinal() - offset) for offset in range(5, -1, -1)
    ]
    rows = {
        day: [_roll_row(f"SH60000{index}", index + 1) for index in range(4)]
        for index, day in enumerate(days)
    }
    svc, _ = _rolling_service(rows)
    payload = await svc.rolling(
        {"blend_id": "bl_x"}, tenant_id="t", user_id="u", window=2, per_day=2
    )
    assert [column["date"] for column in payload["columns"]] == [
        day.isoformat() for day in days[-3:]
    ]
    assert all(len(column["picks"]) == 2 for column in payload["columns"])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rolling_uses_explicit_end_date_without_lookup():
    rows = {D_ROLL: [_roll_row("SH600000", 1)]}
    svc, calls = _rolling_service(rows)
    payload = await svc.rolling(
        {"blend_id": "bl_x"},
        tenant_id="t",
        user_id="u",
        end_date=D_ROLL,
        window=10,
        per_day=5,
    )
    assert "common_date" not in calls
    assert payload["data_trade_date"] == D_ROLL.isoformat()


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(("window", "per_day"), [(0, 5), (61, 5), (10, 0), (10, 51)])
async def test_rolling_rejects_out_of_range_window(window, per_day):
    svc, _ = _rolling_service({})
    with pytest.raises(BlendError):
        await svc.rolling(
            {"blend_id": "bl_x"},
            tenant_id="t",
            user_id="u",
            window=window,
            per_day=per_day,
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rolling_without_common_date_raises_business_code():
    svc, _rolling_calls = _rolling_service({})

    async def no_date(**_kwargs):
        return None

    svc.latest_common_date = no_date
    with pytest.raises(BlendError) as exc:
        await svc.rolling({"blend_id": "bl_x"}, tenant_id="t", user_id="u")
    assert exc.value.code == "no_common_date"


# ── 真库：榜单 / 窗口 / 计划 ─────────────────────────────────────────────


async def _real_t10_members(limit: int = 3) -> list[dict]:
    svc = ModelBlendService()
    models = await svc._normalized_models(tenant_id="default", user_id="10000001")
    t10 = [
        m
        for m in models
        if m["target_horizon_days"] == 10 and m["feature_count"] == 300
    ]
    t10.sort(key=lambda m: m["model_id"])
    return t10[:limit]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ranking_real_models_is_prefix_folded_with_contributions():
    await _probe_db()
    svc = ModelBlendService()
    members = await _real_t10_members(3)
    assert len(members) == 3
    payload = await svc.ranking(
        tenant_id="default",
        user_id="10000001",
        members=members,
        data_trade_date=SAMPLE_DATE,
        min_coverage=2,
        top=20,
    )
    assert payload["data_trade_date"] == SAMPLE_DATE.isoformat()
    assert [m["available"] for m in payload["members"]] == [True, True, True]
    rows = payload["ranking"]
    assert rows, "真实模型在 2026-10-19 无榜单"
    for row in rows:
        assert row["symbol"] == row["code"]
        assert row["symbol"][:2] in {"SH", "SZ", "BJ"}
        assert row["symbol"][2:].isdigit() and len(row["symbol"]) == 8
        assert 0.0 <= row["pct"] <= 1.0
        assert row["coverage"] >= 2
        assert row["position"] in {"BUY", "HOLD", "SELL"}
        assert sum(row["contributions"].values()) == pytest.approx(row["score"])
    scores = [row["score"] for row in rows]
    assert scores == sorted(scores, reverse=True)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ranking_annotate_attaches_history_and_top50_flag():
    await _probe_db()
    svc = ModelBlendService()
    members = await _real_t10_members(2)
    payload = await svc.ranking(
        tenant_id="default",
        user_id="10000001",
        members=members,
        data_trade_date=SAMPLE_DATE,
        min_coverage=2,
        top=5,
        annotate=True,
    )
    rows = payload["ranking"]
    assert rows
    for row in rows:
        history = row["history"]
        assert set(history) == {
            "first_entry_date",
            "last_entry_date",
            "reentry_count",
            "held_days",
            "consecutive_days_in_top50",
        }
        # 未确认过任何计划 → 无持仓历史，但 Top50 连续天数应为正
        assert history["first_entry_date"] is None
        assert history["consecutive_days_in_top50"] >= 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_window_stats_respects_lookback_and_excludes_end_date():
    await _probe_db()
    svc = ModelBlendService()
    members = await _real_t10_members(2)
    window = await svc._window_stats(
        tenant_id="default",
        user_id="10000001",
        members=members,
        weights=None,
        method="mean",
        trim_count=2,
        min_coverage=2,
        consensus_min_count=0,
        end_date=SAMPLE_DATE,
        lookback=10,
        top_k=50,
        symbols=["SH600000", "SZ000001"],
    )
    assert window["days"]
    assert len(window["days"]) <= 10
    assert all(day < SAMPLE_DATE for day in window["days"])
    assert all(day <= SAMPLE_DATE for day in window["top"])
    for day_scores in window["scores"].values():
        assert all(0.0 <= value <= 1.0 for value in day_scores.values())
    assert window["top"], "窗口内 TopK 集合为空"


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["median", "trimmed"])
async def test_window_stats_alternative_methods_return_bounded_scores(method):
    await _probe_db()
    svc = ModelBlendService()
    members = await _real_t10_members(3)
    window = await svc._window_stats(
        tenant_id="default",
        user_id="10000001",
        members=members,
        weights=None,
        method=method,
        trim_count=1,
        min_coverage=2,
        consensus_min_count=0,
        end_date=SAMPLE_DATE,
        lookback=5,
        top_k=20,
        symbols=["SH600000"],
    )
    assert window["days"]
    for day_scores in window["scores"].values():
        assert all(0.0 <= value <= 1.0 for value in day_scores.values())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_plan_readonly_real_data_has_no_order_semantics_and_budget():
    await _probe_db()
    svc = ModelBlendService()
    members = await _real_t10_members(2)
    blend = {
        "blend_id": "bl_unittest_plan",
        "tenant_id": "default",
        "user_id": "10000001",
        "name": "unittest",
        "market": "CN",
        "purpose": "rebalance",
        "method": "mean",
        "weighting": "equal",
        "min_coverage": 2,
        "consensus_min_count": 0,
        "trim_count": 2,
        "entry_per_day": 5,
        "rolling_window": 10,
        "target_positions": 50,
        "horizon_days": 10,
        "members": [
            {"model_id": m["model_id"], "model_source": "user", "weight": 1.0}
            for m in members
        ],
    }
    started = time.monotonic()
    plan = await svc.plan(
        blend,
        tenant_id="default",
        user_id="10000001",
        data_trade_date=SAMPLE_DATE,
        persist=False,
    )
    elapsed = time.monotonic() - started
    assert elapsed < 15, f"计划耗时 {elapsed:.1f}s 超出 P95 预算"
    assert plan["plan_id"] is None
    assert plan["positions_hash"]
    assert plan["config_hash"]
    assert plan["data_trade_date"] == SAMPLE_DATE.isoformat()
    assert plan["target_date"] > plan["data_trade_date"]
    summary = plan["summary"]
    assert summary["n_entries"] == len(plan["entries"]) <= 5
    assert summary["n_exits"] == len(plan["exits"])
    assert summary["n_holds"] == len(plan["holds"])
    for entry in plan["entries"]:
        assert entry["shares"] % 100 == 0 and entry["shares"] > 0
        assert entry["ref_price"] > 0
        assert entry["target_value"] > 0
        assert "side" not in entry and "order_id" not in entry
        # 机构口径：能新进就必然校验过 ADV20（缺失/不足会被拦下）
        assert entry["adv_yi"] is not None and entry["adv_yi"] > 0
        # 展示面字段随行下发（缺失允许 None，但键必须存在）
        assert "name" in entry and "industry" in entry and "market_cap_yi" in entry
    assert isinstance(plan["warnings"], list)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_quotes_adv_requires_min_valid_days(monkeypatch):
    """ADV20 只算最近 20 个有成交额的交易日；有效日不足 min_adv_days 不进映射。"""
    import pandas as pd

    from backend.services.engine.data_platform import quantdb_hub

    rows = []
    # 600000.SH：14 个有效日（<15）→ 不产出 ADV，但当日参考价仍可用
    for i in range(14):
        rows.append(
            {
                "symbol": "600000.SH",
                "trade_date": SAMPLE_DATE - timedelta(days=13 - i),
                "close": 10.0,
                "amount": 10_000.0,  # 万元
            }
        )
    # 000001.SZ：15 个有效日 → 产出 ADV（×10000 转元）
    for i in range(15):
        rows.append(
            {
                "symbol": "000001.SZ",
                "trade_date": SAMPLE_DATE - timedelta(days=14 - i),
                "close": 12.0,
                "amount": 20_000.0,
            }
        )
    fake = pd.DataFrame(rows)

    def fake_fetch(self, symbols, start, end, adjust="none"):
        return fake

    monkeypatch.setattr(
        quantdb_hub.QuantDBDataHub, "fetch_daily_kline_batch", fake_fetch
    )
    prices, amounts, warnings = await ModelBlendService._quotes(
        ["SH600000", "SZ000001"], SAMPLE_DATE, lookback_days=20, min_adv_days=15
    )
    assert warnings == []
    assert prices["SH600000"] == pytest.approx(10.0)
    assert prices["SZ000001"] == pytest.approx(12.0)
    assert "SH600000" not in amounts
    assert amounts["SZ000001"] == pytest.approx(20_000.0 * 10_000.0)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_quotes_amount_is_yuan_after_unit_conversion():
    await _probe_db()
    prices, amounts, warnings = await ModelBlendService._quotes(
        ["SH600000", "SZ000001"], SAMPLE_DATE
    )
    assert not warnings, warnings
    assert prices.get("SH600000", 0) > 0
    assert prices.get("SZ000001", 0) > 0
    # QuantDB daily_unadjusted.amount 单位为万元；转换后应是「元」量级。
    assert amounts.get("SZ000001", 0) > 1e8


@pytest.mark.integration
@pytest.mark.asyncio
async def test_available_dates_real_models_all_present():
    await _probe_db()
    svc = ModelBlendService()
    members = await _real_t10_members(2)
    blend = {
        "tenant_id": "default",
        "user_id": "10000001",
        "members": [{"model_id": m["model_id"]} for m in members],
    }
    dates = await svc.available_dates(
        blend, tenant_id="default", user_id="10000001", limit=10
    )
    assert dates
    assert dates == sorted(dates, reverse=True)
    assert len(dates) <= 10
    assert all(date.fromisoformat(item) for item in dates)


# ── 真库：纸面台账全生命周期 ─────────────────────────────────────────────


async def _cleanup_blends() -> None:
    from backend.shared.database_manager_v2 import get_session

    async with get_session() as session:
        await session.execute(
            text("DELETE FROM qm_model_blends WHERE tenant_id = :tenant_id"),
            {"tenant_id": TEST_TENANT},
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_persistence_lifecycle_plan_confirm_archive():
    await _probe_db()
    persistence = ModelBlendPersistence()
    await persistence.ensure_tables()
    await _cleanup_blends()
    name = f"unittest-{uuid.uuid4().hex[:8]}"
    try:
        blend = await persistence.create_blend(
            tenant_id=TEST_TENANT,
            user_id=TEST_USER,
            name=name,
            market="CN",
            purpose="rebalance",
            source="manual",
            template_id=None,
            horizon_days=10,
            weighting="equal",
            method="mean",
            trim_count=2,
            min_coverage=2,
            consensus_min_count=0,
            entry_per_day=5,
            rolling_window=10,
            target_positions=50,
            feature_signature=HASH,
            data_source="quantdb_factors",
            members=[("m1", "user", 1.0), ("m2", "user", 1.0)],
        )
        blend_id = blend["blend_id"]
        assert blend_id.startswith("bl_")
        assert blend["members"] == [
            {"model_id": "m1", "model_source": "user", "weight": 1.0},
            {"model_id": "m2", "model_source": "user", "weight": 1.0},
        ]

        listed = await persistence.list_blends(tenant_id=TEST_TENANT, user_id=TEST_USER)
        assert [row["blend_id"] for row in listed] == [blend_id]

        await persistence.update_blend(
            blend_id,
            tenant_id=TEST_TENANT,
            user_id=TEST_USER,
            fields={"name": f"{name}-v2", "min_coverage": 2},
            members=[("m1", "user", 2.0), ("m3", "user", 1.0)],
        )
        refreshed = await persistence.get_blend(
            blend_id, tenant_id=TEST_TENANT, user_id=TEST_USER
        )
        assert refreshed["name"] == f"{name}-v2"
        assert [m["model_id"] for m in refreshed["members"]] == ["m1", "m3"]

        # 先有一笔 open 持仓，计划里把它列为平仓
        from backend.shared.database_manager_v2 import get_session

        async with get_session() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO qm_model_blend_positions
                      (blend_id, symbol, entry_date, shares, status)
                    VALUES (:blend_id, 'SH600000', :entry_date, 100, 'open')
                    """
                ),
                {"blend_id": blend_id, "entry_date": date(2026, 9, 1)},
            )

        plan = await persistence.upsert_plan(
            blend_id=blend_id,
            tenant_id=TEST_TENANT,
            user_id=TEST_USER,
            data_trade_date=SAMPLE_DATE,
            target_date=date(2026, 10, 20),
            config_hash="cfg-1",
            positions_hash="pos-1",
            params={"capital": 1e7},
            summary={"n_entries": 1, "n_exits": 1},
            entries=[
                {
                    "symbol": "SZ000001",
                    "shares": 200,
                    "ref_price": 10.0,
                    "target_value": 2000.0,
                    "score": 0.9,
                }
            ],
            holds=[],
            exits=[
                {"symbol": "SH600000", "exit_reason": "到期"},
            ],
        )
        plan_id = plan["plan_id"]
        assert plan_id.startswith("bp_")
        assert plan["entries"][0]["symbol"] == "SZ000001"

        # 同配置重复生成 → 幂等（同一 plan_id）
        again = await persistence.upsert_plan(
            blend_id=blend_id,
            tenant_id=TEST_TENANT,
            user_id=TEST_USER,
            data_trade_date=SAMPLE_DATE,
            target_date=date(2026, 10, 20),
            config_hash="cfg-1",
            positions_hash="pos-1",
            params={},
            summary={},
            entries=[],
            holds=[],
            exits=[],
        )
        assert again["plan_id"] == plan_id

        assert [p["plan_id"] for p in await persistence.list_plans(blend_id)] == [
            plan_id
        ]
        by_date = await persistence.list_plans(blend_id, data_trade_date=SAMPLE_DATE)
        assert [p["plan_id"] for p in by_date] == [plan_id]
        assert (
            await persistence.list_plans(blend_id, data_trade_date=date(2000, 1, 1))
            == []
        )

        await persistence.confirm_plan(plan=plan, target_date=date(2026, 10, 20))
        confirmed = await persistence.get_plan(
            plan_id, tenant_id=TEST_TENANT, user_id=TEST_USER
        )
        assert confirmed["confirmed_at"]
        open_positions = await persistence.list_open_positions(blend_id)
        assert [p["symbol"] for p in open_positions] == ["SZ000001"]
        history = await persistence.list_position_history(blend_id)
        closed = [p for p in history if p["symbol"] == "SH600000"]
        assert closed and closed[0]["status"] == "closed"
        assert closed[0]["exit_reason"] == "到期"

        # 已确认版本不被同配置再生成覆盖
        kept = await persistence.upsert_plan(
            blend_id=blend_id,
            tenant_id=TEST_TENANT,
            user_id=TEST_USER,
            data_trade_date=SAMPLE_DATE,
            target_date=date(2026, 10, 20),
            config_hash="cfg-1",
            positions_hash="pos-2",
            params={},
            summary={},
            entries=[],
            holds=[],
            exits=[],
        )
        assert kept["plan_id"] == plan_id
        assert kept["positions_hash"] == "pos-1"

        await persistence.archive_blend(
            blend_id, tenant_id=TEST_TENANT, user_id=TEST_USER
        )
        archived = await persistence.get_blend(
            blend_id, tenant_id=TEST_TENANT, user_id=TEST_USER
        )
        assert archived["status"] == "archived"
        assert (
            await persistence.list_blends(tenant_id=TEST_TENANT, user_id=TEST_USER)
            == []
        )
        assert (
            len(
                await persistence.list_blends(
                    tenant_id=TEST_TENANT, user_id=TEST_USER, include_archived=True
                )
            )
            == 1
        )
    finally:
        await _cleanup_blends()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_service_confirm_plan_real_ledger_open_and_duplicate():
    from backend.shared.database_manager_v2 import get_session

    await _probe_db()
    persistence = ModelBlendPersistence()
    await persistence.ensure_tables()
    await _cleanup_blends()
    name = f"unittest-confirm-{uuid.uuid4().hex[:8]}"
    try:
        blend = await persistence.create_blend(
            tenant_id=TEST_TENANT,
            user_id=TEST_USER,
            name=name,
            market="CN",
            purpose="rebalance",
            source="manual",
            template_id=None,
            horizon_days=10,
            weighting="equal",
            method="mean",
            trim_count=2,
            min_coverage=2,
            consensus_min_count=0,
            entry_per_day=5,
            rolling_window=10,
            target_positions=50,
            feature_signature=HASH,
            data_source="quantdb_factors",
            members=[("m1", "user", 1.0), ("m2", "user", 1.0)],
        )
        blend_id = blend["blend_id"]
        plan = await persistence.upsert_plan(
            blend_id=blend_id,
            tenant_id=TEST_TENANT,
            user_id=TEST_USER,
            data_trade_date=SAMPLE_DATE,
            target_date=date(2026, 10, 20),
            config_hash="cfg-svc",
            positions_hash=positions_hash([]),
            params={},
            summary={},
            entries=[
                {
                    "symbol": "SZ000001",
                    "shares": 100,
                    "ref_price": 10.0,
                    "target_value": 1000.0,
                    "score": 0.9,
                }
            ],
            holds=[],
            exits=[],
        )
        svc = ModelBlendService(persistence=persistence)

        async def recent():
            return {SAMPLE_DATE}

        svc._recent_trading_days = recent
        refreshed = await svc.confirm_plan(
            blend, plan, tenant_id=TEST_TENANT, user_id=TEST_USER
        )
        assert refreshed["confirmed_at"]
        assert [
            p["symbol"] for p in await persistence.list_open_positions(blend_id)
        ] == ["SZ000001"]

        with pytest.raises(BlendError) as exc:
            await svc.confirm_plan(
                blend, refreshed, tenant_id=TEST_TENANT, user_id=TEST_USER
            )
        assert exc.value.code == "plan_confirmed"

        async with get_session() as session:
            assert (
                await session.execute(
                    text(
                        "SELECT COUNT(*) FROM qm_model_blend_plans "
                        "WHERE blend_id = :blend_id AND confirmed_at IS NOT NULL"
                    ),
                    {"blend_id": blend_id},
                )
            ).scalar_one() == 1
    finally:
        await _cleanup_blends()
