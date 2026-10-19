"""长窗口深度评估报告的图表。

沿用单模型/多模型报告的版式：白底、细线、直接标注，A 股口径红涨绿跌。
所有图都从 ``deep_report_data.DeepBundle`` 读取，不重复计算回测。
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import matplotlib.ticker as mtick
import numpy as np
import pandas as pd

from .deep_report_data import DeepBundle, period_metrics, quarter_ic_matrix
from .model_registry import MODEL_ORDER
from .report_pdf_charts import (
    AMBER,
    BENCH,
    GRID,
    INK,
    MUTED,
    NEG,
    POS,
    SLATE,
    TEAL,
    _save,
    setup_style,
)

PALETTE = (POS, SLATE, AMBER, TEAL, "#6B4C7A", "#2B4C7E", "#8A5A12", "#4F6F52")
HIGHLIGHT = {"LGB": POS, "XGB": SLATE, "CAT": AMBER, "TFT": TEAL}


def _ordered_models(bundle: DeepBundle, horizon: int) -> list[str]:
    ranking = bundle.quality_ranking(horizon)
    if ranking.empty:
        return list(MODEL_ORDER)
    return [str(model) for model in ranking["model"]]


def quality_dumbbell(bundle: DeepBundle, path: Path) -> Path | None:
    """5/10 日 IC 与 Top20 超额的横向点线图。"""
    if not bundle.quality:
        return None
    fig, axes = plt.subplots(1, 2, figsize=(7.4, 3.5), sharey=True)
    for ax, field, title in (
        (axes[0], "ic_mean", "IC 均值"),
        (axes[1], "top20_ret", "Top20 日收益"),
    ):
        for horizon, color in ((5, POS), (10, SLATE)):
            frame = bundle.quality.get(horizon)
            if frame is None or frame.empty:
                continue
            frame = frame.set_index("model").reindex(MODEL_ORDER)
            values = pd.to_numeric(frame[field], errors="coerce")
            y = np.arange(len(MODEL_ORDER))
            ax.scatter(values, y, s=16, color=color, label=f"T+{horizon}", zorder=3)
        ax.set_title(title)
        ax.xaxis.set_major_formatter(mtick.PercentFormatter(1.0, decimals=1))
        ax.grid(axis="y", visible=False)
        ax.tick_params(axis="x", labelsize=6.8)
    axes[0].set_yticks(np.arange(len(MODEL_ORDER)), MODEL_ORDER)
    axes[0].invert_yaxis()
    axes[1].legend(loc="lower right", frameon=False)
    fig.tight_layout()
    return _save(fig, path)


def quarter_heatmap(bundle: DeepBundle, horizon: int, path: Path) -> Path | None:
    matrix = quarter_ic_matrix(bundle, horizon)
    if matrix.empty:
        return None
    order = [
        model for model in _ordered_models(bundle, horizon) if model in matrix.columns
    ]
    matrix = matrix[order].T
    fig, ax = plt.subplots(figsize=(7.4, max(2.8, 0.34 * len(matrix) + 1.0)))
    values = matrix.to_numpy(dtype=float)
    finite = values[np.isfinite(values)]
    limit = float(np.nanmax(np.abs(finite))) if len(finite) else 1.0
    limit = max(limit, 1e-9)
    ax.imshow(
        values,
        aspect="auto",
        cmap=_diverging(),
        vmin=-limit,
        vmax=limit,
    )
    ax.set_xticks(range(matrix.shape[1]), matrix.columns, fontsize=7.0)
    ax.set_yticks(range(matrix.shape[0]), matrix.index, fontsize=7.0)
    ax.grid(False)
    ax.tick_params(length=0)
    for row in range(matrix.shape[0]):
        for column in range(matrix.shape[1]):
            value = values[row, column]
            text = "—" if not np.isfinite(value) else f"{value:.2f}"
            shade = "white" if np.isfinite(value) and abs(value) > limit * 0.55 else INK
            ax.text(
                column, row, text, ha="center", va="center", fontsize=6.4, color=shade
            )
    ax.set_xlabel("季度")
    fig.tight_layout()
    return _save(fig, path)


def period_bars(bundle: DeepBundle, horizon: int, path: Path) -> Path | None:
    frame = period_metrics(bundle, horizon)
    if frame.empty:
        return None
    pivot = frame.pivot_table(index="model", columns="period", values="ic_mean")
    pivot = pivot.reindex(
        [m for m in _ordered_models(bundle, horizon) if m in pivot.index]
    )
    colors = {"stored": SLATE, "clean": TEAL, "degraded": AMBER}
    labels = {"stored": "训练产物期", "clean": "外推·干净", "degraded": "外推·降级"}
    fig, ax = plt.subplots(figsize=(7.4, 3.2))
    x = np.arange(len(pivot))
    width = 0.26
    for offset, period in zip(
        (-width, 0.0, width), ("stored", "clean", "degraded"), strict=True
    ):
        if period not in pivot.columns:
            continue
        values = pd.to_numeric(pivot[period], errors="coerce")
        ax.bar(
            x + offset,
            values,
            width=width,
            color=colors[period],
            label=labels[period],
            edgecolor="white",
            linewidth=0.4,
        )
    ax.axhline(0, color=INK, linewidth=0.6)
    ax.set_xticks(x, pivot.index, rotation=0, fontsize=6.8)
    ax.yaxis.set_major_formatter(mtick.PercentFormatter(1.0, decimals=0))
    ax.set_title(f"T+{horizon} 分段 IC：训练产物期 / 外推干净期 / 外推降级期")
    ax.legend(frameon=False, ncol=3, loc="upper right")
    ax.grid(axis="x", visible=False)
    fig.tight_layout()
    return _save(fig, path)


def equity_curves(bundle: DeepBundle, horizon: int, path: Path) -> Path | None:
    """滚动 Top5 净值；按全窗口终值突出前三名。"""
    ranking = _ordered_models(bundle, horizon)[:3]
    fig, ax = plt.subplots(figsize=(7.4, 3.4))
    benchmark_drawn = False
    for model in MODEL_ORDER:
        equity_path = bundle.run_dir / f"equity_rolling_{horizon}_{model}.csv"
        if not equity_path.is_file():
            continue
        frame = pd.read_csv(equity_path, parse_dates=["date"]).set_index("date")
        if "equity" not in frame.columns or frame["equity"].dropna().empty:
            continue
        series = frame["equity"].astype(float).dropna() / float(
            frame["equity"].dropna().iloc[0]
        )
        is_highlight = model in ranking
        color = HIGHLIGHT.get(model, PALETTE[MODEL_ORDER.index(model) % len(PALETTE)])
        ax.plot(
            series.index,
            series,
            linewidth=1.25 if is_highlight else 0.6,
            color=color if is_highlight else "#C9C4BA",
            alpha=1.0 if is_highlight else 0.85,
            label=model if is_highlight else None,
            zorder=3 if is_highlight else 2,
        )
    bench_path = bundle.run_dir / "benchmark.csv"
    if bench_path.is_file():
        bench = pd.read_csv(bench_path, parse_dates=["date"]).set_index("date")
        bench = bench["equity"].astype(float).dropna()
        if not bench.empty:
            ax.plot(
                bench.index,
                bench / float(bench.iloc[0]),
                color=BENCH,
                linewidth=0.9,
                linestyle="--",
                label="基准（全市场等权）",
            )
            benchmark_drawn = True
    ax.axhline(1.0, color=GRID, linewidth=0.6)
    ax.yaxis.set_major_formatter(mtick.PercentFormatter(1.0, decimals=0))
    ax.set_title(f"T+{horizon} 滚动 Top5 净值（灰线为其余模型）")
    ax.legend(frameon=False, loc="upper left", ncol=2)
    ax.grid(axis="x", visible=False)
    fig.tight_layout()
    _ = benchmark_drawn
    return _save(fig, path)


def consensus_vote_bars(bundle: DeepBundle, horizon: int, path: Path) -> Path | None:
    frame = bundle.consensus.get(f"distribution_{horizon}")
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return None
    frame = frame.copy()
    frame["consensus"] = pd.to_numeric(frame["consensus"], errors="coerce")
    frame = frame.dropna(subset=["consensus"]).sort_values("consensus")
    fig, ax = plt.subplots(figsize=(7.4, 3.0))
    x = frame["consensus"].to_numpy(dtype=float)
    ax.bar(
        x,
        frame["share"].astype(float),
        color=SLATE,
        width=0.72,
        edgecolor="white",
        linewidth=0.4,
    )
    ax.set_xlabel("被多少个模型同时选入 Top20")
    ax.set_ylabel("股票-日占比")
    ax.yaxis.set_major_formatter(mtick.PercentFormatter(1.0, decimals=0))
    ax.set_xticks(x)
    twin = ax.twinx()
    twin.plot(
        x,
        frame["mean_excess"].astype(float),
        color=POS,
        marker="o",
        markersize=2.6,
        linewidth=1.0,
    )
    twin.set_ylabel("平均超额", color=POS)
    twin.yaxis.set_major_formatter(mtick.PercentFormatter(1.0, decimals=1))
    twin.grid(False)
    ax.set_title(f"T+{horizon} 共识票数分布与未来超额")
    fig.tight_layout()
    return _save(fig, path)


def overlap_heatmap(bundle: DeepBundle, horizon: int, path: Path) -> Path | None:
    frame = bundle.consensus.get(f"pairwise_{horizon}")
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return None
    models = [
        model
        for model in MODEL_ORDER
        if model in set(frame["model_a"]) | set(frame["model_b"])
    ]
    matrix = pd.DataFrame(np.nan, index=models, columns=models, dtype=float)
    for record in frame.to_dict("records"):
        a, b = str(record["model_a"]), str(record["model_b"])
        if a not in matrix.index or b not in matrix.columns:
            continue
        matrix.loc[a, b] = record.get("avg_jaccard")
        matrix.loc[b, a] = record.get("avg_jaccard")
    fig, ax = plt.subplots(figsize=(5.6, 4.6))
    values = matrix.to_numpy(dtype=float)
    image = ax.imshow(
        values,
        cmap="YlGnBu",
        vmin=0,
        vmax=float(np.nanmax(values)) if np.isfinite(values).any() else 1,
    )
    ax.set_xticks(range(len(models)), models, rotation=90, fontsize=6.6)
    ax.set_yticks(range(len(models)), models, fontsize=6.6)
    ax.grid(False)
    ax.tick_params(length=0)
    for row in range(len(models)):
        for column in range(len(models)):
            value = values[row, column]
            if np.isfinite(value) and row != column:
                ax.text(
                    column,
                    row,
                    f"{value:.2f}",
                    ha="center",
                    va="center",
                    fontsize=5.8,
                    color=INK,
                )
    fig.colorbar(image, ax=ax, fraction=0.046, pad=0.03, label="平均 Jaccard")
    ax.set_title(f"T+{horizon} 模型两两 Top20 重叠")
    fig.tight_layout()
    return _save(fig, path)


def capacity_advantage(bundle: DeepBundle, path: Path) -> Path | None:
    frame = bundle.capacity.get("advantage")
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return None
    fig, axes = plt.subplots(1, 2, figsize=(7.4, 3.0), sharey=True)
    for ax, horizon in zip(axes, (5, 10), strict=True):
        subset = frame[frame["horizon"] == horizon]
        if subset.empty:
            ax.set_visible(False)
            continue
        participation_numeric = pd.to_numeric(subset["participation"], errors="coerce")
        for participation, color, label in (
            (None, SLATE, "无容量约束"),
            (0.01, POS, "单票 ≤ 当日成交额 1%"),
        ):
            group = (
                subset[participation_numeric.isna()]
                if participation is None
                else subset[np.isclose(participation_numeric, participation)]
            )
            group = group.sort_values("capital")
            ax.plot(
                group["capital"].astype(float),
                group["mean_diff"].astype(float),
                marker="o",
                markersize=3,
                linewidth=1.1,
                color=color,
                label=label,
            )
        ax.axhline(0, color=INK, linewidth=0.6)
        ax.set_xscale("log")
        ax.set_title(f"T+{horizon}")
        ax.set_xlabel("初始资金（元）")
        ax.yaxis.set_major_formatter(mtick.PercentFormatter(1.0, decimals=1))
        ax.grid(axis="x", visible=False)
    axes[0].set_ylabel("Top10 − Top20 平均收益差")
    axes[0].legend(frameon=False, loc="lower left")
    fig.suptitle("容量约束下 Top10 相对 Top20 的优势", fontsize=9.5, y=1.02)
    fig.tight_layout()
    return _save(fig, path)


def attribution_bars(bundle: DeepBundle, horizon: int, path: Path) -> Path | None:
    frame = bundle.attribution
    if frame.empty:
        return None
    subset = frame[
        (frame["family"] == "rolling") & (frame["horizon"] == horizon)
    ].copy()
    if subset.empty:
        return None
    available = set(subset["model"].astype(str))
    subset = subset.set_index("model").reindex(
        [model for model in MODEL_ORDER if model in available]
    )
    days = pd.to_numeric(subset["days"], errors="coerce").replace(0, np.nan)
    components = {
        "行业暴露": pd.to_numeric(subset["industry_sum"], errors="coerce") / days * 252,
        "市值暴露": pd.to_numeric(subset["size_sum"], errors="coerce") / days * 252,
        "选股": pd.to_numeric(subset["selection_sum"], errors="coerce") / days * 252,
        "现金拖累": pd.to_numeric(subset["cash_drag_sum"], errors="coerce")
        / days
        * 252,
    }
    colors = {"行业暴露": AMBER, "市值暴露": SLATE, "选股": POS, "现金拖累": NEG}
    fig, ax = plt.subplots(figsize=(7.4, 3.5))
    left = np.zeros(len(subset))
    for label, series in components.items():
        values = series.fillna(0.0).to_numpy(dtype=float)
        ax.barh(
            np.arange(len(subset)),
            values,
            left=left,
            color=colors[label],
            label=label,
            height=0.72,
            edgecolor="white",
            linewidth=0.4,
        )
        left += values
    ax.set_yticks(np.arange(len(subset)), subset.index)
    ax.invert_yaxis()
    ax.axvline(0, color=INK, linewidth=0.6)
    ax.xaxis.set_major_formatter(mtick.PercentFormatter(1.0, decimals=0))
    ax.set_title(f"T+{horizon} 滚动 Top5 超额归因（年化贡献）")
    ax.legend(frameon=False, ncol=4, loc="lower right")
    ax.grid(axis="y", visible=False)
    fig.tight_layout()
    return _save(fig, path)


def screening_heatmap(bundle: DeepBundle, horizon: int, path: Path) -> Path | None:
    frame = bundle.consensus.get(f"screening_summary_{horizon}")
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return None
    matrix = frame.pivot_table(index="model", columns="filter", values="mean_excess")
    order = [m for m in _ordered_models(bundle, horizon) if m in matrix.index]
    columns = [c for c in ("top10", "top20", "top50") if c in matrix.columns]
    matrix = matrix.reindex(index=order, columns=columns)
    values = matrix.to_numpy(dtype=float)
    fig, ax = plt.subplots(figsize=(3.6, max(2.6, 0.28 * len(matrix) + 1.0)))
    finite = values[np.isfinite(values)]
    limit = float(np.nanmax(np.abs(finite))) if len(finite) else 1.0
    ax.imshow(
        values,
        aspect="auto",
        cmap=_diverging(),
        vmin=-max(limit, 1e-9),
        vmax=max(limit, 1e-9),
    )
    ax.set_xticks(range(len(columns)), [c.upper() for c in columns], fontsize=7.0)
    ax.set_yticks(range(len(order)), order, fontsize=6.8)
    ax.grid(False)
    ax.tick_params(length=0)
    for row in range(matrix.shape[0]):
        for column in range(matrix.shape[1]):
            value = values[row, column]
            if np.isfinite(value):
                shade = "white" if abs(value) > limit * 0.55 else INK
                ax.text(
                    column,
                    row,
                    f"{value * 100:.2f}",
                    ha="center",
                    va="center",
                    fontsize=6.2,
                    color=shade,
                )
    ax.set_title(f"T+{horizon} 分数筛选日均超额（%）", fontsize=8.4)
    fig.tight_layout()
    return _save(fig, path)


def _diverging():
    from matplotlib.colors import LinearSegmentedColormap

    return LinearSegmentedColormap.from_list(
        "qm_pos_neg_deep", [NEG, "#F7F3EA", "#FFFFFF", "#F3DEDC", POS]
    )


def render_all(bundle: DeepBundle, out_dir: Path) -> dict[str, Path]:
    setup_style()
    out_dir.mkdir(parents=True, exist_ok=True)
    charts: dict[str, Path] = {}

    def draw(name: str, path: Path, func, *args) -> None:
        result = func(*args, path)
        if result is not None:
            charts[name] = result

    draw("quality", out_dir / "quality.png", quality_dumbbell, bundle)
    draw("equity_5", out_dir / "equity_5.png", equity_curves, bundle, 5)
    draw("equity_10", out_dir / "equity_10.png", equity_curves, bundle, 10)
    draw("period_5", out_dir / "period_5.png", period_bars, bundle, 5)
    draw("period_10", out_dir / "period_10.png", period_bars, bundle, 10)
    draw("quarter_5", out_dir / "quarter_5.png", quarter_heatmap, bundle, 5)
    draw("quarter_10", out_dir / "quarter_10.png", quarter_heatmap, bundle, 10)
    draw("votes_5", out_dir / "votes_5.png", consensus_vote_bars, bundle, 5)
    draw("votes_10", out_dir / "votes_10.png", consensus_vote_bars, bundle, 10)
    draw("overlap_5", out_dir / "overlap_5.png", overlap_heatmap, bundle, 5)
    draw("overlap_10", out_dir / "overlap_10.png", overlap_heatmap, bundle, 10)
    draw("capacity", out_dir / "capacity.png", capacity_advantage, bundle)
    draw("attr_5", out_dir / "attr_5.png", attribution_bars, bundle, 5)
    draw("attr_10", out_dir / "attr_10.png", attribution_bars, bundle, 10)
    draw("screen_5", out_dir / "screen_5.png", screening_heatmap, bundle, 5)
    draw("screen_10", out_dir / "screen_10.png", screening_heatmap, bundle, 10)
    return charts
