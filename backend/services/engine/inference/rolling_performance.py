"""滚动建仓窗口的收盘价损益口径（纯函数，无 IO）。

口径（2026-10-21 用户拍板）：

- 买入 = 入选当日**收盘价**（尾盘买入）；卖出 = 退出当日收盘价；
- 一笔交易 = 从窗口内首次在榜到首次不在榜；断榜后重进按新一笔计；
- 窗口首列的标的看不到窗口外更早的入场日，按首列收盘价买入并标记
  ``entry_from_window_start=True``；
- 缺失/非法收盘价 → ``return_pct=None``（展示 `—`，绝不写 0），且不计入
  胜率/均值分母；
- 组合口径：每日等权持有当日 TopK，收盘换仓，次日收盘结算，
  ``cycle_return_pct = Π(1 + r_t) − 1``（未计费用）。

机构级统计（2026-10-21）：``simulate_daily_rotation`` 给出每日批次的
已实现/浮动拆分、基准与超额、风险指标；窗口最后一个批次只估值不结算
（计入浮动），末日入选只作为下一批次候选，不建仓。``cell_stats`` 给出
每只标的在窗口内的在榜表现与每个格子的当日涨跌幅/当日盈亏。

本模块只做计算；名称/行业/市值的补全与日期序列化在 service 层完成。
"""

from __future__ import annotations

import math
import statistics
from datetime import date
from typing import Any
from collections.abc import Mapping, Sequence

from backend.services.engine.inference import portfolio_metrics as metrics

RETURN_NDIGITS = 4
WIN_RATE_NDIGITS = 2
AMOUNT_NDIGITS = 2
DEFAULT_CAPITAL = 10_000_000.0

#: 可交易性口径（与纸面计划 ``blend_plan`` 同一套默认值）
YI = 100_000_000.0
DEFAULT_MIN_ADV_YI = 1.0
DEFAULT_ADV_PARTICIPATION = 0.01
DEFAULT_ADV_LOOKBACK_DAYS = 20
DEFAULT_MIN_ADV_DAYS = 15

STATUS_BOUGHT = "bought"
STATUS_NEXT_DAY = "next_day"
STATUS_SKIP_ST = "skip_st"
STATUS_SKIP_LOW_ADV = "skip_low_adv"
STATUS_SKIP_NO_AMOUNT = "skip_no_amount"
STATUS_SKIP_NO_PRICE = "skip_no_price"


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _close(value: Any) -> float | None:
    """收盘价必须是正的有限数；否则视为缺失。"""
    if not _finite(value):
        return None
    number = float(value)
    return number if number > 0 else None


def _return_pct(entry_price: float | None, exit_price: float | None) -> float | None:
    if entry_price is None or exit_price is None:
        return None
    return round((exit_price / entry_price - 1.0) * 100.0, RETURN_NDIGITS)


def _day_picks(picks_by_day: Mapping[date, Sequence[str]], day: date) -> list[str]:
    """当日标的（去重、保持顺序）；空日按空列表处理。"""
    return list(dict.fromkeys(str(symbol) for symbol in (picks_by_day.get(day) or [])))


