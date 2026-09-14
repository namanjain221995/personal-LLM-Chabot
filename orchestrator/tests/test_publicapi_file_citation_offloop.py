"""Two properties of streamed `file_citation` annotations that the event
grammar does not give by itself (2026-09-14).

1. An annotation only ever follows `response.output_text.done` (or another
   annotation). The rank table lets a stage be skipped, and an annotation's
   `index` counts into a final text that does not exist before `done`.
2. A long answer is scanned for its citation labels on a worker thread, once.
   A million-token answer is megabytes of text (216-314 ms for 4 MiB measured
   on Python 3.11), and on the event loop that is a stall of every other
   stream. The stream, the durable settle and the usage row's counts
   (`FileRun.wrap_finish`) all go through the off-loop path, and the file run
   shares one scan between them.
"""
from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from app.publicapi import durable, events, file_inputs, streaming
from tests.test_publicapi_streaming import _FakeEngine, _spec, engine, measured  # noqa: F401 - fixtures

ANNOTATION = {"type": "file_citation", "file_id": "file-" + "a" * 24, "filename": "memo.txt", "index": 14, "page": 1}


# ------------------------------------------------ where an annotation may go --


def test_the_emitter_refuses_an_annotation_before_the_text_is_final_or_after_the_terminal():
    after_delta = events.SequencedEvents(item_id="msg_1")
    after_delta.created({"id": "resp_1", "status": "queued"})
    after_delta.output_item_added()
    after_delta.content_part_added()
    after_delta.output_text_delta("hi")
    with pytest.raises(events.StreamProtocolError):
        after_delta.annotation_added(0, ANNOTATION)

    after_part = events.SequencedEvents(item_id="msg_2")
    after_part.created({"id": "resp_2", "status": "queued"})
    after_part.output_item_added()
    after_part.content_part_added()
    with pytest.raises(events.StreamProtocolError):
        after_part.annotation_added(0, ANNOTATION)

    whole = events.SequencedEvents(item_id="msg_3")
    whole.created({"id": "resp_3", "status": "queued"})
    whole.output_item_added()
    whole.content_part_added()
    whole.output_text_done("hi [memo.txt §1]")
    whole.annotation_added(0, ANNOTATION)
    whole.annotation_added(1, ANNOTATION)
    whole.content_part_done("hi [memo.txt §1]", [ANNOTATION, ANNOTATION])
    with pytest.raises(events.StreamProtocolError):
        whole.annotation_added(2, ANNOTATION)
    whole.output_item_done("hi [memo.txt §1]", [ANNOTATION, ANNOTATION])
    whole.completed({"id": "resp_3", "status": "completed"})
    with pytest.raises(events.StreamProtocolError):
        whole.annotation_added(2, ANNOTATION)
    assert whole.sequence_number == 9


# ------------------------------------------------------ the scan off the loop --


def _scanner(seen: List[int]):
    def annotate(text: str) -> List[Dict[str, Any]]:
        seen.append(threading.get_ident())
        return [dict(ANNOTATION)]

    return annotate


def test_a_long_streamed_response_is_scanned_off_the_event_loop_and_a_short_one_on_it(engine, measured, monkeypatch):
    monkeypatch.setattr(streaming, "ANNOTATE_OFF_LOOP_CHARS", 20)
    measured({"prompt_tokens": 3, "completion_tokens": 4})

    async def run(pieces: List[str], seen: List[int]) -> List[Dict[str, Any]]:
        engine(_FakeEngine(pieces))
        frames = "".join([f async for f in streaming.responses_sse(_spec(), annotate=_scanner(seen))])
        return events.parse_frames(frames)

    async def main():
        loop_thread = threading.get_ident()
        long_seen: List[int] = []
        records = await run(["A long answer ", "[memo.txt §1]."], long_seen)
        short_seen: List[int] = []
        await run(["Short."], short_seen)
        return loop_thread, long_seen, short_seen, records

    loop_thread, long_seen, short_seen, records = asyncio.run(main())
    assert long_seen and long_seen[0] != loop_thread
    assert short_seen == [loop_thread]
    names = [r["event"] for r in records]
    assert names.count(events.RESPONSE_OUTPUT_TEXT_ANNOTATION_ADDED) == 1
    assert names[names.index(events.RESPONSE_OUTPUT_TEXT_DONE) + 1] == events.RESPONSE_OUTPUT_TEXT_ANNOTATION_ADDED


