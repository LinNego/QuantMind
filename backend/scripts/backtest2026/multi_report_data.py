"""多模型 PDF 报告的数据装载。

报告消费 ``multi_model`` 落盘的 ``summary.json`` 与各 CSV。这里只做读取和
轻量整形，不在报告层重算任何指标，保证 PDF 与本评估产物逐位对得上。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from .model_registry import MODEL_NAMES, MODEL_ORDER
from .multi_portfolio import FAMILY_ORDER, FAMILY_TITLES

SHORT_FAMILY = {"top10": "Top10", "top20": "Top20", "rolling": "滚动 Top5"}
HORIZON_TITLES = {5: "T+5", 10: "T+10"}


def display_name(key: str) -> str:
    return MODEL_NAMES.get(key, key)


def family_title(family: str) -> str:
    return FAMILY_TITLES.get(family, family)


def _read_csv(path: Path, **kwargs: object) -> pd.DataFrame:
    if not path.is_file():
        return pd.DataFrame()
    return pd.read_csv(path, **kwargs)


def _read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


@dataclass
class MultiBundle:
    """报告所需的全部产物；缺文件时返回空表，由章节自行降级。"""

    out_dir: Path
    summary: dict
    inventory: pd.DataFrame
    benchmark: pd.DataFrame
    per_stock: pd.DataFrame
    quality_ranking: dict[int, pd.DataFrame]
    portfolio_ranking: dict[tuple[int, str], pd.DataFrame]
    quality_daily: dict[tuple[int, str], pd.DataFrame]
    equity: dict[tuple[int, str, str], pd.DataFrame]
    monthly_returns: dict[tuple[int, str], pd.DataFrame]
    monthly_ic: dict[int, pd.DataFrame]
    monthly_hit: dict[int, pd.DataFrame]
    deciles: dict[int, pd.DataFrame]
    cross: dict

    @property
    def horizons(self) -> tuple[int, ...]:
        return tuple(sorted(self.quality_ranking))

    def models(self, horizon: int) -> list[str]:
        frame = self.quality_ranking.get(horizon)
        if frame is None or frame.empty:
            return []
        present = set(frame["model"])
        return [key for key in MODEL_ORDER if key in present]

    def all_models(self) -> list[str]:
        present: set[str] = set()
        for horizon in self.horizons:
            present.update(self.models(horizon))
        return [key for key in MODEL_ORDER if key in present]

    def quality_row(self, horizon: int, model: str) -> dict:
        frame = self.quality_ranking.get(horizon)
        if frame is None or frame.empty:
            return {}
        rows = frame[frame["model"] == model]
        return {} if rows.empty else rows.iloc[0].to_dict()

    def portfolio_row(self, horizon: int, family: str, model: str) -> dict:
        frame = self.portfolio_ranking.get((horizon, family))
        if frame is None or frame.empty:
            return {}
        rows = frame[frame["model"] == model]
        return {} if rows.empty else rows.iloc[0].to_dict()

    def quality_summary(self, horizon: int, model: str) -> dict:
        return self.summary.get("quality", {}).get(str(horizon), {}).get(model, {})

    def portfolio_summary(self, horizon: int, family: str, model: str) -> dict:
        return (
            self.summary.get("portfolios", {})
            .get(str(horizon), {})
            .get(family, {})
            .get(model, {})
        )

    def quality_stats(self, horizon: int) -> dict:
        return self.summary.get("stats", {}).get(str(horizon), {}).get("quality", {})

    def portfolio_stats(self, horizon: int, family: str) -> dict:
        return (
            self.summary.get("stats", {})
            .get(str(horizon), {})
            .get("portfolios", {})
            .get(family, {})
        )

    def pending(self) -> pd.DataFrame:
        if self.inventory.empty:
            return pd.DataFrame()
        return self.inventory[~self.inventory["ready"].astype(bool)]


def load_bundle(out_dir: Path) -> MultiBundle:
    summary = _read_json(out_dir / "summary.json")
    horizons = tuple(
        int(horizon)
        for horizon in (summary.get("manifest", {}).get("horizons") or [5, 10])
    )
    inventory = _read_csv(out_dir / "inventory.csv")
    benchmark = _read_csv(out_dir / "benchmark.csv", index_col=0, parse_dates=True)
    per_stock = _read_csv(out_dir / "per_stock_coverage.csv", index_col=0)

    quality_ranking: dict[int, pd.DataFrame] = {}
    monthly_ic: dict[int, pd.DataFrame] = {}
    monthly_hit: dict[int, pd.DataFrame] = {}
    deciles: dict[int, pd.DataFrame] = {}
    portfolio_ranking: dict[tuple[int, str], pd.DataFrame] = {}
    monthly_returns: dict[tuple[int, str], pd.DataFrame] = {}
    quality_daily: dict[tuple[int, str], pd.DataFrame] = {}
    equity: dict[tuple[int, str, str], pd.DataFrame] = {}

    for horizon in horizons:
        quality_ranking[horizon] = _read_csv(out_dir / f"quality_league_{horizon}.csv")
        monthly_ic[horizon] = _read_csv(
            out_dir / f"quality_monthly_ic_{horizon}.csv", index_col=0
        )
        monthly_hit[horizon] = _read_csv(
            out_dir / f"quality_monthly_top10_hit_{horizon}.csv", index_col=0
        )
        deciles[horizon] = _read_csv(out_dir / f"deciles_{horizon}.csv", index_col=0)
        for family in FAMILY_ORDER:
            portfolio_ranking[(horizon, family)] = _read_csv(
                out_dir / f"portfolio_league_{family}_{horizon}.csv"
            )
            monthly_returns[(horizon, family)] = _read_csv(
                out_dir / f"monthly_returns_{family}_{horizon}.csv", index_col=0
            )

    models = [
        key
        for key in MODEL_ORDER
        if any(key in quality_ranking[h].get("model", pd.Series()).tolist() for h in horizons)
    ]
    for horizon in horizons:
        for model in models:
            daily = _read_csv(
                out_dir / f"quality_daily_{horizon}_{model}.csv",
                index_col=0,
                parse_dates=True,
            )
            if not daily.empty:
                quality_daily[(horizon, model)] = daily
            for family in FAMILY_ORDER:
                frame = _read_csv(
                    out_dir / f"equity_{family}_{horizon}_{model}.csv",
                    index_col=0,
                    parse_dates=True,
                )
                if not frame.empty:
                    equity[(horizon, family, model)] = frame

    return MultiBundle(
        out_dir=out_dir,
        summary=summary,
        inventory=inventory,
        benchmark=benchmark,
        per_stock=per_stock,
        quality_ranking=quality_ranking,
        portfolio_ranking=portfolio_ranking,
        quality_daily=quality_daily,
        equity=equity,
        monthly_returns=monthly_returns,
        monthly_ic=monthly_ic,
        monthly_hit=monthly_hit,
        deciles=deciles,
        cross=summary.get("cross_horizon", {}),
    )


def quality_matrix(bundle: MultiBundle, horizon: int, column: str) -> pd.Series:
    """按模型排名顺序返回某质量指标。"""
    frame = bundle.quality_ranking.get(horizon)
    if frame is None or frame.empty:
        return pd.Series(dtype=float)
    ordered = frame.sort_values("rank")
    return ordered.set_index("model")[column].astype(float)


def portfolio_matrix(
    bundle: MultiBundle, horizon: int, family: str, column: str
) -> pd.Series:
    frame = bundle.portfolio_ranking.get((horizon, family))
    if frame is None or frame.empty:
        return pd.Series(dtype=float)
    ordered = frame.sort_values("rank")
    return ordered.set_index("model")[column].astype(float)


def latest_monthly(bundle: MultiBundle, horizon: int, family: str) -> pd.Series:
    frame = bundle.monthly_returns.get((horizon, family))
    if frame is None or frame.empty:
        return pd.Series(dtype=float)
    return frame.iloc[-1]


def finite(value: object) -> bool:
    try:
        return bool(np.isfinite(float(value)))
    except (TypeError, ValueError):
        return False
