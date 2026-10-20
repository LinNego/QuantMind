#!/usr/bin/env python3
"""Top-20 选股准确性评估（2025 → 2026），容器内运行。

口径（全部为训练集外 split='test'）：
- 预测：模型目录 pred.parquet；只读 split='test'。
- 收益：daily_forward（前复权）fwd_h(T)=close(T+1+h)/close(T+1)-1。
- 可交易：trade_mask 在买入日 T+1 的 blocked_long=True 的标的不进 Top20。
- 组合：每 h 个交易日换仓、等权、单次换手往返成本 0.2%。

输出：summary_common.csv / summary_yearly.csv / monthly_ic.csv /
ensemble_summary.csv / top20_latest_T*.csv / REPORT.md
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import time
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

PRED_ROOT = "/app/models/users/default/10000001"
FWD_DIR = "/data/quantdb/1_kline_data/daily_forward"
MASK_FILE = "/data/quantdb/5_technical_derived/trade_mask/trade_mask.parquet"
DETAIL_FILE = "/data/quantdb/2_base_sector/instrument_detail/instrument_detail.parquet"
TOP_K = 20
COST_ROUND_TRIP = 0.002
AMOUNT_TO_YI = 1e-4  # daily_forward.amount 单位为万元 → 亿元


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def connect(memory: str, threads: int) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute(f"SET memory_limit='{memory}'")
    con.execute("SET threads=" + str(threads))
    con.execute("SET temp_directory='/tmp/duckdb_top20_spill'")
    con.execute("SET preserve_insertion_order=false")
    return con


def partition_files(pattern: str, lo: int, hi: int) -> list[str]:
    files: list[str] = []
    for path in glob.glob(pattern):
        match = re.search(r"dt=(\d{8})", path)
        if match and lo <= int(match.group(1)) <= hi:
            files.append(path)
    return sorted(files)


def load_panel(
    con: duckdb.DuckDBPyConnection,
    column: str,
    lo: int,
    hi: int,
) -> pd.DataFrame:
    files = partition_files(f"{FWD_DIR}/dt=*/data.parquet", lo, hi)
    if not files:
        raise RuntimeError(f"daily_forward 无分区: {lo}..{hi}")
    sql = (
        f"SELECT symbol, CAST(dt AS INTEGER) AS d, {column} AS v "
        f"FROM read_parquet({files!r}) WHERE {column} IS NOT NULL"
    )
    frame = con.execute(sql).fetchdf()
    frame = frame.drop_duplicates(subset=["symbol", "d"], keep="last")
    panel = frame.pivot(index="d", columns="symbol", values="v").sort_index()
    return panel


def build_fwd_long(close: pd.DataFrame, horizon: int) -> pd.DataFrame:
    fwd = close.shift(-(1 + horizon)) / close.shift(-1) - 1.0
    long = fwd.stack().rename("fwd").reset_index()
    long.columns = ["d", "symbol", "fwd"]
    long["d"] = long["d"].astype("int64")
    return long


def build_adv_long(amount: pd.DataFrame, window: int = 20) -> pd.DataFrame:
    adv = amount.rolling(window, min_periods=5).mean()
    long = adv.stack().rename("adv").reset_index()
    long.columns = ["d", "symbol", "adv"]
    long["d"] = long["d"].astype("int64")
    return long


def next_day_map(close: pd.DataFrame) -> pd.DataFrame:
    dates = close.index.to_numpy(dtype="int64")
    return pd.DataFrame({"d": dates[:-1], "buy_d": dates[1:]})


def load_blocked(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    frame = con.execute(
        "SELECT CAST(dt AS INTEGER) AS buy_d, symbol "
        f"FROM read_parquet('{MASK_FILE}') WHERE blocked_long"
    ).fetchdf()
    return frame.drop_duplicates(subset=["buy_d", "symbol"])


def pred_range(
    con: duckdb.DuckDBPyConnection, path: str
) -> tuple[pd.Timestamp, pd.Timestamp, int]:
    row = con.execute(
        "SELECT min(trade_date), max(trade_date), count(DISTINCT trade_date) "
        f"FROM read_parquet('{path}') WHERE split='test'"
    ).fetchone()
    if row[0] is None:
        raise RuntimeError(f"pred.parquet 无 test 数据: {path}")
    return pd.Timestamp(row[0]), pd.Timestamp(row[1]), int(row[2])


def common_window(
    con: duckdb.DuckDBPyConnection,
    models: list[dict],
    start_override: str | None,
    end_override: str | None,
) -> tuple[str, str]:
    lows, highs = [], []
    for model in models:
        low, high, _ = pred_range(con, model["pred_path"])
        lows.append(low)
        highs.append(high)
    start = max(lows)
    end = min(highs)
    if start_override:
        start = pd.Timestamp(start_override)
    if end_override:
        end = pd.Timestamp(end_override)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def load_model_frame(
    con: duckdb.DuckDBPyConnection,
    path: str,
    fwd_long: pd.DataFrame,
    next_map: pd.DataFrame,
    blocked: pd.DataFrame,
    adv_long: pd.DataFrame,
    start: str,
    end: str,
) -> pd.DataFrame:
    con.register("fwd_long", fwd_long)
    con.register("next_map", next_map)
    con.register("blocked", blocked)
    con.register("adv_long", adv_long)
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
    )
    SELECT p.symbol, p.d, p.pred, f.fwd, a.adv,
           (b.symbol IS NOT NULL) AS blocked
    FROM p
    LEFT JOIN fwd_long f ON p.symbol = f.symbol AND p.d = f.d
    LEFT JOIN next_map nm ON p.d = nm.d
    LEFT JOIN blocked b ON p.symbol = b.symbol AND b.buy_d = nm.buy_d
    LEFT JOIN adv_long a ON p.symbol = a.symbol AND p.d = a.d
    """
    return con.execute(sql).fetchdf()


