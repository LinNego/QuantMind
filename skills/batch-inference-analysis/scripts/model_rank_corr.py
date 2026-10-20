#!/usr/bin/env python3
"""模型间同日预测秩相关（Spearman），用于融合选型 / 判断冗余。

对同一 horizon 的模型，抽取若干交易日，按 symbol 对齐后计算
每日截面 Spearman 矩阵，再对日期取平均。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-map", default="/tmp/model_map_31.json")
    parser.add_argument("--pred-root", default="/app/models/users/default/10000001")
    parser.add_argument("--horizon", type=int, default=10)
    parser.add_argument("--n-dates", type=int, default=30)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    models = [
        m
        for m in json.loads(Path(args.model_map).read_text(encoding="utf-8"))
        if int(m["horizon"]) == args.horizon
    ]
    if len(models) < 2:
        raise SystemExit(f"T+{args.horizon} 模型不足")

    con = duckdb.connect()
    first = f"{args.pred_root}/{models[0]['model_id']}/pred.parquet"
    dates = [
        row[0]
        for row in con.execute(
            "SELECT DISTINCT trade_date "
            f"FROM read_parquet('{first}') WHERE split='test' ORDER BY 1"
        ).fetchall()
    ]
    if len(dates) > args.n_dates:
        picks = np.linspace(0, len(dates) - 1, args.n_dates).astype(int)
        dates = [dates[i] for i in picks]
    date_sql = ",".join(f"DATE '{d.date()}'" for d in dates)

    merged: pd.DataFrame | None = None
    for model in models:
        path = f"{args.pred_root}/{model['model_id']}/pred.parquet"
        frame = con.execute(
            f"""
            SELECT CASE WHEN substr(symbol, 1, 2) IN ('SH', 'SZ', 'BJ')
                        THEN substr(symbol, 3) || '.' || substr(symbol, 1, 2)
                        ELSE symbol END AS symbol,
                   trade_date,
                   CAST(pred AS DOUBLE) AS pred
            FROM read_parquet('{path}')
            WHERE split='test' AND trade_date IN ({date_sql})
            """
        ).fetchdf()
        frame = frame.drop_duplicates(["symbol", "trade_date"])
        frame = frame.rename(columns={"pred": model["model_type"]})
        if merged is None:
            merged = frame
        else:
            merged = merged.merge(frame, on=["symbol", "trade_date"], how="inner")

    columns = [m["model_type"] for m in models]
    matrices = []
    for _, group in merged.groupby("trade_date"):
        if len(group) < 100:
            continue
        matrices.append(group[columns].corr(method="spearman").to_numpy())
    average = np.nanmean(np.stack(matrices), axis=0)
    result = pd.DataFrame(average, index=columns, columns=columns)
    output = (
        args.output or f"/tmp/top20_eval_20261020/model_rank_corr_T{args.horizon}.csv"
    )
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(output, float_format="%.4f")
    print(result.round(3).to_string())
    print(f"\n写入 {output}")


if __name__ == "__main__":
    main()
