import pandas as pd, numpy as np, warnings
warnings.filterwarnings('ignore')
idx=pd.read_parquet('analysis_dip/cache/index.parquet')
p=idx.pivot(index='dt',columns='symbol',values='close').sort_index()
dt=p.index
events=[20160107,20160111,20160126,20160225,20180619,20181011,20190506,20200203,20240205,20241009,20250407]
codes=['000001.SH','000300.SH','000905.SH','000852.SH','399006.SZ','000016.SH','000688.SH']
print('buy at T close vs wait to T+5 close, holding to T+20')
rows=[]
for e in events:
    i=int(np.where(dt==e)[0][0])
    for s in codes:
        c0=p[s].iloc[i]
        rows.append(dict(event=e,code=s,t20=p[s].iloc[i+20]/c0-1,wait5=p[s].iloc[i+20]/p[s].iloc[i+5]-1,
                         wait10=p[s].iloc[i+20]/p[s].iloc[i+10]-1))
R=pd.DataFrame(rows)
print(R.groupby('code')[['t20','wait5','wait10']].median().round(3).to_string())

# stock level wait comparison
G=pd.read_parquet('analysis_dip/cache/groups2.parquet')
print('\nstock median T+20 by event (pooled all):', G.groupby('event').f20.median().round(3).to_dict())

# industry: securities etc
ind=G.dropna(subset=['rs_hyname'])
sec=ind[ind.rs_hyname.str.contains('证券|保险|银行')]
print('\nfinancials rel:'); print(sec.groupby('rs_hyname').agg(n=('rel','size'),rel=('rel','median'),f20=('f20','median')).round(3).to_string())
# worst/best per event top industries
t=ind.groupby(['event','rs_hyname'])['rel'].median().unstack()
print('\n2016 events board rel'); print(G[G.event<20170000].groupby(['event','board'])['rel'].median().unstack().round(3).to_string())
print('\n2024/2025 events board rel'); print(G[G.event>=20240000].groupby(['event','board'])['rel'].median().unstack().round(3).to_string())
# conditional on pre-drawdown: add SSE pre_dd to each event
sse=p['000001.SH']
pre={}
for e in events:
    i=int(np.where(dt==e)[0][0]); pre[e]=sse.iloc[i]/sse.iloc[max(0,i-250):i+1].max()-1
G['pre_dd']=G.event.map(pre)
print('\npre_dd by event', {k:round(v,3) for k,v in pre.items()})
print('\ndeep vs shallow drawdown (stock medians):')
G['deep']=G.pre_dd<-0.15
print(G.groupby(['deep','board'])['f20'].median().unstack().round(3).to_string())
