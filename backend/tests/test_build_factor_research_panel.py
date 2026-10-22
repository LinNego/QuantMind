"""因子研究月末名次面板：列集/列序对齐契约（2026-10-22）。

背景：``_build_panel`` 的 symbol 轴取自**首个因子**的列，随后各因子直接
``sc.to_numpy()`` 按位置取前 K 名。只要某个因子的列集或列序与首个不同
（实测：截断窗下估值面板比行情面板多 1 只），就会
- 列数更多 → IndexError: index 5691 is out of bounds（构建在 [6/8] 崩）；
- 列数相同但顺序不同 → **静默错标 symbol**（分数记到别的票头上，无一层报错）。

契约：所有因子一律 reindex 到统一 symbol 轴后再按位置取，列集/列序差异只能是
「该票在该因子上缺失」。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from backend.scripts.build_factor_research import _build_panel

DATES = pd.to_datetime(["2024-01-31", "2024-02-29"])
A, B, C = "600000.SH", "600001.SH", "600002.SH"


def _frame(columns, rows, index=DATES) -> pd.DataFrame:
    return pd.DataFrame(rows, index=index, columns=columns, dtype=float)


def _fwd(columns) -> pd.DataFrame:
    return _frame(columns, np.zeros((len(DATES), len(columns))))


def test_因子列集更宽时按名字对齐而不是越界():
    """次因子多一只（估值面板常见）→ 不得越界；多出的票按自己的分数进榜。"""
    first = _frame([A, B], [[1.0, 2.0], [1.0, 2.0]])
    wider = _frame([A, B, C], [[0.1, 0.2, 0.3], [0.1, 0.2, 0.3]])

    panel = _build_panel(
        {"a": first, "b": wider},
        {"a": first, "b": wider},
        _fwd([A, B, C]),
        k=2,
    )

    day1_b = panel[(panel["factor_code"] == "b") & (panel["trade_date"] == DATES[0])]
    got = dict(zip(day1_b["symbol"], day1_b["score"], strict=True))
    assert got == {C: np.float32(0.3), B: np.float32(0.2)}


def test_因子列序不同时symbol与分数不错位():
    """列序与首因子相反时，分数必须跟着名字走（错位是静默污染）。"""
    first = _frame([A, B, C], [[3.0, 2.0, 1.0], [3.0, 2.0, 1.0]])
    reversed_ = _frame([C, B, A], [[9.0, 8.0, 7.0], [9.0, 8.0, 7.0]])

    panel = _build_panel(
        {"a": first, "b": reversed_},
        {"a": first, "b": reversed_},
        _fwd([A, B, C]),
        k=1,
    )

    day1 = panel[panel["trade_date"] == DATES[0]].set_index("factor_code")
    assert day1.loc["a", "symbol"] == A and day1.loc["a", "score"] == np.float32(3.0)
    assert day1.loc["b", "symbol"] == C and day1.loc["b", "score"] == np.float32(9.0)


def test_缺失票不进榜且名次连续():
    """统一轴后，某因子缺该票（NaN）→ 该期少一个名次，rank 仍从 1 连续。"""
    first = _frame([A, B], [[1.0, 2.0], [1.0, 2.0]])
    partial = _frame([A, B, C], [[0.5, np.nan, 0.9], [0.5, np.nan, 0.9]])

    panel = _build_panel(
        {"a": first, "b": partial},
        {"a": first, "b": partial},
        _fwd([A, B, C]),
        k=3,
    )

    day1_b = panel[(panel["factor_code"] == "b") & (panel["trade_date"] == DATES[0])]
    assert day1_b["symbol"].tolist() == [C, A]
    assert day1_b["rank"].tolist() == [1, 2]
