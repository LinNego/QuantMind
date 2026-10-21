/**
 * 组合选股 · 滚动建仓视图。
 *
 * 横向 N+1 列（D-N … 今日），每列 `per_day` 只，默认 11×5 = 55 格（窗口内去重约 50）。
 * 色块只表达「新进 / 留存 / 退出」，**刻意不用红绿**——红绿在 A 股是涨跌方向，
 * 与「是否新进」无关，混用会把方向暗示从颜色里加回来。
 *
 * 退出只在「昨列 → 今日」标注一次（后端已限定），这里不做二次推算。
 */

import React from 'react';
import { Alert, Button, Empty, Spin, Tag, Tooltip } from 'antd';
import { RefreshCw } from 'lucide-react';
import type { BlendRollingResponse } from '../../../../services/blendTypes';
import { formatResearchScore, researchScore } from '../../../shared/researchScore';
import { positionTagClass } from '../../positionLabels';
import {
  ROLLING_TONE,
  exitLabel,
  positionLabel,
  rollingHeldLabel,
  rollingStatus,
} from './comboRankingModel';

/** 市值（亿元）→ 展示值；缺失返回 null（渲染 `—`，不写 0） */
function capLabel(marketCapYi: number | null | undefined): string | null {
  if (typeof marketCapYi !== 'number' || !Number.isFinite(marketCapYi)) return null;
  if (marketCapYi >= 10000) return `${(marketCapYi / 10000).toFixed(2)} 万亿`;
  return `${marketCapYi.toFixed(0)} 亿`;
}

export interface RollingBuildPanelProps {
  rolling: BlendRollingResponse | null;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
}

