/**
 * 周期收益展示层纯函数：月份回推、快捷区间、金额符号与权益曲线几何。
 */

import { describe, expect, it } from 'vitest';
import type { BlendPeriodDaily } from '../../../services/blendTypes';
import {
  buildEquityGeometry,
  formatPct,
  formatSignedAmount,
  formatWinRate,
  periodQuickQuery,
  periodRangeLabel,
  periodWarningLabel,
  shiftMonthsIso,
} from '../components/combo/periodReturnModel';

function daily(values: number[], start = '2026-09-01'): BlendPeriodDaily[] {
  return values.map((equity, index) => ({
    date: start.replace('01', String(index + 1).padStart(2, '0')),
    equity,
    cash: 0,
    positions: 0,
    entries: 0,
    exits: 0,
    return_pct: null,
  }));
}

describe('periodReturnModel', () => {
  it('shiftMonthsIso 处理月末与跨年', () => {
    expect(shiftMonthsIso('2026-10-19', -1)).toBe('2026-09-19');
    expect(shiftMonthsIso('2026-03-31', -1)).toBe('2026-02-28');
    expect(shiftMonthsIso('2026-01-15', -6)).toBe('2025-07-15');
    expect(shiftMonthsIso('2026-08-31', -6)).toBe('2026-02-28');
  });

  it('periodQuickQuery 用可用评分日降序取首尾', () => {
    const dates = ['2026-10-19', '2026-09-30', '2026-08-27'];
    expect(periodQuickQuery('1m', dates)).toEqual({ start: '2026-09-19', end: '2026-10-19' });
    expect(periodQuickQuery('6m', dates)).toEqual({ start: '2026-04-19', end: '2026-10-19' });
    expect(periodQuickQuery('all', dates)).toEqual({ start: '2026-08-27', end: '2026-10-19' });
    expect(periodQuickQuery('all', ['2026-10-19'])).toEqual({ end: '2026-10-19' });
    expect(periodQuickQuery('3m', [])).toEqual({});
  });

  it('金额带符号且缺失为 —，绝不写成 0', () => {
    expect(formatSignedAmount(123456.78)).toBe('+12.35 万');
    expect(formatSignedAmount(-64938.15)).toBe('-6.49 万');
    expect(formatSignedAmount(123400000)).toBe('+1.23 亿');
    expect(formatSignedAmount(0)).toBe('0');
    expect(formatSignedAmount(null)).toBe('—');
    expect(formatSignedAmount(Number.NaN)).toBe('—');
  });

  it('百分比与胜率缺失显示 —', () => {
    expect(formatPct(-6.0393)).toBe('-6.04%');
    expect(formatPct(null)).toBe('—');
    expect(formatWinRate(45.45)).toBe('45.5%');
    expect(formatWinRate(null)).toBe('—');
  });

  it('权益曲线几何把初始资金基线纳入纵向范围', () => {
    const geometry = buildEquityGeometry(daily([1000, 1200, 1100]), 1000, 640, 160, 4);
    expect(geometry).not.toBeNull();
    expect(geometry?.points).toHaveLength(3);
    expect(geometry?.minEquity).toBe(1000);
    expect(geometry?.maxEquity).toBe(1200);
    // 基线在区间底部（y 最大）
    expect(geometry?.baselineY).toBeGreaterThan(geometry?.points[1].y ?? 0);
    expect(geometry?.linePath.startsWith('M')).toBe(true);
    expect(geometry?.areaPath.endsWith('Z')).toBe(true);
  });

  it('单点与全等值不除零', () => {
    const single = buildEquityGeometry(daily([1000]), 1000, 640, 160);
    expect(single?.points).toHaveLength(1);
    expect(Number.isFinite(single?.baselineY)).toBe(true);
    const flat = buildEquityGeometry(daily([1000, 1000]), 1000, 640, 160);
    expect(flat?.maxEquity).toBeGreaterThan(flat?.minEquity ?? 0);
    expect(buildEquityGeometry([], 1000, 640, 160)).toBeNull();
  });

  it('告警码与区间文案：未登记码原样显示，夹取时同时给出请求与实际区间', () => {
    expect(periodWarningLabel('partial_data')).toContain('覆盖率');
    expect(periodWarningLabel('unknown_code')).toBe('unknown_code');
    expect(
      periodRangeLabel({
        requested: { start: '2026-04-22', end: '2026-10-19', capital: 1, positions: 1 },
        effective: {
          start: '2026-08-27',
          end: '2026-10-19',
          first_scoring_day: '2026-08-27',
          last_scoring_day: '2026-10-19',
        },
      }),
    ).toContain('按可用评分日');
    expect(
      periodRangeLabel({
        requested: { start: '2026-08-27', end: '2026-10-19', capital: 1, positions: 1 },
        effective: {
          start: '2026-08-27',
          end: '2026-10-19',
          first_scoring_day: '2026-08-27',
          last_scoring_day: '2026-10-19',
        },
      }),
    ).toBe('2026-08-27 ~ 2026-10-19');
  });
});
