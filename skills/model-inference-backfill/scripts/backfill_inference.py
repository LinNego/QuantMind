#!/usr/bin/env python3
"""模型推理补全驱动（批量、按模型并发）。

复用平台的唯一推理落库链路：
``InferenceScriptRunner.execute()`` 写 ``engine_signal_scores`` / run 记录，
``merge_signals_into_pred()`` 把真实分数回写模型目录的 ``pred.parquet``
（coverage 缺口判定与个股分数曲线的数据源）。

典型用法（容器内，cwd=/app）：

    # 先看缺口，不动手
    python /tmp/backfill_inference.py --horizons 5,10,20 --end 2026-10-19 --dry-run

    # 补 13 模型 × 三周期，3 个模型并发
    python /tmp/backfill_inference.py --horizons 5,10,20 --end 2026-10-19 \
        --concurrency 3 --status-file /tmp/inference_backfill_status.json

缺口口径：默认从每个模型 pred.parquet 的最大日期次一交易日补到 ``--end``
（``--start`` 可显式指定左界，取两者较晚者）。不加 ``--scan-holes`` 不回溯
训练期的历史空洞。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger("inference_backfill")


def _project_root() -> Path:
    here = Path(__file__).resolve()
    candidates = [
        Path(os.getenv("QM_PROJECT_ROOT", "") or "/nonexistent"),
        Path("/app"),
        here.parents[3] if len(here.parents) > 3 else here.parent,
        Path.cwd(),
    ]
    for cand in candidates:
        if (cand / "backend").is_dir():
            return cand
    raise RuntimeError("找不到项目根目录（backend/），请设置 QM_PROJECT_ROOT")


ROOT = _project_root()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import duckdb  # noqa: E402
import exchange_calendars as xcals  # noqa: E402
import pandas as pd  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

from backend.services.engine.inference.pred_merge import (  # noqa: E402
    merge_signals_into_pred,
)
from backend.services.engine.inference.script_runner import (  # noqa: E402
    InferenceScriptRunner,
)

_MODEL_PRED_CANDIDATES = ("pred.parquet", "pred/pred.parquet")


@dataclass
class ModelTarget:
    model_id: str
    storage_path: Path
    model_type: str
    display_name: str
    horizon: int


@dataclass
class ModelState:
    target: ModelTarget
    pred_file: Path | None = None
    dates: list[str] = field(default_factory=list)
    covered_max: str | None = None
    total: int = 0
    done: int = 0
    ok: int = 0
    failed: int = 0
    merged_rows: int = 0
    current_date: str | None = None
    errors: list[str] = field(default_factory=list)
    started_at: str | None = None
    finished_at: str | None = None
    duration_s: float = 0.0


_state_lock = threading.Lock()
# pred.parquet 全量读改写单次峰值可达数 GB；多模型线程同时合并会叠内存。
# 平台瓶颈是内存而非磁盘，合并一律串行。
_merge_lock = threading.Lock()
_status: dict[str, Any] = {"models": {}, "started_at": None, "updated_at": None}


def _sync_db_url() -> str:
    raw = os.getenv("DATABASE_URL", "")
    if "+asyncpg" in raw:
        raw = raw.replace("+asyncpg", "+psycopg2")
    if not raw.startswith("postgresql"):
        raw = "postgresql+psycopg2://quantmind:quantmind2026@db:5432/quantmind"
    return raw


def _load_targets(args: argparse.Namespace) -> list[ModelTarget]:
    horizons = [int(h) for h in args.horizons.split(",") if h.strip()]
    engine = create_engine(_sync_db_url(), future=True)
    sql = """
        SELECT model_id, storage_path,
               COALESCE(metadata_json->>'model_type', metadata_json->>'algorithm', '') AS model_type,
               COALESCE(metadata_json->>'display_name', model_id) AS display_name,
               (metadata_json->>'target_horizon_days')::int AS horizon
        FROM qm_user_models
        WHERE tenant_id = :tenant
          AND user_id = :user
          AND status = 'ready'
          AND (metadata_json->>'target_horizon_days')::int = ANY(:horizons)
    """
    params: dict[str, Any] = {
        "tenant": args.tenant,
        "user": args.user,
        "horizons": horizons,
    }
    if args.created_after:
        sql += " AND created_at >= :created_after"
        params["created_after"] = args.created_after
    if args.model_id:
        sql += " AND model_id = ANY(:model_ids)"
        params["model_ids"] = args.model_id
    sql += " ORDER BY horizon, created_at"
    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).mappings().all()
    engine.dispose()
    targets = [
        ModelTarget(
            model_id=r["model_id"],
            storage_path=Path(r["storage_path"]),
            model_type=str(r["model_type"] or ""),
            display_name=str(r["display_name"] or r["model_id"]),
            horizon=int(r["horizon"]),
        )
        for r in rows
        if r["storage_path"]
    ]
    if args.exclude_model_id:
        excluded = set(args.exclude_model_id)
        targets = [t for t in targets if t.model_id not in excluded]
    return targets


def _resolve_pred_file(model_dir: Path) -> Path | None:
    for rel in _MODEL_PRED_CANDIDATES:
        cand = model_dir / rel
        if cand.is_file():
            return cand
    return None


def _read_pred_dates(pred_file: Path) -> list[str]:
    con = duckdb.connect()
    try:
        rows = con.execute(
            f"SELECT DISTINCT CAST(trade_date AS DATE) AS d "
            f"FROM read_parquet('{pred_file}') ORDER BY d"
        ).fetchall()
        return [str(r[0])[:10] for r in rows if r[0] is not None]
    finally:
        con.close()


def _trading_dates(start: str, end: str) -> list[str]:
    cal = xcals.get_calendar("XSHG")
    sessions = cal.sessions_in_range(pd.Timestamp(start), pd.Timestamp(end))
    return [s.strftime("%Y-%m-%d") for s in sessions]


def _next_day(value: str) -> str:
    return (date.fromisoformat(value) + timedelta(days=1)).isoformat()


def _build_dates(
    covered: list[str], args: argparse.Namespace, state: ModelState
) -> list[str]:
    if not covered:
        logger.warning("%s 无 pred 历史，跳过（不伪造历史序列）", state.target.model_id)
        return []
    covered_set = set(covered)
    state.covered_max = covered[-1]
    left = _next_day(covered[-1])
    if args.start:
        left = max(left, args.start)
    if left > args.end:
        return []
    if args.scan_holes:
        left = max(covered[0], args.start or covered[0])
    return [d for d in _trading_dates(left, args.end) if d not in covered_set]


def _write_status(status_file: Path | None) -> None:
    """原子写状态文件。多线程并发调用 → 临时文件名必须唯一。

    状态文件只是运维观测面，写失败绝不向上抛：一次 ENOSPC/EINTR 不该
    杀死正在推理的模型线程（曾因多线程共用同一 .tmp 触发 os.replace
    FileNotFoundError，把线程直接干掉）。
    """
    if not status_file:
        return
    try:
        with _state_lock:
            _status["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            payload = json.dumps(_status, ensure_ascii=False, indent=2, default=str)
        tmp = status_file.with_name(
            f"{status_file.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, status_file)
    except Exception:  # noqa: BLE001
        logger.warning("写状态文件失败（不影响推理）: %s", status_file, exc_info=True)


def _update_model_status(state: ModelState) -> None:
    with _state_lock:
        _status["models"][state.target.model_id] = {
            "display_name": state.target.display_name,
            "model_type": state.target.model_type,
            "horizon": state.target.horizon,
            "storage_path": str(state.target.storage_path),
            "pred_file": str(state.pred_file) if state.pred_file else None,
            "covered_max": state.covered_max,
            "total": state.total,
            "done": state.done,
            "ok": state.ok,
            "failed": state.failed,
            "merged_rows": state.merged_rows,
            "current_date": state.current_date,
            "errors": state.errors[-20:],
            "started_at": state.started_at,
            "finished_at": state.finished_at,
            "duration_s": round(state.duration_s, 1),
        }


def _run_model(
    state: ModelState, args: argparse.Namespace, status_file: Path | None
) -> ModelState:
    state.started_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    t0 = time.time()
    pred_file = state.pred_file
    if pred_file is None or not state.dates:
        state.finished_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        state.duration_s = time.time() - t0
        _update_model_status(state)
        _write_status(status_file)
        return state

    runner = InferenceScriptRunner(
        primary_model_dir=str(state.target.storage_path),
        primary_model_id=state.target.model_id,
        # 补历史必须禁兜底：兜底模型（alpha158）的分数写进该模型 pred.parquet
        # 就是拿别的模型冒充，coverage 会假性消除。
        enable_fallback=False,
    )
    pending: list[tuple[str, list[dict]]] = []
    merge_every = max(1, int(args.merge_every))

    def _flush() -> None:
        if not pending:
            return
        with _merge_lock:
            rows = merge_signals_into_pred(
                pred_file, list(pending), create_if_missing=False
            )
        state.merged_rows += int(rows or 0)
        logger.info(
            "%s 合并 %d 天 → pred.parquet（%d 行）",
            state.target.model_id,
            len(pending),
            rows,
        )
        pending.clear()

    for idx, day in enumerate(state.dates, start=1):
        state.current_date = day
        _update_model_status(state)
        _write_status(status_file)
        day_t0 = time.time()
        try:
            result = runner.execute(
                date=day,
                tenant_id=args.tenant,
                user_id=args.user,
                persist=True,
            )
            elapsed = time.time() - day_t0
            if getattr(result, "success", False) and getattr(result, "signals", None):
                pending.append((day, list(result.signals)))
                state.ok += 1
                logger.info(
                    "[%d/%d] %s h=%d %s ok signals=%d %.1fs",
                    idx,
                    state.total,
                    state.target.model_id,
                    state.target.horizon,
                    day,
                    len(result.signals),
                    elapsed,
                )
            else:
                state.failed += 1
                err = (
                    getattr(result, "error", "")
                    or getattr(result, "fallback_reason", "")
                    or f"无信号（exit={getattr(result, 'exit_code', '?')}）"
                )
                state.errors.append(f"{day}: {err}")
                logger.error(
                    "[%d/%d] %s h=%d %s FAIL %.1fs: %s",
                    idx,
                    state.total,
                    state.target.model_id,
                    state.target.horizon,
                    day,
                    elapsed,
                    err,
                )
        except Exception as exc:  # noqa: BLE001
            state.failed += 1
            state.errors.append(f"{day}: {exc}")
            logger.exception(
                "%s h=%d %s 异常",
                state.target.model_id,
                state.target.horizon,
                day,
            )
        finally:
            state.done += 1
            state.current_date = None
            _update_model_status(state)
            _write_status(status_file)
        if len(pending) >= merge_every:
            try:
                _flush()
            except Exception as exc:  # noqa: BLE001
                state.errors.append(f"{day} merge: {exc}")
                logger.exception("%s 合并 pred 失败", state.target.model_id)
        if args.pause > 0 and idx < state.total:
            time.sleep(args.pause)

    try:
        _flush()
    except Exception as exc:  # noqa: BLE001
        state.errors.append(f"final merge: {exc}")
        logger.exception("%s 最终合并 pred 失败", state.target.model_id)

    state.finished_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    state.duration_s = time.time() - t0
    _update_model_status(state)
    _write_status(status_file)
    return state


def _prepare_state(target: ModelTarget, args: argparse.Namespace) -> ModelState:
    state = ModelState(target=target)
    pred_file = _resolve_pred_file(target.storage_path)
    state.pred_file = pred_file
    if pred_file is None:
        state.errors.append("pred.parquet 不存在")
        return state
    covered = _read_pred_dates(pred_file)
    state.dates = _build_dates(covered, args, state)
    state.total = len(state.dates)
    return state


def _quantdb_latest_date(args: argparse.Namespace) -> str | None:
    if not args.respect_data_cutoff:
        return None
    try:
        from backend.services.engine.data_platform.quantdb_factor_reader import (
            QuantDBFactorReader,
            market_data_dir,
        )

        reader = QuantDBFactorReader(market_data_dir(args.market), market=args.market)
        dates = reader.available_dates(args.factor_source)
        return str(dates[-1])[:10] if dates else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取 QuantDB 最新因子日失败：%s", exc)
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", default="default")
    parser.add_argument("--user", default="10000001")
    parser.add_argument("--market", default="CN")
    parser.add_argument("--factor-source", default="l1_factors")
    parser.add_argument("--horizons", default="5,10,20")
    parser.add_argument("--model-id", action="append", default=[])
    parser.add_argument(
        "--model-id-file", default=None, help="模型 ID 列表文件（一行一个，# 注释）"
    )
    parser.add_argument("--exclude-model-id", action="append", default=[])
    parser.add_argument("--created-after", default="2026-10-17")
    parser.add_argument("--start", default=None, help="左界（默认 pred 最大日期次日）")
    parser.add_argument("--end", required=True, help="右界交易日 YYYY-MM-DD")
    parser.add_argument("--scan-holes", action="store_true", help="同时回补历史空洞")
    parser.add_argument("--merge-every", type=int, default=10)
    parser.add_argument(
        "--longest-first",
        action="store_true",
        default=True,
        help="深序模型优先调度（默认开）",
    )
    parser.add_argument(
        "--no-longest-first", dest="longest_first", action="store_false"
    )
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--pause", type=float, default=0.0, help="每日之间暂停秒数")
    parser.add_argument("--status-file", default="/tmp/inference_backfill_status.json")
    parser.add_argument("--report-file", default=None)
    parser.add_argument(
        "--respect-data-cutoff",
        action="store_true",
        default=True,
        help="右界收敛到 QuantDB 因子最新产出日（默认开）",
    )
    parser.add_argument(
        "--no-respect-data-cutoff", dest="respect_data_cutoff", action="store_false"
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.model_id_file:
        ids_from_file = [
            line.strip()
            for line in Path(args.model_id_file)
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        args.model_id = list(args.model_id) + ids_from_file

    cutoff = _quantdb_latest_date(args)
    if cutoff and cutoff < args.end:
        logger.info("右界从 %s 收敛到 QuantDB 因子最新日 %s", args.end, cutoff)
        args.end = cutoff

    targets = _load_targets(args)
    if not targets:
        logger.error("没有匹配的模型")
        return 2
    logger.info("目标模型 %d 个", len(targets))

    states = [_prepare_state(t, args) for t in targets]
    if args.longest_first:
        # 深序模型单日推理明显更慢（加载 torch + 序列窗口）：先跑长的，
        # 缩短线程池尾部的空转时间。同成本内保持原顺序。
        _deep = {
            "nativetft",
            "gru",
            "lstm",
            "alstm",
            "tcn",
            "transformer",
            "tabnet",
            "mlp",
        }

        def _cost(state: ModelState) -> tuple[int, int]:
            return (
                0 if state.target.model_type.lower() in _deep else 1,
                -state.total,
            )

        states.sort(key=_cost)
    with _state_lock:
        _status["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        _status["end"] = args.end
        _status["tenant"] = args.tenant
        _status["user"] = args.user
    for st in states:
        _update_model_status(st)
    status_file = Path(args.status_file) if args.status_file else None
    _write_status(status_file)

    for st in states:
        logger.info(
            "%s %s h=%d covered_max=%s gap_days=%d",
            st.target.model_id,
            st.target.model_type,
            st.target.horizon,
            st.covered_max,
            st.total,
        )
    total_days = sum(st.total for st in states)
    logger.info("合计待补 %d 个模型×交易日", total_days)

    if args.dry_run:
        report = {
            "dry_run": True,
            "end": args.end,
            "models": [
                {
                    "model_id": st.target.model_id,
                    "display_name": st.target.display_name,
                    "model_type": st.target.model_type,
                    "horizon": st.target.horizon,
                    "covered_max": st.covered_max,
                    "gap_days": st.total,
                    "dates": st.dates,
                }
                for st in states
            ],
        }
        payload = json.dumps(report, ensure_ascii=False, indent=2)
        if args.report_file:
            Path(args.report_file).write_text(payload, encoding="utf-8")
        print(payload)
        return 0

    t_all = time.time()
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        futures = {pool.submit(_run_model, st, args, status_file): st for st in states}
        for fut in as_completed(futures):
            st = futures[fut]
            try:
                fut.result()
            except Exception:  # noqa: BLE001
                logger.exception("%s 线程失败", st.target.model_id)
            _update_model_status(st)
            _write_status(status_file)

    total_ok = sum(s.ok for s in states)
    total_failed = sum(s.failed for s in states)
    summary = {
        "dry_run": False,
        "end": args.end,
        "elapsed_s": round(time.time() - t_all, 1),
        "total_days": total_days,
        "ok": total_ok,
        "failed": total_failed,
        "models": [
            {
                "model_id": s.target.model_id,
                "display_name": s.target.display_name,
                "model_type": s.target.model_type,
                "horizon": s.target.horizon,
                "covered_max": s.covered_max,
                "gap_days": s.total,
                "ok": s.ok,
                "failed": s.failed,
                "merged_rows": s.merged_rows,
                "duration_s": round(s.duration_s, 1),
                "errors": s.errors,
            }
            for s in states
        ],
    }
    payload = json.dumps(summary, ensure_ascii=False, indent=2)
    if args.report_file:
        Path(args.report_file).write_text(payload, encoding="utf-8")
    print(payload)
    return 0 if total_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
