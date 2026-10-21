/**
 * 组合选股工作区：状态编排的边界都在这里——
 * 空列表自动建 C0、模板只能预览、滚动/计划需要已保存组合、无评分日不给假榜单。
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import type {
  BlendRankingResponse,
  BlendRecord,
  BlendRollingResponse,
  ComboAutoCandidatesResponse,
  ComboTemplate,
} from '../../../services/blendTypes';

const mocks = vi.hoisted(() => ({
  listBlends: vi.fn(),
  createBlend: vi.fn(),
  patchBlend: vi.fn(),
  archiveBlend: vi.fn(),
  getEligible: vi.fn(),
  getAutoCandidates: vi.fn(),
  preview: vi.fn(),
  getDates: vi.fn(),
  getRanking: vi.fn(),
  getRolling: vi.fn(),
  createPlan: vi.fn(),
  listPlans: vi.fn(),
  confirmPlan: vi.fn(),
  getEvidence: vi.fn(),
}));

vi.mock('../../../services/blendService', () => ({
  blendService: mocks,
  blendErrorMessage: (err: unknown) => (err instanceof Error ? err.message : '请求失败'),
}));

import { ComboWorkspace } from '../components/combo/ComboWorkspace';

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
    members: [
      { model_id: 'm1', model_source: 'manual', weight: 1 },
      { model_id: 'm2', model_source: 'manual', weight: 1 },
    ],
    ...over,
  };
}

function template(over: Partial<ComboTemplate> = {}): ComboTemplate {
  return {
    template_id: 'tpl-base',
    kind: 'baseline',
    name: '全13·T+10 等权',
    market: 'CN',
    horizon_days: 10,
    method: 'mean',
    trim_count: 2,
    min_coverage: 2,
    consensus_min_count: 0,
    members: [
      { model_id: 'm1', model_source: 'manual', weight: 1, model_type: 'lightgbm', display_name: 'LGB' },
      { model_id: 'm2', model_source: 'manual', weight: 1, model_type: 'xgboost', display_name: 'XGB' },
    ],
    evidence: { grade: 'watch', candidate_id: null },
    baseline: true,
    ...over,
  };
}

function autoCandidates(): ComboAutoCandidatesResponse {
  return {
    market: 'CN',
    items: [template()],
    baseline_template_id: 'tpl-base',
    evidence_available: true,
    evidence_generated_at: '2026-10-21T02:00:00Z',
    excluded: [],
    warnings: [],
  };
}

function ranking(): BlendRankingResponse {
  return {
    data_trade_date: '2026-10-20',
    members: [],
    warnings: [],
    ranking: [
      {
        code: 'SH600000', symbol: 'SH600000', name: '浦发银行', S: 0.9, S_pct: 0.95, score: 0.9, pct: 0.95,
        rank: 1, research_score: 95, position: 'BUY', coverage: 13, dispersion: 0.01,
        dispersion_band: 'low', contributions: {}, missing_members: [],
      },
    ],
  };
}

function rolling(): BlendRollingResponse {
  return {
    blend_id: 'blend-1',
    data_trade_date: '2026-10-20',
    target_date: '2026-10-21',
    window: 10,
    per_day: 5,
    days: ['2026-10-20'],
    columns: [
      {
        date: '2026-10-20',
        picks: [
          { symbol: 'SH600001', code: 'SH600001', name: '甲', score: 0.5, rank: 1, row_rank: 1, coverage: 13, pct: 0.9, position: 'BUY', held_days: 0, is_new: true },
        ],
        exits: [],
      },
    ],
    members: [
      { model_id: 'm1', display_name: 'LGB', model_source: 'manual', model_type: 'lightgbm', horizon_days: 10, weight: 1, available: true },
    ],
    summary: { unique_symbols: 1, new_today: 1, exited_today: 0 },
    warnings: [],
  };
}

function renderWorkspace() {
  return render(<ComboWorkspace market="CN" marketLabel="A股市场" />);
}

beforeEach(() => {
  Object.values(mocks).forEach((fn) => fn.mockReset());
  mocks.listBlends.mockResolvedValue([blend()]);
  mocks.getAutoCandidates.mockResolvedValue(autoCandidates());
  mocks.createBlend.mockResolvedValue(blend({ blend_id: 'blend-new' }));
  mocks.getDates.mockResolvedValue({ blend_id: 'blend-1', dates: ['2026-10-20', '2026-10-19'] });
  mocks.getRanking.mockResolvedValue(ranking());
  mocks.preview.mockResolvedValue(ranking());
  mocks.getRolling.mockResolvedValue(rolling());
  mocks.listPlans.mockResolvedValue({ items: [] });
  mocks.createPlan.mockResolvedValue({});
  mocks.confirmPlan.mockResolvedValue({});
  mocks.getEvidence.mockResolvedValue({ available: false, path: '/data/x/evidence.json' });
});

describe('ComboWorkspace', () => {
  it('首次进入空列表：自动把基线模板建成默认组合，并拉当日榜单', async () => {
    mocks.listBlends.mockResolvedValue([]);
    renderWorkspace();

    await screen.findByText('浦发银行');
    expect(mocks.createBlend).toHaveBeenCalledTimes(1);
    expect(mocks.createBlend.mock.calls[0][0]).toMatchObject({
      name: '全13·T+10 等权',
      market: 'CN',
      model_ids: ['m1', 'm2'],
      source: 'system',
      template_id: 'tpl-base',
      weights: null,
    });
    expect(mocks.getDates).toHaveBeenCalledWith('blend-new');
    await waitFor(() =>
      expect(mocks.getRanking).toHaveBeenCalledWith('blend-new', { date: '2026-10-20' }),
    );
  });

  it('已有组合时不再建 C0，榜单带数据日', async () => {
    renderWorkspace();
    await screen.findByText('浦发银行');
    expect(mocks.createBlend).not.toHaveBeenCalled();
    expect(mocks.getRanking).toHaveBeenCalledWith('blend-1', { date: '2026-10-20' });
    expect(screen.getAllByText('全13·T+10 等权').length).toBeGreaterThan(0);
  });

  it('切到模板只走 /preview，滚动 Tab 提示先保存组合', async () => {
    renderWorkspace();
    await screen.findByText('浦发银行');

    fireEvent.click(screen.getByTestId('combo-item-template-tpl-base'));
    await waitFor(() => expect(mocks.preview).toHaveBeenCalledTimes(1));
    expect(mocks.preview.mock.calls[0][0]).toMatchObject({
      market: 'CN',
      model_ids: ['m1', 'm2'],
      method: 'mean',
    });

    fireEvent.click(screen.getByRole('tab', { name: '滚动建仓' }));
    expect(await screen.findByText(/保存为组合后可查看滚动建仓/)).toBeInTheDocument();
    expect(mocks.getRolling).not.toHaveBeenCalled();
  });

  it('已保存组合进入滚动 Tab 才请求，渲染新进色块', async () => {
    renderWorkspace();
    await screen.findByText('浦发银行');
    fireEvent.click(screen.getByRole('tab', { name: '滚动建仓' }));
    await waitFor(() => expect(mocks.getRolling).toHaveBeenCalled());
    expect(await screen.findByTestId('combo-rolling-pick-2026-10-20-SH600001')).toHaveTextContent('新进');
  });

  it('组合没有任何完整评分日：榜单给明确空因，不发无日期请求', async () => {
    mocks.getDates.mockResolvedValue({ blend_id: 'blend-1', dates: [] });
    renderWorkspace();
    expect(await screen.findByText('该组合暂无完整评分的交易日')).toBeInTheDocument();
    expect(mocks.getRanking).not.toHaveBeenCalled();
  });

  it('证据 Tab 懒加载证据包并经面板呈现空态', async () => {
    renderWorkspace();
    await screen.findByText('浦发银行');
    expect(mocks.getEvidence).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('tab', { name: '证据与回放' }));
    expect(await screen.findByText('证据未生成')).toBeInTheDocument();
    expect(mocks.getEvidence).toHaveBeenCalledTimes(1);
  });

  it('调仓计划 Tab：读取未确认计划，二次确认后落到 confirmPlan', async () => {
    const plan = {
      plan_id: 'plan-1',
      blend_id: 'blend-1',
      data_trade_date: '2026-10-20',
      target_date: '2026-10-21',
      params: {
        capital: 10_000_000, top_k: 50, hold_days: 10, entry_per_day: 5, target_positions: 50,
        cash_buffer: 0.02, adv_participation: 0.05, exit_delta: 0.05, lookback: 20, min_lookback_days: 10,
      },
      summary: {
        capital: 10_000_000, target_positions: 50, entry_per_day: 5, hold_days: 10, top_k: 50,
        cash_buffer: 0.02, n_entries: 1, n_holds: 0, n_exits: 0, n_open_after: 1,
      },
      entries: [{ symbol: 'SH600001', mean20: 0.6, exit_line: 0.5 }],
      holds: [],
      exits: [],
      warnings: [],
      confirmed_at: null,
    };
    mocks.listPlans.mockResolvedValue({ items: [plan] });
    mocks.confirmPlan.mockResolvedValue({ ...plan, confirmed_at: '2026-10-20T15:10:00Z' });
    mocks.createPlan.mockResolvedValue({ ...plan, plan_id: 'plan-2' });

    renderWorkspace();
    await screen.findByText('浦发银行');
    fireEvent.click(screen.getByRole('tab', { name: '调仓计划' }));
    await waitFor(() => expect(mocks.listPlans).toHaveBeenCalledWith('blend-1', { date: '2026-10-20', limit: 1 }));
    expect(await screen.findByText('SH600001')).toBeInTheDocument();

    fireEvent.click(screen.getByTestId('combo-plan-confirm'));
    fireEvent.click(await screen.findByRole('button', { name: '确认（仅纸面）' }));
    await waitFor(() => expect(mocks.confirmPlan).toHaveBeenCalledWith('blend-1', 'plan-1'));

    // 参数沿用计划自身配置；重新生成必须持久化（persist: true）
    fireEvent.click(screen.getByRole('button', { name: '重新生成' }));
    await waitFor(() =>
      expect(mocks.createPlan).toHaveBeenCalledWith('blend-1', {
        data_trade_date: '2026-10-20',
        persist: true,
        capital: 10_000_000,
        top_k: 50,
        hold_days: 10,
      }),
    );
  });

  it('新建按钮与治理面板带入的模型都能打开构建器（预填同组模型）', async () => {
    mocks.getEligible.mockResolvedValue({
      market: 'CN',
      total_eligible: 2,
      excluded: [],
      groups: [
        {
          key: 'g1',
          signature: { target_horizon_days: 10, feature_count: 300, data_source: 'quantdb_factors' },
          models: [
            { model_id: 'm1', display_name: 'LGB', model_type: 'lightgbm', model_source: 'manual', horizon_days: 10, feature_count: 300, status: 'ready' },
            { model_id: 'm2', display_name: 'XGB', model_type: 'xgboost', model_source: 'manual', horizon_days: 10, feature_count: 300, status: 'ready' },
          ],
        },
      ],
    });

    const { unmount } = renderWorkspace();
    await screen.findByText('浦发银行');
    fireEvent.click(screen.getByRole('button', { name: /新\s*建/ }));
    await waitFor(() => expect(mocks.getEligible).toHaveBeenCalledWith('CN'));
    unmount();

    mocks.getEligible.mockClear();
    render(
      <ComboWorkspace market="CN" marketLabel="A股市场" seedModelIds={['m1', 'm2']} seedNonce={1} />,
    );
    await waitFor(() => expect(mocks.getEligible).toHaveBeenCalledTimes(1));
    expect(await screen.findByText(/2 只成员 · T\+10 · 300 维/)).toBeInTheDocument();
  });
});
