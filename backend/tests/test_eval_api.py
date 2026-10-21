"""FE-E 后端测试：评估读 API（`/api/v1/eval/*`）。

覆盖：
1. `_row_to_score` / `build_gate_preview` / `_validate_object_type` 纯函数；
2. **门禁预演与执行点同源**断言（promotion_gate 唯一实现，不复制判定）；
3. 路由注册与可见性口径源断言；
4. 真库 E2E：合成 eval_scores 行 → list_scores（latest_only 网格）/ score_history（升序）/
   strategy_health（最新+历史+门禁预演）→ 清理。
"""

from __future__ import annotations

import uuid
from datetime import date as _date
from pathlib import Path

import pytest
from fastapi import HTTPException

_BACKEND = Path(__file__).resolve().parents[1]


# ── 纯函数 ──────────────────────────────────────────────────────────


@pytest.mark.unit
def test_row_to_score_shape():
    from datetime import date, datetime

    from backend.services.api.routers.eval_scores import _row_to_score

    row = {
        "object_type": "model",
        "object_id": "model_qlib",
        "snapshot_date": date(2026, 9, 16),
        "score": 83.7,
        "grade": "B",
        "low_confidence": False,
        "red_line_failed": [],
        "dimensions": {"oos_predictive": {"score": 83.7}},
        "inputs_version": {"metrics_source": "metrics"},
        "created_at": datetime(2026, 9, 16, 13, 0, 0),
    }
    out = _row_to_score(row)
    assert out["snapshot_date"] == "2026-09-16"
    assert out["grade"] == "B" and out["score"] == 83.7
    assert out["created_at"].startswith("2026-09-16")
    assert out["red_line_failed"] == []

    # 空值容错（未评级/未落日期）
    row_min = dict(row, snapshot_date=None, created_at=None, red_line_failed=None)
    out_min = _row_to_score(row_min)
    assert out_min["snapshot_date"] is None and out_min["created_at"] is None
    assert out_min["red_line_failed"] == []


@pytest.mark.unit
def test_gate_preview_same_source_as_promotion_gate():
    from backend.services.api.routers.eval_scores import build_gate_preview

    # 无记录 → 拒绝（与执行点一致）
    preview = build_gate_preview(None)
    assert preview["allowed"] is False and "体检" in preview["note"]
    # A → 放行；L → 拒绝且带理由
    assert build_gate_preview({"verdict": "A", "confidence": 88})["allowed"] is True
    rejected = build_gate_preview(
        {"verdict": "L", "confidence": 40, "reasons": ["DSR 0.4 < 0.95"]}
    )
    assert rejected["allowed"] is False and "DSR 0.4 < 0.95" in rejected["note"]

    src = (_BACKEND / "services/api/routers/eval_scores.py").read_text(encoding="utf-8")
    assert "from backend.shared.backtest_health import promotion_gate" in src


