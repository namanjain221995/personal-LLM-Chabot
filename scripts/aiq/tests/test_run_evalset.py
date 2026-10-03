"""B-04: the evaluation-set runner, against a fake orchestrator on 127.0.0.1.

The fake speaks the parts of the orchestrator's HTTP surface the runner uses:
/auth/login (a session cookie), /auth/me (the real {user, features} shape),
/health, /history/conversations (+ ?archived=true), /memory/facts, /uploads
(multipart), /chat (an SSE stream with an x-request-id header, framed by the
orchestrator's OWN app/sse.py, ensure_ascii=False), /chat/trace/{id} and
/chat/stop (which ends that conversation's stream). Its answers are
eval_set_answers.py's hand-written ones, so a good answer must pass its
case's checks end to end and a bad one must fail exactly as
tests/test_eval_set.py says it does.

Nothing here reaches a model, a stack or the network beyond 127.0.0.1, and
every run writes into pytest's tmp directories, never under runs/.
"""
from __future__ import annotations

import email.parser
import email.policy
import importlib.util
import io
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

AIQ = Path(__file__).resolve().parents[1]
if str(AIQ) not in sys.path:
    sys.path.append(str(AIQ))

import eval_set as ES  # noqa: E402
import eval_set_answers as EA  # noqa: E402
import harness as H  # noqa: E402
import run_evalset as RE  # noqa: E402

#: the orchestrator's REAL frame formatter, loaded by path (it imports nothing of the app)
_SSE_SPEC = importlib.util.spec_from_file_location("orchestrator_sse", AIQ.parents[1] / "orchestrator" / "app" / "sse.py")
SSE = importlib.util.module_from_spec(_SSE_SPEC)
_SSE_SPEC.loader.exec_module(SSE)

PASSWORD = "test-pw-bbbbbbbb"
EMAIL = "aiq-runner@example.com"
COOKIE = "ts_session=fake-session"
RUN_KEYS = {"kind", "schema", "started", "finished", "seconds", "interrupted", "deadline_reached", "conditions",
            "cases"}
CONDITION_KEYS = {"base", "harness_commit", "label", "workers", "repeats", "only", "health", "case_timeout_s",
                  "eval_set_sha256", "account", "features", "code_sandbox", "deadline"}
RECORD_KEYS = {"id", "category", "section", "effort", "workload", "repeat", "conversation_id", "error", "stop",
               "attachments", "turns"}
RESULT_KEYS = {"http", "answer", "errors", "request_id", "meta", "reasoning_events", "reasoning_chars", "timing",
               "timed_out", "terminal", "bad_frames", "status_events", "usage", "stages", "stage_offsets",
               "server_total_ms", "trace_status", "versions", "trace_thinks", "source_passages",
               "source_passages_captured"}
TIMING_KEYS = {"first_event_s", "first_token_s", "first_answer_s", "total_s"}
FEATURES = {"artifacts": True, "attachments": True, "deep_research": True, "salesforce": False, "web_search": True}


# ------------------------------------------------------------ the fake --

class Fake:
    """State shared by the handler threads: what was asked, and how to answer."""

    def __init__(self):
        self.lock = threading.Lock()
        self.chats: list = []
        self.uploads: list = []
        self.stops: list = []
        self.logins = 0
        self.traces: dict = {}
        self.trace_reads: dict = {}
        #: (monotonic time, what): "chat <conv>", "stop <conv>", "trace <id>", in the order they arrived
        self.timeline: list = []
        #: conversations a /chat/stop named: their stream ends at the next beat
        self.stopped: set = set()
        #: a /chat/stop that does nothing (the generation keeps streaming), or answers late
        self.ignore_stop = False
        self.stop_delay_s = 0.0
        #: what the account already holds
        self.conversations = self.archived = self.facts = 0
        self.history_status = 200
        #: sign-ins after this many answer 503
        self.fail_login_after = None
        self.login_delay_s = 0.0
        #: case id -> overrides: answer, sources, status, reasoning, trace_thinking,
        #: trace_running_reads, trace_404_reads, stall_s, pre_answer_delay, silent_s,
        #: end ("done" | "error" | "none"), raw_frames (extra frames before the answer)
        self.scripts: dict = {}

    def note(self, what: str) -> None:
        with self.lock:
            self.timeline.append((time.monotonic(), what))

    def script(self, cid: str) -> dict:
        good = EA.GOOD.get(cid, {"answer": "ok"})
        return {"answer": good["answer"], "sources": good.get("sources") or [], **self.scripts.get(cid, {})}


def _trace(gid: str, rid: str, cid: str, script: dict) -> dict:
    start = datetime.now(timezone.utc)

    def at(ms: int) -> str:
        return (start + timedelta(milliseconds=ms)).isoformat()

    rows = [
        ("REQUEST_RECEIVED", None, 2, {}),
        ("MODE_RESOLVED", 12, 14, {}),
        ("CONTEXT_ASSEMBLED", 30, 44, {}),
        ("MODEL_PROMPT_PREPARED", 1, 45, {"call": 1, "thinking": bool(script.get("trace_thinking"))}),
        ("MODEL_DISPATCHED", 0, 45, {"call": 1}),
        ("MODEL_FIRST_CHUNK", 80, 125, {"call": 1}),
        ("FIRST_ANSWER_TOKEN", 150, 150, {}),
        ("MODEL_STREAM_ENDED", 200, 325, {"call": 1, "usage": {"prompt_tokens": 120, "completion_tokens": 30}}),
        ("MODEL_PROMPT_PREPARED", 1, 330, {"call": 2, "thinking": False}),
        ("MODEL_FIRST_CHUNK", 40, 370, {"call": 2}),
        ("MODEL_STREAM_ENDED", 20, 390, {"call": 2, "usage": {"prompt_tokens": 50, "completion_tokens": 5}}),
        ("RESPONSE_GENERATED", None, 395, {"answer_characters": 10}),
    ]
    events = [{"sequence_number": i, "stage": stage, "status": "success", "duration_ms": dur, "created_at": at(ms),
               "started_at": at(ms - (dur or 0)), "completed_at": at(ms), "component": "fake",
               "details": details, "error_type": "", "error_message": ""}
              for i, (stage, dur, ms, details) in enumerate(rows, start=1)]
    return {"trace_id": gid, "request_id": rid, "test_case_id": cid, "conversation_id": "x", "final_status": "ok",
            "started_at": start.isoformat(), "completed_at": at(400), "total_duration_ms": 400,
            "versions": {"application": "1.0", "model": "fake-model", "trace_schema": "1.0.0", "nested": {"a": 1}},
            "meta": {"stage_counts": {"model_call": 2}}, "original_question": "q", "events": events}


