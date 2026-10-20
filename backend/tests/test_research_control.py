"""研究总控（``backend/shared/research_control/``）单元测试。

覆盖范围：

1. **独立配置**（config）：``QM_RESEARCH_LLM_*`` 优先、按 provider 回落现有
   供应商变量、key 只写不回（describe 永不泄漏明文）、非法输入一条都不落盘；
2. **落盘面**（store）：报告 / 次日 brief（阶段稿 → 最终稿）/ 七段提取 /
   最近 brief / 对话流条目结构；
3. **提示词**（prompts）：五档任务齐全、事实包与机读契约都进 user 消息；
4. **编排**（runner）：机读块抽取与收敛、失败不污染 brief、``--if-missing``、
   未配置与截断的退出码；
5. **事实包工具**（dossier）：复盘日/目标日推导、brief 紧凑文本的两种形态。

所有测试都把 ``QM_RUNTIME_ENV_FILE`` / ``QM_RESEARCH_CONTROL_DIR`` /
``QM_AGENT_DATA_DIR`` 指向 ``tmp_path``——绝不读写仓库里真实的 ``config/runtime.env``
与 ``data/`` 档案（部署后同日产物会污染断言）。
"""

from __future__ import annotations

import json
from datetime import date, datetime

import pytest

from backend.shared.research_control import config as rc_config
from backend.shared.research_control import dossier as rc_dossier
from backend.shared.research_control import prompts as rc_prompts
from backend.shared.research_control import runner as rc_runner
from backend.shared.research_control import store as rc_store

pytestmark = pytest.mark.unit

_DAY = "2026-10-14"
_TARGET = "20261015"

_ENV_KEYS = (
    "QM_RESEARCH_LLM_BASE_URL",
    "QM_RESEARCH_LLM_API_KEY",
    "QM_RESEARCH_LLM_MODEL",
    "QM_RESEARCH_LLM_PROVIDER",
    "QM_RESEARCH_LLM_TIMEOUT",
    "QM_RESEARCH_LLM_MAX_TOKENS",
    "QM_RESEARCH_LLM_TEMPERATURE",
    "QM_RESEARCH_LLM_REASONING_EFFORT",
    "GLM_API_BASE",
    "GLM_API_KEY",
    "OPENAI_API_BASE",
    "OPENAI_API_KEY",
    "DEEPSEEK_BASE_URL",
    "DEEPSEEK_API_KEY",
)


@pytest.fixture(autouse=True)
def _sandbox(tmp_path, monkeypatch):
    """隔离运行环境：配置/报告/对话流全部落在 ``tmp_path``。"""
    monkeypatch.setenv("QM_RUNTIME_ENV_FILE", str(tmp_path / "runtime.env"))
    monkeypatch.setenv("QM_RESEARCH_CONTROL_DIR", str(tmp_path / "rc"))
    monkeypatch.setenv("QM_AGENT_DATA_DIR", str(tmp_path / "agent"))
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    return tmp_path


def _cfg() -> rc_config.ResearchLLMConfig:
    return rc_config.ResearchLLMConfig(
        base_url="https://research.example/v1",
        api_key="sk-test-123456",
        model="glm-5.3-flash",
        provider="glm",
    )


def _dossier(slot: str = "close") -> rc_dossier.Dossier:
    return rc_dossier.Dossier(
        day=_DAY,
        target=_TARGET,
        slot=slot,
        now=datetime(2026, 10, 14, 16, 20, tzinfo=rc_store.CN_TZ),
        sections={"market": "指数上涨 1.2%"},
        gaps=["pg_unreachable"],
    )


_REPORT = """# 收盘复盘

## 一、市场与资金
指数上涨 1.2%。

```json
{"direction": "震荡偏强", "capital": "缩量", "sectors": ["半导体：资金回流"], "pool": []}
```
"""


# ── 配置 ─────────────────────────────────────────────────────────────


