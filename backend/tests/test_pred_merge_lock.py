"""pred.parquet 并发合并锁。

背景：`merge_signals_into_pred` 是「读全量 → concat → 写临时文件 → os.replace」。
两个不同日期的合并并发时，后写者会用旧快照覆盖前者，**丢行且无报错**。
每日自动推理、单模型补推、多模型串行补推都会写同一个 pred.parquet，必须串行化。

口径：
- 有 Redis（`REDIS_HOST` 已配置）→ Redis 分布式锁 + 进程内锁；
- 无 Redis（本地/单进程测试）→ 仅进程内锁；
- 锁被占 → 重试到超时后抛 `PredMergeLockTimeout`，不静默丢数据。
"""

from __future__ import annotations

import threading
import time

import pandas as pd
import pytest

pytestmark = pytest.mark.unit


class FakeRedis:
    """只实现 redis_lock 用到的 SET NX EX 与 Lua CAS 释放。"""

    def __init__(self, *, always_held: bool = False) -> None:
        self.always_held = always_held
        self._guard = threading.Lock()
        self._values: dict[str, bytes] = {}
        self.set_calls: list[tuple[str, str, int, bool]] = []
        self.eval_calls: list[tuple[str, str]] = []

    def set(self, key: str, value: str, ex: int | None = None, nx: bool = False):
        with self._guard:
            self.set_calls.append((key, value, int(ex or 0), bool(nx)))
            if self.always_held:
                return None
            if nx and key in self._values:
                return None
            self._values[key] = value.encode("utf-8")
            return True

    def eval(self, script: str, numkeys: int, key: str, token: str):
        del script, numkeys
        with self._guard:
            self.eval_calls.append((key, token))
            expected = token.encode("utf-8")
            if self._values.get(key) == expected:
                del self._values[key]
                return 1
            return 0


def _rows(prefix: str, n: int, day: str) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "symbol": f"{prefix}{i:04d}",
                "trade_date": pd.Timestamp(day),
                "label": float("nan"),
                "pred": 0.1 + i / 1000,
                "split": "test",
            }
            for i in range(n)
        ]
    )


def _signals(prefix: str, n: int) -> list[dict]:
    return [{"symbol": f"{prefix}{i:04d}", "score": 0.2 + i / 1000} for i in range(n)]


def test_concurrent_merges_of_different_days_do_not_lose_rows(tmp_path):
    from backend.services.engine.inference import pred_merge

    pf = tmp_path / "pred.parquet"
    _rows("SH60", 100, "2026-01-02").to_parquet(pf, index=False)
    client = FakeRedis()
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def run(day: str, prefix: str) -> None:
        try:
            barrier.wait(timeout=5)
            pred_merge.merge_signals_into_pred(
                pf, [(day, _signals(prefix, 100))], lock_client=client
            )
        except BaseException as exc:  # noqa: BLE001 - 测试要看到任何线程异常
            errors.append(exc)

    threads = [
        threading.Thread(target=run, args=("2026-01-03", "SH61")),
        threading.Thread(target=run, args=("2026-01-04", "SH62")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert not errors, f"并发合并抛错：{errors!r}"
    df = pd.read_parquet(pf)
    assert len(df) == 300, f"两天各 100 行 + 基线 100 行应为 300，实际 {len(df)}"
    dates = set(pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d"))
    assert dates == {"2026-01-02", "2026-01-03", "2026-01-04"}


def test_lock_is_acquired_and_released_with_owner_token(tmp_path):
    from backend.services.engine.inference import pred_merge

    pf = tmp_path / "pred.parquet"
    client = FakeRedis()
    n = pred_merge.merge_signals_into_pred(
        pf,
        [("2026-01-03", _signals("SH60", 2))],
        create_if_missing=True,
        lock_client=client,
    )
    assert n == 2

    key = pred_merge.pred_merge_lock_key(pf)
    assert len(client.set_calls) == 1
    set_key, token, ttl, nx = client.set_calls[0]
    assert set_key == key
    assert token
    assert ttl == pred_merge.LOCK_TTL_SECONDS
    assert nx is True
    # 释放必须带同一 token（CAS），不能盲删他人锁
    assert client.eval_calls == [(key, token)]


def test_lock_held_longer_than_timeout_raises_and_writes_nothing(tmp_path, monkeypatch):
    from backend.services.engine.inference import pred_merge

    monkeypatch.setattr(pred_merge, "LOCK_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(pred_merge, "LOCK_RETRY_INTERVAL_SECONDS", 0.01)
    pf = tmp_path / "pred.parquet"
    client = FakeRedis(always_held=True)

    with pytest.raises(pred_merge.PredMergeLockTimeout):
        pred_merge.merge_signals_into_pred(
            pf,
            [("2026-01-03", _signals("SH60", 2))],
            create_if_missing=True,
            lock_client=client,
        )

    assert client.eval_calls == [], "没拿到锁就不该尝试释放"
    assert not pf.exists(), "没拿到锁就写文件 = 静默丢数据"


def test_lock_is_released_when_merge_raises(tmp_path, monkeypatch):
    from backend.services.engine.inference import pred_merge

    pf = tmp_path / "pred.parquet"
    client = FakeRedis()

    def boom(*args, **kwargs):
        raise RuntimeError("merge failed")

    monkeypatch.setattr(pred_merge, "_merge_locked", boom)
    with pytest.raises(RuntimeError, match="merge failed"):
        pred_merge.merge_signals_into_pred(
            pf,
            [("2026-01-03", _signals("SH60", 1))],
            create_if_missing=True,
            lock_client=client,
        )

    assert len(client.eval_calls) == 1, "合并失败也必须释放锁，否则要等 TTL 过期"


def test_process_local_lock_serializes_without_redis(tmp_path, monkeypatch):
    """未配置 REDIS_HOST 时退化为进程内锁，两个线程也不能丢行。"""
    from backend.services.engine.inference import pred_merge

    monkeypatch.delenv("REDIS_HOST", raising=False)
    monkeypatch.setenv("QM_PRED_MERGE_LOCK", "1")
    pf = tmp_path / "pred.parquet"
    _rows("SH60", 50, "2026-01-02").to_parquet(pf, index=False)

    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def run(day: str, prefix: str) -> None:
        try:
            barrier.wait(timeout=5)
            pred_merge.merge_signals_into_pred(pf, [(day, _signals(prefix, 50))])
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=run, args=("2026-01-03", "SH61")),
        threading.Thread(target=run, args=("2026-01-04", "SH62")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert not errors, f"并发合并抛错：{errors!r}"
    assert len(pd.read_parquet(pf)) == 150


def test_missing_file_without_create_returns_zero_before_locking(tmp_path):
    from backend.services.engine.inference import pred_merge

    pf = tmp_path / "missing.parquet"
    client = FakeRedis()
    assert (
        pred_merge.merge_signals_into_pred(
            pf, [("2026-01-03", _signals("SH60", 1))], lock_client=client
        )
        == 0
    )
    assert client.set_calls == []
    assert not pf.exists()
