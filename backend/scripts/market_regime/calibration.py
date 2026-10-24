"""概率校准与校准评估（纯函数，便于单测）。

背景（2026-10-24）：
- 首轮大盘方向模型 ECE 0.10~0.26，Brier 多数劣于基础率；
- 主要原因是市场基础上涨率随行情漂移，而 Platt 只在固定验证折拟合；
- 本模块提供：Platt / Isotonic / 先验偏移（rolling base rate）/ 混合校准，
  并用验证折内的 K 折交叉验证按 ECE→Brier 选择校准器。

约定：
- 所有函数只接受一维数组；概率裁剪到 [1e-6, 1-1e-6]；
- 校准器只在验证折拟合，测试折不参与任何拟合/选择；
- 先验偏移公式：logit(p) + log(π_target/(1-π_target)) - log(π_source/(1-π_source))。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

EPS = 1e-6


def clip_prob(p: np.ndarray) -> np.ndarray:
    return np.clip(np.asarray(p, dtype=float), EPS, 1 - EPS)


def logit(p: np.ndarray) -> np.ndarray:
    p = clip_prob(p)
    return np.log(p / (1 - p))


def sigmoid(z: np.ndarray) -> np.ndarray:
    z = np.asarray(z, dtype=float)
    return 1.0 / (1.0 + np.exp(-np.clip(z, -60, 60)))


def brier(y: np.ndarray, p: np.ndarray) -> float:
    y = np.asarray(y, dtype=float)
    p = np.asarray(p, dtype=float)
    ok = np.isfinite(p)
    if not ok.any():
        return float("nan")
    return float(np.mean((p[ok] - y[ok]) ** 2))


def brier_skill(y: np.ndarray, p: np.ndarray, base_rate: float) -> float:
    """Brier skill score：1 - Brier / Brier(base_rate)；>0 表示优于基础率。"""
    base = brier(y, np.full_like(np.asarray(y, dtype=float), base_rate))
    model = brier(y, p)
    if not np.isfinite(base) or base <= 0:
        return float("nan")
    return float(1 - model / base)


def logloss(y: np.ndarray, p: np.ndarray) -> float:
    y = np.asarray(y, dtype=float)
    p = clip_prob(p)
    ok = np.isfinite(p)
    if not ok.any():
        return float("nan")
    return float(-np.mean(y[ok] * np.log(p[ok]) + (1 - y[ok]) * np.log(1 - p[ok])))


def ece(y: np.ndarray, p: np.ndarray, bins: int = 10) -> float:
    y = np.asarray(y, dtype=float)
    p = np.asarray(p, dtype=float)
    ok = np.isfinite(p)
    y, p = y[ok], p[ok]
    if len(p) == 0:
        return float("nan")
    edges = np.linspace(0, 1, bins + 1)
    total = 0.0
    for i in range(bins):
        if i < bins - 1:
            mask = (p >= edges[i]) & (p < edges[i + 1])
        else:
            mask = (p >= edges[i]) & (p <= edges[i + 1])
        if not mask.any():
            continue
        total += mask.mean() * abs(float(p[mask].mean() - y[mask].mean()))
    return float(total)


def reliability_table(y: np.ndarray, p: np.ndarray, bins: int = 10) -> list[dict]:
    y = np.asarray(y, dtype=float)
    p = np.asarray(p, dtype=float)
    ok = np.isfinite(p)
    y, p = y[ok], p[ok]
    edges = np.linspace(0, 1, bins + 1)
    rows: list[dict] = []
    for i in range(bins):
        if i < bins - 1:
            mask = (p >= edges[i]) & (p < edges[i + 1])
        else:
            mask = (p >= edges[i]) & (p <= edges[i + 1])
        if not mask.any():
            continue
        rows.append(
            {
                "bin": f"{edges[i]:.1f}-{edges[i + 1]:.1f}",
                "n": int(mask.sum()),
                "p_mean": round(float(p[mask].mean()), 4),
                "y_mean": round(float(y[mask].mean()), 4),
                "gap": round(float(p[mask].mean() - y[mask].mean()), 4),
            }
        )
    return rows


def fit_platt(p: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    """Platt scaling：在 logit(p) 上拟合 a*z+b（比在概率上拟合更稳定）。

    斜率上限钉在 1.0：校准只允许「收缩」置信度，不允许把弱信号再放大。
    """
    from sklearn.linear_model import LogisticRegression

    z = logit(p).reshape(-1, 1)
    y = np.asarray(y, dtype=int)
    if len(np.unique(y)) < 2:
        return 1.0, 0.0
    lr = LogisticRegression(C=1.0, max_iter=1000)
    lr.fit(z, y)
    a = float(np.clip(lr.coef_[0, 0], 0.05, 1.0))
    return a, float(lr.intercept_[0])


def apply_platt(p: np.ndarray, params: tuple[float, float]) -> np.ndarray:
    a, b = params
    return sigmoid(a * logit(p) + b)


def prior_shift(
    p: np.ndarray, *, source_rate: float, target_rate: float
) -> np.ndarray:
    """按先验基础率平移 logit（不改变排序，只改截距）。"""
    source = float(np.clip(source_rate, EPS, 1 - EPS))
    target = float(np.clip(target_rate, EPS, 1 - EPS))
    delta = np.log(target / (1 - target)) - np.log(source / (1 - source))
    return clip_prob(sigmoid(logit(p) + delta))


def _base_rate(y: np.ndarray) -> float:
    y = np.asarray(y, dtype=float)
    return float(np.clip(y.mean() if len(y) else 0.5, EPS, 1 - EPS))


SHRINK_GRID = (0.25, 0.5, 0.75)  # λ=0 退化为常数，会摧毁排序；不再允许


def _candidate_names(allow_prior: bool, target_rates: dict[str, float]) -> list[str]:
    # 最终选择池 = raw + 向 PIT 滚动基础率收缩（窗口 × λ 网格）；
    # Platt/先验偏移只作诊断。收缩只拟合一个参数，252 天验证折下更稳定。
    names = ["raw", "platt"]
    if allow_prior:
        for window in target_rates:
            w = window.replace("base_", "")
            names.extend(
                [f"shrink_{w}_{int(lam * 100)}" for lam in SHRINK_GRID]
            )
        names.append("platt_prior")
    return names


def _fit_candidate(
    name: str,
    p_fit: np.ndarray,
    y_fit: np.ndarray,
    target_rates: dict[str, float] | None,
) -> Callable[[np.ndarray], np.ndarray]:
    p_fit = clip_prob(p_fit)
    y_fit = np.asarray(y_fit, dtype=int)
    if name == "raw":
        return lambda x: clip_prob(x)
    if name == "platt":
        params = fit_platt(p_fit, y_fit)
        return lambda x: apply_platt(x, params)
    if name.startswith("shrink_"):
        parts = name.split("_")
        if len(parts) != 3 or not target_rates:
            raise ValueError(f"{name} 格式应为 shrink_{{window}}_{{lambda}}")
        window, lam_pct = parts[1], parts[2]
        key = f"base_{window}"
        if key not in target_rates:
            raise ValueError(f"{name} 缺少 {key}")
        lam = int(lam_pct) / 100.0
        base = float(target_rates[key])

        def shrink(x: np.ndarray, _lam: float = lam, _base: float = base) -> np.ndarray:
            return clip_prob(_base + _lam * (clip_prob(x) - _base))

        return shrink
    if name == "platt_prior":
        if not target_rates or "base_252" not in target_rates:
            raise ValueError("platt_prior 需要 target_rates['base_252']")
        target_rate = float(target_rates["base_252"])
        params = fit_platt(p_fit, y_fit)

        def base(x: np.ndarray, _params: tuple[float, float] = params) -> np.ndarray:
            return apply_platt(x, _params)

        source = _base_rate(base(p_fit))
        return lambda x: prior_shift(
            base(x), source_rate=source, target_rate=target_rate
        )
    raise ValueError(name)


@dataclass(frozen=True)
class CalibrationChoice:
    name: str
    p_test: np.ndarray
    cv_ece: float
    cv_brier: float
    candidates: dict[str, dict[str, float]]


def select_calibrator(
    raw_val: np.ndarray,
    y_val: np.ndarray,
    raw_test: np.ndarray,
    *,
    target_rate: float | None = None,
    target_rates: dict[str, float] | None = None,
    seed: int = 42,
) -> CalibrationChoice:
    """在验证折内用 K 折交叉验证选择校准器，再在完整验证折重拟合。

    - 选择顺序：CV ECE 最低 → CV Brier 最低；
    - 验证样本不足 / 单一类别 → 退回 raw；
    - `target_rate`/`target_rates` 为 PIT 滚动基础率（由调用方计算），
      用于 rolling_base / prior 候选。
    """
    rates = dict(target_rates or {})
    if target_rate is not None and "base_252" not in rates:
        rates["base_252"] = float(target_rate)
    p = clip_prob(np.asarray(raw_val, dtype=float))
    y = np.asarray(y_val, dtype=int)
    p_test = clip_prob(np.asarray(raw_test, dtype=float))
    names = _candidate_names(bool(rates), rates)

    if len(p) < 40 or len(np.unique(y)) < 2:
        return CalibrationChoice(
            name="raw",
            p_test=p_test,
            cv_ece=ece(y, p),
            cv_brier=brier(y, p),
            candidates={},
        )

    # 验证折原始 ECE 已达标（≤0.05）时不做任何校准：校准只应修正明显失准，
    # 否则会在测试期分布漂移下把好概率改坏。
    raw_ece = ece(y, p)
    if raw_ece <= 0.05:
        return CalibrationChoice(
            name="raw",
            p_test=p_test,
            cv_ece=raw_ece,
            cv_brier=brier(y, p),
            candidates={"raw": {"cv_ece": round(raw_ece, 4), "cv_brier": round(brier(y, p), 4)}},
        )

    from sklearn.model_selection import TimeSeriesSplit

    # 时间序列 CV：校准器在验证折内部也只能用「过去」拟合、评估「之后」，
    # 更接近测试期的分布漂移；不能用随机 K 折。
    n_splits = 4 if len(p) >= 120 else 3
    splitter = TimeSeriesSplit(n_splits=n_splits)

    oof = {name: np.full(len(p), np.nan) for name in names}
    for train_idx, test_idx in splitter.split(p):  # type: ignore[attr-defined]
        y_train = y[train_idx]
        if len(np.unique(y_train)) < 2:
            for name in names:
                oof[name][test_idx] = _base_rate(y_train)
            continue
        for name in names:
            try:
                cal = _fit_candidate(name, p[train_idx], y_train, rates)
                oof[name][test_idx] = cal(p[test_idx])
            except Exception:  # noqa: BLE001 - 单校准器失败不阻断
                oof[name][test_idx] = np.nan

    candidates: dict[str, dict[str, float]] = {}
    for name in names:
        pred = oof[name]
        ok = np.isfinite(pred)
        if int(ok.sum()) < max(40, len(p) // 3):
            continue
        candidates[name] = {
            "cv_ece": round(ece(y[ok], pred[ok]), 4),
            "cv_brier": round(brier(y[ok], pred[ok]), 4),
        }
    if not candidates:
        return CalibrationChoice("raw", p_test, ece(y, p), brier(y, p), {})

    selection_pool = [
        n
        for n in candidates
        if n in ("raw", "platt") or n.startswith("shrink_")
    ]
    if not selection_pool:
        selection_pool = [n for n in ("raw", "platt") if n in candidates]
    if not selection_pool:
        return CalibrationChoice("raw", p_test, ece(y, p), brier(y, p), candidates)
    best = min(
        selection_pool,
        key=lambda k: (candidates[k]["cv_ece"], candidates[k]["cv_brier"]),
    )
    try:
        calibrator = _fit_candidate(best, p, y, rates)
        calibrated = clip_prob(calibrator(p_test))
    except Exception:  # noqa: BLE001 - 兜底 raw
        best = "raw"
        calibrated = p_test
    return CalibrationChoice(
        name=best,
        p_test=calibrated,
        cv_ece=candidates.get(best, {}).get("cv_ece", float("nan")),
        cv_brier=candidates.get(best, {}).get("cv_brier", float("nan")),
        candidates=candidates,
    )
