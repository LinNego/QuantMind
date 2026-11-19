# D1 · 11-17（周一）· A1 规模与断层 + A2 幸存者偏差

> 3h ｜ 产出：`out/a1_counts.csv`、`out/a2_notes.md` ｜ 对应 `plan/n01.md` §2-A
> 今天不碰因子、不碰成本，只回答一件事：**这份数据里，哪些股票看不见？**

## 0 开工（5 分钟）

```bash
cd /root/QuantMind && source .venv/bin/activate
python plan/n01/tools/env_check.py     # 最后一行应为「环境就绪 ✅」
mkdir -p plan/n01/out
```

## 1 A1 规模与断层（约 1h）

### 1.1 每天有多少只票

```bash
python - <<'PY'
import sys; sys.path.insert(0, 'plan/n01/tools')
import qm, pandas as pd

df = qm.kline(adj='unadjusted', columns=['symbol', 'dt'])
daily = df.groupby('dt')['symbol'].nunique().rename('n').reset_index()
daily['prev'] = daily['n'].shift()
daily['chg'] = daily['n'] / daily['prev'] - 1
daily.to_csv('plan/n01/out/a1_counts.csv', index=False)

print('交易日数:', len(daily), '| 区间:', daily.dt.min(), '→', daily.dt.max())
print('股票数: 首日', daily.n.iloc[0], '→ 末日', daily.n.iloc[-1], '| 峰值', daily.n.max())
print('单日骤降 >30% 的天数:', (daily.chg < -0.3).sum())
print('单日暴增 >30% 的天数:', (daily.chg > 0.3).sum())
print(daily[daily.chg < -0.1].to_string(index=False))   # 更大的跌法单独看
PY
```

**预期看到（对照值，不是标准答案）**

| 指标 | 值 |
|---|---|
| 交易日数 | 2,638（2016-01-04 → 2026-11-13） |
| 股票数 | 2,355 → 5,579，峰值 5,580 |
| 单日骤降 >30% | **0 天** |
| 单日暴增 >30% | 0 天 |

### 1.2 怎么解读

- 没有骤降**不等于**数据完美：可能是缺口被上游补齐了（每天按日历补齐），也可能是缺失的票干脆没进库。要区分这两种，看 A2。
- 股票数只增不减 → 新股持续上市，而退市股几乎没出现 → 已经能闻到幸存者偏差。
- 再挑一个 2024 年 1 月（微盘股流动性危机）看看数量有没有异常减少，写进记录。

### 1.3 A1 验收

- [ ] `out/a1_counts.csv` 有 2,638 行，带 `n/prev/chg` 三列
- [ ] 能说出「骤降 >30% 的有几天」——这次是 0 天，写清你信不信这个 0
- [ ] 能说出股票数从 3000 量级涨到 5000 量级发生在哪几年

## 2 A2 幸存者偏差（约 1.5h）

### 2.1 先看「当前名单」里有没有退市股

```bash
python - <<'PY'
import sys; sys.path.insert(0, 'plan/n01/tools')
import qm, pandas as pd

inst = qm.instrument_detail()
print('instrument_detail 行数:', len(inst))
print('IsQuitGP 分布:', inst['IsQuitGP'].value_counts(dropna=False).to_dict())
print('IsSTGP 分布:', inst['IsSTGP'].value_counts(dropna=False).to_dict())
print('上市日期范围:', inst['J_start'].min(), '→', inst['J_start'].max())
PY
```

**预期**：5,581 行，`IsQuitGP` **全是 0**（没有一只退市股），ST 196 只。

### 2.2 再从行情反推：谁「消失」了

```bash
python - <<'PY'
import sys; sys.path.insert(0, 'plan/n01/tools')
import qm, pandas as pd

df = qm.kline(adj='unadjusted', columns=['symbol', 'dt', 'volume'])
last = df.groupby('symbol')['dt'].max().rename('last_dt')
gone = last[last < 20261101]          # 2026-11 之后仍在交易 = 没退
print('代码总数:', len(last), '| 已停止交易:', len(gone))
print(gone.groupby(gone.astype(str).str[:4]).size().to_string())
PY
```

**预期**：5,584 个代码，只有 **4 只**在 2026-11 前停止交易（都在 2026 年）。

### 2.3 把差距量化（今天最重要的数字）

写一个 `out/a2_notes.md`，回答三件事：

1. **缺的是哪类股票？** 答：退市股、以及退市前的「壳/问题股」；它们通常在被 ST、面值退市前有一波暴跌，回测里从没买到过。
2. **每年「消失」多少？** 参考现实：A 股 2016–2025 每年退市 10–50 家，累计 300+；而这份数据 10 年只消失了 4 只 → **数据滞后于现实**。
3. **年化虚高多少（量级即可）？** 用「被剔除的股票在退市前一年平均跌 30–50%、占比按每年 30 家 / 5000 只 ≈ 0.6%」粗算：
   `0.6% × 40% ≈ 0.24%/年`，若把「跌得最惨的 5% 股票」算进来，量级约 **1–3 个百分点/年**。
   不用算精确，写「约 X 个点，量级 1–3 个点」即可，D9 结算时再复核。

### 2.4 A2 验收

- [ ] 能给出两个数字：**当前名单 5,581 只、退市股 0 只**
- [ ] 能给出「每年消失 N 只」：数据里 ≈0，现实里 ≈10–50 → 偏差成立
- [ ] 能给出「虚高几个点」的量级（1–3 个点）
- [ ] `out/a2_notes.md` 落盘，三问各一段

## 3 收尾（5 分钟）

`log.md` 写三行（「看到」必须是数字）：

```text
11-17  A1+A2
看到：2638 个交易日，股票数 2355→5579，骤降>30% 0 天；instrument_detail 5581 只里退市股 0 只
卡住：
明天：A3 单位自洽（先拿 3 只票手算）
```

**坑提醒**

- `dt` 是 `int`（20160104），不是日期字符串；用 `20260101` 比较，别用 `'2026-01-01'`。
- `20261101` 是「仍在交易」的阈值：数据截止 2026-11-13，卡在 11 月前就停的才算消失。
- 停牌 ≠ 退市：长时间停牌也会在 `last_dt` 上表现为「消失」，所以 4 只里可能混着停牌，看 `volume` 末段是否归零再判断。
