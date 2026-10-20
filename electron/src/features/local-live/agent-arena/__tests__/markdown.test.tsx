/**
 * 块级 markdown 渲染器（模型对话「结构化输出」）的排版契约。
 *
 * 真实模型输出（/tmp 日志实测）里三种块最常见：GFM 表格、编号链路（1. 2.）、
 * ```json 围栏；旧渲染器把它们全当普通段落铺在 <pre> 里，右侧面板看着就是一片
 * 等宽文本墙。这里锁住「表格进 table / 编号进 ol / 围栏进 pre」三条，以及
 * ⚠️ 警示条与【段标】小标题的分流（【持仓】这类段标不该被染成黄色警告）。
 */

import { describe, expect, it } from 'vitest';
import { render } from '@testing-library/react';
import { renderMarkdown } from '../arena/utils/markdown';

const mount = (text: string) => {
  const { container } = render(<div>{renderMarkdown(text)}</div>);
  return container;
};

describe('renderMarkdown 结构化块', () => {
  it('GFM 表格：表头/表体成 table，分隔行不进内容，右对齐列带 ta-right', () => {
    // Arrange
    const text = ['| 标的 | 权重 |', '| --- | ---: |', '| **600519.SH** | 12% |'].join('\n');

    // Act
    const c = mount(text);

    // Assert
    const table = c.querySelector('table.mc-md-table');
    expect(table).toBeTruthy();
    expect(c.querySelector('.mc-md-table-wrap')).toBeTruthy();
    expect(c.querySelectorAll('thead th')).toHaveLength(2);
    expect(c.querySelectorAll('tbody tr')).toHaveLength(1);
    const cells = c.querySelectorAll('tbody td');
    expect(cells[0]?.textContent).toBe('600519.SH');
    expect(cells[0]?.querySelector('strong')).toBeTruthy();
    expect(cells[1]?.className).toContain('ta-right');
    // 分隔行（---|---）绝不能被当正文铺出来
    expect(c.textContent).not.toContain('---');
  });

  it('编号项：连续的 1. 2. 收敛成有序列表', () => {
    const c = mount('1. 调用行情工具\n2. 计算因子暴露');

    const ol = c.querySelector('ol.mc-md-ol');
    expect(ol).toBeTruthy();
    expect(ol?.querySelectorAll('li')).toHaveLength(2);
    expect(ol?.textContent).toContain('调用行情工具');
  });

  it('围栏代码块：原样进 pre，内部不解析粗体/斜体', () => {
    const c = mount(['```json', '{"action": "buy", "note": "**不要解析**"}', '```'].join('\n'));

    const pre = c.querySelector('pre.mc-md-pre');
    expect(pre?.textContent).toContain('**不要解析**');
    expect(pre?.querySelector('strong')).toBeNull();
  });

  it('⚠️ 行仍是警示条；【段标】行降级为小节标题（不是黄色警告）', () => {
    const c = mount('⚠️ 风险提示：量能不足\n【持仓】600519 100 股');

    expect(c.querySelectorAll('.mc-md-warn')).toHaveLength(1);
    expect(c.querySelector('.mc-md-warn')?.textContent).toContain('风险提示');
    const sub = c.querySelector('.mc-md-sub');
    expect(sub?.textContent).toContain('【持仓】');
    expect(c.querySelectorAll('.mc-md-warn')).toHaveLength(1);
  });

  it('缩进子项：两级项目符号按层级缩进，不并成同一行', () => {
    const c = mount('- 一级\n  - 二级');

    const items = c.querySelectorAll('.mc-md-list li');
    expect(items).toHaveLength(2);
    expect(items[1]?.className).toContain('mc-md-li-d1');
  });
});
