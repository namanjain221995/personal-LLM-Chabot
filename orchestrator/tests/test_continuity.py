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
   resumes it exactly once (the V29 compare-and-swap is the lease), and an
   `interrupted` row beside it too (round 2: the start-up sweep lists both,
   and a READY edge that lands during it is never dropped);
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
    # An interrupted row in another conversation: the start-up sweep's too
    # (round 2 — a closed tab never comes back for it).
    db.create_conversation(uid, "boot-i", "Boot 2")
    db.create_chat_request(
        "int-boot-i", uid, "boot-i", "gen-boot-i",
        {"message": "and this?", "mode": "assistant", "conversation_id": "boot-i", "effort": "fast"},
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
        while time.monotonic() < deadline and db.get_chat_request("int-boot-i")["status"] != "completed":
            time.sleep(0.05)
        while time.monotonic() < deadline and ("boot-q" in _live_generations or "boot-i" in _live_generations):
            time.sleep(0.05)
        row = db.get_chat_request("int-boot-q")
        assert row["status"] == "completed" and row["attempt"] == 2 and row["generation_id"] != "gen-boot-q"
        other = db.get_chat_request("int-boot-i")
        assert other["status"] == "completed" and other["attempt"] == 2 and other["generation_id"] != "gen-boot-i"
        assert client.get("/chat/requests/int-boot-q").json()["answer_persisted"] is True
    rows = _assistant_rows("boot-q")
    assert len(rows) == 1 and rows[0]["generation_id"] == row["generation_id"]
    rows_i = _assistant_rows("boot-i")
    assert len(rows_i) == 1 and rows_i[0]["generation_id"] == other["generation_id"]
    assert _ledger("int-boot-q") == [(2, "recovery", "completed", "ok")]
    assert _ledger("int-boot-i") == [(2, "lost_process", "completed", "ok")]
    assert _counter("llm_resumed_generations_total", outcome="resumed") == 2
    assert len(engine.stream_calls) == 2


def test_the_ready_edge_sweeps_interrupted_rows_and_never_a_row_that_is_live(engine, as_user):
    uid = int(as_user("alice")["id"])
    db.create_conversation(uid, "edge-1", "Edge")
    db.create_chat_request(
        "int-edge-1", uid, "edge-1", "gen-edge-1",
        {"message": "resume me", "mode": "assistant", "conversation_id": "edge-1", "effort": "fast"},
    )
    db.set_chat_request_status("int-edge-1", "interrupted")

    async def scenario():
        _verdict("READY")
        continuity.start()  # registers the edge listener, as the lifespan does
        assert continuity._sweep_task is not None
        await continuity._sweep_task  # the start-up sweep: queued AND interrupted rows
        await _wait_until(lambda: _status("int-edge-1") == "completed")
        await _wait_until(lambda: "edge-1" not in _live_generations)
        # A row interrupted later, and a not-serving → serving edge: the
        # READY sweep takes it.
        db.create_chat_request(
            "int-edge-3", uid, "edge-1", "gen-edge-3",
            {"message": "and me", "mode": "assistant", "conversation_id": "edge-1", "effort": "fast"},
        )
        db.set_chat_request_status("int-edge-3", "interrupted")
        _verdict("RECOVERING")
        await asyncio.sleep(0.05)
        assert db.get_chat_request("int-edge-3")["status"] == "interrupted"
        _verdict("READY")  # the edge
        await _wait_until(lambda: continuity._sweep_task is not None and continuity._sweep_task.done())
        await _wait_until(lambda: _status("int-edge-3") == "completed")
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
    assert _ledger("int-edge-3") == [(2, "lost_process", "completed", "ok")]
    assert len(_assistant_rows("edge-1")) == 2
    assert _counter("llm_resumed_generations_total", outcome="resumed") == 2


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
        # A sibling re-entering the wait during the SAME outage (one of
        # them is the HALF_OPEN canary): silent — the line was said (round
        # 2, continuity.py:208). The attempt moves once either way.
        await hold.enter(continuity.RECOVERY)
        await hold.resume()
        assert gen.attempt == 2 and said == [continuity.QUEUED_LINE]
        # A call SERVED, then a second outage in the same turn: said again
        # (a new outage the person should know about).
        hold.served()
        await hold.enter(continuity.RECOVERY)
        await hold.resume()
        assert gen.attempt == 2 and said == [continuity.QUEUED_LINE, continuity.QUEUED_LINE]
        # An admission wait carries its own line, is never a new attempt,
        # and is NOT a generation queued for the primary (CONTRACT §7.2:
        # lane waits have llm_admission_*; round 2, continuity.py:193).
        waits_before = metrics._hists.get("llm_queue_wait_seconds", {}).get((), (None, 0.0, 0))[2]
        await hold.enter(continuity.ADMISSION, admission.NORMAL_LINE.format(n=3))
        assert _gauge("llm_queued_generations") == 0.0
        await hold.resume()
        assert gen.attempt == 2 and said[-1] == "Waiting for a free slot on the main model (3 ahead)."
        assert metrics._hists["llm_queue_wait_seconds"][()][2] == waits_before, "lane waits are not queue waits"
        # Abandoned (a Stop, a budget): the gauge, and the turn is marked cut.
        await hold.enter(continuity.RECOVERY)
        assert _gauge("llm_queued_generations") == 1.0
        hold.abandon()
        assert _gauge("llm_queued_generations") == 0.0 and not hold.waiting and hold.cut
        # A cut turn does not queue a second time while the engine is still
        # away: the next entry parks it (the second sentence, once).
        with pytest.raises(continuity.QueuedForRecovery):
            await hold.enter(continuity.RECOVERY)
        assert gen.parked is True and hold.parked and said[-1] == continuity.EXPIRED_LINE
        assert _gauge("llm_queued_generations") == 0.0
        assert _counter("llm_resumed_generations_total", outcome="resumed") == 1
        assert _counter("llm_resumed_generations_total", outcome="expired") == 1
        # THE PARK IS FINAL: no re-entry, no second window, no second line.
        with pytest.raises(continuity.QueuedForRecovery):
            await hold.enter(continuity.RECOVERY)
        with pytest.raises(continuity.QueuedForRecovery):
            await hold.enter(continuity.ADMISSION, "x")
        assert said.count(continuity.EXPIRED_LINE) == 1 and not hold.waiting
        assert _counter("llm_resumed_generations_total", outcome="expired") == 1

    asyncio.run(run())
    assert continuity.current() is None, "the hold is task-scoped"


def test_siblings_of_one_turn_say_the_queued_line_once_per_outage(engine, monkeypatch):
    """continuity.py:208: three best-of-N candidates queue on one hold; the
    breaker goes HALF_OPEN, candidate 1 takes the canary, the others
    re-enter the wait — the person reads QUEUED_LINE once for the outage."""
    from app.core import best_of

    monkeypatch.setattr(settings, "llm_queue_max_wait_s", 10.0)
    monkeypatch.setattr(settings, "llm_breaker_cooldown_s", 0.3)
    said: list = []

    async def notify(line: str) -> None:
        said.append(line)

    async def slow_answer(**kwargs):
        engine.other_calls.append(kwargs)
        await asyncio.sleep(0.4)  # the canary takes a while: the siblings re-enter meanwhile
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=engine.text, reasoning_content=None, reasoning=None,
                                        tool_calls=None, model_extra=None),
                finish_reason="stop",
            )],
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2),
        )

    engine.chat.completions.create = slow_answer

    async def scenario():
        brk = breaker.get(breaker.MAIN)
        for _ in range(3):
            brk.record_failure("connection", permit=brk.acquire())
        gen = SimpleNamespace(intent_id=None, generation_id="g", attempt=1, retry_reason="none",
                              request_status="running", parked=False)
        hold = continuity.bind(gen, notify)
        candidates = await best_of.generate_candidates(
            [{"role": "user", "content": "hi"}], n=3, temperature=0.3, max_tokens=64)
        assert all(c.usable for c in candidates)
        return hold, gen

    hold, gen = asyncio.run(scenario())
    assert said == [continuity.QUEUED_LINE], said
    assert len(engine.other_calls) == 3 and gen.attempt == 2
    assert not hold.announced_outage, "served: the next outage earns a new line"


