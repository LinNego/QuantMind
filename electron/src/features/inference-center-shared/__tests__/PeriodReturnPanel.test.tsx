/**
 * 周期收益面板：区间参数、月度拆解、缺失值 `—`、红涨绿跌与合规字眼。
 */

import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import type {
  BlendPeriodResponse,
  BlendRecord,
} from '../../../services/blendTypes';
import { PeriodReturnPanel } from '../components/combo/PeriodReturnPanel';

function blend(over: Partial<BlendRecord> = {}): BlendRecord {
  return {
    blend_id: 'blend-1',
    tenant_id: 'default',
    user_id: 'u1',
    name: '全13·T+10 等权',
    market: 'CN',
    purpose: 'rebalance',
    source: 'system',
    template_id: 'tpl-base',
    horizon_days: 10,
    weighting: 'equal',
    method: 'mean',
    trim_count: 2,
    min_coverage: 2,
    consensus_min_count: 0,
    entry_per_day: 5,
    rolling_window: 10,
    target_positions: 50,
    feature_signature: 'sig',
    data_source: 'quantdb_factors',
    status: 'active',
    created_at: null,
    updated_at: null,
    ...over,
  };
}

function period(over: Partial<BlendPeriodResponse> = {}): BlendPeriodResponse {
  const base: BlendPeriodResponse = {
    blend_id: 'blend-1',
    requested: { start: '2026-04-22', end: '2026-10-19', capital: 10000000, positions: 50 },
    effective: {
      start: '2026-08-27',
      end: '2026-10-19',
      first_scoring_day: '2026-08-27',
      last_scoring_day: '2026-10-19',
    },
    template: { entry_per_day: 5, hold_days: 10, target_positions: 50, method: 'mean' },
    summary: {
      initial_capital: 10000000,
      final_equity: 9935061.85,
      total_return_pct: -0.65,
      profit_amount: -64938.15,
      realized_pnl_amount: 12000,
      unrealized_pnl_amount: -76938.15,
      max_drawdown_pct: -6.04,
      trading_days: 32,
      scoring_days: 32,
      entries: 160,
      exits: 110,
      closed_trades: 110,
      open_positions: 50,
      wins: 50,
      losses: 60,
      flats: 0,
      win_rate_pct: 45.45,
      avg_trade_return_pct: 0.88,
      avg_positions: 42.97,
      entry_convention: 'close',
      fee_convention: 'none',
      data_quality: 'complete',
      scoring_coverage_pct: 100,
    },
    months: [
      {
        month: '2026-08',
        start_date: '2026-08-27',
        end_date: '2026-08-31',
        start_equity: 10000000,
        end_equity: 10031333.15,
        return_pct: 0.31,
        profit_amount: 31333.15,
        entries: 15,
        exits: 0,
        closed_trades: 0,
        month_end_positions: 15,
        win_rate_pct: null,
        partial: true,
      },
      {
        month: '2026-10',
        start_date: '2026-10-08',
        end_date: '2026-10-19',
        start_equity: 10374000,
        end_equity: 9935061.85,
        return_pct: -4.21,
        profit_amount: -438938.15,
        entries: 40,
        exits: 40,
        closed_trades: 40,
        month_end_positions: 50,
        win_rate_pct: 37.5,
        partial: true,
      },
    ],
    daily: [
      { date: '2026-08-27', equity: 10000000, cash: 9400000, positions: 5, entries: 5, exits: 0, return_pct: null },
      { date: '2026-09-30', equity: 10374000, cash: 4000000, positions: 50, entries: 5, exits: 5, return_pct: 1.1 },
      { date: '2026-10-19', equity: 9935061.85, cash: 3000000, positions: 50, entries: 5, exits: 5, return_pct: -0.4 },
    ],
    trades: [
      {
        symbol: 'SZ002790', code: 'SZ002790', name: '瑞尔特',
        entry_date: '2026-08-27', exit_date: '2026-09-10',
        entry_price: 5.99, exit_price: 8.46, shares: 33389.99, notional: 200000,
        return_pct: 41.24, pnl_amount: 82470.78, held_days: 10,
        exit_reason: '到期', deferred: false,
      },
      {
        symbol: 'SH600000', code: 'SH600000', name: '浦发银行',
        entry_date: '2026-09-01', exit_date: '2026-09-15',
        entry_price: 7.9, exit_price: 7.5, shares: 25316.45, notional: 200000,
        return_pct: -5.06, pnl_amount: -10126.58, held_days: 10,
        exit_reason: '到期', deferred: true,
      },
    ],
    open: [
      {
        symbol: 'SZ002864', code: 'SZ002864', name: '盘龙药业',
        entry_date: '2026-09-29', mark_date: '2026-10-19',
        entry_price: 35.2, mark_price: 34.04, shares: 5681.82, notional: 200000,
        return_pct: -3.3, pnl_amount: -6592.87, held_days: 9, stale: true,
      },
    ],
    warnings: ['effective_start_clamped', 'positions_cap'],
    members: [],
  };
  return { ...base, ...over };
}