@pytest.mark.unit
def test_object_type_validation():
    from backend.services.api.routers.eval_scores import (
        ALLOWED_OBJECT_TYPES,
        _validate_object_type,
    )

    assert _validate_object_type("model") == "model"
    assert _validate_object_type(" strategy_health ") == "strategy_health"
    with pytest.raises(HTTPException) as exc:
        _validate_object_type("nonsense")
    assert exc.value.status_code == 400
    assert "factor" in exc.value.detail  # 提示允许集合
    assert set(ALLOWED_OBJECT_TYPES) == {
        "factor", "model", "strategy", "account", "live_account", "daily_selection",
        "strategy_health",
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_object_types_short_labels():
    """U6：页签短名后端直给（strategy_health → 体检），每类都有——前端不再靠正则剥后缀。"""
    from backend.services.api.routers.eval_scores import (
        ALLOWED_OBJECT_TYPES,
        object_types,
    )

    resp = await object_types()
    rows = resp["data"]
    assert {r["object_type"] for r in rows} == set(ALLOWED_OBJECT_TYPES)
    short = {r["object_type"]: r["short_label"] for r in rows}
    assert short["strategy_health"] == "体检"
    assert short["live_account"] == "实盘账户"
    assert all(short.values())


@pytest.mark.unit
def test_eval_router_registered_and_visibility_clause():
    main_src = (_BACKEND / "services/api/main.py").read_text(encoding="utf-8")
    assert "from backend.services.api.routers.eval_scores import router as eval_router" in main_src
    assert "app.include_router(eval_router)" in main_src

    src = (_BACKEND / "services/api/routers/eval_scores.py").read_text(encoding="utf-8")
    # 可见性：租户内共享行（user_id=''）+ 本人私有行
    assert "(user_id = :u OR user_id = '')" in src
    # 只读纪律：无 INSERT/UPDATE/DELETE
    for verb in ("INSERT INTO", "UPDATE ", "DELETE FROM"):
        assert verb not in src
    # 长序列侧车（§1.6）：单独的按需端点，且复用同一可见性谓词——
    # 侧车在盘上没有租户/用户维度，若绕过 eval_scores 就是「知道 id 就能读别人的序列」
    assert '@router.get("/series")' in src


@pytest.mark.unit
def test_display_name_helpers():
    """展示名纯函数（评估中心列表的人话名；2026-09-17 新增）。"""
    from backend.services.api.routers.eval_scores import (
        SYSTEM_MODEL_LABELS,
        account_display_label,
        backtest_display_label,
        factor_display_name,
        fallback_model_label,
        resolve_strategy_labels,
        strategy_health_display_name,
    )

    # 因子：复用引擎词典（alpha158 窗口族 / gtja 编号）
    assert factor_display_name("a158_LOW0") == "最低相对收盘"
    assert factor_display_name("a158_VMA30") == "30日均量比（均量/当日量）"
    assert "GTJA191 #095" in str(factor_display_name("gtja_095"))
    assert factor_display_name("") is None

    # 模型：系统内置有标签；未知 id 由调用方回退
    assert "Qlib" in SYSTEM_MODEL_LABELS["model_qlib"]

    # 用户模型 id 兜底名（训练元数据缺 display_name 时）
    assert (
        fallback_model_label("mdl_cn_train_20260906064130_43e64ff0_b4cf437f")
        == "A股 训练模型 · 2026-09-06 06:41"
    )
    assert (
        fallback_model_label("mdl_us_train_20260912134006_566f6da2_8d0709f2_xgboost_8f4ac5a6")
        == "美股 训练模型 · xgboost · 2026-09-12 13:40"
    )
    assert (
        fallback_model_label("mdl_us_train_20260912134006_566f6da2_8d0709f2_random_forest_4555743f")
        == "美股 训练模型 · random forest · 2026-09-12 13:40"
    )
    assert fallback_model_label("model_qlib") is None
    assert fallback_model_label("") is None

    # 策略：策略名优先，其次 标的·区间，无信息 → None
    assert backtest_display_label({"strategy_name": "美股多空 TopK"}) == "美股多空 TopK"
    assert (
        backtest_display_label(
            {"symbol": "csi1000", "start_date": "2025-09-09", "end_date": "2026-09-09"}
        )
        == "回测 · csi1000 · 2025-09-09 ~ 2026-09-09"
    )
    assert backtest_display_label({}) is None

    # 策略卡展示名（2026-10-15 接线 strategies 表）：策略表名 → config_json 名 → None
    rows_by_id = {
        "bt1": {"strategy_name": "minibt·TRIX 三重平滑顺势", "config_json": {}},
        "bt2": {"strategy_name": None, "config_json": {"strategy_name": "实验室多空"}},
        "bt3": {"strategy_name": "  ", "config_json": {}},
    }
    labels = resolve_strategy_labels(rows_by_id, ["bt1", "bt2", "bt3", "bt_missing"])
    assert labels["bt1"] == "minibt·TRIX 三重平滑顺势"
    assert labels["bt2"] == "实验室多空"
    assert labels["bt3"] is None
    assert labels["bt_missing"] is None  # 查无此回测 → None（前端回退 backtest_id）

    # 账户：{user}:{market} 口径
    assert account_display_label("999:CN") == "A股 模拟账户（用户 999）"
    assert account_display_label("999:CN", "张三") == "A股 模拟账户（张三）"
    assert account_display_label("") is None

    # 策略体检留档：展示名 = 策略名（空名/无策略 → None，前端回退「策略 #id」）
    assert strategy_health_display_name("默认 Top-K 选股策略") == "默认 Top-K 选股策略"
    assert strategy_health_display_name(" minibt·HullMA 双周期 ") == "minibt·HullMA 双周期"
    assert strategy_health_display_name("") is None
    assert strategy_health_display_name("   ") is None
    assert strategy_health_display_name(None) is None


# ── 真库 E2E ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_eval_api_endpoints_real_db():
    try:
        from sqlalchemy import text

        from backend.shared.database_manager_v2 import get_session
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"依赖不可用: {exc}")
    try:
        async with get_session(read_only=True) as probe:
            await probe.execute(text("SELECT 1"))
    except Exception:
        # 跨事件循环池自愈：前序用例可能留下绑定旧循环的池（asyncpg 陷阱）——
        # 关池重试一次，避免整条真库 E2E 被环境抖动跳过（用后关池纪律的受害者侧补丁）。
        from backend.shared.database_manager_v2 import close_database

        await close_database()
        try:
            async with get_session(read_only=True) as probe:
                await probe.execute(text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"DB 连接抖动: {exc}")

    from backend.services.api.routers.eval_scores import (
        list_scores,
        score_history,
        strategy_health,
    )

    tag = uuid.uuid4().hex[:8]
    model_id = f"pytest_model_{tag}"
    model_id2 = f"pytest_model_{tag}b"
    sid = f"77{uuid.uuid4().int % 10**6:06d}"
    user = {"tenant_id": "default", "user_id": "00000001"}

    try:
        async with get_session() as session:
            # 模型卡两天历史（共享行 user_id=''），策略体检两期（私有行）
            for day, score in (("2026-09-15", 60.0), ("2026-09-16", 72.0)):
                await session.execute(
                    text(
                        "INSERT INTO eval_scores (object_type, object_id, snapshot_date, score, "
                        "grade, low_confidence, red_line_failed, dimensions, inputs_version) "
                        "VALUES ('model', :oid, :d, :s, 'B', FALSE, '[]'::jsonb, "
                        "CAST(:dims AS jsonb), '{}'::jsonb)"
                    ),
                    {
                        "oid": model_id,
                        "d": _date.fromisoformat(day),
                        "s": score,
                        "dims": f'{{"oos_predictive": {{"score": {score}}}}}',
                    },
                )
            # 第二个模型对象：U3 截断断言需要 ≥2 个对象
            await session.execute(
                text(
                    "INSERT INTO eval_scores (object_type, object_id, snapshot_date, score, "
                    "grade, low_confidence, red_line_failed, dimensions, inputs_version) "
                    "VALUES ('model', :oid, :d, 50.0, 'C', FALSE, '[]'::jsonb, "
                    "'{}'::jsonb, '{}'::jsonb)"
                ),
                {"oid": model_id2, "d": _date.fromisoformat("2026-09-16")},
            )
            for day, verdict in (("2026-08-01", "E"), ("2026-09-16", "A")):
                await session.execute(
                    text(
                        "INSERT INTO eval_scores (object_type, object_id, snapshot_date, tenant_id, "
                        "user_id, score, grade, low_confidence, red_line_failed, dimensions, inputs_version) "
                        "VALUES ('strategy_health', :sid, :d, 'default', :u, 80, :v, FALSE, '[]'::jsonb, "
                        "CAST(:dims AS jsonb), CAST(:ver AS jsonb))"
                    ),
                    {
                        "sid": sid,
                        "d": _date.fromisoformat(day),
                        "u": user["user_id"],
                        "v": verdict,
                        "dims": (
                            '{"verdict_label": "标签'
                            + verdict
                            + '", "reasons": ["r"], "suggestions": []}'
                        ),
                        "ver": f'{{"backtest_id": "bt-{day}", "evidence_source": "backtest"}}',
                    },
                )
            await session.commit()

        # 1) 网格：latest_only → 每对象一条（取最新 9/16 的 72）；附 display_name 键
        listing = await list_scores(object_type="model", object_id=model_id, latest_only=True, limit=10, current_user=user)
        assert listing["success"] is True
        assert listing["meta"]["count"] == 1
        assert listing["data"][0]["score"] == 72.0
        assert listing["data"][0]["display_name"] is None  # 随机 model_id 查不到人话名 → None 由前端回退
        assert listing["meta"]["truncated"] is False
        assert listing["meta"]["total"] == 1  # 未截断：total=count，不多查

        # 1a) U3：limit=1 截断 → truncated=True 且 total 补 COUNT（≥2 个对象），
        #     不再把「被 cap」伪装成「只有这么多」
        capped = await list_scores(
            object_type="model", latest_only=True, limit=1, current_user=user
        )
        assert capped["meta"]["truncated"] is True
        assert capped["meta"]["total"] >= 2
        assert len(capped["data"]) == 1

        # 1b) 因子列表：display_name 来自引擎词典
        from backend.services.api.routers.eval_scores import factor_display_name

        assert factor_display_name("a158_QTLD30") == "30日收盘低分位"

        # 2) 历史：升序两根
        hist = await score_history(object_type="model", object_id=model_id, limit=10, current_user=user)
        scores = [row["score"] for row in hist["data"]]
        assert scores == [60.0, 72.0]

        # 3) 体检档案：最新 A + 历史两期 + 门禁放行（同源预演）
        health = await strategy_health(strategy_id=sid, limit=10, current_user=user)
        assert health["data"]["latest"]["verdict"] == "A"
        assert [h["verdict"] for h in health["data"]["history"]] == ["A", "E"]
        assert health["data"]["gate"]["allowed"] is True
        assert health["data"]["latest"]["backtest_id"].startswith("bt-")

        # 4) 可见性：他人 user_id 读不到该策略体检（私有行）
        other = await strategy_health(
            strategy_id=sid, limit=10, current_user={"tenant_id": "default", "user_id": "99999999"}
        )
        assert other["data"]["latest"] is None
    finally:
        from backend.shared.database_manager_v2 import get_session as _gs

        async with _gs() as session:
            await session.execute(
                text("DELETE FROM eval_scores WHERE object_id IN (:m, :m2, :s)"),
                {"m": model_id, "m2": model_id2, "s": sid},
            )
            await session.commit()
        from backend.shared.database_manager_v2 import close_database

        await close_database()


