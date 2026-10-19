"""外推预测的规范网格对齐测试。

深度序列模型与树模型的股票域天然不同：序列模型会丢弃窗口不足
``step_len`` 的标的（分数 NaN 被写盘逻辑剔除），且历史窗口路径不会
经过单日的不可交易过滤（停牌/ST 标的会多出来）。跨模型配对比较要求
每个模型在同一 (trade_date, symbol) 网格上打分，因此外推产物必须先
对齐到规范模型（LGB T+5）的网格：

- 网格外的预测行丢弃（例如深度模型多出的停牌标的）；
- 网格内缺失的 (date, symbol) 补 ``pred=NaN``（该模型当日无观点）；
- 行数/新增/丢弃/NaN 计数写入报告，便于审计。
"""

from __future__ import annotations

import pandas as pd
import pytest

from backend.scripts.backtest2026.align_extensions import align_extension_frame


def _frame(rows: list[tuple[str, str, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": [row[0] for row in rows],
            "trade_date": pd.to_datetime([row[1] for row in rows]),
            "pred": [row[2] for row in rows],
            "label_return": float("nan"),
            "source": "extended",
        }
    )


def _grid(rows: list[tuple[str, str]]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": [row[0] for row in rows],
            "trade_date": pd.to_datetime([row[1] for row in rows]),
        }
    )


@pytest.mark.unit
def test_alignment_keeps_grid_rows_and_drops_extras() -> None:
    frame = _frame(
        [
            ("SH600000", "2026-09-01", 0.1),
            ("SH600001", "2026-09-01", 0.2),
            ("SH600999", "2026-09-01", 0.3),  # 网格外：深度模型多出的停牌股
            ("SH600000", "2026-09-02", 0.4),
        ]
    )
    grid = _grid(
        [
            ("SH600000", "2026-09-01"),
            ("SH600001", "2026-09-01"),
            ("SH600000", "2026-09-02"),
            ("SH600002", "2026-09-02"),  # 网格内但模型无观点
        ]
    )

    aligned, stats = align_extension_frame(frame, grid)

    assert len(aligned) == len(grid)
    assert set(
        zip(
            aligned["symbol"],
            aligned["trade_date"].dt.strftime("%Y-%m-%d"),
            strict=True,
        )
    ) == {
        ("SH600000", "2026-09-01"),
        ("SH600001", "2026-09-01"),
        ("SH600000", "2026-09-02"),
        ("SH600002", "2026-09-02"),
    }
    missing = aligned.loc[aligned["symbol"] == "SH600002"].iloc[0]
    assert pd.isna(missing["pred"])
    assert missing["source"] == "aligned_missing"
    assert stats["added"] == 1
    assert stats["dropped"] == 1
    assert stats["pred_nan"] == 1
    assert stats["rows_before"] == 4
    assert stats["rows_after"] == 4


@pytest.mark.unit
def test_alignment_is_idempotent_and_deduplicates_grid() -> None:
    frame = _frame([("SH600000", "2026-09-01", 0.1)])
    grid = _grid(
        [
            ("SH600000", "2026-09-01"),
            ("SH600000", "2026-09-01"),
            ("SH600001", "2026-09-01"),
        ]
    )

    first, first_stats = align_extension_frame(frame, grid)
    second, second_stats = align_extension_frame(first, grid)

    assert len(first) == 2
    assert first_stats["rows_after"] == 2
    assert second_stats["added"] == 0
    assert second_stats["dropped"] == 0
    pd.testing.assert_frame_equal(
        first.reset_index(drop=True), second.reset_index(drop=True)
    )


@pytest.mark.unit
def test_alignment_with_empty_extension_fills_all_nan() -> None:
    empty = pd.DataFrame(
        columns=["symbol", "trade_date", "pred", "label_return", "source"]
    )
    grid = _grid([("SH600000", "2026-09-01"), ("SH600001", "2026-09-01")])

    aligned, stats = align_extension_frame(empty, grid)

    assert len(aligned) == 2
    assert aligned["pred"].isna().all()
    assert (aligned["source"] == "aligned_missing").all()
    assert stats["added"] == 2
    assert stats["dropped"] == 0
