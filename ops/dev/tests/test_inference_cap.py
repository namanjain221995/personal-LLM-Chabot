"""Tests for the dev stack's inference cap (ops/dev/inference_cap/inference_cap.py).

Run: python3 -m pytest ops/dev/tests/test_inference_cap.py -q -p no:cacheprovider
(or python3 -m unittest discover -s ops/dev/tests -p 'test_inference_cap.py')

Everything runs on 127.0.0.1 with ephemeral ports: fake upstream engines are in-process
ThreadingHTTPServers and the cap is built from a config mapping, except the two tests that run the
module as a process to check its exit code and its SIGTERM drain.
"""

import dataclasses
import http.client
import importlib.util
import io
import json
import os
import queue
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
CAP_PATH = os.path.join(os.path.dirname(HERE), "inference_cap", "inference_cap.py")

_spec = importlib.util.spec_from_file_location("inference_cap", CAP_PATH)
cap = importlib.util.module_from_spec(_spec)
sys.modules["inference_cap"] = cap
_spec.loader.exec_module(cap)

WAIT = 10.0  # upper bound for any single synchronisation wait; a pass never gets near it


# --------------------------------------------------------------------------- fakes

class _QuietServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def server_bind(self):  # skip HTTPServer's reverse DNS lookup
        import socketserver
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class Concurrency:
    """Counts requests inside upstream handlers; shared to measure across upstreams."""

    def __init__(self):
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0

    def __enter__(self):
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)

    def __exit__(self, *exc):
        with self.lock:
            self.active -= 1


class FakeUpstream:
    """An engine stand-in. Paths (by suffix): /hold (0.3 s, counted), /block (until released),
    /sse (event 1, wait for sse_go, event 2), /sse-silent (event 1, then silent until released),
    /big (1 MiB with Content-Length), /models (GET metadata), anything else echoes the request."""

    BIG = bytes(range(256)) * 4096

    def __init__(self, concurrency=None):
        self.concurrency = concurrency or Concurrency()
        self.lock = threading.Lock()
        self.requests = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.sse_go = threading.Event()
        self.sse_second_sent = threading.Event()
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self):
                upstream._handle(self)

            do_POST = do_HEAD = do_PUT = do_DELETE = do_GET

        self.server = _QuietServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05},
                                       daemon=True)
        self.thread.start()

    def close(self):
        self.release.set()
        self.sse_go.set()
        self.server.shutdown()
        self.server.server_close()

    def paths(self):
        with self.lock:
            return [r["path"] for r in self.requests]

    @staticmethod
    def _json(h, status, obj, extra=()):
        data = json.dumps(obj).encode()
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(data)))
        for k, v in extra:
            h.send_header(k, v)
        h.end_headers()
        if h.command != "HEAD":
            h.wfile.write(data)

    @staticmethod
    def _chunk(h, data):
        h.wfile.write(b"%X\r\n%b\r\n" % (len(data), data))

    def _start_sse(self, h):
        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream")
        h.send_header("Transfer-Encoding", "chunked")
        h.end_headers()

    def _handle(self, h):
        length = int(h.headers.get("Content-Length") or 0)
        body = h.rfile.read(length) if length else b""
        with self.lock:
            self.requests.append({"method": h.command, "path": h.path, "headers": h.headers.items(),
                                  "body": body})
        path = h.path.split("?")[0]
        if path.endswith("/hold"):
            with self.concurrency:
                time.sleep(0.3)
            self._json(h, 200, {"ok": True, "path": path})
        elif path.endswith("/block"):
            self.entered.set()
            self.release.wait(WAIT)
            self._json(h, 200, {"released": True})
        elif path.endswith("/sse"):
            self._start_sse(h)
            self._chunk(h, b"data: 1\n\n")
            self.sse_go.wait(WAIT)
            self._chunk(h, b"data: 2\n\n")
            self.sse_second_sent.set()
            h.wfile.write(b"0\r\n\r\n")
        elif path.endswith("/sse-silent"):
            self._start_sse(h)
            self._chunk(h, b"data: 1\n\n")
            self.release.wait(WAIT)
            try:
                self._chunk(h, b"data: 2\n\n")
                h.wfile.write(b"0\r\n\r\n")
            except OSError:
                pass
        elif path.endswith("/big"):
            h.send_response(200)
            h.send_header("Content-Type", "application/octet-stream")
            h.send_header("Content-Length", str(len(self.BIG)))
            h.end_headers()
            h.wfile.write(self.BIG)
        elif path.endswith("/models"):
            self._json(h, 200, {"object": "list", "data": [{"id": "fake-model"}]})
        else:
            self._json(h, 200, {"method": h.command, "path": h.path, "headers": h.headers.items(),
                                "body_len": len(body)},
                       extra=(("Keep-Alive", "timeout=5"), ("Connection", "X-Up-Hop"),
                              ("X-Up-Hop", "1"), ("X-Up-Keep", "1")))


# --------------------------------------------------------------------------- helpers

def request(port, method, path, body=None, headers=None, timeout=WAIT):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, resp.headers, data
    finally:
        conn.close()


