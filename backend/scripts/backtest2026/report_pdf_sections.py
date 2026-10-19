"""PDF 报告正文各章节。

内容顺序按「先结论、再方法、再证据、最后风险」组织，避免把读者直接丢进表格。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from .report_pdf_data import INDEX_TITLES, ModelReport, ReportBundle
from .report_pdf_html import (
    callout,
    date_cn,
    date_short,
    esc,
    figure,
    money,
    num,
    pct,
    section_head,
    span,
    table,
    tone,
    wan,
)

VARIANTS = [
    ("baseline", "基准"),
    ("no_st_filter", "去掉 ST 过滤"),
    ("no_limit_filter", "去掉涨跌停过滤"),
    ("no_new_filter", "去掉次新过滤"),
    ("no_filters", "去掉全部过滤"),
    ("stamp_duty_5bp", "加印花税 5bp"),
    ("slippage_5bp", "加滑点 5bp"),
]


def _quality(report: ModelReport, window: str) -> dict[str, Any]:
    return report.metrics["quality"][window]


def _perf(report: ModelReport, kind: str) -> dict[str, Any]:
    return report.metrics[kind]["performance"]


def _trade_metrics(report: ModelReport, kind: str) -> dict[str, Any]:
    return report.metrics[kind]["trades"]


def _variants(report: ModelReport, kind: str) -> dict[str, Any]:
    return report.metrics[f"{kind}_variants"]


def _kpi(value: str, label: str, sub: str = "") -> str:
    return (
        f'<div class="kpi"><div class="value">{value}</div>'
        f'<div class="label">{label}</div><div class="sub">{sub}</div></div>'
    )


def _attr_line(frame: pd.DataFrame, count: int, worst: bool = False) -> str:
    if frame.empty:
        return "—"
    selected = frame.tail(count).iloc[::-1] if worst else frame.head(count)
    parts = []
    for symbol, row in selected.iterrows():
        parts.append(f"{esc(str(symbol))} {pct(row['pnl'] / 2_000_000.0, 2, signed=True)}")
    return " · ".join(parts)


def _attr_abs_line(frame: pd.DataFrame, count: int, worst: bool = False) -> str:
    if frame.empty:
        return "—"
    selected = frame.tail(count).iloc[::-1] if worst else frame.head(count)
    parts = []
    for symbol, row in selected.iterrows():
        parts.append(f"{esc(str(symbol))} {wan(row['pnl'])}")
    return " · ".join(parts)


def cover(bundle: ReportBundle) -> str:
    t5 = bundle.models["T5"]
    t10 = bundle.models["T10"]
    t5_q = _quality(t5, "extended_window")
    t10_q = _quality(t10, "extended_window")
    t5_perf = _perf(t5, "top20")
    t10_perf = _perf(t10, "top20")
    t5_roll = _perf(t5, "rolling")
    t10_roll = _perf(t10, "rolling")
    return f"""
<div class="cover">
  <div class="cover-band"></div>
  <div class="kicker">QuantMind Research · A 股量化研究</div>
  <h1>2026 年 TopK 组合回测</h1>
  <div class="subtitle">预测质量、组合收益与可执行性评估<br>T+5 / T+10 双周期 · Cross300 LightGBM</div>
  <div class="scope">回测区间 2026-01-05 ~ 2026-09-30 · 初始资金 200 万元 · 双边手续费万 3</div>

  <div class="summary">
    <div class="summary-title">研究结论摘要</div>
    <ul>
      <li>模型仍有稳定的截面区分度：T+5 IC {pct(t5_q['ic_eligible_mean'])}，T+10 IC {pct(t10_q['ic_eligible_mean'])}；IC 大于 0 的交易日占 {pct(t5_q['ic_positive_ratio'], 0)}–{pct(t10_q['ic_positive_ratio'], 0)}。</li>
      <li>200 万元 Top20：T+5 总收益 <strong>{pct(t5_perf['total_return'], 2, True)}</strong>，T+10 总收益 <strong>{pct(t10_perf['total_return'], 2, True)}</strong>；同期等权可交易池为 {pct(t5.metrics['benchmark_ew']['total_return'])}。</li>
      <li>每天买 5 只、滚动持有：T+5 总收益 {pct(t5_roll['total_return'], 2, True)}，T+10 总收益 {pct(t10_roll['total_return'], 2, True)}；单笔净胜率 {pct(t5.stats_rolling['net_win'], 1)} / {pct(t10.stats_rolling['net_win'], 1)}，收益主要来自少数大赢家。</li>
      <li>最大的不确定性不是模型，而是数据：ST 标记使用 2026-10-16 的静态名单回填，存在前视；本报告按研究口径解读，不构成实盘业绩承诺。</li>
    </ul>
  </div>

  <div class="metric-grid">
    <div class="metric"><div class="label">T+5 截面 IC</div><div class="value">{pct(t5_q['ic_eligible_mean'])}</div><div class="sub">170 个交易日 · NW t = {num(t5.nw_ic['t'])}</div></div>
    <div class="metric"><div class="label">T+10 截面 IC</div><div class="value">{pct(t10_q['ic_eligible_mean'])}</div><div class="sub">170 个交易日 · NW t = {num(t10.nw_ic['t'])}</div></div>
    <div class="metric"><div class="label">T+5 Top20 总收益</div><div class="value">{pct(t5_perf['total_return'], 2, True)}</div><div class="sub">期末权益 {wan(t5_perf['final_equity'])} · Sharpe {num(t5_perf['sharpe'])}</div></div>
    <div class="metric"><div class="label">T+10 Top20 总收益</div><div class="value">{pct(t10_perf['total_return'], 2, True)}</div><div class="sub">期末权益 {wan(t10_perf['final_equity'])} · Sharpe {num(t10_perf['sharpe'])}</div></div>
  </div>

  <div class="cover-foot">
    <div>报告编号 QM-R-2026-1019 · 生成日期 {date_cn(bundle.generated_at)}<br>数据来源：QuantDB、模型推理产物、回测台账</div>
    <div>仅供学习研究与技术演示，不构成投资建议。<br>股市有风险，投资需谨慎。</div>
  </div>
</div>
"""


def executive_summary(bundle: ReportBundle) -> str:
    t5 = bundle.models["T5"]
    t10 = bundle.models["T10"]
    t5_q = _quality(t5, "extended_window")
    t10_q = _quality(t10, "extended_window")
    rows = []
    for report, kind, label in (
        (t5, "top20", "T+5 Top20"),
        (t5, "rolling", "T+5 滚动 Top5"),
        (t10, "top20", "T+10 Top20"),
        (t10, "rolling", "T+10 滚动 Top5"),
    ):
        perf = _perf(report, kind)
        trades = _trade_metrics(report, kind)
        net_win = report.stats_top20["net_win"] if kind == "top20" else report.stats_rolling["net_win"]
        rows.append(
            [
                label,
                f"{date_short(perf['start'])} ~ {date_short(perf['end'])}",
                pct(perf["total_return"], 2, True),
                pct(perf["annual_return"], 2, True),
                num(perf["sharpe"]),
                pct(perf["max_drawdown"], 2),
                num(trades["n_round_trips"], 0),
                pct(net_win, 1),
                money(trades["total_fees"]),
            ]
        )
    return f"""
