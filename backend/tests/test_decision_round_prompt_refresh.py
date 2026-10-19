"""决策轮「问模型前按需刷新行情」（2026-10-19）：顺序/开关/失败姿态。

**为什么需要**：热集轮转 ~0.18s/只 × 500+ 只 ≈ 100s 才轮到同一只票一次，而决策轮
读快照是一瞬间的事——池票在提示词里的价格期望年龄 ≈50s，模型看到的是「上一轮的
市场」。轮首对 持仓+池+指数 现查现写，提示词与后续风控读到同一份新鲜行情。

本文件钉三件事（与 ``refresh_symbols_now`` 的 best-effort 契约一致）：

1. **顺序**：刷新必须发生在 ``read_snaps`` **之前**（先写后读，反过来等于没刷）。
2. **开关**：``QM_DECISION_ROUND_PROMPT_REFRESH=false`` 一次都不调（桥维护时
   不该让每轮多等 8s）。
3. **失败不拦轮**：刷新抛异常/桥未配置 → 照常读既有快照，``quotes_failure``
   不被污染（行情增益面不许把整轮打成 abort）。
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest

from backend.services.trade.services import decision_round_blocks as B
from backend.shared.decision_blocks import INDEX_CODES


def _deps(read_snaps, *, series=None):
    return SimpleNamespace(
        load_regime=None,
        read_snaps=read_snaps,
        read_series_summary=series,
    )


def _options() -> tuple[list[dict], list[object], str]:
    """持仓 + 池 + 无 regime：要价清单 = 持仓 ∪ 池 ∪ 四只展示指数。"""
    positions = [{"symbol": "SH600036"}]
    pool = [
        SimpleNamespace(code="300911.SZ", name="亿田智能"),
        SimpleNamespace(code="SH600036", name="招商银行"),  # 与持仓重叠
    ]
    return positions, pool, ""


def _codes() -> set[str]:
    return {"600036.SH", "300911.SZ", *INDEX_CODES}


@pytest.mark.asyncio
async def test_refresh_runs_before_snapshot_read(monkeypatch):
    events: list[tuple[str, tuple[str, ...]]] = []

    async def fake_refresh(codes, round_id):
        events.append(("refresh", tuple(codes)))

    def fake_read(_client, codes):
        events.append(("read", tuple(codes)))
        return {"600036.SH": {"Now": 40.0, "timestamp": 1}}

    monkeypatch.setattr(B, "_refresh_quotes_before_prompt", fake_refresh)
    positions, pool, failure = _options()

    _regime, snapped, _series, out_failure = await B.load_round_snapshots(
        _deps(fake_read), "r-1", object(), positions, pool, failure
    )

    assert [name for name, _ in events] == ["refresh", "read"], (
        "先读后刷 = 提示词拿到的还是旧快照，刷新动作白做"
    )
    assert set(events[0][1]) == _codes()
    assert set(events[1][1]) == _codes()
    assert snapped and out_failure == ""


@pytest.mark.asyncio
async def test_refresh_off_by_env_skips_the_bridge(monkeypatch):
    calls: list[tuple[str, ...]] = []

    async def fake_refresh(codes):
        calls.append(tuple(codes))
        return {}

    fake = types.ModuleType("backend.services.live_trading.services.tdx_quote_feed")
    fake.refresh_symbols_now = fake_refresh
    monkeypatch.setitem(
        sys.modules, "backend.services.live_trading.services.tdx_quote_feed", fake
    )
    monkeypatch.setenv("QM_DECISION_ROUND_PROMPT_REFRESH", "false")

    await B._refresh_quotes_before_prompt(["600036.SH"], "r-1")

    assert calls == []


@pytest.mark.asyncio
async def test_refresh_failure_does_not_block_the_round(monkeypatch):
    async def boom(codes):
        raise RuntimeError("桥挂起")

    fake = types.ModuleType("backend.services.live_trading.services.tdx_quote_feed")
    fake.refresh_symbols_now = boom
    monkeypatch.setitem(
        sys.modules, "backend.services.live_trading.services.tdx_quote_feed", fake
    )
    monkeypatch.delenv("QM_DECISION_ROUND_PROMPT_REFRESH", raising=False)

    # 不抛 = 契约本身
    await B._refresh_quotes_before_prompt(["600036.SH"], "r-1")


@pytest.mark.asyncio
async def test_no_quote_client_means_no_refresh(monkeypatch):
    calls: list[tuple[str, ...]] = []

    async def fake_refresh(codes, round_id):
        calls.append(tuple(codes))

    monkeypatch.setattr(B, "_refresh_quotes_before_prompt", fake_refresh)
    positions, pool, failure = _options()

    _regime, snapped, _series, _failure = await B.load_round_snapshots(
        _deps(lambda *_a: {}), "r-1", None, positions, pool, failure
    )

    assert calls == []  # 客户端都没有，刷新也没有读取方
    assert snapped == {}
