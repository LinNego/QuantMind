"""训练「大盘指数 / 行业板块」预测模型（v2：校准 + 多折 + 波动/风险目标）。

协议（docs/大盘指数预测模型_CatBoost_LightGBM_XGBoost_训练规划_20261024.md）：
- 目标：
  * direction：T+1 进场、T+1+H 出场，y=fwd_ret>0；
  * vol：未来 H 日已实现波动是否高于滚动 252 日 67% 分位（vol_high）；
  * downrisk：窗口最大回撤是否 ≤ -5%（down_risk）；
- 切分：purged walk-forward，支持按年/半年测试；train = 测试期之前，
  验证 = train 末 126 个交易日（早停 + 校准），再留 H+5 个交易日 embargo；
- 模型：LightGBM / XGBoost / CatBoost + 逻辑回归 + 规则 + 基准率；
- 校准：Platt / Isotonic / 先验偏移（PIT 滚动基础率）/ 混合，验证折内 K 折
  交叉验证按 ECE→Brier 选择；测试折完全 OOS；
- 指标：AUC / Brier / Brier skill / LogLoss / ECE / 命中率 / 平衡准确率，
  方向目标另报 long/flat；vol 目标另报 LGBM 回归 R²/QLIKE；
- 只出报告，不写线上表、不接决策。

用法：
  python backend/scripts/market_regime/train_index_regime.py \
      --features /data/market_regime/features/index_regime_features.parquet \
      --out /data/market_regime/reports --target direction --fold-freq half
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from backend.scripts.market_regime.calibration import (
    brier_skill,
    ece,
    logloss,
    reliability_table,
    select_calibrator,
)

META_COLS = {
    "trade_date",
    "index_code",
    "index_name",
    "close",
    "sector_code",
    "sec_eq_ret",
}
LABEL_PREFIXES = ("fwd_ret_", "fwd_vol_", "fwd_mdd_", "vol_high_", "down_risk_")
EMBARGO_EXTRA = 5  # 标签外再留 5 个交易日，防特征窗口重叠
VAL_DAYS = 252  # v2：校准样本从 126 扩到 252，降低 Platt/Isotonic 方差
HORIZONS = (1, 5, 10, 20)

TARGET_CONFIG: dict[str, dict[str, Any]] = {
    "direction": {
        "label": "fwd_ret_{h}",
        "auc_pass": 0.58,
        "description": "未来 H 日收益 > 0",
    },
    "vol": {
        "label": "vol_high_{h}",
        "auc_pass": 0.65,
        "description": "未来 H 日已实现波动 > 滚动 252 日 67% 分位",
    },
    "downrisk": {
        "label": "down_risk_{h}",
        "auc_pass": 0.60,
        "description": "未来 H 日窗口最大回撤 ≤ -5%",
    },
}


def _auc(y: np.ndarray, p: np.ndarray) -> float:
    y = np.asarray(y, bool)
    p = np.asarray(p, float)
    ok = np.isfinite(p)
    y, p = y[ok], p[ok]
    n1, n0 = int(y.sum()), int((~y).sum())
    if n1 == 0 or n0 == 0:
        return float("nan")
    ranks = pd.Series(p).rank(method="average").values
    return float((ranks[y].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def _metrics(y: np.ndarray, p: np.ndarray, base_rate: float, *, auc_pass: float) -> dict[str, Any]:
    y = np.asarray(y, bool)
    p = np.asarray(p, float)
    ok = np.isfinite(p)
    y, p = y[ok], p[ok]
    if len(y) == 0:
        return {}
    pred = p >= 0.5
    up = y
    down = ~y
    tp = int((pred & up).sum())
    tn = int((~pred & down).sum())
    fp = int((pred & down).sum())
    fn = int((~pred & up).sum())
    tpr = tp / max(1, tp + fn)
    tnr = tn / max(1, tn + fp)
    high = p >= 0.6
    low = p <= 0.4
    auc = _auc(y, p)
    return {
        "n": int(len(y)),
        "base_rate": round(float(base_rate), 4),
        "auc": round(auc, 4),
        "auc_pass": bool(np.isfinite(auc) and auc >= auc_pass),
        "brier": round(float(np.mean((p - y.astype(float)) ** 2)), 4),
        "brier_base": round(float(np.mean((base_rate - y.astype(float)) ** 2)), 4),
        "brier_skill": round(brier_skill(y.astype(float), p, base_rate), 4),
        "logloss": round(logloss(y.astype(float), p), 4),
        "ece": round(ece(y.astype(float), p), 4),
        "acc": round(float((pred == y).mean()), 4),
        "balanced_acc": round(float((tpr + tnr) / 2), 4),
        "up_recall": round(float(tpr), 4),
        "down_recall": round(float(tnr), 4),
        "p_ge_06_n": int(high.sum()),
        "p_ge_06_hit": round(float(y[high].mean()), 4) if high.any() else None,
        "p_le_04_n": int(low.sum()),
        "p_le_04_hit": round(float((~y[low]).mean()), 4) if low.any() else None,
    }


def _rule_signal(df: pd.DataFrame, target: str, h: int) -> np.ndarray:
    """规则基线：方向=20 日动量为正；波动/回撤=当前 20 日波动处于近一年高分位。"""
    if target == "direction":
        return (df["ret_20"].fillna(0) > 0).astype(int).values
    vol = (df["vol_20"] * np.sqrt(252)).astype(float)
    threshold = vol.rolling(252, min_periods=120).quantile(0.67)
    return (vol > threshold).fillna(False).astype(int).values


def _select_features(Xtr: pd.DataFrame, Xval: pd.DataFrame, Xte: pd.DataFrame):
    """折内特征筛选：覆盖≥50%、非常量、去高相关（|r|>0.98）。"""
    keep = [
        c
        for c in Xtr.columns
        if Xtr[c].notna().mean() >= 0.5 and Xtr[c].nunique(dropna=True) > 1
    ]
    Xtr, Xval, Xte = Xtr[keep], Xval[keep], Xte[keep]
    if len(keep) > 2:
        corr = Xtr.corr().abs()
        upper = corr.where(np.triu(np.ones(corr.shape), k=1).astype(bool))
        drop = {c for c in upper.columns if (upper[c] > 0.98).any()}
        keep = [c for c in keep if c not in drop]
    return Xtr[keep], Xval[keep], Xte[keep]


def _fit_predict(
    name: str,
    Xtr: pd.DataFrame,
    ytr: np.ndarray,
    Xval: pd.DataFrame,
    yval: np.ndarray,
    Xte: pd.DataFrame,
    *,
    target_rates: dict[str, float] | None,
    seed: int = 42,
) -> dict[str, Any]:
    """训练单模型，返回未校准与校准后的测试集 P(up) 及校准诊断。"""
    from sklearn.linear_model import LogisticRegression

    if name == "logistic":
        from sklearn.impute import SimpleImputer
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import StandardScaler

        pipe = Pipeline(
            [
                ("imp", SimpleImputer(strategy="median")),
                ("sc", StandardScaler()),
                ("clf", LogisticRegression(C=0.08, max_iter=2000)),
            ]
        )
        pipe.fit(Xtr, ytr)
        raw_val = pipe.predict_proba(Xval)[:, 1]
        raw_te = pipe.predict_proba(Xte)[:, 1]
    elif name == "lgb":
        import lightgbm as lgb

        model = lgb.LGBMClassifier(
            n_estimators=300,
            learning_rate=0.03,
            num_leaves=7,
            min_child_samples=80,
            subsample=0.8,
            colsample_bytree=0.6,
            reg_lambda=3.0,
            random_state=seed,
            n_jobs=4,
            verbose=-1,
        )
        model.fit(
            Xtr,
            ytr,
            eval_set=[(Xval, yval)],
            eval_metric="auc",
            callbacks=[lgb.early_stopping(50, verbose=False)],
        )
        raw_val = model.predict_proba(Xval)[:, 1]
        raw_te = model.predict_proba(Xte)[:, 1]
    elif name == "xgb":
        from xgboost import XGBClassifier

        model = XGBClassifier(
            n_estimators=300,
            learning_rate=0.03,
            max_depth=2,
            min_child_weight=40,
            subsample=0.8,
            colsample_bytree=0.6,
            reg_lambda=3.0,
            random_state=seed,
            n_jobs=4,
            eval_metric="auc",
            early_stopping_rounds=50,
        )
        model.fit(Xtr, ytr, eval_set=[(Xval, yval)], verbose=False)
        raw_val = model.predict_proba(Xval)[:, 1]
        raw_te = model.predict_proba(Xte)[:, 1]
    elif name == "cat":
        from catboost import CatBoostClassifier

        model = CatBoostClassifier(
            iterations=400,
            learning_rate=0.03,
            depth=3,
            l2_leaf_reg=10.0,
            random_seed=seed,
            verbose=False,
            allow_writing_files=False,
            thread_count=4,
        )
        model.fit(Xtr, ytr, eval_set=(Xval, yval), early_stopping_rounds=50, verbose=False)
        raw_val = model.predict_proba(Xval)[:, 1]
        raw_te = model.predict_proba(Xte)[:, 1]
    else:
        raise ValueError(name)

    choice = select_calibrator(
        np.asarray(raw_val),
        np.asarray(yval),
        np.asarray(raw_te),
        target_rates=target_rates,
    )
    return {
        "p_raw": np.asarray(raw_te, dtype=float),
        "p": np.asarray(choice.p_test, dtype=float),
        "calibrator": choice.name,
        "calibration_cv": choice.candidates,
    }


def _fit_vol_regression(
    Xtr: pd.DataFrame,
    ytr: np.ndarray,
    Xval: pd.DataFrame,
    yval: np.ndarray,
    Xte: pd.DataFrame,
    yte: np.ndarray,
    baseline: np.ndarray,
    seed: int = 42,
) -> dict[str, Any]:
    """vol 目标的回归分支：预测 log(未来已实现波动)，基线=当前 20 日波动。"""
    import lightgbm as lgb

    model = lgb.LGBMRegressor(
        n_estimators=500,
        learning_rate=0.03,
        num_leaves=7,
        min_child_samples=60,
        subsample=0.8,
        colsample_bytree=0.6,
        reg_lambda=3.0,
        random_state=seed,
        n_jobs=4,
        verbose=-1,
    )
    model.fit(
        Xtr,
        ytr,
        eval_set=[(Xval, yval)],
        eval_metric="l2",
        callbacks=[lgb.early_stopping(50, verbose=False)],
    )
    pred_log = model.predict(Xte)
    pred = np.exp(np.clip(pred_log, -10, 5))
    true = np.exp(np.clip(yte, -10, 5))
    base = np.clip(baseline, 1e-4, None)
    pred_var = np.clip(pred**2, 1e-8, None)
    true_var = np.clip(true**2, 1e-8, None)
    qlike = float(np.mean(np.log(pred_var) + true_var / pred_var))
    base_var = np.clip(base**2, 1e-8, None)
    qlike_base = float(np.mean(np.log(base_var) + true_var / base_var))
    ss_res = float(np.sum((pred_log - yte) ** 2))
    ss_tot = float(np.sum((yte - np.mean(yte)) ** 2))
    return {
        "n": int(len(yte)),
        "r2_log": round(1 - ss_res / ss_tot, 4) if ss_tot > 0 else None,
        "rmse_vol": round(float(np.sqrt(np.mean((pred - true) ** 2))), 4),
        "mae_vol": round(float(np.mean(np.abs(pred - true))), 4),
        "qlike": round(qlike, 4),
        "qlike_base": round(qlike_base, 4),
        "qlike_skill": round(1 - qlike / qlike_base, 4) if qlike_base > 0 else None,
        "corr": round(float(np.corrcoef(pred, true)[0, 1]), 4)
        if len(pred) > 2
        else None,
    }


def _target_label(target: str, h: int) -> str:
    return TARGET_CONFIG[target]["label"].format(h=h)


def _label_vector(data: pd.DataFrame, target: str, h: int) -> pd.Series:
    label = _target_label(target, h)
    if target == "direction":
        return (data[label] > 0).astype(int)
    return data[label].astype(float)


BASE_WINDOWS = (60, 90, 126, 252, 504, 756)


def _rolling_base_rates(
    fit: pd.DataFrame, target: str, h: int
) -> dict[str, float]:
    """PIT 滚动基础率（多窗口）：只取训练折内已观测标签的最近 window 行。"""
    y = _label_vector(fit, target, h).dropna()
    if y.empty:
        return {"base_252": 0.5}
    return {
        f"base_{w}": float(np.clip(y.tail(w).mean(), 1e-4, 1 - 1e-4))
        for w in BASE_WINDOWS
    }


def _rolling_base_rate(fit: pd.DataFrame, target: str, h: int, window: int = 252) -> float:
    y = _label_vector(fit, target, h).dropna()
    if y.empty:
        return 0.5
    return float(np.clip(y.tail(window).mean(), 1e-4, 1 - 1e-4))


def _test_windows(years: list[int], fold_freq: str) -> list[tuple[pd.Timestamp, pd.Timestamp, str]]:
    windows: list[tuple[pd.Timestamp, pd.Timestamp, str]] = []
    for year in years:
        if fold_freq == "half":
            windows.append(
                (pd.Timestamp(f"{year}-01-01"), pd.Timestamp(f"{year}-06-30"), f"{year}H1")
            )
            windows.append(
                (pd.Timestamp(f"{year}-07-01"), pd.Timestamp(f"{year}-12-31"), f"{year}H2")
            )
        else:
            windows.append(
                (pd.Timestamp(f"{year}-01-01"), pd.Timestamp(f"{year}-12-31"), str(year))
            )
    return windows


def _finite(values: list[Any]) -> list[float]:
    return [
        float(v)
        for v in values
        if v is not None and isinstance(v, (int, float)) and np.isfinite(v)
    ]


def _summarize(name: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    a = _finite([r.get("auc") for r in rows])
    a_raw = _finite([r.get("auc_raw") for r in rows])
    ece_raw = _finite([r.get("ece_raw") for r in rows])
    ece_cal = _finite([r.get("ece") for r in rows])
    calibrators: dict[str, int] = {}
    for r in rows:
        calibrators[r.get("calibrator", "raw")] = calibrators.get(r.get("calibrator", "raw"), 0) + 1
    return {
        "folds": len(rows),
        "auc_mean": round(float(np.mean(a)), 4) if a else None,
        "auc_raw_mean": round(float(np.mean(a_raw)), 4) if a_raw else None,
        "auc_folds_ge_pass": round(float(np.mean([r.get("auc_pass", False) for r in rows])), 3)
        if rows
        else None,
        "brier_mean": round(float(np.mean([r["brier"] for r in rows])), 4),
        "brier_base_mean": round(float(np.mean([r["brier_base"] for r in rows])), 4),
        "brier_skill_mean": round(
            float(np.mean([r["brier_skill"] for r in rows])), 4
        ),
        "ece_raw_mean": round(float(np.mean(ece_raw)), 4) if ece_raw else None,
        "ece_mean": round(float(np.mean(ece_cal)), 4) if ece_cal else None,
        "ece_improved_folds": round(
            float(
                np.mean(
                    [
                        r["ece"] <= r.get("ece_raw", r["ece"])
                        for r in rows
                        if r.get("ece") == r.get("ece")
                    ]
                )
            ),
            3,
        )
        if ece_cal
        else None,
        "acc_mean": round(float(np.mean([r["acc"] for r in rows])), 4),
        "balanced_acc_mean": round(
            float(np.mean([r["balanced_acc"] for r in rows])), 4
        ),
        "up_recall_mean": round(float(np.mean([r["up_recall"] for r in rows])), 4),
        "down_recall_mean": round(float(np.mean([r["down_recall"] for r in rows])), 4),
        "long_flat_mean_h": round(
            float(np.mean([r.get("long_flat_mean_h", 0) for r in rows])), 4
        )
        if name != "base_rate"
        else None,
        "buyhold_mean_h": round(
            float(np.mean([r.get("buyhold_mean_h", 0) for r in rows])), 4
        )
        if name != "base_rate"
        else None,
        "calibrators": calibrators,
    }


def _feature_columns(df: pd.DataFrame) -> list[str]:
    """特征列：排除元数据、所有标签前缀、非数值列。"""
    return [
        c
        for c in df.columns
        if c not in META_COLS
        and not c.startswith(LABEL_PREFIXES)
        and pd.api.types.is_numeric_dtype(df[c])
    ]


def run(
    features_path: Path,
    out_dir: Path,
    test_years: list[int],
    only_index: str = "",
    horizons: tuple[int, ...] = HORIZONS,
    *,
    target: str = "direction",
    fold_freq: str = "year",
    models: tuple[str, ...] = ("logistic", "lgb", "xgb", "cat"),
) -> dict[str, Any]:
    if target not in TARGET_CONFIG:
        raise ValueError(f"未知 target：{target}")
    df = pd.read_parquet(features_path)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    # 所有标签列（方向/波动/回撤）绝不能进特征
    feature_cols = _feature_columns(df)
    report: dict[str, Any] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "features": str(features_path),
        "feature_count": len(feature_cols),
        "test_years": test_years,
        "fold_freq": fold_freq,
        "target": target,
        "target_description": TARGET_CONFIG[target]["description"],
        "by_index": {},
    }

    if only_index:
        df = df[df["index_code"] == only_index]
    for code, g in df.groupby("index_code"):
        g = g.sort_values("trade_date").reset_index(drop=True)
        report["by_index"][code] = {}
        for h in horizons:
            label = _target_label(target, h)
            if label not in g.columns:
                report["by_index"][code][f"h{h}"] = {"error": f"缺少标签 {label}"}
                continue
            data = g.dropna(subset=[label]).copy()
            data["y"] = _label_vector(data, target, h).astype(int)
            folds = []
            pooled: dict[str, dict[str, list[np.ndarray]]] = {
                name: {"y": [], "p": [], "p_raw": []}
                for name in ("rule", "base_rate", "rolling_base", *models)
            }
            for test_start, test_end, fold_name in _test_windows(test_years, fold_freq):
                pre = data[data["trade_date"] < test_start]
                if len(pre) <= VAL_DAYS + 60:
                    continue
                val = pre.tail(VAL_DAYS)
                fit = pre.iloc[:-VAL_DAYS]
                # 再 purge 掉训练折末尾 h 行：这些行的标签会用到验证期价格
                if len(fit) > h + 60:
                    fit = fit.iloc[:-h]
                if len(fit) < 300:
                    continue
                # 去掉验证集末尾 H+5 行，避免与测试期标签重叠
                embargo_rows = min(h + EMBARGO_EXTRA, max(0, len(val) - 30))
                val_es = val.iloc[: len(val) - embargo_rows] if embargo_rows else val
                if len(val_es) < 40:
                    continue
                test = data[
                    (data["trade_date"] >= test_start) & (data["trade_date"] <= test_end)
                ]
                if len(test) < 20:
                    continue
                if fit["y"].nunique() < 2 or val_es["y"].nunique() < 2:
                    continue
                Xtr, Xval, Xte = _select_features(
                    fit[feature_cols], val_es[feature_cols], test[feature_cols]
                )
                ytr = fit["y"].values
                yval = val_es["y"].values
                yte = test["y"].values
                base_rate = float(ytr.mean())
                target_rates = _rolling_base_rates(fit, target, h)
                target_rate = target_rates.get("base_252", 0.5)
                fold: dict[str, Any] = {
                    "fold": fold_name,
                    "train": [
                        str(fit["trade_date"].min().date()),
                        str(fit["trade_date"].max().date()),
                        len(fit),
                    ],
                    "val": [
                        str(val_es["trade_date"].min().date()),
                        str(val_es["trade_date"].max().date()),
                        len(val_es),
                    ],
                    "test": [
                        str(test["trade_date"].min().date()),
                        str(test["trade_date"].max().date()),
                        len(test),
                    ],
                    "base_rate": round(base_rate, 4),
                    "rolling_base_rate": round(target_rate, 4),
                    "models": {},
                }
                rule_all = pd.Series(
                    _rule_signal(data, target, h), index=data.index
                )
                rule = rule_all.loc[test.index].values
                fold["models"]["rule"] = _metrics(
                    yte, rule.astype(float), base_rate, auc_pass=TARGET_CONFIG[target]["auc_pass"]
                )
                fold["models"]["base_rate"] = _metrics(
                    yte,
                    np.full(len(yte), base_rate),
                    base_rate,
                    auc_pass=TARGET_CONFIG[target]["auc_pass"],
                )
                fold["models"]["rolling_base"] = _metrics(
                    yte,
                    np.full(len(yte), target_rate),
                    base_rate,
                    auc_pass=TARGET_CONFIG[target]["auc_pass"],
                )
                pooled["rule"]["y"].append(yte)
                pooled["rule"]["p"].append(rule.astype(float))
                pooled["rule"]["p_raw"].append(rule.astype(float))
                pooled["base_rate"]["y"].append(yte)
                pooled["base_rate"]["p"].append(np.full(len(yte), base_rate))
                pooled["base_rate"]["p_raw"].append(np.full(len(yte), base_rate))
                pooled["rolling_base"]["y"].append(yte)
                pooled["rolling_base"]["p"].append(np.full(len(yte), target_rate))
                pooled["rolling_base"]["p_raw"].append(np.full(len(yte), target_rate))
                for name in models:
                    try:
                        pred = _fit_predict(
                            name,
                            Xtr,
                            ytr,
                            Xval,
                            yval,
                            Xte,
                            target_rates=target_rates,
                        )
                        m = _metrics(
                            yte,
                            pred["p"],
                            base_rate,
                            auc_pass=TARGET_CONFIG[target]["auc_pass"],
                        )
                        m["ece_raw"] = round(
                            ece(yte.astype(float), pred["p_raw"]), 4
                        )
                        m["auc_raw"] = round(_auc(yte, pred["p_raw"]), 4)
                        # 信号判据必须看原始排序能力；校准后 AUC 可能因收缩而变
                        m["auc_pass"] = bool(
                            m["auc_raw"] >= TARGET_CONFIG[target]["auc_pass"]
                        )
                        m["calibrator"] = pred["calibrator"]
                        m["calibration_cv"] = pred["calibration_cv"]
                        m["fold_auc"] = m.get("auc")
                        pooled[name]["y"].append(yte)
                        pooled[name]["p"].append(pred["p"])
                        pooled[name]["p_raw"].append(pred["p_raw"])
                        if target == "direction":
                            samp = test.iloc[::h]
                            if len(samp):
                                ps = pred["p"][::h]
                                rets = samp[f"fwd_ret_{h}"].values
                                pos = (ps >= 0.6).astype(float)
                                strat = pos * rets
                                m["long_flat_mean_h"] = round(float(np.mean(strat)), 4)
                                m["buyhold_mean_h"] = round(float(np.mean(rets)), 4)
                                m["long_flat_hit"] = round(float(np.mean(strat > 0)), 4)
                        fold["models"][name] = m
                    except Exception as exc:  # noqa: BLE001 - 单模型失败不阻断
                        fold["models"][name] = {"error": f"{type(exc).__name__}: {exc}"}

                if target == "vol":
                    try:
                        y_vol_tr = np.log(
                            np.clip(fit[f"fwd_vol_{h}"].values.astype(float), 1e-6, None)
                        )
                        y_vol_val = np.log(
                            np.clip(
                                val_es[f"fwd_vol_{h}"].values.astype(float), 1e-6, None
                            )
                        )
                        y_vol_te = np.log(
                            np.clip(test[f"fwd_vol_{h}"].values.astype(float), 1e-6, None)
                        )
                        baseline = (
                            test["vol_20"].fillna(0).values * np.sqrt(252)
                        )
                        fold["vol_regression"] = _fit_vol_regression(
                            Xtr,
                            y_vol_tr,
                            Xval,
                            y_vol_val,
                            Xte,
                            y_vol_te,
                            baseline,
                        )
                    except Exception as exc:  # noqa: BLE001 - 回归失败不阻断
                        fold["vol_regression"] = {
                            "error": f"{type(exc).__name__}: {exc}"
                        }
                folds.append(fold)

            summary: dict[str, Any] = {}
            for name in ("rule", "base_rate", "rolling_base", *models):
                rows = [f["models"].get(name, {}) for f in folds]
                rows = [r for r in rows if r and "error" not in r]
                if not rows:
                    summary[name] = {"error": "no folds"}
                    continue
                summary[name] = _summarize(name, rows)
                acc = pooled.get(name)
                if acc and acc["y"]:
                    y_all = np.concatenate(acc["y"])
                    p_all = np.concatenate(acc["p"])
                    p_raw_all = np.concatenate(acc["p_raw"])
                    pm = _metrics(
                        y_all,
                        p_all,
                        float(y_all.mean()),
                        auc_pass=TARGET_CONFIG[target]["auc_pass"],
                    )
                    pm["auc_raw"] = round(_auc(y_all, p_raw_all), 4)
                    pm["ece_raw"] = round(
                        ece(y_all.astype(float), p_raw_all), 4
                    )
                    summary[name]["pooled"] = pm
            if target == "vol":
                reg_rows = [
                    f["vol_regression"]
                    for f in folds
                    if f.get("vol_regression") and "error" not in f["vol_regression"]
                ]
                if reg_rows:
                    r2 = _finite([r.get("r2_log") for r in reg_rows])
                    qs = _finite([r.get("qlike_skill") for r in reg_rows])
                    corr = _finite([r.get("corr") for r in reg_rows])
                    summary["vol_regression"] = {
                        "folds": len(reg_rows),
                        "r2_log_mean": round(float(np.mean(r2)), 4) if r2 else None,
                        "qlike_skill_mean": round(float(np.mean(qs)), 4) if qs else None,
                        "corr_mean": round(float(np.mean(corr)), 4) if corr else None,
                    }
            report["by_index"][code][f"h{h}"] = {"folds": folds, "summary": summary}

    report["kill_criteria"] = _kill_criteria(report)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    json_path = out_dir / f"index_regime_{target}_{stamp}.json"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    md = _render_markdown(report)
    md_path = out_dir / f"index_regime_{target}_{stamp}.md"
    md_path.write_text(md, encoding="utf-8")
    return {"json": str(json_path), "markdown": str(md_path), "report": report}


def _kill_criteria(report: dict[str, Any]) -> dict[str, Any]:
    """按目标汇总杀判据：AUC 折占比、Brier skill、ECE。"""
    target = report["target"]
    auc_pass = TARGET_CONFIG[target]["auc_pass"]
    rows: list[dict[str, Any]] = []
    for code, by_h in report["by_index"].items():
        for h_key, payload in by_h.items():
            summary = payload.get("summary", {})
            best: tuple[str, dict[str, Any]] | None = None
            best_score = -np.inf
            for name in ("lgb", "xgb", "cat", "logistic"):
                s = summary.get(name, {})
                if not s or "auc_mean" not in s or s.get("auc_mean") is None:
                    continue
                score = s.get("auc_raw_mean", s.get("auc_mean"))
                if best is None or score > best_score:
                    best = (name, s)
                    best_score = score
            if best is None:
                continue
            name, s = best
            pooled = s.get("pooled", {})
            auc_signal = s.get("auc_raw_mean", pooled.get("auc_raw"))
            # 校准质量用「逐折 ECE 均值」：合并 ECE 会因不同折基础率相互抵消而虚低
            skill = s.get("brier_skill_mean")
            ece_cal = s.get("ece_mean")
            passed = (
                (auc_signal or 0) >= auc_pass
                and (s.get("auc_folds_ge_pass") or 0) >= 0.6
                and (skill or -1) >= 0.05
                and ece_cal is not None
                and ece_cal <= 0.05
            )
            rows.append(
                {
                    "index": code,
                    "horizon": h_key,
                    "best_model": name,
                    "auc_mean": s.get("auc_mean"),
                    "auc_raw_mean": auc_signal,
                    "auc_raw_pooled": pooled.get("auc_raw"),
                    "auc_cal_pooled": pooled.get("auc"),
                    "auc_folds_ge_pass": s.get("auc_folds_ge_pass"),
                    "brier_skill_mean": skill,
                    "brier_skill_pooled": pooled.get("brier_skill"),
                    "ece_mean": ece_cal,
                    "ece_pooled": pooled.get("ece"),
                    "passed": bool(passed),
                }
            )
    total = len(rows)
    passed = sum(1 for r in rows if r["passed"])
    return {
        "target": target,
        "auc_threshold": auc_pass,
        "criteria": "AUC≥阈值 且 ≥60% 折通过 且 Brier skill≥5% 且 ECE≤0.05",
        "rows": rows,
        "passed": passed,
        "total": total,
        "pass_ratio": round(passed / total, 3) if total else None,
    }


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def _render_markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# 大盘/行业预测模型 OOS 报告（{report['target']}）",
        "",
        f"- 生成时间：{report['generated_at']}",
        f"- 目标：{report['target_description']}",
        f"- 特征数：{report['feature_count']}",
        f"- 测试期：{report['fold_freq']} 折，年份 {', '.join(map(str, report['test_years']))}",
        "",
    ]
    kill = report.get("kill_criteria", {})
    lines.append(
        f"**杀判据**：{kill.get('criteria')}；通过 {kill.get('passed')}/{kill.get('total')}"
        f"（{_fmt(kill.get('pass_ratio'))}）"
    )
    lines.append("")
    for code, by_h in report["by_index"].items():
        lines.append(f"## {code}")
        lines.append("")
        for h, payload in by_h.items():
            lines.append(f"### {h}")
            lines.append("")
            if "error" in payload:
                lines.append(f"_{payload['error']}_")
                lines.append("")
                continue
            lines.append(
                "| 模型 | folds | AUC均值 | AUC原始合并 | AUC达标折占比 | Brier合并 | Brier skill折均 | ECE原始折均 | ECE校准折均 | ECE校准合并 | 校准器 | 平衡准确率 | 上涨召回 | 下跌召回 | long/flat H日 | 买入持有 H日 |"
            )
            lines.append(
                "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|---:|---:|"
            )
            for name, s in payload["summary"].items():
                if name == "vol_regression":
                    continue
                if "error" in s:
                    lines.append(f"| {name} | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - |")
                    continue
                pooled = s.get("pooled", {})
                lines.append(
                    "| {name} | {folds} | {auc} | {aucr} | {ge} | {brier} | {bs} | {er} | {ec} | {ecp} | {cal} | {ba} | {ur} | {dr} | {lf} | {bh} |".format(
                        name=name,
                        folds=s.get("folds"),
                        auc=_fmt(s.get("auc_mean")),
                        aucr=_fmt(pooled.get("auc_raw")),
                        ge=_fmt(s.get("auc_folds_ge_pass")),
                        brier=_fmt(pooled.get("brier")),
                        bs=_fmt(s.get("brier_skill_mean")),
                        er=_fmt(s.get("ece_raw_mean")),
                        ec=_fmt(s.get("ece_mean")),
                        ecp=_fmt(pooled.get("ece")),
                        cal=",".join(f"{k}:{v}" for k, v in (s.get("calibrators") or {}).items()),
                        ba=_fmt(s.get("balanced_acc_mean")),
                        ur=_fmt(s.get("up_recall_mean")),
                        dr=_fmt(s.get("down_recall_mean")),
                        lf=_fmt(s.get("long_flat_mean_h")),
                        bh=_fmt(s.get("buyhold_mean_h")),
                    )
                )
            if "vol_regression" in payload["summary"]:
                reg = payload["summary"]["vol_regression"]
                lines.append("")
                lines.append(
                    f"波动回归：R²(log)={_fmt(reg.get('r2_log_mean'))}，"
                    f"QLIKE skill={_fmt(reg.get('qlike_skill_mean'))}，"
                    f"corr={_fmt(reg.get('corr_mean'))}，folds={reg.get('folds')}"
                )
            lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--features", default="/data/market_regime/features/index_regime_features.parquet"
    )
    parser.add_argument("--out", default="/data/market_regime/reports")
    parser.add_argument("--years", default="2022,2023,2024,2025,2026")
    parser.add_argument("--index", default="")
    parser.add_argument("--horizons", default="5,10,20")
    parser.add_argument(
        "--target", choices=tuple(TARGET_CONFIG), default="direction"
    )
    parser.add_argument("--fold-freq", choices=("year", "half"), default="year")
    args = parser.parse_args()
    years = [int(x) for x in args.years.split(",") if x.strip()]
    horizons = tuple(int(x) for x in args.horizons.split(",") if x.strip())
    result = run(
        Path(args.features),
        Path(args.out),
        years,
        args.index,
        horizons,
        target=args.target,
        fold_freq=args.fold_freq,
    )
    print(
        json.dumps(
            {"json": result["json"], "markdown": result["markdown"]},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
