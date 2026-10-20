"""推理模板必须复现训练端对尺度敏感模型的特征裁剪。

背景：``docker/training/data/splits.py::FEATURE_ABS_CLIP`` 把 linear / mlp /
tabnet 的特征裁剪到 ±1e14（Ridge 在 float32 下组正规方程、MLP 首层激活、
TabNet BatchNorm 方差都会在 1e23 输入上溢出）。推理模板只填 NaN、不裁剪时，
修订后的因子数据会让 Ridge 直接外推出 1e7 量级（2026-10-19 实测 LIN 在
SH600613 上 pred=3.7e7，而训练产物为 -0.12）。

树模型（LightGBM/XGBoost/CatBoost）与序列模型训练端不裁剪，推理端也必须
保持原始口径，否则线上分数与训练分布不一致。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_TEMPLATE = (
    Path(__file__).resolve().parents[1]
    / "services"
    / "engine"
    / "inference"
    / "templates"
    / "inference_parquet.py"
)


@pytest.fixture(scope="module")
def tpl():
    spec = importlib.util.spec_from_file_location(
        "inference_parquet_clip_tpl", _TEMPLATE
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _frame(values: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {"symbol": [f"SH60000{i}" for i in range(len(values))], "f": values}
    )


@pytest.mark.unit
@pytest.mark.parametrize("model_type", ["linear", "mlp", "tabnet", "Linear"])
def test_scale_sensitive_models_clip_to_training_bound(tpl, model_type) -> None:
    meta = {"features": ["f"], "model_type": model_type}

    values, _ = tpl.preprocess(_frame([1e23, -1e23, 5.0]), meta)

    assert values["f"].tolist() == pytest.approx([1e14, -1e14, 5.0])


@pytest.mark.unit
@pytest.mark.parametrize("model_type", ["lightgbm", "xgboost", "catboost", "nativetft"])
def test_tree_and_sequence_models_keep_raw_values(tpl, model_type) -> None:
    meta = {"features": ["f"], "model_type": model_type}

    values, _ = tpl.preprocess(_frame([1e23, -1e23, 5.0]), meta)

    assert np.isfinite(values.to_numpy(dtype=np.float64)).all()
    assert values["f"].tolist() == pytest.approx([1e23, -1e23, 5.0])


@pytest.mark.unit
def test_clip_runs_after_fill(tpl) -> None:
    meta = {
        "features": ["f"],
        "model_type": "linear",
        "fill_values": {"f": 7.0},
    }

    values, _ = tpl.preprocess(_frame([np.nan, 1e23]), meta)

    assert values["f"].tolist() == pytest.approx([7.0, 1e14])
