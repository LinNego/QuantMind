# D2 · 11-18 · A3 单位自洽 + A5 字段可用时点（3h）

## A3 单位自洽（今天的硬指标）

- [ ] 抽 3 只票、3 个交易日，手算 `close × volume ÷ amount`
- [ ] 结果应稳定在 **1e4（10000）附近**；偏离 10×/100× 说明某列单位不是你以为的那个
- [ ] 把 3 只票的手算结果写进 log.md

```bash
source .venv/bin/activate
python - <<'PY'
import duckdb
con = duckdb.connect()
# 必须用【不复权】数据：前复权 close 含除权因子，与原始 amount 不同口径，比值会失真
q = """
select symbol, time, close, volume, amount,
       close*volume/amount as ratio
from read_parquet('data/quantdb/1_kline_data/daily_unadjusted/dt=*/data.parquet')
where symbol in ('600036.SH','000001.SZ','300750.SZ')
  and dt in (20240102, 20240603, 20250102)
"""
df = con.execute(q).df()
print(df)
PY
```

- 验收：ratio ≈ **9530–10000**（amount 单位是万元，volume 是股），三只票一致
- 反例（写进记录）：同一行用 `daily_forward` 算 ratio ≈ 5850 —— 不是数据错，是「复权价 × 原始成交额」混了口径

## A5 字段可用时点

- [ ] 价格：当天 15:00 后可用（T 日收盘后）——写清
- [ ] 成交额：同上
- [ ] 财报指标：对比 `report_date`（报告期）和 `available_date`/公告日，**财报字段提前了几天**（几周还是几个月）
- [ ] 结论落到 D1 的「哪一天能用」列

```bash
python - <<'PY'
import duckdb
con = duckdb.connect()
q = """
select * from read_parquet('data/quantdb/3_financial_data/income/*.parquet') limit 5
"""
df = con.execute(q).df()
print(df.columns.tolist())
print(df.head())
PY
```

- 验收：能指出「财报字段最早哪天能进因子」，并给出一个具体差值（天/周）

## 当天记录

```text
11-18  A3+A5
看到：
卡住：
明天：
```
