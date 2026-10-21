/**
 * 单模型 Top50 历史统计展示层纯函数：告警文案、快捷区间、口径说明与单期备注。
 */

import { describe, expect, it } from 'vitest';
import type {
  SelectionStatsCohort,
  SelectionStatsResponse,
} from '../../../services/selectionStatsTypes';
import {
  cohortNote,
  coverageToneClass,
  selectionConventionLabel,
  selectionHoldModeOf,
  selectionQuickQuery,
  selectionRangeLabel,
  selectionWarningLabel,
} from '../components/crossstats/selectionStatsModel';

const BOUNDS = { start: '2025-02-05', end: '2026-10-19' };

function cohort(over: Partial<SelectionStatsCohort> = {}): SelectionStatsCohort {
  return {
    index: 1,
    signal_date: '2026-10-09',
    entry_date: '2026-10-10',
    exit_date: '2026-10-24',
    mark_date: '2026-10-24',
    exit_kind: 'closed',
    held: 50,
    priced: 50,
    missing_entry: 0,
    missing_exit: 0,
    stale_exit: 0,
    return_pct: 1.2,
    benchmark_return_pct: 0.3,
    excess_return_pct: 0.9,
    equity: 10_120_000,
    partial: false,
    skipped: false,
    ...over,
  };
}

describe('selectionStatsModel', () => {
  it('告警码译成可读文案，未登记码原样显示', () => {
    expect(selectionWarningLabel('stale_exit')).toContain('最后有效收盘价');
    expect(selectionWarningLabel('effective_start_clamped')).toContain('测试集首日');
    expect(selectionWarningLabel('open_cohort')).toContain('未到期');
    expect(selectionWarningLabel('brand_new_code')).toBe('brand_new_code');
  });

  it('快捷区间按测试集全区间回推，无全区间时留空交给后端', () => {
    expect(selectionQuickQuery('all', BOUNDS)).toEqual({
      start: '2025-02-05',
      end: '2026-10-19',
    });
    expect(selectionQuickQuery('3m', BOUNDS)).toEqual({
      start: '2026-07-19',
      end: '2026-10-19',
    });
    expect(selectionQuickQuery('6m', BOUNDS)).toEqual({
      start: '2026-04-19',
      end: '2026-10-19',
    });
    expect(selectionQuickQuery('1y', BOUNDS)).toEqual({
      start: '2025-10-19',
      end: '2026-10-19',
    });
    expect(selectionQuickQuery('1y', null)).toEqual({});
  });

  it('区间说明在请求值被夹取时补出生效区间', () => {
    const clamped = {
      requested: { start: '2025-01-01', end: '2026-12-31' },
      effective: { start: '2025-02-05', end: '2026-10-19' },
    } as unknown as Pick<SelectionStatsResponse, 'requested' | 'effective'>;
    expect(selectionRangeLabel(clamped)).toBe(
      '2025-01-01 ~ 2026-12-31（按可用测试信号日：2025-02-05 ~ 2026-10-19）',
    );

    const plain = {
      requested: { start: null, end: null },
      effective: { start: '2025-02-05', end: '2026-10-19' },
    } as unknown as Pick<SelectionStatsResponse, 'requested' | 'effective'>;
    expect(selectionRangeLabel(plain)).toBe('2025-02-05 ~ 2026-10-19');
  });

  it('换股节奏由后端 cohort_mode 反推', () => {
    expect(selectionHoldModeOf({ params: { cohort_mode: 'daily' } } as any)).toBe('daily');
    expect(selectionHoldModeOf({ params: { cohort_mode: 'period' } } as any)).toBe('period');
  });

  it('口径摘要同时说清入选滞后、持有期与费用边界', () => {
    const label = selectionConventionLabel({
      params: { execution_lag_days: 1 },
      requested: { hold_days: 10 },
    } as any);
    expect(label).toContain('T+1 收盘入选');
    expect(label).toContain('持有 10 个交易日');
    expect(label).toContain('前复权收盘价');
    expect(label).toContain('不计费用与滑点');
  });

  it('单期备注依次标出未到期、跳过与价格缺失', () => {
    expect(cohortNote(cohort())).toBe('—');
    expect(
      cohortNote(
        cohort({ exit_kind: 'open', stale_exit: 1, missing_entry: 2, missing_exit: 3 }),
      ),
    ).toBe('未到期 · 1 只按窗口内最后价估值 · 2 只缺入选价 · 3 只缺退出价');
    expect(cohortNote(cohort({ skipped: true }))).toBe('无有效价格');
  });

  it('报价覆盖档位色：缺失灰、低于 90% 红', () => {
    expect(coverageToneClass(null)).toContain('slate');
    expect(coverageToneClass(99.7)).toContain('slate');
    expect(coverageToneClass(92)).toContain('amber');
    expect(coverageToneClass(80)).toContain('rose');
  });
});
