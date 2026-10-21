"""组合历史 pred 测试集数据源（DuckDB 聚合）回归。

口径与 ``ModelBlendService._window_stats`` 对齐：
每模型按日 ``PERCENT_RANK`` → 按权重聚合 → 按日排序取 TopK。
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import date, datetime
from pathlib import Path

import duckdb
import pytest

from backend.services.engine.services.blend_pred_source import (
    _READINESS_CACHE,
    _TOP_CACHE,
    BlendPredError,
    BlendPredSource,
    PredMember,
    _as_date,
    _cache_get,
    _cache_put,
)

D0 = date(2026, 1, 5)
D1 = date(2026, 1, 6)


@pytest.fixture(autouse=True)
def _clear_pred_caches():
    """进程内缓存跨测试隔离，避免命中上一个用例的 tmp 路径。"""
    _READINESS_CACHE.clear()
    _TOP_CACHE.clear()
    yield
    _READINESS_CACHE.clear()
    _TOP_CACHE.clear()


class _FakeEffective:
    def __init__(self, storage_path: str, *, model_source="user_model", fallback=False):
        self.storage_path = storage_path
        self.model_source = model_source
        self.fallback_used = fallback


class _FakeResolver:
    def __init__(self, mapping: dict[str, object]):
        self._mapping = mapping

    async def resolve_effective_model(self, *, tenant_id, user_id, model_id, market):
        value = self._mapping[model_id]
        if isinstance(value, Exception):
            raise value
        return value


def _write_pred(path: Path, rows: list[tuple[str, date, float, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    values = ", ".join(
        f"('{symbol}', TIMESTAMP '{day.isoformat()}', {pred}, '{split}')"
        for symbol, day, pred, split in rows
    )
    duckdb.connect().execute(
        f"COPY (SELECT * FROM (VALUES {values}) "
        "AS t(symbol, trade_date, pred, split)) "
        f"TO '{path}' (FORMAT PARQUET)"
    )


def _member(model_id: str, storage: Path, weight: float = 1.0) -> dict:
    return {
        "model_id": model_id,
        "storage_path": str(storage),
        "weight": weight,
    }


@pytest.fixture()
def two_models(tmp_path: Path) -> tuple[Path, Path]:
    first = tmp_path / "m1"
    second = tmp_path / "m2"
    _write_pred(
        first / "pred.parquet",
        [
            ("SH600000", D0, 4.0, "test"),
            ("SH600001", D0, 3.0, "test"),
            ("SH600002", D0, 2.0, "test"),
            ("SH600003", D0, 1.0, "test"),
            ("SH600000", D1, 1.0, "test"),
            ("SH600001", D1, 2.0, "test"),
            ("SH600002", D1, 3.0, "test"),
            ("SH600003", D1, 4.0, "test"),
            ("SH600000", D0, 99.0, "valid"),
        ],
    )
    _write_pred(
        second / "pred.parquet",
        [
            ("SH600000", D0, 3.0, "test"),
            ("SH600001", D0, 2.0, "test"),
            ("SH600002", D0, 1.0, "test"),
        ],
    )
    return first, second


@pytest.mark.unit
@pytest.mark.asyncio
async def test_bounds_intersect_test_split_across_members(two_models):
    first, second = two_models
    source = BlendPredSource()
    members = [_member("m1", first), _member("m2", second)]

    readiness = await source.readiness(members, tenant_id="t", user_id="u", market="CN")

    assert readiness.ready_members == 2
    assert readiness.members_total == 2
    assert readiness.start == D0
    assert readiness.end == D0  # m2 只有 D0
    assert readiness.trading_days == 1
    assert readiness.members[0].path.name == "pred.parquet"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_pred_file_makes_member_unready(tmp_path: Path, two_models):
    first, _ = two_models
    source = BlendPredSource()
    members = [_member("m1", first), _member("m2", tmp_path / "empty")]

    readiness = await source.readiness(members, tenant_id="t", user_id="u", market="CN")

    assert readiness.ready_members == 1
    assert readiness.members_total == 2
    assert "m2" in " ".join(readiness.warnings)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_top_rows_mean_aggregation_and_min_coverage(two_models):
    first, second = two_models
    source = BlendPredSource()
    members = [_member("m1", first), _member("m2", second)]

    ready = await source.readiness(members, tenant_id="t", user_id="u", market="CN")
    rows = await source.load_top_rows(
        ready.members,
        method="mean",
        trim_count=0,
        min_coverage=2,
        consensus_min_count=0,
        start=D0,
        end=D0,
        top_k=2,
    )

    assert list(rows) == [D0]
    day_rows = rows[D0]
    assert [row["symbol"] for row in day_rows] == ["SH600000", "SH600001"]
    assert [row["row_rank"] for row in day_rows] == [1, 2]
    assert day_rows[0]["score"] == pytest.approx(1.0)
    assert day_rows[1]["score"] == pytest.approx((2 / 3 + 0.5) / 2)
    assert day_rows[0]["coverage"] == 2
    assert day_rows[1]["rank"] == 2
    assert day_rows[0]["pct"] == pytest.approx(1.0)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_top_rows_weighted_mean_uses_member_weights(two_models):
    first, second = two_models
    source = BlendPredSource()
    members = [_member("m1", first, 3.0), _member("m2", second, 1.0)]

    ready = await source.readiness(members, tenant_id="t", user_id="u", market="CN")
    rows = await source.load_top_rows(
        ready.members,
        method="mean",
        trim_count=0,
        min_coverage=1,
        consensus_min_count=0,
        start=D0,
        end=D0,
        top_k=10,
    )

    scores = {row["symbol"]: row["score"] for row in rows[D0]}
    assert scores["SH600000"] == pytest.approx(1.0)
    assert scores["SH600001"] == pytest.approx((3 * (2 / 3) + 0.5) / 4)
    assert scores["SH600002"] == pytest.approx((3 * (1 / 3) + 0.0) / 4)
    # m2 没有 SH600003 → 只有 m1 一个模型，权重归一后仍是 1.0
    assert scores["SH600003"] == pytest.approx(0.0)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_top_rows_median_and_trimmed(two_models, tmp_path):
    first, second = two_models
    source = BlendPredSource()

    third = tmp_path / "m3"
    _write_pred(
        third / "pred.parquet",
        [
            ("SH600000", D0, 1.0, "test"),
            ("SH600001", D0, 2.0, "test"),
            ("SH600002", D0, 3.0, "test"),
        ],
    )
    mean_members = [_member("m1", first), _member("m2", second)]
    ready = await source.readiness(
        mean_members, tenant_id="t", user_id="u", market="CN"
    )
    median_rows = await source.load_top_rows(
        ready.members,
        method="median",
        trim_count=0,
        min_coverage=2,
        consensus_min_count=0,
        start=D0,
        end=D0,
        top_k=10,
    )
    median_scores = {row["symbol"]: row["score"] for row in median_rows[D0]}
    assert median_scores["SH600000"] == pytest.approx(1.0)
    assert median_scores["SH600001"] == pytest.approx((2 / 3 + 0.5) / 2)

    trimmed_members = mean_members + [_member("m3", third)]
    trimmed_ready = await source.readiness(
        trimmed_members, tenant_id="t", user_id="u", market="CN"
    )
    trimmed_rows = await source.load_top_rows(
        trimmed_ready.members,
        method="trimmed",
        trim_count=1,
        min_coverage=2,
        consensus_min_count=0,
        start=D0,
        end=D0,
        top_k=10,
    )
    trimmed_scores = {row["symbol"]: row["score"] for row in trimmed_rows[D0]}
    # SH600002 三模型分位 = [1/3, 0, 1]，各剔一头 → 1/3
    assert trimmed_scores["SH600002"] == pytest.approx(1 / 3)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_partial_readiness_blocks_pred_source(two_models, tmp_path: Path):
    first, _ = two_models
    source = BlendPredSource()
    members = [_member("m1", first), _member("m2", tmp_path / "empty")]

    ready = await source.readiness(members, tenant_id="t", user_id="u", market="CN")

    assert not ready.all_ready
    with pytest.raises(Exception) as exc:
        await source.load_top_rows(
            [],
            method="mean",
            trim_count=0,
            min_coverage=1,
            consensus_min_count=0,
            start=D0,
            end=D0,
            top_k=5,
        )
    assert getattr(exc.value, "code", "") == "pred_unavailable"


@pytest.mark.unit
def test_pred_member_is_hashable_and_carries_bounds():
    member = PredMember(
        model_id="m1",
        weight=1.0,
        path=Path("/tmp/pred.parquet"),
        mtime_ns=1,
        size=2,
        start=D0,
        end=D1,
    )
    assert member.start == D0
    assert member.end == D1


@pytest.mark.unit
def test_as_date_accepts_datetime_date_and_iso_text():
    assert _as_date(datetime(2026, 1, 5, 15, 0)) == D0
    assert _as_date(D0) == D0
    assert _as_date("2026-01-05") == D0
    assert _as_date("2026-01-05T15:00:00") == D0


@pytest.mark.unit
def test_cache_expiry_and_lru_eviction():
    cache: OrderedDict = OrderedDict()
    _cache_put(cache, ("expired",), 1, ttl=-1.0, max_entries=2)
    assert _cache_get(cache, ("expired",), 0.0) is None
    assert ("expired",) not in cache

    _cache_put(cache, ("a",), 1, ttl=60.0, max_entries=1)
    _cache_put(cache, ("b",), 2, ttl=60.0, max_entries=1)
    assert _cache_get(cache, ("a",), 60.0) is None
    assert _cache_get(cache, ("b",), 60.0) == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_readiness_cache_hit_skips_rediscovery(two_models, monkeypatch):
    first, second = two_models
    calls = {"n": 0}
    original = BlendPredSource._read_bounds

    def counting(members):
        calls["n"] += 1
        return original(members)

    monkeypatch.setattr(BlendPredSource, "_read_bounds", staticmethod(counting))
    source = BlendPredSource()
    members = [_member("m1", first), _member("m2", second)]

    one = await source.readiness(members, tenant_id="t", user_id="u", market="CN")
    two = await source.readiness(members, tenant_id="t", user_id="u", market="CN")

    assert one == two
    assert calls["n"] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_readiness_resolves_members_without_storage_path(two_models):
    first, second = two_models
    resolver = _FakeResolver(
        {
            "m1": _FakeEffective(str(first)),
            "m2": _FakeEffective(str(second)),
        }
    )
    source = BlendPredSource(resolver=resolver)
    members = [{"model_id": "m1", "weight": 1.0}, {"model_id": "m2"}]

    ready = await source.readiness(members, tenant_id="t", user_id="u", market="CN")

    assert ready.all_ready
    assert ready.ready_members == 2
    assert ready.start == D0
    assert ready.end == D0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_readiness_degrades_members_that_fail_resolution(two_models):
    first, _ = two_models
    resolver = _FakeResolver(
        {
            "m1": _FakeEffective(str(first)),
            "boom": RuntimeError("resolver down"),
            "fallback": _FakeEffective(
                str(first), model_source="system_fallback", fallback=True
            ),
        }
    )
    source = BlendPredSource(resolver=resolver)
    members = [{"model_id": "m1"}, {"model_id": "boom"}, {"model_id": "fallback"}]

    ready = await source.readiness(members, tenant_id="t", user_id="u", market="CN")

    assert ready.members_total == 3
    assert ready.ready_members == 1
    assert not ready.all_ready
    assert "member_unavailable:boom" in ready.warnings
    assert "member_unavailable:fallback" in ready.warnings


@pytest.mark.unit
@pytest.mark.asyncio
async def test_readiness_without_any_resolved_member_returns_empty(tmp_path: Path):
    source = BlendPredSource()
    members = [{"model_id": "m1", "storage_path": str(tmp_path / "nope")}]

    ready = await source.readiness(members, tenant_id="t", user_id="u")

    assert ready.ready_members == 0
    assert ready.members_total == 1
    assert ready.start is None
    assert ready.end is None
    assert ready.trading_days == 0
    assert not ready.all_ready
    assert "member_pred_unavailable:m1" in ready.warnings

    # 空结果同样进缓存：第二次命中直接返回同一载荷
    again = await source.readiness(members, tenant_id="t", user_id="u")
    assert again == ready


@pytest.mark.unit
@pytest.mark.asyncio
async def test_corrupt_member_falls_back_to_per_file_bounds(two_models, tmp_path: Path):
    first, _ = two_models
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "pred.parquet").write_text("not a parquet file")
    source = BlendPredSource()
    members = [_member("good", first), _member("bad", bad)]

    ready = await source.readiness(members, tenant_id="t", user_id="u", market="CN")

    assert ready.ready_members == 1
    assert ready.members[0].model_id == "good"
    assert "member_pred_empty:bad" in ready.warnings


@pytest.mark.unit
@pytest.mark.asyncio
async def test_load_top_rows_rejects_unknown_method(two_models):
    first, _ = two_models
    source = BlendPredSource()
    ready = await source.readiness(
        [_member("m1", first)], tenant_id="t", user_id="u", market="CN"
    )

    with pytest.raises(BlendPredError) as exc:
        await source.load_top_rows(
            ready.members,
            method="bogus",
            trim_count=0,
            min_coverage=1,
            consensus_min_count=0,
            start=D0,
            end=D0,
            top_k=5,
        )
    assert exc.value.code == "invalid_request"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_load_top_rows_window_outside_bounds_is_empty(two_models):
    first, _ = two_models
    source = BlendPredSource()
    ready = await source.readiness(
        [_member("m1", first)], tenant_id="t", user_id="u", market="CN"
    )

    rows = await source.load_top_rows(
        ready.members,
        method="mean",
        trim_count=0,
        min_coverage=1,
        consensus_min_count=0,
        start=date(2026, 2, 1),
        end=date(2026, 2, 2),
        top_k=5,
    )
    assert rows == {}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_load_top_rows_cache_returns_independent_copies(two_models):
    first, _ = two_models
    source = BlendPredSource()
    ready = await source.readiness(
        [_member("m1", first)], tenant_id="t", user_id="u", market="CN"
    )
    kwargs = {
        "method": "mean",
        "trim_count": 0,
        "min_coverage": 1,
        "consensus_min_count": 0,
        "start": D0,
        "end": D0,
        "top_k": 2,
    }

    first_call = await source.load_top_rows(ready.members, **kwargs)
    first_call[D0][0]["symbol"] = "MUTATED"
    second_call = await source.load_top_rows(ready.members, **kwargs)

    assert second_call[D0][0]["symbol"] == "SH600000"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_load_top_rows_maps_query_failure_to_pred_unavailable(tmp_path: Path):
    bad = tmp_path / "bad.parquet"
    bad.write_text("not a parquet file")
    member = PredMember(
        model_id="m1",
        weight=1.0,
        path=bad,
        mtime_ns=0,
        size=0,
        start=D0,
        end=D1,
    )
    source = BlendPredSource()

    with pytest.raises(BlendPredError) as exc:
        await source.load_top_rows(
            [member],
            method="mean",
            trim_count=0,
            min_coverage=1,
            consensus_min_count=0,
            start=D0,
            end=D1,
            top_k=5,
        )
    assert exc.value.code == "pred_unavailable"
