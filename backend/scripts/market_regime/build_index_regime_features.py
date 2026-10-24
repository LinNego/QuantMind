"""构建「大盘指数预测模型」特征表（P0.5：扩展指数 + 两融/北向/ST）。

数据边界（2026-10-24 实测）：
- index_daily：2016-01-04~至今；核心 5 条 + 创业板指/科创50/上证50/中证800/
  中证100/深证100/科创100/北证50（后两者历史较短）；
- stock_daily_latest：2016 至今 1,096 万行；**return_1d 是次日收益（前视），禁用**，
  广度一律用 pct_change/100（当日收益）；
- margin_trading：2016-01-04~2026-09-24（逐股两融）；
- hsgt_north/daily_freq：2017-03-16 起逐股北向（2024-08 后官方停止逐日披露）；
- st_pit：2016-01-04~2026-10-21（PIT ST）。
- turnover_rate/is_st/limit_up/flow_net_amount 在 stock_daily_latest 历史缺失，不用。

输出：
  data/market_regime/features/index_regime_features.parquet
  data/market_regime/features/feature_meta.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

INDEX_CODES: tuple[tuple[str, str], ...] = (
    ("000300.SH", "沪深300"),
    ("000905.SH", "中证500"),
    ("000852.SH", "中证1000"),
    ("000001.SH", "上证综指"),
    ("399001.SZ", "深证成指"),
    ("399006.SZ", "创业板指"),
    ("000688.SH", "科创50"),
    ("000016.SH", "上证50"),
    ("000906.SH", "中证800"),
    ("000903.SH", "中证100"),
    ("399330.SZ", "深证100"),
    ("000698.SH", "科创100"),
    ("899050.BJ", "北证50"),
)

BASE_SECTOR = "/data/quantdb/2_base_sector"

_BREADTH_SQL = """
SELECT trade_date,
       COUNT(*)::int AS mkt_n,
       AVG((pct_change > 0)::int)::float AS mkt_adv_ratio,
       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY (pct_change / 100.0))::float AS mkt_med_ret,
       STDDEV_POP(pct_change / 100.0)::float AS mkt_disp,
       AVG((close > ma20)::int)::float AS mkt_pct_ma20,
       AVG((close > ma60)::int)::float AS mkt_pct_ma60,
       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY pe_ttm)::float AS mkt_med_pe,
       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY pb)::float AS mkt_med_pb,
       SUM(amount)::float AS mkt_amt_sum,
       SUM(total_mv)::float AS mkt_mv_sum,
       AVG((close > ma20)::int) FILTER (WHERE symbol LIKE 'SH%')::float AS sh_pct_ma20,
       AVG((pct_change > 0)::int) FILTER (WHERE symbol LIKE 'SH%')::float AS sh_adv_ratio,
       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY (pct_change / 100.0))
           FILTER (WHERE symbol LIKE 'SH%')::float AS sh_med_ret,
       AVG((close > ma20)::int) FILTER (WHERE symbol LIKE 'SZ%')::float AS sz_pct_ma20,
       AVG((pct_change > 0)::int) FILTER (WHERE symbol LIKE 'SZ%')::float AS sz_adv_ratio,
       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY (pct_change / 100.0))
           FILTER (WHERE symbol LIKE 'SZ%')::float AS sz_med_ret
