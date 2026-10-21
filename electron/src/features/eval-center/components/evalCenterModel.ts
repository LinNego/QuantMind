/** 评估中心纯函数（评分卡展示模型；可单测，无副作用） */

import type { EvalRunStatus, EvalScoreRow } from '../types/evalCenter';

export interface GradeMeta {
  label: string;
  className: string;
  isLowConfidence: boolean;
}

/** 评级 → 徽章样式（A≥85 / B 70-84 / C 60-69 / D<60；体检四分类 A/B/L/E 同表可渲染） */
export function gradeMeta(grade: string | null | undefined, lowConfidence = false): GradeMeta {
  const raw = String(grade || '').trim().toUpperCase();
  const isLowConfidence = lowConfidence || raw.endsWith('†');
  const base = raw.replace('†', '');
  const styleMap: Record<string, string> = {
    A: 'bg-red-50 text-red-700 border-red-200',
    B: 'bg-blue-50 text-blue-700 border-blue-200',
    C: 'bg-orange-50 text-orange-700 border-orange-200',
    D: 'bg-slate-100 text-slate-600 border-slate-200',
    L: 'bg-amber-50 text-amber-800 border-amber-200',
    E: 'bg-slate-100 text-slate-500 border-slate-300',
  };
  const labelMap: Record<string, string> = {
    A: 'A 优秀',
    B: 'B 良好',
    C: 'C 合格',
    D: 'D 不合格',
    L: 'L 运气嫌疑',
    E: 'E 证据不足',
  };
  return {
    label: labelMap[base] || (base ? `${base}` : '未评级'),
    className: styleMap[base] || 'bg-slate-100 text-slate-500 border-slate-200',
    isLowConfidence,
  };
}

/** 评级 → 主色（图表/进度条用 hex；语义遵循 A 股红涨：A 红 / B 蓝 / C 橙 / D 灰） */
export function gradeColor(grade: string | null | undefined): string {
  const base = String(grade || '').trim().toUpperCase().replace('†', '');
  const colorMap: Record<string, string> = {
    A: '#dc2626',
    B: '#2563eb',
    C: '#ea580c',
    D: '#64748b',
    L: '#d97706',
    E: '#94a3b8',
  };
  return colorMap[base] || '#6366f1';
}

export interface GradeCount {
  grade: string;
  count: number;
}

/** 评级分布（A/B/C/D/L/E 固定顺序；未评级归 '?'；零值不出现） */
export function gradeCounts(rows: EvalScoreRow[] | null | undefined): GradeCount[] {
  const list = rows || [];
  const order = ['A', 'B', 'C', 'D', 'L', 'E', '?'];
  const tally = new Map<string, number>();
  for (const row of list) {
    const base = String(row.grade || '').trim().toUpperCase().replace('†', '') || '?';
    const key = order.includes(base) ? base : '?';
    tally.set(key, (tally.get(key) || 0) + 1);
  }
  return order
    .filter((g) => (tally.get(g) || 0) > 0)
    .map((g) => ({ grade: g, count: tally.get(g) as number }));
}

/** 平均分（只统计有效分数；一位小数；无有效分数 → null） */
export function averageScore(rows: EvalScoreRow[] | null | undefined): number | null {
  const scores = (rows || [])
    .map((r) => r.score)
    .filter((s): s is number => typeof s === 'number');
  if (scores.length === 0) return null;
  return Math.round((scores.reduce((a, b) => a + b, 0) / scores.length) * 10) / 10;
}

export interface RowLabels {
  primary: string;
  secondary: string | null;
}

/** 列表主/副标题：display_name 优先、原 id 作副标题；无名字时按类型兜底 */
export function rowLabels(row: Pick<EvalScoreRow, 'object_type' | 'object_id' | 'display_name'>): RowLabels {
  const name = String(row.display_name || '').trim();
  if (name) return { primary: name, secondary: row.object_id };
  if (row.object_type === 'strategy') {
    return { primary: `策略回测 ${row.object_id.slice(0, 8)}…`, secondary: row.object_id };
  }
  if (row.object_type === 'strategy_health') {
    // object_id = 策略 id：后端解析策略名失败时兜底（裸数字看不懂）
    return { primary: `策略 #${row.object_id}`, secondary: null };
  }
  return { primary: row.object_id, secondary: null };
}

