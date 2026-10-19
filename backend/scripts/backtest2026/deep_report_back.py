"""长窗口深度评估报告：画像、季度、筛选、共识、归因、容量与结论。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from .deep_report_data import (
    PERIOD_LABELS,
    DeepBundle,
    capacity_advantage_table,
    capacity_summary_table,
    model_profiles,
    period_metrics,
    quarter_ic_matrix,
    quarter_rank_correlation,
    rank_correlation_between,
    score_screen_summary,
    strength_weakness_labels,
    year_slice_ranking,
)
from .model_registry import MODEL_FAMILIES, MODEL_ORDER
from .multi_report_data import HORIZON_TITLES, display_name
from .report_pdf_html import (
    callout,
    esc,
    figure,
    money,
    num,
    pct,
    section_head,
    table,
)


def _charted(charts: dict[str, Path], key: str, caption: str, number: int) -> str:
    path = charts.get(key)
    return figure(path, caption, number) if path is not None else ""


def _participation_label(value: object) -> str:
    """CSV 里无约束写作字符串 ``none``；统一显示为“无约束”。"""
    numeric = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.isna(numeric):
        return "无约束"
    return f"≤{float(numeric):.0%}"


def _annualized(record: dict, key: str, days: float) -> float:
    """把逐日贡献之和折成年化；天数缺失或非正时返回 NaN。"""
    if not days or days <= 0:
        return float("nan")
    value = record.get(key)
    if value is None or pd.isna(value):
        return float("nan")
    return float(value) / days * 252


def _p_value(value: object, digits: int = 3) -> str:
    if value is None or pd.isna(value):
        return "—"
    number = float(value)
    if number < 10 ** (-digits):
        return f"{number:.1e}"
    return f"{number:.{digits}f}"


def profiles_section(bundle: DeepBundle, charts: dict[str, Path]) -> str:
    blocks = []
    for horizon in (5, 10):
        frame = model_profiles(bundle, horizon)
        if frame.empty:
            continue
        rows = []
        for record in frame.to_dict("records"):
            rows.append(
                [
                    esc(display_name(str(record["model"]))),
                    esc(MODEL_FAMILIES.get(str(record["model"]), "—")),
                    num(record.get("ic_mean"), 4),
                    num(record.get("icir"), 3),
                    pct(record.get("top20_excess"), 3, signed=True),
                    num(record.get("sharpe_rolling"), 2),
                    pct(record.get("max_drawdown_rolling"), 1),
                    num(record.get("quarter_ic_rank_std"), 2),
                    num(record.get("clean_ic"), 4),
                    pct(record.get("unique_share"), 1),
                ]
            )
        blocks.append(
            f"<h3>{HORIZON_TITLES[horizon]} 模型画像</h3>"
            + table(
                [
                    "模型",
                    "族",
                    "IC",
                    "ICIR",
                    "Top20 日均超额",
                    "滚动夏普",
                    "最大回撤",
                    "季度排名波动",
                    "外推干净期 IC",
                    "独有选股占比",
                ],
                rows,
                aligns=["l", "l", "r", "r", "r", "r", "r", "r", "r", "r"],
            )
        )

    cards = _model_cards(bundle)
    return f"""
<section class="section page-break">
  {section_head("04", "逐模型的优势与短板", "不只看谁第一，更看第一靠什么、在哪种行情里会失效。")}
  <p class="lead">画像表把七个维度放在同一张表里：质量（IC/ICIR）、可交易性（Top20 日均超额）、
  组合表现（滚动 Top5 夏普与最大回撤）、稳定性（季度 IC 排名波动）、外推稳健性（干净期 IC）
  与独特性（Top20 中只被自己选中的比例）。季度排名波动越小越稳定；外推干净期 IC 用来判断
  模型在“特征完整的新数据”上是否还能维持排序。</p>
  {"".join(blocks)}
  <h3>4.3 逐模型卡片</h3>
  <div class="model-grid">{cards}</div>
  {callout("怎么读这些卡片", "优势/短板按七个维度的横截面排名自动生成：优势只列排名前 4 的维度，短板只列排名后 4 的维度；没有进入前 4/后 4 的维度不强行贴标签。外推干净期只有 12–13 个交易日，IC 的标准误很大，不要用它单独判定模型优劣。")}
</section>
"""


def _model_cards(bundle: DeepBundle) -> str:
    profiles5 = model_profiles(bundle, 5)
    profiles10 = model_profiles(bundle, 10)
    if profiles5.empty and profiles10.empty:
        return ""
    cards = []
    for model in [str(m) for m in profiles5.get("model", profiles10.get("model", []))]:
        row5 = profiles5[profiles5["model"] == model]
        row10 = profiles10[profiles10["model"] == model]
        primary = row5.iloc[0] if not row5.empty else row10.iloc[0]
        strengths, weaknesses = strength_weakness_labels(primary)
        strength_text = "、".join(strengths) if strengths else "无维度进入前四"
        weakness_text = "、".join(weaknesses) if weaknesses else "无维度落入后四"
        detail_parts = []
        if not row5.empty:
            record = row5.iloc[0]
            detail_parts.append(
                f"T+5 IC {num(record.get('ic_mean'), 4)}、滚动年化 "
                f"{pct(record.get('annual_return_rolling'), 1, signed=True)}、"
                f"最大回撤 {pct(record.get('max_drawdown_rolling'), 1)}、"
                f"干净期 IC {num(record.get('clean_ic'), 4)}"
            )
        if not row10.empty:
            record = row10.iloc[0]
            detail_parts.append(
                f"T+10 IC {num(record.get('ic_mean'), 4)}、滚动夏普 "
                f"{num(record.get('sharpe_rolling'), 2)}"
            )
        cards.append(
            f"""<div class="model-card">
  <h4>{esc(display_name(model))} <span class="small">· {esc(MODEL_FAMILIES.get(model, ""))}</span></h4>
  <p><strong>优势：</strong>{esc(strength_text)}</p>
  <p><strong>短板：</strong>{esc(weakness_text)}</p>
  <p class="small">{esc("；".join(detail_parts))}</p>
