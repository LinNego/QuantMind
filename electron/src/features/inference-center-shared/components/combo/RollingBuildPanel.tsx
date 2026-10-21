/**
 * 组合选股 · 滚动建仓视图。
 *
 * 横向 N+1 列（D-N … 今日），每列 `per_day` 只，默认 11×5 = 55 格（窗口内去重约 50）。
 * 色块只表达「新进 / 留存 / 退出」，**刻意不用红绿**——红绿在 A 股是涨跌方向，
 * 与「是否新进」无关，混用会把方向暗示从颜色里加回来。
 *
 * 退出只在「昨列 → 今日」标注一次（后端已限定），这里不做二次推算。
 */

import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Alert, Button, Empty, Spin, Tag, Tooltip } from 'antd';
import { RefreshCw } from 'lucide-react';
import type { BlendRollingResponse } from '../../../../services/blendTypes';
import { formatResearchScore, researchScore } from '../../../shared/researchScore';
import { positionTagClass } from '../../positionLabels';
import {
  ROLLING_TONE,
  exitLabel,
  exitTradeLabel,
  formatReturnPct,
  positionLabel,
  returnToneClass,
  rollingHeldLabel,
  rollingStatus,
  scrollGeometry,
  type ScrollGeometry,
} from './comboRankingModel';
import './rollingBoard.css';

const HIDDEN_SCROLLBAR: ScrollGeometry = {
  visible: false,
  thumbWidth: 0,
  thumbLeft: 0,
  progress: 0,
};

/** 市值（亿元）→ 展示值；缺失返回 null（渲染 `—`，不写 0） */
function capLabel(marketCapYi: number | null | undefined): string | null {
  if (typeof marketCapYi !== 'number' || !Number.isFinite(marketCapYi)) return null;
  if (marketCapYi >= 10000) return `${(marketCapYi / 10000).toFixed(2)} 万亿`;
  return `${marketCapYi.toFixed(0)} 亿`;
}

export interface RollingBuildPanelProps {
  rolling: BlendRollingResponse | null;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
}

