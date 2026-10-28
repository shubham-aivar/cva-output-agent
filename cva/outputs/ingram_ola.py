"""Fill Ingram Micro's own OLA deck (``templates/Ingram_OLA_Template.pptx``, 25 slides) in place.

The deck that comes out is the template: the same 25 slides in the same order, the same layouts, charts and tables, nothing added and nothing removed.
Only the customer's figures change.  A section whose data was not supplied (no licensing input, no SQL core data, no assessment yet) stays in the deck
with its numbers left BLANK, never with the sample's numbers and never with a guess.  Every number is read from the pipeline result (the ledger, the
scenario view, the workloads); nothing here computes a price.  Slides that carry no customer data (the Ingram story, FAQ, next steps) are untouched.

Slide map (1-based, as in the template):  2 customer name | 3 agenda scope | 4 executive summary | 5 assessment scope | 6 cost summary chart |
7 expense chart | 8 cost detail table | 9-10 SQL core reduction | 11-12 zombie servers | 13 Windows support | 14-15 customer name and the savings message.
"""
from __future__ import annotations

import re
from typing import Any

from pptx.chart.data import CategoryChartData
from pptx.util import Inches

from .scenario_view import rows as scen_rows

TEMPLATE_FILE = "Ingram_OLA_Template.pptx"
BLANK = ""

# the template's four columns, in the template's order, with what A01 can put in each.  "BYOL SQL" is not claimed for the reserved-instance columns:
# AWS Transform prices them licence-included, and the BYOL scenario exists only as A01's own On-Demand estimate when a licensing input was given.
COLS = (("ri_3yr", "3 Year NURI", "License Inc."), ("ri_1yr", "1 Year NURI", "License Inc."), ("byol", "On Demand", "BYOL"), ("on_demand", "On Demand", "License Inc."))
PHRASE = {"ri_3yr": "3 Year No Upfront Reserved Instance (license included)", "ri_1yr": "1 Year No Upfront Reserved Instance (license included)",
          "byol": "On Demand BYOL", "on_demand": "On Demand"}


def is_ingram_template(prs: Any) -> bool:
    try:
        s1 = " ".join(sh.text_frame.text for sh in prs.slides[0].shapes if sh.has_text_frame)
        return len(prs.slides) == 25 and "Optimization and Licensing Assessment" in s1 and prs.slides[7].shapes[3].has_table
    except Exception:
        return False


# ------------------------------------------------------------------------------------------------ text helpers (formatting of the first run is kept)
def _runs(shape: Any) -> list[Any]:
    return [r for p in shape.text_frame.paragraphs for r in p.runs]


def set_text(shape: Any, text: str) -> None:
    """The whole shape becomes ``text`` (one paragraph), keeping the first run's formatting."""
    tf = shape.text_frame
    first = tf.paragraphs[0]
    if first.runs:
        first.runs[0].text = text
        for r in first.runs[1:]:
            r.text = ""
    else:
        first.add_run().text = text
    for p in list(tf.paragraphs)[1:]:
        p._p.getparent().remove(p._p)


def set_para(shape: Any, idx: int, text: str) -> None:
    p = shape.text_frame.paragraphs[idx]
    if p.runs:
        p.runs[0].text = text
        for r in p.runs[1:]:
            r.text = ""
    else:
        p.add_run().text = text


def set_run(shape: Any, idx: int, text: str) -> None:
    _runs(shape)[idx].text = text


def blank(shape: Any) -> None:
    for r in _runs(shape):
        r.text = ""


def set_cell(cell: Any, text: str) -> None:
    runs = [r for p in cell.text_frame.paragraphs for r in p.runs]
    if runs:
        runs[0].text = text
        for r in runs[1:]:
            r.text = ""
    else:
        cell.text_frame.paragraphs[0].add_run().text = text


def set_cell_lines(cell: Any, line1: str, line2: str) -> None:
    """A two-line column header (the template breaks the line with a break or a new paragraph): first run = line 1, last run = line 2."""
    runs = [r for p in cell.text_frame.paragraphs for r in p.runs]
    if len(runs) >= 2:
        runs[0].text, runs[-1].text = line1, line2
        for r in runs[1:-1]:
            r.text = ""
    else:
        set_cell(cell, f"{line1} {line2}")


def by_id(slide: Any, sid: int) -> Any:
    return next(s for s in slide.shapes if s.shape_id == sid)