def _handler(fake: Fake):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *args):  # quiet
            pass

        def _body(self) -> bytes:
            n = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(n) if n else b""

        def _json(self, status: int, data, headers=None):
            raw = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(raw)

        def _authed(self) -> bool:
            if COOKIE not in (self.headers.get("Cookie") or ""):
                self._json(401, {"detail": "Sign in required."})
                return False
            return True

        def do_GET(self):
            if self.path == "/health":
                return self._json(200, {"status": "degraded", "service": "orchestrator", "version": "1",
                                        "checks": {"vllm": {"status": "ok", "url": "http://engine.invalid:8000/v1",
                                                            "detail": "served by engine.invalid"},
                                                   "duckdb": {"status": "error", "error": "boom at /var/lib/x"},
                                                   "http://engine.invalid/v1": {"status": "ok"},
                                                   "engine.invalid": {"status": "ok"},
                                                   "weird": {"status": "Not OK: see http://engine.invalid"}},
                                        "context": {"window": 1}})
            if not self._authed():
                return
            if self.path == "/auth/me":
                # authn/api.py _me_payload: the email is in it, and must not reach the run files
                return self._json(200, {"username": "runner", "user": {"id": 7, "name": "Runner", "email": EMAIL},
                                        "workspace": {"id": 1, "name": "dev", "role": "super_admin"},
                                        "capabilities": ["members.manage"],
                                        "features": {**FEATURES, "bad name/x": True, "voice_input": "yes"}})
            if self.path in ("/history/conversations", "/history/conversations?archived=true"):
                n = fake.archived if self.path.endswith("archived=true") else fake.conversations
                return self._json(fake.history_status, [{"id": f"c{i}", "title": "private title", "pinned": False,
                                                         "archived": self.path.endswith("true")} for i in range(n)])
            if self.path == "/memory/facts":
                return self._json(200, {"facts": [{"id": i, "fact": "a private fact"} for i in range(fake.facts)]})
            m = re.fullmatch(r"/chat/trace/([0-9a-f]{32})", self.path)
            if m:
                fake.note(f"trace {m.group(1)}")
                with fake.lock:
                    trace = fake.traces.get(m.group(1))
                    fake.trace_reads[m.group(1)] = fake.trace_reads.get(m.group(1), 0) + 1
                    reads = fake.trace_reads[m.group(1)]
                script = fake.script(trace["test_case_id"]) if trace else {}
                if trace is None or reads <= int(script.get("trace_404_reads") or 0):
                    return self._json(404, {"detail": "query trace not found"})
                running = int(script.get("trace_running_reads") or 0)
                return self._json(200, {**trace, "final_status": "running" if reads <= running else "ok"})
            return self._json(404, {"detail": "not found"})

        def do_POST(self):
            raw = self._body()
            if self.path == "/auth/login":
                body = json.loads(raw or b"{}")
                time.sleep(fake.login_delay_s)
                if body.get("email") != EMAIL or body.get("password") != PASSWORD:
                    return self._json(401, {"detail": "Invalid email or password."})
                with fake.lock:
                    fake.logins += 1
                    refused = fake.fail_login_after is not None and fake.logins > fake.fail_login_after
                if refused:
                    return self._json(503, {"detail": "busy"})
                return self._json(200, {"user": {"id": 7}}, {"Set-Cookie": COOKIE + "; Path=/; HttpOnly; Secure"})
            if not self._authed():
                return
            if self.path == "/uploads":
                msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
                    b"Content-Type: " + self.headers["Content-Type"].encode() + b"\r\n\r\n" + raw)
                form, upload = {}, {}
                for part in msg.iter_parts():
                    name = part.get_param("name", header="content-disposition")
                    if part.get_filename():
                        upload = {"filename": part.get_filename(), "content_type": part.get_content_type(),
                                  "data": part.get_payload(decode=True)}
                    else:
                        form[name] = part.get_payload(decode=True).decode()
                rec = {**form, **upload, "upload_id": uuid.uuid4().hex}
                with fake.lock:
                    fake.uploads.append(rec)
                return self._json(200, {"upload_id": rec["upload_id"], "filename": rec["filename"],
                                        "bytes": len(rec["data"]), "files": 1, "notes": [], "profile": []})
            if self.path == "/chat/stop":
                body = json.loads(raw or b"{}")
                fake.note(f"stop {body.get('conversation_id')}")
                time.sleep(fake.stop_delay_s)
                with fake.lock:
                    fake.stops.append(body)
                    if not fake.ignore_stop:
                        fake.stopped.add(body.get("conversation_id"))
                return self._json(200, {"stopped": not fake.ignore_stop})
            if self.path == "/chat":
                return self._chat(json.loads(raw))
            return self._json(404, {"detail": "not found"})

        def _chat(self, body: dict):
            fake.note(f"chat {body.get('conversation_id')}")
            with fake.lock:
                fake.chats.append(body)
            cid = body.get("test_case_id") or ""
            script = fake.script(cid)
            rid = "req_" + uuid.uuid4().hex
            if script.get("status"):
                return self._json(int(script["status"]), {"detail": "internal error"}, {"x-request-id": rid})
            gid = uuid.uuid4().hex
            with fake.lock:
                fake.traces[gid] = _trace(gid, rid, cid, script)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("x-request-id", rid)
            self.end_headers()

            def send(text: str):
                self.wfile.write(text.encode("utf-8"))
                self.wfile.flush()

            def ev(name: str, data: dict):
                send(SSE.sse_event(name, data))  # the orchestrator's own framing: ensure_ascii=False

            conv = body.get("conversation_id")
            try:
                if body.get("intent_id"):
                    ev("meta", {"generation_id": gid, "trace_id": gid, "request_id": rid,
                                "intent_id": body["intent_id"], "attempt": 1})
                send(SSE.sse_comment())
                ev("step", {"id": 1, "title": "Working", "status": "running"})
                ev("status", {"text": "Reading the question"})
                for frame in script.get("raw_frames") or []:
                    send(frame)
                time.sleep(float(script.get("pre_answer_delay", 0.05)))
                if script.get("reasoning"):
                    ev("reasoning", {"text": "thinking about it"})
                ev("token", {"text": "\n"})  # whitespace only: a token, not the first answer token
                time.sleep(0.05)
                answer = script["answer"]
                ev("token", {"text": answer[:40]})
                time.sleep(float(script.get("silent_s") or 0))  # no byte at all, not even a heartbeat
                stall = float(script.get("stall_s") or 0)
                until = time.monotonic() + stall
                while time.monotonic() < until:
                    if conv in fake.stopped:  # /chat/stop cancelled the generation: the stream ends
                        return
                    send(SSE.sse_comment())
                    time.sleep(0.05)
                for i in range(40, len(answer), 40):
                    ev("token", {"text": answer[i:i + 40]})
                ev("step", {"id": 1, "title": "Working", "status": "done"})
                ev("meta", {"route": "chat", "sources": script["sources"], "generation_id": gid, "trace_id": gid,
                            "request_id": rid, "mode": "assistant", "effort": body.get("effort")})
                end = script.get("end", "done")
                if end == "done":
                    ev("done", {"session_id": body.get("session_id")})
                elif end == "error":
                    ev("error", {"message": "The model is unavailable."})
            except (BrokenPipeError, ConnectionResetError):
                pass

    return Handler


@pytest.fixture(autouse=True)
def nothing_lands_under_runs():
    runs = os.path.join(RE.HERE, "runs")
    before = sorted(os.listdir(runs)) if os.path.isdir(runs) else []
    yield
    after = sorted(os.listdir(runs)) if os.path.isdir(runs) else []
    assert after == before, "a test wrote under scripts/aiq/runs/"


@pytest.fixture
def fake():
    state = Fake()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(state))
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def pwfile(tmp_path):
    p = tmp_path / "secret" / "pw"
    p.parent.mkdir()
    p.write_text(PASSWORD + "\n")
    return str(p)


def _run(fake, tmp_path, pwfile, *extra, name="run"):
    out = tmp_path / name
    argv = ["--base", fake.base, "--email", EMAIL, "--password-file", pwfile, "--out", str(out),
            "--code-root", str(tmp_path / "code-root"), *extra]
    assert RE.main(argv) == 0
    results = json.loads((out / "results.json").read_text())
    summary = json.loads((out / "summary.json").read_text())
    return out, results, summary


def _failed(turn: dict) -> list:
    return sorted(c["check"] for c in turn["checks"] if not c["ok"])


def _no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("no network call is allowed here")
    monkeypatch.setattr(httpx.Client, "__init__", refuse)


def _sandbox(monkeypatch, ok=True, backend="container"):
    """The code sandbox as available (or not) without asking docker."""
    monkeypatch.setattr(RE.code_sandbox.Toolchain, "available",
                        lambda self, lang: (True, "") if ok else (False, "no sandbox image on this host"))
    monkeypatch.setattr(RE.code_sandbox.Toolchain, "backend", property(lambda self: backend))


def _fake_run_code(calls):
    def run_code(spec, answer, workdir, tc):
        calls.append((spec, answer, workdir))
        return {"lang": "python", "blocks": 1, "source": answer, "ok": True,
                "steps": [{"stage": "build", "name": "compile", "ok": True, "rc": 0, "tail": ""},
                          {"stage": "check", "name": "check.py", "ok": True, "rc": 0, "tail": "ok"}]}
    return run_code


