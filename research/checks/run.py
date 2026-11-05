#!/usr/bin/env python3
"""检查器总入口。

用法：
    python research/checks/run.py strategy <策略文件.py>    # 策略代码 5 问（前 4 问）
    python research/checks/run.py selftest                  # 检查器自测
    qcheck strategy <文件>  /  qcheck selftest

规则来源：research/参考/验收清单.md 的 5 问 + 阶段 0 亲手验证过的坑。
原则：输出 PASS / FAIL / WARN / MANUAL 三态，有 FAIL 就退出码非 0。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print(__doc__)
        return 0 if len(argv) > 1 else 2

    cmd, rest = argv[1], argv[2:]
    if cmd == "strategy":
        target = HERE / "strategy_gate.py"
    elif cmd == "selftest":
        target = HERE / "selftest.py"
    else:
        print(f"[错误] 未知命令：{cmd}\n")
        print(__doc__)
        return 2

    return subprocess.call([sys.executable, str(target), *rest])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
