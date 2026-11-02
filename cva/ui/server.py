"""Demo UI: a JSON API over ``Service`` plus one static page.  Standard library only.

Locally it binds to 127.0.0.1 with no sign-in (one person on their own laptop).  Hosted (``--host 0.0.0.0``) it REFUSES to start
unless sign-in is configured (``CVA_AUTH_FILE``, see ``auth.py``): username and password, a signed session cookie, and an origin
secret so only CloudFront can reach it.  Uploads are size-limited, file names are sanitised and downloads are confined to a run's
``out`` folder.  A request that is still running after ``KEEPALIVE_FIRST`` seconds answers 200 and trickles spaces until the JSON is
ready, so a proxy with a 60-second read timeout (CloudFront) never cuts a slow action (export, fetch results, pre-flight).
"""
from __future__ import annotations

import base64
import json
import mimetypes
import re
import shutil
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from ..config import project_root
from ..ingest.detect import detect_kind
from ..service import Service, ServiceError
from .auth import Auth, login_page

STATIC = Path(__file__).parent / "static"
MAX_BODY = 40 * 1024 * 1024
JOBS: dict[str, dict[str, Any]] = {}      # run id -> background AWS Transform job state (in memory)
KEEPALIVE_FIRST = 8.0                     # seconds a request may take before the keep-alive stream starts
KEEPALIVE_EVERY = 10.0                    # then one space this often (well inside CloudFront's 60 s origin read timeout)


def _safe_name(n: str) -> str:
    n = Path(str(n)).name
    return re.sub(r"[^A-Za-z0-9._ -]", "_", n)[:120] or "file"


def list_samples() -> list[dict[str, Any]]:
    out = []
    root = project_root() / "samples"
    for sub in ("synthetic", "real_format"):
        d = root / sub
        if not d.exists():
            continue
        for p in sorted(d.iterdir()):
            if p.suffix.lower() in (".xlsx", ".json") or p.is_dir():
                kind = None
                if p.suffix.lower() == ".xlsx":
                    try:
                        kind = detect_kind(p)
                    except Exception:
                        kind = None
                role = ("notes" if p.is_dir() else "licensing" if ("licensing" in p.name.lower()) else
                        "transform" if "transform" in p.name.lower() else "estate" if kind else "other")
                out.append({"name": p.name, "group": sub, "kind": kind, "role": role})
    return out