@pytest.mark.asyncio
async def test_object_series_endpoint_real_db(tmp_path, monkeypatch):
    """长序列侧车端到端（设计 §1.6）：写→读回、看不见 404、穿越 400、缺侧车 200 但如实说。"""
    try:
        from sqlalchemy import text

        from backend.shared.database_manager_v2 import get_session
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"依赖不可用: {exc}")
    try:
        async with get_session(read_only=True) as probe:
            await probe.execute(text("SELECT 1"))
    except Exception:
        from backend.shared.database_manager_v2 import close_database

        await close_database()
        try:
            async with get_session(read_only=True) as probe:
                await probe.execute(text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"DB 连接抖动: {exc}")

    from backend.services.api.routers.eval_scores import object_series
    from backend.shared.eval_series import save_series

    tag = uuid.uuid4().hex[:8]
    model_id = f"pytest_series_{tag}"
    user = {"tenant_id": "default", "user_id": "00000001"}
    monkeypatch.setenv("QM_EVAL_SERIES_DIR", str(tmp_path))

    try:
        # Arrange：一条共享行（user_id='' 默认）让该对象对任何人可见 + 一份侧车
        async with get_session() as session:
            await session.execute(
                text(
                    "INSERT INTO eval_scores (object_type, object_id, snapshot_date, score, "
                    "grade, low_confidence, red_line_failed, dimensions, inputs_version) "
                    "VALUES ('model', :oid, :d, 72, 'B', FALSE, '[]'::jsonb, "
                    "'{}'::jsonb, '{}'::jsonb)"
                ),
                {"oid": model_id, "d": _date.fromisoformat("2026-09-16")},
            )
            await session.commit()
        written = save_series(
            "model",
            model_id,
            {
                "series": {"daily_ic": [{"date": "2026-09-15", "value": 0.031}]},
                "scalars": {"monotonicity": 0.72},
            },
        )
        assert written["written"] is True
        assert Path(written["path"]).parent == tmp_path / "model"

        # Act
        resp = await object_series(
            object_type="model", object_id=model_id, current_user=user
        )

        # Assert：序列与标量原样回来（路由不重算、不改写），信封带生成时刻
        assert resp["success"] is True
        assert resp["meta"]["available"] is True
        assert resp["meta"]["reason"] is None
        assert resp["data"]["series"]["daily_ic"] == [
            {"date": "2026-09-15", "value": 0.031}
        ]
        assert resp["data"]["scalars"]["monotonicity"] == 0.72
        assert resp["meta"]["generated_at"] and resp["meta"]["version"] == 1

        # 可见但尚无侧车：200 + available=false 且 reason 说清是「还没产出」
        empty_id = f"pytest_series_none_{tag}"
        async with get_session() as session:
            await session.execute(
                text(
                    "INSERT INTO eval_scores (object_type, object_id, snapshot_date, score, "
                    "grade, low_confidence, red_line_failed, dimensions, inputs_version) "
                    "VALUES ('factor', :oid, :d, 70, 'B', FALSE, '[]'::jsonb, "
                    "'{}'::jsonb, '{}'::jsonb)"
                ),
                {"oid": empty_id, "d": _date.fromisoformat("2026-09-16")},
            )
            await session.commit()
        missing = await object_series(
            object_type="factor", object_id=empty_id, current_user=user
        )
        assert missing["meta"]["available"] is False
        assert missing["meta"]["reason"] == "missing"
        assert "尚未产出" in missing["meta"]["note"]

        # 库里没有该对象 → 404（不是「空序列」）
        with pytest.raises(HTTPException) as exc:
            await object_series(
                object_type="model", object_id=f"pytest_absent_{tag}", current_user=user
            )
        assert exc.value.status_code == 404

        # 路径穿越 → 400（输入错误，不改妆成 404「看不见」）
        with pytest.raises(HTTPException) as exc2:
            await object_series(
                object_type="model", object_id="../../etc/passwd", current_user=user
            )
        assert exc2.value.status_code == 400
        assert "穿越" in exc2.value.detail
    finally:
        from backend.shared.database_manager_v2 import get_session as _gs

        async with _gs() as session:
            await session.execute(
                text("DELETE FROM eval_scores WHERE object_id LIKE :p"),
                {"p": f"pytest_series_%{tag}"},
            )
            await session.commit()
        from backend.shared.database_manager_v2 import close_database

        await close_database()


