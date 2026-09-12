"""app/continuity.py — a person's request while the main model cannot take it
(availability CONTRACT v2 §8.3–8.4).

The real chat route, the real llm.py / resilience.py choreography and the
test database; the engine is a fake OpenAI client at the main model's URL
and the controller's verdict is planted the way the poller would. What is
pinned:

1. an OPEN breaker (the controller says RECOVERING) queues the turn: the row
   says `queued`, the person reads the ONE exact sentence once, the engine
   is not touched; READY wakes the wait through the event and the SAME
   generation resumes — same generation_id, attempt 2, retry_reason
   `recovery` — with one assistant row and one ledger row;
2. a wait that outruns LLM_QUEUE_MAX_WAIT_S ends with the second exact
   sentence and a parked row (`queued`, never failed, never a 500); the
   resume sweep runs it once on READY;
3. a process that died holding a queued row: the next start-up's sweep
   resumes it exactly once (the V29 compare-and-swap is the lease); an
   `interrupted` row is left to the browser at start-up and taken by the
   READY sweep;
4. a row whose answer already exists is healed and counted
   `duplicate_suppressed`, never run again;
5. Stop while queued: cancelled, the queue gauge back to zero, the row a
   Stop, no resume.
"""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import openai
import pytest
from fastapi.testclient import TestClient

from app import admission, breaker, continuity, db, engine_state, llm, metrics, resilience
from app import main as app_main
from app.config import settings
from app.main import _live_generations, app

MAIN_URL = "http://vllm-main.test:8000/v1"
MAIN_MODEL = "Qwen/Qwen3.6-35B-A3B-NVFP4"


# ---------------------------------------------------------------------------
# Harness (tests/test_generation_durability.py's idioms)
# ---------------------------------------------------------------------------


def _parse_sse(text: str):
    events = []
    for block in text.strip().split("\n\n"):
        lines = block.strip().split("\n")
        if len(lines) >= 2 and lines[0].startswith("event: "):
            events.append((lines[0][7:], json.loads(lines[1][6:])))
    return events


def _async_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _wait_until(predicate, timeout: float = 8.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


def _assistant_rows(conversation_id: str) -> list:
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
    with db.connection() as con:
        rows = con.execute(
            "SELECT generation_id, status, meta FROM usage_events "
            "WHERE meta->>'intent_id' = %s ORDER BY id",
            (intent_id,),
        ).fetchall()
    out = []
    for row in rows:
        meta = row["meta"]
        if isinstance(meta, str):
            meta = json.loads(meta)
        out.append((meta.get("attempt"), meta.get("retry_reason"), meta.get("terminal_state"), row["status"]))
    return out


def _counter(name: str, **labels) -> float:
    key = tuple(sorted(labels.items()))
    return metrics._counters.get(name, {}).get(key, 0.0)


def _status(intent_id: str) -> str:
    """The row's status, or "" before the route has written it."""
    row = db.get_chat_request(intent_id)
    return str(row["status"]) if row else ""


def _gauge(name: str) -> float:
    return metrics._gauges.get(name, {}).get((), 0.0)


def _doc(state: str, **extra) -> dict:
    base = {
        "schema": 1,
        "generated_at": time.time(),
        "state": state,
        "state_code": engine_state.STATE_CODES[state],
        "reason": "test",
        "primary_ready": state in engine_state.SERVING,
        "incident": None,
        "recovery": {"in_progress": False, "step": "idle"},
        "signals": {"engine": {"requests_running": 0, "requests_waiting": 0}},
    }
    base.update(extra)
    return base


def _verdict(state: str, **extra) -> None:
    """Plant a controller verdict exactly as the poller records one — the
    gauge, the READY event and the edge listeners included."""
    engine_state._record(
        engine_state.parse_state_document(_doc(state, **extra), observed_at=time.monotonic()), ""
    )


@pytest.fixture(autouse=True)
def _clean_process_state(monkeypatch):
    _live_generations.clear()
    metrics.reset()
    breaker.reset()
    engine_state.reset()
    continuity.reset()
    admission.reset()
    monkeypatch.setattr(app_main, "_shutting_down", False)
    # The poller must not overwrite a planted verdict with "unreachable".
    monkeypatch.setattr(settings, "engine_controller_url", "")
    yield
    _live_generations.clear()
    breaker.reset()
    engine_state.reset()
    continuity.reset()
    admission.reset()
    metrics.reset()


def _body(conversation_id: str, intent_id: str, message: str = "hi", **extra) -> dict:
    return {"message": message, "mode": "assistant", "conversation_id": conversation_id,
            "intent_id": intent_id, "effort": "fast", **extra}


_REQ = httpx.Request("POST", f"{MAIN_URL}/chat/completions")


def _conn_error() -> openai.APIConnectionError:
    exc = openai.APIConnectionError(request=_REQ)
    exc.__cause__ = httpx.ConnectError("connection refused")
    return exc


class _Stream:
    def __init__(self, text: str) -> None:
        half = len(text) // 2
        self._chunks = [
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text[:half]), finish_reason=None)], usage=None),
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text[half:]), finish_reason="stop")], usage=None),
            SimpleNamespace(choices=[], usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2)),
        ]
        self.closed = False

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for chunk in self._chunks:
            yield chunk

    async def close(self) -> None:
        self.closed = True


