"""采集器服务：全市场 pricevol 5s + 热集 snapshot 3s + 1m 聚合 + 熔断。"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from datetime import datetime, time as dtime

from .aggregator import BarAggregator
from .bridge_client import BridgeClient, BridgeError
from .config import CollectorConfig
from .store import MarketStore

log = logging.getLogger(__name__)

SESSION_MORNING = (dtime(9, 15), dtime(11, 35))
SESSION_AFTERNOON = (dtime(12, 55), dtime(15, 5))
FULL_MARKET_PAUSE = ((dtime(9, 25), dtime(9, 30)), (dtime(14, 57), dtime(15, 0)))
HOT_HISTORY_LEN = 240  # 3s × 240 ≈ 12 分钟


def chunked(seq, size: int):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _in_window(t: dtime, window: tuple[dtime, dtime]) -> bool:
    return window[0] <= t <= window[1]


def is_trading_time(dt: datetime | None = None) -> bool:
    dt = dt or datetime.now()
    if dt.weekday() >= 5:
        return False
    t = dt.time()
    return _in_window(t, SESSION_MORNING) or _in_window(t, SESSION_AFTERNOON)


def is_full_market_window(dt: datetime | None = None) -> bool:
    """全市场大调用窗口：盘中避开 09:25–09:30 与 14:57–15:00 两个撮合窗。"""
    dt = dt or datetime.now()
    if not is_trading_time(dt):
        return False
    t = dt.time()
    return not any(_in_window(t, w) for w in FULL_MARKET_PAUSE)


class CircuitBreaker:
    """连续错误达到阈值 → 暂停 N 秒；成功一次即清零。"""

    def __init__(self, threshold: int = 5, pause_s: float = 30.0):
        self.threshold = threshold
        self.pause_s = pause_s
        self.consecutive = 0
        self.total_errors = 0
        self.last_error = ""
        self._paused_until = 0.0

    def record_success(self) -> None:
        self.consecutive = 0

    def record_error(self, message: str, now: float | None = None) -> None:
        self.consecutive += 1
        self.total_errors += 1
        self.last_error = message[:240]
        if self.consecutive >= self.threshold:
            self._paused_until = (now if now is not None else time.monotonic()) + self.pause_s
            self.consecutive = 0
            log.warning("采集熔断：暂停 %.0fs（连续错误达 %d 次）",
                        self.pause_s, self.threshold)

    def is_paused(self, now: float | None = None) -> bool:
        return (now if now is not None else time.monotonic()) < self._paused_until

    def remaining_s(self) -> float:
        return max(self._paused_until - time.monotonic(), 0.0)


class CollectorService:
    def __init__(self, config: CollectorConfig):
        self.config = config
        self.client = BridgeClient(config.bridge_url, config.bridge_token)
        self.store = MarketStore(config.db_path)
        self.aggregator = BarAggregator()
        self.snapshots: dict[str, dict] = {}
        self.hot_history: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=HOT_HISTORY_LEN))
        self.universe: list[str] = []
        self.trading_days: set[str] = set()
        self.snapshot_ts: datetime | None = None
        self.bar_ts: datetime | None = None
        self.last_cycle_ms: float = 0.0
        self.last_pricevol_count: int = 0
        self.empty_cycles: int = 0
        self._cycle_active = False
        self.breaker = CircuitBreaker(pause_s=config.error_pause_s)
        self.hot_breaker = CircuitBreaker(pause_s=config.error_pause_s)
        self._pending_bars: list[dict] = []
        self._flush_lock = asyncio.Lock()
        self._stopping = False
        self._tasks: list[asyncio.Task] = []
        self._runner = None
        self._bridge_health: dict = {}
        self._bridge_health_ts = 0.0
        self._bridge_refreshing = False
        self._db_stats: dict = {}
        self._db_stats_ts = 0.0
        self._db_stats_lock = asyncio.Lock()
        self._last_universe_day = ""
        self._last_prune_day = ""
        self.started_at = datetime.now().astimezone()

    # ---- 生命周期 ----

    async def run(self) -> None:
        from .server import start_http_server

        log.info("采集器启动: bridge=%s db=%s http=%s:%s",
                 self.config.bridge_url, self.config.db_path,
                 self.config.host, self.config.port)
        self._runner = await start_http_server(self)
        await self._refresh_universe()
        await self._refresh_trading_days()
        self._tasks = [
            asyncio.create_task(self._snapshot_loop(), name="snapshot"),
            asyncio.create_task(self._hot_loop(), name="hot"),
            asyncio.create_task(self._flush_loop(), name="flush"),
            asyncio.create_task(self._maintenance_loop(), name="maintenance"),
        ]
        try:
            await asyncio.gather(*self._tasks)
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop()

    async def stop(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        try:
            self._pending_bars.extend(self.aggregator.flush_all())
            await self._flush_pending(force=True)
        except Exception:
            log.warning("退出前落盘失败", exc_info=True)
        if self._runner is not None:
            await self._runner.cleanup()
        await self.client.close()
        self.store.close()
        log.info("采集器已停止")

    # ---- 基础数据 ----

    async def _refresh_universe(self) -> None:
        try:
            codes = await self.client.get_stock_list("5")
        except BridgeError as exc:
            codes = []
            log.warning("股票清单刷新失败: %s", exc)
        if codes:
            # M4：任意非空短清单不得覆盖（TQ 抖动/权限变化时整天采集缩水）
            if self.universe and (len(codes) < 1000
                                  or len(codes) < int(len(self.universe) * 0.8)):
                log.error("股票清单异常缩水: %d -> %d，拒绝替换",
                          len(self.universe), len(codes))
                return
            self.universe = codes
            await asyncio.to_thread(self.store.save_universe, codes)
            self._last_universe_day = datetime.now().strftime("%Y%m%d")
            log.info("股票清单已刷新: %d 只", len(codes))
        elif not self.universe:
            cached = await asyncio.to_thread(self.store.load_universe)
            self.universe = cached
            if cached:
                log.warning("使用缓存股票清单: %d 只", len(cached))

    async def _refresh_trading_days(self) -> None:
        try:
            dates = await self.client.get_trading_dates(count=20)
            self.trading_days = {d for d in dates if len(d) == 8}
        except BridgeError as exc:
            log.warning("交易日历刷新失败，回退到周一至周五: %s", exc)

    def _is_trading_day(self, dt: datetime) -> bool:
        if self.trading_days:
            return dt.strftime("%Y%m%d") in self.trading_days
        return dt.weekday() < 5

    # ---- 采集循环 ----

    async def _snapshot_loop(self) -> None:
        while not self._stopping:
            started = time.monotonic()
            now = datetime.now()
            if (self._is_trading_day(now) and is_full_market_window(now)
                    and not self.breaker.is_paused()):
                try:
                    await self._collect_snapshot_cycle()
                    self.breaker.record_success()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - 循环必须活着
                    self.breaker.record_error(str(exc))
                    log.warning("全市场快照轮次失败: %s", exc)
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(0.2, self.config.snapshot_interval - elapsed))

    async def _collect_snapshot_cycle(self) -> None:
        if not self.universe:
            await self._refresh_universe()
            if not self.universe:
                return
        started = time.monotonic()
        chunks = list(chunked(self.universe, self.config.snapshot_chunk))
        self._cycle_active = True
        cycle_count = 0
        try:
            for idx, chunk in enumerate(chunks):
                data = await self.client.get_pricevol(chunk)
                # H3：空/残缺返回不能被当成功（否则熔断清零、/health 全绿）
                if len(data) < max(1, int(len(chunk) * 0.5)):
                    self.empty_cycles += 1
                    raise BridgeError(
                        f"pricevol 返回 {len(data)}/{len(chunk)}，低于 50%")
                # H2：每块用真实采样时刻，不能用整轮起点
                sample_now = datetime.now()
                for code, info in data.items():
                    if not isinstance(info, dict):
                        continue
                    try:
                        price = float(info.get("Now") or 0)
                    except (TypeError, ValueError):
                        continue
                    if price <= 0:
                        continue
                    try:
                        volume = float(info.get("Volume") or 0)
                    except (TypeError, ValueError):
                        volume = None
                    record = dict(info)
                    record["_ts"] = sample_now.astimezone().isoformat(timespec="seconds")
                    record["_source"] = "pricevol"
                    self.snapshots[code] = record
                    cycle_count += 1
                    self._pending_bars.extend(
                        self.aggregator.on_sample(code, price, volume, now=sample_now))
                if idx + 1 < len(chunks):
                    await asyncio.sleep(0.02)  # 让出事件循环/交易槽
            self.snapshot_ts = datetime.now()
            self.last_pricevol_count = cycle_count
            self.last_cycle_ms = round((time.monotonic() - started) * 1000.0, 1)
            await self._flush_pending()
        finally:
            self._cycle_active = False

    async def _hot_loop(self) -> None:
        if not self.config.hot_set:
            return
        sem = asyncio.Semaphore(2)
        while not self._stopping:
            started = time.monotonic()
            now = datetime.now()
            if (self._is_trading_day(now) and is_trading_time(now)
                    and not self.hot_breaker.is_paused()):
                await asyncio.gather(
                    *(self._collect_hot_one(code, sem) for code in self.config.hot_set),
                    return_exceptions=True)
            # 从本轮开始计时，避免「耗时 + interval」把周期拖成 2 倍
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(0.5, self.config.hot_interval - elapsed))

    async def _collect_hot_one(self, code: str, sem: asyncio.Semaphore) -> None:
        async with sem:
            try:
                snap = await self.client.get_market_snapshot(code)
            except BridgeError as exc:
                self.hot_breaker.record_error(str(exc))
                return
            if not isinstance(snap, dict) or not snap:
                return
            snap = dict(snap)
            snap["_ts"] = datetime.now().astimezone().isoformat(timespec="seconds")
            snap["_source"] = "snapshot"
            self.snapshots[code] = snap
            self.hot_history[code].append(snap)

    async def _flush_loop(self) -> None:
        while not self._stopping:
            await asyncio.sleep(5)
            try:
                # H2：整轮采集中禁止封口，避免残根在同一分钟键上覆盖完整 bar
                if not self._cycle_active:
                    self._pending_bars.extend(self.aggregator.flush_stale())
                await self._flush_pending()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("落盘失败: %s", exc)

    async def _flush_pending(self, force: bool = False) -> None:
        async with self._flush_lock:
            if not self._pending_bars:
                return
            if not force and len(self._pending_bars) < 500:
                return
            bars, self._pending_bars = self._pending_bars, []
        try:
            await asyncio.to_thread(self.store.upsert_bars, bars)
            self.bar_ts = datetime.now()
        except asyncio.CancelledError:
            # stop() 取消在途写：已取出的 bars 必须放回，否则整批丢失
            self._pending_bars = (bars + self._pending_bars)[-20000:]
            raise
        except Exception as exc:  # noqa: BLE001
            # 写失败放回缓冲，下一轮重试（有上限，避免无界增长）
            self._pending_bars = (bars + self._pending_bars)[-20000:]
            log.warning("bars 写入失败（%d 行已回填缓冲）: %s", len(bars), exc)

    async def _maintenance_loop(self) -> None:
        while not self._stopping:
            await asyncio.sleep(60)
            now = datetime.now()
            day = now.strftime("%Y%m%d")
            try:
                if now.hour == 9 and now.minute >= 5 and self._last_universe_day != day:
                    await self._refresh_universe()
                    await self._refresh_trading_days()
                if now.hour == 15 and now.minute >= 10 and self._last_prune_day != day:
                    self._pending_bars.extend(self.aggregator.flush_all())
                    await self._flush_pending(force=True)
                    deleted = await asyncio.to_thread(
                        self.store.prune, self.config.retention_days)
                    self._last_prune_day = day
                    log.info("留存清理完成: 删除 %d 行（保留 %d 天）",
                             deleted, self.config.retention_days)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("维护任务失败: %s", exc)

    # ---- 读侧 ----

    async def _bridge_health_cached(self) -> dict:
        """/health 绝不等待桥：有过期数据先返回旧值，后台刷新。

        桥繁忙时 client.health() 可能 >1s；若在 /health 里 await，会让桥侧
        0.8s 的采集器探针误报「离线」。
        """
        now = time.monotonic()
        if now - self._bridge_health_ts < 5.0 and self._bridge_health:
            return self._bridge_health
        if self._bridge_health:
            if not self._bridge_refreshing:
                self._bridge_refreshing = True
                asyncio.create_task(self._refresh_bridge_health())
            return self._bridge_health
        try:
            self._bridge_health = await asyncio.wait_for(
                self.client.health(), timeout=0.8)
        except (BridgeError, asyncio.TimeoutError) as exc:
            self._bridge_health = {"status": "down", "error": str(exc)[:120]}
        self._bridge_health_ts = now
        return self._bridge_health

    async def _refresh_bridge_health(self) -> None:
        try:
            self._bridge_health = await self.client.health()
        except BridgeError as exc:
            self._bridge_health = {"status": "down", "error": str(exc)[:120]}
        finally:
            self._bridge_health_ts = time.monotonic()
            self._bridge_refreshing = False

    async def _db_stats_cached(self) -> dict:
        now = time.monotonic()
        if now - self._db_stats_ts < 300.0 and self._db_stats:
            return self._db_stats
        async with self._db_stats_lock:
            now = time.monotonic()
            if now - self._db_stats_ts < 300.0 and self._db_stats:
                return self._db_stats
            try:
                self._db_stats = await asyncio.to_thread(self.store.stats)
            except Exception as exc:  # noqa: BLE001
                self._db_stats = {"error": str(exc)}
            self._db_stats_ts = now
            return self._db_stats

    @staticmethod
    def _age_ms(ts: datetime | None) -> float | None:
        if ts is None:
            return None
        return round((datetime.now() - ts).total_seconds() * 1000.0, 1)

    async def health_public(self) -> dict:
        """匿名 /health 只暴露状态与新鲜度，不泄露地址/错误/库统计。"""
        bridge = await self._bridge_health_cached()
        return {
            "status": "ok" if bridge.get("status") == "ok" else "degraded",
            "version": self.config.version,
            "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "tdx_connected": bridge.get("tdx_connected"),
            "snapshot_age_ms": self._age_ms(self.snapshot_ts),
            "bar_age_ms": self._age_ms(self.bar_ts),
            "universe_size": len(self.universe),
            "last_cycle_ms": self.last_cycle_ms,
            "last_pricevol_count": self.last_pricevol_count,
            "empty_cycles": self.empty_cycles,
        }

    async def health(self) -> dict:
        bridge = await self._bridge_health_cached()
        db = await self._db_stats_cached()
        return {
            "status": "ok" if bridge.get("status") == "ok" else "degraded",
            "version": self.config.version,
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "bridge": {
                "url": self.config.bridge_url,
                "status": bridge.get("status"),
                "tdx_connected": bridge.get("tdx_connected"),
                "semaphores": bridge.get("semaphores"),
                "error": bridge.get("error"),
            },
            "universe_size": len(self.universe),
            "snapshot_count": len(self.snapshots),
            "hot_count": len(self.config.hot_set),
            "snapshot_age_ms": self._age_ms(self.snapshot_ts),
            "bar_age_ms": self._age_ms(self.bar_ts),
            "last_cycle_ms": self.last_cycle_ms,
            "last_pricevol_count": self.last_pricevol_count,
            "empty_cycles": self.empty_cycles,
            "open_states": self.aggregator.open_states,
            "pending_bars": len(self._pending_bars),
            "paused": self.breaker.is_paused(),
            "pause_remaining_s": round(self.breaker.remaining_s(), 1),
            "errors": {
                "snapshot": {"total": self.breaker.total_errors,
                             "last": self.breaker.last_error},
                "hot": {"total": self.hot_breaker.total_errors,
                        "last": self.hot_breaker.last_error},
                "bridge": self.client.stats,
            },
            "db": db,
        }

    def snapshot_payload(self, codes: list[str] | None = None,
                         include_all: bool = False) -> dict:
        if codes is None and not include_all:
            sample = {}
            for code in list(self.snapshots)[:5]:
                sample[code] = self.snapshots[code]
            return {
                "ts": self.snapshot_ts.isoformat(timespec="seconds")
                if self.snapshot_ts else None,
                "count": len(self.snapshots),
                "sample": sample,
            }
        wanted = codes or list(self.snapshots)
        wanted = wanted[:6000]
        data = {c: self.snapshots[c] for c in wanted if c in self.snapshots}
        missing = [c for c in wanted if c not in self.snapshots]
        return {
            "ts": self.snapshot_ts.isoformat(timespec="seconds")
            if self.snapshot_ts else None,
            "count": len(data),
            "missing": missing[:100],
            "data": data,
        }

    def hot_payload(self, codes: list[str]) -> dict:
        # M7：最多 100 只 × 最近 60 条，避免单请求序列化几十 MB
        return {c: list(self.hot_history.get(c, ()))[-60:]
                for c in codes[:100]}

    async def bars_payload(self, codes: list[str] | None, start_date: str,
                           end_date: str, limit: int) -> list[dict]:
        return await asyncio.to_thread(
            self.store.query_bars, codes, start_date, end_date, limit)
