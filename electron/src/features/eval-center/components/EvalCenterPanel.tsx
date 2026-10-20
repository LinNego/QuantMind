/**
 * 评估中心（FE-E / T-FE-14/15/16，2026-09-20 三段式改版）
 *
 * 数据源：`/api/v1/eval/*`（eval_scores 表唯一读取面 + 长序列侧车）。
 *
 * 三段式（图形为主，但每一块都要能单独说清一件事）：
 * 1. **总览带**：等级分布堆叠条 + 对象数/平均分/红线数/**证据覆盖率**（分数会被缺维的
 *    薄卡拉高，所以要并排给出「几个对象有 ≥3 维实证」）；
 * 2. **榜单**：分数条 + 等级 + 维度覆盖条（有色=已算 / 斜纹=缺省）+ 该类最关键的一个
 *    实测量（模型→实测 RankIC、因子→ICIR、策略→年化/回撤、账户→利用率、选股→T+H 超额）；
 * 3. **详情**：实测量 → 分维得分 → 换手成本 → 一问一图（长序列）→ 历史曲线 → 页脚
 *    （来源/口径/时间戳）。
 *
 * 展示名：主标题优先 display_name（后端解析），原 object_id 作副标题。
 * 本文件只做编排，口径全在 `evalCenterModel` / `evalInsightModel` / `evalSeriesModel`。
 */

import React, { useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import {
  AlertTriangle,
  Award,
  CalendarCheck,
  Cpu,
  HeartPulse,
  Inbox,
  Landmark,
  RefreshCw,
  Search,
  Sigma,
  Target,
  Wallet,
} from 'lucide-react';
import {
  getEvalObjectTypes,
  getScoreHistory,
  getStrategyHealth,
  listScores,
} from '../services/evalCenterService';
import type { EvalObjectType, EvalScoreRow, StrategyHealthArchive } from '../types/evalCenter';
import { filterEvalRows, parseObjectDeepLink } from './evalCenterModel';
import { useUiMode } from '../../shared/useUiMode';
import { SelfHealthUpload } from './SelfHealthUpload';
import { EvalOverviewBand } from './EvalOverviewBand';
import { EvalRankList } from './EvalRankList';
import { EvalDetail } from './EvalDetail';
import { HealthArchivePanel } from './HealthArchivePanel';

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : '请求失败';
}

const TYPE_ICONS: Record<string, React.ComponentType<{ className?: string }>> = {
  factor: Sigma,
  model: Cpu,
  strategy: Target,
  account: Wallet,
  live_account: Landmark,
  daily_selection: CalendarCheck,
  strategy_health: HeartPulse,
};

/**
 * 页签短名：优先用后端直给的 `short_label`（U6：后端改 label 全称不再牵连页签）；
 * 只有老后端没带 short_label 时才回退到正则剥后缀。
 */
function typeShortLabel(t: EvalObjectType): string {
  if (t.short_label) return t.short_label;
  return t.label.replace(/评分卡$/, '').replace(/留档$/, '') || t.label;
}

/** 单次拉取的榜单项数（后端上限 500）；触到该值即提示可能还有更多 */
const LIST_LIMIT = 200;

