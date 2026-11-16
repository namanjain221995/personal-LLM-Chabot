"""Tests of the sf-assistant web page. Fake pipeline and fake data only: no model, no DuckDB snapshot.

    cd sf_assistant && PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider -q tests
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import httpx
import pytest

sys.dont_write_bytecode = True  # never write __pycache__ into the read-only eval code dir

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
STATIC = WEB / "static"
sys.path.insert(0, str(WEB))

import codes  # noqa: E402
import pipeline  # noqa: E402
import server  # noqa: E402

TRACE = {"queries": [{"tool": "count_records", "object": "Fake_Object__c", "sql": ["SELECT COUNT(*) FROM t WHERE a = ?"],
                      "params": [["x"]], "row_count": 1, "ok": True, "error": None}],
         "record_answer": "There are 3 fake records.", "knowledge_answer": "Fake_Object__c holds fake rows.",
         "timings": {"total_s": 0.1}}


class FakePipeline:
    """Stands in for pipeline.Pipeline: answer() runs in the server's worker thread."""

    def __init__(self, delay: float = 0.0, gate: threading.Event | None = None, error: Exception | None = None):
        self.delay, self.gate, self.error = delay, gate, error
        self.calls: list[tuple[str, str]] = []
        self.active = self.max_active = self.started = 0
        self.intervals: list[tuple[float, float]] = []
        self._lock = threading.Lock()

    def answer(self, question: str, mode: str = "merged") -> dict:
        with self._lock:
            self.active += 1
            self.started += 1
            self.max_active = max(self.max_active, self.active)
            self.calls.append((question, mode))
        t0 = time.monotonic()
        try:
            if self.gate is not None and len(self.calls) == 1:
                self.gate.wait(10)
            if self.delay:
                time.sleep(self.delay)
            if self.error is not None:
                raise self.error
            return {"answer": f"Fake answer to: {question}\n<script>alert(1)</script>", "kind": "merged",
                    "source_line": "Source: fake test copy, synced 2026-01-01 00:00 UTC.",
                    "data_ts": "2026-01-01 00:00 UTC", "trace": TRACE, "log": {"fake": True}}
        finally:
            with self._lock:
                self.active -= 1
                self.intervals.append((t0, time.monotonic()))

    def status(self) -> dict:
        return {"records_ok": True, "data_synced": "2026-01-01 00:00 UTC", "status_line": "records: fake",
                "records_error": None, "origin": "fake copy", "age_hours": 1.0, "model": "fake-model"}

    def close(self) -> None:
        pass


@pytest.fixture
def private(tmp_path):
    d = tmp_path / ".private"
    d.mkdir(mode=0o700)
    friend, admin = codes.write_codes(str(d / "codes.json"))
    return types.SimpleNamespace(dir=d, friend=friend, admin=admin)


def make_app(private, fake, **overrides):
    settings = server.WebSettings(private_dir=str(private.dir), home="unused", warehouse="unused", bind="127.0.0.1",
                               port=0, allowed_hosts=["testserver"], **overrides)
    return server.create_app(settings, pipeline=fake, model_check=lambda: True)


def client(app, addr: str = "127.0.0.1", cookies=None, raise_errors: bool = True) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, client=(addr, 123), raise_app_exceptions=raise_errors)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver", cookies=cookies)


async def login(c: httpx.AsyncClient, code: str) -> httpx.Response:
    return await c.post("/api/login", json={"code": code})


class Reply:
    """The outcome of one question, shaped like a response: 200 + the answer, or the error status + message."""

    def __init__(self, status_code: int, data: dict) -> None:
        self.status_code, self._data = status_code, data
        self.text = json.dumps(data)

    def json(self) -> dict:
        return self._data


async def wait_job(c: httpx.AsyncClient, ticket: int, timeout: float = 10.0) -> dict:
    t0 = time.monotonic()
    while True:
        job = (await c.get("/api/job")).json()["job"]
        if job and job["ticket"] == ticket and job["state"] not in ("waiting", "running"):
            return job
        if time.monotonic() - t0 > timeout:
            raise AssertionError(f"question {ticket} did not finish: {job}")
        await asyncio.sleep(0.02)


def outcome(job: dict) -> Reply:
    if job["state"] == "done":
        return Reply(200, job["result"])
    return Reply(job["status"] or 0, {"error": job["error"], **(job.get("extra") or {})})


async def ask(c: httpx.AsyncClient, body: dict, timeout: float = 10.0):
    """POST /api/ask, then follow the question through /api/job until it ends (what the page does)."""
    r = await c.post("/api/ask", json=body)
    if r.status_code != 202:
        return r
    return outcome(await wait_job(c, r.json()["ticket"], timeout))


def run(coro):
    return asyncio.run(coro)


def log_events(private) -> list[dict]:
    path = private.dir / "web_log.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


async def wait_for(predicate, timeout: float = 5.0):
    t0 = time.monotonic()
    while not predicate():
        if time.monotonic() - t0 > timeout:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.02)


# ---------------------------------------------------------------- login and cookie

def test_login_good_and_bad_codes(private):
    async def go():
        async with client(make_app(private, FakePipeline())) as c:
            r = await login(c, "WRONG-CODE-1234")
            assert r.status_code == 401 and r.json() == {"error": "That code is not valid."}
            assert "set-cookie" not in r.headers
            assert (await c.get("/api/status")).json() == {"role": None, "ready": True, "failed": False, "stuck": False}
            r = await login(c, private.friend.lower().replace("-", " "))  # case, spaces and dashes do not matter
            assert r.status_code == 200 and r.json() == {"role": "friend"}
            assert (await c.get("/api/status")).json()["role"] == "friend"
            r = await login(c, private.admin)
            assert r.json() == {"role": "admin"}
            assert (await c.get("/api/status")).json()["role"] == "admin"
            assert (await c.post("/api/logout", json={})).status_code == 200
            assert (await c.get("/api/status")).json()["role"] is None
        events = [e for e in log_events(private) if e["event"] == "login"]
        assert [e["ok"] for e in events] == [False, True, True]
        assert all(private.friend not in json.dumps(e) and private.admin not in json.dumps(e) for e in events)
    run(go())


def test_cookie_flags(private):
    async def go():
        async with client(make_app(private, FakePipeline())) as c:
            r = await login(c, private.friend)
            cookie = r.headers["set-cookie"]
            parts = [p.strip().lower() for p in cookie.split(";")]
            assert parts[0].startswith(server.COOKIE + "=") and len(parts[0]) > len(server.COOKIE) + 30
            assert "httponly" in parts
            assert "samesite=strict" in parts
            assert "path=/" in parts
            assert any(p.startswith("max-age=") for p in parts)
            assert not any(p.startswith("domain=") for p in parts)
            assert "secure" not in parts  # plain HTTP on the LAN (SFA_COOKIE_SECURE=1 adds it)
            token = parts[0].split("=", 1)[1]
            state = c._transport.app.state.web  # noqa: SLF001
            assert all(token not in k for k in state.sessions)  # only its sha256 is kept
    run(go())


def test_login_throttle(private):
    async def go():
        async with client(make_app(private, FakePipeline())) as c:
            for _ in range(server.LOGIN_FAILS):
                assert (await login(c, "AAAA-BBBB")).status_code == 401
            assert (await login(c, private.friend)).status_code == 429
        async with client(make_app(private, FakePipeline()), addr="10.0.0.9") as c:
            for _ in range(server.LOGIN_FAILS - 1):
                assert (await login(c, "AAAA-BBBB")).status_code == 401
            assert (await login(c, private.friend)).status_code == 200  # a good code takes its attempt back
            assert (await login(c, private.friend)).status_code == 200
    run(go())


def test_login_throttle_holds_for_concurrent_attempts(private, monkeypatch):
    calls = []
    real_verify = codes.verify

    def counting_verify(record, code):
        calls.append(code)
        time.sleep(0.01)
        return real_verify(record, code)

    monkeypatch.setattr(server.codes_mod, "verify", counting_verify)

    async def go():
        async with client(make_app(private, FakePipeline())) as c:
            rs = await asyncio.gather(*[login(c, f"WRONG-{i:04d}") for i in range(50)])
            got = sorted(r.status_code for r in rs)
            assert got.count(401) == server.LOGIN_FAILS and got.count(429) == 50 - server.LOGIN_FAILS
            assert (await login(c, private.friend)).status_code == 429
        async with client(make_app(private, FakePipeline()), addr="10.0.0.8") as other:
            assert (await login(other, private.friend)).status_code == 200  # another address is not blocked
    run(go())
    assert len(calls) == server.LOGIN_FAILS + 1


