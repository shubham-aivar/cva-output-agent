"""Adapters against the REAL public sample layouts (Cloudamize, RVTools) and mapping behaviour."""
import pytest

from cva.ingest import cloudamize_all, cloudamize_ola, rvtools
from cva.ingest.base import load_sheets, read_table
from cva.ingest.detect import detect_kind, profile_file, read_any
from cva.ingest.licensing_in import load_licensing
from cva.util import parse_os, parse_sql, to_float
from tests.conftest import FIX, SYN


def test_parse_os_variants():
    assert parse_os("Microsoft Windows Server 2012 R2 Standard 6.3.9600")["key"] == "windows_server_2012_r2"
    assert parse_os("Microsoft Windows Server 2022 (64-bit)")["key"] == "windows_server_2022"
    assert parse_os("Red Hat Enterprise Linux 8.8 (Ootpa)")["key"] == "rhel_8"
    assert parse_os("Red Hat Enterprise Linux Server release 6.10 (Santiago)")["key"] == "rhel_6"
    assert parse_os("CentOS Linux release 7.9.2009 (Core)")["key"] == "centos_7"
    assert parse_os("Ubuntu 22.04.4 LTS")["key"] == "ubuntu_22_04"
    assert parse_os("SUSE Linux Enterprise 12 (64-bit)")["key"] == "sles_12"
    assert parse_os(None)["family"] == "unknown"
    assert parse_os("Solaris 11")["family"] == "other"


def test_parse_sql_and_numbers():
    assert parse_sql("Standard", "13.3.6300.2") == {"edition": "Standard", "year": "2016"}
    assert parse_sql("-", "SQL Server 2022 16.0.1000.6 (RTM) Enterprise Edition: Core-based Licensing")["edition"] == "Enterprise"
    assert to_float("1,234.5") == 1234.5 and to_float("-") is None and to_float("N.A.") is None and to_float("$660k") == 660000.0


def test_ola_real_sample():
    r = cloudamize_ola.read(FIX / "cloudamize_ola_public_sample.xlsx", __import__("cva.config", fromlist=["x"]).load_config())
    e = r.estate
    assert len(e.workloads) == 33 and r.profile["rows"] == 34            # one server has two SQL instance rows
    assert sum(len(w.sql) for w in e.workloads) == 2
    assert {i.code for i in r.issues} >= {"OLA-RAMUNIT", "OLA-IO-ORPHAN"}
    w = e.by_key()["cqzzqlcbfmw"]
    assert w.os_key == "windows_server_2016" and {s.edition for s in w.sql} == {"Standard", "Express"}
    assert w.disk_iops_p95 and w.storage_used_gb
    assert all(0 <= (x.ram_avg or 0) <= 100 for x in e.workloads)      # fractions were converted to percent
    assert r.licensing_hints == []                                      # the template's licensing columns are blank


def test_all_reports_real_sample(cfg):
    r = cloudamize_all.read(FIX / "cloudamize_all_trimmed_public_sample.xlsx", cfg)
    e = r.estate
    assert len(e.workloads) == 40 and e.collection_days == 14 and e.data_basis == "timeseries"
    assert any(i.code == "ALL-SA-ASSUMED" for i in r.issues)           # Cloudamize assumed Software Assurance
    w = next(x for x in e.workloads if x.sql)
    assert w.source_license_note and "License Mobility" in w.source_license_note
    assert all("cpu_demand_vcpu" in x.extras for x in e.workloads)


def test_rvtools_real_sample(cfg):
    r = rvtools.read(FIX / "rvtools_sample.xlsx", cfg)
    e = r.estate
    assert len(e.workloads) == 57 and e.data_basis == "snapshot"
    assert sum(1 for w in e.workloads if w.os_family == "windows") == 30
    assert any(i.code == "RV-ZOMBIE" and i.detail["count"] == 6 for i in r.issues)
    assert sum(1 for w in e.workloads if w.extras["db_hint"]) == 9
    assert all(w.sql == [] for w in e.workloads)                        # RVTools cannot see SQL Server


def test_unrecognised_workbook_is_refused_with_guidance(cfg, tmp_path):
    from openpyxl import Workbook
    wb = Workbook(); wb.active.title = "Random"; wb.active.append(["a", "b"]); p = tmp_path / "x.xlsx"; wb.save(p)
    assert detect_kind(p) is None
    with pytest.raises(ValueError, match="Could not recognise"):
        read_any(p, cfg)


def test_detect_and_profile(cfg):
    assert detect_kind(FIX / "cloudamize_ola_public_sample.xlsx") == "cloudamize_ola"
    assert detect_kind(FIX / "cloudamize_all_trimmed_public_sample.xlsx") == "cloudamize_all"
    assert detect_kind(FIX / "rvtools_sample.xlsx") == "rvtools"
    p = profile_file(FIX / "rvtools_sample.xlsx")
    assert "vInfo" in p["sheets"] and p["detected"] == "rvtools"


def test_missing_required_column_is_reported_not_skipped(cfg):
    sheets = load_sheets(FIX / "cloudamize_ola_public_sample.xlsx")
    spec = {"name": ["On-Prem Server Name"], "bogus": ["No Such Column"], "required": ["name", "bogus"]}
    t = read_table("f", "Export Template", sheets["Export Template"], spec)
    assert t.missing_required == ["bogus"] and t.rows


def test_accepted_mapping_extends_aliases(cfg):
    base = cfg.mapping("cloudamize_ola")["main"]["cpu_peak"]
    merged = cfg.mapping("cloudamize_ola", {"cloudamize_ola": {"main": {"cpu_peak": ["Peak CPU %"]}}})["main"]["cpu_peak"]
    assert merged == base + ["Peak CPU %"]


def test_licensing_inputs(cfg):
    ls, iss = load_licensing(None, cfg)
    assert ls.mode == "none" and not iss
    ls, iss = load_licensing(SYN / "licensing_a02_estate_b.json", cfg)
    assert ls.mode == "provided" and ls.findings and not any(i.code == "LIC-SCHEMA" for i in iss)
    ls, iss = load_licensing(SYN / "licensing_analyst_estate_a.xlsx", cfg)
    assert ls.mode == "provided" and any(f.origin == "analyst" for f in ls.findings)


def test_unrecognised_enum_values_become_unknown(cfg, tmp_path):
    import json
    p = tmp_path / "l.json"
    p.write_text(json.dumps({"schema": "licensing-findings/0", "findings": [{"workload": "X", "model": "maybe", "product": "sql"}]}))
    ls, iss = load_licensing(p, cfg)
    assert ls.findings[0].model == "unknown" and any(i.code == "LIC-BADVALUE" for i in iss)
