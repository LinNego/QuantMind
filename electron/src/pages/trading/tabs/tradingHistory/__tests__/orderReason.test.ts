/**
 * 交易历史「原因」列：剥机器前缀，保留人话拒因。
 *
 * 2026-10-22 实证：20 张卖单全被 `l3.stale_quote` 拒，历史记录只显示「已拒绝」，
 * 用户看不到为什么；后端 `remarks` 里其实有完整拒因。
 */
import { describe, it, expect } from 'vitest';
import { displayReason, displayReasonSummary } from '../orderReason';

describe('displayReason', () => {
  it('剥掉 mark_rejected 的 Execution rejected 前缀', () => {
    expect(
      displayReason(
        'Execution rejected: 风控拒单[l3.stale_quote]（派发复检）: 行情陈旧',
      ),
    ).toBe('风控拒单[l3.stale_quote]（派发复检）: 行情陈旧');
  });

  it('无前缀原样返回并 trim', () => {
    expect(displayReason('  [CANCELLED: 用户撤单]  ')).toBe('[CANCELLED: 用户撤单]');
  });

  it('空值/非字符串 → 空串（不渲染原因行）', () => {
    expect(displayReason(null)).toBe('');
    expect(displayReason(undefined)).toBe('');
    expect(displayReason(123)).toBe('123');
  });
});

describe('displayReasonSummary（状态列短原因）', () => {
  it('取拒因冒号后的尾巴', () => {
    expect(
      displayReasonSummary(
        'Execution rejected: 风控拒单[l3.stale_quote]（派发复检）: 行情陈旧',
      ),
    ).toBe('行情陈旧');
  });

  it('无冒号的拒因保留并截断', () => {
    expect(
      displayReasonSummary('Execution rejected: Insufficient holdings for sell order'),
    ).toBe('Insufficient holdings fo…');
  });

  it('空值 → 空串', () => {
    expect(displayReasonSummary(null)).toBe('');
  });
});