def test_login_throttle_forgets_the_oldest_addresses_only(private, monkeypatch):
    monkeypatch.setattr(server, "LOGIN_TRACKED", 5)
    state = server.WebState(server.WebSettings(private_dir=str(private.dir), home="u", warehouse="u"))
    for _ in range(server.LOGIN_FAILS):
        state.login_attempt("10.0.0.1")
    for i in range(2, 9):
        state.login_attempt(f"10.0.0.{i}")
    assert len(state.login_fails) <= 6 and state.login_blocked("10.0.0.8") is False
    assert "10.0.0.8" in state.login_fails  # the newest address is kept; the oldest go first


def test_codes_stored_only_hashed_and_rotation_signs_out(private):
    path = private.dir / "codes.json"
    raw = path.read_text()
    for code in (private.friend, private.admin):
        assert code not in raw and codes.normalize(code) not in raw and codes.normalize(code).lower() not in raw
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    record = codes.load_codes(str(path))
    assert set(record["codes"]) == {"friend", "admin"} and record["kdf"] == "scrypt"
    assert codes.verify(record, private.friend) == "friend"
    assert codes.verify(record, private.admin) == "admin"
    assert codes.verify(record, "") is None and codes.verify(record, "X" * 200) is None
    assert re.fullmatch(r"([2-9A-HJ-NP-Z]{4}-){4}[2-9A-HJ-NP-Z]{4}", private.friend)

    async def go():
        async with client(make_app(private, FakePipeline())) as c:
            assert (await login(c, private.friend)).status_code == 200
            assert (await ask(c, {"question": "asked under the old codes"})).status_code == 200
            time.sleep(0.01)
            friend, _ = codes.write_codes(str(path))  # ./run.sh new-codes
            assert (await c.get("/api/status")).json()["role"] is None
            assert (await login(c, private.friend)).status_code == 401
            assert (await login(c, friend)).status_code == 200
            assert (await c.get("/api/job")).json()["job"] is None  # nothing asked under the old codes is shown
    run(go())


def _in_a_terminal(argv: list[str]) -> tuple[int, str]:
    """Run ``argv`` with its stdout on a pseudo-terminal (as in the owner's own terminal); (exit code, output)."""
    import pty

    master, slave = pty.openpty()
    try:
        proc = subprocess.run(argv, stdout=slave, stderr=subprocess.PIPE, timeout=60)
    finally:
        os.close(slave)
    out = b""
    while True:
        try:
            chunk = os.read(master, 4096)
        except OSError:   # EIO: the terminal's other end is closed and everything was read
            break
        if not chunk:
            break
        out += chunk
    os.close(master)
    return proc.returncode, out.decode("utf-8", "replace")


def test_new_codes_command(tmp_path):
    code, out = _in_a_terminal([sys.executable, str(WEB / "codes.py"), "new", str(tmp_path / "p" / "codes.json")])
    assert code == 0
    shown = re.findall(r"([2-9A-HJ-NP-Z]{4}(?:-[2-9A-HJ-NP-Z]{4}){4})", out)
    assert len(shown) == 2
    record = codes.load_codes(str(tmp_path / "p" / "codes.json"))
    assert codes.verify(record, shown[0]) == "friend" and codes.verify(record, shown[1]) == "admin"
    assert shown[0] not in (tmp_path / "p" / "codes.json").read_text()


def test_new_codes_refuses_to_print_into_a_pipe(tmp_path):
    """Like new-mcp-token: no plain code ever lands in a pipe, a log or an agent's transcript, and the codes in use
    stay as they were (nothing is written)."""
    path = tmp_path / "codes.json"
    codes.write_codes(str(path))
    before = path.read_bytes()
    p = subprocess.run([sys.executable, str(WEB / "codes.py"), "new", str(path)], capture_output=True, text=True,
                       timeout=60)
    assert p.returncode == 2 and "not a terminal" in p.stderr and p.stdout == ""
    assert path.read_bytes() == before
    fresh = tmp_path / "fresh" / "codes.json"
    p = subprocess.run([sys.executable, str(WEB / "codes.py"), "new", str(fresh)], capture_output=True, text=True,
                       timeout=60)
    assert p.returncode == 2 and not fresh.exists() and not fresh.parent.exists()
    run_sh = (WEB / "run.sh").read_text(encoding="utf-8")
    assert 'python3 "$HERE/codes.py" new "$PRIVATE/codes.json"' in run_sh


# ---------------------------------------------------------------- roles

def test_friend_never_receives_trace_or_mode_effect(private):
    fake = FakePipeline()

    async def go():
        async with client(make_app(private, fake)) as c:
            assert (await c.post("/api/ask", json={"question": "How many?"})).status_code == 401
            assert (await c.get("/api/job")).status_code == 401
            await login(c, private.friend)
            for mode in ("knowledge", "records", "bogus", None):
                r = await ask(c, {"question": "How many fake records?", "mode": mode})
                assert r.status_code == 200, r.text
                body = r.json()
                assert set(body) == {"answer", "kind", "source_line", "data_ts", "seconds", "queue_wait_s"}
                assert "trace" not in r.text and "SELECT" not in r.text
                job = await c.get("/api/job")
                assert "trace" not in job.text and "SELECT" not in job.text and "mode" not in job.json()["job"]
            status = (await c.get("/api/status")).json()
            assert "admin" not in status and "status_line" not in json.dumps(status)
    run(go())
    assert [m for _, m in fake.calls] == ["merged"] * 4


def test_admin_receives_trace_and_mode(private):
    fake = FakePipeline()

    async def go():
        async with client(make_app(private, fake)) as c:
            await login(c, private.admin)
            r = await ask(c, {"question": "Count fake records", "mode": "records"})
            assert r.status_code == 200
            trace = r.json()["trace"]
            assert trace["queries"][0]["sql"] == ["SELECT COUNT(*) FROM t WHERE a = ?"]
            assert trace["queries"][0]["params"] == [["x"]] and trace["queries"][0]["row_count"] == 1
            assert trace["record_answer"] and trace["knowledge_answer"] and "timings" in trace
            assert (await c.get("/api/job")).json()["job"]["mode"] == "records"
            assert (await ask(c, {"question": "q", "mode": "knowledge"})).status_code == 200
            assert (await ask(c, {"question": "q"})).status_code == 200
            r = await c.post("/api/ask", json={"question": "q", "mode": "sql"})
            assert r.status_code == 400
            status = (await c.get("/api/status")).json()
            assert status["admin"]["status_line"] == "records: fake"
    run(go())
    assert [m for _, m in fake.calls] == ["records", "knowledge", "merged"]


# ---------------------------------------------------------------- queue and timeout

def test_queue_serializes_two_concurrent_asks(private):
    gate = threading.Event()
    fake = FakePipeline(gate=gate)
    app = make_app(private, fake)

    async def go():
        async with client(app) as a, client(app) as b:
            await login(a, private.friend)
            await login(b, private.admin)
            ra = await a.post("/api/ask", json={"question": "first"})
            assert ra.status_code == 202 and ra.json()["position"] == 0
            await wait_for(lambda: fake.started == 1)
            rb = await b.post("/api/ask", json={"question": "second"})
            assert rb.status_code == 202 and rb.json()["position"] == 1
            assert app.state.web.queue_length() == 2
            sa, sb = (await a.get("/api/status")).json(), (await b.get("/api/status")).json()
            assert sa["queue"] == {"length": 2, "your_position": 0}
            assert sb["queue"] == {"length": 2, "your_position": 1}
            assert sa["job"] == {"ticket": ra.json()["ticket"], "state": "running", "position": 0}
            assert sb["job"] == {"ticket": rb.json()["ticket"], "state": "waiting", "position": 1}
            await asyncio.sleep(0.3)
            assert fake.started == 1  # the second question waits for the lock
            r2 = await b.post("/api/ask", json={"question": "third while second waits"})
            assert r2.status_code == 409  # one waiting question per device
            gate.set()
            ra = outcome(await wait_job(a, ra.json()["ticket"]))
            rb = outcome(await wait_job(b, rb.json()["ticket"]))
            assert ra.status_code == 200 and rb.status_code == 200
            assert rb.json()["queue_wait_s"] >= 0.25
            assert (await a.get("/api/status")).json()["queue"] == {"length": 0, "your_position": None}
    try:
        run(go())
    finally:
        gate.set()
    assert fake.max_active == 1
    (s1, e1), (s2, e2) = sorted(fake.intervals)
    assert e1 <= s2


