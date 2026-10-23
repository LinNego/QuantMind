/**
 * 持仓分析抽屉：市值分布（货架马赛克）+ 盈亏贡献 Top12。
 *
 * 旧版两张图常驻 200px，把明细挤到只剩 5–7 行；现在默认折叠为 40px 摘要条
 * （Top1 占比 / 最大盈亏贡献），点开才展开图表，明细高度优先。
 * 展开状态记 localStorage，跨会话保持。
 */
import React, { useMemo, useState } from 'react';
import ReactECharts from 'echarts-for-react';
import { BarChart3, ChevronDown, ChevronUp } from 'lucide-react';
import type { NormalizedHolding } from '../../utils/positionMetrics';
import { fmtMoney, fmtRowPct, tileColor } from './positionFormat';
import { buildInsightSummary, buildShelves } from './positionTableModel';

const STORAGE_KEY = 'qm.position.insights.open';

const readOpen = (): boolean => {
  try {
    return window.localStorage.getItem(STORAGE_KEY) === '1';
  } catch {
    return false;
  }
};

export interface PositionInsightsDrawerProps {
  holdings: NormalizedHolding[];
}

export const PositionInsightsDrawer: React.FC<PositionInsightsDrawerProps> = ({
  holdings,
}) => {
  const [open, setOpen] = useState<boolean>(readOpen);

  const shelves = useMemo(() => buildShelves(holdings), [holdings]);
  const summary = useMemo(() => buildInsightSummary(holdings), [holdings]);

  const contribOption = useMemo(() => {
    const top = [...holdings]
      .filter((h) => h.profit !== 0)
      .sort((a, b) => Math.abs(b.profit) - Math.abs(a.profit))
      .slice(0, 12)
      .sort((a, b) => a.profit - b.profit); // 从小到大 → 柱图自下而上
    return {
      grid: { left: 66, right: 18, top: 4, bottom: 4, containLabel: false },
      xAxis: {
        type: 'value',
        axisLabel: {
          fontSize: 9,
          color: '#94a3b8',
          formatter: (v: number) =>
            Math.abs(v) >= 10000 ? `${(v / 10000).toFixed(0)}万` : String(v),
        },
        splitLine: { lineStyle: { color: '#f1f5f9' } },
      },
      yAxis: {
        type: 'category',
        data: top.map((h) => (h.name || h.code).slice(0, 6)),
        axisLabel: { fontSize: 9, color: '#64748b' },
        axisTick: { show: false },
        axisLine: { show: false },
      },
      tooltip: {
        formatter: (p: { dataIndex: number }) => {
          const h = top[p.dataIndex];
          return `${h.name}（${h.code}）<br/>盈亏 ${h.profit > 0 ? '+' : ''}${fmtMoney(h.profit)}（${fmtRowPct(h.profitPercent)}）`;
        },
      },
      series: [
        {
          type: 'bar',
          barWidth: 9,
          data: top.map((h) => ({
            value: Math.round(h.profit),
            itemStyle: {
              color: h.profit > 0 ? '#dc2626' : '#059669',
              borderRadius: 3,
            },
          })),
        },
      ],
    };
  }, [holdings]);

  const mosaicTile = (h: NormalizedHolding, key: string) => {
    const share = Math.max(h.value, 0) / shelves.total;
    return (
      <div
        key={key}
        title={
          h.priceMissing
            ? `${h.name}（${h.code}）\n持仓 ${h.shares} 股 · 成本 ${fmtMoney(h.cost)}\n现价缺失，市值与盈亏暂不可知`
            : `${h.name}（${h.code}）\n市值 ${fmtMoney(h.value)} · 占比 ${(share * 100).toFixed(1)}%\n盈亏 ${h.profit > 0 ? '+' : ''}${fmtMoney(h.profit)}（${fmtRowPct(h.profitPercent)}）`
        }
        style={{
          flexGrow: Math.max(share, 0.004),
          flexBasis: 0,
          backgroundColor: tileColor(h.profit, h.profitPercent),
        }}
        className="min-w-[30px] h-full rounded-md px-1.5 py-1 overflow-hidden cursor-default transition-transform hover:scale-[1.015]"
      >
        <div className="truncate text-[10px] font-bold text-white/95 leading-3.5">
          {(h.name || h.code).slice(0, 5)}
        </div>
        <div className="truncate font-mono text-[9px] text-white/80 leading-3">
          {h.priceMissing ? '—' : `${(share * 100).toFixed(1)}%`}
        </div>
      </div>
    );
  };

  const toggle = () => {
    const next = !open;
    setOpen(next);
    try {
      window.localStorage.setItem(STORAGE_KEY, next ? '1' : '0');
    } catch {
      /* localStorage 不可用不影响展示 */
    }
  };

  return (
    <div
      data-testid="position-insights"
      data-open={open ? 'true' : 'false'}
      className="shrink-0 rounded-xl border border-slate-100 bg-slate-50/20"
    >
      <button
        type="button"
        onClick={toggle}
        aria-expanded={open}
        className="flex h-9 w-full items-center gap-2 px-3 text-left transition-colors hover:bg-slate-50/70"
      >
        <BarChart3 className="h-3.5 w-3.5 text-slate-400" />
        <span className="text-[10px] font-bold text-slate-500">分析</span>
        <span className="text-[10px] text-slate-400">市值分布 · 盈亏贡献</span>
        {!open && summary.topSharePct !== null && (
          <span className="rounded border border-slate-200 bg-white px-1.5 py-0.5 font-mono text-[9px] font-bold text-slate-500">
            Top1 {summary.topSharePct.toFixed(1)}%
          </span>
        )}
        {!open && summary.topContributor && (
          <span
            className={`truncate font-mono text-[9px] font-bold ${
              summary.topContributor.profit > 0 ? 'text-red-600' : 'text-emerald-600'
            }`}
            title={`${summary.topContributor.name}（${summary.topContributor.code}）盈亏 ${fmtMoney(summary.topContributor.profit)}`}
          >
            最大贡献 {summary.topContributor.profit > 0 ? '+' : ''}
            {fmtMoney(summary.topContributor.profit)}
          </span>
        )}
        <span className="ml-auto text-slate-400">
          {open ? <ChevronUp className="h-3.5 w-3.5" /> : <ChevronDown className="h-3.5 w-3.5" />}
        </span>
      </button>

      {open && (
        <div className="grid grid-cols-1 gap-2 border-t border-slate-100 p-2 lg:grid-cols-2">
          <div className="flex h-[190px] min-w-0 flex-col rounded-xl border border-slate-100 bg-white p-2">
            <div
              className="mb-1 text-[10px] font-bold text-slate-400"
              title="货架马赛克：块宽=持仓市值占比（两行按市值降序），颜色=盈亏（红涨绿跌，越深幅度越大）；小块归并「其余」"
            >
              市值分布
            </div>
            {holdings.length > 0 ? (
              <div className="flex flex-1 min-h-0 flex-col gap-1">
                <div className="flex flex-1 gap-1 min-h-0">
                  {shelves.row1.map((h) => mosaicTile(h, h.code))}
                </div>
                {(shelves.row2.length > 0 || shelves.tail.length > 0) && (
                  <div className="flex flex-1 gap-1 min-h-0">
                    {shelves.row2.map((h) => mosaicTile(h, h.code))}
                    {shelves.tail.length > 0 && (
                      <div
                        title={`其余 ${shelves.tail.length} 只（合计占比 ${((shelves.tailValue / shelves.total) * 100).toFixed(1)}%）\n${shelves.tail.map((h) => h.name || h.code).join('、')}`}
                        style={{
                          flexGrow: Math.max(shelves.tailValue / shelves.total, 0.06),
                          flexBasis: 0,
                        }}
                        className="min-w-[30px] h-full rounded-md bg-slate-200 px-1.5 py-1 overflow-hidden cursor-default"
                      >
                        <div className="truncate text-[10px] font-bold text-slate-600 leading-3.5">
                          其余 {shelves.tail.length} 只
                        </div>
                        <div className="truncate font-mono text-[9px] text-slate-500 leading-3">
                          {((shelves.tailValue / shelves.total) * 100).toFixed(1)}%
                        </div>
                      </div>
                    )}
                  </div>
                )}
              </div>
            ) : (
              <div className="flex flex-1 items-center justify-center text-[11px] text-slate-300">
                暂无持仓
              </div>
            )}
          </div>

          <div className="flex h-[190px] min-w-0 flex-col rounded-xl border border-slate-100 bg-white p-2">
            <div
              className="mb-1 text-[10px] font-bold text-slate-400"
              title="盈亏金额绝对值 Top12（红=盈利、绿=亏损）"
            >
              盈亏贡献 Top12
            </div>
            {holdings.some((h) => h.profit !== 0) ? (
              <div className="flex-1 min-h-0">
                <ReactECharts option={contribOption} style={{ height: '100%' }} notMerge />
              </div>
            ) : (
              <div className="flex flex-1 items-center justify-center text-[11px] text-slate-300">
                {holdings.some((h) => h.priceMissing) ? '持仓缺现价，盈亏不可知' : '暂无盈亏数据'}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
};