def test_an_expired_hold_parks_once_and_is_never_re_entered(monkeypatch):
    """Round 2, continuity.py:187: expire() is final. A turn whose first
    call parked it (best-of-N candidate 1, say) and whose next call (the
    single-stream fallthrough) reaches the gate again does not wait a
    SECOND window or read either line again — it raises at once."""
    said: list = []

    async def notify(line: str) -> None:
        said.append(line)

    async def run():
        gen = SimpleNamespace(intent_id=None, generation_id="g", attempt=1, retry_reason="none",
                              request_status="running", parked=False)
        hold = continuity.bind(gen, notify)
        await hold.enter(continuity.RECOVERY)
        with pytest.raises(continuity.QueuedForRecovery):
            await hold.expire()
        assert said == [continuity.QUEUED_LINE, continuity.EXPIRED_LINE] and gen.parked is True
        for _ in range(3):
            with pytest.raises(continuity.QueuedForRecovery):
                await hold.enter(continuity.RECOVERY)
        assert said == [continuity.QUEUED_LINE, continuity.EXPIRED_LINE]
        assert _counter("llm_resumed_generations_total", outcome="expired") == 1
        assert _gauge("llm_queued_generations") == 0.0

    asyncio.run(run())


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
    # Round 2 (continuity.py:84): a `queued` row is a promise and is listed
    # whatever its age; LLM_RESUME_MAX_AGE_S bounds `interrupted` rows only,
    # from the moment the send was ACCEPTED (created_at), not last touched.
    with db.connection() as con:
        con.execute("UPDATE chat_requests SET updated_at = now() - interval '2 days', "
                    "created_at = now() - interval '2 days' WHERE intent_id = 'i-2'")
        con.execute("UPDATE chat_requests SET created_at = now() - interval '2 hours' WHERE intent_id = 'i-4'")
    assert [r["intent_id"] for r in db.list_resumable_chat_requests(("queued", "interrupted"), max_age_s=3600)] == ["i-2"]
    assert [r["intent_id"] for r in db.list_resumable_chat_requests(("queued", "interrupted"), max_age_s=3 * 3600)] == ["i-2", "i-4"]
    assert db.count_chat_requests("queued") == 2
    # A queued row survives the start-up reconciliation as itself; only the
    # running one (i-1, resumed above) is marked interrupted.
    assert db.interrupt_open_chat_requests() == 1
    assert db.get_chat_request("i-1")["status"] == "interrupted"
    assert db.get_chat_request("i-2")["status"] == "queued"
    # And the V33 CHECK admits it while refusing anything else.
    with pytest.raises(Exception):
        db.set_chat_request_status("i-2", "parked")
    assert db.LATEST_SCHEMA_VERSION >= 32


