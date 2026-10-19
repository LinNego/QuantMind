"""容量约束回测：单票买入不超过当日成交额的固定比例。

按资金规模重跑 Top10 / Top20 / 滚动 Top5，观察：
- 1% 参与率下成交被截断多少、哪些组合先失真；
- 资金从 200 万放大到 1 亿时，Top10 相对 Top20 的优势是否消失；
- 容量约束下的收益、回撤、填充率与买入请求完成度。

口径与主回测一致（t+1 收盘买入、e+H 收盘卖出、整手、双边万 3、无滑点）。
唯一变化是 ``per_name`` 随资金规模等比缩放，保证不同资金档位都尽可能满仓；
200 万档与主回测的 Top10=20 万/只、Top20=10 万/只完全一致。
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from .config import COMMISSION_RATE, OUT_DIR
from .data import (
    attach_listing_age,
    build_market_panel,
    load_extended_predictions,
    read_first_seen,
)
from .deep_common import TEST_END, TEST_START
from .metrics import performance_summary, trade_summary
from .model_registry import HORIZONS, MODEL_ORDER, resolve_model_run
from .multi_portfolio import FAMILY_ORDER, common_end_date
from .runner import _write_json
from .strategies import StrategySpec, build_pred_by_date, run_strategy

LOG = logging.getLogger("backtest2026.capacity")

DEFAULT_CAPITALS = (2_000_000.0, 10_000_000.0, 50_000_000.0, 100_000_000.0)
DEFAULT_PARTICIPATIONS = (None, 0.01)


@dataclass(frozen=True)
class CapacitySettings:
    out_dir: Path
    start: str = TEST_START
    end: str = TEST_END
    price_start: str = "2024-11-01"
    price_end: str = "2026-10-16"
    models: tuple[str, ...] = MODEL_ORDER
    horizons: tuple[int, ...] = HORIZONS
    capitals: tuple[float, ...] = DEFAULT_CAPITALS
    participations: tuple[float | None, ...] = DEFAULT_PARTICIPATIONS
    extension_dir: Path | None = None


def build_capacity_spec(
    family: str,
    horizon: int,
    capital: float,
    participation: float | None,
) -> StrategySpec:
    if family == "top10":
        return StrategySpec(
            mode="cohort",
            k=10,
            horizon=horizon,
            initial_capital=capital,
            per_name=capital / 10.0,
            commission=COMMISSION_RATE,
            max_participation=participation,
        )
    if family == "top20":
        return StrategySpec(
            mode="cohort",
            k=20,
            horizon=horizon,
            initial_capital=capital,
            per_name=capital / 20.0,
            commission=COMMISSION_RATE,
            max_participation=participation,
        )
    if family == "rolling":
        return StrategySpec(
            mode="rolling",
            k=5,
            horizon=horizon,
            initial_capital=capital,
            per_name=None,
            commission=COMMISSION_RATE,
            max_participation=participation,
        )
    raise ValueError(f"未知组合类型: {family}")


def _participation_label(value: float | None) -> str:
    return "none" if value is None else f"{value:.3f}".rstrip("0").rstrip(".")


def _require_listing_age(panel) -> None:
    """次新闸门依赖 ``age_days``；未附加时全部标的会被静默拒绝。"""
    if panel.age_days is None or int(panel.age_days.notna().to_numpy().sum()) == 0:
        raise RuntimeError("上市天数未附加，容量回测的次新闸门会拒绝全部标的")


def _evaluate(
    result,
    *,
    model: str,
    horizon: int,
    family: str,
    capital: float,
    participation: float | None,
    common_end: pd.Timestamp,
) -> dict[str, object]:
    equity = result.equity["equity"].dropna().loc[:common_end]
    performance = performance_summary(equity)
    trades = result.trades
    buys = trades[trades["side"] == "buy"] if not trades.empty else trades
    fill_ratio = (
        float(buys["fill_ratio"].mean())
        if not buys.empty and "fill_ratio" in buys.columns
        else float("nan")
    )
    requested = (
        float(buys["requested_amount"].sum())
        if not buys.empty and "requested_amount" in buys.columns
        else float("nan")
    )
    filled = float(buys["amount"].sum()) if not buys.empty else 0.0
    # 成交表由多个组合拼接，``cap_binding`` 可能是 object dtype；
    # 直接 fillna+astype 在 pandas 2.x 触发未来行为警告，这里逐值判定。
    binding = (
        int(
            buys["cap_binding"]
            .map(lambda value: bool(value) if pd.notna(value) else False)
            .sum()
        )
        if not buys.empty and "cap_binding" in buys.columns
        else 0
    )
    skips = result.skips
    capacity_skips = (
        int((skips["reason"] == "capacity").sum()) if not skips.empty else 0
    )
    cash_skips = (
        int((skips["reason"] == "insufficient_cash").sum()) if not skips.empty else 0
    )
    invested_ratio = (
        float((result.equity["market_value"] / result.equity["equity"]).mean())
        if not result.equity.empty
        else float("nan")
    )
    trade = trade_summary(trades, capital)
    return {
        "model": model,
        "horizon": horizon,
        "family": family,
        "capital": capital,
        "participation": _participation_label(participation),
        "total_return": performance.get("total_return", np.nan),
        "annual_return": performance.get("annual_return", np.nan),
        "sharpe": performance.get("sharpe", np.nan),
        "max_drawdown": performance.get("max_drawdown", np.nan),
        "avg_fill_ratio": fill_ratio,
        "requested_amount": requested,
        "filled_amount": filled,
        "filled_share": filled / requested
        if requested and np.isfinite(requested)
        else np.nan,
        "capacity_binding_trades": binding,
        "buy_trades": int(len(buys)),
        "capacity_skips": capacity_skips,
        "cash_skips": cash_skips,
        "avg_invested_ratio": invested_ratio,
        "turnover_x": trade.get("turnover_x", np.nan),
        "final_equity": performance.get("final_equity", np.nan),
    }


def run(settings: CapacitySettings) -> dict[str, object]:
    settings.out_dir.mkdir(parents=True, exist_ok=True)
    canonical = resolve_model_run(5, "LGB")
    if canonical.run_dir is None:
        raise RuntimeError("缺少规范模型运行目录")
    panel = build_market_panel(
        settings.price_start, settings.price_end, with_amount=True
    )
    attach_listing_age(panel, read_first_seen(canonical.run_dir))
    _require_listing_age(panel)
    LOG.info(
        "容量回测面板 %s ~ %s，股票池 %d",
        settings.price_start,
        settings.price_end,
        len(panel.symbols),
    )
    rows: list[dict[str, object]] = []
    for horizon in settings.horizons:
        for model in settings.models:
            run_spec = resolve_model_run(horizon, model)
            if run_spec.run_dir is None:
                LOG.warning("缺少 %s T+%d 训练目录", model, horizon)
                continue
            pred = load_extended_predictions(
                run_spec.run_dir,
                horizon,
                model,
                settings.start,
                settings.end,
                settings.extension_dir,
            )
            pred_by_date = build_pred_by_date(pred)
            common_end = common_end_date(panel, pred_by_date)
            for family in FAMILY_ORDER:
                for capital in settings.capitals:
                    for participation in settings.participations:
                        spec = build_capacity_spec(
                            family, horizon, capital, participation
                        )
                        result = run_strategy(panel, pred_by_date, spec)
                        row = _evaluate(
                            result,
                            model=model,
                            horizon=horizon,
                            family=family,
                            capital=capital,
                            participation=participation,
                            common_end=common_end,
                        )
                        rows.append(row)
            LOG.info(
                "%s T+%d 完成 %d 组容量回测",
                model,
                horizon,
                len(FAMILY_ORDER)
                * len(settings.capitals)
                * len(settings.participations),
            )
    runs = pd.DataFrame(rows)
    runs.to_csv(settings.out_dir / "capacity_runs.csv", index=False)

    summary = (
        runs.groupby(["horizon", "family", "capital", "participation"])
        .agg(
            models=("model", "nunique"),
            mean_total_return=("total_return", "mean"),
            median_total_return=("total_return", "median"),
            mean_sharpe=("sharpe", "mean"),
            mean_max_drawdown=("max_drawdown", "mean"),
            mean_fill_ratio=("avg_fill_ratio", "mean"),
            mean_invested_ratio=("avg_invested_ratio", "mean"),
            binding_trades=("capacity_binding_trades", "mean"),
            capacity_skips=("capacity_skips", "mean"),
        )
        .reset_index()
    )
    summary.to_csv(settings.out_dir / "capacity_summary.csv", index=False)

    pivot = runs.pivot_table(
        index=["model", "horizon", "capital", "participation"],
        columns="family",
        values="total_return",
        aggfunc="mean",
    ).reset_index()
    if {"top10", "top20"}.issubset(pivot.columns):
        pivot["top10_minus_top20"] = pivot["top10"] - pivot["top20"]
        pivot.to_csv(settings.out_dir / "capacity_top10_vs_top20.csv", index=False)
        advantage = (
            pivot.groupby(["horizon", "capital", "participation"])
            .agg(
                models=("model", "nunique"),
                mean_top10=("top10", "mean"),
                mean_top20=("top20", "mean"),
                mean_diff=("top10_minus_top20", "mean"),
                median_diff=("top10_minus_top20", "median"),
                top10_win_rate=(
                    "top10_minus_top20",
                    lambda values: float((values > 0).mean()),
                ),
            )
            .reset_index()
        )
        advantage.to_csv(settings.out_dir / "capacity_top10_advantage.csv", index=False)

    manifest = {
        "start": settings.start,
        "end": settings.end,
        "price_start": settings.price_start,
        "price_end": settings.price_end,
        "models": list(settings.models),
        "horizons": list(settings.horizons),
        "capitals": list(settings.capitals),
        "participations": [_participation_label(p) for p in settings.participations],
        "runs": int(len(runs)),
    }
    _write_json(settings.out_dir / "capacity_manifest.json", manifest)
    LOG.info("容量回测完成：%d 组", len(runs))
    return manifest


def _parse_participations(values: list[str]) -> tuple[float | None, ...]:
    parsed: list[float | None] = []
    for value in values:
        text = str(value).strip().lower()
        if text in {"none", "null", "off", "无"}:
            parsed.append(None)
        else:
            parsed.append(float(text))
    return tuple(parsed)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=OUT_DIR / "deep_capacity")
    parser.add_argument("--start", default=TEST_START)
    parser.add_argument("--end", default=TEST_END)
    parser.add_argument("--price-start", default="2024-11-01")
    parser.add_argument("--price-end", default="2026-10-16")
    parser.add_argument("--models", nargs="*", default=list(MODEL_ORDER))
    parser.add_argument("--horizons", nargs="*", type=int, default=list(HORIZONS))
    parser.add_argument(
        "--capitals", nargs="*", type=float, default=list(DEFAULT_CAPITALS)
    )
    parser.add_argument(
        "--participations",
        nargs="*",
        default=["none", "0.01"],
        help="单票成交额占比上限；none 表示不约束",
    )
    parser.add_argument("--extension-dir", type=Path, default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    args = parse_args(argv)
    settings = CapacitySettings(
        out_dir=args.out,
        start=args.start,
        end=args.end,
        price_start=args.price_start,
        price_end=args.price_end,
        models=tuple(args.models),
        horizons=tuple(args.horizons),
        capitals=tuple(args.capitals),
        participations=_parse_participations(args.participations),
        extension_dir=args.extension_dir,
    )
    run(settings)


if __name__ == "__main__":
    main()
