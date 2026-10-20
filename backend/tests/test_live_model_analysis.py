"""宿主对话轮（``scripts/live_model_analysis.py``）测试。

覆盖 2026-10-12 晚补丁的三块：
1. **复盘轮**（``--mode review``）：指数块 / 持仓「今日」列 / 复盘要求模板 /
   ``mode`` 日志标记；
2. **摘要与卡片**：``extract_summary``（纯函数）与 ``format_review_card``；
3. **提醒面**：``decide_transition_alert`` 翻转状态机、``_flag_on`` 词表、
   ``format_round_alert``、``notify_qq`` best-effort、告警状态文件读写；
4. **截断重试**：第 2 次尝试带「精简输出」提示 + ``QM_DIALOGUE_MAX_TOKENS`` 覆盖。

脚本不是包内模块，用 importlib 从 ``scripts/`` 直接装载（import 无副作用：
不读 .env、不连桥、不建目录，路径常量只是 Path 拼接）。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "live_model_analysis.py"
_CN_TZ = timezone(timedelta(hours=8))


@pytest.fixture(scope="module")
def lma():
    spec = importlib.util.spec_from_file_location(
        "live_model_analysis_under_test", _SCRIPT
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _isolate_research_brief(lma, monkeypatch):
    """提示词测试不读真实 repo 的研究总控档案（部署后同日产物会出现在 data/）。"""
    monkeypatch.setattr(lma, "_research_block", lambda: "")


def _pos(code: str = "601000.SH", cost: float = 4.5, volume: float = 1000) -> dict:
    return {"code": code, "name": "唐山港", "volume": volume, "cost_price": cost}


def _ledger(*positions: dict, quota: float = 100_000.0, known: bool = True) -> dict:
    used = round(sum(p["volume"] * p["cost_price"] for p in positions), 2)
    return {
        "quota": quota,
        "used": used,
        "remaining": round(quota - used, 2),
        "known": known,
        "positions": list(positions),
    }


# ── 1. 复盘轮提示词拼装 ────────────────────────────────────────────────────


def test_review_content_has_index_block_positions_day_pct_and_requirement(lma):
    """复盘轮：指数当日表现 + 持仓「今日」列 + 复盘要求（摘要指令）。"""
    snap_calls: list[str] = []

    def snapshot_reader(env, code):
        snap_calls.append(code)
        return {"now": 10.0, "last_close": 9.5}

    def price_reader(env, code):  # 复盘轮不该走现价读（应走快照）
        raise AssertionError("review 模式不应调用 price_reader")

    content, gaps = lma.build_user_content(
        {},
        ledger=_ledger(_pos()),
        availability={"601000.SH": 1000},
        price_reader=price_reader,
        snapshot_reader=snapshot_reader,
        mode="review",
    )

    assert gaps == []
    # 指数块：三大指数 + 涨跌（10.0/9.5-1=+5.26%）
    assert "【收盘行情】" in content
    assert "上证指数" in content and "深证成指" in content and "创业板指" in content
    assert "+5.26%" in content
    # 持仓表「今日」列 + 行尾值
    assert "| 浮动盈亏 | 今日 |" in content
    assert "| +122.22% | +5.26% |" in content
    # 隐私边界：只给授予额度，不给账户总额（2026-10-19 用户拍板）
    assert "用户授予 ¥100,000 ｜ 已用（持仓成本） ¥4,500 ｜ 剩余 ¥95,500" in content
    assert "账户总资产、可用资金与其他 agent 的额度不是你的可见范围" in content
    assert "1,015,484" not in content  # 真实账户总额绝不入提示词
    # 复盘要求：三问 + 摘要指令
    assert "收盘复盘" in content
    assert "收盘复盘摘要" in content
    # 指数与持仓快照都走了 snapshot_reader（3 指数 + 1 持仓）
    assert snap_calls == ["000001.SH", "399001.SZ", "399006.SZ", "601000.SH"]


def test_review_content_index_unavailable_degrades_honestly(lma):
    """指数快照全失败：明说不可用，不编数。"""
    content, _ = lma.build_user_content(
        {},
        ledger=_ledger(),
        snapshot_reader=lambda env, code: {},
        mode="review",
    )
    assert "【收盘行情】指数数据不可用" in content


def test_intraday_content_unchanged_shape(lma):
    """盘中轮不回归：无指数块、无「今日」列、现价走 price_reader。"""
    content, gaps = lma.build_user_content(
        {},
        ledger=_ledger(_pos()),
        availability={"601000.SH": 1000},
        price_reader=lambda env, code: 10.0,
        snapshot_reader=lambda env, code: (_ for _ in ()).throw(
            AssertionError("intraday 不应调用 snapshot_reader")
        ),
    )
    assert gaps == []
    assert "【收盘行情】" not in content
    assert "今日 |" not in content
    assert "| +122.22% |" in content  # 10.0/4.5-1
    assert "盘中分析" in content


def test_account_unreachable_still_forbids_position_commentary_both_modes(lma):
    for mode in ("intraday", "review"):
        content, gaps = lma.build_user_content({}, ledger=None, mode=mode)
        assert gaps == ["account_unreachable"]
        assert "禁止" in content and "编造" in content


def test_requirement_block_review_vs_intraday_summary_instruction(lma):
    review = lma._requirement_block(has_account=True, has_positions=True, mode="review")
    assert "收盘复盘摘要" in review and "明日" in review
    intraday = lma._requirement_block(has_account=True, has_positions=True)
    assert "盘中分析" in intraday and "收盘复盘摘要" not in intraday
    # 空仓复盘也带摘要指令（复用同一常量）
    empty = lma._requirement_block(has_account=True, has_positions=False, mode="review")
    assert "收盘复盘摘要" in empty and "空仓" in empty


# ── 2. 摘要抽取与卡片 ─────────────────────────────────────────────────────


def test_extract_summary_skips_heading_and_keeps_its_paragraph(lma):
    """标题不是摘要（2026-10-20 实况：卡片上只有「收盘复盘摘要」四个字）。"""
    content = (
        "# 收盘复盘摘要\n**今日大盘**微跌 `0.3%`。\n\n## 详细展开\n后面还有很多字…"
    )
    assert lma.extract_summary(content) == "今日大盘微跌 0.3%。"


def test_extract_summary_skips_leading_disclaimer(lma):
    """免责声明不是摘要（同日实况：deepseek-v4-pro 卡片只显示「仅复盘观点…」）。"""
    content = (
        "## 收盘复盘摘要\n仅复盘观点，不构成自动交易指令。\n\n"
        "## 一、今日大盘\n沪指窄幅震荡，成交缩量。\n\n后面还有很多字…"
    )
    assert lma.extract_summary(content) == "沪指窄幅震荡，成交缩量。"


def test_extract_summary_handles_bold_label_and_bullets(lma):
    """模型可能用加粗行当标题、用列表写摘要——都要能抽到正文。"""
    content = (
        "**收盘复盘摘要**\n- 大盘：微跌 0.3%\n- 持仓：浮盈 +3.1%\n\n"
        "## 详细展开\n后面还有很多字…"
    )
    assert lma.extract_summary(content) == "大盘：微跌 0.3% 持仓：浮盈 +3.1%"


def test_extract_summary_limits_length(lma):
    out = lma.extract_summary("字" * 300, limit=160)
    assert len(out) == 160 and out.endswith("…")


def test_extract_summary_empty_content(lma):
    assert lma.extract_summary("") == ""
    assert lma.extract_summary("\n\n  \n") == ""


def test_format_review_card_shape(lma):
    now = datetime(2026, 10, 12, 15, 30, tzinfo=_CN_TZ)
    title, content = lma.format_review_card([("m1", "摘要一"), ("m2", "")], now)
    assert title == "📋 收盘复盘 · 10-12"
    assert "◆ m1" in content and "摘要一" in content
    assert "（本轮无摘要）" in content
    assert "━" * 14 in content
    assert "交易台 › 模型对话" in content


def test_format_review_card_includes_pool_sectors_and_scores(lma):
    """「包括晚上推理出来的模型 top20 的股票板块都要发我」——复盘卡带当日池。"""
    now = datetime(2026, 10, 12, 15, 30, tzinfo=_CN_TZ)
    rows = [
        {
            "code": "688543.SH",
            "name": "国科军工",
            "industry": "国防军工",
            "score": 31.21,
        },
        {"code": "300432.SZ", "name": "富临精工", "industry": "", "score": None},
    ]
    _, content = lma.format_review_card([("m1", "摘要一")], now, pool_rows=rows)
    assert "📦 今日池 · 2 只" in content
    assert "01 国科军工 688543.SH · 国防军工 · 31.2" in content
    assert "02 富临精工 300432.SZ · —" in content  # 缺板块显示 —，不编造


def test_format_review_card_drops_pool_rows_over_budget(lma):
    now = datetime(2026, 10, 12, 15, 30, tzinfo=_CN_TZ)
    rows = [
        {"code": f"6000{i:02d}.SH", "name": "股票", "industry": "行业", "score": 10}
        for i in range(20)
    ]
    _, content = lma.format_review_card(
        [("m1", "摘要一")], now, pool_rows=rows, max_chars=420
    )
    assert len(content) <= 420
    assert "另有" in content and "交易台" in content


def test_load_pool_snapshot_joins_industry(lma, monkeypatch, tmp_path):
    picks = tmp_path / "data" / "reports" / "stock_picks"
    picks.mkdir(parents=True)
    (picks / "20261020_agent_picks.json").write_text(
        json.dumps(
            {
                "picks": [
                    {"code": "688543.SH", "name": "国科军工", "score": 31.21},
                    {"code": "300432.SZ", "name": "富临精工", "industry": "汽车零部件"},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(lma, "ROOT", tmp_path)
    monkeypatch.setattr(lma, "load_industry_map", lambda: {"688543.SH": "国防军工"})
    rows = lma.load_pool_snapshot(datetime(2026, 10, 20, 15, 30))
    assert rows == [
        {
            "code": "688543.SH",
            "name": "国科军工",
            "industry": "国防军工",
            "score": 31.21,
        },
        {
            "code": "300432.SZ",
            "name": "富临精工",
            "industry": "汽车零部件",
            "score": None,
        },
    ]


# ── 3. 提醒面 ─────────────────────────────────────────────────────────────


def test_decide_transition_alert_state_machine(lma):
    assert lma.decide_transition_alert(False, False) is None
    assert lma.decide_transition_alert(False, True) == "down"
    assert lma.decide_transition_alert(True, True) is None  # 持续失联不重复推
    assert lma.decide_transition_alert(True, False) == "up"


def test_flag_on_default_on_wordlist(lma):
    assert lma._flag_on({}, "QM_DIALOGUE_ALERTS") is True
    assert lma._flag_on({"QM_DIALOGUE_ALERTS": "true"}, "QM_DIALOGUE_ALERTS") is True
    assert lma._flag_on({"QM_DIALOGUE_ALERTS": "1"}, "QM_DIALOGUE_ALERTS") is True
    for off in ("0", "false", "FALSE", "no", "off", " Off "):
        assert lma._flag_on({"QM_DIALOGUE_ALERTS": off}, "QM_DIALOGUE_ALERTS") is False
    assert lma._flag_on({}, "X", default=False) is False


def test_format_round_alert_partial_and_total(lma):
    title, content = lma.format_round_alert(["b:boom", "c:length"], 1, 3)
    assert "不满员" in title and "1/3" in content and "b、c" in content
    title2, content2 = lma.format_round_alert(["a:e1", "b:e2"], 0, 2)
    assert "全部失败" in title2 and "a、b" in content2


def test_notify_qq_best_effort(lma, monkeypatch):
    sent: list[tuple] = []

    class _FakePush:
        @staticmethod
        def notify(title, content, channel="default"):
            sent.append((title, content, channel))

    monkeypatch.setitem(sys.modules, "push_notify", _FakePush)
    assert lma.notify_qq("T", "C") is True
    assert sent == [("T", "C", "default")]

    class _BoomPush:
        @staticmethod
        def notify(title, content, channel="default"):
            raise RuntimeError("qq down")

    monkeypatch.setitem(sys.modules, "push_notify", _BoomPush)
    assert lma.notify_qq("T", "C") is False  # 绝不抛


def test_alert_state_roundtrip_and_corrupt_tolerance(lma, tmp_path, monkeypatch):
    state_file = tmp_path / ".alert_state.json"
    monkeypatch.setattr(lma, "ALERT_STATE_FILE", state_file)
    assert lma._load_alert_state() == {}  # 不存在 → 空
    lma._save_alert_state({"bridge_down": True, "last_round_alert_ts": 123.0})
    assert lma._load_alert_state()["bridge_down"] is True
    state_file.write_text("{ not json", encoding="utf-8")
    assert lma._load_alert_state() == {}  # 坏文件 → 空，不抛


def test_append_log_review_mode_marker(lma, tmp_path, monkeypatch):
    monkeypatch.setattr(lma, "LOG_DIR", tmp_path)
    path = lma.append_log("u", "a", "m1", None, ["account_unreachable"], mode="review")
    entry = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert entry["mode"] == "review"
    assert entry["data_gaps"] == ["account_unreachable"]
    path2 = lma.append_log("u", "a", "m1", None, None)
    entry2 = json.loads(path2.read_text(encoding="utf-8").splitlines()[-1])
    assert "mode" not in entry2


# ── 4. 截断重试与 token 覆盖 ──────────────────────────────────────────────


class _Resp:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _ok_payload(content: str, finish: str) -> dict:
    return {
        "choices": [{"message": {"content": content}, "finish_reason": finish}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    }


def test_call_model_retry_appends_brevity_note(lma, monkeypatch):
    """首次截断 → 第 2 次尝试必须带「精简输出」提示，且首两次 payload 其余一致。"""
    import requests

    calls: list[dict] = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(json)
        if len(calls) == 1:
            return _Resp(_ok_payload("半截话", "length"))
        return _Resp(_ok_payload("完整回复", "stop"))

    monkeypatch.setattr(requests, "post", fake_post)
    env = {"OPENAI_API_BASE": "https://x.test/v1", "OPENAI_API_KEY": "k"}
    content, usage = lma.call_model(
        env, "deepseek-v4-flash", "deepseek-v4-flash", "正文"
    )
    assert content == "完整回复" and usage["total_tokens"] == 30
    assert len(calls) == 2
    first, second = calls[0], calls[1]
    assert first["max_tokens"] == 8000 and second["max_tokens"] == 8000
    assert "大幅精简" not in first["messages"][1]["content"]
    assert "大幅精简" in second["messages"][1]["content"]
    assert second["messages"][1]["content"].startswith("正文")
    # 其余字段逐字不动
    assert first["messages"][0] == second["messages"][0]
    assert first["temperature"] == second["temperature"] == 0.3


def test_call_model_env_max_tokens_override_and_invalid_fallback(lma, monkeypatch):
    import requests

    seen: list[int] = []

    def fake_post(url, headers=None, json=None, timeout=None):
        seen.append(json["max_tokens"])
        return _Resp(_ok_payload("好", "stop"))

    monkeypatch.setattr(requests, "post", fake_post)
    env = {
        "OPENAI_API_BASE": "https://x.test/v1",
        "OPENAI_API_KEY": "k",
        "QM_DIALOGUE_MAX_TOKENS": "12000",
    }
    lma.call_model(env, "deepseek-v4-flash", "deepseek-v4-flash", "x")
    assert seen == [12000]
    # 非法值 → 回退默认，不抛
    env["QM_DIALOGUE_MAX_TOKENS"] = "abc"
    lma.call_model(env, "deepseek-v4-flash", "deepseek-v4-flash", "x")
    assert seen[-1] == lma.MAX_TOKENS == 8000


def test_call_model_truncated_twice_raises(lma, monkeypatch):
    """两次都截断 → TruncatedOutputError（整轮作废语义不回归）。"""
    import requests

    monkeypatch.setattr(
        requests, "post", lambda *a, **kw: _Resp(_ok_payload("半截", "length"))
    )
    env = {"OPENAI_API_BASE": "https://x.test/v1", "OPENAI_API_KEY": "k"}
    with pytest.raises(lma.TruncatedOutputError):
        lma.call_model(env, "deepseek-v4-flash", "deepseek-v4-flash", "x")


# ── 5. 甲5：决策轮在岗到点 → 盘中轮让位（宿主和决策轮同槽同文件，不许双写） ──


def _marker(tmp_path, monkeypatch, *, slots, age_s=0.0):
    """写一份「决策轮在岗」标记并把读侧的默认落点指到 tmp（不碰仓库里的 config/）。"""
    from backend.shared import decision_round_onduty as od

    path = tmp_path / "decision_round.onduty.json"
    monkeypatch.setattr(od, "MARKER_PATH", path)
    at = datetime.now(_CN_TZ) - timedelta(seconds=age_s)
    assert od.write_on_duty(slots, now=at, path=path) is True
    return od


def _slot_at(now: datetime, offset_min: int = 0) -> str:
    shifted = now + timedelta(minutes=offset_min)
    return shifted.strftime("%H%M")


def _run_main(lma, monkeypatch, tmp_path, argv, *, env=None):
    """跑一次 ``main()``：返回 ``(返回码, 调过的模型, 落盘条数)``。全程不碰网络/磁盘真路径。"""
    monkeypatch.setattr(lma, "LOCK", tmp_path / "lock")
    monkeypatch.setattr(lma, "_load_env", lambda: dict(env or {}))
    monkeypatch.setattr(lma, "build_user_content", lambda e, **kw: ("提示词", []))
    monkeypatch.setattr(lma, "notify_qq", lambda *a, **kw: False)
    monkeypatch.setattr(lma, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(lma, "ALERT_STATE_FILE", tmp_path / ".alert_state.json")
    # 账本/桥都是外部依赖：测试里给一本空账，绝不连真 PG/桥
    monkeypatch.setattr(
        lma, "read_agent_ledgers", lambda e, agents: {a: _ledger() for a in agents}
    )
    monkeypatch.setattr(lma, "_bridge_availability", lambda e: {})
    called: list[str] = []
    monkeypatch.setattr(
        lma,
        "call_model",
        lambda e, sig, model, content: (called.append(sig) or "分析正文", None),
    )
    monkeypatch.setattr(lma, "append_log", lambda *a, **kw: None)
    monkeypatch.setattr(sys, "argv", ["live_model_analysis.py", *argv])
    return lma.main(), called


def test_intraday_round_yields_when_the_decision_round_is_on_duty(
    lma, monkeypatch, tmp_path, capsys
):
    """决策轮在岗且当前落在它的槽位窗内 → 一个模型都不调，返回 0，并打印让位理由。

    这是甲5 的全部目的：同一个槽位只该有一份对话（决策轮那份），否则用户在「模型
    对话」里会看到两张卡片——一张拍板落单、一张明令不下单。
    """
    now = datetime.now(_CN_TZ)
    _marker(tmp_path, monkeypatch, slots=[_slot_at(now)])

    code, called = _run_main(lma, monkeypatch, tmp_path, [])

    assert code == 0 and called == []
    out = capsys.readouterr().out
    assert "[让位]" in out and "--force" in out  # 让位是看得见的行为，不是静默跳过


def test_intraday_round_yields_only_inside_the_slot_window(lma, monkeypatch, tmp_path):
    """标记新鲜但当前**不在**槽位窗内 → 照跑（12:00 之外的空档不归决策轮管）。"""
    now = datetime.now(_CN_TZ)
    _marker(tmp_path, monkeypatch, slots=[_slot_at(now, 30)])

    code, called = _run_main(lma, monkeypatch, tmp_path, [])

    assert code == 0 and called == list(lma.ROSTER)


def test_stale_marker_makes_the_dialogue_round_resume(lma, monkeypatch, tmp_path):
    """worker 死了 → 心跳停 → 标记过期 → **照跑**。

    方向是故意的：判据失灵时宁可回到「两边同跑」的老行为，也不能让「模型对话」
    静默变空——那正是本次要修的投诉。
    """
    from backend.shared.decision_round_onduty import MAX_AGE_S

    now = datetime.now(_CN_TZ)
    _marker(
        tmp_path,
        monkeypatch,
        slots=[_slot_at(now)],
        age_s=MAX_AGE_S + 60,
    )

    code, called = _run_main(lma, monkeypatch, tmp_path, [])

    assert code == 0 and called == list(lma.ROSTER)


def test_force_overrides_the_yield(lma, monkeypatch, tmp_path):
    """``--force``（「立即分析」按钮走这条路）在人按的场景下永不让位。"""
    now = datetime.now(_CN_TZ)
    _marker(tmp_path, monkeypatch, slots=[_slot_at(now)])

    code, called = _run_main(lma, monkeypatch, tmp_path, ["--force"])

    assert code == 0 and called == list(lma.ROSTER)


def test_review_mode_never_yields(lma, monkeypatch, tmp_path):
    """15:30 复盘轮是收盘语义（非盘中决策），决策轮在岗也不让位。"""
    now = datetime.now(_CN_TZ)
    _marker(tmp_path, monkeypatch, slots=[_slot_at(now)])

    code, called = _run_main(lma, monkeypatch, tmp_path, ["--mode", "review"])

    assert code == 0 and called == list(lma.ROSTER)


def test_yield_switch_off_restores_the_old_double_write(lma, monkeypatch, tmp_path):
    """``QM_DIALOGUE_YIELD_TO_DECISION_ROUND=off`` 是运维的逃生门（回到两边同跑）。"""
    now = datetime.now(_CN_TZ)
    _marker(tmp_path, monkeypatch, slots=[_slot_at(now)])

    code, called = _run_main(
        lma,
        monkeypatch,
        tmp_path,
        [],
        env={"QM_DIALOGUE_YIELD_TO_DECISION_ROUND": "off"},
    )

    assert code == 0 and called == list(lma.ROSTER)


def test_yield_predicate_failure_falls_open_to_running(lma, monkeypatch, tmp_path):
    """判据本身炸了（模块缺失/标记不可读）→ 照跑，并把「判据不可用」打进日志。

    fail-open 是契约的一部分（见 ``_yield_to_decision_round`` 的 docstring）：
    判据失灵只该让展示面重复，不该让它空。
    """
    now = datetime.now(_CN_TZ)
    od = _marker(tmp_path, monkeypatch, slots=[_slot_at(now)])

    def boom(**kwargs):
        raise OSError("盘掉")

    monkeypatch.setattr(od, "read_on_duty", boom)

    code, called = _run_main(lma, monkeypatch, tmp_path, [])

    assert code == 0 and called == list(lma.ROSTER)
