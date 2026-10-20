"""recent_build_plan 的纯函数测试（不碰数据库）。"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import recent_build_plan as P  # noqa: E402


def test_board_lot_shares_rounds_down_to_100_share_lots() -> None:
    # Arrange / Act
    shares = P.board_lot_shares(98_000.0, 22.2)

    # Assert
    assert shares == 4400


def test_board_lot_shares_returns_zero_when_one_lot_exceeds_slot() -> None:
    # Arrange / Act
    shares = P.board_lot_shares(98_000.0, 1_500.0)

    # Assert
    assert shares == 0


def test_board_lot_shares_returns_zero_for_invalid_price() -> None:
    # Arrange / Act / Assert
    assert P.board_lot_shares(98_000.0, math.nan) == 0
    assert P.board_lot_shares(98_000.0, 0.0) == 0
    assert P.board_lot_shares(98_000.0, -1.0) == 0


def test_decision_rank_filters_by_average_and_adv() -> None:
    # Arrange
    score = np.array([0.9, 0.8, 0.7])
    avg = np.array([0.5, 0.5, 0.95])
    adv = np.array([100_000.0, 100_000.0, 100_000.0])

    # Act
    rank, cutoff, n_ok = P.decision_rank(score, avg, adv, target=1_000.0, symbol_j=1)

    # Assert：第 3 只 S<均值被剔除；第 2 只在剩余候选中排第 2。
    assert (rank, cutoff, n_ok) == (2, 0.8, 2)


def test_decision_rank_returns_none_for_ineligible_symbol() -> None:
    # Arrange
    score = np.array([0.9, 0.8, 0.7])
    avg = np.array([0.5, 0.5, 0.95])
    adv = np.array([100_000.0, 100_000.0, 100_000.0])

    # Act
    rank, cutoff, n_ok = P.decision_rank(score, avg, adv, target=1_000.0, symbol_j=2)

    # Assert：不满足入池条件时，排名与分数线都不返回，但仍报告候选数。
    assert rank is None
    assert cutoff is None
    assert n_ok == 2


def test_decision_rank_cutoff_uses_requested_top_n() -> None:
    # Arrange：三只都入池，Top1 的分数线应取第 1 名，而不是默认第 20 名。
    score = np.array([0.9, 0.8, 0.7])
    avg = np.array([0.5, 0.5, 0.5])
    adv = np.array([100_000.0, 100_000.0, 100_000.0])

    # Act
    _, cutoff, n_ok = P.decision_rank(
        score, avg, adv, target=1_000.0, symbol_j=0, top_n=1
    )

    # Assert
    assert cutoff == 0.9
    assert n_ok == 3
