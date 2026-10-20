"""实盘账户评分卡（机构级批2 · F1）测试：纯函数口径 + 编排语义（全 hermetic，不碰库）。

覆盖：
- 持仓归一：list 形态 → dict 口径（volume 残留剔除、缺 market_value 兜底、
  代码归一 suffix、缺代码不合并）；
- 台账行：today_pnl 与实盘页契约同式（day_open>0 用 diff，否则原始值）；
- 缺快照语义：**照常出卡**、暴露维缺省且写明是哪一种缺（缺失/读取失败/另一座账户）；
- 空台账语义：skipped（不是 failed），错误文案说清是哪一层没行；
- 侧车：object_type 落 ``live_account``、文案走「实盘」口径（写盘路径打桩）；
- 单账户异常隔离 + 台账扫描失败信封。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


def _ctx_with_snapshot() -> dict:
    """30 天台账 + 最新快照的标准上下文（两行持仓，一只超 50% 红线）。"""
    rows = [
        {
            "date": "2026-10-10",
            "total_asset": 100000.0,
            "cash": 30000.0,
            "market_value": 70000.0,
            "day_open_equity": 99000.0,
            "today_pnl": 1000.0,
            "initial_equity": 100000.0,
            "position_count": 2,
        },
        {
            "date": "2026-10-13",
            "total_asset": 101000.0,
            "cash": 25000.0,
            "market_value": 76000.0,
            "day_open_equity": 100000.0,
            "today_pnl": 1000.0,
            "initial_equity": 100000.0,
            "position_count": 2,
        },
        {
            "date": "2026-10-14",
            "total_asset": 102000.0,
            "cash": 20000.0,
            "market_value": 82000.0,
            "day_open_equity": 101000.0,
            "today_pnl": 1000.0,
            "initial_equity": 100000.0,
            "position_count": 2,
        },
    ]
    positions = {
        "600036.SH": {
            "market_value": 70000.0,
            "price": 35.0,
            "cost": 30.0,
            "volume": 2000.0,
        },
        "000001.SZ": {
            "market_value": 12000.0,
            "price": 12.0,
            "cost": 11.0,
            "volume": 1000.0,
        },
    }
    return {
        "found": True,
        "canonical_user": "10000001",
        "account_id": "tdx-default-10000001",
        "rows": rows,
        "positions": positions,
        "snapshot": {
            "account_id": "tdx-default-10000001",
            "total_asset": 102000.0,
            "available_cash": 20000.0,
            "market_value": 82000.0,
            "snapshot_at": "2026-10-15T07:30:00",
            "source": "qmt_bridge",
            "source_downgraded": False,
        },
        "snapshot_at": "2026-10-15T07:30:00",
        "snapshot_source": "qmt_bridge",
        "snapshot_downgraded": False,
        "snapshot_mismatch": None,
        "snapshot_error": None,
        "missing_reason": "",
    }


def _bench() -> list[tuple[str, float]]:
    return [("2026-10-09", 3800.0), ("2026-10-13", 3810.0), ("2026-10-14", 3820.0)]


# ── 纯函数 ──────────────────────────────────────────────────────────


@pytest.mark.unit
def test_normalize_positions_list_to_dict_contract():
    from backend.scripts.eval.live_account_card import normalize_positions

    out = normalize_positions(
        [
            # 桥的两种键名混用 + 无 market_value（按 price×volume 兜底）
            {"stock_code": "600036", "volume": 2000, "price": 35.0, "cost_price": 30.0},
            # 前缀形态归一 suffix
            {
                "symbol": "SZ000001",
                "volume": 1000,
                "price": 12.0,
                "cost": 11.0,
                "market_value": 12000,
            },
            # 清仓残留（volume=0）剔除
            {"symbol": "600519.SH", "volume": 0, "price": 1700.0, "cost_price": 1600.0},
            # 缺代码不许与别人合并：给 #序号键
            {"volume": 100, "price": 10.0, "cost": 9.0},
        ]
    )
    assert set(out) == {"600036.SH", "000001.SZ", "#3"}
    assert out["600036.SH"] == {
        "market_value": 70000.0,
        "price": 35.0,
        "cost": 30.0,
        "volume": 2000.0,
    }
    assert out["#3"]["volume"] == 100.0


@pytest.mark.unit
def test_normalize_positions_rejects_non_list():
    from backend.scripts.eval.live_account_card import normalize_positions

    assert normalize_positions(None) == {}
    assert normalize_positions({"600036": {}}) == {}


@pytest.mark.unit
def test_ledger_row_dict_today_pnl_matches_contract():
    from backend.scripts.eval.live_account_card import ledger_row_dict

    # day_open>0 → total − day_open（同实盘页契约）
    row = ledger_row_dict(
        {
            "snapshot_date": "2026-10-14",
            "total_asset": 102000.0,
            "day_open_equity": 101000.0,
            "today_pnl_raw": 999.0,
            "cash": 20000.0,
            "market_value": 82000.0,
            "initial_equity": 100000.0,
            "position_count": 2,
        }
    )
    assert row["today_pnl"] == 1000.0
    assert row["date"] == "2026-10-14"
    # day_open=0 → 用券商原始值
    row2 = ledger_row_dict(
        {"snapshot_date": "2026-10-13", "total_asset": 101000.0, "today_pnl_raw": 500.0}
    )
    assert row2["today_pnl"] == 500.0
    # ORM 行（属性形态）同路
    row3 = ledger_row_dict(
        SimpleNamespace(
            snapshot_date=None, total_asset=1.0, day_open_equity=0.0, today_pnl_raw=0.0
        )
    )
    assert row3["date"] == "" and row3["total_asset"] == 1.0


@pytest.mark.unit
def test_exposure_missing_snapshot_reasons():
    from backend.scripts.eval.live_account_card import _exposure_from_context

    base = _ctx_with_snapshot()
    base["snapshot"] = None
    base["snapshot_error"] = "OperationalError: db down"
    dim = _exposure_from_context(base, 102000.0, None)
    assert dim.score is None and dim.detail["insufficient"] is True
    assert "读取失败" in dim.detail["note"] and "db down" in dim.detail["note"]

    base["snapshot_error"] = None
    base["snapshot_mismatch"] = "tdx-default-99999999"
    dim = _exposure_from_context(base, 102000.0, None)
    assert "另一座账户" in dim.detail["note"]

    base["snapshot_mismatch"] = None
    dim = _exposure_from_context(base, 102000.0, None)
    assert "无可用行" in dim.detail["note"]


# ── 编排（全打桩）───────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_score_live_account_skips_when_no_ledger(monkeypatch):
    from backend.scripts.eval import live_account_card as live_mod

    ctx = _ctx_with_snapshot()
    ctx["found"] = False
    ctx["rows"] = []
    ctx["missing_reason"] = "实盘台账无行（default:10000001 从未上报过日报台账）"
    monkeypatch.setattr(live_mod, "load_live_context", lambda *a, **k: _async(ctx))

    result = await live_mod.score_live_account("00000001")  # 别名 → 规范名
    assert result["skipped"] is True
    assert result["object_id"] == "10000001:CN"
    assert "台账无行" in result["error"]
    assert "score" not in result


@pytest.mark.unit
@pytest.mark.asyncio
async def test_score_live_account_full_card(monkeypatch):
    from backend.scripts.eval import live_account_card as live_mod

    captured: dict = {}

    def fake_save_series(object_type, object_id, payload, **kwargs):
        captured.update(
            {"object_type": object_type, "object_id": object_id, "payload": payload}
        )
        return {"path": "/tmp/x.json", "bytes": 1, "written": True, "note": None}

    monkeypatch.setattr(
        live_mod, "load_live_context", lambda *a, **k: _async(_ctx_with_snapshot())
    )
    monkeypatch.setattr(live_mod, "load_benchmark_closes", lambda *a, **k: _bench())
    monkeypatch.setattr(live_mod, "load_risk_summary", lambda *a, **k: _async(None))
    monkeypatch.setattr(live_mod, "save_series", fake_save_series)

    result = await live_mod.score_live_account("10000001")
    assert result["object_type"] == "live_account"
    assert result["object_id"] == "10000001:CN"
    assert result["score"] is not None
    assert set(result["dimensions"]) == {
        "exposure",
        "attribution",
        "risk_events",
        "capital_efficiency",
    }
    # 归因用台账净值序列 vs 沪深300（excess 原样带出）
    attribution = result["dimensions"]["attribution"]
    assert attribution["detail"]["excess"] == pytest.approx(0.02 - 0.005263, abs=1e-3)
    # 单票 70000/82000 ≈ 85% > 50% → 暴露红线
    assert "exposure" in (result.get("red_line_failed") or [])
    assert result["n_positions"] == 2
    # 侧车走实盘类型 + 实盘口径文案（不借模拟盘措辞）
    assert captured["object_type"] == "live_account"
    assert captured["object_id"] == "10000001:CN"
    assert captured["payload"]["scalars"]["live_total_asset"] == 102000.0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_score_live_account_no_snapshot_still_scores(monkeypatch):
    from backend.scripts.eval import live_account_card as live_mod

    ctx = _ctx_with_snapshot()
    ctx["snapshot"] = None
    ctx["positions"] = {}
    ctx["snapshot_error"] = "RuntimeError: view gone"
    monkeypatch.setattr(live_mod, "load_live_context", lambda *a, **k: _async(ctx))
    monkeypatch.setattr(live_mod, "load_benchmark_closes", lambda *a, **k: _bench())
    monkeypatch.setattr(live_mod, "load_risk_summary", lambda *a, **k: _async(None))
    monkeypatch.setattr(
        live_mod,
        "save_series",
        lambda *a, **k: {"path": "", "bytes": 0, "written": False, "note": "stub"},
    )

    result = await live_mod.score_live_account("10000001")
    assert result.get("skipped") is not True
    assert result["score"] is not None
    assert result["dimensions"]["exposure"]["score"] is None
    assert "读取失败" in result["dimensions"]["exposure"]["detail"]["note"]
    assert result["n_positions"] is None
    # 缺一维 → 权重归一照出总分，缺省维进 missing_dims（如实说明）
    assert result.get("missing_dims")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_score_all_live_accounts_isolates_and_reports_scan_failure(monkeypatch):
    from backend.scripts.eval import live_account_card as live_mod

    async def boom(*a, **k):
        raise RuntimeError("打分炸了")

    monkeypatch.setattr(
        live_mod, "list_live_users", lambda **k: _async(["10000001", "20000002"])
    )

    async def fake_score(user, **kwargs):
        if user == "20000002":
            return await boom()
        return {"object_type": "live_account", "object_id": f"{user}:CN", "score": 88.0}

    monkeypatch.setattr(live_mod, "score_live_account", fake_score)
    results = await live_mod.score_all_live_accounts()
    assert results[0]["score"] == 88.0
    assert "评分失败" in results[1]["error"]
    assert results[1]["object_id"] == "20000002:CN"

    async def scan_boom(**k):
        raise RuntimeError("pg down")

    monkeypatch.setattr(live_mod, "list_live_users", scan_boom)
    results = await live_mod.score_all_live_accounts()
    assert results == [
        {
            "object_type": "live_account",
            "object_id": "*",
            "error": "实盘台账扫描失败: pg down",
        }
    ]


@pytest.mark.unit
def test_render_card_skip_error_and_ok():
    from backend.scripts.eval.live_account_card import render_card

    assert "跳过" in render_card(
        {"object_id": "10000001:CN", "skipped": True, "error": "台账无行"}
    )
    assert "错误" in render_card({"object_id": "10000001:CN", "error": "打分失败"})
    text = render_card(
        {
            "object_id": "10000001:CN",
            "score": 81.0,
            "grade": "A",
            "total_asset": 102000.0,
            "n_positions": 2,
            "low_confidence": True,
            "red_line_failed": ["exposure"],
            "dimensions": {
                "exposure": {
                    "label": "风险暴露",
                    "weight": 30.0,
                    "score": 45.0,
                    "red_line_failed": True,
                },
                "attribution": {
                    "label": "归因",
                    "weight": 20.0,
                    "score": None,
                    "red_line_failed": False,
                },
            },
            "missing_dims": ["attribution"],
        }
    )
    assert "实盘账户评分卡 10000001:CN" in text
    assert "低置信" in text and "⚠红线" in text and "缺省" in text


async def _async(value):
    """monkeypatch 用的 async 常量。"""
    return value
