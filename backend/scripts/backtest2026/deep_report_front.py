"""长窗口深度评估报告：封面、摘要、方法、排行榜。"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from .deep_report_data import (
    EXTENSION_WINDOWS,
    DeepBundle,
    period_metrics,
    validation_table,
    year_slice_ranking,
)
from .model_registry import MODEL_FAMILIES, MODEL_ORDER
from .multi_report_data import HORIZON_TITLES, display_name
from .report_pdf_html import (
    callout,
    date_cn,
    esc,
    figure,
    money,
    num,
    pct,
    section_head,
    table,
)

FAMILIES = ("top10", "top20", "rolling")


def _charted(charts: dict[str, Path], key: str, caption: str, number: int) -> str:
    path = charts.get(key)
    return figure(path, caption, number) if path is not None else ""


def _manifest(bundle: DeepBundle) -> dict:
    return bundle.summary.get("manifest", {}) if bundle.summary else {}


def _signature(bundle: DeepBundle) -> dict:
    return (
        bundle.summary.get("panel", {}).get("signature", {}) if bundle.summary else {}
    )


def _stats(bundle: DeepBundle, horizon: int, family: str) -> dict:
    try:
        return bundle.summary["stats"][str(horizon)]["portfolios"][family]
    except (KeyError, TypeError):
        return {}


def _quality_stats(bundle: DeepBundle, horizon: int) -> dict:
    try:
        return bundle.summary["stats"][str(horizon)]["quality"]
    except (KeyError, TypeError):
        return {}


def _p_value(value: object, digits: int = 4) -> str:
    if value is None or pd.isna(value):
        return "—"
    number = float(value)
    if number < 10 ** (-digits):
        return f"{number:.1e}"
    return f"{number:.{digits}f}"


def _best(bundle: DeepBundle, horizon: int, field: str) -> tuple[str, float]:
    frame = bundle.quality_ranking(horizon)
    if frame.empty or field not in frame:
        return "—", float("nan")
    row = frame.iloc[0]
    return str(row["model"]), float(row[field])


def _portfolio_best(bundle: DeepBundle, horizon: int, family: str) -> tuple[str, float]:
    frame = bundle.portfolios.get(horizon, {}).get(family)
    if frame is None or frame.empty:
        return "—", float("nan")
    ordered = frame.sort_values("rank")
    row = ordered.iloc[0]
    return str(row["model"]), float(row["total_return"])


def cover(bundle: DeepBundle) -> str:
    manifest = _manifest(bundle)
    signature = _signature(bundle)
    best5, ic5 = _best(bundle, 5, "ic_mean")
    best10, ic10 = _best(bundle, 10, "ic_mean")
    best_port5, ret5 = _portfolio_best(bundle, 5, "rolling")
    best_port10, ret10 = _portfolio_best(bundle, 10, "rolling")
    benchmark = bundle.summary.get("benchmark", {})
    return f"""
