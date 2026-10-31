# D2 规模与覆盖（阶段 0 第 2 天）

- 生成时间：2026-10-31 17:33:26
- 数据目录：`/opt/quantmind/data/quantdb`
- 引擎：DuckDB 2.0.0

## 1. 三个数（不看屏幕也要能说出来）

```
 股票数  交易日数        起        止      总行数
5581  2623 20160104 20261023 10826172
```

→ **5,581 只股票、2,623 个交易日、20160104 到 20261023**，共 10,826,172 行。

## 2. 每个交易日的股票数，按年汇总

> 只看年初年末会漏掉「某天突然塌到几百只」，所以每年要看 min。

```
  年份  交易日   最少   中位   最多  最差偏离百分比
2016  244 2314 2400 2632     -3.6
2017  244 2628 2837 3053     -7.4
2018  243 2978 3147 3329     -5.4
2019  244 3332 3395 3531     -1.9
2020  243 3530 3650 3964     -3.3
2021  243 3967 4232 4482     -6.3
2022  242 4482 4666 4914     -3.9
2023  242 4914 5094 5229     -3.5
2024  242 5229 5277 5322     -0.9
2025  243 5319 5376 5437     -1.1
2026  193 5433 5503 5577     -1.3
```

→ 股票数**逐年长**（2016 年 2400 左右 → 2026 年 5500 左右），且每天偏离中位数不超过 8% —— 没有哪一天塌掉。

## 3. 覆盖矩阵：每张表分别覆盖什么

> 「覆盖」不是行数多少，而是**能不能覆盖到你要的那批股票和日期**。日线 5581 只，features_daily 只有 5228 只（缺北交所），join 就静默少 350+ 只。

```
                 类别                    表      中文        布局       行数    股票数  列数                   起                   止
       1_kline_data       daily_backward   日线后复权 partition 10826172 5581.0   9            20160104            20261023
       1_kline_data        daily_forward   日线前复权 partition 10826172 5581.0   9            20160104            20261023
       1_kline_data     daily_unadjusted   日线不复权 partition 10826172 5581.0   9            20160104            20261023
       1_kline_data          index_daily    指数日线 partition   114653   46.0  10            20160104            20261023
      2_base_sector        index_weights    指数权重    single     2800 1809.0   3                None                None
      2_base_sector    instrument_detail    个股详情    single     5581 5581.0 152                None                None
      2_base_sector       margin_trading    融资融券 partition  5042454 3874.0  11            20160104            20261022
      2_base_sector       sector_concept    板块概念    single    78890 5972.0   4                None                None
      2_base_sector     trading_calendar    交易日历    single     2672    NaN   2            20160104            20261231
   3_financial_data              balance   资产负债表    single   191900 5559.0  75                None                None
   3_financial_data              capital    股本结构    single   308841 5559.0  10                None                None
   3_financial_data             cashflow   现金流量表    single   196961 5559.0  68                None                None
   3_financial_data     dividend_factors  分红送转因子    single    58933 5451.0  11 1990-03-01 00:00:00 2026-10-23 00:00:00
   3_financial_data           holder_num    股东户数    single   191533 5559.0  38                None                None
   3_financial_data               income     利润表    single   197727 5559.0  37                None                None
   3_financial_data            per_share 每股指标TTM    single   191467 5558.0  10                None                None
   3_financial_data       pershare_index  每股财务指标    single   193749 5559.0  86                None                None
         4_bond_etf     convertible_bond     可转债    single      316    NaN  25                None                None
         4_bond_etf              etf_pcf ETF申赎清单    single   192081    NaN  31                None                None
5_technical_derived     market_sentiment    市场情绪 partition 10760462 5575.0  18            20160104            20260930
5_technical_derived technical_indicators    技术指标 partition 10827362 5582.0  36            20160104            20261023
5_technical_derived            valuation      估值 partition 10760462 5575.0  17            20160104            20260930
      6_ml_datasets       features_daily    日频特征 partition 10550057 5228.0  79            20160104            20261023
```

### 主表有、features_daily 没有的股票

```
 缺的股票数                                                                                                     前 10 个样例
    10 920010.BJ, 920145.BJ, 920242.BJ, 920436.BJ, 920454.BJ, 920670.BJ, 920675.BJ, 920826.BJ, 920839.BJ, 920885.BJ
```

→ 多为**北交所**（43/83/87/88/92 开头）。做多市场因子时 join 会静默丢行；只做沪深反而是对的 —— 前提是你知道自己丢了什么。

## 4. 交易日历 vs 落地数据

```
 日历交易日  数据天数  日历有数据无  数据有日历无
  2672  2623      49       0
```

```
    缺口最早     缺口最晚   数据最后一天
20261026 20261231 20261023
```

→ 差的那几天全在**数据最后一天之后**：日历是排期表（已排到年底），不是数据清单。拿日历 reindex 数据会凭空多出全 NaN 的交易日。
