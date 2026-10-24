/**
 * 大盘分析（模型管理 → 大盘分析）图表口径：均分 × 指数叠加。
 *
 * 指数与均分量纲不同（指数 4000 点、均分 0.0x），直接同轴会画成一条贴地线；
 * 统一按**窗口首个有效日 = 100** 归一，只比走势，不改均分口径。
 */

export interface RegimeIndexPoint {
  trade_date: string;
  /** 指数实际取数日（信号日 T + offset；与 trade_date 不同时用于 tooltip 说明） */
  index_date?: string;
  close: number;
}

export interface RegimeIndexSeries {
  code: string;
  name: string;
  points: RegimeIndexPoint[];
}

export interface RegimeIndexOption {
  code: string;
  name: string;
}

/** A 股常用指数色（与 K 线/图表语义区分：指数线不用红绿，红绿留给涨跌） */
export const INDEX_COLORS: Record<string, string> = {
  '000300.SH': '#f59e0b',
  '000905.SH': '#8b5cf6',
  '000852.SH': '#ec4899',
  '000001.SH': '#10b981',
  '399001.SZ': '#06b6d4',
};

export const FALLBACK_INDEX_OPTIONS: RegimeIndexOption[] = [
  { code: '000300.SH', name: '沪深300' },
  { code: '000905.SH', name: '中证500（沪深500）' },
  { code: '000852.SH', name: '中证1000（沪深1000）' },
  { code: '000001.SH', name: '上证综指（沪市）' },
  { code: '399001.SZ', name: '深证成指（深市）' },
];

/**
 * 指数收盘 → 以窗口首个有效日为 100 的归一序列；缺失日保持 null（connectNulls）。
 */
export function rebaseIndexPoints(
  points: RegimeIndexPoint[],
  dates: string[],
): Array<number | null> {
  const byDate = new Map(points.map((p) => [String(p.trade_date).slice(0, 10), Number(p.close)]));
  let base: number | null = null;
  for (const d of dates) {
    const v = byDate.get(d);
    if (v != null && Number.isFinite(v) && v > 0) {
      base = v;
      break;
    }
  }
  return dates.map((d) => {
    const v = byDate.get(d);
    if (base == null || v == null || !Number.isFinite(v) || v <= 0) return null;
    return Number(((v / base) * 100).toFixed(2));
  });
}
