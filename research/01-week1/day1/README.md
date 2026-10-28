# day1 · 看见数据（阶段 0 第 1 天）

> 这一天的目标不是「跑通」，是**能指着任意一列说出它是什么、什么单位**。
> 目录约定：`research/01-week1/dayN/` = 第 1 周第 N 个学习日；代码 + 本文件 + 生成物都放这一层。

## 1. 为什么这么组织

- **代码**（`*.py`）：每个学习日一个，可重复跑，跑一次几秒到几十秒。
- **本文件**（`README.md`）：当天的结论与判据，写给三天后的自己看。
- **生成物**（`tables_schema.md` / `tables_overview.csv`）：脚本产出，可删可重建。
- 需要长期归档的图表/实验记录才放 `research/out/`（仓库 `.gitignore` 里只有这个目录被例外保留）。

## 2. 盘上现在到底有什么

只下了 **1.2 GB / 9 张表**（L0 的日线与基础板块部分），另外 17 张没下：

| 类别 | 表 | 中文 | 布局 | 文件数 | 体积 |
|---|---|---|---|---|---|
| 1_kline_data | `daily_forward` | 日线前复权 | 按日分区 | 2623 | 0.37 GB |
| 1_kline_data | `daily_backward` | 日线后复权 | 按日分区 | 2623 | 0.39 GB |
| 1_kline_data | `daily_unadjusted` | 日线不复权 | 按日分区 | 2623 | 0.22 GB |
| 1_kline_data | `index_daily` | 指数日线 | 按日分区 | 2623 | 0.01 GB |
| 2_base_sector | `instrument_detail` | 个股详情（152 列） | 单文件 | 1 | 3.1 MB |
| 2_base_sector | `sector_concept` | 板块概念 | 单文件 | 1 | 0.2 MB |
| 2_base_sector | `index_weights` | 指数权重 | 按指数 | 7 | 42 KB |
| 2_base_sector | `trading_calendar` | 交易日历 | 单文件 | 1 | 16 KB |
| 2_base_sector | `margin_trading` | 融资融券 | 按日分区 | 2622 | 0.13 GB |

没下（用到再补，别一次拉 9 GB）：`features_daily`（3.34）、`technical_indicators`（2.44）、`valuation`（0.79）、`market_sentiment`（0.94）、财务 8 表（1.45）、`etf_pcf`、`convertible_bond`。

## 3. 这个目录里有什么

| 文件 | 作用 | 产出 |
|---|---|---|
| `inspect_tables.py` | 扫全部表：列名/类型/行数/前 3 行 | `tables_schema.md`、`tables_overview.csv` |
| `d01_data_check.py` | 第 1 天体检：5 行样本 + 规模 + 单位自检 + 三类异常 | `research/out/D01_data_check.md` |
| `practice.py` | 改数字的练习（改顶部 `DATE` / `SYMBOL` 再跑） | 屏幕 |

交互式查询留在 `research/repl.py`（每天都会用，不归到某一天）。

## 3.1 怎么跑

```bash
cd /root/QuantMind

# ① 扫表结构（约 20~40 秒）
/root/quant/.venv/bin/python research/01-week1/day1/inspect_tables.py

# ② 第 1 天体检（约 15 秒）
/root/quant/.venv/bin/python research/01-week1/day1/d01_data_check.py

# ③ 交互式查数据（改数字用这个）
/root/quant/.venv/bin/python research/repl.py
```

跑完会多两个文件：

- `tables_schema.md` —— 每张表的**全部列名 + 类型 + 前 3 行 + 行数/体积**
- `tables_overview.csv` —— 一张表一行，方便排序比较

## 4. 跑完重点看这五处（这才是「看见数据」）

1. **`trading_calendar` 只有 2 列**：`TradingDate`（`20160104` 这种字符串）+ `IsTradingDay`。日历是**排期表**，含到 2026-12-31 的未来日期（2672 天 > 数据的 2623 天），**不是数据清单**。
2. **日线 9 列**：`symbol / time / dt / open / high / low / close / volume / amount`。同一只股票同一天，`time` 是时间戳、`dt` 是整数 —— 两个都存，别混用。
3. **`index_daily` 和个股日线同构但不同口径**：`volume` 是**手**不是股，`close` 是点位不是元。
4. **`instrument_detail` 是 152 列的宽表**，且 `J_*` 财务字段是**万元**、`Zsz/Ltsz` 是**亿元**、`J_zgb` 是**万股** —— 三套单位混在一张表里。
5. **`margin_trading` 是按日分区的**（2622 个文件），不是按股票；和财务表的「一股一文件」布局相反。

## 5. 判据（自己问自己，答不出就是没做完）

- [ ] 不看文档，说出日线表有哪 9 列，以及 `volume`、`amount` 的单位
- [ ] 说出「按日分区」和「按股票一个文件」两种布局，各举一张表
- [ ] 说出 `index_daily` 和 `daily_unadjusted` 在两个字段上的口径差异

## 6. 下一步（day2）

day2 已经有一份脚本：`research/01-week1/day2/d02_coverage.py`，做的是「规模与覆盖」——
5581 只 / 2623 个交易日 / 20160104–20261023，并核对日历与数据的差集。
跑法一样，把换成 day2 的脚本即可。
