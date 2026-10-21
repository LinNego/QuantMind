"""回填落盘：逐日合并（而非循环末尾一次性）。

背景（2026-10-13）：`trigger_backfill` 曾把各日分数累积在内存、全部跑完才
一次性写 pred.parquet —— 中途重启即丢失已算完的天数，前端表现为
「跑了很久，coverage 缺口没变」。改为每日落一次（同日幂等）。

同时删除「runner 失败 → 复制上一日 parquet 行、只改 trade_date 重标」的兜底：
那是拿旧分数冒充新日分数，属数据完整性问题。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

_BACKEND = Path(__file__).resolve().parents[1]
_CORE = _BACKEND / "services/engine/inference/model_backfill.py"
_ROUTER = _BACKEND / "services/api/routers/model_training.py"


@pytest.mark.unit
def test_daily_merge_is_idempotent_for_same_day(tmp_path):
    """同一天合并两次 == 合并一次（重跑不会写重行）。"""
    from backend.services.engine.inference.pred_merge import merge_signals_into_pred

    pf = tmp_path / "pred.parquet"
    day = "2026-10-13"
    signals = [
        {"symbol": "SH600036", "score": 0.7},
        {"symbol": "SZ000001", "score": 0.2},
    ]

    n1 = merge_signals_into_pred(pf, [(day, signals)], create_if_missing=True)
    n2 = merge_signals_into_pred(pf, [(day, signals)], create_if_missing=True)

    assert n1 == 2 and n2 == 2
    df = pd.read_parquet(pf)
    assert len(df) == 2, f"同日两次合并应只有 2 行，实际 {len(df)}"
    assert set(pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")) == {day}


@pytest.mark.unit
def test_daily_merge_accumulates_across_days(tmp_path):
    """逐日各落一次 → 两天的数据都在（这是断点续跑的前提）。"""
    from backend.services.engine.inference.pred_merge import merge_signals_into_pred

    pf = tmp_path / "pred.parquet"
    merge_signals_into_pred(
        pf, [("2026-10-12", [{"symbol": "SH600036", "score": 0.7}])],
        create_if_missing=True,
    )
    merge_signals_into_pred(
        pf, [("2026-10-13", [{"symbol": "SH600036", "score": 0.9}])],
        create_if_missing=False,
    )

    df = pd.read_parquet(pf).sort_values("trade_date")
    assert len(df) == 2
    assert pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d").tolist() == [
        "2026-10-12",
        "2026-10-13",
    ]
    assert df["pred"].tolist() == [0.7, 0.9]


@pytest.mark.unit
def test_backfill_merges_per_day_and_drops_template_copy():
    """接线断言：单日执行内逐日合并（P3 起在 model_backfill），模板复制兜底已删。"""
    core = _CORE.read_text(encoding="utf-8")
    router = _ROUTER.read_text(encoding="utf-8")
    assert "def _execute_sync" in core, "必须有单日执行入口"
    assert "merge_signals_into_pred" in core, "单日执行必须把分数合并进 pred"
    assert "create_if_missing=True" in core, "补全允许在 pred 缺失时创建"
    assert "_backfill_tasks" not in router, "进程内任务字典必须删除（改落 job 表）"
    assert "merged_days" in router, "旧状态端点应继续暴露已合并天数"
    assert "模板复制" not in core and "模板复制" not in router, (
        "「复制上一日行、只改 trade_date」的模板复制兜底必须删除（假数据）"
    )
