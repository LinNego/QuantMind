/**
 * 滚动建仓面板：色块只表达名单变化，不得把涨跌方向从颜色里带回来。
 */

import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import type {
  BlendRollingPerformance,
  BlendRollingResponse,
} from '../../../services/blendTypes';
import { RollingBuildPanel } from '../components/combo/RollingBuildPanel';

function rolling(over: Partial<BlendRollingResponse> = {}): BlendRollingResponse {
  return {
    blend_id: 'blend-1',
    data_trade_date: '2026-10-20',
    target_date: '2026-10-21',
    window: 10,
    per_day: 5,
    days: ['2026-10-19', '2026-10-20'],
    columns: [
      {
        date: '2026-10-19',
        picks: [
          { symbol: 'SH600001', code: 'SH600001', name: '甲', score: 0.5, rank: 1, row_rank: 1, coverage: 13, pct: 0.95, position: 'BUY', held_days: 1, is_new: true },
        ],
        exits: [],
      },
      {
        date: '2026-10-20',
        picks: [
          { symbol: 'SH600001', code: 'SH600001', name: '甲', score: 0.5, rank: 1, row_rank: 1, coverage: 13, pct: 0.9, position: 'BUY', held_days: 2, is_new: false },
          { symbol: 'SH600002', code: 'SH600002', name: '乙', score: 0.4, rank: 2, row_rank: 2, coverage: 13, pct: 0.6, position: 'HOLD', held_days: 0, is_new: true },
        ],
        exits: ['SH600009'],
      },
    ],
    members: [
      {
        model_id: 'm1', display_name: '模型一', model_source: 'manual', model_type: 'lightgbm',
        horizon_days: 10, weight: 1, available: true,
      },
    ],
    summary: { unique_symbols: 2, new_today: 1, exited_today: 1 },
    warnings: [],
    ...over,
  };
}

const baseProps = { loading: false, error: null, onRetry: vi.fn() };

function performance(over: Partial<BlendRollingPerformance> = {}): BlendRollingPerformance {
  return {
    entry_convention: 'close',
    closed_trades: 2,
    priced_trades: 2,
    unpriced_trades: 0,
    wins: 1,
    losses: 1,
    flats: 0,
    win_rate_pct: 50,
    avg_return_pct: 0,
    median_return_pct: 0,
    best_symbol: 'SH600009',
    best_return_pct: 10,
    worst_symbol: 'SH600008',
    worst_return_pct: -10,
    open_positions: 2,
    open_priced: 2,
    open_avg_return_pct: 3.25,
    cycle_return_pct: 4.5,
    cycle_days: 1,
    cycle_priced_days: 1,
    cycle_win_days: 1,
    cycle_loss_days: 0,
    ...over,
  };
}

