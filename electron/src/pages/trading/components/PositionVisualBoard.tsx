/**
 * 持仓监控主卡（2026-10-23 明细优先重设计）。
 *
 * 布局：KPI 一行 → 工具行 → 明细表（吃满剩余高度）→ 分析抽屉（默认折叠 40px）。
 * 旧版两张图表常驻 200px + 卡片固定 560px，78 只持仓首屏只能看 5–7 行；
 * 现在明细用 react-window 虚拟滚动 + 粘性表头，图表按需展开。
 *
 * 口径不在渲染层重算：合计/排序/过滤全部走 `position-monitor/positionTableModel`
 * 纯函数（缺价行不参与加减，2026-09-22 事故口径）。
 */
import React, { useEffect, useMemo, useRef, useState } from 'react';
import { message } from 'antd';
import type { NormalizedHolding, PositionSummary } from '../utils/positionMetrics';
import type { QuoteFreshness } from '../utils/quoteFreshness';
import { toSuffixCode } from '../../../utils/portfolioUtils';
import { PushConfirmPanel } from '../../../features/stock-terminal/components/PushConfirmPanel';
import type { PushChannel, PushExecute } from '../../../features/stock-terminal-shared/types';
import { PositionKpiBar } from './position-monitor/PositionKpiBar';
import { PositionToolbar, type RowDensity } from './position-monitor/PositionToolbar';
import { PositionTable } from './position-monitor/PositionTable';
import { PositionInsightsDrawer } from './position-monitor/PositionInsightsDrawer';
import {
  DEFAULT_SORT,
  computePnlTotals,
  filterHoldings,
  nextSort,
  sortHoldings,
  type SortKey,
  type SortState,
} from './position-monitor/positionTableModel';

export { ExecutionStrip } from './position-monitor/ExecutionStrip';

const DENSITY_KEY = 'qm.position.density';

const readDensity = (): RowDensity => {
  try {
    // 默认紧凑：78 只持仓时首屏多两行；偏好里显式选过「舒适」才用 44px。
    return window.localStorage.getItem(DENSITY_KEY) === 'comfortable'
      ? 'comfortable'
      : 'compact';
  } catch {
    return 'compact';
  }
};

export interface PositionVisualBoardProps {
  holdings: NormalizedHolding[];
  summary: PositionSummary;
  /** 卖出预检的默认通道（跟随页面顶栏 模拟/实盘） */
  defaultChannels?: PushChannel[];
  /**
   * 每只持仓的行情新鲜度（T6-1 审计 H7a；前缀式代码为键）。缺省（无实时管道时）
   * 整段徽标不渲染，公开形态与本机制引入前一致。
   */
  quoteFreshness?: Record<string, QuoteFreshness>;
  /**
   * 深链带过来的持仓代码（前缀式，如 `SH601000`）：来自哨兵「去持仓」的 `symbol=`
   * 参数。命中行高亮 + 自动滚动进视野；不命中（已卖出/代码不匹配）静默无效果。
   */
  highlightSymbol?: string | null;
  /** 右栏「风险与情报」是否折叠（由 PositionMonitor 持有；缺省不渲染折叠按钮） */
  railCollapsed?: boolean;
  onToggleRail?: () => void;
}

