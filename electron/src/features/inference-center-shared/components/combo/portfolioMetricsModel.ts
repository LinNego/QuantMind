/**
 * 组合历史统计的展示层纯函数（周期收益 / 滚动建仓共用）。
 *
 * 口径约定（与后端 `portfolio_metrics.py` / `period_performance.py` /
 * `rolling_performance.py` 一致）：
 * - 缺失一律 `—`，**绝不写 0**（0 是合法的平盘/零暴露）；
 * - 基准覆盖率不足 95% 时后端已把基准/超额/信息比率置空，前端不再推算；
 * - 展示面只说「入选 / 退出 / 持有 / 换仓 / 批次」，不出现方向性交易字眼。
 */

import type {
  BlendBenchmark,
  BlendBounds,
  BlendDataSource,
  BlendMarketDay,
  BlendMarketPosition,
  BlendMarketTone,
} from '../../../../services/blendTypes';
import { RESEARCH_SCORE_MISSING } from '../../../shared/researchScore';

// ── 数据端口 ───────────────────────────────────────────────────────────

export function sourceLabel(source: BlendDataSource | null | undefined): string {
  if (source === 'pred') return 'pred 测试集';
  if (source === 'runs') return '推理台账';
  return RESEARCH_SCORE_MISSING;
}

export function sourceHint(source: BlendDataSource | null | undefined): string {
  if (source === 'pred') {
    return '成员模型的 pred.parquet 测试集分片（可回看至测试集起点）';
  }
  if (source === 'runs') {
    return 'engine_signal_scores 推理台账（只覆盖最近一次串行补推的日期）';
  }
  return '数据端口未知';
}

/** `2025-02-05 ~ 2026-10-19 · 414 个交易日`；无区间 → null */
export function boundsSpanLabel(bounds: BlendBounds | null | undefined): string | null {
  const span = bounds?.effective;
  if (!span?.start || !span.end) return null;
  const days = Number.isFinite(span.trading_days) ? `${span.trading_days} 个交易日` : null;
  return [ `${span.start} ~ ${span.end}`, days ].filter(Boolean).join(' · ');
}

/** `成员 13/13 有测试集`；pred 完全不可用 → null */
export function memberReadinessLabel(
  bounds: BlendBounds | null | undefined,
): string | null {
  const pred = bounds?.pred;
  if (!pred || !pred.members_total) return null;
  return `成员 ${pred.members_ready}/${pred.members_total} 有测试集`;
}

/** 基准徽标：`沪深300 · 32 日`；不可用 → `沪深300 · 未取到`（非 A 股 → null） */
export function benchmarkLabel(benchmark: BlendBenchmark | null | undefined): string | null {
  if (!benchmark?.name) return null;
  return benchmark.available
    ? `${benchmark.name} · ${benchmark.days} 日`
    : `${benchmark.name} · 未取到`;
}

// ── 数值格式 ───────────────────────────────────────────────────────────

/** 比率（夏普/IR/卡玛/盈亏比…）两位小数；缺失 → `—` */
export function formatRatio(value: number | null | undefined): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return RESEARCH_SCORE_MISSING;
  return value.toFixed(2);
}

/** 整数计数；缺失 → `—`（不把 undefined 当 0） */
export function formatCount(value: number | null | undefined): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return RESEARCH_SCORE_MISSING;
  return String(Math.round(value));
}

/** 倍数（如换手 1.23×）；缺失 → `—` */
export function formatMultiple(value: number | null | undefined): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return RESEARCH_SCORE_MISSING;
  return `${value.toFixed(2)}×`;
}

/** 天数（`19 日`）；缺失 → `—` */
export function formatDays(value: number | null | undefined): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return RESEARCH_SCORE_MISSING;
  return `${Math.round(value)} 日`;
}

// ── 大盘 ───────────────────────────────────────────────────────────────

export function marketToneLabel(tone: BlendMarketTone | string | null | undefined): string {
  switch (String(tone ?? '').toLowerCase()) {
    case 'up':
      return '上涨';
    case 'down':
      return '下跌';
    case 'flat':
      return '平盘';
    default:
      return RESEARCH_SCORE_MISSING;
  }
}

export function marketPositionLabel(
  position: BlendMarketPosition | string | null | undefined,
): string {
  switch (String(position ?? '').toLowerCase()) {
    case 'above_ma20':
      return '站上 20 日线';
    case 'below_ma20':
      return '跌破 20 日线';
    default:
      return RESEARCH_SCORE_MISSING;
  }
}

