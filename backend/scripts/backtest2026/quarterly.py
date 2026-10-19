"""按季度切分长窗口：冠军是否换人、模型排序是否稳定。

每个季度独立跑：
- Friedman + Kendall's W（IC 与 Top20 超额两张矩阵）；
- White Reality Check（控制 13 个模型的数据窥探）；
- PBO/CSCV 与 Deflated Sharpe（季度样本短，只作方向性参考）；
- 按 IC / 超额 / Sharpe 三条轴分别排名，记录冠军与相邻季度秩相关。

季度内日收益仍按 H 日重叠口径计算，因此 Sharpe/DSR 只用于横向比较，
不应当作可交易的年化指标。
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from .config import OUT_DIR
from .data import load_index_closes
from .deep_common import TEST_END, TEST_START, load_equity, quarter_label
from .model_registry import HORIZONS, MODEL_ORDER
from .runner import _write_json
from .strict_stats import (
    deflated_sharpe_ratio,
    friedman_kendall,
    pbo_cscv,
    sharpe_ratio,
    spearman,
    white_reality_check,
)

LOG = logging.getLogger("backtest2026.quarterly")

INDEX_NAMES = {"000300.SH": "hs300", "000905.SH": "zz500", "000852.SH": "zz1000"}


@dataclass(frozen=True)
class QuarterlySettings:
    run_dir: Path
    out_dir: Path
    start: str = TEST_START
    end: str = TEST_END
    models: tuple[str, ...] = MODEL_ORDER
    horizons: tuple[int, ...] = HORIZONS
    min_days: int = 20
    block: int = 10
    n_boot: int = 2000


def _load_matrix(
    settings: QuarterlySettings, horizon: int, column: str
) -> tuple[pd.DataFrame, list[str]]:
    frames: dict[str, pd.Series] = {}
    for model in settings.models:
        path = settings.run_dir / f"quality_daily_{horizon}_{model}.csv"
        if not path.is_file():
            LOG.warning("缺少 %s", path)
            continue
        frame = load_equity(path)
        if column not in frame.columns:
            LOG.warning("%s 缺列 %s", path, column)
            continue
        frames[model] = frame[column]
    if not frames:
        return pd.DataFrame(), []
    matrix = pd.DataFrame(frames).sort_index()
    matrix = matrix.loc[
        (matrix.index >= pd.Timestamp(settings.start))
        & (matrix.index <= pd.Timestamp(settings.end))
    ]
    return matrix, list(frames)


def _clean_matrix(matrix: pd.DataFrame) -> np.ndarray:
    values = matrix.to_numpy(dtype=np.float64)
    values = values[np.isfinite(values).all(axis=1)]
    return values


def _quarter_index_returns(
    start: str, end: str
) -> dict[str, dict[str, float]]:
    closes = load_index_closes(start, end)
    result: dict[str, dict[str, float]] = {}
    for symbol, name in INDEX_NAMES.items():
        if symbol not in closes.columns:
            continue
        series = closes[symbol].dropna()
        if series.empty:
            continue
        grouped = series.groupby(series.index.to_period("Q"))
        for period, values in grouped:
            if len(values) < 2:
                continue
            label = f"{period.year}Q{period.quarter}"
            result.setdefault(label, {})[name] = float(values.iloc[-1] / values.iloc[0] - 1.0)
    return result


def _quarter_stats(
    quarter: str,
    ic: pd.DataFrame,
    excess: pd.DataFrame,
    top_ret: pd.DataFrame,
    pool_ret: pd.Series,
    models: list[str],
    settings: QuarterlySettings,
    horizon: int,
) -> tuple[dict[str, object], pd.DataFrame]:
    days = ic.index
    ic_values = _clean_matrix(ic)
    excess_values = _clean_matrix(excess)
    row: dict[str, object] = {
        "quarter": quarter,
        "days": int(len(days)),
        "models": len(models),
        "pool_ret": float(pool_ret.reindex(days).mean()) if len(days) else np.nan,
        "excess_std": float(np.std(excess_values.mean(axis=0), ddof=1))
        if excess_values.size
        else np.nan,
        "ic_std": float(np.std(ic_values.mean(axis=0), ddof=1))
        if ic_values.size
        else np.nan,
    }
    ic_friedman = friedman_kendall(ic_values)
    excess_friedman = friedman_kendall(excess_values)
    row.update(
        {
            "ic_chi2": ic_friedman["chi2"],
            "ic_p": ic_friedman["p"],
            "ic_kendall_w": ic_friedman["kendall_w"],
            "excess_chi2": excess_friedman["chi2"],
            "excess_p": excess_friedman["p"],
            "excess_kendall_w": excess_friedman["kendall_w"],
        }
    )
    white = white_reality_check(
        excess_values, block=settings.block, n_boot=settings.n_boot
    )
    row.update(
        {
            "white_stat": white["stat"],
            "white_p": white["p"],
            "white_best": models[int(white["best_index"])]
            if np.isfinite(white["best_index"])
            else "",
        }
    )
    pbo = pbo_cscv(excess_values, n_blocks=min(8, max(4, len(excess_values) // 10)))
    row.update(
        {
            "pbo": pbo["pbo"],
            "pbo_combos": pbo["n_combos"],
        }
    )

    ic_mean = ic.mean(axis=0)
    ic_std = ic.std(axis=0, ddof=1)
    excess_mean = excess.mean(axis=0)
    excess_std = excess.std(axis=0, ddof=1)
    sharpe = excess.apply(lambda column: sharpe_ratio(column))
    top_mean = top_ret.mean(axis=0)
    table = pd.DataFrame(
        {
            "model": models,
            "ic_mean": ic_mean.reindex(models).to_numpy(),
            "ic_std": ic_std.reindex(models).to_numpy(),
            "icir": (ic_mean / ic_std.replace(0.0, np.nan)).reindex(models).to_numpy(),
            "excess_mean": excess_mean.reindex(models).to_numpy(),
            "excess_std": excess_std.reindex(models).to_numpy(),
            "top20_ret": top_mean.reindex(models).to_numpy(),
            "sharpe": sharpe.reindex(models).to_numpy(),
            "ic_positive": (ic > 0).mean().reindex(models).to_numpy(),
        }
    )
    table["ic_rank"] = table["ic_mean"].rank(ascending=False, method="min").astype(int)
    table["excess_rank"] = table["excess_mean"].rank(
        ascending=False, method="min"
    ).astype(int)
    table["sharpe_rank"] = table["sharpe"].rank(
        ascending=False, method="min", na_option="bottom"
    ).astype(int)
    table.insert(0, "quarter", quarter)

    best_ic = str(table.sort_values("ic_mean", ascending=False).iloc[0]["model"])
    best_excess = str(
        table.sort_values("excess_mean", ascending=False).iloc[0]["model"]
    )
    best_sharpe = str(table.sort_values("sharpe", ascending=False).iloc[0]["model"])
    best_excess_idx = models.index(best_excess)
    trial_sr = float(
        np.nanstd(
            [excess[model].mean() / excess[model].std(ddof=1) for model in models]
        )
    )
    dsr = deflated_sharpe_ratio(
        excess[best_excess].dropna(),
        n_trials=len(models),
        trial_sr_std=trial_sr,
    )
    row.update(
        {
            "best_ic_model": best_ic,
            "best_excess_model": best_excess,
            "best_sharpe_model": best_sharpe,
            "best_ic_mean": float(table.loc[table["model"] == best_ic, "ic_mean"].iloc[0]),
            "best_excess_mean": float(
                table.loc[table["model"] == best_excess, "excess_mean"].iloc[0]
            ),
            "best_excess_dsr": dsr["dsr"],
            "best_excess_sharpe": dsr["sharpe"],
            "best_excess_sr0": dsr["sr0"],
            "best_excess_index": best_excess_idx,
        }
    )
    return row, table


def run(settings: QuarterlySettings) -> dict[str, object]:
    settings.out_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, object] = {
        "run_dir": str(settings.run_dir),
        "start": settings.start,
        "end": settings.end,
        "models": list(settings.models),
        "horizons": list(settings.horizons),
        "min_days": settings.min_days,
    }
    index_returns = _quarter_index_returns(settings.start, settings.end)
    summary: dict[str, object] = {}
    for horizon in settings.horizons:
        ic, models = _load_matrix(settings, horizon, "ic_eligible")
        if ic.empty:
            LOG.warning("T+%d 没有可用的 quality_daily", horizon)
            continue
        excess, _ = _load_matrix(settings, horizon, "top20_excess")
        top_ret, _ = _load_matrix(settings, horizon, "top20_ret")
        pool_ret, _ = _load_matrix(settings, horizon, "pool_ret")
        pool = pool_ret.mean(axis=1)
        excess = excess.reindex(ic.index)
        top_ret = top_ret.reindex(ic.index)

        quarters = [quarter_label(day) for day in ic.index]
        stats_rows, tables = [], []
        for quarter in sorted(set(quarters)):
            mask = np.asarray([q == quarter for q in quarters])
            if int(mask.sum()) < settings.min_days:
                LOG.warning("T+%d %s 仅 %d 天，跳过统计", horizon, quarter, int(mask.sum()))
                continue
            row, table = _quarter_stats(
                quarter,
                ic.loc[mask],
                excess.loc[mask],
                top_ret.loc[mask],
                pool.loc[mask],
                models,
                settings,
                horizon,
            )
            row.update(index_returns.get(quarter, {}))
            stats_rows.append(row)
            tables.append(table)
        stats = pd.DataFrame(stats_rows)
        stats.to_csv(settings.out_dir / f"quarterly_stats_{horizon}.csv", index=False)
        if tables:
            rank_table = pd.concat(tables, ignore_index=True)
            rank_table.to_csv(
                settings.out_dir / f"quarterly_rank_{horizon}.csv", index=False
            )
        else:
            rank_table = pd.DataFrame()

        # 冠军变化与相邻季度秩相关。
        champion_rows = []
        prev = None
        for row in stats_rows:
            current = {
                "quarter": row["quarter"],
                "champion_ic": row["best_ic_model"],
                "champion_excess": row["best_excess_model"],
                "champion_sharpe": row["best_sharpe_model"],
            }
            if prev is None:
                current.update(
                    {
                        "ic_changed": True,
                        "excess_changed": True,
                        "sharpe_changed": True,
                        "rank_corr_prev_ic": np.nan,
                        "rank_corr_prev_excess": np.nan,
                    }
                )
            else:
                prev_ic = (
                    rank_table[rank_table["quarter"] == prev["quarter"]]
                    .set_index("model")["ic_mean"]
                    .reindex(models)
                )
                curr_ic = (
                    rank_table[rank_table["quarter"] == row["quarter"]]
                    .set_index("model")["ic_mean"]
                    .reindex(models)
                )
                prev_ex = (
                    rank_table[rank_table["quarter"] == prev["quarter"]]
                    .set_index("model")["excess_mean"]
                    .reindex(models)
                )
                curr_ex = (
                    rank_table[rank_table["quarter"] == row["quarter"]]
                    .set_index("model")["excess_mean"]
                    .reindex(models)
                )
                current.update(
                    {
                        "ic_changed": row["best_ic_model"] != prev["best_ic_model"],
                        "excess_changed": row["best_excess_model"]
                        != prev["best_excess_model"],
                        "sharpe_changed": row["best_sharpe_model"]
                        != prev["best_sharpe_model"],
                        "rank_corr_prev_ic": spearman(prev_ic, curr_ic),
                        "rank_corr_prev_excess": spearman(prev_ex, curr_ex),
                    }
                )
            champion_rows.append(current)
            prev = row
        champions = pd.DataFrame(champion_rows)
        champions.to_csv(
            settings.out_dir / f"quarterly_champions_{horizon}.csv", index=False
        )
        if stats_rows:
            summary[f"T{horizon}"] = {
                "quarters": len(stats_rows),
                "ic_champion_changes": int(champions["ic_changed"].iloc[1:].sum())
                if len(champions) > 1
                else 0,
                "excess_champion_changes": int(
                    champions["excess_changed"].iloc[1:].sum()
                )
                if len(champions) > 1
                else 0,
                "mean_rank_corr_ic": float(champions["rank_corr_prev_ic"].mean())
                if len(champions) > 1
                else np.nan,
                "mean_rank_corr_excess": float(
                    champions["rank_corr_prev_excess"].mean()
                )
                if len(champions) > 1
                else np.nan,
                "champions": champions.to_dict(orient="records"),
            }
            LOG.info(
                "T+%d 季度统计完成：%d 个季度，IC 冠军更换 %d 次，超额冠军更换 %d 次",
                horizon,
                len(stats_rows),
                summary[f"T{horizon}"]["ic_champion_changes"],
                summary[f"T{horizon}"]["excess_champion_changes"],
            )
        else:
            summary[f"T{horizon}"] = {"quarters": 0}
            LOG.warning("T+%d 没有季度满足最少天数", horizon)
    manifest["summary"] = summary
    _write_json(settings.out_dir / "quarterly_manifest.json", manifest)
    return manifest


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=OUT_DIR / "deep_quarterly")
    parser.add_argument("--start", default=TEST_START)
    parser.add_argument("--end", default=TEST_END)
    parser.add_argument("--models", nargs="*", default=list(MODEL_ORDER))
    parser.add_argument("--horizons", nargs="*", type=int, default=list(HORIZONS))
    parser.add_argument("--min-days", type=int, default=20)
    parser.add_argument("--block", type=int, default=10)
    parser.add_argument("--n-boot", type=int, default=2000)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    args = parse_args(argv)
    settings = QuarterlySettings(
        run_dir=args.run_dir,
        out_dir=args.out,
        start=args.start,
        end=args.end,
        models=tuple(args.models),
        horizons=tuple(args.horizons),
        min_days=args.min_days,
        block=args.block,
        n_boot=args.n_boot,
    )
    run(settings)


if __name__ == "__main__":
    main()