def drop(shape: Any) -> None:
    shape._element.getparent().remove(shape._element)


def k(v: float | None) -> str:
    return BLANK if v is None else f"${v / 1000:,.0f}K"


# ------------------------------------------------------------------------------------------------ the numbers
class Row:
    """One server with the figures the zombie slides need, from AWS Transform's per-server sheet or from the inventory."""
    __slots__ = ("name", "cpu", "vcpu", "ram", "cost")

    def __init__(self, name: str, cpu: float, vcpu: float | None, ram: float | None, cost: float | None) -> None:
        self.name, self.cpu, self.vcpu, self.ram, self.cost = name, cpu, vcpu, ram, cost


class Facts:
    """Everything the template needs, computed once.  AWS Transform's own outputs come FIRST (its business case deck for the scope, its analysis workbook for
    the per-server and database figures); the inventory fills in only what AWS did not give.  ``None`` means 'not available': the slide shows blank."""

    def __init__(self, c: Any) -> None:
        res = c.res
        self.c, self.res = c, res
        aws = res.aws
        bc = (aws.business_case if aws else None) or {}
        ps = (aws.per_server if aws else None) or {}
        self.rds = (aws.rds if aws else None) or {}
        self.inst = self.rds.get("instances") or []
        ws = res.estate.workloads
        self.src = "AWS Transform business case + analysis workbook" if (bc and ps) else ("AWS Transform business case" if bc else ("AWS Transform analysis workbook" if ps else "inventory"))
        # --- scope
        inv_n = len(ws)
        self.n = int(bc.get("servers_in_scope") or ((aws.servers or {}).get("in_scope") if aws else 0) or inv_n)
        os_aws = [str(v.get("os_type") or "") for v in ps.values()]
        self.win_inv = sum(1 for w in ws if w.os_family == "windows")
        self.win = bc.get("windows_servers") if "windows_servers" in bc else (sum(1 for o in os_aws if o.lower().startswith("win")) if os_aws else (self.win_inv if inv_n else None))
        self.linux = bc.get("linux_servers") if "linux_servers" in bc else (sum(1 for o in os_aws if o and not o.lower().startswith("win")) if os_aws else (sum(1 for w in ws if w.os_family == "linux") if inv_n else None))
        self.sql = bc["sql_servers"] if "sql_servers" in bc else (len(self.inst) if self.inst else (sum(len(w.sql) for w in ws) if inv_n else None))
        if "block_provisioned_tib" in bc:
            self.unit, self.tb_prov, self.tb_used = "TiB", round(bc["block_provisioned_tib"], 1), (round(bc["block_used_tib"], 1) if "block_used_tib" in bc else None)
        else:
            prov, used = sum(w.storage_prov_gb or 0 for w in ws) / 1024.0, sum(w.storage_used_gb or 0 for w in ws) / 1024.0
            self.unit, self.tb_prov, self.tb_used = "TB", (round(prov, 1) or None), (round(used, 1) or None)
        self.apps = len({str(v.get("application")).strip() for v in ps.values() if v.get("application")}) or None
        # --- utilisation (AWS gives a 0-1 average CPU per server; the inventory gives a percentage)
        from ..transform.aws_sizing import recommended_slug
        slug = recommended_slug(aws) if aws is not None else None
        rows: list[Row] = []
        if ps:
            for v in ps.values():
                cpu = (v.get("util") or {}).get("avg_cpu")
                if cpu is not None:
                    m = (v.get("models") or {}).get(slug) or next(iter((v.get("models") or {}).values()), {})
                    rows.append(Row(v.get("name") or v.get("short"), cpu * 100.0, v.get("vcpu"), v.get("ram_gb"), m.get("total")))
        else:
            rows = [Row(w.name, w.cpu_avg, w.vcpu, w.ram_gb, None) for w in ws if str(w.power_state or "").lower() != "poweredoff" and w.cpu_avg is not None]
        self.util_known = bool(rows)
        self.b5 = sorted((r for r in rows if r.cpu < 5), key=lambda r: (r.cpu, -(r.vcpu or 0)))
        self.b10 = [r for r in rows if 5 <= r.cpu < 10]
        self.b20 = [r for r in rows if 10 <= r.cpu < 20]
        self.low = len(self.b5) + len(self.b10) + len(self.b20)
        self.customer = c.tokens().get("customer", "Customer")
        # --- scenarios
        self.rows = {r.id: r for r in scen_rows(res) if r.total is not None and r.status in ("computed", "partial", "illustrative")}
        if aws is not None and self.n:
            ec2_ok = sum(1 for v in ps.values() if any((m or {}).get("instance") for m in (v.get("models") or {}).values()))
            sql_n = bc.get("sql_servers", len(self.inst))
            self.pct_sized = min(100, round(100 * (ec2_ok + sql_n) / self.n)) if ps else None
        else:
            sz = res.rightsizing if (res.aws_sizing or res.estimate_ran) else {}
            self.pct_sized = round(100 * sum(1 for z in sz.values() if getattr(z, "ok", False)) / inv_n) if (inv_n and sz) else None
        cand = min((i for i in ("ri_3yr", "ri_1yr", "byol") if i in self.rows), key=lambda i: self.rows[i].total, default=None)
        od = self.rows.get("on_demand")
        self.best, self.save_infra, self.save_total = None, None, None
        if cand and od and self.rows[cand].total < od.total and self.infra("on_demand"):       # a "best" that is not cheaper than On-Demand is not claimed
            self.best = cand
            self.save_infra = round(100 * (1 - self.infra(cand) / self.infra("on_demand")))
            self.save_total = round(100 * (1 - self.rows[cand].total / od.total))

    def tb(self, v: float | None) -> str:
        return BLANK if v is None else f"{v} {self.unit}"

    def sql_onprem_cpus(self) -> int | None:
        """The on-premises SQL CPU count AWS states on its RDS overview ("... across 84 on-prem CPUs"); it is not a sum of provisioned vCPUs, so it is quoted, not rebuilt."""
        for v in list((self.rds.get("overview") or {}).values()) + list(self.rds.get("notes") or []):
            m = re.search(r"across (\d+) on-prem CPUs", str(v))
            if m:
                return int(m.group(1))
        return None

    def sql_cores(self) -> dict[str, tuple[float, float]]:
        """SQL vCPUs by edition from AWS's database assessment: (on-premises provisioned, on the recommended target)."""
        out: dict[str, list[float]] = {}
        for i in self.inst:
            ed = str(i.get("edition") or "").strip().title()
            tgt = next((m.get("licensed_vcpus") for k, m in (i.get("models") or {}).items() if k == "three_year_nuri"), None) or next(
                (m.get("licensed_vcpus") for m in (i.get("models") or {}).values() if m.get("licensed_vcpus")), None)
            if ed:
                a = out.setdefault(ed, [0.0, 0.0])
                a[0] += i.get("src_vcpus") or 0.0
                a[1] += tgt or 0.0
        return {k: (v[0], v[1]) for k, v in out.items()}

    def parts(self, sid: str) -> dict[str, float | None]:
        r = self.rows.get(sid)
        if r is None:
            return {}
        cm = r.components
        if r.source == "aws_transform":                      # AWS Transform's own split; its compute is licence-included
            return {"compute": cm.get("Compute"), "storage": cm.get("Storage"), "network": cm.get("Network"), "license": 0.0, "support": cm.get("Support")}
        return {"compute": cm.get("Infrastructure"), "storage": cm.get("Storage"), "network": None,
                "license": (cm.get("Windows licenses") or 0.0) + (cm.get("SQL Server licenses") or 0.0), "support": None}

    def infra(self, sid: str) -> float | None:
        p = self.parts(sid)
        vals = [p.get("compute"), p.get("storage"), p.get("network"), p.get("license") if sid == "byol" else None]
        return None if not p else sum(v or 0.0 for v in vals)


