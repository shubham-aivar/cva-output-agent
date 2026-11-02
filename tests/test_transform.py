"""AWS Transform integration: importer, enrichment, guard, driver (against a scripted fake service that reproduces the
behaviour seen in the real manual run), MCP client allow-list (against a real MCP server process), service wiring."""
import csv
import io
import json
import os
import shutil
import sys
import textwrap
import zipfile
from pathlib import Path

import pytest
from openpyxl import load_workbook
from pptx import Presentation

from cva.config import load_config, load_toml
from cva.ingest import rvtools
from cva.service import ServiceError
from cva.transform import enrichment, guard, results
from cva.transform.driver import AssessmentDriver, DriverInputs, NeedsHuman
from cva.transform.mcp_client import ALLOWED_TOOLS, BLOCKED_TOOLS, McpToolClient, ToolBlocked, ToolError
from tests.conftest import FIX

WS, JOB = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
CFG = load_toml(Path(__file__).resolve().parent.parent / "config" / "transform.toml")
GOOD = ("Region us-east-1. Pricing On-Demand, 1-year and 3-year Reserved Instances (No Upfront) side by side. Tenancy Shared. "
        "Instance selection Cost optimized. Windows and RHEL License Included. Sustainability: Disabled. "
        "Older instance families are excluded (t2, m4, c4, r4, i3, a1). Ready to proceed?")
BAD = GOOD.replace("Sustainability: Disabled", "Sustainability: enabled (PUE 1.55)")


# ------------------------------------------------------------------------------------------------ importer (real files)
def test_real_results_are_parsed():
    a = results.load_results(FIX / "aggregated_output.json")
    m = {x.slug: x for x in a.models}
    assert round(m["on_demand"].total, 1) == 122338.5 and round(m["three_year_nuri"].total, 1) == 66969.1
    assert m["three_year_nuri"].recommended and not m["on_demand"].recommended
    assert round(m["on_demand"].by_category["compute"], 1) == 100385.3
    assert a.servers["in_scope"] == 55 and a.servers["excluded"] == 2 and round(a.on_prem_total) == 158981
    assert a.assessments["compute/ec2"]["annual_cost"] == 55016.84


def test_warning_csv_becomes_aggregated_exceptions():
    issues, n = results.parse_errors(FIX / "error_file.csv")
    assert n == 251 and {i.code for i in issues} == {"TRW-UTIL", "TRW-IOPS-ZERO", "TRW-IOPS-NONE"}
    assert {i.code: i.detail["count"] for i in issues} == {"TRW-UTIL": 55, "TRW-IOPS-ZERO": 98, "TRW-IOPS-NONE": 98}
    assert all(i.severity == "warn" for i in issues)


def test_aws_assessment_round_trips_through_state(tmp_path):
    a = results.load_results(FIX / "aggregated_output.json")
    b = results.AwsAssessment.from_dict(json.loads(json.dumps(a.to_dict())))
    assert [m.slug for m in b.models] == [m.slug for m in a.models] and b.on_prem_total == a.on_prem_total


def test_importer_never_searches_outside_the_given_folder(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "error_file.csv").write_text("resource_id,resource_name,resource_type,severity,reason,impact,recommendation\n")
    inner = tmp_path / "inner"
    inner.mkdir()
    (inner / "aggregated_output.json").write_bytes((FIX / "aggregated_output.json").read_bytes())
    a = results.load_results(inner / "aggregated_output.json")
    assert "error_file.csv" not in a.files                               # the sibling folder was not touched
    with pytest.raises(FileNotFoundError):
        results.load_results(outside)


def test_zip_with_deliverables_and_zip_slip(tmp_path):
    z = tmp_path / "Initial_Assessment_x.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("analysis_x.xlsx", b"x")
        zf.writestr("business_case_x.pptx", b"x")
        zf.writestr("report_x.pdf", b"x")
    (tmp_path / "aggregated_output.json").write_bytes((FIX / "aggregated_output.json").read_bytes())
    a = results.load_results(tmp_path)
    assert {"analysis.xlsx", "business_case.pptx", "report.pdf"} <= set(a.files)
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("../escape.txt", b"x")
    with pytest.raises(ValueError, match="unsafe"):
        results._safe_extract(evil, tmp_path / "dest")


# ------------------------------------------------------------------------------------------------ enrichment (real template)
def test_enrichment_matches_the_real_template_and_changes_only_what_it_should(tmp_path):
    cfg = load_config()
    est = rvtools.read(FIX / "rvtools_sample.xlsx", cfg).estate
    out = tmp_path / "filled.csv"
    rep = enrichment.fill_template(FIX / "server_enrichment.csv", out, est, cfg)
    assert rep.template_rows == 57 and rep.matched == 57 and not rep.unmatched_template and not rep.unmatched_workloads
    assert rep.skipped_powered_off == 2
    a = list(csv.DictReader(open(FIX / "server_enrichment.csv", encoding="utf-8-sig")))
    b = list(csv.DictReader(open(out, encoding="utf-8")))
    assert list(a[0]) == list(b[0]) and len(a) == len(b)                  # same 55 columns, same rows, same order
    changed = {k for x, y in zip(a, b) for k in x if x[k] != y[k]}
    assert changed <= {"average_cpu_utilization_percentage", "average_memory_utilization_percentage", "environment",
                       "p95_cpu_utilization_percentage", "peak_memory_utilization_percentage"}
    web = next(r for r in b if r["primary_hostname"] == "WEB-P01")
    assert web["p95_cpu_utilization_percentage"] == "" and web["peak_memory_utilization_percentage"] == ""   # defaults blanked
    assert float(web["average_cpu_utilization_percentage"]) > 0 and web["environment"] == "Production"
    assert any("snapshot" in n.lower() for n in rep.notes)               # snapshot sent as averages only
    off = next(r for r in b if r["current_power_state"] == "poweredoff")
    assert off["p95_cpu_utilization_percentage"] == "25"                  # powered-off rows are left untouched


def test_enrichment_can_keep_template_defaults(tmp_path, monkeypatch):
    cfg = load_config()
    m = cfg.mapping("aws_transform_enrichment")
    m["policy"]["blank_unknown"] = False
    est = rvtools.read(FIX / "rvtools_sample.xlsx", cfg).estate            # read with the real mappings first
    orig = type(cfg).mapping
    monkeypatch.setattr(type(cfg), "mapping", lambda self, name, overrides=None: m if name == "aws_transform_enrichment" else orig(self, name, overrides))
    enrichment.fill_template(FIX / "server_enrichment.csv", tmp_path / "f.csv", est, cfg)
    web = next(r for r in csv.DictReader(open(tmp_path / "f.csv")) if r["primary_hostname"] == "WEB-P01")
    assert web["p95_cpu_utilization_percentage"] == "25"


# ------------------------------------------------------------------------------------------------ guard
def test_guard_blocks_unwanted_settings_and_notes_defaults():
    ok = guard.check(GOOD, CFG["expected"])
    assert ok.ok and ok.accepted_defaults                               # exclusions of old families are noted, not blocking
    bad = guard.check(BAD, CFG["expected"])
    assert not bad.ok and bad.forbidden
    missing = guard.check(GOOD.replace("1-year and 3-year Reserved Instances (No Upfront)", ""), CFG["expected"])
    assert not missing.ok and "1-year pricing" in missing.missing


def test_settings_text_spells_out_everything():
    t = guard.settings_text(CFG["settings"])
    for word in ("us-east-1", "Reserved Instance", "Shared", "Cost optimized", "License Included", "sustainability"):
        assert word.lower() in t.lower()


# ------------------------------------------------------------------------------------------------ scripted fake service
class FakeTransform:
    """Reproduces the sequence seen in the real run: ingest, proposal, proceed, outputs, enrichment, re-run, proposal, outputs."""

    def __init__(self, proposal=GOOD, never_proposes=False):
        self.calls, self.msgs, self.uploads, self.sent = [], [], [], []
        self.arts, self.n, self.gen, self.proposal, self.never = [], 0, 0, proposal, never_proposes
        self.outputs_ready = self.template_ready = False
        self.state, self.ids_only, self.needs_poke = "EXECUTING", False, False
        self.newest_first, self.omit_ts = False, False        # the live listing: newest first, and (possibly) no timestamps
        self.status_replies, self.notice_after_status = [], False
        self.premature, self.prepare_left, self.ignored_proceeds = False, 0, 0   # a status report that sounds like a decision while the job still prepares; a go-ahead sent too early is ignored
        self.ignore_first_proceed, self._ignored_once, self.failure_reason = False, False, ""
        self.odd_question = None                                         # a question the service asks while waiting that is NOT the scenario decision
        self.wedged, self.wedge_after_proceed = False, False             # a STUCK server session: every call times out (seen live), until a fresh one is started
        self.status_after_proceed = None                              # the job status the service reports once the go-ahead is accepted (FAILED, COMPLETED ...)
        self.ignore_always, self.notice_after_proceed, self._notice_pending = False, False, False   # the go-ahead never takes; a "send me a message" notice after the go-ahead
        self.run2 = False                                             # serve the second real run: its totals (113,026 ...) and its real analysis workbook
        self.not_connected = None                                     # None | "all" | "workspace": the server is not signed in (seen live)
        self.scenario, self.real_paths, self.root_has_folders, self.json_as_asset = "Cost Optimized Assessment", "basename", True, False   # mode "real"
        self.tasks, self.task_on_proceed, self.job_status, self.status_failures = [], None, None, 0     # human tasks the job waits on; one that appears at the go-ahead; a forced job status; N timeouts of the status call
        self.task_form, self.completed = False, []
        self.timeouts, self.listing_exception, self.listing_error_code = [], None, None          # per-call timeouts seen; (substring, exception) to raise on a listing
        self.zip_name = "Cost_Optimized_Assessment_20261020_141412.zip"                         # the deliverables zip is named after the scenario
        self.zip_after, self._scen_lists, self.fail_listing = 0, 0, False                  # the zip lands after N listings of the scenario folder; a refused listing
        self.extra_files = False                                      # the job tree also holds an inventory review and the uploaded file
        self.announce_reports = False                                 # post "Reports ready ..." (no files attached) after the go-ahead
        self.full_only, self.artifacts_in_messages = False, None      # only full-path prefixes work; the service names its files (with ids) in a message
        self.folder_mode, self.rel_only = "ids", False          # artifact listing: folders as entries with ids, or (live) as bare paths without ids
        self.require_instructions, self.instructed, self.rejected, self.refuse_send = False, set(), [], False   # live: job tools need load_instructions per session
        self.chatty, self.util, self.await_after_proceed = False, None, False       # free-text replies; the agreed utilization; a question after the go-ahead
        self.readback, self.echo_user = None, False             # what the service lists when asked; echo our own messages back (sender unreadable)
        self.ws_mode = self.job_mode = "normal"            # normal | no_id (answer carries no id; the listing does) | error
        self.ws_name = self.job_name = None
        self.question_first = None                        # seen live: asked to list its configuration, the service answers with a QUESTION first and lists nothing

    def _id(self):
        self.n += 1
        return f"00000000-0000-4000-8000-{self.n:012d}"

    def _agent(self, t, artifacts=None):
        self.msgs.append({"role": "assistant", "text": t, "timestamp": f"2026-10-20T10:{len(self.msgs):02d}:00Z", "messageId": f"m{len(self.msgs)}",
                          **({"artifacts": artifacts} if artifacts else {})})
        if "ready to proceed" in t.lower():
            self.state = "AWAITING_HUMAN_INPUT"

    def _on_message(self, text):
        low = text.lower()
        if "uploaded" in low:
            if self.odd_question:
                self._agent(self.odd_question)
                self.state = "AWAITING_HUMAN_INPUT"
            elif self.needs_poke:             # VERIFIED real behaviour: the service posts a notice and waits for any message
                self._agent("I have an update for you. Please send a message (e.g., 'status') to see the details.")
                self.state = "AWAITING_HUMAN_INPUT"
            elif not self.never:
                self._agent(self.proposal)
        elif low.startswith("before i confirm"):                   # the read-back request: the service lists its configuration
            if not (self.premature and self.prepare_left > 0):      # (answering a question does not make a job that is still preparing wait for a go-ahead)
                self.state = "AWAITING_HUMAN_INPUT"
            if self.question_first:
                self.state = "AWAITING_HUMAN_INPUT"
                self._agent(self.question_first)
            else:
                self._agent((self.readback or self.proposal) + (f"\nUtilization assumptions: {self.util}" if self.util else ""))
        elif self.question_first and low.startswith("proceed with the current inventory"):      # option 3 of its own question: now it presents the configuration
            self.question_first = None
            self.state = "AWAITING_HUMAN_INPUT"
            self._agent("Understood. " + (self.readback or self.proposal))
        elif low.strip() == "status" and self._notice_pending:
            self._notice_pending, self.state, self.outputs_ready = False, "EXECUTING", True
            self._agent("Running the assessment now.")
        elif low.strip() == "status" and self.premature and self.prepare_left > 0:
            self.state = "EXECUTING"
            self._agent("Current status: the inventory is still being processed. When that finishes you can proceed with defaults or give your own values. "
                        "How would you like to proceed?")
        elif low.strip() == "status" and self.needs_poke:
            reply = self.status_replies.pop(0) if self.status_replies else self.proposal
            decision_like = any(k in reply.lower() for k in ("awaiting your", "how would you like to proceed", "ready to proceed", "ready for review"))
            self.state = "AWAITING_HUMAN_INPUT" if decision_like else "EXECUTING"
            self._agent(reply)
            if reply != self.proposal and self.notice_after_status:        # later, the service posts a NEW notice and waits again
                self._agent("I have an update for you. Please send a message (e.g., 'status') to see the details.")
                self.state = "AWAITING_HUMAN_INPUT"
        elif low.startswith("proceed") and ((self.premature and self.state != "AWAITING_HUMAN_INPUT") or (self.ignore_first_proceed and (not self._ignored_once or self.ignore_always))):
            self.ignored_proceeds += 1                                      # the service was not waiting for it: the go-ahead is ignored
            if self.ignore_first_proceed:
                self._ignored_once, self.state = True, "AWAITING_HUMAN_INPUT"
                self._agent(LIVE_STATUS)                                    # ... and it asks again
        elif low.startswith("proceed") and self.await_after_proceed:      # live-like: after the go-ahead the service asks one more thing
            self.state = "AWAITING_HUMAN_INPUT"
            self._agent("I need one more confirmation: should the business case include storage? Please reply yes or no.")
        elif low.startswith("proceed") or (low.startswith("yes") and self.await_after_proceed):
            if low.startswith("yes"):
                self._agent("Thanks, running the assessment now.")
            self.state = "EXECUTING"
            self.gen += 1
            self.outputs_ready = True
            self.out_ids = {n: self._id() for n in ("aggregated_output.json", "error_file.csv", "Initial_Assessment_x.zip")}
            if self.status_after_proceed:
                self.job_status = self.status_after_proceed
            if self.wedge_after_proceed:
                self.wedged = True
            if self.notice_after_proceed:                                 # after the go-ahead the service posts a notice and waits for any message
                self._agent("I have an update for you. Please send a message (e.g., 'status') to see the details.")
                self.state, self.outputs_ready, self._notice_pending = "AWAITING_HUMAN_INPUT", False, True
            if self.task_on_proceed:                                      # the job is blocked on a person's approval until it is answered
                self.tasks, self.state, self.outputs_ready = [dict(self.task_on_proceed)], "AWAITING_HUMAN_INPUT", False
            if self.announce_reports:
                self._agent("Reports ready for scenario 'Initial Assessment'.")
            if self.artifacts_in_messages:
                names = list(self.out_ids) if self.artifacts_in_messages == "all" else ["error_file.csv", "Initial_Assessment_x.zip"]
                self._agent("Reports ready for scenario 'Initial Assessment'.", artifacts=[{"artifactId": self.out_ids[n], "fileName": n} for n in names])
        elif "enrichment template" in low:
            self.template_ready, self.tpl_id = True, self._id()
        elif "apply this enrichment" in low:
            self._agent("The enrichment merged. Re-run the existing scenario or create a new one? Ready to proceed?")
        elif "re-run the existing" in low:
            self.outputs_ready = False
            self._agent(self.proposal)
        elif self.chatty:                                              # a free-text suggestion from the person
            if "cpu" in low and "memory" in low:
                self.util = text.strip()
            self.state = "AWAITING_HUMAN_INPUT"
            self._agent(f"Updated as requested: {text.strip()}. How would you like to proceed?")

    wrap = False        # reproduce the live shape: every answer arrives as a JSON STRING, {"success": true, "data": {...}}

    accepts_timeout = True

    def call(self, name, args, timeout=None):
        self.timeouts.append((name, timeout))
        if self.wedged:
            raise TimeoutError(f"'{name}' did not answer within {timeout or 240:.0f} s")
        le = self.listing_exception
        if name == "list_resources" and args.get("resource") == "artifacts" and le and le[0] in (args.get("pathPrefix") or "") and (len(le) < 3 or le[2] > 0):
            if len(le) == 3:
                le[2] -= 1                                   # (substring, exception, times): fail only that many times
            raise le[1]
        resp = self._handle(name, args)
        return {"result": json.dumps({"success": True, "data": resp})} if self.wrap else resp

    def _real_listing(self, args):
        """The REAL answer of list_resources(resource="artifacts"), as the server's source and the API model give it: {success, data: {artifacts: [{artifactId,
        fileMetadata: {path}, sizeInBytes, artifactType}], assets: [{assetKey}], folders: ["<exact prefix>/"], connectorId}}.  A pathPrefix is matched EXACTLY
        (S3 semantics); with no prefix both stores are merged and nothing is descended into."""
        root = f"AWSTransform/Workspaces/{WS}/Jobs/{JOB}/"
        p = args.get("pathPrefix")
        gen = root + "Generated Outputs/"
        scen = gen + self.scenario + "/"
        ao = scen + "assessment outputs/"

        def art(i, path, size, ftype="JSON"):
            return {"artifactId": i, "fileMetadata": {"path": path, "description": ""}, "sizeInBytes": size, "jobId": JOB,
                    "artifactType": {"categoryType": "CUSTOMER_OUTPUT", "fileType": ftype, "schemaType": ""}}

        def ok(artifacts=(), folders=(), assets=(), cid=None):
            d = {"artifacts": list(artifacts), "assets": list(assets), "folders": list(folders)}
            if cid:
                d["connectorId"] = cid
            return {"success": True, "data": d}

        def pth(base):
            return (ao if base == "aggregated_output.json" else scen).replace(root, "") + base if self.real_paths == "full" else base
        if p is None:
            return ok(folders=[gen, root + "User Uploads/"] if self.root_has_folders else [])
        if p == gen:
            files = [art("d7491744-292d-477d-854b-1c602b143e40", "d7491744-292d-477d-854b-1c602b143e40.json", 38600),
                     art("e80c1f86-4db1-4448-9b51-88e6227cd1e7", "e80c1f86-4db1-4448-9b51-88e6227cd1e7.json", 38600)]
            return ok(files, [scen, gen + "Ingestion outputs/"] if self.outputs_ready else [gen + "Ingestion outputs/"])
        if p == root + "User Uploads/":
            return ok([art("up-0001-bbbb", "rvtools_sample.xlsx", 5000, "XLSX")])
        if p == scen and self.outputs_ready:
            self._scen_lists += 1
            files = [art(self.out_ids["error_file.csv"], pth("error_file.csv"), 1200, "CSV")]
            if self._scen_lists > self.zip_after:
                files.append(art(self.out_ids["Initial_Assessment_x.zip"], pth(self.zip_name), 4_500_000, "ZIP"))
            return ok(files, [ao])
        if p == ao and self.outputs_ready:
            if self.json_as_asset:
                return ok(assets=[{"assetKey": f"transform-output/{JOB}/{self.scenario}/assessment outputs/aggregated_output.json", "sizeInBytes": 36200}], cid="conn-0001")
            return ok([art(self.out_ids["aggregated_output.json"], pth("aggregated_output.json"), 36200)])
        return ok()

    def new_session(self):
        """A new MCP session with the server: the service forgets which jobs had their instructions loaded."""
        self.instructed = set()

    NOT_CONFIGURED = {"content": [{"type": "text", "text": {"success": False, "error": {"code": "NOT_CONFIGURED", "message": "Not connected to AWS Transform.",
                      "suggestedAction": "Call configure with authMode \"cookie\" or \"sso\"."}}}], "isError": True}      # the answer seen live

    def _handle(self, name, args):
        self.calls.append((name, args))
        if self.not_connected == "all" or (self.not_connected == "workspace" and name == "create_workspace"):
            return self.NOT_CONFIGURED
        if self.require_instructions and name != "load_instructions" and args.get("jobId") and args["jobId"] not in self.instructed:
            self.rejected.append(name)                                  # refused, and NOT performed: this is what a skipped load_instructions does
            return {"success": False, "error_code": "INSTRUCTIONS_REQUIRED", "error": "Call load_instructions for this job first."}
        if name == "send_message" and self.refuse_send:
            return {"success": False, "error": "This job is not accepting messages right now."}
        if name == "get_status":
            return {"status": "connected", "apiAccess": "available"}
        if name == "create_workspace":
            self.ws_name = args["name"]
            return {"workspaceId": WS} if self.ws_mode == "normal" else (
                {"error": "A workspace with this name already exists"} if self.ws_mode == "error" else {"text": "Workspace created."})
        if name == "create_job":
            self.job_name = args["jobName"]
            return {"jobId": JOB} if self.job_mode == "normal" else {"text": "Job started."}
        if name == "list_resources" and args["resource"] == "workspaces":
            return {"workspaces": [{"name": self.ws_name, "workspaceId": WS}] if (self.ws_name and self.ws_mode == "no_id") else []}
        if name == "list_resources" and args["resource"] == "jobs":
            return {"jobs": [{"name": self.job_name, "jobId": JOB}] if (self.job_name and self.job_mode == "no_id") else []}
        if name == "load_instructions":
            self.instructed.add(args["jobId"])
            return {"ok": True}
        if name == "upload_artifact":
            self.uploads.append(args)
            return {"artifactId": self._id()}
        if name == "send_message":
            self.sent.append(args["text"])
            if self.echo_user:                                      # our own message appears in the listing with no sender and no timestamp
                self.msgs.append({"text": args["text"], "messageId": f"e{len(self.msgs)}"})
            self._on_message(args["text"])
            return {"sent": True}
        if name == "get_job_status":                                     # the REAL snapshot of get_job_status(detailed=true)
            if self.status_failures > 0:
                self.status_failures -= 1
                raise TimeoutError("'get_job_status' did not answer within 90 s")
            st = self.job_status or self.state
            pend = [dict(t) for t in self.tasks]
            if self.premature and self.prepare_left > 0:                  # the job is still preparing; when it is ready it asks, and waits
                self.prepare_left -= 1
                if self.prepare_left == 0:
                    self.state = "AWAITING_HUMAN_INPUT"
                    self._agent(LIVE_STATUS)
                    st = self.job_status or self.state
            return {"job": {"jobId": JOB, "jobName": "a01 job", "statusDetails": {"status": st, **({"failureReason": self.failure_reason} if st == "FAILED" else {})}},
                    "worklogs": {"worklogs": []}, "tasks": {"hitlTasks": pend}, "messages": {"messages": [dict(m) for m in self.msgs]}, "plan": {"steps": []},
                    "_pollingGuidance": {"isTerminal": False, "jobStatus": "UNKNOWN",                 # the server reads a field the job record does not have: ALWAYS "UNKNOWN"
                                         "hasPendingTasks": bool(pend), "pendingTaskCount": len(pend)}}
        if name == "get_resource" and args.get("resource") == "task":
            t = next((x for x in self.tasks if x["taskId"] == args.get("taskId")), None)
            if t is None:
                return {"success": False, "error": {"code": "NOT_FOUND", "message": "No such open task."}}
            return {"task": {**t, **({"_responseTemplate": {"choice": ""}} if self.task_form else {})}}
        if name == "complete_task":
            for need in ("workspaceId", "jobId", "taskId"):
                if not args.get(need):
                    return {"success": False, "error": {"code": "VALIDATION_ERROR", "message": f"{need} is required."}}
            t = next((x for x in self.tasks if x["taskId"] == args["taskId"]), None)
            if t is None:
                return {"success": False, "error": {"code": "TASK_NOT_FOUND", "message": "No such open task."}}
            act = args.get("action", "APPROVE")
            self.completed.append((args["taskId"], act, args.get("content")))
            self.tasks = [x for x in self.tasks if x["taskId"] != args["taskId"]]
            if act == "REJECT":
                self.job_status = "FAILED"
            elif not self.tasks:
                self.state, self.outputs_ready, self.job_status = "EXECUTING", True, None
            return {"success": True, "data": {"taskId": args["taskId"], "action": act}}
        if name == "list_resources" and args["resource"] == "messages":
            if self.ids_only:                       # a listing of summaries without their text
                return {"messages": [{"messageId": m["messageId"], "timestamp": m["timestamp"], "sender": "AGENT"} for m in self.msgs]}
            ms = [{"role": "user", "text": "hello", "timestamp": "2026-10-20T09:00:00Z", "messageId": "u0"}] + [dict(m) for m in self.msgs]
            if self.omit_ts:
                ms = [{k: v for k, v in m.items() if k != "timestamp"} for m in ms]
            return {"messages": ms[::-1] if self.newest_first else ms}
        if name == "get_resource" and args.get("resource") == "messages":
            return {"messages": [m for m in self.msgs if m["messageId"] in args["messageIds"]]}
        if name == "list_resources" and args["resource"] == "artifacts" and self.folder_mode == "search_only":      # only a search by file name finds anything
            term = args.get("searchTerm")
            if not term or not self.outputs_ready:
                return {"artifacts": []}
            return {"artifacts": [{"artifactId": i, "fileName": n} for n, i in self.out_ids.items() if term.lower() in n.lower()]}
        if name == "list_resources" and args["resource"] == "artifacts" and self.folder_mode == "paged":           # the outputs are on the SECOND page
            if not args.get("nextToken"):
                return {"artifacts": [{"artifactId": "inv-0001-aaaa", "fileName": "inventory-review.json"}], "nextToken": "t2"}
            return {"artifacts": [{"artifactId": i, "fileName": n} for n, i in self.out_ids.items()] if self.outputs_ready else []}
        if name == "list_resources" and args["resource"] == "artifacts" and self.fail_listing:
            return {"success": False, "error": {"code": self.listing_error_code or "ACCESS_DENIED", "message": "You are not allowed to list this job."}}
        if name == "list_resources" and args["resource"] == "artifacts" and self.folder_mode == "real":
            return self._real_listing(args)
        if name == "list_resources" and args["resource"] == "artifacts" and self.folder_mode == "empty":
            return {"artifacts": [], "folders": []}
        if name == "list_resources" and args["resource"] == "artifacts" and self.folder_mode == "partial":
            root = f"AWSTransform/Workspaces/{WS}/Jobs/{JOB}/"
            rel = (args.get("pathPrefix") or "").replace(root, "")
            files = []
            if rel == "":
                return {"artifacts": [], "folders": [root + "Generated Outputs/"]}
            if rel == "Generated Outputs/":
                return {"artifacts": [], "folders": [root + "Generated Outputs/Initial Assessment/"]}
            if rel == "Generated Outputs/Initial Assessment/" and self.outputs_ready:       # the zip and the csv are listed; the 'assessment outputs' folder is not
                files += [{"artifactId": self.out_ids[n], "fileName": n} for n in ("error_file.csv", "Initial_Assessment_x.zip")]
            return {"artifacts": files, "folders": []}
        if name == "list_resources" and args["resource"] == "artifacts" and self.folder_mode == "strings":
            root = f"AWSTransform/Workspaces/{WS}/Jobs/{JOB}/"
            p = args.get("pathPrefix") or ""
            if self.rel_only and p.startswith("AWSTransform/"):
                raise RuntimeError("invalid prefix")                    # this service form refuses the full path
            if self.full_only and p and not p.startswith("AWSTransform/"):
                raise RuntimeError("invalid prefix")                    # this one refuses the relative form
            rel = p.split(f"/Jobs/{JOB}/", 1)[1] if f"/Jobs/{JOB}/" in p else p
            files = []
            if rel == "":
                return {"artifacts": [], "folders": [root + "Generated Outputs/", root + "User Uploads/"]}
            if rel == "User Uploads/" and self.extra_files:
                return {"artifacts": [{"artifactId": "up-0001-bbbb", "fileName": "rvtools_sample.xlsx"}], "folders": []}
            if rel == "Generated Outputs/":
                if self.template_ready:
                    files.append({"artifactId": self.tpl_id, "fileName": "server_enrichment.csv"})
                if self.extra_files:
                    files.append({"artifactId": "inv-0001-aaaa", "fileName": "inventory-review.json"})
                return {"artifacts": files, "folders": [root + "Generated Outputs/Initial Assessment/"]}
            if rel == "Generated Outputs/Initial Assessment/":
                if self.outputs_ready:
                    files += [{"artifactId": self.out_ids[n], "fileName": n} for n in ("error_file.csv", "Initial_Assessment_x.zip")]
                return {"artifacts": files, "folders": [root + "Generated Outputs/Initial Assessment/assessment outputs/"]}
            if rel == "Generated Outputs/Initial Assessment/assessment outputs/" and self.outputs_ready:
                return {"artifacts": [{"artifactId": self.out_ids["aggregated_output.json"], "fileName": "aggregated_output.json"}], "folders": []}
            return {"artifacts": [], "folders": []}
        if name == "list_resources" and args["resource"] == "artifacts":
            if not args.get("pathPrefix"):
                return {"artifacts": [{"artifactId": "folder-1", "fileName": "Generated Outputs/"}]}
            files = []
            if self.outputs_ready:
                files += [{"artifactId": i, "fileName": n} for n, i in self.out_ids.items()]
            if self.template_ready:
                files.append({"artifactId": self.tpl_id, "fileName": "server_enrichment.csv"})
            return {"artifacts": files}
        if name == "get_resource":                                       # the REAL server's rules (read from its source)
            res = args.get("resource")
            for need in (("workspaceId", "jobId", "artifactId") if res == "artifact" else ("workspaceId", "jobId", "connectorId", "assetKey")):
                if not args.get(need):
                    return {"success": False, "error": {"code": "VALIDATION_ERROR", "message": f"{need} is required for downloading an {res}."}}
            sp = args["savePath"]
            fn = args.get("fileName") or args.get("artifactId") or args["assetKey"].split("/")[-1]
            if sp.endswith("/") or "." not in os.path.basename(sp):         # a directory: the file name is appended
                target = Path(sp) / os.path.basename(fn)
            else:                                                              # a full file path: the file name is ignored
                target = Path(os.path.dirname(sp)) / os.path.basename(sp)
            target.parent.mkdir(parents=True, exist_ok=True)
            base = target.name
            if base == "aggregated_output.json" or base == "error_file.csv" or base == "server_enrichment.csv":
                target.write_bytes((FIX / ("aggregated_output_run2.json" if (base == "aggregated_output.json" and self.run2) else base)).read_bytes())
            else:
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w") as zf:
                    if self.run2:                                              # the real deliverables: the workbook is the real one
                        zf.writestr("analysis_20261020_202003.xlsx", (FIX / "analysis_run2.xlsx").read_bytes())
                        zf.writestr("business_case_20261020_202003.pptx", b"x")
                        zf.writestr("report_20261020_202003.pdf", b"x")
                    else:
                        zf.writestr("analysis_x.xlsx", b"x")
                target.write_bytes(buf.getvalue())
            return {"success": True, "data": {"artifactId": args.get("artifactId"), "savedTo": str(target), "sizeBytes": target.stat().st_size}}
        raise AssertionError(f"unexpected tool call {name}")