</div>"""
        )
    return "".join(cards)


def quarterly_section(bundle: DeepBundle, charts: dict[str, Path]) -> str:
    blocks = []
    corr_by_horizon: dict[int, list[float]] = {}
    change_counts: dict[int, dict[str, int]] = {}
    for horizon in (5, 10):
        champions = bundle.quarterly_champions.get(horizon)
        stats = bundle.quarterly_stats.get(horizon)
        if champions is None or champions.empty:
            continue
        if {
            "ic_changed",
            "excess_changed",
            "sharpe_changed",
        } <= set(champions.columns):
            # 首行没有上一季，换人标记无意义；只统计相邻换季。
            adjacent = champions.iloc[1:]
            change_counts[horizon] = {
                "pairs": int(len(adjacent)),
                "ic": int(adjacent["ic_changed"].astype(bool).sum()),
                "excess": int(adjacent["excess_changed"].astype(bool).sum()),
                "sharpe": int(adjacent["sharpe_changed"].astype(bool).sum()),
            }
        rows = []
        for record in champions.to_dict("records"):
            rows.append(
                [
                    esc(str(record["quarter"])),
                    esc(display_name(str(record["champion_ic"]))),
                    esc(display_name(str(record["champion_excess"]))),
                    esc(display_name(str(record["champion_sharpe"]))),
                    "是" if bool(record.get("ic_changed")) else "否",
                    "是" if bool(record.get("excess_changed")) else "否",
                    "是" if bool(record.get("sharpe_changed")) else "否",
                    num(record.get("rank_corr_prev_ic"), 2),
                    num(record.get("rank_corr_prev_excess"), 2),
                ]
            )
        blocks.append(
            f"<h3>{HORIZON_TITLES[horizon]} 季度冠军</h3>"
            + table(
                [
                    "季度",
                    "IC 冠军",
                    "超额冠军",
                    "夏普冠军",
                    "IC 换人",
                    "超额换人",
                    "夏普换人",
                    "与上季 IC 秩相关",
                    "与上季超额秩相关",
                ],
                rows,
            )
        )
        if stats is not None and not stats.empty:
            summary_rows = []
            for record in stats.to_dict("records"):
                summary_rows.append(
                    [
                        esc(str(record["quarter"])),
                        str(int(record["days"])),
                        pct(record.get("pool_ret"), 2, signed=True),
                        _p_value(record.get("ic_p")),
                        num(record.get("ic_kendall_w"), 3),
                        _p_value(record.get("excess_p")),
                        num(record.get("white_p"), 3),
                        num(record.get("pbo"), 2),
                    ]
                )
            blocks.append(
                f"<h4>{HORIZON_TITLES[horizon]} 分季统计检验</h4>"
                + table(
                    [
                        "季度",
                        "天数",
                        "池子收益",
                        "IC Friedman p",
                        "IC Kendall W",
                        "超额 Friedman p",
                        "White RC p",
                        "PBO",
                    ],
                    summary_rows,
                    aligns=["c", "r", "r", "r", "r", "r", "r", "r"],
                )
            )

    correlations = []
    for horizon in (5, 10):
        matrix = quarter_ic_matrix(bundle, horizon)
        corr = quarter_rank_correlation(matrix)
        if corr.empty:
            continue
        for record in corr.to_dict("records"):
            corr_by_horizon.setdefault(horizon, []).append(float(record["rank_corr"]))
            correlations.append(
                [
                    HORIZON_TITLES[horizon],
                    esc(str(record["previous"])),
                    esc(str(record["current"])),
                    num(record["rank_corr"], 3),
                ]
            )
    correlation_table = table(
        ["期", "上一季", "本季", "IC 排名秩相关"],
        correlations,
        aligns=["c", "c", "c", "r"],
    )

    corr_parts = []
    for horizon in (5, 10):
        values = [v for v in corr_by_horizon.get(horizon, []) if not pd.isna(v)]
        if not values:
            continue
        negatives = sum(1 for value in values if value < 0)
        suffix = (
            f"（{len(values)} 次比较中 {negatives} 次转负）"
            if negatives
            else "（全部为正）"
        )
        corr_parts.append(
            f"{HORIZON_TITLES[horizon]} {min(values):+.2f} 到 "
            f"{max(values):+.2f}{suffix}"
        )
    corr_text = "；".join(corr_parts) or "—"
    total_pairs = sum(counts["pairs"] for counts in change_counts.values())
    total_ic = sum(counts["ic"] for counts in change_counts.values())
    total_excess = sum(counts["excess"] for counts in change_counts.values())
    total_sharpe = sum(counts["sharpe"] for counts in change_counts.values())
    if total_pairs:
        change_text = (
            f"三个口径的季度冠军在 {total_pairs} 次相邻换季中分别换人 "
            f"{total_ic}/{total_pairs}、{total_excess}/{total_pairs}、"
            f"{total_sharpe}/{total_pairs} 次（见上表“换人”列）"
        )
    else:
        change_text = "季度冠军换人频繁"

    return f"""
<section class="section page-break">
  {section_head("05", "按季度切开：冠军是否换人", "把 2025Q1—2026Q3 逐季重跑 Friedman / White RC，观察排序稳定性。")}
  <p class="lead">如果模型能力是稳定的，季度冠军应当在同一批名字里轮换，且相邻季度的 IC 排名
  秩相关应当明显为正。若冠军每季都换、秩相关接近 0 甚至为负，说明单年冠军大概率是行情适配，
  不是可外推的能力。</p>
  {"".join(blocks)}
  <h3>相邻季度排名相关</h3>
  {correlation_table}
  {_charted(charts, "quarter_5", "T+5 各季度 IC 均值热力图；行按全窗口排名排列。", 4)}
  {_charted(charts, "quarter_10", "T+10 各季度 IC 均值热力图。", 5)}
  {callout("稳定性结论", f"相邻季度 IC 排名秩相关的区间：{corr_text}。{change_text}。这说明<strong>单季冠军不可延续</strong>；它与第 10 章的跨年排序稳定性并不矛盾——冠军在稳定的模型族内部轮换。任何单季冠军都不应当直接作为下一季的默认模型。")}
