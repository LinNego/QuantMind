# refdata —— 研究用小缓存（入库）

中间产物入库，只为**让回测在别的机器上可复现**。不要手工改，改了就重跑生成脚本。

## 一、文件来源

| 文件 | 大小 | 内容 | 数据来源 | 生成命令 |
|---|---|---|---|---|
| `core_nav.parquet` | 69K | 核心策略日频 NAV（2400 天，含现金） | 本地 DB `1_kline_data/daily_unadjusted` + `2_base_sector/instrument_detail` | `python scripts/research_satellite.py --rebuild-core` |
| `exposure.parquet` | 3K | 每月 e（129 个月） | 同上（国证2000 399303 + 全 A 广度） | 同上 |
| `val_month2.parquet` | 13M | 估值月末面板（533,177 行 × 6 列） | 本地 DB `5_technical_derived/valuation/dt=*/data.parquet` | `python scripts/research_satellite.py --rebuild-panel` |
| `fin_pit.pkl` | 519K | 点时财报（按 `m_anntime` 对齐，防前视） | 本地 DB `3_financial_data/balance/` + `cashflow/`，仅 138 只候选股 | 同上 |
| `val_month.parquet` | 12M | 行业筛选用估值面板（533,177 行 × 5 列） | 同 `val_month2`（少一个 revenue_ttm） | `python scripts/research_industry_checklist.py --rebuild` |
| `index_cyb.parquet` | 177K | 创业板指 399006（4,004 天） | **akshare** `index_zh_daily_sina(symbol='sz399006')` | 一次性下载 |
| `index_zz2000.parquet` | 169K | 国证2000 399303（3,079 天） | **akshare** `index_zh_daily_sina(symbol='sz399303')` | 同上 |
| `index_沪深300.parquet` | 91K | 沪深300 000300（6,039 天） | **akshare** `index_zh_daily_sina(symbol='sh000300')` | 同上 |
| `index_中证500.parquet` | 81K | 中证500 000905（5,318 天） | **akshare** `index_zh_daily_sina(symbol='sh000905')` | 同上 |
| `index_中证1000.parquet` | 46K | 中证1000 000852（2,945 天） | **akshare** `index_zh_daily_sina(symbol='sh000852')` | 同上 |
| `index_科创50.parquet` | 26K | 科创50 000688（1,672 天，2020 起） | **akshare** `index_zh_daily_sina(symbol='sh000688')` | 同上 |
| `index_上证50.parquet` | 83K | 上证50 000016（5,561 天） | **akshare** `index_zh_daily_sina(symbol='sh000016')` | 同上 |

**入库范围（重要）：** 上表里 **`val_month.parquet` 和 `val_month2.parquet`（各 11-13MB）不入库**，
`.gitignore` 里单独排除——它们由 `--rebuild-panel` / `--rebuild` 一分钟重建。
入库的只有 1.3MB：`core_nav` / `exposure` / `fin_pit` / 7 个 `index_*`。

**两类来源：**

1. **本地 quantdb**（`/opt/quantmind/data/quantdb`）—— 由 `eltdx_daily_update.py` 等同步脚本从通达信/东财落盘
2. **akshare 新浪接口**（`index_zh_daily_sina`）—— 指数日线，quantdb 的 `index_daily` 覆盖不全，所以单独拉

## 二、大文件不放这里

全 A 日线矩阵（close/open/high/low/amt，约 350MB）放 `refdata_big/`（.gitignore 忽略），由下式重建：

```bash
python scripts/build_price_panel.py
# 来源：本地 DB 1_kline_data/daily_backward/dt=*/data.parquet（2,623 个交易日）
# 产出：refdata_big/{close_daily_backward,open_adj,high_adj,low_adj,amt}.parquet
```

**为什么分两处：** 小文件（<15MB）入库换可复现；大文件可重建、不值得占 git（当前 .git 285MB）。

## 三、新机器上手顺序

```bash
git clone <repo> && cd QuantMind
python scripts/build_price_panel.py                     # 1) 大面板（3-5 分钟）
python scripts/research_satellite.py --only 2           # 2) 直接跑，小缓存已入库
python scripts/research_industry_checklist.py --screen  # 3) 出当前行业清单
```

**只有 1) 依赖本地 quantdb（`/opt/quantmind/data/quantdb`）。** 若 DB 不在这个路径，改脚本顶部的 `DB` 常量。
