"""自动跟随实盘：定时读账户代码 → 并入订阅（免费 tq 通道，≤100）。"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

log = logging.getLogger(__name__)

TICK_S = 5.0


class SubscriptionAutoSync:
    def __init__(self, manager, provider: Callable[[str, bool], list[str]],
                 tick_s: float = TICK_S):
        self.manager = manager
        self.provider = provider
        self.tick_s = max(1.0, float(tick_s or TICK_S))
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._loop, name="sub-auto-sync", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        last_run = 0.0
        while True:
            try:
                auto = self.manager.auto_status()
                if auto.get("enabled"):
                    interval = max(15, min(3600, int(auto.get("interval_s") or 60)))
                    if time.monotonic() - last_run >= interval:
                        last_run = time.monotonic()
                        source = str(auto.get("source") or "positions")
                        include = bool(auto.get("include_orders"))
                        codes = self.provider(source, include)
                        self.manager.apply_account_codes(codes)
            except Exception as exc:  # noqa: BLE001
                log.warning("自动订阅同步异常: %s", exc)
                try:
                    self.manager.record_auto_error(f"{type(exc).__name__}: {exc}")
                except Exception:  # noqa: BLE001
                    pass
            time.sleep(self.tick_s)