def make_driver(fake, **kw):
    t = iter(range(0, 10**6, 50))
    return AssessmentDriver(fake, CFG, sleep=lambda s: None, clock=lambda: next(t), **kw)


def inputs(tmp_path, **kw):
    f = tmp_path / "rvtools.xlsx"
    f.write_bytes((FIX / "rvtools_sample.xlsx").read_bytes())
    return DriverInputs(files=[f], out_dir=tmp_path / "job", kind="rvtools", **kw)           # names (workspace_name, job_name) may be passed in kw


def test_driver_happy_path_uses_only_allowed_tools_and_imports_the_result(tmp_path):
    fake = FakeTransform()
    res = make_driver(fake).run(inputs(tmp_path))
    names = [c[0] for c in fake.calls]
    assert set(names) <= ALLOWED_TOOLS and not (set(names) & BLOCKED_TOOLS) and "complete_task" not in names
    cj = next(a for n, a in fake.calls if n == "create_job")
    assert cj["orchestratorAgent"] == "migration-assessment-agent-v2" and "us-east-1" in cj["objective"] and "Cost optimized" in cj["objective"]
    up = fake.uploads[0]
    assert up["fileType"] == "XLSX" and up["fileName"] == "rvtools.xlsx" and Path(up["content"]).exists()
    assert sum(1 for m in fake.sent if m.lower().startswith("proceed")) == 1
    assert res.passes == 1 and res.workspace_id == WS and res.job_id == JOB
    assert round(res.assessment.model("three_year_nuri").total) == 66969 and res.assessment.warning_rows == 251
    assert names.index("create_job") < names.index("load_instructions") < names.index("upload_artifact") < names.index("send_message")


def test_driver_refuses_to_confirm_a_mismatching_proposal_until_a_person_overrides(tmp_path):
    fake = FakeTransform(proposal=BAD)
    with pytest.raises(NeedsHuman) as e:
        make_driver(fake).run(inputs(tmp_path))
    assert "does not match" in e.value.reason and "Sustainability is enabled" in e.value.reason and e.value.workspace_id == WS and e.value.job_id == JOB
    assert not any(m.lower().startswith("proceed") for m in fake.sent)         # nothing was confirmed
    # a person looks at it and overrides; the run resumes the SAME job (no new workspace or job)
    fake.calls.clear()
    res = make_driver(fake).run(inputs(tmp_path, accept_proposal=True, resume=(WS, JOB)))
    names = [c[0] for c in fake.calls]
    assert "create_workspace" not in names and "create_job" not in names and "upload_artifact" not in names
    assert any(m.lower().startswith("proceed") for m in fake.sent) and res.passes == 1


def test_driver_stops_cleanly_on_timeout(tmp_path):
    fake = FakeTransform(never_proposes=True)
    with pytest.raises(NeedsHuman, match="(?i)timed out") as e:
        make_driver(fake).run(inputs(tmp_path))
    assert "Last seen" in e.value.reason and "0 new agent message" in e.value.reason          # a wait is never silent about what it saw


def test_driver_refuses_a_file_type_the_service_does_not_list(tmp_path):
    fake = FakeTransform()
    with pytest.raises(NeedsHuman, match="not listed"):
        make_driver(fake).run(inputs(tmp_path, ) if False else DriverInputs([tmp_path / "x.xlsx"], tmp_path / "o", kind="cloudamize_ola"))
    assert fake.calls == []                                                   # refused before touching AWS at all


def test_driver_enrichment_pass_fills_uploads_and_resends_full_settings(tmp_path):
    cfg = load_config()
    est = rvtools.read(FIX / "rvtools_sample.xlsx", cfg).estate
    fake = FakeTransform()
    res = make_driver(fake).run(inputs(tmp_path, enrich=lambda tpl, dst: enrichment.fill_template(tpl, dst, est, cfg).to_dict()))
    assert res.passes == 2 and res.enrichment["matched"] == 57
    second = fake.uploads[-1]
    assert second["fileName"] == "server_enrichment.csv" and second["fileType"] == "CSV"
    web = next(r for r in csv.DictReader(open(second["content"], encoding="utf-8")) if r["primary_hostname"] == "WEB-P01")
    assert web["p95_cpu_utilization_percentage"] == ""                         # the default was blanked, not sent on
    rerun = next(m for m in fake.sent if m.lower().startswith("re-run"))
    assert "Cost optimized" in rerun and "us-east-1" in rerun                   # a re-run does not keep settings: they are re-sent
    assert sum(1 for m in fake.sent if m.lower().startswith("proceed")) == 2   # each proposal was checked and confirmed separately
    assert "complete_task" not in [c[0] for c in fake.calls]


def test_driver_writes_a_trace_of_raw_responses(tmp_path):
    fake = FakeTransform()
    make_driver(fake, trace_path=tmp_path / "trace.jsonl").run(inputs(tmp_path))
    lines = [json.loads(x) for x in (tmp_path / "trace.jsonl").read_text().splitlines()]
    assert lines and {"tool", "response"} <= set(lines[0])


# ------------------------------------------------------------------------------------------------ MCP client (real MCP process)
def _fake_server(tmp_path):
    p = tmp_path / "fake_server.py"
    p.write_text(textwrap.dedent('''
        import sys
        print("NOISE-LINE: No endpoints ruleset found for service elasticgumbyfrontendservice", file=sys.stderr, flush=True)
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("fake-transform")
        @mcp.tool()
        def get_status() -> dict:
            return {"status": "connected", "region": "us-east-1"}
        @mcp.tool()
        def create_workspace(name: str) -> dict:
            return {"workspaceId": "ws-" + name}
        @mcp.tool()
        def complete_task(workspaceId: str, jobId: str, taskId: str) -> dict:
            return {"approved": True}
        @mcp.tool()
        def list_resources(resource: str) -> dict:
            raise ValueError("boom")
        mcp.run()
    '''))
    return p


def test_mcp_client_round_trip_allow_list_and_errors(tmp_path):
    pytest.importorskip("mcp.server.fastmcp")
    with McpToolClient(sys.executable, [str(_fake_server(tmp_path))], timeout=60, errlog_path=str(tmp_path / "server.log")) as c:
        assert {"get_status", "complete_task"} <= c.tool_names()
        assert c.call("get_status", {})["region"] == "us-east-1"
        assert c.call("create_workspace", {"name": "a01"})["workspaceId"] == "ws-a01"
        assert c.call("complete_task", {"workspaceId": "w", "jobId": "j", "taskId": "t"})["approved"] is True       # allowed: only the driver's answer_task calls it
        with pytest.raises(ToolBlocked):
            c.call("delete_job", {"workspaceId": "w", "jobId": "j"})                                              # exists on the server, blocked in our code
        with pytest.raises(ToolBlocked):
            c.call("something_unknown", {})
        with pytest.raises(ToolError):
            c.call("list_resources", {"resource": "jobs"})


def test_blocked_and_allowed_tool_sets_are_disjoint_and_cover_the_dangerous_ones():
    assert not (ALLOWED_TOOLS & BLOCKED_TOOLS)
    for t in ("delete_job", "delete_workspace", "control_job", "accept_connector", "create_connector", "configure", "manage_collaborator", "switch_profile"):
        assert t in BLOCKED_TOOLS
    assert "complete_task" in ALLOWED_TOOLS                                           # a person's decision on a task can be relayed ...
    import pathlib
    import re
    callers = [f.name for f in pathlib.Path(__file__).resolve().parent.parent.joinpath("cva").rglob("*.py")
               if re.search(r'_call\("complete_task"|\.call\("complete_task"', f.read_text())]
    assert callers == ["driver.py"]                                                    # ... and only from the driver, in one place (answer_task)


# ------------------------------------------------------------------------------------------------ service wiring
@pytest.fixture()
def aws_run(svc, tmp_path):
    rid = svc.create_run([str(FIX / "rvtools_sample.xlsx")], as_of="2026-10-20")
    folder = tmp_path / "res"
    folder.mkdir()
    for f in ("aggregated_output.json", "error_file.csv"):
        (folder / f).write_bytes((FIX / f).read_bytes())
    return svc, rid, folder


def test_imported_assessment_replaces_the_figures_and_keeps_a01_as_cross_check(aws_run):
    svc, rid, folder = aws_run
    before = svc.overview(rid)
    assert before["meta"]["figures_source"] == "none" and before["scenarios"] == []        # nothing is calculated until asked
    svc.run_offline_estimate(rid, "tester")                                                # the fallback / cross-check, on request
    assert svc.overview(rid)["meta"]["figures_source"] == "a01_estimate"
    svc.import_transform_results(rid, str(folder), "tester")
    ov = svc.overview(rid)
    assert ov["meta"]["figures_source"] == "aws_transform"
    sc = {s["id"]: s for s in ov["scenarios"]}
    assert round(sc["on_demand"]["total"]) == 122339 and sc["on_demand"]["source"] == "aws_transform"
    assert sc["byol"]["status"] == "not_in_assessment" and sc["byol"]["total"] is None
    f = ov["figures"]
    assert round(f["scenario.on_demand.total"]) == 122339 and f["a01.on_demand.total"] != f["scenario.on_demand.total"]
    assert ov["exceptions"]["by_code"].get("TRW-UTIL") == 1                    # AWS's warnings join the one exceptions list
    t = svc.result(rid).parity_summary["totals"]
    assert t["aws_compute"] and t["a01_compute"] and t["aws_vs_a01_pct"] is not None
    assert svc.audit_view(rid)["verify"]["ok"]


def test_outputs_carry_aws_figures_a_sheet_and_still_prove_their_arithmetic(aws_run):
    svc, rid, folder = aws_run
    svc.import_transform_results(rid, str(folder), "tester")
    svc.run_offline_estimate(rid, "tester")               # the deck's rightsizing slide needs A01's per-server estimate
    out = svc.export(rid, pdf=False, template="placeholder")
    wb = load_workbook(out["files"]["workbook"])
    assert "AWS Transform" in wb.sheetnames
    texts = "\n".join(str(c.value) for r in wb["Summary"].iter_rows() for c in r if c.value)
    assert "AWS Transform" in texts and "imported" in texts           # AWS figures are labelled as imported
    assert "a01.on_demand.total" in texts and "scenario.on_demand.total" in texts   # A01 cross-check keeps its own self-check
    prs = Presentation(out["files"]["deck"])
    deck = "\n".join(sh.text_frame.text for s in prs.slides for sh in s.shapes if sh.has_text_frame)
    assert "AWS Transform" in deck and "$122,339" in deck


def test_transform_run_through_the_service_imports_and_audits(aws_run):
    svc, rid, _ = aws_run
    fake = FakeTransform()
    out = svc.transform_run(rid, client=fake, sleep=lambda s: None, require_approval=False)       # automatic mode: proceeds on a match
    assert out["passes"] == 1 and out["job"]["jobId"] == JOB
    assert svc.overview(rid)["aws"]["job"]["jobId"] == JOB
    ev = [e["event"] for e in svc.audit_view(rid)["entries"]]
    assert "transform_assessment_imported" in ev and "transform_run_completed" in ev and "transform_tool" in ev


def test_transform_run_records_what_a_person_must_decide(aws_run):
    from cva.service import ServiceError
    svc, rid, _ = aws_run
    with pytest.raises(ServiceError, match="needs a person"):
        svc.transform_run(rid, client=FakeTransform(proposal=BAD), sleep=lambda s: None)
    pend = svc.store.state(rid)["transform_pending"]
    assert pend["jobId"] == JOB and pend["guard"]["forbidden"]
    assert svc.overview(rid)["meta"]["figures_source"] == "none"                  # nothing was imported


# ------------------------------------------------------------------------------------------------ gate, view and the UI's background job
def test_gate_requires_the_figures_source_to_be_settled(svc):
    rid = svc.create_run([str(FIX / "rvtools_sample.xlsx")], as_of="2026-10-20")
    chk = next(c for c in svc.gate(rid)["checks"] if c["id"] == "figures_source")
    assert not chk["ok"] and chk["blocking"] and "no cost figures exist yet" in chk["detail"]
    with pytest.raises(Exception, match="no offline estimate to accept"):
        svc.ack_estimate(rid, "Rev", "nothing to accept yet")
    svc.run_offline_estimate(rid, "Rev")
    chk = next(c for c in svc.gate(rid)["checks"] if c["id"] == "figures_source")
    assert not chk["ok"] and "OFFLINE" in chk["detail"]
    svc.ack_estimate(rid, "Rev", "demo: offline estimate accepted")
    assert next(c for c in svc.gate(rid)["checks"] if c["id"] == "figures_source")["ok"]
    rid2 = svc.create_run([str(FIX / "rvtools_sample.xlsx")], as_of="2026-10-20")
    folder = Path(svc.store.path(rid2)) / "res"
    folder.mkdir()
    for f in ("aggregated_output.json", "error_file.csv"):
        (folder / f).write_bytes((FIX / f).read_bytes())
    svc.import_transform_results(rid2, str(folder), "t")
    c2 = next(c for c in svc.gate(rid2)["checks"] if c["id"] == "figures_source")
    assert c2["ok"] and not c2["blocking"]                         # an imported assessment settles it


def test_transform_view_reports_state_and_environment(svc):
    rid = svc.create_run([str(FIX / "rvtools_sample.xlsx")], as_of="2026-10-20")
    v = svc.transform_view(rid)
    assert v["imported"] is False and v["listed_input"] is True and v["assessment"] is None and v["pending"] is None
    assert {"mcp_installed", "uvx", "server_command"} <= set(v["environment"]) and "region" in v["settings_sent"]
    ola = svc.create_run([str(FIX / "cloudamize_ola_public_sample.xlsx")], as_of="2026-10-20")
    assert svc.transform_view(ola)["listed_input"] is False        # AWS Transform does not list this Cloudamize export


@pytest.fixture()
def ui(svc):
    import threading
    from http.server import ThreadingHTTPServer

    from cva.ui import server as srv
    srv.JOBS.clear()
    srv.Handler.svc = svc
    srv.Handler.demo_samples = True       # the sample-assessment import is a demo feature
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", svc
    httpd.shutdown()


def _http(url, body=None):
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _job_started(base, rid):
    return _http(f"{base}/api/run/{rid}/transform")[1]["job"].get("started")


def _wait_job(base, rid, states, after=None):
    """Wait for a job in ``states``; with ``after`` (a previous start time) only a NEWER job counts, so a stale result is never read."""
    import time
    for _ in range(300):
        v = _http(f"{base}/api/run/{rid}/transform")[1]
        if v["job"]["status"] in states and (after is None or v["job"].get("started") != after):
            return v
        time.sleep(0.05)
    raise AssertionError("background job did not reach " + str(states))


def test_ui_background_job_pauses_for_a_person_then_resumes_on_their_override(ui):
    base, svc = ui
    fake = FakeTransform(proposal=BAD)
    svc.transform_client_factory, svc.transform_sleep = (lambda: fake), (lambda s: None)
    rid = svc.create_run([str(FIX / "rvtools_sample.xlsx")], as_of="2026-10-20")
    assert _http(f"{base}/api/run/{rid}/transform_start", {"by": ""})[0] == 400                    # a name is required
    assert _http(f"{base}/api/run/{rid}/transform_resume", {"by": "Rev"})[0] == 400                # nothing paused yet
    assert _http(f"{base}/api/run/{rid}/transform_start", {"by": "Rev"})[1]["started"] is True
    v = _wait_job(base, rid, {"needs_person", "error", "done"})
    assert v["job"]["status"] == "needs_person" and "Sustainability is enabled" in v["pending"]["reason"]
    assert v["pending"]["guard"]["forbidden"] and not v["imported"]                              # nothing was confirmed or imported
    assert not any(m.lower().startswith("proceed") for m in fake.sent)
    t0 = _job_started(base, rid)
    _http(f"{base}/api/run/{rid}/transform_resume", {"by": "Rev", "accept_proposal": False})       # check again: still a mismatch
    assert _wait_job(base, rid, {"needs_person", "done", "error"}, after=t0)["job"]["status"] == "needs_person"
    t1 = _job_started(base, rid)
    _http(f"{base}/api/run/{rid}/transform_resume", {"by": "Rev", "accept_proposal": True})         # the person overrides
    v = _wait_job(base, rid, {"done", "error"}, after=t1)
    assert v["job"]["status"] == "done" and v["imported"] and v["pending"] is None
    ev = svc.audit_view(rid)["entries"]
    ov = next(e for e in ev if e["event"] == "transform_guard_overridden_by_person")
    assert ov["actor"] == "Rev"                                                                      # the override is attributed to a person
    assert svc.overview(rid)["meta"]["figures_source"] == "aws_transform" and svc.audit_view(rid)["verify"]["ok"]


def test_ui_serves_imported_files_without_path_traversal(ui, tmp_path):
    import urllib.error
    import urllib.request
    base, svc = ui
    rid = svc.create_run([str(FIX / "rvtools_sample.xlsx")], as_of="2026-10-20")
    folder = tmp_path / "res"
    folder.mkdir()
    for f in ("aggregated_output.json", "error_file.csv"):
        (folder / f).write_bytes((FIX / f).read_bytes())
    assert _http(f"{base}/api/run/{rid}/import_results", {"by": "Rev", "sample": "aws_transform_assessment_rvtools"})[0] == 200
    with urllib.request.urlopen(f"{base}/download/{rid}/transform/aggregated_output.json") as r:
        assert r.status == 200 and b"pricing_comparison" in r.read()
    for bad in (f"{base}/download/{rid}/transform/..%2F..%2Fstate.json", f"{base}/download/{rid}/transform/nope.json"):
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(bad)
        assert e.value.code == 404
    assert _http(f"{base}/api/run/{rid}/import_results", {"by": "Rev", "sample": "../../etc/passwd"})[0] == 400


def test_ui_never_prints_null_and_uses_no_innerhtml():
    html = (Path(__file__).resolve().parent.parent / "cva" / "ui" / "static" / "index.html").read_text()
    assert "innerHTML" not in html
    i = html.index("async function awsView")
    block = html[i:html.index("\n}\n", i)]
    assert block.count("v.append(") == 1 and "const put =" in block   # only the null-safe helper appends; DOM append() would print "null"


# ------------------------------------------------------------------------------------------------ stall, cancel, external confirmation
def test_driver_reads_message_text_with_get_resource_when_the_listing_has_only_ids(tmp_path):
    fake = FakeTransform()
    fake.ids_only = True
    res = make_driver(fake).run(inputs(tmp_path))
    assert res.passes == 1 and any(c[0] == "get_resource" and c[1].get("resource") == "messages" for c in fake.calls)


def test_driver_stops_with_what_it_saw_when_the_job_waits_for_a_person_but_the_message_is_unrecognised(tmp_path):
    fake = FakeTransform(never_proposes=True)
    fake.state = "AWAITING_HUMAN_INPUT"
    fake._agent("Which Windows Server edition do your SQL databases use?")           # a question A01 does not recognise as a proposal
    with pytest.raises(NeedsHuman) as e:
        make_driver(fake).run(inputs(tmp_path))
    assert "does not recognise" in e.value.reason and "Which Windows Server edition" in e.value.proposal
    assert not any(m.lower().startswith("proceed") for m in fake.sent)               # it never guessed a reply
    assert fake.sent.count("status") == 0 and len(fake.sent) == 1                     # it did not speak over the question: only the start-up message was sent


def test_driver_says_so_when_the_job_is_awaiting_input_but_no_message_can_be_read(tmp_path):
    fake = FakeTransform(never_proposes=True)
    fake.state = "AWAITING_HUMAN_INPUT"
    with pytest.raises(NeedsHuman, match="could not read any message"):
        make_driver(fake).run(inputs(tmp_path))


def test_driver_stops_when_the_service_reports_a_failed_job(tmp_path):
    fake = FakeTransform(never_proposes=True)
    fake.state = "FAILED"
    with pytest.raises(NeedsHuman, match="reports the job as FAILED"):
        make_driver(fake).run(inputs(tmp_path))


def test_a_person_can_stop_the_wait_and_the_job_is_left_alone(tmp_path):
    import threading
    fake = FakeTransform(never_proposes=True)
    ev = threading.Event()
    ev.set()
    with pytest.raises(NeedsHuman, match="Stopped by a person") as e:
        make_driver(fake).run(inputs(tmp_path, cancel=ev))
    assert "A01 cannot stop it" in e.value.reason and e.value.job_id == JOB
    assert {c[0] for c in fake.calls} <= ALLOWED_TOOLS and not any(c[0] in ("control_job", "delete_job") for c in fake.calls)


def test_resume_after_the_person_confirmed_in_the_web_application_never_sends_proceed(tmp_path):
    fake = FakeTransform(proposal=BAD)
    with pytest.raises(NeedsHuman):
        make_driver(fake).run(inputs(tmp_path))
    fake.outputs_ready, fake.out_ids = True, {n: fake._id() for n in ("aggregated_output.json", "error_file.csv", "Initial_Assessment_x.zip")}
    fake.sent.clear()
    res = make_driver(fake).run(inputs(tmp_path, resume=(WS, JOB), confirmed_externally=True))
    assert res.passes == 1 and not fake.sent                                         # A01 only waited for outputs and imported them


# ------------------------------------------------------------------------------------------------ option C: nothing calculated by default
def test_default_run_has_no_a01_figures_until_the_offline_estimate_is_chosen(svc):
    rid = svc.create_run([str(FIX / "rvtools_sample.xlsx")], as_of="2026-10-20")
    ov = svc.overview(rid)
    assert ov["estimate_ran"] is False and ov["scenarios"] == [] and ov["meta"]["figures_source"] == "none"
    assert not any(k.startswith(("sizing.", "scenario.", "a01.")) for k in ov["figures"])        # no figure nobody asked for
    assert all(w["instance"] is None and w["on_demand"] is None for w in svc.workloads(rid))     # the inventory only
    from cva.service import ServiceError
    with pytest.raises(ServiceError, match="Cannot export yet"):
        svc.export(rid, pdf=False)
    with pytest.raises(ServiceError, match="your name"):
        svc.run_offline_estimate(rid, " ")
    out = svc.run_offline_estimate(rid, "Rev")
    assert out["estimate_ran"] and len(out["scenarios"]) == 4
    assert all(w["instance"] for w in svc.workloads(rid) if w["vcpu"]) and svc.export(rid, pdf=False)["status"] == "DRAFT"
    assert any(e["event"] == "offline_estimate_run" and e["actor"] == "Rev" for e in svc.audit_view(rid)["entries"])


