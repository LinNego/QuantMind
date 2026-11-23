import pandas as pd, numpy as np, warnings
warnings.filterwarnings('ignore')
G=pd.read_parquet('analysis_dip/cache/groups.parquet')
inst=pd.read_parquet('data/quantdb/2_base_sector/instrument_detail/instrument_list.parquet')
G=G.join(inst.set_index('Symbol')[['Name','rs_hyname','BelongHS300','Ltsz','DynaPE','PB_MRQ','DYRatio']],on='symbol')
G['hs300']=G.BelongHS300.fillna(0).astype(int)
G['ex']=G.groupby('event')['f20'].transform('median')
G['rel']=G.f20-G.ex
G['r20q']=G.groupby('event')['r20'].transform(lambda s: pd.qcut(s,5,labels=['Q1','Q2','Q3','Q4','Q5'],duplicates='drop'))

def rel(col,bins=None,labels=None):
    d=G.dropna(subset=['rel',col]).copy()
    if bins: d['g']=pd.qcut(d[col],bins,labels=labels,duplicates='drop'); key='g'
    else: key=col
    t=d.groupby(['event',key])['rel'].median().unstack()
    return pd.DataFrame({'rel_med':t.mean(),'pos_events':(t>0).sum(),'events':t.notna().sum()}).round(3)

print('hs300'); print(rel('hs300'))
print('\nboard x ld')
G['bg']=G.board.astype(str)+'|'+np.where(G.ld,'跌停','未跌停')
print(rel('bg'))
print('\nr20q per event (rel)'); print(G.groupby(['event','r20q'])['rel'].median().unstack().round(3).to_string())
d=G.dropna(subset=['rs_hyname'])
t=d.groupby(['event','rs_hyname'])['rel'].median().unstack()
n=d.groupby(['event','rs_hyname'])['rel'].count().unstack()
out=pd.DataFrame({'rel_med':t.mean(),'pos':(t>0).sum(),'events':t.notna().sum(),'avg_n':n.mean()})
out=out[out.events>=8].sort_values('rel_med',ascending=False)
print('\nINDUSTRY top12'); print(out.head(12).round(3).to_string())
print('\nINDUSTRY bottom8'); print(out.tail(8).round(3).to_string())
out.to_csv('analysis_dip/cache/industry_rel.csv')
G.to_parquet('analysis_dip/cache/groups2.parquet')
