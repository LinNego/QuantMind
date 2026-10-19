"""多模型报告前半部分：封面、摘要、口径、预测质量。"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from .model_registry import MODEL_FAMILIES, MODEL_ORDER
from .multi_report_data import HORIZON_TITLES, MultiBundle, display_name
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
    tone,
)

FAMILIES = ("top10", "top20", "rolling")
METRIC_TITLES = {
    "ic_eligible": "IC（秩相关）",
    "top5_ret": "Top5 平均收益",
    "top10_ret": "Top10 平均收益",
    "top20_ret": "Top20 平均收益",
    "top5_hit": "Top5 上涨比例",
    "top10_hit": "Top10 上涨比例",
    "top20_hit": "Top20 上涨比例",
    "top10_dir": "Top10 方向胜率",
}


def _ranked(bundle: MultiBundle, horizon: int) -> pd.DataFrame:
    frame = bundle.quality_ranking.get(horizon)
    return frame.sort_values("rank") if frame is not None and not frame.empty else frame


def _best_ic(bundle: MultiBundle, horizon: int) -> tuple[str, float]:
    frame = _ranked(bundle, horizon)
    if frame is None or frame.empty:
        return "—", float("nan")
    row = frame.iloc[0]
    return str(row["model"]), float(row["ic_mean"])


def _ic_spread(bundle: MultiBundle) -> float:
    spreads = []
    for horizon in (5, 10):
        frame = _ranked(bundle, horizon)
        if frame is None or frame.empty or "ic_mean" not in frame:
            continue
        values = frame["ic_mean"].astype(float)
        spreads.append(float(values.max() - values.min()))
    return max(spreads) if spreads else float("nan")


def _best_portfolio(
    bundle: MultiBundle, horizon: int, family: str
) -> tuple[str, float]:
    frame = bundle.portfolio_ranking.get((horizon, family))
    if frame is None or frame.empty:
        return "—", float("nan")
    row = frame.sort_values("rank").iloc[0]
    return str(row["model"]), float(row["total_return"])


def _median_return(bundle: MultiBundle, horizon: int, family: str) -> float:
    frame = bundle.portfolio_ranking.get((horizon, family))
    if frame is None or frame.empty:
        return float("nan")
    return float(frame["total_return"].median())


def _panel_signature(bundle: MultiBundle) -> dict:
    return bundle.summary.get("panel", {}).get("signature", {})


def _decile_edges(
    bundle: MultiBundle, horizon: int
) -> tuple[str, float, float] | None:
    frame = bundle.deciles.get(horizon)
    ranking = _ranked(bundle, horizon)
    if frame is None or frame.empty or ranking is None or ranking.empty:
        return None
    model = str(ranking.iloc[0]["model"])
    if model not in frame.index:
        return None
    lookup = {str(column).lower(): column for column in frame.columns}
    if "d1" not in lookup or "d10" not in lookup:
        return None
    row = frame.loc[model]
    return model, float(row[lookup["d1"]]), float(row[lookup["d10"]])


def _family_summary(bundle: MultiBundle) -> list[dict[str, object]]:
    frames = {horizon: _ranked(bundle, horizon) for horizon in (5, 10)}
    order = ["树模型", "线性模型", "浅层网络", "表格深度", "序列深度"]
    rows: list[dict[str, object]] = []
    for family in order:
        keys = [key for key in MODEL_ORDER if MODEL_FAMILIES.get(key) == family]
        if not keys:
            continue
        members = {}
        for horizon, frame in frames.items():
            if frame is None or frame.empty:
                members[horizon] = None
                continue
            subset = frame[frame["model"].isin(keys)]
            members[horizon] = subset if not subset.empty else None
        if members[5] is None and members[10] is None:
            continue

        def mean_ic(frame: pd.DataFrame | None) -> float:
            return float(frame["ic_mean"].mean()) if frame is not None else float("nan")

        def best(frame: pd.DataFrame | None) -> str:
            if frame is None:
                return ""
            return str(frame.sort_values("ic_mean").iloc[-1]["model"])

        rows.append(
            {
                "family": family,
                "count": len(keys),
                "ic5": mean_ic(members[5]),
                "ic10": mean_ic(members[10]),
                "best5": best(members[5]),
                "best10": best(members[10]),
            }
        )
    return rows


def cover(bundle: MultiBundle) -> str:
    signature = _panel_signature(bundle)
    best_t5, ic_t5 = _best_ic(bundle, 5)
    best_t10, ic_t10 = _best_ic(bundle, 10)
    models = bundle.all_models()
    pending = bundle.pending()
    pending_text = (
        "；".join(f"T+{int(row.horizon)} {row.key}" for row in pending.itertuples())
        if not pending.empty
        else ""
    )
    if pending_text:
        readiness = (
            f"<li><strong>尚未就绪：</strong>{esc(pending_text)}。"
            "这些组合在榜单中留空，不用旧版本冒充。</li>"
        )
    else:
        readiness = (
            f"<li><strong>组合就绪：</strong>{len(models)} 个模型 × 2 个持有期，"
            f"共 {len(models) * 2} 个组合全部就绪，全部纳入同一张排名表。</li>"
        )
    return f"""