def test_export_with_an_assessment_but_no_estimate_explains_what_is_missing(svc, tmp_path):
    from cva.service import ServiceError
    rid = svc.create_run([str(FIX / "rvtools_sample.xlsx")], as_of="2026-10-20")
    folder = tmp_path / "r"
    folder.mkdir()
    for f in ("aggregated_output.json", "error_file.csv"):
        (folder / f).write_bytes((FIX / f).read_bytes())
    svc.import_transform_results(rid, str(folder), "t")
    assert svc.overview(rid)["scenarios"][0]["source"] == "aws_transform"
    with pytest.raises(ServiceError, match="rightsizing slide needs per-server sizing"):
        svc.export(rid, pdf=False)


def test_sql_coverage_says_not_detected_instead_of_100_percent(svc):
    rid = svc.create_run([str(FIX / "rvtools_sample.xlsx")], as_of="2026-10-20")
    cov = svc.result(rid).lic_cov
    assert cov["sql_pct"] is None and cov["sql_unknown"] == 6                    # RVTools cannot see SQL Server; 6 database VMs need evidence
    assert svc.result(rid).ledger.value("licensing.coverage_sql") is None


def test_each_database_vm_raises_one_warning_not_two(svc):
    rid = svc.create_run([str(FIX / "rvtools_sample.xlsx")], as_of="2026-10-20")
    codes = [i["code"] for i in svc.issues(rid)]
    assert "QS-DB-UNDETECTED" not in codes
    assert sum(1 for i in svc.issues(rid) if i["code"] == "LI-GAP" and "database VM" in i["message"]) == 6


def test_upsize_means_the_workload_needs_more_not_that_the_catalog_instance_is_bigger(cfg):
    from cva.engines.pricebook import PriceBook
    from cva.engines.rightsize import size_workload
    from cva.model import Workload
    w = Workload(id="w", name="w", os_family="linux", vcpu=2, ram_gb=3, cpu_avg=10, cpu_peak=20, ram_peak_gb=2.0, storage_used_gb=10)
    r = size_workload(w, "A", cfg, PriceBook(cfg))
    assert r.ok and r.rec_mem_gb > 3 * 1.3 and r.direction != "upsize"            # needs 2.4 GB of 3; the catalog's smallest fit is 4 GB
    needy = Workload(id="n", name="n", os_family="linux", vcpu=2, ram_gb=4, cpu_avg=70, cpu_peak=95, ram_peak_gb=3.9, storage_used_gb=10)
    assert size_workload(needy, "A", cfg, PriceBook(cfg)).direction == "upsize"


def test_ui_stop_waiting_and_confirmed_in_web_app_and_run_estimate(ui):
    import time
    base, svc = ui
    fake = FakeTransform(never_proposes=True)
    svc.transform_client_factory, svc.transform_sleep = (lambda: fake), (lambda s: time.sleep(0.01))
    rid = svc.create_run([str(FIX / "rvtools_sample.xlsx")], as_of="2026-10-20")
    assert _http(f"{base}/api/run/{rid}/run_estimate", {"by": ""})[0] == 400
    assert _http(f"{base}/api/run/{rid}/run_estimate", {"by": "Rev"})[0] == 200 and svc.overview(rid)["estimate_ran"]
    assert _http(f"{base}/api/run/{rid}/transform_cancel", {"by": "Rev"})[0] == 400              # nothing running yet
    assert _http(f"{base}/api/run/{rid}/transform_start", {"by": "Rev"})[1]["started"] is True
    for _ in range(200):                                                                         # the log shows what it is waiting for
        v = _http(f"{base}/api/run/{rid}/transform")[1]
        if any("waiting for" in e for e in v["job"]["events"]):
            break
        time.sleep(0.02)
    assert v["job"]["status"] == "running" and any("new agent message(s)" in e for e in v["job"]["events"])
    assert _http(f"{base}/api/run/{rid}/transform_cancel", {"by": "Rev"})[1]["stopping"] is True
    v = _wait_job(base, rid, {"needs_person", "error", "done"})
    assert v["job"]["status"] == "needs_person" and "Stopped by a person" in v["job"]["message"] and "Resume with" not in v["job"]["message"]
    assert any(e["event"] == "transform_stop_waiting_requested" for e in svc.audit_view(rid)["entries"])
    fake.outputs_ready, fake.out_ids = True, {n: fake._id() for n in ("aggregated_output.json", "error_file.csv", "Initial_Assessment_x.zip")}
    t2 = _job_started(base, rid)
    _http(f"{base}/api/run/{rid}/transform_resume", {"by": "Rev", "confirmed_externally": True})
    v = _wait_job(base, rid, {"done", "error"}, after=t2)
    assert v["job"]["status"] == "done" and v["imported"]
    assert any(e["event"] == "transform_confirmed_externally" and e["actor"] == "Rev" for e in svc.audit_view(rid)["entries"])


# ------------------------------------------------------------------------------------------------ the verified real behaviour: the service waits for a message
def test_driver_nudges_the_service_when_it_asks_for_a_message(tmp_path):
    """Seen in the AWS Transform web application: 'I have an update for you. Please send a message (e.g., status)...' and nothing
    more until a message arrives.  A01 must send a read-only status request and then read the proposal."""
    fake = FakeTransform()
    fake.needs_poke = True
    res = make_driver(fake).run(inputs(tmp_path))
    assert fake.sent.count("status") == 1 and res.passes == 1
    assert sum(1 for m in fake.sent if m.lower().startswith("proceed")) == 1          # the nudge is not a confirmation
    assert fake.sent.index("status") < next(i for i, m in enumerate(fake.sent) if m.lower().startswith("proceed"))


def test_a_nudged_proposal_that_does_not_match_still_stops_for_a_person(tmp_path):
    fake = FakeTransform(proposal=BAD)
    fake.needs_poke = True
    with pytest.raises(NeedsHuman, match="does not match"):
        make_driver(fake).run(inputs(tmp_path))
    assert fake.sent.count("status") == 1 and not any(m.lower().startswith("proceed") for m in fake.sent)


def test_without_the_nudge_the_old_behaviour_would_have_waited_forever(tmp_path):
    """Regression for the real stall: with nudging switched off the same service never reveals its proposal."""
    cfg = json.loads(json.dumps(CFG))
    cfg["assessment"]["max_pokes"] = 0
    fake = FakeTransform()
    fake.needs_poke = True
    t = iter(range(0, 10**6, 50))
    drv = AssessmentDriver(fake, cfg, sleep=lambda s: None, clock=lambda: next(t))
    with pytest.raises(NeedsHuman, match="(?i)timed out|did not recognise") as e:
        drv.run(inputs(tmp_path))
    assert "I have an update for you" in (e.value.proposal or e.value.reason) or "agent message" in e.value.reason
    assert fake.sent.count("status") == 0


# ------------------------------------------------------------------------------------------------ the default UI offers nothing pre-loaded
def _b64(path):
    import base64
    return {"name": Path(path).name, "b64": base64.b64encode(Path(path).read_bytes()).decode()}


def test_default_ui_offers_no_samples_and_builds_a_run_only_from_an_upload(ui):
    base, svc = ui
    from cva.ui import server as srv
    srv.Handler.demo_samples = False                                                       # the default: your own files only
    try:
        assert _http(f"{base}/api/app")[1] == {"demo_samples": False}
        assert _http(f"{base}/api/samples")[1] == []                                       # nothing bundled is listed
        assert _http(f"{base}/api/runs")[1]["runs"] == []                                  # and nothing exists until you upload
        code, body = _http(f"{base}/api/runs", {"samples": ["rvtools_sample_export_2.xlsx"]})
        assert code == 400 and "switched off" in body["error"]                             # enforced by the server, not just hidden
        assert _http(f"{base}/api/runs", {"licensing_sample": "x.xlsx", "files": [_b64(FIX / "rvtools_sample.xlsx")]})[0] == 400
        assert _http(f"{base}/api/runs", {})[0] == 400                                     # no files: asks for an upload
        code, r = _http(f"{base}/api/runs", {"files": [_b64(FIX / "rvtools_sample.xlsx")], "label": "uploaded"})
        assert code == 200
        ov = _http(f"{base}/api/run/{r['run_id']}/overview")[1]
        assert ov["estate"]["workloads"] == 57 and ov["scenarios"] == []                   # populated from the upload alone; no costs
        code, body = _http(f"{base}/api/run/{r['run_id']}/import_results", {"by": "Rev", "sample": "aws_transform_assessment_rvtools"})
        assert code == 400 and "switched off" in body["error"]                             # the sample assessment is demo-only too
        srv.Handler.demo_samples = True                                                    # demo mode turns the samples on
        assert _http(f"{base}/api/app")[1]["demo_samples"] is True and len(_http(f"{base}/api/samples")[1]) > 0
        assert _http(f"{base}/api/run/{r['run_id']}/import_results", {"by": "Rev", "sample": "aws_transform_assessment_rvtools"})[0] == 200
    finally:
        srv.Handler.demo_samples = True


def test_the_page_never_opens_an_earlier_run_by_itself_and_labels_demo_samples():
    html = (Path(__file__).resolve().parent.parent / "cva" / "ui" / "static" / "index.html").read_text()
    assert "await loadRuns(!S.run)" not in html and "await loadRuns(false);          // never open an earlier run" in html
    assert 'S.app.demo_samples ? await api("/api/samples") : []' in html                   # the dialog lists samples only in demo mode
    assert "DEMO sample" in html and "No runs yet" in html


def test_new_run_dialog_never_passes_null_straight_to_the_dom():
    html = (Path(__file__).resolve().parent.parent / "cva" / "ui" / "static" / "index.html").read_text()
    i = html.index("async function newRun()")
    block = html[i:html.index('$("#newrun")', i)]
    assert 'demo ? el("label"' not in block and "...(demo ? [" in block        # replaceChildren() would print the word null for a missing child


def test_aws_tab_does_not_claim_an_offline_estimate_exists_when_none_was_run():
    html = (Path(__file__).resolve().parent.parent / "cva" / "ui" / "static" / "index.html").read_text()
    assert "so there are no cost figures. Run or import one below." in html
    assert 'S.ov.estimate_ran ? "No assessment imported yet: the only figures' in html


# ------------------------------------------------------------------------------------------------ creation answers without a readable id
def test_driver_finds_the_workspace_and_job_by_name_when_the_answers_carry_no_id(tmp_path):
    fake = FakeTransform()
    fake.ws_mode = fake.job_mode = "no_id"
    res = make_driver(fake).run(inputs(tmp_path, workspace_name="a01-x-1", job_name="a01-x-1-assessment"))
    assert res.workspace_id == WS and res.job_id == JOB and res.passes == 1
    assert [c[0] for c in fake.calls if c[0] == "list_resources" and c[1]["resource"] in ("workspaces", "jobs")]      # looked them up by name


def test_driver_shows_the_services_own_answer_when_no_id_can_be_found(tmp_path):
    fake = FakeTransform()
    fake.ws_mode = "error"
    with pytest.raises(NeedsHuman) as e:
        make_driver(fake).run(inputs(tmp_path, workspace_name="a01-run-0001"))
    assert "Could not read the workspace id" in e.value.reason and "already exists" in e.value.reason       # the service's words, on screen
    assert "a01-run-0001" in e.value.reason
    assert not any(c[0] in ("create_job", "upload_artifact", "send_message") for c in fake.calls)         # it went no further


def test_every_attempt_gets_its_own_workspace_and_job_names(aws_run):
    svc, rid, _ = aws_run
    names = []
    for _ in range(2):
        fake = FakeTransform()
        svc.transform_run(rid, client=fake, sleep=lambda s: None, require_approval=False)
        names.append((fake.ws_name, fake.job_name))
    assert names[0] != names[1] and names[0][0] != f"a01-{rid}"                      # never the bare, repeatable "a01-run-0001"
    assert names[0][1].startswith(names[0][0]) and names[0][1].endswith("-assessment")


# ------------------------------------------------------------------------------------------------ the live answer shape: JSON inside a string
def test_unwrap_decodes_envelopes_but_leaves_real_message_text_alone():
    from cva.transform import shape
    raw = {"result": json.dumps({"success": True, "data": {"messages": [{"messageId": "m1", "text": "Perfect! Processing.", "sender": "AGENT"}]}})}
    out = shape.unwrap(raw)
    assert out["result"]["data"]["messages"][0]["text"] == "Perfect! Processing."
    assert [m["text"] for m in shape.collect_messages(out)] == ["Perfect! Processing."]                  # one message, not one blob
    odd = {"messages": [{"text": '{"note": "a message that merely looks like JSON"}', "sender": "AGENT"}]}
    assert shape.unwrap(odd)["messages"][0]["text"].startswith('{"note"')                               # not an envelope: left as text
    wrapped_in_text = {"text": json.dumps({"success": True, "data": {"jobId": "j-1"}})}                  # the envelope under a text key IS decoded
    assert shape.find_id(shape.unwrap(wrapped_in_text), ("jobId",)) == "j-1"


def test_mcp_client_parse_decodes_string_wrapped_answers():
    from types import SimpleNamespace

    from cva.transform.mcp_client import _parse
    body = json.dumps({"success": True, "data": {"status": "AWAITING_HUMAN_INPUT"}})
    for res in (SimpleNamespace(content=[SimpleNamespace(text=body)], structuredContent=None, isError=False),
                SimpleNamespace(content=[SimpleNamespace(text=body)], structuredContent={"result": body}, isError=False)):
        assert _parse(res)["data"]["status"] == "AWAITING_HUMAN_INPUT"


def test_driver_works_on_answers_wrapped_as_json_strings(tmp_path):
    fake = FakeTransform()
    fake.wrap, fake.needs_poke = True, True
    res = make_driver(fake).run(inputs(tmp_path))
    assert res.passes == 1 and fake.sent.count("status") == 1 and res.workspace_id == WS
    drv = make_driver(FakeTransform())
    drv.c.wrap, drv.c.state = True, "AWAITING_HUMAN_INPUT"
    assert drv._job_state(WS, JOB) == "AWAITING_HUMAN_INPUT"                                          # the state is read, not "unknown"


def test_the_guard_checks_only_the_latest_proposal_never_older_messages(tmp_path):
    """Regression: with the whole reply read as ONE message, an earlier message that echoed every setting made a changed final
    proposal look correct."""
    for wrap in (False, True):
        fake = FakeTransform(proposal="Region us-east-1. Pricing On-Demand, 1-year and 3-year RI No Upfront. Tenancy Shared. "
                                      "Licensing License Included. Sustainability: Disabled. Ready to proceed?")     # "Cost optimized" is missing
        fake.wrap = wrap
        fake._agent("Settings noted: us-east-1, On-Demand, 1-year and 3-year RI, No Upfront, Shared, Cost optimized, License Included.")
        with pytest.raises(NeedsHuman) as e:
            make_driver(fake).run(inputs(tmp_path))
        assert "cost optimized" in e.value.reason.lower() and not any(m.lower().startswith("proceed") for m in fake.sent)


def test_the_servers_own_log_goes_to_a_file_not_the_terminal(tmp_path):
    pytest.importorskip("mcp.server.fastmcp")
    log = tmp_path / "server.log"
    with McpToolClient(sys.executable, [str(_fake_server(tmp_path))], timeout=60, errlog_path=str(log)) as c:
        c.call("get_status", {})
    assert "NOISE-LINE" in log.read_text()


# ------------------------------------------------------------------------------------------------ seen live: a nudge can be answered with a status report
def test_a_new_notice_after_a_status_report_gets_its_own_nudge(tmp_path):
    """Live: the first nudge was answered with 'Current Status: Primary Assessment: still in progress'.  The service later posts a NEW
    notice and waits again.  Counting nudges per message (not once for the whole wait) is what lets A01 answer it."""
    fake = FakeTransform()
    fake.needs_poke, fake.notice_after_status = True, True
    fake.status_replies = ["**Current Status:** Primary Assessment \u2014 Still in progress."]
    res = make_driver(fake).run(inputs(tmp_path))
    assert fake.sent.count("status") == 2 and res.passes == 1
    assert sum(1 for m in fake.sent if m.lower().startswith("proceed")) == 1


def test_nudges_are_bounded_per_message_and_in_total(tmp_path):
    cfg = json.loads(json.dumps(CFG))
    cfg["assessment"]["max_total_pokes"] = 3
    fake = FakeTransform(never_proposes=True)
    fake.needs_poke, fake.notice_after_status = True, True
    fake.status_replies = ["still working"] * 50                                   # every nudge is answered with a status report and a new notice
    fake._agent("I have an update for you. Please send a message (e.g., 'status') to see the details.")
    fake.state = "AWAITING_HUMAN_INPUT"
    t = iter(range(0, 10**6, 50))
    with pytest.raises(NeedsHuman, match="(?i)timed out|did not recognise"):
        AssessmentDriver(fake, cfg, sleep=lambda s: None, clock=lambda: next(t)).run(inputs(tmp_path))
    assert fake.sent.count("status") == 3                                         # never an endless stream of messages


def test_listing_order_does_not_matter_newest_first_or_oldest_first_with_or_without_timestamps(tmp_path):
    for newest_first in (True, False):
        for omit_ts in (True, False):
            fake = FakeTransform()
            fake.needs_poke, fake.newest_first, fake.omit_ts, fake.wrap = True, newest_first, omit_ts, True
            sub = tmp_path / f"{newest_first}{omit_ts}"
            sub.mkdir()
            res = make_driver(fake).run(inputs(sub))
            assert res.passes == 1 and fake.sent.count("status") == 1, (newest_first, omit_ts)


def test_an_old_proposal_is_never_taken_as_the_answer_to_a_later_question(tmp_path):
    """After the enrichment message, only a message that is NEW counts, even if the old proposal is still in the listing."""
    cfg = load_config()
    est = rvtools.read(FIX / "rvtools_sample.xlsx", cfg).estate
    fake = FakeTransform()
    fake.newest_first, fake.omit_ts = True, True
    res = make_driver(fake).run(inputs(tmp_path, enrich=lambda tpl, dst: enrichment.fill_template(tpl, dst, est, cfg).to_dict()))
    assert res.passes == 2 and sum(1 for m in fake.sent if m.lower().startswith("proceed")) == 2


# ------------------------------------------------------------------------------------------------ the LIVE wording (web application screenshots, 20 Oct 2026)
LIVE_NOTICE = "I have an update for you. Please send a message (e.g., 'status') to see the details."
LIVE_STATUS = ("Your assessment is ready for review and execution:\n\nScenario: Primary Assessment\nStatus: Still being set up (awaiting your "
               "confirmation to proceed)\n\nCurrent State:\n- Inventory processed: 57 servers, 101 storage volumes\n- Configuration applied: "
               "us-east-1, Reserved Instances (No Upfront), Cost optimized, License Included\n- Awaiting your decision on utilization defaults "
               "(55 servers missing performance data)\n\nYour Options:\n1. Proceed with defaults (25% CPU, 60% memory) - fastest path\n"
               "2. Provide different values\n3. Download enrichment file\n\nWhat would you like to do?")
LIVE_READBACK = ("Target region: us-east-1\nPricing models: On-Demand, 1-year Reserved Instances (No Upfront), 3-year Reserved Instances (No Upfront)\n"
                 "Tenancy: Shared\nInstance selection: Cost optimized\nLicensing: License Included for Windows and RHEL; SQL Server License Included\n"
                 "Sustainability: Disabled\nExcluded instance families: t2, m4, c4, r4, i3, a1\nNetwork cost: 10% of compute")


def live_service(readback=LIVE_READBACK):
    fake = FakeTransform()
    fake.needs_poke, fake.wrap, fake.newest_first, fake.omit_ts = True, True, True, True
    fake.status_replies = [LIVE_STATUS]
    fake.readback = readback
    return fake


def test_the_live_wording_is_recognised_without_the_phrase_ready_to_proceed(tmp_path):
    """Regression for the real stall: the service said 'awaiting your confirmation to proceed ... What would you like to do?', never
    'Ready to proceed?'.  A01 now recognises that the service waits for a person, asks it to list its configuration, checks that,
    and answers with the service's own suggested 'proceed with defaults'."""
    fake = live_service()
    res = make_driver(fake).run(inputs(tmp_path))
    assert res.passes == 1 and fake.sent.count("status") == 1
    assert any(m.startswith("Before I confirm anything") for m in fake.sent)                 # it asked for the read-back
    proceed = [m for m in fake.sent if m.lower().startswith("proceed")]
    assert len(proceed) == 1 and "Proceed with defaults" in proceed[0] and "25% CPU, 60% memory" in proceed[0]
    assert fake.sent.index("status") < next(i for i, m in enumerate(fake.sent) if m.startswith("Before I confirm")) < fake.sent.index(proceed[0])


def test_a_read_back_that_differs_stops_for_a_person_and_nothing_is_confirmed(tmp_path):
    fake = live_service(LIVE_READBACK.replace("Sustainability: Disabled", "Sustainability: Enabled (PUE 1.55)"))
    with pytest.raises(NeedsHuman, match="Sustainability is enabled"):
        make_driver(fake).run(inputs(tmp_path))
    assert not any(m.lower().startswith("proceed") for m in fake.sent)
    fake2 = live_service(LIVE_READBACK.replace("Tenancy: Shared\n", ""))                     # a setting missing from the read-back
    with pytest.raises(NeedsHuman, match="Shared tenancy"):
        make_driver(fake2).run(inputs(tmp_path))


def test_a_service_that_never_lists_its_configuration_stops_with_a_clear_reason(tmp_path):
    fake = live_service()
    fake.readback = None
    fake.proposal = LIVE_STATUS                       # (only used by the read-back branch)
    orig = fake._on_message

    def deaf(text):                                   # ignores the read-back request entirely
        if not text.startswith("Before I confirm"):
            orig(text)
    fake._on_message = deaf
    with pytest.raises(NeedsHuman, match="did not list its configuration"):
        make_driver(fake).run(inputs(tmp_path))
    assert not any(m.lower().startswith("proceed") for m in fake.sent)


def test_the_drivers_own_messages_are_never_taken_for_the_services(tmp_path):
    """If the sender field cannot be read, our own 'Proceed with defaults' or config request must not look like the service waiting."""
    fake = live_service()
    fake.echo_user = True
    res = make_driver(fake).run(inputs(tmp_path))
    assert res.passes == 1 and sum(1 for m in fake.sent if m.lower().startswith("proceed")) == 1
    assert fake.sent.count("status") == 1


def test_enriched_second_pass_uses_the_enriched_proceed_text(tmp_path):
    cfg = load_config()
    est = rvtools.read(FIX / "rvtools_sample.xlsx", cfg).estate
    fake = FakeTransform()
    res = make_driver(fake).run(inputs(tmp_path, enrich=lambda tpl, dst: enrichment.fill_template(tpl, dst, est, cfg).to_dict()))
    proceeds = [m for m in fake.sent if m.lower().startswith("proceed")]
    assert res.passes == 2 and "Proceed with defaults" in proceeds[0] and "enrichment data I applied" in proceeds[1]


# ------------------------------------------------------------------------------------------------ output discovery when the listing returns folders only
def test_outputs_are_found_when_the_listing_returns_folders_as_paths_without_ids(tmp_path):
    """Seen in a live (manual) run: listing a job's artifacts returned FOLDERS only.  The files are inside, several levels down."""
    fake = live_service()
    fake.folder_mode = "strings"
    res = make_driver(fake).run(inputs(tmp_path))
    assert res.passes == 1 and round(res.assessment.model("three_year_nuri").total) == 66969


def test_folder_finder_reads_paths_and_typed_entries():
    from cva.transform import shape
    resp = {"folders": ["A/Jobs/j/Generated Outputs/", "A/Jobs/j/User Uploads/"], "items": [{"type": "folder", "name": "Sub"}, {"fileName": "x.json", "artifactId": "i"}]}
    assert set(shape.find_folders(resp)) == {"A/Jobs/j/Generated Outputs/", "A/Jobs/j/User Uploads/", "Sub/"}


# ------------------------------------------------------------------------------------------------ confirmed, but the service still waits
def test_a_confirmation_that_did_not_register_is_called_out_instead_of_waiting_silently(tmp_path):
    """Seen live: a person approved in the web application, clicked 'I confirmed it', and A01 waited for outputs while the job still said
    'Awaiting user input' (the service wants the go-ahead typed in its chat)."""
    fake = live_service()
    fake.state = "AWAITING_HUMAN_INPUT"                   # the service never received a typed go-ahead: it is still awaiting input
    with pytest.raises(NeedsHuman, match="shows 'AWAITING_HUMAN_INPUT' but no approval is open") as e:
        make_driver(fake).run(inputs(tmp_path, resume=(WS, JOB), confirmed_externally=True))
    assert "proceed with defaults" in e.value.reason and not any(m.lower().startswith("proceed") for m in fake.sent)


def test_a_running_job_is_not_mistaken_for_one_that_awaits_input(tmp_path):
    fake = live_service()
    res = make_driver(fake).run(inputs(tmp_path))         # after A01's own go-ahead the state is EXECUTING: no false alarm
    assert res.passes == 1


# ------------------------------------------------------------------------------------------------ approve IN A01; A01 relays it; nobody types in the AWS chat
def test_with_approval_required_a_match_stops_for_a_person_and_sends_nothing_yet(tmp_path):
    fake = live_service()
    with pytest.raises(NeedsHuman) as e:
        make_driver(fake).run(inputs(tmp_path, require_approval=True))
    n = e.value
    assert n.kind == "approval" and "Approval needed" in n.reason and n.guard["ok"] is True
    assert n.send_text.startswith("Proceed with defaults") and "Sustainability: Disabled" in n.proposal       # what A01 would send, and the service's own list
    assert not any(m.lower().startswith("proceed") for m in fake.sent)                                      # nothing was confirmed on the person's behalf


def test_the_approval_in_a01_is_relayed_to_aws_transform_and_the_run_completes(tmp_path):
    fake = live_service()
    with pytest.raises(NeedsHuman):
        make_driver(fake).run(inputs(tmp_path, require_approval=True))
    fake.sent.clear()
    res = make_driver(fake).run(inputs(tmp_path, require_approval=True, approve_now=True, resume=(WS, JOB)))
    assert res.passes == 1 and round(res.assessment.model("three_year_nuri").total) == 66969
    assert [m for m in fake.sent if m.lower().startswith("proceed")] and not any(m.startswith("Before I confirm") for m in fake.sent)   # relayed; nothing re-read
    assert "complete_task" not in [c[0] for c in fake.calls]


def test_a_mismatch_is_an_approval_request_too_and_the_person_can_override_it(tmp_path):
    fake = live_service(LIVE_READBACK.replace("Sustainability: Disabled", "Sustainability: Enabled (PUE 1.55)"))
    with pytest.raises(NeedsHuman) as e:
        make_driver(fake).run(inputs(tmp_path, require_approval=True))
    assert e.value.kind == "approval" and e.value.guard["ok"] is False and "Sustainability is enabled" in e.value.reason
    fake.sent.clear()
    res = make_driver(fake).run(inputs(tmp_path, approve_now=True, resume=(WS, JOB)))
    assert res.passes == 1 and any(m.lower().startswith("proceed") for m in fake.sent)