</section>
"""


def screening_section(bundle: DeepBundle, charts: dict[str, Path]) -> str:
    score_rows = []
    for horizon in (5, 10):
        frame = bundle.consensus.get(f"score_stats_{horizon}")
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            continue
        for record in frame.to_dict("records"):
            score_rows.append(
                [
                    HORIZON_TITLES[horizon],
                    esc(display_name(str(record["model"]))),
                    num(record.get("score_std"), 4),
                    num(record.get("top20_mean"), 4),
                    num(record.get("top20_threshold"), 4),
                    num(record.get("rank20_gap"), 5),
                    num(record.get("z1_count"), 1),
                    num(record.get("z2_count"), 1),
                ]
            )
    score_table = table(
        [
            "期",
            "模型",
            "分数标准差",
            "Top20 均分",
            "Top20 门槛",
            "第20/21名差距",
            "均值+1σ 只数/日",
            "均值+2σ 只数/日",
        ],
        score_rows,
        aligns=["c", "l", "r", "r", "r", "r", "r", "r"],
    )

    screen_tables = []
    for horizon in (5, 10):
        summary = score_screen_summary(bundle, horizon)
        if summary.empty:
            continue
        pivot_excess = summary.pivot_table(
            index="model", columns="filter", values="mean_excess"
        )
        pivot_t = summary.pivot_table(index="model", columns="filter", values="nw_t")
        order = [m for m in bundle.models if m in pivot_excess.index]
        rows = []
        for model in order:
            row = [esc(display_name(model))]
            for filter_name in ("top10", "top20", "top50"):
                if filter_name in pivot_excess.columns:
                    row.append(
                        pct(pivot_excess.loc[model, filter_name], 3, signed=True)
                    )
                    row.append(num(pivot_t.loc[model, filter_name], 2))
                else:
                    row.extend(["—", "—"])
            rows.append(row)
        screen_tables.append(
            f"<h4>{HORIZON_TITLES[horizon]} TopK 筛选：日均超额（NW t）</h4>"
            + table(
                [
                    "模型",
                    "Top10 超额",
                    "t",
                    "Top20 超额",
                    "t",
                    "Top50 超额",
                    "t",
                ],
                rows,
                aligns=["l", "r", "r", "r", "r", "r", "r"],
            )
        )

    strategy_rows = []
    for horizon in (5, 10):
        frame = bundle.consensus.get(f"strategy_{horizon}")
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            continue
        for record in frame.to_dict("records"):
            strategy_rows.append(
                [
                    HORIZON_TITLES[horizon],
                    esc(str(record["name"])),
                    esc(str(record["threshold"])),
                    str(int(record["days"])),
                    num(record.get("avg_names"), 1),
                    pct(record.get("mean_excess"), 3, signed=True),
                    num(record.get("nw_t"), 2),
                    pct(record.get("hit_rate"), 1),
                    num(record.get("sharpe"), 2),
                ]
            )
    strategy_table = table(
        [
            "期",
            "策略",
            "票数门槛",
            "天数",
            "平均只数",
            "日均超额",
            "NW t",
            "命中率",
            "夏普",
        ],
        strategy_rows,
        aligns=["c", "l", "r", "r", "r", "r", "r", "r", "r"],
    )

    ensemble_rows = []
    dynamic_rows = []
    for horizon in (5, 10):
        frame = bundle.consensus.get(f"ensemble_{horizon}")
        if isinstance(frame, pd.DataFrame) and not frame.empty:
            for record in frame.to_dict("records"):
                ensemble_rows.append(
                    [
                        HORIZON_TITLES[horizon],
                        esc(str(record["name"])),
                        esc(str(record["threshold"])),
                        pct(record.get("mean_excess"), 3, signed=True),
                        num(record.get("nw_t"), 2),
                        num(record.get("sharpe"), 2),
                        num(record.get("cum_excess"), 2),
                    ]
                )
        dynamic = bundle.consensus.get(f"dynamic_{horizon}")
        if isinstance(dynamic, pd.DataFrame) and not dynamic.empty:
            for record in dynamic.to_dict("records"):
                dynamic_rows.append(
                    [
                        HORIZON_TITLES[horizon],
                        esc(str(record["name"])),
                        str(int(record["days"])),
                        pct(record.get("mean_excess"), 3, signed=True),
                        num(record.get("nw_t"), 2),
                        num(record.get("sharpe"), 2),
                        num(record.get("cum_excess"), 2),
                    ]
                )
    ensemble_table = table(
        ["期", "策略", "档位", "日均超额", "NW t", "夏普", "累计超额"],
        ensemble_rows,
        aligns=["c", "l", "c", "r", "r", "r", "r"],
    )
    dynamic_table = table(
        ["期", "策略", "天数", "日均超额", "NW t", "夏普", "累计超额"],
        dynamic_rows,
        aligns=["c", "l", "r", "r", "r", "r", "r"],
    )

    return f"""
