"""The A01 <-> AWS Transform driver: A01 runs the assessment job itself.

Flow (mirrors the manual run that worked in Claude Code, October 2026):
  create workspace -> create job (full settings in the objective) -> load instructions -> upload the inventory ->
  first message -> wait for the service's proposed scenario -> CHECK it against what we asked for -> confirm only on a
  match -> wait for the outputs -> download -> import.  Optional second pass: fetch the enrichment template, fill it with
  our real measurements, upload, apply, re-run (settings re-sent in full), check, confirm, download again.

Safety, in code: the tool client has an allow-list (``complete_task`` and every destructive tool are blocked); a proposal
that does not match is NEVER confirmed (a person must override); nothing outside the run folder is written.

UNVERIFIED: response shapes (message, artifact and status fields) are read defensively because they are not documented.
Every raw response is written to ``transform_trace.jsonl`` so a first real run shows exactly what to adjust."""
from __future__ import annotations

import dataclasses
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from ..config import load_toml
from . import guard as guard_mod
from . import login as login_mod
from . import shape
from .results import AwsAssessment, load_results


class ToolClient(Protocol):
    def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]: ...


class NeedsHuman(Exception):
    def __init__(self, reason: str, workspace_id: str | None = None, job_id: str | None = None, proposal: str = "",
                 guard: dict[str, Any] | None = None, kind: str = "problem", send_text: str = "", reply: str = "",
                 stage: str = "scenario", customised: bool = False, tasks: list[dict[str, Any]] | None = None, needs_listing: bool = False, question: bool = False) -> None:
        super().__init__(reason)
        self.reason, self.workspace_id, self.job_id, self.proposal, self.guard = reason, workspace_id, job_id, proposal, guard or {}
        self.kind = kind              # "approval": a person is asked to approve (A01 then sends the go-ahead) | "problem": something needs looking at
        self.send_text = send_text    # for an approval: the exact message A01 will send to AWS Transform
        self.reply = reply            # the service's answer to the person's last suggestion, shown with the next approval request
        self.stage = stage            # "scenario": before the go-ahead was sent | "running": after it (waiting for outputs)
        self.customised = customised  # the person has changed something by suggestion: approving then sends the "agreed values" go-ahead
        self.question = question      # kind "approval", but the service listed NO configuration: it asked its own question first (or is still processing). Nothing to check yet
        self.needs_listing = needs_listing   # kind "approval" after the person's own exchange with the service: A01 has said nothing more; the configuration listing is fetched when the person asks
        self.tasks = tasks or []      # kind "task": the human tasks (approvals) the job is waiting on: title, description, severity, category, status

    def to_dict(self) -> dict[str, Any]:
        return {"reason": self.reason, "workspaceId": self.workspace_id, "jobId": self.job_id,
                "proposal": self.proposal, "guard": self.guard, "kind": self.kind, "send_text": self.send_text,
                "reply": self.reply, "stage": self.stage, "customised": self.customised, "tasks": self.tasks, "needs_listing": self.needs_listing, "question": self.question}


@dataclass
class DriverInputs:
    files: list[Path]
    out_dir: Path
    workspace_name: str = "a01-assessment"
    job_name: str = "a01-migration-assessment"
    kind: str | None = None                                    # detected source kind of the primary file
    enrich: Callable[[Path, Path], dict[str, Any]] | None = None
    accept_proposal: bool = False                              # a person's override of a failed check
    resume: tuple[str, str] | None = None                      # (workspaceId, jobId)
    allow_unlisted_format: bool = False
    require_approval: bool = False                             # always stop for a person's approval in A01 before the go-ahead is sent (the UI default)
    approve_now: bool = False                                  # a person approved in A01: send the go-ahead now, without re-reading anything
    reply_text: str = ""                                       # a suggestion or question typed in A01: relayed unchanged, then the loop continues
    reply_stage: str = "scenario"                              # where the job was paused: scenario | running
    task_answer: tuple[str, str] | None = None                 # (taskId, "APPROVE" | "REJECT"): a person's decision on a human task, relayed on resume
    answer_question: bool = False                              # the person chose to answer the service's own question ("proceed with the current inventory"): send that, then fetch the real listing
    show_listing: bool = False                                 # a person asked to see the configuration the service will run (after their own exchange with it)
    resume_stage: str = "scenario"                             # where a resumed run was paused: "running" = the go-ahead was already sent: just keep waiting for the outputs
    customised: bool = False                                   # earlier suggestions were agreed: the go-ahead says "the values we agreed"
    confirmed_externally: bool = False                         # a person already confirmed the scenario in the AWS Transform web application
    cancel: threading.Event | None = None                      # set by a person to stop waiting (the AWS job itself keeps running)


@dataclass
class DriverResult:
    workspace_id: str
    job_id: str
    results_dir: Path
    assessment: AwsAssessment
    passes: int = 1
    events: list[dict[str, Any]] = field(default_factory=list)
    enrichment: dict[str, Any] | None = None


LISTED_KINDS = {"rvtools", "cloudamize_mpa"}
def asks_a_question(text: str) -> bool:
    """Does the message end by asking the person something?  (Its last non-empty line contains a question mark.)"""
    lines = [ln.strip() for ln in str(text or "").strip().splitlines() if ln.strip()]
    return bool(lines) and "?" in lines[-1]


def is_deliverable_zip(a: dict[str, Any]) -> bool:
    """The assessment's deliverables zip.  It is named after the SCENARIO (Initial_Assessment_<ts>.zip, Cost_Optimized_Assessment_<ts>.zip ...), so any zip
    that is not an input or an ingestion output counts."""
    folder = (a.get("folder") or "").lower()
    return a["name"].rsplit("/", 1)[-1].lower().endswith(".zip") and "ingestion" not in folder and "user uploads" not in folder


def is_result_file(a: dict[str, Any]) -> bool:
    return a["name"].rsplit("/", 1)[-1] in ("aggregated_output.json", "error_file.csv") or is_deliverable_zip(a)


def _newest(files: list[dict[str, Any]]) -> dict[str, Any]:
    """Of several files with the same name (a re-run), the most recently created."""
    return max(files, key=lambda a: str((a.get("raw") or {}).get("artifactCreatedTimestamp") or ""))


REFUSAL_TOOLS = {"send_message", "upload_artifact", "load_instructions"}      # a refusal of these is never silently treated as success


