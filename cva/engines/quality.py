"""Data-quality engine: turns everything odd about the input into the analyst's exceptions list.

SOW A01 deliverable: "Data-quality exceptions list for analyst review".  Every exception has a stable
code, a severity, the workload and the source rows, so the analyst can go straight to the cell.
"""
from __future__ import annotations

from collections import Counter

from ..config import Config
from ..model import Estate, Issue

SEV_ORDER = {"error": 0, "warn": 1, "info": 2}


def check(estate: Estate, cfg: Config, adapter_issues: list[Issue]) -> list[Issue]:
    q = cfg.section("quality")
    s = cfg.section("sizing")
    out: list[Issue] = list(adapter_issues)

    def add(code: str, sev: str, msg: str, w=None, **detail) -> None:
        out.append(Issue(code, sev, msg, workload=w.name if w else None,
                         refs=[str(r) for r in (w.refs[:3] if w else [])], detail=detail))

    if estate.data_basis == "snapshot":
        add("QS-SNAPSHOT", "warn", "The data is a single point-in-time snapshot (grade C).  Rightsizing is "
            "directional and every sizing figure carries that label.")
    if estate.data_basis == "timeseries" and (estate.collection_days or 0) < int(q.get("snapshot_min_days", 14)):
        add("QS-SHORT-SPAN", "warn",
            f"Collection span is {estate.collection_days or 'not stated'} day(s); AWS guidance is 14 to 30 days "
            "covering a business peak.  Peak-based sizing may be understated.")

    util_vals = Counter(round(w.cpu_peak, 2) for w in estate.workloads if w.cpu_peak is not None)
    if util_vals and len(estate.workloads) >= 10:
        val, n = util_vals.most_common(1)[0]
        if n / len(estate.workloads) >= float(q.get("util_spike_share", 0.3)):
            add("QS-UTIL-SPIKE", "warn",
                f"{n} of {len(estate.workloads)} workloads share the identical CPU utilisation value {val}. "
                "The metric looks capped or defaulted; confirm what it represents before trusting sizing.",
                value=val, count=n)

    eol_zero = [w for w in estate.workloads if w.extras.get("source_eol_flag") is None]
    if estate.source_kind == "cloudamize_all" and len(eol_zero) == len(estate.workloads):
        add("QS-EOL-EMPTY", "info", "The export's 'OS is End of Life' column is empty for every server; "
            "lifecycle findings come from Aivar's own rules table instead.")

    for w in estate.workloads:
        if w.vcpu is None:
            add("QS-VCPU-MISSING", "warn",
                "vCPU is missing" + (" (the export reports 0)" if w.extras.get("vcpu_raw") == 0 else "") +
                "; sizing falls back to the source tool's recommended instance specs." if w.source_instance else
                "; this workload cannot be sized.", w)
        if w.ram_gb is None:
            add("QS-RAM-MISSING", "warn", "RAM is missing.", w)
        if w.os_family in ("unknown", "other"):
            add("QS-OS-UNKNOWN", "warn", f"Operating system not recognised ({w.os_raw!r}).", w)
        elif w.os_key is None:
            add("QS-OS-VERSION", "info", f"Operating system version not recognised ({w.os_raw!r}); "
                "no lifecycle finding possible.", w)
        if estate.data_basis != "inventory" and w.cpu_peak is None and w.cpu_avg is None:
            add("QS-NO-CPU", "warn", "No CPU utilisation; sized on allocation.", w)
        elif w.cpu_peak is not None and w.cpu_peak >= float(s.get("saturated_peak_pct", 99)) and estate.data_basis == "timeseries":
            add("QS-PEAK-SATURATED", "info",
                f"CPU peak is {w.cpu_peak:.0f}% (average "
                f"{'n/a' if w.cpu_avg is None else f'{w.cpu_avg:.0f}%'}): saturation, not a real requirement.", w)
        if estate.data_basis == "timeseries" and w.ram_peak is None and w.ram_peak_gb is None:
            add("QS-NO-MEM", "info", "No memory utilisation; memory sized on allocation.", w)
        if (w.power_state or "").lower() == "poweredoff":
            add("QS-POWERED-OFF", "info", "VM is powered off; confirm whether it is in scope or can be retired.", w)
        if not w.storage_used_gb:
            add("QS-NO-STORAGE", "info", "No used-storage figure; storage cost is excluded for this workload.", w)
        for si in w.sql:
            if si.edition is None:
                add("QS-SQL-EDITION", "warn", f"SQL Server instance {si.name or ''} has no recognisable edition; "
                    "licensing cannot be priced.", w)
            if w.host_cores in (None, 0.0) and w.machine_type in ("Virtual", None):
                add("QS-HOST-CORES", "info", "Hypervisor physical core count is missing or zero; physical-core "
                    "licensing (Enterprise) cannot be evaluated.", w)
    n_env = sum(1 for w in estate.workloads if w.extras.get("env_defaulted"))
    if n_env:
        out.append(Issue("QS-ENV-DEFAULT", "info",
                         f"{n_env} workload(s) had a blank Environment and defaulted to Prod under the template "
                         "rules.  Environment matters for Dev/Test licensing and sizing; fill it in if known.",
                         detail={"count": n_env}))
    return out


def with_licensing(issues: list[Issue], gaps: list[dict], cov: dict) -> list[Issue]:
    out = list(issues)
    for g in gaps:
        out.append(Issue("LI-GAP", "warn" if g["product"] == "sql_server" else "info",
                         f"{g['product'].replace('_', ' ')} on '{g['workload']}': {g['reason']}.",
                         workload=g["workload"], detail=g))
    return out


def finalize(issues: list[Issue]) -> list[Issue]:
    """Stable order and ids (EX-0001...) so a re-run produces the same list."""
    ordered = sorted(issues, key=lambda i: (SEV_ORDER.get(i.severity, 9), i.code, i.workload or "", i.message))
    for n, i in enumerate(ordered, 1):
        i.id = f"EX-{n:04d}"
    return ordered


def summary(issues: list[Issue]) -> dict:
    by_sev = Counter(i.severity for i in issues)
    by_code = Counter(i.code for i in issues)
    return {"total": len(issues), "error": by_sev.get("error", 0), "warn": by_sev.get("warn", 0),
            "info": by_sev.get("info", 0), "by_code": dict(by_code.most_common())}