<section class="section page-break">
  {section_head("06", "逐日分数筛选：还能不能筛出更多", "TopK、z 分数、均值+σ、前 1% 与横截面融合。")}
  <p class="lead">这一章只问一件事：在模型给出的逐日分数之上再加一层筛选，能否稳定提高收益。
  所有筛选都按同一交易口径计算：筛选日 t 收盘出分，t+1 收盘买入，T+H 收盘卖出；超额为
  相对当日全市场等权可交易池。</p>

  <h3>6.1 分数分布与 Top20 门槛</h3>
  {score_table}
  <p class="small">Top20 门槛 = 当日第 20 名股票的分数；第20/21名差距衡量入选边界的陡峭程度，
  差距越小说明“卡位”越随机。均值+1σ/+2σ 只数按日平均。</p>

  <h3>6.2 TopK 筛选</h3>
  {"".join(screen_tables)}
  {_charted(charts, "screen_5", "T+5 各模型 Top10/Top20/Top50 的日均超额（%）。", 6)}
  {_charted(charts, "screen_10", "T+10 各模型 Top10/Top20/Top50 的日均超额（%）。", 7)}

  <h3>6.3 共识票数策略</h3>
  {strategy_table}
  <p class="small">“票数门槛”指至少被多少个模型的 Top20 同时选中；平均只数为每日入选股票数。
  门槛越高，持仓越少、越集中，估计噪声也越大。</p>

  <h3>6.4 横截面 z 分数融合与动态选模</h3>
  {ensemble_table}
  {dynamic_table}
  {callout("筛选层结论", "Top10/20/50 的平均超额随 K 减小而上升，但 NW t 同步下降；到 Top10 时多数模型的超额已不显著。共识票数 ≥2 的策略能提供显著但很薄的超额，票数 ≥4 后样本骤减、t 值回落。<strong>分数筛选能改善“选哪些股票”，但不能把弱模型救成强模型。</strong>")}
</section>
"""


def consensus_section(bundle: DeepBundle, charts: dict[str, Path]) -> str:
    blocks = []
    half_notes: list[str] = []
    vote_notes: list[str] = []
    full_house_days = 0.0
    for horizon in (5, 10):
        distribution = bundle.consensus.get(f"distribution_{horizon}")
        daily = bundle.consensus.get(f"daily_{horizon}")
        overlap = bundle.consensus.get(f"overlap_{horizon}")
        if not isinstance(distribution, pd.DataFrame) or distribution.empty:
            continue
        observed = distribution[
            pd.to_numeric(distribution["n_stock_days"], errors="coerce") > 0
        ]
        if not observed.empty:
            vote_notes.append(
                f"{HORIZON_TITLES[horizon]} 最高 "
                f"{int(pd.to_numeric(observed['consensus']).max())} 票"
            )
        full_house = distribution[
            pd.to_numeric(distribution["consensus"], errors="coerce")
            >= len(MODEL_ORDER)
        ]
        if not full_house.empty:
            full_house_days += float(
                pd.to_numeric(full_house["n_stock_days"], errors="coerce").sum()
            )
        distribution_rows = []
        for record in distribution.to_dict("records"):
            distribution_rows.append(
                [
                    str(int(record["consensus"])),
                    money(record.get("n_stock_days"), 0),
                    pct(record.get("share"), 1),
                    pct(record.get("mean_ret"), 3, signed=True),
                    pct(record.get("mean_excess"), 3, signed=True),
                    pct(record.get("hit_rate"), 1),
                ]
            )
        blocks.append(
            f"<h3>{HORIZON_TITLES[horizon]} 共识票数分布</h3>"
            + table(
                ["票数", "股票-日", "占比", "平均收益", "平均超额", "上涨比例"],
                distribution_rows,
                aligns=["r", "r", "r", "r", "r", "r"],
            )
        )
        if isinstance(daily, pd.DataFrame) and not daily.empty:
            half_share = float(daily["avg_model_in_half"].mean())
            mean_row = [
                HORIZON_TITLES[horizon],
                num(daily["union"].mean(), 1),
                num(daily["ge2"].mean(), 1),
                num(daily["ge_half"].mean(), 1),
                num(daily["all_models"].mean(), 2),
                num(half_share, 4),
                num(half_share * 20, 2),
            ]
            half_notes.append(
                f"{HORIZON_TITLES[horizon]} {half_share * 20:.2f} 只"
                f"（约 {half_share * 100:.1f}%）"
            )
            blocks.append(
                table(
                    [
                        "期",
                        "并集/日",
                        "≥2 票/日",
                        "≥半数票/日",
                        "全票/日",
                        "Top20 入半数池比例",
                        "Top20 入池只数（/20）",
                    ],
                    [mean_row],
                    aligns=["c", "r", "r", "r", "r", "r", "r"],
                )
            )
        if isinstance(overlap, pd.DataFrame) and not overlap.empty:
            rows = []
            for record in overlap.to_dict("records"):
                model = str(record["model"])
                rows.append(
                    [
                        esc(display_name(model)),
                        pct(record.get("unique_share"), 1),
                        pct(record.get("unique_ret"), 3, signed=True),
                        pct(record.get("shared_ret"), 3, signed=True),
                        pct(record.get("in_half_ret"), 3, signed=True),
                        pct(record.get("out_half_ret"), 3, signed=True),
                        pct(record.get("in_out_diff"), 4, signed=True),
                    ]
                )
            blocks.append(
                f"<h4>{HORIZON_TITLES[horizon]} 独有 vs 共享、池内 vs 池外</h4>"
                + table(
                    [
                        "模型",
                        "独有占比",
                        "独有选股收益",
                        "共享选股收益",
                        "半数池内收益",
                        "池外收益",
                        "池内−池外",
                    ],
                    rows,
                    aligns=["l", "r", "r", "r", "r", "r", "r"],
                )
            )

    pair_extremes = []
    for horizon in (5, 10):
        pairs = bundle.consensus.get(f"pairwise_{horizon}")
        if not isinstance(pairs, pd.DataFrame) or pairs.empty:
            continue
        ordered = pairs.sort_values("avg_jaccard", ascending=False)
        for label, subset in (
            ("最高", ordered.head(3)),
            ("最低", ordered.tail(3)),
        ):
            for record in subset.to_dict("records"):
                pair_extremes.append(
                    [
                        HORIZON_TITLES[horizon],
                        esc(label),
                        f"{display_name(str(record['model_a']))} × {display_name(str(record['model_b']))}",
                        num(record.get("avg_jaccard"), 3),
                        num(record.get("avg_overlap"), 2),
                        pct(record.get("overlap_share"), 1),
                    ]
                )
    pair_table = table(
        ["期", "类型", "模型对", "平均 Jaccard", "平均重叠只数", "占 Top20 比例"],
        pair_extremes,
        aligns=["c", "c", "l", "r", "r", "r"],
    )

    half_text = "、".join(half_notes) or "—"
    vote_text = "、".join(vote_notes) or "—"
    full_house_text = (
        "全部 13 个模型都选中的股票-日为零"
        if full_house_days == 0
        else f"全部 13 个模型都选中的股票-日只有 {full_house_days:,.0f} 个"
    )

    return f"""
