"""Import AWS Transform assessment results.  Layout VERIFIED against a real job's ``aggregated_output.json``,
``error_file.csv`` and analysis workbook (RVTools sample, October 2026).  The totals come from aggregated_output.json; the per-server
recommendations and costs come from the analysis workbook (see ``analysis.py``) and are reconciled against those totals."""
from __future__ import annotations

import csv
import json
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..model import Issue

SLUG_TO_ID = {"on_demand": "on_demand", "one_year_nuri": "ri_1yr", "three_year_nuri": "ri_3yr"}


@dataclass
class PricingModel:
    slug: str
    label: str
    total: float
    by_category: dict[str, float]
    recommended: bool = False


@dataclass
class AwsAssessment:
    scenario_name: str = ""
    timestamp: str = ""
    models: list[PricingModel] = field(default_factory=list)
    on_prem_total: float | None = None
    on_prem_by_category: dict[str, float] = field(default_factory=dict)
    servers: dict[str, Any] = field(default_factory=dict)
    assessments: dict[str, dict[str, Any]] = field(default_factory=dict)
    warning_rows: int = 0
    issues: list[dict[str, Any]] = field(default_factory=list)
    files: dict[str, str] = field(default_factory=dict)
    job: dict[str, Any] = field(default_factory=dict)
    per_server: dict[str, dict[str, Any]] = field(default_factory=dict)       # from the analysis workbook, key: short lower-case server name
    per_server_totals: dict[str, dict[str, float]] = field(default_factory=dict)
    per_server_notes: list[str] = field(default_factory=list)
    excluded: list[dict[str, str]] = field(default_factory=list)
    reconciliation: list[dict[str, Any]] = field(default_factory=list)         # do the per-server rows add up to AWS Transform's totals?
    rds: dict[str, Any] = field(default_factory=dict)                          # the database assessment from the analysis workbook (instances, overview, credit rate)
    s3_annual: float | None = None
    business_case: dict[str, Any] = field(default_factory=dict)                # AWS's own scope figures, from its business case deck

    def model(self, slug: str) -> PricingModel | None:
        return next((m for m in self.models if m.slug == slug), None)

    @property
    def recommended(self) -> PricingModel | None:
        return next((m for m in self.models if m.recommended), None)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AwsAssessment":
        d = dict(d)
        d["models"] = [PricingModel(**m) for m in d.get("models", [])]
        return cls(**d)


def _reason_code(reason: str) -> str:
    r = reason.lower()
    if "utilization" in r:
        return "TRW-UTIL"
    if "zero peak iops" in r:
        return "TRW-IOPS-ZERO"
    if "no peak iops" in r:
        return "TRW-IOPS-NONE"
    return "TRW-OTHER"


def parse_errors(path: Path) -> tuple[list[Issue], int]:
    """One aggregated Issue per distinct reason (the CSV has one row per resource and reason)."""
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    groups: dict[tuple[str, str, str], list[dict[str, str]]] = {}
    for r in rows:
        groups.setdefault((r.get("reason", ""), r.get("severity", ""), r.get("resource_type", "")), []).append(r)
    out: list[Issue] = []
    for (reason, sev, rtype), rs in groups.items():
        severity = "error" if sev.upper() in ("ERROR", "CRITICAL") else "warn" if sev.upper() == "WARNING" else "info"
        names = [r.get("resource_name") or r.get("resource_id", "") for r in rs]
        out.append(Issue(
            _reason_code(reason), severity,
            f"AWS Transform ({rtype.lower()}): {reason} {len(rs)} resource(s). {rs[0].get('impact', '')}",
            detail={"count": len(rs), "resource_type": rtype, "impact": rs[0].get("impact", ""),
                    "recommendation": rs[0].get("recommendation", ""), "resources": names[:400],
                    "source": "aws_transform"}))
    return out, len(rows)


def parse_aggregated(path: Path) -> AwsAssessment:
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    pc = d.get("pricing_comparison", {})
    models = [PricingModel(m["pricing_model_slug"], m["pricing_model"], float(m["total_aws_cost"]),
                           {k: float(v) for k, v in (m.get("by_category") or {}).items()}, bool(m.get("is_recommended")))
              for m in pc.get("models", [])]
    ent = d.get("scope", {}).get("entities", {})
    srv = ent.get("servers", {})
    assess: dict[str, dict[str, Any]] = {}
    for a in d.get("assessments", []):
        t = a.get("metadata", {}).get("assessment_type", "?")
        ov = a.get("overview", {})
        assess[t] = {"annual_cost": ov.get("recommended_annual_cost"), "resources": ov.get("resources_analyzed"),
                     "capacity": ov.get("capacity_analyzed"), "assumptions": [str(x) for x in a.get("assumptions", [])],
                     "recommendations": [r.get("title") for r in a.get("recommendations", []) if isinstance(r, dict)],
                     "fallback_warnings": ov.get("fallback_warnings")}
    return AwsAssessment(
        scenario_name=d.get("metadata", {}).get("scenario_name", ""), timestamp=d.get("metadata", {}).get("aggregation_timestamp", ""),
        models=models, on_prem_total=pc.get("total_on_prem_cost"), on_prem_by_category=pc.get("on_prem_by_category", {}),
        servers={"total": srv.get("total"), "in_scope": (srv.get("in_scope") or {}).get("total"),
                 "excluded": (srv.get("excluded") or {}).get("total"), "reasons": (srv.get("excluded") or {}).get("reasons", []),
                 "windows": (srv.get("details") or {}).get("windows"), "linux": (srv.get("details") or {}).get("linux")},
        assessments=assess)