class _Engine:
    """The main model. Counts every OPEN of a stream and every other call."""

    def __init__(self, base_url: str, text: str) -> None:
        self.base_url = base_url
        self.text = text
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
        return _Stream(self.text)


class _Dead:
    def __init__(self) -> None:
        async def refuse(**kwargs):
            raise _conn_error()

        self.chat = SimpleNamespace(completions=SimpleNamespace(create=refuse))
        self.embeddings = SimpleNamespace(create=refuse)


@pytest.fixture()
def engine(monkeypatch):
    """The primary at the main model's URL, dead sidecars, no cooldown, no
    backoff, a passthrough fit_request, and a queue window of ten seconds
    (tests that expire it shorten it)."""
    monkeypatch.setattr(settings, "openai_base_url", MAIN_URL)
    monkeypatch.setattr(settings, "llm_model", MAIN_MODEL)
    monkeypatch.setattr(settings, "llm_interactive_recovery_s", 0.0)
    monkeypatch.setattr(settings, "llm_queue_max_wait_s", 10.0)
    monkeypatch.setattr(settings, "llm_breaker_failures", 3)
    monkeypatch.setattr(settings, "llm_breaker_window_s", 30.0)
    monkeypatch.setattr(settings, "llm_breaker_cooldown_s", 0.0)
    monkeypatch.setattr(resilience, "_backoff_s", lambda attempt: 0.0)

    async def never(base_url, timeout=None):  # pragma: no cover — the verdict is fresh in every test
        raise AssertionError("/health must not be polled while the controller has a verdict")

    monkeypatch.setattr(resilience, "engine_answers", never)
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
# 1. Queue → one line → READY → the same generation resumes once
# ---------------------------------------------------------------------------


def test_a_recovering_engine_queues_the_turn_and_ready_resumes_the_same_generation(engine):
    async def scenario():
        _verdict("RECOVERING")
        assert breaker.get(breaker.MAIN).state == breaker.OPEN
        async with _async_client() as client:
            request = asyncio.create_task(client.post("/chat", json=_body("q-1", "int-q-1")))
            await _wait_until(lambda: "q-1" in _live_generations)
            gen = _live_generations["q-1"]
            first_generation = gen.generation_id
            # The row is parked, the person is told, the engine untouched.
            await _wait_until(lambda: _status("int-q-1") == "queued")
            await _wait_until(lambda: any(e == "status" for e, _ in gen.events))
            assert engine.stream_calls == [] and engine.other_calls == []
            assert _gauge("llm_queued_generations") == 1.0
            assert gen.request_status == "queued"
            status = (await client.get("/chat/requests/int-q-1")).json()
            assert status["status"] == "queued" and status["live"] is True
            # A while of RECOVERING: nothing moves, nothing is polled.
            await asyncio.sleep(1.5)
            assert engine.stream_calls == [] and not gen.done
            # READY: the event wakes the wait, the breaker's canary is this
            # very stream, and the generation goes on as itself.
            _verdict("READY")
            resp = await request
        return gen, first_generation, resp

    gen, first_generation, resp = asyncio.run(scenario())
    events = _parse_sse(resp.text)
    kinds = [k for k, _ in events]
    assert kinds[0] == "meta" and kinds[-1] == "done" and kinds.count("error") == 0
    said = [d["text"] for k, d in events if k == "status"]
    assert said == [continuity.QUEUED_LINE], "exactly one line, the exact one"
    assert "".join(d["text"] for k, d in events if k == "token") == "from the main model"
    metas = [d for k, d in events if k == "meta"]
    assert {k: v for k, v in metas[0].items() if k not in ('request_id', 'trace_id')} == {"generation_id": first_generation, "intent_id": "int-q-1", "attempt": 1}
    assert metas[-1]["generation_id"] == first_generation and metas[-1]["attempt"] == 2
    assert len(engine.stream_calls) == 1, "one open, once READY"
    assert gen.generation_id == first_generation and gen.attempt == 2 and gen.retry_reason == "recovery"
    row = db.get_chat_request("int-q-1")
    assert row["status"] == "completed" and row["attempt"] == 2 and row["generation_id"] == first_generation
    rows = _assistant_rows("q-1")
    assert len(rows) == 1 and rows[0]["generation_id"] == first_generation
    assert rows[0]["meta"].get("engine") in (None, "primary") and rows[0]["meta"]["model"] == MAIN_MODEL
    assert _ledger("int-q-1") == [(2, "recovery", "completed", "ok")]
    assert _counter("llm_resumed_generations_total", outcome="resumed") == 1
    assert _counter("llm_resumed_generations_total", outcome="expired") == 0
    assert _counter("chat_request_total", result="resumed") == 0, "the same generation, not a new one"
    assert _gauge("llm_queued_generations") == 0.0
    assert metrics._hists["llm_queue_wait_seconds"][()][2] == 1
    assert breaker.get(breaker.MAIN).state == breaker.CLOSED


