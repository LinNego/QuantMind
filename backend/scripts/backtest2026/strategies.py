"""TopK 组合与滚动桶策略的现金流记账。

成交口径固定：
- 信号日 t 收盘出分；
- 入场日 e = t+1 收盘买入；
- 退出日 x = e+H 收盘卖出；
- 买入/卖出均按整手 100 股取整，费用双边万 3（印花税可选）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Iterable

import numpy as np
import pandas as pd

from .config import (
    COMMISSION_RATE,
    LOT_SIZE,
    MAX_EXIT_DEFER_DAYS,
    SLIPPAGE,
    STAMP_DUTY_RATE,
)
from .data import MarketPanel
from .filters import entry_blocked_mask, exit_blocked_mask


@dataclass
class Position:
    symbol: str
    shares: int
    entry_price: float
    signal_date: pd.Timestamp
    entry_date: pd.Timestamp
    due_date: pd.Timestamp
    cost: float
    buy_fee: float


@dataclass
class Bucket:
    bucket_id: int
    label: str
    created_idx: int
    entry_idx: int
    due_idx: int
    target_budget: float
    cash: float = 0.0
    positions: dict[str, Position] = field(default_factory=dict)

    def value(self, day: pd.Timestamp, panel: MarketPanel) -> float:
        total = self.cash
        for pos in self.positions.values():
            total += pos.shares * panel.mark_price(pos.symbol, day)
        return total


class Portfolio:
    def __init__(
        self,
        cash: float,
        fee_rate: float = COMMISSION_RATE,
        stamp_rate: float = 0.0,
        slippage: float = SLIPPAGE,
    ) -> None:
        self.cash = float(cash)
        self.fee_rate = float(fee_rate)
        self.stamp_rate = float(stamp_rate)
        self.slippage = float(slippage)
        self.buckets: dict[int, Bucket] = {}
        self.trades: list[dict] = []
        self.equity_rows: list[dict] = []
        self.skips: list[dict] = []
        self._seq = 0

    # ── 资金 ────────────────────────────────────────────────────────────
    def total_equity(self, day: pd.Timestamp, panel: MarketPanel) -> float:
        return self.cash + sum(
            bucket.value(day, panel) for bucket in self.buckets.values()
        )

    def create_bucket(
        self,
        label: str,
        created_idx: int,
        entry_idx: int,
        due_idx: int,
        target_budget: float,
    ) -> Bucket:
        self._seq += 1
        bucket = Bucket(
            bucket_id=self._seq,
            label=label,
            created_idx=created_idx,
            entry_idx=entry_idx,
            due_idx=due_idx,
            target_budget=float(target_budget),
        )
        self.buckets[bucket.bucket_id] = bucket
        return bucket

    def fund_and_buy(
        self,
        bucket: Bucket,
        symbols: Iterable[str],
        day: pd.Timestamp,
        panel: MarketPanel,
        per_name_budget: float,
    ) -> None:
        symbol_list = list(symbols)
        budget = min(bucket.target_budget, self.cash)
        if budget <= 0 or not symbol_list:
            self.skips.append(
                {
                    "bucket": bucket.bucket_id,
                    "date": day,
                    "symbol": None,
                    "reason": "no_cash",
                }
            )
            return
        self.cash -= budget
        bucket.cash += budget
        per_name = min(per_name_budget, budget / len(symbol_list))
        blocked = entry_blocked_mask(
            pd.Index(symbol_list),
            day,
            panel.limit_up,
            panel.limit_down,
            panel.suspended,
        )
        for symbol in symbol_list:
            if bool(blocked.get(symbol, True)):
                self.skips.append(
                    {
                        "bucket": bucket.bucket_id,
                        "date": day,
                        "symbol": symbol,
                        "reason": "entry_blocked",
                    }
                )
                continue
            price = panel.price(symbol, day)
            if not np.isfinite(price) or price <= 0:
                self.skips.append(
                    {
                        "bucket": bucket.bucket_id,
                        "date": day,
                        "symbol": symbol,
                        "reason": "no_price",
                    }
                )
                continue
            exec_price = price * (1.0 + self.slippage)
            shares = int(per_name // (exec_price * LOT_SIZE)) * LOT_SIZE
            while (
                shares > 0 and shares * exec_price * (1.0 + self.fee_rate) > bucket.cash
            ):
                shares -= LOT_SIZE
            if shares <= 0:
                self.skips.append(
                    {
                        "bucket": bucket.bucket_id,
                        "date": day,
                        "symbol": symbol,
                        "reason": "insufficient_cash",
                    }
                )
                continue
            cost = shares * exec_price
            fee = cost * self.fee_rate
            bucket.cash -= cost + fee
            pos = Position(
                symbol=symbol,
                shares=shares,
                entry_price=exec_price,
                signal_date=panel.dates[bucket.created_idx],
                entry_date=day,
                due_date=panel.dates[bucket.due_idx],
                cost=cost,
                buy_fee=fee,
            )
            bucket.positions[symbol] = pos
            self.trades.append(
                {
                    "bucket": bucket.bucket_id,
                    "label": bucket.label,
                    "side": "buy",
                    "signal_date": panel.dates[bucket.created_idx],
                    "date": day,
                    "symbol": symbol,
                    "shares": shares,
                    "price": exec_price,
                    "amount": cost,
                    "fee": fee,
                    "net_pnl": np.nan,
                    "exit_reason": "",
                    "scheduled_exit": pos.due_date,
                }
            )

    def settle_due(self, day: pd.Timestamp, day_idx: int, panel: MarketPanel) -> None:
        for bucket in list(self.buckets.values()):
            if bucket.due_idx > day_idx:
                continue
            for symbol, pos in list(bucket.positions.items()):
                reason = ""
                sell_price = panel.price(symbol, day)
                if exit_blocked_mask(symbol, day, panel.limit_down, panel.suspended):
                    if day_idx - bucket.due_idx < MAX_EXIT_DEFER_DAYS:
                        continue
                    sell_price = panel.mark_price(symbol, day)
                    reason = "forced_exit"
                if not np.isfinite(sell_price) or sell_price <= 0:
                    continue
                exec_price = sell_price * (1.0 - self.slippage)
                proceeds = pos.shares * exec_price
                fee = proceeds * (self.fee_rate + self.stamp_rate)
                bucket.cash += proceeds - fee
                del bucket.positions[symbol]
                self.trades.append(
                    {
                        "bucket": bucket.bucket_id,
                        "label": bucket.label,
                        "side": "sell",
                        "signal_date": pos.signal_date,
                        "date": day,
                        "symbol": symbol,
                        "shares": pos.shares,
                        "price": exec_price,
                        "amount": proceeds,
                        "fee": fee,
                        "net_pnl": proceeds - fee - pos.cost - pos.buy_fee,
                        "exit_reason": reason or "due",
                        "scheduled_exit": pos.due_date,
                    }
                )
            if not bucket.positions:
                self.cash += bucket.cash
                del self.buckets[bucket.bucket_id]

    def record_equity(self, day: pd.Timestamp, panel: MarketPanel) -> None:
        value = self.total_equity(day, panel)
        market_value = sum(
            pos.shares * panel.mark_price(pos.symbol, day)
            for bucket in self.buckets.values()
            for pos in bucket.positions.values()
        )
        self.equity_rows.append(
            {
                "date": day,
                "equity": value,
                "cash": self.cash + sum(b.cash for b in self.buckets.values()),
                "market_value": market_value,
                "buckets": len(self.buckets),
            }
        )

    # ── 产物 ────────────────────────────────────────────────────────────
    def trades_frame(self) -> pd.DataFrame:
        columns = [
            "bucket",
            "label",
            "side",
            "signal_date",
            "date",
            "symbol",
            "shares",
            "price",
            "amount",
            "fee",
            "net_pnl",
            "exit_reason",
            "scheduled_exit",
        ]
        return pd.DataFrame(self.trades, columns=columns)

    def equity_frame(self) -> pd.DataFrame:
        frame = pd.DataFrame(self.equity_rows)
        if not frame.empty:
            frame = frame.set_index("date").sort_index()
        return frame

    def skips_frame(self) -> pd.DataFrame:
        columns = ["bucket", "date", "symbol", "reason"]
        return pd.DataFrame(self.skips, columns=columns)


@dataclass
class StrategySpec:
    mode: str  # "cohort" | "rolling"
    k: int
    horizon: int
    initial_capital: float
    per_name: float | None = None
    commission: float = COMMISSION_RATE
    stamp: float = 0.0
    slippage: float = SLIPPAGE
    exclude_st: bool = True
    exclude_limits: bool = True
    exclude_new: bool = True


@dataclass
class StrategyResult:
    spec: StrategySpec
    equity: pd.DataFrame
    trades: pd.DataFrame
    skips: pd.DataFrame
    signal_days: int
    selected_total: int


def _select(
    pred_row: pd.Series,
    day: pd.Timestamp,
    panel: MarketPanel,
    spec: StrategySpec,
) -> list[str]:
    """按开关组装闸门；停牌始终剔除（无法成交）。"""
    symbols = pred_row.index
    mask = pred_row.notna()
    mask &= ~panel.suspended.loc[day].reindex(symbols).fillna(True).astype(bool)
    if spec.exclude_st:
        mask &= ~panel.is_st.reindex(symbols).fillna(False).astype(bool)
    if spec.exclude_limits:
        mask &= ~panel.limit_up.loc[day].reindex(symbols).fillna(True).astype(bool)
        mask &= ~panel.limit_down.loc[day].reindex(symbols).fillna(True).astype(bool)
    if spec.exclude_new:
        mask &= panel.age_days.loc[day].reindex(symbols).fillna(-1) >= 60
    scores = pred_row[mask].dropna().sort_values(ascending=False, kind="mergesort")
    return list(scores.index[: spec.k])


def run_strategy(
    panel: MarketPanel,
    pred_by_date: dict[pd.Timestamp, pd.Series],
    spec: StrategySpec,
) -> StrategyResult:
    signal_days = sorted(pred_by_date)
    if not signal_days:
        raise ValueError("预测面板为空")
    date_position = {day: i for i, day in enumerate(panel.dates)}
    first_idx = date_position[signal_days[0]]
    last_signal_idx = date_position[signal_days[-1]]
    final_due_idx = last_signal_idx + 1 + spec.horizon
    last_idx = min(len(panel.dates) - 1, final_due_idx + MAX_EXIT_DEFER_DAYS + 1)

    portfolio = Portfolio(
        spec.initial_capital,
        fee_rate=spec.commission,
        stamp_rate=spec.stamp,
        slippage=spec.slippage,
    )
    pending: list[tuple[Bucket, list[str], float]] = []
    selected_total = 0
    n_buckets = spec.horizon if spec.mode == "rolling" else 1
    per_name = spec.per_name

    for idx in range(first_idx, last_idx + 1):
        day = panel.dates[idx]
        # 1) 到期卖出（收盘）
        portfolio.settle_due(day, idx, panel)
        # 2) 当日应入场的桶（收盘买入），资金来自刚卖出的回款
        remaining = []
        for bucket, symbols, budget in pending:
            if bucket.entry_idx != idx:
                remaining.append((bucket, symbols, budget))
                continue
            per_name_budget = (
                per_name if per_name is not None else budget / max(len(symbols), 1)
            )
            portfolio.fund_and_buy(bucket, symbols, day, panel, per_name_budget)
        pending = remaining
        # 3) 当日信号 → 建桶，次日收盘买
        if day in pred_by_date and idx <= last_signal_idx:
            can_open = True
            if spec.mode == "cohort":
                can_open = not portfolio.buckets and not pending
            # 到期日必须落在面板内，否则 Position.due_date 无值可索引。
            if can_open and idx + 1 + spec.horizon <= len(panel.dates) - 1:
                symbols = _select(pred_by_date[day], day, panel, spec)
                if symbols:
                    selected_total += len(symbols)
                    target = (
                        portfolio.total_equity(day, panel) / n_buckets
                        if spec.mode == "rolling"
                        else portfolio.cash
                    )
                    bucket = portfolio.create_bucket(
                        label=f"{day.strftime('%Y%m%d')}",
                        created_idx=idx,
                        entry_idx=idx + 1,
                        due_idx=idx + 1 + spec.horizon,
                        target_budget=target,
                    )
                    pending.append((bucket, symbols, target))
        # 4) 盯市
        portfolio.record_equity(day, panel)
        if idx >= final_due_idx and not portfolio.buckets and not pending:
            break

    return StrategyResult(
        spec=spec,
        equity=portfolio.equity_frame(),
        trades=portfolio.trades_frame(),
        skips=portfolio.skips_frame(),
        signal_days=len(signal_days),
        selected_total=selected_total,
    )


def build_pred_by_date(pred_panel: pd.DataFrame) -> dict[pd.Timestamp, pd.Series]:
    out: dict[pd.Timestamp, pd.Series] = {}
    for day, group in pred_panel.groupby("trade_date", sort=True):
        out[pd.Timestamp(day)] = group.set_index("symbol")["pred"].astype(float)
    return out