# ------------------------------------------------------- request bodies --

def test_a_history_case_sends_the_seeded_history_then_the_user_turn(fake, tmp_path, pwfile):
    _out, results, _ = _run(fake, tmp_path, pwfile, "--only", "RQ07")
    (body,) = fake.chats
    case = ES.BY_ID["RQ07"]
    message = case["turns"][0]["message"]
    assert body["messages"][:len(case["history"])] == case["history"]
    assert body["messages"][-1] == {"role": "user", "content": message}
    assert len(body["messages"]) == len(case["history"]) + 1
    assert body["message"] == message
    assert body["conversation_id"] == body["session_id"] == results["cases"][0]["conversation_id"]
    assert (body["mode"], body["model"], body["effort"], body["web_search"]) == ("assistant", "smart", "fast", "off")
    assert body["test_case_id"] == "RQ07" and body["deep_research"] is False
    assert re.fullmatch(r"[0-9a-f]{32}", body["intent_id"])
    assert "pdf_uploads" not in body


def test_attachments_are_uploaded_as_documents_and_named_on_the_turn(fake, tmp_path, pwfile):
    _out, results, _ = _run(fake, tmp_path, pwfile, "--only", "EV05")
    (up,) = fake.uploads
    (body,) = fake.chats
    fixture = Path(ES.FIXTURES, ES.HALYARD)
    assert up["purpose"] == "document" and up["conversation_id"] == body["conversation_id"]
    assert up["filename"] == "halyard-h2-spec.md" and up["data"] == fixture.read_bytes()
    assert body["pdf_uploads"] == [{"upload_id": up["upload_id"], "name": "halyard-h2-spec.md"}]
    (att,) = results["cases"][0]["attachments"]
    assert isinstance(att.pop("upload_s"), float)
    assert att == {"fixture": ES.HALYARD, "purpose": "document", "upload_id": up["upload_id"],
                   "name": "halyard-h2-spec.md", "bytes": fixture.stat().st_size}


def test_effort_web_search_deep_research_and_case_id_pass_through(fake, tmp_path, pwfile):
    _run(fake, tmp_path, pwfile, "--only", "EV09,EV04,EV08")
    by = {b["test_case_id"]: b for b in fake.chats}
    assert (by["EV09"]["effort"], by["EV09"]["web_search"], by["EV09"]["deep_research"]) == ("max", "on", True)
    assert (by["EV04"]["effort"], by["EV04"]["web_search"], by["EV04"]["deep_research"]) == ("fast", "auto", False)
    assert (by["EV08"]["effort"], by["EV08"]["web_search"], by["EV08"]["deep_research"]) == ("think", "off", False)
    assert len({b["intent_id"] for b in fake.chats}) == 3


def test_the_harness_body_is_unchanged_for_run_py():
    body = H.chat_body("c1", "hi", [{"role": "user", "content": "a"}], "fast", "off")
    assert body == {"message": "hi", "messages": [{"role": "user", "content": "a"}, {"role": "user", "content": "hi"}],
                    "session_id": "c1", "conversation_id": "c1", "mode": "assistant", "model": "smart",
                    "effort": "fast", "web_search": "off"}


def test_the_harness_upload_still_defaults_to_a_dataset_csv(fake, tmp_path):
    csv = tmp_path / "t.csv"
    csv.write_text("a,b\n1,2\n")
    client = H.Client(fake.base, EMAIL, PASSWORD)
    client.upload("conv-1", str(csv))
    (up,) = fake.uploads
    assert (up["purpose"], up["content_type"], up["conversation_id"]) == ("dataset", "text/csv", "conv-1")


# -------------------------------------------------------------- timing --

def test_timing_is_ordered_and_the_whitespace_token_is_not_the_first_answer(fake, tmp_path, pwfile):
    fake.scripts["EV01"] = {"pre_answer_delay": 0.3, "reasoning": True}
    _out, results, _ = _run(fake, tmp_path, pwfile, "--only", "EV01")
    res = results["cases"][0]["turns"][0]["result"]
    tm = res["timing"]
    assert set(tm) == TIMING_KEYS
    assert tm["first_event_s"] <= tm["first_token_s"] < tm["first_answer_s"] <= tm["total_s"]
    # the meta, the heartbeat, the step, the status and the reasoning all came
    # before the 0.3 s pause; none of them is the first answer token
    assert tm["first_event_s"] < 0.25 <= tm["first_token_s"]
    assert tm["first_answer_s"] - tm["first_token_s"] >= 0.04, "the whitespace token was taken as the answer"
    assert re.fullmatch(r"req_[0-9a-f]{32}", res["request_id"])
    assert res["reasoning_events"] == 1


def test_a_stream_of_steps_and_heartbeats_has_no_first_answer(fake):
    fake.scripts["EV01"] = {"answer": ""}
    client = H.Client(fake.base, EMAIL, PASSWORD)
    res = client.chat("conv-2", "hi", [], "fast", test_case_id="EV01")
    tm = res["timing"]
    assert tm["first_event_s"] is not None and tm["first_token_s"] is not None
    assert tm["first_answer_s"] is None, "only whitespace was streamed: no meaningful answer token"
    assert res["ttft"] is not None and res["seconds"] >= 0 and res["answer"].strip() == ""


# --------------------------------------------------------------- records --

def test_results_json_has_the_schema_1_shape(fake, tmp_path, pwfile):
    _out, results, _ = _run(fake, tmp_path, pwfile, "--only", "EV03,EV01", "--label", "fake stack, cap 2")
    assert set(results) == RUN_KEYS
    assert (results["kind"], results["schema"]) == ("evalset", 1)
    assert results["finished"] and isinstance(results["seconds"], int)
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d{4}", results["started"])
    cond = results["conditions"]
    assert set(cond) == CONDITION_KEYS
    assert (cond["base"], cond["label"], cond["workers"], cond["repeats"], cond["only"]) == (
        fake.base, "fake stack, cap 2", 1, 1, ["EV03", "EV01"])
    assert cond["harness_commit"] is None or re.fullmatch(r"[0-9a-f]{40}", cond["harness_commit"])
    assert [r["id"] for r in results["cases"]] == ["EV01", "EV03"], "sorted in the set's order"
    for rec in results["cases"]:
        assert set(rec) == RECORD_KEYS
        assert rec["workload"] == RE.WORKLOAD[rec["id"]] == "direct_fast" and rec["repeat"] == 1
        assert rec["error"] is None and rec["attachments"] == []
        (turn,) = rec["turns"]
        assert set(turn) == {"message", "expect", "checks", "result"}
        assert turn["expect"] == ES.BY_ID[rec["id"]]["turns"][0]["expect"]
        assert set(turn["result"]) == RESULT_KEYS
        assert turn["result"]["source_passages"] == {} and turn["result"]["source_passages_captured"] is False
        assert all(set(c) == {"check", "dimension", "ok", "detail"} for c in turn["checks"])


def test_the_trace_becomes_stages_offsets_and_usage(fake, tmp_path, pwfile):
    fake.scripts["EV01"] = {"trace_running_reads": 2}
    _out, results, _ = _run(fake, tmp_path, pwfile, "--only", "EV01")
    res = results["cases"][0]["turns"][0]["result"]
    assert res["stages"] == {"MODE_RESOLVED": 12, "CONTEXT_ASSEMBLED": 30, "MODEL_PROMPT_PREPARED": 1,
                             "MODEL_DISPATCHED": 0, "MODEL_FIRST_CHUNK": 80, "FIRST_ANSWER_TOKEN": 150,
                             "MODEL_STREAM_ENDED": 200, "MODEL_PROMPT_PREPARED#2": 1, "MODEL_FIRST_CHUNK#2": 40,
                             "MODEL_STREAM_ENDED#2": 20}
    assert res["stage_offsets"]["REQUEST_RECEIVED"] == 2 and res["stage_offsets"]["RESPONSE_GENERATED"] == 395
    assert res["stage_offsets"]["MODEL_STREAM_ENDED#2"] == 390
    assert res["usage"] == {"prompt_tokens": 170, "completion_tokens": 35, "calls_counted": 2, "calls_total": 2}
    assert res["server_total_ms"] == 400 and res["trace_status"] == "ok", "a 'running' trace is read again"
    assert res["versions"] == {"application": "1.0", "model": "fake-model", "trace_schema": "1.0.0"}
    assert res["trace_thinks"] is False


