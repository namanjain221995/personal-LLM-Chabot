#!/usr/bin/env python3
"""The worker sentinel — Node 2's eyes and hands for the engine controller
(contract §6.4).

WHY THIS EXISTS. The worker rank of the TP=2 pair (``VLLM::Worker_TP1`` in
``sf-local-ai-worker-vllm-worker-1``) has no HTTP endpoint of its own: it is
``--headless``, and the only thing that ever noticed it dying was the head's
executor, four minutes and fifty-one seconds later on 2026-09-11 — first
apport held the 25 GiB corpse for 3 m 59 s writing a core report, then the
head's collective waited out its own timeout. Meanwhile the worker's Docker
healthcheck, which curls the HEAD's /health, saw 200 and was content.

The sentinel looks at the two things that actually tell the truth on this
node — the container's process table (is the rank process there?) and its
log stream (did a fatal signature just scroll past?) — and reports them
within one poll (≤ 5 s) in ``/state``, which the head controller reads every
5 s and turns into the ``worker_rank_dead`` trigger, one observation, no
confirmation needed. It is a SENSOR and an ACTUATOR, not an authority (v2
§6): it restarts the worker container (``docker restart -t 5``, SIGKILL
after 5 s, which also interrupts a core dump in progress) only on the
controller's ``POST /restart`` — so the worker is already waiting at the
rendezvous when the controller restarts the head. ``SENTINEL_AUTONOMOUS=1``
(a bounded self-restart, three an hour) exists only for a deployment without
a controller and is off by default and in production: two actors restarting
the pair on their own rules is the 2026-09-11 pattern this programme ends.

The rank process is matched as ``VLLM::Worker`` — its title while the worker
waits at the rendezvous — and ``VLLM::Worker_TP1`` once the parallel groups
exist (``rank_joined``); verified in the pinned image's
vllm/v1/executor/multiproc_executor.py setup_proc_title_and_log_prefix. A
waiting worker is never "absent".

Trust boundary: it binds to the RoCE rail-A address (``CLUSTER_WORKER_IP``),
a point-to-point link — but a local address, so every process on the worker
host can reach it too. ``POST /restart`` is accepted from ``CLUSTER_HEAD_IP``
alone, and when a shared token is configured EVERY endpoint that carries data
requires it (``/healthz`` is exempt for a local peer only: the container's
own healthcheck probes it). GETs are read-only.

Standard library only, ``python:3.12-slim``, bind-mounted next to
``common.py`` (shipped by scripts/cluster-sync.sh). Prefix every log line
with ``[sentinel]``.
"""
from __future__ import annotations

import hmac
import logging
import signal
import sys
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import List, Optional, Tuple

from common import (
    Budget,
    Clock,
    DockerClient,
    DockerError,
    DockerUnavailable,
    FATAL_SIGNATURES,
    configure_logging,
    env_bool,
    env_float,
    env_int,
    env_str,
    parse_docker_time,
    read_json_body,
    run_periodically,
    send_json,
    send_text,
    utc_stamp,
)

log = logging.getLogger("sentinel")


@dataclass
class Config:
    bind: str = "127.0.0.1"
    port: int = 9839
    worker_container: str = "sf-local-ai-worker-vllm-worker-1"
    head_ip: str = ""
    token: str = ""
    autonomous: bool = False
    poll_s: float = 5.0
    rank_process: str = "VLLM::Worker"
    rank_joined_suffix: str = "_TP"
    rank_grace_s: float = 120.0
    restart_timeout_s: int = 5
    self_restart_budget: int = 3
    self_restart_window_s: float = 3600.0
    diagnostics_lines: int = 400
    docker_socket: str = "/var/run/docker.sock"
    docker_api_version: str = "1.53"
    dry_run: bool = False

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            bind=env_str("SENTINEL_BIND", "127.0.0.1"),
            port=env_int("SENTINEL_PORT", 9839),
            worker_container=env_str("WORKER_CONTAINER", "sf-local-ai-worker-vllm-worker-1"),
            head_ip=env_str("CLUSTER_HEAD_IP", ""),
            token=env_str("CLUSTER_SENTINEL_TOKEN", ""),
            autonomous=env_bool("SENTINEL_AUTONOMOUS", False),
            poll_s=env_float("SENTINEL_POLL_S", 5.0),
            rank_process=env_str("WORKER_RANK_PROCESS", "VLLM::Worker"),
            rank_joined_suffix=env_str("WORKER_RANK_JOINED_SUFFIX", "_TP"),
            rank_grace_s=env_float("SENTINEL_RANK_GRACE_S", 120.0),
            restart_timeout_s=env_int("SENTINEL_RESTART_TIMEOUT_S", 5),
            self_restart_budget=env_int("SENTINEL_SELF_RESTART_BUDGET", 3),
            self_restart_window_s=env_float("SENTINEL_SELF_RESTART_WINDOW_S", 3600.0),
            diagnostics_lines=env_int("SENTINEL_DIAGNOSTICS_LINES", 400),
            docker_socket=env_str("DOCKER_SOCKET", "/var/run/docker.sock"),
            docker_api_version=env_str("DOCKER_API_VERSION", "1.53"),
            dry_run=env_bool("DRY_RUN", False),
        )


