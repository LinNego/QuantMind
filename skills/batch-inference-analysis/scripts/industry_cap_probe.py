"""行业集中度上限探针：只改“组合约束”，不改买卖信号。

动机：Top50 全按综合分排序买入，若某天高分票集中在同一行业（例如银行/电子），
组合会在不知不觉中变成“单行业押注”。行业上限是最经典的风险预算手段，
且不依赖任何未来信息——它只约束买入数量。

预注册（先写规则，再看结果）：

基线 B = Top50 / T+10 / 13 模型 / S>=20 日均值入场 / S<均值-0.03 退出 / 次日收盘成交。

单变量变体：max_industry_pct = 20% / 30% / 40%（对应单行业最多 10 / 15 / 20 只）。
行业映射取 QuantDB `instrument_detail.parquet` 的 `rs_hycode_sim`，两套口径都测：
- fine：128 个细分行业（完整代码）；
- l1：代码前两位聚合出的 30 个一级行业。
两套口径事先固定，不按结果挑选。映射缺失（-1）不受限。

验收门槛（每档独立判定，全部满足才算“更好”）：
1. 全期年化不低于基线 2.0pp 以上（允许为控风险让渡部分收益）；
2. 2025 与 2026 两个年度都不低于基线 1.0pp 以上；
3. 2026-09-01~10-19 前向窗口总收益不低于基线 1.0pp 以上；
4. 风险确实下降：全期最大回撤改善 >= 1.5pp，或 Sharpe 提升 >= 0.03；
5. 族方向一致：三档里至少两档的全期回撤相对基线有改善；
6. 推荐档位取“通过门槛的最松档”（最小干预原则），不做事后挑最优。

已知局限：行业分类是当前快照，用作风险约束而非 alpha；历史行业变更极少，
但严格 point-in-time 分类本数据源不提供。

用法：
    python industry_cap_probe.py \
      --model-map /tmp/model_map_31.json \
      --out-dir /tmp/industry_cap_probe \
      --memory 4GB --threads 3
"""

from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

import rolling_portfolio_sim as R
import top20_model_eval as E


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


INDUSTRY_PATHS = (
    "/data/quantdb/2_base_sector/instrument_detail/instrument_detail.parquet",
    "/home/zbox/projects/quantmind/data/quantdb/2_base_sector/"
    "instrument_detail/instrument_detail.parquet",
)


def load_industry(close: pd.DataFrame, level: str = "fine") -> np.ndarray:
    """按 close 的列顺序返回行业编码；缺失为 -1。

    level=fine 用 128 个细分行业；level=l1 用代码前两位聚合出的 30 个一级行业。
    两个口径都来自同一份 QuantDB 分类，不手工改分组。
    """
    path = next((Path(p) for p in INDUSTRY_PATHS if Path(p).exists()), None)
    if path is None:
        raise FileNotFoundError("找不到 instrument_detail.parquet")
    detail = pd.read_parquet(path, columns=["Symbol", "rs_hycode_sim", "rs_hyname"])
    detail = (
        detail.dropna(subset=["Symbol", "rs_hycode_sim"])
        .drop_duplicates("Symbol")
        .assign(Symbol=lambda d: d["Symbol"].astype(str))
    )
    if level == "l1":
        keys = detail["rs_hycode_sim"].astype(str).str[1:3]
    elif level == "fine":
        keys = detail["rs_hycode_sim"].astype(str)
    else:
        raise ValueError(f"未知行业口径 {level}")
    mapping = dict(zip(detail["Symbol"], keys, strict=True))
    names = [mapping.get(str(s)) for s in close.columns]
    codes = pd.Categorical(names).codes.astype(np.int64)
    coverage = float((codes >= 0).mean())
    log(
        f"行业映射[{level}]覆盖 {coverage:.1%}"
        f"（{int((codes >= 0).sum())}/{len(codes)}，"
        f"{len({n for n in names if n is not None})} 组）"
    )
    if coverage < 0.9:
        log("警告：行业映射覆盖低于 90%，上限可能只对部分票生效")
    return codes


def configs(top_n: int = 50, hold: int = 10) -> list[R.Config]:
    base = R.Config(label="base", top_n=top_n, hold=hold)
    return [
        base,
        replace(base, label="ind20", max_industry_pct=0.20),
        replace(base, label="ind30", max_industry_pct=0.30),
        replace(base, label="ind40", max_industry_pct=0.40),
    ]


