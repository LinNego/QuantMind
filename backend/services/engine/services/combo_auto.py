"""系统自动识别「同特征不同模型」的组合候选。

不训练任何新模型，只对**已注册且可推理**的模型做分组：

- 分组键 ``(market, factor_schema_hash, data_source, feature_count, target_horizon_days)``
  —— 「同特征」的硬定义，不靠模型名猜；
- 组内 ≥3 个模型才生成模板：全组等权 / 树族 / 线性族 / 深度族 / 高共识 / 截尾；
- 模板 ID 只由签名+周期+类型决定（``sys:{hash8}:{horizon}:{kind}``），
  证据等级变化不改 ID，用户收藏不失效；
- 等级只读 P1 证据包（``evidence.json``），不现算；无证据 → ``unverified``，
  且**不显示「推荐」字样**（K-R4）。

纯数据组装；模型元数据由调用方（service）传入，便于单测。
"""

from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path
from typing import Any
from collections.abc import Iterable, Mapping, Sequence

SIGNATURE_KEYS = (
    "market",
    "factor_schema_hash",
    "data_source",
    "feature_count",
    "target_horizon_days",
)

TREE_TYPES = frozenset(
    {"lightgbm", "xgboost", "catboost", "random_forest", "tabnet", "extra_trees"}
)
LINEAR_TYPES = frozenset({"linear", "ridge", "lasso", "elasticnet", "logistic"})
DL_TYPES = frozenset(
    {"nativetft", "tft", "lstm", "gru", "alstm", "tcn", "transformer", "mlp"}
)

#: 模型类型 → 证据包里的成员别名（与 P1 冻结配置一致）。
TYPE_ALIASES = {
    "lightgbm": "LGB",
    "xgboost": "XGB",
    "catboost": "CAT",
    "nativetft": "TFT",
    "tft": "TFT",
    "random_forest": "RF",
    "linear": "LIN",
    "mlp": "MLP",
    "gru": "GRU",
    "lstm": "LSTM",
    "alstm": "ALSTM",
    "tabnet": "TAB",
    "tcn": "TCN",
    "transformer": "TRF",
}

_EVIDENCE_ENV = "QM_COMBO_EVIDENCE_FILE"
_DEFAULT_EVIDENCE = "/data/reports/combo_nextday_20261021/evidence.json"

_ENSEMBLE_MARKERS = ("is_ensemble", "ensemble", "stacking")


def evidence_path() -> Path:
    """证据包路径：``QM_COMBO_EVIDENCE_FILE`` 优先，缺省取 P1 产物落点。"""
    raw = str(os.getenv(_EVIDENCE_ENV) or "").strip()
    return Path(raw) if raw else Path(_DEFAULT_EVIDENCE)


def load_evidence(path: Path | None = None) -> dict[str, Any] | None:
    """读取 P1 证据包；缺失/损坏返回 None（不现算、不猜测）。"""
    target = path or evidence_path()
    try:
        if not target.is_file():
            return None
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_model(entry: Mapping[str, Any], *, source: str) -> dict[str, Any]:
    """把用户态/系统态模型记录归一成组合引擎需要的形状。"""
    meta = (
        entry.get("metadata_json")
        if isinstance(entry.get("metadata_json"), dict)
        else entry
    )
    meta = meta or {}
    model_id = str(entry.get("model_id") or meta.get("model_id") or "").strip()
    return {
        "model_id": model_id,
        "model_source": source,
        "display_name": str(
            entry.get("display_name")
            or meta.get("display_name")
            or meta.get("model_name")
            or model_id
        ),
        "market": str(meta.get("market") or entry.get("market") or "CN").upper(),
        "model_type": str(
            meta.get("model_type") or entry.get("model_type") or ""
        ).lower(),
        "target_horizon_days": _as_int(meta.get("target_horizon_days")),
        "feature_count": _as_int(meta.get("feature_count")),
        "factor_schema_hash": str(meta.get("factor_schema_hash") or ""),
        "data_source": str(meta.get("data_source") or ""),
        "factor_source": str(meta.get("factor_source") or ""),
        "status": str(entry.get("status") or "ready"),
    }