<section class="section">
  {section_head("00", "执行摘要", "先看结论，再看证据")}
  <p class="lead">本报告回答四个问题：模型在 2026 年还有没有预测能力；把预测变成 200 万元 Top20 组合能赚多少；每天买 5 只、连续买入并滚动卖出，方向胜率如何；逐股推理数据是否完整。所有结果均按「信号日收盘出分、次日收盘买入、持有 H 个交易日收盘卖出」执行，并扣除双边万 3 手续费。</p>

  <div class="kpi-row">
    {_kpi(pct(t5_q['ic_eligible_mean']), "T+5 截面 IC", f"ICIR {num(t5_q['icir'])} · IC>0 占比 {pct(t5_q['ic_positive_ratio'], 1)}")}
    {_kpi(pct(t10_q['ic_eligible_mean']), "T+10 截面 IC", f"ICIR {num(t10_q['icir'])} · IC>0 占比 {pct(t10_q['ic_positive_ratio'], 1)}")}
    {_kpi(pct(_perf(t5, "top20")["total_return"], 2, True), "T+5 Top20 总收益", f"最大回撤 {pct(_perf(t5, 'top20')['max_drawdown'])}")}
    {_kpi(pct(_perf(t10, "top20")["total_return"], 2, True), "T+10 Top20 总收益", f"最大回撤 {pct(_perf(t10, 'top20')['max_drawdown'])}")}
  </div>

  <h3>四组组合，一页看懂</h3>
  {table(
      ["组合", "区间", "总收益", "年化", "Sharpe", "最大回撤", "完成回合", "单笔净胜率", "总费用"],
      rows,
      aligns=["l", "l", "r", "r", "r", "r", "r", "r", "r"],
  )}
  <div class="table-note">单笔净胜率按回合交易计算（净收益 &gt; 0 的比例）；总费用包含买入与卖出佣金。滚动 Top5 的区间终点与对应模型持有期一致。</div>

  <div class="two-col">
    <div>
      <h3>先看结论</h3>
      <ul class="checklist">
        <li><strong>模型有效，但不要把 IC 当成收益率。</strong> IC 衡量的是全市场排序能力；组合只持有 20 只或每天 5 只，最终收益还取决于尾部选择、市场方向、换手和成本。</li>
        <li><strong>绝对收益一般，超额明显。</strong> T+5 Top20 赚 {pct(_perf(t5, "top20")["total_return"], 2, True)}，T+10 Top20 赚 {pct(_perf(t10, "top20")["total_return"], 2, True)}；同期等权可交易池为 {pct(t5.metrics["benchmark_ew"]["total_return"])}，沪深 300 为 {pct(t5.metrics["benchmark_indices"]["SH000300"])}（T+5 区间）。</li>
        <li><strong>收益形态是低胜率、正偏态。</strong> 滚动 Top5 的单笔净胜率只有 {pct(t5.stats_rolling["net_win"], 1)} / {pct(t10.stats_rolling["net_win"], 1)}，但平均盈利大于平均亏损；少数大赢家决定了总收益。</li>
        <li><strong>最大的风险来自数据与成本，不是模型本身。</strong> ST 静态名单有前视；收盘成交未建模冲击；高换手策略对滑点非常敏感。</li>
      </ul>
    </div>
    <div>
      {callout(
          "三个必须知道的口径",
          "<p><strong>1. 净收益。</strong> 表格中的组合收益均已扣除双边万 3 佣金；印花税和滑点作为敏感性单独列出。</p>"
          "<p><strong>2. 研究回测。</strong> 不是实盘业绩。ST 名单、收盘成交、无冲击假设都会让结果偏乐观。</p>"
          "<p><strong>3. 单窗口。</strong> 只覆盖 2026 年 1–9 月；不能据此推断更长周期或其它市场状态。</p>",
          "warn",
      )}
      {callout(
          "本报告回答",
          "<ul><li>模型今年还准不准？见第 3 节。</li>"
          "<li>200 万 Top20 能赚多少？见第 4 节。</li>"
          "<li>每天买 5 只、滚动持有，方向胜率多少？见第 5 节。</li>"
          "<li>每个股票都有推理数据吗？见第 2.5 节。</li></ul>",
          "neutral",
      )}
    </div>
  </div>
</section>
"""


def method(bundle: ReportBundle) -> str:
    t5 = bundle.models["T5"]
    t10 = bundle.models["T10"]
    t5_meta = t5.meta
    t10_meta = t10.meta
    model_rows = [
        ["模型名称", esc(t5_meta.get("job_name", "T5")), esc(t10_meta.get("job_name", "T10"))],
        ["模型框架", esc(t5_meta.get("model_type", "lightgbm")), esc(t10_meta.get("model_type", "lightgbm"))],
        ["特征数量", num(t5_meta.get("feature_count"), 0), num(t10_meta.get("feature_count"), 0)],
        ["预测周期", f"{num(t5_meta.get('target_horizon_days'), 0)} 个交易日", f"{num(t10_meta.get('target_horizon_days'), 0)} 个交易日"],
        ["执行滞后", f"{num(t5_meta.get('execution_lag_days'), 0)} 个交易日", f"{num(t10_meta.get('execution_lag_days'), 0)} 个交易日"],
        ["训练区间", f"{t5_meta.get('train_start', '—')} ~ {t5_meta.get('train_end', '—')}", f"{t10_meta.get('train_start', '—')} ~ {t10_meta.get('train_end', '—')}"],
        ["验证区间", f"{t5_meta.get('val_start', '—')} ~ {t5_meta.get('val_end', '—')}", f"{t10_meta.get('val_start', '—')} ~ {t10_meta.get('val_end', '—')}"],
        ["测试区间", f"{t5_meta.get('test_start', '—')} ~ {t5_meta.get('test_end', '—')}", f"{t10_meta.get('test_start', '—')} ~ {t10_meta.get('test_end', '—')}"],
        ["最优迭代", num(t5_meta.get("best_iteration"), 0), num(t10_meta.get("best_iteration"), 0)],
        ["测试集 Rank IC", pct(t5_meta.get("metrics", {}).get("test_rank_ic")), pct(t10_meta.get("metrics", {}).get("test_rank_ic"))],
        ["预测覆盖", f"{money(t5.metrics['pred_rows'])} 行 / {num(t5.metrics['pred_days'], 0)} 天", f"{money(t10.metrics['pred_rows'])} 行 / {num(t10.metrics['pred_days'], 0)} 天"],
    ]
    return f"""
