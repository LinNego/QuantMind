# 09 · pandas 入门：够用就停

> 核心结论：**pandas 只有两个坐标系（行 = 日期，列 = 股票）+ 一个隐式行为（按 index 对齐）。**
> 想清楚「这次是按列算还是按行算」，再想清楚「对齐会不会悄悄多出 NaN」，你就能用它做完 90% 的活。
> API 不用背，用到再查。

本文所有数字都是在本机 `/root/quant/.venv`（pandas 3.0.6）+ `/opt/quantmind/data/quantdb` 上实跑出来的。

## 先看这里：仓库里已经有四份材料，别重复读

| 材料 | 形态 | 用途 | 什么时候用 |
|---|---|---|---|
| [pandas_drill.py](../../../research/lessons/pandas_drill.py) | 6 节课，可运行 | **动手入门**：5 只股票 × 40 天，讲完就能写回测 | 第一次学，60 分钟 |
| [pandas_cheatsheet.md](../../../research/lessons/pandas_cheatsheet.md) | 速查表 | **SQL → pandas 对照**，忘记语法时查 | 写代码时放旁边 |
| 本文（09） | 概念 + 实测 | **补 drill 没讲的三件事**：pandas 3 与老教程的差异、index 对齐的静默坑、全市场规模的真实数字 | 跑完 drill 再读，30 分钟 |
| [research/lessons/pandas_basics.py](../../../research/lessons/pandas_basics.py) | 一个脚本 | 全市场 128 万行真实数据走一遍「读→查→变形→算→存」 | 想确认自己会用，十几秒 |

**别按顺序全读一遍。** 入门只需要：`drill` 跑一遍 → 本文第 0、4 节 → 写你自己的 `research/01_load_data.py`。

---

## 0. 先花 30 秒确认环境

```bash
cd /root/QuantMind
/root/quant/.venv/bin/python -c "import pandas; print(pandas.__version__)"
# 3.0.6
```

- 必须用仓库的 venv。系统自带的 `python3` 里是 pandas 2.2.3，会报 `_ARRAY_API not found`（[07](../2-阶段0/07-阶段0-每日执行计划.md) 第 1 天提过）。
- 本机是 **pandas 3.0**，而网上 90% 的教程写的是 2.x / 1.x。下面四条差异会让「照抄」直接报错，或者更糟——**静默不生效**：

| 变化 | 老教程写法 | 本机实测结果 |
|---|---|---|
| Copy-on-Write 常开 | `df[df.a>1]["b"] = 0` | 抛 `ChainedAssignmentError` 警告，**且原表不变** |
| 字符串默认 `str` dtype | 假设是 `object` | `df.symbol.dtype` → `str` |
| 频率别名收紧 | `resample("M")` | `ValueError: 'M' is no longer supported ... use 'ME'` |
| 整数列塞 `NaN` | 以为会自动变 nullable | 仍然升成 `float64`，`1` 变 `1.0`（想要 `Int64` 得显式指定） |

正确写法只有一种，一步定位行列：

```python
df = pd.DataFrame({"a": [1, 2, 3], "b": [10, 20, 30]})
df.loc[df.a > 1, "b"] = 0        # CoW 下也生效：b 变成 [10, 0, 0]
```

---

## 1. 心智模型：一张表 + 两个轴

DataFrame = 带标签的二维数组；Series = 带标签的一维数组；Index = 那排标签。就这三样东西。

```
             600036.SH  000001.SZ  300750.SZ     ← axis=1（列 / 股票 / 截面方向）
2024-01-02      31.55       9.80      168.0
2024-01-03      32.00       9.85      171.2
      ↑
   axis=0（行 / 日期 / 时序方向）
```

背这一句就够用一辈子：**`axis=1` 横着切（同一天所有股票），`axis=0` 竖着切（同一只股票所有天）。**

```python
# 假设 px 已经是「日期 × 股票」的收盘价宽表
px.rank(axis=1, pct=True)      # 横向 → 截面：今天 5000 只里排第几
px.rolling(20).std()           # 纵向（默认 axis=0）→ 时序：这只股票自己过去 20 天的波动
```

这正是 [02-核心概念速通.md](../3-方法与陷阱/02-核心概念速通.md) 第三节那张「截面 vs 时序」图的代码版。**截面不是机器学习的概念，是数据形状的概念**——按当天排名就是截面。

---

## 2. 只学这 8 个，覆盖 95% 的日常