# ---------------------------------------------------------------------------
# Round-2 review findings (docs/availability/REVIEW-FINDINGS-round2.md)
# ---------------------------------------------------------------------------


class _DiesBeforeFirstChunk:
    """A stream that opened (headers) and then raises from its first
    `__anext__` — the 2026-09-11 shape: the rank died under the open."""

    def __init__(self, exc_factory, on_die=None) -> None:
        self._exc_factory = exc_factory
        self._on_die = on_die
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._on_die is not None:
            self._on_die()
        raise self._exc_factory()

    async def close(self) -> None:
        self.closed = True


def test_a_stream_that_dies_before_its_first_chunk_is_queued_and_resumed_not_failed(engine, monkeypatch):
    """resilience.py:663 (both lenses): a body that dies BEFORE its first
    token is a failed OPEN — retried or queued like a refused connection,
    never MODEL_UNAVAILABLE while the controller says the engine is away.
    The head restarts under the open stream; the controller confirms
    RECOVERING as the body dies; the SAME generation resumes on READY."""
    monkeypatch.setattr(settings, "llm_queue_max_wait_s", 10.0)
    opens = {"n": 0}

    def head_restarting() -> None:
        # The controller's verdict lands as the body dies.
        _verdict("RECOVERING")

    async def create(**kwargs):
        opens["n"] += 1
        if opens["n"] == 1:
            return _DiesBeforeFirstChunk(lambda: httpx.ReadError("connection reset by peer"), head_restarting)
        engine.stream_calls.append(kwargs)
        return _Stream(engine.text)

    engine.chat.completions.create = create

    async def scenario():
        _verdict("READY")
        async with _async_client() as client:
            request = asyncio.create_task(client.post("/chat", json=_body("die-1", "int-die-1")))
            await _wait_until(lambda: "die-1" in _live_generations)
            gen = _live_generations["die-1"]
            first_generation = gen.generation_id
            # The death before the first token went back through the gate:
            # the row is queued, the person told once, nothing failed.
            await _wait_until(lambda: _status("int-die-1") == "queued")
            assert opens["n"] == 1 and engine.stream_calls == []
            assert not gen.failed and not gen.parked
            await asyncio.sleep(0.5)
            assert not gen.done, "held for READY, not failed"
            _verdict("READY")
            resp = await request
        return gen, first_generation, resp

    gen, first_generation, resp = asyncio.run(scenario())
    events = _parse_sse(resp.text)
    kinds = [k for k, _ in events]
    assert kinds[-1] == "done" and kinds.count("error") == 0, "never MODEL_UNAVAILABLE"
    assert [d["text"] for k, d in events if k == "status"] == [continuity.QUEUED_LINE]
    assert "".join(d["text"] for k, d in events if k == "token") == "from the main model"
    assert opens["n"] == 2 and len(engine.stream_calls) == 1
    assert gen.generation_id == first_generation and gen.attempt == 2 and gen.retry_reason == "recovery"
    row = db.get_chat_request("int-die-1")
    assert row["status"] == "completed" and row["attempt"] == 2 and row["generation_id"] == first_generation
    rows = _assistant_rows("die-1")
    assert len(rows) == 1 and rows[0]["content"] == "from the main model" and "error" not in rows[0]["meta"]
    assert _ledger("int-die-1") == [(2, "recovery", "completed", "ok")]
    assert _counter("llm_retry_total", what="stream", reason="connection") == 1
    assert _counter("llm_breaker_failures_total", engine="main", reason="connection") == 1
    assert _counter("llm_resumed_generations_total", outcome="resumed") == 1


