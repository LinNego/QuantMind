# D06 缺失率与起始日 + D10 两个暗坑

- 生成时间：2026-10-31 17:35:14
- 表：`features_daily` ｜ 79 列 ｜ 10,550,057 行

## 1. 全列缺失率（按缺失从多到少）

| # | 列 | 类型 | 缺失率 | 第一份非空日期 |
|---|---|---|---|---|
| 1 | `in_hs300` | VARCHAR | 90.5% | 20260105 |
| 2 | `is_hsgt` | VARCHAR | 90.5% | 20260105 |
| 3 | `is_margin` | VARCHAR | 90.5% | 20260105 |
| 4 | `is_kcb_creatable` | VARCHAR | 90.5% | 20260105 |
| 5 | `is_quit_risk` | VARCHAR | 90.5% | 20260105 |
| 6 | `is_hk` | VARCHAR | 90.5% | 20260105 |
| 7 | `total_cap_yi` | VARCHAR | 90.5% | 20260105 |
| 8 | `float_mv_yi` | VARCHAR | 90.5% | 20260105 |
| 9 | `free_float_shares` | DOUBLE | 90.5% | 20260105 |
| 10 | `zt_price` | VARCHAR | 90.5% | 20260105 |
| 11 | `dt_price` | VARCHAR | 90.5% | 20260105 |
| 12 | `hs_turnover` | VARCHAR | 90.5% | 20260105 |
| 13 | `seal_strength` | VARCHAR | 90.5% | 20260105 |
| 14 | `zaf` | VARCHAR | 90.5% | 20260105 |
| 15 | `beta_now` | VARCHAR | 90.5% | 20260105 |
| 16 | `dyna_pe` | VARCHAR | 90.5% | 20260105 |
| 17 | `static_pe_ttm` | VARCHAR | 90.5% | 20260105 |
| 18 | `div_yield` | VARCHAR | 90.5% | 20260105 |
| 19 | `pb_mrq` | VARCHAR | 90.5% | 20260105 |
| 20 | `ever_zt_count` | VARCHAR | 90.5% | 20260105 |
| 21 | `year_zt_days` | VARCHAR | 90.5% | 20260105 |
| 22 | `ps_ttm` | DOUBLE | 7.6% | 20170116 |
| 23 | `pe_ttm` | DOUBLE | 7.5% | 20170116 |
| 24 | `pe_static` | DOUBLE | 7.5% | 20170116 |
| 25 | `revenue_ttm` | DOUBLE | 7.4% | 20170116 |
| 26 | `net_profit_ttm` | DOUBLE | 7.4% | 20170116 |
| 27 | `annual_net_profit` | DOUBLE | 7.2% | 20170116 |
| 28 | `vol_std_60` | DOUBLE | 3.0% | 20160405 |
| 29 | `future_return_60d` | DOUBLE | 3.0% | 20160104 |
| 30 | `ma60` | DOUBLE | 2.9% | 20160401 |
| 31 | `pb` | DOUBLE | 1.9% | 20160408 |
| 32 | `equity` | DOUBLE | 1.7% | 20160408 |
| 33 | `total_capital` | DOUBLE | 1.6% | 20160104 |
| 34 | `circulating_capital` | DOUBLE | 1.6% | 20160104 |
| 35 | `total_mv` | DOUBLE | 1.6% | 20160104 |
| 36 | `float_mv` | DOUBLE | 1.6% | 20160104 |
| 37 | `vol_std_20` | DOUBLE | 1.0% | 20160201 |
| 38 | `future_return_20d` | DOUBLE | 1.0% | 20160104 |
| 39 | `beta_20` | DOUBLE | 1.0% | 20160201 |
| 40 | `vol_to_ma20` | DOUBLE | 0.9% | 20160129 |
| 41 | `ma20` | DOUBLE | 0.9% | 20160129 |
| 42 | `ma_gap_20` | DOUBLE | 0.9% | 20160129 |
| 43 | `dividend_rate` | DOUBLE | 0.6% | 20160104 |
| 44 | `future_return_10d` | DOUBLE | 0.5% | 20160104 |
| 45 | `ma10` | DOUBLE | 0.4% | 20160115 |
| 46 | `ma_gap_10` | DOUBLE | 0.4% | 20160115 |
| 47 | `kdj_k` | DOUBLE | 0.4% | 20160114 |
| 48 | `kdj_d` | DOUBLE | 0.4% | 20160114 |
| 49 | `kdj_j` | DOUBLE | 0.4% | 20160114 |
| 50 | `vol_std_5` | DOUBLE | 0.2% | 20160111 |
| 51 | `future_return_5d` | DOUBLE | 0.2% | 20160104 |
| 52 | `vol_to_ma5` | DOUBLE | 0.2% | 20160108 |
| 53 | `ma5` | DOUBLE | 0.2% | 20160108 |
| 54 | `ma_gap_5` | DOUBLE | 0.2% | 20160108 |
| 55 | `amount_ma_5` | DOUBLE | 0.2% | 20160108 |
| 56 | `volume_trend_3d` | DOUBLE | 0.2% | 20160107 |
| 57 | `future_return_3d` | DOUBLE | 0.1% | 20160104 |
| 58 | `volume_ma_3` | DOUBLE | 0.1% | 20160106 |
| 59 | `rsi_6` | DOUBLE | 0.0% | 20160105 |
| 60 | `rsi_14` | DOUBLE | 0.0% | 20160105 |
| 61 | `future_return_1d` | DOUBLE | 0.0% | 20160104 |
| 62 | `pct_change` | DOUBLE | 0.0% | 20160105 |
| 63 | `symbol` | VARCHAR | 0.0% | 20160104 |
| 64 | `time` | TIMESTAMP_NS | 0.0% | 20160104 |
| 65 | `close` | DOUBLE | 0.0% | 20160104 |
| 66 | `macd_dif` | DOUBLE | 0.0% | 20160104 |
| 67 | `macd_dea` | DOUBLE | 0.0% | 20160104 |
| 68 | `macd_hist` | DOUBLE | 0.0% | 20160104 |
| 69 | `vol_atr_14` | DOUBLE | 0.0% | 20160104 |
| 70 | `is_st` | DOUBLE | 0.0% | 20160104 |
| 71 | `industry_code` | VARCHAR | 0.0% | 20160104 |
| 72 | `industry_name` | VARCHAR | 0.0% | 20160104 |
| 73 | `sector_code` | VARCHAR | 0.0% | 20160104 |
| 74 | `region_area_code` | VARCHAR | 0.0% | 20160104 |
| 75 | `region_area_name` | VARCHAR | 0.0% | 20160104 |
| 76 | `main_business` | VARCHAR | 0.0% | 20160104 |
| 77 | `list_date` | VARCHAR | 0.0% | 20160104 |
| 78 | `ipo_price` | VARCHAR | 0.0% | 20160104 |

