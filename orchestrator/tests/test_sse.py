"""SSE frame formatting: exactly event: token|meta|done|error + data: JSON (§10)."""
import json

import pytest

from app.sse import ALLOWED_EVENTS, done_event, error_event, meta_event, sse_event, token_event


def test_allowed_events_exactly_four():
    assert set(ALLOWED_EVENTS) == {"token", "meta", "done", "error"}


def test_token_frame_format():
    frame = token_event("hi")
    assert frame == 'event: token\ndata: {"text": "hi"}\n\n'


def test_meta_frame_is_json():
    frame = meta_event({"route": "sql", "n": 1})
    head, data_line, blank1, blank2 = frame.split("\n")
    assert head == "event: meta"
    assert data_line.startswith("data: ")
    assert json.loads(data_line[len("data: "):]) == {"route": "sql", "n": 1}
    assert blank1 == "" and blank2 == ""


def test_done_and_error_frames():
    assert done_event().startswith("event: done\ndata: {}")
    frame = error_event("boom")
    assert frame == 'event: error\ndata: {"message": "boom"}\n\n'


def test_unknown_event_rejected():
    with pytest.raises(ValueError):
        sse_event("progress", {})


def test_non_serializable_values_fall_back_to_str():
    frame = sse_event("meta", {"when": object()})
    assert frame.startswith("event: meta\ndata: ")
