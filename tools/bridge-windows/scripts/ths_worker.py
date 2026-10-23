"""TDX 桥 · 同花顺 worker 启动器（含扫码登录端点）。

设计约束
--------
- 只绑 127.0.0.1（绝不对局域网暴露）；
- 凭据来源优先级：环境变量/`data/ths.env` > 扫码缓存 `data/ths_qr.json`；
- 上游代码在 vendor/ths（git clone，可 git pull 升级）；
  依赖在 vendor/ths_deps（离线 wheel，与桥/TQ 的全局 Python 隔离）；
- 只做行情（只读），不含任何交易能力。

扫码端点（供桥代理，桥侧再套一层 token 鉴权）
--------------------------------------------
- POST /api/qr/start   → 生成二维码，后台线程轮询扫码状态
- GET  /api/qr/status  → {state: idle|waiting|scanned|connecting|ok|error, qrid, url, account, error}
- GET  /api/qr.png     → 二维码 PNG（服务端出图，避免本地 Pillow 依赖）
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "vendor" / "ths" / "src"))
sys.path.insert(0, str(ROOT / "vendor" / "ths_deps"))


def main() -> None:
    parser = argparse.ArgumentParser(description="同花顺行情 worker（只读）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--env", default=str(ROOT / "data" / "ths.env"))
    args = parser.parse_args()

    import uvicorn  # noqa: PLC0415
    from fastapi import FastAPI  # noqa: PLC0415
    from fastapi.responses import JSONResponse, Response  # noqa: PLC0415
    from thspypc import qr_login  # noqa: PLC0415
    from thspypc.server.app import create_app  # noqa: PLC0415
    from thspypc.server.runtime import ThsRuntime, load_env  # noqa: PLC0415

    env_path = Path(args.env)
    qr_cache = Path(ROOT / "data" / "ths_qr.json")

    # 启动前：没有 env 账号时，尝试复用扫码缓存（上游自适应：先试再说）
    env_vals = load_env(str(env_path))
    if not (env_vals.get("THS_USERNAME") or os.environ.get("THS_USERNAME")):
        cached = qr_login.load_credentials(str(qr_cache))
        if cached:
            result, saved_at = cached
            if qr_login.is_credentials_expired(result, saved_at):
                print("[ths-worker] 扫码缓存已确定过期，忽略", flush=True)
            else:
                os.environ["THS_USERNAME"] = result.account
                os.environ["THS_PASSWORD"] = result.password
                print(f"[ths-worker] 复用扫码缓存凭证 account={result.account}", flush=True)

    runtime = ThsRuntime(env_path=str(env_path))
    app: FastAPI = create_app(runtime)

    state = {"state": "idle", "qrid": "", "url": "", "error": "",
             "account": "", "started_at": 0}
    state_lock = threading.Lock()

    def _set(**kw) -> None:
        with state_lock:
            state.update(kw)

    def _apply_and_connect(result) -> dict:
        """把扫码拿到的 account/password 注入 runtime 并重连。

        `_env` / `_client` / `_lifecycle_lock` 是上游 ThsRuntime 的内部字段；
        这里集中适配，上游若改名只需改这一个函数。
        """
        env = getattr(runtime, "_env", None)
        if isinstance(env, dict):
            env["THS_USERNAME"] = result.account
            env["THS_PASSWORD"] = result.password
        os.environ["THS_USERNAME"] = result.account
        os.environ["THS_PASSWORD"] = result.password
        lock = getattr(runtime, "_lifecycle_lock", None)
        if lock is not None:
            lock.acquire()
        try:
            old = getattr(runtime, "_client", None)
            runtime._client = None
            runtime._connected_once = False
        finally:
            if lock is not None:
                lock.release()
        if old is not None:
            try:
                old.disconnect()
            except Exception:  # noqa: BLE001
                pass
        return runtime.connect()

    def _poll(qrid: str, session, url: str) -> None:
        def on_status(data: dict) -> None:
            if data.get("status") == 2:
                _set(state="scanned")

        try:
            result = qr_login.poll_scan_status(qrid, session, timeout=180,
                                               interval=3, on_status=on_status)
        except Exception as exc:  # noqa: BLE001
            _set(state="error", error=f"{type(exc).__name__}: {exc}")
            return
        _set(state="connecting", account=result.account)
        try:
            qr_login.save_credentials(result, str(qr_cache))
            print(f"[ths-worker] 扫码凭证已缓存: {qr_cache}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[ths-worker] 扫码凭证落盘失败: {exc}", flush=True)
        try:
            res = _apply_and_connect(result)
        except Exception as exc:  # noqa: BLE001
            _set(state="error", error=f"{type(exc).__name__}: {exc}")
            return
        if res.get("success"):
            _set(state="ok", error="")
            print(f"[ths-worker] 扫码登录并连接成功 account={result.account}", flush=True)
        else:
            _set(state="error", error=str(res.get("error") or "登录失败"))

    @app.post("/api/qr/start")
    def qr_start():
        with state_lock:
            if state["state"] in ("waiting", "scanned", "connecting"):
                return dict(state)
        try:
            session = qr_login._session()  # noqa: SLF001 上游私有构造，集中适配
            qrid = qr_login.create_qrcode(session)
            url = qr_login.get_scan_url(qrid)
        except Exception as exc:  # noqa: BLE001
            _set(state="error", error=f"{type(exc).__name__}: {exc}")
            return JSONResponse({"state": "error", "error": state["error"]},
                                status_code=502)
        _set(state="waiting", qrid=qrid, url=url, error="", account="",
             started_at=int(time.time()))
        threading.Thread(target=_poll, args=(qrid, session, url),
                         name="ths-qr-poll", daemon=True).start()
        return {"state": "waiting", "qrid": qrid, "url": url}

    @app.get("/api/qr/status")
    def qr_status():
        with state_lock:
            return dict(state)

    @app.get("/api/qr.png")
    def qr_png(qrid: str = ""):
        with state_lock:
            q = qrid or state.get("qrid") or ""
        if not q:
            return JSONResponse({"error": "no qrid"}, status_code=404)
        try:
            png = qr_login.fetch_qr_png(q)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": f"{type(exc).__name__}: {exc}"},
                                status_code=502)
        return Response(content=png, media_type="image/png")

    print(f"[ths-worker] root={ROOT}", flush=True)
    print(f"[ths-worker] env={env_path} qr_cache={qr_cache} "
          f"listen={args.host}:{args.port}", flush=True)
    if not env_path.exists():
        print("[ths-worker] 提示：无 data/ths.env，可用网页扫码登录", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
