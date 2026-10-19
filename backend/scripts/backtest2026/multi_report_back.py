"""多模型报告后半部分：组合、严格检验、风险、结论与附录。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from .multi_report_data import (
    HORIZON_TITLES,
    SHORT_FAMILY,
    MultiBundle,
    display_name,
)
from .report_pdf_html import (
    callout,
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


def _ranked_portfolio(
    bundle: MultiBundle, horizon: int, family: str
) -> pd.DataFrame | None:
    frame = bundle.portfolio_ranking.get((horizon, family))
    if frame is None or frame.empty:
        return None
    return frame.sort_values("rank")


def _portfolio_rows(frame: pd.DataFrame) -> list[list[str]]:
    rows = []
    for row in frame.itertuples():
        rows.append(
            [
                int(row.rank),
                display_name(str(row.model)),
                f'<span class="{tone(row.total_return)}">{pct(row.total_return, 2, True)}</span>',
                pct(row.annual_return, 2, True),
                f'<span class="{tone(row.active_return)}">{pct(row.active_return, 2, True)}</span>',
                num(row.information_ratio, 2),
                num(row.sharpe, 2),
                pct(row.max_drawdown, 2),
                pct(row.daily_win_rate, 1),
                pct(row.trade_win_rate, 1),
                money(row.n_round_trips, 0),
                num(row.dsr, 3),
                num(row.q_excess, 4),
            ]
        )
    return rows


def portfolio_section(bundle: MultiBundle, chart_paths: dict[str, Path]) -> str:
    blocks = []
    for horizon in (5, 10):
        family_blocks = []
        for family in FAMILIES:
            frame = _ranked_portfolio(bundle, horizon, family)
            if frame is None:
                family_blocks.append(f"<p>T+{horizon} {SHORT_FAMILY[family]} 尚无结果。</p>")
                continue
            best = frame.iloc[0]
            stats = bundle.portfolio_stats(horizon, family)
            white = stats.get("white_reality_check", {})
            pbo = stats.get("pbo", {})
            stability = stats.get("stability", {})
            spread = stats.get("spread", {})
            family_blocks.append(
                f"""
  <h4>{SHORT_FAMILY[family]}：{pct(best['total_return'], 2, True)} 领跑，13 模型中位数 {pct(frame['total_return'].median(), 2, True)}</h4>
  {table(
      ["#", "模型", "总收益", "年化", "超额", "IR", "Sharpe", "最大回撤", "日胜率", "单笔净胜率", "完整轮次", "DSR", "q(超额)"],
      _portfolio_rows(frame),
      aligns=["c", "l", "r", "r", "r", "r", "r", "r", "r", "r", "r", "r", "r"],
      raw=True,
  )}
  <p class="small">超额 = 组合收益 − 同一成交时点等权池收益。IR 为日超额均值 / 日超额标准差 × √252。DSR 为 26 次试验校正后的 Deflated Sharpe；q(超额) 为块自助单边 p 值的 BH 校正值。最佳与最差总收益差 {pct(spread.get('best_worst_total_return'), 2)}，最佳与最差 Sharpe 差 {num(spread.get('best_worst_sharpe'), 2)}。上半段 / 下半段收益排名相关 {num(stability.get('first_second_half_rank_corr'), 2)}；两段均为正的模型占比 {pct(stability.get('both_halves_positive_ratio'), 1)}。White RC p = {num(white.get('p'), 4)}，PBO = {pct(pbo.get('pbo'), 1)}。</p>
"""
            )
        blocks.append(
            f"""
  <h3>4.{1 if horizon == 5 else 2} {HORIZON_TITLES[horizon]}</h3>
  {''.join(family_blocks)}
