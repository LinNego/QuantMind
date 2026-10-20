#!/usr/bin/env python3
"""研究总控（market-research）多档复盘 CLI。

宿主 crontab（JST = 北京 + 1）建议五条::

    40 12 * * 1-5  cd <repo> && POSTGRES_HOST=192.168.31.68 python3 scripts/research_control.py --slot midday
    20 17 * * 1-5  ... --slot close
    20 19 * * 1-5  ... --slot inference
    45 23 * * 1-5  ... --slot news
    10 3  * * 2-6  ... --slot night

用法::

    python3 scripts/research_control.py --status
    python3 scripts/research_control.py --list
    python3 scripts/research_control.py --slot midday --dry-run
    python3 scripts/research_control.py --slot news
    python3 scripts/research_control.py --slot close --date 2026-10-20 --if-missing

退出码见 :mod:`backend.shared.research_control.runner`。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / "backend")):
    if p not in sys.path:
        sys.path.insert(0, p)


def _hydrate_env() -> None:
    """把 ``.env`` / ``runtime.env`` 灌进本进程（只补空位，不覆盖真实环境变量）。

    研究总控在宿主 cron 里是新进程，``resolve_config`` 与 ``dossier`` 都按
    「进程环境 + runtime.env」读；不灌 ``.env`` 的话，只写在 ``.env`` 里的
    ``GLM_API_KEY`` / ``OPENAI_API_KEY`` 会让回落链失效。
    """
    try:
        from backend.shared.research_control.dossier import load_env

        for key, value in load_env().items():
            if value and not os.environ.get(key, "").strip():
                os.environ[key] = value
    except Exception:  # noqa: BLE001 - 读配置失败交给 run_slot 报未配置
        pass


def main() -> int:
    _hydrate_env()
    from backend.shared.research_control.runner import main as runner_main

    return runner_main()


if __name__ == "__main__":
    sys.exit(main())
