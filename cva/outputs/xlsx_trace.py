"""Supporting data workbook (SOW A01): traces every slide figure to workload-level data.

Totals on the Summary sheet are live formulas over the Workloads sheet, and each sits beside the value recorded in
the figure ledger with an OK/CHECK flag, so the workbook proves its own arithmetic.  Everything else is data.
"""
from __future__ import annotations

import json
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from ..pipeline import PipelineResult
from ..transform.analysis import short_name
from .scenario_view import rows as scen_rows

FONT = "Arial"
HDR = PatternFill("solid", fgColor="2B2F5E")
WARN = PatternFill("solid", fgColor="FFF2CC")
BAD = PatternFill("solid", fgColor="F8CBAD")
GREY = PatternFill("solid", fgColor="EDEDF4")
MONEY = '$#,##0;($#,##0);"-"'
PCT = '0.0"%"'


def _hdr(ws, row: int, headers: list[str]) -> None:
    for j, h in enumerate(headers, 1):
        c = ws.cell(row, j, h)
        c.font = Font(name=FONT, bold=True, color="FFFFFF", size=10)
        c.fill = HDR
        c.alignment = Alignment(vertical="center", wrap_text=True)


def _widths(ws, widths: list[int]) -> None:
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w


def _aws_cost(models: dict[str, Any], storage: float, slug: str) -> float | None:
    """One server's annual AWS cost under a pricing model: license + EC2 + network (AWS's own per-server total) + its storage.  AWS's support charge is a
    percentage of the total and is not allocated to servers."""
    m = models.get(slug)
    return round((m["total"] or 0.0) + storage, 2) if m and m.get("total") is not None else None