describe('RollingBuildPanel', () => {
  it('出错只显示错误与重试，不显示过期看板', () => {
    const onRetry = vi.fn();
    render(<RollingBuildPanel {...baseProps} rolling={rolling()} error="读取失败" onRetry={onRetry} />);
    expect(screen.getByTestId('combo-rolling-error')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /重\s*试/ }));
    expect(onRetry).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId('combo-rolling-board')).not.toBeInTheDocument();
  });

  it('没有窗口数据时给空态', () => {
    render(<RollingBuildPanel {...baseProps} rolling={null} />);
    expect(screen.getByText('该组合暂无滚动窗口数据')).toBeInTheDocument();
  });

  it('渲染 D-N / 今日 列、摘要与退出标注（只标一次）', () => {
    render(<RollingBuildPanel {...baseProps} rolling={rolling()} />);
    expect(screen.getByTestId('combo-rolling-board')).toBeInTheDocument();
    expect(screen.getByText('D-1')).toBeInTheDocument();
    expect(screen.getByText('今日')).toBeInTheDocument();
    expect(screen.getByText(/今日新进/)).toBeInTheDocument();
    expect(screen.getByText(/今日退出/)).toBeInTheDocument();
    expect(screen.getByText('退出：SH600009')).toBeInTheDocument();
    expect(screen.getByText(/持有天数 = 本窗口内连续在榜天数/)).toBeInTheDocument();
  });

  it('卡片显示行业与市值；缺失显示 — 而不是 0', () => {
    const data = rolling();
    data.columns[1].picks[0].industry = '银行';
    data.columns[1].picks[0].market_cap_yi = 2431.6;
    render(<RollingBuildPanel {...baseProps} rolling={data} />);
    const withProfile = screen.getByTestId('combo-rolling-pick-2026-10-20-SH600001');
    expect(withProfile.textContent).toContain('银行');
    expect(withProfile.textContent).toContain('2432 亿');
    const missing = screen.getByTestId('combo-rolling-pick-2026-10-20-SH600002');
    expect(missing.textContent).toContain('行业—');
    expect(missing.textContent).toContain('市值—');
  });

  it('新进与留存用不同色块，且都不含红绿', () => {
    render(<RollingBuildPanel {...baseProps} rolling={rolling()} />);
    const fresh = screen.getByTestId('combo-rolling-pick-2026-10-20-SH600002');
    const held = screen.getByTestId('combo-rolling-pick-2026-10-20-SH600001');
    expect(fresh.className).toContain('bg-blue-50');
    expect(held.className).toContain('bg-white');
    expect(`${fresh.className} ${held.className}`).not.toMatch(/red|green|rose|emerald/);
    expect(fresh.textContent).toContain('新进');
    expect(held.textContent).toContain('持有 2 日');
  });

  it('合规：只出现相对位置词，不出现买卖字眼', () => {
    const { container } = render(<RollingBuildPanel {...baseProps} rolling={rolling()} />);
    expect(container.textContent).toContain('靠前');
    expect(container.textContent).toContain('居中');
    expect(container.textContent).not.toMatch(/买入|卖出|BUY|SELL/);
  });

  it('退出盈亏红涨绿跌，并展示窗口整体涨跌统计', () => {
    const data = rolling();
    data.summary.performance = performance();
    data.columns[1].exit_rows = [
      {
        symbol: 'SH600009', name: '丙', entry_date: '2026-10-19', exit_date: '2026-10-20',
        entry_price: 10, exit_price: 11, return_pct: 10, held_days: 1,
        entry_from_window_start: true,
      },
      {
        symbol: 'SH600008', name: '丁', entry_date: '2026-10-19', exit_date: '2026-10-20',
        entry_price: 10, exit_price: 9, return_pct: -10, held_days: 1,
        entry_from_window_start: true,
      },
    ];
    render(<RollingBuildPanel {...baseProps} rolling={data} />);

    const win = screen.getByTestId('combo-rolling-exit-2026-10-20-SH600009');
    const loss = screen.getByTestId('combo-rolling-exit-2026-10-20-SH600008');
    expect(win.textContent).toContain('丙');
    expect(win.textContent).toContain('+10.00%');
    expect(win.innerHTML).toContain('text-red-600');
    expect(loss.textContent).toContain('-10.00%');
    expect(loss.innerHTML).toContain('text-green-600');

    const cycle = screen.getByTestId('combo-rolling-cycle-return');
    expect(cycle.textContent).toBe('+4.50%');
    expect(cycle.className).toContain('text-red-600');
    expect(screen.getByTestId('combo-rolling-performance').textContent).toContain('胜率');
    expect(screen.getByTestId('combo-rolling-performance').textContent).toContain('50.0%');
  });

  it('缺行情时收益显示 —，不写成 0', () => {
    const data = rolling();
    data.summary.performance = performance({
      priced_trades: 0,
      unpriced_trades: 2,
      win_rate_pct: null,
      avg_return_pct: null,
      cycle_return_pct: null,
      open_avg_return_pct: null,
    });
    data.columns[1].exit_rows = [
      {
        symbol: 'SH600009', name: '丙', entry_date: '2026-10-19', exit_date: '2026-10-20',
        entry_price: null, exit_price: null, return_pct: null, held_days: 1,
        entry_from_window_start: true,
      },
    ];
    render(<RollingBuildPanel {...baseProps} rolling={data} />);
    const row = screen.getByTestId('combo-rolling-exit-2026-10-20-SH600009');
    expect(row.textContent).toContain('—');
    expect(row.textContent).not.toContain('0.00%');
    expect(screen.getByTestId('combo-rolling-cycle-return').textContent).toBe('—');
  });

  it('横向溢出时渲染常驻滚动条（覆盖式原生滚动条不可见）', async () => {
    render(<RollingBuildPanel {...baseProps} rolling={rolling()} />);
    expect(screen.queryByTestId('combo-rolling-scrollbar')).not.toBeInTheDocument();

    const board = screen.getByTestId('combo-rolling-board');
    Object.defineProperty(board, 'clientWidth', { value: 500, configurable: true });
    Object.defineProperty(board, 'scrollWidth', { value: 900, configurable: true });
    fireEvent.scroll(board);

    const bar = await screen.findByTestId('combo-rolling-scrollbar');
    expect(bar).toHaveAttribute('role', 'scrollbar');
    expect(bar).toHaveAttribute('aria-orientation', 'horizontal');
    expect(bar).toHaveAttribute('aria-valuemin', '0');
    expect(bar).toHaveAttribute('aria-valuemax', '100');
    await waitFor(() =>
      expect(screen.getByTestId('combo-rolling-scroll-thumb')).toBeInTheDocument(),
    );
  });

  it('滚动告警抬头展示', () => {
    render(
      <RollingBuildPanel {...baseProps} rolling={rolling({ warnings: ['部分成员当日无评分'] })} />,
    );
    expect(screen.getByTestId('combo-rolling-warnings')).toBeInTheDocument();
    expect(screen.getByText('部分成员当日无评分')).toBeInTheDocument();
  });
});
