"""多模型 PDF 报告：CLI、HTML 组装与 Chromium 渲染。

用法::

    python -m backend.scripts.backtest2026.multi_report \\
        --artifacts /data/backtest_2026/multi_20261019_rerun \\
        --out /data/backtest_2026/multi_20261019_rerun/report_2026_multi.pdf
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from .multi_report_back import (
    appendix_section,
    conclusion_section,
    portfolio_section,
    risk_section,
    stats_section,
)
from .multi_report_charts import render_all
from .multi_report_data import MultiBundle, load_bundle
from .multi_report_front import cover, method_section, quality_section, summary_section
from .report_pdf_html import html_document

LOG = logging.getLogger("backtest2026.multi_report")

EXTRA_CSS = """
@page { size: A4; }
.cover { break-after: page; }
.section { break-inside: auto; }
.data-table { break-inside: auto; }
.data-table.keep { break-inside: avoid; }
.data-table thead { display: table-header-group; }
.data-table tr { break-inside: avoid; }
h3, h4 { break-after: avoid; }
.footer-note { break-inside: avoid; }
"""

FOOTER_TEMPLATE = """
<div style="width:100%;font-family:'Noto Sans CJK SC',sans-serif;font-size:7pt;
            color:#6E6A63;padding:0 14mm;display:flex;justify-content:space-between;">
  <span>QuantMind Research · 2026 多模型选股回测</span>
  <span>第 <span class="pageNumber"></span> / <span class="totalPages"></span> 页</span>
</div>
"""


def build_html(bundle: MultiBundle, charts: dict[str, Path]) -> str:
    body = "".join(
        [
            cover(bundle),
            summary_section(bundle, charts),
            method_section(bundle, charts),
            quality_section(bundle, charts),
            portfolio_section(bundle, charts),
            stats_section(bundle, charts),
            risk_section(bundle),
            conclusion_section(bundle),
            appendix_section(bundle),
        ]
    )
    return html_document("2026 年 A 股多模型选股回测", body, extra_css=EXTRA_CSS)


def render_pdf(html_path: Path, pdf_path: Path) -> Path:
    from playwright.sync_api import sync_playwright

    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.goto(html_path.resolve().as_uri(), wait_until="load")
        page.pdf(
            path=str(pdf_path),
            format="A4",
            print_background=True,
            margin={
                "top": "14mm",
                "bottom": "16mm",
                "left": "14mm",
                "right": "14mm",
            },
            display_header_footer=True,
            header_template="<span></span>",
            footer_template=FOOTER_TEMPLATE,
        )
        browser.close()
    return pdf_path


def page_count(pdf_path: Path) -> int | None:
    try:
        from pypdf import PdfReader
    except ImportError:
        return None
    return len(PdfReader(str(pdf_path)).pages)


def build_report(
    artifacts: Path,
    out_pdf: Path,
    *,
    charts_dir: Path | None = None,
    html_path: Path | None = None,
    render: bool = True,
) -> dict[str, object]:
    bundle = load_bundle(artifacts)
    if not bundle.horizons:
        raise RuntimeError(f"没有可用的质量榜：{artifacts}")
    charts_dir = charts_dir or artifacts / "report_charts"
    charts = render_all(bundle, charts_dir)
    html = build_html(bundle, charts)
    html_path = html_path or out_pdf.with_suffix(".html")
    html_path.write_text(html, encoding="utf-8")
    result: dict[str, object] = {
        "artifacts": str(artifacts),
        "html": str(html_path),
        "charts": {name: str(path) for name, path in charts.items()},
        "models": bundle.all_models(),
        "horizons": list(bundle.horizons),
    }
    if render:
        render_pdf(html_path, out_pdf)
        result["pdf"] = str(out_pdf)
        result["pages"] = page_count(out_pdf)
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--charts-dir", type=Path, default=None)
    parser.add_argument("--html", type=Path, default=None)
    parser.add_argument("--no-pdf", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    args = parse_args(argv)
    result = build_report(
        args.artifacts,
        args.out,
        charts_dir=args.charts_dir,
        html_path=args.html,
        render=not args.no_pdf,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
