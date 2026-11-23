"""盲区扫描：从涨幅榜反推"你不知道存在的品类"。

不用预先知道产业链有哪些环节——先看谁涨了，再问它做什么，最后按主营聚类。

用法:
    python scripts/research/blindspot_scan.py --top 120
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

# 品类词表：命中即归类（可扩展）
TAGS = {
    "电子布/玻纤": ["玻璃纤维", "电子布", "玻纤"],
    "覆铜板/CCL": ["覆铜板", "铜箔基板", "CCL"],
    "铜箔": ["铜箔"],
    "光模块": ["光模块", "光收发"],
    "光芯片": ["光芯片", "激光器芯片", "光通信芯片"],
    "光纤光缆": ["光纤", "光缆"],
    "PCB": ["印制电路", "线路板", "PCB"],
    "存储": ["存储", "闪存", "内存", "DRAM", "NAND"],
    "液冷/散热": ["液冷", "散热", "温控", "热管理"],
    "电源": ["电源", "UPS", "变流"],
    "连接器": ["连接器", "连接线", "线束"],
    "钻针/刀具": ["钻针", "刀具", "硬质合金", "数控刀"],
    "半导体设备": ["半导体设备", "晶圆", "刻蚀", "薄膜沉积", "封装设备", "测试设备"],
    "半导体材料": ["电子特气", "靶材", "光刻胶", "抛光", "硅片", "湿电子"],
    "芯片设计": ["集成电路设计", "芯片设计", "GPU", "CPU", "SoC"],
    "锂电材料": ["正极", "负极", "隔膜", "电解液", "六氟"],
    "锂矿": ["锂矿", "碳酸锂", "氢氧化锂"],
    "电池": ["锂离子电池", "动力电池", "电芯"],
    "光伏": ["硅料", "硅片", "电池片", "组件", "光伏"],
    "储能": ["储能"],
    "军工": ["军工", "航空", "航天", "雷达", "导弹"],
    "机器人": ["机器人", "减速器", "丝杠", "伺服"],
    "通信设备": ["交换机", "路由器", "基站", "通信设备"],
    "服务器": ["服务器", "整机"],
    "IDC/算力": ["数据中心", "IDC", "算力"],
    "有色/资源": ["稀土", "钨", "锑", "黄金", "铜", "铝", "锌"],
    "医药": ["创新药", "原料药", "医疗器械", "CXO"],
    "消费": ["白酒", "食品", "家电", "家居"],
}


def suffix(code: str) -> str:
    c = code.strip().lower()
    for pre, ex in (("sz", "SZ"), ("sh", "SH"), ("bj", "BJ")):
        if c.startswith(pre):
            return f"{c[2:]}.{ex}"
    return code.upper()


def top_gainers(n: int, start: str, end: str) -> pd.DataFrame:
    files = sorted(glob.glob(str(DATA / "dt=*" / "data.parquet")))
    files = [f for f in files if start <= f.split("dt=")[1][:8] <= end]
    base = pd.read_parquet(files[0], columns=["symbol", "close"]).set_index("symbol")["close"]
    mx: dict[str, float] = {}
    for f in files:
        df = pd.read_parquet(f, columns=["symbol", "close"])
        for s, v in df.set_index("symbol")["close"].items():
            if s not in mx or v > mx[s]:
                mx[s] = v
    r = pd.DataFrame({"base": base, "max": pd.Series(mx)}).dropna()
    r = r[r.base > 0]
    r["mult"] = r["max"] / r.base
    return r.sort_values("mult", ascending=False).head(n)


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
            time.sleep(0.5)
    return ""


def tag_of(text: str) -> str:
    for name, kws in TAGS.items():
        if any(k.lower() in text.lower() for k in kws):
            return name
    return "其他/未归类"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=120)
    ap.add_argument("--start", default="20240924")
    ap.add_argument("--end", default="20261113")
    args = ap.parse_args()

    t = top_gainers(args.top, args.start, args.end)
    print(f"取涨幅榜前 {len(t)} 只，抓主营业务 ...")
    rows = []
    for sym, row in t.iterrows():
        mb = main_business(sym)
        rows.append({"code": sym, "mult": round(float(row["mult"]), 1),
                     "biz": mb, "tag": tag_of(mb)})
        time.sleep(0.15)
    df = pd.DataFrame(rows)
    df.to_csv("data/reports/industry/blindspot.csv", index=False, encoding="utf-8-sig")

    g = (df.groupby("tag")
           .agg(只数=("code", "size"), 最高中位=("mult", "median"), 代表=("code", "first"))
           .sort_values("只数", ascending=False))
    print("\n=== 涨幅榜前 %d 的品类分布（按主营聚类）===" % len(df))
    print(g.to_string())
    print("\n=== 未归类的 ===")
    for _, r in df[df.tag == "其他/未归类"].head(15).iterrows():
        print(f"  {r['code']} {r['mult']:5.1f}x  {r['biz'][:40]}")


if __name__ == "__main__":
    main()
