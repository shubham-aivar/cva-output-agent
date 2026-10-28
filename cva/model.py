"""Canonical model.

Nothing downstream of the adapters knows about Cloudamize, RVTools or any
licensing source: they all produce these structures.  Adding or changing a
source means touching an adapter and its mapping file, not the engines.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .util import norm


@dataclass
class SourceRef:
    file: str
    sheet: str
    row: int

    def __str__(self) -> str:
        return f"{self.file}:{self.sheet}!{self.row}"


@dataclass
class SqlInstance:
    name: str | None = None
    edition: str | None = None          # Standard | Enterprise | Express | Web | Developer
    year: str | None = None             # marketing version, e.g. "2016"
    raw_version: str | None = None
    ref: str | None = None


@dataclass
class Workload:
    id: str
    name: str
    os_raw: str | None = None
    os_family: str = "unknown"          # windows | linux | other | unknown
    os_key: str | None = None
    os_label: str | None = None
    vcpu: float | None = None
    ram_gb: float | None = None
    storage_used_gb: float | None = None
    storage_prov_gb: float | None = None
    env: str | None = None
    machine_type: str | None = None
    power_state: str | None = None
    # utilisation, always percent 0-100 once normalised
    cpu_avg: float | None = None
    cpu_peak: float | None = None
    cpu_basis: str | None = None        # how the cpu figures were measured (free text, shown to reviewers)
    ram_avg: float | None = None
    ram_peak: float | None = None
    ram_peak_gb: float | None = None
    disk_iops_p95: float | None = None
    disk_mbps_p95: float | None = None
    net_mbps_p95: float | None = None
    # what the source tool itself recommended (used for parity, never trusted blindly)
    source_instance: str | None = None
    source_annual_cost: float | None = None
    source_pricing_plan: str | None = None
    source_license_note: str | None = None
    host: str | None = None
    cluster: str | None = None
    host_cores: float | None = None
    sql: list[SqlInstance] = field(default_factory=list)
    refs: list[SourceRef] = field(default_factory=list)
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def key(self) -> str:
        return norm(self.name)


@dataclass
class Estate:
    name: str
    source_kind: str                    # cloudamize_ola | cloudamize_all | cloudamize_mpa | rvtools | generic
    files: list[str] = field(default_factory=list)
    collection_days: int | None = None
    data_basis: str = "inventory"       # timeseries | snapshot | inventory
    region_hint: str | None = None
    workloads: list[Workload] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def by_key(self) -> dict[str, Workload]:
        return {w.key: w for w in self.workloads}

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["richness"] = richness(self)
        return d


def richness(estate: Estate) -> dict[str, Any]:
    """Grade how much the data can support.  The grade travels with every
    rightsizing figure so a reader knows how firm it is."""
    n = len(estate.workloads) or 1
    with_peak = sum(1 for w in estate.workloads if w.cpu_peak is not None) / n
    with_mem = sum(1 for w in estate.workloads if w.ram_peak is not None or w.ram_peak_gb is not None) / n
    days = estate.collection_days
    reasons: list[str] = []
    if estate.data_basis == "timeseries" and (days or 0) >= 14 and with_peak > 0.8:
        grade, label = "A", "Time-series, 14+ days"
        reasons.append(f"{days} days of utilisation with peaks")
    elif estate.data_basis == "timeseries":
        grade, label = "B", "Time-series, short or partial"
        reasons.append(f"collection span {days or 'unknown'} days; peak coverage {with_peak:.0%}")
    elif estate.data_basis == "snapshot":
        grade, label = "C", "Point-in-time snapshot"
        reasons.append("single snapshot; rightsizing is directional")
    else:
        grade, label = "D", "Inventory only"
        reasons.append("no utilisation data; sizing uses allocation")
    if with_mem < 0.5:
        reasons.append("memory utilisation missing for most workloads")
    return {"grade": grade, "label": label, "reasons": reasons,
            "peak_coverage": round(with_peak, 3), "memory_coverage": round(with_mem, 3)}


@dataclass
class Issue:
    """One data-quality exception destined for the analyst's exceptions list."""
    code: str
    severity: str                       # error | warn | info
    message: str
    workload: str | None = None
    refs: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)
    id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LicensingFinding:
    """What a licensing source says about one workload and product.

    Defined by Aivar from the AWS OLA template's own columns and Microsoft's
    published BYOL rules.  It is a proposal until Ingram or agent A02 replaces
    it (see docs/INTEGRATION.md); adapters map whatever arrives into this shape.
    """
    workload: str
    product: str                        # sql_server | windows_server
    instance: str | None = None
    model: str = "unknown"              # byol | li | unknown
    tenancy: str = "unknown"            # shared | dedicated | any | unknown
    licensed_by: str = "unknown"        # cores | server_cal | unknown
    active_sa: bool | None = None
    include_in_count: bool | None = None
    cores: float | None = None
    basis: str = ""
    confidence: str = "medium"          # high | medium | low
    origin: str = "analyst"             # analyst | a02 | ola_template | assumption
    exceptions: list[str] = field(default_factory=list)
    ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LicensingSet:
    mode: str = "none"                  # none | provided
    version: str = "licensing-findings/0"
    provenance: str = "no licensing input supplied"
    findings: list[LicensingFinding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def lookup(self, workload: str, product: str, instance: str | None = None) -> LicensingFinding | None:
        wk = norm(workload)
        hits = [f for f in self.findings if norm(f.workload) == wk and f.product == product]
        if instance is not None:
            exact = [f for f in hits if norm(f.instance or "") == norm(instance)]
            if exact:
                return exact[-1]
        return hits[-1] if hits else None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Figure:
    id: str
    key: str
    label: str
    value: float | int | str | None
    unit: str
    rule: str
    rule_version: str
    inputs: dict[str, Any] = field(default_factory=dict)
    refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Ledger:
    """Every number that reaches a slide is recorded here with its rule,
    rule version, inputs and source rows (SOW 5.2, traceability)."""

    def __init__(self) -> None:
        self._figs: dict[str, Figure] = {}
        self._by_key: dict[str, str] = {}

    def add(self, key: str, label: str, value: Any, unit: str, rule: str, rule_version: str,
            inputs: dict[str, Any] | None = None, refs: list[str] | None = None) -> Figure:
        if key in self._by_key:  # idempotent per key: re-recording replaces the value
            fid = self._by_key[key]
        else:
            fid = f"F-{len(self._figs) + 1:04d}"
            self._by_key[key] = fid
        fig = Figure(fid, key, label, value, unit, rule, rule_version, inputs or {}, refs or [])
        self._figs[fid] = fig
        return fig

    def get(self, key: str) -> Figure | None:
        fid = self._by_key.get(key)
        return self._figs.get(fid) if fid else None

    def value(self, key: str, default: Any = None) -> Any:
        f = self.get(key)
        return f.value if f else default

    def all(self) -> list[Figure]:
        return list(self._figs.values())

    def to_list(self) -> list[dict[str, Any]]:
        return [f.to_dict() for f in self._figs.values()]