def test_timeout_returns_504_and_lock_stays_until_worker_ends(private):
    gate = threading.Event()
    fake = FakePipeline(gate=gate)
    app = make_app(private, fake, timeout_s=0.3)

    async def go():
        async with client(app) as a, client(app) as b:
            await login(a, private.admin)
            await login(b, private.friend)
            r = await ask(a, {"question": "slow one"})
            assert r.status_code == 504
            assert "No answer within" in r.json()["error"] and "still working" in r.json()["error"]
            status = (await a.get("/api/status")).json()
            assert status["queue"]["length"] == 1 and status["admin"]["running_past_timeout"] is True
            assert app.state.web.lock.locked()
            tb = asyncio.create_task(ask(b, {"question": "next one"}))
            await asyncio.sleep(0.4)
            assert fake.started == 1 and not tb.done()  # still waiting: the timed-out worker holds the lock
            gate.set()
            rb = await tb
            assert rb.status_code == 200
            await wait_for(lambda: not app.state.web.lock.locked())
    try:
        run(go())
    finally:
        gate.set()
    assert fake.max_active == 1
    kinds = [(e["event"], e.get("status")) for e in log_events(private) if e["event"] in ("ask", "late_finish")]
    assert ("ask", "timeout") in kinds and ("late_finish", None) in kinds and ("ask", "ok") in kinds
    assert kinds.index(("late_finish", None)) < kinds.index(("ask", "ok"))
    late = [e for e in log_events(private) if e["event"] == "late_finish"][0]
    assert late["answer"].startswith("Fake answer to: slow one") and late["question"] == "slow one"
    assert late["detail"] == {"fake": True} and late["mode"] == "merged" and late["role"] == "admin"


def test_rate_limit_per_device(private):
    app = make_app(private, FakePipeline(), rate_per_hour=2)

    async def go():
        async with client(app) as c, client(app) as window, client(app, addr="10.0.0.7") as other:
            await login(c, private.friend)
            for _ in range(2):
                assert (await ask(c, {"question": "q"})).status_code == 200
            r = await c.post("/api/ask", json={"question": "q"})
            assert r.status_code == 429 and r.json()["retry_after_s"] > 3500
            assert (await c.get("/api/status")).json()["limit"]["left"] == 0
            await login(c, private.friend)  # signing in again does not reset the allowance
            assert (await c.post("/api/ask", json={"question": "q"})).status_code == 429
            await c.post("/api/logout", json={})
            await login(c, private.friend)
            assert (await c.post("/api/ask", json={"question": "q"})).status_code == 429
            await login(window, private.friend)  # nor does a second window on the same device
            assert (await window.post("/api/ask", json={"question": "q"})).status_code == 429
            await login(window, private.admin)  # the admin has an allowance of his own
            assert (await ask(window, {"question": "q"})).status_code == 200
            await login(other, private.friend)  # another device has its own
            assert (await ask(other, {"question": "q"})).status_code == 200
    run(go())


def test_friend_sign_ins_never_end_an_admin_session(private, monkeypatch):
    monkeypatch.setattr(server, "MAX_SESSIONS_PER_ROLE", 3)
    app = make_app(private, FakePipeline())

    async def go():
        async with client(app) as admin:
            await login(admin, private.admin)
            for _ in range(10):
                async with client(app) as f:
                    assert (await login(f, private.friend)).status_code == 200
            assert (await admin.get("/api/status")).json()["role"] == "admin"
            assert sum(1 for s in app.state.web.sessions.values() if s.role == "friend") == 3
    run(go())


def test_question_length_limit_and_validation(private):
    fake = FakePipeline()

    async def go():
        async with client(make_app(private, fake)) as c:
            await login(c, private.friend)
            r = await c.post("/api/ask", json={"question": "x" * 1001})
            assert r.status_code == 400 and "too long" in r.json()["error"]
            assert (await ask(c, {"question": "x" * 1000})).status_code == 200
            assert (await c.post("/api/ask", json={"question": "   "})).status_code == 400
            assert (await c.post("/api/ask", json={"question": 5})).status_code == 400
            assert (await c.post("/api/ask", content=b"question=hi",
                                 headers={"content-type": "application/x-www-form-urlencoded"})).status_code == 415
            assert (await c.post("/api/ask", json={"question": "hi"},
                                 headers={"origin": "http://evil.example"})).status_code == 403
            assert (await c.post("/api/ask", content=b"{" + b" " * 20000 + b"}",
                                 headers={"content-type": "application/json"})).status_code == 413
    run(go())
    assert len(fake.calls) == 1


def test_errors_are_generic(private):
    fake = FakePipeline(error=RuntimeError("secret detail /srv/sfa/src/sf_assistant/tools/records.py line 99"))

    async def go():
        async with client(make_app(private, fake)) as c:
            await login(c, private.friend)
            r = await ask(c, {"question": "q"})
            assert r.status_code == 500
            assert "secret" not in r.text and "Traceback" not in r.text and "/srv" not in r.text
            job = await c.get("/api/job")
            assert "secret" not in job.text and "Traceback" not in job.text
            r = await c.get("/nope")
            assert r.status_code == 404 and r.json() == {"error": "Not found."}
            r = await c.get("/api/status", headers={"host": "evil.example"})
            assert r.status_code == 400
    run(go())
    err = [e for e in log_events(private) if e.get("status") == "error"]
    assert err and "secret detail" in err[0]["error"]  # the detail goes to the private log only


def test_web_log_records_question_answer_and_timings(private):
    async def go():
        async with client(make_app(private, FakePipeline())) as c:
            await login(c, private.friend)
            await ask(c, {"question": "How many fake records exist?"})
    run(go())
    path = private.dir / "web_log.jsonl"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    entry = [e for e in log_events(private) if e["event"] == "ask"][0]
    assert entry["question"] == "How many fake records exist?" and entry["status"] == "ok"
    assert entry["answer"].startswith("Fake answer") and "seconds" in entry and "queue_wait_s" in entry
    assert entry["role"] == "friend" and entry["detail"] == {"fake": True}


def test_answer_survives_a_reload_and_a_lost_response(private):
    gate = threading.Event()
    fake = FakePipeline(gate=gate)
    app = make_app(private, fake)

    async def go():
        async with client(app) as tab:
            await login(tab, private.friend)
            r = await tab.post("/api/ask", json={"question": "asked before the reload"})
            assert r.status_code == 202
            ticket = r.json()["ticket"]
        # the page reloaded (or its request was lost): a new connection with the same cookie
        async with client(app, cookies=tab.cookies) as again:
            job = (await again.get("/api/job")).json()["job"]
            assert job["ticket"] == ticket and job["state"] in ("waiting", "running")
            assert job["question"] == "asked before the reload" and job["result"] is None
            assert (await again.get("/api/status")).json()["job"]["ticket"] == ticket
            assert (await again.post("/api/ask", json={"question": "again"})).status_code == 409
            gate.set()
            done = await wait_job(again, ticket)
            assert done["state"] == "done" and done["result"]["answer"].startswith("Fake answer to: asked before")
            assert "trace" not in done["result"]
    try:
        run(go())
    finally:
        gate.set()
    assert len(fake.calls) == 1


def test_finished_answers_expire(private, monkeypatch):
    app = make_app(private, FakePipeline())

    async def go():
        async with client(app) as c:
            await login(c, private.friend)
            assert (await ask(c, {"question": "q"})).status_code == 200
            assert (await c.get("/api/job")).json()["job"]["state"] == "done"
            monkeypatch.setattr(server, "RESULT_KEEP_S", 0.0)
            await asyncio.sleep(0.01)
            assert (await c.get("/api/job")).json()["job"] is None
            assert (await c.get("/api/status")).json()["job"] is None
    run(go())


def test_sign_out_drops_the_waiting_question_and_the_answer(private):
    gate = threading.Event()
    fake = FakePipeline(gate=gate)
    app = make_app(private, fake)

    async def go():
        async with client(app) as admin, client(app, addr="10.0.0.5") as friend:
            await login(admin, private.admin)
            await login(friend, private.friend)
            first = await admin.post("/api/ask", json={"question": "first"})
            await wait_for(lambda: fake.started == 1)
            second = await friend.post("/api/ask", json={"question": "dropped"})
            assert second.status_code == 202
            await friend.post("/api/logout", json={})
            await wait_for(lambda: app.state.web.queue_length() == 1)
            await login(friend, private.friend)
            assert (await friend.get("/api/job")).json()["job"] is None
            gate.set()
            assert outcome(await wait_job(admin, first.json()["ticket"])).status_code == 200
            await admin.post("/api/logout", json={})
            await login(admin, private.admin)
            assert (await admin.get("/api/job")).json()["job"] is None  # nothing kept after a sign-out
    try:
        run(go())
    finally:
        gate.set()
    assert [q for q, _ in fake.calls] == ["first"]
    assert any(e.get("status") == "abandoned" and e.get("question") == "dropped" for e in log_events(private))


