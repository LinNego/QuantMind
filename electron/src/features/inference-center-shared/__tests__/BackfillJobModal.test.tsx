/**
 * 串行补推弹窗（模型治理 → 一键推理至最新）。
 *
 * 钉住四件事：
 * 1) 预览把「缺口/排除项/警告」摊开，缺口为 0 时不允许起任务；
 * 2) 起任务后按轮询推进，终结时清掉 localStorage 并通知父级刷新；
 * 3) 关掉弹窗后重开能凭 job_id 恢复轮询（这是「后台运行」的全部含金量）；
 * 4) 取消是「当前交易日完成后停止」，按钮态如实反映请求已受理。
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';

vi.mock('../../../services/modelTrainingService', () => ({
  modelTrainingService: {
    previewBackfillJobs: vi.fn(),
    createBackfillJob: vi.fn(),
    getBackfillJob: vi.fn(),
    cancelBackfillJob: vi.fn(),
  },
}));

import { BackfillJobModal } from '../components/combo/BackfillJobModal';
import { modelTrainingService } from '../../../services/modelTrainingService';
import type {
  BackfillJob,
  BackfillPreview,
  BackfillPreviewModel,
} from '../../../services/modelInferenceJobTypes';

const previewBackfillJobs = vi.mocked(modelTrainingService.previewBackfillJobs);
const createBackfillJob = vi.mocked(modelTrainingService.createBackfillJob);
const getBackfillJob = vi.mocked(modelTrainingService.getBackfillJob);
const cancelBackfillJob = vi.mocked(modelTrainingService.cancelBackfillJob);

const STORAGE_KEY = 'qm:inference:backfill-job';

function modelRow(over: Partial<BackfillPreviewModel> = {}): BackfillPreviewModel {
  return {
    model_id: 'm1',
    display_name: '模型一',
    model_type: 'lightgbm',
    horizon_days: 10,
    market: 'CN',
    status: 'ready',
    latest_data_date: '2026-10-17',
    gap_count: 3,
    estimated_seconds: 180,
    eligible: true,
    reason: null,
    ...over,
  };
}

function makePreview(over: Partial<BackfillPreview> = {}): BackfillPreview {
  return {
    models: [
      modelRow(),
      modelRow({ model_id: 'm2', display_name: '模型二', gap_count: 2, estimated_seconds: 120 }),
    ],
    excluded: [{ model_id: 'sys-x', reason: 'system_readonly' }],
    totals: { models: 2, eligible_models: 2, days: 5, estimated_seconds: 300 },
    warnings: ['串行执行，同一时间仅一个补推任务'],
    ...over,
  };
}

function makeJob(over: Partial<BackfillJob> = {}): BackfillJob {
  return {
    job_id: 'job-1',
    status: 'running',
    mode: 'gaps',
    progress: 0.4,
    current: { model_id: 'm2', date: '2026-10-20' },
    totals: { models: 2, days: 5, completed_days: 2, failed_days: 0, skipped_days: 0 },
    models: [
      {
        model_id: 'm1',
        display_name: '模型一',
        status: 'completed',
        days_total: 3,
        days_completed: 3,
        days_failed: 0,
        days_skipped: 0,
        days_merged: 3,
        errors: [],
        gap_dates: [],
      },
      {
        model_id: 'm2',
        display_name: '模型二',
        status: 'running',
        days_total: 2,
        days_completed: 1,
        days_failed: 0,
        days_skipped: 0,
        days_merged: 1,
        errors: [],
        gap_dates: [],
      },
    ],
    logs: ['2026-10-21T09:00:00Z m1 2026-10-20 ok'],
    error: null,
    cancel_requested: false,
    created_at: '2026-10-21T09:00:00Z',
    started_at: '2026-10-21T09:00:01Z',
    finished_at: null,
    heartbeat_at: '2026-10-21T09:00:05Z',
    ...over,
  };
}

function renderModal(onFinished = vi.fn()) {
  return render(
    <BackfillJobModal
      open
      modelIds={['m1', 'm2']}
      onClose={vi.fn()}
      onFinished={onFinished}
      pollIntervalMs={10}
    />,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  previewBackfillJobs.mockResolvedValue(makePreview());
  createBackfillJob.mockResolvedValue(makeJob());
  // 起任务即进入轮询（本文件 pollIntervalMs=10）：默认悬挂，避免用例结束时
  // 定时器打到未配置的 mock 上返回 undefined 而抛未处理异常
  getBackfillJob.mockImplementation(() => new Promise(() => {}));
});

describe('BackfillJobModal 预览', () => {
  it('展示缺口表、排除项与警告，起任务时提交当前勾选', async () => {
    renderModal();

    expect(await screen.findByText('模型一')).toBeTruthy();
    expect(previewBackfillJobs).toHaveBeenCalledWith(['m1', 'm2']);
    expect(screen.getByTestId('backfill-warnings').textContent).toContain('串行执行');
    expect(screen.getByTestId('backfill-excluded').textContent).toContain('系统模型只读');
    expect(screen.getByText('5')).toBeTruthy(); // 合计交易日
    expect(screen.getAllByText(/T\+10/).length).toBe(2);

    await userEvent.click(screen.getByRole('button', { name: /开始串行补推/ }));
    await waitFor(() => expect(createBackfillJob).toHaveBeenCalledWith(['m1', 'm2'], 'gaps'));
  });

  it('零缺口时禁用起任务，并提示没有可补推内容', async () => {
    previewBackfillJobs.mockResolvedValue(
      makePreview({ totals: { models: 2, eligible_models: 2, days: 0, estimated_seconds: 0 } }),
    );
    renderModal();

    await screen.findByText('模型一');
    expect(screen.getByRole('button', { name: /开始串行补推/ })).toBeDisabled();
  });

  it('预览失败显示错误，不渲染假表格', async () => {
    previewBackfillJobs.mockRejectedValue(new Error('coverage_failed'));
    renderModal();

    expect(await screen.findByText('coverage_failed')).toBeTruthy();
    expect(screen.getByTestId('backfill-preview-table').textContent).toContain('没有可预览的模型');
  });
});

describe('BackfillJobModal 执行', () => {
  it('起任务后进入执行态并写入恢复键', async () => {
    createBackfillJob.mockResolvedValue(makeJob({ job_id: 'job-2' }));
    // 轮询悬挂：本用例只关心「起任务 → 执行态」这一段
    getBackfillJob.mockImplementation(() => new Promise(() => {}));
    renderModal();

    await screen.findByText('模型一');
    await userEvent.click(screen.getByRole('button', { name: /开始串行补推/ }));

    expect(await screen.findByTestId('backfill-running')).toBeTruthy();
    expect(localStorage.getItem(STORAGE_KEY)).toBe('job-2');
    expect(screen.getByText(/当前：m2 · 2026-10-20/)).toBeTruthy();
  });

  it('轮询推进至终结：显示完成摘要、清恢复键并通知父级', async () => {
    createBackfillJob.mockResolvedValue(makeJob({ job_id: 'job-2' }));
    getBackfillJob.mockResolvedValue(
      makeJob({
        job_id: 'job-2',
        status: 'partial',
        progress: 1,
        current: null,
        totals: { models: 2, days: 5, completed_days: 4, failed_days: 1, skipped_days: 0 },
        models: [
          {
            ...makeJob().models[0],
            errors: [{ date: '2026-10-20', error: 'pred 合并失败' }],
          },
          { ...makeJob().models[1], status: 'completed', days_completed: 2, days_merged: 2 },
        ],
      }),
    );
    const onFinished = vi.fn();
    renderModal(onFinished);

    await screen.findByText('模型一');
    await userEvent.click(screen.getByRole('button', { name: /开始串行补推/ }));

    expect(await screen.findByText(/部分成功：成功 4 日，失败 1 日/)).toBeTruthy();
    expect(getBackfillJob).toHaveBeenCalledWith('job-2');
    expect(screen.getByTestId('backfill-failures').textContent).toContain('pred 合并失败');
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
    expect(onFinished).toHaveBeenCalled();
  });

  it('重新打开时凭 localStorage 恢复未完成任务，不再重复预览', async () => {
    localStorage.setItem(STORAGE_KEY, 'job-9');
    getBackfillJob.mockResolvedValue(makeJob({ job_id: 'job-9' }));

    renderModal();

    expect(await screen.findByTestId('backfill-running')).toBeTruthy();
    expect(getBackfillJob).toHaveBeenCalledWith('job-9');
    expect(previewBackfillJobs).not.toHaveBeenCalled();
    expect(screen.getByText(/当前：m2 · 2026-10-20/)).toBeTruthy();
  });

  it('取消请求受理后按钮改为已请求取消，任务保持运行态', async () => {
    localStorage.setItem(STORAGE_KEY, 'job-9');
    // 服务端状态可变：取消受理后轮询必须读到 cancelling，否则会用旧快照覆盖按钮态
    let serverJob = makeJob({ job_id: 'job-9' });
    getBackfillJob.mockImplementation(async () => serverJob);
    cancelBackfillJob.mockImplementation(async () => {
      serverJob = makeJob({ job_id: 'job-9', status: 'cancelling', cancel_requested: true });
      return serverJob;
    });

    renderModal();
    await screen.findByTestId('backfill-running');

    await userEvent.click(screen.getByRole('button', { name: '取消任务' }));
    await userEvent.click(await screen.findByRole('button', { name: '请求取消' }));

    await waitFor(() => expect(cancelBackfillJob).toHaveBeenCalledWith('job-9'));
    expect(await screen.findByRole('button', { name: '已请求取消' })).toBeDisabled();
    // 任务未终结：恢复键仍保留，重开弹窗还能接着看
    expect(localStorage.getItem(STORAGE_KEY)).toBe('job-9');
  });

  it('迟到的旧轮询响应不得把已终结任务拉回运行态', async () => {
    localStorage.setItem(STORAGE_KEY, 'job-9');
    const pending: Array<(job: BackfillJob) => void> = [];
    let call = 0;
    getBackfillJob.mockImplementation(() => {
      call += 1;
      if (call === 1) return Promise.resolve(makeJob({ job_id: 'job-9' }));
      return new Promise<BackfillJob>((resolve) => pending.push(resolve));
    });

    renderModal();
    await screen.findByTestId('backfill-running');

    // 至少发出两轮轮询；在 act 回调内同步取「此刻最新」的一条来终结，
    // 否则选取与 resolve 之间又会插进新轮次，旧序号的响应会被（正确地）丢弃
    await waitFor(() => expect(pending.length).toBeGreaterThanOrEqual(2), { timeout: 1000 });
    await act(async () => {
      pending[pending.length - 1](makeJob({ job_id: 'job-9', status: 'completed', progress: 1 }));
    });
    expect(await screen.findByTestId('backfill-done')).toBeTruthy();

    // 旧请求后到：若被应用会把界面拉回运行态，而轮询已停、再也纠不回来
    await act(async () => {
      pending[0](makeJob({ job_id: 'job-9' }));
    });
    expect(screen.queryByTestId('backfill-running')).toBeNull();
    expect(screen.getByTestId('backfill-done')).toBeTruthy();

    // 终态后确实停表
    const callsAtDone = getBackfillJob.mock.calls.length;
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(getBackfillJob.mock.calls.length).toBe(callsAtDone);
  });

  it('起任务响应晚于关窗：不再起轮询，恢复键照写', async () => {
    let resolveCreate: (job: BackfillJob) => void = () => {};
    createBackfillJob.mockImplementation(
      () =>
        new Promise<BackfillJob>((resolve) => {
          resolveCreate = resolve;
        }),
    );
    const { rerender } = render(
      <BackfillJobModal open modelIds={['m1', 'm2']} onClose={vi.fn()} pollIntervalMs={10} />,
    );
    await screen.findByText('模型一');
    await userEvent.click(screen.getByRole('button', { name: /开始串行补推/ }));

    rerender(
      <BackfillJobModal open={false} modelIds={['m1', 'm2']} onClose={vi.fn()} pollIntervalMs={10} />,
    );
    await act(async () => {
      resolveCreate(makeJob({ job_id: 'job-3' }));
    });

    // 任务照跑：恢复键必须落下；但轮询不得在关窗状态下启动
    await waitFor(() => expect(localStorage.getItem(STORAGE_KEY)).toBe('job-3'));
    await new Promise((resolve) => setTimeout(resolve, 40));
    expect(getBackfillJob).not.toHaveBeenCalled();
  });
});
