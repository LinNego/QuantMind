#!/usr/bin/env python3
"""Top20 选股的可投资性拆解：ST / 板块 / 市值 / 流动性。

回答三个问题：
1. 模型 Top20 里有多少 ST、北交所、微盘股？
2. 超额收益里，ST 票贡献了多少（是真 alpha 还是 ST beta）？
3. 强制「不买 ST」「ADV20>=0.5 亿」后，Top20 的收益还剩多少？

复用 top20_model_eval.py 的取数口径（同一 pred / daily_forward / trade_mask）。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import duckdb
import pandas as pd

import top20_model_eval as E


def board_of(symbol: str) -> str:
    code, _, suffix = symbol.partition(".")
    if suffix == "BJ":
        return "北交所"
    if suffix == "SH" and code.startswith("688"):
        return "科创板"
    if suffix == "SZ" and code.startswith(("300", "301")):
        return "创业板"
    return "主板"


def load_detail(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    frame = con.execute(
        "SELECT Symbol AS symbol, Name AS name, IsSTGP AS is_st, "
        "       CAST(Zsz AS DOUBLE) AS mcap_yi, CAST(Ltsz AS DOUBLE) AS float_yi "
        f"FROM read_parquet('{E.DETAIL_FILE}')"
    ).fetchdf()
    frame = frame.drop_duplicates(subset=["symbol"], keep="last")
    frame["is_st"] = frame["is_st"].astype(str).eq("1")
    frame["board"] = frame["symbol"].map(board_of)
    return frame


def top_sets(
    con: duckdb.DuckDBPyConnection,
    path: str,
    fwd_long: pd.DataFrame,
    adv_long: pd.DataFrame,
    detail: pd.DataFrame,
    next_map: pd.DataFrame,
    blocked: pd.DataFrame,
    start: str,
    end: str,
) -> pd.DataFrame:
    con.register("adv_long", adv_long)
    con.register("detail_map", detail[["symbol", "is_st"]])
    con.register("fwd_long", fwd_long)
    con.register("next_map", next_map)
    con.register("blocked", blocked)
    sql = f"""
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
    ),
    j AS (
      SELECT p.symbol, p.d, p.pred,
             coalesce(dm.is_st, FALSE) AS is_st,
             a.adv,
             (b.symbol IS NOT NULL) AS blocked
      FROM p
      LEFT JOIN detail_map dm ON p.symbol = dm.symbol
      LEFT JOIN adv_long a ON p.symbol = a.symbol AND p.d = a.d
      LEFT JOIN next_map nm ON p.d = nm.d
      LEFT JOIN blocked b ON p.symbol = b.symbol AND b.buy_d = nm.buy_d
    )
    SELECT * FROM (
      SELECT j.*,
        row_number() OVER (PARTITION BY d ORDER BY pred DESC) AS r_base,
        row_number() OVER (
          PARTITION BY d
          ORDER BY CASE WHEN is_st THEN NULL ELSE pred END DESC NULLS LAST
        ) AS r_nonst,
        row_number() OVER (
          PARTITION BY d
          ORDER BY CASE WHEN adv >= 5000 THEN pred END DESC NULLS LAST
        ) AS r_adv,
        row_number() OVER (
          PARTITION BY d
          ORDER BY CASE WHEN adv >= 5000 AND NOT is_st THEN pred END DESC NULLS LAST
        ) AS r_both
      FROM j
    )
    WHERE r_base <= 20 OR r_nonst <= 20 OR r_adv <= 20 OR r_both <= 20
    """
    frame = con.execute(sql).fetchdf()
    frame["d"] = frame["d"].astype("int64")
    frame = frame.merge(detail[["symbol", "mcap_yi", "board"]], on="symbol", how="left")
    frame = frame.merge(fwd_long, on=["symbol", "d"], how="left")
    return frame


def universe_daily(fwd_long: pd.DataFrame, detail: pd.DataFrame) -> pd.DataFrame:
    merged = fwd_long.merge(detail[["symbol", "is_st"]], on="symbol", how="left")
    merged["is_st"] = merged["is_st"].fillna(False)
    total = merged.groupby("d")["fwd"].mean().rename("uni_all")
    nonst = merged[~merged["is_st"]].groupby("d")["fwd"].mean().rename("uni_nonst")
    return pd.concat([total, nonst], axis=1)


def summarize(
    sets: pd.DataFrame,
    rank_col: str,
    universe: pd.DataFrame,
    label: str,
) -> list[dict]:
    picks = sets[sets[rank_col] <= 20].dropna(subset=["fwd"]).copy()
    if "blocked" in picks.columns:
        picks = picks[~picks["blocked"].fillna(False)]
    if picks.empty:
        return []
    daily = picks.groupby("d").agg(
        n=("fwd", "size"),
        ret=("fwd", "mean"),
        st_share=("is_st", "mean"),
        mcap_med=("mcap_yi", "median"),
        adv_med=("adv", "median"),
    )
    daily["board_bj"] = picks.groupby("d")["board"].apply(
        lambda s: (s == "北交所").mean()
    )
    daily["board_star"] = picks.groupby("d")["board"].apply(
        lambda s: (s == "科创板").mean()
    )
    daily["board_gem"] = picks.groupby("d")["board"].apply(
        lambda s: (s == "创业板").mean()
    )
    daily["ret_st"] = picks[picks["is_st"]].groupby("d")["fwd"].mean()
    daily["ret_nonst"] = picks[~picks["is_st"]].groupby("d")["fwd"].mean()
    daily["n_st"] = picks[picks["is_st"]].groupby("d")["fwd"].size()
    daily = daily.join(universe, how="left")
    daily["excess"] = daily["ret"] - daily["uni_all"]
    daily["excess_nonst"] = daily["ret"] - daily["uni_nonst"]
    daily["year"] = daily.index // 10000

    rows = []
    for year, group in [("全期", daily)] + list(daily.groupby("year")):
        rows.append(
            {
                "策略": label,
                "年份": year,
                "日数": int(group["n"].size),
                "日均只数": round(float(group["n"].mean()), 2),
                "Top20收益%": round(float(group["ret"].mean()) * 100, 4),
                "全市场%": round(float(group["uni_all"].mean()) * 100, 4),
                "超额%": round(float(group["excess"].mean()) * 100, 4),
                "非ST超额%": round(float(group["excess_nonst"].mean()) * 100, 4),
                "ST占比%": round(float(group["st_share"].mean()) * 100, 2),
                "ST票收益%": round(float(group["ret_st"].mean()) * 100, 4)
                if group["ret_st"].notna().any()
                else None,
                "非ST票收益%": round(float(group["ret_nonst"].mean()) * 100, 4),
                "北交所%": round(float(group["board_bj"].mean()) * 100, 2),
                "科创板%": round(float(group["board_star"].mean()) * 100, 2),
                "创业板%": round(float(group["board_gem"].mean()) * 100, 2),
                "市值中位亿": round(float(group["mcap_med"].median()), 1),
                "ADV中位亿": round(float(group["adv_med"].median()) * 1e-4, 3),
            }
        )
    return rows


def consensus(
    tops: pd.DataFrame,
    detail: pd.DataFrame,
    adv_long: pd.DataFrame,
    fwd_long: pd.DataFrame,
    next_map: pd.DataFrame,
    blocked: pd.DataFrame,
) -> pd.DataFrame:
    base = tops[tops["r_base"] <= 20]
    votes = (
        base.groupby(["d", "symbol"])
        .agg(votes=("model", "size"), rank_mean=("r_base", "mean"))
        .reset_index()
    )
    votes = votes[votes["votes"] >= 3].copy()
    votes["d"] = votes["d"].astype("int64")
    st_map = dict(zip(detail["symbol"], detail["is_st"], strict=False))
    votes["is_st"] = votes["symbol"].map(st_map).fillna(False).astype(bool)
    votes["score"] = votes["votes"] * 1000 - votes["rank_mean"]
    votes["r_base"] = votes.groupby("d")["score"].rank(method="first", ascending=False)
    votes["r_nonst"] = (
        votes[~votes["is_st"]]
        .groupby("d")["score"]
        .rank(method="first", ascending=False)
    )
    votes = votes.merge(detail[["symbol", "mcap_yi", "board"]], on="symbol", how="left")
    votes = votes.merge(adv_long, on=["symbol", "d"], how="left")
    votes = votes.merge(fwd_long, on=["symbol", "d"], how="left")
    votes = votes.merge(next_map, on="d", how="left")
    blocked_keys = pd.MultiIndex.from_frame(blocked[["symbol", "buy_d"]])
    votes["blocked"] = pd.MultiIndex.from_frame(votes[["symbol", "buy_d"]]).isin(
        blocked_keys
    )
    return votes


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-map", default="/tmp/model_map_31.json")
    parser.add_argument("--horizons", default="5,10,20")
    parser.add_argument("--models", default="lightgbm,xgboost,catboost,nativetft")
    parser.add_argument("--output-dir", default="/tmp/top20_eval_20261020/composition")
    parser.add_argument("--memory", default="4GB")
    parser.add_argument("--threads", type=int, default=2)
    args = parser.parse_args()

    models_all = json.loads(Path(args.model_map).read_text(encoding="utf-8"))
    for model in models_all:
        model["pred_path"] = f"{E.PRED_ROOT}/{model['model_id']}/pred.parquet"
    wanted = {m.strip() for m in args.models.split(",") if m.strip()}
    horizons = [int(h) for h in args.horizons.split(",") if h.strip()]

    con = E.connect(args.memory, args.threads)
    detail = load_detail(con)
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)

    for horizon in horizons:
        models = [m for m in models_all if int(m["horizon"]) == horizon]
        if not models:
            continue
        start, end = E.common_window(con, models, None, None)
        E.log(f"=== T+{horizon} 公共窗口 {start}..{end} ===")

        close = E.load_panel(
            con, "close", int(start[:4]) * 10000, int(end[:4]) * 10000 + 1231
        )
        amount = E.load_panel(
            con, "amount", int(start[:4]) * 10000, int(end[:4]) * 10000 + 1231
        )
        fwd_long = E.build_fwd_long(close, horizon)
        adv_long = E.build_adv_long(amount)
        next_map = E.next_day_map(close)
        blocked = E.load_blocked(con)
        universe = universe_daily(fwd_long, detail)

        rows: list[dict] = []
        tops: list[pd.DataFrame] = []
        for model in models:
            sets = top_sets(
                con,
                model["pred_path"],
                fwd_long,
                adv_long,
                detail,
                next_map,
                blocked,
                start,
                end,
            )
            sets["model"] = model["model_type"]
            tops.append(sets)
            if wanted and model["model_type"] not in wanted:
                continue
            for rank_col, suffix in [
                ("r_base", ""),
                ("r_nonst", " 非ST"),
                ("r_adv", " ADV>=0.5亿"),
                ("r_both", " 非ST+ADV>=0.5亿"),
            ]:
                label = f"{model['model_type']}{suffix}"
                rows.extend(summarize(sets, rank_col, universe, label))
            E.log(f"  {model['model_type']} 拆解完成")

        if len(tops) >= 3:
            all_tops = pd.concat(tops, ignore_index=True)
            cons = consensus(all_tops, detail, adv_long, fwd_long, next_map, blocked)
            for rank_col, suffix in [("r_base", ""), ("r_nonst", " 非ST")]:
                rows.extend(summarize(cons, rank_col, universe, f"共识>=3票{suffix}"))
            E.log("  共识>=3票 拆解完成")

        frame = pd.DataFrame(rows)
        path = output / f"composition_T{horizon}.csv"
        frame.to_csv(path, index=False, encoding="utf-8-sig")
        print(f"\n## T+{horizon}\n")
        print(frame.to_markdown(index=False))
        print(f"\n写入 {path}")


if __name__ == "__main__":
    main()
