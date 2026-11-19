# D8 · 11-24（周一）· B5 归因 + D2 三条净值 + D3 敏感表

> 3h ｜ 产出：`out/d2_nav_three.png`、`out/sensitivity.md`、`out/b5_attribution.md` ｜ 对应 §2-B B5、§1 D2/D3
> 今天是交付物日：把前六天的数字拼成三样能交差的东西。

## 1 B5 归因（约 1h）

把「成本影响」和「成交假设影响」拆开。用一个干净的参照点：

- **R0** = 成本 0 + 当日收盘成交（最乐观，几乎不可能实现）
- **R1** = 成本 0 + 次日开盘成交
- **R2** = 成本 10bp + 次日开盘成交
- **R3** = 成本 30bp + 次日开盘成交 + 三条成交规则

计算口径（写进报告）：

```
成交假设效应 = R1 − R0        （只改成交时点）
成本效应     = R2 − R1        （只改成本）
合计差异     = R3 − R0
交互项       = 合计 − 成交 − 成本   （应接近 0，不接近说明有交互，写出来）
```

参考输出（B3/B4 跑完后照抄）：

| 项 | 年化 | 占总差异 |
|---|---|---|
| R0 成本0 + 收盘 | | — |
| R1 成本0 + 次日开盘 | | |
| R2 成本10bp + 次日开盘 | | |
| R3 成本30bp + 全规则 | | |
| 成本效应 | | X% |
| 成交效应 | | Y% |

## 2 D2 三条净值（约 40 分钟）

```bash
python - <<'PY'
import pandas as pd, matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

files = {'0': 'plan/n01/out/b3_nav_0bp.csv',
         '0.1%': 'plan/n01/out/b3_nav_10bp.csv',
         '0.3%': 'plan/n01/out/b3_nav_30bp.csv'}
fig, ax = plt.subplots(figsize=(11, 4.5))
for label, path in files.items():
    nav = pd.read_csv(path, index_col=0, parse_dates=True)['nav']
    ax.plot(nav.index, nav / nav.iloc[0], label=f'cost {label}')
ax.set_title('20日反转 20只等权：三档成本净值')
ax.legend(); ax.grid(alpha=.3)
plt.tight_layout(); plt.savefig('plan/n01/out/d2_nav_three.png', dpi=140)
print('saved')
PY
```

验收：三条线同一张图，成本越高越差；图上标出「年化归零」的档位（本例是「没有归零点，0 成本已为负，需约 -X bp 返佣」）。

## 3 D3 假设敏感表（约 40 分钟）

一行一个假设，列年化，**用 B3/B4 的实测数填**：

| 假设 | 取值 | 年化 | 相对基准（R1）|
|---|---|---|---|
| 成本 | 0 / 0.1% / 0.3% | | |
| 成交时点 | 收盘 / 次日开盘 | | |
| 涨停 | 不滤 / 滤 | | |
| 停牌 | 不滤 / 滤 | | |

落盘 `out/sensitivity.md`，最后一行写：「影响最大的是 ___，年化差 ___ bp」。

## 4 D3 的三个数字（结算要引用）

1. 成本占几成：`(R2−R1) / (R3−R0)`；
2. 成交占几成：`(R1−R0) / (R3−R0)`；
3. 若交互项占比 >20%，在报告里解释为什么（成本随换手变化，换手又随成交假设变化）。

## 5 验收

- [ ] `out/d2_nav_three.png` 三线图
- [ ] `out/sensitivity.md` 四行假设 × 年化，带结论
- [ ] `out/b5_attribution.md` 有成本/成交占比两个百分比，且相加≈100%
- [ ] 能一句话回答 Q3：「回测比实盘好看，主要是 ___ 造成的」

## 6 收尾

```text
11-24  B5+D2+D3
看到：成本占 X%、成交占 Y%；影响最大的是 ___
卡住：
明天：结算（Q1–Q3 + 我犯的错）
```
