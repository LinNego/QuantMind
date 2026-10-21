/**
 * 组合统计展示层公共文案：数据端口、可用区间、基准徽标、大盘与回撤窗口。
 * 核心契约：缺失一律 `—`，未登记告警码原样显示。
 */

import { describe, expect, it } from 'vitest';
import type { BlendBounds } from '../../../services/blendTypes';
import {
  benchmarkLabel,
  blendWarningLabel,
  boundsSpanLabel,
  drawdownWindowLabel,
  formatCount,
  formatDays,
  formatMultiple,
  formatRatio,
  marketChangeLabel,
  marketDayLabel,
  marketMa20Label,
  marketPositionLabel,
  marketToneLabel,
  memberReadinessLabel,
  sourceHint,
  sourceLabel,
} from '../components/combo/portfolioMetricsModel';

function bounds(over: Partial<BlendBounds> = {}): BlendBounds {
  return {
    source: 'pred',
    pred: {
      start: '2025-01-02',
      end: '2026-10-19',
      trading_days: 430,
      members_ready: 13,
      members_total: 13,
    },
    runs: { start: '2026-08-27', end: '2026-10-20', trading_days: 33 },
    effective: { start: '2025-01-02', end: '2026-10-19', trading_days: 430 },
    warnings: [],
    ...over,
  };
}

describe('portfolioMetricsModel', () => {
  it('数据端口标签与说明：pred 可回看测试集起点，runs 只覆盖推理台账', () => {
    expect(sourceLabel('pred')).toBe('pred 测试集');
    expect(sourceLabel('runs')).toBe('推理台账');
    expect(sourceLabel(null)).toBe('—');
    expect(sourceHint('pred')).toContain('pred.parquet');
    expect(sourceHint('runs')).toContain('engine_signal_scores');
    expect(sourceHint(undefined)).toBe('数据端口未知');
  });

  it('可用区间与成员就绪：runs 无成员就绪数时省略', () => {
    expect(boundsSpanLabel(bounds())).toBe('2025-01-02 ~ 2026-10-19 · 430 个交易日');
    expect(boundsSpanLabel(null)).toBeNull();
    expect(
      boundsSpanLabel(
        bounds({ effective: { start: null, end: null, trading_days: 0 } }),
      ),
    ).toBeNull();
    expect(memberReadinessLabel(bounds())).toBe('成员 13/13 有测试集');
    expect(
      memberReadinessLabel(bounds({ source: 'runs', pred: null })),
    ).toBeNull();
  });

  it('基准徽标区分可用与未取到；无名称时省略', () => {
    expect(
      benchmarkLabel({ symbol: 'SH000300', name: '沪深300', available: true, days: 430 }),
    ).toBe('沪深300 · 430 日');
    expect(
      benchmarkLabel({ symbol: null, name: '沪深300', available: false, days: 0 }),
    ).toBe('沪深300 · 未取到');
    expect(benchmarkLabel(null)).toBeNull();
    expect(benchmarkLabel({ symbol: null, name: null, available: false, days: 0 })).toBeNull();
  });

  it('数值格式缺失为 —；0 是合法值', () => {
    expect(formatRatio(1.234)).toBe('1.23');
    expect(formatRatio(null)).toBe('—');
    expect(formatCount(12.4)).toBe('12');
    expect(formatCount(undefined)).toBe('—');
    expect(formatMultiple(1.234)).toBe('1.23×');
    expect(formatMultiple(null)).toBe('—');
    expect(formatDays(19.6)).toBe('20 日');
    expect(formatDays(null)).toBe('—');
  });

  it('大盘文案：涨跌、均线位置与 hover 摘要', () => {
    expect(marketToneLabel('up')).toBe('上涨');
    expect(marketToneLabel('down')).toBe('下跌');
    expect(marketToneLabel('flat')).toBe('平盘');
    expect(marketToneLabel('unknown')).toBe('—');
    expect(marketPositionLabel('above_ma20')).toBe('站上 20 日线');
    expect(marketPositionLabel('below_ma20')).toBe('跌破 20 日线');
    expect(marketPositionLabel('unknown')).toBe('—');

    const day = {
      change_pct: 0.42,
      close: 3900.5,
      ma20_deviation_pct: 1.2,
      position: 'above_ma20' as const,
      tone: 'up' as const,
    };
    expect(marketChangeLabel(day)).toBe('+0.42%');
    expect(marketChangeLabel({ ...day, change_pct: null })).toBe('—');
    expect(marketMa20Label(day)).toBe('MA20 +1.20%');
    expect(marketMa20Label({ ...day, ma20_deviation_pct: null })).toBeNull();
    expect(marketDayLabel(day)).toBe('大盘 +0.42% · 收 3900.50 · MA20 +1.20% · 站上 20 日线');
    expect(marketDayLabel(null)).toBe('大盘 —');
  });

  it('回撤窗口：已修复给日期与天数，未修复明说', () => {
    expect(
      drawdownWindowLabel({
        drawdown_peak_date: '2026-09-01',
        drawdown_trough_date: '2026-10-17',
        drawdown_recovery_date: '2026-10-19',
        drawdown_recovery_days: 19,
      }),
    ).toBe('峰 2026-09-01 → 谷 2026-10-17 · 修复 2026-10-19（19 日）');
    expect(
      drawdownWindowLabel({
        drawdown_peak_date: '2026-09-01',
        drawdown_trough_date: '2026-10-17',
        drawdown_recovery_date: null,
      }),
    ).toBe('峰 2026-09-01 → 谷 2026-10-17 · 尚未修复');
    expect(drawdownWindowLabel({})).toBeNull();
  });

  it('告警码：已登记给中文，成员级码带 ID，未登记码原样显示', () => {
    expect(blendWarningLabel('benchmark_unavailable')).toContain('沪深300');
    expect(blendWarningLabel('member_pred_unavailable:m42')).toBe('组合成员无 pred 测试集：m42');
    expect(blendWarningLabel('something_new')).toBe('something_new');
  });
});
