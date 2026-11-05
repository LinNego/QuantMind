#!/usr/bin/env python3
"""strategy_gate 的自测 —— 证明这个检查器不是摆设。

一个从来不会 FAIL 的检查器，比没有检查器更危险。
所以这里放三个已知答案的样本：

    bad_timing  故意写错时序  → 必须 FAIL
    bad_cost    故意漏印花税  → 必须 FAIL
    good        规范写法      → 必须 PASS

用法：
    python research/checks/selftest.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

GATE = Path(__file__).resolve().parent / "strategy_gate.py"

BAD_TIMING = '''
import pandas as pd
universe = df[(~df.is_st) & (df.volume > 0) & (df.gap <= 10)
              & (df.list_date.shift(60).notna())]
universe = universe[~universe.zt_price.eq(universe.close)]
w = scores.rank(axis=1, pct=True).gt(0.9).astype(float)
ret = prices.pct_change(fill_method=None)
nav = (w * ret).sum(axis=1)          # ← 没有 shift，拿今天的决定吃今天的收益
COMMISSION = 0.00025      # 佣金
STAMP = 0.0005            # 印花税
TRANSFER = 0.00001        # 过户费
SLIPPAGE = 0.001          # 滑点
warmup = 20               # 热启动
'''

BAD_COST = '''
import pandas as pd
universe = df[(~df.is_st) & (df.volume > 0) & (df.gap <= 10)
              & (df.list_date.shift(60).notna())]
universe = universe[~universe.zt_price.eq(universe.close)]
w = scores.shift(1)
ret = prices.pct_change(fill_method=None)
nav = (w.shift(1) * ret).sum(axis=1)
COMMISSION = 0.00025      # 佣金
TRANSFER = 0.00001        # 过户费
SLIPPAGE = 0.001          # 滑点
# ← 忘记印花税
start = 20170116
warmup = 20               # 热启动
'''

GOOD = '''
import pandas as pd
universe = df[(~df.is_st) & (df.volume > 0) & (df.gap <= 10)
              & (df.list_date.shift(60).notna())]
universe = universe[~universe.zt_price.eq(universe.close)]
w = scores.shift(1)
ret = prices.pct_change(fill_method=None)
nav = (w.shift(1) * ret).sum(axis=1)
COMMISSION = 0.00025      # 佣金 万2.5 双边
STAMP = 0.0005            # 印花税 万5 仅卖出
TRANSFER = 0.00001        # 过户费 万0.1
SLIPPAGE = 0.001          # 滑点 0.1%/边
start = 20170116
warmup = 20               # 热启动
'''

CASES = [
    ("bad_timing", BAD_TIMING, 1, "没有 shift"),
    ("bad_cost",   BAD_COST,   1, "印花税"),
    ("good",       GOOD,       0, None),
]


def main() -> int:
    ok = True
    with tempfile.TemporaryDirectory() as td:
        for name, body, want, expect in CASES:
            f = Path(td) / f"{name}.py"
            f.write_text(body, encoding="utf-8")
            r = subprocess.run([sys.executable, str(GATE), str(f)],
                               capture_output=True, text=True)
            fails = [l for l in r.stdout.split("\n") if "FAIL" in l and "项 FAIL" not in l]
            got_fail = r.returncode != 0
            good = got_fail == (want != 0)
            if expect:                       # 还必须命中「预期的那一条」，不能错杀
                good = good and any(expect in l for l in fails)
            ok &= good
            print(f"{'✅' if good else '❌'} {name:<12} 期望 exit={want} 实际 exit={r.returncode}"
                  f" ｜ 命中 FAIL {len(fails)} 条"
                  f"{(' ｜ 预期关键词：' + expect) if expect else ''}")
            for l in fails[:4]:
                print(f"      {l.strip()}")
    print("\n结论：" + ("自测通过 —— 检查器能真的拦住已知错误。" if ok else "自测失败 —— 检查器有漏检。"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
