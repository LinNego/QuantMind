# D01 · 看见数据

> 阶段 0 · 数据与环境 ｜ 第 1 天 ｜ 状态：✅ 已完成

## 今天要干嘛

把 parquet 读成一张表，打印出前 5 行真实行情，并且**能指着每一列说出它是什么**。

## 要看懂什么

- [数据获取与迁移](../../参考/数据获取与迁移.md) 第 3 节（L0/L1/L2 三档），知道自己在用哪档

## 具体做法

### 1. 先进 REPL

```bash
qd          # 等于 duckdb -init research/qdb.sql
```

```sql
SELECT symbol, dt, open, high, low, close, volume, amount
FROM kf WHERE dt = 20261023 LIMIT 5;
```

### 2. 再改数字玩

把日期、股票、列各换一次，直到**不靠抄也能写出来**。改着玩用 `practice.py`：

```bash
python research/stage0-数据与环境/day01-看见数据/practice.py
```

改它开头的 `DATE` / `SYMBOL` 两个变量即可。

## 完成判据

终端里打出 5 行真实股票数据，你**能指着每一列说出它是什么**。

## 当天总结

数据不在数据库里，是 **10.83 GB / 67,959 个 parquet 文件**躺在 `/opt/quantmind/data/quantdb`。
DuckDB 直接读文件，不需要导入。

`research/qdb.sql` 已建好 23 个视图 + 短别名：

| 别名 | 是什么 |
|---|---|
| `ku` / `kf` / `kb` | 不复权 / 前复权 / 后复权日线 |
| `feat` | `features_daily`（79 列） |
| `val` | `valuation` |
| `idx` | 指数 |

**卡住怎么办**：报 `No files found` → 先 `ls /opt/quantmind/data/quantdb/1_kline_data/daily_unadjusted | tail` 看最新是哪天。

## 本目录文件

- [code.py](code.py)
- [output.md](output.md)
- [practice.py](practice.py)

---

[← 返回阶段总览](../../README.md) ｜ [下一天 D02 →](../day02-规模与覆盖/README.md)
