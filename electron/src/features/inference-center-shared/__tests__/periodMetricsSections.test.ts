/**
 * 周期收益指标条（纯数据）：三段结构、已实现/浮动拆分、基准/超额缺失为 —。
 */

import { describe, expect, it } from 'vitest';
import type { BlendPeriodSummary } from '../../../services/blendTypes';
import { buildPeriodMetricSections } from '../components/combo/periodMetricsSections';

function summary(over: Partial<BlendPeriodSummary> = {}): BlendPeriodSummary {
  return {
    initial_capital: 10000000,
    final_equity: 11234000,
    total_return_pct: 12.34,
    profit_amount: 1234000,
    realized_pnl_amount: 900000,
    realized_return_pct: 9,
    unrealized_pnl_amount: 334000,
    unrealized_return_pct: 3.34,
    max_drawdown_pct: -3.21,
    drawdown_peak_date: '2026-09-01',
    drawdown_trough_date: '2026-09-10',
    drawdown_recovery_date: '2026-09-15',
    drawdown_recovery_days: 4,
    benchmark_return_pct: 6.1,
    benchmark_coverage_pct: 100,
    excess_return_pct: 6.24,
    annualized_return_pct: 32.5,
    volatility_pct: 18.2,
    sharpe_ratio: 1.7,
    sortino_ratio: 2.4,
    calmar_ratio: 10.1,
    tracking_error_pct: 5.5,
    information_ratio: 1.13,
    profit_factor: 1.8,
    payoff_ratio: 1.4,
    avg_win_pct: 6.2,
    avg_loss_pct: -4.4,
    gross_profit_amount: 2210000,
    gross_loss_amount: -976000,
    avg_holding_days: 10,
    turnover_x: 3.2,
    avg_exposure_pct: 84.5,
    final_cash_pct: 30.2,
    trading_days: 32,
    scoring_days: 32,
    entries: 160,
    exits: 110,
    closed_trades: 110,
    open_positions: 50,
    wins: 60,
    losses: 50,
    flats: 0,
    win_rate_pct: 54.55,
    avg_trade_return_pct: 0.88,
    avg_positions: 42.97,
    entry_convention: 'close',
    fee_convention: 'none',
    data_quality: 'complete',
    scoring_coverage_pct: 100,
    ...over,
  };
}

function find(sections: ReturnType<typeof buildPeriodMetricSections>, testId: string) {
  return sections
    .flatMap((section) => section.items)
    .find((item) => item.testId === testId);
}

describe('buildPeriodMetricSections', () => {
  it('三段结构：收益 / 风险 / 交易，总收益是 hero', () => {
    const sections = buildPeriodMetricSections(summary());
    expect(sections.map((section) => section.key)).toEqual(['returns', 'risk', 'trades']);
    expect(sections[0].items[0].emphasis).toBe('hero');
    expect(find(sections, 'combo-period-total-return')?.value).toBe('+12.34%');
    expect(find(sections, 'combo-period-profit')?.value).toBe('+123.40 万');
    expect(find(sections, 'combo-period-realized')?.value).toBe('+90.00 万 · 9.00%');
    expect(find(sections, 'combo-period-unrealized')?.value).toBe('+33.40 万 · 3.34%');
    expect(find(sections, 'combo-period-benchmark')?.value).toBe('+6.10%');
    expect(find(sections, 'combo-period-excess')?.value).toBe('+6.24%');
  });

  it('基准缺失时基准与超额为 —，提示说明覆盖率门槛', () => {
    const sections = buildPeriodMetricSections(
      summary({
        benchmark_return_pct: null,
        benchmark_coverage_pct: 40,
        excess_return_pct: null,
        information_ratio: null,
        tracking_error_pct: null,
      }),
    );
    const benchmark = find(sections, 'combo-period-benchmark');
    expect(benchmark?.value).toBe('—');
    expect(benchmark?.hint).toContain('40%');
    expect(find(sections, 'combo-period-excess')?.value).toBe('—');
    // 缺失走中性灰，不是红/绿（不暗示方向）
    expect(find(sections, 'combo-period-excess')?.tone).toBe('text-slate-400');
  });

  it('最大回撤 hover 给出峰谷与修复窗口', () => {
    const sections = buildPeriodMetricSections(summary());
    expect(find(sections, 'combo-period-drawdown')?.hint).toContain('峰 2026-09-01');
    expect(find(sections, 'combo-period-drawdown')?.hint).toContain('修复 2026-09-15');
  });

  it('合规：展示面不出现买卖字眼', () => {
    const text = JSON.stringify(buildPeriodMetricSections(summary()));
    expect(text).not.toMatch(/买入|卖出|加仓|减仓|清仓/);
  });
});
