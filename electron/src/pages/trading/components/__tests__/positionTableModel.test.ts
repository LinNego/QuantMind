/**
 * 持仓监控列表纯逻辑（排序/过滤/合计/货架）。
 *
 * 这些口径直接决定首屏看到的行序与 KPI 合计，必须与渲染层解耦后单独钉住：
 * 缺价行的盈亏是「不知道」，绝不参与加减（2026-09-22 事故）。
 */
import { describe, it, expect } from 'vitest';
import type { NormalizedHolding } from '../../utils/positionMetrics';
import {
  DEFAULT_SORT,
  buildInsightSummary,
  buildShelves,
  computePnlTotals,
  filterHoldings,
  nextSort,
  sortHoldings,
} from '../position-monitor/positionTableModel';

const holding = (over: Partial<NormalizedHolding>): NormalizedHolding => ({
  code: 'SH600000',
  name: '测试股',
  shares: 100,
  cost: 10,
  current: 11,
  profit: 100,
  profitPercent: 10,
  value: 1100,
  priceMissing: false,
  ...over,
});

describe('sortHoldings / nextSort', () => {
  const rows = [
    holding({ code: 'SH600001', value: 300, profit: -10, profitPercent: -2, shares: 30 }),
    holding({ code: 'SH600002', value: 100, profit: 50, profitPercent: 5, shares: 10 }),
    holding({ code: 'SH600003', value: 200, profit: 20, profitPercent: 1, shares: 20 }),
  ];

  it('默认按市值降序', () => {
    expect(sortHoldings(rows, DEFAULT_SORT).map((h) => h.code)).toEqual([
      'SH600001',
      'SH600003',
      'SH600002',
    ]);
  });

  it('升序/降序与同值代码兜底稳定', () => {
    expect(sortHoldings(rows, { key: 'value', dir: 'asc' }).map((h) => h.code)).toEqual([
      'SH600002',
      'SH600003',
      'SH600001',
    ]);
    const tied = [
      holding({ code: 'SH600009', value: 100 }),
      holding({ code: 'SH600008', value: 100 }),
    ];
    expect(sortHoldings(tied, { key: 'value', dir: 'desc' }).map((h) => h.code)).toEqual([
      'SH600008',
      'SH600009',
    ]);
  });

  it('表头点击：同列切换方向，换列默认降序', () => {
    expect(nextSort({ key: 'value', dir: 'desc' }, 'value')).toEqual({
      key: 'value',
      dir: 'asc',
    });
    expect(nextSort({ key: 'value', dir: 'asc' }, 'profit')).toEqual({
      key: 'profit',
      dir: 'desc',
    });
  });
});

describe('filterHoldings', () => {
  const rows = [
    holding({ code: 'SH600036', name: '招商银行' }),
    holding({ code: 'SZ000001', name: '平安银行' }),
  ];

  it('按名称/代码过滤且大小写不敏感', () => {
    expect(filterHoldings(rows, '招商').map((h) => h.code)).toEqual(['SH600036']);
    expect(filterHoldings(rows, 'sz000001').map((h) => h.code)).toEqual(['SZ000001']);
  });

  it('空查询原样返回', () => {
    expect(filterHoldings(rows, '   ')).toBe(rows);
  });
});

describe('computePnlTotals', () => {
  it('缺价行不计入合计与盈亏家数', () => {
    const totals = computePnlTotals([
      holding({ code: 'A', profit: 100 }),
      holding({ code: 'B', profit: -40 }),
      holding({ code: 'C', priceMissing: true, profit: 0, value: 0, current: 0 }),
    ]);
    expect(totals).toMatchObject({
      pricedCount: 2,
      missingCount: 1,
      totalPnl: 60,
      winCount: 1,
      loseCount: 1,
      pnlUnknown: false,
    });
  });

  it('全部缺价 → pnlUnknown（不报 0）', () => {
    const totals = computePnlTotals([
      holding({ priceMissing: true, profit: 0, value: 0 }),
    ]);
    expect(totals.pnlUnknown).toBe(true);
    expect(totals.missingCount).toBe(1);
  });
});

describe('buildShelves / buildInsightSummary', () => {
  it('第一行 2~5 块且累计占比 ≥55%，小块归并其余', () => {
    const rows = [
      holding({ code: 'A', value: 600 }),
      holding({ code: 'B', value: 300 }),
      holding({ code: 'C', value: 5 }),
      holding({ code: 'D', value: 4 }),
    ];
    const shelves = buildShelves(rows);
    expect(shelves.row1.map((h) => h.code)).toEqual(['A', 'B']);
    expect(shelves.row2).toEqual([]);
    expect(shelves.tail.map((h) => h.code)).toEqual(['C', 'D']);
    expect(shelves.tailValue).toBe(9);
    expect(shelves.total).toBe(909);
  });

  it('摘要给 Top1 占比与最大盈亏贡献', () => {
    const summary = buildInsightSummary([
      holding({ code: 'A', value: 800, profit: 5 }),
      holding({ code: 'B', value: 200, profit: -90 }),
    ]);
    expect(summary.topSharePct).toBeCloseTo(80, 5);
    expect(summary.topContributor?.code).toBe('B');
  });
});
