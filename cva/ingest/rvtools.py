"""Adapter: RVTools export (Excel, or a ZIP of CSVs).

Verified against the sample rvtools_sample_export_2.xlsx.  RVTools is a point-in-time snapshot of
vCenter: utilisation is one reading, there is no dependency data and SQL Server cannot be detected.
It is therefore graded "C" and its rightsizing is labelled directional.  It is the fallback for a
pure-VMware estate; Cloudamize is the production source.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..config import Config
from ..model import Estate, Issue, SourceRef, Workload
from ..util import is_blank, parse_os, to_float
from .base import find_sheet, load_sheets, read_table, table_issues
from .result import IngestResult

KIND = "rvtools"


def _s(v: Any) -> str | None:
    return None if is_blank(v) else str(v).strip()


def read(path: str | Path, cfg: Config, overrides: dict[str, Any] | None = None,
         collection_days: int | None = None) -> IngestResult:
    p = Path(path)
    fname = p.name
    m = cfg.mapping("rvtools", overrides)
    sheets = load_sheets(p)
    est = Estate(name=p.stem, source_kind=KIND, files=[fname], data_basis="snapshot",
                 collection_days=collection_days)
    res = IngestResult(est)

    s_i = find_sheet(sheets, m["sheets"]["info"])
    if s_i is None:
        res.issues.append(Issue("RV-NOSHEET", "error", "Sheet 'vInfo' not found in the RVTools export."))
        return res
    t_i = read_table(fname, s_i, sheets[s_i], m["info"])
    res.issues += table_issues(t_i, "RV")
    res.unmapped[s_i] = t_i.unmapped_headers
    res.sections[s_i] = "info"

    cpu_by: dict[str, tuple[float | None, float | None]] = {}
    s_c = find_sheet(sheets, m["sheets"]["cpu"])
    if s_c:
        for row in read_table(fname, s_c, sheets[s_c], m["cpu"]).rows:
            nm = _s(row.d.get("name"))
            if nm:
                cpu_by[nm] = (to_float(row.d.get("max_mhz")), to_float(row.d.get("overall_mhz")))
    mem_by: dict[str, tuple[float | None, float | None, float | None]] = {}
    s_m = find_sheet(sheets, m["sheets"]["mem"])
    if s_m:
        for row in read_table(fname, s_m, sheets[s_m], m["mem"]).rows:
            nm = _s(row.d.get("name"))
            if nm:
                mem_by[nm] = (to_float(row.d.get("size_mib")), to_float(row.d.get("active_mib")),
                              to_float(row.d.get("consumed_mib")))
    host_cores: dict[str, float] = {}
    s_h = find_sheet(sheets, m["sheets"]["host"])
    if s_h:
        for row in read_table(fname, s_h, sheets[s_h], m["host"]).rows:
            nm, c = _s(row.d.get("name")), to_float(row.d.get("cores"))
            if nm and c:
                host_cores[nm] = c

    skipped_templates = 0
    wls: list[Workload] = []
    for row in t_i.rows:
        d = row.d
        name = _s(d.get("name"))
        if not name:
            continue
        if str(d.get("template")).strip().lower() in {"true", "1", "yes"}:
            skipped_templates += 1
            continue
        os_raw = _s(d.get("os_cfg")) or _s(d.get("os_tools"))
        os_info = parse_os(_s(d.get("os_tools")) or os_raw)
        if os_info["family"] in ("unknown", "other") and os_raw:
            os_info = parse_os(os_raw)
        ram_mib = to_float(d.get("ram_mib"))
        ann = _s(d.get("annotation")) or ""
        env_m = re.search(r"env:\s*([A-Za-z]+)", ann, re.I)
        tier = ann.split("|")[0].strip() if ann else None
        mx, ov = cpu_by.get(name, (None, None))
        size, act, cons = mem_by.get(name, (None, None, None))
        cpu_pct = (100.0 * ov / mx) if (mx and ov is not None) else None
        w = Workload(
            id=name, name=name, os_raw=os_raw, os_family=os_info["family"], os_key=os_info["key"],
            os_label=os_info["label"], vcpu=to_float(d.get("vcpu")),
            ram_gb=(ram_mib / 1024.0) if ram_mib else None,
            storage_prov_gb=(to_float(d.get("prov_mib")) or 0) / 1024.0 or None,
            storage_used_gb=(to_float(d.get("used_mib")) or 0) / 1024.0 or None,
            env=env_m.group(1).title() if env_m else None, machine_type="Virtual",
            power_state=_s(d.get("power")), cpu_avg=cpu_pct, cpu_peak=cpu_pct,
            cpu_basis="RVTools snapshot: vCPU 'Overall' / 'Max' MHz at export time (one reading)",
            ram_avg=(100.0 * act / size) if (size and act is not None) else None,
            ram_peak_gb=(cons / 1024.0) if cons is not None else None,
            ram_peak=(100.0 * cons / size) if (size and cons is not None) else None,
            cluster=_s(d.get("cluster")), host=_s(d.get("host")),
            host_cores=host_cores.get(_s(d.get("host")) or ""),
            refs=[SourceRef(fname, s_i, row.n)],
            extras={"tier": tier, "annotation": ann or None,
                    "db_hint": bool(re.search(r"database|sql|oracle|postgres", ann, re.I)),
                    "created": str(d.get("created"))[:10] if d.get("created") else None},
        )
        wls.append(w)
    est.workloads = wls
    if skipped_templates:
        res.issues.append(Issue("RV-TEMPLATE", "info", f"{skipped_templates} VM template(s) were excluded.",
                                detail={"count": skipped_templates}))

    s_hl = find_sheet(sheets, m["sheets"]["health"])
    if s_hl:
        z = [r for r in read_table(fname, s_hl, sheets[s_hl], m["health"]).rows
             if "zombie" in str(r.d.get("mtype") or r.d.get("message") or "").lower()]
        if z:
            res.issues.append(Issue(
                "RV-ZOMBIE", "info",
                f"{len(z)} possible zombie disk file(s) reported by vHealth (storage that is paid for but "
                "attached to no VM).", detail={"count": len(z), "files": [str(r.d.get('name')) for r in z[:10]]}))
    est.notes.append("RVTools is a point-in-time snapshot: no utilisation history, no dependencies, "
                     "and SQL Server cannot be detected.")
    res.profile = {"servers": len(wls), "templates_skipped": skipped_templates}
    return res