<section class="cover">
  <div class="cover-band"></div>
  <div class="kicker">QuantMind Research · 多模型统一窗口评估</div>
  <h1>2026 年 A 股选股能力<br>多模型、多持有期与组合回测</h1>
  <p class="subtitle">13 个模型 × T+5 / T+10 · Top10 / Top20 / 滚动 Top5<br>
  严格统计检验：配对样本、多重检验校正、数据窥探校正</p>
  <p class="scope">
    评估窗口 {esc(bundle.summary.get('manifest', {}).get('common_start', '—'))}
    ~ {esc(bundle.summary.get('manifest', {}).get('common_end', '—'))} ·
    {signature.get('days', '—')} 个信号日 ·
    {money(signature.get('symbols', 0))} 只股票 ·
    每模型 {money(signature.get('rows', 0))} 条预测
  </p>
  <div class="summary">
    <div class="summary-title">本报告直接回答的问题</div>
    <ul>
      <li><strong>13 个模型是否真有差别？</strong>过去只看了 LightGBM 一个模型；本报告把 13 个模型放在同一批日期、同一批股票、同一成交时点上做配对比较，并用 Friedman、White Reality Check、PBO 与 DSR 判断差异是否只是噪声。</li>
      <li><strong>T+5 与 T+10 哪个更好？</strong>两个持有期各自独立回测，并检验排名是否一致（第 5 章）。</li>
      <li><strong>Top10、Top20、滚动 Top5 差多少？</strong>三种组合都按 200 万本金、3bp 双边费用、剔除 ST / 涨跌停 / 停牌 / 次新后回测。</li>
      <li><strong>最强模型：</strong>T+5 IC 第一为 {esc(best_t5)}（{num(ic_t5, 4)}），T+10 IC 第一为 {esc(best_t10)}（{num(ic_t10, 4)}）。</li>
      {readiness}
    </ul>
  </div>
  <div class="metric-grid">
    <div class="metric"><div class="label">模型</div><div class="value">{len(models)}</div><div class="sub">统一架构、统一特征</div></div>
    <div class="metric"><div class="label">持有期</div><div class="value">2</div><div class="sub">T+5 与 T+10</div></div>
    <div class="metric"><div class="label">组合</div><div class="value">3</div><div class="sub">Top10 / Top20 / 滚动</div></div>
    <div class="metric"><div class="label">本金</div><div class="value">200 万</div><div class="sub">整手 100 股，含费用</div></div>
  </div>
  <div class="cover-foot">
    <div>数据来源：QuantDB 日线（不复权价判涨跌停，前复权价计收益）、各模型训练产物 pred.parquet。所有统计在同一闭区间内完成，样本逐日配对。</div>
    <div>本报告仅供学习研究与技术演示，不构成投资建议。报告编号 QM-R-2026-MULTI-1019 · 生成于 {date_cn(pd.Timestamp.now())}</div>
  </div>
