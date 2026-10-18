"""尺度敏感模型特征裁剪策略回归（2026-10-18）。

Cross300 入选因子量纲跨 20 个数量级（absmax 达 9e23，均为有限值）。三类模型
对量纲不免疫：

- Ridge 在 float32 下组正规方程 XᵀX 溢出（2026-10-18 首轮 Linear 实败）；
- sklearn MLP 首层激活/梯度爆炸（同日 MLP 三档 rank IC 全负、预测 rmse 1.8e11）；
- Qlib TabNet：CUDA BatchNorm 用 float32 累加，9e23 输入的方差溢出为 NaN；
  Sparsemax 收到 NaN 后 ``support_size=0``，``gather(dim, -1)`` 越界触发
  ``CUDA error: device-side assert triggered``（2026-10-18 TabNet 实败；
  同一输入 CPU 因双精度累加不复现，GPU 已实证复现与裁剪后修复）。

本文件锁定裁剪策略的唯一出处：linear/mlp/tabnet 裁到 ±1e14，树模型与自带
z-score 的序列模型保持原始口径。训练与推理必须共用同一上界（`_fill`、
`_prepare_arrays` 与 flat 预测分块）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

_TRAINING_DIR = Path(__file__).resolve().parents[2] / "docker" / "training"
if str(_TRAINING_DIR) not in sys.path:
    sys.path.insert(0, str(_TRAINING_DIR))

from data.splits import FEATURE_ABS_CLIP, feature_clip_for  # noqa: E402


def test_scale_sensitive_models_are_clipped() -> None:
    for model_type in ("linear", "mlp", "tabnet"):
        assert feature_clip_for(model_type) == FEATURE_ABS_CLIP, model_type


def test_tree_and_sequence_models_keep_raw_scale() -> None:
    for model_type in (
        "lightgbm",
        "xgboost",
        "catboost",
        "random_forest",
        "gru",
        "lstm",
        "alstm",
        "transformer",
        "tcn",
        "nativetft",
    ):
        assert feature_clip_for(model_type) is None, model_type


def test_feature_abs_clip_square_stays_finite_in_float32() -> None:
    """裁剪上界的平方必须留在 float32 范围内（BatchNorm 方差 / Ridge XᵀX 不溢出）。"""
    assert np.float32(FEATURE_ABS_CLIP) ** 2 < np.finfo(np.float32).max
