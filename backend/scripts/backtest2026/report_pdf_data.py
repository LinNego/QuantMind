"""PDF 报告的数据装配与统计口径。

本模块只负责把回测产物读成可渲染的对象，不生成图表、不拼 HTML。
所有组合收益均为扣费后的净值口径；毛收益与净收益的差异在交易明细中单独计算。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

INITIAL_CAPITAL = 2_000_000.0
MODEL_KEYS = ("T5", "T10")
MODEL_TITLES = {"T5": "T+5", "T10": "T+10"}
INDEX_TITLES = {
    "000300.SH": "沪深 300",
    "000905.SH": "中证 500",
    "000852.SH": "中证 1000",
}
INDEX_ORDER = ("000300.SH", "000905.SH", "000852.SH")


@dataclass(frozen=True)
class ModelReport:
    key: str
    horizon: int
    metrics: dict[str, Any]
    meta: dict[str, Any]
    quality_daily: pd.DataFrame
    monthly_quality: pd.DataFrame
    equity_top20: pd.Series
    equity_rolling: pd.Series
    trades_top20: pd.DataFrame
    trades_rolling: pd.DataFrame
    round_trips_top20: pd.DataFrame
    round_trips_rolling: pd.DataFrame
    stats_top20: dict[str, float]
    stats_rolling: dict[str, float]
    drawdown_top20: dict[str, Any]
    drawdown_rolling: dict[str, Any]
    nw_ic: dict[str, float]
    attribution_top20: pd.DataFrame
    attribution_rolling: pd.DataFrame
    benchmark_ew: pd.Series
    index_closes: pd.DataFrame

    @property
    def title(self) -> str:
        return MODEL_TITLES[self.key]


@dataclass(frozen=True)
class ReportBundle:
    artifact_dir: Path
    generated_at: datetime
    models: dict[str, ModelReport]


def _read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _read_equity(path: Path) -> pd.Series:
    frame = pd.read_csv(path, parse_dates=["date"])
    frame = frame.set_index("date").sort_index()
    return frame["equity"].astype(float)


def _read_trades(path: Path) -> pd.DataFrame:
    return pd.read_csv(
        path,
        parse_dates=["date", "signal_date", "scheduled_exit"],
    )


def round_trips(trades: pd.DataFrame) -> pd.DataFrame:
    """把买卖流水配对成回合交易，并计算毛收益/净收益/持有天数。"""
    buy_columns = ["bucket", "symbol", "date", "price", "amount", "fee"]
    sell_columns = ["bucket", "symbol", "date", "price", "amount", "fee", "net_pnl"]
    if trades.empty:
        return pd.DataFrame(
            columns=[
                "bucket",
                "symbol",
                "entry_date",
                "exit_date",
                "entry_price",
                "exit_price",
                "buy_amount",
                "sell_amount",
                "buy_fee",
                "sell_fee",
                "net_pnl",
                "gross_ret",
                "net_ret",
                "hold_days",
            ]
        )
    buys = trades.loc[trades["side"] == "buy", buy_columns].rename(
        columns={
            "date": "entry_date",
            "price": "entry_price",
            "amount": "buy_amount",
            "fee": "buy_fee",
        }
    )
    sells = trades.loc[trades["side"] == "sell", sell_columns].rename(
        columns={
            "date": "exit_date",
            "price": "exit_price",
            "amount": "sell_amount",
            "fee": "sell_fee",
        }
    )
    merged = sells.merge(buys, on=["bucket", "symbol"], how="inner")
    if merged.empty:
        return merged.assign(gross_ret=[], net_ret=[], hold_days=[])
    merged["gross_ret"] = merged["exit_price"] / merged["entry_price"] - 1.0
    invested = merged["buy_amount"] + merged["buy_fee"]
    merged["net_ret"] = merged["net_pnl"] / invested
    merged["hold_days"] = np.busday_count(
        merged["entry_date"].values.astype("datetime64[D]"),
        merged["exit_date"].values.astype("datetime64[D]"),
    )
    return merged.sort_values(["entry_date", "symbol"]).reset_index(drop=True)


def round_trip_stats(round_trip_frame: pd.DataFrame) -> dict[str, float]:
    """回合交易的胜率、盈亏比、收益分位等。"""
    if round_trip_frame.empty:
        return {
            "n": 0.0,
            "gross_avg": float("nan"),
            "gross_win": float("nan"),
            "net_avg": float("nan"),
            "net_win": float("nan"),
            "median": float("nan"),
            "p10": float("nan"),
            "p90": float("nan"),
            "avg_win": float("nan"),
            "avg_loss": float("nan"),
            "payoff": float("nan"),
            "profit_factor": float("nan"),
            "hold_days": float("nan"),
            "total_pnl": 0.0,
        }
    net = round_trip_frame["net_ret"].astype(float)
    gross = round_trip_frame["gross_ret"].astype(float)
    pnl = round_trip_frame["net_pnl"].astype(float)
    wins = net[net > 0]
    losses = net[net <= 0]
    avg_win = float(wins.mean()) if len(wins) else float("nan")
    avg_loss = float(losses.mean()) if len(losses) else float("nan")
    payoff = (
        avg_win / abs(avg_loss)
        if np.isfinite(avg_win) and np.isfinite(avg_loss) and avg_loss != 0
        else float("nan")
    )
    gross_profit = float(pnl[pnl > 0].sum())
    gross_loss = float(pnl[pnl < 0].sum())
    profit_factor = (
        gross_profit / abs(gross_loss)
        if gross_loss != 0
        else (float("inf") if gross_profit > 0 else float("nan"))
    )
    return {
        "n": float(len(net)),
        "gross_avg": float(gross.mean()),
        "gross_win": float((gross > 0).mean()),
        "net_avg": float(net.mean()),
        "net_win": float((net > 0).mean()),
        "median": float(net.median()),
        "p10": float(net.quantile(0.10)),
        "p90": float(net.quantile(0.90)),
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "payoff": float(payoff),
        "profit_factor": float(profit_factor),
        "hold_days": float(round_trip_frame["hold_days"].mean()),
        "total_pnl": float(pnl.sum()),
    }


def drawdown_stats(equity: pd.Series) -> dict[str, Any]:
    """最大回撤、峰值/谷值/修复日期与水下时间占比。"""
    if equity.empty:
        return {
            "max_drawdown": float("nan"),
            "peak_date": None,
            "trough_date": None,
            "recovery_date": None,
            "peak_to_trough_days": None,
            "trough_to_recovery_days": None,
            "under_water": float("nan"),
        }
    series = equity.astype(float).sort_index()
    peak = series.cummax()
    drawdown = series / peak - 1.0
    trough_date = drawdown.idxmin()
    peak_date = series.loc[:trough_date].idxmax()
    peak_value = float(series.loc[peak_date])
    after_trough = series.loc[trough_date:]
    recovered = after_trough[after_trough >= peak_value]
    recovery_date = recovered.index[0] if len(recovered) else None
    return {
        "max_drawdown": float(drawdown.min()),
        "peak_date": peak_date,
        "trough_date": trough_date,
        "recovery_date": recovery_date,
        "peak_to_trough_days": int((trough_date - peak_date).days),
        "trough_to_recovery_days": (
            int((recovery_date - trough_date).days)
            if recovery_date is not None
            else None
        ),
        "under_water": float((series < peak).mean()),
    }


def newey_west_mean(series: pd.Series, lag: int) -> dict[str, float]:
    """重叠样本下的均值检验：Newey-West 标准误。"""
    values = pd.Series(series).dropna().astype(float).to_numpy()
    n = len(values)
    if n == 0:
        return {"mean": float("nan"), "se": float("nan"), "t": float("nan"), "n": 0.0}
    mean = float(values.mean())
    errors = values - mean
    gamma0 = float(errors @ errors) / n
    long_run = gamma0
    for offset in range(1, min(lag, n - 1) + 1):
        weight = 1.0 - offset / (lag + 1.0)
        gamma = float(errors[offset:] @ errors[:-offset]) / n
        long_run += 2.0 * weight * gamma
    variance = max(long_run, 0.0) / n
    se = float(np.sqrt(variance))
    t_stat = mean / se if se > 0 else float("nan")
    return {"mean": mean, "se": se, "t": float(t_stat), "n": float(n)}


def symbol_attribution(round_trip_frame: pd.DataFrame) -> pd.DataFrame:
    """按标的分组的净盈亏贡献。"""
    if round_trip_frame.empty:
        return pd.DataFrame(columns=["trades", "pnl", "avg_ret", "win_rate"])
    grouped = round_trip_frame.groupby("symbol").agg(
        trades=("net_pnl", "size"),
        pnl=("net_pnl", "sum"),
        avg_ret=("net_ret", "mean"),
    )
    grouped["win_rate"] = round_trip_frame.groupby("symbol")["net_ret"].apply(
        lambda values: float((values > 0).mean())
    )
    return grouped.sort_values("pnl", ascending=False)


def load_index_closes(start: str, end: str, data_dir: Path) -> pd.DataFrame:
    """从 QuantDB 指数日线读取三个基准指数，返回日期 × 指数代码的收盘价。"""
    import pyarrow.dataset as ds

    path = data_dir / "quantdb" / "1_kline_data" / "index_daily"
    if not path.is_dir():
        raise FileNotFoundError(f"指数日线目录不存在：{path}")
    start_key = int(start.replace("-", ""))
    end_key = int(end.replace("-", ""))
    dataset = ds.dataset(str(path), format="parquet", partitioning="hive")
    table = dataset.to_table(
        columns=["symbol", "time", "close"],
        filter=(ds.field("dt") >= start_key)
        & (ds.field("dt") <= end_key)
        & ds.field("symbol").isin(list(INDEX_ORDER)),
    )
    frame = table.to_pandas()
    frame["trade_date"] = pd.to_datetime(frame["time"]).dt.normalize()
    pivot = frame.pivot(index="trade_date", columns="symbol", values="close")
    return pivot.sort_index()


def _prepend_initial(series: pd.Series, first_date: pd.Timestamp) -> pd.Series:
    if first_date in series.index:
        return series.sort_index()
    anchor = pd.Series({first_date: INITIAL_CAPITAL}, dtype=float)
    return pd.concat([anchor, series.sort_index()]).sort_index()


def _model_meta(artifact_dir: Path, run_dir: str) -> dict[str, Any]:
    run_id = Path(str(run_dir)).name
    path = artifact_dir.parent / "training_jobs" / run_id / "metadata.json"
    if not path.is_file():
        return {}
    return _read_json(path)


def _build_model(
    key: str,
    artifact_dir: Path,
    benchmark_ew: pd.Series,
    index_closes: pd.DataFrame,
) -> ModelReport:
    metrics = _read_json(artifact_dir / f"metrics_{key}.json")
    horizon = int(metrics["horizon"])
    quality_daily = pd.read_csv(
        artifact_dir / f"quality_daily_{key}.csv", parse_dates=["date"]
    )
    monthly_quality = pd.read_csv(
        artifact_dir / f"quality_monthly_{key}.csv", parse_dates=["date"]
    )
    equity_top20 = _read_equity(artifact_dir / f"top20_{key}_equity.csv")
    equity_rolling = _read_equity(artifact_dir / f"rolling_{key}_equity.csv")
    trades_top20 = _read_trades(artifact_dir / f"top20_{key}_trades.csv")
    trades_rolling = _read_trades(artifact_dir / f"rolling_{key}_trades.csv")
    round_trips_top20 = round_trips(trades_top20)
    round_trips_rolling = round_trips(trades_rolling)
    return ModelReport(
        key=key,
        horizon=horizon,
        metrics=metrics,
        meta=_model_meta(artifact_dir, metrics["run_dir"]),
        quality_daily=quality_daily,
        monthly_quality=monthly_quality,
        equity_top20=equity_top20,
        equity_rolling=equity_rolling,
        trades_top20=trades_top20,
        trades_rolling=trades_rolling,
        round_trips_top20=round_trips_top20,
        round_trips_rolling=round_trips_rolling,
        stats_top20=round_trip_stats(round_trips_top20),
        stats_rolling=round_trip_stats(round_trips_rolling),
        drawdown_top20=drawdown_stats(equity_top20),
        drawdown_rolling=drawdown_stats(equity_rolling),
        nw_ic=newey_west_mean(quality_daily["ic_eligible"], lag=horizon - 1),
        attribution_top20=symbol_attribution(round_trips_top20),
        attribution_rolling=symbol_attribution(round_trips_rolling),
        benchmark_ew=benchmark_ew,
        index_closes=index_closes,
    )


def load_bundle(artifact_dir: Path) -> ReportBundle:
    """读取全部回测产物；缺文件时直接报错，不做静默降级。"""
    artifact_dir = artifact_dir.resolve()
    benchmark_path = artifact_dir / "benchmark_ew_T5.csv"
    benchmark_frame = pd.read_csv(benchmark_path, parse_dates=["date"])
    benchmark_frame = benchmark_frame.set_index("date").sort_index()
    benchmark_ew = benchmark_frame["equity"].astype(float)
    first_equity = _read_equity(artifact_dir / "top20_T5_equity.csv")
    first_date = first_equity.index[0]
    benchmark_ew = _prepend_initial(benchmark_ew, first_date)
    last_date = max(
        _read_equity(artifact_dir / f"top20_{key}_equity.csv").index[-1]
        for key in MODEL_KEYS
    )
    index_closes = load_index_closes(
        first_date.strftime("%Y-%m-%d"),
        last_date.strftime("%Y-%m-%d"),
        artifact_dir.parent,
    )
    models = {
        key: _build_model(key, artifact_dir, benchmark_ew, index_closes)
        for key in MODEL_KEYS
    }
    return ReportBundle(
        artifact_dir=artifact_dir,
        generated_at=datetime.now(),
        models=models,
    )
