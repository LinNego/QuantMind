#!/usr/bin/env python3
"""pandas 60 分钟：只为回测服务的 6 节课

写给「SQL 很熟、pandas 没入门」的人。
原则：**只学这 6 课，够你写完整个阶段 1~3。** pandas 剩下 90% 的功能你这辈子用不到。

用法（仓库根目录）：
    python research/lessons/pandas_drill.py

每节课的结构都是：演示（能跑出真数据） → 练习（改一行，猜结果，再跑）。
**不要只读，必须改。** 改坏了就 git checkout 回来。
"""

from __future__ import annotations

import os

import duckdb
import pandas as pd

pd.set_option("display.width", 170)
pd.set_option("display.max_columns", 20)
pd.set_option("display.unicode.east_asian_width", True)

DATA_DIR = os.getenv("QM_QUANTDB_DATA_DIR", "/opt/quantmind/data/quantdb")
DATA = f"{DATA_DIR}/1_kline_data/daily_forward/*/data.parquet"
STOCKS = ("600519.SH", "000001.SZ", "600036.SH", "002594.SZ", "601318.SH")


def load() -> pd.DataFrame:
    """取 5 只股票、约 40 个交易日的日线 —— 小到能一眼看完，又足够做所有练习。"""
    con = duckdb.connect()
    return con.execute(
        f"""
        SELECT dt, symbol, close, volume FROM read_parquet('{DATA}', hive_partitioning = true)
        WHERE dt BETWEEN 20241101 AND 20241231 AND symbol IN {STOCKS}
        ORDER BY symbol, dt
        """
    ).df()


def title(n: str, s: str) -> None:
    print(f"\n{'=' * 78}\n{n}  {s}\n{'=' * 78}")


# ===========================================================================
# 第 0 课：DataFrame 到底是什么
# ===========================================================================
def lesson0() -> None:
    title("第 0 课", "DataFrame = 一张表；Series = 表里的一列")
    df = pd.DataFrame(
        {
            "symbol": ["A", "B", "C"],
            "close": [10.0, 20.0, 30.0],
            "volume": [100, 200, 300],
        }
    )
    print(df)
    print("\ndf.shape  →", df.shape, "  ← (行数, 列数)")
    print("df.columns →", list(df.columns))
    print("df['close'] → 这是一个 Series（一列）：")
    print(df["close"])
    print("\n一句话：DataFrame 就是内存里的一张表，Series 就是这一列。")


# ===========================================================================
# 第 1 课：看数据 —— SELECT * LIMIT
# ===========================================================================
def lesson1(df: pd.DataFrame) -> None:
    title("第 1 课", "先看懂手上是什么：shape / head / dtypes / describe")
    print("形状:", df.shape, "（行, 列）")
    print("\n前 3 行（= SELECT * LIMIT 3）：")
    print(df.head(3).to_string(index=False))
    print("\n每列的类型（= DESC table）：")
    print(df.dtypes)
    print("\n数值列的分布（= 一次看 min/max/mean）：")
    # 这是在干嘛
    print(df.describe().round(2).to_string())
    print("\n【练习】把 df.head(3) 改成 df.tail(5)，猜结果再跑。")
    print(df.tail(5).to_string(index=False))


# ===========================================================================
# 第 2 课：WHERE 和 ORDER BY
# ===========================================================================
def lesson2(df: pd.DataFrame) -> None:
    title("第 2 课", "筛选与排序 = SQL 的 WHERE / ORDER BY")

    print("① 挑一列（= SELECT close）")
    print(df["close"].head(3).to_string(), "\n")

    print("② 挑多列（= SELECT symbol, close）—— 注意是两个方括号")
    print(df[["symbol", "close"]].head(3).to_string(index=False), "\n")

    print("③ 按条件筛行（= WHERE dt = 20241231）")
    day = df[df["dt"] == 20241231]
    print(day.to_string(index=False), "\n")

    print("④ 多条件（= WHERE dt=... AND close > 100）")
    print(df[(df["dt"] == 20241231) & (df["close"] > 100)].to_string(index=False), "\n")

    print("⑤ 排序（= ORDER BY close DESC）")
    print(day.sort_values("close", ascending=False).to_string(index=False))
    print("\n【练习】把 > 100 改成 > 30，看多出哪几只。")
    print(df[(df["dt"] == 20241231) & (df["close"] > 30)].to_string(index=False), "\n")

    print("\n⚠️ 最常见的坑：")
    print("   df['close']        → 一列（Series）")
    print("   df[['close']]      → 一张表（DataFrame，只有一个列）")
    print("   少一层方括号，后面 .sum() 的行为就不一样。")