export interface DimensionView {
  key: string;
  label: string;
  score: number | null;
  weight: number;
  redLine: boolean;
  note: string;
}

/** 维度明细 → 视图行（缺省维度如实展示 note，不隐藏） */
export function dimensionViews(row: EvalScoreRow | null | undefined): DimensionView[] {
  const dims = row?.dimensions || {};
  return Object.entries(dims).map(([key, dim]) => {
    const detail = (dim?.detail || {}) as Record<string, unknown>;
    const note = detail.insufficient
      ? String(detail.note || '本期不可评（权重已归一）')
      : detail.red_line
        ? String(detail.red_line)
        : '';
    return {
      key,
      label: String(dim?.label || key),
      score: typeof dim?.score === 'number' ? dim.score : null,
      weight: typeof dim?.weight === 'number' ? dim.weight : 0,
      redLine: Boolean(dim?.red_line_failed),
      note,
    };
  });
}

export interface HistorySeries {
  dates: string[];
  scores: (number | null)[];
  grades: (string | null)[];
}

/** 历史序列（升序输入；空/单点也可返回，由视图层决定是否画线） */
export function historySeries(rows: EvalScoreRow[] | null | undefined): HistorySeries {
  const list = rows || [];
  return {
    dates: list.map((r) => r.snapshot_date || ''),
    scores: list.map((r) => (typeof r.score === 'number' ? r.score : null)),
    grades: list.map((r) => r.grade || null),
  };
}

/**
 * 图表色（评估中心唯一色值出口）：与 `cardKit` 的 TONES 同色系，语义守 A 股红涨
 * —— 红=正向/做多侧，绿=负向/做空侧，琥珀=风险量（回撤、红线、缺证据）。
 * 组件里不许再出现别的 hex。
 */
export const CHART_COLORS = {
  up: '#dc2626',
  down: '#059669',
  neutral: '#64748b',
  risk: '#d97706',
  axis: '#94a3b8',
  split: '#e2e8f0',
} as const;

/** 窄化外部值 → number；null/undefined/空串/NaN/Infinity → null（缺值不与 0 同形） */
export function toNumber(value: unknown): number | null {
  if (value === null || value === undefined) return null;
  if (typeof value === 'string' && value.trim() === '') return null;
  const num = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(num) ? num : null;
}

/** 数字 → 定长小数；缺值 → '—' */
export function formatNumber(value: unknown, digits = 4): string {
  const num = toNumber(value);
  return num === null ? '—' : num.toFixed(digits);
}

/** 小数 → 百分数（不带符号，用于占比/利用率这类天然非负量） */
export function formatPercent(value: unknown, digits = 1): string {
  const num = toNumber(value);
  return num === null ? '—' : `${(num * 100).toFixed(digits)}%`;
}

/** 收益率 → 带符号百分数：正数显式加 `+`（符号本身是信息，A 股红涨口径） */
export function formatSignedPct(value: unknown, digits = 1): string {
  const num = toNumber(value);
  if (num === null) return '—';
  return `${num > 0 ? '+' : ''}${(num * 100).toFixed(digits)}%`;
}

/**
 * URL 深链对象参数（评估徽章 → 评估中心下钻）：`object=<类型>&object_id=<id>`。
 * 两个都齐才认；缺一个 → null（不半跳：类型对、id 空会落到榜单第一行，比不跳更误导）。
 */
export interface ObjectDeepLink {
  objectType: string;
  objectId: string;
}

export function parseObjectDeepLink(params: {
  get(name: string): string | null;
}): ObjectDeepLink | null {
  const objectType = String(params.get('object') || '').trim();
  const objectId = String(params.get('object_id') || '').trim();
  if (!objectType || !objectId) return null;
  return { objectType, objectId };
}