def test_a_fast_turn_whose_trace_says_thinking_fails_thinking_off(fake, tmp_path, pwfile):
    fake.scripts["EV01"] = {"trace_thinking": True}
    _out, results, summary = _run(fake, tmp_path, pwfile, "--only", "EV01")
    turn = results["cases"][0]["turns"][0]
    assert turn["result"]["trace_thinks"] is True and _failed(turn) == ["thinking_off"]
    assert summary["headline"]["fast_turns_thinking"] == 1


def test_no_trace_means_empty_stages_and_null_usage():
    out = RE.reduce_trace(None)
    assert out["stages"] == {} and out["stage_offsets"] == {}
    assert out["usage"] == {"prompt_tokens": None, "completion_tokens": None, "calls_counted": 0, "calls_total": None}
    assert RE.trace_thinks(None) is None


def test_a_count_the_runtime_did_not_report_stays_null():
    trace = {"started_at": "2026-10-04T00:00:00+00:00", "final_status": "ok", "events": [
        {"stage": "MODEL_STREAM_ENDED", "duration_ms": 5, "completed_at": "2026-10-04T00:00:01+00:00",
         "details": {"usage": {"prompt_tokens": None, "completion_tokens": 9}}},
        {"stage": "MODEL_STREAM_ENDED", "duration_ms": 6, "completed_at": "2026-10-04T00:00:02+00:00",
         "details": {"usage": None}}]}
    out = RE.reduce_trace(trace)
    assert out["usage"] == {"prompt_tokens": None, "completion_tokens": 9, "calls_counted": 1, "calls_total": None}
    assert out["stages"] == {"MODEL_STREAM_ENDED": 5, "MODEL_STREAM_ENDED#2": 6}
    assert out["stage_offsets"] == {"MODEL_STREAM_ENDED": 1000, "MODEL_STREAM_ENDED#2": 2000}


def test_repeats_make_one_record_each_with_its_own_conversation(fake, tmp_path, pwfile):
    _out, results, summary = _run(fake, tmp_path, pwfile, "--only", "EV01,RQ03", "--repeats", "2")
    assert [(r["id"], r["repeat"]) for r in results["cases"]] == [("EV01", 1), ("EV01", 2), ("RQ03", 1), ("RQ03", 2)]
    convs = [r["conversation_id"] for r in results["cases"]]
    assert len(set(convs)) == 4 and all(re.fullmatch(r"aiq-(EV01|RQ03)-r[12]-\d+", c) for c in convs)
    assert {b["conversation_id"] for b in fake.chats} == set(convs)
    ev01 = next(c for c in summary["cases"] if c["id"] == "EV01")
    assert [r["repeat"] for r in ev01["repeats"]] == [1, 2] and ev01["pass_rate"] == 1.0
    assert fake.logins == 1, "one sign-in per worker, not one per case"


def test_a_server_error_on_one_case_is_recorded_and_the_run_goes_on(fake, tmp_path, pwfile):
    fake.scripts["RQ03"] = {"status": 500}
    _out, results, summary = _run(fake, tmp_path, pwfile, "--only", "EV01,RQ03,EV03")
    by = {r["id"]: r for r in results["cases"]}
    assert by["RQ03"]["error"].startswith("ChatFailed: /chat returned HTTP 500")
    (turn,) = by["RQ03"]["turns"]
    assert turn["result"]["http"] == 500 and turn["result"]["errors"][0]["http"] == 500
    assert turn["result"]["timing"] == dict.fromkeys(TIMING_KEYS), "an HTTP error is no latency sample"
    assert re.fullmatch(r"req_[0-9a-f]{32}", turn["result"]["request_id"])
    assert by["EV01"]["error"] is None and by["EV03"]["error"] is None
    rq03 = next(c for c in summary["cases"] if c["id"] == "RQ03")
    assert rq03["repeats"][0]["score"] == 0.0 and rq03["pass_rate"] == 0.0
    assert summary["headline"]["errors"] == 1 and summary["headline"]["records_all_pass"] == 2


def test_a_case_past_its_timeout_is_stopped_and_recorded(fake, tmp_path, pwfile):
    fake.scripts["EV01"] = {"stall_s": 5}
    t0 = time.monotonic()
    _out, results, _ = _run(fake, tmp_path, pwfile, "--only", "EV01", "--case-timeout-s", "0.5")
    assert time.monotonic() - t0 < 4
    rec = results["cases"][0]
    assert rec["error"].startswith("CaseTimeout:") and rec["turns"][0]["result"]["timed_out"] is True
    assert fake.stops == [{"conversation_id": rec["conversation_id"], "session_id": rec["conversation_id"]}]


def test_a_silent_stream_cut_at_the_case_limit_is_a_timeout_not_a_dead_pipe(fake, tmp_path, pwfile):
    # the read timeout is the case's time left (60 s idle limit not reached): when it fires the case's
    # time is up, so the turn is timed_out like any other cut, and what it streamed is kept
    fake.scripts["EV01"] = {"silent_s": 3}
    _out, results, _ = _run(fake, tmp_path, pwfile, "--only", "EV01,EV03", "--case-timeout-s", "0.6")
    ev01, ev03 = results["cases"]
    assert ev01["error"].startswith("CaseTimeout:")
    res = ev01["turns"][0]["result"]
    assert (res["timed_out"], res["timing"]["total_s"]) == (True, None)
    assert res["answer"] == "\n" + EA.GOOD["EV01"]["answer"][:40] and res["timing"]["first_answer_s"] is not None
    assert fake.stops == [{"conversation_id": ev01["conversation_id"], "session_id": ev01["conversation_id"]}]
    assert ev03["error"] is None, "the next case still runs"


def test_results_json_is_rewritten_after_every_record(fake, tmp_path, pwfile, monkeypatch):
    counts = []
    real = RE._write_json

    def spy(path, data):
        if path.endswith("results.json"):
            counts.append(len(data["cases"]))
        real(path, data)

    monkeypatch.setattr(RE, "_write_json", spy)
    _run(fake, tmp_path, pwfile, "--only", "EV01,EV03,RQ03")
    assert counts == [0, 1, 2, 3, 3]


def test_a_coding_turn_is_run_through_the_sandbox_and_scored(fake, tmp_path, pwfile, monkeypatch):
    calls = []
    _sandbox(monkeypatch)
    monkeypatch.setattr(RE.code_sandbox, "run_code", _fake_run_code(calls))
    out, results, _ = _run(fake, tmp_path, pwfile, "--only", "RQ05")
    (spec, answer, workdir), = calls
    assert spec == ES.BY_ID["RQ05"]["turns"][0]["expect"]["code"] and answer.strip() == EA.GOOD["RQ05"]["answer"].strip()
    assert workdir.startswith(str(out))
    turn = results["cases"][0]["turns"][0]
    assert turn["result"]["code_result"]["ok"] is True
    assert {"code_present", "code_runs", "code_correct"} <= {c["check"] for c in turn["checks"]}
    assert _failed(turn) == []


# ------------------------------------------------------------- scoring --

@pytest.mark.parametrize("cid", ["EV03", "RQ03", "EV04", "RQ07"])
def test_a_good_answer_passes_its_case_end_to_end(fake, tmp_path, pwfile, cid):
    _out, results, summary = _run(fake, tmp_path, pwfile, "--only", cid)
    turn = results["cases"][0]["turns"][0]
    assert turn["checks"] and _failed(turn) == [], [(c["check"], c["detail"]) for c in turn["checks"] if not c["ok"]]
    assert summary["cases"][0]["pass_rate"] == 1.0 and summary["cases"][0]["mean_score"] == 1.0


