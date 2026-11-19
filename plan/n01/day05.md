# D5 · 11-21（周五）· B2 最简策略跑通

> 3h ｜ 产出：`out/b2_nav.csv`、`out/b2_metrics.md` ｜ 对应 §2-B B2
> 目标不是赚钱，是**把一条能跑的净值曲线做出来**：20 日反转、20 只等权、月调仓、不加成本。

## 1 回测器怎么用（先看 2 分钟）

`plan/n01/tools/backtest.py` 已把口径写死，参数可调：

| 参数 | 默认 | 含义 |
|---|---|---|
| `--top-n` | 20 | 持仓只数 |
| `--lookback` | 20 | 反转信号窗口（过去 N 日收益最低的 N 只） |
| `--cost` | 0 | 单边成本率（0.001 = 10bp） |
| `--exec` | next_open | 成交时点：`next_open`（次日开盘）/ `close`（当日收盘） |
| `--skip-limit-up` | 关 | 涨停不买 |
| `--skip-suspend` | 关 | 停牌不交易 |
| `--out` | — | 净值 CSV 路径 |

内部假设（写报告时要照抄）：等权；调仓日不可交易的票权重留现金；停牌持仓用上一可用收盘价估值；净值按持仓份额逐日 mark。

## 2 先缩样本跑通（20 分钟）

```bash
cd /root/QuantMind && source .venv/bin/activate
time python plan/n01/tools/backtest.py --start 20200101 --cost 0 --out plan/n01/out/b2_nav_debug.csv
```

跑通后再放全区间：

```bash
time python plan/n01/tools/backtest.py --cost 0 --out plan/n01/out/b2_nav.csv
```

**参考输出（我们这台机器实测，2026-11）**

全区间（2016-01-04 → 2026-11-13）：

```
年化收益  : -0.34%
最大回撤  : -70.38%
年换手    : 10.61x（单边）
期末净值  : 0.9632
```

只跑 2020 年以后（`--start 20200101`）：

```
年化收益  : +8.36%
最大回撤  : -37.53%
年换手    : 10.58x（单边）
期末净值  : 1.7354
```

同一个策略，前四年（2016–2019）把十年的收益全吃掉了。**这就是「样本区间选错」的典型例子**：如果只跑 2020 年以后，你会以为这个策略能赚钱。把两段都留着，D9 写「我犯的错」时可以用。

> 这个策略本身不赚钱很正常——20 日反转买「跌得最惨的 20 只」，在没有涨跌停/流动性/退市过滤的情况下就是接飞刀。B2 的意义是得到一条**基准线**，后面 B3/B4 才能量化「成本吃掉多少、假设吃掉多少」。

**你的数和参考差很多时，按顺序查**：

1. 区间对不对（`--start/--end`）；
2. 是否用了 forward 价（默认）；
3. 换手是否在 8–13x/年 量级（差太远说明选股/调仓频率不对）；
4. 有没有把停牌票按 0 元卖掉（那是 bug，当前代码已修）。

## 3 看净值曲线（10 分钟）

```bash
python - <<'PY'
import pandas as pd, matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

nav = pd.read_csv('plan/n01/out/b2_nav.csv', index_col=0, parse_dates=True)
nav['nav'].plot(figsize=(11, 4), title='B2 20日反转 20只等权（成本 0）')
plt.tight_layout(); plt.savefig('plan/n01/out/b2_nav.png', dpi=130)
print(nav.tail(3))
PY
```

把图里 2017、2018、2024-01 附近的跌幅圈出来——这几个时点后面 B4 要解释。

## 4 落盘 `out/b2_metrics.md`

写清：区间、年化、最大回撤、年换手、期末净值、以及一句「这条曲线是什么策略、什么口径」（供 D9 结算引用）。

## 5 验收

- [ ] `out/b2_nav.csv` 有 2,000+ 行日净值
- [ ] 三个指标都有：年化、最大回撤、年换手
- [ ] 能解释「为什么 20 日反转在这份数据上是负的」（至少两条原因：接飞刀、无退市/流动性过滤）
- [ ] `out/b2_metrics.md` 落盘

## 6 收尾

```text
11-21  B2
看到：年化 -0.34%，最大回撤 -70.4%，年换手 10.6x
卡住：
明天：B3 三档成本
```
