#!/usr/bin/env python3
"""用你自己的数据画三张图，讲清楚机器学习最核心的三个概念。

不需要联网，不需要看视频。

图1  找一条线           —— 什么叫"训练"
图2  过拟合曲线         —— 为什么训练好不代表真好
图3  IC 随时间变化      —— 怎么判断模型坏了
"""
from pathlib import Path

import duckdb
import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402

# 用仓库自带的中文字体
FONT = Path("/root/quant/QuantMind/docker/training/fonts/WQYZenHei.ttf")
if FONT.exists():
    font_manager.fontManager.addfont(str(FONT))
    plt.rcParams["font.family"] = font_manager.FontProperties(fname=str(FONT)).get_name()
plt.rcParams["axes.unicode_minus"] = False

OUT = Path("/root/quant/QuantMind/docs/images/learning")
OUT.mkdir(parents=True, exist_ok=True)

DATA = "/opt/quantmind/data/quantdb/6_ml_datasets/features_daily"
TRAIN_END = "2025-01-01"


def load(n: int = 200_000) -> pd.DataFrame:
    con = duckdb.connect()
    df = con.execute(f"""
        SELECT time, rsi_14, future_return_5d
        FROM read_parquet('{DATA}/*/data.parquet')
        WHERE time >= DATE '2022-01-01'
          AND rsi_14 IS NOT NULL AND future_return_5d IS NOT NULL
        USING SAMPLE {n} ROWS
    """).df()
    df["time"] = pd.to_datetime(df["time"])
    return df


def ic(a, b) -> float:
    """两个序列的相关系数。这就是 IC。"""
    return float(np.corrcoef(a, b)[0, 1])


def fig1_line(df: pd.DataFrame) -> None:
    """图1：找到的那条线。"""
    tr = df[df.time < TRAIN_END]
    te = df[df.time >= TRAIN_END]

    b, a = np.polyfit(tr.rsi_14, tr.future_return_5d, 1)
    xs = np.linspace(0, 100, 100)

    fig, axes = plt.subplots(1, 2, figsize=(13, 5), sharey=True)

    for ax, part, title in [
        (axes[0], tr, "训练集 2022–2024（用来找规律）"),
        (axes[1], te, "测试集 2025–2026（用来考试）"),
    ]:
        ax.scatter(part.rsi_14, part.future_return_5d, s=1, alpha=0.08, color="#4C78A8")
        ax.plot(xs, a + b * xs, color="#E45756", lw=2.5, label=f"拟合线：y = {a:.2f} {b:+.4f}×RSI")
        ax.set(xlim=(0, 100), ylim=(-25, 25), xlabel="RSI（今天能看到的特征）",
               title=title)
        ax.axhline(0, color="gray", lw=0.6, ls="--")
        ax.legend(loc="upper right", fontsize=9)
        ax.grid(alpha=0.2)

    axes[0].set_ylabel("未来 5 日收益 %（5 天后才知道的标签）")
    fig.suptitle("图1｜「训练」就是找一条最贴近历史数据的线", fontsize=14, y=1.02)
    fig.tight_layout()
    fig.savefig(OUT / "01_fit_line.png", dpi=110, bbox_inches="tight")
    plt.close(fig)