class TestConfig:
    def test_placeholder_detection(self):
        assert rc_config.looks_like_placeholder("")
        assert rc_config.looks_like_placeholder("your-key-here")
        assert not rc_config.looks_like_placeholder("sk-abcdef123456")

    def test_research_key_takes_precedence_and_infers_provider(self):
        cfg = rc_config.resolve_config(
            {
                "QM_RESEARCH_LLM_BASE_URL": "https://research.example/v1",
                "QM_RESEARCH_LLM_API_KEY": "sk-research-123456",
                "QM_RESEARCH_LLM_MODEL": "deepseek-chat",
            }
        )

        assert cfg.source == "research"
        assert cfg.provider == "deepseek"
        assert cfg.chat_url == "https://research.example/v1/chat/completions"

    def test_missing_research_key_falls_back_to_glm_env(self):
        cfg = rc_config.resolve_config(
            {
                "QM_RESEARCH_LLM_MODEL": "glm-5.3-flash",
                "GLM_API_BASE": "https://open.bigmodel.cn/api/paas/v4",
                "GLM_API_KEY": "glm-key-123456",
            }
        )

        assert cfg.source == "fallback"
        assert cfg.provider == "glm"
        assert cfg.api_key == "glm-key-123456"

    def test_deepseek_fallback_prefers_deepseek_vars(self):
        cfg = rc_config.resolve_config(
            {
                "QM_RESEARCH_LLM_MODEL": "deepseek-chat",
                "DEEPSEEK_BASE_URL": "https://api.deepseek.com/v1",
                "DEEPSEEK_API_KEY": "ds-key-123456",
                "OPENAI_API_BASE": "https://openai.example/v1",
                "OPENAI_API_KEY": "oa-key-123456",
            }
        )

        assert cfg.base_url == "https://api.deepseek.com/v1"
        assert cfg.api_key == "ds-key-123456"
        assert cfg.source == "fallback"

    def test_unconfigured_error_names_variables_without_values(self):
        with pytest.raises(rc_config.ResearchLLMNotConfigured) as err:
            rc_config.resolve_config({"QM_RESEARCH_LLM_API_KEY": "your-key"})

        text = str(err.value)
        assert "QM_RESEARCH_LLM_BASE_URL" in text
        assert "your-key" not in text

    def test_non_numeric_timeout_is_rejected_by_name(self):
        with pytest.raises(rc_config.ResearchLLMNotConfigured) as err:
            rc_config.resolve_config(
                {
                    "QM_RESEARCH_LLM_BASE_URL": "https://x/v1",
                    "QM_RESEARCH_LLM_API_KEY": "sk-123456789",
                    "QM_RESEARCH_LLM_TIMEOUT": "long",
                }
            )

        assert "QM_RESEARCH_LLM_TIMEOUT" in str(err.value)

    def test_describe_masks_key_without_leaking_plaintext(self):
        env = {
            "QM_RESEARCH_LLM_BASE_URL": "https://x/v1",
            "QM_RESEARCH_LLM_API_KEY": "sk-secret-1234567890",
        }

        out = rc_config.describe(env)

        assert out["ready"] is True
        assert out["key_configured"] is True
        assert "sk-secret-1234567890" not in json.dumps(out, ensure_ascii=False)
        assert out["key_mask"].startswith("sk-s")

    def test_describe_survives_bad_numeric_values(self):
        env = {
            "QM_RESEARCH_LLM_BASE_URL": "https://x/v1",
            "QM_RESEARCH_LLM_API_KEY": "sk-secret-1234567890",
            "QM_RESEARCH_LLM_MAX_TOKENS": "not-a-number",
        }

        out = rc_config.describe(env)

        assert out["max_tokens"] == rc_config.DEFAULT_MAX_TOKENS
        assert out["ready"] is False
        assert "QM_RESEARCH_LLM_MAX_TOKENS" in out["error"]

    def test_describe_reports_not_ready_with_reason(self):
        out = rc_config.describe({})

        assert out["ready"] is False
        assert "QM_RESEARCH_LLM_BASE_URL" in out["error"]

    def test_save_keeps_key_when_blank_then_clears_it(self, _sandbox):
        saved = rc_config.save_config(
            {
                "base_url": "https://api.deepseek.com/v1",
                "model": "deepseek-chat",
                "provider": "deepseek",
                "api_key": "sk-persist-123456",
            }
        )
        assert saved["ready"] is True
        assert saved["key_configured"] is True
        runtime = (_sandbox / "runtime.env").read_text(encoding="utf-8")
        assert "QM_RESEARCH_LLM_API_KEY=sk-persist-123456" in runtime

        kept = rc_config.save_config(
            {
                "base_url": "https://api.deepseek.com/v1",
                "model": "deepseek-reasoner",
                "provider": "deepseek",
                "api_key": "",
            }
        )
        assert kept["model"] == "deepseek-reasoner"
        assert kept["key_configured"] is True
        assert rc_config.get_saved_key_present() is True

        cleared = rc_config.save_config(
            {
                "base_url": "https://api.deepseek.com/v1",
                "model": "deepseek-reasoner",
                "provider": "deepseek",
                "api_key": "",
                "clear_api_key": True,
            }
        )
        assert cleared["key_configured"] is False
        assert rc_config.get_saved_key_present() is False

    @pytest.mark.parametrize(
        ("body", "needle"),
        [
            ({"base_url": "ftp://x/v1", "model": "m"}, "http"),
            ({"base_url": "https://x/v1", "model": "your-model"}, "model"),
            (
                {"base_url": "https://x/v1", "model": "m", "provider": "acme"},
                "provider",
            ),
        ],
    )
    def test_save_rejects_invalid_body(self, body, needle):
        with pytest.raises(ValueError) as err:
            rc_config.save_config(body)

        assert needle in str(err.value)

    def test_clear_config_removes_managed_keys(self, _sandbox):
        rc_config.save_config(
            {
                "base_url": "https://api.deepseek.com/v1",
                "model": "deepseek-chat",
                "provider": "deepseek",
                "api_key": "sk-clear-123456",
            }
        )

        out = rc_config.clear_config()

        assert out["ready"] is False
        assert out["key_configured"] is False
        runtime = _sandbox / "runtime.env"
        assert "QM_RESEARCH_LLM" not in runtime.read_text(encoding="utf-8")

    def test_test_connection_reports_missing_base_and_key(self):
        missing_base = rc_config.test_connection()
        assert missing_base["ok"] is False
        assert "base_url" in missing_base["error"]

        missing_key = rc_config.test_connection(
            {"base_url": "https://x/v1", "model": "glm-5.3-flash"}
        )
        assert missing_key["ok"] is False
        assert "API key" in missing_key["error"]


