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
import {
  BLEND_WARNING_LABELS,
  blendWarningLabel,
} from './portfolioMetricsModel';

/** 机读告警码 → 展示文案（后端新增码未登记时原样显示，便于排查） */
export const PERIOD_WARNING_LABELS: Record<string, string> = BLEND_WARNING_LABELS;

export function periodWarningLabel(code: string): string {
  return blendWarningLabel(code);
}

export type PeriodQuickRange = '1m' | '3m' | '6m' | '1y' | 'all';

export const PERIOD_QUICK_RANGES: Array<{ key: PeriodQuickRange; label: string }> = [
  { key: '1m', label: '近 1 月' },
  { key: '3m', label: '近 3 月' },
  { key: '6m', label: '近 6 月' },
  { key: '1y', label: '近 1 年' },
  { key: 'all', label: '全部' },
];

/** 快捷区间锚点：优先用后端 `/bounds` 的实际可用区间（pred 可回看到测试集起点） */
export interface PeriodRangeSpan {
  start?: string | null;
  end?: string | null;
}

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
 * 快捷区间 → 查询参数。
 *
 * `dates` 是后端 `/dates` 的可用评分日（**降序**，只覆盖推理台账）；
 * `span` 是 `/bounds` 的实际可用区间（pred 可回看到 2025），优先作为锚点。
 * 无可用日期时返回空参数（后端按最新可用日与默认窗口计算并给出业务码）。
 */
export function periodQuickQuery(
  kind: PeriodQuickRange,
  dates: readonly string[],
  span?: PeriodRangeSpan | null,
): BlendPeriodQuery {
  const end = span?.end || dates[0];
  if (!end) return {};
  if (kind === 'all') {
    const start = span?.start || dates[dates.length - 1];
    return start && start !== end ? { start, end } : { end };
  }
  const months = kind === '1m' ? 1 : kind === '3m' ? 3 : kind === '6m' ? 6 : 12;
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

/** 权益曲线的最小结构（组合周期收益与单模型历史统计共用） */
export interface EquitySeriesPoint {
  date: string;
  equity: number;
}

/** 组合 + 基准双线几何（共享纵轴，基准按同一初始资金归一） */
export interface ComparisonGeometry {
  equity: EquityGeometry;
  benchmarkPoints: EquityPoint[];
  benchmarkPath: string;
}

/**
 * 权益曲线几何（纯函数，供 SVG 直接渲染）。
 *
 * 纵向范围把**初始资金基线**纳入，避免全程盈利时基线跑到画布外；
 * 单点/全等值时画水平中线，不做除零。
 */
export function buildEquityGeometry(
  daily: readonly EquitySeriesPoint[],
  initialCapital: number,
  width: number,
  height: number,
  padding = 4,
): EquityGeometry | null {
  return buildComparisonGeometry(daily, [], initialCapital, width, height, padding)?.equity ?? null;
}

/**
 * 组合权益 + 基准指数**共享纵轴**的双线几何。
 *
 * 为什么共享纵轴：两条线分开归一化会让「跑赢/跑输」在图上不可见——
 * 基准按初始资金归一后与组合同轴，交点才是真实的超额拐点。
 * `benchmarkDaily` 为空时退化为单线（`benchmarkPath` 为空串）。
 */
export function buildComparisonGeometry(
  equityDaily: readonly EquitySeriesPoint[],
  benchmarkDaily: readonly EquitySeriesPoint[],
  initialCapital: number,
  width: number,
  height: number,
  padding = 4,
): ComparisonGeometry | null {
  if (equityDaily.length === 0 || width <= 0 || height <= 0) return null;
  const equityValues = equityDaily
    .map((row) => row.equity)
    .filter((value) => Number.isFinite(value));
  if (equityValues.length === 0) return null;
  const benchmarkValues = benchmarkDaily
    .map((row) => row.equity)
    .filter((value) => Number.isFinite(value));
  const baseline = Number.isFinite(initialCapital) ? initialCapital : equityValues[0];
  let minEquity = Math.min(...equityValues, ...benchmarkValues, baseline);
  let maxEquity = Math.max(...equityValues, ...benchmarkValues, baseline);
  if (maxEquity - minEquity < 1e-9) {
    minEquity -= 1;
    maxEquity += 1;
  }
  const innerWidth = Math.max(1, width - padding * 2);
  const innerHeight = Math.max(1, height - padding * 2);
  const yOf = (value: number) =>
    padding + (1 - (value - minEquity) / (maxEquity - minEquity)) * innerHeight;
  const toPoints = (series: readonly EquitySeriesPoint[]): EquityPoint[] =>
    series.map((row, index) => ({
      x: round2(
        series.length === 1
          ? padding + innerWidth / 2
          : padding + (index / (series.length - 1)) * innerWidth,
      ),
      y: round2(yOf(row.equity)),
      equity: row.equity,
      date: row.date,
    }));
  const points = toPoints(equityDaily);
  const linePath = toPath(points);
  const first = points[0];
  const last = points[points.length - 1];
  const areaPath = `${linePath} L${last.x},${round2(height)} L${first.x},${round2(height)} Z`;
  const benchmarkPoints = benchmarkValues.length ? toPoints(benchmarkDaily) : [];
  return {
    equity: {
      points,
      linePath,
      areaPath,
      baselineY: round2(yOf(baseline)),
      minEquity,
      maxEquity,
    },
    benchmarkPoints,
    benchmarkPath: benchmarkPoints.length ? toPath(benchmarkPoints) : '',
  };
}

function toPath(points: readonly EquityPoint[]): string {
  return points
    .map((point, index) => `${index === 0 ? 'M' : 'L'}${point.x},${point.y}`)
    .join(' ');
}

function round2(value: number): number {
  return Math.round(value * 100) / 100;
}

/**
 * 由逐日基准收益复利出「基准按初始资金归一」的权益序列。
 *
 * 没有一天有基准收益 → 返回空数组（不画一条平线冒充基准）；
 * 首日缺收益（无前收）时停在初始资金，从第二日起复利。
 */
export function buildBenchmarkEquitySeries(
  daily: readonly Pick<BlendPeriodDaily, 'date' | 'benchmark_return_pct'>[],
  initialCapital: number,
): EquitySeriesPoint[] {
  if (!Number.isFinite(initialCapital) || initialCapital <= 0) return [];
  let value = initialCapital;
  let started = false;
  const out: EquitySeriesPoint[] = [];
  for (const row of daily) {
    const ret = row.benchmark_return_pct;
    if (typeof ret === 'number' && Number.isFinite(ret)) {
      value *= 1 + ret / 100;
      started = true;
    }
    out.push({ date: row.date, equity: value });
  }
  return started ? out : [];
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