def test_a_queued_turn_never_polls_the_dead_port(engine, monkeypatch):
    """CONTRACT §8.3 step 3: while held, the wait sleeps on the READY event.
    The engine sees nothing until READY — and /health is never asked."""
    polls = {"n": 0}

    async def probe(base_url, timeout=None):
        polls["n"] += 1
        return True

    monkeypatch.setattr(resilience, "engine_answers", probe)

    async def scenario():
        _verdict("WEDGED")
        async with _async_client() as client:
            request = asyncio.create_task(client.post("/chat", json=_body("q-2", "int-q-2")))
            await _wait_until(lambda: _status("int-q-2") == "queued")
            await asyncio.sleep(2.2)
            assert engine.stream_calls == [] and polls["n"] == 0
            _verdict("READY")
            return await request

    resp = asyncio.run(scenario())
    assert _parse_sse(resp.text)[-1][0] == "done"
    assert polls["n"] == 0 and len(engine.stream_calls) == 1


# ---------------------------------------------------------------------------
# 2. Expiry: the second sentence, a parked row, the sweep resumes it once
# ---------------------------------------------------------------------------


def test_an_expired_wait_parks_the_row_with_the_second_sentence_and_the_sweep_resumes_it(engine, monkeypatch):
    monkeypatch.setattr(settings, "llm_queue_max_wait_s", 0.6)

    async def scenario():
        _verdict("RECOVERING")
        async with _async_client() as client:
            resp = await client.post("/chat", json=_body("exp-1", "int-exp-1"))
            events = _parse_sse(resp.text)
            gen_id = events[0][1]["generation_id"]
            # Never a 500, never a failed row: the row is parked.
            assert resp.status_code == 200
            row = db.get_chat_request("int-exp-1")
            assert row["status"] == "queued" and row["attempt"] == 1 and row["generation_id"] == gen_id
            report = (await client.get("/chat/requests/int-exp-1")).json()
            assert report["status"] == "queued" and report["live"] is False and report["answer_persisted"] is False
            assert engine.stream_calls == []
            # READY: the sweep the controller's edge triggers resumes it — once.
            _verdict("READY")
            counts = await continuity.resume_sweep("ready")
            assert counts["resumed"] == 1
            await _wait_until(lambda: _status("int-exp-1") == "completed")
            await _wait_until(lambda: "exp-1" not in _live_generations)
            again = await continuity.resume_sweep("ready")
            assert again == {"resumed": 0, "duplicate_suppressed": 0, "skipped": 0}
        return events, gen_id

    events, first_generation = asyncio.run(scenario())
    said = [d["text"] for k, d in events if k == "status"]
    assert said == [continuity.QUEUED_LINE, continuity.EXPIRED_LINE]
    assert events[-1] == ("error", {"message": continuity.EXPIRED_LINE, "code": continuity.PARKED_CODE, "resumable": True})
    assert _counter("llm_resumed_generations_total", outcome="expired") == 1
    assert _counter("llm_resumed_generations_total", outcome="resumed") == 1
    assert _counter("llm_engine_unavailable_total", what="stream", reason="breaker_open") == 1
    row = db.get_chat_request("int-exp-1")
    assert row["status"] == "completed" and row["attempt"] == 2 and row["generation_id"] != first_generation
    rows = _assistant_rows("exp-1")
    assert len(rows) == 1 and rows[0]["content"] == "from the main model"
    assert rows[0]["generation_id"] == row["generation_id"]
    assert _ledger("int-exp-1") == [
        (1, "none", "interrupted", "ok"),
        (2, "recovery", "completed", "ok"),
    ]
    assert len(engine.stream_calls) == 1
    assert _gauge("llm_queued_generations") == 0.0


