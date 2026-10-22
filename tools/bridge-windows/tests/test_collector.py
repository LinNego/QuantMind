"""采集器单元/集成测试（脚本式，运行：python3 tests/test_collector.py）。

覆盖：分块、交易窗口、1m 聚合、SQLite 留存、配置解析、熔断、HTTP 鉴权、
pricevol 一轮采集。不依赖真实桥/通达信。
"""
import asyncio
import os
import sys
import tempfile
from datetime import datetime
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Windows 控制台默认 GBK，打印 ✅ 会 UnicodeEncodeError；与 main.py 同样收口 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

from src.collector.aggregator import BarAggregator  # noqa: E402
from src.collector.config import CollectorConfig, load_config  # noqa: E402
from src.collector.server import build_app  # noqa: E402
from src.collector.service import (  # noqa: E402
    CircuitBreaker,
    CollectorService,
    chunked,
    is_full_market_window,
    is_trading_time,
)
from src.collector.store import MarketStore  # noqa: E402


def test_chunked():
    assert list(chunked([1, 2, 3, 4, 5], 2)) == [[1, 2], [3, 4], [5]]
    assert list(chunked([], 10)) == []
    print("  ✅ chunked")


def test_trading_windows():
    assert is_trading_time(datetime(2026, 10, 22, 9, 20))
    assert is_trading_time(datetime(2026, 10, 22, 13, 0))
    assert not is_trading_time(datetime(2026, 10, 22, 12, 0))
    assert not is_trading_time(datetime(2026, 10, 24, 10, 0))  # 周六
    assert not is_full_market_window(datetime(2026, 10, 22, 9, 26))
    assert is_full_market_window(datetime(2026, 10, 22, 9, 31))
    assert not is_full_market_window(datetime(2026, 10, 22, 14, 58))
    print("  ✅ 交易时段/撮合窗")


def test_aggregator():
    agg = BarAggregator()
    code = "600519.SH"
    t1 = datetime(2026, 10, 22, 9, 30, 5)
    t2 = datetime(2026, 10, 22, 9, 30, 35)
    t3 = datetime(2026, 10, 22, 9, 31, 5)
    assert agg.on_sample(code, 100.0, 1000.0, now=t1) == []
    assert agg.on_sample(code, 101.0, 1100.0, now=t2) == []
    bars = agg.on_sample(code, 102.0, 1200.0, now=t3)
    assert len(bars) == 1, bars
    b = bars[0]
    assert b["minute"] == "0930" and b["open"] == 100.0 and b["high"] == 101.0
    assert b["low"] == 100.0 and b["close"] == 101.0
    assert b["volume"] == 1100.0, b
    assert b["samples"] == 2 and b["amount"] is None
    stale = agg.flush_stale(now=datetime(2026, 10, 22, 9, 32, 0))
    assert len(stale) == 1 and stale[0]["minute"] == "0931"
    assert stale[0]["volume"] == 100.0, stale
    # 盘中启动：首根 bar 不应把开盘至今的累计量全灌进来
    agg2 = BarAggregator()
    agg2.on_sample(code, 10.0, 5000.0, now=datetime(2026, 10, 22, 10, 0, 5))
    out = agg2.flush_stale(now=datetime(2026, 10, 22, 10, 1, 12))
    assert out[0]["volume"] == 0.0, out
    # 价格为 0（停牌）忽略
    assert agg2.on_sample(code, 0.0, 1.0, now=datetime(2026, 10, 22, 10, 2, 0)) == []
    # 跨日：昨日累计量不能当今日基线，新一天首根量必须 > 0
    agg3 = BarAggregator()
    agg3.on_sample(code, 10.0, 1000.0, now=datetime(2026, 10, 22, 14, 59, 55))
    agg3.on_sample(code, 11.0, 100.0, now=datetime(2026, 10, 23, 9, 30, 5))
    _next_day = agg3.flush_stale(now=datetime(2026, 10, 23, 9, 31, 10))
    _d2 = [b for b in _next_day if b["trade_date"] == "20261023"]
    assert _d2 and _d2[0]["volume"] == 100.0, _next_day
    print("  ✅ 1m 聚合（OHLC/量差/盘中启动/停牌/跨日）")


def test_store():
    tmp = tempfile.mkdtemp()
    store = MarketStore(os.path.join(tmp, "bars.db"))
    bars = [
        {"trade_date": "20261022", "code": "600519.SH", "minute": "0930",
         "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 100,
         "amount": None, "samples": 12, "source": "pricevol"},
        {"trade_date": "20261022", "code": "000001.SZ", "minute": "0930",
         "open": 3, "high": 4, "low": 2.5, "close": 3.5, "volume": 200,
         "amount": None, "samples": 12, "source": "pricevol"},
        {"trade_date": "20200101", "code": "600519.SH", "minute": "0930",
         "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1,
         "amount": None, "samples": 1, "source": "pricevol"},
    ]
    assert store.upsert_bars(bars) == 3
    # 唯一键更新
    bars[0]["close"] = 9.9
    store.upsert_bars([bars[0]])
    got = store.query_bars(["600519.SH"], "20261022", "20261022")
    assert len(got) == 1 and got[0]["close"] == 9.9, got
    assert store.stats()["rows"] == 3
    store.save_universe(["600519.SH", "000001.SZ"])
    assert store.load_universe() == ["000001.SZ", "600519.SH"]
    deleted = store.prune(90)
    assert deleted == 1, deleted
    assert store.stats()["rows"] == 2
    store.close()
    print("  ✅ SQLite 写入/查询/唯一键/留存/清单")