/** 大盘涨跌幅（%）；缺失 → `—` */
export function marketChangeLabel(market: BlendMarketDay | null | undefined): string {
  const value = market?.change_pct;
  if (typeof value !== 'number' || !Number.isFinite(value)) return RESEARCH_SCORE_MISSING;
  return `${value > 0 ? '+' : ''}${value.toFixed(2)}%`;
}

/** 大盘相对 20 日线偏离（`MA20 +1.20%`）；缺失 → null（由调用方决定是否省略） */
export function marketMa20Label(market: BlendMarketDay | null | undefined): string | null {
  const value = market?.ma20_deviation_pct;
  if (typeof value !== 'number' || !Number.isFinite(value)) return null;
  return `MA20 ${value > 0 ? '+' : ''}${value.toFixed(2)}%`;
}

/** 单元格 hover 文案：大盘涨跌 + 收盘 + 均线位置 */
export function marketDayLabel(market: BlendMarketDay | null | undefined): string {
  if (!market) return '大盘 —';
  const parts = [`大盘 ${marketChangeLabel(market)}`];
  if (typeof market.close === 'number' && Number.isFinite(market.close)) {
    parts.push(`收 ${market.close.toFixed(2)}`);
  }
  const ma20 = marketMa20Label(market);
  if (ma20) parts.push(ma20);
  parts.push(marketPositionLabel(market.position));
  return parts.join(' · ');
}

// ── 回撤 / 基准 / 告警 ─────────────────────────────────────────────────

/** 最大回撤窗口：`峰 → 谷 · 修复 …`；未修复时明说「尚未修复」 */
export function drawdownWindowLabel(input: {
  drawdown_peak_date?: string | null;
  drawdown_trough_date?: string | null;
  drawdown_recovery_date?: string | null;
  drawdown_recovery_days?: number | null;
}): string | null {
  const peak = input.drawdown_peak_date;
  const trough = input.drawdown_trough_date;
  if (!peak || !trough) return null;
  const parts = [`峰 ${peak} → 谷 ${trough}`];
  if (input.drawdown_recovery_date) {
    const days = input.drawdown_recovery_days;
    parts.push(
      `修复 ${input.drawdown_recovery_date}${typeof days === 'number' ? `（${days} 日）` : ''}`,
    );
  } else {
    parts.push('尚未修复');
  }
  return parts.join(' · ');
}

/** 机读告警码 → 展示文案（未登记码原样显示，便于排查） */
export const BLEND_WARNING_LABELS: Record<string, string> = {
  effective_start_clamped: '建仓日早于最早可用评分日，已从首个评分日开始',
  effective_end_clamped: '截止日没有评分，已夹到最后一个评分日',
  scoring_gap: '区间内部分交易日无评分，只按评分日建仓',
  partial_data: '评分覆盖率偏低（30%–80%），结果仅供参考',
  positions_cap: '持仓上限低于「每日新进 × 持有天数」，满仓后会暂停建仓',
  underfill: '持仓上限高于「每日新进 × 持有天数」，稳态下资金利用率偏低',
  missing_entry_price: '部分入选标的缺当日收盘价，已跳过建仓',
  deferred_exit: '部分标的到期日缺价，已顺延到下一个有效收盘价退出',
  stale_mark: '期末部分持仓缺最新收盘价，沿用上一有效价估值',
  benchmark_unavailable: '基准（沪深300）行情未取到，基准与超额一律显示 —',
  st_filter_static: 'ST/退市按当前证券主表截面剔除（静态口径，历史重算含前视偏差）',
  st_filter_unavailable: 'ST/退市数据源不可用：本轮未剔除，结果可能包含风险警示标的',
  tradability_unavailable: '成交额数据不可用：本轮未校验容量，按等权预算估算',
};

/** 成员级告警码前缀 → 文案模板 */
const MEMBER_WARNING_PREFIXES: Array<{ prefix: string; label: string }> = [
  { prefix: 'member_unavailable:', label: '组合成员不可用' },
  { prefix: 'member_pred_unavailable:', label: '组合成员无 pred 测试集' },
  { prefix: 'member_pred_empty:', label: '组合成员测试集为空' },
];

export function blendWarningLabel(code: string): string {
  const direct = BLEND_WARNING_LABELS[code];
  if (direct) return direct;
  const hit = MEMBER_WARNING_PREFIXES.find((item) => code.startsWith(item.prefix));
  if (hit) return `${hit.label}：${code.slice(hit.prefix.length)}`;
  return code;
}