def test_a_long_streamed_chat_completion_is_scanned_off_the_event_loop(engine, measured, monkeypatch):
    monkeypatch.setattr(streaming, "ANNOTATE_OFF_LOOP_CHARS", 20)
    measured({"prompt_tokens": 3, "completion_tokens": 4})
    engine(_FakeEngine(["A long answer ", "[memo.txt §1]."]))
    seen: List[int] = []

    async def main():
        frames = [f async for f in streaming.chat_completions_sse(_spec(), completion_id="chatcmpl-x", annotate=_scanner(seen))]
        return threading.get_ident(), "".join(frames)

    loop_thread, body = asyncio.run(main())
    assert seen and seen[0] != loop_thread
    assert '"annotations"' in body


def test_the_durable_settle_scans_a_long_answer_off_the_event_loop(monkeypatch):
    monkeypatch.setattr(streaming, "ANNOTATE_OFF_LOOP_CHARS", 20)
    seen: List[int] = []

    def fake(extra, text):
        seen.append(threading.get_ident())
        return [dict(ANNOTATION)]

    monkeypatch.setattr(durable, "file_annotations", fake)

    async def main():
        loop_thread = threading.get_ident()
        long = await durable.file_annotations_off_loop({}, "x" * 20)
        short = await durable.file_annotations_off_loop({}, "x" * 19)
        return loop_thread, long, short

    loop_thread, long, short = asyncio.run(main())
    assert long == short == [ANNOTATION]
    assert seen[0] != loop_thread and seen[1] == loop_thread


def test_an_async_annotator_that_raises_is_no_annotations_and_never_a_failed_answer(engine, measured):
    measured({"prompt_tokens": 3, "completion_tokens": 4})
    engine(_FakeEngine(["Cited ", "[memo.txt §1]."]))

    async def broken(text: str):
        raise RuntimeError("no citation index in this process")

    async def main():
        return "".join([f async for f in streaming.responses_sse(_spec(), annotate=broken)])

    records = events.parse_frames(asyncio.run(main()))
    assert records[-1]["event"] == events.RESPONSE_COMPLETED
    assert events.RESPONSE_OUTPUT_TEXT_ANNOTATION_ADDED not in [r["event"] for r in records]


# -------------------------------------------------- one scan per file run --


def _file_run(monkeypatch, scans: List[int], delay_s: float = 0.0) -> file_inputs.FileRun:
    def annotate(text, prepared):
        scans.append(threading.get_ident())
        time.sleep(delay_s)
        return SimpleNamespace(annotations=[dict(ANNOTATION)])

    monkeypatch.setattr(file_inputs, "_service", lambda: SimpleNamespace(annotate=annotate))
    run = file_inputs.FileRun(None, caller=None, plan=None, request_id="req_scan", caller_messages=[])  # type: ignore[arg-type]
    run.prepared = object()
    return run


def test_a_file_run_scans_a_long_answer_once_off_the_loop_for_the_stream_and_the_usage_row_together(monkeypatch):
    monkeypatch.setattr(streaming, "ANNOTATE_OFF_LOOP_CHARS", 20)
    scans: List[int] = []
    run = _file_run(monkeypatch, scans, delay_s=0.2)
    recorded: List[Any] = []

    async def recorder(outcome):
        recorded.append(run.annotated)

    finish = run.wrap_finish(recorder)
    text = "A long answer [memo.txt §1]."

    async def main():
        # The stream's scan is running on its thread when the client leaves
        # and the recorder runs: both must wait on the one scan.
        stream_scan = asyncio.ensure_future(run.note_output_off_loop(text))
        await asyncio.sleep(0.05)
        await finish(streaming.StreamOutcome(response_id="resp_scan", model="m", created_at=1, text=text))
        return threading.get_ident(), await stream_scan

    loop_thread, annotated = asyncio.run(main())
    assert len(scans) == 1 and scans[0] != loop_thread
    assert annotated.annotations == [ANNOTATION]
    assert recorded and recorded[0] is annotated
    # A body built afterwards reads the cached result, with no further scan.
    assert run.response_wire({"output": [{"content": [{"type": "output_text", "text": text}]}]}, text)
    assert len(scans) == 1


def test_a_file_run_scans_a_short_answer_on_the_loop_as_before(monkeypatch):
    scans: List[int] = []
    run = _file_run(monkeypatch, scans)

    async def main():
        return threading.get_ident(), await run.note_output_off_loop("Short [memo.txt §1].")

    loop_thread, annotated = asyncio.run(main())
    assert scans == [loop_thread] and annotated.annotations == [ANNOTATION]
