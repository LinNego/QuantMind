"""机构级期货因子库（纯计算，不含 IO / API / 缓存）。

设计口径见 ``README.md``：一行 = 一个品种 × 一个交易日，所有因子只使用
当日收盘及以前可得的信息；未来收益标签不进入因子表。
"""

from __future__ import annotations

from backend.shared.futures_factors.registry import (
    FEATURE_COLS,
    FEATURE_FAMILIES,
    FEATURE_VERSION,
    META_COLS,
    feature_hash,
)

__all__ = [
    "FEATURE_COLS",
    "FEATURE_FAMILIES",
    "FEATURE_VERSION",
    "META_COLS",
    "feature_hash",
]
