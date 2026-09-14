"""`GET /v1/files/{id}/events`: framing, throttling, heartbeats, the DB-poll
fallback, the one-terminal rule — and the in-process pub/sub under it.

The stream is driven by a scripted loader (what `schema.get_api_file` +
the File object renderer would return), with the intervals shrunk from
15 s / 5 s / 1 s to fractions of a second so the timing rules are observed
for real, not mocked. No database.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, List

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from app.apifiles import events
from app.publicapi import streaming


@pytest.fixture(scope="session")
def app_database():
    yield None


@pytest.fixture
def isolated_app_db():
    yield None


@pytest.fixture
def ambient_identity():
    yield None


@pytest.fixture(autouse=True)
def clean_bus():
    events.reset_for_tests()
    yield
    events.reset_for_tests()


def file_object(status: str = "uploaded", stage: str = "text", percent: int = 0, state: str = "processing") -> Dict[str, Any]:
    return {
        "id": "file-" + "a" * 24,
        "object": "file",
        "status": status,
        "processing": {"state": state, "stage": stage, "step": 2, "percent": percent, "stages": []},
    }


def parse(frames: List[str]) -> List[Dict[str, Any]]:
    out = []
    for frame in frames:
        if frame.startswith(":"):
            out.append({"comment": frame.strip()})
            continue
        lines = dict(line.split(": ", 1) for line in frame.strip().split("\n"))
        out.append({"event": lines["event"], **json.loads(lines["data"])})
    return out


async def collect(gen, limit_s: float = 10.0) -> List[str]:
    frames: List[str] = []
    deadline = time.monotonic() + limit_s
    async for frame in gen:
        frames.append(frame)
        if time.monotonic() > deadline:
            raise AssertionError("the stream did not end")
    return frames


# ================================================================ framing ==


def test_sequence_numbers_start_at_one_heartbeats_take_none_and_a_second_terminal_is_refused():
    framer = events.FileEventFramer()
    first = framer.frame(events.FILE_PROCESSING, file_object())
    beat = framer.heartbeat()
    second = framer.frame(events.FILE_PROCESSED, file_object("processed", state="processed"))
    parsed = parse([first, beat, second])
    assert [p.get("sequence_number") for p in parsed] == [1, None, 2]
    assert parsed[1] == {"comment": ": ping"}
    assert parsed[2]["type"] == "file.processed" and parsed[2]["data"]["status"] == "processed"
    with pytest.raises(events.StreamProtocolError):
        framer.frame(events.FILE_FAILED, file_object("error"))
    with pytest.raises(events.StreamProtocolError):
        events.FileEventFramer().frame("response.created", file_object())


# ================================================================= stream ==


def test_a_file_already_processed_gets_exactly_one_terminal_event_and_the_stream_ends():
    async def load():
        return file_object("processed", stage="finalize", percent=100, state="processed")

    frames = asyncio.run(collect(events.file_event_frames(load, blob_id="blob_x")))
    parsed = parse(frames)
    assert [p["event"] for p in parsed] == ["file.processed"]
    assert parsed[0]["sequence_number"] == 1


def test_a_failed_file_ends_with_file_failed():
    async def load():
        return file_object("error", state="failed")

    parsed = parse(asyncio.run(collect(events.file_event_frames(load))))
    assert [p["event"] for p in parsed] == ["file.failed"]


def test_rapid_progress_is_coalesced_to_one_event_per_throttle_window_then_one_terminal():
    blob_id = "blob_" + "1" * 24
    state = {"obj": file_object(percent=0)}

    async def load():
        return dict(state["obj"])

    async def driver():
        gen = events.file_event_frames(load, blob_id=blob_id, heartbeat_s=5, poll_s=5, throttle_s=0.5)
        task = asyncio.ensure_future(collect(gen))
        await asyncio.sleep(0.05)
        for pct in range(10, 60, 10):  # five changes inside one throttle window
            state["obj"] = file_object(percent=pct)
            events.publish(blob_id, {"stage": "text", "percent": pct})
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.8)
        state["obj"] = file_object("processed", stage="finalize", percent=100, state="processed")
        events.publish(blob_id, {"status": "processed"})
        return await task

    started = time.monotonic()
    parsed = parse(asyncio.run(driver()))
    assert time.monotonic() - started < 4  # woken by the pub/sub, not the 5 s poll
    names = [p["event"] for p in parsed]
    assert names == ["file.processing", "file.processing", "file.processed"]
    assert [p["data"]["processing"]["percent"] for p in parsed[:2]] == [0, 50]
    assert [p["sequence_number"] for p in parsed] == [1, 2, 3]
    assert events.subscriber_count(blob_id) == 0


def test_a_quiet_stream_sends_a_ping_comment_every_heartbeat_interval_and_never_numbers_it():
    state = {"obj": file_object()}

    async def load():
        return dict(state["obj"])

    async def driver():
        task = asyncio.ensure_future(collect(events.file_event_frames(load, heartbeat_s=0.25, poll_s=1.5, throttle_s=0.1)))
        await asyncio.sleep(1.1)
        state["obj"] = file_object("processed", state="processed")
        return await asyncio.wait_for(task, timeout=15)

    frames = asyncio.run(driver())
    parsed = parse(frames)
    pings = [p for p in parsed if "comment" in p]
    numbered = [p["sequence_number"] for p in parsed if "sequence_number" in p]
    assert len(pings) >= 3
    assert numbered == list(range(1, len(numbered) + 1))
    assert parsed[-1]["event"] == "file.processed"


def test_progress_from_another_process_arrives_through_the_poll_without_any_publish():
    state = {"obj": file_object(stage="sniff")}

    async def load():
        return dict(state["obj"])

    async def driver():
        task = asyncio.ensure_future(collect(events.file_event_frames(load, heartbeat_s=5, poll_s=0.2, throttle_s=0.05)))
        await asyncio.sleep(0.1)
        state["obj"] = file_object(stage="ocr", percent=40)
        await asyncio.sleep(0.5)
        state["obj"] = file_object("processed", state="processed")
        return await asyncio.wait_for(task, timeout=5)

    parsed = parse(asyncio.run(driver()))
    stages = [p["data"]["processing"]["stage"] for p in parsed if p.get("event") == "file.processing"]
    assert stages == ["sniff", "ocr"]
    assert parsed[-1]["event"] == "file.processed"


def test_a_file_deleted_mid_stream_ends_it_with_file_failed_carrying_status_deleted():
    calls = {"n": 0}

    async def load():
        calls["n"] += 1
        return file_object(percent=10) if calls["n"] == 1 else None

    parsed = parse(asyncio.run(collect(events.file_event_frames(load, poll_s=0.05, throttle_s=0.01))))
    assert [p["event"] for p in parsed] == ["file.processing", "file.failed"]
    assert parsed[-1]["data"]["status"] == "deleted" and parsed[-1]["data"]["id"].startswith("file-")


def test_a_client_that_disconnects_ends_the_stream_without_a_terminal_event():
    state = {"gone": False}

    async def load():
        return file_object()

    async def is_disconnected():
        return state["gone"]

    async def driver():
        task = asyncio.ensure_future(
            collect(events.file_event_frames(load, poll_s=0.05, heartbeat_s=5, throttle_s=0.01, is_disconnected=is_disconnected))
        )
        await asyncio.sleep(0.2)
        state["gone"] = True
        return await asyncio.wait_for(task, timeout=5)

    parsed = parse(asyncio.run(driver()))
    assert [p["event"] for p in parsed] == ["file.processing"]


def test_a_file_that_gains_its_blob_while_streaming_starts_hearing_that_blobs_progress():
    blob_id = "blob_" + "2" * 24
    state = {"obj": {**file_object(stage="assemble")}}

    async def load():
        return dict(state["obj"])

    async def driver():
        task = asyncio.ensure_future(collect(events.file_event_frames(load, poll_s=0.2, heartbeat_s=5, throttle_s=0.01)))
        await asyncio.sleep(0.05)
        state["obj"] = {**file_object(stage="sniff"), "_blob_id": blob_id}
        await asyncio.sleep(0.4)
        assert events.subscriber_count(blob_id) == 1
        state["obj"] = {**file_object("processed", state="processed"), "_blob_id": blob_id}
        events.publish(blob_id, {"status": "processed"})
        return await asyncio.wait_for(task, timeout=5)

    parsed = parse(asyncio.run(driver()))
    assert parsed[-1]["event"] == "file.processed"
    assert all("_blob_id" not in p.get("data", {}) for p in parsed)


def test_a_job_publishing_twenty_times_a_second_costs_one_file_load_per_throttle_window_not_one_per_event():
    # The review measured 401 loads in 20.1 s for one stream at 20 publishes/s.
    blob_id = "blob_" + "3" * 24
    state = {"obj": file_object(percent=0), "loads": 0}

    async def load():
        state["loads"] += 1
        return dict(state["obj"])

    async def driver():
        gen = events.file_event_frames(load, blob_id=blob_id, heartbeat_s=5, poll_s=5, throttle_s=0.25)
        task = asyncio.ensure_future(collect(gen))
        await asyncio.sleep(0.05)
        started = time.monotonic()
        tick = 0
        while time.monotonic() - started < 2.0:  # 2 s at 20 publishes per second
            tick += 1
            state["obj"] = file_object(percent=tick % 100)
            events.publish(blob_id, {"stage": "text", "percent": tick})
            await asyncio.sleep(0.05)
        loads_during = state["loads"]
        state["obj"] = file_object("processed", stage="finalize", percent=100, state="processed")
        events.publish(blob_id, {"status": "processed"})
        return await asyncio.wait_for(task, timeout=5), tick, loads_during

    frames, published, loads = asyncio.run(driver())
    assert published >= 30
    # One load at the start plus at most one per 0.25 s window: 2 s → ≤ 9-10.
    assert loads <= 11, loads
    assert parse(frames)[-1]["event"] == "file.processed"


def test_a_loader_that_raises_or_stalls_keeps_the_stream_alive_with_pings_until_it_recovers():
    state = {"n": 0}

    async def load():
        state["n"] += 1
        if state["n"] == 2:
            await asyncio.sleep(30)  # a stalled database round trip
        if state["n"] in (3, 4):
            raise ConnectionError("pool exhausted")
        if state["n"] >= 6:
            return file_object("processed", state="processed")
        return file_object(percent=10)

    async def driver():
        gen = events.file_event_frames(load, heartbeat_s=0.15, poll_s=0.3, throttle_s=0.05, load_timeout_s=0.4)
        return await asyncio.wait_for(collect(gen), timeout=10)

    parsed = parse(asyncio.run(driver()))
    assert parsed[0]["event"] == "file.processing"
    assert any("comment" in p for p in parsed)  # heartbeats flowed through the failures
    assert parsed[-1]["event"] == "file.processed"
    assert [p["sequence_number"] for p in parsed if "sequence_number" in p] == [1, 2]


def test_the_deleted_terminal_frame_carries_no_private_key_and_says_processing_failed():
    calls = {"n": 0}

    async def load():
        calls["n"] += 1
        if calls["n"] == 1:
            return {**file_object(percent=10), "_blob_id": "blob_" + "b" * 24}
        return None

    parsed = parse(asyncio.run(collect(events.file_event_frames(load, poll_s=0.05, throttle_s=0.01))))
    gone = parsed[-1]
    assert gone["event"] == "file.failed" and gone["data"]["status"] == "deleted"
    assert "_blob_id" not in gone["data"] and "blob_" not in json.dumps(gone)
    assert gone["data"]["processing"]["state"] == "failed"


# ============================================================ pub / sub ==


def test_publish_never_blocks_on_a_subscriber_that_stopped_reading_and_keeps_the_newest_events():
    async def scenario():
        with events.subscription("blob_z") as queue:
            for n in range(events.SUBSCRIBER_QUEUE * 3):
                events.publish("blob_z", {"n": n})
            kept = [queue.get_nowait()["n"] for _ in range(queue.qsize())]
        return kept

    kept = asyncio.run(scenario())
    assert len(kept) == events.SUBSCRIBER_QUEUE
    assert kept[-1] == events.SUBSCRIBER_QUEUE * 3 - 1
    assert events.subscriber_count("blob_z") == 0


def test_progress_row_writes_pass_at_most_once_per_interval_unless_forced():
    now = {"t": 100.0}
    throttle = events.ProgressThrottle(interval_s=2.0, clock=lambda: now["t"])
    decisions = []
    for step in range(6):
        decisions.append(throttle.should_write("b"))
        now["t"] += 0.5
    assert decisions == [True, False, False, False, True, False]
    assert throttle.should_write("b", force=True) is True
    assert throttle.should_write("other") is True


def test_the_route_helper_streams_text_event_stream_with_the_contract_headers():
    blob_id = "blob_" + "3" * 24
    file_row = {"id": "file-" + "b" * 24, "project_id": "proj_" + "c" * 24, "blob_id": blob_id}
    rows = [
        {**file_row, "n": 1},
        {**file_row, "n": 2, "done": True},
    ]

    def render(row):
        if row.get("done"):
            return file_object("processed", state="processed")
        return file_object(percent=10)

    async def load_row(project_id, file_id):
        assert (project_id, file_id) == (file_row["project_id"], file_row["id"])
        return rows.pop(0) if len(rows) > 1 else rows[0]

    async def endpoint(request):
        return await events.stream_file_events(request, None, file_row, render=render, load_row=load_row)

    async def shrink(*args, **kwargs):  # keep the real generator, faster poll
        kwargs.update(poll_s=0.05, throttle_s=0.01)
        async for frame in real(*args, **kwargs):
            yield frame

    real = events.file_event_frames
    events.file_event_frames = shrink  # type: ignore[assignment]
    try:
        client = TestClient(Starlette(routes=[Route("/events", endpoint)]))
        with client.stream("GET", "/events") as response:
            body = "".join(response.iter_text())
            headers = response.headers
    finally:
        events.file_event_frames = real  # type: ignore[assignment]
    assert headers["content-type"].startswith("text/event-stream")
    for name, value in streaming.SSE_HEADERS.items():
        if name.lower() != "connection":
            assert headers[name.lower()] == value
    parsed = parse([f + "\n\n" for f in body.strip().split("\n\n")])
    assert [p["event"] for p in parsed] == ["file.processing", "file.processed"]