def raw_exchange(port, payload, timeout=WAIT):
    """Send raw bytes, return everything the cap sends back until it closes or times out."""
    sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        sock.sendall(payload)
        chunks = []
        while True:
            try:
                data = sock.recv(65536)
            except socket.timeout:
                break
            if not data:
                break
            chunks.append(data)
        return b"".join(chunks)
    finally:
        sock.close()


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def wait_until(predicate, timeout=WAIT, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class CapTestCase(unittest.TestCase):
    def upstream(self, concurrency=None):
        up = FakeUpstream(concurrency)
        self.addCleanup(up.close)
        return up

    def cap(self, upstreams, config_hook=None, **settings):
        """upstreams: {name: FakeUpstream | port, or (FakeUpstream | port, base_path)}."""
        entries = []
        for name, spec in upstreams.items():
            target, base = spec if isinstance(spec, tuple) else (spec, "")
            port = target.port if isinstance(target, FakeUpstream) else target
            entries.append(f"{name}=http://127.0.0.1:{port}{base}")
        env = {"CAP_LISTEN": "127.0.0.1:0", "CAP_UPSTREAMS": ",".join(entries)}
        env.update({k: str(v) for k, v in settings.items()})
        config = cap.parse_config(env)
        if config_hook is not None:
            config = config_hook(config)
        self.log = io.StringIO()
        srv = cap.build_server(config, log_stream=self.log)
        srv.start(poll_interval=0.05)
        self.addCleanup(srv.stop)
        return srv

    def stats(self, srv):
        status, _, body = request(srv.port, "GET", "/_cap/stats")
        self.assertEqual(status, 200)
        return json.loads(body)

    def settled(self, srv, served=None):
        """Stats once no request holds or waits for a slot or holds body bytes (the cap releases a
        slot just after the client has the last byte, so a client can look before the release).
        A reservation given back twice would leave buffered_bytes below zero and never settle."""
        def done():
            stats = self.stats(srv)
            return (stats["inflight"] == 0 and stats["queued"] == 0
                    and stats["buffered_bytes"] == 0 and stats["queued_bytes"] == 0
                    and (served is None or stats["served"] >= served))
        self.assertTrue(wait_until(done), self.stats(srv))
        return self.stats(srv)

    def background(self, fn, *args, **kwargs):
        """Run fn in a thread; returns a getter that joins it and returns its result."""
        out = {}

        def run():
            try:
                out["value"] = fn(*args, **kwargs)
            except BaseException as exc:  # surfaced by the getter
                out["error"] = exc

        thread = threading.Thread(target=run, daemon=True)
        thread.start()

        def result():
            thread.join(WAIT * 2)
            self.assertFalse(thread.is_alive(), "background request did not finish")
            if "error" in out:
                raise out["error"]
            return out["value"]

        return result

    def hold_the_slot(self, srv, up):
        """Occupy the (single) slot with a request the upstream keeps until up.release is set."""
        result = self.background(request, srv.port, "POST", "/main/v1/block", b"{}")
        self.assertTrue(up.entered.wait(WAIT), "the holding request never reached the upstream")
        return result


# --------------------------------------------------------------------------- tests

class TestCap(CapTestCase):
    def test_six_concurrent_posts_reach_the_upstream_two_at_a_time(self):
        up = self.upstream()
        srv = self.cap({"main": up})
        barrier = threading.Barrier(6)

        def post(i):
            barrier.wait(WAIT)
            return request(srv.port, "POST", "/main/v1/hold", json.dumps({"i": i}).encode())

        results = [self.background(post, i) for i in range(6)]
        for result in results:
            status, _, body = result()
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), {"ok": True, "path": "/v1/hold"})
        self.assertEqual(up.concurrency.peak, 2)
        stats = self.settled(srv, served=6)
        self.assertEqual(stats["peak_inflight"], 2)
        self.assertEqual(stats["max_inflight"], 2)
        self.assertEqual(stats["served"], 6)
        self.assertEqual(stats["inflight"], 0)
        self.assertEqual(stats["queued"], 0)

    def test_the_cap_spans_upstreams(self):
        shared = Concurrency()
        main, router = self.upstream(shared), self.upstream(shared)
        srv = self.cap({"main": main, "router": router})
        barrier = threading.Barrier(3)

        def post(name):
            barrier.wait(WAIT)
            return request(srv.port, "POST", f"/{name}/v1/hold", b"{}")

        results = [self.background(post, n) for n in ("main", "router", "router")]
        self.assertEqual([r()[0] for r in results], [200, 200, 200])
        self.assertEqual(shared.peak, 2)
        self.assertEqual(len(main.paths()) + len(router.paths()), 3)
        self.assertEqual(self.settled(srv, served=3)["peak_inflight"], 2)

    def test_sse_is_streamed_incrementally_and_keep_alive_survives(self):
        up = self.upstream()
        srv = self.cap({"main": up})
        conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=WAIT)
        self.addCleanup(conn.close)
        conn.request("POST", "/main/v1/sse", body=b'{"stream": true}',
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Content-Type"), "text/event-stream")
        received = b""
        while b"data: 1\n\n" not in received:
            chunk = resp.read1(65536)
            self.assertTrue(chunk, "stream ended before the first event")
            received += chunk
        first_at = time.monotonic()
        # The upstream holds event 2 until told: event 1 arrived on its own, not with the end.
        self.assertFalse(up.sse_second_sent.is_set())
        threading.Timer(0.5, up.sse_go.set).start()
        rest = resp.read()
        second_at = time.monotonic()
        self.assertGreaterEqual(second_at - first_at, 0.4)
        self.assertEqual(received + rest, b"data: 1\n\ndata: 2\n\n")
        # The same keep-alive connection serves the next request (chunked framing was correct).
        conn.request("POST", "/main/v1/echo", body=b"{}")
        again = conn.getresponse()
        self.assertEqual(again.status, 200)
        self.assertEqual(json.loads(again.read())["path"], "/v1/echo")

    def test_upstream_content_length_is_passed_through(self):
        up = self.upstream()
        srv = self.cap({"main": up})
        status, headers, body = request(srv.port, "POST", "/main/v1/big", b"{}")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Length"), str(len(FakeUpstream.BIG)))
        self.assertIsNone(headers.get("Transfer-Encoding"))
        self.assertEqual(body, FakeUpstream.BIG)

    def test_queue_timeout_answers_503_with_retry_after(self):
        up = self.upstream()
        srv = self.cap({"main": up}, CAP_MAX_INFLIGHT=1, CAP_QUEUE_TIMEOUT_S=0.3)
        holder = self.hold_the_slot(srv, up)
        started = time.monotonic()
        status, headers, body = request(srv.port, "POST", "/main/v1/echo", b"{}")
        self.assertEqual(status, 503)
        self.assertGreaterEqual(time.monotonic() - started, 0.25)
        self.assertEqual(headers.get("Retry-After"), "30")
        self.assertEqual(json.loads(body)["error"]["type"], "cap_queue_timeout")
        stats = self.stats(srv)
        self.assertEqual(stats["rejected_queue_timeout"], 1)
        self.assertEqual(stats["inflight"], 1)
        self.assertNotIn("/v1/echo", up.paths())
        up.release.set()
        self.assertEqual(holder()[0], 200)
        self.settled(srv)

    def test_client_disconnect_mid_stream_releases_the_slot(self):
        up = self.upstream()
        srv = self.cap({"main": up}, CAP_MAX_INFLIGHT=1)
        sock = socket.create_connection(("127.0.0.1", srv.port), timeout=WAIT)
        sock.sendall(b"POST /main/v1/sse-silent HTTP/1.1\r\nHost: cap\r\n"
                     b"Content-Type: application/json\r\nContent-Length: 2\r\n\r\n{}")
        received = b""
        while b"data: 1" not in received:
            chunk = sock.recv(4096)
            self.assertTrue(chunk, "stream ended before the first event")
            received += chunk
        self.assertEqual(self.stats(srv)["inflight"], 1)
        sock.close()
        # The upstream stays silent (it would hold the slot for WAIT seconds); only noticing the
        # client's hang-up frees the slot.
        started = time.monotonic()
        status, _, body = request(srv.port, "POST", "/main/v1/echo", b"{}", timeout=5)
        self.assertEqual(status, 200)
        self.assertLess(time.monotonic() - started, 4)
        self.settled(srv)
        self.assertFalse(up.release.is_set())

    def test_a_client_that_pipelined_more_than_a_buffer_and_hung_up_releases_the_slot(self):
        up = self.upstream()
        srv = self.cap({"main": up}, CAP_MAX_INFLIGHT=1)
        sock = socket.create_connection(("127.0.0.1", srv.port), timeout=WAIT)
        # Request 1 streams; request 2 is pipelined behind it with far more than the cap's 8 KiB
        # read buffer, so most of it still waits on the socket while request 1 is relayed.
        sock.sendall(b"POST /main/v1/sse-silent HTTP/1.1\r\nHost: cap\r\nContent-Length: 2\r\n\r\n{}"
                     + b"POST /main/v1/echo HTTP/1.1\r\nHost: cap\r\nContent-Length: 65536\r\n\r\n"
                     + b"x" * 65536)
        received = b""
        while b"data: 1" not in received:
            chunk = sock.recv(4096)
            self.assertTrue(chunk, "stream ended before the first event")
            received += chunk
        self.assertEqual(self.stats(srv)["inflight"], 1)
        sock.close()
        started = time.monotonic()
        status, _, _ = request(srv.port, "POST", "/main/v1/echo", b"{}", timeout=5)
        self.assertEqual(status, 200)
        self.assertLess(time.monotonic() - started, 4)
        self.settled(srv)
        self.assertFalse(up.release.is_set())

    def test_a_queued_client_that_hangs_up_never_reaches_the_upstream(self):
        up = self.upstream()
        srv = self.cap({"main": up}, CAP_MAX_INFLIGHT=1)
        holder = self.hold_the_slot(srv, up)
        sock = socket.create_connection(("127.0.0.1", srv.port), timeout=WAIT)
        sock.sendall(b"POST /main/v1/queued HTTP/1.1\r\nHost: cap\r\nContent-Length: 2\r\n\r\n{}")
        self.assertTrue(wait_until(lambda: self.stats(srv)["queued"] == 1))
        sock.close()
        self.assertTrue(wait_until(lambda: self.stats(srv)["queued"] == 0))
        up.release.set()
        self.assertEqual(holder()[0], 200)
        self.assertNotIn("/v1/queued", up.paths())
        self.settled(srv)

    def test_get_and_head_bypass_the_slot(self):
        up = self.upstream()
        srv = self.cap({"main": up}, CAP_MAX_INFLIGHT=1, CAP_QUEUE_TIMEOUT_S=0.3)
        holder = self.hold_the_slot(srv, up)
        status, _, body = request(srv.port, "GET", "/main/v1/models", timeout=5)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["data"][0]["id"], "fake-model")
        status, headers, body = request(srv.port, "HEAD", "/main/v1/models", timeout=5)
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertIsNotNone(headers.get("Content-Length"))
        self.assertEqual(self.stats(srv)["inflight"], 1)
        up.release.set()
        self.assertEqual(holder()[0], 200)

    def test_get_and_head_with_a_body_take_a_slot(self):
        up = self.upstream()
        srv = self.cap({"main": up}, CAP_MAX_INFLIGHT=1, CAP_QUEUE_TIMEOUT_S=0.3)
        holder = self.hold_the_slot(srv, up)
        for method in ("GET", "HEAD"):
            with self.subTest(method=method):
                status, headers, _ = request(srv.port, method, "/main/v1/models", b'{"prompt": "x"}',
                                             timeout=5)
                self.assertEqual(status, 503)
                self.assertEqual(headers.get("Retry-After"), "30")
        self.assertEqual(self.stats(srv)["rejected_queue_timeout"], 2)
        status, _, _ = request(srv.port, "GET", "/main/v1/models", timeout=5)
        self.assertEqual(status, 200)  # a GET without a body still skips the slot
        self.assertEqual(up.paths(), ["/v1/block", "/v1/models"])
        up.release.set()
        self.assertEqual(holder()[0], 200)
        # With the slot free, a GET with a body is forwarded, body included, through a slot.
        status, _, body = request(srv.port, "GET", "/main/v1/echo", b'{"prompt": "x"}')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["body_len"], len(b'{"prompt": "x"}'))
        self.assertEqual(self.settled(srv)["peak_inflight"], 1)

    def test_method_override_headers_are_not_forwarded(self):
        up = self.upstream()
        srv = self.cap({"main": up})
        status, _, body = request(srv.port, "POST", "/main/v1/echo", b"{}",
                                  headers={"X-HTTP-Method-Override": "GET", "X-HTTP-Method": "GET",
                                           "X-Method-Override": "GET", "X-Keep": "yes"})
        self.assertEqual(status, 200)
        seen = {k.lower() for k, _ in json.loads(body)["headers"]}
        self.assertIn("x-keep", seen)
        for name in ("x-http-method-override", "x-http-method", "x-method-override"):
            self.assertNotIn(name, seen)

    def test_queue_full_answers_503_at_once_without_reading_the_body(self):
        up = self.upstream()
        srv = self.cap({"main": up}, CAP_MAX_INFLIGHT=1, CAP_MAX_QUEUED=1)
        holder = self.hold_the_slot(srv, up)
        queued = self.background(request, srv.port, "POST", "/main/v1/queued", b"{}")
        self.assertTrue(wait_until(lambda: self.stats(srv)["queued"] == 1))
        # The third declares a body it never sends: an answer proves the cap did not wait to read it.
        started = time.monotonic()
        reply = raw_exchange(srv.port, b"POST /main/v1/third HTTP/1.1\r\nHost: cap\r\n"
                                       b"Content-Length: 1000\r\n\r\n", timeout=5)
        self.assertLess(time.monotonic() - started, 1.5)
        head, _, body = reply.partition(b"\r\n\r\n")
        self.assertTrue(head.startswith(b"HTTP/1.1 503 "), reply[:40])
        self.assertIn(b"\r\nRetry-After: 30", head)
        self.assertEqual(json.loads(body)["error"]["type"], "cap_queue_full")
        # Expect: 100-continue gets the refusal, never a 100.
        reply = raw_exchange(srv.port, b"POST /main/v1/third HTTP/1.1\r\nHost: cap\r\n"
                                       b"Expect: 100-continue\r\nContent-Length: 1000\r\n\r\n", timeout=5)
        self.assertTrue(reply.startswith(b"HTTP/1.1 503 "), reply[:40])
        stats = self.stats(srv)
        self.assertEqual((stats["rejected_queue_full"], stats["inflight"], stats["queued"]), (2, 1, 1))
        up.release.set()
        self.assertEqual(holder()[0], 200)
        self.assertEqual(queued()[0], 200)
        self.assertNotIn("/v1/third", up.paths())
        self.settled(srv, served=2)

    def test_buffer_full_answers_503_at_once_without_reading_the_body(self):
        up = self.upstream()
        srv = self.cap({"main": up}, CAP_MAX_INFLIGHT=1, CAP_MAX_BODY_BYTES=1000,
                       CAP_MAX_BUFFERED_BYTES=1500)
        holder = self.background(request, srv.port, "POST", "/main/v1/block", b"x" * 1000)
        self.assertTrue(up.entered.wait(WAIT))
        stats = self.stats(srv)
        self.assertEqual((stats["buffered_bytes"], stats["queued_bytes"]), (1000, 0))
        started = time.monotonic()
        reply = raw_exchange(srv.port, b"POST /main/v1/second HTTP/1.1\r\nHost: cap\r\n"
                                       b"Content-Length: 600\r\n\r\n", timeout=5)
        self.assertLess(time.monotonic() - started, 1.5)
        head, _, body = reply.partition(b"\r\n\r\n")
        self.assertTrue(head.startswith(b"HTTP/1.1 503 "), reply[:40])
        self.assertIn(b"\r\nRetry-After: 30", head)
        self.assertEqual(json.loads(body)["error"]["type"], "cap_buffer_full")
        # A body that fits next to the first one is admitted and waits for the slot.
        fits = self.background(request, srv.port, "POST", "/main/v1/fits", b"y" * 500)
        self.assertTrue(wait_until(lambda: self.stats(srv)["queued_bytes"] == 500))
        self.assertEqual(self.stats(srv)["buffered_bytes"], 1500)
        up.release.set()
        self.assertEqual(holder()[0], 200)
        self.assertEqual(fits()[0], 200)
        stats = self.settled(srv, served=2)
        self.assertEqual(stats["rejected_buffer_full"], 1)
        self.assertNotIn("/v1/second", up.paths())

    def test_the_byte_budget_is_given_back_on_success_error_and_hang_up(self):
        up = self.upstream()
        srv = self.cap({"main": up, "dead": free_port()}, CAP_MAX_INFLIGHT=1, CAP_MAX_BODY_BYTES=1000,
                       CAP_MAX_BUFFERED_BYTES=1000)
        # Each step uses the whole budget, so a reservation that leaked would refuse the next one.
        for _ in range(2):
            status, _, _ = request(srv.port, "POST", "/main/v1/echo", b"x" * 1000)
            self.assertEqual(status, 200)
            self.settled(srv)
        status, _, body = request(srv.port, "POST", "/dead/v1/chat/completions", b"x" * 1000)
        self.assertEqual(status, 502)
        self.settled(srv)
        # The client hangs up part-way through its body.
        sock = socket.create_connection(("127.0.0.1", srv.port), timeout=WAIT)
        sock.sendall(b"POST /main/v1/partial HTTP/1.1\r\nHost: cap\r\nContent-Length: 1000\r\n\r\n"
                     + b"x" * 10)
        self.assertTrue(wait_until(lambda: self.stats(srv)["buffered_bytes"] == 1000))
        self.assertEqual(self.stats(srv)["queued"], 1)
        sock.close()
        self.settled(srv)
        # The client hangs up mid-stream while it holds the slot.
        sock = socket.create_connection(("127.0.0.1", srv.port), timeout=WAIT)
        sock.sendall(b"POST /main/v1/sse-silent HTTP/1.1\r\nHost: cap\r\nContent-Length: 1000\r\n\r\n"
                     + b"x" * 1000)
        received = b""
        while b"data: 1" not in received:
            chunk = sock.recv(4096)
            self.assertTrue(chunk, "stream ended before the first event")
            received += chunk
        stats = self.stats(srv)
        self.assertEqual((stats["inflight"], stats["buffered_bytes"], stats["queued_bytes"]), (1, 1000, 0))
        sock.close()
        self.settled(srv)
        status, _, _ = request(srv.port, "POST", "/main/v1/echo", b"x" * 1000)
        self.assertEqual(status, 200)
        stats = self.settled(srv)
        self.assertEqual((stats["rejected_buffer_full"], stats["upstream_errors"]), (0, 1))
        self.assertNotIn("/v1/partial", up.paths())

    def test_expect_100_continue_is_answered_after_admission(self):
        up = self.upstream()
        srv = self.cap({"main": up})
        sock = socket.create_connection(("127.0.0.1", srv.port), timeout=WAIT)
        self.addCleanup(sock.close)
        sock.sendall(b"POST /main/v1/echo HTTP/1.1\r\nHost: cap\r\nExpect: 100-continue\r\n"
                     b"Content-Length: 5\r\n\r\n")
        received = b""
        while b"\r\n\r\n" not in received:
            chunk = sock.recv(4096)
            self.assertTrue(chunk, "closed before the 100")
            received += chunk
        self.assertTrue(received.startswith(b"HTTP/1.1 100 "), received[:40])
        sock.sendall(b"hello")
        received = received.partition(b"\r\n\r\n")[2]
        while b'"body_len": 5' not in received:
            chunk = sock.recv(65536)
            self.assertTrue(chunk, received[-200:])
            received += chunk
        self.assertTrue(received.startswith(b"HTTP/1.1 200 "), received[:40])
        # A request refused on its headers gets the refusal instead of a 100.
        reply = raw_exchange(srv.port, b"POST /nope/v1/x HTTP/1.1\r\nHost: cap\r\n"
                                       b"Expect: 100-continue\r\nContent-Length: 5\r\n\r\n", timeout=5)
        self.assertTrue(reply.startswith(b"HTTP/1.1 404 "), reply[:40])

    def test_unknown_upstream_and_path_traversal(self):
        main, router = self.upstream(), self.upstream()
        srv = self.cap({"main": main, "router": router})
        status, _, body = request(srv.port, "POST", "/nope/v1/chat/completions", b"{}")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"]["type"], "unknown_upstream")
        for path in ("/main/../router/x", "/main/v1/../../router/x", "/main/%2e%2e/router/x",
                     "/main/%2E%2E/router/x", "/main/..%2frouter/x", "/main/%252e%252e/router/x",
                     "/main/..\\router/x", "/main/./x", "/main/v1%2f..%2f..%2frouter/x"):
            status, _, _ = request(srv.port, "POST", path, b"{}")
            self.assertEqual(status, 400, path)
        status, _, _ = request(srv.port, "GET", "/main/../router/x")
        self.assertEqual(status, 400)
        self.assertEqual(router.paths(), [])
        self.assertEqual(main.paths(), [])

    def test_upstream_down_is_502_and_releases_the_slot(self):
        up = self.upstream()
        srv = self.cap({"main": up, "dead": free_port()}, CAP_MAX_INFLIGHT=1)
        status, _, body = request(srv.port, "POST", "/dead/v1/chat/completions", b"{}")
        self.assertEqual(status, 502)
        self.assertEqual(json.loads(body)["error"]["type"], "upstream_unreachable")
        status, _, _ = request(srv.port, "POST", "/main/v1/echo", b"{}", timeout=5)
        self.assertEqual(status, 200)
        stats = self.settled(srv)
        self.assertEqual(stats["upstream_errors"], 1)

    def test_upstream_read_timeout_is_504_and_releases_the_slot(self):
        up = self.upstream()
        srv = self.cap({"main": up}, CAP_MAX_INFLIGHT=1,
                       config_hook=lambda c: dataclasses.replace(c, read_timeout_s=0.5))
        status, _, body = request(srv.port, "POST", "/main/v1/block", b"{}")
        self.assertEqual(status, 504)
        self.assertEqual(json.loads(body)["error"]["type"], "upstream_timeout")
        up.release.set()
        status, _, _ = request(srv.port, "POST", "/main/v1/echo", b"{}", timeout=5)
        self.assertEqual(status, 200)
        self.assertEqual(self.settled(srv)["upstream_errors"], 1)

    def test_headers_host_base_path_query_and_logging(self):
        up = self.upstream()
        srv = self.cap({"main": (up, "/base")})
        status, headers, body = request(
            srv.port, "POST", "/main/v1/echo?a=1&b=two", b'{"x": 1}',
            headers={"Connection": "keep-alive, X-Drop-Me", "X-Drop-Me": "1", "Keep-Alive": "timeout=5",
                     "Proxy-Authorization": "Basic Zm9vOmJhcg==", "Proxy-Foo": "1", "TE": "trailers",
                     "Upgrade": "websocket", "Trailer": "X-Checksum", "X-Keep": "yes",
                     "Authorization": "Bearer test-token-123", "Content-Type": "application/json"})
        self.assertEqual(status, 200)
        seen = json.loads(body)
        self.assertEqual(seen["path"], "/base/v1/echo?a=1&b=two")
        self.assertEqual(seen["body_len"], len(b'{"x": 1}'))
        got = {}
        for key, value in seen["headers"]:
            got.setdefault(key.lower(), []).append(value)
        self.assertEqual(got["host"], [f"127.0.0.1:{up.port}"])
        self.assertEqual(got["connection"], ["close"])  # the cap's own, not the client's
        for name in ("x-drop-me", "keep-alive", "proxy-authorization", "proxy-foo", "te", "upgrade",
                     "trailer"):
            self.assertNotIn(name, got)
        self.assertEqual(got["x-keep"], ["yes"])
        self.assertEqual(got["authorization"], ["Bearer test-token-123"])
        self.assertEqual(got["content-length"], [str(len(b'{"x": 1}'))])
        # Response hop-by-hop headers stop at the cap too.
        self.assertEqual(headers.get("X-Up-Keep"), "1")
        self.assertIsNone(headers.get("X-Up-Hop"))
        self.assertIsNone(headers.get("Keep-Alive"))
        line = "name=main method=POST path=/main/v1/echo status=200"
        # The cap writes its line just after the response, so the client can be ahead of it.
        self.assertTrue(wait_until(lambda: line in self.log.getvalue()), self.log.getvalue())
        log = self.log.getvalue()
        self.assertNotIn("a=1", log)
        self.assertNotIn("test-token", log)
        self.assertNotIn("Zm9v", log)
        stats_text = json.dumps(self.stats(srv))
        self.assertNotIn("127.0.0.1", stats_text)
        self.assertEqual(json.loads(stats_text)["upstreams"], ["main"])

    def test_body_limits_413_and_chunked_411(self):
        up = self.upstream()
        srv = self.cap({"main": up}, CAP_MAX_BODY_BYTES=1024)
        status, _, _ = request(srv.port, "POST", "/main/v1/echo", b"x" * 1024)
        self.assertEqual(status, 200)
        status, headers, body = request(srv.port, "POST", "/main/v1/echo", b"x" * 2048)
        self.assertEqual(status, 413)
        self.assertEqual(json.loads(body)["error"]["type"], "body_too_large")
        # A huge declared length is refused from the headers alone, without waiting for the body.
        reply = raw_exchange(srv.port, b"POST /main/v1/echo HTTP/1.1\r\nHost: cap\r\n"
                                       b"Content-Length: 1000000000\r\n\r\n", timeout=5)
        self.assertTrue(reply.startswith(b"HTTP/1.1 413 "), reply[:40])
        # Expect: 100-continue gets the refusal instead of a 100.
        reply = raw_exchange(srv.port, b"POST /main/v1/echo HTTP/1.1\r\nHost: cap\r\n"
                                       b"Expect: 100-continue\r\nContent-Length: 5000\r\n\r\n", timeout=5)
        self.assertTrue(reply.startswith(b"HTTP/1.1 413 "), reply[:40])
        reply = raw_exchange(srv.port, b"POST /main/v1/echo HTTP/1.1\r\nHost: cap\r\n"
                                       b"Transfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n0\r\n\r\n", timeout=5)
        self.assertTrue(reply.startswith(b"HTTP/1.1 411 "), reply[:40])
        self.assertIn(b'"length_required"', reply)
        self.assertEqual(len(up.paths()), 1)  # only the 1024-byte request reached the upstream

    def test_admin_endpoints(self):
        up = self.upstream()
        srv = self.cap({"main": up, "router": up})
        status, _, body = request(srv.port, "GET", "/_cap/health")
        self.assertEqual((status, json.loads(body)), (200, {"ok": True}))
        stats = self.stats(srv)
        self.assertEqual(set(stats), {"max_inflight", "max_queued", "max_buffered_bytes", "inflight",
                                      "queued", "peak_inflight", "buffered_bytes", "queued_bytes",
                                      "served", "rejected_queue_timeout", "rejected_queue_full",
                                      "rejected_buffer_full", "upstream_errors", "upstreams"})
        self.assertEqual((stats["max_queued"], stats["max_buffered_bytes"]), (4, 536870912))
        self.assertEqual(stats["upstreams"], ["main", "router"])
        status, _, _ = request(srv.port, "POST", "/_cap/health", b"{}")
        self.assertEqual(status, 405)
        status, _, _ = request(srv.port, "GET", "/_cap/stats", b"{}")
        self.assertEqual(status, 400)
        self.assertEqual(up.paths(), [])


