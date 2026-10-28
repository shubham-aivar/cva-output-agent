"""Adapter: Cloudamize "Migration Portfolio Assessment (MPA) Export".

UNVERIFIED LAYOUT.  Cloudamize documents the tab names (Servers, Applications, Databases, Server to
application, Application dependency, Server communication) but not the column names, and no sample has
been seen.  This adapter is therefore deliberately generic: it reads the 'Servers' (and 'Databases')
sheets through the aliases in config/mappings/cloudamize_mpa.toml, reports every column it could not map,
and labels the estate so the reviewer knows the layout was assumed.  When a real export arrives,
edit the mapping file (or accept the agent's mapping proposal); no code change should be needed.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config import Config
from ..model import Estate, Issue, SourceRef, SqlInstance, Workload
from ..util import is_blank, parse_os, parse_sql, to_float
from .base import find_sheet, load_sheets, read_table, table_issues
from .result import IngestResult

KIND = "cloudamize_mpa"


def _s(v: Any) -> str | None:
    return None if is_blank(v) else str(v).strip()


def read(path: str | Path, cfg: Config, overrides: dict[str, Any] | None = None,
         collection_days: int | None = None) -> IngestResult:
    p = Path(path)
    fname = p.name
    m = cfg.mapping("cloudamize_mpa", overrides)
    sheets = load_sheets(p)
    est = Estate(name=p.stem, source_kind=KIND, files=[fname], data_basis="inventory",
                 collection_days=collection_days)
    res = IngestResult(est)
    res.issues.append(Issue("MPA-UNVERIFIED", "warn",
                            "The MPA export layout is unverified (no real sample has been seen). Column mapping "
                            "follows assumed AWS MPA import names; review the mapped fields before relying on them."))
    s_sv = find_sheet(sheets, m["sheets"]["servers"])
    if s_sv is None:
        res.issues.append(Issue("MPA-NOSHEET", "error", "Sheet 'Servers' not found in the MPA export."))
        return res
    t = read_table(fname, s_sv, sheets[s_sv], m["servers"])
    res.issues += table_issues(t, "MPA")
    res.unmapped[s_sv] = t.unmapped_headers
    res.sections[s_sv] = "servers"
    wl: dict[str, Workload] = {}
    for row in t.rows:
        d = row.d
        name = _s(d.get("name"))
        if not name or name in wl:
            continue
        osi = parse_os(d.get("os"))
        vcpu, ram = to_float(d.get("vcpu")), to_float(d.get("ram_gb"))
        cpu_a, cpu_p = to_float(d.get("cpu_avg")), to_float(d.get("cpu_peak"))
        w = Workload(id=name, name=name, os_raw=_s(d.get("os")), os_family=osi["family"], os_key=osi["key"],
                     os_label=osi["label"], vcpu=vcpu if vcpu and vcpu > 0 else None,
                     ram_gb=ram if ram and ram > 0 else None, storage_used_gb=to_float(d.get("storage_used_gb")),
                     env=_s(d.get("env")), cpu_avg=cpu_a, cpu_peak=cpu_p,
                     cpu_basis="MPA export CPU utilisation (basis not documented)",
                     ram_avg=to_float(d.get("ram_avg")), ram_peak=to_float(d.get("ram_peak")),
                     refs=[SourceRef(fname, s_sv, row.n)], extras={"mpa_id": _s(d.get("id"))})
        if w.ram_gb and w.ram_peak is not None:
            w.ram_peak_gb = w.ram_gb * w.ram_peak / 100.0
        wl[name] = w
    s_db = find_sheet(sheets, m["sheets"]["databases"])
    if s_db:
        td = read_table(fname, s_db, sheets[s_db], m["databases"])
        for row in td.rows:
            w = wl.get(_s(row.d.get("name")) or "")
            if w is None:
                continue
            parsed = parse_sql(row.d.get("sql_edition"), row.d.get("sql_version"))
            if parsed["edition"] or parsed["year"]:
                w.sql.append(SqlInstance(name=_s(row.d.get("sql_instance")), edition=parsed["edition"],
                                         year=parsed["year"], raw_version=_s(row.d.get("sql_version")),
                                         ref=td.ref(row)))
    est.workloads = list(wl.values())
    est.data_basis = "timeseries" if any(w.cpu_peak is not None for w in est.workloads) else "inventory"
    res.profile = {"servers": len(est.workloads)}
    return res