@pytest.mark.parametrize("cid,label", [("RQ03", "sentence"), ("RQ03", "with_roles"),
                                       ("EV03", "wrong_sum_two_synonyms"), ("EV04", "snippet_only")])
def test_a_named_bad_answer_fails_its_named_checks_end_to_end(fake, tmp_path, pwfile, cid, label):
    bad = next(b for b in EA.BAD[cid] if b["label"] == label)
    fake.scripts[cid] = {"answer": bad["answer"], "sources": bad.get("sources") or []}
    _out, results, summary = _run(fake, tmp_path, pwfile, "--only", cid)
    turn = results["cases"][0]["turns"][0]
    assert _failed(turn) == sorted(bad["fails"])
    assert summary["cases"][0]["pass_rate"] == 0.0


def test_passage_support_fails_because_no_passage_text_is_captured(fake, tmp_path, pwfile):
    fake.scripts["EV09"] = {"sources": EA.GOOD["EV09"]["sources"]}
    _out, results, _ = _run(fake, tmp_path, pwfile, "--only", "EV09")
    turn = results["cases"][0]["turns"][0]
    assert _failed(turn) == ["citation_passages"]
    detail = next(c["detail"] for c in turn["checks"] if c["check"] == "citation_passages")
    assert "no passage captured" in detail


def test_rescore_rescores_the_stored_answers_without_the_network(fake, tmp_path, pwfile, monkeypatch, capsys):
    out, results, _ = _run(fake, tmp_path, pwfile, "--only", "RQ03")
    assert _failed(results["cases"][0]["turns"][0]) == []
    results["cases"][0]["turns"][0]["result"]["answer"] = "The platform team is Avery Quinn, Chen Okafor and Eli Navarro."
    results["cases"][0]["turns"][0]["checks"] = []
    (out / "results.json").write_text(json.dumps(results))
    _no_network(monkeypatch)
    assert RE.main(["--rescore", str(out)]) == 0
    again = json.loads((out / "results.json").read_text())
    assert _failed(again["cases"][0]["turns"][0]) == ["names_only"]
    summary = json.loads((out / "summary.json").read_text())
    assert summary["cases"][0]["pass_rate"] == 0.0
    assert "RQ03" in capsys.readouterr().out


# -------------------------------------------------------- the guard rails --

def test_the_password_never_reaches_stdout_or_the_run_files(fake, tmp_path, pwfile, capsys):
    out, _results, _ = _run(fake, tmp_path, pwfile, "--only", "EV01,RQ03")
    printed = capsys.readouterr()
    for blob in (printed.out, printed.err, (out / "results.json").read_text(), (out / "summary.json").read_text()):
        assert PASSWORD not in blob
        assert EMAIL not in blob


def test_the_password_can_come_from_stdin_or_the_environment(fake, tmp_path, monkeypatch, capsys):
    out = tmp_path / "stdin-run"
    monkeypatch.setattr(sys, "stdin", io.StringIO(PASSWORD + "\n"))
    assert RE.main(["--base", fake.base, "--email", EMAIL, "--password-stdin", "--only", "EV01", "--out", str(out)]) == 0
    monkeypatch.setenv("AIQ_PASSWORD", PASSWORD)
    monkeypatch.setenv("AIQ_EMAIL", EMAIL)
    assert RE.main(["--base", fake.base, "--only", "EV01", "--out", str(tmp_path / "env-run")]) == 0
    assert PASSWORD not in capsys.readouterr().out
    assert fake.logins == 2


def test_there_is_no_password_option_on_the_command_line():
    with pytest.raises(SystemExit):
        RE.build_parser().parse_args(["--base", "http://127.0.0.1:28080", "--password", PASSWORD])


def test_a_missing_password_is_refused(fake, tmp_path, monkeypatch):
    monkeypatch.delenv("AIQ_PASSWORD", raising=False)
    with pytest.raises(SystemExit, match="no password"):
        RE.main(["--base", fake.base, "--email", EMAIL, "--out", str(tmp_path / "x")])
    assert fake.chats == []


@pytest.mark.parametrize("workers", ["3", "0"])
def test_more_than_two_workers_is_refused(monkeypatch, workers):
    _no_network(monkeypatch)
    with pytest.raises(SystemExit, match="inference cap"):
        RE.main(["--base", "http://127.0.0.1:28080", "--workers", workers, "--dry-run"])


@pytest.mark.parametrize("base", ["http://example.com:28080", "http://localhost.example.com:28080",
                                  "https://ai.example.org", "http://someone@127.0.0.1:28080",
                                  "ftp://127.0.0.1:28080", "http://127.0.0.1:8080", "http://localhost:3000"])
def test_a_non_loopback_or_production_base_is_refused(monkeypatch, base):
    _no_network(monkeypatch)
    with pytest.raises(SystemExit, match="refusing"):
        RE.main(["--base", base, "--dry-run"])


def test_an_unknown_case_id_is_refused(monkeypatch):
    _no_network(monkeypatch)
    with pytest.raises(SystemExit, match="unknown case id"):
        RE.main(["--base", "http://127.0.0.1:28080", "--only", "EV01,ZZ99", "--dry-run"])


def test_the_dry_run_prints_every_case_and_calls_nothing(monkeypatch, capsys):
    _no_network(monkeypatch)
    assert RE.main(["--base", "http://127.0.0.1:28080", "--dry-run"]) == 0
    out = capsys.readouterr().out
    for cid in ES.BY_ID:
        assert f"\n{cid}  effort=" in out
    assert out.count("POST /uploads") == 2 and out.count("POST /chat ") == len(ES.EVAL_SET_CASES)
    assert '"deep_research": true' in out and "18 history + 1 user" in out


def test_health_keeps_names_and_statuses_only(fake, tmp_path, pwfile):
    _out, results, _ = _run(fake, tmp_path, pwfile, "--only", "EV01")
    assert results["conditions"]["health"] == {"status": "degraded",
                                               "checks": {"duckdb": "error", "vllm": "ok", "weird": "other"}}
    assert "engine.invalid" not in json.dumps(results["conditions"])


def test_the_workload_map_matches_the_spec():
    want = {"direct_fast": ["EV01", "EV02", "EV03", "RQ01", "RQ03", "RQ05", "RQ06", "RQ07"],
            "evidence_fast": ["EV05", "RQ02"], "live_search_fast": ["EV04", "RQ04"], "long_context": ["EV07"],
            "think": ["EV06", "EV08"], "max": ["EV09"]}
    got: dict = {}
    for cid, wl in RE.WORKLOAD.items():
        got.setdefault(wl, []).append(cid)
    assert {k: sorted(v) for k, v in got.items()} == {k: sorted(v) for k, v in want.items()}


def test_two_workers_run_side_by_side(fake, tmp_path, pwfile):
    _out, results, _ = _run(fake, tmp_path, pwfile, "--only", "EV01,EV03,RQ03,RQ07", "--workers", "2")
    assert [r["id"] for r in results["cases"]] == ["EV01", "EV03", "RQ03", "RQ07"]
    assert all(r["error"] is None for r in results["cases"]) and fake.logins == 2
    assert results["conditions"]["workers"] == 2


# ------------------------------------------ failed turns are no samples --

def test_a_timed_out_turn_keeps_its_first_answer_but_has_no_total(fake, tmp_path, pwfile):
    fake.scripts["EV06"] = {"stall_s": 5}
    _out, results, summary = _run(fake, tmp_path, pwfile, "--only", "EV06", "--case-timeout-s", "0.5")
    rec = results["cases"][0]
    tm = rec["turns"][0]["result"]["timing"]
    assert rec["error"].startswith("CaseTimeout:")
    assert tm["first_answer_s"] is not None, "what happened before the cut is kept"
    assert tm["total_s"] is None, "a turn cut at --case-timeout-s is no total_s sample"
    assert summary["by_workload"]["think"]["total_s_n"] == 0


