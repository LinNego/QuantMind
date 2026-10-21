/**
 * 组合选股（model blend）类型契约。
 *
 * 与 `backend/services/api/routers/model_blends.py` 一一对应；字段名按后端原样，
 * **不在前端改名**——改名会制造第二套词表，后端一加字段两边就漂。
 *
 * 合规边界：这里的 `position` 是后端 `BUY/HOLD/SELL` 枚举（位置判定），
 * 展示前必须过 `inference-center-shared/positionLabels.ratingPositionLabel`
 * 转成「靠前 / 居中 / 靠后」；组件里不得直接渲染该枚举。
 */

export type BlendMethod = 'mean' | 'median' | 'trimmed';
export type BlendPurpose = 'rebalance' | 'next_day';

/** 位置枚举：**内部值**，展示面必须经 `ratingPositionLabel` 转中性词 */
export type BlendPosition = 'BUY' | 'HOLD' | 'SELL' | string;

/** 分歧档位由后端唯一口径给出（`blend_engine.dispersion_band`） */
export type DispersionBand = 'low' | 'medium' | 'high' | 'unknown';

export interface BlendMemberRef {
  model_id: string;
  model_source: string;
  weight: number;
}

export interface BlendRecord {
  blend_id: string;
  tenant_id: string;
  user_id: string;
  name: string;
  market: string;
  purpose: BlendPurpose;
  source: 'manual' | 'system';
  template_id: string | null;
  horizon_days: number;
  weighting: 'equal' | 'manual';
  method: BlendMethod;
  trim_count: number;
  min_coverage: number;
  consensus_min_count: number;
  entry_per_day: number;
  rolling_window: number;
  target_positions: number;
  feature_signature: string;
  data_source: string;
  status: string;
  created_at: string | null;
  updated_at: string | null;
  /** `list_blends` 带成员；`create_blend` 返回的也带 */
  members?: BlendMemberRef[];
}

/** 榜单历史标注：与纸面台账/近 10 日 TopK 同源，缺失时全 null / 0 */
export interface BlendHistory {
  first_entry_date: string | null;
  last_entry_date: string | null;
  reentry_count: number;
  held_days: number;
  consecutive_days_in_top50: number;
}

export interface BlendRankingRow {
  code: string;
  symbol: string;
  name: string;
  /** 原始组合分（成员分位的加权平均，0–1） */
  S: number;
  S_pct: number;
  score: number;
  pct: number;
  rank: number;
  /** 对外分数刻度（0–100，一位小数），缺失为 null → 渲染 `—` */
  research_score: number | null;
  position: BlendPosition;
  coverage: number;
  dispersion: number | null;
  dispersion_band: DispersionBand;
  /** 成员贡献（分位口径），键为 model_id */
  contributions: Record<string, number>;
  missing_members: string[];
  history?: BlendHistory;
  /** 以下为后端未来可能补充的可选列（如行业/市值）；当前通常缺省 */
  industry?: string | null;
  market_cap_yi?: number | null;
}

export interface BlendRankingMember {
  model_id: string;
  display_name: string;
  model_source: string;
  model_type: string;
  horizon_days: number;
  weight: number;
  available: boolean;
  run_id?: string | null;
}

export interface BlendRankingStats {
  scored?: number;
  coverage?: number;
  dispersion?: Record<DispersionBand, number>;
  [key: string]: unknown;
}

export interface BlendRankingResponse {
  blend_id?: string;
  data_trade_date: string;
  members: BlendRankingMember[];
  ranking: BlendRankingRow[];
  stats?: BlendRankingStats;
  warnings: string[];
}

export interface BlendDatesResponse {
  blend_id: string;
  dates: string[];
}

/** 滚动建仓：每列一日、每列 Top `per_day`；`exits` 只在「昨列→今日」标注 */
export interface BlendRollingPick {
  symbol: string;
  code?: string;
  name: string;
  score: number;
  rank: number;
  row_rank: number;
  coverage: number;
  pct: number;
  position: BlendPosition;
  /** 本窗口内连续在榜天数（断榜后重新计数，不伪造跨窗连续） */
  held_days: number;
  is_new: boolean;
  industry?: string | null;
  market_cap_yi?: number | null;
}

export interface BlendRollingColumn {
  date: string;
  picks: BlendRollingPick[];
  exits: string[];
}

export interface BlendRollingSummary {
  unique_symbols: number;
  new_today: number;
  exited_today: number;
}

export interface BlendRollingResponse {
  blend_id: string;
  data_trade_date: string;
  target_date: string;
  window: number;
  per_day: number;
  days: string[];
  columns: BlendRollingColumn[];
  members: BlendRankingMember[];
  summary: BlendRollingSummary;
  warnings: string[];
}