def test_a_stream_dying_before_its_first_chunk_with_no_verdict_is_retried_on_the_open(engine, monkeypatch):
    """The same death with the breaker CLOSED and no controller: a retry of
    the OPEN inside one attempt (CONTRACT §8.4 allows it before the first
    token) — one answer, attempt 1, nothing shown twice."""
    monkeypatch.setattr(settings, "llm_interactive_recovery_s", 30.0)
    opens = {"n": 0}

    async def create(**kwargs):
        opens["n"] += 1
        if opens["n"] == 1:
            return _DiesBeforeFirstChunk(lambda: openai.APIError(
                "EngineDeadError: engine core died", request=_REQ, body=None))
        engine.stream_calls.append(kwargs)
        return _Stream(engine.text)

    engine.chat.completions.create = create

    async def scenario():
        async with _async_client() as client:
            return await client.post("/chat", json=_body("die-2", "int-die-2"))

    resp = asyncio.run(scenario())
    events = _parse_sse(resp.text)
    assert events[-1][0] == "done" and not any(k == "error" for k, _ in events)
    assert "".join(d["text"] for k, d in events if k == "token") == "from the main model"
    assert opens["n"] == 2
    assert _ledger("int-die-2") == [(1, "none", "completed", "ok")]
    assert _counter("llm_retry_total", what="stream", reason="engine_error") == 1
    assert len(_assistant_rows("die-2")) == 1


def test_a_sidecar_on_the_main_url_never_expires_the_turns_hold(engine, monkeypatch):
    """resilience.py:549: with the router configured onto the main URL, a
    sidecar call (one attempt, no window) that gives up while the answer
    waits at the gate must not park the turn — the answer keeps waiting
    and resumes on READY with ONE queued line and no EXPIRED_LINE."""
    monkeypatch.setattr(settings, "router_base_url", MAIN_URL)
    monkeypatch.setattr(settings, "llm_queue_max_wait_s", 10.0)
    sidecar_gave_up = {"n": 0}
    original = resilience._give_up

    async def counting_give_up(hold, **kwargs):
        if hold is None:
            sidecar_gave_up["n"] += 1
        return await original(hold, **kwargs)

    monkeypatch.setattr(resilience, "_give_up", counting_give_up)

    async def scenario():
        _verdict("RECOVERING")
        async with _async_client() as client:
            request = asyncio.create_task(client.post("/chat", json=_body("side-1", "int-side-1")))
            await _wait_until(lambda: _status("int-side-1") == "queued")
            gen = _live_generations["side-1"]
            # Give every sidecar of the turn time to give up.
            await asyncio.sleep(1.0)
            assert not gen.parked and _status("int-side-1") == "queued" and not gen.done
            assert _counter("llm_resumed_generations_total", outcome="expired") == 0
            _verdict("READY")
            resp = await request
        return gen, resp

    gen, resp = asyncio.run(scenario())
    events = _parse_sse(resp.text)
    said = [d["text"] for k, d in events if k == "status"]
    assert said == [continuity.QUEUED_LINE], said
    assert events[-1][0] == "done"
    assert gen.attempt == 2 and gen.retry_reason == "recovery" and not gen.parked
    assert db.get_chat_request("int-side-1")["status"] == "completed"
    assert _counter("llm_resumed_generations_total", outcome="expired") == 0
    assert _ledger("int-side-1") == [(2, "recovery", "completed", "ok")]


def test_a_ready_edge_that_lands_during_the_startup_sweep_is_not_dropped(monkeypatch):
    """continuity.py:486 (both lenses): a trigger that lands while a sweep
    is in flight runs right after it — never dropped."""
    ran: list = []
    release = asyncio.Event()

    async def slow_sweep(trigger: str) -> dict:
        ran.append(trigger)
        if trigger == "startup":
            await release.wait()
        return {"resumed": 0, "duplicate_suppressed": 0, "skipped": 0}

    monkeypatch.setattr(continuity, "resume_sweep", slow_sweep)

    async def scenario():
        continuity.start()
        await asyncio.sleep(0.02)
        assert ran == ["startup"]
        _verdict("READY")  # the edge lands mid-sweep
        await asyncio.sleep(0.02)
        assert ran == ["startup"], "coalesced, not run concurrently"
        assert continuity._pending_trigger == "ready"
        release.set()
        await _wait_until(lambda: continuity._sweep_task is not None and continuity._sweep_task.done())
        assert ran == ["startup", "ready"]
        await continuity.stop()

    asyncio.run(scenario())


def test_a_queued_row_is_resumed_whatever_its_age(engine, as_user):
    """continuity.py:84: a DOWN night longer than LLM_RESUME_MAX_AGE_S must
    still resume every parked request when the pair is back (CONTRACT §2:
    never lost). The age limit binds `interrupted` rows only."""
    uid = int(as_user("alice")["id"])
    db.create_conversation(uid, "old-q", "Old")
    db.create_chat_request(
        "int-old-q", uid, "old-q", "gen-old-q",
        {"message": "still here?", "mode": "assistant", "conversation_id": "old-q", "effort": "fast"},
    )
    db.park_chat_request("int-old-q", "gen-old-q")
    db.create_conversation(uid, "old-i", "Old 2")
    db.create_chat_request(
        "int-old-i", uid, "old-i", "gen-old-i",
        {"message": "and me?", "mode": "assistant", "conversation_id": "old-i", "effort": "fast"},
    )
    db.set_chat_request_status("int-old-i", "interrupted")
    with db.connection() as con:
        con.execute("UPDATE chat_requests SET created_at = now() - interval '2 days', "
                    "updated_at = now() - interval '2 days' WHERE intent_id IN ('int-old-q', 'int-old-i')")

    async def scenario():
        _verdict("READY")
        counts = await continuity.resume_sweep("ready")
        assert counts["resumed"] == 1
        await _wait_until(lambda: _status("int-old-q") == "completed")
        await _wait_until(lambda: "old-q" not in _live_generations)

    asyncio.run(scenario())
    assert db.get_chat_request("int-old-q")["status"] == "completed"
    assert db.get_chat_request("int-old-i")["status"] == "interrupted", "too old: left alone"
    assert len(engine.stream_calls) == 1