# ── T-FE-15 自助体检上传 ────────────────────────────────────────────


@pytest.mark.unit
def test_parse_nav_payload_formats():
    from backend.services.api.routers.eval_scores import parse_nav_payload

    # JSON 纯数组
    rows = parse_nav_payload("[" + ",".join(str(100 + i) for i in range(40)) + "]")
    assert len(rows) == 40 and rows[0]["date"] is None

    # JSON 回测结果文件形态（{"equity_curve":[{date,value}]}）
    import json as _json

    payload = {"equity_curve": [{"date": f"2026-01-{i:02d}", "value": 100 + i} for i in range(1, 32)]}
    rows = parse_nav_payload(_json.dumps(payload))
    assert len(rows) == 31 and rows[0]["date"] == "2026-01-01"

    # CSV：日期+数值（表头跳过）；YYYYMMDD 归一为 ISO
    csv = "date,close\n" + "\n".join(f"202601{i:02d},{100 + i}" for i in range(1, 32))
    rows = parse_nav_payload(csv)
    assert len(rows) == 31 and rows[0]["date"] == "2026-01-01"
    assert rows[-1]["value"] == 131

    # CSV：纯数值
    rows = parse_nav_payload("\n".join(str(100 + i) for i in range(35)))
    assert len(rows) == 35


