/**
 * 滚动建仓面板：色块只表达名单变化，不得把涨跌方向从颜色里带回来。
 */

import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import type { BlendRollingResponse } from '../../../services/blendTypes';
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

  it('滚动告警抬头展示', () => {
    render(
      <RollingBuildPanel {...baseProps} rolling={rolling({ warnings: ['部分成员当日无评分'] })} />,
    );
    expect(screen.getByTestId('combo-rolling-warnings')).toBeInTheDocument();
    expect(screen.getByText('部分成员当日无评分')).toBeInTheDocument();
  });
});
