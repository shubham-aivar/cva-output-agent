"""Rightsizing.  Deterministic code: the same input always gives the same answer, and every
result states the basis it used so a reviewer can see how firm it is."""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

from ..config import Config
from ..model import Estate, Workload, richness
from .pricebook import Instance, PriceBook


@dataclass
class SizingResult:
    workload: str
    ok: bool
    basis: str = ""                    # observed_peak | saturated_average | average_only | snapshot | inventory
    confidence: str = "low"            # high | medium | low
    cur_vcpu: float | None = None
    cur_ram_gb: float | None = None
    req_vcpu: float | None = None
    req_mem_gb: float | None = None
    instance: str | None = None
    rec_vcpu: int | None = None
    rec_mem_gb: float | None = None
    burstable: bool = False
    direction: str = ""               # downsize | same | upsize
    flags: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def size_workload(w: Workload, grade: str, cfg: Config, book: PriceBook) -> SizingResult:
    s = cfg.section("sizing")
    r = SizingResult(workload=w.name, ok=False)
    cur_v, cur_r = w.vcpu, w.ram_gb

    # Specs missing from the export: fall back to the specs of the instance the source tool recommended.
    if (cur_v is None or cur_r is None) and book.get(w.source_instance):
        src = book.get(w.source_instance)
        assert src is not None
        if cur_v is None:
            cur_v = float(src.vcpu)
        if cur_r is None:
            cur_r = src.mem_gb
        r.flags.append("specs_from_source_instance")
        r.notes.append(f"Current vCPU/RAM missing in the export; used {src.name} specs as a stand-in.")
    r.cur_vcpu, r.cur_ram_gb = cur_v, cur_r
    if cur_v is None or cur_r is None:
        r.flags.append("insufficient_data")
        r.notes.append("vCPU or RAM missing and no usable source recommendation; cannot size.")
        return r

    # ---- CPU basis
    tgt = float(s["cpu_target_util"])
    peak, avg = w.cpu_peak, w.cpu_avg
    allow_burst = False
    if grade == "C":
        if peak is None:
            util, r.basis = 100.0, "inventory"
            r.flags.append("no_cpu_data")
        else:
            util, r.basis = peak * float(s["snapshot_headroom"]), "snapshot"
            r.flags.append("snapshot_headroom_applied")
    elif grade == "D":
        util, r.basis = 100.0 * float(s["inventory_headroom"]), "inventory"
        r.flags.append("no_cpu_data")
    else:  # A or B: time-series summary
        demand = w.extras.get("cpu_demand_vcpu")
        if demand is not None:
            util, r.basis = None, "hardware_mapped_demand"
            r.notes.append("CPU demand taken from the source tool's hardware-mapped prediction "
                           f"({demand:.2f} target-generation vCPU in use); source CPU % is on older hardware.")
        elif peak is not None and peak >= float(s["saturated_peak_pct"]) and avg is not None \
                and avg < float(s["saturated_avg_ceiling"]):
            util, r.basis = avg * float(s["avg_headroom"]), "saturated_average"
            r.flags.append("saturated_peak_sized_on_average")
            r.notes.append(f"Peak {peak:.0f}% is saturation (average {avg:.0f}%); sized on average x "
                           f"{s['avg_headroom']}.  Confirm the workload is not CPU-bound.")
        elif peak is not None:
            util, r.basis = peak, "observed_peak"
        elif avg is not None:
            util, r.basis = avg * float(s["avg_headroom"]), "average_only"
            r.flags.append("peak_missing")
        else:
            util, r.basis = 100.0, "inventory"
            r.flags.append("no_cpu_data")
        allow_burst = (avg is not None and avg <= float(s["burstable_avg_max"])
                       and (peak is None or peak < float(s["burstable_peak_max"])))
        if r.basis == "hardware_mapped_demand":
            pred = w.extras.get("cpu_pred")
            allow_burst = pred is not None and pred <= float(s["burstable_avg_max"]) * 2
    perf = float(s.get("cpu_perf_factor", 1.0))
    if r.basis == "hardware_mapped_demand":
        req_v = max(float(s["min_vcpu"]), math.ceil(float(w.extras["cpu_demand_vcpu"]) / (tgt / 100.0) - 1e-9))
    else:
        req_v = max(float(s["min_vcpu"]), math.ceil(cur_v * util / perf / tgt - 1e-9))
    r.req_vcpu = float(req_v)

    # ---- memory basis
    mt = float(s["mem_target_util"])
    if grade == "C":
        used = w.ram_peak_gb if w.ram_peak_gb is not None else None
        used = used * float(s["snapshot_headroom"]) if used is not None else cur_r
    elif w.ram_peak_gb is not None:
        used = w.ram_peak_gb
    elif w.ram_peak is not None:
        used = cur_r * w.ram_peak / 100.0
    elif w.ram_avg is not None:
        used = cur_r * w.ram_avg / 100.0 * float(s["avg_headroom"])
        r.flags.append("memory_from_average")
    else:
        used = cur_r
        r.flags.append("no_memory_data")
    req_m = cur_r if "no_memory_data" in r.flags else used / (mt / 100.0)
    req_m = max(req_m, 1.0)
    r.req_mem_gb = round(req_m, 2)

    editions = [x.edition for x in w.sql]
    inst: Instance | None = book.cheapest_fit(req_v, req_m, allow_burst, w.os_family, editions)
    if inst is None:
        r.flags.append("no_instance_fits")
        r.notes.append("No catalog instance meets the requirement; extend the catalog.")
        return r
    r.ok, r.instance, r.rec_vcpu, r.rec_mem_gb, r.burstable = True, inst.name, inst.vcpu, inst.mem_gb, inst.burstable
    if req_v > cur_v or req_m > cur_r:
        r.direction = "upsize"                 # the workload itself needs more than it has (not: the cheapest fitting instance is bigger)
    elif inst.vcpu < cur_v * 0.7 or inst.mem_gb < cur_r * 0.7:
        r.direction = "downsize"
    else:
        r.direction = "same"

    full = {"observed_peak", "hardware_mapped_demand"}
    if grade == "A" and r.basis in full and "no_memory_data" not in r.flags and "specs_from_source_instance" not in r.flags:
        r.confidence = "high"
    elif grade in ("A", "B") and r.basis != "inventory" and "specs_from_source_instance" not in r.flags:
        r.confidence = "medium"
    else:
        r.confidence = "low"
    if r.basis == "saturated_average":
        r.confidence = "low" if grade != "A" else "medium"
    return r


def size_estate(estate: Estate, cfg: Config, book: PriceBook) -> dict[str, SizingResult]:
    grade = richness(estate)["grade"]
    return {w.name: size_workload(w, grade, cfg, book) for w in estate.workloads}