def test_unexpected_500_has_the_security_headers(private):
    app = make_app(private, FakePipeline())

    async def boom():
        raise RuntimeError("secret detail")

    app.add_api_route("/api/boom", boom, methods=["GET"])

    async def go():
        async with client(app, raise_errors=False) as c:
            r = await c.get("/api/boom")
            assert r.status_code == 500 and "secret" not in r.text
            assert r.headers["content-security-policy"] == server.CSP
            assert r.headers["x-content-type-options"] == "nosniff" and r.headers["cache-control"] == "no-store"
    run(go())


def test_health_check_fails_when_the_pipeline_failed(private):
    settings = server.WebSettings(private_dir=str(private.dir), home="unused", warehouse="unused", bind="127.0.0.1",
                               port=0, allowed_hosts=["testserver"])

    def broken(log):
        raise RuntimeError("no knowledge files")

    app = server.create_app(settings, pipeline_factory=broken, model_check=lambda: True)

    async def go():
        async with app.router.lifespan_context(app):
            await wait_for(lambda: app.state.web.pipeline_error is not None)
            async with client(app) as c:
                r = await c.get("/api/status")
                assert r.json() == {"role": None, "ready": False, "failed": True, "stuck": False}
                assert server.health_code(r.status_code, r.json()) == 1
    run(go())
    assert server.health_code(200, {"role": None, "ready": True, "failed": False, "stuck": False}) == 0
    assert server.health_code(200, {"role": None, "ready": True, "failed": False, "stuck": True}) == 1
    assert server.health_code(503, {}) == 1


# ---------------------------------------------------------------- page

def test_security_headers_and_static_page(private):
    async def go():
        async with client(make_app(private, FakePipeline())) as c:
            r = await c.get("/")
            assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
            csp = r.headers["content-security-policy"]
            assert "default-src 'self'" in csp and "script-src 'self'" in csp and "unsafe" not in csp
            assert "frame-ancestors 'none'" in csp
            assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY"
            assert r.headers["cache-control"] == "no-store"
            for name in ("app.js", "render.js", "app.css", "favicon.svg"):
                s = await c.get(f"/static/{name}")
                assert s.status_code == 200 and "content-security-policy" in s.headers
            assert (await c.get("/docs")).status_code == 404 and (await c.get("/openapi.json")).status_code == 404
    run(go())


def test_page_has_no_inline_code_and_no_html_sinks():
    html = (STATIC / "index.html").read_text()
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), "inline <script>"
    assert "<style" not in html and not re.search(r"\sstyle\s*=", html)
    assert not re.search(r"\son[a-z]+\s*=", html), "inline event handler"
    assert "http://" not in html and "https://" not in html, "external resource"
    assert sorted(re.findall(r'src="([^"]+)"', html)) == ["/static/app.js", "/static/render.js"]
    for name in ("app.js", "render.js"):
        js = (STATIC / name).read_text()
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function",
                     "setAttribute(\"on", "setAttribute('on", ".href", "srcdoc", "localStorage", "sessionStorage"):
            assert sink not in js, f"{name} uses {sink}"
    css = (STATIC / "app.css").read_text()
    assert "@import" not in css and "url(" not in css


