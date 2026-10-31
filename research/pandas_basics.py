#!/usr/bin/env python3
"""pandas 入门：把一年 A 股日线走完「读 → 查 → 变形 → 算 → 存」五步。

配套文档：docs/量化学习/09-pandas入门.md
用法：
    cd /root/quant/QuantMind
    /root/quant/.venv/bin/python research/pandas_basics.py

数据来源：/opt/quantmind/data/quantdb（hive 分区 parquet），可用
环境变量 QM_QUANTDB_ROOT 覆盖；找不到数据时退化为合成数据，脚本照样能跑通。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import numpy as np
import pandas as pd

YEAR = 2024
ROOT = Path(os.environ.get("QM_QUANTDB_ROOT", "/opt/quantmind/data/quantdb"))
KLINE = ROOT / "1_kline_data" / "daily_unadjusted"

# 本脚本按 pandas 3.x 写：CoW 常开、字符串默认 str dtype、resample 用 ME
MIN_PANDAS = (3, 0)


def rule(title: str) -> None:
    print(f"\n{'=' * 66}\n{title}\n{'=' * 66}")


def mb(df: pd.DataFrame) -> str:
    return f"{df.memory_usage(deep=True).sum() / 1e6:.0f}MB"


def load() -> pd.DataFrame:
    """第 1 步：读。hive 分区目录可以直接喂给 read_parquet，
    filters 按分区裁剪、columns 只取需要的列，两者都发生在读盘阶段。"""
    if KLINE.exists():
        t0 = time.time()
        df = pd.read_parquet(
            KLINE,
            filters=[
                ("dt", ">=", YEAR * 10000 + 101),
                ("dt", "<=", YEAR * 10000 + 1231),
            ],
            columns=[
                "symbol", "time", "open", "high", "low",
                "close", "volume", "amount",
            ],
        )
        print(f"数据源 {KLINE}（读盘 {time.time() - t0:.2f}s）")
        return df
    print(f"[warn] 没找到 {KLINE}，改用合成数据（结构一致，数字无意义）")
    rng = np.random.default_rng(0)
    days = pd.bdate_range(f"{YEAR}-01-01", f"{YEAR}-12-31")
    syms = [f"{i:06d}.SZ" for i in range(1, 301)]
    close = 10 * np.exp(np.cumsum(rng.normal(0, 0.02, (len(days), len(syms))), axis=0))
    long = close.reshape(-1)
    return pd.DataFrame(
        {
            "symbol": np.tile(syms, len(days)),
            "time": np.repeat(days, len(syms)),
            "open": long, "high": long * 1.01, "low": long * 0.99,
            "close": long, "volume": rng.integers(1e5, 1e7, long.size),
            "amount": long * 1e6,
        }
    )


def main() -> None:
    rule("0 · 环境")
    print("pandas", pd.__version__, "| numpy", np.__version__)
    if tuple(int(x) for x in pd.__version__.split(".")[:2]) < MIN_PANDAS:
        print("[warn] 本脚本按 pandas 3.x 写，2.x 上 resample('ME') / str dtype 等行为不同")

    # ---------------------------------------------------------------- 读
    rule("1 · 读：形状、dtype、时间范围")
    df = load()
    print("形状：", df.shape, "  内存：", mb(df))
    print("dtype：", {k: str(v) for k, v in df.dtypes.items()})
    print("时间：", df.time.min().date(), "→", df.time.max().date())
    print("股票数：", df.symbol.nunique(), " 交易日：", df.time.nunique())
    print("code 列 dtype =", df.symbol.dtype, "（pandas 3 默认 str，不再是 object）")

    # ---------------------------------------------------------------- 查
    rule("2 · 查：五个动作，按顺序做，不要一上来就画图")
    print("-- head(3)")
    print(df.head(3).to_string(index=False))
    print("\n-- 缺失率 top3")
    na = df.isna().mean().sort_values(ascending=False).head(3) * 100
    print(na.round(2).to_string())
    print("\n-- 脏数据体检：close<=0 / high<low / volume<=0")
    bad = {
        "close<=0": int((df.close <= 0).sum()),
        "high<low": int((df.high < df.low).sum()),
        "volume<=0": int((df.volume <= 0).sum()),
    }
    print(bad, "← 全 0 说明停牌日是「整行缺失」，不是填 NaN")
    print("\n-- 一个数字反推单位：close * volume / amount 的中位数")
    ratio = (df.close * df.volume / df.amount).median()
    print(f"{ratio:.0f} ← ≈10000 说明 amount 的单位是「万元」，不是「元」")

    # ------------------------------------------------------------- 变形
    rule("3 · 变形：长表 → 宽表，宽表 → 长表")
    t0 = time.time()
    px = df.pivot(index="time", columns="symbol", values="close").sort_index()
    print(f"long {df.shape} → wide {px.shape}（pivot {time.time() - t0:.2f}s）")
    print(px.iloc[:3, :4].to_string())
    print("\n截面排名 rank(axis=1)：今天全市场涨幅前 3")
    ret1 = px.pct_change()
    print(ret1.rank(axis=1, ascending=False).iloc[-1].nsmallest(3).to_string())
    back = px.iloc[:2, :2].reset_index().melt(
        id_vars="time", var_name="symbol", value_name="close"
    )
    print("\nmelt 回长表（前 2 行 × 2 列）：")
    print(back.to_string(index=False))

    # ---------------------------------------------------------------- 算
    rule("4 · 算：窗口 / 截面 / 分组，量化的三件套")
    rev20 = -(px / px.shift(20) - 1)          # 因子：过去 20 日跌得多的
    fwd5 = px.shift(-5) / px - 1              # 标签：未来 5 日涨跌（shift(-5) 是往后看）
    ic = rev20.corrwith(fwd5, axis=1, method="spearman").dropna()
    print(f"IC 均值 {ic.mean():.4f} | ICIR {ic.mean() / ic.std():.3f} | 样本 {len(ic)} 天")
    print("注意样本 242 → %d 天：shift(20) 吃掉开头 20 天，shift(-5) 吃掉结尾 5 天" % len(ic))

    q = rev20.rank(axis=1, pct=True)
    bars = [
        fwd5.where((q > g / 5) & (q <= (g + 1) / 5)).mean(axis=1).mean() * 100
        for g in range(5)
    ]
    print(
        "按因子分 5 组，持有 5 日的平均收益：",
        " ".join(f"Q{i + 1} {b:+.2f}%" for i, b in enumerate(bars)),
    )

    print("\n截面 zscore：axis=1 先算每行均值，再用 axis=0 把列向量减回去")
    z = rev20.sub(rev20.mean(axis=1), axis=0).div(rev20.std(axis=1), axis=0)
    print("z 的每日均值最大值（应接近 0）：", f"{z.mean(axis=1).abs().max():.2e}")

    print("\ngroupby + transform（把组内均值填回每一行，形状不变）")
    vol_mean = df.groupby("symbol")["volume"].transform("mean")
    peek = df[["symbol", "volume"]].head(3).assign(sym_mean=vol_mean.head(3))
    print(peek.round(0).to_string(index=False))

    # ---------------------------------------------------------------- 存
    rule("5 · 存：同一份长表，parquet 与 csv 的差别")
    print("（长表 128 万行 × 8 列，CSV 写入要几秒，属正常）")
    for suffix in ("parquet", "csv"):
        p = Path(f"/tmp/pandas_basics_long.{suffix}")
        t0 = time.time()
        df.to_parquet(p) if suffix == "parquet" else df.to_csv(p, index=False)
        size = p.stat().st_size / 1e6
        print(f"{suffix:8s} 写 {time.time() - t0:5.2f}s  {size:6.1f}MB", end="  ")
        t0 = time.time()
        if suffix == "parquet":
            back = pd.read_parquet(p)
        else:
            back = pd.read_csv(p, parse_dates=["time"])
        print(f"读 {time.time() - t0:5.2f}s  time 列 dtype = {back.time.dtype}")
    print("→ 长表：parquet 小 3 倍、快一个数量级，且不用手动 parse_dates")
    print("→ 例外：少行多列的宽表（如 242×5333）CSV 反而更快，parquet 无优势")

    # -------------------------------------------------------------- 性能
    rule("6 · 性能：同一件事，换个写法差十几倍到几十倍")
    big = df[["open", "close", "volume"]].head(200_000)
    t0 = time.time()
    _ = [r.open * r.close for r in big.itertuples(index=False)]
    t_it = time.time() - t0
    t0 = time.time()
    _ = big.open * big.close
    t_vec = time.time() - t0
    print(
        f"itertuples {t_it * 1000:6.0f}ms | 向量化 {t_vec * 1000:5.1f}ms"
        f" | 快 {t_it / t_vec:.0f} 倍"
    )

    rule("完成：这 6 段跑通，pandas 的「入门」就结束了")


if __name__ == "__main__":
    main()