class Handler(BaseHTTPRequestHandler):
    svc: Service
    demo_samples: bool = False          # bundled sample files are offered ONLY when the UI is started with --demo-samples
    server_version = "cva-ui"
    protocol_version = "HTTP/1.0"       # one request per connection: the keep-alive stream ends by closing it
    auth: Auth | None = None            # None = no sign-in (local use and tests)

    def log_message(self, *a: Any) -> None:  # quiet
        pass

    # ------------------------------------------------------------------ sign-in gate
    def _https(self) -> bool:
        return "https" in (self.headers.get("CloudFront-Forwarded-Proto", "") + self.headers.get("X-Forwarded-Proto", "")).lower()

    def _who_addr(self) -> str:
        a = self.auth
        xff = (self.headers.get("X-Forwarded-For") or "").split(",")[-1].strip()      # CloudFront APPENDS the viewer's address: the last entry is the one it saw, the first can be forged
        return xff if (a and a.origin_secret and xff) else self.client_address[0]     # X-Forwarded-For is trusted only behind the origin secret

    def _gate(self, is_page: bool) -> str | None:
        """None = stop (the answer was sent); otherwise the signed-in user's name (\"\" when sign-in is off)."""
        a = self.auth
        if a is None:
            return ""
        if not a.origin_ok(self.headers.get("X-Origin-Verify")):
            self._json({"error": "forbidden"}, 403)
            return None
        user = a.user_from(self.headers.get("Cookie"))
        if user:
            return user
        if is_page:
            self._send(302, b"", "text/plain", {"Location": "/login"})
        else:
            self._json({"error": "sign in required"}, 401)
        return None

    def _login_post(self) -> None:
        a = self.auth
        if a is None:
            return self._send(302, b"", "text/plain", {"Location": "/"})
        if not a.origin_ok(self.headers.get("X-Origin-Verify")):
            return self._json({"error": "forbidden"}, 403)
        n = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(min(n, 4096)).decode("utf-8", "replace")) if n else {}
        who = self._who_addr()
        r = a.attempt(who, (form.get("username") or [""])[0], (form.get("password") or [""])[0])
        if r == "ok":
            return self._send(302, b"", "text/plain", {"Location": "/", "Set-Cookie": a.make_cookie(a.username, self._https())})
        msg = "Too many attempts. Try again in a few minutes." if r == "locked" else "Wrong username or password."
        self._send(429 if r == "locked" else 401, login_page(msg), "text/html; charset=utf-8")

    def _respond_slow(self, fn: Any) -> None:
        """Run ``fn`` in a worker.  Quick (under KEEPALIVE_FIRST): a normal answer with its real status.  Slow: commit 200, send a space every
        KEEPALIVE_EVERY seconds, then the JSON (an error is {\"error\": ...}, which the page already shows)."""
        box: dict[str, Any] = {}

        def work() -> None:
            try:
                box["ok"] = fn()
            except ServiceError as e:
                box["err"] = (400, str(e))
            except Exception as e:
                box["err"] = (500, f"{type(e).__name__}: {e}")
        t = threading.Thread(target=work, daemon=True)
        t.start()
        t.join(KEEPALIVE_FIRST)
        if not t.is_alive():
            return self._json({"error": box["err"][1]}, box["err"][0]) if "err" in box else self._json(box.get("ok"))
        try:
            self.send_response(200)
            for k, v in (("Content-Type", "application/json"), ("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"), ("Connection", "close")):
                self.send_header(k, v)
            self.end_headers()
            while t.is_alive():
                self.wfile.write(b" ")
                self.wfile.flush()
                t.join(KEEPALIVE_EVERY)
            self.wfile.write(json.dumps({"error": box["err"][1]} if "err" in box else box.get("ok"), default=str).encode("utf-8"))
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass                                                   # the browser went away; the action still finished

    # ------------------------------------------------------------------ helpers
    def _send(self, code: int, body: bytes, ctype: str = "application/json", extra: dict[str, str] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: Any, code: int = 200) -> None:
        self._send(code, json.dumps(obj, default=str).encode("utf-8"))

    def _body(self) -> dict[str, Any]:
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ServiceError("upload too large (limit 40 MB)")
        raw = self.rfile.read(n) if n else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    def _write_upload(self, folder: Path, item: dict[str, Any]) -> str:
        folder.mkdir(parents=True, exist_ok=True)
        p = folder / _safe_name(item["name"])
        p.write_bytes(base64.b64decode(item["b64"]))
        return str(p)

    # ------------------------------------------------------------------ routing
    def do_GET(self) -> None:
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)
        try:
            if path == "/health":                                   # no sign-in: answers only that the process is up
                return self._json({"ok": True})
            if path == "/login":
                if self.auth is None:
                    return self._send(302, b"", "text/plain", {"Location": "/"})
                if not self.auth.origin_ok(self.headers.get("X-Origin-Verify")):
                    return self._json({"error": "forbidden"}, 403)
                return self._send(200, login_page(), "text/html; charset=utf-8")
            user = self._gate(is_page=path in ("/", "/index.html"))
            if user is None:
                return
            if path in ("/", "/index.html"):
                return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            if path == "/api/app":
                return self._json({"demo_samples": self.demo_samples} | ({"user": user, "signed_in": True} if self.auth is not None else {}))
            if path == "/api/samples":
                return self._json(list_samples() if self.demo_samples else [])
            if path == "/api/runs":
                return self._json({"runs": self.svc.list_runs(), "portfolio": self.svc.portfolio_kpis()})
            m = re.fullmatch(r"/download/(run-\d+)/artifact/([0-9A-Za-z-]{8,64})", path)
            if m:                                                    # one artifact fetched from the AWS Transform job (looked up by id, never by path)
                ent = (self.svc.store.state(m.group(1)).get("transform_artifact_index") or {}).get(m.group(2))
                base = (self.svc.store.path(m.group(1)) / "out" / "transform_artifacts" / "files").resolve()
                f = (base / ent["path"]).resolve() if ent else None
                if not f or base not in f.parents or not f.is_file():
                    return self._json({"error": "file not found"}, 404)
                return self._send(200, f.read_bytes(), mimetypes.guess_type(f.name)[0] or "application/octet-stream",
                                  {"Content-Disposition": f'attachment; filename="{f.name}"'})
            m = re.fullmatch(r"/download/(run-\d+)/transform/([^/]+)", path)
            if m:
                f = self.svc.store.path(m.group(1)) / "out" / "transform" / _safe_name(unquote(m.group(2)))
                if not f.is_file():
                    return self._json({"error": "file not found"}, 404)
                return self._send(200, f.read_bytes(), mimetypes.guess_type(f.name)[0] or "application/octet-stream",
                                  {"Content-Disposition": f'attachment; filename="{f.name}"'})
            m = re.fullmatch(r"/download/(run-\d+)/([^/]+)", path)
            if m:
                rid, fn = m.group(1), _safe_name(unquote(m.group(2)))
                f = self.svc.store.path(rid) / "out" / fn
                if not f.is_file():
                    return self._json({"error": "file not found"}, 404)
                ctype = mimetypes.guess_type(fn)[0] or "application/octet-stream"
                return self._send(200, f.read_bytes(), ctype, {"Content-Disposition": f'attachment; filename="{fn}"'})
            m = re.fullmatch(r"/api/run/(run-\d+)/(\w+)", path)
            if m:
                rid, what = m.groups()
                s = self.svc
                table = {"overview": lambda: s.overview(rid), "workloads": lambda: s.workloads(rid), "services": lambda: s.service_catalog(rid),
                         "issues": lambda: s.issues(rid), "licensing": lambda: s.licensing(rid),
                         "parity": lambda: s.parity(rid), "figures": lambda: s.figures(rid), "gate": lambda: s.gate(rid),
                         "proposals": lambda: s.proposals(rid), "audit": lambda: s.audit_view(rid),
                         "kpis": lambda: s.kpis(rid), "profile": lambda: s.file_profile(rid),
                         "workload": lambda: s.workload(rid, (q.get("name") or [""])[0]),
                         "outputs": lambda: self._outputs(rid), "transcripts": lambda: self._transcripts(rid, q),
                         "transform": lambda: self._transform(rid)}
                if what not in table:
                    return self._json({"error": "unknown view"}, 404)
                return self._json(table[what]())
            self._json({"error": "not found"}, 404)
        except ServiceError as e:
            self._json({"error": str(e)}, 400)
        except Exception as e:  # keep the server alive; the message is shown in the UI
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/login":
            return self._login_post()
        try:
            if self._gate(is_page=False) is None:
                return
            if path == "/logout":
                a = self.auth
                return self._send(302, b"", "text/plain", {"Location": "/login", "Set-Cookie": a.clear_cookie(self._https()) if a else ""})
            if self.auth is not None and "application/json" not in (self.headers.get("Content-Type") or ""):
                return self._json({"error": "unsupported content type"}, 415)       # a cross-site form cannot send JSON: with SameSite=Lax this blocks CSRF
            b = self._body()
        except ServiceError as e:
            return self._json({"error": str(e)}, 400)
        except Exception as e:
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)
        slow_nolock = path.endswith(("/transform_artifacts_list", "/transform_artifacts_fetch", "/transform_fetch_results", "/transform_preflight", "/transform_sync"))

        def run() -> Any:
            if slow_nolock:
                return self._post(path, b)          # these wait on AWS Transform: they must not freeze the rest of the page (they lock state themselves)
            with self.svc.lock:
                return self._post(path, b)
        self._respond_slow(run)

    def _post(self, path: str, b: dict[str, Any]) -> Any:
        s = self.svc
        if path == "/api/transform_preflight":
            return s.transform_preflight()
        if path == "/api/runs":
            if (b.get("samples") or b.get("licensing_sample") or b.get("notes_sample")) and not self.demo_samples:
                raise ServiceError("built-in sample files are switched off: upload your own files "
                                   "(start the UI with --demo-samples to try the samples)")
            tmp = s.store.base / "_uploads" / uuid.uuid4().hex[:10]
            root = project_root() / "samples"
            files: list[str] = []
            for it in b.get("files", []):
                files.append(self._write_upload(tmp, it))
            for nm in b.get("samples", []):
                p = next((x for x in (root / "synthetic" / nm, root / "real_format" / nm) if x.exists()), None)
                if p is None:
                    raise ServiceError(f"unknown sample '{nm}'")
                files.append(str(p))
            if not files:
                raise ServiceError("choose the estate file(s) to upload first")
            lic = None
            if b.get("licensing"):
                lic = self._write_upload(tmp, b["licensing"])
            elif b.get("licensing_sample"):
                lic = next((str(x) for x in (root / "synthetic" / b["licensing_sample"],) if x.exists()), None)
            notes = None
            if b.get("notes"):
                nd = tmp / "notes"
                for it in b["notes"]:
                    self._write_upload(nd, it)
                notes = str(nd)
            elif b.get("notes_sample"):
                cand = root / "synthetic" / b["notes_sample"]
                notes = str(cand) if cand.exists() else None
            try:
                rid = s.create_run(files, lic, notes, b.get("label", ""), b.get("collection_days") or None, None,
                                   b.get("customer") or None)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            return {"run_id": rid}
        m = re.fullmatch(r"/api/run/(run-\d+)/(\w+)", path)
        if not m:
            raise ServiceError("unknown action")
        rid, act = m.groups()
        by = str(b.get("by", "")).strip()
        if act == "rename":
            return {"name": s.rename_run(rid, str(b.get("name", "")), by)}
        if act == "agent":
            return s.run_agent(rid, b.get("task") or None, b.get("mode", "stub"), actor=by or "agent")
        if act == "decide":
            return s.decide_proposal(rid, b["id"], b["decision"], by, b.get("note", ""), b.get("edited_text") or None)
        if act == "ack":
            s.ack_issue(rid, b["code"], by, b.get("note", ""), b.get("workload") or None)
            return {"ok": True}
        if act == "ack_parity":
            s.ack_parity(rid, by, b.get("note", ""))
            return {"ok": True}
        if act == "ack_licensing":
            s.ack_licensing(rid, by, b.get("note", ""))
            return {"ok": True}
        if act == "confirm":
            return s.confirm(rid, by)
        if act == "finalise":
            return s.finalise(rid, by, int(b.get("rework_rounds", 0)))
        if act == "reopen":
            s.reopen(rid, by, b.get("reason", ""))
            return {"ok": True}
        if act == "hours":
            s.log_hours(rid, float(b["hours"]), by, b.get("note", ""))
            return {"ok": True}
        if act == "export":
            return s.export(rid, b.get("template") or None, bool(b.get("pdf", True)))
        if act == "run_estimate":
            return s.run_offline_estimate(rid, by)
        if act == "ack_estimate":
            s.ack_estimate(rid, by, b.get("note", ""))
            return {"ok": True}
        if act in ("transform_start", "transform_resume", "transform_suggest", "transform_task", "transform_listing"):
            return self._start_transform(rid, by, b, resume=(act != "transform_start"), suggest=(act == "transform_suggest"), task=(act == "transform_task"),
                                         listing=(act == "transform_listing"))
        if act == "transform_artifacts_list":
            return s.transform_artifacts_list(rid)
        if act == "transform_artifacts_fetch":
            return s.transform_artifacts_fetch(rid, b.get("ids") or [], bool(b.get("all")), by or "ui")
        if act == "transform_sync":
            return s.transform_sync(rid)
        if act == "transform_fetch_results":
            return s.transform_fetch_results(rid, by)
        if act == "transform_diagnostics":
            return s.transform_diagnostics(rid)
        if act == "transform_say":
            return s.transform_say(rid, b.get("text", ""), by)
        if act == "transform_cancel":
            j = JOBS.get(rid)
            if not j or j.get("status") != "running":
                raise ServiceError("no AWS Transform run is in progress")
            j["cancel"].set()
            s.audit(rid).append("transform_stop_waiting_requested", {}, by or "ui")
            return {"stopping": True}
        if act == "import_results":
            tmp = s.store.base / "_uploads" / uuid.uuid4().hex[:10]
            try:
                if b.get("sample"):
                    if not self.demo_samples:
                        raise ServiceError("built-in sample files are switched off (start the UI with --demo-samples to use them)")
                    p = project_root() / "samples" / "real_format" / _safe_name(b["sample"])
                    if not p.exists():
                        raise ServiceError("unknown sample")
                    return s.import_transform_results(rid, str(p), by or "ui")
                if b.get("files"):                                # several files at once (aggregated_output.json, error_file.csv, the zip)
                    for it in b["files"]:
                        self._write_upload(tmp, it)
                    return s.import_transform_results(rid, str(tmp), by or "ui")
                return s.import_transform_results(rid, self._write_upload(tmp, b["file"]), by or "ui")
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        if act == "transform_package":
            return s.transform_package(rid)
        if act == "import_transform":
            tmp = s.store.base / "_uploads" / uuid.uuid4().hex[:10]
            try:
                p = self._write_upload(tmp, b["file"])
                return s.import_transform(rid, p, by)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        raise ServiceError("unknown action")

    def _transform(self, rid: str) -> dict[str, Any]:
        # Snapshot the job FIRST: the job saves its state before it flips its status, so a view built afterwards always contains
        # whatever a finished job saved (reading in the other order could show "needs a person" without the pending panel).
        j = JOBS.get(rid)
        job = ({"status": j["status"], "events": list(j["events"][-40:]), "started": j["started"], "message": j.get("message", "")}
               if j else {"status": "idle", "events": [], "message": ""})
        v = self.svc.transform_view(rid)
        v["job"] = job
        v["roadmap"] = self.svc.transform_roadmap(rid, job["status"], job.get("message", ""))
        return v

    def _start_transform(self, rid: str, by: str, b: dict[str, Any], resume: bool, suggest: bool = False, task: bool = False, listing: bool = False) -> dict[str, Any]:
        if not by:
            raise ServiceError("say who is starting this (your name is recorded)")
        if JOBS.get(rid, {}).get("status") == "running":
            raise ServiceError("an AWS Transform run is already in progress for this run")
        cancel = threading.Event()
        kwargs: dict[str, Any] = {"enrich": bool(b.get("enrich")), "allow_unlisted": bool(b.get("allow_unlisted")), "by": by, "cancel": cancel,
                                  "confirmed_externally": bool(b.get("confirmed_externally")), "approve_now": bool(b.get("approve")),
                                  "require_approval": bool(b.get("require_approval", True))}
        if resume:
            pend = self.svc.store.state(rid).get("transform_pending") or {}
            if not (pend.get("workspaceId") and pend.get("jobId")):
                raise ServiceError("there is no paused AWS Transform job to resume")
            kwargs.update({"resume": (pend["workspaceId"], pend["jobId"]), "accept_proposal": bool(b.get("accept_proposal")),
                           "customised": bool(pend.get("customised")), "resume_stage": pend.get("stage", "scenario"),
                           "answer_question": bool(pend.get("question")) and bool(b.get("approve"))})
            if suggest:
                text = str(b.get("text", "")).strip()
                if not text or len(text) > 2000:
                    raise ServiceError("type a suggestion or question of 1 to 2000 characters")
                kwargs.update({"reply_text": text, "reply_stage": pend.get("stage", "scenario")})
            if listing:                                       # the person asks to see the exact configuration the service will run (A01 sent nothing before this)
                kwargs["show_listing"] = True
            if task:                                          # a person's decision on a human task the job is waiting on: relayed, then the wait goes on
                tid, decision = str(b.get("taskId", "")), str(b.get("decision", "")).lower()
                tasks = {t.get("taskId"): t for t in (pend.get("tasks") or [])}
                if decision not in ("approve", "reject"):
                    raise ServiceError("choose approve or reject")
                if tid not in tasks:
                    raise ServiceError("that task is not one of the approvals the job is waiting on: use Check again")
                kwargs["task_answer"] = (tid, decision.upper())
                kwargs["task_title"] = tasks[tid].get("title", "")
        job: dict[str, Any] = {"status": "running", "events": [], "started": time.time(), "message": "", "cancel": cancel}
        JOBS[rid] = job
        svc = self.svc
        if suggest:
            svc.audit(rid).append("transform_suggestion_sent", {"text": kwargs["reply_text"][:500], "stage": kwargs["reply_stage"]}, by)
        if listing:
            svc.audit(rid).append("transform_listing_requested", {"stage": (svc.store.state(rid).get("transform_pending") or {}).get("stage", "scenario")}, by)
        if task:
            svc.audit(rid).append("transform_task_decision", {"taskId": kwargs["task_answer"][0], "title": kwargs.pop("task_title", ""), "decision": kwargs["task_answer"][1]}, by)
        svc.audit(rid).append("transform_started_from_ui", {"resume": resume, "accept_proposal": bool(b.get("accept_proposal")),
                                                             "confirmed_externally": kwargs["confirmed_externally"], "enrich": kwargs["enrich"]}, by)

        def work() -> None:
            try:
                svc.transform_run(rid, progress=lambda m: job["events"].append(f"{time.strftime('%H:%M:%S')}  {m}"), **kwargs)
                job["status"], job["message"] = "done", "AWS Transform assessment imported."
            except ServiceError as e:
                job["status"] = "needs_person" if "needs a person" in str(e) else "error"
                job["message"] = str(e).split(" Resume with:")[0]
            except Exception as e:  # keep the UI informed instead of dying silently
                job["status"], job["message"] = "error", f"{type(e).__name__}: {e}"

        threading.Thread(target=work, daemon=True).start()
        return {"started": True}

    def _outputs(self, rid: str) -> dict[str, Any]:
        out = self.svc.store.path(rid) / "out"
        files = sorted(p.name for p in out.iterdir() if p.is_file()) if out.exists() else []
        return {"files": files, "transform": self.svc.store.state(rid)["transform"] | {"per_workload": None}}

    def _transcripts(self, rid: str, q: dict[str, list[str]]) -> Any:
        d = self.svc.store.path(rid) / "agent"
        name = (q.get("name") or [""])[0]
        if name:
            f = d / _safe_name(name)
            return json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {"error": "not found"}
        return sorted(p.name for p in d.glob("*.json")) if d.exists() else []


def serve(svc: Service, port: int = 8765, demo_samples: bool = False, host: str = "127.0.0.1") -> None:
    auth = Auth.from_env()
    if host not in ("127.0.0.1", "localhost", "::1") and auth is None:
        raise SystemExit("refusing to listen on " + host + " without sign-in: set CVA_AUTH_FILE (see cva/ui/auth.py)")
    Handler.svc = svc
    Handler.demo_samples = demo_samples
    Handler.auth = auth
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"CVA agent UI: http://{host}:{port}   (" + ("sign-in required" if auth else "local only; no sign-in") + "; Ctrl+C to stop)")
    print("Mode: " + ("DEMO: bundled sample files are offered" if demo_samples else "your files only: nothing is pre-loaded; upload to begin"))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        srv.server_close()
