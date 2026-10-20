"""推理模板必须把 ±inf 当缺失处理。

背景：训练端 ``docker/training/data/splits.py::_fill`` 已把 ±inf 与 NaN
同口径视作缺失（中位数/0 填充），所以 ``gap_mined:HD_NUMCHG_YOY`` 这类除零
因子不会炸训练。推理模板此前只填 NaN，inf 原样进入 ``astype(np.float32)``：

- LightGBM 默认把 inf 当缺失，静默掩盖；
- XGBoost 直接 ``Check failed: Input data contains inf``；
- sklearn StandardScaler / TabNet 同样报错。

2026-10-19 的 26 模型外推在 2026-08-28 实测：XGB/LIN/RF/MLP/TAB 五个模型
全部因此 exit=1。本文件钉住推理端与训练端同口径。
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
        "inference_parquet_inf_tpl", _TEMPLATE
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.mark.unit
def test_preprocess_fills_inf_with_metadata_fill_values(tpl) -> None:
    frame = pd.DataFrame(
        {
            "symbol": ["SH600000", "SH600001"],
            "f1": [np.inf, 1.0],
            "f2": [-np.inf, 2.0],
        }
    )
    meta = {
        "features": ["f1", "f2"],
        "fill_values": {"f1": 0.5, "f2": 0.0},
    }

    values, symbols = tpl.preprocess(frame, meta)

    assert symbols == ["SH600000", "SH600001"]
    assert np.isfinite(values.to_numpy(dtype=np.float64)).all()
    assert values.loc[0, "f1"] == pytest.approx(0.5)
    assert values.loc[0, "f2"] == pytest.approx(0.0)


@pytest.mark.unit
def test_preprocess_cross_sectional_path_treats_inf_as_missing(tpl) -> None:
    frame = pd.DataFrame(
        {"symbol": ["SH600000", "SH600001", "SH600002"], "f": [np.inf, 1.0, 3.0]}
    )
    meta = {
        "features": ["f"],
        "preprocessing": {"enabled": True, "winsor": False},
    }

    values, _ = tpl.preprocess(frame, meta)

    assert np.isfinite(values.to_numpy(dtype=np.float64)).all()
    assert values.loc[0, "f"] == pytest.approx(0.0)