@pytest.mark.unit
def test_parse_nav_payload_guards():
    from backend.services.api.routers.eval_scores import parse_nav_payload

    with pytest.raises(ValueError, match="内容为空"):
        parse_nav_payload("  ")
    with pytest.raises(ValueError, match="不足"):
        parse_nav_payload("1,2,3")  # <30 点
    with pytest.raises(ValueError, match="过多"):
        parse_nav_payload("\n".join(str(100 + (i % 7)) for i in range(5001)))
    # 非法值（0/负）被过滤后不足
    with pytest.raises(ValueError, match="不足"):
        parse_nav_payload("\n".join(["0"] * 40))


@pytest.mark.asyncio
async def test_upload_health_check_real_report():
    """合成强势曲线 → 真实九项体检报告（A/B/L/E 皆可，但必须结构完整）；口径不足 400。"""
    import json as _json

    import numpy as np
    from fastapi import HTTPException

    from backend.services.api.routers.eval_scores import upload_health_check

    rng = np.random.default_rng(7)
    bench = rng.normal(0.0003, 0.006, 750)
    strat = 0.3 * bench + 0.002 + rng.normal(0, 0.004, 750)
    vals = list(np.cumprod(1 + strat) * 100)
    start = __import__("datetime").date(2024, 1, 2)
    curve = [
        {"date": (start + __import__("datetime").timedelta(days=i)).isoformat(), "value": float(v)}
        for i, v in enumerate(vals[:300])
    ]
    resp = await upload_health_check(
        payload={"content": _json.dumps({"equity_curve": curve})},
        current_user={"tenant_id": "default", "user_id": "00000001"},
    )
    data = resp["data"]
    assert data["report"]["verdict"] in {"A", "B", "L", "E"}
    assert "结论标签" in data["report_text"]
    assert data["points"] == 300
    assert "不参与" in data["disclaimer"]

    with pytest.raises(HTTPException) as exc:
        await upload_health_check(payload={"content": "1,2"}, current_user={"tenant_id": "default", "user_id": "1"})
    assert exc.value.status_code == 400


