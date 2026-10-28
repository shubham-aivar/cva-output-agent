"""The deck is Ingram's own OLA template, filled in place: same 25 slides, customer figures only, blank where there is no data."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from pptx import Presentation

from cva.outputs.ingram_ola import is_ingram_template
from cva.outputs.pptx_deck import build_deck
from cva.pipeline import RunInputs, run_pipeline
from cva.transform.results import load_results

ROOT = Path(__file__).resolve().parent.parent
TPL = ROOT / "templates" / "Ingram_OLA_Template.pptx"
RVT = ROOT / "samples" / "real_format" / "rvtools_sample_export_2.xlsx"
AWS = ROOT / "samples" / "real_format" / "aws_transform_assessment_rvtools"
SAMPLE_FIGURES = ("$209K", "$253K", "$318K", "$467K", "$221K", "$270K", "$342K", "$491K", "355 Workloads", "151.1", "61.5 TB", "$88K", "$157K", "$848K", "116 VM")


def slide_text(prs, i):
    out = []
    for sh in prs.slides[i].shapes:
        if sh.has_text_frame:
            out.append(sh.text_frame.text)
        if getattr(sh, "has_table", False) and sh.has_table:
            out += [c.text for r in sh.table.rows for c in r.cells]
    return "\n".join(out)


def all_text(prs):
    return "\n".join(slide_text(prs, i) for i in range(len(prs.slides)))


def chart_xml(prs):
    return "".join(str(p.blob) for p in prs.part.package.iter_parts() if "/charts/chart" in str(p.partname))


@pytest.fixture(scope="module")
def decks(tmp_path_factory):
    d = tmp_path_factory.mktemp("decks")
    aws = load_results(str(AWS), workdir=str(d / "aws"))
    out = {}
    for name, inp in (("blank", RunInputs(files=[str(RVT)], as_of="2026-10-20")), ("aws", RunInputs(files=[str(RVT)], as_of="2026-10-20", aws=aws.to_dict()))):
        r = run_pipeline(inp)
        info = build_deck(r, {"finalised": None}, {"open": 2}, {}, "run-0001", "wb.xlsx", str(d / f"{name}.pptx"), template=str(TPL))
        assert info["problems"] == [], info["problems"]
        out[name] = (Presentation(str(d / f"{name}.pptx")), r)
    return out


def test_the_shipped_template_is_recognised_and_is_the_one_the_customer_supplied():
    assert is_ingram_template(Presentation(str(TPL))) and len(Presentation(str(TPL)).slides) == 25


@pytest.mark.parametrize("name", ["blank", "aws"])
def test_the_deck_has_exactly_the_templates_25_slides_in_order_with_the_same_layouts_and_titles(decks, name):
    prs, _ = decks[name]
    tpl = Presentation(str(TPL))
    assert len(prs.slides) == 25
    assert [s.slide_layout.name for s in prs.slides] == [s.slide_layout.name for s in tpl.slides]
    for i in range(25):
        t = [sh.text_frame.text for sh in tpl.slides[i].shapes if sh.has_text_frame and sh.shape_type == 14 and sh.name.startswith("Title")][:1]
        n = [sh.text_frame.text for sh in prs.slides[i].shapes if sh.has_text_frame and sh.shape_type == 14 and sh.name.startswith("Title")][:1]
        assert t == n, f"slide {i + 1} title changed"


@pytest.mark.parametrize("name", ["blank", "aws"])
def test_no_sample_figure_of_the_template_survives_in_any_deck(decks, name):
    prs, _ = decks[name]
    text = all_text(prs) + chart_xml(prs)
    for s in SAMPLE_FIGURES:
        assert s not in text, s


def test_slides_with_no_customer_data_are_untouched(decks):
    prs, _ = decks["aws"]
    tpl = Presentation(str(TPL))
    for i in (0, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24):                       # title, plan, MAP, why Ingram, services, next steps, thanks, hidden costs, FAQ, picture
        assert slide_text(prs, i) == slide_text(tpl, i), f"slide {i + 1} changed"


def test_without_cost_data_the_cost_sections_are_blank_but_present(decks):
    prs, _ = decks["blank"]
    s8 = next(sh for sh in prs.slides[7].shapes if sh.has_table).table
    assert all(s8.cell(r, c).text.strip() == "" for r in range(3, 11) for c in range(1, 5))
    assert [s8.cell(r, 0).text for r in range(3, 11)][:3] == ["Compute", "Storage", "Network"]            # the section and its row labels stay
    assert "annualized spend" not in slide_text(prs, 3) and "annualized spend" not in slide_text(prs, 6)
    assert "SQL BYOL Core Reduction" in slide_text(prs, 8) and "Microsoft Windows Support" in slide_text(prs, 12)


def test_with_the_assessment_the_cost_table_carries_awss_own_totals(decks):
    prs, r = decks["aws"]
    t = next(sh for sh in prs.slides[7].shapes if sh.has_table).table
    by = {s.id: s for s in __import__("cva.outputs.scenario_view", fromlist=["rows"]).rows(r) if s.total is not None}
    assert t.cell(9, 1).text == f"${by['ri_3yr'].total / 1000:,.0f}K" and t.cell(9, 2).text == f"${by['ri_1yr'].total / 1000:,.0f}K"
    assert t.cell(9, 4).text == f"${by['on_demand'].total / 1000:,.0f}K"
    assert t.cell(9, 3).text == "" and t.cell(10, 3).text == ""                          # On Demand BYOL: no licensing input, so no figure
    assert t.cell(10, 4).text == "-"
    pct = round(100 * (1 - by["ri_3yr"].total / by["on_demand"].total))
    assert f"~{pct}% savings" in slide_text(prs, 7)


def test_sql_core_tables_stay_blank_because_no_core_counts_are_in_the_inputs(decks):
    prs, _ = decks["aws"]
    for i, rows in ((8, (1, 2, 3)), (9, (1, 2))):
        t = next(sh for sh in prs.slides[i].shapes if sh.has_table).table
        assert all(t.cell(r, c).text.strip() == "" for r in rows for c in (1, 2, 3))


def test_scope_slide_counts_come_from_the_inventory_and_the_doughnuts_are_native(decks):
    prs, r = decks["aws"]
    ws = r.estate.workloads
    txt = slide_text(prs, 4)
    assert str(sum(1 for w in ws if w.os_family == "windows")) in txt
    slide = prs.slides[4]
    assert not slide.shapes._spTree.findall("{http://schemas.openxmlformats.org/markup-compatibility/2006}AlternateContent")      # no stale picture fallback
    charts = [sh.chart for sh in slide.shapes if getattr(sh, "has_chart", False) and sh.has_chart]
    assert len(charts) == 2 and sum(charts[0].plots[0].series[0].values) == len(ws)


def test_the_release_status_is_in_the_speaker_notes_not_on_the_slides(decks):
    prs, _ = decks["aws"]
    assert all("DRAFT" in s.notes_slide.notes_text_frame.text and "run-0001" in s.notes_slide.notes_text_frame.text for s in prs.slides)
    assert "DRAFT" not in all_text(prs)


def test_windows_support_counts_the_versions_in_the_inventory(decks):
    prs, r = decks["aws"]
    t = next(sh for sh in prs.slides[12].shapes if sh.has_table).table
    n = {"old": 0, "2016": 0, "2019": 0, "2022": 0, "2025": 0}
    for w in r.estate.workloads:
        m = re.search(r"(20\d\d)", f"{w.os_label or ''} {w.os_raw or ''}") if w.os_family == "windows" else None
        if m:
            y = int(m.group(1))
            if y <= 2012:
                n["old"] += 1
            elif str(y) in n:
                n[str(y)] += 1
    assert [t.cell(i, 1).text for i in range(1, 6)] == [str(n["old"]), str(n["2016"]), str(n["2019"]), str(n["2022"]), str(n["2025"])]


def test_the_service_exports_from_the_ingram_template_by_default(tmp_path):
    from cva.service import Service
    assert Service.default_template() == str(TPL)


# ---------------------------------------------------------------------------- AWS Transform's own outputs fill the deck (business case deck, analysis workbook with RDS sheets)
FIX = ROOT / "tests" / "fixtures"


def _workbook_with_database(tmp_path):
    """analysis_run2.xlsx plus RDS and S3 sheets in the layout of a real MPA run (synthetic numbers)."""
    import openpyxl
    wb = openpyxl.load_workbook(FIX / "analysis_run2.xlsx")
    hdr = ["source_server_environment", "source_server_role", "source_server_name", "source_provisioned_vcpus", "source_provisioned_memory_gb", "name",
           "is_baseline", "is_recommended", "license_deployment_option", "licensed_vcpu_count", "Region", " annual_compute_cost", " annual_license_cost",
           " annual_storage_cost", " annual_backup_cost", " annual_software_assurance_cost", "total_annual_cost"]
    for title, comp in (("RDS Annual Costs - On-Demand", 1200.0), ("RDS Annual Costs - 1yr NU", 1000.0), ("RDS Annual Costs - 3yr NU", 1000.0)):
        ws = wb.create_sheet(title)
        ws.append(hdr)
        for n, (_, v) in enumerate((("Standard", 8), ("Enterprise", 4)), start=1):
            ws.append(["Prod", "Standalone", f"db{n}.corp.local\\MSSQL15", v, 32, "x", None, "Yes", "LI", 4, "us-east-1", comp, 4000.0, 500.0, 0, 0, comp + 4500.0])
    ws = wb.create_sheet("RDS Server Mappings - 3yr NU")
    ws.append(["server_name", "source_sql_server_edition", "source_environment", "source_provisioned_vcpus", "target_instance_class", "target_instance_vcpus"])
    ws.append(["db1.corp.local\\MSSQL15", "Standard", "Prod", 8, "db.m8i.xlarge", 4])
    ws.append(["db2.corp.local\\MSSQL15", "Enterprise", "Prod", 4, "db.m8i.xlarge", 4])
    ov = wb.create_sheet("RDS Overview")
    for row in (["RDS SQL Server Assessment Overview"], ["Total Annual Cost (Pre-Credits)", 11000.0], ["Total Credits & Discounts", 2750.0],
                ["Annual Software Assurance (SA) savings of $1,000 by migrating to RDS (no longer need to pay SA fees across 12 on-prem CPUs)"]):
        ov.append(row)
    s3 = wb.create_sheet("Object-S3")
    s3.append(["S3 Classes / S3 INT Tiers", "AWS Directional Cost Estimation (Annually)"])
    s3.append(["STANDARD", 100.0])
    s3.append(["Total", 100.0])
    p = tmp_path / "analysis_db.xlsx"
    wb.save(p)
    return p


def test_the_database_sheets_close_the_gap_between_the_per_server_rows_and_awss_totals(tmp_path):
    from cva.transform.analysis import parse_analysis, reconcile
    from cva.transform.results import PricingModel
    a = parse_analysis(_workbook_with_database(tmp_path))
    assert len(a["rds"]["instances"]) == 2 and a["rds"]["credit_rate"] == 0.25 and a["s3_annual"] == 100.0
    keep = 0.75                                                           # AWS's own credit rate, from its overview
    stor = 1000.0
    for slug, comp in (("on_demand", 1200.0), ("three_year_nuri", 1000.0)):
        gross = 2 * (comp + 4000.0 + 500.0)
        assert a["totals"][slug]["rds_compute"] == round(gross * keep - stor, 2)
    t = a["totals"]
    m = [PricingModel(s, s, 0, {"compute": t[s]["compute"] + t[s]["rds_compute"], "network": t[s]["network"], "storage": t["storage"]["total"] + 1000.0 + 100.0}, False)
         for s in ("on_demand", "one_year_nuri", "three_year_nuri")]
    rows = reconcile(m, t)
    assert all(r["ok"] for r in rows) and any("EC2 + RDS" in r["what"] for r in rows) and any("S3 + RDS" in r["what"] for r in rows)
    assert not any("RDS" in n and n.startswith("not read") for n in a["notes"])


def test_volumes_named_server_dash_storage_belong_to_their_server(tmp_path):
    import openpyxl

    from cva.transform.analysis import parse_analysis
    wb = openpyxl.load_workbook(FIX / "analysis_run2.xlsx")
    ws = wb["Block-EBS-Cost-Optimized"]
    hdr = [c.value for c in ws[1]]
    name = next(r[0] for r in wb["Shared Tenancy - On-Demand"].iter_rows(min_row=2, values_only=True) if r[0])
    server = str(wb["Shared Tenancy - On-Demand"].cell(2, 2).value).split(".")[0]
    row = [None] * len(hdr)
    row[hdr.index("Volume Name")] = f"{server}-storage"
    row[hdr.index("Estimated Annual AWS Cost incl. Boot (Cost Optimized)")] = 10.0
    ws.append(row)
    p = tmp_path / "a.xlsx"
    wb.save(p)
    notes = parse_analysis(p)["notes"]
    assert not any(f"{server}-storage" in n for n in notes) and name


def test_the_business_case_deck_is_read_by_its_labels_only(tmp_path):
    from pptx import Presentation

    from cva.transform.business_case import parse_business_case
    prs = Presentation()
    s = prs.slides.add_slide(prs.slide_layouts[5])
    tb = s.shapes.add_textbox(0, 0, 100, 100).text_frame
    tb.text = "Total servers: 224"
    for line in ("Windows Servers in scope: 188", "Linux Servers in scope: 38", "Total SQL Servers: 15", "Block Storage Provisioned (TiB): 89.41 TiB",
                 "$643,857 annualized spend on AWS", "2.0% savings compared to On-Premises cost", "Region: us-east-1"):
        tb.add_paragraph().text = line
    p = tmp_path / "bc.pptx"
    prs.save(p)
    d = parse_business_case(p)
    assert d == {"servers_in_scope": 224, "windows_servers": 188, "linux_servers": 38, "sql_servers": 15, "block_provisioned_tib": 89.41,
                 "annual_spend": 643857, "region": "us-east-1", "vs_on_prem_pct": 2.0}                  # nothing else is invented


def test_the_deck_takes_its_scope_from_awss_business_case_and_database_sheets(tmp_path):
    from cva.transform.analysis import parse_analysis
    aws = load_results(str(AWS), workdir=str(tmp_path / "w"))
    a = parse_analysis(_workbook_with_database(tmp_path))
    aws.rds, aws.s3_annual = a["rds"], a["s3_annual"]
    aws.business_case = {"servers_in_scope": 224, "windows_servers": 188, "linux_servers": 38, "sql_servers": 15, "block_provisioned_tib": 89.41, "block_used_tib": 51.17}
    r = run_pipeline(RunInputs(files=[str(RVT)], as_of="2026-10-20", aws=aws.to_dict()))
    out = tmp_path / "d.pptx"
    assert build_deck(r, {"finalised": None}, {"open": 1}, {}, "run-0001", "wb.xlsx", str(out), template=str(TPL))["problems"] == []
    prs = Presentation(str(out))
    agenda, scope = slide_text(prs, 2).replace("\xa0", " "), slide_text(prs, 4)
    assert "188 Windows VMs" in agenda and "38" in agenda and "89.4 TiB" in agenda
    assert "188" in scope and "15" in scope and "89.4 TiB" in scope and "51.2 TiB" in scope
    t9 = next(sh for sh in prs.slides[8].shapes if sh.has_table).table
    assert t9.cell(1, 3).text == "12" and t9.cell(2, 1).text == "4" and t9.cell(2, 2).text == "4" and t9.cell(2, 3).text == "8"    # AWS's stated 12 CPUs; licensed vCPUs by edition
    assert t9.cell(3, 1).text == "" and "business case" in prs.slides[2].notes_slide.notes_text_frame.text
