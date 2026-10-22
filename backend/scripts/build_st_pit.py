#!/usr/bin/env python3
"""A 股 PIT ST 状态表构建（2026-10-22）。

背景：``2_base_sector/instrument_detail.IsSTGP`` 是**当日快照**，历史回测/训练
把它当过滤条件就是前视（2026-10-22 审计：预测 top decile 里 ST 占 8.5%，全市场
4.0%）。本模块用 baostock 的逐日 ``isST`` 建**点时**表：

产物（``<data_root>/2_base_sector/st_pit/``）：
  ``st_pit.parquet``   symbol(后缀式), time(YYYYMMDD), is_st —— **只存 is_st=True
                       的稀疏行**（A 股 ST 占比常年 <5%，稀疏存 ~1/20 体积）
  ``coverage.json``    每符号的 ST 天数 / 首个末个 ST 日 / 已抓取到的日期
  ``manifest.json``    行数/符号数/生成时间

读取：``load_st_mask(data_root, dates, symbols)`` → DataFrame[bool]（缺失 = 非 ST）。

逐符号缓存 ``<data_root>/factor_research/st_pit_cache/``：
  ``{symbol}.parquet``   该符号的稀疏 ST 行
  ``_journal.jsonl``     每符号一行覆盖水印（queried_to / last_data / st_days）

覆盖水印**单独存 journal 而不是塞进 parquet**：全期非 ST 的票（A 股大多数）与
退市票的 parquet 是空表/不满区间，只能靠「查询到哪一天」判断是否可跳过，否则
每轮都会重抓。journal 是追加日志，最后一行胜出，尾部半行损坏直接跳过。

用法（容器内，cwd=/app）：
    python backend/scripts/build_st_pit.py --apply                 # 全量抓取 + 重建
    python backend/scripts/build_st_pit.py --resume-only           # 只重建不联网
    python backend/scripts/build_st_pit.py                         # dry-run（只统计）
"""

from __future__ import annotations

import argparse
import json
import logging
import multiprocessing as mp
import os
import time
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger("build_st_pit")

ST_PIT_REL = ("2_base_sector", "st_pit")
CACHE_REL = ("factor_research", "st_pit_cache")
JOURNAL_NAME = "_journal.jsonl"
BAOSTOCK_FIELDS = "date,code,isST"
DEFAULT_START = "2016-01-01"
_SYMBOL_COLUMNS = ("symbol", "time", "is_st")


class UnsupportedSymbol(RuntimeError):
    """数据源整类不覆盖该标的（当前只有北交所）。"""


# ------------------------------------------------------------------ 代码转换


def symbol_to_baostock(symbol: str) -> str:
    """``SH600000`` / ``600000.SH`` → ``sh.600000``（baostock 口径）。"""
    s = str(symbol).strip().upper()
    if "." in s:
        code, _, suffix = s.partition(".")
        return f"{suffix.lower()}.{code}"
    if len(s) > 2 and s[:2] in {"SH", "SZ", "BJ"}:
        return f"{s[:2].lower()}.{s[2:]}"
    raise ValueError(f"无法识别的股票代码: {symbol!r}")


def baostock_to_symbol(code: str) -> str:
    """``sh.600000`` → ``600000.SH``（QuantDB parquet 后缀式）。"""
    c = str(code).strip()
    if "." not in c:
        raise ValueError(f"无法识别的 baostock 代码: {code!r}")
    prefix, _, digits = c.partition(".")
    return f"{digits}.{prefix.upper()}"


def _yyyymmdd(value: str) -> str:
    return str(value).replace("-", "")[:8]


# ------------------------------------------------------------------ 纯函数


def rows_to_frame(rows: list[list[str]], symbol: str | None = None) -> pd.DataFrame:
    """baostock 行（date, code, isST）→ 稀疏 ST 行（只留 isST=='1'）。

    返回列 ``symbol,time,is_st``；``time`` 为 YYYYMMDD 字符串。空/非法行跳过；
    isST 非 '0'/'1' 视为未知 → 丢弃（宁缺勿错）。
    """
    out: list[dict] = []
    for row in rows:
        if len(row) < 3:
            continue
        date, code, flag = row[0], row[1], str(row[2]).strip()
        if flag != "1":
            continue
        sym = baostock_to_symbol(code) if code else symbol
        if not sym:
            continue
        out.append({"symbol": sym, "time": _yyyymmdd(date), "is_st": True})
    frame = pd.DataFrame(out, columns=list(_SYMBOL_COLUMNS))
    if frame.empty:
        return frame
    return frame.drop_duplicates(subset=["symbol", "time"]).sort_values(
        ["symbol", "time"], ignore_index=True
    )