"""
        )

    variants_rows = []
    for horizon in (5, 10):
        for family in FAMILIES:
            frame = bundle.portfolio_ranking.get((horizon, family))
            if frame is None or frame.empty:
                continue
            baseline = frame["total_return"].median()
            stamp = []
            slippage = []
            best_model = str(frame.sort_values("rank").iloc[0]["model"])
            best_payload = bundle.portfolio_summary(horizon, family, best_model)
            variants = best_payload.get("variants", {})
            for model in frame["model"]:
                payload = bundle.portfolio_summary(horizon, family, str(model))
                variant = payload.get("variants", {})
                if "stamp_duty_5bp" in variant:
                    stamp.append(variant["stamp_duty_5bp"]["total_return"])
                if "slippage_5bp" in variant:
                    slippage.append(variant["slippage_5bp"]["total_return"])
            variants_rows.append(
                [
                    f"T+{horizon}",
                    SHORT_FAMILY[family],
                    pct(baseline, 2, True),
                    pct(np.median(stamp) if stamp else float("nan"), 2, True),
                    pct(np.median(slippage) if slippage else float("nan"), 2, True),
                    esc(display_name(best_model)),
                    pct(best_payload.get("performance", {}).get("total_return"), 2, True),
                    pct(variants.get("stamp_duty_5bp", {}).get("total_return"), 2, True),
                    pct(variants.get("slippage_5bp", {}).get("total_return"), 2, True),
                ]
            )

    return f"""
<section class="section page-break">
  {section_head("04", "组合收益：Top10、Top20 与滚动 Top5", "200 万本金，整手交易，费用后口径")}
  <p class="section-deck">组合收益与预测质量不是同一件事。预测 IC 只衡量排序；组合还要经过资金约束、整手取整、涨跌停无法成交、费用与持有期。以下所有组合共用同一基准与同一交易日历。</p>
  {figure(chart_paths['returns'], "三种组合的总收益：红点为 T+5，蓝点为 T+10。Top10 / Top20 为单批轮动，滚动 Top5 为每日建仓。", 5) if 'returns' in chart_paths else ''}
  {''.join(blocks)}

  <h3>4.3 费用敏感性</h3>
  <p>基准为双边 3bp 佣金。另加压两种情形：卖出加 5bp 印花税、双边加 5bp 滑点。下表同时给出 13 个模型的中位数与最佳模型的具体变化。</p>
  {table(
      ["周期", "组合", "基准中位数", "加印花税中位数", "加滑点中位数", "最佳模型", "该模型基准", "该模型加印花税", "该模型加滑点"],
      variants_rows,
      aligns=["l", "l", "r", "r", "r", "l", "r", "r", "r"],
      raw=True,
  )}
  <p class="small">成本敏感性的绝对值取决于换手：滚动 Top5 每天换 5 只，受滑点影响最大；Top10 / Top20 的持有期更长，换手更低。印花税只在卖出端收取，因此对高换手组合的拖累约为滑点的一半。</p>

  {figure(chart_paths['equity_5_top10'], "T+5 Top10：收益前 5 的模型净值曲线与等权基准。起点归一为 1。", 6) if 'equity_5_top10' in chart_paths else ''}
  {figure(chart_paths['equity_10_top10'], "T+10 Top10：收益前 5 的模型净值曲线与等权基准。", 7) if 'equity_10_top10' in chart_paths else ''}
  {figure(chart_paths['equity_5_rolling'], "T+5 滚动 Top5：收益前 5 的模型净值曲线。滚动组合每日建仓，曲线更平滑。", 8) if 'equity_5_rolling' in chart_paths else ''}
  {figure(chart_paths['equity_10_rolling'], "T+10 滚动 Top5：收益前 5 的模型净值曲线。持有期更长，批次重叠更多。", 9) if 'equity_10_rolling' in chart_paths else ''}
  {figure(chart_paths['monthly_5'], "T+5 Top10 月度收益。看颜色是否长期单向，以及是否由单月贡献大部分收益。", 10) if 'monthly_5' in chart_paths else ''}
  {figure(chart_paths['monthly_10'], "T+10 Top10 月度收益。与 T+5 对照，判断收益是否依赖单一持有期。", 11) if 'monthly_10' in chart_paths else ''}
