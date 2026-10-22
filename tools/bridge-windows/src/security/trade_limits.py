"""交易硬上限（用户 2026-10-22 拍板：299/秒、1999/天，写死，不可配置）。

为什么写死：
- 程序化交易报告义务的高频阈值是 300 笔/秒、20000 笔/日；
- 用户要求把系统压在这条线以下：**299 笔/秒、1999 笔/日**；
- 这是最后一道物理闸（桥的 order_stock/cancel_order_stock 前），
  不读环境变量、不读 config.yaml、不提供覆盖参数。

下单与撤单**各自独立计数**：撤单是减小风险的动作，不能因为当日下单额度
用尽而被连坐；但撤单本身也有 299/秒、1999/天的硬上限。

**计数持久化**：日计数落盘到 data/trade_limits_*.json。桥重启不能清零日额度；
状态文件读失败/写失败一律 fail-closed（拒绝下单），宁可当日不交易也不越线。
多桥实例不共享计数——生产必须只跑一个桥（本项目约定）。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime

log = logging.getLogger(__name__)

# 硬编码，禁止任何形式的运行时覆盖。
HARD_MAX_TRADES_PER_SEC = 299
HARD_MAX_TRADES_PER_DAY = 1999
# 当日晚间预警阈值（用户 2026-10-22：到 150 笔就要提醒）。写死，不提供覆盖。
WARN_AT_TRADES_PER_DAY = 150


class HardLimitTampered(RuntimeError):
    """硬上限被改动：立即停机，不接受降级/告警后继续。"""


def verify_hard_limits() -> None:
    """启动自检：常量必须仍是 299/1999，且限速行为必须符合预期。

    任何一项不通过 → SystemExit，桥拒绝启动（用户 2026-10-22：改了就停机）。
    """
    if (HARD_MAX_TRADES_PER_SEC != 299 or HARD_MAX_TRADES_PER_DAY != 1999):
        raise SystemExit("交易硬上限被篡改：必须为 299/1999，拒绝启动")
    if WARN_AT_TRADES_PER_DAY != 150:
        raise SystemExit("交易预警阈值被篡改：必须为 150，拒绝启动")
    # 行为自检：第 300 笔/秒必须被拒
    g = TradeRateGuard()
    now = datetime(2026, 1, 5, 9, 30, 0)
    for _ in range(HARD_MAX_TRADES_PER_SEC):
        ok, _ = g.allow(now, mono=1000.0)
        if not ok:
            raise SystemExit("交易硬上限自检失败：299 笔内出现拒绝")
    ok, _ = g.allow(now, mono=1000.0)
    if ok:
        raise SystemExit("交易硬上限自检失败：第 300 笔未被拒绝，拒绝启动")
    # 行为自检：第 2000 笔/日必须被拒
    g2 = TradeRateGuard()
    allowed = 0
    for sec in range(10):
        mono = 2000.0 + sec * 2.0
        for _ in range(HARD_MAX_TRADES_PER_SEC):
            ok, _ = g2.allow(now, mono=mono)
            if ok:
                allowed += 1
    if allowed != HARD_MAX_TRADES_PER_DAY:
        raise SystemExit(
            f"交易硬上限自检失败：日放行 {allowed} != {HARD_MAX_TRADES_PER_DAY}，拒绝启动")


class TradeRateGuard:
    """滑动 1 秒窗口 + 自然日计数；线程安全（下单在线程池里并发）。"""

    def __init__(self, max_per_sec: int = HARD_MAX_TRADES_PER_SEC,
                 max_per_day: int = HARD_MAX_TRADES_PER_DAY,
                 state_path: str = ""):
        self.max_per_sec = max_per_sec
        self.max_per_day = max_per_day
        self.state_path = state_path
        self._lock = threading.Lock()
        self._recent: deque[float] = deque()
        self._day = datetime.now().strftime("%Y%m%d")
        self._day_count = 0
        self._warned_150 = False
        self._warning_pending = False
        self._state_error = ""
        self.denied_sec = 0
        self.denied_day = 0
        self._load_state()

    def _check_immutable(self) -> None:
        if (self.max_per_sec, self.max_per_day) != (
                HARD_MAX_TRADES_PER_SEC, HARD_MAX_TRADES_PER_DAY):
            raise HardLimitTampered(
                f"交易硬上限被篡改: {self.max_per_sec}/s, {self.max_per_day}/d")

    def _load_state(self) -> None:
        if not self.state_path:
            return
        try:
            with open(self.state_path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            # 读不到/损坏 = 无法证明当日额度 → fail-closed
            self._state_error = f"硬上限状态文件不可读: {exc}"
            log.critical(self._state_error)
            return
        try:
            if data.get("day") == self._day:
                self._day_count = int(data.get("count", 0))
                self._warned_150 = bool(data.get("warned_150", False))
                if (self._day_count >= WARN_AT_TRADES_PER_DAY
                        and not self._warned_150):
                    self._warned_150 = True
                    self._warning_pending = True
        except (TypeError, ValueError):
            self._state_error = "硬上限状态文件内容非法"
            log.critical(self._state_error)

    def _persist(self) -> None:
        if not self.state_path:
            return
        data = {"day": self._day, "count": self._day_count,
                "warned_150": self._warned_150}
        os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.state_path)

    def _roll_day(self, now: datetime) -> None:
        day = now.strftime("%Y%m%d")
        if day != self._day:
            self._day = day
            self._day_count = 0
            self._warned_150 = False
            self._warning_pending = False
            self._recent.clear()

    def allow(self, now: datetime | None = None,
              mono: float | None = None) -> tuple[bool, str]:
        """放行返回 (True, "")；拒绝返回 (False, 原因)。

        硬上限一旦被改动，进程**立即停机**（os._exit），不是拒绝一笔。
        """
        try:
            self._check_immutable()
        except HardLimitTampered:
            log.critical("交易硬上限被篡改，进程停机（用户铁律：改了就停机）")
            os._exit(3)
        if self._state_error:
            return False, f"{self._state_error}（fail-closed，拒绝交易）"
        now = now or datetime.now()
        ts = mono if mono is not None else time.monotonic()
        with self._lock:
            self._roll_day(now)
            while self._recent and ts - self._recent[0] >= 1.0:
                self._recent.popleft()
            if len(self._recent) >= self.max_per_sec:
                self.denied_sec += 1
                return False, (f"硬上限：{self.max_per_sec} 笔/秒已用尽，"
                               f"本秒拒绝（写死，不可改）")
            if self._day_count >= self.max_per_day:
                self.denied_day += 1
                return False, (f"硬上限：{self.max_per_day} 笔/日已用尽，"
                               f"今日拒绝（写死，不可改）")
            self._day_count += 1
            if self._day_count == WARN_AT_TRADES_PER_DAY and not self._warned_150:
                self._warned_150 = True
                self._warning_pending = True
            try:
                self._persist()
            except OSError as exc:
                # 落盘失败 = 重启后无法证明额度 → fail-closed（本笔不放行）
                self._day_count -= 1
                log.critical("交易硬上限状态落盘失败，拒绝本笔: %s", exc)
                return False, "硬上限状态落盘失败（fail-closed，拒绝本笔）"
            self._recent.append(ts)
            return True, ""

    def consume_warning(self) -> str:
        """当日达到 150 笔时返回一次预警文案；其它时候返回空串。"""
        with self._lock:
            if not self._warning_pending:
                return ""
            self._warning_pending = False
            return (f"交易硬上限预警：今日已成交/提交 {self._day_count} 笔"
                    f"（提醒阈值 {WARN_AT_TRADES_PER_DAY}，硬上限 {self.max_per_day}"
                    f"；{self.max_per_sec}/秒）。请确认策略是否异常。")

    def stats(self) -> dict:
        with self._lock:
            ts = time.monotonic()
            recent = sum(1 for t in self._recent if ts - t < 1.0)
            return {
                "day": self._day,
                "day_count": self._day_count,
                "day_limit": self.max_per_day,
                "day_remaining": max(self.max_per_day - self._day_count, 0),
                "last_sec_count": recent,
                "sec_limit": self.max_per_sec,
                "denied_sec": self.denied_sec,
                "denied_day": self.denied_day,
                "warn_at": WARN_AT_TRADES_PER_DAY,
                "warned_150": self._warned_150,
                "persisted": bool(self.state_path),
                "state_error": self._state_error,
            }