def compute_rolling_performance(
    days: Sequence[date],
    picks_by_day: Mapping[date, Sequence[str]],
    closes: Mapping[str, Mapping[date, float]],
) -> dict[str, Any]:
    """计算窗口内每笔退出损益、在场浮动与组合周期收益。"""
    ordered_days = list(days)
    exits_by_day: dict[date, list[dict[str, Any]]] = {day: [] for day in ordered_days}
    closed: list[dict[str, Any]] = []

    def close_of(symbol: str, day: date) -> float | None:
        return _close((closes.get(symbol) or {}).get(day))

    entry_dates: dict[str, date] = {}
    streak: dict[str, int] = {}
    previous: set[str] = set()
    for day in ordered_days:
        picks = _day_picks(picks_by_day, day)
        today = set(picks)
        for symbol in picks:
            if symbol not in entry_dates:
                entry_dates[symbol] = day
                streak[symbol] = 0
            streak[symbol] += 1
        for symbol in sorted(previous - today):
            entry_day = entry_dates.pop(symbol)
            entry_price = close_of(symbol, entry_day)
            exit_price = close_of(symbol, day)
            row = {
                "symbol": symbol,
                "entry_date": entry_day,
                "exit_date": day,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "return_pct": _return_pct(entry_price, exit_price),
                "held_days": streak.pop(symbol, 0),
                "entry_from_window_start": entry_day == ordered_days[0],
            }
            exits_by_day[day].append(row)
            closed.append(row)
        previous = today

    open_rows: list[dict[str, Any]] = []
    last_day = ordered_days[-1] if ordered_days else None
    if last_day is not None:
        for symbol in sorted(entry_dates, key=lambda s: (entry_dates[s], s)):
            entry_day = entry_dates[symbol]
            entry_price = close_of(symbol, entry_day)
            mark_price = close_of(symbol, last_day)
            open_rows.append(
                {
                    "symbol": symbol,
                    "entry_date": entry_day,
                    "mark_date": last_day,
                    "entry_price": entry_price,
                    "mark_price": mark_price,
                    "return_pct": _return_pct(entry_price, mark_price),
                    "held_days": streak.get(symbol, 0),
                    "entry_from_window_start": entry_day == ordered_days[0],
                }
            )

    daily: list[dict[str, Any]] = []
    for index in range(max(0, len(ordered_days) - 1)):
        day = ordered_days[index]
        next_day = ordered_days[index + 1]
        returns: list[float] = []
        for symbol in _day_picks(picks_by_day, day):
            value = _return_pct(close_of(symbol, day), close_of(symbol, next_day))
            if value is not None:
                returns.append(value)
        daily.append(
            {
                "date": day,
                "next_date": next_day,
                "return_pct": round(sum(returns) / len(returns), RETURN_NDIGITS)
                if returns
                else None,
                "priced": len(returns),
            }
        )

    return {
        "exits_by_day": exits_by_day,
        "closed": closed,
        "open": open_rows,
        "daily": daily,
        "stats": _stats(closed, open_rows, daily),
    }


