/**
 * 候选信号「推理模型」下拉选项构建。
 *
 * 后端 `/stock-terminal/list` 回传的 `models` 现在包含**注册表全量（非归档）**，
 * 其中部分模型近 90 天没跑过推理。直接铺平会让用户以为「模型漏了」，所以：
 * - `has_signals_90d !== false` 的项排在前（有信号 → 近 30 天无信号加后缀）；
 * - `has_signals_90d === false` 的项放进「近 90 天无信号」分组，仍然可选；
 * - 老后端没有 `has_signals_90d` 字段 → 保持平铺与旧后缀，不臆造分组。
 */

import type { StockModelOption } from '../stock-terminal-shared/types';

export interface ModelSelectOption {
  label: string;
  value: string;
}

export interface ModelSelectGroup {
  label: string;
  options: ModelSelectOption[];
}

/** 单个选项的文案（不含分组标题）。 */
export function modelOptionLabel(m: StockModelOption): string {
  const name = m.display_name || m.model_id;
  // 无 90 天信号的项已落在分组标题下，后缀由标题交代，避免重复。
  if (m.has_signals_90d === false) return name;
  return `${name}${m.has_signals_30d === false ? ' · 近 30 天无信号' : ''}`;
}

/** 构建 antd Select 的 options（平铺数组或分组数组）。 */
export function buildModelSelectOptions(
  models: StockModelOption[],
): ModelSelectOption[] | ModelSelectGroup[] {
  const toOption = (m: StockModelOption): ModelSelectOption => ({
    label: modelOptionLabel(m),
    value: m.model_id,
  });

  const withoutSignals = models.filter((m) => m.has_signals_90d === false);
  // 老后端无 has_signals_90d → 全部按「有信号」处理，保持旧渲染
  if (withoutSignals.length === 0) return models.map(toOption);

  const withSignals = models.filter((m) => m.has_signals_90d !== false);
  const groups: ModelSelectGroup[] = [];
  if (withSignals.length > 0) {
    groups.push({
      label: `近 90 天有信号（${withSignals.length}）`,
      options: withSignals.map(toOption),
    });
  }
  groups.push({
    label: `近 90 天无信号（${withoutSignals.length}）`,
    options: withoutSignals.map(toOption),
  });
  return groups;
}