def test_a_stream_that_ends_in_error_fails_the_case_and_is_no_sample(fake, tmp_path, pwfile):
    fake.scripts["EV01"] = {"end": "error"}
    _out, results, summary = _run(fake, tmp_path, pwfile, "--only", "EV01,EV03")
    ev01, ev03 = results["cases"]
    res = ev01["turns"][0]["result"]
    assert res["terminal"] == "error" and res["errors"] == [{"message": "The model is unavailable."}]
    assert res["timing"]["total_s"] is None and res["timing"]["first_answer_s"] is not None
    assert ev01["error"].startswith("StreamFailed:")
    assert summary["cases"][0]["repeats"][0]["score"] == 0.0 and summary["cases"][0]["pass_rate"] == 0.0
    assert ev03["error"] is None and ev03["turns"][0]["result"]["terminal"] == "done"


def test_a_stream_with_no_terminal_event_is_a_stream_error(fake, tmp_path, pwfile):
    fake.scripts["EV01"] = {"end": "none"}
    _out, results, summary = _run(fake, tmp_path, pwfile, "--only", "EV01")
    rec = results["cases"][0]
    res = rec["turns"][0]["result"]
    assert res["terminal"] is None and res["errors"] == [{"error": "stream ended without done"}]
    assert res["answer"] == "\n" + EA.GOOD["EV01"]["answer"], "the whole answer arrived; only the done did not"
    assert res["timing"]["total_s"] is None
    assert rec["error"].startswith("StreamFailed:") and summary["cases"][0]["mean_score"] == 0.0
    assert summary["headline"]["turns_with_stream_errors"] == 1


def test_the_harness_reports_the_terminal_event(fake):
    client = H.Client(fake.base, EMAIL, PASSWORD)
    res = client.chat("conv-t", "hi", [], "fast", test_case_id="EV01")
    assert (res["terminal"], res["errors"], res["bad_frames"]) == ("done", [], 0)
    assert res["timing"]["total_s"] is not None


# ----------------------------------------------------------- SSE lines --

@pytest.mark.parametrize("sep", ["\u2028", "\u2029", "\x85"])
def test_line_separators_inside_a_frame_are_kept(fake, sep):
    # app/sse.py writes these raw (ensure_ascii=False); a splitlines reader cut the frame in two
    answer = f"The battery lasts 14 months{sep}at the 5-minute interval, sampled hourly."
    sources = [{"n": 1, "url": "https://example.org/x", "title": f"Spec{sep}sheet", "read": True}]
    fake.scripts["EV01"] = {"answer": answer, "sources": sources}
    client = H.Client(fake.base, EMAIL, PASSWORD)
    res = client.chat("conv-sep", "q", [], "fast", test_case_id="EV01")
    assert res["answer"] == "\n" + answer  # the fake's whitespace token, then the answer
    assert res["meta"]["sources"] == sources and res["meta"]["route"] == "chat"
    assert res["bad_frames"] == 0 and res["terminal"] == "done"


def test_a_data_line_that_is_not_json_is_counted(fake, tmp_path, pwfile):
    fake.scripts["EV01"] = {"raw_frames": ["event: status\ndata: {not json\n\n"]}
    _out, results, summary = _run(fake, tmp_path, pwfile, "--only", "EV01")
    res = results["cases"][0]["turns"][0]["result"]
    assert res["bad_frames"] == 1 and res["answer"] == "\n" + EA.GOOD["EV01"]["answer"]
    assert summary["headline"]["turns_with_bad_frames"] == 1


def test_sse_lines_splits_on_newline_only_across_chunk_boundaries():
    raw = "event: token\r\ndata: {\"text\": \"é\u2028ü\"}\n\n: c\n".encode("utf-8")
    cut = raw.index("é".encode()) + 1  # inside the two bytes of é
    chunks = [raw[:cut], raw[cut:cut + 3], raw[cut + 3:]]
    assert list(H.sse_lines(chunks)) == ["event: token", 'data: {"text": "é\u2028ü"}', "", ": c"]
    assert list(H.sse_lines([b"data: 1\r\n", b"data: 2"])) == ["data: 1", "data: 2"]


# ------------------------------------------------------- stop, trace --

def test_a_timed_out_turn_is_stopped_before_its_trace_is_read_once(fake, tmp_path, pwfile):
    fake.scripts["EV01"] = {"stall_s": 5, "trace_running_reads": 99}
    t0 = time.monotonic()
    _out, results, _ = _run(fake, tmp_path, pwfile, "--only", "EV01", "--case-timeout-s", "0.5")
    rec = results["cases"][0]
    whats = [w for _t, w in fake.timeline]
    stop_at = next(t for t, w in fake.timeline if w.startswith("stop "))
    assert stop_at - t0 < 1.5, "the stop went out late"
    assert whats.index(f"stop {rec['conversation_id']}") < next(i for i, w in enumerate(whats) if w.startswith("trace "))
    assert sum(w.startswith("trace ") for w in whats) == 1, "a stopped turn's trace is read once, not waited on"
    assert rec["stop"] == {"http": 200, "stopped": True} and len(fake.stops) == 1
    assert rec["turns"][0]["result"]["trace_status"] == "running"


def test_a_stop_that_cannot_be_sent_is_recorded_not_raised(fake):
    client = RE.EvalClient(fake.base, EMAIL, PASSWORD)
    assert client.stop("conv-x") == {"http": 200, "stopped": True}
    client.base = "http://127.0.0.1:1"  # nothing listens there
    out = client.stop("conv-x")
    assert set(out) == {"error"} and out["error"].startswith("ConnectError")


def test_a_trace_that_is_not_there_yet_is_read_again(fake, tmp_path, pwfile):
    fake.scripts["EV01"] = {"trace_404_reads": 2}
    _out, results, summary = _run(fake, tmp_path, pwfile, "--only", "EV01")
    res = results["cases"][0]["turns"][0]["result"]
    assert res["trace_status"] == "ok" and res["stages"]["MODE_RESOLVED"] == 12
    assert list(fake.trace_reads.values()) == [3]
    assert summary["headline"]["turns_trace_not_ok"] == 0


def test_a_trace_that_never_appears_is_counted_in_the_headline(fake, tmp_path, pwfile, monkeypatch):
    monkeypatch.setattr(RE, "TRACE_RETRY_S", 0.01)
    fake.scripts["EV01"] = {"trace_404_reads": 99}
    _out, results, summary = _run(fake, tmp_path, pwfile, "--only", "EV01,EV03")
    assert results["cases"][0]["turns"][0]["result"]["trace_status"] is None
    assert max(fake.trace_reads.values()) == RE.TRACE_RETRIES
    assert summary["headline"]["turns_trace_not_ok"] == 1


# ------------------------------------------------------------- timeouts --

def test_sign_in_is_bounded(fake, monkeypatch):
    monkeypatch.setattr(RE, "REQUEST_TIMEOUT_S", 0.3)
    fake.login_delay_s = 2.0
    t0 = time.monotonic()
    with pytest.raises(httpx.ReadTimeout):
        RE.EvalClient(fake.base, EMAIL, PASSWORD)
    assert time.monotonic() - t0 < 1.5


def test_a_silent_stream_fails_at_the_idle_limit_not_the_case_limit(fake, tmp_path, pwfile, monkeypatch):
    monkeypatch.setattr(RE, "STREAM_IDLE_S", 0.4)
    fake.scripts["EV01"] = {"silent_s": 3}
    t0 = time.monotonic()
    _out, results, _ = _run(fake, tmp_path, pwfile, "--only", "EV01", "--case-timeout-s", "60")
    assert time.monotonic() - t0 < 2.5
    rec = results["cases"][0]
    assert rec["error"].startswith("ReadTimeout") and rec["stop"] == {"http": 200, "stopped": True}


def test_the_default_case_timeout_outlasts_the_servers_wall_clock(fake, tmp_path, pwfile):
    assert RE.build_parser().parse_args([]).case_timeout_s == 2400.0 > 1800.0
    _out, results, _ = _run(fake, tmp_path, pwfile, "--only", "EV01")
    assert results["conditions"]["case_timeout_s"] == 2400.0


