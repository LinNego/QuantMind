import pandas as pd, numpy as np, warnings
warnings.filterwarnings('ignore')
u=pd.read_parquet('analysis_dip/cache/unadj.parquet')
b=pd.read_parquet('analysis_dip/cache/back.parquet')
o=u.pivot(index='dt',columns='symbol',values='open').sort_index()


c=u.pivot(index='dt',columns='symbol',values='close').sort_index()
cb=b.pivot(index='dt',columns='symbol',values='close').sort_index()
ob=b.pivot(index='dt',columns='symbol',values='open').sort_index()
dt=c.index; syms=c.columns
ret=c.pct_change(fill_method=None)
pre2=pd.Series(syms).str[:2]
board=pd.Series(np.select([pre2.isin(['60']),pre2.isin(['00']),pre2.isin(['30']),pre2.isin(['68']),pre2.isin(['43','83','87','88','92'])],
 ['沪主板','深主板','创业板','科创板','北交所'],'其他'),index=syms)
gem20=pd.Series(dt>=20200824,index=dt)
pct=pd.DataFrame(10.0,index=dt,columns=syms)
pct.loc[:,board[board=='创业板'].index]=np.where(gem20.values[:,None],20.0,10.0)
pct.loc[:,board[board=='科创板'].index]=20.0
pct.loc[:,board[board=='北交所'].index]=30.0
prev=c.shift(1)
lim_px=(prev*(1-pct/100)).round(2)
isld=ret<=-(pct/100-0.003)
# sealed: close at/below limit price (never opened) -> close==lim_px essentially
sealed = isld & (c<=lim_px+0.011)
opened = isld & (c>lim_px+0.011)
events=[20160107,20160111,20160126,20160225,20180619,20181011,20190506,20200203,20240205,20241009,20250407]
rows=[]
for e in events:
    i=int(np.where(dt==e)[0][0])
    f=lambda m,n: m.iloc[i+n]
    seg=pd.DataFrame({'event':e,'sym':syms})
    seg['sealed']=sealed.iloc[i].values
    seg['opened']=opened.iloc[i].values
    seg['ret']=ret.iloc[i].values
    seg['px']=c.iloc[i].values; seg['lim']=lim_px.iloc[i].values
    seg['t1']=(f(cb,1)/f(cb,0)-1).values
    seg['gap1']=(f(ob,1)/f(cb,0)-1).values
    seg['t1_ld']=(ret.iloc[i+1].values<=-(pct/100-0.003).iloc[i+1].values)
    seg['t1_sealed']=sealed.iloc[i+1].values
    seg['t5']=(f(cb,5)/f(cb,0)-1).values
    seg['t20']=(f(cb,20)/f(cb,0)-1).values
    seg['t2_ld']=(ret.iloc[i+2].values<=-(pct/100-0.003).iloc[i+2].values)
    rows.append(seg[seg.sealed|seg.opened])
S=pd.concat(rows)
S=S.dropna(subset=['t20'])
print('样本',len(S),'其中封死',int(S.sealed.sum()),'打开',int(S.opened.sum()))
print('\n盘中打开跌停的占比 by event:')
g=S.groupby('event').agg(n=('sym','size'),sealed=('sealed','mean'))
g['opened']=1-g.sealed; print(g.round(3).to_string())
print('\n封死 vs 打开 (跌停价=当日收盘价)')
print(S.groupby('sealed')[['t1','gap1','t1_ld','t2_ld','t5','t20']].median().round(3).to_string())
print('\n封死的股票 次日继续跌停比例', round(S[S.sealed].t1_ld.mean(),3), ' 打开的:', round(S[S.opened].t1_ld.mean(),3))
# 买在跌停价 vs 买在收盘（仅打开组：收盘价高于跌停价）
S['buy_lim']=(1+S.t20)*(S.px/S.lim)-1
S['buy_close']=S.t20
op=S[S.opened]
print('\n打开组：跌停价买入 vs 收盘买入, 20日')
print(pd.DataFrame({'跌停价':op.buy_lim,'收盘':op.buy_close}).median().round(3).to_string())
print('跌停价买入20日胜率',round((op.buy_lim>0).mean(),3),' 收盘买入',round((op.buy_close>0).mean(),3))
S.to_parquet('analysis_dip/cache/sealed.parquet')