def test_automatic_mode_still_proceeds_on_a_match_without_asking(tmp_path):
    fake = live_service()
    res = make_driver(fake).run(inputs(tmp_path, require_approval=False))
    assert res.passes == 1 and sum(1 for m in fake.sent if m.lower().startswith("proceed")) == 1


def test_service_asks_then_relays_the_approval_and_imports_the_result(aws_run):
    from cva.service import ServiceError
    svc, rid, _ = aws_run
    fake = live_service()
    with pytest.raises(ServiceError, match="needs a person"):
        svc.transform_run(rid, client=fake, sleep=lambda s: None)                             # the default: ask first
    pend = svc.store.state(rid)["transform_pending"]
    assert pend["kind"] == "approval" and pend["send_text"].startswith("Proceed with defaults") and pend["jobId"] == JOB
    assert svc.overview(rid)["meta"]["figures_source"] == "none"
    out = svc.transform_run(rid, client=fake, sleep=lambda s: None, resume=(pend["workspaceId"], pend["jobId"]), approve_now=True, by="Rev")
    assert out["passes"] == 1 and svc.overview(rid)["meta"]["figures_source"] == "aws_transform"
    ev = [(e["event"], e["actor"]) for e in svc.audit_view(rid)["entries"]]
    assert ("transform_approved_by_person", "Rev") in ev                                      # the approval is attributed to the person
    assert svc.store.state(rid)["transform_pending"] is None


def test_a_message_typed_in_a01_is_relayed_unchanged_and_audited(aws_run):
    from cva.service import ServiceError
    svc, rid, _ = aws_run
    fake = live_service()
    with pytest.raises(ServiceError):
        svc.transform_run(rid, client=fake, sleep=lambda s: None)
    fake.sent.clear()
    assert svc.transform_say(rid, "  use 40% CPU and 70% memory  ", "Rev", client=fake) == {"sent": True}
    assert fake.sent == ["use 40% CPU and 70% memory"]
    assert any(e["event"] == "transform_message_sent_by_person" and e["actor"] == "Rev" for e in svc.audit_view(rid)["entries"])
    for bad in ("", "x" * 2001):
        with pytest.raises(ServiceError):
            svc.transform_say(rid, bad, "Rev", client=fake)
    with pytest.raises(ServiceError, match="your name"):
        svc.transform_say(rid, "hello", " ", client=fake)


def test_ui_asks_for_approval_and_relays_it_with_no_typing_in_the_aws_chat(ui):
    base, svc = ui
    fake = live_service()
    svc.transform_client_factory, svc.transform_sleep = (lambda: fake), (lambda s: None)
    rid = svc.create_run([str(FIX / "rvtools_sample.xlsx")], as_of="2026-10-20")
    assert _http(f"{base}/api/run/{rid}/transform_start", {"by": "Rev"})[1]["started"] is True               # default: approval required
    v = _wait_job(base, rid, {"needs_person", "error", "done"})
    assert v["job"]["status"] == "needs_person" and v["pending"]["kind"] == "approval" and not v["imported"]
    t = _job_started(base, rid)
    _http(f"{base}/api/run/{rid}/transform_resume", {"by": "Rev", "approve": True})                          # one click in A01
    v = _wait_job(base, rid, {"done", "error"}, after=t)
    assert v["job"]["status"] == "done" and v["imported"] and v["pending"] is None
    assert _http(f"{base}/api/run/{rid}/transform_say", {"by": "Rev", "text": "hi"})[0] == 400              # nothing paused any more


def test_the_page_offers_a_plain_approve_button_and_hides_the_externally_confirmed_option():
    html = (Path(__file__).resolve().parent.parent / "cva" / "ui" / "static" / "index.html").read_text()
    assert "Approve and start the assessment" in html and "approve: true" in html and "Suggest a change or ask AWS Transform a question" in html and "transform_suggest" in html
    i = html.index("I already typed the go-ahead")
    adv = html.rfind('el("details", {}, el("summary", {}, "Advanced")', 0, i)
    assert adv > html.rfind("Approve and start the assessment", 0, i) and adv != -1                                       # the old option sits inside an Advanced section


# ------------------------------------------------------------------------------------------------ the loop: approve, or suggest; AWS Transform answers; approve again
SUGGESTION = "Use 40% CPU and 70% memory for all servers"


def chatty_service():
    fake = live_service()
    fake.chatty = True
    return fake


def _asks_for_the_listing(fake, tmp_path, **kw):
    """The person asks to see the exact configuration (the only thing that makes A01 send its read-back after a suggestion)."""
    return make_driver(fake).run(inputs(tmp_path, require_approval=True, show_listing=True, resume=(WS, JOB), **kw))


def test_after_a_suggestion_a01_says_nothing_more_until_the_person_acts(tmp_path):
    """Seen live (run-0003, the AWS Transform chat): the person replied; the service answered and ASKED 'anything else you'd like me to adjust, or are you ready to proceed?';
    A01 immediately posted its own read-back request instead of letting the person answer.  Now A01 relays the person's words, shows the answer, and waits."""
    fake = chatty_service()
    with pytest.raises(NeedsHuman) as first:
        make_driver(fake).run(inputs(tmp_path, require_approval=True))
    assert first.value.kind == "approval" and not first.value.needs_listing                   # the first decision: A01 gathers the listing before it asks the person
    fake.sent.clear()
    with pytest.raises(NeedsHuman) as e:                                                       # the person types a reply in A01
        make_driver(fake).run(inputs(tmp_path, require_approval=True, resume=(WS, JOB), reply_text=SUGGESTION, reply_stage="scenario"))
    n = e.value
    assert fake.sent == [SUGGESTION]                                                           # relayed UNCHANGED, and NOTHING else was sent
    assert n.kind == "approval" and n.needs_listing and n.customised and not n.proposal and "Updated as requested" in n.reply and "How would you like to proceed?" in n.reply
    assert n.send_text.startswith("Proceed with exactly the configuration") and "defaults" not in n.send_text.lower()
    with pytest.raises(NeedsHuman) as e2:                                                      # a second reply: relayed, and still nothing else
        make_driver(fake).run(inputs(tmp_path, require_approval=True, resume=(WS, JOB), reply_text="Also use 80% memory for databases", customised=True))
    assert fake.sent == [SUGGESTION, "Also use 80% memory for databases"] and e2.value.needs_listing
    assert not any(m.startswith("Before I confirm") or m.lower().startswith("proceed") for m in fake.sent)      # no read-back, no go-ahead: the person has not asked


def test_the_configuration_is_requested_only_when_the_person_asks_and_then_put_to_them(tmp_path):
    fake = chatty_service()
    with pytest.raises(NeedsHuman):
        make_driver(fake).run(inputs(tmp_path, require_approval=True))
    with pytest.raises(NeedsHuman):
        make_driver(fake).run(inputs(tmp_path, require_approval=True, resume=(WS, JOB), reply_text=SUGGESTION, reply_stage="scenario"))
    fake.sent.clear()
    with pytest.raises(NeedsHuman) as e:
        _asks_for_the_listing(fake, tmp_path, customised=True)
    n = e.value
    assert [m for m in fake.sent if m.startswith("Before I confirm anything")] and len(fake.sent) == 1          # exactly one message: the read-back
    assert n.kind == "approval" and not n.needs_listing and n.customised and n.guard["ok"] is True
    assert "Utilization assumptions: " + SUGGESTION in n.proposal and n.send_text.startswith("Proceed with exactly the configuration")
    res = make_driver(fake).run(inputs(tmp_path, approve_now=True, customised=True, resume=(WS, JOB)))        # and now one approval sends the go-ahead
    proceeds = [m for m in fake.sent if m.lower().startswith("proceed")]
    assert res.passes == 1 and len(proceeds) == 1 and proceeds[0].startswith("Proceed with exactly the configuration")


def test_a_suggestion_that_changes_a_requested_setting_is_flagged_but_the_person_decides(tmp_path):
    fake = chatty_service()
    with pytest.raises(NeedsHuman):
        make_driver(fake).run(inputs(tmp_path, require_approval=True))
    with pytest.raises(NeedsHuman):                                                              # the reply: relayed, answered, nothing else sent
        make_driver(fake).run(inputs(tmp_path, require_approval=True, resume=(WS, JOB), reply_text="Drop the 3-year pricing", reply_stage="scenario"))
    fake.readback = LIVE_READBACK.replace("3-year Reserved Instances (No Upfront)", "")          # the service now lists only On-Demand and 1-year
    with pytest.raises(NeedsHuman) as e:
        _asks_for_the_listing(fake, tmp_path, customised=True)                                   # the person asks: the difference is flagged
    assert e.value.kind == "approval" and e.value.guard["ok"] is False and "3-year pricing" in e.value.reason
    res = make_driver(fake).run(inputs(tmp_path, approve_now=True, customised=True, resume=(WS, JOB)))      # the person accepts the difference
    assert res.passes == 1


def test_a_reply_after_the_go_ahead_continues_the_wait_instead_of_asking_again(tmp_path):
    """The service asks one more thing AFTER the go-ahead.  The person answers in A01; A01 relays it and goes on waiting for the outputs."""
    fake = chatty_service()
    fake.await_after_proceed = True
    with pytest.raises(NeedsHuman) as e:
        make_driver(fake).run(inputs(tmp_path, require_approval=False))
    assert e.value.stage == "running" and "one more confirmation" in e.value.proposal
    fake.sent.clear()
    res = make_driver(fake).run(inputs(tmp_path, resume=(WS, JOB), reply_text="yes, include storage", reply_stage="running"))
    assert res.passes == 1 and fake.sent == ["yes, include storage"]                         # relayed; no re-read, no new approval request


def test_the_service_answers_loop_through_the_ui_with_no_typing_in_the_aws_chat(ui):
    base, svc = ui
    fake = chatty_service()
    svc.transform_client_factory, svc.transform_sleep = (lambda: fake), (lambda s: None)
    rid = svc.create_run([str(FIX / "rvtools_sample.xlsx")], as_of="2026-10-20")
    assert _http(f"{base}/api/run/{rid}/transform_suggest", {"by": "Rev", "text": "x"})[0] == 400                  # nothing paused yet
    _http(f"{base}/api/run/{rid}/transform_start", {"by": "Rev"})
    v = _wait_job(base, rid, {"needs_person", "error", "done"})
    assert v["pending"]["kind"] == "approval" and not v["pending"]["customised"]
    assert _http(f"{base}/api/run/{rid}/transform_suggest", {"by": "", "text": SUGGESTION})[0] == 400              # a name is required
    assert _http(f"{base}/api/run/{rid}/transform_suggest", {"by": "Rev", "text": "  "})[0] == 400
    assert _http(f"{base}/api/run/{rid}/transform_suggest", {"by": "Rev", "text": "x" * 2001})[0] == 400
    t = _job_started(base, rid)
    assert _http(f"{base}/api/run/{rid}/transform_suggest", {"by": "Rev", "text": SUGGESTION})[0] == 200
    v = _wait_job(base, rid, {"needs_person", "error", "done"}, after=t)
    assert v["job"]["status"] == "needs_person" and v["pending"]["kind"] == "approval" and v["pending"]["customised"] and v["pending"]["needs_listing"]
    assert "Updated as requested" in v["pending"]["reply"] and not v["pending"]["proposal"] and not v["imported"]
    assert not [m for m in fake.sent if m.startswith("Before I confirm")][1:]                    # A01 sent nothing of its own after the person's message
    t = _job_started(base, rid)                                                                   # the person asks to see what will run
    assert _http(f"{base}/api/run/{rid}/transform_listing", {"by": ""})[0] == 400                 # a name is required
    assert _http(f"{base}/api/run/{rid}/transform_listing", {"by": "Rev"})[0] == 200
    v = _wait_job(base, rid, {"needs_person", "error", "done"}, after=t)
    assert v["job"]["status"] == "needs_person" and v["pending"]["kind"] == "approval" and not v["pending"]["needs_listing"] and SUGGESTION in v["pending"]["proposal"]
    t = _job_started(base, rid)
    _http(f"{base}/api/run/{rid}/transform_resume", {"by": "Rev", "approve": True})
    v = _wait_job(base, rid, {"done", "error"}, after=t)
    assert v["job"]["status"] == "done" and v["imported"]
    assert SUGGESTION in fake.sent and [m for m in fake.sent if m.startswith("Proceed with exactly the configuration")]
    ev = [(e["event"], e["actor"]) for e in svc.audit_view(rid)["entries"]]
    assert ("transform_suggestion_sent", "Rev") in ev and ("transform_listing_requested", "Rev") in ev and ("transform_approved_by_person", "Rev") in ev
    assert svc.audit_view(rid)["verify"]["ok"]


# ------------------------------------------------------------------------------------------------ seen live: a suggestion never reached the AWS chat
def test_a_resumed_session_loads_the_jobs_instructions_so_a_suggestion_is_delivered(tmp_path):
    """Live: the first session sent messages fine; the approval and the suggestion run in a NEW session, which skipped load_instructions, so
    the service refused them (INSTRUCTIONS_REQUIRED) and nothing reached the chat while A01 waited for an answer."""
    fake = chatty_service()
    fake.require_instructions = True
    with pytest.raises(NeedsHuman):
        make_driver(fake).run(inputs(tmp_path, require_approval=True))
    fake.new_session()                                                                       # the person acts: a new session with the server
    fake.sent.clear()
    with pytest.raises(NeedsHuman) as e:
        make_driver(fake).run(inputs(tmp_path, require_approval=True, resume=(WS, JOB), reply_text=SUGGESTION, reply_stage="scenario"))
    assert fake.rejected == [] and fake.sent[0] == SUGGESTION and "Updated as requested" in e.value.reply
    fake.new_session()
    fake.sent.clear()
    res = make_driver(fake).run(inputs(tmp_path, approve_now=True, customised=True, resume=(WS, JOB)))      # and the approval is delivered too
    assert res.passes == 1 and fake.rejected == [] and [m for m in fake.sent if m.lower().startswith("proceed")]


def test_an_instructions_required_answer_is_handled_by_loading_and_retrying_once(tmp_path):
    fake = FakeTransform()
    fake.require_instructions = True
    drv = make_driver(fake)
    drv._call("send_message", workspaceId=WS, jobId=JOB, text="hello")
    assert fake.rejected == ["send_message"] and fake.sent == ["hello"]                      # refused once, instructions loaded, delivered
    assert [c[0] for c in fake.calls].count("load_instructions") == 1


def test_a_message_the_service_refuses_is_never_treated_as_sent(tmp_path):
    fake = FakeTransform()
    fake.refuse_send = True
    with pytest.raises(NeedsHuman, match="did not accept 'send_message'") as e:
        make_driver(fake)._call("send_message", workspaceId=WS, jobId=JOB, text="hello")
    assert "not accepting messages" in e.value.reason and fake.sent == []


def test_a_relayed_message_from_the_service_layer_also_loads_instructions_first(aws_run):
    from cva.service import ServiceError
    svc, rid, _ = aws_run
    fake = chatty_service()
    fake.require_instructions = True
    with pytest.raises(ServiceError):
        svc.transform_run(rid, client=fake, sleep=lambda s: None)
    fake.new_session()
    fake.sent.clear()
    svc.transform_say(rid, "hello", "Rev", client=fake)
    assert fake.rejected == [] and fake.sent == ["hello"]


# ------------------------------------------------------------------------------------------------ pre-flight, diagnostics, and the full live replica
class PreflightFake(FakeTransform):
    tools = set(ALLOWED_TOOLS)

    def tool_names(self):
        return self.tools


def test_preflight_passes_on_a_healthy_connection_and_changes_nothing(svc, monkeypatch):
    monkeypatch.setenv("AWS_PROFILE", "p")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    fake = PreflightFake()
    r = svc.transform_preflight(client=fake)
    assert r["ok"] and all(c["ok"] for c in r["checks"])
    assert {c["name"] for c in r["checks"]} >= {"AWS profile and region are set", "The tools A01 needs exist", "Connected to AWS Transform", "A read-only listing works"}
    assert {c[0] for c in fake.calls} <= {"get_status", "list_resources"}                      # nothing was created, uploaded or sent


def test_preflight_names_each_thing_that_is_wrong(svc, monkeypatch):
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)

    class Broken(PreflightFake):
        tools = set(ALLOWED_TOOLS) - {"send_message"}

        def _handle(self, name, args):
            return {"status": "not configured"} if name == "get_status" else super()._handle(name, args)
    r = svc.transform_preflight(client=Broken())
    bad = {c["name"]: c["detail"] for c in r["checks"] if not c["ok"]}
    assert not r["ok"] and "NOT SET" in bad["AWS profile and region are set"]
    assert "send_message" in bad["The tools A01 needs exist"] and "NOT_CONFIGURED" in bad["Connected to AWS Transform"]


def test_diagnostics_bundle_collects_what_is_needed_to_explain_a_problem(ui):
    import zipfile
    base, svc = ui
    fake = live_service()
    svc.transform_client_factory, svc.transform_sleep = (lambda: fake), (lambda s: None)
    rid = svc.create_run([str(FIX / "rvtools_sample.xlsx")], as_of="2026-10-20")
    _http(f"{base}/api/run/{rid}/transform_start", {"by": "Rev"})
    _wait_job(base, rid, {"needs_person", "error", "done"})
    assert _http(f"{base}/api/run/{rid}/transform_diagnostics", {"by": "Rev"})[0] == 200
    import urllib.request
    with urllib.request.urlopen(f"{base}/download/{rid}/transform/diagnostics.zip") as r:
        data = r.read()
    p = Path(svc.store.path(rid)) / "out" / "transform" / "diagnostics.zip"
    assert p.read_bytes() == data
    with zipfile.ZipFile(p) as z:
        names = set(z.namelist())
        assert {"trace_tail.jsonl", "mcp_server_tail.log", "audit_transform_events.json", "state_excerpt.json", "versions.txt", "config_transform.toml"} <= names
        assert json.loads(z.read("state_excerpt.json"))["transform_pending"]["kind"] == "approval"
        assert "A01 0." in z.read("versions.txt").decode() and "python" in z.read("versions.txt").decode()
        assert any(json.loads(line) for line in z.read("trace_tail.jsonl").decode().splitlines() if line.strip())


def test_the_page_offers_the_preflight_and_the_diagnostics_download():
    html = (Path(__file__).resolve().parent.parent / "cva" / "ui" / "static" / "index.html").read_text()
    assert "Check the connection first" in html and "/api/transform_preflight" in html and "Download diagnostics" in html


def test_live_replica_the_whole_journey_with_every_observed_behaviour_at_once(ui):
    """Everything seen live, together: answers wrapped as JSON strings; newest-first listings without timestamps or readable senders (our own
    messages echoed back); a notice that waits for a nudge, answered with a status report in the live wording; the job's instructions needed
    afresh in EVERY session; folders-only artifact listings that accept only relative prefixes; a suggestion loop; and approval relayed from A01."""
    base, svc = ui
    fake = chatty_service()
    fake.folder_mode, fake.echo_user, fake.require_instructions = "strings", True, True
    svc.transform_client_factory = lambda: (fake.new_session(), fake)[1]                  # every action is a NEW session with the server
    svc.transform_sleep = lambda s: None
    rid = svc.create_run([str(FIX / "rvtools_sample.xlsx")], as_of="2026-10-20")

    _http(f"{base}/api/run/{rid}/transform_start", {"by": "Rev"})                         # 1. start: it must ask for approval, not run
    v = _wait_job(base, rid, {"needs_person", "error", "done"})
    assert v["job"]["status"] == "needs_person" and v["pending"]["kind"] == "approval" and "Utilization" not in v["pending"]["proposal"]
    assert not any(m.lower().startswith("proceed") for m in fake.sent)

    t = _job_started(base, rid)                                                           # 2. a suggestion, in a fresh session
    assert _http(f"{base}/api/run/{rid}/transform_suggest", {"by": "Rev", "text": SUGGESTION})[0] == 200
    v = _wait_job(base, rid, {"needs_person", "error", "done"}, after=t)
    assert v["job"]["status"] == "needs_person" and v["pending"]["customised"] and v["pending"]["needs_listing"] and "Updated as requested" in v["pending"]["reply"]
    t = _job_started(base, rid)                                                           # 2b. the person asks to see what will run (the only thing that makes A01 post its read-back)
    assert _http(f"{base}/api/run/{rid}/transform_listing", {"by": "Rev"})[0] == 200
    v = _wait_job(base, rid, {"needs_person", "error", "done"}, after=t)
    assert v["job"]["status"] == "needs_person" and v["pending"]["customised"] and SUGGESTION in v["pending"]["proposal"]

    t = _job_started(base, rid)                                                           # 3. approval, in another fresh session
    _http(f"{base}/api/run/{rid}/transform_resume", {"by": "Rev", "approve": True})
    v = _wait_job(base, rid, {"done", "error"}, after=t)
    assert v["job"]["status"] == "done", v["job"]["message"]
    assert v["imported"] and v["pending"] is None and round(v["assessment"]["models"][0]["total"]) == 122339

    assert fake.rejected == []                                                            # no message was ever refused or lost
    called = {c[0] for c in fake.calls}
    assert called <= ALLOWED_TOOLS and not (called & {"complete_task", "control_job", "delete_job", "delete_workspace", "configure"})
    texts = [m for m in fake.sent if not m.startswith("I've uploaded")]
    assert texts[0] == "status" and SUGGESTION in texts and texts[-1].startswith("Proceed with exactly the configuration")
    assert sum(1 for m in fake.sent if m.lower().startswith("proceed")) == 1               # exactly one go-ahead, sent after the person approved
    ev = [(e["event"], e["actor"]) for e in svc.audit_view(rid)["entries"]]
    assert ("transform_suggestion_sent", "Rev") in ev and ("transform_approved_by_person", "Rev") in ev and svc.audit_view(rid)["verify"]["ok"]
    assert svc.overview(rid)["meta"]["figures_source"] == "aws_transform"


# ------------------------------------------------------------------------------------------------ seen live: reports ready, but the page waits
def test_outputs_are_found_when_only_the_full_path_prefix_form_is_accepted(tmp_path):
    fake = live_service()
    fake.folder_mode, fake.full_only = "strings", True
    res = make_driver(fake).run(inputs(tmp_path))
    assert res.passes == 1 and round(res.assessment.model("three_year_nuri").total) == 66969


def test_files_the_service_names_in_its_own_message_are_found_even_if_no_listing_works(tmp_path):
    """Seen live: the service's message 'Reports ready for scenario ...' carries the artifact ids of its deliverables."""
    fake = live_service()
    fake.folder_mode, fake.artifacts_in_messages = "empty", "all"
    res = make_driver(fake).run(inputs(tmp_path))
    assert res.passes == 1 and res.assessment.warning_rows == 251


def test_reports_visible_but_not_the_numbers_file_stops_with_the_fallback_instead_of_waiting(tmp_path):
    for mode in ("listing", "message"):
        fake = live_service()
        if mode == "listing":
            fake.folder_mode = "partial"                                         # the 'assessment outputs' folder cannot be opened
        else:
            fake.folder_mode, fake.artifacts_in_messages = "empty", "no_json"    # the message names only the zip and the csv
        with pytest.raises(NeedsHuman) as e:
            make_driver(fake).run(inputs(tmp_path / mode if (tmp_path / mode).mkdir() is None else tmp_path))
        n = e.value
        assert "cannot find aggregated_output.json" in n.reason and "error_file.csv" in n.reason and "Choose File" in n.reason, mode
        assert n.stage == "running" and n.workspace_id == WS and n.job_id == JOB


def test_several_files_can_be_imported_at_once_through_the_page_api(ui):
    base, svc = ui
    rid = svc.create_run([str(FIX / "rvtools_sample.xlsx")], as_of="2026-10-20")
    body = {"by": "Rev", "files": [_b64(FIX / "aggregated_output.json"), _b64(FIX / "error_file.csv")]}
    assert _http(f"{base}/api/run/{rid}/import_results", body)[0] == 200
    ov = svc.overview(rid)
    assert ov["meta"]["figures_source"] == "aws_transform" and ov["exceptions"]["by_code"].get("TRW-UTIL") == 1       # the warnings file came along
    assert _http(f"{base}/api/run/{rid}/import_results", {"by": "Rev", "files": [_b64(FIX / "error_file.csv")]})[0] == 400      # no numbers file: refused


def test_the_page_lets_you_pick_several_files_to_import():
    html = (Path(__file__).resolve().parent.parent / "cva" / "ui" / "static" / "index.html").read_text()
    i = html.index('"aria-label": "AWS Transform assessment"')
    assert "multiple: true" in html[i - 120:i] and "Import file(s)" in html


# ------------------------------------------------------------------------------------------------ seen live: 13 minutes of "0 new file(s)" after the reports were ready
def test_listings_are_read_whatever_the_key_names_are():
    from cva.transform import shape
    resp = {"items": [{"artifactID": "a1", "artifactName": "report.pdf"}, {"id": "a2", "label": "x", "value": "aggregated_output.json"},
                      {"assetId": "a3", "filename": "error_file.csv"}, {"id": "no-name-here"}]}
    got = {a["artifactId"]: a["name"] for a in shape.find_artifacts(resp)}
    assert got == {"a1": "report.pdf", "a2": "aggregated_output.json", "a3": "error_file.csv"}          # an id without any name or file-like value is ignored
    folders = shape.find_folders({"entries": [{"isFolder": True, "name": "Generated Outputs"}, {"type": "FOLDER", "path": "A/B"}, {"name": "x.json"}]})
    assert set(folders) == {"Generated Outputs/", "A/B/"}


def test_when_the_service_announces_reports_that_cannot_be_listed_a01_stops_and_shows_what_it_saw(tmp_path):
    fake = live_service()
    fake.folder_mode, fake.announce_reports = "empty", True                       # the live symptom: reports announced, zero files visible
    with pytest.raises(NeedsHuman) as e:
        make_driver(fake).run(inputs(tmp_path))
    r = e.value.reason
    assert "says the reports are ready" in r and "cannot list any output file" in r and "What each listing returned" in r
    assert "0 file(s), 0 folder(s)" in r and "Choose File" in r and e.value.stage == "running"


def test_the_wait_log_names_every_listing_it_tried(tmp_path):
    fake = live_service()
    fake.folder_mode = "strings"
    drv = make_driver(fake)
    drv.run(inputs(tmp_path))
    assert any("Generated Outputs/" in x for x in drv._listing_report) or drv._folders                  # the crawl is recorded
    fake2 = live_service()
    fake2.folder_mode, fake2.announce_reports = "empty", True
    drv2 = make_driver(fake2)
    with pytest.raises(NeedsHuman):
        drv2.run(inputs(tmp_path / "x" if (tmp_path / "x").mkdir() is None else tmp_path))
    assert "listing:" in drv2._diag and "0 file(s), 0 folder(s)" in drv2._diag