def build_workbook(res: PipelineResult, state: dict[str, Any], gate: dict[str, Any], audit_info: dict[str, Any],
                   commentary: dict[str, dict[str, str]], run_id: str, out_path: str) -> str:
    wb = Workbook()
    ledger = res.ledger
    finalised = bool(state.get("finalised"))

    # ------------------------------------------------------------------ Workloads (built first: Summary points at it)
    wl = wb.active
    wl.title = "Workloads"
    heads = ["Workload", "Source rows", "OS", "Env", "Current vCPU", "Current RAM (GB)", "CPU avg %", "CPU peak %",
             "Sizing basis", "Confidence", "Recommended instance", "Rec vCPU", "Rec RAM (GB)", "Direction",
             "On-Demand ($/yr)", "1-yr RI ($/yr)", "3-yr RI ($/yr)", "BYOL ($/yr)", "Infra ($/yr, On-Demand)",
             "Windows lic ($/yr, On-Demand)", "SQL lic ($/yr, On-Demand)", "Storage ($/yr)",
             "BYOL treatment", "Lifecycle", "Flags and notes",
             "AWS instance (recommended model)", "AWS On-Demand ($/yr)", "AWS 1-yr RI ($/yr)", "AWS 3-yr RI ($/yr)", "AWS storage ($/yr)", "AWS note"]
    wl.cell(1, 1, "Workload-level data behind every figure.  Each cost cell is an output of rule PR-INFRA / PR-LIC / "
                  "PR-STORAGE (see Ledger).  Costs use the catalog named on the Summary sheet.").font = Font(name=FONT, italic=True, size=9)
    _hdr(wl, 3, heads)
    sc_ids = ["on_demand", "ri_1yr", "ri_3yr", "byol"]
    lc_by: dict[str, list[str]] = {}
    for f in res.lifecycle:
        lc_by.setdefault(f.workload, []).append(f"{f.label}: {f.status.replace('_', ' ')} ({f.end_of_support})")
    r = 4
    for w in res.estate.workloads:
        sz = res.rightsizing.get(w.name)
        costs = {sid: res.pricing.costs.get(sid, {}).get(w.name) for sid in sc_ids}
        arec = res.aws.per_server.get(short_name(w.name)) if (res.aws and res.aws.per_server) else None
        am = (arec or {}).get("models", {})
        stor = (arec or {}).get("storage_annual") or 0.0

        od = costs["on_demand"]
        by = costs["byol"]
        byol_status = (res.pricing.scenario("byol").status if res.pricing.scenario("byol") else "")
        treat = "; ".join(f"{t['product'].replace('_server', '')}: {t['model_used']} ({t['source']})"
                          for t in (by.treatments if by else [])) or ""
        row = [w.name, ", ".join(str(x) for x in w.refs[:3]), w.os_label or w.os_raw, w.env, w.vcpu, w.ram_gb,
               w.cpu_avg, w.cpu_peak, sz.basis if sz else None, sz.confidence if sz else None,
               sz.instance if sz and sz.ok else None, sz.rec_vcpu if sz else None, sz.rec_mem_gb if sz else None,
               sz.direction if sz else None,
               od.total if od else None, costs["ri_1yr"].total if costs["ri_1yr"] else None,
               costs["ri_3yr"].total if costs["ri_3yr"] else None,
               (by.total if (by and byol_status != "not_computable") else None),
               od.infra if od else None, od.windows if od else None, od.sql if od else None, od.storage if od else None,
               treat, "; ".join(lc_by.get(w.name, [])), "; ".join((sz.flags if sz else []) + (sz.notes if sz else [])),
               (am.get("three_year_nuri") or am.get("on_demand") or {}).get("instance"), _aws_cost(am, stor, "on_demand"), _aws_cost(am, stor, "one_year_nuri"),
               _aws_cost(am, stor, "three_year_nuri"), (arec or {}).get("storage_annual"),
               (None if arec else ("not in AWS Transform's per-server results" if (res.aws and res.aws.per_server) else None))]
        for j, v in enumerate(row, 1):
            c = wl.cell(r, j, v)
            if j in (7, 8) and v is not None:
                c.number_format = "0.0"
            if 15 <= j <= 22:
                c.number_format = MONEY
        r += 1
    last = r - 1
    wl.cell(r + 1, 1, "Total").font = Font(name=FONT, bold=True, size=10)
    for j in range(15, 23):
        col = get_column_letter(j)
        c = wl.cell(r + 1, j, f"=SUM({col}4:{col}{last})")
        c.number_format = MONEY
        c.font = Font(name=FONT, bold=True, size=10)
    for j in (5, 6, 12, 13):
        col = get_column_letter(j)
        c = wl.cell(r + 1, j, f"=SUM({col}4:{col}{last})")
        c.font = Font(name=FONT, bold=True, size=10)
    wl.freeze_panes = "B4"
    _widths(wl, [16, 30, 26, 8, 10, 12, 9, 9, 18, 11, 18, 9, 11, 11, 14, 14, 14, 14, 14, 14, 14, 12, 40, 44, 60])
    tot_row = r + 1

    # ------------------------------------------------------------------ Ledger
    lg = wb.create_sheet("Ledger")
    lg.cell(1, 1, "Figure ledger: every number that can appear on a slide, with the rule and rule version that produced it.").font = Font(name=FONT, italic=True, size=9)
    _hdr(lg, 3, ["Figure ID", "Key", "Label", "Value", "Unit", "Rule", "Rule version", "Inputs (JSON)", "Source files"])
    for i, f in enumerate(ledger.all(), 4):
        vals = [f.id, f.key, f.label, f.value, f.unit, f.rule, f.rule_version,
                json.dumps(f.inputs, default=str)[:300], ", ".join(f.refs)]
        for j, v in enumerate(vals, 1):
            c = lg.cell(i, j, v)
            if j == 4 and isinstance(v, (int, float)) and f.unit == "USD/yr":
                c.number_format = MONEY
    lg.freeze_panes = "A4"
    _widths(lg, [10, 34, 40, 14, 9, 12, 12, 60, 36])
    lg_last = 3 + len(ledger.all())

    # ------------------------------------------------------------------ Summary
    sm = wb.create_sheet("Summary", 0)
    sm.cell(1, 1, "CVA / OLA supporting data workbook").font = Font(name=FONT, bold=True, size=14)
    status_txt = "FINAL (review gate passed)" if finalised else f"DRAFT: not released; {gate['open']} review-gate item(s) open"
    c = sm.cell(2, 1, status_txt)
    c.font = Font(name=FONT, bold=True, color="C00000" if not finalised else "1F6F3F", size=11)
    info = [("Run", run_id), ("Source files", ", ".join(res.estate.files)), ("Source kind", res.estate.source_kind),
            ("Data richness", f"{res.meta['richness']['grade']}: {res.meta['richness']['label']}"),
            ("Workloads", len(res.estate.workloads)), ("Figures source", (f"AWS Transform assessment '{res.aws.scenario_name}'; A01 estimate is a cross-check"
                                                                       if res.aws else "A01 offline estimate (placeholder prices)")),
            ("Rule set", res.meta["ruleset"]),
            ("Config digest", res.meta["config_digest"]), ("As-of date (lifecycle)", res.meta["as_of"]),
            ("Price catalog", f"{res.meta['catalog']['version']}" + ("  PLACEHOLDER PRICES: not the AWS Price List"
                                                                  if res.meta["catalog"]["placeholder"] else "")),
            ("Lifecycle rules", "UNVERIFIED dates (see Assumptions)" if res.meta["lifecycle_rules_unverified"] else "verified"),
            ("Licensing input", f"{res.licensing.mode}: {res.licensing.provenance}"),
            ("Audit chain", f"{audit_info['message']} ({audit_info['entries']} entries)")]
    for i, (k, v) in enumerate(info, 4):
        sm.cell(i, 1, k).font = Font(name=FONT, bold=True, size=10)
        sm.cell(i, 2, v)
        if k == "Price catalog" and res.meta["catalog"]["placeholder"]:
            sm.cell(i, 2).fill = WARN
    r0 = 4 + len(info) + 1
    sm.cell(r0, 1, "Scenario totals ($ per year)").font = Font(name=FONT, bold=True, size=11)
    _hdr(sm, r0 + 1, ["Scenario", "Total from Workloads (formula)", "Ledger value", "Difference", "Check", "Status", "Note"])
    col_for = {"on_demand": "O", "ri_1yr": "P", "ri_3yr": "Q", "byol": "R"}
    led_ref = lambda key: f'INDEX(Ledger!$D$4:$D${lg_last},MATCH("{key}",Ledger!$B$4:$B${lg_last},0))'  # noqa: E731
    view = scen_rows(res)
    k = 0
    for row in view:
        rr = r0 + 2 + k
        k += 1
        sm.cell(rr, 1, row.label)
        if row.total is None:
            sm.cell(rr, 2, row.status.replace("_", " "))
            sm.cell(rr, 5, "n/a")
        elif row.source == "aws_transform":
            sm.cell(rr, 2, "=" + led_ref(f"scenario.{row.id}.total")).number_format = MONEY
            sm.cell(rr, 3, "AWS Transform figure (imported unchanged; see the AWS Transform sheet)")
            sm.cell(rr, 5, "imported")
        else:
            key = "scenario.byol.total" if (res.aws and row.id == "byol") else f"{'a01' if res.aws else 'scenario'}.{row.id}.total"
            sm.cell(rr, 2, f"=Workloads!{col_for[row.id]}{tot_row}").number_format = MONEY
            sm.cell(rr, 3, "=" + led_ref(key)).number_format = MONEY
            sm.cell(rr, 4, f"=B{rr}-C{rr}").number_format = '$#,##0.00;($#,##0.00);"-"'
            sm.cell(rr, 5, f'=IF(ABS(D{rr})<1,"OK","CHECK")')
        sm.cell(rr, 6, f"{row.status} | {'AWS Transform' if row.source == 'aws_transform' else 'A01 estimate'}")
        sm.cell(rr, 7, " ".join(row.notes))
    if res.aws:                                   # A01's own estimate stays visible as a cross-check, with its self-check
        sm.cell(r0 + 2 + k, 1, "A01 offline estimate (cross-check, placeholder prices)").font = Font(name=FONT, bold=True, size=10)
        k += 1
        for sid in ("on_demand", "ri_1yr", "ri_3yr"):
            sc = res.pricing.scenario(sid)
            rr = r0 + 2 + k
            k += 1
            sm.cell(rr, 1, "  " + (sc.label if sc else sid))
            if sc and sc.total is not None:
                sm.cell(rr, 2, f"=Workloads!{col_for[sid]}{tot_row}").number_format = MONEY
                sm.cell(rr, 3, "=" + led_ref(f"a01.{sid}.total")).number_format = MONEY
                sm.cell(rr, 4, f"=B{rr}-C{rr}").number_format = '$#,##0.00;($#,##0.00);"-"'
                sm.cell(rr, 5, f'=IF(ABS(D{rr})<1,"OK","CHECK")')
    rr = r0 + 2 + k + 1
    sm.cell(rr, 1, "Workload and exception counts").font = Font(name=FONT, bold=True, size=11)
    sm.cell(rr + 1, 1, "Workloads listed")
    sm.cell(rr + 1, 2, "=COUNTA(Workloads!A4:A" + str(last) + ")")
    sm.cell(rr + 1, 3, f'=INDEX(Ledger!$D$4:$D${lg_last},MATCH("estate.workloads",Ledger!$B$4:$B${lg_last},0))')
    sm.cell(rr + 1, 5, f'=IF(B{rr + 1}=C{rr + 1},"OK","CHECK")')
    sm.cell(rr + 2, 1, "Exceptions listed")
    sm.cell(rr + 2, 2, "=COUNTA(Exceptions!A4:A" + str(3 + max(1, len(res.issues))) + ")")
    sm.cell(rr + 2, 3, f'=INDEX(Ledger!$D$4:$D${lg_last},MATCH("quality.total",Ledger!$B$4:$B${lg_last},0))')
    sm.cell(rr + 2, 5, f'=IF(B{rr + 2}=C{rr + 2},"OK","CHECK")')
    sm.cell(rr + 3, 1, "Workloads sized")
    sm.cell(rr + 3, 2, f'=COUNTA(Workloads!K4:K{last})')
    sm.cell(rr + 3, 3, f'=INDEX(Ledger!$D$4:$D${lg_last},MATCH("sizing.sized",Ledger!$B$4:$B${lg_last},0))')
    sm.cell(rr + 3, 5, f'=IF(B{rr + 3}=C{rr + 3},"OK","CHECK")')
    rr += 5
    sm.cell(rr, 1, "Commentary used on the slides").font = Font(name=FONT, bold=True, size=11)
    for k, (slide, d) in enumerate(commentary.items(), 1):
        sm.cell(rr + k, 1, slide.replace("_", " "))
        sm.cell(rr + k, 2, f"[{d['source']}] {d['text']}")
        sm.cell(rr + k, 2).alignment = Alignment(wrap_text=True, vertical="top")
    _widths(sm, [30, 46, 18, 14, 10, 16, 80])

    # ------------------------------------------------------------------ Exceptions
    ex = wb.create_sheet("Exceptions")
    ex.cell(1, 1, "Data-quality exceptions for analyst review.  'Acknowledged' is a recorded human decision.").font = Font(name=FONT, italic=True, size=9)
    _hdr(ex, 3, ["ID", "Severity", "Code", "Workload", "Message", "Source rows", "Acknowledged", "Acknowledgement note"])
    for i, it in enumerate(res.issues, 4):
        key = f"{it.code}|{it.workload or ''}"
        ack = state["acks"].get(key) or state["ack_codes"].get(it.code)
        vals = [it.id, it.severity, it.code, it.workload, it.message, ", ".join(it.refs), "yes" if ack else "no",
                (ack or {}).get("note", "")]
        for j, v in enumerate(vals, 1):
            c = ex.cell(i, j, v)
            c.alignment = Alignment(wrap_text=(j == 5), vertical="top")
        if it.severity == "error":
            ex.cell(i, 2).fill = BAD
        elif it.severity == "warn":
            ex.cell(i, 2).fill = WARN
    ex.freeze_panes = "A4"
    _widths(ex, [10, 9, 18, 18, 90, 40, 13, 40])

    # ------------------------------------------------------------------ Licensing
    li = wb.create_sheet("Licensing")
    li.cell(1, 1, f"Licensing input: {res.licensing.mode} ({res.licensing.provenance}).  Findings labelled 'assumption' are "
                  "not evidence.").font = Font(name=FONT, italic=True, size=9)
    _hdr(li, 3, ["Workload", "Product", "Instance", "Model", "Tenancy", "Licensed by", "Active SA", "Include in count",
                 "Cores", "Origin", "Confidence", "Basis", "Ref"])
    rr = 4
    for f in res.licensing.findings:
        for j, v in enumerate([f.workload, f.product, f.instance, f.model, f.tenancy, f.licensed_by, f.active_sa,
                               f.include_in_count, f.cores, f.origin, f.confidence, f.basis, f.ref], 1):
            li.cell(rr, j, v)
        if f.origin == "assumption":
            for j in range(1, 14):
                li.cell(rr, j).fill = WARN
        rr += 1
    rr += 1
    li.cell(rr, 1, "Gaps (license-bearing items with no usable finding)").font = Font(name=FONT, bold=True, size=11)
    _hdr(li, rr + 1, ["Workload", "Product", "Instance", "Edition", "Reason", "Env"])
    for k, g in enumerate(res.lic_gaps, rr + 2):
        for j, v in enumerate([g["workload"], g["product"], g["instance"], g["edition"], g["reason"], g["env"]], 1):
            li.cell(k, j, v)
    _widths(li, [18, 16, 18, 10, 12, 12, 10, 14, 8, 12, 11, 60, 24])

    # ------------------------------------------------------------------ Lifecycle
    lc = wb.create_sheet("Lifecycle")
    lc.cell(1, 1, "End-of-support findings.  ALL dates are from an unverified rules table (config/rules/lifecycle.toml): "
                  "verify against the vendor lifecycle page before customer use.").font = Font(name=FONT, italic=True, size=9, color="C00000")
    _hdr(lc, 3, ["Workload", "Product", "Item", "End of support", "Status", "Months to/after", "Note", "Source", "Verified"])
    for i, f in enumerate(sorted(res.lifecycle, key=lambda x: (x.status != "end_of_support", x.months)), 4):
        for j, v in enumerate([f.workload, f.product, f.label, f.end_of_support, f.status, f.months, f.note, f.source,
                               "yes" if f.verified else "no"], 1):
            lc.cell(i, j, v)
    _widths(lc, [18, 9, 28, 14, 16, 12, 44, 22, 9])

    # ------------------------------------------------------------------ Parity
    pa = wb.create_sheet("Parity")
    pa.cell(1, 1, f"Our independent sizing compared with reference engines.  Tolerance {res.parity_summary['tolerance_pct']}%.  "
                  "Costs are compute only, priced like for like.").font = Font(name=FONT, italic=True, size=9)
    _hdr(pa, 3, ["Workload", "Reference", "Our instance", "Reference instance", "Our cost ($/yr)", "Reference cost ($/yr)",
                 "Difference %", "Status", "Note", "Reviewer override"])
    for i, p in enumerate(res.parity_rows, 4):
        ov = state["sizing_overrides"].get(p.workload)
        for j, v in enumerate([p.workload, p.reference, p.ours_instance, p.ref_instance, p.ours_cost, p.ref_cost,
                               p.diff_pct, p.status, p.note, (ov or {}).get("instance")], 1):
            c = pa.cell(i, j, v)
            if j in (5, 6) and v is not None:
                c.number_format = MONEY
    pa.freeze_panes = "A4"
    _widths(pa, [18, 14, 16, 16, 14, 16, 12, 16, 36, 16])

    # ------------------------------------------------------------------ Assumptions
    asum = wb.create_sheet("Assumptions")
    _hdr(asum, 1, ["Item", "Value / statement", "Where it comes from"])
    items: list[tuple[str, str, str]] = [
        ("Price catalog (A01 estimate only)", f"{res.meta['catalog']['version']}", "config/rules/pricing_catalog.toml.  PLACEHOLDER: Linux prices are Aivar estimates; Windows prices for t3/t3a come from Cloudamize's public sample.  Used only as a cross-check when an AWS Transform assessment is imported."),
        ("Scenario definitions", "; ".join(f"{s['label']} = {s['purchase']} + {s['license']}" for s in res.cfg.scenarios),
         "config/scenarios.toml.  ASSUMPTION: 'BYOL' means on-demand purchase with the customer's own licenses where eligible; confirm with Ingram."),
        ("Sizing targets", f"CPU {res.cfg.section('sizing')['cpu_target_util']}%, memory {res.cfg.section('sizing')['mem_target_util']}%",
         "config/settings.toml [sizing]"),
        ("Lifecycle rules", "All dates unverified", "config/rules/lifecycle.toml"),
        ("Hours per year", str(res.book.hours), "config/settings.toml [pricing]"),
        ("Storage", f"used x {res.cfg.section('sizing')['storage_headroom']} x ${res.book.storage_gb_month}/GB-month x 12", "PR-STORAGE"),
        ("Reviewer overrides", str(len(state["sizing_overrides"])), "state: accepted parity proposals"),
        ("Licensing assumptions accepted", str(len(state["assumptions"])), "state: accepted licensing assumptions (labelled assumption, not evidence)"),
    ]
    for k, w in state["sizing_overrides"].items():
        items.append((f"Override: {k}", f"{w.get('instance')}", w.get("reason", "")))
    for a in state["assumptions"]:
        items.append((f"Assumption: {a['workload']}", f"{a['product']} {a['model']}", a.get("basis", "")))
    for i, row in enumerate(items, 2):
        for j, v in enumerate(row, 1):
            c = asum.cell(i, j, v)
            c.alignment = Alignment(wrap_text=True, vertical="top")
    _widths(asum, [32, 60, 90])

    # ------------------------------------------------------------------ AWS Transform (imported assessment)
    if res.aws:
        aw = wb.create_sheet("AWS Transform", 1)
        a = res.aws
        aw.cell(1, 1, f"Imported AWS Transform assessment '{a.scenario_name}' ({a.timestamp}).  Figures are imported unchanged; "
                      "per-server recommendations are in the analysis workbook listed below.").font = Font(name=FONT, italic=True, size=9)
        _hdr(aw, 3, ["Pricing model", "Total ($/yr)", "Compute", "Storage", "Network", "Support", "Recommended by AWS Transform"])
        for i, m in enumerate(a.models, 4):
            for j, v in enumerate([m.label, m.total, m.by_category.get("compute"), m.by_category.get("storage"),
                                   m.by_category.get("network"), m.by_category.get("support"), "yes" if m.recommended else ""], 1):
                c = aw.cell(i, j, v)
                if 2 <= j <= 6 and v is not None:
                    c.number_format = MONEY
        r = 5 + len(a.models)
        facts = [("On-premises baseline ($)", a.on_prem_total), ("Servers in the file", a.servers.get("total")),
                 ("Servers in scope", a.servers.get("in_scope")), ("Servers excluded", a.servers.get("excluded")),
                 ("Exclusion reasons", "; ".join(a.servers.get("reasons") or [])), ("Warning rows (error_file.csv)", a.warning_rows),
                 ("Job", json.dumps(a.job, default=str))]
        for k, (lab, val) in enumerate(facts):
            aw.cell(r + k, 1, lab).font = Font(name=FONT, bold=True, size=10)
            aw.cell(r + k, 2, val)
        r += len(facts) + 1
        _hdr(aw, r, ["Assessment", "Annual cost", "Resources", "Capacity", "Assumptions", "Recommendations"])
        for i, (t, v) in enumerate(a.assessments.items(), r + 1):
            for j, val in enumerate([t, v.get("annual_cost"), v.get("resources"), v.get("capacity"), "; ".join(v.get("assumptions") or []),
                                     "; ".join(x for x in (v.get("recommendations") or []) if x)], 1):
                aw.cell(i, j, val).alignment = Alignment(wrap_text=True, vertical="top")
        r += len(a.assessments) + 2
        _hdr(aw, r, ["Files", "Path"])
        for i, (k2, f) in enumerate(a.files.items(), r + 1):
            aw.cell(i, 1, k2)
            aw.cell(i, 2, f)
        _widths(aw, [34, 16, 14, 14, 60, 60, 24])

    # ------------------------------------------------------------------ Audit
    au = wb.create_sheet("Audit")
    au.cell(1, 1, f"Audit chain: {audit_info['message']}; {audit_info['entries']} entries; head {audit_info.get('head', '')[:16]}").font = Font(name=FONT, italic=True, size=9)
    _hdr(au, 3, ["#", "Time (UTC)", "Event", "Actor", "Data (summary)"])
    for i, e in enumerate(audit_info.get("recent", []), 4):
        for j, v in enumerate([e["n"], e["ts"], e["event"], e["actor"], json.dumps(e["data"], default=str)[:200]], 1):
            au.cell(i, j, v)
    _widths(au, [6, 22, 18, 12, 100])

    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for c in row:
                if c.font.name != FONT:
                    c.font = Font(name=FONT, size=10, bold=c.font.bold, italic=c.font.italic, color=c.font.color)
    wb.save(out_path)
    return out_path
