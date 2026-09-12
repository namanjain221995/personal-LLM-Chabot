"""Generation durability under engine failure (availability CONTRACT §8.4).

The invariants, each proven here against the test database with the model
stubbed the way tests/test_chat_requests.py stubs it (or with a fake OpenAI
engine behind `llm._client`, so the real llm.py / resilience.py retry
choreography is what runs when that is what is under test):

1. a second POST /chat with the same intent_id while the generation is live
   attaches to it — one worker, one model call, one assistant row;
2. after completion the same POST replays the persisted answer — no second
   row, no second attempt in the ledger;
3. an automatic retry happens only BEFORE the first token; after the first
   token the attempt is marked `interrupted`, the partial text is kept as a
   partial, nothing re-runs by itself and no second answer is appended —
   whether the engine raised, or the chat route's continuation loop
   returned the partial;
4. every attempt records `attempt`, `engine` (`primary` in one-model mode),
   `retry_reason` and `terminal_state` — in `usage_events.meta`, one row
   per generation_id, which is one row per attempt, with no schema change;
5. a client that disconnects mid-stream reconnects (GET /chat/requests, then
   GET /chat/attach) and receives exactly one final message;
6. a restart marks open rows `interrupted`, and a later resume never
   duplicates: a lost request runs once more under a new attempt, and a
   request whose answer WAS persisted before the process died is replayed,
   not answered again.

SLO C (docs/availability/SLO.md) asks for lost and duplicate counts. Neither
can be a truthful counter — the process that loses a request is the one
that is gone — so they are SQL over chat_requests ⋈ messages (⋈ usage_events
for earlier attempts), given at the bottom and run here against planted rows.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import openai
import pytest
from fastapi.testclient import TestClient

from app import breaker, db, engine_state, llm, metrics, resilience
from app import main as app_main
from app.config import settings
from app.main import _live_generations, app

MAIN_URL = "http://vllm-main.test:8000/v1"
MAIN_MODEL = "Qwen/Qwen3.6-35B-A3B-NVFP4"

_UNAVAILABLE = (
    "The model is temporarily unavailable. It may still be starting up — "
    "please try again in a moment."
)


# ---------------------------------------------------------------------------
# Harness (the idioms of test_chat_requests.py / test_llm_resilience.py)
# ---------------------------------------------------------------------------


def _parse_sse(text: str):
    events = []
    for block in text.strip().split("\n\n"):
        lines = block.strip().split("\n")
        if len(lines) >= 2 and lines[0].startswith("event: "):
            events.append((lines[0][7:], json.loads(lines[1][6:])))
    return events


def _fake_stream(deltas, calls=None):
    async def fake(messages, **kwargs):
        if calls is not None:
            calls.append(1)
        for kind, text in deltas:
            yield kind, text

    return fake


def _gated_stream(gate: "asyncio.Event", before, calls, after=()):
    """Streams `before`, holds until `gate` is set, then streams `after` —
    a generation that is live long enough for a second request to arrive."""

    async def fake(messages, **kwargs):
        calls.append(1)
        for kind, text in before:
            yield kind, text
        await gate.wait()
        for kind, text in after:
            yield kind, text

    return fake


def _async_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _wait_until(predicate, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met in time")


def _assistant_rows(conversation_id: str) -> list:
    """Every assistant row of the thread, oldest first — the number that
    must be exactly one after any of the flows below."""
    with db.connection() as con:
        rows = con.execute(
            "SELECT generation_id, content, meta FROM messages "
            "WHERE conversation_id = %s AND role = 'assistant' ORDER BY id",
            (conversation_id,),
        ).fetchall()
    out = []
    for row in rows:
        meta = row["meta"]
        if isinstance(meta, str):
            meta = json.loads(meta)
        out.append({"generation_id": row["generation_id"], "content": row["content"], "meta": meta or {}})
    return out


def _ledger(intent_id: str) -> list:
    """The attempt ledger of one intent: usage_events rows, oldest first,
    reduced to the four CONTRACT facts plus the row's own status columns."""
    with db.connection() as con:
        rows = con.execute(
            "SELECT generation_id, status, error_kind, meta FROM usage_events "
            "WHERE meta->>'intent_id' = %s ORDER BY id",
            (intent_id,),
        ).fetchall()
    out = []
    for row in rows:
        meta = row["meta"]
        if isinstance(meta, str):
            meta = json.loads(meta)
        out.append(
            {
                "generation_id": row["generation_id"],
                "status": row["status"],
                "error_kind": row["error_kind"],
                "attempt": meta.get("attempt"),
                "engine": meta.get("engine"),
                "retry_reason": meta.get("retry_reason"),
                "terminal_state": meta.get("terminal_state"),
            }
        )
    return out


def _counter(name: str, **labels) -> float:
    key = tuple(sorted(labels.items()))
    return metrics._counters.get(name, {}).get(key, 0.0)


@pytest.fixture(autouse=True)
def _clean_process_state(monkeypatch):
    _live_generations.clear()
    metrics.reset()
    breaker.reset()
    engine_state.reset()
    # A TestClient block's lifespan exit raises the process-wide shutdown
    # flag; the next "process" of a test starts with it down.
    monkeypatch.setattr(app_main, "_shutting_down", False)
    yield
    _live_generations.clear()
    breaker.reset()
    engine_state.reset()
    metrics.reset()


