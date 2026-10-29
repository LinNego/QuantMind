# pandas 速查（给 SQL 老手）

> 只收「回测真的会用到的」。看不懂就先跑 [pandas_drill.py](pandas_drill.py)。
> 假设 `df` 是长表（`dt, symbol, close`），`wide` 是宽表（行=日期，列=股票）。

## 1. 看数据

| 想干什么 | SQL | pandas |
|---|---|---|
| 前几行 | `LIMIT 5` | `df.head(5)` |
| 形状 | — | `df.shape` → (行, 列) |
| 列名 | `DESC t` | `df.columns` / `df.dtypes` |
| 分布 | `MIN/MAX/AVG` | `df.describe()` |
| 去重 | `DISTINCT` | `df["symbol"].unique()` / `df.drop_duplicates()` |

## 2. 选数据

| 想干什么 | SQL | pandas |
|---|---|---|
| 选一列 | `SELECT close` | `df["close"]` ← **一个**方括号，得到 Series |
| 选多列 | `SELECT symbol, close` | `df[["symbol", "close"]]` ← **两个**方括号，得到 DataFrame |
| 按行号 | `LIMIT/OFFSET` | `df.iloc[0:5]` |
| 按标签 | — | `df.loc["行标签", "列名"]` |
| 条件筛 | `WHERE close > 100` | `df[df["close"] > 100]` |
| 多条件 | `WHERE a AND b` | `df[(df["a"]) & (df["b"])]` ⚠️ 每个条件都要括号 |
| 或条件 | `WHERE a OR b` | `df[(df["a"]) \| (df["b"])]` |

## 3. 排序 / 排名

| 想干什么 | SQL | pandas |
|---|---|---|
| 排序 | `ORDER BY close DESC` | `df.sort_values("close", ascending=False)` |
| 多列排序 | `ORDER BY a, b` | `df.sort_values(["a", "b"])` |
| 排名（每天横向） | `RANK() OVER (PARTITION BY dt)` | `df.rank(axis=1, ascending=False)` |
| 排名（每只股票纵向） | `RANK() OVER (PARTITION BY symbol)` | `df.rank()` 或 `groupby(...).rank()` |

**`axis` 的记法**：
- `axis=0` = 竖着算，**跨时间**（同一只股票的不同日期）
- `axis=1` = 横着算，**跨股票**（同一天的不同股票）← 选股永远用这个

## 4. 分组

| 想干什么 | SQL | pandas |
|---|---|---|
| 分组求均值 | `GROUP BY symbol` + `AVG(close)` | `df.groupby("symbol")["close"].mean()` |
| 多个指标 | `MIN, MAX, COUNT` | `.agg(["min", "max", "count"])` |
| 组内结果放回每行 | `AVG(...) OVER (PARTITION BY ...)` | `df.groupby("symbol")["close"].transform("mean")` |

`transform` vs `agg`：**`agg` 每组一行；`transform` 保持原行数**（能直接拼回原表）。

## 5. 长表 ↔ 宽表（回测的核心操作）

| 想干什么 | pandas |
|---|---|
| 长 → 宽 | `wide = df.pivot(index="dt", columns="symbol", values="close")` |
| 宽 → 长 | `wide.stack().reset_index()` |

SQL 没有真正等价的写法（要写一堆 `CASE WHEN`），这是 pandas 唯一不可替代的地方。

## 6. 时间序列（对应窗口函数）

| 想干什么 | SQL | pandas |
|---|---|---|
| 上一行 | `LAG(close, 1)` | `s.shift(1)` |
| 20 天前 | `LAG(close, 20)` | `s.shift(20)` |
| 收益率 | `close / LAG(close) - 1` | `s.pct_change(fill_method=None)` |
| 20 日涨幅 | — | `s / s.shift(20) - 1` |
| 移动平均 | `AVG(...) OVER (ROWS 20 PRECEDING)` | `s.rolling(20).mean()` |
| 移动标准差 | 同上 `STDDEV` | `s.rolling(20).std()` |
| 累计连乘（净值） | `EXP(SUM(LN(1+x)))` | `(1 + ret).cumprod()` |
| 历史最高 | `MAX(...) OVER (ROWS UNBOUNDED PRECEDING)` | `s.cummax()` |

## 7. 四则运算与对齐

| 想干什么 | pandas |
|---|---|
| 逐元素加 | `a + b`（**按索引自动对齐**，不是按位置） |
| 表 × 列 | `df.mul(s, axis=0)` ← 每行乘 |
| 表 ÷ 行和 | `df.div(df.sum(axis=1), axis=0)` |
| 行求和 | `df.sum(axis=1)` |
| 保留条件、其余填 NaN | `df.where(mask)` |
| 用上一行填 NaN | `df.ffill()` |
| NaN 填 0 | `df.fillna(0)` |
| 转浮点 | `df.astype(float)` |

**自动对齐是 pandas 最大的优点也是最大的坑**：两张表的行/列顺序不同也能算对，但**索引不一致时会静默产生 NaN**。所以永远先 `sort_index()`。

## 8. 三个必踩的坑

```python
# 坑 1：链式赋值，改了不生效（SettingWithCopyWarning）
df[df.close > 100]["flag"] = 1        # ❌ 改的是临时副本
df.loc[df.close > 100, "flag"] = 1    # ✅

# 坑 2：把 Series 当 DataFrame 用
df["close"].sum()      # ✅ 一列求和
df[["close"]].sum()    # ✅ 一张表求和 → 返回 Series（带列名）

# 坑 3：fill_method 默认会"补"缺失值（新版会报警告）
s.pct_change()                  # ⚠️ 旧默认会把 NaN 前向填充
s.pct_change(fill_method=None)  # ✅ 明确不填
```
