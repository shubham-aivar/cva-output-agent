from datetime import date

from cva.engines import licensing as lic
from cva.engines import lifecycle, parity, quality
from cva.engines.pricebook import PriceBook
from cva.engines.pricing import price_estate
from cva.engines.rightsize import size_workload
from cva.ingest import cloudamize_all
from cva.model import Estate, LicensingFinding, LicensingSet, SqlInstance, Workload
from tests.conftest import FIX


def W(**kw):
    base = dict(id="w", name="w", os_family="windows", os_key="windows_server_2019", vcpu=4, ram_gb=16,
                cpu_avg=20, cpu_peak=40, ram_peak_gb=6, storage_used_gb=100)
    base.update(kw)
    return Workload(**base)


def test_price_book_reproduces_cloudamize_windows_cost(cfg):
    pb = PriceBook(cfg)
    i = pb.get("t3a.large")
    assert round(pb.licensed_total_hr(i, "windows", []) * 8760, 2) == 900.53      # Cloudamize sample value
    assert round(pb.licensed_total_hr(pb.get("r7i.large"), "windows", []), 4) == 0.2243


def test_price_book_fidelity_on_real_sample(cfg):
    pb = PriceBook(cfg)
    e = cloudamize_all.read(FIX / "cloudamize_all_trimmed_public_sample.xlsx", cfg).estate
    errs = []
    for w in e.workloads:
        c = parity._compute_cost(pb, w.source_instance, w.os_family, [s.edition for s in w.sql], "byo" in (w.source_pricing_plan or "").lower())
        if c and w.source_annual_cost:
            errs.append(abs(c - w.source_annual_cost) / w.source_annual_cost)
    assert sorted(errs)[len(errs) // 2] < 0.01 and sum(1 for x in errs if x < 0.03) / len(errs) > 0.9


def test_sizing_observed_peak(cfg):
    r = size_workload(W(), "A", cfg, PriceBook(cfg))
    assert r.ok and r.basis == "observed_peak" and r.confidence == "high" and r.rec_vcpu >= 2


def test_sizing_saturated_peak_uses_average_and_is_low_confidence_on_b(cfg):
    r = size_workload(W(cpu_peak=100, cpu_avg=10), "B", cfg, PriceBook(cfg))
    assert r.basis == "saturated_average" and "saturated_peak_sized_on_average" in r.flags and r.confidence == "low"


def test_sizing_snapshot_headroom_and_grade_c(cfg):
    r = size_workload(W(cpu_avg=30, cpu_peak=30, ram_peak_gb=8), "C", cfg, PriceBook(cfg))
    assert r.basis == "snapshot" and "snapshot_headroom_applied" in r.flags and r.confidence == "low"


def test_sizing_hardware_mapped_demand(cfg):
    w = W(cpu_peak=93, extras={"cpu_demand_vcpu": 1.2, "cpu_pred": 60})
    r = size_workload(w, "A", cfg, PriceBook(cfg))
    assert r.basis == "hardware_mapped_demand" and r.req_vcpu == 2          # ceil(1.2 / 0.8)


def test_sizing_falls_back_to_source_instance_specs(cfg):
    r = size_workload(W(vcpu=None, source_instance="t3a.large"), "A", cfg, PriceBook(cfg))
    assert r.ok and "specs_from_source_instance" in r.flags and r.confidence != "high"


def test_sizing_without_any_specs_cannot_size(cfg):
    r = size_workload(W(vcpu=None, ram_gb=None), "A", cfg, PriceBook(cfg))
    assert not r.ok and "insufficient_data" in r.flags


def _estate(*ws):
    return Estate("t", "cloudamize_ola", workloads=list(ws), data_basis="timeseries", collection_days=14)


def _price(est, ls, cfg, patch=None):
    from cva.config import load_config
    c = load_config(settings_patch=patch) if patch else cfg
    pb = PriceBook(c)
    sz = {w.name: size_workload(w, "A", c, pb) for w in est.workloads}
    return price_estate(est, sz, ls, c, pb)


def test_byol_not_computable_without_input(cfg):
    w = W(sql=[SqlInstance("M", "Standard", "2019")])
    p = _price(_estate(w), LicensingSet(), cfg)
    b = p.scenario("byol")
    assert b.status == "not_computable" and b.total is None and p.scenario("on_demand").total > 0


def test_byol_computed_and_cheaper_with_coherent_finding(cfg):
    w = W(sql=[SqlInstance("M", "Standard", "2019")])
    ls = LicensingSet("provided", findings=[LicensingFinding("w", "sql_server", "M", "byol", "shared", "cores", True)])
    p = _price(_estate(w), ls, cfg)
    assert p.scenario("byol").status in ("computed", "partial") and p.scenario("byol").total < p.scenario("on_demand").total
    assert p.scenario("byol").sql == 0


def test_incoherent_byol_finding_falls_back_to_license_included(cfg):
    w = W(sql=[SqlInstance("M", "Standard", "2019")])
    ls = LicensingSet("provided", findings=[LicensingFinding("w", "sql_server", "M", "byol", "shared", "cores", False)])
    p = _price(_estate(w), ls, cfg)
    assert p.scenario("byol").sql == p.scenario("on_demand").sql
    assert any(i.code == "LIC-CONFLICT" for i in lic.validate(ls, _estate(w)))


def test_windows_byol_needs_dedicated_tenancy(cfg):
    w = W()
    bad = LicensingSet("provided", findings=[LicensingFinding("w", "windows_server", None, "byol", "shared")])
    ok = LicensingSet("provided", findings=[LicensingFinding("w", "windows_server", None, "byol", "dedicated")])
    assert any(i.code == "LIC-WIN-BYOL" for i in lic.validate(bad, _estate(w)))
    assert _price(_estate(w), ok, cfg).scenario("byol").windows == 0
    assert _price(_estate(w), bad, cfg).scenario("byol").windows > 0


def test_illustrative_upper_bound_is_labelled(cfg):
    w = W(sql=[SqlInstance("M", "Standard", "2019")])
    p = _price(_estate(w), LicensingSet(), cfg, {"pricing": {"byol_without_input": "illustrative_upper_bound"}})
    b = p.scenario("byol")
    assert b.status == "illustrative" and b.total is not None and any("ILLUSTRATIVE" in n for n in b.notes)


def test_free_sql_editions_cost_nothing(cfg):
    w = W(sql=[SqlInstance("E", "Express", "2019"), SqlInstance("D", "Developer", "2019")])
    assert _price(_estate(w), LicensingSet(), cfg).scenario("on_demand").sql == 0


def test_reserved_instances_discount_infra_not_licenses(cfg):
    w = W()
    p = _price(_estate(w), LicensingSet(), cfg)
    od, r3 = p.scenario("on_demand"), p.scenario("ri_3yr")
    assert r3.infra < od.infra and r3.windows == od.windows and r3.total < od.total


def test_licensing_gaps_and_coverage_split_by_product(cfg):
    w = W(sql=[SqlInstance("M", "Standard", "2019")])
    ls = LicensingSet("provided", findings=[LicensingFinding("w", "sql_server", "M", "li")])
    c = lic.coverage(_estate(w), ls)
    assert c["sql_pct"] == 100 and c["windows_pct"] == 0 and c["gaps"] == 1
    assert {g["product"] for g in lic.gaps(_estate(w), ls)} == {"windows_server"}


def test_orphan_finding_reported():
    ls = LicensingSet("provided", findings=[LicensingFinding("nobody", "sql_server", None, "byol")])
    assert any(i.code == "LIC-ORPHAN" for i in lic.validate(ls, _estate(W())))


def test_lifecycle_statuses(cfg):
    ws = [W(name="old", os_key="windows_server_2008_r2"), W(name="soon", os_key="windows_server_2016"),
          W(name="ok", os_key="windows_server_2022")]
    f = {x.workload: x for x in lifecycle.evaluate(_estate(*ws), cfg, date(2026, 10, 20))}
    assert f["old"].status == "end_of_support" and f["soon"].status == "approaching" and f["ok"].status == "supported"
    assert not f["old"].verified                                             # every rule is flagged unverified


def test_quality_flags(cfg):
    ws = [W(name="a", vcpu=None, source_instance="t3a.large"), W(name="b", cpu_peak=100, cpu_avg=5, power_state="poweredOff")]
    codes = {i.code for i in quality.check(_estate(*ws), cfg, [])}
    assert {"QS-VCPU-MISSING", "QS-PEAK-SATURATED", "QS-POWERED-OFF"} <= codes


def test_quality_ids_are_stable():
    from cva.model import Issue
    a = quality.finalize([Issue("B", "warn", "m2"), Issue("A", "error", "m1")])
    assert [i.id for i in a] == ["EX-0001", "EX-0002"] and a[0].code == "A"


def test_parity_is_like_for_like_on_sql(cfg):
    pb = PriceBook(cfg)
    w = W(source_instance="t3a.large", source_pricing_plan="On-demand BYO MS SQL License", sql=[SqlInstance("M", "Standard", "2019")])
    sz = {w.name: size_workload(w, "A", cfg, pb)}
    rows, _ = parity.compare(_estate(w), sz, cfg, pb)
    assert rows[0].reference == "cloudamize" and rows[0].status != "not_comparable"