# ---------------------------------------------------------------------------
# 3. A restart mid-queue: the start-up sweep, exactly once
# ---------------------------------------------------------------------------


def test_a_row_a_dead_process_left_queued_is_resumed_once_at_startup(engine, as_user):
    uid = int(as_user("alice")["id"])
    db.create_conversation(uid, "boot-q", "Boot")
    db.create_chat_request(
        "int-boot-q", uid, "boot-q", "gen-boot-q",
        {"message": "what happened?", "mode": "assistant", "conversation_id": "boot-q", "effort": "fast"},
    )
    assert db.park_chat_request("int-boot-q", "gen-boot-q")["status"] == "queued"
    # An interrupted row beside it: the browser's, at start-up.
    db.create_chat_request(
        "int-boot-i", uid, "boot-q", "gen-boot-i",
        {"message": "and this?", "mode": "assistant", "conversation_id": "boot-q", "effort": "fast"},
    )
    db.set_chat_request_status("int-boot-i", "interrupted")
    _verdict("READY")
    with TestClient(app) as client:
        assert continuity.describe()["sweep_installed"] is True
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline and db.get_chat_request("int-boot-q")["status"] != "completed":
            time.sleep(0.05)
        # The detached worker finishes its bookkeeping after the row says
        # completed; the lifespan exit must not cut that short.
        while time.monotonic() < deadline and "boot-q" in _live_generations:
            time.sleep(0.05)
        row = db.get_chat_request("int-boot-q")
        assert row["status"] == "completed" and row["attempt"] == 2 and row["generation_id"] != "gen-boot-q"
        assert db.get_chat_request("int-boot-i")["status"] == "interrupted", "left to the browser at start-up"
        assert client.get("/chat/requests/int-boot-q").json()["answer_persisted"] is True
    rows = _assistant_rows("boot-q")
    assert len(rows) == 1 and rows[0]["generation_id"] == row["generation_id"]
    assert _ledger("int-boot-q") == [(2, "recovery", "completed", "ok")]
    assert _counter("llm_resumed_generations_total", outcome="resumed") == 1
    assert len(engine.stream_calls) == 1


def test_the_ready_edge_sweeps_interrupted_rows_and_never_a_row_that_is_live(engine, as_user):
    uid = int(as_user("alice")["id"])
    db.create_conversation(uid, "edge-1", "Edge")
    db.create_chat_request(
        "int-edge-1", uid, "edge-1", "gen-edge-1",
        {"message": "resume me", "mode": "assistant", "conversation_id": "edge-1", "effort": "fast"},
    )
    db.set_chat_request_status("int-edge-1", "interrupted")

    async def scenario():
        continuity.start()  # registers the edge listener, as the lifespan does
        _verdict("RECOVERING")
        assert continuity._sweep_task is not None
        await continuity._sweep_task  # the start-up sweep: queued rows only
        assert db.get_chat_request("int-edge-1")["status"] == "interrupted"
        _verdict("READY")  # the edge
        await _wait_until(lambda: continuity._sweep_task is not None and continuity._sweep_task.done())
        await _wait_until(lambda: _status("int-edge-1") == "completed")
        await _wait_until(lambda: "edge-1" not in _live_generations)  # the worker's bookkeeping is done
        # The live-generation check is what protects a row this process is
        # running: plant one and sweep.
        gen = app_main.LiveGeneration("edge-1", uid)
        gen.intent_id = "int-edge-2"
        db.create_chat_request("int-edge-2", uid, "edge-1", gen.generation_id,
                               {"message": "live", "mode": "assistant", "conversation_id": "edge-1"})
        db.park_chat_request("int-edge-2", gen.generation_id)
        _live_generations["edge-1"] = gen
        counts = await continuity.resume_sweep("ready")
        assert counts == {"resumed": 0, "duplicate_suppressed": 0, "skipped": 1}
        assert db.get_chat_request("int-edge-2")["status"] == "queued"
        await continuity.stop()

    asyncio.run(scenario())
    assert _ledger("int-edge-1") == [(2, "lost_process", "completed", "ok")]
    assert len(_assistant_rows("edge-1")) == 1
    assert _counter("llm_resumed_generations_total", outcome="resumed") == 1


