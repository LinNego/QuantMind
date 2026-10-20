#!/usr/bin/env python3
"""共识投票实验：分数阈值 vs TopN，叠加 ADV 与大盘过滤。

问题：
1. 把「每模型 Top20 投票」换成「每模型分数超过某阈值就投票」是否可行？
2. 阈值应按绝对分数还是截面分位？各模型分数尺度是否可比？
3. 叠加 ADV 过滤时，应该先过滤再投票，还是先投票再过滤？
4. 大盘过滤（等权指数 MA / 广度）能不能改善组合？参数敏感吗？

输出：
- threshold_grid.csv：阈值 x 票数 x ADV 的完整网格
- market_filter.csv：大盘过滤参数敏感性
- picks_*.csv：若干代表性策略的逐日持仓
- REPORT.md
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

import top20_model_eval as E

HORIZON_DEFAULT = 10
COST = E.COST_ROUND_TRIP


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def load_detail(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    frame = con.execute(
        f"SELECT Symbol AS symbol, IsSTGP AS is_st FROM read_parquet('{E.DETAIL_FILE}')"
    ).fetchdf()
    frame = frame.drop_duplicates(subset=["symbol"], keep="last")
    frame["is_st"] = frame["is_st"].astype(str).eq("1")
    return frame


def build_scores(
    con: duckdb.DuckDBPyConnection,
    models: list[dict],
    adv_long: pd.DataFrame,
    start: str,
    end: str,
    min_adv_yi: float,
) -> None:
    min_adv = min_adv_yi * 1e4
    con.execute("DROP TABLE IF EXISTS adv_tbl")
    con.register("adv_long", adv_long)
    con.execute(
        "CREATE TABLE adv_tbl AS "
        f"SELECT symbol, d, adv, (adv >= {min_adv}) AS adv_ok "
        "FROM adv_long WHERE adv IS NOT NULL"
    )
    con.execute("DROP TABLE IF EXISTS scores")
    con.execute(
        "CREATE TABLE scores ("
        "model VARCHAR, symbol VARCHAR, d INTEGER, pred DOUBLE, "
        "rk INTEGER, n_all INTEGER, rk_adv INTEGER, n_adv INTEGER)"
    )
    for model in models:
        path = model["pred_path"]
        label = model["model_type"]
        con.execute(
            f"""
            INSERT INTO scores
            WITH p AS (
              SELECT CASE WHEN substr(symbol, 1, 2) IN ('SH', 'SZ', 'BJ')
                          THEN substr(symbol, 3) || '.' || substr(symbol, 1, 2)
                          ELSE symbol END AS symbol,
                     CAST(strftime(trade_date, '%Y%m%d') AS INTEGER) AS d,
                     max(CAST(pred AS DOUBLE)) AS pred
              FROM read_parquet('{path}')
              WHERE split = 'test' AND pred IS NOT NULL
                AND trade_date >= DATE '{start}' AND trade_date <= DATE '{end}'
              GROUP BY 1, 2
            ), j AS (
              SELECT p.symbol, p.d, p.pred,
                     coalesce(a.adv_ok, FALSE) AS adv_ok
              FROM p LEFT JOIN adv_tbl a
                ON p.symbol = a.symbol AND p.d = a.d
            )
            SELECT '{label}' AS model, symbol, d, pred,
                   row_number() OVER (
                     PARTITION BY d ORDER BY pred DESC
                   ) AS rk,
                   count(*) OVER (PARTITION BY d) AS n_all,
                   row_number() OVER (
                     PARTITION BY d
                     ORDER BY CASE WHEN adv_ok THEN pred END DESC NULLS LAST
                   ) AS rk_adv,
                   count(CASE WHEN adv_ok THEN 1 END)
                     OVER (PARTITION BY d) AS n_adv
            FROM j
            """
        )
        log(f"  装载 {label}")

    con.execute("DROP TABLE IF EXISTS votes")
    con.execute(
        """
        CREATE TABLE votes AS
        SELECT symbol, d,
          sum(CASE WHEN rk <= 20 THEN 1 ELSE 0 END) AS v_top20,
          sum(CASE WHEN 1.0 - (rk - 1.0) / n_all >= 0.99 THEN 1 ELSE 0 END)
            AS v_p99,
          sum(CASE WHEN 1.0 - (rk - 1.0) / n_all >= 0.98 THEN 1 ELSE 0 END)
            AS v_p98,
          sum(CASE WHEN 1.0 - (rk - 1.0) / n_all >= 0.95 THEN 1 ELSE 0 END)
            AS v_p95,
          sum(CASE WHEN 1.0 - (rk - 1.0) / n_all >= 0.90 THEN 1 ELSE 0 END)
            AS v_p90,
          sum(CASE WHEN rk_adv <= 20 THEN 1 ELSE 0 END) AS v_adv_top20,
          sum(CASE WHEN 1.0 - (rk_adv - 1.0) / n_adv >= 0.99 THEN 1 ELSE 0 END)
            AS v_adv_p99,
          sum(CASE WHEN 1.0 - (rk_adv - 1.0) / n_adv >= 0.98 THEN 1 ELSE 0 END)
            AS v_adv_p98,
          sum(CASE WHEN 1.0 - (rk_adv - 1.0) / n_adv >= 0.95 THEN 1 ELSE 0 END)
            AS v_adv_p95,
          sum(CASE WHEN 1.0 - (rk_adv - 1.0) / n_adv >= 0.90 THEN 1 ELSE 0 END)
            AS v_adv_p90,
          avg(1.0 - (rk - 1.0) / n_all) AS avg_pr,
          avg(1.0 - (rk_adv - 1.0) / n_adv) AS avg_pr_adv,
          avg(pred) AS avg_pred,
          count(*) AS n_models
        FROM scores
        GROUP BY symbol, d
        """
    )
    n_scores = con.execute("SELECT count(*) FROM scores").fetchone()[0]
    n_votes = con.execute("SELECT count(*) FROM votes").fetchone()[0]
    log(f"  scores={n_scores:,} votes={n_votes:,}")


def fetch_picks(
    con: duckdb.DuckDBPyConnection,
    detail: pd.DataFrame,
    vote_col: str,
    min_votes: int,
    adv_mode: str,
    tie_col: str,
    topk: int = 20,
) -> pd.DataFrame:
    con.register("detail_map", detail[["symbol", "is_st"]])
    where_votes = f"{vote_col} >= {max(min_votes, 1)}"
    adv_filter = ""
    if adv_mode == "post":
        adv_filter = "AND a.adv_ok"
    sql = f"""
    WITH v AS (
      SELECT symbol, d, {vote_col} AS votes, avg_pr, avg_pr_adv, avg_pred,
             n_models
      FROM votes
      WHERE {where_votes}
    ), r AS (
      SELECT *,
             row_number() OVER (
               PARTITION BY d ORDER BY votes DESC, {tie_col} DESC, avg_pred DESC
             ) AS pick_rk
      FROM v
    )
    SELECT r.symbol, r.d, r.votes, r.pick_rk, f.fwd, a.adv, nm.buy_d,
           (b.symbol IS NOT NULL) AS blocked, dm.is_st
    FROM r
    LEFT JOIN fwd_long f ON r.symbol = f.symbol AND r.d = f.d
    LEFT JOIN next_map nm ON r.d = nm.d
    LEFT JOIN blocked b ON r.symbol = b.symbol AND b.buy_d = nm.buy_d
    LEFT JOIN adv_tbl a ON r.symbol = a.symbol AND r.d = a.d
    LEFT JOIN detail_map dm ON r.symbol = dm.symbol
    WHERE r.pick_rk <= {topk} {adv_filter}
    """
    return con.execute(sql).fetchdf()


def fetch_single_model(
    con: duckdb.DuckDBPyConnection,
    detail: pd.DataFrame,
    model_type: str,
    adv: bool,
) -> pd.DataFrame:
    con.register("detail_map", detail[["symbol", "is_st"]])
    rank_col = "rk_adv" if adv else "rk"
    sql = f"""
    SELECT p.symbol, p.d, 1 AS votes, {rank_col} AS pick_rk,
           f.fwd, a.adv, nm.buy_d,
           (b.symbol IS NOT NULL) AS blocked, dm.is_st
    FROM scores p
    LEFT JOIN fwd_long f ON p.symbol = f.symbol AND p.d = f.d
    LEFT JOIN next_map nm ON p.d = nm.d
    LEFT JOIN blocked b ON p.symbol = b.symbol AND b.buy_d = nm.buy_d
    LEFT JOIN adv_tbl a ON p.symbol = a.symbol AND p.d = a.d
    LEFT JOIN detail_map dm ON p.symbol = dm.symbol
    WHERE p.model = '{model_type}' AND {rank_col} <= 20
    """
    return con.execute(sql).fetchdf()


def eval_picks(
    picks: pd.DataFrame,
    dates: list[int],
    uni_all: pd.Series,
    uni_adv: pd.Series,
    horizon: int,
    nonst: bool = False,
) -> tuple[dict, pd.DataFrame, dict[int, set[str]]]:
    data = picks.copy()
    data = data[~data["blocked"].fillna(False)]
    if nonst:
        data = data[~data["is_st"].fillna(False)]
    data = data.dropna(subset=["fwd"])
    data["d"] = data["d"].astype("int64")

    tops = data.groupby("d").agg(
        top=("fwd", "mean"),
        n_sel=("fwd", "size"),
        min_votes=("votes", "min"),
        med_votes=("votes", "median"),
    )
    day = pd.DataFrame(index=pd.Index(dates, name="d"))
    day = day.join(tops)
    day["top"] = day["top"].fillna(0.0)
    day["n_sel"] = day["n_sel"].fillna(0.0)
    day["uni"] = uni_all.reindex(day.index)
    day["uni_adv"] = uni_adv.reindex(day.index)
    day = day.dropna(subset=["uni"])

    day["excess"] = day["top"] - day["uni"]
    day["excess_adv"] = day["top"] - day["uni_adv"]
    beat = data.copy()
    beat["beat"] = beat["fwd"] > beat["d"].map(day["uni"])
    stock_beat = beat.groupby("d")["beat"].mean()
    day["stock_beat"] = stock_beat.reindex(day.index)

    topsets = {int(d): set(group["symbol"]) for d, group in data.groupby("d")}
    port = E.portfolio_stats(day, horizon, topsets)
    summary = {
        "n_days": int(len(day)),
        "avg_selected": float(day["n_sel"].mean()),
        "med_votes": float(day["med_votes"].median()),
        "min_votes": float(day["min_votes"].median()),
        "top_ret": float(day["top"].mean()),
        "uni_ret": float(day["uni"].mean()),
        "top_excess": float(day["excess"].mean()),
        "excess_adv": float(day["excess_adv"].mean()),
        "win_day": float((day["top"] > day["uni"]).mean()),
        "win_day_adv": float((day["top"] > day["uni_adv"]).mean()),
        "stock_beat": float(day["stock_beat"].mean()),
        **port,
    }
    return summary, day, topsets


def yearly_rows(
    label: str,
    day: pd.DataFrame,
    topsets: dict[int, set[str]],
    horizon: int,
) -> list[dict]:
    rows = []
    for year in sorted({int(d) // 10000 for d in day.index}):
        sub = day[[int(d) // 10000 == year for d in day.index]]
        if len(sub) < 5:
            continue
        sub_sets = {int(d): topsets[int(d)] for d in sub.index if int(d) in topsets}
        port = E.portfolio_stats(sub, horizon, sub_sets)
        rows.append(
            {
                "strategy": label,
                "year": year,
                "n_days": len(sub),
                "avg_selected": float(sub["n_sel"].mean()),
                "top_excess": float(sub["excess"].mean()),
                "excess_adv": float(sub["excess_adv"].mean()),
                "win_day": float((sub["top"] > sub["uni"]).mean()),
                "stock_beat": float(sub["stock_beat"].mean()),
                "port_ann_net": port.get("port_ann_net", float("nan")),
                "port_maxdd": port.get("port_maxdd", float("nan")),
                "port_turnover": port.get("port_turnover", float("nan")),
            }
        )
    return rows


def market_on(close: pd.DataFrame, kind: str, window: int) -> pd.Series:
    ret = close.pct_change()
    if kind == "ma":
        mkt = ret.mean(axis=1).fillna(0.0)
        idx = (1.0 + mkt).cumprod()
        ma = idx.rolling(window, min_periods=window).mean()
        return (idx > ma).rename("on")
    if kind == "breadth":
        above = close > close.rolling(window, min_periods=window).mean()
        return (above.mean(axis=1) > 0.5).rename("on")
    raise ValueError(kind)


def filtered_stats(
    day: pd.DataFrame,
    topsets: dict[int, set[str]],
    on: pd.Series,
    horizon: int,
) -> dict:
    selected = list(day.index)[::horizon]
    prev_names: set[str] = set()
    prev_inv = False
    rets, turns, states = [], [], []
    for date in selected:
        invested = bool(on.get(date, False))
        names = topsets.get(int(date), set()) if invested else set()
        if invested:
            ret = float(day.at[date, "top"])
            if prev_inv and names:
                turn = 1.0 - len(names & prev_names) / float(len(names))
            else:
                turn = 1.0
        else:
            ret = 0.0
            turn = 1.0 if prev_inv else 0.0
        rets.append(ret)
        turns.append(turn)
        states.append(invested)
        prev_names = names
        prev_inv = invested
    rets_arr = np.asarray(rets, dtype=float)
    turns_arr = np.asarray(turns, dtype=float)
    net = rets_arr - turns_arr * COST
    periods = len(net)
    years = max(periods * horizon, 1) / 252.0
    total = float(np.prod(1.0 + net) - 1.0)
    equity = np.concatenate(([1.0], np.cumprod(1.0 + net)))
    peak = np.maximum.accumulate(equity)
    switches = int(sum(states[i] != states[i - 1] for i in range(1, len(states))))
    return {
        "periods": periods,
        "ann_net": float((1.0 + total) ** (1.0 / years) - 1.0) if total > -1 else -1.0,
        "total_net": total,
        "maxdd": float((equity / peak - 1.0).min()),
        "off_share": float(1.0 - np.mean(states)) if states else 0.0,
        "switches": switches,
        "turnover": float(turns_arr[1:].mean()) if len(turns_arr) > 1 else 1.0,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-map", default="/tmp/model_map_31.json")
    parser.add_argument("--horizon", type=int, default=HORIZON_DEFAULT)
    parser.add_argument("--start", default="2025-02-01")
    parser.add_argument("--end", default="2026-08-31")
    parser.add_argument("--min-adv-yi", type=float, default=0.5)
    parser.add_argument("--output-dir", default="/tmp/consensus_threshold_20261020")
    parser.add_argument("--memory", default="4GB")
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--skip-market-filter", action="store_true")
    args = parser.parse_args()

    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    con = E.connect(args.memory, args.threads)

    models_all = json.loads(Path(args.model_map).read_text(encoding="utf-8"))
    for model in models_all:
        model["pred_path"] = f"{E.PRED_ROOT}/{model['model_id']}/pred.parquet"
    models = [m for m in models_all if int(m["horizon"]) == args.horizon]
    if not models:
        raise RuntimeError(f"没有 T+{args.horizon} 模型")
    log(f"T+{args.horizon} 模型 {len(models)} 个，窗口 {args.start}..{args.end}")

    close = E.load_panel(
        con, "close", int(args.start[:4]) * 10000, int(args.end[:4]) * 10000 + 1231
    )
    amount = E.load_panel(
        con, "amount", int(args.start[:4]) * 10000, int(args.end[:4]) * 10000 + 1231
    )
    fwd_long = E.build_fwd_long(close, args.horizon)
    adv_long = E.build_adv_long(amount)
    next_map = E.next_day_map(close)
    blocked = E.load_blocked(con)
    detail = load_detail(con)
    con.register("fwd_long", fwd_long)
    con.register("next_map", next_map)
    con.register("blocked", blocked)

    build_scores(con, models, adv_long, args.start, args.end, args.min_adv_yi)

    dates = [
        int(r[0])
        for r in con.execute("SELECT DISTINCT d FROM votes ORDER BY d").fetchall()
    ]
    min_adv = args.min_adv_yi * 1e4
    uni_all = fwd_long.groupby("d")["fwd"].mean()
    merged_adv = fwd_long.merge(adv_long, on=["symbol", "d"], how="left")
    uni_adv = merged_adv[merged_adv["adv"] >= min_adv].groupby("d")["fwd"].mean()

    variants: list[dict] = [
        {
            "label": "Top20投票",
            "col": "v_top20",
            "k": 0,
            "adv": "none",
            "tie": "avg_pr",
        },
        {"label": "P99阈值", "col": "v_p99", "k": 0, "adv": "none", "tie": "avg_pr"},
        {"label": "P98阈值", "col": "v_p98", "k": 0, "adv": "none", "tie": "avg_pr"},
        {"label": "P95阈值", "col": "v_p95", "k": 0, "adv": "none", "tie": "avg_pr"},
        {"label": "P90阈值", "col": "v_p90", "k": 0, "adv": "none", "tie": "avg_pr"},
        {
            "label": "ADV+Top20投票",
            "col": "v_adv_top20",
            "k": 0,
            "adv": "pre",
            "tie": "avg_pr_adv",
        },
        {
            "label": "ADV+P99阈值",
            "col": "v_adv_p99",
            "k": 0,
            "adv": "pre",
            "tie": "avg_pr_adv",
        },
        {
            "label": "ADV+P98阈值",
            "col": "v_adv_p98",
            "k": 0,
            "adv": "pre",
            "tie": "avg_pr_adv",
        },
        {
            "label": "ADV+P95阈值",
            "col": "v_adv_p95",
            "k": 0,
            "adv": "pre",
            "tie": "avg_pr_adv",
        },
        {
            "label": "ADV+P90阈值",
            "col": "v_adv_p90",
            "k": 0,
            "adv": "pre",
            "tie": "avg_pr_adv",
        },
        {
            "label": "Top20投票 k=3",
            "col": "v_top20",
            "k": 3,
            "adv": "none",
            "tie": "avg_pr",
        },
        {
            "label": "P98阈值 k=4",
            "col": "v_p98",
            "k": 4,
            "adv": "none",
            "tie": "avg_pr",
        },
        {
            "label": "P95阈值 k=5",
            "col": "v_p95",
            "k": 5,
            "adv": "none",
            "tie": "avg_pr",
        },
        {
            "label": "ADV+Top20投票 k=3",
            "col": "v_adv_top20",
            "k": 3,
            "adv": "pre",
            "tie": "avg_pr_adv",
        },
        {
            "label": "ADV+P98阈值 k=4",
            "col": "v_adv_p98",
            "k": 4,
            "adv": "pre",
            "tie": "avg_pr_adv",
        },
    ]

    grid_rows, yearly_all, day_store, picks_store = [], [], {}, {}
    for variant in variants:
        picks = fetch_picks(
            con,
            detail,
            variant["col"],
            variant["k"],
            variant["adv"],
            variant["tie"],
        )
        summary, day, topsets = eval_picks(picks, dates, uni_all, uni_adv, args.horizon)
        summary.update(
            {
                "strategy": variant["label"],
                "vote_col": variant["col"],
                "min_votes": variant["k"],
                "adv_mode": variant["adv"],
            }
        )
        grid_rows.append(summary)
        yearly_all.extend(
            [
                {"vote_col": variant["col"], **row}
                for row in yearly_rows(variant["label"], day, topsets, args.horizon)
            ]
        )
        day_store[variant["label"]] = day
        picks_store[variant["label"]] = picks
        log(
            f"{variant['label']}: 只数 {summary['avg_selected']:.1f} "
            f"超额 {summary['top_excess'] * 100:.3f}% "
            f"年化 {summary['port_ann_net'] * 100:.1f}% "
            f"回撤 {summary['port_maxdd'] * 100:.1f}%"
        )

    for label, model_type, adv in [
        ("单CAT Top20", "catboost", False),
        ("单CAT ADV Top20", "catboost", True),
    ]:
        picks = fetch_single_model(con, detail, model_type, adv)
        summary, day, topsets = eval_picks(picks, dates, uni_all, uni_adv, args.horizon)
        summary.update(
            {
                "strategy": label,
                "vote_col": f"single_{model_type}_{'adv' if adv else 'base'}",
                "min_votes": 1,
                "adv_mode": "pre" if adv else "none",
            }
        )
        grid_rows.append(summary)
        yearly_all.extend(
            [
                {"vote_col": summary["vote_col"], **row}
                for row in yearly_rows(label, day, topsets, args.horizon)
            ]
        )
        day_store[label] = day
        picks_store[label] = picks
        log(
            f"{label}: 超额 {summary['top_excess'] * 100:.3f}% "
            f"年化 {summary['port_ann_net'] * 100:.1f}%"
        )

    grid = pd.DataFrame(grid_rows)
    grid.to_csv(output / "threshold_grid.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(yearly_all).to_csv(
        output / "threshold_yearly.csv", index=False, encoding="utf-8-sig"
    )

    if not args.skip_market_filter:
        market_rows = []
        targets = [
            "Top20投票 k=3",
            "P95阈值",
            "ADV+P95阈值",
            "单CAT ADV Top20",
        ]
        for target in targets:
            if target not in day_store:
                continue
            day, topsets = day_store[target], picks_store[target]
            base = filtered_stats(
                day, topsets, pd.Series(True, index=day.index), args.horizon
            )
            market_rows.append({"strategy": target, "filter": "none", **base})
            for kind, window in [
                ("ma", 10),
                ("ma", 20),
                ("ma", 60),
                ("breadth", 20),
                ("breadth", 60),
            ]:
                on = market_on(close, kind, window)
                stats = filtered_stats(day, topsets, on, args.horizon)
                market_rows.append(
                    {
                        "strategy": target,
                        "filter": f"{kind}{window}",
                        **stats,
                    }
                )
        market = pd.DataFrame(market_rows)
        market.to_csv(output / "market_filter.csv", index=False, encoding="utf-8-sig")
        log("大盘过滤完成")

    for label in ["Top20投票 k=3", "P95阈值", "ADV+P95阈值", "单CAT ADV Top20"]:
        if label in picks_store:
            picks_store[label].to_csv(
                output / f"picks_{label.replace(' ', '_')}.csv",
                index=False,
                encoding="utf-8-sig",
            )

    log(f"输出目录 {output}")


if __name__ == "__main__":
    main()
