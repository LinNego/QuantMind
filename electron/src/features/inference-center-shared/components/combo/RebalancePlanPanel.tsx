/**
 * 组合选股 · 纸面调仓计划。
 *
 * **只写本地纸面台账**：页面不出现「买入/卖出」，确认按钮明确写「仅纸面记录」，
 * 本组件不引用任何交易服务。资金/TopK/持有日/每日新进/最低成交额/剔除 ST
 * 只是本面板的**计划参数覆盖**（后端 `PlanBody` 覆盖项），不改组合配置。
 *
 * 表格口径：新进/留存/退出三类同表，左侧固定「类别 + 代码」，右侧横滚；
 * 成交额（ADV20）与目标金额同列展示，缺失一律 `—`（**不显示 0**）。
 */

import React, { useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Checkbox,
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
import { CalendarClock, ChevronDown, RefreshCw } from 'lucide-react';
import type {
  BlendPlan,
  BlendPlanOverrides,
  BlendRecord,
} from '../../../../services/blendTypes';
import { formatResearchScore, RESEARCH_SCORE_MISSING } from '../../../shared/researchScore';
import {
  formatAmount,
  isPlanStale,
  planDisplayRows,
  type PlanDisplayRow,
} from './comboRankingModel';

export interface RebalancePlanPanelProps {
  plan: BlendPlan | null;
  /** 当前选中的组合：无计划时用它派生「持有/目标持仓/每日新进」默认值 */
  blend?: BlendRecord | null;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
  onGenerate: (overrides: BlendPlanOverrides) => Promise<void>;
  onConfirm: () => Promise<void>;
  /** `/dates` 返回的降序可用日；仅在 can_confirm 缺失时兜底判过期 */
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

const DEFAULT_MIN_ADV_YI = 1;

function fixed2(value: number | null | undefined): string {
  return typeof value === 'number' && Number.isFinite(value)
    ? value.toFixed(2)
    : RESEARCH_SCORE_MISSING;
}

function score3(value: number | null | undefined): string {
  return typeof value === 'number' && Number.isFinite(value)
    ? value.toFixed(3)
    : RESEARCH_SCORE_MISSING;
}

function capLabel(value: number | null | undefined): string {
  return typeof value === 'number' && Number.isFinite(value)
    ? value.toFixed(0)
    : RESEARCH_SCORE_MISSING;
}

function integerLabel(value: number | null | undefined): string {
  return typeof value === 'number' && Number.isFinite(value)
    ? value.toLocaleString('zh-CN')
    : RESEARCH_SCORE_MISSING;
}

interface SummaryTileProps {
  label: string;
  value: React.ReactNode;
  hint?: string;
  tone?: 'blue' | 'slate' | 'amber' | 'indigo';
}

function SummaryTile({ label, value, hint, tone = 'slate' }: SummaryTileProps) {
  const toneClass: Record<NonNullable<SummaryTileProps['tone']>, string> = {
    blue: 'border-blue-100 bg-blue-50/70 text-blue-800',
    slate: 'border-slate-200 bg-slate-50 text-slate-700',
    amber: 'border-amber-100 bg-amber-50/70 text-amber-800',
    indigo: 'border-indigo-100 bg-indigo-50/70 text-indigo-800',
  };
  return (
    <div className={`rounded-lg border px-2.5 py-1.5 min-w-[96px] ${toneClass[tone]}`}>
      <div className="text-[10px] leading-tight opacity-70">{label}</div>
      <div className="font-mono text-sm font-black leading-tight">{value}</div>
      {hint ? <div className="text-[9px] leading-tight opacity-60">{hint}</div> : null}
    </div>
  );
}

export const RebalancePlanPanel: React.FC<RebalancePlanPanelProps> = ({
  plan,
  blend,
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
  const [entryPerDay, setEntryPerDay] = useState(5);
  const [minAdvYi, setMinAdvYi] = useState(DEFAULT_MIN_ADV_YI);
  const [excludeSt, setExcludeSt] = useState(true);
  const [warningsOpen, setWarningsOpen] = useState(false);

  useEffect(() => {
    const params = plan?.params;
    // 没有计划时也必须按组合自身配置回填：硬编码 10/50/5 会在首次生成时
    // 把 T+5/T+20 派生参数覆盖掉（持有 10 日、目标持仓 50）。
    setCapitalWan(Math.round((params?.capital ?? 10_000_000) / 10000));
    setTopK(params?.top_k ?? blend?.target_positions ?? 50);
    setHoldDays(params?.hold_days ?? blend?.rolling_window ?? 10);
    setEntryPerDay(
      typeof params?.entry_per_day === 'number' && params.entry_per_day > 0
        ? params.entry_per_day
        : (blend?.entry_per_day ?? 5),
    );
    setMinAdvYi(
      typeof params?.min_adv_yi === 'number' ? params.min_adv_yi : DEFAULT_MIN_ADV_YI,
    );
    setExcludeSt(params?.exclude_st !== false);
  }, [
    plan?.params,
    blend?.blend_id,
    blend?.rolling_window,
    blend?.target_positions,
    blend?.entry_per_day,
  ]);

  const overrides = useMemo<BlendPlanOverrides>(
    () => ({
      capital: capitalWan * 10000,
      top_k: topK,
      hold_days: holdDays,
      entry_per_day: entryPerDay,
      min_adv_yi: minAdvYi,
      exclude_st: excludeSt,
    }),
    [capitalWan, topK, holdDays, entryPerDay, minAdvYi, excludeSt],
  );

  if (isTemplate) {
    return (
      <div className="h-full min-h-0 min-w-0 flex items-center justify-center bg-white border border-slate-200 rounded-xl">
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
      <div className="h-full min-h-0 min-w-0 flex items-center justify-center bg-white border border-slate-200 rounded-xl">
        <Spin tip="生成纸面计划…" />
      </div>
    );
  }

  if (!plan) {
    return (
      <div
        className="h-full min-h-0 min-w-0 flex items-center justify-center bg-white border border-slate-200 rounded-xl"
        data-testid="combo-plan-empty"
      >
        <Empty description="当日还没有纸面计划">
          <Button type="primary" loading={loading} onClick={() => void onGenerate(overrides)}>
            生成今日计划
          </Button>
        </Empty>
      </div>
    );
  }

  const persisted = Boolean(plan.plan_id);
  const confirmed = Boolean(plan.confirmed_at);
  // 行情/风险标记缺失的计划只能看，不能确认（后端 confirm_plan 同口径拦截）
  const degraded = plan.data_ok === false || plan.summary.data_ok === false;
  // 确认窗口以服务端 can_confirm 为准（容器时钟+交易日历单一口径）；
  // 旧后端缺字段时才退回 /dates 序位判断
  const confirmable = confirmed
    ? false
    : degraded
      ? false
      : plan.can_confirm != null
        ? plan.can_confirm
        : !isPlanStale(plan.data_trade_date, dates);
  const stale = persisted && !confirmed && !confirmable && !degraded;
  const rows = planDisplayRows(plan);
  const summary = plan.summary;
  const warnings = plan.warnings ?? [];

  const columns: ColumnsType<PlanDisplayRow> = [
    {
      title: '类别',
      dataIndex: 'category',
      width: 64,
      fixed: 'left',
      align: 'center',
      render: (v: PlanDisplayRow['category']) => (
        <span className={`rounded border px-1.5 py-0.5 text-[10px] font-bold ${CATEGORY_CLASS[v]}`}>
          {v}
        </span>
      ),
    },
    {
      title: '代码',
      dataIndex: 'symbol',
      width: 104,
      fixed: 'left',
      render: (v: string) => (
        <span className="font-mono text-[11px] font-bold text-slate-800">{v}</span>
      ),
    },
    {
      title: '名称',
      dataIndex: 'name',
      width: 104,
      ellipsis: true,
      render: (v: string | null | undefined) => (
        <span className="text-[11px] text-slate-700">{v || RESEARCH_SCORE_MISSING}</span>
      ),
    },
    {
      title: '行业',
      dataIndex: 'industry',
      width: 104,
      ellipsis: true,
      render: (v: string | null | undefined) => (
        <span className="text-[11px] text-slate-600">{v || RESEARCH_SCORE_MISSING}</span>
      ),
    },
    {
      title: '市值(亿)',
      dataIndex: 'market_cap_yi',
      width: 88,
      align: 'right',
      render: (v: number | null | undefined) => (
        <span className="font-mono text-[11px] text-slate-600">{capLabel(v)}</span>
      ),
    },
    {
      title: '综合分',
      dataIndex: 'research_score',
      width: 80,
      align: 'right',
      render: (v: number | null | undefined) => (
        <span className="font-mono text-[11px] font-bold text-slate-700">
          {formatResearchScore(v ?? null)}
        </span>
      ),
    },
    {
      title: '20日均分',
      dataIndex: 'mean20',
      width: 88,
      align: 'right',
      render: (v: number | null) => (
        <span className="font-mono text-[11px] text-slate-600">{score3(v)}</span>
      ),
    },
    {
      title: '退出线',
      dataIndex: 'exit_line',
      width: 88,
      align: 'right',
      render: (v: number | null) => (
        <span className="font-mono text-[11px] text-slate-600">{score3(v)}</span>
      ),
    },
    {
      title: (
        <Tooltip title="ADV20：近 20 个交易日日均成交额（亿元）；缺失不新进">
          <span className="cursor-help">ADV20(亿)</span>
        </Tooltip>
      ),
      dataIndex: 'adv_yi',
      width: 96,
      align: 'right',
      render: (v: number | null | undefined) => (
        <span className="font-mono text-[11px] text-slate-600">{fixed2(v)}</span>
      ),
    },
    {
      title: '目标金额',
      dataIndex: 'target_value',
      width: 104,
      align: 'right',
      render: (v: number | undefined) => (
        <span className="font-mono text-[11px] text-slate-700">{formatAmount(v)}</span>
      ),
    },
    {
      title: '参考价',
      dataIndex: 'ref_price',
      width: 80,
      align: 'right',
      render: (v: number | undefined) => (
        <span className="font-mono text-[11px] text-slate-600">{fixed2(v)}</span>
      ),
    },
    {
      title: '股数',
      dataIndex: 'shares',
      width: 88,
      align: 'right',
      render: (v: number | undefined) => (
        <span className="font-mono text-[11px] text-slate-600">{integerLabel(v)}</span>
      ),
    },
    {
      title: '持有天数',
      dataIndex: 'held_days',
      width: 80,
      align: 'center',
      render: (v: number | undefined) => (
        <span className="font-mono text-[11px] text-slate-600">
          {typeof v === 'number' ? v : RESEARCH_SCORE_MISSING}
        </span>
      ),
    },
    {
      title: '备注',
      dataIndex: 'note',
      width: 240,
      render: (v: string | undefined, row) => {
        const text = [row.exit_reason, v].filter(Boolean).join(' · ');
        return (
          <Tooltip title={text || undefined}>
            <span className="text-[10px] text-slate-500">{text || RESEARCH_SCORE_MISSING}</span>
          </Tooltip>
        );
      },
    },
  ];

  const generateButton = (
    <Button
      size="small"
      type="primary"
      loading={loading}
      onClick={() => void onGenerate(overrides)}
      data-testid="combo-plan-generate"
    >
      {persisted ? '重新生成' : '生成今日计划'}
    </Button>
  );

  return (
    <div
      className="h-full min-h-0 min-w-0 flex flex-col bg-white border border-slate-200 rounded-xl overflow-hidden"
      data-testid="combo-plan-root"
    >
      {/* 参数区：资金 / TopK / 持有 / 每日新进 / 最低成交额 / 剔除 ST */}
      <div className="shrink-0 px-3 pt-2 pb-1.5 border-b border-slate-100 space-y-1.5">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-[11px] font-black text-slate-700">纸面调仓计划</span>
          <span className="text-[10px] text-slate-400">参数只影响本计划，不改组合配置</span>
          <span className="ml-auto flex items-center gap-2">
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
              disabled={!persisted || confirmed || stale || degraded}
              onConfirm={() => void onConfirm()}
            >
              <Button
                size="small"
                type="primary"
                ghost
                disabled={!persisted || confirmed || stale || degraded || loading}
                data-testid="combo-plan-confirm"
              >
                确认纸面调仓
              </Button>
            </Popconfirm>
          </span>
        </div>

        <div className="flex items-center gap-x-3 gap-y-1.5 flex-wrap text-[11px] text-slate-600">
          <span className="flex items-center gap-1">
            <span className="font-bold">资金</span>
            <Select
              size="small"
              className="!w-24"
              value={capitalWan}
              options={CAPITAL_OPTIONS}
              onChange={setCapitalWan}
            />
          </span>
          <span className="flex items-center gap-1">
            <span className="font-bold">TopK</span>
            <InputNumber
              size="small"
              className="!w-20"
              min={1}
              max={2000}
              value={topK}
              onChange={(v) => setTopK(Number(v ?? 50))}
            />
          </span>
          <span className="flex items-center gap-1">
            <span className="font-bold">持有</span>
            <InputNumber
              size="small"
              className="!w-16"
              min={1}
              max={60}
              value={holdDays}
              onChange={(v) => setHoldDays(Number(v ?? 10))}
            />
            <span className="text-slate-400">交易日</span>
          </span>
          <span className="flex items-center gap-1">
            <span className="font-bold">每日新进</span>
            <InputNumber
              size="small"
              className="!w-16"
              min={1}
              max={50}
              value={entryPerDay}
              onChange={(v) => setEntryPerDay(Number(v ?? 5))}
            />
          </span>
          <span className="flex items-center gap-1">
            <Tooltip title="ADV20 低于该值不新进；0 表示不设门槛">
              <span className="font-bold cursor-help">最低成交额</span>
            </Tooltip>
            <InputNumber
              size="small"
              className="!w-20"
              min={0}
              max={1000}
              step={0.5}
              value={minAdvYi}
              onChange={(v) => setMinAdvYi(Number(v ?? 0))}
            />
            <span className="text-slate-400">亿</span>
          </span>
          <Checkbox
            checked={excludeSt}
            onChange={(e) => setExcludeSt(e.target.checked)}
            className="text-[11px]"
          >
            剔除 ST / 退市
          </Checkbox>
          {generateButton}
          <Button size="small" icon={<RefreshCw size={13} />} onClick={onRetry} loading={loading}>
            刷新
          </Button>
        </div>
      </div>

      {/* 概览：名单变化 + 资金占用 */}
      <div className="shrink-0 px-3 py-1.5 border-b border-slate-100 flex items-center gap-1.5 flex-wrap">
        <SummaryTile label="新进" value={summary.n_entries} tone="blue" />
        <SummaryTile label="留存" value={summary.n_holds} />
        <SummaryTile label="退出" value={summary.n_exits} tone="amber" />
        <SummaryTile label="计划后持仓" value={summary.n_open_after} tone="indigo" />
        <SummaryTile
          label="资金占用"
          value={formatAmount(summary.capital_used ?? undefined)}
          hint={
            typeof summary.capital_available === 'number'
              ? `可用 ${formatAmount(summary.capital_available)}`
              : undefined
          }
          tone="indigo"
        />
        <SummaryTile
          label="剩余资金"
          value={formatAmount(summary.capital_remaining ?? undefined)}
          hint={`现金缓冲 ${(summary.cash_buffer * 100).toFixed(0)}%`}
          tone={summary.capital_remaining === 0 ? 'amber' : 'slate'}
        />
        <span className="ml-auto flex items-center gap-1 text-[11px] text-slate-500">
          <CalendarClock size={13} className="text-slate-400" />
          数据日 <span className="font-mono text-slate-700">{plan.data_trade_date}</span>
          <span className="text-slate-300">→</span>
          生效日 <span className="font-mono text-slate-700">{plan.target_date}</span>
          <span className="text-slate-400">
            门槛 {typeof summary.min_adv_yi === 'number' ? summary.min_adv_yi : minAdvYi} 亿
          </span>
          {stale && (
            <span className="text-amber-700 font-bold">计划已过期，需在最近 2 个交易日内确认</span>
          )}
        </span>
      </div>

      {error && (
        <div className="shrink-0 px-3 pt-2" data-testid="combo-plan-inline-error">
          <Alert type="warning" showIcon message={error} />
        </div>
      )}
      {degraded && (
        <div className="shrink-0 px-3 pt-2" data-testid="combo-plan-data-degraded">
          <Alert
            type="error"
            showIcon
            message="行情或 ST/退市标记缺失：本次计划仅供预览，不能确认"
          />
        </div>
      )}
      {warnings.length > 0 && (
        <div className="shrink-0 px-3 pt-2" data-testid="combo-plan-warnings">
          <div className="rounded border border-amber-200 bg-amber-50 px-2 py-1.5">
            <button
              type="button"
              className="flex w-full items-center gap-1 text-left text-[11px] text-amber-800"
              onClick={() => setWarningsOpen((open) => !open)}
            >
              <span className="font-bold">计划提示 {warnings.length} 条</span>
              <span className="min-w-0 truncate opacity-80">
                {warningsOpen ? '' : warnings.slice(0, 2).join('；')}
              </span>
              <ChevronDown
                size={13}
                className={`ml-auto shrink-0 transition-transform ${warningsOpen ? 'rotate-180' : ''}`}
              />
            </button>
            {warningsOpen && (
              <ul className="mt-1 max-h-24 overflow-auto list-disc pl-4 text-[10px] text-amber-800/90">
                {warnings.map((warning, index) => (
                  <li key={`${index}-${warning}`}>{warning}</li>
                ))}
              </ul>
            )}
          </div>
        </div>
      )}

      <div className="flex-1 min-h-0 min-w-0" data-testid="combo-plan-table">
        <Table<PlanDisplayRow>
          rowKey={(row) => `${row.category}:${row.symbol}`}
          size="small"
          dataSource={rows}
          columns={columns}
          pagination={false}
          scroll={{ x: 1500, y: 'calc(100vh - 430px)' }}
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
