"""一档复盘的编排：取事实 → 调研究总控模型 → 落报告 → 更新次日 brief → 写对话流。

退出码（CLI 与 cron 共用）::

    0  成功
    1  参数错误
    3  LLM 调用失败 / 空回复
    4  研究总控 LLM 未配置（去「设置 → 研究总控」保存独立 key）
    5  连续两次被 max_tokens 截断（半截报告不落盘）

**失败不污染 brief**：任何失败都只更新 ``state``，不写报告、不 upsert brief——
次日消费方宁可拿到上一份完整稿，也不能拿到一段「模型调用失败」的占位。
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from typing import Any

from .config import (
    SLOTS,
    ResearchLLMConfig,
    ResearchLLMNotConfigured,
    describe,
    resolve_config,
)
from .dossier import (
    Dossier,
    brief_for_prompt,
    build_dossier,
    resolve_days,
)
from .prompts import SLOT_TITLES, build_system_prompt, build_user_prompt
from .store import (
    append_dialogue,
    read_report,
    report_path,
    update_state,
    upsert_brief,
    write_report,
)

#: 截断后重试用的一句话（追加在 user 末尾）
_TRUNCATE_HINT = (
    "\n\n【重要】上一次输出被 max_tokens 截断。本次请压缩到 1800 字以内："
    "七段标题必须保留，每段正文不超过 4 行；文末 JSON 围栏必须完整闭合。"
)

#: 机读块字段
_MACHINE_TEXT_KEYS = ("direction", "capital", "news", "models")
_MACHINE_LIST_KEYS = ("sectors", "risks")


def _log(msg: str) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"[research-control {stamp}] {msg}", flush=True)


def extract_machine(content: str) -> dict[str, Any]:
    """从报告里抽机读块：优先最后一个 ```json 围栏，其次最后一个平衡 JSON。"""
    blocks = re.findall(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", content or "")
    for raw in reversed(blocks):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    matches = list(re.finditer(r"\{[\s\S]*\}", content or ""))
    for m in reversed(matches):
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return {}


def normalize_machine(obj: dict[str, Any]) -> dict[str, Any]:
    """机读块收敛：文本字段截断、列表去空、池行只留契约键。"""
    out: dict[str, Any] = {}
    for key in _MACHINE_TEXT_KEYS:
        val = obj.get(key)
        out[key] = str(val or "").strip()[:600]
    for key in _MACHINE_LIST_KEYS:
        val = obj.get(key) or []
        if isinstance(val, str):
            val = [val]
        out[key] = [str(x).strip()[:200] for x in val if str(x).strip()][:8]
    pool = obj.get("pool") or []
    rows: list[dict[str, Any]] = []
    if isinstance(pool, list):
        for row in pool[:20]:
            if not isinstance(row, dict):
                continue
            code = str(row.get("code") or "").strip()
            name = str(row.get("name") or "").strip()
            if not code or not name:
                continue
            try:
                score = float(row.get("score") or 0)
            except (TypeError, ValueError):
                score = 0.0
            rows.append(
                {
                    "code": code,
                    "name": name,
                    "score": round(score, 2),
                    "reason": str(row.get("reason") or "").strip()[:160],
                }
            )
    out["pool"] = rows
    return out


def strip_wrapping(content: str) -> str:
    """去掉模型偶尔加的开场白/结束语，保留从第一个标题开始的正文。"""
    text = (content or "").strip()
    if not text:
        return text
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if re.match(r"^\s{0,3}#{1,3}\s+", line):
            return "\n".join(lines[i:]).strip()
    return text


def call_llm(
    prompt: str,
    *,
    config: ResearchLLMConfig,
    system: str | None = None,
    timeout_s: float | None = None,
) -> tuple[str, dict[str, int] | None, str]:
    """真调一次研究总控模型 → ``(正文, usage, finish_reason)``。

    ``reasoning_content`` 兜底（推理模型 ``content`` 可能为空）；``finish_reason``
    原样抬出。网络/HTTP 错误由调用方重试，本函数只负责一次调用。
    """
    try:
        import httpx
    except ImportError:  # pragma: no cover - 运行环境缺失时才走到
        import requests as httpx  # type: ignore[no-redef]

    payload: dict[str, Any] = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": system or build_system_prompt()},
            {"role": "user", "content": prompt},
        ],
        "temperature": config.temperature,
        "max_tokens": config.max_tokens,
    }
    if config.reasoning_effort:
        payload["reasoning_effort"] = config.reasoning_effort
    elif config.provider == "glm":
        # glm 族 thinking 不可关闭，low 档最省（night_pool 同口径）
        payload["reasoning_effort"] = "low"
    resp = httpx.post(
        config.chat_url,
        json=payload,
        headers={
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        },
        timeout=timeout_s or config.timeout,
    )
    if hasattr(resp, "raise_for_status"):
        resp.raise_for_status()
    data = resp.json()
    choices = data.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        raise RuntimeError(f"响应没有 choices：{str(data)[:300]}")
    msg = choices[0].get("message") or {}
    if not isinstance(msg, dict):
        raise RuntimeError(f"choices[0].message 不是对象：{str(choices[0])[:300]}")
    text = str(msg.get("content") or "").strip()
    if not text:
        text = str(msg.get("reasoning_content") or "").strip()
    usage = data.get("usage") or None
    if usage:
        usage = {
            k: int(usage.get(k) or 0)
            for k in ("prompt_tokens", "completion_tokens", "total_tokens")
            if k in usage
        }
    return text, usage, str(choices[0].get("finish_reason") or "").strip()


def _call_with_retry(
    prompt: str,
    cfg: ResearchLLMConfig,
    *,
    system: str | None = None,
) -> tuple[str, dict[str, int] | None, str]:
    """一次重试：网络/空回复重发；``length`` 用精简提示重发。"""
    last_exc: BaseException | None = None
    attempt_prompt = prompt
    for attempt in range(2):
        try:
            text, usage, finish = call_llm(attempt_prompt, config=cfg, system=system)
            if finish == "length":
                _log(f"输出被截断（max_tokens={cfg.max_tokens}），压缩后重试")
                attempt_prompt = prompt + _TRUNCATE_HINT
                last_exc = RuntimeError("finish_reason=length")
                continue
            if not text:
                raise RuntimeError("空回复")
            return text, usage, finish
        except Exception as exc:  # noqa: BLE001 - 供应商抖动都重试一次
            last_exc = exc
            _log(f"调用失败（第 {attempt + 1}/2 次）：{type(exc).__name__}: {exc}")
    assert last_exc is not None
    raise last_exc


def run_slot(
    slot: str,
    *,
    day: str | None = None,
    dry: bool = False,
    if_missing: bool = False,
    now: datetime | None = None,
    config: ResearchLLMConfig | None = None,
    dossier: Dossier | None = None,
    caller=None,
) -> int:
    """跑一档复盘。``caller(prompt, cfg)`` 可注入（测试用），默认 :func:`call_llm`。"""
    if slot not in SLOTS:
        _log(f"未知档位 {slot!r}（可选：{', '.join(SLOTS)}）")
        return 1
    day_str, target = resolve_days(slot, now=now, day=day)
    if if_missing and report_path(day_str, slot).is_file():
        _log(f"{SLOT_TITLES.get(slot, slot)} {day_str} 已有报告，跳过")
        return 0

    dossier = dossier or build_dossier(day=day_str, target=target, slot=slot, now=now)
    prior = brief_for_prompt(day_str)
    prompt = build_user_prompt(slot, dossier, prior_brief=prior)
    _log(
        f"档位={slot} 复盘日={day_str} 目标日={target} "
        f"事实包={len(dossier.render())}字 提示词={len(prompt)}字 "
        f"缺口={len(set(dossier.gaps))}"
    )
    if dry:
        print(dossier.render())
        print(f"\n（dry-run：未调用模型、未写盘；提示词 {len(prompt)} 字）")
        return 0

    cfg = config
    if cfg is None:
        try:
            cfg = resolve_config()
        except ResearchLLMNotConfigured as exc:
            _log(f"未配置：{exc}")
            update_state(day_str, slot, {"ok": False, "error": "not_configured"})
            return 4

    try:
        if caller is not None:
            result = caller(prompt, cfg)
            text, usage, finish = result if len(result) == 3 else (*result, "")
        else:
            text, usage, finish = _call_with_retry(prompt, cfg)
    except Exception as exc:  # noqa: BLE001
        _log(f"研究总控调用失败：{type(exc).__name__}: {exc}")
        update_state(
            day_str,
            slot,
            {
                "ok": False,
                "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                "target": target,
            },
        )
        return 5 if "length" in str(exc) else 3

    content = strip_wrapping(text)
    if not content:
        _log("模型返回空正文，未写盘")
        update_state(
            day_str, slot, {"ok": False, "error": "empty_output", "target": target}
        )
        return 3
    machine = normalize_machine(extract_machine(content))
    meta = {
        "model": cfg.model,
        "provider": cfg.provider,
        "usage": usage or {},
        "finish_reason": finish,
        "gaps": sorted(set(dossier.gaps)),
        "machine": machine,
        "source": cfg.source,
    }
    path = write_report(day_str, slot, content, target=target, meta=meta)
    upsert_brief(target, day_str, slot, content, machine=machine)
    append_dialogue(
        day_str, slot, prompt, content, usage=usage, gaps=sorted(set(dossier.gaps))
    )
    _log(
        f"完成：{path}（{len(content)} 字，usage={usage or '—'}，"
        f"机读块={'有' if machine else '无'}）"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI 入口（``python scripts/research_control.py --slot ...``）。"""
    import argparse

    ap = argparse.ArgumentParser(description="研究总控·多档复盘")
    ap.add_argument("--slot", choices=SLOTS, help="档位")
    ap.add_argument(
        "--date", help="复盘日 YYYY-MM-DD（默认今天；night 档凌晨默认昨天）"
    )
    ap.add_argument(
        "--dry-run", "--dry", action="store_true", dest="dry", help="只打印事实包"
    )
    ap.add_argument("--if-missing", action="store_true", help="该档已有报告则跳过")
    ap.add_argument("--status", action="store_true", help="打印配置现状")
    ap.add_argument("--list", action="store_true", help="打印五档与今日状态")
    args = ap.parse_args(argv)

    if args.status:
        print(json.dumps(describe(), ensure_ascii=False, indent=2))
        if not args.slot:
            return 0
    if args.list:
        from .store import data_root, read_state

        day = (args.date or datetime.now().strftime("%Y-%m-%d")).replace("/", "-")
        state = read_state(day).get("slots") or {}
        for slot in SLOTS:
            print(
                f"{slot:10s} {SLOT_TITLES.get(slot, '')}：{state.get(slot, '未运行')}"
            )
        print(f"目录：{data_root()}")
        return 0
    if not args.slot:
        ap.print_help()
        return 1
    return run_slot(
        args.slot,
        day=args.date,
        dry=args.dry,
        if_missing=args.if_missing,
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
