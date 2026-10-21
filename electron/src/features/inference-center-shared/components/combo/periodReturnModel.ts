/**
 * 周期收益（固定持有期滚动桶）展示层纯函数。
 *
 * 口径（与后端 `period_performance.py` 一致）：
 * - 建仓/退出都按**收盘价**，不计手续费、印花税与滑点；
 * - 缺失值一律 `—`，绝不显示成 0（0 是合法平盘）；
 * - 展示面只说「建仓/退出/入选」，不出现方向性交易字眼。
 */

import type {
  BlendPeriodDaily,
  BlendPeriodQuery,
  BlendPeriodResponse,
} from '../../../../services/blendTypes';
import { RESEARCH_SCORE_MISSING } from '../../../shared/researchScore';

/** 机读告警码 → 展示文案（后端新增码未登记时原样显示，便于排查） */
export const PERIOD_WARNING_LABELS: Record<string, string> = {
  effective_start_clamped: '建仓日早于最早可用评分日，已从首个评分日开始',
  effective_end_clamped: '截止日没有评分，已夹到最后一个评分日',
  scoring_gap: '区间内部分交易日无评分，只按评分日建仓',
  partial_data: '评分覆盖率偏低（30%–80%），结果仅供参考',
  positions_cap: '持仓上限低于「每日新进 × 持有天数」，满仓后会暂停建仓',
  underfill: '持仓上限高于「每日新进 × 持有天数」，稳态下资金利用率偏低',
  missing_entry_price: '部分入选标的缺当日收盘价，已跳过建仓',
  deferred_exit: '部分标的到期日缺价，已顺延到下一个有效收盘价退出',
  stale_mark: '期末部分持仓缺最新收盘价，沿用上一有效价估值',
};

export function periodWarningLabel(code: string): string {
  return PERIOD_WARNING_LABELS[code] ?? code;
}

export type PeriodQuickRange = '1m' | '3m' | '6m' | 'all';

export const PERIOD_QUICK_RANGES: Array<{ key: PeriodQuickRange; label: string }> = [
  { key: '1m', label: '近 1 月' },
  { key: '3m', label: '近 3 月' },
  { key: '6m', label: '近 6 月' },
  { key: 'all', label: '全部' },
];

export function shiftMonthsIso(iso: string, months: number): string {
  const [year, month, day] = iso.split('-').map((part) => Number(part));
  const base = new Date(Date.UTC(year, month - 1 + months, 1));
  const lastDay = new Date(
    Date.UTC(base.getUTCFullYear(), base.getUTCMonth() + 1, 0),
  ).getUTCDate();
  base.setUTCDate(Math.min(day, lastDay));
  return base.toISOString().slice(0, 10);
}

/**
 * 快捷区间 → 查询参数。`dates` 是后端 `/dates` 的可用评分日（**降序**）。
 * 无可用日期时返回空参数（后端会按最新可用日与默认窗口计算并给出空态/业务码）。
 */
export function periodQuickQuery(
  kind: PeriodQuickRange,
  dates: readonly string[],
): BlendPeriodQuery {
  if (dates.length === 0) return {};
  const end = dates[0];
  if (kind === 'all') {
    const start = dates[dates.length - 1];
    return start && start !== end ? { start, end } : { end };
  }
  const months = kind === '1m' ? 1 : kind === '3m' ? 3 : 6;
  return { start: shiftMonthsIso(end, -months), end };
}

/** 金额（元）带符号，万元/亿元缩写；缺失 → `—` */
export function formatSignedAmount(value: number | null | undefined): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    return RESEARCH_SCORE_MISSING;
  }
  const sign = value > 0 ? '+' : '';
  const abs = Math.abs(value);
  if (abs >= 1e8) return `${sign}${(value / 1e8).toFixed(2)} 亿`;
  if (abs >= 1e4) return `${sign}${(value / 1e4).toFixed(2)} 万`;
  return `${sign}${value.toFixed(0)}`;
}

/** 百分比（0–100）两位小数；缺失 → `—` */
export function formatPct(value: number | null | undefined): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    return RESEARCH_SCORE_MISSING;
  }
  return `${value.toFixed(2)}%`;
}

/** 胜率一位小数；无已了结交易 → `—` */
export function formatWinRate(value: number | null | undefined): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    return RESEARCH_SCORE_MISSING;
  }
  return `${value.toFixed(1)}%`;
}

export interface EquityPoint {
  x: number;
  y: number;
  equity: number;
  date: string;
}

export interface EquityGeometry {
  points: EquityPoint[];
  linePath: string;
  areaPath: string;
  baselineY: number;
  minEquity: number;
  maxEquity: number;
}

/**
 * 权益曲线几何（纯函数，供 SVG 直接渲染）。
 *
 * 纵向范围把**初始资金基线**纳入，避免全程盈利时基线跑到画布外；
 * 单点/全等值时画水平中线，不做除零。
 */
export function buildEquityGeometry(
  daily: readonly BlendPeriodDaily[],
  initialCapital: number,
  width: number,
  height: number,
  padding = 4,
): EquityGeometry | null {
  if (daily.length === 0 || width <= 0 || height <= 0) return null;
  const values = daily.map((row) => row.equity);
  const candidates = values.filter((value) => Number.isFinite(value));
  if (candidates.length === 0) return null;
  const baseline = Number.isFinite(initialCapital) ? initialCapital : candidates[0];
  let minEquity = Math.min(...candidates, baseline);
  let maxEquity = Math.max(...candidates, baseline);
  if (maxEquity - minEquity < 1e-9) {
    minEquity -= 1;
    maxEquity += 1;
  }
  const innerWidth = Math.max(1, width - padding * 2);
  const innerHeight = Math.max(1, height - padding * 2);
  const yOf = (value: number) =>
    padding + (1 - (value - minEquity) / (maxEquity - minEquity)) * innerHeight;
  const xOf = (index: number) =>
    daily.length === 1
      ? padding + innerWidth / 2
      : padding + (index / (daily.length - 1)) * innerWidth;
  const points = daily.map((row, index) => ({
    x: round2(xOf(index)),
    y: round2(yOf(row.equity)),
    equity: row.equity,
    date: row.date,
  }));
  const linePath = points
    .map((point, index) => `${index === 0 ? 'M' : 'L'}${point.x},${point.y}`)
    .join(' ');
  const first = points[0];
  const last = points[points.length - 1];
  const areaPath = `${linePath} L${last.x},${round2(height)} L${first.x},${round2(height)} Z`;
  return {
    points,
    linePath,
    areaPath,
    baselineY: round2(yOf(baseline)),
    minEquity,
    maxEquity,
  };
}

function round2(value: number): number {
  return Math.round(value * 100) / 100;
}

/** 「建仓日 / 截止日 / 资金 / 持仓上限」摘要行（实际生效区间） */
export function periodRangeLabel(
  response: Pick<BlendPeriodResponse, 'requested' | 'effective'>,
): string {
  const requested = `${response.requested.start} ~ ${response.requested.end}`;
  if (
    response.effective.start === response.requested.start &&
    response.effective.end === response.requested.end
  ) {
    return requested;
  }
  return `${requested}（按可用评分日：${response.effective.start} ~ ${response.effective.end}）`;
}

/** 「已了结 n 笔 · 在场 m 只」 */
export function periodTradeCountLabel(
  summary: Pick<BlendPeriodResponse['summary'], 'closed_trades' | 'open_positions'>,
): string {
  return `已了结 ${summary.closed_trades} 笔 · 在场 ${summary.open_positions} 只`;
}
