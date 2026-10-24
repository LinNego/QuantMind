"""构建申万一级行业板块特征表（PIT 官方指数口径，2026-10-24 v2）。

与旧版 `build_sector_regime_features.py` 的根本区别：
- 旧版：用**当前成分快照**回溯合成板块指数 → 成分变更/幸存者偏差；
- 本版：直接用**申万官方一级行业指数日线**（成分由申万维护，历史序列本身
  就是 PIT 口径），从源头消除幸存者偏差。

输入：
  data/market_regime/external/sw_l1_index_daily.parquet
  data/market_regime/features/index_regime_features.parquet（取市场级列）
输出：
  data/market_regime/features/sw_sector_regime_features.parquet
  data/market_regime/features/sw_sector_feature_meta.json
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from backend.scripts.market_regime.build_index_regime_features import (
    LABEL_PREFIXES,
    MARKET_COL_PREFIXES,
    _add_labels,
    _add_risk_labels,
    _index_features,
)

HORIZONS = (1, 5, 10, 20)


def _market_columns(index_features: Path) -> pd.DataFrame:
    df = pd.read_parquet(index_features)
    base = df[df["index_code"] == "000300.SH"].copy()
    keep = [
        c
        for c in base.columns
        if c == "trade_date"
        or any(c == p or c.startswith(p) for p in MARKET_COL_PREFIXES)
    ]
    calendar_cols = ["dow", "month", "is_month_end", "is_quarter_end", "pre_holiday_gap"]
    keep = [c for c in keep if c not in calendar_cols]
    return (
        base[keep]
        .drop_duplicates(subset=["trade_date"])
        .sort_values("trade_date")
        .reset_index(drop=True)
    )


def build(
    sw_index_path: Path,
    index_features_path: Path,
    out_dir: Path,
) -> dict[str, Any]:
    raw = pd.read_parquet(sw_index_path)
    raw["trade_date"] = pd.to_datetime(raw["trade_date"]).dt.normalize()
    raw = raw.sort_values(["sector_code", "trade_date"]).reset_index(drop=True)
    market = _market_columns(index_features_path)

    rows: list[pd.DataFrame] = []
    for code, g in raw.groupby("sector_code"):
        g = g.sort_values("trade_date").reset_index(drop=True)
        if len(g) < 300:
            continue
        feats = _index_features(
            g["close"].astype(float),
            g["volume"].astype(float),
            g["amount"].astype(float),
        )
        one = pd.concat([g[["trade_date", "close"]], feats], axis=1)
        one["index_code"] = f"SW_L1_{code.split('.')[0]}"
        one["index_name"] = str(g["sector_name"].iloc[0])
        one["sector_code"] = code
        one = one.merge(market, on="trade_date", how="left")
        one["dow"] = one["trade_date"].dt.dayofweek
        one["month"] = one["trade_date"].dt.month
        one["is_month_end"] = (
            one["trade_date"].dt.to_period("M")
            != one["trade_date"].shift(-1).dt.to_period("M")
        ).astype(int)
        one["is_quarter_end"] = (
            (one["month"] % 3 == 0) & (one["is_month_end"] == 1)
        ).astype(int)
        gap = one["trade_date"].shift(-1) - one["trade_date"]
        one["pre_holiday_gap"] = (gap.dt.days >= 2).astype(int)
        one = _add_labels(one, HORIZONS)
        one = _add_risk_labels(one, HORIZONS)
        rows.append(one)

    if not rows:
        raise RuntimeError("申万一级行业特征为空（指数历史不足 300 行）")
    full = pd.concat(rows, ignore_index=True)
    full = full.sort_values(["index_code", "trade_date"]).reset_index(drop=True)

    out_dir.mkdir(parents=True, exist_ok=True)
    full.to_parquet(out_dir / "sw_sector_regime_features.parquet", index=False)
    label_cols = {c for c in full.columns if c.startswith(LABEL_PREFIXES)}
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
        }
        | label_cols
    ]
    meta = {
        "generated_at": date.today().isoformat(),
        "source": "SW L1 official index (akshare.index_hist_sw)",
        "rows": int(len(full)),
        "sectors": int(full["index_code"].nunique()),
        "start": str(full["trade_date"].min().date()),
        "end": str(full["trade_date"].max().date()),
        "feature_count": len(feature_cols),
        "features": feature_cols,
        "coverage": {c: round(float(full[c].notna().mean()), 4) for c in feature_cols},
        "label_coverage": {
            c: round(float(full[c].notna().mean()), 4) for c in sorted(label_cols)
        },
        "notes": [
            "申万官方一级行业指数：成分由申万维护，历史序列天然 PIT，无幸存者偏差",
            "旧版当前成分快照合成的板块结果仍保留在 market_regime/reports，仅作对照",
            "市场级特征（广度/两融/北向/ST/期指基差/宏观/ETF）取自 index_regime_features 的 000300 行",
            "波动/回撤标签仅用未来价格，禁止进特征",
        ],
    }
    (out_dir / "sw_sector_feature_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return meta


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--sw-indices",
        default="/data/market_regime/external/sw_l1_index_daily.parquet",
    )
    parser.add_argument(
        "--index-features",
        default="/data/market_regime/features/index_regime_features.parquet",
    )
    parser.add_argument("--out", default="/data/market_regime/features")
    args = parser.parse_args()
    meta = build(Path(args.sw_indices), Path(args.index_features), Path(args.out))
    print(
        json.dumps(
            {
                k: meta[k]
                for k in ("rows", "sectors", "feature_count", "start", "end")
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
