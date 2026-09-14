"""The SSE grammar additions of the no-timeout wave (2026-09-13).

Three promises a client (or the gateway) depends on, pinned without a server:

* a stream can be CONTINUED by an emitter that did not open it — the router
  sends `response.created` from the generation's emitter and may have to
  send the terminal itself after a failed capacity wait — and the numbering
  and the forward-only lifecycle still hold;
* nothing this API frames ever sets an SSE `id:` or `retry:` field
  (openai-python's decoder breaks on an `id:` line followed by a comment);
* the capacity-wait note is a comment, so it costs no sequence number.
"""
from __future__ import annotations

import json

import pytest

from app.publicapi import errors, events


def _wire(status: str) -> dict:
    return {"id": "resp_x", "object": "response", "status": status, "output": [], "usage": None}


def test_a_resumed_emitter_numbers_its_first_event_after_the_last_one_sent():
    opened = events.SequencedEvents()
    first = opened.created(_wire("queued"))
    assert json.loads(first.split("data: ", 1)[1])["sequence_number"] == 1

    resumed = events.SequencedEvents.resume_from(1, events.RESPONSE_CREATED)
    frame = resumed.failed(_wire("failed"))

    record = events.parse_frames(first + frame)
    assert [r["event"] for r in record] == ["response.created", "response.failed"]
    assert [r["data"]["sequence_number"] for r in record] == [1, 2]
    assert resumed.finished


def test_a_resumed_emitter_still_refuses_to_go_backwards_or_open_twice():
    resumed = events.SequencedEvents.resume_from(3, events.RESPONSE_IN_PROGRESS)
    with pytest.raises(events.StreamProtocolError):
        resumed.created(_wire("queued"))
    with pytest.raises(events.StreamProtocolError):
        resumed.queued(_wire("queued"))
    delta = resumed.output_text_delta("hi")
    assert json.loads(delta.split("data: ", 1)[1])["sequence_number"] == 4


def test_a_stream_that_already_ended_cannot_be_resumed():
    for terminal in events.TERMINAL_EVENTS:
        with pytest.raises(events.StreamProtocolError):
            events.SequencedEvents.resume_from(5, terminal)
    with pytest.raises(events.StreamProtocolError):
        events.SequencedEvents.resume_from(0, events.RESPONSE_CREATED)
    with pytest.raises(events.StreamProtocolError):
        events.SequencedEvents.resume_from(2, "response.invented")


def test_the_queued_note_is_a_comment_that_consumes_no_sequence_number():
    emitter = events.SequencedEvents()
    stream = emitter.created(_wire("queued")) + events.queued_comment() + emitter.in_progress(_wire("in_progress"))
    assert events.queued_comment() == ": queued\n\n"
    assert [r["data"]["sequence_number"] for r in events.parse_frames(stream)] == [1, 2]


def test_no_frame_either_dialect_writes_ever_sets_an_id_or_retry_field():
    # Text that LOOKS like the fields, inside the payload: json.dumps escapes
    # the newline, so it can never become a line of its own.
    hostile = "ok\nid: 7\nretry: 1\n\n: ts-seq=9\n"
    emitter = events.SequencedEvents()
    responses = "".join(
        [
            emitter.created(_wire("queued")),
            emitter.heartbeat(),
            emitter.in_progress(_wire("in_progress")),
            emitter.output_text_delta(hostile),
            emitter.output_text_done(hostile),
            emitter.error(errors.model_unavailable(retry_after=30)),
        ]
    )
    chunks = events.ChatCompletionChunks(completion_id="chatcmpl_x", model="m", include_usage=True)
    chat = "".join(
        [
            chunks.heartbeat(),
            chunks.delta(hostile),
            chunks.error_chunk(errors.internal_error(hostile)),
            chunks.stop("stop"),
            chunks.usage_chunk(None),
            chunks.done(),
        ]
    )
    assert events.reserved_field_lines(responses) == []
    assert events.reserved_field_lines(chat) == []
    # And the checker itself sees a real one (so the two asserts above mean something).
    assert events.reserved_field_lines("id: 3\ndata: {}\n\n") == ["id: 3"]
    assert events.reserved_field_lines("retry: 10\n\n") == ["retry: 10"]
