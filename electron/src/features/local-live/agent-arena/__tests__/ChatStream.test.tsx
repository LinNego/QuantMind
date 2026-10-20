/**
 * 「模型对话」数据缺口横幅 + 合规尾注（审计 C5 / T3-2）的渲染闸门。
 *
 * 背景：桥不可达时分析端（scripts/live_model_analysis.py）会改写提示词（禁止编造
 * 持仓点评）并在日志条目上写 data_gaps=['account_unreachable']；前端必须据此**显式**
 * 挂横幅——若靠正文文本判读，LLM 幻觉出来的「持仓点评」就会被当事实读。
 *
 * 文案断言全部引用 compliance 单一来源，测试里不抄字面量——抄了就等于又开一个来源。
 */

import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import ChatStream from '../arena/components/ChatStream';
import type { LogLine } from '../arena/api/client';
import {
  COMPLIANCE_AI_GENERATED_TEXT,
  COMPLIANCE_HISTORY_TEXT,
  COMPLIANCE_TOOL_BOUNDARY_TEXT,
} from '../../../../components/shared/compliance/ComplianceChrome';

/** 一轮 A 股口径日志（单行含 user+assistant），缺口标记挂在同一行 */
const round = (gaps?: string[]): LogLine => ({
  timestamp: '2026-10-10T09:30:00',
  ...(gaps ? { data_gaps: gaps } : {}),
  new_messages: [
    { role: 'user', content: '【要求】……' },
    { role: 'assistant', content: '总结：新闻面偏多。' },
  ],
});

const agentsWith = (line: LogLine) => [
  { name: 'deepseek-v4-flash', id: 'deepseek-v4-flash', lines: [line] },
];

describe('数据缺口横幅（C5）', () => {
  it('account_unreachable 轮：卡片默认折叠态下横幅也可见', () => {
    render(<ChatStream agents={agentsWith(round(['account_unreachable']))} />);

    const banner = screen.getByRole('alert');
    expect(banner.textContent).toContain('本轮无实盘账户数据');
    expect(banner.textContent).toContain('未点评持仓');
  });

  it('无缺口轮不挂横幅', () => {
    render(<ChatStream agents={agentsWith(round())} />);

    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('未知缺口值不误触发（字段存在 ≠ 本轮桥挂）', () => {
    render(<ChatStream agents={agentsWith(round(['something_else']))} />);

    expect(screen.queryByRole('alert')).toBeNull();
  });
});

describe('合规尾注（AI 生成免责，单一来源文案）', () => {
  it('有分析记录：列表尾部带 AI 生成提示 + 统一免责', () => {
    render(<ChatStream agents={agentsWith(round())} />);

    expect(screen.getByText(COMPLIANCE_AI_GENERATED_TEXT)).toBeInTheDocument();
    expect(screen.getByText(COMPLIANCE_TOOL_BOUNDARY_TEXT)).toBeInTheDocument();
    expect(screen.getByText(COMPLIANCE_HISTORY_TEXT)).toBeInTheDocument();
  });

  it('空态同样出尾注（列表空时也是 AI 展示面）', () => {
    render(<ChatStream agents={[]} />);

    expect(screen.getByText('暂无分析记录')).toBeInTheDocument();
    expect(screen.getByText(COMPLIANCE_AI_GENERATED_TEXT)).toBeInTheDocument();
    expect(screen.getByText(COMPLIANCE_TOOL_BOUNDARY_TEXT)).toBeInTheDocument();
  });
});

describe('总结条不直出机器码 + 卡头日期口径（2026-10-15 排版批）', () => {
  it('纯 JSON 回复：总结条出「本轮 N 项决策」，无原始 JSON 残串', () => {
    // Arrange：agent 现为纯结构化 JSON 输出（无【】/①②标记）的真实形态
    const jsonRound: LogLine = {
      timestamp: '2026-10-15T09:35:26',
      new_messages: [
        { role: 'user', content: '【要求】盘中决策……' },
        {
          role: 'assistant',
          content: JSON.stringify({
            decisions: [{ action: 'sell', code: '600338.SH', name: '西藏珠峰', pct: 1 }],
          }),
        },
      ],
    };

    // Act
    render(<ChatStream agents={agentsWith(jsonRound)} />);

    // Assert：总结条是人话，不是 `{"decisions": …`
    const sum = document.querySelector('.mc-sum-text');
    expect(sum?.textContent).toContain('本轮 1 项决策');
    expect(sum?.textContent).toContain('卖出 西藏珠峰 100%');
    expect(sum?.textContent).not.toContain('{');
    expect(sum?.textContent).not.toContain('"decisions"');
  });

  it('卡头日期收口北京口径：aware UTC 换算（01:35Z → 10-15 09:35），无裸 T', () => {
    const awareRound: LogLine = {
      timestamp: '2026-10-15T01:35:26Z',
      new_messages: [
        { role: 'user', content: '【要求】……' },
        { role: 'assistant', content: '总结：观望。' },
      ],
    };

    render(<ChatStream agents={agentsWith(awareRound)} />);

    const date = document.querySelector('.mc-date');
    expect(date?.textContent).toBe('10-15 09:35');
  });

  it('卡头日期：naive（写入侧已按北京墙钟）原样截取，不带 T', () => {
    render(<ChatStream agents={agentsWith(round())} />); // timestamp 2026-10-10T09:30:00（naive）

    const date = document.querySelector('.mc-date');
    expect(date?.textContent).toBe('10-10 09:30');
  });
});

describe('模型对话结构化渲染（2026-10-20「像新闻那样」）', () => {
  /** 真实输出形态：四段式 + 粗体结论 + 编号链路 + markdown 表格 */
  const mdRound: LogLine = {
    timestamp: '2026-10-20T09:35:00',
    new_messages: [
      { role: 'user', content: '【要求】盘中决策……' },
      {
        role: 'assistant',
        content: [
          '【总体总结】**持仓偏防守**：量能不足，控制仓位。',
          '【分析链路】',
          '1. 调用行情工具',
          '2. 计算因子暴露',
          '【推理论证】',
          '| 标的 | 权重 |',
          '| --- | ---: |',
          '| 600519.SH | 12% |',
        ].join('\n'),
      },
    ],
  };

  it('总结条：粗体结论提为标题，正文不丢且仍挂在 mc-sum-text 内', () => {
    // Arrange / Act
    render(<ChatStream agents={agentsWith(mdRound)} />);

    // Assert
    const sum = document.querySelector('.mc-sum-text');
    expect(sum?.querySelector('.mc-sum-headline')?.textContent).toBe('持仓偏防守');
    expect(sum?.textContent).toContain('量能不足');
  });

  it('展开后：链路成有序列表、推理表格成 table（不再是等宽文本墙）', () => {
    render(<ChatStream agents={agentsWith(mdRound)} />);

    fireEvent.click(document.querySelector('.mc-head') as HTMLElement);

    expect(document.querySelector('.mc-code.mc-prose')).toBeTruthy();
    expect(document.querySelector('ol.mc-md-ol')).toBeTruthy();
    expect(document.querySelector('table.mc-md-table')).toBeTruthy();
    expect(document.querySelector('.mc-card pre')).toBeNull();
  });
});
