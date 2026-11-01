# D02 · 规模与覆盖

> 阶段 0 · 数据与环境 ｜ 第 2 天 ｜ 状态：✅ 已完成

## 今天要干嘛

说清「多少只股票、多少交易日、时间范围、总行数」。

## 要看懂什么

- [quantdb_datasets.py](../../../backend/shared/quantdb_datasets.py) 里的 `DatasetSpec` 列表（29 个），只看名字和备注

## 具体做法

一条 SQL 全拿到：

```sql
SELECT count(DISTINCT symbol) AS 股票数, count(DISTINCT dt) AS 交易日数,
       min(dt) AS 起, max(dt) AS 止, count(*) AS 总行数
FROM kf;
```

再翻一遍全部 23 张表的结构：

```bash
python research/stage0-数据与环境/day02-规模与覆盖/inspect_tables.py
```

## 完成判据

能不看屏幕说出「我的数据是 X 只股票、Y 个交易日、从 A 到 B」。

## 当天总结

| 项 | 实测 |
|---|---|
| 股票数 | **5,581**（含北交所 353） |
| 交易日 | **2,623** |
| 范围 | **2016-01-04 ～ 2026-10-23** |
| 总行数 | **10,826,172** |
| 体积 | 10.83 GB / 67,959 个 parquet |

⚠️ 注意三张主表覆盖不一样：`daily_forward` 5,581 只、`valuation` 5,575 只、
**`features_daily` 只有 5,228 只（北交所 353 只全缺）**。join 时它们会静默消失。

完整数据字典：[参考/表结构.md](../../参考/表结构.md)。

## 本目录文件

- [code.py](code.py)
- [inspect_tables.py](inspect_tables.py)
- [output.md](output.md)

---

[← 返回阶段总览](../../README.md) ｜ [上一天 D01 →](../day01-看见数据/README.md) ｜ [下一天 D03 →](../day03-每天股票数/README.md)
