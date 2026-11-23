import pandas as pd, pyarrow.parquet as pq, glob
rows=[]
for f in sorted(glob.glob('data/quantdb/1_kline_data/index_daily/dt=*/data.parquet')):
    rows.append(pq.read_table(f).to_pandas())
df=pd.concat(rows, ignore_index=True)
df.to_parquet('analysis_dip/cache/index.parquet', index=False)
p=df.pivot(index='dt',columns='symbol',values='close').sort_index()
print(p[['000001.SH','399006.SZ','000852.SH']].loc[20260601:].to_string())
