"""B-04: the evaluation-set runner, against a fake orchestrator on 127.0.0.1.

The fake speaks the parts of the orchestrator's HTTP surface the runner uses:
/auth/login (a session cookie), /auth/me, /health, /uploads (multipart),
/chat (an SSE stream with an x-request-id header), /chat/trace/{id} and
/chat/stop. Its answers are eval_set_answers.py's hand-written ones, so a
good answer must pass its case's checks end to end and a bad one must fail
exactly as tests/test_eval_set.py says it does.

Nothing here reaches a model, a stack or the network beyond 127.0.0.1, and
every run writes into pytest's tmp directories, never under runs/.
"""
from __future__ import annotations

import email.parser
import email.policy
import io
import json
import os
import re
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

PASSWORD = "test-pw-bbbbbbbb"
EMAIL = "aiq-runner@example.com"
COOKIE = "ts_session=fake-session"
RUN_KEYS = {"kind", "schema", "started", "finished", "seconds", "conditions", "cases"}
CONDITION_KEYS = {"base", "harness_commit", "label", "workers", "repeats", "only", "health", "case_timeout_s",
                  "eval_set_sha256"}
RECORD_KEYS = {"id", "category", "section", "effort", "workload", "repeat", "conversation_id", "error",
               "attachments", "turns"}
RESULT_KEYS = {"http", "answer", "errors", "request_id", "meta", "reasoning_events", "reasoning_chars", "timing",
               "timed_out", "usage", "stages", "stage_offsets", "server_total_ms", "trace_status", "versions",
               "trace_thinks", "source_passages", "source_passages_captured"}
TIMING_KEYS = {"first_event_s", "first_token_s", "first_answer_s", "total_s"}


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
        #: case id -> overrides: answer, sources, status, reasoning, trace_thinking,
        #: trace_running_reads, stall_s, pre_answer_delay
        self.scripts: dict = {}

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
                return self._json(200, {"id": 7, "email": EMAIL})
            m = re.fullmatch(r"/chat/trace/([0-9a-f]{32})", self.path)
            if m:
                with fake.lock:
                    trace = fake.traces.get(m.group(1))
                    fake.trace_reads[m.group(1)] = fake.trace_reads.get(m.group(1), 0) + 1
                    reads = fake.trace_reads[m.group(1)]
                if trace is None:
                    return self._json(404, {"detail": "query trace not found"})
                running = int(fake.script(trace["test_case_id"]).get("trace_running_reads") or 0)
                return self._json(200, {**trace, "final_status": "running" if reads <= running else "ok"})
            return self._json(404, {"detail": "not found"})

        def do_POST(self):
            raw = self._body()
            if self.path == "/auth/login":
                body = json.loads(raw or b"{}")
                if body.get("email") != EMAIL or body.get("password") != PASSWORD:
                    return self._json(401, {"detail": "Invalid email or password."})
                with fake.lock:
                    fake.logins += 1
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
                with fake.lock:
                    fake.stops.append(json.loads(raw or b"{}"))
                return self._json(200, {"stopped": True})
            if self.path == "/chat":
                return self._chat(json.loads(raw))
            return self._json(404, {"detail": "not found"})

        def _chat(self, body: dict):
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
                send(f"event: {name}\ndata: {json.dumps(data)}\n\n")

            try:
                if body.get("intent_id"):
                    ev("meta", {"generation_id": gid, "trace_id": gid, "request_id": rid,
                                "intent_id": body["intent_id"], "attempt": 1})
                send(": keep-alive\n\n")
                ev("step", {"id": 1, "title": "Working", "status": "running"})
                ev("status", {"text": "Reading the question"})
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
                    send(": keep-alive\n\n")
                    time.sleep(0.05)
                for i in range(40, len(answer), 40):
                    ev("token", {"text": answer[i:i + 40]})
                ev("step", {"id": 1, "title": "Working", "status": "done"})
                ev("meta", {"route": "chat", "sources": script["sources"], "generation_id": gid, "trace_id": gid,
                            "request_id": rid, "mode": "assistant", "effort": body.get("effort")})
                ev("done", {"session_id": body.get("session_id")})
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


def test_a_silent_stream_is_a_read_timeout_and_the_generation_is_stopped(fake, tmp_path, pwfile):
    fake.scripts["EV01"] = {"silent_s": 3}
    _out, results, _ = _run(fake, tmp_path, pwfile, "--only", "EV01,EV03", "--case-timeout-s", "0.6")
    ev01, ev03 = results["cases"]
    assert ev01["error"].startswith("ReadTimeout") and ev01["turns"] == []
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

    def run_code(spec, answer, workdir, tc):
        calls.append((spec, answer, workdir))
        return {"lang": "python", "blocks": 1, "source": answer, "ok": True,
                "steps": [{"stage": "build", "name": "compile", "ok": True, "rc": 0, "tail": ""},
                          {"stage": "check", "name": "check.py", "ok": True, "rc": 0, "tail": "ok"}]}

    monkeypatch.setattr(RE.code_sandbox, "run_code", run_code)
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
