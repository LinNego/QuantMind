"""长窗口多模型深度评估报告：HTML 组装与 PDF 渲染。

用法::

    python -m backend.scripts.backtest2026.deep_report \\
        --run-dir /data/backtest_2026/deep_ext_20261019 \\
        --quarterly-dir /data/backtest_2026/deep_q_ext_20261019 \\
        --consensus-dir /data/backtest_2026/deep_consensus_ext_20261019 \\
        --capacity-dir /data/backtest_2026/deep_capacity_ext_20261019 \\
        --attribution-dir /data/backtest_2026/deep_attr_ext_20261019 \\
        --extension-dir /data/backtest_2026/extend_matrix_20261019 \\
        --out /data/backtest_2026/deep_report_20261019.pdf
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from .deep_report_back import (
    appendix_section,
    attribution_section,
    capacity_section,
    comparison_section,
    conclusion_section,
    consensus_section,
    profiles_section,
    quarterly_section,
    screening_section,
)
from .deep_report_charts import render_all
from .deep_report_data import DeepBundle, load_bundle
from .deep_report_front import (
    cover,
    leaderboard_section,
    method_section,
    summary_section,
)
from .report_pdf_html import html_document

LOG = logging.getLogger("backtest2026.deep_report")

EXTRA_CSS = """
@page { size: A4; }
.cover { break-after: page; }
.section { break-inside: auto; }
.data-table { break-inside: auto; }
.data-table.compact th, .data-table.compact td { padding: 0.7mm 2mm; font-size: 7.9pt; }
.data-table thead { display: table-header-group; }
.data-table tr { break-inside: avoid; }
h3, h4 { break-after: avoid; }
.footer-note { break-inside: avoid; }
.model-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 2.6mm 3.2mm;
  margin: 2mm 0 4mm;
}
.model-card {
  border: 1px solid var(--line);
  border-left: 2.4px solid var(--accent);
  padding: 2.6mm 3mm 2mm;
  break-inside: avoid;
  background: #FFFDF8;
}
.model-card h4 { margin: 0 0 1.2mm; font-size: 9.4pt; }
.model-card p { margin: 0 0 1.1mm; font-size: 8.1pt; line-height: 1.5; text-align: left; }
.model-card .small { font-size: 7.2pt; color: var(--muted); }
.callout ol, .callout ul { margin-bottom: 1mm; }
"""

FOOTER_TEMPLATE = """
<div style="width:100%;font-family:'Noto Sans CJK SC',sans-serif;font-size:7pt;
            color:#6E6A63;padding:0 14mm;display:flex;justify-content:space-between;">
  <span>QuantMind Research · 长窗口多模型深度评估</span>
  <span>第 <span class="pageNumber"></span> / <span class="totalPages"></span> 页</span>
</div>
"""


def build_html(bundle: DeepBundle, charts: dict[str, Path]) -> str:
    body = "".join(
        [
            cover(bundle),
            summary_section(bundle, charts),
            method_section(bundle, charts),
            leaderboard_section(bundle, charts),
            profiles_section(bundle, charts),
            quarterly_section(bundle, charts),
            screening_section(bundle, charts),
            consensus_section(bundle, charts),
            attribution_section(bundle, charts),
            capacity_section(bundle, charts),
            comparison_section(bundle),
            conclusion_section(bundle),
            appendix_section(bundle),
        ]
    )
    return html_document("长窗口多模型选股深度评估", body, extra_css=EXTRA_CSS)


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
    run_dir: Path,
    out_pdf: Path,
    *,
    quarterly_dir: Path | None = None,
    consensus_dir: Path | None = None,
    capacity_dir: Path | None = None,
    attribution_dir: Path | None = None,
    extension_dir: Path | None = None,
    charts_dir: Path | None = None,
    html_path: Path | None = None,
    render: bool = True,
) -> dict[str, object]:
    bundle = load_bundle(
        run_dir,
        quarterly_dir=quarterly_dir,
        consensus_dir=consensus_dir,
        capacity_dir=capacity_dir,
        attribution_dir=attribution_dir,
        extension_dir=extension_dir,
    )
    if not any(not frame.empty for frame in bundle.quality.values()):
        raise RuntimeError(f"没有可用的质量榜：{run_dir}")
    charts_dir = charts_dir or out_pdf.parent / "deep_report_charts"
    charts = render_all(bundle, charts_dir)
    html = build_html(bundle, charts)
    html_path = html_path or out_pdf.with_suffix(".html")
    html_path.write_text(html, encoding="utf-8")
    result: dict[str, object] = {
        "run_dir": str(run_dir),
        "html": str(html_path),
        "charts": {name: str(path) for name, path in charts.items()},
        "models": list(bundle.models),
        "horizons": [5, 10],
    }
    if render:
        render_pdf(html_path, out_pdf)
        result["pdf"] = str(out_pdf)
        result["pages"] = page_count(out_pdf)
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--quarterly-dir", type=Path, default=None)
    parser.add_argument("--consensus-dir", type=Path, default=None)
    parser.add_argument("--capacity-dir", type=Path, default=None)
    parser.add_argument("--attribution-dir", type=Path, default=None)
    parser.add_argument("--extension-dir", type=Path, default=None)
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
        args.run_dir,
        args.out,
        quarterly_dir=args.quarterly_dir,
        consensus_dir=args.consensus_dir,
        capacity_dir=args.capacity_dir,
        attribution_dir=args.attribution_dir,
        extension_dir=args.extension_dir,
        charts_dir=args.charts_dir,
        html_path=args.html,
        render=not args.no_pdf,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