</section>
"""


def _friedman_rows(bundle: MultiBundle, horizon: int) -> list[list[str]]:
    rows = []
    quality = bundle.quality_stats(horizon).get("friedman", {}).get("ic_eligible", {})
    if quality:
        rows.append(
            [
                "预测 IC",
                num(quality.get("chi2"), 2),
                num(quality.get("p"), 4),
                num(quality.get("kendall_w"), 3),
                money(quality.get("n_days", 0)),
            ]
        )
    labels = {
        "top10": "Top10",
        "top20": "Top20",
        "rolling": "滚动 Top5",
    }
    for family in FAMILIES:
        stats = bundle.portfolio_stats(horizon, family)
        for key, label in (("friedman_returns", "日收益"), ("friedman_excess", "日超额")):
            payload = stats.get(key, {})
            if not payload:
                continue
            rows.append(
                [
                    f"{labels[family]} · {label}",
                    num(payload.get("chi2"), 2),
                    num(payload.get("p"), 4),
                    num(payload.get("kendall_w"), 3),
                    money(payload.get("n_days", 0)),
                ]
            )
    return rows


def _white_rows(bundle: MultiBundle, horizon: int) -> list[list[str]]:
    rows = []
    for family in FAMILIES:
        stats = bundle.portfolio_stats(horizon, family)
        white = stats.get("white_reality_check", {})
        pbo = stats.get("pbo", {})
        if not white:
            continue
        rows.append(
            [
                SHORT_FAMILY[family],
                esc(display_name(str(white.get("best_model", "")))),
                num(white.get("stat"), 4),
                num(white.get("p"), 4),
                pct(pbo.get("pbo"), 1),
                money(pbo.get("n_combos", 0)),
            ]
        )
    return rows


def _pairwise_rows(bundle: MultiBundle, horizon: int) -> list[list[str]]:
    rows = []
    quality = bundle.quality_stats(horizon)
    pairwise = quality.get("pairwise_ic", [])
    if pairwise:
        significant = sum(1 for row in pairwise if float(row.get("q", 1.0)) < 0.05)
        rows.append(
            [
                "预测 IC",
                esc(display_name(str(quality.get("best_ic_model", "")))),
                significant,
                len(pairwise),
                num(min(float(row.get("q", 1.0)) for row in pairwise), 4),
            ]
        )
    for family in FAMILIES:
        stats = bundle.portfolio_stats(horizon, family)
        pairwise = stats.get("pairwise_returns", [])
        if not pairwise:
            continue
        significant = sum(1 for row in pairwise if float(row.get("q", 1.0)) < 0.05)
        rows.append(
            [
                f"{SHORT_FAMILY[family]} 日收益",
                esc(display_name(str(stats.get("best_total_return_model", "")))),
                significant,
                len(pairwise),
                num(min(float(row.get("q", 1.0)) for row in pairwise), 4),
            ]
        )
    return rows


def _paired_rows(bundle: MultiBundle) -> list[list[str]]:
    rows = []
    for row in bundle.cross.get("paired_differences", []):
        rows.append(
            [
                esc(display_name(str(row.get("model", "")))),
                num(row.get("delta_ic"), 4),
                pct(row.get("delta_top10_hit"), 2, True),
                pct(row.get("delta_top10_return"), 2, True),
                pct(row.get("delta_rolling_return"), 2, True),
            ]
        )
    return rows


def stats_section(bundle: MultiBundle, chart_paths: dict[str, Path]) -> str:
    blocks = []
    for horizon in (5, 10):
        blocks.append(
            f"""
  <h3>5.{1 if horizon == 5 else 2} {HORIZON_TITLES[horizon]}：整体差异与冠军可信度</h3>
  {table(
      ["比较对象", "Friedman 卡方", "p 值", "Kendall W", "有效天数"],
      _friedman_rows(bundle, horizon),
      aligns=["l", "r", "r", "r", "r"],
  )}
  <h4>White Reality Check 与 PBO</h4>
  {table(
      ["组合", "White RC 最佳模型", "最佳统计量", "White RC p", "PBO", "组合数"],
      _white_rows(bundle, horizon),
      aligns=["l", "l", "r", "r", "r", "r"],
  )}
  <p class="small">White RC 的原假设是“所有模型都不优于随机”；p 小才说明“最佳模型”有统计支持。PBO 估计样本内最优模型在样本外落到中位数以下的概率；低于 0.5 表示选模过程相对稳健，高于 0.5 表示过拟合风险大于抛硬币。</p>
  <h4>冠军与其余模型的配对检验</h4>
  {table(
      ["比较", "冠军", "显著更优的模型数", "比较总数", "最小 q 值"],
      _pairwise_rows(bundle, horizon),
      aligns=["l", "l", "r", "r", "r"],
  )}
  <p class="small">配对 Wilcoxon 使用逐日序列，BH 校正覆盖冠军对其余全部模型的多重比较。q &lt; 0.05 才计为“显著更优”；若显著数为 0，冠军与其余模型的差距不能被当前样本区分。</p>
  <p class="small"><strong>三层检验各回答一个问题，不能互相替代：</strong></p>
  <ul class="small">
    <li><strong>整体：</strong>Friedman 检验 13 个模型作为一个整体是否同质。p 不显著通常意味着样本不足以区分，而不是模型等价；只有 p 小且 Kendall W 不接近 0，才说明排序差异是系统性的。</li>
    <li><strong>冠军：</strong>White RC 与 PBO 回答“收益最高的模型是否只是 26 次挑选里撞上的运气”。前者校正“挑最大值”的选择偏差，后者估计样本内最优在样本外跌回中位数的概率。</li>
    <li><strong>配对：</strong>Wilcoxon 回答“冠军与其余模型的差距是否可区分”。若显著数为 0，冠军只能算“这批样本里的第一名”，不能写成“优于其他模型”。</li>
  </ul>
