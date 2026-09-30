"""scripts/whisper-cpu-chat-gate.py: the owner's gate for a CPU speech replica (chat drop <= 5 %).

The decision is a paired one: probes with the replica idle and busy alternate, and the verdict is
the median paired drop. These tests pin the arithmetic, the stream parsing, the "ran alone on the
engine" rule, the memory floor, and, end to end against a fake vLLM and a fake replica on loopback,
that a replica which slows decode FAILS and one that does not PASSES.
"""

from __future__ import annotations

import importlib.util
import io
import json
import math
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    from .support import REPO_ROOT
except ImportError:  # `unittest discover -s launcher/tests` imports top-level modules.
    from support import REPO_ROOT

_SPEC = importlib.util.spec_from_file_location("whisper_cpu_chat_gate", REPO_ROOT / "scripts" / "whisper-cpu-chat-gate.py")
gate = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gate)


class ArithmeticTests(unittest.TestCase):
    def test_decode_rate_excludes_the_first_token(self) -> None:
        self.assertAlmostEqual(gate.decode_rate(300, 1.0, 14.8), 299 / 13.8)
        self.assertTrue(math.isnan(gate.decode_rate(1, 1.0, 1.0)))
        self.assertTrue(math.isnan(gate.decode_rate(300, None, 2.0)))

    def test_a_slower_on_arm_is_a_positive_drop(self) -> None:
        self.assertEqual(gate.paired_drops([(100.0, 95.0), (20.0, 22.0)]), [0.05, -0.1])

    def test_the_verdict_is_the_median_paired_drop_against_the_threshold(self) -> None:
        small = [(22.0, 22.0 * 0.97)] * 6 + [(22.0, 22.0 * 0.99)] * 6
        large = [(22.0, 22.0 * 0.90)] * 6 + [(22.0, 22.0 * 0.93)] * 6
        self.assertEqual(gate.verdict(small, 0.05, 8)["verdict"], "PASS")
        self.assertTrue(gate.verdict(small, 0.05, 8)["larger_drop_ruled_out"])
        self.assertEqual(gate.verdict(large, 0.05, 8)["verdict"], "FAIL")
        self.assertEqual(gate.verdict(small[:5], 0.05, 8)["verdict"], "NOT_MEASURED")

    def test_the_stream_parser_counts_tokens_from_usage_and_stops_at_done(self) -> None:
        ticks = iter([10.0, 10.5, 11.0])
        lines = [
            b": keep-alive\n",
            b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n',
            b'data: {"choices":[{"delta":{"reasoning_content":"a"}}]}\n',
            b'data: {"choices":[{"delta":{"content":"b"}}]}\n',
            b'data: {"choices":[{"delta":{"content":"c"}}]}\n',
            b'data: {"choices":[],"usage":{"prompt_tokens":40,"completion_tokens":3}}\n',
            b"data: [DONE]\n",
            b'data: {"choices":[{"delta":{"content":"never read"}}]}\n',
        ]
        parsed = gate.read_stream(lines, clock=lambda: next(ticks))
        self.assertEqual((parsed["t_first"], parsed["t_last"], parsed["tokens"], parsed["prompt_tokens"], parsed["chunks"]),
                         (10.0, 11.0, 3, 40, 3))


