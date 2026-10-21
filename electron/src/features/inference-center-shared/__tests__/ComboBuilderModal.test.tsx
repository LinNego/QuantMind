/**
 * 新建组合构建器：同特征组硬约束、三步闸门、保存请求默认值。
 * 这里不测样式，只测「点错会造出什么脏数据」的路径。
 */

import type { ComponentProps } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import type {
  BlendRankingResponse,
  BlendRecord,
  ComboEligibleResponse,
} from '../../../services/blendTypes';

const mocks = vi.hoisted(() => ({
  getEligible: vi.fn(),
  preview: vi.fn(),
  createBlend: vi.fn(),
}));

vi.mock('../../../services/blendService', () => ({
  blendService: {
    getEligible: mocks.getEligible,
    preview: mocks.preview,
    createBlend: mocks.createBlend,
  },
  blendErrorMessage: (err: unknown) => (err instanceof Error ? err.message : '请求失败'),
}));

import { ComboBuilderModal } from '../components/combo/ComboBuilderModal';

const GROUP_SIG = { target_horizon_days: 10, feature_count: 300, data_source: 'quantdb_factors', factor_schema_hash: 'abc' };

function eligible(): ComboEligibleResponse {
  return {
    market: 'CN',
    total_eligible: 4,
    groups: [
      {
        key: 'g1',
        signature: GROUP_SIG,
        models: [
          { model_id: 'm1', display_name: 'LGB', model_type: 'lightgbm', model_source: 'manual', horizon_days: 10, feature_count: 300, status: 'ready' },
          { model_id: 'm2', display_name: 'XGB', model_type: 'xgboost', model_source: 'manual', horizon_days: 10, feature_count: 300, status: 'ready' },
          { model_id: 'm3', display_name: 'TFT', model_type: 'tft', model_source: 'manual', horizon_days: 10, feature_count: 300, status: 'ready' },
        ],
      },
      {
        key: 'g2',
        signature: { ...GROUP_SIG, target_horizon_days: 5 },
        models: [
          { model_id: 'm4', display_name: 'LGB-5', model_type: 'lightgbm', model_source: 'manual', horizon_days: 5, feature_count: 300, status: 'ready' },
        ],
      },
    ],
    excluded: [{ model_id: 'old', reason: 'archived' }],
  };
}

function previewResponse(): BlendRankingResponse {
  return {
    data_trade_date: '2026-10-20',
    members: [],
    warnings: [],
    ranking: [
      {
        code: 'SH600000', symbol: 'SH600000', name: '浦发银行', S: 0.9, S_pct: 0.9, score: 0.9, pct: 0.9,
        rank: 1, research_score: 90, position: 'BUY', coverage: 2, dispersion: 0.01,
        dispersion_band: 'low', contributions: {}, missing_members: [],
      },
    ],
  };
}

function createdBlend(): BlendRecord {
  return {
    blend_id: 'blend-new', tenant_id: 'default', user_id: 'u1', name: '我的组合', market: 'CN',
    purpose: 'rebalance', source: 'manual', template_id: null, horizon_days: 10, weighting: 'equal',
    method: 'mean', trim_count: 2, min_coverage: 2, consensus_min_count: 0, entry_per_day: 5,
    rolling_window: 10, target_positions: 50, feature_signature: 'sig', data_source: 'quantdb_factors',
    status: 'active', created_at: null, updated_at: null,
  };
}

function renderModal(over: Partial<ComponentProps<typeof ComboBuilderModal>> = {}) {
  const props = {
    open: true,
    market: 'CN',
    marketLabel: 'A股市场',
    onClose: vi.fn(),
    onCreated: vi.fn(),
    ...over,
  };
  return { ...render(<ComboBuilderModal {...props} />), props };
}

// Modal 渲染在 portal（document.body），不能用 render 返回的 container 查
function checkboxes(): HTMLInputElement[] {
  return Array.from(document.querySelectorAll<HTMLInputElement>('.ant-checkbox-input'));
}

beforeEach(() => {
  mocks.getEligible.mockReset().mockResolvedValue(eligible());
  mocks.preview.mockReset().mockResolvedValue(previewResponse());
  mocks.createBlend.mockReset().mockResolvedValue(createdBlend());
});

