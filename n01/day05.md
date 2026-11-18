# D5 · 11-21 · B2 最简策略跑通（3h）

策略：**20 日反转、20 只等权、月调仓，先不加成本**。

- [ ] 选股：每月最后一个交易日，按过去 20 日收益升序取 20 只（跌得多的）
- [ ] 持仓：等权，持一个月
- [ ] 输出：年化、最大回撤、年换手率
- 验收：三条数字都出来（哪怕只有 1 年数据）；曲线存 `n01/out/b2_nav.csv`

```bash
source .venv/bin/activate
python - <<'PY'
import duckdb, pandas as pd
con = duckdb.connect()
# 只取收盘价，按票透视；先跑通再放大
q = """
select symbol, time, close
from read_parquet('data/quantdb/1_kline_data/daily_forward/dt=*/data.parquet')
where dt >= 20200101
"""
df = con.execute(q).df()
px = df.pivot(index='time', columns='symbol', values='close').sort_index()
px.to_parquet('n01/out/b2_close.parquet')
print(px.shape, px.index.min(), px.index.max())
PY
```

- 卡住就缩：先 1 年、200 只票，跑通再放大（不要一上来全市场）

## 当天记录

```text
11-21  B2
看到：
卡住：
明天：
```
