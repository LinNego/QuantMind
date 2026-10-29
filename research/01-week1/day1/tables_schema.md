# QuantDB 数据字典（day1 自动生成）

- 生成时间：2026-10-29 21:27:40
- 数据目录：`/opt/quantmind/data/quantdb`
- 引擎：DuckDB 2.0.0
- 扫描表数：23

> 本文件由 `inspect_tables.py` 生成，只含结构（列名/类型/行数）与 3 行样本，不含分析结论。

## daily_backward（日线后复权）

- 类别：`1_kline_data` ｜ 布局：`partition` ｜ 文件数：2623 ｜ 体积：394.4 MB
- 行数：**10,826,172** ｜ 列数：**9** ｜ 扫描耗时：2.5s

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
- 行数：**10,826,172** ｜ 列数：**9** ｜ 扫描耗时：1.4s

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
- 行数：**114,653** ｜ 列数：**10** ｜ 扫描耗时：1.4s

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
- 行数：**5,042,454** ｜ 列数：**11** ｜ 扫描耗时：1.6s

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

## balance（资产负债表）

- 类别：`3_financial_data` ｜ 布局：`single` ｜ 文件数：5559 ｜ 体积：317.4 MB
- 行数：**191,900** ｜ 列数：**75** ｜ 扫描耗时：10.6s

### 列

| column_name | column_type | null | key | default | extra |
|---|---|---|---|---|---|
| m_timetag | VARCHAR | YES | nan | nan | nan |
| m_anntime | VARCHAR | YES | nan | nan | nan |
| cash_equivalents | DOUBLE | YES | nan | nan | nan |
| account_receivable | DOUBLE | YES | nan | nan | nan |
| bill_receivable | DOUBLE | YES | nan | nan | nan |
| advance_payment | DOUBLE | YES | nan | nan | nan |
| int_rcv | DOUBLE | YES | nan | nan | nan |
| other_receivable | DOUBLE | YES | nan | nan | nan |
| inventories | DOUBLE | YES | nan | nan | nan |
| other_current_assets | DOUBLE | YES | nan | nan | nan |
| total_current_assets | DOUBLE | YES | nan | nan | nan |
| fix_assets | DOUBLE | YES | nan | nan | nan |
| constru_in_process | DOUBLE | YES | nan | nan | nan |
| intang_assets | DOUBLE | YES | nan | nan | nan |
| goodwill | DOUBLE | YES | nan | nan | nan |
| long_deferred_expense | DOUBLE | YES | nan | nan | nan |
| deferred_tax_assets | DOUBLE | YES | nan | nan | nan |
| total_non_current_assets | DOUBLE | YES | nan | nan | nan |
| tot_assets | DOUBLE | YES | nan | nan | nan |
| shortterm_loan | DOUBLE | YES | nan | nan | nan |
| notes_payable | DOUBLE | YES | nan | nan | nan |
| accounts_payable | DOUBLE | YES | nan | nan | nan |
| advance_peceipts | DOUBLE | YES | nan | nan | nan |
| taxes_surcharges_payable | DOUBLE | YES | nan | nan | nan |
| other_payable | DOUBLE | YES | nan | nan | nan |
| non_current_liability_in_one_year | DOUBLE | YES | nan | nan | nan |
| total_current_liability | DOUBLE | YES | nan | nan | nan |
| long_term_loans | DOUBLE | YES | nan | nan | nan |
| bonds_payable | DOUBLE | YES | nan | nan | nan |
| deferred_tax_liab | DOUBLE | YES | nan | nan | nan |
| non_current_liabilities | DOUBLE | YES | nan | nan | nan |
| tot_liab | DOUBLE | YES | nan | nan | nan |
| cap_stk | DOUBLE | YES | nan | nan | nan |
| cap_rsrv | DOUBLE | YES | nan | nan | nan |
| surplus_rsrv | DOUBLE | YES | nan | nan | nan |
| undistributed_profit | DOUBLE | YES | nan | nan | nan |
| tot_shrhldr_eqy_excl_min_int | DOUBLE | YES | nan | nan | nan |
| minority_int | DOUBLE | YES | nan | nan | nan |
| total_equity | DOUBLE | YES | nan | nan | nan |
| tot_liab_shrhldr_eqy | DOUBLE | YES | nan | nan | nan |
| Symbol | VARCHAR | YES | nan | nan | nan |
| trading_financial_assets | DOUBLE | YES | nan | nan | nan |
| receivable_related_party | DOUBLE | YES | nan | nan | nan |
| dividend_receivable | DOUBLE | YES | nan | nan | nan |
| consumable_bio_assets | DOUBLE | YES | nan | nan | nan |
| non_current_assets_in_one_year | DOUBLE | YES | nan | nan | nan |
| available_for_sale_financial_assets | DOUBLE | YES | nan | nan | nan |
| held_to_maturity_investments | DOUBLE | YES | nan | nan | nan |
| long_term_receivables | DOUBLE | YES | nan | nan | nan |
| long_term_equity_investments | DOUBLE | YES | nan | nan | nan |
| investment_property | DOUBLE | YES | nan | nan | nan |
| engineering_materials | DOUBLE | YES | nan | nan | nan |
| fixed_assets_disposal | DOUBLE | YES | nan | nan | nan |
| productive_bio_assets | DOUBLE | YES | nan | nan | nan |
| oil_and_gas_assets | DOUBLE | YES | nan | nan | nan |
| development_expenditure | DOUBLE | YES | nan | nan | nan |
| other_non_current_assets | DOUBLE | YES | nan | nan | nan |
| trading_financial_liabilities | DOUBLE | YES | nan | nan | nan |
| employee_payable | DOUBLE | YES | nan | nan | nan |
| interest_payable | DOUBLE | YES | nan | nan | nan |
| dividend_payable | DOUBLE | YES | nan | nan | nan |
| payable_related_party | DOUBLE | YES | nan | nan | nan |
| other_current_liabilities | DOUBLE | YES | nan | nan | nan |
| long_term_payable | DOUBLE | YES | nan | nan | nan |
| special_payable | DOUBLE | YES | nan | nan | nan |
| estimated_liabilities | DOUBLE | YES | nan | nan | nan |
| other_non_current_liabilities | DOUBLE | YES | nan | nan | nan |
| treasury_stock | DOUBLE | YES | nan | nan | nan |
| foreign_currency_translation_diff | DOUBLE | YES | nan | nan | nan |
| non_recurring_income_adjustment | DOUBLE | YES | nan | nan | nan |
| notes_and_accounts_payable | DOUBLE | YES | nan | nan | nan |
| bills_and_accounts_receivable | DOUBLE | YES | nan | nan | nan |
| deferred_income | DOUBLE | YES | nan | nan | nan |
| other_comprehensive_income_bs | DOUBLE | YES | nan | nan | nan |
| other_equity_instruments | DOUBLE | YES | nan | nan | nan |

### 前 3 行

| m_timetag | m_anntime | cash_equivalents | account_receivable | bill_receivable | advance_payment | int_rcv | other_receivable | inventories | other_current_assets | total_current_assets | fix_assets | constru_in_process | intang_assets | goodwill | long_deferred_expense | deferred_tax_assets | total_non_current_assets | tot_assets | shortterm_loan | notes_payable | accounts_payable | advance_peceipts | taxes_surcharges_payable | other_payable | non_current_liability_in_one_year | total_current_liability | long_term_loans | bonds_payable | deferred_tax_liab | non_current_liabilities | tot_liab | cap_stk | cap_rsrv | surplus_rsrv | undistributed_profit | tot_shrhldr_eqy_excl_min_int | minority_int | total_equity | tot_liab_shrhldr_eqy | Symbol | trading_financial_assets | receivable_related_party | dividend_receivable | consumable_bio_assets | non_current_assets_in_one_year | available_for_sale_financial_assets | held_to_maturity_investments | long_term_receivables | long_term_equity_investments | investment_property | engineering_materials | fixed_assets_disposal | productive_bio_assets | oil_and_gas_assets | development_expenditure | other_non_current_assets | trading_financial_liabilities | employee_payable | interest_payable | dividend_payable | payable_related_party | other_current_liabilities | long_term_payable | special_payable | estimated_liabilities | other_non_current_liabilities | treasury_stock | foreign_currency_translation_diff | non_recurring_income_adjustment | notes_and_accounts_payable | bills_and_accounts_receivable | deferred_income | other_comprehensive_income_bs | other_equity_instruments |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 20160331 | 20160421 | 477015015424.0 | 0.0 | 0.0 | 0.0 | 14410000384.0 | 0.0 | 0.0 | 0.0 | 0.0 | 4649999872.0 | 0.0 | 4838000128.0 | 0.0 | 0.0 | 9731999744.0 | 0.0 | 2681155092480.0 | 0.0 | 0.0 | 191000000.0 | 0.0 | 8752000000.0 | 0.0 | 0.0 | 0.0 | 0.0 | 247359995904.0 | 0.0 | 0.0 | 2493586079744.0 | 14309000192.0 | 59326001152.0 | 8520999936.0 | 59019001856.0 | 187568996352.0 | 0.0 | 187568996352.0 | 2681155092480.0 | 000001.SZ | 37060001792.0 | 0.0 | 0.0 | 0.0 | 0.0 | 2745999872.0 | 289276002304.0 | 0.0 | 521000000.0 | 229000000.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 8374000128.0 | 22705999872.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 25000000.0 | 0.0 | 0.0 | 0.0 | 0.0 | 191000000.0 | 0.0 | 0.0 | 0.0 | 0.0 |
| 20160630 | 20160812 | 436307984384.0 | 0.0 | 0.0 | 0.0 | 14115999744.0 | 0.0 | 0.0 | 0.0 | 0.0 | 5310000128.0 | 0.0 | 4741000192.0 | 0.0 | 0.0 | 11731999744.0 | 0.0 | 2800982949888.0 | 0.0 | 0.0 | 3000000.0 | 0.0 | 6490999808.0 | 0.0 | 0.0 | 0.0 | 0.0 | 307603013632.0 | 0.0 | 0.0 | 2609283072000.0 | 17169999872.0 | 56464998400.0 | 8520999936.0 | 63035998208.0 | 191699992576.0 | 0.0 | 191699992576.0 | 2800982949888.0 | 000001.SZ | 41147998208.0 | 0.0 | 0.0 | 0.0 | 0.0 | 1280000000.0 | 284598009856.0 | 0.0 | 542000000.0 | 228000000.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 22738999296.0 | 9900999680.0 | 20714999808.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 25000000.0 | 0.0 | 0.0 | 0.0 | 0.0 | 3000000.0 | 0.0 | 0.0 | -973000000.0 | 19953000448.0 |
| 20160930 | 20161021 | 427013996544.0 | 0.0 | 0.0 | 0.0 | 13470999552.0 | 0.0 | 0.0 | 0.0 | 0.0 | 7407000064.0 | 0.0 | 4631000064.0 | 0.0 | 0.0 | 13361000448.0 | 0.0 | 2791238008832.0 | 0.0 | 0.0 | 0.0 | 0.0 | 9240999936.0 | 0.0 | 0.0 | 0.0 | 0.0 | 325419008000.0 | 0.0 | 0.0 | 2593009958912.0 | 17169999872.0 | 56464998400.0 | 8520999936.0 | 69462999040.0 | 198228000768.0 | 0.0 | 198228000768.0 | 2791238008832.0 | 000001.SZ | 15984000000.0 | 0.0 | 0.0 | 0.0 | 0.0 | 1502000000.0 | 282861993984.0 | 0.0 | 542000000.0 | 211000000.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 20585000960.0 | 10648000512.0 | 21151000576.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 24000000.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | -872000000.0 | 19953000448.0 |