# ===========================================================================
# 第 3 课：GROUP BY
# ===========================================================================
def lesson3(df: pd.DataFrame) -> None:
    title("第 3 课", "分组统计 = SQL 的 GROUP BY")

    print("① 每只股票的平均收盘（= GROUP BY symbol）")
    print(df.groupby("symbol")["close"].mean().round(2).to_string(), "\n")

    print("② 一次算多个指标（.agg）")
    g = df.groupby("symbol")["close"].agg(["min", "max", "mean", "count"])
    print(g.round(2).to_string(), "\n")

    print("③ 分组后还要横向算（transform：把组内结果放回每一行）")
    df2 = df.copy()
    df2["股票均价"] = df2.groupby("symbol")["close"].transform("mean")
    df2["相对均价"] = (df2["close"] / df2["股票均价"] - 1).round(4)
    print(df2.tail(4)[["symbol", "dt", "close", "股票均价", "相对均价"]].to_string(index=False))
    print("\n【练习】把 'mean' 换成 'std'，看哪只股票波动最大。")
    df3 = df.copy()
    df3["股票均价"] = df3.groupby("symbol")["close"].transform("std")
    df3["相对均价"] = (df3["close"] / df3["股票均价"] - 1).round(4)
    print(df2.tail(3)[["symbol", "dt", "close", "股票均价", "相对均价"]].to_string(index=False))


# ===========================================================================
# 第 4 课：pivot —— 长表变宽表（回测的第一步）
# ===========================================================================
def lesson4(df: pd.DataFrame) -> pd.DataFrame:
    title("第 4 课", "长表 → 宽表（pivot）：回测代码的第一行")
    print("长表（一行 = 一只股票的一天）：")
    print(df.head(4).to_string(index=False), "\n")

    wide = df.pivot(index="dt", columns="symbol", values="close").sort_index()
    print("宽表（行=日期，列=股票）—— 这就是回测里的 close：")
    print(wide.head(3).round(2).to_string())
    print("\n宽表的好处：同一天的所有股票在**同一行**，")
    print("所以「横向比较」（选股）= axis=1；「纵向比较」（时间）= axis=0。")
    print("这一步是 SQL 做不到的顺手事，也是 pandas 唯一不可替代的地方。")
    print("\n【练习】把 values='close' 改成 'volume'，看看成交量版长什么样。")
    wide2 = df.pivot(index="dt", columns="symbol", values="close").sort_index()
    print(wide2.head(3).round(2).to_string())

    return wide


# ===========================================================================
# 第 5 课：时间序列 —— shift / pct_change / rolling
# ===========================================================================
def lesson5(wide: pd.DataFrame) -> None:
    title("第 5 课", "时间错位 = LAG()；收益率 = 今天的价 / 昨天的价 - 1")

    print("① shift(1) = LAG(close, 1)（把昨天搬到今天）")
    cmp = pd.DataFrame({"今天": wide["600519.SH"], "昨天": wide["600519.SH"].shift(1)})
    print(cmp.head(4).round(2).to_string(), "\n")

    print("② pct_change() = 今天/昨天-1（一步算收益率）")
    ret = wide.pct_change(fill_method=None)
    print((ret * 100).head(4).round(2).to_string(), "\n")

    print("③ rolling(20).mean() = 20 日移动平均（SQL 里要写窗口函数）")
    ma20 = wide["600519.SH"].rolling(20).mean()
    print(pd.DataFrame({"收盘": wide["600519.SH"], "MA20": ma20}).tail(4).round(2).to_string(), "\n")

    print("④ shift(20) 用来算「20 日涨幅」，正是玩具回测里的因子")
    mom = wide / wide.shift(20) - 1
    print((mom * 100).tail(3).round(2).to_string())
    print("\n【练习】把 shift(20) 改成 shift(5)，看 5 日涨幅的分布怎么变。")
    mom2 = wide / wide.shift(5) - 1
    print((mom2 * 100).tail(3).round(2).to_string())


