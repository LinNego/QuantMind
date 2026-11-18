# D1 · 11-17 · A1 规模与断层 + A2 幸存者偏差（3h）

目标：先回答「数据里有多少只票、有没有断层、过去消失了多少只」。

## A1 规模与断层

- [ ] 统计每个交易日 `daily_forward` 的股票数，画出（或打印）近 10 年曲线
- [ ] 找出**单日行数骤降 >30%** 的日子，数一共有几个
- [ ] 记下：股票数从 3000 量级涨到 5000 量级发生在哪几年

```bash
source .venv/bin/activate
python - <<'PY'
import duckdb
con = duckdb.connect()
# 分区内字段：symbol / time / close / volume / amount（amount 单位：万元）
q = """
select dt, count(*) n
from read_parquet('data/quantdb/1_kline_data/daily_forward/dt=*/data.parquet')
group by 1 order by 1
"""
df = con.execute(q).df()
df['d'] = df['dt']
df['prev'] = df['n'].shift()
df['chg'] = df['n']/df['prev']-1
print(df.head(), df.tail())
print('骤降>30% 天数:', (df['chg'] < -0.3).sum())
df.to_csv('n01/out/a1_daily_count.csv', index=False)
PY
```

- 验收：能说出「骤降的日子有几个」，并解释其中一个（是不是新股停发/数据缺口/交易日历问题）

## A2 幸存者偏差

- [ ] 用 `2_base_sector/instrument_detail` 看当前股票列表有多少只
- [ ] 对比每天出现过、后来「消失」的代码数（按年）
- [ ] 估算：消失的这批不进回测，会让年化虚高几个点（量级即可：1 个点？3 个点？）

```bash
python - <<'PY'
import pandas as pd
inst = pd.read_parquet('data/quantdb/2_base_sector/instrument_detail/instrument_list.parquet')
print(inst.shape, inst.columns[:10])
PY
```

- 验收：给出「每年消失 N 只」和「虚高约 X 个点」两个数字

## 当天记录（3 行，写进 log.md）

```text
11-17  A1+A2
看到：
卡住：
明天：
```