## 2. 怎么读这张表

**完全没缺的列：16 个** —— 主力数据（close / pe_ttm / total_mv / is_st …）都在其中。

**2026 年才开始的列：21 个**，2016–2025 天然全空，**不是坏数据**：

```
  in_hs300
  is_hsgt
  is_margin
  is_kcb_creatable
  is_quit_risk
  is_hk
  total_cap_yi
  float_mv_yi
  free_float_shares
  zt_price
  dt_price
  hs_turnover
  seal_strength
  zaf
  beta_now
  dyna_pe
  static_pe_ttm
  div_yield
  pb_mrq
  ever_zt_count
  year_zt_days
```

→ 这些列只能用在 2026 之后。做 2016–2024 回测时，
  `zt_price` / `hs_turnover` / `zaf` 一律不可用，要自己按板块算涨跌停、自己算换手。

## 3. 哪些列是「中途才开始」的（真正要小心的）

| 列 | 缺失率 | 起始日 | 意味着 |
|---|---|---|---|
| `net_profit_ttm` | 7.4% | 20170116 | 20170116 之前用不了 |
| `revenue_ttm` | 7.4% | 20170116 | 20170116 之前用不了 |
| `annual_net_profit` | 7.2% | 20170116 | 20170116 之前用不了 |
| `pe_ttm` | 7.5% | 20170116 | 20170116 之前用不了 |
| `pe_static` | 7.5% | 20170116 | 20170116 之前用不了 |
| `ps_ttm` | 7.6% | 20170116 | 20170116 之前用不了 |

> 注意：`ma5` / `ma20` / `rsi_14` 这类指标在 2016 年 1–4 月才陆续有值，
> 那是**指标自己的预热期**（要攒够 N 天才能算），不是数据缺失。
> 它们从 2016 年 5 月起就全有了，不用担心。

## 4. 暗坑 A：标签泄漏列

共 **6** 列：`future_return_1d`, `future_return_3d`, `future_return_5d`, `future_return_10d`, `future_return_20d`, `future_return_60d`

```python
# ❌ 这样写，未来数据直接进模型/进打分
df = con.execute("SELECT * FROM read_parquet('.../features_daily/*/data.parquet')").df()

# ✅ 显式列举你要的列
COLS = ['symbol', 'dt', 'close', 'pe_ttm', 'total_mv']   # 不含 future_*
```

**约定：读 features_daily 一律写显式列清单。**

## 5. 暗坑 B：字符串存储的数值列

共 **29** 列：

```
  symbol
  in_hs300
  is_hsgt
  is_margin
  is_kcb_creatable
  is_quit_risk
  is_hk
  industry_code
  industry_name
  sector_code
  region_area_code
  region_area_name
  main_business
  list_date
  total_cap_yi
  float_mv_yi
  ipo_price
  zt_price
  dt_price
  hs_turnover
  seal_strength
  zaf
  beta_now
  dyna_pe
  static_pe_ttm
  div_yield
  pb_mrq
  ever_zt_count
  year_zt_days
```

实测换手率 Top3（最新一天）：

| 方式 | 第1 | 第2 | 第3 |
|---|---|---|---|
| 按字符串排（错） | 603344.SH 9.96 | 301152.SZ 9.96 | 301560.SZ 9.91 |
| 转数值后排（对） | 301569.SZ 79.79 | 301520.SZ 46.63 | 301052.SZ 45.6 |

→ 字典序里 `"9.96" > "79.79"`。**不报错，静默排错。**
→ 凡是 `hs_turnover` / `zt_price` / `zaf` / `total_cap_yi` 这类列，
   排序、比较、求和之前必须 `CAST(... AS DOUBLE)`。
