#!/usr/bin/env python3
"""策略代码检查器 —— 5 问的前 4 问，机器能查的部分。

用法：
    python research/checks/strategy_gate.py <策略文件.py>
    qcheck strategy <策略文件.py>

输出三态：
    PASS     机器确认没问题
    FAIL     机器确认有问题 —— 不许看净值
    MANUAL   机器查不了，必须人回答

退出码：有 FAIL 时为 1。

设计原则（重要）：
    检查项来自「已知失败模式清单」，不是从代码里长出来的。
    每一条规则都对应阶段 0 亲手验证过的一个坑，或验收清单 5 问里的一句。
    改规则时要写清楚「这条是为了防什么」。
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

PASS, FAIL, WARN, MANUAL = "PASS", "FAIL", "WARN", "MANUAL"


# --------------------------------------------------------------------------
# 只看代码，不看注释
# --------------------------------------------------------------------------
# 一个「注释里写了印花税」的文件，不该通过成本检查。
# 所以先剥掉注释和文档字符串，再做匹配。行号保持不变。
TRIPLE_D = '"' * 3
TRIPLE_S = "'" * 3


def code_only(src: str) -> str:
    def blank(m: re.Match) -> str:
        return "\n" * m.group(0).count("\n")

    src = re.sub(TRIPLE_D + r"[\s\S]*?" + TRIPLE_D, blank, src)
    src = re.sub(TRIPLE_S + r"[\s\S]*?" + TRIPLE_S, blank, src)
    # 去掉行尾注释（不处理 # 在字符串里的边角情况，够用）
    return "\n".join(re.sub(r"#.*$", "", ln) for ln in src.split("\n"))


@dataclass
class Result:
    group: str
    level: str
    msg: str
    line: int | None = None


# --------------------------------------------------------------------------
# ① 时序 / 未来数据
# --------------------------------------------------------------------------
# 已知失败模式：用 T 日收盘价打分、又用 T 日收盘价成交 —— 能做出年化 34817%
# 已知失败模式：features_daily 的 future_return_* 是标签，不是特征
# 已知失败模式：长期停牌整行消失，shift() 数的是「行」不是「天」
def check_timing(src: str, lines: list[str]) -> list[Result]:
    out: list[Result] = []

    # 裸乘：w * ret 没有 shift
    bare = []
    for i, ln in enumerate(lines, 1):
        code = ln.split("#")[0]
        if re.search(r"\bw\b\s*\*\s*(ret|returns|fwd_ret|fwd)", code) and "shift" not in code:
            bare.append(i)
    if bare:
        out.append(Result("①时序", FAIL,
                          f"L{','.join(map(str, bare))}：`w * ret` 没有 shift —— 拿今天的决定吃今天的收益", bare[0]))
    shifts = len(re.findall(r"\.shift\s*\(", src))
    if shifts:
        out.append(Result("①时序", PASS, f"找到 `shift(` 共 {shifts} 处"))
    elif not bare:
        out.append(Result("①时序", WARN, "没找到 `shift(` —— 确认你的时序是在别的形式里保证的"))

    # 标签泄漏
    leaks = re.findall(r"future_return_\w+", src)
    if leaks:
        out.append(Result("①时序", FAIL, f"出现标签泄漏列：{', '.join(sorted(set(leaks)))}"))
    else:
        out.append(Result("①时序", PASS, "无 `future_return_*`"))

    # SELECT *
    if re.search(r"SELECT\s+\*", src, re.I):
        out.append(Result("①时序", FAIL, "出现 `SELECT *` —— 会把 6 个泄漏列带进特征"))

    # 前向填充：填在「权重」上是对的（持仓不动），填在「价格」上是错的
    fill_lines = [i for i, ln in enumerate(lines, 1)
                  if re.search(r"(ffill|fillna\(\s*method\s*=\s*['\"]pad)", ln.split("#")[0])]
    if fill_lines:
        on_price = [i for i in fill_lines
                    if re.search(r"(close|open|high|low|price|px\b)",
                                 lines[i - 1].split("#")[0], re.I)]
        if on_price:
            out.append(Result("①时序", WARN,
                              f"L{','.join(map(str, on_price))} 对**价格**做了 ffill —— 停牌期间不该有价格", on_price[0]))
        else:
            out.append(Result("①时序", PASS, f"ffill 用在权重/持仓上（{len(fill_lines)} 处），合法"))

    # shift 跨停牌断档：有 shift 就必须有断档处理
    if shifts and not re.search(r"(pdt|prev_dt|gap|停牌|suspend|交易日间隔|n_trade_days)", src):
        out.append(Result("①时序", WARN,
                          "有 shift 但没有断档处理 —— 长期停牌复牌当天，shift 会跨到几年前的价"))
    return out


# --------------------------------------------------------------------------
# ② 股票池
# --------------------------------------------------------------------------
# 已知失败模式：100 只 ST 涨跌幅只有 ±5%、机构不能买
# 已知失败模式：254 行 volume=0 是临时停牌；长期停牌是整行消失（两种都要滤）
# 已知失败模式：301569.SZ 上市前 8 天 zaf=401.71%，会稳排动量第一名
# 已知失败模式：新股头几天不设涨跌幅
UNIVERSE = [
    ("ST",        r"(?:\bis_st\b|['\"]\*?ST['\"]|\*ST|含\s*ST|special_treatment)", "剔除 ST / *ST"),
    ("停牌-短",    r"(volume\s*>\s*0|volume\s*==\s*0|成交量)",                  "剔除当日无成交（volume=0）"),
    ("停牌-长",    r"(近\s*\d+\s*天|无数据|gap|pdt|停牌天数|suspended_days)",     "剔除长期停牌（整行不存在）"),
    ("新股",      r"(list_date|上市|ipo|new_stock|上市天数)",                     "剔除上市不满 N 天"),
    ("涨跌停",    r"(zt_price|dt_price|涨停|跌停|limit_up|limit_down)",           "剔除涨停买不进 / 跌停卖不出"),
]


def check_universe(src: str) -> list[Result]:
    out: list[Result] = []
    hit = {}
    for name, pat, desc in UNIVERSE:
        hit[name] = bool(re.search(pat, src, re.I))
        out.append(Result("②股票池", PASS if hit[name] else FAIL,
                          f"{desc} · {'找到' if hit[name] else '没找到'}"))
    if not hit["停牌-长"] and hit["停牌-短"]:
        out.append(Result("②股票池", WARN,
                          "只处理了 volume=0，没处理「长期停牌整行消失」—— 两条缺一不可"))
    return out


# --------------------------------------------------------------------------
# ③ 成本
# --------------------------------------------------------------------------
# 已知失败模式：印花税只在卖出时收，2023-08-28 之后是万 5（不是千 1）
# 已知失败模式：滑点是最容易被「忘记」的一项，且它常常比佣金大
COST = [
    ("佣金",   r"(佣金|commission|0\.00025|万\s*2\.5|万分之\s*2\.5)", "佣金 万2.5 双边，最低 5 元"),
    ("印花税", r"(印花税|stamp|0\.0005|万\s*5|万分之\s*5)",           "印花税 万5，仅卖出"),
    ("过户费", r"(过户费|transfer|0\.00001|万分之\s*0\.1)",            "过户费 万0.1 双边"),
    ("滑点",   r"(滑点|slippage|0\.001|0\.1\s*%)",                     "滑点 0.1%/边"),
]


def check_cost(src: str) -> list[Result]:
    out: list[Result] = []
    have = 0
    for name, pat, desc in COST:
        ok = bool(re.search(pat, src, re.I))
        have += ok
        out.append(Result("③成本", PASS if ok else FAIL, f"{desc} · {'找到' if ok else '没找到'}"))
    if have and have < 4:
        out.append(Result("③成本", WARN, f"只有 {have}/4 项 —— 缺一项就可能差出好几个点/年"))
    if re.search(r"(千\s*1|0\.001\s*#?\s*印花|stamp.*0\.001)", src):
        out.append(Result("③成本", FAIL, "印花税写成千 1 —— 2023-08-28 起是万 5"))
    return out


# --------------------------------------------------------------------------
# ④ 回测窗口
# --------------------------------------------------------------------------
# 已知失败模式：pe_ttm 等 6 列 2017-01-16 才有值，用 2016 当起点会整年空仓且不报错
def check_window(src: str, lines: list[str]) -> list[Result]:
    out: list[Result] = []
    # 支持 2016 / 20160101 / 2016-01-01 三种写法
    early = sorted(set(re.findall(r"(?<!\d)(201[0-6])(?:\d{4})?(?!\d)", src.replace("-", ""))))
    uses_val = bool(re.search(r"(pe_ttm|pe_static|pb\b|ps_ttm|valuation|\bval\b)", src, re.I))

    if early and uses_val:
        out.append(Result("④窗口", FAIL,
                          f"用到估值且出现 2016 及更早（{', '.join(early)}）—— `pe_ttm` 从 2017-01-16 才有值"))
    elif early:
        out.append(Result("④窗口", PASS,
                          f"起点含 2016（{', '.join(early)}）但只用价格 —— 价格数据 2016 年可用"))
    elif re.search(r"2017011[67]", src.replace("-", "")):
        out.append(Result("④窗口", PASS, "回测起点 ≥ 2017-01-16"))
    else:
        out.append(Result("④窗口", MANUAL, "没识别出回测起点 —— 确认它 ≥ 2017-01-16（`pe_ttm` 起始日）"))

    if re.search(r"(warmup|warm_up|预热|热启动|缓冲)", src, re.I):
        out.append(Result("④窗口", PASS, "有热启动处理"))
    else:
        out.append(Result("④窗口", WARN, "没找到热启动 —— 前 N 天因子为空，别算进收益"))

    if re.search(r"features_daily\.close|feat\.close", src) and re.search(r"(成交|fill|price\s*=|买入)", src):
        out.append(Result("④窗口", FAIL, "成交价来自 features_daily.close —— 那是**后复权**，不是真实价格"))
    return out


# --------------------------------------------------------------------------
# MANUAL：机器查不了，必须人回答
# --------------------------------------------------------------------------
MANUAL_Q = [
    "这个 shift 的位置，在「决定」和「成交」之间吗？",
    "股票池四个数字（ST/停牌/新股/涨跌停各剔多少只）你都核对过吗？",
    "成本四项的费率，和你账户的实际情况一致吗？",
    "如果这个结果是假的，会在哪个数字上第一个露馅？",
]


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    p = Path(argv[1])
    if not p.is_file():
        print(f"[错误] 找不到文件：{p}")
        return 2

    raw = p.read_text(encoding="utf-8")
    src = code_only(raw)          # ← 注释不参与判定
    lines = src.split("\n")

    results: list[Result] = []
    results += check_timing(src, lines)
    results += check_universe(src)
    results += check_cost(src)
    results += check_window(src, lines)

    w = max(len(r.group) for r in results) + 2
    print(f"\n检查：{p}\n" + "─" * 68)
    for r in results:
        mark = {"PASS": "✅", "FAIL": "❌", "WARN": "⚠️ ", "MANUAL": "❓"}[r.level]
        print(f"  {mark} {r.group:<{w}} {r.level:<6} {r.msg}")

    n_fail = sum(r.level == FAIL for r in results)
    n_warn = sum(r.level == WARN for r in results)

    print("─" * 68)
    if n_fail:
        print(f"\n❌ {n_fail} 项 FAIL，{n_warn} 项 WARN —— **不允许看净值**，打回去改。\n")
    else:
        print(f"\n✅ 0 项 FAIL，{n_warn} 项 WARN —— 机器这一关过了。\n")
        print("但下面 4 个问题机器查不了，必须你自己答（答不出一样不许看净值）：\n")
        for i, q in enumerate(MANUAL_Q, 1):
            print(f"  ❓ {i}. {q}")
        print()

    print(f"文件：{p.resolve()}")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
