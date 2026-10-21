import { describe, expect, it } from 'vitest';
import type { EvalRunStatus, EvalScoreRow } from '../../types/evalCenter';
import {
  averageScore,
  coverageNote,
  dimensionViews,
  filterEvalRows,
  formatNumber,
  formatPercent,
  formatSignedPct,
  gradeColor,
  gradeCounts,
  gradeMeta,
  historySeries,
  parseObjectDeepLink,
  rowLabels,
  runStatusView,
} from '../evalCenterModel';

function makeRow(overrides: Partial<EvalScoreRow> = {}): EvalScoreRow {
  return {
    object_type: 'model',
    object_id: 'model_qlib',
    snapshot_date: '2026-09-16',
    score: 72,
    grade: 'B',
    low_confidence: false,
    red_line_failed: [],
    dimensions: {},
    inputs_version: {},
    created_at: '2026-09-16T13:00:00',
    ...overrides,
  };
}

describe('gradeMeta 评级徽章', () => {
  it('A/B/C/D 各自映射标签与样式，低置信 † 单独标注', () => {
    expect(gradeMeta('A').label).toContain('A');
    expect(gradeMeta('A').className).toContain('red');
    expect(gradeMeta('D').className).toContain('slate');
    expect(gradeMeta('C', true).isLowConfidence).toBe(true);
    expect(gradeMeta('C†').isLowConfidence).toBe(true);
    expect(gradeMeta('L').label).toContain('运气');
    expect(gradeMeta('E').label).toContain('证据不足');
  });

  it('空评级回退"未评级"而不抛错', () => {
    expect(gradeMeta(null).label).toBe('未评级');
    expect(gradeMeta(undefined).className).toContain('slate');
  });
});

describe('dimensionViews 维度明细', () => {
  it('区分可评/缺省/红线，缺省携带 note 如实展示', () => {
    const row = makeRow({
      dimensions: {
        predictive: { label: '预测力', score: 73.3, weight: 30 },
        quality_gate: {
          label: '质量闸门',
          score: null,
          weight: 20,
          detail: { insufficient: true, note: 'PFS/DH 未落库' },
        },
        risk: {
          label: '风险',
          score: 20,
          weight: 20,
          red_line_failed: true,
          detail: { red_line: 'MDD ≤ -50%' },
        },
      },
    });
    const views = dimensionViews(row);
    expect(views).toHaveLength(3);
    const byKey = Object.fromEntries(views.map((v) => [v.key, v]));
    expect(byKey.predictive.score).toBe(73.3);
    expect(byKey.quality_gate.score).toBeNull();
    expect(byKey.quality_gate.note).toContain('PFS/DH');
    expect(byKey.risk.redLine).toBe(true);
    expect(byKey.risk.note).toContain('MDD');

    expect(dimensionViews(null)).toEqual([]);
  });
});

describe('historySeries 历史序列', () => {
  it('历史序列按输入顺序取日期/分数/评级', () => {
    const series = historySeries([
      makeRow({ snapshot_date: '2026-09-15', score: 60, grade: 'C' }),
      makeRow({ snapshot_date: '2026-09-16', score: null, grade: 'D' }),
    ]);
    expect(series.dates).toEqual(['2026-09-15', '2026-09-16']);
    expect(series.scores).toEqual([60, null]);
    expect(series.grades).toEqual(['C', 'D']);
    expect(historySeries(null).dates).toEqual([]);
  });
});

describe('parseObjectDeepLink（评估徽章深链参数）', () => {
  const params = (s: string) => new URLSearchParams(s);

  it('object 与 object_id 都齐才认', () => {
    expect(parseObjectDeepLink(params('object=model&object_id=abc'))).toEqual({
      objectType: 'model',
      objectId: 'abc',
    });
  });

  it('缺一个 / 空白一律 null（不半跳：类型对、id 空会落到榜单第一行，比不跳更误导）', () => {
    expect(parseObjectDeepLink(params('object=model'))).toBeNull();
    expect(parseObjectDeepLink(params('object_id=abc'))).toBeNull();
    expect(parseObjectDeepLink(params('object=%20&object_id=%20'))).toBeNull();
    expect(parseObjectDeepLink(params(''))).toBeNull();
  });
});

