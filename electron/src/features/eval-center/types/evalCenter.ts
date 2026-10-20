/** 评估中心类型（FE-E：对齐后端 /api/v1/eval/* 契约） */

export interface EvalDimension {
  label?: string;
  score?: number | null;
  weight?: number;
  red_line_failed?: boolean;
  detail?: Record<string, unknown>;
}

export interface EvalScoreRow {
  object_type: string;
  object_id: string;
  /** 人话名（后端尽力解析：因子词典/模型元数据/回测配置/账户用户名；无 → null 由前端回退 object_id） */
  display_name?: string | null;
  snapshot_date: string | null;
  score: number | null;
  grade: string | null;
  low_confidence: boolean;
  red_line_failed: string[];
  dimensions: Record<string, EvalDimension>;
  inputs_version: Record<string, unknown>;
  created_at: string | null;
}

export interface EvalListResponse {
  success: boolean;
  data: EvalScoreRow[];
  /**
   * count = 本次返回条数。`truncated` 为真时 `total` 是该类型的真实对象总数
   * （后端只在触到 limit 时补算；老后端缺这两键 → 按「可能还有更多」提示）。
   */
  meta: { count: number; object_type: string; total?: number | null; truncated?: boolean };
}

export interface EvalHistoryResponse {
  success: boolean;
  data: EvalScoreRow[];
  meta: { count: number; object_type: string; object_id: string };
}

export interface EvalObjectType {
  object_type: string;
  label: string;
  /** 页签短名（后端直给；老后端缺省时前端回退剥后缀） */
  short_label?: string;
}

/** 长序列侧车的一个点（各序列的键不同：date/label/bucket/horizon/pair/name） */
export interface EvalSeriesPoint {
  date?: string;
  label?: string;
  name?: string;
  bucket?: number;
  horizon?: number;
  pair?: number;
  value: number;
  n_days?: number;
  is_min_segment?: boolean;
}

/** `/eval/series` 的 data（后端 factor_series / model_series 摊平产物，前端不重算） */
export interface EvalSeriesData {
  series?: Record<string, EvalSeriesPoint[]>;
  scalars?: Record<string, number | null>;
  /** 序列缺省原因（键与 series 同名）；缺省时这里是唯一解释来源，必须展示 */
  notes?: Record<string, string>;
}

export interface EvalSeriesMeta {
  object_type: string;
  object_id: string;
  /** false = 该对象尚未产出序列（正常返回，不是错误） */
  available: boolean;
  reason: string | null;
  note: string | null;
  generated_at: string | null;
  version: number | null;
}

export interface EvalSeriesResponse {
  success: boolean;
  data: EvalSeriesData;
  meta: EvalSeriesMeta;
}

export interface HealthSnapshot {
  verdict: string | null;
  verdict_label?: string | null;
  confidence: number | null;
  reasons: string[];
  suggestions: string[];
  items: Record<string, unknown>;
  backtest_id?: string | null;
  evidence_source?: string | null;
  snapshot_date: string | null;
}

export interface HealthHistoryPoint {
  snapshot_date: string | null;
  verdict: string | null;
  confidence: number | null;
  evidence_source?: string | null;
}

/**
 * 启动资格清单单项（后端 `shared/start_eligibility.py` 产出，与 `/start` 的 400 信封同源）。
 * `key`：lifecycle / health / sim_days / tracking_error。
 */
export interface StartEligibilityItem {
  key: string;
  /** 人话名（生命周期状态 / 回测体检 / 模拟交易日 / 跟踪误差） */
  label: string;
  /** 现状描述；**缺记录时后端给「无记录」，绝不渲染成 0** */
  value: string;
  passed: boolean;
  /** 未通过时的理由（后端与 `/start` 400 文案逐字一致）；通过时为空串 */
  reason: string;
  /** 未通过时的可执行下一步；通过时为空串 */
  next_step: string;
}

export interface StartEligibility {
  allowed: boolean;
  mode: string;
  /** 内置模板/文件上传不吃门禁 → 空数组（视为放行） */
  items: StartEligibilityItem[];
  /** 首个未通过项的下一步；全通过时为空串 */
  next_step: string;
}

/** 启动资格：按模式分（`gate` 只有 SIMULATION 预演，不要拿它当实盘门槛用） */
export type StartEligibilityMap = Partial<Record<'SIMULATION' | 'REAL', StartEligibility>>;

export interface StrategyHealthArchive {
  strategy_id: string;
  latest: HealthSnapshot | null;
  history: HealthHistoryPoint[];
  gate: { mode: string; allowed: boolean; note: string };
  /** 两模式逐项启动资格（启动前可见）；后端新加的键，老响应可能缺省 */
  eligibility?: StartEligibilityMap;
  eligibility_mode?: string;
}

export interface StrategyHealthResponse {
  success: boolean;
  data: StrategyHealthArchive;
  meta: { count: number };
}
