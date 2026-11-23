import pyarrow.parquet as pq, pandas as pd, numpy as np, glob, os, time

BASE='data/quantdb/1_kline_data'
def load(folder, cols=('symbol','open','close','amount')):
    rows=[]
    files=sorted(glob.glob(f'{BASE}/{folder}/dt=*/data.parquet'))
    for f in files:
        dt=int(os.path.basename(os.path.dirname(f)).split('=')[1])
        t=pq.read_table(f, columns=list(cols)).to_pandas()
        t['dt']=dt
        rows.append(t)
    df=pd.concat(rows, ignore_index=True)
    return df

t0=time.time()
u=load('daily_unadjusted')
print('unadj',u.shape, round(time.time()-t0,1))
u.to_parquet('analysis_dip/cache/unadj.parquet', index=False)
b=load('daily_backward')
print('back',b.shape, round(time.time()-t0,1))
b.to_parquet('analysis_dip/cache/back.parquet', index=False)
