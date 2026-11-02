"""OLA / CVA deck (PPTX).

Two paths, same content:

* no template   a clean PLACEHOLDER design is generated (clearly labelled, because the approved Ingram template
                has not been supplied; SOW D06 / risk R-4).
* a template    the approved Ingram .pptx is filled in place: text containing ``{{token}}`` is replaced, and a
                shape *named* ``CHART_<name>`` or ``TABLE_<name>`` is replaced by a native chart or table at the
                same position.  Tokens left over are reported, never silently dropped.  Tokens and names are
                listed in docs/TEMPLATE.md.

Every slide carries the release status and the figure ids it uses; nothing here changes a number.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any

from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Inches, Pt

from ..pipeline import PipelineResult
from .scenario_view import by_id as scen_by_id
from .scenario_view import rows as scen_rows
from .scenario_view import source_label

INK = RGBColor(0x1F, 0x29, 0x37)
MUTED = RGBColor(0x6B, 0x72, 0x80)
TEAL = RGBColor(0x0F, 0x76, 0x6E)
TEAL_L = RGBColor(0xCC, 0xE7, 0xE4)
AMBER = RGBColor(0xB4, 0x53, 0x09)
AMBER_L = RGBColor(0xFD, 0xE9, 0xC8)
RED = RGBColor(0xB9, 0x1C, 0x1C)
PAPER = RGBColor(0xF6, 0xF7, 0xF9)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
SLATE = RGBColor(0x47, 0x55, 0x69)
FONT = "Arial"
W, H = Inches(13.333), Inches(7.5)


def money(v: Any, short: bool = False) -> str:
    if v is None:
        return "n/a"
    if short and abs(v) >= 1000:
        return f"${v / 1000:,.1f}k" if abs(v) < 1_000_000 else f"${v / 1_000_000:,.2f}M"
    return f"${v:,.0f}"


class Ctx:
    def __init__(self, res: PipelineResult, state: dict[str, Any], gate: dict[str, Any],
                 commentary: dict[str, dict[str, str]], run_id: str, workbook_name: str) -> None:
        self.res, self.state, self.gate, self.com = res, state, gate, commentary
        self.run_id, self.wb = run_id, workbook_name
        self.final = bool(state.get("finalised"))
        self.led = res.ledger

    def fig(self, key: str) -> Any:
        return self.led.value(key)

    def fid(self, *keys: str) -> str:
        ids = [self.led.get(k).id for k in keys if self.led.get(k)]
        return ", ".join(ids)

    @property
    def status(self) -> str:
        return "FINAL" if self.final else f"DRAFT: not reviewed ({self.gate['open']} gate item(s) open)"

    def tokens(self) -> dict[str, str]:
        t = {"status": self.status, "run": self.run_id, "date": self.res.meta["as_of"],
             "customer": self.res.cfg.section("run").get("customer_label", "Customer"),
             "workbook": self.wb, "grade": str(self.fig("estate.data_grade"))}
        for f in self.led.all():
            if isinstance(f.value, (int, float)):
                t[f"F:{f.key}"] = money(f.value) if f.unit == "USD/yr" else \
                    (f"{f.value:,.1f}" if isinstance(f.value, float) else f"{f.value:,}")
            elif f.value is not None:
                t[f"F:{f.key}"] = str(f.value)
            else:
                t[f"F:{f.key}"] = "n/a"
        for k, d in self.com.items():
            t[f"commentary:{k}"] = d["text"]
        return t


# ---------------------------------------------------------------------------------------------- primitives
def _text(slide, x, y, w, h, text, size=14, bold=False, color=INK, align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP,
          name=None, italic=False):
    tb = slide.shapes.add_textbox(x, y, w, h)
    if name:
        tb.name = name
    tf = tb.text_frame
    tf.word_wrap = True
    tf.margin_left = tf.margin_right = Inches(0.05)
    tf.margin_top = tf.margin_bottom = Inches(0.03)
    tf.vertical_anchor = anchor
    lines = text if isinstance(text, list) else [text]
    for i, ln in enumerate(lines):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        r = p.add_run()
        r.text = ln
        r.font.size, r.font.bold, r.font.italic = Pt(size), bold, italic
        r.font.color.rgb, r.font.name = color, FONT
        if i:
            p.space_before = Pt(4)
    return tb


def _box(slide, x, y, w, h, fill=PAPER, line=None, shape=MSO_SHAPE.RECTANGLE):
    s = slide.shapes.add_shape(shape, x, y, w, h)
    s.fill.solid()
    s.fill.fore_color.rgb = fill
    if line is None:
        s.line.fill.background()
    else:
        s.line.color.rgb = line
        s.line.width = Pt(0.75)
    s.shadow.inherit = False
    return s


def _bg(slide, color):
    f = slide.background.fill
    f.solid()
    f.fore_color.rgb = color


def _title(slide, text, sub=None):
    _text(slide, Inches(0.6), Inches(0.38), Inches(12.1), Inches(0.75), text, size=30, bold=True, name="TITLE")
    if sub:
        _text(slide, Inches(0.6), Inches(1.08), Inches(12.1), Inches(0.4), sub, size=14, color=MUTED)


def _footer(slide, c: Ctx, figs: str, n: int):
    color = RED if not c.final else TEAL
    _text(slide, Inches(0.6), Inches(7.0), Inches(8.4), Inches(0.3),
          f"Figures {figs or 'n/a'}  |  trace: {c.wb}  |  {c.run_id}", size=10, color=MUTED)
    _text(slide, Inches(8.7), Inches(7.0), Inches(3.5), Inches(0.3), c.status, size=10, bold=True, color=color,
          align=PP_ALIGN.RIGHT)
    _text(slide, Inches(12.3), Inches(7.0), Inches(0.5), Inches(0.3), str(n), size=10, color=MUTED, align=PP_ALIGN.RIGHT)


def _stat(slide, x, y, w, big, small, fill=PAPER, big_color=INK):
    _box(slide, x, y, w, Inches(1.25), fill)
    _text(slide, x + Inches(0.12), y + Inches(0.1), w - Inches(0.24), Inches(0.62), big, size=26, bold=True, color=big_color)
    _text(slide, x + Inches(0.12), y + Inches(0.74), w - Inches(0.24), Inches(0.45), small, size=11, color=MUTED)


def _table(slide, x, y, w, rows: list[list[str]], col_w: list[float], row_h=0.34, size=11, name=None, header=True):
    n_r, n_c = len(rows), len(rows[0])
    gs = slide.shapes.add_table(n_r, n_c, x, y, w, Inches(row_h * n_r))
    if name:
        gs.name = name
    tbl = gs.table
    tot = sum(col_w)
    for j, cw in enumerate(col_w):
        tbl.columns[j].width = int(w * cw / tot)
    for i, row in enumerate(rows):
        tbl.rows[i].height = Inches(row_h)
        for j, val in enumerate(row):
            cell = tbl.cell(i, j)
            cell.margin_left = cell.margin_right = Inches(0.06)
            cell.margin_top = cell.margin_bottom = Inches(0.02)
            cell.vertical_anchor = MSO_ANCHOR.MIDDLE
            tf = cell.text_frame
            tf.word_wrap = True
            p = tf.paragraphs[0]
            p.text = ""
            r = p.add_run()
            r.text = str(val)
            r.font.size, r.font.name = Pt(size), FONT
            cell.fill.solid()
            if header and i == 0:
                cell.fill.fore_color.rgb = INK
                r.font.bold, r.font.color.rgb = True, WHITE
            else:
                cell.fill.fore_color.rgb = WHITE if i % 2 else PAPER
                r.font.color.rgb = INK
    return gs


def _style_chart(chart, legend=True, size=11):
    chart.font.size = Pt(size)
    chart.font.name = FONT
    chart.font.color.rgb = SLATE
    chart.has_legend = legend
    if legend:
        chart.legend.position = XL_LEGEND_POSITION.BOTTOM
        chart.legend.include_in_layout = False
    try:
        va = chart.value_axis
        va.has_major_gridlines = True
        va.major_gridlines.format.line.color.rgb = RGBColor(0xE5, 0xE7, 0xEB)
        va.format.line.fill.background()
        chart.category_axis.format.line.color.rgb = RGBColor(0xCB, 0xD5, 0xE1)
    except Exception:
        pass


def add_chart(slide, x, y, w, h, kind, cats, series: list[tuple[str, list[float]]], colors, labels=True, fmt='#,##0',
              legend=True, name=None):
    cd = CategoryChartData()
    cd.categories = cats
    for nm, vals in series:
        cd.add_series(nm, vals)
    gf = slide.shapes.add_chart(kind, x, y, w, h, cd)
    if name:
        gf.name = name
    ch = gf.chart
    _style_chart(ch, legend=legend and len(series) > 1)
    for i, s in enumerate(ch.series):
        s.format.fill.solid()
        s.format.fill.fore_color.rgb = colors[i % len(colors)]
        s.invert_if_negative = False
    pl = ch.plots[0]
    pl.gap_width = 60
    if labels:
        pl.has_data_labels = True
        dl = pl.data_labels
        dl.number_format = fmt
        dl.number_format_is_linked = False
        dl.font.size, dl.font.name, dl.font.color.rgb = Pt(11), FONT, INK
    return gf


# ---------------------------------------------------------------------------------------------- slides
def _cost_series(res):
    """Stacked-chart data from the scenario view: AWS Transform's categories, or A01's own components."""
    vrows = [r for r in scen_rows(res) if r.total is not None]
    comps: list[str] = []
    for r in vrows:
        for k in r.components:
            if k not in comps:
                comps.append(k)
    palette = [TEAL, AMBER, RGBColor(0x7C, 0x3A, 0xED), RGBColor(0x94, 0xA3, 0xB8), SLATE, RED]
    return [r.label for r in vrows], [(k, [r.components.get(k, 0.0) for r in vrows]) for k in comps], palette


def build_placeholder(c: Ctx) -> Presentation:
    prs = Presentation()
    prs.slide_width, prs.slide_height = W, H
    blank = prs.slide_layouts[6]
    res = c.res
    L = c.led
    n = 0

    def new():
        nonlocal n
        n += 1
        s = prs.slides.add_slide(blank)
        _bg(s, WHITE)
        return s, n

    # 1 title ------------------------------------------------------------------------
    s, _ = new()
    _bg(s, INK)
    _text(s, Inches(0.8), Inches(1.6), Inches(11.9), Inches(0.9), "Optimization and Licensing Assessment", size=40, bold=True, color=WHITE)
    _text(s, Inches(0.8), Inches(2.75), Inches(11.5), Inches(0.6), "Cloud Value Assessment: cost, rightsizing, licensing and lifecycle findings",
          size=20, color=TEAL_L)
    _text(s, Inches(0.8), Inches(4.3), Inches(8), Inches(0.9),
          [c.tokens()["customer"], f"{res.meta['as_of']}  |  {len(res.estate.workloads)} workloads  |  data grade {c.fig('estate.data_grade')}"],
          size=16, color=WHITE)
    _box(s, Inches(0.8), Inches(5.6), Inches(6.2), Inches(0.5), AMBER_L if not c.final else TEAL_L, shape=MSO_SHAPE.ROUNDED_RECTANGLE)
    _text(s, Inches(0.9), Inches(5.65), Inches(6.0), Inches(0.4), c.status, size=14, bold=True, color=AMBER if not c.final else TEAL)
    _text(s, Inches(0.8), Inches(6.5), Inches(11.8), Inches(0.5),
          "Template: PLACEHOLDER design (the approved Ingram template has not been supplied).  " +
          (f"Figures: AWS Transform assessment \u201c{res.aws.scenario_name}\u201d; A01's own estimate is a cross-check only." if res.aws else
           "Prices: " + ("PLACEHOLDER catalog, not the AWS Price List." if res.pricing.placeholder else "catalog " + res.pricing.catalog_version)),
          size=11, color=RGBColor(0x9C, 0xA3, 0xAF))

    # 2 executive summary ----------------------------------------------------------------
    s, k = new()
    _title(s, "Executive summary", "What the data shows, before review")
    _text(s, Inches(0.6), Inches(1.7), Inches(6.6), Inches(3.2), c.com["executive_summary"]["text"], size=17, name="TEXT_executive_summary")
    _text(s, Inches(0.6), Inches(5.0), Inches(6.6), Inches(0.35), f"Commentary source: {c.com['executive_summary']['source']}.  "
          "Every figure is traced in the supporting workbook.", size=11, color=MUTED, italic=True)
    od, r3 = c.fig("scenario.on_demand.total"), c.fig("scenario.ri_3yr.total")
    _stat(s, Inches(7.7), Inches(1.7), Inches(2.5), f"{c.fig('estate.workloads'):,}", "workloads assessed")
    _stat(s, Inches(10.35), Inches(1.7), Inches(2.4), str(c.fig("estate.data_grade")), res.meta["richness"]["label"])
    _stat(s, Inches(7.7), Inches(3.15), Inches(2.5), money(od, True), "On-Demand, per year", TEAL_L, TEAL)
    _stat(s, Inches(10.35), Inches(3.15), Inches(2.4), money(r3, True), "3-year commitment, per year", TEAL_L, TEAL)
    byol = scen_by_id(res, "byol")
    _stat(s, Inches(7.7), Inches(4.6), Inches(5.05), money(byol.total, True) if byol and byol.total is not None else "not computable",
          "BYOL, per year" + ("" if byol and byol.status == "computed" else f"  ({byol.status.replace('_', ' ')})" if byol else ""),
          AMBER_L if (byol and byol.status != "computed") else PAPER, AMBER if (byol and byol.status != "computed") else INK)
    _footer(s, c, c.fid("estate.workloads", "scenario.on_demand.total", "scenario.ri_3yr.total", "scenario.byol.total"), k)

    # 3 estate overview -------------------------------------------------------------------
    s, k = new()
    _title(s, "Estate overview", f"{c.fig('estate.workloads')} workloads from {', '.join(res.estate.files)}")
    other = c.fig("estate.workloads") - c.fig("estate.windows") - c.fig("estate.linux")
    add_chart(s, Inches(0.6), Inches(1.7), Inches(5.6), Inches(4.8), XL_CHART_TYPE.COLUMN_CLUSTERED,
              ["Windows", "Linux", "Other"], [("Workloads", [c.fig("estate.windows"), c.fig("estate.linux"), other])],
              [TEAL], name="CHART_estate_os")
    env = Counter((w.env or "unknown") for w in res.estate.workloads)
    rows = [["Measure", "Value"],
            ["Current vCPU (reported)", f"{c.fig('estate.vcpu'):,.0f}"], ["Current RAM (GB, reported)", f"{c.fig('estate.ram_gb'):,.0f}"],
            ["Used storage (TB)", f"{c.fig('estate.storage_tb'):,.1f}"], ["SQL Server instances", f"{c.fig('estate.sql_instances')}"],
            ["Environment mix", ", ".join(f"{k} {v}" for k, v in env.most_common(4))],
            ["Data basis", f"{res.estate.data_basis}; collection {res.estate.collection_days or 'span not stated'} day(s)"]]
    _table(s, Inches(6.7), Inches(1.8), Inches(6.0), rows, [2.4, 3.2], row_h=0.5, size=12, name="TABLE_estate")
    _text(s, Inches(6.7), Inches(5.55), Inches(6.0), Inches(0.9), "; ".join(res.meta["richness"]["reasons"]), size=11, color=MUTED, italic=True)
    _footer(s, c, c.fid("estate.workloads", "estate.windows", "estate.linux", "estate.data_grade"), k)

    # 4 cost comparison --------------------------------------------------------------------
    s, k = new()
    _title(s, "Cost comparison across the pricing scenarios", f"Annual cost per scenario. Source: {source_label(res)}")
    cats, series, colors = _cost_series(res)
    add_chart(s, Inches(0.6), Inches(1.65), Inches(7.2), Inches(5.1), XL_CHART_TYPE.COLUMN_STACKED, cats, series,
              colors, labels=False, fmt='$#,##0', name="CHART_cost")
    base = c.fig("scenario.on_demand.total")
    rows = [["Scenario", "Per year", "vs On-Demand", "Source"]]
    for x in scen_rows(res):
        rows.append([x.label, money(x.total) if x.total is not None else x.status.replace("_", " "),
                     "n/a" if (x.total is None or not base) else f"{100 * (x.total / base - 1):+.1f}%",
                     "AWS Transform" if x.source == "aws_transform" else "A01 estimate"])
    _table(s, Inches(8.1), Inches(1.8), Inches(4.7), rows, [1.4, 1.3, 1.2, 1.3], row_h=0.5, size=11, name="TABLE_scenarios")
    _text(s, Inches(8.1), Inches(4.5), Inches(4.7), Inches(2.3), [c.com["cost_comparison"]["text"],
                                                                  f"[{c.com['cost_comparison']['source']}]"], size=12, color=SLATE)
    _footer(s, c, c.fid("scenario.on_demand.total", "scenario.ri_1yr.total", "scenario.ri_3yr.total", "scenario.byol.total"), k)

    # 5 rightsizing -----------------------------------------------------------------------------
    s, k = new()
    _title(s, "Rightsizing", "Recommended capacity against what is allocated today" + {
        "aws_transform": "  |  AWS Transform per-server sizing",
        "a01_estimate": "  |  A01 estimate" + (" (AWS Transform's per-server sizing was not imported)" if res.aws else "")}.get(res.sizing_source or "", ""))
    add_chart(s, Inches(0.6), Inches(1.65), Inches(5.4), Inches(3.3), XL_CHART_TYPE.COLUMN_CLUSTERED,
              ["Over-provisioned", "Close to right", "Need more"],
              [("Workloads", [c.fig("sizing.downsize"), c.fig("sizing.same"), c.fig("sizing.upsize")])],
              [TEAL], legend=False, name="CHART_rightsize")
    _stat(s, Inches(0.6), Inches(5.2), Inches(2.6), f"{c.fig('sizing.vcpu_rec'):,.0f} vCPU", f"recommended; {c.fig('sizing.vcpu_cur'):,.0f} today")
    _stat(s, Inches(3.4), Inches(5.2), Inches(2.6), f"{c.fig('sizing.mem_rec'):,.0f} GB", f"recommended; {c.fig('sizing.mem_cur'):,.0f} GB today")
    top = sorted([z for z in res.rightsizing.values() if z.ok and z.direction == "downsize"],
                 key=lambda z: -((z.cur_vcpu or 0) - (z.rec_vcpu or 0)))[:7]
    rows = [["Workload", "Now", "Recommended", "Conf."]]
    for z in top:
        rows.append([z.workload, f"{z.cur_vcpu:.0f} vCPU / {z.cur_ram_gb:.0f} GB", f"{z.instance}", z.confidence])
    _table(s, Inches(6.4), Inches(1.75), Inches(6.4), rows, [1.4, 1.6, 1.6, 0.7], row_h=0.4, size=11, name="TABLE_rightsize")
    _text(s, Inches(6.4), Inches(5.0), Inches(6.4), Inches(1.8), [c.com["rightsizing"]["text"], f"[{c.com['rightsizing']['source']}]"], size=12, color=SLATE)
    _footer(s, c, c.fid("sizing.vcpu_rec", "sizing.vcpu_cur", "sizing.downsize", "sizing.upsize"), k)

    # 6 licensing --------------------------------------------------------------------------------
    s, k = new()
    _title(s, "Licensing", f"Input: {res.licensing.mode}; {res.licensing.provenance}")
    add_chart(s, Inches(0.6), Inches(1.65), Inches(5.0), Inches(3.6), XL_CHART_TYPE.BAR_CLUSTERED, ["Windows Server", "SQL Server"],
              [("Items with a licensing finding (%)", [c.fig("licensing.coverage_windows"), c.fig("licensing.coverage_sql")])],
              [TEAL], fmt='0.0"%"', legend=False, name="CHART_licensing")
    s.shapes[-1].chart.value_axis.maximum_scale = 100
    s.shapes[-1].chart.value_axis.minimum_scale = 0
    _text(s, Inches(0.6), Inches(5.3), Inches(5.0), Inches(0.5), "Licensing input coverage, %", size=11, color=MUTED, italic=True)
    byol = res.pricing.scenario("byol")
    rows = [["Item", "Count"], ["License-bearing items", str(c.fig("licensing.bearing"))],
            ["Items without a finding", str(c.fig("licensing.gaps"))], ["Reviewer-accepted assumptions", str(len(c.state["assumptions"]))],
            ["BYOL status", (byol.status.replace("_", " ") if byol else "n/a")]]
    _table(s, Inches(6.0), Inches(1.75), Inches(6.8), rows, [3.4, 2.4], row_h=0.42, size=12, name="TABLE_licensing")
    asks = [p for p in c.state["proposals"] if p["type"] == "licensing_request" and p["status"] == "accepted"]
    qs = [f"Open: {p['payload']['question'][:230]}" for p in asks[:3]] or [c.com["licensing"]["text"]]
    _text(s, Inches(6.0), Inches(4.1), Inches(6.8), Inches(2.6), qs + ([f"[{c.com['licensing']['source']}]"] if not asks else []), size=12, color=SLATE)
    _footer(s, c, c.fid("licensing.coverage_sql", "licensing.coverage_windows", "licensing.gaps", "scenario.byol.total"), k)

    # 7 lifecycle --------------------------------------------------------------------------------
    s, k = new()
    _title(s, "Risk and lifecycle", "End of support for operating systems and SQL Server")
    add_chart(s, Inches(0.6), Inches(1.65), Inches(5.0), Inches(4.0), XL_CHART_TYPE.COLUMN_CLUSTERED, ["Past support", "Approaching"],
              [("Items", [c.fig("lifecycle.eos"), c.fig("lifecycle.approaching")])], [AMBER], legend=False, name="CHART_lifecycle")
    rows = [["Workload", "Item", "End of support", "Status"]]
    worst = sorted([f for f in res.lifecycle if f.status != "supported"], key=lambda f: f.months)[:8]
    for f in worst:
        rows.append([f.workload, f.label, f.end_of_support, f.status.replace("_", " ")])
    if len(rows) == 1:
        rows.append(["none", "", "", ""])
    _table(s, Inches(6.0), Inches(1.75), Inches(6.8), rows, [1.4, 2.2, 1.3, 1.4], row_h=0.4, size=11, name="TABLE_lifecycle")
    _text(s, Inches(6.0), Inches(5.6), Inches(6.8), Inches(1.2),
          [c.com["lifecycle"]["text"]] + (["Dates come from an unverified rules table."] if res.meta["lifecycle_rules_unverified"] and "nverified" not in c.com["lifecycle"]["text"] else []),
          size=12, color=RED if res.meta["lifecycle_rules_unverified"] else SLATE)
    _footer(s, c, c.fid("lifecycle.eos", "lifecycle.approaching", "lifecycle.at_risk"), k)

    # 8 data quality -----------------------------------------------------------------------------
    s, k = new()
    _title(s, "Data-quality exceptions", "For analyst review before release")
    codes = Counter(i.code for i in res.issues)
    top = codes.most_common(8)
    add_chart(s, Inches(0.6), Inches(1.65), Inches(7.0), Inches(4.9), XL_CHART_TYPE.BAR_CLUSTERED, [t[0] for t in reversed(top)] or ["none"],
              [("Exceptions", [t[1] for t in reversed(top)] or [0])], [SLATE], legend=False, name="CHART_exceptions")
    _stat(s, Inches(8.0), Inches(1.75), Inches(1.5), str(c.fig("quality.error")), "errors", AMBER_L if c.fig("quality.error") else PAPER, RED if c.fig("quality.error") else INK)
    _stat(s, Inches(9.65), Inches(1.75), Inches(1.5), str(c.fig("quality.warn")), "warnings")
    _stat(s, Inches(11.3), Inches(1.75), Inches(1.5), str(c.fig("quality.info")), "informational")
    _text(s, Inches(8.0), Inches(3.3), Inches(4.8), Inches(3.4), [c.com["data_quality"]["text"], f"[{c.com['data_quality']['source']}]",
          "The full list, with source rows, is on the Exceptions sheet of the workbook."], size=12, color=SLATE)
    _footer(s, c, c.fid("quality.total", "quality.error", "quality.warn"), k)

    # 9 method and assumptions ---------------------------------------------------------------------
    s, k = new()
    _title(s, "Method and assumptions", "How the numbers were made, and what is not yet settled")
    lines = [f"Sizing: CPU target {res.cfg.section('sizing')['cpu_target_util']}% and memory target {res.cfg.section('sizing')['mem_target_util']}% on the cheapest fitting instance; basis stated per workload.",
             "Scenarios: " + "; ".join(f"{x['label']} = {x['purchase'].replace('_', ' ')} + {x['license'].replace('_', ' ')}" for x in res.cfg.scenarios) + ".",
             (f"Figures: AWS Transform assessment '{res.aws.scenario_name}' (job {res.aws.job.get('jobId', 'n/a')}); A01's own estimate is a cross-check only and BYOL where shown."
              if res.aws else f"Prices: catalog {res.pricing.catalog_version}" + (" (PLACEHOLDER, replace with an AWS Price List snapshot)." if res.pricing.placeholder else ".")),
             "Lifecycle: " + ("dates unverified; check vendor lifecycle pages before customer use." if res.meta["lifecycle_rules_unverified"] else "dates verified."),
             f"Parity: {res.parity_summary.get('compared', 0)} workloads compared with a reference; {res.parity_summary.get('outliers', 0)} outside {res.parity_summary['tolerance_pct']}%; {len(c.state['sizing_overrides'])} reviewer override(s).",
             "AI assistance: the agent only proposes. Commentary is labelled by source; a person accepted or rejected each proposal; nothing is released without the review gate."]
    _text(s, Inches(0.6), Inches(1.65), Inches(12.0), Inches(3.9), lines, size=14, color=INK)
    _stat(s, Inches(0.6), Inches(5.55), Inches(3.8), ("AWS Transform" if res.aws else "placeholder" if res.pricing.placeholder else "catalogued"), ("figures source" if res.aws else "price catalog"), (PAPER if res.aws else AMBER_L if res.pricing.placeholder else PAPER), (INK if res.aws else AMBER if res.pricing.placeholder else INK))
    _stat(s, Inches(4.6), Inches(5.55), Inches(3.8), "unverified" if res.meta["lifecycle_rules_unverified"] else "verified", "lifecycle dates", AMBER_L if res.meta["lifecycle_rules_unverified"] else PAPER, AMBER if res.meta["lifecycle_rules_unverified"] else INK)
    _stat(s, Inches(8.6), Inches(5.55), Inches(4.1), str(len(c.state["sizing_overrides"]) + len(c.state["assumptions"])), "reviewer overrides and assumptions")
    _footer(s, c, "", k)

    # 10 lineage appendix --------------------------------------------------------------------------
    s, k = new()
    _title(s, "Appendix: figure lineage", f"Every figure is in the Ledger sheet of {c.wb} with its rule and inputs")
    keys = ["estate.workloads", "scenario.on_demand.total", "scenario.ri_1yr.total", "scenario.ri_3yr.total", "scenario.byol.total",
            "sizing.vcpu_rec", "sizing.downsize", "licensing.coverage_sql", "lifecycle.eos", "quality.total", "parity.outliers"]
    rows = [["ID", "Figure", "Value", "Rule@version"]]
    for key in keys:
        f = L.get(key)
        if f:
            val = money(f.value) if f.unit == "USD/yr" else ("n/a" if f.value is None else str(f.value))
            rows.append([f.id, f.label, val, f"{f.rule}@{f.rule_version}"])
    _table(s, Inches(0.6), Inches(1.7), Inches(12.1), rows, [0.9, 5.0, 1.8, 1.8], row_h=0.38, size=11, name="TABLE_lineage")
    _footer(s, c, "", k)
    return prs


# ---------------------------------------------------------------------------------------------- real template
_TOKEN = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")


def fill_template(path: str, c: Ctx) -> tuple[Presentation, list[str]]:
    """Fill the approved Ingram template.  Returns the deck and the list of problems (unknown tokens, unused names)."""
    prs = Presentation(path)
    toks = c.tokens()
    problems: list[str] = []

    def replace_in_frame(tf) -> None:
        for p in tf.paragraphs:
            full = "".join(r.text for r in p.runs)
            if "{{" not in full:
                continue
            def sub(m):
                k = m.group(1)
                if k in toks:
                    return toks[k]
                problems.append(f"unknown token {{{{{k}}}}}")
                return m.group(0)
            new = _TOKEN.sub(sub, full)
            if p.runs:
                p.runs[0].text = new           # keep the first run's formatting
                for r in p.runs[1:]:
                    r.text = ""

    for slide in prs.slides:
        for shp in list(slide.shapes):
            nm = shp.name or ""
            if nm.startswith(("CHART_", "TABLE_")):
                if nm not in NAMED_VISUALS:
                    problems.append(f"no builder for shape name '{nm}'")
                    continue
                x, y, w, h = shp.left, shp.top, shp.width, shp.height
                shp._element.getparent().remove(shp._element)      # replaced by a native chart/table in place
                build_named_visual(slide, c, nm, x, y, w, h)
                continue
            if shp.has_text_frame:
                replace_in_frame(shp.text_frame)
            if getattr(shp, "has_table", False) and shp.has_table:
                for row in shp.table.rows:
                    for cell in row.cells:
                        replace_in_frame(cell.text_frame)
    return prs, problems


NAMED_VISUALS = {"CHART_cost", "CHART_estate_os", "TABLE_scenarios", "TABLE_lifecycle"}


def build_named_visual(slide, c: Ctx, name: str, x, y, w, h):
    res = c.res
    if name == "CHART_cost":
        cats, series, colors = _cost_series(res)
        return add_chart(slide, x, y, w, h, XL_CHART_TYPE.COLUMN_STACKED, cats, series, colors, labels=False, name=name)
    if name == "CHART_estate_os":
        other = c.fig("estate.workloads") - c.fig("estate.windows") - c.fig("estate.linux")
        return add_chart(slide, x, y, w, h, XL_CHART_TYPE.COLUMN_CLUSTERED, ["Windows", "Linux", "Other"],
                         [("Workloads", [c.fig("estate.windows"), c.fig("estate.linux"), other])], [TEAL], name=name)
    if name == "TABLE_scenarios":
        base = c.fig("scenario.on_demand.total")
        rows = [["Scenario", "Per year", "vs On-Demand", "Source"]] + [
            [s.label, money(s.total) if s.total is not None else s.status.replace("_", " "),
             "n/a" if (s.total is None or not base) else f"{100 * (s.total / base - 1):+.1f}%",
             "AWS Transform" if s.source == "aws_transform" else "A01 estimate"] for s in scen_rows(res)]
        return _table(slide, x, y, w, rows, [1.4, 1.3, 1.2, 1.3], row_h=0.4, size=11, name=name)
    if name == "TABLE_lifecycle":
        rows = [["Workload", "Item", "End of support", "Status"]] + [
            [f.workload, f.label, f.end_of_support, f.status.replace("_", " ")]
            for f in sorted([f for f in res.lifecycle if f.status != "supported"], key=lambda f: f.months)[:8]] or \
            [["Workload", "Item", "End of support", "Status"], ["none", "", "", ""]]
        return _table(slide, x, y, w, rows, [1.4, 2.2, 1.3, 1.4], row_h=0.4, size=11, name=name)
    return None


def build_deck(res: PipelineResult, state: dict[str, Any], gate: dict[str, Any], commentary: dict[str, dict[str, str]],
               run_id: str, workbook_name: str, out_path: str, template: str | None = None) -> dict[str, Any]:
    c = Ctx(res, state, gate, commentary, run_id, workbook_name)
    problems: list[str] = []
    if template:
        from .ingram_ola import fill_ingram_template, is_ingram_template
        prs = Presentation(template)
        if is_ingram_template(prs):                    # Ingram's own OLA deck: filled in place, all 25 slides kept, missing data left blank
            problems = fill_ingram_template(prs, c)
        else:
            prs, problems = fill_template(template, c)
    else:
        prs = build_placeholder(c)
    prs.core_properties.title = "Optimization and Licensing Assessment"
    prs.core_properties.author = "Aivar Innovations (A01)"
    prs.core_properties.subject = f"{run_id} | {c.status}"
    prs.save(out_path)
    return {"path": out_path, "slides": len(prs.slides), "template": template, "problems": problems}
