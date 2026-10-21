"""系统自动识别组合候选（``combo_auto``）测试。

锁死四件事：

- 「同特征」判据是签名五元组（含 ``feature_count``），不靠模型名猜——
  同名同哈希但特征数不同的遗留模型不得并组；
- 模板 ID 只由签名+周期+类型决定，证据等级变化不改 ID；
- 证据等级只读 P1 证据包，缺证据一律 ``unverified``，绝不显示「推荐」；
- C0（13 个 T+10 全组等权）恒排第一，且标 ``baseline``。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.services.engine.services.combo_auto import (
    build_auto_candidates,
    eligible_models,
    group_key,
    group_models,
    is_ensemble,
    load_evidence,
    load_production_models,
    normalize_model,
)

pytestmark = pytest.mark.unit

HASH = "e405bd8c8f9e36ab575008664313f653db840b1e9064d1f5527c794a0de5c3a4"


def _model(
    model_id: str,
    *,
    model_type: str,
    horizon: int = 10,
    feature_count: int = 300,
    schema_hash: str = HASH,
    data_source: str = "quantdb_factors",
    market: str = "CN",
    source: str = "user",
    status: str = "ready",
    **meta_extra,
):
    return {
        "model_id": model_id,
        "status": status,
        "metadata_json": {
            "model_type": model_type,
            "target_horizon_days": horizon,
            "feature_count": feature_count,
            "factor_schema_hash": schema_hash,
            "data_source": data_source,
            "market": market,
            "display_name": model_id.upper(),
            **meta_extra,
        },
        "_source": source,
    }


def _thirteen():
    types = [
        "lightgbm",
        "lightgbm",
        "lightgbm",
        "xgboost",
        "xgboost",
        "xgboost",
        "catboost",
        "catboost",
        "catboost",
        "nativetft",
        "nativetft",
        "nativetft",
        "lstm",
    ]
    return [_model(f"m{i:02d}", model_type=t) for i, t in enumerate(types)]


# ── 归一与分组 ───────────────────────────────────────────────────────────


def test_group_key_separates_legacy_model_with_different_feature_count():
    modern = normalize_model(_model("m1", model_type="lightgbm"), source="user")
    legacy = normalize_model(
        _model("mdl_cn_hub_T_10_LightGBM", model_type="lightgbm", feature_count=55),
        source="user",
    )
    assert group_key(modern) != group_key(legacy)
    assert "300" in group_key(modern)
    assert "55" in group_key(legacy)


def test_group_models_is_deterministic_and_sorted_by_size():
    models = [
        normalize_model(_model("b", model_type="xgboost", horizon=5), source="user"),
        normalize_model(_model("a", model_type="lightgbm", horizon=5), source="user"),
        normalize_model(_model("c", model_type="lightgbm"), source="user"),
    ]
    groups = group_models(models)
    assert len(groups) == 2
    assert [g["signature"]["target_horizon_days"] for g in groups] == [5, 10]
    assert [m["model_id"] for m in groups[0]["models"]] == ["a", "b"]


def test_ensemble_models_are_excluded():
    model = normalize_model(
        _model("ensemble_1", model_type="ensemble", is_ensemble="true"), source="user"
    )
    assert is_ensemble(model) is True
    models = _thirteen() + [_model("mdl_cn_ensemble_a", model_type="stacking")]
    eligible, excluded = eligible_models(
        [normalize_model(m, source="user") for m in models]
    )
    assert all("ensemble" not in m["model_id"] for m in eligible)
    reasons = {row["model_id"]: row["reason"] for row in excluded}
    assert reasons["mdl_cn_ensemble_a"] == "ensemble"


def test_eligible_reports_reasons_instead_of_silent_drop():
    raw = [
        _model("ok", model_type="lightgbm"),
        _model("archived", model_type="lightgbm", status="archived"),
        _model("nohash", model_type="lightgbm", schema_hash=""),
        _model("hk", model_type="lightgbm", market="HK"),
    ]
    eligible, excluded = eligible_models(
        [normalize_model(m, source="user") for m in raw]
    )
    assert [m["model_id"] for m in eligible] == ["ok"]
    reasons = {row["model_id"]: row["reason"] for row in excluded}
    assert reasons == {
        "archived": "not_ready",
        "nohash": "missing_signature",
        "hk": "market_not_supported",
    }


# ── 自动模板 ─────────────────────────────────────────────────────────────


def test_auto_candidates_make_baseline_first_and_cover_families():
    models = [normalize_model(m, source="user") for m in _thirteen()]
    result = build_auto_candidates(models)
    items = result["items"]
    kinds = [item["kind"] for item in items]
    assert kinds[0] == "full_equal"
    assert items[0]["baseline"] is True
    assert result["baseline_template_id"] == items[0]["template_id"]
    assert "family_tree" in kinds
    assert "family_dl" in kinds
    assert "consensus_high" in kinds
    assert "trimmed" in kinds
    assert "family_linear" not in kinds
    full = items[0]
    assert len(full["members"]) == 13
    assert full["min_coverage"] == 8  # ceil(13*0.6)
    # 滚动参数随周期派生：T+10 → 5 只/日、滚动 10 日、目标 50 只
    assert full["purpose"] == "rebalance"
    assert full["entry_per_day"] == 5
    assert full["rolling_window"] == 10
    assert full["target_positions"] == 50
    consensus = next(i for i in items if i["kind"] == "consensus_high")
    assert consensus["min_coverage"] == 10  # ceil(13*0.75)
    trimmed = next(i for i in items if i["kind"] == "trimmed")
    assert trimmed["method"] == "trimmed"
    assert trimmed["trim_count"] == 2


def test_template_rolling_defaults_follow_horizon():
    for horizon, target in ((5, 25), (20, 100)):
        models = [
            normalize_model(
                _model(f"m{i}", model_type="lightgbm", horizon=horizon),
                source="user",
            )
            for i in range(4)
        ]
        result = build_auto_candidates(models)
        full = result["items"][0]
        assert full["rolling_window"] == horizon
        assert full["entry_per_day"] == 5
        assert full["target_positions"] == target


def test_small_group_does_not_generate_templates():
    models = [normalize_model(m, source="user") for m in _thirteen()[:2]]
    result = build_auto_candidates(models)
    assert result["items"] == []
    assert result["baseline_template_id"] is None


def test_auto_candidates_filter_members_without_runs():
    """K2：T+5 全组只有部分成员有 run 时，模板只保留有 run 的成员并标注缺失数。

    不把覆盖门槛降到 2 来硬凑全组——模板成员缩减后 min_coverage 按新成员数
    重算，缺失成员数在模板上明示（前端显示「N 只成员暂无推理」）。
    """
    models = [normalize_model(m, source="user") for m in _thirteen()[:4]]
    runnable = {models[0]["model_id"], models[1]["model_id"], models[2]["model_id"]}

    result = build_auto_candidates(models, runnable_ids=runnable)
    full = result["items"][0]
    assert full["kind"] == "full_equal"
    assert len(full["members"]) == 3
    assert full["missing_members"] == 1
    assert full["min_coverage"] == 2  # ceil(3*0.6)

    # 有 run 的成员不足 3 只：不生成模板（不伪造成员）
    result = build_auto_candidates(
        models, runnable_ids=runnable - {models[0]["model_id"]}
    )
    assert result["items"] == []
    assert result["baseline_template_id"] is None


def test_auto_candidates_without_run_filter_keeps_all_members():
    models = [normalize_model(m, source="user") for m in _thirteen()]
    result = build_auto_candidates(models)
    assert result["items"][0]["missing_members"] == 0
    assert len(result["items"][0]["members"]) == 13


def test_template_ids_are_stable_when_evidence_changes():
    models = [normalize_model(m, source="user") for m in _thirteen()]
    without = build_auto_candidates(models)
    evidence = {
        "generated_at": "2026-10-21T00:00:00Z",
        "candidates": [
            {
                "id": "C0",
                "status": "ok",
                "members": ["LGB", "XGB", "CAT", "TFT", "LSTM"],
                "method": "mean",
                "horizon": 10,
                "verdict": "recommended",
                "criteria": {
                    "checks": {
                        "min_hit_lift": True,
                        "ci_lower_gt_zero": True,
                        "bh_q": True,
                    },
                    "delta_test": 0.012,
                },
                "windows": {"test": {"hit_exec_mean": 0.48}},
            }
        ],
    }
    with_evidence = build_auto_candidates(models, evidence)
    ids_without = {item["template_id"] for item in without["items"]}
    ids_with = {item["template_id"] for item in with_evidence["items"]}
    assert ids_without == ids_with
    # 证据只改等级，不改 ID / 成员顺序
    assert (
        with_evidence["items"][0]["template_id"] == without["items"][0]["template_id"]
    )


def test_evidence_grades_map_to_recommended_watch_research_unverified():
    models = [normalize_model(m, source="user") for m in _thirteen()]
    all_members = ["LGB"] * 3 + ["XGB"] * 3 + ["CAT"] * 3 + ["TFT"] * 3 + ["LSTM"]

    def evidence(checks, verdict=""):
        return {
            "candidates": [
                {
                    "id": "C0",
                    "status": "ok",
                    "members": all_members,
                    "method": "mean",
                    "horizon": 10,
                    "verdict": verdict,
                    "criteria": {"checks": checks},
                }
            ]
        }

    recommended = build_auto_candidates(
        models,
        evidence(
            {"min_hit_lift": True, "ci_lower_gt_zero": True, "bh_q": True},
            verdict="recommended",
        ),
    )
    watch = build_auto_candidates(
        models,
        evidence(
            {"min_hit_lift": True, "ci_lower_gt_zero": True, "bh_q": True},
            verdict="not_recommended",
        ),
    )
    research = build_auto_candidates(
        models,
        evidence(
            {"min_hit_lift": False, "ci_lower_gt_zero": True, "bh_q": True},
            verdict="not_recommended",
        ),
    )
    unverified = build_auto_candidates(models, {"generated_at": None, "candidates": []})
    assert recommended["items"][0]["evidence"]["grade"] == "recommended"
    assert watch["items"][0]["evidence"]["grade"] == "watch"
    assert research["items"][0]["evidence"]["grade"] == "research"
    assert unverified["items"][0]["evidence"]["grade"] == "unverified"
    assert "evidence_missing" in unverified["warnings"]


def test_evidence_alias_uses_model_type_not_member_count():
    """证据匹配按「别名集合+方法+周期」，成员数相同但类型不同不得串证据。"""
    models = [normalize_model(m, source="user") for m in _thirteen()]
    # 证据写的是 5 个成员的别集，但组合全组 13 个 → 不应命中
    evidence = {
        "candidates": [
            {
                "id": "C9",
                "status": "ok",
                "members": ["LGB", "XGB", "CAT", "TFT", "LSTM"],
                "method": "mean",
                "horizon": 10,
                "verdict": "recommended",
                "criteria": {"checks": {}},
            }
        ]
    }
    result = build_auto_candidates(models, evidence)
    assert result["items"][0]["evidence"]["grade"] == "unverified"


# ── 证据文件 ─────────────────────────────────────────────────────────────


def test_load_evidence_returns_none_for_missing_or_broken_file(tmp_path: Path):
    assert load_evidence(tmp_path / "missing.json") is None
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert load_evidence(broken) is None
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"candidates": []}), encoding="utf-8")
    assert load_evidence(good) == {"candidates": []}


def test_load_production_models_reads_metadata(tmp_path: Path):
    sub = tmp_path / "native_tft_t10"
    sub.mkdir()
    (sub / "metadata.json").write_text(
        json.dumps({"model_type": "nativetft", "target_horizon_days": 10}),
        encoding="utf-8",
    )
    (tmp_path / "not_a_model").mkdir()
    models = load_production_models(tmp_path)
    assert len(models) == 1
    assert models[0]["model_id"] == "sys-native_tft_t10"
    assert models[0]["status"] == "ready"
