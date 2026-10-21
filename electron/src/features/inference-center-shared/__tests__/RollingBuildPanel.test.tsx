/**
 * 滚动建仓面板：色块只表达名单变化，不得把涨跌方向从颜色里带回来。
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
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
    capital: 10000000,
    source: 'pred',
    bounds: {
      source: 'pred',
      pred: {
        start: '2025-01-02', end: '2026-10-19', trading_days: 430,
        members_ready: 1, members_total: 1,
      },
      runs: { start: '2026-08-27', end: '2026-10-20', trading_days: 33 },
      effective: { start: '2025-01-02', end: '2026-10-19', trading_days: 430 },
      warnings: [],
    },
    benchmark: { symbol: 'SH000300', name: '沪深300', available: true, days: 430 },
    batches: [
      {
        entry_date: '2026-10-19', exit_date: '2026-10-20', positions: 1, priced: 1,
        return_pct: 2, pnl_amount: 200000, equity: 10200000,
        notional_amount: 10000000, settled: true, holding_days: 1,
      },
      {
        entry_date: '2026-10-20', exit_date: '2026-10-20', positions: 2, priced: 2,
        return_pct: 1, pnl_amount: 250000, equity: 10450000,
        notional_amount: 10200000, settled: false, holding_days: 1,
      },
    ],
    daily: [
      {
        date: '2026-10-19', exit_date: '2026-10-20', return_pct: 2, equity: 10200000,
        market_value: 10200000, cash: 0, exposure_pct: 100,
        benchmark_return_pct: 0.5, excess_return_pct: 1.5, settled: true,
      },
      {
        date: '2026-10-20', exit_date: '2026-10-20', return_pct: 1, equity: 10450000,
        market_value: 10450000, cash: 0, exposure_pct: 100,
        benchmark_return_pct: 0.2, excess_return_pct: 0.8, settled: false,
      },
    ],
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
        market: {
          change_pct: 0.42,
          close: 3900.5,
          ma20_deviation_pct: 1.2,
          position: 'above_ma20',
          tone: 'up',
        },
        picks: [
          {
            symbol: 'SH600001', code: 'SH600001', name: '甲', score: 0.5, rank: 1, row_rank: 1,
            coverage: 13, pct: 0.9, position: 'BUY', held_days: 2, is_new: false,
            change_pct: 2.1, weight_pct: 50, day_pnl_amount: 12345,
            stats: {
              appearances: 2, streak_days: 2, first_seen: '2026-10-19',
              since_first_pct: 3.2, next_day_avg_pct: 0.8, next_day_win_rate_pct: 100,
              next_day_samples: 1, next_day_std_pct: null, latest_close: 12.3,
              change_pct: 2.1,
            },
          },
          {
            symbol: 'SH600002', code: 'SH600002', name: '乙', score: 0.4, rank: 2, row_rank: 2,
            coverage: 13, pct: 0.6, position: 'HOLD', held_days: 0, is_new: true,
            change_pct: -1.2, weight_pct: null, day_pnl_amount: null,
            stats: {
              appearances: 1, streak_days: 1, first_seen: '2026-10-20',
              since_first_pct: null, next_day_avg_pct: null, next_day_win_rate_pct: null,
              next_day_samples: 0, next_day_std_pct: null, latest_close: 8.4,
              change_pct: -1.2,
            },
          },
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

const baseProps = {
  loading: false,
  error: null,
  onRetry: vi.fn(),
  capital: 10000000,
  onCapitalCommit: vi.fn(),
};

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
    benchmark_coverage_pct: 100,
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
    gross_loss_amount: 100000,
    avg_holding_days: 1,
    turnover_x: 9,
    avg_exposure_pct: 100,
    final_cash_pct: 0,
    trading_days: 10,
    batches: 2,
    settled_batches: 1,
    open_batches: 1,
    priced_batches: 2,
    unpriced_batches: 0,
    partial_batches: 0,
    cycle_days: 1,
    cycle_priced_days: 1,
    cycle_win_days: 1,
    cycle_loss_days: 0,
    entry_convention: 'close',
    ...over,
  };
}

describe('RollingBuildPanel', () => {
  // 折叠状态写 localStorage：每个用例从「展开」开始，避免互相污染
  beforeEach(() => {
    window.localStorage.removeItem('qm:combo:rolling:metrics-collapsed');
  });

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

    const total = screen.getByTestId('combo-rolling-total-return');
    expect(total.textContent).toBe('+4.50%');
    expect(total.className).toContain('text-red-600');
    const strip = screen.getByTestId('combo-rolling-performance');
    expect(strip.textContent).toContain('胜率');
    expect(strip.textContent).toContain('50.0%');
    // 已实现 / 浮动必须分开显示（机构级口径）
    expect(screen.getByTestId('combo-rolling-realized').textContent).toContain('+32.00 万');
    expect(screen.getByTestId('combo-rolling-unrealized').textContent).toContain('+13.00 万');
    expect(screen.getByTestId('combo-rolling-benchmark').textContent).toBe('+1.20%');
    expect(screen.getByTestId('combo-rolling-excess').textContent).toBe('+3.30%');
  });

  it('缺行情时收益显示 —，不写成 0', () => {
    const data = rolling();
    data.summary.performance = performance({
      total_return_pct: null,
      cycle_return_pct: null,
      win_rate_pct: null,
      avg_return_pct: null,
      unrealized_pnl_amount: 0,
      unrealized_return_pct: null,
      benchmark_return_pct: null,
      excess_return_pct: null,
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
    expect(screen.getByTestId('combo-rolling-total-return').textContent).toBe('—');
    expect(screen.getByTestId('combo-rolling-benchmark').textContent).toBe('—');
    expect(screen.getByTestId('combo-rolling-excess').textContent).toBe('—');
  });

  it('每列头部展示当日大盘涨跌幅与 20 日线位置（红涨绿跌）', () => {
    render(<RollingBuildPanel {...baseProps} rolling={rolling()} />);
    const market = screen.getByTestId('combo-rolling-market-2026-10-20');
    expect(market.textContent).toContain('+0.42%');
    expect(market.textContent).toContain('站上 20 日线');
    expect(market.innerHTML).toContain('text-red-600');
    // 无大盘数据的列：— 而不是 0
    expect(screen.getByTestId('combo-rolling-market-2026-10-19').textContent).toContain('—');
  });

  it('格子展示当日涨跌幅、当日盈亏与在榜统计；新进不计当日盈亏', () => {
    render(<RollingBuildPanel {...baseProps} rolling={rolling()} />);
    const change = screen.getByTestId('combo-rolling-pick-change-2026-10-20-SH600001');
    expect(change.textContent).toContain('+2.10%');
    expect(change.className).toContain('text-red-600');
    expect(
      screen.getByTestId('combo-rolling-pick-pnl-2026-10-20-SH600001').textContent,
    ).toBe('+1.23 万');
    const stats = screen.getByTestId('combo-rolling-pick-stats-2026-10-20-SH600001');
    expect(stats.textContent).toContain('在榜 2');
    expect(stats.textContent).toContain('+3.20%');
    expect(stats.textContent).toContain('+0.80%');

    // 新进（当日入选）没有当日盈亏；涨跌幅照常显示且绿跌
    expect(screen.queryByTestId('combo-rolling-pick-pnl-2026-10-20-SH600002')).toBeNull();
    expect(
      screen.getByTestId('combo-rolling-pick-change-2026-10-20-SH600002').className,
    ).toContain('text-green-600');
  });

  it('数据端口徽标展示 pred 测试集、可用区间与基准', () => {
    render(<RollingBuildPanel {...baseProps} rolling={rolling()} />);
    const badge = screen.getByTestId('combo-rolling-source');
    expect(badge.textContent).toContain('pred 测试集');
    expect(badge.textContent).toContain('2025-01-02 ~ 2026-10-19');
    expect(badge.textContent).toContain('成员 1/1 有测试集');
    expect(badge.textContent).toContain('沪深300 · 430 日');
    expect(badge.textContent).toContain('本金 1000 万');
  });

  it('批次本金失焦才提交，不逐字符重算', () => {
    const onCapitalCommit = vi.fn();
    render(
      <RollingBuildPanel
        {...baseProps}
        onCapitalCommit={onCapitalCommit}
        rolling={rolling()}
      />,
    );
    const input = screen.getByTestId('combo-rolling-capital') as HTMLInputElement;
    fireEvent.change(input, { target: { value: '2000' } });
    expect(onCapitalCommit).not.toHaveBeenCalled();
    fireEvent.blur(input);
    expect(onCapitalCommit).toHaveBeenCalledWith(20000000);
  });

  it('展开每日批次与逐日净值明细', async () => {
    render(<RollingBuildPanel {...baseProps} rolling={rolling()} />);
    expect(screen.getByTestId('combo-rolling-details')).toBeInTheDocument();
    fireEvent.click(screen.getByText(/每日批次（2 批/));
    const batches = await screen.findByTestId('combo-rolling-batches');
    expect(batches.textContent).toContain('已结算');
    expect(batches.textContent).toContain('浮动中');
    expect(batches.textContent).toContain('+20.00 万');
    fireEvent.click(screen.getByText(/逐日净值（2 日）/));
    const daily = await screen.findByTestId('combo-rolling-daily');
    // 权益 1020 万、基准 +0.50%、超额 +1.50%
    expect(daily.textContent).toContain('1020.0 万');
    expect(daily.textContent).toContain('+0.50%');
    expect(daily.textContent).toContain('+1.50%');
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

  it('顶部统计可折叠：折叠后只留一行摘要，状态写入 localStorage', () => {
    const data = rolling();
    data.summary.performance = performance({
      evaluated_picks: 20,
      bought_positions: 18,
      final_cash_pct: 3.2,
    });
    render(<RollingBuildPanel {...baseProps} rolling={data} />);

    const toggle = screen.getByTestId('combo-rolling-performance-toggle');
    expect(toggle.textContent).toBe('收起统计');
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByTestId('combo-rolling-total-return')).toBeInTheDocument();

    fireEvent.click(toggle);
    expect(toggle.textContent).toBe('展开统计');
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByTestId('combo-rolling-total-return')).not.toBeInTheDocument();
    const header = screen.getByTestId('combo-rolling-performance-header');
    expect(header.textContent).toContain('窗口收益 +4.50%');
    expect(header.textContent).toContain('收益金额 +45.00 万');
    expect(header.textContent).toContain('建仓 18/20');
    expect(header.textContent).toContain('期末现金 3.20%');
    expect(header.textContent).not.toMatch(/undefined/);
    expect(window.localStorage.getItem('qm:combo:rolling:metrics-collapsed')).toBe('1');

    fireEvent.click(screen.getByTestId('combo-rolling-performance-toggle'));
    expect(screen.getByTestId('combo-rolling-total-return')).toBeInTheDocument();
    expect(window.localStorage.getItem('qm:combo:rolling:metrics-collapsed')).toBeNull();
  });

  it('折叠状态从 localStorage 恢复', () => {
    window.localStorage.setItem('qm:combo:rolling:metrics-collapsed', '1');
    const data = rolling();
    data.summary.performance = performance();
    render(<RollingBuildPanel {...baseProps} rolling={data} />);
    expect(screen.queryByTestId('combo-rolling-total-return')).not.toBeInTheDocument();
    expect(screen.getByTestId('combo-rolling-performance-toggle').textContent).toBe('展开统计');
    // 摘要里的未知字段按 — 处理，不渲染 undefined
    expect(screen.getByTestId('combo-rolling-performance-header').textContent).not.toMatch(
      /undefined/,
    );
  });

  it('格子展示建仓状态：建仓 x% / 次日建仓 / 未建仓原因，且不用红绿', () => {
    const data = rolling();
    data.columns[0].picks[0] = {
      ...data.columns[0].picks[0],
      buy_status: 'skip_st',
      adv_yi: 2.5,
    };
    data.columns[1].picks[0] = {
      ...data.columns[1].picks[0],
      buy_status: 'bought',
      entry_weight_pct: 2.5,
      adv_yi: 3.2,
    };
    data.columns[1].picks[1] = {
      ...data.columns[1].picks[1],
      buy_status: 'skip_low_adv',
      adv_yi: 0.2,
    };
    data.columns[1].picks.push({
      symbol: 'SH600003', code: 'SH600003', name: '戊', score: 0.3, rank: 3, row_rank: 3,
      coverage: 13, pct: 0.4, position: 'HOLD', held_days: 0, is_new: true,
      change_pct: 0, buy_status: 'next_day',
    });
    const { container } = render(<RollingBuildPanel {...baseProps} rolling={data} />);

    expect(
      screen.getByTestId('combo-rolling-pick-buy-2026-10-19-SH600001').textContent,
    ).toBe('未建仓 · ST/退市');
    const bought = screen.getByTestId('combo-rolling-pick-buy-2026-10-20-SH600001');
    expect(bought.textContent).toBe('建仓 2.5%');
    expect(bought.innerHTML).toContain('text-indigo-700');
    expect(
      screen.getByTestId('combo-rolling-pick-buy-2026-10-20-SH600002').textContent,
    ).toBe('未建仓 · 成交额不足');
    const nextDay = screen.getByTestId('combo-rolling-pick-buy-2026-10-20-SH600003');
    expect(nextDay.textContent).toBe('次日建仓');
    expect(nextDay.innerHTML).toContain('text-sky-700');

    const toneText = [
      screen.getByTestId('combo-rolling-pick-buy-2026-10-19-SH600001').innerHTML,
      bought.innerHTML,
      nextDay.innerHTML,
    ].join(' ');
    expect(toneText).not.toMatch(/red|green|rose|emerald/);
    expect(container.textContent).not.toMatch(/买入|卖出|BUY|SELL|undefined/);
  });

  it('旧后端没有 buy_status：不渲染建仓标签，保持原样', () => {
    const { container } = render(<RollingBuildPanel {...baseProps} rolling={rolling()} />);
    expect(
      screen.queryByTestId('combo-rolling-pick-buy-2026-10-20-SH600001'),
    ).not.toBeInTheDocument();
    expect(container.textContent).not.toMatch(/undefined/);
  });

  it('批次明细展示建仓只数；旧后端缺字段显示 —', async () => {
    render(<RollingBuildPanel {...baseProps} rolling={rolling()} />);
    fireEvent.click(screen.getByText(/每日批次（2 批/));
    await screen.findByTestId('combo-rolling-batches');
    const bought = screen.getAllByTestId('combo-rolling-batch-bought');
    expect(bought[0].textContent).toBe('—');
  });

  it('滚动告警抬头展示', () => {
    render(
      <RollingBuildPanel {...baseProps} rolling={rolling({ warnings: ['部分成员当日无评分'] })} />,
    );
    expect(screen.getByTestId('combo-rolling-warnings')).toBeInTheDocument();
    expect(screen.getByText('部分成员当日无评分')).toBeInTheDocument();
  });

  it('可交易性告警优先展示，不被前 3 条成员级告警挤掉', () => {
    render(
      <RollingBuildPanel
        {...baseProps}
        rolling={rolling({
          warnings: ['甲成员无评分', '乙成员无评分', '丙成员无评分', 'tradability_unavailable'],
        })}
      />,
    );
    const alert = screen.getByTestId('combo-rolling-warnings');
    expect(alert.textContent).toContain('成交额数据不可用');
  });
});