def _safe_extract(zpath: Path, dest: Path) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    out = []
    with zipfile.ZipFile(zpath) as z:
        for info in z.infolist():
            target = (dest / info.filename).resolve()
            if dest.resolve() not in target.parents and target != dest.resolve():
                raise ValueError(f"unsafe path in zip: {info.filename}")
            if info.is_dir():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(z.read(info))
            out.append(target)
    return out


def _find(root: Path, pattern: str, deep: bool) -> Path | None:
    """Look only inside the folder the user gave (recursively when they gave a folder, flat when they gave a file)."""
    hits = sorted(root.rglob(pattern) if deep else root.glob(pattern))
    return hits[0] if hits else None


def load_results(path: str | Path, workdir: str | Path | None = None) -> AwsAssessment:
    """Accept a results folder, an ``Initial_Assessment_*.zip``, or ``aggregated_output.json`` itself.  Nothing outside
    the given folder is ever searched."""
    p = Path(path)
    if p.suffix.lower() == ".zip":
        p = Path(workdir or p.parent / (p.stem + "_unzipped"))
        _safe_extract(Path(path), p)
    deep = p.is_dir()
    root = p if deep else p.parent
    agg = _find(root, "aggregated_output.json", deep) if deep else p
    if agg is None or not agg.exists():
        raise FileNotFoundError("aggregated_output.json not found in " + str(root))
    res = parse_aggregated(agg)
    files = {"aggregated_output.json": str(agg)}
    for pat, key in (("error_file.csv", "error_file.csv"), ("analysis_*.xlsx", "analysis.xlsx"),
                     ("business_case_*.pptx", "business_case.pptx"), ("report_*.pdf", "report.pdf")):
        f = _find(root, pat, deep)
        if f is None and agg.parent.name.lower().startswith("assessment"):
            f = _find(agg.parent.parent, pat, False)          # real layout: outputs sit one folder above 'assessment outputs'
        if f:
            files[key] = str(f)
    res.files = files
    if "error_file.csv" in files:
        issues, n = parse_errors(Path(files["error_file.csv"]))
        res.warning_rows, res.issues = n, [i.to_dict() for i in issues]
    if "analysis.xlsx" not in files:                           # the deliverables are inside the assessment zip, which is named after the scenario
        zips = [z for z in sorted((root.rglob("*.zip") if deep else root.glob("*.zip")), key=lambda p: p.stat().st_mtime, reverse=True)
                if "unzipped" not in z.parts]                  # (Initial_Assessment_<ts>.zip, Cost_Optimized_Assessment_<ts>.zip, ...)
        for z in zips:
            extracted = _safe_extract(z, root / "unzipped")
            for f in extracted:
                for pre, key in (("analysis_", "analysis.xlsx"), ("business_case_", "business_case.pptx"), ("report_", "report.pdf")):
                    if f.name.startswith(pre):
                        res.files[key] = str(f)
            if "analysis.xlsx" in res.files:
                break
    if "analysis.xlsx" in res.files:                           # the per-server results: failure here never loses the totals
        from .analysis import parse_analysis, reconcile
        try:
            a = parse_analysis(res.files["analysis.xlsx"])
            res.per_server, res.per_server_totals, res.excluded, res.per_server_notes = a["per_server"], a["totals"], a["excluded"], a["notes"]
            res.rds, res.s3_annual = a.get("rds") or {}, a.get("s3_annual")
            res.reconciliation = reconcile(res.models, a["totals"])
            bad = [r for r in res.reconciliation if not r["ok"]]
            if bad:
                res.issues.append(Issue("TRW-RECON", "warn", "The per-server results do not add up to AWS Transform's own totals (" + "; ".join(
                    f"{r['what']}: AWS {r['aws']:,.0f}, per-server {r['per_server']:,.0f}" for r in bad[:4]) + "): check the analysis workbook against the totals.",
                                         detail={"count": len(bad), "impact": "Per-server figures may not match AWS Transform's totals.",
                                                 "recommendation": "Compare the Reconciliation table on the AWS Transform tab with the analysis workbook."}).to_dict())
        except Exception as e:
            res.per_server_notes = [f"the analysis workbook could not be read: {type(e).__name__}: {str(e)[:200]}"]
            res.issues.append(Issue("TRW-ANALYSIS", "warn", f"The analysis workbook could not be read, so there are no per-server AWS results: {str(e)[:160]}",
                                     detail={"count": 1, "impact": "Workloads and the deck's rightsizing slide have no AWS per-server sizing.",
                                             "recommendation": "Import the analysis workbook (analysis_<timestamp>.xlsx, inside the assessment zip) or run A01's offline estimate."}).to_dict())
    if "business_case.pptx" in res.files:                         # AWS's own scope figures: a deck that cannot be read never loses the rest
        from .business_case import parse_business_case
        try:
            res.business_case = parse_business_case(res.files["business_case.pptx"])
        except Exception as e:
            res.per_server_notes = list(res.per_server_notes) + [f"the business case deck could not be read: {type(e).__name__}: {str(e)[:160]}"]
    return res
