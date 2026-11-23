#!/usr/bin/env python3
"""光伏产业链全景：把 N 家公司的方向地图聚成一张横向对比表。

分层口径（重要）：**按主营收入构成分层，不按行业标签分层**。
典型例子是隆基——申万把它归在「光伏设备」，但 2025 年报里
「组件及电池」占营收 85.2%、「硅片及硅棒」仅 9.3%，所以这里归入「组件（一体化）」。
各家的归属依据写在 roster 的 basis 字段里，可追溯。

用法：
    python3 chain.py --json
    python3 chain.py
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

from direction_map import build, render_text  # noqa: F401

# symbol, 显示名, 分层, 分层依据
ROSTER: list[tuple[str, str, str, str]] = [
    ("600438.SH", "通威股份", "硅料", "硅料为主，电池环节占比上升"),
    ("688303.SH", "大全能源", "硅料", "高纯多晶硅单主业"),
    ("002129.SZ", "TCL中环", "硅片", "硅片为主"),
    ("601012.SH", "隆基绿能", "组件（一体化）", "年报：组件及电池占 85.2%，硅片仅 9.3%"),
    ("688223.SH", "晶科能源", "组件（一体化）", "组件为主，自供电池"),
    ("688599.SH", "天合光能", "组件（一体化）", "组件+分布式系统"),
    ("002459.SZ", "晶澳科技", "组件（一体化）", "组件为主"),
    ("300118.SZ", "东方日升", "组件（一体化）", "组件+储能"),
    ("002865.SZ", "钧达股份", "电池", "TOPCon 电池单环节"),
    ("600732.SH", "爱旭股份", "电池", "ABC/PERC 电池单环节"),
    ("300274.SZ", "阳光电源", "逆变器/储能", "逆变器+储能系统"),
    ("605117.SH", "德业股份", "逆变器/储能", "户储逆变器+微逆"),
    ("300763.SZ", "锦浪科技", "逆变器/储能", "组串逆变器"),
    ("688390.SH", "固德威", "逆变器/储能", "并网+储能逆变器"),
    ("300751.SZ", "迈为股份", "设备", "HJT 整线设备"),
    ("300724.SZ", "捷佳伟创", "设备", "电池片设备"),
    ("688516.SH", "奥特维", "设备", "组件串焊机+单晶炉"),
    ("603806.SH", "福斯特", "辅材", "光伏胶膜"),
    ("601865.SH", "福莱特", "辅材", "光伏玻璃"),
    ("688680.SH", "海优新材", "辅材", "光伏胶膜"),
]


def _median(xs: list[float]) -> float | None:
    vals = [x for x in xs if x is not None]
    return statistics.median(vals) if vals else None


def build_chain(data_dir: Path, facts_dir: Path) -> dict:
    rows = []
    for symbol, name, layer, basis in ROSTER:
        try:
            m = build(symbol, data_dir, facts_dir)
        except SystemExit:
            rows.append({"symbol": symbol, "name": name, "layer": layer, "basis": basis,
                         "missing": True})
            continue
        rev, cap = m["revenue"], m["capital"]
        pm = m.get("product_mix") or {}
        products = pm.get("product_mix") or []
        top = products[0] if products else None
        rows.append(
            {
                "symbol": symbol,
                "name": m.get("name") or name,
                "layer": layer,
                "basis": basis,
                "industry": m.get("industry"),
                "revenue_ttm": rev.get("revenue_ttm"),
                "net_profit_ttm": rev.get("net_profit_ttm"),
                "operating_cash_flow_ttm": rev.get("operating_cash_flow_ttm"),
                "gross_margin": rev.get("gross_margin_latest"),
                "revenue_yoy": rev.get("revenue_yoy"),
                "fixed_assets_yoy": cap.get("fixed_assets_yoy"),
                "inventory_yoy": cap.get("inventory_yoy"),
                "net_cash": cap.get("net_cash"),
                "accel": rev.get("accel"),
                "signals": [s["key"] for s in m.get("signals", [])],
                "has_annual_report": bool(products),
                "top_product": top["name"] if top else None,
                "top_product_share": top["share"] if top else None,
                "top_product_gm": top["gross_margin"] if top else None,
            }
        )

    layers = []
    for layer in dict.fromkeys(r[2] for r in ROSTER):
        group = [r for r in rows if r.get("layer") == layer and not r.get("missing")]
        if not group:
            continue
        losing = [r for r in group if (r.get("gross_margin") or 0) < 0]
        cash_neg = [r for r in group if (r.get("operating_cash_flow_ttm") or 0) < 0]
        layers.append(
            {
                "layer": layer,
                "count": len(group),
                "revenue_ttm": sum(r["revenue_ttm"] or 0 for r in group),
                "median_gross_margin": _median([r.get("gross_margin") for r in group]),
                "median_revenue_yoy": _median([r.get("revenue_yoy") for r in group]),
                "losing_count": len(losing),
                "negative_ocf_count": len(cash_neg),
                "names": [r["name"] for r in group],
            }
        )

    return {
        "chain": "光伏产业链",
        "as_of": "最新报告期（见各公司）",
        "rows": rows,
        "layers": layers,
        "classification_note": "分层按主营收入构成，不按行业标签；隆基已按年报归入组件（一体化）。",
    }


def main(argv: list[str] | None = None) -> int:
    from direction_map import resolve_data_dir

    ap = argparse.ArgumentParser(description="光伏产业链全景")
    ap.add_argument("--data-dir", default=None)
    ap.add_argument("--facts-dir", default=str(Path(__file__).parent / "facts"))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    data_dir = resolve_data_dir(args.data_dir)
    out = build_chain(data_dir, Path(args.facts_dir))

    if args.json:
        json.dump(out, sys.stdout, ensure_ascii=False, default=str)
        print()
        return 0

    print(f"{out['chain']}（{len(out['rows'])} 家）\n")
    hdr = f"{'公司':<10}{'分层':<14}{'TTM营收':>10}{'毛利率':>9}{'营收同比':>9}{'固资同比':>9}{'经营现金流':>12}"
    print(hdr)
    print("-" * len(hdr))
    for r in out["rows"]:
        if r.get("missing"):
            print(f"{r['name']:<10}{r['layer']:<14}{'数据缺失':>10}")
            continue
        print(
            f"{r['name']:<10}{r['layer']:<14}"
            f"{(r['revenue_ttm'] or 0) / 1e8:>9.0f}亿"
            f"{(r['gross_margin'] or 0) * 100:>8.1f}%"
            f"{(r['revenue_yoy'] or 0) * 100:>8.0f}%"
            f"{(r['fixed_assets_yoy'] or 0) * 100:>8.0f}%"
            f"{(r['operating_cash_flow_ttm'] or 0) / 1e8:>10.0f}亿"
        )
    print("\n分环节：")
    for lay in out["layers"]:
        print(
            f"  {lay['layer']:<14} {lay['count']} 家｜合计营收 {lay['revenue_ttm'] / 1e8:,.0f}亿"
            f"｜毛利率中位 {(lay['median_gross_margin'] or 0) * 100:>5.1f}%"
            f"｜亏损 {lay['losing_count']} 家｜经营现金流为负 {lay['negative_ocf_count']} 家"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
