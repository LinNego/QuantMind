# D10 · 两个暗坑

- 数据：`/opt/quantmind/data/quantdb/6_ml_datasets/features_daily`（79 列）

## 坑 A：标签泄漏列

共 **6** 个：`future_return_1d`, `future_return_3d`, `future_return_5d`, `future_return_10d`, `future_return_20d`, `future_return_60d`

这些是**未来收益（标签）**，不是特征。`SELECT *` 或整表 merge 会把它们带进特征矩阵，
你会得到一个 IC ≈ 0.9 的「完美模型」，然后拿真钱去验证它。

> **约定：读 `features_daily` 一律写显式列清单。**

## 坑 B：字符串数值列

共 **29** 个：`symbol`, `in_hs300`, `is_hsgt`, `is_margin`, `is_kcb_creatable`, `is_quit_risk`, `is_hk`, `industry_code`, `industry_name`, `sector_code`, `region_area_code`, `region_area_name`, `main_business`, `list_date`, `total_cap_yi`, `float_mv_yi`, `ipo_price`, `zt_price`, `dt_price`, `hs_turnover`, `seal_strength`, `zaf`, `beta_now`, `dyna_pe`, `static_pe_ttm`, `div_yield`, `pb_mrq`, `ever_zt_count`, `year_zt_days`

### 反例：`hs_turnover` 的排序

| 排序方式 | Top 3 |
|---|---|
| 按字符串（字典序） | 9.99 / 9.99 / 9.98 |
| CAST 成 DOUBLE 后 | 79.79 / 79.79 / 79.79 |

因为字符串比较是**逐字符**的：`'9.96' > '79.79'`（`'9' > '7'`）。

> **约定：凡这类列，一律先 `CAST(x AS DOUBLE)` 再参与任何计算。**

## 为什么这两个坑最值钱

它们**不会让你报错**，只会让你做出一个漂亮的假回测。
