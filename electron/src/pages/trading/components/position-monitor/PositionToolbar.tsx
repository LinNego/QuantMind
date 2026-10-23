/**
 * 持仓监控工具行：全选 / 批量清仓 / 搜索 / 排序 / 密度。
 *
 * 排序与表头点击共用同一 `SortState`（`nextSort`），不存在两套口径。
 */
import React from 'react';
import { ArrowDown, ArrowUp, Rows2, Rows3, Search } from 'lucide-react';
import { Checkbox } from 'antd';
import type { SortKey, SortState } from './positionTableModel';

export type RowDensity = 'comfortable' | 'compact';

const SORT_LABELS: Array<{ key: SortKey; label: string }> = [
  { key: 'value', label: '市值' },
  { key: 'profit', label: '盈亏额' },
  { key: 'pct', label: '盈亏比例' },
  { key: 'shares', label: '持仓量' },
];

export interface PositionToolbarProps {
  query: string;
  onQueryChange: (value: string) => void;
  pickedCount: number;
  totalCount: number;
  allPicked: boolean;
  onToggleAll: () => void;
  onSellPicked: () => void;
  sort: SortState;
  onSortChange: (sort: SortState) => void;
  density: RowDensity;
  onToggleDensity: () => void;
}

export const PositionToolbar: React.FC<PositionToolbarProps> = ({
  query,
  onQueryChange,
  pickedCount,
  totalCount,
  allPicked,
  onToggleAll,
  onSellPicked,
  sort,
  onSortChange,
  density,
  onToggleDensity,
}) => (
  <div className="shrink-0 flex items-center gap-2 pb-1">
    <div className="flex shrink-0 items-center gap-1.5">
      {totalCount > 0 && (
        <Checkbox
          checked={allPicked}
          indeterminate={pickedCount > 0 && !allPicked}
          onChange={onToggleAll}
          className="!text-[10px] !font-bold !text-slate-500"
        >
          全选
        </Checkbox>
      )}
      {pickedCount > 0 && (
        <button
          type="button"
          onClick={onSellPicked}
          title="对勾选的持仓发起整仓卖出预检（数量由服务端按可用持仓给，仍需在确认面板里手动确认）"
          className="flex items-center gap-1 rounded-lg bg-emerald-600 px-2 py-0.5 text-[10px] font-bold text-white transition-colors hover:bg-emerald-700"
        >
          清仓选中 {pickedCount} 只
        </button>
      )}
    </div>

    <div className="ml-auto flex shrink-0 items-center gap-1.5">
      <div className="flex items-center overflow-hidden rounded-lg border border-slate-200 bg-white">
        <select
          aria-label="排序字段"
          value={sort.key}
          onChange={(e) => onSortChange({ key: e.target.value as SortKey, dir: sort.dir })}
          className="h-6 bg-transparent pl-1.5 pr-0.5 text-[10px] font-bold text-slate-500 outline-none"
        >
          {SORT_LABELS.map((item) => (
            <option key={item.key} value={item.key}>
              {item.label}
            </option>
          ))}
        </select>
        <button
          type="button"
          onClick={() => onSortChange({ key: sort.key, dir: sort.dir === 'desc' ? 'asc' : 'desc' })}
          title={sort.dir === 'desc' ? '当前降序，点击改升序' : '当前升序，点击改降序'}
          aria-label={sort.dir === 'desc' ? '改为升序' : '改为降序'}
          className="flex h-6 w-5 items-center justify-center border-l border-slate-100 text-slate-500 hover:text-blue-600"
        >
          {sort.dir === 'desc' ? (
            <ArrowDown className="h-3 w-3" />
          ) : (
            <ArrowUp className="h-3 w-3" />
          )}
        </button>
      </div>
      <div className="relative">
        <Search className="absolute left-2 top-1/2 h-3 w-3 -translate-y-1/2 text-slate-300" />
        <input
          value={query}
          onChange={(e) => onQueryChange(e.target.value)}
          placeholder="代码/名称"
          aria-label="搜索持仓"
          className="w-28 rounded-lg border border-slate-200 py-0.5 pl-6 pr-1.5 text-[10px] outline-none focus:border-blue-400"
        />
      </div>
      <button
        type="button"
        onClick={onToggleDensity}
        title={density === 'compact' ? '当前紧凑，点击改舒适' : '当前舒适，点击改紧凑'}
        aria-label={density === 'compact' ? '切换为舒适密度' : '切换为紧凑密度'}
        className="flex h-6 w-6 items-center justify-center rounded-lg border border-slate-200 text-slate-500 transition-colors hover:border-blue-300 hover:text-blue-600"
      >
        {density === 'compact' ? (
          <Rows2 className="h-3.5 w-3.5" />
        ) : (
          <Rows3 className="h-3.5 w-3.5" />
        )}
      </button>
    </div>
  </div>
);