<section class="section page-break">
  {section_head("01", "研究设计与口径", "先钉死执行假设，再谈收益")}
  <p class="lead">本报告使用两套 Cross300 LightGBM 模型：T+5 与 T+10。两套模型共享 300 维因子输入、相同的训练/验证/测试切分与执行滞后，只改变预测周期。2026 年回测窗口落在测试集内，因此相对训练与验证是样本外。</p>

  <h3>1.1 模型与切分</h3>
  {table(["项目", "T+5", "T+10"], model_rows, aligns=["l", "l", "l"])}
  <div class="table-note">标签：<code>close[t+1+H] / close[t+1] - 1</code>，H 为 5 或 10；执行滞后固定为 1 个交易日。训练标签使用后复权价，组合收益使用前复权价；两种口径在报告中分别标注。</div>

  <h3>1.2 从信号到成交</h3>
  <div class="timeline">
    <div class="step"><div class="step-no">STEP 01</div><div class="step-title">T 日收盘：出分</div><div class="step-desc">模型只使用 T 日及以前的特征，输出每只股票的截面分数。</div></div>
    <div class="step"><div class="step-no">STEP 02</div><div class="step-title">T+1 日收盘：买入</div><div class="step-desc">按分数排序取 TopK；若涨停、跌停、停牌或次新，则跳过，不递补下一名。</div></div>
    <div class="step"><div class="step-no">STEP 03</div><div class="step-title">T+1+H 日收盘：卖出</div><div class="step-desc">到期卖出；若跌停或停牌无法卖出，顺延最多 10 个交易日，之后按最近价格强制退出。</div></div>
  </div>
  <p class="small">信号与成交之间强制隔一个交易日，避免用当日收盘价「假装成交」。这也是报告里所有「前视」讨论的基准。</p>

  <h3>1.3 股票池与过滤</h3>
  {table(
      ["过滤项", "规则", "本报告处理"],
      [
          ["股票池", "沪深 A 股，含主板、创业板、科创板", "剔除北交所；只保留 SH/SZ 六位代码"],
          ["ST", "2026-10-16 静态名单（198 只）", "全区间回填；存在前视，见第 6 节"],
          ["涨停 / 跌停", "主板 10%；创业板 / 科创板 20%；ST 主板 2026-07-06 前 5%，之后 10%", "信号日与入场日都剔除；按不复权价和板块规则判定"],
          ["停牌", "成交量为 0 或价格缺失", "信号日剔除；入场日买不到则跳过"],
          ["次新股", "上市不足 60 个交易日", "以预测面板首次出现日近似上市日"],
          ["买不到", "涨停、跌停、停牌、次新、现金不足", "跳过，不递补；在台账中记录原因"],
          ["卖不出", "跌停或停牌", "顺延，最多 10 个交易日；之后按最近可得价格强制退出"],
      ],
      aligns=["l", "l", "l"],
  )}

  <h3>1.4 费用与数据补齐</h3>
  <div class="two-col">
    <div>
      <ul>
        <li><strong>基准费用：</strong>买入与卖出各 3bp（万 3），按成交金额计。</li>
        <li><strong>印花税敏感性：</strong>卖出额外 5bp。</li>
        <li><strong>滑点敏感性：</strong>买入上浮 5bp、卖出下浮 5bp。</li>
        <li><strong>整手约束：</strong>按 100 股取整；买不满一手则跳过。</li>
      </ul>
    </div>
    <div>
      <ul>
        <li><strong>外推补齐：</strong>训练 pred 截止 2026-08-27 / 08-26，之后用模型推理补到 2026-09-14。</li>
        <li><strong>重叠校验：</strong>重叠日相关系数 0.99999，Top100 重合 100/100。</li>
        <li><strong>停止边界：</strong>2026-09-15 起 <code>gap_mined</code> 因子分区不完整（约 91.7% 缺失），不喂给模型。</li>
      </ul>
    </div>
  </div>

  <h3>1.5 逐股推理覆盖</h3>
  <div class="two-col">
    <div>
      {table(
          ["项目", "T+5", "T+10"],
          [
              ["预测行数", money(t5.metrics["pred_rows"]), money(t10.metrics["pred_rows"])],
              ["预测交易日", num(t5.metrics["pred_days"], 0), num(t10.metrics["pred_days"], 0)],
              ["覆盖股票数", money(t5.metrics["pred_symbols"]), money(t10.metrics["pred_symbols"])],
              ["未覆盖股票", num(len(t5.metrics["per_stock_missing"]), 0), num(len(t10.metrics["per_stock_missing"]), 0)],
          ],
          aligns=["l", "r", "r"],
      )}
    </div>
    <div>
      <p>预测面板覆盖 <code>l1_factors</code> 因子源的全部 5,202 只股票。未覆盖的 25 只中，23 只是在 2026-08-06 之后上市；另 2 只（<code>SH689009</code>、<code>SZ302132</code>）不在因子源内。逐股文件 <code>per_stock_*.csv</code> 将后者标记为 <code>never_predicted</code>。</p>
      <p class="small">结论：不是推理失败，而是模型实际可用的因子域边界。新上市股票在进入因子源之前不会出现在预测面板中。</p>
    </div>
  </div>
</section>
"""


def _heat_cell(value: float, max_abs: float) -> str:
    if pd.isna(value):
        return "—"
    alpha = 0.10 + 0.55 * min(abs(float(value)) / max_abs, 1.0)
    if value > 0:
        color = "#FFFFFF" if alpha > 0.42 else "#1D1D1B"
        return f'<td class="center" style="background:rgba(166,43,47,{alpha:.2f});color:{color}">{pct(value)}</td>'
    color = "#FFFFFF" if alpha > 0.42 else "#1D1D1B"
    return f'<td class="center" style="background:rgba(30,107,79,{alpha:.2f});color:{color}">{pct(value)}</td>'


def quality(bundle: ReportBundle, paths: dict[str, Path]) -> str:
    t5 = bundle.models["T5"]
    t10 = bundle.models["T10"]
    quality_rows = []
    for report in (t5, t10):
        for window, label in (("stored_window", "训练 pred 窗口"), ("extended_window", "含外推全窗口")):
            q = _quality(report, window)
            quality_rows.append(
                [
                    report.title,
                    label,
                    num(q["days"], 0),
                    pct(q["ic_eligible_mean"]),
                    num(q["icir"]),
                    pct(q["ic_positive_ratio"], 1),
                    pct(q["hit_all_mean"], 1),
                    pct(q["top_ret_mean"], 2),
                    pct(q["top_hit_mean"], 1),
                    pct(q["long_short_mean"], 2),
                ]
            )
    monthly = t5.monthly_quality.merge(
        t10.monthly_quality, on="date", suffixes=("_t5", "_t10")
    )
    max_abs = max(
        float(monthly["ic_eligible_t5"].abs().max()),
        float(monthly["ic_eligible_t10"].abs().max()),
        0.01,
    )
    heat_rows = []
    for _, row in monthly.iterrows():
        month = pd.Timestamp(row["date"]).strftime("%Y-%m")
        heat_rows.append(
            f"<tr><td>{month}</td>{_heat_cell(row['ic_eligible_t5'], max_abs)}"
            f"<td class='num'>{pct(row['hit_all_t5'], 1)}</td><td class='num'>{pct(row['top_ret_t5'], 2)}</td>"
            f"{_heat_cell(row['ic_eligible_t10'], max_abs)}"
            f"<td class='num'>{pct(row['hit_all_t10'], 1)}</td><td class='num'>{pct(row['top_ret_t10'], 2)}</td></tr>"
        )
    return f"""