export const RollingBuildPanel: React.FC<RollingBuildPanelProps> = ({
  rolling,
  loading,
  error,
  onRetry,
}) => {
  if (error) {
    return (
      <div className="p-3" data-testid="combo-rolling-error">
        <Alert
          type="error"
          showIcon
          message={error}
          action={
            <Button size="small" onClick={onRetry}>
              重试
            </Button>
          }
        />
      </div>
    );
  }

  if (loading && !rolling) {
    return (
      <div className="h-full min-h-0 min-w-0 flex items-center justify-center bg-white border border-slate-200 rounded-xl">
        <Spin tip="读取滚动窗口…" />
      </div>
    );
  }

  if (!rolling || rolling.columns.length === 0) {
    return (
      <div className="h-full min-h-0 min-w-0 flex items-center justify-center bg-white border border-slate-200 rounded-xl">
        <Empty description="该组合暂无滚动窗口数据" />
      </div>
    );
  }

  const memberCount = rolling.members.length;
  const latest = rolling.data_trade_date;

  return (
    <div
      className="h-full min-h-0 min-w-0 flex flex-col bg-white border border-slate-200 rounded-xl overflow-hidden"
      data-testid="combo-rolling-root"
    >
      <div className="shrink-0 flex items-center gap-3 px-3 py-2 border-b border-slate-100">
        <div className="flex items-center gap-2 text-[11px] text-slate-500">
          <span>
            窗口 <strong className="font-mono text-slate-800">{rolling.columns.length}</strong> 日
          </span>
          <span>
            每日 <strong className="font-mono text-slate-800">{rolling.per_day}</strong> 只
          </span>
          <span>
            去重 <strong className="font-mono text-slate-800">{rolling.summary.unique_symbols}</strong> 只
          </span>
          <span className="text-blue-700">
            今日新进 <strong className="font-mono">{rolling.summary.new_today}</strong>
          </span>
          <span className="text-amber-700">
            今日退出 <strong className="font-mono">{rolling.summary.exited_today}</strong>
          </span>
          <span className="text-slate-400">生效日 {rolling.target_date}</span>
        </div>
        <Tooltip title="新进=蓝、留存=白、退出=琥珀；颜色只表达名单变化，不表示涨跌">
          <span className="ml-auto text-[10px] text-slate-400 cursor-help">色块图例</span>
        </Tooltip>
        <Button size="small" icon={<RefreshCw size={13} />} loading={loading} onClick={onRetry}>
          刷新
        </Button>
      </div>

      {rolling.warnings.length > 0 && (
        <div className="shrink-0 px-3 pt-2" data-testid="combo-rolling-warnings">
          <Alert
            type="warning"
            showIcon
            message={rolling.warnings.slice(0, 3).join('；')}
          />
        </div>
      )}

      <div
        className="flex-1 min-h-0 overflow-auto overscroll-contain p-2"
        data-testid="combo-rolling-board"
      >
        <div className="flex gap-1.5 min-w-max items-stretch">
          {rolling.columns.map((column, index) => {
            const isLatest = column.date === latest;
            const exits = exitLabel(column);
            return (
              <div
                key={column.date}
                className="w-[160px] shrink-0 flex flex-col rounded-lg border border-slate-200 bg-slate-50/60"
                data-testid={`combo-rolling-col-${column.date}`}
              >
                <div
                  className={`px-2 py-1.5 border-b border-slate-200 rounded-t-lg ${
                    isLatest ? 'bg-blue-50' : 'bg-white'
                  }`}
                >
                  <div className="flex items-center justify-between">
                    <span className="font-mono text-[10px] text-slate-500">
                      {index === rolling.columns.length - 1
                        ? '今日'
                        : `D-${rolling.columns.length - 1 - index}`}
                    </span>
                    {isLatest && <Tag className="!m-0 text-[9px] leading-tight">最新</Tag>}
                  </div>
                  <div className="font-mono text-[11px] font-bold text-slate-700">{column.date}</div>
                </div>

                <div className="flex-1 p-1 space-y-1">
                  {column.picks.map((pick) => {
                    const status = rollingStatus(pick);
                    const label = positionLabel(pick);
                    const composite = formatResearchScore(researchScore(pick.pct));
                    const cap = capLabel(pick.market_cap_yi);
                    const profileLine = [pick.industry || '行业—', cap ?? '市值—'].join(' · ');
                    return (
                      <Tooltip
                        key={pick.symbol}
                        title={`${pick.name || pick.symbol} · 排名 ${pick.rank} · 覆盖 ${pick.coverage}/${memberCount} · 综合分 ${composite} · ${positionLabel(pick)} · ${profileLine}`}
                      >
                        <div
                          className={`rounded-md border px-1.5 py-1 ${ROLLING_TONE[status]}`}
                          data-testid={`combo-rolling-pick-${column.date}-${pick.symbol}`}
                        >
                          <div className="flex items-center justify-between gap-1">
                            <span className="font-mono text-[10px] font-black truncate">
                              {pick.code || pick.symbol}
                            </span>
                            <span className="font-mono text-[10px] shrink-0">{composite}</span>
                          </div>
                          <div className="text-[10px] truncate opacity-80">{pick.name || '—'}</div>
                          <div className="text-[9px] truncate opacity-60">{profileLine}</div>
                          <div className="flex items-center justify-between gap-1 mt-0.5">
                            <span
                              className={`rounded border px-1 text-[9px] font-bold ${positionTagClass(label)}`}
                            >
                              {label}
                            </span>
                            <span className="text-[9px] opacity-70">{rollingHeldLabel(pick)}</span>
                          </div>
                        </div>
                      </Tooltip>
                    );
                  })}
                  {column.picks.length === 0 && (
                    <div className="text-center text-[10px] text-slate-400 py-4">当日无标的</div>
                  )}
                </div>

                <div className="px-1.5 py-1 border-t border-dashed border-slate-200 min-h-[26px]">
                  {exits ? (
                    <Tooltip title="上一列在榜、本列不在榜（只标注一次）">
                      <div className="text-[9px] text-amber-700 leading-tight">
                        退出：{exits}
                      </div>
                    </Tooltip>
                  ) : (
                    <div className="text-[9px] text-slate-300">—</div>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </div>

      <div className="shrink-0 px-3 py-1.5 border-t border-slate-100 text-[10px] text-slate-400">
        持有天数 = 本窗口内连续在榜天数；断榜后重新计数，跨窗口不伪造连续。
      </div>
    </div>
  );
};

export default RollingBuildPanel;
