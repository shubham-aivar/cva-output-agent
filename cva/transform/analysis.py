"""Per-server results of an AWS Transform assessment, read from its analysis workbook (``analysis_<timestamp>.xlsx``, inside the
``<Scenario>_<timestamp>.zip`` deliverables).  Layout VERIFIED against a real workbook (RVTools sample, October 2026):

* one sheet per pricing model, ``<Tenancy> Tenancy - On-Demand`` / ``- 1yr NU`` / ``- 3yr NU``, ONE ROW PER IN-SCOPE SERVER, the same 28 columns on each:
  Server Id (``server-<hex>``), Server Name (the DNS name), Total Cores, RAM (GB), OS, the utilization AWS used, ``EC2 Instance Recommended``, EC2 Total Cores,
  EC2 RAM (GB), AWS Region and the annual cost split into license, EC2 (excluding license) and network.  Per server the cost is
  ``Annualized Total = License Only + EC2 Excl. License + Network``;
* the recommended instance can differ between pricing models (7 of 55 servers did), so everything is kept PER MODEL;
* ``Block-EBS-Cost-Optimized``: one row per volume (``SERVER:disk.vmdk``) with its annual cost including boot; they add up to the storage total;
* ``Assessment Issues`` (one row per resource and reason) and ``Excluded Resources``.

Sheets are found by their column headers, never by position.  Nothing is guessed: a column that is missing is reported, not assumed.
Every figure is kept as AWS gave it; the only thing A01 adds is the match to its own workloads (by the first label of the DNS name)."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

MODEL_SHEETS = (                      # sheet-name pattern -> the pricing-model slug used in aggregated_output.json
    (re.compile(r"on[\s-]*demand", re.I), "on_demand"),
    (re.compile(r"1\s*-?\s*y(ea)?r.*\b(nu|no\s*upfront)\b", re.I), "one_year_nuri"),
    (re.compile(r"3\s*-?\s*y(ea)?r.*\b(nu|no\s*upfront)\b", re.I), "three_year_nuri"),
)


def _h(c: Any) -> str:
    return re.sub(r"\s+", " ", str(c).replace("\xa0", " ")).strip().lower() if c is not None else ""


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace("$", "").replace(",", "").replace("%", "").strip())
    except ValueError:
        return None


def short_name(name: Any) -> str:
    """``web-p01.corp.example.local`` -> ``web-p01``; an IP address is kept whole."""
    s = str(name or "").strip().lower()
    return s if re.fullmatch(r"[0-9.]+", s) else s.split(".")[0]


class _Sheet:
    """A sheet as header -> column, so nothing depends on column positions."""

    def __init__(self, ws: Any) -> None:
        rows = list(ws.iter_rows(values_only=True))
        self.header_row = next((i for i, r in enumerate(rows[:6]) if any(_h(c) for c in r)), 0)
        raw = list(rows[self.header_row]) if rows else []
        self.heads_raw = [re.sub(r"\s+", " ", str(c)).strip() for c in raw if c not in (None, "")]      # the headers as written (for a sheet A01 does not read)
        heads = [_h(c) for c in raw]
        self.index = {h: i for i, h in enumerate(heads) if h}
        self.rows = [r for r in rows[self.header_row + 1:] if any(c not in (None, "") for c in r)]

    def col(self, *names: str) -> int | None:
        for n in names:
            if n in self.index:
                return self.index[n]
        for n in names:                                        # a header may carry a suffix ("annualized total cost")
            hit = next((i for h, i in self.index.items() if h.startswith(n)), None)
            if hit is not None:
                return hit
        return None

    def get(self, row: tuple[Any, ...], *names: str) -> Any:
        i = self.col(*names)
        return row[i] if i is not None and i < len(row) else None


def model_slug(sheet_name: str) -> str | None:
    return next((slug for pat, slug in MODEL_SHEETS if pat.search(sheet_name)), None)


def parse_analysis(path: str | Path) -> dict[str, Any]:
    """Read the workbook.  Returns ``per_server`` (key: short lower-case name), ``totals`` (sums per pricing model and for storage), ``excluded``, ``issue_counts``
    and ``notes`` (what was read, what was ignored, what looked odd).  Raises ValueError if no per-server sheet is found."""
    import openpyxl
    wb = openpyxl.load_workbook(str(path), data_only=True)
    per: dict[str, dict[str, Any]] = {}
    totals: dict[str, dict[str, float]] = {}
    notes: list[str] = []
    read, ignored = [], []
    seen_models: dict[str, str] = {}
    dup: list[str] = []

    for ws in wb.worksheets:
        sh = _Sheet(ws)
        is_server_sheet = sh.col("server id") is not None and sh.col("ec2 instance") is not None
        if not is_server_sheet:
            continue
        slug = model_slug(ws.title)
        if slug is None:
            ignored.append(ws.title)
            continue
        if slug in seen_models:                                # e.g. a Mixed-tenancy sheet for the same model: the first (shared) one wins
            ignored.append(ws.title)
            continue
        seen_models[slug] = ws.title
        read.append(ws.title)
        tot = {"compute": 0.0, "network": 0.0, "total": 0.0, "servers": 0}
        for r in sh.rows:
            sid = sh.get(r, "server id")
            name = sh.get(r, "server name")
            if not sid or not name:
                continue
            key = short_name(name)
            rec = per.setdefault(key, {"id": str(sid), "name": str(name), "short": key, "models": {}})
            if "os_type" not in rec:
                util = {k: _num(sh.get(r, h)) for k, h in (("peak_cpu", "peak cpu utilization %"), ("p95_cpu", "p95 cpu utilization %"),
                                                          ("avg_cpu", "average cpu utilization %"), ("peak_ram", "peak ram utilization %"),
                                                          ("avg_ram", "average ram utilization %"), ("uptime", "uptime %"))}
                rec.update({"os_type": sh.get(r, "operating system type"), "os_name": sh.get(r, "operating system name"),
                            "vcpu": _num(sh.get(r, "total cores")), "ram_gb": _num(sh.get(r, "ram (gb)")),
                            "server_type": sh.get(r, "server type"), "region": sh.get(r, "aws region"), "util": util,
                            "env": sh.get(r, "environment"), "application": sh.get(r, "application")})
            elif rec["id"] != str(sid) and key not in dup:
                dup.append(key)                                 # two different servers share this short name: they cannot be told apart by name
            total = _num(sh.get(r, "annualized total cost"))
            net = _num(sh.get(r, "annualized network cost"))
            lic = _num(sh.get(r, "annualized license only cost"))
            ec2 = _num(sh.get(r, "annualized ec2 cost"))
            compute = (lic or 0.0) + (ec2 or 0.0) if (lic is not None or ec2 is not None) else ((total or 0.0) - (net or 0.0) if total is not None else None)
            rec["models"][slug] = {"instance": sh.get(r, "ec2 instance recommended"), "ec2_vcpu": _num(sh.get(r, "ec2 total cores")),
                                   "ec2_ram_gb": _num(sh.get(r, "ec2 ram (gb)")), "total": total, "compute": compute, "network": net,
                                   "license": lic, "ec2_excl_license": ec2}
            tot["servers"] += 1
            tot["compute"] += compute or 0.0
            tot["network"] += net or 0.0
            tot["total"] += total or 0.0
        totals[slug] = {k: round(v, 2) if k != "servers" else v for k, v in tot.items()}
    if not per:
        raise ValueError("no per-server sheet found (a sheet with 'Server Id' and 'EC2 Instance Recommended' columns for the On-Demand, 1yr NU or 3yr NU model)")
    if dup:
        notes.append(f"{len(dup)} short server name(s) are shared by different servers and cannot be matched by name: {', '.join(dup[:6])}")
        for k in dup:
            per[k]["ambiguous"] = True

    # storage: one row per volume, the cost-optimized mapping (the one the storage total uses)
    storage_total = 0.0
    orphans: list[tuple[str, float]] = []                       # volumes whose server has no row in the per-server sheets
    for ws in wb.worksheets:
        if not ws.title.lower().startswith("block-ebs-cost"):
            continue
        sh = _Sheet(ws)
        ci = sh.col("estimated annual aws cost incl. boot (cost optimized)")
        vi = sh.col("volume name")
        if ci is None or vi is None:
            notes.append(f"sheet '{ws.title}' has no volume cost column: storage per server not read")
            continue
        read.append(ws.title)
        for r in sh.rows:
            vol = str(r[vi] or "")
            cost = _num(r[ci]) or 0.0
            key = short_name(vol.split(":")[0])
            if key not in per and key.endswith("-storage"):             # the MPA layout names a volume "<server>-storage" (RVTools: "<server>:<disk>.vmdk")
                key = key[: -len("-storage")]
            storage_total += cost
            if key in per:
                per[key]["storage_annual"] = round(per[key].get("storage_annual", 0.0) + cost, 2)
                per[key]["volumes"] = per[key].get("volumes", 0) + 1
                svc = sh.get(r, "aws service mapping (cost optimized)")
                if svc:
                    per[key].setdefault("storage_services", [])
                    if svc not in per[key]["storage_services"]:
                        per[key]["storage_services"].append(str(svc))
            else:
                orphans.append((vol, cost))
        totals["storage"] = {"total": round(storage_total, 2)}
    # issues per server, and a count by reason for everything
    issue_counts: dict[str, int] = {}
    for ws in wb.worksheets:
        if _h(ws.title) != "assessment issues":
            continue
        sh = _Sheet(ws)
        read.append(ws.title)
        for r in sh.rows:
            typ = str(sh.get(r, "resource type") or "").lower()
            reason = str(sh.get(r, "reason") or "")
            issue_counts[f"{typ}: {reason}"] = issue_counts.get(f"{typ}: {reason}", 0) + 1
            if typ == "server":
                key = short_name(sh.get(r, "resource name"))
                if key in per:
                    per[key].setdefault("issues", []).append({"severity": sh.get(r, "severity"), "reason": reason,
                                                              "impact": str(sh.get(r, "impact") or "")[:300]})
    excluded: list[dict[str, str]] = []
    for ws in wb.worksheets:
        if _h(ws.title) != "excluded resources":
            continue
        sh = _Sheet(ws)
        read.append(ws.title)
        for r in sh.rows:
            excluded.append({"name": str(sh.get(r, "resource name") or sh.get(r, "resource id") or ""), "type": str(sh.get(r, "resource type") or ""),
                             "reason": str(sh.get(r, "reason") or "")})
    rds = _parse_rds(wb, read)
    if rds["instances"]:
        for slug, t in rds["totals"].items():
            totals.setdefault(slug, {})["rds_compute"] = t["compute_cat"]
        totals.setdefault("storage", {"total": round(storage_total, 2)})["rds"] = rds["totals"].get("three_year_nuri", next(iter(rds["totals"].values())))["storage"]
    s3 = _parse_s3(wb, read)
    if s3 is not None:
        totals.setdefault("storage", {"total": round(storage_total, 2)})["s3"] = s3
    if orphans:                                                  # ONE line, not one per volume: they all say the same thing
        names = sorted({short_name(v.split(":")[0]) for v, _ in orphans})
        notes.append(f"{len(orphans)} volume(s) of {len(names)} server(s) have no row in the per-server sheets (annual storage cost ${sum(c for _, c in orphans):,.0f}): "
                     + ", ".join(names[:8]) + (f", and {len(names) - 8} more" if len(names) > 8 else ""))
    for ws in wb.worksheets:                                     # a sheet that was NOT read is named with its size and columns, never skipped silently
        if ws.title in read or ws.title in ignored or _h(ws.title) in ("read me", "glossary"):      # the two documentation sheets are not data
            continue
        sh = _Sheet(ws)
        notes.append(f"not read: '{ws.title}' ({len(sh.rows)} row(s); columns: " + ", ".join(sh.heads_raw[:14]) + (", ..." if len(sh.heads_raw) > 14 else "") + ")")
    notes.insert(0, "read: " + ", ".join(read) + ("; ignored (another tenancy or pricing model): " + ", ".join(ignored) if ignored else ""))
    return {"per_server": per, "totals": totals, "excluded": excluded, "issue_counts": issue_counts, "notes": notes, "rds": rds, "s3_annual": s3}


def _yes(v: Any) -> bool:
    return v is True or str(v).strip().lower() in ("yes", "true", "1")


def _parse_rds(wb: Any, read: list[str]) -> dict[str, Any]:
    """The database assessment (RDS for SQL Server), layout VERIFIED against a real workbook (231-server MPA run, October 2026):
    ``RDS Annual Costs - <model>`` (one row per SQL Server instance and option; the recommended row is flagged), ``RDS Server Mappings - <model>`` (edition,
    environment, source and target sizing) and ``RDS Overview`` (a key/value list).  AWS Transform's totals take the database cost in as: compute category =
    (compute + license + backup + software assurance + storage) x (1 - MAP credit rate) - storage; storage category = the database storage.  The credit
    rate is AWS's own (Total Credits / Total pre-credit cost on the overview sheet); it is not assumed."""
    out: dict[str, Any] = {"instances": [], "overview": {}, "credit_rate": None, "totals": {}}
    inst: dict[str, dict[str, Any]] = {}
    for ws in wb.worksheets:
        t = _h(ws.title)
        if t == "rds overview":
            read.append(ws.title)
            for r in ws.iter_rows(values_only=True):
                if r and r[0] is not None and len(r) > 1 and r[1] is not None:
                    out["overview"][str(r[0]).strip()] = r[1]
                else:                                                # a sentence in a single cell (savings, credits; in the real file it sits in the SECOND column): kept as text
                    cell = next((c for c in (r or ()) if c not in (None, "")), None)
                    if cell is not None:
                        out.setdefault("notes", []).append(str(cell).strip())
            continue
        slug = model_slug(ws.title)
        if t.startswith("rds annual costs") and slug:
            sh = _Sheet(ws)
            read.append(ws.title)
            for r in sh.rows:
                if not _yes(sh.get(r, "is_recommended")):
                    continue
                name = str(sh.get(r, "source_server_name") or "")
                if not name:
                    continue
                rec = inst.setdefault(name, {"name": name, "short": short_name(name.split("\\")[0]), "env": sh.get(r, "source_server_environment"),
                                             "role": sh.get(r, "source_server_role"), "src_vcpus": _num(sh.get(r, "source_provisioned_vcpus")),
                                             "src_ram_gb": _num(sh.get(r, "source_provisioned_memory_gb")), "models": {}})
                c = {k: _num(sh.get(r, k)) or 0.0 for k in ("annual_compute_cost", "annual_license_cost", "annual_storage_cost", "annual_backup_cost", "annual_software_assurance_cost")}
                rec["models"][slug] = {"compute": c["annual_compute_cost"], "license": c["annual_license_cost"], "storage": c["annual_storage_cost"],
                                       "backup": c["annual_backup_cost"], "sa": c["annual_software_assurance_cost"], "licensed_vcpus": _num(sh.get(r, "licensed_vcpu_count")),
                                       "option": sh.get(r, "license_deployment_option")}
        elif t.startswith("rds server mappings") and slug:
            sh = _Sheet(ws)
            read.append(ws.title)
            for r in sh.rows:
                rec = inst.get(str(sh.get(r, "server_name") or ""))
                if rec is not None and "edition" not in rec:
                    rec.update({"edition": sh.get(r, "source_sql_server_edition"), "target_class": sh.get(r, "target_instance_class"),
                                "target_vcpus": _num(sh.get(r, "target_instance_vcpus")), "peak_cpu": _num(sh.get(r, "source_peak_cpu_utilization"))})
    out["instances"] = list(inst.values())
    ov = out["overview"]
    pre, cred = _num(ov.get("Total Annual Cost (Pre-Credits)")), _num(ov.get("Total Credits & Discounts"))
    out["credit_rate"] = (cred / pre) if (pre and cred is not None) else None
    keep = 1.0 - (out["credit_rate"] or 0.0)
    for slug in {m for i in out["instances"] for m in i["models"]}:
        rows = [i["models"][slug] for i in out["instances"] if slug in i["models"]]
        stor = sum(x["storage"] for x in rows)
        gross = sum(x["compute"] + x["license"] + x["backup"] + x["sa"] for x in rows) + stor
        out["totals"][slug] = {"compute_cat": round(gross * keep - stor, 2), "storage": round(stor, 2), "gross": round(gross, 2)}
    return out