<section class="section page-break">
  {section_head("02", "预测质量", "模型今年还准不准")}
  <p class="lead">预测质量用三个层次衡量：全市场截面 IC（排序能力）、逐日方向胜率（符号判断）、Top20 组合表现（尾部选择）。三者不是同一个东西；IC 高不代表 Top20 一定赚钱，Top20 赚钱也不代表全市场方向判断准确。</p>

  <h3>2.1 全窗口指标</h3>
  {table(
      ["模型", "窗口", "交易日", "IC", "ICIR", "IC>0 占比", "方向胜率", "Top20 平均", "Top20 命中", "多空"],
      quality_rows,
      aligns=["l", "l", "r", "r", "r", "r", "r", "r", "r", "r"],
  )}
  <div class="table-note">IC 为可交易池的截面秩相关；ICIR 为 IC 均值 / IC 标准差（未年化）；方向胜率为全可交易池 <code>sign(pred)=sign(actual)</code> 的比例；Top20 命中率为 Top20 中实际收益为正的比例；多空为 Top20 减 Bottom20 的平均收益。</div>

  {figure(paths["ic"], "逐日 IC 与 20 日均值。灰线为逐日 IC，红线为 20 日均值；虚线为全窗口均值。", 1)}

  <div class="two-col">
    <div>
      <h3>2.2 怎么读这些数</h3>
      <ul>
        <li><strong>IC 的绝对水平不低。</strong> 全窗口 IC 分别为 {pct(_quality(t5, 'extended_window')['ic_eligible_mean'])} 与 {pct(_quality(t10, 'extended_window')['ic_eligible_mean'])}；用 Newey-West 修正重叠样本后，t 值分别为 {num(t5.nw_ic['t'])} 与 {num(t10.nw_ic['t'])}，仍在 1% 水平显著。</li>
        <li><strong>T+10 的 IC 更高，但不代表更稳。</strong> 10 日持有期让相邻样本高度重叠，有效独立样本更少；NW t 反而低于 T+5。不要把 {pct(_quality(t10, 'extended_window')['ic_eligible_mean'])} 简单理解为比 {pct(_quality(t5, 'extended_window')['ic_eligible_mean'])} 更可靠。</li>
        <li><strong>方向胜率约 52%，Top20 命中率约 48%–50%。</strong> 这不是「每只都涨」的策略；Top20 平均收益为正，是因为赢家幅度大于输家。</li>
        <li><strong>正收益天数高于个股命中率。</strong> Top20 组合收益为正的天数约 55% / 54%，但 Top20 中个股正收益比例约 49% / 48%；平均被少数大赢家拉高。</li>
      </ul>
    </div>
    <div>
      {callout(
          "IC 10% 是什么概念",
          "<p>把全市场按模型分数从高到低排序，高分组的平均表现好于低分组；IC 10% 表示这种排序关系在统计上稳定存在，而不是「10 只里赚 1 只」。</p>"
          "<p>它也不是涨跌概率。模型输出的是截面分数，不是「上涨概率 60%」这样的二元判断。</p>",
          "neutral",
      )}
      {callout(
          "4 月是唯一的负 IC 月份",
          "<p>T+5 与 T+10 在 2026 年 4 月的月度 IC 都为负，5–6 月 Top20 平均收益转负；7–8 月修复。组合的最大回撤也集中在 5–7 月。</p>",
          "warn",
      )}
    </div>
  </div>

  <h3>2.3 月度稳定性</h3>
  <table class="data-table heat-table">
    <thead><tr><th>月份</th><th>T+5 IC</th><th>T+5 方向胜率</th><th>T+5 Top20 平均</th><th>T+10 IC</th><th>T+10 方向胜率</th><th>T+10 Top20 平均</th></tr></thead>
    <tbody>{''.join(heat_rows)}</tbody>
  </table>
  <div class="table-note">单元格颜色越深表示 IC 绝对值越大；红色为正，绿色为负。T+5 与 T+10 都只有 4 月为负 IC，但 Top20 平均收益在 5–6 月连续为负。</div>
</section>
"""


def top20(bundle: ReportBundle, paths: dict[str, Path]) -> str:
    t5 = bundle.models["T5"]
    t10 = bundle.models["T10"]
    rows = []
    for label, report in (("T+5 Top20", t5), ("T+10 Top20", t10)):
        perf = _perf(report, "top20")
        trades = _trade_metrics(report, "top20")
        rows.append(
            [
                label,
                f"{date_short(perf['start'])} ~ {date_short(perf['end'])}",
                pct(perf["total_return"], 2, True),
                pct(perf["annual_return"], 2, True),
                pct(perf["annual_vol"], 2),
                num(perf["sharpe"]),
                pct(perf["max_drawdown"], 2),
                pct(perf["daily_win_rate"], 1),
                money(perf["final_equity"]),
                num(trades["n_round_trips"], 0),
                pct(trades["avg_trade_return"], 2, True),
                pct(trades["trade_win_rate"], 1),
                money(trades["total_fees"]),
                num(trades["turnover_x"], 1),
            ]
        )
    trade_rows = []
    for label, report in (("T+5 Top20", t5), ("T+10 Top20", t10)):
        stats = report.stats_top20
        trade_rows.append(
            [
                label,
                num(stats["n"], 0),
                pct(stats["gross_avg"], 2, True),
                pct(stats["gross_win"], 1),
                pct(stats["net_avg"], 2, True),
                pct(stats["net_win"], 1),
                pct(stats["median"], 2, True),
                pct(stats["p10"], 2, True),
                pct(stats["p90"], 2, True),
                pct(stats["payoff"], 2),
                num(stats["profit_factor"]),
                num(stats["hold_days"], 1),
            ]
        )
    attr_rows = []
    for label, report in (("T+5 Top20", t5), ("T+10 Top20", t10)):
        attr_rows.append(
            [
                label,
                _attr_abs_line(report.attribution_top20, 5),
                _attr_abs_line(report.attribution_top20, 3, worst=True),
            ]
        )
    return f"""
