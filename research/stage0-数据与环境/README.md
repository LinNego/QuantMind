# 阶段 0 · 数据与环境（D01–D12）

> **目标不是「跑起来」，是能说清每一列是什么、什么时候可用、哪里是脏的。**
> 周期：2 周（12 个学习日） ｜ 过关：**Gate 1**

## 为什么阶段 0 不许交给 AI

阶段 1 起代码全部由 AI 写，但这一阶段**必须你自己动手**。原因很实在：

后面你要验收的所有东西都是数据层面的——负 PE、复权口径、停牌、幸存者偏差。
**不亲手摸一遍，你永远只能「看起来对」。**

## 12 天一览

| 天 | 今日目标 | 状态 |
|---|---|---|
| [D01 看见数据](day01-看见数据/README.md) | 把 parquet 读成一张表，说出每列是什么 | ✅ |
| [D02 规模与覆盖](day02-规模与覆盖/README.md) | 说清「X 只、Y 个交易日、A 到 B」 | ✅ |
| [D03 每天股票数](day03-每天股票数/README.md) | 看清股票数是逐年增长的，不是恒定 5000 | ✅ |
| [D04 脏数据](day04-脏数据/README.md) | 找出「不可能存在」的行 | ✅ |
| [D05 涨跌幅分档](day05-涨跌幅分档/README.md) | 按板块分档查超档，发现新股首日污染 | ✅ |
| [D06 缺失率与起始日](day06-缺失率与起始日/README.md) | 知道 `pe_ttm` 从 2017-01-16 才有 | ✅ |
| [D07 单位与量纲](day07-单位与量纲/README.md) | 证明 volume 是股、amount 是万元 | ✅ |
| [D08 复权三兄弟](day08-复权三兄弟/README.md) | 说清前复权/后复权/不复权各用在哪 | ✅ |
| [D09 幸存者偏差](day09-幸存者偏差/README.md) | 算每年消失的股票数 → 得到 0 只 | ✅ |
| [D10 两个暗坑](day10-两个暗坑/README.md) | 禁 `SELECT *`、字符串列必须 CAST | ✅ |
| [D11 整合脚本](day11-整合脚本/README.md) | 听懂参考实现，能自己跑出体检报告 | ⬜ **下一步** |
| [D12 Gate 1](day12-Gate1/README.md) | 三问写文字 + 填实验日志第一条 | ⚠️ AI 代写 |

## Gate 1（三问，不许翻资料）

1. `daily_forward` / `daily_backward` / `daily_unadjusted` 分别用在哪？
2. 你的数据有多少只股票、多少交易日、哪一年缺得最厉害？
3. 举出 3 个你发现的数据异常，说明它会不会影响回测。

> ⚠️ **状态：D12 的三问是 AI 代写的，不算考过。**
> 真正的验收考在阶段 1 的 D19——那时你要盯 AI 写的回测代码，指出哪一行保证时序。

## 这个阶段的产物

| 天 | 代码 | 报告 |
|---|---|---|
| D01 | [code.py](day01-看见数据/code.py) ｜ [practice.py](day01-看见数据/practice.py) | [output.md](day01-看见数据/output.md) |
| D02 | [code.py](day02-规模与覆盖/code.py) ｜ [inspect_tables.py](day02-规模与覆盖/inspect_tables.py) | [output.md](day02-规模与覆盖/output.md) |
| D06 | [code.py](day06-缺失率与起始日/code.py) | [output.md](day06-缺失率与起始日/output.md) |
| D09 | — | [output.md](day09-幸存者偏差/output.md) |
| D11 | [full_check.py](day11-整合脚本/full_check.py) | — |
| D12 | — | [output.md](day12-Gate1/output.md) |

数据字典（全 23 张表）在 [参考/表结构.md](../参考/表结构.md)。

## 阶段总结

[阶段总结.md](阶段总结.md)
