"""逐模型周度（周一买 / 周五卖）回测，对比 T+5 与 T+10。

- 选股：每个模型每日在 ADV>=0.5 亿的可交易池内取预测前 20；
- 信号日 = 上一交易周最后一个交易日，买入 = 下一交易周第一个交易日收盘，
  卖出 = 该交易周最后一个交易日收盘；
- 等权、逐周转仓、往返成本 0.2% * 换手率；
- 剔除买入日 blocked_long 的标的。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

import top20_model_eval as E
import weekly_schedule_probe as W

ADV_MIN_WAN = 5000.0  # 0.5 亿


def normalize_symbol_sql(column: str = "symbol") -> str:
    return (
        f"CASE WHEN substr({column}, 1, 2) IN ('SH', 'SZ', 'BJ') "
        f"THEN substr({column}, 3) || '.' || substr({column}, 1, 2) "
        f"ELSE {column} END"
    )


def load_adv_table(con: duckdb.DuckDBPyConnection, lo: int, hi: int) -> None:
    amount = E.load_panel(con, "amount", lo, hi)
    adv = amount.rolling(20, min_periods=5).mean()
    long = adv.stack().rename("adv").reset_index()
    long.columns = ["d", "symbol", "adv"]
    long["d"] = long["d"].astype("int64")
    con.register("adv_long", long)
    con.execute(
        "CREATE OR REPLACE TABLE adv_tbl AS "
        "SELECT symbol, d, adv, (adv >= ?) AS adv_ok "
        "FROM adv_long WHERE adv IS NOT NULL",
        [ADV_MIN_WAN],
    )


def fetch_model_picks(
    con: duckdb.DuckDBPyConnection, path: str, start: str, end: str
) -> pd.DataFrame:
    sql = f"""
    WITH p AS (
      SELECT {normalize_symbol_sql()} AS symbol,
             CAST(strftime(trade_date, '%Y%m%d') AS INTEGER) AS d,
             max(CAST(pred AS DOUBLE)) AS pred
      FROM read_parquet('{path}')
      WHERE split = 'test' AND pred IS NOT NULL
        AND trade_date >= DATE '{start}' AND trade_date <= DATE '{end}'
      GROUP BY 1, 2
    ), j AS (
      SELECT p.symbol, p.d, p.pred,
             coalesce(a.adv_ok, FALSE) AS adv_ok
      FROM p LEFT JOIN adv_tbl a ON p.symbol = a.symbol AND p.d = a.d
    ), r AS (
      SELECT symbol, d, pred,
             row_number() OVER (
               PARTITION BY d
               ORDER BY CASE WHEN adv_ok THEN pred END DESC NULLS LAST
             ) AS rk_adv
      FROM j
    )
    SELECT r.symbol, r.d, r.pred, nm.buy_d,
           coalesce(b.symbol IS NOT NULL, FALSE) AS blocked,
           coalesce(dm.is_st, FALSE) AS is_st
    FROM r
    LEFT JOIN next_map nm ON r.d = nm.d
    LEFT JOIN blocked b ON r.symbol = b.symbol AND b.buy_d = nm.buy_d
    LEFT JOIN detail_map dm ON r.symbol = dm.symbol
    WHERE r.rk_adv <= 20
    """
    frame = con.execute(sql).fetchdf()
    return frame[~frame["blocked"].fillna(False)]


def evaluate(
    picks: pd.DataFrame,
    windows: list[W.WeekWindow],
    close_dates: list[int],
    date_pos: dict[int, int],
    close: np.ndarray,
    sym_pos: dict[str, int],
) -> tuple[dict, list[dict]]:
    picks = picks[["symbol", "d"]].copy()
    picks["d"] = picks["d"].astype("int64")
    rows = W.build_rows(picks, windows, close_dates, date_pos, close, sym_pos, hold=0)
    stats = W.summarize(rows, 5.0 / 252.0)
    years = W.yearly_rows(rows, 5.0 / 252.0)
    return stats, years


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-map", default="/tmp/model_map_31.json")
    parser.add_argument("--start", default="2025-02-01")
    parser.add_argument("--end", default="2026-08-31")
    parser.add_argument("--out-dir", default="/tmp/per_model_weekly")
    parser.add_argument("--memory", default="6GB")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--drop-st", action="store_true")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    models = json.loads(Path(args.model_map).read_text(encoding="utf-8"))
    models = [m for m in models if int(m["horizon"]) in (5, 10)]

    lo = int(pd.Timestamp(args.start).strftime("%Y%m%d"))
    hi = int((pd.Timestamp(args.end) + pd.Timedelta(days=40)).strftime("%Y%m%d"))
    con = E.connect(args.memory, args.threads)
    close_panel = E.load_panel(con, "close", lo, hi)
    close_dates = [int(d) for d in close_panel.index]
    date_pos = {d: i for i, d in enumerate(close_dates)}
    close = close_panel.to_numpy(dtype=np.float64, copy=False)
    sym_pos = {str(s): j for j, s in enumerate(close_panel.columns)}
    windows = W.week_windows(close_dates)

    load_adv_table(con, lo, hi)
    blocked = E.load_blocked(con)
    con.register("blocked", blocked)
    detail = con.execute(
        f"SELECT Symbol AS symbol, IsSTGP AS is_st FROM read_parquet('{E.DETAIL_FILE}')"
    ).fetchdf()
    detail = detail.drop_duplicates(subset=["symbol"], keep="last")
    detail["is_st"] = detail["is_st"].astype(str).eq("1")
    con.register("detail_map", detail[["symbol", "is_st"]])
    con.register("next_map", E.next_day_map(close_panel))

    rows = []
    year_rows = []
    for model in models:
        model_type = model["model_type"]
        horizon = int(model["horizon"])
        path = f"{E.PRED_ROOT}/{model['model_id']}/pred.parquet"
        picks = fetch_model_picks(con, path, args.start, args.end)
        if args.drop_st:
            picks = picks[~picks["is_st"].fillna(False)]
        stats, years = evaluate(picks, windows, close_dates, date_pos, close, sym_pos)
        if not stats:
            continue
        rows.append(
            {
                "model_type": model_type,
                "horizon": horizon,
                "model_id": model["model_id"],
                **stats,
            }
        )
        for row in years:
            year_rows.append(
                {
                    "model_type": model_type,
                    "horizon": horizon,
                    "model_id": model["model_id"],
                    **row,
                }
            )
        print(
            f"[{model_type:>14} T+{horizon}] ann={stats['ann_net']:.1%} "
            f"dd={stats['maxdd_net']:.1%}",
            flush=True,
        )

    summary = pd.DataFrame(rows)
    yearly = pd.DataFrame(year_rows)
    summary.to_csv(out_dir / "per_model_weekly_summary.csv", index=False)
    yearly.to_csv(out_dir / "per_model_weekly_yearly.csv", index=False)

    pivot = summary.pivot(index="model_type", columns="horizon", values="ann_net")
    pivot.columns = [f"T+{c}" for c in pivot.columns]
    pivot["diff_T10_minus_T5"] = pivot["T+10"] - pivot["T+5"]
    print()
    print(pivot.sort_values("T+10", ascending=False).to_string())

    ypivot = yearly.pivot_table(
        index=["model_type", "year"],
        columns="horizon",
        values="ann_net",
    )
    ypivot.columns = [f"T+{c}" for c in ypivot.columns]
    ypivot["diff_T10_minus_T5"] = ypivot["T+10"] - ypivot["T+5"]
    print()
    print(ypivot.to_string())


if __name__ == "__main__":
    main()