def _body(conversation_id: str, intent_id: str, message: str = "hi", **extra) -> dict:
    return {"message": message, "mode": "assistant", "conversation_id": conversation_id, "intent_id": intent_id, **extra}


# --- a fake engine behind llm._client (the real llm.py / resilience.py run) -


_REQ = httpx.Request("POST", f"{MAIN_URL}/chat/completions")


def _conn_error() -> openai.APIConnectionError:
    exc = openai.APIConnectionError(request=_REQ)
    exc.__cause__ = httpx.ConnectError("connection refused")
    return exc


class _Stream:
    """A streamed reply: the text in two chunks, a finish, a usage chunk —
    or, with `die_after_first`, the first chunk and then the raw httpx
    error a stream that dies mid-body raises from its iterator."""

    def __init__(self, text: str, *, die_after_first: bool = False) -> None:
        half = len(text) // 2
        self._chunks = [
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text[:half]), finish_reason=None)], usage=None),
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text[half:]), finish_reason="stop")], usage=None),
            SimpleNamespace(choices=[], usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2)),
        ]
        self._die_after_first = die_after_first
        self.closed = False

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for index, chunk in enumerate(self._chunks):
            if self._die_after_first and index == 1:
                raise httpx.ReadError("stream died")
            yield chunk

    async def close(self) -> None:
        self.closed = True


class _Engine:
    """One fake OpenAI-compatible engine. Its first `stream_failures`
    stream opens raise `exc_factory()`; then it streams `text`. Non-stream
    calls (a sidecar that happens to share the URL) answer `text` at once
    and are counted apart, so `stream_calls` is exactly the number of times
    a generation was OPENED against this engine."""

    def __init__(self, base_url: str, text: str, *, exc_factory=None, stream_failures: int = 0, die_after_first: bool = False) -> None:
        self.base_url = base_url  # _open_stream reads it to find the breaker
        self.text = text
        self.exc_factory = exc_factory
        self.stream_failures = stream_failures
        self.die_after_first = die_after_first
        self.stream_calls: list = []
        self.other_calls: list = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        if not kwargs.get("stream"):
            self.other_calls.append(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(content=self.text, reasoning_content=None, reasoning=None, tool_calls=None, model_extra=None),
                    finish_reason="stop",
                )],
                usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2),
            )
        self.stream_calls.append(kwargs)
        if len(self.stream_calls) <= self.stream_failures:
            raise self.exc_factory()
        return _Stream(self.text, die_after_first=self.die_after_first)


class _Dead:
    """Every other endpoint (router, embeddings): unreachable. On an
    interactive turn a sidecar makes one attempt and its caller falls back
    (resilience.sidecar_recovery_s) — the same path the real client takes
    in this suite, with no network."""

    def __init__(self) -> None:
        async def refuse(**kwargs):
            raise _conn_error()

        self.chat = SimpleNamespace(completions=SimpleNamespace(create=refuse))
        self.embeddings = SimpleNamespace(create=refuse)


@pytest.fixture()
def engine(monkeypatch):
    """The primary at the main model's URL and a dead endpoint everywhere
    else; a frozen breaker clock; no backoff; /health always answers; and
    fit_request is a passthrough (no /tokenize round trip)."""
    monkeypatch.setattr(settings, "openai_base_url", MAIN_URL)
    monkeypatch.setattr(settings, "llm_model", MAIN_MODEL)
    monkeypatch.setattr(settings, "llm_interactive_recovery_s", 0.0)
    monkeypatch.setattr(settings, "llm_breaker_failures", 3)
    monkeypatch.setattr(settings, "llm_breaker_window_s", 30.0)
    monkeypatch.setattr(settings, "llm_breaker_cooldown_s", 10.0)
    monkeypatch.setattr(resilience, "_backoff_s", lambda attempt: 0.0)

    async def up(base_url, timeout=None):
        return True

    monkeypatch.setattr(resilience, "engine_answers", up)
    breaker.install(
        breaker.MAIN, breaker.Breaker(breaker.MAIN, clock=lambda: 1000.0, external_open=engine_state.external_open)
    )
    main = _Engine(MAIN_URL, "from the main model")
    dead = _Dead()

    def client(base_url, api_key=None, **kwargs):
        return main if breaker.engine_for_base_url(base_url) == breaker.MAIN else dead

    monkeypatch.setattr(llm, "_client", client)

    async def fit(messages, *, base_url, model, requested_max_tokens=None):
        return list(messages), requested_max_tokens or 64

    monkeypatch.setattr(llm.context, "fit_request", fit)
    return main


# ---------------------------------------------------------------------------
# 1 + 2. One intent, one generation, one row — live and after completion
# ---------------------------------------------------------------------------


