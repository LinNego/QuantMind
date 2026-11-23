import pandas as pd, numpy as np, warnings
warnings.filterwarnings('ignore')
G=pd.read_parquet('analysis_dip/cache/groups.parquet')
inst=pd.read_parquet('data/quantdb/2_base_sector/instrument_detail/instrument_list.parquet')
G=G.join(inst.set_index('Symbol')[['Name','rs_hyname','BelongHS300','Ltsz','DynaPE','PB_MRQ','DYRatio']],on='symbol')
G['ex']=G.groupby('event')['f20'].transform('median')
G['rel']=G.f20-G.ex   # within-event excess vs all-stock median

def rel(col,bins=None,labels=None):
    d=G.dropna(subset=['rel',col]).copy()
    if bins: d['g']=pd.qcut(d[col],bins,labels=labels,duplicates='drop'); key='g'
    else: key=col
    t=d.groupby(['event',key])['rel'].median().unstack()
    return pd.DataFrame({'rel_med':t.mean(),'pos_events':(t>0).sum(),'events':t.notna().sum()}).round(3)

print('board'); print(rel('board'))
print('\nld x r20'); 
G['r20q']=G.groupby('event')['r20'].transform(lambda s: pd.qcut(s,5,labels=['Q1','Q2','Q3','Q4','Q5'],duplicates='drop'))
print(rel('r20q'))
print('\nld only x r20q'); print(rel('r20q') if False else G[G.ld].groupby(['event','r20q'])['rel'].median().unstack().mean().round(3))
print('\nsize'); print(rel('a20',5,['Q1small','Q2','Q3','Q4','Q5big']))
print('\nhs300'); print(rel('hs300'))

# industry
d=G.dropna(subset=['rs_hyname'])
t=d.groupby(['event','rs_hyname'])['rel'].median().unstack()
n=d.groupby(['event','rs_hyname'])['rel'].count().unstack()
out=pd.DataFrame({'rel_med':t.mean(),'pos':(t>0).sum(),'events':t.notna().sum(),'avg_n':n.mean()})
out=out[out.events>=8].sort_values('rel_med',ascending=False)
print('\nINDUSTRY top15'); print(out.head(15).round(3).to_string())
print('\nINDUSTRY bottom10'); print(out.tail(10).round(3).to_string())
out.to_csv('analysis_dip/cache/industry_rel.csv')

# per event board rel
print('\nper-event board rel'); print(G.groupby(['event','board'])['rel'].median().unstack().round(3).to_string())