> 带 SQL 对照的完整速查表在 [pandas_cheatsheet.md](../../../research/lessons/pandas_cheatsheet.md)，这里只给「什么时候用哪个」。

| 动作 | API | 一句话 |
|---|---|---|
| 读 | `pd.read_parquet(path, columns=..., filters=...)` | 只读要用的列和日期区间，在盘上就裁掉 |
| 看 | `.shape` `.dtypes` `.isna().mean()` `.describe()` | 先体检，再画图 |
| 选 | `.loc[标签, 标签]` / `.iloc[位置, 位置]` / `df[布尔]` | 有标签用 loc，有位置用 iloc |
| 加列 | `.assign(col=表达式)` | 返回新表，不改原表 |
| 分组 | `.groupby(k)[col].agg(...)` / `.transform(...)` | agg 形状变小，transform 形状不变 |
| 变形 | `.pivot(...)` / `.melt(...)` | 长表 ↔ 宽表 |
| 合并 | `.merge(...)` / `.reindex(...)` | 按 key / 按标签对齐，**不是按行号** |
| 时序 | `.shift(n)` / `.rolling(w)` / `.resample("ME")` / `.pct_change()` | 未来函数就藏在 `shift(-n)` 里 |

`apply`、`MultiIndex`、`pivot_table` 这些等你真需要时再查——**pandas 文档的搜索框比背 API 有用**。

---

## 3. 拿真实数据走一遍（脚本 6 段，12 秒跑完）

```bash
/root/quant/.venv/bin/python research/lessons/pandas_basics.py
```

它做六件事（整个跑完十几秒），每件都给一个「该看什么」：

| 段 | 做什么 | 输出 | 你要能说出 |
|---|---|---|---|
| 1 读 | `read_parquet` + `filters` + `columns` | 1,277,076 行 × 8 列，242 天，5333 只，93MB | 一年全市场日线才 1.3M 行，**放得进内存** |
| 2 查 | 缺失率 / 脏数据 / 单位 | 缺失全 0；`volume<=0` 也是 0；`close*volume/amount` 中位数 **9994** | 停牌是**整行缺失**，不是 NaN；`amount` 单位是**万元** |
| 3 变形 | `pivot` → `rank(axis=1)` → `melt` | 长表 (1277076, 8) → 宽表 (242, 5333)，0.33 秒 | 宽表是「日期 × 股票」，**列数 = 股票数** |
| 4 算 | 20 日反转 + 未来 5 日 + IC | IC 0.0868｜ICIR 0.433｜样本 **217** 天 | 242 → 217：窗口吃掉首尾 |
| 5 存 | parquet vs csv | parquet 27.6MB / 读 0.18s；csv 78.2MB / 读 3.5s | 长表用 parquet |
| 6 性能 | `itertuples` vs 向量化 | 245ms vs 8.7ms | 别写逐行循环 |

### 3.1 第一段就藏着两个坑

```python
df = pd.read_parquet(
    "/opt/quantmind/data/quantdb/1_kline_data/daily_unadjusted",   # 直接喂分区目录
    filters=[("dt", ">=", 20240101), ("dt", "<=", 20241231)],      # 按分区裁剪
    columns=["symbol", "time", "open", "high", "low", "close", "volume", "amount"],
)                                                                  # 只读需要的列
```

- 那个目录是 `dt=20240102/data.parquet` 这种 **hive 分区**。`pd.read_parquet` 能直接吃目录，但**不能吃通配符**（`"**/*.parquet"` 会 `FileNotFoundError`，那时用 DuckDB）。
- `filters` 落在分区列 `dt` 上才会真的跳过文件；`columns` 才是列裁剪。两个都写，读盘从 13 秒降到 0.8 秒。
- `symbol` 的 dtype 是 `str` 而不是 `object`——pandas 3 的默认值，省内存，但有些老代码 `assert df.dtypes == object` 会挂。

### 3.2 体检：先看「有没有」，再看「对不对」

```python
df.isna().mean().sort_values(ascending=False).head(3)   # 缺失率
(df.close <= 0).sum(), (df.high < df.low).sum(), (df.volume <= 0).sum()   # 脏数据
```

本机这份数据三个都是 **0**。这**不代表数据干净**，而是说明：**停牌那天压根没有这一行**。所以在宽表里停牌表现为 `NaN`（该行该列不存在），而不是 `0`。

由此引出第一条红线：**看到 `NaN` 先问它是「缺」还是「本来就没有」**。`ffill()` 把停牌日填成「昨天收盘」，等于告诉模型「这天收益为 0」，是假的平静。