export const RollingBuildPanel: React.FC<RollingBuildPanelProps> = ({
  rolling,
  loading,
  error,
  onRetry,
}) => {
  // 常驻横向滚动条（覆盖式原生滚动条不滚动时不可见，见滚动条组件注释）
  const boardRef = useRef<HTMLDivElement | null>(null);
  const trackRef = useRef<HTMLDivElement | null>(null);
  const [overflowX, setOverflowX] = useState(false);
  const [scrollbar, setScrollbar] = useState<ScrollGeometry>(HIDDEN_SCROLLBAR);

  const syncScrollbar = useCallback(() => {
    const el = boardRef.current;
    if (!el) return;
    const hasOverflow = el.scrollWidth > el.clientWidth + 1;
    setOverflowX(hasOverflow);
    const next = hasOverflow
      ? scrollGeometry({
          clientWidth: el.clientWidth,
          scrollWidth: el.scrollWidth,
          scrollLeft: el.scrollLeft,
          trackWidth: trackRef.current?.clientWidth || el.clientWidth,
        })
      : HIDDEN_SCROLLBAR;
    // 纵向滚动也会触发 onScroll；几何没变就不重渲染整块看板
    setScrollbar((prev) =>
      prev.visible === next.visible &&
      Math.abs(prev.thumbWidth - next.thumbWidth) < 0.5 &&
      Math.abs(prev.thumbLeft - next.thumbLeft) < 0.5 &&
      Math.abs(prev.progress - next.progress) < 0.001
        ? prev
        : next,
    );
  }, []);

  useEffect(() => {
    const el = boardRef.current;
    if (!el) return;
    const frame = requestAnimationFrame(syncScrollbar);
    const observer =
      typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(syncScrollbar);
    observer?.observe(el);
    return () => {
      cancelAnimationFrame(frame);
      observer?.disconnect();
    };
  }, [syncScrollbar, rolling]);

  // 轨道在 overflowX 变 true 后才挂载，挂载后重新量一次宽度（首帧回退用看板宽度）
  useEffect(() => {
    if (!overflowX) return;
    const frame = requestAnimationFrame(syncScrollbar);
    return () => cancelAnimationFrame(frame);
  }, [overflowX, syncScrollbar]);

  const onTrackPointerDown = (event: React.PointerEvent<HTMLDivElement>) => {
    const el = boardRef.current;
    const track = trackRef.current;
    if (!el || !track) return;
    const maxScroll = el.scrollWidth - el.clientWidth;
    if (maxScroll <= 0) return;
    const rect = track.getBoundingClientRect();
    const travel = Math.max(1, rect.width - scrollbar.thumbWidth);
    // 点轨道空白处 = 把滑块中心挪到点击处；点滑块本身 = 直接拖
    if ((event.target as HTMLElement).dataset.role !== 'thumb') {
      const target = Math.min(
        1,
        Math.max(0, (event.clientX - rect.left - scrollbar.thumbWidth / 2) / travel),
      );
      el.scrollLeft = target * maxScroll;
      syncScrollbar();
    }
    const startX = event.clientX;
    const startScroll = el.scrollLeft;
    const move = (moveEvent: PointerEvent) => {
      const delta = moveEvent.clientX - startX;
      el.scrollLeft = Math.min(
        maxScroll,
        Math.max(0, startScroll + (delta / travel) * maxScroll),
      );
      syncScrollbar();
    };
    const stop = () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', stop);
      window.removeEventListener('pointercancel', stop);
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', stop);
    window.addEventListener('pointercancel', stop);
    event.preventDefault();
  };

  if (error) {
    return (
      <div className="p-3" data-testid="combo-rolling-error">
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

  if (loading && !rolling) {
    return (
      <div className="h-full min-h-0 min-w-0 flex items-center justify-center bg-white border border-slate-200 rounded-xl">
        <Spin tip="读取滚动窗口…" />
      </div>
    );
  }

  if (!rolling || rolling.columns.length === 0) {
    return (
      <div className="h-full min-h-0 min-w-0 flex items-center justify-center bg-white border border-slate-200 rounded-xl">
        <Empty description="该组合暂无滚动窗口数据" />
      </div>
    );
  }

  const memberCount = rolling.members.length;
  const latest = rolling.data_trade_date;
  const performance = rolling.summary.performance;

  return (
    <div
      className="h-full min-h-0 min-w-0 flex flex-col bg-white border border-slate-200 rounded-xl overflow-hidden"
      data-testid="combo-rolling-root"
    >
      <div className="shrink-0 flex items-center gap-3 px-3 py-2 border-b border-slate-100">
        <div className="flex items-center gap-2 text-[11px] text-slate-500">
          <span>
            窗口 <strong className="font-mono text-slate-800">{rolling.columns.length}</strong> 日
          </span>
          <span>
            每日 <strong className="font-mono text-slate-800">{rolling.per_day}</strong> 只
          </span>
          <span>
            去重 <strong className="font-mono text-slate-800">{rolling.summary.unique_symbols}</strong> 只
          </span>
          <span className="text-blue-700">
            今日新进 <strong className="font-mono">{rolling.summary.new_today}</strong>
          </span>
          <span className="text-amber-700">
            今日退出 <strong className="font-mono">{rolling.summary.exited_today}</strong>
          </span>
          <span className="text-slate-400">生效日 {rolling.target_date}</span>
        </div>
        <Tooltip title="新进=蓝、留存=白、退出=琥珀；色块只表达名单变化。退出收益单独用红涨绿跌">
          <span className="ml-auto text-[10px] text-slate-400 cursor-help">色块图例</span>
        </Tooltip>
        <Button size="small" icon={<RefreshCw size={13} />} loading={loading} onClick={onRetry}>
          刷新
        </Button>
      </div>

      {performance && (
        <div
          className="shrink-0 flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-1.5 border-b border-slate-100 bg-slate-50/70 text-[11px] text-slate-500"
          data-testid="combo-rolling-performance"
        >
          <span className="font-bold text-slate-600">窗口涨跌</span>
          <span
            className={`font-mono text-sm font-black ${returnToneClass(performance.cycle_return_pct)}`}
            data-testid="combo-rolling-cycle-return"
          >
            {formatReturnPct(performance.cycle_return_pct)}
          </span>
          <span className="text-slate-400">
            每日等权换仓 · {performance.cycle_priced_days}/{performance.cycle_days} 日有价
          </span>
          <span className="w-px h-3 bg-slate-200" />
          <span>
            已了结{' '}
            <strong className="font-mono text-slate-700">{performance.closed_trades}</strong> 笔
          </span>
          <span>
            盈 <strong className="font-mono text-red-600">{performance.wins}</strong> / 亏{' '}
            <strong className="font-mono text-green-600">{performance.losses}</strong>
            {performance.flats > 0 && (
              <>
                {' '}
                / 平 <strong className="font-mono text-slate-500">{performance.flats}</strong>
              </>
            )}
          </span>
          <span>
            胜率{' '}
            <strong className="font-mono text-slate-700">
              {performance.win_rate_pct === null ? '—' : `${performance.win_rate_pct.toFixed(1)}%`}
            </strong>
          </span>
          <span>
            单笔均值{' '}
            <strong className={`font-mono ${returnToneClass(performance.avg_return_pct)}`}>
              {formatReturnPct(performance.avg_return_pct)}
            </strong>
          </span>
          <span>
            在场{' '}
            <strong className="font-mono text-slate-700">{performance.open_positions}</strong> 只 ·
            浮动{' '}
            <strong className={`font-mono ${returnToneClass(performance.open_avg_return_pct)}`}>
              {formatReturnPct(performance.open_avg_return_pct)}
            </strong>
          </span>
          {performance.unpriced_trades > 0 && (
            <span className="text-slate-400">缺行情 {performance.unpriced_trades} 笔（不计入）</span>
          )}
          <Tooltip title="损益按收盘价计：入选日收盘价 → 退出日收盘价；窗口涨跌=每日等权持有当日 TopK、次日收盘换仓的复利收益。均未计费用，缺失行情显示 —。">
            <span className="ml-auto text-[10px] text-slate-400 cursor-help">口径</span>
          </Tooltip>
        </div>
      )}

      {rolling.warnings.length > 0 && (
        <div className="shrink-0 px-3 pt-2" data-testid="combo-rolling-warnings">
          <Alert
            type="warning"
            showIcon
            message={rolling.warnings.slice(0, 3).join('；')}
          />
        </div>
      )}

      <div
        ref={boardRef}
        onScroll={syncScrollbar}
        className="rolling-board-scroll flex-1 min-h-0 overflow-auto overscroll-contain p-2"
        data-testid="combo-rolling-board"
      >
        <div className="flex gap-1.5 min-w-max items-stretch">
          {rolling.columns.map((column, index) => {
            const isLatest = column.date === latest;
            const exits = exitLabel(column);
            return (
              <div
                key={column.date}
                className="w-[160px] shrink-0 flex flex-col rounded-lg border border-slate-200 bg-slate-50/60"
                data-testid={`combo-rolling-col-${column.date}`}
              >
                <div
                  className={`px-2 py-1.5 border-b border-slate-200 rounded-t-lg ${
                    isLatest ? 'bg-blue-50' : 'bg-white'
                  }`}
                >
                  <div className="flex items-center justify-between">
                    <span className="font-mono text-[10px] text-slate-500">
                      {index === rolling.columns.length - 1
                        ? '今日'
                        : `D-${rolling.columns.length - 1 - index}`}
                    </span>
                    {isLatest && <Tag className="!m-0 text-[9px] leading-tight">最新</Tag>}
                  </div>
                  <div className="font-mono text-[11px] font-bold text-slate-700">{column.date}</div>
                </div>

                <div className="flex-1 p-1 space-y-1">
                  {column.picks.map((pick) => {
                    const status = rollingStatus(pick);
                    const label = positionLabel(pick);
                    const composite = formatResearchScore(researchScore(pick.pct));
                    const cap = capLabel(pick.market_cap_yi);
                    const profileLine = [pick.industry || '行业—', cap ?? '市值—'].join(' · ');
                    return (
                      <Tooltip
                        key={pick.symbol}
                        title={`${pick.name || pick.symbol} · 排名 ${pick.rank} · 覆盖 ${pick.coverage}/${memberCount} · 综合分 ${composite} · ${positionLabel(pick)} · ${profileLine}`}
                      >
                        <div
                          className={`rounded-md border px-1.5 py-1 ${ROLLING_TONE[status]}`}
                          data-testid={`combo-rolling-pick-${column.date}-${pick.symbol}`}
                        >
                          <div className="flex items-center justify-between gap-1">
                            <span className="font-mono text-[10px] font-black truncate">
                              {pick.code || pick.symbol}
                            </span>
                            <span className="font-mono text-[10px] shrink-0">{composite}</span>
                          </div>
                          <div className="text-[10px] truncate opacity-80">{pick.name || '—'}</div>
                          <div className="text-[9px] truncate opacity-60">{profileLine}</div>
                          <div className="flex items-center justify-between gap-1 mt-0.5">
                            <span
                              className={`rounded border px-1 text-[9px] font-bold ${positionTagClass(label)}`}
                            >
                              {label}
                            </span>
                            <span className="text-[9px] opacity-70">{rollingHeldLabel(pick)}</span>
                          </div>
                        </div>
                      </Tooltip>
                    );
                  })}
                  {column.picks.length === 0 && (
                    <div className="text-center text-[10px] text-slate-400 py-4">当日无标的</div>
                  )}
                </div>

                <div
                  className="px-1.5 py-1 border-t border-dashed border-slate-200 min-h-[26px] space-y-0.5"
                  data-testid={`combo-rolling-exits-${column.date}`}
                >
                  {(column.exit_rows?.length ?? 0) > 0 ? (
                    <>
                      <div className="text-[9px] font-bold text-amber-700">
                        退出 {column.exit_rows?.length} 只
                      </div>
                      {column.exit_rows?.map((row) => (
                        <Tooltip key={row.symbol} title={exitTradeLabel(row)}>
                          <div
                            className="flex items-center justify-between gap-1 cursor-help"
                            data-testid={`combo-rolling-exit-${column.date}-${row.symbol}`}
                          >
                            <span className="text-[9px] truncate text-slate-600">
                              {row.name || row.code || row.symbol}
                            </span>
                            <span
                              className={`text-[9px] font-mono font-bold shrink-0 ${returnToneClass(
                                row.return_pct,
                              )}`}
                            >
                              {formatReturnPct(row.return_pct)}
                            </span>
                          </div>
                        </Tooltip>
                      ))}
                    </>
                  ) : exits ? (
                    <Tooltip title="上一列在榜、本列不在榜（只标注一次）">
                      <div className="text-[9px] text-amber-700 leading-tight">
                        退出：{exits}
                      </div>
                    </Tooltip>
                  ) : (
                    <div className="text-[9px] text-slate-300">—</div>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {overflowX && (
        <div className="shrink-0 px-2 pb-1">
          <div
            ref={trackRef}
            role="scrollbar"
            aria-orientation="horizontal"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={Math.round(scrollbar.progress * 100)}
            aria-label="滚动建仓横向滚动"
            className="rolling-board-scrollbar relative h-2.5 w-full rounded-full bg-slate-100 cursor-pointer touch-none select-none"
            data-testid="combo-rolling-scrollbar"
            onPointerDown={onTrackPointerDown}
          >
            <div
              data-role="thumb"
              className="absolute top-0 h-full rounded-full bg-slate-400 hover:bg-slate-500"
              style={{ left: scrollbar.thumbLeft, width: scrollbar.thumbWidth }}
              data-testid="combo-rolling-scroll-thumb"
            />
          </div>
        </div>
      )}

      <div className="shrink-0 px-3 py-1.5 border-t border-slate-100 text-[10px] text-slate-400">
        持有天数 = 本窗口内连续在榜天数；断榜后重新计数，跨窗口不伪造连续。退出收益 =
        退出日收盘价 ÷ 入选日收盘价 − 1（按收盘价计，未计费用；窗口首列按窗口起点计）。
      </div>
    </div>
  );
};

export default RollingBuildPanel;
