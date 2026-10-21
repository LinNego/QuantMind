/**
 * 多模型串行补推弹窗（模型治理 → 一键推理至最新）。
 *
 * 三步：预览（缺口/排除项/ETA）→ 执行（逐模型逐日进度、可取消）→ 完成（失败明细）。
 * 任务状态落在后端 `qm_model_inference_jobs`，`localStorage` 只存 job_id：
 * 关掉弹窗/切页后重新打开会恢复轮询；同一用户同一时间只允许一个活跃任务。
 */

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  Button,
  Modal,
  Popconfirm,
  Progress,
  Table,
  Tag,
  Tooltip,
  message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { CheckCircle2, Clock, RefreshCw, XCircle, Zap } from 'lucide-react';
import { modelTrainingService } from '../../../../services/modelTrainingService';
import {
  ACTIVE_JOB_STATUSES,
  backfillReasonLabel,
  formatDuration,
  type BackfillJob,
  type BackfillModelError,
  type BackfillPreview,
  type BackfillPreviewModel,
  type BackfillJobStatus,
} from '../../../../services/modelInferenceJobTypes';

/** 关掉弹窗后仍能恢复任务的唯一凭据（只存 job_id，不存进度快照）。 */
const STORAGE_KEY = 'qm:inference:backfill-job';
const DEFAULT_POLL_MS = 1500;

type Step = 'preview' | 'running' | 'done';

interface BackfillJobModalProps {
  open: boolean;
  /** 治理面板当前勾选的模型（≥2；系统模型由预览标只读） */
  modelIds: string[];
  onClose: () => void;
  /** 任务终结后通知父级刷新（可选） */
  onFinished?: () => void;
  /** 测试注入；生产固定 1.5s */
  pollIntervalMs?: number;
}

function errorMessage(error: unknown): string {
  if (error instanceof Error && error.message) return error.message;
  return '请求失败，请稍后重试';
}

const STATUS_META: Record<BackfillJobStatus, { label: string; color: string }> = {
  queued: { label: '排队中', color: 'default' },
  running: { label: '执行中', color: 'processing' },
  cancelling: { label: '取消中', color: 'warning' },
  partial: { label: '部分成功', color: 'warning' },
  completed: { label: '已完成', color: 'success' },
  failed: { label: '失败', color: 'error' },
  cancelled: { label: '已取消', color: 'default' },
};

function statusTag(status: string) {
  const meta = STATUS_META[status as BackfillJobStatus] ?? { label: status, color: 'default' };
  return <Tag color={meta.color}>{meta.label}</Tag>;
}

