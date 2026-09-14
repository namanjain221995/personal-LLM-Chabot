"""POST /v1/responses with stream=true (CONTRACT-3 §10)."""
from __future__ import annotations

import json

import pytest

from techsara_conformance import sse

PROMPT = "Name three primary colours, comma separated."
TERMINALS = {"response.completed", "response.failed", "error"}


def _collect(client, target, **extra):
    stream = client.responses.create(
        model=target.models["chat"], input=PROMPT, max_output_tokens=target.small_output_tokens, stream=True, **extra
    )
    with stream:
        return list(stream)


def test_a_streamed_response_emits_the_documented_event_order(client, target):
    events = _collect(client, target)
    types = [e.type for e in events]
    assert types[0] == "response.created", types
    allowed_before_progress = {"response.queued"}
    i = 1
    while i < len(types) and types[i] in allowed_before_progress:
        i += 1
    assert types[i] == "response.in_progress", types
    body = types[i + 1 :]
    deltas = [t for t in body if t == "response.output_text.delta"]
    assert deltas, f"no response.output_text.delta: {types}"
    assert types[-2] == "response.output_text.done", types
    assert types[-1] == "response.completed", types
    assert sum(t in TERMINALS for t in types) == 1, f"exactly one terminal event (§10): {types}"
    assert set(body[:-2]) == {"response.output_text.delta"}, f"unexpected events between in_progress and done: {types}"


def test_sequence_numbers_start_at_one_and_increase_by_exactly_one(client, target):
    numbers = [e.sequence_number for e in _collect(client, target)]
    assert numbers == list(range(1, len(numbers) + 1)), numbers


def test_usage_is_on_the_terminal_event_only(client, target):
    events = _collect(client, target)
    terminal = events[-1]
    assert terminal.type == "response.completed"
    usage = terminal.response.usage
    assert usage is not None and usage.output_tokens > 0 and usage.total_tokens == usage.input_tokens + usage.output_tokens
    for event in events[:-1]:
        snapshot = getattr(event, "response", None)
        if snapshot is not None:
            assert snapshot.usage is None, f"{event.type} carries usage; non-terminal events carry usage: null (§10)"


def test_the_deltas_add_up_to_the_done_text_and_the_final_output(client, target):
    events = _collect(client, target)
    streamed = "".join(e.delta for e in events if e.type == "response.output_text.delta")
    done = next(e for e in events if e.type == "response.output_text.done")
    assert streamed == done.text
    assert events[-1].response.output_text == done.text
    ids = {e.response.id for e in events if getattr(e, "response", None) is not None}
    assert len(ids) == 1, f"one response id across the stream: {ids}"


def test_the_raw_stream_is_uncached_event_stream_with_named_events_matching_their_type(raw, target):
    body = {"model": target.models["chat"], "input": PROMPT, "max_output_tokens": target.small_output_tokens, "stream": True}
    with raw.stream("POST", "responses", json=body) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        cache = response.headers.get("cache-control", "")
        assert "no-cache" in cache and "no-store" in cache, cache
        assert "content-length" not in response.headers
        assert response.headers.get("x-request-id")
        transcript = sse.read(response.iter_lines())
    assert transcript.events
    for event in transcript.events:
        payload = event.json()
        assert event.name == payload["type"], f"event: {event.name} but data.type {payload['type']}"


@pytest.mark.feature("output_ceiling")
def test_every_stream_snapshot_carries_max_output_tokens_and_incomplete_details(client, target):
    events = _collect(client, target)
    snapshots = [e for e in events if getattr(e, "response", None) is not None]
    for event in snapshots:
        wire = event.response.model_dump()
        assert "max_output_tokens" in wire and "incomplete_details" in wire, f"{event.type}: {sorted(wire)}"
    applied = snapshots[-1].response.model_dump()["max_output_tokens"]
    planned = snapshots[0].response.model_dump()["max_output_tokens"]
    assert isinstance(applied, int) and isinstance(planned, int) and applied <= planned, (planned, applied)