<section class="cover">
  <div class="cover-band"></div>
  <div class="kicker">QuantMind Research · 长窗口多模型深度评估</div>
  <h1>13 个选股模型的长窗口<br>优势、短板与共识结构</h1>
  <p class="subtitle">2025-02 — 2026-10 · T+5 / T+10 · Top10 / Top20 / 滚动 Top5<br>
  外推补齐至 2026-10-16 · 分段控制特征覆盖 · 共识票数与容量约束</p>
  <p class="scope">
    评估窗口 {esc(manifest.get("common_start", "—"))} ~ {esc(manifest.get("common_end", "—"))} ·
    {esc(manifest.get("signal_days", "—"))} 个信号日 ·
    {money(signature.get("symbols", 0))} 只股票 ·
    每组合 {money(signature.get("rows", 0))} 条预测 ·
    基准 {pct(benchmark.get("total_return"), 2, signed=True)}
  </p>
  <div class="summary">
    <div class="summary-title">这份报告回答什么</div>
    <ul>
      <li><strong>把测试期从 2026 年拉长到 2025-02 起：</strong>同一套 26 组合协议，判断 2026 年的模型排序是不是单年行情。</li>
      <li><strong>每个模型的优势与短板：</strong>IC、ICIR、Top20 超额、组合夏普、最大回撤、季度排名稳定性、外推稳健性七个维度横向对比。</li>
      <li><strong>逐日分数能否再筛：</strong>Top10/20/50、z 分数、均值+1σ/2σ、前 1%，以及 13 模型共识票数分层。</li>
      <li><strong>Top20 里有多少是共识股：</strong>每个模型 Top20 与其余模型的逐日重叠、Jaccard、共识池内/池外收益、独有与共享选股的收益差。</li>
      <li><strong>长窗口冠军：</strong>T+5 IC 第一 {esc(display_name(best5))}（{num(ic5, 4)}），滚动 Top5 第一 {esc(display_name(best_port5))}（{pct(ret5, 1)}）；T+10 IC 第一 {esc(display_name(best10))}（{num(ic10, 4)}），滚动 Top5 第一 {esc(display_name(best_port10))}（{pct(ret10, 1)}）。</li>
    </ul>
  </div>
  <div class="metric-grid">
    <div class="metric"><div class="label">模型 × 持有期</div><div class="value">26</div><div class="sub">13 个模型 × 2 档</div></div>
    <div class="metric"><div class="label">信号日</div><div class="value">{esc(manifest.get("signal_days", "—"))}</div><div class="sub">训练产物 + 外推</div></div>
    <div class="metric"><div class="label">外推终点</div><div class="value">10-16</div><div class="sub">干净期至 09-14</div></div>
    <div class="metric"><div class="label">本金</div><div class="value">200 万</div><div class="sub">整手 100 股，含费用</div></div>
  </div>
  <div class="cover-foot">
    <div>数据来源：QuantDB 日线（不复权价判涨跌停，前复权价计收益）、26 个训练产物 pred.parquet、逐日外推预测。统计在同一闭区间内逐日配对完成。</div>
    <div>本报告仅供学习研究与技术演示，不构成投资建议。报告编号 QM-R-2026-DEEP-1019 · 生成于 {date_cn(pd.Timestamp.now())}</div>
  </div>
