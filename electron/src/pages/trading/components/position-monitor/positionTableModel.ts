/**
 * 持仓监控列表纯逻辑（排序 / 过滤 / 合计 / 货架），与渲染解耦以便单测。
 *
 * 口径纪律：
 * - 缺价行（`priceMissing`）的盈亏是「不知道」，绝不参与合计（2026-09-22 事故：
 *   十行缺价被算成「市值 0 − 成本」，浮动盈亏显示 −84,778.90）；
 * - 排序稳定：同值以代码升序兜底，虚拟滚动下顺序不抖动。
 */
import type { NormalizedHolding } from '../../utils/positionMetrics';

export type SortKey = 'value' | 'profit' | 'pct' | 'shares';
export type SortDir = 'asc' | 'desc';

export interface SortState {
  key: SortKey;
  dir: SortDir;
}

export const DEFAULT_SORT: SortState = { key: 'value', dir: 'desc' };

const ACCESSOR: Record<SortKey, (h: NormalizedHolding) => number> = {
  value: (h) => h.value || 0,
  profit: (h) => h.profit || 0,
  pct: (h) => h.profitPercent || 0,
  shares: (h) => h.shares || 0,
};

export function sortHoldings(
  rows: NormalizedHolding[],
  sort: SortState,
): NormalizedHolding[] {
  const get = ACCESSOR[sort.key];
  const sign = sort.dir === 'asc' ? 1 : -1;
  return [...rows].sort((a, b) => {
    const diff = get(a) - get(b);
    if (diff !== 0) return diff * sign;
    return a.code.localeCompare(b.code);
  });
}

/** 表头点击：同列切换升降序；换列默认降序（市值/盈亏这类「大的先看」） */
export function nextSort(current: SortState, key: SortKey): SortState {
  if (current.key === key) {
    return { key, dir: current.dir === 'desc' ? 'asc' : 'desc' };
  }
  return { key, dir: 'desc' };
}

export function filterHoldings(
  rows: NormalizedHolding[],
  query: string,
): NormalizedHolding[] {
  const kw = String(query || '').trim().toLowerCase();
  if (!kw) return rows;
  return rows.filter(
    (h) =>
      (h.name || '').toLowerCase().includes(kw) ||
      h.code.toLowerCase().includes(kw),
  );
}

export interface PnlTotals {
  pricedCount: number;
  missingCount: number;
  totalPnl: number;
  winCount: number;
  loseCount: number;
  /** 一行都算不出来时宁可不报数：0 会被读成「不赚不亏」 */
  pnlUnknown: boolean;
}

export function computePnlTotals(rows: NormalizedHolding[]): PnlTotals {
  let pricedCount = 0;
  let totalPnl = 0;
  let winCount = 0;
  let loseCount = 0;
  for (const h of rows) {
    if (h.priceMissing) continue;
    pricedCount += 1;
    totalPnl += h.profit || 0;
    if (h.profit > 0) winCount += 1;
    else if (h.profit < 0) loseCount += 1;
  }
  return {
    pricedCount,
    missingCount: rows.length - pricedCount,
    totalPnl,
    winCount,
    loseCount,
    pnlUnknown: rows.length > 0 && pricedCount === 0,
  };
}

export interface Shelves {
  row1: NormalizedHolding[];
  row2: NormalizedHolding[];
  tail: NormalizedHolding[];
  tailValue: number;
  total: number;
}

/** 市值分布货架：第一行大块（累计 ≥55% 或 2~5 块），第二行 ≥1.2%，其余归并 */
export function buildShelves(rows: NormalizedHolding[]): Shelves {
  const items = [...rows].sort((a, b) => b.value - a.value);
  const total = items.reduce((sum, h) => sum + Math.max(h.value, 0), 0) || 1;
  const row1: NormalizedHolding[] = [];
  let cum = 0;
  for (const h of items) {
    if (row1.length >= 5 || (row1.length >= 2 && cum / total >= 0.55)) break;
    row1.push(h);
    cum += Math.max(h.value, 0);
  }
  const rest = items.slice(row1.length);
  const row2 = rest.filter((h) => Math.max(h.value, 0) / total >= 0.012);
  const tail = rest.slice(row2.length);
  const tailValue = tail.reduce((sum, h) => sum + Math.max(h.value, 0), 0);
  return { row1, row2, tail, tailValue, total };
}

/** 顶部摘要（折叠态一条 40px 摘要条用）：Top1 占比 + 最大盈亏贡献 */
export function buildInsightSummary(rows: NormalizedHolding[]): {
  topSharePct: number | null;
  topContributor: NormalizedHolding | null;
} {
  const shelves = buildShelves(rows);
  const first = shelves.row1[0];
  const topSharePct = first ? (Math.max(first.value, 0) / shelves.total) * 100 : null;
  const topContributor = [...rows]
    .filter((h) => !h.priceMissing && h.profit !== 0)
    .sort((a, b) => Math.abs(b.profit) - Math.abs(a.profit))[0] ?? null;
  return { topSharePct, topContributor };
}
