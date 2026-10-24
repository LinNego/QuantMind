"""构建「行业一级板块预测」特征表（P0 试点，当前成分口径）。

数据事实（2026-10-24）：
- `2_base_sector/sector_concept/sector_members.parquet`：**当前成分快照**（无日期列），
  行业一级 48 个、二级 80 个、概念 420 个；本脚本用行业一级。
- `stock_daily_latest`：2016 至今；`pct_change` 为当日收益（%/100），
  `return_1d` 是次日收益（前视，禁用）。
- 市场级特征（广度/估值/两融/北向/ST/跨市场）复用 index_regime_features 的
  000300 行（这些列按日相同）。

**偏差声明**：当前成分回溯到 2016 会引入成分变更/幸存者偏差；报告必须标注。
生产级板块模型需要历史行业成分（PIT）。

输出：
  data/market_regime/features/sector_regime_features.parquet
  data/market_regime/features/sector_feature_meta.json
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

from backend.scripts.market_regime.build_index_regime_features import (
    _index_features,
    _rolling_pct,
)

SECTOR_TYPE = "行业板块(一级)"
MARKET_COL_PREFIXES = (
    "mkt_",
    "sh_",
    "sz_",
    "fin_",
    "slo_",
    "north_",
    "st_",
    "hsi_",
    "spx_",
    "dow",
    "month",
    "is_month_end",
    "is_quarter_end",
    "pre_holiday_gap",
)

_SECTOR_SQL = """
SELECT s.trade_date,
       m.sector_code,
       AVG(s.pct_change / 100.0)::float AS sec_eq_ret,
       SUM((s.pct_change / 100.0) * s.total_mv)
           FILTER (WHERE s.total_mv IS NOT NULL AND s.total_mv > 0)
         / NULLIF(SUM(s.total_mv)
           FILTER (WHERE s.total_mv IS NOT NULL AND s.total_mv > 0), 0)::float AS sec_cap_ret,
       AVG((s.pct_change > 0)::int)::float AS sec_adv_ratio,
       STDDEV_POP(s.pct_change / 100.0)::float AS sec_disp,
       AVG((s.close > s.ma20)::int)::float AS sec_pct_ma20,
       AVG((s.close > s.ma60)::int)::float AS sec_pct_ma60,
       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY s.pe_ttm)::float AS sec_med_pe,
       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY s.pb)::float AS sec_med_pb,
       SUM(s.volume)::float AS sec_vol_sum,
       SUM(s.amount)::float AS sec_amt_sum,
       COUNT(*)::int AS sec_n