</section>
"""


def summary_section(bundle: DeepBundle, charts: dict[str, Path]) -> str:
    manifest = _manifest(bundle)
    signature = _signature(bundle)
    best5 = bundle.quality_ranking(5)
    best10 = bundle.quality_ranking(10)
    top5_ic = best5.iloc[0] if not best5.empty else None
    top10_ic = best10.iloc[0] if not best10.empty else None

    data_end = str(manifest.get("common_end") or "2100-01-01")
    champion_notes = []
    champion_compared = 0
    champion_matched = 0
    for horizon, best in ((5, best5), (10, best10)):
        if best.empty:
            continue
        full_name = str(best.iloc[0]["model"])
        single = year_slice_ranking(bundle, horizon, "2026-01-01", data_end)
        if single.empty or full_name not in single.index:
            continue
        champion_compared += 1
        single_name = str(single.index[0])
        if single_name == full_name:
            champion_matched += 1
            champion_notes.append(
                f"{HORIZON_TITLES[horizon]} 全窗口与 2026 单年同为 "
                f"{display_name(full_name)}"
            )
        else:
            champion_notes.append(
                f"{HORIZON_TITLES[horizon]} 全窗口为 {display_name(full_name)}、"
                f"2026 单年为 {display_name(single_name)}"
            )
    if not champion_compared:
        champion_match_text = "的对比"
    elif champion_matched == champion_compared:
        champion_match_text = "一致"
    elif champion_matched == 0:
        champion_match_text = "不同"
    else:
        champion_match_text = "部分一致"
    champion_detail = "；".join(champion_notes) + "。" if champion_notes else ""

    tree_models = [
        model for model in MODEL_ORDER if MODEL_FAMILIES.get(model) == "树模型"
    ]
    periods = {horizon: period_metrics(bundle, horizon) for horizon in (5, 10)}

    kendall_values: list[float] = []
    rc_p_values: list[float] = []
    for horizon in (5, 10):
        friedman_stat = (
            _quality_stats(bundle, horizon).get("friedman", {}).get("ic_eligible", {})
        )
        kendall = friedman_stat.get("kendall_w")
        if kendall is not None and not pd.isna(kendall):
            kendall_values.append(float(kendall))
        rc_p = _stats(bundle, horizon, "top10").get("white_reality_check", {}).get("p")
        if rc_p is not None and not pd.isna(rc_p):
            rc_p_values.append(float(rc_p))
    w_text = (
        f"{min(kendall_values):.3f}–{max(kendall_values):.3f}"
        if kendall_values
        else "—"
    )
    if not rc_p_values:
        rc_text = "White Reality Check 结果缺失"
    else:
        rc_range = f"（p = {min(rc_p_values):.3f}–{max(rc_p_values):.3f}）"
        if max(rc_p_values) < 0.05:
            rc_text = (
                f"White Reality Check 在两个持有期都拒绝「所有模型等价」{rc_range}"
            )
        elif min(rc_p_values) >= 0.05:
            rc_text = (
                f"White Reality Check 在两个持有期都不能拒绝「所有模型等价」{rc_range}"
            )
        else:
            rc_text = (
                f"White Reality Check 只在部分持有期拒绝「所有模型等价」{rc_range}"
            )

    degraded_parts = []
    for horizon in (5, 10):
        frame = periods[horizon]
        if frame.empty:
            continue
        subset = frame[
            (frame["period"] == "degraded") & frame["model"].isin(tree_models)
        ]
        if subset.empty:
            continue
        window = EXTENSION_WINDOWS[horizon]["degraded"]
        degraded_parts.append(
            f"{HORIZON_TITLES[horizon]} 树模型 IC "
            f"{subset['ic_mean'].min():.2f}–{subset['ic_mean'].max():.2f}、"
            f"Top20 日均超额 {subset['top20_excess'].mean() * 100:+.2f}%"
            f"（{window[0]} 至 {window[1]}）"
        )
    degraded_text = "；".join(degraded_parts) if degraded_parts else "样本缺失"

    consensus_parts = []
    consensus_lifts = []
    gap_values: list[float] = []
    for horizon in (5, 10):
        daily = bundle.consensus.get(f"daily_{horizon}", pd.DataFrame())
        distribution = bundle.consensus.get(f"distribution_{horizon}", pd.DataFrame())
        if not daily.empty and "avg_model_in_half" in daily:
            avg_in_half = pd.to_numeric(
                daily["avg_model_in_half"], errors="coerce"
            ).mean()
            half_share = float("nan")
            if not distribution.empty and "share" in distribution:
                half_rows = distribution[
                    pd.to_numeric(distribution["consensus"], errors="coerce") >= 7
                ]
                half_share = pd.to_numeric(half_rows["share"], errors="coerce").sum()
            if not pd.isna(avg_in_half):
                # avg_model_in_half 是「占该模型 Top20 的比例」，×20 换成只数。
                detail = f"{avg_in_half * 20:.2f} 只"
                if not pd.isna(half_share):
                    detail += f"（占全部股票-日的 {half_share * 100:.2f}%）"
                consensus_parts.append(f"{HORIZON_TITLES[horizon]} {detail}")
        if not distribution.empty and "mean_excess" in distribution:
            consensus = pd.to_numeric(distribution["consensus"], errors="coerce")
            excess = pd.to_numeric(distribution["mean_excess"], errors="coerce")
            share = pd.to_numeric(distribution["share"], errors="coerce")
            low = excess[consensus.isin([1, 2])].mean()
            mid_mask = (consensus >= 3) & (consensus <= 6)
            mid = excess[mid_mask].mean()
            mid_share = share[mid_mask].sum()
            if not pd.isna(low) and not pd.isna(mid):
                consensus_lifts.append(
                    f"{HORIZON_TITLES[horizon]} 1–2 票 {low * 100:+.2f}% vs "
                    f"3–6 票 {mid * 100:+.2f}%（后者仅占股票-日的 {mid_share * 100:.1f}%）"
                )
        overlap = bundle.consensus.get(f"overlap_{horizon}", pd.DataFrame())
        if not overlap.empty and {"unique_ret", "shared_ret"} <= set(overlap.columns):
            gap = (
                (
                    pd.to_numeric(overlap["shared_ret"], errors="coerce")
                    - pd.to_numeric(overlap["unique_ret"], errors="coerce")
                )
                .abs()
                .mean()
            )
            if not pd.isna(gap):
                gap_values.append(float(gap))
    consensus_text = "；".join(consensus_parts) if consensus_parts else "共识数据缺失"
    consensus_lift_text = (
        "；".join(consensus_lifts) if consensus_lifts else "共识票数分层样本缺失"
    )
    gap_text = f"{sum(gap_values) / len(gap_values) * 100:.2f}" if gap_values else "—"

    attribution = bundle.attribution
    attr_count = 0
    attr_total = 0
    lin_text = ""
    if not attribution.empty and {
        "model",
        "horizon",
        "selection_share",
        "industry_share",
        "size_share",
    } <= set(attribution.columns):
        numeric = attribution.copy()
        for column in ("selection_share", "industry_share", "size_share"):
            numeric[column] = pd.to_numeric(numeric[column], errors="coerce")
        dominant = (numeric["selection_share"] > numeric["industry_share"]) & (
            numeric["selection_share"] > numeric["size_share"]
        )
        attr_count = int(dominant.sum())
        attr_total = int(dominant.notna().sum())
        linear = numeric[
            (numeric["model"] == "LIN")
            & (pd.to_numeric(numeric["horizon"], errors="coerce") == 5)
        ]
        if not linear.empty:
            lin_text = (
                f"线性模型（T+5）的选股占比仅 "
                f"{linear['selection_share'].iloc[0] * 100:.0f}%、"
                f"市值占比 {linear['size_share'].iloc[0] * 100:.0f}%，"
            )
    attr_text = (
        f"在 {attr_count}/{attr_total} 个「模型 × 持有期」组合里，选股项贡献最大；"
        if attr_total
        else ""
    )

    advantage = bundle.capacity.get("advantage", pd.DataFrame())
    capacity_text = ""
    if not advantage.empty and {"capital", "participation", "mean_diff"} <= set(
        advantage.columns
    ):
        numeric = advantage.copy()
        numeric["capital"] = pd.to_numeric(numeric["capital"], errors="coerce")
        numeric["mean_diff"] = pd.to_numeric(numeric["mean_diff"], errors="coerce")
        participation = pd.to_numeric(numeric["participation"], errors="coerce")
        constrained = numeric[participation == 0.01]
        small = constrained[constrained["capital"] == 2_000_000]
        large = constrained[constrained["capital"] >= 10_000_000]
        if not large.empty:
            capacity_text = (
                f"200 万本金、1% 参与率时 Top10−Top20 的平均收益差仅 "
                f"{small['mean_diff'].min() * 100:+.1f} 至 "
                f"{small['mean_diff'].max() * 100:+.1f} 个百分点；"
                f"资金 ≥1000 万时扩大到 "
                f"{large['mean_diff'].min() * 100:+.1f} 至 "
                f"{large['mean_diff'].max() * 100:+.1f} 个百分点"
                + ("，全部为负" if (large["mean_diff"] < 0).all() else "")
                + "。"
            )

    stats_rows = []
    for horizon in (5, 10):
        friedman = (
            _quality_stats(bundle, horizon).get("friedman", {}).get("ic_eligible", {})
        )
        white = _stats(bundle, horizon, "top10").get("white_reality_check", {})
        pbo = _stats(bundle, horizon, "top10").get("pbo", {})
        stats_rows.append(
            [
                HORIZON_TITLES[horizon],
                num(friedman.get("chi2"), 1),
                _p_value(friedman.get("p")),
                num(friedman.get("kendall_w"), 4),
                esc(str(white.get("best_model", "—"))),
                _p_value(white.get("p"), 3),
                num(pbo.get("pbo"), 3),
            ]
        )

    ranking_rows = []
    for horizon, frame in ((5, best5), (10, best10)):
        if frame.empty:
            continue
        for record in frame.head(5).to_dict("records"):
            ranking_rows.append(
                [
                    HORIZON_TITLES[horizon],
                    str(int(record["rank"])),
                    esc(display_name(str(record["model"]))),
                    num(record["ic_mean"], 4),
                    num(record["icir"], 3),
                    pct(record["top20_hit"], 1),
                    pct(record["top20_ret"], 3, signed=True),
                    num(record["top_bottom_spread"], 4),
                ]
            )

    stats_table = table(
        ["持有期", "Friedman χ²", "p", "Kendall W", "White RC 最优", "RC p", "PBO"],
        stats_rows,
    )
    top_table = table(
        [
            "期",
            "排名",
            "模型",
            "IC 均值",
            "ICIR",
            "Top20 上涨比",
            "Top20 日均收益",
            "多空价差",
        ],
        ranking_rows,
        aligns=["c", "r", "l", "r", "r", "r", "r", "r"],
    )

    return f"""
