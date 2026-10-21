/**
 * 组合接口错误文案：后端 `detail` 是唯一可信原因，但要挡住两类误导——
 * 1. 旧后端把 /models/blends/* 兜进 `GET /{model_id}`，回 `Model not found`；
 * 2. 结构化 detail 被 String() 成 [object Object]。
 */

import { describe, expect, it } from 'vitest';
import { blendErrorMessage } from '../../../services/blendService';

describe('blendErrorMessage', () => {
  it('旧后端兜底路由的 404 Model not found 转成可操作中文，而不是原样英文', () => {
    const err = { response: { status: 404, data: { detail: 'Model not found' } } };
    const msg = blendErrorMessage(err);
    expect(msg).not.toBe('Model not found');
    expect(msg).toContain('后端');
    expect(msg).toContain('重试');
  });

  it('业务错误 detail 原样透出（不吞 409 等语义）', () => {
    const err = { response: { status: 409, data: { detail: '计划已确认' } } };
    expect(blendErrorMessage(err)).toBe('计划已确认');
  });

  it('结构化 detail 取 message 而不是 [object Object]', () => {
    const err = {
      response: { status: 422, data: { detail: { code: 'bad', message: '模型不可参与组合' } } },
    };
    expect(blendErrorMessage(err)).toBe('模型不可参与组合');
  });
});
