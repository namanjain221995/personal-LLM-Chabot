"""The worker sentinel (contract §6.4 v2): its state document, the
log-signature and process-table detection (``VLLM::Worker`` while waiting,
``_TP1`` once joined), autonomy OFF by default, the token on every endpoint
once configured, the peer rule on POST /restart, and the under-lock re-check
before an autonomous restart."""
from __future__ import annotations

import json
import socket
import time

from sentinel import Handler, scan_fatal_lines

FAULT_LINE = ("(Worker_TP1 pid=297) ERROR 09-11 22:15:50 [multiproc_executor.py:1018] "
              "RuntimeError: Triton Error [CUDA]: misaligned address")


def test_state_document_shape(worker_world):
    w = worker_world
    w.sentinel.tick()
    snap = json.loads(json.dumps(w.sentinel.snapshot()))
    assert snap["container"]["running"] is True
    assert snap["container"]["health"] == "healthy"
    assert snap["container"]["restart_count"] == 0
    assert isinstance(snap["container"]["started_at"], float)
    assert snap["rank_process_alive"] is True
    assert snap["rank_joined"] is True
    assert snap["last_fault"] is None
    assert snap["autonomous"] is False
    assert snap["self_restarts_in_window"] == 0
    assert isinstance(snap["observed_at"], float)


def test_sentinel_is_not_autonomous_by_default_and_reports_a_fatal_signature_within_one_poll(worker_world):
    """v2 §6.4: the sentinel is a sensor; only the controller's POST /restart acts."""
    w = worker_world
    assert w.cfg.autonomous is False
    w.sentinel.tick()
    w.container.log(FAULT_LINE)
    w.sentinel.tick()
    snap = w.sentinel.snapshot()
    assert snap["last_fault"]["signature"] == "misaligned address"
    assert w.restarts == []
    assert snap["self_restarts_in_window"] == 0
    w.container.processes = [p for p in w.container.processes if "Worker" not in p]
    w.clock.advance(w.cfg.rank_grace_s + 1)
    w.sentinel.tick()
    w.sentinel.tick()
    assert w.sentinel.snapshot()["rank_process_alive"] is False
    assert w.restarts == []                                   # reported, never acted on


def test_a_waiting_worker_is_alive_but_not_joined_and_a_joined_one_is_both(worker_world):
    """The title is ``VLLM::Worker`` while the worker waits at the rendezvous
    (setup_proc_title_and_log_prefix before the parallel groups exist) and
    ``VLLM::Worker_TP1`` once joined: a waiting worker is never absent."""
    w = worker_world
    w.container.processes = [p if "Worker_TP1" not in p else "VLLM::Worker" for p in w.container.processes]
    w.sentinel.tick()
    snap = w.sentinel.snapshot()
    assert snap["rank_process_alive"] is True and snap["rank_joined"] is False
    w.container.processes = [p if p != "VLLM::Worker" else "VLLM::Worker_TP1" for p in w.container.processes]
    w.sentinel.tick()
    snap = w.sentinel.snapshot()
    assert snap["rank_process_alive"] is True and snap["rank_joined"] is True
    w.container.processes = [p for p in w.container.processes if "VLLM::Worker" not in p]
    w.sentinel.tick()
    snap = w.sentinel.snapshot()
    assert snap["rank_process_alive"] is False and snap["rank_joined"] is False
    # …and with autonomy on, a WAITING worker is never restarted for being "absent"
    w.cfg.autonomous = True
    w.container.processes.append("VLLM::Worker")
    w.clock.advance(w.cfg.rank_grace_s * 3)
    w.sentinel.tick()
    w.sentinel.tick()
    assert w.restarts == []


def test_autonomous_sentinel_restarts_on_a_fatal_signature(worker_world):
    w = worker_world
    w.cfg.autonomous = True
    w.sentinel.tick()
    assert w.restarts == []
    w.container.log(FAULT_LINE)
    w.container.log("(Worker_TP1 pid=297) ERROR terminate called after throwing an instance of 'c10::AcceleratorError'")
    w.sentinel.tick()
    assert len(w.restarts) == 1 and w.restarts[0][3] == w.cfg.restart_timeout_s   # docker restart t=5
    snap = w.sentinel.snapshot()
    # the FIRST matching signature on the newest line wins; here the newest
    # fatal line is the AcceleratorError one
    assert snap["last_fault"]["signature"] in ("misaligned address", "AcceleratorError")
    assert snap["self_restarts_in_window"] == 1
    assert snap["container"]["restart_count"] == 1
    assert snap["container"]["started_at"] >= snap["last_fault"]["at"]
    assert snap["autonomous"] is True
    # the same fault is never acted on twice
    w.sentinel.tick()
    w.sentinel.tick()
    assert len(w.restarts) == 1