<section class="section page-break">
  {section_head("01", "执行摘要", "先把结论摆出来，再用数据逐项验证。")}
  <p class="lead">测试期从原先的 2026-01 拉长到 {esc(manifest.get("common_start", "—"))} ~
  {esc(manifest.get("common_end", "—"))}，并把推理外推补齐到 2026-10-16。全窗口共
  {esc(manifest.get("signal_days", "—"))} 个信号日、{money(signature.get("rows", 0))} 条逐日预测。
  外推区间按特征覆盖质量拆成「干净期」与「降级期」，所有结论都分段复核，避免把特征缺失日的
  高 IC 当成模型能力。</p>

  <h3>核心发现的六条</h3>
  <div class="callout">
    <ol>
      <li><strong>长窗口冠军与 2026 单年{champion_match_text}，但差距仍小。</strong>
      T+5 IC 第一为 {esc(display_name(str(top5_ic["model"]))) if top5_ic is not None else "—"}
      （{num(top5_ic["ic_mean"], 4) if top5_ic is not None else "—"}），
      T+10 IC 第一为 {esc(display_name(str(top10_ic["model"]))) if top10_ic is not None else "—"}
      （{num(top10_ic["ic_mean"], 4) if top10_ic is not None else "—"}）。
      {esc(champion_detail)}模型间 IC 差距小于季度之间的波动，单年冠军不能外推。</li>
      <li><strong>模型差异在统计上显著，但可解释方差很小。</strong>
      Friedman 检验的 Kendall W 只在 {w_text} 量级；{rc_text}（见下表）。这说明选对
      「一类模型」比选对「某个模型」重要。</li>
      <li><strong>2026-09 中旬之后的外推段更像一次行情切换，而不是模型变强。</strong>
      降级期{degraded_text}；这一段只适合做稳健性观察，不能计入样本外业绩。</li>
      <li><strong>Top20 的共识度很低。</strong>
      每个模型 Top20 中同时进入半数模型（≥7 个）Top20 的股票平均只有 {consensus_text}。
      共享与独有选股收益的平均绝对差仅 {gap_text} 个百分点；{consensus_lift_text}。</li>
      <li><strong>容量约束会改写 Top10 与 Top20 的相对优劣。</strong>
      {capacity_text if capacity_text else "容量扫描结果缺失。"}
      原因是 Top10 更集中、更容易撞上成交额上限。</li>
      <li><strong>超额的主要来源是选股，不是行业或市值暴露。</strong>
      滚动 Top5 的归因中，{attr_text}{lin_text}这也是线性模型 IC 不低但组合表现偏弱的原因。</li>
    </ol>
  </div>

  {stats_table}
  <p class="small">Friedman 基于逐日 IC 的模型间配对秩；White RC 与 PBO 基于滚动 Top5 组合收益
  （n_boot=5000，n_trials=26）。PBO 越接近 0.5 越说明「事后选出的冠军」不可复现。</p>

  <h3>IC 前五名（全窗口）</h3>
  {top_table}
  {_charted(charts, "quality", "T+5 与 T+10 的 IC 均值、Top20 日均收益对比；点越靠右越强。", 1)}
