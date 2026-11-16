"""sf-assistant web page: the Salesforce assistant (the sf_assistant package) behind a small LAN page.

Endpoints (JSON; every POST needs Content-Type application/json):

    POST /api/login   {code}               -> {role: friend|admin} + HttpOnly SameSite=Strict session cookie
    POST /api/logout                        -> {ok}
    GET  /api/status                        -> model / records / queue / limit / job state (role null when signed out)
    POST /api/ask     {question, mode?}     -> 202 {ticket, position}: the question is queued
    GET  /api/job                           -> {job}: the question of this device, with its answer once done
                                               (answer, kind, source_line, data_ts, seconds, queue_wait_s, charts?,
                                               trace?)

A question is answered in the background: the page polls /api/status and fetches /api/job when its
question is done, so a reload, a phone's locked screen or a dropped connection never loses the answer.
The question and the last answer (kept RESULT_KEEP_S) belong to the "device": the role plus the client
address that signed in. The hourly allowance and the one-waiting-question rule are per device too, so
signing in again, or in a second window, neither resets the allowance nor queues a second question.

``mode`` (merged | records | knowledge) and ``trace`` (the "How it was answered" panel) are admin only.
One question runs at a time (asyncio.Lock, FIFO); a question runs in one worker thread with stdout
captured and gets SFA_TIMEOUT_S (240 s) before it ends with a timeout. A thread cannot be killed: after
a timeout the question keeps running and the lock stays held until it ends; the next question waits for it.

Run in the container by ``python server.py`` (compose.yaml); settings come from SFA_* env vars.
"""
from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import logging
import os
import secrets
import sys
import threading
import time
import traceback
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import codes as codes_mod  # noqa: E402
import pipeline as pipeline_mod  # noqa: E402

log = logging.getLogger("sf-assistant")

COOKIE = "sfa_session"
MAX_BODY = 16 * 1024
LOGIN_FAILS, LOGIN_WINDOW_S = 10, 900.0      # wrong codes per client address per 15 minutes
LOGIN_TRACKED = 1000                         # client addresses whose wrong codes are remembered
VERIFY_SLOTS = 2                             # code checks (scrypt, 16 MiB each) running at once
MAX_SESSIONS_PER_ROLE = 50                   # a new sign-in beyond this ends the oldest session of the same role
MAX_DEVICES = 1000
RESULT_KEEP_S = 900.0                        # a finished question stays fetchable this long
STUCK_S = 1800.0                             # health check fails when a question runs this long past its timeout
MODEL_CHECK_TTL_S = 15.0
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "font-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), interest-cohort=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}


# ---------------------------------------------------------------- settings

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


@dataclass
class WebSettings:
    """The page's own settings (SFA_BIND, SFA_PORT, SFA_PRIVATE_DIR ...); not sf_assistant.config.Settings.

    ``home`` is the sf_assistant folder (``SFA_HOME``) the pipeline imports the package from (``<home>/src``).
    """

    private_dir: str
    home: str
    warehouse: str
    bind: str = "127.0.0.1"
    port: int = 8600
    timeout_s: float = 240.0             # per question, then a timeout
    queue_wait_s: float = 600.0          # longest wait for the lock, then "busy"
    rate_per_hour: int = 20              # questions per device (role + client address) per rolling hour
    max_question_chars: int = 1000
    session_hours: float = 12.0
    allowed_hosts: list[str] = field(default_factory=list)
    cookie_secure: bool = False          # plain HTTP on the LAN; set SFA_COOKIE_SECURE=1 behind TLS

    @classmethod
    def from_env(cls) -> "WebSettings":
        bind = os.environ.get("SFA_BIND") or "127.0.0.1"
        extra = [h.strip() for h in (os.environ.get("SFA_ALLOWED_HOSTS") or "").split(",") if h.strip()]
        return cls(
            private_dir=os.environ.get("SFA_PRIVATE_DIR") or os.path.join(os.path.dirname(HERE), ".private"),
            home=os.environ.get("SFA_HOME") or os.path.dirname(HERE),
            warehouse=os.environ.get("SFA_WAREHOUSE") or "/data/warehouse.read.duckdb",
            bind=bind, port=int(_env_float("SFA_PORT", 8600)),
            timeout_s=_env_float("SFA_TIMEOUT_S", 240.0), queue_wait_s=_env_float("SFA_QUEUE_WAIT_S", 600.0),
            rate_per_hour=int(_env_float("SFA_RATE_PER_HOUR", 20)),
            session_hours=_env_float("SFA_SESSION_HOURS", 12.0),
            allowed_hosts=[bind, "localhost", "127.0.0.1", *extra],
            cookie_secure=os.environ.get("SFA_COOKIE_SECURE", "") == "1")


