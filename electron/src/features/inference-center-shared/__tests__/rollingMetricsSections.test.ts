/**
 * 滚动建仓指标条（纯数据）：每日批次口径、已实现/浮动拆分、批次胜率只算已结算。
 */

import { describe, expect, it } from 'vitest';
import type { BlendRollingPerformance } from '../../../services/blendTypes';
import { buildRollingMetricSections } from '../components/combo/rollingMetricsSections';

function performance(over: Partial<BlendRollingPerformance> = {}): BlendRollingPerformance {
  return {
    initial_capital: 10000000,
    final_equity: 10450000,
    profit_amount: 450000,
    total_return_pct: 4.5,
    cycle_return_pct: 4.5,
    realized_pnl_amount: 320000,
    realized_return_pct: 3.2,
    unrealized_pnl_amount: 130000,
    unrealized_return_pct: 1.3,
    benchmark_return_pct: 1.2,
    benchmark_coverage_pct: 96,
    excess_return_pct: 3.3,
    annualized_return_pct: 12,
    volatility_pct: 8,
    sharpe_ratio: 1.5,
    sortino_ratio: 2,
    calmar_ratio: 1.1,
    max_drawdown_pct: -2.5,
    drawdown_peak_date: '2026-10-15',
    drawdown_trough_date: '2026-10-16',
    drawdown_recovery_date: '2026-10-19',
    drawdown_recovery_days: 2,
    tracking_error_pct: 3,
    information_ratio: 1.1,
    avg_daily_excess_pct: 0.2,
    trades: 2,
    wins: 1,
    losses: 1,
    flats: 0,
    win_rate_pct: 50,
    avg_return_pct: 0,
    avg_win_pct: 2,
    avg_loss_pct: -2,
    payoff_ratio: 1,
    profit_factor: 1,
    gross_profit_amount: 100000,
    gross_loss_amount: -100000,
    avg_holding_days: 1,
    turnover_x: 9,
    avg_exposure_pct: 100,
    final_cash_pct: 0,
    trading_days: 10,
    batches: 11,
    settled_batches: 10,
    open_batches: 1,
    priced_batches: 11,
    unpriced_batches: 0,
    partial_batches: 0,
    cycle_days: 10,
    cycle_priced_days: 10,
    cycle_win_days: 6,
    cycle_loss_days: 4,
    entry_convention: 'close',
    ...over,
  };
}

function find(sections: ReturnType<typeof buildRollingMetricSections>, testId: string) {
  return sections
    .flatMap((section) => section.items)
    .find((item) => item.testId === testId);
}

describe('buildRollingMetricSections', () => {
  it('三段结构：收益 / 风险 / 批次，窗口收益是 hero', () => {
    const sections = buildRollingMetricSections(performance());
    expect(sections.map((section) => section.key)).toEqual(['returns', 'risk', 'batches']);
    expect(sections[0].items[0].emphasis).toBe('hero');
    expect(find(sections, 'combo-rolling-total-return')?.value).toBe('+4.50%');
    expect(find(sections, 'combo-rolling-profit')?.value).toBe('+45.00 万');
    expect(find(sections, 'combo-rolling-realized')?.value).toBe('+32.00 万 · 3.20%');
    expect(find(sections, 'combo-rolling-unrealized')?.value).toBe('+13.00 万 · 1.30%');
    expect(find(sections, 'combo-rolling-benchmark')?.value).toBe('+1.20%');
    expect(find(sections, 'combo-rolling-excess')?.value).toBe('+3.30%');
  });

  it('批次计数区分结算与浮动，胜率提示只统计已结算批次', () => {
    const sections = buildRollingMetricSections(performance());
    expect(find(sections, 'combo-rolling-batches')?.value).toBe('11（结算 10 · 浮动 1）');
    const win = sections
      .flatMap((section) => section.items)
      .find((item) => item.key === 'win');
    expect(win?.value).toBe('50.0%');
    expect(win?.hint).toContain('仅已结算批次');
  });

  it('基准不可用：基准/超额/IR 全部 —，不写 0', () => {
    const sections = buildRollingMetricSections(
      performance({
        benchmark_return_pct: null,
        benchmark_coverage_pct: null,
        excess_return_pct: null,
        tracking_error_pct: null,
        information_ratio: null,
        avg_daily_excess_pct: null,
      }),
    );
    expect(find(sections, 'combo-rolling-benchmark')?.value).toBe('—');
    expect(find(sections, 'combo-rolling-benchmark')?.hint).toContain('不写 0');
    expect(find(sections, 'combo-rolling-excess')?.value).toBe('—');
    const ir = sections
      .flatMap((section) => section.items)
      .find((item) => item.key === 'ir');
    expect(ir?.value).toBe('—');
  });

  it('合规：展示面不出现买卖字眼', () => {
    const text = JSON.stringify(buildRollingMetricSections(performance()));
    expect(text).not.toMatch(/买入|卖出|加仓|减仓|清仓/);
  });
});