## capital（股本结构）

- 类别：`3_financial_data` ｜ 布局：`single` ｜ 文件数：5559 ｜ 体积：43.3 MB
- 行数：**308,841** ｜ 列数：**10** ｜ 扫描耗时：2.9s

### 列

| column_name | column_type | null | key | default | extra |
|---|---|---|---|---|---|
| m_timetag | VARCHAR | YES | nan | nan | nan |
| m_anntime | VARCHAR | YES | nan | nan | nan |
| total_capital | DOUBLE | YES | nan | nan | nan |
| circulating_capital | DOUBLE | YES | nan | nan | nan |
| restrict_circulating_capital | DOUBLE | YES | nan | nan | nan |
| freeFloatCapital | DOUBLE | YES | nan | nan | nan |
| Symbol | VARCHAR | YES | nan | nan | nan |
| circulating_b_shares | DOUBLE | YES | nan | nan | nan |
| circulating_h_shares | DOUBLE | YES | nan | nan | nan |
| free_float_shares | DOUBLE | YES | nan | nan | nan |

### 前 3 行

| m_timetag | m_anntime | total_capital | circulating_capital | restrict_circulating_capital | freeFloatCapital | Symbol | circulating_b_shares | circulating_h_shares | free_float_shares |
|---|---|---|---|---|---|---|---|---|---|
| 20160331 | 20160421 | 14308676608.0 | 11804054528.0 | 2504621568.0 | 5953305743.0 | 000001.SZ | 0.0 | 0.0 | 5953305600.0 |
| 20160520 | 20160520 | 14308676139.0 | 11804054580.0 | 0.0 | 5953305744.0 | 000001.SZ | nan | nan | nan |
| 20160523 | 20160520 | 14308676139.0 | 12192650323.0 | 0.0 | 6341901487.0 | 000001.SZ | nan | nan | nan |

## cashflow（现金流量表）

- 类别：`3_financial_data` ｜ 布局：`single` ｜ 文件数：5559 ｜ 体积：308.7 MB
- 行数：**196,961** ｜ 列数：**68** ｜ 扫描耗时：10.9s

### 列

| column_name | column_type | null | key | default | extra |
|---|---|---|---|---|---|
| m_timetag | VARCHAR | YES | nan | nan | nan |
| m_anntime | VARCHAR | YES | nan | nan | nan |
| goods_sale_and_service_render_cash | DOUBLE | YES | nan | nan | nan |
| other_cash_recp_ral_oper_act | DOUBLE | YES | nan | nan | nan |
| stot_cash_inflows_oper_act | DOUBLE | YES | nan | nan | nan |
| goods_and_services_cash_paid | DOUBLE | YES | nan | nan | nan |
| cash_pay_beh_empl | DOUBLE | YES | nan | nan | nan |
| pay_all_typ_tax | DOUBLE | YES | nan | nan | nan |
| other_cash_pay_ral_oper_act | DOUBLE | YES | nan | nan | nan |
| stot_cash_outflows_oper_act | DOUBLE | YES | nan | nan | nan |
| net_cash_flows_oper_act | DOUBLE | YES | nan | nan | nan |
| cash_recp_disp_withdrwl_invest | DOUBLE | YES | nan | nan | nan |
| cash_recp_return_invest | DOUBLE | YES | nan | nan | nan |
| stot_cash_inflows_inv_act | DOUBLE | YES | nan | nan | nan |
| cash_pay_acq_const_fiolta | DOUBLE | YES | nan | nan | nan |
| cash_paid_for_investments | DOUBLE | YES | nan | nan | nan |
| stot_cash_outflows_inv_act | DOUBLE | YES | nan | nan | nan |
| net_cash_flows_inv_act | DOUBLE | YES | nan | nan | nan |
| cash_recp_cap_contrib | DOUBLE | YES | nan | nan | nan |
| cash_recp_borrow | DOUBLE | YES | nan | nan | nan |
| stot_cash_inflows_fnc_act | DOUBLE | YES | nan | nan | nan |
| cash_prepay_amt_borr | DOUBLE | YES | nan | nan | nan |
| cash_pay_dist_dpcp_int_exp | DOUBLE | YES | nan | nan | nan |
| stot_cash_outflows_fnc_act | DOUBLE | YES | nan | nan | nan |
| net_cash_flows_fnc_act | DOUBLE | YES | nan | nan | nan |
| eff_fx_flu_cash | DOUBLE | YES | nan | nan | nan |
| net_incr_cash_cash_equ | DOUBLE | YES | nan | nan | nan |
| cash_cash_equ_beg_period | DOUBLE | YES | nan | nan | nan |
| cash_cash_equ_end_period | DOUBLE | YES | nan | nan | nan |
| Symbol | VARCHAR | YES | nan | nan | nan |
| tax_refund_received | DOUBLE | YES | nan | nan | nan |
| cash_recp_disp_fixed_assets | DOUBLE | YES | nan | nan | nan |
| cash_recp_disp_subsidiary | DOUBLE | YES | nan | nan | nan |
| other_cash_recp_inv_act | DOUBLE | YES | nan | nan | nan |
| cash_pay_acq_subsidiary | DOUBLE | YES | nan | nan | nan |
| other_cash_pay_inv_act | DOUBLE | YES | nan | nan | nan |
| other_cash_recp_fnc_act | DOUBLE | YES | nan | nan | nan |
| other_cash_pay_fnc_act | DOUBLE | YES | nan | nan | nan |
| other_cash_effect | DOUBLE | YES | nan | nan | nan |
| net_profit_adj | DOUBLE | YES | nan | nan | nan |
| asset_impairment_provision | DOUBLE | YES | nan | nan | nan |
| fixed_asset_depreciation | DOUBLE | YES | nan | nan | nan |
| intangible_asset_amortization | DOUBLE | YES | nan | nan | nan |
| long_term_deferred_amortization | DOUBLE | YES | nan | nan | nan |
| loss_on_disposal_of_assets | DOUBLE | YES | nan | nan | nan |
| fixed_asset_scrap_loss | DOUBLE | YES | nan | nan | nan |
| fair_value_change_loss | DOUBLE | YES | nan | nan | nan |
| financial_expense | DOUBLE | YES | nan | nan | nan |
| investment_loss | DOUBLE | YES | nan | nan | nan |
| deferred_tax_asset_decrease | DOUBLE | YES | nan | nan | nan |
| deferred_tax_liability_increase | DOUBLE | YES | nan | nan | nan |
| inventory_decrease | DOUBLE | YES | nan | nan | nan |
| operating_receivables_decrease | DOUBLE | YES | nan | nan | nan |
| operating_payables_increase | DOUBLE | YES | nan | nan | nan |
| other_adjustments | DOUBLE | YES | nan | nan | nan |
| net_cash_flows_oper_act_2 | DOUBLE | YES | nan | nan | nan |
| debt_to_equity | DOUBLE | YES | nan | nan | nan |
| convertible_bond_in_one_year | DOUBLE | YES | nan | nan | nan |
| finance_lease_fixed_assets | DOUBLE | YES | nan | nan | nan |
| cash_end_balance | DOUBLE | YES | nan | nan | nan |
| less_cash_begin_balance | DOUBLE | YES | nan | nan | nan |
| add_cash_equiv_end_balance | DOUBLE | YES | nan | nan | nan |
| less_cash_equiv_begin_balance | DOUBLE | YES | nan | nan | nan |
| net_incr_cash_cash_equiv_adj | DOUBLE | YES | nan | nan | nan |
| operating_cash_flow_income | DOUBLE | YES | nan | nan | nan |
| investing_cash_flow_income | DOUBLE | YES | nan | nan | nan |
| financing_cash_flow_income | DOUBLE | YES | nan | nan | nan |
| net_cash_flow_income | DOUBLE | YES | nan | nan | nan |

### 前 3 行

