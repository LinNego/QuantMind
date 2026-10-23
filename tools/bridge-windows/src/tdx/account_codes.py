"""实盘账户代码 → 订阅列表（自动跟随）。

只做「读账户 + 代码归一」，不做订阅决策；订阅由 SubscriptionManager 负责。
账户查询是阻塞 DLL 调用，调用方必须放在线程/交易槽里执行。
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def normalize_symbol(raw: str) -> str:
    """裸码/前缀式/后缀式 → 后缀式（订阅通道统一用后缀）。"""
    code = str(raw or "").strip().upper()
    if not code:
        return ""
    if "." in code:
        num, _, suffix = code.partition(".")
        return f"{num}.{suffix}" if num and suffix else code
    for prefix in ("SH", "SZ", "BJ"):
        if code.startswith(prefix) and code[2:].isdigit():
            return f"{code[2:]}.{prefix}"
    if code.isdigit() and len(code) == 6:
        if code.startswith("88"):  # 板块码属沪市
            return f"{code}.SH"
        head = code[0]
        if head in "69":
            return f"{code}.SH"
        if head in "48":
            return f"{code}.BJ"
        return f"{code}.SZ"
    return code


def fetch_account_codes(tdx, executor, source: str = "positions",
                        include_orders: bool = False) -> list[str]:
    """读实盘持仓/当日委托，返回去重后的后缀式代码（账户原顺序）。"""
    account_id = executor.resolve_account_id("", "stock")
    out: list[str] = []
    seen: set[str] = set()

    def add(row) -> None:
        if not isinstance(row, dict):
            return
        code = normalize_symbol(row.get("Code") or row.get("stock_code") or "")
        if code and code not in seen:
            seen.add(code)
            out.append(code)

    if source != "orders":
        for row in tdx.query_stock_positions(account_id) or []:
            add(row)
        if include_orders:
            for row in tdx.query_stock_orders(account_id, "", False) or []:
                add(row)
    else:
        for row in tdx.query_stock_orders(account_id, "", False) or []:
            add(row)
    return out
