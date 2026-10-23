/**
 * 持仓监控渲染层：缺价行不得被画成「亏光」。
 *
 * 事故（2026-09-22 实盘持仓监控浮动盈亏 −84,778.90）：后端补价失败，落库的持仓行
 * 是 `price: 0, market_value: 0`。`positionMetrics` 那一层已把这类行标成
 * `priceMissing` 且不参与加减，但**合计是在本组件里做的** ——
 * `holdings.reduce((s, h) => s + h.profit, 0)` 只要还在用全量 holdings，
 * 十行「市值 0 − 成本」照样能凑出那串数字。utils 侧的绿测拦不住这个：
 * 它测的是 metrics，测不到这个 reduce。
 *
 * 同一条口径在本仓已有明文：研究评分「缺失一律 `—`，绝不显示成 0」。
 */

import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, screen, cleanup, fireEvent, waitFor, within } from '@testing-library/react';

import { PositionVisualBoard, ExecutionStrip } from '../PositionVisualBoard';
import type { NormalizedHolding, PositionSummary } from '../../utils/positionMetrics';

vi.mock('echarts-for-react', () => ({
  default: () => <div data-testid="echarts-mock" />,
}));

// 卖出预检面板会拉起 stock-terminal 的整条依赖；本文件只测展示口径
vi.mock('../../../../features/stock-terminal/components/PushConfirmPanel', () => ({
  PushConfirmPanel: () => null,
}));

// ExecutionStrip 自取 `/api/v1/desk/today`（health:false, plan:false）
const deskToday = vi.fn();
vi.mock('../../../../features/desk/services/deskService', () => ({
  getDeskToday: (...args: unknown[]) => deskToday(...args),
}));

afterEach(cleanup);

beforeEach(() => {
  window.localStorage.clear(); // 分析与密度偏好跨用例隔离
});

/** 明细行：货架马赛克块与「卖出」按钮的 title 也带股票名，只有明细行的 title
 *  是 `名称（代码） 持仓 N 股 · 市值 …` 这个形状（马赛克块在 `）` 后换行）。 */
const rowOf = (name: string) =>
  screen.getByTitle(new RegExp(`^${name}（[^）]+） 持仓 \\d+ 股 · 市值`));

/** 补价失败的行：有股数、有成本价，现价与市值为 0（后端原样落库） */
const missing = (over: Partial<NormalizedHolding> = {}): NormalizedHolding => ({
  code: 'SZ000028',
  name: '国药一致',
  shares: 200,
  cost: 19.886,
  current: 0,
  profit: 0,
  profitPercent: 0,
  value: 0,
  priceMissing: true,
  ...over,
});

const priced = (over: Partial<NormalizedHolding> = {}): NormalizedHolding => ({
  code: 'SH600036',
  name: '招商银行',
  shares: 200,
  cost: 19.886,
  current: 20.03,
  profit: 28.8,
  profitPercent: 0.724,
  value: 4006,
  priceMissing: false,
  ...over,
});

const summaryOf = (positionValue: number): PositionSummary => ({
  totalAsset: 920498.86,
  cashValue: 834408.86,
  positionValue,
  positionRatio: 9.35,
  cashRatio: 90.65,
});