def test_same_intent_while_live_attaches_and_never_adds_a_second_row(monkeypatch):
    calls: list = []

    async def scenario():
        gate = asyncio.Event()
        monkeypatch.setattr(
            llm, "stream_chat_events", _gated_stream(gate, [("token", "Hel")], calls, [("token", "lo!")])
        )
        body = _body("dur-1", "int-dur-1")
        async with _async_client() as client:
            first = asyncio.create_task(client.post("/chat", json=body))
            await _wait_until(lambda: bool(calls))
            gen = _live_generations["dur-1"]
            second = asyncio.create_task(client.post("/chat", json=body))
            await _wait_until(lambda: gen.subscribers >= 2)
            # Still ONE generation for the conversation, one worker, one
            # model call — the retry is a second follower of the same stream.
            assert len(_live_generations) == 1 and _live_generations["dur-1"] is gen
            assert calls == [1]
            row = db.get_chat_request("int-dur-1")
            assert row["status"] == "running" and row["generation_id"] == gen.generation_id
            gate.set()
            r1, r2 = await asyncio.gather(first, second)
        return gen, r1, r2

    gen, r1, r2 = asyncio.run(scenario())
    e1, e2 = _parse_sse(r1.text), _parse_sse(r2.text)
    assert e1[0][1]["generation_id"] == gen.generation_id == e2[0][1]["generation_id"]
    assert [k for k, _ in e1].count("done") == 1 and [k for k, _ in e2].count("done") == 1
    assert "".join(d["text"] for k, d in e2 if k == "token") == "Hello!"
    assert calls == [1]
    rows = _assistant_rows("dur-1")
    assert len(rows) == 1 and rows[0]["content"] == "Hello!"
    assert rows[0]["generation_id"] == gen.generation_id
    assert _ledger("int-dur-1") == [
        {
            "generation_id": gen.generation_id,
            "status": "ok",
            "error_kind": "",
            "attempt": 1,
            "engine": "primary",
            "retry_reason": "none",
            "terminal_state": "completed",
        }
    ]
    assert _counter("chat_request_total", result="accepted") == 1
    assert _counter("chat_request_total", result="attached") == 1
    assert _counter("chat_request_attempts_total", engine="main", terminal_state="completed") == 1


def test_same_intent_after_completion_replays_and_records_no_second_attempt(monkeypatch):
    calls: list = []
    monkeypatch.setattr(llm, "stream_chat_events", _fake_stream([("token", "Hello!")], calls))
    body = _body("dur-2", "int-dur-2")
    with TestClient(app) as client:
        first = _parse_sse(client.post("/chat", json=body).text)
        generation_id = first[0][1]["generation_id"]
        # Send again — the same intent, three times over.
        replays = [_parse_sse(client.post("/chat", json=body).text) for _ in range(3)]
    for events in replays:
        assert [k for k, _ in events] == ["meta", "token", "meta", "done"]
        assert events[0][1] == {"generation_id": generation_id, "intent_id": "int-dur-2", "attempt": 1}
        assert events[1][1] == {"text": "Hello!"}
        assert events[2][1]["generation_id"] == generation_id
    assert calls == [1], "a replay never runs the model"
    assert "dur-2" not in _live_generations
    rows = _assistant_rows("dur-2")
    assert len(rows) == 1 and rows[0]["generation_id"] == generation_id
    row = db.get_chat_request("int-dur-2")
    assert row["status"] == "completed" and row["attempt"] == 1
    # One attempt in the ledger, however many times it was asked for.
    assert [(r["attempt"], r["terminal_state"]) for r in _ledger("int-dur-2")] == [(1, "completed")]
    assert _counter("chat_request_total", result="replayed") == 3
    assert _counter("chat_request_total", result="resumed") == 0


# ---------------------------------------------------------------------------
# 3. An automatic retry: before the first token only
# ---------------------------------------------------------------------------


def test_a_failure_before_the_first_token_is_retried_on_the_primary(engine, monkeypatch):
    """The stream open fails once with a connection error; the recovery
    window is open, so the resilient wrapper retries the OPEN. That is a
    retry inside ONE attempt: one answer, one row, attempt 1, engine primary."""
    monkeypatch.setattr(settings, "llm_interactive_recovery_s", 30.0)
    engine.exc_factory = _conn_error
    engine.stream_failures = 1
    with TestClient(app) as client:
        events = _parse_sse(client.post("/chat", json=_body("retry-1", "int-retry-1", effort="fast")).text)
    assert events[-1][0] == "done"
    assert "".join(d["text"] for k, d in events if k == "token") == "from the main model"
    assert len(engine.stream_calls) == 2, "opened twice: the failed open and its retry"
    assert _counter("llm_retry_total", what="stream", reason="connection") == 1
    rows = _assistant_rows("retry-1")
    assert len(rows) == 1 and rows[0]["content"] == "from the main model"
    assert "engine" not in rows[0]["meta"]
    assert _ledger("int-retry-1") == [
        {
            "generation_id": rows[0]["generation_id"],
            "status": "ok",
            "error_kind": "",
            "attempt": 1,
            "engine": "primary",
            "retry_reason": "none",
            "terminal_state": "completed",
        }
    ]
    assert db.get_chat_request("int-retry-1")["status"] == "completed"


