/**
 * 组合历史统计的公共展示件：指标条 + 数据端口徽标。
 *
 * 两个面板（周期收益 / 滚动建仓）共用同一套排版与口径：
 * - 指标一律「标签 + 数值」，数值缺失由调用方传 `—`（本组件不造数）；
 * - 分组标题只在有指标时渲染，不渲染空组；
 * - 红涨绿跌由调用方给 `tone`，本组件不判断方向。
 */

import React from 'react';
import { Tag, Tooltip } from 'antd';
import type { BlendBenchmark, BlendBounds, BlendDataSource } from '../../../../services/blendTypes';
import {
  benchmarkLabel,
  boundsSpanLabel,
  memberReadinessLabel,
  sourceHint,
  sourceLabel,
} from './portfolioMetricsModel';

export interface MetricItem {
  key: string;
  label: string;
  value: React.ReactNode;
  /** 数值色调（如 A 股红涨绿跌 `text-red-600`） */
  tone?: string;
  /** hover 说明（口径、窗口、缺失原因） */
  hint?: string;
  /** hero = 主指标（大号），normal = 次级 */
  emphasis?: 'hero' | 'normal';
  testId?: string;
}

export interface MetricSection {
  key: string;
  /** 组标题（可选；有值时常驻左侧） */
  title?: string;
  items: MetricItem[];
}

export interface PortfolioMetricsStripProps {
  sections: MetricSection[];
  testId: string;
}

const HERO_CLASS = 'text-base font-black';
const NORMAL_CLASS = 'text-[12px] font-bold';

function metricNode(item: MetricItem): React.ReactNode {
  const value = (
    <span
      className={`font-mono whitespace-nowrap ${
        item.emphasis === 'hero' ? HERO_CLASS : NORMAL_CLASS
      } ${item.tone ?? 'text-slate-700'}`}
      data-testid={item.testId}
    >
      {item.value}
    </span>
  );
  const body = (
    <span className="inline-flex items-baseline gap-1">
      <span className="whitespace-nowrap text-[11px] text-slate-500">{item.label}</span>
      {value}
    </span>
  );
  return item.hint ? (
    <Tooltip key={item.key} title={item.hint}>
      <span className="cursor-help">{body}</span>
    </Tooltip>
  ) : (
    <span key={item.key}>{body}</span>
  );
}

export const PortfolioMetricsStrip: React.FC<PortfolioMetricsStripProps> = ({
  sections,
  testId,
}) => {
  const visible = sections.filter((section) => section.items.length > 0);
  if (visible.length === 0) return null;
  return (
    <div
      className="shrink-0 divide-y divide-slate-100 border-b border-slate-100 bg-slate-50/70"
      data-testid={testId}
    >
      {visible.map((section) => (
        <div
          key={section.key}
          className="flex flex-wrap items-baseline gap-x-4 gap-y-1 px-3 py-1.5"
          data-testid={`${testId}-${section.key}`}
        >
          {section.title && (
            <span className="w-10 shrink-0 text-[10px] font-black tracking-wide text-slate-400">
              {section.title}
            </span>
          )}
          {section.items.map((item) => metricNode(item))}
        </div>
      ))}
    </div>
  );
};

export interface DataSourceBadgeProps {
  source?: BlendDataSource | null;
  bounds?: BlendBounds | null;
  benchmark?: BlendBenchmark | null;
  /** 可选：本金说明（滚动建仓会随输入变化） */
  capital?: number | null;
  testId?: string;
}

/** 数据端口徽标：pred 测试集 / 推理台账 + 实际可用区间 + 基准可用性 */
export const DataSourceBadge: React.FC<DataSourceBadgeProps> = ({
  source,
  bounds,
  benchmark,
  capital,
  testId = 'combo-source-badge',
}) => {
  const span = boundsSpanLabel(bounds);
  const readiness = memberReadinessLabel(bounds);
  const bench = benchmarkLabel(benchmark);
  return (
    <span
      className="inline-flex flex-wrap items-center gap-1.5 text-[10px] text-slate-400"
      data-testid={testId}
    >
      <Tooltip title={sourceHint(source)}>
        <Tag
          color={source === 'pred' ? 'green' : 'orange'}
          className="!m-0 !text-[10px] !leading-tight"
          data-testid={`${testId}-tag`}
        >
          {sourceLabel(source)}
        </Tag>
      </Tooltip>
      {span && (
        <span className="font-mono" data-testid={`${testId}-span`}>
          {span}
        </span>
      )}
      {readiness && <span data-testid={`${testId}-readiness`}>{readiness}</span>}
      {bench && (
        <Tooltip title="基准覆盖率不足 95% 时，基准与超额一律显示 —（不写 0）">
          <span
            className={benchmark?.available ? 'text-slate-400' : 'text-amber-600'}
            data-testid={`${testId}-benchmark`}
          >
            {bench}
          </span>
        </Tooltip>
      )}
      {typeof capital === 'number' && Number.isFinite(capital) && capital > 0 && (
        <span className="font-mono" data-testid={`${testId}-capital`}>
          本金 {(capital / 10000).toFixed(0)} 万
        </span>
      )}
    </span>
  );
};

export default PortfolioMetricsStrip;