| m_timetag | m_anntime | goods_sale_and_service_render_cash | other_cash_recp_ral_oper_act | stot_cash_inflows_oper_act | goods_and_services_cash_paid | cash_pay_beh_empl | pay_all_typ_tax | other_cash_pay_ral_oper_act | stot_cash_outflows_oper_act | net_cash_flows_oper_act | cash_recp_disp_withdrwl_invest | cash_recp_return_invest | stot_cash_inflows_inv_act | cash_pay_acq_const_fiolta | cash_paid_for_investments | stot_cash_outflows_inv_act | net_cash_flows_inv_act | cash_recp_cap_contrib | cash_recp_borrow | stot_cash_inflows_fnc_act | cash_prepay_amt_borr | cash_pay_dist_dpcp_int_exp | stot_cash_outflows_fnc_act | net_cash_flows_fnc_act | eff_fx_flu_cash | net_incr_cash_cash_equ | cash_cash_equ_beg_period | cash_cash_equ_end_period | Symbol | tax_refund_received | cash_recp_disp_fixed_assets | cash_recp_disp_subsidiary | other_cash_recp_inv_act | cash_pay_acq_subsidiary | other_cash_pay_inv_act | other_cash_recp_fnc_act | other_cash_pay_fnc_act | other_cash_effect | net_profit_adj | asset_impairment_provision | fixed_asset_depreciation | intangible_asset_amortization | long_term_deferred_amortization | loss_on_disposal_of_assets | fixed_asset_scrap_loss | fair_value_change_loss | financial_expense | investment_loss | deferred_tax_asset_decrease | deferred_tax_liability_increase | inventory_decrease | operating_receivables_decrease | operating_payables_increase | other_adjustments | net_cash_flows_oper_act_2 | debt_to_equity | convertible_bond_in_one_year | finance_lease_fixed_assets | cash_end_balance | less_cash_begin_balance | add_cash_equiv_end_balance | less_cash_equiv_begin_balance | net_incr_cash_cash_equiv_adj | operating_cash_flow_income | investing_cash_flow_income | financing_cash_flow_income | net_cash_flow_income |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 20160331 | 20160421 | 0.0 | 18322999296.0 | 159508004864.0 | 0.0 | 5904000000.0 | 2688999936.0 | 25202999296.0 | 100239998976.0 | 59268001792.0 | 409509003264.0 | 7218999808.0 | 416727990272.0 | 236000000.0 | 511273009152.0 | 511509004288.0 | -94780997632.0 | 0.0 | 0.0 | 187420000256.0 | 134880002048.0 | 612000000.0 | 135539998720.0 | 51880001536.0 | -171000000.0 | 16195999744.0 | 261341003776.0 | 277536997376.0 | 000001.SZ | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 20000000000.0 | 48000000.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 59268001792.0 | -94780997632.0 | 51880001536.0 | 16195999744.0 |
| 20160630 | 20160812 | 0.0 | 21787000832.0 | 237589004288.0 | 0.0 | 8384000000.0 | 9884999680.0 | 52114001920.0 | 270787002368.0 | -33198000128.0 | 994033991680.0 | 14069999616.0 | 1008103981056.0 | 600000000.0 | 1131404984320.0 | 1132005031936.0 | -123901001728.0 | 0.0 | 0.0 | 440453005312.0 | 0.0 | 2188999936.0 | 332997984256.0 | 107454996480.0 | 1448000000.0 | -48196001792.0 | 261341003776.0 | 213144993792.0 | 000001.SZ | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 20000000000.0 | 330809016320.0 | 0.0 | 12291999744.0 | 20000000000.0 | 395000000.0 | 285000000.0 | 226000000.0 | 5000000.0 | 5000000.0 | 0.0 | 0.0 | 0.0 | 0.0 | -3052999936.0 | 0.0 | -214419996672.0 | 154093993984.0 | -3027000064.0 | -33198000128.0 | 0.0 | 0.0 | 0.0 | 3593999872.0 | 4119000064.0 | 209550999552.0 | 257222000640.0 | -48196001792.0 | -92466003968.0 | -29120000000.0 | 55574999040.0 | -64391999488.0 |
| 20160930 | 20161021 | 0.0 | 36665999360.0 | 257971994624.0 | 0.0 | 11059999744.0 | 11059000320.0 | 53096001536.0 | 410913996800.0 | -152942002176.0 | 1626196934656.0 | 21756999680.0 | 1647953969152.0 | 906000000.0 | 1744209969152.0 | 1745115938816.0 | -97162002432.0 | 0.0 | 0.0 | 629763997696.0 | 503189995520.0 | 2188999936.0 | 506796015616.0 | 122967998464.0 | 1546000000.0 | -125590003712.0 | 261341003776.0 | 135751000064.0 | 000001.SZ | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 20000000000.0 | 1416999936.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | -119744004096.0 | 26738999296.0 | 15512999936.0 | -77394001920.0 |

## dividend_factors（分红送转因子）

- 类别：`3_financial_data` ｜ 布局：`single` ｜ 文件数：5451 ｜ 体积：24.1 MB
- 行数：**58,933** ｜ 列数：**11** ｜ 扫描耗时：2.1s

### 列

| column_name | column_type | null | key | default | extra |
|---|---|---|---|---|---|
| time | TIMESTAMP_NS | YES | nan | nan | nan |
| type | VARCHAR | YES | nan | nan | nan |
| interest | DOUBLE | YES | nan | nan | nan |
| allotPrice | DOUBLE | YES | nan | nan | nan |
| stockBonus | DOUBLE | YES | nan | nan | nan |
| allotment | DOUBLE | YES | nan | nan | nan |
| Symbol | VARCHAR | YES | nan | nan | nan |
| stockGift | DOUBLE | YES | nan | nan | nan |
| allotNum | DOUBLE | YES | nan | nan | nan |
| gugai | DOUBLE | YES | nan | nan | nan |
| dr | DOUBLE | YES | nan | nan | nan |

### 前 3 行

| time | type | interest | allotPrice | stockBonus | allotment | Symbol | stockGift | allotNum | gugai | dr |
|---|---|---|---|---|---|---|---|---|---|---|
| 1990-03-01 00:00:00 | 1 | 0.0 | 3.56 | 0.0 | 1.0 | 000001.SZ | nan | nan | nan | nan |
| 1991-05-02 00:00:00 | 1 | 3.0 | 0.0 | 4.0 | 0.0 | 000001.SZ | nan | nan | nan | nan |
| 1991-08-17 00:00:00 | 1 | 0.0 | 0.0 | 10.0 | 0.0 | 000001.SZ | nan | nan | nan | nan |

### 单位（来自 skills/quantdb-fields）

- `time`：时间戳

## holder_num（股东户数）

- 类别：`3_financial_data` ｜ 布局：`single` ｜ 文件数：5559 ｜ 体积：144.8 MB
- 行数：**191,533** ｜ 列数：**38** ｜ 扫描耗时：6.3s

### 列

| column_name | column_type | null | key | default | extra |
|---|---|---|---|---|---|
| declareDate | VARCHAR | YES | nan | nan | nan |
| endDate | VARCHAR | YES | nan | nan | nan |
| Symbol | VARCHAR | YES | nan | nan | nan |
| m_timetag | VARCHAR | YES | nan | nan | nan |
| m_anntime | VARCHAR | YES | nan | nan | nan |
| holder_num | DOUBLE | YES | nan | nan | nan |
| top1_holder_shares | DOUBLE | YES | nan | nan | nan |
| top10_float_holder_shares | DOUBLE | YES | nan | nan | nan |
| top10_holder_shares | DOUBLE | YES | nan | nan | nan |
| institution_count | DOUBLE | YES | nan | nan | nan |
| institution_holdings | DOUBLE | YES | nan | nan | nan |
| qfii_institution_count | DOUBLE | YES | nan | nan | nan |
| qfii_holdings | DOUBLE | YES | nan | nan | nan |
| broker_institution_count | DOUBLE | YES | nan | nan | nan |
| broker_holdings | DOUBLE | YES | nan | nan | nan |
| insurance_institution_count | DOUBLE | YES | nan | nan | nan |
| insurance_holdings | DOUBLE | YES | nan | nan | nan |
| fund_institution_count | DOUBLE | YES | nan | nan | nan |
| fund_holdings | DOUBLE | YES | nan | nan | nan |
| social_security_count | DOUBLE | YES | nan | nan | nan |
| social_security_holdings | DOUBLE | YES | nan | nan | nan |
| private_equity_count | DOUBLE | YES | nan | nan | nan |
| private_equity_holdings | DOUBLE | YES | nan | nan | nan |
| finance_company_count | DOUBLE | YES | nan | nan | nan |
| finance_company_holdings | DOUBLE | YES | nan | nan | nan |
| annuity_count | DOUBLE | YES | nan | nan | nan |
| annuity_holdings | DOUBLE | YES | nan | nan | nan |
| top10_float_holder_a_shares | DOUBLE | YES | nan | nan | nan |
| top1_float_holder_shares | DOUBLE | YES | nan | nan | nan |
| bank_institution_count | DOUBLE | YES | nan | nan | nan |
| bank_holdings | DOUBLE | YES | nan | nan | nan |
| general_legal_person_count | DOUBLE | YES | nan | nan | nan |
| general_legal_person_holdings | DOUBLE | YES | nan | nan | nan |
| trust_institution_count | DOUBLE | YES | nan | nan | nan |
| trust_holdings | DOUBLE | YES | nan | nan | nan |
| special_legal_person_count | DOUBLE | YES | nan | nan | nan |
| special_legal_person_holdings | DOUBLE | YES | nan | nan | nan |
| national_team_holdings | DOUBLE | YES | nan | nan | nan |

### 前 3 行

| declareDate | endDate | Symbol | m_timetag | m_anntime | holder_num | top1_holder_shares | top10_float_holder_shares | top10_holder_shares | institution_count | institution_holdings | qfii_institution_count | qfii_holdings | broker_institution_count | broker_holdings | insurance_institution_count | insurance_holdings | fund_institution_count | fund_holdings | social_security_count | social_security_holdings | private_equity_count | private_equity_holdings | finance_company_count | finance_company_holdings | annuity_count | annuity_holdings | top10_float_holder_a_shares | top1_float_holder_shares | bank_institution_count | bank_holdings | general_legal_person_count | general_legal_person_holdings | trust_institution_count | trust_holdings | special_legal_person_count | special_legal_person_holdings | national_team_holdings |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
|  |  | 000001.SZ | 20160331 | 20160421 | 341525.0 | 7092077568.0 | 7226396160.0 | 9379768320.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 7226396160.0 | 4976196608.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 591096200.0 |
|  |  | 000001.SZ | 20160630 | 20160812 | 340528.0 | 8510493184.0 | 8668601344.0 | 11207658496.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 8668601344.0 | 5971436032.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 759314900.0 |
|  |  | 000001.SZ | 20160930 | 20161021 | 340324.0 | 8510493184.0 | 8680253440.0 | 11219311616.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 8680253440.0 | 5971436032.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 695583800.0 |

