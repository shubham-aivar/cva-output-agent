"""Adapter: Cloudamize "OLA Accelerator Export" (the AWS OLA template).

Verified layout (Cloudamize public sample): sheets "Export Template" (one row per server and
SQL instance), "IO Throughput" (95th percentile), "Storage Details" (per disk) and a hidden list sheet.
The template's licensing columns (Active SQL SA?, License SQL By, AWS SQL Licensing Model,
Include/Exclude from license count) are meant to be filled by a person; when they are, they are
returned as licensing hints for the licensing layer to weigh.
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

from ..config import Config
from ..model import Estate, Issue, LicensingFinding, SourceRef, SqlInstance, Workload
from ..util import is_blank, norm, parse_os, parse_sql, to_bool, to_float
from .base import find_sheet, load_sheets, read_table, table_issues
from .result import IngestResult

KIND = "cloudamize_ola"


def _s(v: Any) -> str | None:
    return None if is_blank(v) else str(v).strip()


def read(path: str | Path, cfg: Config, overrides: dict[str, Any] | None = None,
         collection_days: int | None = None) -> IngestResult:
    p = Path(path)
    fname = p.name
    m = cfg.mapping("cloudamize_ola", overrides)
    sheets = load_sheets(p)
    est = Estate(name=p.stem, source_kind=KIND, files=[fname], data_basis="timeseries",
                 collection_days=collection_days)
    res = IngestResult(est)

    s_main = find_sheet(sheets, m["sheets"]["main"])
    if s_main is None:
        res.issues.append(Issue("OLA-NOSHEET", "error", "Sheet 'Export Template' not found in the OLA export."))
        return res
    t_main = read_table(fname, s_main, sheets[s_main], m["main"])
    res.issues += table_issues(t_main, "OLA")
    res.unmapped[s_main] = t_main.unmapped_headers
    res.sections[s_main] = "main"

    # --- main sheet: one workload per server name; extra rows are extra SQL instances
    wl: dict[str, Workload] = {}
    for row in t_main.rows:
        d = row.d
        name = _s(d.get("name"))
        if not name:
            continue
        ref = SourceRef(fname, s_main, row.n)
        w = wl.get(name)
        if w is None:
            os_info = parse_os(d.get("os"))
            vcpu = to_float(d.get("vcpu"))
            ram_gb = to_float(d.get("ram_gb"))
            w = Workload(
                id=name, name=name, os_raw=_s(d.get("os")), os_family=os_info["family"],
                os_key=os_info["key"], os_label=os_info["label"],
                vcpu=vcpu if vcpu and vcpu > 0 else None, ram_gb=ram_gb if ram_gb and ram_gb > 0 else None,
                storage_used_gb=to_float(d.get("storage_used_gb")),
                machine_type=_s(d.get("machine_type")) or "Virtual", env=_s(d.get("env")) or "Prod",
                cpu_avg=to_float(d.get("cpu_avg")), cpu_peak=to_float(d.get("cpu_peak")),
                cpu_basis="Cloudamize average and peak CPU % (collection span not stated in this export)",
                ram_avg=to_float(d.get("ram_avg")), ram_peak=to_float(d.get("ram_peak")),
                source_instance=_s(d.get("source_instance")),
                cluster=_s(d.get("cluster")), host=_s(d.get("host")),
                host_cores=(to_float(d.get("host_cores")) or None),
                refs=[ref],
                extras={"vcpu_raw": to_float(d.get("vcpu")), "region_label": _s(d.get("region")),
                        "env_defaulted": _s(d.get("env")) is None,
                        "machine_type_defaulted": _s(d.get("machine_type")) is None},
            )
            wl[name] = w
        else:
            w.refs.append(ref)
        edition, inst = _s(d.get("sql_edition")), _s(d.get("sql_instance"))
        if edition or inst:
            parsed = parse_sql(d.get("sql_edition"), d.get("sql_version"))
            si = SqlInstance(name=inst, edition=parsed["edition"] or edition, year=parsed["year"],
                             raw_version=_s(d.get("sql_version")), ref=str(ref))
            w.sql.append(si)
            hint = _licensing_hint(w.name, si, d, str(ref))
            if hint:
                res.licensing_hints.append(hint)

    # RAM utilisation in this template appears as a fraction (0..1) while CPU is a percent.
    rams = [x for w in wl.values() for x in (w.ram_avg, w.ram_peak) if x is not None]
    if rams and max(rams) <= 1.0:
        for w in wl.values():
            if w.ram_avg is not None:
                w.ram_avg *= 100.0
            if w.ram_peak is not None:
                w.ram_peak *= 100.0
        res.issues.append(Issue("OLA-RAMUNIT", "info",
                                "RAM utilisation values are all between 0 and 1, so they were read as fractions "
                                "(x100).  CPU columns in the same sheet are percentages.  Confirm with the owner "
                                "of the export.", detail={"max_raw": max(rams)}))
    for w in wl.values():
        if w.ram_gb and w.ram_peak is not None:
            w.ram_peak_gb = w.ram_gb * w.ram_peak / 100.0

    # --- IO throughput (95th percentile): several rows per server; keep the highest (conservative)
    s_io = find_sheet(sheets, m["sheets"]["io"])
    if s_io:
        t_io = read_table(fname, s_io, sheets[s_io], m["io"])
        res.issues += table_issues(t_io, "OLA-IO")
        agg: dict[str, dict[str, float]] = defaultdict(lambda: {"net": 0.0, "disk": 0.0, "iops": 0.0})
        for row in t_io.rows:
            nm = _s(row.d.get("name"))
            if not nm:
                continue
            for k in ("net", "disk", "iops"):
                v = to_float(row.d.get(k))
                if v is not None:
                    agg[nm][k] = max(agg[nm][k], v)
        extra = 0
        for nm, a in agg.items():
            w = wl.get(nm)
            if w is None:
                extra += 1
                continue
            w.net_mbps_p95, w.disk_mbps_p95, w.disk_iops_p95 = a["net"], a["disk"], a["iops"]
        if extra:
            res.issues.append(Issue("OLA-IO-ORPHAN", "info",
                                    f"{extra} server name(s) in 'IO Throughput' have no row in the main sheet "
                                    "and were ignored.", detail={"count": extra}))

    # --- storage per disk -> sums per server
    s_st = find_sheet(sheets, m["sheets"]["storage"])
    if s_st:
        t_st = read_table(fname, s_st, sheets[s_st], m["storage"])
        res.issues += table_issues(t_st, "OLA-ST")
        cap: dict[str, float] = defaultdict(float)
        used: dict[str, float] = defaultdict(float)
        seen: set[str] = set()
        for row in t_st.rows:
            nm = _s(row.d.get("name"))
            if not nm:
                continue
            seen.add(nm)
            cap[nm] += to_float(row.d.get("capacity")) or 0.0
            used[nm] += to_float(row.d.get("used")) or 0.0
        for nm in seen:
            w = wl.get(nm)
            if w is None:
                continue
            w.storage_prov_gb = cap[nm] or None
            if w.storage_used_gb is None:
                w.storage_used_gb = used[nm] or None

    est.workloads = list(wl.values())
    est.region_hint = next((w.extras.get("region_label") for w in est.workloads if w.extras.get("region_label")), None)
    if not est.collection_days:
        est.notes.append("Collection span is not stated in the OLA export; declare it if known.")
    res.profile = {"servers": len(est.workloads), "rows": len(t_main.rows),
                   "sql_instances": sum(len(w.sql) for w in est.workloads),
                   "licensing_hints": len(res.licensing_hints)}
    return res


def _licensing_hint(workload: str, si: SqlInstance, d: dict[str, Any], ref: str) -> LicensingFinding | None:
    by, sa = _s(d.get("lic_by")), to_bool(d.get("active_sa"))
    model, ovr = _s(d.get("lic_model")), _s(d.get("lic_override"))
    if by is None and sa is None and model is None and ovr is None:
        return None
    nm = norm(model or "")
    lic_model = "byol" if "byol" in nm else "li" if nm.endswith("li") or " li" in nm else "unknown"
    nb = norm(by or "")
    licensed_by = "cores" if "core" in nb else "server_cal" if "cal" in nb else "unknown"
    inc = None
    if ovr:
        inc = False if "exclude" in norm(ovr) else True if "include" in norm(ovr) else None
    return LicensingFinding(
        workload=workload, product="sql_server", instance=si.name, model=lic_model,
        tenancy="shared" if (lic_model == "byol" and sa is True) else "unknown",
        licensed_by=licensed_by, active_sa=sa, include_in_count=inc,
        basis="Filled in the AWS OLA template licensing columns", confidence="medium",
        origin="ola_template", ref=ref)