# ===========================================================================
# 第 6 课：横向排名 + 选股 + 净值（把回测拆成 5 行）
# ===========================================================================
def lesson6(wide: pd.DataFrame) -> None:
    title("第 6 课", "把前 5 课拼成一个回测：每一步都打印出来")

    # 3 只股票 × 5 天的小表：所有数字都能心算，不用信我
    close = pd.DataFrame(
        {
            "A": [10.0, 11.0, 12.0, 13.0, 14.0],
            "B": [20.0, 21.0, 20.0, 23.0, 26.0],
            "C": [30.0, 32.0, 35.0, 36.0, 34.0],
        },
        index=["周一", "周二", "周三", "周四", "周五"],
    )
    close.index.name = "日期"
    S = 1          # 用 2 天窗口，这样第三天就能第一次算出因子

    print("【第 0 步】收盘价（行=日期，列=股票）")
    print(close.to_string())
    print(f"   形状 {close.shape} = 5 个交易日 × 3 只股票\n")

    sh = close.shift(S)
    print(f"【第 1 步】close.shift({S}) —— 整张表往下推 {S} 格")
    print(sh.to_string())
    print(f"   形状 {sh.shape}，和原来一样：不删行，只是顶上补 {S} 行 NaN")
    print(f"   周三 A 那格 = {sh.loc['周三', 'A']:.0f}，其实是【周一】A 的价\n")

    mom = close / sh - 1
    print(f"【第 2 步】因子 mom = close / close.shift({S}) - 1")
    print((mom * 100).round(2).to_string())
    print(f"   周三 A = 12.0 / 10.0 - 1 = {(12.0 / 10.0 - 1) * 100:.2f}%  ← 两天涨了 20%\n")

    rank = mom.rank(axis=1, ascending=False)
    print("【第 3 步】rank(axis=1, ascending=False) —— 每天横着排名")
    print(rank.to_string())
    print("   周三这行：A=20.00% 第1，C=16.67% 第2，B=0.00% 第3\n")

    pick = rank <= 2
    print("【第 4 步】pick = rank <= 2 —— 取前 2 名")
    print(pick.to_string())
    print("   True = 今天买它。周三选 A、C；周五变成选 A、B（换仓了）\n")

    w = pick.astype(float)
    print("【第 5 步】astype(float) —— True/False → 1.0/0.0")
    print(w.to_string())
    print("   布尔值不能做除法，先转成数字\n")

    rowsum = w.sum(axis=1)
    print("【第 6 步】w.sum(axis=1) —— 每行加起来 = 今天选了几只")
    print(rowsum.to_string())
    print("   周三 = 2.0，表示选了 2 只\n")

    w = w.div(rowsum.replace(0, float("nan")), axis=0).fillna(0.0)
    print("【第 7 步】w.div(每行的和, axis=0) —— 把 1.0 摊成 1/N")
    print(w.round(2).to_string())
    print("   周三：A=1/2=0.5，C=1/2=0.5，B=0/2=0")
    print("   验算每行加起来 =", w.sum(axis=1).round(2).tolist(), "（都该是 1.0）")
    print("   ⚠️ 漏了这步，权重和 = 2.0，等于偷偷用了 2 倍杠杆\n")

    ret = close.pct_change(fill_method=None)
    print("【第 8 步】ret = close.pct_change() —— 每只股票当天的涨跌")
    print((ret * 100).round(2).to_string())
    print("   周四 B = 23/20 - 1 = +15%")
    print("   周一整行是 NaN：没有'昨天'\n")

    w_prev = w.shift(1)
    print("【第 9 步】w.shift(1) —— 仓位往下推一格")
    print(w_prev.round(2).to_string())
    print("   周四这行拿到的是【周三】的仓位；周五拿到【周四】的")
    print("   含义：昨天做的决定，今天才生效\n")

    detail = w_prev * ret
    print("【第 10 步】w_prev * ret —— 每只股票贡献多少收益")
    print((detail * 100).round(2).to_string())
    print("   周四这行：A 0.5×8.33%=4.17%，C 0.5×2.86%=1.43%")
    print("   注意周五持有的仍是【周四选的 A、C】，不是周五才选的 A、B\n")

    port = detail.sum(axis=1)
    print("【第 11 步】.sum(axis=1) —— 横着加 = 组合当天赚了多少")
    print((port * 100).round(2).to_string())
    print(f"   周四 = 4.17% + 1.43% = {(port.loc['周四']) * 100:.2f}%\n")

    nav = (1 + port.fillna(0.0)).cumprod()
    print("【第 12 步】nav = (1 + port).cumprod() —— 连乘成净值")
    print(nav.round(4).to_string())
    print("   周四：1.0000 × (1 + 5.60%) = 1.0560")
    print("   周五：1.0560 × (1 + 1.07%) = 1.0672")
    print("   是【乘】不是【加】：第二天赚的钱建在第一天赚的钱之上\n")
    print(f"【结果】5 天总收益 = {(nav.iloc[-1] - 1) * 100:+.2f}%")

    # ---- 反面对照 ----
    port_bad = (w * ret).sum(axis=1)
    nav_bad = (1 + port_bad.fillna(0.0)).cumprod()
    print("\n【对照】把 w.shift(1) 写成 w（漏掉 shift）：")
    cmp = pd.DataFrame(
        {
            "✅ 有 shift(1)": (port * 100).round(2),
            "❌ 无 shift": (port_bad * 100).round(2),
        }
    )
    print(cmp.to_string())
    print(f"   ✅ 正确版 5 天总收益：{(nav.iloc[-1] - 1) * 100:+.2f}%")
    print(f"   ❌ 作弊版 5 天总收益：{(nav_bad.iloc[-1] - 1) * 100:+.2f}%")
    print("   周五差得最多：作弊版用【周五收盘才选出来】的 A、B，")
    print("   去吃【周五白天】的涨跌。这在现实中不可能做到。\n")

    print("【练习】")
    print("  1. S = 2 改成 S = 1，因子变成'1 日涨幅'，看名次怎么变")
    print("  2. rank <= 2 改成 rank <= 1（只买一只）")
    print("  3. 把 w.shift(1) 改回 w，自己算一遍周五那行的差")


def main() -> None:
    df = load()
    lesson0()
    lesson1(df)
    lesson2(df)
    lesson3(df)
    wide = lesson4(df)
    lesson5(wide)
    lesson6(wide)
    print(f"\n{'=' * 78}")
    print("跑完了。这 6 课覆盖了回测代码里 100% 的 pandas 语法。")
    print("看不懂哪一节，就把那一节单独再跑一遍，改数字玩。")
    print("=" * 78)


if __name__ == "__main__":
    main()