## income（利润表）

- 类别：`3_financial_data` ｜ 布局：`single` ｜ 文件数：5559 ｜ 体积：179.3 MB
- 行数：**197,727** ｜ 列数：**37** ｜ 扫描耗时：6.2s

### 列

| column_name | column_type | null | key | default | extra |
|---|---|---|---|---|---|
| m_timetag | VARCHAR | YES | nan | nan | nan |
| m_anntime | VARCHAR | YES | nan | nan | nan |
| revenue | DOUBLE | YES | nan | nan | nan |
| total_operating_cost | DOUBLE | YES | nan | nan | nan |
| less_taxes_surcharges_ops | DOUBLE | YES | nan | nan | nan |
| sale_expense | DOUBLE | YES | nan | nan | nan |
| less_gerl_admin_exp | DOUBLE | YES | nan | nan | nan |
| research_expenses | DOUBLE | YES | nan | nan | nan |
| financial_expense | DOUBLE | YES | nan | nan | nan |
| less_impair_loss_assets | DOUBLE | YES | nan | nan | nan |
| plus_net_invest_inc | DOUBLE | YES | nan | nan | nan |
| incl_inc_invest_assoc_jv_entp | DOUBLE | YES | nan | nan | nan |
| oper_profit | DOUBLE | YES | nan | nan | nan |
| plus_non_oper_rev | DOUBLE | YES | nan | nan | nan |
| less_non_oper_exp | DOUBLE | YES | nan | nan | nan |
| tot_profit | DOUBLE | YES | nan | nan | nan |
| inc_tax | DOUBLE | YES | nan | nan | nan |
| net_profit_incl_min_int_inc | DOUBLE | YES | nan | nan | nan |
| net_profit_excl_min_int_inc | DOUBLE | YES | nan | nan | nan |
| net_profit_incl_min_int_inc_after | DOUBLE | YES | nan | nan | nan |
| minority_int_inc | DOUBLE | YES | nan | nan | nan |
| s_fa_eps_basic | DOUBLE | YES | nan | nan | nan |
| s_fa_eps_diluted | DOUBLE | YES | nan | nan | nan |
| other_compreh_inc | DOUBLE | YES | nan | nan | nan |
| tot_compreh_inc | DOUBLE | YES | nan | nan | nan |
| total_income | DOUBLE | YES | nan | nan | nan |
| actual_ann_dt | DOUBLE | YES | nan | nan | nan |
| Symbol | VARCHAR | YES | nan | nan | nan |
| deducted_net_profit | DOUBLE | YES | nan | nan | nan |
| other_income | DOUBLE | YES | nan | nan | nan |
| asset_disposal_income | DOUBLE | YES | nan | nan | nan |
| continuing_operations_net_profit | DOUBLE | YES | nan | nan | nan |
| discontinued_operations_net_profit | DOUBLE | YES | nan | nan | nan |
| interest_expense | DOUBLE | YES | nan | nan | nan |
| interest_income | DOUBLE | YES | nan | nan | nan |
| s_fa_eps_basic_q | DOUBLE | YES | nan | nan | nan |
| revenue_q | DOUBLE | YES | nan | nan | nan |

### 前 3 行

| m_timetag | m_anntime | revenue | total_operating_cost | less_taxes_surcharges_ops | sale_expense | less_gerl_admin_exp | research_expenses | financial_expense | less_impair_loss_assets | plus_net_invest_inc | incl_inc_invest_assoc_jv_entp | oper_profit | plus_non_oper_rev | less_non_oper_exp | tot_profit | inc_tax | net_profit_incl_min_int_inc | net_profit_excl_min_int_inc | net_profit_incl_min_int_inc_after | minority_int_inc | s_fa_eps_basic | s_fa_eps_diluted | other_compreh_inc | tot_compreh_inc | total_income | actual_ann_dt | Symbol | deducted_net_profit | other_income | asset_disposal_income | continuing_operations_net_profit | discontinued_operations_net_profit | interest_expense | interest_income | s_fa_eps_basic_q | revenue_q |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 20160331 | 20160421 | 27531999232.0 | 19518000000.0 | 1888000000.0 | nan | nan | 0.0 | nan | 9550000000.0 | 824000000.0 | nan | 8014000128.0 | 18000000.0 | 3000000.0 | 8029000000.0 | 1943000000.0 | 6086000000.0 | 6086000128.0 | 6075000000.0 | nan | 0.35 | 0.35 | 30000000.0 | 6115999744.0 | 6116000000.0 | nan | 000001.SZ | 6074767872.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.43 | 27532000000.0 |
| 20160630 | 20160812 | 27236999168.0 | 38613000000.0 | 2839000000.0 | nan | nan | 0.0 | nan | 20000000000.0 | 1608000000.0 | 25000000.0 | 8142000128.0 | 29000000.0 | 31000000.0 | 16154000000.0 | 3862000000.0 | 12292000000.0 | 6206000128.0 | 12294000000.0 | nan | 0.72 | 0.72 | 144000000.0 | 12435999744.0 | 12436000000.0 | nan | 000001.SZ | 6219231744.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.29 | 27237000000.0 |
| 20160930 | 20161021 | 27199000576.0 | 57423000000.0 | 3100000000.0 | nan | nan | 0.0 | nan | 31615000000.0 | 2466000000.0 | nan | 8389000192.0 | 52000000.0 | 37000000.0 | 24560000000.0 | 5841000000.0 | 18719000000.0 | 6426999808.0 | 18707000000.0 | nan | 1.09 | 1.09 | 245000000.0 | 18964000768.0 | 18964000000.0 | nan | 000001.SZ | 6413000192.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.37 | 27199000000.0 |

## per_share（每股指标TTM）

- 类别：`3_financial_data` ｜ 布局：`single` ｜ 文件数：5558 ｜ 体积：47.1 MB
- 行数：**191,467** ｜ 列数：**10** ｜ 扫描耗时：2.7s

### 列

| column_name | column_type | null | key | default | extra |
|---|---|---|---|---|---|
| m_timetag | VARCHAR | YES | nan | nan | nan |
| m_anntime | VARCHAR | YES | nan | nan | nan |
| Symbol | VARCHAR | YES | nan | nan | nan |
| net_profit_ttm | DOUBLE | YES | nan | nan | nan |
| revenue_ttm | DOUBLE | YES | nan | nan | nan |
| operating_cf_ttm | DOUBLE | YES | nan | nan | nan |
| net_profit_parent_ttm | DOUBLE | YES | nan | nan | nan |
| deducted_net_profit_ttm | DOUBLE | YES | nan | nan | nan |
| net_cash_flow_ttm | DOUBLE | YES | nan | nan | nan |
| investing_cf_ttm | DOUBLE | YES | nan | nan | nan |

### 前 3 行

| m_timetag | m_anntime | Symbol | net_profit_ttm | revenue_ttm | operating_cf_ttm | net_profit_parent_ttm | deducted_net_profit_ttm | net_cash_flow_ttm | investing_cf_ttm |
|---|---|---|---|---|---|---|---|---|---|
| 20160331 | 20160421 | 000001.SZ | 22321999872.0 | 103024000000.0 | 45946998784.0 | 22322000000.0 | 22340647500.0 | 70499000000.0 | -168237000000.0 |
| 20160630 | 20160812 | 000001.SZ | 22571999232.0 | 104357000000.0 | -217545998336.0 | 22572000000.0 | 22605120000.0 | -174440000000.0 | -170214000000.0 |
| 20160930 | 20161021 | 000001.SZ | 22844000256.0 | 106979000000.0 | -246753001472.0 | 22844000000.0 | 22855752500.0 | -264109000000.0 | -158627000000.0 |

## pershare_index（每股财务指标）

- 类别：`3_financial_data` ｜ 布局：`single` ｜ 文件数：5559 ｜ 体积：416.5 MB
- 行数：**193,749** ｜ 列数：**86** ｜ 扫描耗时：13.6s

### 列

