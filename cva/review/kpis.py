"""KPIs from the audit log and the run state (SOW A01: turnaround, analyst hours, first-pass acceptance,
OLA-to-MAP).  Only what can really be measured is computed; the rest says so."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from ..util import now_iso

TARGETS = {"turnaround_hours_max": 72, "analyst_hours_max": 3.2, "first_pass_min_pct": 90, "ola_to_map_min_pct": 30}
BASELINE = {"turnaround_days": 14, "analyst_hours": 16, "ola_to_map_pct": "15-20"}


def _hours(a: str, b: str) -> float:
    return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 3600.0


def run_kpis(entries: list[dict[str, Any]], state: dict[str, Any]) -> dict[str, Any]:
    first = next((e["ts"] for e in entries if e["event"] == "run_created"), None)
    fin = state.get("finalised")
    end = fin["ts"] if fin else now_iso()
    turnaround = round(_hours(first, end), 2) if first else None
    hours = round(sum(h["hours"] for h in state["hours"]), 2)
    props = state["proposals"]
    dec = [p for p in props if p["status"] in ("accepted", "rejected")]
    calls = [e for e in entries if e["event"] == "llm_call"]
    tok_in = sum(e["data"].get("input_tokens", 0) for e in calls)
    tok_out = sum(e["data"].get("output_tokens", 0) for e in calls)
    return {
        "turnaround_hours": turnaround, "turnaround_final": bool(fin),
        "turnaround_vs_target": None if turnaround is None else turnaround <= TARGETS["turnaround_hours_max"],
        "analyst_hours_logged": hours if state["hours"] else None,
        "analyst_hours_vs_target": (hours <= TARGETS["analyst_hours_max"]) if state["hours"] else None,
        "first_pass_accepted": None if not fin else fin.get("rework_rounds", 0) == 0,
        "rework_rounds": None if not fin else fin.get("rework_rounds", 0),
        "proposals": {"total": len(props), "accepted": sum(1 for p in props if p["status"] == "accepted"),
                      "rejected": sum(1 for p in props if p["status"] == "rejected"),
                      "pending": sum(1 for p in props if p["status"] == "pending"),
                      "acceptance_pct": round(100.0 * sum(1 for p in dec if p["status"] == "accepted") / len(dec), 1) if dec else None},
        "overrides": len(state["sizing_overrides"]), "assumptions": len(state["assumptions"]),
        "llm_calls": len(calls), "tokens_in": tok_in, "tokens_out": tok_out,
        "ola_to_map": state["outcomes"].get("map", "not measurable in the pilot window; record the outcome when known"),
        "targets": TARGETS, "baseline": BASELINE,
        "note": "Turnaround is measured from run creation, not from when Cloudamize data was received; analyst "
                "hours must be logged by a person; first-pass acceptance is a single yes/no per run (the 90% "
                "target applies across runs).",
    }


def portfolio(runs: list[dict[str, Any]]) -> dict[str, Any]:
    fin = [r for r in runs if r["first_pass_accepted"] is not None]
    return {"runs": len(runs), "finalised": len(fin),
            "first_pass_pct": round(100.0 * sum(1 for r in fin if r["first_pass_accepted"]) / len(fin), 1) if fin else None,
            "median_turnaround_hours": sorted(r["turnaround_hours"] for r in fin if r["turnaround_hours"] is not None)[len(fin) // 2] if fin else None}
