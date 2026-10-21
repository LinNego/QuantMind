/**
 * 截面选股 · 单模型 TopK 历史统计展示层纯函数。
 *
 * 口径（与后端 `selection_stats.py` 一致）：
 * - 信号日 T → T 之后第 `execution_lag_days` 个交易日**收盘入选** →
 *   入选后第 H 个交易日**收盘退出**；期与期首尾相接；
 * - 每期全部权益等权投入，期收益 = 有效标的等权平均，权益逐期复利；
 * - 不计手续费、印花税与滑点，也不建模停牌/涨跌停等不可交易状态；
 * - 缺失一律 `—`，绝不显示成 0。

 * 展示面合规：只说「入选 / 退出 / 换股 / 持有」，不出现方向性交易字眼。
 */

import { shiftMonthsIso } from '../combo/periodReturnModel';
import type {
  SelectionStatsCohort,
  SelectionStatsEffective,
  SelectionStatsQuery,
  SelectionStatsResponse,
} from '../../../../services/selectionStatsTypes';

/** 机读告警码 → 展示文案（未登记的新码原样显示，便于排查） */
export const SELECTION_WARNING_LABELS: Record<string, string> = {
  effective_start_clamped: '起始日早于测试集首日，已从首个测试信号日开始',
  effective_end_clamped: '截止日超出测试集末日，已夹到最后一个测试信号日',
  short_window: '可用信号日不足 20 天，统计仅供粗看',
  underfill_signals: '部分信号日可选标的少于设定数量，按实际数量等权',
  missing_entry_price: '部分入选标的缺入选日收盘价，该期已剔除该标的',
  missing_exit_price: '部分标的窗口内没有有效收盘价，该期已剔除该标的',
  stale_exit: '部分标的到期日缺价，已用窗口内最后有效收盘价估值',
  partial_cohorts: '部分期有效标的占比不足 80%，期收益代表性偏低',
  skipped_cohorts: '部分期全部标的缺价，已跳过且权益不变',
  open_cohort: '区间末尾有未到期的一期，按最后有效收盘价估值',
  benchmark_unavailable: '存在基准缺价的期，基准与超额按已接上的期计算',
};

export function selectionWarningLabel(code: string): string {
  return SELECTION_WARNING_LABELS[code] ?? code;
}

/** 换股节奏：模型周期（T+H）或每日滚动（T+1 每天都换） */
export type SelectionHoldMode = 'period' | 'daily';

export const SELECTION_HOLD_MODES: Array<{ value: SelectionHoldMode; label: string }> = [
  { value: 'period', label: '按模型周期换股' },
  { value: 'daily', label: '每日滚动入选' },
];

export function selectionHoldModeOf(
  response: Pick<SelectionStatsResponse, 'params'>,
): SelectionHoldMode {
  return response.params.cohort_mode === 'daily' ? 'daily' : 'period';
}

export type SelectionQuickRange = '1y' | '6m' | '3m' | 'all';

export const SELECTION_QUICK_RANGES: Array<{ key: SelectionQuickRange; label: string }> = [
  { key: '1y', label: '近 1 年' },
  { key: '6m', label: '近 6 月' },
  { key: '3m', label: '近 3 月' },
  { key: 'all', label: '全部' },
];

/** 快捷区间 → 查询参数；`bounds` 是测试集全区间（首次自动统计的生效区间） */
export function selectionQuickQuery(
  kind: SelectionQuickRange,
  bounds: Pick<SelectionStatsEffective, 'start' | 'end'> | null,
): SelectionStatsQuery {
  if (!bounds) return {};
  if (kind === 'all') return { start: bounds.start, end: bounds.end };
  const months = kind === '1y' ? 12 : kind === '6m' ? 6 : 3;
  return { start: shiftMonthsIso(bounds.end, -months), end: bounds.end };
}

/** 统计区间说明：请求值被夹取时补出生效区间 */
export function selectionRangeLabel(
  response: Pick<SelectionStatsResponse, 'requested' | 'effective'>,
): string {
  const { requested, effective } = response;
  const reqStart = requested.start ?? effective.start;
  const reqEnd = requested.end ?? effective.end;
  if (reqStart === effective.start && reqEnd === effective.end) {
    return `${effective.start} ~ ${effective.end}`;
  }
  return `${reqStart} ~ ${reqEnd}（按可用测试信号日：${effective.start} ~ ${effective.end}）`;
}

/** 口径摘要：「T+1 收盘入选 · 持有 10 日 · ...」 */
export function selectionConventionLabel(
  response: Pick<SelectionStatsResponse, 'params' | 'requested'>,
): string {
  const { params, requested } = response;
  return [
    `T+${params.execution_lag_days} 收盘入选`,
    `持有 ${requested.hold_days} 个交易日`,
    '等权满仓',
    '前复权收盘价',
    '不计费用与滑点',
  ].join(' · ');
}

/** 单期备注：未到期 / 价格缺失 / 估值顺延 */
export function cohortNote(row: SelectionStatsCohort): string {
  const parts: string[] = [];
  if (row.exit_kind === 'open') parts.push('未到期');
  if (row.skipped) parts.push('无有效价格');
  if (row.stale_exit > 0) parts.push(`${row.stale_exit} 只按窗口内最后价估值`);
  if (row.missing_entry > 0) parts.push(`${row.missing_entry} 只缺入选价`);
  if (row.missing_exit > 0) parts.push(`${row.missing_exit} 只缺退出价`);
  return parts.length > 0 ? parts.join(' · ') : '—';
}

/** 入选覆盖率（%）的档位色：≥95 绿、≥90 黄、其余红 */
export function coverageToneClass(value: number | null | undefined): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return 'text-slate-400';
  if (value >= 95) return 'text-slate-700';
  if (value >= 90) return 'text-amber-600';
  return 'text-rose-600';
}