describe('filterEvalRows（榜单客户端搜索）', () => {
  const rows: EvalScoreRow[] = [
    makeRow({ object_id: 'mdl_nativetft_2026', display_name: 'NativeTFT 主模型' }),
    makeRow({ object_type: 'strategy', object_id: '17', display_name: null }),
    makeRow({ object_id: 'f13_0001', display_name: null }),
  ];

  it('空查询原样返回（同一引用，便于调用方 memo）', () => {
    expect(filterEvalRows(rows, '  ')).toBe(rows);
    expect(filterEvalRows(null, 'x')).toEqual([]);
  });

  it('命中 ID / 展示名（大小写不敏感）', () => {
    expect(filterEvalRows(rows, 'NATIVETFT').map((r) => r.object_id)).toEqual(['mdl_nativetft_2026']);
    expect(filterEvalRows(rows, 'f13').map((r) => r.object_id)).toEqual(['f13_0001']);
  });

  it('无展示名时按行内人话名（rowLabels.primary）也能搜到', () => {
    // 策略行没有 display_name → rowLabels 兜底「策略回测 17…」
    expect(filterEvalRows(rows, '策略回测').map((r) => r.object_id)).toEqual(['17']);
  });

  it('无命中返回空数组（调用方出「没有匹配」空态）', () => {
    expect(filterEvalRows(rows, 'zzz_不存在')).toEqual([]);
  });
});

describe('gradeColor / gradeCounts / averageScore / rowLabels（2026-09-17 改版）', () => {
  it('gradeColor：A 红 B 蓝 C 橙 D 灰（A股口径），† 归并同档，未知回退', () => {
    expect(gradeColor('A')).toBe('#dc2626');
    expect(gradeColor('b')).toBe('#2563eb');
    expect(gradeColor('C†')).toBe('#ea580c');
    expect(gradeColor('D')).toBe('#64748b');
    expect(gradeColor(null)).toBe('#6366f1');
  });

  it('gradeCounts：按 A/B/C/D/L/E 固定顺序计数，未评级归 ?，零值不出现', () => {
    const rows = [
      makeRow({ grade: 'A' }),
      makeRow({ grade: 'A' }),
      makeRow({ grade: 'B†' }),
      makeRow({ grade: 'L' }),
      makeRow({ grade: null }),
    ];
    expect(gradeCounts(rows)).toEqual([
      { grade: 'A', count: 2 },
      { grade: 'B', count: 1 },
      { grade: 'L', count: 1 },
      { grade: '?', count: 1 },
    ]);
    expect(gradeCounts(null)).toEqual([]);
  });

  it('averageScore：只统计有效分数，一位小数；无 → null', () => {
    expect(averageScore([makeRow({ score: 60 }), makeRow({ score: 80.25 }), makeRow({ score: null })])).toBe(70.1);
    expect(averageScore([])).toBeNull();
    expect(averageScore(null)).toBeNull();
  });

  it('rowLabels：display_name 优先并保留原 id 副标题；无名字按类型兜底', () => {
    const named = rowLabels(makeRow({ object_type: 'factor', object_id: 'a158_LOW0', display_name: '最低相对收盘' }));
    expect(named).toEqual({ primary: '最低相对收盘', secondary: 'a158_LOW0' });

    const strat = rowLabels(makeRow({ object_type: 'strategy', object_id: '93085758a7284ad3ba447a6a9c9ace21', display_name: null }));
    expect(strat.primary).toBe('策略回测 93085758…');
    expect(strat.secondary).toBe('93085758a7284ad3ba447a6a9c9ace21');

    const daily = rowLabels(makeRow({ object_type: 'daily_selection', object_id: '2026-09-15', display_name: null }));
    expect(daily).toEqual({ primary: '2026-09-15', secondary: null });

    // 体检留档：后端解析出策略名时用名；解析不到时兜底「策略 #id」（裸数字看不懂）
    const healthNamed = rowLabels(
      makeRow({ object_type: 'strategy_health', object_id: '32', display_name: 'minibt·HullMA 双周期' })
    );
    expect(healthNamed).toEqual({ primary: 'minibt·HullMA 双周期', secondary: '32' });

    const healthBare = rowLabels(makeRow({ object_type: 'strategy_health', object_id: '32', display_name: null }));
    expect(healthBare).toEqual({ primary: '策略 #32', secondary: null });
  });
});

describe('数值格式化（实测量与曲线标注共用口径）', () => {
  it('formatNumber：按位数四舍五入，缺值给「—」而不是 0', () => {
    expect(formatNumber(0.0412, 4)).toBe('0.0412');
    expect(formatNumber(0.04125, 4)).toBe('0.0413');
    expect(formatNumber(-1.12, 2)).toBe('-1.12');
    expect(formatNumber(null)).toBe('—');
    expect(formatNumber(undefined)).toBe('—');
    expect(formatNumber(Number.NaN)).toBe('—');
  });

  it('formatPercent：小数 → 百分数（默认带符号，正数带 +）', () => {
    expect(formatPercent(0.724, 1)).toBe('72.4%');
    expect(formatPercent(0.625, 1)).toBe('62.5%');
    expect(formatPercent(0.66, 0)).toBe('66%');
    expect(formatPercent(null)).toBe('—');
  });

  it('formatSignedPct：收益率带正负号（A 股口径：正=涨/红）', () => {
    expect(formatSignedPct(0.183)).toBe('+18.3%');
    expect(formatSignedPct(-0.081)).toBe('-8.1%');
    expect(formatSignedPct(0)).toBe('0.0%');
    expect(formatSignedPct(null)).toBe('—');
  });
});