| column_name | column_type | null | key | default | extra |
|---|---|---|---|---|---|
| m_timetag | VARCHAR | YES | nan | nan | nan |
| m_anntime | VARCHAR | YES | nan | nan | nan |
| s_fa_ocfps | DOUBLE | YES | nan | nan | nan |
| s_fa_bps | DOUBLE | YES | nan | nan | nan |
| s_fa_eps_basic | DOUBLE | YES | nan | nan | nan |
| s_fa_eps_diluted | DOUBLE | YES | nan | nan | nan |
| s_fa_undistributedps | DOUBLE | YES | nan | nan | nan |
| s_fa_surpluscapitalps | DOUBLE | YES | nan | nan | nan |
| adjusted_earnings_per_share | DOUBLE | YES | nan | nan | nan |
| du_return_on_equity | DOUBLE | YES | nan | nan | nan |
| inc_revenue_rate | DOUBLE | YES | nan | nan | nan |
| du_profit_rate | DOUBLE | YES | nan | nan | nan |
| inc_net_profit_rate | DOUBLE | YES | nan | nan | nan |
| adjusted_net_profit_rate | DOUBLE | YES | nan | nan | nan |
| equity_roe | DOUBLE | YES | nan | nan | nan |
| net_roe | DOUBLE | YES | nan | nan | nan |
| net_profit | DOUBLE | YES | nan | nan | nan |
| gear_ratio | DOUBLE | YES | nan | nan | nan |
| inventory_turnover | DOUBLE | YES | nan | nan | nan |
| s_fa_cfps | DOUBLE | YES | nan | nan | nan |
| Symbol | VARCHAR | YES | nan | nan | nan |
| s_fa_eps_deducted | DOUBLE | YES | nan | nan | nan |
| s_fa_weighted_roe | DOUBLE | YES | nan | nan | nan |
| s_fa_eps_deducted_q | DOUBLE | YES | nan | nan | nan |
| inc_oper_profit_rate | DOUBLE | YES | nan | nan | nan |
| inc_deducted_eps_rate | DOUBLE | YES | nan | nan | nan |
| inc_deducted_net_profit_rate | DOUBLE | YES | nan | nan | nan |
| cost_expense_profit_rate | DOUBLE | YES | nan | nan | nan |
| operating_profit_margin | DOUBLE | YES | nan | nan | nan |
| tax_rate_operating | DOUBLE | YES | nan | nan | nan |
| cost_rate_operating | DOUBLE | YES | nan | nan | nan |
| roe_dupont | DOUBLE | YES | nan | nan | nan |
| return_on_investment | DOUBLE | YES | nan | nan | nan |
| net_profit_margin | DOUBLE | YES | nan | nan | nan |
| roa | DOUBLE | YES | nan | nan | nan |
| net_margin | DOUBLE | YES | nan | nan | nan |
| gross_margin | DOUBLE | YES | nan | nan | nan |
| three_fee_ratio | DOUBLE | YES | nan | nan | nan |
| admin_expense_ratio | DOUBLE | YES | nan | nan | nan |
| financial_expense_ratio | DOUBLE | YES | nan | nan | nan |
| deducted_net_profit_2 | DOUBLE | YES | nan | nan | nan |
| ebit | DOUBLE | YES | nan | nan | nan |
| ebitda | DOUBLE | YES | nan | nan | nan |
| ebitda_to_revenue | DOUBLE | YES | nan | nan | nan |
| s_fa_net_cfps | DOUBLE | YES | nan | nan | nan |
| operating_cash_flow_to_net_profit | DOUBLE | YES | nan | nan | nan |
| inc_equity_rate | DOUBLE | YES | nan | nan | nan |
| inc_fixed_asset_rate | DOUBLE | YES | nan | nan | nan |
| inc_total_asset_rate | DOUBLE | YES | nan | nan | nan |
| inc_investment_income_rate | DOUBLE | YES | nan | nan | nan |
| current_ratio | DOUBLE | YES | nan | nan | nan |
| quick_ratio | DOUBLE | YES | nan | nan | nan |
| cash_ratio | DOUBLE | YES | nan | nan | nan |
| interest_coverage | DOUBLE | YES | nan | nan | nan |
| tangible_asset_debt_ratio | DOUBLE | YES | nan | nan | nan |
| equity_multiplier | DOUBLE | YES | nan | nan | nan |
| equity_to_liability | DOUBLE | YES | nan | nan | nan |
| tangible_asset_to_liability | DOUBLE | YES | nan | nan | nan |
| operating_cf_to_liability | DOUBLE | YES | nan | nan | nan |
| ebitda_to_liability | DOUBLE | YES | nan | nan | nan |
| receivables_turnover | DOUBLE | YES | nan | nan | nan |
| working_capital_turnover | DOUBLE | YES | nan | nan | nan |
| total_asset_turnover | DOUBLE | YES | nan | nan | nan |
| fixed_asset_turnover | DOUBLE | YES | nan | nan | nan |
| receivables_turnover_days | DOUBLE | YES | nan | nan | nan |
| inventory_turnover_days | DOUBLE | YES | nan | nan | nan |
| current_asset_turnover | DOUBLE | YES | nan | nan | nan |
| current_asset_turnover_days | DOUBLE | YES | nan | nan | nan |
| total_asset_turnover_days | DOUBLE | YES | nan | nan | nan |
| equity_turnover | DOUBLE | YES | nan | nan | nan |
| revenue_cash_content | DOUBLE | YES | nan | nan | nan |
| operating_cf_to_operating_profit | DOUBLE | YES | nan | nan | nan |
| sales_cash_to_revenue | DOUBLE | YES | nan | nan | nan |
| operating_cf_to_revenue | DOUBLE | YES | nan | nan | nan |
| capex_to_depreciation | DOUBLE | YES | nan | nan | nan |
| operating_cf_to_short_debt | DOUBLE | YES | nan | nan | nan |
| operating_cf_to_total_debt | DOUBLE | YES | nan | nan | nan |
| total_asset_cash_recovery | DOUBLE | YES | nan | nan | nan |
| current_asset_ratio | DOUBLE | YES | nan | nan | nan |
| cash_asset_ratio | DOUBLE | YES | nan | nan | nan |
| inventory_ratio | DOUBLE | YES | nan | nan | nan |
| fixed_asset_ratio | DOUBLE | YES | nan | nan | nan |
| debt_structure_ratio | DOUBLE | YES | nan | nan | nan |
| equity_to_invested_capital | DOUBLE | YES | nan | nan | nan |
| equity_to_interest_bearing_debt | DOUBLE | YES | nan | nan | nan |
| tangible_asset_to_net_debt | DOUBLE | YES | nan | nan | nan |

### 前 3 行

| m_timetag | m_anntime | s_fa_ocfps | s_fa_bps | s_fa_eps_basic | s_fa_eps_diluted | s_fa_undistributedps | s_fa_surpluscapitalps | adjusted_earnings_per_share | du_return_on_equity | inc_revenue_rate | du_profit_rate | inc_net_profit_rate | adjusted_net_profit_rate | equity_roe | net_roe | net_profit | gear_ratio | inventory_turnover | s_fa_cfps | Symbol | s_fa_eps_deducted | s_fa_weighted_roe | s_fa_eps_deducted_q | inc_oper_profit_rate | inc_deducted_eps_rate | inc_deducted_net_profit_rate | cost_expense_profit_rate | operating_profit_margin | tax_rate_operating | cost_rate_operating | roe_dupont | return_on_investment | net_profit_margin | roa | net_margin | gross_margin | three_fee_ratio | admin_expense_ratio | financial_expense_ratio | deducted_net_profit_2 | ebit | ebitda | ebitda_to_revenue | s_fa_net_cfps | operating_cash_flow_to_net_profit | inc_equity_rate | inc_fixed_asset_rate | inc_total_asset_rate | inc_investment_income_rate | current_ratio | quick_ratio | cash_ratio | interest_coverage | tangible_asset_debt_ratio | equity_multiplier | equity_to_liability | tangible_asset_to_liability | operating_cf_to_liability | ebitda_to_liability | receivables_turnover | working_capital_turnover | total_asset_turnover | fixed_asset_turnover | receivables_turnover_days | inventory_turnover_days | current_asset_turnover | current_asset_turnover_days | total_asset_turnover_days | equity_turnover | revenue_cash_content | operating_cf_to_operating_profit | sales_cash_to_revenue | operating_cf_to_revenue | capex_to_depreciation | operating_cf_to_short_debt | operating_cf_to_total_debt | total_asset_cash_recovery | current_asset_ratio | cash_asset_ratio | inventory_ratio | fixed_asset_ratio | debt_structure_ratio | equity_to_invested_capital | equity_to_interest_bearing_debt | tangible_asset_to_net_debt |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 20160331 | 20160421 | 4.14 | 11.71 | 0.43 | 0.43 | 4.12 | 4.15 | nan | 3.487 | 33.19 | 8.1187 | 8.12 | 7.7701 | 3.7 | 3.24 | 22.1052 | 93.0 | 0.0 | 4.14 | 000001.SZ | 0.42 | 3.7 | 0.42 | 7.61 | 3.56 | 7.78 | 61.06 | 29.11 | 6.86 | 0.0 | 3.24 | 0.27 | 22.11 | 0.23 | 22.1 | 0.0 | 29.35 | 29.35 | 0.0 | 6074767872.0 | 0.0 | 0.0 | 0.0 | 0.83 | 973.84 | 16.14 | -2.88 | 6.94 | 77.59 | 0.0 | 0.0 | 0.0 | 0.0 | 1364.62 | 14.29 | 7.52 | 19.32 | 2.38 | 0.0 | 0.0 | 0.0 | 0.01 | 5.83 | 0.0 | 0.0 | 0.0 | 0.0 | 8480.0 | 0.16 | 579.35 | 840.44 | 0.0 | 215.27 | 0.0 | 0.0 | 2.38 | 2.21 | 0.0 | 0.0 | 0.0 | 0.17 | 0.0 | 0.0 | 0.0 | 0.0 |
| 20160630 | 20160812 | -1.93 | 10.0 | 0.72 | 0.72 | 3.67 | 3.29 | nan | 6.9604 | 17.59 | 6.1027 | 6.1 | 6.0651 | 7.35 | 6.41 | 22.4434 | 93.16 | 0.0 | -1.93 | 000001.SZ | 0.72 | 7.35 | 0.36 | 5.82 | -14.29 | 6.07 | 66.04 | 29.5 | 5.18 | 0.0 | 6.41 | 0.52 | 22.44 | 0.46 | 22.44 | 0.0 | 28.8 | 28.8 | 0.0 | 12293999616.0 | 0.0 | 0.0 | 0.0 | -2.48 | -270.08 | 18.7 | 10.9 | 11.72 | -23.57 | 0.0 | 0.0 | 0.0 | 0.0 | 1395.64 | 14.61 | 7.35 | 16.92 | -1.27 | 0.0 | 0.0 | 0.0 | 0.02 | 10.85 | 0.0 | 0.0 | 0.0 | 0.0 | 8723.0 | 0.31 | 433.8 | -235.36 | 0.0 | -60.61 | 65.86 | 0.0 | -1.27 | -1.18 | 0.0 | 0.0 | 0.0 | 0.19 | 0.0 | 0.0 | 0.0 | 0.0 |
| 20160930 | 20161021 | -8.91 | 10.38 | 1.09 | 1.09 | 4.05 | 3.29 | nan | 10.4073 | 15.2 | 5.5186 | 5.52 | 5.3678 | 11.02 | 9.44 | 22.837 | 92.9 | 0.0 | -8.91 | 000001.SZ | 1.09 | 11.02 | 0.37 | 5.32 | -14.21 | 5.37 | 72.53 | 29.94 | 3.78 | 0.0 | 9.44 | 0.84 | 22.84 | 0.71 | 22.84 | 0.0 | 27.7 | 27.7 | 0.0 | 18706999296.0 | 0.0 | 0.0 | 0.0 | -6.47 | -817.04 | 22.74 | 54.7 | 11.33 | -19.96 | 0.0 | 0.0 | 0.0 | 0.0 | 1339.39 | 14.08 | 7.64 | 16.75 | -5.9 | 0.0 | 0.0 | 0.0 | 0.03 | 13.44 | 0.0 | 0.0 | 0.0 | 0.0 | 8726.0 | 0.46 | 314.72 | -714.65 | 0.0 | -186.59 | 0.0 | 0.0 | -5.9 | -5.48 | 0.0 | 0.0 | 0.0 | 0.26 | 0.0 | 0.0 | 0.0 | 0.0 |

