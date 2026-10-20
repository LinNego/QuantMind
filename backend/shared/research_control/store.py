"""研究总控的落盘面：报告 / 次日 brief / 对话流。

目录（``QM_RESEARCH_CONTROL_DIR`` 可覆盖，默认 ``data/research_control``）::

    reports/{YYYY-MM-DD}/{slot}.md     人读报告（进对话流的就是它）
    reports/{YYYY-MM-DD}/{slot}.json   元信息 + 机读块 + usage + 数据缺口
    brief/{YYYYMMDD}.md                次日共享 brief（别的 agent 读这个）
    brief/{YYYYMMDD}.json              分档 section 的权威结构（md 由它渲染）
    state/{YYYY-MM-DD}.json            当日各档运行状态（前端/排障用）

**brief 按目标交易日命名**：D 日所有档都写 ``brief/{D+1}.md``。这样「第二天别的
agent 看得到」是文件名自带的，不需要消费方再做日期偏移。

**对话流**与 ``live_model_analysis`` 同结构（``new_messages``），前端直接可读；
``kind="research_control"`` 不写 ``review``——``review`` 会被前端当成「盘后复盘」
折叠摘要，而我们要展示完整报告。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

CN_TZ = timezone(timedelta(hours=8))

#: 报告固定七段（提示词要求，store 不强制；仅用于 brief 段落标题与摘要提取）
SECTION_TITLES: dict[str, str] = {
    "midday": "午间复盘",
    "close": "收盘复盘",
    "inference": "推理数据复盘",
    "news": "新闻复盘",
    "night": "夜间综合总结",
}

#: brief 里单档正文上限（消费方是决策提示词，不能把盘口/新闻块挤掉）
SECTION_CONTENT_LIMIT = 2400
#: brief 最终结论上限
FINAL_CONTENT_LIMIT = 4000
#: 各档报告里「结论段」的标题关键字（行文各档不同，命中任一即可）
CONCLUSION_NAMES = ("明日方向", "明日结论", "明日执行计划")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _data_base() -> Path:
    """数据根：容器用挂载 ``/data``，宿主用仓库 ``data/``（符号链接到真实盘）。

    **不能**按 ``repo_root()/data`` 是否存在来判容器：镜像里自带一个陈旧的
    ``/app/data``（只有 ``backtest_2026``），按它解析会让容器内的 API / 交易进程
    读到与 cron（写 ``/data``）完全不同的目录——面板永远「五档未运行」、
    决策轮永远读不到 brief，且全链无报错。
    """
    quantdb_dir = os.getenv("QM_QUANTDB_DATA_DIR")
    if quantdb_dir:
        return Path(quantdb_dir).parent
    if Path("/.dockerenv").exists() and Path("/data").is_dir():
        return Path("/data")
    repo_data = repo_root() / "data"
    return repo_data if repo_data.is_dir() else Path("/data")


def data_base() -> Path:
    """``data/`` 根（quantdb / news_brief / reports 的共同父目录）。"""
    return _data_base()


def data_root() -> Path:
    env = os.getenv("QM_RESEARCH_CONTROL_DIR")
    if env:
        return Path(env)
    return _data_base() / "research_control"


def agent_data_root() -> Path:
    env = os.getenv("QM_AGENT_DATA_DIR")
    if env:
        return Path(env)
    return _data_base() / "agent_data_astock"


def reports_dir(day: str) -> Path:
    return data_root() / "reports" / day


def report_path(day: str, slot: str) -> Path:
    return reports_dir(day) / f"{slot}.md"


def report_meta_path(day: str, slot: str) -> Path:
    return reports_dir(day) / f"{slot}.json"


def brief_path(target: str) -> Path:
    return data_root() / "brief" / f"{target}.md"


def brief_meta_path(target: str) -> Path:
    return data_root() / "brief" / f"{target}.json"


def state_path(day: str) -> Path:
    return data_root() / "state" / f"{day}.json"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, data: dict) -> None:
    _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2, default=str))


# ── 报告 ─────────────────────────────────────────────────────────────


def write_report(
    day: str,
    slot: str,
    content: str,
    *,
    target: str,
    meta: dict | None = None,
) -> Path:
    """写 ``reports/{day}/{slot}.md|json``，并更新当日 state。"""
    path = report_path(day, slot)
    _atomic_write(path, content)
    payload = {
        "day": day,
        "slot": slot,
        "target": target,
        "title": SECTION_TITLES.get(slot, slot),
        "ts": datetime.now(CN_TZ).isoformat(),
        **(meta or {}),
        "content": content,
    }
    _write_json(report_meta_path(day, slot), payload)
    update_state(
        day,
        slot,
        {"ok": True, "target": target, "bytes": len(content), "ts": payload["ts"]},
    )
    return path


def read_report(day: str, slot: str) -> str:
    try:
        return report_path(day, slot).read_text(encoding="utf-8")
    except OSError:
        return ""


def read_report_meta(day: str, slot: str) -> dict:
    return _read_json(report_meta_path(day, slot))


def list_reports(day: str) -> list[dict]:
    out: list[dict] = []
    for slot in SECTION_TITLES:
        meta = read_report_meta(day, slot)
        if meta:
            out.append(
                {
                    "slot": slot,
                    "title": SECTION_TITLES.get(slot, slot),
                    "ts": meta.get("ts", ""),
                    "target": meta.get("target", ""),
                    "bytes": len(str(meta.get("content") or "")),
                    "error": meta.get("error", ""),
                }
            )
    return out


# ── 运行状态 ─────────────────────────────────────────────────────────


def update_state(day: str, slot: str, info: dict) -> None:
    path = state_path(day)
    state = _read_json(path)
    slots = state.setdefault("slots", {})
    if isinstance(slots, dict):
        slots[slot] = {**info, "updated_at": datetime.now(CN_TZ).isoformat()}
    state["day"] = day
    _write_json(path, state)


def read_state(day: str) -> dict:
    return _read_json(state_path(day))


# ── 摘要 / 七段提取 ─────────────────────────────────────────────────


_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+")
_FENCE_RE = re.compile(r"```.*?```", re.S)


def summary_of(content: str, limit: int = 160) -> str:
    """从报告里抽一句摘要：跳过标题、围栏、分隔线、空行，取第一段正文。"""
    text = _FENCE_RE.sub("", content or "")
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ">", "---", "===", "|")):
            continue
        line = re.sub(r"^[-*]\s+", "", line)
        line = re.sub(r"[*_`#]", "", line).strip()
        if line:
            return line[:limit]
    return ""


def extract_section(content: str, names: tuple[str, ...]) -> str:
    """抽某个二级标题下的正文（到下一个同级/更高级标题为止）；找不到返回空串。"""
    lines = (content or "").splitlines()
    start = -1
    level = 0
    for i, line in enumerate(lines):
        m = re.match(r"^\s{0,3}(#{2,4})\s+(.*)$", line)
        if not m:
            continue
        title = m.group(2)
        if any(name in title for name in names):
            start = i + 1
            level = len(m.group(1))
            break
    if start < 0:
        return ""
    out: list[str] = []
    for line in lines[start:]:
        m = re.match(r"^\s{0,3}(#{1,6})\s+", line)
        if m and len(m.group(1)) <= level:
            break
        out.append(line)
    return "\n".join(out).strip()


def _excerpt(content: str, limit: int) -> str:
    """按行边界截断并标注「全文在报告文件」，不把半句话/空标题留在 brief 里。"""
    text = (content or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    nl = cut.rfind("\n")
    if nl > limit // 2:
        cut = cut[:nl]
    lines = cut.splitlines()
    while lines and (
        not lines[-1].strip() or re.match(r"^\s{0,3}#{1,6}\s+", lines[-1])
    ):
        lines.pop()
    return "\n".join(lines).rstrip() + "\n\n…（全文见当日报告文件）"


def conclusion_of(content: str, limit: int = 1200) -> str:
    """取报告结论段（明日方向/结论）；没有才退回首段摘要。

    摘要取「第一段正文」会把「当前结论」抬成市场综述——新闻档的第一段是利率/汇率，
    真正的次日方向在第七节。结论段优先，才配得上 brief 顶部那行「当前结论」。
    """
    section = extract_section(content, CONCLUSION_NAMES)
    if section:
        return _excerpt(section, limit)
    return summary_of(content, limit=min(limit, 240))


# ── brief ────────────────────────────────────────────────────────────


def upsert_brief(
    target: str,
    day: str,
    slot: str,
    content: str,
    *,
    machine: dict | None = None,
) -> Path:
    """把一档结论并入 ``brief/{target}.json`` 并重渲染 md。

    - ``night`` 档 = 最终稿（``status=final``）；其余为阶段稿（``status=interim``）。
    - 同一 target 重跑同一档 = 覆盖该档，不重复堆叠。
    """
    meta = _read_json(brief_meta_path(target))
    meta.setdefault("target", target)
    meta.setdefault("day", day)
    sections = meta.get("sections")
    if not isinstance(sections, dict):
        sections = {}
    sections[slot] = {
        "slot": slot,
        "title": SECTION_TITLES.get(slot, slot),
        "ts": datetime.now(CN_TZ).isoformat(),
        "summary": conclusion_of(content),
        "content": _excerpt(content, SECTION_CONTENT_LIMIT),
        "machine": machine or {},
    }
    meta["sections"] = sections
    meta["status"] = "final" if slot == "night" else "interim"
    meta["updated_at"] = datetime.now(CN_TZ).isoformat()
    if slot == "night":
        meta["final"] = {
            "ts": datetime.now(CN_TZ).isoformat(),
            "content": _excerpt(content, FINAL_CONTENT_LIMIT),
            "machine": machine or {},
        }
    _write_json(brief_meta_path(target), meta)
    _atomic_write(brief_path(target), render_brief(target, meta))
    return brief_path(target)


def render_brief(target: str, meta: dict | None = None) -> str:
    meta = meta if meta is not None else _read_json(brief_meta_path(target))
    if not meta:
        return ""
    day = str(meta.get("day") or "")
    status = str(meta.get("status") or "interim")
    sections = meta.get("sections") or {}
    lines = [f"# 研究总控 · {_dash(target)} 交易日前瞻", ""]
    lines.append(
        f"> 复盘日 {day} ｜ 状态：{'最终稿' if status == 'final' else '阶段稿（夜间综合尚未生成）'}"
        f" ｜ 更新 {meta.get('updated_at', '')}"
    )
    lines.append("")
    final = meta.get("final") or {}
    if status == "final" and final.get("content"):
        lines += ["## 明日结论（综合）", "", str(final["content"]), ""]
    else:
        # 阶段稿：把最近一档的摘要抬到最上面，消费方先看结论再看细节。
        latest = _latest_section(sections)
        if latest:
            lines += [
                f"## 当前结论（{latest.get('title')}）",
                "",
                str(latest.get("summary") or ""),
                "",
            ]
    if sections:
        lines += ["## 各档复盘", ""]
        for slot in ("midday", "close", "inference", "news"):
            sec = sections.get(slot)
            if not sec:
                continue
            lines += [
                f"### {sec.get('title', slot)}",
                "",
                str(sec.get("content") or ""),
                "",
            ]
        if status != "final" and sections.get("night"):
            sec = sections["night"]
            lines += [
                f"### {sec.get('title', 'night')}",
                "",
                str(sec.get("content") or ""),
                "",
            ]
    return "\n".join(lines).rstrip() + "\n"


def _latest_section(sections: dict) -> dict:
    items = [v for v in sections.values() if isinstance(v, dict)]
    items.sort(key=lambda x: str(x.get("ts") or ""), reverse=True)
    return items[0] if items else {}


def read_brief(target: str) -> str:
    try:
        return brief_path(target).read_text(encoding="utf-8")
    except OSError:
        return ""


def latest_brief(on_or_before: str, *, lookback_days: int = 7) -> tuple[str, str]:
    """找 ``target <= on_or_before`` 的最近一份 brief → ``(target, md)``。

    周末/节假日没有当晚的 night 档时，消费方拿上一交易日的最终稿，而不是空。
    """
    root = data_root() / "brief"
    try:
        files = sorted(root.glob("*.md"), reverse=True)
    except OSError:
        return "", ""
    try:
        cutoff = datetime.strptime(on_or_before.replace("-", ""), "%Y%m%d").date()
    except ValueError:
        cutoff = date.today()
    floor = cutoff - timedelta(days=max(1, lookback_days))
    for path in files:
        key = path.stem
        try:
            d = datetime.strptime(key, "%Y%m%d").date()
        except ValueError:
            continue
        if floor <= d <= cutoff:
            try:
                return key, path.read_text(encoding="utf-8")
            except OSError:
                continue
    return "", ""


# ── 对话流 ───────────────────────────────────────────────────────────


def append_dialogue(
    day: str,
    slot: str,
    user: str,
    content: str,
    *,
    usage: dict | None = None,
    gaps: list[str] | None = None,
) -> Path:
    """追加 ``market-research`` 对话流（前端「模型对话」直接显示完整报告）。"""
    log_dir = agent_data_root() / "market-research" / "log" / day
    log_dir.mkdir(parents=True, exist_ok=True)
    entry: dict[str, Any] = {
        "timestamp": datetime.now(CN_TZ).isoformat(),
        "signature": "market-research",
        "kind": "research_control",
        "slot": slot,
        "new_messages": [
            {"role": "user", "content": user},
            {"role": "assistant", "content": content},
        ],
    }
    if usage:
        entry["usage"] = usage
    if gaps:
        entry["data_gaps"] = list(gaps)
    path = log_dir / "log.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return path


def _dash(day: str) -> str:
    s = str(day or "").replace("-", "")
    return f"{s[:4]}-{s[4:6]}-{s[6:]}" if len(s) == 8 else str(day)
