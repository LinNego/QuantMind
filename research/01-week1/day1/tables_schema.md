# QuantDB 数据字典（day1 自动生成）

- 生成时间：2026-10-28 05:06:30
- 数据目录：`/opt/quantmind/data/quantdb`
- 引擎：DuckDB 2.0.0
- 扫描表数：9

> 本文件由 `inspect_tables.py` 生成，只含结构（列名/类型/行数）与 3 行样本，不含分析结论。

## daily_backward（日线后复权）

- 类别：`1_kline_data` ｜ 布局：`partition` ｜ 文件数：2623 ｜ 体积：394.4 MB
- 行数：**10,826,172** ｜ 列数：**9** ｜ 扫描耗时：2.2s

### 列

| column_name | column_type | null | key | default | extra |
|---|---|---|---|---|---|
| symbol | VARCHAR | YES | nan | nan | nan |
| time | TIMESTAMP_NS | YES | nan | nan | nan |
| open | DOUBLE | YES | nan | nan | nan |
| high | DOUBLE | YES | nan | nan | nan |
| low | DOUBLE | YES | nan | nan | nan |
| close | DOUBLE | YES | nan | nan | nan |
| volume | DOUBLE | YES | nan | nan | nan |
| amount | DOUBLE | YES | nan | nan | nan |
| dt | BIGINT | YES | nan | nan | nan |

### 前 3 行

| symbol | time | open | high | low | close | volume | amount | dt |
|---|---|---|---|---|---|---|---|---|
| 000001.SZ | 2016-01-04 00:00:00 | 12.0 | 12.03 | 11.23 | 11.33 | 56349700.0 | 66037.6 | 20160104 |
| 000006.SZ | 2016-01-04 00:00:00 | 11.46 | 11.52 | 10.36 | 10.38 | 23285272.0 | 25463.98 | 20160104 |
| 000008.SZ | 2016-01-04 00:00:00 | 11.88 | 11.88 | 10.6 | 10.61 | 14039993.0 | 15701.46 | 20160104 |

### 单位（来自 skills/quantdb-fields）

- `symbol`：后缀式 600519.SH
- `time`：时间戳
- `open`：元
- `high`：元
- `low`：元
- `close`：元（注意各表复权口径不同）
- `volume`：股（个股）；指数表是手
- `amount`：万元
- `dt`：YYYYMMDD 整数

## daily_forward（日线前复权）

- 类别：`1_kline_data` ｜ 布局：`partition` ｜ 文件数：2623 ｜ 体积：383.4 MB
- 行数：**10,826,172** ｜ 列数：**9** ｜ 扫描耗时：1.6s

### 列

| column_name | column_type | null | key | default | extra |
|---|---|---|---|---|---|
| symbol | VARCHAR | YES | nan | nan | nan |
| time | TIMESTAMP_NS | YES | nan | nan | nan |
| open | DOUBLE | YES | nan | nan | nan |
| high | DOUBLE | YES | nan | nan | nan |
| low | DOUBLE | YES | nan | nan | nan |
| close | DOUBLE | YES | nan | nan | nan |
| volume | DOUBLE | YES | nan | nan | nan |
| amount | DOUBLE | YES | nan | nan | nan |
| dt | BIGINT | YES | nan | nan | nan |

### 前 3 行

| symbol | time | open | high | low | close | volume | amount | dt |
|---|---|---|---|---|---|---|---|---|
| 000001.SZ | 2016-01-04 00:00:00 | 7.271923384930041 | 7.2901031933923655 | 6.805308301063697 | 6.86590766260478 | 56349700.0 | 66037.6 | 20160104 |
| 000006.SZ | 2016-01-04 00:00:00 | 9.204110840127822 | 9.252299902118018 | 8.320644703640857 | 8.336707724304258 | 23285272.0 | 25463.98 | 20160104 |
| 000008.SZ | 2016-01-04 00:00:00 | 11.674632665244886 | 11.674632665244886 | 10.416759785487859 | 10.42658691736096 | 14039993.0 | 15701.46 | 20160104 |

### 单位（来自 skills/quantdb-fields）

- `symbol`：后缀式 600519.SH
- `time`：时间戳
- `open`：元
- `high`：元
- `low`：元
- `close`：元（注意各表复权口径不同）
- `volume`：股（个股）；指数表是手
- `amount`：万元
- `dt`：YYYYMMDD 整数

