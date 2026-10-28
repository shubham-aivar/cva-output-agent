"""AWS Transform's per-server recommendations, expressed as A01's ``SizingResult`` so the same ledger, deck and workbook code can use them.

Nothing is recomputed: the instance, its cores and RAM and the costs are AWS Transform's, for the pricing model AWS recommends (3-year no-upfront in the
real assessment, On-Demand if none is flagged).  The one thing A01 derives is the direction, by the SAME rule as A01's own engine:
"upsize" only when the workload itself needs more than it has (here: AWS's own utilization applied to what it has today), "downsize" when the
recommended instance has less than 70% of the cores or of the RAM allocated today, otherwise "same".  Confidence is low where AWS itself says the utilization
data was missing or invalid (it then used defaults), otherwise high."""
from __future__ import annotations

from typing import Any

from ..engines.rightsize import SizingResult
from .analysis import short_name


def recommended_slug(aws: Any) -> str:
    rec = aws.recommended
    return rec.slug if rec is not None else "on_demand"


def sizing_from_aws(estate: Any, aws: Any) -> dict[str, SizingResult]:
    """One SizingResult per workload of the estate, from the analysis workbook.  Empty if the workbook was not imported."""
    if not aws.per_server:
        return {}
    slug = recommended_slug(aws)
    excluded = {short_name(e["name"]): e["reason"] for e in aws.excluded if e.get("type", "").lower() == "server"}
    out: dict[str, SizingResult] = {}
    for w in estate.workloads:
        key = short_name(w.name)
        rec = aws.per_server.get(key)
        if rec is None or rec.get("ambiguous"):
            why = ("excluded by AWS Transform: " + excluded[key]) if key in excluded else (
                "two servers share this short name, so AWS's row cannot be matched" if rec else "not in AWS Transform's per-server results")
            out[w.name] = SizingResult(w.name, False, "aws_transform", "low", flags=["not_in_aws_scope"], notes=[why])
            continue
        model = rec["models"].get(slug) or rec["models"].get("on_demand") or next(iter(rec["models"].values()), None)
        if not model or not model.get("instance"):
            out[w.name] = SizingResult(w.name, False, "aws_transform", "low", flags=["no_aws_instance"], notes=["AWS Transform gave no instance for this server"])
            continue
        cur_v = w.vcpu if w.vcpu is not None else rec.get("vcpu")
        cur_r = w.ram_gb if w.ram_gb is not None else rec.get("ram_gb")
        util = rec.get("util") or {}
        pc, pr = util.get("peak_cpu"), util.get("peak_ram")
        req_v = round(cur_v * pc, 2) if (cur_v and pc is not None) else None
        req_m = round(cur_r * pr, 2) if (cur_r and pr is not None) else None
        rv, rm = model.get("ec2_vcpu"), model.get("ec2_ram_gb")
        if (req_v is not None and cur_v and req_v > cur_v) or (req_m is not None and cur_r and req_m > cur_r):
            direction = "upsize"
        elif (cur_v and rv is not None and rv < cur_v * 0.7) or (cur_r and rm is not None and rm < cur_r * 0.7):
            direction = "downsize"
        else:
            direction = "same"
        util_issue = any("utilization" in str(i.get("reason", "")).lower() for i in rec.get("issues", []))
        flags = ["aws_transform"] + (["default_utilization"] if util_issue else [])
        notes = [f"AWS Transform recommendation for the {slug.replace('_', ' ')} model"
                 + (f"; utilization used: CPU {pc:.0%}" if pc is not None else "") + (f", memory {pr:.0%}" if pr is not None else "")
                 + (" (AWS used defaults: the inventory has no measured utilization)" if util_issue else "")]
        inst = str(model["instance"])
        out[w.name] = SizingResult(w.name, True, "aws_transform", "low" if util_issue else "high", cur_v, cur_r, req_v, req_m, inst,
                                   int(rv) if rv is not None else None, rm, inst.split(".")[0].startswith(("t2", "t3", "t3a", "t4g")), direction, flags, notes)
    return out
