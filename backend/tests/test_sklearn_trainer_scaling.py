"""尺度敏感 sklearn 训练器（linear/mlp）的标准化回归（2026-10-18）。

背景：Cross300 入选因子量纲跨 20 个数量级（absmax 达 9e23）。直接喂原始值：

- Ridge 的 float32 正规方程溢出（首轮 Linear 实败：scipy
  "array must not contain infs or NaNs"）；
- MLP 首层激活/梯度爆炸（同日 MLP 三档 rank IC 全负、预测 rmse 1.8e11）。

修复：`_train_linear` / `_train_mlp` 返回 `Pipeline(StandardScaler, model)`，
scaler 随模型一起 pickle，预测路径自动同口径。本文件锁定两条契约：

1. 训练器确实带 StandardScaler（不是裸回归器）；
2. 极端量纲列在场时，预测仍有限且量级正常，且能学到主信号方向。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("sklearn")
pytest.importorskip("lightgbm")

_TRAINING_DIR = Path(__file__).resolve().parents[2] / "docker" / "training"
if str(_TRAINING_DIR) not in sys.path:
    sys.path.insert(0, str(_TRAINING_DIR))

from model_trainers.trainers_gbdt import _train_linear, _train_mlp  # noqa: E402

_FEATURES = ["f_normal", "f_extreme"]
# 训练器收到的矩阵已被 _prepare_arrays 裁到 ±1e14；此处沿用同一上限。
_EXTREME_VALUE = np.float32(1e14)
_CFG = {
    "seed": 42,
    "label": {"target_mode": "return"},
    "model": {"dl_params": {"n_epochs": 30, "batch_size": 128}},
}


def _make_split(n_rows: int = 2000, seed: int = 7):
    """f_normal 是有效信号，f_extreme 为偶发 ±1e14 的噪声列（仿真实因子）。"""
    rng = np.random.default_rng(seed)
    normal = rng.standard_normal(n_rows).astype(np.float32)
    extreme = rng.standard_normal(n_rows).astype(np.float32)
    extreme[::97] = _EXTREME_VALUE
    extreme[1::194] = -_EXTREME_VALUE
    X = np.column_stack([normal, extreme])
    y = (normal + rng.standard_normal(n_rows) * 0.1).astype(np.float32)
    return X, y


def _pearson(a: np.ndarray, b: np.ndarray) -> float:
    return float(
        np.corrcoef(np.asarray(a, dtype=np.float64), np.asarray(b, dtype=np.float64))[
            0, 1
        ]
    )


def _assert_sane_predictions(pred: np.ndarray) -> None:
    assert np.isfinite(pred).all(), "预测含 NaN/inf——标准化链路失效"
    assert np.abs(pred).max() < 1e3, f"预测量级失控：max={np.abs(pred).max():.3e}"


def test_linear_trainer_standardizes_extreme_features() -> None:
    X_train, y_train = _make_split()
    X_val, y_val = _make_split(seed=11)

    # Act
    model = _train_linear(_CFG, _FEATURES, X_train, y_train, X_val, y_val)
    pred = model.predict(X_val)

    # Assert —— 带 scaler 且预测有限、量级正常、方向正确
    assert list(model.named_steps) == ["scaler", "model"]
    _assert_sane_predictions(pred)
    assert _pearson(pred, y_val) > 0.8


def test_mlp_trainer_standardizes_extreme_features() -> None:
    X_train, y_train = _make_split()
    X_val, y_val = _make_split(seed=11)

    # Act
    model = _train_mlp(_CFG, _FEATURES, X_train, y_train, X_val, y_val)
    pred = model.predict(X_val)

    # Assert —— MLP 对量纲最敏感：标准化后预测不得爆炸，且主信号方向为正
    assert list(model.named_steps) == ["scaler", "model"]
    _assert_sane_predictions(pred)
    assert _pearson(pred, y_val) > 0.4
