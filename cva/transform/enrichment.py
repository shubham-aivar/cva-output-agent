"""Fill AWS Transform's enrichment template with the measurements we actually have.

Verified against the template your account generated (``server_enrichment.csv``): key column ``id``, 55 columns, the
two columns ``p95_cpu_utilization_percentage`` and ``peak_memory_utilization_percentage`` pre-filled with the
defaults 25 and 60.  Matching is by host name (``primary_hostname`` / ``system_device_name``).  We write only the
columns we hold real values for and, by policy, blank the template defaults for those servers."""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Config
from ..model import Estate, Workload
from ..util import norm


def _host(s: str) -> str:
    return norm((s or "").split(".")[0])


@dataclass
class EnrichmentReport:
    template_rows: int = 0
    matched: int = 0
    unmatched_template: list[str] = field(default_factory=list)
    unmatched_workloads: list[str] = field(default_factory=list)
    filled: dict[str, int] = field(default_factory=dict)
    blanked_defaults: int = 0
    skipped_powered_off: int = 0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def _value(w: Workload, field_name: str, spec: dict[str, Any], snapshot: bool, policy: dict[str, Any]) -> Any:
    if field_name == "sql_edition":
        return next((s.edition for s in w.sql if s.edition), None)
    if field_name == "sql_year":
        return next((s.year for s in w.sql if s.year), None)
    if snapshot and policy.get("snapshot_as_average_only", True) and field_name in ("cpu_peak", "ram_peak"):
        return None                          # a single reading is not a peak or a p95
    v = getattr(w, field_name, None)
    if v is None:
        return None
    if "map" in spec:
        return spec["map"].get(str(v).strip().lower(), None)
    return round(float(v), 2) if isinstance(v, (int, float)) else v


def fill_template(template: str | Path, out: str | Path, estate: Estate, cfg: Config) -> EnrichmentReport:
    m = cfg.mapping("aws_transform_enrichment")
    cols, policy = m["columns"], m.get("policy", {})
    snapshot = estate.data_basis == "snapshot"
    with open(template, encoding="utf-8-sig", newline="") as fh:
        rd = csv.DictReader(fh)
        header = list(rd.fieldnames or [])
        rows = list(rd)
    rep = EnrichmentReport(template_rows=len(rows))
    missing_cols = [c for c in cols if c not in header]
    if missing_cols:
        rep.notes.append(f"template lacks column(s) {missing_cols}; they were skipped (layout changed?)")
    by_host = {_host(w.name): w for w in estate.workloads}
    used: set[str] = set()
    for r in rows:
        w = by_host.get(_host(r.get("primary_hostname", ""))) or by_host.get(_host(r.get("system_device_name", "")))
        if w is None:
            rep.unmatched_template.append(r.get("primary_hostname", r.get("id", "?")))
            continue
        used.add(w.key)
        rep.matched += 1
        if policy.get("skip_powered_off", True) and (w.power_state or "").lower() == "poweredoff":
            rep.skipped_powered_off += 1
            continue
        for col, spec in cols.items():
            if col not in header:
                continue
            v = _value(w, spec["field"], spec, snapshot, policy)
            if v is None:
                if policy.get("blank_unknown", True) and r.get(col, "") != "" and col.startswith(("p95_", "peak_", "average_")):
                    r[col] = ""             # the template's default is not a measurement
                    rep.blanked_defaults += 1
                continue
            r[col] = str(v)
            rep.filled[col] = rep.filled.get(col, 0) + 1
    rep.unmatched_workloads = [w.name for w in estate.workloads if w.key not in used]
    if snapshot:
        rep.notes.append("Data is a point-in-time snapshot: sent as averages only, never as peak or p95.")
    if policy.get("blank_unknown", True):
        rep.notes.append("Template defaults were blanked where we hold no measurement. Whether the service accepts blanks "
                         "is untested: set policy.blank_unknown=false to keep the defaults.")
    with open(out, "w", encoding="utf-8", newline="") as fh:
        wr = csv.DictWriter(fh, fieldnames=header, lineterminator="\n")
        wr.writeheader()
        wr.writerows(rows)
    return rep
