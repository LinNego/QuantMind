import pandas as pd, numpy as np, matplotlib
matplotlib.use('Agg'); import matplotlib.pyplot as plt
plt.rcParams.update({'figure.facecolor':'white','axes.facecolor':'white','axes.edgecolor':'#cccccc',
 'axes.grid':True,'grid.color':'#eeeeee','grid.linewidth':0.8,'font.size':9,
 'axes.spines.top':False,'axes.spines.right':False,'axes.axisbelow':True})
S=pd.read_csv('analysis_dip/cache/etf_summary_both.csv',index_col=0)
C=pd.read_csv('analysis_dip/cache/sector_now.csv',index_col=0)
S=S.join(C,how='left')
BROAD=['上证50ETF(510050)','沪深300ETF(510300)','中证500ETF(510500)','中证1000ETF(512100)','创业板ETF(159915)','科创50ETF(588000)','北证50ETF']
S['is_broad']=S.index.isin(BROAD)
S=S.sort_values('t20_mean')
fig,axes=plt.subplots(1,2,figsize=(15,8.2),gridspec_kw={'width_ratios':[1.55,1]})
ax=axes[0]; y=np.arange(len(S)); h=0.38
ax.barh(y+h/2,S.t20_mean*100,height=h,color=np.where(S.is_broad,'#2980b9','#e74c3c'),label='equal-weight')
ax.barh(y-h/2,S.aw_t20*100,height=h,color=np.where(S.is_broad,'#a9cce3','#f5b7b1'),label='liquidity-weighted')
ax.set_yticks(y); ax.set_yticklabels(S.index,fontsize=8.5)
ax.axvline(0,color='#333',lw=0.9)
for j,(v,w) in enumerate(zip(S.t20_mean*100,S.win*100)):
    ax.text(max(v,S.aw_t20.iloc[j]*100)+0.4,j,f'{v:+.1f}%  ({w:.0f}%)',va='center',fontsize=7.5)
ax.set_xlabel('average return over 20 trading days after the crash day (%)')
ax.set_title('Buy at T close, hold 20 days — average of 11 events (2016-2025)\nlabel: EW return (win rate across events)',loc='left',fontsize=10.5)
ax.legend(frameon=False,fontsize=8,loc='lower right')

ax=axes[1]
d=S.dropna(subset=['from_peak'])
ax.scatter(d.from_peak*100,d.t20_mean*100,s=55,c=np.where(d.is_broad,'#2980b9','#e74c3c'),zorder=3)
for nm,r in d.iterrows():
    ax.annotate(nm.replace('ETF','').split('(')[0],(r.from_peak*100,r.t20_mean*100),fontsize=7,
                xytext=(4,3),textcoords='offset points',color='#444')
ax.axhline(0,color='#999',lw=0.8); ax.axvline(0,color='#999',lw=0.8)
ax.set_xlabel('drawdown from 2026 high, as of 2026-11-13 (%)')
ax.set_ylabel('average 20-day rebound after crash days (%)')
ax.set_title('Crushed in 2026 = biggest rebound after a panic\n(blue = broad index ETF)',loc='left',fontsize=10.5)
r=np.corrcoef(d.from_peak,d.t20_mean)[0,1]
ax.text(0.02,0.97,f'corr = {r:.2f}',transform=ax.transAxes,fontsize=9,va='top',color='#555')
plt.tight_layout(); plt.savefig('analysis_dip/fig4_etf.png',dpi=170); plt.close()
print(S[['t20_mean','aw_t20','win','worst','t60','from_peak']].round(3).to_string())
print('corr',round(r,3))
