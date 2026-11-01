# D10 · 两个暗坑

> 阶段 0 · 数据与环境 ｜ 第 10 天 ｜ 状态：✅ 已完成

## 今天要干嘛

把两个「不报错但结果全错」的坑堵死。

## 要看懂什么

- [常见陷阱与自检红线](../../参考/常见陷阱与自检红线.md) 第 2.3 节（`future_return_*` 那一段）

## 具体做法

① 列出 `features_daily` 里所有 `future_return_*` 列，写一条断言：

> **读这张表时禁止 `SELECT *`，必须显式列出列名。**

② 列出所有 VARCHAR 类型的数值列，验证「按字符串排序」和「转数值后排序」结果不同：

```sql
SELECT hs_turnover FROM feat ORDER BY hs_turnover DESC LIMIT 3;             -- 字符串序
SELECT CAST(hs_turnover AS DOUBLE) h FROM feat ORDER BY h DESC LIMIT 3;     -- 数值序
```

## 完成判据

能举出 `hs_turnover` 的字典序反例，并说清为什么。

## 当天总结

**① 泄漏列：6 个**

`future_return_1d / 3d / 5d / 10d / 20d / 60d`

这些是**标签**，不是特征。`SELECT *` 把它们带进特征矩阵，你会得到 IC ≈ 0.9 的「完美模型」。

**② 字符串数值列：29 个**

`hs_turnover`、`zt_price`、`zaf`、`total_cap_yi` …

| 排序方式 | Top3 |
|---|---|
| 按字符串 | 9.96 / 9.96 / 9.91 |
| 转数值 | **79.79 / 46.63 / 45.60** |

因为字符串比较是**逐字符**的，`"9.96" > "79.79"`（`'9' > '7'`）。

**约定：凡这类列，一律先 `CAST(x AS DOUBLE)` 再参与任何计算。**

> 这两个坑最值钱的地方：它们不会让你报错，只会让你**做出一个漂亮的假回测**。

---

[← 返回阶段总览](../../README.md) ｜ [上一天 D09 →](../day09-幸存者偏差/README.md) ｜ [下一天 D11 →](../day11-整合脚本/README.md)