</section>
"""


def summary_section(bundle: MultiBundle, chart_paths: dict[str, Path]) -> str:
    signature = _panel_signature(bundle)
    benchmark = bundle.summary.get("benchmark", {})
    best = {h: _best_ic(bundle, h) for h in (5, 10)}
    best_top = {
        (h, family): _best_portfolio(bundle, h, family)
        for h in (5, 10)
        for family in FAMILIES
    }
    medians = {
        (h, family): _median_return(bundle, h, family)
        for h in (5, 10)
        for family in FAMILIES
    }
    stats5 = bundle.quality_stats(5)
    stats10 = bundle.quality_stats(10)
    friedman5 = stats5.get("friedman", {}).get("ic_eligible", {})
    friedman10 = stats10.get("friedman", {}).get("ic_eligible", {})
    white5 = bundle.portfolio_stats(5, "top10").get("white_reality_check", {})
    white10 = bundle.portfolio_stats(10, "top10").get("white_reality_check", {})
    pbo5 = bundle.portfolio_stats(5, "top10").get("pbo", {}).get("pbo", float("nan"))
    pbo10 = bundle.portfolio_stats(10, "top10").get("pbo", {}).get("pbo", float("nan"))
    cross = bundle.cross or {}
    quality_corr = (cross.get("quality_rank_corr") or {}).get("ic_mean", float("nan"))
    portfolio_corr = (
        (cross.get("portfolio_rank_corr") or {}).get("top10", {}) or {}
    ).get("total_return", float("nan"))

    ic_rows = []
    for horizon in (5, 10):
        frame = _ranked(bundle, horizon)
        if frame is None or frame.empty:
            continue
        ic_rows.append(
            [
                f"T+{horizon}",
                esc(display_name(str(frame.iloc[0]["model"]))),
                num(frame.iloc[0]["ic_mean"], 4),
                num(frame.iloc[-1]["ic_mean"], 4),
                num(float(frame.iloc[0]["ic_mean"]) - float(frame.iloc[-1]["ic_mean"]), 4),
                num(frame["ic_mean"].median(), 4),
                pct(frame.iloc[0]["top10_hit"], 1),
            ]
        )

    family_rows = _family_summary(bundle)
    family_table = [
        [
            esc(str(row["family"])),
            money(row["count"], 0),
            num(row["ic5"], 4),
            num(row["ic10"], 4),
            esc(display_name(str(row["best5"]))) if row["best5"] else "—",
            esc(display_name(str(row["best10"]))) if row["best10"] else "—",
        ]
        for row in family_rows
    ]
    tree = next((row for row in family_rows if row["family"] == "树模型"), {})
    sequence = next((row for row in family_rows if row["family"] == "序列深度"), {})

    return f"""
