"""拉取 A 股公告（东方财富公告 API 直连），按日缓存，可断点续跑。

只拉 f_node=5「重大事项」这一类：一天约 50~150 条、1~2 页，比拉全量快 20 倍。
重大合同 / 签订协议 / 诉讼仲裁 / 股份质押 / 违法违规 / 获得补贴 都在这一类里，
后续做别的事件类型可直接复用这份缓存。

跑法：
    python tools/pull_announcements.py 2023-01-01 2025-12-31
产物：
    scratch/announcements/dt=YYYYMMDD.parquet
      columns: code / name / title / ann_type / art_code / url / dt
"""
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.normpath(os.path.join(HERE, "..", "scratch", "announcements"))
os.makedirs(OUT, exist_ok=True)

URL = "https://np-anotice-stock.eastmoney.com/api/security/ann"
F_NODE = os.getenv("ANN_F_NODE", "5")  # 5=重大事项，0=全部


def trading_days(start: str, end: str) -> list[str]:
    import duckdb

    cache = os.path.normpath(os.path.join(HERE, "..", "scratch", "cache.duckdb"))
    con = duckdb.connect(cache, read_only=True)
    rows = con.execute("SELECT d FROM cal WHERE d BETWEEN ? AND ? ORDER BY d", [start, end]).fetchall()
    return [r[0].strftime("%Y%m%d") for r in rows]


def _page(dt: str, page: int, size: int = 100) -> dict:
    d = f"{dt[:4]}-{dt[4:6]}-{dt[6:]}"
    params = {
        "sr": "-1", "page_size": str(size), "page_index": str(page),
        "ann_type": "A", "client_source": "web", "f_node": F_NODE, "s_node": "0",
        "begin_time": d, "end_time": d,
    }
    r = requests.get(URL, params=params, timeout=20)
    return r.json()["data"]


def fetch(dt: str, tries: int = 3) -> pd.DataFrame | None:
    for i in range(tries):
        try:
            first = _page(dt, 1)
            total = first.get("total_hits", 0)
            rows = list(first.get("list") or [])
            pages = math.ceil(total / 100)
            for p in range(2, pages + 1):
                rows += _page(dt, p).get("list") or []
            out = []
            for it in rows:
                cols = it.get("columns") or [{}]
                codes = it.get("codes") or [{}]
                code = (codes[0].get("stock_code") or "").strip()
                out.append({
                    "code": code,
                    "name": (codes[0].get("short_name") or "").strip(),
                    "title": it.get("title") or "",
                    "ann_type": cols[0].get("column_name") or "",
                    "art_code": it.get("art_code") or "",
                    "url": f"https://data.eastmoney.com/notices/detail/{code}/{it.get('art_code','')}.html",
                    "dt": dt,
                })
            return pd.DataFrame(out)
        except Exception as e:  # noqa: BLE001
            if i == tries - 1:
                print(f"  [FAIL] {dt}: {e}", flush=True)
                return None
            time.sleep(1.5 * (i + 1))
    return None


def main(argv: list[str]) -> int:
    start, end = (argv[0], argv[1]) if len(argv) >= 2 else ("2023-01-01", "2025-12-31")
    days = trading_days(start, end)
    todo = [d for d in days if not os.path.exists(os.path.join(OUT, f"dt={d}.parquet"))]
    print(f"{start} ~ {end}: {len(days)} 个交易日，待拉 {len(todo)} 天", flush=True)

    workers = int(os.getenv("ANN_WORKERS", "4"))
    t0, ok, fail = time.time(), 0, 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fetch, d): d for d in todo}
        for i, fut in enumerate(as_completed(futs), 1):
            d, df = futs[fut], None
            try:
                df = fut.result()
            except Exception:  # noqa: BLE001
                pass
            if df is None:
                fail += 1
            else:
                if not df.empty:
                    df.to_parquet(os.path.join(OUT, f"dt={d}.parquet"), index=False)
                ok += 1
            if i % 50 == 0:
                el = time.time() - t0
                print(f"  {i}/{len(todo)}  {el/60:.1f}min  剩余约 {el/i*(len(todo)-i)/60:.1f}min", flush=True)
    print(f"完成 ok={ok} fail={fail} 用时 {(time.time()-t0)/60:.1f}min", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
