import pandas as pd, numpy as np, warnings
warnings.filterwarnings('ignore')
b=pd.read_parquet('analysis_dip/cache/back.parquet')
px=b.pivot(index='dt',columns='symbol',values='close').sort_index()
inst=pd.read_parquet('data/quantdb/2_base_sector/instrument_detail/instrument_list.parquet').set_index('Symbol')
dt=px.index
last=dt[-1]
peak_i=int(np.where(dt==20260625)[0][0])
def ret_since(start):
    s=px.loc[start] if start in px.index else px.iloc[np.searchsorted(dt,start)]
    return px.iloc[-1]/s-1
r=pd.DataFrame({'ytd':ret_since(20251231) if 20251231 in dt else px.iloc[-1]/px.loc[:20251231].iloc[-1]-1,
                'from_peak':px.iloc[-1]/px.loc[20260101:].max()-1,
                'since0625':px.iloc[-1]/px.iloc[peak_i]-1,
                'r20':px.iloc[-1]/px.iloc[-21]-1,
                'r60':px.iloc[-1]/px.iloc[-61]-1})
r=r.join(inst[['Name','rs_hyname','IsSTGP','DynaPE','PB_MRQ','DYRatio','Ltsz','BelongHS300']])
print('universe',len(r),'last dt',last)
print('\nmarket median ytd/from_peak/since0625/r20:', r[['ytd','from_peak','since0625','r20']].median().round(3).to_dict())
print('\nby board')
r['board']=np.select([r.index.str[:2].isin(['60']),r.index.str[:2].isin(['00']),r.index.str[:2].isin(['30']),r.index.str[:2].isin(['68'])],['沪主板','深主板','创业板','科创板'],'其他')
print(r.groupby('board')[['ytd','from_peak','since0625','r60']].median().round(3).to_string())
ind=r[r.IsSTGP==0].groupby('rs_hyname').agg(n=('Name','size'),since0625=('since0625','median'),from_peak=('from_peak','median'),r20=('r20','median'),pe=('DynaPE','median'),pb=('PB_MRQ','median'),dy=('DYRatio','median'))
ind=ind[ind.n>=10]
print('\nworst 15 industries since June peak'); print(ind.sort_values('since0625').head(15).round(3).to_string())
print('\nbest 10 industries since June peak'); print(ind.sort_values('since0625').tail(10).round(3).to_string())
ind.to_csv('analysis_dip/cache/industry_now.csv')
r.to_parquet('analysis_dip/cache/now.parquet')
