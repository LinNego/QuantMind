#!/usr/bin/env python3
"""抓取并解析 A 股年报，抽取出「公司方向」相关的原文事实。

只抽三样最影响方向判断的东西（都是本地财务库拿不到的）：
  1. 主营业务分产品收入（决定这家公司到底靠什么赚钱、分层该按什么分）
  2. 产销量（看产能是自用还是外销、库存压力）
  3. 研发投入 + 管理层对业务边界的原话（它自己说要变成什么）

结果写到 facts/<symbol>.json，供 direction_map.py 读取。

数据源：东方财富公告接口（免费、无需登录）
   列表 https://np-anotice-stock.eastmoney.com/api/security/ann
   正文 https://pdf.dfcfw.com/pdf/H2_<art_code>_1.pdf

用法：
    python3 pdf_facts.py --symbol 601012.SH --year 2025
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
ANN_API = (
    "https://np-anotice-stock.eastmoney.com/api/security/ann"
    "?sr=-1&page_size=50&page_index={page}&ann_type=A"
    "&client_source=web&stock_list={code}&f_node=0"
)
PDF_URL = "https://pdf.dfcfw.com/pdf/H2_{art}_1.pdf"


# ---------------------------------------------------------------------------
# 抓取
# ---------------------------------------------------------------------------


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as r:  # noqa: S310
        return json.loads(r.read().decode("utf-8", "ignore"))


def find_annual_report(symbol: str, year: int) -> tuple[str, str] | None:
    """返回 (art_code, title)。"""
    code = symbol.split(".")[0]
    want = f"{year}年年度报告"
    for page in range(1, 9):
        data = _get_json(ANN_API.format(page=page, code=code))
        rows = (data.get("data") or {}).get("list") or []
        if not rows:
            break
        for it in rows:
            title = it.get("title", "")
            if want in title and "摘要" not in title and "英文" not in title and "审计" not in title:
                return it.get("art_code", ""), title
    return None


def download_pdf(art_code: str, dest: Path) -> Path:
    """下载公告 PDF。

    注意：东财对裸 urllib 请求会返回一段 JS 反爬脚本（`<script>function a(a...`），
    而 curl 带正常 UA/Referer 可以拿到真 PDF，所以优先用 curl，失败再退回 urllib。
    """
    url = PDF_URL.format(art=art_code)
    if subprocess.run(
        ["curl", "-sS", "-m", "120", "-o", str(dest), "-H", UA["User-Agent"],
         "-H", "Referer: https://data.eastmoney.com/", url],
        capture_output=True,
        check=False,
    ).returncode == 0 and dest.exists() and dest.read_bytes()[:4] == b"%PDF":
        return dest
    req = urllib.request.Request(
        url,
        headers={**UA, "Accept": "*/*", "Referer": "https://data.eastmoney.com/"},
    )
    with urllib.request.urlopen(req, timeout=120) as r, dest.open("wb") as f:  # noqa: S310
        while chunk := r.read(1 << 16):
            f.write(chunk)
    if dest.read_bytes()[:4] != b"%PDF":
        raise SystemExit(f"下载到的不是 PDF（可能被反爬拦截）: {art_code}")
    return dest


def pdf_to_text(pdf: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise SystemExit("需要 pypdf：pip install pypdf") from None
    reader = PdfReader(str(pdf))
    return "\n".join((p.extract_text() or "") for p in reader.pages)


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------

NUM = r"-?[\d,]+\.\d{2}"
PRODUCT_ROW = re.compile(
    rf"^(?P<name>[\u4e00-\u9fa5A-Za-z0-9（）()·\-]{{2,24}}?)\s+"
    rf"(?P<rev>{NUM})\s+(?P<cost>{NUM})\s+(?P<gm>-?[\d.]+)\s+(?P<ryoy>-?[\d.]+)"
)
OUTPUT_ROW = re.compile(
    r"^(?P<name>[\u4e00-\u9fa5A-Za-z0-9（）()·\-]{2,24}?)\s+(?P<unit>[A-Za-z%]{1,6})\s+"
    r"(?P<prod>[\d,]+\.?\d*)\s+(?P<sales>[\d,]+\.?\d*)\s+(?P<inv>[\d,]+\.?\d*)"
)


def _f(x: str) -> float:
    return float(x.replace(",", ""))


def parse_product_mix(text: str) -> list[dict]:
    start = text.find("主营业务分产品情况")
    if start < 0:
        return []
    end = text.find("主营业务分地区情况", start)
    block = text[start : end if end > 0 else start + 4000]
    out, total = [], 0.0
    for line in block.splitlines():
        m = PRODUCT_ROW.match(line.strip())
        if not m:
            continue
        rev = _f(m.group("rev"))
        out.append(
            {
                "name": m.group("name").strip(),
                "revenue": rev,
                "cost": _f(m.group("cost")),
                "gross_margin": _f(m.group("gm")),
                "revenue_yoy": _f(m.group("ryoy")) / 100.0,
            }
        )
        total += rev
    for row in out:
        row["share"] = row["revenue"] / total if total else None
    return out


def parse_product_mix_tables(pdf_path: Path) -> list[dict]:
    """表格级兜底解析：pypdf 把跨行表格拍平后正则失效时用 pdfplumber 读表格。"""
    try:
        import pdfplumber
    except ImportError:
        return []

    num_re = re.compile(r"^-?[\d,]+\.\d{2}$")
    out: list[dict] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            if "主营业务分产品" not in text:
                continue
            for table in page.extract_tables():
                rows = [[(c or "").replace("\n", "").strip() for c in row] for row in table]
                header_at = next(
                    (
                        i
                        for i, row in enumerate(rows)
                        if any("营业收入" in c for c in row) and any("营业成本" in c for c in row)
                    ),
                    None,
                )
                if header_at is None:
                    continue
                for row in rows[header_at + 1 :]:
                    if not row or not row[0] or "地区" in row[0] or "合计" in row[0]:
                        break
                    if not re.match(r"^[\u4e00-\u9fa5A-Za-z]", row[0]):
                        break
                    nums = [c for c in row if num_re.match(c)]
                    if len(nums) < 2:
                        continue
                    out.append(
                        {
                            "name": row[0],
                            "revenue": _f(nums[0]),
                            "cost": _f(nums[1]),
                            "gross_margin": _f(nums[2]) if len(nums) > 2 else None,
                            "revenue_yoy": (_f(nums[3]) / 100.0) if len(nums) > 3 else None,
                        }
                    )
    total = sum(r["revenue"] for r in out)
    for row in out:
        row["share"] = row["revenue"] / total if total else None
    return out



RATIO_ROW = re.compile(
    r"(?P<name>[\u4e00-\u9fa5A-Za-z（）()、/·\-]{2,40}?)\s*"
    r"(?P<amt>[\d,]+\.\d{2})\s+(?P<share>[\d.]+)%\s+"
    r"(?P<prev>[\d,]+\.\d{2})\s+(?P<prev_share>[\d.]+)%\s+(?P<yoy>-?[\d.]+)%"
)


def parse_product_mix_ratio(text: str) -> list[dict]:
    """深市「营业收入构成」版式：金额 / 占营业收入比重 / 上年金额 / 占比 / 同比增减。

    与沪市版的区别是**没有营业成本列**，所以毛利率留给财报指标库，这里只取结构与增速。
    """
    i = text.find("分产品")
    if i < 0:
        return []
    j = text.find("分地区", i)
    block = text[i : j if j > 0 else i + 3000].replace("\n", "")
    out: list[dict] = []
    for m in RATIO_ROW.finditer(block):
        name = re.sub(r"\s+", "", m.group("name")).removeprefix("分产品")
        if len(name) < 2 or name in {"分产品", "分地区"}:
            continue
        out.append(
            {
                "name": name,
                "revenue": _f(m.group("amt")),
                "cost": None,
                "gross_margin": None,
                "revenue_yoy": _f(m.group("yoy")) / 100.0,
                "share": _f(m.group("share")) / 100.0,
            }
        )
    # 去掉 page 页眉残留造成的重复行
    seen, uniq = set(), []
    for row in out:
        if row["name"] in seen:
            continue
        seen.add(row["name"])
        uniq.append(row)
    return uniq



CAP_ROW = re.compile(r"([\u4e00-\u9fa5]{2,12})\s+([\d.]+)\s*(GW|MW)\s+([\d.]+)%")
COST_ITEM = re.compile(
    r"(原材料|直接人工|折旧|能源动力|制造费用|履约成本|运费|其他|外购|辅助材料)\s*"
    r"([\d,]+\.\d{2})\s+([\d.]+)\s+"
    r"([\d,]+\.\d{2})\s+([\d.]+)\s+(-?[\d.]+)"
)
REGION_ROW = re.compile(
    r"([\u4e00-\u9fa5（）()]{2,12})\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+"
    r"(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)"
)


def parse_capacity(text: str) -> list[dict]:
    """光伏产品的自产量与产能利用率 → 反推设计产能。"""
    i = text.find("产能情况")
    if i < 0:
        return []
    block = text[i : i + 2500].replace("\n", " ")
    out = []
    for m in CAP_ROW.finditer(block):
        name, prod, unit, util = m.group(1), _f(m.group(2)), m.group(3), _f(m.group(4))
        if util <= 0 or util > 100 or prod <= 0:
            continue
        gw = prod if unit == "GW" else prod / 1000.0
        out.append(
            {
                "name": name,
                "output_gw": gw,
                "utilization": util / 100.0,
                "capacity_gw": gw / (util / 100.0) if util else None,
            }
        )
    return out


def parse_cost_structure(text: str) -> list[dict]:
    """成本构成：原材料/人工/折旧/能源/制造费用 的金额、占比与同比。"""
    i = text.find("分产品情况")
    if i < 0:
        i = text.find("成本分析表")
    if i < 0:
        return []
    block = text[i : i + 3000].replace("\n", " ")
    out, seen = [], set()
    for m in COST_ITEM.finditer(block):
        name = m.group(1)
        if name in seen:
            continue
        seen.add(name)
        out.append(
            {
                "item": name,
                "amount": _f(m.group(2)),
                "share": _f(m.group(3)) / 100.0,
                "prev_amount": _f(m.group(4)),
                "prev_share": _f(m.group(5)) / 100.0,
                "yoy": _f(m.group(6)) / 100.0,
            }
        )
    return out


def parse_regional(text: str) -> list[dict]:
    """主营业务分地区：境内 vs 境外的收入与毛利率差。"""
    i = text.find("主营业务分地区情况")
    if i < 0:
        return []
    block = text[i : i + 2000].replace("\n", " ")
    out = []
    for m in REGION_ROW.finditer(block):
        name = m.group(1)
        if name in {"分地区", "主营业务"}:
            continue
        out.append(
            {
                "region": name,
                "revenue": _f(m.group(2)),
                "cost": _f(m.group(3)),
                "gross_margin": _f(m.group(4)),
                "revenue_yoy": _f(m.group(5)) / 100.0,
            }
        )
    return out[:10]


def parse_output(text: str) -> list[dict]:
    start = text.find("产销量情况分析表")
    if start < 0:
        return []
    block = text[start : start + 1500]
    out = []
    for line in block.splitlines():
        m = OUTPUT_ROW.match(line.strip())
        if not m or m.group("unit") not in {"MW", "GW", "万", "吨", "台", "套", "万平方米", "%"}:
            continue
        out.append(
            {
                "name": m.group("name").strip(),
                "unit": m.group("unit"),
                "production": _f(m.group("prod")),
                "sales": _f(m.group("sales")),
                "inventory": _f(m.group("inv")),
            }
        )
    return out[:8]


def parse_rnd(text: str) -> dict:
    out: dict = {}
    m = re.search(r"研发投入合计\s*(" + NUM + r")", text)
    if m:
        out["total"] = _f(m.group(1)) * 1e4  # 单位万元
    m = re.search(r"研发投入总额占营业收入比例（%）\s*([\d.]+)", text)
    if m:
        out["ratio"] = _f(m.group(1)) / 100.0
    m = re.search(r"研发投入资本化的比重（%）\s*([\d.]+)", text)
    if m:
        out["capitalized_ratio"] = _f(m.group(1)) / 100.0
    return out


def parse_strategy(text: str) -> dict:
    """抓管理层自述业务边界与转型的原话。"""
    out: dict = {}
    keys = ["从卖产品", "业务组合", "解决方案", "第二增长", "业务边界"]
    for k in keys:
        i = text.find(k)
        if i > 0:
            seg = text[max(0, i - 120) : i + 320].replace("\n", "")
            out.setdefault("quotes", []).append(seg)
    # 经营范围 / 主营业务描述
    m = re.search(r"主要业务[、，]?主要产品.{0,600}", text)
    if m:
        out["main_business_text"] = m.group(0).replace("\n", "")[:600]
    return out


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def build(symbol: str, year: int, facts_dir: Path, keep_pdf: bool = False) -> dict:
    hit = find_annual_report(symbol, year)
    if not hit:
        raise SystemExit(f"未找到 {symbol} {year} 年年报公告")
    art, title = hit
    with tempfile.TemporaryDirectory() as td:
        pdf = download_pdf(art, Path(td) / "a.pdf")
        text = pdf_to_text(pdf)
        if keep_pdf:
            (facts_dir / f"{symbol}.pdf").write_bytes(pdf.read_bytes())
        product_mix = parse_product_mix(text) or parse_product_mix_ratio(text)
        if not product_mix:
            product_mix = parse_product_mix_tables(pdf)

    facts = {
        "symbol": symbol,
        "report_year": year,
        "source": f"东方财富公告 {art}",
        "title": title,
        "pages_chars": len(text),
        "product_mix": product_mix,
        "output": parse_output(text),
        "capacity": parse_capacity(text),
        "cost_structure": parse_cost_structure(text),
        "regional": parse_regional(text),
        "rnd": parse_rnd(text),
        "strategy": parse_strategy(text),
    }
    facts_dir.mkdir(parents=True, exist_ok=True)
    (facts_dir / f"{symbol}.json").write_text(
        json.dumps(facts, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return facts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="抓取年报并抽取分产品/产销量/研发/战略原话")
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--year", type=int, default=2025)
    ap.add_argument("--facts-dir", default=str(Path(__file__).parent / "facts"))
    ap.add_argument("--keep-pdf", action="store_true")
    args = ap.parse_args(argv)

    facts = build(args.symbol.strip().upper(), args.year, Path(args.facts_dir), args.keep_pdf)
    print(json.dumps(facts, ensure_ascii=False, indent=2)[:3000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
