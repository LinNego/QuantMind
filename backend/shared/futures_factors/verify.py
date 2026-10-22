"""因子数据集自检。

校验的是**数据集本身**能否直接喂给模型：主键唯一、数值有限、OHLC 自洽、
特征覆盖率达标。常量列只告警不拦截（某些品种在早期年份确实没有某类数据）。

若给定 ``future_col``，额外做一次标签相关性体检：单因子与未来收益的
|corr| 高到不合常理时，大概率是把未来信息编码进了特征。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = ["verify_factor_panel"]

_BASE_COLUMNS = ("symbol", "date", "open", "high", "low", "close", "volume", "amount")
_LABEL_CORR_LIMIT = 0.95
_MIN_COVERAGE = 0.5


def _numeric(df: pd.DataFrame, col: str) -> pd.Series:
    if col not in df.columns:
        return pd.Series(np.nan, index=df.index, dtype=float)
    return pd.to_numeric(df[col], errors="coerce")


def verify_factor_panel(
    df: pd.DataFrame,
    feature_cols: tuple[str, ...] | list[str],
    future_col: str | None = None,
) -> dict[str, object]:
    """返回结构化体检报告；``ok`` 为 False 表示存在阻断级问题。"""
    features = list(feature_cols)
    issues: list[str] = []
    warnings: list[str] = []

    report: dict[str, object] = {
        "ok": True,
        "rows": int(len(df)) if df is not None else 0,
        "issues": issues,
        "warnings": warnings,
        "missing_columns": [],
        "duplicate_keys": 0,
        "infinite_columns": {},
        "constant_columns": [],
        "ohlc_violations": 0,
        "coverage": {},
        "label_corr_max": None,
    }
    if df is None or df.empty:
        issues.append("数据集为空")
        report["ok"] = False
        return report

    missing_base = [col for col in _BASE_COLUMNS if col not in df.columns]
    missing_features = [col for col in features if col not in df.columns]
    missing = missing_base + missing_features
    if missing:
        report["missing_columns"] = missing
        issues.append(f"缺少列: {', '.join(missing)}")

    if {"symbol", "date"} <= set(df.columns):
        duplicates = int(df.duplicated(["symbol", "date"]).sum())
        report["duplicate_keys"] = duplicates
        if duplicates:
            issues.append(f"主键重复 {duplicates} 行")
    elif not missing_base:
        issues.append("无法校验主键：缺少 symbol/date")

    infinite: dict[str, int] = {}
    constant: list[str] = []
    coverage: dict[str, float] = {}
    for col in features:
        if col not in df.columns:
            continue
        values = _numeric(df, col)
        count = int(np.isinf(values).sum())
        if count:
            infinite[col] = count
            issues.append(f"{col} 含 {count} 个无穷值")
        coverage[col] = round(float(values.notna().mean()), 6)
        if values.notna().sum() and values.dropna().nunique() <= 1:
            constant.append(col)
            warnings.append(col)
        if coverage[col] < _MIN_COVERAGE:
            warnings.append(f"{col} 覆盖率仅 {coverage[col]:.1%}")
    report["infinite_columns"] = infinite
    report["constant_columns"] = constant
    report["coverage"] = coverage

    high = _numeric(df, "high")
    low = _numeric(df, "low")
    open_ = _numeric(df, "open")
    close = _numeric(df, "close")
    upper = pd.concat([open_, close], axis=1).max(axis=1)
    lower = pd.concat([open_, close], axis=1).min(axis=1)
    violations = (high < low) | (high < upper) | (low > lower)
    violations = violations & high.notna() & low.notna()
    report["ohlc_violations"] = int(violations.sum())
    if report["ohlc_violations"]:
        issues.append(f"OHLC 不自洽 {report['ohlc_violations']} 行")

    if future_col is not None and future_col in df.columns:
        future = _numeric(df, future_col)
        correlations = {}
        for col in features:
            if col not in df.columns:
                continue
            values = _numeric(df, col)
            if values.nunique(dropna=True) <= 1:
                continue
            corr = values.corr(future)
            if pd.notna(corr):
                correlations[col] = float(corr)
        if correlations:
            worst = max(correlations, key=lambda key: abs(correlations[key]))
            report["label_corr_max"] = abs(correlations[worst])
            if report["label_corr_max"] > _LABEL_CORR_LIMIT:
                issues.append(
                    f"疑似 label 泄漏: {worst} 与 {future_col} "
                    f"相关性 {correlations[worst]:+.3f}"
                )

    report["ok"] = not issues
    return report
