"""由 5s pricevol 采样实时聚合 1m OHLCV。

为什么不用 get_market_data(1m)：2026-10-22 实测 1m/5m 返回空（客户端分钟数据包
未装或该接口不支持），而 pricevol 全市场 5,581 只一次 2.7–3.4s 可用。这里用
12 个采样点/分钟聚合 OHLC，成交量用累计 Volume 差分；成交额不在 pricevol 里，
保持 None（下游必须按“缺失”处理，不得当 0）。

分钟标签 = 采样所在的墙钟分钟（CST）。09:30:05 的采样归入 09:30 柱。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class _BarState:
    trade_date: str
    minute: str
    open: float
    high: float
    low: float
    close: float
    volume_baseline: float | None
    last_volume: float | None
    samples: int


class BarAggregator:
    """单进程、单线程使用（采集器事件循环内），无锁。"""

    def __init__(self) -> None:
        self._states: dict[str, _BarState] = {}
        self._prev_volume: dict[str, float] = {}
        self._day = ""

    def on_sample(self, code: str, price: float,
                  cumulative_volume: float | None = None,
                  now: datetime | None = None) -> list[dict]:
        """喂一个采样点；跨分钟时返回上一根已封口的 bar（0 或 1 根）。"""
        now = now or datetime.now()
        if price <= 0:
            return []
        minute = now.strftime("%H%M")
        trade_date = now.strftime("%Y%m%d")
        if trade_date != self._day:
            # 跨日：昨日收盘累计量不能当今日基线，否则新一天首根量恒为 0
            self._prev_volume.clear()
            self._day = trade_date
        bars: list[dict] = []
        state = self._states.get(code)
        if state is not None and (state.minute != minute or state.trade_date != trade_date):
            bars.append(self._finalize(code, state))
            state = None
        if state is None:
            prev = self._prev_volume.get(code)
            if prev is None:
                # 首次采样：开盘第一分钟用 0 作基线；盘中启动则用当前累计量，
                # 避免把开盘至今的成交量全部灌进第一根 bar。
                baseline = 0.0 if minute <= "0931" else cumulative_volume
            else:
                baseline = prev
            state = _BarState(
                trade_date=trade_date,
                minute=minute,
                open=price,
                high=price,
                low=price,
                close=price,
                volume_baseline=baseline,
                last_volume=cumulative_volume,
                samples=1,
            )
            self._states[code] = state
        else:
            state.high = max(state.high, price)
            state.low = min(state.low, price)
            state.close = price
            state.samples += 1
            if cumulative_volume is not None:
                state.last_volume = cumulative_volume
        if cumulative_volume is not None:
            self._prev_volume[code] = cumulative_volume
        return bars

    def flush_stale(self, now: datetime | None = None) -> list[dict]:
        """把已经不属于当前分钟的 bar 全部封口（采集器每 5s 调一次）。"""
        now = now or datetime.now()
        cur_minute = now.strftime("%H%M")
        today = now.strftime("%Y%m%d")
        out: list[dict] = []
        for code, state in list(self._states.items()):
            if state.trade_date != today or state.minute != cur_minute:
                out.append(self._finalize(code, state))
                del self._states[code]
        return out

    def flush_all(self) -> list[dict]:
        out = [self._finalize(code, state) for code, state in self._states.items()]
        self._states.clear()
        return out

    @staticmethod
    def _finalize(code: str, state: _BarState) -> dict:
        volume = None
        if state.last_volume is not None and state.volume_baseline is not None:
            volume = max(state.last_volume - state.volume_baseline, 0.0)
        return {
            "trade_date": state.trade_date,
            "code": code,
            "minute": state.minute,
            "open": round(state.open, 4),
            "high": round(state.high, 4),
            "low": round(state.low, 4),
            "close": round(state.close, 4),
            "volume": volume,
            "amount": None,
            "samples": state.samples,
            "source": "pricevol",
        }

    @property
    def open_states(self) -> int:
        return len(self._states)