## daily_unadjusted（日线不复权）

- 类别：`1_kline_data` ｜ 布局：`partition` ｜ 文件数：2623 ｜ 体积：223.8 MB
- 行数：**10,826,172** ｜ 列数：**9** ｜ 扫描耗时：1.5s

### 列

| column_name | column_type | null | key | default | extra |
|---|---|---|---|---|---|
| symbol | VARCHAR | YES | nan | nan | nan |
| time | TIMESTAMP_NS | YES | nan | nan | nan |
| open | DOUBLE | YES | nan | nan | nan |
| high | DOUBLE | YES | nan | nan | nan |
| low | DOUBLE | YES | nan | nan | nan |
| close | DOUBLE | YES | nan | nan | nan |
| volume | DOUBLE | YES | nan | nan | nan |
| amount | DOUBLE | YES | nan | nan | nan |
| dt | BIGINT | YES | nan | nan | nan |

### 前 3 行

| symbol | time | open | high | low | close | volume | amount | dt |
|---|---|---|---|---|---|---|---|---|
| 000001.SZ | 2016-01-04 00:00:00 | 12.0 | 12.03 | 11.23 | 11.33 | 56349700.0 | 66037.6 | 20160104 |
| 000006.SZ | 2016-01-04 00:00:00 | 11.46 | 11.52 | 10.36 | 10.38 | 23285272.0 | 25463.98 | 20160104 |
| 000008.SZ | 2016-01-04 00:00:00 | 11.88 | 11.88 | 10.6 | 10.61 | 14039993.0 | 15701.46 | 20160104 |

### 单位（来自 skills/quantdb-fields）

- `symbol`：后缀式 600519.SH
- `time`：时间戳
- `open`：元
- `high`：元
- `low`：元
- `close`：元（注意各表复权口径不同）
- `volume`：股（个股）；指数表是手
- `amount`：万元
- `dt`：YYYYMMDD 整数

## index_daily（指数日线）

- 类别：`1_kline_data` ｜ 布局：`partition` ｜ 文件数：2623 ｜ 体积：8.1 MB
- 行数：**114,653** ｜ 列数：**10** ｜ 扫描耗时：1.7s

### 列

| column_name | column_type | null | key | default | extra |
|---|---|---|---|---|---|
| symbol | VARCHAR | YES | nan | nan | nan |
| time | TIMESTAMP_NS | YES | nan | nan | nan |
| open | DOUBLE | YES | nan | nan | nan |
| high | DOUBLE | YES | nan | nan | nan |
| low | DOUBLE | YES | nan | nan | nan |
| close | DOUBLE | YES | nan | nan | nan |
| volume | DOUBLE | YES | nan | nan | nan |
| amount | DOUBLE | YES | nan | nan | nan |
| Category | VARCHAR | YES | nan | nan | nan |
| dt | BIGINT | YES | nan | nan | nan |

### 前 3 行

| symbol | time | open | high | low | close | volume | amount | Category | dt |
|---|---|---|---|---|---|---|---|---|---|
| 000001.SH | 2016-01-04 00:00:00 | 3536.59 | 3538.69 | 3295.74 | 3296.26 | 18441841600.0 | 24092916.0 | 宽基 | 20160104 |
| 000002.SH | 2016-01-04 00:00:00 | 3701.58 | 3703.78 | 3449.5 | 3450.03 | 18281590400.0 | 23961832.0 | 风格 | 20160104 |
| 000010.SH | 2016-01-04 00:00:00 | 7986.39 | 7986.39 | 7458.01 | 7458.31 | 7461066400.0 | 8746727.0 | 风格 | 20160104 |

### 单位（来自 skills/quantdb-fields）

- `symbol`：后缀式 600519.SH
- `time`：时间戳
- `open`：元
- `high`：元
- `low`：元
- `close`：元（注意各表复权口径不同）
- `volume`：股（个股）；指数表是手
- `amount`：万元
- `dt`：YYYYMMDD 整数

## index_weights（指数权重）

- 类别：`2_base_sector` ｜ 布局：`single` ｜ 文件数：7 ｜ 体积：41.6 KB
- 行数：**2,800** ｜ 列数：**3** ｜ 扫描耗时：0.0s

### 列