def test_a_fault_from_a_previous_incarnation_is_ignored(worker_world):
    w = worker_world
    w.cfg.autonomous = True
    w.container.log(FAULT_LINE, at=w.container.started_at - 30)   # before the current start
    w.sentinel.tick()
    assert w.restarts == []
    assert w.sentinel.snapshot()["last_fault"] is None


def test_autonomous_sentinel_restarts_on_a_missing_rank_process_after_the_grace_and_honours_its_budget(worker_world):
    w = worker_world
    w.cfg.autonomous = True
    w.container.processes = [p for p in w.container.processes if "Worker" not in p]
    # a fresh container: the rank has not spawned yet → not a fault
    w.container.started_at = w.clock.time() - 10
    w.sentinel.tick()
    assert w.restarts == [] and w.sentinel.snapshot()["rank_process_alive"] is False
    # past the grace → restart 1
    w.clock.advance(w.cfg.rank_grace_s)
    w.sentinel.tick()
    assert len(w.restarts) == 1
    # the rank still does not come back: grace again, restart 2 (budget = 2)
    w.clock.advance(w.cfg.rank_grace_s + 1)
    w.sentinel.tick()
    assert len(w.restarts) == 2
    # budget exhausted: stands down
    w.clock.advance(w.cfg.rank_grace_s + 1)
    w.sentinel.tick()
    w.sentinel.tick()
    assert len(w.restarts) == 2
    assert w.sentinel.snapshot()["self_restarts_in_window"] == 2
    # the window slides: allowed again
    w.clock.advance(w.cfg.self_restart_window_s)
    w.sentinel.tick()
    assert len(w.restarts) == 3


def test_an_autonomous_decision_is_rechecked_under_the_restart_lock(worker_world):
    """[minor] the head's POST /restart landed between the decision and the
    lock: the freshly restarted worker is left alone and not charged to the
    autonomous budget."""
    w = worker_world
    w.cfg.autonomous = True
    w.sentinel.tick()
    fault_at = w.clock.time()
    w.clock.advance(4)
    status, reply = w.sentinel.restart_requested("127.0.0.1")    # the controller's restart
    assert status == 200 and len(w.restarts) == 1
    ok, why, at = w.sentinel._restart_container("fatal signature", autonomous=True, fault_at=fault_at)
    assert ok and why == "already restarted" and at == reply["restarted_at"]
    assert len(w.restarts) == 1
    assert w.sentinel.budget.used() == 0
    # a fault NEWER than the last restart is acted on
    w.clock.advance(4)
    ok, why, _ = w.sentinel._restart_container("fatal signature", autonomous=True, fault_at=w.clock.time())
    assert ok and len(w.restarts) == 2 and w.sentinel.budget.used() == 1


def test_scan_fatal_lines_uses_the_bounded_signature_list_and_the_newest_line():
    text = ("2026-09-11T22:15:50.000000000Z INFO all fine\n"
            "2026-09-11T22:15:51.000000000Z ERROR NCCL error: unhandled system error\n"
            "2026-09-11T22:15:52.000000000Z ERROR WorkerProc hit an exception.\n"
            "no timestamp on this line: illegal memory access\n")
    at, sig = scan_fatal_lines(text, None)
    assert sig == "WorkerProc hit an exception"
    assert at == 1789164952.0
    assert scan_fatal_lines(text, 1789164952.0) is None
    assert scan_fatal_lines("2026-09-11T22:15:50.000000000Z INFO nothing here\n", None) is None


def test_docker_unavailable_is_reported_not_acted_on(worker_world):
    w = worker_world
    w.cfg.autonomous = True
    w.docker.stop()
    w.sentinel.tick()
    snap = w.sentinel.snapshot()
    assert snap["docker_ok"] is False
    assert snap["rank_process_alive"] is None
    assert w.restarts == []


def test_post_restart_requires_the_head_peer_and_the_token_when_configured(worker_world, peer_tools):
    connect_from, http_status, serve = peer_tools
    w = worker_world
    w.sentinel.tick()
    server, port = serve(Handler, "sentinel", w.sentinel)
    req = b"POST /restart HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}"
    try:
        # no token configured: only CLUSTER_HEAD_IP (127.0.0.1 here) may POST
        raw = connect_from("127.0.0.2", port, req)
        assert http_status(raw) == 403 and w.restarts == []
        raw = connect_from("127.0.0.1", port, req)
        assert http_status(raw) == 200
        body = json.loads(raw.split(b"\r\n\r\n", 1)[1])
        assert isinstance(body["restarted_at"], float)
        assert len(w.restarts) == 1
        # a head-driven restart is not a SELF restart
        assert w.sentinel.snapshot()["self_restarts_in_window"] == 0

        # token configured: the header is required AND the peer rule still holds
        w.cfg.token = "s3cret"
        with_token = (b"POST /restart HTTP/1.1\r\nHost: x\r\nX-Sentinel-Token: s3cret\r\n"
                      b"Content-Length: 2\r\nConnection: close\r\n\r\n{}")
        wrong = with_token.replace(b"s3cret", b"nope")
        assert http_status(connect_from("127.0.0.2", port, with_token)) == 403   # right token, wrong peer
        assert http_status(connect_from("127.0.0.1", port, wrong)) == 403        # right peer, wrong token
        assert http_status(connect_from("127.0.0.1", port, req)) == 403          # right peer, no token
        assert len(w.restarts) == 1
        assert http_status(connect_from("127.0.0.1", port, with_token)) == 200
        assert len(w.restarts) == 2
        raw = connect_from("127.0.0.1", port, b"POST /state HTTP/1.1\r\nHost: x\r\nX-Sentinel-Token: s3cret\r\n"
                                                  b"Content-Length: 0\r\nConnection: close\r\n\r\n")
        assert http_status(raw) == 404
    finally:
        server.shutdown()
        server.server_close()