export const EvalCenterPanel: React.FC = () => {
  const { isSimple } = useUiMode();
  const [objectTypes, setObjectTypes] = useState<EvalObjectType[]>([]);
  // 空串 = 未定档；类型表到达后取后端第一类（U5：默认不再硬编码 factor）
  const [activeType, setActiveType] = useState<string>('');
  const [rows, setRows] = useState<EvalScoreRow[]>([]);
  const [listTruncated, setListTruncated] = useState(false);
  const [listTotal, setListTotal] = useState<number | null>(null);
  const [selectedId, setSelectedId] = useState<string>('');
  const [history, setHistory] = useState<EvalScoreRow[]>([]);
  const [archive, setArchive] = useState<StrategyHealthArchive | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [query, setQuery] = useState('');
  /** 徽章深链待选中对象：等该类型榜单落地后再点中（榜单在途时 selectedId 无处可指） */
  const [pendingSelect, setPendingSelect] = useState<{ type: string; id: string } | null>(null);

  // 徽章深链（?object=model&object_id=…）：面板已挂载时再次点徽章也生效（searchParams 变化触发）
  const [searchParams] = useSearchParams();
  const deepLink = useMemo(() => parseObjectDeepLink(searchParams), [searchParams]);

  const loadObjectTypes = async () => {
    try {
      const resp = await getEvalObjectTypes();
      const types = resp?.data || [];
      setObjectTypes(types);
      setActiveType((prev) =>
        prev && types.some((t) => t.object_type === prev) ? prev : types[0]?.object_type || prev
      );
    } catch (err: unknown) {
      setError(errorText(err));
    }
  };

  useEffect(() => {
    void loadObjectTypes();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const ot = deepLink?.objectType;
    if (!deepLink || !ot) return;
    // 类型表未到先等（同一对象参数会随 objectTypes 变化再触发）；未知类型不盲跳
    if (objectTypes.length === 0 || !objectTypes.some((t) => t.object_type === ot)) return;
    setPendingSelect({ type: ot, id: deepLink.objectId });
    if (activeType !== ot) setActiveType(ot);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [deepLink, objectTypes]);

  // 榜单落地后消费待选中：命中即选中；该类型已加载完（非在途）仍没有此对象 → 如实放弃
  // （徽章显示「未生成」时点进来，榜单里本就没有它，硬停个空选中比不跳更误导）
  useEffect(() => {
    if (!pendingSelect || pendingSelect.type !== activeType) return;
    if (rows.some((r) => r.object_id === pendingSelect.id)) {
      setSelectedId(pendingSelect.id);
      setPendingSelect(null);
    } else if (!loading) {
      setPendingSelect(null);
    }
  }, [pendingSelect, rows, activeType, loading]);

  useEffect(() => {
    if (!activeType) return; // 类型未定档不发请求（object_type='' 必 400）
    void loadRows(activeType);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeType]);

  useEffect(() => {
    // 守卫：选中项必须属于当前类型（切页签瞬间旧 selectedId 未清，直查体检接口会 400）
    const row = rows.find((r) => r.object_id === selectedId);
    if (!selectedId || !row || row.object_type !== activeType) {
      setHistory([]);
      setArchive(null);
      return;
    }
    void loadDetail(activeType, selectedId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId, activeType, rows]);

  const loadRows = async (type: string, opts?: { keepSelection?: boolean }) => {
    const keepId = opts?.keepSelection ? selectedId : '';
    setLoading(true);
    setError('');
    setSelectedId('');
    setArchive(null);
    setHistory([]);
    try {
      const resp = await listScores({ objectType: type, latestOnly: true, limit: LIST_LIMIT });
      const data = resp?.data || [];
      setRows(data);
      setListTruncated(Boolean(resp?.meta?.truncated) || data.length >= LIST_LIMIT);
      setListTotal(typeof resp?.meta?.total === 'number' ? resp.meta.total : null);
      // 刷新保留选中（对象还在时）；首次加载/切类型则落第一行
      if (keepId && data.some((r) => r.object_id === keepId)) setSelectedId(keepId);
      else if (data.length > 0) setSelectedId(data[0].object_id);
    } catch (err: unknown) {
      setError(errorText(err));
      setRows([]);
      setListTruncated(false);
      setListTotal(null);
    } finally {
      setLoading(false);
    }
  };

  const loadDetail = async (type: string, objectId: string) => {
    try {
      if (type === 'strategy_health') {
        if (!/^\d+$/.test(objectId)) {
          // 策略体检仅数字策略 id（后端契约）；非数字直接空态，不发请求
          setArchive(null);
          setHistory([]);
          return;
        }
        const resp = await getStrategyHealth(objectId);
        setArchive(resp?.data || null);
        setHistory([]);
      } else {
        const resp = await getScoreHistory(type, objectId);
        setHistory(resp?.data || []);
        setArchive(null);
      }
    } catch (err: unknown) {
      setError(errorText(err));
    }
  };

  /** 刷新按钮：类型表（U5，失败后可重试）+ 榜单（保留选中）一起重取 */
  const refresh = async () => {
    if (objectTypes.length === 0) await loadObjectTypes();
    if (activeType) await loadRows(activeType, { keepSelection: true });
  };

  const visibleRows = useMemo(() => filterEvalRows(rows, query), [rows, query]);

  const selectedRow = useMemo(
    () => rows.find((r) => r.object_id === selectedId) || null,
    [rows, selectedId]
  );

  const isHealth = activeType === 'strategy_health';
  const activeTypeMeta = objectTypes.find((t) => t.object_type === activeType);
  const activeLabel = activeTypeMeta?.label || '';

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1.5">
        <div className="flex flex-wrap items-baseline gap-x-2.5">
          <h2 className="text-lg font-bold text-slate-800">评估中心</h2>
          <p className="text-[11px] text-slate-400">每日收盘自动评分 · 回测完成自动体检</p>
        </div>
        <button
          type="button"
          onClick={() => void refresh()}
          disabled={loading}
          title="重取类型表与榜单（保留当前选中对象）"
          className="inline-flex items-center gap-1 rounded-xl border border-slate-200 bg-white px-3 py-1.5 text-xs text-slate-700 hover:bg-slate-50 disabled:opacity-50"
        >
          <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
          刷新
        </button>
      </div>

      {/* 类型页签（图标 + 胶囊；短名，全称在悬停里） */}
      <div className="flex w-fit max-w-full flex-wrap items-center gap-1 rounded-full border border-slate-200 bg-slate-100 p-0.5">
        {objectTypes.map((otype) => {
          const Icon = TYPE_ICONS[otype.object_type] || Award;
          const active = activeType === otype.object_type;
          return (
            <button
              key={otype.object_type}
              type="button"
              title={otype.label}
              onClick={() => setActiveType(otype.object_type)}
              className={`flex items-center gap-1.5 rounded-full px-3.5 py-1.5 text-[11px] font-bold transition-colors ${
                active ? 'bg-white text-slate-800 shadow-sm' : 'text-slate-500 hover:text-slate-700'
              }`}
            >
              <Icon className="w-3 h-3" />
              {typeShortLabel(otype)}
            </button>
          );
        })}
      </div>

      {/* 榜单已有数据时的局部错误（如下钻详情失败）：顶部横幅提示；整榜失败走下方错误态块 */}
      {error && rows.length > 0 && (
        <div className="flex items-start gap-2 rounded-2xl border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800">
          <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0" />
          {error}
        </div>
      )}

      {loading ? (
        // 骨架屏与榜单卡片同形（三态视觉统一：加载=骨架、空=图标文案、错误=独立块）
        <div className="space-y-2" aria-label="正在读取评分卡">
          {[0, 1, 2, 3].map((i) => (
            <div key={i} className="h-[76px] animate-pulse rounded-2xl border border-slate-200/60 bg-slate-100/70" />
          ))}
          <div className="pt-1 text-center text-xs text-slate-400">正在读取评分卡…</div>
        </div>
      ) : error && rows.length === 0 ? (
        // 读取失败≠没有记录：低分说「暂无」等于把网络/权限问题说成业务结论
        <div className="flex flex-col items-center gap-2 rounded-2xl border border-amber-200 bg-amber-50/60 p-10 text-sm text-amber-800">
          <AlertTriangle className="w-8 h-8 text-amber-400" />
          评分卡读取失败，暂无法显示榜单
          <span className="text-xs text-amber-700/80">{error} · 可点右上角「刷新」重试</span>
        </div>
      ) : rows.length === 0 ? (
        isHealth ? (
          <div className="space-y-4">
            <div className="flex flex-col items-center gap-2 rounded-2xl border border-slate-200/80 bg-slate-50 p-6 text-center text-sm text-slate-500">
              <HeartPulse className="w-8 h-8 text-slate-300" />
              暂无体检留档（回测完成后自动生成；月度复检每月留档）——下方可直接自助体检：
            </div>
            <SelfHealthUpload />
          </div>
        ) : (
          <div className="flex flex-col items-center gap-2 rounded-2xl border border-slate-200/80 bg-slate-50 p-10 text-sm text-slate-500">
            <Inbox className="w-8 h-8 text-slate-300" />
            暂无该类型评分记录（评分任务在 EOD 跑批/回测体检后自动写入）
          </div>
        )
      ) : (
        <>
          {!isHealth && <EvalOverviewBand rows={rows} />}

          <div className="grid grid-cols-1 gap-4 lg:grid-cols-5">
            <div className="lg:col-span-2 flex flex-col gap-2 max-h-[720px]">
              {/* 客户端搜索：对象名/ID 包含即命中（榜单一次最多 200 条，直接过滤） */}
              <div className="flex items-center gap-1.5 rounded-xl border border-slate-200 bg-white px-2.5 py-1.5">
                <Search className="w-3.5 h-3.5 shrink-0 text-slate-400" />
                <input
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder="搜索对象名 / ID…"
                  className="w-full bg-transparent text-xs outline-none placeholder:text-slate-300"
                />
                {query && (
                  <span className="shrink-0 text-[10px] tabular-nums text-slate-400">
                    {visibleRows.length} / {rows.length}
                  </span>
                )}
              </div>
              <div className="min-h-0 flex-1 space-y-2 overflow-y-auto pr-1">
                {visibleRows.length === 0 ? (
                  <div className="rounded-2xl border border-slate-200/80 bg-slate-50 p-6 text-center text-xs text-slate-400">
                    没有匹配「{query}」的对象
                  </div>
                ) : (
                  <EvalRankList rows={visibleRows} selectedId={selectedId} onSelect={setSelectedId} />
                )}
              </div>
              {listTruncated && (
                <div className="shrink-0 rounded-xl border border-slate-200/80 bg-slate-50 px-3 py-1.5 text-[10px] leading-relaxed text-slate-400">
                  共{listTotal !== null ? ` ${listTotal} ` : ''}个对象，本次展示前 {LIST_LIMIT} 个
                  {listTotal === null ? '（可能还有更多）' : ''}——用上方搜索或切换类型缩小范围
                </div>
              )}
            </div>

            <div className="lg:col-span-3">
              {isHealth ? (
                <div className="space-y-4">
                  {archive ? (
                    <HealthArchivePanel archive={archive} />
                  ) : (
                    <div className="rounded-2xl border border-slate-200/80 bg-slate-50 p-8 text-center text-sm text-slate-500">
                      选择左侧策略查看体检档案
                    </div>
                  )}
                  <SelfHealthUpload />
                </div>
              ) : (
                <EvalDetail
                  row={selectedRow}
                  objectType={activeType}
                  typeLabel={activeTypeMeta ? typeShortLabel(activeTypeMeta) : activeLabel}
                  history={history}
                  isSimple={isSimple}
                />
              )}
            </div>
          </div>
        </>
      )}
    </div>
  );
};