## convertible_bond（可转债）

- 类别：`4_bond_etf` ｜ 布局：`single` ｜ 文件数：1 ｜ 体积：45.2 KB
- 行数：**316** ｜ 列数：**25** ｜ 扫描耗时：0.0s

### 列

| column_name | column_type | null | key | default | extra |
|---|---|---|---|---|---|
| BondCode | VARCHAR | YES | nan | nan | nan |
| bondCode_1 | VARCHAR | YES | nan | nan | nan |
| bondConvPrice | DOUBLE | YES | nan | nan | nan |
| bondCouponRate | DOUBLE | YES | nan | nan | nan |
| bondRestScope | DOUBLE | YES | nan | nan | nan |
| putBackPrice | DOUBLE | YES | nan | nan | nan |
| forceRedeemPrice | DOUBLE | YES | nan | nan | nan |
| convStartDate | TIMESTAMP_NS | YES | nan | nan | nan |
| expireRedeemPrice | DOUBLE | YES | nan | nan | nan |
| bondMaturityDate | TIMESTAMP_NS | YES | nan | nan | nan |
| conversionPremium | DOUBLE | YES | nan | nan | nan |
| bondValue | DOUBLE | YES | nan | nan | nan |
| expireYield | DOUBLE | YES | nan | nan | nan |
| bondRating | VARCHAR | YES | nan | nan | nan |
| stockRating | VARCHAR | YES | nan | nan | nan |
| forceRedeemDate | TIMESTAMP_NS | YES | nan | nan | nan |
| forceRedeemRedeemPrice | DOUBLE | YES | nan | nan | nan |
| putDate | TIMESTAMP_NS | YES | nan | nan | nan |
| putPrice | DOUBLE | YES | nan | nan | nan |
| stockPrice | DOUBLE | YES | nan | nan | nan |
| bondPrice | DOUBLE | YES | nan | nan | nan |
| bondPremium | DOUBLE | YES | nan | nan | nan |
| conversionValue | DOUBLE | YES | nan | nan | nan |
| stockCode | VARCHAR | YES | nan | nan | nan |
| bondName | VARCHAR | YES | nan | nan | nan |

### 前 3 行

| BondCode | bondCode_1 | bondConvPrice | bondCouponRate | bondRestScope | putBackPrice | forceRedeemPrice | convStartDate | expireRedeemPrice | bondMaturityDate | conversionPremium | bondValue | expireYield | bondRating | stockRating | forceRedeemDate | forceRedeemRedeemPrice | putDate | putPrice | stockPrice | bondPrice | bondPremium | conversionValue | stockCode | bondName |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 123064.SZ | 123064 | 20.88 | 2.0 | 59760.199 | 14.62 | 27.14 | 2021-03-08 00:00:00 | 110.0 | 2026-09-01 00:00:00 | 0.4 | 0.0 | 0.0 | AA | AA | NaT | 0.0 | NaT | 0.0 | 16.87 | 111.42 | 37.9 | 80.795 | 300482.SZ | 123064.SZ |
| 123065.SZ | 123065 | 18.3 | 3.5 | 21799.42 | 12.81 | 23.79 | 2021-03-11 00:00:00 | 115.0 | 2026-09-04 00:00:00 | 0.459 | 0.0 | 0.0 | AA- | AA- | NaT | 0.0 | NaT | 0.0 | 15.65 | 120.862 | 41.33 | 85.519 | 300246.SZ | 123065.SZ |
| 123071.SZ | 123071 | 6.68 | 3.0 | 69366.438 | 4.68 | 8.68 | 2021-04-27 00:00:00 | 115.0 | 2026-10-21 00:00:00 | 0.905 | 0.0 | 0.0 | AA- | AA- | NaT | 0.0 | NaT | 0.0 | 4.69 | 115.44 | 64.42 | 70.21 | 300569.SZ | 123071.SZ |

## etf_pcf（ETF申赎清单）

- 类别：`4_bond_etf` ｜ 布局：`single` ｜ 文件数：3 ｜ 体积：1.1 MB
- 行数：**192,081** ｜ 列数：**31** ｜ 扫描耗时：0.2s

### 列

| column_name | column_type | null | key | default | extra |
|---|---|---|---|---|---|
| EtfCode | VARCHAR | YES | nan | nan | nan |
| ComponentCode | VARCHAR | YES | nan | nan | nan |
| componentExchID | VARCHAR | YES | nan | nan | nan |
| componentCode_1 | VARCHAR | YES | nan | nan | nan |
| componentVolume | BIGINT | YES | nan | nan | nan |
| ReplaceFlag | BIGINT | YES | nan | nan | nan |
| etfCode_1 | VARCHAR | YES | nan | nan | nan |
| etfExchID | VARCHAR | YES | nan | nan | nan |
| prCode | VARCHAR | YES | nan | nan | nan |
| cashBalance | DOUBLE | YES | nan | nan | nan |
| maxCashRatio | DOUBLE | YES | nan | nan | nan |
| reportUnit | BIGINT | YES | nan | nan | nan |
| name | VARCHAR | YES | nan | nan | nan |
| navPerCU | DOUBLE | YES | nan | nan | nan |
| nav | DOUBLE | YES | nan | nan | nan |
| ecc | DOUBLE | YES | nan | nan | nan |
| needPublish | BIGINT | YES | nan | nan | nan |
| enableCreation | BIGINT | YES | nan | nan | nan |
| enableRedemption | BIGINT | YES | nan | nan | nan |
| creationLimit | BIGINT | YES | nan | nan | nan |
| redemptionLimit | BIGINT | YES | nan | nan | nan |
| type | BIGINT | YES | nan | nan | nan |
| tradingDay | VARCHAR | YES | nan | nan | nan |
| preTradingDay | VARCHAR | YES | nan | nan | nan |
| TrackIndex | VARCHAR | YES | nan | nan | nan |
| Code | VARCHAR | YES | nan | nan | nan |
| NowPrice | VARCHAR | YES | nan | nan | nan |
| PreClose | VARCHAR | YES | nan | nan | nan |
| IOPV | VARCHAR | YES | nan | nan | nan |
| Zgb | VARCHAR | YES | nan | nan | nan |
| Sz | VARCHAR | YES | nan | nan | nan |

### 前 3 行

| EtfCode | ComponentCode | componentExchID | componentCode_1 | componentVolume | ReplaceFlag | etfCode_1 | etfExchID | prCode | cashBalance | maxCashRatio | reportUnit | name | navPerCU | nav | ecc | needPublish | enableCreation | enableRedemption | creationLimit | redemptionLimit | type | tradingDay | preTradingDay | TrackIndex | Code | NowPrice | PreClose | IOPV | Zgb | Sz |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 159001.SZ | 159900.SZ | SZ | 159900 | 0 | 0 |  |  |  | nan | nan | <NA> |  | nan | nan | nan | <NA> | <NA> | <NA> | <NA> | <NA> | <NA> |  |  |  |  |  |  |  |  |  |
| 159003.SZ | 159900.SZ | SZ | 159900 | 0 | 0 |  |  |  | nan | nan | <NA> |  | nan | nan | nan | <NA> | <NA> | <NA> | <NA> | <NA> | <NA> |  |  |  |  |  |  |  |  |  |
| 159005.SZ | 159900.SZ | SZ | 159900 | 0 | 0 |  |  |  | nan | nan | <NA> |  | nan | nan | nan | <NA> | <NA> | <NA> | <NA> | <NA> | <NA> |  |  |  |  |  |  |  |  |  |

## market_sentiment（市场情绪）

- 类别：`5_technical_derived` ｜ 布局：`partition` ｜ 文件数：2611 ｜ 体积：967.3 MB
- 行数：**10,760,462** ｜ 列数：**18** ｜ 扫描耗时：2.4s

### 列

| column_name | column_type | null | key | default | extra |
|---|---|---|---|---|---|
| symbol | VARCHAR | YES | nan | nan | nan |
| time | TIMESTAMP_NS | YES | nan | nan | nan |
| close | DOUBLE | YES | nan | nan | nan |
| price_range | DOUBLE | YES | nan | nan | nan |
| upper_shadow | DOUBLE | YES | nan | nan | nan |
| lower_shadow | DOUBLE | YES | nan | nan | nan |
| body_ratio | DOUBLE | YES | nan | nan | nan |
| amount_per_trade | DOUBLE | YES | nan | nan | nan |
| liquidity_score | DOUBLE | YES | nan | nan | nan |
| intraday_vol | DOUBLE | YES | nan | nan | nan |
| gap_up_down | DOUBLE | YES | nan | nan | nan |
| buy_pressure | DOUBLE | YES | nan | nan | nan |
| sell_pressure | DOUBLE | YES | nan | nan | nan |
| momentum_1d | DOUBLE | YES | nan | nan | nan |
| momentum_3d | DOUBLE | YES | nan | nan | nan |
| am_pm_trend | DOUBLE | YES | nan | nan | nan |
| volume_concentration | DOUBLE | YES | nan | nan | nan |
| dt | BIGINT | YES | nan | nan | nan |

### 前 3 行