export interface BlendPlanConfig {
  capital: number;
  top_k: number;
  hold_days: number;
  entry_per_day: number;
  target_positions: number;
  cash_buffer: number;
  adv_participation: number;
  exit_delta: number;
  lookback: number;
  min_lookback_days: number;
  /** 新进最低 ADV20（亿元）；0 表示不设门槛 */
  min_adv_yi: number;
  /** 是否剔除 ST/退市（硬条件） */
  exclude_st: boolean;
  adv_lookback_days: number;
  min_adv_days: number;
}

export interface BlendPlanSummary {
  capital: number;
  target_positions: number;
  entry_per_day: number;
  hold_days: number;
  top_k: number;
  cash_buffer: number;
  /** 扣除现金缓冲后的可用资金（元） */
  capital_available?: number;
  /** 已占用资金（持仓现值 + 新进目标金额，元） */
  capital_used?: number;
  /** 剩余资金（元） */
  capital_remaining?: number;
  min_adv_yi?: number;
  exclude_st?: boolean;
  adv_lookback_days?: number;
  /** false=行情或风险标记缺失；此类计划只能预览，不能确认 */
  data_ok?: boolean;
  n_entries: number;
  n_holds: number;
  n_exits: number;
  n_open_after: number;
}

/** 计划行：新进(entries)/留存(holds)/退出(exits) 共用形状，字段按类别可缺 */
export interface BlendPlanRow {
  symbol: string;
  /** 简称/行业/市值（当前截面；缺失为 null → 渲染 `—`） */
  name?: string | null;
  /** 仅 holds/exits：纸面台账首入日期 */
  first_entry_date?: string;
  /** 仅 holds/exits：已持有交易日数 */
  held_days?: number;
  score?: number | null;
  pct?: number | null;
  research_score?: number | null;
  position?: BlendPosition | null;
  rank?: number | null;
  mean20: number | null;
  exit_line: number | null;
  exit_reason?: string;
  note?: string;
  target_value?: number;
  ref_price?: number;
  shares?: number;
  /** ADV20（亿元）；缺失为 null → 渲染 `—` */
  adv_yi?: number | null;
  industry?: string | null;
  market_cap_yi?: number | null;
}

export interface BlendPlan {
  plan_id: string | null;
  blend_id: string;
  data_trade_date: string;
  target_date: string;
  params: BlendPlanConfig;
  summary: BlendPlanSummary;
  entries: BlendPlanRow[];
  holds: BlendPlanRow[];
  exits: BlendPlanRow[];
  warnings: string[];
  /** false=行情或 ST/退市标记缺失；后端 confirm_plan 会拒绝确认 */
  data_ok?: boolean;
  config_hash?: string;
  positions_hash?: string;
  confirmed_at?: string | null;
  /** 后端标注的确认窗口（最近 2 个交易日）；null=日历不可用，前端退回 /dates 序位判断 */
  can_confirm?: boolean | null;
  created_at?: string | null;
  blend?: { blend_id: string; name?: string; method?: string; horizon_days?: number };
}

/** 计划覆盖参数：只发用户显式改过的字段（缺省沿用组合自身配置） */
export interface BlendPlanOverrides {
  capital?: number;
  top_k?: number;
  hold_days?: number;
  entry_per_day?: number;
  target_positions?: number;
  min_adv_yi?: number;
  exclude_st?: boolean;
}

export interface BlendPlanRequest extends BlendPlanOverrides {
  data_trade_date?: string;
  persist?: boolean;
}

// ── 系统自动识别（模板） ───────────────────────────────────────────────

export type EvidenceGrade = 'recommended' | 'watch' | 'unverified' | string;

export interface ComboTemplateMember {
  model_id: string;
  model_source: string;
  weight: number;
  model_type: string;
  display_name: string;
}

export interface ComboTemplate {
  template_id: string;
  kind: string;
  name: string;
  market: string;
  horizon_days: number | null;
  /** 默认调仓用途与滚动建仓参数（后端按周期派生，旧模板可能缺省） */
  purpose?: BlendPurpose;
  entry_per_day?: number;
  rolling_window?: number;
  target_positions?: number;
  method: BlendMethod;
  trim_count: number;
  min_coverage: number;
  consensus_min_count: number;
  members: ComboTemplateMember[];
  /** 因「从未有过有效推理」被排除的成员数；0/缺省表示成员齐全 */
  missing_members?: number;
  evidence: { grade: EvidenceGrade; candidate_id: string | null };
  /** 基线模板恒排第一（C0 = 全 13 · T+10 等权） */
  baseline: boolean;
}