</section>
"""


def method_section(bundle: DeepBundle, charts: dict[str, Path]) -> str:
    manifest = _manifest(bundle)
    validation_rows = []
    for horizon in (5, 10):
        frame = validation_table(bundle, horizon)
        if frame.empty:
            continue
        for record in frame.to_dict("records"):
            validation_rows.append(
                [
                    HORIZON_TITLES[horizon],
                    esc(display_name(str(record["model"]))),
                    money(record.get("overlap_rows"), 0),
                    num(record.get("corr"), 4),
                    num(record.get("rank_corr"), 4),
                    num(record.get("max_abs_diff"), 5),
                    esc(str(record.get("top100_overlap", "—"))),
                ]
            )
    validation_table_html = table(
        ["期", "模型", "重叠行", "相关系数", "秩相关", "最大绝对差", "Top100 重合"],
        validation_rows,
    )

    alignment = bundle.alignment or {}
    totals = alignment.get("totals", {}) if isinstance(alignment, dict) else {}
    alignment_note = (
        f"网格 {money(alignment.get('grid_rows'), 0)} 个（日期 × 股票）、"
        f"{alignment.get('grid_days', '—')} 个交易日；"
        f"{totals.get('files', 0)} 个组合完成对齐，补 NaN {money(totals.get('pred_nan'), 0)} 行、"
        f"丢弃网格外 {money(totals.get('dropped'), 0)} 行。"
        if alignment
        else "未找到对齐审计报告。"
    )

    return f"""