/**
 * 榜单客户端搜索：对象 id / 展示名 / 行内人话名（含副标题 id）包含即命中。
 * 空查询原样返回（同一引用，便于调用方 memo）。
 */
export function filterEvalRows(
  rows: EvalScoreRow[] | null | undefined,
  query: string,
): EvalScoreRow[] {
  const list = rows || [];
  const q = query.trim().toLowerCase();
  if (!q) return list;
  return list.filter((row) => {
    const labels = rowLabels(row);
    const haystack = `${row.object_id} ${row.display_name || ''} ${labels.primary} ${labels.secondary}`;
    return haystack.toLowerCase().includes(q);
  });
}

/**
 * 本轮覆盖说明（P1，2026-10-21）：把「评了多少 / 总共多少 / 为什么只有这些」
 * 从 `inputs_version.coverage` 翻成人话。没有覆盖元数据（老库/旧卡）→ null，
 * 页面不显示——**绝不猜 0/0**。
 */
export function coverageNote(rows: EvalScoreRow[] | null | undefined): string | null {
  const row = (rows || [])[0];
  const raw = row?.inputs_version?.['coverage'];
  if (!row || !raw || typeof raw !== 'object') return null;
  const cov = raw as Record<string, unknown>;
  const num = (value: unknown): number | null =>
    typeof value === 'number' && Number.isFinite(value) ? value : null;

  if (row.object_type === 'strategy') {
    const candidates = num(cov['candidates']);
    const scored = num(cov['scored']);
    if (candidates === null || scored === null) return null;
    const skipped = num(cov['skipped']) ?? 0;
    const limit = num(cov['limit']);
    const cap =
      cov['cap_reached'] === true
        ? `（已达上限${limit !== null ? ` ${limit} 条` : ''}，未覆盖全部候选）`
        : '（候选池已跑完）';
    return `本轮候选 ${candidates} 条已完成回测，评分 ${scored} 条${
      skipped > 0 ? `，跳过 ${skipped} 条` : ''
    }${cap}`;
  }

  if (row.object_type === 'factor') {
    const scored = num(cov['scored']);
    const total = num(cov['total']);
    if (scored === null || total === null) return null;
    if (cov['selection'] === 'explicit') return '本轮为单因子评分（1/1）';
    if (cov['all'] === true) return `本轮全量评分 ${scored} / ${total} 个因子`;
    const requested = num(cov['requested_top']);
    return `本轮按 |IC| 评前 ${scored} / 共 ${total} 个因子${
      requested !== null ? `（上限 ${requested}）` : ''
    }；全量重评用 CLI --factor-top 0`;
  }

  return null;
}

export interface RunStatusView {
  label: string;
  detail: string;
  tone: 'running' | 'done' | 'error';
  /** 是否应继续轮询（queued/running） */
  active: boolean;
}

/** 手动重评状态 → 页面文案（idle → null，不渲染状态条）。 */
export function runStatusView(
  status: EvalRunStatus | null | undefined
): RunStatusView | null {
  if (!status || status.state === 'idle') return null;
  if (status.state === 'queued') {
    return { label: '排队中', detail: '等待 trade worker 消费', tone: 'running', active: true };
  }
  if (status.state === 'running') {
    return { label: '评分中', detail: '六卡重算中，约 1–3 分钟', tone: 'running', active: true };
  }
  if (status.state === 'done') {
    const scored = status.total_scored ?? 0;
    const errors = status.total_errors ?? 0;
    const rerun = status.backfill?.rerun;
    const detail = `落分 ${scored}，异常 ${errors}${
      typeof rerun === 'number' ? `；回填 ${rerun} 条` : ''
    }`;
    return { label: '已完成', detail, tone: 'done', active: false };
  }
  if (status.state === 'error') {
    return {
      label: '失败',
      detail: status.errors?.[0] || status.note || '未知错误',
      tone: 'error',
      active: false,
    };
  }
  return null;
}