FROM stock_daily_latest s
JOIN sector_map m ON s.symbol = m.symbol
WHERE s.trade_date >= :start
GROUP BY s.trade_date, m.sector_code
ORDER BY s.trade_date, m.sector_code
"""


async def _fetch_sector_aggregates(
    start: str, members: pd.DataFrame, sector_col: str, symbol_col: str
) -> pd.DataFrame:
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    async with get_session(read_only=True) as session:
        await session.execute(
            text("CREATE TEMP TABLE IF NOT EXISTS sector_map (sector_code text, symbol text)")
        )
        await session.execute(text("TRUNCATE sector_map"))
        rows = [
            {"sector_code": str(r[sector_col]), "symbol": str(r[symbol_col])}
            for _, r in members.iterrows()
        ]
        if rows:
            await session.execute(
                text("INSERT INTO sector_map (sector_code, symbol) VALUES (:sector_code, :symbol)"),
                rows,
            )
        result = (
            await session.execute(
                text(_SECTOR_SQL), {"start": date.fromisoformat(start)}
            )
        ).mappings().all()
    df = pd.DataFrame([dict(r) for r in result])
    if not df.empty:
        df["trade_date"] = pd.to_datetime(df["trade_date"])
    return df


def _load_sector_members() -> tuple[pd.DataFrame, str, str]:
    import duckdb

    from backend.shared.stock_utils import StockCodeUtil

    path = "/data/quantdb/2_base_sector/sector_concept/sector_members.parquet"
    con = duckdb.connect()
    try:
        df = con.execute(
            "SELECT SectorCode, SectorName, SectorType, Symbol FROM read_parquet(?) "
            "WHERE SectorType = ?",
            [path, SECTOR_TYPE],
        ).fetchdf()
    finally:
        con.close()
    df = df.dropna(subset=["SectorCode", "Symbol"]).copy()
    df["Symbol"] = df["Symbol"].map(lambda s: StockCodeUtil.to_prefix(str(s)))
    return df, "SectorCode", "Symbol"


def _market_columns(index_features: Path) -> pd.DataFrame:
    df = pd.read_parquet(index_features)
    base = df[df["index_code"] == "000300.SH"].copy()
    keep = [
        c
        for c in base.columns
        if c == "trade_date"
        or any(c == p or c.startswith(p) for p in MARKET_COL_PREFIXES)
    ]
    return base[keep].drop_duplicates(subset=["trade_date"]).sort_values("trade_date")


def _add_labels(df: pd.DataFrame, horizons: tuple[int, ...]) -> pd.DataFrame:
    close = df["close"]
    for h in horizons:
        df[f"fwd_ret_{h}"] = close.shift(-(1 + h)) / close.shift(-1) - 1
    return df


def build(
    start: str,
    index_features: Path,
    out_dir: Path,
    max_sectors: int = 15,
    weight: str = "eq",
) -> dict[str, Any]:
    members, sector_col, symbol_col = _load_sector_members()
    # 试点：成员数最多的前 N 个行业（仍属当前成分口径）
    sizes = (
        members.groupby([sector_col, "SectorName"]).size().reset_index(name="members")
        .sort_values("members", ascending=False)
    )
    pilot = sizes.head(max_sectors)
    members = members[members[sector_col].isin(pilot[sector_col].tolist())]

    aggs = asyncio.run(_fetch_sector_aggregates(start, members, sector_col, symbol_col))
    if aggs.empty:
        raise RuntimeError("板块聚合为空（成分/行情未匹配）")
    market = _market_columns(index_features)

    rows: list[pd.DataFrame] = []
    # aggs 只有 sector_code；补名称
    names = members.drop_duplicates(sector_col).set_index(sector_col)["SectorName"].to_dict()
    for code, g in aggs.groupby("sector_code"):
        g = g.sort_values("trade_date").reset_index(drop=True)
        ret = g["sec_cap_ret"] if weight == "cap" else g["sec_eq_ret"]
        if weight == "cap":
            ret = ret.fillna(g["sec_eq_ret"])
        close = (1 + ret.fillna(0)).cumprod()
        g["close"] = close
        feats = _index_features(
            close,
            g["sec_vol_sum"].astype(float),
            g["sec_amt_sum"].astype(float),
        )
        one = pd.concat([g, feats], axis=1)
        one["index_code"] = f"SEC_L1_{code}"
        one["index_name"] = str(names.get(code) or code)
        one = one.merge(market, on="trade_date", how="left")
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
    full.to_parquet(out_dir / "sector_regime_features.parquet", index=False)
    label_cols = {f"fwd_ret_{h}" for h in (1, 5, 10, 20)}
    feature_cols = [
        c
        for c in full.columns
        if c
        not in {
            "trade_date",
            "index_code",
            "index_name",
            "close",
            "sector_code",
            "sec_eq_ret",
        }
        | label_cols
    ]
    meta = {
        "generated_at": date.today().isoformat(),
        "start": start,
        "end": str(full["trade_date"].max().date()),
        "rows": int(len(full)),
        "sectors": int(full["index_code"].nunique()),
        "sector_type": SECTOR_TYPE,
        "feature_count": len(feature_cols),
        "features": feature_cols,
        "coverage": {c: round(float(full[c].notna().mean()), 4) for c in feature_cols},
        "label_coverage": {c: round(float(full[c].notna().mean()), 4) for c in sorted(label_cols)},
        "pilot": pilot.to_dict("records"),
        "weight": weight,
        "notes": [
            "**当前成分快照回溯**：存在成分变更/幸存者偏差，报告必须标注",
            "return_1d 是次日收益（前视），禁用；板块收益用 pct_change/100 等权合成",
            "市场级特征（广度/两融/北向/ST/跨市场）取自 index_regime_features 的 000300 行",
        ],
    }
    (out_dir / "sector_feature_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    return meta


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2016-01-01")
    parser.add_argument(
        "--index-features",
        default="/data/market_regime/features/index_regime_features.parquet",
    )
    parser.add_argument("--out", default="/data/market_regime/features")
    parser.add_argument("--max-sectors", type=int, default=15)
    parser.add_argument("--weight", choices=("eq", "cap"), default="eq")
    args = parser.parse_args()
    meta = build(
        args.start, Path(args.index_features), Path(args.out), args.max_sectors, args.weight
    )
    print(
        json.dumps(
            {k: meta[k] for k in ("rows", "sectors", "feature_count", "start", "end")},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
