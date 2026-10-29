#!/usr/bin/env python3
"""pandas 60 分钟：只为回测服务的 6 节课

写给「SQL 很熟、pandas 没入门」的人。
原则：**只学这 6 课，够你写完整个阶段 1~3。** pandas 剩下 90% 的功能你这辈子用不到。

用法（仓库根目录）：
    python research/01-week1/day4/pandas_drill.py

每节课的结构都是：演示（能跑出真数据） → 练习（改一行，猜结果，再跑）。
**不要只读，必须改。** 改坏了就 git checkout 回来。
"""

from __future__ import annotations

import duckdb
import pandas as pd

pd.set_option("display.width", 170)
pd.set_option("display.max_columns", 20)
pd.set_option("display.unicode.east_asian_width", True)

DATA = "/opt/quantmind/data/quantdb/1_kline_data/daily_forward/*/data.parquet"
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
    print(df.describe().round(2).to_string())
    print("\n【练习】把 df.head(3) 改成 df.tail(5)，猜结果再跑。")


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


# ===========================================================================
# 第 6 课：横向排名 + 选股 + 净值（把回测拆成 5 行）
# ===========================================================================
def lesson6(wide: pd.DataFrame) -> None:
    title("第 6 课", "axis=1：每天横向排名 → 选股 → 净值")

    mom = wide / wide.shift(20) - 1
    rank = mom.rank(axis=1, ascending=False)          # 每天横着排名，1 = 涨得最多
    print("① 每天的名次（axis=1 = 横着算）：")
    print(rank.tail(3).to_string(), "\n")

    pick = rank <= 2                                   # 每天取前 2 名
    print("② 选中谁（True/False 矩阵）：")
    print(pick.tail(3).to_string(), "\n")

    w = pick.astype(float)
    w = w.div(w.sum(axis=1).replace(0, float("nan")), axis=0).fillna(0.0)
    print("③ 权重（选中的平分，每只 0.5）：")
    print(w.tail(3).round(2).to_string(), "\n")

    ret = wide.pct_change(fill_method=None).fillna(0.0)
    port = (w.shift(1) * ret).sum(axis=1)              # 昨天的仓位 × 今天的收益
    nav = (1 + port).cumprod()

    print("④ 组合日收益 —— 注意前面一段全是 0：")
    print((port * 100).head(5).round(3).to_string())
    print("   前 20 天收益为 0 不是 bug：")
    print("   20 日因子需要 20 天历史，前 20 天算不出名次 → 没有持仓 → 收益 0。")
    print("   这叫「热启动期」，真实回测里要把它剔掉再算年化。\n")
    print("   热启动之后长这样（第 21~25 个交易日）：")
    print((port * 100).iloc[20:25].round(3).to_string(), "\n")
    print("⑤ 净值（连乘）：")
    print(nav.tail(3).round(4).to_string())
    print(f"\n这段区间的总收益：{(nav.iloc[-1] - 1) * 100:.2f}%")
    print("\n【练习】把 rank <= 2 改成 <= 1（只买一只），看净值怎么变。")


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
