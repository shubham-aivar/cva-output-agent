"""Model layer.

Messages use the Amazon Bedrock Converse shape so the real model needs no translation.

* ``BedrockLLM``   the real model (Converse API with tool use).  Written to the documented API; NOT yet exercised
                   against a real account in this build (see docs/STATUS.md).
* ``StubLLM``      an offline stand-in so the demo and tests run on a laptop.  It follows a fixed, readable plan
                   per task.  It is NOT an agent and everything it writes is labelled "stub".
* ``ScriptedLLM``  returns pre-written responses; used by tests to prove the guardrails (for example that a
                   model which obeys an injected instruction still cannot change a figure).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Generator, Protocol

from .tools import best_header_match


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class LLMResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = "end_turn"          # end_turn | tool_use | max_tokens | error
    input_tokens: int = 0
    output_tokens: int = 0

    def to_message(self) -> dict[str, Any]:
        content: list[dict[str, Any]] = []
        if self.text:
            content.append({"text": self.text})
        for t in self.tool_calls:
            content.append({"toolUse": {"toolUseId": t.id, "name": t.name, "input": t.input}})
        return {"role": "assistant", "content": content or [{"text": ""}]}


class LLM(Protocol):
    name: str
    real: bool

    def converse(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
                 max_tokens: int) -> LLMResponse: ...


# --------------------------------------------------------------------------------------------- Bedrock
class BedrockLLM:
    real = True

    def __init__(self, model_id: str, region: str = "us-east-1", profile: str | None = None) -> None:
        import boto3  # imported lazily: boto3 is only needed for the real model
        sess = boto3.Session(profile_name=profile) if profile else boto3.Session()
        self.client = sess.client("bedrock-runtime", region_name=region)
        self.model_id = model_id
        self.name = "model"

    def converse(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
                 max_tokens: int) -> LLMResponse:
        kwargs: dict[str, Any] = {"modelId": self.model_id, "messages": messages,
                                  "system": [{"text": system}], "inferenceConfig": {"maxTokens": max_tokens}}
        if tools:
            kwargs["toolConfig"] = {"tools": tools}
        out = self.client.converse(**kwargs)
        blocks = out["output"]["message"]["content"]
        text = "".join(b.get("text", "") for b in blocks)
        calls = [ToolCall(b["toolUse"]["toolUseId"], b["toolUse"]["name"], b["toolUse"].get("input") or {})
                 for b in blocks if "toolUse" in b]
        u = out.get("usage", {})
        return LLMResponse(text, calls, out.get("stopReason", "end_turn"), u.get("inputTokens", 0), u.get("outputTokens", 0))


# --------------------------------------------------------------------------------------------- scripted
class ScriptedLLM:
    real = False
    name = "scripted"

    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = list(responses)
        self.seen: list[list[dict[str, Any]]] = []

    def converse(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
                 max_tokens: int) -> LLMResponse:
        self.seen.append(messages)
        if not self.responses:
            return LLMResponse("done")
        return self.responses.pop(0)


# --------------------------------------------------------------------------------------------- stub
def _pair_results(messages: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    calls: dict[str, tuple[str, dict[str, Any]]] = {}
    out: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for m in messages:
        for b in m.get("content", []):
            if "toolUse" in b:
                tu = b["toolUse"]
                calls[tu["toolUseId"]] = (tu["name"], tu.get("input") or {})
            elif "toolResult" in b:
                tr = b["toolResult"]
                name, args = calls.get(tr["toolUseId"], ("?", {}))
                payload: dict[str, Any] = {}
                for c in tr.get("content", []):
                    if "json" in c:
                        payload = c["json"]
                    elif "text" in c:
                        try:
                            payload = json.loads(c["text"])
                        except ValueError:
                            payload = {"text": c["text"]}
                out.append((name, args, payload))
    return out


Plan = Generator[tuple[str, dict[str, Any]], dict[str, Any], str]


def _plan_licensing() -> Plan:
    yield ("get_run_summary", {})
    gaps = (yield ("list_licensing_gaps", {"limit": 40})).get("gaps", [])
    notes = (yield ("list_notes", {})).get("notes", [])
    made = 0
    asked: set[tuple[str, str | None]] = set()
    if notes:
        seen_w: list[str] = []
        for g in gaps:
            if g["product"] == "sql_server" and g["workload"] not in seen_w:
                seen_w.append(g["workload"])
        for wl in seen_w[:8]:
            hits = (yield ("search_notes", {"query": wl})).get("hits", [])
            for h in hits:
                ex = h["excerpt"]
                if wl.lower() in ex.lower() and re.search(r"software assurance", ex, re.I) and \
                        re.search(r"active|covered|current|until|valid", ex, re.I):
                    sent = next((s for s in re.split(r"(?<=[.!?])\s+", ex) if re.search(r"software assurance", s, re.I)), ex)
                    r = yield ("propose_licensing_assumption", {
                        "workload": wl, "product": "sql_server", "model": "byol", "active_sa": True,
                        "note_id": h["note"], "quote": sent.strip(),
                        "rationale": f"Interview note {h['note']} says Software Assurance is in place for {wl}. "
                                     "Assumption only; the licensing analyst must confirm."})
                    made += 1 if "proposal" in r else 0
                    break
    groups: dict[tuple[str, str | None], dict[str, Any]] = {}
    for g in gaps:
        k = (g["product"], g.get("edition"))
        d = groups.setdefault(k, {"n": 0, "usd": 0.0, "top": g["workload"]})
        d["n"] += 1
        d["usd"] += g.get("annual_license_cost_usd") or 0.0
    for (prod, ed), d in sorted(groups.items(), key=lambda kv: -kv[1]["usd"])[:4]:
        label = prod.replace("_", " ") + (f" {ed}" if ed else "")
        if (prod, ed) in asked:
            continue
        asked.add((prod, ed))
        yield ("propose_licensing_request", {
            "workload": d["top"], "product": prod, "priority": "high" if d["usd"] > 20000 else "medium",
            "question": f"Please confirm the licensing position for {d['n']} {label} item(s), largest first: "
                        f"{d['top']}. For each: license model (BYOL or license-included), whether Software "
                        "Assurance is active, and for Windows Server the original purchase date.",
            "rationale": f"{d['n']} item(s) with no finding; " + (f"license-included costs ${d['usd']:,.0f} a year in total, which is the most BYOL could save."
                                                                if d["usd"] else "no cost estimate exists yet, so the saving is not quantified.")})
    return f"Stub licensing pass: {made} evidence-backed assumption(s) and {len(asked)} request(s) proposed."


def _plan_parity() -> Plan:
    yield ("get_run_summary", {})
    out = (yield ("list_parity_outliers", {"limit": 25})).get("outliers", [])
    made = 0
    for o in out[:7]:
        if made >= 6:
            break
        w = (yield ("get_workload", {"name": o["workload"]}))
        wl = w.get("workload") or {}
        ref = o.get("ref_instance") or ""
        avg, peak = wl.get("cpu_avg"), wl.get("cpu_peak")
        pred = (wl.get("extras") or {}).get("cpu_pred")
        calm = ((avg is None or avg <= 30) and (peak is None or peak < 99)) or (pred is not None and pred <= 65)
        if o["status"] == "ours_costlier" and ref.startswith("t3") and calm:
            yield ("propose_override", {
                "workload": o["workload"], "instance": ref,
                "reason": f"Our sizing ({o['ours_instance']}) costs {abs(o['diff_pct'] or 0):.1f}% more than {ref}; CPU average is "
                          f"{avg if avg is not None else 'n/a'}%, peak {peak if peak is not None else 'n/a'}%, "
                          f"predicted on target hardware {pred if pred is not None else 'n/a'}%, so a burstable "
                          "instance is plausible. Reviewer to confirm the workload is not sustained."})
            made += 1
        elif o["status"] == "ours_cheaper":
            yield ("propose_review_flag", {
                "workload": o["workload"],
                "text": f"Our sizing ({o['ours_instance']}) is {abs(o['diff_pct'] or 0):.1f}% below the reference "
                        f"({ref}). Check memory, disk and network constraints before accepting the smaller size."})
            made += 1
    return f"Stub parity pass: {made} proposal(s) from {len(out)} outlier(s) examined."


def _plan_narrative() -> Plan:
    figs = {f["key"]: f for f in (yield ("list_figures", {})).get("figures", [])}

    def v(k: str) -> Any:
        return figs.get(k, {}).get("value")

    def money(k: str) -> str:
        x = v(k)
        return "not computed" if x is None else f"${x:,.0f}"

    status = {}
    summ = yield ("get_run_summary", {})
    for s in summ.get("scenarios", []):
        status[s["id"]] = s["status"]
    byol = {"computed": "BYOL is computed from the licensing input", "partial": "BYOL is partially computed",
            "not_computable": "BYOL cannot be computed without a licensing input",
            "not_in_assessment": "BYOL is not part of the AWS Transform assessment",
            "illustrative": "BYOL is an illustrative upper bound"}.get(status.get("byol", ""), "BYOL status unknown")
    has_sizing = "sizing.downsize" in figs
    sql_cov = v("licensing.coverage_sql")
    texts = {
        "executive_summary": f"{v('estate.workloads')} workloads were assessed. The data is graded {v('estate.data_grade')}. "
                             + (f"On-Demand costs {money('scenario.on_demand.total')} a year and the 3-year commitment scenario "
                                f"{money('scenario.ri_3yr.total')}. {byol}." if "scenario.on_demand.total" in figs else
                                "No cost figures exist yet: they come from an AWS Transform assessment."),
        "cost_comparison": (f"Annual totals: On-Demand {money('scenario.on_demand.total')}, 1-year commitment "
                            f"{money('scenario.ri_1yr.total')}, 3-year commitment {money('scenario.ri_3yr.total')}, BYOL "
                            f"{money('scenario.byol.total')}." if "scenario.on_demand.total" in figs else
                            "No cost figures exist yet: they come from an AWS Transform assessment."),
        "rightsizing": (f"{v('sizing.downsize')} workloads are over-provisioned, {v('sizing.same')} are close to right-sized "
                        f"and {v('sizing.upsize')} need more capacity. Sized capacity is {v('sizing.vcpu_rec')} vCPU "
                        f"against {v('sizing.vcpu_cur')} vCPU today. {v('sizing.conf_low')} sizings are low confidence."
                        if has_sizing else "Rightsizing figures are not available in this run: A01's offline estimate was not run and "
                                           "AWS Transform's per-server recommendations are not imported."),
        "licensing": (f"The licensing input covers {sql_cov}% of SQL Server and {v('licensing.coverage_windows')}% of Windows Server items; "
                      f"{v('licensing.gaps')} items have no finding. {byol}." if sql_cov is not None else
                      f"No SQL Server was detected in this input; the licensing input covers {v('licensing.coverage_windows')}% of Windows "
                      f"Server items and {v('licensing.gaps')} items have no finding. {byol}."),
        "lifecycle": f"{v('lifecycle.eos')} items are past end of support and {v('lifecycle.approaching')} are "
                     f"approaching it, affecting {v('lifecycle.at_risk')} workloads. Lifecycle dates are unverified.",
        "data_quality": f"{v('quality.total')} exceptions were raised: {v('quality.error')} errors, "
                        f"{v('quality.warn')} warnings and {v('quality.info')} informational.",
    }
    for slide, text in texts.items():
        yield ("propose_commentary", {"slide": slide, "text": text})
    return "Stub narrative pass: commentary drafted for each slide from recorded figures."


def _plan_intake() -> Plan:
    rep = yield ("get_intake_report", {})
    made = 0
    for m in rep.get("missing_required", []):
        fld = (m.get("detail") or {}).get("field")
        ref = (m.get("refs") or [""])[0]
        sheet = ref.split(":", 1)[-1].split("!")[0] if ":" in ref else ""
        headers = rep.get("unmapped_headers", {}).get(sheet, [])
        if not fld or not headers:
            continue
        section = rep.get("sections", {}).get(sheet, "")
        aliases = rep.get("field_aliases", {}).get(section, {}).get(fld, [])
        best, score = best_header_match(fld, aliases, headers)
        if best and score >= 0.5:
            r = yield ("propose_mapping", {"sheet": sheet, "field": fld, "header": best,
                                           "rationale": f"Header '{best}' is the closest match for '{fld}' "
                                                        f"(similarity {score:.2f}). A person must confirm."})
            made += 1 if "proposal" in r else 0
    return f"Stub intake pass: {made} mapping proposal(s)."


def _plan_orchestrator() -> Plan:
    st = yield ("get_run_state", {})
    if st.get("needs_intake"):
        yield ("run_task", {"task": "intake"})
    if st.get("licensing_gaps", 0) > 0 and not st.get("licensing_task_done"):
        yield ("run_task", {"task": "licensing"})
    if st.get("parity_outliers", 0) > 0 and not st.get("parity_task_done"):
        yield ("run_task", {"task": "parity"})
    if not st.get("narrative_task_done"):
        yield ("run_task", {"task": "narrative"})
    yield ("finish", {"summary": "Stub orchestration: ran the specialist tasks the run state called for; "
                                 "proposals are waiting for a reviewer."})
    return ""


PLANS = {"licensing": _plan_licensing, "parity": _plan_parity, "narrative": _plan_narrative,
         "intake": _plan_intake, "orchestrator": _plan_orchestrator}


class StubLLM:
    real = False
    name = "stub"

    def converse(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
                 max_tokens: int) -> LLMResponse:
        m = re.search(r"\[\[TASK:(\w+)\]\]", system)
        task = m.group(1) if m else "narrative"
        gen = PLANS[task]()
        results = _pair_results(messages)
        try:
            req = next(gen)
            for _name, _args, payload in results:
                req = gen.send(payload)
        except StopIteration as stop:
            return LLMResponse(text=str(stop.value or "done"), stop_reason="end_turn")
        name, args = req
        return LLMResponse(tool_calls=[ToolCall(f"stub-{len(results) + 1}", name, args)], stop_reason="tool_use")