# ---------------------------------------------------------------------------
# 4. A durable answer is never answered again
# ---------------------------------------------------------------------------


def test_a_queued_row_whose_answer_exists_is_healed_not_run(engine, as_user):
    uid = int(as_user("alice")["id"])
    db.create_conversation(uid, "dup-q", "Dup")
    db.create_chat_request(
        "int-dup-q", uid, "dup-q", "gen-dup-q",
        {"message": "again?", "mode": "assistant", "conversation_id": "dup-q"},
    )
    db.park_chat_request("int-dup-q", "gen-dup-q")
    db.add_message(uid, "dup-q", "assistant", "The whole answer.",
                   {"route": "chat", "generation_id": "gen-dup-q", "intent_id": "int-dup-q", "attempt": 1})

    async def scenario():
        _verdict("READY")
        return await continuity.resume_sweep("ready")

    assert asyncio.run(scenario()) == {"resumed": 0, "duplicate_suppressed": 1, "skipped": 0}
    row = db.get_chat_request("int-dup-q")
    assert row["status"] == "completed" and row["generation_id"] == "gen-dup-q" and row["attempt"] == 1
    assert engine.stream_calls == [] and len(_assistant_rows("dup-q")) == 1
    assert _counter("llm_resumed_generations_total", outcome="duplicate_suppressed") == 1


# ---------------------------------------------------------------------------
# 5. Stop while queued
# ---------------------------------------------------------------------------


def test_stop_while_queued_cancels_and_leaves_no_resume(engine):
    async def scenario():
        _verdict("RECOVERING")
        async with _async_client() as client:
            request = asyncio.create_task(client.post("/chat", json=_body("stop-q", "int-stop-q")))
            await _wait_until(lambda: _status("int-stop-q") == "queued")
            assert _gauge("llm_queued_generations") == 1.0
            assert (await client.post("/chat/stop", json={"conversation_id": "stop-q"})).json() == {"stopped": True}
            resp = await request
            await _wait_until(lambda: bool(_ledger("int-stop-q")))
            # READY afterwards: nothing to resume — a Stop stays a Stop.
            _verdict("READY")
            assert await continuity.resume_sweep("ready") == {"resumed": 0, "duplicate_suppressed": 0, "skipped": 0}
        return resp

    resp = asyncio.run(scenario())
    assert _parse_sse(resp.text)[-1][0] != "done"
    assert db.get_chat_request("int-stop-q")["status"] == "cancelled"
    assert _gauge("llm_queued_generations") == 0.0
    assert _ledger("int-stop-q") == [(1, "none", "cancelled", "cancelled")]
    assert engine.stream_calls == [] and _assistant_rows("stop-q") == []
    assert _counter("llm_resumed_generations_total", outcome="resumed") == 0


# ---------------------------------------------------------------------------
# The hold itself, and the store helpers, without a route
# ---------------------------------------------------------------------------