def merge_frames(old: pd.DataFrame | None, new: pd.DataFrame) -> pd.DataFrame:
    """两代稀疏帧取并集（(symbol,time) 去重，新数据覆盖旧值）。"""
    frames = [f for f in (old, new) if f is not None]
    if not frames:
        return pd.DataFrame(columns=list(_SYMBOL_COLUMNS))
    merged = pd.concat(frames, ignore_index=True)
    merged = merged.drop_duplicates(subset=["symbol", "time"], keep="last")
    return merged.sort_values(["symbol", "time"], ignore_index=True)


def append_journal(cache_dir: Path, entries: dict[str, dict]) -> None:
    """覆盖水印追加一行一符号（O_APPEND 小行原子，可多进程并发）。"""
    if not entries:
        return
    path = Path(cache_dir) / JOURNAL_NAME
    with path.open("a", encoding="utf-8") as fh:
        for symbol, entry in entries.items():
            fh.write(json.dumps({"symbol": symbol, **entry}, ensure_ascii=False) + "\n")
        fh.flush()


def read_journal(cache_dir: Path) -> dict[str, dict]:
    """读覆盖水印；同符号后者胜出，损坏行（含崩溃时写了一半的行）跳过。"""
    path = Path(cache_dir) / JOURNAL_NAME
    out: dict[str, dict] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        symbol = entry.pop("symbol", None)
        if symbol:
            out[symbol] = entry
    return out


def covered_until(cache_dir: Path, symbol: str) -> str | None:
    """该符号已抓取到的最后日期（YYYYMMDD）；无记录 → None。"""
    entry = read_journal(cache_dir).get(symbol)
    return str(entry["queried_to"]) if entry and entry.get("queried_to") else None


# ------------------------------------------------------------------ 抓取


def _fetch_baostock(
    symbol: str, start: str, end: str
) -> tuple[pd.DataFrame, str | None]:
    """单符号区间 isST → (稀疏 ST 帧, 最后有数据日)。"""
    code = symbol_to_baostock(symbol)
    if code.startswith("bj."):
        # baostock 只认 sh./sz.（实测 10004011「股票代码未标识sh或sz」），
        # 北交所按「无 ST 数据」显式登记，不当作失败反复重试。
        raise UnsupportedSymbol("baostock 不覆盖北交所")
    import baostock as bs

    rs = bs.query_history_k_data_plus(
        code,
        BAOSTOCK_FIELDS,
        start_date=start,
        end_date=end,
        frequency="d",
        adjustflag="3",
    )
    if rs.error_code != "0":
        raise RuntimeError(f"baostock {rs.error_code} {rs.error_msg}")
    rows: list[list[str]] = []
    while rs.next():
        rows.append(rs.get_row_data())
    last = max((_yyyymmdd(r[0]) for r in rows), default=None)
    return rows_to_frame(rows, symbol=symbol), last


def write_symbol(
    cache_dir: Path,
    symbol: str,
    frame: pd.DataFrame,
    *,
    queried_to: str,
    last_data: str | None,
) -> None:
    """落一个符号的稀疏帧 + 覆盖水印（parquet 原子替换，水印后写）。"""
    cache_dir = Path(cache_dir)
    path = cache_dir / f"{symbol}.parquet"
    old = pd.read_parquet(path) if path.exists() else None
    merged = merge_frames(old, frame)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    merged.to_parquet(tmp, index=False)
    os.replace(tmp, path)
    append_journal(
        cache_dir,
        {
            symbol: {
                "queried_to": queried_to,
                "last_data": last_data,
                "st_days": int(len(merged)),
                "at": pd.Timestamp.now().isoformat(timespec="seconds"),
            }
        },
    )


def _worker_init() -> None:
    import baostock as bs

    bs.login()


def record_unsupported(
    cache_dir: Path, symbol: str, *, queried_to: str, reason: str
) -> None:
    """登记「数据源不覆盖」：水印照写，避免每轮重试。"""
    append_journal(
        cache_dir,
        {
            symbol: {
                "queried_to": _yyyymmdd(queried_to),
                "last_data": None,
                "unsupported": reason,
                "at": pd.Timestamp.now().isoformat(timespec="seconds"),
            }
        },
    )