| symbol | time | close | price_range | upper_shadow | lower_shadow | body_ratio | amount_per_trade | liquidity_score | intraday_vol | gap_up_down | buy_pressure | sell_pressure | momentum_1d | momentum_3d | am_pm_trend | volume_concentration | dt |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 000001.SZ | 2016-01-04 00:00:00 | 11.33 | 0.07060900264783751 | 0.0026478375992938534 | 0.008826125330979668 | 0.8374999998953135 | 11.719246065196442 | nan | 0.07060900264783751 | nan | 0.12499999998437472 | 0.8749999998906253 | nan | nan | -0.8374999998953135 | nan | 20160104 |
| 000006.SZ | 2016-01-04 00:00:00 | 10.38 | 0.11175337186897881 | 0.005780346820809125 | 0.0019267822736032127 | 0.9310344826783591 | 10.935659244178037 | nan | 0.11175337186897881 | nan | 0.017241379308859665 | 0.9827586206049335 | nan | nan | -0.9310344826783591 | nan | 20160104 |
| 000008.SZ | 2016-01-04 00:00:00 | 10.61 | 0.12064090480678616 | 0.0 | 0.0009425070688029959 | 0.9921874999224856 | 11.183381644136148 | nan | 0.12064090480678616 | nan | 0.007812499999389475 | 0.9921874999224856 | nan | nan | -0.9921874999224856 | nan | 20160104 |

### 单位（来自 skills/quantdb-fields）

- `symbol`：后缀式 600519.SH
- `time`：时间戳
- `close`：元（注意各表复权口径不同）
- `dt`：YYYYMMDD 整数

## technical_indicators（技术指标）

- 类别：`5_technical_derived` ｜ 布局：`partition` ｜ 文件数：2623 ｜ 体积：2.4 GB
- 行数：**10,827,362** ｜ 列数：**36** ｜ 扫描耗时：3.3s

### 列

| column_name | column_type | null | key | default | extra |
|---|---|---|---|---|---|
| symbol | VARCHAR | YES | nan | nan | nan |
| time | TIMESTAMP_NS | YES | nan | nan | nan |
| close | DOUBLE | YES | nan | nan | nan |
| ma5 | DOUBLE | YES | nan | nan | nan |
| ma10 | DOUBLE | YES | nan | nan | nan |
| ma20 | DOUBLE | YES | nan | nan | nan |
| ma60 | DOUBLE | YES | nan | nan | nan |
| ma_gap_5 | DOUBLE | YES | nan | nan | nan |
| ma_gap_10 | DOUBLE | YES | nan | nan | nan |
| ma_gap_20 | DOUBLE | YES | nan | nan | nan |
| rsi_6 | DOUBLE | YES | nan | nan | nan |
| rsi_14 | DOUBLE | YES | nan | nan | nan |
| kdj_k | DOUBLE | YES | nan | nan | nan |
| kdj_d | DOUBLE | YES | nan | nan | nan |
| kdj_j | DOUBLE | YES | nan | nan | nan |
| macd_dif | DOUBLE | YES | nan | nan | nan |
| macd_dea | DOUBLE | YES | nan | nan | nan |
| macd_hist | DOUBLE | YES | nan | nan | nan |
| vol_std_5 | DOUBLE | YES | nan | nan | nan |
| vol_std_20 | DOUBLE | YES | nan | nan | nan |
| vol_std_60 | DOUBLE | YES | nan | nan | nan |
| vol_atr_14 | DOUBLE | YES | nan | nan | nan |
| vol_to_ma5 | DOUBLE | YES | nan | nan | nan |
| vol_to_ma20 | DOUBLE | YES | nan | nan | nan |
| volume_ma_3 | DOUBLE | YES | nan | nan | nan |
| amount_ma_5 | DOUBLE | YES | nan | nan | nan |
| volume_trend_3d | DOUBLE | YES | nan | nan | nan |
| future_return_1d | DOUBLE | YES | nan | nan | nan |
| future_return_3d | DOUBLE | YES | nan | nan | nan |
| future_return_5d | DOUBLE | YES | nan | nan | nan |
| future_return_10d | DOUBLE | YES | nan | nan | nan |
| future_return_20d | DOUBLE | YES | nan | nan | nan |
| future_return_60d | DOUBLE | YES | nan | nan | nan |
| pct_change | DOUBLE | YES | nan | nan | nan |
| beta_20 | DOUBLE | YES | nan | nan | nan |
| dt | BIGINT | YES | nan | nan | nan |

### 前 3 行

| symbol | time | close | ma5 | ma10 | ma20 | ma60 | ma_gap_5 | ma_gap_10 | ma_gap_20 | rsi_6 | rsi_14 | kdj_k | kdj_d | kdj_j | macd_dif | macd_dea | macd_hist | vol_std_5 | vol_std_20 | vol_std_60 | vol_atr_14 | vol_to_ma5 | vol_to_ma20 | volume_ma_3 | amount_ma_5 | volume_trend_3d | future_return_1d | future_return_3d | future_return_5d | future_return_10d | future_return_20d | future_return_60d | pct_change | beta_20 | dt |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 000001.SZ | 2016-01-04 00:00:00 | 11.33 | nan | nan | nan | nan | nan | nan | nan | nan | nan | nan | nan | nan | 0.0 | 0.0 | 0.0 | nan | nan | nan | 0.7999999999999989 | nan | nan | nan | nan | nan | 0.6178287731685828 | -3.4421888790820865 | -5.030891438658436 | -8.120035304501327 | -13.503971756398936 | -5.560458958517223 | nan | nan | 20160104 |
| 000006.SZ | 2016-01-04 00:00:00 | 10.38 | nan | nan | nan | nan | nan | nan | nan | nan | nan | nan | nan | nan | 0.0 | 0.0 | 0.0 | nan | nan | nan | 1.1600000000000001 | nan | nan | nan | nan | nan | -3.2755298651252596 | -10.693641618497118 | -18.68978805394992 | -18.68978805394992 | -31.310211946050103 | -18.68978805394992 | nan | nan | 20160104 |
| 000008.SZ | 2016-01-04 00:00:00 | 10.61 | nan | nan | nan | nan | nan | nan | nan | nan | nan | nan | nan | nan | 0.0 | 0.0 | 0.0 | nan | nan | nan | 1.2800000000000011 | nan | nan | nan | nan | nan | 1.8850141376060447 | -5.84354382657869 | -8.199811498586229 | -12.629594721960412 | -21.67766258246936 | 2.5447690857681504 | nan | nan | 20160104 |

### 单位（来自 skills/quantdb-fields）

- `symbol`：后缀式 600519.SH
- `time`：时间戳
- `close`：元（注意各表复权口径不同）
- `dt`：YYYYMMDD 整数

## valuation（估值）

- 类别：`5_technical_derived` ｜ 布局：`partition` ｜ 文件数：2611 ｜ 体积：809.8 MB
- 行数：**10,760,462** ｜ 列数：**17** ｜ 扫描耗时：2.2s

### 列

| column_name | column_type | null | key | default | extra |
|---|---|---|---|---|---|
| symbol | VARCHAR | YES | nan | nan | nan |
| time | TIMESTAMP_NS | YES | nan | nan | nan |
| close | DOUBLE | YES | nan | nan | nan |
| total_capital | DOUBLE | YES | nan | nan | nan |
| circulating_capital | DOUBLE | YES | nan | nan | nan |
| total_mv | DOUBLE | YES | nan | nan | nan |
| float_mv | DOUBLE | YES | nan | nan | nan |
| net_profit_ttm | DOUBLE | YES | nan | nan | nan |
| revenue_ttm | DOUBLE | YES | nan | nan | nan |
| equity | DOUBLE | YES | nan | nan | nan |
| annual_net_profit | DOUBLE | YES | nan | nan | nan |
| pe_ttm | DOUBLE | YES | nan | nan | nan |
| pe_static | DOUBLE | YES | nan | nan | nan |
| pb | DOUBLE | YES | nan | nan | nan |
| ps_ttm | DOUBLE | YES | nan | nan | nan |
| dividend_rate | DOUBLE | YES | nan | nan | nan |
| dt | BIGINT | YES | nan | nan | nan |

### 前 3 行

| symbol | time | close | total_capital | circulating_capital | total_mv | float_mv | net_profit_ttm | revenue_ttm | equity | annual_net_profit | pe_ttm | pe_static | pb | ps_ttm | dividend_rate | dt |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 000001.SZ | 2016-01-04 00:00:00 | 11.33 | nan | nan | nan | nan | nan | nan | nan | nan | nan | nan | nan | nan | 0.015357458075904678 | 20160104 |
| 000006.SZ | 2016-01-04 00:00:00 | 10.38 | nan | nan | nan | nan | nan | nan | nan | nan | nan | nan | nan | nan | 0.011368015414258187 | 20160104 |
| 000008.SZ | 2016-01-04 00:00:00 | 10.61 | nan | nan | nan | nan | nan | nan | nan | nan | nan | nan | nan | nan | 0.0 | 20160104 |

### 单位（来自 skills/quantdb-fields）

- `symbol`：后缀式 600519.SH
- `time`：时间戳
- `close`：元（注意各表复权口径不同）
- `dt`：YYYYMMDD 整数

## features_daily（日频特征）

- 类别：`6_ml_datasets` ｜ 布局：`partition` ｜ 文件数：2623 ｜ 体积：3.3 GB
- 行数：**10,550,057** ｜ 列数：**79** ｜ 扫描耗时：5.6s

### 列

