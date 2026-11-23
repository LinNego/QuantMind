import pandas as pd, numpy as np, matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib import font_manager as fm
for p in ['docker/training/fonts/WQYZenHei.ttf','docker/training/fonts/NotoSansCJK.ttf']:
    fm.fontManager.addfont(p)
plt.rcParams.update({'font.family':'WenQuanYi Zen Hei','axes.unicode_minus':False,
 'figure.facecolor':'white','axes.facecolor':'white','axes.edgecolor':'#cccccc',
 'axes.grid':True,'grid.color':'#eeeeee','grid.linewidth':0.8,'font.size':10,
 'axes.spines.top':False,'axes.spines.right':False,'axes.axisbelow':True})

# ---------- fig1 ----------
idx=pd.read_parquet('analysis_dip/cache/index.parquet')
p=idx.pivot(index='dt',columns='symbol',values='close').sort_index(); dt=p.index
events=[20160107,20160111,20160126,20160225,20180619,20181011,20190506,20200203,20240205,20241009,20250407]
names={'000300.SH':('沪深300','#7f8c8d'),'000852.SH':('中证1000','#2980b9'),
       '399006.SZ':('创业板指','#e74c3c'),'000688.SH':('科创50','#8e44ad')}
x=np.arange(-5,21)
fig,ax=plt.subplots(figsize=(9.5,5.2))
for s,(nm,col) in names.items():
    paths=[]
    for e in events:
        i=int(np.where(dt==e)[0][0])
        if i<5 or i+20>=len(dt): continue
        seg=p[s].iloc[i-5:i+21]
        if len(seg)==26 and seg.notna().all(): paths.append((seg/seg.iloc[5]-1).values*100)
    arr=np.array(paths); m=arr.mean(0)
    ax.plot(x,m,color=col,lw=2.6,label=f'{nm}　平均 T+20 {m[-1]:+.1f}%（n={len(arr)}）')
    if s=='399006.SZ':
        for a in arr: ax.plot(x,a,color=col,lw=0.6,alpha=0.22)
ax.axvline(0,color='#333',lw=1,ls='--'); ax.axhline(0,color='#999',lw=0.8)
ax.set_xlabel('相对千股跌停日的交易日（T=0 为跌停日）'); ax.set_ylabel('相对 T 日收盘的累计涨跌（%）')
ax.set_title('千股跌停之后，指数怎么走\n2016–2025 年 11 次事件的平均路径',loc='left',fontsize=13)
ax.legend(frameon=False,loc='upper left',fontsize=9.5)
ax.set_xticks([-5,0,5,10,15,20]); ax.set_xlim(-5,20)
ax.text(0.99,0.03,'细线 = 11 次事件中创业板指的各条路径',transform=ax.transAxes,ha='right',fontsize=8.5,color='#888')
plt.tight_layout(); plt.savefig('analysis_dip/fig1_paths.png',dpi=180); plt.close()

# ---------- fig2 ----------
G=pd.read_parquet('analysis_dip/cache/groups2.parquet')
G['a20q']=G.groupby('event')['a20'].transform(lambda s: pd.qcut(s,5,labels=['Q1','Q2','Q3','Q4','Q5'],duplicates='drop'))
def gmed(col,order):
    t=G.dropna(subset=[col]).groupby(['event',col])['f20'].median().unstack()
    return t.mean().reindex(order)
fig,axes=plt.subplots(1,3,figsize=(12.8,4.4))
specs=[('board',['创业板','科创板','深主板','沪主板'],'按板块',['创业板','科创板','深主板','沪主板']),
       ('r20q',['Q1','Q2','Q3','Q4','Q5'],'按前 20 日涨跌幅\n（Q1 = 跌得最狠）',['Q1 最弱','Q2','Q3','Q4','Q5 最强']),
       ('a20q',['Q1','Q2','Q3','Q4','Q5'],'按市值/流动性\n（Q1 = 最小）',['Q1 最小','Q2','Q3','Q4','Q5 最大'])]
for ax,(col,order,title,labs) in zip(axes,specs):
    v=gmed(col,order)*100
    cols=['#e74c3c' if x==v.max() else '#95a5a6' for x in v.values]
    ax.barh(range(len(v)),v.values,color=cols,height=0.6)
    ax.set_yticks(range(len(v))); ax.set_yticklabels(labs,fontsize=9.5); ax.invert_yaxis()
    ax.axvline(0,color='#333',lw=0.9)
    for j,val in enumerate(v.values): ax.text(val+(0.35 if val>=0 else -0.35),j,f'{val:+.1f}%',va='center',ha='left' if val>=0 else 'right',fontsize=9)
    ax.set_title(title,fontsize=10.5,loc='left'); ax.set_xlabel('20 日后收益中位数（%）')
    ax.set_xlim(min(0,v.min())-2.5,v.max()+4)