def scan_fatal_lines(text: str, after: Optional[float]) -> Optional[Tuple[float, str]]:
    """The NEWEST fatal signature in a ``docker logs --timestamps`` capture
    whose line timestamp is after ``after`` (the container's current start, so
    a fault from a previous incarnation is never re-reported). Returns
    ``(epoch, signature)`` with the signature drawn from the bounded list."""
    found: Optional[Tuple[float, str]] = None
    for line in text.splitlines():
        if not line:
            continue
        stamp, _, rest = line.partition(" ")
        at = parse_docker_time(stamp)
        if at is None:
            continue  # no timestamp prefix: not a log line we can place in time
        if after is not None and at <= after:
            continue
        for sig in FATAL_SIGNATURES:
            if sig in rest:
                if found is None or at >= found[0]:
                    found = (at, sig)
                break
    return found


class Sentinel:
    def __init__(self, cfg: Config, docker: DockerClient, clock: Optional[Clock] = None):
        self.cfg = cfg
        self.docker = docker
        self.clock = clock or Clock()
        self.budget = Budget(cfg.self_restart_budget, cfg.self_restart_window_s, self.clock)
        self._restart_lock = threading.Lock()
        self._snap_lock = threading.Lock()
        self.docker_ok: Optional[bool] = None
        self.docker_error = ""
        self.container: dict = {"exists": None, "running": None, "health": None, "status": None,
                                "restart_count": None, "started_at": None, "id": None}
        self.rank_process_alive: Optional[bool] = None
        self.rank_joined: Optional[bool] = None
        self.last_fault: Optional[dict] = None
        self.last_self_restart_at: Optional[float] = None
        self.last_restart_at: Optional[float] = None      # any restart, self or head-driven
        self._log_cursor: Optional[float] = None          # epoch of the newest log line scanned
        self._budget_logged = False
        self._prev_started_at: Optional[float] = None
        self.restarts: List[dict] = []
        self._snapshot: dict = {}
        self._publish(self.clock.time())

    # ------------------------------------------------------------------

    def _observe(self, now: float) -> None:
        cfg = self.cfg
        try:
            info = self.docker.inspect(cfg.worker_container)
        except DockerUnavailable as exc:
            self.docker_ok = False
            self.docker_error = str(exc)
            return
        except DockerError as exc:
            self.docker_ok = exc.status == 404
            self.docker_error = "" if exc.status == 404 else str(exc)
            if exc.status == 404:
                self.container.update({"exists": False, "running": False, "status": "absent", "health": None})
                self.rank_process_alive = self.rank_joined = None
            return
        self.docker_ok = True
        self.docker_error = ""
        state = info.get("State") or {}
        running = bool(state.get("Running"))
        started_at = parse_docker_time(state.get("StartedAt"))
        if started_at is not None and self._prev_started_at not in (None, started_at):
            log.info("worker container restarted: started_at %.0f -> %.0f (restart_count %s)",
                     self._prev_started_at, started_at, info.get("RestartCount"))
            # A new incarnation: its log stream starts here.
            self._log_cursor = None
        if started_at is not None:
            self._prev_started_at = started_at
        self.container.update({
            "exists": True, "running": running, "status": state.get("Status"),
            "health": (state.get("Health") or {}).get("Status"),
            "restart_count": int(info.get("RestartCount") or 0),
            "started_at": started_at, "id": info.get("Id"),
        })
        if not running:
            self.rank_process_alive = self.rank_joined = None
            return
        try:
            procs = self.docker.top(cfg.worker_container)
            # ``VLLM::Worker`` while waiting at the rendezvous, ``VLLM::Worker_TP1``
            # once joined: both are the rank process being alive.
            self.rank_process_alive = any(cfg.rank_process in p for p in procs)
            self.rank_joined = any(cfg.rank_process + cfg.rank_joined_suffix in p for p in procs)
        except DockerError:
            self.rank_process_alive = self.rank_joined = None  # between states: unknown, not dead
        self._scan_logs(started_at)

    def _scan_logs(self, started_at: Optional[float]) -> None:
        """Only lines newer than the current incarnation's start and than the
        last line already scanned. ``since`` is whole seconds, so the line
        timestamps do the exact de-duplication."""
        floor = self._log_cursor if self._log_cursor is not None else started_at
        try:
            text = self.docker.logs(self.cfg.worker_container, tail=5000,
                                    since=int(floor) if floor else None, timeout=15.0)
        except DockerError as exc:
            log.debug("log scan skipped: %s", exc)
            return
        newest = floor
        for line in text.splitlines():
            at = parse_docker_time(line.partition(" ")[0])
            if at is not None and (newest is None or at > newest):
                newest = at
        hit = scan_fatal_lines(text, floor)
        if hit is not None:
            at, sig = hit
            if self.last_fault is None or at > float(self.last_fault["at"]):
                self.last_fault = {"at": at, "signature": sig}
                log.error("fatal signature in the worker log: %r at %s", sig, utc_stamp(at))
        self._log_cursor = newest

    # ------------------------------------------------------------------

    def _restart_container(self, why: str, autonomous: bool,
                           fault_at: Optional[float] = None) -> Tuple[bool, str, float]:
        """``docker restart -t 5``. Serialised: the head's POST and the
        sentinel's own decision must not race into two restarts — and an
        autonomous decision taken BEFORE the lock is re-checked under it:
        if the head's POST restarted the container meanwhile (our own
        ``last_restart_at`` moved past the fault, or the container's
        ``started_at`` did), the fault is already answered and the freshly
        restarted worker is left alone."""
        with self._restart_lock:
            now = self.clock.time()
            if autonomous and fault_at is not None:
                if (self.last_restart_at or 0.0) >= fault_at:
                    log.info("autonomous restart skipped: already restarted at %.0f for a fault at %.0f",
                             self.last_restart_at or 0.0, fault_at)
                    return True, "already restarted", self.last_restart_at or now
                try:
                    started = parse_docker_time(((self.docker.inspect(self.cfg.worker_container).get("State") or {})
                                                 .get("StartedAt")))
                except DockerError:
                    started = None
                if started is not None and started > fault_at:
                    log.info("autonomous restart skipped: the container started at %.0f, after the fault at %.0f",
                             started, fault_at)
                    return True, "already restarted", started
            if self.cfg.dry_run:
                log.warning("DRY_RUN: would restart %s (%s)", self.cfg.worker_container, why)
                self._record_restart(now, why, autonomous)
                return True, "dry run", now
            try:
                self.docker.restart(self.cfg.worker_container, t=self.cfg.restart_timeout_s)
            except DockerError as exc:
                log.error("restart of %s failed (%s): %s", self.cfg.worker_container, why, exc)
                return False, exc.kind, now
            at = self.clock.time()
            self._record_restart(at, why, autonomous)
            log.warning("restarted %s (t=%d): %s", self.cfg.worker_container, self.cfg.restart_timeout_s, why)
            return True, "", at

    def _record_restart(self, at: float, why: str, autonomous: bool) -> None:
        self.last_restart_at = at
        if autonomous:
            self.last_self_restart_at = at
            self.budget.record()
        self.restarts.append({"at": at, "reason": why, "autonomous": autonomous})
        del self.restarts[:-50]
        self._log_cursor = None

    def _decide(self, now: float) -> None:
        """Autonomy (§6.4), OFF unless ``SENTINEL_AUTONOMOUS=1``: the rank
        process is gone, or a fatal signature newer than the current start
        appeared → restart the worker now. With it off (the default and
        production), the same facts are only published for the controller."""
        cfg = self.cfg
        if not cfg.autonomous:
            return
        c = self.container
        if not (self.docker_ok and c.get("running") and c.get("started_at")):
            return
        started = float(c["started_at"])
        uptime = now - started
        # Never act twice on one event: a fault older than our own last
        # restart, or a rank absence measured inside the grace after it, is
        # the incarnation we already replaced (inspect can lag the restart).
        last_restart = self.last_restart_at or 0.0
        since_restart = now - last_restart
        why = ""
        fault_at: Optional[float] = None
        fault = self.last_fault
        if fault is not None and float(fault["at"]) > started and float(fault["at"]) > last_restart:
            fault_at = float(fault["at"])
            why = f"fatal signature '{fault['signature']}' at {utc_stamp(fault_at)}"
        elif (self.rank_process_alive is False and uptime >= cfg.rank_grace_s
              and since_restart >= cfg.rank_grace_s):
            fault_at = now
            why = f"rank process '{cfg.rank_process}*' absent {uptime:.0f}s after container start"
        if not why:
            self._budget_logged = False
            return
        if self.budget.exhausted():
            if not self._budget_logged:
                self._budget_logged = True
                log.error("self-restart budget exhausted (%d in %.0fs); %s — standing down, the head "
                          "controller or a human must act", self.budget.used(), cfg.self_restart_window_s, why)
            return
        log.error("autonomous restart: %s", why)
        self._restart_container(why, autonomous=True, fault_at=fault_at)
        # The restart moved started_at past the fault; observe again so the
        # published state already reflects the new incarnation.
        self._observe(self.clock.time())

    def tick(self) -> None:
        now = self.clock.time()
        self._observe(now)
        self._decide(now)
        self._publish(self.clock.time())

    # ------------------------------------------------------------------

    def _publish(self, now: float) -> None:
        c = self.container
        started_at = c.get("started_at")
        snap = {
            "schema": 1,
            "container": {"running": c.get("running"), "health": c.get("health"),
                          "restart_count": c.get("restart_count"), "started_at": started_at,
                          "status": c.get("status"), "exists": c.get("exists")},
            # How long ago the container started, measured on THIS host's
            # clock alone (observed_at − started_at, both Node 2's): the
            # controller sets it against its own time since the last proven
            # completion (§6.3 trigger 1(a)) so Node 2's wall clock is never
            # compared with Node 1's — a skewed NTP must not turn every
            # recovery into a fresh worker_rank_dead.
            "started_ago_s": (max(0.0, now - float(started_at)) if started_at is not None else None),
            "rank_process_alive": self.rank_process_alive,
            "rank_joined": self.rank_joined,
            "last_fault": dict(self.last_fault) if self.last_fault else None,
            "autonomous": self.cfg.autonomous,
            "self_restarts_in_window": self.budget.used(),
            "self_restart_budget": self.cfg.self_restart_budget,
            "last_self_restart_at": self.last_self_restart_at,
            "last_restart_at": self.last_restart_at,
            "docker_ok": self.docker_ok,
            "docker_error": self.docker_error,
            "dry_run": self.cfg.dry_run,
            "observed_at": now,
        }
        with self._snap_lock:
            self._snapshot = snap

    def snapshot(self) -> dict:
        with self._snap_lock:
            return dict(self._snapshot)

    def diagnostics(self) -> str:
        return self.docker.logs(self.cfg.worker_container, tail=self.cfg.diagnostics_lines, timeout=20.0)

    def restart_requested(self, peer: str) -> Tuple[int, dict]:
        ok, err, at = self._restart_container(f"POST /restart from {peer}", autonomous=False)
        if not ok:
            return 502, {"restarted_at": None, "error": err}
        self._observe(self.clock.time())
        self._publish(self.clock.time())
        return 200, {"restarted_at": at}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    #: Applied to every accepted socket, so a peer that never finishes its
    #: request frees its handler thread after this long.
    timeout = 10
    sentinel: Optional[Sentinel] = None

    def _peer(self) -> str:
        return self.client_address[0] if self.client_address else ""

    def _token_ok(self) -> bool:
        cfg = self.sentinel.cfg
        if not cfg.token:
            return True
        presented = self.headers.get("X-Sentinel-Token") or ""
        return hmac.compare_digest(presented.encode(), cfg.token.encode())

    def _peer_is_head(self) -> bool:
        allowed = self.sentinel.cfg.head_ip or "127.0.0.1"
        return self._peer() in (allowed, f"::ffff:{allowed}")

    def _peer_is_local(self) -> bool:
        bind = self.sentinel.cfg.bind
        return self._peer() in ("127.0.0.1", "::ffff:127.0.0.1", bind, f"::ffff:{bind}")

    def _authorised_post(self) -> bool:
        # Both: the head's address AND the token when one is configured. A
        # configured token never loosens the peer rule (contract §6.4).
        return self._peer_is_head() and self._token_ok()

    def do_GET(self) -> None:  # noqa: N802
        s = self.sentinel
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if s is None:
            send_text(self, 503, "starting\n")
            return
        if path == "/healthz":
            # Liveness only, no data: the container's own healthcheck probes
            # it from this host without a token; anyone else needs one.
            if self._peer_is_local() or self._token_ok():
                send_text(self, 200, "ok\n")
            else:
                send_text(self, 403, "not authorised\n")
            return
        if not self._token_ok():
            log.warning("GET %s refused from %s: token", path, self._peer())
            send_text(self, 403, "not authorised\n")
            return
        if path == "/state":
            send_json(self, 200, s.snapshot())
        elif path == "/diagnostics":
            try:
                send_text(self, 200, s.diagnostics())
            except DockerError as exc:
                send_text(self, 503, f"unavailable: {exc.kind}\n")
        else:
            send_text(self, 404, "not found\n")

    def do_POST(self) -> None:  # noqa: N802
        s = self.sentinel
        path = self.path.split("?", 1)[0].rstrip("/")
        peer = self._peer()
        # Path and authorisation BEFORE the body is read: an unauthorised
        # peer cannot pin a handler thread on a body it never sends. The
        # unread body means the connection cannot be reused, so it is closed.
        if path != "/restart" or s is None:
            self.close_connection = True
            send_text(self, 404, "not found\n")
            return
        if not self._authorised_post():
            self.close_connection = True
            log.warning("POST /restart refused from %s", peer)
            send_json(self, 403, {"restarted_at": None, "error": "not authorised"})
            return
        read_json_body(self)  # drain; the body carries nothing we act on
        status, reply = s.restart_requested(peer)
        send_json(self, status, reply)

    def log_message(self, fmt: str, *args) -> None:
        log.debug(fmt, *args)


def main() -> int:
    global log
    log = configure_logging("sentinel", "SENTINEL_LOG_LEVEL")
    cfg = Config.from_env()
    clock = Clock()
    docker = DockerClient(cfg.docker_socket, cfg.docker_api_version)
    sentinel = Sentinel(cfg, docker, clock)
    Handler.sentinel = sentinel
    server = ThreadingHTTPServer((cfg.bind, cfg.port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="http", daemon=True).start()
    log.info("listening on %s:%d; container=%s head_ip=%s token=%s autonomous=%s rank_process=%s grace=%.0fs "
             "budget=%d/%.0fs poll=%.0fs dry_run=%s",
             cfg.bind, cfg.port, cfg.worker_container, cfg.head_ip or "(unset: loopback only)",
             "set" if cfg.token else "unset", cfg.autonomous, cfg.rank_process, cfg.rank_grace_s,
             cfg.self_restart_budget, cfg.self_restart_window_s, cfg.poll_s, cfg.dry_run)
    stop = threading.Event()

    def on_signal(signum, _frame) -> None:
        log.info("signal %d: stopping", signum)
        stop.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    try:
        run_periodically(sentinel.tick, cfg.poll_s, clock, stop, log, "sentinel")
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
