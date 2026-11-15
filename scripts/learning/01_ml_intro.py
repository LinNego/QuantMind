#!/usr/bin/env python3
"""第 1 课：机器学习最小实例 —— 特征、标签、训练、验证、泄漏

给后端背景的同学：先把 ML 术语翻译成你熟悉的东西。

    ML 术语          后端类比
    ---------------  ------------------------------------------------
    特征 (feature)   请求参数。今天能看到的数值：RSI、均线偏离、换手率
    标签 (label)     期望响应。未来 5 天涨了多少 —— 训练时才有，预测时没有
    模型 (model)     一个函数 f(特征) -> 预测。就是一堆权重的矩阵乘法
    训练 (fit)       用历史数据解出这堆权重（最小二乘）
    样本内 (in-sample)  用训练集评估 —— 相当于拿测试数据写单测
    样本外 (out-of-sample) 用没见过的数据评估 —— 真正的验收
    过拟合 (overfit) 把训练集的噪声也背下来，换个数据就崩
    未来函数 (leakage) 特征里混进了"当时不可能知道"的信息

本脚本只做一件事：用 6 个常见特征，预测未来 5 日收益，
然后演示"看起来很好"和"真实水平"之间的差距是怎么来的。

依赖：numpy + pandas + duckdb（本机已装，不需要 sklearn）
"""
from __future__ import annotations

import duckdb
import numpy as np
import pandas as pd

DATA = "/opt/quantmind/data/quantdb/6_ml_datasets/features_daily"

# 选 6 个特征。都是"今天收盘后就能算出来"的，不含未来信息。
# ⚠️ 注意：features_daily 有 29 列是 VARCHAR（字符串），包括 div_yield、pb_mrq、
#    ever_zt_count 这些看起来像数值的列。直接拿来算会静默变常数或报错。
#    这里只选 DOUBLE 类型且方差非零的列。
FEATURES = [
    "ma_gap_5",      # 收盘价相对 5 日均线的偏离
    "ma_gap_20",     # 相对 20 日均线
    "rsi_14",        # 相对强弱指标
    "vol_std_20",    # 20 日波动率
    "macd_hist",     # MACD 柱
    "vol_to_ma20",   # 成交量相对 20 日均量
]
LABEL = "future_return_5d"   # 未来 5 日收益（数据里已算好）
LEAK = "future_return_1d"    # 故意用来演示泄漏的列

TRAIN_END = "2024-01-01"     # 2024 年前训练，之后验证


def load() -> pd.DataFrame:
    """只取需要的列，并做一个基础股票池过滤（非 ST、价格 > 1 元）。

    同时做一次数据体检：打印每列的缺失率和方差。
    实战里这一步比模型选择重要得多 —— 一列全是空值或常数，
    模型会给出毫无意义的权重，而且不报错。
    """
    con = duckdb.connect()
    cols = ", ".join(["symbol", "time", "close", "is_st"] + FEATURES + [LABEL, LEAK])
    df = con.execute(
        f"SELECT {cols} FROM read_parquet('{DATA}/*/data.parquet') "
        "WHERE time >= DATE '2020-01-01'"
    ).df()
    df = df[(df.is_st != 1) & (df.close > 1.0)].copy()
    df["time"] = pd.to_datetime(df["time"])

    print("\n[数据体检] 每列缺失率 / 标准差（标准差 = 0 说明整列是常数，必须剔除）：")
    for c in FEATURES + [LABEL]:
        na = df[c].isna().mean() * 100
        sd = df[c].std()
        flag = "  ⚠️ 常数列！" if sd == 0 else ""
        print(f"  {c:16} 缺失 {na:5.2f}%   标准差 {sd:10.4f}{flag}")
    return df


