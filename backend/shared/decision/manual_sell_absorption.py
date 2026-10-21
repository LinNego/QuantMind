"""未归属真单卖出的分账吸收计划（纯核心，不碰 DB）。

背景：``agent_ledger_fill`` 纪律 1——``orders.agent`` 为空的成交（人工在桥里点的
单、风控单）**不写分账**。人工清仓后账本留下幻影仓：读侧（复盘/对话提示词）有
真实账户闸门兜底，但决策轮「已用额度」、实况页 agent 净值、arena 台账仍按账本算。
``backend/scripts/repair_agent_ledger_manual_sells.py`` 用本模块判定并把结果写回
账本（走 ``apply_fill`` 同一实现，现金与回合行口径与真单一致）。

吸收判据（**全部满足**才吸收；任何一条不满足只报告不写）：

1. 该标的已不在真实账户（真实持仓由调用方读好传入，与决策轮同一口径）；
2. 未归属卖出总量足以覆盖账本该标的**合计持仓**（不足 = 账本本来就没记那么多，
   **不猜**）；
3. 账本每一行的 ``buy_ts`` 早于分配到的卖出成交（卖出之后才买的仓不由这笔解释；
   ``buy_ts`` 缺失同样不动——无法证明它当时存在）。

分配：按 ``buy_ts`` FIFO 逐行吃卖出成交（一笔单可跨多家 agent，一家也可跨多笔单），
每一段一条流水。幂等键 ``<prefix>:<order_id>:<agent>``——账本 fill 唯一键是
``(tenant, user, trade_date, fill_key)``，**不含 agent**，故键里必须带 agent：两家
同持一只票时第二家的流水才不会被幂等守卫吞掉。

**为什么单独成模块**：判定与分配是纯函数（无 DB、无时钟、无环境变量），修复脚本
只负责读数、写库与复验。后续做「每日幻影仓体检」时复用同一份判据，不另写一套。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from typing import Any

from backend.shared.stock_utils import StockCodeUtil

#: 幂等键前缀（体检/审计按它把这类流水与券商成交号区分开）。
FILL_KEY_PREFIX = "manual-absorb"

#: 卖出理由（只记录，进回合台账；与真单的 NULL 区分，行为归因可单独过滤）。
EXIT_REASON = "manual_absorb"

#: 浮点比较容差（股数/金额都是两位小数以内的量）。
EPS = 1e-6

# ── 跳过原因（报告与测试共用一份常量）─────────────────────────────────
REASON_REAL_UNAVAILABLE = "real_positions_unavailable"
REASON_STILL_HELD = "real_account_still_holds"
REASON_NO_SELL_EVIDENCE = "no_unattributed_sells"
REASON_INSUFFICIENT = "unattributed_sells_insufficient"
REASON_BOUGHT_AFTER_SELL = "position_bought_after_sell"
REASON_BUY_TS_UNKNOWN = "position_buy_ts_unknown"

REASON_TEXT = {
    REASON_REAL_UNAVAILABLE: "真实持仓不可读",
    REASON_STILL_HELD: "真实账户仍持有",
    REASON_NO_SELL_EVIDENCE: "窗口内无未归属卖出",
    REASON_INSUFFICIENT: "未归属卖出不足以覆盖账本持仓",
    REASON_BOUGHT_AFTER_SELL: "卖出之后才买入",
    REASON_BUY_TS_UNKNOWN: "买入时间未知",
}


def to_float(value: Any) -> float:
    """宽容转 float（``None``/坏值 → 0），只用于读数与比对。"""
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def to_suffix_code(value: Any) -> str:
    """任意写法 → ``600760.SH``（账本持仓列口径）。"""
    return StockCodeUtil.to_suffix(str(value or "").strip())


def as_aware_utc(value: datetime | None) -> datetime | None:
    """统一成 aware UTC；naive 按 UTC 解释（调用方负责把北京墙钟先转好）。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class AbsorbFill:
    """一段吸收流水：某 agent 的某只持仓被一笔未归属卖出覆盖的数量。"""

    agent: str
    code: str
    volume: float
    price: float
    order_id: str
    fill_key: str
    trade_date: date
    filled_at: datetime
    cost_price: float
    realized_pnl: float
    closes: bool  # True = 这段把这行持仓清零（回合行 closed 口径）

    @property
    def proceeds(self) -> float:
        return round(self.volume * self.price, 2)


