#!/usr/bin/env python3
"""公司方向地图（Direction Map）分析内核。

把一家 A 股公司拆成三层证据，用来判断「它正在从什么变成什么」：

  ① 钱从哪来   —— 单季营收 / 毛利率 / 净利的 8 季轨迹
  ② 钱投向哪   —— 在建工程、固定资产、存货、有息负债、研发强度
  ③ 它自己说   —— 年报「主营业务分产品」收入结构（由 pdf_facts 抓取补齐）

数据全部来自本地 QuantDB parquet，不联网（PDF 部分由 pdf_facts.py 单独抓取并缓存）。

口径（已实测校验，勿随意改动）：
  income.parquet        单季口径（茅台 2025 四季营收合计 1688.4 亿，对得上）
  cashflow.parquet      **年初至今累计**口径（茅台 2024 年报经营现金流 924.6 亿，对得上）
                        → 必须在本脚本内做年度内差分，才能得到单季经营现金流
  balance.parquet       期末时点值

用法：
  python3 direction_map.py --symbol 601012.SH            # 人类可读
  python3 direction_map.py --symbol 601012.SH --json     # 给插件/程序消费
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import pandas as pd

# ---------------------------------------------------------------------------
# 数据目录
# ---------------------------------------------------------------------------

_DEFAULT_DATA_DIRS = [
    Path(os.environ.get("QM_QUANTDB_DATA_DIR", "")),
    Path("/data/quantdb"),
    Path(__file__).resolve().parents[2] / "data" / "quantdb",
]


def resolve_data_dir(explicit: str | None = None) -> Path:
    if explicit:
        p = Path(explicit)
        if not (p / "3_financial_data").is_dir():
            raise SystemExit(f"数据目录不含 3_financial_data: {p}")
        return p
    for cand in _DEFAULT_DATA_DIRS:
        if str(cand) and (cand / "3_financial_data").is_dir():
            return cand
    raise SystemExit(
        "找不到 QuantDB 数据目录，请用 --data-dir 指定（应包含 3_financial_data/）"
    )


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------


def _read(data_dir: Path, table: str, symbol: str) -> pd.DataFrame | None:
    f = data_dir / "3_financial_data" / table / f"{symbol}.parquet"
    if not f.exists():
        return None
    df = pd.read_parquet(f)
    if df.empty:
        return None
    df["m_timetag"] = df["m_timetag"].astype(str).str[:8]
    return df.sort_values("m_timetag").reset_index(drop=True)


def _num(df: pd.DataFrame, col: str) -> pd.Series:
    if col not in df.columns:
        return pd.Series([float("nan")] * len(df), index=df.index)
    return pd.to_numeric(df[col], errors="coerce")


def _single_quarter_flow(df: pd.DataFrame, col: str) -> list[float]:
    """把累计口径的现金流差分成本季值（按年度重置）。"""
    vals = _num(df, col).tolist()
    years = [t[:4] for t in df["m_timetag"].tolist()]
    out: list[float] = []
    prev = 0.0
    prev_year = None
    for v, y in zip(vals, years):
        if pd.isna(v):
            out.append(float("nan"))
            prev, prev_year = 0.0, y
            continue
        base = 0.0 if y != prev_year else prev
        out.append(v - base)
        prev, prev_year = v, y
    return out


def _yoy(series: list[float], back: int = 4) -> float:
    if len(series) <= back or not series[-1 - back] or pd.isna(series[-1 - back]):
        return float("nan")
    return series[-1] / series[-1 - back] - 1.0


def _qoq(series: list[float]) -> float:
    if len(series) < 2 or not series[-2] or pd.isna(series[-2]):
        return float("nan")
    return series[-1] / series[-2] - 1.0


def _safe(x) -> float | None:
    try:
        if x is None or (isinstance(x, float) and pd.isna(x)):
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 元信息：名称 / 行业 / 主营
# ---------------------------------------------------------------------------


def load_meta(data_dir: Path, symbol: str) -> dict:
    meta = {"name": symbol, "industry": None, "main_business": None}
    f = data_dir / "2_base_sector" / "instrument_detail" / "instrument_list.parquet"
    if f.exists():
        df = pd.read_parquet(f)
        row = df[df["Symbol"] == symbol]
        if not row.empty:
            r = row.iloc[0]
            meta["name"] = str(r.get("Name") or symbol)
            meta["industry"] = str(r.get("rs_hyname") or "") or None
            mb = r.get("MainBusiness")
            meta["main_business"] = str(mb)[:200] if isinstance(mb, str) and mb else None
    return meta


def load_sectors(data_dir: Path, symbol: str) -> list[str]:
    f = data_dir / "2_base_sector" / "sector_concept" / "sector_members.parquet"
    if not f.exists():
        return []
    df = pd.read_parquet(f)
    hit = df[df["Symbol"] == symbol]
    return [f"{r.SectorType}:{r.SectorName}" for r in hit.itertuples()][:8]


# ---------------------------------------------------------------------------
# ① 钱从哪来
# ---------------------------------------------------------------------------


def _align_pct(inc: pd.DataFrame, ps: pd.DataFrame | None, col: str) -> list[float]:
    """把 pershare_index 的百分比指标按报告期对齐到利润表行；值转为小数。"""
    if ps is None or col not in ps.columns:
        return [float("nan")] * len(inc)
    m = dict(zip(ps["m_timetag"].astype(str), pd.to_numeric(ps[col], errors="coerce")))
    return [
        (m[t] / 100.0 if t in m and not pd.isna(m[t]) else float("nan"))
        for t in inc["m_timetag"].astype(str)
    ]


def _period_days(period: str) -> int:
    return {"03": 91, "06": 182, "09": 273, "12": 365}.get(period[4:6], 365)


def layer_revenue(inc: pd.DataFrame, cmf: pd.DataFrame, ps: pd.DataFrame | None = None) -> dict:
    rev = _num(inc, "revenue").tolist()
    np_ = _num(inc, "net_profit_excl_min_int_inc").tolist()
    deduct = _num(inc, "deducted_net_profit").tolist()
    rd = _num(inc, "research_expenses").tolist()
    oper = _num(inc, "oper_profit").tolist()
    ocf = _single_quarter_flow(cmf, "net_cash_flows_oper_act") if cmf is not None else [
        float("nan")
    ] * len(rev)

    # 营业成本明细在本地库里大面积为空（实测 17 家里仅 7 家有零星一期），
    # 因此毛利率统一取 pershare_index 的 YTD 口径（年报期末值已与年报原文核对一致）。
    gm = _align_pct(inc, ps, "gross_margin")
    nm = _align_pct(inc, ps, "net_margin")
    oper_m = [
        (o / r) if r and not pd.isna(r) and not pd.isna(o) else float("nan")
        for o, r in zip(oper, rev)
    ]

    qs = []
    for i in range(max(0, len(rev) - 8), len(rev)):
        qs.append(
            {
                "period": inc["m_timetag"].iloc[i],
                "quarter": _quarter_label(inc["m_timetag"].iloc[i]),
                "revenue": _safe(rev[i]),
                "gross_margin": _safe(gm[i]),
                "net_profit": _safe(np_[i]),
                "deducted_net_profit": _safe(deduct[i] if i < len(deduct) else None),
                "operating_cash_flow": _safe(ocf[i] if i < len(ocf) else None),
                "research_expense": _safe(rd[i] if i < len(rd) else None),
            }
        )

    def ttm(series: list[float]) -> float | None:
        tail = [x for x in series[-4:] if not pd.isna(x)]
        return _safe(sum(tail)) if len(tail) == 4 else None

    rev_ttm = ttm(rev)
    np_ttm = ttm(np_)
    rd_ttm = ttm(rd)
    ocf_ttm = ttm(ocf)

    return {
        "latest_period": inc["m_timetag"].iloc[-1] if len(inc) else None,
        "quarters": qs,
        "revenue_ttm": rev_ttm,
        "net_profit_ttm": np_ttm,
        "operating_cash_flow_ttm": ocf_ttm,
        "research_expense_ttm": rd_ttm,
        "revenue_yoy": _safe(_yoy(rev)),
        "revenue_qoq": _safe(_qoq(rev)),
        "gross_margin_latest": _safe(gm[-1]) if gm else None,
        "gross_margin_ytd": True,
        "gross_margin_yoy_pp": _safe(
            (gm[-1] - gm[-5]) * 100 if len(gm) >= 5 and not pd.isna(gm[-5]) else None
        ),
        "net_margin_latest": _safe(nm[-1]) if nm else None,
        "oper_margin_latest": _safe(oper_m[-1]) if oper_m else None,
        "ocf_to_profit_ttm": _safe(ocf_ttm / np_ttm) if np_ttm and np_ttm != 0 else None,
        "research_intensity_ttm": _safe(rd_ttm / rev_ttm) if rev_ttm else None,
        "accel": _growth_accel(rev),
    }


def _quarter_label(t: str) -> str:
    m = {"03": "Q1", "06": "Q2", "09": "Q3", "12": "Q4"}
    return f"{t[:4]}{m.get(t[4:6], '?')}"


def _growth_accel(rev: list[float]) -> str | None:
    """最新季同比 vs 上一季同比：降幅收窄 = 拐点信号。"""
    if len(rev) < 6:
        return None
    cur = _yoy(rev)
    prev = _yoy(rev[:-1])
    if pd.isna(cur) or pd.isna(prev):
        return None
    if cur > prev + 0.05:
        return "降幅收窄/加速向上"
    if cur < prev - 0.05:
        return "继续恶化"
    return "基本持平"


# ---------------------------------------------------------------------------
# ② 钱投向哪
# ---------------------------------------------------------------------------


def layer_capital(bal: pd.DataFrame, inc: pd.DataFrame, ps: pd.DataFrame | None = None) -> dict:
    def col(c):
        return _num(bal, c)

    cip = col("constru_in_process")
    fa = col("fix_assets")
    inv = col("inventories")
    ar = col("account_receivable") + col("bill_receivable").fillna(0)
    cash = col("cash_equivalents")
    st = col("shortterm_loan") + col("non_current_liability_in_one_year").fillna(0)
    lt = col("long_term_loans") + col("bonds_payable").fillna(0)
    debt = st + lt
    assets = col("tot_assets")
    liab = col("tot_liab")

    latest = {
        "period": bal["m_timetag"].iloc[-1] if len(bal) else None,
        "construction_in_progress": _safe(cip.iloc[-1]),
        "fixed_assets": _safe(fa.iloc[-1]),
        "inventory": _safe(inv.iloc[-1]),
        "receivables": _safe(ar.iloc[-1]),
        "cash": _safe(cash.iloc[-1]),
        "interest_bearing_debt": _safe(debt.iloc[-1]),
        "net_cash": _safe(cash.iloc[-1] - debt.iloc[-1]),
        "total_assets": _safe(assets.iloc[-1]),
        "debt_ratio": _safe(liab.iloc[-1] / assets.iloc[-1]) if assets.iloc[-1] else None,
    }

    def chg(series, back=4):
        if len(series) <= back or not series.iloc[-1 - back]:
            return None
        return _safe(series.iloc[-1] / series.iloc[-1 - back] - 1)

    latest["fixed_assets_yoy"] = chg(fa)
    latest["construction_yoy"] = chg(cip)
    latest["inventory_yoy"] = chg(inv)
    latest["receivables_yoy"] = chg(ar)

    # 资本开支强度：在建工程 / 固定资产
    latest["capex_ratio"] = (
        _safe(cip.iloc[-1] / fa.iloc[-1]) if len(cip) and fa.iloc[-1] else None
    )

    # 存货周转天数：本地库缺营业成本，改用 pershare_index 的周转率（YTD 次）折算
    turn = None
    if ps is not None and "inventory_turnover" in ps.columns:
        v = pd.to_numeric(ps.sort_values("m_timetag")["inventory_turnover"], errors="coerce")
        if len(v) and v.iloc[-1] and not pd.isna(v.iloc[-1]):
            turn = float(v.iloc[-1])
    latest["inventory_turnover"] = turn
    latest["inventory_days"] = (
        _safe(_period_days(latest["period"] or "1231") / turn) if turn else None
    )

    # 偿债与费用（pershare_index，YTD 口径）
    for key, colname in (
        ("interest_coverage", "interest_coverage"),
        ("current_ratio", "current_ratio"),
        ("quick_ratio", "quick_ratio"),
        ("three_fee_ratio", "three_fee_ratio"),
    ):
        latest[key] = None
        if ps is not None and colname in ps.columns:
            s = pd.to_numeric(ps.sort_values("m_timetag")[colname], errors="coerce")
            if len(s) and not pd.isna(s.iloc[-1]):
                latest[key] = float(s.iloc[-1])

    return latest


# ---------------------------------------------------------------------------
# 派生信号：把数字翻译成「方向」判断
# ---------------------------------------------------------------------------


def derive_signals(map_data: dict) -> list[dict]:
    sig: list[dict] = []
    rev = map_data["revenue"]
    cap = map_data["capital"]

    gm = rev.get("gross_margin_latest")
    if gm is not None:
        if gm < 0:
            sig.append({"key": "亏损出货", "tone": "bad", "detail": "单季毛利率为负，卖得越多亏得越多"})
        elif gm < 0.05:
            sig.append({"key": "微利", "tone": "warn", "detail": "毛利率不足 5%，处在价格战底部"})
        else:
            sig.append({"key": "有毛利", "tone": "ok", "detail": "毛利率为正，具备熬过出清的空间"})

    ocfp = rev.get("ocf_to_profit_ttm")
    if ocfp is not None:
        if ocfp < 0:
            sig.append({"key": "现金流出", "tone": "bad", "detail": "经营现金流为负，靠外部输血"})
        elif ocfp < 0.8:
            sig.append({"key": "利润不含金", "tone": "warn", "detail": "经营现金流低于净利润，盈利质量偏弱"})
        else:
            sig.append({"key": "现金优良", "tone": "ok", "detail": "经营现金流覆盖净利润"})

    fx = cap.get("fixed_assets_yoy")
    if fx is not None:
        if fx > 0.05:
            sig.append({"key": "逆势扩产", "tone": "info", "detail": f"固定资产同比 {fx:+.0%}，仍在投产能"})
        elif fx < -0.05:
            sig.append({"key": "产能收缩", "tone": "info", "detail": f"固定资产同比 {fx:+.0%}，在砍产能"})

    acc = rev.get("accel")
    if acc:
        sig.append({"key": "季度拐点", "tone": "info" if acc != "继续恶化" else "warn", "detail": acc})

    rd = rev.get("research_intensity_ttm")
    if rd is not None:
        sig.append({"key": "研发强度", "tone": "info", "detail": f"TTM 研发费用率 {rd:.1%}"})

    nc = cap.get("net_cash")
    if nc is not None:
        sig.append(
            {
                "key": "净现金",
                "tone": "ok" if nc > 0 else "warn",
                "detail": f"货币资金-有息负债 = {nc / 1e8:+.0f} 亿",
            }
        )
    return sig


# ---------------------------------------------------------------------------
# ③ 年报分产品（pdf_facts 缓存）
# ---------------------------------------------------------------------------


def load_pdf_facts(facts_dir: Path, symbol: str) -> dict | None:
    f = facts_dir / f"{symbol}.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def build(symbol: str, data_dir: Path, facts_dir: Path | None = None) -> dict:
    inc = _read(data_dir, "income", symbol)
    cmf = _read(data_dir, "cashflow", symbol)
    bal = _read(data_dir, "balance", symbol)
    ps = _read(data_dir, "pershare_index", symbol)
    if inc is None or bal is None:
        raise SystemExit(f"本地无 {symbol} 的财务数据（income/balance 缺失）")

    meta = load_meta(data_dir, symbol)
    rev = layer_revenue(inc, cmf, ps)
    cap = layer_capital(bal, inc, ps)

    out = {
        "symbol": symbol,
        **meta,
        "sectors": load_sectors(data_dir, symbol),
        "revenue": rev,
        "capital": cap,
    }
    out["signals"] = derive_signals(out)
    out["product_mix"] = load_pdf_facts(facts_dir, symbol) if facts_dir else None
    return out


def _fmt(v: float | None, unit: str = "亿", scale: float = 1e8, digits: int = 1) -> str:
    if v is None:
        return "—"
    return f"{v / scale:,.{digits}f}{unit}"


def _pct(v: float | None, digits: int = 1) -> str:
    return "—" if v is None else f"{v * 100:+.{digits}f}%"


def render_text(m: dict) -> str:
    rev, cap = m["revenue"], m["capital"]
    lines = [
        f"【{m['name']} {m['symbol']}】行业：{m.get('industry') or '—'}",
        f"最新报告期：{rev.get('latest_period')}",
        "",
        "① 钱从哪来",
        f"  TTM 营收 {_fmt(rev['revenue_ttm'])}｜TTM 归母 {_fmt(rev['net_profit_ttm'])}"
        f"｜TTM 经营现金流 {_fmt(rev['operating_cash_flow_ttm'])}",
        f"  单季营收同比 {_pct(rev['revenue_yoy'])}｜环比 {_pct(rev['revenue_qoq'])}"
        f"｜毛利率(YTD) {_pct(rev['gross_margin_latest'])}（同比 {rev.get('gross_margin_yoy_pp') or 0:+.1f}pp）"
        f"｜净利率(YTD) {_pct(rev.get('net_margin_latest'))}",
        "  近 8 季单季轨迹：",
    ]
    for q in rev["quarters"]:
        lines.append(
            f"    {q['quarter']}  营收 {_fmt(q['revenue']):>10}  毛利率YTD {_pct(q['gross_margin']):>7}"
            f"  归母 {_fmt(q['net_profit']):>10}  经营现金流 {_fmt(q['operating_cash_flow']):>10}"
        )
    lines += [
        "",
        "② 钱投向哪",
        f"  在建工程 {_fmt(cap['construction_in_progress'])}（同比 {_pct(cap.get('construction_yoy'))}）"
        f"｜固定资产 {_fmt(cap['fixed_assets'])}（同比 {_pct(cap.get('fixed_assets_yoy'))}）",
        f"  存货 {_fmt(cap['inventory'])}（同比 {_pct(cap.get('inventory_yoy'))}，周转 {cap.get('inventory_days') or 0:.0f} 天）"
        f"｜应收账款 {_fmt(cap['receivables'])}（同比 {_pct(cap.get('receivables_yoy'))}）",
        f"  货币资金 {_fmt(cap['cash'])}｜有息负债 {_fmt(cap['interest_bearing_debt'])}"
        f"｜净现金 {_fmt(cap['net_cash'])}｜资产负债率 {_pct(cap.get('debt_ratio'))}",
        "",
        "③ 它自己说（年报分产品）",
    ]
    pm = m.get("product_mix")
    if not pm:
        lines.append("  （未抓取年报，运行 pdf_facts.py 后可补齐）")
    else:
        lines.append(f"  报告年度：{pm.get('report_year')}  来源：{pm.get('source', '年报')}")
        for row in pm.get("product_mix", [])[:6]:
            lines.append(
                f"    {row['name']:<16} 收入 {row['revenue'] / 1e8:>9,.1f}亿 "
                f"占比 {row.get('share', 0) * 100:>5.1f}%  毛利率 {row.get('gross_margin') or 0:>6.1f}%"
            )
    lines += ["", "方向信号："]
    for s in m["signals"]:
        lines.append(f"  [{s['tone']}] {s['key']}：{s['detail']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="公司方向地图分析内核")
    ap.add_argument("--symbol", required=True, help="如 601012.SH")
    ap.add_argument("--data-dir", default=None)
    ap.add_argument("--facts-dir", default=None, help="年报分产品缓存目录")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    data_dir = resolve_data_dir(args.data_dir)
    facts_dir = Path(args.facts_dir) if args.facts_dir else Path(__file__).parent / "facts"
    m = build(args.symbol.strip().upper(), data_dir, facts_dir)

    if args.json:
        json.dump(m, sys.stdout, ensure_ascii=False, default=str)
        print()
    else:
        print(render_text(m))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
