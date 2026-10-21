"""P1 组合选股研究运行器：16 个候选 × 三段窗口，产出可复核证据包。

运行（容器内，宿主机缺 loguru/exchange_calendars）：

    docker exec quantmind sh -lc 'cd /app && python -m backend.scripts.backtest2026.combo_next_day'

输出（默认 ``data/reports/combo_nextday_20261021/``）：

- ``next_day_hit.csv``      逐候选 × 逐日命中明细（Top50 / Top20）
- ``bootstrap_ci.csv``      测试窗逐日配对差的按月分块 bootstrap
- ``monthly_hit.csv``       测试窗逐月命中与相对 C0 的差
- ``ten_day_gate.csv``      10 日滚动调仓闸门（年化/回撤/交易统计）
- ``model_individual.csv``  13 模型 × 两个周期的单模型基准
- ``evidence.json``         全部结论、判据与 kill criteria 的机读证据
- ``REPORT.md``             人读报告

研究纪律（与细案一致）：
- 检验窗只看一次；C7/C12/C16 只在选择窗拟合。
- 组合分只用成员当日截面分位；缺失成员重新归一，不按 0 分。
- 未通过 K-R1/K-R2 的候选不进入推荐，K-R4 缺省即「无推荐」。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from backend.scripts.backtest2026.combo_eval import (
    RebalanceConfig,
    benjamini_hochberg,
    block_bootstrap_ci,
    combine_ranks,
    daily_hits,
    fit_meta_logit,
    meta_features,
    monthly_mean,
    paired_diff,
    predict_meta,
    rank_frame,
    simulate_rebalance,
    topk_hit_day,
)
from backend.scripts.backtest2026.config import DATA_VOLUME, PRICE_END
from backend.scripts.backtest2026.data import (
    attach_listing_age,
    build_market_panel,
    load_stored_predictions,
    read_first_seen,
)
from backend.scripts.backtest2026.model_registry import (
    MODEL_ORDER,
    resolve_registry,
)

logger = logging.getLogger(__name__)

DEFAULT_CONFIG = Path(__file__).with_name("combo_candidates_20261021.json")
DEFAULT_OUT = DATA_VOLUME / "reports" / "combo_nextday_20261021"
WINDOW_NAMES = ("selection", "validation", "test")


# ── 配置与数据装载 ───────────────────────────────────────────────────────


def load_config(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        config = json.load(handle)
    if "candidates" not in config or "splits" not in config:
        raise ValueError(f"组合候选配置缺字段：{path}")
    return config


def expand_members(config: dict, candidate: dict) -> list[str]:
    raw = candidate["members"]
    if isinstance(raw, str):
        return list(config["members"][raw])
    members: list[str] = []
    for item in raw:
        if item in config["members"]:
            members.extend(config["members"][item])
        else:
            members.append(item)
    return list(dict.fromkeys(members))


def config_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _window(config: dict, name: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    start, end = config["splits"][name]
    return pd.Timestamp(start), pd.Timestamp(end)


def build_panel(config: dict):
    """价格/成交额面板：从选择窗前 1 个月到固定 PRICE_END，保证 t+2 可成交。"""
    selection_start = _window(config, "selection")[0]
    start = (selection_start - pd.Timedelta(days=30)).strftime("%Y-%m-%d")
    panel = build_market_panel(start=start, end=PRICE_END, with_amount=True)
    first_seen = read_first_seen(_run_dir(config, 10, "LGB"))
    return attach_listing_age(panel, first_seen)


def _run_dir(config: dict, horizon: int, key: str) -> Path:
    from backend.scripts.backtest2026.model_registry import resolve_model_run

    run = resolve_model_run(horizon, key)
    if run.run_dir is None or not run.ready:
        raise RuntimeError(f"模型未就绪：{key} T+{horizon}（{run.reason}）")
    return run.run_dir


def build_rank_panels(
    config: dict,
    registry: dict[int, dict],
) -> dict[tuple[int, str], pd.DataFrame]:
    """每个 (周期, 模型) 一张「日期 × 标的」的截面分位表（float32）。"""
    start = _window(config, "selection")[0]
    end = _window(config, "test")[1]
    ranks: dict[tuple[int, str], pd.DataFrame] = {}
    for horizon, models in registry.items():
        for key in MODEL_ORDER:
            run = models[key]
            frame = load_stored_predictions(
                run.run_dir,
                start.strftime("%Y-%m-%d"),
                end.strftime("%Y-%m-%d"),
            )
            if frame.empty:
                raise RuntimeError(f"{key} T+{horizon} 在选择-检验窗内没有预测")
            wide = frame.pivot(index="trade_date", columns="symbol", values="pred")
            wide = wide.sort_index()
            ranks[(horizon, key)] = rank_frame(wide).astype("float32")
            logger.info(
                "分位表就绪 %s T+%s：%s 日 × %s 标的",
                key,
                horizon,
                len(wide),
                wide.shape[1],
            )
    return ranks


def model_signature(registry: dict[int, dict]) -> dict:
    """训练产物的因子契约签名；同组必须完全一致才能谈「同特征」。"""
    rows: dict[str, dict] = {}
    hashes: dict[str, set[str]] = {}
    for horizon, models in registry.items():
        for key in MODEL_ORDER:
            run = models[key]
            metadata_path = run.run_dir / "metadata.json"
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
            signature = {
                "factor_schema_hash": payload.get("factor_schema_hash"),
                "feature_count": payload.get("feature_count")
                or payload.get("n_features"),
                "data_source": payload.get("data_source"),
                "factor_source": payload.get("factor_source"),
                "target_horizon_days": payload.get("target_horizon_days"),
            }
            rows[f"T{horizon}:{key}"] = {
                "run_id": run.run_dir.name,
                **signature,
            }
            hashes.setdefault(f"T{horizon}", set()).add(
                json.dumps(signature, sort_keys=True)
            )
    return {
        "models": rows,
        "consistent": {group: len(values) == 1 for group, values in hashes.items()},
    }


# ── 单模型基准与选择窗拟合 ───────────────────────────────────────────────


def model_individual_table(
    config: dict,
    ranks: dict[tuple[int, str], pd.DataFrame],
    panel,
    top_k: int,
) -> tuple[pd.DataFrame, dict[tuple[int, str], dict[str, pd.DataFrame]]]:
    """单模型在三段窗口的命中，同时缓存逐日明细供 C7 选择。"""
    rows = []
    cache: dict[tuple[int, str], dict[str, pd.DataFrame]] = {}
    for (horizon, key), rank_panel in ranks.items():
        by_date = {day: rank_panel.loc[day] for day in rank_panel.index}
        cache[(horizon, key)] = {}
        row: dict[str, object] = {"horizon": horizon, "model": key}
        for name in WINDOW_NAMES:
            start, end = _window(config, name)
            hits = daily_hits(
                by_date,
                panel,
                top_k,
                start=start.strftime("%Y-%m-%d"),
                end=end.strftime("%Y-%m-%d"),
                min_listing_days=config["evaluation"]["min_listing_days"],
                cost_bps=config["evaluation"]["cost_bps"],
            )
            cache[(horizon, key)][name] = hits
            row[f"{name}_hit_exec"] = (
                float(hits["hit_exec"].mean()) if not hits.empty else float("nan")
            )
            row[f"{name}_hit_theory"] = (
                float(hits["hit_theory"].mean()) if not hits.empty else float("nan")
            )
            row[f"{name}_days"] = int(len(hits))
        rows.append(row)
    return pd.DataFrame(rows), cache


def ic_weights(
    config: dict,
    ranks: dict[tuple[int, str], pd.DataFrame],
    panel,
    members: list[str],
    horizon: int,
) -> dict[str, float]:
    """选择窗内逐日 rank IC 均值，只取正 IC 成员做权重。"""
    start, end = _window(config, "selection")
    next_ret = panel.close_qfq.shift(-1) / panel.close_qfq - 1.0
    min_days = config["evaluation"]["min_listing_days"]
    ics: dict[str, float] = {}
    for key in members:
        rank_panel = ranks.get((horizon, key))
        if rank_panel is None:
            continue
        daily: list[float] = []
        for day in rank_panel.index:
            if day < start or day > end:
                continue
            scores = rank_panel.loc[day].dropna()
            returns = next_ret.loc[day].reindex(scores.index)
            known = ~panel.is_st.reindex(scores.index).fillna(False).astype(bool)
            known &= (
                panel.age_days.loc[day].reindex(scores.index).fillna(-1) >= min_days
            )
            known &= ~panel.suspended.loc[day].reindex(scores.index).fillna(
                True
            ).astype(bool)
            scores, returns = scores[known], returns[known]
            pair = pd.concat([scores, returns], axis=1).dropna()
            if len(pair) < 100:
                continue
            ic = pair.iloc[:, 0].rank().corr(pair.iloc[:, 1].rank())
            if np.isfinite(ic):
                daily.append(float(ic))
        ics[key] = float(np.mean(daily)) if daily else float("nan")
    positive = {
        key: max(value, 0.0) for key, value in ics.items() if np.isfinite(value)
    }
    total = sum(positive.values())
    if total <= 0:
        positive = {key: 1.0 for key in members if (horizon, key) in ranks}
        total = float(len(positive)) or 1.0
    return {key: value / total for key, value in positive.items()}, ics


def _meta_label_frames(
    panel, min_listing_days: int
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """次日真实收益与「t+1 收盘可执行」掩码。"""
    next_ret = panel.close_qfq.shift(-1) / panel.close_qfq - 1.0
    eligible = pd.DataFrame(True, index=panel.dates, columns=panel.symbols, dtype=bool)
    eligible &= ~panel.is_st.reindex(panel.symbols).fillna(False).astype(bool)
    eligible &= panel.age_days >= min_listing_days
    eligible &= ~panel.suspended
    eligible &= ~panel.suspended.shift(-1).fillna(True)
    eligible &= ~panel.limit_up.shift(-1).fillna(True)
    eligible &= np.isfinite(next_ret)
    return next_ret, eligible


def build_meta_frames(
    ranks: dict[tuple[int, str], pd.DataFrame],
    panel,
    days: pd.DatetimeIndex,
    members: list[str],
    horizon: int,
    min_coverage: int,
    min_listing_days: int = 60,
) -> tuple[pd.DataFrame, pd.Series]:
    """把逐日截面分位堆成元模型训练/预测矩阵（索引 = 日期 × 标的）。"""
    next_ret, eligible = _meta_label_frames(panel, min_listing_days)
    feature_frames: list[pd.DataFrame] = []
    label_frames: list[pd.Series] = []
    for day in days:
        if day not in panel.dates:
            continue
        columns = {}
        for key in members:
            rank_panel = ranks.get((horizon, key))
            if rank_panel is not None and day in rank_panel.index:
                columns[key] = rank_panel.loc[day]
        if not columns:
            continue
        feats = meta_features(pd.DataFrame(columns), list(columns), min_coverage)
        if feats.empty:
            continue
        mask = eligible.loc[day]
        symbols = feats.index.intersection(mask[mask].index)
        if symbols.empty:
            continue
        feats = feats.loc[symbols]
        labels = (next_ret.loc[day].reindex(symbols) > 0).astype(int)
        index = pd.MultiIndex.from_product([[day], symbols])
        feats.index = index
        labels.index = index
        feature_frames.append(feats)
        label_frames.append(labels)
    if not feature_frames:
        return pd.DataFrame(), pd.Series(dtype="int64")
    return pd.concat(feature_frames), pd.concat(label_frames)


# ── 候选评估 ─────────────────────────────────────────────────────────────


def candidate_panels(
    candidate: dict,
    config: dict,
    ranks: dict[tuple[int, str], pd.DataFrame],
    horizon: int,
    members: list[str],
) -> dict[str, pd.DataFrame]:
    """候选成员的「显示名 → 分位面板」。

    同周期成员显示名就是模型 key；C9 这类跨周期混合用 ``T10:LGB`` 形式，
    避免同一个模型 key 在两张面板间相撞。
    """
    raw = candidate["members"]
    aliases = (
        [item for item in raw if isinstance(item, str) and item in config["members"]]
        if isinstance(raw, list)
        else []
    )
    horizons = {
        10 if alias.startswith("T10") else 5
        for alias in aliases
        if alias in {"T10", "T5"}
    }
    if len(horizons) > 1 and aliases:
        panels: dict[str, pd.DataFrame] = {}
        for alias in aliases:
            if alias not in {"T10", "T5"}:
                continue
            alias_horizon = 10 if alias == "T10" else 5
            for key in config["members"][alias]:
                frame = ranks.get((alias_horizon, key))
                if frame is not None:
                    panels[f"{alias}:{key}"] = frame
        return panels
    return {key: ranks[(horizon, key)] for key in members if (horizon, key) in ranks}


def _scores_from_panels(
    panels: dict[str, pd.DataFrame],
    *,
    method: str,
    weights: dict[str, float] | None = None,
    min_coverage: int = 1,
    trim: int = 2,
    consensus_gate: dict[str, float] | None = None,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> dict[pd.Timestamp, pd.Series]:
    """按日合成成员分位；只保留合成后有值的交易日。"""
    if not panels:
        return {}
    index = pd.DatetimeIndex([])
    for frame in panels.values():
        index = index.union(frame.loc[start:end].index)
    combined: dict[pd.Timestamp, pd.Series] = {}
    for day in index.sort_values():
        ranks_day = pd.DataFrame(
            {
                name: frame.loc[day]
                for name, frame in panels.items()
                if day in frame.index
            }
        )
        if ranks_day.empty:
            continue
        series = combine_ranks(
            ranks_day,
            list(ranks_day.columns),
            method=method,
            weights=weights,
            min_coverage=min_coverage,
            trim=trim,
            consensus_gate=consensus_gate,
        ).dropna()
        if not series.empty:
            combined[day] = series
    return combined


def _window_hits(
    scores: dict[pd.Timestamp, pd.Series],
    panel,
    config: dict,
    name: str,
    k: int,
) -> pd.DataFrame:
    start, end = _window(config, name)
    return daily_hits(
        scores,
        panel,
        k,
        start=start.strftime("%Y-%m-%d"),
        end=end.strftime("%Y-%m-%d"),
        min_listing_days=config["evaluation"]["min_listing_days"],
        cost_bps=config["evaluation"]["cost_bps"],
    )


def _summarize_hits(hits: pd.DataFrame, k: int) -> dict[str, float]:
    if hits.empty:
        return {"days": 0}
    return {
        "days": int(len(hits)),
        "hit_theory_mean": float(hits["hit_theory"].mean()),
        "hit_exec_mean": float(hits["hit_exec"].mean()),
        "ret_theory_mean": float(hits["ret_theory"].mean()),
        "ret_exec_net_mean": float(hits["ret_exec_net"].mean()),
        "n_scored_mean": float(hits["n_scored"].mean()),
        "n_entry_mean": float(hits["n_entry"].mean()),
        "full_top_ratio": float((hits["n_top"] >= k).mean()),
    }


def evaluate_candidate(
    candidate: dict,
    config: dict,
    ranks: dict[tuple[int, str], pd.DataFrame],
    panel,
    individual_hits: dict[tuple[int, str], dict[str, pd.DataFrame]],
    baseline_hits: dict[str, pd.DataFrame],
    *,
    skip_gate: bool = False,
) -> dict:
    cand_id = candidate["id"]
    horizon = int(candidate["horizon"])
    members = expand_members(config, candidate)
    weights = None
    ic_values = None
    notes: list[str] = []
    status = "ok"

    same_horizon = all((horizon, key) in ranks for key in members)
    panels = candidate_panels(candidate, config, ranks, horizon, members)

    if (
        same_horizon
        and candidate.get("selection_rule", {}).get("kind") == "top_n_by_selection_hit"
    ):
        rule = candidate["selection_rule"]
        scored = []
        for key in members:
            hits = individual_hits.get((horizon, key), {}).get("selection")
            if hits is None or hits.empty:
                continue
            metric = rule.get("metric", "hit_exec_top50").replace("_top50", "")
            scored.append((key, float(hits[metric].mean())))
        scored.sort(key=lambda item: item[1], reverse=True)
        members = [key for key, _ in scored[: int(rule.get("n", 4))]]
        panels = {key: panels[key] for key in members if key in panels}
        notes.append(f"C7 按选择窗单模型命中选出：{', '.join(members)}")

    if same_horizon and candidate.get("weight_rule", {}).get("kind") == "selection_ic":
        weights, ic_values = ic_weights(config, ranks, panel, members, horizon)
        notes.append("IC 权重只用选择窗拟合，负 IC 截断为 0 后归一")

    meta_model = None
    if candidate.get("method") == "meta_logit":
        try:
            train_days = _selection_days(config, ranks, horizon, members)
            features, labels = build_meta_frames(
                ranks,
                panel,
                train_days,
                members,
                horizon,
                int(candidate.get("min_coverage", 8)),
                int(config["evaluation"]["min_listing_days"]),
            )
            if features.empty:
                raise ValueError("选择窗元特征为空")
            meta_model = fit_meta_logit(
                features,
                labels,
                l2=float(candidate.get("meta", {}).get("l2", 1.0)),
                max_iter=int(candidate.get("meta", {}).get("max_iter", 500)),
            )
            notes.append(f"元模型训练样本 {len(features)} 行（仅选择窗）")
        except Exception as exc:  # noqa: BLE001 - C16 是探索项，失败不应拖垮整批
            logger.warning("C16 元模型不可用：%s", exc)
            status = "unavailable"
            notes.append(f"元模型不可用：{exc}")

    start, end = _window(config, "selection")[0], _window(config, "test")[1]
    if status == "ok" and meta_model is not None:
        all_days = _rank_days(ranks, horizon, members, start, end)
        features, _ = build_meta_frames(
            ranks,
            panel,
            all_days,
            members,
            horizon,
            int(candidate.get("min_coverage", 8)),
            int(config["evaluation"]["min_listing_days"]),
        )
        if features.empty:
            status = "unavailable"
            notes.append("元模型预测矩阵为空")
            scores: dict[pd.Timestamp, pd.Series] = {}
        else:
            predicted = predict_meta(meta_model, features)
            scores = {
                day: group.droplevel(0) for day, group in predicted.groupby(level=0)
            }
    elif status == "ok":
        scores = _scores_from_panels(
            panels,
            method=candidate.get("method", "mean"),
            weights=weights,
            min_coverage=int(candidate.get("min_coverage", 1)),
            trim=int(candidate.get("trim", 2)),
            consensus_gate=candidate.get("consensus_gate"),
            start=start,
            end=end,
        )
    else:
        scores = {}

    top_k = int(config["evaluation"]["top_k"])
    secondary_k = int(config["evaluation"]["secondary_top_k"])
    windows: dict[str, dict] = {}
    hits_by_window: dict[str, pd.DataFrame] = {}
    if scores:
        for name in WINDOW_NAMES:
            hits = _window_hits(scores, panel, config, name, top_k)
            hits_by_window[name] = hits
            summary = _summarize_hits(hits, top_k)
            secondary = _window_hits(scores, panel, config, name, secondary_k)
            summary["hit_exec_top20"] = (
                float(secondary["hit_exec"].mean())
                if not secondary.empty
                else float("nan")
            )
            windows[name] = summary
    else:
        for name in WINDOW_NAMES:
            windows[name] = {"days": 0}

    test_hits = hits_by_window.get("test", pd.DataFrame())
    boot = (
        block_bootstrap_ci(
            paired_diff(test_hits, baseline_hits["test"], "hit_exec"),
            n_boot=int(config["bootstrap"]["n_boot"]),
            block=str(config["bootstrap"]["block"]),
            seed=int(config["bootstrap"]["seed"]),
        )
        if not test_hits.empty
        else {
            "mean": float("nan"),
            "ci_low": float("nan"),
            "ci_high": float("nan"),
            "ci_width": float("nan"),
            "p_value": 1.0,
            "n_days": 0,
            "n_blocks": 0,
        }
    )

    monthly = {}
    if not test_hits.empty:
        diff = paired_diff(test_hits, baseline_hits["test"], "hit_exec")
        by_month = monthly_mean(diff)
        monthly = {
            "win_ratio": float((by_month > 0).mean())
            if len(by_month)
            else float("nan"),
            "months": {str(period): float(value) for period, value in by_month.items()},
            "candidate": {
                str(period): float(value)
                for period, value in test_hits["hit_exec"]
                .groupby(test_hits.index.to_period("M"))
                .mean()
                .items()
            },
        }

    gate = {}
    if not skip_gate and scores and "rebalance" in candidate.get("purposes", []):
        gate = _gate_for(scores, panel, config)

    loo = {}
    if (
        status == "ok"
        and candidate.get("method") == "mean"
        and len(members) >= 3
        and not test_hits.empty
    ):
        loo = _leave_one_out(
            candidate,
            config,
            panels,
            panel,
            weights,
            full_hit=float(test_hits["hit_exec"].mean()),
        )

    return {
        "id": cand_id,
        "name": candidate.get("name", cand_id),
        "horizon": horizon,
        "method": candidate.get("method", "mean"),
        "members": members,
        "weights": weights,
        "selection_ic": ic_values,
        "status": status,
        "notes": notes,
        "exploratory": bool(candidate.get("exploratory", False)),
        "windows": windows,
        "bootstrap": boot,
        "monthly": monthly,
        "gate": gate,
        "leave_one_out": loo,
    }


def _rank_days(
    ranks: dict[tuple[int, str], pd.DataFrame],
    horizon: int,
    members: list[str],
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.DatetimeIndex:
    index = pd.DatetimeIndex([])
    for key in members:
        frame = ranks.get((horizon, key))
        if frame is None:
            continue
        index = index.union(frame.loc[start:end].index)
    return index.sort_values()


def _selection_days(
    config: dict,
    ranks: dict[tuple[int, str], pd.DataFrame],
    horizon: int,
    members: list[str],
) -> pd.DatetimeIndex:
    start, end = _window(config, "selection")
    return _rank_days(ranks, horizon, members, start, end)


def _gate_for(scores: dict, panel, config: dict) -> dict:
    rebalance = config["rebalance"]
    cfg = RebalanceConfig(
        capital=float(rebalance["capital"]),
        top_k=int(rebalance["top_k"]),
        hold_days=int(rebalance["hold_days"]),
        entry_per_day=int(rebalance["entry_per_day"]),
        target_positions=int(rebalance["target_positions"]),
        cash_buffer=float(rebalance["cash_buffer"]),
        adv_participation=float(rebalance["adv_participation"]),
        exit_delta=float(rebalance["exit_delta"]),
        lookback=int(rebalance["lookback"]),
        min_lookback_days=int(rebalance["min_lookback_days"]),
        fee_rate=float(rebalance["fee_rate"]),
        min_listing_days=int(config["evaluation"]["min_listing_days"]),
    )
    start, end = _window(config, "test")
    result = simulate_rebalance(
        scores,
        panel,
        start=start.strftime("%Y-%m-%d"),
        end=end.strftime("%Y-%m-%d"),
        config=cfg,
    )
    reference = config["report_reference"]
    annual = result.performance.get("annual_return", float("nan"))
    drawdown = result.performance.get("max_drawdown", float("nan"))
    return {
        "performance": result.performance,
        "avg_trade_return": result.avg_trade_return,
        "n_entries": result.n_entries,
        "n_exits": result.n_exits,
        "deferred_exits": result.deferred_exits,
        "capacity_unchecked": result.capacity_unchecked,
        "open_positions": result.open_positions,
        "reference_annual": float(reference["full_period_annual"]),
        "annual_delta": float(annual - reference["full_period_annual"])
        if np.isfinite(annual)
        else float("nan"),
        "reference_max_drawdown": float(reference.get("max_drawdown", float("nan"))),
        "drawdown_delta": float(drawdown - reference.get("max_drawdown", drawdown))
        if np.isfinite(drawdown)
        else float("nan"),
    }


def _leave_one_out(
    candidate: dict,
    config: dict,
    panels: dict[str, pd.DataFrame],
    panel,
    weights: dict[str, float] | None,
    *,
    full_hit: float,
) -> dict:
    """逐个剔除成员，看命中是否依赖单一模型。"""
    min_coverage = int(candidate.get("min_coverage", 1))
    method = candidate.get("method", "mean")
    trim = int(candidate.get("trim", 2))
    start, end = _window(config, "selection")[0], _window(config, "test")[1]
    drops: dict[str, float] = {}
    for member in panels:
        subset = {key: frame for key, frame in panels.items() if key != member}
        if not subset:
            continue
        adjusted = weights
        if weights:
            adjusted = {key: value for key, value in weights.items() if key != member}
            total = sum(adjusted.values())
            adjusted = (
                {key: value / total for key, value in adjusted.items()}
                if total > 0
                else None
            )
        scores = _scores_from_panels(
            subset,
            method=method,
            weights=adjusted,
            min_coverage=min(min_coverage, len(subset)),
            trim=trim,
            consensus_gate=None,
            start=start,
            end=end,
        )
        hits = _window_hits(
            scores, panel, config, "test", int(config["evaluation"]["top_k"])
        )
        if hits.empty:
            continue
        drops[member] = full_hit - float(hits["hit_exec"].mean())
    if not drops:
        return {}
    member, delta = max(drops.items(), key=lambda item: item[1])
    return {"max_delta": float(delta), "member": member, "deltas": drops}


# ── 判据与推荐 ───────────────────────────────────────────────────────────


def apply_criteria(
    candidates: list[dict], config: dict
) -> tuple[pd.Series, list[dict]]:
    """BH q 值挂到每个候选上，再逐条判据出 verdict。"""
    pvalues = pd.Series(
        {
            candidate["id"]: float(candidate.get("bootstrap", {}).get("p_value", 1.0))
            for candidate in candidates
        }
    )
    qvalues = benjamini_hochberg(pvalues)
    criteria = config["criteria"]
    baseline = next((item for item in candidates if item["id"].startswith("C0_")), None)
    if baseline is None:
        raise RuntimeError("候选配置缺少 C0 基线")

    for candidate in candidates:
        cand_id = candidate["id"]
        q = float(qvalues.get(cand_id, 1.0))
        base_windows = baseline["windows"]
        test = candidate["windows"]["test"]
        validation = candidate["windows"]["validation"]
        delta_test = _delta(test, base_windows["test"], "hit_exec_mean")
        delta_validation = _delta(
            validation, base_windows["validation"], "hit_exec_mean"
        )
        boot = candidate.get("bootstrap", {})
        gate = candidate.get("gate", {})
        loo = candidate.get("leave_one_out", {})
        checks = {
            "status_ok": candidate["status"] == "ok",
            "coverage_ratio": test.get("full_top_ratio", 0.0)
            >= float(config["kill_criteria"]["K-R1_min_coverage_ratio"]),
            "min_hit_lift": delta_test >= float(criteria["min_hit_lift"]),
            "ci_lower_gt_zero": float(boot.get("ci_low", -1.0)) > 0.0,
            "ci_width": float(boot.get("ci_width", 99.0))
            <= float(criteria["max_ci_width"]),
            "bh_q": q < float(criteria["bh_q"]),
            "validation_delta": delta_validation
            > float(criteria["validation_delta_gt"]),
            "monthly_win_ratio": float(
                candidate.get("monthly", {}).get("win_ratio", 0.0)
            )
            >= float(criteria["min_monthly_win_ratio"]),
            "loo_stability": (
                not loo
                or float(loo.get("max_delta", 0.0))
                <= float(criteria["max_leave_one_out_delta"])
            ),
            "gate_annual": (
                not gate
                or not np.isfinite(gate.get("annual_delta", float("nan")))
                or float(gate["annual_delta"])
                >= -float(criteria["gate_annual_tolerance"])
            ),
            "gate_drawdown": (
                not gate
                or not np.isfinite(gate.get("drawdown_delta", float("nan")))
                or float(gate["drawdown_delta"])
                >= -float(criteria["gate_drawdown_tolerance"])
            ),
        }
        candidate["bh_q"] = q
        candidate["criteria"] = {
            "delta_test": delta_test,
            "delta_validation": delta_validation,
            "checks": checks,
        }
        failed = [name for name, passed in checks.items() if not passed]
        candidate["verdict"] = (
            "recommended"
            if not failed and not candidate["exploratory"]
            else "not_recommended"
        )
        candidate["failed_checks"] = failed
    return qvalues, candidates


def _delta(candidate: dict, baseline: dict, key: str) -> float:
    left = float(candidate.get(key, float("nan")))
    right = float(baseline.get(key, float("nan")))
    if not np.isfinite(left) or not np.isfinite(right):
        return float("nan")
    return left - right


def kill_criteria(candidates: list[dict], config: dict) -> dict:
    rules = config["kill_criteria"]
    c0 = next(item for item in candidates if item["id"].startswith("C0_"))
    coverage_ok = all(
        item["windows"]["test"].get("full_top_ratio", 0.0)
        >= float(rules["K-R1_min_coverage_ratio"])
        for item in candidates
        if item["status"] == "ok"
    )
    width_ok = all(
        float(item.get("bootstrap", {}).get("ci_width", 99.0))
        <= float(rules["K-R2_max_ci_width"])
        for item in candidates
        if item["status"] == "ok"
    )
    gate_ok = all(
        not item.get("gate")
        or not np.isfinite(item["gate"].get("annual_delta", float("nan")))
        or float(item["gate"]["annual_delta"]) >= -float(rules["K-R3_annual_tolerance"])
        for item in candidates
        if item["status"] == "ok"
    )
    return {
        "K-R1_min_coverage_ratio": {
            "threshold": float(rules["K-R1_min_coverage_ratio"]),
            "passed": bool(coverage_ok),
            "c0_full_top_ratio": float(
                c0["windows"]["test"].get("full_top_ratio", 0.0)
            ),
        },
        "K-R2_max_ci_width": {
            "threshold": float(rules["K-R2_max_ci_width"]),
            "passed": bool(width_ok),
        },
        "K-R3_annual_tolerance": {
            "threshold": float(rules["K-R3_annual_tolerance"]),
            "reference_annual": float(config["report_reference"]["full_period_annual"]),
            "passed": bool(gate_ok),
            "caveat": "参照为 2026-01..10 全期年化；本证据包检验窗仅 2026-05..08，"
            "年化噪声大，K-R3 只作方向性判据",
        },
        "K-R4_no_recommendation": {
            "enforced": bool(rules["K-R4_no_recommendation"]),
            "passed": True,
        },
    }


def build_recommendation(candidates: list[dict]) -> dict:
    eligible = [
        item
        for item in candidates
        if item["verdict"] == "recommended" and item["status"] == "ok"
    ]
    eligible.sort(
        key=lambda item: float(item["windows"]["test"].get("hit_exec_mean", 0.0)),
        reverse=True,
    )
    return {
        "ids": [item["id"] for item in eligible],
        "top": eligible[0]["id"] if eligible else None,
        "reason": (
            "按检验窗 Top50 可执行命中率排序，全部判据通过"
            if eligible
            else "无候选同时满足提升幅度、置信区间、验证窗与闸门判据（K-R4）"
        ),
    }


# ── 输出 ─────────────────────────────────────────────────────────────────


def _write_csv(rows: list[dict], path: Path) -> None:
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8-sig")


def _jsonable(value):
    """numpy / pandas 标量转 Python；NaN/inf → None（严格 JSON）。"""
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        number = float(value)
        return number if np.isfinite(number) else None
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if value is None or isinstance(value, (str, int)):
        return value
    return str(value)


def write_outputs(
    out_dir: Path,
    config_path: Path,
    config: dict,
    signature: dict,
    registry: dict[int, dict],
    individual: pd.DataFrame,
    candidates: list[dict],
    kill: dict,
    recommendation: dict,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    individual.to_csv(
        out_dir / "model_individual.csv", index=False, encoding="utf-8-sig"
    )

    hit_rows = []
    boot_rows = []
    monthly_rows = []
    gate_rows = []
    for item in candidates:
        for window, summary in item["windows"].items():
            hit_rows.append(
                {
                    "candidate": item["id"],
                    "name": item["name"],
                    "window": window,
                    **summary,
                }
            )
        boot = item.get("bootstrap", {})
        boot_rows.append(
            {
                "candidate": item["id"],
                "window": "test",
                "mean_delta": boot.get("mean"),
                "ci_low": boot.get("ci_low"),
                "ci_high": boot.get("ci_high"),
                "ci_width": boot.get("ci_width"),
                "p_value": boot.get("p_value"),
                "bh_q": item.get("bh_q"),
                "n_days": boot.get("n_days"),
                "n_blocks": boot.get("n_blocks"),
            }
        )
        for period, value in item.get("monthly", {}).get("months", {}).items():
            monthly_rows.append(
                {
                    "candidate": item["id"],
                    "month": period,
                    "delta_vs_c0": value,
                    "candidate_hit": item["monthly"]["candidate"].get(period),
                }
            )
        gate = item.get("gate") or {}
        if gate:
            gate_rows.append(
                {
                    "candidate": item["id"],
                    "window": "test",
                    **gate.get("performance", {}),
                    "avg_trade_return": gate.get("avg_trade_return"),
                    "n_entries": gate.get("n_entries"),
                    "n_exits": gate.get("n_exits"),
                    "deferred_exits": gate.get("deferred_exits"),
                    "capacity_unchecked": gate.get("capacity_unchecked"),
                    "open_positions": gate.get("open_positions"),
                    "reference_annual": gate.get("reference_annual"),
                    "annual_delta": gate.get("annual_delta"),
                    "drawdown_delta": gate.get("drawdown_delta"),
                }
            )
    _write_csv(hit_rows, out_dir / "next_day_hit.csv")
    _write_csv(boot_rows, out_dir / "bootstrap_ci.csv")
    _write_csv(monthly_rows, out_dir / "monthly_hit.csv")
    _write_csv(gate_rows, out_dir / "ten_day_gate.csv")

    evidence = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config": {"path": str(config_path), "sha256": config_digest(config_path)},
        "splits": config["splits"],
        "registry": {
            f"T{horizon}": {
                key: {
                    "run_id": run.run_dir.name if run.run_dir else None,
                    "ready": run.ready,
                    "reason": run.reason,
                }
                for key, run in models.items()
            }
            for horizon, models in registry.items()
        },
        "feature_signature": signature,
        "model_individual": individual.to_dict(orient="records"),
        "candidates": candidates,
        "kill_criteria": kill,
        "recommendation": recommendation,
        "caveats": [
            "静态 ST 名单以 2026-10-16 为 as-of，早于该日的历史回测存在 ST 前视。",
            "组合分是成员分位的等权/加权平均，不训练新模型（C16 除外且仅探索）。",
            "检验窗 2026-05-06..2026-08-26 仅 4 个月，年化指标噪声大。",
            "pred.parquet 的 pred 为各模型收益尺度；本评估先转截面分位再加权。",
        ],
    }
    (out_dir / "evidence.json").write_text(
        json.dumps(_jsonable(evidence), ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    (out_dir / "REPORT.md").write_text(
        _render_report(config, individual, candidates, kill, recommendation),
        encoding="utf-8",
    )


def _fmt(value: float, digits: int = 4) -> str:
    return "—" if value is None or not np.isfinite(value) else f"{value:.{digits}f}"


def _render_report(
    config: dict,
    individual: pd.DataFrame,
    candidates: list[dict],
    kill: dict,
    recommendation: dict,
) -> str:
    lines = [
        "# 组合选股次日命中研究（P1）",
        "",
        f"生成日期：{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "## 窗口",
        "",
    ]
    for name in WINDOW_NAMES:
        start, end = config["splits"][name]
        lines.append(f"- **{name}**：{start} .. {end}")
    lines += [
        "",
        "## 单模型基准（Top50 可执行命中率）",
        "",
        "| 周期 | 模型 | 选择窗 | 验证窗 | 检验窗 |",
        "|---|---|---|---|---|",
    ]
    for row in individual.sort_values(
        ["horizon", "test_hit_exec"], ascending=[True, False]
    ).to_dict(orient="records"):
        lines.append(
            f"| T+{row['horizon']} | {row['model']} | {_fmt(row['selection_hit_exec'])} | "
            f"{_fmt(row['validation_hit_exec'])} | {_fmt(row['test_hit_exec'])} |"
        )

    lines += [
        "",
        "## 候选组合（检验窗）",
        "",
        "| 候选 | 成员 | 命中率 | Δ vs C0 | 95% CI | 月份胜率 | 年化 | 回撤 | 判定 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    baseline = next(item for item in candidates if item["id"].startswith("C0_"))
    base_hit = baseline["windows"]["test"].get("hit_exec_mean", float("nan"))
    for item in candidates:
        test = item["windows"]["test"]
        boot = item.get("bootstrap", {})
        gate = item.get("gate") or {}
        delta = (
            test.get("hit_exec_mean", float("nan")) - base_hit
            if np.isfinite(test.get("hit_exec_mean", float("nan")))
            else float("nan")
        )
        lines.append(
            f"| {item['id']} | {', '.join(item['members'])} | "
            f"{_fmt(test.get('hit_exec_mean'))} | {_fmt(delta, 4)} | "
            f"[{_fmt(boot.get('ci_low'))}, {_fmt(boot.get('ci_high'))}] | "
            f"{_fmt(item.get('monthly', {}).get('win_ratio'), 2)} | "
            f"{_fmt(gate.get('performance', {}).get('annual_return'), 3)} | "
            f"{_fmt(gate.get('performance', {}).get('max_drawdown'), 3)} | "
            f"{item['verdict']} |"
        )

    lines += ["", "## Kill criteria", ""]
    for name, payload in kill.items():
        lines.append(
            f"- **{name}**：{'PASS' if payload.get('passed') else 'FAIL'} — {payload}"
        )

    lines += ["", "## 结论", ""]
    if recommendation["ids"]:
        lines.append(
            f"推荐候选：{', '.join(recommendation['ids'])}（首选 {recommendation['top']}）。"
        )
    else:
        lines.append(f"**无推荐**：{recommendation['reason']}")
    lines += ["", "## 口径与限制", ""]
    for caveat in (
        "组合分先转成员截面分位再加权；缺失成员重新归一，不按 0 分处理。",
        "可执行口径：t+1 收盘成交、t+2 收盘卖出，双边成本 6bp。",
        "10 日闸门与生产同参：Top50 / 新进 5 / 持有 10 / S≥20 日均分 / S<均分-0.03 退出。",
        "C7/C12/C16 只在选择窗拟合；C16 为探索项，永不进入推荐。",
        "静态 ST 名单存在历史前视；检验窗仅 4 个月，年化噪声大。",
    ):
        lines.append(f"- {caveat}")
    lines.append("")
    return "\n".join(lines)


# ── 入口 ─────────────────────────────────────────────────────────────────


def run(
    config_path: Path = DEFAULT_CONFIG,
    out_dir: Path = DEFAULT_OUT,
    *,
    skip_gate: bool = False,
    only: list[str] | None = None,
) -> dict:
    config = load_config(config_path)
    registry = resolve_registry(horizons=(5, 10), require_all=True)
    signature = model_signature(registry)
    logger.info("因子契约一致：%s", signature["consistent"])
    panel = build_panel(config)
    ranks = build_rank_panels(config, registry)

    top_k = int(config["evaluation"]["top_k"])
    individual, individual_hits = model_individual_table(config, ranks, panel, top_k)

    def _baseline_scores() -> dict:
        candidate = next(item for item in config["candidates"] if item.get("baseline"))
        horizon = int(candidate["horizon"])
        members = expand_members(config, candidate)
        return _scores_from_panels(
            candidate_panels(candidate, config, ranks, horizon, members),
            method=candidate.get("method", "mean"),
            weights=None,
            min_coverage=int(candidate.get("min_coverage", 1)),
            start=_window(config, "selection")[0],
            end=_window(config, "test")[1],
        )

    baseline_scores = _baseline_scores()
    baseline_hits = {
        name: _window_hits(baseline_scores, panel, config, name, top_k)
        for name in WINDOW_NAMES
    }

    selected = [
        item for item in config["candidates"] if only is None or item["id"] in only
    ]
    if only is not None:
        c0 = next(item for item in config["candidates"] if item.get("baseline"))
        if all(item["id"] != c0["id"] for item in selected):
            selected.insert(0, c0)
    results = []
    for candidate in selected:
        logger.info("评估候选 %s", candidate["id"])
        results.append(
            evaluate_candidate(
                candidate,
                config,
                ranks,
                panel,
                individual_hits,
                baseline_hits,
                skip_gate=skip_gate,
            )
        )

    _, results = apply_criteria(results, config)
    kill = kill_criteria(results, config)
    recommendation = build_recommendation(results)
    write_outputs(
        out_dir,
        config_path,
        config,
        signature,
        registry,
        individual,
        results,
        kill,
        recommendation,
    )
    logger.info("证据包已写入 %s", out_dir)
    return {
        "candidates": results,
        "kill_criteria": kill,
        "recommendation": recommendation,
        "out_dir": str(out_dir),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="组合选股次日命中研究（P1）")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--skip-gate", action="store_true", help="跳过 10 日闸门（快速冒烟）"
    )
    parser.add_argument("--candidates", default="", help="逗号分隔的候选 ID，缺省全跑")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    only = [item.strip() for item in args.candidates.split(",") if item.strip()] or None
    result = run(args.config, args.out, skip_gate=args.skip_gate, only=only)
    print(
        json.dumps(
            {
                "out_dir": result["out_dir"],
                "recommendation": result["recommendation"],
                "kill_criteria": result["kill_criteria"],
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