# ------------------------------------------------------------------------------------------------ slides
def _customer(prs: Any, f: Facts) -> None:
    name = f.customer
    s = prs.slides
    for shp in (by_id(s[1], 7), by_id(s[14], 39)):
        for r in _runs(shp):
            r.text = r.text.replace("<Customer>", name)
    set_run(by_id(s[14], 9), 1, name)
    if name and not name.lower().startswith("customer"):
        set_run(by_id(s[13], 2), 1, name)


def _agenda(prs: Any, f: Facts) -> None:
    sh = by_id(prs.slides[2], 2)
    set_para(sh, 2, f"\t- {f.win} Windows VMs " if f.win is not None else BLANK)
    set_para(sh, 3, f"\t- {f.linux}\u00a0Linux VMs" if f.linux is not None else BLANK)
    set_para(sh, 4, f"\t- {f.tb_prov}\u00a0{f.unit} Storage" if f.tb_prov else BLANK)


def _summary(prs: Any, f: Facts) -> None:
    s = prs.slides[3]
    def set_runs(sid: int, text: str) -> None:
        set_text(by_id(s, sid), text)
    set_runs(9, f"{len(f.res.estate.files)} Documents and Artifacts" if f.res.estate.files else BLANK)
    set_runs(12, f"{f.low} VM\u2019s with less than 20% usage" if f.util_known else BLANK)
    set_runs(13, f"{f.win} Windows Virtual Machines" if f.win is not None else BLANK)
    set_runs(17, f"{len(f.rows)} Cloud Operating Models" if f.rows else BLANK)
    t = by_id(s, 23)
    set_para(t, 0, f"{f.tb_prov} {f.unit} Storage" if f.tb_prov else BLANK)
    set_para(t, 1, "Provisioned" if f.tb_prov else BLANK)
    set_runs(26, f"{f.pct_sized}% of servers right-sized" if f.pct_sized is not None else BLANK)
    call = by_id(s, 25)
    if f.best:
        runs = _runs(call)
        runs[0].text = f"{PHRASE[f.best]} with instance resizing, on AWS for the Windows/Linux environments offered the best combination of operational resiliency, while generating "
        runs[1].text = f"~{f.save_infra}% savings"
        runs[4].text = f"${f.infra(f.best) / 1000:,.0f}K annualized spend"
        runs[7].text = "On Demand License included pricing model."
    else:
        blank(call)