class TestConfig(unittest.TestCase):
    GOOD = "main=http://192.0.2.10:8000,router=http://192.0.2.11:8001/v1/,embed=http://embed"

    def parse(self, **env):
        return cap.parse_config(env)

    def test_defaults_and_a_good_config(self):
        cfg = self.parse(CAP_UPSTREAMS=self.GOOD)
        self.assertEqual((cfg.listen_host, cfg.listen_port), ("0.0.0.0", 9100))
        self.assertEqual(cfg.max_inflight, 2)
        self.assertEqual(cfg.queue_timeout_s, 900)
        self.assertEqual(cfg.connect_timeout_s, 10)
        self.assertEqual(cfg.read_timeout_s, 4500)
        self.assertEqual(cfg.max_body_bytes, 134217728)
        self.assertEqual(cfg.max_queued, 4)
        self.assertEqual(cfg.max_buffered_bytes, 536870912)
        self.assertEqual(list(cfg.upstreams), ["main", "router", "embed"])
        self.assertEqual(cfg.upstreams["router"].base_path, "/v1")
        self.assertEqual(cfg.upstreams["embed"].port, 80)
        self.assertEqual(cfg.upstreams["embed"].host_header, "embed")
        self.assertEqual(cfg.upstreams["main"].host_header, "192.0.2.10:8000")
        self.assertEqual(self.parse(CAP_UPSTREAMS=self.GOOD, CAP_MAX_INFLIGHT="1").max_inflight, 1)
        self.assertEqual(self.parse(CAP_UPSTREAMS=self.GOOD, CAP_MAX_QUEUED="0").max_queued, 0)
        self.assertEqual(self.parse(CAP_UPSTREAMS=self.GOOD, CAP_MAX_QUEUED="64").max_queued, 64)
        cfg = self.parse(CAP_UPSTREAMS=self.GOOD, CAP_MAX_BODY_BYTES="1000", CAP_MAX_BUFFERED_BYTES="1000")
        self.assertEqual((cfg.max_body_bytes, cfg.max_buffered_bytes), (1000, 1000))

    def test_refusals(self):
        bad = [
            {"CAP_UPSTREAMS": self.GOOD, "CAP_MAX_INFLIGHT": "3"},
            {"CAP_UPSTREAMS": self.GOOD, "CAP_MAX_INFLIGHT": "0"},
            {"CAP_UPSTREAMS": self.GOOD, "CAP_MAX_INFLIGHT": "two"},
            {"CAP_UPSTREAMS": "main=https://192.0.2.10:8000"},
            {"CAP_UPSTREAMS": "main=ftp://192.0.2.10:8000"},
            {"CAP_UPSTREAMS": "Main=http://192.0.2.10:8000"},
            {"CAP_UPSTREAMS": "-main=http://192.0.2.10:8000"},
            {"CAP_UPSTREAMS": "ma_in=http://192.0.2.10:8000"},
            {"CAP_UPSTREAMS": "_cap=http://192.0.2.10:8000"},
            {"CAP_UPSTREAMS": "main=http://192.0.2.10:8000,main=http://192.0.2.11:8000"},
            {"CAP_UPSTREAMS": "main=http://192.0.2.10:8000,"},
            {"CAP_UPSTREAMS": "main"},
            {"CAP_UPSTREAMS": "main=http://user:pw@192.0.2.10:8000"},
            {"CAP_UPSTREAMS": "main=http://192.0.2.10:8000/v1?x=1"},
            {"CAP_UPSTREAMS": "main=http://192.0.2.10:99999"},
            {"CAP_UPSTREAMS": "main=http://192.0.2.10:8000/a/../b"},
            {"CAP_UPSTREAMS": ""},
            {"CAP_UPSTREAMS": "   "},
            {},
            {"CAP_UPSTREAMS": self.GOOD, "CAP_LISTEN": "9100"},
            {"CAP_UPSTREAMS": self.GOOD, "CAP_QUEUE_TIMEOUT_S": "-1"},
            {"CAP_UPSTREAMS": self.GOOD, "CAP_QUEUE_TIMEOUT_S": "nan"},
            {"CAP_UPSTREAMS": self.GOOD, "CAP_READ_TIMEOUT_S": "600"},
            {"CAP_UPSTREAMS": self.GOOD, "CAP_MAX_BODY_BYTES": "1e9"},
            {"CAP_UPSTREAMS": self.GOOD, "CAP_MAX_QUEUED": "65"},
            {"CAP_UPSTREAMS": self.GOOD, "CAP_MAX_QUEUED": "-1"},
            {"CAP_UPSTREAMS": self.GOOD, "CAP_MAX_QUEUED": "four"},
            {"CAP_UPSTREAMS": self.GOOD, "CAP_MAX_BUFFERED_BYTES": "134217727"},
            {"CAP_UPSTREAMS": self.GOOD, "CAP_MAX_BODY_BYTES": "1001", "CAP_MAX_BUFFERED_BYTES": "1000"},
            {"CAP_UPSTREAMS": self.GOOD, "CAP_MAX_BUFFERED_BYTES": "0"},
        ]
        for env in bad:
            with self.subTest(env=env):
                with self.assertRaises(cap.ConfigError) as caught:
                    cap.parse_config(env)
                message = str(caught.exception)
                self.assertNotIn("\n", message)
                self.assertNotIn("pw", message)

    def test_main_exits_non_zero_with_one_line(self):
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "CAP_LISTEN": "127.0.0.1:0"}
        for extra in ({}, {"CAP_UPSTREAMS": "main=http://192.0.2.10:8000", "CAP_MAX_INFLIGHT": "3"},
                      {"CAP_UPSTREAMS": "main=http://192.0.2.10:8000", "CAP_MAX_QUEUED": "65"},
                      {"CAP_UPSTREAMS": "main=http://192.0.2.10:8000", "CAP_MAX_BUFFERED_BYTES": "1024"}):
            with self.subTest(extra=extra):
                proc = subprocess.run([sys.executable, CAP_PATH], env={**env, **extra},
                                      capture_output=True, text=True, timeout=30)
                self.assertNotEqual(proc.returncode, 0)
                lines = proc.stderr.strip().splitlines()
                self.assertEqual(len(lines), 1, proc.stderr)
                self.assertIn("configuration error", lines[0])


