/**
 * 组合选股 · 左栏（我的组合 / 推荐模板 / 新建）。
 *
 * 「推荐模板」是系统按同特征签名自动归组的候选（C0 全 13·T+10 恒为基线），
 * 证据等级来自 P1 研究产物；本轮研究结论为**无推荐组合**，所以模板只标「观察/未验证」，
 * 不写「最优」「命中率最高」这类话术。
 */

import React from 'react';
import { Button, Empty, Skeleton, Tag } from 'antd';
import { Layers, Plus } from 'lucide-react';
import type { BlendRecord, ComboTemplate } from '../../../../services/blendTypes';
import type { ComboSelection } from '../../hooks/useComboWorkspace';
import {
  blendHorizonLabel,
  blendMethodLabel,
  templateGradeLabel,
} from './comboRankingModel';

export interface ComboListProps {
  blends: BlendRecord[];
  templates: ComboTemplate[];
  selection: ComboSelection | null;
  baselineTemplateId: string | null;
  loading: boolean;
  /** 清单/模板读取失败：空态文案要说「读取失败」，不能谎称「暂无」 */
  error?: string | null;
  onSelect: (selection: ComboSelection) => void;
  onNew: () => void;
}

function selected(selection: ComboSelection | null, kind: ComboSelection['kind'], id: string): boolean {
  return selection?.kind === kind && selection.id === id;
}

const ITEM_CLASS =
  'w-full text-left rounded-lg border px-2.5 py-2 transition-colors';
const ITEM_ACTIVE = 'border-blue-300 bg-blue-50/70 ring-1 ring-blue-100';
const ITEM_IDLE = 'border-slate-200 bg-white hover:border-blue-200 hover:bg-slate-50';

export const ComboList: React.FC<ComboListProps> = ({
  blends,
  templates,
  selection,
  baselineTemplateId,
  loading,
  error = null,
  onSelect,
  onNew,
}) => {
  return (
    <aside
      className="w-[268px] shrink-0 flex flex-col bg-white border border-slate-200 rounded-xl overflow-hidden"
      data-testid="combo-list"
    >
      <div className="shrink-0 flex items-center justify-between px-3 py-2 border-b border-slate-100">
        <span className="flex items-center gap-1.5 text-xs font-black text-slate-800">
          <Layers size={14} className="text-blue-600" /> 组合
        </span>
        <Button size="small" type="primary" icon={<Plus size={13} />} onClick={onNew}>
          新建
        </Button>
      </div>

      <div className="flex-1 min-h-0 overflow-auto p-2 space-y-3">
        <section>
          <div className="px-1 pb-1 text-[10px] font-bold tracking-wide text-slate-400">
            我的组合 {blends.length > 0 ? `· ${blends.length}` : ''}
          </div>
          {loading && blends.length === 0 ? (
            <Skeleton active paragraph={{ rows: 3 }} title={false} />
          ) : error && blends.length === 0 ? (
            <div className="px-1 py-3 text-[11px] text-rose-500">组合清单读取失败</div>
          ) : blends.length === 0 ? (
            <div className="px-1 py-3 text-[11px] text-slate-400">
              暂无已保存组合；选中下方模板后保存即可启用滚动与纸面计划。
            </div>
          ) : (
            <div className="space-y-1.5">
              {blends.map((blend) => {
                const active = selected(selection, 'blend', blend.blend_id);
                const isBaseline = Boolean(blend.template_id && blend.template_id === baselineTemplateId);
                return (
                  <button
                    key={blend.blend_id}
                    type="button"
                    onClick={() => onSelect({ kind: 'blend', id: blend.blend_id })}
                    className={`${ITEM_CLASS} ${active ? ITEM_ACTIVE : ITEM_IDLE}`}
                    data-testid={`combo-item-blend-${blend.blend_id}`}
                  >
                    <div className="flex items-center justify-between gap-1">
                      <span className="text-[11px] font-bold text-slate-800 truncate">{blend.name}</span>
                      {isBaseline && (
                        <Tag color="blue" className="!m-0 text-[9px] leading-tight">
                          基线
                        </Tag>
                      )}
                    </div>
                    <div className="mt-0.5 flex items-center gap-1.5 text-[10px] text-slate-500">
                      <span className="font-mono">{blendHorizonLabel(blend.horizon_days)}</span>
                      <span>·</span>
                      <span>{blend.members?.length ?? '—'} 只</span>
                      <span>·</span>
                      <span>{blendMethodLabel(blend.method)}</span>
                      <span className="ml-auto text-slate-400">
                        {blend.source === 'system' ? '系统' : '自建'}
                      </span>
                    </div>
                  </button>
                );
              })}
            </div>
          )}
        </section>

        <section>
          <div className="px-1 pb-1 text-[10px] font-bold tracking-wide text-slate-400">
            推荐模板（同特征自动归组）
          </div>
          {templates.length === 0 ? (
            loading ? (
              <Skeleton active paragraph={{ rows: 2 }} title={false} />
            ) : error ? (
              <div className="px-1 py-2 text-[11px] text-rose-500">模板读取失败</div>
            ) : (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无可用模板" />
            )
          ) : (
            <div className="space-y-1.5">
              {templates.map((template) => {
                const active = selected(selection, 'template', template.template_id);
                const grade = template.evidence?.grade ?? 'unverified';
                return (
                  <button
                    key={template.template_id}
                    type="button"
                    onClick={() => onSelect({ kind: 'template', id: template.template_id })}
                    className={`${ITEM_CLASS} ${active ? ITEM_ACTIVE : ITEM_IDLE}`}
                    data-testid={`combo-item-template-${template.template_id}`}
                  >
                    <div className="flex items-center justify-between gap-1">
                      <span className="text-[11px] font-bold text-slate-800 truncate">{template.name}</span>
                      <span className="flex items-center gap-1 shrink-0">
                        {template.baseline && (
                          <Tag color="blue" className="!m-0 text-[9px] leading-tight">
                            基线
                          </Tag>
                        )}
                        <Tag
                          color={grade === 'recommended' ? 'green' : grade === 'watch' ? 'gold' : 'default'}
                          className="!m-0 text-[9px] leading-tight"
                        >
                          {templateGradeLabel(grade)}
                        </Tag>
                      </span>
                    </div>
                    <div className="mt-0.5 flex items-center gap-1.5 text-[10px] text-slate-500">
                      <span className="font-mono">{blendHorizonLabel(template.horizon_days)}</span>
                      <span>·</span>
                      <span>{template.members.length} 只</span>
                      <span>·</span>
                      <span>{blendMethodLabel(template.method)}</span>
                    </div>
                  </button>
                );
              })}
            </div>
          )}
        </section>
      </div>

      <div className="shrink-0 px-3 py-2 border-t border-slate-100 text-[10px] leading-relaxed text-slate-400">
        模板只预览当日榜单；保存为组合后才可查看滚动建仓、生成纸面计划。
      </div>
    </aside>
  );
};

export default ComboList;