def test_an_interrupted_row_that_streamed_tokens_is_not_re_run_by_the_sweep(engine, as_user):
    """continuity.py:425 (CONTRACT §8.4): after the first token the partial
    is kept and no second answer is appended. Two kinds of evidence — the
    ledger's ttft (an orderly shutdown wrote it) and the viewer's persisted
    partial (all a crash leaves) — and in both the row is settled `failed`
    for a person's Retry, the partial untouched, the engine not called."""
    from app import usage

    uid = int(as_user("alice")["id"])
    # (a) the ledger says the attempt streamed
    db.create_conversation(uid, "part-a", "Partial A")
    db.create_chat_request(
        "int-part-a", uid, "part-a", "gen-part-a",
        {"message": "long one", "mode": "assistant", "conversation_id": "part-a", "effort": "fast"},
    )
    db.set_chat_request_status("int-part-a", "interrupted")
    usage.record(user_id=uid, workspace_id=None, conversation_id="part-a", generation_id="gen-part-a",
                 route="chat", ttft_ms=800, status=usage.CANCELLED,
                 meta={"intent_id": "int-part-a", "attempt": 1, "terminal_state": "interrupted"})
    assert db.generation_streamed("gen-part-a") is True
    # (b) only the viewer's partial exists
    db.create_conversation(uid, "part-b", "Partial B")
    db.create_chat_request(
        "int-part-b", uid, "part-b", "gen-part-b",
        {"message": "another", "mode": "assistant", "conversation_id": "part-b", "effort": "fast"},
    )
    db.set_chat_request_status("int-part-b", "interrupted")
    db.add_message(uid, "part-b", "assistant", "The first half of the ans",
                   {"generation_id": "gen-part-b", "intent_id": "int-part-b", "attempt": 1,
                    "error": {"message": "interrupted", "code": "MODEL_UNAVAILABLE", "resumable": True}})
    assert db.generation_streamed("gen-part-b") is False
    # (c) an interrupted row with neither: nothing was kept anywhere → re-run
    db.create_conversation(uid, "part-c", "Partial C")
    db.create_chat_request(
        "int-part-c", uid, "part-c", "gen-part-c",
        {"message": "third", "mode": "assistant", "conversation_id": "part-c", "effort": "fast"},
    )
    db.set_chat_request_status("int-part-c", "interrupted")

    async def scenario():
        _verdict("READY")
        counts = await continuity.resume_sweep("ready")
        assert counts == {"resumed": 1, "duplicate_suppressed": 0, "skipped": 0, "kept_partial": 2}
        await _wait_until(lambda: _status("int-part-c") == "completed")
        await _wait_until(lambda: "part-c" not in _live_generations)

    asyncio.run(scenario())
    row_a = db.get_chat_request("int-part-a")
    assert row_a["status"] == "failed" and row_a["attempt"] == 1
    assert row_a["error"] == continuity.INTERRUPTED_NOTHING_KEPT, "no partial to point at"
    row_b = db.get_chat_request("int-part-b")
    assert row_b["status"] == "failed" and row_b["attempt"] == 1
    assert row_b["error"] == continuity.INTERRUPTED_AFTER_TOKENS
    rows_b = _assistant_rows("part-b")
    assert len(rows_b) == 1 and rows_b[0]["content"] == "The first half of the ans", "the partial stays"
    assert "error" in rows_b[0]["meta"]
    assert _assistant_rows("part-a") == []
    assert len(engine.stream_calls) == 1, "only the row with no evidence of a token is re-run"
    assert _counter("llm_resumed_generations_total", outcome="resumed") == 1
    # Asked again by the person: a new attempt (the Retry the row now offers).
    async def retry():
        async with _async_client() as client:
            return await client.post("/chat", json=_body("part-b", "int-part-b", message="another"))

    resp = asyncio.run(retry())
    assert _parse_sse(resp.text)[-1][0] == "done"
    assert db.get_chat_request("int-part-b")["status"] == "completed" and db.get_chat_request("int-part-b")["attempt"] == 2
    rows_b = _assistant_rows("part-b")
    assert len(rows_b) == 1 and rows_b[0]["content"] == "from the main model", "a person's retry supersedes the partial"


