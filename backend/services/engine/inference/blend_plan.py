"""纸面调仓计划：对组合榜做「继续持有 / 到期退出 / 新进」的确定性裁定。

口径与 P1 研究脚本 ``combo_eval.simulate_rebalance`` 的 10 日闸门一致，但这里是
**单日计划**（不是逐日回放）：

- 持有天数按交易日计（``trading_days`` 中 entry_date 之后、含 T 的日数）；
- 分数退出线 = ``mean20(S) − exit_delta``，``mean20`` 只取 T 之前的
  ``lookback`` 个交易日，有效日不足 ``min_lookback_days`` 视为「数据不足」；
- 新进每日最多 ``entry_per_day`` 只、总持仓不超过 ``target_positions``，
  **不为了凑数降低门槛**；
- 单票目标金额 = ``min(资金上限, T 日成交额 × 参与率)``，缺成交额只标注
  「容量未校验」，不静默放行；
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
) -> RebalancePlan:
    """生成单日纸面调仓计划。

    ``history`` 是 ``date → symbol → S``（不含 T）；``trading_days`` 升序，
    必须包含 T 之前与 T 当天的真实交易日；``prices/amounts`` 来自
    QuantDB 不复权日线（amount 单位为元）。
    """
    cfg = config or PlanConfig()
    prices = prices or {}
    amounts = amounts or {}
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
        }
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
                    "note": "数据不足，需人工复核",
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
                    "note": "历史不足，需人工复核",
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
                "note": "",
            }
        )

    slots = max(0, cfg.target_positions - len(holds))
    budget_per_position = (
        cfg.capital * (1.0 - cfg.cash_buffer) / max(1, cfg.target_positions)
    )
    entries: list[dict[str, Any]] = []
    for row in ranking:
        if len(entries) >= min(cfg.entry_per_day, slots):
            break
        if row.symbol in held:
            continue
        if row.rank > cfg.top_k:
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
        amount = amounts.get(row.symbol)
        if _finite(amount):
            target_value = min(
                budget_per_position, float(amount) * cfg.adv_participation
            )
            note = ""
        else:
            target_value = budget_per_position
            note = "容量未校验"
            warnings.append(f"容量未校验:{row.symbol}")
        shares = _lot_shares(target_value, float(price))
        if shares <= 0:
            warnings.append(f"资金不足一手:{row.symbol}")
            continue
        entries.append(
            {
                **_row_payload(row),
                "mean20": mean20,
                "exit_line": mean20 - cfg.exit_delta,
                "target_value": target_value,
                "ref_price": float(price),
                "shares": shares,
                "note": note,
            }
        )

    summary = {
        "capital": cfg.capital,
        "target_positions": cfg.target_positions,
        "entry_per_day": cfg.entry_per_day,
        "hold_days": cfg.hold_days,
        "top_k": cfg.top_k,
        "cash_buffer": cfg.cash_buffer,
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
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
