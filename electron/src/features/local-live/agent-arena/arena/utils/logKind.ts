import type { LogLine } from '../api/client';

/** 复盘轮判定（两代写入口径）。
 *
 *  历史日志写入方写 `mode: "review"`，后来的写入方写 `kind: "review"`；
 *  前端 2026-10-20 前只认 `kind`，导致存量复盘轮全部被当成分析轮——
 *  「盘后复盘」筛选为空、复盘内容混进「全部轮次」。两个键都认，避免再漏。
 */
export const isReviewLine = (line: Pick<LogLine, 'kind' | 'mode'> | null | undefined): boolean =>
  line?.kind === 'review' || line?.mode === 'review';
