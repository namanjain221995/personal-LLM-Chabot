"""The relay between the engine and the browser (plan items 1 and 8).

2026-09-13: the engine decodes ~105 tok/s single-stream, people SAW a p50 of
87.6 tok/s (usage_events, n=13) — a 15-35% loss after the engine, largest on
short answers. Three causes are pinned here:

* one HTTP write per token: LiveGeneration.follow now coalesces live tokens
  into ~25 ms frames (sse.COALESCE_SECONDS) without holding back the first
  token, a terminal frame, or the 15 s keep-alive;
* trace INSERTs awaited on the path to the first token: they now run on an
  ordered background writer (main._QueuedTraceRecorder) that is drained
  before the stream closes;
* an unread clarification cancel awaited before the engine: it now runs
  beside the answer, and `done` waits for it (bounded), so a follow-up sent
  the instant the answer ends never finds the stale card.

And one guarantee coalescing must keep: the first answer token and the first
reasoning token are never held by the frame window, whatever was written just
before them.

And the measurements that prove it in production: relay_overhead_seconds,
chat_first_visible_seconds, context_assembly_seconds and
orchestrate_decide_seconds are observed under the metric engineer's names.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from app import db, llm, main, metrics, sse
from app.config import settings
from app.engines import router as router_engine


def _parse_sse(body: str):
    events = []
    for block in body.split("\n\n"):
        lines = [line for line in block.split("\n") if line and not line.startswith(":")]
        if len(lines) >= 2 and lines[0].startswith("event: "):
            events.append((lines[0][7:], json.loads(lines[1][6:])))
    return events


def _frames(events) -> str:
    return "".join(sse.sse_event(kind, data) for kind, data in events)


async def _read_live(gen: main.LiveGeneration, writes: list, stamps: list) -> None:
    async for chunk in gen.follow():
        writes.append(chunk)
        stamps.append(time.perf_counter())


# ---------------------------------------------------------------------------
# Frame coalescing (LiveGeneration.follow)
# ---------------------------------------------------------------------------


def test_the_first_token_is_written_at_once_and_the_tokens_behind_it_share_frames(monkeypatch):
    monkeypatch.setattr(sse, "COALESCE_SECONDS", 0.025)
    published = [("token", {"text": f"t{i} "}) for i in range(12)]

    async def scenario():
        gen = main.LiveGeneration("relay-1", None)
        writes: list = []
        stamps: list = []
        reader = asyncio.ensure_future(_read_live(gen, writes, stamps))
        await asyncio.sleep(0.01)
        started = time.perf_counter()
        await gen.publish(*published[0])
        await asyncio.sleep(0.005)
        first_write_delay = stamps[0] - started if stamps else None
        for event in published[1:]:  # ~decode speed: one token every 4 ms
            await gen.publish(*event)
            await asyncio.sleep(0.004)
        await gen.publish("done", {})
        await gen.finish()
        await asyncio.wait_for(reader, 2)
        return writes, first_write_delay

    writes, first_write_delay = asyncio.run(scenario())
    assert first_write_delay is not None and first_write_delay < 0.02, "the first token waited for a frame"
    assert writes[0] == sse.sse_event(*published[0])
    assert len(writes) < len(published), f"{len(writes)} writes for {len(published) + 1} events"
    # Concatenated, never merged: the bytes are the per-event frames.
    assert "".join(writes) == _frames([*published, ("done", {})])


def _first_token_delay(first_kind: str, token_kind: str) -> float:
    """Seconds from publishing `token_kind` to the write that carries it,
    3 ms after a `first_kind` frame was WRITTEN to a live reader."""

    async def scenario():
        gen = main.LiveGeneration("relay-first", None)
        writes: list = []
        stamps: list = []
        reader = asyncio.ensure_future(_read_live(gen, writes, stamps))
        await asyncio.sleep(0.01)
        await gen.publish(first_kind, {"text": "Reading your documents"})
        for _ in range(200):  # until the first frame is on the wire
            if writes:
                break
            await asyncio.sleep(0.0005)
        await asyncio.sleep(0.003)
        published = time.perf_counter()
        await gen.publish(token_kind, {"text": "Hello"})
        for _ in range(400):
            if len(writes) >= 2:
                break
            await asyncio.sleep(0.0002)
        delay = stamps[1] - published if len(stamps) >= 2 else float("inf")
        await gen.publish("done", {})
        await gen.finish()
        await asyncio.wait_for(reader, 2)
        assert "".join(writes) == _frames(
            [(first_kind, {"text": "Reading your documents"}), (token_kind, {"text": "Hello"}), ("done", {})]
        )
        return delay

    return asyncio.run(scenario())


@pytest.mark.parametrize(
    "first_kind,token_kind",
    [("status", "token"), ("reasoning", "token"), ("status", "reasoning")],
)
def test_the_first_token_and_the_first_reasoning_are_never_held_by_the_coalescing_window(
    monkeypatch, first_kind, token_kind
):
    # 2026-09-13, prover: with the window keyed on ANY previous write, the
    # first answer token 3 ms behind a status line or behind reasoning waited
    # 22.3 ms — invisible to chat_ttft_seconds, which is stamped before
    # publish. The hold now needs a streamed write of the same kind first.
    monkeypatch.setattr(sse, "COALESCE_SECONDS", 0.025)
    delays = sorted(_first_token_delay(first_kind, token_kind) for _ in range(5))
    median = delays[2]
    assert median < 0.005, f"first {token_kind} after {first_kind} waited {[round(d * 1000, 1) for d in delays]} ms"


def test_a_second_token_right_behind_the_first_is_still_coalesced(monkeypatch):
    # The first-token rule must not switch coalescing off: once a token has
    # been written, the next ones inside the window share a frame.
    monkeypatch.setattr(sse, "COALESCE_SECONDS", 0.05)

    async def scenario():
        gen = main.LiveGeneration("relay-second", None)
        writes: list = []
        reader = asyncio.ensure_future(_read_live(gen, writes, []))
        await asyncio.sleep(0.01)
        await gen.publish("status", {"text": "Searching"})
        await asyncio.sleep(0.005)
        await gen.publish("token", {"text": "a"})
        await asyncio.sleep(0.005)
        for piece in ("b", "c", "d"):
            await gen.publish("token", {"text": piece})
            await asyncio.sleep(0.002)
        await asyncio.sleep(0.1)
        await gen.publish("done", {})
        await gen.finish()
        await asyncio.wait_for(reader, 2)
        return writes

    writes = asyncio.run(scenario())
    assert writes[:2] == [sse.sse_event("status", {"text": "Searching"}), sse.sse_event("token", {"text": "a"})]
    assert writes[2] == _frames([("token", {"text": "b"}), ("token", {"text": "c"}), ("token", {"text": "d"})])


def test_a_pending_terminal_frame_is_written_without_waiting_out_the_window(monkeypatch):
    monkeypatch.setattr(sse, "COALESCE_SECONDS", 0.5)

    async def scenario():
        gen = main.LiveGeneration("relay-2", None)
        writes: list = []
        stamps: list = []
        reader = asyncio.ensure_future(_read_live(gen, writes, stamps))
        await asyncio.sleep(0.01)
        await gen.publish("token", {"text": "a"})
        await asyncio.sleep(0.01)
        started = time.perf_counter()
        await gen.publish("meta", {"route": "chat"})
        await gen.publish("done", {})
        await asyncio.sleep(0.05)
        waited = stamps[-1] - started
        await gen.finish()
        await asyncio.wait_for(reader, 2)
        return writes, waited

    writes, waited = asyncio.run(scenario())
    assert waited < 0.1, "meta/done were held for the token frame window"
    assert "".join(writes) == _frames([("token", {"text": "a"}), ("meta", {"route": "chat"}), ("done", {})])


def test_coalescing_off_writes_every_event_on_its_own(monkeypatch):
    monkeypatch.setattr(sse, "COALESCE_SECONDS", 0.0)

    async def scenario():
        gen = main.LiveGeneration("relay-3", None)
        writes: list = []
        reader = asyncio.ensure_future(_read_live(gen, writes, []))
        await asyncio.sleep(0.01)
        for i in range(5):
            await gen.publish("token", {"text": str(i)})
            await asyncio.sleep(0.002)
        await gen.finish()
        await asyncio.wait_for(reader, 2)
        return writes

    assert len(asyncio.run(scenario())) == 5


def test_the_coalescing_window_is_read_from_sse_coalesce_ms_with_blank_meaning_the_default(monkeypatch):
    monkeypatch.setenv("SSE_COALESCE_MS", "")
    assert sse._coalesce_seconds() == pytest.approx(0.025)
    monkeypatch.setenv("SSE_COALESCE_MS", "40")
    assert sse._coalesce_seconds() == pytest.approx(0.040)
    monkeypatch.setenv("SSE_COALESCE_MS", "0")
    assert sse._coalesce_seconds() == 0.0


def test_the_keep_alive_still_fires_while_a_generation_is_silent(monkeypatch):
    monkeypatch.setattr(main, "HEARTBEAT_SECONDS", 0.05)
    assert sse.HEARTBEAT_SECONDS == 15.0 or sse.HEARTBEAT_SECONDS > 0

    async def scenario():
        gen = main.LiveGeneration("relay-4", None)
        writes: list = []
        reader = asyncio.ensure_future(_read_live(gen, writes, []))
        await asyncio.sleep(0.18)
        await gen.finish()
        await asyncio.wait_for(reader, 2)
        return writes

    writes = asyncio.run(scenario())
    assert writes and all(w == sse.sse_comment() for w in writes)


# ---------------------------------------------------------------------------
# /chat: trace writes and the clarification cancel are off the first-token path
# ---------------------------------------------------------------------------


def _fake_stream(started: dict, pairs):
    async def fake(messages, **_kwargs):
        started.setdefault("at", time.perf_counter())
        for pair in pairs:
            yield pair

    return fake


def _no_router_calls(monkeypatch):
    """The default effort asks the orchestrate router; no test here may reach
    a model endpoint, so the plan is stubbed."""
    from app.engines import orchestrate

    async def plan(message, history, effort):
        return orchestrate.Plan(agent=False, search=False)

    monkeypatch.setattr(orchestrate, "decide", plan)


def test_slow_trace_writes_do_not_hold_the_engine_back_and_the_trace_is_complete_when_the_stream_ends(
    monkeypatch,
):
    real_append = db.append_query_trace_event
    real_start = db.start_query_trace

    def slow_append(*args, **kwargs):
        time.sleep(0.3)
        return real_append(*args, **kwargs)

    def slow_start(*args, **kwargs):
        time.sleep(0.3)
        return real_start(*args, **kwargs)

    monkeypatch.setattr(db, "append_query_trace_event", slow_append)
    monkeypatch.setattr(db, "start_query_trace", slow_start)
    monkeypatch.setattr(settings, "living_knowledge_enabled", False)
    monkeypatch.setattr(settings, "fact_extraction_enabled", False)
    started: dict = {}
    monkeypatch.setattr(llm, "stream_chat_events", _fake_stream(started, [("token", "hi")]))

    with TestClient(main.app) as client:
        sent = time.perf_counter()
        resp = client.post("/chat", json={"message": "hello", "mode": "assistant", "effort": "fast"})
        events = _parse_sse(resp.text)
        meta = next(data for kind, data in events if kind == "meta")
        trace = client.get(f"/chat/trace/{meta['trace_id']}")
    # Awaited inline, the root + REQUEST_RECEIVED + MODE_RESOLVED +
    # CONTEXT_ASSEMBLED writes held the engine for >= 1.2 s here.
    assert started["at"] - sent < 0.9, f"engine started {started['at'] - sent:.2f}s after the send"
    assert trace.status_code == 200, trace.text
    stages = [row["stage"] for row in trace.json()["events"]]
    assert stages[:3] == ["REQUEST_RECEIVED", "MODE_RESOLVED", "CONTEXT_ASSEMBLED"]
    assert "RESPONSE_GENERATED" in stages
    sequences = [row["sequence_number"] for row in trace.json()["events"]]
    assert sequences == sorted(sequences) == list(range(1, len(sequences) + 1))


def test_the_context_assembled_trace_carries_the_exact_meter_count_even_when_it_was_deferred(monkeypatch):
    from app import context

    async def count(base_url, model, messages):
        return 4242, 1_000_000

    monkeypatch.setattr(context, "count_tokens", count)
    base_url, _key, _model = llm.resolve_model_choice("smart")
    monkeypatch.setitem(context._window_cache, base_url, 1_000_000)
    monkeypatch.setattr(settings, "living_knowledge_enabled", False)
    monkeypatch.setattr(settings, "fact_extraction_enabled", False)
    monkeypatch.setattr(llm, "stream_chat_events", _fake_stream({}, [("token", "hi")]))

    with TestClient(main.app) as client:
        resp = client.post(
            "/chat",
            json={"message": "hello", "mode": "assistant", "effort": "fast", "conversation_id": "meter-1"},
        )
        meta = next(data for kind, data in _parse_sse(resp.text) if kind == "meta")
        trace = client.get(f"/chat/trace/{meta['trace_id']}").json()
    assert meta["context"]["tokens_used"] == 4242
    assembled = next(row for row in trace["events"] if row["stage"] == "CONTEXT_ASSEMBLED")
    assert assembled["details"]["context"]["tokens_used"] == 4242


def test_the_clarification_cancel_runs_beside_the_answer_and_finishes_before_the_turn(monkeypatch):
    from app.core.sf_intel import state as sf_intel_state

    done: dict = {}

    async def slow_cancel(conversation_id):
        await asyncio.sleep(0.8)
        done["at"] = time.perf_counter()
        return 0

    async def route_chat(message, has_image=False, history=()):
        return "chat"

    _no_router_calls(monkeypatch)

    monkeypatch.setattr(sf_intel_state, "cancel_pending", slow_cancel)
    monkeypatch.setattr(router_engine, "route_request", route_chat)
    monkeypatch.setattr(settings, "salesforce_intelligence_enabled", False)
    monkeypatch.setattr(settings, "clarify_before_answering", False)
    started: dict = {}
    monkeypatch.setattr(llm, "stream_chat_events", _fake_stream(started, [("token", "Hello!")]))

    with TestClient(main.app) as client:
        sent = time.perf_counter()
        resp = client.post("/chat", json={"message": "hey there"})
        ended = time.perf_counter()
    assert [kind for kind, _ in _parse_sse(resp.text)][-1] == "done"
    assert started["at"] - sent < 0.7, "the engine waited for the clarification cancel"
    assert "at" in done and done["at"] <= ended, "the cancel did not finish before the turn"


def test_a_follow_up_sent_the_instant_done_arrives_finds_no_stale_clarification(monkeypatch):
    # 2026-09-13, prover: the cancel became fire-and-forget and was awaited
    # only in the worker's `finally`, AFTER `done` — a fast follow-up could
    # still read the old card, and one pending card per conversation blocks
    # the follow-up's own. The cancel still runs beside the answer; `done`
    # now waits for it (bounded). Real rows, a slowed-down real UPDATE.
    from app.core.sf_intel import state as sf_state
    from app.core.sf_intel.models import ClarificationDraft, ClarificationOption

    conversation = "conv-follow-up-clarify"
    intent = sf_state.new_intent(conversation, text="show my pipeline", root_user_message_id="m1")
    asyncio.run(sf_state.save_intent(intent))
    draft = ClarificationDraft(
        slot="date_range",
        question="Which period should I use?",
        options=[ClarificationOption(id=f"o{i}", label=label, value=label.upper())
                 for i, label in enumerate(("This month", "This quarter"))],
    )
    assert asyncio.run(sf_state.open_clarification(intent, draft, run_id="r1")) is not None
    assert asyncio.run(sf_state.get_pending(conversation)) is not None

    real_cancel = db.cancel_sf_clarifications
    stamps: dict = {}

    def slow_cancel(conversation_id):
        time.sleep(0.6)
        cancelled = real_cancel(conversation_id)
        stamps["cancelled_at"] = time.perf_counter()
        return cancelled

    real_publish = main.LiveGeneration.publish

    async def publish(self, event, data):
        if event == "token":
            stamps.setdefault("token_at", time.perf_counter())
        if event == "done":
            # What the follow-up turn reads first (main.py answer_to_pending).
            stamps["pending_at_done"] = await sf_state.get_pending(conversation)
        await real_publish(self, event, data)

    async def route_chat(message, has_image=False, history=()):
        return "chat"

    _no_router_calls(monkeypatch)

    monkeypatch.setattr(db, "cancel_sf_clarifications", slow_cancel)
    monkeypatch.setattr(main.LiveGeneration, "publish", publish)
    monkeypatch.setattr(router_engine, "route_request", route_chat)
    monkeypatch.setattr(settings, "salesforce_intelligence_enabled", False)
    monkeypatch.setattr(settings, "clarify_before_answering", False)
    monkeypatch.setattr(settings, "living_knowledge_enabled", False)
    monkeypatch.setattr(llm, "stream_chat_events", _fake_stream({}, [("token", "Hello!")]))

    with TestClient(main.app) as client:
        resp = client.post("/chat", json={"message": "hey there", "conversation_id": conversation})
    assert [kind for kind, _ in _parse_sse(resp.text)][-1] == "done"
    assert "pending_at_done" in stamps
    assert stamps["pending_at_done"] is None, "done went out while the clarification was still pending"
    assert stamps["token_at"] < stamps["cancelled_at"], "the answer waited for the cancel"


def test_a_wedged_clarification_cancel_holds_done_only_for_its_bound(monkeypatch):
    from app.core.sf_intel import state as sf_intel_state

    async def wedged(conversation_id):
        await asyncio.sleep(30)
        return 0

    async def route_chat(message, has_image=False, history=()):
        return "chat"

    _no_router_calls(monkeypatch)

    monkeypatch.setattr(main, "_CLARIFICATION_CANCEL_BOUND_S", 0.2)
    monkeypatch.setattr(sf_intel_state, "cancel_pending", wedged)
    monkeypatch.setattr(router_engine, "route_request", route_chat)
    monkeypatch.setattr(settings, "salesforce_intelligence_enabled", False)
    monkeypatch.setattr(settings, "clarify_before_answering", False)
    monkeypatch.setattr(settings, "living_knowledge_enabled", False)
    monkeypatch.setattr(llm, "stream_chat_events", _fake_stream({}, [("token", "Hello!")]))

    with TestClient(main.app) as client:
        sent = time.perf_counter()
        resp = client.post("/chat", json={"message": "hey there"})
        ended = time.perf_counter()
    assert [kind for kind, _ in _parse_sse(resp.text)][-1] == "done"
    # Two bounds of 0.2 s (before `done`, and in `finally`) plus the turn;
    # unbounded, the 30 s sleep held the stream open for all of it.
    assert ended - sent < 3.0, f"a wedged cancel held the stream {ended - sent:.1f}s"


# ---------------------------------------------------------------------------
# The latency histograms (plan item 1, main.py half)
# ---------------------------------------------------------------------------


def _hist(name: str) -> dict:
    return dict(metrics._hists.get(name) or {})


def test_a_thinking_turn_reports_first_visible_as_reasoning_plus_assembly_decide_and_relay(monkeypatch):
    from app.engines import orchestrate

    async def plan(message, history, effort):
        return orchestrate.Plan(agent=False, search=False)

    metrics.reset()
    monkeypatch.setattr(orchestrate, "decide", plan)
    monkeypatch.setattr(settings, "living_knowledge_enabled", False)
    monkeypatch.setattr(settings, "fact_extraction_enabled", False)
    monkeypatch.setattr(
        llm, "stream_chat_events", _fake_stream({}, [("reasoning", "thinking…"), ("token", "Answer.")])
    )
    with TestClient(main.app) as client:
        resp = client.post("/chat", json={"message": "why is the sky blue", "mode": "assistant", "effort": "think"})
    assert [kind for kind, _ in _parse_sse(resp.text)][-1] == "done"

    first_visible = _hist("chat_first_visible_seconds")
    assert list(first_visible) == [(("effort", "think"), ("kind", "reasoning"), ("route", "chat"))]
    assert list(_hist("context_assembly_seconds")) == [(("effort", "think"), ("mode", "assistant"))]
    assert list(_hist("orchestrate_decide_seconds")) == [
        (("effort", "think"), ("outcome", "ok"), ("plan", "none"))
    ]
    relay = _hist("relay_overhead_seconds")
    assert list(relay) == [(("effort", "think"), ("route", "chat"))]
    _buckets, _total, count = relay[(("effort", "think"), ("route", "chat"))]
    assert count == 1, "relay overhead is observed once per generation"


def test_a_fast_turn_reports_its_decide_as_skipped_and_first_visible_as_the_answer(monkeypatch):
    metrics.reset()
    monkeypatch.setattr(settings, "living_knowledge_enabled", False)
    monkeypatch.setattr(settings, "fact_extraction_enabled", False)
    monkeypatch.setattr(llm, "stream_chat_events", _fake_stream({}, [("token", "Hi.")]))
    with TestClient(main.app) as client:
        client.post("/chat", json={"message": "hello", "mode": "assistant", "effort": "fast"})
    assert list(_hist("chat_first_visible_seconds")) == [(("effort", "fast"), ("kind", "answer"), ("route", "chat"))]
    assert list(_hist("orchestrate_decide_seconds")) == [
        (("effort", "fast"), ("outcome", "skipped"), ("plan", "none"))
    ]


def test_a_reattach_replaying_the_buffer_does_not_report_relay_overhead():
    metrics.reset()

    async def scenario():
        gen = main.LiveGeneration("relay-5", None)
        gen.effort = "fast"
        await gen.publish("token", {"text": "a"})
        await gen.publish("done", {})
        gen.final_meta = {"route": "chat"}
        await gen.finish()
        async for _ in gen.follow():
            pass
        gen.report_relay()

    asyncio.run(scenario())
    assert _hist("relay_overhead_seconds") == {}