def concentration_stats(
    trades: pd.DataFrame,
    industry: np.ndarray,
    symbols: np.ndarray,
    top_n: int,
    max_pct: float | None,
) -> dict:
    """统计每个交易日的单行业最大持仓只数，以及触发上限的天数。"""
    if trades.empty:
        return {}
    sym_to_j = {str(s): j for j, s in enumerate(symbols)}
    days = np.arange(int(trades["entry_i"].min()), int(trades["exit_i"].max()) + 1)
    counts = np.zeros(len(days), dtype=np.int64)
    over = 0
    cap = max(1, math.ceil(top_n * max_pct)) if max_pct is not None else None
    for d in days:
        active = trades[(trades["entry_i"] <= d) & (trades["exit_i"] > d)]
        inds = [
            int(industry[sym_to_j[s]])
            for s in active["symbol"]
            if s in sym_to_j and int(industry[sym_to_j[s]]) >= 0
        ]
        if not inds:
            continue
        _, c = np.unique(inds, return_counts=True)
        top = int(c.max())
        counts[d - days[0]] = top
        if cap is not None and top > cap:
            over += 1
    return {
        "days": int(len(days)),
        "top_ind_mean": float(counts.mean()),
        "top_ind_max": int(counts.max()),
        "days_over_cap": int(over),
    }


def run_period(
    label: str,
    cfgs: list[R.Config],
    close: pd.DataFrame,
    adv: pd.DataFrame,
    score: pd.DataFrame,
    avg: pd.DataFrame,
    blocked_long: np.ndarray,
    blocked_short: np.ndarray,
    start_i: int,
    end_i: int,
    market20: pd.Series,
    industry: np.ndarray,
    out_dir: Path,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[R.SimResult] = []
    symbols = close.columns.to_numpy()
    diag_rows = []
    for cfg in cfgs:
        t0 = time.time()
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
            industry=industry,
        )
        results.append(res)
        s = res.stats
        log(
            f"{label:<8} {cfg.label:<8} ann={s['ann_ret']:>7.1%} "
            f"tot={s['total_ret']:>7.1%} dd={s['maxdd']:>7.1%} "
            f"sr={s['sharpe']:>5.2f} turn={s['turnover_ann']:>5.1f}x "
            f"({time.time() - t0:.1f}s)"
        )
        if not res.trades.empty:
            diag = concentration_stats(
                res.trades, industry, symbols, cfg.top_n, cfg.max_industry_pct
            )
            diag_rows.append({"period": label, "label": cfg.label, **diag})
    summary = pd.DataFrame([r.stats for r in results])
    summary.insert(0, "period", label)
    summary.to_csv(out_dir / "summary.csv", index=False)
    yearly = pd.concat(
        [r.yearly.assign(label=r.config.label) for r in results if not r.yearly.empty],
        ignore_index=True,
    )
    yearly.to_csv(out_dir / "yearly.csv", index=False)
    for r in results:
        name = R.slug(r.config.label)
        r.nav.rename("nav").to_csv(out_dir / f"nav_{name}.csv")
        if not r.trades.empty:
            r.trades.to_csv(out_dir / f"trades_{name}.csv", index=False)
    return summary, pd.DataFrame(diag_rows)


