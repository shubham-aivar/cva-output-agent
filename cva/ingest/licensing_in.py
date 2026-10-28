"""Licensing input: the part of A01 we know least about, so the most adaptable.

Three entry points, all returning the same ``LicensingSet``:

* ``none``      no licensing input exists yet (the situation today)
* analyst file  the Pilot input (SOW OI-4): an Excel/CSV the Ingram licensing analyst prepares
* A02 JSON      the Release 2 handoff, a versioned contract ("licensing-findings/0")

Whatever arrives is coerced to ``LicensingFinding``; unknown values become "unknown" and are reported,
never guessed.  When the real analyst sheet or A02 contract is known, only the mapping file or
``parse_findings`` changes.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..config import Config
from ..model import Issue, LicensingFinding, LicensingSet
from ..util import is_blank, norm, to_bool, to_float
from .base import best_sheet_for, find_sheet, load_sheets, read_table, table_issues

SCHEMA = "licensing-findings/0"
_MODELS = {"byol": "byol", "sql byol": "byol", "bring your own license": "byol", "li": "li", "sql li": "li",
           "license included": "li", "license-included": "li"}
_PRODUCTS = {"sql": "sql_server", "sql server": "sql_server", "sql_server": "sql_server",
             "windows": "windows_server", "windows server": "windows_server", "windows_server": "windows_server"}
_TENANCY = {"shared": "shared", "default": "shared", "dedicated": "dedicated", "dedicated host": "dedicated",
            "any": "any", "": "unknown"}
_BY = {"cores": "cores", "core": "cores", "server/cal": "server_cal", "server cal": "server_cal",
       "server_cal": "server_cal", "": "unknown"}


def _enum(table: dict[str, str], raw: Any, default: str = "unknown") -> tuple[str, bool]:
    if is_blank(raw):
        return default, True
    key = norm(raw).replace("  ", " ")
    key2 = str(raw).strip().lower()
    for k in (key2, key):
        if k in table:
            return table[k], True
    return default, False


def parse_findings(items: list[dict[str, Any]], origin: str, ref_prefix: str = "") -> tuple[list[LicensingFinding], list[Issue]]:
    findings: list[LicensingFinding] = []
    issues: list[Issue] = []
    for i, it in enumerate(items):
        wl = str(it.get("workload") or "").strip()
        if not wl:
            issues.append(Issue("LIC-NOWORKLOAD", "warn", f"Licensing finding #{i + 1} has no workload name; skipped."))
            continue
        model, ok1 = _enum(_MODELS, it.get("model"))
        prod, ok2 = _enum(_PRODUCTS, it.get("product"), "sql_server")
        ten, ok3 = _enum(_TENANCY, it.get("tenancy"))
        by, ok4 = _enum(_BY, it.get("licensed_by"))
        bad = [n for n, ok in (("model", ok1), ("product", ok2), ("tenancy", ok3), ("licensed_by", ok4)) if not ok]
        if bad:
            issues.append(Issue("LIC-BADVALUE", "warn",
                                f"Licensing finding for '{wl}' has unrecognised value(s) for {', '.join(bad)}; "
                                "treated as unknown.", workload=wl, detail={"fields": bad}))
        conf = str(it.get("confidence") or "medium").lower()
        findings.append(LicensingFinding(
            workload=wl, product=prod, instance=(None if is_blank(it.get("instance")) else str(it["instance"])),
            model=model, tenancy=ten, licensed_by=by,
            active_sa=it["active_sa"] if isinstance(it.get("active_sa"), bool) else to_bool(it.get("active_sa")),
            include_in_count=it["include_in_count"] if isinstance(it.get("include_in_count"), bool)
            else to_bool(it.get("include_in_count")),
            cores=to_float(it.get("cores")), basis=str(it.get("basis") or ""),
            confidence=conf if conf in {"high", "medium", "low"} else "medium", origin=origin,
            exceptions=[str(x) for x in (it.get("exceptions") or [])], ref=f"{ref_prefix}#{i + 1}"))
    return findings, issues


def from_json(path: Path) -> tuple[LicensingSet, list[Issue]]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    issues: list[Issue] = []
    if doc.get("schema") != SCHEMA:
        issues.append(Issue("LIC-SCHEMA", "warn",
                            f"Licensing file declares schema '{doc.get('schema')}', expected '{SCHEMA}'. "
                            "Values were read leniently; confirm the contract with the producer."))
    f, iss = parse_findings(doc.get("findings", []), "a02", path.name)
    issues += iss
    return LicensingSet("provided", str(doc.get("schema") or SCHEMA),
                        f"{doc.get('source', 'A02')} file {path.name}", f,
                        notes=[str(doc.get("generated", ""))]), issues


def from_table(path: Path, cfg: Config, overrides: dict[str, Any] | None = None) -> tuple[LicensingSet, list[Issue]]:
    m = cfg.mapping("licensing_analyst", overrides)
    sheets = load_sheets(path)
    name = find_sheet(sheets, m["sheet"]["names"]) or best_sheet_for(sheets, m["columns"])
    issues: list[Issue] = []
    if name is None:
        issues.append(Issue("LIC-NOSHEET", "error", f"No recognisable licensing sheet in {path.name}."))
        return LicensingSet("none", provenance=f"unreadable file {path.name}"), issues
    t = read_table(path.name, name, sheets[name], m["columns"])
    issues += table_issues(t, "LIC")
    items: list[dict[str, Any]] = []
    for row in t.rows:
        d = row.d
        if is_blank(d.get("workload")):
            continue
        prod = d.get("product")
        if is_blank(prod):  # SQL-style sheets (the AWS OLA template) carry no product column
            prod = "sql_server" if any(not is_blank(d.get(k)) for k in ("instance", "model", "active_sa")) \
                else "windows_server"
        items.append({**d, "product": prod})
    f, iss = parse_findings(items, "analyst", path.name)
    issues += iss
    return LicensingSet("provided", SCHEMA, f"analyst workbook {path.name} (sheet '{name}')", f,
                        notes=[f"{len(f)} finding(s) read; layout assumed from AWS OLA template columns"]), issues


def load_licensing(path: str | Path | None, cfg: Config, hints: list[LicensingFinding] | None = None,
                   overrides: dict[str, Any] | None = None) -> tuple[LicensingSet, list[Issue]]:
    issues: list[Issue] = []
    if path:
        p = Path(path)
        ls, issues = from_json(p) if p.suffix.lower() == ".json" else from_table(p, cfg, overrides)
        if ls.mode == "provided":
            return ls, issues
    if hints:
        return LicensingSet("provided", SCHEMA, "licensing columns filled in the Cloudamize OLA template",
                            list(hints)), issues
    return LicensingSet("none", SCHEMA, "no licensing input supplied"), issues
