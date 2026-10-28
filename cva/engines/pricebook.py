"""Price book: the one place instance specs and prices are read from.

Swap ``config/rules/pricing_catalog.toml`` for an AWS Price List snapshot with the same shape and nothing
else changes.  While ``meta.placeholder`` is true every output says so.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config import Config


@dataclass(frozen=True)
class Instance:
    name: str
    family: str
    vcpu: int
    mem_gb: float
    linux_hr: float
    burstable: bool
    windows_hr: float | None


class PriceBook:
    def __init__(self, cfg: Config) -> None:
        cat = cfg.catalog
        self.meta: dict[str, Any] = cat.get("meta", {})
        self.lic: dict[str, float] = cat.get("license", {})
        self.mult: dict[str, float] = cat.get("multipliers", {})
        self.storage_gb_month: float = float(cat.get("storage", {}).get("gp3_gb_month", 0.08))
        self.hours: float = float(cfg.section("pricing").get("hours_per_year", 8760))
        self.instances: dict[str, Instance] = {
            i["name"]: Instance(i["name"], i["family"], int(i["vcpu"]), float(i["mem_gb"]),
                                float(i["linux_hr"]), bool(i.get("burstable", False)), i.get("windows_hr"))
            for i in cat.get("instance", [])}

    @property
    def placeholder(self) -> bool:
        return bool(self.meta.get("placeholder", True))

    def get(self, name: str | None) -> Instance | None:
        return self.instances.get(name or "")

    # -- component prices (per hour) -------------------------------------------------
    def windows_license_hr(self, inst: Instance) -> float:
        if inst.windows_hr is not None:
            return max(0.0, inst.windows_hr - inst.linux_hr)
        rate = self.lic["windows_per_vcpu_hr_burstable"] if inst.burstable else self.lic["windows_per_vcpu_hr"]
        return rate * inst.vcpu

    def sql_license_hr(self, inst: Instance, edition: str | None) -> float:
        rate = self.lic.get(f"sql_{(edition or 'standard').lower()}_per_vcpu_hr",
                            self.lic.get("sql_standard_per_vcpu_hr", 0.0))
        return rate * inst.vcpu

    def licensed_total_hr(self, inst: Instance, os_family: str, sql_editions: list[str | None]) -> float:
        """Infra + license-included add-ons: what the cheapest-fit search minimises."""
        t = inst.linux_hr
        if os_family == "windows":
            t += self.windows_license_hr(inst)
        for ed in sql_editions:
            t += self.sql_license_hr(inst, ed)
        return t

    def cheapest_fit(self, vcpu: float, mem_gb: float, allow_burstable: bool, os_family: str,
                     sql_editions: list[str | None]) -> Instance | None:
        best: tuple[float, float, str, Instance] | None = None
        for inst in self.instances.values():
            if inst.vcpu < vcpu or inst.mem_gb < mem_gb or (inst.burstable and not allow_burstable):
                continue
            cost = self.licensed_total_hr(inst, os_family, sql_editions)
            key = (cost, inst.vcpu + inst.mem_gb, inst.name, inst)
            if best is None or key[:3] < best[:3]:
                best = key
        return best[3] if best else None

    def infra_multiplier(self, purchase: str) -> float:
        return {"on_demand": 1.0, "ri_1yr": self.mult.get("ri_1yr_infra", 0.62),
                "ri_3yr": self.mult.get("ri_3yr_infra", 0.40)}[purchase]