def is_ensemble(model: Mapping[str, Any]) -> bool:
    """融合/堆叠模型不能再当组合成员（否则分数被重复计入）。"""
    meta = (
        model.get("metadata_json")
        if isinstance(model.get("metadata_json"), dict)
        else {}
    )
    if str(meta.get("is_ensemble", "")).lower() == "true":
        return True
    model_type = str(model.get("model_type") or "").lower()
    if any(marker in model_type for marker in _ENSEMBLE_MARKERS):
        return True
    return "ensemble" in str(model.get("model_id") or "").lower()


def signature_of(model: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "market": str(model.get("market") or "CN").upper(),
        "factor_schema_hash": str(model.get("factor_schema_hash") or ""),
        "data_source": str(model.get("data_source") or ""),
        "feature_count": _as_int(model.get("feature_count")),
        "target_horizon_days": _as_int(model.get("target_horizon_days")),
    }


def group_key(model: Mapping[str, Any]) -> str:
    sig = signature_of(model)
    return "|".join(str(sig[key]) for key in SIGNATURE_KEYS)


def eligible_models(
    models: Iterable[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """拆分可参与组合的模型与不可参与原因（不静默丢弃）。"""
    eligible: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    seen: set[str] = set()
    for model in models:
        model_id = str(model.get("model_id") or "")
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        if str(model.get("status") or "").lower() not in {"ready", "active"}:
            excluded.append({"model_id": model_id, "reason": "not_ready"})
            continue
        if is_ensemble(model):
            excluded.append({"model_id": model_id, "reason": "ensemble"})
            continue
        sig = signature_of(model)
        if (
            not sig["factor_schema_hash"]
            or not sig["data_source"]
            or sig["feature_count"] is None
        ):
            excluded.append({"model_id": model_id, "reason": "missing_signature"})
            continue
        if str(sig["market"]).upper() != "CN":
            excluded.append({"model_id": model_id, "reason": "market_not_supported"})
            continue
        eligible.append(model)
    return eligible, excluded


def group_models(models: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """按签名分组；组内按模型类型排序（确定性）。"""
    groups: dict[str, dict[str, Any]] = {}
    for model in models:
        key = group_key(model)
        group = groups.setdefault(
            key,
            {"key": key, "signature": signature_of(model), "models": []},
        )
        group["models"].append(model)
    result = list(groups.values())
    for group in result:
        group["models"].sort(
            key=lambda m: (str(m.get("model_type") or ""), str(m.get("model_id") or ""))
        )
    result.sort(key=lambda g: (-len(g["models"]), g["key"]))
    return result


def _signature_short(sig: Mapping[str, Any]) -> str:
    raw = str(sig.get("factor_schema_hash") or "")
    return raw[:8] or "unknown"


def _template(
    *,
    sig: Mapping[str, Any],
    kind: str,
    name: str,
    members: Sequence[Mapping[str, Any]],
    method: str = "mean",
    trim_count: int = 2,
    min_coverage: int | None = None,
    consensus_min_count: int = 0,
) -> dict[str, Any]:
    horizon = sig.get("target_horizon_days")
    return {
        "template_id": f"sys:{_signature_short(sig)}:{horizon}:{kind}",
        "kind": kind,
        "name": name,
        "market": sig.get("market") or "CN",
        "horizon_days": horizon,
        "method": method,
        "trim_count": trim_count,
        "min_coverage": min_coverage
        if min_coverage is not None
        else max(2, math.ceil(len(members) * 0.6)),
        "consensus_min_count": consensus_min_count,
        "members": [
            {
                "model_id": str(m.get("model_id")),
                "model_source": str(m.get("model_source") or "user"),
                "weight": 1.0,
                "model_type": str(m.get("model_type") or ""),
                "display_name": str(m.get("display_name") or m.get("model_id")),
            }
            for m in members
        ],
    }


def build_auto_candidates(
    models: Sequence[Mapping[str, Any]],
    evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """生成系统模板 + 证据等级；C0（13 个 T+10）恒第一。"""
    eligible, excluded = eligible_models(models)
    groups = group_models(eligible)
    evidence = evidence or {}
    evidence_candidates = (
        evidence.get("candidates") if isinstance(evidence, dict) else None
    )
    by_alias = _evidence_index(evidence_candidates or [])

    items: list[dict[str, Any]] = []
    for group in groups:
        sig = group["signature"]
        members = group["models"]
        if len(members) < 3:
            continue
        full = [m for m in members if m.get("target_horizon_days") is not None]
        if len(full) < 3:
            continue
        items.append(
            _template(
                sig=sig,
                kind="full_equal",
                name=f"全{len(full)}·T+{sig.get('target_horizon_days')} 等权",
                members=full,
                min_coverage=max(2, math.ceil(len(full) * 0.6)),
            )
        )
        trees = [m for m in full if m.get("model_type") in TREE_TYPES]
        if len(trees) >= 2:
            items.append(
                _template(
                    sig=sig,
                    kind="family_tree",
                    name=f"树族{len(trees)}·T+{sig.get('target_horizon_days')}",
                    members=trees,
                    min_coverage=max(2, math.ceil(len(trees) * 0.6)),
                )
            )
        linear = [m for m in full if m.get("model_type") in LINEAR_TYPES]
        if len(linear) >= 2:
            items.append(
                _template(
                    sig=sig,
                    kind="family_linear",
                    name=f"线性族{len(linear)}·T+{sig.get('target_horizon_days')}",
                    members=linear,
                    min_coverage=2,
                )
            )
        deep = [m for m in full if m.get("model_type") in DL_TYPES]
        if len(deep) >= 2:
            items.append(
                _template(
                    sig=sig,
                    kind="family_dl",
                    name=f"深度族{len(deep)}·T+{sig.get('target_horizon_days')}",
                    members=deep,
                    min_coverage=max(2, math.ceil(len(deep) * 0.6)),
                )
            )
        items.append(
            _template(
                sig=sig,
                kind="consensus_high",
                name=f"全{len(full)}·高共识·T+{sig.get('target_horizon_days')}",
                members=full,
                min_coverage=math.ceil(len(full) * 0.75),
            )
        )
        if len(full) >= 5:
            items.append(
                _template(
                    sig=sig,
                    kind="trimmed",
                    name=f"全{len(full)}·截尾·T+{sig.get('target_horizon_days')}",
                    members=full,
                    method="trimmed",
                    trim_count=2,
                    min_coverage=max(5, math.ceil(len(full) * 0.6)),
                )
            )

    for item in items:
        # 别名按**多重集**匹配：同一族 3 个模型与 1 个模型不是同一个成员集合，
        # 用 frozenset 会让 13 模型全组与 5 模型子集串证据。
        aliases = tuple(
            sorted(
                TYPE_ALIASES.get(str(m.get("model_type")), "") for m in item["members"]
            )
        )
        item["evidence"] = _evidence_for(
            by_alias, aliases, method=item["method"], horizon=item["horizon_days"]
        )

    items.sort(
        key=lambda item: (
            _grade_rank(item["evidence"]["grade"]),
            -len(item["members"]),
            item["template_id"],
        )
    )
    baseline_id = _baseline_template(items)
    for item in items:
        item["baseline"] = item["template_id"] == baseline_id
    if baseline_id:
        items.sort(key=lambda item: (item["template_id"] != baseline_id,))

    return {
        "market": "CN",
        "items": items,
        "baseline_template_id": baseline_id,
        "evidence_available": bool(evidence_candidates),
        "evidence_generated_at": (evidence or {}).get("generated_at"),
        "excluded": excluded,
        "warnings": [] if evidence_candidates else ["evidence_missing"],
    }


def _evidence_index(
    candidates: Sequence[Mapping[str, Any]],
) -> dict[tuple[tuple[str, ...], str, int | None], Mapping[str, Any]]:
    index: dict[tuple[tuple[str, ...], str, int | None], Mapping[str, Any]] = {}
    for candidate in candidates:
        if candidate.get("status") != "ok":
            continue
        members = tuple(sorted(str(m) for m in candidate.get("members") or []))
        key = (
            members,
            str(candidate.get("method") or "mean"),
            _as_int(candidate.get("horizon")),
        )
        index[key] = candidate
    return index


def _evidence_for(
    by_alias: Mapping[tuple[tuple[str, ...], str, int | None], Mapping[str, Any]],
    aliases: tuple[str, ...],
    *,
    method: str,
    horizon: int | None,
) -> dict[str, Any]:
    candidate = by_alias.get((aliases, method, horizon))
    if candidate is None:
        return {"grade": "unverified", "candidate_id": None}
    criteria = candidate.get("criteria") or {}
    checks = criteria.get("checks") or {}
    verdict = str(candidate.get("verdict") or "")
    if verdict == "recommended":
        grade = "recommended"
    elif all(checks.get(flag) for flag in ("min_hit_lift", "ci_lower_gt_zero", "bh_q")):
        grade = "watch"
    else:
        grade = "research"
    windows = candidate.get("windows") or {}
    test = windows.get("test") or {}
    return {
        "grade": grade,
        "candidate_id": candidate.get("id"),
        "verdict": verdict,
        "delta_test": (criteria.get("delta_test") if criteria else None),
        "hit_exec_test": test.get("hit_exec_mean"),
        "ci": candidate.get("bootstrap"),
        "monthly": candidate.get("monthly"),
        "failed_checks": candidate.get("failed_checks") or [],
    }


def _grade_rank(grade: str) -> int:
    return {"recommended": 0, "watch": 1, "research": 2, "unverified": 3}.get(grade, 4)


def _baseline_template(items: Sequence[Mapping[str, Any]]) -> str | None:
    """C0：最大 T+10 全组等权模板；没有 T+10 时退化为成员最多的全组等权。"""
    full = [
        item
        for item in items
        if item["kind"] == "full_equal" and item["horizon_days"] is not None
    ]
    if not full:
        return None
    t10 = [item for item in full if item["horizon_days"] == 10]
    pool = t10 or full
    best = sorted(pool, key=lambda item: (-len(item["members"]), item["template_id"]))[
        0
    ]
    return str(best["template_id"])


def load_production_models(
    models_root: str | Path | None = None,
) -> list[dict[str, Any]]:
    """扫描系统模型目录（``models/production/*/metadata.json``）。

    只取组合需要的字段；展示名/市场/类型/签名的归一与主模型列表同一套
    键名，避免两处口径分叉。
    """
    root = Path(models_root or os.getenv("MODELS_PRODUCTION", "/app/models/production"))
    if not root.is_dir():
        return []
    results: list[dict[str, Any]] = []
    for subdir in sorted(root.iterdir()):
        meta_file = subdir / "metadata.json"
        if not meta_file.is_file():
            continue
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(meta, dict):
            continue
        results.append(
            {
                "model_id": f"sys-{subdir.name}",
                "status": "ready",
                "metadata_json": meta,
            }
        )
    return results


def template_slug(name: str) -> str:
    """组合名合法性：去控制字符、压缩空白（DB 唯一约束按 tenant/user/name）。"""
    cleaned = re.sub(r"[\x00-\x1f\x7f]+", "", str(name or ""))
    return re.sub(r"\s+", " ", cleaned).strip()[:80]
