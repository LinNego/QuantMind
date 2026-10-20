"""实时分钟 K 线口径唯一实现（1m OHLCV + 会话对齐的 5m/15m/30m/60m 聚合）。

**数据来源**：``market:series:{PREFIX}``（ZSET，score=epoch 秒，member=JSON 快照）。
写侧有三家，字段契约一致（symbol/timestamp/price/volume/amount/source）：

- ``tdx_bridge``      通达信桥轮询（手 / 万元，累计值）
- ``tdx_aidata_sub``  TdxAiData 订阅帧（手 / 万元，累计值）
- ``qmt_big``         QMT 备源席（QMT 口径；切换源时本模块按 source 变化重置基线）

**本模块只做纯计算**（无 I/O、无 Redis、无时钟依赖，入参显式给出）：
分桶、会话分钟映射、跨源/跨日基线处理、1m→多周期聚合。任务层与 API 层共用，
禁止在别处再写一份分钟口径。

**A 股会话口径**（``Asia/Shanghai``）：

    09:30–11:30, 13:00–15:00；共 240 个 1m 桶。
    09:30 之前的样本（集合竞价）并入 09:30 桶；
    11:30:00–11:30:59 并入 11:29 桶；午休样本丢弃；
    15:00 之后的样本（收盘竞价落点）并入 14:59 桶。

**Bar 标签 = 区间起点**（09:30 的第一根覆盖 09:30:00–09:30:59，含开盘竞价量）。
``ts`` 是 epoch 秒，展示用 ``time``（HH:MM）由 ``ts`` 派生，二者不独立生成。

**量的口径**：快照里的 ``volume``/``amount`` 是**当日累计值**，Bar 的量取相邻样本
差分。以下三种情况差分不可用，Bar 的 ``volume`` 置 ``None`` 并标 ``volume_unknown``
（**绝不把累计值写进单分钟 Bar 冒充分钟量**）：

1. 同一交易日内 ``source`` 变化（不同源单位可能不同，如手 vs 股）；
2. 累计值倒挂（数据重置/重连）；
3. 缺字段。

跨交易日**不是**不可用：新交易日的累计值从 0 开始，首个样本的累计值即当日开盘
至该样本的成交量，归入首个样本所在 Bar 并标 ``from_open=True``。

**质量字段**（消费方必须看，禁止把稀疏样本当完整分钟线）：

- ``samples``：落进该 Bar 的快照数；
- ``span_s``：该 Bar 覆盖的**会话内秒数**（已扣除午休/隔夜），等于 60 才是一根
  被完整采样的分钟；远大于 60 说明样本稀疏，量是跨分钟累计的差分；
- ``complete``：该分钟是否已经结束（最后一根进行中的 Bar 为 ``False``）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import date as date_cls
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

CST = ZoneInfo("Asia/Shanghai")

#: 支持的周期 → 分钟数
PERIOD_MINUTES: dict[str, int] = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60}
#: 输入别名 → 规范周期键（对外暴露 1h，内部统一按 60m 分桶）
PERIOD_ALIASES: dict[str, str] = {"1h": "60m"}
DEFAULT_PERIOD = "1m"

#: 会话边界（秒 since 00:00:00）
_MORNING_OPEN_S = 9 * 3600 + 30 * 60  # 09:30
_MORNING_CLOSE_S = 11 * 3600 + 30 * 60  # 11:30
_AFTERNOON_OPEN_S = 13 * 3600  # 13:00
_SESSION_CLOSE_S = 15 * 3600  # 15:00
_SESSION_MINUTES = 240

#: 会话内推进的秒数（09:30–11:30 与 13:00–15:00 各 7200s）
_SESSION_SECONDS = 14400

#: 源累计量连续不变超过该秒数后，后续零差分不再可信：零差分只说明「源没动」，
#: 不说明「这一分钟没成交」。2026-10-20 实测桥的 Volume 对 SH600036 冻结 7 分钟
#: （13:43:37→13:50:35 恒 484255），期间订阅源累计已增加约 8k 股。把这种
#: 差分记成 0 会凭空造出「零成交分钟」；记成未知才符合事实。
_ZERO_DELTA_TRUST_S = 60


def to_cst(ts: float | int) -> datetime:
    """epoch 秒 → 上海时区 aware datetime。"""
    return datetime.fromtimestamp(int(ts), tz=CST)


def date_str(ts: float | int) -> str:
    """epoch 秒 → ``YYYY-MM-DD``（上海口径）。"""
    return to_cst(ts).strftime("%Y-%m-%d")


def time_str(ts: float | int) -> str:
    """epoch 秒 → ``HH:MM``（上海口径，Bar 起点）。"""
    return to_cst(ts).strftime("%H:%M")


def _sec_of_day(dt: datetime) -> int:
    return dt.hour * 3600 + dt.minute * 60 + dt.second


def session_seconds_of_day(dt: datetime) -> int:
    """会话内秒数：09:30=0，11:30=7200，13:00=7200，15:00=14400。

    集合竞价（<09:30）映射 0；午休映射 7200（不产生时间推进）；收盘后映射 14400。
    """
    s = _sec_of_day(dt)
    if s < _MORNING_OPEN_S:
        return 0
    if s < _MORNING_CLOSE_S:
        return s - _MORNING_OPEN_S
    if s < _AFTERNOON_OPEN_S:
        return 7200
    if s < _SESSION_CLOSE_S:
        return 7200 + (s - _AFTERNOON_OPEN_S)
    return _SESSION_SECONDS


def session_seconds_between(prev_ts: float | int, ts: float | int) -> int:
    """两个时刻之间经过的**会话内**秒数（扣除午休与隔夜）。

    跨日时退化为「当天自开盘起的会话秒数」——调用方在交易日切换时会先重置
    基线，本函数只保证不返回负数或跨日巨值。
    """
    prev = to_cst(prev_ts)
    cur = to_cst(ts)
    if prev.date() != cur.date():
        return session_seconds_of_day(cur)
    return max(0, session_seconds_of_day(cur) - session_seconds_of_day(prev))


def bar_bucket(ts: float | int) -> int | None:
    """样本 epoch → 所属 1m Bar 起点 epoch（会话边界归并见模块 docstring）。

    返回 ``None`` 表示落在午休（丢弃）。
    """
    dt = to_cst(ts)
    sec = _sec_of_day(dt)
    day_start = int(dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    if sec < _MORNING_OPEN_S:
        return day_start + _MORNING_OPEN_S
    if sec < _MORNING_CLOSE_S:
        return ts - ts % 60
    if sec < _MORNING_CLOSE_S + 60:
        return day_start + _MORNING_CLOSE_S - 60  # 11:29 桶
    if sec < _AFTERNOON_OPEN_S:
        return None
    if sec < _SESSION_CLOSE_S:
        return ts - ts % 60
    return day_start + _SESSION_CLOSE_S - 60  # 14:59 桶


def session_index(ts: float | int) -> tuple[str, int] | None:
    """Bar 起点 epoch → ``(交易日, 会话分钟下标 0..239)``；午休/非交易日返回 None。"""
    dt = to_cst(ts)
    sec = _sec_of_day(dt)
    day = dt.strftime("%Y-%m-%d")
    if _MORNING_OPEN_S <= sec < _MORNING_CLOSE_S:
        return day, (sec - _MORNING_OPEN_S) // 60
    if _AFTERNOON_OPEN_S <= sec < _SESSION_CLOSE_S:
        return day, 120 + (sec - _AFTERNOON_OPEN_S) // 60
    return None


def index_to_epoch(day: str, idx: int) -> int:
    """``(交易日, 会话分钟下标)`` → Bar 起点 epoch 秒。"""
    y, m, d = (int(x) for x in day.split("-"))
    base = datetime(y, m, d, tzinfo=CST)
    if idx < 120:
        return int(base.replace(hour=9, minute=30).timestamp()) + idx * 60
    return int(base.replace(hour=13, minute=0).timestamp()) + (idx - 120) * 60


def _num(value: Any) -> float | None:
    """容错数值转换；空值/非法值/NaN 一律 None。"""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None


@dataclass(frozen=True)
class Sample:
    """一根快照样本（来自 ``market:series`` 的 JSON member）。"""

    ts: int
    price: float | None
    volume: float | None = None
    amount: float | None = None
    source: str = ""


@dataclass(frozen=True)
class Bar:
    """1m Bar（``period`` 聚合后字段含义不变，仅跨度不同）。"""

    symbol: str
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float | None
    amount: float | None
    samples: int
    span_s: int
    source: str
    complete: bool
    from_open: bool = False
    volume_unknown: bool = False

    @property
    def date(self) -> str:
        return date_str(self.ts)

    @property
    def time(self) -> str:
        return time_str(self.ts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "ts": self.ts,
            "date": self.date,
            "time": self.time,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "amount": self.amount,
            "samples": self.samples,
            "span_s": self.span_s,
            "source": self.source,
            "complete": self.complete,
            "from_open": self.from_open,
            "volume_unknown": self.volume_unknown,
        }


def sample_from_payload(
    payload: dict[str, Any], *, score: float | int | None = None
) -> Sample | None:
    """``market:series`` JSON → :class:`Sample`；价格缺失或时间非法返回 None。"""
    ts_raw = payload.get("timestamp", score)
    try:
        ts = int(float(ts_raw))
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    price = _num(payload.get("price"))
    if price is None or price <= 0:
        return None
    return Sample(
        ts=ts,
        price=price,
        volume=_num(payload.get("volume")),
        amount=_num(payload.get("amount")),
        source=str(payload.get("source") or ""),
    )


class MinuteBarAccumulator:
    """单标的增量聚合器：喂样本，吐**已定稿**的 Bar。

    纯内存、确定性；重启后从当日 ``market:series`` 重放即可得到同一组 Bar
    （任务层因此不需要持久化状态）。
    """

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self._day: str | None = None
        self._last_ts: int | None = None
        self._last_volume: float | None = None
        self._last_amount: float | None = None
        #: 最近一次「累计量确实前进」的样本时间（判零差分是否可信）
        self._last_vol_advance_ts: int | None = None
        self._last_amt_advance_ts: int | None = None
        self._last_source: str | None = None
        self._bar: Bar | None = None
        self._bar_volume: float | None = None
        self._bar_amount: float | None = None
        self._bar_volume_bad = False
        self._bar_amount_bad = False

    @property
    def current(self) -> Bar | None:
        """进行中的 Bar 快照（``complete=False``，量按已确认差分实时计算）。"""
        return self.snapshot()

    def snapshot(self) -> Bar | None:
        bar = self._bar
        if bar is None:
            return None
        return replace(
            bar,
            volume=None if self._bar_volume_bad else self._bar_volume,
            amount=None if self._bar_amount_bad else self._bar_amount,
            volume_unknown=self._bar_volume_bad,
            complete=False,
        )

    @staticmethod
    def _is_stale_zero(ts: int, last_advance_ts: int | None) -> bool:
        """零差分是否发生在「源累计已长时间不动」之后（不可信，按未知处理）。"""
        return (
            last_advance_ts is not None and ts - last_advance_ts > _ZERO_DELTA_TRUST_S
        )

    def _start_bar(self, ts: int, price: float, *, source: str) -> None:
        self._bar = Bar(
            symbol=self.symbol,
            ts=ts,
            open=price,
            high=price,
            low=price,
            close=price,
            volume=0.0,
            amount=0.0,
            samples=0,
            span_s=0,
            source=source,
            complete=False,
        )
        self._bar_volume = 0.0
        self._bar_amount = 0.0
        self._bar_volume_bad = False
        self._bar_amount_bad = False

    def _finalize(self) -> Bar | None:
        bar = self._bar
        if bar is None:
            return None
        finalized = Bar(
            symbol=bar.symbol,
            ts=bar.ts,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=None if self._bar_volume_bad else self._bar_volume,
            amount=None if self._bar_amount_bad else self._bar_amount,
            samples=bar.samples,
            span_s=bar.span_s,
            source=bar.source,
            complete=True,
            from_open=bar.from_open,
            volume_unknown=self._bar_volume_bad,
        )
        self._bar = None
        return finalized

    def flush(self) -> Bar | None:
        """会话结束/停机终刷：把进行中的 Bar 定稿。"""
        return self._finalize()

    def feed(self, sample: Sample) -> Bar | None:
        """喂一个样本；若触发分钟/交易日切换，返回**上一根**定稿 Bar。

        同一分钟内 ``source`` 变化**不切 Bar**（否则同一 ``ts`` 会产出两根、写
        ZSET 互相覆盖）：价格 OHLC 继续累计，量的基线重置并把该 Bar 的
        ``volume`` 置 ``None``（跨源单位不可加）。
        """
        bucket = bar_bucket(sample.ts)
        if bucket is None or sample.price is None:
            return None
        if self._bar is not None and bucket < self._bar.ts:
            return None  # 迟到的旧样本：不回溯改写已定稿 Bar

        day = date_str(sample.ts)
        source_changed = bool(
            self._last_source and sample.source and sample.source != self._last_source
        )
        day_changed = bool(self._day and day != self._day)
        bar_changed = bool(self._bar and bucket != self._bar.ts)

        finalized: Bar | None = None
        if day_changed or bar_changed:
            finalized = self._finalize()

        if day_changed:
            # 新交易日：累计量从 0 开始，首个样本的累计值=开盘至今
            self._last_volume = None
            self._last_amount = None
            self._last_vol_advance_ts = None
            self._last_amt_advance_ts = None
            self._bar_volume_bad = False
            self._bar_amount_bad = False

        # 进程/标的首次出现：若落在开盘头几分钟，累计值≈开盘至今，可安全归入首根；
        # 若在盘中才首次出现（series 被截断/中途启动），累计值无法拆分到分钟 → 置未知。
        first_ever = self._last_ts is None
        first_at_open = first_ever and session_seconds_of_day(to_cst(sample.ts)) <= 300
        if self._bar is None:
            self._start_bar(bucket, sample.price, source=sample.source)
            if day_changed or first_at_open:
                # 当日首根：累计值本身就是开盘至今的量
                assert self._bar is not None
                self._bar = replace(self._bar, from_open=True)
                span = session_seconds_between(index_to_epoch(day, 0), sample.ts)
            elif self._last_ts is not None:
                # 新 Bar 的首个样本：本根量是「上一根末样本 → 本样本」的差分，
                # 覆盖秒数必须含跨 Bar 的间隔（样本稀疏时 span_s 会 >60）
                span = session_seconds_between(self._last_ts, sample.ts)
            else:
                # 当日首次出现在盘中：累计量无法拆分，不宣称覆盖
                span = 0
        else:
            span = session_seconds_between(self._last_ts or sample.ts, sample.ts)

        # ── 累计量差分 ──────────────────────────────────────────────
        volume_delta: float | None = None
        amount_delta: float | None = None
        if day_changed or first_at_open:
            volume_delta = sample.volume
            amount_delta = sample.amount
            if sample.volume is not None and sample.volume > 0:
                self._last_vol_advance_ts = sample.ts
            if sample.amount is not None and sample.amount > 0:
                self._last_amt_advance_ts = sample.ts
        elif source_changed:
            volume_delta = None
            amount_delta = None
            # 新源的累计基线从本样本起算，前进历史不可继承
            self._last_vol_advance_ts = None
            self._last_amt_advance_ts = None
        else:
            if sample.volume is not None and self._last_volume is not None:
                diff = sample.volume - self._last_volume
                if diff < 0:
                    volume_delta = None
                elif diff == 0 and self._is_stale_zero(
                    sample.ts, self._last_vol_advance_ts
                ):
                    volume_delta = None
                else:
                    volume_delta = diff
                    if diff > 0:
                        self._last_vol_advance_ts = sample.ts
            elif sample.volume is not None and self._last_volume is None:
                volume_delta = None
            if sample.amount is not None and self._last_amount is not None:
                diff = sample.amount - self._last_amount
                if diff < 0:
                    amount_delta = None
                elif diff == 0 and self._is_stale_zero(
                    sample.ts, self._last_amt_advance_ts
                ):
                    amount_delta = None
                else:
                    amount_delta = diff
                    if diff > 0:
                        self._last_amt_advance_ts = sample.ts

        if volume_delta is None:
            self._bar_volume_bad = True
        else:
            self._bar_volume = (self._bar_volume or 0.0) + volume_delta
        if amount_delta is None:
            self._bar_amount_bad = True
        else:
            self._bar_amount = (self._bar_amount or 0.0) + amount_delta

        bar = self._bar
        assert bar is not None
        self._bar = Bar(
            symbol=bar.symbol,
            ts=bar.ts,
            open=bar.open,
            high=max(bar.high, sample.price),
            low=min(bar.low, sample.price),
            close=sample.price,
            volume=bar.volume,
            amount=bar.amount,
            samples=bar.samples + 1,
            span_s=bar.span_s + max(0, span),
            source=bar.source if not source_changed else sample.source,
            complete=False,
            from_open=bar.from_open,
            volume_unknown=bar.volume_unknown,
        )

        self._day = day
        self._last_ts = sample.ts
        self._last_volume = sample.volume
        self._last_amount = sample.amount
        self._last_source = sample.source or self._last_source
        return finalized


def _collect_bars(samples: list[Sample], symbol: str) -> list[Bar]:
    """单源样本重放 → 该源自己的 Bar 列表（源内差分，跨源不混）。"""
    acc = MinuteBarAccumulator(symbol)
    bars: list[Bar] = []
    for sample in samples:
        done = acc.feed(sample)
        if done is not None:
            bars.append(done)
    last = acc.flush()
    if last is not None:
        bars.append(last)
    return bars


def bars_by_source(samples: list[Sample], *, symbol: str = "") -> list[Bar]:
    """按 ``source`` 分别重放，再逐分钟选覆盖最好的那一根。

    **为什么必须分源**：``market:series`` 是多写侧共用键（桥轮询 / TdxAiData 订阅 /
    QMT 备源），各家累计量的刷新节奏不同——2026-10-20 实测桥的 ``Volume`` 可连续
    数分钟不变（明显滞后于订阅帧）。跨源差分会产生倒挂或错位分钟量；本函数让每个
    源在自己的序列内差分，再以「量已知 > samples 多 > span_s 小」逐分钟挑一根，
    OHLC 与量始终取自同一根，绝不拼接。

    代价：同一分钟内的多个源只保留一根，另一源的样本不进 Bar 的 ``samples``；
    这是有意的——分钟 Bar 的消费方要的是「这一分钟的价格/量」，不是样本清单。
    """
    groups: dict[str, list[Sample]] = {}
    for sample in samples:
        groups.setdefault(sample.source, []).append(sample)
    if not groups:
        return []

    candidates: dict[int, list[Bar]] = {}
    for group in groups.values():
        group.sort(key=lambda s: s.ts)
        for bar in _collect_bars(group, symbol or "?"):
            candidates.setdefault(bar.ts, []).append(bar)

    out: list[Bar] = []
    for ts in sorted(candidates):
        out.append(
            max(
                candidates[ts],
                key=lambda b: (
                    b.volume is not None,  # 量已知优先
                    b.samples,  # 覆盖样本多者优先
                    -b.span_s,  # 覆盖秒数小者优先（量归属更精确）
                    b.source,  # 确定性兜底
                ),
            )
        )
    return out


def normalize_period(period: str) -> str:
    """周期输入归一：``1h`` → ``60m``；其余原样小写返回（未知值由调用方校验）。"""
    key = str(period or "").strip().lower()
    return PERIOD_ALIASES.get(key, key)


def aggregate_period(
    bars: list[Bar], period: str, *, symbol: str | None = None
) -> list[Bar]:
    """1m Bar 列表 → 指定周期（会话对齐，午休不跨桶）。

    只聚合同交易日、同周期桶内的 1m Bar；缺失分钟的桶仍返回（``complete=False``），
    便于消费方看见缺口而不是被静默拼接。
    """
    period = normalize_period(period)
    minutes = PERIOD_MINUTES.get(period)
    if minutes is None:
        raise ValueError(f"unsupported period: {period}")
    if minutes == 1:
        return list(bars)

    buckets: dict[tuple[str, int], list[Bar]] = {}
    order: list[tuple[str, int]] = []
    for bar in bars:
        idx_info = session_index(bar.ts)
        if idx_info is None:
            continue
        day, idx = idx_info
        key = (day, (idx // minutes) * minutes)
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(bar)

    out: list[Bar] = []
    for day, start_idx in order:
        group = buckets[(day, start_idx)]
        group.sort(key=lambda b: b.ts)
        volumes = [b.volume for b in group]
        amounts = [b.amount for b in group]
        # 缺失分钟的桶仍返回已覆盖部分的量（complete=False 已标明缺口）；
        # 只有「已覆盖分钟里存在未知量」才把总量置 None，绝不把未知当 0。
        volume_unknown = any(v is None for v in volumes)
        amount_unknown = any(a is None for a in amounts)
        out.append(
            Bar(
                symbol=symbol or group[0].symbol,
                ts=index_to_epoch(day, start_idx),
                open=group[0].open,
                high=max(b.high for b in group),
                low=min(b.low for b in group),
                close=group[-1].close,
                volume=None
                if volume_unknown
                else sum(v for v in volumes if v is not None),
                amount=None
                if amount_unknown
                else sum(a for a in amounts if a is not None),
                samples=sum(b.samples for b in group),
                span_s=sum(b.span_s for b in group),
                source=group[-1].source,
                complete=all(b.complete for b in group) and len(group) == minutes,
                from_open=any(b.from_open for b in group),
                volume_unknown=volume_unknown,
            )
        )
    return out


def parse_bars(raw_members: list[str]) -> list[Bar]:
    """``market:minute`` ZSET 成员 → Bar 列表（坏数据跳过，不抛）。"""
    out: list[Bar] = []
    for raw in raw_members:
        try:
            data = json.loads(raw)
            out.append(
                Bar(
                    symbol=str(data["symbol"]),
                    ts=int(data["ts"]),
                    open=float(data["open"]),
                    high=float(data["high"]),
                    low=float(data["low"]),
                    close=float(data["close"]),
                    volume=_num(data.get("volume")),
                    amount=_num(data.get("amount")),
                    samples=int(data.get("samples") or 0),
                    span_s=int(data.get("span_s") or 0),
                    source=str(data.get("source") or ""),
                    complete=bool(data.get("complete")),
                    from_open=bool(data.get("from_open")),
                    volume_unknown=bool(data.get("volume_unknown")),
                )
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    out.sort(key=lambda b: b.ts)
    return out


def bar_to_json(bar: Bar) -> str:
    """Bar → Redis 成员 JSON（字段顺序稳定，便于对拍）。"""
    return json.dumps(bar.to_dict(), ensure_ascii=False, separators=(",", ":"))


def is_trading_day_date(day: str) -> bool:
    """基础交易日判断（周一~周五）；节假日由调用方的交易日历兜底。"""
    try:
        d = date_cls.fromisoformat(day)
    except ValueError:
        return False
    return d.weekday() < 5
