/** 台账轮次筛选口径（四档互斥且不漏）。
 *
 *  全部轮次 = 有成交 + 无成交 + 盘后复盘。
 *  复盘轮（kind === 'review'）是只读沉淀、从不成交，属于独立档：
 *  - 不能落进「无成交」——否则四档语义搅混（2026-10-20 前即如此）；
 *  - 不能落进「有成交」——模拟盘按日归属成交时，复盘日会被按日成交误算。
 */

export type RoundFilter = 'all' | 'trade' | 'idle' | 'review';
export type RoundKind = 'review' | 'analysis';

export function matchesRoundFilter(
  filter: RoundFilter,
  kind: RoundKind,
  fillCount: number,
): boolean {
  if (filter === 'review') return kind === 'review';
  if (filter === 'trade') return kind !== 'review' && fillCount > 0;
  if (filter === 'idle') return kind !== 'review' && fillCount === 0;
  return true;
}
