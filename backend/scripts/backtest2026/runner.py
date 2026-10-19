"""2026 年 TopK 回测主程序（T+5 / T+10）。

用法::

    python -m backend.scripts.backtest2026.runner            # 全量
    python -m backend.scripts.backtest2026.runner --models T5
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

from .config import (
    COMMISSION_RATE,
    EXTEND_END,
    INDEX_SYMBOLS,
    INITIAL_CAPITAL,
    MODELS,
    OUT_DIR,
    ROLLING_K,
    SIGNAL_START,
    STAMP_DUTY_RATE,
    STORED_PRED_END,
    TOP20_K,
    TOP20_PER_NAME,
)
from .data import (
    MarketPanel,
    attach_listing_age,
    build_market_panel,
    load_index_closes,
    load_prediction_panel,
    forward_return_matrix,
)
from .filters import signal_eligible_mask
from .metrics import (
    index_summary,
    monthly_equity,
    monthly_quality,
    performance_summary,
    quality_by_day,
    summarize_quality,
    trade_summary,
)
from .strategies import StrategySpec, StrategyResult, build_pred_by_date, run_strategy


def _jsonable(value):
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return None if not math.isfinite(float(value)) else float(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, (pd.Timestamp,)):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, float):
        return None if not math.isfinite(value) else value
    if isinstance(value, pd.DataFrame):
        return value.to_dict(orient="records")
    if isinstance(value, pd.Series):
        return value.to_dict()
    return value


def _write_json(path: Path, payload) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(_jsonable(payload), handle, ensure_ascii=False, indent=2)


def _pct(value: float | None) -> str:
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return "—"
    return f"{value:.2%}"


def _num(value: float | None, digits: int = 3) -> str:
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return "—"
    return f"{value:.{digits}f}"


def _md_table(frame: pd.DataFrame, percent_cols: set[str] | None = None) -> str:
    percent_cols = percent_cols or set()
    columns = [str(c) for c in frame.columns]
    lines = [
        "| " + " | ".join(columns) + " |",
        "|" + "|".join(["---"] * len(columns)) + "|",
    ]
    for _, row in frame.iterrows():
        cells = []
        for column in frame.columns:
            value = row[column]
            if column in percent_cols:
                cells.append(_pct(float(value)) if pd.notna(value) else "—")
            elif isinstance(value, (float, np.floating)):
                cells.append(_num(float(value)))
            else:
                cells.append("—" if pd.isna(value) else str(value))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _summary_frame(summary: dict, rows: list[tuple[str, str]]) -> pd.DataFrame:
    return pd.DataFrame(
        [{"指标": label, "数值": summary.get(key)} for key, label in rows]
    )


def _benchmark_daily(
    panel: MarketPanel,
    pred_by_date: dict[pd.Timestamp, pd.Series],
    initial_capital: float,
) -> pd.DataFrame:
    """等权可交易池基准：信号日 t 选池，t+1 日按权重持有，日度再平衡。"""
    position = {day: i for i, day in enumerate(panel.dates)}
    rows = []
    for day in sorted(pred_by_date):
        idx = position.get(day)
        if idx is None or idx + 1 >= len(panel.dates):
            continue
        pred = pred_by_date[day]
        mask = signal_eligible_mask(
            pred,
            day,
            panel.limit_up,
            panel.limit_down,
            panel.suspended,
            panel.age_days,
            panel.is_st,
        )
        selected = mask[mask].index
        if len(selected) == 0:
            continue
        today = panel.close_qfq.iloc[idx][selected]
        tomorrow = panel.close_qfq.iloc[idx + 1][selected]
        returns = (tomorrow / today - 1.0).replace([np.inf, -np.inf], np.nan).dropna()
        if returns.empty:
            continue
        rows.append(
            {
                "date": panel.dates[idx + 1],
                "return": float(returns.mean()),
                "n": int(len(returns)),
            }
        )
    if not rows:
        empty_index = pd.DatetimeIndex([], name="date")
        return pd.DataFrame(
            columns=["return", "n", "equity"], index=empty_index, dtype=float
        )
    frame = pd.DataFrame(rows).set_index("date").sort_index()
    frame["equity"] = initial_capital * (1.0 + frame["return"]).cumprod()
    return frame


def _per_stock_table(pred_panel: pd.DataFrame, panel: MarketPanel) -> pd.DataFrame:
    grouped = pred_panel.groupby("symbol")
    table = grouped.agg(
        days=("pred", "size"),
        first_date=("trade_date", "min"),
        last_date=("trade_date", "max"),
        mean_pred=("pred", "mean"),
    )
    latest = (
        pred_panel.sort_values("trade_date")
        .groupby("symbol")
        .tail(1)
        .set_index("symbol")["pred"]
    )
    table["latest_pred"] = latest
    table["is_st"] = panel.is_st.reindex(table.index).fillna(False)
    last_day = panel.dates[-1]
    table["last_price"] = [panel.mark_price(symbol, last_day) for symbol in table.index]
    table["status"] = "predicted"

    missing = panel.symbols.difference(table.index)
    if len(missing):
        missing_rows = pd.DataFrame(
            {
                "days": 0,
                "first_date": pd.NaT,
                "last_date": pd.NaT,
                "mean_pred": np.nan,
                "latest_pred": np.nan,
                "is_st": panel.is_st.reindex(missing).fillna(False),
                "last_price": [
                    panel.mark_price(symbol, last_day) for symbol in missing
                ],
                "status": "never_predicted",
            },
            index=missing,
        )
        table = pd.concat([table, missing_rows])
    return table.sort_index()


def _variants(base: StrategySpec) -> dict[str, StrategySpec]:
    return {
        "baseline": base,
        "no_st_filter": replace(base, exclude_st=False),
        "no_limit_filter": replace(base, exclude_limits=False),
        "no_new_filter": replace(base, exclude_new=False),
        "no_filters": replace(
            base, exclude_st=False, exclude_limits=False, exclude_new=False
        ),
        "stamp_duty_5bp": replace(base, stamp=STAMP_DUTY_RATE),
        "slippage_5bp": replace(base, slippage=0.0005),
    }


def _signal_gate_counts(
    pred_by_date: dict[pd.Timestamp, pd.Series], panel: MarketPanel
) -> dict[str, int]:
    """原始 Top20 中被各闸门命中的行数（用于解释消融为何可能无差异）。"""
    counts = {
        "top20_rows": 0,
        "st": 0,
        "limit_up": 0,
        "limit_down": 0,
        "suspended": 0,
        "new": 0,
    }
    for day, pred in pred_by_date.items():
        raw = pred.nlargest(TOP20_K)
        symbols = raw.index
        counts["top20_rows"] += int(len(symbols))
        counts["st"] += int(panel.is_st.reindex(symbols).fillna(False).sum())
        counts["limit_up"] += int(panel.limit_up.loc[day, symbols].sum())
        counts["limit_down"] += int(panel.limit_down.loc[day, symbols].sum())
        counts["suspended"] += int(panel.suspended.loc[day, symbols].sum())
        counts["new"] += int((panel.age_days.loc[day, symbols].fillna(-1) < 60).sum())
    return counts


def _strategy_block(result: StrategyResult, initial_capital: float) -> dict:
    equity = result.equity["equity"]
    performance = performance_summary(equity)
    trades = trade_summary(result.trades, initial_capital)
    monthly = monthly_equity(equity)
    return {
        "performance": performance,
        "trades": trades,
        "monthly": {str(period): float(value) for period, value in monthly.items()},
        "n_signals": result.signal_days,
        "n_selected": result.selected_total,
        "n_skips": int(len(result.skips)),
        "skip_reasons": result.skips["reason"].value_counts().to_dict()
        if not result.skips.empty
        else {},
    }


def run_model(spec, index_closes: pd.DataFrame, panel: MarketPanel) -> dict:
    run_dir = spec.run_dir
    pred_panel, first_seen = load_prediction_panel(
        run_dir, OUT_DIR, spec.name, SIGNAL_START
    )
    attach_listing_age(panel, first_seen)
    window = (pred_panel["trade_date"] >= pd.Timestamp(SIGNAL_START)) & (
        pred_panel["trade_date"] <= pd.Timestamp(EXTEND_END)
    )
    pred_panel = (
        pred_panel[window].sort_values(["trade_date", "symbol"]).reset_index(drop=True)
    )
    pred_panel.to_parquet(OUT_DIR / f"pred_panel_{spec.name}.parquet", index=False)

    label_matrix = pred_panel.pivot(
        index="trade_date", columns="symbol", values="label_return"
    )
    forward = forward_return_matrix(panel.close_qfq, spec.horizon)
    pred_by_date = build_pred_by_date(pred_panel)

    quality = quality_by_day(
        pred_by_date, forward, panel, topk=TOP20_K, label_matrix=label_matrix
    )
    quality.to_csv(OUT_DIR / f"quality_daily_{spec.name}.csv")
    monthly = monthly_quality(quality)
    monthly.to_csv(OUT_DIR / f"quality_monthly_{spec.name}.csv")

    stored_mask = quality.index <= pd.Timestamp(STORED_PRED_END)
    quality_blocks = {
        "stored_window": summarize_quality(quality[stored_mask]),
        "extended_window": summarize_quality(quality),
        "monthly": monthly.to_dict(orient="index"),
    }

    top20_spec = StrategySpec(
        mode="cohort",
        k=TOP20_K,
        horizon=spec.horizon,
        initial_capital=INITIAL_CAPITAL,
        per_name=TOP20_PER_NAME,
        commission=COMMISSION_RATE,
    )
    rolling_spec = StrategySpec(
        mode="rolling",
        k=ROLLING_K,
        horizon=spec.horizon,
        initial_capital=INITIAL_CAPITAL,
        per_name=None,
        commission=COMMISSION_RATE,
    )

    top20 = run_strategy(panel, pred_by_date, top20_spec)
    top20.equity.to_csv(OUT_DIR / f"top20_{spec.name}_equity.csv")
    top20.trades.to_csv(OUT_DIR / f"top20_{spec.name}_trades.csv", index=False)

    rolling = run_strategy(panel, pred_by_date, rolling_spec)
    rolling.equity.to_csv(OUT_DIR / f"rolling_{spec.name}_equity.csv")
    rolling.trades.to_csv(OUT_DIR / f"rolling_{spec.name}_trades.csv", index=False)
    rolling.skips.to_csv(OUT_DIR / f"rolling_{spec.name}_skips.csv", index=False)

    top20_variants = {}
    for name, variant in _variants(top20_spec).items():
        result = run_strategy(panel, pred_by_date, variant)
        top20_variants[name] = _strategy_block(result, INITIAL_CAPITAL)["performance"]

    rolling_variants = {}
    for name, variant in _variants(rolling_spec).items():
        result = run_strategy(panel, pred_by_date, variant)
        rolling_variants[name] = _strategy_block(result, INITIAL_CAPITAL)["performance"]

    benchmark = _benchmark_daily(panel, pred_by_date, INITIAL_CAPITAL)
    benchmark.to_csv(OUT_DIR / f"benchmark_ew_{spec.name}.csv")
    equity_start = rolling.equity.index[0]
    equity_end = rolling.equity.index[-1]
    bench_summary = performance_summary(benchmark["equity"])
    indices = index_summary(index_closes, equity_start, equity_end)

    per_stock = _per_stock_table(pred_panel, panel)
    per_stock.to_csv(OUT_DIR / f"per_stock_{spec.name}.csv")

    return {
        "model": spec.name,
        "horizon": spec.horizon,
        "run_dir": str(run_dir),
        "pred_rows": int(len(pred_panel)),
        "pred_days": int(pred_panel["trade_date"].nunique()),
        "pred_symbols": int(pred_panel["symbol"].nunique()),
        "quality": quality_blocks,
        "top20": _strategy_block(top20, INITIAL_CAPITAL),
        "rolling": _strategy_block(rolling, INITIAL_CAPITAL),
        "top20_variants": top20_variants,
        "rolling_variants": rolling_variants,
        "benchmark_ew": bench_summary,
        "benchmark_indices": indices,
        "per_stock_missing": int((per_stock["status"] != "predicted").sum()),
        "signal_gate_counts": _signal_gate_counts(pred_by_date, panel),
    }


def render_report(results: list[dict], extension_summary: list[dict]) -> str:
    lines = [
        "# 2026 年 TopK 组合回测报告（T+5 / T+10）",
        "",
        "> 模型：`T5_Cross300_LGB_Split20261018` / `T10_Cross300_LGB_Split20261018`",
        f"> 信号窗口：{SIGNAL_START} ~ {EXTEND_END}（训练 pred 至 {STORED_PRED_END}，之后为模型外推）",
        "> 成交：信号日收盘出分 → 次日收盘买入 → 持有 H 个交易日收盘卖出；双边万 3。",
        "",
        "## 0. 外推校验",
        "",
    ]
    if extension_summary:
        rows = []
        for item in extension_summary:
            validation = item.get("validation", {})
            rows.append(
                {
                    "模型": item["model"],
                    "外推日期数": item["dates"],
                    "行数": item["rows"],
                    "股票数": item["symbols"],
                    "重叠相关系数": validation.get("corr"),
                    "重叠秩相关": validation.get("rank_corr"),
                    "最大绝对差": validation.get("max_abs_diff"),
                    "Top100 重合": validation.get("top100_overlap"),
                }
            )
        lines.append(_md_table(pd.DataFrame(rows)))
    else:
        lines.append("未找到外推校验结果。")
    lines.extend(["", "## 1. 预测质量", ""])

    quality_rows = []
    for result in results:
        for window_name, label in (
            ("stored_window", "训练 pred 窗口"),
            ("extended_window", "含外推全窗口"),
        ):
            summary = result["quality"][window_name]
            quality_rows.append(
                {
                    "模型": result["model"],
                    "窗口": label,
                    "交易日": summary.get("days"),
                    "IC(可交易池)": summary.get("ic_eligible_mean"),
                    "ICIR": summary.get("icir"),
                    "IC>0 占比": summary.get("ic_positive_ratio"),
                    "IC(训练标签)": summary.get("ic_label_mean"),
                    "方向胜率": summary.get("hit_all_mean"),
                    "Top20 平均收益": summary.get("top_ret_mean"),
                    "Top20 命中率": summary.get("top_hit_mean"),
                    "多空": summary.get("long_short_mean"),
                }
            )
    lines.append(
        _md_table(
            pd.DataFrame(quality_rows),
            percent_cols={
                "IC(可交易池)",
                "IC>0 占比",
                "IC(训练标签)",
                "方向胜率",
                "Top20 平均收益",
                "Top20 命中率",
                "多空",
            },
        )
    )
    lines.append("")
    lines.append(
        "> 说明：`IC(训练标签)` 只覆盖训练 pred 已有的日期（外推日无标签）；"
        "`方向胜率` 为全可交易池 sign(pred)=sign(实际收益) 的比例。"
    )
    lines.append("")

    for section, result in enumerate(results, start=2):
        gates = result.get("signal_gate_counts", {})
        lines.extend([f"## {section}. {result['model']} 组合结果", ""])
        lines.append(
            f"> 信号日原始 Top20 共 {gates.get('top20_rows', 0)} 行；其中 ST "
            f"{gates.get('st', 0)} 行、涨停 {gates.get('limit_up', 0)} 行、跌停 "
            f"{gates.get('limit_down', 0)} 行、停牌 {gates.get('suspended', 0)} 行、"
            f"上市不足 60 日 {gates.get('new', 0)} 行。"
        )
        lines.append("")
        for key, title in (
            ("top20", "Top20（每只 10 万 / 合计 200 万）"),
            ("rolling", "滚动 Top5"),
        ):
            block = result[key]
            performance = block["performance"]
            trades = block["trades"]
            rows = [
                ("区间", f"{performance.get('start')} ~ {performance.get('end')}"),
                ("总收益", _pct(performance.get("total_return"))),
                ("年化收益", _pct(performance.get("annual_return"))),
                ("年化波动", _pct(performance.get("annual_vol"))),
                ("Sharpe", _num(performance.get("sharpe"))),
                ("最大回撤", _pct(performance.get("max_drawdown"))),
                ("日胜率", _pct(performance.get("daily_win_rate"))),
                ("期末权益", f"{performance.get('final_equity', float('nan')):,.0f}"),
                ("买入笔数", trades.get("n_buy_orders")),
                ("卖出笔数", trades.get("n_sell_orders")),
                ("单笔平均收益", _pct(trades.get("avg_trade_return"))),
                ("单笔胜率", _pct(trades.get("trade_win_rate"))),
                ("总费用", f"{trades.get('total_fees', float('nan')):,.0f}"),
                ("换手（倍初始资金）", _num(trades.get("turnover_x"), 2)),
                ("顺延退出", trades.get("deferred_exits")),
                ("强制退出", trades.get("forced_exits")),
                ("剔除记录", block.get("n_skips")),
            ]
            lines.append(f"### {title}")
            lines.append("")
            lines.append(_md_table(pd.DataFrame(rows, columns=["指标", "数值"])))
            lines.append("")
            if block.get("monthly"):
                monthly = pd.DataFrame(
                    [
                        {"月份": key, "收益": value}
                        for key, value in block["monthly"].items()
                    ]
                )
                lines.append(_md_table(monthly, percent_cols={"收益"}))
                lines.append("")

        lines.extend(["### 消融（滚动 Top5）", ""])
        rows = []
        for name, performance in result["rolling_variants"].items():
            rows.append(
                {
                    "变体": name,
                    "总收益": performance.get("total_return"),
                    "年化": performance.get("annual_return"),
                    "Sharpe": performance.get("sharpe"),
                    "最大回撤": performance.get("max_drawdown"),
                }
            )
        lines.append(
            _md_table(
                pd.DataFrame(rows),
                percent_cols={"总收益", "年化", "最大回撤"},
            )
        )
        lines.append("")

        lines.extend(["### 基准", ""])
        benchmark = result["benchmark_ew"]
        rows = [
            ("等权可交易池", _pct(benchmark.get("total_return"))),
            ("等权最大回撤", _pct(benchmark.get("max_drawdown"))),
        ]
        for code, value in result["benchmark_indices"].items():
            rows.append((code, _pct(value)))
        lines.append(_md_table(pd.DataFrame(rows, columns=["基准", "区间收益"])))
        lines.append("")

    limitations_index = 2 + len(results)
    artifacts_index = limitations_index + 1
    lines.extend(
        [
            f"## {limitations_index}. 口径与局限",
            "",
            "1. **ST 为静态名单**：QuantDB 无 2026-09-14 之前的逐日 ST 标记，使用 2026-10-16 的 198 只 ST 名单回填，存在前视；消融表中的 `no_st_filter` 给出量级。",
            "2. **训练标签用后复权，组合收益用前复权**：IC 同时给出两种口径；组合收益以 `daily_forward` 为准。",
            "3. **北交所不在股票池**：训练装载时剔除 BJ，回测不补。",
            "4. **外推仅至 2026-09-14**：09-15 起 `gap_mined` 因子分区不完整（91.7% NaN），300 维特征不可靠。",
            "5. **收盘成交假设**：未建模盘中冲击；`slippage_5bp` 变体给出成本敏感性。",
            "6. **上市日代理**：以预测面板首次出现日代替真实上市日，次新股剔除阈值 60 个交易日。",
            "7. **逐股覆盖**：预测面板覆盖 `l1_factors` 因子源的全部 5,202 只；未覆盖的 25 只中 23 只在 2026-08-06 后上市，另 2 只（`SH689009`、`SZ302132`）不在因子源内，`per_stock_*.csv` 标记为 `never_predicted`。",
            "",
            f"## {artifacts_index}. 产物清单",
            "",
            "- `pred_panel_T5.parquet` / `pred_panel_T10.parquet`：训练 pred + 外推的逐股预测面板",
            "- `pred_extend_T5.parquet` / `pred_extend_T10.parquet`：2026-08-28 ~ 2026-09-14 外推",
            "- `quality_daily_*.csv`、`quality_monthly_*.csv`：预测质量",
            "- `top20_*_trades.csv`、`top20_*_equity.csv`：Top20 组合",
            "- `rolling_*_trades.csv`、`rolling_*_equity.csv`、`rolling_*_skips.csv`：滚动 Top5",
            "- `per_stock_*.csv`：逐股覆盖与最新预测",
            "- `metrics_T5.json` / `metrics_T10.json`：全部指标",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", nargs="*", default=[spec.name for spec in MODELS])
    args = parser.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    panel = build_market_panel()
    index_closes = load_index_closes(
        panel.dates[0].strftime("%Y-%m-%d"), panel.dates[-1].strftime("%Y-%m-%d")
    )
    wanted_indices = [f"{code[-2:]}{code[:6]}" for code in INDEX_SYMBOLS]
    index_closes = index_closes[
        [column for column in index_closes.columns if column in wanted_indices]
    ]

    extension_summary = []
    ext_path = OUT_DIR / "extend_summary.json"
    if ext_path.exists():
        with open(ext_path, encoding="utf-8") as handle:
            extension_summary = json.load(handle)

    results = []
    for spec in MODELS:
        if spec.name not in args.models:
            continue
        result = run_model(spec, index_closes, panel)
        _write_json(OUT_DIR / f"metrics_{spec.name}.json", result)
        results.append(result)

    report = render_report(results, extension_summary)
    (OUT_DIR / "report_2026_topk.md").write_text(report, encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