| column_name | column_type | null | key | default | extra |
|---|---|---|---|---|---|
| Symbol | VARCHAR | YES | nan | nan | nan |
| Weight | DOUBLE | YES | nan | nan | nan |
| IndexCode | VARCHAR | YES | nan | nan | nan |

### 前 3 行

| Symbol | Weight | IndexCode |
|---|---|---|
| 600028.SH | 0.811 | 000016.SH |
| 600030.SH | 2.938 | 000016.SH |
| 600031.SH | 1.291 | 000016.SH |

## instrument_detail（个股详情）

- 类别：`2_base_sector` ｜ 布局：`single` ｜ 文件数：1 ｜ 体积：3.0 MB
- 行数：**5,581** ｜ 列数：**152** ｜ 扫描耗时：0.1s

### 列

| column_name | column_type | null | key | default | extra |
|---|---|---|---|---|---|
| Symbol | VARCHAR | YES | nan | nan | nan |
| ErrorId | VARCHAR | YES | nan | nan | nan |
| Name | VARCHAR | YES | nan | nan | nan |
| Unit | VARCHAR | YES | nan | nan | nan |
| VolBase | VARCHAR | YES | nan | nan | nan |
| MinPrice | VARCHAR | YES | nan | nan | nan |
| XsFlag | VARCHAR | YES | nan | nan | nan |
| Fz | VARCHAR[] | YES | nan | nan | nan |
| DelayMin | VARCHAR | YES | nan | nan | nan |
| QHVolBaseRate | VARCHAR | YES | nan | nan | nan |
| HKVolBaseRate | VARCHAR | YES | nan | nan | nan |
| BelongHS300 | VARCHAR | YES | nan | nan | nan |
| BelongHasKQZ | VARCHAR | YES | nan | nan | nan |
| BelongRZRQ | VARCHAR | YES | nan | nan | nan |
| BelongHSGT | VARCHAR | YES | nan | nan | nan |
| IsHKGP | VARCHAR | YES | nan | nan | nan |
| IsQH | VARCHAR | YES | nan | nan | nan |
| IsQQ | VARCHAR | YES | nan | nan | nan |
| IsSTGP | VARCHAR | YES | nan | nan | nan |
| IsQuitGP | VARCHAR | YES | nan | nan | nan |
| TodayDRFlag | VARCHAR | YES | nan | nan | nan |
| HSStockKind | VARCHAR | YES | nan | nan | nan |
| ActiveCapital | VARCHAR | YES | nan | nan | nan |
| J_zgb | DOUBLE | YES | nan | nan | nan |
| J_bg | VARCHAR | YES | nan | nan | nan |
| J_hg | VARCHAR | YES | nan | nan | nan |
| J_zzc | VARCHAR | YES | nan | nan | nan |
| J_ldzc | VARCHAR | YES | nan | nan | nan |
| J_gdzc | VARCHAR | YES | nan | nan | nan |
| J_wxzc | VARCHAR | YES | nan | nan | nan |
| J_ldfz | VARCHAR | YES | nan | nan | nan |
| J_cqfz | VARCHAR | YES | nan | nan | nan |
| J_zbgjj | VARCHAR | YES | nan | nan | nan |
| J_jzc | VARCHAR | YES | nan | nan | nan |
| J_yysy | VARCHAR | YES | nan | nan | nan |
| J_yycb | VARCHAR | YES | nan | nan | nan |
| J_yszk | VARCHAR | YES | nan | nan | nan |
| J_yyly | VARCHAR | YES | nan | nan | nan |
| J_tzsy | VARCHAR | YES | nan | nan | nan |
| J_jyxjl | VARCHAR | YES | nan | nan | nan |
| J_zxjl | VARCHAR | YES | nan | nan | nan |
| J_ch | VARCHAR | YES | nan | nan | nan |
| J_lyze | VARCHAR | YES | nan | nan | nan |
| J_shly | VARCHAR | YES | nan | nan | nan |
| J_jly | VARCHAR | YES | nan | nan | nan |
| J_wfply | VARCHAR | YES | nan | nan | nan |
| J_jyl | VARCHAR | YES | nan | nan | nan |
| J_mgwfp | VARCHAR | YES | nan | nan | nan |
| J_mgsy | VARCHAR | YES | nan | nan | nan |
| J_mgsy2 | VARCHAR | YES | nan | nan | nan |
| J_mggjj | VARCHAR | YES | nan | nan | nan |
| J_mgjzc | VARCHAR | YES | nan | nan | nan |
| J_mgjzc2 | VARCHAR | YES | nan | nan | nan |
| J_gdqyb | VARCHAR | YES | nan | nan | nan |
| J_gdrs | VARCHAR | YES | nan | nan | nan |
| J_HalfYearFlag | VARCHAR | YES | nan | nan | nan |
| J_start | VARCHAR | YES | nan | nan | nan |
| tdx_dycode | VARCHAR | YES | nan | nan | nan |
| tdx_dyname | VARCHAR | YES | nan | nan | nan |
| rs_hycode_sim | VARCHAR | YES | nan | nan | nan |
| rs_hyname | VARCHAR | YES | nan | nan | nan |
| blockzscode | VARCHAR | YES | nan | nan | nan |
| underly_setcode | VARCHAR | YES | nan | nan | nan |
| underly_code | VARCHAR | YES | nan | nan | nan |
| MainBusiness | VARCHAR | YES | nan | nan | nan |
| SafeValue | VARCHAR | YES | nan | nan | nan |
| ShineValue | VARCHAR | YES | nan | nan | nan |
| ShapeValue | VARCHAR | YES | nan | nan | nan |
| TPFlag | VARCHAR | YES | nan | nan | nan |
| ZTPrice | VARCHAR | YES | nan | nan | nan |
| DTPrice | VARCHAR | YES | nan | nan | nan |
| HqDate | VARCHAR | YES | nan | nan | nan |
| fHSL | VARCHAR | YES | nan | nan | nan |
| fLianB | VARCHAR | YES | nan | nan | nan |
| Wtb | VARCHAR | YES | nan | nan | nan |
| Zsz | VARCHAR | YES | nan | nan | nan |
| Ltsz | VARCHAR | YES | nan | nan | nan |
| vzangsu | VARCHAR | YES | nan | nan | nan |
| Fzhsl | VARCHAR | YES | nan | nan | nan |
| FzAmo | VARCHAR | YES | nan | nan | nan |
| VOpenZAF | VARCHAR | YES | nan | nan | nan |
| ZAF | VARCHAR | YES | nan | nan | nan |
| ZAFYesterday | VARCHAR | YES | nan | nan | nan |
| ZAFPre2D | VARCHAR | YES | nan | nan | nan |
| ZAFPre5 | VARCHAR | YES | nan | nan | nan |
| ZAFPre10 | VARCHAR | YES | nan | nan | nan |
| ZAFPre20 | VARCHAR | YES | nan | nan | nan |
| ZAFPre30 | VARCHAR | YES | nan | nan | nan |
| ZAFPre60 | VARCHAR | YES | nan | nan | nan |
| ZAFYear | VARCHAR | YES | nan | nan | nan |
| ZAFPreMyMonth | VARCHAR | YES | nan | nan | nan |
| ZAFPreOneYear | VARCHAR | YES | nan | nan | nan |
| Zjl | VARCHAR | YES | nan | nan | nan |
| Zjl_HB | VARCHAR | YES | nan | nan | nan |
| TotalBVol | VARCHAR | YES | nan | nan | nan |
| TotalSVol | VARCHAR | YES | nan | nan | nan |
| BCancel | VARCHAR | YES | nan | nan | nan |
| SCancel | VARCHAR | YES | nan | nan | nan |
| L2TicNum | VARCHAR | YES | nan | nan | nan |
| L2OrderNum | VARCHAR | YES | nan | nan | nan |
| FCAmo | VARCHAR | YES | nan | nan | nan |
| FCb | VARCHAR | YES | nan | nan | nan |
| OpenZAF | VARCHAR | YES | nan | nan | nan |
| OpenAmo | VARCHAR | YES | nan | nan | nan |
| OpenZTBuy | VARCHAR | YES | nan | nan | nan |
| OpenAmoPre1 | VARCHAR | YES | nan | nan | nan |
| OpenVolPre1 | VARCHAR | YES | nan | nan | nan |
| CJJEPre1 | VARCHAR | YES | nan | nan | nan |
| CJJEPre3 | VARCHAR | YES | nan | nan | nan |
| FDEPre1 | VARCHAR | YES | nan | nan | nan |
| FDEPre2 | VARCHAR | YES | nan | nan | nan |
| ZTGPNum | VARCHAR | YES | nan | nan | nan |
| LastStartZT | VARCHAR | YES | nan | nan | nan |
| LastZTHzNum | VARCHAR | YES | nan | nan | nan |
| EverZTCount | VARCHAR | YES | nan | nan | nan |
| ConZAFDateNum | VARCHAR | YES | nan | nan | nan |
| YearZTDay | VARCHAR | YES | nan | nan | nan |
| MA5Value | VARCHAR | YES | nan | nan | nan |
| HisHigh | VARCHAR | YES | nan | nan | nan |
| HisLow | VARCHAR | YES | nan | nan | nan |
| IPO_Price | VARCHAR | YES | nan | nan | nan |
| More_YJL | VARCHAR | YES | nan | nan | nan |
| BetaValue | VARCHAR | YES | nan | nan | nan |
| DynaPE | VARCHAR | YES | nan | nan | nan |
| MorePE | VARCHAR | YES | nan | nan | nan |
| StaticPE_TTM | VARCHAR | YES | nan | nan | nan |
| DYRatio | VARCHAR | YES | nan | nan | nan |
| PB_MRQ | VARCHAR | YES | nan | nan | nan |
| IsT0Fund | VARCHAR | YES | nan | nan | nan |
| IsZCZGP | VARCHAR | YES | nan | nan | nan |
| IsKzz | VARCHAR | YES | nan | nan | nan |
| Kzz_HSCode | VARCHAR | YES | nan | nan | nan |
| QHMainYYMM | VARCHAR | YES | nan | nan | nan |
| FreeLtgb | DOUBLE | YES | nan | nan | nan |
| Yield | VARCHAR | YES | nan | nan | nan |
| KfEarnMoney | VARCHAR | YES | nan | nan | nan |
| RDInputFee | VARCHAR | YES | nan | nan | nan |
| CashZJ | VARCHAR | YES | nan | nan | nan |
| PreReceiveZJ | VARCHAR | YES | nan | nan | nan |
| OtherQYJzc | VARCHAR | YES | nan | nan | nan |
| StaffNum | VARCHAR | YES | nan | nan | nan |
| RecentGGJYDate | VARCHAR | YES | nan | nan | nan |
| RecentHGDate | VARCHAR | YES | nan | nan | nan |
| RecentIncentDate | VARCHAR | YES | nan | nan | nan |
| NoticeDate_Recent | VARCHAR | YES | nan | nan | nan |
| RecentReleaseDate | VARCHAR | YES | nan | nan | nan |
| RecentDZDate | VARCHAR | YES | nan | nan | nan |
| ReportDate | VARCHAR | YES | nan | nan | nan |
| ZTDate_Recent | VARCHAR | YES | nan | nan | nan |
| DTDate_Recent | VARCHAR | YES | nan | nan | nan |
| TopDate_Recent | VARCHAR | YES | nan | nan | nan |
| StopJYDate_Recent | VARCHAR | YES | nan | nan | nan |

