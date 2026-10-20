"""QQ 卡片共用的文本压行工具（纯函数，无外部依赖）。

QQ 是纯文本消息、手机一屏只有十几行：方向叙述、拒绝理由这类「一句话很长」的
字段必须先**折成一句**再上卡，否则一条消息要滚三屏。本模块收口两处口径：

* :func:`short_clause` —— 在第一个够长的顶层分句处截断（括号内的顿号是列举，
  在那里切会砍出「大盘偏弱（上证5日-1.7%」这种残句）；
* :func:`first_line` —— 多行原因只取首行（台账里换行是给人看的，卡片不是）。

2026-10-20 从 ``decision_round_alerts._short_direction`` / ``duty_summary._oneline``
提取（两处各写一份 = 两条卡片对同一个方向给出两种长度）。
"""

from __future__ import annotations

#: 方向原文是一整段策略叙述，摘要面只留首句判断（一屏内要看到计数与要点）。
DEFAULT_DIRECTION_MAX_CHARS = 40
#: 截断点至少要有这么长的首句，否则硬切到上限（避免砍成「弱」这种残句）。
DEFAULT_MIN_CLAUSE_CHARS = 12


def short_clause(
    text: str,
    max_chars: int = DEFAULT_DIRECTION_MAX_CHARS,
    min_clause_chars: int = DEFAULT_MIN_CLAUSE_CHARS,
) -> str:
    """压成一句：折叠空白；超限时在**第一个够长的顶层分句**处截断加 ``…``。

    截断点只认括号外的 ``，；。;``——括号内的顿号是列举
    （如 ``大盘偏弱（上证5日-1.7%、平量、低位）``），在那里切会砍出残句。
    """
    flat = " ".join(str(text or "").split())
    if len(flat) <= max_chars:
        return flat
    head = flat[:max_chars]
    cut = -1
    depth = 0
    for idx, ch in enumerate(head):
        if ch == "（":
            depth += 1
        elif ch == "）":
            depth = max(0, depth - 1)
        elif depth == 0 and ch in "，；。;":
            if idx + 1 >= min_clause_chars:
                cut = idx
                break
    if cut >= 0:
        head = head[:cut]
    return head.rstrip("，；。、,; ") + "…"


def first_line(text: object, width: int = 50) -> str:
    """多行原因只取第一条非空行；超 ``width`` 字截断加 ``…``。"""
    raw = str(text or "").strip()
    if not raw:
        return ""
    first = raw.splitlines()[0].strip()
    return first if len(first) <= width else first[: width - 1] + "…"


__all__ = [
    "DEFAULT_DIRECTION_MAX_CHARS",
    "DEFAULT_MIN_CLAUSE_CHARS",
    "first_line",
    "short_clause",
]