# ── 落盘 ─────────────────────────────────────────────────────────────


class TestStore:
    def test_write_report_updates_meta_and_state(self):
        content = "# 收盘复盘\n\n今日指数上涨。\n"

        path = rc_store.write_report(
            _DAY,
            "close",
            content,
            target=_TARGET,
            meta={"machine": {"direction": "偏强"}},
        )

        assert path.is_file()
        assert rc_store.read_report(_DAY, "close") == content
        meta = rc_store.read_report_meta(_DAY, "close")
        assert meta["target"] == _TARGET
        assert meta["title"] == "收盘复盘"
        assert meta["machine"] == {"direction": "偏强"}
        state = rc_store.read_state(_DAY)
        assert state["slots"]["close"]["ok"] is True
        assert [row["slot"] for row in rc_store.list_reports(_DAY)] == ["close"]

    def test_upsert_brief_moves_from_interim_to_final(self):
        midday = "## 明日结论\n方向偏强\n\n## 经验与教训\n- 不追高"
        rc_store.upsert_brief(
            _TARGET, _DAY, "midday", midday, machine={"direction": "偏强"}
        )
        meta = json.loads(rc_store.brief_meta_path(_TARGET).read_text(encoding="utf-8"))
        assert meta["status"] == "interim"
        md = rc_store.read_brief(_TARGET)
        assert "阶段稿" in md
        assert "当前结论（午间复盘）" in md

        night = "# 夜间综合\n\n## 明日结论\n- 方向：偏强\n"
        rc_store.upsert_brief(
            _TARGET,
            _DAY,
            "night",
            night,
            machine={"direction": "偏强", "capital": "缩量"},
        )
        meta = json.loads(rc_store.brief_meta_path(_TARGET).read_text(encoding="utf-8"))
        assert meta["status"] == "final"
        assert meta["final"]["machine"]["capital"] == "缩量"
        md = rc_store.read_brief(_TARGET)
        assert "最终稿" in md
        assert "## 明日结论（综合）" in md

    def test_upsert_brief_rerun_replaces_same_slot(self):
        rc_store.upsert_brief(_TARGET, _DAY, "midday", "第一版", machine={})
        rc_store.upsert_brief(_TARGET, _DAY, "midday", "第二版", machine={})

        meta = json.loads(rc_store.brief_meta_path(_TARGET).read_text(encoding="utf-8"))
        assert meta["sections"]["midday"]["content"] == "第二版"

    def test_upsert_brief_truncates_section_content(self):
        rc_store.upsert_brief(_TARGET, _DAY, "news", "x" * 3000)

        meta = json.loads(rc_store.brief_meta_path(_TARGET).read_text(encoding="utf-8"))
        assert (
            len(meta["sections"]["news"]["content"]) == rc_store.SECTION_CONTENT_LIMIT
        )

    def test_extract_section_stops_at_same_level_heading(self):
        text = "## 经验与教训\n- 教训一\n### 细项\n- 子项\n## 明日方向\n偏强"

        body = rc_store.extract_section(text, ("经验", "教训"))

        assert "教训一" in body
        assert "子项" in body
        assert "偏强" not in body
        assert rc_store.extract_section(text, ("不存在",)) == ""

    def test_summary_skips_headings_fences_and_markup(self):
        text = '# 标题\n\n```json\n{"a": 1}\n```\n\n- **第一句**正文\n第二句'

        assert rc_store.summary_of(text) == "第一句正文"

    def test_latest_brief_picks_most_recent_within_lookback(self):
        for target in ("20261009", "20261013"):
            path = rc_store.brief_path(target)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"# {target}\n", encoding="utf-8")

        key, md = rc_store.latest_brief("20261014")
        assert key == "20261013"
        assert "20261013" in md

        key, _ = rc_store.latest_brief("20261012")
        assert key == "20261009"
        assert rc_store.latest_brief("20261101", lookback_days=3) == ("", "")

    def test_append_dialogue_writes_research_control_entry(self):
        path = rc_store.append_dialogue(
            _DAY,
            "close",
            "提示词",
            "报告",
            usage={"total_tokens": 12},
            gaps=["pg_unreachable"],
        )

        rows = [
            json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
        ]
        assert rows[0]["signature"] == "market-research"
        assert rows[0]["kind"] == "research_control"
        assert rows[0]["slot"] == "close"
        assert [m["role"] for m in rows[0]["new_messages"]] == ["user", "assistant"]
        assert rows[0]["usage"] == {"total_tokens": 12}
        assert rows[0]["data_gaps"] == ["pg_unreachable"]