<section class="section page-break">
  {section_head("02", "数据、外推与口径", "外推补齐到 10 月之后，先证明新增数据没有改变模型定义。")}
  <h3>2.1 时间轴</h3>
  <p>训练切分保持原协议：train 2016-01-04 至 2023-06-18，valid 2023-06-29 至 2025-01-21，
  test 2025-02-01 起。测试期原始产物覆盖到 2026-08-26/27；本次把每个组合的预测从各自
  产物最后一天之后逐日外推到 2026-10-16，评估截止取 <strong>2026-10-08</strong>（T+5 的
  完整前向收益需要 5 个交易日；T+10 的质量统计自然截止到 2026-09-23）。</p>
  <p>外推不是重新训练：逐日读取当天可得特征，加载同一份模型产物做推理。推理模板本轮修了两处
  与训练端不一致的地方——把 ±inf 当缺失、对 linear/MLP/TabNet 施加训练端同款 ±1e14 裁剪；
  树模型与序列模型保持原始口径。</p>

  <h3>2.2 推理复现校验</h3>
  <p>在每个模型产物最后一天（与训练环境重叠的一天）重跑推理，对比产物里的 pred：
  相关系数接近 1、Top100 重合度高，才允许把该模型的外推预测放进跨模型比较。
  下表是 26 个组合的校验结果。序列深度模型没有做到逐位复现（窗口读取路径与单日路径不同），
  它们的外推段结论只作方向参考。</p>
  {validation_table_html}

  <h3>2.3 股票域对齐</h3>
  <p>树/线性模型在单日路径上先做不可交易过滤（停牌、ST、次新），序列模型走多日窗口路径，
  会多出停牌股、丢掉窗口不足的股票。跨模型配对检验要求同一交易日、同一批股票，
  因此先把 26 个组合的外推面板对齐到 LGB T+5 的规范网格：网格外的行丢弃，网格内缺失的
  (date, symbol) 补 <code>pred=NaN</code>（该模型当日无观点，不冒充 0 分）。{alignment_note}</p>

  <h3>2.4 外推分段</h3>
  <p>2026-09-15 起，当天可得的 300 维特征开始大面积缺失（272–300/300），而 08-28 至 09-14
  基本完整。两段的 IC 水平差异很大，因此报告把外推拆开：<strong>干净期</strong>
  （T+5：08-28 至 09-14；T+10：08-27 至 09-14）与<strong>降级期</strong>
  （T+5：09-15 至 10-08；T+10：09-15 至 09-23）。</p>

  <h3>2.5 回测口径</h3>
  <p>信号日 t 收盘出分，t+1 收盘买入，t+1+H 收盘卖出；整手 100 股；双边费用 3bp；
  初始资金 200 万元；剔除 ST、涨跌停、停牌与上市不足 60 个交易日的股票。基准为全市场等权
  可交易池，不加任何风格约束。所有统计在同一闭区间逐日配对，重叠样本用
  Newey-West（lag=H−1）修正。</p>
  {callout("读法提醒", "外推段（尤其是降级期）不是严格的样本外业绩：特征覆盖不完整、行情也处在切换期。本报告把它们用于<strong>稳健性观察</strong>，主排名仍以训练产物覆盖期（2025-02 至 2026-08-26）为准。")}
  <p class="small">产物目录：{esc(str(bundle.run_dir))}。基准区间：
  {esc(manifest.get("common_start", "—"))} ~ {esc(manifest.get("common_end", "—"))}。</p>
