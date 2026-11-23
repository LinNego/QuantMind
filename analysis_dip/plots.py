import pandas as pd, numpy as np, matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

plt.rcParams.update({'figure.facecolor':'white','axes.facecolor':'white','axes.edgecolor':'#cccccc',
 'axes.grid':True,'grid.color':'#eeeeee','grid.linewidth':0.8,'font.size':10,
 'axes.spines.top':False,'axes.spines.right':False})

idx=pd.read_parquet('analysis_dip/cache/index.parquet')
p=idx.pivot(index='dt',columns='symbol',values='close').sort_index()
dt=p.index
events=[20160107,20160111,20160126,20160225,20180619,20181011,20190506,20200203,20240205,20241009,20250407]
names={'000300.SH':('CSI 300','#7f8c8d'),'000852.SH':('CSI 1000','#2980b9'),
       '399006.SZ':('ChiNext index','#e74c3c'),'000688.SH':('STAR 50','#8e44ad')}
x=np.arange(-5,21)
fig,ax=plt.subplots(figsize=(9,5))
for s,(nm,col) in names.items():
    paths=[]
    for e in events:
        i=int(np.where(dt==e)[0][0])
        seg=p[s].iloc[i-5:i+21]
        if len(seg)==26 and seg.notna().all():
            paths.append((seg/seg.iloc[5]-1).values*100)
    if not paths: continue
    arr=np.array(paths); m=arr.mean(0)
    ax.plot(x,m,color=col,lw=2.4,label=f'{nm}  (avg T+20 {m[-1]:+.1f}%)')
    if s=='399006.SZ':
        for a in arr: ax.plot(x,a,color=col,lw=0.6,alpha=0.25)
ax.axvline(0,color='#333',lw=1,ls='--'); ax.axhline(0,color='#999',lw=0.8)
ax.set_xlabel('trading days relative to the 1,000-limit-down day (T=0)')
ax.set_ylabel('cumulative return from T close (%)')
ax.set_title('What happens after a "thousand stocks limit-down" day\nA-share indices, average of 11 events 2016-2025',loc='left',fontsize=12)
ax.legend(frameon=False,loc='upper left',fontsize=9)
ax.set_xticks([-5,0,5,10,15,20]); ax.set_xlim(-5,20)
ax.text(0.99,0.02,'faint lines = individual ChiNext paths',transform=ax.transAxes,ha='right',fontsize=8,color='#888')
plt.tight_layout(); plt.savefig('analysis_dip/fig1_paths.png',dpi=180); plt.close()

# fig2 groups
G=pd.read_parquet('analysis_dip/cache/groups2.parquet')
def gmed(col,order,labels=None):
    d=G.dropna(subset=[col])
    t=d.groupby(['event',col])['f20'].median().unstack()
    v=t.mean().reindex(order)
    return v
fig,axes=plt.subplots(1,3,figsize=(12.5,4.2),sharex=False)
specs=[('board',['创业板','科创板','深主板','沪主板'],'By board',['ChiNext','STAR','SZ main','SH main']),
       ('r20q',['Q1','Q2','Q3','Q4','Q5'],'By prior 20d return\n(Q1 = most oversold)',['Q1 weakest','Q2','Q3','Q4','Q5 strongest']),
       ('a20q',['Q1','Q2','Q3','Q4','Q5'],'By size / liquidity\n(Q1 = smallest)',['Q1 small','Q2','Q3','Q4','Q5 large'])]
G['a20q']=G.groupby('event')['a20'].transform(lambda s: pd.qcut(s,5,labels=['Q1','Q2','Q3','Q4','Q5'],duplicates='drop'))
for ax,(col,order,title,labs) in zip(axes,specs):
    v=gmed(col,order)*100
    cols=['#e74c3c' if x==v.max() else '#95a5a6' for x in v.values]
    ax.barh(range(len(v)),v.values,color=cols,height=0.62)
    ax.set_yticks(range(len(v))); ax.set_yticklabels(labs,fontsize=9); ax.invert_yaxis()
    ax.axvline(0,color='#333',lw=0.9)
    for j,val in enumerate(v.values): ax.text(val+(0.3 if val>=0 else -0.3),j,f'{val:+.1f}%',va='center',ha='left' if val>=0 else 'right',fontsize=9)
    ax.set_title(title,fontsize=10,loc='left'); ax.set_xlabel('median 20-day forward return (%)')
    ax.set_xlim(min(0,v.min())-2.5, v.max()+3.5)
fig.suptitle('Same crash day, very different 20-day payoff (average of 11 events)',x=0.01,ha='left',fontsize=12)
plt.tight_layout(); plt.savefig('analysis_dip/fig2_groups.png',dpi=180); plt.close()

# fig3 histogram
fig,ax=plt.subplots(figsize=(8,4.2))
ax.hist(G.f20*100,bins=np.arange(-40,70,2.5),color='#2980b9',alpha=.85)
ax.axvline(0,color='#333',lw=1); ax.axvline(G.f20.median()*100,color='#e74c3c',lw=2,ls='--')
ax.set_xlabel('individual stock return, T close -> T+20 close (%)')
ax.set_ylabel('stock-event observations')
ax.set_title(f'Distribution of 20-day returns after 1,000-limit-down days\nn={len(G):,} stock-event obs, median {G.f20.median()*100:+.1f}%, {100*(G.f20>0).mean():.0f}% positive',
             loc='left',fontsize=11)
ax.text(G.f20.median()*100+1,ax.get_ylim()[1]*0.9,'median',color='#e74c3c',fontsize=9)
plt.tight_layout(); plt.savefig('analysis_dip/fig3_dist.png',dpi=180); plt.close()
print('done')
