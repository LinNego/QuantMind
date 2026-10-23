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