export const PositionVisualBoard: React.FC<PositionVisualBoardProps> = ({
  holdings,
  summary,
  defaultChannels = ['sim'],
  quoteFreshness,
  highlightSymbol,
  railCollapsed,
  onToggleRail,
}) => {
  const [sort, setSort] = useState<SortState>(DEFAULT_SORT);
  const [q, setQ] = useState('');
  const [density, setDensity] = useState<RowDensity>(readDensity);

  // 深链高亮：目标行滚动进视野（虚拟列表按 index scrollToItem）+ 一段时间的醒目描边
  const highlightedRef = useRef<string | null>(null);
  const [highlighted, setHighlighted] = useState<string | null>(null);
  useEffect(() => {
    const target = (highlightSymbol || '').trim().toUpperCase();
    if (!target) return;
    if (highlightedRef.current === target) return; // 同标的已高亮过，不重复触发
    if (!holdings.some((h) => h.code.toUpperCase() === target)) return;
    highlightedRef.current = target;
    setHighlighted(target);
    const timer = window.setTimeout(() => setHighlighted(null), 6000);
    return () => window.clearTimeout(timer);
  }, [highlightSymbol, holdings]);

  // 卖出入口：勾选集合 + 当前要卖的标的（null = 面板关闭）
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [sellSymbols, setSellSymbols] = useState<string[] | null>(null);
  const [channels, setChannels] = useState<PushChannel[]>(defaultChannels);

  const totals = useMemo(() => computePnlTotals(holdings), [holdings]);

  // 断流可见性：KPI 行单列一格「行情陈旧 N 只」——逐行徽标会被滚动埋掉。
  const staleCount = useMemo(
    () =>
      holdings.filter(
        (h) => !h.priceMissing && quoteFreshness?.[h.code]?.kind === 'stale',
      ).length,
    [holdings, quoteFreshness],
  );

  const rows = useMemo(
    () => sortHoldings(filterHoldings(holdings, q), sort),
    [holdings, q, sort],
  );

  const base = summary.positionValue > 0 ? summary.positionValue : 1;

  const scrollToIndex = useMemo(() => {
    if (!highlighted) return null;
    const index = rows.findIndex(
      (h) => h.code.toUpperCase() === highlighted.toUpperCase(),
    );
    return index >= 0 ? index : null;
  }, [highlighted, rows]);

  // 勾选集以**当前持仓**为准重算：卖出后市集刷新，勾选里会留下已不在持仓的代码，
  // 直接拿 `[...picked]` 去预检会为一只已经卖掉的票再发一张卖单。
  const pickedRows = rows.filter((h) => picked.has(h.code));
  const allPicked = rows.length > 0 && pickedRows.length === rows.length;

  // —— 卖出入口 ——
  // 持仓行的 code 是**前缀式**（SH600036），预检接口要**后缀式**（600036.SH）：
  // 直接透传会被当成非法代码逐笔拦下。
  // 本仓库 tsc 下函数式 setter（`setX(prev => …)`）一律 TS2345，只能传值更新。
  const togglePick = (code: string) => {
    const next = new Set(picked);
    if (next.has(code)) next.delete(code);
    else next.add(code);
    setPicked(next);
  };
  const togglePickAll = () =>
    setPicked(
      allPicked
        ? new Set<string>()
        : new Set(rows.map((h) => h.code)),
    );
  const openSell = (codes: string[]) => {
    const symbols = codes.map(toSuffixCode).filter(Boolean);
    if (symbols.length === 0) {
      message.warning('没有可提交的标的');
      return;
    }
    // 通道默认值每次**打开时**重取：`useState(defaultChannels)` 只在挂载那一次生效，
    // 而账户视图（模拟/实盘）可以在页面生命周期内随时切换 —— 切到实盘后再点卖出，
    // 面板若还停在「仅模拟盘」，实盘持仓会被逐笔拦成「未勾选实盘通道」。
    setChannels(defaultChannels);
    setSellSymbols(symbols);
  };
  const closeSell = () => setSellSymbols(null);
  const sellDone = (outcome: PushExecute) => {
    // 只有真的成交/部分成交才清勾选。被拦下时保留勾选，用户改完条件可以重推——
    // 无条件清空会让「被风控拦下」看起来像「已经卖掉了」。
    const succeeded = Number(outcome?.summary?.succeeded ?? 0);
    if (!outcome?.dry_run && succeeded > 0) setPicked(new Set());
    else message.warning('本次未提交成功，勾选保留——请查看预检面板里的逐笔原因');
  };

  const handleSort = (key: SortKey) => setSort(nextSort(sort, key));

  const toggleDensity = () => {
    const next: RowDensity = density === 'compact' ? 'comfortable' : 'compact';
    setDensity(next);
    try {
      window.localStorage.setItem(DENSITY_KEY, next);
    } catch {
      /* localStorage 不可用不影响展示 */
    }
  };

  return (
    <div className="flex-1 min-h-0 rounded-2xl border border-gray-200 bg-white shadow-sm flex flex-col overflow-hidden">
      <PositionKpiBar
        holdingsCount={holdings.length}
        summary={summary}
        totals={totals}
        staleCount={staleCount}
        railCollapsed={railCollapsed}
        onToggleRail={onToggleRail}
      />

      <div className="flex-1 min-h-0 flex flex-col gap-2 px-3 pt-1.5 pb-2">
        <PositionToolbar
          query={q}
          onQueryChange={setQ}
          pickedCount={pickedRows.length}
          totalCount={rows.length}
          allPicked={allPicked}
          onToggleAll={togglePickAll}
          onSellPicked={() => openSell(pickedRows.map((h) => h.code))}
          sort={sort}
          onSortChange={setSort}
          density={density}
          onToggleDensity={toggleDensity}
        />
        <PositionTable
          rows={rows}
          totalHoldings={holdings.length}
          base={base}
          summaryValue={summary.positionValue}
          totals={totals}
          picked={picked}
          onTogglePick={togglePick}
          onSell={(code) => openSell([code])}
          sort={sort}
          onSort={handleSort}
          quoteFreshness={quoteFreshness}
          highlighted={highlighted}
          scrollToIndex={scrollToIndex}
          density={density}
        />
        <PositionInsightsDrawer holdings={holdings} />
      </div>

      {/* 卖出预检确认面板：本板只负责「预填 + 打开」，数量/风控/新闻全部由服务端
          `_build_legs` 给，与真正下单同源。`sellSymbols` 非空即打开，关闭时置 null。 */}
      <PushConfirmPanel
        open={sellSymbols !== null}
        symbols={sellSymbols ?? []}
        side="sell"
        channels={channels}
        onChannelsChange={setChannels}
        onClose={closeSell}
        onDone={sellDone}
      />
    </div>
  );
};
