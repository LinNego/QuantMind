# D2 · 11-18（周二）· A3 单位自洽 + A5 字段可用时点

> 3h ｜ 产出：`out/data_dictionary_v1.md`（初稿）｜ 对应 `plan/n01.md` §2-A
> 今天要回答：**这些数字的单位到底是什么？哪一个字段是哪天才能用的？**
> 顺序不能反：单位没验过就去算成本，成本里的「万元/元」会再错一次。

## 0 开工（3 分钟）

```bash
cd /root/QuantMind && source .venv/bin/activate
python plan/n01/tools/env_check.py     # 看最后一行 + A3 口径那一行
```

## 1 A3 单位自洽（约 1.5h）

### 1.1 先做「全市场体检」，别一上来手算

```bash
python - <<'PY'
import sys; sys.path.insert(0, 'plan/n01/tools')
import qm

df = qm.kline(adj='unadjusted', start=20261102, end=20261113)
df['ratio'] = df['close'] * df['volume'] / df['amount']
print('样本:', len(df), '只票 × 天')
print(df['ratio'].describe(percentiles=[.01, .5, .99]).round(1).to_string())
PY
```

**预期**：中位数 ≈ **9,900**，1%–99% 分位约 **9,500–10,400**，即稳定在 1e4 附近。

### 1.2 为什么是 1e4（把它推出来，别背结论）

- `close` 单位 **元/股**；`volume` 单位 **股** → `close × volume` 单位 **元**；
- `amount` 单位 **万元**（1 万元 = 1e4 元）；
- 所以 `close × volume ÷ amount` 无量纲 ≈ **1e4**（元/(万元)）。
- 若某天比值变成 ~1e2 或 ~1e6，说明 `amount` 的单位变成了「亿元」或「元」——这是会让因子排序悄悄反掉的那种错。

### 1.3 抽 3 只票手算（必须亲手算一遍）

```bash
python - <<'PY'
import sys; sys.path.insert(0, 'plan/n01/tools')
import qm

df = qm.kline(adj='unadjusted', start=20261113, end=20261113,
              symbols=['600036.SH', '000001.SZ', '300750.SZ'])
for _, r in df.iterrows():
    ratio = r['close'] * r['volume'] / r['amount']
    print(f"{r['symbol']} 收 {r['close']:.2f} 量 {r['volume']:.0f} 额 {r['amount']:.0f} "
          f"→ 比值 {ratio:.0f}（手算: {r['close']:.2f}×{r['volume']:.0f}/{r['amount']:.0f}）")
PY
```

注意：`amount` 打印出来是「元」的数量级看着不对，是对的——它单位是万元。用「元」去算会得到 1e8，那就是单位错。

### 1.4 反例：复权价 × 原始成交额

```bash
python - <<'PY'
import sys; sys.path.insert(0, 'plan/n01/tools')
import qm

fwd = qm.kline(adj='forward', start=20261113, end=20261113, symbols=['600036.SH', '000001.SZ'])
raw = qm.kline(adj='unadjusted', start=20261113, end=20261113, symbols=['600036.SH', '000001.SZ'])
m = fwd.merge(raw[['symbol', 'amount']], on='symbol')
m['ratio'] = m['close'] * m['volume'] / m['amount']
print(m[['symbol', 'ratio']].round(0).to_string(index=False))
PY
```

**预期**：前复权算出 ≈ **5,800–6,500**，比 1e4 低了约 40%。不是数据错，是**口径混用**（复权价配原始成交额）。这条写进 D1 的「口径」列。

### 1.5 A3 验收

- [ ] 全市场比值中位数 9,500–10,400，且写出 1%/99% 分位
- [ ] 3 只票手算结果与程序一致（误差 <1%）
- [ ] 能解释「为什么前复权会算成 5,800」——口径混用

## 2 A5 字段可用时点（约 1.5h）

要回答的不是「字段什么时候产生」，而是「**我最早哪一天能拿它做决策**」。

### 2.1 财报：报告期 vs 公告日

```bash
python - <<'PY'
import sys; sys.path.insert(0, 'plan/n01/tools')
import qm, pandas as pd

for code in ['000001.SZ', '600036.SH', '000858.SZ']:
    inc = qm.financial('income', code)
    tt = pd.to_datetime(inc['m_timetag'])
    an = pd.to_datetime(inc['m_anntime'])
    lag = (an - tt).dt.days
    print(code, '报告期数:', len(inc), '| 公告滞后: 中位', lag.median(), '天, 最大', lag.max(), '天')
    print(inc[['m_timetag', 'm_anntime', 'actual_ann_dt']].tail(3).to_string(index=False))
PY
```

**预期**：单只票 40+ 期；报告期 → 公告日滞后 **中位 27 天**，最长 80 天（年报）；`actual_ann_dt` 大量为空。

结论：**财报字段的「可用日」= `m_anntime`（公告日），不是 `m_timetag`（报告期）**。
用报告期做因子 = 提前 20–80 天知道了还不知道的东西，是最典型的未来函数之一。

### 2.2 行情字段

- `close / open / high / low`：收盘后（15:00）可用，T 日数据 T 日收盘可得；
- `volume / amount`：同上；
- `dt`：分区日期，等于是「这一行属于哪一天」，不是「入库时间」。

### 2.3 落盘 D1 初稿

建 `out/data_dictionary_v1.md`，先填 10 个字段（含义 / 单位 / 口径 / 哪天能用）：

| # | 字段 | 含义 | 单位 | 口径 | 哪天能用 |
|---|---|---|---|---|---|
| 1 | close | 收盘价 | 元 | 前复权（`daily_forward`） | T 日 15:00 |
| 2 | open | 开盘价 | 元 | 同上 | T 日 09:30 |
| 3 | high | 最高价 | 元 | 同上 | T 日 15:00 |
| 4 | low | 最低价 | 元 | 同上 | T 日 15:00 |
| 5 | volume | 成交量 | 股 | 不复权原始量 | T 日 15:00 |
| 6 | amount | 成交额 | **万元** | 不复权原始额 | T 日 15:00 |
| 7 | dt | 分区日 | YYYYMMDD | 交易日 | — |
| 8 | symbol | 代码 | — | 后缀式 `600036.SH` | — |
| 9 | m_timetag | 财报报告期 | — | 季末/年末 | **不能用**（未来函数） |
| 10 | m_anntime | 财报公告日 | — | 实际披露日 | 公告日盘后 |

## 3 收尾（5 分钟）

```text
11-18  A3+A5
看到：
卡住：
明天：A4 复权三兄弟
```

**坑提醒**

- 财报每张表字段名不同（`income` 用 `m_timetag`，有些表用 `report_date`），先 `print(df.columns)` 再写逻辑；
- `actual_ann_dt` 是「实际公告日」但常为空，别指望；
- 同一天不同票的 `amount` 量级差很多是正常的（成交额本身差几个数量级），别用绝对值判断单位，用比值。
