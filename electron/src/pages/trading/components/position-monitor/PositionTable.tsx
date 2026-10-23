/**
 * 持仓明细表：粘性表头 + react-window 虚拟滚动 + 固定合计行。
 *
 * 78 只持仓时首屏可扫 ~12 行（旧实现 5–7 行）：图表移出、KPI 压成一行、
 * 明细吃满剩余高度；行高有舒适/紧凑两档。
 *
 * 口径不在这里重算：缺价行（`priceMissing`）的现价/市值/盈亏一律 `—`，
 * 合计来自 `computePnlTotals`（缺价行不参与加减，2026-09-22 事故口径）。
 */
import React, { useEffect, useMemo, useRef } from 'react';
import { FixedSizeList as List, type ListChildComponentProps } from 'react-window';
import { ArrowDown, ArrowDownUp, ArrowUp } from 'lucide-react';
import { Checkbox } from 'antd';
import type { NormalizedHolding } from '../../utils/positionMetrics';
import { quoteAgeText, type QuoteFreshness } from '../../utils/quoteFreshness';
import {
  barTone,
  fmtAvailableShares,
  fmtMoney,
  fmtRowPct,
  pnlTone,
  positionRowTitle,
} from './positionFormat';
import type { PnlTotals, SortKey, SortState } from './positionTableModel';
import { useElementSize } from './useElementSize';
import type { RowDensity } from './PositionToolbar';

const GRID_COLS =
  'grid-cols-[1.1rem_minmax(0,1.5fr)_minmax(0,0.9fr)_minmax(0,1.1fr)_minmax(0,1.2fr)_minmax(0,1.05fr)_3.2rem]';

const ROW_HEIGHT: Record<RowDensity, number> = { comfortable: 44, compact: 36 };

interface RowData {
  rows: NormalizedHolding[];
  picked: Set<string>;
  onTogglePick: (code: string) => void;
  onSell: (code: string) => void;
  quoteFreshness?: Record<string, QuoteFreshness>;
  highlighted: string | null;
  base: number;
  density: RowDensity;
}

const freshTagOf = (
  h: NormalizedHolding,
  fresh: QuoteFreshness | undefined,
): React.ReactNode => {
  if (h.priceMissing || !fresh || fresh.kind === 'fresh') return null;
  if (fresh.kind === 'stale') {
    return (
      <span
        className="shrink-0 rounded border border-amber-200 bg-amber-50 px-1 py-px text-[9px] font-bold text-amber-700"
        title={`最后一条行情${fresh.ageSec !== null ? `在 ${quoteAgeText(fresh.ageSec)} 前` : ''}${fresh.source ? `（源：${fresh.source}）` : ''}——不是实时价，市值与盈亏按该价计`}
      >
        已陈旧{fresh.ageSec !== null ? ` ${quoteAgeText(fresh.ageSec)}` : ''}
      </span>
    );
  }
  return (
    <span
      className="shrink-0 rounded border border-slate-200 bg-slate-50 px-1 py-px text-[9px] font-bold text-slate-500"
      title="实时行情不可用，现价来自最近交易日收盘（QuantDB 日线兜底）——市值与盈亏按收盘价计"
    >
      日线兜底
    </span>
  );
};