def apply_liquidity(frame: pd.DataFrame, min_adv_yi: float) -> pd.DataFrame:
    """只保留 ADV20 >= min_adv_yi 亿元 的标的（adv 原始单位：万元）。"""
    if min_adv_yi <= 0:
        return frame
    return frame[frame["adv"].fillna(0.0) >= min_adv_yi * 1e4]


def evaluate(
    frame: pd.DataFrame,
    topk: int = TOP_K,
    bench: pd.Series | None = None,
    bench_n: pd.Series | None = None,
) -> tuple[dict, pd.DataFrame, dict[int, set[str]]]:
    data = frame.dropna(subset=["pred", "fwd"]).copy()
    if data.empty:
        raise ValueError("评估集为空")
    day = data.groupby("d", sort=True)["fwd"].agg(uni_sub="mean", n_sub="count")
    if bench is not None:
        day["uni"] = bench.reindex(day.index).fillna(day["uni_sub"])
    else:
        day["uni"] = day["uni_sub"]
    if bench_n is not None:
        day["n_eff"] = bench_n.reindex(day.index).fillna(day["n_sub"])
    else:
        day["n_eff"] = day["n_sub"]

    ranked = data[["d", "pred", "fwd"]].copy()
    ranked["rx"] = ranked.groupby("d")["pred"].rank(method="average")
    ranked["ry"] = ranked.groupby("d")["fwd"].rank(method="average")
    grouped = ranked.groupby("d")
    ranked["dx"] = ranked["rx"] - grouped["rx"].transform("mean")
    ranked["dy"] = ranked["ry"] - grouped["ry"].transform("mean")
    cov = (ranked["dx"] * ranked["dy"]).groupby(ranked["d"]).sum()
    var_x = (ranked["dx"] ** 2).groupby(ranked["d"]).sum()
    var_y = (ranked["dy"] ** 2).groupby(ranked["d"]).sum()
    ic = cov / np.sqrt(var_x * var_y)
    day["ic"] = ic.replace([np.inf, -np.inf], np.nan).reindex(day.index)

    top = (
        data.sort_values(["d", "pred"], ascending=[True, False], kind="mergesort")
        .groupby("d", sort=False)
        .head(topk)
    )
    bottom = (
        data.sort_values(["d", "pred"], ascending=[True, True], kind="mergesort")
        .groupby("d", sort=False)
        .head(topk)
    )
    true_top = (
        data.sort_values(["d", "fwd"], ascending=[True, False], kind="mergesort")
        .groupby("d", sort=False)
        .head(topk)
    )

    day["top"] = top.groupby("d")["fwd"].mean().reindex(day.index)
    day["bot"] = bottom.groupby("d")["fwd"].mean().reindex(day.index)
    day["n_sel"] = top.groupby("d").size().reindex(day.index).fillna(0)
    day["pos"] = (
        (top["fwd"] > 0).astype(float).groupby(top["d"]).mean().reindex(day.index)
    )
    day["beat"] = (
        (top["fwd"] > top["d"].map(day["uni"]))
        .astype(float)
        .groupby(top["d"])
        .mean()
        .reindex(day.index)
    )
    hits = (
        top[["d", "symbol"]]
        .merge(true_top[["d", "symbol"]], on=["d", "symbol"])
        .groupby("d")
        .size()
    )
    day["prec"] = hits.reindex(day.index, fill_value=0) / float(topk)
    pct_rank = data.groupby("d")["fwd"].rank(pct=True)
    top_pct = pct_rank.loc[top.index]
    day["pct_med"] = top_pct.groupby(top["d"]).median().reindex(day.index)
    day["top_decile"] = (
        (top_pct >= 0.9).astype(float).groupby(top["d"]).mean().reindex(day.index)
    )
    day["top_quintile"] = (
        (top_pct >= 0.8).astype(float).groupby(top["d"]).mean().reindex(day.index)
    )
    if "adv" in top.columns:
        day["adv_med"] = top.groupby("d")["adv"].median().reindex(day.index)
        day["adv_min"] = top.groupby("d")["adv"].min().reindex(day.index)

    topsets = {int(d): set(group["symbol"]) for d, group in top.groupby("d")}
    summary = {
        "n_days": int(len(day)),
        "avg_universe": float(day["n_sub"].mean()),
        "avg_selected": float(day["n_sel"].mean()),
        "ic_mean": float(day["ic"].mean()),
        "ic_std": float(day["ic"].std()),
        "icir": float(day["ic"].mean() / day["ic"].std()),
        "ic_pos_rate": float((day["ic"] > 0).mean()),
        "top_ret": float(day["top"].mean()),
        "uni_ret": float(day["uni"].mean()),
        "top_excess": float((day["top"] - day["uni"]).mean()),
        "win_day": float((day["top"] > day["uni"]).mean()),
        "stock_pos_rate": float(day["pos"].mean()),
        "stock_beat_rate": float(day["beat"].mean()),
        "precision20": float(day["prec"].mean()),
        "precision_lift": float(day["prec"].mean() / (topk / day["n_eff"].mean())),
        "pct_med": float(day["pct_med"].mean()),
        "top_decile_rate": float(day["top_decile"].mean()),
        "top_quintile_rate": float(day["top_quintile"].mean()),
        "bot_ret": float(day["bot"].mean()),
        "ls_spread": float((day["top"] - day["bot"]).mean()),
        "adv_med_yi": float(day["adv_med"].mean() * AMOUNT_TO_YI)
        if "adv_med" in day
        else float("nan"),
        "capacity_port_1pct_yi": float(
            day["adv_med"].mean() * 0.01 * AMOUNT_TO_YI * day["n_sel"].mean()
        )
        if "adv_med" in day
        else float("nan"),
    }
    return summary, day, topsets