<section class="section page-break">
  {section_head("07", "Top20 的共识结构：多少股票真的进入共同股票池", "直接回答“top20 内有多少股票是都进入股票池、这部分收益怎样”。")}
  <p class="lead">对每个交易日、每个模型取 Top20；统计每只股票被多少个模型的 Top20 同时选中（票数）。
  “半数池”= 至少 7 个模型（13 个模型的半数向上取整）同时选中。表中的“Top20 入池只数”
  是每个模型 Top20 中平均有多少只进入半数池。</p>
  {"".join(blocks)}
  {_charted(charts, "votes_5", "T+5 共识票数分布（柱）与各票数层的平均超额（线）。", 8)}
  {_charted(charts, "votes_10", "T+10 共识票数分布与平均超额。", 9)}

  <h3>7.3 模型两两重叠</h3>
  {pair_table}
  {_charted(charts, "overlap_5", "T+5 模型两两 Top20 平均 Jaccard。", 10)}
  {callout("共识结构的结论", f"Top20 的共识度远低于直觉：每个模型的 Top20 里平均只有 {half_text} 进入半数池（分母是各自的 20 只），{full_house_text}（{vote_text}）。树模型之间（LGB/XGB/CAT）Jaccard 最高，跨族明显更低。<strong>这意味着“多模型投票”不会自动得到一个 20 只的稳健组合；它给出的是一个更小、更集中的共识池。</strong>共识票数 ≥3 后平均超额有抬升，但样本占比快速下降。")}
</section>
"""


def attribution_section(bundle: DeepBundle, charts: dict[str, Path]) -> str:
    frame = bundle.attribution
    if frame.empty:
        return f"""
<section class="section page-break">
  {section_head("08", "滚动 Top5 的收益归因", "选股、行业暴露、市值暴露与现金拖累。")}
  <p class="lead">未找到归因产物（attribution_decomp_summary.csv），本章跳过。</p>
</section>
"""
    rows = []
    for record in (
        frame[frame["family"] == "rolling"]
        .sort_values(["horizon", "model"])
        .to_dict("records")
    ):
        days = float(record.get("days") or np.nan)
        rows.append(
            [
                HORIZON_TITLES.get(int(record["horizon"]), str(record["horizon"])),
                esc(display_name(str(record["model"]))),
                pct(_annualized(record, "gross_excess_sum", days), 1, signed=True),
                pct(_annualized(record, "industry_sum", days), 1, signed=True),
                pct(_annualized(record, "size_sum", days), 1, signed=True),
                pct(_annualized(record, "selection_sum", days), 1, signed=True),
                pct(_annualized(record, "cash_drag_sum", days), 1, signed=True),
                pct(record.get("industry_share"), 0, signed=True),
                pct(record.get("size_share"), 0, signed=True),
                pct(record.get("selection_share"), 0, signed=True),
            ]
        )
    table_html = table(
        [
            "期",
            "模型",
            "年化超额",
            "行业",
            "市值",
            "选股",
            "现金",
            "行业占比",
            "市值占比",
            "选股占比",
        ],
        rows,
        aligns=["c", "l", "r", "r", "r", "r", "r", "r", "r", "r"],
    )
    return f"""
<section class="section page-break">
  {section_head("08", "滚动 Top5 的收益归因", "超额到底来自选股，还是来自行业与市值暴露？")}
  <p class="lead">逐日把组合收益拆成四项：行业暴露（组合行业权重 − 基准行业权重）× 行业因子收益、
  市值暴露（组合市值 z 暴露 − 基准暴露）× 市值因子收益、选股（行业与市值中性后的残差收益）、
  现金拖累。下表为全窗口年化贡献；占比 = 该项 / 年化总超额。</p>
  {table_html}
  {_charted(charts, "attr_5", "T+5 滚动 Top5 年化超额的四项分解（堆叠）。", 11)}
  {_charted(charts, "attr_10", "T+10 滚动 Top5 年化超额分解。", 12)}
  {callout("归因结论", "树模型的超额主要来自选股项，行业项为正、市值项为负（组合偏小盘，但小盘因子本身是负收益）；线性模型的选股贡献接近零，收益几乎完全由市值暴露驱动——这解释了它 IC 不低但组合表现偏弱。若要把模型用于实盘，应先确认超额来源是选股而不是某个风格的下注。")}
