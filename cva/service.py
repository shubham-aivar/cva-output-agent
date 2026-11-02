"""Every operation of the agent, used by the command line and the demo UI alike.

Design rule: results are always re-derived from inputs + recorded decisions (state), never stored as truth.
A reviewer's decision (accept a proposal, acknowledge an exception, confirm figures, finalise) is state plus an
audit entry; recomputing then gives the same numbers again.  A finalised run is locked until reopened with a reason.
"""
from __future__ import annotations

import contextlib
import csv
import json
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .agent.checker import allowed_numbers, check_text
from .agent.llm import LLM, BedrockLLM, StubLLM
from .agent.notes import Notes
from .agent.tasks import TASKS, orchestrate, run_task
from .agent.tools import AgentContext
from .engines import quality
from .ingest.detect import detect_kind, profile_file
from .outputs.narrative import commentary_for
from .outputs.pdf import convert_to_pdf
from .outputs.pptx_deck import build_deck
from .outputs.scenario_view import rows as scen_rows
from .outputs.xlsx_trace import build_workbook
from .pipeline import PipelineResult, RunInputs, run_pipeline
from .review import gate as gate_mod
from .review.audit import AuditLog
from .review.kpis import portfolio, run_kpis
from .review.store import RunStore
from .transform.adapter import ConsoleFallbackAdapter, get_adapter
from .util import now_iso


def num_s(v: Any) -> str:
    return "?" if v in (None, "") else f"{float(v):,.0f}" if float(v) == int(float(v)) else f"{float(v):,.1f}"


class ServiceError(Exception):
    pass


def make_llm(mode: str, cfg) -> LLM:
    if mode == "bedrock":
        a = cfg.section("agent")
        return BedrockLLM(os.environ.get("CVA_MODEL_ID", a["model_id"]), os.environ.get("CVA_AWS_REGION", a["region"]),
                          os.environ.get("CVA_AWS_PROFILE") or None)
    return StubLLM()