def portfolio_stats(
    day: pd.DataFrame,
    horizon: int,
    topsets: dict[int, set[str]],
    cost: float = COST_ROUND_TRIP,
) -> dict:
    dates = list(day.index)
    selected = dates[::horizon]
    rets, unis, turns = [], [], []
    previous: set[str] | None = None
    for date in selected:
        names = topsets.get(int(date), set())
        if not names:
            continue
        rets.append(float(day.at[date, "top"]))
        unis.append(float(day.at[date, "uni"]))
        if previous is None:
            turns.append(1.0)
        else:
            turns.append(1.0 - len(names & previous) / float(len(names)))
        previous = names
    if not rets:
        return {}
    rets_arr = np.asarray(rets, dtype=float)
    unis_arr = np.asarray(unis, dtype=float)
    turn_arr = np.asarray(turns, dtype=float)
    periods = len(rets_arr)
    net_period = rets_arr - turn_arr * cost
    total_gross = float(np.prod(1.0 + rets_arr) - 1.0)
    total_net = float(np.prod(1.0 + net_period) - 1.0)
    total_uni = float(np.prod(1.0 + unis_arr) - 1.0)
    years = max(periods * horizon, 1) / 252.0

    def annualize(total: float) -> float:
        return float((1.0 + total) ** (1.0 / years) - 1.0)

    equity = np.concatenate(([1.0], np.cumprod(1.0 + rets_arr)))
    peak = np.maximum.accumulate(equity)
    max_dd = float((equity / peak - 1.0).min())
    return {
        "port_periods": periods,
        "port_total_gross": total_gross,
        "port_total_net": total_net,
        "port_ann_gross": annualize(total_gross),
        "port_ann_net": annualize(total_net),
        "port_ann_uni": annualize(total_uni),
        "port_excess_ann": annualize(total_net) - annualize(total_uni),
        "port_maxdd": max_dd,
        "port_turnover": float(turn_arr[1:].mean()) if len(turn_arr) > 1 else 1.0,
        "port_win_periods": float((rets_arr > unis_arr).mean()),
    }


