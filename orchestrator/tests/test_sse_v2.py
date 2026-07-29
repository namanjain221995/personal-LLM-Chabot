"""V2 SSE extension (V2-DESIGN §2): reasoning + step events, while the four
v1 frames (token/meta/done/error) stay byte-identical."""
import json

import pytest

from app.sse import (
    ALL_EVENTS,
    ALLOWED_EVENTS,
    PROGRESS_EVENTS,
    RESEARCH_EVENTS,
    V2_EVENTS,
    done_event,
    error_event,
    reasoning_event,
    sse_event,
    step_event,
    token_event,
)


def test_v1_events_unchanged_and_v2_added():
    assert set(ALLOWED_EVENTS) == {"token", "meta", "done", "error"}
    assert set(V2_EVENTS) == {"reasoning", "step"}
    # Phase 1 adds the transient "status" progress event; v1 set is untouched.
    assert set(PROGRESS_EVENTS) == {"status"}
    # The research panel adds its own event; the v1 set is still untouched.
    assert set(RESEARCH_EVENTS) == {"research"}
    assert set(ALL_EVENTS) == (
        set(ALLOWED_EVENTS)
        | set(V2_EVENTS)
        | set(PROGRESS_EVENTS)
        | set(RESEARCH_EVENTS)
    )


def test_v1_frames_byte_identical():
    assert token_event("hi") == 'event: token\ndata: {"text": "hi"}\n\n'
    assert done_event() == "event: done\ndata: {}\n\n"
    assert error_event("boom") == 'event: error\ndata: {"message": "boom"}\n\n'


def test_reasoning_frame_format():
    assert reasoning_event("mull") == 'event: reasoning\ndata: {"text": "mull"}\n\n'


def test_step_frame_format_without_detail():
    frame = step_event(1, "Query pipeline", "running")
    head, data_line, blank1, blank2 = frame.split("\n")
    assert head == "event: step"
    assert json.loads(data_line[len("data: "):]) == {
        "id": 1,
        "title": "Query pipeline",
        "status": "running",
    }
    assert blank1 == "" and blank2 == ""


def test_step_frame_includes_detail_when_given():
    frame = step_event(2, "Search records", "done", detail="8 record(s)")
    payload = json.loads(frame.split("\n")[1][len("data: "):])
    assert payload == {
        "id": 2,
        "title": "Search records",
        "status": "done",
        "detail": "8 record(s)",
    }


def test_step_rejects_unknown_status():
    with pytest.raises(ValueError):
        step_event(1, "x", "paused")


def test_unknown_event_still_rejected():
    with pytest.raises(ValueError):
        sse_event("progress", {})


def test_sse_event_accepts_v2_types():
    assert sse_event("reasoning", {"text": "t"}).startswith("event: reasoning\n")
    assert sse_event("step", {"id": 1}).startswith("event: step\n")