"""
        )

    rank_rows = []
    quality_corr = (bundle.cross.get("quality_rank_corr") or {})
    for metric, value in quality_corr.items():
        rank_rows.append(["IC / 质量指标", str(metric), num(value, 3)])
    portfolio_corr = (bundle.cross.get("portfolio_rank_corr") or {})
    for family, payload in portfolio_corr.items():
        rank_rows.append(
            [
                f"{SHORT_FAMILY.get(family, family)} 总收益",
                str(family),
                num(payload.get("total_return"), 3),
            ]
        )
    return f"""
<section class="section page-break">
  {section_head("05", "模型差异的严格检验", "整体差异、冠军可信度、跨周期一致性")}
  <p class="section-deck">这一章回答“都没区别”到底成不成立。注意：预测层面的检验功效高（157 个交易日 × 数千只股票），组合层面的检验功效低（Top10 / Top20 只有 13 至 22 个独立轮次）。两层结论必须分开看。</p>
  {''.join(blocks)}

  <h3>5.3 跨周期一致性</h3>
  {figure(chart_paths['cross'], "T+5 与 T+10 的模型排名对照。左图为 IC，右图为 Top10 总收益；虚线为 y = x。", 12) if 'cross' in chart_paths else ''}
  {table(
      ["指标", "内部键", "Spearman 秩相关"],
      rank_rows,
      aligns=["l", "l", "r"],
  )}
  <p class="small">秩相关接近 1：两个持有期选出的模型顺序几乎相同；接近 0：T+5 的结论不能外推到 T+10；为负：两个周期互相矛盾。样本只有 13 个模型，秩相关的置信区间很宽，不宜过度解读小数点。</p>
  <p class="small">区分两件事：<strong>预测排序</strong>在 T+5 与 T+10 之间高度一致（IC 秩相关 {num(quality_corr.get('ic_mean'), 3)}），但<strong>组合收益</strong>的排名并不一致（Top10 秩相关 {num((portfolio_corr.get('top10') or {}).get('total_return'), 3)}）。原因在于组合只取头部少数股票，收益由少数大赢家决定，持有期一换，赢家名单就换；IC 用全截面，统计上稳定得多。因此选模型可以主要看 IC，配置组合必须按持有期分别验证。</p>
</section>
"""


def risk_section(bundle: MultiBundle) -> str:
    manifest = bundle.summary.get("manifest", {})
    start = manifest.get("common_start", "—")
    end = manifest.get("common_end", "—")
    pending = bundle.pending()
    pending_text = (
        "、".join(f"T+{int(row.horizon)} {row.key}" for row in pending.itertuples())
        if not pending.empty
        else "无"
    )
    return f"""
