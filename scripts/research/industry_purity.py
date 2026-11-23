"""产业纯度过滤：把"产业驱动"和"情绪/重组/借壳"分开。

四个反脆弱指标（都从量价算，不依赖公告）：
  ① 板块效应：同品类兄弟数（≥3 才是产业，1 只是孤例）
  ② 持续性：上涨天数占比（脉冲式 <50%）
  ③ 非炒作：涨停天数占比（>8% 多为游资/连板）
  ④ 非重组：停牌天数（长期停牌 = 重组特征）
  ⑤ 量能健康：成交额放大 1.5~6 倍（>10 倍多为游资对倒）

用法:
    python scripts/research/industry_purity.py --start 20260820 --end 20261113 --top 120
"""

from __future__ import annotations

import argparse
import glob
import re
import subprocess
import time
from pathlib import Path

import pandas as pd

DATA = Path("data/quantdb/1_kline_data/daily_backward")
TAGS = {
    "电子布/玻纤": ["玻璃纤维", "电子布", "玻纤"],
    "覆铜板/CCL": ["覆铜板", "铜箔基板"],
    "铜箔": ["铜箔"],
    "光模块": ["光模块", "光收发"],
    "光芯片": ["光芯片", "激光器芯片", "光电子器件"],
    "光纤光缆": ["光纤", "光缆"],
    "PCB": ["印制电路", "线路板"],
    "存储": ["存储", "闪存", "内存"],
    "液冷/散热": ["液冷", "散热", "温控", "热管理"],
    "电源": ["电源", "UPS"],
    "连接器": ["连接器", "连接线"],
    "钻针/刀具": ["钻针", "刀具", "硬质合金"],
    "半导体设备": ["半导体设备", "晶圆", "刻蚀", "检测设备", "测试设备"],
    "半导体材料": ["电子特气", "靶材", "光刻胶", "单晶硅", "湿电子", "抛光"],
    "芯片设计": ["集成电路", "芯片"],
    "锂电材料": ["正极", "负极", "隔膜", "电解液"],
    "锂矿": ["锂矿", "碳酸锂"],
    "电池": ["锂电池", "动力电池", "电芯"],
    "光伏": ["硅料", "硅片", "电池片", "组件", "光伏"],
    "储能": ["储能"],
    "军工": ["军工", "航空", "航天", "雷达"],
    "机器人": ["机器人", "减速器", "丝杠"],
    "通信设备": ["交换机", "路由器", "通信设备"],
    "服务器": ["服务器", "整机"],
    "IDC/算力": ["数据中心", "算力"],
    "有色/资源": ["稀土", "钨", "锑", "铜", "铝", "锌", "矿"],
}


def main_business(sym: str) -> str:
    code = sym.split(".")[0]
    url = f"https://vip.stock.finance.sina.com.cn/corp/go.php/vCI_CorpInfo/stockid/{code}.phtml"
    for _ in range(2):
        try:
            p = subprocess.run(["curl", "-sS", "-A", "Mozilla/5.0", url],
                               capture_output=True, timeout=15)
            txt = p.stdout.decode("gbk", errors="ignore")
            m = re.search(r"主营业务：</td>\s*<td[^>]*>([^<]{2,120})", txt)
            if m:
                return m.group(1).strip()
        except Exception:
            time.sleep(0.4)
    return ""


def tag_of(text: str) -> str:
    for name, kws in TAGS.items():
        if any(k.lower() in text.lower() for k in kws):
            return name
    return "未归类"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="20260820")
    ap.add_argument("--end", default="20261113")
    ap.add_argument("--top", type=int, default=120)
    args = ap.parse_args()

    files = sorted(glob.glob(str(DATA / "dt=*" / "data.parquet")))
    files = [f for f in files if args.start <= f.split("dt=")[1][:8] <= args.end]
    frames = [pd.read_parquet(f, columns=["symbol", "time", "close", "amount"]) for f in files]
    px = pd.concat(frames, ignore_index=True)
    px["time"] = pd.to_datetime(px["time"])
    n_days = px.time.nunique()
    cal = pd.DatetimeIndex(sorted(px.time.unique()))

    g = px.sort_values(["symbol", "time"]).groupby("symbol")
    stats = []
    for sym, d in g:
        d = d.dropna(subset=["close"])
        if len(d) < 30:
            continue
        c = d.close.values
        ret = c[-1] / c[0] - 1
        r = pd.Series(c).pct_change().dropna()
        stats.append({
            "code": sym, "ret": ret,
            "up_ratio": float((r > 0).mean()),
            "lu_ratio": float((r > 0.095).mean()),
            "halt": int(n_days - d.time.nunique()),
            "amt_ratio": float(d.amount.tail(20).mean() / d.amount.head(60).mean())
            if len(d) > 80 and d.amount.head(60).mean() > 0 else 1.0,
        })
    s = pd.DataFrame(stats).sort_values("ret", ascending=False).head(args.top)

    print(f"窗口 {args.start}~{args.end}（{n_days} 个交易日），取前 {len(s)} 只，抓主营 ...")
    biz = {sym: main_business(sym) for sym in s.code}
    s["biz"] = s.code.map(biz)
    s["tag"] = s.biz.map(tag_of)
    time.sleep(0.1)

    # 品类兄弟数
    cnt = s.tag.value_counts()
    s["siblings"] = s.tag.map(cnt)

    def purity(row) -> int:
        p = 0
        p += 30 if row.siblings >= 3 else (15 if row.siblings == 2 else 0)
        p += 20 if row.lu_ratio < 0.05 else (5 if row.lu_ratio < 0.10 else 0)
        p += 20 if row.up_ratio > 0.55 else (10 if row.up_ratio > 0.48 else 0)
        p += 15 if row.halt == 0 else 0
        p += 15 if 1.3 < row.amt_ratio < 8 else (5 if row.amt_ratio >= 8 else 0)
        return p

    s["purity"] = s.apply(purity, axis=1)
    s["verdict"] = pd.cut(s.purity, [-1, 39, 69, 101], labels=["伪信号", "待验证", "真产业"])

    s.to_csv("data/reports/industry/purity.csv", index=False, encoding="utf-8-sig")

    print("\n=== 品类纯度（按平均纯度排序）===")
    gg = (s.groupby("tag")
            .agg(只数=("code", "size"), 涨幅中位=("ret", "median"),
                 平均纯度=("purity", "mean"), 真产业=("verdict", lambda x: (x == "真产业").sum()))
            .sort_values("平均纯度", ascending=False))
    gg["涨幅中位"] = (gg["涨幅中位"] * 100).round(0)
    gg["平均纯度"] = gg["平均纯度"].round(0)
    print(gg.to_string())

    print("\n=== 伪信号 Top10（应剔除）===")
    bad = s[s.verdict == "伪信号"].nlargest(10, "ret")
    for _, r in bad.iterrows():
        print(f"  {r.code} {r.ret:+6.0%} 纯度{r.purity:3d} 涨停占比{r.lu_ratio:.0%} 停牌{r.halt:3d} 兄弟{r.siblings}  {r.biz[:32]}")


if __name__ == "__main__":
    main()