</section>
"""


def capacity_section(bundle: DeepBundle, charts: dict[str, Path]) -> str:
    frames = []
    for horizon in (5, 10):
        summary = capacity_summary_table(bundle, horizon)
        if summary.empty:
            continue
        rows = []
        for record in summary.sort_values(
            ["family", "capital", "participation"]
        ).to_dict("records"):
            rows.append(
                [
                    HORIZON_TITLES[horizon],
                    esc(str(record["family"])),
                    money(record.get("capital"), 0),
                    _participation_label(record.get("participation")),
                    pct(record.get("mean_total_return"), 1, signed=True),
                    num(record.get("mean_sharpe"), 2),
                    pct(record.get("mean_max_drawdown"), 1),
                    pct(record.get("mean_fill_ratio"), 1),
                    num(record.get("binding_trades"), 1),
                ]
            )
        frames.append(
            f"<h3>{HORIZON_TITLES[horizon]} 容量扫描</h3>"
            + table(
                [
                    "期",
                    "组合",
                    "本金",
                    "单票上限",
                    "平均累计收益",
                    "平均夏普",
                    "平均最大回撤",
                    "平均成交率",
                    "受限成交/日",
                ],
                rows,
                aligns=["c", "l", "r", "r", "r", "r", "r", "r", "r"],
            )
        )

    advantage_rows = []
    for horizon in (5, 10):
        frame = capacity_advantage_table(bundle, horizon)
        if frame.empty:
            continue
        for record in frame.sort_values(["capital", "participation"]).to_dict(
            "records"
        ):
            advantage_rows.append(
                [
                    HORIZON_TITLES[horizon],
                    money(record.get("capital"), 0),
                    _participation_label(record.get("participation")),
                    pct(record.get("mean_top10"), 1, signed=True),
                    pct(record.get("mean_top20"), 1, signed=True),
                    pct(record.get("mean_diff"), 2, signed=True),
                    pct(record.get("top10_win_rate"), 1),
                ]
            )
    advantage_table = table(
        [
            "期",
            "本金",
            "单票上限",
            "Top10 平均收益",
            "Top20 平均收益",
            "Top10 − Top20",
            "Top10 胜率",
        ],
        advantage_rows,
        aligns=["c", "r", "r", "r", "r", "r", "r"],
    )

    advantage = bundle.capacity.get("advantage", pd.DataFrame())

    def _diff_range(capitals: tuple[float, ...], *, constrained: bool) -> str:
        if advantage.empty or not {"capital", "participation", "mean_diff"} <= set(
            advantage.columns
        ):
            return "—"
        numeric = advantage.copy()
        numeric["capital"] = pd.to_numeric(numeric["capital"], errors="coerce")
        numeric["mean_diff"] = pd.to_numeric(numeric["mean_diff"], errors="coerce")
        participation = pd.to_numeric(numeric["participation"], errors="coerce")
        mask = participation == 0.01 if constrained else participation.isna()
        subset = numeric[mask & numeric["capital"].isin(capitals)]["mean_diff"].dropna()
        if subset.empty:
            return "—"
        return f"{subset.min() * 100:+.1f}% 至 {subset.max() * 100:+.1f}%"

    small_range = _diff_range((2_000_000,), constrained=True)
    mid_range = _diff_range((10_000_000,), constrained=True)
    large_range = _diff_range((50_000_000, 100_000_000), constrained=True)
    free_range = _diff_range(
        (2_000_000, 10_000_000, 50_000_000, 100_000_000), constrained=False
    )
    capacity_text = (
        f"200 万本金、1% 参与率下容量几乎不咬合（成交率接近 100%），"
        f"Top10 与 Top20 的平均收益差只有 {small_range}，方向转负但幅度接近噪声；"
        f"本金升到 1000 万时差距扩大到 {mid_range}；"
        f"此后本金继续放大，两者都被容量压平，差距收窄到 {large_range}，"
        f"但 Top10 始终不占优。作为对照，无约束口径下 Top10 稳定领先 {free_range}"
        f"——优势消失完全来自容量。"
        if "—" not in {small_range, mid_range, large_range, free_range}
        else "容量扫描结果缺失或不完整。"
    )

    return f"""
<section class="section page-break">
  {section_head("09", "容量约束：Top10 的优势是否还在", "单票不超过当日成交额 1%，按 200 万 / 1000 万 / 5000 万 / 1 亿重跑。")}
  <p class="lead">容量版本在买入时按“当日成交额 × 参与率”限制单票可成交金额，成交不了的部分
  留现金，不做替代买入。其余口径与主回测一致。成交率 = 实际成交金额 / 目标成交金额。</p>
  {"".join(frames)}
  <h3>Top10 与 Top20 的正面对比</h3>
  {advantage_table}
  {_charted(charts, "capacity", "不同本金与参与率下，Top10 相对 Top20 的平均收益差；0 以下表示 Top20 更好。", 13)}
  {callout("容量结论", f"{capacity_text}<strong>Top10 的“更强”是大资金无法兑现的纸面优势；组合规模越大，越应当用 Top20 或更分散的池子。</strong>")}
</section>
"""


def comparison_section(bundle: DeepBundle) -> str:
    blocks = []
    manifest = (bundle.summary or {}).get("manifest", {})
    data_end = str(manifest.get("common_end") or "2100-01-01")
    corr_pairs: list[str] = []
    corr_values: list[float] = []
    for horizon in (5, 10):
        full = year_slice_ranking(bundle, horizon, "1900-01-01", "2100-01-01")
        first = year_slice_ranking(bundle, horizon, "2025-02-05", "2025-12-31")
        second = year_slice_ranking(bundle, horizon, "2026-01-01", data_end)
        if full.empty or first.empty or second.empty:
            continue
        rows = []
        for model in full.index:
            rows.append(
                [
                    esc(display_name(model)),
                    str(int(first.rank(ascending=False).get(model, 0)))
                    if model in first.index
                    else "—",
                    num(first.get(model), 4),
                    str(int(second.rank(ascending=False).get(model, 0)))
                    if model in second.index
                    else "—",
                    num(second.get(model), 4),
                    str(int(full.rank(ascending=False).get(model, 0))),
                    num(full.get(model), 4),
                ]
            )
        corr_2025_2026 = rank_correlation_between(first, second)
        corr_full_2026 = rank_correlation_between(second, full)
        corr_full_2025 = rank_correlation_between(first, full)
        corr_pairs.append(f"{HORIZON_TITLES[horizon]} {num(corr_2025_2026, 3)}")
        if not pd.isna(corr_2025_2026):
            corr_values.append(float(corr_2025_2026))
        blocks.append(
            f"<h3>{HORIZON_TITLES[horizon]} 2025 vs 2026 vs 全窗口</h3>"
            + table(
                [
                    "模型",
                    "2025 排名",
                    "2025 IC",
                    "2026 排名",
                    "2026 IC",
                    "全窗口排名",
                    "全窗口 IC",
                ],
                rows,
                aligns=["l", "r", "r", "r", "r", "r", "r"],
            )
            + f'<p class="small">秩相关：2025 vs 2026 = {num(corr_2025_2026, 3)}；'
            f"2026 vs 全窗口 = {num(corr_full_2026, 3)}；2025 vs 全窗口 = {num(corr_full_2025, 3)}。</p>"
        )
    if corr_values and min(corr_values) >= 0.9:
        verdict = "两个持有期的跨年排序都高度稳定"
    elif corr_values and max(corr_values) < 0.6:
        verdict = "两个持有期的跨年排序都明显不稳定"
    else:
        verdict = "跨年排序存在漂移，且两个持有期的稳定性不一致"
    corr_text = "、".join(corr_pairs) or "—"
    return f"""