const PositionRow = React.memo(function PositionRow({
  index,
  style,
  data,
}: ListChildComponentProps<RowData>) {
  const h = data.rows[index];
  const compact = data.density === 'compact';
  const base = data.base > 0 ? data.base : 1;
  const pct = Math.max(0, Math.min(1, (h.value || 0) / base));
  const isHighlighted = data.highlighted === h.code;
  const fresh = data.quoteFreshness?.[h.code];
  const freshTag = freshTagOf(h, fresh);
  const strongText = compact ? 'text-[11px]' : 'text-xs';

  return (
    <div style={style} className="px-2">
      <div
        data-row={h.code}
        data-testid="position-row"
        title={positionRowTitle(h)}
        className={`grid ${GRID_COLS} h-[calc(100%-4px)] items-center gap-3 rounded-xl border px-2 transition-colors hover:border-slate-200 hover:bg-slate-50/70 ${
          isHighlighted
            ? 'border-blue-400 bg-blue-50/60 shadow-[0_0_0_1px_#60a5fa]'
            : 'border-transparent'
        }`}
      >
        <Checkbox
          checked={data.picked.has(h.code)}
          onChange={() => data.onTogglePick(h.code)}
        />

        <div className="min-w-0">
          <div className={`truncate font-bold text-slate-800 ${strongText}`}>
            {h.name || h.code}
          </div>
          <div className="truncate font-mono text-[10px] text-slate-400">{h.code}</div>
        </div>

        <div className="min-w-0 text-right" data-testid="cell-shares">
          <div className={`truncate font-mono font-bold text-slate-800 ${strongText}`}>
            {h.shares.toLocaleString('zh-CN', { maximumFractionDigits: 0 })}
          </div>
          <div className="truncate font-mono text-[10px] text-slate-400">
            可用 {fmtAvailableShares(h)}
          </div>
        </div>

        {/* 缺价行：现价与盈亏一律出 `—`。市值占比同理——0% 会读成「这只票不值钱」，
            它其实只是「不知道」。成本价照常显示，缺的是价不是成本。 */}
        <div className="min-w-0 text-right" data-testid="cell-price">
          <div className="flex items-center justify-end gap-1">
            <span
              className={`truncate font-mono font-bold ${strongText} ${
                fresh?.kind === 'stale' ? 'text-slate-400' : 'text-slate-800'
              }`}
            >
              {h.priceMissing ? '—' : fmtMoney(h.current)}
            </span>
            {freshTag}
          </div>
          <div className="truncate font-mono text-[10px] text-slate-400">
            成本 {fmtMoney(h.cost)}
          </div>
        </div>

        <div className="min-w-0" data-testid="cell-market-value">
          <div className={`truncate text-right font-mono font-bold text-slate-700 ${strongText}`}>
            {h.priceMissing ? '—' : fmtMoney(h.value)}
          </div>
          <div className="mt-0.5 flex items-center gap-2">
            <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-slate-100">
              <div
                className={`h-full rounded-full transition-all ${barTone(h.profit)}`}
                style={{ width: `${(pct * 100).toFixed(1)}%` }}
              />
            </div>
            <span className="w-10 shrink-0 text-right font-mono text-[10px] font-bold text-slate-500">
              {h.priceMissing ? '—' : `${(pct * 100).toFixed(1)}%`}
            </span>
          </div>
        </div>

        <div className="min-w-0 text-right" data-testid="cell-profit">
          {h.priceMissing ? (
            <>
              <div className={`truncate font-mono font-bold text-slate-400 ${strongText}`}>
                —
              </div>
              <div className="truncate font-mono text-[10px] text-slate-400">缺现价</div>
            </>
          ) : (
            <>
              <div className={`truncate font-mono font-bold ${strongText} ${pnlTone(h.profit)}`}>
                {h.profit > 0 ? '+' : ''}
                {fmtMoney(h.profit)}
              </div>
              <div className={`truncate font-mono text-[10px] ${pnlTone(h.profit)}`}>
                {h.profitPercent > 0 ? '+' : ''}
                {fmtRowPct(h.profitPercent)}
              </div>
            </>
          )}
        </div>

        {/* 只有一个「卖出」而不是「卖出 / 清仓」两个：预检面板本来就把数量预填成
            全部可用持仓，想卖一半才需要手动改数。批量清仓走上方「全选 → 清仓选中」。 */}
        <button
          type="button"
          onClick={() => data.onSell(h.code)}
          title={`卖出 ${h.name || h.code}（打开预检确认面板，不会自动下单）`}
          className="justify-self-end rounded-lg bg-emerald-600 px-2 py-0.5 text-[10px] font-bold text-white transition-colors hover:bg-emerald-700"
        >
          卖出
        </button>
      </div>
    </div>
  );
});

const SortHeader: React.FC<{
  label: string;
  sortKey: SortKey;
  sort: SortState;
  onSort: (key: SortKey) => void;
}> = ({ label, sortKey, sort, onSort }) => {
  const active = sort.key === sortKey;
  return (
    <button
      type="button"
      onClick={() => onSort(sortKey)}
      className={`justify-self-end inline-flex items-center gap-0.5 hover:text-blue-600 ${
        active ? 'text-blue-600' : ''
      }`}
      title={`按${label}排序`}
    >
      {label}
      {active ? (
        sort.dir === 'desc' ? (
          <ArrowDown className="h-2.5 w-2.5" />
        ) : (
          <ArrowUp className="h-2.5 w-2.5" />
        )
      ) : (
        <ArrowDownUp className="h-2.5 w-2.5 opacity-40" />
      )}
    </button>
  );
};

