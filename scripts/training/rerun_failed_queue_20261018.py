#!/usr/bin/env python3
"""补跑 2026-10-18 全窗队列中失败的 LIN / MLP / TAB 三单（串行）。

背景：当日 Cross300 新切分（T+5，三段式 2016-01-04..2026-08-28）串行队列里，
- LIN 失败：`array must not contain infs or NaNs`（已由 4ef22b82/2b14f446 修复）
- MLP 虽 completed 但极端量纲失效（RMSE 2.6e11；同一修复覆盖）
- TAB 失败：CUDA device-side assert（已由 f3eb813c 修复）

本脚本等当前训练容器（Transformer 压轴单）退出后，按 LIN → MLP → TAB 顺序
重放失败单的原始请求载荷（`data/training_jobs/_rerun_20261018/payload_*.json`），
逐单等待终态并汇总指标。训练单飞锁占用（409）时自动退避重试，绝不并跑。

用法：
    QM_PASSWORD=... python3 scripts/training/rerun_failed_queue_20261018.py

口令只从环境变量读取，不落盘；状态写入 `_rerun_20261018/status.json`，
日志写入 `_rerun_20261018/rerun.log`，全部结束时写 `DONE`。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
_RERUN_DIR = _ROOT / "data/training_jobs/_rerun_20261018"
_JOBS_DIR = _ROOT / "data/training_jobs"
_LOG_PATH = _RERUN_DIR / "rerun.log"
_STATUS_PATH = _RERUN_DIR / "status.json"
_DONE_PATH = _RERUN_DIR / "DONE"

_API_BASE = os.getenv("QM_API_BASE", "http://localhost:8000/api/v1")
_DB_CONTAINER = "quantmind-db"
_CONTAINER_PREFIX = "qm-train-"

_POLL_S = 20.0
_LOCK_RETRY_MAX_S = 1800.0
_START_WAIT_MAX_S = 900.0
_RESULT_WAIT_MAX_S = 180.0

# 顺序即执行顺序；payload 为失败单的原始请求载荷（job/display 名沿用旧惯例）。
_QUEUE: list[tuple[str, str]] = [
    ("LIN", "payload_LIN.json"),
    ("MLP", "payload_MLP.json"),
    ("TAB", "payload_TAB.json"),
]

_TERMINAL = {"completed", "failed", "cancelled", "canceled", "error"}


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str) -> None:
    line = f"[{_now()}] {msg}"
    print(line, flush=True)
    with _LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _http_json(
    url: str, body: dict[str, Any] | None = None, token: str = "", timeout: float = 120
) -> tuple[int, dict[str, Any]]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers=headers, method="POST" if body is not None else "GET"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {"detail": raw[:800]}
        return exc.code, parsed


def _login(password: str) -> str:
    status, body = _http_json(
        f"{_API_BASE}/auth/login",
        {"username": "admin", "password": password, "tenant_id": "default"},
    )
    token = body.get("access_token") or (body.get("data") or {}).get("access_token")
    if status != 200 or not token:
        raise SystemExit(f"[rerun] 登录失败 {status}: {str(body)[:300]}")
    return str(token)


def _docker(*args: str, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout
    )


def _training_containers() -> list[str]:
    proc = _docker(
        "ps", "--filter", f"name={_CONTAINER_PREFIX}", "--format", "{{.Names}}"
    )
    if proc.returncode != 0:
        log(f"docker ps 失败：{proc.stderr.strip()[:200]}")
        return []
    return [x for x in proc.stdout.splitlines() if x.strip()]


def _container_status(run_id: str) -> str | None:
    """返回容器状态；容器已不存在返回 None。"""
    proc = _docker(
        "inspect", "-f", "{{.State.Status}}", f"{_CONTAINER_PREFIX}{run_id}"
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _db_status(run_id: str) -> str | None:
    sql = (
        "SELECT status FROM admin_training_jobs "
        f"WHERE id='{run_id.replace(chr(39), '')}';"
    )
    proc = _docker(
        "exec",
        _DB_CONTAINER,
        "psql",
        "-U",
        "quantmind",
        "-d",
        "quantmind",
        "-t",
        "-A",
        "-c",
        sql,
    )
    if proc.returncode != 0:
        return None
    value = proc.stdout.strip()
    return value or None


def _wait_no_training_container() -> None:
    announced = False
    while True:
        names = _training_containers()
        if not names:
            if announced:
                log("前序训练容器已退出，单飞锁窗口等待中")
            return
        if not announced:
            log(f"等待前序训练容器退出：{', '.join(names)}")
            announced = True
        time.sleep(30.0)


def _submit(payload: dict[str, Any], password: str) -> str:
    token = _login(password)
    waited = 0.0
    while True:
        status, body = _http_json(
            f"{_API_BASE}/models/run-training", payload, token=token, timeout=180
        )
        if status in (200, 201, 202):
            run_id = str(body.get("runId") or body.get("run_id") or "")
            if not run_id:
                raise RuntimeError(f"提交响应缺 runId：{str(body)[:400]}")
            log(
                f"已提交 run_id={run_id} missingFeatures="
                f"{body.get('missingFeatureCount')}"
            )
            return run_id
        if status == 401:
            token = _login(password)
            continue
        if status == 409:
            if waited >= _LOCK_RETRY_MAX_S:
                raise RuntimeError(f"单飞锁占用超时：{str(body)[:300]}")
            log(f"单飞锁占用中（{str(body.get('detail'))[:120]}），30s 后重试")
            time.sleep(30.0)
            waited += 30.0
            continue
        raise RuntimeError(f"提交失败 {status}: {str(body)[:600]}")


def _wait_for_start(run_id: str) -> None:
    waited = 0.0
    while waited <= _START_WAIT_MAX_S:
        if _container_status(run_id) is not None:
            log(f"容器已启动：{_CONTAINER_PREFIX}{run_id}")
            return
        db_status = _db_status(run_id)
        if db_status and db_status.lower() in _TERMINAL:
            log(f"容器未起但 DB 已终态：{db_status}")
            return
        time.sleep(_POLL_S)
        waited += _POLL_S
    raise RuntimeError(f"容器在 {_START_WAIT_MAX_S:.0f}s 内未启动：{run_id}")


def _wait_for_exit(run_id: str) -> str | None:
    log(f"等待训练结束：{run_id}")
    last = ""
    while True:
        state = _container_status(run_id)
        if state is None:
            db_status = _db_status(run_id)
            log(f"容器已退出，DB 状态={db_status}")
            return db_status
        if state != last:
            log(f"容器状态={state}")
            last = state
        if state in ("exited", "dead"):
            db_status = _db_status(run_id)
            log(f"容器状态={state}，DB 状态={db_status}")
            return db_status
        time.sleep(_POLL_S)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _collect_result(run_id: str, db_status: str | None) -> dict[str, Any]:
    result_path = _JOBS_DIR / run_id / "result.json"
    eval_path = _JOBS_DIR / run_id / "eval_report.json"
    waited = 0.0
    result = None
    while waited <= _RESULT_WAIT_MAX_S:
        result = _read_json(result_path)
        if result is not None:
            break
        time.sleep(5.0)
        waited += 5.0

    summary: dict[str, Any] = {
        "run_id": run_id,
        "db_status": db_status,
        "status": (result or {}).get("status") or db_status,
        "error": (result or {}).get("error"),
        "metrics": (result or {}).get("metrics"),
    }
    eval_report = _read_json(eval_path) or {}
    by_split = eval_report.get("by_split") or {}
    summary["rank_ic"] = {
        seg: {
            "mean": (by_split.get(seg) or {}).get("mean"),
            "icir": (by_split.get(seg) or {}).get("icir"),
            "n_days": (by_split.get(seg) or {}).get("n_days"),
        }
        for seg in ("train", "valid", "test")
        if seg in by_split
    }
    return summary


def _load_state() -> dict[str, Any]:
    if _STATUS_PATH.is_file():
        state = _read_json(_STATUS_PATH)
        if isinstance(state, dict):
            return state
    return {"tags": {}, "updated_at": _now()}


def _save_state(state: dict[str, Any]) -> None:
    state["updated_at"] = _now()
    tmp = _STATUS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_STATUS_PATH)


def main() -> int:
    password = os.getenv("QM_PASSWORD", "")
    if not password:
        raise SystemExit("[rerun] 未提供 QM_PASSWORD（admin 登录口令）")

    _RERUN_DIR.mkdir(parents=True, exist_ok=True)
    if _DONE_PATH.exists():
        _DONE_PATH.unlink()
    state = _load_state()

    log("=== 补跑队列启动：LIN → MLP → TAB（串行） ===")
    failures = 0
    for tag, payload_name in _QUEUE:
        entry = state["tags"].get(tag) or {}
        if entry.get("status") == "completed":
            log(f"[{tag}] 已完成，跳过（run_id={entry.get('run_id')}）")
            continue

        payload_path = _RERUN_DIR / payload_name
        payload = _read_json(payload_path)
        if payload is None:
            log(f"[{tag}] 载荷缺失/不可解析：{payload_path}")
            failures += 1
            continue

        _wait_no_training_container()

        run_id = str(entry.get("run_id") or "")
        try:
            if run_id and entry.get("status") == "submitted":
                log(f"[{tag}] 续跑已提交单 run_id={run_id}")
            else:
                log(f"[{tag}] 提交：{payload.get('job_name')}")
                run_id = _submit(payload, password)
                state["tags"][tag] = {
                    "status": "submitted",
                    "run_id": run_id,
                    "submitted_at": _now(),
                }
                _save_state(state)

            _wait_for_start(run_id)
            db_status = _wait_for_exit(run_id)
            summary = _collect_result(run_id, db_status)
            final = (
                "completed"
                if str(summary.get("status") or "").lower() == "completed"
                else "failed"
            )
            state["tags"][tag] = {
                **state["tags"].get(tag, {}),
                "status": final,
                "run_id": run_id,
                "finished_at": _now(),
                "summary": summary,
            }
            _save_state(state)
            log(f"[{tag}] 终态={final} 指标={json.dumps(summary.get('rank_ic'), ensure_ascii=False)}")
            if final != "completed":
                failures += 1
                if summary.get("error"):
                    log(f"[{tag}] 错误：{str(summary['error'])[:400]}")
        except Exception as exc:  # noqa: BLE001 —— 单子失败不能拖垮后续队列
            failures += 1
            state["tags"][tag] = {
                **state["tags"].get(tag, {}),
                "status": "failed",
                "run_id": run_id or None,
                "finished_at": _now(),
                "error": str(exc),
            }
            _save_state(state)
            log(f"[{tag}] 编排异常：{exc}")

    _DONE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"=== 补跑队列结束，失败 {failures} 单 ===")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
