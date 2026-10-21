/**
 * 纸面调仓面板：确认闸门是**唯一**能把名单写进台账的动作，所以按状态逐条测。
 */

import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { BlendPlan, BlendRecord } from '../../../services/blendTypes';
import { RebalancePlanPanel } from '../components/combo/RebalancePlanPanel';

function makePlan(over: Partial<BlendPlan> = {}): BlendPlan {
  return {
    plan_id: 'plan-1',
    blend_id: 'blend-1',
    data_trade_date: '2026-10-20',
    target_date: '2026-10-21',
    params: {
      capital: 10_000_000,
      top_k: 50,
      hold_days: 10,
      entry_per_day: 5,
      target_positions: 50,
      cash_buffer: 0.02,
      adv_participation: 0.05,
      exit_delta: 0.05,
      lookback: 20,
      min_lookback_days: 10,
      min_adv_yi: 1,
      exclude_st: true,
      adv_lookback_days: 20,
      min_adv_days: 15,
    },
    summary: {
      capital: 10_000_000,
      target_positions: 50,
      entry_per_day: 5,
      hold_days: 10,
      top_k: 50,
      cash_buffer: 0.02,
      n_entries: 1,
      n_holds: 1,
      n_exits: 1,
      n_open_after: 50,
    },
    entries: [{ symbol: 'SH600001', mean20: 0.6, exit_line: 0.55, research_score: 88.2, target_value: 200_000, shares: 1000 }],
    holds: [{ symbol: 'SH600002', mean20: 0.5, exit_line: 0.45, held_days: 3 }],
    exits: [{ symbol: 'SH600003', mean20: 0.3, exit_line: 0.4, exit_reason: '评分跌破退出线' }],
    warnings: [],
    ...over,
  };
}

function makeBlend(over: Partial<BlendRecord> = {}): BlendRecord {
  return {
    blend_id: 'blend-20',
    tenant_id: 't',
    user_id: 'u',
    name: 'T+20 组合',
    market: 'CN',
    purpose: 'rebalance',
    source: 'system',
    template_id: null,
    horizon_days: 20,
    weighting: 'equal',
    method: 'mean',
    trim_count: 2,
    min_coverage: 3,
    consensus_min_count: 0,
    entry_per_day: 5,
    rolling_window: 20,
    target_positions: 100,
    feature_signature: 'sig',
    data_source: 'quantdb_factors',
    status: 'active',
    created_at: null,
    updated_at: null,
    ...over,
  };
}

const baseProps = {
  loading: false,
  error: null,
  onRetry: vi.fn(),
  onGenerate: vi.fn().mockResolvedValue(undefined),
  onConfirm: vi.fn().mockResolvedValue(undefined),
  dates: ['2026-10-20', '2026-10-19', '2026-10-18'],
};