FROM stock_daily_latest
WHERE trade_date >= :start
GROUP BY trade_date
ORDER BY trade_date
"""


async def _fetch_breadth(start: str) -> pd.DataFrame:
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    async with get_session(read_only=True) as session:
        rows = (
            await session.execute(
                text(_BREADTH_SQL), {"start": date.fromisoformat(start)}
            )
        ).mappings().all()
    df = pd.DataFrame([dict(r) for r in rows])
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    return df


def _load_margin(start: str, end: str) -> pd.DataFrame:
    import duckdb

    con = duckdb.connect()
    try:
        df = con.execute(
            f"""
            WITH m AS (
                SELECT CAST(COALESCE(
                           try_strptime(CAST(time AS VARCHAR), '%Y%m%d'),
                           try_strptime(CAST(time AS VARCHAR), '%Y-%m-%d'),
                           try_strptime(CAST(time AS VARCHAR), '%Y-%m-%d %H:%M:%S')
                       ) AS DATE) AS trade_date,
                       finance_balance, finance_net, slo_volume, slo_net
                FROM read_parquet('{BASE_SECTOR}/margin_trading/dt=*/data.parquet')
            )
            SELECT trade_date,
                   SUM(finance_balance)::DOUBLE AS fin_bal,
                   SUM(finance_net)::DOUBLE AS fin_net,
                   SUM(slo_volume)::DOUBLE AS slo_vol,
                   SUM(slo_net)::DOUBLE AS slo_net
            FROM m
            WHERE trade_date >= DATE '{start}' AND trade_date <= DATE '{end}'
            GROUP BY 1 ORDER BY 1
            """
        ).fetchdf()
    finally:
        con.close()
    if df.empty:
        return df
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    for col in ("fin_bal", "slo_vol"):
        df[f"{col}_chg_5"] = df[col].pct_change(5)
        df[f"{col}_z60"] = (
            df[col] - df[col].rolling(60).mean()
        ) / df[col].rolling(60).std().replace(0, np.nan)
    for col in ("fin_net", "slo_net"):
        df[f"{col}_5"] = df[col].rolling(5).sum()
        df[f"{col}_z60"] = (
            df[col] - df[col].rolling(60).mean()
        ) / df[col].rolling(60).std().replace(0, np.nan)
    return df


def _load_north(start: str, end: str) -> pd.DataFrame:
    import duckdb

    con = duckdb.connect()
    try:
        df = con.execute(
            f"""
            SELECT query_date AS trade_date,
                   SUM(holding_value)::DOUBLE AS north_hold_val,
                   SUM(increase_amount)::DOUBLE AS north_net,
                   COUNT(*)::INTEGER AS north_n
            FROM read_parquet('{BASE_SECTOR}/hsgt_north/daily_freq/*.parquet')
            WHERE query_date >= DATE '{start}' AND query_date <= DATE '{end}'
            GROUP BY 1 ORDER BY 1
            """
        ).fetchdf()
    finally:
        con.close()
    if df.empty:
        return df
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df["north_hold_chg_5"] = df["north_hold_val"].pct_change(5)
    df["north_net_5"] = df["north_net"].rolling(5).sum()
    df["north_net_z60"] = (
        df["north_net"] - df["north_net"].rolling(60).mean()
    ) / df["north_net"].rolling(60).std().replace(0, np.nan)
    return df


def _load_st(start: str, end: str) -> pd.DataFrame:
    import duckdb

    con = duckdb.connect()
    try:
        df = con.execute(
            f"""
            SELECT strptime(time, '%Y%m%d')::DATE AS trade_date,
                   COUNT(*) FILTER (WHERE is_st)::INTEGER AS st_n
            FROM read_parquet('{BASE_SECTOR}/st_pit/st_pit.parquet')
            WHERE strptime(time, '%Y%m%d')::DATE
                  BETWEEN DATE '{start}' AND DATE '{end}'
            GROUP BY 1 ORDER BY 1
            """
        ).fetchdf()
    finally:
        con.close()
    if df.empty:
        return df
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    return df


def _rsi(close: pd.Series, n: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def _index_features(close: pd.Series, volume: pd.Series, amount: pd.Series) -> pd.DataFrame:
    out = pd.DataFrame(index=close.index)
    ret1 = close.pct_change()
    for n in (1, 5, 10, 20, 60, 120):
        out[f"ret_{n}"] = close.pct_change(n)
    for n in (5, 10, 20, 60, 120):
        out[f"ma_gap_{n}"] = close / close.rolling(n).mean() - 1
    out["rsi_6"] = _rsi(close, 6)
    out["rsi_14"] = _rsi(close, 14)
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    out["macd_dif"] = dif
    out["macd_dea"] = dea
    out["macd_hist"] = 2 * (dif - dea)
    ma20 = close.rolling(20).mean()
    sd20 = close.rolling(20).std()
    upper, lower = ma20 + 2 * sd20, ma20 - 2 * sd20
    out["boll_pctb"] = (close - lower) / (upper - lower).replace(0, np.nan)
    for n in (10, 20, 60):
        out[f"vol_{n}"] = ret1.rolling(n).std()
    tr = pd.concat(
        [(close - close.shift(1)).abs(), (close / close.shift(1) - 1).abs()], axis=1
    ).max(axis=1)
    out["atr_pct_14"] = tr.rolling(14).mean()
    out["dd_252"] = close / close.rolling(252, min_periods=60).max() - 1
    out["dist_high_20"] = close / close.rolling(20).max() - 1
    out["dist_low_20"] = close / close.rolling(20).min() - 1
    out["vol_z_20"] = volume / volume.rolling(20).mean() - 1
    out["amt_z_20"] = amount / amount.rolling(20).mean() - 1
    return out


def _rolling_pct(series: pd.Series, window: int = 756, min_periods: int = 252) -> pd.Series:
    return series.rolling(window, min_periods=min_periods).rank(pct=True)


def _load_index_frames(end: str) -> dict[str, pd.DataFrame]:
    from backend.shared.benchmark import load_index_frame

    frames: dict[str, pd.DataFrame] = {}
    for code, _name in INDEX_CODES:
        try:
            df = load_index_frame(
                columns=("close", "volume", "amount"),
                symbols=[code],
                start="2015-01-01",
                end=end,
                market="CN",
            )
        except Exception:
            continue
        if df.empty:
            continue
        df = df.sort_values("time").reset_index(drop=True)
        df["trade_date"] = pd.to_datetime(df["time"]).dt.normalize()
        frames[code] = df
    return frames


def _cross_market_features(a_dates: pd.Series, start: str, end: str) -> pd.DataFrame:
    from backend.shared.benchmark import load_index_frame

    out = pd.DataFrame({"trade_date": pd.to_datetime(a_dates)}).sort_values("trade_date")
    for market, sym, col in (("HK", "HSI.HK", "hsi_ret_1"), ("US", "SPX.US", "spx_ret_1")):
        try:
            df = load_index_frame(
                columns=("close",), symbols=[sym], start=start, end=end, market=market
            )
        except Exception:
            continue
        if df.empty:
            continue
        df = df.sort_values("time").reset_index(drop=True)
        df["date"] = pd.to_datetime(df["time"]).dt.normalize()
        df[col] = df["close"].astype(float).pct_change()
        out = pd.merge_asof(
            out,
            df[["date", col]].sort_values("date"),
            left_on="trade_date",
            right_on="date",
            direction="backward",
            allow_exact_matches=False,
        ).drop(columns=["date"])
    return out


def _add_labels(df: pd.DataFrame, horizons: tuple[int, ...]) -> pd.DataFrame:
    close = df["close"]
    for h in horizons:
        df[f"fwd_ret_{h}"] = close.shift(-(1 + h)) / close.shift(-1) - 1
    return df


def build(start: str, out_dir: Path, only: list[str] | None = None) -> dict[str, Any]:
    end = date.today().isoformat()
    breadth = asyncio.run(_fetch_breadth(start)).sort_values("trade_date").reset_index(drop=True)
    breadth["mkt_turnover"] = breadth["mkt_amt_sum"] / breadth["mkt_mv_sum"].replace(0, np.nan)
    for col in ("mkt_med_pe", "mkt_med_pb", "mkt_turnover"):
        breadth[f"{col}_pct_3y"] = _rolling_pct(breadth[col])
    for col in ("mkt_adv_ratio", "mkt_pct_ma20", "mkt_disp"):
        mean = breadth[col].rolling(60).mean()
        std = breadth[col].rolling(60).std()
        breadth[f"{col}_z60"] = (breadth[col] - mean) / std.replace(0, np.nan)

    for extra in (_load_margin(start, end), _load_north(start, end), _load_st(start, end)):
        if not extra.empty:
            breadth = breadth.merge(extra, on="trade_date", how="left")
    if "st_n" in breadth.columns:
        breadth["st_ratio"] = breadth["st_n"] / breadth["mkt_n"].replace(0, np.nan)
        breadth["st_ratio_z60"] = (
            breadth["st_ratio"] - breadth["st_ratio"].rolling(60).mean()
        ) / breadth["st_ratio"].rolling(60).std().replace(0, np.nan)

    cross = _cross_market_features(breadth["trade_date"], "2014-01-01", end)
    breadth = breadth.merge(cross, on="trade_date", how="left")

    frames = _load_index_frames(end)
    codes = [c for c, _ in INDEX_CODES if not only or c in only]
    rows: list[pd.DataFrame] = []
    for code in codes:
        name = dict(INDEX_CODES)[code]
        df = frames.get(code)
        if df is None or df.empty:
            continue
        feats = _index_features(
            df["close"].astype(float),
            df["volume"].astype(float),
            df["amount"].astype(float),
        )
        one = pd.concat([df[["trade_date", "close"]], feats], axis=1)
        one["index_code"] = code
        one["index_name"] = name
        one = one.merge(breadth, on="trade_date", how="left")
        one["dow"] = one["trade_date"].dt.dayofweek
        one["month"] = one["trade_date"].dt.month
        one["is_month_end"] = (
            one["trade_date"].dt.to_period("M") != one["trade_date"].shift(-1).dt.to_period("M")
        ).astype(int)
        one["is_quarter_end"] = ((one["month"] % 3 == 0) & (one["is_month_end"] == 1)).astype(int)
        gap = one["trade_date"].shift(-1) - one["trade_date"]
        one["pre_holiday_gap"] = (gap.dt.days >= 2).astype(int)
        one = _add_labels(one, (1, 5, 10, 20))
        rows.append(one)
    full = pd.concat(rows, ignore_index=True)
    full = full[full["trade_date"] >= pd.Timestamp(start)].reset_index(drop=True)
    full = full.sort_values(["index_code", "trade_date"]).reset_index(drop=True)

    out_dir.mkdir(parents=True, exist_ok=True)
    full.to_parquet(out_dir / "index_regime_features.parquet", index=False)
    label_cols = {f"fwd_ret_{h}" for h in (1, 5, 10, 20)}
    feature_cols = [
        c
        for c in full.columns
        if c not in {"trade_date", "index_code", "index_name", "close"} | label_cols
    ]
    meta = {
        "generated_at": date.today().isoformat(),
        "start": start,
        "end": str(full["trade_date"].max().date()),
        "rows": int(len(full)),
        "indexes": sorted(full["index_code"].unique().tolist()),
        "feature_count": len(feature_cols),
        "features": feature_cols,
        "coverage": {c: round(float(full[c].notna().mean()), 4) for c in feature_cols},
        "label_coverage": {
            c: round(float(full[c].notna().mean()), 4) for c in sorted(label_cols)
        },
        "notes": [
            "return_1d 是**次日**收益（前视），禁止用于特征；广度改用 pct_change/100（当日收益）",
            "两融 2016-2026-09；北向 2017-03 起（2024-08 后官方停止逐日披露）；ST 为 PIT",
            "港美特征只取严格早于 A 股 T 日的收盘",
        ],
    }
    (out_dir / "feature_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return meta


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2016-01-01")
    parser.add_argument("--out", default="/data/market_regime/features")
    parser.add_argument("--indices", default="", help="逗号分隔的 index_code，空=全部")
    args = parser.parse_args()
    only = [x.strip() for x in args.indices.split(",") if x.strip()] or None
    meta = build(args.start, Path(args.out), only)
    print(
        json.dumps(
            {k: meta[k] for k in ("rows", "feature_count", "start", "end")},
            ensure_ascii=False,
        )
    )
    low = {k: v for k, v in meta["coverage"].items() if v < 0.5}
    print("low coverage:", json.dumps(low, ensure_ascii=False))


if __name__ == "__main__":
    main()
