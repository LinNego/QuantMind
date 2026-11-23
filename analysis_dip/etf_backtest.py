import pandas as pd, numpy as np, warnings
warnings.filterwarnings('ignore')

b=pd.read_parquet('analysis_dip/cache/back.parquet')     # 后复权（含分红/拆股）
u=pd.read_parquet('analysis_dip/cache/unadj.parquet')
pxb=b.pivot(index='dt',columns='symbol',values='close').sort_index()
pxu=u.pivot(index='dt',columns='symbol',values='close').sort_index()
amt=u.pivot(index='dt',columns='symbol',values='amount').sort_index()
dt=pxb.index

inst=pd.read_parquet('data/quantdb/2_base_sector/instrument_detail/instrument_list.parquet').set_index('Symbol')
ind=inst.rs_hyname.reindex(pxb.columns)

BUCKET={
 '银行':['全国性银行','地方性银行'],
 '非银金融':['证券','保险','多元金融'],
 '食品饮料':['酿酒','饮料乳品','食品加工','休闲食品','调味品','农产品加工'],
 '医药生物':['化学制药','中药','生物制品','医疗器械','医疗服务','医药商业','医疗美容'],
 '半导体':['半导体'],
 '电子':['消费电子','光学光电','其他电子','元器件','电子化学品'],
 '军工':['军工电子','航空装备','航天装备','地面兵装','航海装备'],
 '通信':['通信设备','通信工程','电信服务'],
 '计算机':['软件服务','云服务','IT设备','产业互联网'],
 '传媒游戏':['游戏','广告营销','影视院线','出版业','广播电视','数字媒体','文娱用品'],
 '新能源':['电池','光伏设备','风电设备','电网设备','其他发电设备','能源金属'],
 '汽车':['汽车零部件','乘用车','商用车','汽车服务','摩托车及其他'],
 '家电':['白色家电','黑色家电','小家电','厨卫电器','家电零部件'],
 '房地产':['房地产开发','房产服务','商业物业经营'],
 '煤炭':['煤炭开采','焦炭加工'],
 '有色金属':['工业金属','稀有金属','贵金属','金属新材料'],
 '钢铁':['普钢','特钢','冶钢原料'],
 '基础化工':['化学制品','化学原料','农用化工','化纤','塑料','橡胶','石油化工','日用化工','非金属材料'],
 '农林牧渔':['养殖业','种植业','饲料','渔业','林业','动物保健'],
 '建筑建材':['基础建设','专业工程','工程咨询服务','房屋建设','装修装饰','装饰建材','水泥','玻璃玻纤'],
 '公用事业':['电力','燃气','水务','环境治理','环保设备','环境监测'],
 '交通运输':['航运港口','航空机场','公路铁路','物流'],
 '机械设备':['通用设备','专用设备','自动化设备','工程机械','轨交设备','电机制造'],
 '消费零售':['一般零售','专业连锁','电子商务','贸易','旅游','酒店餐饮','教育培训','体育','专业服务','家居用品','服装家纺','纺织制造','饰品','造纸','包装印刷','综合类','油服工程','油气开采'],
}
hy2b={}
for k,v in BUCKET.items():
    for h in v: hy2b[h]=k
secs=pd.Series(ind.map(hy2b))
unmapped=sorted(set(ind.dropna().unique())-set(hy2b))
print('未映射行业:',unmapped)
print(secs.value_counts().to_string())

events=[20160107,20160111,20160126,20160225,20180619,20181011,20190506,20200203,20240205,20241009,20250407]
ret=pxb.pct_change(fill_method=None)
nonST=(inst.IsSTGP.fillna(0).astype(int)==0).reindex(pxb.columns)

rows=[]
for e in events:
    i=int(np.where(dt==e)[0][0])
    for sec,cols in secs.dropna().groupby(secs).groups.items():
        cols=[c for c in cols if nonST.get(c,False)]
        r=ret.iloc[i][cols].dropna()
        if len(r)<8: continue
        # 等权组合：平均个股收益
        def f(n):
            j=i+n
            if j>=len(dt): return np.nan
            sub=(pxb.iloc[j][cols]/pxb.iloc[i][cols]-1).dropna()
            return sub.mean() if len(sub)>=8 else np.nan
        dd=(pxb.iloc[i:i+21][cols].min()/pxb.iloc[i][cols]-1).dropna()
        rows.append(dict(event=e,asset=sec,n=len(r),ret_t=ret.iloc[i][cols].mean(),
                         f5=f(5),f10=f(10),f20=f(20),f60=f(60),mdd20=dd.mean()))
S=pd.DataFrame(rows)

# 宽基指数代理
idx=pd.read_parquet('analysis_dip/cache/index.parquet')
p=idx.pivot(index='dt',columns='symbol',values='close').sort_index()
IDX={'上证50ETF(510050)':'000016.SH','沪深300ETF(510300)':'000300.SH','中证500ETF(510500)':'000905.SH',
     '中证1000ETF(512100)':'000852.SH','创业板ETF(159915)':'399006.SZ','科创50ETF(588000)':'000688.SH',
     '北证50ETF':'899050.BJ'}
brows=[]
for nm,code in IDX.items():
    for e in events:
        i=int(np.where(dt==e)[0][0])
        if code not in p: continue
        c0=p[code].iloc[i]
        if np.isnan(c0): continue
        def g(n):
            j=i+n
            return p[code].iloc[j]/c0-1 if j<len(dt) and not np.isnan(p[code].iloc[j]) else np.nan
        seg=p[code].iloc[i:i+21]
        brows.append(dict(event=e,asset=nm,n=np.nan,ret_t=0.0,f5=g(5),f10=g(10),f20=g(20),f60=g(60),
                          mdd20=(seg.min()/c0-1)))
B=pd.DataFrame(brows)
ALL=pd.concat([B,S],ignore_index=True)
ALL.to_parquet('analysis_dip/cache/etf_events.parquet')

summary=ALL.groupby('asset').agg(
    n_events=('f20','count'),
    t20_mean=('f20','mean'),t20_med=('f20','median'),
    win=('f20',lambda s:(s>0).mean()),
    t5=('f5','mean'),t10=('f10','mean'),t60=('f60','mean'),
    worst=('f20','min'),best=('f20','max'),mdd=('mdd20','mean')).sort_values('t20_mean',ascending=False)
print('\n=== 按 T+20 平均收益排序 ===')
print(summary.round(3).to_string())
summary.to_csv('analysis_dip/cache/etf_summary.csv')
