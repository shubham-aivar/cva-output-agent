"""Record every figure that can reach a slide in the ledger with its rule, rule version and inputs."""
from __future__ import annotations

from typing import Any

from ..model import Estate, Issue, Ledger, LicensingSet, richness
from . import quality
from .lifecycle import LifecycleFinding
from .lifecycle import summary as lc_summary
from .pricing import PricingResult
from .rightsize import SizingResult
from .rules import rv


def record_all(led: Ledger, estate: Estate, sizing: dict[str, SizingResult], pricing: PricingResult,
               ls: LicensingSet, lifecycle: list[LifecycleFinding], issues: list[Issue],
               parity_summary: dict[str, Any], lic_cov: dict[str, Any], aws: Any = None, estimate: bool = True,
               aws_sizing: dict[str, SizingResult] | None = None) -> None:
    files = list(estate.files)
    ws = estate.workloads
    n = len(ws)
    led.add("estate.workloads", "Workloads in scope", n, "count", "ES-COUNT", rv("ES-COUNT"), {"files": files}, files)
    led.add("estate.windows", "Windows workloads", sum(1 for w in ws if w.os_family == "windows"), "count",
            "ES-COUNT", rv("ES-COUNT"), {"filter": "os_family=windows"}, files)
    led.add("estate.linux", "Linux workloads", sum(1 for w in ws if w.os_family == "linux"), "count",
            "ES-COUNT", rv("ES-COUNT"), {"filter": "os_family=linux"}, files)
    led.add("estate.sql_instances", "SQL Server instances", sum(len(w.sql) for w in ws), "count", "ES-COUNT",
            rv("ES-COUNT"), {}, files)
    led.add("estate.vcpu", "Current vCPU (where reported)", round(sum(w.vcpu or 0 for w in ws), 1), "vCPU",
            "ES-COUNT", rv("ES-COUNT"), {"missing": sum(1 for w in ws if w.vcpu is None)}, files)
    led.add("estate.ram_gb", "Current RAM (where reported)", round(sum(w.ram_gb or 0 for w in ws), 1), "GB",
            "ES-COUNT", rv("ES-COUNT"), {"missing": sum(1 for w in ws if w.ram_gb is None)}, files)
    led.add("estate.storage_tb", "Used storage", round(sum(w.storage_used_gb or 0 for w in ws) / 1024.0, 2), "TB",
            "ES-COUNT", rv("ES-COUNT"), {"missing": sum(1 for w in ws if not w.storage_used_gb)}, files)
    rich = richness(estate)
    led.add("estate.data_grade", "Data richness grade", rich["grade"], "grade", "ES-COUNT", rv("ES-COUNT"),
            {"label": rich["label"], "reasons": rich["reasons"]}, files)

    def sizing_block(pfx: str, sz: dict[str, SizingResult], r_pick: str, r_cpu: str, r_mem: str, inp: dict[str, Any]) -> None:
        okz = [s for s in sz.values() if s.ok]
        led.add(f"{pfx}.sized", "Workloads sized", len(okz), "count", r_pick, rv(r_pick), {"of": n, **inp})
        led.add(f"{pfx}.vcpu_rec", "Recommended vCPU", sum(s.rec_vcpu or 0 for s in okz), "vCPU", r_cpu, rv(r_cpu), inp)
        led.add(f"{pfx}.vcpu_cur", "Current vCPU of sized workloads", round(sum(s.cur_vcpu or 0 for s in okz), 1), "vCPU", r_cpu, rv(r_cpu), inp)
        led.add(f"{pfx}.mem_rec", "Recommended memory", round(sum(s.rec_mem_gb or 0 for s in okz), 1), "GB", r_mem, rv(r_mem), inp)
        led.add(f"{pfx}.mem_cur", "Current memory of sized workloads", round(sum(s.cur_ram_gb or 0 for s in okz), 1), "GB", r_mem, rv(r_mem), inp)
        for d in ("downsize", "same", "upsize"):
            led.add(f"{pfx}.{d}", f"Workloads: {d}", sum(1 for s in okz if s.direction == d), "count", r_pick, rv(r_pick), inp)
        for c in ("high", "medium", "low"):
            led.add(f"{pfx}.conf_{c}", f"Sizing confidence {c}", sum(1 for s in okz if s.confidence == c), "count", r_pick, rv(r_pick), inp)
        led.add(f"{pfx}.unsized", "Workloads that could not be sized", n - len(okz), "count", r_pick, rv(r_pick), inp)

    if aws_sizing:                      # AWS Transform's per-server results are the sizing; A01's own estimate (if run) is kept apart as a cross-check
        sizing_block("sizing", aws_sizing, "AT-SIZING", "AT-SIZING", "AT-SIZING",
                     {"source": "AWS Transform analysis workbook", "scenario": getattr(aws, "scenario_name", ""), "job": getattr(aws, "job", {})})
        if estimate:
            sizing_block("a01.sizing", sizing, "RS-PICK", "RS-CPU", "RS-MEM", {"source": "A01 offline estimate"})
    elif estimate:
        sizing_block("sizing", sizing, "RS-PICK", "RS-CPU", "RS-MEM", {})

    pre = "scenario" if aws is None else "a01"      # with an AWS Transform assessment, A01's own numbers move to "a01.*"
    for sc in pricing.scenarios:
        base = f"{pre}.{sc.id}"
        inp = {"status": sc.status, "priced": sc.priced_workloads, "catalog": pricing.catalog_version,
               "placeholder_prices": pricing.placeholder}
        led.add(f"{base}.total", f"{sc.label}: annual total", sc.total, "USD/yr", "PR-SCEN", rv("PR-SCEN"), inp)
        led.add(f"{base}.infra", f"{sc.label}: infrastructure", round(sc.infra, 2), "USD/yr", "PR-INFRA", rv("PR-INFRA"), inp)
        led.add(f"{base}.windows", f"{sc.label}: Windows licenses", round(sc.windows, 2), "USD/yr", "PR-LIC", rv("PR-LIC"), inp)
        led.add(f"{base}.sql", f"{sc.label}: SQL Server licenses", round(sc.sql, 2), "USD/yr", "PR-LIC", rv("PR-LIC"), inp)
        led.add(f"{base}.storage", f"{sc.label}: storage", round(sc.storage, 2), "USD/yr", "PR-STORAGE", rv("PR-STORAGE"), inp)
        if sc.coverage_pct is not None:
            led.add(f"{base}.coverage", f"{sc.label}: licensing coverage", sc.coverage_pct, "%", "PR-LIC", rv("PR-LIC"),
                    {"bearing": sc.bearing, "unresolved": sc.unresolved, "byol_applied": sc.byol_applied})
    if aws is not None:
        from ..transform.results import SLUG_TO_ID
        ainp = {"scenario": aws.scenario_name, "timestamp": aws.timestamp, "job": aws.job, "files": list(aws.files)}
        for slug, sid in SLUG_TO_ID.items():
            m = aws.model(slug)
            if m is None:
                continue
            led.add(f"scenario.{sid}.total", f"{m.label}: annual total (AWS Transform)", round(m.total, 2), "USD/yr", "AT-IMPORT",
                    rv("AT-IMPORT"), {**ainp, "recommended": m.recommended})
            for cat, val in m.by_category.items():
                led.add(f"scenario.{sid}.{cat}", f"{m.label}: {cat} (AWS Transform)", round(val, 2), "USD/yr", "AT-IMPORT", rv("AT-IMPORT"), ainp)
        byol = pricing.scenario("byol")
        if byol is not None and byol.total is not None and byol.status in ("computed", "partial", "illustrative"):
            led.add("scenario.byol.total", "BYOL: annual total (A01 estimate, not AWS Transform)", byol.total, "USD/yr", "PR-SCEN",
                    rv("PR-SCEN"), {"status": byol.status, "placeholder_prices": pricing.placeholder})
        if aws.on_prem_total is not None:
            led.add("aws.on_prem_total", "On-premises cost baseline (AWS Transform default assumptions)", round(aws.on_prem_total, 2), "USD",
                    "AT-IMPORT", rv("AT-IMPORT"), {**ainp, "by_category": aws.on_prem_by_category})
        led.add("aws.servers_in_scope", "Servers in the AWS Transform assessment", aws.servers.get("in_scope") or 0, "count", "AT-IMPORT",
                rv("AT-IMPORT"), ainp)
        led.add("aws.servers_excluded", "Servers excluded by AWS Transform", aws.servers.get("excluded") or 0, "count", "AT-IMPORT",
                rv("AT-IMPORT"), {**ainp, "reasons": aws.servers.get("reasons")})
        led.add("aws.warnings", "AWS Transform warning rows", aws.warning_rows, "count", "AT-IMPORT", rv("AT-IMPORT"), ainp)
    od_t, r3_t = led.value("scenario.on_demand.total"), led.value("scenario.ri_3yr.total")
    if od_t and r3_t:
        led.add("scenario.ri3_saving_pct", "3-year RI saving vs On-Demand", round(100 * (od_t - r3_t) / od_t, 1),
                "%", "PR-SCEN", rv("PR-SCEN"), {"on_demand": od_t, "ri_3yr": r3_t})

    led.add("licensing.bearing", "License-bearing items", lic_cov["bearing"], "count", "LI-GAP", rv("LI-GAP"))
    led.add("licensing.gaps", "Items without a licensing finding", lic_cov["gaps"], "count", "LI-GAP", rv("LI-GAP"))
    led.add("licensing.coverage", "Licensing input coverage (all products)", lic_cov["pct"], "%", "LI-GAP", rv("LI-GAP"),
            {"mode": ls.mode, "provenance": ls.provenance})
    led.add("licensing.coverage_sql", "SQL Server licensing coverage", lic_cov["sql_pct"], "%", "LI-GAP", rv("LI-GAP"),
            {"bearing": lic_cov["sql_bearing"], "gaps": lic_cov["sql_gaps"]})
    led.add("licensing.coverage_windows", "Windows Server licensing coverage", lic_cov["windows_pct"], "%", "LI-GAP",
            rv("LI-GAP"), {"bearing": lic_cov["windows_bearing"], "gaps": lic_cov["windows_gaps"]})

    s = lc_summary(lifecycle)
    unver = any(not f.verified for f in lifecycle)
    led.add("lifecycle.eos", "Past end of support (OS or SQL)", s["end_of_support"], "count", "LC-EOS", rv("LC-EOS"),
            {"unverified_rules": unver})
    led.add("lifecycle.approaching", "Approaching end of support", s["approaching"], "count", "LC-EOS", rv("LC-EOS"),
            {"unverified_rules": unver})
    led.add("lifecycle.at_risk", "Workloads at lifecycle risk", s["workloads_at_risk"], "count", "LC-EOS", rv("LC-EOS"),
            {"unverified_rules": unver})

    q = quality.summary(issues)
    led.add("quality.total", "Data-quality exceptions", q["total"], "count", "ES-COUNT", rv("ES-COUNT"), q["by_code"])
    for sev in ("error", "warn", "info"):
        led.add(f"quality.{sev}", f"Exceptions ({sev})", q[sev], "count", "ES-COUNT", rv("ES-COUNT"))
    led.add("parity.compared", "Workloads compared with a reference", parity_summary.get("compared", 0), "count",
            "PA-COST", rv("PA-COST"), parity_summary)
    led.add("parity.outliers", "Parity outliers", parity_summary.get("outliers", 0), "count", "PA-COST", rv("PA-COST"),
            parity_summary)
    tot = parity_summary.get("totals")
    if tot:
        for k in ("aws_vs_a01_pct", "aws_vs_cloudamize_pct"):
            if tot.get(k) is not None:
                led.add(f"parity.totals.{k}", f"Compute cost: {k.replace('_pct', '').replace('_', ' ')} (%)", tot[k], "%", "PA-COST",
                        rv("PA-COST"), tot)