<section class="section page-break">
  {section_head("03", "Top20 组合", "200 万元，每只 10 万元")}
  <p class="lead">Top20 是最直观的组合：每天按模型分数取前 20 名，等权买入，持有 H 个交易日后卖出，再开下一期。它把模型的尾部选择能力直接变成收益，但也把集中度、路径和成本暴露得很清楚。</p>

  <div class="kpi-row">
    {_kpi(pct(_perf(t5, 'top20')['total_return'], 2, True), "T+5 Top20 总收益", f"期末 {wan(_perf(t5, 'top20')['final_equity'])}")}
    {_kpi(pct(_perf(t5, 'top20')['max_drawdown'], 2), "T+5 最大回撤", f"Sharpe {num(_perf(t5, 'top20')['sharpe'])}")}
    {_kpi(pct(_perf(t10, 'top20')['total_return'], 2, True), "T+10 Top20 总收益", f"期末 {wan(_perf(t10, 'top20')['final_equity'])}")}
    {_kpi(pct(_perf(t10, 'top20')['max_drawdown'], 2), "T+10 最大回撤", f"Sharpe {num(_perf(t10, 'top20')['sharpe'])}")}
  </div>

  {figure(paths["equity_t5"], "T+5 组合净值曲线。红线为 Top20，橙线为滚动 Top5，灰线为等权可交易池，浅灰虚线为沪深 300。", 2)}
  {figure(paths["equity_t10"], "T+10 组合净值曲线。红线为 Top20，青线为滚动 Top5，灰线为等权可交易池，浅灰虚线为沪深 300。", 3)}

  <h3>3.1 组合结果</h3>
  {table(
      ["指标", "T+5 Top20", "T+10 Top20"],
      [
          ["区间", f"{date_short(_perf(t5, 'top20')['start'])} ~ {date_short(_perf(t5, 'top20')['end'])}", f"{date_short(_perf(t10, 'top20')['start'])} ~ {date_short(_perf(t10, 'top20')['end'])}"],
          ["总收益", pct(_perf(t5, 'top20')['total_return'], 2, True), pct(_perf(t10, 'top20')['total_return'], 2, True)],
          ["年化收益", pct(_perf(t5, 'top20')['annual_return'], 2, True), pct(_perf(t10, 'top20')['annual_return'], 2, True)],
          ["年化波动", pct(_perf(t5, 'top20')['annual_vol'], 2), pct(_perf(t10, 'top20')['annual_vol'], 2)],
          ["Sharpe", num(_perf(t5, 'top20')['sharpe']), num(_perf(t10, 'top20')['sharpe'])],
          ["最大回撤", pct(_perf(t5, 'top20')['max_drawdown'], 2), pct(_perf(t10, 'top20')['max_drawdown'], 2)],
          ["日胜率", pct(_perf(t5, 'top20')['daily_win_rate'], 1), pct(_perf(t10, 'top20')['daily_win_rate'], 1)],
          ["期末权益", money(_perf(t5, 'top20')['final_equity']), money(_perf(t10, 'top20')['final_equity'])],
          ["完成回合", num(_trade_metrics(t5, 'top20')['n_round_trips'], 0), num(_trade_metrics(t10, 'top20')['n_round_trips'], 0)],
          ["单笔净收益", pct(_trade_metrics(t5, 'top20')['avg_trade_return'], 2, True), pct(_trade_metrics(t10, 'top20')['avg_trade_return'], 2, True)],
          ["单笔净胜率", pct(_trade_metrics(t5, 'top20')['trade_win_rate'], 1), pct(_trade_metrics(t10, 'top20')['trade_win_rate'], 1)],
          ["总费用", money(_trade_metrics(t5, 'top20')['total_fees']), money(_trade_metrics(t10, 'top20')['total_fees'])],
          ["换手（倍初始资金）", num(_trade_metrics(t5, 'top20')['turnover_x'], 1), num(_trade_metrics(t10, 'top20')['turnover_x'], 1)],
          ["顺延 / 强制退出", f"{num(_trade_metrics(t5, 'top20')['deferred_exits'], 0)} / {num(_trade_metrics(t5, 'top20')['forced_exits'], 0)}", f"{num(_trade_metrics(t10, 'top20')['deferred_exits'], 0)} / {num(_trade_metrics(t10, 'top20')['forced_exits'], 0)}"],
      ],
      aligns=["l", "r", "r"],
  )}

  {figure(paths["drawdown"], "回撤曲线。上栏 T+5，下栏 T+10；红线为 Top20，橙/青线为滚动 Top5。", 4)}

  <h3>3.2 回合交易质量</h3>
  {table(
      ["组合", "回合数", "毛平均", "毛胜率", "净平均", "净胜率", "中位数", "10% 分位", "90% 分位", "盈亏比", "利润因子", "平均持有"],
      trade_rows,
      aligns=["l", "r", "r", "r", "r", "r", "r", "r", "r", "r", "r", "r"],
  )}
  <div class="table-note">毛收益 = 卖出价 / 买入价 - 1，不含费用；净收益 = 净盈亏 / （买入金额 + 买入费用）。中位数为负而平均为正，说明收益分布右偏：多数交易小亏，少数交易大赚。</div>

  <h3>3.3 收益来自哪里</h3>
  {table(["组合", "前 5 名贡献（净盈亏）", "后 3 名贡献（净盈亏）"], attr_rows, aligns=["l", "l", "l"])}
  <div class="table-note">按标的汇总，展示区间内该标的全部回合的净盈亏；同一标的可能被多次买入卖出。前 5 名贡献约占总收益的 40%–100% 量级，后 3 名各亏损约 1–2 万元。</div>

  <h3>3.4 解读</h3>
  <ul>
    <li><strong>T+5 Top20 是四组里绝对收益最高的一组。</strong> 但最大回撤 {pct(_perf(t5, 'top20')['max_drawdown'])}，峰值出现在 {date_short(t5.drawdown_top20['peak_date'])}，谷值在 {date_short(t5.drawdown_top20['trough_date'])}，直到 {date_short(t5.drawdown_top20['recovery_date'])} 才修复；区间内 {pct(t5.drawdown_top20['under_water'], 1)} 的交易日低于前高。</li>
    <li><strong>T+10 Top20 的 IC 更高，但组合收益更低。</strong> 月度胜率只有 37.5%（3/8），最大回撤 {pct(_perf(t10, 'top20')['max_drawdown'])}，到区间结束仍未修复。持有期更长，独立下注次数更少，路径风险更大。</li>
    <li><strong>收益高度集中。</strong> T+5 与 T+10 的前 5 名贡献都远超后 3 名的亏损；组合不是靠 20 只平均贡献，而是靠少数大赢家。这也意味着样本外一旦缺少大赢家，收益会明显下台阶。</li>
    <li><strong>费用是真实的摩擦。</strong> T+5 换手 {num(_trade_metrics(t5, 'top20')['turnover_x'], 1)} 倍，总费用 {money(_trade_metrics(t5, 'top20')['total_fees'])} 元；T+10 换手 {num(_trade_metrics(t10, 'top20')['turnover_x'], 1)} 倍，费用 {money(_trade_metrics(t10, 'top20')['total_fees'])} 元。</li>
  </ul>
</section>
"""


def rolling(bundle: ReportBundle, paths: dict[str, Path]) -> str:
    t5 = bundle.models["T5"]
    t10 = bundle.models["T10"]
    rows = []
    for label, report in (("T+5 滚动 Top5", t5), ("T+10 滚动 Top5", t10)):
        perf = _perf(report, "rolling")
        trades = _trade_metrics(report, "rolling")
        rows.append(
            [
                label,
                f"{date_short(perf['start'])} ~ {date_short(perf['end'])}",
                pct(perf["total_return"], 2, True),
                pct(perf["annual_return"], 2, True),
                pct(perf["annual_vol"], 2),
                num(perf["sharpe"]),
                pct(perf["max_drawdown"], 2),
                pct(perf["daily_win_rate"], 1),
                money(perf["final_equity"]),
                num(trades["n_round_trips"], 0),
                pct(trades["avg_trade_return"], 2, True),
                pct(trades["trade_win_rate"], 1),
                money(trades["total_fees"]),
                num(trades["turnover_x"], 1),
            ]
        )
    direction_rows = []
    for label, report, kind in (
        ("T+5 Top20", t5, "top20"),
        ("T+5 滚动 Top5", t5, "rolling"),
        ("T+10 Top20", t10, "top20"),
        ("T+10 滚动 Top5", t10, "rolling"),
    ):
        stats = report.stats_top20 if kind == "top20" else report.stats_rolling
        direction_rows.append(
            [
                label,
                num(stats["n"], 0),
                pct(stats["gross_avg"], 2, True),
                pct(stats["gross_win"], 1),
                pct(stats["net_avg"], 2, True),
                pct(stats["net_win"], 1),
                pct(stats["median"], 2, True),
                pct(stats["payoff"], 2),
                num(stats["hold_days"], 1),
            ]
        )
    return f"""
