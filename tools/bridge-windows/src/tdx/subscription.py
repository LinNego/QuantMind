"""实时订阅管理：≤100 只标的的推送。

两条通道
--------
- ``tq``（免费）：走通达信客户端的 ``tqcenter.tq.subscribe_hq``。推送体只有
  ``{"Code":"600519.SH"}`` —— 是个「该股有更新」的信号；收到后由本模块再去
  桥的 ``get_market_snapshot`` 拉最新盘口。好处：不需要付费 Key，只要 TdxW 在跑。
- ``aidata``（付费）：走 ``tdxaidata.tqs.subscribe``，推送体是完整盘口 JSON
  （bid1-5/ask1-5/涨跌停/成交量等），需要 ``TdxAiData.ini`` 里的付费 Key。

上限：通达信 SDK 单次订阅**最多 100 只**（累计并集），本模块在入口处强制截断，
绝不让它变成「订阅失败但界面显示已订阅」。
"""
from __future__ import annotations

import json
import logging
import os
import queue
import sys
import threading
import time
from collections import deque
from datetime import datetime

log = logging.getLogger(__name__)

MAX_SUBSCRIBE_SYMBOLS = 100
DEFAULT_AIDATA_MAX_SYMBOLS = 500
MODES = ("tq", "aidata")