export interface ComboAutoCandidatesResponse {
  market: string;
  items: ComboTemplate[];
  baseline_template_id: string | null;
  evidence_available: boolean;
  evidence_generated_at: string | null;
  excluded: { model_id: string; reason: string }[];
  warnings: string[];
}

export interface ComboEligibleModel {
  model_id: string;
  display_name: string;
  model_type: string;
  model_source: string;
  horizon_days: number | null;
  feature_count: number;
  status: string;
}

export interface ComboEligibleGroup {
  key: string;
  signature: Record<string, unknown>;
  models: ComboEligibleModel[];
}

export interface ComboEligibleResponse {
  market: string;
  groups: ComboEligibleGroup[];
  excluded: { model_id: string; reason: string }[];
  total_eligible: number;
}

// ── 证据包（只读 P1 产物，不现算） ─────────────────────────────────────

export interface EvidenceWindowStats {
  days?: number;
  hit_theory_mean?: number | null;
  hit_exec_mean?: number | null;
  ret_theory_mean?: number | null;
  ret_exec_net_mean?: number | null;
  hit_exec_top20?: number | null;
  n_scored_mean?: number | null;
  n_entry_mean?: number | null;
  full_top_ratio?: number | null;
  [key: string]: unknown;
}

export interface EvidenceBootstrap {
  mean: number | null;
  ci_low: number | null;
  ci_high: number | null;
  ci_width: number | null;
  p_value: number | null;
  n_days?: number;
  n_blocks?: number;
}

export interface EvidenceMonthly {
  win_ratio: number | null;
  months?: Record<string, number>;
  candidate?: Record<string, number>;
}

export interface EvidenceGatePerformance {
  total_return?: number | null;
  annual_return?: number | null;
  max_drawdown?: number | null;
  sharpe?: number | null;
  trading_days?: number | null;
}

export interface EvidenceGate {
  performance?: EvidenceGatePerformance;
  avg_trade_return?: number | null;
  n_entries?: number | null;
  n_exits?: number | null;
  capacity_unchecked?: number | null;
  annual_delta?: number | null;
  drawdown_delta?: number | null;
  reference_annual?: number | null;
  reference_max_drawdown?: number | null;
}

export interface EvidenceCandidate {
  id: string;
  name: string;
  status: string;
  verdict: string;
  horizon?: number | null;
  method?: string;
  members: string[];
  windows?: Partial<Record<'selection' | 'validation' | 'test', EvidenceWindowStats>>;
  criteria?: {
    delta_test?: number | null;
    delta_validation?: number | null;
    checks?: Record<string, boolean>;
  };
  bootstrap?: EvidenceBootstrap | null;
  monthly?: EvidenceMonthly | null;
  bh_q?: number | null;
  gate?: EvidenceGate | Record<string, never>;
  leave_one_out?: { max_delta?: number | null; member?: string; deltas?: Record<string, number> } | null;
  notes?: string[];
  failed_checks?: string[];
  [key: string]: unknown;
}

export interface EvidenceRecommendation {
  ids: string[];
  top: string | null;
  reason: string;
}

export interface BlendEvidenceResponse {
  available: boolean;
  path: string;
  schema_version?: number;
  generated_at?: string;
  recommendation?: EvidenceRecommendation;
  kill_criteria?: Record<string, Record<string, unknown>>;
  caveats?: string[];
  candidates?: EvidenceCandidate[];
  /** 证据包实际形状：`selection: ["2025-02-05", "2025-12-31"]` */
  splits?: Record<string, [string, string] | { start?: string; end?: string }>;
  [key: string]: unknown;
}

// ── 请求体 ─────────────────────────────────────────────────────────────

export interface BlendPreviewRequest {
  market?: string;
  model_ids: string[];
  data_trade_date?: string;
  purpose?: BlendPurpose;
  method?: BlendMethod;
  trim_count?: number;
  top_k?: number;
  top?: number;
  min_coverage?: number;
  consensus_min_count?: number;
  weights?: Record<string, number> | null;
}

export interface BlendCreateRequest {
  name: string;
  market?: string;
  model_ids: string[];
  purpose?: BlendPurpose;
  method?: BlendMethod;
  trim_count?: number;
  min_coverage?: number;
  consensus_min_count?: number;
  entry_per_day?: number;
  rolling_window?: number;
  target_positions?: number;
  weights?: Record<string, number> | null;
  source?: 'manual' | 'system';
  template_id?: string | null;
}

export interface BlendPatchRequest {
  name?: string;
  purpose?: BlendPurpose;
  method?: BlendMethod;
  trim_count?: number;
  min_coverage?: number;
  consensus_min_count?: number;
  entry_per_day?: number;
  rolling_window?: number;
  target_positions?: number;
  model_ids?: string[];
  weights?: Record<string, number> | null;
}