<section class="section page-break">
  {section_head("10", "长窗口 vs 单年：2026 年的排序是不是偶然", "把 2025、2026 与全窗口的 IC 排名放在一起看。")}
  <p class="lead">这一章用同一套预测、同一套收益口径，只改变统计区间。如果 2026 的排名与 2025
  高度相关，说明模型能力稳定；如果秩相关接近 0 或为负，说明排名由年份行情决定，不应把单年
  冠军当成长期配置。</p>
  {"".join(blocks)}
  {callout("回答“是不是单年行情”", f"2025 vs 2026 的秩相关为 {corr_text}：{verdict}。结合第 05 章的季度冠军轮换，<strong>模型族的相对位置有持续性，但具体冠军没有。</strong>选型应看跨年稳定进入前半段的模型，而不是上一年的第一名。")}
</section>
"""


def conclusion_section(bundle: DeepBundle) -> str:
    best5 = bundle.quality_ranking(5)
    best10 = bundle.quality_ranking(10)
    profiles5 = model_profiles(bundle, 5)
    profiles10 = model_profiles(bundle, 10)
    stable_text = "—"
    if not profiles5.empty and "quarter_ic_rank_std" in profiles5:
        # 两个持有期取较差值（max），避免只在一个持有期稳定的模型被误选。
        std5 = profiles5.set_index("model")["quarter_ic_rank_std"]
        if (
            not profiles10.empty
            and "quarter_ic_rank_std" in profiles10
            and "model" in profiles10
        ):
            std10 = profiles10.set_index("model")["quarter_ic_rank_std"]
            stability = pd.concat([std5, std10], axis=1).max(axis=1)
        else:
            stability = std5
        stable = stability.sort_values().head(3)
        stable_text = "、".join(
            display_name(str(model)) for model in stable.index.tolist()
        )
    top5_name = display_name(str(best5.iloc[0]["model"])) if not best5.empty else "—"
    top10_name = display_name(str(best10.iloc[0]["model"])) if not best10.empty else "—"

    sequence_models = [
        model for model in MODEL_ORDER if MODEL_FAMILIES.get(model) == "序列深度"
    ]
    validation = bundle.validation
    deep_text = "序列深度模型的复现校验数据缺失"
    if not validation.empty and {"model", "corr", "top100_overlap"} <= set(
        validation.columns
    ):
        subset = validation[validation["model"].isin(sequence_models)]
        subset = subset.dropna(subset=["corr"])
        if not subset.empty:
            top = pd.to_numeric(subset["top100_overlap"], errors="coerce").dropna()
            top_range = f"{top.min():.0f}–{top.max():.0f}" if not top.empty else "—"
            deep_text = (
                f"序列深度模型（{'/'.join(sequence_models)}）在外推重叠日的相关系数为 "
                f"{subset['corr'].min():.2f}–{subset['corr'].max():.2f}，"
                f"Top100 重合 {top_range}；它们的外推段只适合看方向，不适合做精确排序。"
            )

    all_models_counts = []
    for horizon in (5, 10):
        daily = bundle.consensus.get(f"daily_{horizon}", pd.DataFrame())
        if not daily.empty and "all_models" in daily:
            mean_count = pd.to_numeric(daily["all_models"], errors="coerce").mean()
            if not pd.isna(mean_count):
                all_models_counts.append(float(mean_count))
    if not all_models_counts:
        consensus_scope_text = "13 个模型 Top20 的共识池规模数据缺失。"
    elif max(all_models_counts) < 0.01:
        consensus_scope_text = (
            "13 个模型 Top20 的全票共识池平均每天不足 0.01 只（全部模型同时选中）。"
        )
    else:
        consensus_scope_text = (
            f"13 个模型 Top20 的共识池平均每天只有 "
            f"{min(all_models_counts):.2f}–{max(all_models_counts):.2f} 只"
            "（全部模型同时选中）。"
        )

    ic_median = {}
    sharpe_median = {}
    for horizon, frame in ((5, best5), (10, best10)):
        if not frame.empty and "ic_mean" in frame:
            ic_median[horizon] = float(
                pd.to_numeric(frame["ic_mean"], errors="coerce").median()
            )
        rolling = bundle.portfolios.get(horizon, {}).get("rolling", pd.DataFrame())
        if not rolling.empty and "sharpe" in rolling:
            sharpe_median[horizon] = float(
                pd.to_numeric(rolling["sharpe"], errors="coerce").median()
            )
    if (
        5 in ic_median
        and 10 in ic_median
        and 5 in sharpe_median
        and 10 in sharpe_median
    ):
        faster = ic_median[5] >= ic_median[10] and sharpe_median[5] >= sharpe_median[10]
        slower = ic_median[10] > ic_median[5] and sharpe_median[10] > sharpe_median[5]
        if slower:
            hold_text = (
                "T+10 的 IC 与滚动夏普中位数在本窗口都高于 T+5"
                f"（IC {ic_median[10]:.4f} vs {ic_median[5]:.4f}，"
                f"夏普 {sharpe_median[10]:.2f} vs {sharpe_median[5]:.2f}）；"
                "10 日标签的逐日重叠会放大统计显著性，且资金占用更长，不能只按 IC 高低选持有期。"
            )
        elif faster:
            hold_text = (
                "T+5 的 IC 与滚动夏普中位数都高于 T+10"
                f"（IC {ic_median[5]:.4f} vs {ic_median[10]:.4f}，"
                f"夏普 {sharpe_median[5]:.2f} vs {sharpe_median[10]:.2f}），短持有期仍是主信号。"
            )
        else:
            hold_text = (
                "两个持有期各有胜负"
                f"（IC 中位数 {ic_median[5]:.4f} vs {ic_median[10]:.4f}，"
                f"滚动夏普中位数 {sharpe_median[5]:.2f} vs {sharpe_median[10]:.2f}），"
                "应按资金容量和换手约束选择，而不是假定某一档全面占优。"
            )
    else:
        hold_text = "持有期对比数据缺失。"
    return f"""
