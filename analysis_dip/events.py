import pandas as pd, numpy as np

u=pd.read_parquet('analysis_dip/cache/unadj.parquet')
# wide matrices
px=u.pivot(index='dt', columns='symbol', values='close').sort_index()
amt=u.pivot(index='dt', columns='symbol', values='amount').sort_index()
ret=px.pct_change(fill_method=None)

syms=px.columns.to_numpy()
board=np.where(pd.Series(syms).str[:2].isin(['60']),'SH_main',
      np.where(pd.Series(syms).str[:2].isin(['00']),'SZ_main',
      np.where(pd.Series(syms).str[:2].isin(['30']),'GEM',
      np.where(pd.Series(syms).str[:2].isin(['68']),'STAR',
      np.where(pd.Series(syms).str[:2].isin(['43','83','87','88','92']),'BSE','OTHER')))))
board=pd.Series(board, index=syms)
dt_idx=px.index
gem20 = pd.Series(dt_idx>=20200824, index=dt_idx)
limit=pd.DataFrame(10.0, index=dt_idx, columns=syms)
limit.loc[:, board[board=='GEM'].index]=np.where(gem20.values[:,None],20.0,10.0)
limit.loc[:, board[board=='STAR'].index]=20.0
limit.loc[:, board[board=='BSE'].index]=30.0
# limit-down: ret <= -(limit-0.3)%
ld = ret <= -(limit/100 - 0.003)
ld_cnt = ld.sum(axis=1)
res=pd.DataFrame({'limit_down':ld_cnt,'total':px.notna().sum(axis=1)})
res['pct']=res.limit_down/res.total*100
res.to_csv('analysis_dip/cache/breadth.csv')
top=res.sort_values('limit_down',ascending=False).head(40)
print(top.to_string())
print('\n>=1000 days:', (res.limit_down>=1000).sum())
print(res[res.limit_down>=1000].to_string())
