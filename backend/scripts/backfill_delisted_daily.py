#!/usr/bin/env python3
"""回补退市 A 股日线到 QuantDB 的日线/估值分区。

背景
----
QuantDB 的 ``1_kline_data/daily_*`` 与 ``5_technical_derived/valuation`` 只含
当前在市标的。2016 年后退市的 250 只 A 股不在历史面板里，因子精选的
K0-a 幸存者偏差硬闸因此失败（2016 年在册、后来退市的 233 只里只捕获 1 只）。

本脚本从 baostock 拉取退市股全生命期日线，按**已有分区**合并：

- ``daily_unadjusted``：不复权
- ``daily_forward``：baostock 前复权（qfq）
- ``daily_backward``：qfq × (raw_ref / qfq_ref)，参考日取标的在面板内的
  首个交易日，使后复权序列在参考日与不复权价对齐（QuantDB 后复权以
  2016-01-04 为锚）
- ``valuation``：close=不复权；float_mv=close × (volume / (turn/100))；
  pe_ttm/pb/ps_ttm 取 baostock；其余基本面列留空（退市股财报回补另案）

口径
----
- baostock 成交额单位元 → QuantDB 万元（/1e4）
- 只保留 ``tradestatus=1`` 且 ``volume>0`` 的交易日，与 QuantDB 面板不含停牌行一致
- 只新增缺失 symbol；既有行绝不覆盖（写盘前校验既有 symbol 集合不变）

安全
----
- 默认 dry-run，``--apply`` 才落盘
- 每个分区原子写（临时文件 + ``os.replace``），写后校验行数与既有 symbol 集合
- manifest JSONL 逐分区记录新增 symbol、行数与前后 sha256
- baostock 原始响应缓存到 ``<data-root>/factor_research/selection_v2_20261021/delisted_raw/``
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import time
from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger("backfill_delisted_daily")

OHLCV_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "volume", "amount")
VALUATION_COLUMNS: tuple[str, ...] = (
    "symbol",
    "time",
    "close",
    "total_capital",
    "circulating_capital",
    "total_mv",
    "float_mv",
    "net_profit_ttm",
    "revenue_ttm",
    "equity",
    "annual_net_profit",
    "pe_ttm",
    "pe_static",
    "pb",
    "ps_ttm",
    "dividend_rate",
)
DATASET_PARTS: dict[str, str] = {
    "daily_unadjusted": "1_kline_data/daily_unadjusted",
    "daily_forward": "1_kline_data/daily_forward",
    "daily_backward": "1_kline_data/daily_backward",
    "valuation": "5_technical_derived/valuation",
}
BACKWARD_ANCHOR = pd.Timestamp("2016-01-04")
BAOSTOCK_FIELDS = (
    "date,open,high,low,close,preclose,volume,amount,turn,tradestatus,"
    "peTTM,pbMRQ,psTTM,pcfNcfTTM,isST"
)
A_SHARE_PREFIXES = ("sh.6", "sz.0", "sz.3")
# baostock 的 socketutil.send_msg 是裸 recv 循环，没有超时：网络半开时永久挂起。
# 登录后由本脚本给底层 socket 装上超时，并由 _reset_connection() 在异常后重连。
BAOSTOCK_SOCKET_TIMEOUT_SECONDS = 60.0
_LOGGED_IN = False


# ------------------------------------------------------------------ 纯函数


def baostock_code_to_symbol(code: str) -> str:
    """``sh.600005`` → ``600005.SH``（QuantDB/PG 前缀式）。"""
    prefix, _, digits = str(code).strip().partition(".")
    suffix = {"sh": "SH", "sz": "SZ", "bj": "BJ"}.get(prefix.lower())
    if not suffix or not digits:
        raise ValueError(f"无法识别的 baostock 代码: {code!r}")
    return f"{digits}.{suffix}"


def empty_ohlcv_frame() -> pd.DataFrame:
    """带固定列的空白 OHLCV 帧，避免空分区写出 object dtype。"""
    return pd.DataFrame(
        {
            "symbol": pd.Series(dtype="object"),
            "time": pd.Series(dtype="datetime64[ns]"),
            **{col: pd.Series(dtype="float64") for col in OHLCV_COLUMNS},
        }
    )


def _empty_history_frame() -> pd.DataFrame:
    frame = empty_ohlcv_frame()
    for col in ("turn", "pe_ttm", "pb", "ps_ttm", "pcf_ncf_ttm"):
        frame[col] = pd.Series(dtype="float64")
    return frame


def normalize_history(
    rows: Sequence[Sequence[str]], fields: Sequence[str], symbol: str
) -> pd.DataFrame:
    """baostock 行 → QuantDB 日线口径（只留可交易行，成交额元→万元）。"""
    if not rows:
        return _empty_history_frame()

    frame = pd.DataFrame(rows, columns=list(fields))
    frame.columns = [str(col).strip() for col in frame.columns]
    rename = {
        "date": "time",
        "peTTM": "pe_ttm",
        "pbMRQ": "pb",
        "psTTM": "ps_ttm",
        "pcfNcfTTM": "pcf_ncf_ttm",
    }
    frame = frame.rename(
        columns={k: v for k, v in rename.items() if k in frame.columns}
    )
    frame["symbol"] = symbol
    frame["time"] = pd.to_datetime(frame["time"], errors="coerce")

    numeric = [
        *OHLCV_COLUMNS,
        "turn",
        "pe_ttm",
        "pb",
        "ps_ttm",
        "pcf_ncf_ttm",
    ]
    for col in numeric:
        if col not in frame.columns:
            frame[col] = np.nan
        frame[col] = pd.to_numeric(
            frame[col].replace({"": np.nan, "None": np.nan}), errors="coerce"
        )

    if "tradestatus" not in frame.columns:
        frame["tradestatus"] = "1"
    status = frame["tradestatus"].astype(str).str.strip().str.removesuffix(".0")
    frame = frame[(status == "1") & (frame["volume"].fillna(0) > 0)]
    frame = (
        frame.dropna(subset=["time"])
        .sort_values("time")
        .drop_duplicates("time", keep="last")
    )
    if frame.empty:
        return _empty_history_frame()

    frame["amount"] = frame["amount"] / 1e4
    out = frame[
        [
            "symbol",
            "time",
            *OHLCV_COLUMNS,
            *("turn", "pe_ttm", "pb", "ps_ttm", "pcf_ncf_ttm"),
        ]
    ]
    return out.reset_index(drop=True)


def build_backward(raw: pd.DataFrame, qfq: pd.DataFrame) -> pd.DataFrame:
    """qrq × 参考日缩放 → 与 QuantDB 后复权锚点对齐的序列。"""
    if raw is None or qfq is None or raw.empty or qfq.empty:
        return empty_ohlcv_frame()

    merged = raw[["symbol", "time", *OHLCV_COLUMNS]].merge(
        qfq[["time", *OHLCV_COLUMNS]], on="time", suffixes=("_raw", "_qfq")
    )
    if merged.empty:
        return empty_ohlcv_frame()

    anchored = merged[merged["time"] >= BACKWARD_ANCHOR]
    reference = (anchored if not anchored.empty else merged).iloc[0]
    qfq_close = float(reference["close_qfq"])
    scale = float(reference["close_raw"]) / qfq_close if qfq_close > 0 else np.nan

    out = merged[["symbol", "time"]].copy()
    for col in OHLCV_COLUMNS[:-2]:
        out[col] = merged[f"{col}_qfq"].to_numpy(dtype=float) * scale
    out["volume"] = merged["volume_raw"].to_numpy(dtype=float)
    out["amount"] = merged["amount_raw"].to_numpy(dtype=float)
    return out[["symbol", "time", *OHLCV_COLUMNS]].reset_index(drop=True)


def build_valuation(raw: pd.DataFrame) -> pd.DataFrame:
    """日线 → 估值分区。流通股本由 volume / 换手率反推，float_mv=close×流通股本。"""
    if raw is None or raw.empty:
        return pd.DataFrame(
            {col: pd.Series(dtype="float64") for col in VALUATION_COLUMNS}
        )

    turn = pd.to_numeric(raw["turn"], errors="coerce")
    volume = pd.to_numeric(raw["volume"], errors="coerce")
    circulating = np.where(turn > 0, volume / (turn / 100.0), np.nan)
    close = pd.to_numeric(raw["close"], errors="coerce")

    frame = pd.DataFrame(
        {
            "symbol": raw["symbol"].to_numpy(),
            "time": pd.to_datetime(raw["time"]).to_numpy(),
            "close": close.to_numpy(),
            "total_capital": np.nan,
            "circulating_capital": circulating,
            "total_mv": np.nan,
            "float_mv": close.to_numpy(dtype=float) * circulating,
            "net_profit_ttm": np.nan,
            "revenue_ttm": np.nan,
            "equity": np.nan,
            "annual_net_profit": np.nan,
            "pe_ttm": pd.to_numeric(raw.get("pe_ttm"), errors="coerce").to_numpy(),
            "pe_static": np.nan,
            "pb": pd.to_numeric(raw.get("pb"), errors="coerce").to_numpy(),
            "ps_ttm": pd.to_numeric(raw.get("ps_ttm"), errors="coerce").to_numpy(),
            "dividend_rate": np.nan,
        }
    )
    return frame[list(VALUATION_COLUMNS)]


def merge_partition(
    existing: pd.DataFrame, new: pd.DataFrame
) -> tuple[pd.DataFrame, int]:
    """只追加缺失 symbol 的行；既有行保持原样。返回 (合并帧, 新增行数)。"""
    if new is None or new.empty:
        return existing, 0
    if existing is None or existing.empty:
        return new.copy(), len(new)

    existing_symbols = set(existing["symbol"].astype(str))
    add = new[~new["symbol"].astype(str).isin(existing_symbols)]
    if add.empty:
        return existing, 0
    merged = pd.concat([existing, add], ignore_index=True)
    return merged, len(add)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_partition_atomic(
    target: Path, frame: pd.DataFrame, *, existing_symbols: set[str]
) -> None:
    """临时文件 + replace；写后校验既有 symbol 未丢失、行数一致。"""
    tmp = target.with_name(f"{target.stem}.delisted.tmp.parquet")
    try:
        frame.to_parquet(tmp, index=False)
        check = pd.read_parquet(tmp, columns=["symbol"])
        after = set(check["symbol"].astype(str))
        if not existing_symbols <= after:
            raise RuntimeError(f"{target}: 既有 symbol 在写盘后丢失")
        if len(check) != len(frame):
            raise RuntimeError(f"{target}: 行数校验失败 {len(check)} != {len(frame)}")
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


# ------------------------------------------------------------------ baostock


class _GuardedSocket:
    """baostock socket 代理：对端关闭时 ``recv`` 返回空串，原实现会死循环。

    ``baostock.util.socketutil.send_msg`` 的收包循环是::

        while True:
            recv = default_socket.recv(8192)
            receive += recv
            if receive[-13:] == END_MARKER: break

    连接被服务端关闭后 ``recv`` 立刻返回 ``b""``，循环既拿不到分隔符、也不阻塞
    （100% CPU 空转）。代理把空包翻译成 ``ConnectionError``，交给上层重连重试。
    """

    def __init__(self, inner):
        self._inner = inner
        self.closed_error: ConnectionError | None = None

    def send(self, data):
        return self._inner.send(data)

    def recv(self, bufsize):
        data = self._inner.recv(bufsize)
        if not data:
            self.closed_error = ConnectionError("baostock socket 已被对端关闭")
            raise self.closed_error
        return data

    def settimeout(self, value):
        return self._inner.settimeout(value)

    def close(self):
        return self._inner.close()

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _socket_context():
    """取 baostock 的模块级 context；测试用假 baostock 时返回 None。"""
    try:
        from baostock.common import context
    except Exception:  # noqa: BLE001 - 假模块/版本差异都不该阻断主流程
        return None
    return context


def _current_socket_guard() -> _GuardedSocket | None:
    context = _socket_context()
    sock = getattr(context, "default_socket", None) if context is not None else None
    return sock if isinstance(sock, _GuardedSocket) else None


def _raise_if_socket_closed() -> None:
    """翻页途中对端断开时 ``ResultData.next()`` 只返回 False（静默截断）。

    守卫在空包时记录了异常；这里把它抬出来，交给 ``_fetch_history`` 重连重试，
    避免把半截历史当完整数据写进缓存。
    """
    guard = _current_socket_guard()
    if guard is not None and guard.closed_error is not None:
        error = guard.closed_error
        guard.closed_error = None
        raise error


def _apply_socket_timeout(
    timeout: float = BAOSTOCK_SOCKET_TIMEOUT_SECONDS,
) -> None:
    """给 baostock 底层 socket 装超时 + 空包守卫，避免永久挂起/空转。"""
    context = _socket_context()
    sock = getattr(context, "default_socket", None) if context is not None else None
    if sock is None:
        return
    if not isinstance(sock, _GuardedSocket):
        sock = _GuardedSocket(sock)
        try:
            context.default_socket = sock
        except AttributeError as exc:
            log.warning("baostock context 不可写，跳过 socket 守卫: %s", exc)
            return
    if hasattr(sock, "settimeout"):
        try:
            sock.settimeout(timeout)
        except OSError as exc:  # 已关闭的 socket：下次调用会走重连
            log.warning("baostock socket 设置超时失败: %s", exc)


def _reset_connection() -> None:
    """超时/断连后丢弃旧 socket，下次 ``_ensure_login()`` 重新登录。

    不走 ``bs.logout()``：它也要在同一根坏 socket 上收发，可能再次挂住。
    """
    global _LOGGED_IN
    context = _socket_context()
    sock = getattr(context, "default_socket", None) if context is not None else None
    if sock is not None:
        try:
            sock.close()
        except OSError:
            pass
        try:
            context.default_socket = None
        except AttributeError:
            pass
    _LOGGED_IN = False


def _ensure_login() -> None:
    global _LOGGED_IN
    if _LOGGED_IN:
        return
    import baostock as bs

    result = bs.login()
    if result.error_code != "0":
        raise RuntimeError(f"baostock 登录失败: {result.error_code} {result.error_msg}")
    _LOGGED_IN = True
    _apply_socket_timeout()


def _logout() -> None:
    global _LOGGED_IN
    if not _LOGGED_IN:
        return
    import baostock as bs

    bs.logout()
    _LOGGED_IN = False


def _fetch_stock_basic_rows() -> tuple[list[list[str]], list[str]]:
    """query_stock_basic 全量翻页；result.data 只有首页（默认 2000 行）。"""
    import baostock as bs

    _ensure_login()
    result = bs.query_stock_basic()
    if result.error_code != "0":
        raise RuntimeError(
            f"query_stock_basic 失败: {result.error_code} {result.error_msg}"
        )
    rows = []
    while result.next():
        rows.append(result.get_row_data())
    _raise_if_socket_closed()
    return rows, list(result.fields)


def load_delisted_universe(start: str, end: str) -> pd.DataFrame:
    """退市 A 股清单（outDate ∈ [start, end]），返回 symbol/code/name/ipo/out。"""
    last_error: Exception | None = None
    rows: list[list[str]] = []
    fields: list[str] = []
    for attempt in range(3):
        try:
            rows, fields = _fetch_stock_basic_rows()
            break
        except Exception as exc:  # noqa: BLE001 - 网络抖动/超时：重连后重试
            last_error = exc
            log.warning("query_stock_basic 第 %d 次失败: %s", attempt + 1, exc)
            _reset_connection()
            time.sleep(1.0 * (attempt + 1))
    else:
        raise RuntimeError(f"query_stock_basic 拉取失败: {last_error}")

    frame = pd.DataFrame(rows, columns=fields)
    frame = frame[frame["type"].astype(str) == "1"].copy()
    frame["ipo"] = pd.to_datetime(frame["ipoDate"], errors="coerce")
    frame["out"] = pd.to_datetime(frame["outDate"], errors="coerce")
    start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
    frame = frame[
        frame["out"].notna()
        & (frame["out"] >= start_ts)
        & (frame["out"] <= end_ts)
        & (frame["ipo"].notna())
        & (frame["ipo"] <= end_ts)
        & frame["code"].astype(str).str.startswith(A_SHARE_PREFIXES)
    ].copy()
    frame["symbol"] = frame["code"].map(baostock_code_to_symbol)
    return (
        frame[["symbol", "code", "code_name", "ipo", "out"]]
        .sort_values("out")
        .reset_index(drop=True)
    )


def _fetch_history(
    code: str, symbol: str, start: str, end: str, adjustflag: str
) -> pd.DataFrame:
    import baostock as bs

    last_error: Exception | None = None
    for attempt in range(3):
        try:
            _ensure_login()  # 上次失败可能已丢弃 socket，这里按需重连
            result = bs.query_history_k_data_plus(
                code,
                BAOSTOCK_FIELDS,
                start_date=start,
                end_date=end,
                frequency="d",
                adjustflag=adjustflag,
            )
            if result.error_code != "0":
                raise RuntimeError(f"{result.error_code} {result.error_msg}")
            # result.data 只是当前页（默认 2000 行），长跨度单票必须翻页
            rows = []
            while result.next():
                rows.append(result.get_row_data())
            _raise_if_socket_closed()
            return normalize_history(rows, result.fields, symbol)
        except Exception as exc:  # noqa: BLE001 - 网络抖动/超时：重连后重试
            last_error = exc
            log.warning(
                "%s adjustflag=%s 第 %d 次失败: %s", code, adjustflag, attempt + 1, exc
            )
            _reset_connection()
            time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"{code} adjustflag={adjustflag} 拉取失败: {last_error}")


def fetch_stock_pair(
    code: str,
    symbol: str,
    start: str,
    end: str,
    *,
    cache_dir: Path | None = None,
    refresh: bool = False,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """拉取 (不复权, 前复权) 日线；有缓存且未 refresh 时直接读缓存。"""
    if cache_dir is not None:
        raw_path = cache_dir / f"{symbol}_raw.parquet"
        qfq_path = cache_dir / f"{symbol}_qfq.parquet"
        if not refresh and raw_path.exists() and qfq_path.exists():
            return pd.read_parquet(raw_path), pd.read_parquet(qfq_path)
    else:
        raw_path = qfq_path = None

    raw = _fetch_history(code, symbol, start, end, "3")
    qfq = _fetch_history(code, symbol, start, end, "2")

    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        raw.to_parquet(raw_path, index=False)
        qfq.to_parquet(qfq_path, index=False)
    return raw, qfq


# ------------------------------------------------------------------ 编排


def _frames_by_dataset(
    raw: pd.DataFrame, qfq: pd.DataFrame, datasets: Iterable[str]
) -> dict[str, pd.DataFrame]:
    names = tuple(datasets)
    # 按需构造：只算请求的数据集，避免为 daily_* 计算 valuation 时因缺
    # turn/pe_ttm 等列而失败（delisted_daily_store 只取三张 K 线表）。
    builders = {
        "daily_unadjusted": lambda: raw[["symbol", "time", *OHLCV_COLUMNS]],
        "daily_forward": lambda: qfq[["symbol", "time", *OHLCV_COLUMNS]],
        "daily_backward": lambda: build_backward(raw, qfq),
        "valuation": lambda: build_valuation(raw),
    }
    return {name: builders[name]() for name in names}


def _manifest_record(
    dataset: str,
    dt: str,
    target: Path,
    added: int,
    before_rows: int,
    after_rows: int,
    before_hash: str | None,
    after_hash: str | None,
) -> dict:
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "dataset": dataset,
        "dt": dt,
        "path": str(target),
        "added_rows": int(added),
        "rows_before": int(before_rows),
        "rows_after": int(after_rows),
        "sha256_before": before_hash,
        "sha256_after": after_hash,
    }


def run_backfill(
    data_root: str | Path,
    *,
    start: str = "2016-01-01",
    end: str | None = None,
    codes: Sequence[str] | None = None,
    datasets: Sequence[str] = tuple(DATASET_PARTS),
    apply: bool = False,
    cache_dir: str | Path | None = None,
    manifest_path: str | Path | None = None,
    refresh_cache: bool = False,
) -> dict:
    """回补主流程。默认 dry-run；apply=True 才写盘并追加 manifest。"""
    root = Path(data_root)
    end = end or date.today().isoformat()
    datasets = tuple(datasets)
    unknown = set(datasets) - set(DATASET_PARTS)
    if unknown:
        raise ValueError(f"未知数据集: {sorted(unknown)}")

    research_dir = root / "factor_research" / "selection_v2_20261021"
    cache = Path(cache_dir) if cache_dir else research_dir / "delisted_raw"
    manifest = (
        Path(manifest_path)
        if manifest_path
        else research_dir / "delisted_backfill_manifest.jsonl"
    )

    universe = load_delisted_universe(start, end)
    if codes:
        wanted = set(codes)
        universe = universe[
            universe["symbol"].isin(wanted) | universe["code"].isin(wanted)
        ].reset_index(drop=True)
    if universe.empty:
        return {
            "stocks_requested": 0,
            "stocks_fetched": 0,
            "failed_stocks": [],
            "added_rows": 0,
            "skipped_missing_partition": 0,
            "applied": False,
            "datasets": {name: {"added_rows": 0, "partitions": 0} for name in datasets},
            "manifest_path": str(manifest),
        }

    per_dataset: dict[str, dict[str, list[pd.DataFrame]]] = {
        name: defaultdict(list) for name in datasets
    }
    failed: list[dict[str, str]] = []
    fetched = 0
    for index, row in enumerate(universe.itertuples(index=False), start=1):
        fetch_start = max(pd.Timestamp(start), pd.Timestamp(row.ipo)).strftime(
            "%Y-%m-%d"
        )
        fetch_end = min(pd.Timestamp(end), pd.Timestamp(row.out)).strftime("%Y-%m-%d")
        if fetch_start > fetch_end:
            continue
        try:
            raw, qfq = fetch_stock_pair(
                row.code,
                row.symbol,
                fetch_start,
                fetch_end,
                cache_dir=cache,
                refresh=refresh_cache,
            )
        except Exception as exc:
            log.error("%s 拉取失败: %s", row.symbol, exc)
            failed.append({"symbol": row.symbol, "error": str(exc)})
            continue
        if raw.empty or qfq.empty:
            log.warning(
                "%s 无可用日线（raw=%d qfq=%d）", row.symbol, len(raw), len(qfq)
            )
            continue
        fetched += 1
        for name, frame in _frames_by_dataset(raw, qfq, datasets).items():
            if frame.empty:
                continue
            for dt, group in frame.groupby(frame["time"].dt.strftime("%Y%m%d")):
                per_dataset[name][str(dt)].append(group)
        if index % 25 == 0:
            log.info(
                "已拉取 %d/%d（成功 %d，失败 %d）",
                index,
                len(universe),
                fetched,
                len(failed),
            )

    added_total = 0
    skipped_missing = 0
    dataset_summary: dict[str, dict[str, int]] = {
        name: {"added_rows": 0, "partitions": 0} for name in datasets
    }
    records: list[dict] = []
    for name in datasets:
        for dt in sorted(per_dataset[name]):
            target = root / DATASET_PARTS[name] / f"dt={dt}" / "data.parquet"
            if not target.exists():
                skipped_missing += 1
                continue
            existing = pd.read_parquet(target)
            new = pd.concat(per_dataset[name][dt], ignore_index=True)
            merged, added = merge_partition(existing, new)
            if added == 0:
                continue
            added_total += added
            dataset_summary[name]["added_rows"] += added
            dataset_summary[name]["partitions"] += 1
            before_hash = _sha256(target) if apply else None
            after_hash = None
            if apply:
                write_partition_atomic(
                    target,
                    merged,
                    existing_symbols=set(existing["symbol"].astype(str)),
                )
                after_hash = _sha256(target)
            records.append(
                _manifest_record(
                    name,
                    dt,
                    target,
                    added,
                    len(existing),
                    len(merged),
                    before_hash,
                    after_hash,
                )
            )
            if len(records) % 100 == 0:
                log.info("已处理 %d 个分区（新增 %d 行）", len(records), added_total)

    if apply and records:
        manifest.parent.mkdir(parents=True, exist_ok=True)
        with manifest.open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(
                    json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
                )

    summary = {
        "stocks_requested": len(universe),
        "stocks_fetched": fetched,
        "failed_stocks": failed,
        "added_rows": added_total,
        "skipped_missing_partition": skipped_missing,
        "applied": bool(apply),
        "datasets": dataset_summary,
        "manifest_path": str(manifest),
    }
    log.info("完成: %s", json.dumps(summary, ensure_ascii=False))
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="回补退市 A 股日线到 QuantDB 分区")
    parser.add_argument(
        "--data-root",
        default=os.getenv("QM_QUANTDB_DATA_DIR", "/data/quantdb"),
        help="QuantDB 根目录（默认 QM_QUANTDB_DATA_DIR 或 /data/quantdb）",
    )
    parser.add_argument("--start", default="2016-01-01", help="起始日 YYYY-MM-DD")
    parser.add_argument(
        "--end", default=date.today().isoformat(), help="结束日 YYYY-MM-DD"
    )
    parser.add_argument(
        "--codes", default=None, help="逗号分隔的标的（600005.SH 或 sh.600005）"
    )
    parser.add_argument(
        "--datasets",
        default=",".join(DATASET_PARTS),
        help="逗号分隔的数据集：" + ",".join(DATASET_PARTS),
    )
    parser.add_argument("--apply", action="store_true", help="实际写盘（默认 dry-run）")
    parser.add_argument(
        "--refresh-cache", action="store_true", help="忽略 baostock 缓存重新拉取"
    )
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--manifest", default=None)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    datasets = tuple(part.strip() for part in args.datasets.split(",") if part.strip())
    codes = (
        tuple(part.strip() for part in args.codes.split(",") if part.strip())
        if args.codes
        else None
    )
    try:
        summary = run_backfill(
            args.data_root,
            start=args.start,
            end=args.end,
            codes=codes,
            datasets=datasets,
            apply=args.apply,
            cache_dir=args.cache_dir,
            manifest_path=args.manifest,
            refresh_cache=args.refresh_cache,
        )
    finally:
        _logout()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
