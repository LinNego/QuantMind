"""主线景气雷达：8 条主线的四层数据采集与打分。

数据层
------
L1 产业价格  新浪期货（碳酸锂/纯碱/工业硅/铜/铝/黄金/原油/螺纹/玻璃/生猪）
L2 需求宏观  东财数据中心（PMI、进出口）、两融余额
L3 资金行为  东财行业板块快照（主力净流入、涨跌家数、成交额）
L4 财报      预留（v2 接东财业绩报表或 QuantDB 财务库）
L5 量价      新浪指数日线（相对强度、加速度、回撤）

输出
----
data/reports/industry/radar.json  供 DSH 插件读取
data/reports/industry/radar.csv   供人看

用法
----
    python scripts/research/industry_radar_v2.py
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

OUT = Path("data/reports/industry")
SINA_K = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
SINA_F = "https://stock.finance.sina.com.cn/futures/api/jsonp.php/var%20t=/InnerFuturesNewService.getDailyKLine"
EM_DATA = "https://datacenter-web.eastmoney.com/api/data/v1/get"
EM_CLIST = "https://push2.eastmoney.com/api/qt/clist/get"
H = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"}

# 8 条主线：指数 + 价格锚 + 东财板块关键词
LINES = [
    {"key": "ai", "name": "AI/算力", "index": "sh000993", "idx_name": "全指信息",
     "fut": [], "kw": ["通信设备", "计算机设备", "软件开发", "IT服务", "光学光电子"]},
    {"key": "semi", "name": "半导体", "index": "sh000688", "idx_name": "科创50",
     "fut": [], "kw": ["半导体", "电子元件", "消费电子"]},
    {"key": "newenergy", "name": "新能源", "index": "sz399808", "idx_name": "中证新能",
     "fut": ["LC0", "SA0", "SI0"], "kw": ["电池", "光伏设备", "风电设备", "能源金属"]},
    {"key": "pharma", "name": "医药", "index": "sh000991", "idx_name": "全指医药",
     "fut": [], "kw": ["化学制药", "生物制品", "医疗器械", "中药", "医疗服务"]},
    {"key": "consumer", "name": "消费", "index": "sh000932", "idx_name": "中证消费",
     "fut": [], "kw": ["白酒", "食品饮料", "家电", "旅游", "商业百货"]},
    {"key": "finance", "name": "金融", "index": "sh000992", "idx_name": "全指金融",
     "fut": [], "kw": ["银行", "证券", "保险", "多元金融"]},
    {"key": "resource", "name": "资源", "index": "sh000987", "idx_name": "全指材料",
     "fut": ["CU0", "AL0", "AU0", "SC0"], "kw": ["有色金属", "煤炭", "石油", "钢铁"]},
    {"key": "manufacture", "name": "高端制造", "index": "sz399967", "idx_name": "中证军工",
     "fut": ["RB0", "FG0"], "kw": ["航天航空", "军工", "专用设备", "通用设备"]},
]
BENCH = ("sh000300", "沪深300")
FUT_NAME = {"LC0": "碳酸锂", "SA0": "纯碱", "SI0": "工业硅", "CU0": "铜", "AL0": "铝",
            "AU0": "黄金", "SC0": "原油", "RB0": "螺纹钢", "FG0": "玻璃", "LH0": "生猪"}


def _get(url: str, params: dict | None = None, tries: int = 3, **kw):
    for _ in range(tries):
        try:
            r = requests.get(url, params=params, headers=H, timeout=20, **kw)
            if r.status_code == 200 and r.text.strip():
                return r
        except Exception:
            time.sleep(0.8)
    return None


def load_index(sym: str, n: int = 800) -> tuple[pd.Series, pd.Series]:
    r = _get(SINA_K, {"symbol": sym, "scale": 240, "ma": "no", "datalen": n})
    if r is None:
        return pd.Series(dtype=float), pd.Series(dtype=float)
    js = r.json()
    if not js:
        return pd.Series(dtype=float), pd.Series(dtype=float)
    df = pd.DataFrame(js).assign(day=lambda x: pd.to_datetime(x["day"])).set_index("day")
    return (pd.to_numeric(df["close"]).sort_index(),
            pd.to_numeric(df["volume"]).sort_index())


def load_fut(sym: str) -> pd.Series:
    r = _get(f"{SINA_F}?symbol={sym}")
    if r is None:
        return pd.Series(dtype=float)
    txt = r.text
    i = txt.find("([")
    if i < 0:
        return pd.Series(dtype=float)
    js = json.loads(txt[i + 1: txt.rfind("])") + 1])
    df = pd.DataFrame(js)
    df["day"] = pd.to_datetime(df["d"])
    return pd.to_numeric(df.set_index("day")["c"]).sort_index()


def ret(s: pd.Series, n: int) -> float:
    return s.iloc[-1] / s.iloc[-1 - n] - 1 if len(s) > n else float("nan")


def em_macro(report: str, col: str) -> tuple[float, float]:
    r = _get(EM_DATA, {"reportName": report, "columns": "ALL", "pageSize": 14,
                       "sortColumns": "REPORT_DATE", "sortTypes": -1})
    if r is None:
        return float("nan"), float("nan")
    try:
        d = r.json()["result"]["data"]
        now = float(d[0][col])
        old = float(d[3][col]) if len(d) > 3 else now
        return now, now - old
    except Exception:
        return float("nan"), float("nan")


def em_flow() -> dict[str, float]:
    """东财行业板块：主力净流入率（净流入/成交额）。"""
    import subprocess

    out: dict[str, float] = {}
    for pn in (1, 2, 3):
        url = (f"{EM_CLIST}?pn={pn}&pz=200&po=1&fid=f3&fs=m:90+t:2"
               "&ut=fa5fd1943c7b386f172d6893dbfba10b&fields=f3,f6,f14,f62")
        d = None
        for _ in range(4):
            try:
                r = subprocess.run(["curl", "-sS", "--compressed", "-A", "Mozilla/5.0",
                                    "-H", "Referer: https://quote.eastmoney.com/",
                                    "-H", "Accept: */*", url],
                                   capture_output=True, text=True, timeout=20)
                d = json.loads(r.stdout).get("data")
                if d:
                    break
            except Exception:
                d = None
            time.sleep(1.0)
        if not d:
            break
        for v in d["diff"].values():
            try:
                amt = float(v["f6"])
                net = float(v["f62"])
                if amt > 0:
                    out[str(v["f14"])] = net / amt
            except Exception:
                continue
        time.sleep(0.4)
    return out


def zs(vals: list[float]) -> list[float]:
    s = pd.Series(vals, dtype=float)
    sd = s.std(ddof=0)
    if not sd or pd.isna(sd):
        return [0.0] * len(vals)
    return list(((s - s.mean()) / sd).fillna(0.0))


def main() -> None:
    print("① 指数日线 ...")
    bench, _ = load_index(BENCH[0])
    px = {}
    vols = {}
    for ln in LINES:
        s, v = load_index(ln["index"])
        if len(s) > 260:
            px[ln["key"]] = s
            vols[ln["key"]] = v
            print(f"   {ln['name']:6s} {ln['idx_name']:8s} {s.index[-1].date()}  {len(s)} 根")
        else:
            print(f"   {ln['name']:6s} 数据不足，跳过")

    print("② 产业价格 ...")
    fut = {}
    for ln in LINES:
        for f in ln["fut"]:
            if f in fut:
                continue
            s = load_fut(f)
            if len(s) > 70:
                fut[f] = s
                print(f"   {FUT_NAME.get(f, f):6s} {s.iloc[-1]:>10.0f}  60日 {ret(s, 60):+.1%}")
            time.sleep(0.2)

    print("③ 宏观 ...")
    pmi, pmi_d3 = em_macro("RPT_ECONOMY_PMI", "MAKE_INDEX")
    exp_yoy, _ = em_macro("RPT_ECONOMY_CUSTOMS", "EXIT_BASE_SAME")
    print(f"   PMI {pmi:.1f}（3个月 {pmi_d3:+.1f}）  出口同比 {exp_yoy:+.1f}%")

    print("④ 资金流 ...")
    flow = em_flow()
    print(f"   板块 {len(flow)} 个")

    rows = []
    for ln in LINES:
        k = ln["key"]
        s = px.get(k)
        if s is None or len(s) < 260:
            continue
        mom = {
            "r20": ret(s, 20), "r60": ret(s, 60), "r120": ret(s, 120), "r250": ret(s, 250),
            "rs20": ret(s, 20) - ret(bench, 20),
            "rs60": ret(s, 60) - ret(bench, 60),
            "rs120": ret(s, 120) - ret(bench, 120),
            "dd250": s.iloc[-1] / s.iloc[-250:].max() - 1,
        }
        if len(s) > 130:
            mom["accel"] = mom["rs60"] - (s.iloc[-61] / s.iloc[-121] - 1
                                          - (bench.iloc[-61] / bench.iloc[-121] - 1))
        else:
            mom["accel"] = float("nan")

        anchors = []
        for f in ln["fut"]:
            fs = fut.get(f)
            if fs is not None:
                anchors.append({"name": FUT_NAME.get(f, f), "px": float(fs.iloc[-1]),
                                "chg60": ret(fs, 60)})
        price_mom = float(pd.Series([a["chg60"] for a in anchors]).mean()) if anchors else float("nan")

        fs_ratio = [flow[n] for n in flow if any(w in n for w in ln["kw"])]
        flow_ratio = float(pd.Series(fs_ratio).mean()) if fs_ratio else 0.0
        v = vols.get(k)
        if v is not None and len(v) > 120:
            flow_ratio = float(v.iloc[-20:].mean() / v.iloc[-120:].mean() - 1)

        rows.append({"key": k, "name": ln["name"], "index": ln["idx_name"],
                     "close": float(s.iloc[-1]), "mom": mom, "anchors": anchors,
                     "price_mom": price_mom, "flow_ratio": flow_ratio,
                     "spark": [float(v) for v in (s.iloc[-60:] / s.iloc[-60] * 100)]})

    # 合成打分
    z_mom = zs([r["mom"]["rs60"] for r in rows])
    z_acc = zs([r["mom"]["accel"] for r in rows])
    z_pri = zs([r["price_mom"] if pd.notna(r["price_mom"]) else 0.0 for r in rows])
    z_flo = zs([r["flow_ratio"] for r in rows])

    for i, r in enumerate(rows):
        r["score"] = round(0.35 * z_mom[i] + 0.20 * z_acc[i] + 0.30 * z_pri[i] + 0.15 * z_flo[i], 3)
        m = r["mom"]
        if m["rs60"] > 0.25:
            st = "过热"
        elif m["rs60"] > 0.10 and m["accel"] > 0:
            st = "加速"
        elif m["rs60"] > 0 and m["accel"] > 0:
            st = "启动"
        elif m["rs60"] < 0 and m["rs20"] < 0:
            st = "衰竭"
        else:
            st = "休眠"
        r["state"] = st

    rows.sort(key=lambda x: -x["score"])

    payload = {
        "updated": str(px[list(px)[0]].index[-1].date()) if px else "",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "env": {"pmi": pmi, "pmi_d3": pmi_d3, "exports_yoy": exp_yoy},
        "lines": rows,
    }

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "radar.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"\n=== 主线雷达（截至 {payload['updated']}）===")
    print(f"{'主线':10s}{'状态':6s}{'景气分':>7s}{'RS60':>8s}{'加速度':>8s}{'价格60日':>9s}{'资金率':>8s}{'距高':>7s}")
    for r in rows:
        pm = f"{r['price_mom']:+.1%}" if pd.notna(r["price_mom"]) else "—"
        print(f"{r['name']:10s}{r['state']:6s}{r['score']:>7.2f}{r['mom']['rs60']:>8.1%}"
              f"{r['mom']['accel']:>8.1%}{pm:>9s}{r['flow_ratio']:>8.2%}{r['mom']['dd250']:>7.1%}")

    pd.DataFrame([{**{"主线": r["name"], "状态": r["state"], "景气分": r["score"]},
                   **{k: v for k, v in r["mom"].items()},
                   "价格60日": r["price_mom"], "资金率": r["flow_ratio"]} for r in rows]
                 ).to_csv(OUT / "radar.csv", index=False, encoding="utf-8-sig")
    print(f"\n已写入 {OUT/'radar.json'} 与 radar.csv")


if __name__ == "__main__":
    main()