def test_the_trace_and_diagnostics_keep_the_real_shapes_for_each_listing(aws_run):
    import zipfile
    svc, rid, _ = aws_run
    fake = live_service()
    fake.folder_mode = "strings"
    svc.transform_run(rid, client=fake, sleep=lambda s: None, require_approval=False)
    trace = [json.loads(x) for x in (svc.store.path(rid) / "out" / "transform_job" / "transform_trace.jsonl").read_text().splitlines() if x.strip()]
    prefixes = {r["args"].get("pathPrefix") for r in trace if r["tool"] == "list_resources" and r["args"].get("resource") == "artifacts"}
    assert any(p and "Generated Outputs/" in p for p in prefixes) and "text" not in {k for r in trace for k in r.get("args", {})}       # prefixes are kept; message text is not
    svc.transform_diagnostics(rid)
    with zipfile.ZipFile(svc.store.path(rid) / "out" / "transform" / "diagnostics.zip") as z:
        rows = [json.loads(x) for x in z.read("listing_samples.jsonl").decode().splitlines() if x.strip()]
    assert any(r["args"].get("resource") == "artifacts" and r["args"].get("pathPrefix") for r in rows)
    assert len(rows) < len(trace)                                                                  # one sample per distinct listing, not the whole trace


# ------------------------------------------------------------------------------------------------ the artifacts panel, and fetching the results on demand
def tree_service():
    fake = live_service()
    fake.folder_mode, fake.extra_files = "strings", True
    return fake


def test_every_page_of_a_listing_is_read(tmp_path):
    fake = live_service()
    fake.folder_mode = "paged"
    res = make_driver(fake).run(inputs(tmp_path))
    assert res.passes == 1 and any(c[1].get("nextToken") == "t2" for c in fake.calls if c[0] == "list_resources")


def test_the_job_listing_keeps_each_files_folder(tmp_path):
    fake = tree_service()
    drv = make_driver(fake)
    drv.run(inputs(tmp_path))
    items, _ = drv.list_job_artifacts(WS, JOB)
    by = {i["name"]: i["folder"] for i in items}
    assert by["aggregated_output.json"] == "Generated Outputs/Initial Assessment/assessment outputs/"
    assert by["error_file.csv"] == "Generated Outputs/Initial Assessment/" and by["inventory-review.json"] == "Generated Outputs/"
    assert by["rvtools_sample.xlsx"] == "User Uploads/"


def _finished_run(svc, rid, fake):
    svc.transform_client_factory, svc.transform_sleep = (lambda: fake), (lambda s: None)
    svc.transform_run(rid, client=fake, sleep=lambda s: None, require_approval=False)


def test_the_artifacts_panel_lists_selects_and_downloads_chosen_files_or_everything(aws_run):
    import zipfile
    svc, rid, _ = aws_run
    fake = tree_service()
    _finished_run(svc, rid, fake)
    listing = svc.transform_artifacts_list(rid)
    names = {i["name"]: i for i in listing["items"]}
    assert set(names) == {"aggregated_output.json", "error_file.csv", "Initial_Assessment_x.zip", "inventory-review.json", "rvtools_sample.xlsx"}
    assert listing["job"] == {"workspaceId": WS, "jobId": JOB} and not any(i["downloaded"] for i in listing["items"])
    one = svc.transform_artifacts_fetch(rid, [names["aggregated_output.json"]["artifactId"]], False, "Rev")      # one file: no zip
    base = svc.store.path(rid) / "out" / "transform_artifacts" / "files"
    assert one["zip"] is None and (base / "Generated Outputs/Initial Assessment/assessment outputs/aggregated_output.json").is_file()
    two = svc.transform_artifacts_fetch(rid, [names["error_file.csv"]["artifactId"], names["inventory-review.json"]["artifactId"]], False, "Rev")
    assert two["zip"] == "artifacts_selected.zip"
    allr = svc.transform_artifacts_fetch(rid, None, True, "Rev")                                                 # everything: one zip, folders kept
    assert allr["zip"] == "artifacts_all.zip" and len(allr["files"]) == 5
    with zipfile.ZipFile(svc.store.path(rid) / "out" / "transform" / "artifacts_all.zip") as z:
        assert "Generated Outputs/Initial Assessment/assessment outputs/aggregated_output.json" in z.namelist() and "User Uploads/rvtools_sample.xlsx" in z.namelist()
    assert all(i["downloaded"] for i in svc.transform_artifacts_list(rid)["items"])                             # the list now says what is in A01
    assert [e for e in svc.audit_view(rid)["entries"] if e["event"] == "transform_artifacts_downloaded" and e["actor"] == "Rev"]
    from cva.service import ServiceError
    with pytest.raises(ServiceError, match="at least one"):
        svc.transform_artifacts_fetch(rid, ["nope"], False, "Rev")


def test_an_unsafe_artifact_name_can_never_write_outside_the_run(aws_run):
    svc, rid, _ = aws_run
    fake = tree_service()
    _finished_run(svc, rid, fake)
    st = svc.store.state(rid)
    st["transform_artifact_listing"] = {"evil-0001-aaaa": {"artifactId": "evil-0001-aaaa", "name": "../../evil.json", "folder": "../x/../../y/", "size": 1}}
    svc.store.save_state(rid, st)
    out = svc.transform_artifacts_fetch(rid, ["evil-0001-aaaa"], False, "Rev")
    p = (svc.store.path(rid) / "out" / "transform_artifacts" / "files" / out["files"][0]["path"]).resolve()
    assert (svc.store.path(rid) / "out" / "transform_artifacts" / "files").resolve() in p.parents and ".." not in out["files"][0]["path"].split("/")


def test_the_panel_works_even_for_a_run_that_is_still_waiting(aws_run):
    """The job is remembered the moment it exists, so its files can be listed while the run is paused or stuck."""
    from cva.service import ServiceError
    svc, rid, _ = aws_run
    fake = tree_service()
    svc.transform_client_factory, svc.transform_sleep = (lambda: fake), (lambda s: None)
    with pytest.raises(ServiceError, match="needs a person"):
        svc.transform_run(rid, client=fake, sleep=lambda s: None)                      # stops at the approval request
    assert svc.store.state(rid)["transform_job"] == {"workspaceId": WS, "jobId": JOB}
    assert {i["name"] for i in svc.transform_artifacts_list(rid)["items"]} >= {"rvtools_sample.xlsx"}


def test_fetching_the_results_now_fills_the_other_tabs_without_any_manual_download(aws_run):
    from cva.service import ServiceError
    svc, rid, _ = aws_run
    fake = tree_service()
    fake.folder_mode = "partial"                                                       # the live symptom: the numbers file cannot be listed
    svc.transform_client_factory, svc.transform_sleep = (lambda: fake), (lambda s: None)
    ticks = iter(range(0, 10**6, 50))                                                  # a test clock: 50 s per look, so the 90 s rule fires at once
    with pytest.raises(ServiceError, match="needs a person"):
        svc.transform_run(rid, client=fake, sleep=lambda s: None, require_approval=False, clock=lambda: next(ticks))
    assert svc.overview(rid)["meta"]["figures_source"] == "none"
    with pytest.raises(ServiceError, match="What each listing returned"):               # still invisible: it says why, and imports nothing
        svc.transform_fetch_results(rid, "Rev")
    fake.folder_mode = "strings"                                                        # the listing works now (or the fix is in place)
    out = svc.transform_fetch_results(rid, "Rev")
    ov = svc.overview(rid)
    assert ov["meta"]["figures_source"] == "aws_transform" and round(ov["scenarios"][0]["total"]) == 122339 and out
    assert [e for e in svc.audit_view(rid)["entries"] if e["event"] == "transform_results_fetched" and e["actor"] == "Rev"]
    with pytest.raises(ServiceError, match="your name"):
        svc.transform_fetch_results(rid, " ")


def test_the_artifacts_panel_through_the_page_api_and_the_download_route(ui):
    import urllib.error
    import urllib.request
    base, svc = ui
    fake = tree_service()
    svc.transform_client_factory, svc.transform_sleep = (lambda: (fake.new_session(), fake)[1]), (lambda s: None)
    rid = svc.create_run([str(FIX / "rvtools_sample.xlsx")], as_of="2026-10-20")
    assert _http(f"{base}/api/run/{rid}/transform_artifacts_list", {})[0] == 400                   # no job yet
    _http(f"{base}/api/run/{rid}/transform_start", {"by": "Rev"})
    _wait_job(base, rid, {"needs_person", "error", "done"})
    t = _job_started(base, rid)
    _http(f"{base}/api/run/{rid}/transform_resume", {"by": "Rev", "approve": True})
    assert _wait_job(base, rid, {"done", "error"}, after=t)["job"]["status"] == "done"
    code, listing = _http(f"{base}/api/run/{rid}/transform_artifacts_list", {})
    assert code == 200 and len(listing["items"]) == 5
    ids = {i["name"]: i["artifactId"] for i in listing["items"]}
    assert _http(f"{base}/api/run/{rid}/transform_artifacts_fetch", {"ids": []})[0] == 400
    code, got = _http(f"{base}/api/run/{rid}/transform_artifacts_fetch", {"by": "Rev", "ids": [ids["aggregated_output.json"]]})
    assert code == 200 and got["zip"] is None
    with urllib.request.urlopen(f"{base}/download/{rid}/artifact/{ids['aggregated_output.json']}") as r:
        assert r.read() == (FIX / "aggregated_output.json").read_bytes() and "aggregated_output.json" in r.headers["Content-Disposition"]
    code, got = _http(f"{base}/api/run/{rid}/transform_artifacts_fetch", {"by": "Rev", "all": True})
    assert code == 200 and got["zip"] == "artifacts_all.zip"
    with urllib.request.urlopen(f"{base}/download/{rid}/transform/artifacts_all.zip") as r:
        assert r.read(2) == b"PK"
    for bad in ("unknown-0001-zzzz", "..%2f..%2fstate", "x"):
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(f"{base}/download/{rid}/artifact/{bad}")
        assert e.value.code == 404


def test_the_page_has_the_artifacts_panel_and_the_fetch_button():
    html = (Path(__file__).resolve().parent.parent / "cva" / "ui" / "static" / "index.html").read_text()
    assert "const ART = " in html                                                         # the list and the ticks survive the page's automatic re-renders
    for s in ("AWS Transform artifacts", "Download selected", "Download all (one zip)", "Select all", "Fetch the results now and fill the other tabs",
              "/transform_artifacts_list", "/transform_artifacts_fetch", "/transform_fetch_results"):
        assert s in html, s


def test_a_run_from_an_older_version_gets_its_job_back_from_the_saved_trace(aws_run):
    """Older runs did not store the job; the ids are in the trace (answers are JSON inside JSON strings)."""
    svc, rid, _ = aws_run
    tdir = svc.store.path(rid) / "out" / "transform_job"
    tdir.mkdir(parents=True, exist_ok=True)
    ws, job = "03282506-e0a1-7099-1111-aaaaaaaaaaaa", "4c087a9e-0000-4000-8000-bbbbbbbbbbbb"
    lines = [json.dumps({"tool": "create_workspace", "args_keys": ["name"], "response": json.dumps({"success": True, "data": {"workspaceId": ws}})}),
             json.dumps({"tool": "create_job", "args_keys": ["workspaceId"], "response": json.dumps({"success": True, "data": {"jobId": job, "workspaceId": ws}})}),
             json.dumps({"tool": "list_resources", "args_keys": ["jobId"], "response": "{}"})]
    (tdir / "transform_trace.jsonl").write_text("\n".join(lines) + "\n")
    assert svc._job_ids(rid) == (ws, job)


def test_the_listing_reads_a_files_size_whatever_the_key_is():
    from cva.transform import shape
    got = shape.find_artifacts({"items": [{"artifactId": "a1", "fileName": "x.json", "sizeBytes": 36100}, {"id": "a2", "name": "y.csv", "contentLength": 12}]})
    assert [a["size"] for a in got] == [36100, 12]


# ------------------------------------------------------------------------------------------------ seen live: "NOT_CONFIGURED: Not connected to AWS Transform"
@pytest.mark.parametrize("mode", ["all", "workspace"])
def test_not_connected_stops_at_once_with_the_real_reason_and_creates_nothing(tmp_path, mode):
    fake = live_service()
    fake.not_connected = mode              # "all": even get_status says so; "workspace": the status looks fine and create_workspace refuses (the live case)
    with pytest.raises(NeedsHuman) as e:
        make_driver(fake).run(inputs(tmp_path))
    n = e.value
    assert n.kind == "connection" and "NOT_CONFIGURED" in n.reason and "Configure AWS Transform" in n.reason and "never signs in for you" in n.reason
    assert "Could not read the workspace id" not in n.reason                                   # the misleading message of the live run
    assert {c[0] for c in fake.calls} <= {"get_status", "create_workspace"}                    # nothing was created, uploaded or sent


def test_the_run_through_the_service_and_page_shows_a_connection_problem_not_a_workspace_problem(ui):
    base, svc = ui
    fake = live_service()
    fake.not_connected = "workspace"
    svc.transform_client_factory, svc.transform_sleep = (lambda: fake), (lambda s: None)
    rid = svc.create_run([str(FIX / "rvtools_sample.xlsx")], as_of="2026-10-20")
    _http(f"{base}/api/run/{rid}/transform_start", {"by": "Rev"})
    v = _wait_job(base, rid, {"needs_person", "error", "done"})
    assert v["job"]["status"] == "needs_person" and v["pending"]["kind"] == "connection" and not v["pending"]["workspaceId"]
    assert "NOT_CONFIGURED" in v["pending"]["reason"]


def test_the_preflight_catches_a_server_that_is_not_signed_in(svc, monkeypatch):
    monkeypatch.setenv("AWS_PROFILE", "p")
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    class NotSignedIn(PreflightFake):
        def _handle(self, name, args):
            self.calls.append((name, args))
            return self.NOT_CONFIGURED                       # the answer seen live, whatever is asked
    r = svc.transform_preflight(client=NotSignedIn())
    bad = {c["name"]: c["detail"] for c in r["checks"] if not c["ok"]}
    assert not r["ok"] and set(bad) == {"Connected to AWS Transform", "A read-only listing works"}
    assert "Configure AWS Transform" in bad["Connected to AWS Transform"]


def test_the_page_explains_a_lapsed_connection_and_lets_you_recheck_it():
    html = (Path(__file__).resolve().parent.parent / "cva" / "ui" / "static" / "index.html").read_text()
    for s in ("AWS Transform is not connected. ", "Check the connection again", "Configure AWS Transform with SSO", "never asks for it"):
        assert s in html, s


# ------------------------------------------------------------------------------------------------ the REAL listing shape (read from the server's source)
def _artifact_calls(fake):
    return [c[1] for c in fake.calls if c[0] == "list_resources" and c[1].get("resource") == "artifacts"]


def test_the_real_listing_shape_is_read_and_the_scenario_folder_is_followed(tmp_path):
    """Seen live: A01 listed NOTHING because a file's name is fileMetadata.path, not a key beside the id.  The scenario folder is named after the scenario."""
    fake = live_service()
    fake.folder_mode = "real"
    res = make_driver(fake).run(inputs(tmp_path))
    assert res.passes == 1 and round(res.assessment.model("three_year_nuri").total) == 66969 and res.assessment.warning_rows == 251
    prefixes = {a.get("pathPrefix") for a in _artifact_calls(fake)}
    assert any(p and p.endswith("/Generated Outputs/Cost Optimized Assessment/assessment outputs/") for p in prefixes)    # the folder was found, not assumed
    assert not any("maxResults" in a or "nextToken" in a for a in _artifact_calls(fake))                                # the server auto-paginates artifacts


@pytest.mark.parametrize("flags", [{"real_paths": "full"}, {"root_has_folders": False}, {"json_as_asset": True},
                                   {"scenario": "Initial Assessment"}, {"scenario": "Right-sized & priced (v2)"}])
def test_the_real_listing_works_in_every_shape_the_server_documents(tmp_path, flags):
    fake = live_service()
    fake.folder_mode = "real"
    for k, v in flags.items():
        setattr(fake, k, v)
    res = make_driver(fake).run(inputs(tmp_path))
    assert res.passes == 1 and round(res.assessment.model("on_demand").total) == 122339, flags
    gets = [c[1] for c in fake.calls if c[0] == "get_resource" and c[1].get("resource") in ("artifact", "asset")]
    assert gets and all(g["savePath"].endswith("/") and g["fileName"] and g["workspaceId"] == WS and g["jobId"] == JOB for g in gets)   # a directory + file name
    if flags.get("json_as_asset"):
        asset = [g for g in gets if g["resource"] == "asset"]
        assert asset and asset[0]["connectorId"] == "conn-0001" and asset[0]["assetKey"].endswith("aggregated_output.json")


def test_when_the_root_listing_has_no_folders_the_canonical_folders_are_tried_directly(tmp_path):
    fake = live_service()
    fake.folder_mode, fake.root_has_folders = "real", False
    drv = make_driver(fake)
    drv.run(inputs(tmp_path))
    drv._artifacts(WS, JOB)                                                  # the whole-job crawl also tries the connector store
    prefixes = {a.get("pathPrefix") for a in _artifact_calls(fake)}
    assert f"AWSTransform/Workspaces/{WS}/Jobs/{JOB}/Generated Outputs/" in prefixes and f"transform-output/{JOB}/" in prefixes


def test_a_refused_listing_shows_the_services_own_error_and_an_empty_one_shows_the_raw_answer(tmp_path):
    fake = live_service()
    fake.fail_listing = True
    drv = make_driver(fake)
    assert drv._artifacts(WS, JOB) == []
    assert "service said ACCESS_DENIED: You are not allowed to list this job." in " | ".join(drv._listing_report)
    fake2 = live_service()
    fake2.folder_mode = "real"                                   # nothing is ready yet: the folders are empty, and the report says what came back
    drv2 = make_driver(fake2)
    drv2._artifacts(WS, JOB)
    assert any("raw answer" in x or "file(s)" in x for x in drv2._listing_report)


def test_the_warnings_file_and_the_zip_are_waited_for_briefly_once_the_numbers_are_there(tmp_path):
    fake = live_service()
    fake.folder_mode, fake.zip_after = "real", 1                                 # the zip appears one listing later
    res = make_driver(fake).run(inputs(tmp_path))
    names = [c[1]["fileName"] for c in fake.calls if c[0] == "get_resource" and c[1].get("resource") == "artifact"]
    assert res.passes == 1 and any(n.endswith(".zip") for n in names) and "error_file.csv" in names                 # the zip is named after the scenario
    fake2 = live_service()
    fake2.folder_mode, fake2.zip_after = "real", 10**6                           # it never appears: the run still completes, with what exists
    res2 = make_driver(fake2).run(inputs(tmp_path / "b" if (tmp_path / "b").mkdir() is None else tmp_path))
    assert res2.passes == 1


def test_the_newest_job_is_used_even_if_an_older_pause_is_still_stored(aws_run):
    svc, rid, _ = aws_run
    st = svc.store.state(rid)
    st["transform_pending"] = {"workspaceId": "old-ws-0001", "jobId": "old-job-0001", "kind": "approval"}
    st["transform_job"] = {"workspaceId": WS, "jobId": JOB}
    svc.store.save_state(rid, st)
    assert svc._job_ids(rid) == (WS, JOB)


def test_the_artifacts_panel_lists_and_downloads_the_real_shape_including_assets(aws_run):
    import zipfile
    svc, rid, _ = aws_run
    fake = live_service()
    fake.folder_mode, fake.json_as_asset = "real", True
    _finished_run(svc, rid, fake)
    items = {i["name"]: i for i in svc.transform_artifacts_list(rid)["items"]}
    assert items["aggregated_output.json"]["kind"] == "asset" and items["aggregated_output.json"]["connectorId"] == "conn-0001"
    assert items["aggregated_output.json"]["folder"].endswith("Cost Optimized Assessment/assessment outputs/") and items["aggregated_output.json"]["size"] == 36200
    assert items["error_file.csv"]["folder"].endswith("Generated Outputs/Cost Optimized Assessment/") and "rvtools_sample.xlsx" in items
    one = svc.transform_artifacts_fetch(rid, [items["aggregated_output.json"]["artifactId"]], False, "Rev")
    saved = svc.store.path(rid) / "out" / "transform_artifacts" / "files" / one["files"][0]["path"]
    assert saved.read_bytes() == (FIX / "aggregated_output.json").read_bytes()                       # the asset was downloaded with its connector
    allr = svc.transform_artifacts_fetch(rid, None, True, "Rev")
    assert allr["zip"] == "artifacts_all.zip" and len(allr["files"]) == len(items)
    with zipfile.ZipFile(svc.store.path(rid) / "out" / "transform" / "artifacts_all.zip") as z:
        assert any(n.endswith("assessment outputs/aggregated_output.json") for n in z.namelist())


def test_fetching_the_results_now_works_on_the_real_shape(aws_run):
    from cva.service import ServiceError
    svc, rid, _ = aws_run
    fake = live_service()
    fake.folder_mode = "real"
    svc.transform_client_factory, svc.transform_sleep = (lambda: fake), (lambda s: None)
    ticks = iter(range(0, 10**6, 50))
    fake.announce_reports = True
    fake.fail_listing = True                                                                    # the listing is refused while the run waits ...
    with pytest.raises(ServiceError, match="needs a person"):
        svc.transform_run(rid, client=fake, sleep=lambda s: None, require_approval=False, clock=lambda: next(ticks))
    with pytest.raises(ServiceError, match="ACCESS_DENIED"):                                    # ... and the reason is shown, not "0 files"
        svc.transform_fetch_results(rid, "Rev")
    fake.fail_listing = False
    svc.transform_fetch_results(rid, "Rev")
    ov = svc.overview(rid)
    assert ov["meta"]["figures_source"] == "aws_transform" and round(ov["scenarios"][0]["total"]) == 122339


def test_live_replica_the_outputs_are_fetched_and_the_tabs_filled_automatically_on_the_real_listing(ui):
    """The doubt: does A01 fetch and process the outputs BY ITSELF once AWS Transform has generated them?  Whole journey through the page's API, every action
    in a new session, instructions required each time, and the REAL listing shape: scenario folder 'Cost Optimized Assessment', the numbers file as an asset."""
    base, svc = ui
    fake = chatty_service()
    fake.folder_mode, fake.json_as_asset, fake.require_instructions, fake.echo_user = "real", True, True, True
    svc.transform_client_factory = lambda: (fake.new_session(), fake)[1]
    svc.transform_sleep = lambda s: None
    rid = svc.create_run([str(FIX / "rvtools_sample.xlsx")], as_of="2026-10-20")
    assert svc.overview(rid)["meta"]["figures_source"] == "none"                                   # nothing yet: no cost figures
    _http(f"{base}/api/run/{rid}/transform_start", {"by": "Rev"})
    v = _wait_job(base, rid, {"needs_person", "error", "done"})
    assert v["pending"]["kind"] == "approval" and not v["imported"]
    t = _job_started(base, rid)
    _http(f"{base}/api/run/{rid}/transform_resume", {"by": "Rev", "approve": True})                # the only human action: one click
    v = _wait_job(base, rid, {"done", "error"}, after=t)
    assert v["job"]["status"] == "done", v["job"]["message"]
    ov = svc.overview(rid)                                                                         # the other tabs are filled, by A01 itself
    assert v["imported"] and ov["meta"]["figures_source"] == "aws_transform"
    assert [round(s["total"]) for s in ov["scenarios"][:3]] == [122339, 89360, 66969]
    assert ov["exceptions"]["by_code"].get("TRW-UTIL") == 1                                        # the warnings file came along
    assert fake.rejected == []
    files = svc.transform_view(rid)["assessment"]["files"]
    assert {"aggregated_output.json", "error_file.csv", "analysis.xlsx"} <= set(files)             # the zip's deliverables are downloadable
    listing = svc.transform_artifacts_list(rid)                                                    # and everything the job produced can be browsed and downloaded
    assert {"aggregated_output.json", "error_file.csv"} <= {i["name"] for i in listing["items"]}


# ------------------------------------------------------------------------------------------------ seen live: one hung listing, 4 minutes of silence, then "list_failed"
def _ready_outputs(fake):
    fake.outputs_ready = True
    fake.out_ids = {n: fake._id() for n in ("aggregated_output.json", "error_file.csv", "Initial_Assessment_x.zip")}


def test_a_listing_gets_a_short_timeout_so_one_hung_call_cannot_freeze_a_check(tmp_path):
    fake = live_service()
    fake.folder_mode = "real"
    make_driver(fake).run(inputs(tmp_path))
    art = {t for (n, t), c in zip(fake.timeouts, fake.calls) if n == "list_resources" and c[1].get("resource") == "artifacts"}
    msg = {t for (n, t), c in zip(fake.timeouts, fake.calls) if n == "list_resources" and c[1].get("resource") == "messages"}
    stat = {t for n, t in fake.timeouts if n == "get_job_status"}
    assert art == {45.0} and msg == {120.0} and stat == {90.0}                          # each kind of call gets a timeout that fits it
    assert all(t is None for n, t in fake.timeouts if n not in ("list_resources", "get_job_status"))


def test_a_listing_that_times_out_is_reported_by_name_and_the_check_goes_on():
    fake = live_service()
    fake.folder_mode = "real"
    _ready_outputs(fake)
    fake.listing_exception = ["Ingestion", TimeoutError("'list_resources' did not answer within 45 s"), 99]
    drv = make_driver(fake)
    names = {a["name"] for a in drv._artifacts(WS, JOB)}                                       # a full crawl
    assert {"aggregated_output.json", "error_file.csv", "rvtools_sample.xlsx"} <= names           # the rest was still read
    assert "Generated Outputs/Ingestion outputs/: FAILED (TimeoutError" in " | ".join(drv._listing_report)
    assert drv._listing_stats[1] == 1 and drv._listing_stats[0] >= 4


def test_waiting_for_outputs_lists_only_what_it_needs():
    fake = live_service()
    fake.folder_mode = "real"
    _ready_outputs(fake)
    drv = make_driver(fake)
    names = {a["name"] for a in drv._artifacts(WS, JOB, outputs_only=True)}
    prefixes = [a.get("pathPrefix") or "(root)" for a in _artifact_calls(fake)]
    assert "aggregated_output.json" in names and len(prefixes) <= 5
    assert not any("User Uploads" in p or "Ingestion outputs" in p or p == "(root)" for p in prefixes)    # no crawl of the inputs on every check
    assert not any("transform-output" in p for p in prefixes)                                              # nor of the (always empty) connector store: one call fewer on a busy server


@pytest.mark.parametrize("how", ["answer", "exception"])
def test_a_sign_in_that_expires_in_the_middle_of_the_wait_is_raised_not_swallowed(how):
    fake = live_service()
    fake.folder_mode = "real"
    _ready_outputs(fake)
    if how == "answer":
        fake.fail_listing, fake.listing_error_code = True, "INVALID_SESSION"
    else:
        fake.listing_exception = ["", RuntimeError("AccessDeniedException: INVALID_SESSION (session cookie expired)")]
    with pytest.raises(NeedsHuman) as e:
        make_driver(fake)._artifacts(WS, JOB, outputs_only=True)
    assert e.value.kind == "connection" and "signed A01's MCP server out" in e.value.reason and "Fetch the results now" in e.value.reason


