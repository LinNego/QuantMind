"""研究总控（market-research）后端包。

- :mod:`config`  独立 LLM 配置（runtime.env 单一事实源，key 只写不回）
- :mod:`dossier` 多面事实采集（系统/交易/对话/新闻/模型/经验）
- :mod:`prompts` 五档复盘提示词与输出契约
- :mod:`store`   报告 / 次日 brief / 对话流落盘
- :mod:`runner`  一档复盘的编排（取事实 → 调模型 → 落盘 → 更新 brief）
"""

from __future__ import annotations

from .config import (
    DEFAULT_MODEL,
    ResearchLLMConfig,
    ResearchLLMNotConfigured,
    describe,
    resolve_config,
    save_config,
    test_connection,
)
from .runner import SLOTS, run_slot

__all__ = [
    "DEFAULT_MODEL",
    "ResearchLLMConfig",
    "ResearchLLMNotConfigured",
    "SLOTS",
    "describe",
    "resolve_config",
    "run_slot",
    "save_config",
    "test_connection",
]