def test_a_browser_re_attach_never_re_runs_an_attempt_that_streamed(engine, as_user):
    """continuity.py:425, the other half: the automatic /chat/attach after a
    restart must not append a second answer beside the kept partial — the
    row is settled `failed` for a Retry and the re-attach ends like a
    finished one (404: load history)."""
    uid = int(as_user("alice")["id"])
    db.create_conversation(uid, "att-1", "Attach")
    db.create_chat_request(
        "int-att-1", uid, "att-1", "gen-att-1",
        {"message": "long one", "mode": "assistant", "conversation_id": "att-1", "effort": "fast"},
    )
    db.set_chat_request_status("int-att-1", "interrupted")
    db.add_message(uid, "att-1", "assistant", "Half an ans",
                   {"generation_id": "gen-att-1", "intent_id": "int-att-1", "attempt": 1,
                    "error": {"message": "interrupted", "code": "MODEL_UNAVAILABLE", "resumable": True}})

    async def scenario():
        _verdict("READY")
        async with _async_client() as client:
            return await client.get("/chat/attach/att-1")

    resp = asyncio.run(scenario())
    assert resp.status_code == 404
    row = db.get_chat_request("int-att-1")
    assert row["status"] == "failed" and row["error"] == continuity.INTERRUPTED_AFTER_TOKENS
    rows = _assistant_rows("att-1")
    assert len(rows) == 1 and rows[0]["content"] == "Half an ans"
    assert engine.stream_calls == []


def test_the_sweep_yields_to_a_newer_send_in_the_conversation(engine, as_user):
    """continuity.py:398 (both lenses): a person's send that lands between
    the sweep's busy check and /chat's "newest message wins" block must
    win — the sweep's resume is refused, the row put back for the next
    READY, the person's generation not cancelled."""
    uid = int(as_user("alice")["id"])
    db.create_conversation(uid, "race-1", "Race")
    db.create_chat_request(
        "int-race-old", uid, "race-1", "gen-race-old",
        {"message": "old question", "mode": "assistant", "conversation_id": "race-1", "effort": "fast"},
    )
    db.park_chat_request("int-race-old", "gen-race-old")
    original = app_main._retry_chat_request

    async def scenario():
        _verdict("READY")
        # The person's live generation, as /chat would register it.
        live = app_main.LiveGeneration("race-1", uid)
        live.intent_id = "int-race-new"
        live.task = asyncio.create_task(asyncio.sleep(30))

        # 1. Before the compare-and-swap: the sweep sees the live generation and refuses.
        _live_generations["race-1"] = live
        principal = continuity._principal_for(uid)
        request = app_main.ChatRequest.model_validate(
            {"message": "old question", "mode": "assistant", "conversation_id": "race-1", "intent_id": "int-race-old"})
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as info:
            await app_main.chat(request, continuity._synthetic_request(principal))
        assert info.value.status_code == 409 and "busy" in info.value.detail
        row = db.get_chat_request("int-race-old")
        assert row["status"] == "queued" and row["generation_id"] == "gen-race-old" and row["attempt"] == 1
        assert not live.task.cancelled()
        _live_generations.pop("race-1")

        # 2. During the compare-and-swap: the send lands while the CAS runs
        # in its thread; the sweep loses at the "newest wins" block, the row
        # goes back to `queued`, the person's generation is untouched.
        def cas_then_send(intent_id, generation_id, **kwargs):
            moved = original(intent_id, generation_id, **kwargs)
            _live_generations["race-1"] = live
            return moved

        app_main._retry_chat_request = cas_then_send
        try:
            with pytest.raises(HTTPException) as info:
                await app_main.chat(request, continuity._synthetic_request(principal))
        finally:
            app_main._retry_chat_request = original
        assert info.value.status_code == 409 and "busy" in info.value.detail
        row = db.get_chat_request("int-race-old")
        assert row["status"] == "queued" and row["generation_id"] != "gen-race-old"
        assert not live.task.cancelled() and not live.replaced
        assert _live_generations["race-1"] is live
        # 3. The sweep counts it as skipped and leaves it for the next READY.
        assert await continuity.resume_sweep("ready") == {"resumed": 0, "duplicate_suppressed": 0, "skipped": 1}
        live.task.cancel()
        _live_generations.clear()

    asyncio.run(scenario())
    assert engine.stream_calls == []
    assert _counter("llm_resumed_generations_total", outcome="resumed") == 0


def test_a_new_send_supersedes_a_parked_row_in_the_conversation(engine, as_user):
    """main.py:1775: the newest message wins over a PARKED question too —
    a row left `queued` with no live generation is cancelled ('replaced by
    a newer message') when the person sends again, so the sweep never
    answers the old question below the new exchange."""
    uid = int(as_user("alice")["id"])
    db.create_conversation(uid, "sup-1", "Supersede")
    db.create_chat_request(
        "int-sup-old", uid, "sup-1", "gen-sup-old",
        {"message": "first try", "mode": "assistant", "conversation_id": "sup-1", "effort": "fast"},
    )
    db.park_chat_request("int-sup-old", "gen-sup-old")

    async def scenario():
        _verdict("READY")
        async with _async_client() as client:
            resp = await client.post("/chat", json=_body("sup-1", "int-sup-new", message="asking again"))
            assert _parse_sse(resp.text)[-1][0] == "done"
            await _wait_until(lambda: "sup-1" not in _live_generations)
            return await continuity.resume_sweep("ready")

    counts = asyncio.run(scenario())
    old = db.get_chat_request("int-sup-old")
    assert old["status"] == "cancelled" and old["error"] == "replaced by a newer message"
    assert db.get_chat_request("int-sup-new")["status"] == "completed"
    assert counts == {"resumed": 0, "duplicate_suppressed": 0, "skipped": 0}
    assert len(engine.stream_calls) == 1 and len(_assistant_rows("sup-1")) == 1


