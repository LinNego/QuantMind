"""`_compute_metrics` 整帧拷贝治理回归（2026-10-16 全窗训练 OOM 根因）。

背景：全窗（2016 起 10.55M 行 × 300 特征）训练的 train 段 ≈ 9.5M×304 f32。
`model_trainers/metrics.py::_compute_metrics` 旧实现 `df.iloc[valid].copy()`
对该帧是**两份整帧深拷 ≈ 23G 瞬时驻留**（掩码取行物化一份 + copy 再一份），
训练进程稳态 ~21G + 23G ≈ 宿主内核报告 anon-rss 44.3G，全局 OOM，产物全丢。
修复 = 只物化 trade_date 一列（下游 `_rank_ic_series` 仅按 trade_date 分组
消费 _pred/_label；DL 路径早在 trainers_dl._dl_metrics 收敛到同款小帧口径）。

配套第二颗雷：`data/splits.py::_frame_with_range_index` —— pandas 2.3（无
CoW）下 `reset_index(drop=True)` 内部无条件 `self.copy(deep=None)`（_take_rows
docstring 2026-09-23 实测 2.01 份段），train.py 返回段的 4 处 reset_index 对
全窗帧又是一份 11.4G 深拷；索引已归零 RangeIndex 时放行原帧即可。

本文件锁定：
1. 逐键等价：新实现与旧实现（逐行复刻）在正常/NaN/±inf/反向/早退/小分组各
   边界输出完全一致；
2. 助手语义：RangeIndex 直通同一对象（零拷贝）、非 RangeIndex 重置且与旧
   写法逐位一致、源帧不动；
3. 内存形态（子进程 VmHWM 实测）：1.2M×200 f32 帧下新实现调用峰值增量
   < 250MB，旧实现复刻 > 500MB —— 证明闸门既能挡回归、又有判别力。
"""
from __future__ import annotations

import importlib.util
import json
import math
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_TRAINING_DIR = Path(__file__).resolve().parents[2] / "docker" / "training"
if str(_TRAINING_DIR) not in sys.path:
    sys.path.insert(0, str(_TRAINING_DIR))