# ------------------------------------------------------- interruption --

def test_a_baseexception_stops_the_run_and_the_generation_in_flight(fake, tmp_path, pwfile, monkeypatch):
    fake.scripts["EV03"] = {"stall_s": 3}
    real_score = RE.score_turn

    def score(case, turn, result):
        if case["id"] == "EV01":
            time.sleep(0.4)  # EV03 is streaming on the other worker by now
            raise KeyboardInterrupt
        return real_score(case, turn, result)

    monkeypatch.setattr(RE, "score_turn", score)
    out = tmp_path / "run"
    with pytest.raises(KeyboardInterrupt):
        RE.main(["--base", fake.base, "--email", EMAIL, "--password-file", pwfile, "--out", str(out),
                 "--only", "EV01,EV03,RQ03,RQ06,RQ07", "--workers", "2"])
    results = json.loads((out / "results.json").read_text())
    assert results["interrupted"] is True and results["interrupted_by"] == "KeyboardInterrupt"
    assert results["finished"] and isinstance(results["seconds"], int)
    ev03 = next(b["conversation_id"] for b in fake.chats if b["test_case_id"] == "EV03")
    assert results["stopped_on_interrupt"] == [{"conversation_id": ev03, "http": 200, "stopped": True}]
    time.sleep(0.5)
    assert {b["test_case_id"] for b in fake.chats} == {"EV01", "EV03"}, "no case started after the interrupt"
    assert json.loads((out / "summary.json").read_text())["headline"]["interrupted"] is True