def test_the_hold_speaks_once_per_wait_and_the_gauge_follows(monkeypatch):
    said: list = []

    async def notify(line: str) -> None:
        said.append(line)

    async def run():
        gen = SimpleNamespace(intent_id=None, generation_id="g", attempt=1, retry_reason="none",
                              request_status="running", parked=False)
        hold = continuity.bind(gen, notify)
        assert continuity.current() is hold and continuity.held()
        await hold.enter(continuity.RECOVERY)
        await hold.enter(continuity.RECOVERY)  # every tick of the gate: idempotent
        assert said == [continuity.QUEUED_LINE] and _gauge("llm_queued_generations") == 1.0
        await hold.resume()
        assert gen.attempt == 2 and gen.retry_reason == "recovery" and _gauge("llm_queued_generations") == 0.0
        # A second recovery wait in the same turn: said again (a new outage
        # the person should know about), but the attempt moves once.
        await hold.enter(continuity.RECOVERY)
        await hold.resume()
        assert gen.attempt == 2 and said == [continuity.QUEUED_LINE, continuity.QUEUED_LINE]
        # An admission wait carries its own line and is never a new attempt.
        await hold.enter(continuity.ADMISSION, admission.NORMAL_LINE.format(n=3))
        await hold.resume()
        assert gen.attempt == 2 and said[-1] == "Waiting for the model to finish current work (3 ahead)."
        # Abandoned (a Stop): the gauge, nothing else.
        await hold.enter(continuity.RECOVERY)
        assert _gauge("llm_queued_generations") == 1.0
        hold.abandon()
        assert _gauge("llm_queued_generations") == 0.0 and not hold.waiting
        # Expiry: the second sentence, the parked mark, the exception.
        await hold.enter(continuity.RECOVERY)
        with pytest.raises(continuity.QueuedForRecovery):
            await hold.expire()
        assert gen.parked is True and said[-1] == continuity.EXPIRED_LINE
        assert _gauge("llm_queued_generations") == 0.0
        assert _counter("llm_resumed_generations_total", outcome="resumed") == 1
        assert _counter("llm_resumed_generations_total", outcome="expired") == 1

    asyncio.run(run())
    assert continuity.current() is None, "the hold is task-scoped"


def test_the_store_moves_only_the_generation_the_caller_holds(as_user):
    uid = int(as_user("alice")["id"])
    db.create_chat_request("i-1", uid, "c", "g-1", {"message": "m"})
    db.set_chat_request_status("i-1", "running")
    assert db.park_chat_request("i-1", "g-other") is None, "not this caller's generation"
    assert db.park_chat_request("i-1", "g-1")["status"] == "queued"
    assert db.park_chat_request("i-1", "g-1") is None, "already parked"
    # A Stop that landed meanwhile is never overwritten.
    db.set_chat_request_status("i-1", "cancelled")
    assert db.park_chat_request("i-1", "g-1") is None
    assert db.resume_queued_chat_request("i-1", "g-1", new_attempt=True) is None
    db.set_chat_request_status("i-1", "queued")
    row = db.resume_queued_chat_request("i-1", "g-1", new_attempt=False)
    assert row["status"] == "running" and row["attempt"] == 1
    db.set_chat_request_status("i-1", "queued")
    row = db.resume_queued_chat_request("i-1", "g-1", new_attempt=True)
    assert row["status"] == "running" and row["attempt"] == 2 and row["generation_id"] == "g-1"
    # The sweep's listing: by status, resumable, young enough, oldest first.
    db.create_chat_request("i-2", uid, "c", "g-2", {"message": "m"})
    db.set_chat_request_status("i-2", "queued")
    db.create_chat_request("i-3", uid, "c", "g-3", {"message": "m"}, resumable=False)
    db.set_chat_request_status("i-3", "queued")
    db.create_chat_request("i-4", uid, "c", "g-4", {"message": "m"})
    db.set_chat_request_status("i-4", "interrupted")
    assert [r["intent_id"] for r in db.list_resumable_chat_requests(("queued",), max_age_s=3600)] == ["i-2"]
    assert [r["intent_id"] for r in db.list_resumable_chat_requests(("queued", "interrupted"), max_age_s=3600)] == ["i-2", "i-4"]
    with db.connection() as con:
        con.execute("UPDATE chat_requests SET updated_at = now() - interval '2 days' WHERE intent_id = 'i-2'")
    assert db.list_resumable_chat_requests(("queued",), max_age_s=3600) == []
    # A queued row survives the start-up reconciliation as itself; only the
    # running one (i-1, resumed above) is marked interrupted.
    assert db.interrupt_open_chat_requests() == 1
    assert db.get_chat_request("i-1")["status"] == "interrupted"
    assert db.get_chat_request("i-2")["status"] == "queued"
    # And the V33 CHECK admits it while refusing anything else.
    with pytest.raises(Exception):
        db.set_chat_request_status("i-2", "parked")
    assert db.LATEST_SCHEMA_VERSION >= 32