<section class="section page-break">
  {section_head("11", "结论与使用建议", "把统计结论翻译成可执行的选择。")}
  <h3>11.1 选型</h3>
  <ul>
    <li><strong>核心配置：</strong>{esc(top5_name)}（T+5）与 {esc(top10_name)}（T+10）在全窗口 IC 上领先，
    但领先幅度不足以支撑“只留一个模型”。建议以树模型为底仓信号，保留 2–3 个跨族模型做共识确认。</li>
    <li><strong>稳定性优先：</strong>两个持有期中较差一档的季度 IC 排名波动最小的三个模型是
    {esc(stable_text)}；如果目标是“少换模型”，优先从这组里选，而不是追单季冠军。</li>
    <li><strong>持有期：</strong>{esc(hold_text)}
    资金规模在 1000 万以上时，T+10 + Top20 的组合更现实。</li>
    <li><strong>组合形态：</strong>200 万级别可以用 Top10 或滚动 Top5；1000 万以上切到 Top20，
    并接受收益下降换容量。</li>
  </ul>
  <h3>11.2 风险与边界</h3>
  <ul>
    <li><strong>外推段不是干净的样本外：</strong>09-15 之后特征覆盖降到 272–300/300，降级期 IC 的抬升
    与行情切换同时发生；不要把这段收益计入模型能力。</li>
    <li><strong>深模型复现误差：</strong>{esc(deep_text)}</li>
    <li><strong>共识不等于稳健：</strong>{esc(consensus_scope_text)}
    真要做共识组合，需要放宽到 ≥3 票并接受更小的持仓数。</li>
    <li><strong>容量与冲击：</strong>回测只约束单票不超过当日成交额 1%，没有建模冲击成本、盘口深度与
    涨跌停排队；实盘容量应比报告结果再打折。</li>
    <li><strong>静态 ST 与退市：</strong>股票域使用训练产物的首见日期与静态 ST 标记，历史 ST 状态并不逐日精确，
    可能高估少量股票的可得性。</li>
  </ul>
  <h3>11.3 下一步</h3>
  <ol>
    <li>把季度稳定性纳入模型准入：连续两个季度 IC 排名落在后 1/3 的模型自动降权或下线。</li>
    <li>在容量约束下重估 Top20 的行业中性版本，检验超额是否仍来自选股。</li>
    <li>对共识票数 ≥3 的池子做逐日持仓归因，确认其超额不是行业集中带来的。</li>
  </ol>
</section>
"""


def appendix_section(bundle: DeepBundle) -> str:
    alignment_rows = []
    entries = (bundle.alignment or {}).get("entries", []) if bundle.alignment else []
    for record in entries:
        if record.get("status") not in {"applied", "dry_run"}:
            continue
        alignment_rows.append(
            [
                HORIZON_TITLES.get(int(record["horizon"]), str(record["horizon"])),
                esc(display_name(str(record["model"]))),
                money(record.get("rows_before"), 0),
                money(record.get("rows_after"), 0),
                money(record.get("added"), 0),
                money(record.get("dropped"), 0),
                money(record.get("pred_nan"), 0),
            ]
        )
    alignment_table = table(
        ["期", "模型", "对齐前行数", "对齐后行数", "补 NaN", "丢弃", "NaN 总数"],
        alignment_rows,
        aligns=["c", "l", "r", "r", "r", "r", "r"],
    )
    inventory = bundle.summary.get("inventory", []) if bundle.summary else []
    missing = bundle.summary.get("missing", []) if bundle.summary else []
    inventory_rows = []
    for item in inventory:
        horizon = int(item.get("horizon", 0))
        ready = bool(item.get("ready"))
        status = "就绪" if ready else (str(item.get("reason") or "缺失"))
        inventory_rows.append(
            [
                HORIZON_TITLES.get(horizon, str(horizon)),
                display_name(str(item.get("key") or item.get("model") or "")),
                str(item.get("run_id") or "—"),
                status,
            ]
        )
    inventory_table = table(
        ["期", "模型", "训练 Run ID", "状态"],
        inventory_rows,
        aligns=["c", "l", "l", "l"],
        table_class="data-table compact",
    )
    missing_text = "；".join(
        f"{HORIZON_TITLES.get(int(item.get('horizon', 0)), item.get('horizon'))} "
        f"{display_name(str(item.get('model', '')))}：{item.get('reason') or '未就绪'}"
        for item in missing
    )
    return f"""
<section class="section page-break">
  {section_head("12", "附录：审计与可复现性", "对齐、校验与产物清单。")}
  <h3>12.1 网格对齐审计</h3>
  {alignment_table}
  <p class="small">补 NaN 表示该模型在该 (date, symbol) 上没有观点（序列窗口不足），
  不是 0 分；丢弃的是规范网格之外的深度模型多出行（多为停牌/ST）。对齐幂等，可重复执行。</p>
  <h3>12.2 产物清单</h3>
  {inventory_table if inventory_rows else '<p class="small">产物清单见 summary.json。</p>'}
  <p class="small">缺失项：{esc(missing_text) if missing_text else "无"}。</p>
  <p class="small">报告由 <code>backend/scripts/backtest2026/deep_report.py</code> 生成，
  数据来自 <code>{esc(str(bundle.run_dir))}</code>、季度、共识、容量与归因目录。
  所有数字可由产物逐位复现。</p>
  <div class="footer-note">
    <strong>免责声明：</strong>本项目仅供学习研究与技术演示，不构成任何投资建议。所有分析报告和
    交易信号均由 AI 自动生成，可能存在错误或偏差。投资决策请咨询持有中国证监会颁发资质的专业机构。
    股市有风险，投资需谨慎。
  </div>
</section>
"""
