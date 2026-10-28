"""Is the person's AWS login alive, and which role does it use?  One read-only call of the AWS CLI: ``aws sts get-caller-identity``.

Why A01 does this itself.  Read from the AWS Transform MCP server's source: it builds a FRESH botocore session for EVERY call and resolves the profile's credentials in that call's
worker thread (the "Loading cached SSO token" line appears on every call), so every call depends on the SSO login (the access token in ~/.aws/sso/cache) being valid and on one more
network step to the SSO service.  A step that stalls has a 60-second timeout with 3 retries: one call can take over four minutes, while A01 only sees "timeout".  Seen live: the
file listing answered at 17:24:42 and every call stalled from about 17:25 (about 45 minutes after the login), with the files already created.
The AWS CLI refreshes an SSO login that is near its end and writes the refreshed token to that shared cache, so one CLI call every few minutes (a short, separate process with a
timeout: a stall costs A01 seconds, never the run) keeps the login fresh for the server's own lookups, and an expired login is reported at once, in plain words, before anything hangs.
This is a mitigation of the most likely cause, not a proof: the server's own log (~/.aws-transform-mcp/server.log) is in the diagnostics file for the day it is not enough.

Nothing secret is read or kept: only the account, the role name and whether the call worked."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from typing import Any, Callable

EXPIRED = re.compile(r"expired|sso login|invalid.{0,30}token|token.{0,30}invalid|refresh.{0,30}(fail|invalid)|unauthorized|session associated", re.I)
NO_CREDENTIALS = re.compile(r"unable to locate credentials|could not be found|profile .{0,60}not found|no credentials|partial credentials", re.I)
NETWORK = re.compile(r"could not connect|connect timeout|connection (was )?(closed|reset|aborted)|name or service not known|nodename nor servname|proxy|ssl|timed out|timeout", re.I)


def login_help(profile: str | None, kind: str, detail: str = "") -> str:
    """What to do, in the person's own words, for each way the login can fail."""
    p = profile or "aivar-vision-admin"
    if kind == "expired":
        return (f"Your AWS login has expired or cannot be refreshed. In the terminal that started A01 run: aws sso login --profile {p}  (your browser opens: approve it), "
                "then click 'Check the connection again'. The job keeps running in AWS; afterwards use 'Fetch the results now and fill the other tabs' if A01 had not finished.")
    if kind == "no_credentials":
        return (f"The AWS CLI finds no credentials for profile '{p}'. In the terminal that started A01 run: export AWS_PROFILE={p} AWS_REGION=us-east-1 and "
                f"aws sso login --profile {p}, then start A01 again from that terminal.")
    if kind == "timeout" or kind == "network":
        return ("The AWS CLI could not reach AWS in time (network, VPN or proxy). If your network needs a proxy set HTTPS_PROXY in the terminal that starts A01. "
                f"Detail: {detail}")
    return f"The AWS CLI check failed: {detail}"


def check_login(profile: str | None = None, region: str | None = None, timeout: float = 25.0,
                run: Callable[..., Any] = subprocess.run, which: Callable[[str], str | None] = shutil.which) -> dict[str, Any]:
    """Run the check.  ``ok`` is True (alive), False (a problem: see ``kind``) or None (not checked: the CLI is missing, or the check is switched off with CVA_LOGIN_CHECK=off)."""
    if os.environ.get("CVA_LOGIN_CHECK", "").strip().lower() in ("off", "0", "no", "false"):
        return {"ok": None, "kind": "disabled", "detail": "the login check is switched off (CVA_LOGIN_CHECK=off)"}
    aws = which("aws")
    if not aws:
        return {"ok": None, "kind": "no_cli", "detail": "the AWS CLI is not installed or not on the PATH, so A01 cannot check or keep your login warm"}
    cmd = [aws, "sts", "get-caller-identity", "--output", "json"] + (["--profile", profile] if profile else []) + (["--region", region] if region else [])
    t0 = time.monotonic()
    try:
        p = run(cmd, capture_output=True, text=True, timeout=timeout, env=os.environ.copy())
    except subprocess.TimeoutExpired:
        return {"ok": False, "kind": "timeout", "detail": f"the AWS CLI did not answer within {timeout:.0f} s", "seconds": round(timeout, 1)}
    except OSError as e:
        return {"ok": None, "kind": "no_cli", "detail": f"the AWS CLI could not be started: {e}"}
    secs = round(time.monotonic() - t0, 1)
    if p.returncode == 0:
        try:
            d = json.loads(p.stdout)
        except ValueError:
            return {"ok": False, "kind": "error", "detail": "the AWS CLI answered with something unreadable", "seconds": secs}
        arn = str(d.get("Arn", ""))
        m = re.search(r"assumed-role/([^/]+)/", arn)
        return {"ok": True, "kind": "ok", "account": d.get("Account"), "role": m.group(1) if m else arn.rsplit("/", 1)[-1], "seconds": secs}
    err = " ".join((p.stderr or "").split())[-300:]
    kind = "expired" if EXPIRED.search(err) else "no_credentials" if NO_CREDENTIALS.search(err) else "network" if NETWORK.search(err) else "error"
    return {"ok": False, "kind": kind, "detail": err, "seconds": secs}