def _scope(prs: Any, f: Facts) -> None:
    s = prs.slides[4]
    for sid, val in ((6, None), (7, f.win), (8, f.sql), (10, f.linux), (11, f.apps)):
        set_text(by_id(s, sid), BLANK if val is None else str(val))     # datacenters are in no output: blank, not guessed
    set_text(by_id(s, 4), f.tb(f.tb_prov))
    set_text(by_id(s, 37), f.tb(f.tb_used))
    assets = by_id(s, 30)
    set_para(assets, 1, BLANK)
    set_para(assets, 2, "Interview findings" if getattr(f.res, "notes", None) else BLANK)
    kind = {"cloudamize_ola": "Cloudamize Data Collector", "cloudamize_all": "Cloudamize Data Collector", "cloudamize_mpa": "Cloudamize Data Collector",
            "rvtools": "RVTools export"}.get(f.res.estate.source_kind, BLANK)
    set_para(by_id(s, 31), 2, kind)


def _chart(chart: Any, cats: list[str], series: list[tuple[str, list[float | None]]], money_fmt: bool = False) -> None:
    cd = CategoryChartData(number_format='"$"#,##0' if money_fmt else "General")
    cd.categories = cats
    for name, vals in series:
        cd.add_series(name, [None if v is None else round(v) if money_fmt else v for v in vals])
    chart.replace_data(cd)


def _part(prs: Any, name: str) -> Any:
    return next((p for p in prs.part.package.iter_parts() if str(p.partname) == name), None)


def _user_shapes(prs: Any, chart_shape: Any) -> Any:
    """The drawing that holds a chart's own text boxes and pictures (the template keeps its total labels there)."""
    return next((r.target_part for r in chart_shape.chart.part.rels.values() if r.reltype.endswith("/chartUserShapes")), None)


def _edit_blob(part: Any, fn: Any) -> None:
    part._blob = fn(part.blob)


def _env_counts(f: Facts) -> tuple[int, int, int]:
    """Production / non-production / DR by the inventory's own environment label; a workload with no label is counted in none of them."""
    prod = non = dr = 0
    ps = (f.res.aws.per_server if f.res.aws else None) or {}
    envs = [str(v.get("env") or "") for v in ps.values()] if any(v.get("env") for v in ps.values()) else [w.env or "" for w in f.res.estate.workloads]
    for env in envs:
        e = env.strip().lower()
        if not e:
            continue
        if re.search(r"\bdr\b|disaster|backup", e):
            dr += 1
        elif e.startswith("prod") or e in ("prd", "live"):
            prod += 1
        else:
            non += 1
    return prod, non, dr


