"""Scenario pricing.

Each workload is priced under every scenario from its rightsized instance.  The BYOL scenario applies the
licensing findings where they make BYOL eligible and keeps license-included everywhere else, and says how
much of the estate was actually resolved: a BYOL total built on guesses would be worse than no total.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from ..config import Config
from ..model import Estate, LicensingSet
from . import licensing as lic
from .pricebook import PriceBook
from .rightsize import SizingResult


@dataclass
class WorkloadCost:
    workload: str
    scenario: str
    instance: str | None
    infra: float = 0.0
    windows: float = 0.0
    sql: float = 0.0
    storage: float = 0.0
    total: float = 0.0
    treatments: list[dict[str, Any]] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ScenarioResult:
    id: str
    label: str
    status: str                       # computed | partial | not_computable | illustrative
    total: float | None
    infra: float = 0.0
    windows: float = 0.0
    sql: float = 0.0
    storage: float = 0.0
    priced_workloads: int = 0
    excluded_workloads: int = 0
    bearing: int = 0
    byol_applied: int = 0
    unresolved: int = 0
    unresolved_sql: int = 0
    bearing_sql: int = 0
    coverage_pct: float | None = None
    coverage_sql_pct: float | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PricingResult:
    scenarios: list[ScenarioResult]
    costs: dict[str, dict[str, WorkloadCost]]      # scenario id -> workload name -> cost
    placeholder: bool
    catalog_version: str

    def scenario(self, sid: str) -> ScenarioResult | None:
        return next((s for s in self.scenarios if s.id == sid), None)


def price_estate(estate: Estate, sizing: dict[str, SizingResult], ls: LicensingSet, cfg: Config,
                 book: PriceBook) -> PricingResult:
    pset = cfg.section("pricing")
    s_sz = cfg.section("sizing")
    mode_no_input = pset.get("byol_without_input", "not_computable")
    storage_headroom = float(s_sz.get("storage_headroom", 1.2))
    hours = book.hours
    results: list[ScenarioResult] = []
    costs: dict[str, dict[str, WorkloadCost]] = {}

    for sc in cfg.scenarios:
        sid, purchase, lmode = sc["id"], sc["purchase"], sc["license"]
        per: dict[str, WorkloadCost] = {}
        sr = ScenarioResult(sid, sc["label"], "computed", 0.0)
        for w in estate.workloads:
            sz = sizing.get(w.name)
            inst = book.get(sz.instance) if (sz and sz.ok) else None
            if inst is None:
                sr.excluded_workloads += 1
                continue
            wc = WorkloadCost(w.name, sid, inst.name)
            infra_hr = inst.linux_hr * book.infra_multiplier(purchase)
            win_hr = 0.0
            sql_hr = 0.0
            dedicated = False
            for product, instance, edition in lic.bearing_products(w):
                sr.bearing += 1
                if product == "sql_server":
                    sr.bearing_sql += 1
                if lmode == "byol_where_eligible":
                    res = lic.resolve(w, product, instance, edition, ls, mode_no_input)
                else:
                    res = lic.Resolution(product, instance, "li", "shared", "default_li", "license-included scenario")
                wc.treatments.append(res.to_dict())
                if product == "windows_server":
                    win_hr += 0.0 if res.model_used == "byol" else book.windows_license_hr(inst)
                else:
                    sql_hr += 0.0 if res.model_used in ("byol", "free", "excluded") \
                        else book.sql_license_hr(inst, edition)
                if lmode == "byol_where_eligible":
                    if res.model_used == "byol":
                        sr.byol_applied += 1
                        dedicated = dedicated or res.tenancy == "dedicated"
                    elif res.source == "default_li":
                        sr.unresolved += 1
                        if product == "sql_server":
                            sr.unresolved_sql += 1
            if dedicated:
                infra_hr *= book.mult.get("dedicated_host_infra", 1.3)
                wc.flags.append("dedicated_tenancy_surcharge")
            wc.infra = infra_hr * hours
            wc.windows = win_hr * hours
            wc.sql = sql_hr * hours
            stor = w.storage_used_gb
            wc.storage = (stor * storage_headroom * book.storage_gb_month * 12.0) if stor else 0.0
            if not stor:
                wc.flags.append("no_storage_data")
            wc.total = wc.infra + wc.windows + wc.sql + wc.storage
            per[w.name] = wc
            sr.priced_workloads += 1
            sr.infra += wc.infra
            sr.windows += wc.windows
            sr.sql += wc.sql
            sr.storage += wc.storage
            sr.total = (sr.total or 0.0) + wc.total
        if lmode == "byol_where_eligible":
            sr.coverage_pct = round(100.0 * (sr.bearing - sr.unresolved) / sr.bearing, 1) if sr.bearing else 100.0
            sr.coverage_sql_pct = (round(100.0 * (sr.bearing_sql - sr.unresolved_sql) / sr.bearing_sql, 1)
                                   if sr.bearing_sql else None)
            illustrative = any(t.get("source") == "illustrative" for wc in per.values() for t in wc.treatments)
            if sr.bearing == 0:
                sr.notes.append("No license-bearing workloads; BYOL equals On-Demand.")
            elif illustrative:
                sr.status = "illustrative"
                sr.notes.append("ILLUSTRATIVE UPPER BOUND: BYOL assumed eligible for every license-bearing "
                                "workload with no finding.  Not evidence.")
            elif ls.mode == "none" or sr.byol_applied == 0 and sr.unresolved == sr.bearing:
                sr.status, sr.total = "not_computable", None
                sr.notes.append("No usable licensing input: BYOL cannot be computed.  Supply the licensing "
                                "analyst's findings (Pilot) or the A02 output (Release 2).")
            elif sr.unresolved_sql:
                sr.status = "partial"
                sr.notes.append(f"BYOL resolved for {sr.bearing_sql - sr.unresolved_sql} of {sr.bearing_sql} "
                                "SQL Server instance(s); the rest stay license-included.")
            if sr.unresolved - sr.unresolved_sql > 0 and sr.status in ("computed", "partial"):
                sr.notes.append(f"{sr.unresolved - sr.unresolved_sql} Windows Server item(s) have no BYOL finding "
                                "and stay license-included (the normal case: BYOL needs pre-Oct-2019 licenses "
                                "on dedicated hosts).")
        sr.total = None if sr.total is None else round(sr.total, 2)
        results.append(sr)
        costs[sid] = per
    return PricingResult(results, costs, book.placeholder, str(book.meta.get("version", "?")))
