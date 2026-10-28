"""Licensing engine: validates findings, finds gaps and resolves what each workload may use.

This engine never decides eligibility from Microsoft agreements; that is the licensing analyst's (Pilot) or
agent A02's (Release 2) job.  It only (a) checks that what they supplied is internally coherent, (b) lists the
workloads for which no usable finding exists, and (c) applies the findings to pricing.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from ..model import Estate, Issue, LicensingFinding, LicensingSet, Workload
from ..util import norm


@dataclass
class Resolution:
    product: str
    instance: str | None
    model_used: str          # li | byol | free
    tenancy: str             # shared | dedicated
    source: str              # finding | default_li | assumption | illustrative | edition_free | excluded
    reason: str
    cores: float | None = None
    finding: LicensingFinding | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("finding", None)
        return d


FREE_EDITIONS = {"express", "developer"}


def bearing_products(w: Workload) -> list[tuple[str, str | None, str | None]]:
    """(product, instance, edition) tuples that carry a license cost for this workload."""
    out: list[tuple[str, str | None, str | None]] = []
    if w.os_family == "windows":
        out.append(("windows_server", None, None))
    for s in w.sql:
        if (s.edition or "").lower() not in FREE_EDITIONS:
            out.append(("sql_server", s.name, s.edition))
    return out


def validate(ls: LicensingSet, estate: Estate) -> list[Issue]:
    issues: list[Issue] = []
    names = {w.key: w for w in estate.workloads}
    for f in ls.findings:
        w = names.get(norm(f.workload))
        if w is None:
            issues.append(Issue("LIC-ORPHAN", "warn",
                                f"Licensing finding refers to '{f.workload}', which is not in the estate.",
                                workload=f.workload, refs=[f.ref or ""]))
            continue
        if f.product == "sql_server" and not w.sql and not w.extras.get("db_hint"):
            issues.append(Issue("LIC-NOSQL", "info",
                                f"Finding for '{f.workload}' mentions SQL Server but none was detected there.",
                                workload=f.workload, refs=[f.ref or ""]))
        if f.model == "byol":
            if f.product == "sql_server" and f.active_sa is False and f.tenancy != "dedicated":
                issues.append(Issue("LIC-CONFLICT", "error",
                                    f"'{f.workload}': BYOL on shared tenancy needs active Software Assurance, "
                                    "but the finding says SA is not active.  Treated as license-included.",
                                    workload=f.workload, refs=[f.ref or ""]))
            if f.product == "windows_server" and f.tenancy != "dedicated":
                issues.append(Issue("LIC-WIN-BYOL", "error",
                                    f"'{f.workload}': Windows Server has no License Mobility; BYOL needs dedicated "
                                    "tenancy.  Treated as license-included until the tenancy is confirmed.",
                                    workload=f.workload, refs=[f.ref or ""]))
        if f.model == "unknown":
            issues.append(Issue("LIC-UNKNOWN", "warn",
                                f"'{f.workload}' ({f.product}): finding gives no license model.",
                                workload=f.workload, refs=[f.ref or ""]))
    return issues


def resolve(w: Workload, product: str, instance: str | None, edition: str | None, ls: LicensingSet,
            byol_without_input: str) -> Resolution:
    """What license treatment applies under the BYOL scenario for one product on one workload."""
    if product == "sql_server" and (edition or "").lower() in FREE_EDITIONS:
        return Resolution(product, instance, "free", "shared", "edition_free", f"{edition} edition needs no license")
    f = ls.lookup(w.name, product, instance) if ls.mode == "provided" else None
    if f is not None and f.include_in_count is False and product == "sql_server":
        return Resolution(product, instance, "free", "shared", "excluded",
                          "Excluded from the SQL license count by the licensing input", finding=f)
    if f is not None and f.model == "byol":
        if product == "sql_server":
            if f.active_sa is True or f.tenancy == "dedicated":
                ten = "dedicated" if f.tenancy == "dedicated" else "shared"
                return Resolution(product, instance, "byol", ten,
                                  "assumption" if f.origin == "assumption" else "finding",
                                  "BYOL: " + ("active SA (License Mobility)" if f.active_sa else "dedicated tenancy"),
                                  cores=f.cores, finding=f)
        elif product == "windows_server" and f.tenancy == "dedicated":
            return Resolution(product, instance, "byol", "dedicated",
                              "assumption" if f.origin == "assumption" else "finding",
                              "BYOL on dedicated host (pre-Oct-2019 license evidence supplied)", finding=f)
        return Resolution(product, instance, "li", "shared", "default_li",
                          "BYOL finding is not coherent (see exceptions); license-included used", finding=f)
    if f is not None and f.model == "li":
        return Resolution(product, instance, "li", "shared", "finding", "License-included per licensing input",
                          finding=f)
    if byol_without_input == "illustrative_upper_bound":
        ten = "dedicated" if product == "windows_server" else "shared"
        return Resolution(product, instance, "byol", ten, "illustrative",
                          "ILLUSTRATIVE UPPER BOUND: assumes BYOL is eligible; not evidence")
    return Resolution(product, instance, "li", "shared", "default_li",
                      "No licensing finding; license-included kept")


def gaps(estate: Estate, ls: LicensingSet) -> list[dict[str, Any]]:
    """Workloads that carry a license but have no usable finding (the agent's licensing task starts here)."""
    out: list[dict[str, Any]] = []
    for w in estate.workloads:
        for product, inst, edition in bearing_products(w):
            f = ls.lookup(w.name, product, inst) if ls.mode == "provided" else None
            if f is None or f.model == "unknown":
                out.append({"workload": w.name, "product": product, "instance": inst, "edition": edition,
                            "reason": "no finding" if f is None else "finding has no license model",
                            "env": w.env})
        if not w.sql and w.os_family == "windows" and w.extras.get("db_hint"):
            out.append({"workload": w.name, "product": "sql_server", "instance": None, "edition": None,
                        "reason": "database VM on Windows; SQL Server presence and edition cannot be detected "
                                  "from this export", "env": w.env})
    return out


def coverage(estate: Estate, ls: LicensingSet) -> dict[str, Any]:
    """Coverage of the licensing input, overall and per product.  A Windows Server without a BYOL finding
    simply stays license-included (the normal case); a SQL Server without one is a real gap."""
    undetected = "database VM on Windows; SQL Server presence and edition cannot be detected from this export"
    by: dict[str, dict[str, int]] = {"sql_server": {"bearing": 0, "gaps": 0}, "windows_server": {"bearing": 0, "gaps": 0}}
    for w in estate.workloads:
        for product, inst, _ed in bearing_products(w):
            by[product]["bearing"] += 1
            f = ls.lookup(w.name, product, inst) if ls.mode == "provided" else None
            if f is None or f.model == "unknown":
                by[product]["gaps"] += 1
    bearing = sum(v["bearing"] for v in by.values())
    gp = sum(v["gaps"] for v in by.values())
    undet = sum(1 for g in gaps(estate, ls) if g["reason"] == undetected)

    def pct(v: dict[str, int]) -> float:
        return round(100.0 * (v["bearing"] - v["gaps"]) / v["bearing"], 1) if v["bearing"] else 100.0

    return {"bearing": bearing, "covered": bearing - gp, "gaps": gp,
            "pct": round(100.0 * (bearing - gp) / bearing, 1) if bearing else 100.0,
            "sql_pct": pct(by["sql_server"]) if by["sql_server"]["bearing"] else None,      # None: no SQL Server detected (not "100%")
            "sql_unknown": undet, "windows_pct": pct(by["windows_server"]),
            "sql_bearing": by["sql_server"]["bearing"], "sql_gaps": by["sql_server"]["gaps"],
            "windows_bearing": by["windows_server"]["bearing"], "windows_gaps": by["windows_server"]["gaps"],
            "undetected_sql_hints": undet}
