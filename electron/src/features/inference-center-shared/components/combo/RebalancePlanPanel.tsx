/**
 * 组合选股 · 纸面调仓计划。
 *
 * **只写本地纸面台账**：页面不出现「买入/卖出」，确认按钮明确写「仅纸面记录」，
 * 本组件不引用任何交易服务。计划参数的「资金/TopK/持有日」只影响本面板，
 * 不改组合配置（后端以 PlanBody 覆盖项处理）。
 */

import React, { useEffect, useState } from 'react';
import {
  Alert,
  Button,
  Empty,
  InputNumber,
  Popconfirm,
  Select,
  Spin,
  Table,
  Tag,
  Tooltip,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { CalendarClock, RefreshCw } from 'lucide-react';
import type { BlendPlan, BlendPlanOverrides } from '../../../../services/blendTypes';
import { formatResearchScore } from '../../../shared/researchScore';
import {
  formatAmount,
  isPlanStale,
  planDisplayRows,
  type PlanDisplayRow,
} from './comboRankingModel';

export interface RebalancePlanPanelProps {
  plan: BlendPlan | null;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
  onGenerate: (overrides: BlendPlanOverrides) => Promise<void>;
  onConfirm: () => Promise<void>;
  /** `/dates` 返回的降序可用日；用于「计划是否过期」判定 */
  dates: string[];
  isTemplate: boolean;
  onSaveTemplate?: () => void;
}

const CAPITAL_OPTIONS = [
  { value: 500, label: '500 万' },
  { value: 1000, label: '1000 万' },
  { value: 3000, label: '3000 万' },
  { value: 5000, label: '5000 万' },
  { value: 10000, label: '1 亿' },
];

const CATEGORY_CLASS: Record<PlanDisplayRow['category'], string> = {
  新进: 'border-blue-200 bg-blue-50 text-blue-700',
  留存: 'border-slate-200 bg-slate-50 text-slate-600',
  退出: 'border-amber-200 bg-amber-50 text-amber-700',
};

export const RebalancePlanPanel: React.FC<RebalancePlanPanelProps> = ({
  plan,
  loading,
  error,
  onRetry,
  onGenerate,
  onConfirm,
  dates,
  isTemplate,
  onSaveTemplate,
}) => {
  const [capitalWan, setCapitalWan] = useState(1000);
  const [topK, setTopK] = useState(50);
  const [holdDays, setHoldDays] = useState(10);

  useEffect(() => {
    if (!plan?.params) return;
    setCapitalWan(Math.round(plan.params.capital / 10000));
    setTopK(plan.params.top_k);
    setHoldDays(plan.params.hold_days);
  }, [plan?.params]);

  if (isTemplate) {
    return (
      <div className="flex-1 min-h-0 flex items-center justify-center bg-white border border-slate-200 rounded-xl">
        <Empty
          description={
            <span className="text-xs text-slate-500">
              纸面计划需要已保存的组合（滚动窗口与台账都挂在组合上）
            </span>
          }
        >
          {onSaveTemplate && (
            <Button type="primary" onClick={onSaveTemplate}>
              保存当前模板为组合
            </Button>
          )}
        </Empty>
      </div>
    );
  }

  if (error && !plan) {
    return (
      <div className="p-3" data-testid="combo-plan-error">
        <Alert
          type="error"
          showIcon
          message={error}
          action={
            <Button size="small" onClick={onRetry}>
              重试
            </Button>
          }
        />
      </div>
    );
  }

  if (loading && !plan) {
    return (
      <div className="flex-1 min-h-0 flex items-center justify-center bg-white border border-slate-200 rounded-xl">
        <Spin tip="生成纸面计划…" />
      </div>
    );
  }

  if (!plan) {
    return (
      <div className="flex-1 min-h-0 flex items-center justify-center bg-white border border-slate-200 rounded-xl" data-testid="combo-plan-empty">
        <Empty description="当日还没有纸面计划">
          <Button type="primary" loading={loading} onClick={() => void onGenerate({ capital: capitalWan * 10000, top_k: topK, hold_days: holdDays })}>
            生成今日计划
          </Button>
        </Empty>
      </div>
    );
  }

  const stale = isPlanStale(plan.data_trade_date, dates);
  const persisted = Boolean(plan.plan_id);
  const confirmed = Boolean(plan.confirmed_at);
  const rows = planDisplayRows(plan);

  const columns: ColumnsType<PlanDisplayRow> = [
    {
      title: '类别',
      dataIndex: 'category',
      width: 64,
      align: 'center',
      render: (v: PlanDisplayRow['category']) => (
        <span className={`rounded border px-1.5 py-0.5 text-[10px] font-bold ${CATEGORY_CLASS[v]}`}>{v}</span>
      ),
    },
    {
      title: '代码',
      dataIndex: 'symbol',
      width: 104,
      render: (v: string) => <span className="font-mono text-[11px] font-bold text-slate-800">{v}</span>,
    },
    {
      title: '综合分',
      dataIndex: 'research_score',
      width: 76,
      align: 'right',
      render: (v: number | null) => <span className="font-mono text-[11px]">{formatResearchScore(v)}</span>,
    },
    {
      title: '20日均分',
      dataIndex: 'mean20',
      width: 84,
      align: 'right',
      render: (v: number | null) => (
        <span className="font-mono text-[11px] text-slate-600">{typeof v === 'number' ? v.toFixed(3) : '—'}</span>
      ),
    },
    {
      title: '退出线',
      dataIndex: 'exit_line',
      width: 84,
      align: 'right',
      render: (v: number | null) => (
        <span className="font-mono text-[11px] text-slate-600">{typeof v === 'number' ? v.toFixed(3) : '—'}</span>
      ),
    },
    {
      title: '目标金额',
      dataIndex: 'target_value',
      width: 96,
      align: 'right',
      render: (v: number | undefined) => <span className="font-mono text-[11px]">{formatAmount(v)}</span>,
    },
    {
      title: '参考价',
      dataIndex: 'ref_price',
      width: 80,
      align: 'right',
      render: (v: number | undefined) => (
        <span className="font-mono text-[11px] text-slate-600">
          {typeof v === 'number' && Number.isFinite(v) ? v.toFixed(2) : '—'}
        </span>
      ),
    },
    {
      title: '股数',
      dataIndex: 'shares',
      width: 80,
      align: 'right',
      render: (v: number | undefined) => (
        <span className="font-mono text-[11px] text-slate-600">
          {typeof v === 'number' && Number.isFinite(v) ? v.toLocaleString('zh-CN') : '—'}
        </span>
      ),
    },
    {
      title: '持有天数',
      dataIndex: 'held_days',
      width: 80,
      align: 'center',
      render: (v: number | undefined) => (
        <span className="font-mono text-[11px] text-slate-600">{typeof v === 'number' ? v : '—'}</span>
      ),
    },
    {
      title: '备注',
      dataIndex: 'note',
      ellipsis: true,
      render: (v: string | undefined, row) => (
        <Tooltip title={[row.exit_reason, v].filter(Boolean).join(' · ')}>
          <span className="text-[10px] text-slate-500">{[row.exit_reason, v].filter(Boolean).join(' · ') || '—'}</span>
        </Tooltip>
      ),
    },
  ];

  return (
    <div className="flex-1 min-h-0 flex flex-col bg-white border border-slate-200 rounded-xl overflow-hidden">
      <div className="shrink-0 flex items-center gap-2 px-3 py-2 border-b border-slate-100">
        <span className="text-[11px] font-bold text-slate-600">资金</span>
        <Select
          size="small"
          className="!w-28"
          value={capitalWan}
          options={CAPITAL_OPTIONS}
          onChange={setCapitalWan}
        />
        <span className="text-[11px] font-bold text-slate-600">TopK</span>
        <InputNumber size="small" className="!w-20" min={1} max={2000} value={topK} onChange={(v) => setTopK(Number(v ?? 50))} />
        <span className="text-[11px] font-bold text-slate-600">持有</span>
        <InputNumber size="small" className="!w-20" min={1} max={60} value={holdDays} onChange={(v) => setHoldDays(Number(v ?? 10))} />
        <span className="text-[11px] text-slate-400">交易日</span>
        <Button
          size="small"
          type="primary"
          loading={loading}
          onClick={() => void onGenerate({ capital: capitalWan * 10000, top_k: topK, hold_days: holdDays })}
        >
          {persisted ? '重新生成' : '生成今日计划'}
        </Button>
        <Button size="small" icon={<RefreshCw size={13} />} onClick={onRetry} loading={loading}>
          刷新
        </Button>
        <div className="ml-auto flex items-center gap-2">
          {!persisted && <Tag color="blue">未保存预览</Tag>}
          {confirmed && (
            <Tag color="green" className="!m-0">
              已确认 {plan.confirmed_at ? String(plan.confirmed_at).slice(0, 10) : ''}
            </Tag>
          )}
          <Popconfirm
            title="确认纸面调仓"
            description="仅纸面记录：写入本地纸面台账，不产生任何委托，不影响真实账户。"
            okText="确认（仅纸面）"
            cancelText="再看一下"
            disabled={!persisted || confirmed || stale}
            onConfirm={() => void onConfirm()}
          >
            <Button
              size="small"
              type="primary"
              ghost
              disabled={!persisted || confirmed || stale || loading}
              data-testid="combo-plan-confirm"
            >
              确认纸面调仓
            </Button>
          </Popconfirm>
        </div>
      </div>

      <div className="shrink-0 px-3 py-1.5 border-b border-slate-100 flex items-center gap-3 text-[11px] text-slate-500">
        <span className="flex items-center gap-1 text-slate-600 font-bold">
          <CalendarClock size={13} /> 数据日 <span className="font-mono">{plan.data_trade_date}</span>
        </span>
        <span>生效日 <span className="font-mono text-slate-700">{plan.target_date}</span></span>
        <span>新进 <strong className="font-mono text-blue-700">{plan.summary.n_entries}</strong></span>
        <span>留存 <strong className="font-mono text-slate-800">{plan.summary.n_holds}</strong></span>
        <span>退出 <strong className="font-mono text-amber-700">{plan.summary.n_exits}</strong></span>
        <span>现金 {(plan.summary.cash_buffer * 100).toFixed(0)}%</span>
        <span>目标持仓 {plan.summary.target_positions}</span>
        {stale && <span className="text-amber-700 font-bold">计划已过期，需在最近 2 个交易日内确认</span>}
      </div>

      {error && (
        <div className="shrink-0 px-3 pt-2" data-testid="combo-plan-inline-error">
          <Alert type="warning" showIcon message={error} />
        </div>
      )}
      {plan.warnings?.length > 0 && (
        <div className="shrink-0 px-3 pt-2" data-testid="combo-plan-warnings">
          <Alert type="warning" showIcon message={plan.warnings.slice(0, 3).join('；')} />
        </div>
      )}

      <div className="flex-1 min-h-0" data-testid="combo-plan-table">
        <Table<PlanDisplayRow>
          rowKey={(row) => `${row.category}:${row.symbol}`}
          size="small"
          dataSource={rows}
          columns={columns}
          pagination={false}
          scroll={{ y: 500 }}
          locale={{ emptyText: '计划为空' }}
        />
      </div>

      <div className="shrink-0 px-3 py-1.5 border-t border-slate-100 text-[10px] text-slate-400">
        仅纸面记录，不产生委托。「新进/留存/退出」描述组合名单变化，不构成任何交易建议。
      </div>
    </div>
  );
};

export default RebalancePlanPanel;
