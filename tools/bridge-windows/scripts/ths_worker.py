"""TDX 桥 · 同花顺 worker 启动器。

设计约束
--------
- 只绑 127.0.0.1（绝不对局域网暴露）；
- 凭据只从 data/ths.env 读取（THS_USERNAME / THS_PASSWORD / THS_IMEI）；
- 上游代码在 vendor/ths（git clone，可 git pull 直接升级）；
  依赖在 vendor/ths_deps（离线 wheel 安装，与桥/TQ 的全局 Python 隔离）；
- 本进程只做行情（只读），不含任何交易能力。
"""
from __future__ import annotations

import argparse
import sys
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
    from thspypc.server.app import create_app  # noqa: PLC0415
    from thspypc.server.runtime import ThsRuntime  # noqa: PLC0415

    env_path = Path(args.env)
    print(f"[ths-worker] root={ROOT}", flush=True)
    print(f"[ths-worker] env={env_path} listen={args.host}:{args.port}", flush=True)
    if not env_path.exists():
        print("[ths-worker] 警告：凭据文件不存在，登录会失败（/api/connect）", flush=True)
    app = create_app(ThsRuntime(env_path=str(env_path)))
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