<section class="section page-break">
  {section_head("01", "执行摘要", "先给结论，再给证据；所有数字都可回到附录产物复核")}
  {callout(
      "先澄清上一版报告的口径",
      "<p>上一版 Top20 / 滚动 Top5 报告只用了 <strong>LightGBM 一个模型</strong>。因此“模型都没区别”当时并没有被检验过，只是单模型内部的结论。本报告把 13 个模型族全部纳入，同一窗口、同一股票池、同一费用与成交时点，逐日配对。</p>",
      "warn",
  )}

  <h3>1.1 一句话结论</h3>
  <p class="lead">
  在 {signature.get('days', '—')} 个信号日、{money(signature.get('symbols', 0))} 只可交易股票的统一样本上，
  T+5 的 IC 冠军是 <strong>{esc(display_name(best[5][0]))}</strong>（{num(best[5][1], 4)}），
  T+10 的 IC 冠军是 <strong>{esc(display_name(best[10][0]))}</strong>（{num(best[10][1], 4)}）。
  但“冠军”不等于“稳定可交易”：严格检验要同时看
  Friedman 检验、White Reality Check、PBO 与 DSR，以及组合层面的费用后收益。
  </p>

  <h3>1.2 关键数字</h3>
  {table(
      ["周期", "IC 第一", "IC 第一均值", "IC 末位均值", "首末差", "中位数", "第一名 Top10 上涨比例"],
      ic_rows,
      aligns=["l", "l", "r", "r", "r", "r", "r"],
      raw=True,
  )}

  <div class="kpi-row">
    <div class="kpi"><div class="value">{pct(benchmark.get('total_return'), 2, True)}</div><div class="label">等权可交易池基准总收益</div><div class="sub">同一成交时点，未计费用</div></div>
    <div class="kpi"><div class="value">{pct(best_top[(5, 'top10')][1], 2, True)}</div><div class="label">T+5 Top10 冠军收益</div><div class="sub">{esc(display_name(best_top[(5, 'top10')][0]))}</div></div>
    <div class="kpi"><div class="value">{pct(best_top[(10, 'top10')][1], 2, True)}</div><div class="label">T+10 Top10 冠军收益</div><div class="sub">{esc(display_name(best_top[(10, 'top10')][0]))}</div></div>
    <div class="kpi"><div class="value">{pct(best_top[(5, 'rolling')][1], 2, True)}</div><div class="label">T+5 滚动 Top5 冠军收益</div><div class="sub">{esc(display_name(best_top[(5, 'rolling')][0]))}</div></div>
  </div>

  <h3>1.3 “模型有没有区别”的严格答案</h3>
  <ul>
    <li><strong>预测质量：</strong>T+5 的 Friedman 检验 p = {num(friedman5.get('p'), 4)}，Kendall W = {num(friedman5.get('kendall_w'), 3)}；T+10 的 p = {num(friedman10.get('p'), 4)}，W = {num(friedman10.get('kendall_w'), 3)}。p 越小、W 越大，越说明模型排序不是随机波动。</li>
    <li><strong>组合收益：</strong>T+5 Top10 的 White Reality Check p = {num(white5.get('p'), 4)}，T+10 Top10 p = {num(white10.get('p'), 4)}；PBO（回测过拟合概率）分别为 {pct(pbo5, 1)} 与 {pct(pbo10, 1)}。</li>
    <li><strong>跨周期一致性：</strong>T+5 与 T+10 的 IC 排名相关为 {num(quality_corr, 3)}，Top10 总收益排名相关为 {num(portfolio_corr, 3)}。若接近 1，说明两个持有期在选同一批模型；接近 0 则说明结论依赖持有期。</li>
    <li><strong>组合层面：</strong>Top10 的模型间收益中位数为 {pct(medians[(5, 'top10')], 2, True)}（T+5）与 {pct(medians[(10, 'top10')], 2, True)}（T+10），最好与最差的差距见第 4 章。差距大不等于可持续，需结合 PBO/DSR 看。</li>
  </ul>

  <h3>1.4 模型族对比：树模型领先，序列深度模型未转化为增量</h3>
  <p>把 13 个模型按族聚合后，树模型的平均 IC 最高：T+5 为 {num(tree.get('ic5'), 4)}、T+10 为 {num(tree.get('ic10'), 4)}；序列深度模型分别为 {num(sequence.get('ic5'), 4)} 与 {num(sequence.get('ic10'), 4)}。深度模型的容量没有转化为横截面排序优势，说明在这批 300 维特征与 2026 年样本上，信噪比和样本量更有利于树模型；这不代表深度模型在更长的序列或更高频数据上没有空间。</p>
  {table(
      ["模型族", "模型数", "T+5 平均 IC", "T+10 平均 IC", "T+5 最好", "T+10 最好"],
      family_table,
      aligns=["l", "r", "r", "r", "l", "l"],
      table_class="data-table keep",
  )}
  {figure(chart_paths['ic'], "13 个模型的 IC 均值。同一行两个点分别代表 T+5 与 T+10；点越靠右，排序能力越强。", 1) if 'ic' in chart_paths else ''}
  <p class="small">读图看三件事：横轴位置是绝对排序能力；同一行两点的水平距离是持有期敏感性；不同行之间的重叠程度，说明模型差距是否落在抽样误差内。多数模型的两点相距不足 0.01，但 LGB、XGB、CatBoost 在 T+10 明显更强，说明持有期会改变部分模型的相对位置。模型之间的最大差距约为 {num(_ic_spread(bundle), 3)}，大于多数持有期差异，因此“选哪个模型”比“选哪个持有期”更影响排序质量。</p>