def test_three_failed_listings_in_a_row_stop_with_the_reason_instead_of_waiting_40_minutes(tmp_path):
    fake = live_service()
    fake.folder_mode = "real"
    fake.fail_listing = True                                                                       # every listing is refused (not a sign-in problem)
    with pytest.raises(NeedsHuman) as e:
        make_driver(fake).run(inputs(tmp_path))
    assert "failed on the last 3 checks" in e.value.reason and "ACCESS_DENIED" in e.value.reason and e.value.stage == "running"


def test_one_failed_listing_is_shown_readably_in_the_pages_log_and_the_run_still_completes(aws_run):
    svc, rid, _ = aws_run
    fake = live_service()
    fake.folder_mode = "real"
    fake.listing_exception = ["Generated Outputs/", TimeoutError("'list_resources' did not answer within 45 s"), 1]      # once
    lines: list[str] = []
    out = svc.transform_run(rid, client=fake, sleep=lambda s: None, require_approval=False, progress=lambda m: lines.append(str(m)))
    assert out["passes"] == 1
    assert any("listing of" in x and "failed" in x and "TimeoutError" in x for x in lines), lines


def test_a_zip_named_after_any_scenario_is_unpacked(tmp_path):
    import zipfile

    from cva.transform.results import load_results
    for f in ("aggregated_output.json", "error_file.csv"):
        (tmp_path / f).write_bytes((FIX / f).read_bytes())
    with zipfile.ZipFile(tmp_path / "Cost_Optimized_Assessment_20261020_141412.zip", "w") as z:
        z.writestr("analysis_20261020_141412.xlsx", b"x")
        z.writestr("business_case_20261020_141412.pptx", b"x")
        z.writestr("report_20261020_141412.pdf", b"x")
    assert {"analysis.xlsx", "business_case.pptx", "report.pdf"} <= set(load_results(tmp_path).files)


def test_the_newest_of_two_files_with_the_same_name_is_the_one_downloaded(tmp_path):
    fake = live_service()
    drv = make_driver(fake)

    def a(i, name, ts, folder="Generated Outputs/S/"):
        return {"kind": "artifact", "artifactId": i, "name": name, "folder": folder, "raw": {"artifactCreatedTimestamp": ts}}
    arts = [a("old-json-0001", "aggregated_output.json", "2026-10-20T10:00:00"), a("new-json-0002", "aggregated_output.json", "2026-10-20T11:00:00"),
            a("only-csv-0003", "error_file.csv", "2026-10-20T11:00:00"), a("input-zip-0004", "inputs.zip", "x", "Generated Outputs/Ingestion outputs/")]
    drv._download(WS, JOB, arts, tmp_path / "d")
    got = [c[1].get("artifactId") for c in fake.calls if c[0] == "get_resource"]
    assert "new-json-0002" in got and "old-json-0001" not in got and "input-zip-0004" not in got      # the newest numbers; an ingestion zip is not a deliverable


def test_stop_waiting_works_in_the_middle_of_a_listing():
    import threading
    fake = live_service()
    fake.folder_mode = "real"
    drv = make_driver(fake)
    drv._cancel = threading.Event()
    drv._cancel.set()
    with pytest.raises(NeedsHuman, match="Stopped by a person while listing"):
        drv._artifacts(WS, JOB)
    assert not _artifact_calls(fake)                                                              # it did not make a single listing call


# ------------------------------------------------------------------------------------------------ seen live: AWAITING_HUMAN_INPUT, an approval in the web app, timeouts, then "failed"
TASK = {"taskId": "task-0001-aaaa", "title": "Approve running the migration assessment tool", "description": "The agent asks permission to run the assessment tool on your inventory.",
        "severity": "STANDARD", "category": "TOOL_APPROVAL", "status": "AWAITING_APPROVAL", "blockingType": "BLOCKING", "createdAt": "2026-10-20T23:58:00Z"}


def _with_task(**over):
    fake = live_service()
    fake.folder_mode = "real"
    fake.task_on_proceed = {**TASK, **over}
    return fake


def test_the_status_snapshot_is_read_as_the_server_builds_it():
    from cva.transform import shape
    snap = {"success": True, "data": {"job": {"jobId": "j", "status": "AWAITING_HUMAN_INPUT"}, "worklogs": {}, "messages": {"messages": [{"text": "hello", "role": "assistant"}]},
            "tasks": {"hitlTasks": [{**TASK}, {**TASK, "taskId": "done-1", "status": "CLOSED"}, {**TASK, "taskId": "done-2", "status": "DELIVERED"}]},
            "_pollingGuidance": {"isTerminal": False, "jobStatus": "AWAITING_HUMAN_INPUT", "hasPendingTasks": True, "pendingTaskCount": 1}}}
    p = shape.parse_snapshot(snap)
    assert p["status"] == "AWAITING_HUMAN_INPUT" and not p["terminal"] and [t["taskId"] for t in p["tasks"]] == ["task-0001-aaaa"]      # finished tasks are not pending
    assert p["tasks"][0]["title"].startswith("Approve running") and p["tasks"][0]["category"] == "TOOL_APPROVAL" and p["messages"]["messages"][0]["text"] == "hello"
    done = shape.parse_snapshot({"data": {"job": {"status": "COMPLETED"}, "tasks": {"hitlTasks": []}, "_pollingGuidance": {"isTerminal": True, "jobStatus": "COMPLETED"}}})
    assert done["terminal"] and done["status"] == "COMPLETED" and done["tasks"] == []


def test_a_job_that_waits_for_an_approval_stops_at_once_shows_the_task_and_answers_nothing_by_itself(tmp_path):
    """Seen live: the job went AWAITING_HUMAN_INPUT after the go-ahead; A01 ignored the task and kept waiting for files that could not appear."""
    fake = _with_task()
    with pytest.raises(NeedsHuman) as e:
        make_driver(fake).run(inputs(tmp_path))
    n = e.value
    assert n.kind == "task" and n.stage == "running" and "waiting for 1 approval" in n.reason and n.job_id == JOB
    assert n.tasks[0]["title"] == TASK["title"] and n.tasks[0]["description"] == TASK["description"] and n.tasks[0]["category"] == "TOOL_APPROVAL"
    assert fake.completed == [] and "complete_task" not in [c[0] for c in fake.calls]                 # A01 decided nothing


def test_a_persons_approval_is_relayed_and_the_run_goes_on_to_the_results(tmp_path):
    fake = _with_task()
    with pytest.raises(NeedsHuman):
        make_driver(fake).run(inputs(tmp_path))
    res = make_driver(fake).run(inputs(tmp_path, resume=(WS, JOB), task_answer=("task-0001-aaaa", "APPROVE")))
    assert res.passes == 1 and round(res.assessment.model("on_demand").total) == 122339
    assert fake.completed == [("task-0001-aaaa", "APPROVE", None)]                                   # the decision, and no invented content


def test_a_rejection_is_relayed_and_a_failed_job_is_reported_not_waited_on(tmp_path):
    fake = _with_task()
    with pytest.raises(NeedsHuman):
        make_driver(fake).run(inputs(tmp_path))
    with pytest.raises(NeedsHuman, match="ended with status FAILED"):
        make_driver(fake).run(inputs(tmp_path, resume=(WS, JOB), task_answer=("task-0001-aaaa", "REJECT")))
    assert fake.completed == [("task-0001-aaaa", "REJECT", None)]


@pytest.mark.parametrize("over,why", [({"severity": "CRITICAL"}, "CRITICAL"), ({"category": "REGULAR"}, "asks for information")])
def test_a_task_a01_cannot_safely_answer_is_left_to_the_web_application(tmp_path, over, why):
    fake = _with_task(**over)
    fake.task_form = over.get("category") == "REGULAR"                              # a task with a form
    with pytest.raises(NeedsHuman):
        make_driver(fake).run(inputs(tmp_path))
    with pytest.raises(NeedsHuman, match=why):
        make_driver(fake).run(inputs(tmp_path, resume=(WS, JOB), task_answer=("task-0001-aaaa", "APPROVE")))
    assert fake.completed == []                                                     # nothing was sent


def test_a_decision_on_a_task_that_is_no_longer_open_or_not_a_decision_is_refused(tmp_path):
    fake = _with_task()
    with pytest.raises(NeedsHuman):
        make_driver(fake).run(inputs(tmp_path))
    with pytest.raises(NeedsHuman, match="could not show the task"):
        make_driver(fake).run(inputs(tmp_path, resume=(WS, JOB), task_answer=("not-a-task-0001", "APPROVE")))
    with pytest.raises(NeedsHuman, match="not a decision"):
        make_driver(fake).run(inputs(tmp_path, resume=(WS, JOB), task_answer=("task-0001-aaaa", "DELETE")))
    assert fake.completed == []


def test_a_non_blocking_task_never_holds_up_the_import(tmp_path):
    fake = live_service()
    fake.folder_mode = "real"
    res = make_driver(fake).run(inputs(tmp_path))
    assert res.passes == 1
    fake2 = live_service()
    fake2.folder_mode = "real"
    fake2.tasks = [{**TASK, "blockingType": "NON_BLOCKING"}]                          # open, but the job does not wait for it
    res2 = make_driver(fake2).run(inputs(tmp_path / "b" if (tmp_path / "b").mkdir() is None else tmp_path))
    assert res2.passes == 1


def test_a_slow_status_check_never_ends_the_run(tmp_path):
    """Seen live: get_job_status hung for 240 s, a messages listing timed out, and the run was reported as failed although the reports arrived."""
    fake = live_service()
    fake.folder_mode = "real"
    fake.status_failures = 10**6                                                     # the status snapshot never answers
    res = make_driver(fake).run(inputs(tmp_path))
    assert res.passes == 1 and round(res.assessment.model("three_year_nuri").total) == 66969     # the files were still found and fetched


def test_when_status_and_listings_both_time_out_for_many_checks_it_stops_with_the_reason_not_an_exception(tmp_path):
    fake = live_service()
    fake.folder_mode = "real"
    fake.status_failures = 10**6
    fake.listing_exception = ["", TimeoutError("'list_resources' did not answer within 45 s")]
    with pytest.raises(NeedsHuman) as e:
        make_driver(fake).run(inputs(tmp_path))
    assert "lost contact" in e.value.reason and "6 checks" in e.value.reason and "Fetch the results now" in e.value.reason and e.value.job_id == JOB


def test_a_job_that_ended_without_results_is_reported(tmp_path):
    for status in ("FAILED", "STOPPED"):
        fake = live_service()
        fake.folder_mode, fake.status_after_proceed = "empty", status
        with pytest.raises(NeedsHuman, match=f"ended with status {status}"):
            make_driver(fake).run(inputs(tmp_path / status if (tmp_path / status).mkdir() is None else tmp_path))
    fake = live_service()
    fake.folder_mode, fake.status_after_proceed = "empty", "COMPLETED"
    with pytest.raises(NeedsHuman, match="the job completed"):                       # completed, but the numbers file cannot be found
        make_driver(fake).run(inputs(tmp_path / "c" if (tmp_path / "c").mkdir() is None else tmp_path))


def test_any_unexpected_error_in_a_run_becomes_a_pause_with_a_way_forward_never_a_dead_failure(aws_run):
    from cva.service import ServiceError

    class Boom(FakeTransform):
        def _handle(self, name, args):
            if name == "create_workspace":
                raise RuntimeError("boom")
            return super()._handle(name, args)
    svc, rid, _ = aws_run
    with pytest.raises(ServiceError, match="needs a person") as e:
        svc.transform_run(rid, client=Boom(), sleep=lambda s: None)
    assert "lost contact with AWS Transform" in str(e.value) and "RuntimeError: boom" in str(e.value)
    pend = svc.store.state(rid)["transform_pending"]
    assert pend["kind"] == "problem" and "Fetch the results now" in pend["reason"]


def test_the_task_decision_through_the_page_api_is_validated_relayed_and_audited(ui):
    base, svc = ui
    fake = _with_task()
    fake.chatty = True
    svc.transform_client_factory, svc.transform_sleep = (lambda: (fake.new_session(), fake)[1]), (lambda s: None)
    rid = svc.create_run([str(FIX / "rvtools_sample.xlsx")], as_of="2026-10-20")
    assert _http(f"{base}/api/run/{rid}/transform_task", {"by": "Rev", "taskId": "x", "decision": "approve"})[0] == 400          # nothing paused
    _http(f"{base}/api/run/{rid}/transform_start", {"by": "Rev"})
    _wait_job(base, rid, {"needs_person", "error", "done"})
    t = _job_started(base, rid)
    _http(f"{base}/api/run/{rid}/transform_resume", {"by": "Rev", "approve": True})                                    # the scenario approval
    v = _wait_job(base, rid, {"needs_person", "error", "done"}, after=t)
    assert v["job"]["status"] == "needs_person" and v["pending"]["kind"] == "task" and v["pending"]["tasks"][0]["title"] == TASK["title"]
    for body in ({"by": "Rev", "taskId": "task-0001-aaaa", "decision": "maybe"}, {"by": "Rev", "taskId": "nope", "decision": "approve"},
                 {"by": "", "taskId": "task-0001-aaaa", "decision": "approve"}):
        assert _http(f"{base}/api/run/{rid}/transform_task", body)[0] == 400
    assert fake.completed == []
    t = _job_started(base, rid)
    assert _http(f"{base}/api/run/{rid}/transform_task", {"by": "Rev", "taskId": "task-0001-aaaa", "decision": "approve"})[0] == 200
    v = _wait_job(base, rid, {"done", "error"}, after=t)
    assert v["job"]["status"] == "done" and v["imported"] and fake.completed == [("task-0001-aaaa", "APPROVE", None)]
    ev = [(e["event"], e["actor"]) for e in svc.audit_view(rid)["entries"]]
    assert ("transform_task_decision", "Rev") in ev and ("transform_task_answered", "Rev") in ev and svc.audit_view(rid)["verify"]["ok"]


def test_the_page_shows_a_pending_approval_with_approve_and_reject():
    html = (Path(__file__).resolve().parent.parent / "cva" / "ui" / "static" / "index.html").read_text()
    for s in ("AWS Transform is waiting for an approval. ", "/transform_task", 'decide(t, "approve")', 'decide(t, "reject")', "I answered it in the AWS Transform web application",
              "never answers a task by itself"):
        assert s in html, s


def test_live_replica_scenario_approval_then_a_tool_approval_then_the_results_fill_the_tabs(ui):
    """The loop as seen live, end to end through the page's API: approve the scenario in A01; the job then blocks on a TOOL approval; A01 shows it (nothing is answered by
    itself); the person approves it in A01; A01 relays it, keeps waiting, fetches the results by itself and fills the tabs.  Every action is a NEW session with the server, the
    job's instructions are required each time, and the listing has the real shape."""
    base, svc = ui
    fake = _with_task()
    fake.chatty, fake.json_as_asset, fake.require_instructions, fake.echo_user = True, True, True, True
    svc.transform_client_factory = lambda: (fake.new_session(), fake)[1]
    svc.transform_sleep = lambda s: None
    rid = svc.create_run([str(FIX / "rvtools_sample.xlsx")], as_of="2026-10-20")
    _http(f"{base}/api/run/{rid}/transform_start", {"by": "Rev"})
    v = _wait_job(base, rid, {"needs_person", "error", "done"})
    assert v["pending"]["kind"] == "approval"                                                        # 1. the scenario approval
    t = _job_started(base, rid)
    _http(f"{base}/api/run/{rid}/transform_resume", {"by": "Rev", "approve": True})
    v = _wait_job(base, rid, {"needs_person", "error", "done"}, after=t)
    assert v["job"]["status"] == "needs_person" and v["pending"]["kind"] == "task"                    # 2. the job now waits for a tool approval: shown, not answered
    assert v["pending"]["tasks"][0]["title"] == TASK["title"] and fake.completed == [] and svc.overview(rid)["meta"]["figures_source"] == "none"
    t = _job_started(base, rid)
    _http(f"{base}/api/run/{rid}/transform_task", {"by": "Rev", "taskId": "task-0001-aaaa", "decision": "approve"})      # 3. the person approves it, in A01
    v = _wait_job(base, rid, {"done", "error", "needs_person"}, after=t)
    assert v["job"]["status"] == "done", v["job"]["message"]
    ov = svc.overview(rid)                                                                           # 4. the other tabs filled by A01 itself
    assert v["imported"] and ov["meta"]["figures_source"] == "aws_transform" and [round(s["total"]) for s in ov["scenarios"][:3]] == [122339, 89360, 66969]
    assert fake.rejected == [] and fake.completed == [("task-0001-aaaa", "APPROVE", None)]
    assert ov["exceptions"]["by_code"].get("TRW-UTIL") == 1 and {"aggregated_output.json", "analysis.xlsx"} <= set(svc.transform_view(rid)["assessment"]["files"])


# ------------------------------------------------------------------------------------------------ the analysis workbook: AWS Transform's per-server results (a REAL workbook)
ROOT = Path(__file__).resolve().parent.parent
RVT2 = ROOT / "samples" / "real_format" / "rvtools_sample_export_2.xlsx"


def _results_dir(tmp_path, agg="aggregated_output_run2.json", analysis=True):
    d = tmp_path / "res"
    d.mkdir(exist_ok=True)
    shutil.copy(FIX / agg, d / "aggregated_output.json")
    shutil.copy(FIX / "error_file.csv", d / "error_file.csv")
    if analysis:
        shutil.copy(FIX / "analysis_run2.xlsx", d / "analysis_20261020_202003.xlsx")
    return d


def _imported(svc, tmp_path, **kw):
    rid = svc.create_run([str(RVT2)], as_of="2026-10-20")
    svc.import_transform_results(rid, str(_results_dir(tmp_path, **kw)), "tester")
    return rid


def test_the_real_analysis_workbook_is_read_per_server_and_per_pricing_model():
    from cva.transform.analysis import parse_analysis
    r = parse_analysis(FIX / "analysis_run2.xlsx")
    per = r["per_server"]
    assert len(per) == 55 and per["web-p01"]["id"].startswith("server-") and per["web-p01"]["name"] == "web-p01.corp.example.local"
    assert set(per["web-p01"]["models"]) == {"on_demand", "one_year_nuri", "three_year_nuri"}
    fs = per["fs-p01"]["models"]                                                                   # the recommended instance is PER pricing model
    assert (fs["on_demand"]["instance"], fs["three_year_nuri"]["instance"]) == ("r5a.large", "t3a.xlarge")
    assert sum(1 for v in per.values() if "storage_annual" in v) == 45 and round(r["totals"]["storage"]["total"]) == 4499
    assert {e["name"] for e in r["excluded"] if e["type"] == "server"} == {"JUMP-P01", "MON-P02"}
    assert per["fs-p01"]["issues"][0]["reason"].startswith("Utilization data missing")
    m = per["web-p01"]["models"]["on_demand"]
    assert round(m["license"] + m["ec2_excl_license"] + m["network"], 2) == round(m["total"], 2)       # total = license + EC2 + network, as AWS gives it


def test_the_per_server_rows_add_up_to_aws_transforms_own_totals(tmp_path):
    res = results.load_results(_results_dir(tmp_path))
    assert len(res.per_server) == 55 and res.reconciliation and all(r["ok"] for r in res.reconciliation)
    assert {r["what"] for r in res.reconciliation} >= {"On-Demand: compute", "On-Demand: network", "Storage (EBS)"}
    assert not [i for i in res.issues if i["code"] in ("TRW-RECON", "TRW-ANALYSIS")]


def test_totals_that_do_not_match_the_workbook_are_flagged_and_nothing_is_hidden(tmp_path):
    res = results.load_results(_results_dir(tmp_path, agg="aggregated_output.json"))       # the FIRST run's totals with the SECOND run's workbook
    assert [r for r in res.reconciliation if not r["ok"]] and any(i["code"] == "TRW-RECON" for i in res.issues)
    assert len(res.per_server) == 55 and res.models


def test_an_unreadable_analysis_workbook_is_a_warning_and_the_totals_survive(tmp_path):
    d = _results_dir(tmp_path, analysis=False)
    (d / "analysis_20261020_202003.xlsx").write_bytes(b"not a workbook")
    res = results.load_results(d)
    assert res.models and not res.per_server and any(i["code"] == "TRW-ANALYSIS" and i["detail"]["recommendation"] for i in res.issues)


def test_aws_per_server_results_become_the_sizing_without_running_the_offline_estimate(svc, tmp_path):
    from cva.transform.analysis import parse_analysis
    expect = parse_analysis(FIX / "analysis_run2.xlsx")["per_server"]["web-p01"]["models"]["three_year_nuri"]["instance"]
    rid = _imported(svc, tmp_path)
    ov = svc.overview(rid)
    assert ov["sizing_source"] == "aws_transform" and ov["estimate_ran"] is False and ov["aws_servers"] == 55
    rows = {r["name"]: r for r in svc.workloads(rid)}
    a = rows["WEB-P01"]["aws"]
    assert a["matched"] and a["instance"] == expect and a["direction"] == "downsize" and a["confidence"] == "low"      # 4 vCPU today; AWS used default utilization
    assert set(a["models"]) == {"on_demand", "one_year_nuri", "three_year_nuri"} and a["models"]["on_demand"]["network"] > 0
    assert rows["JUMP-P01"]["aws"]["matched"] is False and "Exclude Powered Off" in rows["JUMP-P01"]["aws"]["reason"]
    assert sum(1 for r in rows.values() if r["aws"] and r["aws"]["matched"]) == 55 and rows["WEB-P01"]["instance"] is None      # A01's own estimate was not run
    led = svc.result(rid).ledger
    assert led.value("sizing.sized") == 55 and led.get("sizing.sized").rule == "AT-SIZING"
    assert led.value("sizing.downsize") + led.value("sizing.same") + led.value("sizing.upsize") == 55


def test_the_deck_and_workbook_export_from_aws_sizing_alone_and_say_so(svc, tmp_path):
    from cva.transform.analysis import parse_analysis
    rid = _imported(svc, tmp_path)
    out = svc.export(rid, pdf=False, template="placeholder")                                                              # before: refused, "run the offline estimate"
    deck = Presentation([p for p in out["files"].values() if str(p).endswith(".pptx")][0])
    texts = [sh.text_frame.text for s in deck.slides for sh in s.shapes if sh.has_text_frame]
    assert any("Rightsizing" in t for t in texts) and any("AWS Transform per-server sizing" in t for t in texts)
    assert any("AWS Transform's per-server recommendation" in t for t in texts)                   # the commentary says whose sizing it is
    wb = load_workbook(out["files"]["workbook"])
    wl = wb["Workloads"]
    heads = [c.value for c in wl[3]]
    assert {"AWS instance (recommended model)", "AWS On-Demand ($/yr)", "AWS 3-yr commitment ($/yr)", "AWS storage ($/yr)"} <= set(heads)
    row = next(r for r in wl.iter_rows(min_row=4, values_only=True) if r[0] == "WEB-P01")
    ps = parse_analysis(FIX / "analysis_run2.xlsx")["per_server"]["web-p01"]
    assert row[heads.index("AWS On-Demand ($/yr)")] == round(ps["models"]["on_demand"]["total"] + ps.get("storage_annual", 0.0), 2)
    assert row[heads.index("AWS instance (recommended model)")] == ps["models"]["three_year_nuri"]["instance"]


def test_an_import_without_the_workbook_still_cannot_export_and_says_why(svc, tmp_path):
    from cva.service import ServiceError
    rid = _imported(svc, tmp_path, analysis=False)
    assert svc.overview(rid)["sizing_source"] is None
    with pytest.raises(ServiceError, match="per-server results were not imported"):
        svc.export(rid, pdf=False)


def test_a01s_own_estimate_stays_a_separate_cross_check_and_aws_sizing_remains_the_headline(svc, tmp_path):
    rid = _imported(svc, tmp_path)
    svc.run_offline_estimate(rid, "Rev")
    led = svc.result(rid).ledger
    assert led.get("sizing.sized").rule == "AT-SIZING" and led.value("a01.sizing.sized") is not None and led.get("a01.sizing.sized").rule == "RS-PICK"
    ov = svc.overview(rid)
    assert ov["sizing_source"] == "aws_transform" and ov["estimate_ran"] is True
    assert any(r["instance"] and r["aws"] and r["aws"]["matched"] for r in svc.workloads(rid))        # both are available per server


def test_the_whole_automatic_journey_gives_aws_sizing_in_the_tabs_and_exports_without_the_estimate(svc):
    """Fetch by itself (real listing shape, the real zip with the real workbook) -> Workloads, Overview and the deck all have AWS's per-server sizing."""
    fake = live_service()
    fake.folder_mode, fake.run2 = "real", True
    rid = svc.create_run([str(RVT2)], as_of="2026-10-20")
    svc.transform_run(rid, client=fake, sleep=lambda s: None, require_approval=False)
    ov = svc.overview(rid)
    assert [round(s["total"]) for s in ov["scenarios"][:3]] == [113026, 82908, 62898]                    # your screen's figures
    assert ov["sizing_source"] == "aws_transform" and ov["aws_servers"] == 55 and not ov["estimate_ran"]
    view = svc.transform_view(rid)["assessment"]["per_server"]
    assert view["count"] == 55 and view["matched"] == 55 and all(r["ok"] for r in view["reconciliation"]) and sorted(view["unmatched"]) == ["JUMP-P01", "MON-P02"]
    assert svc.export(rid, pdf=False)["status"] == "DRAFT"


def test_the_page_shows_aws_per_server_sizing_on_workloads_and_the_reconciliation_on_the_aws_tab():
    html = (ROOT / "cva" / "ui" / "static" / "index.html").read_text()
    for s in ("AWS recommends", "Workloads by AWS service", "AWS Transform per-server result", "Per-server results", "does not add up", "do not add up to AWS Transform's own totals", "Sheets of the analysis workbook",
              "Rightsizing (AWS Transform)"):
        assert s in html, s


def test_fetching_the_results_now_also_brings_in_the_per_server_workbook(svc):
    """The button the person clicks on a run that was imported before the per-server import existed: it fetches again and now reads the workbook in the zip."""
    fake = live_service()
    fake.folder_mode, fake.run2 = "real", True
    _ready_outputs(fake)
    svc.transform_client_factory, svc.transform_sleep = (lambda: fake), (lambda s: None)
    rid = svc.create_run([str(RVT2)], as_of="2026-10-20")
    st = svc.store.state(rid)
    st["transform_job"] = {"workspaceId": WS, "jobId": JOB}
    svc.store.save_state(rid, st)
    svc.transform_fetch_results(rid, "Rev")
    ov = svc.overview(rid)
    assert ov["sizing_source"] == "aws_transform" and ov["aws_servers"] == 55 and [round(s["total"]) for s in ov["scenarios"][:3]] == [113026, 82908, 62898]
    svc.transform_fetch_results(rid, "Rev")                                              # and a second click is harmless
    assert svc.overview(rid)["aws_servers"] == 55


def test_the_import_box_accepts_the_analysis_workbook_directly():
    html = (ROOT / "cva" / "ui" / "static" / "index.html").read_text()
    assert '".zip,.json,.csv,.xlsx"' in html and "analysis_<timestamp>.xlsx" in html


