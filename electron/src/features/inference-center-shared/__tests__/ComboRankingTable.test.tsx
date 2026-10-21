/**
 * 当日榜单：历史标注、模板预览提示、CSV 导出与展开贡献。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import type { BlendRankingResponse } from '../../../services/blendTypes';
import { ComboRankingTable } from '../components/combo/ComboRankingTable';

function ranking(): BlendRankingResponse {
  return {
    data_trade_date: '2026-10-20',
    members: [
      { model_id: 'm1', display_name: 'LGB', model_source: 'manual', model_type: 'lightgbm', horizon_days: 10, weight: 1, available: true },
      { model_id: 'm2', display_name: 'XGB', model_source: 'manual', model_type: 'xgboost', horizon_days: 10, weight: 1, available: true },
    ],
    stats: { dispersion: { low: 1, medium: 0, high: 0, unknown: 0 } },
    warnings: [],
    ranking: [
      {
        code: 'SH600000', symbol: 'SH600000', name: '浦发银行', S: 0.9, S_pct: 0.95, score: 0.9, pct: 0.95,
        rank: 1, research_score: 95, position: 'BUY', coverage: 2, dispersion: 0.01,
        dispersion_band: 'low',
        contributions: { m1: 0.8, m2: 0.6 },
        missing_members: ['m2'],
        history: {
          first_entry_date: '2026-10-14', last_entry_date: '2026-10-20',
          reentry_count: 1, held_days: 4, consecutive_days_in_top50: 4,
        },
        industry: '银行',
        market_cap_yi: 3200.5,
      },
      {
        code: 'SH600001', symbol: 'SH600001', name: '无历史', S: 0.5, S_pct: 0.6, score: 0.5, pct: 0.6,
        rank: 2, research_score: null, position: 'SELL', coverage: 1, dispersion: null,
        dispersion_band: 'unknown', contributions: {}, missing_members: [],
      },
    ],
  };
}

const baseProps = {
  loading: false,
  error: null,
  onRetry: vi.fn(),
  blendName: '全13·T+10 等权',
  holdDays: 10,
  isTemplate: false,
};

describe('ComboRankingTable', () => {
  beforeEach(() => {
    Object.defineProperty(URL, 'createObjectURL', { writable: true, value: vi.fn(() => 'blob:mock') });
    Object.defineProperty(URL, 'revokeObjectURL', { writable: true, value: vi.fn() });
  });
  afterEach(() => vi.restoreAllMocks());

  it('表头统计与行内容齐全，历史标注带持有分母', () => {
    render(<ComboRankingTable {...baseProps} ranking={ranking()} />);
    expect(screen.getByText('浦发银行')).toBeInTheDocument();
    expect(screen.getByText('持有 4/10 日 · 连续在榜 4 日 · 再入选 1 次')).toBeInTheDocument();
    expect(screen.getAllByText('95.0').length).toBeGreaterThan(0);
    expect(screen.getAllByText('2/2').length).toBeGreaterThan(0);
    expect(screen.getByText('银行')).toBeInTheDocument();
    expect(screen.getByText('3200.5')).toBeInTheDocument();
    expect(screen.getByText(/数据日 2026-10-20/)).toBeInTheDocument();
  });

  it('缺失历史/分数只显示 —，不显示 0', () => {
    render(<ComboRankingTable {...baseProps} ranking={ranking()} holdDays={null} />);
    const missingRow = screen.getByText('无历史').closest('tr')!;
    expect(missingRow.textContent).toContain('—');
    expect(missingRow.textContent).not.toContain('持有 0');
  });

  it('合规：行内只有相对位置词', () => {
    const { container } = render(<ComboRankingTable {...baseProps} ranking={ranking()} />);
    expect(container.textContent).toContain('靠前');
    expect(container.textContent).toContain('靠后');
    expect(container.textContent).not.toMatch(/买入|卖出|BUY|SELL/);
  });

  it('模板预览明确提示历史标注要保存后才启用', () => {
    render(<ComboRankingTable {...baseProps} ranking={ranking()} isTemplate />);
    expect(screen.getByText('模板预览 · 保存后启用历史标注')).toBeInTheDocument();
  });

  it('展开行显示成员贡献与缺失成员', () => {
    const { container } = render(<ComboRankingTable {...baseProps} ranking={ranking()} />);
    fireEvent.click(container.querySelector('.ant-table-row-expand-icon')!);
    expect(screen.getByTestId('combo-contributions')).toBeInTheDocument();
    expect(screen.getByText('成员贡献（分位口径）')).toBeInTheDocument();
    expect(screen.getByText('0.800')).toBeInTheDocument();
    expect(screen.getByText(/缺失成员/)).toBeInTheDocument();
  });

  it('导出名单：下载文件名为组合名+数据日，内容带 BOM 与表头', () => {
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
    // FileReader 会吞掉 BOM，直接截获传给 Blob 的内容
    let captured = '';
    const NativeBlob = globalThis.Blob;
    class CaptureBlob extends NativeBlob {
      constructor(parts: BlobPart[], opts?: BlobPropertyBag) {
        super(parts, opts);
        captured = String(parts[0] ?? '');
      }
    }
    vi.stubGlobal('Blob', CaptureBlob);

    render(<ComboRankingTable {...baseProps} ranking={ranking()} />);
    fireEvent.click(screen.getByRole('button', { name: /导出名单/ }));
    expect(URL.createObjectURL).toHaveBeenCalledTimes(1);
    expect(clickSpy).toHaveBeenCalledTimes(1);
    expect(captured.startsWith('﻿')).toBe(true);
    expect(captured).toContain('排名,代码,名称,综合分,位置,覆盖,分歧,行业,市值(亿)');
    expect(captured).toContain('1,SH600000,浦发银行,95.0,靠前,2/2,低,银行,3200.5');
    vi.unstubAllGlobals();
  });

  it('出错可重试；空榜单给出空文案', () => {
    const onRetry = vi.fn();
    const { rerender } = render(
      <ComboRankingTable {...baseProps} ranking={ranking()} error="读取失败" onRetry={onRetry} />,
    );
    fireEvent.click(screen.getByRole('button', { name: /重\s*试/ }));
    expect(onRetry).toHaveBeenCalledTimes(1);

    rerender(
      <ComboRankingTable
        {...baseProps}
        ranking={{ data_trade_date: '2026-10-20', members: [], ranking: [], warnings: [] }}
      />,
    );
    expect(screen.getByText('该组合当日没有可展示的标的')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /导出名单/ })).toBeDisabled();
  });
});
