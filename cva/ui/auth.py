"""Username and password sign-in for the hosted demo (standard library only; no MFA by design).

Switched on only when ``CVA_AUTH_FILE`` points to a JSON file::

    {"username": "...", "password_hash": "pbkdf2_sha256$<iterations>$<salt hex>$<hash hex>",
     "session_key": "<random hex>", "origin_secret": "<random hex, optional>"}

* The password is never stored: only a salted PBKDF2-SHA256 hash.
* A session is a signed cookie (``user|expiry`` + HMAC-SHA256 with ``session_key``): HttpOnly, SameSite=Lax, Secure when hosted.
* ``origin_secret``: when set, every request must carry it in the ``X-Origin-Verify`` header (CloudFront adds it), so the origin
  refuses anyone who reaches it directly.
* Failed sign-ins are slowed down and, after ``MAX_FAILS`` from one address inside ``WINDOW`` seconds, refused for ``LOCK`` seconds.

Make a hash:  ``python -m cva.ui.auth hash``  (prompts; ``--stdin`` reads the password from standard input for scripts).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import threading
import time
from html import escape
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any

COOKIE = "cva_session"
SESSION_SECONDS = 8 * 3600
ITERATIONS = 600_000
MAX_FAILS, WINDOW, LOCK = 5, 300.0, 300.0
_sleep = time.sleep                                   # a name of its own, so tests can skip the delay without touching time.sleep elsewhere


def hash_password(password: str, iterations: int = ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, it, salt, want = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), int(it))
        return hmac.compare_digest(dk.hex(), want)
    except (ValueError, TypeError):
        return False


class Auth:
    def __init__(self, username: str, password_hash: str, session_key: str, origin_secret: str = "", secure: str = "auto",
                 clock: Any = time.time) -> None:
        if len(session_key) < 32:
            raise ValueError("session_key must be at least 32 characters")
        self.username, self.password_hash, self.origin_secret = username, password_hash, origin_secret
        self._key = session_key.encode("utf-8")
        self.secure = secure                                   # "always" | "never" | "auto" (Secure only when the request arrived over https)
        self._clock = clock
        self._fails: dict[str, list[float]] = {}
        self._locked: dict[str, float] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls) -> Auth | None:
        path = os.environ.get("CVA_AUTH_FILE", "").strip()
        if not path:
            return None
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(d["username"], d["password_hash"], d["session_key"], d.get("origin_secret", ""),
                   os.environ.get("CVA_COOKIE_SECURE", "auto").strip().lower())

    # ------------------------------------------------------------ origin check
    def origin_ok(self, header_value: str | None) -> bool:
        return not self.origin_secret or hmac.compare_digest(header_value or "", self.origin_secret)

    # ------------------------------------------------------------ session cookie
    def _sign(self, payload: str) -> str:
        return hmac.new(self._key, payload.encode("utf-8"), hashlib.sha256).hexdigest()

    def make_cookie(self, user: str, https: bool) -> str:
        payload = base64.urlsafe_b64encode(f"{user}|{int(self._clock()) + SESSION_SECONDS}".encode()).decode()
        c = f"{COOKIE}={payload}.{self._sign(payload)}; Path=/; Max-Age={SESSION_SECONDS}; HttpOnly; SameSite=Lax"
        return c + ("; Secure" if self._secure(https) else "")

    def clear_cookie(self, https: bool) -> str:
        return f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax" + ("; Secure" if self._secure(https) else "")

    def _secure(self, https: bool) -> bool:
        return self.secure == "always" or (self.secure == "auto" and https)

    def user_from(self, cookie_header: str | None) -> str | None:
        if not cookie_header:
            return None
        try:
            c = SimpleCookie()
            c.load(cookie_header)
            raw = c[COOKIE].value
            payload, sig = raw.rsplit(".", 1)
            if not hmac.compare_digest(sig, self._sign(payload)):
                return None
            user, exp = base64.urlsafe_b64decode(payload.encode()).decode().rsplit("|", 1)
            return user if int(exp) > self._clock() and user == self.username else None
        except Exception:
            return None

    # ------------------------------------------------------------ sign-in with throttling
    def locked(self, who: str) -> bool:
        with self._lock:
            return self._locked.get(who, 0.0) > self._clock()

    def attempt(self, who: str, username: str, password: str) -> str:
        """Return \"ok\", \"bad\" or \"locked\".  A wrong guess costs about half a second, so guessing is slow even before the lock."""
        if self.locked(who):
            return "locked"
        good_user = hmac.compare_digest(username.encode(), self.username.encode())
        good_pw = verify_password(password, self.password_hash)         # always computed: the time does not reveal which part was wrong
        if good_user and good_pw:
            with self._lock:
                self._fails.pop(who, None)
            return "ok"
        _sleep(0.5)
        with self._lock:
            now = self._clock()
            recent = [t for t in self._fails.get(who, []) if now - t < WINDOW] + [now]
            self._fails[who] = recent
            if len(recent) >= MAX_FAILS:
                self._locked[who] = now + LOCK
                self._fails.pop(who, None)
        return "bad"


LOGIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>A01 CVA Output Processing Agent: sign in</title><style>
body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;background:#f4f2f9;margin:0;display:flex;min-height:100vh;align-items:center;justify-content:center}
form{background:#fff;padding:32px 36px;border-radius:12px;box-shadow:0 2px 16px rgba(60,30,120,.12);width:320px}
h1{font-size:18px;margin:0 0 4px;color:#3b1f78}p{margin:0 0 20px;color:#555;font-size:13px}
label{display:block;font-size:13px;margin:12px 0 4px;color:#333}
input{width:100%;box-sizing:border-box;padding:9px 10px;border:1px solid #bbb;border-radius:6px;font-size:14px}
button{margin-top:20px;width:100%;padding:10px;border:0;border-radius:6px;background:#5b2fc4;color:#fff;font-size:14px;cursor:pointer}
.err{background:#fdecea;color:#a12622;padding:8px 10px;border-radius:6px;font-size:13px;margin-bottom:8px}
</style></head><body><form method="post" action="/login"><h1>A01 CVA Output Processing Agent</h1><p>Project Elevate demo. Sign in to continue.</p>
%ERR%<label for="u">Username</label><input id="u" name="username" autocomplete="username" autofocus required>
<label for="p">Password</label><input id="p" name="password" type="password" autocomplete="current-password" required>
<button type="submit">Sign in</button></form></body></html>"""


def login_page(error: str = "") -> bytes:
    return LOGIN_PAGE.replace("%ERR%", f'<div class="err" role="alert">{escape(error)}</div>' if error else "").encode("utf-8")


def _main(argv: list[str]) -> int:
    if argv[:1] != ["hash"]:
        print("usage: python -m cva.ui.auth hash [--stdin]", file=sys.stderr)
        return 2
    if "--stdin" in argv:
        pw = sys.stdin.readline().rstrip("\n")                  # for scripts: the password never appears on a command line
    elif "--password" in argv:
        pw = argv[argv.index("--password") + 1]
    else:
        import getpass
        pw = getpass.getpass("Password: ")
        if pw != getpass.getpass("Again: "):
            print("the two entries differ", file=sys.stderr)
            return 1
    if len(pw) < 12:
        print("use at least 12 characters", file=sys.stderr)
        return 1
    print(hash_password(pw))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
