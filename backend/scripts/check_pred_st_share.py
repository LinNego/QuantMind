#!/usr/bin/env python3
"""B3 判据：选股 top decile 的 ST 占比必须 ≤ 同截面全市场占比（2026-10-22）。

审计实测（PIT ST 表建成前）：预测 top decile 的 ST 占比 8.5%、全市场 4.0%
（2.1×）——模型在「买得到 ST」这件事上系统性超额，既有风格暴露也有幸存者偏差
成分。本工具用**同一截面**复核：分母只算当日有预测分的票（模型实际可选面），
避免把停牌/无分的票算进分母造成不可比。

用法（容器内，cwd=/app）：
    python backend/scripts/check_pred_st_share.py \
        --pred /data/training_jobs/<run>/pred.parquet --split test
    # 可选：--start 20260511 --end 20260910 --decile 0.1 --json out.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

DEFAULT_DECILE = 0.1


def daily_st_shares(
    pred: pd.DataFrame, st_pit: pd.DataFrame | None, decile: float = DEFAULT_DECILE
) -> pd.DataFrame:
    """逐日截面：n / top_n / market_st / top_st。

    ``pred`` 需含 symbol / trade_date / pred（多余列忽略）。NaN 分与重复
    (symbol, trade_date) 行剔除（后者 keep=last）；``st_pit`` 缺失 = 非 ST。
    """
    if pred is None or pred.empty:
        return pd.DataFrame(
            columns=["n", "top_n", "market_st", "top_st"],
            index=pd.DatetimeIndex([], name="trade_date"),
        )
    from backend.shared.stock_utils import StockCodeUtil

    df = pred[["symbol", "trade_date", "pred"]].copy()
    # pred.parquet 是前缀式（PG/训练层），st_pit 是后缀式（QuantDB 层）——
    # 不归一就静默零匹配，把全市场 ST 占比算成 0.0（判据假通过）。
    df["symbol"] = [StockCodeUtil.to_suffix(s) for s in df["symbol"]]
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.normalize()
    df = df[np.isfinite(df["pred"].to_numpy(dtype=float))]
    df = df.drop_duplicates(subset=["symbol", "trade_date"], keep="last")

    dates = pd.DatetimeIndex(sorted(df["trade_date"].unique()))
    symbols = sorted(df["symbol"].unique())
    from backend.services.engine.factor_research.data import st_mask_over

    mask = st_mask_over(dates, symbols, st_pit)
    st_lookup = mask.stack()
    st_lookup.index.names = ["trade_date", "symbol"]

    df = df.set_index(["trade_date", "symbol"])
    df["is_st"] = st_lookup.reindex(df.index).fillna(False).to_numpy(dtype=bool)

    rows = []
    for date, g in df.groupby(level="trade_date", sort=True):
        g = g.droplevel("trade_date")
        n = len(g)
        k = max(1, int(round(n * float(decile))))
        top = g["pred"].nlargest(k)
        rows.append(
            {
                "trade_date": date,
                "n": n,
                "top_n": k,
                "market_st": int(g["is_st"].sum()),
                "top_st": int(g.loc[top.index, "is_st"].sum()),
            }
        )
    out = pd.DataFrame(rows).set_index("trade_date")
    return out


def summarize(shares: pd.DataFrame) -> dict:
    """汇总占比（分子分母逐日累加）与判据裁决。"""
    if shares is None or shares.empty:
        return {
            "days": 0,
            "market_st_share": None,
            "top_st_share": None,
            "pass": False,
        }
    n = int(shares["n"].sum())
    top_n = int(shares["top_n"].sum())
    return {
        "days": int(len(shares)),
        "n": n,
        "top_n": top_n,
        "market_st_share": float(shares["market_st"].sum() / n) if n else None,
        "top_st_share": float(shares["top_st"].sum() / top_n) if top_n else None,
        "pass": bool(shares["top_st"].sum() <= shares["market_st"].sum() * top_n / n)
        if n and top_n
        else False,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pred", type=Path, required=True, help="pred.parquet")
    ap.add_argument(
        "--split", default=None, help="只统计某个 split（train/valid/test）"
    )
    ap.add_argument("--start", default=None, help="起始交易日 YYYYMMDD（含）")
    ap.add_argument("--end", default=None, help="截止交易日 YYYYMMDD（含）")
    ap.add_argument("--decile", type=float, default=DEFAULT_DECILE)
    ap.add_argument("--json", type=Path, default=None, help="把汇总结果写成 JSON")
    args = ap.parse_args(argv)

    cols = ["symbol", "trade_date", "pred"]
    if args.split:
        cols.append("split")
    pred = pd.read_parquet(args.pred, columns=cols)
    if args.split:
        pred = pred[pred["split"].astype(str) == args.split]
    if args.start:
        pred = pred[pd.to_datetime(pred["trade_date"]) >= pd.Timestamp(args.start)]
    if args.end:
        pred = pred[pd.to_datetime(pred["trade_date"]) <= pd.Timestamp(args.end)]
    if pred.empty:
        print("没有符合筛选条件的预测行", file=sys.stderr)
        return 2

    from backend.services.engine.factor_research.data import load_st_pit

    st_pit = load_st_pit("CN")
    if st_pit is None:
        print(
            "PIT ST 表缺失：先跑 backend/scripts/build_st_pit.py --apply",
            file=sys.stderr,
        )
        return 2

    shares = daily_st_shares(pred, st_pit, decile=args.decile)
    summary = summarize(shares)
    summary.update({"pred": str(args.pred), "split": args.split, "decile": args.decile})
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    if args.json:
        args.json.write_text(
            json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    return 0 if summary["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