def _worker_one(args: tuple[str, str, str, str]) -> tuple[str, str]:
    symbol, start, end, cache_dir = args
    try:
        frame, last = _fetch_baostock(symbol, start, end)
        write_symbol(
            cache_dir, symbol, frame, queried_to=_yyyymmdd(end), last_data=last
        )
        return symbol, "ok"
    except UnsupportedSymbol as exc:
        record_unsupported(cache_dir, symbol, queried_to=end, reason=str(exc))
        return symbol, "unsupported"
    except Exception as exc:  # noqa: BLE001 - 单符号失败不中断全量
        return symbol, f"error: {exc}"


def crawl(
    symbols: list[str],
    *,
    data_root: Path,
    start: str = DEFAULT_START,
    end: str | None = None,
    workers: int = 1,
    fetch=None,
    log_fn=log.info,
) -> dict:
    """抓取 symbols 的 isST 历史到逐符号缓存（可断点续跑）。

    ``fetch(symbol, start, end) -> (frame, last_data)`` 可注入（测试用）。
    返回 ``{"symbols", "fetched", "errors"}``。
    """
    data_root = Path(data_root)
    end = _yyyymmdd(end or pd.Timestamp.now().strftime("%Y%m%d"))
    end_dash = f"{end[:4]}-{end[4:6]}-{end[6:]}"
    cache_dir = data_root.joinpath(*CACHE_REL)
    cache_dir.mkdir(parents=True, exist_ok=True)

    journal = read_journal(cache_dir)
    todo = [
        s
        for s in symbols
        if not journal.get(s, {}).get("queried_to")
        or str(journal[s]["queried_to"]) < end
    ]
    log_fn(
        f"ST PIT 抓取：{len(symbols)} 只，待抓 {len(todo)}"
        f"（已覆盖跳过 {len(symbols) - len(todo)}），end={end}"
    )

    errors: dict[str, str] = {}
    skipped: list[str] = []
    done = 0
    if fetch is not None:
        for sym in todo:
            try:
                frame, last = fetch(sym, start, end_dash)
                write_symbol(cache_dir, sym, frame, queried_to=end, last_data=last)
                done += 1
            except UnsupportedSymbol as exc:
                record_unsupported(cache_dir, sym, queried_to=end, reason=str(exc))
                skipped.append(sym)
            except Exception as exc:  # noqa: BLE001
                errors[sym] = str(exc)
        return {
            "symbols": len(symbols),
            "fetched": done,
            "unsupported": skipped,
            "errors": errors,
        }

    if todo:
        with mp.Pool(processes=max(1, workers), initializer=_worker_init) as pool:
            for sym, status in pool.imap_unordered(
                _worker_one, [(s, start, end_dash, str(cache_dir)) for s in todo]
            ):
                if status == "ok":
                    done += 1
                elif status == "unsupported":
                    skipped.append(sym)
                else:
                    errors[sym] = status
                finished = done + len(errors) + len(skipped)
                if finished % 200 == 0:
                    log_fn(
                        f"  进度 {finished}/{len(todo)}"
                        f"（不支持 {len(skipped)}，失败 {len(errors)}）"
                    )
    return {
        "symbols": len(symbols),
        "fetched": done,
        "unsupported": skipped,
        "errors": errors,
    }


# ------------------------------------------------------------------ 重建与读取