describe('coverageNote 本轮覆盖说明', () => {
  it('策略：达上限时明确说明未覆盖全部候选', () => {
    const note = coverageNote([
      makeRow({
        object_type: 'strategy',
        inputs_version: {
          coverage: {
            candidates: 80,
            scored: 20,
            skipped: 60,
            limit: 20,
            candidate_limit: 80,
            cap_reached: true,
          },
        },
      }),
    ]);
    expect(note).toContain('候选 80 条');
    expect(note).toContain('评分 20 条');
    expect(note).toContain('跳过 60 条');
    expect(note).toContain('已达上限 20 条');
  });

  it('策略：候选池已跑完时不谎称触顶；无跳过不显示跳过', () => {
    const note = coverageNote([
      makeRow({
        object_type: 'strategy',
        inputs_version: {
          coverage: { candidates: 7, scored: 7, skipped: 0, limit: 20, cap_reached: false },
        },
      }),
    ]);
    expect(note).toContain('候选 7 条');
    expect(note).toContain('候选池已跑完');
    expect(note).not.toContain('跳过');
    expect(note).not.toContain('已达上限');
  });

  it('因子：单因子 / 全量 / 按 |IC| 前 N 三种口径各自成句', () => {
    const explicit = coverageNote([
      makeRow({
        object_type: 'factor',
        inputs_version: {
          coverage: { selection: 'explicit', scored: 1, total: 1, all: true },
        },
      }),
    ]);
    expect(explicit).toBe('本轮为单因子评分（1/1）');

    const all = coverageNote([
      makeRow({
        object_type: 'factor',
        inputs_version: {
          coverage: { selection: 'top_abs_mean_ic', scored: 429, total: 429, all: true },
        },
      }),
    ]);
    expect(all).toBe('本轮全量评分 429 / 429 个因子');

    const top = coverageNote([
      makeRow({
        object_type: 'factor',
        inputs_version: {
          coverage: {
            selection: 'top_abs_mean_ic',
            scored: 20,
            total: 429,
            all: false,
            requested_top: 20,
          },
        },
      }),
    ]);
    expect(top).toContain('前 20 / 共 429 个因子');
    expect(top).toContain('上限 20');
    expect(top).toContain('--factor-top 0');
  });

  it('缺 coverage / 结构不完整 / 未知类型 → null（不猜 0/0）', () => {
    expect(coverageNote([])).toBeNull();
    expect(coverageNote([makeRow()])).toBeNull();
    expect(
      coverageNote([
        makeRow({ object_type: 'strategy', inputs_version: { coverage: { scored: 5 } } }),
      ])
    ).toBeNull();
    expect(
      coverageNote([
        makeRow({ object_type: 'model', inputs_version: { coverage: { scored: 5, total: 5 } } }),
      ])
    ).toBeNull();
  });
});

describe('runStatusView 手动重评状态', () => {
  function makeStatus(overrides: Partial<EvalRunStatus> = {}): EvalRunStatus {
    return {
      state: 'queued',
      request_id: 'r1',
      date: null,
      trigger: 'manual',
      requested_by: '1',
      requested_at: '2026-10-21T08:00:00+00:00',
      started_at: null,
      finished_at: null,
      total_scored: null,
      total_errors: null,
      errors: [],
      backfill: null,
      elapsed_sec: null,
      note: '',
      ...overrides,
    };
  }

  it('idle / null 不渲染状态条', () => {
    expect(runStatusView(null)).toBeNull();
    expect(runStatusView(makeStatus({ state: 'idle' }))).toBeNull();
  });

  it('queued / running 保持轮询并显示等待文案', () => {
    const queued = runStatusView(makeStatus({ state: 'queued' }));
    expect(queued?.active).toBe(true);
    expect(queued?.label).toBe('排队中');
    const running = runStatusView(makeStatus({ state: 'running' }));
    expect(running?.active).toBe(true);
    expect(running?.label).toBe('评分中');
  });

  it('done 汇总落分/异常/回填且停止轮询', () => {
    const view = runStatusView(
      makeStatus({
        state: 'done',
        total_scored: 42,
        total_errors: 1,
        backfill: { rerun: 3 },
      })
    );
    expect(view?.active).toBe(false);
    expect(view?.tone).toBe('done');
    expect(view?.detail).toContain('落分 42');
    expect(view?.detail).toContain('异常 1');
    expect(view?.detail).toContain('回填 3 条');
  });

  it('error 优先显示首个错误，缺错误时回落 note', () => {
    const withError = runStatusView(
      makeStatus({ state: 'error', errors: ['RuntimeError: boom'] })
    );
    expect(withError?.tone).toBe('error');
    expect(withError?.detail).toBe('RuntimeError: boom');

    const fallback = runStatusView(makeStatus({ state: 'error', note: '回填异常' }));
    expect(fallback?.detail).toBe('回填异常');
  });
});