describe('PositionVisualBoard · 缺价行', () => {
  it('全部缺价时浮动盈亏出 —，既不报 0.00 也不报负数', () => {
    render(<PositionVisualBoard holdings={[missing()]} summary={summaryOf(0)} />);

    const kpi = screen.getByText('浮动盈亏');

    expect(kpi).toHaveTextContent('浮动盈亏 —');
    expect(kpi).not.toHaveTextContent('0.00');
    // 旧口径：市值 0 − 成本 19.886×200 = −3,977.20，十行就是用户看到的那串数字
    expect(kpi).not.toHaveTextContent('-');
  });

  it('缺价行不计入盈利/亏损家数，另立「缺价」计数', () => {
    render(
      <PositionVisualBoard holdings={[missing(), priced()]} summary={summaryOf(4006)} />,
    );

    expect(screen.getByText('盈利')).toHaveTextContent('盈利 1');
    expect(screen.getByText('亏损')).toHaveTextContent('亏损 0');
    expect(screen.getByText('缺价')).toHaveTextContent('缺价 1');
  });

  it('浮动盈亏只合计可定价的行，缺价行不参与', () => {
    render(
      <PositionVisualBoard holdings={[missing(), priced()]} summary={summaryOf(4006)} />,
    );

    const kpi = screen.getByText('浮动盈亏');

    expect(kpi).toHaveTextContent('浮动盈亏 +28.80');
  });

  it('缺价行：现价 / 市值 / 盈亏一律出 —，成本照常显示', () => {
    render(<PositionVisualBoard holdings={[missing()]} summary={summaryOf(0)} />);

    const row = rowOf('国药一致');

    expect(row).toHaveTextContent('成本 19.89');
    expect(row).toHaveTextContent('缺现价');
    // 缺价三要素逐格核对（新增列后总数不再是 3，按单元格断言更精确）
    expect(within(row).getByTestId('cell-price')).toHaveTextContent('—');
    expect(within(row).getByTestId('cell-market-value')).toHaveTextContent('—');
    expect(within(row).getByTestId('cell-profit')).toHaveTextContent('—');
    // 旧口径的两个特征：现价画成 ¥0.00、盈亏画成 −3,977.20 与 −100.00%
    expect(row).not.toHaveTextContent('0.00');
    expect(row).not.toHaveTextContent('100.00%');
    expect(row).not.toHaveTextContent('-3,977.20');
  });

  it('可定价的行不受影响，照常出价与盈亏', () => {
    render(<PositionVisualBoard holdings={[priced()]} summary={summaryOf(4006)} />);

    const row = rowOf('招商银行');

    expect(row).toHaveTextContent('20.03');
    expect(row).toHaveTextContent('+28.80');
    expect(row).toHaveTextContent('+0.72%');
    expect(screen.queryByText('缺价')).toBeNull();
  });
});

/**
 * 2026-10-23 明细优先重设计：SIM 账户 78 只持仓，旧版首屏只能看 5–7 行。
 * 这些用例钉住三件事：虚拟列表真的渲染多行、分析图表默认不占明细高度、
 * 右栏可折叠（折叠后明细拿满整行）。
 */
describe('PositionVisualBoard · 明细优先布局', () => {
  const many = (n: number): NormalizedHolding[] =>
    Array.from({ length: n }, (_, i) =>
      priced({
        code: `SH60${String(i).padStart(4, '0')}`,
        name: `股票${i}`,
        shares: 100 * (i + 1),
        value: 100000 - i * 100,
        profit: i - 5,
        profitPercent: i / 10,
      }),
    );

  it('虚拟滚动首屏渲染多行（不再只显示 3–5 行）', () => {
    render(<PositionVisualBoard holdings={many(80)} summary={summaryOf(8000000)} />);

    expect(screen.getAllByTestId('position-row').length).toBeGreaterThanOrEqual(8);
  });

  it('粘性表头与固定合计行始终在场', () => {
    render(<PositionVisualBoard holdings={many(20)} summary={summaryOf(2000000)} />);

    expect(screen.getByTestId('position-table-header')).toBeTruthy();
    expect(screen.getByTestId('position-total-row')).toHaveTextContent('总盈亏');
  });

  it('分析图表默认折叠（不占明细高度），点开才渲染', () => {
    render(<PositionVisualBoard holdings={many(10)} summary={summaryOf(1000000)} />);

    const drawer = screen.getByTestId('position-insights');
    expect(drawer.getAttribute('data-open')).toBe('false');
    expect(screen.queryByTestId('echarts-mock')).toBeNull();

    fireEvent.click(within(drawer).getByRole('button'));

    expect(screen.getByTestId('position-insights').getAttribute('data-open')).toBe('true');
    expect(screen.getAllByTestId('echarts-mock').length).toBeGreaterThan(0);
  });

  it('可用量缺失出 —，不拿持仓量冒充', () => {
    render(<PositionVisualBoard holdings={[priced()]} summary={summaryOf(4006)} />);

    expect(within(rowOf('招商银行')).getByTestId('cell-shares')).toHaveTextContent('可用 —');
  });

  it('右栏折叠按钮回调（由 PositionMonitor 注入）', () => {
    const onToggle = vi.fn();
    render(
      <PositionVisualBoard
        holdings={[priced()]}
        summary={summaryOf(4006)}
        railCollapsed={false}
        onToggleRail={onToggle}
      />,
    );

    fireEvent.click(screen.getByLabelText('折叠风险与情报栏'));

    expect(onToggle).toHaveBeenCalledTimes(1);
  });
});