def test_a_parked_turn_nothing_can_resume_is_failed_with_resumable_false(engine, monkeypatch):
    """main.py:3242: a snapshot that cannot be resumed (inline image bytes
    the server does not keep) must not be promised 'will resume
    automatically' — the turn fails with a truthful sentence, a Retry the
    browser can make, and a `failed` row, not a `queued` one no sweep will
    ever list."""
    monkeypatch.setattr(settings, "llm_queue_max_wait_s", 0.5)
    png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="

    async def scenario():
        _verdict("RECOVERING")
        async with _async_client() as client:
            return await client.post("/chat", json=_body(
                "img-1", "int-img-1", message="what is this?", image_base64=png))

    resp = asyncio.run(scenario())
    events = _parse_sse(resp.text)
    assert resp.status_code == 200
    final = events[-1]
    assert final[0] == "error" and final[1]["code"] == "MODEL_UNAVAILABLE" and final[1]["resumable"] is False
    assert final[1]["message"] == app_main._NOT_RESUMABLE_SENTENCE
    row = db.get_chat_request("int-img-1")
    assert row["status"] == "failed" and row["resumable"] is False
    assert engine.stream_calls == []
    # Nothing for the sweep, ever.
    assert asyncio.run(continuity.resume_sweep("ready")) == {"resumed": 0, "duplicate_suppressed": 0, "skipped": 0}


def test_hold_resume_raises_lease_lost_when_the_row_was_taken_over(engine, as_user):
    """continuity.py:239: a second process's sweep took the row under its
    own generation while this hold slept; on READY this generation must
    not answer — the worker ends the turn with the `replaced` code, writes
    no status, and the ledger says the attempt was interrupted."""
    uid = int(as_user("alice")["id"])
    db.create_conversation(uid, "lease-1", "Lease")

    async def scenario():
        _verdict("RECOVERING")
        async with _async_client() as client:
            request = asyncio.create_task(client.post("/chat", json=_body("lease-1", "int-lease-1")))
            await _wait_until(lambda: _status("int-lease-1") == "queued")
            gen = _live_generations["lease-1"]
            # Another process resumes the row under its own generation.
            taken = db.resume_chat_request("int-lease-1", "gen-elsewhere", expected_generation_id=gen.generation_id)
            assert taken["generation_id"] == "gen-elsewhere" and taken["attempt"] == 2
            db.set_chat_request_status("int-lease-1", "running")
            _verdict("READY")
            resp = await request
            await _wait_until(lambda: bool(_ledger("int-lease-1")))
        return gen, resp

    gen, resp = asyncio.run(scenario())
    events = _parse_sse(resp.text)
    assert events[-1] == ("error", {"message": app_main._TAKEN_OVER_SENTENCE, "code": "replaced"})
    assert gen.lease_lost and not gen.failed and not gen.parked
    row = db.get_chat_request("int-lease-1")
    assert row["generation_id"] == "gen-elsewhere" and row["status"] == "running", "theirs, untouched"
    assert engine.stream_calls == [] and _assistant_rows("lease-1") == []
    assert _ledger("int-lease-1") == [(1, "none", "interrupted", "ok")] or _ledger("int-lease-1")[0][2] == "interrupted"


def test_the_sweep_counts_a_resume_that_attached_elsewhere_as_duplicate_suppressed(engine, as_user):
    """continuity.py:441: when /chat attaches to a generation somebody else
    started (a browser's re-attach won the compare-and-swap), the sweep
    must count duplicate_suppressed, not resumed."""
    uid = int(as_user("alice")["id"])
    db.create_conversation(uid, "acc-1", "Accounting")
    db.create_chat_request(
        "int-acc-1", uid, "acc-1", "gen-acc-1",
        {"message": "count me", "mode": "assistant", "conversation_id": "acc-1", "effort": "fast"},
    )
    db.park_chat_request("int-acc-1", "gen-acc-1")
    original = app_main._retry_chat_request

    async def scenario():
        _verdict("READY")
        browser = app_main.LiveGeneration("acc-1", uid)
        browser.intent_id = "int-acc-1"
        browser.generation_id = "gen-browser"

        def browser_wins(intent_id, generation_id, **kwargs):
            # The browser's re-attach moved the row first.
            row = original(intent_id, "gen-browser", **kwargs)
            _live_generations["acc-1-browser"] = browser
            return row

        app_main._retry_chat_request = browser_wins
        try:
            counts = await continuity.resume_sweep("ready")
        finally:
            app_main._retry_chat_request = original
            _live_generations.clear()
        return counts

    counts = asyncio.run(scenario())
    assert counts == {"resumed": 0, "duplicate_suppressed": 1, "skipped": 0}
    assert _counter("llm_resumed_generations_total", outcome="resumed") == 0
    assert _counter("llm_resumed_generations_total", outcome="duplicate_suppressed") == 1
    assert db.get_chat_request("int-acc-1")["generation_id"] == "gen-browser"


