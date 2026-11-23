"""行业行情复盘：把一个行业拆成环节，按行情阶段看谁先涨、谁涨最多、谁先见顶。

数据：本地 QuantDB 后复权日线（daily_backward）
用法：
    python scripts/research/chain_replay.py config/industry_cards/ai_chain.yaml \
        --phases 20240924:20241231 20250101:20250930 20251001:20260630 20260701:20261113
"""

from __future__ import annotations

import argparse
import glob
from pathlib import Path

import pandas as pd
import yaml

DATA = Path("data/quantdb/1_kline_data/daily_backward")


def suffix(code: str) -> str:
    """sz300308 -> 300308.SZ"""
    c = code.strip().lower()
    for pre, ex in (("sz", "SZ"), ("sh", "SH"), ("bj", "BJ")):
        if c.startswith(pre):
            return f"{c[2:]}.{ex}"
    return code.upper()


def load_panel(symbols: set[str], start: str, end: str) -> pd.DataFrame:
    files = sorted(glob.glob(str(DATA / "dt=*" / "data.parquet")))
    files = [f for f in files if start <= f.split("dt=")[1][:8] <= end]
    frames = []
    for f in files:
        df = pd.read_parquet(f, columns=["symbol", "time", "close"])
        df = df[df.symbol.isin(symbols)]
        if len(df):
            frames.append(df)
    out = pd.concat(frames, ignore_index=True)
    out["time"] = pd.to_datetime(out["time"])
    return out.pivot_table(index="time", columns="symbol", values="close")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("chain")
    ap.add_argument("--phases", nargs="+", required=True)
    ap.add_argument("--start", default="20240924")
    ap.add_argument("--end", default="20261113")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.chain).read_text(encoding="utf-8"))
    segs = {s["id"]: s for s in cfg["segments"]}
    syms = {suffix(m["code"]) for s in cfg["segments"] for m in s["members"]}
    px = load_panel(syms, args.start, args.end)
    print(f"面板 {px.shape[0]} 天 × {px.shape[1]} 只  {px.index[0].date()} ~ {px.index[-1].date()}\n")

    idx = {}
    missing = []
    for sid, s in segs.items():
        codes = [suffix(m["code"]) for m in s["members"]]
        have = [c for c in codes if c in px.columns and px[c].notna().sum() > 50]
        missing += [c for c in codes if c not in have]
        if have:
            norm = px[have].ffill() / px[have].ffill().iloc[0]
            idx[sid] = norm.mean(axis=1)

    phases = [p.split(":") for p in args.phases]
    rows = []
    for sid, ser in idx.items():
        r = {"name": segs[sid]["name"], "stage": segs[sid]["stage"]}
        prev = ser.iloc[0]
        for a, b in phases:
            seg = ser.loc[a:b]
            r[f"{a[4:6]}-{b[4:6]}"] = seg.iloc[-1] / prev - 1
            prev = seg.iloc[-1]
        r["全程"] = ser.iloc[-1] / ser.iloc[0] - 1
        r["峰值"] = ser.max() / ser.iloc[0] - 1
        r["峰值日"] = str(ser.idxmax().date())
        r["保住"] = ser.iloc[-1] / ser.max()
        rows.append(r)

    t = pd.DataFrame(rows)
    t = t.sort_values("峰值", ascending=False)
    num_cols = [c for c in t.columns if c not in ("name", "stage", "峰值日")]
    print(f"{'环节':12s}{'位置':9s}" + "".join(f"{c:>10s}" for c in num_cols) + f"{'峰值日':>13s}")
    for _, r in t.iterrows():
        print(f"{r['name']:12s}{r['stage']:9s}"
              + "".join(f"{r[c]:>10.1%}" for c in num_cols)
              + f"{r['峰值日']:>13s}")

    out = Path("data/reports/industry")
    out.mkdir(parents=True, exist_ok=True)
    t.to_csv(out / f"replay_{cfg['industry']}.csv", index=False, encoding="utf-8-sig")
    print(f"\n缺失/数据不足: {len(set(missing))} 只 -> {sorted(set(missing))[:8]}")
    print(f"已写入 {out}/replay_{cfg['industry']}.csv")


if __name__ == "__main__":
    main()