export interface PositionTableProps {
  rows: NormalizedHolding[];
  totalHoldings: number;
  base: number;
  summaryValue: number;
  totals: PnlTotals;
  picked: Set<string>;
  onTogglePick: (code: string) => void;
  onSell: (code: string) => void;
  sort: SortState;
  onSort: (key: SortKey) => void;
  quoteFreshness?: Record<string, QuoteFreshness>;
  highlighted: string | null;
  /** 高亮行在当前排序结果中的下标；-1/undefined = 不滚动 */
  scrollToIndex?: number | null;
  density: RowDensity;
}

export const PositionTable: React.FC<PositionTableProps> = ({
  rows,
  totalHoldings,
  base,
  summaryValue,
  totals,
  picked,
  onTogglePick,
  onSell,
  sort,
  onSort,
  quoteFreshness,
  highlighted,
  scrollToIndex,
  density,
}) => {
  const listRef = useRef<List | null>(null);
  const { ref: sizeRef, size } = useElementSize<HTMLDivElement>({
    width: 960,
    height: 480,
  });

  useEffect(() => {
    if (scrollToIndex === null || scrollToIndex === undefined || scrollToIndex < 0) return;
    listRef.current?.scrollToItem(scrollToIndex, 'center');
  }, [scrollToIndex]);

  const itemData = useMemo<RowData>(
    () => ({
      rows,
      picked,
      onTogglePick,
      onSell,
      quoteFreshness,
      highlighted,
      base,
      density,
    }),
    [rows, picked, onTogglePick, onSell, quoteFreshness, highlighted, base, density],
  );

  const { winCount, loseCount, missingCount, totalPnl, pnlUnknown } = totals;
  const itemHeight = ROW_HEIGHT[density];

  return (
    <div className="flex-1 min-h-0 flex flex-col" data-testid="position-table">
      <div
        data-testid="position-table-header"
        className={`shrink-0 grid ${GRID_COLS} items-center gap-3 px-2 pb-1 text-[10px] font-bold text-slate-400`}
      >
        <span />
        <span>股票</span>
        <SortHeader label="持仓/可用" sortKey="shares" sort={sort} onSort={onSort} />
        <span className="text-right">成本/现价</span>
        <SortHeader label="市值/占比" sortKey="value" sort={sort} onSort={onSort} />
        <SortHeader label="盈亏/比例" sortKey="profit" sort={sort} onSort={onSort} />
        <span className="text-right">操作</span>
      </div>

      <div ref={sizeRef} className="flex-1 min-h-0">
        {rows.length > 0 ? (
          <List
            ref={listRef}
            height={size.height}
            width={size.width}
            itemCount={rows.length}
            itemSize={itemHeight}
            itemData={itemData}
            overscanCount={8}
            className="custom-scrollbar"
          >
            {PositionRow}
          </List>
        ) : (
          <div className="flex h-full items-center justify-center text-sm text-slate-400">
            {totalHoldings === 0 ? '暂无持仓数据' : '无匹配持仓'}
          </div>
        )}
      </div>

      <div
        data-testid="position-total-row"
        className="shrink-0 flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-slate-100 px-2 pt-1.5 text-[10px] font-bold text-slate-500"
      >
        <span>合计</span>
        <span>
          市值 <span className="font-mono text-slate-700">{fmtMoney(summaryValue)}</span>
        </span>
        <span>
          总盈亏{' '}
          <span
            className={`font-mono ${
              pnlUnknown ? 'text-slate-500' : totalPnl > 0 ? 'text-red-600' : totalPnl < 0 ? 'text-emerald-600' : 'text-slate-700'
            }`}
          >
            {pnlUnknown ? '—' : `${totalPnl > 0 ? '+' : ''}${fmtMoney(totalPnl)}`}
          </span>
        </span>
        <span>
          盈利 {winCount} / 亏损 {loseCount}
        </span>
        {missingCount > 0 && <span className="text-amber-600">缺价 {missingCount}</span>}
        <span className="ml-auto font-mono text-slate-400">
          {picked.size > 0 ? `已选 ${picked.size} 只` : `共 ${rows.length} 只`}
        </span>
      </div>
    </div>
  );
};
