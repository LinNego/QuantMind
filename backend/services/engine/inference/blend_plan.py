"""纸面调仓计划：对组合榜做「继续持有 / 到期退出 / 新进」的确定性裁定。

口径与 P1 研究脚本 ``combo_eval.simulate_rebalance`` 的 10 日闸门一致，但这里是
**单日计划**（不是逐日回放）：

- 持有天数按交易日计（``trading_days`` 中 entry_date 之后、含 T 的日数）；
- 分数退出线 = ``mean20(S) − exit_delta``，``mean20`` 只取 T 之前的
  ``lookback`` 个交易日，有效日不足 ``min_lookback_days`` 视为「数据不足」；
- 新进每日最多 ``entry_per_day`` 只、总持仓不超过 ``target_positions``，
  **不为了凑数降低门槛**；
- 机构级硬条件：ADV20 最低日均成交额、ST/退市剔除、当日剩余资金逐笔扣减；
  成交额不可校验时**不新进**（不再按「容量未校验」放行）；
- 单票目标金额 = ``min(单票预算, ADV20 × 参与率, 当日剩余资金)``；
- 输出只有纸面台账字段，**不产生任何订单/委托/成交语义**。

纯函数、无 IO；持仓与行情由 service 层查库后传入。
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from collections.abc import Collection, Mapping, Sequence

from backend.services.engine.inference.blend_engine import BlendRow

LOT_SIZE = 100
DEFAULT_TARGET_POSITIONS = 50
DEFAULT_TOP_K = 50
DEFAULT_HOLD_DAYS = 10
DEFAULT_ENTRY_PER_DAY = 5
DEFAULT_CASH_BUFFER = 0.02
DEFAULT_ADV_PARTICIPATION = 0.01
DEFAULT_EXIT_DELTA = 0.03
DEFAULT_LOOKBACK = 20
DEFAULT_MIN_LOOKBACK_DAYS = 15
DEFAULT_CAPITAL = 10_000_000.0
#: 新进最低 20 日均成交额（亿元）。低于该值视为机构容量不足，不新进。
DEFAULT_MIN_ADV_YI = 1.0
DEFAULT_ADV_LOOKBACK_DAYS = 20
DEFAULT_MIN_ADV_DAYS = 15
YI = 100_000_000.0


@dataclass(frozen=True)
class PlanConfig:
    """调仓计划参数（默认值与设计稿 §4.4 一致）。"""

    capital: float = DEFAULT_CAPITAL
    top_k: int = DEFAULT_TOP_K
    hold_days: int = DEFAULT_HOLD_DAYS
    entry_per_day: int = DEFAULT_ENTRY_PER_DAY
    target_positions: int = DEFAULT_TARGET_POSITIONS
    cash_buffer: float = DEFAULT_CASH_BUFFER
    adv_participation: float = DEFAULT_ADV_PARTICIPATION
    exit_delta: float = DEFAULT_EXIT_DELTA
    lookback: int = DEFAULT_LOOKBACK
    min_lookback_days: int = DEFAULT_MIN_LOOKBACK_DAYS
    min_adv_yi: float = DEFAULT_MIN_ADV_YI
    exclude_st: bool = True
    adv_lookback_days: int = DEFAULT_ADV_LOOKBACK_DAYS
    min_adv_days: int = DEFAULT_MIN_ADV_DAYS

    def to_dict(self) -> dict[str, Any]:
        return {
            "capital": self.capital,
            "top_k": self.top_k,
            "hold_days": self.hold_days,
            "entry_per_day": self.entry_per_day,
            "target_positions": self.target_positions,
            "cash_buffer": self.cash_buffer,
            "adv_participation": self.adv_participation,
            "exit_delta": self.exit_delta,
            "lookback": self.lookback,
            "min_lookback_days": self.min_lookback_days,
            "min_adv_yi": self.min_adv_yi,
            "exclude_st": self.exclude_st,
            "adv_lookback_days": self.adv_lookback_days,
            "min_adv_days": self.min_adv_days,
        }


@dataclass(frozen=True)
class PaperPosition:
    """纸面台账里的一笔开仓（确认计划后写入）。"""

    symbol: str
    entry_date: date
    shares: int = 0
    target_value: float | None = None


@dataclass(frozen=True)
class RebalancePlan:
    data_trade_date: date
    target_date: date
    params: Mapping[str, Any]
    summary: Mapping[str, Any]
    entries: Sequence[Mapping[str, Any]]
    holds: Sequence[Mapping[str, Any]]
    exits: Sequence[Mapping[str, Any]]
    warnings: Sequence[str] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "data_trade_date": self.data_trade_date.isoformat(),
            "target_date": self.target_date.isoformat(),
            "params": dict(self.params),
            "summary": dict(self.summary),
            "entries": [dict(row) for row in self.entries],
            "holds": [dict(row) for row in self.holds],
            "exits": [dict(row) for row in self.exits],
            "warnings": list(self.warnings),
        }


def _finite(value: float | None) -> bool:
    return value is not None and math.isfinite(float(value))


def mean_score(
    history: Mapping[date, Mapping[str, float]],
    symbol: str,
    *,
    date: date,
    lookback: int = DEFAULT_LOOKBACK,
    min_lookback_days: int = DEFAULT_MIN_LOOKBACK_DAYS,
) -> float | None:
    """T 之前 ``lookback`` 个交易日的 S 均值；有效日不足返回 None（公开包装）。"""
    return _mean20(
        history,
        symbol,
        date=date,
        lookback=lookback,
        min_lookback_days=min_lookback_days,
    )


def _mean20(
    history: Mapping[date, Mapping[str, float]],
    symbol: str,
    *,
    date: date,
    lookback: int,
    min_lookback_days: int,
) -> float | None:
    """T 之前最近 ``lookback`` 个交易日里该标的的 S 均值；有效日不足返回 None。"""
    days = sorted(day for day in history if day < date)
    if not days:
        return None
    window = days[-lookback:]
    values = [
        float(history[day][symbol])
        for day in window
        if symbol in history[day] and _finite(history[day][symbol])
    ]
    if len(values) < min_lookback_days:
        return None
    return sum(values) / len(values)


def _held_days(trading_days: Sequence[date], entry_date: date, date: date) -> int:
    """持有交易日数：entry_date 之后、含 date 的交易日个数。"""
    return sum(1 for day in trading_days if entry_date < day <= date)


def _lot_shares(target_value: float, price: float) -> int:
    if not _finite(price) or price <= 0:
        return 0
    return int(target_value / price / LOT_SIZE) * LOT_SIZE


def _row_payload(row: BlendRow) -> dict[str, Any]:
    return {
        "symbol": row.symbol,
        "score": row.score,
        "pct": row.pct,
        "research_score": row.research_score,
        "position": row.position,
        "rank": row.rank,
    }


def build_rebalance_plan(
    *,
    data_trade_date: date,
    target_date: date,
    ranking: Sequence[BlendRow],
    history: Mapping[date, Mapping[str, float]],
    trading_days: Sequence[date],
    positions: Sequence[PaperPosition],
    config: PlanConfig | None = None,
    prices: Mapping[str, float] | None = None,
    amounts: Mapping[str, float] | None = None,
    avg_amounts: Mapping[str, float] | None = None,
    risk_flags: Mapping[str, str] | None = None,
) -> RebalancePlan:
    """生成单日纸面调仓计划。

    ``history`` 是 ``date → symbol → S``（不含 T）；``trading_days`` 升序，
    必须包含 T 之前与 T 当天的真实交易日；``prices/avg_amounts`` 来自
    QuantDB 不复权日线（amount 单位为元）。``avg_amounts`` 是 ADV20；
    未提供时退回 ``amounts``（仅兼容旧调用，生产必须传 ADV）。
    ``risk_flags`` 为 ``symbol → "ST"/"退市"`` 的当前截面风险标记。
    """
    cfg = config or PlanConfig()
    prices = prices or {}
    amounts = amounts or {}
    adv_map = avg_amounts if avg_amounts is not None else amounts
    risk_flags = risk_flags or {}
    min_adv = max(0.0, float(cfg.min_adv_yi) * YI)
    ranking_by_symbol = {row.symbol: row for row in ranking}
    held = {position.symbol: position for position in positions}
    warnings: list[str] = []

    mean_cache: dict[str, float | None] = {}

    def mean_of(symbol: str) -> float | None:
        if symbol not in mean_cache:
            mean_cache[symbol] = _mean20(
                history,
                symbol,
                date=data_trade_date,
                lookback=cfg.lookback,
                min_lookback_days=cfg.min_lookback_days,
            )
        return mean_cache[symbol]

    def adv_fields(symbol: str) -> dict[str, Any]:
        adv = adv_map.get(symbol)
        return {"adv_yi": float(adv) / YI if _finite(adv) else None}

    def liquidity_note(symbol: str) -> str:
        adv = adv_map.get(symbol)
        if not _finite(adv):
            return "成交额缺失，需人工复核"
        if min_adv > 0 and float(adv) < min_adv:
            return "流动性不足，需人工复核"
        return ""

    def merge_note(note: str, extra: str) -> str:
        return "；".join(part for part in (note, extra) if part)

    holds: list[dict[str, Any]] = []
    exits: list[dict[str, Any]] = []
    for symbol, position in sorted(held.items()):
        row = ranking_by_symbol.get(symbol)
        held_days = _held_days(trading_days, position.entry_date, data_trade_date)
        mean20 = mean_of(symbol)
        base = {
            "symbol": symbol,
            "first_entry_date": position.entry_date.isoformat(),
            "held_days": held_days,
            **adv_fields(symbol),
        }
        flag = risk_flags.get(symbol) if cfg.exclude_st else None
        if flag:
            exits.append(
                {
                    **base,
                    **(
                        _row_payload(row)
                        if row is not None
                        else {
                            "score": None,
                            "pct": None,
                            "research_score": None,
                            "position": None,
                        }
                    ),
                    "mean20": mean20,
                    "exit_line": (mean20 - cfg.exit_delta)
                    if mean20 is not None
                    else None,
                    "exit_reason": f"风险警示({flag})",
                }
            )
            continue
        if row is None:
            holds.append(
                {
                    **base,
                    "score": None,
                    "pct": None,
                    "research_score": None,
                    "position": None,
                    "mean20": mean20,
                    "exit_line": (mean20 - cfg.exit_delta)
                    if mean20 is not None
                    else None,
                    "note": merge_note("数据不足，需人工复核", liquidity_note(symbol)),
                }
            )
            continue
        if held_days >= cfg.hold_days:
            exits.append(
                {
                    **base,
                    **_row_payload(row),
                    "mean20": mean20,
                    "exit_line": (mean20 - cfg.exit_delta)
                    if mean20 is not None
                    else None,
                    "exit_reason": "到期",
                }
            )
            continue
        if mean20 is None:
            holds.append(
                {
                    **base,
                    **_row_payload(row),
                    "mean20": None,
                    "exit_line": None,
                    "note": merge_note("历史不足，需人工复核", liquidity_note(symbol)),
                }
            )
            continue
        exit_line = mean20 - cfg.exit_delta
        if row.score < exit_line:
            exits.append(
                {
                    **base,
                    **_row_payload(row),
                    "mean20": mean20,
                    "exit_line": exit_line,
                    "exit_reason": "分数退出",
                }
            )
            continue
        holds.append(
            {
                **base,
                **_row_payload(row),
                "mean20": mean20,
                "exit_line": exit_line,
                "note": liquidity_note(symbol),
            }
        )

    slots = max(0, cfg.target_positions - len(holds))
    available = max(0.0, cfg.capital * (1.0 - cfg.cash_buffer))
    budget_per_position = available / max(1, cfg.target_positions)
    # 持仓（含当日待退出）仍占用资金：A 股卖出资金当日不可再用于买入
    held_value = 0.0
    for position in positions:
        price = prices.get(position.symbol)
        if _finite(price) and position.shares > 0:
            held_value += float(price) * position.shares
        elif _finite(position.target_value):
            held_value += float(position.target_value)
    remaining = max(0.0, available - held_value)
    entries: list[dict[str, Any]] = []
    for row in ranking:
        if len(entries) >= min(cfg.entry_per_day, slots):
            break
        if row.symbol in held:
            continue
        if row.rank > cfg.top_k:
            continue
        flag = risk_flags.get(row.symbol) if cfg.exclude_st else None
        if flag:
            warnings.append(f"风险警示:{row.symbol}:{flag}")
            continue
        mean20 = mean_of(row.symbol)
        if mean20 is None:
            warnings.append(f"历史不足:{row.symbol}")
            continue
        if row.score < mean20:
            continue
        price = prices.get(row.symbol)
        if not _finite(price):
            warnings.append(f"无参考价:{row.symbol}")
            continue
        adv = adv_map.get(row.symbol)
        if not _finite(adv):
            # 机构口径：无法校验容量就不新进，不按「未校验」放行
            warnings.append(f"成交额缺失:{row.symbol}")
            continue
        if float(adv) <= 0 or (min_adv > 0 and float(adv) < min_adv):
            warnings.append(f"成交额不足:{row.symbol}:{float(adv) / YI:.2f}亿")
            continue
        if remaining <= 0:
            warnings.append("资金用尽")
            break
        target_value = min(
            budget_per_position,
            float(adv) * cfg.adv_participation,
            remaining,
        )
        shares = _lot_shares(target_value, float(price))
        if shares <= 0:
            warnings.append(f"资金不足一手:{row.symbol}")
            continue
        entries.append(
            {
                **_row_payload(row),
                "mean20": mean20,
                "exit_line": mean20 - cfg.exit_delta,
                "target_value": shares * float(price),
                "ref_price": float(price),
                "shares": shares,
                "adv_yi": float(adv) / YI,
                "note": "",
            }
        )
        remaining -= shares * float(price)

    capital_used = held_value + sum(
        float(row["shares"]) * float(row["ref_price"]) for row in entries
    )
    summary = {
        "capital": cfg.capital,
        "target_positions": cfg.target_positions,
        "entry_per_day": cfg.entry_per_day,
        "hold_days": cfg.hold_days,
        "top_k": cfg.top_k,
        "cash_buffer": cfg.cash_buffer,
        "capital_available": round(available, 2),
        "capital_used": round(capital_used, 2),
        "capital_remaining": round(max(0.0, available - capital_used), 2),
        "min_adv_yi": cfg.min_adv_yi,
        "exclude_st": cfg.exclude_st,
        "adv_lookback_days": cfg.adv_lookback_days,
        "n_entries": len(entries),
        "n_holds": len(holds),
        "n_exits": len(exits),
        "n_open_after": len(holds) + len(entries),
    }
    return RebalancePlan(
        data_trade_date=data_trade_date,
        target_date=target_date,
        params=cfg.to_dict(),
        summary=summary,
        entries=entries,
        holds=holds,
        exits=exits,
        warnings=warnings,
    )


def consecutive_days_in_topk(
    top_history: Mapping[date, Collection[str]],
    *,
    symbol: str,
    date: date,
) -> int:
    """截至 ``date``（含）连续在 TopK 的天数；当日不在榜 → 0。"""
    days = sorted(day for day in top_history if day <= date)
    count = 0
    for day in reversed(days):
        if symbol in top_history[day]:
            count += 1
        else:
            break
    return count


def positions_hash(positions: Sequence[PaperPosition]) -> str:
    """当前 open 持仓的稳定哈希（确认计划时防止基于过期台账落单）。"""
    lines = sorted(
        f"{position.symbol}|{position.entry_date.isoformat()}|{position.shares}"
        for position in positions
    )
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def config_hash(
    *,
    members: Sequence[tuple[str, float]],
    min_coverage: int,
    method: str = "mean",
    trim_count: int = 2,
    consensus_min_count: int,
    top_k: int = DEFAULT_TOP_K,
    hold_days: int = DEFAULT_HOLD_DAYS,
    entry_per_day: int = DEFAULT_ENTRY_PER_DAY,
    target_positions: int = DEFAULT_TARGET_POSITIONS,
    cash_buffer: float = DEFAULT_CASH_BUFFER,
    adv_participation: float = DEFAULT_ADV_PARTICIPATION,
    exit_delta: float = DEFAULT_EXIT_DELTA,
    lookback: int = DEFAULT_LOOKBACK,
    min_lookback_days: int = DEFAULT_MIN_LOOKBACK_DAYS,
    capital: float = DEFAULT_CAPITAL,
    min_adv_yi: float = DEFAULT_MIN_ADV_YI,
    exclude_st: bool = True,
    adv_lookback_days: int = DEFAULT_ADV_LOOKBACK_DAYS,
    min_adv_days: int = DEFAULT_MIN_ADV_DAYS,
    consensus_threshold: float = 0.70,
) -> str:
    """组合配置指纹：成员/权重/门槛/调仓参数任一变化 → 新计划。"""
    payload = {
        "members": sorted(
            [str(model_id), float(weight)] for model_id, weight in members
        ),
        "method": str(method),
        "trim_count": int(trim_count),
        "min_coverage": int(min_coverage),
        "consensus_min_count": int(consensus_min_count),
        "consensus_threshold": float(consensus_threshold),
        "top_k": int(top_k),
        "hold_days": int(hold_days),
        "entry_per_day": int(entry_per_day),
        "target_positions": int(target_positions),
        "cash_buffer": float(cash_buffer),
        "adv_participation": float(adv_participation),
        "exit_delta": float(exit_delta),
        "lookback": int(lookback),
        "min_lookback_days": int(min_lookback_days),
        "capital": float(capital),
        "min_adv_yi": float(min_adv_yi),
        "exclude_st": bool(exclude_st),
        "adv_lookback_days": int(adv_lookback_days),
        "min_adv_days": int(min_adv_days),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