<section class="section page-break">
  {section_head("06", "风险、局限与不该下的结论", "把回测能做和不能做的事写清楚")}

  <h3>6.1 样本窗口</h3>
  <p>{esc(start)} 至 {esc(end)} 只有约 8 个月，覆盖的是单一市场环境。若 2026 年整体偏成长或偏小盘，所有模型都会共享同一风格暴露；
  “模型 A 比模型 B 高 2 个百分点”很可能只是风格暴露的细微差别，而不是选股能力。
  本报告没有做跨市场状态（牛 / 熊 / 震荡）的分层回测，不能据此断言模型在 2018 年式下跌中同样有效。</p>

  <h3>6.2 数据与执行假设</h3>
  <ul>
    <li><strong>静态 ST 名单：</strong>使用 2026-10-16 的 ST 状态回填全窗口，存在前视偏差；可能把后来的 ST 股从年初就剔除，收益偏乐观。</li>
    <li><strong>收盘成交：</strong>信号在 t 日收盘生成、t+1 日收盘成交。真实盘中若无法在收盘价附近完成，滑点会高于报告的 5bp 敏感性。</li>
    <li><strong>涨跌停与停牌：</strong>入场日涨停 / 跌停 / 停牌直接放弃该股票；退出日无法卖出时顺延，最多 10 个交易日，之后按最近价格强平。真实交易中“跌停排队卖不出”可能更严重。</li>
    <li><strong>容量：</strong>没有按成交额限制单票买入金额。200 万本金对多数 A 股流动性不构成约束，但放大到数千万时，Top10 集中持仓会显著冲击价格。</li>
    <li><strong>深度模型的标签：</strong>TFT、GRU、LSTM、ALSTM、TCN、Transformer、TabNet 的训练产物只落排序目标，没有真实收益列。本报告只用它们的预测分数，不用其标签；这不影响 IC 与组合评估。</li>
    <li><strong>未就绪组合：</strong>{esc(pending_text)} 尚未训练完成，榜单中留空，不用其他周期的结果替代。</li>
  </ul>

  <h3>6.3 统计上的边界</h3>
  <ul>
    <li>13 个模型 × 2 个周期 = 26 次“选第一”。即使全部模型都没有真实能力，单纯挑最大值也会得到正收益；DSR、White RC 与 PBO 就是为这个问题服务的。</li>
    <li>Friedman 显著只说明“至少一个模型不同”，不保证冠军在未来继续领先。配对 Wilcoxon 的显著数才是冠军与其余模型的可区分程度。</li>
    <li>Top10 / Top20 的独立轮次少，组合层面的 p 值天然偏大；不能因为“不显著”就断言模型无效，也不能因为“总收益高”就断言模型有效。</li>
    <li>十分位单调性与 IC 高度相关，不是独立证据；两者同时好，只能说明排序在样本内一致。</li>
  </ul>
  {callout(
      "不该下的结论",
      "<p>“某个模型 2026 年收益最高，所以明年也买它”不成立；“IC 显著大于 0，所以扣费后一定赚钱”不成立；“T+5 比 T+10 好，所以只做 T+5”也不成立——两个周期的排名相关、费用与资金占用都不同。正确用法是把本报告当作模型筛选与风险预算的输入，而不是收益承诺。</p>",
      "warn",
  )}
</section>
"""


def conclusion_section(bundle: MultiBundle) -> str:
    best = {}
    for horizon in (5, 10):
        frame = bundle.quality_ranking.get(horizon)
        best[f"ic_{horizon}"] = (
            (str(frame.sort_values("rank").iloc[0]["model"]), float(frame.sort_values("rank").iloc[0]["ic_mean"]))
            if frame is not None and not frame.empty
            else ("—", float("nan"))
        )
        for family in FAMILIES:
            portfolio = bundle.portfolio_ranking.get((horizon, family))
            best[f"{family}_{horizon}"] = (
                (
                    str(portfolio.sort_values("rank").iloc[0]["model"]),
                    float(portfolio.sort_values("rank").iloc[0]["total_return"]),
                )
                if portfolio is not None and not portfolio.empty
                else ("—", float("nan"))
            )
    medians = {}
    for horizon in (5, 10):
        for family in FAMILIES:
            frame = bundle.portfolio_ranking.get((horizon, family))
            medians[(horizon, family)] = (
                float(frame["total_return"].median())
                if frame is not None and not frame.empty
                else float("nan")
            )
    stats5 = bundle.quality_stats(5)
    stats10 = bundle.quality_stats(10)
    friedman5 = stats5.get("friedman", {}).get("ic_eligible", {})
    friedman10 = stats10.get("friedman", {}).get("ic_eligible", {})
    white5 = bundle.portfolio_stats(5, "top10").get("white_reality_check", {})
    white10 = bundle.portfolio_stats(10, "top10").get("white_reality_check", {})
    pbo5 = bundle.portfolio_stats(5, "top10").get("pbo", {}).get("pbo", float("nan"))
    pbo10 = bundle.portfolio_stats(10, "top10").get("pbo", {}).get("pbo", float("nan"))
    quality_corr = (bundle.cross.get("quality_rank_corr") or {}).get("ic_mean", float("nan"))
    return f"""
