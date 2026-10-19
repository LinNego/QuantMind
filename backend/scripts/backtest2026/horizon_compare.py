"""跨持有期对比：从 multi_model 的 summary.json 提取子集模型的指标。

用于 T+20 对照这类「不是全矩阵」的场景：只读 `multi_model.py --horizons ...`
产出的 summary，输出质量指标表、组合指标表、周期两两秩相关与逐模型差值。
不重新计算任何收益，所有数字都来自那次 multi 运行。
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

import numpy as np
import pandas as pd

from .model_registry import MODEL_ORDER
from .multi_portfolio import FAMILY_ORDER

DEFAULT_HORIZONS = (5, 10, 20)

QUALITY_METRICS = {
    "ic_mean": "ic_eligible_mean",
    "icir": "icir",
    "nw_t": "ic_nw_t",
    "top10_hit": "top10_hit_mean",
    "top20_ret": "top20_ret_mean",
    "top_bottom_spread": "top_bottom_spread",
}

PORTFOLIO_METRICS = (
    "total_return",
    "annual_return",
    "sharpe",
    "max_drawdown",
    "n_round_trips",
)


def load_summary(run_dir: Path) -> dict:
    path = run_dir / "summary.json"
    if not path.is_file():
        raise FileNotFoundError(f"缺少 multi 运行汇总：{path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _available_horizons(summary: Mapping[str, object]) -> tuple[int, ...]:
    quality = summary.get("quality") or {}
    return tuple(sorted(int(key) for key in quality))


def _pick_horizons(
    summary: Mapping[str, object], requested: Sequence[int]
) -> tuple[int, ...]:
    available = set(_available_horizons(summary))
    picked = tuple(horizon for horizon in requested if horizon in available)
    missing = sorted(set(requested) - available)
    if missing:
        raise SystemExit(f"summary 缺少周期：{missing}（可用 {sorted(available)}）")
    return picked


def quality_table(
    summary: Mapping[str, object],
    models: Sequence[str],
    horizons: Sequence[int],
) -> pd.DataFrame:
    quality = summary.get("quality") or {}
    rows = []
    for horizon in horizons:
        block = quality.get(str(horizon)) or {}
        for model in models:
            metrics = block.get(model)
            if not isinstance(metrics, Mapping):
                continue
            rows.append(
                {
                    "model": model,
                    "horizon": int(horizon),
                    **{
                        label: metrics.get(key)
                        for label, key in QUALITY_METRICS.items()
                    },
                }
            )
    return pd.DataFrame(rows)


def portfolio_table(
    summary: Mapping[str, object],
    family: str,
    models: Sequence[str],
    horizons: Sequence[int],
) -> pd.DataFrame:
    portfolios = summary.get("portfolios") or {}
    rows = []
    for horizon in horizons:
        block = (portfolios.get(str(horizon)) or {}).get(family) or {}
        for model in models:
            item = block.get(model)
            if not isinstance(item, Mapping):
                continue
            performance = item.get("performance") or {}
            stats = item.get("stats") or {}
            row = {
                "model": model,
                "horizon": int(horizon),
                "family": family,
                **{key: performance.get(key) for key in PORTFOLIO_METRICS},
                "active_return": stats.get("active_return"),
                "information_ratio": stats.get("information_ratio"),
            }
            rows.append(row)
    return pd.DataFrame(rows)


def spearman_rank_corr(left: Iterable[float], right: Iterable[float]) -> float:
    a = pd.Series(list(left), dtype="float64")
    b = pd.Series(list(right), dtype="float64")
    valid = a.notna() & b.notna()
    if int(valid.sum()) < 3:
        return float("nan")
    return float(np.corrcoef(a[valid].rank(), b[valid].rank())[0, 1])


def rank_correlations(
    quality: pd.DataFrame,
    portfolios: Mapping[str, pd.DataFrame],
    horizons: Sequence[int],
) -> dict[str, object]:
    out: dict[str, object] = {}
    for index, left in enumerate(horizons):
        for right in horizons[index + 1 :]:
            pair = f"{left}v{right}"
            left_quality = quality[quality["horizon"] == left].set_index("model")
            right_quality = quality[quality["horizon"] == right].set_index("model")
            common = left_quality.index.intersection(right_quality.index)
            quality_corr = {
                metric: spearman_rank_corr(
                    left_quality.loc[common, metric],
                    right_quality.loc[common, metric],
                )
                for metric in QUALITY_METRICS
            }
            portfolio_corr = {}
            for family, frame in portfolios.items():
                left_frame = frame[frame["horizon"] == left].set_index("model")
                right_frame = frame[frame["horizon"] == right].set_index("model")
                common_models = left_frame.index.intersection(right_frame.index)
                portfolio_corr[family] = {
                    metric: spearman_rank_corr(
                        left_frame.loc[common_models, metric],
                        right_frame.loc[common_models, metric],
                    )
                    for metric in ("total_return", "sharpe")
                }
            out[pair] = {
                "quality": quality_corr,
                "portfolio": portfolio_corr,
            }
    return out


def delta_table(
    quality: pd.DataFrame,
    portfolios: Mapping[str, pd.DataFrame],
    horizons: Sequence[int],
) -> pd.DataFrame:
    rows = []
    for index, left in enumerate(horizons):
        for right in horizons[index + 1 :]:
            left_quality = quality[quality["horizon"] == left].set_index("model")
            right_quality = quality[quality["horizon"] == right].set_index("model")
            for model in left_quality.index.intersection(right_quality.index):
                for metric in QUALITY_METRICS:
                    rows.append(
                        {
                            "model": model,
                            "group": "quality",
                            "metric": metric,
                            "from_horizon": left,
                            "to_horizon": right,
                            "delta": _subtract(
                                right_quality.loc[model, metric],
                                left_quality.loc[model, metric],
                            ),
                        }
                    )
                for family, frame in portfolios.items():
                    left_frame = frame[frame["horizon"] == left].set_index("model")
                    right_frame = frame[frame["horizon"] == right].set_index("model")
                    if model not in right_frame.index:
                        continue
                    for metric in ("total_return", "sharpe"):
                        rows.append(
                            {
                                "model": model,
                                "group": family,
                                "metric": metric,
                                "from_horizon": left,
                                "to_horizon": right,
                                "delta": _subtract(
                                    right_frame.loc[model, metric],
                                    left_frame.loc[model, metric],
                                ),
                            }
                        )
    return pd.DataFrame(rows)


def _subtract(left: object, right: object) -> float:
    try:
        return float(left) - float(right)
    except (TypeError, ValueError):
        return float("nan")


def _format_pct(value: object) -> str:
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return "—"


def _print_summary(
    quality: pd.DataFrame,
    portfolios: Mapping[str, pd.DataFrame],
    correlations: Mapping[str, object],
) -> None:
    print("\n== 质量指标（IC 全截面） ==")
    print(
        quality.pivot(index="model", columns="horizon", values="ic_mean")
        .reindex(columns=sorted(quality["horizon"].unique()))
        .to_string(float_format=lambda value: f"{value:.4f}")
    )
    print("\n== Top10 组合总收益 ==")
    top10 = portfolios.get("top10")
    if top10 is not None and not top10.empty:
        print(
            top10.pivot(index="model", columns="horizon", values="total_return")
            .reindex(columns=sorted(top10["horizon"].unique()))
            .map(_format_pct)
            .to_string()
        )
    print("\n== 周期秩相关 ==")
    for pair, block in correlations.items():
        quality_corr = (block.get("quality") or {}).get("ic_mean")
        top10_corr = ((block.get("portfolio") or {}).get("top10") or {}).get(
            "total_return"
        )
        print(f"{pair}: IC ρ={quality_corr:.3f}  Top10 总收益 ρ={top10_corr:.3f}")


def run(
    run_dir: Path,
    *,
    models: Sequence[str],
    horizons: Sequence[int],
    out_dir: Path | None = None,
) -> dict[str, object]:
    summary = load_summary(run_dir)
    picked = _pick_horizons(summary, horizons)
    target = out_dir or run_dir

    quality = quality_table(summary, models, picked)
    portfolios = {
        family: portfolio_table(summary, family, models, picked)
        for family in FAMILY_ORDER
    }
    correlations = rank_correlations(quality, portfolios, picked)
    deltas = delta_table(quality, portfolios, picked)

    target.mkdir(parents=True, exist_ok=True)
    quality.to_csv(target / "horizon_quality.csv", index=False)
    for family, frame in portfolios.items():
        frame.to_csv(target / f"horizon_portfolio_{family}.csv", index=False)
    deltas.to_csv(target / "horizon_deltas.csv", index=False)
    (target / "horizon_rank_corr.json").write_text(
        json.dumps(correlations, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    _print_summary(quality, portfolios, correlations)
    return {
        "horizons": list(picked),
        "models": list(models),
        "quality": quality.to_dict(orient="records"),
        "portfolios": {
            family: frame.to_dict(orient="records")
            for family, frame in portfolios.items()
        },
        "rank_corr": correlations,
        "deltas": deltas.to_dict(orient="records"),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--models", nargs="*", default=list(MODEL_ORDER))
    parser.add_argument(
        "--horizons", nargs="*", type=int, default=list(DEFAULT_HORIZONS)
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    unknown = [key for key in args.models if key not in MODEL_ORDER]
    if unknown:
        raise SystemExit(f"未知模型: {', '.join(unknown)}")
    run(
        args.run_dir,
        models=tuple(args.models),
        horizons=tuple(args.horizons),
        out_dir=args.out,
    )


if __name__ == "__main__":
    main()