| column_name | column_type | null | key | default | extra |
|---|---|---|---|---|---|
| symbol | VARCHAR | YES | nan | nan | nan |
| time | TIMESTAMP_NS | YES | nan | nan | nan |
| close | DOUBLE | YES | nan | nan | nan |
| ma5 | DOUBLE | YES | nan | nan | nan |
| ma10 | DOUBLE | YES | nan | nan | nan |
| ma20 | DOUBLE | YES | nan | nan | nan |
| ma60 | DOUBLE | YES | nan | nan | nan |
| ma_gap_5 | DOUBLE | YES | nan | nan | nan |
| ma_gap_10 | DOUBLE | YES | nan | nan | nan |
| ma_gap_20 | DOUBLE | YES | nan | nan | nan |
| rsi_6 | DOUBLE | YES | nan | nan | nan |
| rsi_14 | DOUBLE | YES | nan | nan | nan |
| kdj_k | DOUBLE | YES | nan | nan | nan |
| kdj_d | DOUBLE | YES | nan | nan | nan |
| kdj_j | DOUBLE | YES | nan | nan | nan |
| macd_dif | DOUBLE | YES | nan | nan | nan |
| macd_dea | DOUBLE | YES | nan | nan | nan |
| macd_hist | DOUBLE | YES | nan | nan | nan |
| vol_std_5 | DOUBLE | YES | nan | nan | nan |
| vol_std_20 | DOUBLE | YES | nan | nan | nan |
| vol_std_60 | DOUBLE | YES | nan | nan | nan |
| vol_atr_14 | DOUBLE | YES | nan | nan | nan |
| vol_to_ma5 | DOUBLE | YES | nan | nan | nan |
| vol_to_ma20 | DOUBLE | YES | nan | nan | nan |
| volume_ma_3 | DOUBLE | YES | nan | nan | nan |
| amount_ma_5 | DOUBLE | YES | nan | nan | nan |
| volume_trend_3d | DOUBLE | YES | nan | nan | nan |
| future_return_1d | DOUBLE | YES | nan | nan | nan |
| future_return_3d | DOUBLE | YES | nan | nan | nan |
| future_return_5d | DOUBLE | YES | nan | nan | nan |
| future_return_10d | DOUBLE | YES | nan | nan | nan |
| future_return_20d | DOUBLE | YES | nan | nan | nan |
| future_return_60d | DOUBLE | YES | nan | nan | nan |
| pct_change | DOUBLE | YES | nan | nan | nan |
| beta_20 | DOUBLE | YES | nan | nan | nan |
| total_capital | DOUBLE | YES | nan | nan | nan |
| circulating_capital | DOUBLE | YES | nan | nan | nan |
| total_mv | DOUBLE | YES | nan | nan | nan |
| float_mv | DOUBLE | YES | nan | nan | nan |
| net_profit_ttm | DOUBLE | YES | nan | nan | nan |
| revenue_ttm | DOUBLE | YES | nan | nan | nan |
| equity | DOUBLE | YES | nan | nan | nan |
| annual_net_profit | DOUBLE | YES | nan | nan | nan |
| pe_ttm | DOUBLE | YES | nan | nan | nan |
| pe_static | DOUBLE | YES | nan | nan | nan |
| pb | DOUBLE | YES | nan | nan | nan |
| ps_ttm | DOUBLE | YES | nan | nan | nan |
| dividend_rate | DOUBLE | YES | nan | nan | nan |
| in_hs300 | VARCHAR | YES | nan | nan | nan |
| is_hsgt | VARCHAR | YES | nan | nan | nan |
| is_margin | VARCHAR | YES | nan | nan | nan |
| is_kcb_creatable | VARCHAR | YES | nan | nan | nan |
| is_st | DOUBLE | YES | nan | nan | nan |
| is_quit_risk | VARCHAR | YES | nan | nan | nan |
| is_hk | VARCHAR | YES | nan | nan | nan |
| industry_code | VARCHAR | YES | nan | nan | nan |
| industry_name | VARCHAR | YES | nan | nan | nan |
| sector_code | VARCHAR | YES | nan | nan | nan |
| region_area_code | VARCHAR | YES | nan | nan | nan |
| region_area_name | VARCHAR | YES | nan | nan | nan |
| main_business | VARCHAR | YES | nan | nan | nan |
| list_date | VARCHAR | YES | nan | nan | nan |
| total_cap_yi | VARCHAR | YES | nan | nan | nan |
| float_mv_yi | VARCHAR | YES | nan | nan | nan |
| free_float_shares | DOUBLE | YES | nan | nan | nan |
| ipo_price | VARCHAR | YES | nan | nan | nan |
| zt_price | VARCHAR | YES | nan | nan | nan |
| dt_price | VARCHAR | YES | nan | nan | nan |
| hs_turnover | VARCHAR | YES | nan | nan | nan |
| seal_strength | VARCHAR | YES | nan | nan | nan |
| zaf | VARCHAR | YES | nan | nan | nan |
| beta_now | VARCHAR | YES | nan | nan | nan |
| dyna_pe | VARCHAR | YES | nan | nan | nan |
| static_pe_ttm | VARCHAR | YES | nan | nan | nan |
| div_yield | VARCHAR | YES | nan | nan | nan |
| pb_mrq | VARCHAR | YES | nan | nan | nan |
| ever_zt_count | VARCHAR | YES | nan | nan | nan |
| year_zt_days | VARCHAR | YES | nan | nan | nan |
| dt | BIGINT | YES | nan | nan | nan |

### 前 3 行

| symbol | time | close | ma5 | ma10 | ma20 | ma60 | ma_gap_5 | ma_gap_10 | ma_gap_20 | rsi_6 | rsi_14 | kdj_k | kdj_d | kdj_j | macd_dif | macd_dea | macd_hist | vol_std_5 | vol_std_20 | vol_std_60 | vol_atr_14 | vol_to_ma5 | vol_to_ma20 | volume_ma_3 | amount_ma_5 | volume_trend_3d | future_return_1d | future_return_3d | future_return_5d | future_return_10d | future_return_20d | future_return_60d | pct_change | beta_20 | total_capital | circulating_capital | total_mv | float_mv | net_profit_ttm | revenue_ttm | equity | annual_net_profit | pe_ttm | pe_static | pb | ps_ttm | dividend_rate | in_hs300 | is_hsgt | is_margin | is_kcb_creatable | is_st | is_quit_risk | is_hk | industry_code | industry_name | sector_code | region_area_code | region_area_name | main_business | list_date | total_cap_yi | float_mv_yi | free_float_shares | ipo_price | zt_price | dt_price | hs_turnover | seal_strength | zaf | beta_now | dyna_pe | static_pe_ttm | div_yield | pb_mrq | ever_zt_count | year_zt_days | dt |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 000001.SZ | 2016-01-04 00:00:00 | 11.33 | nan | nan | nan | nan | nan | nan | nan | nan | nan | nan | nan | nan | 0.0 | 0.0 | 0.0 | nan | nan | nan | 0.7999999999999989 | nan | nan | nan | nan | nan | 0.6178287731685828 | -3.4421888790820865 | -5.030891438658436 | -8.120035304501327 | -13.503971756398936 | -5.560458958517223 | nan | nan | nan | nan | nan | nan | nan | nan | nan | nan | nan | nan | nan | nan | 0.015357458075904678 |  |  |  |  | 0.0 |  |  | X5001 | 全国性银行 | 881386 | 18 | 深圳板块 | 批发金融业务 | 19910403 |  |  | nan | 40.00 |  |  |  |  |  |  |  |  |  |  |  |  | 20160104 |
| 000006.SZ | 2016-01-04 00:00:00 | 10.38 | nan | nan | nan | nan | nan | nan | nan | nan | nan | nan | nan | nan | 0.0 | 0.0 | 0.0 | nan | nan | nan | 1.1600000000000001 | nan | nan | nan | nan | nan | -3.2755298651252596 | -10.693641618497118 | -18.68978805394992 | -18.68978805394992 | -31.310211946050103 | -18.68978805394992 | nan | nan | nan | nan | nan | nan | nan | nan | nan | nan | nan | nan | nan | nan | 0.011368015414258187 |  |  |  |  | 0.0 |  |  | X5301 | 房地产开发 | 881418 | 18 | 深圳板块 | 房产销售 | 19920427 |  |  | nan | 10.00 |  |  |  |  |  |  |  |  |  |  |  |  | 20160104 |
| 000008.SZ | 2016-01-04 00:00:00 | 10.61 | nan | nan | nan | nan | nan | nan | nan | nan | nan | nan | nan | nan | 0.0 | 0.0 | 0.0 | nan | nan | nan | 1.2800000000000011 | nan | nan | nan | nan | nan | 1.8850141376060447 | -5.84354382657869 | -8.199811498586229 | -12.629594721960412 | -21.67766258246936 | 2.5447690857681504 | nan | nan | nan | nan | nan | nan | nan | nan | nan | nan | nan | nan | nan | nan | 0.0 |  |  |  |  | 0.0 |  |  | X3201 | 轨交设备 | 881293 | 7 | 北京板块 | 轨道交通运营检修装备 | 19920507 |  |  | nan | 10.00 |  |  |  |  |  |  |  |  |  |  |  |  | 20160104 |

### 单位（来自 skills/quantdb-fields）

- `symbol`：后缀式 600519.SH
- `time`：时间戳
- `close`：元（注意各表复权口径不同）
- `dt`：YYYYMMDD 整数

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
| 3_financial_data | balance | 资产负债表 | single | 5559 | 317.4 MB | 191900 | 75 |
| 3_financial_data | capital | 股本结构 | single | 5559 | 43.3 MB | 308841 | 10 |
| 3_financial_data | cashflow | 现金流量表 | single | 5559 | 308.7 MB | 196961 | 68 |
| 3_financial_data | dividend_factors | 分红送转因子 | single | 5451 | 24.1 MB | 58933 | 11 |
| 3_financial_data | holder_num | 股东户数 | single | 5559 | 144.8 MB | 191533 | 38 |
| 3_financial_data | income | 利润表 | single | 5559 | 179.3 MB | 197727 | 37 |
| 3_financial_data | per_share | 每股指标TTM | single | 5558 | 47.1 MB | 191467 | 10 |
| 3_financial_data | pershare_index | 每股财务指标 | single | 5559 | 416.5 MB | 193749 | 86 |
| 4_bond_etf | convertible_bond | 可转债 | single | 1 | 45.2 KB | 316 | 25 |
| 4_bond_etf | etf_pcf | ETF申赎清单 | single | 3 | 1.1 MB | 192081 | 31 |
| 5_technical_derived | market_sentiment | 市场情绪 | partition | 2611 | 967.3 MB | 10760462 | 18 |
| 5_technical_derived | technical_indicators | 技术指标 | partition | 2623 | 2.4 GB | 10827362 | 36 |
| 5_technical_derived | valuation | 估值 | partition | 2611 | 809.8 MB | 10760462 | 17 |
| 6_ml_datasets | features_daily | 日频特征 | partition | 2623 | 3.3 GB | 10550057 | 79 |
