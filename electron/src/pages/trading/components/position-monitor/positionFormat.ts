/**
 * 持仓监控展示口径（纯格式化，无副作用）。
 *
 * 口径纪律：
 * - 缺失一律 `—`，绝不显示成 0（研究评分同一条仓规）；
 * - A 股口径：盈利红、亏损绿；
 * - 个股盈亏比例由 positionMetrics 按百分数返回（9.8 = 9.8%），不要 ×100。
 */
import type { NormalizedHolding } from '../../utils/positionMetrics';

export const fmtMoney = (v: number | null | undefined): string =>
  v === null || v === undefined || Number.isNaN(Number(v))
    ? '—'
    : Number(v).toLocaleString('zh-CN', {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      });

export const fmtPct = (v: number | null | undefined, digits = 2): string =>
  v === null || v === undefined || Number.isNaN(Number(v))
    ? '—'
    : `${(Number(v) * 100).toFixed(digits)}%`;

/** 个股盈亏比例：positionMetrics 已按百分数口径返回（9.8 = 9.8%），不要再 ×100 */
export const fmtRowPct = (v: number | null | undefined): string =>
  v === null || v === undefined || Number.isNaN(Number(v))
    ? '—'
    : `${Number(v).toFixed(2)}%`;

/** A 股口径：盈利红、亏损绿 */
export const pnlTone = (v: number): string =>
  v > 0 ? 'text-red-600' : v < 0 ? 'text-emerald-600' : 'text-slate-500';

export const barTone = (v: number): string =>
  v > 0 ? 'bg-red-400' : v < 0 ? 'bg-emerald-400' : 'bg-slate-300';

/** Treemap 颜色：涨红跌绿按 |pct| 分四档（0/2%/5%/10%），零涨跌灰 */
export function tileColor(profit: number, pct: number): string {
  if (profit === 0) return '#cbd5e1';
  const a = Math.abs(pct);
  const pos = profit > 0;
  if (a >= 10) return pos ? '#b91c1c' : '#047857';
  if (a >= 5) return pos ? '#dc2626' : '#059669';
  if (a >= 2) return pos ? '#f87171' : '#34d399';
  return pos ? '#fecaca' : '#a7f3d0';
}

/** 明细行 title：`名称（代码） 持仓 N 股 · 市值 …`（测试按此前缀定位行） */
export function positionRowTitle(h: NormalizedHolding): string {
  return `${h.name}（${h.code}） 持仓 ${h.shares} 股 · 市值 ${
    h.priceMissing ? '—（缺现价）' : fmtMoney(h.value)
  }`;
}

/** 可用股数：上游给了才显示，没给一律 `—`（不拿持仓量冒充可用量） */
export function fmtAvailableShares(h: NormalizedHolding): string {
  const available = h.availableShares;
  return available === null || available === undefined || Number.isNaN(Number(available))
    ? '—'
    : Number(available).toLocaleString('zh-CN', { maximumFractionDigits: 0 });
}