class AssessmentDriver:
    def __init__(self, client: ToolClient, cfg: dict[str, Any] | None = None, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic, on_event: Callable[[str, dict[str, Any]], None] | None = None,
                 trace_path: Path | None = None, client_factory: Callable[[], ToolClient] | None = None,
                 login_check: Callable[[], dict[str, Any]] | None = None) -> None:
        self.c = client
        self._login_check = login_check                    # one read-only AWS CLI call: keeps the login fresh for the server and reports an expired one at once
        self._last_login_at: float | None = None
        self._factory = client_factory                     # starts a FRESH server session (a new process): used when the current one stops answering
        self._consec_timeouts, self._restarts, self._restart_pending = 0, 0, False
        self._last_ids: tuple[str, str] | None = None
        self.cfg = cfg or load_toml(Path(__file__).resolve().parents[2] / "config" / "transform.toml")
        self.sleep, self.clock, self.on_event, self.trace_path = sleep, clock, on_event, trace_path
        self.events: list[dict[str, Any]] = []
        self._cancel: threading.Event | None = None
        self._newest_first = True        # the live listing returned the NEWEST message first (inferred from a run); learned again from each listing
        self._prev_ids: list[str] = []
        self._listing_report: list[str] = []
        self._listing_stats: tuple[int, int, str] = (0, 0, "")           # (listings that answered, listings that failed, the last error) of the latest crawl
        self._folders: dict[str, str] = {}   # job folders found so far (relative form -> as the service wrote it)
        self._sent: list[str] = [str(self.cfg["messages"][k]).strip() for k in (        # what A01 itself sends: never mistaken for the service's words
            "config_request", "proceed", "proceed_as_is", "proceed_custom", "proceed_enriched", "rerun", "enrichment_request", "enrichment_apply", "status_request")
            if self.cfg["messages"].get(k)]                                            # (a new session starts with the standard texts; it adds what it sends)
        self._diag = ""                                        # what the driver last saw: shown on screen so a wait is never silent

    # ----------------------------------------------------------------- plumbing
    def _log(self, event: str, **data: Any) -> None:
        e = {"event": event, **data}
        self.events.append(e)
        if self.on_event:
            self.on_event(event, data)

    def _call(self, tool: str, /, **args: Any) -> dict[str, Any]:
        if tool == "send_message":
            self._sent.append(str(args.get("text", "")).strip())
        ws_id, job_id = args.get("workspaceId"), args.get("jobId")
        if ws_id and job_id:
            self._last_ids = (ws_id, job_id)
        if self._restart_pending:
            self._restart()
        kw: dict[str, Any] = {}
        to = self._timeout_for(tool, args)
        if to and getattr(self.c, "accepts_timeout", False):
            kw["timeout"] = to                                                     # one hung call must not freeze a check for 4 minutes
        try:
            raw = self.c.call(tool, args, **kw)
            self._consec_timeouts = 0
        except NeedsHuman:
            raise
        except TimeoutError:                                                       # the server did not answer: two in a row means the session is stuck
            self._consec_timeouts += 1
            if self._factory is not None and self._consec_timeouts >= int(self.cfg["assessment"].get("restart_after_timeouts", 2)):
                self._restart_pending = True
            raise
        except Exception as e:
            if shape.not_connected(str(e)) or shape.auth_problem(str(e)):
                raise NeedsHuman(shape.EXPIRED_HELP, ws_id, job_id, kind="connection") from e
            raise
        resp = shape.unwrap(raw)
        if shape.not_connected(raw) or shape.not_connected(resp):
            raise NeedsHuman(shape.NOT_CONNECTED_HELP, ws_id, job_id, kind="connection")
        if shape.auth_problem(raw) or shape.auth_problem(resp):
            raise NeedsHuman(shape.EXPIRED_HELP, ws_id, job_id, kind="connection")
        if tool != "load_instructions" and ws_id and job_id and "INSTRUCTIONS_REQUIRED" in json.dumps(resp, default=str):
            # The service refuses job tools until load_instructions was called IN THIS SESSION (it is per session with the MCP server, so a
            # resumed run needs it again).  Load them, retry once; never treat the refusal as success.
            self._log("instructions_required", tool=tool)
            self.c.call("load_instructions", {"workspaceId": ws_id, "jobId": job_id})
            raw = self.c.call(tool, args)
            resp = shape.unwrap(raw)
            if "INSTRUCTIONS_REQUIRED" in json.dumps(resp, default=str):
                raise NeedsHuman(f"AWS Transform keeps refusing '{tool}' until its instructions are loaded. It answered: {self._excerpt(resp)}", ws_id, job_id)
        if tool in REFUSAL_TOOLS and isinstance(resp, dict) and (resp.get("success") is False or resp.get("error")):
            raise NeedsHuman(f"AWS Transform did not accept '{tool}'. It answered: {self._excerpt(resp)}", ws_id, job_id)
        self._log("tool", tool=tool, args={k: (v if len(str(v)) < 120 else str(v)[:120] + "...") for k, v in args.items()})
        if self.trace_path:
            self.trace_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.trace_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"tool": tool, "args_keys": sorted(args),
                                     "args": {k: str(v)[:200] for k, v in args.items() if k not in ("text", "content")},
                                     "response": json.dumps(raw, default=str)[:6000]}) + "\n")
        return resp

    @property
    def settings(self) -> dict[str, Any]:
        return self.cfg["settings"]

    def _wait(self, predicate: Callable[[], Any], what: str, ws: str, job: str, max_s: float | None = None) -> Any:
        limit = max_s or float(self.cfg["assessment"]["max_wait_seconds"])
        start = self.clock()
        poll = float(self.cfg["assessment"]["poll_seconds"])
        while True:
            if self._cancel is not None and self._cancel.is_set():
                raise NeedsHuman(f"Stopped by a person while waiting for {what}. The job keeps running in AWS: A01 cannot stop it. "
                                 "Stop or delete it in the AWS Transform web application if it is no longer wanted.", ws, job)
            self._keep_login_warm(ws, job)
            v = predicate()
            if v:
                return v
            elapsed = int(self.clock() - start)
            self._log("waiting", what=what, elapsed=elapsed, detail=self._diag)
            if elapsed > limit:
                raise NeedsHuman(f"Timed out waiting for {what} after {int(limit // 60)} minutes. Last seen: {self._diag}", ws, job)
            self.sleep(poll)

    # ----------------------------------------------------------------- steps
    @staticmethod
    def _excerpt(resp: Any, n: int = 320) -> str:
        """The service's own answer, shortened: shown on screen so a failure is never just 'could not read'."""
        return json.dumps(resp, default=str)[:n]

    def _find_by_name(self, resource: str, name: str, ws: str | None = None) -> str | None:
        """Look a workspace or job up by the name we gave it when the creation answer carried no readable id."""
        args: dict[str, Any] = {"resource": resource}
        if ws:
            args["workspaceId"] = ws
        try:
            resp = self._call("list_resources", **args)
        except Exception as e:
            self._log("lookup_failed", resource=resource, error=str(e)[:200])
            return None
        id_keys = ("workspaceId", "jobId", "id")
        hits = [d for d in shape.iter_dicts(resp) if any(isinstance(v, str) and v == name for k, v in d.items() if k in ("name", "workspaceName", "jobName", "title"))]
        for d in reversed(hits):                                    # the newest match
            for k in id_keys:
                if isinstance(d.get(k), str) and d[k]:
                    return d[k]
        return None

    def _start(self, inp: DriverInputs) -> tuple[str, str]:
        if inp.kind and inp.kind not in LISTED_KINDS and not inp.allow_unlisted_format:
            raise NeedsHuman(
                f"AWS Transform's documented inputs are RVTools, MPA and Migration Evaluator exports; '{inp.kind}' is not listed. "
                "Supply Cloudamize's MPA export, or confirm you want to try this file (allow_unlisted_format).")
        st = self._call("get_status")
        low = json.dumps(st).lower()
        if "not configured" in low or "not available" in low or "unavailable" in low:
            raise NeedsHuman("AWS Transform is not reachable with the current credentials: " + json.dumps(st)[:300])
        resp = self._call("create_workspace", name=inp.workspace_name)
        ws = shape.find_id(resp, ("workspaceId", "id")) or self._find_by_name("workspaces", inp.workspace_name)
        if not ws:
            raise NeedsHuman("Could not read the workspace id from create_workspace. The service answered: " + self._excerpt(resp) +
                             " (A workspace named '" + inp.workspace_name + "' may already exist in the AWS Transform web application.)")
        objective = "Assess on-premises inventory for migration to AWS. " + guard_mod.settings_text(self.settings)
        resp = self._call("create_job", workspaceId=ws, jobName=inp.job_name, objective=objective,
                          intent="Migration assessment of on-premises workloads", orchestratorAgent=self.cfg["assessment"]["agent"])
        job = shape.find_id(resp, ("jobId", "id")) or self._find_by_name("jobs", inp.job_name, ws)
        if not job:
            raise NeedsHuman("Could not read the job id from create_job. The service answered: " + self._excerpt(resp), ws)
        self._call("load_instructions", workspaceId=ws, jobId=job)
        self._log("job_created", workspaceId=ws, jobId=job)
        up: list[tuple[str, str]] = []
        types = self.cfg["assessment"]["upload_types"]
        for f in inp.files:
            ftype = types.get(f.suffix.lower())
            if ftype is None:
                raise NeedsHuman(f"upload type for '{f.suffix}' is not configured", ws, job)
            r = self._call("upload_artifact", workspaceId=ws, jobId=job, content=str(f), fileName=f.name, fileType=ftype)
            up.append((f.name, shape.find_id(r, ("artifactId", "id")) or "?"))
        label = {"rvtools": "an RVTools export", "cloudamize_mpa": "a Cloudamize Migration Portfolio Assessment (MPA) export"}.get(
            inp.kind or "", "an inventory export")
        listing = ", ".join('artifact %s, "%s"' % (aid, name) for name, aid in up)
        self._call("send_message", workspaceId=ws, jobId=job,
                   text=f"I've uploaded {label} ({listing}) as my on-premises inventory. "
                        f"Please use it for the assessment. Preferences: {guard_mod.settings_text(self.settings)}")
        return ws, job

    def _agent_messages(self, ws: str, job: str, resp: Any = None) -> list[dict[str, Any]]:
        """Agent messages OLDEST FIRST.  ``resp`` may be an answer that already carries the messages (the status snapshot): then nothing is fetched.  If the
        listing returns ids without text, the text is fetched with get_resource.  When the records carry no timestamps, the listing's direction is learned by
        watching where new messages appear."""
        if resp is None:
            resp = self._call("list_resources", resource="messages", workspaceId=ws, jobId=job)
        msgs = shape.collect_messages(resp)
        if not msgs:
            ids = shape.message_ids(resp)
            if ids:
                msgs = shape.collect_messages(self._call("get_resource", resource="messages", workspaceId=ws, jobId=job, messageIds=ids[-20:]))
        agent = [m for m in msgs if "user" not in m["who"] and "human" not in m["who"] and m["text"].strip() not in self._sent
                 and not m["text"].lstrip().startswith("I've uploaded")]
        if not (msgs and all(m["ts"] for m in msgs)):                  # no timestamps: the listing order is all we have
            ids = [m["id"] or m["text"] for m in agent]
            if self._prev_ids and ids != self._prev_ids:
                new_pos = [i for i, x in enumerate(ids) if x not in self._prev_ids]
                old_pos = [i for i, x in enumerate(ids) if x in self._prev_ids]
                if new_pos and old_pos:
                    if max(new_pos) < min(old_pos):
                        self._newest_first = True
                    elif min(new_pos) > max(old_pos):
                        self._newest_first = False
            self._prev_ids = ids
            if self._newest_first:
                agent = agent[::-1]
        return agent

    def _agent_ids(self, ws: str, job: str) -> set[str]:
        return {m["id"] or m["text"] for m in self._agent_messages(ws, job)}

    def _keep_login_warm(self, ws: str, job: str, force: bool = False) -> None:
        """Every ``login_check_seconds`` (and before a fresh server starts) check the AWS login with the AWS CLI.  This refreshes a login that is near its end into the cache the
        server reads, so the server's own calls do not have to; and an expired login stops the run NOW with the exact command, instead of every call hanging for minutes."""
        if self._login_check is None:
            return
        every = float(self.cfg["assessment"].get("login_check_seconds", 300))
        now = self.clock()
        if not force and self._last_login_at is not None and now - self._last_login_at < every:
            return
        self._last_login_at = now
        r = self._login_check()
        self._log("login_checked", ok=r.get("ok"), kind=r.get("kind"), role=r.get("role"), detail=str(r.get("detail", ""))[:160], seconds=r.get("seconds"))
        if r.get("ok") is False and r.get("kind") in ("expired", "no_credentials"):
            raise NeedsHuman(login_mod.login_help(os.environ.get("AWS_PROFILE"), r["kind"], str(r.get("detail", ""))), ws or None, job or None, kind="connection")

    def _restart(self) -> None:
        """The server session stopped answering (seen live: every call, of every kind, timing out for minutes while the files already existed).  The cure is a fresh
        server process: close the stuck one (about 2 s; it kills the process), start another, load the job's instructions again, and carry on.  At most
        ``max_restarts`` times in a run, then a person is told."""
        ws, job = self._last_ids or (None, None)
        limit = int(self.cfg["assessment"].get("max_restarts", 6))
        if self._restarts >= limit:
            raise NeedsHuman(f"The connection to the AWS Transform server stopped answering, and starting a fresh one {self._restarts} times did not help. The job keeps running in AWS. "
                             "Check the connection (the 'Check the connection first' button shows what the server logged), then use 'Fetch the results now and fill the other tabs'.",
                             ws, job, kind="problem")
        self._keep_login_warm(ws or "", job or "", force=True)                    # a fresh server must not start on an expired login
        self._restart_pending = False
        self._restarts += 1
        self._log("mcp_restarting", n=self._restarts, note=f"the AWS Transform server stopped answering ({self._consec_timeouts} timeouts in a row): starting a fresh one")
        try:
            self.c.close()                                                         # best effort: a stuck server is killed, a dead one is already gone
        except Exception:
            pass
        assert self._factory is not None
        self.c = self._factory()
        self._consec_timeouts = 0
        if ws and job:                                                             # a new session must load the job's instructions before it accepts job calls
            try:
                self.c.call("load_instructions", {"workspaceId": ws, "jobId": job})
            except Exception as e:
                self._log("load_instructions_failed", error=str(e)[:200])
        self._log("mcp_restarted", n=self._restarts)

    def _timeout_for(self, tool: str, args: dict[str, Any]) -> float | None:
        """A timeout per kind of call: a file listing is quick, the message listing and the status snapshot fan out to several backend calls and are given longer;
        the rest keep the client's default."""
        a = self.cfg["assessment"]
        if tool == "list_resources":
            return float(a.get("messages_timeout_seconds", 120)) if args.get("resource") == "messages" else float(a.get("list_timeout_seconds", 45))
        if tool == "get_job_status":
            return float(a.get("status_timeout_seconds", 90))
        return None

    def snapshot(self, ws: str, job: str) -> dict[str, Any]:
        """``get_job_status(detailed=true)``, parsed: the job status, the human tasks waiting for a person, the recent messages.  Read-only: it sends nothing
        (without ``detailed`` the server would post a status question into the job's chat and wait for the assistant's answer)."""
        return shape.parse_snapshot(self._call("get_job_status", workspaceId=ws, jobId=job, detailed=True))

    def probe(self, ws: str, job: str) -> dict[str, Any]:
        """Read-only look at the job, for the screen: its status, whether the result files exist, the human tasks open, and the service's last messages.
        Sends nothing.  A job whose outputs exist is DONE, whatever question the screen last showed."""
        snap = self.snapshot(ws, job)
        arts = self._artifacts(ws, job, outputs_only=True, messages_resp=snap["messages"])
        names = [x["name"].rsplit("/", 1)[-1] for x in arts]
        try:
            said = [m["text"] for m in self._agent_messages(ws, job, snap["messages"])][-6:]
        except Exception:
            said = []
        return {"status": snap["status"], "outputs_ready": "aggregated_output.json" in names, "files": sorted(set(names))[:12],
                "tasks": len(snap.get("tasks") or []), "said": [t[:700] for t in said]}

    def _job_state(self, ws: str, job: str) -> str | None:
        try:
            return self.snapshot(ws, job)["status"]
        except NeedsHuman:
            raise                                                  # a sign-in problem is never swallowed
        except Exception as e:                                     # a status failure must not break the wait
            self._log("job_state_unavailable", error=f"{type(e).__name__}: {str(e)[:140]}")
            return None

    def _await_proposal(self, ws: str, job: str, after: set[str] | None = None) -> str:
        """Find the point where the service waits for OUR decision, then have it list its configuration back, and return THAT reply
        (the thing the configuration check judges).  Two steps because the service is an AI assistant whose wording varies from run to
        run: its own status text cannot be relied on to name every setting, but a direct request to list them can."""
        baseline = set(after or ())
        self._await_decision(ws, job, baseline)
        return self._read_back(ws, job)

    def _await_decision(self, ws: str, job: str, baseline: set[str]) -> str:
        """Wait until the service is waiting for a person.  Signals, none of which needs an exact phrase: the job state says awaiting
        input; or the service answered our nudge with something other than a notice; or its message uses a waiting/confirmation phrase.

        VERIFIED live: the service does not announce its decision point by itself.  It posts "I have an update for you. Please send a
        message (e.g., 'status')" and waits.  Nudges are counted PER MESSAGE: a nudge may be answered by a mere status report and the
        service may later post a new notice, which deserves a new nudge."""
        msgs_cfg = self.cfg["messages"]
        pats = [re.compile(p, re.I) for p in list(msgs_cfg["confirm_patterns"]) + list(msgs_cfg.get("waiting_patterns", []))]
        notif = [re.compile(p, re.I) for p in msgs_cfg.get("notification_patterns", [])]
        stall = float(self.cfg["assessment"].get("stall_seconds", 300))
        poke_every = float(self.cfg["assessment"].get("poke_seconds", 45))
        max_pokes = int(self.cfg["assessment"].get("max_pokes", 4))
        max_total = int(self.cfg["assessment"].get("max_total_pokes", 40))
        since: list[float | None] = [None]
        st: dict[str, Any] = {"cur": None, "n": 0, "last": None, "total": 0, "seen": set()}

        def mid(m: dict[str, Any]) -> str:
            return m["id"] or m["text"]

        def is_notice(m: dict[str, Any]) -> bool:
            return any(p.search(m["text"]) for p in notif)

        def fresh(agent: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return [m for m in agent if mid(m) not in baseline]

        def decision_in(new: list[dict[str, Any]]) -> str | None:
            hits = [m for m in new if not is_notice(m) and any(p.search(m["text"]) for p in pats)]
            return hits[-1]["text"] if hits else None

        def ready() -> str | None:
            new = fresh(self._agent_messages(ws, job))
            state = self._job_state(ws, job)
            may_decide = state is None or "AWAIT" in state          # a job that is still working is not waiting for us, whatever its messages say
            found = decision_in(new)                                # (seen live: a status report said "you can proceed with defaults ..." while the job was still preparing)
            if found and may_decide:
                return found
            newest = new[-1] if new else None
            cur = mid(newest) if newest else None
            if cur != st["cur"]:                                      # a new message from the service: a fresh chance to answer it
                st["cur"], st["n"], st["last"] = cur, 0, None
            waiting = bool(state and "AWAITING" in state)
            if waiting and newest is not None and not is_notice(newest) and asks_a_question(newest["text"]):
                # The service is waiting on a QUESTION that is not the scenario decision.  A01 does not speak over it (no status request, no read-back): it shows it at once.
                raise NeedsHuman("AWS Transform is asking you something that A01 does not recognise as the scenario decision (its message is shown below). A01 has sent nothing. "
                                 "Reply from here in the box below, or answer it in the AWS Transform web application and use Check again.", ws, job, proposal=newest["text"])
            now = self.clock()
            if ((newest is not None and is_notice(newest)) or waiting) and st["n"] < max_pokes and st["total"] < max_total \
                    and (st["last"] is None or now - st["last"] >= poke_every):
                self._log("poke", n=st["n"] + 1, note="the service waits for a message before showing its update: sent a read-only status request")
                st["seen"] = {mid(m) for m in new}
                self._call("send_message", workspaceId=ws, jobId=job, text=msgs_cfg.get("status_request", "status"))
                st["n"] += 1
                st["total"] += 1
                st["last"] = now
                new = fresh(self._agent_messages(ws, job))
                state = self._job_state(ws, job)
                may_decide = state is None or "AWAIT" in state
                waiting = bool(state and "AWAITING" in state)           # the state AFTER the nudge, not the one from before it
                found = decision_in(new)
                if found and may_decide:
                    return found
                newest = new[-1] if new else None
            # the job says it waits for a person and the service has answered our nudge with something that is not a notice
            if waiting and st["n"] >= 1 and newest is not None and not is_notice(newest) and mid(newest) not in st["seen"]:
                return newest["text"]
            last = newest["text"] if newest else None
            self._diag = (f"job state {state or 'unknown'}; {len(new)} new agent message(s); status requests sent: {st['total']}; "
                          f"latest: {(last or 'none')[:160]!r}" + ("; the job is still working, so its messages are not taken for a question yet" if found and not may_decide else ""))
            if state and any(w in state for w in ("FAILED", "CANCELLED", "CANCELED", "STOPPED")):
                raise NeedsHuman(f"AWS Transform reports the job as {state}.", ws, job, proposal=last or "")
            if waiting and st["n"] >= max_pokes:
                since[0] = since[0] if since[0] is not None else now
                if now - since[0] >= stall:
                    raise NeedsHuman("AWS Transform is waiting for a person, but A01 did not recognise its latest message as a scenario "
                                     "proposal. Read it in the AWS Transform web application; if you confirm it there, use \"I confirmed it "
                                     "in the web application\" to continue." if last else
                                     "AWS Transform is waiting for a person, but A01 could not read any message from the job (see the "
                                     "trace file). Open the job in the AWS Transform web application; if you confirm it there, use "
                                     "\"I confirmed it in the web application\" to continue.", ws, job, proposal=last or "")
            else:
                since[0] = None
            return None
        return self._wait(ready, "the service to wait for a decision", ws, job)

    def _ask(self, ws: str, job: str, text: str, what: str) -> str:
        """Send ``text`` to the service and return its NEW answer (a message that is not a notice).  If it posts a notice instead and waits,
        nudge it (bounded) and keep waiting.  Used for the read-back request and for a person's suggestion, which is relayed unchanged."""
        msgs_cfg = self.cfg["messages"]
        notif = [re.compile(p, re.I) for p in msgs_cfg.get("notification_patterns", [])]
        limit = float(self.cfg["assessment"].get("readback_seconds", 240))
        poke_every = float(self.cfg["assessment"].get("poke_seconds", 45))
        max_pokes = int(self.cfg["assessment"].get("max_pokes", 4))
        before = {m["id"] or m["text"] for m in self._agent_messages(ws, job)}
        self._call("send_message", workspaceId=ws, jobId=job, text=text)
        st: dict[str, Any] = {"n": 0, "last": None}

        def answered() -> str | None:
            new = [m for m in self._agent_messages(ws, job) if (m["id"] or m["text"]) not in before]
            replies = [m for m in new if not any(p.search(m["text"]) for p in notif)]
            if replies:
                return replies[-1]["text"]
            now = self.clock()
            if new and st["n"] < max_pokes and (st["last"] is None or now - st["last"] >= poke_every):   # a notice again: nudge it
                st["n"] += 1
                st["last"] = now
                self._log("poke", n=st["n"], note="the service posted a notice and waits: sent a read-only status request")
                self._call("send_message", workspaceId=ws, jobId=job, text=msgs_cfg.get("status_request", "status"))
            self._diag = f"waiting for {what}; {len(new)} new message(s) so far"
            return None
        return self._wait(answered, what, ws, job, max_s=limit)

    def _read_back(self, ws: str, job: str) -> str:
        """Ask the service to list the configuration it will run, and return its reply.  This is what the configuration check judges."""
        try:
            return self._ask(ws, job, self.cfg["messages"]["config_request"], "the service to list its configuration")
        except NeedsHuman as e:
            if e.kind != "problem":                                  # a connection or login problem keeps its own kind and screen
                raise
            raise NeedsHuman("AWS Transform did not list its configuration when asked, so it cannot be checked. " + e.reason, ws, job) from e

    def _confirm(self, ws: str, job: str, proposal: str, inp: DriverInputs, message: str, need_approval: bool = True,
                 reply: str = "", force_approval: bool = False) -> dict[str, Any]:
        """Check the service's configuration; then either send the go-ahead or ask a person.  A mismatch ALWAYS needs a person.  A match needs
        one too when ``require_approval`` is on (the UI default) or when the person has just made a suggestion (``force_approval``).  What the
        person approves is sent by A01: nobody types in the AWS Transform chat."""
        g = guard_mod.check(proposal, self.cfg["expected"])
        self._log("guard", **g.to_dict())
        approved = inp.accept_proposal or inp.approve_now
        custom = inp.customised or force_approval
        must = self.cfg["expected"].get("must_contain", [])
        if not inp.approve_now and must and len(g.missing) == len(must) and not g.forbidden:
            # NOT A LISTING.  None of the expected settings appears: the service did not list a configuration, it said something else (seen live: "I cannot provide the complete
            # configuration list yet ... your input on the data quality gaps first", or "your data is being processed").  That is not a mismatch to approve "anyway": the standard
            # go-ahead ("exactly the configuration you just listed") would be false.  The person is shown what the service said and chooses how to answer it.
            self._log("not_a_listing", note="the service listed no configuration: it is asking something first or still processing")
            raise NeedsHuman("AWS Transform has not listed its configuration yet, so there is nothing to check. It asked something first, or it is still processing (read its reply below). "
                             "If it asks about the inventory, answer it with the button, or reply in the box; if it says it is still processing, use Check again. A01 sends nothing until you act.",
                             ws, job, proposal, g.to_dict(), kind="approval", send_text=self.cfg["messages"]["proceed_as_is"], reply=reply, customised=inp.customised, question=True)
        if not approved and not g.ok:
            raise NeedsHuman("The configuration AWS Transform listed does not match what was requested: " + g.report() +
                             ". Approve to run it anyway, suggest a change, or stop.", ws, job, proposal, g.to_dict(), kind="approval",
                             send_text=message, reply=reply, customised=custom)
        if not approved and (force_approval or (inp.require_approval and need_approval)):
            raise NeedsHuman("Approval needed before AWS Transform runs. The configuration it listed matches what was requested.",
                             ws, job, proposal, g.to_dict(), kind="approval", send_text=message, reply=reply, customised=custom)
        if approved and not g.ok:
            self._log("guard_overridden_by_person", problems=g.report())
        self._call("send_message", workspaceId=ws, jobId=job, text=message, skipPolling=True)
        return g.to_dict()

    def _outputs(self, ws: str, job: str, known: set[str], reask: Any = None) -> list[dict[str, Any]]:
        """Wait for the outputs; a stop here is marked as happening AFTER the go-ahead (a reply then continues the wait, it does not re-ask).  An approval request
        raised here is the scenario decision again, so it keeps the stage 'scenario'."""
        try:
            return self._await_outputs(ws, job, known, reask)
        except NeedsHuman as e:
            if e.kind != "approval":
                e.stage = "running"
            raise

    def _reconfirm(self, ws: str, job: str, inp: DriverInputs, message: str) -> None:
        """The job waits for the scenario decision AGAIN: our go-ahead came before the service was ready, or did not take.  Go back to the decision step: find the
        service's question, have it list its configuration, check it, and either ask the person (approval mode) or send the go-ahead again (automatic mode)."""
        proposal = self._await_proposal(ws, job)
        again = dataclasses.replace(inp, approve_now=False, accept_proposal=False, confirmed_externally=False, reply_text="", task_answer=None)
        self._confirm(ws, job, proposal, again, message)

    def _list_all(self, page_size: int | None = None, **args: Any) -> list[dict[str, Any]]:
        """Every page of one listing.  (The server paginates artifacts by itself and says not to pass nextToken; if an answer carries one anyway
        it is followed.)  Returns the answers, in order."""
        pages: list[dict[str, Any]] = []
        token: str | None = None
        for _ in range(25):
            call = dict(args)
            if page_size:
                call["maxResults"] = page_size
            if token:
                call["nextToken"] = token
            resp = self._call("list_resources", **call)
            pages.append(resp)
            token = shape.find_key(resp, ("nextToken", "next_token", "NextToken"))
            if not (isinstance(token, str) and token):
                break
        return pages

    def _artifacts(self, ws: str, job: str, outputs_only: bool = False, messages_resp: Any = None) -> list[dict[str, Any]]:
        """Every downloadable file of the job, following how the AWS Transform MCP server actually lists them (read from its source):
        * with a jobId and no pathPrefix it queries BOTH stores and returns ``{artifacts, assets, folders}``; it does NOT descend into folders: each folder
          string is listed again VERBATIM as ``pathPrefix`` (a prefix is matched EXACTLY: no other forms are tried);
        * some stages omit ``folders`` at the root, so the canonical folders are tried directly as well;
        * a file's name is ``fileMetadata.path``; files may come as ``assets`` (downloaded with the listing's connectorId).
        ``outputs_only`` lists just ``Generated Outputs/`` (minus the ingestion folder) and the connector output: what waiting for results needs, in a few calls
        instead of a crawl of the whole job.  A call that fails or hangs is cut off (see ``list_timeout_seconds``), counted and reported, never silent; a sign-in
        problem is raised.  The whole crawl has a time budget."""
        found: dict[str, dict[str, Any]] = {}
        report: list[str] = []
        ok = failed = 0
        last_err = ""
        t0 = time.monotonic()
        budget = float(self.cfg["assessment"].get("listing_budget_seconds", 75))
        marker = f"/Jobs/{job}/"
        root = f"AWSTransform/Workspaces/{ws}/Jobs/{job}/"

        def rel(p: str) -> str:
            return p.split(marker, 1)[1] if marker in p else (p[len(root):] if p.startswith(root) else p)

        def wanted(p: str) -> bool:
            r = rel(p)
            return not outputs_only or not (r.startswith("User Uploads/") or r.startswith("Generated Outputs/Ingestion outputs/"))

        def take(pages: list[dict[str, Any]], prefix: str) -> tuple[int, list[str], str | None]:
            n, folders, err = 0, [], None
            for resp in pages:
                files, fl = shape.parse_listing(resp)
                err = err or shape.error_of(resp)
                for f in files:
                    p = f.get("path") or ""
                    f["folder"] = (p.rsplit("/", 1)[0] + "/") if f["kind"] == "artifact" and "/" in p else rel(prefix)
                    found.setdefault(f["artifactId"], f)
                    n += 1
                folders += fl
            return n, folders, err

        # the frequent check lists only where the results are (Generated Outputs/); the connector store (transform-output/) has been empty in every real run and costs a call on a server
        # that may be struggling, so it is left to the whole-job crawl (every 4th check, the artifacts panel, "Fetch the results now")
        seeds = [root + "Generated Outputs/"] if outputs_only else ["", root + "Generated Outputs/", root + "User Uploads/", f"transform-output/{job}/"]
        todo: list[str] = seeds + [f for f in self._folders.values() if wanted(f)]
        seen: set[str] = set()
        while todo and len(seen) < 80:
            if self._cancel is not None and self._cancel.is_set():
                raise NeedsHuman("Stopped by a person while listing the job's files. The job keeps running in AWS: A01 cannot stop it.", ws, job)
            if time.monotonic() - t0 > budget:
                report.append(f"(listing stopped after {budget:.0f} s; the rest is read on the next check)")
                break
            prefix = todo.pop(0)
            key = rel(prefix) or prefix
            if key in seen:
                continue
            seen.add(key)
            label = rel(prefix) or ("(root)" if not prefix else prefix)
            args: dict[str, Any] = {"resource": "artifacts", "workspaceId": ws, "jobId": job}
            if prefix:
                args["pathPrefix"] = prefix
            t1 = time.monotonic()
            try:
                pages = self._list_all(**args)
            except NeedsHuman:
                raise
            except Exception as e:                                           # a call that failed or hung: counted, reported, and the crawl goes on
                failed += 1
                last_err = f"{type(e).__name__}: {str(e)[:140]}"
                self._log("list_failed", prefix=label, error=last_err)
                report.append(f"{label}: FAILED ({last_err[:70]})")
                continue
            n, folders, err = take(pages, prefix)
            secs = time.monotonic() - t1
            if err:
                failed += 1
                last_err = err
                report.append(f"{label}: service said {err[:90]}")
            else:
                ok += 1
                line = f"{label}: {n} file(s), {len(folders)} folder(s)"
                if not n and not folders and not label.startswith("transform-output"):
                    line += f" [raw: {json.dumps(pages[0], default=str)[:90]}]"
                report.append(line + (f" ({secs:.0f}s)" if secs >= 5 else ""))
            for f in folders:
                if wanted(f):
                    self._folders.setdefault(rel(f) or f, f)
                    if (rel(f) or f) not in seen:
                        todo.append(f)
        try:                                                                 # the service also names the files (with their ids) in its own messages
            mresp = messages_resp if messages_resp is not None else self._call("list_resources", resource="messages", workspaceId=ws, jobId=job)
            for a in shape.find_artifacts(mresp):
                a.setdefault("folder", "")
                found.setdefault(a["artifactId"], a)
        except NeedsHuman:
            raise
        except Exception as e:
            self._log("list_failed", prefix="(messages)", error=f"{type(e).__name__}: {str(e)[:140]}")
        self._listing_report = report[-14:]
        self._listing_stats = (ok, failed, last_err)
        return list(found.values())

    def answer_task(self, ws: str, job: str, task_id: str, action: str) -> None:
        """Relay a PERSON's decision on a human task (an approval the job is waiting on).  The only place A01 ever calls complete_task, and only for a task
        that needs nothing but the decision: a tool approval, or a task with no form, that is not CRITICAL.  The task is fetched again first, so the decision
        is checked against what the service says NOW.  A task that asks for information is left to the web application: A01 would have to invent the answers."""
        if action not in ("APPROVE", "REJECT"):
            raise NeedsHuman(f"'{action}' is not a decision A01 relays (only APPROVE or REJECT)", ws, job)
        resp = self._call("get_resource", resource="task", workspaceId=ws, jobId=job, taskId=task_id)
        err = shape.error_of(resp)
        if err:
            raise NeedsHuman(f"AWS Transform could not show the task: {err}", ws, job)
        task = next((d["task"] for d in shape.iter_dicts(resp) if isinstance(d.get("task"), dict)), None) or {}
        title = str(task.get("title") or task_id)[:160]
        if not task:
            raise NeedsHuman("AWS Transform did not return that task: it may already be answered. Use Check again.", ws, job)
        if task.get("status") in shape.TERMINAL_TASK:
            raise NeedsHuman(f"The task '{title}' is already {task.get('status')}. Use Check again.", ws, job)
        if task.get("severity") == "CRITICAL":
            raise NeedsHuman(f"'{title}' is a CRITICAL task: it needs an administrator in the AWS Transform web application.", ws, job)
        if task.get("category") != "TOOL_APPROVAL" and task.get("_responseTemplate"):
            raise NeedsHuman(f"'{title}' asks for information (a form). Complete it in the AWS Transform web application: A01 would have to invent the answers.", ws, job)
        resp = self._call("complete_task", workspaceId=ws, jobId=job, taskId=task_id, action=action)
        err = shape.error_of(resp)
        if err:
            raise NeedsHuman(f"AWS Transform did not accept the {action.lower()} of '{title}': {err}", ws, job)
        self._log("task_answered", taskId=task_id, title=title, action=action)

    def list_job_artifacts(self, ws: str, job: str) -> tuple[list[dict[str, Any]], list[str]]:
        """Every file of the job with its folder and size, and the report of what each listing returned (for the artifacts panel)."""
        arts = self._artifacts(ws, job)
        return [{"artifactId": a["artifactId"], "name": a["name"].rsplit("/", 1)[-1], "folder": a.get("folder", ""), "size": a.get("size"),
                 "kind": a.get("kind", "artifact"), "assetKey": a.get("assetKey"), "connectorId": a.get("connectorId")} for a in arts], list(self._listing_report)

    def fetch_item(self, ws: str, job: str, item: dict[str, Any], dest_dir: Path, file_name: str) -> Path:
        """Download one file (an artifact, or an asset with its connector) into ``dest_dir``.  The server treats a savePath ending in "/" as a directory and
        appends fileName, and writes only inside its allowed base directory."""
        dest_dir.mkdir(parents=True, exist_ok=True)
        save = str(dest_dir) + os.sep
        if item.get("kind") == "asset":
            if not item.get("connectorId"):
                raise NeedsHuman(f"'{file_name}' is an asset but the listing gave no connectorId, which the download needs", ws, job)
            resp = self._call("get_resource", resource="asset", workspaceId=ws, jobId=job, connectorId=item["connectorId"],
                              assetKey=item["assetKey"], savePath=save, fileName=file_name)
        else:
            resp = self._call("get_resource", resource="artifact", workspaceId=ws, jobId=job, artifactId=item["artifactId"], savePath=save, fileName=file_name)
        err = shape.error_of(resp)
        if err:
            raise NeedsHuman(f"AWS Transform could not download '{file_name}': {err}", ws, job)
        saved = shape.find_key(resp, ("savedTo",))
        path = Path(saved) if isinstance(saved, str) and saved else dest_dir / os.path.basename(file_name)
        if not path.exists():
            raise NeedsHuman(f"AWS Transform reported '{file_name}' as downloaded but it is not on disk ({path.name})", ws, job)
        return path

    def _await_outputs(self, ws: str, job: str, known: set[str], reask: Any = None) -> list[dict[str, Any]]:
        """Wait for the assessment outputs, one check at a time.  Each check reads the status snapshot (job status, the human tasks waiting for a person, the
        recent messages) and lists ``Generated Outputs/`` (every 4th check the whole job).  Nothing that goes wrong in a check ends the run:
        * results present: done (the warnings file and the zip get ``outputs_grace_seconds`` to land);
        * the job FAILED or STOPPED: stop and say so;
        * the job waits for a person's APPROVAL (a human task): stop at once with the task shown, so it can be answered from A01;
        * the job waits for chat input but no task is open: stop with the service's last message after ``awaiting_after_confirm_seconds``;
        * the reports are announced, or the job COMPLETED, but the numbers file is not visible for ``reports_without_json_seconds``: stop and say what was seen;
        * the status and the listings both fail on ``max_failed_checks`` checks in a row: stop with the reason (the job keeps running in AWS)."""
        a = self.cfg["assessment"]
        awaiting_limit = float(a.get("awaiting_after_confirm_seconds", 45))
        partial = float(a.get("reports_without_json_seconds", 90))
        grace = float(a.get("outputs_grace_seconds", 60))
        max_bad = int(a.get("max_failed_checks", 6))
        since: list[float | None] = [None]
        reports: list[float | None] = [None]
        json_seen: list[float | None] = [None]
        polls, bad, task_polls = [0], [0], [0]
        msgs_cfg = self.cfg["messages"]
        pats = [re.compile(p, re.I) for p in list(msgs_cfg["confirm_patterns"]) + list(msgs_cfg.get("waiting_patterns", []))]
        notif = [re.compile(p, re.I) for p in msgs_cfg.get("notification_patterns", [])]
        max_reasks = int(a.get("max_reasks", 3))
        nudges, reasks = [0], [0]

        def latest(msgs: Any) -> str:
            try:
                ag = self._agent_messages(ws, job, msgs) if msgs is not None else []
            except Exception:
                ag = []
            return ag[-1]["text"] if ag else ""

        def ready() -> list[dict[str, Any]] | None:
            polls[0] += 1
            snap: dict[str, Any] | None = None
            try:
                snap = self.snapshot(ws, job)
            except NeedsHuman:
                raise
            except Exception as e:                                       # the service is slow: counted, shown, and the check goes on
                self._log("status_unavailable", error=f"{type(e).__name__}: {str(e)[:140]}")
            msgs = snap["messages"] if snap else None
            arts = [x for x in self._artifacts(ws, job, outputs_only=(polls[0] % 4 != 0), messages_resp=msgs) if x["artifactId"] not in known]
            ok, failed, last_err = self._listing_stats
            bad[0] = bad[0] + 1 if (failed and not ok) else 0                     # not one listing answered: the files cannot be seen at all
            names = [x["name"].rsplit("/", 1)[-1] for x in arts]
            has_csv, has_zip = "error_file.csv" in names, any(is_deliverable_zip(x) for x in arts)
            status = snap["status"] if snap else None
            now = self.clock()
            listing = " | ".join(self._listing_report)[:420]
            transient = "Timeout" in last_err or "did not answer" in last_err           # a slow service gets longer than a refusal
            if bad[0] >= (max_bad if transient else 3):
                raise NeedsHuman(f"A01 lost contact with AWS Transform's file listing: it failed on the last {bad[0]} checks (the last error: {last_err or 'none given'}; "
                                 f"the job status is {status or 'unavailable'}). The job keeps running in AWS. If the sign-in expired, sign in again and click 'Check the "
                                 "connection first'; then use 'Fetch the results now and fill the other tabs'. What each listing returned: " + listing, ws, job)
            if "aggregated_output.json" in names:                          # the results are there
                json_seen[0] = json_seen[0] if json_seen[0] is not None else now
                if (has_csv and has_zip) or now - json_seen[0] >= grace:   # the warnings file and the zip may land a moment after the numbers
                    return arts
                self._diag = "aggregated_output.json is there; waiting a moment for error_file.csv and the assessment zip"
                return None
            if status in ("FAILED", "STOPPED"):
                raise NeedsHuman(f"The AWS Transform job ended with status {status} before its results appeared."
                                 + (f" AWS says: {snap['failure']}." if snap and snap.get("failure") else "")
                                 + " Open the job in the AWS Transform web application to see why; A01 cannot restart it.", ws, job, proposal=latest(msgs))
            blocking = [t for t in (snap["tasks"] if snap else []) if t.get("blockingType") != "NON_BLOCKING"]
            if blocking:                                                   # the job waits for a person's approval
                task_polls[0] += 1
                if task_polls[0] >= 2:                                     # two checks in a row: not a task that closes by itself
                    n = len(blocking)
                    raise NeedsHuman(f"AWS Transform is waiting for {n} approval{'s' if n != 1 else ''} before it can continue: "
                                     + "; ".join(t["title"] for t in blocking) + ". The job stays paused in AWS until each one is answered.",
                                     ws, job, kind="task", tasks=blocking, proposal=latest(msgs))
            else:
                task_polls[0] = 0
            self._diag = (f"job {status or 'status unavailable'}; {len(blocking)} approval(s) waiting; {len(arts)} new file(s): {', '.join(names)[:160] or 'none'}; "
                          f"listing: {listing or 'n/a'}")
            said_ready = False
            if msgs is not None:
                try:
                    said_ready = any(re.search(r"reports? (is|are)? ?ready|deliverables", m["text"], re.I) for m in self._agent_messages(ws, job, msgs)[-6:])
                except Exception:
                    said_ready = False
            if said_ready or has_csv or has_zip or status == "COMPLETED":
                reports[0] = reports[0] if reports[0] is not None else now
                if now - reports[0] >= partial:
                    seen = ("A01 can see " + ", ".join(names)) if names else "A01 cannot list any output file"
                    raise NeedsHuman("AWS Transform says the reports are ready (or the job completed), and " + seen + ", but A01 cannot find aggregated_output.json (the "
                                     "numbers), which sits in the scenario's 'assessment outputs' folder under 'Generated Outputs'. What each listing returned: " + (listing or "n/a") +
                                     ". Use 'Fetch the results now and fill the other tabs', or in the AWS Transform web application open the job's Artifacts, "
                                     "download aggregated_output.json and error_file.csv, and use 'Choose File' then 'Import file(s)' on this tab. "
                                     "'Download diagnostics' shows exactly what the service returned.", ws, job)
            if status and "AWAITING" in status and not blocking:
                since[0] = since[0] if since[0] is not None else now
                if now - since[0] >= awaiting_limit:
                    last_text = latest(msgs)
                    if last_text and any(p.search(last_text) for p in notif) and nudges[0] < 3:      # it waits for any message before it shows its update
                        nudges[0] += 1
                        self._log("poke", n=nudges[0], note="the service waits for a message before showing its update: sent a read-only status request")
                        self._call("send_message", workspaceId=ws, jobId=job, text=msgs_cfg.get("status_request", "status"), skipPolling=True)
                        since[0] = now
                        return None
                    if reask is not None and reasks[0] < max_reasks and last_text and any(p.search(last_text) for p in pats) and not any(p.search(last_text) for p in notif):
                        reasks[0] += 1                                                                # the scenario question is open again: go back to the decision step
                        self._log("scenario_asked_again", n=reasks[0], note="the job still waits for the scenario decision")
                        since[0] = None
                        reask()
                        return None
                    raise NeedsHuman(f"The job shows '{status}' but no approval is open, so the service probably asked something in the chat (or the go-ahead did not "
                                     "register). Its last message is shown below. Reply from here in the box below (for example: proceed with defaults), or if you already "
                                     "answered in the AWS Transform web application, use Check again.", ws, job, proposal=latest(msgs))
            else:
                since[0] = None
            return None
        return self._wait(ready, "the assessment outputs", ws, job)

    def _download(self, ws: str, job: str, arts: list[dict[str, Any]], dest: Path) -> list[Path]:
        dest.mkdir(parents=True, exist_ok=True)
        by_name: dict[str, list[dict[str, Any]]] = {}
        for a in arts:
            if is_result_file(a):
                by_name.setdefault(a["name"].rsplit("/", 1)[-1], []).append(a)
        out = [self.fetch_item(ws, job, _newest(group), dest, base) for base, group in by_name.items()]
        if not any(p.name == "aggregated_output.json" for p in out):
            raise NeedsHuman("aggregated_output.json was listed but not downloaded", ws, job)
        return out

    # ----------------------------------------------------------------- the run
    def run(self, inp: DriverInputs) -> DriverResult:
        inp.out_dir.mkdir(parents=True, exist_ok=True)
        self._cancel = inp.cancel
        ws, job = inp.resume if inp.resume else self._start(inp)
        if inp.resume:                                          # a resumed run is a NEW session with the server: the job's instructions must be loaded again
            self._log("job_resumed", workspaceId=ws, jobId=job)
            try:
                self._call("load_instructions", workspaceId=ws, jobId=job)
            except Exception as e:
                self._log("load_instructions_failed", error=str(e)[:200])
        msgs = self.cfg["messages"]
        proceed = msgs["proceed_custom"] if inp.customised else msgs["proceed"]
        if inp.task_answer and inp.resume:                     # a person's decision on a human task (an approval the job waits on): relay it, then keep waiting
            self.answer_task(ws, job, inp.task_answer[0], inp.task_answer[1])
        elif inp.show_listing and inp.resume:                  # the person asked to see what will run: now (and only now) the configuration is requested, checked, and put to them
            self._log("listing_requested", note="a person asked AWS Transform to list the configuration it will run")
            proposal = self._read_back(ws, job)
            again = dataclasses.replace(inp, approve_now=False, accept_proposal=False, confirmed_externally=False, reply_text="", task_answer=None, show_listing=False)
            self._confirm(ws, job, proposal, again, msgs["proceed_custom"], force_approval=True)
        elif inp.reply_text and inp.resume:                    # a suggestion or question typed in A01: relay it unchanged, then continue the loop
            self._log("suggestion_relayed", text=inp.reply_text[:300])
            answer = self._ask(ws, job, inp.reply_text, "the service's answer to your message")
            self._log("service_replied", text=answer[:400])
            if inp.reply_stage == "scenario":                  # before the go-ahead
                # TURN-TAKING.  The service has answered, and its answer may ask the person something ("anything else you'd like to adjust, or are you ready to proceed?").
                # A01 says NOTHING more until the person acts: it shows the answer; the configuration listing is requested only when the person asks for it (or the person types
                # another message).  (Seen live: A01 sent its own read-back request right after such a question, before the person had answered.)
                raise NeedsHuman("AWS Transform answered your message (shown below) and is waiting for you. Reply in the box, or ask to see the full configuration it will run.",
                                 ws, job, proposal="", kind="approval", send_text=msgs["proceed_custom"], reply=answer, customised=True, needs_listing=True)
        elif inp.confirmed_externally and inp.resume:
            self._log("confirmed_externally", note="a person confirmed the scenario in the AWS Transform web application")
        elif inp.approve_now and inp.resume and inp.answer_question:   # the person answered the service's question (proceed with the current inventory): the service then lists its configuration
            self._log("question_answered_by_person", note="a person chose to proceed with the current inventory; the real configuration is fetched next and put to them")
            seen = self._agent_ids(ws, job)
            self._call("send_message", workspaceId=ws, jobId=job, text=msgs["proceed_as_is"], skipPolling=True)
            proposal = self._await_proposal(ws, job, after=seen)
            again = dataclasses.replace(inp, approve_now=False, accept_proposal=False, confirmed_externally=False, reply_text="", task_answer=None, show_listing=False, answer_question=False)
            self._confirm(ws, job, proposal, again, proceed)
        elif inp.approve_now and inp.resume:                   # a person approved in A01: relay it, then wait for the outputs
            self._log("approved_by_person", note="a person approved in A01; the go-ahead is sent to AWS Transform")
            self._call("send_message", workspaceId=ws, jobId=job, text=proceed, skipPolling=True)
        elif inp.resume and inp.resume_stage == "running":     # paused AFTER the go-ahead (a stuck connection, an expired login): nothing to decide again, keep waiting
            self._log("continuing_to_wait", note="the go-ahead was sent earlier: waiting for the outputs again")
        else:
            proposal = self._await_proposal(ws, job)
            self._confirm(ws, job, proposal, inp, msgs["proceed"])
        arts = self._outputs(ws, job, set(), reask=lambda: self._reconfirm(ws, job, inp, proceed))
        res_dir = inp.out_dir / "pass1"
        self._download(ws, job, arts, res_dir)
        assessment = load_results(res_dir)
        passes, enrichment = 1, None
        if inp.enrich is not None:
            self._call("send_message", workspaceId=ws, jobId=job, text=msgs["enrichment_request"], skipPolling=True)
            tpl_art = self._wait(lambda: next((a for a in self._artifacts(ws, job, outputs_only=True) if a["name"].endswith("server_enrichment.csv")), None),
                                 "the enrichment template", ws, job)
            tpl = inp.out_dir / "enrichment" / "server_enrichment.csv"
            tpl.parent.mkdir(parents=True, exist_ok=True)
            self._call("get_resource", resource="artifact", workspaceId=ws, jobId=job, artifactId=tpl_art["artifactId"],
                       savePath=str(tpl), fileName="server_enrichment.csv")
            filled = inp.out_dir / "enrichment" / "server_enrichment_filled.csv"
            enrichment = inp.enrich(tpl, filled)
            self._call("upload_artifact", workspaceId=ws, jobId=job, content=str(filled), fileName="server_enrichment.csv", fileType="CSV")
            known = {a["artifactId"] for a in self._artifacts(ws, job, outputs_only=True)}
            n0 = self._agent_ids(ws, job)
            self._call("send_message", workspaceId=ws, jobId=job, text=msgs["enrichment_apply"], skipPolling=True)
            self._await_proposal(ws, job, after=n0)             # the service asks: re-run or new scenario?
            n1 = self._agent_ids(ws, job)
            self._call("send_message", workspaceId=ws, jobId=job,
                       text=msgs["rerun"] + " " + guard_mod.settings_text(self.settings), skipPolling=True)
            proposal2 = self._await_proposal(ws, job, after=n1)
            self._confirm(ws, job, proposal2, inp, msgs.get("proceed_enriched", msgs["proceed"]), need_approval=False)   # one approval per run
            arts2 = self._outputs(ws, job, known)
            res_dir = inp.out_dir / "pass2"
            self._download(ws, job, arts2, res_dir)
            assessment = load_results(res_dir)
            passes = 2
        assessment.job = {"workspaceId": ws, "jobId": job, "passes": passes}
        return DriverResult(ws, job, res_dir, assessment, passes, self.events, enrichment)
