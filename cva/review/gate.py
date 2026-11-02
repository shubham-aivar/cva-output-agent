"""Release gate.  Nothing is "customer-ready" until a person has dealt with everything the gate lists.

The gate never approves anything itself: it reports what is still open, and ``finalise`` refuses while any
blocking item is open.  Acknowledging an exception is a recorded human decision, not a suppression."""
from __future__ import annotations

from typing import Any

from ..model import Issue
from ..pipeline import PipelineResult
from ..util import canonical_json, sha256_hex


def issue_key(i: Issue) -> str:
    return f"{i.code}|{i.workload or ''}"


def is_acked(i: Issue, state: dict[str, Any]) -> bool:
    return issue_key(i) in state["acks"] or i.code in state["ack_codes"]


def figure_digest(res: PipelineResult) -> str:
    return sha256_hex(canonical_json([[f.key, f.value] for f in res.ledger.all()]))


def evaluate(res: PipelineResult, state: dict[str, Any]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(cid: str, title: str, ok: bool, detail: str, blocking: bool = True) -> None:
        checks.append({"id": cid, "title": title, "ok": ok, "detail": detail, "blocking": blocking})

    errs = [i for i in res.issues if i.severity == "error"]
    warns = [i for i in res.issues if i.severity == "warn"]
    open_e = [i for i in errs if not is_acked(i, state)]
    open_w = [i for i in warns if not is_acked(i, state)]
    add("errors", "Serious data problems are resolved or accepted", not open_e,
        f"{len(open_e)} of {len(errs)} still open" if errs else "None found")
    add("warnings", "Data warnings have been reviewed", not open_w,
        f"{len(open_w)} of {len(warns)} still open" if warns else "None found")
    pend = [p for p in state["proposals"] if p["status"] == "pending"]
    add("proposals", "Agent suggestions are decided", not pend,
        f"{len(pend)} pending" if pend else f"{len(state['proposals'])} decided")
    out_n = res.parity_summary.get("outliers", 0)
    covered = out_n == 0 or bool(state["parity_reviewed"]) or \
        all(r.workload in state["sizing_overrides"] for r in res.parity_rows if r.status in ("ours_cheaper", "ours_costlier"))
    add("parity", "Cross-check differences are reviewed", covered,
        "No differences found" if out_n == 0 else (f"{out_n} difference(s) reviewed" if covered else f"{out_n} difference(s) not yet reviewed"))
    byol = res.pricing.scenario("byol")
    lic_needs_ack = (res.licensing.mode == "none" or res.lic_cov["sql_gaps"] > 0 or (byol and byol.status != "computed"))
    add("licensing", "The licensing position is accepted", (not lic_needs_ack) or bool(state["licensing_ack"]),
        "Complete" if not lic_needs_ack else
        ("acknowledged: " + (state["licensing_ack"] or {}).get("note", "")) if state["licensing_ack"] else
        f"BYOL status: {(byol.status if byol else 'n/a').replace('_', ' ')}; {res.lic_cov['sql_gaps']} SQL Server item(s) lack licensing evidence. A reviewer must accept this position.")
    if res.aws is not None:
        add("figures_source", "The source of the figures is settled", True, f"AWS Transform assessment '{res.aws.scenario_name}' imported", blocking=False)
    elif not res.estimate_ran:
        add("figures_source", "The source of the figures is settled",
            False, "no cost figures exist yet: run or import an AWS Transform assessment, or run A01's offline estimate (fallback)")
    else:
        ack = state.get("estimate_ack")
        add("figures_source", "The source of the figures is settled", bool(ack),
            ("accepted: " + ack.get("note", "")) if ack else
            "figures are A01's own OFFLINE estimate on placeholder prices: import an AWS Transform assessment, or a reviewer must accept the estimate")
    conf = state["confirmed"]
    digest = figure_digest(res)
    ok_conf = bool(conf) and conf.get("digest") == digest
    add("confirmed", "A reviewer has confirmed the figures", ok_conf,
        "confirmed by " + conf["by"] if ok_conf else
        ("figures changed since the confirmation; confirm again" if conf else "Not yet confirmed"))
    blocking_open = [c for c in checks if c["blocking"] and not c["ok"]]
    return {"ready": not blocking_open, "checks": checks, "open": len(blocking_open), "figure_digest": digest,
            "finalised": state["finalised"]}
