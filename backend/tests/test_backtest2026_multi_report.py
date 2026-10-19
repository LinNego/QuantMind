"""多模型 PDF 报告的数据装载、图表与 HTML 组装测试。"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backend.scripts.backtest2026.multi_report import build_report
from backend.scripts.backtest2026.multi_report_data import load_bundle

MODELS = ("LGB", "XGB")
HORIZONS = (5, 10)
FAMILIES = ("top10", "top20", "rolling")


def _quality_league(dates: int) -> pd.DataFrame:
    rows = []
    for rank, (model, ic) in enumerate((("LGB", 0.06), ("XGB", 0.04)), 1):
        rows.append(
            {
                "rank": rank,
                "model": model,
                "ic_mean": ic,
                "ic_std": 0.1,
                "icir": ic / 0.1,
                "ic_positive_ratio": 0.6 - rank * 0.05,
                "nw_t": 3.0 - rank,
                "nw_p": 0.01 * rank,
                "ic_ci_low": ic - 0.01,
                "ic_ci_high": ic + 0.01,
                "q_bootstrap": 0.02 * rank,
                "top5_hit": 0.55 - rank * 0.01,
                "top10_hit": 0.54 - rank * 0.01,
                "top20_hit": 0.53 - rank * 0.01,
                "top10_dir": 0.56 - rank * 0.01,
                "top10_ret": 0.02 - rank * 0.002,
                "top20_ret": 0.018 - rank * 0.002,
                "top_bottom_spread": 0.03 - rank * 0.004,
                "decile_monotonicity": 0.8 - rank * 0.1,
            }
        )
    return pd.DataFrame(rows)


def _portfolio_league() -> pd.DataFrame:
    rows = []
    for rank, (model, total) in enumerate((("LGB", 0.12), ("XGB", 0.07)), 1):
        rows.append(
            {
                "rank": rank,
                "model": model,
                "total_return": total,
                "annual_return": total * 1.5,
                "annual_vol": 0.2,
                "sharpe": 1.2 - rank * 0.2,
                "max_drawdown": -0.1 - rank * 0.01,
                "daily_win_rate": 0.53 - rank * 0.01,
                "benchmark_total": 0.03,
                "active_return": total - 0.03,
                "information_ratio": 0.8 - rank * 0.1,
                "tracking_error": 0.1,
                "excess_ci_low": 0.001,
                "excess_ci_high": 0.02,
                "excess_p_positive": 0.04,
                "sortino": 1.1,
                "calmar": 0.9,
                "monthly_win_rate": 0.6,
                "invested_ratio": 0.95,
                "first_half_return": 0.05,
                "second_half_return": 0.06,
                "trade_win_rate": 0.52,
                "trade_net_avg": 0.004,
                "payoff": 1.3,
                "profit_factor": 1.4,
                "n_round_trips": 20,
                "open_positions": 0,
                "q_excess": 0.03,
                "dsr": 0.7 - rank * 0.1,
                "sr0_annual": 0.2,
            }
        )
    return pd.DataFrame(rows)


def _write_artifacts(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    dates = pd.bdate_range("2026-01-05", periods=40)
    summary = {
        "manifest": {
            "horizons": list(HORIZONS),
            "common_start": "2026-01-05",
            "common_end": "2026-02-27",
            "signal_days": len(dates),
            "models": list(MODELS),
        },
        "panel": {
            "signature": {
                "rows": 100,
                "days": len(dates),
                "symbols": 50,
                "pred_nan": 0,
            }
        },
        "benchmark": {"total_return": 0.03, "annual_return": 0.1},
        "inventory": [],
        "quality": {str(horizon): {model: {} for model in MODELS} for horizon in HORIZONS},
        "portfolios": {
            str(horizon): {
                family: {
                    model: {
                        "variants": {
                            "baseline": {"total_return": 0.12},
                            "stamp_duty_5bp": {"total_return": 0.115},
                            "slippage_5bp": {"total_return": 0.11},
                        }
                    }
                    for model in MODELS
                }
                for family in FAMILIES
            }
            for horizon in HORIZONS
        },
        "stats": {
            str(horizon): {
                "quality": {
                    "friedman": {
                        "ic_eligible": {
                            "chi2": 10.0,
                            "p": 0.01,
                            "kendall_w": 0.4,
                            "n_days": len(dates),
                        }
                    },
                    "best_ic_model": "LGB",
                    "pairwise_ic": [{"model": "XGB", "p": 0.1, "q": 0.2}],
                },
                "portfolios": {
                    family: {
                        "friedman_returns": {
                            "chi2": 8.0,
                            "p": 0.02,
                            "kendall_w": 0.3,
                            "n_days": len(dates),
                        },
                        "friedman_excess": {
                            "chi2": 7.0,
                            "p": 0.03,
                            "kendall_w": 0.25,
                            "n_days": len(dates),
                        },
                        "white_reality_check": {
                            "best_model": "LGB",
                            "best_stat": 0.01,
                            "p": 0.04,
                        },
                        "pbo": {"pbo": 0.2, "n_combos": 100},
                        "pairwise_returns": [{"model": "XGB", "p": 0.2, "q": 0.3}],
                        "best_total_return_model": "LGB",
                        "stability": {
                            "first_second_half_rank_corr": 0.5,
                            "both_halves_positive_ratio": 0.5,
                        },
                        "spread": {
                            "best_worst_total_return": 0.05,
                            "best_worst_sharpe": 0.2,
                        },
                    }
                    for family in FAMILIES
                },
            }
            for horizon in HORIZONS
        },
        "cross_horizon": {
            "quality_rank_corr": {"ic_mean": 0.6},
            "portfolio_rank_corr": {
                "top10": {"total_return": 0.5},
            },
            "paired_differences": [
                {
                    "model": "LGB",
                    "delta_ic": 0.01,
                    "delta_top10_hit": 0.02,
                    "delta_top10_return": 0.03,
                    "delta_rolling_return": 0.04,
                }
            ],
        },
    }
    (root / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False), encoding="utf-8"
    )

    ranked = _quality_league(len(dates))
    inventory = []
    for horizon in HORIZONS:
        ranked.to_csv(root / f"quality_league_{horizon}.csv", index=False)
        pd.DataFrame(
            {"2026-01": [0.06, 0.04], "2026-02": [0.05, 0.03]}, index=list(MODELS)
        ).to_csv(root / f"quality_monthly_ic_{horizon}.csv")
        pd.DataFrame(
            {"2026-01": [0.55, 0.53], "2026-02": [0.54, 0.52]}, index=list(MODELS)
        ).to_csv(root / f"quality_monthly_top10_hit_{horizon}.csv")
        deciles = pd.DataFrame(
            {f"D{number}": [0.01 * number, 0.008 * number] for number in range(1, 11)},
            index=list(MODELS),
        )
        deciles["monotonicity"] = [0.8, 0.7]
        deciles.to_csv(root / f"deciles_{horizon}.csv")
        for model in MODELS:
            pd.DataFrame(
                {"ic_eligible": np.linspace(-0.05, 0.12, len(dates))},
                index=dates,
            ).to_csv(root / f"quality_daily_{horizon}_{model}.csv")
            inventory.append(
                {
                    "horizon": horizon,
                    "key": model,
                    "name": model,
                    "family": "树模型",
                    "run_id": f"run_{horizon}_{model}",
                    "job_name": f"T{horizon}_{model}",
                    "model_type": model.lower(),
                    "ready": True,
                    "reason": "",
                }
            )
        for family in FAMILIES:
            league = _portfolio_league()
            league.to_csv(
                root / f"portfolio_league_{family}_{horizon}.csv", index=False
            )
            pd.DataFrame(
                {"LGB": [0.05, 0.07], "XGB": [0.03, 0.04]},
                index=["2026-01", "2026-02"],
            ).to_csv(root / f"monthly_returns_{family}_{horizon}.csv")
            for rank, model in enumerate(MODELS):
                equity = pd.DataFrame(
                    {
                        "equity": 2_000_000
                        * np.cumprod(1 + np.full(len(dates), 0.001 * (2 - rank))),
                        "daily_return": np.full(len(dates), 0.001 * (2 - rank)),
                        "benchmark_return": 0.0,
                        "benchmark_equity": 2_000_000.0,
                        "excess": 0.0,
                    },
                    index=dates,
                )
                equity.to_csv(root / f"equity_{family}_{horizon}_{model}.csv")
    pd.DataFrame(inventory).to_csv(root / "inventory.csv", index=False)

    benchmark = pd.DataFrame(
        {
            "return": np.full(len(dates), 0.0005),
            "n": 100,
            "equity": 2_000_000 * np.cumprod(np.full(len(dates), 1.0005)),
        },
        index=dates,
    )
    benchmark.to_csv(root / "benchmark.csv")

    per_stock = pd.DataFrame(
        {
            "status": ["predicted", "predicted", "never_predicted"],
            "valid_days_5_LGB": [40, 39, 0],
            "latest_pred_5_XGB": [0.1, 0.2, np.nan],
        },
        index=["SH600000", "SH600001", "SH600002"],
    )
    per_stock.to_csv(root / "per_stock_coverage.csv")
    return root


@pytest.mark.unit
def test_load_bundle_reads_ranking_equity_and_per_stock(tmp_path: Path) -> None:
    root = _write_artifacts(tmp_path / "artifacts")

    bundle = load_bundle(root)

    assert bundle.horizons == HORIZONS
    assert bundle.all_models() == list(MODELS)
    assert bundle.quality_row(5, "LGB")["ic_mean"] == pytest.approx(0.06)
    assert bundle.portfolio_row(10, "rolling", "XGB")["total_return"] == pytest.approx(
        0.07
    )
    assert len(bundle.per_stock) == 3
    assert (5, "top10", "LGB") in bundle.equity
    assert bundle.summary["manifest"]["signal_days"] == 40


@pytest.mark.unit
def test_build_report_writes_html_and_charts(tmp_path: Path) -> None:
    root = _write_artifacts(tmp_path / "artifacts")
    out_pdf = root / "report.pdf"

    result = build_report(root, out_pdf, render=False)

    html_path = Path(str(result["html"]))
    assert html_path.is_file()
    html = html_path.read_text(encoding="utf-8")
    assert "执行摘要" in html
    assert "预测质量" in html
    assert "组合收益" in html
    assert "模型差异的严格检验" in html
    assert "LightGBM" in html
    assert result["models"] == list(MODELS)
    assert result["horizons"] == [5, 10]
    assert len(result["charts"]) >= 8
    assert (root / "report_charts" / "m_ic.png").is_file()
    assert "pdf" not in result