<section class="section page-break">
  {section_head("07", "结论与使用建议", "按证据强度排序，不把统计显著等同于可交易")}

  <h3>7.1 直接回答</h3>
  <ol>
    <li><strong>上一版报告用的是什么模型？</strong> LightGBM 单模型。本报告覆盖 13 个模型 × 2 个持有期，共 24 个已就绪组合；T+10 的 TCN 与 Transformer 尚未完成，未纳入排名。</li>
    <li><strong>13 个模型有没有区别？</strong> 预测层面：T+5 Friedman p = {num(friedman5.get('p'), 4)}、W = {num(friedman5.get('kendall_w'), 3)}；T+10 p = {num(friedman10.get('p'), 4)}、W = {num(friedman10.get('kendall_w'), 3)}。组合层面：Top10 的 White RC p 分别为 {num(white5.get('p'), 4)} 与 {num(white10.get('p'), 4)}，PBO 为 {pct(pbo5, 1)} 与 {pct(pbo10, 1)}。请把“预测有差异”和“组合可区分”分开理解。</li>
    <li><strong>T+5 与 T+10 哪个好？</strong> IC 冠军分别为 {esc(display_name(best['ic_5'][0]))}（{num(best['ic_5'][1], 4)}）与 {esc(display_name(best['ic_10'][0]))}（{num(best['ic_10'][1], 4)}）；两周期 IC 排名相关 {num(quality_corr, 3)}。Top10 收益中位数：T+5 {pct(medians[(5, 'top10')], 2, True)}，T+10 {pct(medians[(10, 'top10')], 2, True)}。</li>
    <li><strong>Top10、Top20、滚动 Top5 怎么选？</strong> 收益中位数：T+5 分别为 {pct(medians[(5, 'top10')], 2, True)} / {pct(medians[(5, 'top20')], 2, True)} / {pct(medians[(5, 'rolling')], 2, True)}；T+10 分别为 {pct(medians[(10, 'top10')], 2, True)} / {pct(medians[(10, 'top20')], 2, True)} / {pct(medians[(10, 'rolling')], 2, True)}。Top10 弹性最大但批次最少、回撤最集中；滚动 Top5 更接近每日调仓，统计样本也更多。</li>
  </ol>

  <h3>7.2 按证据强度的建议</h3>
  {table(
      ["优先级", "建议", "依据", "反向风险"],
      [
          ["1", "把滚动 Top5 作为主策略形态", "每日建仓、批次多、可交易性最接近实盘；费用敏感性可量化", "换手高，滑点与冲击成本最敏感"],
          ["2", "用 IC + 十分位单调性筛模型，而不是只看总收益", "预测层面样本量大、检验功效高；组合总收益的轮次太少", "IC 稳定但费用后不赚钱的模型仍可能存在"],
          ["3", "Top10 只作为卫星仓位", "收益弹性最高，但单批集中、回撤大、独立轮次少", "一两批股票的运气决定全年排名"],
          ["4", "对冠军做 DSR / White RC / PBO 三重门槛", "26 次选模的选择偏差必须校正", "门槛通过也不保证未来有效"],
          ["5", "实盘前做滚动前推与容量测试", "本报告是固定窗口回测，不是前推验证", "参数、股票池、交易规则变化都会破坏结论"],
      ],
      aligns=["c", "l", "l", "l"],
  )}

  <h3>7.3 下一步建议</h3>
  <ul>
    <li>把 2026 年窗口按季度切开，重复第 5 章的 Friedman / White RC，观察冠军是否换人。</li>
    <li>对滚动 Top5 做逐日资金曲线归因，分离“选股”“行业暴露”“市值暴露”三部分。</li>
    <li>在真实成交额约束下重跑容量版本（单票不超过当日成交额的 1%），再看 Top10 是否仍有优势。</li>
    <li>T+10 TCN 与 Transformer 训练完成后，补齐 26 个组合并重跑本报告；当前缺失不影响其余 24 个组合的配对比较。</li>
  </ul>