def test_config():
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "config.yaml")
    with open(path, "w", encoding="utf-8") as f:
        f.write("auth:\n  token: \"%s\"\nchannels:\n  http:\n    port: 8550\n"
                % ("a" * 64))
    cfg = load_config(path, tmp)
    assert cfg.bridge_url == "http://127.0.0.1:8550"
    assert cfg.token == "a" * 64 and cfg.port == 8560
    # 缺 token 必须 fail-fast
    path2 = os.path.join(tmp, "empty.yaml")
    open(path2, "w", encoding="utf-8").close()
    saved = {k: os.environ.pop(k, None) for k in ("BRIDGE_TOKEN", "COLLECTOR_TOKEN")}
    try:
        try:
            load_config(path2, tmp)
            raise AssertionError("缺 token 未报错")
        except ValueError:
            pass
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
    print("  ✅ 配置解析/缺 token fail-fast")


def test_circuit_breaker():
    cb = CircuitBreaker(threshold=2, pause_s=10.0)
    cb.record_error("e1", now=100.0)
    assert not cb.is_paused(now=100.0)
    cb.record_error("e2", now=100.0)
    assert cb.is_paused(now=101.0)
    assert not cb.is_paused(now=111.0)
    cb.record_success()
    assert cb.consecutive == 0 and cb.total_errors == 2
    print("  ✅ 熔断状态机")


class _FakeService:
    def __init__(self, token="t" * 16):
        self.config = SimpleNamespace(token=token, host="127.0.0.1", port=0)

    async def health(self):
        return {"status": "ok", "version": "test"}

    def snapshot_payload(self, codes=None, include_all=False):
        return {"ts": "2026-10-22T10:00:00+08:00", "count": 1,
                "data": {"600519.SH": {"Now": "100"}}}

    def hot_payload(self, codes):
        return {c: [{"Now": "100"}] for c in codes}

    async def bars_payload(self, codes, start_date, end_date, limit):
        return [{"trade_date": "20261022", "code": "600519.SH",
                 "minute": "0930", "close": 100.0}]


async def _http_checks():
    from aiohttp.test_utils import TestClient, TestServer
    fake = _FakeService()
    async with TestClient(TestServer(build_app(fake))) as client:
        r = await client.get("/health")
        assert r.status == 200 and (await r.json())["data"]["status"] == "ok"
        r = await client.get("/snapshot")
        assert r.status == 401, r.status
        r = await client.get("/snapshot", headers={"Authorization": "Bearer " + fake.config.token})
        assert r.status == 200
        body = await r.json()
        assert body["data"]["count"] == 1
        r = await client.get("/bars?codes=600519.SH&limit=10",
                             headers={"Authorization": "Bearer " + fake.config.token})
        assert r.status == 200 and (await r.json())["data"]["count"] == 1
        r = await client.get("/bars?codes=600519.SH&limit=abc",
                             headers={"Authorization": "Bearer " + fake.config.token})
        assert r.status == 400


def test_http():
    asyncio.run(_http_checks())
    print("  ✅ HTTP 鉴权/health/snapshot/bars")


class _FakeClient:
    def __init__(self):
        self.calls = []

    async def get_pricevol(self, codes):
        self.calls.append(("pricevol", list(codes)))
        return {
            "600519.SH": {"LastClose": "99", "Now": "100.0", "Volume": "1000"},
            "000001.SZ": {"LastClose": "9", "Now": "10.0", "Volume": "2000"},
        }

    async def get_market_snapshot(self, code):
        return {"Now": "100.0", "Amount": "10000", "Volume": "1000", "Buyp": ["99.9"]}


async def _cycle_checks():
    tmp = tempfile.mkdtemp()
    cfg = CollectorConfig(
        bridge_url="http://127.0.0.1:8550", bridge_token="a" * 64,
        host="127.0.0.1", port=0, token="a" * 64,
        db_path=os.path.join(tmp, "m.db"), log_dir=tmp,
        snapshot_interval=5.0, hot_interval=3.0, snapshot_chunk=1,
        hot_set=("600519.SH",), retention_days=90,
        slow_call_ms=3000.0, error_pause_s=30.0,
    )
    svc = CollectorService(cfg)
    svc.client = _FakeClient()
    svc.universe = ["600519.SH", "000001.SZ"]
    await svc._collect_snapshot_cycle()
    assert len(svc.snapshots) == 2, svc.snapshots
    assert svc.snapshot_ts is not None
    assert len(svc.client.calls) == 2  # chunk=1 → 2 次
    await svc._collect_hot_one("600519.SH", asyncio.Semaphore(1))
    assert svc.snapshots["600519.SH"]["_source"] == "snapshot"
    assert len(svc.hot_history["600519.SH"]) == 1
    # 落盘：封口当前分钟并强制刷掉
    svc._pending_bars.extend(svc.aggregator.flush_all())
    await svc._flush_pending(force=True)
    rows = svc.store.query_bars(["600519.SH"])
    assert len(rows) >= 1, rows
    assert svc.snapshots["600519.SH"]["Now"] == "100.0"
    svc.store.close()


def test_cycle():
    asyncio.run(_cycle_checks())
    print("  ✅ pricevol 一轮采集/热集快照/落盘")


def main() -> int:
    print("=== 采集器测试 ===")
    test_chunked()
    test_trading_windows()
    test_aggregator()
    test_store()
    test_config()
    test_circuit_breaker()
    test_http()
    test_cycle()
    print("\n=== 全部测试通过 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