def test_llm_queued_generations_counts_durably_queued_rows(engine, as_user):
    """continuity.py:117 (decision 9): the gauge is max(holds in this
    process, rows `queued` in the database) — a long DOWN that expired
    every hold, or an orchestrator restart, never reads as 'nothing
    queued' while rows wait."""
    from app import health

    uid = int(as_user("alice")["id"])
    db.create_conversation(uid, "gauge-1", "Gauge")
    for i in range(3):
        db.create_chat_request(
            f"int-gauge-{i}", uid, "gauge-1", f"gen-gauge-{i}",
            {"message": "m", "mode": "assistant", "conversation_id": "gauge-1", "effort": "fast"},
        )
        db.park_chat_request(f"int-gauge-{i}", f"gen-gauge-{i}")
    assert _gauge("llm_queued_generations") == 0.0, "nothing held in this process yet"
    health._publish_work_gauges(health._read_work())
    assert _gauge("chat_requests_queued") == 3.0
    assert _gauge("llm_queued_generations") == 3.0, "the durable rows count"
    assert continuity.describe()["queued_rows"] == 3

    async def scenario():
        _verdict("READY")
        counts = await continuity.resume_sweep("ready")
        assert counts["resumed"] == 1  # one conversation: the others wait for it
        await _wait_until(lambda: "gauge-1" not in _live_generations)

    asyncio.run(scenario())
    assert _gauge("llm_queued_generations") == 2.0, "re-counted after the sweep"


def test_the_breaker_closing_sweeps_when_the_controller_is_unreachable(engine, as_user, monkeypatch):
    """breaker.py:89 (dead hook): with no controller verdict the breaker
    works from observed failures; its OPEN/HALF_OPEN → CLOSED edge is the
    only READY there is, and it must sweep."""
    monkeypatch.setattr(settings, "llm_breaker_cooldown_s", 0.0)
    ran: list = []

    async def fake_sweep(trigger: str) -> dict:
        ran.append(trigger)
        return {"resumed": 0, "duplicate_suppressed": 0, "skipped": 0}

    monkeypatch.setattr(continuity, "resume_sweep", fake_sweep)

    async def scenario():
        continuity.start()
        await _wait_until(lambda: ran == ["startup"])
        brk = breaker.get(breaker.MAIN)
        for _ in range(3):
            brk.record_failure("connection", permit=brk.acquire())
        assert brk.state in (breaker.OPEN, breaker.HALF_OPEN)  # the zero cooldown runs at once
        permit = brk.acquire()  # HALF_OPEN after the (zero) cooldown: the canary
        assert permit is not None and permit.canary
        brk.record_success(permit)
        assert brk.state == breaker.CLOSED
        await _wait_until(lambda: ran == ["startup", "breaker_closed"])
        await continuity.stop()

    asyncio.run(scenario())


def test_admission_capacity_refusal_has_its_own_sentence():
    """admission.py:129: a `capacity` refusal is said as what it is."""
    from app import admission as adm

    sentence, code = app_main._failure_sentence(adm.AdmissionRejected("normal", "capacity", 0.0))
    assert sentence == "The model's queue is full right now. Please try again in a moment." and code == "TIMEOUT"
    sentence, code = app_main._failure_sentence(adm.AdmissionRejected("normal", "timeout", 600.0))
    assert "could not start your request in time" in sentence and code == "TIMEOUT"
    assert settings.admission_max_waiting >= continuity._SWEEP_LIMIT + 100 or settings.admission_max_waiting != 100


def test_an_orderly_shutdown_leaves_a_held_rows_queued_status_alone(engine, as_user, monkeypatch):
    """continuity.py:486 (the shutdown half): a hold torn down by the
    lifespan's exit keeps its row `queued` — it says the truth about
    itself, and the next process's sweep lists it by name without an age
    limit — instead of being rewritten `interrupted`."""
    uid = int(as_user("alice")["id"])
    db.create_chat_request("int-shut-1", uid, "shut-1", "gen-shut-1", {"message": "m"})
    db.set_chat_request_status("int-shut-1", "running")
    assert db.park_chat_request("int-shut-1", "gen-shut-1")["status"] == "queued"
    gen = app_main.LiveGeneration("shut-1", uid)
    gen.intent_id = "int-shut-1"
    gen.generation_id = "gen-shut-1"
    gen.request_status = "queued"
    gen.cancelled = True  # the loop's teardown cancelled the worker
    monkeypatch.setattr(app_main, "_shutting_down", True)
    asyncio.run(app_main._settle_chat_request(gen))
    assert db.get_chat_request("int-shut-1")["status"] == "queued"
    # Not shutting down: a Stop while queued is a Stop.
    monkeypatch.setattr(app_main, "_shutting_down", False)
    asyncio.run(app_main._settle_chat_request(gen))
    assert db.get_chat_request("int-shut-1")["status"] == "cancelled"