</section>
"""


def appendix_section(bundle: MultiBundle) -> str:
    per_stock = bundle.per_stock
    status_counts = (
        per_stock["status"].value_counts().to_dict()
        if not per_stock.empty and "status" in per_stock.columns
        else {}
    )
    prediction_inventory = pd.DataFrame()
    inventory_path = bundle.out_dir / "prediction_inventory.csv"
    if inventory_path.is_file():
        prediction_inventory = pd.read_csv(inventory_path)
    exported = (
        prediction_inventory[prediction_inventory["status"] == "exported"]
        if not prediction_inventory.empty and "status" in prediction_inventory.columns
        else pd.DataFrame()
    )
    export_rows = int(exported["rows"].sum()) if not exported.empty else 0
    pred_missing = int(exported["pred_nan"].sum()) if not exported.empty else 0
    pred_missing_note = ""
    export_note = ""
    if export_rows:
        pred_missing_note = (
            f"其中 {money(pred_missing)} 行预测为空"
            f"（{pct(pred_missing / export_rows, 2)}），集中在深度模型的序列预热与缺失特征样本；"
            "评估按缺失剔除，不做插值或补零。深度模型产物未原生保存真实收益列，"
            "导出时按（周期、股票、日期）从同周期树模型回填，跨周期不借标签。"
        )
        export_note = (
            f"长表共 {money(export_rows)} 行"
            f"（每周期 × 每模型 × 每股票 × 每交易日），{pred_missing_note}"
        )
    signature = bundle.summary.get("panel", {}).get("signature", {})
    rows = [
        ["per_stock_coverage.csv", f"{money(len(per_stock))} 只股票 × 每模型有效天数 / 均值 / 最新预测"],
        ["per_stock_latest_5.csv", "T+5 每只股票 13 个模型的最新预测分数"],
        ["per_stock_latest_10.csv", "T+10 每只股票已就绪模型的最新预测分数"],
        [
            "all_predictions_2026.parquet",
            (
                f"长表：symbol, trade_date, horizon, model, pred, label_return"
                f"（{len(exported)} 个模型-周期组合，{money(export_rows)} 行）"
                if export_rows
                else "长表：symbol, trade_date, horizon, model, pred, label_return（如已生成）"
            ),
        ],
        ["quality_league_5/10.csv", "第 3 章质量榜原始数据"],
        ["portfolio_league_*_5/10.csv", "第 4 章各组合榜原始数据"],
        ["equity_*_*.csv", "逐日净值、基准、超额"],
        ["trades_*_*.csv", "逐笔成交（含费用、股数、卖出原因）"],
        ["attribution_*_*.csv", "按股票的盈亏归因"],
        ["summary.json", "全部指标、检验结果与清单的机器可读汇总"],
        ["inventory.csv", "26 个模型-周期组合的训练产物登记与就绪状态"],
    ]
    return f"""
<section class="section page-break">
  {section_head("08", "附录：逐股推理数据与复现", "把报告里的每个数字对应到可下载的文件")}

  <h3>8.1 逐股推理数据</h3>
  <p>本次评估的每模型预测面板为 {money(signature.get('rows', 0))} 行、{money(signature.get('symbols', 0))} 只股票、{signature.get('days', '—')} 个交易日。
  每只股票在每个模型下的有效预测天数、均值与最新分数汇总在 <span class="mono">per_stock_coverage.csv</span>；
  最新交易日的横截面在 <span class="mono">per_stock_latest_5.csv</span> 与 <span class="mono">per_stock_latest_10.csv</span>。
  逐日逐股的长表在 <span class="mono">all_predictions_2026.parquet</span>，可直接用 pandas / DuckDB 读取。
  {export_note}</p>
  {table(
      ["状态", "股票数"],
      [[str(key), money(value)] for key, value in status_counts.items()],
      aligns=["l", "r"],
  )}
  <p class="small">状态为 predicted 表示至少一个模型给出过有效预测；never_predicted 表示该股票在统一窗口内从未进入任何模型的预测面板。缺失预测不补零，评估时按缺失剔除。</p>

  <h3>8.2 产物清单</h3>
  {table(["文件", "内容"], rows, aligns=["l", "l"])}

  <h3>8.3 复现命令</h3>
  <pre># 1) 统一窗口评估（容器内；24 个已就绪组合）
