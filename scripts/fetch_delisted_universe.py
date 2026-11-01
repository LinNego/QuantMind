#!/usr/bin/env python3
"""补充「退市股」历史数据，供小市值回测消除幸存者偏差。

为什么需要
----------
QuantDB 的 A 股 parquet 里只有**当前还活着**的 5,228 只股票（见
`research/stage0-数据与环境/day09-幸存者偏差`）。而 2016-2026 年 A 股实际退市
271 家，退市前普遍跌 80-90%，且「几块钱 + 十几亿市值」正好落在低价小市值池里。
不补这块，回测收益是**上限**。

数据来源
--------
- 退市名单：`akshare.stock_info_sh_delist()` / `stock_info_sz_delist()`（交易所口径）
- 日线行情：`baostock`（覆盖已退市代码，免费、无需 token）

产出
----
    data/delisted/delist_list.parquet   退市名单（code / name / delist_date / market）
    data/delisted/daily.parquet         退市前日线（含后复权与不复权两套价格）

用法
----
    python scripts/fetch_delisted_universe.py                # 默认补 2016 年以后退市的
    python scripts/fetch_delisted_universe.py --since 2010 --lookback 730
    python scripts/fetch_delisted_universe.py --dry-run      # 只列名单，不拉行情
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import pandas as pd

OUT_DIR = Path("data/delisted")


def fetch_delist_list(since: str) -> pd.DataFrame:
    import akshare as ak

    sh = ak.stock_info_sh_delist().rename(
        columns={"公司代码": "code", "公司简称": "name", "暂停上市日期": "delist_date"}
    )
    sz = ak.stock_info_sz_delist().rename(
        columns={"证券代码": "code", "证券简称": "name", "终止上市日期": "delist_date"}
    )
    sh["market"] = "SH"
    sz["market"] = "SZ"
    df = pd.concat(
        [
            sh[["code", "name", "delist_date", "market"]],
            sz[["code", "name", "delist_date", "market"]],
        ]
    )
    df["delist_date"] = pd.to_datetime(df["delist_date"], errors="coerce")
    df = (
        df.dropna(subset=["delist_date"])
        .sort_values("delist_date")
        .reset_index(drop=True)
    )
    df["code"] = df["code"].astype(str).str.zfill(6)
    df["symbol"] = df["code"] + df["market"].map({"SH": ".SH", "SZ": ".SZ"})
    return df[df["delist_date"] >= since].reset_index(drop=True)


def fetch_daily(
    codes: list[tuple[str, pd.Timestamp]],
    lookback_days: int,
    ckpt: Path | None = None,
) -> pd.DataFrame:
    import baostock as bs

    lg = bs.login()
    if lg.error_code != "0":
        raise SystemExit(f"baostock 登录失败：{lg.error_msg}")
    frames = []
    try:
        for i, (symbol, delist) in enumerate(codes, 1):
            prefix = "sh." if symbol.endswith(".SH") else "sz."
            code = prefix + symbol[:6]
            start = (delist - pd.Timedelta(days=lookback_days)).strftime("%Y-%m-%d")
            end = delist.strftime("%Y-%m-%d")
            for flag, tag in (("1", "hfq"), ("3", "raw")):
                rs = bs.query_history_k_data_plus(
                    code,
                    "date,open,close,volume",
                    start_date=start,
                    end_date=end,
                    frequency="d",
                    adjustflag=flag,
                )
                rows = []
                while rs.error_code == "0" and rs.next():
                    rows.append(rs.get_row_data())
                if not rows:
                    continue
                d = pd.DataFrame(rows, columns=["date", "open", "close", "volume"])
                d["symbol"] = symbol
                d["dt"] = pd.to_datetime(d.date).dt.strftime("%Y%m%d").astype(int)
                for c in ("open", "close", "volume"):
                    d[c] = pd.to_numeric(d[c], errors="coerce")
                d = d.drop(columns=["date"]).rename(
                    columns={
                        "open": f"open_{tag}",
                        "close": f"close_{tag}",
                        "volume": f"volume_{tag}",
                    }
                )
                frames.append(d)
            if i % 25 == 0:
                _flush(frames, ckpt)
                print(f"  已拉取 {i}/{len(codes)}", flush=True)
            time.sleep(0.05)
    finally:
        bs.logout()
    return _flush(frames, ckpt)


def _flush(frames: list[pd.DataFrame], ckpt: Path | None) -> pd.DataFrame:
    """把 hfq / raw 两块拼成一张宽表；按 (symbol, dt) 去重，避免 merge 出重复列。"""
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    out = out.groupby(["symbol", "dt"], as_index=False).first()
    out = out.sort_values(["symbol", "dt"]).reset_index(drop=True)
    if ckpt is not None:
        ckpt.parent.mkdir(parents=True, exist_ok=True)
        out.to_parquet(ckpt, index=False)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="补充退市股历史数据")
    ap.add_argument("--since", default="2016-01-01", help="只补该日期之后退市的")
    ap.add_argument("--lookback", type=int, default=540, help="每只补退市前多少自然日")
    ap.add_argument("--out", type=Path, default=OUT_DIR)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    lst = fetch_delist_list(args.since)
    print(f"退市名单：{len(lst)} 只（{args.since} 之后退市）")
    args.out.mkdir(parents=True, exist_ok=True)
    lst.to_parquet(args.out / "delist_list.parquet", index=False)
    print(f"名单已写入 {args.out / 'delist_list.parquet'}")
    if args.dry_run:
        return 0

    codes = list(zip(lst.symbol, lst.delist_date, strict=False))
    daily = fetch_daily(codes, args.lookback, args.out / "daily.parquet")
    if daily.empty:
        print("没有拉到任何行情，检查网络或 baostock 状态")
        return 1
    daily.to_parquet(args.out / "daily.parquet", index=False)
    print(
        f"日线已写入 {args.out / 'daily.parquet'}：{len(daily)} 行 / "
        f"{daily.symbol.nunique()} 只"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
