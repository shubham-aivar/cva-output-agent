"""Defensive helpers for reading tool responses whose exact shape is not documented.

The AWS Transform MCP tools return JSON (or text containing JSON) whose field names we have only partly seen.
Rather than assume one shape, these helpers search for the likely names and the driver records every raw
response in a trace so a real run can correct any wrong guess."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterator

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def iter_dicts(o: Any) -> Iterator[dict[str, Any]]:
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from iter_dicts(v)
    elif isinstance(o, list):
        for v in o:
            yield from iter_dicts(v)


def find_key(o: Any, keys: tuple[str, ...]) -> Any:
    for d in iter_dicts(o):
        for k in keys:
            if k in d and d[k] not in (None, ""):
                return d[k]
    return None


def find_id(resp: Any, keys: tuple[str, ...]) -> str | None:
    v = find_key(resp, keys)
    if isinstance(v, str) and v:
        return v
    m = _UUID.search(json.dumps(resp, default=str))
    return m.group(0) if m else None


def flatten_text(o: Any) -> str:
    return json.dumps(o, default=str) if not isinstance(o, str) else o


def collect_messages(resp: Any) -> list[dict[str, Any]]:
    """Return message-like dicts (with some text) in time order when a timestamp is present."""
    out = []
    for d in iter_dicts(resp):
        text = next((d[k] for k in ("text", "content", "message", "body") if isinstance(d.get(k), str) and d[k].strip()), None)
        if text:
            who = str(next((d[k] for k in ("role", "sender", "author", "type", "from") if d.get(k)), "")).lower()
            ts = str(next((d[k] for k in ("timestamp", "createdAt", "created_at", "time", "createdTimestamp") if d.get(k)), ""))
            mid = next((d[k] for k in ("messageId", "id") if isinstance(d.get(k), str)), "")
            out.append({"text": text, "who": who, "ts": ts, "id": mid})
    return sorted(out, key=lambda m: m["ts"]) if all(m["ts"] for m in out) else out


def latest_agent_text(resp: Any) -> str | None:
    msgs = [m for m in collect_messages(resp) if "user" not in m["who"] and "human" not in m["who"]]
    return msgs[-1]["text"] if msgs else None


_FILE_EXT = re.compile(r"\.[A-Za-z0-9]{2,5}$")
_ID_KEYS = ("artifactId", "artifact_id", "artifactID", "id", "assetId")
_NAME_KEYS = ("fileName", "file_name", "filename", "name", "artifactName", "title", "path", "key")
_SIZE_KEYS = ("sizeInBytes", "size", "sizeBytes", "size_bytes", "fileSize", "contentLength")


def _size(d: dict[str, Any]) -> Any:
    return next((d[k] for k in _SIZE_KEYS if isinstance(d.get(k), (int, float))), None)


def _file_from_artifact(a: dict[str, Any]) -> dict[str, Any] | None:
    """One entry of ``artifacts[]`` as the Transform API returns it: ``{artifactId, fileMetadata: {path, description}, sizeInBytes,
    artifactType: {categoryType, fileType}, jobId, planStepId, ...}``.  The name is the last part of ``fileMetadata.path``; entries of an
    unknown shape fall back to the usual name keys."""
    aid = next((a[k] for k in _ID_KEYS if isinstance(a.get(k), str) and a[k]), None)
    if not aid:
        return None
    meta = a.get("fileMetadata") if isinstance(a.get("fileMetadata"), dict) else {}
    path = meta.get("path") if isinstance(meta.get("path"), str) and meta.get("path") else ""
    if not path:
        path = next((a[k] for k in _NAME_KEYS if isinstance(a.get(k), str) and a[k]), "")
    if not path:
        path = next((v for v in a.values() if isinstance(v, str) and _FILE_EXT.search(v) and "/" not in v[:1]), "")
    atype = a.get("artifactType") if isinstance(a.get("artifactType"), dict) else {}
    ftype = atype.get("fileType") if isinstance(atype.get("fileType"), str) else None
    name = path.rsplit("/", 1)[-1] if path else aid + ("." + ftype.lower() if ftype and ftype != "OTHER" else "")
    return {"kind": "artifact", "artifactId": aid, "name": name, "path": path, "size": _size(a), "fileType": ftype,
            "category": atype.get("categoryType"), "raw": a}


def parse_listing(resp: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """Files and folders of one ``list_resources(resource="artifacts")`` answer.  The real answer is ``{artifacts: [...], assets: [...], folders:
    ["<prefix>/", ...], connectorId, nextToken}`` (inside ``{"success": true, "data": ...}``).  Files may arrive as ARTIFACTS (id + fileMetadata.path)
    or as ASSETS (assetKey, downloaded with the listing's connectorId); folders are plain strings to be passed verbatim as ``pathPrefix``."""
    files: list[dict[str, Any]] = []
    folders: list[str] = []
    structured = False
    for d in iter_dicts(resp):
        if not any(isinstance(d.get(k), list) for k in ("artifacts", "assets", "folders")):
            continue
        structured = True
        cid = d.get("connectorId") if isinstance(d.get("connectorId"), str) else None
        for a in d.get("artifacts") or []:
            f = _file_from_artifact(a) if isinstance(a, dict) else None
            if f:
                files.append(f)
        for s in d.get("assets") or []:
            key = s.get("assetKey") if isinstance(s, dict) else None
            if isinstance(key, str) and key:
                files.append({"kind": "asset", "artifactId": "asset-" + hashlib.sha1(key.encode()).hexdigest()[:24], "assetKey": key,
                              "connectorId": cid, "name": key.rsplit("/", 1)[-1], "path": key, "size": _size(s), "fileType": None, "category": None, "raw": s})
        for f in d.get("folders") or []:
            if isinstance(f, str) and f.strip():
                folders.append(f if f.endswith("/") else f + "/")
    if not structured:                                         # an answer of an unknown shape: look for id + name pairs and folder-like strings anywhere
        for d in iter_dicts(resp):
            f = _file_from_artifact(d)
            if f and f["path"]:
                files.append(f)
        folders = find_folders(resp)
    return files, list(dict.fromkeys(folders))


def find_artifacts(resp: Any) -> list[dict[str, Any]]:
    """The files of an answer (see ``parse_listing``)."""
    return parse_listing(resp)[0]


def error_of(resp: Any) -> str | None:
    """The service's own error, if the answer is one: ``CODE: message`` from ``{"success": false, "error": {code, message}}``."""
    for d in iter_dicts(resp):
        if d.get("success") is False:
            e = d.get("error")
            if isinstance(e, dict):
                return f"{e.get('code', 'ERROR')}: {e.get('message', '')}"[:240]
            return (str(e) if e else "success=false")[:240]
    return None


_STATE_KEYS = ("jobStatus", "jobState", "status", "state")


def find_state(resp: Any) -> str | None:
    """The job's state word (for example AWAITING_HUMAN_INPUT or EXECUTING), wherever the response keeps it."""
    for d in iter_dicts(resp):
        for k in _STATE_KEYS:
            v = d.get(k)
            if isinstance(v, str) and v and (v.isupper() or "_" in v) and len(v) < 40:
                return v
    return None


def message_ids(resp: Any) -> list[str]:
    """Message ids when a listing returns summaries without their text (get_resource can then fetch the text)."""
    ids: list[str] = []
    for d in iter_dicts(resp):
        mid = d.get("messageId") or (d.get("id") if any(k in d for k in ("timestamp", "createdAt", "sender", "role", "createdTimestamp")) else None)
        if isinstance(mid, str) and mid and mid not in ids:
            ids.append(mid)
    return ids


_TEXT_KEYS = ("text", "content", "message", "body")
_ENVELOPE = ("success", "data")


def unwrap(o: Any, key: str = "", depth: int = 0) -> Any:
    """The service wraps its answers in JSON that arrives as a STRING (seen in a live run: a single "message" whose text was the
    whole reply, {"success": true, "data": {"messages": [...]}}).  Decode such strings so messages, ids and states are found
    individually.  A genuine message whose text merely looks like JSON is left alone: under message-text keys only an envelope
    (with "success" or "data") is unwrapped."""
    if depth > 6:
        return o
    if isinstance(o, dict):
        return {k: unwrap(v, k, depth + 1) for k, v in o.items()}
    if isinstance(o, list):
        return [unwrap(v, key, depth + 1) for v in o]
    if isinstance(o, str):
        t = o.strip()
        if 2 < len(t) < 3_000_000 and t[0] in "{[" and t[-1] in "}]":
            try:
                parsed = json.loads(t)
            except ValueError:
                return o
            if key in _TEXT_KEYS and not (isinstance(parsed, dict) and any(k in parsed for k in _ENVELOPE)):
                return o
            return unwrap(parsed, key, depth + 1)
    return o


def find_folders(resp: Any) -> list[str]:
    """Folder paths in a listing, however it names them: bare strings ending in "/", or entries typed folder/directory/prefix.  Seen live
    (a manual run): a listing of a job's artifacts returned FOLDERS ("Generated Outputs/", "User Uploads/") and no files, which must be opened
    one by one with ``pathPrefix``."""
    out: list[str] = []

    def add(p: str) -> None:
        p = p.strip()
        if 1 < len(p) < 500 and p.endswith("/") and "://" not in p and p not in out:
            out.append(p)

    def walk(o: Any) -> None:
        if isinstance(o, dict):
            flagged = any(o.get(k) is True for k in ("isFolder", "is_folder", "isDirectory", "is_directory", "folder", "directory"))
            if flagged or str(o.get("type") or o.get("kind") or "").lower() in ("folder", "directory", "prefix", "dir"):
                for k in ("path", "prefix", "name", "key", "folder"):
                    v = o.get(k)
                    if isinstance(v, str) and v:
                        add(v if v.endswith("/") else v + "/")
                        break
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
        elif isinstance(o, str):
            add(o)
    walk(resp)
    return out


NOT_CONNECTED_HELP = ("AWS Transform is not connected from the MCP server A01 starts (it answered NOT_CONFIGURED). That server signs in with your AWS credentials "
                      "(the profile in the terminal that started A01) or with a saved sign-in. Usual causes: the AWS login expired, AWS_PROFILE and AWS_REGION are not set "
                      "in that terminal, or the server's 5-second start-up check timed out (A01 already retries that). In that terminal run 'aws sts get-caller-identity' "
                      "(it must show the admin role; if not, 'aws sso login --profile aivar-vision-admin' and export AWS_PROFILE and AWS_REGION), restart A01 from it, then "
                      "click 'Check the connection first'. Only if that fails is a saved sign-in needed (for example Claude Code: 'Configure AWS Transform with SSO'). "
                      "A01 never signs in for you.")


NOT_CONNECTED_SHORT = ("The server is not signed in to AWS Transform (it answered NOT_CONFIGURED). Check your AWS login in A01's terminal ('aws sts get-caller-identity'; "
                       "if needed 'aws sso login --profile aivar-vision-admin'), restart A01 from it and check again; only then a saved sign-in (for example Claude Code: "
                       "'Configure AWS Transform with SSO').")


def not_connected(o: Any) -> bool:
    """True if an answer says the MCP server is not signed in to AWS Transform (code NOT_CONFIGURED)."""
    blob = json.dumps(o, default=str)
    return "NOT_CONFIGURED" in blob or "Not connected to AWS Transform" in blob


EXPIRED_HELP = ("AWS Transform signed A01's MCP server out in the middle of the run (it answered INVALID_SESSION, an expired token or a denied session). The sign-in "
                "lasts a limited time: a session cookie or an SSO token expires. The job keeps running in AWS. Sign in again once (for example Claude Code: 'Configure "
                "AWS Transform with SSO', or with cookie auth), click 'Check the connection first', then 'Fetch the results now and fill the other tabs'.")
_AUTH = re.compile(r"INVALID_SESSION|ExpiredToken|Token (is |has )?expired|Session (is |has )?expired|expired (session|token)|UnauthorizedException|"
                   r"AccessDeniedException|Not authorized|UNAUTHENTICATED", re.I)


def auth_problem(o: Any) -> bool:
    """True if an ERROR answer (or an exception text) says the sign-in expired or was refused.  Only error-shaped answers count, so a chat message that
    happens to mention a session is never mistaken for one."""
    if isinstance(o, str):
        return bool(_AUTH.search(o))
    text = error_of(o) or ""
    if not text and any(d.get("isError") is True for d in iter_dicts(o)):
        text = json.dumps(o, default=str)
    return bool(text and _AUTH.search(text))


TERMINAL_TASK = {"CANCELLED", "CLOSED", "CLOSED_PENDING_NEXT_TASK", "DELIVERED"}      # the server's own list of finished human-task states


def _slim_task(t: dict[str, Any]) -> dict[str, Any]:
    return {"taskId": t.get("taskId"), "title": str(t.get("title") or "(untitled task)")[:200], "description": str(t.get("description") or "")[:1500],
            "severity": t.get("severity"), "category": t.get("category"), "status": t.get("status"), "blockingType": t.get("blockingType"),
            "createdAt": str(t.get("createdAt") or "")}


def parse_snapshot(resp: Any) -> dict[str, Any]:
    """The answer of ``get_job_status(detailed=true)``: ``{job: {status, ...}, worklogs, tasks: {hitlTasks: [...]}, messages: {messages: [...]}, plan,
    _pollingGuidance: {isTerminal, jobStatus, hasPendingTasks, pendingTaskCount}}``.  Returns the job status (CREATED, STARTING, ASSESSING, PLANNING, PLANNED,
    EXECUTING, AWAITING_HUMAN_INPUT, COMPLETED, FAILED, STOPPING, STOPPED), the human tasks still waiting for a person, and the recent messages."""
    data = next((d for d in iter_dicts(resp) if isinstance(d.get("job"), dict) or isinstance(d.get("_pollingGuidance"), dict)), None)
    status: str | None = None
    failure: str | None = None
    tasks: list[dict[str, Any]] = []
    messages: Any = None
    terminal = False
    if data is not None:
        job = data.get("job") if isinstance(data.get("job"), dict) else {}
        # The REAL job record keeps its status in ``statusDetails.status`` (+ ``failureReason``); it has no top-level ``status``.  The server's own
        # ``_pollingGuidance.jobStatus`` reads that missing field, so it is ALWAYS "UNKNOWN" (and isTerminal always false): never trust it.
        sd = job.get("statusDetails") if isinstance(job.get("statusDetails"), dict) else {}
        status = next((v for v in (sd.get("status"), job.get("status")) if isinstance(v, str) and v and v.upper() != "UNKNOWN"), None)
        failure = sd.get("failureReason") if isinstance(sd.get("failureReason"), str) else None
        terminal = status in ("COMPLETED", "FAILED", "STOPPED")
        t = data.get("tasks")
        lst = t.get("hitlTasks") if isinstance(t, dict) else (t if isinstance(t, list) else [])
        tasks = [_slim_task(x) for x in (lst or []) if isinstance(x, dict) and x.get("status") not in TERMINAL_TASK and x.get("taskId")]
        messages = data.get("messages")
    if not status and data is not None and isinstance(data.get("job"), dict):
        status = find_state(data["job"])                      # an unexpected layout of the job record: look inside it, never in the tasks
    if status and status.upper() == "UNKNOWN":
        status = None
    return {"status": status, "failure": failure, "tasks": tasks, "messages": messages, "terminal": terminal, "found": data is not None}