</section>
"""


def leaderboard_section(bundle: DeepBundle, charts: dict[str, Path]) -> str:
    quality_tables = []
    for horizon in (5, 10):
        frame = bundle.quality_ranking(horizon)
        if frame.empty:
            continue
        rows = []
        for record in frame.to_dict("records"):
            rows.append(
                [
                    str(int(record["rank"])),
                    esc(display_name(str(record["model"]))),
                    num(record["ic_mean"], 4),
                    num(record["icir"], 3),
                    num(record["nw_t"], 2),
                    pct(record["ic_positive_ratio"], 1),
                    pct(record["top20_hit"], 1),
                    pct(record["top20_ret"], 3, signed=True),
                    num(record["top_bottom_spread"], 4),
                    num(record["decile_monotonicity"], 3),
                ]
            )
        quality_tables.append(
            f"<h3>{HORIZON_TITLES[horizon]} 质量榜</h3>"
            + table(
                [
                    "排名",
                    "模型",
                    "IC 均值",
                    "ICIR",
                    "NW t",
                    "IC 为正比例",
                    "Top20 上涨比",
                    "Top20 日均收益",
                    "多空价差",
                    "分层单调性",
                ],
                rows,
                aligns=["r", "l", "r", "r", "r", "r", "r", "r", "r", "r"],
            )
        )

    portfolio_rows = []
    for horizon in (5, 10):
        frame = bundle.portfolios.get(horizon, {}).get("rolling")
        if frame is None or frame.empty:
            continue
        ordered = frame.sort_values("rank")
        for record in ordered.to_dict("records"):
            portfolio_rows.append(
                [
                    HORIZON_TITLES[horizon],
                    str(int(record["rank"])),
                    esc(display_name(str(record["model"]))),
                    pct(record["total_return"], 1, signed=True),
                    pct(record["annual_return"], 1, signed=True),
                    num(record["sharpe"], 2),
                    pct(record["max_drawdown"], 1),
                    num(record["calmar"], 2),
                    num(record["information_ratio"], 2),
                    str(int(record["n_round_trips"]))
                    if pd.notna(record.get("n_round_trips"))
                    else "—",
                ]
            )
    portfolio_table = table(
        [
            "期",
            "排名",
            "模型",
            "累计收益",
            "年化",
            "夏普",
            "最大回撤",
            "Calmar",
            "信息比率",
            "回合数",
        ],
        portfolio_rows,
        aligns=["c", "r", "l", "r", "r", "r", "r", "r", "r", "r"],
    )

    family_rows = []
    for horizon in (5, 10):
        frame = bundle.quality_ranking(horizon)
        if frame.empty:
            continue
        for family in ("树模型", "线性模型", "浅层网络", "表格深度", "序列深度"):
            subset = frame[frame["model"].map(MODEL_FAMILIES) == family]
            if subset.empty:
                continue
            family_rows.append(
                [
                    HORIZON_TITLES[horizon],
                    esc(family),
                    str(len(subset)),
                    num(subset["ic_mean"].mean(), 4),
                    num(subset["ic_mean"].max(), 4),
                    num(subset["icir"].mean(), 3),
                ]
            )
    family_table = table(
        ["期", "模型族", "数量", "族内 IC 均值", "族内最高 IC", "族内平均 ICIR"],
        family_rows,
        aligns=["c", "l", "r", "r", "r", "r"],
    )

    return f"""
<section class="section page-break">
  {section_head("03", "全窗口排行榜", "先看单模型质量，再看组合表现；两者排序不一致本身就是信息。")}
  <p class="lead">质量榜回答「分数排序对不对」，组合榜回答「这个排序能不能赚钱」。两者都按
  同一批信号日、同一批可交易股票计算。IC 是逐日 Spearman 秩相关，NW t 用 lag=H−1
  的重叠样本修正；多空价差为当日 Top20 减 Bottom20 的平均收益。</p>
  {"".join(quality_tables)}
  {_charted(charts, "quality", "T+5 与 T+10 的 IC 均值与 Top20 日均收益。", 1)}

  <h3>滚动 Top5 组合榜</h3>
  {portfolio_table}
  {_charted(charts, "equity_5", "T+5 滚动 Top5 净值曲线；灰线为其余模型，虚线为全市场等权基准。", 2)}
  {_charted(charts, "equity_10", "T+10 滚动 Top5 净值曲线。", 3)}

  <h3>按模型族汇总</h3>
  {family_table}
  {callout("先看这里", "树模型（LightGBM/XGBoost/CatBoost/随机森林）在 T+5 上的 IC 与组合表现整体靠前；序列深度模型内部差异比模型族之间更大——同族里既有前五名，也有末位。选型时先排除明显失效的族，再在族内选稳定者。")}
</section>
"""
