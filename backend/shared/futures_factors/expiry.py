"""合约到期日规则（交易所口径）与交易日历。

只做纯计算：日历由调用方从行情日期或交易日历文件构造，本模块不读文件。

交易所最后交易日规则（2026 年口径）：

===========  ==========================================================
交易所        规则
===========  ==========================================================
SHFE         交割月份 15 日，遇节假日/周末顺延到下一交易日
INE          原油/低硫燃料油/20 号胶/国际铜：交割月前一月最后交易日
             集运指数 EC：交割月最后交易日
DCE/CZCE     交割月份第 10 个交易日
GFEX         交割月份第 10 个交易日
CFFEX        股指（IF/IH/IC/IM）：交割月第三个周五
             国债（TS/TF/T/TL）：交割月第二个周五
===========  ==========================================================

规则只用于**估算**历史合约的到期日；有交易所公布的准确日期时用
``overrides`` 覆盖（键为 ``(品种, 年, 月)``）。
"""

from __future__ import annotations

import re
from bisect import bisect_left
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import NamedTuple

__all__ = [
    "ContractCode",
    "TradingCalendar",
    "estimate_last_trade_date",
    "last_trading_day_of_month",
    "next_trading_day",
    "nth_trading_day",
    "parse_contract_code",
    "third_friday",
]

_CONTRACT_RE = re.compile(r"^([A-Za-z]+)(\d{3,4})$")
_SUFFIX_RE = re.compile(r"\.[A-Za-z]+$")
_TREASURY = frozenset({"TS", "TF", "T", "TL"})


class ContractCode(NamedTuple):
    """合约代码拆解结果。"""

    variety: str
    year: int
    month: int

    @property
    def yymm(self) -> str:
        return f"{self.year % 100:02d}{self.month:02d}"


def _as_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value[:10])
    # pandas.Timestamp / numpy.datetime64 都有 date()
    as_date = getattr(value, "date", None)
    if callable(as_date):
        return as_date()
    raise TypeError(f"无法转换为日期: {value!r}")


@dataclass(frozen=True)
class TradingCalendar:
    """有序、去重的交易日集合，附带月份边界查询。"""

    dates: tuple[date, ...]

    def __post_init__(self) -> None:
        normalized = tuple(sorted({_as_date(d) for d in self.dates}))
        object.__setattr__(self, "dates", normalized)

    @classmethod
    def from_dates(cls, values: Iterable[object]) -> TradingCalendar:
        return cls(tuple(_as_date(v) for v in values))

    # ---- 单日查询 ----

    def is_trading_day(self, day: date) -> bool:
        day = _as_date(day)
        idx = bisect_left(self.dates, day)
        return idx < len(self.dates) and self.dates[idx] == day

    def next_trading_day(self, day: date) -> date:
        """返回 >= day 的第一个交易日；超出日历范围时按工作日回退。"""
        day = _as_date(day)
        if not self.dates or day > self.dates[-1] or day < self.dates[0]:
            return _next_weekday(day)
        idx = bisect_left(self.dates, day)
        return self.dates[idx]

    # ---- 月份查询 ----

    def _month_dates(self, year: int, month: int) -> list[date]:
        return [d for d in self.dates if d.year == year and d.month == month]

    def nth_trading_day(self, year: int, month: int, n: int) -> date:
        """交割月第 n 个交易日（1 起）；日历未覆盖该月时按工作日回退。"""
        if n < 1:
            raise ValueError("n 必须 >= 1")
        days = self._month_dates(year, month)
        if len(days) >= n:
            return days[n - 1]
        fallback = _nth_weekday(year, month, n)
        if fallback is None:
            raise ValueError(f"{year}-{month:02d} 没有第 {n} 个交易日")
        return fallback

    def last_trading_day_of_month(self, year: int, month: int) -> date:
        days = self._month_dates(year, month)
        if days:
            return days[-1]
        fallback = _last_weekday(year, month)
        if fallback is None:  # pragma: no cover - 每月必有工作日
            raise ValueError(f"{year}-{month:02d} 没有交易日")
        return fallback

    def days_to_month_end(self, day: date) -> int:
        """day 之后、同一自然月内还剩多少个交易日（不含 day）。"""
        day = _as_date(day)
        days = self._month_dates(day.year, day.month)
        if days:
            return sum(1 for d in days if d > day)
        end = _last_weekday(day.year, day.month)
        if end is None:  # pragma: no cover
            return 0
        return sum(
            1
            for offset in range(1, (end - day).days + 1)
            if (day + timedelta(days=offset)).weekday() < 5
        )