# ── 提示词 ───────────────────────────────────────────────────────────


class TestPrompts:
    def test_every_slot_has_task_and_title(self):
        assert set(rc_prompts.SLOT_TASKS) == set(rc_config.SLOTS)
        assert set(rc_prompts.SLOT_TITLES) == set(rc_config.SLOTS)

    def test_user_prompt_carries_task_facts_prior_and_contract(self):
        prompt = rc_prompts.build_user_prompt(
            "close", _dossier("close"), prior_brief="旧 brief 结论"
        )

        assert "收盘复盘" in prompt
        assert "指数上涨 1.2%" in prompt
        assert "旧 brief 结论" in prompt
        assert "```json" in prompt

    def test_night_task_asks_for_leading_conclusions(self):
        prompt = rc_prompts.build_user_prompt("night", _dossier("night"))

        assert "夜间综合总结" in prompt
        assert "## 明日结论" in prompt

    def test_system_prompt_forbids_fabrication_and_total_assets(self):
        text = rc_prompts.build_system_prompt()

        assert "数据缺口" in text
        assert "总资产" in text
        assert "绝不编造" in text


# ── 编排 ─────────────────────────────────────────────────────────────


class TestMachineParsing:
    def test_extract_prefers_last_fenced_json(self):
        content = (
            '正文\n```json\n{"direction": "旧"}\n```\n'
            '再改\n```json\n{"direction": "新"}\n```'
        )

        assert rc_runner.extract_machine(content) == {"direction": "新"}

    def test_extract_accepts_unfenced_balanced_object(self):
        assert rc_runner.extract_machine('结论 {"direction": "偏强"} 完') == {
            "direction": "偏强"
        }

    def test_extract_returns_empty_on_garbage(self):
        assert rc_runner.extract_machine("没有机读块") == {}
        assert rc_runner.extract_machine("```json\n{坏的}\n```") == {}

    def test_normalize_truncates_and_filters_pool(self):
        obj = {
            "direction": "d" * 700,
            "sectors": ["s" * 300] + ["x"] * 20,
            "pool": [
                {
                    "code": "600000.SH",
                    "name": "浦发银行",
                    "score": "88.5",
                    "reason": "r",
                    "extra": 1,
                },
                {"code": "600001.SH", "score": 90},
                "not-a-row",
            ],
            "risks": "单条风险",
        }

        out = rc_runner.normalize_machine(obj)

        assert len(out["direction"]) == 600
        assert len(out["sectors"]) == 8
        assert len(out["sectors"][0]) == 200
        assert out["pool"] == [
            {"code": "600000.SH", "name": "浦发银行", "score": 88.5, "reason": "r"}
        ]
        assert out["risks"] == ["单条风险"]

    def test_strip_wrapping_drops_preamble_only(self):
        assert rc_runner.strip_wrapping("好的，下面是报告。\n\n# 收盘复盘\n正文") == (
            "# 收盘复盘\n正文"
        )
        assert rc_runner.strip_wrapping("  纯正文  ") == "纯正文"


