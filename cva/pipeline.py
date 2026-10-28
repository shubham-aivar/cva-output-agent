"""The deterministic pipeline: inputs -> normalised estate -> licensing -> sizing -> pricing -> findings.

Everything here is code: the same inputs always produce the same figures.  The agent layer reads this
result through tools and may only *propose* changes; accepted proposals come back as inputs
(mapping overrides, licensing assumptions, sizing overrides) and the pipeline is simply run again.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .config import Config, load_config
from .engines import licensing as lic
from .engines import lifecycle, parity, quality
from .engines.figures import record_all
from .engines.pricebook import PriceBook
from .engines.pricing import PricingResult, price_estate
from .engines.rightsize import SizingResult, size_estate
from .engines.rules import RULESET_VERSION
from .ingest.detect import read_any
from .ingest.licensing_in import load_licensing
from .ingest.result import IngestResult
from .model import Estate, Issue, Ledger, LicensingFinding, LicensingSet, richness


@dataclass
class RunInputs:
    files: list[str]
    licensing_file: str | None = None
    kind: str | None = None
    collection_days: int | None = None
    as_of: str | None = None                                  # ISO date; defaults to today
    customer_label: str | None = None
    mapping_overrides: dict[str, Any] = field(default_factory=dict)
    assumptions: list[dict[str, Any]] = field(default_factory=list)   # accepted licensing assumptions
    sizing_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    transform_results: dict[str, dict[str, Any]] | None = None
    settings_patch: dict[str, Any] = field(default_factory=dict)
    aws: dict[str, Any] | None = None                         # an imported AWS Transform assessment (AwsAssessment.to_dict())
    offline_estimate: bool = False                            # run A01's own sizing and pricing (fallback / cross-check); off by default


@dataclass
class PipelineResult:
    estate: Estate
    licensing: LicensingSet
    sizing: dict[str, SizingResult]
    pricing: PricingResult
    lifecycle: list[lifecycle.LifecycleFinding]
    parity_rows: list[parity.ParityRow]
    parity_summary: dict[str, Any]
    issues: list[Issue]
    lic_gaps: list[dict[str, Any]]
    lic_cov: dict[str, Any]
    ledger: Ledger
    book: PriceBook
    cfg: Config
    as_of: date
    unmapped: dict[str, list[str]]
    sections: dict[str, str]
    inputs: RunInputs
    meta: dict[str, Any] = field(default_factory=dict)
    aws: Any = None
    estimate_ran: bool = False
    aws_sizing: dict[str, SizingResult] = field(default_factory=dict)      # AWS Transform's per-server sizing (analysis workbook), if imported

    @property
    def rightsizing(self) -> dict[str, SizingResult]:
        """The per-server sizing the slides, workbook and Workloads tab use: AWS Transform's when imported, otherwise A01's own estimate."""
        return self.aws_sizing or self.sizing

    @property
    def sizing_source(self) -> str | None:
        return "aws_transform" if self.aws_sizing else ("a01_estimate" if self.estimate_ran else None)


def merge_estates(primary: Estate, others: list[Estate]) -> list[str]:
    """Fill gaps in the primary estate from further exports of the same environment.  Returns notes."""
    notes: list[str] = []
    idx = primary.by_key()
    for o in others:
        added = filled = 0
        for w in o.workloads:
            p = idx.get(w.key)
            if p is None:
                primary.workloads.append(w)
                idx[w.key] = w
                added += 1
                continue
            for f in ("os_raw", "os_key", "os_label", "vcpu", "ram_gb", "storage_used_gb", "storage_prov_gb", "env",
                      "machine_type", "cpu_avg", "cpu_peak", "ram_avg", "ram_peak", "ram_peak_gb", "disk_iops_p95",
                      "disk_mbps_p95", "net_mbps_p95", "source_instance", "source_annual_cost", "host", "cluster",
                      "host_cores", "power_state"):
                if getattr(p, f) is None and getattr(w, f) is not None:
                    setattr(p, f, getattr(w, f))
                    filled += 1
            if p.os_family in ("unknown", "other") and w.os_family not in ("unknown", "other"):
                p.os_family = w.os_family
            if not p.sql and w.sql:
                p.sql = w.sql
            p.refs += w.refs
            for k, v in w.extras.items():
                p.extras.setdefault(k, v)
        primary.files += [f for f in o.files if f not in primary.files]
        notes.append(f"{o.files[0]}: filled {filled} gap(s), added {added} workload(s) from a further export.")
    return notes


def run_pipeline(inp: RunInputs, cfg: Config | None = None) -> PipelineResult:
    cfg = cfg or load_config(overrides=inp.mapping_overrides, settings_patch=inp.settings_patch)
    if inp.customer_label:
        cfg.settings.setdefault("run", {})["customer_label"] = inp.customer_label
    as_of = date.fromisoformat(inp.as_of) if inp.as_of else date.today()
    book = PriceBook(cfg)

    results: list[IngestResult] = [read_any(f, cfg, kind=inp.kind if i == 0 else None,
                                            overrides=inp.mapping_overrides, collection_days=inp.collection_days)
                                   for i, f in enumerate(inp.files)]
    primary = results[0]
    estate = primary.estate
    notes = merge_estates(estate, [r.estate for r in results[1:]])
    estate.notes += notes
    issues: list[Issue] = list(primary.issues)
    hints: list[LicensingFinding] = list(primary.licensing_hints)
    unmapped = dict(primary.unmapped)
    sections = dict(primary.sections)
    for r in results[1:]:
        issues += r.issues
        hints += r.licensing_hints
        unmapped.update(r.unmapped)
        sections.update(r.sections)

    ls, lissues = load_licensing(inp.licensing_file, cfg, hints, inp.mapping_overrides)
    issues += lissues
    if inp.assumptions:
        extra = [LicensingFinding(**{**a, "origin": "assumption", "confidence": "low"}) for a in inp.assumptions]
        ls = LicensingSet("provided", ls.version,
                          (ls.provenance + " + " if ls.mode == "provided" else "") +
                          f"{len(extra)} reviewer-accepted assumption(s)",
                          list(ls.findings) + extra, ls.notes)
    issues += lic.validate(ls, estate)

    estimate = bool(inp.offline_estimate)
    lcs = lifecycle.evaluate(estate, cfg, as_of)
    if estimate:                                   # A01's own sizing and pricing: only when someone chose to run it (fallback / cross-check)
        sizing = size_estate(estate, cfg, book)
        for name, ov in inp.sizing_overrides.items():
            sz = sizing.get(name)
            inst = book.get(ov.get("instance"))
            if sz is not None and inst is not None:
                sz.ok, sz.instance, sz.rec_vcpu, sz.rec_mem_gb, sz.burstable = True, inst.name, inst.vcpu, inst.mem_gb, inst.burstable
                sz.flags.append("reviewer_override")
                sz.notes.append(f"Reviewer override to {inst.name}: {ov.get('reason', '')}")
        pricing = price_estate(estate, sizing, ls, cfg, book)
        prow, psum = parity.compare(estate, sizing, cfg, book, inp.transform_results)
    else:
        sizing = {}
        pricing = PricingResult([], {}, book.placeholder, str(book.meta.get("version", "?")))
        prow = []
        psum = {"tolerance_pct": float(cfg.section("parity").get("cost_tolerance_pct", 15)), "compared": 0, "outliers": 0, "by_status": {}}
    gaps = lic.gaps(estate, ls)
    cov = lic.coverage(estate, ls)

    aws = None
    aws_sizing: dict[str, SizingResult] = {}
    if inp.aws:
        from .transform.aws_sizing import sizing_from_aws
        from .transform.results import AwsAssessment
        aws = AwsAssessment.from_dict(inp.aws)
        aws_sizing = sizing_from_aws(estate, aws)
        issues += [Issue(**{k: v for k, v in d.items()}) for d in aws.issues]      # one exceptions list from all sources
        psum["totals"] = parity.total_compare(aws, pricing, estate)
    issues = quality.check(estate, cfg, issues)
    issues = quality.with_licensing(issues, gaps, cov)
    issues = quality.finalize(issues)

    led = Ledger()
    record_all(led, estate, sizing, pricing, ls, lcs, issues, psum, cov, aws, estimate, aws_sizing)
    meta = {"ruleset": RULESET_VERSION, "config_digest": cfg.digest(), "as_of": as_of.isoformat(),
            "catalog": {"version": book.meta.get("version"), "placeholder": book.placeholder},
            "richness": richness(estate), "source_kind": estate.source_kind, "files": estate.files,
            "lifecycle_rules_unverified": any(not f.verified for f in lcs),
            "figures_source": "aws_transform" if aws else ("a01_estimate" if estimate else "none"), "estimate_ran": estimate,
            "sizing_source": "aws_transform" if aws_sizing else ("a01_estimate" if estimate else None), "aws_servers": len(aws.per_server) if aws else 0,
            "aws": ({"scenario": aws.scenario_name, "job": aws.job} if aws else None)}
    return PipelineResult(estate, ls, sizing, pricing, lcs, prow, psum, issues, gaps, cov, led, book, cfg, as_of,
                          unmapped, sections, inp, meta, aws, estimate, aws_sizing)
