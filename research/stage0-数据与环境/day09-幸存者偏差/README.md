# D09 · 幸存者偏差

> 阶段 0 · 数据与环境 ｜ 第 9 天 ｜ 状态：✅ 已完成

## 今天要干嘛

回答「我的回测是不是天生就虚高」。

## 要看懂什么

- [常见陷阱与自检红线](../../参考/常见陷阱与自检红线.md) 第 3 节（幸存者偏差）

## 具体做法

算：每年出现过的股票里，有多少在**下一年彻底消失**。

```sql
WITH y AS (SELECT dt // 10000 AS yr, symbol FROM kf GROUP BY 1, 2)
SELECT a.yr AS 年份, a.n AS 当年股票数, coalesce(b.gone, 0) AS 次年消失
FROM (SELECT yr, count(*) AS n FROM y GROUP BY 1) a
LEFT JOIN (
  SELECT y1.yr, count(*) AS gone FROM y y1
  LEFT JOIN y y2 ON y1.symbol = y2.symbol AND y2.yr = y1.yr + 1
  WHERE y2.symbol IS NULL AND y1.yr < (SELECT max(yr) FROM y) GROUP BY 1
) b USING (yr) ORDER BY 1;
```

跑出来别急着接受——**追查那几只到底是谁**：

```sql
SELECT symbol AS 代码, min(dt) AS 首次, max(dt) AS 末次, count(*) AS 行数
FROM kf WHERE symbol IN ('000029.SZ','000670.SZ','002075.SZ','600733.SH') GROUP BY 1;
```

## 完成判据

得到一张「每年消失股票数」表，并且**追查过那几只的末次日期**。

## 当天总结

**十年退市 0 只。** 只有 4 只「消失」，全部 `末次 = 20261023` → 停牌后**复牌了**。

| 代码 | 现名 | 停牌起因 | 缺多少 |
|---|---|---|---|
| `000029.SZ` | 深深房A | 恒大借壳重组 | 1,006 天 ≈ 4.1 年 |
| `600733.SH` | 前锋股份→北汽蓝谷 | 重组停牌 | 496 天 ≈ 2.0 年 |
| `002075.SZ` | 沙钢股份 | 重大资产重组 | 544 天 ≈ 2.2 年 |
| `000670.SZ` | 盈方微 | 停牌 | 695 天 ≈ 2.9 年 |

**→ 这份数据里没有退市股。所有回测都含幸存者偏差，低 PE 策略受影响最大，粗估年化虚高 1% 上下。**

详细推导与量级估算：[output.md](output.md)。

> 今天学到的：量化里「数据看着很干净」通常不是好消息。

## 本目录文件

- [output.md](output.md)

---

[← 返回阶段总览](../../README.md) ｜ [上一天 D08 →](../day08-复权三兄弟/README.md) ｜ [下一天 D10 →](../day10-两个暗坑/README.md)
