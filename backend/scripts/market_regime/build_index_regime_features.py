"""构建「大盘指数预测模型」特征表（P0）。

数据边界（2026-10-24 实测）：
- 指数日线 2016-01-04~至今（5 条指数）；
- 全市场截面 stock_daily_latest：2016 至今 1,096 万行；
  - return_1d / close / volume / amount 100% 覆盖；
  - ma20 ≈100%（2018+）、ma60 97%、PE 2018+、PB 2017+、total_mv 2017+；
  - turnover_rate / is_st / limit_up_today / flow_net_amount 历史缺失（≈0%），**不使用**；
- HSI/SPX 日线用于跨市场特征。

输出：
  data/market_regime/features/index_regime_features.parquet
  data/market_regime/features/feature_meta.json（列清单/覆盖率/日期范围）

用法：
  python backend/scripts/market_regime/build_index_regime_features.py \
      --start 2016-01-01 --out /data/market_regime/features
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
)

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


def _load_index_frames(start: str, end: str) -> dict[str, pd.DataFrame]:
    from backend.shared.benchmark import load_index_frame

    frames: dict[str, pd.DataFrame] = {}
    for code, _name in INDEX_CODES:
        # 多取 200 个自然日，保证 MA120/252 回撤在样本起点前已就绪
        df = load_index_frame(
            columns=("close", "volume", "amount"),
            symbols=[code],
            start="2015-01-01",
            end=end,
            market="CN",
        )
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
        # A 股 T 日信号只能看到 **严格早于 T** 的港/美收盘
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
        # 信号日 T → T+1 收盘进场 → T+1+H 收盘出场
        df[f"fwd_ret_{h}"] = close.shift(-(1 + h)) / close.shift(-1) - 1
    return df


def build(start: str, out_dir: Path) -> dict[str, Any]:
    end = date.today().isoformat()
    breadth = asyncio.run(_fetch_breadth(start))
    breadth = breadth.sort_values("trade_date").reset_index(drop=True)
    # 派生：换手代理、估值分位、广度 z
    breadth["mkt_turnover"] = breadth["mkt_amt_sum"] / breadth["mkt_mv_sum"].replace(0, np.nan)
    for col in ("mkt_med_pe", "mkt_med_pb", "mkt_turnover"):
        breadth[f"{col}_pct_3y"] = _rolling_pct(breadth[col])
    for col in ("mkt_adv_ratio", "mkt_pct_ma20", "mkt_disp"):
        mean = breadth[col].rolling(60).mean()
        std = breadth[col].rolling(60).std()
        breadth[f"{col}_z60"] = (breadth[col] - mean) / std.replace(0, np.nan)

    cross = _cross_market_features(breadth["trade_date"], "2014-01-01", end)
    breadth = breadth.merge(cross, on="trade_date", how="left")

    frames = _load_index_frames(start, end)
    rows: list[pd.DataFrame] = []
    for code, name in INDEX_CODES:
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
        # 日历
        one["dow"] = one["trade_date"].dt.dayofweek
        one["month"] = one["trade_date"].dt.month
        one["is_month_end"] = (
            one["trade_date"].dt.to_period("M") != one["trade_date"].shift(-1).dt.to_period("M")
        ).astype(int)
        one["is_quarter_end"] = (
            (one["month"] % 3 == 0) & (one["is_month_end"] == 1)
        ).astype(int)
        gap = one["trade_date"].shift(-1) - one["trade_date"]
        one["pre_holiday_gap"] = (gap.dt.days >= 2).astype(int)
        one = _add_labels(one, (5, 10, 20))
        rows.append(one)
    full = pd.concat(rows, ignore_index=True)
    full = full[full["trade_date"] >= pd.Timestamp(start)].reset_index(drop=True)
    full = full.sort_values(["index_code", "trade_date"]).reset_index(drop=True)

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "index_regime_features.parquet"
    full.to_parquet(path, index=False)
    feature_cols = [
        c
        for c in full.columns
        if c
        not in {
            "trade_date",
            "index_code",
            "index_name",
            "close",
            "fwd_ret_5",
            "fwd_ret_10",
            "fwd_ret_20",
        }
    ]
    meta = {
        "generated_at": date.today().isoformat(),
        "start": start,
        "end": str(full["trade_date"].max().date()),
        "rows": int(len(full)),
        "indexes": sorted(full["index_code"].unique().tolist()),
        "feature_count": len(feature_cols),
        "features": feature_cols,
        "coverage": {
            c: round(float(full[c].notna().mean()), 4) for c in feature_cols
        },
        "label_coverage": {
            f"fwd_ret_{h}": round(float(full[f"fwd_ret_{h}"].notna().mean()), 4)
            for h in (5, 10, 20)
        },
        "notes": [
            "return_1d 是**次日**收益（前视），禁止用于特征；广度改用 pct_change/100（当日收益）",
            "turnover_rate/is_st/limit_up/flow_net_amount 历史缺失，未使用",
            "广度/估值为全市场口径；SH/SZ 分交易所",
            "港美特征只取严格早于 A 股 T 日的收盘（merge_asof backward, allow_exact_matches=False）",
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
    args = parser.parse_args()
    meta = build(args.start, Path(args.out))
    print(json.dumps({k: meta[k] for k in ("rows", "feature_count", "start", "end")}, ensure_ascii=False))
    low = {k: v for k, v in meta["coverage"].items() if v < 0.5}
    print("low coverage:", json.dumps(low, ensure_ascii=False))


if __name__ == "__main__":
    main()