class TestRunSlot:
    def test_success_writes_report_brief_and_dialogue(self):
        calls = []

        def caller(prompt, cfg):
            calls.append((prompt, cfg))
            return _REPORT, {"total_tokens": 99}, "stop"

        code = rc_runner.run_slot(
            "close", day=_DAY, config=_cfg(), dossier=_dossier(), caller=caller
        )

        assert code == 0
        assert len(calls) == 1
        assert "指数上涨 1.2%" in calls[0][0]
        assert rc_store.read_report(_DAY, "close") == _REPORT.strip()
        meta = rc_store.read_report_meta(_DAY, "close")
        assert meta["machine"]["direction"] == "震荡偏强"
        assert meta["usage"] == {"total_tokens": 99}
        assert meta["gaps"] == ["pg_unreachable"]
        brief = json.loads(
            rc_store.brief_meta_path(_TARGET).read_text(encoding="utf-8")
        )
        assert brief["status"] == "interim"
        assert brief["sections"]["close"]["machine"]["capital"] == "缩量"
        log = (
            rc_store.agent_data_root() / "market-research" / "log" / _DAY / "log.jsonl"
        )
        entry = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
        assert entry["kind"] == "research_control"
        assert entry["slot"] == "close"

    def test_night_slot_marks_brief_final(self):
        code = rc_runner.run_slot(
            "night",
            day=_DAY,
            config=_cfg(),
            dossier=_dossier("night"),
            caller=lambda prompt, cfg: (_REPORT, None, "stop"),
        )

        assert code == 0
        meta = json.loads(rc_store.brief_meta_path(_TARGET).read_text(encoding="utf-8"))
        assert meta["status"] == "final"

    def test_failed_slot_does_not_pollute_existing_final_brief(self):
        rc_store.upsert_brief(
            _TARGET, _DAY, "night", "# 夜间\n最终正文", machine={"direction": "已定稿"}
        )

        def boom(prompt, cfg):
            raise RuntimeError("供应商 500")

        code = rc_runner.run_slot(
            "news",
            day=_DAY,
            config=_cfg(),
            dossier=_dossier("news"),
            caller=boom,
        )

        assert code == 3
        assert rc_store.read_report(_DAY, "news") == ""
        meta = json.loads(rc_store.brief_meta_path(_TARGET).read_text(encoding="utf-8"))
        assert meta["status"] == "final"
        assert meta["final"]["machine"]["direction"] == "已定稿"
        state = rc_store.read_state(_DAY)
        assert state["slots"]["news"]["ok"] is False
        assert "RuntimeError" in state["slots"]["news"]["error"]

    def test_length_failure_returns_five(self):
        def boom(prompt, cfg):
            raise RuntimeError("finish_reason=length")

        code = rc_runner.run_slot(
            "midday",
            day=_DAY,
            config=_cfg(),
            dossier=_dossier("midday"),
            caller=boom,
        )

        assert code == 5

    def test_empty_output_is_not_written(self):
        code = rc_runner.run_slot(
            "midday",
            day=_DAY,
            config=_cfg(),
            dossier=_dossier("midday"),
            caller=lambda prompt, cfg: ("   ", None, "stop"),
        )

        assert code == 3
        assert rc_store.read_report(_DAY, "midday") == ""

    def test_if_missing_skips_existing_report(self):
        rc_store.write_report(_DAY, "midday", "旧稿", target=_TARGET)

        def must_not_call(prompt, cfg):
            pytest.fail("已有报告不应再调模型")

        code = rc_runner.run_slot(
            "midday",
            day=_DAY,
            if_missing=True,
            config=_cfg(),
            dossier=_dossier("midday"),
            caller=must_not_call,
        )

        assert code == 0
        assert rc_store.read_report(_DAY, "midday") == "旧稿"

    def test_unknown_slot_returns_one(self):
        code = rc_runner.run_slot(
            "dawn", config=_cfg(), caller=lambda prompt, cfg: ("x", None, "")
        )

        assert code == 1

    def test_unconfigured_llm_returns_four_without_writing(self, monkeypatch):
        def raise_not_configured():
            raise rc_config.ResearchLLMNotConfigured("QM_RESEARCH_LLM_API_KEY 为空")

        monkeypatch.setattr(rc_runner, "resolve_config", raise_not_configured)

        code = rc_runner.run_slot(
            "midday",
            day=_DAY,
            dossier=_dossier("midday"),
            caller=lambda prompt, cfg: pytest.fail("未配置不应调用模型"),
        )

        assert code == 4
        assert rc_store.read_report(_DAY, "midday") == ""
        state = rc_store.read_state(_DAY)
        assert state["slots"]["midday"]["error"] == "not_configured"

    def test_dry_run_prints_facts_without_writing(self, capsys):
        code = rc_runner.run_slot(
            "midday", day=_DAY, dry=True, config=_cfg(), dossier=_dossier("midday")
        )

        out = capsys.readouterr().out
        assert code == 0
        assert "指数上涨 1.2%" in out
        assert "dry-run" in out
        assert rc_store.read_report(_DAY, "midday") == ""


