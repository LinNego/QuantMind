/**
 * 滚动建仓 · 每日批次口径的机构级指标条（纯数据，不含 JSX）。
 *
 * 口径：入选日收盘按全部权益等权建仓 → 次一交易日收盘结算；最后一批只估值
 * 不结算（计入浮动）。`realized + unrealized == final_equity − initial_capital`。
 * 胜率/盈亏比只统计**已结算批次**，未结算批次不得提前计入。
 */

import type { BlendRollingPerformance } from '../../../../services/blendTypes';
import { formatAmount, formatReturnPct, returnToneClass } from './comboRankingModel';
import { formatPct, formatSignedAmount, formatWinRate } from './periodReturnModel';
import { formatDays, formatMultiple, formatRatio } from './portfolioMetricsModel';
import type { MetricItem, MetricSection } from './PortfolioMetricsStrip';

function benchmarkHint(performance: BlendRollingPerformance): string {
  const coverage = performance.benchmark_coverage_pct;
  if (typeof coverage === 'number' && Number.isFinite(coverage)) {
    return `沪深300 按日收益复利；区间覆盖率 ${coverage.toFixed(0)}%（<95% 时基准与超额置 —）`;
  }
  return '沪深300 行情不可用：基准与超额一律显示 —，不写 0';
}

function amountWithPct(amount: number, pct: number | null | undefined): string {
  return `${formatSignedAmount(amount)} · ${formatPct(pct)}`;
}

export function buildRollingMetricSections(
  performance: BlendRollingPerformance,
): MetricSection[] {
  const returns: MetricItem[] = [
    {
      key: 'total',
      label: '窗口收益',
      value: formatReturnPct(performance.total_return_pct),
      tone: returnToneClass(performance.total_return_pct),
      emphasis: 'hero',
      testId: 'combo-rolling-total-return',
      hint: '期末权益 ÷ 初始资金 − 1（含已实现与浮动）；每日收盘等权换仓',
    },
    {
      key: 'profit',
      label: '收益金额',
      value: formatSignedAmount(performance.profit_amount),
      tone: returnToneClass(performance.profit_amount),
      testId: 'combo-rolling-profit',
    },
    {
      key: 'equity',
      label: '期末权益',
      value: formatAmount(performance.final_equity),
      hint: `初始资金 ${formatAmount(performance.initial_capital)}`,
    },
    {
      key: 'realized',
      label: '已实现',
      value: amountWithPct(performance.realized_pnl_amount, performance.realized_return_pct),
      tone: returnToneClass(performance.realized_pnl_amount),
      testId: 'combo-rolling-realized',
      hint: '除最后一批外，每日批次已在次一交易日收盘结算；收益率 = 损益 ÷ 初始资金',
    },
    {
      key: 'unrealized',
      label: '浮动',
      value: amountWithPct(
        performance.unrealized_pnl_amount,
        performance.unrealized_return_pct,
      ),
      tone: returnToneClass(performance.unrealized_pnl_amount),
      testId: 'combo-rolling-unrealized',
      hint: '最后一个批次只在末日收盘估值，未结算；末日入选不建仓',
    },
    {
      key: 'benchmark',
      label: '基准',
      value: formatReturnPct(performance.benchmark_return_pct),
      tone: returnToneClass(performance.benchmark_return_pct),
      hint: benchmarkHint(performance),
      testId: 'combo-rolling-benchmark',
    },
    {
      key: 'excess',
      label: '超额',
      value: formatReturnPct(performance.excess_return_pct),
      tone: returnToneClass(performance.excess_return_pct),
      hint: '窗口收益 − 基准收益；基准不可用时显示 —',
      testId: 'combo-rolling-excess',
    },
  ];

  const risk: MetricItem[] = [
    {
      key: 'annualized',
      label: '年化',
      value: formatPct(performance.annualized_return_pct),
      tone: returnToneClass(performance.annualized_return_pct),
      hint: '按 244 个交易日/年折算；窗口较短时仅供横向参考',
    },
    { key: 'volatility', label: '波动率', value: formatPct(performance.volatility_pct) },
    { key: 'sharpe', label: '夏普', value: formatRatio(performance.sharpe_ratio) },
    { key: 'sortino', label: '索提诺', value: formatRatio(performance.sortino_ratio) },
    { key: 'calmar', label: '卡玛', value: formatRatio(performance.calmar_ratio) },
    {
      key: 'drawdown',
      label: '最大回撤',
      value: formatPct(performance.max_drawdown_pct),
      tone: returnToneClass(performance.max_drawdown_pct),
      hint: '逐日权益峰值到谷值的最大跌幅',
      testId: 'combo-rolling-drawdown',
    },
    {
      key: 'tracking',
      label: '跟踪误差',
      value: formatPct(performance.tracking_error_pct),
      hint: '组合与基准日超额收益的年化标准差；基准不可用时 —',
    },
    {
      key: 'ir',
      label: '信息比率',
      value: formatRatio(performance.information_ratio),
      hint: `年化超额收益 ÷ 跟踪误差；日均超额 ${formatPct(performance.avg_daily_excess_pct)}`,
    },
  ];

  const batches: MetricItem[] = [
    {
      key: 'counts',
      label: '批次',
      value: `${performance.batches}（结算 ${performance.settled_batches} · 浮动 ${performance.open_batches}）`,
      hint: `已计价 ${performance.priced_batches} · 缺价 ${performance.unpriced_batches} · 部分缺价 ${performance.partial_batches}`,
      testId: 'combo-rolling-batches',
    },
    {
      key: 'win',
      label: '批次胜率',
      value: formatWinRate(performance.win_rate_pct),
      hint: `盈 ${performance.wins} / 亏 ${performance.losses} / 平 ${performance.flats}（仅已结算批次）`,
    },
    {
      key: 'profit_factor',
      label: '盈利因子',
      value: formatRatio(performance.profit_factor),
      hint: `总盈利 ${formatSignedAmount(performance.gross_profit_amount)} ÷ |总亏损 ${formatSignedAmount(performance.gross_loss_amount)}|`,
    },
    {
      key: 'payoff',
      label: '盈亏比',
      value: formatRatio(performance.payoff_ratio),
      hint: `平均盈利 ${formatPct(performance.avg_win_pct)} ÷ |平均亏损 ${formatPct(performance.avg_loss_pct)}|`,
    },
    { key: 'avg', label: '平均批次收益', value: formatReturnPct(performance.avg_return_pct) },
    {
      key: 'holding',
      label: '平均持有',
      value: formatDays(performance.avg_holding_days),
      hint: '每日换仓，批次持有 1 个交易日',
    },
    {
      key: 'turnover',
      label: '换手',
      value: formatMultiple(performance.turnover_x),
      hint: '每日建仓名义金额合计 ÷ 初始资金',
    },
    {
      key: 'exposure',
      label: '平均仓位',
      value: formatPct(performance.avg_exposure_pct),
      hint: '有价批次为满仓 100%，缺价批次保持现金',
    },
    {
      key: 'cash',
      label: '期末现金',
      value: formatPct(performance.final_cash_pct),
    },
  ];

  return [
    { key: 'returns', title: '收益', items: returns },
    { key: 'risk', title: '风险', items: risk },
    { key: 'batches', title: '批次', items: batches },
  ];
}