def cross_section_rank(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """按「天」做截面百分位排名，映射到 0~1。

    为什么要排名而不是直接用原始值？
      - 不同股票的 RSI 不可比，但"今天在全市场排前 10%"是可比的
      - 消除量纲、去极值（一个 500 倍的异常值不会毁掉模型）
    这正是量化里最标准的特征处理方式（rank normalization）。
    """
    out = df.copy()
    out[cols] = out.groupby("time")[cols].rank(pct=True)
    out[cols] = out[cols].fillna(0.5)          # 缺失填中位数
    return out


def fit_ols(X: np.ndarray, y: np.ndarray) -> np.ndarray:
    """最小二乘解权重。这就是「训练」——一行公式，没有魔法。

    在 X 前面加一列 1（截距项），解 w 使 ||Xw - y||^2 最小。

    ⚠️ 两个防呆检查（实战里救过很多人）：
      1. 常数列会让设计矩阵奇异，解出来的权重是数值垃圾；
      2. 条件数过大说明特征之间高度共线，权重不可解释。
    """
    sd = X.std(axis=0)
    if (sd == 0).any():
        bad = [FEATURES[i] for i in np.where(sd == 0)[0]]
        raise ValueError(f"这些特征是常数，必须剔除：{bad}")

    X1 = np.column_stack([np.ones(len(X)), X])
    cond = np.linalg.cond(X1)
    if cond > 1e8:
        raise ValueError(f"设计矩阵条件数 {cond:.3e} 过大，特征可能强共线")
    w, *_ = np.linalg.lstsq(X1, y, rcond=None)
    return w


def predict(w: np.ndarray, X: np.ndarray) -> np.ndarray:
    X1 = np.column_stack([np.ones(len(X)), X])
    return X1 @ w


def daily_ic(df: pd.DataFrame, pred_col: str, label_col: str) -> pd.Series:
    """每天算一个 IC（预测值与真实收益的截面相关系数）。

    IC = information coefficient。量化里最核心的指标：
      IC > 0.02~0.03 就算有信息，> 0.05 已经很强
      IC 为 0 = 模型没用，IC 为负 = 反向预测
    """
    g = df.groupby("time")
    ic = g.apply(
        lambda d: d[pred_col].corr(d[label_col], method="spearman"),
        include_groups=False,
    )
    return ic.dropna()


def report(name: str, ic: pd.Series, overlap: int = 1) -> dict:
    """把一串日频 IC 汇总成几个数。

    ⚠️ t 值的陷阱：标签是"未来 5 日收益"，所以相邻 5 天的 IC 共享 4/5 的收益窗口，
    彼此高度自相关 —— 有效独立样本只有 N/overlap，不是 N。
    直接用 N 算 t，会把显著性放大约 sqrt(5)≈2.2 倍。
    """
    mean = ic.mean()
    std = ic.std()
    icir = mean / std if std else np.nan
    eff_n = len(ic) / overlap
    t = icir * np.sqrt(eff_n)
    return {
        "情景": name,
        "天数": len(ic),
        "平均IC": round(mean, 4),
        "ICIR": round(icir, 3),
        "t值(校正)": round(t, 2),
    }


def main() -> None:
    print("=" * 70)
    print("第 1 步：加载数据")
    print("=" * 70)
    df = load()
    print(f"样本：{len(df):,} 行，{df.symbol.nunique()} 只股票，"
          f"{df.time.min().date()} ~ {df.time.max().date()}")
    print("\n前 3 行（注意 future_return_5d 是「未来」的，训练才用）：")
    print(df[["symbol", "time", "close"] + FEATURES[:2] + [LABEL]].head(3).to_string(index=False))

    print("\n" + "=" * 70)
    print("第 2 步：特征工程 —— 按天做截面排名")
    print("=" * 70)
    df = cross_section_rank(df, FEATURES + [LABEL, LEAK])
    print("排名后，所有特征都变成 0~1 之间的百分位：")
    print(df[FEATURES].describe().loc[["min", "50%", "max"]].round(3).to_string())

    print("\n" + "=" * 70)
    print("第 3 步：按时间切分训练集 / 验证集")
    print("=" * 70)
    train = df[df.time < TRAIN_END]
    test = df[df.time >= TRAIN_END]
    print(f"训练集（{TRAIN_END} 之前）：{len(train):,} 行")
    print(f"验证集（{TRAIN_END} 之后）：{len(test):,} 行")

    rows = []

    # ---------- 情景 A：诚实的做法（样本外） ----------
    w = fit_ols(train[FEATURES].to_numpy(), train[LABEL].to_numpy())
    test = test.copy()
    test["pred"] = predict(w, test[FEATURES].to_numpy())
    rows.append(report("A 诚实：训练早期 / 验证后期（样本外）", daily_ic(test, "pred", LABEL), overlap=5))

    # ---------- 情景 B：自欺的做法（样本内） ----------
    tr = train.copy()
    tr["pred"] = predict(w, tr[FEATURES].to_numpy())
    rows.append(report("B 自欺：在训练集上评估（样本内）", daily_ic(tr, "pred", LABEL), overlap=5))

    # ---------- 情景 C：未来函数（把明天的收益当特征） ----------
    feats_leak = FEATURES + [LEAK]
    w2 = fit_ols(train[feats_leak].to_numpy(), train[LABEL].to_numpy())
    t2 = test.copy()
    t2["pred"] = predict(w2, t2[feats_leak].to_numpy())
    rows.append(report("C 泄漏：特征里混了未来 1 日收益", daily_ic(t2, "pred", LABEL), overlap=5))

    # ---------- 情景 D：安慰剂（把标签打乱再训练） ----------
    # 关键：只跑一个种子会误导。跑多个种子，看它是不是围绕 0 随机波动。
    placebo = []
    for seed in (1, 42, 123):
        y_shuffled = train[LABEL].to_numpy().copy()
        np.random.default_rng(seed).shuffle(y_shuffled)
        w3 = fit_ols(train[FEATURES].to_numpy(), y_shuffled)
        t3 = test.copy()
        t3["pred"] = predict(w3, t3[FEATURES].to_numpy())
        placebo.append(daily_ic(t3, "pred", LABEL).mean())
    rows.append({
        "情景": "D 安慰剂：标签打乱（3 个种子）",
        "天数": 678,
        "平均IC": f"{np.mean(placebo):+.4f}（区间 {min(placebo):+.3f} ~ {max(placebo):+.3f}）",
        "ICIR": "-",
        "t值(校正)": "-",
    })

    print("\n" + "=" * 70)
    print("第 4 步：四种情景对比")
    print("=" * 70)
    print(pd.DataFrame(rows).to_string(index=False))

    print("""
怎么读这张表
------------
平均IC   每天「预测值和真实收益」的相关系数均值。0.02 就有信息，0.05 很强。
         A 行就是这套特征的真实水平。
ICIR     IC 的稳定性 = 平均IC / IC标准差。比单纯平均 IC 更能说明问题。
t值      已按标签重叠度（5 日）校正。绝对值 > 2 才算统计上显著。

关键对比
--------
A vs B  同样一个模型，样本内（B）比样本外（A）好看 —— 这就是「过拟合」的样子。
         差距越大，说明模型越是在背训练集的噪声。
C        特征里塞了未来信息，IC 会跳到离谱的水平 —— 这叫未来函数。
         只要你看到 IC 突然变得很高，第一反应应该是：是不是泄漏了。
D        标签打乱后再训练，IC 应该围绕 0 随机波动。
         ⚠️ 只跑一个种子会被误导：单独看可能冒出 +0.05 甚至 -0.05，
            那是噪声，不是信号。所以必须跑多个种子看区间。

两个容易忽略的坑
----------------
1. t 值别乱信：5 日标签意味着相邻 5 天的 IC 共享收益窗口，彼此高度相关。
   有效样本只有 天数/5，直接用天数算 t 会把显著性放大约 2.2 倍。
2. 安慰剂必须多跑几次：单次结果可能看起来「有信号」，其实是噪声。
""")

    print("=" * 70)
    print("第 5 步：模型到底学到了什么（看权重）")
    print("=" * 70)
    names = ["截距"] + FEATURES
    print(pd.DataFrame({"特征": names, "权重": np.round(w, 4)}).to_string(index=False))
    print("""
权重的含义：权重为正 = 这个特征越高，未来收益越高（在截面意义上）。
注意：单个权重小不代表没用，因为特征之间相关；要看整体 IC。
""")


if __name__ == "__main__":
    main()
