"""`schedule_ctl run eval_scores` 与 EOD worker 同口径（P0-2，2026-10-21）。

历史缺陷：控制台只调 `run_all`，不跑 `run_backfill_once`；而 worker 的
`run_eval_scores_once` 两件事都做。于是「手动补跑」比「自动跑批」少一段，
每日选股的 pending † 不会闭合——两条入口给出的结果不一致，操作员却以为等价。

本测试盯住「唯一入口」：schedule_ctl 必须调 `run_eval_scores_once`，不得在
控制台里另写一份 `run_all` + 回填编排。
"""

from __future__ import annotations

import pytest


@pytest.mark.unit
def test_schedule_ctl_eval_uses_shared_one_shot_entry(monkeypatch, capsys):
    import backend.scripts.schedule_ctl as schedule_ctl
    import backend.services.trade.services.eval_scores_service as service
    import backend.shared.database_manager_v2 as dbm

    calls: list[dict] = []

    async def fake_once(*, save=True, trade_date=None):
        calls.append({"save": save, "trade_date": trade_date})
        return {
            "date": trade_date or "2026-10-21",
            "total_scored": 7,
            "total_errors": 0,
            "elapsed_sec": 1.25,
            "backfill": {"rerun": 2, "still_pending": 0, "errors": [], "note": "回填 2 条"},
        }

    async def fake_close():
        return None

    monkeypatch.setattr(service, "run_eval_scores_once", fake_once)
    monkeypatch.setattr(dbm, "close_database", fake_close)

    rc = schedule_ctl._run_eval_scores("2026-10-21", force=False)

    assert rc == 0
    assert calls == [{"save": True, "trade_date": "2026-10-21"}]
    out = capsys.readouterr().out
    assert "落分=7" in out
    assert "回填" in out and "2" in out


@pytest.mark.unit
def test_schedule_ctl_eval_returns_1_when_nothing_scored(monkeypatch):
    import backend.scripts.schedule_ctl as schedule_ctl
    import backend.services.trade.services.eval_scores_service as service
    import backend.shared.database_manager_v2 as dbm

    async def fake_once(*, save=True, trade_date=None):
        return {"total_scored": 0, "total_errors": 1, "backfill": {"errors": []}}

    async def fake_close():
        return None

    monkeypatch.setattr(service, "run_eval_scores_once", fake_once)
    monkeypatch.setattr(dbm, "close_database", fake_close)

    assert schedule_ctl._run_eval_scores(None, force=False) == 1
