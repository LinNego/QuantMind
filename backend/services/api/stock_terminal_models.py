"""个股终端模型下拉的选项合并（纯函数，便于单测）。

背景（2026-10-20 用户复报「候选信号选模型显示不全」）：
`/stock-terminal/list` 原先只把「近 90 天有信号」的模型放进下拉，注册表里
尚未跑推理的新模型全部不可见。用户刚训练完模型来选，看到的就是「界面漏了」。

修复口径：
- **注册表（非归档）全量可选**；信号事实只用于分组与「近 30 天无信号」标注。
- 有信号的模型保持既有排序（默认模型 / 最新信号优先），注册表新增项排在其后
  （SQL 已按 updated_at DESC）。
- 注册表新增项按页面市场收口（CN 收 CN + CUSTOM，HK 收 HK + CUSTOM）；
  已有信号的外市场模型沿用旧行为，不因收口而消失。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime
from typing import Any

_UNIVERSAL_MARKETS = frozenset({"CUSTOM"})


def _as_date(value: Any) -> date | None:
    """把 asyncpg 可能返回的 datetime 归一化为 date。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def _display_name(row: Mapping[str, Any]) -> str:
    return str(row.get("display_name") or "").strip()


def merge_model_options(
    signal_rows: Iterable[Mapping[str, Any]],
    registry_rows: Iterable[Mapping[str, Any]],
    *,
    recent_cut: date | datetime | None,
    market_scope: str,
) -> list[dict[str, Any]]:
    """合并「有信号」与「注册表」两个来源，返回下拉选项。

    Args:
        signal_rows: 近 90 天有信号的模型，按后端既定顺序传入
            （`{model_id, latest}`）。
        registry_rows: 非归档注册表，按 updated_at DESC 传入
            （`{model_id, display_name, market}`）。
        recent_cut: 近 30 天判定线；无信号日时传 None。
        market_scope: 当前页面市场（CN/HK），决定注册表新增项是否可见。

    Returns:
        选项列表，字段：`model_id` / `display_name` /
        `has_signals_30d` / `has_signals_90d`。
    """
    scope = (market_scope or "CN").upper()
    cutoff = _as_date(recent_cut)

    registry: dict[str, Mapping[str, Any]] = {}
    for row in registry_rows:
        model_id = str(row.get("model_id") or "")
        if model_id and model_id not in registry:
            registry[model_id] = row

    options: list[dict[str, Any]] = []
    seen: set[str] = set()

    # 1) 有信号的模型：保持输入顺序（默认模型 / 最新信号优先）
    for row in signal_rows:
        model_id = str(row.get("model_id") or "")
        if not model_id or model_id in seen:
            continue
        latest = _as_date(row.get("latest"))
        options.append(
            {
                "model_id": model_id,
                "display_name": _display_name(registry.get(model_id, {})),
                "has_signals_30d": bool(
                    cutoff is not None and latest is not None and latest >= cutoff
                ),
                "has_signals_90d": True,
            }
        )
        seen.add(model_id)

    # 2) 注册表新增项：全部可选，排在有信号模型之后
    for model_id, row in registry.items():
        if model_id in seen:
            continue
        market = str(row.get("market") or scope).upper()
        if market not in {scope, *_UNIVERSAL_MARKETS}:
            continue
        options.append(
            {
                "model_id": model_id,
                "display_name": _display_name(row),
                "has_signals_30d": False,
                "has_signals_90d": False,
            }
        )
        seen.add(model_id)

    return options