def _parse_s3(wb: Any, read: list[str]) -> float | None:
    for ws in wb.worksheets:
        if _h(ws.title) != "object-s3":
            continue
        sh = _Sheet(ws)
        ci = sh.col("aws directional cost estimation (annually)")
        if ci is None:
            return None
        read.append(ws.title)
        for r in sh.rows:
            if _h(r[0]) == "total":
                return _num(r[ci])
    return None


def reconcile(models: list[Any], totals: dict[str, dict[str, float]]) -> list[dict[str, Any]]:
    """Do the per-server rows add up to AWS Transform's own totals (aggregated_output.json)?  Compute and network per pricing model, and storage."""
    rows: list[dict[str, Any]] = []

    def add(label: str, aws: float | None, ours: float | None) -> None:
        if aws is None or ours is None:
            return
        diff = round(ours - aws, 2)
        rows.append({"what": label, "aws": round(aws, 2), "per_server": round(ours, 2), "diff": diff, "ok": abs(diff) <= max(1.5, 0.002 * abs(aws))})
    for m in models:
        t = totals.get(m.slug)
        if not t:
            continue
        extra = t.get("rds_compute")                                        # the database part AWS Transform adds to its compute category
        add(f"{m.label}: compute" + (" (EC2 + RDS)" if extra else ""), m.by_category.get("compute"), None if t.get("compute") is None else t["compute"] + (extra or 0.0))
        add(f"{m.label}: network", m.by_category.get("network"), t.get("network"))
    if models and totals.get("storage"):
        st = totals["storage"]
        extra = st.get("rds", 0.0) + st.get("s3", 0.0)
        add("Storage (EBS" + (" + S3 + RDS)" if extra else ")"), models[0].by_category.get("storage"), st["total"] + extra)
    return rows