class TestProcess(CapTestCase):
    def test_sigterm_stops_accepting_and_drains_in_flight_requests(self):
        up = self.upstream()
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONUNBUFFERED": "1",
               "CAP_LISTEN": "127.0.0.1:0", "CAP_UPSTREAMS": f"main=http://127.0.0.1:{up.port}"}
        proc = subprocess.Popen([sys.executable, CAP_PATH], env=env, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        lines = queue.Queue()
        threading.Thread(target=lambda: [lines.put(l) for l in proc.stderr], daemon=True).start()
        port = None
        deadline = time.monotonic() + WAIT
        while port is None and time.monotonic() < deadline:
            match = re.search(r"listening on 127\.0\.0\.1:(\d+)", lines.get(timeout=WAIT))
            port = int(match.group(1)) if match else None
        self.assertIsNotNone(port, "the cap did not report its port")

        holder = self.background(request, port, "POST", "/main/v1/block", b"{}")
        self.assertTrue(up.entered.wait(WAIT))
        proc.send_signal(signal.SIGTERM)

        def refused():
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                return False
            except OSError:
                return True

        self.assertTrue(wait_until(refused, timeout=5), "still accepting after SIGTERM")
        self.assertIsNone(proc.poll(), "exited before the in-flight request finished")
        up.release.set()
        status, _, body = holder()
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"released": True})
        self.assertEqual(proc.wait(timeout=WAIT), 0)


if __name__ == "__main__":
    unittest.main()