describe('RebalancePlanPanel', () => {
  it('模板未保存时不请求计划，只给「保存为组合」出口', () => {
    const onSaveTemplate = vi.fn();
    render(
      <RebalancePlanPanel
        {...baseProps}
        plan={null}
        dates={[]}
        isTemplate
        onSaveTemplate={onSaveTemplate}
      />,
    );
    expect(screen.getByText(/纸面计划需要已保存的组合/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '保存当前模板为组合' }));
    expect(onSaveTemplate).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId('combo-plan-table')).not.toBeInTheDocument();
  });

  it('空态可生成计划，覆盖参数按资金万元换算为元', async () => {
    const onGenerate = vi.fn().mockResolvedValue(undefined);
    render(<RebalancePlanPanel {...baseProps} plan={null} onGenerate={onGenerate} isTemplate={false} />);
    fireEvent.click(screen.getByRole('button', { name: '生成今日计划' }));
    await waitFor(() =>
      expect(onGenerate).toHaveBeenCalledWith({
        capital: 10_000_000,
        top_k: 50,
        hold_days: 10,
        entry_per_day: 5,
        min_adv_yi: 1,
        exclude_st: true,
      }),
    );
  });

  it('无计划时按组合周期回填：T+20 不会被写死成 10 日/50 只', async () => {
    const onGenerate = vi.fn().mockResolvedValue(undefined);
    render(
      <RebalancePlanPanel
        {...baseProps}
        plan={null}
        blend={makeBlend()}
        onGenerate={onGenerate}
        isTemplate={false}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: '生成今日计划' }));
    await waitFor(() =>
      expect(onGenerate).toHaveBeenCalledWith({
        capital: 10_000_000,
        top_k: 100,
        hold_days: 20,
        entry_per_day: 5,
        min_adv_yi: 1,
        exclude_st: true,
      }),
    );
  });

  it('行情/风险标记缺失的计划只可预览，不能确认', () => {
    render(
      <RebalancePlanPanel
        {...baseProps}
        plan={makePlan({ data_ok: false, can_confirm: true })}
        isTemplate={false}
      />,
    );
    expect(screen.getByTestId('combo-plan-data-degraded')).toBeInTheDocument();
    expect(screen.getByTestId('combo-plan-confirm')).toBeDisabled();
    // 不是过期，不能误报「计划已过期」
    expect(screen.queryByText(/计划已过期/)).not.toBeInTheDocument();
  });

  it('三类行按新进/留存/退出渲染，退出原因单独成文', () => {
    render(<RebalancePlanPanel {...baseProps} plan={makePlan()} isTemplate={false} />);
    expect(screen.getByText('SH600001')).toBeInTheDocument();
    expect(screen.getByText('SH600002')).toBeInTheDocument();
    expect(screen.getByText('SH600003')).toBeInTheDocument();
    // 类别标签与摘要同词，按出现次数断言
    expect(screen.getAllByText('新进').length).toBeGreaterThan(0);
    expect(screen.getAllByText('留存').length).toBeGreaterThan(0);
    expect(screen.getAllByText('退出').length).toBeGreaterThan(0);
    expect(screen.getAllByText(/评分跌破退出线/).length).toBeGreaterThan(0);
  });

  it('合规：面板不出现买卖字眼，确认文案写明仅纸面记录', () => {
    const { container } = render(
      <RebalancePlanPanel {...baseProps} plan={makePlan()} isTemplate={false} />,
    );
    expect(container.textContent).not.toMatch(/买入|卖出|BUY|SELL/);
    expect(container.textContent).toContain('仅纸面记录，不产生委托');
  });

  it('未落库的计划不能确认（避免确认一个不存在的台账）', () => {
    render(
      <RebalancePlanPanel {...baseProps} plan={makePlan({ plan_id: null })} isTemplate={false} />,
    );
    expect(screen.getByTestId('combo-plan-confirm')).toBeDisabled();
    expect(screen.getByText('未保存预览')).toBeInTheDocument();
  });

  it('过期计划不能确认并给出提示', () => {
    render(
      <RebalancePlanPanel
        {...baseProps}
        plan={makePlan({ data_trade_date: '2026-10-01' })}
        isTemplate={false}
      />,
    );
    expect(screen.getByTestId('combo-plan-confirm')).toBeDisabled();
    expect(screen.getByText(/计划已过期/)).toBeInTheDocument();
  });

  it('已确认的计划不能重复确认', () => {
    render(
      <RebalancePlanPanel
        {...baseProps}
        plan={makePlan({ confirmed_at: '2026-10-20T15:10:00Z' })}
        isTemplate={false}
      />,
    );
    expect(screen.getByTestId('combo-plan-confirm')).toBeDisabled();
    expect(screen.getByText(/已确认/)).toBeInTheDocument();
  });

  it('后端 can_confirm=true：日期不在 /dates 前两位也允许确认', () => {
    // 确认窗口以服务端日历为准（评分日缺口时 /dates 序位会误判）
    render(
      <RebalancePlanPanel
        {...baseProps}
        plan={makePlan({ data_trade_date: '2026-10-01', can_confirm: true })}
        isTemplate={false}
      />,
    );
    expect(screen.getByTestId('combo-plan-confirm')).not.toBeDisabled();
    expect(screen.queryByText(/计划已过期/)).not.toBeInTheDocument();
  });

  it('后端 can_confirm=false：日期看似在窗口内也禁用确认', () => {
    render(
      <RebalancePlanPanel
        {...baseProps}
        plan={makePlan({ can_confirm: false })}
        isTemplate={false}
      />,
    );
    expect(screen.getByTestId('combo-plan-confirm')).toBeDisabled();
    expect(screen.getByText(/计划已过期/)).toBeInTheDocument();
  });

  it('计划行展示行业/市值/ADV20，缺失显示 — 而不是 0', () => {
    const plan = makePlan({
      entries: [
        {
          symbol: 'SH600001',
          name: '浦发银行',
          industry: '银行',
          market_cap_yi: 2431.6,
          adv_yi: 12.34,
          mean20: 0.6,
          exit_line: 0.55,
          research_score: 88.2,
          target_value: 200_000,
          shares: 1000,
        },
      ],
      holds: [{ symbol: 'SH600002', mean20: 0.5, exit_line: 0.45, held_days: 3 }],
      exits: [],
    });
    render(<RebalancePlanPanel {...baseProps} plan={plan} isTemplate={false} />);
    expect(screen.getByText('浦发银行')).toBeInTheDocument();
    expect(screen.getAllByText('银行').length).toBeGreaterThan(0);
    expect(screen.getByText('2432')).toBeInTheDocument();
    expect(screen.getByText('12.34')).toBeInTheDocument();
    // 缺失的 ADV/市值：该行渲染 —（不写成 0）
    const missingRow = screen.getByText('SH600002').closest('tr');
    expect(missingRow?.textContent).toContain('—');
    expect(missingRow?.textContent).not.toMatch(/\b0\.00\b/);
  });

  it('计划提示默认折叠，展开后列出全部条目', () => {
    const plan = makePlan({
      warnings: ['成交额不足:SH600009:0.50亿', '成交额缺失:SH600010', '资金用尽'],
    });
    render(<RebalancePlanPanel {...baseProps} plan={plan} isTemplate={false} />);
    expect(screen.getByText(/计划提示 3 条/)).toBeInTheDocument();
    expect(screen.queryByText('资金用尽')).not.toBeInTheDocument();
    fireEvent.click(screen.getByText(/计划提示 3 条/));
    expect(screen.getByText('资金用尽')).toBeInTheDocument();
  });

  it('勾选/取消「剔除 ST / 退市」会带进下一次生成', async () => {
    const onGenerate = vi.fn().mockResolvedValue(undefined);
    render(
      <RebalancePlanPanel
        {...baseProps}
        plan={makePlan()}
        onGenerate={onGenerate}
        isTemplate={false}
      />,
    );
    fireEvent.click(screen.getByRole('checkbox', { name: /剔除 ST/ }));
    fireEvent.click(screen.getByTestId('combo-plan-generate'));
    await waitFor(() =>
      expect(onGenerate).toHaveBeenCalledWith(
        expect.objectContaining({ exclude_st: false, min_adv_yi: 1 }),
      ),
    );
  });

  it('新鲜且已落库的计划：二次确认后才写入台账', async () => {
    const onConfirm = vi.fn().mockResolvedValue(undefined);
    render(
      <RebalancePlanPanel
        {...baseProps}
        plan={makePlan()}
        onConfirm={onConfirm}
        isTemplate={false}
      />,
    );
    const confirm = screen.getByTestId('combo-plan-confirm');
    expect(confirm).not.toBeDisabled();
    fireEvent.click(confirm);
    fireEvent.click(await screen.findByRole('button', { name: '确认（仅纸面）' }));
    await waitFor(() => expect(onConfirm).toHaveBeenCalledTimes(1));
  });
});
