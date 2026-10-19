"""滚动 Top5 逐日归因：选股 / 行业暴露 / 市值暴露 / 现金拖累。

口径：
- 组合权重由成交重建，d 日收益使用 d-1 日收盘后的持仓；
- 基准为当日可投资域的等权组合；
- 日截面回归 ``r_i = β_industry + γ·size_z + ε``，三块贡献相加等于
  「已投资部分相对等权域的超额」，现金拖累单列；
- 组合权重先按投资比例归一，再乘回投资比例，避免现金被错误地当成一只股票。
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from .config import OUT_DIR
from .data import (
    attach_listing_age,
    build_market_panel,
    read_first_seen,
)
from .deep_common import (
    TEST_END,
    TEST_START,
    add_period_labels,
    cross_sectional_z,
    decompose_day,
    daily_returns,
    factor_panel,
    load_equity,
    load_industry_map,
    log_size_matrix,
    position_weights,
)
from .model_registry import HORIZONS, MODEL_ORDER, resolve_model_run
from .runner import _write_json

LOG = logging.getLogger("backtest2026.attribution")


@dataclass(frozen=True)
class AttributionSettings:
    run_dir: Path
    out_dir: Path
    start: str = TEST_START
    end: str = TEST_END
    price_start: str = "2024-11-01"
    price_end: str = "2026-10-16"
    models: tuple[str, ...] = MODEL_ORDER
    horizons: tuple[int, ...] = HORIZONS
    family: str = "rolling"


def _merge_residuals(
    factors_residual: pd.Series, held_residual: pd.Series
) -> pd.Series:
    """把持仓残差并入当日残差。

    持仓名可能不在归因域内（缺股本/停牌/次新），``Series.loc`` 赋值会直接
    抛 KeyError；这里按并集合并，持仓值覆盖同名的域内值。
    """
    merged = pd.concat([factors_residual, held_residual])
    return merged[~merged.index.duplicated(keep="last")]


def _held_residual(
    factors_resid: pd.Series,
    weights: pd.Series,
    returns_day: pd.Series,
    size_z_day: pd.Series,
    beta: pd.Series,
    gamma: float,
    industry: pd.Series,
) -> pd.Series:
    """补齐持仓中不在归因域内的残差（停牌/新 ST/次新）。"""
    held = weights[weights > 0].index
    residual = factors_resid.reindex(held)
    missing = residual.isna()
    if missing.any():
        symbols = held[missing]
        ind = industry.reindex(symbols).fillna("未分类")
        base = beta.reindex(ind).fillna(0.0)
        z = size_z_day.reindex(symbols).fillna(0.0)
        r = returns_day.reindex(symbols).fillna(0.0)
        residual.loc[symbols] = (r - base.to_numpy() - gamma * z).to_numpy()
    return residual.astype(float)


def attribute_one(
    model: str,
    horizon: int,
    settings: AttributionSettings,
    panel,
    factor_days,
    residuals,
    size_z,
    industry,
    returns,
) -> tuple[pd.DataFrame, dict[str, object], pd.Series]:
    equity_path = settings.run_dir / f"equity_{settings.family}_{horizon}_{model}.csv"
    trades_path = settings.run_dir / f"trades_{settings.family}_{horizon}_{model}.csv"
    if not equity_path.is_file() or not trades_path.is_file():
        raise FileNotFoundError(f"缺少 {model} T+{horizon} 的净值/成交文件")
    equity = load_equity(equity_path)
    trades = pd.read_csv(trades_path)
    # 从价格面板起点开始重建，确保窗口起点之前已持有的仓位不会丢。
    weights_all = position_weights(trades, equity, panel)
    weights_all = weights_all.loc[
        (weights_all.index >= pd.Timestamp(settings.start))
        & (weights_all.index <= pd.Timestamp(settings.end))
    ]
    common_days = [
        day for day in weights_all.index if day in factor_days and day in returns.index
    ]
    if not common_days:
        raise RuntimeError(f"{model} T+{horizon} 没有可归因的交易日")

    rows: list[dict[str, object]] = []
    industry_totals: dict[str, float] = {}
    for day in common_days:
        factors = factor_days[day]
        weights = weights_all.loc[day]
        returns_day = returns.loc[day]
        residual = _held_residual(
            residuals.loc[day],
            weights,
            returns_day,
            size_z.loc[day],
            factors.beta_industry,
            factors.gamma_size,
            industry,
        )
        # 用补齐后的残差替换当天残差，只影响持仓股票。
        adjusted = _merge_residuals(factors.residual, residual)
        factors = type(factors)(
            beta_industry=factors.beta_industry,
            gamma_size=factors.gamma_size,
            residual=adjusted,
            universe_return=factors.universe_return,
            universe_industry_weight=factors.universe_industry_weight,
            universe_size=factors.universe_size,
            n_universe=factors.n_universe,
        )
        row = decompose_day(weights, factors, industry, size_z.loc[day])
        held = weights[weights > 0]
        invested = float(weights.sum())
        if invested > 0:
            gross = (
                float((held / invested) @ returns_day.reindex(held.index).fillna(0.0))
                * invested
            )
        else:
            gross = 0.0
        gross_excess = gross - factors.universe_return
        component_sum = float(row["component_sum"])
        rows.append(
            {
                "date": day,
                "invested": invested,
                "universe_return": factors.universe_return,
                "portfolio_gross_return": gross,
                "gross_excess": gross_excess,
                "industry": float(row["industry"]),
                "size": float(row["size"]),
                "selection": float(row["selection"]),
                "cash_drag": float(row["cash_drag"]),
                "component_sum": component_sum,
                "decomp_error": component_sum - gross_excess,
                "size_exposure": float(row["size_exposure"]),
                "industry_hhi": float(row["industry_hhi"]),
                "n_holdings": int((held > 0).sum()),
            }
        )
        detail = row["industry_detail"]
        if isinstance(detail, pd.Series):
            for name, value in detail.items():
                industry_totals[str(name)] = industry_totals.get(
                    str(name), 0.0
                ) + float(value)

    daily = pd.DataFrame(rows).set_index("date").sort_index()
    for column in ("industry", "size", "selection", "cash_drag", "gross_excess"):
        daily[f"cum_{column}"] = daily[column].cumsum()

    n = len(daily)
    total = float(daily["gross_excess"].sum())
    components = {
        "industry": float(daily["industry"].sum()),
        "size": float(daily["size"].sum()),
        "selection": float(daily["selection"].sum()),
        "cash_drag": float(daily["cash_drag"].sum()),
    }
    summary = {
        "model": model,
        "horizon": horizon,
        "family": settings.family,
        "days": n,
        "start": str(daily.index.min().date()),
        "end": str(daily.index.max().date()),
        "gross_excess_sum": total,
        "gross_excess_annualized": total / n * 252.0 if n else float("nan"),
        "industry_sum": components["industry"],
        "size_sum": components["size"],
        "selection_sum": components["selection"],
        "cash_drag_sum": components["cash_drag"],
        "industry_share": components["industry"] / total if total else float("nan"),
        "size_share": components["size"] / total if total else float("nan"),
        "selection_share": components["selection"] / total if total else float("nan"),
        "cash_drag_share": components["cash_drag"] / total if total else float("nan"),
        "avg_invested": float(daily["invested"].mean()),
        "avg_size_exposure": float(daily["size_exposure"].mean()),
        "avg_industry_hhi": float(daily["industry_hhi"].mean()),
        "max_abs_decomp_error": float(daily["decomp_error"].abs().max()),
        "mean_abs_decomp_error": float(daily["decomp_error"].abs().mean()),
        "avg_holdings": float(daily["n_holdings"].mean()),
    }
    industry_series = pd.Series(industry_totals, name="contribution").sort_values(
        ascending=False
    )
    return daily, summary, industry_series


def _quarter_table(daily: pd.DataFrame) -> pd.DataFrame:
    labels = add_period_labels(daily.index)
    frame = daily.copy()
    frame["quarter"] = labels["quarter"]
    columns = ["industry", "size", "selection", "cash_drag", "gross_excess"]
    grouped = frame.groupby("quarter")[columns].sum()
    grouped["days"] = frame.groupby("quarter").size()
    return grouped.reset_index()


def run(settings: AttributionSettings) -> dict[str, object]:
    settings.out_dir.mkdir(parents=True, exist_ok=True)
    canonical = resolve_model_run(5, "LGB")
    if canonical.run_dir is None:
        raise RuntimeError("缺少规范模型运行目录")
    panel = build_market_panel(settings.price_start, settings.price_end)
    attach_listing_age(panel, read_first_seen(canonical.run_dir))
    LOG.info("归因窗口 %s ~ %s", settings.start, settings.end)
    factor_days, residuals = factor_panel(panel, start=settings.start, end=settings.end)
    size_z = cross_sectional_z(log_size_matrix(panel))
    industry = load_industry_map()
    returns = daily_returns(panel)
    LOG.info("归因交易日 %d", len(factor_days))

    summaries: list[dict[str, object]] = []
    quarter_rows: list[pd.DataFrame] = []
    for horizon in settings.horizons:
        for model in settings.models:
            try:
                daily, summary, industry_series = attribute_one(
                    model,
                    horizon,
                    settings,
                    panel,
                    factor_days,
                    residuals,
                    size_z,
                    industry,
                    returns,
                )
            except FileNotFoundError as exc:
                LOG.warning("%s", exc)
                continue
            daily.to_csv(settings.out_dir / f"attribution_decomp_{horizon}_{model}.csv")
            industry_series.to_csv(
                settings.out_dir / f"attribution_decomp_industry_{horizon}_{model}.csv",
                header=True,
            )
            quarter = _quarter_table(daily)
            quarter.insert(0, "model", model)
            quarter.insert(0, "horizon", horizon)
            quarter_rows.append(quarter)
            summaries.append(summary)
            LOG.info(
                "%s T+%d 归因完成：超额 %.3f（选股 %.3f / 行业 %.3f / 市值 %.3f / 现金 %.3f）",
                model,
                horizon,
                summary["gross_excess_sum"],
                summary["selection_sum"],
                summary["industry_sum"],
                summary["size_sum"],
                summary["cash_drag_sum"],
            )
    summary_frame = pd.DataFrame(summaries)
    summary_frame.to_csv(
        settings.out_dir / "attribution_decomp_summary.csv", index=False
    )
    if quarter_rows:
        pd.concat(quarter_rows, ignore_index=True).to_csv(
            settings.out_dir / "attribution_decomp_quarter.csv", index=False
        )
    _write_json(
        settings.out_dir / "attribution_decomp_manifest.json",
        {
            "run_dir": str(settings.run_dir),
            "start": settings.start,
            "end": settings.end,
            "price_start": settings.price_start,
            "price_end": settings.price_end,
            "family": settings.family,
            "models": list(settings.models),
            "horizons": list(settings.horizons),
        },
    )
    return {"models": len(summaries), "out_dir": str(settings.out_dir)}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=OUT_DIR / "deep_attribution")
    parser.add_argument("--start", default=TEST_START)
    parser.add_argument("--end", default=TEST_END)
    parser.add_argument("--price-start", default="2024-11-01")
    parser.add_argument("--price-end", default="2026-10-16")
    parser.add_argument("--models", nargs="*", default=list(MODEL_ORDER))
    parser.add_argument("--horizons", nargs="*", type=int, default=list(HORIZONS))
    parser.add_argument("--family", default="rolling")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    args = parse_args(argv)
    settings = AttributionSettings(
        run_dir=args.run_dir,
        out_dir=args.out,
        start=args.start,
        end=args.end,
        price_start=args.price_start,
        price_end=args.price_end,
        models=tuple(args.models),
        horizons=tuple(args.horizons),
        family=args.family,
    )
    run(settings)


if __name__ == "__main__":
    main()