# ------------------------------------------------------------------------------------------------ NOT_CONFIGURED again: the start-up credential check, and what the screen tells the person
class _StubClient:
    """Stands in for McpToolClient: the first ``fail_first`` sessions start 'not connected' (the server's 5-second start-up check timed out)."""
    starts, closed, fail_first = 0, 0, 0

    def __init__(self, cmd, args, env, timeout=None, errlog_path=None):
        type(self).starts += 1
        self.n = type(self).starts

    def start(self):
        return self

    def call(self, name, args, timeout=None):
        return FakeTransform.NOT_CONFIGURED if self.n <= type(self).fail_first else {"status": "connected"}

    def close(self):
        type(self).closed += 1


def _stub_mcp(monkeypatch, fail_first):
    import time

    import cva.transform.mcp_client as mc
    _StubClient.starts, _StubClient.closed, _StubClient.fail_first = 0, 0, fail_first
    monkeypatch.setattr(mc, "McpToolClient", _StubClient)
    monkeypatch.setattr(time, "sleep", lambda s: None)


def test_a_server_that_starts_not_connected_by_chance_is_started_again(svc, monkeypatch, tmp_path):
    _stub_mcp(monkeypatch, fail_first=2)                                         # the start-up check fails twice, then works
    c = svc._start_mcp({}, tmp_path / "log")
    assert _StubClient.starts == 3 and _StubClient.closed == 2 and c.n == 3      # two failed sessions were closed, the third was kept
    _stub_mcp(monkeypatch, fail_first=1)
    svc._start_mcp({}, tmp_path / "log")
    assert _StubClient.starts == 2                                               # a session that is connected at once is not restarted


def test_a_server_that_never_connects_is_reported_after_three_tries_not_hidden(svc, monkeypatch, tmp_path):
    _stub_mcp(monkeypatch, fail_first=99)
    c = svc._start_mcp({}, tmp_path / "log")
    assert _StubClient.starts == 3 and c.n == 3                                  # the last session is returned: its NOT_CONFIGURED answer is then reported
    _stub_mcp(monkeypatch, fail_first=99)
    svc._start_mcp({}, tmp_path / "log", attempts=2)
    assert _StubClient.starts == 2


def test_what_the_server_logged_at_start_up_reaches_the_person(aws_run):
    from cva.service import ServiceError
    svc, rid, _ = aws_run
    log = svc.store.path(rid) / "out" / "transform_job"
    log.mkdir(parents=True, exist_ok=True)
    (log / "mcp_server.log").write_text("noise\n2026-10-20 22:28:12.231 | INFO | awslabs.aws_transform_mcp_server.server:_call_region:248 - Region discovery failed for us-east-1:\n"
                                        "more noise\n")
    fake = live_service()
    fake.not_connected = "all"
    with pytest.raises(ServiceError, match="needs a person"):
        svc.transform_run(rid, client=fake, sleep=lambda s: None)
    pend = svc.store.state(rid)["transform_pending"]
    assert pend["kind"] == "connection" and "What the MCP server logged at start-up: " in pend["reason"] and "Region discovery failed for us-east-1" in pend["reason"]
    assert svc._mcp_log_hint(log / "nothing.log") == ""                           # no log, no hint


def test_the_messages_put_aws_credentials_first_and_a_claude_code_sign_in_last():
    from cva.transform import shape
    for t in (shape.NOT_CONNECTED_HELP, shape.NOT_CONNECTED_SHORT):
        assert "aws sts get-caller-identity" in t and "aws sso login" in t and t.index("aws sts get-caller-identity") < t.index("Configure AWS Transform")
    html = (ROOT / "cva" / "ui" / "static" / "index.html").read_text()
    for s in ("aws sts get-caller-identity", "export AWS_PROFILE=aivar-vision-admin AWS_REGION=us-east-1", "start it again from that same terminal", "Only if it still fails",
              "What the MCP server logged at start-up"):
        assert s in html, s
    assert html.index("aws sts get-caller-identity") < html.index("Only if it still fails")


# ------------------------------------------------------------------------------------------------ seen live (run-0003): A01 waited for outputs before the service had asked its question
def test_a_status_report_that_sounds_like_a_decision_is_not_taken_for_one_while_the_job_still_prepares(tmp_path):
    """The service's own status report said 'you can proceed with defaults ... how would you like to proceed?' while the job was still EXECUTING.  A01 took it for the
    decision and sent its go-ahead; the service was not waiting yet, ignored it, and later asked its real question, which nobody answered."""
    fake = live_service()
    fake.premature, fake.prepare_left = True, 6                      # the job prepares for 6 status checks, then asks and waits
    res = make_driver(fake).run(inputs(tmp_path, require_approval=False))
    assert fake.ignored_proceeds == 0                                # the go-ahead was only sent once the service was waiting
    assert res.passes == 1 and sum(1 for m in fake.sent if m.lower().startswith("proceed")) == 1


def test_the_same_in_approval_mode_the_first_request_a_person_sees_is_the_real_one(tmp_path):
    fake = live_service()
    fake.premature, fake.prepare_left = True, 6
    with pytest.raises(NeedsHuman) as e:
        make_driver(fake).run(inputs(tmp_path, require_approval=True))
    assert e.value.kind == "approval" and fake.state == "AWAITING_HUMAN_INPUT" and fake.prepare_left == 0      # asked only once the job was really waiting
    res = make_driver(fake).run(inputs(tmp_path, approve_now=True, resume=(WS, JOB)))
    assert res.passes == 1 and fake.ignored_proceeds == 0


def _proceeds(fake):
    return [m for m in fake.sent if m.lower().startswith("proceed")]


def test_the_status_is_read_from_the_job_record_not_from_the_servers_unknown_guidance():
    from cva.transform import shape
    real = {"success": True, "data": {"job": {"jobId": "j", "statusDetails": {"status": "AWAITING_HUMAN_INPUT"}}, "tasks": {"hitlTasks": []},
                                      "_pollingGuidance": {"isTerminal": False, "jobStatus": "UNKNOWN"}}}
    assert shape.parse_snapshot(real)["status"] == "AWAITING_HUMAN_INPUT"                        # the live log said "job UNKNOWN": this is what it must say instead
    assert shape.parse_snapshot({"data": {"job": {"jobId": "j"}, "_pollingGuidance": {"jobStatus": "UNKNOWN"}}})["status"] is None         # never the string UNKNOWN
    done = shape.parse_snapshot({"data": {"job": {"statusDetails": {"status": "COMPLETED"}}, "_pollingGuidance": {"isTerminal": False}}})
    assert done["status"] == "COMPLETED" and done["terminal"] is True                           # the server's isTerminal is always false: A01 decides


def test_a_go_ahead_that_did_not_take_brings_the_decision_back_to_the_person(tmp_path):
    """Approval mode.  The person approved; the service ignored it and still waits.  A01 notices (the status is readable now), goes back to the decision step, and
    shows a NEW approval request with the service's own configuration: one click again, nothing typed in the AWS chat."""
    fake = live_service()
    fake.ignore_first_proceed = True
    with pytest.raises(NeedsHuman) as e1:
        make_driver(fake).run(inputs(tmp_path, require_approval=True))
    assert e1.value.kind == "approval"
    with pytest.raises(NeedsHuman) as e2:                                                       # the go-ahead is ignored; the wait notices
        make_driver(fake).run(inputs(tmp_path, require_approval=True, approve_now=True, resume=(WS, JOB)))
    n = e2.value
    assert n.kind == "approval" and n.stage == "scenario" and "Sustainability" in n.proposal and n.send_text.startswith("Proceed")     # a new request, with the listing
    assert fake.ignored_proceeds == 1
    res = make_driver(fake).run(inputs(tmp_path, require_approval=True, approve_now=True, resume=(WS, JOB)))           # the second approval takes
    assert res.passes == 1 and len(_proceeds(fake)) == 2 and fake.ignored_proceeds == 1


def test_in_automatic_mode_the_go_ahead_is_sent_again_by_itself_when_it_did_not_take(tmp_path):
    fake = live_service()
    fake.ignore_first_proceed = True
    res = make_driver(fake).run(inputs(tmp_path, require_approval=False))
    assert res.passes == 1 and fake.ignored_proceeds == 1 and len(_proceeds(fake)) == 2


def test_the_loop_back_to_the_decision_has_a_limit_and_then_says_so(tmp_path):
    fake = live_service()
    fake.ignore_first_proceed, fake.ignore_always = True, True                                   # the service never accepts it
    with pytest.raises(NeedsHuman, match="no approval is open") as e:
        make_driver(fake).run(inputs(tmp_path, require_approval=False))
    assert e.value.kind == "problem" and len(_proceeds(fake)) == 4 and "LIVE" not in e.value.reason       # the first go-ahead and three more, then a person is told


def test_a_notice_after_the_go_ahead_is_answered_with_the_read_only_status_request(tmp_path):
    fake = live_service()
    fake.notice_after_proceed = True
    sent_before = None
    res = make_driver(fake).run(inputs(tmp_path, require_approval=False))
    assert res.passes == 1
    after = fake.sent[fake.sent.index(_proceeds(fake)[0]) + 1:]
    assert after == ["status"] and sent_before is None                                           # exactly one nudge after the go-ahead, then the results


def test_a_job_that_failed_says_why_in_awss_own_words(tmp_path):
    fake = live_service()
    fake.folder_mode, fake.status_after_proceed, fake.failure_reason = "empty", "FAILED", "The scenario could not be built from the inventory"
    with pytest.raises(NeedsHuman, match="AWS says: The scenario could not be built from the inventory"):
        make_driver(fake).run(inputs(tmp_path))


def test_your_run_0003_timeline_end_to_end_early_report_ignored_go_ahead_and_recovery(svc):
    """The live failure, whole: the service's status report sounds like a decision while the job still prepares; later it really asks; a go-ahead sent into that is
    ignored.  The person is asked once the job is really waiting, approves; the go-ahead does not take; the person is asked AGAIN (never a silent wait); approves; the
    results arrive and fill the tabs.  Every action in a fresh session, the real listing shape, the real job record."""
    from cva.service import ServiceError
    fake = live_service()
    fake.folder_mode, fake.premature, fake.prepare_left, fake.ignore_first_proceed = "real", True, 6, True
    svc.transform_client_factory, svc.transform_sleep = (lambda: (fake.new_session(), fake)[1]), (lambda s: None)
    rid = svc.create_run([str(RVT2)], as_of="2026-10-21")
    ticks = iter(range(0, 10**7, 50))

    def go(**kw):
        return svc.transform_run(rid, sleep=lambda s: None, clock=lambda: next(ticks), by="Rev", **kw)

    with pytest.raises(ServiceError, match="needs a person"):                                      # 1. asked only once the job really waits
        go(client=fake)
    p1 = svc.store.state(rid)["transform_pending"]
    assert p1["kind"] == "approval" and fake.state == "AWAITING_HUMAN_INPUT" and not _proceeds(fake)
    with pytest.raises(ServiceError, match="needs a person"):                                      # 2. approved; the go-ahead did not take; asked AGAIN
        go(client=fake, resume=(p1["workspaceId"], p1["jobId"]), approve_now=True)
    p2 = svc.store.state(rid)["transform_pending"]
    assert p2["kind"] == "approval" and p2["stage"] == "scenario" and fake.ignored_proceeds == 1
    go(client=fake, resume=(p2["workspaceId"], p2["jobId"]), approve_now=True)                     # 3. approved again; it takes; the results arrive
    ov = svc.overview(rid)
    assert ov["meta"]["figures_source"] == "aws_transform" and [round(s["total"]) for s in ov["scenarios"][:3]] == [122339, 89360, 66969]
    assert fake.ignored_proceeds == 1 and len(_proceeds(fake)) == 2 and fake.rejected == []
    assert len([e for e in svc.audit_view(rid)["entries"] if e["event"] == "transform_approved_by_person" and e["actor"] == "Rev"]) == 2      # both approvals, by name


def test_the_approve_checkbox_keeps_the_persons_choice_across_page_refreshes():
    html = (ROOT / "cva" / "ui" / "static" / "index.html").read_text()
    assert "checked: S.requireApproval !== false" in html and "S.requireApproval = appr.checked" in html


# ------------------------------------------------------------------------------------------------ NOT_CONFIGURED "every single time": A01's server had a stripped environment, and its real log was never read
def test_the_server_gets_the_whole_environment_including_proxy_and_certificate_settings(svc, monkeypatch):
    """The MCP library passes a server only HOME, PATH, USER, SHELL, TERM, LOGNAME; A01 added three AWS variables.  Proxy and company-certificate settings were dropped, so a
    server behind such a network timed out at its 5-second start-up limit and answered NOT_CONFIGURED, although the person's own terminal worked."""
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")
    monkeypatch.setenv("SSL_CERT_FILE", "/etc/company-ca.pem")
    monkeypatch.setenv("AWS_PROFILE", "aivar-vision-admin")
    monkeypatch.setenv("VIRTUAL_ENV", "/home/me/a01/.venv")
    monkeypatch.setenv("PYTHONPATH", "/somewhere")
    env = svc._mcp_env({"AWS_TRANSFORM_MCP_WRITE_DIR": "/out"})
    assert env["HTTPS_PROXY"] == "http://proxy.example:8080" and env["SSL_CERT_FILE"] == "/etc/company-ca.pem" and env["AWS_PROFILE"] == "aivar-vision-admin"
    assert env["AWS_TRANSFORM_MCP_WRITE_DIR"] == "/out" and env["FASTMCP_LOG_LEVEL"] == "ERROR" and "HOME" in env and "PATH" in env
    assert "VIRTUAL_ENV" not in env and "PYTHONPATH" not in env                              # A01's own Python environment must not leak into the server's


def test_every_place_that_starts_the_server_passes_that_environment(svc, monkeypatch, tmp_path):
    import cva.transform.mcp_client as mc
    seen: list[dict[str, str]] = []

    class Capture:
        def __init__(self, cmd, args, env, timeout=None, errlog_path=None):
            seen.append(env)

        def start(self):
            return self

        def tool_names(self):
            return set(ALLOWED_TOOLS)

        def call(self, name, args, timeout=None):
            return {"workspaces": []} if name == "list_resources" else {"status": "connected"}

        def close(self):
            pass
    monkeypatch.setattr(mc, "McpToolClient", Capture)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")
    monkeypatch.setenv("AWS_PROFILE", "aivar-vision-admin")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("CVA_TRANSFORM_MCP_CMD", "python3 -c pass")                      # a command that exists here
    assert svc.transform_preflight()["ok"] is True                                      # the pre-flight starts a server
    rid = svc.create_run([str(RVT2)], as_of="2026-10-21")
    st = svc.store.state(rid)
    st["transform_job"] = {"workspaceId": WS, "jobId": JOB}
    svc.store.save_state(rid, st)
    try:
        svc.transform_artifacts_list(rid)                                                # the artifacts panel starts one
    except Exception:
        pass
    assert len(seen) >= 2 and all(e.get("HTTPS_PROXY") == "http://proxy.example:8080" and "PATH" in e for e in seen)


def _server_log(tmp_path, *blocks):
    p = tmp_path / "server.log"
    p.write_text("\n".join(blocks) + "\n")
    return p


def _line(t, msg, fn="_probe_sigv4_transform_api:171"):
    return f"2026-10-21 {t} | INFO     | awslabs.aws_transform_mcp_server.server:{fn} - {msg}"


START = _line("16:43:55.100", "Credential probe starting (build: region-discovery), AWS_PROFILE=aivar-vision-admin, AWS_REGION=us-east-1")
FOUND = _line("16:43:56.200", "Credential probe: credentials found, starting region discovery")
NONE_ = _line("16:44:03.500", "Credential probe: no regions available, configure required")


@pytest.mark.parametrize("lines,expect", [
    ([START, FOUND, _line("16:44:03.4", "Region discovery failed for us-east-1: ", "_call_region:248"), _line("16:44:03.4", "Region discovery failed for eu-west-1: ", "_call_region:248"), NONE_],
     ["every region timed out", "failed in 2 region(s)", "no message: the 5-second limit passed", "HTTPS_PROXY"]),
    ([START, _line("16:43:56.0", "AWS credential resolution failed, skipping credential probe: Token has expired and refresh failed")],
     ["could not use your AWS login", "aws sso login --profile aivar-vision-admin"]),
    ([START, FOUND, _line("16:44:03.4", "Region discovery failed for us-east-1: An error occurred (AccessDeniedException) when calling the ListWorkspaces operation: not authorized",
                          "_call_region:248"), NONE_], ["not allowed to use AWS Transform", "Ask your AWS administrator"]),
    ([START, FOUND, _line("16:44:03.4", "Region discovery failed for us-east-1: SSL validation failed [SSL: CERTIFICATE_VERIFY_FAILED]", "_call_region:248"), NONE_],
     ["certificate problem", "SSL_CERT_FILE"]),
    ([START, FOUND, _line("16:44:00.0", "Credential probe succeeded — 2 regions available, selection required: ['us-east-1', 'eu-west-1']")], ["SEVERAL regions"]),
])
def test_the_servers_own_log_is_read_and_the_cause_is_explained_in_plain_words(svc, tmp_path, lines, expect):
    hint = svc._mcp_log_hint(tmp_path / "quiet-stderr.log", _server_log(tmp_path, *lines))
    for e in expect:
        assert e in hint, (e, hint)


def test_only_the_latest_start_up_counts_and_a_good_one_shows_no_failure(svc, tmp_path):
    old = [START, FOUND, _line("16:30:00.0", "Region discovery failed for us-east-1: ", "_call_region:248"), NONE_]
    good = [_line("16:50:00.0", "Credential probe starting (build: region-discovery), AWS_PROFILE=aivar-vision-admin, AWS_REGION=us-east-1"), _line("16:50:01.0", "Credential probe succeeded — auto-selected region us-east-1")]
    hint = svc._mcp_log_hint(tmp_path / "x.log", _server_log(tmp_path, *old, *good))
    assert "auto-selected region us-east-1" in hint and "Region discovery failed" not in hint and "failed in" not in hint and "Most likely" not in hint
    assert svc._mcp_log_hint(tmp_path / "x.log", tmp_path / "missing.log") == ""         # no log anywhere: no hint, no crash


def test_the_explanation_reaches_the_screen_and_the_preflight(aws_run, tmp_path, monkeypatch):
    from cva.service import ServiceError
    svc, rid, _ = aws_run
    slog = _server_log(tmp_path, START, FOUND, _line("16:44:03.4", "Region discovery failed for us-east-1: ", "_call_region:248"), NONE_)
    monkeypatch.setenv("CVA_MCP_SERVER_LOG", str(slog))
    fake = live_service()
    fake.not_connected = "all"
    with pytest.raises(ServiceError, match="needs a person"):
        svc.transform_run(rid, client=fake, sleep=lambda s: None)
    pend = svc.store.state(rid)["transform_pending"]
    assert pend["kind"] == "connection" and "What the MCP server logged at start-up: " in pend["reason"] and "every region timed out" in pend["reason"]
    monkeypatch.setenv("AWS_PROFILE", "p")
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    class NotSignedIn(PreflightFake):
        def _handle(self, name, args):
            self.calls.append((name, args))
            return self.NOT_CONFIGURED
    r = svc.transform_preflight(client=NotSignedIn())
    detail = next(c["detail"] for c in r["checks"] if c["name"] == "Connected to AWS Transform")
    assert "Most likely: every region timed out" in detail


# ------------------------------------------------------------------------------------------------ seen live (run at 17:16): the files existed, every call timed out for minutes
def _fresh_server(fake, unstick=True, counter=None):
    def start():
        if counter is not None:
            counter[0] += 1
        fake.new_session()                                           # a new process: it must load the job's instructions again
        if unstick:
            fake.wedged = False
        return fake
    return start


def test_a_server_that_stops_answering_is_replaced_by_a_fresh_one_and_the_run_finishes(tmp_path):
    fake = live_service()
    fake.folder_mode, fake.wedge_after_proceed, fake.require_instructions = "real", True, True
    n = [0]
    drv = make_driver(fake, client_factory=_fresh_server(fake, True, n))
    res = drv.run(inputs(tmp_path, require_approval=False))
    assert res.passes == 1 and n[0] == 1 and drv._restarts == 1
    assert fake.rejected == []                                       # the fresh session loaded the job's instructions before any job call
    ev = [e["event"] for e in drv.events]
    assert "mcp_restarting" in ev and "mcp_restarted" in ev and ev.index("mcp_restarting") < ev.index("mcp_restarted")


def test_one_slow_call_does_not_start_a_fresh_server(tmp_path):
    fake = live_service()
    fake.folder_mode, fake.status_failures = "real", 1               # one status call times out, the next answers
    n = [0]
    drv = make_driver(fake, client_factory=_fresh_server(fake, True, n))
    assert drv.run(inputs(tmp_path, require_approval=False)).passes == 1 and n[0] == 0


def test_starting_a_fresh_server_has_a_limit_and_then_a_person_is_told(tmp_path):
    import copy
    fake = live_service()
    fake.folder_mode, fake.wedge_after_proceed = "real", True
    cfg = copy.deepcopy(CFG)
    cfg["assessment"]["max_restarts"] = 2
    n = [0]
    ticks = iter(range(0, 10**6, 50))
    drv = AssessmentDriver(fake, cfg, sleep=lambda s: None, clock=lambda: next(ticks), client_factory=_fresh_server(fake, False, n))      # every fresh server is stuck too
    with pytest.raises(NeedsHuman) as e:
        drv.run(inputs(tmp_path, require_approval=False))
    assert n[0] == 2 and "starting a fresh one 2 times did not help" in e.value.reason and "Fetch the results now" in e.value.reason and e.value.job_id == JOB


def test_without_a_way_to_start_a_fresh_server_nothing_changes(tmp_path):
    fake = live_service()
    fake.folder_mode, fake.status_failures = "real", 3
    assert make_driver(fake).run(inputs(tmp_path, require_approval=False)).passes == 1       # the same behaviour as before: timeouts are tolerated


def test_the_service_lets_a_run_replace_a_stuck_server(svc):
    fake = live_service()
    fake.folder_mode, fake.wedge_after_proceed = "real", True
    n = [0]
    svc.transform_client_factory, svc.transform_sleep = _fresh_server(fake, True, n), (lambda s: None)
    rid = svc.create_run([str(RVT2)], as_of="2026-10-21")
    ticks = iter(range(0, 10**7, 50))
    svc.transform_run(rid, client=fake, sleep=lambda s: None, clock=lambda: next(ticks), require_approval=False)
    assert n[0] >= 1 and svc.overview(rid)["meta"]["figures_source"] == "aws_transform"


def test_a_stuck_real_server_is_closed_quickly_and_a_fresh_one_answers(tmp_path):
    """With the real MCP library: a server that blocks its own event loop makes EVERY call time out; closing it kills the process in seconds; a new one answers at once."""
    import time
    srv = tmp_path / "stuck_server.py"
    srv.write_text(textwrap.dedent('''
        import time
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("stuck")

        @mcp.tool()
        async def get_status() -> str:
            return "ok"

        @mcp.tool()
        async def list_resources(block: float = 0) -> str:
            time.sleep(block)            # a synchronous wait inside the server: its whole event loop is blocked
            return "listed"

        mcp.run()
    '''))
    c = McpToolClient(sys.executable, [str(srv)], timeout=20).start()
    assert c.call("get_status", {}, timeout=10)
    for _ in range(2):
        with pytest.raises(TimeoutError):
            c.call("list_resources", {"block": 40}, timeout=1)                    # the server is stuck: this and every other call times out
    with pytest.raises(TimeoutError):
        c.call("get_status", {}, timeout=2)
    t0 = time.time()
    c.close()
    assert time.time() - t0 < 15                                                  # closing a stuck server does not hang
    c2 = McpToolClient(sys.executable, [str(srv)], timeout=20).start()
    try:
        assert c2.call("get_status", {}, timeout=10)                              # a fresh server answers
    finally:
        c2.close()


def test_a_restart_is_visible_on_the_pages_log_and_the_run_still_completes(ui):
    base, svc = ui
    fake = live_service()
    fake.folder_mode, fake.wedge_after_proceed, fake.chatty = "real", True, True
    n = [0]
    svc.transform_client_factory, svc.transform_sleep = _fresh_server(fake, True, n), (lambda s: None)
    rid = svc.create_run([str(RVT2)], as_of="2026-10-21")
    _http(f"{base}/api/run/{rid}/transform_start", {"by": "Rev"})
    _wait_job(base, rid, {"needs_person", "error", "done"})
    t = _job_started(base, rid)
    _http(f"{base}/api/run/{rid}/transform_resume", {"by": "Rev", "approve": True})
    v = _wait_job(base, rid, {"done", "error", "needs_person"}, after=t)
    assert v["job"]["status"] == "done" and v["imported"], v["job"]["message"]
    log = "\n".join(str(e) for e in v["job"].get("events", []))
    assert n[0] >= 1 and "stopped answering" in log and "a fresh AWS Transform server is running" in log


