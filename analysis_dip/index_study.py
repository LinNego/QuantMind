import pandas as pd, numpy as np
idx=pd.read_parquet('analysis_dip/cache/index.parquet')
px=idx.pivot(index='dt',columns='symbol',values='close').sort_index()
op=idx.pivot(index='dt',columns='symbol',values='open').sort_index()
dt=px.index
names={'000001.SH':'上证指数','000300.SH':'沪深300','000905.SH':'中证500','000852.SH':'中证1000','399006.SZ':'创业板指','000016.SH':'上证50','000688.SH':'科创50'}
events=[20160107,20160111,20160126,20160225,20180619,20181011,20190506,20200203,20240205,20241009,20250407]
rows=[]
for e in events:
    i=int(np.where(dt==e)[0][0])
    for s in names:
        if s not in px: continue
        c0=px[s].iloc[i]
        seg=px[s].iloc[i:i+21]
        rows.append(dict(event=e,code=s,name=names[s],
            gap=op[s].iloc[i+1]/c0-1,d1=px[s].iloc[i+1]/c0-1,
            d5=px[s].iloc[i+5]/c0-1,d10=px[s].iloc[i+10]/c0-1,
            d20=px[s].iloc[i+20]/c0-1,
            mdd=(seg.min()/c0-1),
            pre_dd=(c0/px[s].iloc[max(0,i-250):i+1].max()-1)))
R=pd.DataFrame(rows)
R.to_csv('analysis_dip/cache/index_events.csv',index=False)
print(R.pivot(index='event',columns='code',values='d20').round(3).to_string())
print('\nsummary by index')
print(R.groupby('name')[['gap','d1','d5','d20','mdd','pre_dd']].agg(['mean','median']).round(3).to_string())
print('\nSSE pre-event drawdown & T+20')
print(R[R.code=='000001.SH'][['event','pre_dd','gap','d1','d20','mdd']].round(3).to_string())
