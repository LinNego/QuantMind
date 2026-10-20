#!/usr/bin/env python3
"""通用串行训练队列驱动（manifest 驱动，断点续跑）。

用于把一批训练单按清单顺序串行提交，逐单等待终态并汇总指标。与
`rerun_failed_queue_20261018.py` 同一套纪律：

- 训练单飞锁占用（HTTP 409）自动退避重试，绝不并跑；
- 每单提交前确认没有其它 `qm-train-*` 容器在跑；
- 状态落 `status.json`（原子写），重跑本脚本会跳过已完成单、续等已提交单；
- 全部结束写 `DONE`。

清单格式（JSON）：
    {"queue": [
        {"tag": "LGB", "payload": "payload_LGB.json"},
        ...
    ]}

用法（二选一）：
    QM_PASSWORD=... python3 scripts/training/queue_driver.py \\
        --manifest data/training_jobs/_t10_queue_20261018/manifest.json \\
        --state-dir data/training_jobs/_t10_queue_20261018 \\
        --wait-for-file data/training_jobs/_rerun_20261018/DONE

    # 无口令场景：走内部调用密钥（X-Internal-Call + X-User-Id），
    # 密钥从容器运行时读取，不落盘、不出现在命令行字面量里。
    QM_INTERNAL_CALL_SECRET="$(docker exec quantmind printenv INTERNAL_CALL_SECRET)" \\
        python3 scripts/training/queue_driver.py ...
"""

from __future__ import annotations

import argparse
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
_JOBS_DIR = _ROOT / "data/training_jobs"

_API_BASE = os.getenv("QM_API_BASE", "http://localhost:8000/api/v1")
_DB_CONTAINER = "quantmind-db"
_CONTAINER_PREFIX = "qm-train-"

_POLL_S = 20.0
_GATE_POLL_S = 60.0
_LOCK_RETRY_MAX_S = 1800.0
_START_WAIT_MAX_S = 900.0
_RESULT_WAIT_MAX_S = 180.0

_TERMINAL = {"completed", "failed", "cancelled", "canceled", "error"}
_GATE_TAGS = ("LIN", "MLP", "TAB")


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


