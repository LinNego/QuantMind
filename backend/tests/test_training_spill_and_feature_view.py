"""特征溢出（memmap）与零拷贝 X 视图的等价性回归（2026-10-16 全窗内存改造）。

背景：全窗（2016 起 ≈ 10.72M 行 × 300 特征）训练必 OOM 的两个头号点——

1. `load_data` 尾部 `.loc[valid_index, keep_cols].reset_index(drop=True)` 同时驻留
   3 份帧级拷贝（源帧 + 取行副本 + reset_index 内部再拷），全窗 ≈ 42G；
2. `_prepare_arrays` 的 `X_train = _fill(train_df)` 与训练帧、库内部压缩结构共存，
   全窗 ≈ 30G 级。

改造：特征落 C 序 memmap（帧架在映射上，页缓存可回收）+ `_fill_inplace` 把
NaN/±inf 原地填进帧特征块、返回零拷贝 (n,F) C 连续视图（不可行时回退拷贝路径）。
本文件锁定两条契约，**逐位一致**：

- 溢出帧 == 旧 `.loc[...].reset_index()` 写法（行序/列序/dtype/索引，含 NaN/±inf）；
- 视图路径的 X == 拷贝路径（与帧共享内存、写透传），且非 f32 特征时确实回退。

判决证据（2026-10-16，训练镜像 `quantmind-oss-gpu:latest`，pandas 2.3.3/numpy 1.26.4，
与 train.py 同环境）：真实链条 溢出帧 → `_take_rows` 切段 → `_prepare_arrays` 上
train/val 视图均生效（C 连续、shares_memory 全过），X 与拷贝基准逐位一致。宿主开发机
为 pandas 3.x（f32 列按列拆块、字符串列为 ArrowStringArray），块视图判据自然落空 →
安全回退。故视图断言为双模态：视图生效则锁视图合同（共享内存+原地填充），否则锁回退
行为（帧逐位未动）；两条路径都锁「与旧拷贝口径逐位一致」。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

_TRAINING_DIR = Path(__file__).resolve().parents[2] / "docker" / "training"
if str(_TRAINING_DIR) not in sys.path:
    sys.path.insert(0, str(_TRAINING_DIR))

from data.loading import _clip_rows_via_spill  # noqa: E402
from data.splits import _prepare_arrays, _take_rows  # noqa: E402

_FEATURES = [f"f{i}" for i in range(6)]
_KEYS = ["SH600000", "SZ000001", "SH600036"]


def _make_wide_frame(n_per_key: int = 40) -> pd.DataFrame:
    """与真实读帧同构：乱序索引 + symbol/trade_date/label + f32 特征（含 NaN/±inf）。"""
    rows = n_per_key * len(_KEYS)
    rng = np.random.default_rng(7)
    df = pd.DataFrame(
        {
            "symbol": np.repeat(_KEYS, n_per_key).astype(object),
            "trade_date": pd.to_datetime(
                np.tile(pd.bdate_range("2024-01-02", periods=n_per_key), len(_KEYS))
            ),
            "label": rng.standard_normal(rows),
        }
    )
    for i, c in enumerate(_FEATURES):
        vals = rng.standard_normal(rows).astype(np.float32)
        if i == 0:
            vals[::13] = np.nan
        if i == 1:
            vals[::17] = np.inf
            vals[1::17] = -np.inf
        df[c] = vals
    return df.sample(frac=1.0, random_state=3)


def _sorted_valid_index(df: pd.DataFrame) -> pd.Index:
    """模拟 load_data：按 (symbol, trade_date) 排序后的有效行标签（与源帧顺序错开）。"""
    return df.sort_values(["symbol", "trade_date"]).index


def _legacy_fill(frame: pd.DataFrame, features: list[str], fill_vec: np.ndarray) -> np.ndarray:
    """拷贝路径的等价复算（与 `splits._fill` 同口径：NaN|±inf 一律按 fill_vec）。"""
    arr = np.empty((len(frame), len(features)), dtype=np.float32)
    for i, c in enumerate(features):
        col = frame[c].to_numpy(dtype=np.float32, copy=True)
        mask = np.isnan(col) | np.isinf(col)
        col[mask] = fill_vec[i]
        arr[:, i] = col
    return arr


def test_spill_clip_matches_legacy_loc_clip_bit_for_bit() -> None:
    df = _make_wide_frame()
    valid_index = _sorted_valid_index(df)
    keep_cols = ["symbol", "trade_date", "label"] + _FEATURES

    # Act
    pos = df.index.get_indexer(valid_index)
    assert (pos >= 0).all()
    spilled = _clip_rows_via_spill(df, pos, _FEATURES)
    legacy = df.loc[valid_index, keep_cols].reset_index(drop=True)

    # Assert
    assert spilled is not None
    assert list(spilled.columns) == keep_cols
    assert isinstance(spilled.index, pd.RangeIndex)
    assert len(spilled) == len(legacy)
    for c in keep_cols:
        assert spilled[c].dtype == legacy[c].dtype, c
        np.testing.assert_array_equal(spilled[c].to_numpy(), legacy[c].to_numpy(), err_msg=c)


def test_spill_clip_falls_back_on_non_float32_features() -> None:
    df = _make_wide_frame()
    df["f0"] = df["f0"].astype(np.float64)

    pos = df.index.get_indexer(_sorted_valid_index(df))

    assert _clip_rows_via_spill(df, pos, _FEATURES) is None


def test_spilled_frame_take_segments_stay_bit_exact() -> None:
    """溢出帧（memmap 块）上 take 取段与旧帧逐位一致——切分链路的读法。"""
    df = _make_wide_frame()
    valid_index = _sorted_valid_index(df)
    keep_cols = ["symbol", "trade_date", "label"] + _FEATURES
    pos = df.index.get_indexer(valid_index)

    spilled = _clip_rows_via_spill(df, pos, _FEATURES)
    legacy = df.loc[valid_index, keep_cols].reset_index(drop=True)
    assert spilled is not None

    seg_pos = np.array([0, 5, 9, 10, 33, 119])
    seg_sp = spilled.take(seg_pos)
    seg_sp.index = pd.RangeIndex(len(seg_sp))
    seg_lg = legacy.take(seg_pos)
    seg_lg.index = pd.RangeIndex(len(seg_lg))
    for c in keep_cols:
        np.testing.assert_array_equal(seg_sp[c].to_numpy(), seg_lg[c].to_numpy(), err_msg=c)


def _fake_val(train_df: pd.DataFrame) -> pd.DataFrame:
    """与真实切分同款地取验证段：`_take_rows`（take + 就地赋 index）。"""
    return _take_rows(train_df, np.arange(len(train_df)) % 3 == 0, "val")


def test_prepare_arrays_inplace_view_matches_copy_path_bit_for_bit() -> None:
    df = _make_wide_frame()
    pos = df.index.get_indexer(_sorted_valid_index(df))
    seg = df.take(pos)
    seg.index = pd.RangeIndex(len(seg))
    train_df = seg[["symbol", "trade_date", "label"] + _FEATURES]
    val_df = _fake_val(train_df)
    train_raw = train_df.copy(deep=True)  # 调用前留对照（视图路径会原地填充）

    # Act
    fill_values, X_train, y_train, X_val, y_val, _fill = _prepare_arrays(
        train_df, val_df, _FEATURES
    )

    # Assert —— 两条路径共同的硬合同：X 与旧拷贝口径（独立复算）逐位一致
    fill_vec = np.array([fill_values[c] for c in _FEATURES], dtype=np.float32)
    np.testing.assert_array_equal(X_train, _legacy_fill(train_df, _FEATURES, fill_vec))
    np.testing.assert_array_equal(X_val, _legacy_fill(val_df, _FEATURES, fill_vec))
    assert np.isfinite(X_train).all()
    assert np.isfinite(X_val).all()
    np.testing.assert_array_equal(y_train, train_raw["label"].astype("float32").to_numpy())

    # Assert —— 双模态：块布局允许（pandas 2.x F 序单块，训练容器实测）则视图生效，
    # 与帧共享内存、原地填充；否则（如 pandas 3 拆列块）安全回退、帧逐位未动。
    if np.shares_memory(X_train, train_df["f0"].to_numpy()):
        assert X_train.flags.c_contiguous
        assert np.shares_memory(X_val, val_df["f0"].to_numpy())
        assert X_val.flags.c_contiguous
        assert not np.isnan(train_df["f0"].to_numpy()).any()  # 帧被原地填充
        assert np.isfinite(train_df["f1"].to_numpy()).all()
        assert not np.isnan(val_df["f0"].to_numpy()).any()
    else:
        for c in ("f0", "f1"):
            np.testing.assert_array_equal(
                train_df[c].to_numpy(), train_raw[c].to_numpy(), err_msg=c
            )
    # 预测路径经 _fill 重算的结果同样逐位一致（掩码幂等；与视图路径互证）
    np.testing.assert_array_equal(_fill(train_df), _legacy_fill(train_df, _FEATURES, fill_vec))
    np.testing.assert_array_equal(_fill(train_df), X_train)


def test_prepare_arrays_second_call_reuses_fill_values_and_view() -> None:
    """多模型循环对同一批帧重复调用：fill_values 与 X 必须完全一致（attrs 缓存）。"""
    df = _make_wide_frame()
    pos = df.index.get_indexer(_sorted_valid_index(df))
    seg = df.take(pos)
    seg.index = pd.RangeIndex(len(seg))
    train_df = seg[["symbol", "trade_date", "label"] + _FEATURES]
    val_df = _fake_val(train_df)

    fill1, X1, _, _, _, _ = _prepare_arrays(train_df, val_df, _FEATURES)
    fill2, X2, _, _, _, _ = _prepare_arrays(train_df, val_df, _FEATURES)

    assert fill1.keys() == fill2.keys()
    for k in fill1:
        assert fill1[k] == fill2[k], k
    np.testing.assert_array_equal(X1, X2)


def test_prepare_arrays_falls_back_to_copy_when_block_not_found() -> None:
    """特征列不构成单一 f32 块（如混入 f64 列）→ 回退拷贝路径且不改动帧。"""
    df = _make_wide_frame()
    df["extra_f64"] = np.arange(len(df), dtype=np.float64)
    pos = df.index.get_indexer(_sorted_valid_index(df))
    seg = df.take(pos)
    seg.index = pd.RangeIndex(len(seg))
    feats = _FEATURES + ["extra_f64"]
    train_df = seg[["symbol", "trade_date", "label"] + feats]
    val_df = _fake_val(train_df)

    fill_values, X_train, _, X_val, _, _ = _prepare_arrays(train_df, val_df, feats)

    # 拷贝路径：X 不与帧共享内存，帧保持原始（NaN 仍在）
    assert not np.shares_memory(X_train, train_df["f0"].to_numpy())
    assert np.isnan(train_df["f0"].to_numpy()).any()
    # 值仍与旧口径逐位一致
    fill_vec = np.array([fill_values[c] for c in feats], dtype=np.float32)
    np.testing.assert_array_equal(X_train, _legacy_fill(train_df, feats, fill_vec))