@dataclass(frozen=True, slots=True)
class AbsorbSkip:
    code: str
    reason: str
    detail: str


@dataclass(frozen=True, slots=True)
class AbsorbPlan:
    fills: tuple[AbsorbFill, ...] = ()
    skipped: tuple[AbsorbSkip, ...] = ()
    #: 每家模型的回款增量（按流水顺序逐笔 round，与 ``record_sell`` 写库一致）
    proceeds_by_agent: Mapping[str, float] = field(default_factory=dict)
    #: 每家模型的已实现盈亏（同上，逐笔 round）
    pnl_by_agent: Mapping[str, float] = field(default_factory=dict)


def plan_absorption(
    ledger_rows: Sequence[Mapping[str, Any]],
    sells: Sequence[Mapping[str, Any]],
    real_volumes: Mapping[str, float] | None,
    *,
    fill_key_prefix: str = FILL_KEY_PREFIX,
) -> AbsorbPlan:
    """账本持仓 × 未归属卖出 → 吸收计划（纯函数，判据见模块 docstring）。

    :param ledger_rows: ``{agent, code, volume, cost_price, buy_ts}``；``buy_ts`` 为
        aware UTC（``None`` = 不知道，整只票不动）。
    :param sells: ``{order_id, code, volume, price, executed_at, trade_date, agent?}``；
        ``executed_at`` 为 aware UTC；``agent`` 非空的行会被忽略（那是真单落账路径的活）。
    :param real_volumes: ``{suffix_code: volume}``；``None`` = 真实持仓不可读 → 整批不动。
    """
    ledger_by_code: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in ledger_rows:
        code = to_suffix_code(row.get("code"))
        volume = to_float(row.get("volume"))
        if not code or volume <= 0:
            continue
        ledger_by_code[code].append(
            {
                "agent": str(row.get("agent") or ""),
                "code": code,
                "volume": volume,
                "cost_price": to_float(row.get("cost_price")),
                "buy_ts": as_aware_utc(row.get("buy_ts")),
            }
        )

    sells_by_code: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for sell in sells:
        if str(sell.get("agent") or "").strip():
            continue  # 归属卖单不归吸收器管（真单落账路径已经写过或本就不该写）
        code = to_suffix_code(sell.get("code"))
        volume = to_float(sell.get("volume"))
        price = to_float(sell.get("price"))
        executed_at = as_aware_utc(sell.get("executed_at"))
        if not code or volume <= 0 or price <= 0 or executed_at is None:
            continue
        trade_date = sell.get("trade_date")
        if not isinstance(trade_date, date):
            trade_date = executed_at.date()
        sells_by_code[code].append(
            {
                "order_id": str(sell.get("order_id") or ""),
                "code": code,
                "volume": volume,
                "price": price,
                "executed_at": executed_at,
                "trade_date": trade_date,
            }
        )

    skipped: list[AbsorbSkip] = []
    fills: list[AbsorbFill] = []

    for code in sorted(ledger_by_code):
        if real_volumes is None:
            skipped.append(AbsorbSkip(code, REASON_REAL_UNAVAILABLE, ""))
            continue
        real = to_float(real_volumes.get(code))
        if real > 0:
            skipped.append(AbsorbSkip(code, REASON_STILL_HELD, f"{real:g} 股"))
            continue

        rows = ledger_by_code[code]
        if any(row["buy_ts"] is None for row in rows):
            skipped.append(
                AbsorbSkip(
                    code, REASON_BUY_TS_UNKNOWN, "账本行缺 buy_ts，无法证明卖出时存在"
                )
            )
            continue

        code_sells = sorted(
            sells_by_code.get(code, []),
            key=lambda s: (s["executed_at"], s["order_id"]),
        )
        ledger_total = round(sum(row["volume"] for row in rows), 4)
        sold_total = round(sum(s["volume"] for s in code_sells), 4)
        if not code_sells:
            skipped.append(AbsorbSkip(code, REASON_NO_SELL_EVIDENCE, ""))
            continue
        latest_sell = code_sells[-1]["executed_at"]
        late_rows = [row for row in rows if row["buy_ts"] > latest_sell]
        if late_rows:
            skipped.append(
                AbsorbSkip(
                    code,
                    REASON_BOUGHT_AFTER_SELL,
                    f"{late_rows[0]['agent']} 的买入晚于最后一笔未归属卖出",
                )
            )
            continue

        rows_sorted = sorted(rows, key=lambda r: (r["buy_ts"], r["agent"]))
        remaining = [s["volume"] for s in code_sells]
        code_fills: list[AbsorbFill] = []
        starved: dict[str, Any] | None = None
        starved_need = 0.0

        for row in rows_sorted:
            need = row["volume"]
            row_fills: list[AbsorbFill] = []
            for idx, sell in enumerate(code_sells):
                if remaining[idx] <= EPS:
                    continue
                if sell["executed_at"] < row["buy_ts"]:
                    continue  # 这笔卖出发生在建仓之前，解释不了这行持仓
                take = min(need, remaining[idx])
                if take <= EPS:
                    continue
                realized = round((sell["price"] - row["cost_price"]) * take, 2)
                row_fills.append(
                    AbsorbFill(
                        agent=row["agent"],
                        code=code,
                        volume=take,
                        price=sell["price"],
                        order_id=sell["order_id"],
                        fill_key=f"{fill_key_prefix}:{sell['order_id']}:{row['agent']}",
                        trade_date=sell["trade_date"],
                        filled_at=sell["executed_at"],
                        cost_price=row["cost_price"],
                        realized_pnl=realized,
                        closes=False,
                    )
                )
                remaining[idx] = round(remaining[idx] - take, 6)
                need = round(need - take, 6)
                if need <= EPS:
                    break
            if need > EPS:
                starved = row
                starved_need = need
                break
            # 这一行完整覆盖：最后一段即清仓段
            row_fills[-1] = replace(row_fills[-1], closes=True)
            code_fills.extend(row_fills)

        if starved is not None:
            detail = (
                f"未归属卖出 {sold_total:g} 股 < 账本持仓 {ledger_total:g} 股"
                if sold_total + EPS < ledger_total
                else f"{starved['agent']} 缺 {starved_need:g} 股可分配卖出"
            )
            skipped.append(AbsorbSkip(code, REASON_INSUFFICIENT, detail))
            continue
        fills.extend(code_fills)

    proceeds: dict[str, float] = defaultdict(float)
    pnl: dict[str, float] = defaultdict(float)
    for fill in fills:
        proceeds[fill.agent] = round(proceeds[fill.agent] + fill.proceeds, 2)
        pnl[fill.agent] = round(pnl[fill.agent] + fill.realized_pnl, 2)

    return AbsorbPlan(
        fills=tuple(fills),
        skipped=tuple(skipped),
        proceeds_by_agent=dict(proceeds),
        pnl_by_agent=dict(pnl),
    )


def fill_fields(fill: AbsorbFill) -> dict[str, Any]:
    """``AbsorbFill`` → 构造参数（dataclass 是 frozen 的，改字段只能重建）。"""
    return {
        "agent": fill.agent,
        "code": fill.code,
        "volume": fill.volume,
        "price": fill.price,
        "order_id": fill.order_id,
        "fill_key": fill.fill_key,
        "trade_date": fill.trade_date,
        "filled_at": fill.filled_at,
        "cost_price": fill.cost_price,
        "realized_pnl": fill.realized_pnl,
        "closes": fill.closes,
    }


def expected_cash(before: float, fills: Sequence[AbsorbFill]) -> float:
    """账本现金的期望值：与 ``record_sell`` 的 ``round(cash + sold*price, 2)`` 逐步对齐。"""
    cash = round(to_float(before), 2)
    for fill in fills:
        cash = round(cash + fill.volume * fill.price, 2)
    return cash
