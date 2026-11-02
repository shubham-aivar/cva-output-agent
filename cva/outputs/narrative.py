"""Slide commentary: accepted agent proposals when they exist, otherwise deterministic template text.
Either way every sentence is built from, or checked against, recorded figures, and is labelled by source."""
from __future__ import annotations

from typing import Any

from ..pipeline import PipelineResult
from .scenario_view import by_id as scen_by_id
from .scenario_view import source_label


def _money(v: Any) -> str:
    return "not computed" if v is None else f"${v:,.0f}"


def template_commentary(res: PipelineResult) -> dict[str, str]:
    L = res.ledger
    g = lambda k: L.value(k)  # noqa: E731
    byol = scen_by_id(res, "byol")
    byol_txt = {"computed": "BYOL is computed from the licensing input.",
                "not_in_assessment": "BYOL is not part of the AWS Transform assessment; it needs database data and a licensing finding.",
                "partial": "BYOL is only partly computed because some SQL Server items have no licensing finding.",
                "not_computable": "BYOL cannot be computed without a licensing input.",
                "illustrative": "BYOL is an illustrative upper bound, not evidence."}.get(byol.status if byol else "", "")
    unver = " Lifecycle dates are from an unverified rules table." if res.meta["lifecycle_rules_unverified"] else ""
    ph = " Prices come from a placeholder catalog." if (res.pricing.placeholder and res.aws is None) else ""
    src = f" Figures come from the {source_label(res)}."
    return {
        "executive_summary": (f"{g('estate.workloads')} workloads were assessed (data grade {g('estate.data_grade')}). "
                              + (f"On-Demand costs {_money(g('scenario.on_demand.total'))} a year; the 3-year commitment scenario "
                                 f"{_money(g('scenario.ri_3yr.total'))}. {byol_txt}{ph}{src}" if g('scenario.on_demand.total') is not None else
                                 "No cost figures exist yet: they come from an AWS Transform assessment.")),
        "cost_comparison": (f"Annual totals: On-Demand {_money(g('scenario.on_demand.total'))}, 1-year commitment "
                            f"{_money(g('scenario.ri_1yr.total'))}, 3-year commitment {_money(g('scenario.ri_3yr.total'))}, BYOL "
                            f"{_money(g('scenario.byol.total'))}.{ph}" if g('scenario.on_demand.total') is not None else
                            "No cost figures exist yet: they come from an AWS Transform assessment."),
        "rightsizing": ((f"{g('sizing.downsize')} workloads are over-provisioned, {g('sizing.same')} are close to "
                         f"right-sized and {g('sizing.upsize')} need more capacity. {g('sizing.conf_low')} of "
                         f"{g('sizing.sized')} sizings are low confidence."
                         + (" This sizing is AWS Transform's per-server recommendation; where its inventory had no measured utilization, AWS used defaults and the sizing is low confidence."
                            if res.sizing_source == "aws_transform" else
                            " This sizing is A01's own estimate; AWS Transform's per-server recommendations were not imported." if res.aws else ""))
                        if res.sizing_source else "Rightsizing figures are not available: import AWS Transform's per-server results, or run A01's offline estimate."),
        "licensing": ((f"The licensing input covers {g('licensing.coverage_sql')}% of SQL Server items; " if g('licensing.coverage_sql') is not None
                       else "No SQL Server was detected in this input; ")
                      + f"{g('licensing.gaps')} license-bearing items have no finding. {byol_txt}"),
        "lifecycle": f"{g('lifecycle.eos')} items are past end of support and {g('lifecycle.approaching')} are "
                     f"approaching it, across {g('lifecycle.at_risk')} workloads.{unver}",
        "data_quality": f"{g('quality.total')} exceptions were raised: {g('quality.error')} errors, "
                        f"{g('quality.warn')} warnings and {g('quality.info')} informational.",
    }


def commentary_for(res: PipelineResult, state: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Accepted commentary is used only while the figures are unchanged since it was accepted; otherwise the
    deterministic template text is used and the source says why."""
    from ..review.gate import figure_digest
    tpl = template_commentary(res)
    acc = state.get("commentary", {})
    cur = figure_digest(res)
    out: dict[str, dict[str, str]] = {}
    for k, v in tpl.items():
        a = acc.get(k)
        if a and a.get("digest") == cur:
            out[k] = {"text": a["text"], "source": a.get("source", "model")}
        elif a:
            out[k] = {"text": v, "source": "template (accepted commentary is out of date)"}
        else:
            out[k] = {"text": v, "source": "template"}
    return out
