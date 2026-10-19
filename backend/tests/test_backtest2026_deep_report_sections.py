"""深度报告叙述层必须使用当前产物计算，而不是上一轮运行的硬编码结论。

三类历史事故都在这里兜住：季度换人口径写成「单年行情=是」、共识池规模
停留在旧窗口的 0.4–0.6 只、容量结论把非单调的差距说成「越紧越大」。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backend.scripts.backtest2026.deep_report_back import (
    capacity_section,
    consensus_section,
    quarterly_section,
)
from backend.scripts.backtest2026.deep_report_data import DeepBundle


@pytest.mark.unit
def test_quarterly_callout_reports_computed_ranges_and_change_counts() -> None:
    quarterly_rank = pd.DataFrame(
        [
            {"quarter": "2025Q1", "model": "LGB", "ic_mean": 0.30, "excess_mean": 0.0},
            {"quarter": "2025Q1", "model": "XGB", "ic_mean": 0.20, "excess_mean": 0.0},
            {"quarter": "2025Q1", "model": "CAT", "ic_mean": 0.10, "excess_mean": 0.0},
            {"quarter": "2025Q2", "model": "LGB", "ic_mean": 0.10, "excess_mean": 0.0},
            {"quarter": "2025Q2", "model": "XGB", "ic_mean": 0.20, "excess_mean": 0.0},
            {"quarter": "2025Q2", "model": "CAT", "ic_mean": 0.30, "excess_mean": 0.0},
            {"quarter": "2025Q3", "model": "LGB", "ic_mean": 0.30, "excess_mean": 0.0},
            {"quarter": "2025Q3", "model": "XGB", "ic_mean": 0.20, "excess_mean": 0.0},
            {"quarter": "2025Q3", "model": "CAT", "ic_mean": 0.10, "excess_mean": 0.0},
        ]
    )
    champions = pd.DataFrame(
        [
            {
                "quarter": "2025Q1",
                "champion_ic": "LGB",
                "champion_excess": "CAT",
                "champion_sharpe": "XGB",
                "ic_changed": True,
                "excess_changed": True,
                "sharpe_changed": True,
            },
            {
                "quarter": "2025Q2",
                "champion_ic": "LGB",
                "champion_excess": "XGB",
                "champion_sharpe": "CAT",
                "ic_changed": False,
                "excess_changed": True,
                "sharpe_changed": True,
            },
            {
                "quarter": "2025Q3",
                "champion_ic": "CAT",
                "champion_excess": "XGB",
                "champion_sharpe": "CAT",
                "ic_changed": True,
                "excess_changed": False,
                "sharpe_changed": False,
            },
        ]
    )
    bundle = DeepBundle(
        run_dir=Path("."),
        quarterly_rank={5: quarterly_rank},
        quarterly_champions={5: champions},
    )

    html = quarterly_section(bundle, charts={})

    assert "T+5 -1.00 到 -1.00（2 次比较中 2 次转负）" in html
    assert "分别换人 1/2、1/2、1/2 次" in html
    assert "单季冠军不可延续" in html
    assert "单年行情" not in html


@pytest.mark.unit
def test_consensus_callout_reports_current_half_pool_size() -> None:
    distribution = pd.DataFrame(
        [
            {
                "consensus": 1,
                "n_stock_days": 100,
                "share": 0.91,
                "mean_ret": 0.01,
                "mean_excess": 0.003,
                "hit_rate": 0.5,
            },
            {
                "consensus": 13,
                "n_stock_days": 0,
                "share": 0.0,
                "mean_ret": np.nan,
                "mean_excess": np.nan,
                "hit_rate": np.nan,
            },
        ]
    )
    daily = pd.DataFrame(
        {
            "union": [10.0, 12.0],
            "ge2": [2.0, 3.0],
            "ge_half": [0.0, 1.0],
            "all_models": [0.0, 0.0],
            "avg_model_in_half": [0.01, 0.03],
        }
    )
    bundle = DeepBundle(
        run_dir=Path("."),
        consensus={"distribution_5": distribution, "daily_5": daily},
    )

    html = consensus_section(bundle, charts={})

    assert "T+5 0.40 只（约 2.0%）" in html
    assert "全部 13 个模型都选中的股票-日为零" in html
    assert "0.4–0.6 只" not in html


@pytest.mark.unit
def test_capacity_callout_uses_monotonic_free_vs_constrained_ranges() -> None:
    rows = []
    for horizon, values in {
        5: {
            (2_000_000, 0.01): -0.012820,
            (2_000_000, "none"): 0.007964,
            (10_000_000, 0.01): -0.085656,
            (10_000_000, "none"): 0.008777,
            (50_000_000, 0.01): -0.039523,
            (50_000_000, "none"): 0.008996,
            (100_000_000, 0.01): -0.030053,
            (100_000_000, "none"): 0.008898,
        },
        10: {
            (2_000_000, 0.01): -0.002860,
            (2_000_000, "none"): 0.026372,
            (10_000_000, 0.01): -0.087520,
            (10_000_000, "none"): 0.023566,
            (50_000_000, 0.01): -0.054381,
            (50_000_000, "none"): 0.022547,
            (100_000_000, 0.01): -0.043085,
            (100_000_000, "none"): 0.022407,
        },
    }.items():
        for (capital, participation), mean_diff in values.items():
            rows.append(
                {
                    "horizon": horizon,
                    "capital": capital,
                    "participation": participation,
                    "mean_diff": mean_diff,
                }
            )
    bundle = DeepBundle(run_dir=Path("."), capacity={"advantage": pd.DataFrame(rows)})

    html = capacity_section(bundle, charts={})

    assert "-1.3% 至 -0.3%" in html
    assert "-8.8% 至 -8.6%" in html
    assert "-5.4% 至 -3.0%" in html
    assert "+0.8% 至 +2.6%" in html
    assert "参与率约束越紧" not in html