# ------------------------------------------------------------------------------------------------ calls worked at 17:24:42 and stalled from ~17:25: keep the AWS login warm, notice an expired one at once
class _Proc:
    def __init__(self, code=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = code, out, err


ARN_OUT = json.dumps({"UserId": "AROA:me", "Account": "604775478403",
                      "Arn": "arn:aws:sts::604775478403:assumed-role/AWSReservedSSO_aivar_vision_admin_d1b0b81be79ae76a/shubham.anand@aivar.tech"})


@pytest.mark.parametrize("proc,kind", [
    (_Proc(255, "", "Error when retrieving token from sso: Token has expired and refresh failed"), "expired"),
    (_Proc(255, "", "The SSO session associated with this profile has expired or is otherwise invalid. To refresh this SSO session run aws sso login with the corresponding profile."), "expired"),
    (_Proc(253, "", "Unable to locate credentials. You can configure credentials by running aws configure."), "no_credentials"),
    (_Proc(255, "", "The config profile (aivar-vision-admn) could not be found"), "no_credentials"),
    (_Proc(255, "", "Could not connect to the endpoint URL: https://sts.us-east-1.amazonaws.com/"), "network"),
    (_Proc(1, "", "something nobody has seen before"), "error"),
])
def test_the_aws_login_check_names_each_way_it_can_fail(monkeypatch, proc, kind):
    from cva.transform import login
    monkeypatch.delenv("CVA_LOGIN_CHECK", raising=False)
    r = login.check_login("aivar-vision-admin", "us-east-1", run=lambda *a, **k: proc, which=lambda n: "/usr/local/bin/aws")
    assert r["ok"] is False and r["kind"] == kind and (r["detail"] or kind == "error")


def test_the_aws_login_check_reads_the_role_and_nothing_secret(monkeypatch):
    from cva.transform import login
    monkeypatch.delenv("CVA_LOGIN_CHECK", raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")
    seen = {}

    def run(cmd, **kw):
        seen.update(cmd=cmd, env=kw["env"], timeout=kw["timeout"])
        return _Proc(0, ARN_OUT)
    r = login.check_login("aivar-vision-admin", "us-east-1", run=run, which=lambda n: "/usr/local/bin/aws")
    assert r["ok"] is True and r["role"] == "AWSReservedSSO_aivar_vision_admin_d1b0b81be79ae76a" and r["account"] == "604775478403"
    assert set(r) <= {"ok", "kind", "account", "role", "seconds"}                              # no token, no key, no ARN with the person's name
    assert seen["cmd"][1:3] == ["sts", "get-caller-identity"] and "--profile" in seen["cmd"] and "aivar-vision-admin" in seen["cmd"]
    assert seen["env"]["HTTPS_PROXY"] == "http://proxy.example:8080" and seen["timeout"] > 0   # the whole environment, and a timeout


def test_the_aws_login_check_survives_a_hang_a_missing_cli_and_the_off_switch(monkeypatch):
    import subprocess

    from cva.transform import login

    def hang(*a, **k):
        raise subprocess.TimeoutExpired("aws", 1)
    assert login.check_login(None, None, which=lambda n: None)["kind"] == "disabled"                                           # the test environment switches it off
    monkeypatch.delenv("CVA_LOGIN_CHECK", raising=False)
    assert login.check_login(None, None, timeout=1, run=hang, which=lambda n: "aws")["kind"] == "timeout"                      # a stall costs seconds, not the run
    r = login.check_login(None, None, which=lambda n: None)
    assert r["ok"] is None and r["kind"] == "no_cli"
    assert "aws sso login --profile aivar-vision-admin" in login.login_help(None, "expired") and "export AWS_PROFILE=p" in login.login_help("p", "no_credentials")


def test_the_login_is_checked_at_once_then_every_five_minutes_and_before_a_fresh_server():
    t = [0.0]
    calls = []

    def check():
        calls.append(t[0])
        return {"ok": True, "kind": "ok", "role": "admin", "account": "1", "seconds": 1.0}
    drv = AssessmentDriver(FakeTransform(), CFG, sleep=lambda s: None, clock=lambda: t[0], login_check=check)
    drv._keep_login_warm(WS, JOB)
    t[0] = 120
    drv._keep_login_warm(WS, JOB)                 # too soon
    t[0] = 301
    drv._keep_login_warm(WS, JOB)
    drv._keep_login_warm(WS, JOB, force=True)     # before a fresh server
    assert calls == [0.0, 301, 301] and [e["event"] for e in drv.events].count("login_checked") == 3


@pytest.mark.parametrize("kind", ["expired", "no_credentials"])
def test_an_expired_login_stops_the_run_at_once_with_the_command_before_anything_hangs(tmp_path, kind):
    fake = live_service()
    fake.folder_mode = "real"
    drv = make_driver(fake, login_check=lambda: {"ok": False, "kind": kind, "detail": "Token has expired"})
    with pytest.raises(NeedsHuman) as e:
        drv.run(inputs(tmp_path, require_approval=False))
    assert e.value.kind == "connection" and "aws sso login --profile" in e.value.reason and e.value.job_id == JOB


def test_a_login_check_that_cannot_decide_never_stops_a_run(tmp_path):
    fake = live_service()
    fake.folder_mode = "real"
    for r in ({"ok": None, "kind": "no_cli", "detail": "no CLI"}, {"ok": False, "kind": "timeout", "detail": "slow"}, {"ok": False, "kind": "network", "detail": "proxy"}):
        drv = make_driver(fake.__class__() if False else live_service(), login_check=lambda r=r: r)
        drv.c.folder_mode = "real"
        assert drv.run(inputs(tmp_path / r["kind"] if (tmp_path / r["kind"]).mkdir() is None else tmp_path, require_approval=False)).passes == 1
        assert any(e["event"] == "login_checked" for e in drv.events)


def test_a_fresh_server_is_not_started_on_a_login_that_expired_meanwhile(tmp_path):
    import copy
    fake = live_service()
    fake.folder_mode, fake.wedge_after_proceed = "real", True
    cfg = copy.deepcopy(CFG)
    cfg["assessment"]["login_check_seconds"] = 10**9                  # only the first check and the one before a fresh server: deterministic
    answers = iter([{"ok": True, "kind": "ok", "role": "admin"}])
    n = [0]
    ticks = iter(range(0, 10**7, 50))
    drv = AssessmentDriver(fake, cfg, sleep=lambda s: None, clock=lambda: next(ticks), client_factory=_fresh_server(fake, True, n),
                           login_check=lambda: next(answers, {"ok": False, "kind": "expired", "detail": "Token has expired"}))
    with pytest.raises(NeedsHuman) as e:
        drv.run(inputs(tmp_path, require_approval=False))
    assert e.value.kind == "connection" and "aws sso login" in e.value.reason and n[0] == 0         # it said so; no fresh server was started on a dead login
    assert drv._restart_pending is False or n[0] == 0


def test_a_connection_or_login_stop_is_never_disguised_as_a_listing_problem(tmp_path):
    drv = make_driver(live_service())
    drv._ask = lambda *a, **k: (_ for _ in ()).throw(NeedsHuman("Your AWS login has expired", WS, JOB, kind="connection"))
    with pytest.raises(NeedsHuman) as e:
        drv._read_back(WS, JOB)
    assert e.value.kind == "connection" and e.value.reason == "Your AWS login has expired"
    drv._ask = lambda *a, **k: (_ for _ in ()).throw(NeedsHuman("Timed out", WS, JOB))
    with pytest.raises(NeedsHuman, match="did not list its configuration") as e2:
        drv._read_back(WS, JOB)
    assert e2.value.kind == "problem"


def test_check_again_after_the_go_ahead_keeps_waiting_it_does_not_go_back_to_the_decision(tmp_path):
    fake = live_service()
    fake.folder_mode, fake.wedge_after_proceed = "real", True
    with pytest.raises(NeedsHuman) as e1:                                          # the connection stalls after the go-ahead and A01 reports it (no way to restart here)
        make_driver(fake).run(inputs(tmp_path, require_approval=False))
    assert e1.value.stage == "running" and len(_proceeds(fake)) == 1
    before = list(fake.sent)
    fake.wedged = False                                                            # the connection is back
    res = make_driver(fake).run(inputs(tmp_path, require_approval=False, resume=(WS, JOB), resume_stage="running"))
    assert res.passes == 1 and fake.sent == before                                 # nothing was asked or sent again: it just waited and fetched


def test_the_page_passes_the_stage_so_check_again_after_a_failure_sends_nothing_new(ui):
    base, svc = ui
    fake = live_service()
    fake.folder_mode, fake.status_after_proceed, fake.chatty = "empty", "FAILED", True
    svc.transform_client_factory, svc.transform_sleep = (lambda: (fake.new_session(), fake)[1]), (lambda s: None)
    rid = svc.create_run([str(RVT2)], as_of="2026-10-21")
    _http(f"{base}/api/run/{rid}/transform_start", {"by": "Rev"})
    _wait_job(base, rid, {"needs_person", "error", "done"})
    t = _job_started(base, rid)
    _http(f"{base}/api/run/{rid}/transform_resume", {"by": "Rev", "approve": True})
    v = _wait_job(base, rid, {"needs_person", "error", "done"}, after=t)
    assert v["pending"]["stage"] == "running" and "ended with status FAILED" in v["pending"]["reason"]
    sent = list(fake.sent)
    t = _job_started(base, rid)
    _http(f"{base}/api/run/{rid}/transform_resume", {"by": "Rev"})                 # "Check again"
    v = _wait_job(base, rid, {"needs_person", "error", "done"}, after=t)
    assert v["pending"]["stage"] == "running" and fake.sent == sent                # no read-back, no second go-ahead


def test_the_preflight_shows_the_aws_login_and_which_role_it_is(svc, monkeypatch):
    monkeypatch.setenv("AWS_PROFILE", "aivar-vision-admin")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    svc.login_check = lambda: {"ok": True, "kind": "ok", "role": "AWSReservedSSO_admin_x", "account": "604775478403", "seconds": 1.2}
    r = svc.transform_preflight(client=PreflightFake())
    c = next(c for c in r["checks"] if c["name"] == "Your AWS login works")
    assert c["ok"] and "AWSReservedSSO_admin_x" in c["detail"] and "604775478403" in c["detail"] and r["ok"]
    svc.login_check = lambda: {"ok": False, "kind": "expired", "detail": "Token has expired"}
    r = svc.transform_preflight(client=PreflightFake())
    assert not r["ok"] and [c["name"] for c in r["checks"]] == ["AWS profile and region are set", "Your AWS login works"]       # it stops there: nothing else can work
    assert "aws sso login --profile aivar-vision-admin" in r["checks"][1]["detail"]
    svc.login_check = lambda: {"ok": None, "kind": "no_cli", "detail": "the AWS CLI is not installed"}
    assert next(c for c in svc.transform_preflight(client=PreflightFake())["checks"] if c["name"] == "Your AWS login works")["ok"]    # not checked is not a failure
    svc.login_check = lambda: {"ok": None, "kind": "disabled", "detail": "off"}
    assert "Your AWS login works" not in [c["name"] for c in svc.transform_preflight(client=PreflightFake())["checks"]]


def test_a_server_is_not_started_on_an_expired_login_and_a_run_says_so(aws_run, monkeypatch, tmp_path):
    from cva.service import ServiceError
    svc, rid, _ = aws_run
    svc.login_check = lambda: {"ok": False, "kind": "expired", "detail": "Token has expired"}
    with pytest.raises(ServiceError, match="aws sso login --profile"):
        svc._start_mcp({}, tmp_path / "log")
    fake = live_service()
    with pytest.raises(ServiceError, match="needs a person") as e:                       # mid-run: the keep-warm check stops it with the command
        svc.transform_run(rid, client=fake, sleep=lambda s: None)
    assert "aws sso login --profile" in str(e.value)
    assert svc.store.state(rid)["transform_pending"]["kind"] == "connection"


def test_the_diagnostics_carry_the_servers_own_log_and_the_login_check(svc, tmp_path, monkeypatch):
    import zipfile
    slog = tmp_path / "server.log"
    slog.write_text("\n".join(f"2026-10-21 17:25:{i:02d}.000 | INFO     | awslabs.x:y:1 - line {i}" for i in range(60)) + "\n")
    monkeypatch.setenv("CVA_MCP_SERVER_LOG", str(slog))
    svc.login_check = lambda: {"ok": True, "kind": "ok", "role": "admin", "account": "1", "seconds": 1.0}
    rid = svc.create_run([str(RVT2)], as_of="2026-10-21")
    svc.transform_diagnostics(rid)
    with zipfile.ZipFile(svc.store.path(rid) / "out" / "transform" / "diagnostics.zip") as z:
        assert "line 59" in z.read("aws_transform_server_log_tail.log").decode() and json.loads(z.read("login_check.json"))["role"] == "admin"


def test_the_page_explains_an_expired_login_and_offers_to_continue_waiting():
    html = (ROOT / "cva" / "ui" / "static" / "index.html").read_text()
    for s in ("Your AWS login needs attention", "Continue waiting for the results", "Your AWS login works", "/AWS login|AWS CLI/"):
        assert s in html or s == "Your AWS login works", s


def test_a_question_a01_does_not_recognise_is_shown_to_the_person_and_nothing_is_sent_over_it(tmp_path):
    """The service waits with a question that is not the scenario decision ('Which target region ...?').  A01 must not send a status request over it: it shows it at once."""
    fake = live_service()
    fake.odd_question = "Which target region would you like me to use for this assessment?"
    with pytest.raises(NeedsHuman) as e:
        make_driver(fake).run(inputs(tmp_path, require_approval=True))
    n = e.value
    assert "asking you something" in n.reason and "A01 has sent nothing" in n.reason and "Which target region" in n.proposal
    assert fake.sent[0].startswith("I've uploaded") and "status" not in fake.sent and len(fake.sent) == 1          # only the start-up message: no nudge, no read-back


def test_a_notice_still_gets_the_read_only_status_request(tmp_path):
    fake = live_service()                                            # the verified live behaviour: a "send me a message" notice
    res = make_driver(fake).run(inputs(tmp_path, require_approval=False))
    assert res.passes == 1 and "status" in fake.sent


# ---------------------------------------------------------------------------- the service asks its own question before it lists a configuration (seen live)
QUESTION_FIRST = ("I understand you want to see the exact configuration settings before making any decisions about the data quality gaps.\n\n"
                  "However, I cannot provide the complete configuration list yet because the scenario planning process requires your input on the data quality gaps first. "
                  "The planner has identified that all 19 SQL Server database instances have unrecognized environment and server role values.\n\n"
                  "Your three options again:\n1. Download an enrichment file to review and update the database classifications\n"
                  "2. Describe how to classify the databases\n3. Proceed with current inventory using the defaults noted above\n\nWhich would you prefer?")


def test_a_reply_that_lists_no_configuration_is_a_question_not_a_mismatch_and_nothing_is_sent(tmp_path):
    fake = live_service()
    fake.question_first = QUESTION_FIRST
    with pytest.raises(NeedsHuman) as e:
        make_driver(fake).run(inputs(tmp_path, require_approval=True))
    n = e.value
    assert n.kind == "approval" and n.question is True and "has not listed its configuration" in n.reason
    assert n.send_text.startswith("Proceed with the current inventory") and "exactly the configuration you just listed" not in n.send_text   # never claims a listing that does not exist
    assert not any(m.lower().startswith("proceed") for m in fake.sent)                                                                      # nothing was sent for the person
    assert n.to_dict()["question"] is True


def test_answering_the_question_fetches_the_real_listing_and_asks_again_before_anything_runs(tmp_path):
    fake = live_service()
    fake.question_first = QUESTION_FIRST
    with pytest.raises(NeedsHuman):
        make_driver(fake).run(inputs(tmp_path, require_approval=True))
    fake.sent.clear()
    with pytest.raises(NeedsHuman) as e2:                                                  # the person chose "proceed with the current inventory"
        make_driver(fake).run(inputs(tmp_path, require_approval=True, approve_now=True, answer_question=True, resume=(WS, JOB)))
    n = e2.value
    assert n.kind == "approval" and n.question is False and n.guard["ok"] is True          # the REAL configuration is now listed, checked and put to the person
    assert n.send_text.startswith("Proceed with defaults")
    assert [m for m in fake.sent if m.lower().startswith("proceed")] == [CFG["messages"]["proceed_as_is"]]      # only the answer to the question went out: the run did not start
    assert fake.outputs_ready is False
    res = make_driver(fake).run(inputs(tmp_path, require_approval=True, approve_now=True, resume=(WS, JOB)))      # the second, ordinary approval starts it
    assert res.passes == 1 and round(res.assessment.model("three_year_nuri").total) == 66969


def test_a_real_mismatch_is_still_an_approve_anyway_request_not_a_question(tmp_path):
    fake = live_service(LIVE_READBACK.replace("Sustainability: Disabled", "Sustainability: Enabled (PUE 1.55)"))
    with pytest.raises(NeedsHuman) as e:
        make_driver(fake).run(inputs(tmp_path, require_approval=True))
    assert e.value.question is False and e.value.guard["ok"] is False


def test_the_ui_route_answers_the_question_only_when_one_is_pending_and_the_person_approved(aws_run):
    from cva.service import ServiceError
    svc, rid, _ = aws_run
    fake = live_service()
    fake.question_first = QUESTION_FIRST
    with pytest.raises(ServiceError, match="has not listed its configuration"):
        svc.transform_run(rid, client=fake, sleep=lambda s: None)
    pend = svc.store.state(rid)["transform_pending"]
    assert pend["question"] is True and pend["send_text"].startswith("Proceed with the current inventory")
    with pytest.raises(ServiceError, match="needs a person"):
        svc.transform_run(rid, client=fake, sleep=lambda s: None, resume=(pend["workspaceId"], pend["jobId"]), approve_now=True, answer_question=True, by="Rev")
    pend2 = svc.store.state(rid)["transform_pending"]
    assert pend2["question"] is False and pend2["send_text"].startswith("Proceed with defaults")
    ev = [(e["event"], e["actor"]) for e in svc.audit_view(rid)["entries"]]
    assert ("transform_question_answered_by_person", "Rev") in ev


# ---------------------------------------------------------------------------- the analysis workbook: nothing skipped silently, no wall of repeated lines (seen live: 231-server MPA run)
def _workbook_with_extras(tmp_path):
    import openpyxl
    wb = openpyxl.load_workbook(FIX / "analysis_run2.xlsx")
    ws = wb["Block-EBS-Cost-Optimized"]
    hdr = [c.value for c in ws[1]]
    first = next(r for r in ws.iter_rows(min_row=2, values_only=True) if r[0])
    for i in range(3):                                                  # three volumes of a server that has no row on the per-server sheets
        row = list(first)
        row[hdr.index("Volume Name")] = f"orphan-db{i % 2 + 1}.corp.local:disk{i}.vmdk"
        ws.append(row)
    db = wb.create_sheet("RDS SQL Server - On-Demand")                  # a sheet of another layout, which A01 does not read
    db.append(["Database Id", "Database Name", "RDS Instance Recommended", "Annualized Total Cost"])
    db.append(["db-1", "orphan-db1", "db.m6i.large", 1000.0])
    p = tmp_path / "analysis_extra.xlsx"
    wb.save(p)
    return p


def test_a_sheet_the_parser_does_not_read_is_named_with_its_columns_and_orphan_volumes_make_one_line(tmp_path):
    from cva.transform.analysis import parse_analysis
    notes = parse_analysis(_workbook_with_extras(tmp_path))["notes"]
    assert any(n.startswith("not read: 'RDS SQL Server - On-Demand' (1 row(s); columns: Database Id, Database Name, RDS Instance Recommended") for n in notes)
    orphan = [n for n in notes if "have no row in the per-server sheets" in n]
    assert len(orphan) == 1 and orphan[0].startswith("3 volume(s) of 2 server(s)") and "orphan-db1" in orphan[0] and "orphan-db2" in orphan[0]
    assert not any("belongs to no server" in n for n in notes)                      # the old one-line-per-volume wall is gone
    assert not any("Read Me" in n or "Glossary" in n for n in notes)                # documentation sheets are not reported as unread data
    assert notes[0].startswith("read: ")


def test_a_clean_workbook_reports_no_orphans_and_no_unread_data_sheets():
    from cva.transform.analysis import parse_analysis
    notes = parse_analysis(FIX / "analysis_run2.xlsx")["notes"]
    assert not any("have no row" in n for n in notes) and [n for n in notes if n.startswith("not read: ")] == [
        n for n in notes if n.startswith("not read: ") and "Block-EBS-1-2-Many" in n]         # only the alternative volume mapping, which the storage total does not use


# ---------------------------------------------------------------------------- an approval screen must not outlive the job it asks about; the exchange is shown as a conversation
def test_a_paused_question_is_cleared_and_the_results_fetched_once_the_outputs_exist(aws_run):
    from cva.service import ServiceError
    svc, rid, _ = aws_run
    fake = tree_service()
    fake.folder_mode = "partial"                                                       # the numbers file cannot be listed yet
    svc.transform_client_factory, svc.transform_sleep = (lambda: fake), (lambda s: None)
    ticks = iter(range(0, 10**6, 50))
    with pytest.raises(ServiceError, match="needs a person"):
        svc.transform_run(rid, client=fake, sleep=lambda s: None, require_approval=False, clock=lambda: next(ticks))
    assert svc.store.state(rid)["transform_pending"]                                    # a question is on screen
    r = svc.transform_sync(rid)
    assert r["outputs_ready"] is False and r["fetched"] is False and r["state"] == "waiting"
    assert svc.store.state(rid)["transform_pending"]                                    # nothing is cleared while the job has no outputs
    fake.folder_mode = "strings"                                                        # the job finished meanwhile
    r = svc.transform_sync(rid)
    assert r["fetched"] is True and r["state"] == "imported"
    assert not svc.store.state(rid)["transform_pending"]                                # the stale question is gone
    assert svc.overview(rid)["meta"]["figures_source"] == "aws_transform"
    assert svc.transform_sync(rid)["state"] == "imported"                               # a later look changes nothing and fetches nothing
    ev = [(e["event"], e["actor"]) for e in svc.audit_view(rid)["entries"]]
    assert ("transform_outputs_found_while_waiting", "A01 (automatic)") in ev


def test_the_conversation_shows_who_said_what_from_the_audit_trail_and_invents_nothing(aws_run):
    svc, rid, _ = aws_run
    a = svc.audit(rid)
    a.append("transform_job_created", {}, "driver")
    a.append("transform_guard", {"ok": False}, "driver")
    a.append("transform_needs_human", {"reason": "AWS Transform has not listed its configuration yet"}, "driver")
    a.append("transform_suggestion_relayed", {"text": "Use 40% CPU for all servers"}, "Rev")
    a.append("transform_service_replied", {"text": "Understood: 40% CPU."}, "driver")
    a.append("transform_approved_by_person", {}, "Rev")
    a.append("transform_waiting", {"what": "outputs"}, "driver")                        # polling noise is not a turn
    t = svc.transform_conversation(rid)
    assert [(x["who"], x["text"][:30]) for x in t] == [
        ("A01", "Created the AWS Transform job."), ("A01", "Checked the configuration the "), ("A01", "AWS Transform has not listed i"),
        ("Rev", "Use 40% CPU for all servers"), ("AWS Transform", "Understood: 40% CPU."), ("Rev", "Approved: the go-ahead was sen")]
    assert "does NOT match" in t[1]["text"]
    assert svc.transform_view(rid)["conversation"] == t


# ------------------------------------------------------------------------------------------------ presentation: names, roadmap, services (0.13.0)
def _import_with_database(svc, rid, tmp_path, rename_db1=None):
    import openpyxl

    from tests.test_ingram_ola import _workbook_with_database
    p = _workbook_with_database(tmp_path)
    if rename_db1:
        wb = openpyxl.load_workbook(p)
        for ws in wb.worksheets:
            for row in ws.iter_rows():
                for c in row:
                    if isinstance(c.value, str) and c.value.startswith("db1.corp.local"):
                        c.value = c.value.replace("db1.corp.local", rename_db1)
        wb.save(p)
    f = tmp_path / "res"
    f.mkdir(exist_ok=True)
    for n in ("aggregated_output.json", "error_file.csv"):
        (f / n).write_bytes((FIX / n).read_bytes())
    shutil.copy(p, f / "analysis_20261020_202003.xlsx")
    svc.import_transform_results(rid, str(f), "t")


def test_runs_are_named_after_the_customer_and_can_be_renamed(svc):
    rid = svc.create_run([str(FIX / "rvtools_sample.xlsx")], as_of="2026-10-20", customer_label="Acme Retail")
    assert svc.list_runs()[0]["name"] == "Acme Retail" and svc.overview(rid)["name"] == "Acme Retail" and rid.startswith("run-")
    other = svc.create_run([str(FIX / "rvtools_sample.xlsx")], as_of="2026-10-20")
    assert [r["name"] for r in svc.list_runs()][1] == "rvtools sample"           # no customer: the file's name, never the id
    svc.rename_run(other, "  Northwind   Traders ", "tester")
    assert svc.overview(other)["name"] == "Northwind Traders"
    with pytest.raises(ServiceError):
        svc.rename_run(other, "", "tester")
    with pytest.raises(ServiceError):
        svc.rename_run(other, "x", "")


def test_the_roadmap_reads_the_audit_trail_in_plain_language(svc):
    rid = svc.create_run([str(FIX / "rvtools_sample.xlsx")], as_of="2026-10-20")
    assert svc.transform_roadmap(rid)["steps"] == []
    a = svc.audit(rid)
    for ev, d in (("transform_tool", {"tool": "create_workspace"}), ("transform_job_created", {"workspaceId": "w", "jobId": "job-12345678"}), ("transform_tool", {"tool": "upload_artifact"})):
        a.append(ev, d, "driver")
    st = lambda r: {s["key"]: s["state"] for s in r["steps"]}  # noqa: E731
    r = svc.transform_roadmap(rid, "running")
    assert st(r) == {"workspace": "done", "job": "done", "inventory": "done", "configuration": "active", "assessment": "pending", "import": "pending"}
    assert "analysing" in r["steps"][3]["title"] and not any("{" in s["title"] or "_" in s["title"] for s in r["steps"])
    s2 = svc.store.state(rid)
    s2["transform_pending"] = {"kind": "approval", "workspaceId": "w", "jobId": "j"}
    svc.store.save_state(rid, s2)
    r = svc.transform_roadmap(rid, "needs_person")
    assert st(r)["configuration"] == "needs_you" and r["steps"][3]["title"] == "Your approval is needed"
    a.append("transform_approved_by_person", {}, "Asha")
    s2["transform_pending"] = None
    svc.store.save_state(rid, s2)
    r = svc.transform_roadmap(rid, "running")
    assert st(r)["configuration"] == "done" and st(r)["assessment"] == "active" and r["approved_by"] == "Asha" and "Approved by Asha" in r["steps"][3]["detail"]
    assert svc.transform_roadmap(rid, "error", "boom")["steps"][4]["state"] == "error"


def test_workloads_are_grouped_by_aws_service_and_databases_get_their_own_group(svc, tmp_path):
    rid = svc.create_run([str(FIX / "rvtools_sample.xlsx")], as_of="2026-10-20")
    _import_with_database(svc, rid, tmp_path, rename_db1="WEB-P01")
    c = svc.service_catalog(rid)
    keys = [b["key"] for b in c["services"]]
    assert keys[:2] == ["ec2", "rds"] and "ebs" in keys and "s3" in keys
    rds = next(b for b in c["services"] if b["key"] == "rds")
    assert [i["name"] for i in rds["items"]] == ["WEB-P01"] and rds["items"][0]["annual"] and rds["items"][0]["target"] == "db.m8i.xlarge"
    ec2 = next(b for b in c["services"] if b["key"] == "ec2")
    assert not any(i["name"] == "WEB-P01" for i in ec2["items"])                    # a server is in exactly one group
    assert abs(sum(b["share"] for b in c["services"] if b["share"] is not None) - 100) < 0.5
    assert svc.workloads(rid)                                                       # the flat list is unchanged for the agent and the exports


def test_the_commitment_scenarios_are_not_called_reserved_instances_only(svc, tmp_path):
    rid = svc.create_run([str(FIX / "rvtools_sample.xlsx")], as_of="2026-10-20")
    _import_with_database(svc, rid, tmp_path)
    labels = [s["label"] for s in svc.overview(rid)["scenarios"]]
    assert "1-year commitment" in labels and "3-year commitment" in labels and not any("RI" in x.split() for x in labels)
    assert "Reserved Instances" in svc.overview(rid)["commitment_note"]


def test_the_agent_tab_is_hidden_but_its_code_is_kept_and_the_release_checks_read_plainly():
    html = (ROOT / "cva" / "ui" / "static" / "index.html").read_text()
    nav = html[html.index("const VIEWS"):html.index("\n", html.index("const VIEWS"))]
    assert '"Agent"' not in nav and "async function agent(v)" in html
    gate = (ROOT / "cva" / "review" / "gate.py").read_text()
    assert "Cross-check differences are reviewed" in gate and "Parity outliers" not in gate
