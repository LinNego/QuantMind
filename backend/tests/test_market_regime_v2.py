"""大盘/行业预测模型 v2 的纯函数测试（校准 / 标签 / 数据合并）。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backend.scripts.market_regime.build_index_regime_features import (
    _add_risk_labels,
    _load_etf_features,
    _merge_external,
    _rolling_mdd,
)
from backend.scripts.market_regime.data_sources import pit_membership_asof
from backend.scripts.market_regime.calibration import (
    brier_skill,
    ece,
    prior_shift,
    reliability_table,
    select_calibrator,
)
from backend.scripts.market_regime.train_index_regime import (
    _feature_columns,
    _kill_criteria,
    _rolling_base_rate,
    _test_windows,
)

pytestmark = pytest.mark.unit


def test_ece_zero_for_perfectly_calibrated_bins():
    # 每个分箱内预测均值 = 实际上涨率
    y = np.array([0, 0, 0, 1, 1, 1, 1, 0], dtype=float)
    p = np.array([0.25, 0.25, 0.25, 0.25, 0.75, 0.75, 0.75, 0.75])
    assert ece(y, p) == pytest.approx(0.0, abs=1e-9)


def test_brier_skill_positive_when_model_beats_base_rate():
    y = np.array([0, 0, 0, 1, 1, 1], dtype=float)
    p = np.array([0.1, 0.2, 0.2, 0.8, 0.9, 0.8])
    assert brier_skill(y, p, float(y.mean())) > 0


def test_prior_shift_moves_mean_and_preserves_ranking():
    p = np.array([0.2, 0.5, 0.8])
    shifted = prior_shift(p, source_rate=0.5, target_rate=0.3)
    assert shifted.mean() < p.mean()
    assert np.all(np.diff(shifted) > 0)


def test_reliability_table_counts_cover_all_samples():
    y = np.array([0, 1, 0, 1, 1])
    p = np.array([0.15, 0.25, 0.55, 0.85, 0.95])
    table = reliability_table(y, p)
    assert sum(row["n"] for row in table) == len(y)
    assert all(0 <= row["y_mean"] <= 1 for row in table)


def test_select_calibrator_improves_overconfident_scores():
    rng = np.random.default_rng(7)
    y = rng.binomial(1, 0.4, size=800).astype(int)
    # 原始概率过度自信：正例 0.9、负例 0.1，但整体基础率 0.4
    raw = np.where(y == 1, 0.9, 0.1) + rng.normal(0, 0.02, size=len(y))
    raw = np.clip(raw, 1e-4, 1 - 1e-4)
    raw_test = raw[:200]
    choice = select_calibrator(
        raw[200:], y[200:], raw_test, target_rate=0.4, seed=11
    )
    assert choice.p_test.shape == raw_test.shape
    assert np.all((choice.p_test > 0) & (choice.p_test < 1))
    assert ece(y[:200], choice.p_test) <= ece(y[:200], raw_test) + 0.02
    assert choice.candidates


def test_select_calibrator_falls_back_when_validation_single_class():
    raw_val = np.array([0.2, 0.3, 0.4, 0.5] * 20)
    y_val = np.zeros(80, dtype=int)
    raw_test = np.array([0.1, 0.9])
    choice = select_calibrator(raw_val, y_val, raw_test)
    assert choice.name == "raw"
    assert np.allclose(choice.p_test, raw_test)


def test_calibration_never_collapses_ranking():
    rng = np.random.default_rng(21)
    y = rng.binomial(1, 0.45, size=600).astype(int)
    raw = np.clip(0.2 + 0.6 * y + rng.normal(0, 0.08, size=600), 1e-3, 1 - 1e-3)
    raw_test = np.array([0.05, 0.2, 0.4, 0.6, 0.8, 0.95])
    choice = select_calibrator(
        raw[:400], y[:400], raw_test, target_rates={"base_252": 0.45}
    )
    # 最终校准器必须是单调保序（λ>0 收缩 / Platt），不能退化为常数
    assert np.all(np.diff(choice.p_test) > 0)
    assert len(set(np.round(choice.p_test, 6))) > 1


def test_rolling_mdd_matches_manual_window():
    close = pd.Series([100.0, 110.0, 90.0, 95.0, 105.0, 100.0, 120.0])
    mdd = _rolling_mdd(close, 5)
    assert mdd.iloc[0] == pytest.approx(90.0 / 110.0 - 1)
    assert mdd.iloc[1] == pytest.approx(100.0 / 105.0 - 1)
    assert pd.isna(mdd.iloc[2])  # 窗口越过序列末尾


def test_risk_labels_use_only_future_window():
    n = 320
    rng = np.random.default_rng(3)
    close = pd.Series(100 * np.cumprod(1 + rng.normal(0, 0.01, n)))
    df = pd.DataFrame({"close": close})
    out = _add_risk_labels(df.copy(), (5,))
    # fwd_vol_5[T] = T+1..T+5 的日收益标准差（年化）
    ret = close.pct_change()
    expected = ret.iloc[101:106].std() * np.sqrt(252)
    assert out["fwd_vol_5"].iloc[100] == pytest.approx(expected, rel=1e-9)
    # 最后 5 行标签必须缺失
    assert out["fwd_vol_5"].iloc[-5:].isna().all()


def test_vol_high_threshold_is_point_in_time():
    n = 320
    rng = np.random.default_rng(5)
    close = pd.Series(100 * np.cumprod(1 + rng.normal(0, 0.01, n)))
    base = _add_risk_labels(pd.DataFrame({"close": close}), (5,))
    changed = close.copy()
    changed.iloc[250:] *= 1.5  # 改动未来价格
    perturbed = _add_risk_labels(pd.DataFrame({"close": changed}), (5,))
    # T=150 的阈值只依赖 <=T 的 rv20，未来改动不能影响它
    assert base["vol_high_5"].iloc[150] == perturbed["vol_high_5"].iloc[150]


def test_feature_columns_exclude_all_label_prefixes():
    df = pd.DataFrame(
        {
            "trade_date": pd.date_range("2024-01-01", periods=2),
            "index_code": ["000300.SH", "000300.SH"],
            "ret_5": [0.1, 0.2],
            "fwd_ret_5": [0.3, 0.4],
            "fwd_vol_5": [0.2, 0.3],
            "fwd_mdd_5": [-0.1, -0.2],
            "vol_high_5": [0, 1],
            "down_risk_5": [0, 0],
            "index_name": ["沪深300", "沪深300"],
        }
    )
    cols = _feature_columns(df)
    assert cols == ["ret_5"]


def test_test_windows_half_year():
    windows = _test_windows([2024, 2025], "half")
    assert len(windows) == 4
    assert windows[0][0] == pd.Timestamp("2024-01-01")
    assert windows[1][1] == pd.Timestamp("2024-12-31")


def test_rolling_base_rate_uses_latest_observed_labels():
    fit = pd.DataFrame(
        {
            "trade_date": pd.date_range("2024-01-01", periods=6),
            "fwd_ret_5": [0.1, -0.1, 0.1, 0.1, -0.1, 0.1],
        }
    )
    rate = _rolling_base_rate(fit, "direction", 5, window=3)
    assert rate == pytest.approx(2 / 3, abs=1e-6)


def test_kill_criteria_requires_all_conditions():
    def summary(auc, folds_ge, skill, ece_val):
        return {
            "lgb": {
                "auc_mean": auc,
                "auc_raw_mean": auc,
                "auc_folds_ge_pass": folds_ge,
                "brier_skill_mean": skill,
                "ece_mean": ece_val,
                "pooled": {},
            }
        }

    report = {
        "target": "direction",
        "by_index": {
            "A": {"h5": {"summary": summary(0.62, 0.8, 0.10, 0.03)}},
            "B": {"h5": {"summary": summary(0.62, 0.8, 0.10, 0.20)}},
        },
    }
    result = _kill_criteria(report)
    assert result["total"] == 2
    assert result["passed"] == 1
    assert result["rows"][0]["passed"] is True
    assert result["rows"][1]["passed"] is False


def test_load_etf_features_uses_available_date(tmp_path):
    path = tmp_path / "etf.parquet"
    pd.DataFrame(
        {
            "snapshot_date": pd.to_datetime(["2024-01-31", "2024-02-29"]),
            "available_date": pd.to_datetime(["2024-02-01", "2024-03-01"]),
            "etf_share_total": [100.0, 110.0],
            "etf_share_total_chg_1m": [np.nan, 0.1],
            "unrelated": [1, 2],
        }
    ).to_parquet(path, index=False)
    out = _load_etf_features(path)
    assert list(out["trade_date"]) == [
        pd.Timestamp("2024-02-01"),
        pd.Timestamp("2024-03-01"),
    ]
    assert "unrelated" not in out.columns


def test_merge_external_is_asof_and_respects_etf_availability(tmp_path):
    ext = tmp_path / "external"
    ext.mkdir()
    pd.DataFrame(
        {
            "trade_date": pd.to_datetime(["2024-01-02", "2024-01-05"]),
            "shibor_on": [1.0, 2.0],
        }
    ).to_parquet(ext / "macro_daily.parquet", index=False)
    pd.DataFrame(
        {
            "snapshot_date": pd.to_datetime(["2024-01-04"]),
            "available_date": pd.to_datetime(["2024-01-04"]),
            "etf_share_total": [100.0],
        }
    ).to_parquet(ext / "etf_shares_monthly.parquet", index=False)
    pd.DataFrame(
        {
            "trade_date": pd.to_datetime(["2024-01-02", "2024-01-05"]),
            "if_basis": [0.01, 0.02],
        }
    ).to_parquet(ext / "index_futures_basis.parquet", index=False)

    base = pd.DataFrame(
        {"trade_date": pd.to_datetime(["2024-01-03", "2024-01-06"])}
    )
    out = _merge_external(base, ext)
    assert out["shibor_on"].tolist() == [1.0, 2.0]
    assert pd.isna(out["etf_share_total"].iloc[0])
    assert out["etf_share_total"].iloc[1] == 100.0
    assert out["if_basis"].tolist() == [0.01, 0.02]


def test_pit_membership_asof_uses_latest_effective_record():
    membership = pd.DataFrame(
        {
            "symbol_prefix": ["SZ000001", "SZ000001", "SH600519"],
            "start_date": pd.to_datetime(
                ["2014-01-01", "2021-07-30", "2001-07-31"]
            ),
            "industry_code": ["440101", "480301", "340301"],
            "industry_l1": ["44", "48", "34"],
        }
    )
    old = pit_membership_asof(membership, "2020-01-01").set_index("symbol_prefix")
    new = pit_membership_asof(membership, "2022-01-01").set_index("symbol_prefix")
    assert old.loc["SZ000001", "industry_code"] == "440101"
    assert new.loc["SZ000001", "industry_code"] == "480301"
    assert new.loc["SH600519", "industry_code"] == "340301"

