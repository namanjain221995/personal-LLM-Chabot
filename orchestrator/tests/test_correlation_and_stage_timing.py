"""B-03: one correlation id through POST /chat, and the §11 stage times on
the trace (2026-10-03).

What is pinned:

- an inbound `X-Request-ID` is adopted only in the trace contract's shape
  (`req_` + 32 hex); anything else is replaced by a fresh id, never trimmed
  into shape; the route returns the id it used in the same header (on a 401
  too), and the trace root and the first meta event carry it;
- a request id sent twice keeps the second trace (under a fresh id) instead
  of losing it to the unique index;
- every stage time is the moment of the call, not of the (queued, grouped)
  write: a queued event whose details arrive late does not move the stages
  after it, and `started_at = completed_at - duration_ms`;
- a streamed main-model call records MODEL_PROMPT_PREPARED,
  MODEL_DISPATCHED, MODEL_FIRST_CHUNK and MODEL_STREAM_ENDED in that order,
  with the prefill as MODEL_FIRST_CHUNK's duration; nothing outside a traced
  turn; at most MAX_TRACED_MODEL_CALLS per turn, the rest counted;
- FIRST_ANSWER_TOKEN is the first answer token with text, not a status line.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from app import admission, breaker, context, continuity, db, engine_state, llm, main, metrics
from app.config import settings
from app.core import tracing

VALID = "req_" + "0123456789abcdef" * 2
SHAPE = re.compile(r"^req_[0-9a-f]{32}$")


def _request(headers: dict) -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/chat",
        "headers": [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()],
        "query_string": b"",
    }
    return Request(scope)


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


# ------------------------------------------------------------ the id itself --


def test_a_well_formed_inbound_id_is_adopted_and_cached():
    request = _request({"X-Request-ID": VALID})
    assert tracing.request_id_for(request) == VALID
    # Upper-case hex is the same shape the evaluation schema allows.
    upper = "req_" + "ABCDEF0123456789" * 2
    assert tracing.request_id_for(_request({"x-request-id": upper})) == upper


@pytest.mark.parametrize(
    "inbound",
    [
        "",
        "req-42",
        "r1",
        "req_" + "g" * 32,
        "req_" + "a" * 31,
        "req_" + "a" * 33,
        "REQ_" + "a" * 32,
        " " + VALID,
        VALID + "\n",
        "x" * 5000,
        "00000000-0000-4000-8000-000000000000",
        'req_"; DROP TABLE query_traces; --',
    ],
)
def test_anything_else_is_replaced_never_trimmed_into_shape(inbound):
    request = _request({"X-Request-ID": inbound})
    minted = tracing.request_id_for(request)
    assert SHAPE.fullmatch(minted), minted
    assert minted != inbound
    # The route and the generation it starts read the SAME id.
    assert tracing.request_id_for(request) == minted


def test_no_header_mints_one_and_two_requests_never_share_it():
    first, second = tracing.request_id_for(_request({})), tracing.request_id_for(_request({}))
    assert SHAPE.fullmatch(first) and SHAPE.fullmatch(second)
    assert first != second


def test_the_recorder_validates_ids_from_in_process_callers_too():
    assert tracing.TraceRecorder("t-1", request_id=VALID).request_id == VALID
    replaced = tracing.TraceRecorder("t-2", request_id="not an id").request_id
    assert SHAPE.fullmatch(replaced)


# ------------------------------------------------- persisted stage times --


def _user_and_conversation(name: str, conversation_id: str) -> int:
    user_id = db.create_user(name, "!x")
    db.create_conversation(user_id, conversation_id, "traced")
    return user_id


def test_stage_times_are_the_calls_not_the_writes():
    """A queued event whose details take 400 ms (CONTEXT_ASSEMBLED waiting for
    its count) holds every write behind it; the times must not move."""
    user_id = _user_and_conversation("timer", "c-times")
    recorder = main._QueuedTraceRecorder("gen-times", request_id=VALID, versions={"application": "t"})

    async def go():
        await recorder.start(
            conversation_id="c-times", user_id=user_id, workspace_id="ws",
            question="q", requested_mode="assistant",
        )
        await recorder.event("REQUEST_RECEIVED", component="t", details={})

        async def late_details() -> dict:
            await asyncio.sleep(0.4)
            return {"n": 1}

        recorder.event_when_ready("CONTEXT_ASSEMBLED", late_details, component="t", duration_ms=25)
        await asyncio.sleep(0.03)
        recorder.event_nowait("FIRST_ANSWER_TOKEN", component="t", duration_ms=40)
        await recorder.finish("ok", route="chat", resolved_mode="assistant")
        await recorder.flush()

    asyncio.run(go())
    trace = db.get_query_trace("gen-times", user_id)
    assert trace["request_id"] == VALID
    events = {e["stage"]: e for e in trace["events"]}
    assert [e["stage"] for e in trace["events"]] == ["REQUEST_RECEIVED", "CONTEXT_ASSEMBLED", "FIRST_ANSWER_TOKEN"]
    root = _ts(trace["started_at"])
    offsets = [(_ts(e["completed_at"]) - root).total_seconds() for e in trace["events"]]
    assert offsets == sorted(offsets), offsets
    # Taken at the call: well before the 400 ms build finished.
    assert offsets[1] < 0.2, offsets
    assert 0.02 <= offsets[2] < 0.25, offsets
    # The total is the finish CALL's, not the flush's.
    assert trace["total_duration_ms"] < 250, trace["total_duration_ms"]
    assert _ts(trace["completed_at"]) - root < timedelta(milliseconds=250)
    # started_at is the stage's own start: completed_at - duration_ms.
    first = events["FIRST_ANSWER_TOKEN"]
    assert _ts(first["completed_at"]) - _ts(first["started_at"]) == timedelta(milliseconds=40)


def test_a_request_id_sent_twice_keeps_both_traces():
    user_id = _user_and_conversation("twice", "c-twice")

    def run(trace_id: str) -> None:
        recorder = main._QueuedTraceRecorder(trace_id, request_id=VALID, versions={"application": "t"})

        async def go():
            await recorder.start(
                conversation_id="c-twice", user_id=user_id, workspace_id="ws",
                question="q", requested_mode="assistant",
            )
            await recorder.event("REQUEST_RECEIVED", component="t", details={"request_id": recorder.request_id})
            await recorder.finish("ok", route="chat")
            await recorder.flush()

        asyncio.run(go())

    run("gen-first")
    run("gen-second")
    first = db.get_query_trace("gen-first", user_id)
    second = db.get_query_trace("gen-second", user_id)
    assert first["request_id"] == VALID
    assert second is not None, "the reused id cost the second request its whole trace"
    assert SHAPE.fullmatch(second["request_id"]) and second["request_id"] != VALID
    assert [e["stage"] for e in second["events"]] == ["REQUEST_RECEIVED"]
    # The id the request carried is still on it.
    assert second["events"][0]["details"]["request_id"] == VALID
    assert second["final_status"] == "ok"


# ------------------------------------------------- the model call stages --

MAIN_URL = "http://vllm-main.test:8000/v1"
MODEL = "Qwen/Qwen3.6-35B-A3B-NVFP4"
MSGS = [{"role": "system", "content": "be brief"}, {"role": "user", "content": "count"}]


class _Stream:
    def __init__(self, pieces, first_delay_s: float, delay_s: float) -> None:
        self.pieces = list(pieces)
        self.first_delay_s = first_delay_s
        self.delay_s = delay_s

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for i, text in enumerate(self.pieces):
            await asyncio.sleep(self.first_delay_s if i == 0 else self.delay_s)
            last = i == len(self.pieces) - 1
            yield SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content=text), finish_reason="stop" if last else None)],
                usage=None,
            )
        yield SimpleNamespace(choices=[], usage=SimpleNamespace(prompt_tokens=12, completion_tokens=len(self.pieces)))

    async def close(self) -> None:
        pass


class _Engine:
    """A main engine whose prefill takes `prefill_s` and whose decode emits a
    piece every `decode_s`."""

    def __init__(self) -> None:
        self.prefill_s = 0.08
        self.decode_s = 0.01
        self.pieces = ["1", " 2", " 3", " 4"]
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        return _Stream(self.pieces, self.prefill_s, self.decode_s)


@pytest.fixture()
def engine(monkeypatch):
    """The real breaker, admission lanes and resilient wrapper over a fake
    engine (the pattern of test_llm_public_stream_kwargs.py)."""
    metrics.reset()
    breaker.reset()
    engine_state.reset()
    continuity.reset()
    admission.reset()
    monkeypatch.setattr(settings, "openai_base_url", MAIN_URL)
    monkeypatch.setattr(settings, "llm_model", MODEL)
    monkeypatch.setattr(settings, "admission_normal_max", 4)
    breaker.install("main", breaker.Breaker("main", external_open=engine_state.external_open))
    fake = _Engine()
    monkeypatch.setattr(llm, "_client", lambda base_url, api_key=None, **options: fake)

    async def fit(messages, *, base_url, model, requested_max_tokens=None):
        return list(messages), requested_max_tokens or 64

    async def count(base_url, model, messages):
        context._last_count_exact.set(True)
        return 40, 1000

    async def window(base_url, model):
        return 1000

    monkeypatch.setattr(llm.context, "fit_request", fit)
    monkeypatch.setattr(llm.context, "count_tokens", count)
    monkeypatch.setattr(llm.context, "model_window", window)
    yield fake
    admission.reset()
    breaker.reset()
    engine_state.reset()


async def _drain(**kwargs) -> list:
    return [item async for item in llm.stream_chat_events(MSGS, model_choice="smart", effort="fast", **kwargs)]


def test_a_streamed_call_records_its_stages_in_order(engine):
    user_id = _user_and_conversation("modeller", "c-model")
    recorder = main._QueuedTraceRecorder("gen-model", versions={"application": "t"})
    dispatched: list = []

    async def go():
        token = recorder.activate()
        try:
            await recorder.start(
                conversation_id="c-model", user_id=user_id, workspace_id="ws",
                question="q", requested_mode="assistant",
            )
            out = await _drain(max_tokens=64, on_dispatch=lambda: dispatched.append(1))
            await recorder.finish("ok", route="chat")
            await recorder.flush()
            return out
        finally:
            tracing.TraceRecorder.deactivate(token)

    out = asyncio.run(go())
    assert "".join(text for kind, text in out if kind == "token") == "1 2 3 4"
    # The caller's own hook still fires, once.
    assert dispatched == [1]

    trace = db.get_query_trace("gen-model", user_id)
    stages = [e["stage"] for e in trace["events"]]
    assert stages == ["MODEL_PROMPT_PREPARED", "MODEL_DISPATCHED", "MODEL_FIRST_CHUNK", "MODEL_STREAM_ENDED"]
    completed = [_ts(e["completed_at"]) for e in trace["events"]]
    assert completed == sorted(completed)
    events = {e["stage"]: e for e in trace["events"]}
    assert all(e["details"]["call"] == 1 for e in trace["events"])
    assert events["MODEL_PROMPT_PREPARED"]["details"]["output_budget"] == 64
    assert events["MODEL_PROMPT_PREPARED"]["details"]["model_choice"] == "smart"
    # The prefill (dispatch to first chunk) is the 80 ms the engine took.
    assert events["MODEL_FIRST_CHUNK"]["duration_ms"] >= 70, events["MODEL_FIRST_CHUNK"]
    assert events["MODEL_FIRST_CHUNK"]["details"]["dispatches"] == 1
    ended = events["MODEL_STREAM_ENDED"]
    assert ended["details"]["outcome"] == "completed"
    assert ended["details"]["finish_reason"] == "stop"
    assert ended["details"]["answer_chunks"] == 4
    assert ended["details"]["usage"] == {"prompt_tokens": 12, "completion_tokens": 4}
    # Generation: first chunk to the end, three more 10 ms pieces.
    assert ended["duration_ms"] >= 25, ended
    assert trace["meta"]["stage_counts"] == {"model_call": 1}


def test_a_cancelled_stream_says_so(engine):
    user_id = _user_and_conversation("canceller", "c-cancel")
    recorder = main._QueuedTraceRecorder("gen-cancel", versions={"application": "t"})
    engine.pieces = [f" {i}" for i in range(50)]
    engine.decode_s = 0.02

    async def go():
        token = recorder.activate()
        try:
            await recorder.start(
                conversation_id="c-cancel", user_id=user_id, workspace_id="ws",
                question="q", requested_mode="assistant",
            )
            stream = llm.stream_chat_events(MSGS, model_choice="smart", effort="fast", max_tokens=64)
            async for _kind, _text in stream:
                break  # the consumer walks away mid-answer
            await stream.aclose()
            await recorder.finish("cancelled")
            await recorder.flush()
        finally:
            tracing.TraceRecorder.deactivate(token)

    asyncio.run(go())
    trace = db.get_query_trace("gen-cancel", user_id)
    ended = next(e for e in trace["events"] if e["stage"] == "MODEL_STREAM_ENDED")
    assert ended["details"]["outcome"] == "cancelled"
    assert ended["details"]["answer_chunks"] == 1


def test_no_trace_records_nothing_and_a_turn_traces_at_most_the_cap(engine, monkeypatch):
    engine.prefill_s = 0.0
    engine.decode_s = 0.0
    calls: list = []

    async def capture(fn, *args, **kwargs):
        calls.append((fn.__name__, args, kwargs))

    monkeypatch.setattr(tracing.db, "run_in_thread", capture)

    # Outside a traced turn (/v1, warm-ups): not one write.
    assert tracing.model_call() is None
    asyncio.run(_drain(max_tokens=16))
    assert calls == []

    recorder = tracing.TraceRecorder("gen-cap")

    async def go():
        token = recorder.activate()
        try:
            for _ in range(tracing.MAX_TRACED_MODEL_CALLS + 1):
                await _drain(max_tokens=16)
            await asyncio.sleep(0.05)  # the plain recorder writes on its own tasks
            await recorder.finish("ok")
        finally:
            tracing.TraceRecorder.deactivate(token)

    asyncio.run(go())
    traced_calls = {
        args[5]["call"] for name, args, _kw in calls if name == "append_query_trace_event"
    }
    assert traced_calls == set(range(1, tracing.MAX_TRACED_MODEL_CALLS + 1))
    (finish,) = [kw for name, _args, kw in calls if name == "finish_query_trace"]
    assert finish["meta"]["stage_counts"] == {"model_call": tracing.MAX_TRACED_MODEL_CALLS + 1}


# ------------------------------------------------------- POST /chat itself --


def _parse_sse(body: str):
    events = []
    for block in body.split("\n\n"):
        lines = [line for line in block.split("\n") if line and not line.startswith(":")]
        if len(lines) >= 2 and lines[0].startswith("event: "):
            events.append((lines[0][7:], json.loads(lines[1][6:])))
    return events


@pytest.fixture()
def offline(monkeypatch):
    from app.engines import orchestrate

    monkeypatch.setattr(settings, "living_knowledge_enabled", False)
    monkeypatch.setattr(settings, "fact_extraction_enabled", False)
    monkeypatch.setattr(settings, "salesforce_intelligence_enabled", False)
    monkeypatch.setattr(settings, "clarify_before_answering", False)

    async def plan(message, history, effort):
        return orchestrate.Plan(agent=False, search=False)

    async def stream_chat_events(messages, **kwargs):
        # A blank first token is not the first MEANINGFUL answer token.
        for piece in (" ", "TCP is", " connection-oriented."):
            yield ("token", piece)

    monkeypatch.setattr(orchestrate, "decide", plan)
    monkeypatch.setattr(llm, "stream_chat_events", stream_chat_events)


def _send(client, conversation_id: str, headers: dict):
    body = {
        "message": "What is the difference between TCP and UDP?",
        "mode": "assistant",
        "effort": "fast",
        "web_search": "off",
        "conversation_id": conversation_id,
        "session_id": conversation_id,
        "intent_id": f"intent-{conversation_id}",
    }
    resp = client.post("/chat", json=body, headers=headers)
    assert resp.status_code == 200, resp.text
    events = _parse_sse(resp.text)
    assert events[-1][0] == "done", events[-3:]
    return resp, events


def test_post_chat_carries_one_id_from_header_to_trace(offline):
    with TestClient(main.app) as client:
        resp, events = _send(client, "corr-conv-1", {"X-Request-ID": VALID})
        first_meta = next(data for kind, data in events if kind == "meta")
        trace = client.get(f"/chat/trace/{first_meta['trace_id']}").json()

    assert resp.headers["x-request-id"] == VALID
    assert first_meta["request_id"] == VALID
    assert trace["request_id"] == VALID
    received = next(e for e in trace["events"] if e["stage"] == "REQUEST_RECEIVED")
    assert received["details"]["request_id"] == VALID

    stages = [e["stage"] for e in trace["events"]]
    assert stages.index("REQUEST_RECEIVED") < stages.index("CONTEXT_ASSEMBLED") \
        < stages.index("FIRST_ANSWER_TOKEN") < stages.index("RESPONSE_GENERATED")
    assert stages.count("FIRST_ANSWER_TOKEN") == 1
    root = _ts(trace["started_at"])
    by_stage = {e["stage"]: e for e in trace["events"]}
    in_order = [
        _ts(by_stage[s]["completed_at"]) - root
        for s in ("REQUEST_RECEIVED", "CONTEXT_ASSEMBLED", "FIRST_ANSWER_TOKEN", "RESPONSE_GENERATED")
    ]
    assert in_order == sorted(in_order), in_order
    assert by_stage["CONTEXT_ASSEMBLED"]["duration_ms"] is not None
    assert by_stage["FIRST_ANSWER_TOKEN"]["duration_ms"] is not None


def test_post_chat_replaces_a_malformed_id_and_still_agrees_with_itself(offline):
    with TestClient(main.app) as client:
        resp, events = _send(client, "corr-conv-2", {"X-Request-ID": "req-42\\r\\nX-Evil: 1"})
        first_meta = next(data for kind, data in events if kind == "meta")
        trace = client.get(f"/chat/trace/{first_meta['trace_id']}").json()

    minted = resp.headers["x-request-id"]
    assert SHAPE.fullmatch(minted), minted
    assert first_meta["request_id"] == minted
    assert trace["request_id"] == minted


def test_a_refused_request_still_names_its_id(anonymous_mode):
    with TestClient(main.app) as client:
        resp = client.post("/chat", json={"message": "hi"}, headers={"X-Request-ID": VALID})
        bad = client.post("/chat", json={"message": "hi"}, headers={"X-Request-ID": "nope"})
    assert resp.status_code == 401
    assert resp.headers["x-request-id"] == VALID
    assert bad.status_code == 401
    assert SHAPE.fullmatch(bad.headers["x-request-id"])


# ---------------------------------------------------------------- rerank --


class _RerankCaps:
    enabled = True
    supports_reranking = True
    requires_authentication = False


@pytest.fixture()
def reranker(monkeypatch):
    from app import rerank

    monkeypatch.setattr(settings, "rerank_enabled", True)
    monkeypatch.setattr(settings, "rerank_base_url", "http://reranker.test")
    monkeypatch.setattr(settings, "rerank_model", "test-reranker")
    monkeypatch.setattr(settings, "rerank_api_key", "")
    monkeypatch.setattr(settings, "reranker_capabilities", _RerankCaps())
    monkeypatch.setattr(settings, "rerank_canary_enabled", False)
    rerank.reset_for_tests()
    state = {"fail": False}

    async def post(query, documents, instruction, timeout):
        await asyncio.sleep(0.02)
        if state["fail"]:
            raise RuntimeError("reranker down")
        return [0.1 * (i + 1) for i in range(len(documents))]

    monkeypatch.setattr(rerank, "_post", post)
    yield rerank, state
    rerank.reset_for_tests()


def test_a_rerank_inside_a_turn_is_a_stage_and_outside_one_is_not(reranker, monkeypatch):
    rerank, state = reranker
    calls: list = []

    async def capture(fn, *args, **kwargs):
        calls.append((fn.__name__, args, kwargs))

    monkeypatch.setattr(tracing.db, "run_in_thread", capture)
    asyncio.run(rerank.score("q", ["a", "b", "c"]))
    assert calls == []

    recorder = tracing.TraceRecorder("gen-rerank")

    async def go():
        token = recorder.activate()
        try:
            await rerank.score("q", ["a", "b", "c"])
            state["fail"] = True
            with pytest.raises(rerank.RerankUnavailable):
                await rerank.score("q", ["a", "b"])
            await asyncio.sleep(0.05)
        finally:
            tracing.TraceRecorder.deactivate(token)

    asyncio.run(go())
    events = [args for name, args, _kw in calls if name == "append_query_trace_event"]
    assert [(e[2], e[3]) for e in events] == [("RERANK", "success"), ("RERANK", "failed")]
    ok, failed = events
    assert ok[5]["outcome"] == "ok" and ok[5]["documents"] == 3 and ok[5]["kind"] == rerank.BULK
    assert ok[6] >= 15  # duration_ms: the 20 ms request
    assert failed[5]["outcome"] == "error" and failed[5]["documents"] == 2


# ------------------------------------- the pre-pass stages on a full turn --

# The fully stubbed knowledge path of the Fast-lane suite (every network
# boundary replaced, every pre-pass counted). Imported, not copied, so the
# two suites cannot drift apart.
from tests.test_fast_lane_route import _send as _lane_send  # noqa: E402
from tests.test_fast_lane_route import wired  # noqa: E402,F401 — a fixture


def test_a_full_turn_records_retrieval_and_routing_with_their_durations(wired):  # noqa: F811
    history = [{"role": "user", "content": "what is the gold price today?"}]
    with TestClient(main.app) as client:
        _events, meta = _lane_send(client, "corr-conv-3", history)
        trace = client.get(f"/chat/trace/{meta['trace_id']}").json()
    assert wired["calls"]["living_knowledge.prepare"] == 1
    by_stage = {e["stage"]: e for e in trace["events"]}
    stages = [e["stage"] for e in trace["events"]]

    knowledge = by_stage["KNOWLEDGE_PREPARED"]
    assert knowledge["duration_ms"] is not None and knowledge["duration_ms"] >= 0
    assert knowledge["details"]["outcome"] == "ok"
    assert knowledge["details"]["blocked_ms"] <= knowledge["duration_ms"]
    assert knowledge["details"]["decision"] == meta["knowledge"]["decision"]

    routed = by_stage["MODE_RESOLVED"]
    assert routed["duration_ms"] is not None
    assert routed["details"]["decide_ms"] is not None
    assert routed["details"]["decide_ms"] <= routed["duration_ms"]

    # In order: routing, context, retrieval awaited, first answer token.
    order = ["REQUEST_RECEIVED", "MODE_RESOLVED", "CONTEXT_ASSEMBLED", "KNOWLEDGE_PREPARED", "FIRST_ANSWER_TOKEN"]
    assert [s for s in stages if s in order] == order
    root = _ts(trace["started_at"])
    offsets = [_ts(by_stage[s]["completed_at"]) - root for s in order]
    assert offsets == sorted(offsets), offsets