# ---------------------------------------------------------------- state

@dataclass
class WebSession:
    key: str                     # sha256 of the cookie token (the token itself is never stored)
    role: str
    generation: str
    created: float
    expires: float
    device: tuple[str, str]      # (role, client address at sign-in)


@dataclass
class Job:
    """One question: queued, running, then done (200) or failed (500 / 503 / 504) or abandoned."""
    ticket: int
    question: str
    mode: str
    role: str
    session: str                 # short key of the session that asked
    generation: str              # of the codes it was asked under: rotating the codes drops it
    asked_at: float              # time.time()
    state: str = "waiting"       # waiting | running | done | failed | abandoned
    status: int | None = None
    result: dict | None = None   # the answer; ``trace`` only for the admin
    error: str | None = None
    extra: dict = field(default_factory=dict)
    finished_at: float | None = None
    task: Any = None

    @property
    def active(self) -> bool:
        return self.state in ("waiting", "running")

    def finish(self, status: int | None, *, result: dict | None = None, error: str | None = None,
               state: str | None = None, **extra: Any) -> None:
        self.state = state or ("done" if status == 200 else "failed")
        self.status, self.result, self.error, self.extra = status, result, error, extra
        self.finished_at = time.time()


@dataclass
class Device:
    """What one role on one client address shares across its sessions: the allowance and the question."""
    key: tuple[str, str]
    asks: deque = field(default_factory=deque)
    job: Job | None = None
    seen: float = 0.0