单位也能用一个数字反推：

```python
(df.close * df.volume / df.amount).median()   # 9994
```

≈ 10000 → `amount` 的单位是**万元**。这就是 [08](../1-开始/08-全程每日计划.md) 里 D07 那道题，一行代码出答案。**任何一列，先确认单位再用。**

### 3.3 变形：长表 → 宽表，再变回来

```python
px = df.pivot(index="time", columns="symbol", values="close").sort_index()  # (242, 5333)
back = px.reset_index().melt(id_vars="time", var_name="symbol", value_name="close")
```

- 长表 (127万 × 8) 适合**存储和增量**；宽表 (242 × 5333) 适合**截面运算**（`rank(axis=1)`、`corrwith(axis=1)`）。
- `pivot` 要求 `(time, symbol)` **唯一**；有重复行会报错，那时才用 `pivot_table(aggfunc="first")`。
- 宽表列数 = 股票数。5000 只 × 2500 天 = 1250 万个 `float64` ≈ 100MB；全历史（2623 天 × 5581 只）也就 **117MB**。日频研究的全部价格面板，一台笔记本的内存绰绰有余。

### 3.4 算：窗口吃掉首尾，这是数学，不是 bug

```python
rev20 = -(px / px.shift(20) - 1)     # 因子：过去 20 日跌得多的
fwd5 = px.shift(-5) / px - 1         # 标签：未来 5 日涨跌（往后看）
ic = rev20.corrwith(fwd5, axis=1, method="spearman").dropna()
# IC 均值 0.0868｜ICIR 0.433｜样本 217 天（不是 242）
```

`shift(20)` 让前 20 天没有值，`shift(-5)` 让最后 5 天没有值，`dropna()` 后剩 217 天。**看到「样本数比预期少」先数窗口，别急着怀疑数据。**

单年 IC 0.0868 偏高。同一因子跑 2016–2026 全样本（2623 天）是 **IC 0.0567、ICIR 0.387**，正好落在 [02](../3-方法与陷阱/02-核心概念速通.md) 说的「日频 A 股 0.03~0.06」量级。**这就是为什么不能拿单年数字下结论**——见 [03](../3-方法与陷阱/03-常见陷阱与自检红线.md) 的自检红线。

### 3.5 存：长表用 parquet，宽表随意

| 对象 | parquet | csv | 结论 |
|---|---|---|---|
| 长表 128万×8：写 | 27.6MB / 0.6~1.0s | 78.2MB / 7~13s | parquet 小 3 倍、写快 10 倍 |
| 长表 128万×8：读 | 0.18s | 2~3.5s | parquet 快一个数量级 |
| 宽表 242×5333：读 3 列 | 215ms | 76ms | **csv 反而更快** |

反直觉但合理：parquet 是列存，列一多（5333 列）元数据开销就上来了；而行少列多的矩阵，CSV 逐行解析很快。**你的真实工作对象是长表，所以默认 parquet。**

还有一条：CSV 读回来 `time` 是 `str`（要手动 `parse_dates`），parquet 读回来还是 `datetime64[ns]`。**少一步手工转换，就少一个出错点。**

---

## 4. 对齐：pandas 唯一会让你「静默出错」的地方

两个 Series 相加，**不是按位置，是按 index**：

```python
x = pd.Series([1, 2, 3], index=["a", "b", "c"])
y = pd.Series([1, 1],    index=["a", "c"])
x + y            # [2.0, NaN, 4.0]   ← b 对不上，直接 NaN，不报错
```

这是 pandas 最「聪明」也最危险的设计。在量化里它有三种长相：

| 场景 | 现象 | 正确做法 |
|---|---|---|
| 两只股票日期不一样（停牌） | 相减/相除后多出一批 NaN | 先看 `isna().mean()`，必要时 `dropna(how="all")` |
| 财报数据按季度、行情按日 | merge 后行数莫名变多/变少 | 用 `merge_asof` 按「最近一期」对齐 |
| 手动造了个 DataFrame 忘了设 index | `df["x"] - series` 全 NaN | 先 `set_index` 或用 `.to_numpy()` |

**判据：任何一次「两个对象做运算」，都要问一句「它们的 index 一样吗」。** 不确定就先 `print(a.index.equals(b.index))`。

---

## 5. 时间序列三件套

```python
px.pct_change()                  # 日收益，行方向
px.rolling(20).std()             # 20 日波动率，纵向
px.shift(-5) / px - 1            # 未来 5 日收益 = 标签
px.resample("ME").last()         # 按月末重采样（pandas 3 里 "M" 已废弃）
```