# ── 事实包工具 ───────────────────────────────────────────────────────


class TestDossierHelpers:
    def test_next_trading_day_skips_weekend(self):
        assert rc_dossier.next_trading_day(date(2026, 10, 16)) == date(2026, 10, 19)
        assert rc_dossier.next_trading_day(date(2026, 10, 17)) == date(2026, 10, 19)

    def test_resolve_days_night_before_eight_uses_yesterday(self):
        now = datetime(2026, 10, 15, 2, 10, tzinfo=rc_store.CN_TZ)

        assert rc_dossier.resolve_days("night", now=now) == ("2026-10-14", "20261015")

    def test_resolve_days_midday_uses_today_and_next_weekday(self):
        now = datetime(2026, 10, 15, 11, 40, tzinfo=rc_store.CN_TZ)

        assert rc_dossier.resolve_days("midday", now=now) == ("2026-10-15", "20261016")

    def test_resolve_days_explicit_day_wins(self):
        now = datetime(2026, 10, 15, 11, 40, tzinfo=rc_store.CN_TZ)

        assert rc_dossier.resolve_days("close", now=now, day="2026-10-16") == (
            "2026-10-16",
            "20261019",
        )

    def test_render_marks_empty_sections_and_gap_summary(self):
        d = rc_dossier.Dossier(
            day=_DAY,
            target=_TARGET,
            slot="close",
            now=datetime(2026, 10, 14, 16, 20, tzinfo=rc_store.CN_TZ),
            gaps=["pg_unreachable"],
        )

        text = d.render()

        assert "（数据缺口：本段无可用数据）" in text
        assert "## 数据缺口汇总" in text
        assert "pg_unreachable" in text

    def test_brief_for_prompt_empty_without_archive(self):
        assert rc_dossier.brief_for_prompt(_TARGET) == ""

    def test_brief_for_prompt_uses_final_machine(self):
        rc_store.upsert_brief(
            _TARGET,
            _DAY,
            "night",
            "# 夜间\n正文",
            machine={
                "direction": "震荡偏强",
                "capital": "缩量",
                "risks": ["外盘"],
            },
        )

        text = rc_dossier.brief_for_prompt(_TARGET)

        assert "【研究总控·2026-10-15 交易日前瞻】（最终稿）" in text
        assert "明日方向：震荡偏强" in text
        assert "- 风险：外盘" in text

    def test_brief_for_prompt_falls_back_to_latest_md(self):
        path = rc_store.brief_path("20261014")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "# 研究总控 · 2026-10-14 交易日前瞻\n\n阶段稿正文", encoding="utf-8"
        )

        text = rc_dossier.brief_for_prompt(_TARGET)

        assert "最近复盘与今日要点" in text
        assert "目标交易日 2026-10-14" in text