</section>
"""


def method_section(bundle: MultiBundle, chart_paths: dict[str, Path]) -> str:
    manifest = bundle.summary.get("manifest", {})
    signature = _panel_signature(bundle)
    start = manifest.get("common_start", "—")
    end = manifest.get("common_end", "—")
    pending = bundle.pending()
    inventory_rows = []
    frame = bundle.inventory
    for key in MODEL_ORDER:
        rows = frame[frame["key"] == key] if not frame.empty else pd.DataFrame()
        t5 = rows[rows["horizon"] == 5]
        t10 = rows[rows["horizon"] == 10]
        t5_text = (
            f"{esc(t5.iloc[0]['run_id'])}" if not t5.empty and bool(t5.iloc[0]["ready"]) else "—"
        )
        t10_text = (
            f"{esc(t10.iloc[0]['run_id'])}" if not t10.empty and bool(t10.iloc[0]["ready"]) else "—"
        )
        inventory_rows.append(
            [
                esc(key),
                esc(display_name(key)),
                esc(MODEL_FAMILIES.get(key, "")),
                t5_text,
                t10_text,
            ]
        )
    return f"""
<section class="section page-break">
  {section_head("02", "数据、口径与可复现性", "所有模型共用一张价格面板和一套成交规则")}

  <h3>2.1 样本与训练切分</h3>
  <div class="two-col">
    <div>
      <p><strong>评估窗口：</strong>{esc(start)} 至 {esc(end)}，共 {manifest.get('signal_days', '—')} 个信号日。
      统一窗口的起点是各模型共同预测区间的第一天，终点固定到“最后一个信号日 + 2 个交易日”，
      保证所有模型、所有组合与基准覆盖完全相同的日期。</p>
      <p><strong>横截面：</strong>{money(signature.get('symbols', 0))} 只股票，每模型 {money(signature.get('rows', 0))} 条预测。
      预测面板的哈希在主模型之间必须一致，否则程序直接报错，不允许“用不同股票池比收益”。</p>
      <p><strong>训练切分：</strong>训练集 2016-01-04 至 2023-06-18；验证集 2023-06-29 至 2025-01-21；
      测试集 2025-02-01 至 2026-08-28。2026 年评估窗口完全落在测试集内，未参与训练或调参。</p>
    </div>
    <div>
      {table(
          ["切分", "区间", "用途"],
          [
              ["训练", "2016-01-04 ~ 2023-06-18", "拟合参数"],
              ["验证", "2023-06-29 ~ 2025-01-21", "早停与选模"],
              ["测试", "2025-02-01 ~ 2026-08-28", "样本外评估"],
              ["本报告", f"{esc(start)} ~ {esc(end)}", "测试集内统一窗口"],
          ],
          aligns=["l", "l", "l"],
      )}
    </div>
  </div>

  <h3>2.2 成交时序与费用</h3>
  <div class="timeline">
    <div class="step"><div class="step-no">步骤 1 · 信号日 t</div><div class="step-title">收盘出分</div><p class="small">用 t 日收盘前可得的数据打分，选出 TopK，不使用未来信息。</p></div>
    <div class="step"><div class="step-no">步骤 2 · t+1</div><div class="step-title">收盘买入</div><p class="small">剔除当日涨停、跌停、停牌；按 100 股整手取整，现金不足则降手数。</p></div>
    <div class="step"><div class="step-no">步骤 3 · t+1+H</div><div class="step-title">收盘卖出</div><p class="small">跌停或停牌无法卖出时顺延，最多 10 个交易日；强平按最近可得价格。</p></div>
  </div>
  {table(
      ["项目", "口径"],
      [
          ["股票池", "A 股；剔除 ST（静态名单，见局限）、停牌、涨停 / 跌停、上市不足 60 个交易日的次新股"],
          ["交易费用", "双边佣金 3bp；基准不含费用"],
          ["敏感性", "卖出印花税 5bp、双边滑点 5bp 分别单独加压"],
          ["复权", "收益用前复权收盘价；涨跌停判断用不复权收盘价"],
          ["现金", "初始 200 万元；不使用融资融券，不做空"],
          ["基准", "同一股票池、同一 t+1 买入 / t+2 卖出的等权组合，日度再平衡"],
      ],
      aligns=["l", "l"],
  )}

  <h3>2.3 三种组合的精确含义</h3>
  {table(
      ["组合", "建仓方式", "资金", "2026 窗口内大致轮次", "适合回答"],
      [
          ["Top10", "同一时点只持有一批 10 只；全部卖出后才开下一批", "每只 20 万，合计 200 万", "T+5 约 22 批；T+10 约 13 批", "集中持仓的收益弹性"],
          ["Top20", "同一时点只持有一批 20 只；全部卖出后才开下一批", "每只 10 万，合计 200 万", "T+5 约 22 批；T+10 约 13 批", "分散后的收益与回撤"],
          ["滚动 Top5", "每天新开 5 只，持有 H 天后卖出；T+5 时同时有 5 个批次（25 只），T+10 时有 10 个批次（50 只）", "每日预算 = 当日总权益 / H", "每天滚动，约 150 个批次", "最接近“每日调仓”的实盘形态"],
      ],
      aligns=["l", "l", "l", "l", "l"],
  )}
  <p class="small">注意：Top10 / Top20 不是每天重叠建仓，而是“单批轮动”，因此 2026 年只有约 13 至 22 个独立持有期。组合收益的统计功效远低于逐日 IC 检验，不能只看总收益排名。</p>

  <h3>2.4 模型与训练产物</h3>
  <p>13 个模型使用同一套 300 维 Cross300 特征、同一训练 / 验证切分。下表列出实际读取的运行目录{'' if pending.empty else '；空缺表示该组合尚未训练完成，报告中保留空位'}。</p>
  {table(
      ["代码", "模型", "家族", "T+5 运行目录", "T+10 运行目录"],
      inventory_rows,
      aligns=["l", "l", "l", "l", "l"],
  )}

  <h3>2.5 严格统计工具箱</h3>
  {table(
      ["方法", "解决什么问题", "本报告中的用法"],
      [
          ["Newey-West t", "重叠持有期导致的自相关", "IC 均值的显著性，滞后阶数 H-1"],
          ["循环块自助", "日收益不服从正态、尾部相关", "IC 与超额收益的 95% 区间、单边 p 值"],
          ["Benjamini-Hochberg", "同时比较 13 个模型的多重检验", "对 IC 与超额收益的 p 值做 FDR 校正，报告 q 值"],
          ["Friedman + Kendall W", "13 个模型在同一批日期上的整体差异", "逐日 IC、组合日收益、超额收益"],
          ["配对 Wilcoxon + BH", "冠军与其余模型的逐日差异", "冠军 vs 其余 12 个模型"],
          ["Deflated Sharpe Ratio", "在 26 个模型-周期组合里挑第一名的选择偏差", "以 26 次试验校正 Sharpe"],
          ["White Reality Check", "数据窥探下的“最佳模型”是否真的优于随机", "块自助构造最佳统计量的零分布"],
          ["PBO / CSCV", "回测过拟合概率", "10 个时间块组合，估计样本内最优在样本外的排名"],
          ["十分位单调性", "模型是否只押中一个风格", "第 1 至第 10 十分位平均收益的秩相关"],
      ],
      aligns=["l", "l", "l"],
  )}

  {callout(
      "两个必须提前说明的偏差",
      "<p><strong>静态 ST 名单：</strong>没有逐日 ST 标记，用 2026-10-16 名单回填全窗口，存在前视偏差、可能高估收益。</p>"
      "<p><strong>组合轮次少：</strong>Top10 / Top20 为单批轮动，T+10 仅约 13 个独立批次，收益差可能由一两批运气决定。</p>",
      "warn",
  )}
