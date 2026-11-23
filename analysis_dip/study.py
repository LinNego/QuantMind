import pandas as pd, numpy as np, json

u=pd.read_parquet('analysis_dip/cache/unadj.parquet')
b=pd.read_parquet('analysis_dip/cache/back.parquet')
pxu=u.pivot(index='dt',columns='symbol',values='close').sort_index()
pxb=b.pivot(index='dt',columns='symbol',values='close').sort_index()
amt=u.pivot(index='dt',columns='symbol',values='amount').sort_index()
dt=pxu.index
syms=pxu.columns

# boards
pre2=pd.Series(syms).str[:2]
board=pd.Series(np.select(
    [pre2.isin(['60']),pre2.isin(['00']),pre2.isin(['30']),pre2.isin(['68']),pre2.isin(['43','83','87','88','92'])],
    ['沪主板','深主板','创业板','科创板','北交所'],'其他'),index=syms)

events=[20160107,20160111,20160126,20160225,20180619,20181011,20190506,20200203,20240205,20241009,20250407]
iT=[int(np.where(dt==e)[0][0]) for e in events]

# limit-down flag for grouping
ret=pxu.pct_change(fill_method=None)
gem20=pd.Series(dt>=20200824,index=dt)
lim=pd.DataFrame(10.0,index=dt,columns=syms)
lim.loc[:,board[board=='创业板'].index]=np.where(gem20.values[:,None],20.0,10.0)
lim.loc[:,board[board=='科创板'].index]=20.0
lim.loc[:,board[board=='北交所'].index]=30.0
ld=ret<=-(lim/100-0.003)

def fwd(mat,i,n,offset=0):
    j=i+offset+n
    if j>=len(dt): return None
    return mat.iloc[j]/mat.iloc[i+offset]-1

rows=[]
for e,i in zip(events,iT):
    # groups at T
    a20=amt.iloc[max(0,i-20):i].mean()
    r20=pxb.iloc[i]/pxb.iloc[max(0,i-21)]-1
    r60=pxb.iloc[i]/pxb.iloc[max(0,i-61)]-1
    vol=pxb.iloc[max(0,i-20):i].pct_change(fill_method=None).std()
    g=pd.DataFrame({'symbol':syms,'board':board.values,
                    'ld':ld.iloc[i].values,'a20':a20.values,'r20':r20.values,
                    'r60':r60.values,'vol':vol.values,'price':pxu.iloc[i].values,
                    'f5':fwd(pxb,i,5).values,'f10':fwd(pxb,i,10).values,'f20':fwd(pxb,i,20).values,
                    'f60':fwd(pxb,i,60).values})
    # risk: min close within 20d vs T
    seg=pxb.iloc[i:i+21]
    g['mdd20']=(seg.min()/pxb.iloc[i]-1).values
    g['event']=e
    g=g.dropna(subset=['f20'])
    rows.append(g)
G=pd.concat(rows,ignore_index=True)
G.to_parquet('analysis_dip/cache/groups.parquet')
print(G.shape, G.event.nunique())
print(G.groupby('event')[['f5','f10','f20','mdd20']].median().round(3).to_string())
