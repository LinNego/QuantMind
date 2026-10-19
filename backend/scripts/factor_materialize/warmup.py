"""公式复算所需的**复利**预热窗（嵌套 rolling 相加，不是取最大值）。

``formulas.json`` 里的 ``window`` 字段只是单条 rolling 的最大数值窗口；嵌套时
外层 rolling 要在内层「已就绪」的值上再滚 N 天，所需历史是**相加**的：

    risk_fac_momentum = 0.5 * (R(TSRANK(c / REF(c, 504) - 1, 504)) + ...)
    → 504 (REF) + 504 (TSRANK) = 1008 个交易日

2026-10-20 真实回填按清单 ``window`` 定预热（factor_defs 900、factor1300 400），
结果长窗因子在新日期上整列为空：``risk_fac_momentum`` 需 1008，
``f13_0645`` 需 1220，``f13_0261`` 需 960，``f13_0422/0390/0423`` 需 480。
本模块把「公式 → 所需交易日」算成静态量，供编排器把长窗因子拆到第二遍
（``--only``）用更长的回看窗复算。

只做 AST 分析：不求值、不读行情。窗口参数位置按
``backend/shared/factor_dsl.Evaluator.ops`` 的实际签名登记；新增算子若带窗口，
必须同步这里，否则会被低估成 0。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping

from backend.shared.factor_dsl import parse

LOG = logging.getLogger("factor_materialize.warmup")

#: 滚动窗口在实参中的下标（与 ``alpha_library_factors`` 的函数签名一致）
WINDOW_ARG_INDEX: dict[str, int] = {
    "MEAN": 1,
    "SUM": 1,
    "STD": 1,
    "PROD": 1,
    "MAX": 1,
    "MIN": 1,
    "TSRANK": 1,
    "TSARGMAX": 1,
    "TSARGMIN": 1,
    "HIGHDAY": 1,
    "LOWDAY": 1,
    "DECAY": 1,
    "QTL": 1,  # QTL(df, w, q)
    "REG": 1,
    "EMA_SPAN": 1,  # 递归平滑，记忆长度 ~span
    "REALIZED_VOL": 1,
    "CORR": 2,  # CORR(x, y, w)
    "COV": 2,  # COV(x, y, w)
}

#: 滞后在实参中的下标：REF/DELTA(x, n) 直接消费 n 天历史
LAG_ARG_INDEX: dict[str, int] = {
    "REF": 1,
    "DELTA": 1,
}

#: 非窗口算子（登记只为说明为何不参与累加）：
#: ``EWMA(x, alpha)`` 的第二个参数是衰减系数不是天数；``R``/``Z``/``IN``/
#: ``WINSOR_Z``/``SCALE`` 是当日截面算子；``INTRADAY_*``/``SEG`` 把分钟折叠成
#: 日频，不消费日频历史；``RANK_TS`` 是整窗静态排名（无固定回看需求）。


def _literal_number(node: object) -> float | None:
    if isinstance(node, tuple) and len(node) == 2 and node[0] == "num":
        return float(node[1])
    return None


def _history_days(node: object) -> int:
    """AST 节点需要的最少历史交易日（所有分支取最大值，嵌套窗口相加）。"""
    if not isinstance(node, tuple) or not node:
        return 0
    kind = node[0]
    if kind in ("num", "str", "name"):
        return 0
    if kind in ("neg", "not", "attr"):
        return _history_days(node[1])
    if kind in ("bin", "cmp"):
        return max(_history_days(node[2]), _history_days(node[3]))
    if kind in ("and", "or"):
        return max(_history_days(node[1]), _history_days(node[2]))
    if kind != "call":
        return 0
    name = node[1]
    args = node[2]
    base = max((_history_days(arg) for arg in args), default=0)
    for table in (WINDOW_ARG_INDEX, LAG_ARG_INDEX):
        index = table.get(name)
        if index is None or index >= len(args):
            continue
        amount = _literal_number(args[index])
        if amount is None:
            # 窗口不是字面量（表达式或变量）：无法静态判定，按 0 计。
            LOG.debug("算子 %s 的窗口参数不是字面量，按 0 计", name)
            return base
        return base + max(int(amount), 0)
    return base


def required_days(formula: str) -> int:
    """一条公式从起算日到首个有效值所需的交易日数。"""
    return _history_days(parse(formula))


def windows_for(
    records: Iterable[Mapping[str, object]],
    *,
    name_key: str = "name",
    formula_key: str = "formula",
) -> dict[str, int]:
    """``{因子名: 所需交易日}``；解析失败的条目跳过并告警（生产者会另行报错）。"""
    result: dict[str, int] = {}
    for record in records:
        name = str(record.get(name_key) or "")
        formula = str(record.get(formula_key) or "")
        if not name or not formula:
            continue
        try:
            result[name] = required_days(formula)
        except Exception as exc:  # 解析器抛 ParseError/TokenError 等
            LOG.warning("公式解析失败，不参与预热分析：%s（%s）", name, exc)
    return result


def select_long(windows: Mapping[str, int], warmup: int) -> list[str]:
    """所需历史超过 ``warmup`` 的因子名（需要第二遍长窗复算）。"""
    return sorted(name for name, days in windows.items() if days > warmup)


def longest_requirement(windows: Mapping[str, int], names: Iterable[str]) -> int:
    wanted = set(names)
    return max((days for name, days in windows.items() if name in wanted), default=0)
