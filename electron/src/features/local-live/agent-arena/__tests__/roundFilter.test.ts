import { describe, expect, test } from 'vitest';
import { matchesRoundFilter, type RoundFilter } from '../arena/utils/roundFilter';
import { isReviewLine } from '../arena/utils/logKind';

describe('isReviewLine', () => {
  test('recognises the legacy mode:review write path', () => {
    const line = { mode: 'review' };
    expect(isReviewLine(line)).toBe(true);
  });

  test('recognises the newer kind:review write path', () => {
    const line = { kind: 'review' };
    expect(isReviewLine(line)).toBe(true);
  });

  test('treats an analysis line with neither key as a trading round', () => {
    expect(isReviewLine({ kind: 'analysis' })).toBe(false);
    expect(isReviewLine({})).toBe(false);
    expect(isReviewLine(null)).toBe(false);
  });
});

describe('matchesRoundFilter', () => {
  const cases: { filter: RoundFilter; kind: 'analysis' | 'review'; fills: number; want: boolean }[] = [
    { filter: 'all', kind: 'analysis', fills: 2, want: true },
    { filter: 'all', kind: 'review', fills: 0, want: true },
    { filter: 'trade', kind: 'analysis', fills: 1, want: true },
    { filter: 'trade', kind: 'analysis', fills: 0, want: false },
    { filter: 'trade', kind: 'review', fills: 1, want: false },
    { filter: 'idle', kind: 'analysis', fills: 0, want: true },
    { filter: 'idle', kind: 'analysis', fills: 3, want: false },
    { filter: 'idle', kind: 'review', fills: 0, want: false },
    { filter: 'review', kind: 'review', fills: 0, want: true },
    { filter: 'review', kind: 'analysis', fills: 0, want: false },
  ];

  test.each(cases)('$filter × $kind × fills=$fills → $want', ({ filter, kind, fills, want }) => {
    expect(matchesRoundFilter(filter, kind, fills)).toBe(want);
  });

  test('the four filters partition every round exactly once', () => {
    const rounds = [
      { kind: 'analysis' as const, fills: 0 },
      { kind: 'analysis' as const, fills: 2 },
      { kind: 'review' as const, fills: 0 },
      { kind: 'review' as const, fills: 3 }, // 模拟盘按日误归属的极端情形
    ];
    const buckets = (['trade', 'idle', 'review'] as const).map((f) =>
      rounds.filter((r) => matchesRoundFilter(f, r.kind, r.fills)),
    );
    const total = buckets.reduce((n, b) => n + b.length, 0);
    expect(total).toBe(rounds.length);
    expect(rounds.every((r) => matchesRoundFilter('all', r.kind, r.fills))).toBe(true);
  });
});
