/**
 * 交易历史「原因」列的口径（纯函数）。
 *
 * `mark_rejected` 落库时给 `remarks` 加 `Execution rejected:` 前缀
 * （backend/services/simulation/services/execution_engine.py）；历史记录要展示
 * 的是**为什么被拒**，前端剥掉机器前缀，保留
 * `风控拒单[l3.stale_quote]（派发复检）: 行情陈旧` 这类正文。
 */
export function displayReason(raw: unknown): string {
  return String(raw ?? '')
    .trim()
    .replace(/^Execution rejected:\s*/i, '');
}

/**
 * 状态列用的短原因：`风控拒单[l3.stale_quote]（派发复检）: 行情陈旧` → `行情陈旧`。
 *
 * 状态列只有 ~12% 宽，全文放不下；完整拒因仍由 `displayReason` 进 title 与导出。
 * 没有冒号的技术性拒因（`Insufficient holdings for sell order`）原样保留并截断。
 */
export function displayReasonSummary(raw: unknown, maxChars = 24): string {
  const text = displayReason(raw);
  if (!text) return '';
  const parts = text.split(/[:：]/);
  const tail = parts.length > 1 ? parts[parts.length - 1].trim() : text;
  const source = tail || text;
  return source.length > maxChars ? `${source.slice(0, maxChars)}…` : source;
}
