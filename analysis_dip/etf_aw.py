import pandas as pd, numpy as np, warnings
warnings.filterwarnings('ignore')
b=pd.read_parquet('analysis_dip/cache/back.parquet'); u=pd.read_parquet('analysis_dip/cache/unadj.parquet')
pxb=b.pivot(index='dt',columns='symbol',values='close').sort_index()
u2=u.pivot(index='dt',columns='symbol',values='amount').sort_index()
dt=pxb.index
inst=pd.read_parquet('data/quantdb/2_base_sector/instrument_detail/instrument_list.parquet').set_index('Symbol')
ind=inst.rs_hyname.reindex(pxb.columns)
BUCKET=pd.read_pickle('analysis_dip/cache/bucket.pkl') if False else None
exec(open('analysis_dip/etf_backtest.py').read().split('events=')[0].split('rows=[]')[0].replace("print(","#print("))
# 上式会重跑映射；简单起见直接重建
