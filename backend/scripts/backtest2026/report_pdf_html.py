"""PDF 报告的 HTML 外壳、样式与排版辅助函数。"""

from __future__ import annotations

import base64
import html
import math
from collections.abc import Iterable, Sequence
from pathlib import Path

CSS = r"""
:root {
  --ink: #1D1D1B;
  --muted: #6E6A63;
  --line: #DCD5C8;
  --cream: #F7F3EA;
  --paper: #FFFFFF;
  --accent: #A62B2F;
  --amber: #C98A2E;
  --slate: #3E4A5A;
  --teal: #2F6F73;
  --pos: #A62B2F;
  --neg: #1E6B4F;
  --sans: 'Noto Sans CJK SC', 'WenQuanYi Micro Hei', sans-serif;
  --serif: 'Noto Serif CJK SC', 'Noto Sans CJK SC', serif;
  --mono: 'DejaVu Sans Mono', 'Noto Sans Mono', monospace;
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  font-family: var(--sans);
  font-size: 9.4pt;
  line-height: 1.62;
  color: var(--ink);
  background: var(--paper);
  -webkit-font-smoothing: antialiased;
}
h1, h2, h3, h4 {
  font-family: var(--serif);
  font-weight: 600;
  color: var(--ink);
}
h1 { font-size: 30pt; line-height: 1.12; margin: 0; letter-spacing: -0.01em; }
h2 { font-size: 17pt; line-height: 1.2; margin: 0; }
h3 { font-size: 11.5pt; margin: 6mm 0 2.5mm; padding-left: 2.6mm; border-left: 2.2px solid var(--accent); }
h4 { font-size: 9.8pt; margin: 4mm 0 1.6mm; color: var(--slate); }
p { margin: 0 0 2.6mm; text-align: justify; }
ul, ol { margin: 0 0 3mm 5mm; padding: 0; }
li { margin-bottom: 1.2mm; }
strong { font-weight: 600; }
em { font-style: normal; color: var(--accent); }
a { color: var(--accent); text-decoration: none; }
.page-break { break-before: page; }
.section { margin: 0 0 9mm; }
.lead { font-size: 10.4pt; line-height: 1.72; }
.small { font-size: 7.8pt; color: var(--muted); }
.mono { font-family: var(--mono); font-size: 7.8pt; }
.center { text-align: center; }
.nowrap { white-space: nowrap; }

/* ── 封面 ─────────────────────────────────────────────── */
.cover { min-height: 257mm; display: flex; flex-direction: column; }
.cover-band { height: 8mm; background: var(--accent); margin-bottom: 14mm; }
.kicker {
  font-size: 7.4pt;
  letter-spacing: 0.20em;
  color: var(--accent);
  text-transform: uppercase;
  font-weight: 600;
}
.cover h1 { margin: 7mm 0 4mm; max-width: 160mm; }
.cover .subtitle { font-family: var(--serif); font-size: 13.2pt; color: var(--muted); line-height: 1.5; }
.cover .scope { margin-top: 5mm; font-size: 8.2pt; color: var(--muted); letter-spacing: 0.02em; }
.cover .summary {
  margin: 10mm 0 0;
  padding: 4mm 5mm;
  background: var(--cream);
  border-left: 3px solid var(--accent);
}
.cover .summary-title {
  font-family: var(--serif);
  font-size: 9.6pt;
  font-weight: 600;
  color: var(--accent);
  margin-bottom: 2mm;
}
.cover .summary ul { margin: 0 0 0 4.5mm; }
.cover .summary li { margin-bottom: 1.3mm; font-size: 8.4pt; line-height: 1.55; }
.metric-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  border-top: 1px solid var(--line);
  border-bottom: 1px solid var(--line);
  margin: 10mm 0 0;
}
.metric {
  padding: 4mm 3.5mm 3.5mm;
  border-right: 1px solid var(--line);
}
.metric:last-child { border-right: 0; }
.metric .label { font-size: 7.0pt; color: var(--muted); letter-spacing: 0.04em; }
.metric .value { font-family: var(--serif); font-size: 19pt; line-height: 1.1; margin: 1.8mm 0 1mm; }
.metric .sub { font-size: 6.8pt; color: var(--muted); }
.cover-foot {
  margin-top: auto;
  padding-top: 4mm;
  border-top: 1px solid var(--line);
  display: flex;
  justify-content: space-between;
  gap: 6mm;
  font-size: 6.8pt;
  color: var(--muted);
}
.cover-foot div { max-width: 48%; }

/* ── 章节 ─────────────────────────────────────────────── */
.section-head {
  display: flex;
  gap: 5mm;
  align-items: flex-start;
  border-bottom: 1.2px solid var(--ink);
  padding-bottom: 3mm;
  margin-bottom: 5mm;
}
.section-no {
  font-family: var(--serif);
  font-size: 23pt;
  line-height: 1;
  color: var(--accent);
  min-width: 15mm;
}
.section-sub { margin: 1.4mm 0 0; color: var(--muted); font-size: 8.4pt; }
.section-deck { font-size: 9.2pt; color: var(--muted); margin: -1mm 0 4mm; }

/* ── 表格 ─────────────────────────────────────────────── */
.data-table {
  width: 100%;
  border-collapse: collapse;
  margin: 2mm 0 3mm;
  font-size: 8.0pt;
  line-height: 1.42;
  break-inside: avoid;
}
.data-table th {
  background: var(--cream);
  font-weight: 600;
  text-align: left;
  padding: 2mm 2mm;
  border-bottom: 1px solid var(--line);
  color: var(--ink);
}
.data-table td {
  padding: 1.65mm 2mm;
  border-bottom: 0.6px solid var(--line);
  vertical-align: top;
}
.data-table tr:nth-child(even) td { background: #FCFAF6; }
.data-table .num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.data-table .center { text-align: center; }
.data-table .pos { color: var(--pos); }
.data-table .neg { color: var(--neg); }
.data-table .muted { color: var(--muted); }
.data-table .total td { border-top: 1px solid var(--line); font-weight: 600; background: #F7F3EA; }
.table-note { font-size: 7.1pt; color: var(--muted); margin: -1.2mm 0 3.5mm; line-height: 1.5; }
.heat-table td { text-align: center; font-variant-numeric: tabular-nums; }
.heat-table th { text-align: center; }

/* ── 提示框 ───────────────────────────────────────────── */
.callout {
  background: var(--cream);
  border-left: 3px solid var(--accent);
  padding: 3.5mm 4mm;
  margin: 3mm 0 4mm;
  break-inside: avoid;
}
.callout-title {
  font-family: var(--serif);
  font-weight: 600;
  font-size: 9.4pt;
  color: var(--accent);
  margin-bottom: 1.5mm;
}
.callout p:last-child, .callout ul:last-child { margin-bottom: 0; }
.callout.neutral { border-left-color: var(--slate); background: #F2F4F5; }
.callout.neutral .callout-title { color: var(--slate); }
.callout.warn { border-left-color: var(--amber); background: #FBF5E9; }
.callout.warn .callout-title { color: #8A5A12; }

/* ── 布局 ─────────────────────────────────────────────── */
.two-col { display: grid; grid-template-columns: 1.12fr 0.88fr; gap: 7mm; }
.two-col-w { display: grid; grid-template-columns: 1.3fr 0.7fr; gap: 7mm; }
.three-col { display: grid; grid-template-columns: repeat(3, 1fr); gap: 5mm; }
.kpi-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 4mm; margin: 3mm 0 5mm; }
.kpi { border-top: 2px solid var(--accent); padding-top: 2.5mm; }
.kpi .value { font-family: var(--serif); font-size: 16pt; line-height: 1.1; }
.kpi .label { font-size: 7.1pt; color: var(--muted); margin-top: 1mm; }
.kpi .sub { font-size: 6.8pt; color: var(--muted); margin-top: 0.8mm; }
.checklist { list-style: none; margin-left: 0; }
.checklist li { padding-left: 5mm; position: relative; }
.checklist li::before { content: "—"; position: absolute; left: 0; color: var(--accent); }

/* ── 图 ───────────────────────────────────────────────── */
figure { margin: 3mm 0 4mm; break-inside: avoid; }
figure img { width: 100%; display: block; }
figcaption { font-size: 7.3pt; color: var(--muted); margin-top: 1.5mm; line-height: 1.5; }
.fig-no { color: var(--accent); font-weight: 600; margin-right: 1.5mm; }

/* ── 流程图 ───────────────────────────────────────────── */
.timeline {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  border-top: 1px solid var(--line);
  border-bottom: 1px solid var(--line);
  margin: 3mm 0 4mm;
  break-inside: avoid;
}
.timeline .step { padding: 3.2mm 4mm; border-right: 1px solid var(--line); }
.timeline .step:last-child { border-right: 0; }
.timeline .step-no { font-size: 6.8pt; color: var(--accent); letter-spacing: 0.10em; font-weight: 600; }
.timeline .step-title { font-family: var(--serif); font-size: 10pt; margin: 1mm 0; }
.timeline .step-desc { font-size: 7.5pt; color: var(--muted); line-height: 1.5; }

/* ── 代码与附录 ───────────────────────────────────────── */
code { font-family: var(--mono); font-size: 7.6pt; background: #F2EEE5; padding: 0.4mm 1mm; }
pre {
  background: #F2EEE5;
  padding: 3mm 3.5mm;
  font-family: var(--mono);
  font-size: 7.2pt;
  line-height: 1.55;
  white-space: pre-wrap;
  break-inside: avoid;
  margin: 2mm 0 3mm;
}
.footer-note {
  margin-top: 6mm;
  padding-top: 3mm;
  border-top: 1px solid var(--line);
  font-size: 7.1pt;
  color: var(--muted);
  line-height: 1.55;
}
"""


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def is_missing(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    return False


def pct(value: float | None, digits: int = 2, signed: bool = False) -> str:
    if is_missing(value):
        return "—"
    number = float(value) * 100.0
    if signed:
        return f"{number:+.{digits}f}%"
    return f"{number:.{digits}f}%"


def num(value: float | None, digits: int = 2) -> str:
    if is_missing(value):
        return "—"
    return f"{float(value):,.{digits}f}"


def money(value: float | None, digits: int = 0) -> str:
    if is_missing(value):
        return "—"
    return f"{float(value):,.{digits}f}"


def wan(value: float | None, digits: int = 1) -> str:
    if is_missing(value):
        return "—"
    return f"{float(value) / 10_000.0:,.{digits}f} 万"


def date_short(value: object) -> str:
    if value is None:
        return "—"
    if hasattr(value, "strftime"):
        return str(value.strftime("%Y-%m-%d"))
    return str(value)


def date_cn(value: object) -> str:
    if hasattr(value, "strftime"):
        return f"{value.year} 年 {value.month} 月 {value.day} 日"
    return str(value)


def tone(value: float | None) -> str:
    if is_missing(value) or value == 0:
        return ""
    return "pos" if float(value) > 0 else "neg"


def span(text: str, cls: str) -> str:
    return f'<span class="{cls}">{esc(text)}</span>'


def table(
    headers: Sequence[str],
    rows: Iterable[Sequence[str]],
    *,
    aligns: Sequence[str] | None = None,
    raw: bool = False,
    table_class: str = "data-table",
) -> str:
    """生成数据表；默认所有单元格转义，raw=True 时由调用方保证安全。"""
    align_list = list(aligns or ["l"] + ["r"] * (len(headers) - 1))
    head = "".join(
        f'<th class="{"num" if align == "r" else "center" if align == "c" else ""}">'
        f"{esc(label)}</th>"
        for label, align in zip(headers, align_list, strict=True)
    )
    body_rows = []
    for row in rows:
        cells = []
        for cell, align in zip(row, align_list, strict=True):
            value = str(cell) if raw else esc(cell)
            css = "num" if align == "r" else "center" if align == "c" else ""
            cells.append(f'<td class="{css}">{value}</td>')
        body_rows.append("<tr>" + "".join(cells) + "</tr>")
    return (
        f'<table class="{table_class}"><thead><tr>{head}</tr></thead>'
        f"<tbody>{''.join(body_rows)}</tbody></table>"
    )


def figure(path: Path, caption: str, number: int) -> str:
    payload = base64.b64encode(path.read_bytes()).decode("ascii")
    return (
        f'<figure><img src="data:image/png;base64,{payload}" alt="">'
        f'<figcaption><span class="fig-no">图 {number}</span>{caption}</figcaption></figure>'
    )


def callout(title: str, body: str, kind: str = "") -> str:
    cls = f"callout {kind}".strip()
    return (
        f'<div class="{cls}"><div class="callout-title">{title}</div>'
        f"{body}</div>"
    )


def section_head(number: str, title: str, subtitle: str) -> str:
    return (
        f'<div class="section-head"><div class="section-no">{number}</div>'
        f'<div><h2>{title}</h2><p class="section-sub">{subtitle}</p></div></div>'
    )


def html_document(title: str, body: str, *, extra_css: str = "") -> str:
    return (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        f"<title>{esc(title)}</title><style>{CSS}{extra_css}</style></head>"
        f"<body>{body}</body></html>"
    )