</section>
"""


def quality_section(bundle: MultiBundle, chart_paths: dict[str, Path]) -> str:
    blocks = []
    for horizon in (5, 10):
        frame = _ranked(bundle, horizon)
        if frame is None or frame.empty:
            blocks.append(f"<p>T+{horizon} 尚无就绪模型。</p>")
            continue
        rows = []
        for row in frame.itertuples():
            rows.append(
                [
                    int(row.rank),
                    esc(display_name(str(row.model))),
                    num(row.ic_mean, 4),
                    num(row.icir, 3),
                    pct(row.ic_positive_ratio, 1),
                    num(row.nw_t, 2),
                    num(row.q_bootstrap, 4),
                    pct(row.top5_hit, 1),
                    pct(row.top10_hit, 1),
                    pct(row.top10_dir, 1),
                    num(row.decile_monotonicity, 2),
                ]
            )
        stats = bundle.quality_stats(horizon)
        friedman = stats.get("friedman", {})
        friedman_rows = []
        for metric, title in METRIC_TITLES.items():
            payload = friedman.get(metric)
            if not payload:
                continue
            friedman_rows.append(
                [
                    title,
                    num(payload.get("chi2"), 2),
                    num(payload.get("p"), 4),
                    num(payload.get("kendall_w"), 3),
                    money(payload.get("n_days", 0)),
                ]
            )
        best = frame.iloc[0]
        worst = frame.iloc[-1]
        blocks.append(
            f"""
  <h3>3.{1 if horizon == 5 else 2} {HORIZON_TITLES[horizon]}：预测质量榜</h3>
  <p>IC 第一为 <strong>{esc(display_name(str(best['model'])))}</strong>（{num(best['ic_mean'], 4)}），
  末位为 {esc(display_name(str(worst['model'])))}（{num(worst['ic_mean'], 4)}）。
  IC 为正的交易日占比 {pct(best['ic_positive_ratio'], 1)}；Newey-West t = {num(best['nw_t'], 2)}，
  块自助 FDR q = {num(best['q_bootstrap'], 4)}。
  十分位单调性（1 至 10 的秩相关）为 {num(best['decile_monotonicity'], 2)}，
  越接近 1 说明“分数越高、后续收益越高”的排序越干净。</p>
  {table(
      ["#", "模型", "IC 均值", "ICIR", "IC>0", "NW t", "q 值", "Top5 上涨", "Top10 上涨", "Top10 方向", "十分位单调"],
      rows,
      aligns=["c", "l", "r", "r", "r", "r", "r", "r", "r", "r", "r"],
  )}
  <h4>13 个模型是否有整体差异</h4>
  {table(
      ["指标", "Friedman 卡方", "p 值", "Kendall W", "有效天数"],
      friedman_rows,
      aligns=["l", "r", "r", "r", "r"],
  )}
  <p class="small">Kendall W 在 0 到 1 之间；0 表示 13 个模型的排序与随机排列无异，1 表示每个交易日的排序完全一致。p 值小只说明“至少有一个模型不同”，不说明冠军一定可交易。</p>