### 前 3 行

| Symbol | ErrorId | Name | Unit | VolBase | MinPrice | XsFlag | Fz | DelayMin | QHVolBaseRate | HKVolBaseRate | BelongHS300 | BelongHasKQZ | BelongRZRQ | BelongHSGT | IsHKGP | IsQH | IsQQ | IsSTGP | IsQuitGP | TodayDRFlag | HSStockKind | ActiveCapital | J_zgb | J_bg | J_hg | J_zzc | J_ldzc | J_gdzc | J_wxzc | J_ldfz | J_cqfz | J_zbgjj | J_jzc | J_yysy | J_yycb | J_yszk | J_yyly | J_tzsy | J_jyxjl | J_zxjl | J_ch | J_lyze | J_shly | J_jly | J_wfply | J_jyl | J_mgwfp | J_mgsy | J_mgsy2 | J_mggjj | J_mgjzc | J_mgjzc2 | J_gdqyb | J_gdrs | J_HalfYearFlag | J_start | tdx_dycode | tdx_dyname | rs_hycode_sim | rs_hyname | blockzscode | underly_setcode | underly_code | MainBusiness | SafeValue | ShineValue | ShapeValue | TPFlag | ZTPrice | DTPrice | HqDate | fHSL | fLianB | Wtb | Zsz | Ltsz | vzangsu | Fzhsl | FzAmo | VOpenZAF | ZAF | ZAFYesterday | ZAFPre2D | ZAFPre5 | ZAFPre10 | ZAFPre20 | ZAFPre30 | ZAFPre60 | ZAFYear | ZAFPreMyMonth | ZAFPreOneYear | Zjl | Zjl_HB | TotalBVol | TotalSVol | BCancel | SCancel | L2TicNum | L2OrderNum | FCAmo | FCb | OpenZAF | OpenAmo | OpenZTBuy | OpenAmoPre1 | OpenVolPre1 | CJJEPre1 | CJJEPre3 | FDEPre1 | FDEPre2 | ZTGPNum | LastStartZT | LastZTHzNum | EverZTCount | ConZAFDateNum | YearZTDay | MA5Value | HisHigh | HisLow | IPO_Price | More_YJL | BetaValue | DynaPE | MorePE | StaticPE_TTM | DYRatio | PB_MRQ | IsT0Fund | IsZCZGP | IsKzz | Kzz_HSCode | QHMainYYMM | FreeLtgb | Yield | KfEarnMoney | RDInputFee | CashZJ | PreReceiveZJ | OtherQYJzc | StaffNum | RecentGGJYDate | RecentHGDate | RecentIncentDate | NoticeDate_Recent | RecentReleaseDate | RecentDZDate | ReportDate | ZTDate_Recent | DTDate_Recent | TopDate_Recent | StopJYDate_Recent |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 000001.SZ | 0 | 平安银行 | 100 | 4652.04 | 0.01 | 2 | ['570' '690' '780' '900' '900' '900' '90… | 0 | 0 | 0 | 1 | 0 | 1 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 1940568.50 | 19405918800.0 | 0.00 | 0.00 | 602878528.00 | 0.00 | 1046400.00 | 541500.00 | 0.00 | 0.00 | 8042800.00 | 54821400.00 | 7061700.00 | 3978899.75 | 0.00 | 3082800.00 | 1152600.00 | 21501200.00 | -5149500.00 | 0.00 | 3079500.00 | 2569600.00 | 2569600.00 | 28785698.00 | 4.69 | 14.83 | 2.65 | 0.00 | 4.14 | 24.13 | 24.13 | 0.09 | 450712.00 | 6 | 19910403 | 18 | 深圳板块 | X5001 | 全国性银行 | 881386 | 0 |  | 批发金融业务 | 100 | 8 | 80804 | 0 | 12.65 | 10.35 | 20261015 | 0.78 | 1.35 | 20.92 | 2214.22 | 2214.19 | 0.83 | 0.03 | 1128.33 | -0.16 | -0.78 | -0.61 | -1.36 | -3.14 | 0.53 | -0.35 | -0.61 | 7.74 | 5.65 | -1.39 | 6.44 | 0.00 | 0.00 | 92424.00 | 158467.00 | 343777.00 | 479282.00 | 127028 | 191152 | 0.00 | 0.00 | 0.00 | 11716600.00 | 0.00 | 417.91 | 3612.00 | 112228.39 | 399935.95 | 0.00 | 0.00 | 0 | 1 | 0 | 0 | -3 | 0 | 11.56 | 11.89 | 9.74 | 40.00 | 0.00 | -0.24 | 4.31 | 5.23 | 5.14 | 5.30 | 0.47 | 0 | 0 | 0 | 0 | 0 | 8160565800.0 | 1171.66 | 2563299.94 | 0.00 | 35487701.40 | 0.00 | 8000000.00 | 41698 | 0 | 0 | 0 | 0 | 20180521 | 20150521 | 20260815 | 20240221 | 20150119 | 20240221 | 0 |
| 000002.SZ | 0 | 万 科Ａ | 100 | 20978.63 | 0.01 | 2 | ['570' '690' '780' '900' '900' '900' '90… | 0 | 0 | 0 | 1 | 0 | 1 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 971511.31 | 11930710000.0 | 0.00 | 220651.30 | 95976152.00 | 65302464.00 | 1811977.38 | 708454.94 | 55342984.00 | 11362503.00 | 2695837.50 | 10218120.00 | 7016911.00 | 6459992.50 | 1106781.50 | -1251303.00 | -334048.94 | 49476.60 | -834638.88 | 34595788.00 | -1348998.88 | -1602125.25 | -1495126.88 | -5936986.50 | -14.63 | -4.98 | -2.51 | 0.00 | 2.26 | 8.56 | 8.56 | 0.12 | 458102.00 | 6 | 19910129 | 18 | 深圳板块 | X5301 | 房地产开发 | 881418 | 0 |  | 抵销,开发经营业务 | 69 | 4 | 80511 | 0 | 4.04 | 3.30 | 20261015 | 3.48 | 0.67 | 33.26 | 429.51 | 349.74 | 0.89 | 0.10 | 1010.80 | 1.67 | -1.91 | -1.61 | -1.06 | -11.33 | -8.17 | 12.86 | 15.01 | 15.01 | -22.59 | -15.49 | -44.87 | 0.00 | 0.00 | 146120.00 | 465385.00 | 1337439.00 | 1883313.00 | 101150 | 189151 | 0.00 | 0.00 | -0.27 | 22511600.00 | 0.00 | 2358.98 | 62906.00 | 119696.51 | 380069.81 | 0.00 | 0.00 | 0 | 1 | 0 | 0 | -4 | 3 | 3.77 | 6.57 | 2.88 | 1.00 | 0.00 | 0.44 | -1.44 | -0.49 | -0.48 | 0.00 | 0.42 | 0 | 0 | 0 | 0 | 0 | 6472302600.0 | 2251.16 | -1384043.62 | 21326.42 | 5904928.36 | 7755316.43 | 0.00 | 131429 | 0 | 0 | 0 | 0 | 20091229 | 20061227 | 20260828 | 20260929 | 20241009 | 20260930 | 0 |
| 000006.SZ | 0 | 深振业Ａ | 100 | 1246.30 | 0.01 | 2 | ['570' '690' '780' '900' '900' '900' '90… | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 134999.50 | 1349995000.0 | 0.00 | 0.00 | 1598571.13 | 1422465.00 | 6120.22 | 0.00 | 726661.13 | 78273.30 | 48432.15 | 526578.69 | 79643.24 | 68688.92 | 3848.30 | -9512.71 | 1.87 | 44395.35 | 8313.46 | 1023992.00 | -9438.16 | -9703.15 | -9573.12 | 125320.48 | -1.82 | 0.93 | -0.14 | 0.00 | 0.36 | 3.90 | 3.90 | 0.35 | 55505.00 | 6 | 19920427 | 18 | 深圳板块 | X5301 | 房地产开发 | 881418 | 0 |  | 房产销售 | 82 | 2 | 141309 | 0 | 7.68 | 6.28 | 20261015 | 1.60 | 0.72 | -11.71 | 90.72 | 90.72 | 1.01 | 0.05 | 117.00 | 0.00 | -3.72 | -2.92 | 0.98 | -10.40 | -7.18 | -6.54 | -5.49 | 3.71 | -32.46 | -10.52 | -51.27 | 0.00 | 0.00 | 5494.00 | 23772.00 | 98216.00 | 101775.00 | 18435 | 36139 | 0.00 | 0.00 | -2.58 | 3391800.00 | 0.00 | 127.35 | 1759.00 | 15176.26 | 46355.16 | 0.00 | 0.00 | 0 | 1 | 0 | 0 | -2 | 4 | 7.09 | 15.50 | 5.58 | 10.00 | 0.00 | 0.67 | -47.38 | -222.99 | -140.62 | 0.00 | 1.72 | 0 | 0 | 0 | 0 | 0 | 856196799.9999999 | 339.18 | -9887.14 | 0.00 | 253535.97 | 257279.97 | 0.00 | 417 | 0 | 0 | 0 | 0 | 20130517 | 0 | 20260826 | 20260813 | 20251015 | 20251021 | 0 |

## margin_trading（融资融券）

- 类别：`2_base_sector` ｜ 布局：`partition` ｜ 文件数：2622 ｜ 体积：136.8 MB
- 行数：**5,042,454** ｜ 列数：**11** ｜ 扫描耗时：2.5s

### 列

| column_name | column_type | null | key | default | extra |
|---|---|---|---|---|---|
| symbol | VARCHAR | YES | nan | nan | nan |
| time | TIMESTAMP_NS | YES | nan | nan | nan |
| finance_balance | DOUBLE | YES | nan | nan | nan |
| slo_volume | DOUBLE | YES | nan | nan | nan |
| finance_buy | DOUBLE | YES | nan | nan | nan |
| slo_sell_volume | DOUBLE | YES | nan | nan | nan |
| finance_repay | DOUBLE | YES | nan | nan | nan |
| slo_repay | DOUBLE | YES | nan | nan | nan |
| finance_net | DOUBLE | YES | nan | nan | nan |
| slo_net | DOUBLE | YES | nan | nan | nan |
| dt | BIGINT | YES | nan | nan | nan |

### 前 3 行

| symbol | time | finance_balance | slo_volume | finance_buy | slo_sell_volume | finance_repay | slo_repay | finance_net | slo_net | dt |
|---|---|---|---|---|---|---|---|---|---|---|
| 000410.SZ | 2016-01-04 00:00:00 | 180911.0 | 4000.0 | 6646.45 | 0.0 | 14364.74 | 0.0 | -7718.29 | 0.0 | 20160104 |
| 000422.SZ | 2016-01-04 00:00:00 | 67654.7 | 106100.0 | 2293.87 | 13300.0 | 4423.91 | 22300.0 | -2130.05 | -9000.0 | 20160104 |
| 000536.SZ | 2016-01-04 00:00:00 | 79478.67 | 400.0 | 3313.49 | 300.0 | 2147.31 | 0.0 | 1166.18 | 300.0 | 20160104 |

### 单位（来自 skills/quantdb-fields）

- `symbol`：后缀式 600519.SH
- `time`：时间戳
- `dt`：YYYYMMDD 整数

## sector_concept（板块概念）

- 类别：`2_base_sector` ｜ 布局：`single` ｜ 文件数：1 ｜ 体积：170.0 KB
- 行数：**78,890** ｜ 列数：**4** ｜ 扫描耗时：0.0s

### 列

| column_name | column_type | null | key | default | extra |
|---|---|---|---|---|---|
| SectorCode | VARCHAR | YES | nan | nan | nan |
| SectorName | VARCHAR | YES | nan | nan | nan |
| SectorType | VARCHAR | YES | nan | nan | nan |
| Symbol | VARCHAR | YES | nan | nan | nan |

### 前 3 行

| SectorCode | SectorName | SectorType | Symbol |
|---|---|---|---|
| 880081.SH | 轮动趋势 | 风格板块 | 511010.SH |
| 880081.SH | 轮动趋势 | 风格板块 | 511260.SH |
| 880082.SH | 板块趋势 | 风格板块 | 600188.SH |

## trading_calendar（交易日历）

- 类别：`2_base_sector` ｜ 布局：`single` ｜ 文件数：1 ｜ 体积：15.4 KB
- 行数：**2,672** ｜ 列数：**2** ｜ 扫描耗时：0.0s

### 列

| column_name | column_type | null | key | default | extra |
|---|---|---|---|---|---|
| TradingDate | VARCHAR | YES | nan | nan | nan |
| IsTradingDay | BIGINT | YES | nan | nan | nan |

### 前 3 行

| TradingDate | IsTradingDay |
|---|---|
| 20160104 | 1 |
| 20160105 | 1 |
| 20160106 | 1 |

---

## 总览

| 类别 | 表 | 中文 | 布局 | 文件数 | 体积 | 行数 | 列数 |
|---|---|---|---|---|---|---|---|
| 1_kline_data | daily_backward | 日线后复权 | partition | 2623 | 394.4 MB | 10826172 | 9 |
| 1_kline_data | daily_forward | 日线前复权 | partition | 2623 | 383.4 MB | 10826172 | 9 |
| 1_kline_data | daily_unadjusted | 日线不复权 | partition | 2623 | 223.8 MB | 10826172 | 9 |
| 1_kline_data | index_daily | 指数日线 | partition | 2623 | 8.1 MB | 114653 | 10 |
| 2_base_sector | index_weights | 指数权重 | single | 7 | 41.6 KB | 2800 | 3 |
| 2_base_sector | instrument_detail | 个股详情 | single | 1 | 3.0 MB | 5581 | 152 |
| 2_base_sector | margin_trading | 融资融券 | partition | 2622 | 136.8 MB | 5042454 | 11 |
| 2_base_sector | sector_concept | 板块概念 | single | 1 | 170.0 KB | 78890 | 4 |
| 2_base_sector | trading_calendar | 交易日历 | single | 1 | 15.4 KB | 2672 | 2 |