**`shift(-n)` 出现在哪里，哪里就是标签，不是特征。** 把这行删掉重跑，IC 会变得「好得离谱」——那就是未来函数。

`rolling` 有个隐藏参数 `min_periods`，默认等于窗口长度，所以前 19 天是 `NaN`；设成 `min_periods=1` 会让开头几天用不足 20 天的数据算波动率，**看着连续了，其实是垃圾**。别为了「不留空」而改它。

---

## 6. groupby：agg 还是 transform

```python
df.groupby("symbol")["volume"].mean()              # 每只股票一行 → 形状变小
df.groupby("symbol")["volume"].transform("mean")   # 每行都带上本股均值 → 形状不变
```

判据只有一条：**结果要不要跟原表逐行对齐**。要，就 `transform`；不要，就 `agg`。

截面标准化（zscore）属于「不要 groupby、但要按行算」，用广播：

```python
z = rev20.sub(rev20.mean(axis=1), axis=0).div(rev20.std(axis=1), axis=0)
# 每天横向求均值 → 得到一个列向量 → 用 axis=0 把它减回每一行
```

`axis=0` 那个参数的意思不是「按行算」，而是「**让 Series 的 index 去匹配列标签**」。这是整个 pandas 里最反直觉的一个参数，记不住就记住：**减一行均值，用 `axis=0`。**

---

## 7. 性能：三档，差十几到几十倍

| 写法 | 200k 行实测 | 什么时候用 |
|---|---|---|
| 向量化 `df.a * df.b` | 2~9ms | 默认，永远优先 |
| `itertuples` | 140~245ms | 真的需要分支逻辑、且只跑一次 |
| `iterrows` | 更慢 | 基本别用 |

```python
big = df[["open", "close"]].head(200_000)
[r.open * r.close for r in big.itertuples(index=False)]   # 245ms
big.open * big.close                                      # 8.7ms
```

其余三条性价比最高的优化：

1. **列裁剪**：`read_parquet(columns=[...])`，读 3 列比读 8 列快一倍。
2. **`astype("category")`**：股票代码列从 `str` 转 `category`，内存 42MB → 23MB（省 45%）。
3. **分批**：真遇到内存不够，按年读、算完只留结果，别一次 `concat` 全历史。

---

## 8. 十条自检红线（贴屏幕上）

- [ ] `symbol` 是 `str`/`category`，不是 `object`，且格式统一（`600036.SH` 还是 `SH600036`？）
- [ ] `time` 是 `datetime64`，不是字符串（字符串排序会把 `2024-1-9` 排到 `2024-1-10` 后面）
- [ ] 每列单位确认过（`amount` 是万元、`volume` 是股还是手）
- [ ] 停牌是「行缺失」还是「NaN」搞清楚了，`ffill()` 用得有理由
- [ ] 任何两个对象运算前，确认过 index 一致
- [ ] `shift(-n)` 只出现在标签里
- [ ] 窗口函数吃掉的首尾行数，能和样本数对上
- [ ] 分组用的是 `agg` 还是 `transform`，形状对不对
- [ ] 中间结果存 parquet，不存 csv（省一次 `parse_dates` 的心智负担）
- [ ] 没有一行 `iterrows`

---

## 9. 下一步

1. 先把 [pandas_drill.py](../../../research/lessons/pandas_drill.py) 跑一遍，六节课每节都改一行数字再跑——**改坏了才叫学会**。
2. 再跑 `python research/lessons/pandas_basics.py`，把六段输出逐条解释给自己听——**说不出来的那句，就是你还没懂的那句**。
3. 打开 [research/tools/repl.py](../../../research/tools/repl.py)，用 `q("SELECT ...")` 交互式查真实数据，比看教程有用。
4. 然后把 [08](../1-开始/08-全程每日计划.md) 的 **D01–D05** 做了：那些题全部只用本文这 8 个 API。
4. 想去重/去极值/中性化，接着看 [03-常见陷阱与自检红线.md](../3-方法与陷阱/03-常见陷阱与自检红线.md) 和 [02-核心概念速通.md](../3-方法与陷阱/02-核心概念速通.md) 第 7 节的 20 行骨架。

> 最后一句：**本文覆盖了 pandas 不到 5% 的 API，但覆盖了你未来一年 95% 的操作。** 剩下的部分，等你真的写出「第三个类似的循环」时再去查——那时你才知道该搜什么。
