"""训练「大盘指数预测模型」（P0）：CatBoost / LightGBM / XGBoost + 基线。

协议（遵循 docs/大盘指数预测模型_CatBoost_LightGBM_XGBoost_训练规划_20261024.md）：
- 标签：信号日 T → T+1 收盘进场 → T+1+H 收盘出场；二分类 y=fwd_ret>0；
- 切分：purged walk-forward，按年测试（2022..2026），train = 测试年之前且
  留出 H+5 个交易日 embargo；验证 = train 末 126 个交易日（早停 + 校准）；
- 模型：LightGBM / XGBoost / CatBoost + 逻辑回归 + 规则（ret_20>0）+ 基准率；
- 指标：pooled OOS AUC / Brier / LogLoss / ECE / 命中率（p≥0.6）/ 平衡准确率，
  以及分折 AUC、long/flat H 日抽样收益；
- 只出报告，不写线上表、不接决策。

用法：
  python backend/scripts/market_regime/train_index_regime.py \
      --features /data/market_regime/features/index_regime_features.parquet \
      --out /data/market_regime/reports
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

META_COLS = {
    "trade_date",
    "index_code",
    "index_name",
    "close",
    "fwd_ret_5",
    "fwd_ret_10",
    "fwd_ret_20",
}
EMBARGO_EXTRA = 5  # 标签外再留 5 个交易日，防特征窗口重叠
VAL_DAYS = 126
HORIZONS = (5, 10, 20)


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


def _ece(y: np.ndarray, p: np.ndarray, bins: int = 10) -> float:
    y = np.asarray(y, float)
    p = np.asarray(p, float)
    ok = np.isfinite(p)
    y, p = y[ok], p[ok]
    if len(p) == 0:
        return float("nan")
    edges = np.linspace(0, 1, bins + 1)
    total = 0.0
    for i in range(bins):
        m = (p >= edges[i]) & (p < edges[i + 1] if i < bins - 1 else p <= edges[i + 1])
        if not m.any():
            continue
        total += (m.mean()) * abs(p[m].mean() - y[m].mean())
    return float(total)


def _brier(y: np.ndarray, p: np.ndarray) -> float:
    y = np.asarray(y, float)
    p = np.asarray(p, float)
    ok = np.isfinite(p)
    return float(np.mean((p[ok] - y[ok]) ** 2))


def _logloss(y: np.ndarray, p: np.ndarray) -> float:
    y = np.asarray(y, float)
    p = np.clip(np.asarray(p, float), 1e-6, 1 - 1e-6)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def _metrics(y: np.ndarray, p: np.ndarray, base_rate: float) -> dict[str, Any]:
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
    return {
        "n": int(len(y)),
        "base_rate": round(float(base_rate), 4),
        "auc": round(_auc(y, p), 4),
        "brier": round(_brier(y, p), 4),
        "brier_base": round(float(np.mean((base_rate - y.astype(float)) ** 2)), 4),
        "logloss": round(_logloss(y, p), 4),
        "ece": round(_ece(y, p), 4),
        "acc": round(float((pred == y).mean()), 4),
        "balanced_acc": round(float((tpr + tnr) / 2), 4),
        "up_recall": round(float(tpr), 4),
        "down_recall": round(float(tnr), 4),
        "p_ge_06_n": int(high.sum()),
        "p_ge_06_hit": round(float(y[high].mean()), 4) if high.any() else None,
        "p_le_04_n": int(low.sum()),
        "p_le_04_hit": round(float((~y[low]).mean()), 4) if low.any() else None,
    }


def _rule_signal(df: pd.DataFrame) -> np.ndarray:
    """规则基线：20 日动量为正 → 1，否则 0。"""
    return (df["ret_20"].fillna(0) > 0).astype(int).values


def _fit_predict(
    name: str,
    Xtr: pd.DataFrame,
    ytr: np.ndarray,
    Xval: pd.DataFrame,
    yval: np.ndarray,
    Xte: pd.DataFrame,
    seed: int = 42,
) -> np.ndarray:
    """训练单模型 + 验证集早停/校准，返回测试集 P(up)。"""
    from sklearn.isotonic import IsotonicRegression

    if name == "logistic":
        from sklearn.impute import SimpleImputer
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import StandardScaler

        pipe = Pipeline(
            [
                ("imp", SimpleImputer(strategy="median")),
                ("sc", StandardScaler()),
                ("clf", LogisticRegression(C=0.2, max_iter=2000, class_weight="balanced")),
            ]
        )
        pipe.fit(Xtr, ytr)
        raw_val = pipe.predict_proba(Xval)[:, 1]
        raw_te = pipe.predict_proba(Xte)[:, 1]
    elif name == "lgb":
        import lightgbm as lgb

        model = lgb.LGBMClassifier(
            n_estimators=400,
            learning_rate=0.03,
            num_leaves=15,
            min_child_samples=40,
            subsample=0.8,
            colsample_bytree=0.8,
            reg_lambda=1.0,
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
            n_estimators=400,
            learning_rate=0.03,
            max_depth=3,
            min_child_weight=20,
            subsample=0.8,
            colsample_bytree=0.8,
            reg_lambda=1.0,
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
            iterations=600,
            learning_rate=0.03,
            depth=4,
            l2_leaf_reg=3.0,
            random_seed=seed,
            verbose=False,
            allow_writing_files=False,
        )
        model.fit(Xtr, ytr, eval_set=(Xval, yval), early_stopping_rounds=50, verbose=False)
        raw_val = model.predict_proba(Xval)[:, 1]
        raw_te = model.predict_proba(Xte)[:, 1]
    else:
        raise ValueError(name)

    # 校准：验证集 isotonic（测试集仍完全 OOS）
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    try:
        iso.fit(raw_val, yval)
        return iso.predict(raw_te)
    except Exception:
        return raw_te


def run(features_path: Path, out_dir: Path, test_years: list[int], only_index: str = '', horizons: tuple[int, ...] = HORIZONS) -> dict[str, Any]:
    df = pd.read_parquet(features_path)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    feature_cols = [c for c in df.columns if c not in META_COLS]
    models = ("logistic", "lgb", "xgb", "cat")
    report: dict[str, Any] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "features": str(features_path),
        "feature_count": len(feature_cols),
        "test_years": test_years,
        "by_index": {},
    }

    if only_index:
        df = df[df["index_code"] == only_index]
    for code, g in df.groupby("index_code"):
        g = g.sort_values("trade_date").reset_index(drop=True)
        report["by_index"][code] = {}
        for h in horizons:
            label = f"fwd_ret_{h}"
            data = g.dropna(subset=[label]).copy()
            data["y"] = (data[label] > 0).astype(int)
            folds = []
            for year in test_years:
                test_start = pd.Timestamp(f"{year}-01-01")
                test_end = pd.Timestamp(f"{year}-12-31")
                # 用交易日数量做 embargo：取测试开始前的 H+5 行
                pre = data[data["trade_date"] < test_start]
                if len(pre) <= VAL_DAYS + 60:
                    continue
                val = pre.tail(VAL_DAYS)
                fit = pre.iloc[:-VAL_DAYS]
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
                Xtr, ytr = fit[feature_cols], fit["y"].values
                Xval, yval = val_es[feature_cols], val_es["y"].values
                Xte, yte = test[feature_cols], test["y"].values
                base_rate = float(ytr.mean())
                fold: dict[str, Any] = {
                    "year": year,
                    "train": [str(fit["trade_date"].min().date()), str(fit["trade_date"].max().date()), len(fit)],
                    "val": [str(val_es["trade_date"].min().date()), str(val_es["trade_date"].max().date()), len(val_es)],
                    "test": [str(test["trade_date"].min().date()), str(test["trade_date"].max().date()), len(test)],
                    "base_rate": round(base_rate, 4),
                    "models": {},
                }
                # 规则 / 基准率
                rule = _rule_signal(test)
                fold["models"]["rule"] = _metrics(yte, rule.astype(float), base_rate)
                fold["models"]["base_rate"] = _metrics(
                    yte, np.full(len(yte), base_rate), base_rate
                )
                for name in models:
                    try:
                        p = _fit_predict(name, Xtr, ytr, Xval, yval, Xte)
                        m = _metrics(yte, p, base_rate)
                        m["fold_auc"] = m.get("auc")
                        # long/flat：p>=0.6 持有 H 日，按 H 日抽样（非重叠）
                        samp = test.iloc[::h]
                        if len(samp):
                            ps = p[::h]
                            rets = samp[label].values
                            pos = (ps >= 0.6).astype(float)
                            strat = pos * rets
                            m["long_flat_mean_h"] = round(float(np.mean(strat)), 4)
                            m["buyhold_mean_h"] = round(float(np.mean(rets)), 4)
                            m["long_flat_hit"] = round(float(np.mean(strat > 0)), 4)
                        fold["models"][name] = m
                    except Exception as exc:  # noqa: BLE001 - 单模型失败不阻断
                        fold["models"][name] = {"error": f"{type(exc).__name__}: {exc}"}
                folds.append(fold)
            # 汇总：pooled 指标由分折模型指标加权近似 + 分折 AUC 列表
            summary: dict[str, Any] = {}
            for name in ("rule", "base_rate", *models):
                rows = [f["models"].get(name, {}) for f in folds]
                rows = [r for r in rows if r and "error" not in r]
                if not rows:
                    summary[name] = {"error": "no folds"}
                    continue
                a = [r["auc"] for r in rows if r.get("auc") == r.get("auc")]
                summary[name] = {
                    "folds": len(rows),
                    "auc_mean": round(float(np.mean(a)), 4) if a else None,
                    "auc_folds_ge_058": round(
                        float(np.mean([x >= 0.58 for x in a])), 3
                    )
                    if a
                    else None,
                    "brier_mean": round(float(np.mean([r["brier"] for r in rows])), 4),
                    "brier_base_mean": round(
                        float(np.mean([r["brier_base"] for r in rows])), 4
                    ),
                    "ece_mean": round(float(np.mean([r["ece"] for r in rows])), 4),
                    "acc_mean": round(float(np.mean([r["acc"] for r in rows])), 4),
                    "balanced_acc_mean": round(
                        float(np.mean([r["balanced_acc"] for r in rows])), 4
                    ),
                    "up_recall_mean": round(
                        float(np.mean([r["up_recall"] for r in rows])), 4
                    ),
                    "down_recall_mean": round(
                        float(np.mean([r["down_recall"] for r in rows])), 4
                    ),
                    "long_flat_mean_h": round(
                        float(np.mean([r.get("long_flat_mean_h", 0) for r in rows])), 4
                    ),
                    "buyhold_mean_h": round(
                        float(np.mean([r.get("buyhold_mean_h", 0) for r in rows])), 4
                    ),
                }
            report["by_index"][code][f"h{h}"] = {"folds": folds, "summary": summary}

    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    json_path = out_dir / f"index_regime_oos_{stamp}.json"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    md = _render_markdown(report)
    md_path = out_dir / f"index_regime_oos_{stamp}.md"
    md_path.write_text(md, encoding="utf-8")
    return {"json": str(json_path), "markdown": str(md_path), "report": report}


def _render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# 大盘指数预测模型 OOS 报告",
        "",
        f"- 生成时间：{report['generated_at']}",
        f"- 特征数：{report['feature_count']}",
        f"- 测试年：{', '.join(map(str, report['test_years']))}",
        "",
    ]
    for code, by_h in report["by_index"].items():
        lines.append(f"## {code}")
        lines.append("")
        for h, payload in by_h.items():
            lines.append(f"### {h}")
            lines.append("")
            lines.append(
                "| 模型 | folds | AUC均值 | AUC≥0.58折占比 | Brier | Brier基准 | ECE | 平衡准确率 | 上涨召回 | 下跌召回 | long/flat H日均值 | 买入持有H日均值 |"
            )
            lines.append(
                "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"
            )
            for name, s in payload["summary"].items():
                if "error" in s:
                    lines.append(f"| {name} | - | - | - | - | - | - | - | - | - | - | - |")
                    continue
                lines.append(
                    "| {name} | {folds} | {auc} | {ge} | {brier} | {bb} | {ece} | {ba} | {ur} | {dr} | {lf} | {bh} |".format(
                        name=name,
                        folds=s.get("folds"),
                        auc=s.get("auc_mean"),
                        ge=s.get("auc_folds_ge_058"),
                        brier=s.get("brier_mean"),
                        bb=s.get("brier_base_mean"),
                        ece=s.get("ece_mean"),
                        ba=s.get("balanced_acc_mean"),
                        ur=s.get("up_recall_mean"),
                        dr=s.get("down_recall_mean"),
                        lf=s.get("long_flat_mean_h"),
                        bh=s.get("buyhold_mean_h"),
                    )
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
    args = parser.parse_args()
    years = [int(x) for x in args.years.split(",") if x.strip()]
    horizons = tuple(int(x) for x in args.horizons.split(",") if x.strip())
    result = run(Path(args.features), Path(args.out), years, args.index, horizons)
    print(json.dumps({"json": result["json"], "markdown": result["markdown"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
