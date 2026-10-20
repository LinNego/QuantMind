/**
 * 策略体检档案面板：最新结论 + **晋级门禁预演**（与执行点同源）+ 结论历史
 * + **启动资格清单**（SIM/REAL 两模式切换）。
 *
 * 从 `EvalCenterPanel` 抽出（面板只做编排）；除排版口径外行为与抽出前一致。
 *
 * 2026-10-09 改版：结论块由整卡底色（大块粉/红满屏）改为白底 + 左色边 +
 * 徽章化结论 —— 与详情页主卡同一语言，门禁放行/拦截用独立语义色（绿=放行），
 * 不再随结论色漂移。
 *
 * 2026-10-15（批 2-F3）：资格清单接线。后端 `/health/{id}` 的 `eligibility` 载荷
 * **恒含两模式**（`mode` 参数只回显 `eligibility_mode`），所以 SIM/REAL 切换是
 * 纯前端选择，不重发请求。逐项渲染 key/label/value/passed/reason/next_step——
 * 未过项必须把「卡在哪 + 下一步做什么」原文亮出来，不能只给一个红叉。
 */

import React, { useState } from 'react';
import { CheckCircle2, ShieldCheck, XCircle } from 'lucide-react';
import type {
  StartEligibilityMap,
  StrategyHealthArchive,
} from '../types/evalCenter';
import { gradeColor, gradeMeta } from './evalCenterModel';
import { CARD } from '../../desk/components/cardKit';

interface HealthArchivePanelProps {
  archive: StrategyHealthArchive;
}

type EligibilityMode = 'SIMULATION' | 'REAL';

const MODE_LABEL: Record<EligibilityMode, string> = {
  SIMULATION: '模拟资格',
  REAL: '实盘资格',
};

/** 启动资格清单：两模式切换 + 逐项明细（与 `/start` 执行点同源，展示即真实口径） */
const EligibilitySection: React.FC<{ map: StartEligibilityMap | undefined }> = ({ map }) => {
  const [mode, setMode] = useState<EligibilityMode>('SIMULATION');
  const current = map?.[mode];

  return (
    <section className={CARD}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-1.5">
          <ShieldCheck className="h-4 w-4 text-slate-400" />
          <h4 className="text-sm font-semibold text-slate-800">启动资格</h4>
          <span className="text-[10px] text-slate-400">与启动校验同源</span>
        </div>
        <div className="flex w-fit items-center gap-0.5 rounded-full border border-slate-200 bg-slate-100 p-0.5">
          {(['SIMULATION', 'REAL'] as EligibilityMode[]).map((m) => (
            <button
              key={m}
              type="button"
              onClick={() => setMode(m)}
              className={`rounded-full px-3 py-1 text-[11px] font-bold transition-colors ${
                mode === m ? 'bg-white text-slate-800 shadow-sm' : 'text-slate-500 hover:text-slate-700'
              }`}
            >
              {MODE_LABEL[m]}
            </button>
          ))}
        </div>
      </div>

      {!map ? (
        <p className="mt-2 text-xs text-slate-400">该档案未含启动资格清单（旧接口返回，刷新后重试）</p>
      ) : !current ? (
        <p className="mt-2 text-xs text-slate-400">该模式资格清单缺省（后端未返回 {mode} 档）</p>
      ) : (
        <>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <span
              className={`rounded-full border px-2.5 py-0.5 text-[11px] font-bold ${
                current.allowed
                  ? 'border-emerald-200 bg-emerald-50 text-emerald-700'
                  : 'border-red-200 bg-red-50 text-red-700'
              }`}
            >
              {current.allowed ? '放行' : '拦截'}
            </span>
            <span className="text-[10px] text-slate-400">
              {mode === 'REAL'
                ? '实盘资格在模拟预演之外加严（模拟交易日 / 跟踪误差）'
                : '模拟预演门禁：生命周期 + 回测体检'}
            </span>
          </div>

          <div className="mt-2.5 space-y-1.5">
            {current.items.map((item) => (
              <div
                key={item.key}
                className={`rounded-xl border p-2.5 ${
                  item.passed ? 'border-slate-100 bg-slate-50/60' : 'border-red-100 bg-red-50/50'
                }`}
              >
                <div className="flex items-start gap-2">
                  {item.passed ? (
                    <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0 text-emerald-500" />
                  ) : (
                    <XCircle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-red-500" />
                  )}
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                      <span className="text-xs font-semibold text-slate-700">{item.label}</span>
                      <span className={`text-xs tabular-nums ${item.passed ? 'text-slate-500' : 'text-red-700'}`}>
                        {item.value}
                      </span>
                    </div>
                    {!item.passed && item.reason && (
                      <div className="mt-1 text-[11px] text-red-700">{item.reason}</div>
                    )}
                    {!item.passed && item.next_step && (
                      <div className="mt-0.5 text-[11px] text-slate-500">下一步：{item.next_step}</div>
                    )}
                  </div>
                </div>
              </div>
            ))}
          </div>

          {!current.allowed && current.next_step && (
            <p className="mt-2 text-[11px] text-slate-500">先做：{current.next_step}</p>
          )}
        </>
      )}
    </section>
  );
};