def fig2_overfit(df: pd.DataFrame) -> None:
    """图2：模型越复杂，训练集越好看，测试集越差 —— 过拟合。"""
    tr_all = df[df.time < TRAIN_END]
    te = df[df.time >= TRAIN_END]

    # 只拿一小撮历史数据训练（模拟"样本很少"的真实处境），
    # 这样高次多项式才真的能把训练集"背"下来，过拟合才看得见。
    tr = tr_all.sample(n=1500, random_state=42)
    xs = np.linspace(0, 100, 300)

    degrees = [1, 2, 3, 5, 8, 12, 20]
    train_ic, test_ic = [], []

    for d in degrees:
        coef = np.polyfit(tr.rsi_14, tr.future_return_5d, d)
        f = np.poly1d(coef)
        train_ic.append(ic(f(tr.rsi_14), tr.future_return_5d))
        test_ic.append(ic(f(te.rsi_14), te.future_return_5d))

    fig, axes = plt.subplots(1, 2, figsize=(13, 5))

    ax = axes[0]
    ax.plot(degrees, train_ic, "o-", color="#E45756", lw=2, label="训练集 IC（考试前看的答案）")
    ax.plot(degrees, test_ic, "o-", color="#4C78A8", lw=2, label="测试集 IC（真实水平）")
    ax.set(xlabel="模型复杂度（多项式次数）", ylabel="IC",
           title="模型越复杂 → 训练集越好看，测试集反而变差")
    ax.axhline(0, color="gray", lw=0.6, ls="--")
    ax.legend(fontsize=9)
    ax.grid(alpha=0.25)

    ax = axes[1]
    for d, color in [(1, "#4C78A8"), (20, "#E45756")]:
        coef = np.polyfit(tr.rsi_14, tr.future_return_5d, d)
        f = np.poly1d(coef)
        ax.plot(xs, f(xs), lw=2, color=color, label=f"{d} 次多项式（测试IC={test_ic[degrees.index(d)]:+.4f}）")
    ax.scatter(tr.rsi_14, tr.future_return_5d, s=1, alpha=0.05, color="gray")
    ax.set(xlim=(0, 100), ylim=(-25, 25), xlabel="RSI", ylabel="未来 5 日收益 %",
           title="简单模型 vs 复杂模型，在真实数据上的样子")
    ax.axhline(0, color="gray", lw=0.6, ls="--")
    ax.legend(fontsize=9)
    ax.grid(alpha=0.2)

    fig.suptitle("图2｜什么叫「过拟合」", fontsize=14, y=1.02)
    fig.tight_layout()
    fig.savefig(OUT / "02_overfit.png", dpi=110, bbox_inches="tight")
    plt.close(fig)

    print("多项式次数 / 训练IC / 测试IC：")
    for d, a, b in zip(degrees, train_ic, test_ic):
        print(f"  {d:2d} 次   {a:+.4f}   {b:+.4f}")


def fig3_ic_time(df: pd.DataFrame) -> None:
    """图3：IC 是逐日算的，看它稳不稳。"""
    tr = df[df.time < TRAIN_END]
    b, a = np.polyfit(tr.rsi_14, tr.future_return_5d, 1)

    d = df.copy()
    d["pred"] = a + b * d.rsi_14
    daily = (
        d.groupby("time")
        .apply(lambda g: ic(g.pred, g.future_return_5d), include_groups=False)
        .rolling(20, min_periods=10)
        .mean()
    )

    fig, ax = plt.subplots(figsize=(13, 4.5))
    ax.plot(daily.index, daily.values, color="#4C78A8", lw=1.6)
    ax.axvline(pd.Timestamp(TRAIN_END), color="#E45756", ls="--", lw=2)
    ax.axhline(0, color="gray", lw=0.6)
    ax.text(pd.Timestamp(TRAIN_END), ax.get_ylim()[1] * 0.85,
            "  训练/测试分界", color="#E45756", fontsize=10, va="top")
    ax.set(xlabel="时间", ylabel="IC（20 日移动平均）",
           title="图3｜IC 是逐日算的：同一套规则，在不同年份效果会变")
    ax.grid(alpha=0.25)
    fig.tight_layout()
    fig.savefig(OUT / "03_ic_time.png", dpi=110, bbox_inches="tight")
    plt.close(fig)


def main() -> None:
    df = load()
    print(f"样本 {len(df):,} 行，{df.time.min().date()} ~ {df.time.max().date()}")
    fig1_line(df)
    fig2_overfit(df)
    fig3_ic_time(df)
    print(f"图已写入 {OUT}")


if __name__ == "__main__":
    main()
