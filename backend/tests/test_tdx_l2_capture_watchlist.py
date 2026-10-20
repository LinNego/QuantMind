"""``tdx_l2_capture_task`` 轮询集合单测：决策池优先。

口径（2026-10-20 实测）：只按推理分数 Top-N 建 watchlist 时，决策池 20 只里
常只有 1 只重合（1/20），付费 L2 的购买成本等于空转——agent 决策轮只渲染
持仓+池，池里的票没有快照就是「本轮无数据」。轮询顺序必须是
**决策池 → 持仓 → 分数 Top-N**，池/持仓在 50 只上限内永不被分数挤掉。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.services.live_trading.services import tdx_l2_capture_task as T
from backend.shared.decision_context_source import PoolDoc


# ── 决策池读取 ─────────────────────────────────────────────────────
def _pool_doc(*codes: str) -> PoolDoc:
    return PoolDoc(
        rows=tuple(SimpleNamespace(code=code) for code in codes),
        direction=SimpleNamespace(direction="—", total_score=None),
        missing_columns=(),
        source="test",
    )


def test_decision_pool_codes_normalizes_suffix_to_prefix_and_dedupes(monkeypatch):
    seen: list[str] = []

    def fake_load(day: str) -> PoolDoc:
        seen.append(day)
        return _pool_doc("600036.SH", "600036.SH", "000001.SZ", "830799.BJ")

    monkeypatch.setattr(
        "backend.shared.decision_context_source.load_pool_doc", fake_load
    )

    codes = T._decision_pool_codes("20261020")

    assert codes == ["SH600036", "SZ000001", "BJ830799"]
    assert seen == ["20261020"]


def test_decision_pool_codes_missing_file_returns_empty(monkeypatch):
    monkeypatch.setattr(
        "backend.shared.decision_context_source.load_pool_doc",
        lambda day: None,
    )

    assert T._decision_pool_codes("20261020") == []


def test_decision_pool_codes_read_failure_degrades_to_empty(monkeypatch):
    """池读失败只降级（返回空），绝不让采集循环整体退出。"""

    def boom(day: str):
        raise OSError("池文件在但读不动")

    monkeypatch.setattr("backend.shared.decision_context_source.load_pool_doc", boom)

    assert T._decision_pool_codes("20261020") == []


def test_decision_pool_codes_skips_rows_without_code(monkeypatch):
    monkeypatch.setattr(
        "backend.shared.decision_context_source.load_pool_doc",
        lambda day: _pool_doc("", "600036.SH"),
    )

    assert T._decision_pool_codes("20261020") == ["SH600036"]


# ── watchlist 优先级 ───────────────────────────────────────────────
def test_decision_pool_wins_over_positions_and_scores():
    """决策池是 agent 真正会看的票：排在持仓与分数之前，且去重保序。"""

    watch = T._resolve_watchlist(
        scores={"SH600036": 90.0, "SZ000001": 80.0},
        tdx_positions=[{"symbol": "SZ000001"}],
        paper_positions=[{"symbol": "SH601000"}],
        pool_size=20,
        decision_pool=["SZ002469", "SH600036"],
    )

    assert watch[:3] == ["SZ002469", "SH600036", "SZ000001"]
    assert watch == ["SZ002469", "SH600036", "SZ000001", "SH601000"]


def test_pool_size_still_caps_only_the_score_leg():
    """``pool_size`` 是分数候选池的配置，不许缩掉决策池/持仓。"""

    watch = T._resolve_watchlist(
        scores={"SH600036": 90.0, "SZ000001": 80.0, "SH600000": 70.0},
        tdx_positions=[{"symbol": "SH601000"}],
        paper_positions=[],
        pool_size=1,
        decision_pool=["SZ002469", "SH600519"],
    )

    assert watch == ["SZ002469", "SH600519", "SH601000", "SH600036"]


def test_watchlist_cap_keeps_decision_pool_never_scores():
    """上限 50：决策池先占位，分数腿尾随，溢出时分数先被裁。"""

    pool = [f"SH{600000 + i:06d}" for i in range(40)]
    scores = {f"SZ{1 + i:06d}": 100.0 - i for i in range(30)}

    watch = T._resolve_watchlist(
        scores=scores,
        tdx_positions=[{"symbol": "SH601000"}],
        paper_positions=[],
        pool_size=30,
        decision_pool=pool,
    )

    assert len(watch) == T.MAX_WATCHLIST
    assert watch[:41] == pool + ["SH601000"]
    assert all(sym in watch for sym in pool)


def test_without_a_decision_pool_positions_and_scores_are_still_covered():
    """池未产出（空列表）时退化为 持仓 → 分数 Top-N；持仓在 cycle_order 里本就先扫。"""

    watch = T._resolve_watchlist(
        scores={"SH600036": 90.0, "SZ000001": 80.0},
        tdx_positions=[{"symbol": "SH601000"}],
        paper_positions=[],
        pool_size=20,
    )

    assert watch == ["SH601000", "SH600036", "SZ000001"]


@pytest.mark.parametrize("bad_pool", [[None], ["", "  "]])
def test_blank_decision_pool_entries_are_ignored(bad_pool):
    watch = T._resolve_watchlist(
        scores={"SH600036": 90.0},
        tdx_positions=[],
        paper_positions=[],
        pool_size=20,
        decision_pool=bad_pool,
    )

    assert watch == ["SH600036"]
