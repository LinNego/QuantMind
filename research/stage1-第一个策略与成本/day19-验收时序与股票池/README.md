# D19 · 验收 ①②

> 阶段 1 · 第一个策略与成本 ｜ 第 19 天 ｜ 状态：⬜ 未开始

## 今天要干嘛

用验收清单的第 ①② 问过一遍代码，**指着某一行说「这里保证时序」**。

## 要看懂什么

- [验收清单](../../参考/验收清单.md) 第 ① 问（未来数据）与第 ② 问（股票池）

## 具体做法

### ① 未来数据：5 个 grep

```bash
grep -n "shift" code.py
grep -n "future_return" code.py
grep -n "SELECT \*" code.py
grep -n "ffill\|fillna(method" code.py
grep -n "\.rank(" code.py
```

然后跑**三档 shift 对比**：

```bash
python research/stage1-第一个策略与成本/day19-验收时序与股票池/toy_backtest.py
```

### ② 股票池：4 项过滤逐条核对

对着 D13 写的四个数字，看代码里是不是真的实现了，剔掉的只数对不对得上。

## 完成判据

能指着某一行说「这里保证时序」；三档 shift 差异能解释。

## 当天总结

### 玩具回测已经预演过这个对比

同一条信号，只改约束：

| 版本 | 改了什么 | 年化 |
|---|---|---|
| ① naive | 无 shift、无成本 | **+34,817%** |
| ② fixed | 加 T+1 时序 | **−39.8%** |
| ③ +cost | 加真实成本 | **−41.95%** |
| ④ cost×1.7 | 成本翻到 0.3%/边 | **−43.42%** |

**从 +34,817% 到 −42%，只改了「什么时候能成交」和「手续费多少」。信号一个字母没动。**

图：[toy_nav.png](toy_nav.png) ｜ 指标：[toy_backtest_metrics.csv](toy_backtest_metrics.csv)

### 判定标准

- 有 `shift(1)` 或等价的对齐 → 时序对
- 有 `future_return_*` → **立刻打回**
- 有 `SELECT *` → **立刻打回**（会把 6 个泄漏列带进来）
- 股票池四项缺任意一项 → 打回

## 本目录文件

- [toy_backtest.py](toy_backtest.py)
- [toy_backtest_metrics.csv](toy_backtest_metrics.csv)
- [toy_nav.png](toy_nav.png)

---

[← 返回阶段总览](../../README.md) ｜ [上一天 D18 →](../day18-第一次交付/README.md) ｜ [下一天 D20 →](../day20-验收成本与窗口/README.md)
