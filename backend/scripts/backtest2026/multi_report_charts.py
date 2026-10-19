"""多模型 PDF 报告的图表。

与单模型报告共用字体与配色：A 股口径红涨绿跌，白底细线，直接标注，
不使用默认模板的高饱和色板。
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import matplotlib.ticker as mtick
import numpy as np
import pandas as pd
from matplotlib.colors import LinearSegmentedColormap

from .multi_portfolio import FAMILY_ORDER
from .multi_report_data import (
    SHORT_FAMILY,
    MultiBundle,
    display_name,
)
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

T5_COLOR = POS
T10_COLOR = SLATE
PALETTE = (POS, SLATE, AMBER, TEAL, "#6B4C7A", "#2B4C7E", "#8A5A12", "#4F6F52")

_NEG_COLOR = "#1E6B4F"
_MID_COLOR = "#F7F3EA"


def _diverging() -> LinearSegmentedColormap:
    return LinearSegmentedColormap.from_list(
        "qm_pos_neg", [_NEG_COLOR, _MID_COLOR, "#FFFFFF", "#F3DEDC", POS]
    )


def _heat(ax: plt.Axes, frame: pd.DataFrame, *, digits: int = 2) -> None:
    values = frame.to_numpy(dtype=float)
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
    ax.set_xticks(range(frame.shape[1]), frame.columns, fontsize=7.2)
    ax.set_yticks(range(frame.shape[0]), frame.index, fontsize=7.2)
    ax.set_xlim(-0.65, frame.shape[1] - 0.35)
    ax.set_ylim(frame.shape[0] - 0.35, -0.65)
    ax.grid(False)
    ax.tick_params(length=0)
    for row in range(frame.shape[0]):
        for column in range(frame.shape[1]):
            value = values[row, column]
            if not np.isfinite(value):
                text = "—"
            else:
                text = f"{value:.{digits}f}"
            shade = "white" if abs(value) > limit * 0.55 else INK
            ax.text(
                column,
                row,
                text,
                ha="center",
                va="center",
                fontsize=6.4,
                color=shade,
            )
    for spine in ax.spines.values():
        spine.set_visible(False)


def ic_dumbbell(bundle: MultiBundle, path: Path) -> Path:
    """13 个模型的 IC 均值：T+5 与 T+10 同一行对照。"""
    setup_style()
    left = bundle.quality_ranking.get(5)
    right = bundle.quality_ranking.get(10)
    if left is None or left.empty:
        raise ValueError("缺少 T+5 质量榜")
    left = left.sort_values("ic_mean", ascending=True)
    models = left["model"].tolist()
    right_map = (
        right.set_index("model")["ic_mean"].to_dict()
        if right is not None and not right.empty
        else {}
    )
    t5_values = left["ic_mean"].to_numpy(dtype=float)
    t10_values = np.array([right_map.get(model, np.nan) for model in models])

    fig, ax = plt.subplots(figsize=(6.6, 4.6))
    positions = np.arange(len(models))
    for index in range(len(models)):
        a, b = t5_values[index], t10_values[index]
        if np.isfinite(a) and np.isfinite(b):
            ax.plot([a, b], [index, index], color=GRID, lw=2.2, zorder=1)
    ax.scatter(t5_values, positions, s=42, color=T5_COLOR, zorder=3, label="T+5")
    if np.isfinite(t10_values).any():
        ax.scatter(
            t10_values, positions, s=42, color=T10_COLOR, zorder=3, label="T+10"
        )
    ax.axvline(0.0, color=MUTED, lw=0.8)
    ax.set_yticks(positions, [display_name(key) for key in models], fontsize=7.6)
    ax.set_xlabel("逐日截面秩相关 IC 均值")
    ax.grid(axis="x", color=GRID, lw=0.6)
    ax.grid(axis="y", visible=False)
    ax.legend(frameon=False, loc="lower right", fontsize=7.4)
    ax.set_title("预测质量：13 个模型的 IC 均值", loc="left", fontsize=10)
    fig.tight_layout()
    return _save(fig, path)


def hit_bars(bundle: MultiBundle, path: Path) -> Path:
    """Top10 方向胜率与上涨比例，两周期并列。"""
    setup_style()
    models = bundle.all_models()
    if not models:
        raise ValueError("缺少模型列表")
    panels = (
        ("top10_dir", "Top10 方向胜率", "sign(预测) = sign(实际收益) 的占比"),
        ("top10_hit", "Top10 上涨比例", "Top10 中实际收益为正的占比"),
    )
    fig, axes = plt.subplots(1, 2, figsize=(7.4, 4.4), sharey=True)
    positions = np.arange(len(models))
    height = 0.36
    for ax, (column, title, note) in zip(axes, panels, strict=True):
        for offset, horizon, color, label in (
            (-height / 2, 5, T5_COLOR, "T+5"),
            (height / 2, 10, T10_COLOR, "T+10"),
        ):
            series = bundle.quality_ranking.get(horizon)
            values = (
                series.set_index("model")[column].reindex(models).to_numpy(float)
                if series is not None and not series.empty
                else np.full(len(models), np.nan)
            )
            ax.barh(
                positions + offset,
                values,
                height=height,
                color=color,
                label=label,
                alpha=0.92,
            )
        ax.axvline(0.5, color=MUTED, lw=0.8, ls=(0, (3, 3)))
        ax.set_xlim(0.3, max(0.75, ax.get_xlim()[1]))
        ax.xaxis.set_major_formatter(mtick.FuncFormatter(lambda v, _: f"{v * 100:.0f}%"))
        ax.set_title(title, loc="left", fontsize=9.2)
        ax.set_xlabel(note, fontsize=7.2)
        ax.grid(axis="x", color=GRID, lw=0.6)
        ax.grid(axis="y", visible=False)
    axes[0].set_yticks(positions, [display_name(key) for key in models], fontsize=7.4)
    axes[0].invert_yaxis()
    axes[0].legend(frameon=False, loc="lower right", fontsize=7.2)
    fig.suptitle(
        "方向胜率：虚线为 50% 随机基准", x=0.01, ha="left", fontsize=10
    )
    fig.tight_layout()
    return _save(fig, path)


def portfolio_return_bars(bundle: MultiBundle, path: Path) -> Path:
    """Top10 / Top20 / 滚动 Top5 的总收益：13 模型 × 两周期。"""
    setup_style()
    models = bundle.all_models()
    if not models:
        raise ValueError("缺少模型列表")
    fig, axes = plt.subplots(
        1, len(FAMILY_ORDER), figsize=(7.6, 4.8), sharey=True
    )
    positions = np.arange(len(models))
    height = 0.36
    for ax, family in zip(axes, FAMILY_ORDER, strict=True):
        for offset, horizon, color, label in (
            (-height / 2, 5, T5_COLOR, "T+5"),
            (height / 2, 10, T10_COLOR, "T+10"),
        ):
            frame = bundle.portfolio_ranking.get((horizon, family))
            values = (
                frame.set_index("model")["total_return"].reindex(models).to_numpy(float)
                if frame is not None and not frame.empty
                else np.full(len(models), np.nan)
            )
            ax.barh(
                positions + offset,
                values,
                height=height,
                color=color,
                label=label,
                alpha=0.92,
            )
        ax.axvline(0.0, color=MUTED, lw=0.8)
        ax.xaxis.set_major_formatter(mtick.FuncFormatter(lambda v, _: f"{v * 100:.0f}%"))
        ax.set_title(SHORT_FAMILY[family], loc="left", fontsize=9.4)
        ax.grid(axis="x", color=GRID, lw=0.6)
        ax.grid(axis="y", visible=False)
    axes[0].set_yticks(positions, [display_name(key) for key in models], fontsize=7.4)
    axes[0].invert_yaxis()
    axes[0].legend(frameon=False, loc="lower right", fontsize=7.2)
    fig.suptitle(
        "组合总收益：同一 200 万本金、同一成交时点", x=0.01, ha="left", fontsize=10
    )
    fig.tight_layout()
    return _save(fig, path)


def equity_top_models(
    bundle: MultiBundle,
    horizon: int,
    family: str,
    path: Path,
    *,
    top_n: int = 5,
) -> Path:
    """某组合下收益最高的若干模型净值曲线，与等权基准对照。"""
    setup_style()
    frame = bundle.portfolio_ranking.get((horizon, family))
    if frame is None or frame.empty:
        raise ValueError(f"缺少 T+{horizon} {family} 组合榜")
    chosen = frame.sort_values("total_return", ascending=False)["model"].tolist()[
        :top_n
    ]
    fig, ax = plt.subplots(figsize=(7.2, 3.9))
    for index, model in enumerate(chosen):
        equity = bundle.equity.get((horizon, family, model))
        if equity is None or equity.empty:
            continue
        series = equity["equity"].dropna()
        if series.empty:
            continue
        normalized = series / series.iloc[0]
        ax.plot(
            normalized.index,
            normalized,
            lw=1.35,
            color=PALETTE[index % len(PALETTE)],
            label=f"{model} {display_name(model)}",
        )
    benchmark = bundle.benchmark.get("equity")
    if benchmark is not None and not benchmark.empty:
        bench = benchmark.dropna()
        ax.plot(
            bench.index,
            bench / bench.iloc[0],
            lw=1.1,
            color=BENCH,
            ls=(0, (4, 3)),
            label="等权可交易池",
        )
    ax.axhline(1.0, color=MUTED, lw=0.7)
    ax.yaxis.set_major_formatter(mtick.FuncFormatter(lambda v, _: f"{v:.2f}"))
    ax.set_ylabel("净值（起点 = 1）")
    ax.grid(color=GRID, lw=0.6)
    ax.legend(
        frameon=False,
        fontsize=6.6,
        loc="upper left",
        ncol=2,
    )
    ax.set_title(
        f"T+{horizon} · {SHORT_FAMILY[family]}：收益前 {top_n} 模型",
        loc="left",
        fontsize=10,
    )
    fig.tight_layout()
    return _save(fig, path)


def ic_distribution(bundle: MultiBundle, path: Path) -> Path:
    """逐日 IC 分布：箱线图展示中位数与离散度，而非只看均值。"""
    setup_style()
    horizons = [h for h in (5, 10) if bundle.models(h)]
    if not horizons:
        raise ValueError("缺少逐日质量数据")
    fig, axes = plt.subplots(1, len(horizons), figsize=(7.6, 4.3), sharey=True)
    if len(horizons) == 1:
        axes = [axes]
    for ax, horizon in zip(axes, horizons, strict=True):
        models = bundle.models(horizon)
        series = []
        labels = []
        for model in models:
            daily = bundle.quality_daily.get((horizon, model))
            if daily is None or daily.empty or "ic_eligible" not in daily:
                continue
            series.append(daily["ic_eligible"].dropna().to_numpy(float))
            labels.append(display_name(model))
        if not series:
            ax.set_visible(False)
            continue
        box = ax.boxplot(
            series,
            vert=False,
            patch_artist=True,
            widths=0.6,
            showfliers=False,
            medianprops={"color": INK, "lw": 1.0},
            whiskerprops={"color": MUTED, "lw": 0.8},
            capprops={"color": MUTED, "lw": 0.8},
        )
        for patch in box["boxes"]:
            patch.set_facecolor(T5_COLOR if horizon == 5 else T10_COLOR)
            patch.set_alpha(0.35)
            patch.set_edgecolor(MUTED)
            patch.set_linewidth(0.6)
        ax.axvline(0.0, color=MUTED, lw=0.8, ls=(0, (3, 3)))
        ax.set_yticks(range(1, len(labels) + 1), labels, fontsize=7.2)
        ax.set_title(f"T+{horizon}", loc="left", fontsize=9.4)
        ax.grid(axis="x", color=GRID, lw=0.6)
        ax.grid(axis="y", visible=False)
    fig.suptitle("逐日 IC 分布：箱体为四分位，粗线为中位数", x=0.01, ha="left", fontsize=10)
    fig.tight_layout()
    return _save(fig, path)


def decile_heatmap(bundle: MultiBundle, path: Path) -> Path:
    """模型 × 十分位平均收益；单调性比单点 IC 更难伪装。"""
    setup_style()
    horizons = [h for h in (5, 10) if not bundle.deciles.get(h, pd.DataFrame()).empty]
    if not horizons:
        raise ValueError("缺少十分位数据")
    fig, axes = plt.subplots(
        len(horizons), 1, figsize=(7.4, 2.2 + 2.3 * len(horizons))
    )
    if len(horizons) == 1:
        axes = [axes]
    for ax, horizon in zip(axes, horizons, strict=True):
        frame = bundle.deciles[horizon].copy()
        rank = bundle.quality_ranking.get(horizon)
        order = (
            rank.sort_values("rank")["model"].tolist()
            if rank is not None and not rank.empty
            else frame.index.tolist()
        )
        order = [model for model in order if model in frame.index]
        frame = frame.loc[order]
        lookup = {str(column).lower(): column for column in frame.columns}
        columns = [lookup[f"d{number}"] for number in range(1, 11)]
        matrix = frame[columns].astype(float) * 100.0
        matrix.index = [display_name(model) for model in matrix.index]
        matrix.columns = [str(number) for number in range(1, 11)]
        _heat(ax, matrix, digits=2)
        ax.set_title(f"T+{horizon} 十分位平均收益（%）", loc="left", fontsize=9.4)
        ax.set_xlabel("预测分数从低（1）到高（10）", fontsize=7.2)
    fig.tight_layout()
    return _save(fig, path)


def monthly_heatmap(
    bundle: MultiBundle, horizon: int, family: str, path: Path
) -> Path:
    """月度收益热力图：模型 × 月份。"""
    setup_style()
    frame = bundle.monthly_returns.get((horizon, family))
    if frame is None or frame.empty:
        raise ValueError(f"缺少 T+{horizon} {family} 月度收益")
    rank = bundle.portfolio_ranking.get((horizon, family))
    order = (
        rank.sort_values("rank")["model"].tolist()
        if rank is not None and not rank.empty
        else frame.index.tolist()
    )
    if order and all(model in frame.columns for model in order):
        frame = frame.T
    order = [model for model in order if model in frame.index]
    matrix = frame.loc[order].astype(float) * 100.0
    matrix.index = [display_name(model) for model in matrix.index]
    fig, ax = plt.subplots(figsize=(7.4, 3.7))
    _heat(ax, matrix, digits=1)
    ax.set_title(
        f"T+{horizon} · {SHORT_FAMILY[family]} 月度收益（%）",
        loc="left",
        fontsize=9.6,
    )
    fig.tight_layout()
    return _save(fig, path)


def _cross_formatter(is_ic: bool) -> mtick.FuncFormatter:
    if is_ic:
        return mtick.FuncFormatter(lambda value, _: f"{value:.3f}")
    return mtick.FuncFormatter(lambda value, _: f"{value * 100:.0f}%")


def cross_horizon_scatter(bundle: MultiBundle, path: Path) -> Path:
    """T+5 与 T+10 的模型排序是否一致。"""
    setup_style()
    models = [key for key in bundle.all_models() if key in bundle.models(5)]
    models = [key for key in models if key in bundle.models(10)]
    if not models:
        raise ValueError("缺少两周期共同模型")
    left = bundle.quality_ranking[5].set_index("model")
    right = bundle.quality_ranking[10].set_index("model")
    top_left = bundle.portfolio_ranking.get((5, "top10"))
    top_right = bundle.portfolio_ranking.get((10, "top10"))
    if top_left is None or top_right is None or top_left.empty or top_right.empty:
        raise ValueError("缺少 Top10 组合榜")

    fig, axes = plt.subplots(1, 2, figsize=(7.2, 3.5))
    panels = (
        (
            axes[0],
            left["ic_mean"].reindex(models).to_numpy(float),
            right["ic_mean"].reindex(models).to_numpy(float),
            "IC 均值",
            "T+5 IC 均值",
            "T+10 IC 均值",
        ),
        (
            axes[1],
            top_left.set_index("model")["total_return"]
            .reindex(models)
            .to_numpy(float),
            top_right.set_index("model")["total_return"]
            .reindex(models)
            .to_numpy(float),
            "Top10 总收益",
            "T+5 总收益",
            "T+10 总收益",
        ),
    )
    for ax, x, y, title, xlabel, ylabel in panels:
        ax.scatter(x, y, s=30, color=SLATE, zorder=3)
        for index, model in enumerate(models):
            if not np.isfinite(x[index]) or not np.isfinite(y[index]):
                continue
            ax.annotate(
                model,
                (x[index], y[index]),
                textcoords="offset points",
                xytext=(4, 2),
                fontsize=6.4,
                color=MUTED,
            )
        low = float(np.nanmin([np.nanmin(x), np.nanmin(y)]))
        high = float(np.nanmax([np.nanmax(x), np.nanmax(y)]))
        pad = (high - low) * 0.08 if high > low else 0.01
        ax.plot(
            [low - pad, high + pad],
            [low - pad, high + pad],
            color=BENCH,
            lw=0.8,
            ls=(0, (4, 3)),
        )
        ax.set_title(title, loc="left", fontsize=9.2)
        ax.set_xlabel(xlabel, fontsize=7.4)
        ax.set_ylabel(ylabel, fontsize=7.4)
        ax.grid(color=GRID, lw=0.6)
        formatter = _cross_formatter(title == "IC 均值")
        ax.xaxis.set_major_formatter(formatter)
        ax.yaxis.set_major_formatter(formatter)
    fig.suptitle(
        "跨周期一致性：虚线为 y = x，点越靠近对角线说明两个持有期结论越一致",
        x=0.01,
        ha="left",
        fontsize=9.4,
    )
    fig.tight_layout()
    return _save(fig, path)


def render_all(bundle: MultiBundle, out_dir: Path) -> dict[str, Path]:
    """生成报告用到的全部图；缺数据的图跳过。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    jobs = {
        "ic": lambda: ic_dumbbell(bundle, out_dir / "m_ic.png"),
        "hit": lambda: hit_bars(bundle, out_dir / "m_hit.png"),
        "returns": lambda: portfolio_return_bars(bundle, out_dir / "m_returns.png"),
        "ic_dist": lambda: ic_distribution(bundle, out_dir / "m_ic_dist.png"),
        "deciles": lambda: decile_heatmap(bundle, out_dir / "m_deciles.png"),
    }
    paths: dict[str, Path] = {}
    for name, job in jobs.items():
        try:
            paths[name] = job()
        except ValueError:
            continue
    for horizon in bundle.horizons:
        for family in ("top10", "rolling"):
            try:
                paths[f"equity_{horizon}_{family}"] = equity_top_models(
                    bundle,
                    horizon,
                    family,
                    out_dir / f"m_equity_{horizon}_{family}.png",
                )
            except ValueError:
                continue
        try:
            paths[f"monthly_{horizon}"] = monthly_heatmap(
                bundle, horizon, "top10", out_dir / f"m_monthly_{horizon}.png"
            )
        except ValueError:
            continue
    try:
        paths["cross"] = cross_horizon_scatter(bundle, out_dir / "m_cross.png")
    except ValueError:
        pass
    return paths