describe('ComboBuilderModal', () => {
  it('打开即读取可参与模型，按签名组分栏并列出不可参与原因', async () => {
    renderModal();
    await screen.findByText('T+10 · 300 维 · quantdb_factors');
    expect(mocks.getEligible).toHaveBeenCalledWith('CN');
    expect(screen.getByText('T+5 · 300 维 · quantdb_factors')).toBeInTheDocument();
    expect(screen.getByText('old · archived')).toBeInTheDocument();
  });

  it('少于两只成员不能进入下一步', async () => {
    renderModal();
    await screen.findByText('LGB');
    const next = screen.getByRole('button', { name: '下一步' });
    expect(next).toBeDisabled();

    fireEvent.click(checkboxes()[0]);
    expect(next).toBeDisabled();

    fireEvent.click(checkboxes()[1]);
    await waitFor(() => expect(next).not.toBeDisabled());
    expect(screen.getByText(/2 只成员 · T\+10 · 300 维/)).toBeInTheDocument();
  });

  it('跨组预填给警告且禁止继续（组合必须同特征）', async () => {
    renderModal({ initialModelIds: ['m1', 'm4'] });
    await screen.findByText(/不属于同一特征签名组/);
    expect(screen.getByRole('button', { name: '下一步' })).toBeDisabled();
  });

  it('从另一个组勾选会整组切换，而不是混搭', async () => {
    renderModal();
    await screen.findByText('LGB');
    fireEvent.click(checkboxes()[0]);
    fireEvent.click(checkboxes()[3]); // g2 的 m4
    await waitFor(() => expect(screen.getByText(/1 只成员 · T\+5/)).toBeInTheDocument());
    expect(screen.getByRole('button', { name: '下一步' })).toBeDisabled();
  });

  it('走完三步：预览 → 保存，请求带默认滚动参数且不落权重', async () => {
    const { props } = renderModal();
    await screen.findByText('LGB');
    fireEvent.click(checkboxes()[0]);
    fireEvent.click(checkboxes()[1]);
    fireEvent.click(screen.getByRole('button', { name: '下一步' }));

    const nameInput = await screen.findByPlaceholderText('如：13 模型 T+10 等权');
    fireEvent.change(nameInput, { target: { value: '我的组合' } });
    fireEvent.click(screen.getByRole('button', { name: '预览榜单' }));

    await screen.findByText('浦发银行');
    expect(mocks.preview).toHaveBeenCalledWith({
      market: 'CN',
      model_ids: ['m1', 'm2'],
      method: 'mean',
      trim_count: 2,
      min_coverage: 2,
      consensus_min_count: 0,
      top: 50,
    });

    fireEvent.click(screen.getByRole('button', { name: '保存组合' }));
    await waitFor(() => expect(mocks.createBlend).toHaveBeenCalledTimes(1));
    expect(mocks.createBlend.mock.calls[0][0]).toMatchObject({
      name: '我的组合',
      market: 'CN',
      model_ids: ['m1', 'm2'],
      entry_per_day: 5,
      rolling_window: 10,
      target_positions: 50,
      weights: null,
      source: 'manual',
      template_id: null,
    });
    expect(props.onCreated).toHaveBeenCalledWith(createdBlend());
    expect(props.onClose).toHaveBeenCalled();
  });

  it('从模板保存：预填成员与参数，未改成员时标 system + template_id', async () => {
    const template = {
      template_id: 'sys:abc:10:baseline',
      kind: 'baseline',
      name: '全13·T+10 等权',
      market: 'CN',
      horizon_days: 10,
      method: 'mean' as const,
      trim_count: 2,
      min_coverage: 2,
      consensus_min_count: 0,
      members: [
        { model_id: 'm1', model_source: 'manual', weight: 1, model_type: 'lightgbm', display_name: 'LGB' },
        { model_id: 'm2', model_source: 'manual', weight: 1, model_type: 'xgboost', display_name: 'XGB' },
      ],
      evidence: { grade: 'watch', candidate_id: null },
      baseline: true,
    };
    const { props } = renderModal({ initialTemplate: template });
    await screen.findByText(/2 只成员/);
    fireEvent.click(screen.getByRole('button', { name: '下一步' }));
    expect(await screen.findByDisplayValue('全13·T+10 等权')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '预览榜单' }));
    await screen.findByText('浦发银行');
    fireEvent.click(screen.getByRole('button', { name: '保存组合' }));
    await waitFor(() => expect(mocks.createBlend).toHaveBeenCalledTimes(1));
    expect(mocks.createBlend.mock.calls[0][0]).toMatchObject({
      source: 'system',
      template_id: 'sys:abc:10:baseline',
    });
    expect(props.onCreated).toHaveBeenCalled();
  });

  it('读取失败时显示错误而不是空列表', async () => {
    mocks.getEligible.mockRejectedValueOnce(new Error('网关超时'));
    renderModal();
    expect(await screen.findByText('网关超时')).toBeInTheDocument();
    // 失败与「确实没有可参与模型」是两件事，不能同时给出误导性空态
    expect(screen.queryByText('没有可参与组合的模型')).toBeNull();
  });

  it('读取失败后可重试，重试成功恢复模型清单', async () => {
    mocks.getEligible
      .mockRejectedValueOnce(new Error('Model not found'))
      .mockResolvedValueOnce(eligible());
    renderModal();
    expect(await screen.findByText('Model not found')).toBeInTheDocument();
    // antd 对两字按钮自动插入空格（渲染成「重 试」）
    fireEvent.click(screen.getByRole('button', { name: /重\s*试/ }));
    expect(await screen.findByText('LGB')).toBeInTheDocument();
    expect(mocks.getEligible).toHaveBeenCalledTimes(2);
  });
});
