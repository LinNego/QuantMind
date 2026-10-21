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
    expect(find(sections, 'combo-rolling-batch-counts')?.value).toBe('11（结算 10 · 浮动 1）');
    const win = sections
      .flatMap((section) => section.items)
      .find((item) => item.key === 'win');
    expect(win?.value).toBe('50.0%');
    expect(win?.hint).toContain('仅已结算批次');
  });

  it('批次明细：新后端给「无建仓」批次数，旧后端不硬造', () => {
    const withSkips = buildRollingMetricSections(
      performance({ no_position_batches: 3, partial_batches: 1, unpriced_batches: 2 }),
    );
    const hint = String(find(withSkips, 'combo-rolling-batch-counts')?.hint);
    expect(hint).toContain('已计价 11');
    expect(hint).toContain('缺价 2');
    expect(hint).toContain('部分缺价 1');
    expect(hint).toContain('无建仓 3 批');
    const legacyHint = String(
      find(buildRollingMetricSections(performance()), 'combo-rolling-batch-counts')?.hint,
    );
    expect(legacyHint).not.toContain('无建仓');
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

  it('旧后端缺少批次计数字段：显示 —，绝不渲染 undefined', () => {
    const legacy = {
      ...performance(),
      batches: undefined,
      settled_batches: undefined,
      open_batches: undefined,
      priced_batches: undefined,
      unpriced_batches: undefined,
      partial_batches: undefined,
      wins: undefined,
      losses: undefined,
      flats: undefined,
    } as unknown as BlendRollingPerformance;
    const sections = buildRollingMetricSections(legacy);
    const counts = find(sections, 'combo-rolling-batch-counts');
    expect(counts?.value).toBe('—（结算 — · 浮动 —）');
    expect(String(counts?.hint)).not.toMatch(/undefined/);
    const win = sections
      .flatMap((section) => section.items)
      .find((item) => item.key === 'win');
    expect(String(win?.hint)).not.toMatch(/undefined/);
  });

  it('执行约束段：建仓覆盖 / 容量 / ST / 跳过明细', () => {
    const sections = buildRollingMetricSections(
      performance({
        evaluated_picks: 50,
        bought_positions: 40,
        capacity_applied: true,
        st_filter_applied: true,
        skipped_st: 2,
        skipped_low_adv: 5,
        skipped_no_amount: 2,
        skipped_no_price: 1,
        min_adv_yi: 1,
        adv_participation: 0.01,
        adv_lookback_days: 20,
        min_adv_days: 15,
      }),
    );
    expect(sections.map((section) => section.key)).toEqual([
      'returns',
      'risk',
      'batches',
      'tradability',
    ]);
    expect(find(sections, 'combo-rolling-coverage')?.value).toBe('40 / 50（80%）');
    expect(find(sections, 'combo-rolling-capacity')?.value).toBe('1 亿 · 参与率 1.0%');
    expect(String(find(sections, 'combo-rolling-capacity')?.hint)).toContain('ADV20');
    expect(find(sections, 'combo-rolling-st-filter')?.value).toBe('已启用');
    expect(find(sections, 'combo-rolling-skips')?.value).toBe('10 只');
    expect(find(sections, 'combo-rolling-skips')?.hint).toBe(
      'ST/退市 2 · 成交额不足 5 · 缺成交额 2 · 缺入选价 1',
    );
  });

  it('旧后端没有可交易性字段：整段省略，不渲染一串 —', () => {
    const sections = buildRollingMetricSections(performance());
    expect(sections.map((section) => section.key)).toEqual(['returns', 'risk', 'batches']);
  });

  it('成交额/ST 数据源不可用：明确写「未启用」，提示说明原因', () => {
    const sections = buildRollingMetricSections(
      performance({ capacity_applied: false, st_filter_applied: false }),
    );
    expect(find(sections, 'combo-rolling-capacity')?.value).toBe('未启用');
    expect(String(find(sections, 'combo-rolling-capacity')?.hint)).toContain('未校验容量');
    expect(find(sections, 'combo-rolling-st-filter')?.value).toBe('未启用');
    expect(JSON.stringify(sections)).not.toMatch(/undefined/);
  });

  it('非 A 股市场：写「不适用」，不误报为数据源故障', () => {
    const sections = buildRollingMetricSections(
      performance({ capacity_applied: false, st_filter_applied: false }),
      { market: 'US', exclude_st: true, st_applied: false, capacity_applied: false },
    );
    expect(find(sections, 'combo-rolling-capacity')?.value).toBe('不适用');
    expect(String(find(sections, 'combo-rolling-capacity')?.hint)).toContain('该市场');
    expect(find(sections, 'combo-rolling-st-filter')?.value).toBe('不适用');
    expect(String(find(sections, 'combo-rolling-st-filter')?.hint)).toContain('该市场');
  });

  it('显式关闭 ST 过滤：写「已关闭」，不误报为数据源故障', () => {
    const sections = buildRollingMetricSections(
      performance({ capacity_applied: true, st_filter_applied: false }),
      { market: 'CN', exclude_st: false, st_applied: false, capacity_applied: true },
    );
    expect(find(sections, 'combo-rolling-st-filter')?.value).toBe('已关闭');
    expect(String(find(sections, 'combo-rolling-st-filter')?.hint)).toContain('exclude_st=false');
  });

  it('评估数为 0：覆盖显示 — 而不是 0%', () => {
    const sections = buildRollingMetricSections(
      performance({ evaluated_picks: 0, bought_positions: 0 }),
    );
    expect(find(sections, 'combo-rolling-coverage')?.value).toBe('—');
  });

  it('合规：展示面不出现买卖字眼', () => {
    const text = JSON.stringify(
      buildRollingMetricSections(
        performance({
          evaluated_picks: 50,
          bought_positions: 40,
          capacity_applied: true,
          st_filter_applied: true,
          skipped_st: 2,
          skipped_low_adv: 5,
          skipped_no_amount: 2,
          skipped_no_price: 1,
        }),
      ),
    );
    expect(text).not.toMatch(/买入|卖出|加仓|减仓|清仓/);
  });
});
