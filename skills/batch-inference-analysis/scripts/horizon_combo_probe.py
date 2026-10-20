"""周期共识探针：T+5 / T+10 / 双周期等权，同一套买卖规则。

动机：T+5 与 T+10 的模型看的是不同持有期，若两者的截面排名一致，
说明“这只票被不同周期的模型同时认可”，比单周期排名更不容易被单一模型的
短期噪声带偏。这里不调任何买卖参数，只换综合分的模型集合：

- t10：13 个 T+10 模型，min_models=8（现役基线）
- t5：13 个 T+5 模型，min_models=8（周期对照）
- combo26：T+5 + T+10 共 26 个模型，min_models=16（保持约 60% 覆盖门槛）

验收：combo26 要在全期、2025、2026、2026-09~10 四个窗口都不低于 t10，
才认为“多周期共识”是稳健改进；只赢一个窗口就只记录、不推荐。

用法：
    python horizon_combo_probe.py \
      --model-map /tmp/model_map_31.json \
      --out-dir /tmp/horizon_combo_probe \
      --memory 4GB --threads 3
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

import rolling_portfolio_sim as R
import top20_model_eval as E


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def load_window(con, start: str, end: str):
    lo = int((pd.Timestamp(start) - pd.Timedelta(days=45)).strftime("%Y%m%d"))
    hi = int((pd.Timestamp(end) + pd.Timedelta(days=10)).strftime("%Y%m%d"))
    close, adv, blocked_long, blocked_short = R.load_panels(con, lo, hi)
    R.register_adv(con, adv)
    return close, adv, blocked_long, blocked_short, lo, hi


def build_score(
    con,
    models: list[dict],
    min_models: int,
    start: str,
    end: str,
    close: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    score, _, _ = R.build_composite(
        con,
        models,
        start,
        end,
        min_models=min_models,
        diag_horizon=None,
    )
    score = score.reindex(index=close.index, columns=close.columns)
    return score, R.score_average(score)


def run(
    label: str,
    score: pd.DataFrame,
    avg: pd.DataFrame,
    close: pd.DataFrame,
    adv: pd.DataFrame,
    blocked_long: np.ndarray,
    blocked_short: np.ndarray,
    start_i: int,
    end_i: int,
    market20: pd.Series,
) -> R.SimResult:
    cfg = R.Config(label=label, top_n=50, hold=10)
    res = R.simulate(
        cfg,
        close,
        adv,
        score,
        avg,
        blocked_long,
        blocked_short,
        start_i,
        end_i,
        market20,
    )
    s = res.stats
    log(
        f"{label:<12} ann={s['ann_ret']:>7.1%} tot={s['total_ret']:>7.1%} "
        f"dd={s['maxdd']:>7.1%} sr={s['sharpe']:>5.2f} "
        f"turn={s['turnover_ann']:>5.1f}x win={s['trade_win']:>5.1%}"
    )
    return res


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-map", default="/tmp/model_map_31.json")
    parser.add_argument("--out-dir", default="/tmp/horizon_combo_probe")
    parser.add_argument("--memory", default="4GB")
    parser.add_argument("--threads", type=int, default=3)
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    models_all = json.loads(Path(args.model_map).read_text(encoding="utf-8"))
    m5 = [m for m in models_all if int(m["horizon"]) == 5]
    m10 = [m for m in models_all if int(m["horizon"]) == 10]
    if not m5 or not m10:
        raise RuntimeError("model map 必须同时包含 T+5 与 T+10")
    log(f"T+5={len(m5)} T+10={len(m10)}")

    start, end = "2025-02-01", "2026-10-19"
    con = E.connect(args.memory, args.threads)
    close, adv, blocked_long, blocked_short, _, _ = load_window(con, start, end)
    log(f"panel close={close.shape} adv={adv.shape}")

    scores: dict[str, pd.DataFrame] = {}
    avgs: dict[str, pd.DataFrame] = {}
    for label, models, min_models in (
        ("t10", m10, 8),
        ("t5", m5, 8),
        ("combo26", m5 + m10, 16),
    ):
        score, avg = build_score(con, models, min_models, start, end, close)
        scores[label] = score
        avgs[label] = avg
        log(
            f"{label} coverage/day="
            f"{float(score.notna().sum(axis=1).loc[20250219:].mean()):.0f}"
        )

    market = R.market_daily(close, adv)
    market20 = (1.0 + market).rolling(20, min_periods=15).apply(np.prod, raw=True) - 1.0
    dates = close.index.to_numpy(dtype="int64")
    avg_rows = avgs["t10"].notna().sum(axis=1)
    first_avg_i = int(np.argmax(avg_rows.to_numpy() >= 100))
    full_start_i = first_avg_i + 1
    full_end_i = int(np.searchsorted(dates, 20260826))
    sep_start_i = int(np.searchsorted(dates, 20260901))
    sep_end_i = int(np.searchsorted(dates, 20261019))

    rows = []
    for period, start_i, end_i in (
        ("full", full_start_i, full_end_i),
        ("sep_oct", sep_start_i, sep_end_i),
    ):
        for label in ("t10", "t5", "combo26"):
            res = run(
                f"{period}_{label}",
                scores[label],
                avgs[label],
                close,
                adv,
                blocked_long,
                blocked_short,
                start_i,
                end_i,
                market20,
            )
            rows.append({"period": period, **res.stats})
            res.nav.rename("nav").to_csv(out_dir / f"nav_{period}_{label}.csv")
            res.trades.to_csv(out_dir / f"trades_{period}_{label}.csv", index=False)
    summary = pd.DataFrame(rows)
    summary.to_csv(out_dir / "summary.csv", index=False)

    yearly = []
    for label in ("t10", "t5", "combo26"):
        res = R.simulate(
            R.Config(label=label, top_n=50, hold=10),
            close,
            adv,
            scores[label],
            avgs[label],
            blocked_long,
            blocked_short,
            full_start_i,
            full_end_i,
            market20,
        )
        yearly.append(res.yearly.assign(score_set=label))
    pd.concat(yearly, ignore_index=True).to_csv(out_dir / "yearly.csv", index=False)
    print("\n周期共识对照：")
    print(
        summary[
            [
                "period",
                "label",
                "ann_ret",
                "total_ret",
                "maxdd",
                "sharpe",
                "turnover_ann",
                "trade_win",
            ]
        ].to_string(index=False)
    )


if __name__ == "__main__":
    main()