def test_ctrl_c_exits_130_keeps_what_finished_and_stops_what_runs(fake, tmp_path, pwfile):
    for cid in ("EV01", "EV02", "EV03", "RQ01"):
        fake.scripts[cid] = {"stall_s": 1.0}
    out = tmp_path / "run"
    proc = subprocess.Popen([sys.executable, os.path.join(RE.HERE, "run_evalset.py"), "--base", fake.base,
                             "--email", EMAIL, "--password-file", pwfile, "--out", str(out),
                             "--only", "EV01,EV02,EV03,RQ01", "--code-root", str(tmp_path / "cr")],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    deadline = time.monotonic() + 30
    while len(fake.chats) < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert len(fake.chats) == 2, "the second case never started"
    time.sleep(0.2)
    sent = time.monotonic()
    proc.send_signal(signal.SIGINT)
    output, _ = proc.communicate(timeout=30)
    assert proc.returncode == RE.EXIT_SIGINT, output
    assert not [w for t, w in fake.timeline if t > sent and w.startswith("chat ")], "a case started after Ctrl-C"
    results = json.loads((out / "results.json").read_text())
    assert results["interrupted"] is True and results["finished"]
    assert [r["id"] for r in results["cases"]] == ["EV01"], "the finished case is kept"
    second = fake.chats[1]["conversation_id"]
    assert results["stopped_on_interrupt"] == [{"conversation_id": second, "http": 200, "stopped": True}]
    assert PASSWORD not in output


def test_a_worker_that_cannot_sign_in_records_errors_and_the_run_goes_on(fake, tmp_path, pwfile):
    fake.fail_login_after = 1  # the first worker's sign-in works, the second's is refused
    fake.scripts["EV01"] = {"pre_answer_delay": 0.3}
    _out, results, summary = _run(fake, tmp_path, pwfile, "--only", "EV01,EV03,RQ03,RQ06", "--workers", "2")
    assert len(results["cases"]) == 4
    errors = [r["error"] for r in results["cases"] if r["error"]]
    assert errors and all(e.startswith("LoginFailed: login failed: 503") for e in errors)
    assert any(r["error"] is None for r in results["cases"])
    assert summary["headline"]["errors"] == len(errors)


def test_parse_deadline_is_the_next_time_the_clock_shows_it():
    ist = timezone(timedelta(hours=5, minutes=30))
    at = datetime(2026, 10, 4, 5, 10, 30, tzinfo=ist)
    assert RE.parse_deadline("07:00", at) == datetime(2026, 10, 4, 7, 0, tzinfo=ist)
    assert RE.parse_deadline("05:10", at) == datetime(2026, 10, 5, 5, 10, tzinfo=ist), "already past: tomorrow"
    assert RE.parse_deadline("7:05", datetime(2026, 10, 4, 23, 0, tzinfo=ist)) == datetime(2026, 10, 5, 7, 5, tzinfo=ist)
    for bad in ("24:00", "7", "07:60", "07:00pm", ""):
        with pytest.raises(SystemExit, match="HH:MM"):
            RE.parse_deadline(bad, at)


def test_no_case_starts_after_the_deadline(fake, tmp_path, pwfile, monkeypatch):
    soon = datetime.now().astimezone() + timedelta(seconds=0.3)
    monkeypatch.setattr(RE, "parse_deadline", lambda text, now: soon)
    fake.scripts["EV01"] = {"pre_answer_delay": 0.6}
    _out, results, summary = _run(fake, tmp_path, pwfile, "--only", "EV01,EV03,RQ03", "--deadline", "07:00")
    assert [r["id"] for r in results["cases"]] == ["EV01"] and results["cases"][0]["error"] is None
    assert results["deadline_reached"] is True and results["interrupted"] is False and results["finished"]
    assert results["conditions"]["deadline"] == soon.isoformat(timespec="minutes")
    assert [b["test_case_id"] for b in fake.chats] == ["EV01"]
    assert summary["headline"]["deadline_reached"] is True


# --------------------------------------------------------- account state --

def test_a_fresh_account_is_counted_and_recorded(fake, tmp_path, pwfile):
    _out, results, _ = _run(fake, tmp_path, pwfile, "--only", "EV01")
    acct = results["conditions"]["account"]
    assert acct == {"checked": True, "conversations": 0, "conversations_archived": 0, "facts": 0,
                    "allowed_used": False, "memory_switch": RE.MEMORY_SWITCH_NOT_EXPOSED}


@pytest.mark.parametrize("held", [{"conversations": 2}, {"archived": 1}, {"facts": 3}])
def test_a_used_account_is_refused(fake, tmp_path, pwfile, capsys, held):
    for k, v in held.items():
        setattr(fake, k, v)
    out = tmp_path / "run"
    rc = RE.main(["--base", fake.base, "--email", EMAIL, "--password-file", pwfile, "--out", str(out),
                  "--only", "EV01"])
    assert rc == RE.EXIT_REFUSED and fake.chats == [] and not out.exists()
    assert "--allow-used-account" in capsys.readouterr().err


def test_an_account_that_cannot_be_counted_is_refused(fake, tmp_path, pwfile, capsys):
    fake.history_status = 404
    rc = RE.main(["--base", fake.base, "--email", EMAIL, "--password-file", pwfile, "--out", str(tmp_path / "r"),
                  "--only", "EV01"])
    assert rc == RE.EXIT_REFUSED and fake.chats == []
    assert "could not count" in capsys.readouterr().err


def test_allow_used_account_runs_and_says_so_in_counts_only(fake, tmp_path, pwfile):
    fake.conversations, fake.archived, fake.facts = 4, 1, 2
    out, results, _ = _run(fake, tmp_path, pwfile, "--only", "EV01", "--allow-used-account")
    acct = results["conditions"]["account"]
    assert (acct["checked"], acct["conversations"], acct["conversations_archived"], acct["facts"],
            acct["allowed_used"]) == (True, 5, 1, 2, True)
    blob = (out / "results.json").read_text()
    assert "private title" not in blob and "a private fact" not in blob


# ------------------------------------------- features and status events --

def test_status_events_and_features_are_recorded(fake, tmp_path, pwfile):
    out, results, _ = _run(fake, tmp_path, pwfile, "--only", "EV01")
    assert results["cases"][0]["turns"][0]["result"]["status_events"] == ["Reading the question"]
    assert results["conditions"]["features"] == FEATURES, "names and booleans only"
    assert EMAIL not in (out / "results.json").read_text()


# ---------------------------------------------------------- code sandbox --

def test_an_unavailable_sandbox_refuses_a_run_with_a_coding_case(fake, tmp_path, pwfile, monkeypatch, capsys):
    _sandbox(monkeypatch, ok=False)
    rc = RE.main(["--base", fake.base, "--email", EMAIL, "--password-file", pwfile, "--out", str(tmp_path / "r"),
                  "--code-root", str(tmp_path / "cr"), "--only", "RQ05,EV01"])
    assert rc == RE.EXIT_REFUSED and fake.chats == [] and fake.logins == 0
    assert "--no-code" in capsys.readouterr().err


def test_no_code_runs_nothing_and_the_run_checks_fail(fake, tmp_path, pwfile, monkeypatch):
    _sandbox(monkeypatch, ok=False)
    real_run_code = RE.code_sandbox.run_code
    monkeypatch.setattr(RE.code_sandbox, "run_sandboxed", lambda *a, **k: pytest.fail("--no-code ran something"))
    seen = []
    monkeypatch.setattr(RE.code_sandbox, "run_code", lambda *a: seen.append(a[3]) or real_run_code(*a))
    _out, results, _ = _run(fake, tmp_path, pwfile, "--only", "RQ05", "--no-code")
    sb = results["conditions"]["code_sandbox"]
    assert (sb["needed"], sb["no_code"], sb["backend"], sb["available"]) == (True, True, None, {"python": False})
    turn = results["cases"][0]["turns"][0]
    assert turn["result"]["code_result"]["unavailable"] == "not run: --no-code"
    assert {"code_runs", "code_correct"} <= set(_failed(turn)), "an unrun check never passes"
    assert all(isinstance(tc, RE._NoCode) for tc in seen)


def test_an_available_sandbox_is_recorded(fake, tmp_path, pwfile, monkeypatch):
    _sandbox(monkeypatch, backend="container")
    monkeypatch.setattr(RE.code_sandbox, "run_code", _fake_run_code([]))
    _out, results, _ = _run(fake, tmp_path, pwfile, "--only", "RQ05")
    sb = results["conditions"]["code_sandbox"]
    assert (sb["needed"], sb["backend"], sb["available"], sb["no_code"]) == (True, "container", {"python": True}, False)


def test_no_coding_case_never_asks_the_sandbox(fake, tmp_path, pwfile, monkeypatch):
    monkeypatch.setattr(RE.code_sandbox, "Toolchain", lambda *a, **k: pytest.fail("the sandbox was asked"))
    _out, results, _ = _run(fake, tmp_path, pwfile, "--only", "EV01")
    assert results["conditions"]["code_sandbox"]["needed"] is False


# --------------------------------------------------------------- rescore --

def test_rescore_refuses_a_run_made_with_another_eval_set(fake, tmp_path, pwfile, monkeypatch, capsys):
    out, results, _ = _run(fake, tmp_path, pwfile, "--only", "RQ03")
    results["conditions"]["eval_set_sha256"] = "0" * 64
    (out / "results.json").write_text(json.dumps(results))
    before = (out / "results.json").read_text()
    _no_network(monkeypatch)
    assert RE.main(["--rescore", str(out)]) == RE.EXIT_REFUSED
    assert (out / "results.json").read_text() == before
    assert "--force-rescore" in capsys.readouterr().err
    assert RE.main(["--rescore", str(out), "--force-rescore"]) == 0
    again = json.loads((out / "results.json").read_text())
    rs = again["conditions"]["rescored"]
    assert (rs["eval_set_sha256"], rs["original_eval_set_sha256"], rs["forced"]) == (RE.eval_set_sha256(), "0" * 64, True)
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d{4}", rs["rescored_at"])
    assert again["conditions"]["eval_set_sha256"] == "0" * 64, "the set the run was made with stays on record"


def test_rescore_reuses_a_stored_code_result_and_says_so(fake, tmp_path, pwfile, monkeypatch):
    _sandbox(monkeypatch)
    monkeypatch.setattr(RE.code_sandbox, "run_code", _fake_run_code([]))
    out, _results, _ = _run(fake, tmp_path, pwfile, "--only", "RQ05,EV01")
    monkeypatch.setattr(RE.code_sandbox, "run_code", lambda *a: pytest.fail("a rescore ran code"))
    assert RE.main(["--rescore", str(out)]) == 0
    again = json.loads((out / "results.json").read_text())
    by = {r["id"]: r for r in again["cases"]}
    assert "code was not run again" in by["RQ05"]["rescore_note"] and "rescore_note" not in by["EV01"]
    assert again["conditions"]["rescored"]["forced"] is False
    assert _failed(by["RQ05"]["turns"][0]) == []


# ------------------------------------------------------------ the base --

@pytest.mark.parametrize("base", ["http://localhost", "https://localhost", "http://127.0.0.1",
                                  "http://127.0.0.1:28080/?x=1", "http://127.0.0.1:28080/#@example.org",
                                  "http://127.0.0.1:28080\\@example.org", "http://127.0.0.1:08080"])
def test_a_base_without_an_explicit_safe_port_is_refused(monkeypatch, base):
    _no_network(monkeypatch)
    with pytest.raises(SystemExit, match="refusing"):
        RE.main(["--base", base, "--dry-run"])


def test_an_accepted_base_is_normalised():
    assert RE.check_base("http://LOCALHOST:28080/") == "http://localhost:28080"
    assert RE.check_base("http://127.0.0.1:028080") == "http://127.0.0.1:28080"
    assert RE.check_base("https://127.0.0.1:28443") == "https://127.0.0.1:28443"


def test_the_docstring_states_the_baseline_protocol():
    doc = " ".join(RE.__doc__.split())
    assert "Use --workers 1 for a baseline" in doc
    assert ("one fresh account per run directory; the baseline is N run directories of --repeats 1, each on its "
            "own fresh account (devstack.sh seed), so repeats never see each other") in doc


# ------------------------------------------------- the harness reader --

def test_chat_stops_reading_on_cancel_while_bytes_keep_flowing(fake):
    fake.ignore_stop = True
    fake.scripts["EV01"] = {"stall_s": 30}  # a heartbeat every 50 ms, for 30 s
    client = H.Client(fake.base, EMAIL, PASSWORD)
    cancel = threading.Event()
    threading.Timer(0.4, cancel.set).start()
    t0 = time.monotonic()
    res = client.chat("conv-c", "hi", [], "fast", test_case_id="EV01", cancel=cancel)
    assert time.monotonic() - t0 < 1.5
    assert (res["cancelled"], res["timed_out"], res["terminal"], res["errors"]) == (True, False, None, [])
    assert res["timing"]["total_s"] is None and res["http"] == 200


def test_chat_cancelled_before_the_post_sends_nothing(fake):
    client = H.Client(fake.base, EMAIL, PASSWORD)
    cancel = threading.Event()
    cancel.set()
    res = client.chat("conv-c", "hi", [], "fast", test_case_id="EV01", cancel=cancel)
    assert (res["cancelled"], res["http"], res["timing"]["total_s"]) == (True, None, None)
    assert fake.chats == []
    assert client.chat("conv-d", "hi", [], "fast", test_case_id="EV01")["cancelled"] is False


def test_a_data_frame_that_is_json_but_not_an_object_is_a_bad_frame(fake, tmp_path, pwfile):
    fake.scripts["EV01"] = {"raw_frames": [f"event: {ev}\ndata: {payload}\n\n" for ev in ("token", "status", "meta")
                                           for payload in ('"just a string"', "[1, 2]", "42", "null")]}
    _out, results, _ = _run(fake, tmp_path, pwfile, "--only", "EV01")
    rec = results["cases"][0]
    res = rec["turns"][0]["result"]
    assert rec["error"] is None, rec["error"]
    assert res["bad_frames"] == 12 and res["answer"] == "\n" + EA.GOOD["EV01"]["answer"]
    assert res["status_events"] == ["Reading the question"] and res["terminal"] == "done"