const baseProps = {
  loading: false,
  error: null,
  dates: ['2026-10-19', '2026-08-27'],
  onRetry: vi.fn(),
};

describe('PeriodReturnPanel', () => {
  it('渲染总收益、月度拆解与口径脚注，缺失值显示 —', () => {
    const { container } = render(
      <PeriodReturnPanel
        {...baseProps}
        blend={blend()}
        period={period()}
        onCalculate={vi.fn()}
      />,
    );

    expect(screen.getByTestId('combo-period-total-return')).toHaveTextContent('-0.65%');
    expect(screen.getByTestId('combo-period-total-return').className).toContain('text-green-600');
    expect(screen.getByTestId('combo-period-profit')).toHaveTextContent('-6.49 万');
    expect(screen.getByTestId('combo-period-quality')).toHaveTextContent('数据完整');
    expect(screen.getByTestId('combo-period-chart')).toBeInTheDocument();
    expect(screen.getByText('月度拆解')).toBeInTheDocument();
    // 2026-08 无已了结交易 → 胜率必须是 —，不是 0
    expect(screen.getAllByText('—').length).toBeGreaterThan(0);
    expect(screen.getByTestId('combo-period-warnings')).toHaveTextContent(
      '建仓日早于最早可用评分日',
    );
    expect(screen.getByTestId('combo-period-warnings')).toHaveTextContent('持仓上限');
    expect(container.textContent).not.toMatch(/买入|卖出/);
  });

  it('点计算：按表单发送资金（万→元）与持仓上限', async () => {
    const onCalculate = vi.fn();
    render(
      <PeriodReturnPanel
        {...baseProps}
        blend={blend()}
        period={period()}
        onCalculate={onCalculate}
      />,
    );
    const [capitalInput] = screen.getAllByRole('spinbutton');
    fireEvent.change(capitalInput, { target: { value: '3000' } });
    fireEvent.click(screen.getByTestId('combo-period-calculate'));
    await waitFor(() => expect(onCalculate).toHaveBeenCalledTimes(1));
    expect(onCalculate.mock.calls[0][0]).toMatchObject({
      start: '2026-04-22',
      end: '2026-10-19',
      capital: 30000000,
      positions: 50,
    });
  });

  it('快捷区间「近 1 月」按可用评分日回推并带上当前资金/持仓', async () => {
    const onCalculate = vi.fn();
    render(
      <PeriodReturnPanel
        {...baseProps}
        blend={blend()}
        period={period()}
        onCalculate={onCalculate}
      />,
    );
    fireEvent.click(screen.getByTestId('combo-period-quick-1m'));
    await waitFor(() => expect(onCalculate).toHaveBeenCalledTimes(1));
    expect(onCalculate.mock.calls[0][0]).toMatchObject({
      start: '2026-09-19',
      end: '2026-10-19',
      capital: 10000000,
      positions: 50,
    });
  });

  it('展开明细：已了结与在场表格都渲染，顺延/沿用标记可见', async () => {
    render(
      <PeriodReturnPanel
        {...baseProps}
        blend={blend()}
        period={period()}
        onCalculate={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByText('已了结明细（2 笔）'));
    expect(await screen.findByText('瑞尔特')).toBeInTheDocument();
    expect(screen.getByText('顺延')).toBeInTheDocument();

    fireEvent.click(screen.getByText('期末在场（1 只）'));
    expect(await screen.findByText('盘龙药业')).toBeInTheDocument();
    expect(screen.getByText('沿用')).toBeInTheDocument();
  });

  it('错误态：展示原因并可重试', () => {
    const onRetry = vi.fn();
    render(
      <PeriodReturnPanel
        {...baseProps}
        error="评分日不足"
        blend={blend()}
        period={null}
        onCalculate={vi.fn()}
        onRetry={onRetry}
      />,
    );
    expect(screen.getByTestId('combo-period-error')).toHaveTextContent('评分日不足');
    fireEvent.click(screen.getByRole('button', { name: /重\s*试/ }));
    expect(onRetry).toHaveBeenCalled();
  });

  it('无结果时给出空态提示，不伪造 0 收益', () => {
    render(
      <PeriodReturnPanel
        {...baseProps}
        blend={blend()}
        period={null}
        onCalculate={vi.fn()}
      />,
    );
    expect(screen.getByText(/按模板滚动评估综合选股收益/)).toBeInTheDocument();
    expect(screen.queryByTestId('combo-period-total-return')).toBeNull();
  });
});
