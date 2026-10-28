"""One view of the scenarios for the deck, workbook, UI and agent: AWS Transform's figures when an assessment has
been imported, otherwise A01's own offline estimate.  Every row says which, so a reader is never misled."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ScenarioRow:
    id: str
    label: str
    total: float | None
    components: dict[str, float] = field(default_factory=dict)
    status: str = "computed"        # computed | partial | illustrative | not_computable | not_in_assessment
    source: str = "a01_estimate"    # aws_transform | a01_estimate
    notes: list[str] = field(default_factory=list)
    coverage_pct: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _a01_components(s) -> dict[str, float]:
    return {"Infrastructure": s.infra, "Windows licenses": s.windows, "SQL Server licenses": s.sql, "Storage": s.storage}


def rows(res) -> list[ScenarioRow]:
    from ..transform.results import SLUG_TO_ID
    labels = {s["id"]: s["label"] for s in res.cfg.scenarios}
    if res.aws is None and not res.estimate_ran:
        return []                                  # no figures yet: nothing is shown rather than a number nobody asked for
    if res.aws is None:
        return [ScenarioRow(s.id, s.label, s.total, _a01_components(s) if s.total is not None else {}, s.status,
                            "a01_estimate", list(s.notes), s.coverage_pct) for s in res.pricing.scenarios]
    out: list[ScenarioRow] = []
    for slug, sid in SLUG_TO_ID.items():
        m = res.aws.model(slug)
        if m is None:
            out.append(ScenarioRow(sid, labels.get(sid, sid), None, {}, "not_in_assessment", "aws_transform",
                                   ["This pricing model is not in the imported AWS Transform assessment."]))
        else:
            out.append(ScenarioRow(sid, labels.get(sid, sid), m.total, {k.title(): v for k, v in m.by_category.items()},
                                   "computed", "aws_transform",
                                   ["Recommended by AWS Transform."] if m.recommended else []))
    b = res.pricing.scenario("byol")
    if b is not None and b.total is not None and b.status in ("computed", "partial", "illustrative"):
        out.append(ScenarioRow("byol", labels.get("byol", "BYOL") + " (A01 estimate)", b.total, _a01_components(b), b.status,
                               "a01_estimate", ["Not from AWS Transform: the assessment had no database or licensing data. "
                                                "A01 estimate on a placeholder price catalog."] + list(b.notes), b.coverage_pct))
    else:
        out.append(ScenarioRow("byol", labels.get("byol", "BYOL"), None, {}, "not_in_assessment", "aws_transform",
                               ["BYOL is not in the AWS Transform assessment: it needs SQL Server / database data and a "
                                "licensing finding. Ask AWS Transform for a licensing what-if once that data is supplied."]))
    return out


def by_id(res, sid: str) -> ScenarioRow | None:
    return next((r for r in rows(res) if r.id == sid), None)


def source_label(res) -> str:
    return "AWS Transform assessment" if res.aws is not None else "A01 offline estimate"