class ErrorReply(Exception):
    """A client error with a message that is safe to show."""

    def __init__(self, status: int, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra


def http_model_check(base_url: str, api_key: str, model: str, timeout: float = 3.0) -> bool:
    """GET <base_url>/models (no generation): True when the server answers and lists ``model``."""
    req = urllib.request.Request(base_url.rstrip("/") + "/models", headers={"Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed local endpoint
            data = json.load(resp)
    except Exception:  # noqa: BLE001
        return False
    ids = [m.get("id") for m in (data.get("data") or []) if isinstance(m, dict)]
    return model in ids if model else bool(ids)


class WebState:
    def __init__(self, settings: WebSettings, pipeline: Any = None, pipeline_factory: Callable | None = None,
                 model_check: Callable[[], bool] | None = None) -> None:
        self.settings = settings
        self.pipeline = pipeline
        self.pipeline_factory = pipeline_factory
        self.pipeline_error: str | None = None
        self.model_check = model_check
        self.lock = asyncio.Lock()
        self.verify_slots = asyncio.Semaphore(VERIFY_SLOTS)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ask")
        self.sessions: dict[str, WebSession] = {}
        self.devices: dict[tuple[str, str], Device] = {}
        self.tasks: set[asyncio.Task] = set()
        self.waiting: list[int] = []
        self.running: int | None = None
        self.late: bool = False          # the running question passed its timeout and still holds the lock
        self.late_since: float | None = None
        self._ticket = 0
        self.login_fails: dict[str, deque] = {}
        self._codes: tuple[int | None, dict | None] = (None, None)
        self._model: tuple[float, bool] = (0.0, False)
        self._log_lock = threading.Lock()
        os.makedirs(settings.private_dir, mode=0o700, exist_ok=True)
        self.log_path = os.path.join(settings.private_dir, "web_log.jsonl")

    # ---- web log (.private/web_log.jsonl, mode 600; holds real names)
    def log_event(self, event: dict) -> None:
        line = json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **event}, ensure_ascii=False, default=str)
        with self._log_lock:
            try:
                fd = os.open(self.log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                log.exception("could not write %s", self.log_path)

    # ---- codes and sessions
    def codes(self) -> dict | None:
        path = os.path.join(self.settings.private_dir, "codes.json")
        try:
            mtime = os.stat(path).st_mtime_ns
        except OSError:
            self._codes = (None, None)
            return None
        if mtime != self._codes[0]:
            self._codes = (mtime, codes_mod.load_codes(path))
        return self._codes[1]

    def session_of(self, request: Request) -> WebSession | None:
        token = request.cookies.get(COOKIE)
        if not token or len(token) > 200:
            return None
        key = hashlib.sha256(token.encode("utf-8", "replace")).hexdigest()
        sess = self.sessions.get(key)
        if sess is None:
            return None
        record = self.codes()
        if time.time() > sess.expires or record is None or record.get("generation") != sess.generation:
            self.sessions.pop(key, None)   # expired, or the codes were rotated (run.sh new-codes)
            return None
        return sess

    def new_session(self, role: str, generation: str, client: str) -> tuple[str, WebSession]:
        now = time.time()
        for key in [k for k, s in self.sessions.items() if s.expires < now]:
            self.sessions.pop(key, None)
        same_role = sorted((s.created, k) for k, s in self.sessions.items() if s.role == role)
        while len(same_role) >= MAX_SESSIONS_PER_ROLE:   # never ends a session of the other role
            self.sessions.pop(same_role.pop(0)[1], None)
        token = secrets.token_urlsafe(32)
        key = hashlib.sha256(token.encode()).hexdigest()
        sess = WebSession(key=key, role=role, generation=generation, created=now,
                          expires=now + self.settings.session_hours * 3600, device=(role, client))
        self.sessions[key] = sess
        return token, sess

    def login_blocked(self, client: str) -> bool:
        fails = self.login_fails.get(client)
        now = time.monotonic()
        while fails and now - fails[0] > LOGIN_WINDOW_S:
            fails.popleft()
        return bool(fails) and len(fails) >= LOGIN_FAILS

    def login_attempt(self, client: str) -> None:
        """Count an attempt as wrong before its code is checked (:meth:`login_ok` takes it back)."""
        self.login_fails.setdefault(client, deque()).append(time.monotonic())
        if len(self.login_fails) > LOGIN_TRACKED:
            now = time.monotonic()
            for addr in [a for a, d in self.login_fails.items() if not d or now - d[-1] > LOGIN_WINDOW_S]:
                del self.login_fails[addr]
            if len(self.login_fails) > LOGIN_TRACKED:   # drop the addresses whose last wrong code is oldest
                by_age = sorted(self.login_fails, key=lambda a: self.login_fails[a][-1])
                for addr in by_age[: len(self.login_fails) - LOGIN_TRACKED]:
                    if addr != client:
                        del self.login_fails[addr]

    def login_ok(self, client: str) -> None:
        fails = self.login_fails.get(client)
        if fails:
            fails.pop()

    # ---- devices: allowance and question
    def device(self, key: tuple[str, str]) -> Device:
        dev = self.devices.get(key)
        if dev is None:
            if len(self.devices) >= MAX_DEVICES:
                self._prune_devices()
            dev = self.devices[key] = Device(key=key)
        dev.seen = time.time()
        return dev

    def _prune_devices(self) -> None:
        now = time.time()
        idle = []
        for key, dev in self.devices.items():
            while dev.asks and now - dev.asks[0] > 3600:
                dev.asks.popleft()
            if not dev.asks and (dev.job is None or not dev.job.active):
                idle.append((dev.seen, key))
        for _, key in sorted(idle)[: max(1, len(self.devices) - MAX_DEVICES + 1)]:
            self.devices.pop(key, None)

    def current_job(self, dev: Device) -> Job | None:
        """The device's question, unless it expired or was asked under codes that were rotated since."""
        job = dev.job
        if job is None:
            return None
        expired = not job.active and job.finished_at and time.time() - job.finished_at > RESULT_KEEP_S
        if expired or job.generation != (self.codes() or {}).get("generation"):
            dev.job = job = None   # a rotated-out question still running ends and is logged, but is not shown
        return job

    def limit_state(self, dev: Device) -> dict:
        now = time.time()
        while dev.asks and now - dev.asks[0] > 3600:
            dev.asks.popleft()
        used = len(dev.asks)
        reset = int(3600 - (now - dev.asks[0])) + 1 if dev.asks else 0
        return {"per_hour": self.settings.rate_per_hour, "used": used,
                "left": max(0, self.settings.rate_per_hour - used), "reset_in_s": reset}

    # ---- queue
    def next_ticket(self) -> int:
        self._ticket += 1
        return self._ticket

    def position(self, ticket: int | None) -> int | None:
        """Questions ahead of ``ticket`` (0 = being answered), None when it is not queued."""
        if ticket is None:
            return None
        if ticket == self.running:
            return 0
        if ticket in self.waiting:
            return self.waiting.index(ticket) + (1 if self.running is not None else 0)
        return None

    def queue_length(self) -> int:
        return len(self.waiting) + (1 if self.running is not None else 0)

    def stuck(self) -> bool:
        return self.late and self.late_since is not None and time.monotonic() - self.late_since > STUCK_S

    def job_brief(self, job: Job | None) -> dict | None:
        if job is None:
            return None
        return {"ticket": job.ticket, "state": job.state, "position": self.position(job.ticket) if job.active else None}

    def job_view(self, job: Job | None, role: str) -> dict | None:
        if job is None:
            return None
        view = {**self.job_brief(job), "question": job.question, "age_s": round(time.time() - job.asked_at, 1),
                "status": job.status, "error": job.error, "extra": dict(job.extra), "result": None}
        if job.result is not None:
            view["result"] = dict(job.result) if role == "admin" else {k: v for k, v in job.result.items() if k != "trace"}
        if role == "admin":
            view["mode"] = job.mode
        return view

    async def model_ok(self) -> bool:
        if self.model_check is None:
            return False
        at, ok = self._model
        if time.monotonic() - at < MODEL_CHECK_TTL_S:
            return ok
        ok = bool(await asyncio.to_thread(self.model_check))
        self._model = (time.monotonic(), ok)
        return ok


# ---------------------------------------------------------------- helpers

def _error(status: int, message: str, **extra: Any) -> JSONResponse:
    resp = JSONResponse({"error": message, **extra}, status_code=status)
    # also on the catch-all 500, which Starlette sends from outside the security_headers middleware
    resp.headers.update(SECURITY_HEADERS)
    resp.headers["Cache-Control"] = "no-store"
    return resp


async def _read_json(request: Request) -> dict:
    ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if ctype != "application/json":
        raise ErrorReply(415, "Send JSON.")
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc != (request.headers.get("host") or ""):
        raise ErrorReply(403, "Cross-site requests are not accepted.")
    length = request.headers.get("content-length")
    if length and (not length.isdigit() or int(length) > MAX_BODY):
        raise ErrorReply(413, "The request is too large.")
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY:
            raise ErrorReply(413, "The request is too large.")
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        raise ErrorReply(400, "The request is not valid JSON.") from None
    if not isinstance(data, dict):
        raise ErrorReply(400, "The request is not valid JSON.")
    return data


def _client(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _short(key: str) -> str:
    return key[:12]


# ---------------------------------------------------------------- app

def create_app(settings: WebSettings | None = None, *, pipeline: Any = None, pipeline_factory: Callable | None = None,
               model_check: Callable[[], bool] | None = None) -> FastAPI:
    """The app. Tests pass a fake ``pipeline``; the container passes ``pipeline_factory`` (built at startup)."""
    settings = settings or WebSettings.from_env()
    state = WebState(settings, pipeline=pipeline, pipeline_factory=pipeline_factory, model_check=model_check)
    static_dir = os.path.join(HERE, "static")

    def build_pipeline() -> None:
        t0 = time.perf_counter()
        try:
            state.pipeline = state.pipeline_factory(log=state.log_event)
            st = state.pipeline.status()
            state.log_event({"event": "startup", "ok": True, "seconds": round(time.perf_counter() - t0, 2),
                             "status": st})
            log.info("pipeline ready in %.1f s: %s", time.perf_counter() - t0, st.get("status_line") or st.get("records_error"))
        except BaseException as exc:  # noqa: BLE001 - also SystemExit from sf_assistant.pipeline.build()
            state.pipeline_error = f"{type(exc).__name__}: {str(exc)[:300]}"
            state.log_event({"event": "startup", "ok": False, "error": state.pipeline_error})
            log.error("pipeline could not be built:\n%s", traceback.format_exc())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if state.pipeline is None and state.pipeline_factory is not None:
            threading.Thread(target=build_pipeline, name="build", daemon=True).start()
        yield
        for task in list(state.tasks):
            task.cancel()
        state.executor.shutdown(wait=False, cancel_futures=True)
        if state.pipeline is not None and hasattr(state.pipeline, "close"):
            try:
                state.pipeline.close()
            except Exception:  # noqa: BLE001
                pass

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.web = state
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts or ["*"])

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if request.url.path.startswith("/api/") or request.url.path == "/":
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ErrorReply)
    async def _error_reply(request: Request, exc: ErrorReply):
        return _error(exc.status, exc.message, **exc.extra)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        return _error(exc.status_code, {404: "Not found.", 405: "Method not allowed."}.get(exc.status_code, "Request failed."))

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception):
        log.error("unexpected error on %s:\n%s", request.url.path, traceback.format_exc())
        return _error(500, "Something went wrong on the server. Try again.")

    # ---- page
    @app.get("/")
    async def index():
        return FileResponse(os.path.join(static_dir, "index.html"), media_type="text/html")

    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    # ---- auth
    @app.post("/api/login")
    async def login(request: Request):
        client = _client(request)
        data = await _read_json(request)
        if state.login_blocked(client):
            raise ErrorReply(429, "Too many wrong codes from this device. Wait 15 minutes and try again.")
        code = data.get("code")
        if not isinstance(code, str) or not code.strip() or len(code) > codes_mod.MAX_CODE_CHARS:
            state.login_attempt(client)
            raise ErrorReply(401, "That code is not valid.")
        record = state.codes()
        if record is None:
            raise ErrorReply(503, "No access codes are set up yet. The owner creates them with ./run.sh new-codes.")
        # counted before the check (no await in between): concurrent attempts cannot all pass login_blocked()
        state.login_attempt(client)
        async with state.verify_slots:
            role = await asyncio.to_thread(codes_mod.verify, record, code)
        if role is None:
            state.log_event({"event": "login", "ok": False, "client": client})
            raise ErrorReply(401, "That code is not valid.")
        state.login_ok(client)
        old = state.session_of(request)
        if old is not None:
            state.sessions.pop(old.key, None)
        token, sess = state.new_session(role, record["generation"], client)
        state.log_event({"event": "login", "ok": True, "role": role, "client": client, "session": _short(sess.key)})
        response = JSONResponse({"role": role})
        response.set_cookie(COOKIE, token, max_age=int(settings.session_hours * 3600), path="/", httponly=True,
                            samesite="strict", secure=settings.cookie_secure)
        return response

    @app.post("/api/logout")
    async def logout(request: Request):
        await _read_json(request)
        sess = state.session_of(request)
        if sess is not None:
            state.sessions.pop(sess.key, None)
            state.log_event({"event": "logout", "role": sess.role, "session": _short(sess.key)})
            dev = state.devices.get(sess.device)
            if dev is not None and dev.job is not None and not any(s.device == sess.device for s in state.sessions.values()):
                # signing out leaves nothing to fetch: a waiting question is dropped; a running one cannot be
                # stopped, it ends and is logged, but its answer is no longer kept for this device
                if dev.job.state == "waiting" and dev.job.task is not None:
                    dev.job.task.cancel()
                dev.job = None
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, path="/", httponly=True, samesite="strict", secure=settings.cookie_secure)
        return response

    # ---- status
    @app.get("/api/status")
    async def status(request: Request):
        sess = state.session_of(request)
        ready = state.pipeline is not None
        if sess is None:
            return {"role": None, "ready": ready, "failed": state.pipeline_error is not None, "stuck": state.stuck()}
        dev = state.device(sess.device)
        job = state.current_job(dev)
        p = state.pipeline.status() if ready else {}
        out = {
            "role": sess.role, "ready": ready, "starting": not ready and state.pipeline_error is None,
            "failed": state.pipeline_error is not None,
            "model": {"ok": await state.model_ok()},
            "records": {"ok": bool(p.get("records_ok")), "data_synced": p.get("data_synced")},
            "queue": {"length": state.queue_length(),
                      "your_position": state.position(job.ticket) if job is not None and job.active else None},
            "job": state.job_brief(job),
            "limit": state.limit_state(dev),
            "max_question_chars": settings.max_question_chars, "timeout_s": settings.timeout_s,
        }
        if sess.role == "admin":
            out["admin"] = {"status_line": p.get("status_line"), "records_error": p.get("records_error"),
                            "origin": p.get("origin"), "age_hours": p.get("age_hours"), "model": p.get("model"),
                            "pipeline_error": state.pipeline_error, "running_past_timeout": state.late}
        return out

    @app.get("/api/job")
    async def job_endpoint(request: Request):
        sess = state.session_of(request)
        if sess is None:
            raise ErrorReply(401, "Sign in first.")
        dev = state.device(sess.device)
        return {"job": state.job_view(state.current_job(dev), sess.role)}

    # ---- ask
    def finished(ticket: int, started: float, ctx: dict, fut: asyncio.Future) -> None:
        """Done callback of the worker future (event loop thread): the lock is released only here."""
        if state.running == ticket:
            state.running = None
        state.late, state.late_since = False, None
        state.lock.release()
        if not ctx.get("timed_out"):
            return
        event = {"event": "late_finish", "ticket": ticket, "session": ctx["session"], "role": ctx["role"],
                 "mode": ctx["mode"], "question": ctx["question"], "seconds": round(time.monotonic() - started, 2)}
        exc = None if fut.cancelled() else fut.exception()
        if fut.cancelled() or exc is not None:
            event["error"] = f"{type(exc).__name__}: {str(exc)[:200]}" if exc is not None else "cancelled"
        else:
            res = fut.result() or {}
            event.update({"answer": str(res.get("answer") or ""), "kind": res.get("kind"),
                          "source_line": res.get("source_line"), "data_ts": res.get("data_ts"),
                          "detail": res.get("log")})
        state.log_event(event)

    async def run_job(job: Job, base: dict) -> None:
        """Wait for the lock, answer in the worker thread, keep the outcome on ``job``. Never raises."""
        ticket = job.ticket
        entered = time.monotonic()
        try:
            try:
                async with asyncio.timeout(settings.queue_wait_s):
                    await state.lock.acquire()
            except TimeoutError:
                state.log_event({**base, "status": "busy", "queue_wait_s": round(time.monotonic() - entered, 2)})
                job.finish(503, error="The assistant is busy with other questions. Try again in a few minutes.")
                return
            except asyncio.CancelledError:   # signed out while waiting in line, or the server stops
                state.log_event({**base, "status": "abandoned", "queue_wait_s": round(time.monotonic() - entered, 2)})
                job.finish(None, state="abandoned", error="The question was dropped before it was answered.")
                return
            if ticket in state.waiting:
                state.waiting.remove(ticket)
            state.running = ticket
            job.state = "running"
            queue_wait = round(time.monotonic() - entered, 2)
            started = time.monotonic()
            ctx = {"session": job.session, "role": job.role, "mode": job.mode, "question": job.question,
                   "timed_out": False}
            try:
                fut = asyncio.get_running_loop().run_in_executor(state.executor, state.pipeline.answer,
                                                                 job.question, job.mode)
            except BaseException:
                state.running = None
                state.lock.release()
                raise
            fut.add_done_callback(functools.partial(finished, ticket, started, ctx))
            try:
                result = await asyncio.wait_for(asyncio.shield(fut), settings.timeout_s)
            except (TimeoutError, asyncio.CancelledError) as exc:
                # the thread cannot be stopped: it keeps the lock until it ends (finished() logs late_finish)
                ctx["timed_out"] = True
                seconds = round(time.monotonic() - started, 2)
                if isinstance(exc, asyncio.CancelledError):
                    state.log_event({**base, "status": "abandoned", "seconds": seconds, "queue_wait_s": queue_wait})
                    job.finish(503, error="The server is restarting. Ask again in a minute.")
                    return
                state.late, state.late_since = True, time.monotonic()
                state.log_event({**base, "status": "timeout", "seconds": seconds, "queue_wait_s": queue_wait})
                job.finish(504, error=f"No answer within {settings.timeout_s:.0f} seconds. The server is still working "
                                      "on it and the next question starts when it ends. Try a narrower question.",
                           seconds=seconds, queue_wait_s=queue_wait)
                return
            except Exception as exc:  # noqa: BLE001 - never sent to the client
                seconds = round(time.monotonic() - started, 2)
                log.error("question %s failed:\n%s", ticket, "".join(traceback.format_exception(exc)))
                state.log_event({**base, "status": "error", "seconds": seconds, "queue_wait_s": queue_wait,
                                 "error": f"{type(exc).__name__}: {str(exc)[:300]}"})
                job.finish(500, error="The question could not be answered because of a server error. Try again.")
                return
            seconds = round(time.monotonic() - started, 2)
            out = {"answer": str(result.get("answer") or ""), "kind": result.get("kind") or "none",
                   "source_line": result.get("source_line"), "data_ts": result.get("data_ts"),
                   "seconds": seconds, "queue_wait_s": queue_wait}
            if result.get("charts"):   # beside the answer, for friend and admin alike; never logged
                out["charts"] = result["charts"]
            if job.role == "admin":
                out["trace"] = result.get("trace") or {}
            state.log_event({**base, "status": "ok", "seconds": seconds, "queue_wait_s": queue_wait,
                             "answer": out["answer"], "kind": out["kind"], "source_line": out["source_line"],
                             "data_ts": out["data_ts"], "detail": result.get("log")})
            job.finish(200, result=out)
        except Exception as exc:  # noqa: BLE001 - the job must always end
            log.error("question %s: unexpected error:\n%s", ticket, "".join(traceback.format_exception(exc)))
            state.log_event({**base, "status": "error", "error": f"{type(exc).__name__}: {str(exc)[:300]}"})
            if job.active:
                job.finish(500, error="The question could not be answered because of a server error. Try again.")
        finally:
            if ticket in state.waiting:
                state.waiting.remove(ticket)

    @app.post("/api/ask")
    async def ask(request: Request):
        sess = state.session_of(request)
        if sess is None:
            raise ErrorReply(401, "Sign in first.")
        data = await _read_json(request)
        question = data.get("question")
        if not isinstance(question, str) or not question.strip():
            raise ErrorReply(400, "Type a question first.")
        question = question.strip()
        if len(question) > settings.max_question_chars:
            raise ErrorReply(400, f"The question is too long: {len(question)} characters, at most "
                                  f"{settings.max_question_chars}.")
        mode = "merged"
        if sess.role == "admin" and data.get("mode") not in (None, ""):
            mode = data.get("mode")
            if mode not in pipeline_mod.MODES:
                raise ErrorReply(400, "Unknown mode. Use merged, records or knowledge.")
        if state.pipeline is None:
            if state.pipeline_error:
                raise ErrorReply(503, "The assistant could not start. The owner can see why on the status line.")
            raise ErrorReply(503, "The assistant is still starting. Try again in a minute.")
        dev = state.device(sess.device)
        job = state.current_job(dev)
        if job is not None and job.active:
            raise ErrorReply(409, "Your earlier question is still being answered; it will appear here when it is done.",
                             ticket=job.ticket)
        limit = state.limit_state(dev)
        if limit["left"] <= 0:
            raise ErrorReply(429, f"You have asked {limit['used']} questions in the last hour. The next one is possible "
                                  f"in {max(1, round(limit['reset_in_s'] / 60))} min.", retry_after_s=limit["reset_in_s"])
        dev.asks.append(time.time())
        ticket = state.next_ticket()
        job = dev.job = Job(ticket=ticket, question=question, mode=mode, role=sess.role, session=_short(sess.key),
                            generation=sess.generation, asked_at=time.time())
        state.waiting.append(ticket)
        base = {"event": "ask", "ticket": ticket, "session": _short(sess.key), "role": sess.role,
                "client": _client(request), "mode": mode, "question": question}
        task = asyncio.get_running_loop().create_task(run_job(job, base))
        job.task = task
        state.tasks.add(task)
        task.add_done_callback(state.tasks.discard)
        return JSONResponse({"ticket": ticket, "position": state.position(ticket)}, status_code=202)

    return app