describe('PositionVisualBoard · 现金占比口径', () => {
  // 2026-10-23 实测：getPositionSummary 返回的 cashRatio 已是百分数（2.52 = 2.52%），
  // KPI 旧实现用 fmtPct（×100）并按 >1.5 判异常 → 2.52 被当成 252% 直接显示 —。
  it('cashRatio 百分数直出：2.52 → 2.52%', () => {
    render(
      <PositionVisualBoard
        holdings={[priced()]}
        summary={{ ...summaryOf(4006), cashRatio: 2.52 }}
      />,
    );

    expect(screen.getByText('现金占比')).toHaveTextContent('2.52%');
  });

  it('口径异常（>150%）出 — 并说明原因', () => {
    render(
      <PositionVisualBoard
        holdings={[priced()]}
        summary={{ ...summaryOf(4006), cashRatio: 200 }}
      />,
    );

    const pill = screen.getByText('现金占比');
    expect(pill).toHaveTextContent('—');
    expect(pill.getAttribute('title')).toContain('账户口径数据异常');
  });
});

/**
 * 执行条「取价来源」可见面（2026-10-14 自 `DeskCards.ExecutionCard` 移植）。
 *
 * 背景：原渲染点是交易台的 `ExecutionCard`，该卡自 2026-09-17 起已无活引用（死代码），
 * 而它是前端**唯一**解释 `price_source` 降级标记的地方——直接删除会让
 * 「这单用的是陈旧价 / 非当日收盘」这个降级事实从界面上消失（后端 desk.py 仍在投递该字段）。
 * 故先把 tooltip 落到活的 `ExecutionStrip`，再删死卡。本用例钉住移植后的行为。
 */
describe('ExecutionStrip · 取价来源 tooltip', () => {
  afterEach(() => {
    deskToday.mockReset();
  });

  const stripOf = (items: unknown[]) => {
    deskToday.mockResolvedValue({ success: true, data: { execution: { source: 'db:sim_orders+orders', items } } });
    render(<ExecutionStrip />);
  };

  it('price_source 有值时出「取价来源：<人话>」title，缺失时不挂 title', async () => {
    stripOf([
      { mode: 'SIM', symbol: 'SH600036', side: 'BUY', quantity: 100, status: 'FILLED', price_source: 'prev_close_bar' },
      { mode: 'SIM', symbol: 'SZ000001', side: 'SELL', quantity: 200, status: 'REJECTED' },
    ]);

    fireEvent.click(await screen.findByText('今日执行'));

    const degraded = await screen.findByTitle(/^取价来源：/);
    expect(degraded).toHaveTextContent('SH600036');
    // 降级标记必须解释成「非当日」——只印枚举值等于没说
    expect(degraded.getAttribute('title')).toContain('降级');
    expect(degraded.getAttribute('title')).toContain('最近可用日线收盘');

    await waitFor(() => expect(screen.getByText('SZ000001')).toBeTruthy());
    expect(screen.getByText('SZ000001').getAttribute('title')).toBeNull();
  });

  it('price_source 为空时不挂 title（与移植前 ExecutionCard 的判据一致）', async () => {
    stripOf([
      { mode: 'SIM', symbol: 'SH601988', side: 'BUY', quantity: 100, status: 'FILLED', price_source: null },
    ]);

    fireEvent.click(await screen.findByText('今日执行'));

    // 判据是 `it.price_source ? title : undefined`：没有标记就不编一个来源出来
    const span = await screen.findByText('SH601988');
    expect(span.getAttribute('title')).toBeNull();
  });
});
