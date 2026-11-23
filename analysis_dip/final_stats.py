import pandas as pd, numpy as np, warnings
warnings.filterwarnings('ignore')
G=pd.read_parquet('analysis_dip/cache/groups2.parquet')
print('pooled n',len(G),'win',round((G.f20>0).mean(),3),'med',round(G.f20.median(),3),'mean',round(G.f20.mean(),3))
print('q', G.f20.quantile([.1,.25,.5,.75,.9]).round(3).to_dict())
# event-level table
t=G.groupby('event').agg(n=('f20','size'),med20=('f20','median'),pos=('f20',lambda s:(s>0).mean()),meddd=('mdd20','median')).round(3)
print(t.to_string()); t.to_csv('analysis_dip/cache/event_table.csv')
# group table for report
def tab(col,labels=None,bins=None):
    d=G.dropna(subset=[col]).copy()
    if bins: d['k']=pd.qcut(d[col],bins,labels=labels,duplicates='drop'); key='k'
    else: key=col
    tt=d.groupby(['event',key])['f20'].median().unstack()
    return pd.DataFrame({'median_20d':tt.mean(),'win_events':(tt>0).sum(),'n_events':tt.notna().sum(),'worst':tt.min()}).round(3)
for c,kw in [('board',{}),('r20q',{}),('szq',{}),('hs300',{})]:
    print('\n',c); print(tab(c,**kw).to_string())