def test_gets_are_open_without_a_token_and_require_it_on_every_endpoint_once_configured(worker_world, peer_tools):
    """[major] the token guards ALL endpoints, not only the mutation — a
    forged or read /state is what trigger 1 acts on."""
    connect_from, http_status, serve = peer_tools
    w = worker_world
    w.sentinel.tick()
    server, port = serve(Handler, "sentinel", w.sentinel)
    try:
        raw = connect_from("127.0.0.2", port, b"GET /state HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        assert http_status(raw) == 200
        w.container.log("worker line for diagnostics")
        raw = connect_from("127.0.0.2", port, b"GET /diagnostics HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        assert http_status(raw) == 200 and b"worker line for diagnostics" in raw
        raw = connect_from("127.0.0.2", port, b"GET /healthz HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        assert http_status(raw) == 200 and raw.endswith(b"ok\n")

        w.cfg.token = "s3cret"
        for path in ("/state", "/diagnostics"):
            raw = connect_from("127.0.0.2", port, b"GET " + path.encode() + b" HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            assert http_status(raw) == 403, path
            raw = connect_from("127.0.0.2", port, b"GET " + path.encode() + b" HTTP/1.1\r\nHost: x\r\n"
                                                    b"X-Sentinel-Token: s3cret\r\nConnection: close\r\n\r\n")
            assert http_status(raw) == 200, path
        # /healthz: the container's own healthcheck (a local peer) needs no token; a remote one does
        raw = connect_from("127.0.0.1", port, b"GET /healthz HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        assert http_status(raw) == 200
        raw = connect_from("127.0.0.2", port, b"GET /healthz HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        assert http_status(raw) == 403
        raw = connect_from("127.0.0.2", port, b"GET /healthz HTTP/1.1\r\nHost: x\r\nX-Sentinel-Token: s3cret\r\nConnection: close\r\n\r\n")
        assert http_status(raw) == 200
    finally:
        server.shutdown()
        server.server_close()


def test_post_body_is_not_read_before_authorisation_and_the_handler_times_out(worker_world, peer_tools):
    """[minor] an unauthorised peer announcing a body it never sends gets its
    403 at once; an authorised one holds a thread only up to Handler.timeout."""
    connect_from, http_status, serve = peer_tools
    w = worker_world
    w.sentinel.tick()
    old_timeout = Handler.timeout
    Handler.timeout = 1
    server, port = serve(Handler, "sentinel", w.sentinel)
    try:
        hdr = b"POST /restart HTTP/1.1\r\nHost: x\r\nContent-Length: 1000\r\n\r\n"
        s = socket.socket()
        s.settimeout(3.0)
        s.bind(("127.0.0.2", 0))
        s.connect(("127.0.0.1", port))
        t0 = time.monotonic()
        s.sendall(hdr)
        raw = s.recv(65536)
        assert http_status(raw) == 403 and time.monotonic() - t0 < 0.9
        s.close()
        assert w.restarts == []
        s = socket.socket()
        s.settimeout(3.0)
        s.connect(("127.0.0.1", port))          # the head's peer, body never sent
        t0 = time.monotonic()
        s.sendall(hdr)
        try:
            data = s.recv(65536)
        except socket.timeout:
            data = b"(no close)"
        assert data == b"" and 0.8 <= time.monotonic() - t0 <= 2.5, data
        s.close()
        assert w.restarts == []                 # nothing acted on without a complete request
    finally:
        Handler.timeout = old_timeout
        server.shutdown()
        server.server_close()


def test_dry_run_records_but_does_not_restart(worker_world):
    w = worker_world
    w.cfg.autonomous = True
    w.cfg.dry_run = True
    w.container.log(FAULT_LINE)
    w.sentinel.tick()
    assert w.restarts == []
    assert w.sentinel.snapshot()["self_restarts_in_window"] == 1
