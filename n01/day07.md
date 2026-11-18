# D7 · 11-23 · B4 成交假设（3h）

在 D6 的基础上加三条成交规则，看年化掉多少：

- [ ] **T+1**：信号日收盘出信号，**次日开盘**成交（不是当天收盘）
- [ ] **涨停不买**：次日开盘涨停（open ≈ 涨停价，或 pct_chg ≥ 9.9%）的票跳过
- [ ] **停牌不交易**：当日 vol == 0 / 无数据的票跳过

- [ ] 记录：加规则前后年化差多少（这一条通常就是 Q3 的答案）

```bash
python - <<'PY'
# 提示：用 daily_unadjusted 判断涨跌停/停牌更干净（原始价）
import duckdb
con = duckdb.connect()
print(con.execute("""
select * from read_parquet('data/quantdb/1_kline_data/daily_unadjusted/dt=*/data.parquet')
limit 3
""").df().columns.tolist())
PY
```

- 验收：能指出「年化掉的几个点里，T+1 占多少、涨停/停牌占多少」

## 当天记录

```text
11-23  B4
看到：
卡住：
明天：
```
