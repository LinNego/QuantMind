"""PDF 报告的图表生成。

图表只使用白底、细线和直接标注，避免默认模板感；A 股口径红涨绿跌。
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.dates as mdates
import matplotlib.font_manager as font_manager
import matplotlib.pyplot as plt
import matplotlib.ticker as mtick
import numpy as np
import pandas as pd

from .report_pdf_data import (
    INDEX_TITLES,
    INITIAL_CAPITAL,
    ModelReport,
    ReportBundle,
)

FONT_SANS = "Noto Sans CJK SC"
FONT_SERIF = "Noto Serif CJK SC"

INK = "#1D1D1B"
MUTED = "#6E6A63"
GRID = "#E7E2D8"
CREAM = "#F7F3EA"
POS = "#A62B2F"
NEG = "#1E6B4F"
AMBER = "#C98A2E"
SLATE = "#3E4A5A"
TEAL = "#2F6F73"
BENCH = "#8C877C"
INDEX = "#B8B2A6"

MODEL_COLORS = {
    "T5": {"top20": POS, "rolling": AMBER},
    "T10": {"top20": SLATE, "rolling": TEAL},
}


def setup_style() -> None:
    for path in (
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc",
    ):
        if Path(path).is_file():
            font_manager.fontManager.addfont(path)
    plt.rcParams.update(
        {
            "font.family": [FONT_SANS],
            "font.size": 8.0,
            "axes.titlesize": 9.0,
            "axes.labelsize": 7.8,
            "xtick.labelsize": 7.2,
            "ytick.labelsize": 7.2,
            "legend.fontsize": 7.2,
            "axes.unicode_minus": False,
            "axes.edgecolor": "#B8B2A6",
            "axes.linewidth": 0.6,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.grid": True,
            "grid.color": GRID,
            "grid.linewidth": 0.6,
            "grid.alpha": 0.9,
            "figure.facecolor": "white",
            "axes.facecolor": "white",
            "savefig.facecolor": "white",
            "savefig.dpi": 300,
            "savefig.bbox": "tight",
            "savefig.pad_inches": 0.04,
        }
    )


def _save(fig: plt.Figure, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path)
    plt.close(fig)
    return path


def _date_axis(ax: plt.Axes) -> None:
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=2))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
    ax.tick_params(axis="x", length=0)
    ax.margins(x=0.01)


def _percent_axis(ax: plt.Axes, axis: str = "y") -> None:
    formatter = mtick.FuncFormatter(lambda value, _: f"{value * 100:.0f}%")
    if axis == "y":
        ax.yaxis.set_major_formatter(formatter)
    else:
        ax.xaxis.set_major_formatter(formatter)


def _clip_to(series: pd.Series, end: pd.Timestamp) -> pd.Series:
    return series.loc[:end].dropna()


def equity_chart(report: ModelReport, path: Path) -> Path:
    """单模型的净值曲线：Top20、滚动 Top5、等权池、沪深 300。"""
    end = report.equity_top20.index[-1]
    top = _clip_to(report.equity_top20, end) / INITIAL_CAPITAL
    rolling = _clip_to(report.equity_rolling, end) / INITIAL_CAPITAL
    benchmark = _clip_to(report.benchmark_ew, end) / INITIAL_CAPITAL
    if "000300.SH" in report.index_closes.columns:
        csi300 = _clip_to(report.index_closes["000300.SH"], end)
        csi300 = csi300 / csi300.iloc[0]
    else:
        csi300 = pd.Series(dtype=float)

    fig, ax = plt.subplots(figsize=(7.1, 2.65))
    if len(csi300):
        ax.plot(
            csi300.index,
            csi300,
            color=INDEX,
            lw=0.9,
            ls=(0, (4, 3)),
            label=f"沪深 300（{csi300.iloc[-1] - 1:+.1%}）",
        )
    ax.plot(
        benchmark.index,
        benchmark,
        color=BENCH,
        lw=1.0,
        label=f"等权可交易池（{benchmark.iloc[-1] - 1:+.1%}）",
    )
    ax.plot(
        rolling.index,
        rolling,
        color=MODEL_COLORS[report.key]["rolling"],
        lw=1.4,
        label=f"{report.title} 滚动 Top5（{rolling.iloc[-1] - 1:+.1%}）",
    )
    ax.plot(
        top.index,
        top,
        color=MODEL_COLORS[report.key]["top20"],
        lw=1.8,
        label=f"{report.title} Top20（{top.iloc[-1] - 1:+.1%}）",
    )
    ax.axhline(1.0, color=MUTED, lw=0.7, ls=(0, (2, 3)))
    _date_axis(ax)
    ax.set_ylabel("净值（起点 = 1.00）")
    ax.set_ylim(bottom=min(0.90, float(top.min()) - 0.02))
    ax.legend(loc="upper left", frameon=False, ncol=2, handlelength=1.6)
    ax.grid(axis="x", visible=False)
    return _save(fig, path)


def drawdown_chart(bundle: ReportBundle, path: Path) -> Path:
    """两个模型的回撤曲线，上下两栏。"""
    fig, axes = plt.subplots(2, 1, figsize=(7.1, 4.05))
    for ax, key in zip(axes, ("T5", "T10"), strict=True):
        report = bundle.models[key]
        colors = MODEL_COLORS[key]
        end = report.equity_top20.index[-1]
        for series, label, color, fill in (
            (report.equity_top20, f"{report.title} Top20", colors["top20"], 0.16),
            (report.equity_rolling, f"{report.title} 滚动 Top5", colors["rolling"], 0.08),
        ):
            clipped = _clip_to(series, end)
            drawdown = clipped / clipped.cummax() - 1.0
            ax.plot(drawdown.index, drawdown, color=color, lw=1.0, label=label)
            ax.fill_between(drawdown.index, drawdown, 0, color=color, alpha=fill, lw=0)
        ax.axhline(0, color=INK, lw=0.7)
        _date_axis(ax)
        _percent_axis(ax)
        ax.set_ylim(-0.26, 0.025)
        ax.set_title(report.title, loc="left", fontweight="bold", color=INK)
        ax.legend(loc="lower left", frameon=False, ncol=2, handlelength=1.4)
        ax.grid(axis="x", visible=False)
    fig.subplots_adjust(hspace=0.42)
    return _save(fig, path)


def monthly_returns_chart(bundle: ReportBundle, path: Path) -> Path:
    """四个组合的月度收益分组柱状图。"""
    months = sorted(bundle.models["T5"].metrics["top20"]["monthly"])
    series = {
        "T+5 Top20": [
            bundle.models["T5"].metrics["top20"]["monthly"][month] for month in months
        ],
        "T+5 滚动": [
            bundle.models["T5"].metrics["rolling"]["monthly"][month] for month in months
        ],
        "T+10 Top20": [
            bundle.models["T10"].metrics["top20"]["monthly"][month] for month in months
        ],
        "T+10 滚动": [
            bundle.models["T10"].metrics["rolling"]["monthly"][month] for month in months
        ],
    }
    colors = {"T+5 Top20": POS, "T+5 滚动": AMBER, "T+10 Top20": SLATE, "T+10 滚动": TEAL}
    positions = np.arange(len(months), dtype=float)
    width = 0.20
    fig, ax = plt.subplots(figsize=(7.1, 2.75))
    ax.axvspan(4.5, 6.5, color=CREAM, alpha=0.75, zorder=0, lw=0)
    offsets = np.linspace(-1.5 * width, 1.5 * width, len(series))
    for offset, (label, values) in zip(offsets, series.items(), strict=True):
        ax.bar(
            positions + offset,
            np.array(values) * 100,
            width=width * 0.92,
            color=colors[label],
            label=label,
            zorder=2,
        )
    ax.axhline(0, color=INK, lw=0.7, zorder=3)
    ax.set_xticks(positions)
    ax.set_xticklabels([f"{int(month[5:])} 月" for month in months])
    ax.yaxis.set_major_formatter(mtick.FuncFormatter(lambda value, _: f"{value:.0f}%"))
    ax.set_ylabel("月度收益")
    ax.legend(loc="upper left", frameon=False, ncol=4, handlelength=1.2)
    ax.grid(axis="x", visible=False)
    ax.text(5.5, ax.get_ylim()[1] * 0.90, "5–6 月连续回撤", ha="center", va="top",
            fontsize=7, color=MUTED)
    return _save(fig, path)


def ic_series_chart(bundle: ReportBundle, path: Path) -> Path:
    """逐日 IC 与 20 日均值，上下两栏。"""
    fig, axes = plt.subplots(2, 1, figsize=(7.1, 4.05))
    for ax, key in zip(axes, ("T5", "T10"), strict=True):
        report = bundle.models[key]
        frame = report.quality_daily[["date", "ic_eligible"]].dropna()
        values = frame["ic_eligible"] * 100
        rolling = values.rolling(20, min_periods=10).mean()
        ax.plot(frame["date"], values, color="#C9C3B8", lw=0.55, label="逐日 IC")
        ax.plot(frame["date"], rolling, color=POS, lw=1.5, label="20 日均值")
        mean = float(values.mean())
        ax.axhline(0, color=INK, lw=0.7)
        ax.axhline(mean, color=POS, lw=0.7, ls=(0, (3, 3)))
        _date_axis(ax)
        ax.set_ylim(-45, 55)
        ax.yaxis.set_major_formatter(mtick.FuncFormatter(lambda value, _: f"{value:.0f}%"))
        ax.set_title(report.title, loc="left", fontweight="bold", color=INK)
        ax.legend(loc="lower left", frameon=False, ncol=2, handlelength=1.4)
        ax.grid(axis="x", visible=False)
        ax.text(
            0.99,
            0.94,
            f"均值 {mean:.1f}% · NW t = {report.nw_ic['t']:.2f} · 样本 {int(report.nw_ic['n'])} 天",
            transform=ax.transAxes,
            ha="right",
            va="top",
            fontsize=7.2,
            color=MUTED,
        )
    fig.subplots_adjust(hspace=0.42)
    return _save(fig, path)


def trade_distribution_chart(bundle: ReportBundle, path: Path) -> Path:
    """滚动 Top5 的单笔净收益分布：解释低胜率为何仍能赚钱。"""
    fig, axes = plt.subplots(1, 2, figsize=(7.1, 2.55))
    for ax, key in zip(axes, ("T5", "T10"), strict=True):
        report = bundle.models[key]
        stats = report.stats_rolling
        values = report.round_trips_rolling["net_ret"].astype(float) * 100
        values = values.clip(-15, 15)
        counts, edges = np.histogram(values, bins=np.linspace(-15, 15, 49))
        centers = (edges[:-1] + edges[1:]) / 2.0
        bar_colors = [POS if center > 0 else NEG for center in centers]
        ax.bar(centers, counts, width=(edges[1] - edges[0]) * 0.88, color=bar_colors, alpha=0.86)
        ax.axvline(0, color=INK, lw=0.7)
        ax.axvline(stats["net_avg"] * 100, color=INK, lw=0.9, ls="-")
        ax.axvline(stats["median"] * 100, color=MUTED, lw=0.9, ls=(0, (3, 3)))
        ax.set_xlim(-15, 15)
        ax.set_xlabel("单笔净收益（%）")
        ax.set_ylabel("笔数")
        ax.set_title(
            f"{report.title} 滚动 Top5",
            loc="left",
            fontweight="bold",
            color=INK,
        )
        ax.grid(axis="x", visible=False)
        ax.text(
            0.02,
            0.95,
            f"胜率 {stats['net_win']:.1%} · 盈亏比 {stats['payoff']:.2f}\n"
            f"均值 {stats['net_avg']:+.2%} · 中位数 {stats['median']:+.2%}",
            transform=ax.transAxes,
            ha="left",
            va="top",
            fontsize=6.9,
            color=MUTED,
        )
    fig.subplots_adjust(wspace=0.22)
    return _save(fig, path)


def cost_sensitivity_chart(bundle: ReportBundle, path: Path) -> Path:
    """基准、加印花税、加滑点三档成本敏感性。"""
    groups = [
        ("T+5 Top20", "T5", "top20_variants"),
        ("T+5 滚动", "T5", "rolling_variants"),
        ("T+10 Top20", "T10", "top20_variants"),
        ("T+10 滚动", "T10", "rolling_variants"),
    ]
    labels = ["双边万 3", "加印花税 5bp", "加滑点 5bp"]
    colors = [POS, AMBER, SLATE]
    keys = ["baseline", "stamp_duty_5bp", "slippage_5bp"]
    positions = np.arange(len(groups), dtype=float)
    height = 0.24
    fig, ax = plt.subplots(figsize=(7.1, 2.75))
    for offset, label, color, key in zip(
        np.linspace(-height, height, 3), labels, colors, keys, strict=True
    ):
        values = [
            bundle.models[model].metrics[variant][key]["performance"]["total_return"] * 100
            for _, model, variant in groups
        ]
        bars = ax.barh(
            positions + offset,
            values,
            height=height * 0.92,
            color=color,
            label=label,
        )
        for bar, value in zip(bars, values, strict=True):
            ax.text(
                value + 0.25,
                bar.get_y() + bar.get_height() / 2,
                f"{value:.1f}",
                va="center",
                fontsize=6.6,
                color=MUTED,
            )
    ax.set_yticks(positions)
    ax.set_yticklabels([group[0] for group in groups])
    ax.invert_yaxis()
    ax.axvline(0, color=INK, lw=0.7)
    ax.xaxis.set_major_formatter(mtick.FuncFormatter(lambda value, _: f"{value:.0f}%"))
    ax.set_xlabel("区间总收益")
    ax.legend(loc="lower right", frameon=False, ncol=3, handlelength=1.2)
    ax.grid(axis="y", visible=False)
    ax.set_xlim(0, max(16.0, ax.get_xlim()[1] + 1.0))
    return _save(fig, path)


def render_all(bundle: ReportBundle, out_dir: Path) -> dict[str, Path]:
    setup_style()
    return {
        "equity_t5": equity_chart(bundle.models["T5"], out_dir / "equity_t5.png"),
        "equity_t10": equity_chart(bundle.models["T10"], out_dir / "equity_t10.png"),
        "drawdown": drawdown_chart(bundle, out_dir / "drawdown.png"),
        "monthly": monthly_returns_chart(bundle, out_dir / "monthly_returns.png"),
        "ic": ic_series_chart(bundle, out_dir / "ic_series.png"),
        "trade_dist": trade_distribution_chart(bundle, out_dir / "trade_distribution.png"),
        "cost": cost_sensitivity_chart(bundle, out_dir / "cost_sensitivity.png"),
    }
