"""Adapter: Cloudamize "Download All Reports -> Detailed" workbook.

Verified layout (Cloudamize public sample, 308 servers): Compute is the spine; License, Machine Details,
Infrastructure Summary and Storage add to it, joined on the server name.  Cloudamize's own recommendation
(instance, cost, pricing plan) is kept on the workload for parity only: our engine sizes independently.
"""
from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from ..config import Config
from ..model import Estate, Issue, SourceRef, SqlInstance, Workload
from ..util import is_blank, norm, parse_os, parse_sql, to_float
from .base import find_sheet, load_sheets, read_table, table_issues
from .result import IngestResult

KIND = "cloudamize_all"


def _s(v: Any) -> str | None:
    return None if is_blank(v) else str(v).strip()


def read(path: str | Path, cfg: Config, overrides: dict[str, Any] | None = None,
         collection_days: int | None = None) -> IngestResult:
    p = Path(path)
    fname = p.name
    m = cfg.mapping("cloudamize_all", overrides)
    sheets = load_sheets(p)
    est = Estate(name=p.stem, source_kind=KIND, files=[fname], data_basis="timeseries",
                 collection_days=collection_days)
    res = IngestResult(est)

    s_c = find_sheet(sheets, m["sheets"]["compute"])
    if s_c is None:
        res.issues.append(Issue("ALL-NOSHEET", "error", "Sheet 'Compute' not found in the Cloudamize workbook."))
        return res
    t_c = read_table(fname, s_c, sheets[s_c], m["compute"])
    res.issues += table_issues(t_c, "ALL")
    res.unmapped[s_c] = t_c.unmapped_headers
    res.sections[s_c] = "compute"

    wl: dict[str, Workload] = {}
    dup = 0
    for row in t_c.rows:
        d = row.d
        name = _s(d.get("name"))
        if not name:
            continue
        ref = SourceRef(fname, s_c, row.n)
        if name in wl:
            dup += 1
            wl[name].refs.append(ref)
            continue
        osr = " ".join(x for x in (_s(d.get("os_version")), ) if x) or _s(d.get("os"))
        os_info = parse_os(osr)
        if os_info["family"] in ("unknown", "other") and _s(d.get("os")):
            os_info = parse_os(_s(d.get("os")))
        vcpu = to_float(d.get("vcpu"))
        ram = to_float(d.get("ram_gb"))
        cpu_cur = to_float(d.get("cpu_current"))
        peak_gb = to_float(d.get("ram_peak_gb"))
        w = Workload(
            id=name, name=name, os_raw=osr, os_family=os_info["family"], os_key=os_info["key"],
            os_label=os_info["label"], vcpu=vcpu if vcpu and vcpu > 0 else None,
            ram_gb=ram if ram and ram > 0 else None,
            env=None, machine_type=None,
            cpu_avg=None, cpu_peak=cpu_cur,
            cpu_basis="Cloudamize 'Current CPU Utilization (%)' on the source hardware (percentile not stated); "
                      "its 'Predicted CPU Utilization' is hardware-mapped to the recommended instance",
            ram_peak_gb=peak_gb,
            ram_peak=(100.0 * peak_gb / ram) if (peak_gb is not None and ram) else None,
            source_instance=_s(d.get("source_instance")), source_annual_cost=to_float(d.get("annual_cost")),
            source_pricing_plan=_s(d.get("plan")), refs=[ref],
            extras={"constraint": _s(d.get("constraint")), "tenancy_source": _s(d.get("tenancy")),
                    "rec_vcpu": to_float(d.get("rec_vcpu")), "rec_mem_gb": to_float(d.get("rec_mem")),
                    "cpu_pred": to_float(d.get("cpu_pred")), "on_time_pct": to_float(d.get("on_time")),
                    "asset": _s(d.get("asset")), "region_label": _s(d.get("region")), "ip": _s(d.get("ip"))},
        )
        rv_, pr_ = to_float(d.get("rec_vcpu")), to_float(d.get("cpu_pred"))
        if rv_ and pr_ is not None:
            # Cloudamize maps CPU by hardware benchmark: its predicted utilisation is on the recommended
            # (newer) hardware.  vCPUs of target-generation capacity actually used = rec vCPU x predicted %.
            w.extras["cpu_demand_vcpu"] = rv_ * pr_ / 100.0
        ed, ver = _s(d.get("sql_edition")), _s(d.get("sql_version"))
        if ed or ver:
            parsed = parse_sql(ed, ver)
            w.sql.append(SqlInstance(name=None, edition=parsed["edition"], year=parsed["year"],
                                     raw_version=ver or ed, ref=str(ref)))
        wl[name] = w
    if dup:
        res.issues.append(Issue("ALL-DUPNAME", "warn",
                                f"{dup} repeated server name(s) in the Compute sheet were merged into one workload.",
                                detail={"count": dup}))

    # License sheet: Cloudamize's own licensing assumption per server
    s_l = find_sheet(sheets, m["sheets"]["license"])
    if s_l:
        t_l = read_table(fname, s_l, sheets[s_l], m["license"])
        res.issues += table_issues(t_l, "ALL-LIC")
        for row in t_l.rows:
            w = wl.get(_s(row.d.get("name")) or "")
            if w is None:
                continue
            reason = _s(row.d.get("reason"))
            if reason:
                w.source_license_note = reason
            if _s(row.d.get("plan")) and not w.source_pricing_plan:
                w.source_pricing_plan = _s(row.d.get("plan"))

    # Machine Details: SQL flag, end-of-life flag, VM memory
    s_m = find_sheet(sheets, m["sheets"]["machine"])
    eol_filled = 0
    if s_m:
        t_m = read_table(fname, s_m, sheets[s_m], m["machine"])
        res.issues += table_issues(t_m, "ALL-MD")
        for row in t_m.rows:
            w = wl.get(_s(row.d.get("name")) or "")
            if w is None:
                continue
            runs = _s(row.d.get("runs_sql"))
            w.extras["runs_sql"] = (runs or "").lower() == "yes" if runs else None
            eol = _s(row.d.get("eol"))
            if eol:
                eol_filled += 1
                w.extras["source_eol_flag"] = eol
        res.profile["eol_flag_filled"] = eol_filled

    # Infrastructure Summary: collection span
    s_i = find_sheet(sheets, m["sheets"]["infra"])
    spans: set[str] = set()
    if s_i:
        t_i = read_table(fname, s_i, sheets[s_i], m["infra"])
        for row in t_i.rows:
            sp = _s(row.d.get("span"))
            if sp:
                spans.add(sp)
        days = [int(x) for sp in spans for x in re.findall(r"(\d+)\s*day", sp, re.I)]
        if days and not est.collection_days:
            est.collection_days = min(days)
        if len(spans) > 1:
            res.issues.append(Issue("ALL-SPAN-MIXED", "warn",
                                    f"Collection time spans differ across servers: {sorted(spans)}.",
                                    detail={"spans": sorted(spans)}))

    # Storage per disk
    s_s = find_sheet(sheets, m["sheets"]["storage"])
    if s_s:
        t_s = read_table(fname, s_s, sheets[s_s], m["storage"])
        res.issues += table_issues(t_s, "ALL-ST")
        cap: dict[str, float] = defaultdict(float)
        used: dict[str, float] = defaultdict(float)
        for row in t_s.rows:
            nm = _s(row.d.get("name"))
            if nm:
                cap[nm] += to_float(row.d.get("capacity")) or 0.0
                used[nm] += to_float(row.d.get("used")) or 0.0
        for nm, w in wl.items():
            if nm in cap:
                w.storage_prov_gb = cap[nm] or None
                w.storage_used_gb = used[nm] or None

    est.workloads = list(wl.values())
    est.region_hint = next((w.extras["region_label"] for w in est.workloads if w.extras.get("region_label")), None)
    byol_sa = sum(1 for w in est.workloads if norm(w.source_license_note or "").startswith("license mobility"))
    res.profile.update({"servers": len(est.workloads), "collection_days": est.collection_days,
                        "cloudamize_assumed_sa_servers": byol_sa})
    if byol_sa:
        res.issues.append(Issue(
            "ALL-SA-ASSUMED", "warn",
            f"Cloudamize applied its own 'License Mobility through SA' assumption to {byol_sa} server(s). "
            "That assumes active Software Assurance; it is not evidence.  A licensing input must confirm or "
            "override it before any BYOL figure is relied on.", detail={"servers": byol_sa}))
    return res