def test_after_the_first_token_a_dying_stream_is_kept_as_a_partial_and_never_re_run(engine, monkeypatch):
    """The chat route's continuation loop: the primary streams one chunk and
    the body dies. The recovery window is open and the breaker is CLOSED —
    and neither is used: nothing re-opens the stream, the partial is what
    the person gets (told so under it: meta.continuation.stop_reason =
    "error"), the row is completed with that partial as its durable answer,
    and the ledger says the attempt was INTERRUPTED. A repeat of the intent
    replays the partial; it does not answer again."""
    monkeypatch.setattr(settings, "llm_interactive_recovery_s", 30.0)
    engine.die_after_first = True
    body = _body("cut-1", "int-cut-1", effort="fast")
    with TestClient(app) as client:
        events = _parse_sse(client.post("/chat", json=body).text)
        again = _parse_sse(client.post("/chat", json=body).text)
    streamed = "".join(d["text"] for k, d in events if k == "token")
    assert streamed == "from the main model"[: len("from the main model") // 2]
    assert events[-1][0] == "done"
    final = [d for k, d in events if k == "meta"][-1]
    assert final["continuation"]["stop_reason"] == "error" and final["continuation"]["truncated"] is True
    assert len(engine.stream_calls) == 1, "no second open after the first token"
    assert _counter("llm_retry_total", what="stream", reason="connection") == 0
    assert breaker.get(breaker.MAIN).state == breaker.CLOSED, "a death mid-body is not a breaker failure of the open"
    rows = _assistant_rows("cut-1")
    assert len(rows) == 1 and rows[0]["content"] == streamed
    assert rows[0]["meta"]["continuation"]["stop_reason"] == "error"
    row = db.get_chat_request("int-cut-1")
    assert row["status"] == "completed" and row["attempt"] == 1
    assert _ledger("int-cut-1") == [
        {
            "generation_id": rows[0]["generation_id"],
            "status": "ok",
            "error_kind": "",
            "attempt": 1,
            "engine": "primary",
            "retry_reason": "none",
            "terminal_state": "interrupted",
        }
    ]
    assert _counter("chat_request_attempts_total", engine="main", terminal_state="interrupted") == 1
    # Asked again: the partial is replayed, the model is not run again.
    assert [k for k, _ in again] == ["meta", "token", "meta", "done"]
    assert again[1][1] == {"text": streamed}
    assert len(engine.stream_calls) == 1
    assert len(_assistant_rows("cut-1")) == 1


def test_after_the_first_token_an_engine_error_marks_the_attempt_interrupted_and_keeps_the_partial(monkeypatch):
    """An engine that raises AFTER it has streamed text — a recoverable,
    connection-class error, the kind a retry would have answered had
    it come before the first token. It is not retried: the wire ends with
    one `error`, the partial is persisted as the failure record (so a
    reload shows it and a Retry the PERSON may press), the row is `failed`
    (not `interrupted`: `interrupted` is what /chat/attach resumes by
    itself, and nothing may re-run this by itself), and the ledger says
    the attempt was interrupted, by which error."""
    from app.engines import chat as chat_engine

    calls: list = []

    async def dies_after_a_token(message, history, emit, **kwargs):
        calls.append(1)
        await emit("token", {"text": "Half an "})
        raise _conn_error()

    monkeypatch.setattr(chat_engine, "run_chat_engine", dies_after_a_token)
    body = _body("cut-2", "int-cut-2")
    with TestClient(app) as client:
        events = _parse_sse(client.post("/chat", json=body).text)
        kinds = [k for k, _ in events]
        assert kinds[0] == "meta" and kinds[-1] == "error" and kinds.count("error") == 1
        assert events[-1][1] == {"message": _UNAVAILABLE, "code": "MODEL_UNAVAILABLE"}
        assert calls == [1], "not re-run: the first token had already reached the viewer"
        status = client.get("/chat/requests/int-cut-2").json()
        assert status["status"] == "failed" and status["answer_persisted"] is False and status["live"] is False
        # No automatic resume: the row is not `interrupted`, so attach has
        # nothing to run and says so.
        assert client.get("/chat/attach/cut-2").status_code == 404
        assert calls == [1]
    row = db.get_chat_request("int-cut-2")
    assert row["status"] == "failed" and row["error"] == _UNAVAILABLE
    rows = _assistant_rows("cut-2")
    assert len(rows) == 1 and rows[0]["content"] == "Half an "
    assert rows[0]["meta"]["error"]["code"] == "MODEL_UNAVAILABLE"
    assert rows[0]["meta"]["error"]["resumable"] is True
    assert _ledger("int-cut-2") == [
        {
            "generation_id": row["generation_id"],
            "status": "error",
            "error_kind": "MODEL_UNAVAILABLE",
            "attempt": 1,
            "engine": "primary",
            "retry_reason": "none",
            "terminal_state": "interrupted",
        }
    ]
    assert _counter("chat_request_attempts_total", engine="main", terminal_state="interrupted") == 1


def test_a_failure_before_any_token_is_failed_and_a_persons_retry_is_attempt_two(monkeypatch):
    """The contrast: an engine that dies before a single token is a FAILED
    attempt (nothing to keep). The person's Retry is a new attempt of the
    same intent with `retry_reason: failed`; the failure record is
    superseded, and the thread still holds exactly one assistant row."""
    from app.engines import chat as chat_engine

    real = chat_engine.run_chat_engine
    calls: list = []

    async def boom(*args, **kwargs):
        calls.append(1)
        raise RuntimeError("engine exploded before any token")

    monkeypatch.setattr(chat_engine, "run_chat_engine", boom)
    body = _body("fail-1", "int-fail-1")
    with TestClient(app) as client:
        events = _parse_sse(client.post("/chat", json=body).text)
        assert events[-1][0] == "error" and events[-1][1]["code"] == "APPLICATION_ERROR"
        first_generation = db.get_chat_request("int-fail-1")["generation_id"]
        monkeypatch.setattr(chat_engine, "run_chat_engine", real)
        monkeypatch.setattr(llm, "stream_chat_events", _fake_stream([("token", "Second time lucky")], calls))
        events = _parse_sse(client.post("/chat", json=body).text)
    assert events[0][1]["attempt"] == 2 and events[-1][0] == "done"
    row = db.get_chat_request("int-fail-1")
    assert row["status"] == "completed" and row["attempt"] == 2
    rows = _assistant_rows("fail-1")
    assert len(rows) == 1 and rows[0]["content"] == "Second time lucky"
    assert rows[0]["generation_id"] == row["generation_id"] != first_generation
    assert _ledger("int-fail-1") == [
        {
            "generation_id": first_generation,
            "status": "error",
            "error_kind": "APPLICATION_ERROR",
            "attempt": 1,
            "engine": "primary",
            "retry_reason": "none",
            "terminal_state": "failed",
        },
        {
            "generation_id": row["generation_id"],
            "status": "ok",
            "error_kind": "",
            "attempt": 2,
            "engine": "primary",
            "retry_reason": "failed",
            "terminal_state": "completed",
        },
    ]
    assert _counter("chat_request_attempts_total", engine="main", terminal_state="failed") == 1
    assert _counter("chat_request_attempts_total", engine="main", terminal_state="completed") == 1


# ---------------------------------------------------------------------------
# 4. The ledger's terminal states, exhaustively
# ---------------------------------------------------------------------------


def test_attempt_record_terminal_states(monkeypatch):
    def record(**state):
        streamed = state.pop("streamed", False)
        gen = app_main.LiveGeneration("c", 1)
        for key, value in state.items():
            setattr(gen, key, value)
        return app_main._attempt_record(gen, streamed=streamed)

    async def run():
        base = {"attempt": 1, "engine": "primary", "retry_reason": "none"}
        assert record() == {**base, "terminal_state": "completed"}
        assert record(final_meta={"continuation": {"stop_reason": "error", "truncated": True}}) == {**base, "terminal_state": "interrupted"}
        assert record(final_meta={"continuation": {"stop_reason": "budget", "truncated": True}}) == {**base, "terminal_state": "completed"}
        assert record(failed=True) == {**base, "terminal_state": "failed"}
        assert record(failed=True, streamed=True) == {**base, "terminal_state": "interrupted"}
        assert record(cancelled=True) == {**base, "terminal_state": "cancelled"}
        assert record(attempt=3, retry_reason="lost_process") == {**base, "attempt": 3, "retry_reason": "lost_process", "terminal_state": "completed"}
        monkeypatch.setattr(app_main, "_shutting_down", True)
        assert record(cancelled=True) == {**base, "terminal_state": "interrupted"}
        # An acknowledged Stop stays a Stop, even during shutdown (as the row does).
        assert record(cancelled=True, request_status="cancelled") == {**base, "terminal_state": "cancelled"}
        monkeypatch.setattr(app_main, "_shutting_down", False)
        # `engine` is the answer's own stamp (§8.3), "primary" when there is none.
        assert record(final_meta={"route": "chat"})["engine"] == "primary"
        assert record(final_meta={"route": "chat", "engine": "fallback"})["engine"] == "fallback"

    asyncio.run(run())


def test_stop_is_recorded_as_a_cancelled_attempt(monkeypatch):
    calls: list = []

    async def scenario():
        gate = asyncio.Event()
        monkeypatch.setattr(llm, "stream_chat_events", _gated_stream(gate, [("token", "Hel")], calls))
        async with _async_client() as client:
            first = asyncio.create_task(client.post("/chat", json=_body("stop-1", "int-stop-1")))
            await _wait_until(lambda: bool(calls))
            assert (await client.post("/chat/stop", json={"conversation_id": "stop-1"})).json() == {"stopped": True}
            await first
            await _wait_until(lambda: bool(_ledger("int-stop-1")))

    asyncio.run(scenario())
    assert db.get_chat_request("int-stop-1")["status"] == "cancelled"
    assert _assistant_rows("stop-1") == []
    assert [(r["status"], r["attempt"], r["terminal_state"], r["retry_reason"]) for r in _ledger("int-stop-1")] == [
        ("cancelled", 1, "cancelled", "none")
    ]
    assert _counter("chat_request_attempts_total", engine="main", terminal_state="cancelled") == 1


# ---------------------------------------------------------------------------
# 5. Disconnect mid-stream, reconnect, one final message
# ---------------------------------------------------------------------------


def test_a_client_that_disconnects_mid_stream_reconnects_to_exactly_one_final_message(monkeypatch):
    calls: list = []

    async def scenario():
        gate = asyncio.Event()
        # A whitespace-terminated token: the continuation loop holds back a
        # trailing partial word, so this is what reaches the wire at once.
        monkeypatch.setattr(
            llm, "stream_chat_events", _gated_stream(gate, [("token", "Hello ")], calls, [("token", "world!")])
        )
        async with _async_client() as client:
            first = asyncio.create_task(client.post("/chat", json=_body("dc-1", "int-dc-1")))
            await _wait_until(lambda: bool(calls))
            gen = _live_generations["dc-1"]
            await _wait_until(lambda: gen.subscribers == 1 and len(gen.events) >= 2)
            # The tab loses its connection: the response body is abandoned.
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            await _wait_until(lambda: gen.subscribers == 0)
            assert not gen.done and not gen.cancelled, "the generation is detached from the request"
            # What a reloaded tab asks first (frontend/lib/streams.ts
            # reconnectInterrupted): the send is known, live, not yet persisted.
            report = (await client.get("/chat/requests/int-dc-1")).json()
            assert report["status"] == "running" and report["live"] is True
            assert report["answer_persisted"] is False and report["generation_id"] == gen.generation_id
            # Then it re-joins: the buffer is replayed, then the rest streams.
            attach = asyncio.create_task(client.get("/chat/attach/dc-1"))
            await _wait_until(lambda: gen.subscribers == 1)
            gate.set()
            resp = await attach
            # Nothing left live; the row and the thread agree.
            report = (await client.get("/chat/requests/int-dc-1")).json()
            assert report["status"] == "completed" and report["live"] is False
            assert report["answer_persisted"] is True
            assert (await client.get("/chat/attach/dc-1")).status_code == 404
            # The same send from the reloaded tab: the answer, once more, from history.
            replay = await client.post("/chat", json=_body("dc-1", "int-dc-1"))
        return gen, resp, replay

    gen, resp, replay = asyncio.run(scenario())
    events = _parse_sse(resp.text)
    kinds = [k for k, _ in events]
    assert kinds[0] == "meta" and kinds[-1] == "done" and kinds.count("done") == 1
    assert events[0][1]["generation_id"] == gen.generation_id
    assert "".join(d["text"] for k, d in events if k == "token") == "Hello world!"
    finals = [d for k, d in events if k == "meta" and d.get("route")]
    assert len(finals) == 1 and finals[0]["generation_id"] == gen.generation_id
    assert calls == [1]
    rows = _assistant_rows("dc-1")
    assert len(rows) == 1 and rows[0]["content"] == "Hello world!" and rows[0]["generation_id"] == gen.generation_id
    assert [k for k, _ in _parse_sse(replay.text)] == ["meta", "token", "meta", "done"]
    assert len(_assistant_rows("dc-1")) == 1
    assert [(r["attempt"], r["terminal_state"]) for r in _ledger("int-dc-1")] == [(1, "completed")]


# ---------------------------------------------------------------------------
# 6. Restart: rows marked interrupted; a later resume never duplicates
# ---------------------------------------------------------------------------


def test_a_restart_with_the_answer_already_persisted_replays_instead_of_answering_again(monkeypatch, as_user):
    """The server persists the answer BEFORE it marks the row completed. A
    process that dies in that window leaves the row `running`; the next
    startup marks it `interrupted` — and a resume of it would run the
    question again beside the answer already in history. The durable answer
    wins: it is replayed, the row is healed, nothing runs."""
    calls: list = []
    monkeypatch.setattr(llm, "stream_chat_events", _fake_stream([("token", "never")], calls))
    uid = int(as_user("alice")["id"])
    db.create_conversation(uid, "dead-1", "Dead")
    db.create_chat_request(
        "int-dead-1", uid, "dead-1", "gen-dead-1",
        {"message": "what happened?", "mode": "assistant", "conversation_id": "dead-1"},
    )
    db.set_chat_request_status("int-dead-1", "running")
    db.add_message(uid, "dead-1", "user", "what happened?", {"intent": {"id": "int-dead-1"}})
    db.add_message(
        uid, "dead-1", "assistant", "The whole answer.",
        {"route": "chat", "generation_id": "gen-dead-1", "intent_id": "int-dead-1", "attempt": 1},
    )
    with TestClient(app) as client:
        # Startup marked the row `interrupted`; the start-up sweep (round 2:
        # it lists interrupted rows too) finds the durable answer and HEALS
        # the row to `completed` — the answer wins, nothing runs.
        import time as _time

        deadline = _time.monotonic() + 8.0
        while _time.monotonic() < deadline and db.get_chat_request("int-dead-1")["status"] != "completed":
            _time.sleep(0.05)
        row = db.get_chat_request("int-dead-1")
        assert row["status"] == "completed" and row["attempt"] == 1 and row["generation_id"] == "gen-dead-1"
        report = client.get("/chat/requests/int-dead-1").json()
        assert report["status"] == "completed" and report["answer_persisted"] is True
        assert calls == [], "the answer exists: the model is not run again"
        # The browser's re-attach after the restart: finished — load history.
        assert client.get("/chat/attach/dead-1").status_code == 404
        # A repeat of the send replays the durable answer.
        again = _parse_sse(client.post("/chat", json=_body("dead-1", "int-dead-1", message="what happened?")).text)
        assert [k for k, _ in again] == ["meta", "token", "meta", "done"]
        assert again[0][1] == {"generation_id": "gen-dead-1", "intent_id": "int-dead-1", "attempt": 1}
        assert again[1][1] == {"text": "The whole answer."} and calls == []
    assert [m["role"] for m in db.list_messages("dead-1")] == ["user", "assistant"]
    assert len(_assistant_rows("dead-1")) == 1
    assert _counter("chat_request_total", result="replayed") == 1
    assert _counter("chat_request_total", result="resumed") == 0
    assert _counter("llm_resumed_generations_total", outcome="duplicate_suppressed") == 1


def test_a_restart_mid_stream_resumes_once_under_attempt_two(monkeypatch):
    """Process one accepts the send and streams a token to a tab that then
    loses it; the process's orderly shutdown (a deploy's recreate) runs the
    real lifespan exit — the row is marked `interrupted`, the loop teardown
    cancels the worker, which records the attempt as interrupted (not
    cancelled: nobody pressed Stop) and persists nothing. Process two's
    start-up sweep finds the row and — CONTRACT §8.4, round 2: the attempt
    had streamed a token, so nothing re-runs it by itself — settles it
    `failed` for a Retry; the browser's re-attach ends like a finished one
    (404) and the PERSON's retry answers ONCE more under attempt 2 with
    `retry_reason: failed` — one assistant row in the thread, two attempts
    in the ledger."""
    calls: list = []
    body = _body("boot-1", "int-boot-1")

    async def process_one():
        gate = asyncio.Event()  # never set: the process dies holding the stream
        monkeypatch.setattr(llm, "stream_chat_events", _gated_stream(gate, [("token", "Hello ")], calls))
        async with app.router.lifespan_context(app):
            async with _async_client() as client:
                request = asyncio.create_task(client.post("/chat", json=body))
                await _wait_until(lambda: bool(calls))
                gen = _live_generations["boot-1"]
                await _wait_until(lambda: len(gen.events) >= 2)
                request.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await request
                assert db.get_chat_request("int-boot-1")["status"] == "running"
        # The lifespan exit has run (the row is written; the flag is up);
        # what asyncio.run does next is cancel every task still alive and
        # let it finish — the worker's `finally` then settles and records.
        assert app_main._shutting_down is True
        gen.task.cancel()
        await gen.task  # the worker catches its own cancellation and records it
        return gen

    gen = asyncio.run(process_one())
    row = db.get_chat_request("int-boot-1")
    assert row["status"] == "interrupted" and row["attempt"] == 1
    assert row["generation_id"] == gen.generation_id
    assert gen.cancelled and not gen.failed
    assert _assistant_rows("boot-1") == [], "a lost process persists nothing on its way out"
    assert _ledger("int-boot-1") == [
        {
            "generation_id": gen.generation_id,
            "status": "cancelled",
            "error_kind": "",
            "attempt": 1,
            "engine": "primary",
            "retry_reason": "none",
            "terminal_state": "interrupted",
        }
    ]

    # Process two.
    _live_generations.clear()
    monkeypatch.setattr(app_main, "_shutting_down", False)
    monkeypatch.setattr(llm, "stream_chat_events", _fake_stream([("token", "Hello again")], calls))
    with TestClient(app) as client:
        # The start-up sweep settles the row: the ledger says a token had
        # streamed, so the attempt is not re-run by itself (§8.4).
        import time as _time

        deadline = _time.monotonic() + 8.0
        while _time.monotonic() < deadline and db.get_chat_request("int-boot-1")["status"] != "failed":
            _time.sleep(0.05)
        from app import continuity

        report = client.get("/chat/requests/int-boot-1").json()
        assert report == {
            "intent_id": "int-boot-1",
            "conversation_id": "boot-1",
            "status": "failed",
            "generation_id": gen.generation_id,
            "attempt": 1,
            "resumable": True,
            "answer_persisted": False,
            "live": False,
        }
        assert db.get_chat_request("int-boot-1")["error"] == continuity.INTERRUPTED_NOTHING_KEPT
        assert calls == [1], "nothing re-ran it"
        # The browser's automatic re-attach: finished — load history (the
        # failed turn with its Retry).
        assert client.get("/chat/attach/boot-1").status_code == 404
        # The person's Retry: attempt 2, once.
        events = _parse_sse(client.post("/chat", json=body).text)
        assert events[0][1]["intent_id"] == "int-boot-1" and events[0][1]["attempt"] == 2
        assert events[-1][0] == "done"
        assert "".join(d["text"] for k, d in events if k == "token") == "Hello again"
        assert client.get("/chat/attach/boot-1").status_code == 404
        # A late re-attach or a repeat of the send: the answer, not a third attempt.
        again = _parse_sse(client.post("/chat", json=body).text)
        assert again[0][1]["attempt"] == 2 and again[1][1] == {"text": "Hello again"}
    assert calls == [1, 1]
    row = db.get_chat_request("int-boot-1")
    assert row["status"] == "completed" and row["attempt"] == 2 and row["generation_id"] != gen.generation_id
    rows = _assistant_rows("boot-1")
    assert len(rows) == 1 and rows[0]["generation_id"] == row["generation_id"]
    assert [(r["attempt"], r["retry_reason"], r["terminal_state"]) for r in _ledger("int-boot-1")] == [
        (1, "none", "interrupted"),
        (2, "failed", "completed"),
    ]
    assert _counter("chat_request_resume_total") == 1


def test_startup_marks_only_open_rows_interrupted(as_user):
    uid = int(as_user("alice")["id"])
    for intent, gen, status in (
        ("i-acc", "g1", "accepted"),
        ("i-run", "g2", "running"),
        ("i-done", "g3", "completed"),
        ("i-fail", "g4", "failed"),
        ("i-stop", "g5", "cancelled"),
    ):
        db.create_chat_request(intent, uid, "conv-x", gen, {"message": "m"})
        if status != "accepted":
            db.set_chat_request_status(intent, status)
    assert asyncio.run(app_main._interrupt_open_requests()) == 2
    assert {i: db.get_chat_request(i)["status"] for i in ("i-acc", "i-run", "i-done", "i-fail", "i-stop")} == {
        "i-acc": "interrupted",
        "i-run": "interrupted",
        "i-done": "completed",
        "i-fail": "failed",
        "i-stop": "cancelled",
    }
    assert asyncio.run(app_main._interrupt_open_requests()) == 0


# ---------------------------------------------------------------------------
# SLO C: lost and duplicate counts are SQL, not counters
# ---------------------------------------------------------------------------

#: A DUPLICATE final answer: more than one non-failure assistant row for one
#: intent, across every attempt it ever had (usage_events holds one row per
#: attempt and names the intent; chat_requests only knows the latest
#: generation_id). Must return no rows.
DUPLICATE_ANSWERS_SQL = """
SELECT r.intent_id, count(DISTINCT m.id) AS answers
FROM chat_requests r
JOIN usage_events u ON u.meta->>'intent_id' = r.intent_id
JOIN messages m
  ON m.conversation_id = r.conversation_id
 AND m.generation_id = u.generation_id
 AND m.role = 'assistant'
 AND NOT (m.meta ? 'error')
GROUP BY r.intent_id
HAVING count(DISTINCT m.id) > 1
"""

#: A LOST request: an accepted send that ended with neither an answer nor a
#: failure record the person could see, and that no process is still
#: running — older than the reconnect loop's patience. Excludes a Stop (the
#: person ended it). Must return no rows after every recovery.
LOST_REQUESTS_SQL = """
SELECT r.intent_id, r.status, r.attempt, r.updated_at
FROM chat_requests r
LEFT JOIN messages m
  ON m.conversation_id = r.conversation_id
 AND m.generation_id = r.generation_id
 AND m.role = 'assistant'
WHERE m.id IS NULL
  AND r.status <> 'cancelled'
  AND r.updated_at < now() - (%s * interval '1 second')
"""


def _query(sql: str, *params):
    with db.connection() as con:
        return [dict(r) for r in con.execute(sql, params).fetchall()]


def test_slo_c_sql_counts_lost_and_duplicate_answers(as_user):
    from datetime import datetime, timedelta, timezone

    from app import usage

    uid = int(as_user("alice")["id"])
    db.create_conversation(uid, "slo-1", "S")
    old = datetime.now(timezone.utc) - timedelta(minutes=30)

    def intent(intent_id: str, generation_id: str, status: str) -> None:
        db.create_chat_request(intent_id, uid, "slo-1", generation_id, {"message": "m"})
        db.set_chat_request_status(intent_id, status)
        with db.connection() as con:
            con.execute("UPDATE chat_requests SET updated_at = %s WHERE intent_id = %s", (old, intent_id))

    def attempt(intent_id: str, generation_id: str, n: int, terminal: str) -> None:
        usage.record(
            user_id=uid, workspace_id="w", conversation_id="slo-1", generation_id=generation_id, route="chat",
            meta={"intent_id": intent_id, "attempt": n, "engine": "primary", "retry_reason": "none", "terminal_state": terminal},
        )

    # Healthy: completed with its answer; failed with its failure record;
    # cancelled with nothing; a resumed one whose first attempt left nothing.
    intent("ok", "g-ok", "completed")
    db.add_message(uid, "slo-1", "assistant", "answer", {"generation_id": "g-ok"})
    attempt("ok", "g-ok", 1, "completed")
    intent("failed", "g-failed", "failed")
    db.add_message(uid, "slo-1", "assistant", "", {"generation_id": "g-failed", "error": {"message": "x", "code": "TIMEOUT"}})
    attempt("failed", "g-failed", 1, "failed")
    intent("stopped", "g-stopped", "cancelled")
    attempt("stopped", "g-stopped", 1, "cancelled")
    intent("resumed", "g-resumed-2", "completed")
    attempt("resumed", "g-resumed-1", 1, "interrupted")
    attempt("resumed", "g-resumed-2", 2, "completed")
    db.add_message(uid, "slo-1", "assistant", "second attempt", {"generation_id": "g-resumed-2"})
    assert _query(DUPLICATE_ANSWERS_SQL) == []
    assert _query(LOST_REQUESTS_SQL, 900) == []

    # Lost: interrupted and never resumed; completed but the persist failed.
    intent("lost-int", "g-lost-int", "interrupted")
    intent("lost-done", "g-lost-done", "completed")
    lost = _query(LOST_REQUESTS_SQL, 900)
    assert sorted((r["intent_id"], r["status"]) for r in lost) == [("lost-done", "completed"), ("lost-int", "interrupted")]
    # A row younger than the patience window is not lost yet — it may still be resumed.
    assert _query(LOST_REQUESTS_SQL, 3600 * 24) == []

    # Duplicate: a resume that answered beside an answer that was already there.
    intent("dup", "g-dup-2", "completed")
    attempt("dup", "g-dup-1", 1, "interrupted")
    attempt("dup", "g-dup-2", 2, "completed")
    db.add_message(uid, "slo-1", "assistant", "first", {"generation_id": "g-dup-1"})
    db.add_message(uid, "slo-1", "assistant", "again", {"generation_id": "g-dup-2"})
    assert _query(DUPLICATE_ANSWERS_SQL) == [{"intent_id": "dup", "answers": 2}]