export const HealthArchivePanel: React.FC<HealthArchivePanelProps> = ({ archive }) => {
  const latest = archive.latest;
  const meta = gradeMeta(latest?.verdict, false);
  const color = gradeColor(latest?.verdict);
  return (
    <div className="space-y-3">
      <section
        className="rounded-2xl border border-slate-200/80 bg-white p-4 shadow-[0_1px_2px_rgba(15,23,42,0.04)]"
        style={{ borderLeft: `4px solid ${color}` }}
      >
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <span className={`shrink-0 rounded-lg border px-2 py-0.5 text-xs font-extrabold ${meta.className}`}>
                {latest ? String(latest.verdict || '—') : '—'}
              </span>
              <span className="text-base font-bold text-slate-800">
                {latest ? latest.verdict_label || latest.verdict : '暂无体检记录'}
              </span>
            </div>
            {latest && (
              <div className="mt-1 text-[11px] text-slate-400">
                可信度 <b className="tabular-nums text-slate-600">{Math.round(latest.confidence ?? 0)}</b>/100
                {latest.evidence_source ? ` · 证据源 ${latest.evidence_source}` : ''}
                {latest.snapshot_date ? ` · ${latest.snapshot_date}` : ''}
              </div>
            )}
          </div>
          <span
            className={`shrink-0 rounded-full border px-2.5 py-1 text-[11px] font-bold ${
              archive.gate.allowed
                ? 'border-emerald-200 bg-emerald-50 text-emerald-700'
                : 'border-red-200 bg-red-50 text-red-700'
            }`}
          >
            晋级门禁：{archive.gate.allowed ? '放行' : '拦截'}
          </span>
        </div>
        <div className="mt-2 text-xs text-slate-600">{archive.gate.note}</div>
        {latest && (latest.reasons?.length > 0 || latest.suggestions?.length > 0) && (
          <div className="mt-2 space-y-1 border-t border-slate-100 pt-2 text-xs text-slate-600">
            {latest.reasons?.map((reason, index) => (
              <div key={`r-${index}`} className="flex gap-1.5">
                <span className="text-slate-300">·</span>
                <span>{reason}</span>
              </div>
            ))}
            {latest.suggestions?.map((suggestion, index) => (
              <div key={`s-${index}`} className="flex gap-1.5 text-slate-500">
                <span className="shrink-0 text-slate-300">建议 {index + 1}</span>
                <span>{suggestion}</span>
              </div>
            ))}
          </div>
        )}
      </section>

      <EligibilitySection map={archive.eligibility} />

      <div className={CARD}>
        <h4 className="mb-2 text-sm font-semibold text-slate-800">结论历史</h4>
        {archive.history.length === 0 ? (
          <p className="text-xs text-slate-400">
            暂无历史（体检在回测完成后自动生成；月度复检每月留档）
          </p>
        ) : (
          <div className="space-y-1.5">
            {archive.history.map((point, index) => {
              const pointMeta = gradeMeta(point.verdict, false);
              return (
                <div key={index} className="flex items-center gap-2 text-xs">
                  <span className="w-24 text-slate-500">{point.snapshot_date || '—'}</span>
                  <span className={`rounded border px-1.5 py-0.5 ${pointMeta.className}`}>
                    {point.verdict || '—'}
                  </span>
                  <span className="text-slate-600">可信度 {Math.round(point.confidence ?? 0)}</span>
                  {point.evidence_source && (
                    <span className="text-slate-400">（{point.evidence_source}）</span>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
};