<section class="section page-break">
  {section_head("04", "滚动 Top5", "每天买 5 只，连续买入、滚动卖出")}
  <p class="lead">滚动策略把资金拆成 H 个桶：每天新建一个桶，买入当天 Top5；最早的一桶在持有 H 个交易日后卖出，回款用于当天的新桶。T+5 用 5 个桶，T+10 用 10 个桶。它比 Top20 更接近「每天都有仓位、每天都有到期」的真实执行节奏。</p>

  <h3>4.1 资金桶规则</h3>
  {table(
      ["项目", "T+5 滚动", "T+10 滚动"],
      [
          ["资金桶数", "5", "10"],
          ["每桶资金", "总权益的 1/5", "总权益的 1/10"],
          ["每日新买", "5 只", "5 只"],
          ["持有期", "5 个交易日", "10 个交易日"],
          ["同时在手上限", "25 只（5 桶 × 5 只）", "50 只（10 桶 × 5 只）"],
          ["卖出回款", "当天用于新桶买入", "当天用于新桶买入"],
          ["买不到", "跳过，不递补", "跳过，不递补"],
          ["卖不出", "顺延，最多 10 个交易日", "顺延，最多 10 个交易日"],
      ],
      aligns=["l", "l", "l"],
  )}

  <h3>4.2 组合结果</h3>
  {table(
      ["指标", "T+5 滚动 Top5", "T+10 滚动 Top5"],
      [
          ["区间", f"{date_short(_perf(t5, 'rolling')['start'])} ~ {date_short(_perf(t5, 'rolling')['end'])}", f"{date_short(_perf(t10, 'rolling')['start'])} ~ {date_short(_perf(t10, 'rolling')['end'])}"],
          ["总收益", pct(_perf(t5, 'rolling')['total_return'], 2, True), pct(_perf(t10, 'rolling')['total_return'], 2, True)],
          ["年化收益", pct(_perf(t5, 'rolling')['annual_return'], 2, True), pct(_perf(t10, 'rolling')['annual_return'], 2, True)],
          ["年化波动", pct(_perf(t5, 'rolling')['annual_vol'], 2), pct(_perf(t10, 'rolling')['annual_vol'], 2)],
          ["Sharpe", num(_perf(t5, 'rolling')['sharpe']), num(_perf(t10, 'rolling')['sharpe'])],
          ["最大回撤", pct(_perf(t5, 'rolling')['max_drawdown'], 2), pct(_perf(t10, 'rolling')['max_drawdown'], 2)],
          ["日胜率", pct(_perf(t5, 'rolling')['daily_win_rate'], 1), pct(_perf(t10, 'rolling')['daily_win_rate'], 1)],
          ["期末权益", money(_perf(t5, 'rolling')['final_equity']), money(_perf(t10, 'rolling')['final_equity'])],
          ["完成回合", num(_trade_metrics(t5, 'rolling')['n_round_trips'], 0), num(_trade_metrics(t10, 'rolling')['n_round_trips'], 0)],
          ["单笔净收益", pct(_trade_metrics(t5, 'rolling')['avg_trade_return'], 2, True), pct(_trade_metrics(t10, 'rolling')['avg_trade_return'], 2, True)],
          ["单笔净胜率", pct(_trade_metrics(t5, 'rolling')['trade_win_rate'], 1), pct(_trade_metrics(t10, 'rolling')['trade_win_rate'], 1)],
          ["总费用", money(_trade_metrics(t5, 'rolling')['total_fees']), money(_trade_metrics(t10, 'rolling')['total_fees'])],
          ["换手（倍初始资金）", num(_trade_metrics(t5, 'rolling')['turnover_x'], 1), num(_trade_metrics(t10, 'rolling')['turnover_x'], 1)],
          ["顺延 / 强制退出", f"{num(_trade_metrics(t5, 'rolling')['deferred_exits'], 0)} / {num(_trade_metrics(t5, 'rolling')['forced_exits'], 0)}", f"{num(_trade_metrics(t10, 'rolling')['deferred_exits'], 0)} / {num(_trade_metrics(t10, 'rolling')['forced_exits'], 0)}"],
      ],
      aligns=["l", "r", "r"],
  )}

  <h3>4.3 方向胜率：毛收益与净收益</h3>
  {table(
      ["组合", "回合数", "毛平均", "毛胜率", "净平均", "净胜率", "中位数", "盈亏比", "平均持有"],
      direction_rows,
      aligns=["l", "r", "r", "r", "r", "r", "r", "r", "r"],
  )}
  <div class="table-note">本表是回合交易层面的方向胜率（单笔净收益 &gt; 0 的比例），不是全市场逐日方向胜率；后者见第 2 节。毛收益不含费用，净收益扣除佣金后的结果。T+10 滚动的毛胜率仅 46.05%，净胜率 45.93%，但平均盈利远大于平均亏损。</div>

  {figure(paths["trade_dist"], "滚动 Top5 单笔净收益分布。红柱为正收益，绿柱为负收益；实线为均值，虚线为中位数。", 5)}
  {figure(paths["monthly"], "四组组合的月度收益。米色底纹标出 5–6 月连续回撤区间。", 6)}

  <h3>4.4 解读</h3>
  <ul>
    <li><strong>T+5 滚动跑输 T+5 Top20。</strong> 总收益 {pct(_perf(t5, 'rolling')['total_return'], 2, True)} vs {pct(_perf(t5, 'top20')['total_return'], 2, True)}；滚动同时持有最多 25 只，换手 {num(_trade_metrics(t5, 'rolling')['turnover_x'], 1)} 倍，单笔净收益只有 {pct(t5.stats_rolling['net_avg'], 2, True)}。分散降低了单票冲击，也稀释了尾部收益。</li>
    <li><strong>T+10 滚动跑赢 T+10 Top20。</strong> 总收益 {pct(_perf(t10, 'rolling')['total_return'], 2, True)} vs {pct(_perf(t10, 'top20')['total_return'], 2, True)}；10 个桶平滑了建仓时点，日胜率 {pct(_perf(t10, 'rolling')['daily_win_rate'], 1)} 高于 Top20 的 {pct(_perf(t10, 'top20')['daily_win_rate'], 1)}。</li>
    <li><strong>低胜率、正偏态。</strong> T+5 滚动净胜率 {pct(t5.stats_rolling['net_win'], 1)}，盈亏比 {num(t5.stats_rolling['payoff'])}；T+10 滚动净胜率 {pct(t10.stats_rolling['net_win'], 1)}，盈亏比 {num(t10.stats_rolling['payoff'])}。中位数都为负，意味着「大多数交易小亏，少数交易大赚」；执行上必须能承受连续小亏。</li>
    <li><strong>成本敏感。</strong> 滚动 T+5 换手 {num(_trade_metrics(t5, 'rolling')['turnover_x'], 1)} 倍，总费用 {money(_trade_metrics(t5, 'rolling')['total_fees'])} 元；加 5bp 滑点后总收益从 {pct(_perf(t5, 'rolling')['total_return'], 2, True)} 降到 {pct(_variants(t5, 'rolling')['slippage_5bp']['performance']['total_return'], 2, True)}。这不是一个可以忽略执行细节的策略。</li>
  </ul>
</section>
"""


def costs(bundle: ReportBundle, paths: dict[str, Path]) -> str:
    t5 = bundle.models["T5"]
    t10 = bundle.models["T10"]
    def ablation_rows(kind: str) -> list[list[str]]:
        rows = []
        for key, label in VARIANTS:
            t5_perf = _variants(t5, kind)[key]["performance"]
            t10_perf = _variants(t10, kind)[key]["performance"]
            rows.append(
                [
                    label,
                    pct(t5_perf["total_return"], 2, True),
                    num(t5_perf["sharpe"]),
                    pct(t5_perf["max_drawdown"], 2),
                    pct(t10_perf["total_return"], 2, True),
                    num(t10_perf["sharpe"]),
                    pct(t10_perf["max_drawdown"], 2),
                ]
            )
        return rows
    gate_rows = []
    for report in (t5, t10):
        gates = report.metrics["signal_gate_counts"]
        gate_rows.append(
            [
                report.title,
                num(gates["top20_rows"], 0),
                num(gates["st"], 0),
                num(gates["limit_up"], 0),
                num(gates["limit_down"], 0),
                num(gates["suspended"], 0),
                num(gates["new"], 0),
            ]
        )
    return f"""
