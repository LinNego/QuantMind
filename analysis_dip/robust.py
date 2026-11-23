import pandas as pd, numpy as np, warnings
warnings.filterwarnings('ignore')
ev_all=[20160107,20160111,20160126,20160225,20180619,20181011,20190506,20200203,20220425,20240205,20240416,20241009,20250407]
u=pd.read_parquet('analysis_dip/cache/unadj.parquet'); b=pd.read_parquet('analysis_dip/cache/back.parquet')
pxu=u.pivot(index='dt',columns='symbol',values='close').sort_index()
pxb=b.pivot(index='dt',columns='symbol',values='close').sort_index()
amt=u.pivot(index='dt',columns='symbol',values='amount').sort_index()
dt=pxu.index; syms=pxu.columns
pre2=pd.Series(syms).str[:2]
board=pd.Series(np.select([pre2.isin(['60']),pre2.isin(['00']),pre2.isin(['30']),pre2.isin(['68']),pre2.isin(['43','83','87','88','92'])],
 ['沪主板','深主板','创业板','科创板','北交所'],'其他'),index=syms)
rows=[]
for e in ev_all:
    i=int(np.where(dt==e)[0][0])
    a20=amt.iloc[max(0,i-20):i].mean(); r20=pxb.iloc[i]/pxb.iloc[max(0,i-21)]-1
    g=pd.DataFrame({'event':e,'board':board.values,'r20':r20.values,'a20':a20.values,
                    'f20':(pxb.iloc[i+20]/pxb.iloc[i]-1).values})
    g['r20q']=pd.qcut(g.r20,5,labels=['Q1','Q2','Q3','Q4','Q5'],duplicates='drop')
    g['szq']=pd.qcut(g.a20,5,labels=['S1','S2','S3','S4','S5'],duplicates='drop')
    rows.append(g.dropna(subset=['f20']))
G=pd.concat(rows)
for c in ['board','r20q','szq']:
    t=G.groupby(['event',c])['f20'].median().unstack()
    print(c); print(pd.DataFrame({'mean_med':t.mean(),'pos':(t>0).sum(),'n':t.notna().sum()}).round(3).to_string()); print()
print('pooled stock win rate', (G.f20>0).mean().round(3), 'median', G.f20.median().round(3), 'mean', G.f20.mean().round(3))
print('p10/p25/p75/p90', G.f20.quantile([.1,.25,.75,.9]).round(3).to_dict())
