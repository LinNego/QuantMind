"""组合选股研究纯函数：分位口径、组合分、次日命中、bootstrap 与 10 日闸门。

这些函数是 P1 证据包的算盘：口径错了，报告再漂亮也是假的。测试用合成面板
锁死三件事：
- 分位与 ``compute_rank_pct`` 完全一致（含并列、NaN、单值）；
- 缺失成员重新归一，不按 0 分；
- 可执行命中同时受「t+1 不能买」与「t+2 不能卖」约束。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backend.shared.signal_contract import compute_rank_pct
from backend.scripts.backtest2026.combo_eval import (
    RebalanceConfig,
    benjamini_hochberg,
    block_bootstrap_ci,
    combine_ranks,
    daily_hits,
    meta_features,
    monthly_mean,
    paired_diff,
    rank_frame,
    simulate_rebalance,
    topk_hit_day,
)

pytestmark = pytest.mark.unit


class FakePanel:
    """满足 MarketView 协议的最小行情面板。"""

    def __init__(
        self,
        dates: pd.DatetimeIndex,
        symbols: list[str],
        close: pd.DataFrame,
        *,
        amount: pd.DataFrame | None = None,
    ) -> None:
        self.dates = dates
        self.symbols = pd.Index(symbols)
        self.close_qfq = close
        self.limit_up = pd.DataFrame(False, index=dates, columns=self.symbols)
        self.limit_down = pd.DataFrame(False, index=dates, columns=self.symbols)
        self.suspended = pd.DataFrame(False, index=dates, columns=self.symbols)
        self.is_st = pd.Series(False, index=self.symbols)
        self.age_days = pd.DataFrame(1000.0, index=dates, columns=self.symbols)
        self.amount = amount


def _panel_from_close(close: pd.DataFrame) -> FakePanel:
    return FakePanel(close.index, list(close.columns), close)


# ── 分位 ─────────────────────────────────────────────────────────────────


def test_rank_frame_matches_compute_rank_pct_including_ties_and_missing():
    raw = pd.DataFrame(
        [
            [0.9, 0.9, 0.1, np.nan],
            [5.0, np.inf, -np.inf, np.nan],
            [1.0, np.nan, np.nan, np.nan],
            [np.nan, np.nan, np.nan, np.nan],
        ],
        columns=list("ABCD"),
    )
    pct = rank_frame(raw)
    for row in range(len(raw)):
        expected = compute_rank_pct(raw.iloc[row].tolist())
        actual = pct.iloc[row].tolist()
        for got, want in zip(actual, expected, strict=True):
            if want is None:
                assert pd.isna(got)
            else:
                assert got == pytest.approx(want)


def test_rank_frame_single_column_is_zero_for_valid_only():
    raw = pd.DataFrame(
        {"A": [0.5, np.nan]}, index=pd.to_datetime(["2026-01-05", "2026-01-06"])
    )
    pct = rank_frame(raw)
    assert pct.iloc[0, 0] == 0.0
    assert pd.isna(pct.iloc[1, 0])


# ── 组合分 ───────────────────────────────────────────────────────────────


def test_combine_ranks_renormalizes_missing_members_not_zero_fill():
    ranks = pd.DataFrame(
        {"A": [0.9], "B": [np.nan], "C": [0.1]},
        index=pd.to_datetime(["2026-01-05"]),
    )
    score = combine_ranks(ranks, ["A", "B", "C"], min_coverage=1)
    assert score.iloc[0] == pytest.approx(0.5)
    gated = combine_ranks(ranks, ["A", "B", "C"], min_coverage=3)
    assert pd.isna(gated.iloc[0])


def test_combine_ranks_supports_median_trimmed_and_consensus():
    ranks = pd.DataFrame(
        {
            "A": [0.1, 0.1],
            "B": [0.5, 0.95],
            "C": [0.9, 0.92],
            "D": [np.nan, 0.7],
            "E": [0.7, 0.8],
        },
        index=pd.to_datetime(["2026-01-05", "2026-01-06"]),
    )
    median = combine_ranks(
        ranks, ["A", "B", "C", "D", "E"], method="median", min_coverage=4
    )
    # 第一行可用值 [0.1,0.5,0.9,0.7] → (0.5+0.7)/2
    assert median.iloc[0] == pytest.approx(0.6)
    assert median.iloc[1] == pytest.approx(0.8)

    trimmed = combine_ranks(
        ranks, ["A", "B", "C", "D", "E"], method="trimmed", trim=1, min_coverage=4
    )
    # 第一行可用值 [0.1,0.5,0.9,0.7] → 去掉 0.1/0.9 → (0.5+0.7)/2
    assert trimmed.iloc[0] == pytest.approx(0.6)
    # 第二行 5 个值，去掉 0.1/0.95 → (0.7+0.8+0.92)/3
    assert trimmed.iloc[1] == pytest.approx((0.7 + 0.8 + 0.92) / 3)

    gate = combine_ranks(
        ranks,
        ["A", "B", "C", "D", "E"],
        method="mean",
        min_coverage=1,
        consensus_gate={"threshold": 0.7, "min_count": 4},
    )
    assert pd.isna(gate.iloc[0])
    assert gate.iloc[1] == pytest.approx(np.mean([0.1, 0.95, 0.92, 0.7, 0.8]))


# ── 次日命中 ─────────────────────────────────────────────────────────────


def _hit_panel() -> FakePanel:
    dates = pd.to_datetime(["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"])
    close = pd.DataFrame(
        {
            "A": [10.0, 10.2, 10.1, 10.1],
            "B": [10.0, 10.5, 10.6, 10.6],
            "C": [10.0, np.nan, 10.0, 10.0],
            "D": [10.0, 9.9, 10.0, 10.0],
        },
        index=dates,
    )
    return _panel_from_close(close)


def test_topk_hit_day_theory_includes_limit_up_but_exec_does_not():
    panel = _hit_panel()
    panel.limit_up.loc[pd.Timestamp("2026-01-06"), "B"] = True
    scores = pd.Series({"A": 0.9, "B": 0.8, "C": 0.7, "D": 0.6})
    hit = topk_hit_day(scores, pd.Timestamp("2026-01-05"), panel, 2)
    assert hit is not None
    assert hit.n_top == 2
    assert hit.n_theory == 2
    assert hit.n_entry == 1
    assert hit.n_exec == 1
    assert hit.hit_theory == pytest.approx(1.0)
    assert hit.hit_exec == pytest.approx(0.0)
    assert hit.ret_exec == pytest.approx(10.1 / 10.2 - 1.0)


def test_topk_hit_day_excludes_suspended_entry_and_blocked_exit():
    panel = _hit_panel()
    panel.limit_down.loc[pd.Timestamp("2026-01-07"), "A"] = True
    scores = pd.Series({"A": 0.9, "B": 0.8, "C": 0.7, "D": 0.6})
    hit = topk_hit_day(scores, pd.Timestamp("2026-01-05"), panel, 3)
    assert hit is not None
    assert hit.n_top == 3
    # C 在 t+1 停牌无收盘价，理论口径也拿不到收益
    assert hit.n_theory == 2
    # A 在 t+2 跌停不能卖；只剩 B
    assert hit.n_exec == 1
    assert hit.ret_exec == pytest.approx(10.6 / 10.5 - 1.0)
    assert hit.ret_exec_net == pytest.approx(10.6 / 10.5 - 1.0 - 6 / 10_000)


def test_daily_hits_respects_window_and_skips_tail_without_t_plus_2():
    panel = _hit_panel()
    scores = {
        pd.Timestamp("2026-01-05"): pd.Series({"A": 0.9, "B": 0.8}),
        pd.Timestamp("2026-01-06"): pd.Series({"A": 0.9, "B": 0.8}),
        pd.Timestamp("2026-01-08"): pd.Series({"A": 0.9, "B": 0.8}),
    }
    frame = daily_hits(scores, panel, 2, start="2026-01-05", end="2026-01-08")
    assert list(frame.index) == [pd.Timestamp("2026-01-05"), pd.Timestamp("2026-01-06")]
    assert (frame["n_top"] == 2).all()


# ── 统计 ─────────────────────────────────────────────────────────────────


def test_block_bootstrap_positive_diff_has_ci_above_zero():
    index = pd.date_range("2026-01-01", periods=6, freq="MS")
    diff = pd.Series([0.05, 0.06, 0.04, 0.07, 0.05, 0.06], index=index)
    result = block_bootstrap_ci(diff, n_boot=500, seed=7)
    assert result["mean"] == pytest.approx(0.055)
    assert result["ci_low"] > 0
    assert result["p_value"] == 0.0


def test_block_bootstrap_mixed_diff_ci_crosses_zero():
    index = pd.date_range("2026-01-01", periods=6, freq="MS")
    diff = pd.Series([0.2, -0.2, 0.2, -0.2, 0.2, -0.2], index=index)
    result = block_bootstrap_ci(diff, n_boot=500, seed=7)
    assert result["mean"] == pytest.approx(0.0, abs=1e-12)
    assert result["ci_low"] < 0 < result["ci_high"]
    assert result["p_value"] > 0.05


def test_paired_diff_and_monthly_mean_only_keep_common_days():
    index = pd.to_datetime(["2026-01-05", "2026-01-06", "2026-02-02", "2026-02-03"])
    left = pd.DataFrame({"hit_exec": [0.5, np.nan, 0.4, 0.3]}, index=index)
    right = pd.DataFrame({"hit_exec": [0.4, 0.2, np.nan, 0.3]}, index=index)
    diff = paired_diff(left, right, "hit_exec")
    assert list(diff.index) == [pd.Timestamp("2026-01-05"), pd.Timestamp("2026-02-03")]
    monthly = monthly_mean(diff)
    assert monthly.loc[pd.Period("2026-01", "M")] == pytest.approx(0.1)
    assert monthly.loc[pd.Period("2026-02", "M")] == pytest.approx(0.0)


def test_benjamini_hochberg_is_monotone_and_bounded():
    pvalues = pd.Series([0.001, 0.008, 0.039, 0.041, 0.042, 0.06, 0.074, 0.205])
    qvalues = benjamini_hochberg(pvalues)
    assert (qvalues >= pvalues).all()
    assert (qvalues <= 1.0).all()
    assert qvalues.is_monotonic_increasing
    assert qvalues.iloc[0] == pytest.approx(0.008)


# ── 10 日闸门 ────────────────────────────────────────────────────────────


def _gate_setup(days: int = 40):
    dates = pd.bdate_range("2026-01-05", periods=days)
    close = pd.DataFrame(
        {"A": np.full(days, 10.0), "B": np.full(days, 10.0)},
        index=dates,
    )
    panel = _panel_from_close(close)
    panel.amount = pd.DataFrame(1e9, index=dates, columns=panel.symbols)
    scores = {day: pd.Series({"A": 0.9, "B": 0.1}) for day in dates}
    cfg = RebalanceConfig(
        capital=1_000_000,
        top_k=1,
        hold_days=3,
        entry_per_day=1,
        target_positions=1,
        cash_buffer=0.02,
        adv_participation=0.01,
        exit_delta=0.03,
        lookback=5,
        min_lookback_days=3,
        fee_rate=0.0003,
        min_listing_days=60,
    )
    return dates, panel, scores, cfg


def test_simulate_rebalance_enters_next_day_and_deducts_cash():
    dates, panel, scores, cfg = _gate_setup()
    result = simulate_rebalance(
        scores, panel, start=str(dates[0].date()), end=str(dates[-1].date()), config=cfg
    )
    entries = [trade for trade in result.trades if trade["side"] == "entry"]
    exits = [trade for trade in result.trades if trade["side"] == "exit"]
    assert entries and entries[0]["date"] == dates[4]
    assert entries[0]["symbol"] == "A"
    assert exits and exits[0]["date"] == dates[7]
    # 手续费之外权益不凭空增加（现金必须被扣掉）
    assert result.equity.loc[dates[4]] < 1_000_000
    assert result.equity.loc[dates[4]] == pytest.approx(
        1_000_000 - entries[0]["shares"] * 10.0 * 0.0003, rel=1e-9
    )
    assert result.n_exits + result.open_positions == result.n_entries
    assert result.n_entries >= 5
    assert result.avg_trade_return == pytest.approx(-2 * 0.0003, abs=1e-6)


def test_simulate_rebalance_caps_order_by_adv():
    dates, panel, scores, cfg = _gate_setup()
    panel.amount.loc[:, :] = 1_000_000.0
    result = simulate_rebalance(
        scores, panel, start=str(dates[0].date()), end=str(dates[-1].date()), config=cfg
    )
    first = next(trade for trade in result.trades if trade["side"] == "entry")
    assert first["shares"] == 1000  # 1% × 100 万 = 1 万元，10 元/股


def test_simulate_rebalance_defers_sell_when_limit_down():
    dates, panel, scores, cfg = _gate_setup()
    panel.limit_down.loc[dates[7], "A"] = True
    result = simulate_rebalance(
        scores, panel, start=str(dates[0].date()), end=str(dates[-1].date()), config=cfg
    )
    exits = [trade for trade in result.trades if trade["side"] == "exit"]
    assert result.deferred_exits >= 1
    assert exits and exits[0]["date"] == dates[8]


def test_simulate_rebalance_score_exit_before_maturity():
    dates, panel, scores, cfg = _gate_setup()
    scores[dates[5]] = pd.Series({"A": 0.5, "B": 0.1})
    result = simulate_rebalance(
        scores, panel, start=str(dates[0].date()), end=str(dates[-1].date()), config=cfg
    )
    exits = [trade for trade in result.trades if trade["side"] == "exit"]
    assert exits and exits[0]["date"] == dates[6]


def test_simulate_rebalance_blocks_entry_on_limit_up():
    dates, panel, scores, cfg = _gate_setup()
    panel.limit_up.loc[dates[4], "A"] = True
    result = simulate_rebalance(
        scores, panel, start=str(dates[0].date()), end=str(dates[-1].date()), config=cfg
    )
    entries = [trade for trade in result.trades if trade["side"] == "entry"]
    assert entries and entries[0]["date"] == dates[5]
    assert all(trade["date"] != dates[4] for trade in entries)


def test_meta_features_coverage_and_dispersion():
    ranks = pd.DataFrame(
        {"A": [0.9], "B": [np.nan], "C": [0.1]}, index=pd.to_datetime(["2026-01-05"])
    )
    features = meta_features(ranks, ["A", "B", "C"], min_coverage=1)
    assert features.loc[pd.Timestamp("2026-01-05"), "coverage"] == 2
    assert features.loc[pd.Timestamp("2026-01-05"), "r_B"] == pytest.approx(0.5)
    assert features.loc[pd.Timestamp("2026-01-05"), "dispersion"] == pytest.approx(
        pd.Series([0.9, 0.1]).std()
    )
    assert meta_features(ranks, ["A", "B", "C"], min_coverage=3).empty