docker exec -w /app quantmind python -m backend.scripts.backtest2026.multi_model \\
  --allow-missing --out /data/backtest_2026/multi_20261019_rerun

# 2) 逐股预测长表（容器内）
docker exec -w /app quantmind python -m backend.scripts.backtest2026.export_predictions \\
  --out /data/backtest_2026/multi_20261019_rerun

# 3) 生成 PDF（宿主机；需要 matplotlib / playwright）
python -m backend.scripts.backtest2026.multi_report \\
  --artifacts /media/zbox/data/quantmind/backtest_2026/multi_20261019_rerun \\
  --out /media/zbox/data/quantmind/backtest_2026/multi_20261019_rerun/report_2026_multi.pdf</pre>

  <h3>8.4 附表：同一模型在两个持有期的差值</h3>
  {table(
      ["模型", "T+10 IC − T+5 IC", "Top10 上涨比例差", "Top10 收益差", "滚动 Top5 收益差"],
      _paired_rows(bundle),
      aligns=["l", "r", "r", "r", "r"],
  )}
  <p class="small">差值 = T+10 − T+5。IC 差为小数，收益差与上涨比例差为百分点。正值表示该模型在更长持有期更好；逐行对照可以判断第 5.3 节的整体秩相关由哪些模型驱动。</p>

  <h3>8.5 术语表</h3>
  {table(
      ["术语", "含义"],
      [
          ["IC", "逐日截面秩相关：模型分数排序与真实收益排序的一致性，范围 -1 到 1。"],
          ["ICIR", "IC 均值 / IC 标准差；本报告不年化。"],
          ["方向胜率", "TopK 中 sign(预测) 与 sign(实际收益) 一致的比例；剔除收益恰为 0 的样本。"],
          ["上涨比例", "TopK 中实际收益为正的股票占比，不等同于方向胜率。"],
          ["十分位单调性", "第 1 至第 10 十分位平均收益与序号 1 至 10 的 Spearman 相关。"],
          ["Newey-West t", "允许重叠持有期自相关的均值显著性检验。"],
          ["FDR q", "Benjamini-Hochberg 校正后的 p 值；q < 0.05 表示在 5% 错误发现率下显著。"],
          ["DSR", "Deflated Sharpe Ratio：对试验次数与 Sharpe 方差校正后的 Sharpe 显著性。"],
          ["White RC", "White Reality Check：块自助下“最佳模型”相对随机基准的检验。"],
          ["PBO", "Probability of Backtest Overfitting：样本内最优在样本外落到中位数以下的概率。"],
          ["Kendall W", "Friedman 检验的效应量，0 到 1；越大表示模型排序越一致。"],
          ["单批轮动", "同一时点只持有一批股票，全部卖出后才开下一批。"],
          ["滚动 Top5", "每天新开 5 只，持有 H 天，形成 H 个重叠批次。"],
      ],
      aligns=["l", "l"],
  )}

  <div class="footer-note">
    <strong>免责声明：</strong>本报告仅供学习研究与技术演示，不构成任何投资建议。报告中的分析、评分与交易信号均由程序自动生成，可能存在错误或偏差。投资决策请咨询持有中国证监会颁发资质的专业机构。作者不对使用本工具产生的任何投资损失承担责任。股市有风险，投资需谨慎。
    <br><br>QuantMind Research · 报告编号 QM-R-2026-MULTI-1019 · 数据窗口 {esc(bundle.summary.get('manifest', {}).get('common_start', '—'))} 至 {esc(bundle.summary.get('manifest', {}).get('common_end', '—'))}
  </div>
</section>
"""