# ── 手动重评入口（P2，2026-10-21）────────────────────────────────────


@pytest.mark.unit
def test_eval_run_endpoints_are_admin_only_and_do_not_run_scoring():
    src = (_BACKEND / "services/api/routers/eval_scores.py").read_text(encoding="utf-8")
    assert '@router.post("/run")' in src
    assert '@router.get("/run/status")' in src
    assert "Depends(require_admin)" in src
    # API 进程不得直接跑评分：重活必须在 trade worker
    assert "from backend.scripts.eval.run_all import run_all" not in src
    assert "run_eval_scores_once" not in src
    # 指令必须走 shared 契约（键名唯一出处）
    assert "from backend.shared import eval_trigger" in src


@pytest.mark.unit
@pytest.mark.asyncio
async def test_trigger_eval_run_queues(monkeypatch):
    from backend.services.api.routers import eval_scores as router

    req = {
        "request_id": "r1",
        "date": "2026-10-21",
        "tenant_id": "default",
        "requested_by": "1",
        "requested_at": "2026-10-21T07:00:00+00:00",
    }
    monkeypatch.setattr(router.eval_trigger, "eval_worker_enabled", lambda: True)
    monkeypatch.setattr(router, "_trade_redis", lambda: object())
    monkeypatch.setattr(router.eval_trigger, "request_run", lambda *a, **k: req)
    monkeypatch.setattr(
        router.eval_trigger,
        "build_run_status",
        lambda state, **k: {"state": state, "request_id": req["request_id"]},
    )

    resp = await router.trigger_eval_run(
        router.EvalRunRequest(date="2026-10-21"),
        current_user={"user_id": 1, "tenant_id": "default"},
    )
    assert resp["success"] is True
    assert resp["data"]["request"]["request_id"] == "r1"
    assert resp["data"]["status"]["state"] == "queued"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_trigger_eval_run_validation_and_gates(monkeypatch):
    from backend.services.api.routers import eval_scores as router

    # 非管理员由 Depends(require_admin) 拦；直调测试在这里只验业务闸门
    monkeypatch.setattr(router.eval_trigger, "eval_worker_enabled", lambda: False)
    with pytest.raises(HTTPException) as exc:
        await router.trigger_eval_run(
            router.EvalRunRequest(date=None), current_user={"user_id": 1}
        )
    assert exc.value.status_code == 503

    monkeypatch.setattr(router.eval_trigger, "eval_worker_enabled", lambda: True)
    with pytest.raises(HTTPException) as exc:
        await router.trigger_eval_run(
            router.EvalRunRequest(date="20261021"), current_user={"user_id": 1}
        )
    assert exc.value.status_code == 400


@pytest.mark.unit
@pytest.mark.asyncio
async def test_trigger_eval_run_busy_returns_409_with_status(monkeypatch):
    from backend.services.api.routers import eval_scores as router

    def fake_request(*a, **k):
        raise router.eval_trigger.EvalRunBusy({"state": "running", "request_id": "r0"})

    monkeypatch.setattr(router.eval_trigger, "eval_worker_enabled", lambda: True)
    monkeypatch.setattr(router, "_trade_redis", lambda: object())
    monkeypatch.setattr(router.eval_trigger, "request_run", fake_request)

    with pytest.raises(HTTPException) as exc:
        await router.trigger_eval_run(None, current_user={"user_id": 1})
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "eval_run_busy"
    assert exc.value.detail["status"]["state"] == "running"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_eval_run_status_returns_idle_when_no_record(monkeypatch):
    from backend.services.api.routers import eval_scores as router

    monkeypatch.setattr(router, "_trade_redis", lambda: object())
    monkeypatch.setattr(
        router.eval_trigger,
        "read_run_status",
        lambda _r: router.eval_trigger.idle_status(),
    )
    resp = await router.eval_run_status(current_user={"user_id": 1})
    assert resp["success"] is True
    assert resp["data"]["state"] == "idle"