class QueueDriver:
    def __init__(
        self,
        manifest: Path,
        state_dir: Path,
        wait_for_file: Path | None,
        internal_secret: str = "",
        internal_user_id: str = "10000001",
        internal_tenant_id: str = "default",
    ):
        self.manifest = manifest
        self.state_dir = state_dir
        self.wait_for_file = wait_for_file
        self.log_path = state_dir / "queue.log"
        self.status_path = state_dir / "status.json"
        self.done_path = state_dir / "DONE"
        self.state: dict[str, Any] = {"tags": {}, "updated_at": _now()}
        # 内部调用认证：与 admin 路由 get_current_user 的内部头约定一致。
        # 有密钥就不走登录，避免无口令环境（定时/无人值守）被卡住。
        self._internal_headers: dict[str, str] = {}
        if internal_secret:
            self._internal_headers = {
                "X-Internal-Call": internal_secret,
                "X-User-Id": str(internal_user_id),
                "X-Tenant-Id": str(internal_tenant_id),
            }

    # ---------- 基础 I/O ----------

    def log(self, msg: str) -> None:
        line = f"[{_now()}] {msg}"
        print(line, flush=True)
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def _save_state(self) -> None:
        self.state["updated_at"] = _now()
        tmp = self.status_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(self.status_path)

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any] | None:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    # ---------- HTTP / Docker ----------

    def _http_json(
        self,
        url: str,
        body: dict[str, Any] | None = None,
        token: str = "",
        timeout: float = 120,
    ) -> tuple[int, dict[str, Any]]:
        headers = {"Content-Type": "application/json", **self._internal_headers}
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

    def _login(self, password: str) -> str:
        status, body = self._http_json(
            f"{_API_BASE}/auth/login",
            {"username": "admin", "password": password, "tenant_id": "default"},
        )
        token = body.get("access_token") or (body.get("data") or {}).get("access_token")
        if status != 200 or not token:
            raise SystemExit(f"[queue] 登录失败 {status}: {str(body)[:300]}")
        return str(token)

    @staticmethod
    def _docker(*args: str, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["docker", *args], capture_output=True, text=True, timeout=timeout
        )

    def _training_containers(self) -> list[str]:
        proc = self._docker(
            "ps", "--filter", f"name={_CONTAINER_PREFIX}", "--format", "{{.Names}}"
        )
        if proc.returncode != 0:
            self.log(f"docker ps 失败：{proc.stderr.strip()[:200]}")
            return []
        return [x for x in proc.stdout.splitlines() if x.strip()]

    def _container_status(self, run_id: str) -> str | None:
        proc = self._docker(
            "inspect", "-f", "{{.State.Status}}", f"{_CONTAINER_PREFIX}{run_id}"
        )
        if proc.returncode != 0:
            return None
        return proc.stdout.strip() or None

    def _db_status(self, run_id: str) -> str | None:
        sql = (
            "SELECT status FROM admin_training_jobs "
            f"WHERE id='{run_id.replace(chr(39), '')}';"
        )
        proc = self._docker(
            "exec", _DB_CONTAINER, "psql", "-U", "quantmind",
            "-d", "quantmind", "-t", "-A", "-c", sql,
        )
        if proc.returncode != 0:
            return None
        return proc.stdout.strip() or None

    # ---------- 等待原语 ----------

    def _gate_ready(self) -> bool:
        if self.wait_for_file is None:
            return True
        if self.wait_for_file.is_file():
            return True
        # 兜底：前序队列三单均已终态时，即使 DONE 标记丢失也放行。
        prev = self._read_json(self.wait_for_file.parent / "status.json") or {}
        tags = prev.get("tags") or {}
        return all(
            str((tags.get(tag) or {}).get("status")) in _TERMINAL for tag in _GATE_TAGS
        )

    def _wait_gate(self) -> None:
        announced = False
        while not self._gate_ready():
            if not announced:
                self.log(f"等待前序队列完成：{self.wait_for_file}")
                announced = True
            time.sleep(_GATE_POLL_S)
        if self.wait_for_file is not None:
            self.log("前序队列已完成，开始本队列")

    def _wait_no_training_container(self) -> None:
        announced = False
        while True:
            names = self._training_containers()
            if not names:
                if announced:
                    self.log("前序训练容器已退出，等待单飞锁释放")
                return
            if not announced:
                self.log(f"等待前序训练容器退出：{', '.join(names)}")
                announced = True
            time.sleep(30.0)

    def _submit(self, payload: dict[str, Any], password: str) -> str:
        token = "" if self._internal_headers else self._login(password)
        waited = 0.0
        while True:
            status, body = self._http_json(
                f"{_API_BASE}/models/run-training", payload, token=token, timeout=180
            )
            if status in (200, 201, 202):
                run_id = str(body.get("runId") or body.get("run_id") or "")
                if not run_id:
                    raise RuntimeError(f"提交响应缺 runId：{str(body)[:400]}")
                self.log(
                    f"已提交 run_id={run_id} missingFeatures="
                    f"{body.get('missingFeatureCount')}"
                )
                return run_id
            if status == 401:
                if self._internal_headers:
                    raise RuntimeError(
                        f"内部调用认证被拒 401（检查 INTERNAL_CALL_SECRET 与用户角色）: "
                        f"{str(body)[:300]}"
                    )
                token = self._login(password)
                continue
            if status == 409:
                if waited >= _LOCK_RETRY_MAX_S:
                    raise RuntimeError(f"单飞锁占用超时：{str(body)[:300]}")
                self.log(f"单飞锁占用中（{str(body.get('detail'))[:120]}），30s 后重试")
                time.sleep(30.0)
                waited += 30.0
                continue
            raise RuntimeError(f"提交失败 {status}: {str(body)[:600]}")

    def _wait_for_start(self, run_id: str) -> None:
        waited = 0.0
        while waited <= _START_WAIT_MAX_S:
            if self._container_status(run_id) is not None:
                self.log(f"容器已启动：{_CONTAINER_PREFIX}{run_id}")
                return
            db_status = self._db_status(run_id)
            if db_status and db_status.lower() in _TERMINAL:
                self.log(f"容器未起但 DB 已终态：{db_status}")
                return
            time.sleep(_POLL_S)
            waited += _POLL_S
        raise RuntimeError(f"容器在 {_START_WAIT_MAX_S:.0f}s 内未启动：{run_id}")

    def _wait_for_exit(self, run_id: str) -> str | None:
        self.log(f"等待训练结束：{run_id}")
        last = ""
        while True:
            state = self._container_status(run_id)
            if state is None:
                db_status = self._db_status(run_id)
                self.log(f"容器已退出，DB 状态={db_status}")
                return db_status
            if state != last:
                self.log(f"容器状态={state}")
                last = state
            if state in ("exited", "dead"):
                db_status = self._db_status(run_id)
                self.log(f"容器状态={state}，DB 状态={db_status}")
                return db_status
            time.sleep(_POLL_S)

    # ---------- 结果汇总 ----------

    def _collect_result(self, run_id: str, db_status: str | None) -> dict[str, Any]:
        result_path = _JOBS_DIR / run_id / "result.json"
        eval_path = _JOBS_DIR / run_id / "eval_report.json"
        waited = 0.0
        result = None
        while waited <= _RESULT_WAIT_MAX_S:
            result = self._read_json(result_path)
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
        eval_report = self._read_json(eval_path) or {}
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

    # ---------- 主流程 ----------

    def run(self, password: str) -> int:
        manifest = self._read_json(self.manifest) or {}
        queue = manifest.get("queue") or []
        if not queue:
            raise SystemExit(f"[queue] manifest 无 queue 条目：{self.manifest}")

        old = self._read_json(self.status_path)
        if isinstance(old, dict):
            self.state = old

        self.log(
            f"=== 训练队列启动：{len(queue)} 单（{', '.join(str(x.get('tag')) for x in queue)}） ==="
        )
        self._wait_gate()

        failures = 0
        for idx, item in enumerate(queue, 1):
            tag = str(item.get("tag") or "")
            entry = self.state["tags"].get(tag) or {}
            if entry.get("status") == "completed":
                self.log(f"[{tag}] 已完成，跳过（run_id={entry.get('run_id')}）")
                continue

            payload = self._read_json(self.state_dir / str(item.get("payload") or ""))
            if payload is None:
                failures += 1
                self.log(f"[{tag}] 载荷缺失/不可解析：{item.get('payload')}")
                continue

            self._wait_no_training_container()
            run_id = str(entry.get("run_id") or "")
            try:
                if run_id and entry.get("status") == "submitted":
                    self.log(f"[{tag}] 续跑已提交单 run_id={run_id}")
                else:
                    self.log(
                        f"[{tag}] ({idx}/{len(queue)}) 提交：{payload.get('job_name')}"
                    )
                    run_id = self._submit(payload, password)
                    self.state["tags"][tag] = {
                        "status": "submitted",
                        "run_id": run_id,
                        "submitted_at": _now(),
                    }
                    self._save_state()

                self._wait_for_start(run_id)
                db_status = self._wait_for_exit(run_id)
                summary = self._collect_result(run_id, db_status)
                final = (
                    "completed"
                    if str(summary.get("status") or "").lower() == "completed"
                    else "failed"
                )
                self.state["tags"][tag] = {
                    **self.state["tags"].get(tag, {}),
                    "status": final,
                    "run_id": run_id,
                    "finished_at": _now(),
                    "summary": summary,
                }
                self._save_state()
                self.log(
                    f"[{tag}] 终态={final} "
                    f"指标={json.dumps(summary.get('rank_ic'), ensure_ascii=False)}"
                )
                if final != "completed":
                    failures += 1
                    if summary.get("error"):
                        self.log(f"[{tag}] 错误：{str(summary['error'])[:400]}")
            except Exception as exc:  # noqa: BLE001 —— 单子失败不能拖垮后续队列
                failures += 1
                self.state["tags"][tag] = {
                    **self.state["tags"].get(tag, {}),
                    "status": "failed",
                    "run_id": run_id or None,
                    "finished_at": _now(),
                    "error": str(exc),
                }
                self._save_state()
                self.log(f"[{tag}] 编排异常：{exc}")

        self.done_path.write_text(
            json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.log(f"=== 训练队列结束，失败 {failures} 单 ===")
        return 0 if failures == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--manifest", type=Path, required=True)
    ap.add_argument("--state-dir", type=Path, required=True)
    ap.add_argument("--wait-for-file", type=Path, default=None)
    args = ap.parse_args()

    password = os.getenv("QM_PASSWORD", "")
    internal_secret = os.getenv("QM_INTERNAL_CALL_SECRET", "")
    if not password and not internal_secret:
        raise SystemExit(
            "[queue] 未提供认证：QM_PASSWORD（admin 口令）或 "
            "QM_INTERNAL_CALL_SECRET（内部调用密钥）"
        )

    args.state_dir.mkdir(parents=True, exist_ok=True)
    driver = QueueDriver(
        args.manifest,
        args.state_dir,
        args.wait_for_file,
        internal_secret=internal_secret,
        internal_user_id=os.getenv("QM_INTERNAL_USER_ID", "10000001"),
        internal_tenant_id=os.getenv("QM_INTERNAL_TENANT_ID", "default"),
    )
    if driver.done_path.exists():
        driver.done_path.unlink()
    return driver.run(password)


if __name__ == "__main__":
    sys.exit(main())