def yearly_rows(
    scope: str,
    name: str,
    horizon: int,
    day: pd.DataFrame,
    topsets: dict[int, set[str]],
) -> list[dict]:
    rows = []
    years = sorted({int(d) // 10000 for d in day.index})
    for year in years:
        mask = [int(d) // 10000 == year for d in day.index]
        sub = day.loc[mask]
        if len(sub) < 5:
            continue
        subset_sets = {int(d): topsets[int(d)] for d in sub.index if int(d) in topsets}
        port = portfolio_stats(sub, horizon, subset_sets)
        rows.append(
            {
                "scope": scope,
                "strategy": name,
                "horizon": horizon,
                "year": year,
                "n_days": len(sub),
                "ic_mean": float(sub["ic"].mean()),
                "top_ret": float(sub["top"].mean()),
                "uni_ret": float(sub["uni"].mean()),
                "top_excess": float((sub["top"] - sub["uni"]).mean()),
                "win_day": float((sub["top"] > sub["uni"]).mean()),
                "stock_beat_rate": float(sub["beat"].mean()),
                "precision20": float(sub["prec"].mean()),
                "pct_med": float(sub["pct_med"].mean()),
                "top_decile_rate": float(sub["top_decile"].mean()),
                "port_total_net": port.get("port_total_net", float("nan")),
                "port_maxdd": port.get("port_maxdd", float("nan")),
            }
        )
    return rows


def monthly_rows(name: str, horizon: int, day: pd.DataFrame) -> list[dict]:
    rows = []
    months = day.index.to_numpy(dtype="int64") // 100
    for month in sorted(set(months.tolist())):
        mask = months == month
        sub = day.loc[mask]
        if len(sub) < 3:
            continue
        rows.append(
            {
                "strategy": name,
                "horizon": horizon,
                "month": int(month),
                "n_days": len(sub),
                "ic_mean": float(sub["ic"].mean()),
                "top_excess": float((sub["top"] - sub["uni"]).mean()),
            }
        )
    return rows


def ensemble_frames(
    con: duckdb.DuckDBPyConnection,
    models: list[dict],
    horizon: int,
    start: str,
    end: str,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    con.execute("DROP TABLE IF EXISTS scores")
    con.execute(
        "CREATE TABLE scores (model VARCHAR, symbol VARCHAR, d INTEGER, pred DOUBLE)"
    )
    for model in models:
        sql = f"""
        INSERT INTO scores
        SELECT '{model["model_type"]}' AS model,
               CASE WHEN substr(symbol, 1, 2) IN ('SH', 'SZ', 'BJ')
                    THEN substr(symbol, 3) || '.' || substr(symbol, 1, 2)
                    ELSE symbol END AS symbol,
               CAST(strftime(trade_date, '%Y%m%d') AS INTEGER) AS d,
               CAST(pred AS DOUBLE) AS pred
        FROM read_parquet('{model["pred_path"]}')
        WHERE split = 'test' AND pred IS NOT NULL
          AND trade_date >= DATE '{start}' AND trade_date <= DATE '{end}'
        """
        con.execute(sql)
        log(f"  H{horizon} 融合装载 {model['model_type']}")
    con.execute("DROP TABLE IF EXISTS ens")
    con.execute(
        """
        CREATE TABLE ens AS
        WITH r AS (
          SELECT model, symbol, d,
                 percent_rank() OVER (PARTITION BY model, d ORDER BY pred) AS pr
          FROM scores
        )
        SELECT symbol, d, avg(pr) AS pred, count(*) AS n_models
        FROM r GROUP BY symbol, d
        """
    )
    con.execute("DROP TABLE IF EXISTS cons")
    con.execute(
        """
        CREATE TABLE cons AS
        WITH r AS (
          SELECT model, symbol, d,
                 row_number() OVER (
                   PARTITION BY model, d ORDER BY pred DESC
                 ) AS rk
          FROM scores
        )
        SELECT symbol, d,
               sum(CASE WHEN rk <= 20 THEN 1 ELSE 0 END) AS votes,
               count(*) AS n_models
        FROM r GROUP BY symbol, d
        """
    )
    con.execute("DROP TABLE IF EXISTS scores")
    ens = con.execute("SELECT symbol, d, pred, n_models FROM ens").fetchdf()
    cons = con.execute("SELECT symbol, d, votes, n_models FROM cons").fetchdf()
    return ens, cons


def top20_latest(
    con: duckdb.DuckDBPyConnection,
    frame: pd.DataFrame,
    detail: pd.DataFrame,
    horizon: int,
    label: str,
) -> pd.DataFrame:
    data = frame.dropna(subset=["pred", "fwd"])
    if data.empty:
        return pd.DataFrame()
    latest = int(data["d"].max())
    picks = (
        data[data["d"] == latest]
        .sort_values("pred", ascending=False)
        .head(TOP_K)
        .copy()
    )
    picks["rank"] = range(1, len(picks) + 1)
    picks = picks.merge(detail, on="symbol", how="left")
    picks["horizon"] = horizon
    picks["strategy"] = label
    picks["trade_date"] = latest
    picks["adv_yi"] = picks["adv"] * AMOUNT_TO_YI
    cols = [
        "strategy",
        "horizon",
        "trade_date",
        "rank",
        "symbol",
        "name",
        "industry",
        "pred",
        "adv_yi",
    ]
    return picks[cols]


def md_table(frame: pd.DataFrame, columns: list[str], headers: list[str]) -> str:
    lines = ["| " + " | ".join(headers) + " |"]
    lines.append("|" + "|".join(["---"] * len(headers)) + "|")
    for _, row in frame.iterrows():
        cells = []
        for column in columns:
            value = row[column]
            if isinstance(value, float):
                if np.isnan(value):
                    cells.append("—")
                elif abs(value) < 10:
                    cells.append(f"{value:.4f}")
                else:
                    cells.append(f"{value:.2f}")
            else:
                cells.append(str(value))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def pct(value: float) -> float:
    return value * 100.0


def build_report(
    out_dir: Path,
    summary: pd.DataFrame,
    yearly: pd.DataFrame,
    ensemble: pd.DataFrame,
    windows: dict[int, tuple[str, str]],
    latest: dict[int, pd.DataFrame],
    min_adv_yi: float = 0.0,
) -> None:
    parts = [
        "# 模型 Top20 选股准确性评估（2025 → 2026）",
        "",
        f"生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## 口径",
        "",
        "- 预测只用 `pred.parquet` 的 `split='test'`（训练集外）。",
        "- 收益 = 前复权 `close(T+1+h)/close(T+1)-1`（T 日收盘出信号，T+1 收盘买入）。",
        "- 可交易组合剔除 `trade_mask` 在 T+1 涨停/停牌的标的（`blocked_long`）。",
        "- 组合每 h 个交易日换仓、等权，换手往返成本 0.2%。",
        "- 命中率 = Top20 与「事后真实收益 Top20」的重合度。",
    ]
    if min_adv_yi > 0:
        parts.append(f"- 流动性门槛：只保留 ADV20 ≥ {min_adv_yi:g} 亿元的标的。")
    parts.append("")
    for horizon in sorted(windows):
        start, end = windows[horizon]
        parts.append(f"## T+{horizon}（公共窗口 {start} ~ {end}）")
        parts.append("")
        model_rows = summary[
            (summary["scope"] == "model")
            & (summary["horizon"] == horizon)
            & (summary["variant"] == "tradable")
        ]
        model_rows = model_rows.sort_values("top_excess", ascending=False)
        show = model_rows[
            [
                "model_type",
                "n_days",
                "ic_mean",
                "icir",
                "top_ret",
                "uni_ret",
                "top_excess",
                "win_day",
                "stock_beat_rate",
                "top_decile_rate",
                "pct_med",
                "precision20",
                "precision_lift",
                "ls_spread",
                "port_ann_net",
                "port_excess_ann",
                "port_maxdd",
                "port_turnover",
                "capacity_port_1pct_yi",
            ]
        ].copy()
        for column in [
            "top_ret",
            "uni_ret",
            "top_excess",
            "win_day",
            "stock_beat_rate",
            "top_decile_rate",
            "pct_med",
            "precision20",
            "ls_spread",
            "port_ann_net",
            "port_excess_ann",
            "port_maxdd",
            "port_turnover",
        ]:
            show[column] = show[column].map(pct)
        parts.append(
            md_table(
                show,
                list(show.columns),
                [
                    "模型",
                    "日数",
                    "IC",
                    "ICIR",
                    "Top20日均%",
                    "全市场日均%",
                    "超额%",
                    "日胜率%",
                    "个股跑赢率%",
                    "真实前10%率%",
                    "中位分位",
                    "命中率%",
                    "提升x",
                    "多空差%",
                    "组合年化净%",
                    "年化超额%",
                    "最大回撤%",
                    "换手%",
                    "组合1%容量(亿)",
                ],
            )
        )
        parts.append("")
        ens_rows = (
            ensemble[ensemble["horizon"] == horizon]
            if not ensemble.empty
            else pd.DataFrame()
        )
        if not ens_rows.empty:
            show = ens_rows[
                [
                    "strategy",
                    "n_days",
                    "avg_selected",
                    "ic_mean",
                    "top_ret",
                    "top_excess",
                    "win_day",
                    "top_decile_rate",
                    "pct_med",
                    "precision20",
                    "port_ann_net",
                    "port_maxdd",
                    "port_turnover",
                ]
            ].copy()
            for column in [
                "top_ret",
                "top_excess",
                "win_day",
                "top_decile_rate",
                "pct_med",
                "precision20",
                "port_ann_net",
                "port_maxdd",
                "port_turnover",
            ]:
                show[column] = show[column].map(pct)
            parts.append("**融合 / 共识策略**")
            parts.append("")
            parts.append(
                md_table(
                    show,
                    list(show.columns),
                    [
                        "策略",
                        "日数",
                        "日均选股数",
                        "IC",
                        "Top20日均%",
                        "超额%",
                        "日胜率%",
                        "真实前10%率%",
                        "中位分位",
                        "命中率%",
                        "组合年化净%",
                        "最大回撤%",
                        "换手%",
                    ],
                )
            )
            parts.append("")
        picks = latest.get(horizon)
        if picks is not None and not picks.empty:
            show = picks.head(20)[
                ["rank", "symbol", "name", "industry", "pred", "adv_yi"]
            ].copy()
            show["adv_yi"] = show["adv_yi"].round(2)
            parts.append(
                f"**最新一期融合 Top20（数据日 {int(picks['trade_date'].iloc[0])}）**"
            )
            parts.append("")
            parts.append(
                md_table(
                    show,
                    list(show.columns),
                    ["#", "代码", "名称", "行业", "分数", "ADV20(亿)"],
                )
            )
            parts.append("")

    parts.append("## 分年度（2025 vs 2026）")
    parts.append("")
    show = yearly.sort_values(["horizon", "strategy", "year"]).copy()
    for column in [
        "top_ret",
        "uni_ret",
        "top_excess",
        "win_day",
        "stock_beat_rate",
        "top_decile_rate",
        "pct_med",
        "precision20",
        "port_total_net",
        "port_maxdd",
    ]:
        show[column] = show[column].map(pct)
    parts.append(
        md_table(
            show,
            [
                "horizon",
                "strategy",
                "year",
                "n_days",
                "ic_mean",
                "top_ret",
                "uni_ret",
                "top_excess",
                "win_day",
                "stock_beat_rate",
                "top_decile_rate",
                "pct_med",
                "precision20",
                "port_total_net",
                "port_maxdd",
            ],
            [
                "周期",
                "策略",
                "年份",
                "日数",
                "IC",
                "Top20日均%",
                "全市场日均%",
                "超额%",
                "日胜率%",
                "个股跑赢率%",
                "真实前10%率%",
                "中位分位",
                "命中率%",
                "组合累计净%",
                "最大回撤%",
            ],
        )
    )
    parts.append("")
    parts.append("## 文件")
    parts.append("")
    for name in [
        "summary_common.csv",
        "summary_yearly.csv",
        "monthly_ic.csv",
        "ensemble_summary.csv",
    ]:
        parts.append(f"- `{name}`")
    for horizon in sorted(windows):
        if horizon in latest:
            parts.append(f"- `top20_latest_T{horizon}.csv`")
    (out_dir / "REPORT.md").write_text("\n".join(parts), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-map", default="/tmp/model_map_31.json")
    parser.add_argument("--pred-root", default=PRED_ROOT)
    parser.add_argument("--horizons", default="5,10,20")
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--output-dir", default="/tmp/top20_eval_20261020")
    parser.add_argument("--memory", default="4GB")
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--skip-ensemble", action="store_true")
    parser.add_argument(
        "--min-adv-yi",
        type=float,
        default=0.0,
        help="流动性门槛：只保留 20 日平均成交额 >= 该值（亿元）的标的",
    )
    args = parser.parse_args()

    horizons = [int(x) for x in args.horizons.split(",") if x.strip()]
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    models = json.loads(Path(args.model_map).read_text(encoding="utf-8"))
    for model in models:
        model["pred_path"] = f"{args.pred_root}/{model['model_id']}/pred.parquet"
        if not Path(model["pred_path"]).exists():
            raise FileNotFoundError(model["pred_path"])
        model["pred_path"] = model["pred_path"].replace("/app/", "/app/")

    con = connect(args.memory, args.threads)
    lo = min(int((args.start or "2025-01-01").replace("-", "")), 20250101)
    hi = int((args.end or "2026-12-31").replace("-", ""))
    log(f"加载前复权价格面板 dt={lo}..{hi}")
    close = load_panel(con, "close", lo, hi)
    log(f"加载成交额面板 {close.shape}")
    amount = load_panel(con, "amount", lo, hi)
    log("构建次日映射 / 涨停停牌掩码 / 行业名称")
    nxt = next_day_map(close)
    blocked = load_blocked(con)
    detail = (
        con.execute(
            "SELECT Symbol AS symbol, Name AS name, rs_hyname AS industry "
            f"FROM read_parquet('{DETAIL_FILE}')"
        )
        .fetchdf()
        .drop_duplicates("symbol")
    )
    adv_long = build_adv_long(amount)

    summary_rows: list[dict] = []
    yearly_out: list[dict] = []
    monthly_out: list[dict] = []
    ensemble_rows: list[dict] = []
    windows: dict[int, tuple[str, str]] = {}
    latest_picks: dict[int, pd.DataFrame] = {}

    for horizon in horizons:
        hmodels = [m for m in models if int(m["horizon"]) == horizon]
        if not hmodels:
            continue
        start, end = common_window(con, hmodels, args.start, args.end)
        windows[horizon] = (start, end)
        log(f"=== T+{horizon} 模型 {len(hmodels)} 个，公共窗口 {start}..{end} ===")
        fwd_long = build_fwd_long(close, horizon)

        for model in hmodels:
            t0 = time.time()
            frame = load_model_frame(
                con,
                model["pred_path"],
                fwd_long,
                nxt,
                blocked,
                adv_long,
                start,
                end,
            )
            frame = apply_liquidity(frame, args.min_adv_yi)
            tradable = frame[~frame["blocked"]]
            for variant, subset in (("ideal", frame), ("tradable", tradable)):
                summ, day, topsets = evaluate(subset)
                port = portfolio_stats(day, horizon, topsets)
                summary_rows.append(
                    {
                        "scope": "model",
                        "strategy": f"{model['model_type']}|{variant}",
                        "horizon": horizon,
                        "model_type": model["model_type"],
                        "model_id": model["model_id"],
                        "variant": variant,
                        "start": start,
                        "end": end,
                        **summ,
                        **port,
                    }
                )
                if variant == "tradable":
                    yearly_out.extend(
                        yearly_rows("model", model["model_type"], horizon, day, topsets)
                    )
                    monthly_out.extend(monthly_rows(model["model_type"], horizon, day))
            log(
                f"  {model['model_type']} 完成 {time.time() - t0:.1f}s "
                f"行数={len(frame)}"
            )
            del frame, tradable

        if args.skip_ensemble:
            continue
        log(f"=== T+{horizon} 融合 / 共识 ===")
        ens, cons = ensemble_frames(con, hmodels, horizon, start, end)
        ens_frame = load_model_frame_from_table(
            con, "ens", fwd_long, nxt, blocked, adv_long, start, end
        )
        ens_frame = apply_liquidity(ens_frame, args.min_adv_yi)
        summ, day, topsets = evaluate(ens_frame)
        port = portfolio_stats(day, horizon, topsets)
        ensemble_rows.append(
            {
                "scope": "ensemble",
                "strategy": f"等权排名融合(T+{horizon})",
                "horizon": horizon,
                "start": start,
                "end": end,
                **summ,
                **port,
            }
        )
        yearly_out.extend(yearly_rows("ensemble", "equal_rank", horizon, day, topsets))
        monthly_out.extend(monthly_rows("equal_rank", horizon, day))
        latest_picks[horizon] = top20_latest(
            con, ens_frame, detail, horizon, f"等权排名融合(T+{horizon})"
        )

        bench = day["uni"]
        bench_n = day["n_eff"]
        n_models = len(hmodels)
        ks = [3, 4, 5, 6, 8] if n_models >= 10 else [2, 3, 4]
        for k in ks:
            if k > n_models:
                continue
            sub = cons[cons["votes"] >= k].copy()
            if sub.empty:
                continue
            sub = sub.merge(
                ens[["symbol", "d", "pred"]], on=["symbol", "d"], how="left"
            )
            sub["score"] = sub["votes"] * 10.0 + sub["pred"]
            frame = load_frame_from_df(con, sub, fwd_long, nxt, blocked, adv_long)
            frame = apply_liquidity(frame, args.min_adv_yi)
            frame = frame[~frame["blocked"]]
            if frame.empty:
                continue
            summ, day_k, topsets_k = evaluate(frame, bench=bench, bench_n=bench_n)
            port = portfolio_stats(day_k, horizon, topsets_k)
            ensemble_rows.append(
                {
                    "scope": "consensus",
                    "strategy": f"共识>={k}票(T+{horizon})",
                    "horizon": horizon,
                    "start": start,
                    "end": end,
                    **summ,
                    **port,
                }
            )
            yearly_out.extend(
                yearly_rows("consensus", f"votes>={k}", horizon, day_k, topsets_k)
            )
            monthly_out.extend(monthly_rows(f"votes>={k}", horizon, day_k))
        del ens, cons, ens_frame

    summary = pd.DataFrame(summary_rows)
    yearly = pd.DataFrame(yearly_out)
    monthly = pd.DataFrame(monthly_out)
    ensemble = pd.DataFrame(ensemble_rows)
    summary.to_csv(out_dir / "summary_common.csv", index=False, float_format="%.6f")
    yearly.to_csv(out_dir / "summary_yearly.csv", index=False, float_format="%.6f")
    monthly.to_csv(out_dir / "monthly_ic.csv", index=False, float_format="%.6f")
    ensemble.to_csv(out_dir / "ensemble_summary.csv", index=False, float_format="%.6f")
    for horizon, picks in latest_picks.items():
        picks.to_csv(
            out_dir / f"top20_latest_T{horizon}.csv",
            index=False,
            float_format="%.6f",
        )
    build_report(
        out_dir,
        summary,
        yearly,
        ensemble,
        windows,
        latest_picks,
        min_adv_yi=args.min_adv_yi,
    )
    log(f"输出完成: {out_dir}")
    cols = [
        "scope",
        "strategy",
        "horizon",
        "ic_mean",
        "top_excess",
        "precision20",
        "port_ann_net",
        "port_maxdd",
    ]
    printable = summary[cols].copy()
    if not ensemble.empty:
        printable = pd.concat(
            [printable, ensemble[[c for c in cols if c in ensemble.columns]]],
            ignore_index=True,
        )
    print(printable.to_string(index=False, float_format="%.4f"))


def load_frame_from_df(
    con: duckdb.DuckDBPyConnection,
    scores: pd.DataFrame,
    fwd_long: pd.DataFrame,
    next_map: pd.DataFrame,
    blocked: pd.DataFrame,
    adv_long: pd.DataFrame,
) -> pd.DataFrame:
    con.register("scores_df", scores)
    con.register("fwd_long", fwd_long)
    con.register("next_map", next_map)
    con.register("blocked", blocked)
    con.register("adv_long", adv_long)
    sql = """
    SELECT s.symbol, s.d, s.score AS pred, f.fwd, a.adv,
           (b.symbol IS NOT NULL) AS blocked
    FROM scores_df s
    LEFT JOIN fwd_long f ON s.symbol = f.symbol AND s.d = f.d
    LEFT JOIN next_map nm ON s.d = nm.d
    LEFT JOIN blocked b ON s.symbol = b.symbol AND b.buy_d = nm.buy_d
    LEFT JOIN adv_long a ON s.symbol = a.symbol AND s.d = a.d
    """
    return con.execute(sql).fetchdf()


def load_model_frame_from_table(
    con: duckdb.DuckDBPyConnection,
    table: str,
    fwd_long: pd.DataFrame,
    next_map: pd.DataFrame,
    blocked: pd.DataFrame,
    adv_long: pd.DataFrame,
    start: str,
    end: str,
) -> pd.DataFrame:
    con.register("fwd_long", fwd_long)
    con.register("next_map", next_map)
    con.register("blocked", blocked)
    con.register("adv_long", adv_long)
    sql = f"""
    SELECT e.symbol, e.d, e.pred, f.fwd, a.adv,
           (b.symbol IS NOT NULL) AS blocked
    FROM {table} e
    LEFT JOIN fwd_long f ON e.symbol = f.symbol AND e.d = f.d
    LEFT JOIN next_map nm ON e.d = nm.d
    LEFT JOIN blocked b ON e.symbol = b.symbol AND b.buy_d = nm.buy_d
    LEFT JOIN adv_long a ON e.symbol = a.symbol AND e.d = a.d
    """
    return con.execute(sql).fetchdf()


if __name__ == "__main__":
    main()
