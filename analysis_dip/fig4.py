import pandas as pd, numpy as np, matplotlib
matplotlib.use('Agg'); import matplotlib.pyplot as plt
from matplotlib import font_manager as fm
fm.fontManager.addfont('docker/training/fonts/WQYZenHei.ttf')
plt.rcParams.update({'font.family':'WenQuanYi Zen Hei','axes.unicode_minus':False,'figure.facecolor':'white',
 'axes.facecolor':'white','axes.edgecolor':'#cccccc','axes.grid':True,'grid.color':'#eeeeee','grid.linewidth':0.8,
 'font.size':10,'axes.spines.top':False,'axes.spines.right':False,'axes.axisbelow':True})

S=pd.read_csv('analysis_dip/cache/etf_summary_both.csv',index_col=0)
C=pd.read_csv('analysis_dip/cache/sector_now.csv',index_col=0)
S=S.join(C[['from_peak']],how='left')
idx=pd.read_parquet('analysis_dip/cache/index.parquet')
p=idx.pivot(index='dt',columns='symbol',values='close').sort_index()
CODES={'上证50ETF(510050)':'000016.SH','沪深300ETF(510300)':'000300.SH','中证500ETF(510500)':'000905.SH',
       '中证1000ETF(512100)':'000852.SH','创业板ETF(159915)':'399006.SZ','科创50ETF(588000)':'000688.SH','北证50ETF':'899050.BJ'}
for nm,c in CODES.items():
    s=p[c].dropna(); y=s[s.index>=20260101]
    S.loc[nm,'from_peak']=y.iloc[-1]/y.max()-1
BROAD=list(CODES)
S['is_broad']=S.index.isin(BROAD)
S['label']=S.index.str.replace(r'\((5\d{5}|1\d{5})\)','',regex=True)
S.loc[S.n_events<11,'label']=S.loc[S.n_events<11,'label']+' (n='+S.loc[S.n_events<11,'n_events'].astype(int).astype(str)+')'
S=S.sort_values('t20_mean')

fig,axes=plt.subplots(1,2,figsize=(15.5,8.6),gridspec_kw={'width_ratios':[1.5,1]})
ax=axes[0]; y=np.arange(len(S)); h=0.38
ax.barh(y+h/2,S.t20_mean*100,height=h,color=np.where(S.is_broad,'#2980b9','#e74c3c'),label='等权（行业为等权组合）')
aw=S.aw_t20*100
m=aw.notna()
ax.barh(y[m]-h/2,aw[m],height=h,color='#f5b7b1',label='成交额加权（更接近 ETF 的市值加权）')
ax.set_yticks(y); ax.set_yticklabels(S.label,fontsize=8.8)
ax.axvline(0,color='#333',lw=0.9)
for j,(v,w) in enumerate(zip(S.t20_mean*100,S.win*100)):
    ref=max(v, aw.iloc[j] if not np.isnan(aw.iloc[j]) else v)
    ax.text(ref+0.45,j,f'{v:+.1f}%  胜率{w:.0f}%',va='center',fontsize=7.6)
ax.set_xlabel('千股跌停日收盘买入、持有 20 个交易日的平均收益（%）')
ax.set_title('宽基与行业：跌停后 20 天谁涨得多\n2016–2025 共 11 次事件，事件等权平均',loc='left',fontsize=11)
ax.legend(frameon=False,fontsize=8,loc='lower right')
ax.set_xlim(-1.5,S.t20_mean.max()*100+14)

ax=axes[1]
d=S.dropna(subset=['from_peak'])
d=d[d.index!='北证50ETF']
ax.scatter(d.from_peak*100,d.t20_mean*100,s=62,c=np.where(d.is_broad,'#2980b9','#e74c3c'),zorder=3,edgecolor='white',linewidth=.6)
OFF={'军工':(4,6),'电子':(4,-10),'半导体':(-10,7),'新能源':(4,-11),'沪深300ETF':(4,-11),
    '钢铁':(-28,3),'创业板ETF':(-20,-13),'中证1000ETF':(4,3),'机械设备':(6,-9),'通信':(4,3)}
for nm,r in d.iterrows():
    dx,dy=OFF.get(r.label,(4,4))
    ax.annotate(r.label,(r.from_peak*100,r.t20_mean*100),fontsize=7.2,xytext=(dx,dy),
                textcoords='offset points',color='#333')
ax.axhline(0,color='#999',lw=0.8); ax.axvline(0,color='#999',lw=0.8)
ax.set_xlabel('2026 年内高点回撤（截至 2026-11-13，%）'); ax.set_ylabel('跌停后 20 日平均反弹（%）')
r=np.corrcoef(d.from_peak,d.t20_mean)[0,1]
ax.set_title(f'2026 年跌得越狠，恐慌后弹得越高（相关系数 {r:.2f}）\n蓝点=宽基 ETF，红点=行业；北证50 仅 3 次样本未画出',loc='left',fontsize=10.5)
ax.set_xlim(-52,2); ax.set_ylim(-0.5,14.5)
plt.tight_layout(); plt.savefig('analysis_dip/fig4_etf.png',dpi=170); plt.close()
print('ok', round(r,3))
