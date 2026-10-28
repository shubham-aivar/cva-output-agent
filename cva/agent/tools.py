"""The agent's tools.

Two kinds, and only two:

* READ tools look at the deterministic result (estate, sizing, pricing, findings, notes, figures).
* PROPOSE tools append a suggestion to a list for a person to accept or reject.

No tool changes a figure, an input, a decision or a deliverable; a test fails if one is ever added.  Every
argument is validated against a schema, quotes from notes are verified word for word, and notes and spreadsheet
text are returned flagged as untrusted data.
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Any, Callable

from ..engines import licensing as lic
from ..outputs.scenario_view import rows as scen_rows
from ..pipeline import PipelineResult
from ..util import norm
from .checker import allowed_numbers, check_text
from .notes import Notes

SLIDES = ["executive_summary", "cost_comparison", "rightsizing", "licensing", "lifecycle", "data_quality"]
TASK_TOOLS: dict[str, list[str]] = {
    "intake": ["get_run_summary", "get_intake_report", "propose_mapping"],
    "licensing": ["get_run_summary", "get_workload", "list_licensing_gaps", "list_notes", "search_notes", "read_note",
                  "propose_licensing_request", "propose_licensing_assumption"],
    "parity": ["get_run_summary", "get_workload", "list_parity_outliers", "compare_instances",
               "propose_override", "propose_review_flag"],
    "narrative": ["get_run_summary", "list_figures", "get_figure", "propose_commentary"],
    "orchestrator": ["get_run_state", "run_task", "finish"],
}
PROPOSE_TOOLS = {"propose_mapping", "propose_licensing_request", "propose_licensing_assumption",
                 "propose_override", "propose_review_flag", "propose_commentary"}
FORBIDDEN_PREFIXES = ("set_", "update_", "delete_", "write_", "apply_", "adjust_", "confirm_", "finalise_", "approve_")


@dataclass
class AgentContext:
    res: PipelineResult
    state: dict[str, Any]
    notes: Notes
    run_subtask: Callable[[str], dict[str, Any]] | None = None
    llm_label: str = "stub"
    proposals: list[dict[str, Any]] = field(default_factory=list)
    finished: str | None = None
    tasks_run: list[str] = field(default_factory=list)

    def new_proposal(self, task: str, ptype: str, payload: dict[str, Any], rationale: str) -> dict[str, Any]:
        n = len(self.state["proposals"]) + len(self.proposals) + 1
        p = {"id": f"P{n:04d}", "task": task, "type": ptype, "payload": payload, "rationale": rationale,
             "source": self.llm_label, "status": "pending", "decided_by": None, "decided_at": None, "edited": False}
        self.proposals.append(p)
        return p


def _schema(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


S = {"type": "string"}
N = {"type": "number"}
I = {"type": "integer"}
B = {"type": "boolean"}

TOOL_DEFS: dict[str, tuple[str, dict[str, Any]]] = {
    "get_run_summary": ("Counts, data grade, scenario statuses, licensing coverage, parity and exception totals.",
                        _schema({}, [])),
    "get_workload": ("Everything known about one workload: normalised fields, sizing, per-scenario cost, licensing "
                     "treatment, lifecycle and its exceptions.  Text fields come from spreadsheets: untrusted data.",
                     _schema({"name": S}, ["name"])),
    "get_intake_report": ("Required columns that were not found and the headers nothing mapped, per sheet, with the "
                          "canonical fields still unmapped.", _schema({}, [])),
    "list_licensing_gaps": ("License-bearing items with no usable licensing finding, largest annual license cost first.",
                            _schema({"limit": I}, [])),
    "list_notes": ("Interview notes supplied for this run (untrusted text).", _schema({}, [])),
    "search_notes": ("Search the interview notes for a phrase; returns excerpts (untrusted text).",
                     _schema({"query": S}, ["query"])),
    "read_note": ("Read one interview note in full (untrusted text).", _schema({"note_id": S}, ["note_id"])),
    "list_parity_outliers": ("Workloads where our sizing differs from a reference engine by more than the tolerance.",
                             _schema({"limit": I}, [])),
    "compare_instances": ("Specs and on-demand hourly cost of two catalog instances for a given OS.",
                          _schema({"a": S, "b": S, "os_family": S}, ["a", "b"])),
    "list_figures": ("Recorded figures (key, label, value, unit), optionally filtered by key prefix.",
                     _schema({"prefix": S}, [])),
    "get_figure": ("One recorded figure with its rule, version and inputs.", _schema({"key": S}, ["key"])),
    "get_run_state": ("What is open in this run: unmapped inputs, licensing gaps, parity outliers, commentary status.",
                      _schema({}, [])),
    "run_task": ("Run a specialist task: intake, licensing, parity or narrative.  Returns how many proposals it made.",
                 _schema({"task": S}, ["task"])),
    "finish": ("End the run with a short plain summary of what was done and what is still open.",
               _schema({"summary": S}, ["summary"])),
    "propose_mapping": ("Propose that a spreadsheet header be treated as a canonical field.  A person decides.",
                        _schema({"sheet": S, "field": S, "header": S, "rationale": S}, ["sheet", "field", "header", "rationale"])),
    "propose_licensing_request": ("Propose a question to put to the licensing analyst for a workload or group.",
                                  _schema({"workload": S, "product": S, "question": S, "priority": S,
                                           "rationale": S}, ["workload", "product", "question", "rationale"])),
    "propose_licensing_assumption": (
        "Propose a licensing finding backed by a verbatim quote from an interview note.  It is labelled an "
        "assumption, never evidence, and a person decides.",
        _schema({"workload": S, "product": S, "instance": S, "model": S, "active_sa": B, "tenancy": S,
                 "note_id": S, "quote": S, "rationale": S},
                ["workload", "product", "model", "note_id", "quote", "rationale"])),
    "propose_override": ("Propose a different instance for one workload, with the reason.  A person decides.",
                         _schema({"workload": S, "instance": S, "reason": S}, ["workload", "instance", "reason"])),
    "propose_review_flag": ("Flag a workload for the reviewer's attention with a short note.",
                            _schema({"workload": S, "text": S}, ["workload", "text"])),
    "propose_commentary": ("Propose slide commentary.  Every number must be a recorded figure and the text must "
                           "contain no advice; otherwise it is rejected with the reasons.",
                           _schema({"slide": S, "text": S}, ["slide", "text"])),
}


def _validate(args: dict[str, Any], schema: dict[str, Any]) -> str | None:
    if not isinstance(args, dict):
        return "arguments must be an object"
    for r in schema.get("required", []):
        if r not in args:
            return f"missing required argument '{r}'"
    for k, v in args.items():
        spec = schema["properties"].get(k)
        if spec is None:
            return f"unexpected argument '{k}'"
        t = spec["type"]
        ok = ((t == "string" and isinstance(v, str)) or (t == "boolean" and isinstance(v, bool)) or
              (t == "integer" and isinstance(v, int) and not isinstance(v, bool)) or
              (t == "number" and isinstance(v, (int, float)) and not isinstance(v, bool)))
        if not ok:
            return f"argument '{k}' must be of type {t}"
    return None


class ToolBox:
    def __init__(self, ctx: AgentContext, task: str) -> None:
        self.ctx = ctx
        self.task = task
        self.allowed = TASK_TOOLS[task]
        self.calls = 0

    def specs(self) -> list[dict[str, Any]]:
        return [{"toolSpec": {"name": n, "description": TOOL_DEFS[n][0], "inputSchema": {"json": TOOL_DEFS[n][1]}}}
                for n in self.allowed]

    def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        if name not in self.allowed or name not in TOOL_DEFS:
            return {"error": f"unknown or unavailable tool '{name}' for task '{self.task}'"}
        err = _validate(args, TOOL_DEFS[name][1])
        if err:
            return {"error": err}
        try:
            return getattr(self, f"t_{name}")(**args)
        except Exception as e:  # tool bugs must not crash the loop
            return {"error": f"tool failed: {type(e).__name__}: {e}"}

    # ------------------------------------------------------------------ helpers
    @property
    def res(self) -> PipelineResult:
        return self.ctx.res

    def _w(self, name: str):
        return self.res.estate.by_key().get(norm(name))

    # ------------------------------------------------------------------ read tools
    def t_get_run_summary(self) -> dict[str, Any]:
        r = self.res
        from ..engines import quality
        return {"workloads": len(r.estate.workloads), "source": r.estate.source_kind, "data_grade": r.meta["richness"],
                "figures_source": r.meta["figures_source"], "offline_estimate_ran": r.estimate_ran,
                "scenarios": [{"id": s.id, "label": s.label, "status": s.status, "total_usd_per_year": s.total,
                               "coverage_pct": s.coverage_pct, "source": s.source} for s in scen_rows(r)],
                "licensing": {"mode": r.licensing.mode, **r.lic_cov}, "parity": r.parity_summary,
                "exceptions": {k: v for k, v in quality.summary(r.issues).items() if k != "by_code"},
                "price_catalog_placeholder": r.pricing.placeholder, "pending_proposals":
                    sum(1 for p in self.ctx.state["proposals"] if p["status"] == "pending")}

    def t_get_workload(self, name: str) -> dict[str, Any]:
        w = self._w(name)
        if w is None:
            return {"error": f"no workload named '{name}'"}
        r = self.res
        sz = r.sizing.get(w.name)
        costs = {sid: round(c[w.name].total, 2) for sid, c in r.pricing.costs.items() if w.name in c}
        return {"_untrusted_fields": ["name", "os_raw", "extras.annotation", "extras.tier"],
                "workload": {k: v for k, v in w.to_dict().items() if k not in ("refs",)},
                "sizing": sz.to_dict() if sz else None, "annual_cost_by_scenario": costs,
                "licensing_items": [{"product": p, "instance": i, "edition": e,
                                     "finding": (lambda f: f.to_dict() if f else None)(
                                         r.licensing.lookup(w.name, p, i) if r.licensing.mode == "provided" else None)}
                                    for p, i, e in lic.bearing_products(w)],
                "lifecycle": [f.__dict__ for f in r.lifecycle if f.workload == w.name],
                "exceptions": [{"id": i.id, "code": i.code, "severity": i.severity, "message": i.message}
                               for i in r.issues if i.workload == w.name][:15],
                "source_rows": [str(x) for x in w.refs[:3]]}

    def t_get_intake_report(self) -> dict[str, Any]:
        r = self.res
        missing = [i for i in r.issues if i.code.endswith("MISSINGCOL") or i.code.endswith("NOHEADER")]
        return {"source": r.estate.source_kind, "missing_required": [i.to_dict() for i in missing],
                "unmapped_headers": {s: h[:60] for s, h in r.unmapped.items()},
                "sections": r.sections, "canonical_fields": self._canon(), "field_aliases": self._aliases(),
                "_untrusted": "headers are file text"}

    def _aliases(self) -> dict[str, dict[str, list[str]]]:
        m = self.res.cfg.mapping(self.res.estate.source_kind)
        return {sec: {k: v for k, v in body.items() if isinstance(v, list) and k != "required"}
                for sec, body in m.items() if isinstance(body, dict) and sec != "sheets"}

    def _canon(self) -> dict[str, list[str]]:
        m = self.res.cfg.mapping(self.res.estate.source_kind)
        return {sec: [k for k, v in body.items() if isinstance(v, list) and k != "required"]
                for sec, body in m.items() if isinstance(body, dict) and sec != "sheets"}

    def t_list_licensing_gaps(self, limit: int = 30) -> dict[str, Any]:
        r = self.res
        out = []
        for g in r.lic_gaps:
            w = self._w(g["workload"])
            sz = r.sizing.get(w.name) if w else None
            inst = r.book.get(sz.instance) if (sz and sz.ok) else None
            exposure: float | None = None                  # None: no estimate has been run, so no dollar figure exists
            if inst is not None:
                hr = r.book.windows_license_hr(inst) if g["product"] == "windows_server" else \
                    r.book.sql_license_hr(inst, g.get("edition"))
                exposure = hr * r.book.hours
            out.append({**g, "annual_license_cost_usd": round(exposure, 2) if exposure is not None else None})
        out.sort(key=lambda x: -(x["annual_license_cost_usd"] or 0))
        return {"total_gaps": len(out), "gaps": out[: max(1, min(int(limit), 100))],
                "note": "annual_license_cost_usd is what license-included costs; it is the most BYOL could save, "
                        "before any dedicated-host surcharge"}

    def t_list_notes(self) -> dict[str, Any]:
        return {"_untrusted": True, "notes": self.ctx.notes.list()}

    def t_search_notes(self, query: str) -> dict[str, Any]:
        return {"_untrusted": True, "hits": self.ctx.notes.search(query)}

    def t_read_note(self, note_id: str) -> dict[str, Any]:
        n = self.ctx.notes.get(note_id)
        if n is None:
            return {"error": f"no note '{note_id}'"}
        return {"_untrusted": True, "note": {"id": n.id, "title": n.title, "text": n.text[:6000]}}

    def t_list_parity_outliers(self, limit: int = 20) -> dict[str, Any]:
        rows = [r for r in self.res.parity_rows if r.status in ("ours_cheaper", "ours_costlier")]
        rows.sort(key=lambda r: -abs(r.diff_pct or 0))
        return {"tolerance_pct": self.res.parity_summary["tolerance_pct"], "total_outliers": len(rows),
                "outliers": [r.to_dict() for r in rows[: max(1, min(int(limit), 100))]]}

    def t_compare_instances(self, a: str, b: str, os_family: str = "linux") -> dict[str, Any]:
        out = {}
        for k, n in (("a", a), ("b", b)):
            i = self.res.book.get(n)
            if i is None:
                return {"error": f"instance '{n}' is not in the price catalog"}
            out[k] = {"name": i.name, "vcpu": i.vcpu, "mem_gb": i.mem_gb, "burstable": i.burstable,
                      "hourly_usd": round(self.res.book.licensed_total_hr(i, os_family, []), 4)}
        return out

    def t_list_figures(self, prefix: str = "") -> dict[str, Any]:
        return {"figures": [{"key": f.key, "label": f.label, "value": f.value, "unit": f.unit}
                            for f in self.res.ledger.all() if f.key.startswith(prefix)]}

    def t_get_figure(self, key: str) -> dict[str, Any]:
        f = self.res.ledger.get(key)
        return f.to_dict() if f else {"error": f"no figure '{key}'"}

    def t_get_run_state(self) -> dict[str, Any]:
        r, st = self.res, self.ctx.state
        missing = [i for i in r.issues if i.code.endswith("MISSINGCOL") or i.code.endswith("NOHEADER")]
        return {"needs_intake": bool(missing) and not any(p["task"] == "intake" for p in st["proposals"]),
                "licensing_gaps": r.lic_cov["gaps"], "licensing_mode": r.licensing.mode,
                "licensing_task_done": "licensing" in self.ctx.tasks_run or any(p["task"] == "licensing" for p in st["proposals"]),
                "parity_outliers": r.parity_summary.get("outliers", 0),
                "parity_task_done": "parity" in self.ctx.tasks_run or any(p["task"] == "parity" for p in st["proposals"]),
                "commentary_slides_accepted": sorted(st.get("commentary", {})),
                "narrative_task_done": "narrative" in self.ctx.tasks_run or any(p["task"] == "narrative" for p in st["proposals"]),
                "tasks_run_this_session": list(self.ctx.tasks_run)}

    def t_run_task(self, task: str) -> dict[str, Any]:
        if task not in ("intake", "licensing", "parity", "narrative"):
            return {"error": "task must be one of intake, licensing, parity, narrative"}
        if task in self.ctx.tasks_run:
            return {"error": f"task '{task}' already ran in this session"}
        if self.ctx.run_subtask is None:
            return {"error": "sub-tasks are not available in this context"}
        self.ctx.tasks_run.append(task)
        return self.ctx.run_subtask(task)

    def t_finish(self, summary: str) -> dict[str, Any]:
        self.ctx.finished = summary[:1500]
        return {"ok": True}

    # ------------------------------------------------------------------ propose tools
    def t_propose_mapping(self, sheet: str, field: str, header: str, rationale: str) -> dict[str, Any]:
        r = self.res
        section = r.sections.get(sheet)
        if section is None:
            return {"error": f"sheet '{sheet}' is not a mapped sheet; known: {sorted(r.sections)}"}
        canon = self._canon().get(section, [])
        if field not in canon:
            return {"error": f"'{field}' is not a canonical field of section '{section}'; choose from {canon}"}
        if header not in r.unmapped.get(sheet, []):
            return {"error": f"'{header}' is not an unmapped header of sheet '{sheet}'"}
        p = self.ctx.new_proposal("intake", "mapping", {"source": r.estate.source_kind, "sheet": sheet,
                                                         "section": section, "field": field, "header": header}, rationale)
        return {"proposal": p["id"], "status": "pending"}

    def t_propose_licensing_request(self, workload: str, product: str, question: str, rationale: str,
                                    priority: str = "medium") -> dict[str, Any]:
        if product not in ("sql_server", "windows_server"):
            return {"error": "product must be sql_server or windows_server"}
        p = self.ctx.new_proposal("licensing", "licensing_request",
                                  {"workload": workload, "product": product, "question": question[:600],
                                   "priority": priority}, rationale)
        return {"proposal": p["id"], "status": "pending"}

    def t_propose_licensing_assumption(self, workload: str, product: str, model: str, note_id: str, quote: str,
                                       rationale: str, instance: str = "", active_sa: bool = False,
                                       tenancy: str = "") -> dict[str, Any]:
        w = self._w(workload)
        if w is None:
            return {"error": f"no workload named '{workload}'"}
        if product not in ("sql_server", "windows_server") or model not in ("byol", "li"):
            return {"error": "product must be sql_server|windows_server and model byol|li"}
        if not self.ctx.notes.verify_quote(note_id, quote):
            return {"error": "the quote was not found word for word in that note; assumptions need verbatim evidence"}
        ten = tenancy or ("dedicated" if product == "windows_server" else "shared" if active_sa else "")
        p = self.ctx.new_proposal(
            "licensing", "licensing_assumption",
            {"workload": w.name, "product": product, "instance": instance or None, "model": model,
             "active_sa": bool(active_sa), "tenancy": ten or "unknown", "licensed_by": "cores",
             "basis": f"ASSUMPTION from note {note_id}: \u201c{quote.strip()[:240]}\u201d", "note_id": note_id,
             "quote": quote.strip()[:240], "risk": "optimistic" if model == "byol" else "conservative"}, rationale)
        return {"proposal": p["id"], "status": "pending (labelled an assumption, not evidence)"}

    def t_propose_override(self, workload: str, instance: str, reason: str) -> dict[str, Any]:
        w = self._w(workload)
        if w is None:
            return {"error": f"no workload named '{workload}'"}
        if self.res.book.get(instance) is None:
            return {"error": f"instance '{instance}' is not in the price catalog"}
        if len(reason.strip()) < 20:
            return {"error": "give a reason of at least 20 characters that cites the data"}
        sz = self.res.sizing.get(w.name)
        p = self.ctx.new_proposal("parity", "override", {"workload": w.name, "instance": instance,
                                                          "from_instance": sz.instance if sz else None,
                                                          "reason": reason.strip()[:500]}, reason.strip()[:500])
        return {"proposal": p["id"], "status": "pending"}

    def t_propose_review_flag(self, workload: str, text: str) -> dict[str, Any]:
        w = self._w(workload)
        if w is None:
            return {"error": f"no workload named '{workload}'"}
        p = self.ctx.new_proposal("parity", "review_flag", {"workload": w.name, "text": text.strip()[:400]},
                                  text.strip()[:400])
        return {"proposal": p["id"], "status": "pending"}

    def t_propose_commentary(self, slide: str, text: str) -> dict[str, Any]:
        if slide not in SLIDES:
            return {"error": f"slide must be one of {SLIDES}"}
        problems = check_text(text, allowed_numbers(self.res.ledger.all()))
        if problems:
            return {"accepted": False, "problems": problems,
                    "hint": "use only recorded figures (list_figures), state facts, and give no advice"}
        p = self.ctx.new_proposal("narrative", "commentary", {"slide": slide, "text": text.strip()}, "checked against the figure ledger")
        return {"accepted": True, "proposal": p["id"], "status": "pending"}


def best_header_match(field_name: str, aliases: list[str], headers: list[str]) -> tuple[str | None, float]:
    """Used by the offline stub: the unmapped header closest to a canonical field or one of its aliases.

    Scores spelling similarity and shared words together, and returns no match when the best two candidates are
    too close to call (a wrong mapping is worse than none: the agent should say it cannot tell)."""
    targets = [norm(field_name.replace("_", " "))] + [norm(a) for a in aliases]
    scored: list[tuple[float, str]] = []
    for h in headers:
        nh = norm(h)
        best = 0.0
        for t in targets:
            ratio = difflib.SequenceMatcher(None, nh, t).ratio()
            a, b = set(nh.split()), set(t.split())
            jac = len(a & b) / len(a | b) if (a | b) else 0.0
            best = max(best, 0.5 * ratio + 0.5 * jac)
        scored.append((best, h))
    scored.sort(reverse=True)
    if not scored:
        return None, 0.0
    if len(scored) > 1 and scored[0][0] - scored[1][0] < 0.05:
        return None, scored[0][0]
    return scored[0][1], scored[0][0]


def assert_no_mutating_tools() -> None:
    bad = [n for n in TOOL_DEFS if n.startswith(FORBIDDEN_PREFIXES)]
    assert not bad, f"mutating tools are not allowed: {bad}"
    unknown = [n for n in TOOL_DEFS if not n.startswith(("get_", "list_", "search_", "read_", "compare_", "propose_",
                                                         "run_task", "finish"))]
    assert not unknown, f"unclassified tools: {unknown}"