fig.suptitle('同一个跌停日，20 天后的结果差很多（11 次事件平均）',x=0.01,ha='left',fontsize=12.5)
plt.tight_layout(); plt.savefig('analysis_dip/fig2_groups.png',dpi=180); plt.close()

# ---------- fig3 ----------
fig,ax=plt.subplots(figsize=(8,4.3))
ax.hist(G.f20*100,bins=np.arange(-40,70,2.5),color='#2980b9',alpha=.85)
ax.axvline(0,color='#333',lw=1); ax.axvline(G.f20.median()*100,color='#e74c3c',lw=2,ls='--')
ax.set_xlabel('个股收益：T 日收盘买入 → 第 20 个交易日收盘（%）'); ax.set_ylabel('股票×事件 观测数')
ax.set_title(f'千股跌停后 20 天的个股收益分布\nn={len(G):,}，中位数 {G.f20.median()*100:+.1f}%，{100*(G.f20>0).mean():.0f}% 为正',loc='left',fontsize=11.5)
ax.text(G.f20.median()*100+1,ax.get_ylim()[1]*0.92,'中位数',color='#e74c3c',fontsize=9)
plt.tight_layout(); plt.savefig('analysis_dip/fig3_dist.png',dpi=180); plt.close()

# ---------- fig4 ----------
S=pd.read_csv('analysis_dip/cache/etf_summary_both.csv',index_col=0)
C=pd.read_csv('analysis_dip/cache/sector_now.csv',index_col=0)
S=S.join(C,how='left')
BROAD=['上证50ETF(510050)','沪深300ETF(510300)','中证500ETF(510500)','中证1000ETF(512100)','创业板ETF(159915)','科创50ETF(588000)','北证50ETF']
S['is_broad']=S.index.isin(BROAD)
S['label']=S.index.str.replace(r'\((5\d{5}|1\d{5})\)','',regex=True)
S=S.sort_values('t20_mean')
fig,axes=plt.subplots(1,2,figsize=(15.5,8.4),gridspec_kw={'width_ratios':[1.5,1]})
ax=axes[0]; y=np.arange(len(S)); h=0.38
ax.barh(y+h/2,S.t20_mean*100,height=h,color=np.where(S.is_broad,'#2980b9','#e74c3c'),label='等权持有（行业为等权组合）')
aw=S.aw_t20.fillna(S.t20_mean)
ax.barh(y-h/2,aw*100,height=h,color=np.where(S.is_broad,'#aed6f1','#f5b7b1'),label='成交额加权（更接近 ETF 的市值加权）')
ax.set_yticks(y); ax.set_yticklabels(S.label,fontsize=8.8)
ax.axvline(0,color='#333',lw=0.9)
for j,(v,w) in enumerate(zip(S.t20_mean*100,S.win*100)):
    ax.text(max(v,aw.iloc[j]*100)+0.4,j,f'{v:+.1f}%  胜率{w:.0f}%',va='center',fontsize=7.6)
ax.set_xlabel('千股跌停日买入、持有 20 个交易日的平均收益（%）')
ax.set_title('宽基与行业：跌停后 20 天谁涨得多（2016–2025，11 次事件平均）',loc='left',fontsize=11)
ax.legend(frameon=False,fontsize=8,loc='lower right')
ax.set_xlim(-1.5,S.t20_mean.max()*100+13)

ax=axes[1]
d=S.dropna(subset=['from_peak'])
ax.scatter(d.from_peak*100,d.t20_mean*100,s=60,c=np.where(d.is_broad,'#2980b9','#e74c3c'),zorder=3,edgecolor='white',linewidth=.6)
for nm,r in d.iterrows():
    ax.annotate(r.label,(r.from_peak*100,r.t20_mean*100),fontsize=7.4,xytext=(4,3),textcoords='offset points',color='#444')
ax.axhline(0,color='#999',lw=0.8); ax.axvline(0,color='#999',lw=0.8)
ax.set_xlabel('2026 年内高点回撤（截至 2026-11-13，%）'); ax.set_ylabel('跌停后 20 日平均反弹（%）')
r=np.corrcoef(d.from_peak,d.t20_mean)[0,1]
ax.set_title(f'2026 年跌得越狠，恐慌后弹得越高\n（相关系数 {r:.2f}；蓝点=宽基，红点=行业）',loc='left',fontsize=11)
plt.tight_layout(); plt.savefig('analysis_dip/fig4_etf.png',dpi=170); plt.close()
print('all charts ok')