def decision_table(
    summary: pd.DataFrame, yearly: pd.DataFrame, diag: pd.DataFrame
) -> pd.DataFrame:
    full = summary[summary["period"] == "full"].set_index("label")
    sep = summary[summary["period"] == "sep_oct"].set_index("label")
    y25 = yearly[yearly["year"] == 2025].set_index("label")
    y26 = yearly[yearly["year"] == 2026].set_index("label")
    base = full.loc["base"]
    cap_labels = [c for c in full.index if c.startswith("ind")]
    dd_improved = {
        c: float(full.loc[c, "maxdd"] - base["maxdd"]) >= 0.015 for c in cap_labels
    }
    family_ok = sum(dd_improved.values()) >= 2
    rows = []
    for label in cap_labels:
        row = full.loc[label]
        d_full = float(row["ann_ret"] - base["ann_ret"])
        d_25 = float(y25.loc[label, "ann_ret"] - y25.loc["base", "ann_ret"])
        d_26 = float(y26.loc[label, "ann_ret"] - y26.loc["base", "ann_ret"])
        d_sep = float(sep.loc[label, "total_ret"] - sep.loc["base", "total_ret"])
        d_sharpe = float(row["sharpe"] - base["sharpe"])
        d_dd = float(row["maxdd"] - base["maxdd"])
        risk_ok = bool(d_dd >= 0.015 or d_sharpe >= 0.03)
        passed = bool(
            d_full >= -0.02
            and d_25 >= -0.01
            and d_26 >= -0.01
            and d_sep >= -0.01
            and risk_ok
            and family_ok
        )
        rows.append(
            {
                "label": label,
                "d_full_ann": d_full,
                "d_2025_ann": d_25,
                "d_2026_ann": d_26,
                "d_sep_oct": d_sep,
                "d_sharpe": d_sharpe,
                "d_maxdd": d_dd,
                "risk_ok": risk_ok,
                "family_ok": family_ok,
                "pass": passed,
            }
        )
    out = pd.DataFrame(rows)
    if not diag.empty:
        out = out.merge(
            diag[diag["period"] == "full"][
                ["label", "top_ind_mean", "top_ind_max", "days_over_cap"]
            ],
            on="label",
            how="left",
        )
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-map", default="/tmp/model_map_31.json")
    parser.add_argument("--out-dir", default="/tmp/industry_cap_probe")
    parser.add_argument("--memory", default="4GB")
    parser.add_argument("--threads", type=int, default=3)
    parser.add_argument("--top-n", type=int, default=50)
    parser.add_argument("--hold", type=int, default=10)
    parser.add_argument(
        "--industry-level",
        choices=("fine", "l1"),
        default="fine",
        help="fine=128 个细分行业；l1=代码前两位的 30 个一级行业",
    )
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    models_all = json.loads(Path(args.model_map).read_text(encoding="utf-8"))
    models = [m for m in models_all if int(m["horizon"]) == args.hold]
    if not models:
        raise RuntimeError(f"model map 里没有 T+{args.hold} 模型")
    log(f"T+{args.hold} 模型数 {len(models)}")

    lo, hi = 20250101, 20261029
    con = E.connect(args.memory, args.threads)
    close, adv, blocked_long, blocked_short = R.load_panels(con, lo, hi)
    R.register_adv(con, adv)
    log(f"panel close={close.shape} adv={adv.shape}")
    industry = load_industry(close, args.industry_level)

    score, _, _ = R.build_composite(
        con,
        models,
        "2025-02-01",
        "2026-10-19",
        diag_horizon=None,
    )
    score = score.reindex(index=close.index, columns=close.columns)
    avg = R.score_average(score)
    market = R.market_daily(close, adv)
    market20 = (1.0 + market).rolling(20, min_periods=15).apply(np.prod, raw=True) - 1.0

    dates = close.index.to_numpy(dtype="int64")
    avg_rows = avg.notna().sum(axis=1)
    first_avg_i = int(np.argmax(avg_rows.to_numpy() >= 100))
    full_start_i = first_avg_i + 1
    full_end_i = int(np.searchsorted(dates, 20260826))
    sep_start_i = int(np.searchsorted(dates, 20260901))
    sep_end_i = int(np.searchsorted(dates, 20261019))
    log(
        f"full {dates[full_start_i]}..{dates[full_end_i]} "
        f"sep_oct {dates[sep_start_i]}..{dates[sep_end_i]}"
    )

    cfgs = [replace(c, capital=R.CAPITAL) for c in configs(args.top_n, args.hold)]
    common = {
        "cfgs": cfgs,
        "close": close,
        "adv": adv,
        "score": score,
        "avg": avg,
        "blocked_long": blocked_long,
        "blocked_short": blocked_short,
        "market20": market20,
        "industry": industry,
    }
    full, diag_full = run_period(
        "full",
        start_i=full_start_i,
        end_i=full_end_i,
        out_dir=out_dir / "full",
        **common,
    )
    sep, diag_sep = run_period(
        "sep_oct",
        start_i=sep_start_i,
        end_i=sep_end_i,
        out_dir=out_dir / "sep_oct",
        **common,
    )
    diag = pd.concat([diag_full, diag_sep], ignore_index=True)
    diag.to_csv(out_dir / "concentration.csv", index=False)
    summary = pd.concat([full, sep], ignore_index=True)
    summary.to_csv(out_dir / "summary_all.csv", index=False)
    yearly = pd.read_csv(out_dir / "full" / "yearly.csv")
    decision = decision_table(summary, yearly, diag)
    decision.to_csv(out_dir / "decision.csv", index=False)
    print("\n行业上限判定：")
    print(decision.to_string(index=False))
    print("\n集中度诊断：")
    print(diag.to_string(index=False))


if __name__ == "__main__":
    main()
