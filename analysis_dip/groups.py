import pandas as pd, numpy as np
G=pd.read_parquet('analysis_dip/cache/groups.parquet')
inst=pd.read_parquet('data/quantdb/2_base_sector/instrument_detail/instrument_list.parquet')
ind=inst.set_index('Symbol')[['Name','rs_hyname','BelongHS300','IsSTGP','DynaPE','PB_MRQ','DYRatio','Ltsz']]
G=G.join(ind,on='symbol')
G['hs300']=G.BelongHS300.fillna(0).astype(int)
G['isst']=G.IsSTGP.fillna(0).astype(int)

def agg(df,col,func='median'):
    return df.groupby(['event',col])['f20'].median().unstack()

def summarize(col,bins=None,labels=None,minn=20):
    d=G.dropna(subset=['f20',col]).copy()
    if bins is not None:
        d['g']=pd.qcut(d[col],bins,labels=labels,duplicates='drop')
        key='g'
    else:
        key=col
    t=d.groupby(['event',key])['f20'].median().unstack()
    n=d.groupby(['event',key])['f20'].count().unstack()
    out=pd.DataFrame({'mean_med':t.mean(),'events_pos':(t>0).sum(),'events':t.notna().sum(),
                      'worst':t.min(),'best':t.max(),'avg_n':n.mean()})
    return out.round(3)

print('=== board ==='); print(summarize('board'))
print('\n=== limit-down on T ==='); print(summarize('ld'))
print('\n=== prior 20d ret quintile ==='); print(summarize('r20',5,['Q1最弱','Q2','Q3','Q4','Q5最强']))
print('\n=== prior 60d ret quintile ==='); print(summarize('r60',5,['Q1','Q2','Q3','Q4','Q5']))
print('\n=== size (20d avg amount) quintile ==='); print(summarize('a20',5,['Q1最小','Q2','Q3','Q4','Q5最大']))
print('\n=== vol quintile ==='); print(summarize('vol',5,['Q1','Q2','Q3','Q4','Q5最高波']))
print('\n=== price bucket ===')
G['pb']=pd.cut(G.price,[0,5,10,20,50,1e9],labels=['<5','5-10','10-20','20-50','>50'])
print(summarize('pb'))
print('\n=== hs300 ==='); print(summarize('hs300'))
print('\n=== ST ==='); print(summarize('isst'))
