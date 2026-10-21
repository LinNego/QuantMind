"""推理分数回写 pred.parquet 的共享工具。

两套推理数据的一致性维护：
- A 套：engine_signal_scores（单日批次，symbol 纯数字，trade_date=T+1 信号生效日）
- B 套：模型目录 pred.parquet（历史分数序列，symbol SH/SZ 前缀式，trade_date=T 数据日）

coverage 缺口判定与个股分数曲线均读 B 套；每日自动推理与一键补全成功后
须把真实分数合并回 B 套，否则 B 套停在训练日，缺口与曲线永不更新。

**并发纪律（2026-10-21）**：合并是「读全量 → concat → 原子替换」。两个不同日期的
合并并发时，后写者会用旧快照覆盖前者，丢行且无报错。每日推理、单模型补推、
多模型串行补推都会写同一文件，因此：

- 同一进程：按 parquet 绝对路径加 `threading.Lock`；
- 跨进程：配置了 `REDIS_HOST` 时再加 Redis 分布式锁（`redis_lock` 唯一原语）；
- 未配置 Redis（本地/单进程测试）只保留进程内锁；
- 锁被占重试到 `LOCK_TIMEOUT_SECONDS` 后抛 `PredMergeLockTimeout`，**不静默丢数据**；
- Redis 连接异常直接抛 `PredMergeLockError`（fail closed），不降级成无锁写。
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path

from backend.shared import redis_lock

logger = logging.getLogger(__name__)

# 合并 200MB 级 parquet 通常 5–20s；300s 给慢盘/大文件留足余量。
LOCK_TTL_SECONDS = 300
LOCK_TIMEOUT_SECONDS = 300.0
LOCK_RETRY_INTERVAL_SECONDS = 0.2

_LOCAL_LOCKS: dict[str, threading.Lock] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()


class PredMergeLockError(RuntimeError):
    """合并锁不可用（Redis 连接/权限/配置错误）。"""


class PredMergeLockTimeout(PredMergeLockError):
    """等待合并锁超时（另一个进程持有时间过长）。"""


def pred_merge_lock_key(parquet_file: Path) -> str:
    """合并锁键：按文件绝对路径哈希，不同模型的 pred.parquet 互不阻塞。"""
    digest = hashlib.sha256(
        str(Path(parquet_file).resolve()).encode("utf-8")
    ).hexdigest()[:32]
    return f"qm:lock:pred_merge:{digest}"


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name) or "").strip() or default)
    except (TypeError, ValueError):
        return default


def _lock_client():
    """构造合并锁 Redis 客户端；未配置 Redis 或显式关闭时返回 None。

    生产 compose 一定有 `REDIS_HOST`；本地单进程/单测没有，退化为进程内锁。
    """
    flag = str(os.getenv("QM_PRED_MERGE_LOCK", "1")).strip().lower()
    if flag in {"0", "false", "off", "no"}:
        return None
    host = str(os.getenv("REDIS_HOST") or "").strip()
    if not host:
        return None

    import redis as _redis

    return _redis.Redis(
        host=host,
        port=_env_int("REDIS_PORT", 6379),
        password=os.getenv("REDIS_PASSWORD") or None,
        db=_env_int("REDIS_DB", 0),
        decode_responses=False,
        socket_connect_timeout=2.0,
        socket_timeout=2.0,
    )


def _local_path_lock(key: str) -> threading.Lock:
    """按锁键取进程内互斥锁（同进程多线程快速路径）。"""
    with _LOCAL_LOCKS_GUARD:
        lock = _LOCAL_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCAL_LOCKS[key] = lock
        return lock


def _acquire_or_raise(client, key: str, token: str) -> None:
    """SET NX EX 重试；被占等到超时，连接异常立即 fail closed。"""
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    while True:
        try:
            ok = redis_lock.acquire(client, key, token, LOCK_TTL_SECONDS)
        except Exception as exc:  # noqa: BLE001 - 连接/权限错误统一转成显式失败
            raise PredMergeLockError(
                f"pred.parquet 合并锁不可用（{key}）：{exc}"
            ) from exc
        if ok:
            return
        if time.monotonic() >= deadline:
            raise PredMergeLockTimeout(
                f"等待 pred.parquet 合并锁超时（{LOCK_TIMEOUT_SECONDS:.0f}s）：{key}"
            )
        time.sleep(LOCK_RETRY_INTERVAL_SECONDS)


def merge_signals_into_pred(
    parquet_file: Path,
    signals_by_date: list[tuple[str, list[dict]]],
    *,
    create_if_missing: bool = False,
    lock_client=None,
) -> int:
    """把 runner 真实推理分数合并进 pred.parquet。

    - symbol 归一为 SH/SZ 前缀式（pred.parquet 约定）
    - 按 (symbol, trade_date) 去重，新分数覆盖旧值
    - 临时文件 + 原子替换，避免并发读到半写文件
    - 默认不凭单日数据创建残缺历史（create_if_missing=False 时文件
      不存在直接返回 0）
    - 合并的同时刷新「按日物化分片」（pred_daily/dt=YYYYMMDD/data.parquet），
      供投研平台按日直读单日截面，避免重复全文件扫描
    - 读改写全程持锁：同进程线程锁 + 跨进程 Redis 锁（见模块 docstring）

    `lock_client` 仅供测试注入；生产不传，按环境变量构造。
    返回本次写入的行数。
    """
    if not parquet_file.is_file() and not create_if_missing:
        return 0

    key = pred_merge_lock_key(parquet_file)
    with _local_path_lock(key):
        client = lock_client if lock_client is not None else _lock_client()
        if client is None:
            return _merge_locked(
                parquet_file, signals_by_date, create_if_missing=create_if_missing
            )

        token = uuid.uuid4().hex
        _acquire_or_raise(client, key, token)
        try:
            return _merge_locked(
                parquet_file, signals_by_date, create_if_missing=create_if_missing
            )
        finally:
            try:
                redis_lock.release(client, key, token)
            except Exception:  # noqa: BLE001 - 释放失败不掩盖合并结果，等 TTL 过期
                logger.warning(
                    "pred.parquet 合并锁释放失败（等待 TTL 过期）：%s",
                    key,
                    exc_info=True,
                )


def _merge_locked(
    parquet_file: Path,
    signals_by_date: list[tuple[str, list[dict]]],
    *,
    create_if_missing: bool,
) -> int:
    """持锁后的实际合并逻辑。"""
    import os
    import tempfile

    import duckdb
    import pandas as pd

    from backend.shared.stock_utils import StockCodeUtil

    if not parquet_file.is_file() and not create_if_missing:
        return 0

    rows = []
    for d, signals in signals_by_date:
        for s in signals or []:
            raw = str(s.get("symbol", "")).strip()
            sym = StockCodeUtil.to_prefix(raw)
            if re.match(r"^(SH|SZ|BJ)\d{6}$", sym):
                pass  # A 股路径不变
            elif raw.endswith((".HK", ".hk")) or raw.isdigit():
                # 港股：pred.parquet symbol 为 4位+.HK（0700.HK），非 A 股前缀式
                sym = StockCodeUtil.to_hk_suffix(
                    raw[:-3] if raw.endswith((".HK", ".hk")) else raw
                )
                if not re.match(r"^\d{4,5}\.HK$", sym, re.IGNORECASE):
                    continue
            else:
                continue
            try:
                score = float(s.get("score"))
            except (TypeError, ValueError):
                continue
            rows.append(
                {
                    "symbol": sym,
                    "trade_date": pd.Timestamp(d),
                    # 推理日无真实标签；用 NaN 保持 label 列 float64 类型不变
                    "label": float("nan"),
                    "pred": score,
                    "split": "test",
                }
            )
    if not rows:
        return 0
    new_df = pd.DataFrame(rows)
    con = duckdb.connect()
    try:
        if parquet_file.is_file():
            existing = con.execute(
                f"SELECT * FROM read_parquet('{str(parquet_file)}')"
            ).df()
            combined = pd.concat([existing, new_df], ignore_index=True)
        else:
            combined = new_df
        combined = combined.drop_duplicates(
            subset=["symbol", "trade_date"], keep="last"
        )
        combined = combined.sort_values(["trade_date", "symbol"]).reset_index(drop=True)
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=str(parquet_file.parent), suffix=".parquet.tmp"
        )
        os.close(tmp_fd)
        combined.to_parquet(tmp_path, index=False)
        os.replace(tmp_path, str(parquet_file))
    finally:
        try:
            con.close()
        except Exception:
            pass

    # 刷新按日物化分片：合并后的当日截面直接落盘，投研平台无需再从全量重提。
    _refresh_pred_daily(parquet_file, new_df)
    return len(new_df)


def _refresh_pred_daily(parquet_file: Path, new_df) -> None:
    """把本次写入的每日截面刷新到 pred_daily/dt=YYYYMMDD/data.parquet。

    物化分片是投研平台的读取加速层（惰性物化 + 推理合并后主动刷新），
    仅保存 symbol/pred 两列，已剔除 B 股/北交所/指数（与投研口径一致）。
    """
    import os
    import tempfile

    import pandas as pd

    if new_df.empty or "trade_date" not in new_df.columns:
        return
    daily_dir = parquet_file.parent / "pred_daily"
    for d, grp in new_df.groupby(new_df["trade_date"].dt.normalize()):
        date_str = d.strftime("%Y-%m-%d")
        rows = []
        for _, r in grp.iterrows():
            sym = str(r.get("symbol", ""))
            if not re.match(r"^(SH|SZ|BJ)\d{6}$", sym):
                continue
            if sym.startswith("SH000") or sym.startswith("SZ399"):
                continue
            if sym.startswith("SH900") or sym.startswith("SZ200"):
                continue
            if sym.startswith("BJ"):
                continue
            pred_val = r.get("pred")
            if pred_val is None or (isinstance(pred_val, float) and pd.isna(pred_val)):
                continue
            rows.append({"symbol": sym, "score": float(pred_val)})
        if not rows:
            continue
        part = daily_dir / f"dt={date_str.replace('-', '')}" / "data.parquet"
        part.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=str(part.parent), suffix=".parquet.tmp")
        os.close(tmp_fd)
        pd.DataFrame(rows).to_parquet(tmp_path, index=False)
        os.replace(tmp_path, str(part))
