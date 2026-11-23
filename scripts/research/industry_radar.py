"""行业雷达：多周期相对强度 + 资金流 + 广度，找出正在走强的行业。

数据源（均为公开接口）：
- 新浪财经：中证行业指数日线（计算动量、相对强度）
- 东方财富：行业板块快照（60 日涨幅、主力净流入、涨跌家数、成交额）

用法：
    python scripts/research/industry_radar.py
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pandas as pd
import requests

SINA_K = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
EM_CLIST = "https://push2.eastmoney.com/api/qt/clist/get"
HEAD = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"}
OUT = Path("data/reports/research")

SECTORS = {
    "sh000986": "能源",
    "sh000987": "材料",
    "sh000989": "可选消费",
    "sh000991": "医药",
    "sh000992": "金融",
    "sh000993": "信息",
    "sh000932": "主要消费",
    "sh000935": "中证信息",
}
BENCH = {"sh000300": "沪深300", "sh000905": "中证500"}


def fetch_k(sym: str, n: int = 700) -> pd.Series:
    r = requests.get(
        SINA_K,
        params={"symbol": sym, "scale": 240, "ma": "no", "datalen": n},
        headers=HEAD,
        timeout=20,
    )
    js = r.json()
    if not js:
        return pd.Series(dtype=float)
    df = pd.DataFrame(js)
    return pd.to_numeric(df["close"]).values[::-1][::-1], pd.to_datetime(df["day"])  # placeholder


def load(sym: str) -> pd.Series:
    js = requests.get(
        SINA_K,
        params={"symbol": sym, "scale": 240, "ma": "no", "datalen": 700},
        headers=HEAD,
        timeout=20,
    ).json()
    df = pd.DataFrame(js).assign(day=lambda x: pd.to_datetime(x["day"]))
    s = pd.to_numeric(df.set_index("day")["close"]).sort_index()
    return s


def rs_score(s: pd.Series, b: pd.Series) -> dict:
    def ret(x, n):
        return x.iloc[-1] / x.iloc[-1 - n] - 1 if len(x) > n else float("nan")

    d = {}
    for n, lab in ((20, "20日"), (60, "60日"), (120, "120日"), (250, "250日")):
        d[lab] = ret(s, n)
        d[f"RS{lab}"] = ret(s, n) - ret(b, n)
    # 动量加速度：近 60 日超额 - 前 60 日超额
    if len(s) > 130:
        d["加速度"] = d["RS60日"] - (
            s.iloc[-61] / s.iloc[-121] - 1 - (b.iloc[-61] / b.iloc[-121] - 1)
        )
    else:
        d["加速度"] = float("nan")
    d["距高回撤"] = s.iloc[-1] / s.iloc[-250:].max() - 1 if len(s) > 250 else float("nan")
    return d


def em_snapshot() -> pd.DataFrame:
    """东财行业板块快照。用 curl 子进程取，requests 容易被断连。"""
    import subprocess

    rows = []
    for pn in (1, 2, 3):
        url = (
            "https://push2.eastmoney.com/api/qt/clist/get?"
            f"pn={pn}&pz=200&po=1&fid=f3&fs=m:90+t:2"
            "&ut=fa5fd1943c7b386f172d6893dbfba10b"
            "&fields=f3,f6,f12,f14,f20,f24,f25,f62,f104,f105"
        )
        d = None
        for _ in range(3):
            try:
                out = subprocess.run(
                    ["curl", "-sS", "-A", "Mozilla/5.0",
                     "-H", "Referer: https://quote.eastmoney.com/", url],
                    capture_output=True, text=True, timeout=20,
                )
                d = json.loads(out.stdout).get("data")
                if d:
                    break
            except Exception:
                time.sleep(1.0)
        if not d:
            break
        rows += list(d["diff"].values())
        time.sleep(0.5)

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    for c in ["f3", "f6", "f24", "f25", "f62", "f104", "f105"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.rename(
        columns={
            "f14": "板块", "f3": "今日%", "f6": "成交额", "f24": "60日%",
            "f25": "YTD%", "f62": "主力净流入", "f104": "涨", "f105": "跌",
        }
    )
    df["成交额"] = df["成交额"] / 1e8
    df["主力净流入"] = df["主力净流入"] / 1e8
    df["广度"] = df["涨"] / (df["涨"] + df["跌"])
    return df


def main() -> None:
    print("拉取行业指数日线 ...")
    bench = load("sh000300")
    data = {}
    for sym, name in SECTORS.items():
        try:
            s = load(sym)
            if len(s) > 260 and s.index[-1] >= bench.index[-1] - pd.Timedelta(days=10):
                data[name] = s
                print(f"  {name:6s} {s.index[-1].date()}  {len(s)} 根")
            else:
                print(f"  {name:6s} 数据过期/不足，跳过")
        except Exception as e:
            print(f"  {name:6s} 失败 {repr(e)[:40]}")
        time.sleep(0.2)

    rows = []
    for name, s in data.items():
        d = rs_score(s, bench)
        rows.append({"行业": name, **d})
    t = pd.DataFrame(rows)

    # 综合分：RS60 权重最高，其次 RS20、RS120、加速度
    for c in ["RS60日", "RS20日", "RS120日", "加速度"]:
        t[c + "_z"] = (t[c] - t[c].mean()) / t[c].std(ddof=0)
    t["综合分"] = (0.35 * t.RS60日_z + 0.25 * t.RS20日_z
                   + 0.2 * t.RS120日_z + 0.2 * t.加速度_z)
    t = t.sort_values("综合分", ascending=False)

    print("\n=== 一、行业相对强度（vs 沪深300，截至 %s）===" % bench.index[-1].date())
    show = t[["行业", "20日", "60日", "120日", "250日", "RS60日", "加速度", "距高回撤", "综合分"]].copy()
    for c in ["20日", "60日", "120日", "250日", "RS60日", "加速度", "距高回撤"]:
        show[c] = show[c].map(lambda v: f"{v:+.1%}" if pd.notna(v) else "—")
    show["综合分"] = show["综合分"].map(lambda v: f"{v:+.2f}")
    print(show.to_string(index=False))

    print("\n=== 二、东财行业板块快照（今日）===")
    try:
        em = em_snapshot()
    except Exception as exc:
        print(f"  东财快照获取失败: {exc!r}"[:120])
        em = pd.DataFrame()
    if not em.empty:
        em = em[em["成交额"] > 5]
        print("\n主力净流入 TOP10（亿元）")
        a = em.nlargest(10, "主力净流入")[["板块", "今日%", "60日%", "YTD%", "主力净流入", "广度"]]
        a["今日%"] = a["今日%"].map(lambda v: f"{v/100:+.2f}%")
        a["60日%"] = a["60日%"].map(lambda v: f"{v/100:+.1f}%")
        a["YTD%"] = a["YTD%"].map(lambda v: f"{v/100:+.1f}%")
        a["主力净流入"] = a["主力净流入"].map(lambda v: f"{v:+.1f}")
        a["广度"] = a["广度"].map(lambda v: f"{v:.0%}")
        print(a.to_string(index=False))

        print("\n60 日涨幅 TOP10（剔除今日跌超 5%）")
        b = em[em["今日%"] > -500].nlargest(10, "60日%")[["板块", "今日%", "60日%", "YTD%", "主力净流入", "成交额"]]
        b["今日%"] = b["今日%"].map(lambda v: f"{v/100:+.2f}%")
        b["60日%"] = b["60日%"].map(lambda v: f"{v/100:+.1f}%")
        b["YTD%"] = b["YTD%"].map(lambda v: f"{v/100:+.1f}%")
        b["主力净流入"] = b["主力净流入"].map(lambda v: f"{v:+.1f}")
        b["成交额"] = b["成交额"].map(lambda v: f"{v:.0f}")
        print(b.to_string(index=False))

    OUT.mkdir(parents=True, exist_ok=True)
    t.to_csv(OUT / "industry_radar_rs.csv", index=False, encoding="utf-8-sig")
    if not em.empty:
        em.to_csv(OUT / "industry_radar_em.csv", index=False, encoding="utf-8-sig")
    print(f"\n已保存: {OUT}")


if __name__ == "__main__":
    main()