class SubscriptionManager:
    """订阅列表 + 推送计数 + 最近推送环形缓冲。线程安全。"""

    def __init__(self, aidata=None, tq_sys_path: str = "",
                 state_file: str = "", snapshot_fetcher=None,
                 max_symbols: int = MAX_SUBSCRIBE_SYMBOLS,
                 max_symbols_aidata: int = DEFAULT_AIDATA_MAX_SYMBOLS):
        self.aidata = aidata
        self.tq_sys_path = tq_sys_path or ""
        self.state_file = state_file or ""
        self.snapshot_fetcher = snapshot_fetcher
        # 100 只是**免费 tq 通道**的硬上限（tqcenter.subscribe_hq 源码写死）；
        # 付费 tdxaidata 的 subscribe 在 Python 层没有 100 检查（源码实测），
        # 真实上限以通达信服务端为准，这里按可配上限执行。
        self.max_symbols_tq = min(int(max_symbols or MAX_SUBSCRIBE_SYMBOLS),
                                  MAX_SUBSCRIBE_SYMBOLS)
        self.max_symbols_aidata = max(1, int(max_symbols_aidata or
                                             DEFAULT_AIDATA_MAX_SYMBOLS))
        self._lock = threading.Lock()
        self._enabled = False
        self._mode = "tq"
        self._codes: list[str] = []
        self._state = "stopped"
        self._error = ""
        self._pushes_total = 0
        self._last_push_ts = 0.0
        self._recent: deque = deque(maxlen=200)
        self._latest: dict[str, dict] = {}
        self._tq = None
        self._thread: threading.Thread | None = None
        self._snap_q: queue.Queue = queue.Queue(maxsize=1000)
        self._snap_thread: threading.Thread | None = None
        self._load_state()

    # ---- 持久化 ----

    def _load_state(self) -> None:
        if not self.state_file or not os.path.exists(self.state_file):
            return
        try:
            with open(self.state_file, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            log.warning("订阅状态读取失败(忽略): %s", exc)
            return
        self._enabled = bool(data.get("enabled"))
        self._mode = str(data.get("mode") or "tq")
        self._codes = self._clean_codes(data.get("codes") or [])

    def _persist(self) -> None:
        if not self.state_file:
            return
        os.makedirs(os.path.dirname(self.state_file) or ".", exist_ok=True)
        tmp = self.state_file + ".tmp"
        payload = {"enabled": self._enabled, "mode": self._mode,
                   "codes": self._codes,
                   "updated_at": datetime.now().isoformat(timespec="seconds")}
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.state_file)

    # ---- 配置 ----

    def _limit_for(self, mode: str | None = None) -> int:
        return (self.max_symbols_tq if (mode or self._mode) == "tq"
                else self.max_symbols_aidata)

    def _clean_codes(self, codes, mode: str | None = None) -> list[str]:
        out: list[str] = []
        seen = set()
        for raw in codes or []:
            code = str(raw or "").strip().upper()
            if not code or code in seen:
                continue
            seen.add(code)
            out.append(code)
        return out[: self._limit_for(mode)]

    @property
    def max_symbols(self) -> int:
        """当前通道的上限（保留旧属性名，UI/日志用）。"""
        return self._limit_for()

    def set_config(self, enabled: bool | None = None, mode: str | None = None,
                   codes: list[str] | None = None) -> dict:
        """改配置；**全部校验通过后才落状态**。

        旧实现是边走边改：`mode=aidata` 因缺 Key 抛错时 `self._mode` 已经改成
        aidata，下一次设置代码就按 500 截断（2026-10-23 单测抓到）。
        """
        # aidata Key 校验放在锁外：_on_push 会从 DLL 回调线程拿管理锁，
        # 若这里持锁再回调 tqcenter/aidata，锁序反转会死锁（2026-10-23 实测）。
        check_mode = str(mode) if mode is not None else self._mode
        will_enable = self._enabled if enabled is None else bool(enabled)
        if will_enable and check_mode == "aidata":
            if self.aidata is None or not self.aidata.status().get("token_configured"):
                raise ValueError("aidata 模式需要先配置 TdxAiData 付费 Key")
        with self._lock:
            # 取消订阅必须用**旧**列表：tqcenter 的 subscribe_hq 是累计并集，
            # 拿新列表去取消会把刚加上的又取消掉、旧的却留着（2026-10-23 实测）。
            old_mode, old_codes, old_state = self._mode, list(self._codes), self._state
            new_mode = self._mode if mode is None else str(mode)
            if new_mode not in MODES:
                raise ValueError(f"mode 必须是 {MODES}")
            if codes is None:
                # 切换通道时按新上限重新截断（aidata 500 → tq 100）
                new_codes = old_codes[: self._limit_for(new_mode)]
            else:
                new_codes = self._clean_codes(codes, new_mode)
            new_enabled = self._enabled if enabled is None else bool(enabled)
            if new_enabled and not new_codes:
                raise ValueError("订阅列表为空")
            stop_first = (new_mode != old_mode or new_codes != old_codes
                          or new_enabled != self._enabled)
            self._mode, self._codes, self._enabled = new_mode, new_codes, new_enabled
            self._persist()
            codes_now, mode_now, enabled_now = list(self._codes), self._mode, self._enabled
        if stop_first:
            self._stop_with(old_mode, old_codes, old_state)
            if enabled_now:
                self.start()
        return {"enabled": enabled_now, "mode": mode_now, "codes": codes_now}

    # ---- 启停 ----

    def autostart(self) -> None:
        """进程启动时按持久化状态恢复；失败只记状态，不炸启动。"""
        with self._lock:
            enabled, codes = self._enabled, list(self._codes)
        if enabled and codes:
            try:
                self.start()
            except Exception as exc:  # noqa: BLE001
                log.warning("订阅自动恢复失败: %s", exc)

    def start(self) -> dict:
        with self._lock:
            if self._state == "running":
                return self.status()
            self._state = "starting"
            self._error = ""
            mode, codes = self._mode, list(self._codes)
        if not codes:
            with self._lock:
                self._state = "stopped"
            raise ValueError("订阅列表为空")
        limit = self._limit_for()
        if len(codes) > limit:
            raise ValueError(f"{self._mode} 通道订阅上限 {limit} 只")
        self._ensure_snapshot_worker()
        try:
            # DLL 的 subscribe 是阻塞调用，且在本进程内可能长时间不返回
            # （与 tqcenter 同进程时实测会卡死，2026-10-23）。放到独立线程并限时，
            # 绝不让唯一的订阅执行器被它占死——否则 /subscription 状态接口也会排队。
            if mode == "aidata":
                self._run_with_timeout(lambda: self._start_aidata(codes),
                                       15.0, "aidata 订阅")
            else:
                self._run_with_timeout(lambda: self._start_tq(codes),
                                       15.0, "tq 订阅")
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._state = "error"
                self._error = f"{type(exc).__name__}: {exc}"
            log.warning("订阅启动失败(%s): %s", mode, exc)
            raise
        with self._lock:
            self._state = "running"
        log.info("订阅已启动 mode=%s codes=%d", mode, len(codes))
        return self.status()

    def stop(self) -> None:
        with self._lock:
            mode, codes = self._mode, list(self._codes)
            was = self._state
            self._state = "stopped"
        self._stop_with(mode, codes, was)

    def _stop_with(self, mode: str, codes: list[str], was: str = "running") -> None:
        """按**调用方给的**旧 mode/codes 取消订阅（set_config 改列表时用）。"""
        if was == "stopped":
            return
        try:
            if mode == "aidata" and self.aidata is not None:
                self.aidata.unsubscribe(codes)
            elif self._tq is not None and codes:
                # tqcenter 的取消方法叫 unsubscribe_hq（不是 unsubscribe）；
                # 名字取错会静默留下旧订阅，累计到 100 后再也加不进去。
                fn = (getattr(self._tq, "unsubscribe_hq", None)
                      or getattr(self._tq, "unsubscribe", None))
                if fn is not None:
                    fn(codes)
        except Exception as exc:  # noqa: BLE001
            log.warning("取消订阅失败(忽略): %s", exc)

    @staticmethod
    def _run_with_timeout(fn, timeout: float, label: str) -> None:
        """在独立守护线程里跑阻塞的 subscribe/initialize，超时就报错（线程自行泄漏）。"""
        box: dict = {}

        def runner():
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                box["err"] = exc

        thread = threading.Thread(target=runner, name="sub-start", daemon=True)
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            raise RuntimeError(f"{label} 超时（{timeout:.0f}s 未返回）")
        if "err" in box:
            raise box["err"]

    def _start_aidata(self, codes: list[str]) -> None:
        if self.aidata is None:
            raise RuntimeError("AidataService 未初始化")
        status = self.aidata.status()
        if not status.get("available"):
            raise RuntimeError(f"tdxaidata 不可用: {status.get('load_error')}")
        if not status.get("token_configured"):
            raise RuntimeError("未配置 TdxAiData 付费 Key")
        self.aidata.subscribe(codes, self._on_push)

    def _start_tq(self, codes: list[str]) -> None:
        if self.tq_sys_path and self.tq_sys_path not in sys.path:
            sys.path.insert(0, self.tq_sys_path)
        from tqcenter import tq  # noqa: PLC0415
        self._tq = tq
        tq.initialize(__file__)
        tq.subscribe_hq(codes, self._on_push)

    # ---- 推送处理 ----

    def _on_push(self, data) -> int:
        """回调（DLL/客户端线程）：只做入队与计数，绝不做阻塞 IO。"""
        text = str(data or "")
        symbol = ""
        try:
            parsed = json.loads(text) if text.startswith("{") else {}
        except ValueError:
            parsed = {}
        if isinstance(parsed, dict):
            symbol = str(parsed.get("Code") or parsed.get("code") or "")
            if not symbol:
                # 付费通道（tdxaidata）推送体：
                # {"ResultSets":[{"ColDes":[...],"Content":[[...]]}]}
                rs = parsed.get("ResultSets")
                if isinstance(rs, list) and rs:
                    cols = rs[0].get("ColDes") or []
                    rows = rs[0].get("Content") or []
                    if rows and isinstance(rows[0], list):
                        row = dict(zip(cols, rows[0]))
                        symbol = str(row.get("code") or "")
                        if symbol:
                            parsed = row
        with self._lock:
            self._pushes_total += 1
            self._last_push_ts = time.time()
            self._recent.appendleft({
                "ts": datetime.now().isoformat(timespec="seconds"),
                "symbol": symbol,
                "mode": self._mode,
                "payload": text[:1200],
            })
            mode = self._mode
        if mode == "tq" and symbol:
            try:
                self._snap_q.put_nowait(symbol)
            except queue.Full:
                pass
        elif mode == "aidata" and symbol and isinstance(parsed, dict):
            with self._lock:
                self._latest[symbol] = {"ts": datetime.now().isoformat(timespec="seconds"),
                                        "data": parsed}
        return 1

    def _ensure_snapshot_worker(self) -> None:
        with self._lock:
            if self._snap_thread is not None and self._snap_thread.is_alive():
                return
            self._snap_thread = threading.Thread(
                target=self._snapshot_loop, name="sub-snapshot", daemon=True)
            self._snap_thread.start()

    def _snapshot_loop(self) -> None:
        """tq 模式：收到 Code 通知后拉一次快照（阻塞调用，绝不进事件循环）。"""
        while True:
            symbol = self._snap_q.get()
            try:
                if self.snapshot_fetcher is None:
                    continue
                snap = self.snapshot_fetcher(symbol)
                if isinstance(snap, dict):
                    with self._lock:
                        self._latest[symbol] = {
                            "ts": datetime.now().isoformat(timespec="seconds"),
                            "data": snap,
                        }
            except Exception as exc:  # noqa: BLE001
                log.debug("订阅快照拉取失败 %s: %s", symbol, exc)

    # ---- 状态 ----

    def status(self) -> dict:
        # 后端订阅查询**必须在 manager 锁外**：DLL 推送线程持 tqcenter 的
        # _callback_lock 调进 _on_push 再拿 manager 锁；这里若持锁反查
        # get_subscribe_hq_stock_list() 就是锁序反转 → 接口永久挂住。
        with self._lock:
            mode = self._mode
        subscribed = []
        if mode == "aidata" and self.aidata is not None:
            try:
                subscribed = list(self.aidata.status().get("subscribed") or [])
            except Exception:  # noqa: BLE001
                subscribed = []
        elif mode == "tq" and self._tq is not None:
            try:
                subscribed = list(self._tq.get_subscribe_hq_stock_list() or [])
            except Exception:  # noqa: BLE001
                subscribed = []
        with self._lock:
            return {
                "enabled": self._enabled,
                "mode": self._mode,
                "state": self._state,
                "error": self._error,
                "codes": list(self._codes),
                "count": len(self._codes),
                "max_symbols": self._limit_for(),
                "max_symbols_tq": self.max_symbols_tq,
                "max_symbols_aidata": self.max_symbols_aidata,
                "pushes_total": self._pushes_total,
                "last_push_at": (datetime.fromtimestamp(self._last_push_ts)
                                 .isoformat(timespec="seconds")
                                 if self._last_push_ts else ""),
                "subscribed": subscribed,
                "latest": dict(self._latest),
                "recent": list(self._recent)[:50],
            }
