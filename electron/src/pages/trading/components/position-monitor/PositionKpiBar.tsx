/**
 * 持仓监控 KPI 行：总市值 / 持仓数 / 盈利·亏损 / 缺价 / 行情陈旧 / 现金占比 / 浮动盈亏。
 *
 * 纯展示：合计由 `computePnlTotals` 给出（缺价行不参与加减，2026-09-22 事故口径）。
 * 右侧可挂「风险与情报栏」折叠按钮（由 PositionMonitor 注入，缺省不渲染）。
 */
import React from 'react';
import { PanelRightClose, PanelRightOpen, PieChart } from 'lucide-react';
import type { PositionSummary } from '../../utils/positionMetrics';
import { fmtMoney, fmtRowPct } from './positionFormat';
import type { PnlTotals } from './positionTableModel';

export interface PositionKpiBarProps {
  holdingsCount: number;
  summary: PositionSummary;
  totals: PnlTotals;
  staleCount: number;
  railCollapsed?: boolean;
  onToggleRail?: () => void;
}

export const PositionKpiBar: React.FC<PositionKpiBarProps> = ({
  holdingsCount,
  summary,
  totals,
  staleCount,
  railCollapsed = false,
  onToggleRail,
}) => {
  const { missingCount, winCount, loseCount, totalPnl, pnlUnknown } = totals;

  return (
    <div
      data-testid="position-kpi-bar"
      className="shrink-0 flex flex-wrap items-center gap-2 px-4 py-2.5 border-b border-gray-100"
    >
      <div className="flex items-center gap-2">
        <span className="w-7 h-7 rounded-lg bg-blue-50 text-blue-600 flex items-center justify-center">
          <PieChart className="w-4 h-4" />
        </span>
        <span className="text-sm font-bold text-slate-800">持仓监控</span>
      </div>
      <div className="ml-auto flex flex-wrap items-center gap-1.5 text-[11px] font-bold">
        <span className="rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1 text-slate-600">
          总市值{' '}
          <span className="font-mono text-slate-800">
            {fmtMoney(summary.positionValue)}
          </span>
        </span>
        <span className="rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1 text-slate-600">
          持仓 <span className="font-mono text-slate-800">{holdingsCount}</span> 只
        </span>
        <span className="rounded-full border border-red-100 bg-red-50/60 px-2.5 py-1 text-red-600">
          盈利 <span className="font-mono">{winCount}</span>
        </span>
        <span className="rounded-full border border-emerald-100 bg-emerald-50/60 px-2.5 py-1 text-emerald-600">
          亏损 <span className="font-mono">{loseCount}</span>
        </span>
        {/* 缺价单列一格：这些行的盈亏没被算进「盈利/亏损」，但也没消失——
            不单独报数的话，用户会以为持仓数对不上。 */}
        {missingCount > 0 && (
          <span
            className="rounded-full border border-amber-200 bg-amber-50 px-2.5 py-1 text-amber-700"
            title="这些持仓拿不到现价（补价失败 / 停牌 / 退市），市值与盈亏暂不可知，未计入盈亏合计"
          >
            缺价 <span className="font-mono">{missingCount}</span>
          </span>
        )}
        {/* 行情陈旧（T6-1）：交易时段内最后一条行情超 60s——显示价不是实时价 */}
        {staleCount > 0 && (
          <span
            className="rounded-full border border-amber-300 bg-amber-100/70 px-2.5 py-1 text-amber-800"
            title="这些持仓的最后一条行情超过 60 秒未更新（交易时段内判定）——当前显示的价不是实时价，市值与盈亏按该价计"
          >
            行情陈旧 <span className="font-mono">{staleCount}</span> 只
          </span>
        )}
        <span
          className="rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1 text-slate-600"
          title={
            summary.cashRatio > 150
              ? '现金/总资产 > 100%，账户口径数据异常，暂显 —'
              : undefined
          }
        >
          现金占比{' '}
          <span className="font-mono text-slate-800">
            {/* getPositionSummary 的 cashRatio 已是百分数（2.52 = 2.52%），
                不能再用 fmtPct（会 ×100 变成 252.41%）；异常阈值为 >150%。 */}
            {summary.cashRatio > 150 ? '—' : fmtRowPct(summary.cashRatio)}
          </span>
        </span>
        <span
          className={`rounded-full border px-2.5 py-1 ${
            pnlUnknown
              ? 'border-slate-200 bg-slate-50 text-slate-500'
              : totalPnl > 0
                ? 'border-red-200 bg-red-50 text-red-600'
                : totalPnl < 0
                  ? 'border-emerald-200 bg-emerald-50 text-emerald-600'
                  : 'border-slate-200 bg-slate-50 text-slate-500'
          }`}
          title={
            pnlUnknown
              ? '全部持仓都拿不到现价，盈亏不可知'
              : missingCount > 0
                ? `${missingCount} 只缺价未计入`
                : undefined
          }
        >
          浮动盈亏{' '}
          <span className="font-mono">
            {pnlUnknown ? '—' : `${totalPnl > 0 ? '+' : ''}${fmtMoney(totalPnl)}`}
          </span>
        </span>
        {onToggleRail && (
          <button
            type="button"
            onClick={onToggleRail}
            aria-pressed={railCollapsed}
            aria-label={railCollapsed ? '展开风险与情报栏' : '折叠风险与情报栏'}
            title={railCollapsed ? '展开风险与情报栏' : '折叠风险与情报栏'}
            className="ml-1 flex h-6 w-6 items-center justify-center rounded-lg border border-slate-200 bg-white text-slate-500 transition-colors hover:border-blue-300 hover:text-blue-600"
          >
            {railCollapsed ? (
              <PanelRightOpen className="h-3.5 w-3.5" />
            ) : (
              <PanelRightClose className="h-3.5 w-3.5" />
            )}
          </button>
        )}
      </div>
    </div>
  );
};
