/**
 * 大盘分析指数叠加口径：窗口首日归一为 100，缺失日 null（connectNulls）。
 */
import { describe, it, expect } from 'vitest';
import { rebaseIndexPoints } from '../marketRegimeModel';

describe('rebaseIndexPoints', () => {
  it('首个有效日 = 100，后续按比例', () => {
    const out = rebaseIndexPoints(
      [
        { trade_date: '2026-09-01', close: 4000 },
        { trade_date: '2026-09-02', close: 4100 },
        { trade_date: '2026-09-03', close: 3800 },
      ],
      ['2026-09-01', '2026-09-02', '2026-09-03'],
    );
    expect(out).toEqual([100, 102.5, 95]);
  });

  it('日期缺失保持 null，不填充成 0', () => {
    const out = rebaseIndexPoints(
      [{ trade_date: '2026-09-02', close: 200 }],
      ['2026-09-01', '2026-09-02', '2026-09-03'],
    );
    expect(out).toEqual([null, 100, null]);
  });

  it('首个日期无数据时，以第一个有数据的日期为基准', () => {
    const out = rebaseIndexPoints(
      [
        { trade_date: '2026-09-02', close: 50 },
        { trade_date: '2026-09-03', close: 60 },
      ],
      ['2026-09-01', '2026-09-02', '2026-09-03'],
    );
    expect(out).toEqual([null, 100, 120]);
  });

  it('全无数据 → 全 null', () => {
    expect(rebaseIndexPoints([], ['2026-09-01', '2026-09-02'])).toEqual([null, null]);
  });

  it('非法收盘价（0/负数/NaN）视为缺失', () => {
    const out = rebaseIndexPoints(
      [
        { trade_date: '2026-09-01', close: 0 },
        { trade_date: '2026-09-02', close: 100 },
        { trade_date: '2026-09-03', close: Number.NaN },
      ],
      ['2026-09-01', '2026-09-02', '2026-09-03'],
    );
    expect(out).toEqual([null, 100, null]);
  });
});