<section class="section page-break">
  {section_head("05", "成本、过滤与消融", "哪些假设真正影响结果")}
  <p class="lead">回测里最容易「看起来很美」的地方，是费用和过滤。本节把成本逐项加回去，并把 ST、涨跌停、次新过滤逐个拿掉，观察结果变化。注意：消融不是因果推断，尤其 ST 过滤带有前视，不能把差异直接解释为「ST 过滤创造超额」。</p>

  <h3>5.1 成本敏感性</h3>
  {figure(paths["cost"], "三档成本下的区间总收益：双边万 3、加卖出印花税 5bp、加双边滑点 5bp。", 7)}
  {table(
      ["组合", "基准", "加印花税 5bp", "加滑点 5bp", "印花税影响", "滑点影响"],
      [
          [
              label,
              pct(_variants(report, kind)["baseline"]["performance"]["total_return"], 2, True),
              pct(_variants(report, kind)["stamp_duty_5bp"]["performance"]["total_return"], 2, True),
              pct(_variants(report, kind)["slippage_5bp"]["performance"]["total_return"], 2, True),
              pct(_variants(report, kind)["stamp_duty_5bp"]["performance"]["total_return"] - _variants(report, kind)["baseline"]["performance"]["total_return"], 2, True),
              pct(_variants(report, kind)["slippage_5bp"]["performance"]["total_return"] - _variants(report, kind)["baseline"]["performance"]["total_return"], 2, True),
          ]
          for label, report, kind in (
              ("T+5 Top20", t5, "top20"),
              ("T+5 滚动", t5, "rolling"),
              ("T+10 Top20", t10, "top20"),
              ("T+10 滚动", t10, "rolling"),
          )
      ],
      aligns=["l", "r", "r", "r", "r", "r"],
  )}
  <div class="table-note">印花税按卖出金额 5bp；滑点按买入上浮 5bp、卖出下浮 5bp。每 1 倍换手的显性佣金约 6bp（双边万 3），与台账费用 / 换手 / 初始资金的计算一致。</div>

  <h3>5.2 Top20 消融</h3>
  {table(
      ["变体", "T+5 总收益", "T+5 Sharpe", "T+5 最大回撤", "T+10 总收益", "T+10 Sharpe", "T+10 最大回撤"],
      ablation_rows("top20"),
      aligns=["l", "r", "r", "r", "r", "r", "r"],
  )}

  <h3>5.3 滚动 Top5 消融</h3>
  {table(
      ["变体", "T+5 总收益", "T+5 Sharpe", "T+5 最大回撤", "T+10 总收益", "T+10 Sharpe", "T+10 最大回撤"],
      ablation_rows("rolling"),
      aligns=["l", "r", "r", "r", "r", "r", "r"],
  )}
  <div class="table-note">「去掉涨跌停过滤」与「去掉次新过滤」在两组结果中都与基准相同，因为信号日命中的涨跌停只有 3 行、次新为 0 行；但入场日的涨跌停拦截在所有变体中始终生效。ST 消融方向不一致：T+5 Top20 去掉 ST 后变差，T+5 滚动和 T+10 去掉 ST 后也变差或小幅变化，不能据此判断 ST 过滤的因果贡献。</div>

  <h3>5.4 信号日过滤计数</h3>
  {table(["模型", "原始 Top20 行", "ST", "涨停", "跌停", "停牌", "次新"], gate_rows, aligns=["l", "r", "r", "r", "r", "r", "r"])}
  <div class="table-note">计数基于信号日原始 Top20 行；真正的「买不到」发生在入场日，已在组合台账中按 <code>entry_blocked</code> 记录并跳过。</div>

  <h3>5.5 解读</h3>
  <ul>
    <li><strong>滑点比印花税更伤。</strong> 对 T+5 Top20，5bp 滑点把总收益从 {pct(_perf(t5, 'top20')['total_return'], 2, True)} 拉到 {pct(_variants(t5, 'top20')['slippage_5bp']['performance']['total_return'], 2, True)}（{pct(_variants(t5, 'top20')['slippage_5bp']['performance']['total_return'] - _perf(t5, 'top20')['total_return'], 2, True)}）；对 T+5 滚动，拉到 {pct(_variants(t5, 'rolling')['slippage_5bp']['performance']['total_return'], 2, True)}（{pct(_variants(t5, 'rolling')['slippage_5bp']['performance']['total_return'] - _perf(t5, 'rolling')['total_return'], 2, True)}）。高换手策略对执行质量非常敏感。</li>
    <li><strong>ST 过滤不能当作超额来源。</strong> 基准使用了 2026-10-16 的静态 ST 名单，包含未来信息；去掉 ST 过滤的变化方向在不同策略上不一致。正确做法是接入逐日 ST 标记，再做一次干净的消融。</li>
    <li><strong>涨跌停过滤在本样本中不是主要变量。</strong> 信号日涨跌停各只有 3 行；但入场日的拦截仍然存在（T+5 Top20 跳过 5 笔，T+5 滚动跳过 8 笔，T+10 Top20 跳过 2 笔，T+10 滚动跳过 10 笔）。实盘中这部分会随市场状态变化，不能因为回测里少就删掉。</li>
    <li><strong>次新过滤在本样本中为空。</strong> 不是过滤器无效，而是新上市股票还没进入因子源；实盘接入新股票后，这条过滤器会重新变得重要。</li>
  </ul>
</section>
"""


def risks(bundle: ReportBundle) -> str:
    t5 = bundle.models["T5"]
    t10 = bundle.models["T10"]
    return f"""