class Service:
    def __init__(self, runs_dir: str | Path = "runs") -> None:
        self.store = RunStore(runs_dir)
        self._cache: dict[str, tuple[tuple[float, float], PipelineResult]] = {}
        self.lock = threading.RLock()                 # serialises state changes (the UI's background AWS Transform job shares it)
        self.transform_client_factory: Any = None     # tests / embedding: returns a tool client instead of starting the MCP server
        self.transform_sleep: Any = None

    # ------------------------------------------------------------------ plumbing
    def audit(self, rid: str) -> AuditLog:
        return AuditLog(self.store.path(rid) / "audit.jsonl")

    def _need(self, rid: str) -> None:
        if not self.store.exists(rid):
            raise ServiceError(f"no such run '{rid}'")

    def _open(self, rid: str) -> dict[str, Any]:
        self._need(rid)
        st = self.store.state(rid)
        if st["finalised"]:
            raise ServiceError("this run is finalised and locked; reopen it with a reason first")
        return st

    def result(self, rid: str) -> PipelineResult:
        self._need(rid)
        p = self.store.path(rid)
        key = ((p / "state.json").stat().st_mtime_ns, (p / "manifest.json").stat().st_mtime_ns)
        hit = self._cache.get(rid)
        if hit and hit[0] == key:
            return hit[1]
        res = run_pipeline(self.store.inputs_for(rid))
        self._cache[rid] = (key, res)
        return res

    def _notes(self, rid: str) -> Notes:
        return Notes.from_dir(self.store.path(rid) / "inputs" / "notes")

    # ------------------------------------------------------------------ runs
    def create_run(self, files: list[str], licensing: str | None = None, notes_dir: str | None = None,
                   label: str = "", collection_days: int | None = None, as_of: str | None = None,
                   customer_label: str | None = None, kind: str | None = None) -> str:
        for f in files + ([licensing] if licensing else []):
            if not Path(f).exists():
                raise ServiceError(f"file not found: {f}")
        inp = RunInputs(files=files, licensing_file=licensing, kind=kind, collection_days=collection_days,
                        as_of=as_of, customer_label=customer_label)
        rid = self.store.create(inp, label)
        if notes_dir and Path(notes_dir).exists():
            dst = self.store.path(rid) / "inputs" / "notes"
            shutil.copytree(notes_dir, dst)
        a = self.audit(rid)
        a.append("run_created", {"files": [Path(f).name for f in files], "licensing": Path(licensing).name if licensing else None,
                                 "detected": [detect_kind(f) for f in files], "label": label})
        try:
            res = self.result(rid)
            a.append("compute", {"workloads": len(res.estate.workloads), "ruleset": res.meta["ruleset"],
                                 "config_digest": res.meta["config_digest"], "figures": len(res.ledger.all()),
                                 "grade": res.meta["richness"]["grade"]})
        except Exception as e:
            a.append("compute_failed", {"error": f"{type(e).__name__}: {e}"})
            raise ServiceError(f"could not process the input: {e}") from e
        return rid

    def list_runs(self) -> list[dict[str, Any]]:
        out = []
        for rid in self.store.list_runs():
            m, st = self.store.manifest(rid), self.store.state(rid)
            out.append({"run_id": rid, "created": m["created"], "label": m.get("label", ""), "files": m["files"], "name": self.display_name(m),
                        "finalised": bool(st["finalised"])})
        return out

    # ------------------------------------------------------------------ names shown to people
    @staticmethod
    def display_name(m: dict[str, Any]) -> str:
        """What a run is called on screen: the customer, else the note, else the file's name.  The id (run-NNNN) stays the internal key."""
        n = str(m.get("customer_label") or "").strip() or str(m.get("label") or "").strip()
        if not n and m.get("files"):
            n = Path(str(m["files"][0])).stem.replace("_", " ").replace("-", " ").strip()
        return n or "Untitled assessment"

    def rename_run(self, rid: str, name: str, by: str) -> str:
        name = " ".join(str(name).split())
        if not by.strip():
            raise ServiceError("say who is renaming this (your name is recorded)")
        if not name or len(name) > 80:
            raise ServiceError("type a customer name of 1 to 80 characters")
        with self.lock:
            m = self.store.manifest(rid)
            old = m.get("customer_label")
            m["customer_label"] = name
            (self.store.path(rid) / "manifest.json").write_text(json.dumps(m, indent=2), encoding="utf-8")
        self.audit(rid).append("run_renamed", {"from": old, "to": name}, by)
        self._cache.pop(rid, None)
        return name

    def commitment_note(self, rid: str) -> str | None:
        """Honest wording for the 1-year / 3-year scenarios: what AWS Transform priced them as, per service."""
        res = self.result(rid)
        if not res.aws:
            return None
        txt = " ".join(str(x) for x in (res.aws.rds.get("notes") or [])) + " " + " ".join(str(v) for v in (res.aws.rds.get("overview") or {}).values())
        sp = "savings plan" in txt.lower()
        return ("Commitment scenarios are priced with No Upfront payment. For servers (Amazon EC2) AWS Transform uses Reserved Instances"
                + ("; for SQL Server databases (Amazon RDS) it applies Database Savings Plans." if sp else "."))

    # ------------------------------------------------------------------ views
    def overview(self, rid: str) -> dict[str, Any]:
        res, st = self.result(rid), self.store.state(rid)
        g = gate_mod.evaluate(res, st)
        led = {f.key: f.value for f in res.ledger.all()}
        return {
            "run_id": rid, "estimate_ran": res.estimate_ran, "sizing_source": res.sizing_source, "aws_servers": len(res.aws.per_server) if res.aws else 0, "manifest": self.store.manifest(rid), "name": self.display_name(self.store.manifest(rid)), "commitment_note": self.commitment_note(rid), "meta": res.meta, "gate": g,
            "finalised": st["finalised"], "estate": {"workloads": len(res.estate.workloads), "files": res.estate.files,
                                                      "basis": res.estate.data_basis, "days": res.estate.collection_days,
                                                      "notes": res.estate.notes},
            "scenarios": [s.to_dict() for s in scen_rows(res)], "figures": led,
            "aws": ({"scenario": res.aws.scenario_name, "job": res.aws.job, "files": list(res.aws.files), "servers": res.aws.servers,
                     "on_prem_total": res.aws.on_prem_total, "warning_rows": res.aws.warning_rows} if res.aws else None),
            "licensing": {"mode": res.licensing.mode, "provenance": res.licensing.provenance, **res.lic_cov},
            "lifecycle": [f.__dict__ for f in res.lifecycle if f.status != "supported"][:40],
            "parity": res.parity_summary, "exceptions": quality.summary(res.issues),
            "kpis": self.kpis(rid), "commentary": commentary_for(res, st),
            "transform": {"mode": st["transform"]["mode"], "status": st["transform"]["status"],
                          "servers": len(st["transform"].get("per_workload") or {})},
            "audit": self.audit(rid).verify(),
        }

    def run_offline_estimate(self, rid: str, by: str) -> dict[str, Any]:
        """Run A01's own sizing and pricing (fallback and cross-check).  Off by default: AWS Transform is the source of the figures."""
        if not by.strip():
            raise ServiceError("say who is running the offline estimate (your name is recorded)")
        with self.lock:
            st = self._open(rid)
            st["offline_estimate"] = True
            self.store.save_state(rid, st)
        self.audit(rid).append("offline_estimate_run", {"note": "A01's own sizing and pricing, placeholder price catalog"}, by)
        res = self.result(rid)
        return {"estimate_ran": res.estimate_ran, "scenarios": [s.to_dict() for s in scen_rows(res)]}

    def workloads(self, rid: str) -> list[dict[str, Any]]:
        from .transform.analysis import short_name
        res = self.result(rid)
        lc: dict[str, list[str]] = {}
        for f in res.lifecycle:
            if f.status != "supported":
                lc.setdefault(f.workload, []).append(f.status)
        have_aws = bool(res.aws and res.aws.per_server)
        out = []
        for w in res.estate.workloads:
            sz = res.sizing.get(w.name)
            od = res.pricing.costs.get("on_demand", {}).get(w.name)
            aw = None
            if have_aws:                              # AWS Transform's own per-server result, per pricing model; nothing recomputed
                rec = res.aws.per_server.get(short_name(w.name))
                z = res.aws_sizing.get(w.name)
                if rec and not rec.get("ambiguous") and z and z.ok:
                    aw = {"matched": True, "aws_id": rec["id"], "instance": z.instance, "vcpu": z.rec_vcpu, "ram_gb": z.rec_mem_gb, "direction": z.direction,
                          "confidence": z.confidence, "basis_note": "; ".join(z.notes), "region": rec.get("region"), "util": rec.get("util"),
                          "models": {slug: {"instance": m.get("instance"), "vcpu": m.get("ec2_vcpu"), "ram_gb": m.get("ec2_ram_gb"), "total": m.get("total"),
                                            "compute": m.get("compute"), "network": m.get("network"), "license": m.get("license")}
                                     for slug, m in rec["models"].items()},
                          "storage": rec.get("storage_annual"), "volumes": rec.get("volumes"), "issues": rec.get("issues", [])}
                else:
                    aw = {"matched": False, "reason": (z.notes[0] if z and z.notes else "not in AWS Transform's per-server results")}
            out.append({"name": w.name, "os": w.os_label or w.os_raw, "env": w.env, "vcpu": w.vcpu, "ram_gb": w.ram_gb,
                        "cpu_peak": w.cpu_peak, "sql": [s.edition for s in w.sql],
                        "instance": sz.instance if sz and sz.ok else None, "direction": sz.direction if sz else None,
                        "confidence": sz.confidence if sz else None, "basis": sz.basis if sz else None,
                        "on_demand": od.total if od else None, "lifecycle": lc.get(w.name, []),
                        "flags": sz.flags if sz else [], "estimate_ran": res.estimate_ran, "aws": aw, "sizing_source": res.sizing_source})
        return out

    def service_catalog(self, rid: str) -> dict[str, Any]:
        """The estate grouped by the AWS service each finding belongs to, with cost per service (AWS Transform's recommended pricing model)."""
        from .transform.analysis import short_name
        res = self.result(rid)
        rows = self.workloads(rid)
        a = res.aws
        rec = a.recommended if a else None
        slug = rec.slug if rec else "three_year_nuri"
        model_label = (rec.label if rec else None) or "recommended pricing model"
        rds = (a.rds.get("instances") if a else None) or []
        rds_by = {i.get("short"): i for i in rds}
        excl = {short_name(x.get("name", "").split("\\")[0]): x for x in (a.excluded if a else [])}
        svc: dict[str, dict[str, Any]] = {}

        def bucket(key: str, name: str, blurb: str) -> dict[str, Any]:
            return svc.setdefault(key, {"key": key, "name": name, "blurb": blurb, "items": [], "annual": None, "count": 0})

        for r in rows:
            sn = short_name(r["name"])
            z = r.get("aws") or {}
            lc = r.get("lifecycle") or []
            if sn in rds_by:
                i = rds_by[sn]
                m = (i.get("models") or {}).get(slug) or next(iter((i.get("models") or {}).values()), {})
                cost = sum(m.get(k) or 0 for k in ("compute", "license", "storage", "backup", "sa")) if m else None
                bucket("rds", "Amazon RDS for SQL Server", "Managed SQL Server databases (before AWS credits)")["items"].append({
                    "name": r["name"], "env": i.get("env") or r.get("env"), "now": f"{num_s(i.get('src_vcpus') or r.get('vcpu'))} vCPU / {num_s(i.get('src_ram_gb') or r.get('ram_gb'))} GB",
                    "target": i.get("target_class") or "—", "detail": " ".join(x for x in [str(i.get("edition") or ""), f"peak CPU {round(i['peak_cpu'])}%" if i.get("peak_cpu") is not None else ""] if x),
                    "annual": cost, "direction": None, "confidence": None, "lifecycle": lc, "option": (m or {}).get("option")})
            elif sn in excl and not (z.get("matched")):
                x = excl[sn]
                bucket("excluded", "Not assessed", "Resources AWS Transform left out of the assessment")["items"].append(
                    {"name": r["name"], "env": r.get("env"), "now": "", "target": "—", "detail": x.get("reason") or x.get("type") or "Excluded by AWS Transform", "annual": None, "lifecycle": lc})
            elif z.get("matched"):
                m = (z.get("models") or {}).get(slug) or next(iter((z.get("models") or {}).values()), {})
                bucket("ec2", "Amazon EC2", "Virtual servers, right-sized by AWS Transform")["items"].append({
                    "name": r["name"], "env": r.get("env"), "os": r.get("os"), "now": f"{num_s(r.get('vcpu'))} vCPU / {num_s(r.get('ram_gb'))} GB",
                    "target": z.get("instance"), "target_spec": f"{num_s(z.get('vcpu'))} vCPU / {num_s(z.get('ram_gb'))} GB", "detail": "", "annual": (m or {}).get("total"),
                    "direction": z.get("direction"), "confidence": z.get("confidence"), "lifecycle": lc, "storage": z.get("storage"), "issues": len(z.get("issues") or [])})
            elif a is None and r.get("instance"):
                bucket("ec2", "Amazon EC2", "Virtual servers (A01 offline estimate)")["items"].append({
                    "name": r["name"], "env": r.get("env"), "os": r.get("os"), "now": f"{num_s(r.get('vcpu'))} vCPU / {num_s(r.get('ram_gb'))} GB", "target": r["instance"], "detail": "",
                    "annual": r.get("on_demand"), "direction": r.get("direction"), "confidence": r.get("confidence"), "lifecycle": lc})
            else:
                bucket("other", "Awaiting assessment" if a is None else "Not matched to AWS results", "Inventory items without an AWS sizing result")["items"].append(
                    {"name": r["name"], "env": r.get("env"), "os": r.get("os"), "now": f"{num_s(r.get('vcpu'))} vCPU / {num_s(r.get('ram_gb'))} GB", "target": "—",
                     "detail": (z.get("reason") or "") if z else "", "annual": None, "lifecycle": lc})
        if a:                                                   # services that are not per-server rows
            stor = [(z.get("storage") or 0, z.get("volumes") or 0) for z in (v.get("aws") or {} for v in rows) if z.get("matched")]
            sv, nv = sum(x[0] for x in stor), sum(x[1] for x in stor)
            if sv:
                b = bucket("ebs", "Amazon EBS", "Block storage attached to the servers")
                b.update({"annual": round(sv, 2), "count": nv, "unit": "volumes", "items": []})
            if a.s3_annual:
                b = bucket("s3", "Amazon S3", "Object storage")
                b.update({"annual": round(a.s3_annual, 2), "count": None, "unit": "", "items": []})
        order = ["ec2", "rds", "ebs", "s3", "other", "excluded"]
        out = []
        for k in order:
            b = svc.get(k)
            if not b:
                continue
            if b["items"]:
                b["count"], b["unit"] = len(b["items"]), "servers" if k in ("ec2", "other") else "databases" if k == "rds" else "resources"
                costs = [x["annual"] for x in b["items"] if x.get("annual") is not None]
                b["annual"] = round(sum(costs), 2) if costs else None
                for flag in ("upsize", "downsize", "same"):
                    b[flag] = sum(1 for x in b["items"] if x.get("direction") == flag)
                b["eos"] = sum(1 for x in b["items"] if "end_of_support" in (x.get("lifecycle") or []))
            out.append(b)
        total = sum(b["annual"] or 0 for b in out if b["key"] in ("ec2", "rds", "ebs", "s3"))
        for b in out:
            b["share"] = round(100 * (b["annual"] or 0) / total, 1) if total and b["key"] in ("ec2", "rds", "ebs", "s3") else None
        return {"services": out, "total": round(total, 2) if total else None, "model": model_label, "from_aws": a is not None,
                "note": ("Costs are AWS Transform's annual figures for its recommended pricing model; RDS is shown before AWS credits and AWS's support charge is not allocated to individual services."
                         if a else "Costs are A01's own offline estimate on placeholder prices.")}

    def workload(self, rid: str, name: str) -> dict[str, Any]:
        from .agent.tools import ToolBox
        res = self.result(rid)
        ctx = AgentContext(res, self.store.state(rid), self._notes(rid))
        return ToolBox(ctx, "licensing").t_get_workload(name)

    def issues(self, rid: str) -> list[dict[str, Any]]:
        res, st = self.result(rid), self.store.state(rid)
        return [{**i.to_dict(), "acked": gate_mod.is_acked(i, st)} for i in res.issues]

    def licensing(self, rid: str) -> dict[str, Any]:
        res = self.result(rid)
        return {"mode": res.licensing.mode, "provenance": res.licensing.provenance, "coverage": res.lic_cov,
                "findings": [f.to_dict() for f in res.licensing.findings], "gaps": res.lic_gaps,
                "scenario": (res.pricing.scenario("byol").to_dict() if res.pricing.scenario("byol") else None)}

    def parity(self, rid: str) -> dict[str, Any]:
        res, st = self.result(rid), self.store.state(rid)
        return {"summary": res.parity_summary, "rows": [r.to_dict() for r in res.parity_rows],
                "overrides": st["sizing_overrides"]}

    def figures(self, rid: str) -> list[dict[str, Any]]:
        return self.result(rid).ledger.to_list()

    def gate(self, rid: str) -> dict[str, Any]:
        return gate_mod.evaluate(self.result(rid), self.store.state(rid))

    def kpis(self, rid: str) -> dict[str, Any]:
        return run_kpis(self.audit(rid).entries(), self.store.state(rid))

    def portfolio_kpis(self) -> dict[str, Any]:
        return portfolio([self.kpis(r) for r in self.store.list_runs()])

    def audit_view(self, rid: str, limit: int = 200) -> dict[str, Any]:
        a = self.audit(rid)
        return {"verify": a.verify((self.store.state(rid)["finalised"] or {}).get("head")),
                "entries": a.entries()[-limit:]}

    def file_profile(self, rid: str) -> list[dict[str, Any]]:
        m = self.store.manifest(rid)
        return [profile_file(self.store.path(rid) / "inputs" / f) for f in m["files"]]

    # ------------------------------------------------------------------ agent
    def run_agent(self, rid: str, task: str | None = None, mode: str = "stub", actor: str = "agent") -> dict[str, Any]:
        st = self._open(rid)
        res = self.result(rid)
        llm = make_llm(mode, res.cfg)
        a = self.audit(rid)
        ctx = AgentContext(res, st, self._notes(rid))
        tdir = self.store.path(rid) / "agent"
        if task in (None, "orchestrator"):
            out = orchestrate(ctx, llm, res.cfg.section("agent"), a, tdir)
        elif task in TASKS:
            r = run_task(task, ctx, llm, res.cfg.section("agent"), a, tdir)
            out = {"tasks": [r.summary()], "proposals": [p["id"] for p in ctx.proposals]}
        else:
            raise ServiceError(f"unknown task '{task}'; choose from {list(TASKS)}")
        st["proposals"] += ctx.proposals
        self.store.save_state(rid, st)
        a.append("proposals_created", {"count": len(ctx.proposals), "ids": [p["id"] for p in ctx.proposals],
                                       "model": llm.name, "real_model": llm.real}, actor)
        out["model"] = {"name": llm.name, "real": llm.real}
        return out

    def proposals(self, rid: str) -> list[dict[str, Any]]:
        return self.store.state(rid)["proposals"]

    def decide_proposal(self, rid: str, pid: str, decision: str, by: str, note: str = "",
                        edited_text: str | None = None) -> dict[str, Any]:
        if decision not in ("accept", "reject"):
            raise ServiceError("decision must be 'accept' or 'reject'")
        if not by.strip():
            raise ServiceError("say who is deciding")
        st = self._open(rid)
        p = next((x for x in st["proposals"] if x["id"] == pid), None)
        if p is None:
            raise ServiceError(f"no proposal '{pid}' (run an agent task first)")
        if p["status"] != "pending":
            raise ServiceError(f"proposal {pid} is already {p['status']}")
        res = self.result(rid)
        pl = p["payload"]
        if decision == "accept":
            t = p["type"]
            if t == "mapping":
                mo = st["mapping_overrides"].setdefault(pl["source"], {}).setdefault(pl["section"], {})
                mo.setdefault(pl["field"], [])
                if pl["header"] not in mo[pl["field"]]:
                    mo[pl["field"]].append(pl["header"])
            elif t == "licensing_assumption":
                st["assumptions"].append({k: pl[k] for k in ("workload", "product", "instance", "model", "active_sa",
                                                              "tenancy", "licensed_by", "basis")} | {"ref": pid})
            elif t == "override":
                st["sizing_overrides"][pl["workload"]] = {"instance": pl["instance"], "reason": pl["reason"], "by": by,
                                                         "proposal": pid}
            elif t == "commentary":
                text = (edited_text or pl["text"]).strip()
                probs = check_text(text, allowed_numbers(res.ledger.all()))
                if probs:
                    raise ServiceError("this commentary no longer matches the recorded figures (" + "; ".join(probs) +
                                   "). Figures changed after it was drafted: run the narrative task again.")
                if edited_text:
                    p["edited"] = True
                st["commentary"][pl["slide"]] = {"text": text, "source": p["source"] + (" (edited)" if edited_text else ""),
                                                 "proposal": pid, "digest": gate_mod.figure_digest(res)}
            elif t == "review_flag":
                st["flags"][pl["workload"]] = pl["text"]
        p["status"] = "accepted" if decision == "accept" else "rejected"
        p["decided_by"], p["decided_at"], p["decision_note"] = by, now_iso(), note
        if decision == "accept" and p["type"] in ("mapping", "licensing_assumption", "override"):
            stale = [x["id"] for x in st["proposals"] if x["type"] == "commentary" and x["status"] == "pending"]
            for x in st["proposals"]:
                if x["id"] in stale:
                    x["status"], x["decision_note"] = "stale", "figures changed after this was drafted"
            if stale:
                self.audit(rid).append("commentary_marked_stale", {"ids": stale, "cause": pid})
        self.store.save_state(rid, st)
        self.audit(rid).append("proposal_decided", {"id": pid, "type": p["type"], "decision": p["status"], "note": note,
                                                    "edited": p["edited"]}, by)
        return p

    # ------------------------------------------------------------------ review actions
    def ack_issue(self, rid: str, code: str, by: str, note: str, workload: str | None = None) -> None:
        st = self._open(rid)
        if not by.strip() or not note.strip():
            raise ServiceError("an acknowledgement needs a name and a note")
        rec = {"by": by, "note": note, "ts": now_iso()}
        if workload:
            st["acks"][f"{code}|{workload}"] = rec
        else:
            st["ack_codes"][code] = rec
        self.store.save_state(rid, st)
        self.audit(rid).append("exception_acknowledged", {"code": code, "workload": workload, "note": note}, by)

    def ack_parity(self, rid: str, by: str, note: str) -> None:
        self._ack(rid, "parity_reviewed", by, note)

    def ack_licensing(self, rid: str, by: str, note: str) -> None:
        self._ack(rid, "licensing_ack", by, note)

    def _ack(self, rid: str, key: str, by: str, note: str) -> None:
        st = self._open(rid)
        if not by.strip() or not note.strip():
            raise ServiceError("an acknowledgement needs a name and a note")
        st[key] = {"by": by, "note": note, "ts": now_iso()}
        self.store.save_state(rid, st)
        self.audit(rid).append(key, {"note": note}, by)

    def confirm(self, rid: str, by: str) -> dict[str, Any]:
        st = self._open(rid)
        if not by.strip():
            raise ServiceError("say who is confirming")
        res = self.result(rid)
        st["confirmed"] = {"by": by, "ts": now_iso(), "digest": gate_mod.figure_digest(res)}
        self.store.save_state(rid, st)
        self.audit(rid).append("figures_confirmed", {"digest": st["confirmed"]["digest"]}, by)
        return st["confirmed"]

    def finalise(self, rid: str, by: str, rework_rounds: int = 0) -> dict[str, Any]:
        st = self._open(rid)
        g = gate_mod.evaluate(self.result(rid), st)
        if not g["ready"]:
            raise ServiceError("the release gate is not clear: " + "; ".join(
                f"{c['title']} ({c['detail']})" for c in g["checks"] if c["blocking"] and not c["ok"]))
        a = self.audit(rid)
        head = a.head()
        st["finalised"] = {"by": by, "ts": now_iso(), "head": head, "rework_rounds": int(rework_rounds)}
        self.store.save_state(rid, st)
        a.append("finalised", {"head_before": head, "rework_rounds": int(rework_rounds)}, by)
        return st["finalised"]

    def reopen(self, rid: str, by: str, reason: str) -> None:
        self._need(rid)
        st = self.store.state(rid)
        if not st["finalised"]:
            raise ServiceError("the run is not finalised")
        if not reason.strip():
            raise ServiceError("reopening needs a reason")
        st["finalised"], st["confirmed"] = None, None
        self.store.save_state(rid, st)
        self.audit(rid).append("reopened", {"reason": reason}, by)

    def log_hours(self, rid: str, hours: float, by: str, note: str = "") -> None:
        st = self._open(rid)
        st["hours"].append({"hours": float(hours), "by": by, "note": note, "ts": now_iso()})
        self.store.save_state(rid, st)
        self.audit(rid).append("hours_logged", {"hours": float(hours), "note": note}, by)

    def record_outcome(self, rid: str, key: str, value: str, by: str) -> None:
        self._need(rid)
        st = self.store.state(rid)
        st["outcomes"][key] = value
        self.store.save_state(rid, st)
        self.audit(rid).append("outcome_recorded", {key: value}, by)

    # ------------------------------------------------------------------ AWS Transform
    def transform_package(self, rid: str) -> dict[str, Any]:
        self._need(rid)
        res = self.result(rid)
        ad = ConsoleFallbackAdapter(res.cfg)
        out = self.store.path(rid) / "out" / "transform_package"
        files = [str(self.store.path(rid) / "inputs" / f) for f in self.store.manifest(rid)["files"]]
        r = ad.prepare_package(files, out)
        self.audit(rid).append("transform_package_prepared", {"files": r.artifacts})
        return {**r.to_dict(), "folder": str(out)}

    def import_transform_results(self, rid: str, path: str, by: str = "system", job: dict[str, Any] | None = None) -> dict[str, Any]:
        """Import a real AWS Transform assessment (folder, the assessment zip or aggregated_output.json)."""
        with self.lock:
            return self._import_transform_results(rid, path, by, job)

    def _import_transform_results(self, rid: str, path: str, by: str, job: dict[str, Any] | None) -> dict[str, Any]:
        from .transform.results import load_results
        st = self._open(rid)
        dest = self.store.path(rid) / "out" / "transform"
        dest.mkdir(parents=True, exist_ok=True)
        try:
            aws = load_results(path, workdir=dest / "unzipped")
        except Exception as e:
            raise ServiceError(f"could not read the AWS Transform results: {e}") from e
        stored: dict[str, str] = {}
        for k, f in aws.files.items():
            if Path(f).exists():
                target = dest / Path(f).name
                if Path(f).resolve() != target.resolve():
                    shutil.copy2(f, target)
                stored[k] = str(target)
        aws.files = stored
        aws.job = {**(job or {}), "imported_by": by, "imported_at": now_iso()}
        st["transform_pending"] = None
        st["transform_assessment"] = aws.to_dict()
        self.store.save_state(rid, st)
        self.audit(rid).append("transform_assessment_imported", {
            "scenario": aws.scenario_name, "models": {m.slug: m.total for m in aws.models}, "servers": aws.servers,
            "files": {k: Path(v).name for k, v in stored.items()}, "warning_rows": aws.warning_rows}, by)
        return {"scenario": aws.scenario_name, "models": {m.slug: m.total for m in aws.models}, "servers": aws.servers,
                "files": list(stored), "warnings": aws.warning_rows}

    def transform_view(self, rid: str) -> dict[str, Any]:
        """Everything the AWS Transform screen shows: the imported assessment, what is waiting for a person, the environment."""
        import importlib.util

        from .config import load_toml
        res, st = self.result(rid), self.store.state(rid)
        a = res.aws
        tcfg = load_toml(res.cfg.root / "transform.toml")
        return {
            "imported": a is not None,
            "assessment": ({
                "scenario": a.scenario_name, "timestamp": a.timestamp, "job": a.job, "on_prem_total": a.on_prem_total,
                "on_prem_by_category": a.on_prem_by_category, "servers": a.servers, "warning_rows": a.warning_rows,
                "models": [{"slug": m.slug, "label": m.label, "total": m.total, "by_category": m.by_category, "recommended": m.recommended}
                           for m in a.models],
                "assessments": a.assessments, "files": {k: Path(v).name for k, v in a.files.items()},
                "per_server": {"count": len(a.per_server), "notes": a.per_server_notes, "reconciliation": a.reconciliation, "excluded": len(a.excluded),
                               "matched": sum(1 for z in res.aws_sizing.values() if z.ok), "unmatched": [w for w, z in res.aws_sizing.items() if not z.ok][:30]}} if a else None),
            "pending": st.get("transform_pending"),
            "conversation": self.transform_conversation(rid),
            "parity_totals": res.parity_summary.get("totals"),
            "warnings": [i.to_dict() for i in res.issues if i.code.startswith("TRW")],
            "environment": {"aws_profile": os.environ.get("AWS_PROFILE"), "aws_region": os.environ.get("AWS_REGION"),
                            "mcp_installed": importlib.util.find_spec("mcp") is not None, "uvx": bool(shutil.which("uvx")),
                            "server_command": os.environ.get("CVA_TRANSFORM_MCP_CMD", "uvx awslabs.aws-transform-mcp-server@latest")},
            "settings_sent": tcfg["settings"],
            "must_contain": [m["label"] if isinstance(m, dict) else m for m in tcfg["expected"]["must_contain"]],
            "source_kind": res.estate.source_kind, "listed_input": res.estate.source_kind in ("rvtools", "cloudamize_mpa"),
        }

    # ------------------------------------------------------------------ roadmap (the AWS Transform tab, in plain language)
    def transform_roadmap(self, rid: str, job_status: str = "idle", job_message: str = "") -> dict[str, Any]:
        """The AWS Transform run as a short list of steps a person can follow, built from the audit trail (never from raw log text).
        Each step: key, title, state (done | active | needs_you | pending | error | stopped), detail (one plain sentence) and ts."""
        res, st = self.result(rid), self.store.state(rid)
        ents = [e for e in self.audit(rid).entries() if str(e.get("event", "")).startswith("transform")]
        starts = [i for i, e in enumerate(ents) if e["event"] == "transform_tool" and (e.get("data") or {}).get("tool") == "create_workspace"]
        ents = ents[starts[-1]:] if starts else ents
        tools = {}
        for e in ents:
            if e["event"] == "transform_tool":
                tools.setdefault((e.get("data") or {}).get("tool"), e)
        ev = {}
        for e in ents:
            ev.setdefault(e["event"], e)
        imported = res.aws is not None
        pend = st.get("transform_pending")
        if not ents and not pend:
            return {"mode": "imported" if imported else "idle", "steps": [], "status": job_status}
        job_e = ev.get("transform_job_created") or ev.get("transform_job_resumed")
        ws_done = "create_workspace" in tools or job_e is not None
        job_done = job_e is not None
        sent = "upload_artifact" in tools or "send_message" in tools
        appr = ev.get("transform_approved_by_person") or ev.get("transform_confirmed_externally")
        completed = ev.get("transform_run_completed") is not None or imported
        suggestions = sum(1 for e in ents if e["event"] == "transform_suggestion_relayed")
        jid = (job_e or {}).get("data", {}).get("jobId") or (st.get("transform_job") or {}).get("jobId") or ""
        who = (appr or {}).get("actor") or ""
        running = job_status == "running"
        stage = 5 if completed else 4 if appr else 3 if sent else 2 if job_done else 1 if ws_done else 0   # index of the first step that is not finished
        kind = (pend or {}).get("kind")
        titles_you = {"approval": "Your approval is needed", "question": "AWS Transform has a question for you", "connection": "AWS connection needs attention",
                      "task": "An approval task is waiting for you"}
        title_you = titles_you.get(kind or "", "AWS Transform is waiting for you")
        if kind == "approval" and (pend or {}).get("needs_listing"):
            title_you = "Review the reply, then confirm the configuration"
        steps = [
            {"key": "workspace", "title": "Workspace created", "detail": "A dedicated AWS Transform workspace holds this assessment.", "ts": (tools.get("create_workspace") or {}).get("ts")},
            {"key": "job", "title": "Assessment job created and started", "detail": f"Job {jid[:8]}… is registered in AWS Transform." if jid else "The job is registered in AWS Transform.", "ts": (job_e or {}).get("ts")},
            {"key": "inventory", "title": "Server inventory and assessment settings submitted", "detail": "Region, pricing models, tenancy and optimisation preference were sent together with the inventory.",
             "ts": (tools.get("upload_artifact") or tools.get("send_message") or {}).get("ts")},
            {"key": "configuration", "title": "Configuration prepared and reviewed", "ts": (appr or {}).get("ts"),
             "detail": (f"Approved by {who}" if who else "Approved") + (f" after {suggestions} suggestion(s) exchanged with AWS Transform." if suggestions else ".")},
            {"key": "assessment", "title": "Assessment completed", "detail": "Sizing, pricing and the business case were produced.", "ts": (ev.get("transform_run_completed") or {}).get("ts")},
            {"key": "import", "title": "Results retrieved and imported", "ts": (ev.get("transform_run_completed") or {}).get("ts"),
             "detail": (f"{len(res.aws.per_server)} servers analysed across {len(res.aws.models)} pricing models." if imported else "")},
        ]
        for i, s in enumerate(steps):
            s["state"] = "done" if (completed or i < stage) else "pending"
            if s["state"] == "pending":
                s["title"] = {"configuration": "Configuration review", "assessment": "Assessment: sizing, pricing and business case", "import": "Results retrieved and imported"}.get(s["key"], s["title"])
                s["detail"] = ""
        if not completed:
            cur = steps[min(stage, 4)] if stage < 5 else steps[5]
            if stage in (0, 1, 2) and pend:
                cur["state"], cur["title"], cur["detail"] = "needs_you", title_you, ""
            elif stage in (0, 1, 2):
                cur["state"] = "active" if running else "stopped"
                cur["title"] = {0: "Creating the workspace", 1: "Creating the assessment job", 2: "Submitting the inventory and settings"}[stage]
                cur["detail"] = "In progress…" if running else (job_message or "This step did not finish. You can start the assessment again.")
            elif stage == 3:                                  # AWS Transform prepares its configuration, a person approves it
                if pend:
                    cur["state"], cur["title"], cur["detail"] = "needs_you", title_you, ""
                else:
                    cur["state"] = "active" if running else "stopped"
                    cur["title"] = "AWS Transform is analysing the inventory and preparing its configuration"
                    cur["detail"] = "This usually takes a few minutes." if running else (job_message or "Paused. Resume from this step when you are ready.")
            elif stage == 4:                                  # approved: the assessment itself runs
                if pend:
                    cur["state"], cur["title"], cur["detail"] = "needs_you", title_you, ""
                else:
                    cur["state"] = "active" if running else "stopped"
                    cur["title"] = "Assessment in progress: sizing servers, pricing and building the business case"
                    cur["detail"] = "This can take several minutes. You can leave this page; progress is saved." if running else (job_message or "Paused. Resume to keep waiting for the results.")
            if job_status == "error" and not pend:
                cur["state"], cur["detail"] = "error", job_message or "Something went wrong."
            steps[min(stage, 5)] = cur
        elif stage == 5 and not imported:
            steps[5]["state"] = "active"
        return {"mode": "job", "steps": steps, "status": job_status, "message": job_message, "pending_kind": kind, "suggestions": suggestions, "approved_by": who or None}

    def ack_estimate(self, rid: str, by: str, note: str) -> None:
        """A reviewer accepts that this release uses A01's offline estimate (no AWS Transform assessment imported)."""
        if not self.result(rid).estimate_ran:
            raise ServiceError("there is no offline estimate to accept: run it first, or import an AWS Transform assessment")
        self._ack(rid, "estimate_ack", by, note)

    def transform_run(self, rid: str, enrich: bool = False, accept_proposal: bool = False, resume: tuple[str, str] | None = None,
                      allow_unlisted: bool = False, client: Any = None, sleep: Any = None, by: str = "driver",
                      progress: Any = None, cancel: Any = None, confirmed_externally: bool = False,
                      require_approval: bool = True, approve_now: bool = False, reply_text: str = "", reply_stage: str = "scenario",
                      customised: bool = False, clock: Any = None, task_answer: tuple[str, str] | None = None, resume_stage: str = "scenario",
                      show_listing: bool = False, answer_question: bool = False) -> dict[str, Any]:
        """A01 runs the AWS Transform assessment itself through the MCP server and imports the result."""

        from .transform.driver import AssessmentDriver, DriverInputs, NeedsHuman
        from .transform.enrichment import fill_template
        self._open(rid)
        res = self.result(rid)
        run_dir = self.store.path(rid)
        out = run_dir / "out" / "transform_job"
        out.mkdir(parents=True, exist_ok=True)
        a = self.audit(rid)
        client = client or (self.transform_client_factory() if self.transform_client_factory else None)
        sleep = sleep or self.transform_sleep
        own = client is None
        env_run: dict[str, str] = {}
        if own:
            env = self._mcp_env({"AWS_TRANSFORM_MCP_WRITE_DIR": str(out)})
            env_run = env
            try:
                client = self._start_mcp(env, out / "mcp_server.log")
            except Exception as e:
                raise ServiceError(f"could not start the AWS Transform MCP server: {e}") from e

        polls = {"n": 0}

        def on_event(ev: str, data: dict[str, Any]) -> None:
            if ev in ("job_created", "job_resumed"):                      # remember the job as soon as it exists: the artifacts panel works even while a run is stuck
                with self.lock:
                    s2 = self.store.state(rid)
                    s2["transform_job"] = {"workspaceId": data.get("workspaceId"), "jobId": data.get("jobId")}
                    self.store.save_state(rid, s2)
            polling = ev == "tool" and data.get("tool") in ("list_resources", "get_job_status")
            if ev == "waiting":
                polls["n"] += 1
            if not polling and (ev != "waiting" or polls["n"] % 10 == 1):          # polling is summarised, not logged call by call
                a.append("transform_" + ev, {k: v for k, v in data.items() if k != "args"} | ({"tool_args": sorted(data.get("args", {}))} if "args" in data else {}),
                         by if ev in ("guard_overridden_by_person", "confirmed_externally", "approved_by_person", "question_answered_by_person", "task_answered", "listing_requested") else "driver")
            if progress and not polling:
                progress({"tool": f"called {data.get('tool')}", "guard": "configuration check: " + ("matches" if data.get("ok") else "MISMATCH"),
                          "guard_overridden_by_person": f"configuration mismatch overridden by {by}",
                          "confirmed_externally": f"{by} says the scenario was confirmed in the AWS Transform web application",
                          "approved_by_person": f"{by} approved in A01: the go-ahead is sent to AWS Transform",
                          "question_answered_by_person": f"{by} chose to proceed with the current inventory: A01 sent that answer and now asks AWS Transform for its configuration",
                          "not_a_listing": "AWS Transform listed no configuration (it asked something first or is still processing): a person is asked",
                          "suggestion_relayed": f"{by}'s message sent to AWS Transform, unchanged: {str(data.get('text', ''))[:160]}",
                          "service_replied": f"AWS Transform replied: {str(data.get('text', ''))[:200]}",
                          "listing_requested": f"{by} asked AWS Transform to list the configuration it will run",
                          "list_failed": f"listing of '{data.get('prefix')}' failed: {data.get('error')}",
                          "status_unavailable": f"the job status did not answer ({data.get('error')}): still waiting",
                          "scenario_asked_again": f"the job is still waiting for the scenario decision (check {data.get('n')}): asking for it again",
                          "login_checked": (f"AWS login checked: valid, role {data.get('role')}" if data.get("ok") else
                                            f"AWS login check: {data.get('kind')}" + (f" ({data.get('detail')})" if data.get("detail") else "")),
                          "continuing_to_wait": "continuing: the go-ahead was sent earlier, so A01 just waits for the outputs again",
                          "mcp_restarting": f"{data.get('note')} (fresh start {data.get('n')})",
                          "mcp_restarted": f"a fresh AWS Transform server is running (start {data.get('n')}); carrying on",
                          "task_answered": f"{by} chose {str(data.get('action', '')).lower()} for the task '{data.get('title')}' in A01: relayed to AWS Transform",
                          "job_created": "AWS Transform job created",
                          "job_resumed": "continuing the existing AWS Transform job",
                          "instructions_required": f"AWS Transform wanted the job's instructions loaded before '{data.get('tool')}': loaded them and retried",
                          "poke": f"AWS Transform asked for a message: sent a read-only status request ({data.get('n')})",
                          "waiting": f"waiting for {data.get('what')} ({data.get('elapsed')} s). {data.get('detail')}"}.get(ev, ev))

        driver: Any = None
        try:
            kw = {**({"sleep": sleep} if sleep else {}), **({"clock": clock} if clock else {})}
            driver = AssessmentDriver(client, on_event=on_event, trace_path=out / "transform_trace.jsonl",
                                      client_factory=(lambda: self._start_mcp(env_run, out / "mcp_server.log", check_login=False)) if own else self.transform_client_factory,
                                      login_check=self.login_check, **kw)
            files = [run_dir / "inputs" / f for f in self.store.manifest(rid)["files"]]
            stamp = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]       # unique per attempt: a fresh runs folder restarts at run-0001
            inp = DriverInputs(files=files, out_dir=out, workspace_name=f"a01-{rid}-{stamp}", job_name=f"a01-{rid}-{stamp}-assessment",
                               kind=res.estate.source_kind, accept_proposal=accept_proposal, resume=resume, allow_unlisted_format=allow_unlisted,
                               confirmed_externally=confirmed_externally, cancel=cancel, require_approval=require_approval, approve_now=approve_now,
                               reply_text=reply_text, reply_stage=reply_stage, customised=customised, task_answer=task_answer, show_listing=show_listing, resume_stage=resume_stage, answer_question=answer_question,
                               enrich=(lambda tpl, dst: fill_template(tpl, dst, res.estate, res.cfg).to_dict()) if enrich else None)
            def pause(nh: NeedsHuman) -> ServiceError:
                with self.lock:
                    st = self.store.state(rid)
                    st["transform_pending"] = nh.to_dict()
                    self.store.save_state(rid, st)
                if nh.kind == "connection":
                    hint = self._mcp_log_hint(out / "mcp_server.log")
                    if hint:
                        nh.reason += " What the MCP server logged at start-up: " + hint
                with self.lock:
                    st = self.store.state(rid)
                    st["transform_pending"] = nh.to_dict()
                    self.store.save_state(rid, st)
                a.append("transform_needs_human", {"reason": nh.reason, "workspaceId": nh.workspace_id, "jobId": nh.job_id, "guard": nh.guard, "kind": nh.kind})
                hint = f" Resume with: --resume {nh.workspace_id}:{nh.job_id}" if nh.workspace_id and nh.job_id else ""
                return ServiceError("AWS Transform needs a person. " + nh.reason + hint)
            try:
                dres = driver.run(inp)
            except NeedsHuman as nh:
                raise pause(nh) from nh
            except ServiceError:
                raise
            except Exception as exc:                  # nothing unexpected may turn a running job into a dead "failed": keep the job, say what happened, say what to do
                ids = self.store.state(rid).get("transform_job") or {}
                nh = NeedsHuman(f"A01 lost contact with AWS Transform while working on the job ({type(exc).__name__}: {str(exc)[:200]}). The job keeps running in AWS. "
                                "Use 'Check again', or 'Fetch the results now and fill the other tabs'.", ids.get("workspaceId"), ids.get("jobId"), stage="running")
                raise pause(nh) from exc
        finally:
            if own and client is not None:
                (driver.c if driver is not None else client).close()               # the live session: the driver may have started a fresh one
        summary = self.import_transform_results(rid, str(dres.results_dir), by if by != "driver" else "driver",
                                                job={"workspaceId": dres.workspace_id, "jobId": dres.job_id, "passes": dres.passes})
        a.append("transform_run_completed", {"passes": dres.passes, "workspaceId": dres.workspace_id, "jobId": dres.job_id,
                                             "enrichment": dres.enrichment})
        return {**summary, "passes": dres.passes, "enrichment": dres.enrichment, "job": {"workspaceId": dres.workspace_id, "jobId": dres.job_id}}

    # ---------------------------------------------------------------- AWS Transform artifacts: list, download some or all, fetch the results
    def _start_mcp(self, env: dict[str, str], errlog: Path, timeout: float | None = None, attempts: int = 3, check_login: bool = True) -> Any:
        """Start the AWS Transform MCP server and check that it is connected.  At start-up the server checks your AWS credentials against AWS Transform with a
        5-second limit per region; if that check fails by chance (a slow network) the WHOLE session answers NOT_CONFIGURED although the login is fine.  So a session
        that starts not connected is closed and started again, up to ``attempts`` times; the last one is returned either way and its answer reported."""
        import shlex
        import time

        from .transform import login, shape
        from .transform.mcp_client import McpToolClient
        if check_login:                                                   # never start a server on a login that is already over
            r = self.login_check()
            if r.get("ok") is False and r.get("kind") in ("expired", "no_credentials"):
                raise ServiceError(login.login_help(os.environ.get("AWS_PROFILE"), r["kind"], str(r.get("detail", ""))))
        cmd = shlex.split(os.environ.get("CVA_TRANSFORM_MCP_CMD", "uvx awslabs.aws-transform-mcp-server@latest"))
        client: Any = None
        for i in range(attempts):
            client = McpToolClient(cmd[0], cmd[1:], env, errlog_path=str(errlog), **({"timeout": timeout} if timeout else {})).start()
            try:
                if not shape.not_connected(client.call("get_status", {})):
                    return client
            except Exception:
                return client                                   # another kind of problem: the caller's own checks will report it
            if i < attempts - 1:
                client.close()
                time.sleep(3)
        return client

    def login_check(self) -> dict[str, Any]:
        """One read-only AWS CLI call (see transform/login.py): is the login alive, which role is it, and a refresh of a login that is near its end."""
        from .transform import login
        return login.check_login(os.environ.get("AWS_PROFILE") or None, os.environ.get("AWS_REGION") or None)

    @staticmethod
    def _mcp_env(extra: dict[str, str]) -> dict[str, str]:
        """The environment the AWS Transform MCP server runs in: the person's WHOLE environment, as it would be from their own terminal or from Claude Code.  The MCP library
        otherwise passes a server only HOME, PATH, USER, SHELL, TERM and LOGNAME, which silently drops proxy settings, a company certificate bundle and the other AWS_*
        variables, and a server that cannot reach AWS Transform inside its 5-second start-up limit then answers NOT_CONFIGURED.  Only variables that would make the server
        run in A01's own Python environment are left out."""
        skip = {"VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME", "__PYVENV_LAUNCHER__", "PYTHONSTARTUP"}
        env = {k: v for k, v in os.environ.items() if isinstance(v, str) and k not in skip}
        env.update(extra)
        env["FASTMCP_LOG_LEVEL"] = "ERROR"
        return env

    @staticmethod
    def _probe_report(server_log: Path) -> str:
        """The result of the server's latest start-up credential check, read from ITS OWN log (``~/.aws-transform-mcp/server.log``: it records every step at INFO level, while the
        quiet stderr that A01 keeps does not), with a plain-words 'most likely' when the lines make the cause clear."""
        if not server_log.exists():
            return ""
        lines = server_log.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max((i for i, ln in enumerate(lines) if "Credential probe starting" in ln), default=None)
        if start is None:
            return ""
        other: list[str] = []
        failed: dict[str, str] = {}
        for ln in lines[start:start + 80]:
            m = re.match(r"^\d{4}-\d\d-\d\d (\d\d:\d\d:\d\d)\.\d+ \| \w+\s*\| [^-]*- (.*)$", ln)
            if not m:
                continue
            msg = m.group(2).strip()
            f = re.match(r"Region discovery failed for ([\w-]+):\s*(.*)$", msg)
            if f:
                failed[f.group(1)] = f.group(2).strip()
            elif msg.startswith(("Credential probe", "AWS credential resolution failed", "No AWS credentials found")) or ("Region discovery:" in msg and "succeeded" in msg):
                other.append(f"{m.group(1)} {msg}")
        if not other and not failed:
            return ""
        low = " ".join(other).lower()
        msgs = [x.lower() for x in failed.values()]
        if "credential resolution failed" in low or "no aws credentials" in low:
            why = "Most likely: the server could not use your AWS login (expired or not found). Run aws sso login --profile aivar-vision-admin and start A01 from that terminal."
        elif "selection required" in low:
            why = "Most likely: AWS Transform answered in SEVERAL regions and the server waits for one to be chosen, which A01 cannot do. Ask an administrator which region is yours."
        elif any(("accessdenied" in x or "not authorized" in x or "403" in x) for x in msgs):
            why = "Most likely: AWS answered, but your AWS role is not allowed to use AWS Transform (a permission). Ask your AWS administrator to allow AWS Transform for this role."
        elif any(("ssl" in x or "certificate" in x) for x in msgs):
            why = "Most likely: a certificate problem: your network inspects traffic. Set SSL_CERT_FILE or AWS_CA_BUNDLE to the company certificate in that terminal, then restart A01."
        elif failed and all(not x for x in failed.values()):
            why = ("Most likely: every region timed out at the server's 5-second limit, so it could not reach AWS Transform (network, VPN or proxy). If your network needs a "
                   "proxy, set HTTPS_PROXY in the terminal that starts A01: A01 now passes it on to the server.")
        else:
            why = ""
        groups: dict[str, list[str]] = {}
        for region, text in failed.items():
            groups.setdefault(text or "(no message: the 5-second limit passed)", []).append(region)
        fails = [f"failed in {len(r)} region(s) [{', '.join(sorted(r)[:4])}{', ...' if len(r) > 4 else ''}]: {t[:140]}" for t, r in groups.items()]
        return " | ".join([x for x in [why] if x] + other[:4] + fails[:3])

    @classmethod
    def _mcp_log_hint(cls, errlog: Path, server_log: Path | None = None) -> str:
        """What explains a failed sign-in: first the server's own start-up record (see ``_probe_report``), else the lines of A01's quiet stderr copy that mention credentials."""
        slog = server_log or Path(os.environ.get("CVA_MCP_SERVER_LOG") or (Path.home() / ".aws-transform-mcp" / "server.log"))
        report = cls._probe_report(slog)
        if report:
            return report
        if not errlog.exists():
            return ""
        keys = ("region discovery failed", "credential", "expired", "sso", "unable to locate", "accessdenied", "profile", "not authorized")
        lines = [ln.strip()[:220] for ln in errlog.read_text(encoding="utf-8", errors="replace").splitlines()[-80:] if any(k in ln.lower() for k in keys)]
        return " | ".join(dict.fromkeys(lines[-3:]))

    def _job_ids(self, rid: str) -> tuple[str, str]:
        st = self.store.state(rid)
        for src in (st.get("transform_job"), st.get("transform_pending"), (st.get("transform_assessment") or {}).get("job")):    # newest first: set on every create and resume
            if src and src.get("workspaceId") and src.get("jobId"):
                return src["workspaceId"], src["jobId"]
        ids = self._ids_from_trace(rid)                    # a run made by an older version did not store its job: recover it from the saved trace
        if ids:
            return ids
        raise ServiceError("no AWS Transform job is known for this run yet: run the assessment first (or import a finished one)")

    def _ids_from_trace(self, rid: str) -> tuple[str, str] | None:
        tp = self.store.path(rid) / "out" / "transform_job" / "transform_trace.jsonl"
        if not tp.exists():
            return None
        ws = job = None
        for line in tp.read_text(encoding="utf-8", errors="replace").splitlines():       # answers are JSON inside JSON strings: quotes may be escaped
            for m in re.finditer(r'(workspaceId|workspace_id|jobId|job_id)\\?"\s*:\s*\\?"([0-9A-Za-z-]{8,64})', line):
                if m.group(1).startswith("workspace"):
                    ws = m.group(2)
                else:
                    job = m.group(2)
        return (ws, job) if ws and job else None

    @contextlib.contextmanager
    def _artifact_session(self, rid: str, ws: str, job: str) -> Any:
        """A short session with the AWS Transform server for reading a job's files.  Yields (driver, the folder files are saved into)."""

        from .transform.driver import AssessmentDriver
        out = self.store.path(rid) / "out" / "transform_artifacts"
        out.mkdir(parents=True, exist_ok=True)
        client = self.transform_client_factory() if self.transform_client_factory else None
        own = client is None
        if own:
            env = self._mcp_env({"AWS_TRANSFORM_MCP_WRITE_DIR": str(out)})
            try:
                client = self._start_mcp(env, out / "mcp_server.log")
            except Exception as e:
                raise ServiceError(f"could not start the AWS Transform MCP server: {e}") from e
        kw = {"sleep": self.transform_sleep} if self.transform_sleep else {}
        panel_env = env if own else {}
        drv = AssessmentDriver(client, trace_path=out / "trace.jsonl",
                               client_factory=(lambda: self._start_mcp(panel_env, out / "mcp_server.log", check_login=False)) if own else self.transform_client_factory,
                               login_check=self.login_check, **kw)
        try:
            try:
                drv._call("load_instructions", workspaceId=ws, jobId=job)
            except Exception:
                pass
            yield drv, out
        finally:
            if own and client is not None:
                drv.c.close()

    def transform_artifacts_list(self, rid: str) -> dict[str, Any]:
        """Every file AWS Transform produced for this run's job, read live, with its folder and size."""
        self._need(rid)
        ws, job = self._job_ids(rid)
        with self._artifact_session(rid, ws, job) as (drv, _out):
            items, report = drv.list_job_artifacts(ws, job)
        idx = self.store.state(rid).get("transform_artifact_index") or {}
        items = sorted(({**i, "downloaded": i["artifactId"] in idx} for i in items), key=lambda i: (i["folder"], i["name"]))
        with self.lock:
            st = self.store.state(rid)
            st["transform_artifact_listing"] = {i["artifactId"]: i for i in items}
            self.store.save_state(rid, st)
        return {"job": {"workspaceId": ws, "jobId": job}, "items": items, "report": report}

    @staticmethod
    def _artifact_relpath(item: dict[str, Any]) -> str:
        def clean(seg: str) -> str:
            seg = re.sub(r"[^A-Za-z0-9._ -]", "_", seg).strip().strip(".")[:120]
            return seg or "file"
        parts = [clean(p) for p in (item.get("folder") or "").strip("/").split("/") if p.strip("./ ")]
        return "/".join(parts + [clean(item["name"])])

    def transform_artifacts_fetch(self, rid: str, ids: list[str] | None = None, everything: bool = False, by: str = "ui") -> dict[str, Any]:
        """Download chosen artifacts (or all) from the job into the run, keeping their folders; several files also come as one zip."""
        import zipfile
        self._need(rid)
        ws, job = self._job_ids(rid)
        listing = self.store.state(rid).get("transform_artifact_listing") or {}
        if not listing:
            raise ServiceError("list the job's files first")
        chosen = list(listing) if everything else [i for i in (ids or []) if i in listing]
        if not chosen:
            raise ServiceError("choose at least one file")
        files: list[dict[str, Any]] = []
        with self._artifact_session(rid, ws, job) as (drv, out):
            base = (out / "files").resolve()
            for aid in chosen:
                item = listing[aid]
                rel = self._artifact_relpath(item)
                dest = (base / rel).resolve()
                if base not in dest.parents:
                    raise ServiceError("refused an unsafe file path")
                drv.fetch_item(ws, job, item, dest.parent, dest.name)
                files.append({"id": aid, "name": item["name"], "folder": item.get("folder", ""), "path": rel, "bytes": dest.stat().st_size})
        with self.lock:
            st = self.store.state(rid)
            idx = st.get("transform_artifact_index") or {}
            idx.update({f["id"]: {"path": f["path"], "name": f["name"], "bytes": f["bytes"]} for f in files})
            st["transform_artifact_index"] = idx
            self.store.save_state(rid, st)
        zip_name = None
        if len(files) > 1:
            zip_name = "artifacts_all.zip" if everything else "artifacts_selected.zip"
            tdir = self.store.path(rid) / "out" / "transform"
            tdir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(tdir / zip_name, "w", zipfile.ZIP_DEFLATED) as z:
                for f in files:
                    z.write(base / f["path"], f["path"])
        self.audit(rid).append("transform_artifacts_downloaded", {"files": [f["path"] for f in files][:50], "count": len(files), "zip": zip_name}, by)
        return {"files": files, "zip": zip_name}

    def transform_fetch_results(self, rid: str, by: str) -> dict[str, Any]:
        """Fetch what A01 needs (aggregated_output.json, error_file.csv, the assessment zip) straight from the job and import it, so the other
        tabs fill with no manual download.  The same step the run does by itself, available on demand."""
        from .transform.driver import is_result_file
        if not by.strip():
            raise ServiceError("say who is fetching the results (your name is recorded)")
        self._need(rid)
        ws, job = self._job_ids(rid)
        with self._artifact_session(rid, ws, job) as (drv, out):
            arts = drv._artifacts(ws, job)
            report = " | ".join(drv._listing_report)
            wanted = [a for a in arts if is_result_file(a)]
            if not any(a["name"].rsplit("/", 1)[-1] == "aggregated_output.json" for a in wanted):
                raise ServiceError("A01 cannot find aggregated_output.json in the job's files. What each listing returned: " + (report or "n/a"))
            dest = out / "results"
            drv._download(ws, job, wanted, dest)
        summary = self.import_transform_results(rid, str(dest), by, job={"workspaceId": ws, "jobId": job, "passes": 1, "via": "fetch_results"})
        self.audit(rid).append("transform_results_fetched", {"files": sorted(a["name"].rsplit("/", 1)[-1] for a in wanted), "jobId": job}, by)
        return summary

    def transform_sync(self, rid: str, by: str = "A01 (automatic)") -> dict[str, Any]:
        """Ask the job where it really is.  If its outputs exist the screen must not keep asking a person to approve something that already ran: A01 fetches
        and imports the results itself (the same step as 'Fetch the results now') and the paused question is cleared.  Read-only otherwise."""
        self._need(rid)
        st = self.store.state(rid)
        if st.get("transform_assessment") and not st.get("transform_pending"):
            return {"state": "imported", "outputs_ready": True, "fetched": False, "said": []}
        try:
            ws, job = self._job_ids(rid)
        except ServiceError:
            return {"state": "no_job", "outputs_ready": False, "fetched": False, "said": []}
        with self._artifact_session(rid, ws, job) as (drv, out):
            p = drv.probe(ws, job)
        res = {"state": "outputs_ready" if p["outputs_ready"] else "waiting", **p, "fetched": False}
        if p["outputs_ready"]:
            self.transform_fetch_results(rid, by)                              # imports, and clears the paused question
            res.update(fetched=True, state="imported")
            self.audit(rid).append("transform_outputs_found_while_waiting", {"jobId": job, "status": p["status"]}, by)
        return res

    def transform_conversation(self, rid: str, limit: int = 60) -> list[dict[str, Any]]:
        """The exchange between the person, A01 and AWS Transform as a list of turns (who, what), oldest first, built from the audit trail: nothing is invented."""
        who = {"transform_job_created": ("A01", "Created the AWS Transform job."), "transform_job_resumed": ("A01", "Continuing the existing job."),
               "transform_guard": ("A01", None), "transform_not_a_listing": ("A01", "AWS Transform listed no configuration yet: it asked something first or is still processing."),
               "transform_needs_human": ("A01", None), "transform_service_replied": ("AWS Transform", None),
               "transform_suggestion_relayed": ("You", None), "transform_message_sent_by_person": ("You", None),
               "transform_approved_by_person": ("You", "Approved: the go-ahead was sent."),
               "transform_question_answered_by_person": ("You", "Chose to proceed with the current inventory."),
               "transform_listing_requested": ("You", "Asked AWS Transform to list the configuration it will run."),
               "transform_guard_overridden_by_person": ("You", "Approved a configuration that differs from the request."),
               "transform_task_answered": ("You", None), "transform_outputs_found_while_waiting": ("A01", "The outputs exist: fetched and imported the results."),
               "transform_run_completed": ("A01", "AWS Transform finished; results imported."), "transform_results_fetched": ("A01", "Fetched the result files and imported them.")}
        turns: list[dict[str, Any]] = []
        for e in self.audit(rid).entries():
            ev = str(e.get("event") or e.get("type") or "")
            if ev not in who:
                continue
            sp, text = who[ev]
            d = e.get("data") or e.get("detail") or {}
            if text is None:
                if ev == "transform_guard":
                    text = "Checked the configuration the service listed: " + ("it matches what was requested." if d.get("ok") else "it does NOT match what was requested.")
                elif ev == "transform_needs_human":
                    text = str(d.get("reason") or "A person is needed.")[:400]
                elif ev == "transform_task_answered":
                    text = f"Chose {str(d.get('action', '')).lower()} for the task '{d.get('title')}'."
                else:
                    text = str(d.get("text") or "")[:700]
            if sp == "You" and e.get("actor") and e.get("actor") not in ("driver", "You"):
                sp = str(e.get("actor"))
            if text:
                turns.append({"who": sp, "text": text, "at": e.get("ts") or e.get("time") or ""})
        return turns[-limit:]

    def transform_preflight(self, client: Any = None) -> dict[str, Any]:
        """A 10-second test of the whole connection that CHANGES NOTHING in AWS: credentials, the MCP server, the tools A01 needs, the connection to
        AWS Transform, and a read-only listing.  Run it before a 20-minute assessment: an expired login or a missing tool shows up here."""
        import shlex

        from .transform import shape
        from .transform.mcp_client import ALLOWED_TOOLS
        checks: list[dict[str, Any]] = []

        def add(name: str, ok: bool, detail: str) -> bool:
            checks.append({"name": name, "ok": ok, "detail": detail})
            return ok
        prof, region = os.environ.get("AWS_PROFILE"), os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
        add("AWS profile and region are set", bool(region), (f"profile {prof}, " if prof else "no profile: the instance role or environment credentials are used, ")
            + f"region {region or 'NOT SET'}" + ("" if region else ": export AWS_REGION (and AWS_PROFILE when you sign in with SSO) before starting A01"))
        lg = self.login_check()
        if lg.get("kind") != "disabled":
            if lg.get("ok") is True:
                add("Your AWS login works", True, f"role {lg.get('role')} in account {lg.get('account')} ({lg.get('seconds')} s). Make sure this is the role you expect (the admin role).")
            elif lg.get("ok") is None:
                add("Your AWS login works", True, "not checked: " + str(lg.get("detail")))
            else:
                from .transform import login as login_mod
                add("Your AWS login works", False, login_mod.login_help(prof, str(lg.get("kind")), str(lg.get("detail", ""))))
                if lg.get("kind") in ("expired", "no_credentials"):
                    return {"ok": False, "checks": checks}
        client = client or (self.transform_client_factory() if self.transform_client_factory else None)
        own = client is None
        if own:
            cmd = shlex.split(os.environ.get("CVA_TRANSFORM_MCP_CMD", "uvx awslabs.aws-transform-mcp-server@latest"))
            if not add("The server command is installed", bool(shutil.which(cmd[0])), f"{cmd[0]} " + ("found" if shutil.which(cmd[0]) else "NOT FOUND (install uv: brew install uv)")):
                return {"ok": False, "checks": checks}
            try:
                env = self._mcp_env({})
                logdir = self.store.base / "_preflight"
                logdir.mkdir(parents=True, exist_ok=True)
                client = self._start_mcp(env, logdir / "mcp_server.log", timeout=90, attempts=2)
                add("The AWS Transform MCP server starts", True, " ".join(cmd))
            except Exception as e:
                add("The AWS Transform MCP server starts", False, f"{type(e).__name__}: {str(e)[:240]}")
                return {"ok": False, "checks": checks}
        try:
            names = client.tool_names() if hasattr(client, "tool_names") else set(ALLOWED_TOOLS)
            missing = sorted(ALLOWED_TOOLS - set(names))
            add("The tools A01 needs exist", not missing, "all present" if not missing else "missing: " + ", ".join(missing))
            try:
                st = client.call("get_status", {})
                low = json.dumps(st, default=str).lower().replace("_", " ")
                bad = shape.not_connected(st) or any(w in low for w in ("not configured", "not connected", "not available", "unavailable", "expired"))
                hint = self._mcp_log_hint(self.store.base / "_preflight" / "mcp_server.log") if bad else ""
                add("Connected to AWS Transform", not bad, (shape.NOT_CONNECTED_SHORT + (" What the MCP server logged at start-up: " + hint if hint else "")) if bad
                    else json.dumps(st, default=str)[:260])
            except Exception as e:
                add("Connected to AWS Transform", False, f"{type(e).__name__}: {str(e)[:240]}")
            try:
                r = client.call("list_resources", {"resource": "workspaces"})
                refused = shape.not_connected(r) or (isinstance(r, dict) and (r.get("success") is False or r.get("error") or r.get("isError") is True))
                add("A read-only listing works", not refused, json.dumps(r, default=str)[:200])
            except Exception as e:
                add("A read-only listing works", False, f"{type(e).__name__}: {str(e)[:240]}")
        finally:
            if own and client is not None:
                client.close()
        return {"ok": all(c["ok"] for c in checks), "checks": checks}

    def transform_diagnostics(self, rid: str) -> dict[str, Any]:
        """One file to send when something looks wrong: the tail of the raw conversation with the service, the server's log, the run's AWS Transform events,
        its paused state and the versions.  It contains the job's messages, so share it only with people who may see them."""
        import platform
        import zipfile
        from importlib import metadata
        self._need(rid)
        run_dir = self.store.path(rid)
        out = run_dir / "out" / "transform"
        out.mkdir(parents=True, exist_ok=True)

        def tail(p: Path, n: int) -> str:
            return "".join(p.read_text(encoding="utf-8", errors="replace").splitlines(True)[-n:]) if p.exists() else "(not present)\n"
        st = self.store.state(rid)
        events = [e for e in self.audit(rid).entries() if str(e.get("event", "")).startswith("transform")][-200:]
        try:
            mcp_v = metadata.version("mcp")
        except Exception:
            mcp_v = "not installed"
        try:
            import tomllib
            a01_v = tomllib.loads((Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
        except Exception:
            a01_v = "unknown"
        target = out / "diagnostics.zip"
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("trace_tail.jsonl", tail(run_dir / "out" / "transform_job" / "transform_trace.jsonl", 300))
            samples: dict[tuple[str, str], list[str]] = {}
            tp = run_dir / "out" / "transform_job" / "transform_trace.jsonl"
            if tp.exists():                                          # the first and the last raw answer of each distinct listing: the real shapes
                for line in tp.read_text(encoding="utf-8", errors="replace").splitlines():
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    a = rec.get("args") or {}
                    if rec.get("tool") in ("list_resources", "get_resource", "get_job_status"):
                        key = (rec["tool"] + ":" + str(a.get("resource", "")), str(a.get("pathPrefix", "")))
                        samples.setdefault(key, [line, line])[1] = line
            z.writestr("listing_samples.jsonl", "".join(x + "\n" for pair in samples.values() for x in dict.fromkeys(pair)))
            slog = Path(os.environ.get("CVA_MCP_SERVER_LOG") or (Path.home() / ".aws-transform-mcp" / "server.log"))
            z.writestr("aws_transform_server_log_tail.log", tail(slog, 300))
            z.writestr("login_check.json", json.dumps(self.login_check(), indent=2, default=str))
            z.writestr("mcp_server_tail.log", tail(run_dir / "out" / "transform_job" / "mcp_server.log", 300))
            z.writestr("audit_transform_events.json", json.dumps(events, indent=2, default=str))
            z.writestr("state_excerpt.json", json.dumps({"transform_pending": st.get("transform_pending"), "offline_estimate": st.get("offline_estimate"),
                                                         "imported": bool(st.get("transform_assessment"))}, indent=2, default=str))
            z.writestr("versions.txt", f"A01 {a01_v}\nmcp {mcp_v}\npython {platform.python_version()}\nplatform {platform.platform()}\n"
                                       f"AWS_PROFILE set: {bool(os.environ.get('AWS_PROFILE'))}\nAWS_REGION: {os.environ.get('AWS_REGION')}\n")
            cfg = res_cfg = self.result(rid).cfg.root / "transform.toml"
            z.writestr("config_transform.toml", cfg.read_text(encoding="utf-8") if res_cfg.exists() else "")
        return {"file": target.name, "bytes": target.stat().st_size}

    def transform_say(self, rid: str, text: str, by: str, client: Any = None) -> dict[str, Any]:
        """Relay a message you typed IN A01 to the paused AWS Transform job, so nobody has to type in the AWS web chat.  Sent unchanged."""
        text = (text or "").strip()
        if not by.strip():
            raise ServiceError("say who is sending this (your name is recorded)")
        if not text or len(text) > 2000:
            raise ServiceError("type a message of 1 to 2000 characters")
        self._need(rid)
        pend = self.store.state(rid).get("transform_pending") or {}
        if not (pend.get("workspaceId") and pend.get("jobId")):
            raise ServiceError("there is no paused AWS Transform job to send this to")
        client = client or (self.transform_client_factory() if self.transform_client_factory else None)
        own = client is None
        if own:
            env = self._mcp_env({})
            try:
                client = self._start_mcp(env, self.store.path(rid) / "out" / "mcp_server_say.log")
            except Exception as e:
                raise ServiceError(f"could not start the AWS Transform MCP server: {e}") from e
        try:
            try:                                             # a fresh session with the server: the job's instructions must be loaded first
                client.call("load_instructions", {"workspaceId": pend["workspaceId"], "jobId": pend["jobId"]})
            except Exception:
                pass
            client.call("send_message", {"workspaceId": pend["workspaceId"], "jobId": pend["jobId"], "text": text, "skipPolling": True})
        finally:
            if own:
                client.close()
        self.audit(rid).append("transform_message_sent_by_person", {"text": text[:500], "jobId": pend["jobId"]}, by)
        return {"sent": True}

    def import_transform(self, rid: str, path: str, by: str, mode: str = "console_fallback") -> dict[str, Any]:
        st = self._open(rid)
        res = self.result(rid)
        ad = get_adapter(mode, res.cfg, st["mapping_overrides"])
        r = ad.import_results(Path(path))
        if r.status != "imported":
            raise ServiceError("the AWS Transform results could not be read: " + "; ".join(i.message for i in r.issues))
        st["transform"] = {"mode": mode, "status": r.status, "per_workload": r.per_workload, "source": Path(path).name}
        self.store.save_state(rid, st)
        self.audit(rid).append("transform_results_imported", {"file": Path(path).name, "servers": len(r.per_workload),
                                                              "manual_step": mode == "console_fallback"}, by)
        return r.to_dict()

    # ------------------------------------------------------------------ export
    @staticmethod
    def default_template() -> str | None:
        """Ingram's OLA deck shipped with A01: every export is made from it unless another template is named."""
        p = Path(__file__).resolve().parent.parent / "templates" / "Ingram_OLA_Template.pptx"
        return str(p) if p.exists() else None

    def export(self, rid: str, template: str | None = None, pdf: bool = True) -> dict[str, Any]:
        self._need(rid)
        res, st = self.result(rid), self.store.state(rid)
        if not res.sizing_source:
            raise ServiceError(
                "Cannot export yet: the deck's rightsizing slide needs per-server sizing, and AWS Transform's per-server results were not imported "
                "(the analysis workbook was missing or unreadable: use 'Fetch the results now' again, or import the zip). Alternatively run A01's offline "
                "estimate; the cost figures on the slides still come from the AWS Transform assessment." if res.aws else
                "Cannot export yet: no cost figures exist. Import or run an AWS Transform assessment (its per-server results give the rightsizing "
                "slide), or run A01's offline estimate.")
        g = gate_mod.evaluate(res, st)
        a = self.audit(rid)
        out = self.store.path(rid) / "out"
        out.mkdir(exist_ok=True)
        suffix = "FINAL" if st["finalised"] else "DRAFT"
        wb_name = f"{rid}_supporting_data_{suffix}.xlsx"
        a.append("export_started", {"status": suffix})
        v = a.verify()
        recent = a.entries()[-60:]
        com = commentary_for(res, st)
        build_workbook(res, st, g, {**v, "head": a.head(), "recent": recent}, com, rid, str(out / wb_name))
        deck_name = f"{rid}_OLA_{suffix}.pptx"
        d = build_deck(res, st, g, com, rid, wb_name, str(out / deck_name), None if template == "placeholder" else (template or self.default_template()))
        files = {"workbook": str(out / wb_name), "deck": str(out / deck_name)}
        notes = list(d["problems"])
        if pdf:
            pth, msg = convert_to_pdf(out / deck_name, out)
            if pth:
                files["pdf"] = pth
            else:
                notes.append(msg)
        csv_path = out / f"{rid}_exceptions_{suffix}.csv"
        with open(csv_path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["id", "severity", "code", "workload", "message", "source_rows", "acknowledged"])
            for i in res.issues:
                w.writerow([i.id, i.severity, i.code, i.workload or "", i.message, "; ".join(i.refs),
                            "yes" if gate_mod.is_acked(i, st) else "no"])
        files["exceptions_csv"] = str(csv_path)
        a.append("export", {"status": suffix, "files": {k: Path(v).name for k, v in files.items()}, "notes": notes})
        return {"status": suffix, "files": files, "notes": notes, "gate_open": g["open"]}

    def purge(self, rid: str, confirm: bool) -> None:
        self._need(rid)
        if not confirm:
            raise ServiceError("purge needs explicit confirmation")
        with open(self.store.base / "purge.log", "a", encoding="utf-8") as fh:   # the run's own audit log goes with it
            fh.write(json.dumps({"ts": now_iso(), "run": rid, "event": "purged"}) + "\n")
        shutil.rmtree(self.store.path(rid))
        self._cache.pop(rid, None)
