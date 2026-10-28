"""Hosted-demo sign-in, origin secret, keep-alive stream and the refusal to listen openly without sign-in."""
from __future__ import annotations

import http.client
import json
import threading
import time
from http.server import ThreadingHTTPServer
from urllib.parse import urlencode

import pytest

from cva.service import Service, ServiceError
from cva.ui import auth as auth_mod
from cva.ui import server as srv

PW, KEY, ORIGIN = "correct horse battery", "k" * 40, "o" * 40


def make_auth(clock=time.time, origin=""):
    return auth_mod.Auth("cva-output-agent", auth_mod.hash_password(PW, 1000), KEY, origin, "never", clock)


@pytest.fixture()
def host(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_mod, "_sleep", lambda s: None)          # a wrong guess normally costs 0.5 s
    state: dict = {}

    def start(a):
        svc = Service(tmp_path / "runs")
        srv.Handler.svc, srv.Handler.demo_samples, srv.Handler.auth = svc, False, a
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        state["httpd"] = httpd
        return httpd.server_address[1], svc
    yield start
    if "httpd" in state:
        state["httpd"].shutdown()
    srv.Handler.auth = None


def req(port, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse()
    data = r.read()
    out = (r.status, dict(r.getheaders()), data)
    c.close()
    return out


def login(port, user="cva-output-agent", pw=PW, headers=None):
    return req(port, "POST", "/login", urlencode({"username": user, "password": pw}),
               {"Content-Type": "application/x-www-form-urlencoded", **(headers or {})})


def cookie_of(h):
    return h["Set-Cookie"].split(";")[0]


def test_password_hash_roundtrip_and_wrong_password():
    h = auth_mod.hash_password(PW, 1000)
    assert h.startswith("pbkdf2_sha256$1000$") and PW not in h
    assert auth_mod.verify_password(PW, h) and not auth_mod.verify_password(PW + "x", h)
    assert not auth_mod.verify_password(PW, "garbage") and not auth_mod.verify_password(PW, "md5$1$aa$bb")


def test_without_sign_in_pages_redirect_and_api_is_refused_but_health_is_open(host):
    port, _ = host(make_auth())
    s, h, _ = req(port, "GET", "/")
    assert s == 302 and h["Location"] == "/login"
    assert req(port, "GET", "/api/runs")[0] == 401
    assert req(port, "POST", "/api/runs", "{}", {"Content-Type": "application/json"})[0] == 401
    assert req(port, "GET", "/download/run-0001/x.pptx")[0] == 401
    s, _, b = req(port, "GET", "/health")
    assert s == 200 and json.loads(b) == {"ok": True}
    assert b"Sign in" in req(port, "GET", "/login")[2]


def test_sign_in_sets_a_hardened_cookie_and_unlocks_the_app(host):
    port, _ = host(make_auth())
    assert login(port, pw="wrong")[0] == 401 and login(port, user="someone", pw=PW)[0] == 401
    s, h, _ = login(port)
    assert s == 302 and h["Location"] == "/"
    assert "HttpOnly" in h["Set-Cookie"] and "SameSite=Lax" in h["Set-Cookie"] and PW not in h["Set-Cookie"]
    ck = {"Cookie": cookie_of(h)}
    assert req(port, "GET", "/", headers=ck)[0] == 200
    s, _, b = req(port, "GET", "/api/app", headers=ck)
    assert json.loads(b) == {"demo_samples": False, "user": "cva-output-agent", "signed_in": True}
    assert req(port, "GET", "/api/runs", headers=ck)[0] == 200
    s, h2, _ = req(port, "POST", "/logout", "", ck)
    assert s == 302 and "Max-Age=0" in h2["Set-Cookie"]


def test_tampered_or_expired_cookies_are_refused(host):
    t = {"now": 1_000_000.0}
    a = make_auth(clock=lambda: t["now"])
    port, _ = host(a)
    ck = cookie_of(login(port)[1])
    assert req(port, "GET", "/api/runs", headers={"Cookie": ck})[0] == 200
    name, val = ck.split("=", 1)
    assert req(port, "GET", "/api/runs", headers={"Cookie": f"{name}={val[:-1]}{'0' if val[-1] != '0' else '1'}"})[0] == 401
    assert req(port, "GET", "/api/runs", headers={"Cookie": f"{name}=abc.def"})[0] == 401
    t["now"] += auth_mod.SESSION_SECONDS + 1
    assert req(port, "GET", "/api/runs", headers={"Cookie": ck})[0] == 401
    other = auth_mod.Auth("cva-output-agent", a.password_hash, "z" * 40, "", "never")       # a cookie signed with another key
    assert other.user_from(ck) is None


def test_repeated_wrong_passwords_lock_the_address_even_for_the_right_password(host):
    port, _ = host(make_auth())
    codes = [login(port, pw="nope")[0] for _ in range(auth_mod.MAX_FAILS)]
    assert codes == [401] * auth_mod.MAX_FAILS
    s, _, b = login(port)
    assert s == 429 and b"Too many attempts" in b


def test_origin_secret_blocks_direct_access_and_trusts_forwarded_address_only_with_it(host):
    port, _ = host(make_auth(origin=ORIGIN))
    assert req(port, "GET", "/login")[0] == 403 and login(port)[0] == 403
    assert req(port, "GET", "/api/runs", headers={"X-Origin-Verify": "wrong"})[0] == 403
    oh = {"X-Origin-Verify": ORIGIN}
    s, h, _ = login(port, headers=oh)
    assert s == 302
    assert req(port, "GET", "/api/runs", headers={**oh, "Cookie": cookie_of(h)})[0] == 200
    assert req(port, "GET", "/api/runs", headers={"Cookie": cookie_of(h)})[0] == 403          # a valid cookie alone is not enough
    for _ in range(auth_mod.MAX_FAILS):                                                         # each client address is counted on its own
        login(port, pw="nope", headers={**oh, "X-Forwarded-For": "203.0.113.9"})
    assert login(port, headers={**oh, "X-Forwarded-For": "203.0.113.9"})[0] == 429
    assert login(port, headers={**oh, "X-Forwarded-For": "203.0.113.10"})[0] == 302
    for i in range(auth_mod.MAX_FAILS):                                                         # a forged first entry must not dodge the lock: CloudFront appends the real address last
        login(port, pw="nope", headers={**oh, "X-Forwarded-For": f"10.9.9.{i}, 198.51.100.7"})
    assert login(port, headers={**oh, "X-Forwarded-For": "10.1.1.1, 198.51.100.7"})[0] == 429


def test_signed_in_posts_must_be_json(host):
    port, _ = host(make_auth())
    ck = {"Cookie": cookie_of(login(port)[1])}
    assert req(port, "POST", "/api/runs", "a=b", {**ck, "Content-Type": "application/x-www-form-urlencoded"})[0] == 415
    assert req(port, "POST", "/api/runs", "{}", {**ck, "Content-Type": "application/json"})[0] == 400     # reaches the app: no files chosen


def test_slow_actions_stream_keepalive_spaces_then_the_json(host, monkeypatch):
    port, svc = host(None)
    monkeypatch.setattr(srv, "KEEPALIVE_FIRST", 0.2)
    monkeypatch.setattr(srv, "KEEPALIVE_EVERY", 0.2)

    def slow():
        time.sleep(0.9)
        return {"ok": True, "checks": []}
    monkeypatch.setattr(svc, "transform_preflight", slow)
    s, _, b = req(port, "POST", "/api/transform_preflight", "{}", {"Content-Type": "application/json"})
    assert s == 200 and b.startswith(b" ") and json.loads(b) == {"ok": True, "checks": []}

    def slow_error():
        time.sleep(0.5)
        raise ServiceError("no connection")
    monkeypatch.setattr(svc, "transform_preflight", slow_error)
    s, _, b = req(port, "POST", "/api/transform_preflight", "{}", {"Content-Type": "application/json"})
    assert s == 200 and json.loads(b) == {"error": "no connection"}            # the page's api() shows j.error

    monkeypatch.setattr(svc, "transform_preflight", lambda: {"ok": True})
    s, _, b = req(port, "POST", "/api/transform_preflight", "{}", {"Content-Type": "application/json"})
    assert s == 200 and not b.startswith(b" ")                                  # a quick answer is a plain answer


def test_listening_openly_without_sign_in_is_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("CVA_AUTH_FILE", raising=False)
    with pytest.raises(SystemExit, match="without sign-in"):
        srv.serve(Service(tmp_path / "runs"), 0, False, "0.0.0.0")


def test_auth_file_is_read_from_the_environment(tmp_path, monkeypatch):
    f = tmp_path / "auth.json"
    f.write_text(json.dumps({"username": "cva-output-agent", "password_hash": auth_mod.hash_password(PW, 1000), "session_key": KEY, "origin_secret": ORIGIN}))
    monkeypatch.setenv("CVA_AUTH_FILE", str(f))
    monkeypatch.setenv("CVA_COOKIE_SECURE", "always")
    a = auth_mod.Auth.from_env()
    assert a and a.origin_ok(ORIGIN) and not a.origin_ok("x") and "Secure" in a.make_cookie("cva-output-agent", False)
    monkeypatch.delenv("CVA_AUTH_FILE")
    assert auth_mod.Auth.from_env() is None
    with pytest.raises(ValueError):
        auth_mod.Auth("u", "h", "short")