def _next_weekday(day: date) -> date:
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def _nth_weekday(year: int, month: int, n: int) -> date | None:
    day = date(year, month, 1)
    seen = 0
    while day.month == month:
        if day.weekday() < 5:
            seen += 1
            if seen == n:
                return day
        day += timedelta(days=1)
    return None


def _last_weekday(year: int, month: int) -> date | None:
    if month == 12:
        day = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        day = date(year, month + 1, 1) - timedelta(days=1)
    while day.month == month:
        if day.weekday() < 5:
            return day
        day -= timedelta(days=1)
    return None  # pragma: no cover


def _nth_weekday_of_month(year: int, month: int, weekday: int, n: int) -> date:
    """某月第 n 个指定星期几（weekday: 周一=0）。"""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def third_friday(year: int, month: int) -> date:
    return _nth_weekday_of_month(year, month, 4, 3)


def _second_friday(year: int, month: int) -> date:
    return _nth_weekday_of_month(year, month, 4, 2)


def parse_contract_code(symbol: str, ref_date: date | None = None) -> ContractCode:
    """把 ``CU2706.CN`` / ``AP610`` 拆成品种与交割年月。

    ``ref_date`` 只用于三位月份代码（郑商所旧格式）的世纪推断。
    """
    stem = _SUFFIX_RE.sub("", str(symbol).strip())
    match = _CONTRACT_RE.match(stem)
    if not match:
        raise ValueError(f"无法解析合约代码: {symbol!r}")
    variety = match.group(1).upper()
    digits = match.group(2)
    if len(digits) == 4:
        year = 2000 + int(digits[:2])
        month = int(digits[2:])
    else:
        year_digit = int(digits[0])
        month = int(digits[1:])
        if ref_date is None:
            year = 2020 + year_digit
        else:
            ref = _as_date(ref_date)
            year = ref.year - (ref.year % 10) + year_digit
            if (year, month) < (ref.year - 1, ref.month):
                year += 10
    if not 1 <= month <= 12:
        raise ValueError(f"合约月份非法: {symbol!r}")
    return ContractCode(variety=variety, year=year, month=month)


def _previous_month(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def estimate_last_trade_date(
    variety: str,
    year: int,
    month: int,
    calendar: TradingCalendar,
    exchange: str | None = None,
    overrides: dict[tuple[str, int, int], date] | None = None,
) -> date:
    """按交易所规则估算最后交易日；``overrides`` 优先。"""
    variety = variety.upper()
    if overrides:
        exact = overrides.get((variety, year, month))
        if exact is not None:
            return _as_date(exact)

    venue = (exchange or "").upper()
    if venue == "CFFEX":
        friday = (
            _second_friday(year, month)
            if variety in _TREASURY
            else third_friday(year, month)
        )
        return calendar.next_trading_day(friday)
    if venue in {"DCE", "CZCE", "GFEX"}:
        return calendar.nth_trading_day(year, month, 10)
    if venue == "INE":
        if variety == "EC":
            return calendar.last_trading_day_of_month(year, month)
        return calendar.last_trading_day_of_month(*_previous_month(year, month))
    # SHFE 及未知交易所走 15 日规则
    return calendar.next_trading_day(date(year, month, 15))


# 便捷包装（测试与脚本层用）
def next_trading_day(day: date, calendar: TradingCalendar) -> date:
    return calendar.next_trading_day(day)


def nth_trading_day(year: int, month: int, n: int, calendar: TradingCalendar) -> date:
    return calendar.nth_trading_day(year, month, n)


def last_trading_day_of_month(year: int, month: int, calendar: TradingCalendar) -> date:
    return calendar.last_trading_day_of_month(year, month)