def rebuild_sparse(data_root: Path, *, cache_dir: Path | None = None) -> dict:
    """逐符号缓存 → 稀疏 ``st_pit.parquet`` + coverage/manifest。"""
    data_root = Path(data_root)
    cache_dir = Path(cache_dir or data_root.joinpath(*CACHE_REL))
    journal = read_journal(cache_dir)
    files = sorted(cache_dir.glob("*.parquet"))
    frames = []
    coverage: dict[str, dict] = {}
    # 只出现在 journal 里的符号（北交所等不支持项）也要进覆盖表
    covered_symbols = sorted({p.stem for p in files} | set(journal))
    for symbol in covered_symbols:
        p = cache_dir / f"{symbol}.parquet"
        if not p.exists():
            stamp = journal.get(symbol, {})
            coverage[symbol] = {
                "first_st": None,
                "last_st": None,
                "last_data": stamp.get("last_data"),
                "queried_to": stamp.get("queried_to"),
                "unsupported": stamp.get("unsupported"),
                "st_days": 0,
            }
            continue
        try:
            df = pd.read_parquet(p)
        except Exception as exc:  # noqa: BLE001
            coverage[symbol] = {"error": str(exc)}
            continue
        st = df[df["is_st"].astype(bool)][["symbol", "time", "is_st"]]
        if len(st):
            frames.append(st)
        stamp = journal.get(symbol, {})
        coverage[symbol] = {
            "first_st": str(st["time"].min()) if len(st) else None,
            "last_st": str(st["time"].max()) if len(st) else None,
            "last_data": stamp.get("last_data"),
            "queried_to": stamp.get("queried_to"),
            "unsupported": stamp.get("unsupported"),
            "st_days": int(len(st)),
        }
    sparse = (
        pd.concat(frames, ignore_index=True)
        .drop_duplicates(subset=["symbol", "time"])
        .sort_values(["symbol", "time"], ignore_index=True)
        if frames
        else pd.DataFrame(columns=list(_SYMBOL_COLUMNS))
    )
    out_dir = data_root.joinpath(*ST_PIT_REL)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "st_pit.parquet"
    tmp = target.with_name(f"st_pit.tmp.{os.getpid()}.parquet")
    sparse.to_parquet(tmp, index=False)
    check = pd.read_parquet(tmp)
    if len(check) != len(sparse) or (len(check) and not check["is_st"].all()):
        tmp.unlink(missing_ok=True)
        raise RuntimeError("st_pit 落盘校验失败")
    os.replace(tmp, target)

    (out_dir / "coverage.json").write_text(
        json.dumps(coverage, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    manifest = {
        "built_at": pd.Timestamp.now().isoformat(timespec="seconds"),
        "symbols": int(sparse["symbol"].nunique()) if len(sparse) else 0,
        "rows": int(len(sparse)),
        "cached_symbols": len(files),
        "journal_symbols": len(journal),
        "unsupported_symbols": sum(
            1 for c in coverage.values() if c.get("unsupported")
        ),
        "source": "baostock query_history_k_data_plus(isST)",
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return manifest


def load_st_mask(data_root: Path, dates: pd.Index, symbols: list[str]) -> pd.DataFrame:
    """(交易日 × 标的) 的 PIT ST 布尔掩码；稀疏表未命中 = 非 ST。

    未覆盖（未抓到的标的/日期）一律按 False 处理 —— 与当前「不做 ST 过滤」
    行为一致，绝不因为缺数据把正常票误杀；覆盖度见 ``coverage.json``。
    """
    from backend.services.engine.factor_research.data import st_mask_over

    path = Path(data_root).joinpath(*ST_PIT_REL) / "st_pit.parquet"
    if not path.exists():
        return pd.DataFrame(
            False, index=pd.Index(dates), columns=list(symbols), dtype=bool
        )
    return st_mask_over(dates, symbols, pd.read_parquet(path))


# ------------------------------------------------------------------ CLI


def kline_symbols(data_root: Path) -> list[str]:
    """daily_unadjusted 分区里出现过的全部标的（含退市）。"""
    import duckdb

    glob = str(
        Path(data_root) / "1_kline_data" / "daily_unadjusted" / "dt=*" / "data.parquet"
    )
    con = duckdb.connect()
    try:
        rows = con.execute(
            "SELECT DISTINCT symbol FROM read_parquet(?) ORDER BY symbol", [glob]
        ).fetchall()
    finally:
        con.close()
    return [r[0] for r in rows]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--data-root", default=os.environ.get("QM_QUANTDB_DATA_DIR", "/data/quantdb")
    )
    ap.add_argument("--start", default=DEFAULT_START)
    ap.add_argument("--end", default=None)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--apply", action="store_true", help="真正抓取/落盘")
    ap.add_argument(
        "--resume-only", action="store_true", help="只从缓存重建稀疏表，不联网"
    )
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    root = Path(args.data_root)
    if not args.resume_only:
        symbols = kline_symbols(root)
        log.info("标的全集：%d 只（来自 daily_unadjusted）", len(symbols))
        if not args.apply:
            have = len(read_journal(root.joinpath(*CACHE_REL)))
            log.info("dry-run：已有缓存 %d 只；加 --apply 开始抓取", have)
            return 0
        t0 = time.time()
        stats = crawl(
            symbols,
            data_root=root,
            start=args.start,
            end=args.end,
            workers=args.workers,
        )
        log.info(
            "抓取完成：成功 %d，不支持 %d，失败 %d，用时 %.0fs",
            stats["fetched"],
            len(stats["unsupported"]),
            len(stats["errors"]),
            time.time() - t0,
        )
        if stats["errors"]:
            (root.joinpath(*CACHE_REL) / "_errors.json").write_text(
                json.dumps(stats["errors"], ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
    manifest = rebuild_sparse(root)
    log.info("st_pit 重建：%s", manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