CASES = {
    "script": "<script>alert(1)</script>",
    "img": "Hello <img src=x onerror=alert(1)> there",
    "link": "[click](javascript:alert(1)) and https://example.invalid/x",
    "bold": "**bold <b>x</b>** after",
    "code": "Use `<i>code</i>` here",
    "fence": "```\n<script>x()</script>\n```",
    "table": "| Field | Value |\n|---|---|\n| Status__c | <b>cell</b> |\n| Name | `x` |",
    "list": "- one\n- two **2**\n  - nested <u>u</u>\n\n3. third\n4. fourth",
    "underscores": "Interview__c and Application__c and Record_Type",
    "heading": "## Result <svg onload=alert(1)>\nLine one\nLine two",
}


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_renderer_keeps_model_html_as_text():
    proc = subprocess.run(["node", str(ROOT / "tests" / "render_check.js")], input=json.dumps(CASES),
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    allowed = set(out.pop("__allowed"))
    assert not allowed & {"script", "a", "img", "iframe", "svg", "style", "object", "embed", "form", "input"}
    for name, res in out.items():
        assert set(res["tags"]) <= allowed, (name, res["tags"])
        assert set(res["classes"]) <= {"md-table"}, (name, res["classes"])
        assert all(isinstance(s, int) for s in res["starts"])
    assert out["script"]["tree"] == 'p("<script>alert(1)</script>")'
    assert "<img src=x onerror=alert(1)>" in "".join(out["img"]["texts"]) and out["img"]["tags"] == ["p"]
    assert out["link"]["tags"] == ["p"] and "[click](javascript:alert(1))" in out["link"]["texts"][0]
    assert out["bold"]["tree"] == 'p(strong("bold <b>x</b>")," after")'
    assert out["code"]["tree"] == 'p("Use ",code("<i>code</i>")," here")'
    assert out["fence"]["tree"] == 'pre(code("<script>x()</script>"))'
    assert out["table"]["tags"] == ["code", "div", "table", "tbody", "td", "th", "thead", "tr"]
    assert 'td("<b>cell</b>")' in out["table"]["tree"]
    assert out["list"]["tree"].startswith('ul(li("one"),li("two ",strong("2"),ul(li("nested <u>u</u>"))))')
    assert 'ol(li("third"),li("fourth"))' in out["list"]["tree"] and out["list"]["starts"] == [3]
    assert out["underscores"]["tree"] == 'p("Interview__c and Application__c and Record_Type")'
    assert out["heading"]["tree"] == 'h3("Result <svg onload=alert(1)>"),p("Line one",br(),"Line two")'


# ---------------------------------------------------------------- pipeline (fake ask module, fake files)

class FakeSession:
    open_now = 0

    def __init__(self, path: str):
        self.version = Path(path).read_text().strip()
        self.closed = False
        FakeSession.open_now += 1
        assert FakeSession.open_now == 1, "a new session was opened while the old one was still open"

    def status(self):
        return types.SimpleNamespace(newest_sync_utc=f"sync of {self.version}", age_hours=1.0, origin="fake copy",
                                     line=lambda: f"records: fake {self.version}")

    def close(self):
        if not self.closed:
            self.closed = True
            FakeSession.open_now -= 1


class FakeAsk:
    """The two sf_assistant.cli functions the pipeline calls: open_record_session() and ask()."""

    def __init__(self):
        self.sessions: list[FakeSession] = []
        self.calls: list[dict] = []
        self.knowledge_dirs: list = []
        self.on_ask = None

    def open_record_session(self, cfg, rcfg, knowledge_dir=None):
        self.knowledge_dirs.append(knowledge_dir)
        if not os.path.isfile(rcfg.warehouse):
            return None, ["records off: no read copy"]
        s = FakeSession(rcfg.warehouse)
        self.sessions.append(s)
        return s, [s.status().line()]

    def ask(self, text, cfg, registry, system, llm, n, verbose, session=None, max_rows=20, records_only=False,
            records_wanted=False, lean_knowledge=True):
        print(f"transcript of question {n}: {text}")
        self.calls.append({"session": session, "records_only": records_only, "records_wanted": records_wanted,
                           "lean": lean_knowledge, "verbose": verbose})
        queries = [{"tool": "count_records", "object": "Fake__c", "ok": True, "error": None, "error_code": None,
                    "sql": ["SELECT 1"], "params": [[]], "row_count": 1, "summary": "1 row"}]
        if self.on_ask is not None:
            queries = self.on_ask(session) or queries
        where = session.version if session is not None else "knowledge"
        kind = "merged" if session is not None else "knowledge"
        answer = f"Answer from {where}.\n- **Source:** fake copy, synced 2026-01-01." if session else f"From {where}."
        return {"answer": kind, "records": "1 q, 1 row", "rq": 1, "audit": "ok", "ktools": 2, "rtools": 1,
                "error": None, "record_error": None,
                "jsonl": {"answer": answer, "answer_kind": kind, "queries": queries, "record_answer": "r",
                          "knowledge_answer": "k", "record_facts": [{"rows": ["Real Name"]}], "seconds": 1.5,
                          "route_seconds": 0.1, "knowledge_seconds": 0.5, "record_seconds": 0.7, "merge_seconds": 0.2,
                          "knowledge_mode": "lean", "record_error": None}}


def replace_file(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)  # what the sync-worker does: a new inode


def make_pipeline(tmp_path, events, knowledge_dir=None):
    FakeSession.open_now = 0
    wh = tmp_path / "warehouse.read.duckdb"
    wh.write_text("v1")
    fake = FakeAsk()
    p = pipeline.Pipeline(fake, types.SimpleNamespace(model="m", base_url="http://localhost:8000/v1"), None, None,
                          None, types.SimpleNamespace(warehouse=str(wh)), log=events.append, knowledge_dir=knowledge_dir)
    return p, fake, wh


def test_pipeline_modes_source_split_and_capture(tmp_path):
    events: list[dict] = []
    p, fake, _ = make_pipeline(tmp_path, events)
    out = p.answer("How many?", "merged")
    assert out["answer"] == "Answer from v1." and out["source_line"] == "Source: fake copy, synced 2026-01-01."
    assert out["kind"] == "merged" and out["data_ts"] == "sync of v1"
    assert "transcript of question 1: How many?" in out["trace"]["transcript"]
    assert out["trace"]["queries"][0]["sql"] == ["SELECT 1"] and "record_facts" not in out["log"]["jsonl"]
    assert fake.calls[-1] == {"session": fake.sessions[0], "records_only": False, "records_wanted": True,
                              "lean": True, "verbose": False}
    p.answer("q", "records")
    assert fake.calls[-1]["records_only"] is True and fake.calls[-1]["session"] is fake.sessions[0]
    out = p.answer("q", "knowledge")
    assert fake.calls[-1]["session"] is None and fake.calls[-1]["records_wanted"] is False
    assert out["data_ts"] is None and out["source_line"] == "Source: object knowledge files."
    with pytest.raises(ValueError):
        p.answer("q", "sql")
    assert [e["event"] for e in events] == ["records_opened"]


def test_records_mode_says_no_answer_after_an_empty_record_answer(tmp_path):
    """Review of 2026-10-22: since evaluation fix 2 an empty record answer in records mode ends as kind "none"; the page
    says "not needed" only when the record side found no record question (no record error)."""
    events: list[dict] = []
    p, fake, _ = make_pipeline(tmp_path, events)
    base, err = fake.ask, ["empty record answer"]

    def ask(*a, **k):
        out = base(*a, **k)
        out["answer"], out["record_error"] = "none", err[0]
        out["jsonl"].update(answer="", answer_kind="none", record_error=err[0])
        return out

    fake.ask = ask
    assert p.answer("q", "records")["answer"] == "No answer was found for this question."
    err[0] = None
    assert p.answer("q", "records")["answer"] == "No record data is needed for this question."


def test_admin_trace_has_the_route_and_why_it_was_overruled(tmp_path):
    """Value spots (2026-10-28): the admin trace's "route" comes from the JSONL triage (the route call, the route
    taken, what overruled it and the spotted values as the log keeps them: no stored value, no Id); missing keys give
    None or []. The web log keeps the JSONL triage as it is."""
    events: list[dict] = []
    p, fake, _ = make_pipeline(tmp_path, events)
    base = fake.ask
    value = {"span": "zorvex", "object": "Department__c", "field": "Name", "kind": "name", "strength": "strong",
             "why": "cue word", "records": 1}
    triage = {"routed": True, "route": False, "names": [], "people": [], "missing": [], "people_data": True,
              "seconds": 0.0, "values": [value], "overruled_by": "value",
              "overrule_reason": "the stored value 'zorvex' (Department__c.Name, cue word)"}

    def ask(*a, **k):
        out = base(*a, **k)
        out["jsonl"]["triage"] = dict(triage)
        return out

    fake.ask = ask
    out = p.answer("what does team zorvex do?", "merged")
    assert out["trace"]["route"] == {"route": False, "routed": True, "overruled_by": "value",
                                     "overrule_reason": "the stored value 'zorvex' (Department__c.Name, cue word)",
                                     "values": [value]}
    assert out["log"]["jsonl"]["triage"] == triage
    fake.ask = base   # no triage in the JSONL (knowledge mode, or a question as before): None and []
    for mode in ("merged", "knowledge"):
        assert p.answer("q", mode)["trace"]["route"] == {"route": None, "routed": None, "overruled_by": None,
                                                         "overrule_reason": None, "values": []}


def test_pipeline_reopens_when_the_read_copy_is_replaced(tmp_path):
    events: list[dict] = []
    p, fake, wh = make_pipeline(tmp_path, events)
    assert p.answer("q1")["answer"] == "Answer from v1."
    assert p.answer("q2")["answer"] == "Answer from v1."
    assert len(fake.sessions) == 1
    replace_file(wh, "v2")
    out = p.answer("q3")
    assert out["answer"] == "Answer from v2." and out["data_ts"] == "sync of v2"
    assert len(fake.sessions) == 2 and fake.sessions[0].closed and not fake.sessions[1].closed
    assert [e["event"] for e in events] == ["records_opened", "records_reopened"]
    assert events[-1]["reason"] == "the read copy was replaced"


def test_pipeline_record_sessions_read_the_knowledge_copy(tmp_path):
    events: list[dict] = []
    p, fake, wh = make_pipeline(tmp_path, events, knowledge_dir=str(tmp_path / "copy"))
    replace_file(wh, "v2")
    p.answer("q")
    assert fake.knowledge_dirs == [str(tmp_path / "copy")] * 2  # at start-up and at the reopen


def test_snapshot_knowledge_is_a_fixed_copy(tmp_path):
    src = tmp_path / "knowledge"
    src.mkdir()
    (src / "Fake_A__c.json").write_text('{"api_name": "Fake_A__c"}')
    (src / "Fake_B__c.json").write_text('{"api_name": "Fake_B__c"}')
    (src / "Fake_B__c.raw.json").write_text("{}")
    files_of = lambda d: sorted(str(x) for x in Path(d).glob("*.json") if not x.name.endswith(".raw.json"))  # noqa: E731
    dest, paths = pipeline.snapshot_knowledge(str(src), files_of, root=str(tmp_path))
    assert [Path(x).name for x in paths] == ["Fake_A__c.json", "Fake_B__c.json"]
    assert all(Path(x).parent == Path(dest) for x in paths) and stat.S_IMODE(os.stat(dest).st_mode) == 0o700
    (src / "Fake_A__c.json").write_text('{"api_name": "changed"}')
    assert json.loads(Path(paths[0]).read_text()) == {"api_name": "Fake_A__c"}  # later edits do not reach the copy
    (src / "Fake_A__c.json").write_text('{"api_name": ')  # caught mid-write, and it stays broken
    with pytest.raises(RuntimeError, match="not valid JSON"):
        pipeline.snapshot_knowledge(str(src), files_of, root=str(tmp_path), wait_s=0.01)
    assert sorted(p.name for p in tmp_path.glob("sfa-knowledge-*")) == [Path(dest).name]  # the failed copy is removed


def test_snapshot_components_copies_index_and_flows(tmp_path):
    sys.path.insert(0, str(ROOT / "tests"))
    from component_fixture import make_components

    src = make_components(tmp_path / "components", fillers=2)
    dest = pipeline.snapshot_components(str(src), str(tmp_path / "copy" / "_components"))
    assert dest == str(tmp_path / "copy" / "_components")
    assert (Path(dest) / "index.json").read_bytes() == (src / "index.json").read_bytes()
    assert sorted(x.name for x in Path(dest, "flows").glob("*.json")) == sorted(x.name for x in (src / "flows").glob("*.json"))
    (src / "index.json").write_text("{}")        # later edits do not reach the copy
    assert (Path(dest) / "index.json").read_text() != "{}"
    assert pipeline.snapshot_components(str(tmp_path / "none"), str(tmp_path / "x")) is None   # no index: no copy
    assert not (tmp_path / "x").exists()
    (src / "flows" / "Fake_Filler_00.json").write_text('{"kind": ')   # caught mid-write, and it stays broken
    with pytest.raises(RuntimeError, match="not valid JSON"):
        pipeline.snapshot_components(str(src), str(tmp_path / "copy2"), wait_s=0.01)


def test_pipeline_retries_once_after_a_query_error_during_a_swap(tmp_path):
    events: list[dict] = []
    p, fake, wh = make_pipeline(tmp_path, events)

    def swap_mid_question(session):
        fake.on_ask = None
        replace_file(wh, "v2")
        return [{"tool": "count_records", "object": "Fake__c", "ok": False, "error_code": "E13", "sql": [], "params": [],
                 "error": "the query failed in DuckDB: IOException: could not read", "row_count": 0, "summary": ""}]

    fake.on_ask = swap_mid_question
    out = p.answer("q")
    assert out["answer"] == "Answer from v2." and out["trace"]["retried_after_swap"] is True
    assert len(fake.calls) == 2 and fake.sessions[0].closed
    assert [e["event"] for e in events] == ["records_opened", "swap_retry", "records_reopened"]


def test_reopen_and_retry_seconds_reach_the_ask_that_paid_for_them(tmp_path):
    """Latency logging (2026-10-29): an ask that reopened the records carries ``reopen`` = {close_s, open_s, s} in its
    log detail, a swap-retried ask ``retry_s``; ``records_reopened`` logs ``close_seconds``. Other asks carry neither."""
    events: list[dict] = []
    p, fake, wh = make_pipeline(tmp_path, events)
    out = p.answer("q1")
    assert "reopen" not in out["log"] and "retry_s" not in out["log"]
    replace_file(wh, "v2")
    reopen = p.answer("q2")["log"]["reopen"]
    assert set(reopen) == {"close_s", "open_s", "s"} and reopen["s"] >= reopen["open_s"] >= 0 <= reopen["close_s"]
    assert events[-1]["event"] == "records_reopened" and events[-1]["close_seconds"] == reopen["close_s"]
    assert events[0]["event"] == "records_opened" and "close_seconds" not in events[0]   # nothing to close at start
    assert "reopen" not in p.answer("q3")["log"] and "reopen" not in p.answer("q4", "knowledge")["log"]

    def swap_mid_question(session):
        fake.on_ask = None
        replace_file(wh, "v3")
        return [{"tool": "count_records", "object": "Fake__c", "ok": False, "error_code": "E13", "sql": [], "params": [],
                 "error": "the query failed in DuckDB: IOException: could not read", "row_count": 0, "summary": ""}]

    fake.on_ask = swap_mid_question
    out = p.answer("q5")
    assert out["trace"]["retried_after_swap"] is True and out["log"]["retry_s"] >= 0 and "reopen" not in out["log"]


def test_a_failed_reopen_after_a_swap_error_is_logged_with_its_seconds(tmp_path):
    """Review of 2026-10-29: when the reopen after a swap error fails (the new copy cannot be opened yet), its close
    and open seconds go to ``failed_reopen``, so the page seconds still add up."""
    events: list[dict] = []
    p, fake, wh = make_pipeline(tmp_path, events)

    def copy_gone_mid_question(session):
        fake.on_ask = None
        wh.unlink()
        return [{"tool": "count_records", "object": "Fake__c", "ok": False, "error_code": "E13", "sql": [], "params": [],
                 "error": "the query failed in DuckDB: IOException: could not read", "row_count": 0, "summary": ""}]

    fake.on_ask = copy_gone_mid_question
    out = p.answer("q")
    failed = out["log"]["failed_reopen"]
    assert set(failed) == {"close_s", "open_s", "s"} and failed["s"] >= failed["open_s"] >= 0
    assert out["trace"]["retried_after_swap"] is False and "retry_s" not in out["log"] and "reopen" not in out["log"]
    assert [e["event"] for e in events] == ["records_opened", "swap_retry", "records_open_failed"]
    assert events[-1]["close_seconds"] == failed["close_s"]


def test_pipeline_query_error_without_a_swap_is_not_retried(tmp_path):
    events: list[dict] = []
    p, fake, _ = make_pipeline(tmp_path, events)
    fake.on_ask = lambda s: [{"tool": "count_records", "object": "Fake__c", "ok": False, "error_code": "E13",
                              "error": "the query failed in DuckDB", "sql": [], "params": [], "row_count": 0}]
    out = p.answer("q")
    assert len(fake.calls) == 1 and out["trace"]["retried_after_swap"] is False


def test_pipeline_records_off_when_the_file_is_missing(tmp_path):
    events: list[dict] = []
    p, fake, wh = make_pipeline(tmp_path, events)
    p.close()
    wh.unlink()
    p.session = None
    out = p.answer("q", "records")
    assert out["kind"] == "none" and "not available" in out["answer"] and not fake.calls
    out = p.answer("q", "merged")  # merged without records: the knowledge answer
    assert fake.calls[-1]["session"] is None and out["kind"] == "knowledge"
    wh.write_text("v3")
    assert p.answer("q", "merged")["answer"] == "Answer from v3."


def test_split_source_forms():
    assert pipeline.split_source("A\n\n**Source:** x, synced y.") == ("A", "Source: x, synced y.")
    assert pipeline.split_source("A\n- **Source**: x\n\n") == ("A", "Source: x")
    assert pipeline.split_source("A\nSource: one\nB\nsource: two") == ("A\nSource: one\nB", "Source: two")
    assert pipeline.split_source("A\nLookup used: x.\nSource: s") == ("A\nLookup used: x.", "Source: s")
    assert pipeline.split_source("No source here") == ("No source here", None)
    field_doc = "Fields:\n- **Source**: where the finding came from\n- **Status**: open or closed"
    assert pipeline.split_source(field_doc) == (field_doc, None)


def test_knowledge_answers_keep_their_source_lines(tmp_path):
    events: list[dict] = []
    p, fake, _ = make_pipeline(tmp_path, events)
    text = "Quality_Finding__c fields:\n- **Source**: where the finding came from"

    def knowledge_row(*args, **kwargs):
        return {"answer": "knowledge", "jsonl": {"answer": text, "answer_kind": "knowledge", "queries": []}}

    fake.ask = knowledge_row
    out = p.answer("Which fields?", "knowledge")
    assert out["answer"] == text and out["source_line"] == "Source: object knowledge files."


def test_stdout_capture_takes_prints_of_other_threads():
    cap = pipeline.install_capture()
    with cap.capture() as buf:
        t = threading.Thread(target=lambda: print("from the record agent thread"))
        t.start()
        t.join()
        print("from ask")
    assert "from the record agent thread" in buf.getvalue() and "from ask" in buf.getvalue()


# ---------------------------------------------------------------- charts (synthetic labels)

CHART_LABEL = "Sentinel Status 7f3a"
CHART_QUESTION = "bar chart of fake records by status"


def chart_item(label: str = CHART_LABEL) -> dict:
    """One chart item as sf_assistant.agent.charts.make returns it, from a synthetic result: no model, no warehouse."""
    from sf_assistant.agent import charts

    result = {"query_id": "q1", "object": "Fake__c", "grouped_by": "Status__c", "measure": "count",
              "groups": [{"group": label, "value": 3}, {"group": "Status B", "value": 2}], "group_count": 2, "shown": 2,
              "total_across_groups": 5, "unresolved_ids": 0, "ties_at_cutoff": [], "ties_at_cutoff_count": 0}
    evidence = types.SimpleNamespace(tool="group_records", ok=True, kind="data", object="Fake__c", arguments={},
                                     model_result=result)
    rec = types.SimpleNamespace(evidence=[evidence], table_blocks={})
    _final, items, lines, error = charts.make(charts.chart_ask(CHART_QUESTION), CHART_QUESTION, "Five fake records.",
                                              "records", "lean", rec, None, None)
    assert len(items) == 1 and items[0]["chart"] and lines == [] and error == ""
    return items[0]


class ChartPipeline(FakePipeline):
    """A fake pipeline whose answer holds these chart items."""

    def __init__(self, items: list[dict]):
        super().__init__()
        self.items = items

    def answer(self, question: str, mode: str = "merged") -> dict:
        return {**super().answer(question, mode), "charts": self.items}


def ask_returns_chart(fake: FakeAsk, item: dict) -> None:
    """``fake.ask`` now returns a row as sf_assistant.pipeline.result_row shapes a chart answer."""
    from sf_assistant.agent import charts

    plain = fake.ask

    def with_chart(*args, **kwargs):
        row = plain(*args, **kwargs)
        row["charts"] = [item]
        row["jsonl"].update({"charts": charts.for_jsonl([item]), "charts_talk": True})
        return row

    fake.ask = with_chart


def test_charts_reach_friend_and_admin(private):
    item = chart_item()

    async def go():
        for code, role in ((private.friend, "friend"), (private.admin, "admin")):
            async with client(make_app(private, ChartPipeline([item]))) as c:
                await login(c, code)
                r = await ask(c, {"question": CHART_QUESTION})
                assert r.status_code == 200, r.text
                assert r.json()["charts"] == [item] and ("trace" in r.json()) is (role == "admin")
                assert (await c.get("/api/job")).json()["job"]["result"]["charts"] == [item]    # after a reload too
            async with client(make_app(private, ChartPipeline([]))) as c:                       # no chart: no key
                await login(c, code)
                assert "charts" not in (await ask(c, {"question": "How many?"})).json()
            async with client(make_app(private, FakePipeline())) as c:
                await login(c, code)
                assert "charts" not in (await ask(c, {"question": "How many?"})).json()
    run(go())


def test_chart_label_with_a_lone_surrogate_reaches_the_page(private):
    """The job reply is encoded as UTF-8: a chart item from a label holding half a surrogate pair still gets a 200, and
    the answer is not lost to it."""
    item = chart_item("Team \ud83d full")

    async def go():
        async with client(make_app(private, ChartPipeline([item])), raise_errors=False) as c:
            await login(c, private.friend)
            r = await ask(c, {"question": CHART_QUESTION})
            assert r.status_code == 200, r.text
            assert r.json()["charts"][0]["data"][0]["Status"] == "Team full"
            job = await c.get("/api/job")
            assert job.status_code == 200 and job.json()["job"]["result"]["charts"] == r.json()["charts"]
    run(go())


def test_web_log_holds_no_chart(private, tmp_path):
    """P10, through the real ``_shape`` and ``run_job``: the job reply holds the chart, no log line does."""
    events: list[dict] = []
    p, fake, _ = make_pipeline(tmp_path, events)
    item = chart_item()
    ask_returns_chart(fake, item)

    async def go():
        async with client(make_app(private, p)) as c:
            await login(c, private.admin)
            r = await ask(c, {"question": CHART_QUESTION})
            assert r.status_code == 200, r.text
            assert r.json()["charts"] == [item]
    run(go())
    p.close()
    raw = (private.dir / "web_log.jsonl").read_text(encoding="utf-8")
    entry = [e for e in log_events(private) if e["event"] == "ask"][0]
    assert entry["status"] == "ok" and set(entry["detail"]["jsonl"]) >= {"charts_talk", "queries"}
    assert "charts" not in entry["detail"]["jsonl"] and "charts" not in entry
    for held in (CHART_LABEL, item["svg_b64"], "svg_b64", item["chart"]["title"]):
        assert held not in raw, held
    assert CHART_LABEL not in json.dumps(events)


def test_svg_b64_has_no_markup(private):
    """The job reply's picture is base64 only, whatever a label holds: no ``<`` a page could ever parse."""
    item = chart_item("Status <script>alert(1)</script>")

    async def go():
        async with client(make_app(private, ChartPipeline([item]))) as c:
            await login(c, private.friend)
            return (await ask(c, {"question": CHART_QUESTION})).json()
    picture = run(go())["charts"][0]["svg_b64"]
    assert "<" not in picture and ">" not in picture and re.fullmatch(r"[A-Za-z0-9+/]+=*", picture)
    svg = base64.b64decode(picture).decode("utf-8")
    assert svg.startswith("<svg ") and "<script" not in svg and "&lt;script" in svg


def test_shape_moves_charts(tmp_path):
    """``_shape`` moves the key and draws nothing: out of the record for the log, into the reply, the same object."""
    events: list[dict] = []
    p, fake, _ = make_pipeline(tmp_path, events)
    try:
        assert "charts" not in p.answer("How many?", "merged")
        item = chart_item()
        ask_returns_chart(fake, item)
        out = p.answer(CHART_QUESTION, "merged")
        assert out["charts"] == [item] and out["charts"][0] is item
        assert "charts" not in out["log"]["jsonl"] and out["log"]["jsonl"]["charts_talk"] is True
        assert "charts" not in out["trace"]
        assert CHART_LABEL not in json.dumps(out["log"]) and CHART_LABEL not in json.dumps(out["trace"])
    finally:
        p.close()


@pytest.mark.skipif(not (ROOT / "knowledge" / "objects").is_dir(),
                    reason="the git-ignored knowledge files knowledge/objects/*.json are not there")
@pytest.mark.skipif(os.path.exists("/data/warehouse.read.duckdb"), reason="a read copy exists here: records would open")
def test_pipeline_builds_from_the_package_without_a_read_copy(tmp_path):
    """The container's start-up on the real package, prompts and knowledge files, records off: no model call, no data."""
    events: list[dict] = []
    p = pipeline.Pipeline.from_home(str(ROOT), "/data/warehouse.read.duckdb", log=events.append,
                                    knowledge_root=str(tmp_path))
    try:
        built = [e for e in events if e["event"] == "knowledge_built"][0]
        live = sorted(x.name for x in (ROOT / "knowledge" / "objects").glob("*.json") if not x.name.endswith(".raw.json"))
        assert built["backend"] == "rank_bm25" and built["files"] == len(live) and built["objects"] == len(live)
        copied = sorted(os.listdir(p.knowledge_dir))
        assert [x for x in copied if x != "_components"] == live and p.knowledge_dir.startswith(str(tmp_path))
        # the flow components are copied with the knowledge (a subfolder: no *.json of the catalog folder) and the
        # knowledge agent's component tools read that copy
        live_components = ROOT / "knowledge" / "components"
        if (live_components / "index.json").is_file():
            comp = Path(p.knowledge_dir) / "_components"
            assert "_components" in copied and (comp / "index.json").read_bytes() == \
                (live_components / "index.json").read_bytes()
            assert sorted(x.name for x in (comp / "flows").glob("*.json")) == \
                sorted(x.name for x in (live_components / "flows").glob("*.json"))
            assert p.registry.components is not None and p.registry.components.knowledge.folder == str(comp)
            assert built["components"] == p.registry.components.knowledge.counts()
            assert p.system.endswith(sys.modules["sf_assistant.agent.knowledge_prompts"].KNOWLEDGE_COMPONENTS_NOTE)
        else:
            assert "_components" not in copied and p.registry.components is None
        assert p.session is None and "no read copy" in (p.records_error or "")
        import sf_assistant.cli
        assert p.ask_mod is sf_assistant.cli
        for name in ("sf_assistant.agent.lean", "sf_assistant.agent.render", "sf_assistant.agent.record_session",
                     "sf_assistant.agent.record_prompts", "sf_assistant.tools.record_tools", "openai"):
            assert name in sys.modules, name
        src = ROOT / "src" / "sf_assistant"
        assert os.path.dirname(os.path.realpath(sys.modules["sf_assistant.agent.lean"].__file__)) == str(src / "agent")
        assert p.rcfg.warehouse == "/data/warehouse.read.duckdb"
    finally:
        p.close()
    assert not os.path.exists(p.knowledge_dir)


def test_preload_imports_every_wired_module_and_reads_every_prompt():
    from sf_assistant import prompts

    names = pipeline.preload_package()
    assert "sf_assistant.agent.record_session" in names and "sf_assistant.engine.warehouse" in names
    assert not set(pipeline.NOT_WIRED) & set(names)
    # pipeline.build / open_record_session build the term resolver (unless SFA_RESOLVER=off): preloaded like the rest
    assert {"sf_assistant.knowledge.resolver", "sf_assistant.knowledge.terms", "sf_assistant.engine.vocabulary"} <= \
        set(names)
    assert all(name in sys.modules for name in names)
    assert set(prompts._cache) >= set(prompts.ids())   # every prompt file read once, at start-up


def test_import_package_refuses_another_home(tmp_path, monkeypatch):
    import sf_assistant  # noqa: F401 - already imported (conftest): a second folder can never replace it

    monkeypatch.setattr(sys, "path", list(sys.path))
    with pytest.raises(RuntimeError, match="no src/sf_assistant"):
        pipeline.import_package(str(tmp_path))
    fake = tmp_path / "src" / "sf_assistant"
    fake.mkdir(parents=True)
    (fake / "__init__.py").write_text("")
    with pytest.raises(RuntimeError, match="imported from"):
        pipeline.import_package(str(tmp_path))
    assert pipeline.import_package(str(ROOT)).HOME == ROOT


def test_read_copy_allowance_keeps_the_writer_file_refused():
    from sf_assistant.config import CONTAINER_SOURCE, FORBIDDEN_SOURCE
    from sf_assistant.engine.snapshot import check_warehouse_path
    from sf_assistant.engine.types import RecordsUnavailable

    module = types.SimpleNamespace(check_warehouse_path=check_warehouse_path)
    pipeline.allow_read_copy(module, CONTAINER_SOURCE, FORBIDDEN_SOURCE, RecordsUnavailable)
    pipeline.allow_read_copy(module, CONTAINER_SOURCE, FORBIDDEN_SOURCE, RecordsUnavailable)  # idempotent
    for path in (FORBIDDEN_SOURCE, "/data/../data/warehouse.duckdb", "/data/other.duckdb", "/data/",
                 "/var/lib/docker/volumes/sf-local-ai_data/_data/warehouse.read.duckdb"):
        with pytest.raises(RecordsUnavailable, match="refusing"):
            module.check_warehouse_path(path)
    # the read copy passes the path rule (on the host it does not exist, so only the existence check fails)
    with pytest.raises(RecordsUnavailable, match="no read copy"):
        module.check_warehouse_path(CONTAINER_SOURCE)
    assert module.check_warehouse_path(CONTAINER_SOURCE, must_exist=False) == CONTAINER_SOURCE


def test_compose_mounts_the_package_read_only_and_keeps_the_log_folder():
    text = (WEB / "compose.yaml").read_text()
    web = text.split("\n  sf-assistant-web:\n", 1)[1].split("\n  sf-assistant-mcp:\n", 1)[0]
    assert "object_knowledge_eval" not in web and "SFA_CODE_DIR" not in web
    assert "SFA_HOME: /srv/sfa" in web and "PYTHONPATH: /srv/sfa/src" in web
    home = web.split("source: ..\n", 1)[1].split("- type:", 1)[0]
    assert "target: /srv/sfa" in home and "read_only: true" in home
    private = web.split("source: ../.private\n", 1)[1].split("- type:", 1)[0]
    assert "target: /srv/private" in private and "read_only" not in private   # web_log.jsonl is appended here
    assert "SFA_PRIVATE_DIR: /srv/private" in web and "SFA_WAREHOUSE: /data/warehouse.read.duckdb" in web


def _service(name: str) -> str:
    text = (WEB / "compose.yaml").read_text()
    body = text.split(f"\n  {name}:\n", 1)[1]
    return re.split(r"\n  [a-z][\w-]*:\n|\nvolumes:\n", body, maxsplit=1)[0]


def test_compose_passes_every_page_setting_to_the_container():
    """Every SFA_* variable WebSettings.from_env reads reaches the container (runbook "Page settings"), and an unset
    one arrives empty, which from_env reads as its own default."""
    import inspect

    src = inspect.getsource(server.WebSettings.from_env)
    read = set(re.findall(r'"(SFA_[A-Z_]+)"', src))
    assert {"SFA_TIMEOUT_S", "SFA_QUEUE_WAIT_S", "SFA_RATE_PER_HOUR", "SFA_SESSION_HOURS", "SFA_COOKIE_SECURE"} <= read
    env = _service("sf-assistant-web").split("    environment:\n", 1)[1].split("    volumes:\n", 1)[0]
    given = set(re.findall(r"^\s+(SFA_[A-Z_]+):", env, re.MULTILINE))
    assert read <= given, sorted(read - given)
    for name in ("SFA_TIMEOUT_S", "SFA_QUEUE_WAIT_S", "SFA_RATE_PER_HOUR", "SFA_SESSION_HOURS", "SFA_COOKIE_SECURE"):
        assert f'{name}: "${{{name}:-}}"' in env
    with_empty = {n: "" for n in ("SFA_TIMEOUT_S", "SFA_QUEUE_WAIT_S", "SFA_RATE_PER_HOUR", "SFA_SESSION_HOURS",
                                  "SFA_COOKIE_SECURE")}
    old = {n: os.environ.get(n) for n in with_empty}
    os.environ.update(with_empty)
    try:
        ws = server.WebSettings.from_env()
    finally:
        for n, v in old.items():
            if v is None:
                os.environ.pop(n, None)
            else:
                os.environ[n] = v
    assert (ws.timeout_s, ws.queue_wait_s, ws.rate_per_hour, ws.session_hours, ws.cookie_secure) == (
        240.0, 600.0, 20, 12.0, False)


def test_compose_passes_the_spelling_switch_to_both_containers_and_empty_means_on(monkeypatch):
    """SFA_RESOLVER reaches the page and the MCP server (docs/runbook.md "The spelling help"); unset, it arrives empty,
    which reads as on (the default since 2026-10-27); off turns it off. The word lists are the image's (no
    SFA_LEXICON)."""
    from sf_assistant.config import load_resolver_config, resolver_settings

    for svc in ("sf-assistant-web", "sf-assistant-mcp"):
        env = _service(svc).split("    environment:\n", 1)[1].split("    volumes:\n", 1)[0]
        assert 'SFA_RESOLVER: "${SFA_RESOLVER:-}"' in env, svc
        assert "SFA_LEXICON" not in env, svc
        assert "empty = on" in env and "empty = off" not in env, svc     # the comment says what empty does
    monkeypatch.setenv("SFA_RESOLVER", "")
    assert load_resolver_config().enabled is True and resolver_settings().enabled is True
    monkeypatch.setenv("SFA_RESOLVER", "off")
    assert load_resolver_config().enabled is False and resolver_settings().enabled is False
    monkeypatch.setenv("SFA_RESOLVER", "on")
    assert load_resolver_config().enabled is True


def test_the_page_opens_every_record_session_with_its_one_settings_switch(tmp_path):
    """The spelling help has one source of truth on the page: ``settings.resolver`` of from_home goes to every
    open_record_session (start and each reopen after a swap); a Pipeline made without it passes nothing (the env)."""
    from sf_assistant.config import ResolverConfig

    class Recording(FakeAsk):
        def __init__(self):
            super().__init__()
            self.kwargs: list[dict] = []

        def open_record_session(self, cfg, rcfg, knowledge_dir=None, **kw):
            self.kwargs.append(kw)
            return super().open_record_session(cfg, rcfg, knowledge_dir)

    FakeSession.open_now = 0
    wh = tmp_path / "warehouse.read.duckdb"
    wh.write_text("v1")
    fake = Recording()
    rc = ResolverConfig(enabled=True, lexicon=("words.txt",))
    p = pipeline.Pipeline(fake, types.SimpleNamespace(model="m", base_url="http://localhost:8000/v1"), None, None,
                          None, types.SimpleNamespace(warehouse=str(wh)), resolver=rc)
    replace_file(wh, "v2")
    p.ensure_fresh()
    assert fake.kwargs == [{"resolver": rc}, {"resolver": rc}] and FakeSession.open_now == 1
    (tmp_path / "plain").mkdir()
    plain, plain_fake, _wh = make_pipeline(tmp_path / "plain", [])
    assert plain.resolver is None and plain_fake.sessions


def test_run_sh_refuses_a_bad_spelling_switch_before_starting_anything():
    """A typo in SFA_RESOLVER never reaches the page or the MCP server: ./run.sh up and up-mcp check it first (the
    function alone is run here; nothing is started)."""
    script = (WEB / "run.sh").read_text(encoding="utf-8")
    fn = re.search(r"^check_switch\(\) \{.*?^\}", script, re.S | re.M).group(0)
    for cmd in ("up)\n    check_switch\n", "up-mcp)\n    check_switch\n"):
        assert cmd in script, cmd
    for value, code in (("", 0), ("off", 0), ("ON", 0), (" on ", 0), ("yes", 2), ("1", 2), ("maybe", 2)):
        out = subprocess.run(["bash", "-c", fn + "\ncheck_switch"], env={"PATH": "/usr/bin:/bin", "SFA_RESOLVER": value},
                             capture_output=True, text=True)
        assert out.returncode == code, (value, out.stderr)
        assert ("must be off, on or empty" in out.stderr) is bool(code)


def test_compose_gives_the_mcp_container_only_its_own_private_files():
    """The MCP container (on the LAN) sees three files of .private, never the web log, the codes, the question sets,
    the run folders or the snapshot copy; only its audit log is writable."""
    mcp = _service("sf-assistant-mcp")
    blocks = mcp.split("    volumes:\n", 1)[1].split("    healthcheck:\n", 1)[0].split("- type: ")[1:]
    mounts = [(re.search(r"source: (\S+)", b).group(1), re.search(r"target: (\S+)", b).group(1),
               "read_only: true" in b) for b in blocks]
    private = {m for m in mounts if ".private" in m[0] + m[1]}
    assert private == {("../.private/mcp_token.json", "/srv/sfa/.private/mcp_token.json", True),
                       ("../.private/mcp_audit_key", "/srv/sfa/.private/mcp_audit_key", True),
                       ("../.private/mcp_log.jsonl", "/srv/sfa/.private/mcp_log.jsonl", False)}
    assert "source: ..\n" not in mcp and "source: ../.private\n" not in mcp
    run_sh = (WEB / "run.sh").read_text(encoding="utf-8")
    assert "for f in mcp_log.jsonl mcp_token.json; do" in run_sh and "mcp_audit_key" in run_sh


def test_run_sh_warns_while_the_host_firewall_is_off():
    """up, up-mcp and new-mcp-token warn (stderr only) while /etc/ufw/ufw.conf says ENABLED=no (review 2026-10-20:
    the docs had said ufw was active)."""
    run_sh = (WEB / "run.sh").read_text(encoding="utf-8")
    assert "grep -qsx 'ENABLED=no' /etc/ufw/ufw.conf" in run_sh
    body = run_sh.split('case "${1:-}" in', 1)[1]
    for command in ("  up)\n", "  up-mcp)\n", "  new-mcp-token)\n"):
        branch = body.split(command, 1)[1].split(";;", 1)[0]
        assert "firewall_note" in branch, command
    func = run_sh.split("firewall_note() {", 1)[1].split("\n}\n", 1)[0]
    assert all(">&2" in line for line in func.splitlines() if "echo" in line)
