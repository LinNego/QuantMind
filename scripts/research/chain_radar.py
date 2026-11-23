"""产业链环节雷达：把一个行业拆成环节，等权合成环节指数，看哪一段在走强。

用法:
    python scripts/research/chain_radar.py config/industry_cards/newenergy_chain.yaml
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pandas as pd
import requests
import yaml

K = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
F = "https://stock.finance.sina.com.cn/futures/api/jsonp.php/var%20t=/InnerFuturesNewService.getDailyKLine"
H = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"}


def kline(sym: str, n: int = 400) -> pd.Series:
    for _ in range(3):
        try:
            js = requests.get(K, params={"symbol": sym, "scale": 240, "ma": "no",
                                         "datalen": n}, headers=H, timeout=15).json()
            if js:
                df = pd.DataFrame(js).assign(day=lambda x: pd.to_datetime(x["day"]))
                return pd.to_numeric(df.set_index("day")["close"]).sort_index()
        except Exception:
            time.sleep(0.6)
    return pd.Series(dtype=float)


def fut(sym: str) -> pd.Series:
    try:
        r = requests.get(f"{F}?symbol={sym}", headers=H, timeout=15)
        i = r.text.find("([")
        if i < 0:
            return pd.Series(dtype=float)
        js = json.loads(r.text[i + 1: r.text.rfind("])") + 1])
        df = pd.DataFrame(js)
        return pd.to_numeric(df.set_index(pd.to_datetime(df["d"]))["c"]).sort_index()
    except Exception:
        return pd.Series(dtype=float)


def ret(s: pd.Series, n: int) -> float:
    return s.iloc[-1] / s.iloc[-1 - n] - 1 if len(s) > n else float("nan")


def main() -> None:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "config/industry_cards/newenergy_chain.yaml")
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))

    bench = kline("sh000300")
    print(f"基准 沪深300 {bench.index[-1].date()}  {bench.iloc[-1]:.2f}\n")

    # 价格锚
    anchors = {}
    for code, meta in (cfg.get("anchors") or {}).items():
        s = fut(code)
        if len(s) > 70:
            anchors[code] = (meta["name"], s)
            print(f"锚 {meta['name']:6s} {s.iloc[-1]:>9.1f}  20日 {ret(s,20):+.1%}  60日 {ret(s,60):+.1%}")
    print()

    rows = []
    for seg in cfg["segments"]:
        series = []
        for m in seg["members"]:
            s = kline(m["code"], 320)
            if len(s) > 200:
                series.append(s / s.iloc[0])
        if not series:
            continue
        idx = pd.concat(series, axis=1).mean(axis=1, skipna=True)
        rows.append({
            "id": seg["id"], "name": seg["name"], "stage": seg["stage"],
            "n": len(series),
            "r20": ret(idx, 20), "r60": ret(idx, 60), "r120": ret(idx, 120),
            "rs20": ret(idx, 20) - ret(bench, 20),
            "rs60": ret(idx, 60) - ret(bench, 60),
            "rs120": ret(idx, 120) - ret(bench, 120),
            "dd120": idx.iloc[-1] / idx.iloc[-120:].max() - 1,
        })
        time.sleep(0.1)

    t = pd.DataFrame(rows).sort_values("rs60", ascending=False)
    print(f"=== 新能源产业链环节（截至 {bench.index[-1].date()}，等权合成）===")
    print(f"{'环节':10s}{'位置':9s}{'只数':>4s}{'20日':>8s}{'60日':>8s}{'120日':>8s}{'RS60':>8s}{'距120日高':>9s}")
    for _, r in t.iterrows():
        print(f"{r['name']:10s}{r['stage']:9s}{int(r['n']):>4d}{r['r20']:>8.1%}{r['r60']:>8.1%}"
              f"{r['r120']:>8.1%}{r['rs60']:>8.1%}{r['dd120']:>9.1%}")

    out = Path("data/reports/industry")
    out.mkdir(parents=True, exist_ok=True)
    (out / f"chain_{cfg['industry']}.json").write_text(
        json.dumps({"updated": str(bench.index[-1].date()), "segments": rows},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n已写入 {out}/chain_{cfg['industry']}.json")


if __name__ == "__main__":
    main()