_NS = {"mc": "http://schemas.openxmlformats.org/markup-compatibility/2006", "cx": "http://schemas.microsoft.com/office/drawing/2014/chartex",
       "p": "http://schemas.openxmlformats.org/presentationml/2006/main", "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
       "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}


def _donuts(prs: Any, f: Facts) -> None:
    """Slide 5's two doughnuts are PowerPoint-2016-only 'chartEx' charts with a static picture as fallback: any other viewer (and the PDF) would show the SAMPLE's
    picture.  They are replaced, at the same place and size, by standard doughnut charts that every viewer draws from the customer's numbers."""
    from pptx.dml.color import RGBColor
    from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION
    from pptx.util import Pt
    slide = prs.slides[4]
    zom = f.low if f.util_known else 0
    spec = {"chartEx2.xml": (["Virtual Machines", "Zombies"], [max(f.n - zom, 0), zom], f"{f.n} Workloads" if f.n else BLANK),
            "chartEx1.xml": (["Production", "Non-Production", "DR Backup"], list(_env_counts(f)), "Workload category")}
    palette = (RGBColor(0x0F, 0x5B, 0xD8), RGBColor(0x8F, 0xB4, 0xF2), RGBColor(0xC7, 0xD7, 0xF0))
    for ac in list(slide.shapes._spTree.findall("mc:AlternateContent", _NS)):
        cx = ac.find(".//cx:chart", _NS)
        if cx is None:
            continue
        rid = cx.get(f"{{{_NS['r']}}}id")
        name = str(slide.part.related_part(rid).partname).rsplit("/", 1)[-1]
        off, ext = ac.find(".//p:xfrm/a:off", _NS), ac.find(".//p:xfrm/a:ext", _NS)
        x, y, w, h = int(off.get("x")), int(off.get("y")), int(ext.get("cx")), int(ext.get("cy"))
        ac.getparent().remove(ac)
        slide.part.drop_rel(rid)
        cats, vals, title = spec[name]
        cd = CategoryChartData(number_format="0")
        cd.categories = cats
        cd.add_series("Workloads", vals)
        ch = slide.shapes.add_chart(XL_CHART_TYPE.DOUGHNUT, x, y, w, h, cd).chart
        ch.has_title = bool(title)
        if title:
            ch.chart_title.text_frame.text = title
            ch.chart_title.text_frame.paragraphs[0].runs[0].font.size = Pt(12)
            ch.chart_title.include_in_layout = False
        ch.has_legend = True
        ch.legend.position, ch.legend.include_in_layout = XL_LEGEND_POSITION.BOTTOM, False
        ch.legend.font.size = Pt(9)
        plot = ch.plots[0]
        plot.has_data_labels = True
        plot.data_labels.font.size, plot.data_labels.font.color.rgb = Pt(9), RGBColor(0xFF, 0xFF, 0xFF)
        plot.data_labels.show_value, plot.data_labels.number_format, plot.data_labels.number_format_is_linked = True, '0;;;', False   # a zero slice shows no label
        for i in range(len(cats)):
            pt = plot.series[0].points[i]
            pt.format.fill.solid()
            pt.format.fill.fore_color.rgb = palette[i % 3]


def _cost_summary(prs: Any, f: Facts) -> None:
    s = prs.slides[5]
    series = [(f"{PHRASE_SHORT[i]}", [f.infra(i)]) for i, _, _ in COLS]
    _chart(by_id(s, 10).chart, ["Total"], series, money_fmt=True)
    have = any(v[0] is not None for _, v in series)
    if not have:
        us = _user_shapes(prs, by_id(s, 10))
        if us is not None:                           # the sample's logo that sits on a column would float over an empty chart
            _edit_blob(us, lambda b: re.sub(rb"<cdr:relSizeAnchor.*?</cdr:relSizeAnchor>", b"", b, flags=re.S))
    call, rec = by_id(s, 11), by_id(s, 5)
    if f.best:
        set_run(call, 0, f"The {PHRASE[f.best]} will yield the lowest total cost of ownership with ")
        set_run(call, 1, f"savings of ~{f.save_infra}%")
        set_run(call, 2, " vs On Demand LI pricing model")
        set_run(rec, 1, f" Consider the {PHRASE[f.best]} option on a shared tenancy model")
    else:
        blank(call)
        set_run(rec, 1, "")
    if not have:                                  # no figures: the arrows and the frame that point at columns would point at nothing
        for sid in (12, 14, 4, 6):
            drop(by_id(s, sid))
        set_run(rec, 0, "")


PHRASE_SHORT = {"ri_3yr": "3 Year NURI - LI", "ri_1yr": "1 Year NURI - LI", "byol": "On-Demand - BYOL", "on_demand": "On-Demand - LI"}


def _expense(prs: Any, f: Facts) -> None:
    """The template prints each bar's total in a text box at a fixed spot, which cannot follow different values: those boxes are emptied and the
    total goes into the bar's own label, so a bar and its total can never disagree."""
    s = prs.slides[6]
    order = ("on_demand", "byol", "ri_1yr", "ri_3yr")
    base = ("On Demand - LI", "On Demand - BYOL", "1 Year NURI - LI", "3 Year NURI - LI")
    cats = [b + (f"  ({k(f.infra(i))})" if f.infra(i) is not None else "") for b, i in zip(base, order)]

    def col(key: str) -> list[float | None]:
        return [f.parts(i).get(key) if f.parts(i) else None for i in order]
    chart = by_id(s, 6)
    _chart(chart.chart, cats, [("Column1", [None] * 4), ("Compute", col("compute")), ("Attached Storage", col("storage")),
                                 ("Network", col("network")), ("License", col("license"))], money_fmt=True)
    us = _user_shapes(prs, chart)
    if us is not None:
        _edit_blob(us, lambda b: re.sub(rb"(<a:t>)\s*\$[\d,.]+K(</a:t>)", rb"\1\2", b))
    call = by_id(s, 8)
    if f.best:
        set_text(call, f"{PHRASE[f.best]} with instance resizing, on AWS for the Windows/Linux environments offered the best combination of operational resiliency, "
                       f"while generating ~{f.save_infra}% savings with a ${f.infra(f.best) / 1000:,.0f}K annualized spend compared to On Demand LI pricing model. ")
    else:
        blank(call)
    set_text(by_id(s, 2), BLANK)


def _detail(prs: Any, f: Facts) -> None:
    s = prs.slides[7]
    tbl = by_id(s, 3).table
    for j, (sid, l1, l2) in enumerate(COLS, start=1):
        set_cell_lines(tbl.cell(1, j), l1, l2)
        p = f.parts(sid)
        infra = f.infra(sid)
        lic = BLANK if not p else ("incl." if (p.get("license") == 0.0 and sid != "byol") else (k(p["license"]) if p.get("license") else "-"))
        vals = [k(p.get("compute")), k(p.get("storage")), k(p.get("network")), lic, k(infra), k(p.get("support")), k(f.rows[sid].total if sid in f.rows else None)]
        for i, v in enumerate(vals, start=3):
            set_cell(tbl.cell(i, j), v)
        od = f.rows.get("on_demand")
        sav = BLANK if (sid not in f.rows or od is None) else ("-" if sid == "on_demand" else f"{round(100 * (1 - f.rows[sid].total / od.total))}%")
        set_cell(tbl.cell(10, j), sav)
    call, note = by_id(s, 6), by_id(s, 4)
    if f.best:
        set_run(call, 1, f"  {PHRASE[f.best]} with instance resizing, on AWS offered the best combination of operational resiliency, while generating ~{f.save_total}% savings "
                         f"with a ${f.rows[f.best].total / 1000:,.0f}K annualized spend compared to On Demand LI pricing model. (AWS Business Support Included)")
    else:
        blank(call)
    src = "AWS Transform assessment" if any(r.source == "aws_transform" for r in f.rows.values()) else ("A01 offline estimate on placeholder prices" if f.rows else "")
    set_text(note, f"Figures from the {src}; network and support as calculated there." if src else BLANK)
    if not f.rows:
        drop(by_id(s, 7))


def _sql(prs: Any, f: Facts) -> None:
    """SQL vCPUs by edition from AWS Transform's database assessment: what is provisioned on premises, and what its recommended RDS instances license.  The third
    row ("Optimize CPU") and the Software Assurance tables are not in any AWS output, so they stay blank."""
    t9 = by_id(prs.slides[8], 29).table
    for i in (1, 2, 3):
        for j in (1, 2, 3):
            set_cell(t9.cell(i, j), BLANK)
    cores = f.sql_cores()
    if cores:                                                     # right-sized: the vCPUs AWS licenses on its recommended RDS instances, by edition
        set_cell(t9.cell(2, 1), f"{cores.get('Standard', (0.0, 0.0))[1]:g}")
        set_cell(t9.cell(2, 2), f"{cores.get('Enterprise', (0.0, 0.0))[1]:g}")
        set_cell(t9.cell(2, 3), f"{sum(v[1] for v in cores.values()):g}")
    if f.sql_onprem_cpus() is not None:                           # on premises: AWS's own total only (no edition split is given)
        set_cell(t9.cell(1, 3), str(f.sql_onprem_cpus()))
    t10 = by_id(prs.slides[9], 29).table
    for i in (1, 2):
        for j in (1, 2, 3):
            set_cell(t10.cell(i, j), BLANK)
    blank(by_id(prs.slides[9], 4))


def _zombies(prs: Any, f: Facts) -> None:
    _chart(by_id(prs.slides[10], 8).chart, [""], [("<5%", [len(f.b5) if f.util_known else None]), ("5%-10%", [len(f.b10) if f.util_known else None]),
                                                   ("10%-20%", [len(f.b20) if f.util_known else None])])
    s = prs.slides[11]
    call = by_id(s, 4)
    save = _zombie_saving(f)
    txt = call.text_frame.text
    cut = re.sub(r"\s*Upwards of roughly \$[\d,.]+K? when migrating the environment to the AWS Cloud\.", "", txt)
    set_text(call, cut + (f" Upwards of roughly ${save / 1000:,.0f}K when migrating the environment to the AWS Cloud." if save else ""))
    top = f.b5[:10]
    if top:
        from .pptx_deck import _table
        rows = [["Server", "Avg CPU %", "vCPU", "RAM GB"]] + [[w.name.split(".")[0], f"{w.cpu:.1f}", "" if w.vcpu is None else f"{w.vcpu:g}", "" if w.ram is None else f"{w.ram:g}"] for w in top]
        _table(s, Inches(0.38), Inches(1.95), Inches(6.6), rows, [3.0, 1.2, 1.0, 1.0], row_h=0.32, size=11, name="TABLE_zombies")


def _zombie_saving(f: Facts) -> float | None:
    """The AWS Transform cost per year of the servers under 5% CPU, in the pricing model AWS recommends.  Nothing is estimated without it."""
    tot = sum(r.cost or 0.0 for r in f.b5)
    return tot or None


_WIN_ROWS = ((1, lambda y, r2: y <= 2012), (2, lambda y, r2: y == 2016), (3, lambda y, r2: y == 2019), (4, lambda y, r2: y == 2022), (5, lambda y, r2: y == 2025))


def _windows(prs: Any, f: Facts) -> None:
    s = prs.slides[12]
    counts = {i: 0 for i, _ in _WIN_ROWS}
    for w in f.res.estate.workloads:
        if w.os_family != "windows":
            continue
        m = re.search(r"(20\d\d)", f"{w.os_label or ''} {w.os_raw or ''}")
        if not m:
            continue
        y = int(m.group(1))
        for i, test in _WIN_ROWS:
            if test(y, False):
                counts[i] += 1
    tbl = by_id(s, 6).table
    for i, _ in _WIN_ROWS:
        set_cell(tbl.cell(i, 1), str(counts[i]) if f.win_inv else BLANK)       # AWS's workbook names no Windows versions: from the inventory only
    rec = by_id(s, 8)
    if not f.win_inv:
        blank(rec)
    elif counts[1]:
        set_text(rec, "Recommendation: compute workload carries a high risk because it runs unsupported Windows Server versions. Upgrading to the latest supported version during migration mitigates the risk.")
    elif counts[2] or counts[3]:
        pass                                                      # the template's own medium-risk wording is correct for this estate
    else:
        set_text(rec, "Recommendation: compute workload carries a low risk: the Windows Server versions in use are supported.")


def _notes(prs: Any, f: Facts) -> None:
    """The release status travels with the deck in the speaker notes, not on the slides, so the slides stay exactly the template's."""
    c = f.c
    for i, slide in enumerate(prs.slides, start=1):
        slide.notes_slide.notes_text_frame.text = f"A01 {c.run_id} | {c.status} | scope figures from: {f.src} | slide {i} of {len(prs.slides)}"


def fill_ingram_template(prs: Any, c: Any) -> list[str]:
    """Fill the open template.  Returns the problems (empty when everything was filled)."""
    f = Facts(c)
    problems: list[str] = []
    for fn in (_customer, _agenda, _summary, _scope, _donuts, _cost_summary, _expense, _detail, _sql, _zombies, _windows, _notes):
        try:
            fn(prs, f)
        except Exception as e:                                     # one slide that does not fit must not lose the others: it is reported
            problems.append(f"{fn.__name__.strip('_')}: {type(e).__name__}: {e}")
    return problems