class _Fake:
    """One loopback server playing both vLLM (chat, metrics, models) and the CPU replica."""

    def __init__(self, token_s: float, busy_factor: float, intrude_first: int = 0) -> None:
        self.token_s, self.busy_factor, self.intrude_left = token_s, busy_factor, intrude_first
        self.lock = threading.Lock()
        self.replica_in_flight = 0
        self.running = 0
        self.gen = 0
        self.prompt = 0
        self.chat_requests = 0
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args) -> None:  # quiet
                pass

            def _send(self, code: int, body: bytes, ctype: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                if self.path == "/v1/models":
                    self._send(200, json.dumps({"data": [{"id": "fake/model"}]}).encode(), "application/json")
                elif self.path == "/metrics":
                    with fake.lock:
                        text = (f'vllm:num_requests_running{{engine="0"}} {fake.running}.0\n'
                                f'vllm:generation_tokens_total{{engine="0"}} {fake.gen}.0\n'
                                f'vllm:prompt_tokens_total{{engine="0"}} {fake.prompt}.0\n')
                    self._send(200, text.encode(), "text/plain")
                else:
                    self._send(404, b"", "text/plain")

            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                if self.path == "/v1/audio/transcriptions":
                    with fake.lock:
                        fake.replica_in_flight += 1
                    time.sleep(0.3)
                    with fake.lock:
                        fake.replica_in_flight -= 1
                    self._send(200, b'{"text":"ok"}', "application/json")
                    return
                request = json.loads(body)
                tokens = int(request["max_tokens"])
                with fake.lock:
                    fake.chat_requests += 1
                    fake.running += 1
                    intrude = fake.intrude_left > 0
                    fake.intrude_left -= 1 if intrude else 0
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                for _ in range(tokens):
                    with fake.lock:
                        busy = fake.replica_in_flight > 0
                    time.sleep(fake.token_s * (fake.busy_factor if busy else 1.0))
                    self.wfile.write(b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n')
                    self.wfile.flush()
                usage = {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": tokens}}
                self.wfile.write(b"data: " + json.dumps(usage).encode() + b"\n\n")
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                with fake.lock:
                    fake.running -= 1
                    # Another tenant's tokens landing during this probe make it unclean.
                    fake.gen += tokens + (7 if intrude else 0)
                    fake.prompt += 10
                self.close_connection = True

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class EndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="techsara-gate-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "clips").mkdir()
        for name in ("a.wav", "b.flac"):
            (self.root / "clips" / name).write_bytes(b"RIFF....WAVE")
        self.meminfo = self.root / "meminfo"
        self.meminfo.write_text("MemAvailable:   31457280 kB\n", encoding="utf-8")  # 30 GiB

    def _gate(self, fake: _Fake, *extra: str) -> tuple[int, list[dict]]:
        self.addCleanup(fake.close)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = gate.main(["--chat", fake.base, "--replica", fake.base, "--clips", str(self.root / "clips"),
                              "--tokens", "30", "--pairs", "4", "--min-pairs", "4", "--before", "1", "--after", "1",
                              "--settle", "0.15", "--meminfo", str(self.meminfo), *extra])
        rows = [json.loads(line) for line in buffer.getvalue().splitlines() if line.startswith("{")]
        return code, rows

    def test_a_replica_that_slows_decode_by_a_third_fails_the_gate(self) -> None:
        code, rows = self._gate(_Fake(token_s=0.006, busy_factor=1.5))
        final = rows[-1]
        self.assertEqual((code, final["verdict"]), (1, "FAIL"), final)
        self.assertGreater(final["median_drop_pct"], 20)
        self.assertEqual(final["replica_clip_failures"], 0)

    def test_a_replica_that_does_not_slow_decode_passes(self) -> None:
        # The code path, not timer precision on a shared CI runner: a generous threshold.
        code, rows = self._gate(_Fake(token_s=0.006, busy_factor=1.0), "--threshold", "20")
        final = rows[-1]
        self.assertEqual((code, final["verdict"]), (0, "PASS"), final)
        self.assertGreater(final["replica_clips_ok"], 0)

    def test_a_probe_that_shared_the_engine_is_retried_not_counted(self) -> None:
        fake = _Fake(token_s=0.002, busy_factor=1.0, intrude_first=2)
        self.addCleanup(fake.close)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = gate.main(["--chat", fake.base, "--probe-only", "--before", "2", "--tokens", "20",
                              "--meminfo", str(self.meminfo)])
        rows = [json.loads(line) for line in buffer.getvalue().splitlines()]
        probes = [r for r in rows if not r.get("summary")]
        self.assertEqual(code, 0)
        self.assertEqual([r["clean"] for r in probes], [False, False, True, True])
        self.assertEqual(probes[0]["other_gen_tokens"], 7)
        self.assertEqual(rows[-1]["n"], 2, "only the clean probes are summarised")

    def test_under_the_memory_floor_nothing_is_sent(self) -> None:
        self.meminfo.write_text("MemAvailable:   10485760 kB\n", encoding="utf-8")  # 10 GiB
        fake = _Fake(token_s=0.002, busy_factor=1.0)
        code, rows = self._gate(fake)
        self.assertEqual(code, 2)
        self.assertEqual(rows[-1]["label"], "abort")
        self.assertEqual(fake.chat_requests, 0)
        self.assertEqual(fake.replica_in_flight, 0)


if __name__ == "__main__":
    unittest.main()