export const BackfillJobModal: React.FC<BackfillJobModalProps> = ({
  open,
  modelIds,
  onClose,
  onFinished,
  pollIntervalMs = DEFAULT_POLL_MS,
}) => {
  const [step, setStep] = useState<Step>('preview');
  const [preview, setPreview] = useState<BackfillPreview | null>(null);
  const [job, setJob] = useState<BackfillJob | null>(null);
  const [loading, setLoading] = useState(false);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pollError, setPollError] = useState<string | null>(null);

  const timerRef = useRef<number | null>(null);
  const modelIdsRef = useRef<string[]>(modelIds);
  modelIdsRef.current = modelIds;

  const stopPolling = useCallback(() => {
    if (timerRef.current != null) {
      window.clearInterval(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  const applyJob = useCallback(
    (next: BackfillJob) => {
      setJob(next);
      setPollError(null);
      if (ACTIVE_JOB_STATUSES.has(next.status)) {
        setStep('running');
        return;
      }
      stopPolling();
      localStorage.removeItem(STORAGE_KEY);
      setStep('done');
      onFinished?.();
    },
    [onFinished, stopPolling],
  );

  const startPolling = useCallback(
    (jobId: string, intervalMs: number) => {
      stopPolling();
      timerRef.current = window.setInterval(() => {
        // 页面隐藏时暂停：后台标签页不烧请求，回到前台立即补一次
        if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return;
        void modelTrainingService
          .getBackfillJob(jobId)
          .then(applyJob)
          .catch((err: unknown) => setPollError(errorMessage(err)));
      }, intervalMs);
    },
    [applyJob, stopPolling],
  );

  const loadPreview = useCallback(async () => {
    const ids = modelIdsRef.current;
    if (ids.length < 2) {
      setError('至少选择 2 个模型');
      return;
    }
    setLoading(true);
    setError(null);
    setPollError(null);
    try {
      const data = await modelTrainingService.previewBackfillJobs(ids);
      setPreview(data);
      setStep('preview');
    } catch (err: unknown) {
      setError(errorMessage(err));
    } finally {
      setLoading(false);
    }
  }, []);

  // 打开：先尝试恢复未完成任务；没有则拉预览
  useEffect(() => {
    if (!open) {
      stopPolling();
      return;
    }
    setError(null);
    setPollError(null);
    const stored = localStorage.getItem(STORAGE_KEY);
    if (!stored) {
      void loadPreview();
      // 预览分支同样要交还清理函数：起任务后轮询由本 effect 之外的 startPolling 接管，
      // 卸载（而非关闭）时若不清理，定时器会一直打到任务终态。
      return stopPolling;
    }
    setLoading(true);
    void modelTrainingService
      .getBackfillJob(stored)
      .then((restored) => {
        if (ACTIVE_JOB_STATUSES.has(restored.status)) {
          setJob(restored);
          setStep('running');
          startPolling(stored, pollIntervalMs);
        } else {
          localStorage.removeItem(STORAGE_KEY);
          void loadPreview();
        }
      })
      .catch(() => {
        // job 不存在/无权访问：清掉脏键，回到预览
        localStorage.removeItem(STORAGE_KEY);
        void loadPreview();
      })
      .finally(() => setLoading(false));
    return stopPolling;
    // modelIds 变化不重跑：弹窗打开期间选择已冻结（modelIdsRef）
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, pollIntervalMs, loadPreview, startPolling, stopPolling]);

  const handleStart = async () => {
    setCreating(true);
    setError(null);
    try {
      const created = await modelTrainingService.createBackfillJob(modelIdsRef.current, 'gaps');
      if (ACTIVE_JOB_STATUSES.has(created.status)) {
        localStorage.setItem(STORAGE_KEY, created.job_id);
        setJob(created);
        setStep('running');
        startPolling(created.job_id, pollIntervalMs);
      } else {
        // 零缺口：后端直接终结，无需轮询
        applyJob(created);
      }
    } catch (err: unknown) {
      setError(errorMessage(err));
    } finally {
      setCreating(false);
    }
  };

  const handleCancelJob = async () => {
    if (!job) return;
    try {
      const cancelled = await modelTrainingService.cancelBackfillJob(job.job_id);
      applyJob(cancelled);
      message.info('已请求取消：当前交易日完成后停止');
    } catch (err: unknown) {
      message.error(errorMessage(err));
    }
  };

  const previewColumns: ColumnsType<BackfillPreviewModel> = useMemo(
    () => [
      {
        title: '模型',
        dataIndex: 'display_name',
        width: 220,
        ellipsis: true,
        render: (name: string, row) => (
          <Tooltip title={row.model_id}>
            <span className="text-xs font-bold text-slate-800">{name || row.model_id}</span>
          </Tooltip>
        ),
      },
      {
        title: '架构',
        dataIndex: 'model_type',
        width: 100,
        render: (v: string) => <span className="text-[11px] text-slate-600">{v || '—'}</span>,
      },
      {
        title: '周期',
        dataIndex: 'horizon_days',
        width: 76,
        align: 'center',
        render: (v: number | null) =>
          v == null ? <span className="text-slate-300">—</span> : <span className="font-mono text-[11px]">{`T+${v}`}</span>,
      },
      {
        title: '最新数据日',
        dataIndex: 'latest_data_date',
        width: 108,
        render: (v: string | null) => (
          <span className="font-mono text-[11px] text-slate-600">{v ? String(v).slice(0, 10) : '—'}</span>
        ),
      },
      {
        title: '缺口',
        dataIndex: 'gap_count',
        width: 76,
        align: 'right',
        render: (v: number, row) => (
          <span
            className={
              row.eligible
                ? 'font-mono text-[11px] font-bold text-amber-700'
                : 'font-mono text-[11px] text-slate-300'
            }
          >
            {row.eligible ? `${v} 日` : '—'}
          </span>
        ),
      },
      {
        title: '预计',
        dataIndex: 'estimated_seconds',
        width: 88,
        align: 'right',
        render: (v: number, row) => (
          <span className="font-mono text-[11px] text-slate-600">
            {row.eligible ? formatDuration(v) : '—'}
          </span>
        ),
      },
      {
        title: '状态',
        dataIndex: 'eligible',
        width: 110,
        render: (eligible: boolean, row) =>
          eligible ? (
            <span className="text-[11px] font-bold text-emerald-700">可补推</span>
          ) : (
            <Tooltip title={row.reason ?? ''}>
              <span className="text-[11px] font-bold text-slate-400">
                {backfillReasonLabel(row.reason)}
              </span>
            </Tooltip>
          ),
      },
    ],
    [],
  );

  const eligible = preview?.models.filter((row) => row.eligible) ?? [];
  const canStart = eligible.length >= 2 && (preview?.totals.days ?? 0) > 0;

  const footer =
    step === 'preview' ? (
      <div className="flex items-center justify-end gap-2">
        <Button onClick={onClose}>取消</Button>
        <Button
          type="primary"
          icon={<Zap size={14} />}
          loading={creating}
          disabled={!canStart}
          onClick={() => void handleStart()}
        >
          开始串行补推
        </Button>
      </div>
    ) : step === 'running' ? (
      <div className="flex items-center justify-between gap-2">
        <span className="text-[11px] text-slate-500">串行执行，同一时间仅一个补推任务</span>
        <div className="flex items-center gap-2">
          <Popconfirm
            title="取消补推任务"
            description="当前交易日完成后停止；已完成的天数会保留。"
            okText="请求取消"
            cancelText="继续执行"
            onConfirm={() => void handleCancelJob()}
          >
            <Button danger disabled={Boolean(job?.cancel_requested)}>
              {job?.cancel_requested ? '已请求取消' : '取消任务'}
            </Button>
          </Popconfirm>
          <Button onClick={onClose}>后台运行</Button>
        </div>
      </div>
    ) : (
      <div className="flex items-center justify-end gap-2">
        <Button icon={<RefreshCw size={13} />} onClick={() => void loadPreview()} loading={loading}>
          重新检查缺口
        </Button>
        <Button type="primary" onClick={onClose}>
          关闭
        </Button>
      </div>
    );

  return (
    <Modal
      open={open}
      title={
        <span className="flex items-center gap-2 text-sm font-black text-slate-800">
          <Zap size={15} className="text-blue-600" /> 一键推理至最新
          <span className="text-[11px] font-normal text-slate-400">串行补推 · 逐模型逐日</span>
        </span>
      }
      width={900}
      footer={footer}
      onCancel={step === 'running' ? undefined : onClose}
      maskClosable={false}
      destroyOnHidden
    >
      <div className="space-y-3" data-testid="backfill-job-body">
        {error && (
          <div data-testid="backfill-error">
            <Alert type="error" showIcon message={error} />
          </div>
        )}
        {pollError && step === 'running' && (
          <div data-testid="backfill-poll-error">
            <Alert type="warning" showIcon message={`进度读取失败，正在重试：${pollError}`} />
          </div>
        )}

        {step === 'preview' && (
          <div className="space-y-3" data-testid="backfill-preview">
            {preview?.warnings?.length ? (
              <div data-testid="backfill-warnings">
                <Alert
                  type="info"
                  showIcon
                  description={
                    <div className="space-y-0.5">
                      {preview.warnings.map((w) => (
                        <div key={w} className="text-[11px]">
                          {w}
                        </div>
                      ))}
                    </div>
                  }
                />
              </div>
            ) : null}

            <div data-testid="backfill-preview-table">
              <Table<BackfillPreviewModel>
                rowKey="model_id"
                size="small"
                loading={loading}
                dataSource={preview?.models ?? []}
                columns={previewColumns}
                pagination={false}
                scroll={{ y: 260 }}
                locale={{ emptyText: loading ? ' ' : '没有可预览的模型' }}
              />
            </div>

            {preview?.excluded?.length ? (
              <div className="flex items-center gap-1.5 flex-wrap" data-testid="backfill-excluded">
                <span className="text-[11px] font-bold text-slate-500">已排除：</span>
                {preview.excluded.map((row) => (
                  <Tag key={row.model_id} color="default">
                    {row.model_id} · {backfillReasonLabel(row.reason)}
                  </Tag>
                ))}
              </div>
            ) : null}

            <div className="flex items-center gap-4 text-[11px] text-slate-600 bg-slate-50 border border-slate-200 rounded-lg px-3 py-2">
              <span>
                可补推 <strong className="font-mono text-slate-900">{eligible.length}</strong> 个模型
              </span>
              <span>
                合计 <strong className="font-mono text-slate-900">{preview?.totals.days ?? 0}</strong> 个交易日
              </span>
              <span>
                预计{' '}
                <strong className="font-mono text-slate-900">
                  {formatDuration((preview?.totals.estimated_seconds ?? 0) * 0.7)}–
                  {formatDuration((preview?.totals.estimated_seconds ?? 0) * 1.5)}
                </strong>
              </span>
              <span className="ml-auto text-slate-400">缺口口径含历史中间空洞</span>
            </div>
          </div>
        )}

        {step === 'running' && job && (
          <div className="space-y-3" data-testid="backfill-running">
            <div className="flex items-center justify-between">
              <span className="text-xs font-bold text-slate-700 flex items-center gap-1.5">
                <Clock size={13} className="text-blue-600" />
                {job.current
                  ? `当前：${job.current.model_id}${job.current.date ? ` · ${job.current.date}` : ''}`
                  : '等待执行'}
              </span>
              {statusTag(job.status)}
            </div>
            <div data-testid="backfill-progress">
              <Progress
                percent={Math.round((job.progress ?? 0) * 100)}
                status={job.cancel_requested ? 'exception' : 'active'}
              />
            </div>
            <div className="grid grid-cols-4 gap-2 text-center text-[11px]">
              <div className="rounded-lg border border-slate-200 py-1.5">
                <div className="text-slate-400">总交易日</div>
                <div className="font-mono font-black text-slate-800">{job.totals.days}</div>
              </div>
              <div className="rounded-lg border border-slate-200 py-1.5">
                <div className="text-slate-400">成功</div>
                <div className="font-mono font-black text-emerald-700">{job.totals.completed_days}</div>
              </div>
              <div className="rounded-lg border border-slate-200 py-1.5">
                <div className="text-slate-400">失败</div>
                <div className="font-mono font-black text-rose-700">{job.totals.failed_days}</div>
              </div>
              <div className="rounded-lg border border-slate-200 py-1.5">
                <div className="text-slate-400">跳过</div>
                <div className="font-mono font-black text-amber-700">{job.totals.skipped_days}</div>
              </div>
            </div>
            <div className="space-y-1.5 max-h-56 overflow-auto pr-1" data-testid="backfill-model-progress">
              {job.models.map((row) => {
                const done = row.days_completed + row.days_failed + row.days_skipped;
                const percent = row.days_total ? Math.round((done / row.days_total) * 100) : 0;
                return (
                  <div key={row.model_id} className="flex items-center gap-2">
                    <Tooltip title={row.model_id}>
                      <span className="w-44 shrink-0 truncate text-[11px] font-bold text-slate-700">
                        {row.display_name || row.model_id}
                      </span>
                    </Tooltip>
                    <Progress percent={percent} size="small" className="flex-1 !mb-0" />
                    <span className="w-16 shrink-0 text-right font-mono text-[10px] text-slate-500">
                      {done}/{row.days_total} 日
                    </span>
                    <span className="w-14 shrink-0 text-right text-[10px] text-slate-400">
                      并 {row.days_merged}
                    </span>
                  </div>
                );
              })}
            </div>
            {job.logs.length > 0 && (
              <pre className="max-h-28 overflow-auto rounded-lg bg-slate-900 p-2 text-[10px] text-slate-100">
                {job.logs.slice(-8).join('\n')}
              </pre>
            )}
          </div>
        )}

        {step === 'done' && job && (
          <div className="space-y-3" data-testid="backfill-done">
            <Alert
              type={job.status === 'completed' ? 'success' : job.status === 'cancelled' ? 'info' : 'warning'}
              showIcon
              icon={job.status === 'completed' ? <CheckCircle2 size={14} /> : <XCircle size={14} />}
              message={
                job.status === 'completed'
                  ? `补推完成：成功 ${job.totals.completed_days} 日`
                  : job.status === 'cancelled'
                    ? `已取消：完成 ${job.totals.completed_days} 日，未执行 ${Math.max(
                        0,
                        job.totals.days - job.totals.completed_days - job.totals.failed_days - job.totals.skipped_days,
                      )} 日`
                    : job.error || `部分成功：成功 ${job.totals.completed_days} 日，失败 ${job.totals.failed_days} 日`
              }
              description={
                <span className="text-[11px]">
                  成功 {job.totals.completed_days} · 失败 {job.totals.failed_days} · 跳过{' '}
                  {job.totals.skipped_days} · 共 {job.totals.days} 个交易日
                </span>
              }
            />
            <div data-testid="backfill-failures">
              <Table<BackfillModelError & { model_id: string }>
                rowKey={(row) => `${row.model_id}:${row.date}`}
                size="small"
                dataSource={job.models.flatMap((row) =>
                  row.errors.map((err) => ({ ...err, model_id: row.model_id })),
                )}
                pagination={false}
                scroll={{ y: 180 }}
                locale={{ emptyText: '没有失败记录' }}
                columns={[
                  { title: '模型', dataIndex: 'model_id', width: 180, ellipsis: true },
                  { title: '日期', dataIndex: 'date', width: 110 },
                  { title: '原因', dataIndex: 'error', ellipsis: true },
                ]}
              />
            </div>
          </div>
        )}
      </div>
    </Modal>
  );
};

export default BackfillJobModal;