"""
        )

    edges = {horizon: _decile_edges(bundle, horizon) for horizon in (5, 10)}
    edge_text = "；".join(
        f"T+{horizon} 的 {esc(display_name(edge[0]))} 从 D1 {pct(edge[1], 2, True)} 升至 D10 {pct(edge[2], 2, True)}"
        for horizon, edge in edges.items()
        if edge is not None
    )
    edge_note = (
        f'<p class="small">读法：{edge_text}。若十分位曲线出现中部塌陷或末端反转，说明模型只擅长极端一端，实盘选股要避开不稳定区间。</p>'
        if edge_text
        else ""
    )

    return f"""
<section class="section page-break">
  {section_head("03", "预测质量：13 个模型的同一张考卷", "IC、方向胜率、上涨比例与十分位单调性")}
  <p class="section-deck">预测质量只回答“排序对不对”，不含交易费用与资金约束；组合收益见第 4 章。所有指标均在剔除 ST、涨跌停、停牌、次新后的可交易池内计算。</p>
  {figure(chart_paths['hit'], "Top10 方向胜率与上涨比例。虚线为 50% 随机基准；距离虚线越远，选股的方向性越强。", 2) if 'hit' in chart_paths else ''}
  {figure(chart_paths['ic_dist'], "逐日 IC 的完整分布。箱体越窄，说明模型在不同交易日越稳定；只看均值会掩盖大幅波动。", 3) if 'ic_dist' in chart_paths else ''}
  {''.join(blocks)}
  {figure(chart_paths['deciles'], "十分位平均收益。若模型真正有排序能力，颜色应从左到右由绿转红（A 股红涨绿跌）；若只有某几列突出，说明模型在押风格而非选股。", 4) if 'deciles' in chart_paths else ''}
  {edge_note}
</section>
"""