<section class="section page-break">
  {section_head("06", "风险、局限与结论", "哪些结论可以信，哪些不能")}
  <p class="lead">这份报告的证据是充分的，但边界也很清楚。下面按影响从大到小列出局限，并给出「结论在什么情况下会被推翻」。读者应把本报告理解为研究口径下的历史回测，而不是实盘业绩。</p>

  <h3>6.1 主要局限</h3>
  {table(
      ["局限", "影响", "严重度", "缓解方式"],
      [
          ["ST 静态名单", "用 2026-10-16 的名单回填全区间，含前视；ST 的涨跌停规则也不同", "高", "接入逐日 ST / 风险警示标记，按当日状态过滤"],
          ["生存者偏差", "股票池来自当前因子源，退市股票可能缺失", "中高", "纳入退市股票与历史成分股"],
          ["收盘成交、无冲击", "按收盘价成交，未建模盘中冲击与排队", "中", "盘中 VWAP / 限价单 / 参与率模型"],
          ["外推边界 2026-09-14", "9 月 15 日后因子分区不完整，样本被截断", "中", "等 <code>gap_mined</code> 分区完整后回填"],
          ["单一 2026 窗口", "只覆盖一种市场状态，不能外推", "中", "2023–2026 滚动窗口 / 多年度 walk-forward"],
          ["模型族与特征选择", "300 维特征来自既有研究，存在选择偏差", "中", "预注册模型与特征，避免用测试集调参"],
          ["复权与分红", "训练用后复权、组合用前复权；分红现金流未单列", "低中", "核对除权除息与分红再投资口径"],
          ["容量与流动性", "2 百万元规模不构成约束，大资金会", "中", "设置单票参与率上限、分批建仓"],
      ],
      aligns=["l", "l", "c", "l"],
  )}

  <h3>6.2 结论在什么情况下会被推翻</h3>
  <ul class="checklist">
    <li><strong>逐日 ST 数据加入后，ST 过滤的收益贡献消失。</strong> 这是最可能改变结论的一项；当前基准的 ST 过滤含前视。</li>
    <li><strong>执行成本从 3bp 升到 10bp 以上。</strong> 滚动 Top5 对滑点非常敏感；成本再高一档，优势可能消失。</li>
    <li><strong>2023–2025 的滚动窗口不复现 2026 的结果。</strong> 单一 2026 窗口不足以证明策略在不同市场状态下都有效。</li>
    <li><strong>模型在 2026 年 9 月后继续衰减。</strong> 月度 IC 在 9 月已降到 {pct(t5.monthly_quality.iloc[-1]['ic_eligible'])} / {pct(t10.monthly_quality.iloc[-1]['ic_eligible'])}；需要持续监控。</li>
  </ul>

  <h3>6.3 下一步建议</h3>
  <div class="three-col">
    <div>
      {callout("数据", "<p>接入逐日 ST、退市股票与历史成分股；把生存者偏差和前视偏差从回测里拿掉。</p>", "neutral")}
    </div>
    <div>
      {callout("验证", "<p>做 2023–2026 的滚动窗口；每年重新训练、重新回测，观察结论是否稳定。</p>", "neutral")}
    </div>
    <div>
      {callout("执行", "<p>加入 VWAP / 限价单 / 参与率模型；把滑点从敏感性分析变成执行约束。</p>", "neutral")}
    </div>
  </div>

  <div class="callout" style="margin-top:6mm">
    <div class="callout-title">结论</div>
    <p><strong>模型有信号，组合有超额，但绝对收益有限、路径不平滑。</strong> T+5 Top20 是绝对收益最高的一组（{pct(_perf(t5, 'top20')['total_return'], 2, True)}），T+10 滚动的风险收益比更均衡（Sharpe {num(_perf(t10, 'rolling')['sharpe'])}）。四组组合都跑赢等权可交易池，但都经历了 18%–21% 的最大回撤，且大部分交易日处于水下。</p>
    <p>在当前数据条件下，最值得继续投入的不是「再调一次模型」，而是把 ST 前视、退市股票、执行成本和容量约束补齐；这些因素对结论的影响，大于模型参数的小幅调整。</p>
  </div>
</section>
"""


def appendix(bundle: ReportBundle) -> str:
    t5 = bundle.models["T5"]
    t10 = bundle.models["T10"]
    artifact_rows = [
        ["pred_panel_T5.parquet / pred_panel_T10.parquet", "训练 pred + 外推的逐股预测面板"],
        ["pred_extend_T5.parquet / pred_extend_T10.parquet", "2026-08-28 ~ 2026-09-14 外推"],
        ["quality_daily_*.csv / quality_monthly_*.csv", "逐日与月度预测质量"],
        ["top20_*_trades.csv / top20_*_equity.csv", "Top20 组合台账与净值"],
        ["rolling_*_trades.csv / rolling_*_equity.csv / rolling_*_skips.csv", "滚动 Top5 台账、净值与跳过记录"],
        ["per_stock_*.csv", "逐股覆盖与最新预测"],
        ["metrics_T5.json / metrics_T10.json", "全部指标与消融"],
        ["report_2026_topk.md / report_2026_topk.pdf", "Markdown 与 PDF 报告"],
    ]
    return f"""
<section class="section page-break">
  {section_head("07", "附录", "术语、复现与产物")}
  <h3>7.1 术语表</h3>
  {table(
      ["术语", "含义"],
      [
          ["IC", "截面秩相关：模型分数排序与实际收益排序的一致性；范围 -1 到 1。"],
          ["ICIR", "IC 均值 / IC 标准差；本报告未年化。"],
          ["方向胜率", "全可交易池中，预测符号与实际收益符号一致的比例。"],
          ["Top20 命中率", "Top20 中实际收益为正的股票占比。"],
          ["多空", "Top20 平均收益减 Bottom20 平均收益；A 股融券受限，不能直接复制。"],
          ["Sharpe", "年化收益 / 年化波动；无风险利率按 0 处理。"],
          ["最大回撤", "净值从峰值到谷值的最大跌幅。"],
          ["水下时间", "净值低于前高的交易日占比。"],
          ["换手", "累计买入金额 / 初始资金；本报告未做年化。"],
          ["盈亏比", "平均盈利 / 平均亏损的绝对值。"],
          ["利润因子", "总盈利 / 总亏损的绝对值。"],
          ["顺延退出", "到期日因跌停或停牌无法卖出，延后到可成交日。"],
      ],
      aligns=["l", "l"],
  )}

  <h3>7.2 复现命令</h3>
  <pre># 1) 生成外推（容器内；自动复用模型目录的 inference.py）
python -m backend.scripts.backtest2026.extend_predictions --runner docker

# 2) 重跑回测与指标（容器内）
docker exec quantmind python -m backend.scripts.backtest2026.runner

# 3) 生成 PDF（宿主机；需要 matplotlib / playwright / pypdf）
python -m backend.scripts.backtest2026.report_pdf \\
  --artifacts data/backtest_2026 \\
  --out data/backtest_2026/report_2026_topk.pdf</pre>

  <h3>7.3 产物清单</h3>
  {table(["文件", "内容"], artifact_rows, aligns=["l", "l"])}

  <h3>7.4 关键数字速查</h3>
  {table(
      ["指标", "T+5", "T+10"],
      [
          ["Top20 总收益", pct(_perf(t5, 'top20')['total_return'], 2, True), pct(_perf(t10, 'top20')['total_return'], 2, True)],
          ["Top20 最大回撤", pct(_perf(t5, 'top20')['max_drawdown'], 2), pct(_perf(t10, 'top20')['max_drawdown'], 2)],
          ["滚动 Top5 总收益", pct(_perf(t5, 'rolling')['total_return'], 2, True), pct(_perf(t10, 'rolling')['total_return'], 2, True)],
          ["滚动 Top5 最大回撤", pct(_perf(t5, 'rolling')['max_drawdown'], 2), pct(_perf(t10, 'rolling')['max_drawdown'], 2)],
          ["滚动 Top5 单笔净胜率", pct(t5.stats_rolling['net_win'], 1), pct(t10.stats_rolling['net_win'], 1)],
          ["滚动 Top5 盈亏比", num(t5.stats_rolling['payoff']), num(t10.stats_rolling['payoff'])],
      ],
      aligns=["l", "r", "r"],
  )}

  <div class="footer-note">
    <strong>免责声明：</strong>本报告仅供学习研究与技术演示，不构成任何投资建议。报告中的分析、评分与交易信号均由程序自动生成，可能存在错误或偏差。投资决策请咨询持有中国证监会颁发资质的专业机构。作者不对使用本工具产生的任何投资损失承担责任。股市有风险，投资需谨慎。
    <br><br>QuantMind Research · 报告编号 QM-R-2026-1019 · 生成日期 {date_cn(bundle.generated_at)}
  </div>
</section>
"""


def build_body(bundle: ReportBundle, paths: dict[str, Path]) -> str:
    return "".join(
        [
            executive_summary(bundle),
            method(bundle),
            quality(bundle, paths),
            top20(bundle, paths),
            rolling(bundle, paths),
            costs(bundle, paths),
            risks(bundle),
            appendix(bundle),
        ]
    )