def _load_metrics_module():
    """按文件直载 model_trainers/metrics.py。

    不能走 `from model_trainers.metrics import ...`：包 `__init__` 聚合导入
    trainers_dl（拖 torch），宿主开发机无 torch；metrics 本体只依赖
    numpy/pandas，直载与容器内语义一致（test_training_shap_summary 同款手法）。
    """
    path = _TRAINING_DIR / "model_trainers" / "metrics.py"
    spec = importlib.util.spec_from_file_location("qm_training_metrics", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


_M = _load_metrics_module()
_compute_metrics = _M._compute_metrics
_ic = _M._ic
_rank_ic_series = _M._rank_ic_series

from data.splits import _frame_with_range_index  # noqa: E402  (sys.path 已就位)


def _legacy_compute_metrics(df: pd.DataFrame, y_true, y_pred) -> dict:
    """2026-10-16 修复前的原实现逐行复刻（对照基准，勿改）。"""
    y_true = np.asarray(y_true, dtype=np.float64)
    y_pred = np.asarray(y_pred, dtype=np.float64)
    valid = np.isfinite(y_true) & np.isfinite(y_pred)
    if valid.sum() < 10:
        return {"ic": float("nan"), "rank_ic": float("nan"), "rank_icir": float("nan"),
                "rmse": 0.0, "auc": 0.0, "score_direction": "normal"}
    y_true = y_true[valid]
    y_pred = y_pred[valid]
    if len(df) == len(valid):
        df = df.iloc[valid].copy()
    ic = _ic(y_pred, y_true)
    series = _rank_ic_series(df.assign(_pred=y_pred, _label=y_true), "_pred", "_label")
    rank_ic = float(np.nanmean(series)) if series else float("nan")
    rank_icir = float(np.mean(series) / (np.std(series) + 1e-9)) if series else float("nan")
    rmse = float(np.sqrt(np.mean(np.square(y_pred - y_true)))) if len(y_true) else float("nan")
    labels = (y_true > 0).astype(int)
    pos = int(labels.sum())
    neg = int(len(labels) - pos)
    auc = float("nan")
    if pos > 0 and neg > 0:
        ranks = pd.Series(y_pred).rank(method="average").to_numpy()
        auc = float((ranks[labels == 1].sum() - pos * (pos + 1) / 2.0) / (pos * neg))
    score_direction = "normal" if np.isnan(ic) or ic >= 0 else "reversed"
    return {"ic": ic, "rank_ic": rank_ic, "rank_icir": rank_icir,
            "rmse": rmse, "auc": auc, "score_direction": score_direction}


def _make_frame(rows: int) -> pd.DataFrame:
    """两标的 × 多日期的宽帧，索引为默认 RangeIndex（与切分链输出同构）。"""
    rng = np.random.default_rng(42)
    keys = ["SH600000", "SZ000001"]
    df = pd.DataFrame({
        "symbol": np.repeat(keys, rows // 2).astype(object),
        "trade_date": pd.to_datetime(
            np.tile(pd.bdate_range("2025-01-02", periods=rows // 2), len(keys))
        ),
        "label": rng.standard_normal(rows),
    })
    for i in range(3):
        df[f"f{i}"] = rng.standard_normal(rows).astype(np.float32)
    return df


def _assert_same_metrics(new: dict, old: dict) -> None:
    assert new.keys() == old.keys()
    for key in old:
        a, b = new[key], old[key]
        if isinstance(b, str):
            assert a == b, key
        elif math.isnan(b):
            assert math.isnan(a), key
        else:
            assert a == b, (key, a, b)  # 同输入同口径：位级一致


def test_new_impl_matches_legacy_bit_for_bit_across_edges() -> None:
    df = _make_frame(600)
    rng = np.random.default_rng(7)

    # 正常 + NaN/±inf 混入（含 df 特征列污染，验证其余列不参与计算的口径）
    y_true = rng.standard_normal(600)
    y_pred = rng.standard_normal(600)
    y_true[::5] = np.nan
    y_pred[::7] = np.inf
    y_pred[13::19] = -np.inf
    df.loc[df.index[3], "f0"] = np.nan
    df.loc[df.index[4], "f1"] = np.inf
    _assert_same_metrics(_compute_metrics(df, y_true, y_pred),
                         _legacy_compute_metrics(df, y_true, y_pred))

    # 反向模型（IC<0 → score_direction=reversed）
    y_fin = rng.standard_normal(600)
    _assert_same_metrics(_compute_metrics(df, y_fin, -y_fin),
                         _legacy_compute_metrics(df, y_fin, -y_fin))

    # 早退分支：有效对 < 10
    y_short = np.full(600, np.nan)
    y_short[:5] = rng.standard_normal(5)
    y_pred_short = rng.standard_normal(600)
    _assert_same_metrics(_compute_metrics(df, y_short, y_pred_short),
                         _legacy_compute_metrics(df, y_short, y_pred_short))


def test_new_impl_matches_legacy_with_small_daily_groups() -> None:
    """某交易日样本 < 10 被跳过、其余照常 —— 分组边界两实现必须一致。"""
    rng = np.random.default_rng(9)
    rows_a, rows_b = 30, 5
    df = pd.DataFrame({
        "trade_date": pd.to_datetime(["2025-01-02"] * rows_a + ["2025-01-03"] * rows_b),
        "label": rng.standard_normal(rows_a + rows_b),
    })
    y_true = rng.standard_normal(rows_a + rows_b)
    y_pred = rng.standard_normal(rows_a + rows_b)
    new = _compute_metrics(df, y_true, y_pred)
    old = _legacy_compute_metrics(df, y_true, y_pred)
    _assert_same_metrics(new, old)
    # 反向确认：第二个分组（5 行）确实被跳过，只剩 1 个日度 IC
    assert not math.isnan(new["rank_ic"])


def test_frame_with_range_index_noop_and_reset_equivalence() -> None:
    df = _make_frame(40)

    # 非 RangeIndex：返回重置后的新帧，与旧写法逐位一致，源帧不动
    shuffled = df.sample(frac=1.0, random_state=3)
    assert not isinstance(shuffled.index, pd.RangeIndex)
    src = shuffled.copy(deep=True)
    out = _frame_with_range_index(shuffled)
    assert out is not shuffled
    assert isinstance(out.index, pd.RangeIndex)
    pd.testing.assert_frame_equal(out, shuffled.reset_index(drop=True))
    pd.testing.assert_frame_equal(shuffled, src)  # 源帧未被就地改动

    # RangeIndex：原样返回同一对象（零拷贝，全窗 OOM 修复的关键路径）
    again = _frame_with_range_index(out)
    assert again is out


_PROBE_SCRIPT = r'''
import gc, importlib.util, json, os, resource
import numpy as np
import pandas as pd

metrics_path = os.path.join(os.environ["QM_TRAIN_DIR"], "model_trainers", "metrics.py")
spec = importlib.util.spec_from_file_location("qm_metrics_probe", metrics_path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def legacy(df, y_true, y_pred):
    """修复前旧实现的复刻（内存关切段：iloc[valid].copy() + assign）。"""
    y_true = np.asarray(y_true, dtype=np.float64)
    y_pred = np.asarray(y_pred, dtype=np.float64)
    valid = np.isfinite(y_true) & np.isfinite(y_pred)
    if valid.sum() < 10:
        return []
    y_true = y_true[valid]
    y_pred = y_pred[valid]
    if len(df) == len(valid):
        df = df.iloc[valid].copy()
    return mod._rank_ic_series(df.assign(_pred=y_pred, _label=y_true), "_pred", "_label")


n, c = 1_200_000, 200
rng = np.random.default_rng(11)
# dtype=f32 直出：避免 f64 中转的 1.9G 峰值污染基线高水位
arr = rng.standard_normal((n, c), dtype=np.float32)
df = pd.DataFrame(arr, columns=[f"f{i}" for i in range(c)])
dates = np.tile(pd.bdate_range("2024-01-02", periods=1000).to_numpy(), n // 1000 + 1)[:n]
df.insert(0, "trade_date", dates)
y_true = rng.standard_normal(n)
y_pred = rng.standard_normal(n)
y_true[::997] = np.nan
y_pred[::991] = np.inf

gc.collect()
base = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss  # Linux: KB, 单调高水位
if os.environ["QM_PROBE_IMPL"] == "legacy":
    legacy(df, y_true, y_pred)
else:
    mod._compute_metrics(df, y_true, y_pred)
after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
print(json.dumps({"delta_mb": (after - base) / 1024.0}))
'''


def _run_probe(impl: str) -> float:
    env = {**os.environ, "QM_TRAIN_DIR": str(_TRAINING_DIR), "QM_PROBE_IMPL": impl}
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE_SCRIPT],
        env=env, capture_output=True, text=True, timeout=600,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])["delta_mb"]


def test_compute_metrics_call_peak_small_and_legacy_regression_caught() -> None:
    """1.2M×200 f32 帧：新实现调用峰值增量 < 250MB；旧实现复刻 > 500MB（判别力）。"""
    new_delta = _run_probe("new")
    legacy_delta = _run_probe("legacy")
    assert new_delta < 250, f"新实现调用峰值增量 {new_delta:.0f}MB，疑似整帧拷贝回归"
    assert legacy_delta > 500, f"旧实现复刻仅 {legacy_delta:.0f}MB，探针失去判别力"
