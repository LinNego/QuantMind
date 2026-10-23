/** 今日执行折叠条：默认一行摘要，点开展开订单列表（并入持仓监控同页）。 */
import React, { useEffect, useState } from 'react';
import { Activity, ChevronDown, ChevronUp } from 'lucide-react';
import { getDeskToday } from '../../../../features/desk/services/deskService';
import {
  executionUnavailableReason,
  priceSourceHint,
} from '../../../../features/desk/deskModel';
import type { ExecutionItem } from '../../../../features/desk/types';

export const ExecutionStrip: React.FC = () => {
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<ExecutionItem[]>([]);
  const [unavailableReason, setUnavailableReason] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  // T6-2 审计 H7b：此前 catch(() => {}) 吞掉失败——`loaded` 永远 false，
  // 摘要行「加载中…」永驻；展开还被渲染成「今日暂无委托/成交记录」。
  // 抓取失败必须说出来，且给重试；「失败」与「没有」是两回事。
  const [loadError, setLoadError] = useState<string | null>(null);
  const [reloadTick, setReloadTick] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoadError(null);
    getDeskToday({ health: false, plan: false })
      .then((resp) => {
        if (!cancelled) {
          const execution = resp?.data?.execution;
          // 不可归集 ≠ 没交易：留着原因，别让下面渲染成"共 0 单"
          setUnavailableReason(executionUnavailableReason(execution));
          setItems(execution?.items || []);
          setLoaded(true);
        }
      })
      .catch((e: unknown) => {
        if (!cancelled) setLoadError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [reloadTick]);

  const filled = items.filter((it) => String(it.status).toUpperCase() === 'FILLED').length;
  const rejected = items.filter((it) => String(it.status).toUpperCase() === 'REJECTED').length;

  return (
    <div className="shrink-0 rounded-2xl border border-gray-200 bg-white shadow-sm overflow-hidden">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="w-full flex items-center gap-2 px-4 py-2 text-left hover:bg-slate-50/70 transition-colors"
      >
        <span className="w-6 h-6 rounded-lg bg-amber-50 text-amber-600 flex items-center justify-center">
          <Activity className="w-3.5 h-3.5" />
        </span>
        <span className="text-xs font-bold text-slate-700">今日执行</span>
        <span className="text-[11px] text-slate-400">
          {loadError
            ? '加载失败'
            : unavailableReason
              ? '暂不可按市场归集'
              : loaded
                ? `共 ${items.length} 单`
                : '加载中…'}
          {!unavailableReason && loaded && !loadError && items.length > 0 && ` · 成交 ${filled} · 拒单 ${rejected}`}
        </span>
        <span className="ml-auto text-slate-400">{open ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}</span>
      </button>
      {open && (
        <div className="max-h-52 overflow-y-auto px-3 pb-2 space-y-0.5 border-t border-slate-100 pt-1.5">
          {loadError ? (
            <div className="py-3 px-2 text-[11px] leading-relaxed text-rose-600">
              今日执行数据加载失败：{loadError}
              <button
                type="button"
                onClick={() => setReloadTick((t) => t + 1)}
                className="ml-2 rounded border border-rose-200 bg-rose-50 px-1.5 py-0.5 font-bold text-rose-700 hover:bg-rose-100"
              >
                重试
              </button>
            </div>
          ) : unavailableReason ? (
            <div className="py-3 px-2 text-[11px] leading-relaxed text-slate-500">{unavailableReason}</div>
          ) : (
            items.length === 0 && <div className="py-3 text-center text-[11px] text-slate-400">今日暂无委托/成交记录</div>
          )}
          {items.map((it, idx) => {
            const s = String(it.status || '').toUpperCase();
            const isBuy = String(it.side).toUpperCase() === 'BUY';
            const tone =
              s === 'FILLED'
                ? 'bg-red-50 text-red-700 border-red-200'
                : s === 'REJECTED'
                  ? 'bg-rose-50 text-rose-700 border-rose-200'
                  : 'bg-amber-50 text-amber-700 border-amber-200';
            return (
              <div key={`${it.client_order_id || it.symbol}-${idx}`} className="flex items-center gap-2 rounded-lg px-2 py-1 text-[11px] hover:bg-slate-50">
                <span className={`shrink-0 rounded border px-1 py-px text-[9px] font-bold ${it.mode === 'REAL' ? 'bg-purple-50 text-purple-700 border-purple-200' : 'bg-slate-100 text-slate-500 border-slate-200'}`}>
                  {it.mode}
                </span>
                <span className={`shrink-0 font-bold ${isBuy ? 'text-red-600' : 'text-emerald-600'}`}>{isBuy ? '买' : '卖'}</span>
                <span
                  className="min-w-0 truncate font-mono text-slate-700"
                  title={it.price_source ? `取价来源：${priceSourceHint(it.price_source)}` : undefined}
                >
                  {it.symbol}
                </span>
                <span className="shrink-0 font-mono text-slate-400">{it.quantity} 股</span>
                <span className={`ml-auto shrink-0 rounded border px-1.5 py-px text-[10px] font-bold ${tone}`}>
                  {s === 'FILLED' ? '成交' : s === 'REJECTED' ? '拒单' : s === 'SUBMITTED' || s === 'PENDING' || s === 'NEW' ? '已报' : it.status}
                </span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
};
