/**
 * 推理中心展示面的位置标签（单一来源）。
 *
 * 位置词本身来自 `features/shared/signalVocabulary.ts`；这里只补两件事：
 * 1. 后端历史评级里的 `STRONG_BUY / STRONG_SELL` 是**已知枚举**
 *    （`research_service` 里 STRONG_BUY = BUY 且分数 ≥ 0.03），不是未知值，
 *    归一成 BUY/SELL 后再走唯一词表，避免满屏 `—`；
 * 2. 位置标签统一用中性色（蓝/灰/紫），**不用红绿**——红绿在 A 股语境是涨跌方向，
 *    不是「排名前后」，两者混用会把刚去掉的方向暗示从颜色里加回来。
 */

import { signalPositionLabel } from '../shared/signalVocabulary';

/** 位置标签配色：靠前=蓝、居中=灰、靠后=紫 */
export const POSITION_TAG_CLASS: Record<string, string> = {
  靠前: 'text-blue-700 bg-blue-50 border-blue-200',
  居中: 'text-slate-600 bg-slate-100 border-slate-200',
  靠后: 'text-violet-700 bg-violet-50 border-violet-200',
};

/** 未知位置（如 `—`）的兜底配色 */
export const POSITION_TAG_FALLBACK_CLASS = 'text-slate-500 bg-slate-50 border-slate-200';

/**
 * 后端评级 → 位置词。
 *
 * `STRONG_BUY` / `STRONG_SELL` 只做前缀归一，不在这里另造一套译法；
 * 真正的未知值仍由 `signalPositionLabel` 返回 `—`（不猜）。
 */
export function ratingPositionLabel(rating: string | null | undefined): string {
  const key = String(rating ?? '').trim().toUpperCase();
  const side = key === 'STRONG_BUY' ? 'BUY' : key === 'STRONG_SELL' ? 'SELL' : key;
  return signalPositionLabel(side);
}

/** 位置标签的 Tailwind 类名（未知位置走兜底） */
export function positionTagClass(label: string): string {
  return POSITION_TAG_CLASS[label] ?? POSITION_TAG_FALLBACK_CLASS;
}