# ---------------------------------------------------------------- main

def health_code(http_status: int, data: Any) -> int:
    """0 (healthy) when /api/status answers 200 and the pipeline neither failed nor got stuck."""
    if http_status != 200 or not isinstance(data, dict):
        return 1
    return 1 if data.get("failed") or data.get("stuck") else 0


def _health(settings: WebSettings) -> int:
    """Container health check: the page answers /api/status, the pipeline was built and no question is stuck."""
    try:
        with urllib.request.urlopen(f"http://{settings.bind}:{settings.port}/api/status", timeout=5) as resp:  # noqa: S310
            return health_code(resp.status, json.load(resp))
    except Exception:  # noqa: BLE001
        return 1


def main(argv: list[str]) -> int:
    settings = WebSettings.from_env()
    if argv[:1] == ["--health"]:
        return _health(settings)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    import uvicorn
    from uvicorn.config import LOGGING_CONFIG

    log_config = json.loads(json.dumps(LOGGING_CONFIG))
    for handler in log_config.get("handlers", {}).values():
        handler["stream"] = "ext://sys.stderr"   # stdout belongs to ask()'s captured transcript

    def factory(log: Callable[[dict], None]):
        return pipeline_mod.Pipeline.from_home(settings.home, settings.warehouse, log=log)

    def model_check() -> bool:
        return http_model_check(os.environ.get("OKEVAL_BASE_URL") or "http://localhost:8000/v1",
                                os.environ.get("OKEVAL_API_KEY") or "EMPTY",
                                os.environ.get("OKEVAL_MODEL") or "Qwen/Qwen3.6-35B-A3B-NVFP4")

    app = create_app(settings, pipeline_factory=factory, model_check=model_check)
    uvicorn.run(app, host=settings.bind, port=settings.port, log_config=log_config, proxy_headers=False,
                server_header=False, access_log=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
