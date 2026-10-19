"""13 个模型 × T+5/T+10 的统一窗口评估。

用法::

    python -m backend.scripts.backtest2026.multi_model \
        --start 2026-01-05 --end 2026-08-26 \
        --out /data/backtest_2026/multi

所有模型读取各自训练产物 ``pred.parquet`` 的同一闭区间，价格面板只构建一次，
因此 IC / 组合收益 / 统计检验都是逐日配对样本。尚未训练完成的组合（默认
T+10 TCN、Transformer）会被列入 ``inventory.csv`` 的 pending 行；加
``--allow-missing`` 可先生成其余模型的报告。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from .config import (
    COMMISSION_RATE,
    INDEX_SYMBOLS,
    INITIAL_CAPITAL,
    OUT_DIR,
    PRICE_END,
    PRICE_START,
)
from .data import (
    MarketPanel,
    attach_listing_age,
    build_market_panel,
    forward_return_matrix,
    load_extended_predictions,
    load_index_closes,
    read_first_seen,
)
from .metrics import index_summary, performance_summary
from .model_registry import (
    HORIZONS,
    MODEL_ORDER,
    ModelRun,
    inventory_rows,
    resolve_registry,
)
from .multi_portfolio import (
    FAMILY_ORDER,
    FAMILY_TITLES,
    PortfolioEvaluation,
    run_family,
)
from .multi_quality import (
    benchmark_daily,
    decile_frame,
    quality_by_day_multi,
    summarize_quality_multi,
)
from .multi_stats import (
    StatsSettings,
    cross_horizon_stats,
    portfolio_stats,
    quality_stats,
)
from .runner import _write_json
from .strategies import build_pred_by_date

LOG = logging.getLogger("backtest2026.multi")

DEFAULT_START = "2026-01-05"
DEFAULT_END = "2026-08-26"


@dataclass(frozen=True)
class RunSettings:
    start: str = DEFAULT_START
    end: str = DEFAULT_END
    out_dir: Path = OUT_DIR / "multi"
    models: tuple[str, ...] = MODEL_ORDER
    horizons: tuple[int, ...] = HORIZONS
    allow_missing: bool = False
    block: int = 20
    n_boot: int = 5000
    seed: int = 20261019
    n_trials: int = 26
    price_start: str = PRICE_START
    price_end: str = PRICE_END
    extension_dir: Path | None = None


@dataclass
class ModelOutput:
    run: ModelRun
    quality_daily: pd.DataFrame
    quality_summary: dict[str, object]
    portfolios: dict[str, PortfolioEvaluation]
    coverage: pd.DataFrame
    latest: pd.Series
    signature: dict[str, object]


def _prediction_signature(frame: pd.DataFrame) -> dict[str, object]:
    dates = np.sort(frame["trade_date"].unique())
    symbols = np.sort(frame["symbol"].unique())
    counts = frame.groupby("symbol")["trade_date"].nunique().sort_index()
    digest = hashlib.sha256()
    digest.update(
        "|".join(pd.Timestamp(day).strftime("%Y-%m-%d") for day in dates).encode()
    )
    digest.update(b"#")
    digest.update("|".join(symbols).encode())
    digest.update(b"#")
    digest.update(
        "|".join(f"{symbol}:{count}" for symbol, count in counts.items()).encode()
    )
    label_available = bool(frame.attrs.get("label_return_available", True))
    return {
        "rows": int(len(frame)),
        "days": int(len(dates)),
        "symbols": int(len(symbols)),
        "hash": digest.hexdigest()[:16],
        "pred_nan": int(frame["pred"].isna().sum()),
        "pred_valid": int(frame["pred"].notna().sum()),
        "label_return_available": label_available,
        "label_nan": int(frame["label_return"].isna().sum())
        if label_available
        else None,
    }


def _first_seen_signature(run_dir: Path) -> dict[str, object]:
    first_seen = read_first_seen(run_dir).sort_index()
    digest = hashlib.sha256()
    for symbol, day in first_seen.items():
        digest.update(f"{symbol}|{pd.Timestamp(day):%Y-%m-%d}\n".encode())
    return {
        "symbols": int(len(first_seen)),
        "hash": digest.hexdigest()[:16],
    }


def _load_frame(run: ModelRun, settings: RunSettings) -> pd.DataFrame:
    if run.run_dir is None:
        raise FileNotFoundError(f"{run.label} 没有运行目录")
    frame = load_extended_predictions(
        run.run_dir,
        run.horizon,
        run.key,
        settings.start,
        settings.end,
        settings.extension_dir,
    )
    duplicates = frame.duplicated(subset=["trade_date", "symbol"]).sum()
    if duplicates:
        raise RuntimeError(
            f"{run.label} 预测面板存在 {duplicates} 条重复 (date, symbol)"
        )
    return frame


def _monthly_wide(
    values: dict[str, dict[str, float]],
    models: list[str],
) -> pd.DataFrame:
    return pd.DataFrame(
        {model: pd.Series(values[model]) for model in models}
    ).sort_index()


def _quality_monthly_wide(
    summaries: dict[str, dict[str, object]],
    models: list[str],
    field: str,
) -> pd.DataFrame:
    columns = {}
    for model in models:
        monthly = summaries[model]["monthly"]
        columns[model] = pd.Series(
            {month: float(payload[field]) for month, payload in monthly.items()}
        )
    return pd.DataFrame(columns).sort_index()


def _write_equity(
    evaluation: PortfolioEvaluation,
    benchmark: pd.DataFrame,
    path: Path,
) -> None:
    benchmark_return = (
        benchmark["return"].reindex(evaluation.daily_returns.index).fillna(0.0)
    )
    benchmark_equity = INITIAL_CAPITAL * (1.0 + benchmark_return).cumprod()
    frame = pd.DataFrame(
        {
            "equity": evaluation.equity_common,
            "daily_return": evaluation.daily_returns,
            "benchmark_return": benchmark_return,
            "benchmark_equity": benchmark_equity,
            "excess": evaluation.excess,
        }
    )
    frame.to_csv(path)


def _write_model_outputs(
    output: ModelOutput,
    benchmark: pd.DataFrame,
    settings: RunSettings,
) -> None:
    horizon = output.run.horizon
    key = output.run.key
    output.quality_daily.to_csv(settings.out_dir / f"quality_daily_{horizon}_{key}.csv")
    for family, evaluation in output.portfolios.items():
        _write_equity(
            evaluation,
            benchmark,
            settings.out_dir / f"equity_{family}_{horizon}_{key}.csv",
        )
        evaluation.trades_frame.to_csv(
            settings.out_dir / f"trades_{family}_{horizon}_{key}.csv", index=False
        )
        evaluation.attribution.to_csv(
            settings.out_dir / f"attribution_{family}_{horizon}_{key}.csv"
        )


def _run_model(
    run: ModelRun,
    panel: MarketPanel,
    forward: dict[int, pd.DataFrame],
    benchmark: pd.DataFrame,
    common_end: pd.Timestamp,
    settings: RunSettings,
) -> ModelOutput:
    LOG.info("评估 %s (%s)", run.label, run.run_dir.name if run.run_dir else "—")
    frame = _load_frame(run, settings)
    signature = _prediction_signature(frame)
    pred_by_date = build_pred_by_date(frame)
    quality_daily = quality_by_day_multi(pred_by_date, forward[run.horizon], panel)
    quality_summary = summarize_quality_multi(
        quality_daily,
        horizon=run.horizon,
        block=settings.block,
        n_boot=settings.n_boot,
        seed=settings.seed,
    )
    portfolios = {}
    for family in FAMILY_ORDER:
        portfolios[family] = run_family(
            panel,
            frame,
            family=family,
            horizon=run.horizon,
            model=run.key,
            benchmark_returns=benchmark["return"],
            common_end=common_end,
            pred_by_date=pred_by_date,
        )
    coverage = frame.groupby("symbol").agg(
        rows=("pred", "size"),
        days=("pred", "count"),
        first_date=("trade_date", "min"),
        last_date=("trade_date", "max"),
        mean_pred=("pred", "mean"),
    )
    latest = (
        frame.sort_values("trade_date")
        .groupby("symbol")
        .tail(1)
        .set_index("symbol")["pred"]
    )
    return ModelOutput(
        run=run,
        quality_daily=quality_daily,
        quality_summary=quality_summary,
        portfolios=portfolios,
        coverage=coverage,
        latest=latest,
        signature=signature,
    )


def _build_per_stock(
    outputs: list[ModelOutput],
    panel: MarketPanel,
    settings: RunSettings,
) -> pd.DataFrame:
    canonical = outputs[0].coverage
    index = panel.symbols
    frame = pd.DataFrame(index=index)
    frame.index.name = "symbol"
    frame["is_st"] = panel.is_st.reindex(index).fillna(False).astype(bool)
    last_day = panel.dates[-1]
    frame["last_price"] = [panel.mark_price(str(symbol), last_day) for symbol in index]
    frame["days"] = canonical["days"].reindex(index)
    frame["first_date"] = canonical["first_date"].reindex(index)
    frame["last_date"] = canonical["last_date"].reindex(index)
    for output in outputs:
        suffix = f"{output.run.horizon}_{output.run.key}"
        frame[f"valid_days_{suffix}"] = output.coverage["days"].reindex(index)
        frame[f"mean_pred_{suffix}"] = output.coverage["mean_pred"].reindex(index)
        frame[f"latest_pred_{suffix}"] = output.latest.reindex(index)
    latest_columns = [
        f"latest_pred_{output.run.horizon}_{output.run.key}" for output in outputs
    ]
    frame["status"] = np.where(
        frame[latest_columns].notna().any(axis=1), "predicted", "never_predicted"
    )
    return frame


def _write_per_stock(
    frame: pd.DataFrame,
    outputs: list[ModelOutput],
    settings: RunSettings,
) -> None:
    frame.to_csv(settings.out_dir / "per_stock_coverage.csv")
    for horizon in settings.horizons:
        columns = [
            f"latest_pred_{horizon}_{output.run.key}"
            for output in outputs
            if output.run.horizon == horizon
        ]
        if not columns:
            continue
        table = frame[columns].copy()
        table.columns = [column.rsplit("_", 1)[-1] for column in columns]
        table.to_csv(settings.out_dir / f"per_stock_latest_{horizon}.csv")


def _inventory(
    registry: dict[int, dict[str, ModelRun]],
    selected_models: tuple[str, ...],
    horizons: tuple[int, ...],
) -> pd.DataFrame:
    rows = [
        row
        for row in inventory_rows(registry)
        if row["key"] in selected_models and int(row["horizon"]) in horizons
    ]
    return pd.DataFrame(rows)


def _missing_runs(
    registry: dict[int, dict[str, ModelRun]],
    settings: RunSettings,
) -> list[ModelRun]:
    missing = []
    for horizon in settings.horizons:
        for key in settings.models:
            run = registry[horizon][key]
            if not run.ready:
                missing.append(run)
    return missing


def _benchmark_block(
    benchmark: pd.DataFrame,
    index_closes: pd.DataFrame,
    common_start: pd.Timestamp,
    common_end: pd.Timestamp,
) -> tuple[dict[str, object], dict[str, float]]:
    performance = performance_summary(benchmark["equity"])
    performance["n_pool_mean"] = float(benchmark.loc[benchmark["n"] > 0, "n"].mean())
    indices = index_summary(index_closes, common_start, common_end)
    return performance, indices


def run(settings: RunSettings) -> dict[str, object]:
    settings.out_dir.mkdir(parents=True, exist_ok=True)
    registry = resolve_registry()
    missing = _missing_runs(registry, settings)
    if missing and not settings.allow_missing:
        labels = ", ".join(run.label for run in missing)
        raise RuntimeError(
            f"模型尚未就绪：{labels}（可加 --allow-missing 先跑其余模型）"
        )
    outputs_runs = [
        registry[horizon][key]
        for horizon in settings.horizons
        for key in settings.models
        if registry[horizon][key].ready
    ]
    if not outputs_runs:
        raise RuntimeError("没有可评估的模型")

    LOG.info("构建价格面板 %s ~ %s", settings.price_start, settings.price_end)
    panel = build_market_panel(settings.price_start, settings.price_end)
    canonical = outputs_runs[0]
    if canonical.run_dir is None:
        raise RuntimeError("规范模型缺少运行目录")
    canonical_first_seen = _first_seen_signature(canonical.run_dir)
    attach_listing_age(panel, read_first_seen(canonical.run_dir))
    canonical_frame = _load_frame(canonical, settings)
    canonical_signature = _prediction_signature(canonical_frame)
    canonical_pred_by_date = build_pred_by_date(canonical_frame)
    benchmark = benchmark_daily(panel, canonical_pred_by_date, INITIAL_CAPITAL, lag=2)
    common_start = benchmark.index[0]
    common_end = benchmark.index[-1]
    LOG.info(
        "共同窗口 %s ~ %s；信号日 %d；基准日 %d",
        common_start.strftime("%Y-%m-%d"),
        common_end.strftime("%Y-%m-%d"),
        len(canonical_pred_by_date),
        len(benchmark),
    )

    forward = {
        horizon: forward_return_matrix(panel.close_qfq, horizon)
        for horizon in settings.horizons
    }
    index_closes = load_index_closes(settings.price_start, settings.price_end)
    wanted = [f"{code[-2:]}{code[:6]}" for code in INDEX_SYMBOLS]
    index_closes = index_closes[
        [column for column in index_closes.columns if column in wanted]
    ]
    benchmark_performance, indices = _benchmark_block(
        benchmark, index_closes, common_start, common_end
    )
    benchmark.to_csv(settings.out_dir / "benchmark.csv")
    pd.Series(indices, name="total_return").to_csv(settings.out_dir / "indices.csv")

    outputs: list[ModelOutput] = []
    first_seen_hashes: dict[str, str] = {}
    for run_spec in outputs_runs:
        if run_spec.run_dir is None:
            continue
        seen = _first_seen_signature(run_spec.run_dir)
        first_seen_hashes[f"{run_spec.horizon}_{run_spec.key}"] = str(seen["hash"])
        if seen["hash"] != canonical_first_seen["hash"]:
            raise RuntimeError(
                f"{run_spec.label} 的上市日代理与规范模型不一致："
                f"{seen['hash']} != {canonical_first_seen['hash']}"
            )
        output = _run_model(run_spec, panel, forward, benchmark, common_end, settings)
        if output.signature["hash"] != canonical_signature["hash"]:
            raise RuntimeError(
                f"{run_spec.label} 预测面板与规范模型不一致："
                f"{output.signature['hash']} != {canonical_signature['hash']}"
            )
        _write_model_outputs(output, benchmark, settings)
        outputs.append(output)

    models_by_horizon = {
        horizon: [output.run.key for output in outputs if output.run.horizon == horizon]
        for horizon in settings.horizons
    }
    quality_by_horizon = {
        horizon: {
            output.run.key: output.quality_daily
            for output in outputs
            if output.run.horizon == horizon
        }
        for horizon in settings.horizons
    }
    summaries_by_horizon = {
        horizon: {
            output.run.key: output.quality_summary
            for output in outputs
            if output.run.horizon == horizon
        }
        for horizon in settings.horizons
    }
    evaluations_by_horizon = {
        horizon: {
            family: {
                output.run.key: output.portfolios[family]
                for output in outputs
                if output.run.horizon == horizon
            }
            for family in FAMILY_ORDER
        }
        for horizon in settings.horizons
    }

    stats_settings = StatsSettings(
        block=settings.block,
        n_boot=settings.n_boot,
        seed=settings.seed,
        n_trials=settings.n_trials,
    )
    stats: dict[str, object] = {}
    for horizon in settings.horizons:
        models = models_by_horizon[horizon]
        quality_block = quality_stats(
            quality_by_horizon[horizon],
            summaries_by_horizon[horizon],
            models,
            settings=stats_settings,
        )
        portfolio_blocks = {
            family: portfolio_stats(
                evaluations_by_horizon[horizon][family],
                models,
                settings=stats_settings,
            )
            for family in FAMILY_ORDER
        }
        stats[str(horizon)] = {
            "quality": quality_block,
            "portfolios": portfolio_blocks,
        }
        pd.DataFrame(quality_block["ranking"]).to_csv(
            settings.out_dir / f"quality_league_{horizon}.csv", index=False
        )
        _quality_monthly_wide(summaries_by_horizon[horizon], models, "ic").to_csv(
            settings.out_dir / f"quality_monthly_ic_{horizon}.csv"
        )
        _quality_monthly_wide(
            summaries_by_horizon[horizon], models, "top10_hit"
        ).to_csv(settings.out_dir / f"quality_monthly_top10_hit_{horizon}.csv")
        deciles = decile_frame(quality_by_horizon[horizon])
        deciles["monotonicity"] = [
            quality_block["decile_monotonicity"].get(model, float("nan"))
            for model in deciles.index
        ]
        deciles.to_csv(settings.out_dir / f"deciles_{horizon}.csv")
        for family in FAMILY_ORDER:
            pd.DataFrame(portfolio_blocks[family]["ranking"]).to_csv(
                settings.out_dir / f"portfolio_league_{family}_{horizon}.csv",
                index=False,
            )
            _monthly_wide(
                {
                    model: evaluations_by_horizon[horizon][family][model].monthly
                    for model in models
                },
                models,
            ).to_csv(settings.out_dir / f"monthly_returns_{family}_{horizon}.csv")

    common_models = [
        key
        for key in settings.models
        if all(
            key in models_by_horizon.get(horizon, []) for horizon in settings.horizons
        )
    ]
    cross = (
        cross_horizon_stats(summaries_by_horizon, evaluations_by_horizon, common_models)
        if len(settings.horizons) > 1
        else {}
    )
    _write_json(settings.out_dir / "cross_horizon.json", cross)

    per_stock = _build_per_stock(outputs, panel, settings)
    _write_per_stock(per_stock, outputs, settings)

    inventory = _inventory(registry, settings.models, settings.horizons)
    inventory.to_csv(settings.out_dir / "inventory.csv", index=False)

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "start": settings.start,
        "end": settings.end,
        "common_start": common_start.strftime("%Y-%m-%d"),
        "common_end": common_end.strftime("%Y-%m-%d"),
        "signal_days": len(canonical_pred_by_date),
        "initial_capital": INITIAL_CAPITAL,
        "commission_rate": COMMISSION_RATE,
        "stamp_duty_rate": 0.0005,
        "slippage_rate": 0.0005,
        "block": settings.block,
        "n_boot": settings.n_boot,
        "seed": settings.seed,
        "n_trials": settings.n_trials,
        "models": list(settings.models),
        "horizons": list(settings.horizons),
        "price_start": settings.price_start,
        "price_end": settings.price_end,
        "versions": {
            "python": sys.version.split()[0],
            "pandas": pd.__version__,
            "numpy": np.__version__,
        },
    }
    try:
        import scipy

        manifest["versions"]["scipy"] = scipy.__version__
    except ImportError:
        manifest["versions"]["scipy"] = None

    summary = {
        "manifest": manifest,
        "panel": {
            "canonical": canonical.label,
            "signature": canonical_signature,
            "first_seen": canonical_first_seen,
            "first_seen_hashes": first_seen_hashes,
        },
        "benchmark": benchmark_performance,
        "indices": indices,
        "inventory": inventory.to_dict(orient="records"),
        "missing": [
            {"model": run.key, "horizon": run.horizon, "reason": run.reason}
            for run in missing
        ],
        "quality": {
            str(horizon): summaries_by_horizon[horizon] for horizon in settings.horizons
        },
        "portfolios": {
            str(horizon): {
                family: {
                    output.run.key: output.portfolios[family].summary()
                    for output in outputs
                    if output.run.horizon == horizon
                }
                for family in FAMILY_ORDER
            }
            for horizon in settings.horizons
        },
        "stats": stats,
        "cross_horizon": cross,
        "per_stock": {
            "rows": int(len(per_stock)),
            "missing": int((per_stock["status"] != "predicted").sum()),
        },
    }
    _write_json(settings.out_dir / "summary.json", summary)
    LOG.info("完成：%s", settings.out_dir)
    return summary


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default=DEFAULT_END)
    parser.add_argument("--out", type=Path, default=OUT_DIR / "multi")
    parser.add_argument("--models", nargs="*", default=list(MODEL_ORDER))
    parser.add_argument("--horizons", nargs="*", type=int, default=list(HORIZONS))
    parser.add_argument("--allow-missing", action="store_true")
    parser.add_argument("--block", type=int, default=20)
    parser.add_argument("--n-boot", type=int, default=5000)
    parser.add_argument("--seed", type=int, default=20261019)
    parser.add_argument("--n-trials", type=int, default=26)
    parser.add_argument("--price-start", default=PRICE_START)
    parser.add_argument("--price-end", default=PRICE_END)
    parser.add_argument(
        "--extension-dir",
        type=Path,
        default=None,
        help="extend_matrix 产物目录；给定时把外推预测接到训练 pred 之后",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    args = parse_args(argv)
    unknown = [key for key in args.models if key not in MODEL_ORDER]
    if unknown:
        raise SystemExit(f"未知模型: {', '.join(unknown)}")
    settings = RunSettings(
        start=args.start,
        end=args.end,
        out_dir=args.out,
        models=tuple(args.models),
        horizons=tuple(args.horizons),
        allow_missing=args.allow_missing,
        block=args.block,
        n_boot=args.n_boot,
        seed=args.seed,
        n_trials=args.n_trials,
        price_start=args.price_start,
        price_end=args.price_end,
        extension_dir=args.extension_dir,
    )
    run(settings)


if __name__ == "__main__":
    main()
