import { describe, expect, test, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { UserModelRecord } from '../../services/modelTrainingService';
import { ModelCard } from '../modelRegistryPanels';

// 评分徽章会拉取评估 API；卡片交互测试与它无关，直接置空。
vi.mock('../../components/shared/EvalScoreBadge', () => ({
  EvalScoreBadge: () => null,
}));

function model(overrides: Partial<UserModelRecord> = {}): UserModelRecord {
  return {
    tenant_id: 'default',
    user_id: '1',
    model_id: 'mdl_test',
    source_run_id: '',
    status: 'ready',
    storage_path: '',
    model_file: '',
    metadata_json: { display_name: '测试模型', model_type: 'lightgbm' },
    metrics_json: {},
    is_default: false,
    created_at: '2026-10-01T00:00:00Z',
    ...overrides,
  };
}

describe('ModelCard 置顶 / 收藏', () => {
  test('点击置顶与收藏只触发对应回调，不会顺带选中卡片', async () => {
    const user = userEvent.setup();
    const onClick = vi.fn();
    const onTogglePin = vi.fn();
    const onToggleFavorite = vi.fn();

    render(
      <ModelCard
        model={model()}
        isSelected={false}
        onClick={onClick}
        onSetDefault={() => {}}
        canSetDefault={false}
        isPinned={false}
        isFavorite={false}
        onTogglePin={onTogglePin}
        onToggleFavorite={onToggleFavorite}
      />,
    );

    await user.click(screen.getByLabelText('置顶'));
    await user.click(screen.getByLabelText('收藏'));

    expect(onTogglePin).toHaveBeenCalledTimes(1);
    expect(onToggleFavorite).toHaveBeenCalledTimes(1);
    expect(onClick).not.toHaveBeenCalled();
  });

  test('已置顶/已收藏时按钮转为取消语义', () => {
    render(
      <ModelCard
        model={model()}
        isSelected
        onClick={() => {}}
        onSetDefault={() => {}}
        canSetDefault={false}
        isPinned
        isFavorite
        onTogglePin={() => {}}
        onToggleFavorite={() => {}}
      />,
    );

    expect(screen.getByLabelText('取消置顶')).toBeInTheDocument();
    expect(screen.getByLabelText('取消收藏')).toBeInTheDocument();
  });
});