def _stats(
    closed: Sequence[Mapping[str, Any]],
    open_rows: Sequence[Mapping[str, Any]],
    daily: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    priced_rows = [row for row in closed if row["return_pct"] is not None]
    priced = [float(row["return_pct"]) for row in priced_rows]
    wins = sum(1 for value in priced if value > 0)
    losses = sum(1 for value in priced if value < 0)
    flats = sum(1 for value in priced if value == 0)
    best = (
        max(priced_rows, key=lambda row: float(row["return_pct"]))
        if priced_rows
        else None
    )
    worst = (
        min(priced_rows, key=lambda row: float(row["return_pct"]))
        if priced_rows
        else None
    )

    open_priced = [
        float(row["return_pct"]) for row in open_rows if row["return_pct"] is not None
    ]
    cycle_values = [
        float(row["return_pct"]) for row in daily if row["return_pct"] is not None
    ]
    cycle_return: float | None = None
    if cycle_values:
        growth = math.prod(1.0 + value / 100.0 for value in cycle_values)
        cycle_return = round((growth - 1.0) * 100.0, RETURN_NDIGITS)

    return {
        "closed_trades": len(closed),
        "priced_trades": len(priced),
        "unpriced_trades": len(closed) - len(priced),
        "wins": wins,
        "losses": losses,
        "flats": flats,
        "win_rate_pct": round(wins / len(priced) * 100.0, WIN_RATE_NDIGITS)
        if priced
        else None,
        "avg_return_pct": round(sum(priced) / len(priced), RETURN_NDIGITS)
        if priced
        else None,
        "median_return_pct": round(statistics.median(priced), RETURN_NDIGITS)
        if priced
        else None,
        "best_symbol": best["symbol"] if best else None,
        "best_return_pct": best["return_pct"] if best else None,
        "worst_symbol": worst["symbol"] if worst else None,
        "worst_return_pct": worst["return_pct"] if worst else None,
        "open_positions": len(open_rows),
        "open_priced": len(open_priced),
        "open_avg_return_pct": round(
            sum(open_priced) / len(open_priced), RETURN_NDIGITS
        )
        if open_priced
        else None,
        "cycle_return_pct": cycle_return,
        "cycle_days": len(daily),
        "cycle_priced_days": len(cycle_values),
        "cycle_win_days": sum(1 for value in cycle_values if value > 0),
        "cycle_loss_days": sum(1 for value in cycle_values if value < 0),
    }


def _sym_closes(
    closes: Mapping[str, Mapping[date, float]], symbol: str
) -> Mapping[date, float]:
    return closes.get(symbol) or {}


def trailing_adv(
    amounts: Mapping[str, Mapping[date, float]],
    symbol: str,
    entry_day: date,
    *,
    lookback_days: int = DEFAULT_ADV_LOOKBACK_DAYS,
    min_adv_days: int = DEFAULT_MIN_ADV_DAYS,
) -> float | None:
    """入选日（含）之前最近 ``lookback_days`` 个有效交易日的日均成交额（元）。

    有效日不足 ``min_adv_days`` 返回 ``None``（计划层按「成交额缺失」处理，
    绝不拿不足样本的均值冒充 ADV20）。``entry_day`` 之后的成交额一律不计入。
    """
    series = amounts.get(symbol) or {}
    valid = sorted(
        (day, float(value))
        for day, value in series.items()
        if day <= entry_day and _finite(value) and float(value) > 0
    )
    window = valid[-max(1, int(lookback_days)) :]
    if len(window) < max(1, int(min_adv_days)):
        return None
    return sum(value for _, value in window) / len(window)


def _entry_plan(
    *,
    picks: Sequence[str],
    entry_day: date,
    closes: Mapping[str, Mapping[date, float]],
    amounts: Mapping[str, Mapping[date, float]] | None,
    risk_flags: Mapping[str, str] | None,
    equity: float,
    tradable: bool,
    min_adv: float,
    participation: float,
    adv_lookback_days: int,
    min_adv_days: int,
) -> tuple[list[dict[str, Any]], float]:
    """当日批次的建仓计划：等权预算 → ST/退市、成交额、参与率逐项收紧。

    ``amounts=None`` 表示成交额不可用（等权回退，不按「缺失」跳过）；
    ``risk_flags=None`` 表示本轮不做 ST/退市过滤。返回 ``(plan, invested)``，
    ``invested`` 为计划建仓金额合计（元）。
    """
    budget = equity / len(picks) if picks else 0.0
    plan: list[dict[str, Any]] = []
    invested = 0.0
    for symbol in picks:
        item: dict[str, Any] = {
            "symbol": symbol,
            "status": STATUS_SKIP_NO_PRICE,
            "flag": None,
            "budget": budget,
            "value": 0.0,
            "weight": 0.0,
            "adv": None,
        }
        flag = (risk_flags or {}).get(symbol)
        if flag:
            item["status"] = STATUS_SKIP_ST
            item["flag"] = flag
            plan.append(item)
            continue
        price = _close(_sym_closes(closes, symbol).get(entry_day))
        if price is None:
            plan.append(item)
            continue
        adv: float | None = None
        if amounts is not None:
            adv = trailing_adv(
                amounts,
                symbol,
                entry_day,
                lookback_days=adv_lookback_days,
                min_adv_days=min_adv_days,
            )
            item["adv"] = adv
            if adv is None:
                item["status"] = STATUS_SKIP_NO_AMOUNT
                plan.append(item)
                continue
            if adv < min_adv:
                item["status"] = STATUS_SKIP_LOW_ADV
                plan.append(item)
                continue
        value = budget if adv is None else min(budget, adv * participation)
        if value <= 0:
            item["status"] = STATUS_SKIP_LOW_ADV
            plan.append(item)
            continue
        item["status"] = STATUS_BOUGHT if tradable else STATUS_NEXT_DAY
        item["weight"] = value / equity if equity > 0 else 0.0
        # 末日候选只给计划权重，不计实际建仓金额
        item["value"] = value if tradable else 0.0
        if tradable:
            invested += value
        plan.append(item)
    return plan, invested


def simulate_daily_rotation(
    *,
    days: Sequence[date],
    picks_by_day: Mapping[date, Sequence[str]],
    closes: Mapping[str, Mapping[date, float]],
    capital: float = DEFAULT_CAPITAL,
    benchmark_closes: Mapping[date, float] | None = None,
    amounts: Mapping[str, Mapping[date, float]] | None = None,
    risk_flags: Mapping[str, str] | None = None,
    min_adv_yi: float = DEFAULT_MIN_ADV_YI,
    adv_participation: float = DEFAULT_ADV_PARTICIPATION,
    adv_lookback_days: int = DEFAULT_ADV_LOOKBACK_DAYS,
    min_adv_days: int = DEFAULT_MIN_ADV_DAYS,
) -> dict[str, Any]:
    """每日换仓的组合账本（批次口径 + 可交易性约束）。

    - 每个标的先拿等权预算 ``权益 / 入选只数``；
    - ST/退市、缺入选价、缺 ADV20、ADV20 低于门槛的标的不建仓，**不替补**，
      相应资金留作现金；
    - 建仓金额 ``min(等权预算, ADV20 × 参与率)``；
    - 入选日收盘建仓，次一交易日收盘结算；最后一个批次只估值不结算，
      末日入选为下一批次候选（``next_day``，不建仓）；
    - 批次收益按建仓权重加权，未建仓现金形成拖累：
      ``Σ w_i·r_i ÷ Σ w_i × Σ w_i``（仅在可计价标的上取收益）；
    - ``amounts=None`` → 成交额不可用，等权回退（不静默套用容量约束）；
      ``risk_flags=None`` → 本轮不做 ST/退市过滤；
    - ``realized + unrealized == final_equity - initial_capital``（恒等式）。
    """
    ordered_days = list(days)
    if capital <= 0:
        raise ValueError("capital 必须为正数")
    if min_adv_yi < 0 or adv_participation <= 0:
        raise ValueError("成交额门槛必须非负，参与率必须为正数")
    if not ordered_days:
        return _empty_rotation(float(capital), benchmark_closes)

    min_adv = float(min_adv_yi) * YI
    equity = float(capital)
    equity_by_day: dict[date, float] = {ordered_days[0]: equity}
    batches: list[dict[str, Any]] = []
    daily: list[dict[str, Any]] = []
    entry_plans: dict[date, dict[str, dict[str, Any]]] = {}
    exposures: list[float] = [0.0]
    turnover_amount = 0.0
    plan_stats: dict[str, int] = {
        "evaluated_picks": 0,
        "bought_positions": 0,
        "skipped_st": 0,
        "skipped_low_adv": 0,
        "skipped_no_amount": 0,
        "skipped_no_price": 0,
    }

    def make_plan(
        entry_day: date, picks: list[str], current_equity: float, *, tradable: bool
    ) -> tuple[list[dict[str, Any]], float]:
        return _entry_plan(
            picks=picks,
            entry_day=entry_day,
            closes=closes,
            amounts=amounts,
            risk_flags=risk_flags,
            equity=current_equity,
            tradable=tradable,
            min_adv=min_adv,
            participation=float(adv_participation),
            adv_lookback_days=int(adv_lookback_days),
            min_adv_days=int(min_adv_days),
        )

    for index in range(len(ordered_days) - 1):
        entry_day = ordered_days[index]
        exit_day = ordered_days[index + 1]
        picks = _day_picks(picks_by_day, entry_day)
        equity_entry = equity
        plan, invested = make_plan(entry_day, picks, equity_entry, tradable=True)
        entry_plans[entry_day] = {item["symbol"]: item for item in plan}
        _accumulate_plan_stats(plan_stats, plan)

        weighted = 0.0
        priced_weight = 0.0
        priced_count = 0
        for item in plan:
            if item["status"] != STATUS_BOUGHT or item["weight"] <= 0:
                continue
            series = _sym_closes(closes, str(item["symbol"]))
            value = _return_pct(
                _close(series.get(entry_day)), _close(series.get(exit_day))
            )
            if value is None:
                continue
            priced_count += 1
            priced_weight += float(item["weight"])
            weighted += float(item["weight"]) * value

        invested_weight = invested / equity_entry if equity_entry > 0 else 0.0
        batch_return = (
            round(weighted / priced_weight * invested_weight, RETURN_NDIGITS)
            if priced_weight > 0
            else None
        )
        pnl_amount = (
            round(equity_entry * batch_return / 100.0, AMOUNT_NDIGITS)
            if batch_return is not None
            else None
        )
        if batch_return is not None:
            equity = equity_entry * (1.0 + batch_return / 100.0)

        cash_amount = max(0.0, equity_entry - invested)
        market_value = equity - cash_amount
        exposure = (invested / equity_entry * 100.0) if equity_entry > 0 else 0.0
        settled = index < len(ordered_days) - 2
        equity_by_day[exit_day] = equity
        if picks:
            batches.append(
                {
                    "entry_date": entry_day,
                    "exit_date": exit_day,
                    "positions": len(picks),
                    "bought": sum(
                        1 for item in plan if item["status"] == STATUS_BOUGHT
                    ),
                    "priced": priced_count,
                    "return_pct": batch_return,
                    "notional_amount": round(invested, AMOUNT_NDIGITS),
                    "pnl_amount": pnl_amount,
                    "equity": round(equity, AMOUNT_NDIGITS),
                    "settled": settled,
                    "holding_days": 1,
                }
            )
        if invested > 0:
            turnover_amount += invested
        exposures.append(round(exposure, 2))
        daily.append(
            {
                "date": entry_day,
                "exit_date": exit_day,
                "return_pct": batch_return,
                "equity": round(equity, AMOUNT_NDIGITS),
                "market_value": round(market_value, AMOUNT_NDIGITS),
                "cash": round(cash_amount, AMOUNT_NDIGITS),
                "exposure_pct": round(exposure, 2),
                "benchmark_return_pct": None,
                "excess_return_pct": None,
                "settled": settled,
            }
        )

    if ordered_days:
        last_day = ordered_days[-1]
        last_plan, _ = make_plan(
            last_day, _day_picks(picks_by_day, last_day), equity, tradable=False
        )
        entry_plans[last_day] = {item["symbol"]: item for item in last_plan}

    benchmark = metrics.benchmark_stats(ordered_days, benchmark_closes or {})
    benchmark_daily = benchmark["daily_returns"]
    for index, row in enumerate(daily):
        value = benchmark_daily[index + 1] if index + 1 < len(benchmark_daily) else None
        if value is None:
            continue
        row["benchmark_return_pct"] = round(value * 100.0, RETURN_NDIGITS)
        if row["return_pct"] is not None:
            row["excess_return_pct"] = round(
                float(row["return_pct"]) - value * 100.0, RETURN_NDIGITS
            )

    summary = _rotation_summary(
        ordered_days=ordered_days,
        capital=float(capital),
        equity_by_day=equity_by_day,
        batches=batches,
        daily=daily,
        benchmark=benchmark,
        exposures=exposures,
        turnover_amount=turnover_amount,
        plan_stats=plan_stats,
        capacity_applied=amounts is not None,
        st_filter_applied=risk_flags is not None,
        min_adv_yi=float(min_adv_yi),
        adv_participation=float(adv_participation),
        adv_lookback_days=int(adv_lookback_days),
        min_adv_days=int(min_adv_days),
    )
    return {
        "batches": batches,
        "daily": daily,
        "equity_by_day": equity_by_day,
        "entry_plans": entry_plans,
        "summary": summary,
    }


_SKIP_STAT_KEYS = {
    STATUS_SKIP_ST: "skipped_st",
    STATUS_SKIP_LOW_ADV: "skipped_low_adv",
    STATUS_SKIP_NO_AMOUNT: "skipped_no_amount",
    STATUS_SKIP_NO_PRICE: "skipped_no_price",
}


def _accumulate_plan_stats(
    stats: dict[str, int], plan: Sequence[Mapping[str, Any]]
) -> None:
    stats["evaluated_picks"] += len(plan)
    for item in plan:
        status = str(item.get("status"))
        if status == STATUS_BOUGHT:
            stats["bought_positions"] += 1
        elif status in _SKIP_STAT_KEYS:
            stats[_SKIP_STAT_KEYS[status]] += 1


def _empty_rotation(
    capital: float, benchmark_closes: Mapping[date, float] | None
) -> dict[str, Any]:
    benchmark = metrics.benchmark_stats([], benchmark_closes or {})
    return {
        "batches": [],
        "daily": [],
        "equity_by_day": {},
        "entry_plans": {},
        "summary": _rotation_summary(
            ordered_days=[],
            capital=capital,
            equity_by_day={},
            batches=[],
            daily=[],
            benchmark=benchmark,
            exposures=[],
            turnover_amount=0.0,
            plan_stats={
                "evaluated_picks": 0,
                "bought_positions": 0,
                "skipped_st": 0,
                "skipped_low_adv": 0,
                "skipped_no_amount": 0,
                "skipped_no_price": 0,
            },
            capacity_applied=False,
            st_filter_applied=False,
            min_adv_yi=DEFAULT_MIN_ADV_YI,
            adv_participation=DEFAULT_ADV_PARTICIPATION,
            adv_lookback_days=DEFAULT_ADV_LOOKBACK_DAYS,
            min_adv_days=DEFAULT_MIN_ADV_DAYS,
        ),
    }


def _rotation_summary(
    *,
    ordered_days: Sequence[date],
    capital: float,
    equity_by_day: Mapping[date, float],
    batches: Sequence[Mapping[str, Any]],
    daily: Sequence[Mapping[str, Any]],
    benchmark: Mapping[str, Any],
    exposures: Sequence[float],
    turnover_amount: float,
    plan_stats: Mapping[str, int],
    capacity_applied: bool,
    st_filter_applied: bool,
    min_adv_yi: float,
    adv_participation: float,
    adv_lookback_days: int,
    min_adv_days: int,
) -> dict[str, Any]:
    final_equity = (
        float(equity_by_day[ordered_days[-1]]) if ordered_days else float(capital)
    )
    settled_equity = (
        float(equity_by_day[ordered_days[-2]])
        if len(ordered_days) >= 2
        else float(capital)
    )
    realized = settled_equity - capital
    unrealized = final_equity - settled_equity
    total_return_pct = round((final_equity / capital - 1.0) * 100.0, RETURN_NDIGITS)

    equity_series = [float(equity_by_day[day]) for day in ordered_days]
    returns = metrics.daily_returns(equity_series)
    risk = metrics.risk_metrics(returns)
    drawdown = metrics.drawdown_stats(ordered_days, equity_series)
    excess = metrics.excess_metrics(returns, benchmark["daily_returns"])

    settled_batches = [batch for batch in batches if batch["settled"]]
    trade = metrics.trade_stats(
        [batch["return_pct"] for batch in settled_batches],
        [batch["pnl_amount"] for batch in settled_batches],
        [int(batch["holding_days"]) for batch in settled_batches],
    )

    coverage = benchmark["coverage_pct"]
    benchmark_ok = bool(
        coverage is not None and float(coverage) >= metrics.BENCHMARK_MIN_COVERAGE_PCT
    )
    benchmark_return = benchmark["return_pct"] if benchmark_ok else None
    excess_return = (
        round(total_return_pct - float(benchmark_return), RETURN_NDIGITS)
        if benchmark_return is not None
        else None
    )

    trading_days = max(0, len(ordered_days) - 1)
    annualized = metrics.annualized_return_pct(total_return_pct, trading_days)
    max_drawdown = drawdown["max_drawdown_pct"]
    calmar = (
        round(float(annualized) / abs(float(max_drawdown)), metrics.RATIO_NDIGITS)
        if annualized is not None and max_drawdown not in (None, 0)
        else None
    )
    cycle_values = [
        float(row["return_pct"]) for row in daily if row["return_pct"] is not None
    ]

    return {
        "initial_capital": round(capital, AMOUNT_NDIGITS),
        "final_equity": round(final_equity, AMOUNT_NDIGITS),
        "profit_amount": round(final_equity - capital, AMOUNT_NDIGITS),
        "total_return_pct": total_return_pct,
        "cycle_return_pct": total_return_pct,
        "realized_pnl_amount": round(realized, AMOUNT_NDIGITS),
        "realized_return_pct": round(realized / capital * 100.0, RETURN_NDIGITS),
        "unrealized_pnl_amount": round(unrealized, AMOUNT_NDIGITS),
        "unrealized_return_pct": round(unrealized / capital * 100.0, RETURN_NDIGITS),
        "benchmark_return_pct": benchmark_return,
        "benchmark_coverage_pct": coverage,
        "excess_return_pct": excess_return,
        "annualized_return_pct": annualized,
        "volatility_pct": risk["volatility_pct"],
        "sharpe_ratio": risk["sharpe_ratio"],
        "sortino_ratio": risk["sortino_ratio"],
        "calmar_ratio": calmar,
        "max_drawdown_pct": max_drawdown,
        "drawdown_peak_date": drawdown["peak_date"],
        "drawdown_trough_date": drawdown["trough_date"],
        "drawdown_recovery_date": drawdown["recovery_date"],
        "drawdown_recovery_days": drawdown["recovery_days"],
        "tracking_error_pct": excess["tracking_error_pct"] if benchmark_ok else None,
        "information_ratio": excess["information_ratio"] if benchmark_ok else None,
        "avg_daily_excess_pct": excess["avg_daily_excess_pct"]
        if benchmark_ok
        else None,
        "trades": trade["trades"],
        "wins": trade["wins"],
        "losses": trade["losses"],
        "flats": trade["flats"],
        "win_rate_pct": trade["win_rate_pct"],
        "avg_return_pct": trade["avg_return_pct"],
        "avg_win_pct": trade["avg_win_pct"],
        "avg_loss_pct": trade["avg_loss_pct"],
        "payoff_ratio": trade["payoff_ratio"],
        "profit_factor": trade["profit_factor"],
        "gross_profit_amount": trade["gross_profit_amount"],
        "gross_loss_amount": trade["gross_loss_amount"],
        "avg_holding_days": trade["avg_holding_days"],
        "turnover_x": round(turnover_amount / capital, metrics.RATIO_NDIGITS),
        "avg_exposure_pct": round(statistics.fmean(exposures), 2)
        if exposures
        else None,
        "final_cash_pct": (
            round(float(daily[-1]["cash"]) / final_equity * 100.0, 2)
            if daily and final_equity > 0
            else 100.0
        ),
        "trading_days": trading_days,
        "batches": len(batches),
        "settled_batches": len(settled_batches),
        "open_batches": len(batches) - len(settled_batches),
        "priced_batches": sum(
            1 for batch in batches if batch["return_pct"] is not None
        ),
        # 缺价/部分缺价只在**实际建仓**的标的上判定：被 ST/成交额门槛跳过的标的
        # 本来就不建仓（bought=0），不能把它们算成「缺价」。
        "unpriced_batches": sum(
            1
            for batch in batches
            if batch["return_pct"] is None and int(batch.get("bought") or 0) > 0
        ),
        "partial_batches": sum(
            1
            for batch in batches
            if 0 < batch["priced"] < int(batch.get("bought") or 0)
        ),
        "no_position_batches": sum(
            1 for batch in batches if int(batch.get("bought") or 0) == 0
        ),
        "cycle_days": len(daily),
        "cycle_priced_days": len(cycle_values),
        "cycle_win_days": sum(1 for value in cycle_values if value > 0),
        "cycle_loss_days": sum(1 for value in cycle_values if value < 0),
        "entry_convention": "close",
        # 可交易性口径：是否实际应用容量/ST 过滤 + 建仓覆盖与跳过明细
        "capacity_applied": bool(capacity_applied),
        "st_filter_applied": bool(st_filter_applied),
        "evaluated_picks": int(plan_stats.get("evaluated_picks") or 0),
        "bought_positions": int(plan_stats.get("bought_positions") or 0),
        "skipped_st": int(plan_stats.get("skipped_st") or 0),
        "skipped_low_adv": int(plan_stats.get("skipped_low_adv") or 0),
        "skipped_no_amount": int(plan_stats.get("skipped_no_amount") or 0),
        "skipped_no_price": int(plan_stats.get("skipped_no_price") or 0),
        "min_adv_yi": round(float(min_adv_yi), 4),
        "adv_participation": float(adv_participation),
        "adv_lookback_days": int(adv_lookback_days),
        "min_adv_days": int(min_adv_days),
    }


def cell_stats(
    days: Sequence[date],
    picks_by_day: Mapping[date, Sequence[str]],
    closes: Mapping[str, Mapping[date, float]],
    *,
    equity_by_day: Mapping[date, float] | None = None,
    entry_plans: Mapping[date, Mapping[str, Mapping[str, Any]]] | None = None,
) -> dict[str, Any]:
    """每只标的的窗口内表现 + 每个格子的当日涨跌幅/当日盈亏。

    - ``by_symbol``：在榜天数、当前连续在榜、首次入选、首次入选至今涨跌、
      在榜日次日收益均值/胜率/样本/标准差、末日收盘与当日涨跌；
    - ``by_day``：日期 × 标的 → 当日涨跌幅、当日持仓权重与当日盈亏金额
      （仅统计前一交易日计划建仓、即当日实际持有的标的；未持有为 ``None``），
      另带当日计划的建仓状态/计划权重/ADV20（``buy_status`` /
      ``entry_weight_pct`` / ``adv_yi``）；
    - 不传 ``entry_plans`` 时退回旧的等权口径（前一日有价标的均分）。
    """
    ordered_days = list(days)
    last_day = ordered_days[-1] if ordered_days else None
    appearances: dict[str, list[date]] = {}
    for day in ordered_days:
        for symbol in _day_picks(picks_by_day, day):
            appearances.setdefault(symbol, []).append(day)

    streak: dict[str, int] = {}
    for day in ordered_days:
        today = set(_day_picks(picks_by_day, day))
        for symbol in today:
            streak[symbol] = streak.get(symbol, 0) + 1
        streak = {symbol: count for symbol, count in streak.items() if symbol in today}

    by_symbol: dict[str, dict[str, Any]] = {}
    for symbol, days_in_list in appearances.items():
        series = _sym_closes(closes, symbol)
        listed = set(days_in_list)
        next_returns: list[float] = []
        for index in range(len(ordered_days) - 1):
            day = ordered_days[index]
            if day not in listed:
                continue
            value = _return_pct(
                _close(series.get(day)), _close(series.get(ordered_days[index + 1]))
            )
            if value is not None:
                next_returns.append(value)
        first_day = days_in_list[0]
        latest_close = _close(series.get(last_day)) if last_day is not None else None
        latest_change = (
            metrics.change_pct(series, last_day) if last_day is not None else None
        )
        by_symbol[symbol] = {
            "appearances": len(days_in_list),
            "streak_days": streak.get(symbol, 0),
            "first_seen": first_day,
            "since_first_pct": _return_pct(_close(series.get(first_day)), latest_close),
            "next_day_avg_pct": round(statistics.fmean(next_returns), RETURN_NDIGITS)
            if next_returns
            else None,
            "next_day_win_rate_pct": round(
                sum(1 for value in next_returns if value > 0)
                / len(next_returns)
                * 100.0,
                WIN_RATE_NDIGITS,
            )
            if next_returns
            else None,
            "next_day_samples": len(next_returns),
            "next_day_std_pct": round(statistics.stdev(next_returns), RETURN_NDIGITS)
            if len(next_returns) >= 3
            else None,
            "latest_close": round(latest_close, RETURN_NDIGITS)
            if latest_close is not None
            else None,
            "change_pct": round(latest_change, RETURN_NDIGITS)
            if latest_change is not None
            else None,
        }

    by_day: dict[date, dict[str, dict[str, Any]]] = {day: {} for day in ordered_days}
    for index, day in enumerate(ordered_days):
        picks = _day_picks(picks_by_day, day)
        previous_day = ordered_days[index - 1] if index > 0 else None
        previous_picks = (
            _day_picks(picks_by_day, previous_day) if previous_day is not None else []
        )
        previous_equity = (
            float(equity_by_day[previous_day])
            if equity_by_day is not None and previous_day in equity_by_day
            else None
        )
        priced_previous: list[str] = []
        if previous_day is not None:
            priced_previous = [
                symbol
                for symbol in previous_picks
                if _close(_sym_closes(closes, symbol).get(previous_day)) is not None
                and _close(_sym_closes(closes, symbol).get(day)) is not None
            ]
        previous_set = set(previous_picks)
        previous_plan = (
            entry_plans.get(previous_day)
            if entry_plans is not None and previous_day is not None
            else None
        )
        today_plan = (entry_plans or {}).get(day) or {}
        for symbol in picks:
            series = _sym_closes(closes, symbol)
            change = metrics.change_pct(series, day)
            plan_entry = today_plan.get(symbol) or {}
            plan_weight = plan_entry.get("weight")
            plan_adv = plan_entry.get("adv")
            cell: dict[str, Any] = {
                "change_pct": round(change, RETURN_NDIGITS)
                if change is not None
                else None,
                "weight_pct": None,
                "pnl_amount": None,
                "buy_status": plan_entry.get("status"),
                "entry_weight_pct": round(float(plan_weight) * 100.0, RETURN_NDIGITS)
                if plan_weight is not None
                else None,
                "adv_yi": round(float(plan_adv) / YI, RETURN_NDIGITS)
                if plan_adv is not None
                else None,
            }
            value = (
                _return_pct(_close(series.get(previous_day)), _close(series.get(day)))
                if previous_day is not None
                else None
            )
            if (
                previous_day is not None
                and symbol in previous_set
                and previous_equity is not None
                and value is not None
            ):
                if entry_plans is not None:
                    held = (previous_plan or {}).get(symbol) or {}
                    weight = float(held.get("weight") or 0.0)
                    if held.get("status") == STATUS_BOUGHT and weight > 0:
                        cell["weight_pct"] = round(weight * 100.0, RETURN_NDIGITS)
                        cell["pnl_amount"] = round(
                            previous_equity * weight * value / 100.0, AMOUNT_NDIGITS
                        )
                elif priced_previous and symbol in priced_previous:
                    weight = 1.0 / len(priced_previous)
                    cell["weight_pct"] = round(weight * 100.0, RETURN_NDIGITS)
                    cell["pnl_amount"] = round(
                        previous_equity * weight * value / 100.0, AMOUNT_NDIGITS
                    )
            by_day[day][symbol] = cell

    return {"by_symbol": by_symbol, "by_day": by_day}
