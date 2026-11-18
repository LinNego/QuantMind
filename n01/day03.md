# D3 · 11-19 · A4 复权三兄弟（2h）

目标：同一只票，前复权 / 后复权 / 不复权，各算 20 日收益，看差多少；挑一只分红送转大的票。

- [ ] 选一只近年分红/送转大的票（如 600036.SH、000858.SZ、601318.SH）
- [ ] 三个口径各算一次 20 日收益（同一个 20 日窗口）
- [ ] 记录三种结果的差；指出**哪一种会把未来信息带进来**

```bash
source .venv/bin/activate
python - <<'PY'
import duckdb
con = duckdb.connect()
for name in ['daily_forward','daily_backward','daily_unadjusted']:
    q = f"""
    select time, close from read_parquet(
      'data/quantdb/1_kline_data/{name}/dt=*/data.parquet')
    where symbol='600036.SH' and dt >= 20240101
    order by time
    """
    df = con.execute(q).df()
    print(name, '20日收益:', round(df['close'].iloc[20]/df['close'].iloc[0]-1, 4))
PY
```

- 验收：三种收益率的差值写出来（不是「有差别」，是差几个点）
- 验收：一句话回答「前复权为什么不能用未来信息做回测」（提示：除权日的口径变化）

## 当天记录

```text
11-19  A4
看到：
卡住：
明天：
```
