/**
 * 错误文案提取：FastAPI 的 `detail` 可能是对象（`{code, message}`），
 * 直接拼进模板字符串会变成 `[object Object]`。
 *
 * 事故（2026-10-23）：模型治理「一键推理至最新」全选 50 个模型 → 后端 400
 * `{"detail":{"code":"too_many_models","message":"最多选择 20 个模型"}}` →
 * 弹窗显示 `[object Object]` 且预览为空，用户看不出原因。
 */
import { describe, it, expect } from 'vitest';
import { errorHandler } from '../errorHandler';

const axiosError = (data: unknown, status = 400) =>
  ({
    response: { status, statusText: '', data },
    message: 'Request failed with status code 400',
    config: { url: '/models/inference/backfill-jobs/preview' },
  }) as never;

describe('parseError · 嵌套 detail 对象', () => {
  it('取 detail.message 作为 message（不再输出 [object Object]）', () => {
    const parsed = errorHandler.parseError(
      axiosError({
        error: { code: 'HTTP_400', message: '最多选择 20 个模型' },
        detail: { code: 'too_many_models', message: '最多选择 20 个模型' },
      }),
    );

    expect(typeof parsed.message).toBe('string');
    expect(parsed.message).toBe('最多选择 20 个模型');
    expect(parsed.message).not.toContain('[object Object]');
    expect(parsed.code).toBe('too_many_models');
  });

  it('detail 是字符串时原样使用', () => {
    const parsed = errorHandler.parseError(axiosError({ detail: '任务不存在' }, 404));

    expect(parsed.message).toBe('任务不存在');
  });

  it('只有 error 信封（{error:{message}}）时也能取到人话', () => {
    const parsed = errorHandler.parseError(
      axiosError({ error: { code: 'HTTP_403', message: '实盘开关关闭' } }, 403),
    );

    expect(parsed.message).toBe('实盘开关关闭');
    expect(parsed.code).toBe('HTTP_403');
  });

  it('details 永远是字符串（避免上层模板拼接再产出 [object Object]）', () => {
    const parsed = errorHandler.parseError(
      axiosError({ detail: { code: 'x', message: '原因' } }),
    );

    expect(typeof parsed.details).toBe('string');
  });
});
