"""Citations on a streamed answer (2026-09-14, the Files API published).

A synchronous `/v1/responses` or `/v1/chat/completions` answer to a request
that names files carried its `file_citation` annotations; the same request
with `stream: true` silently dropped them, because
`response.output_text.annotation.added` was not in the stream grammar. A
client could not link a streamed answer back to the page it came from, and the
usage row counted citations the client never received.

Now the router hands the file run's citations to its stream launch, the
emitter numbers one event per annotation after `output_text.done`, the
terminal response carries them all, and Chat puts them on the finish chunk.
The scenarios run through the real public router with a memory file store and
a stub engine (tests/test_files_hookup.py's `files_world`).
"""
from __future__ import annotations

import asyncio
import json
import threading
from typing import Any, Dict, List

import pytest

from app import usage as usage_ledger
from app.apifiles import service
from app.publicapi import events, openapi as openapi_module, registry, streaming
from tests.test_files_hookup import _file_body, files_world  # noqa: F401 - fixture
from tests.test_publicapi_routes import TOKENS, _auth, _pepper, api, platform  # noqa: F401 - fixtures
from tests.test_publicapi_streaming import _FakeEngine, _frames, engine, measured  # noqa: F401 - fixtures


# ------------------------------------------------ citations in a stream --


def _sse_records(text: str) -> List[Dict[str, Any]]:
    return events.parse_frames(text)


def test_a_streamed_response_with_a_file_sends_its_citation_as_one_numbered_event_after_the_text_and_on_the_completed_response(
    api, files_world
):
    fid = files_world["file_id"]
    with api.stream("POST", "/v1/responses", headers=_auth(), json=_file_body(fid, stream=True)) as response:
        assert response.status_code == 200
        text = "".join(response.iter_text())
    records = _sse_records(text)
    names = [record["event"] for record in records]
    # Between the finished text and the terminal: exactly one annotation event
    # (the grammar may also close the content part and the item there, §10.2).
    after_text = names[names.index("response.output_text.done") + 1:]
    assert after_text[-1] == "response.completed"
    assert after_text.count("response.output_text.annotation.added") == 1
    assert after_text[0] == "response.output_text.annotation.added"
    assert names.count("response.output_text.annotation.added") == 1
    assert [record["data"]["sequence_number"] for record in records] == list(range(1, len(records) + 1))

    done = records[names.index("response.output_text.done")]["data"]
    added = records[names.index("response.output_text.annotation.added")]["data"]
    expected = {
        "type": "file_citation", "file_id": fid, "filename": "memo.txt",
        "index": done["text"].index("[memo.txt"), "page": 1,
    }
    assert added["annotation"] == expected
    assert (added["item_id"], added["output_index"], added["content_index"], added["annotation_index"]) == (
        done["item_id"], 0, 0, 0,
    )
    completed = records[-1]["data"]["response"]
    assert completed["output"][0]["content"][0]["annotations"] == [expected]


def test_a_streamed_chat_completion_with_a_file_carries_its_citations_on_the_finish_chunk(api, files_world):
    fid = files_world["file_id"]
    body = {"model": registry.TECHSARA_35B, "stream": True, "messages": [{"role": "user", "content": [
        {"type": "file", "file": {"file_id": fid}}, {"type": "text", "text": "What is the launch code?"}]}]}
    with api.stream("POST", "/v1/chat/completions", headers=_auth(), json=body) as response:
        assert response.status_code == 200
        text = "".join(response.iter_text())
    chunks = [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: {")]
    finish = [chunk for chunk in chunks if chunk["choices"] and chunk["choices"][0]["finish_reason"]]
    assert len(finish) == 1
    annotations = finish[0]["choices"][0]["delta"]["annotations"]
    content = "".join(chunk["choices"][0]["delta"].get("content", "") for chunk in chunks if chunk["choices"])
    assert annotations == [{
        "type": "file_citation", "file_id": fid, "filename": "memo.txt", "index": content.index("[memo.txt"), "page": 1,
    }]
    # Only the finish chunk carries them: a text delta never does.
    assert sum("annotations" in chunk["choices"][0]["delta"] for chunk in chunks if chunk["choices"]) == 1
    assert text.endswith(events.DONE_SENTINEL)


def test_the_streamed_citations_are_the_synchronous_bodys_and_the_usage_row_counts_them(api, files_world, monkeypatch):
    recorded: List[Dict[str, Any]] = []

    async def capture(**kwargs):
        recorded.append(kwargs)

    monkeypatch.setattr(usage_ledger, "record_async", capture)
    fid = files_world["file_id"]
    sync = api.post("/v1/responses", headers=_auth(), json=_file_body(fid))
    assert sync.status_code == 200, sync.text
    with api.stream("POST", "/v1/responses", headers=_auth(), json=_file_body(fid, stream=True)) as response:
        streamed = _sse_records("".join(response.iter_text()))
    assert streamed[-1]["data"]["response"]["output"][0]["content"][0]["annotations"] == (
        sync.json()["output"][0]["content"][0]["annotations"]
    )
    metas = [row["meta"] for row in recorded if row["route"] == "v1_responses"]
    assert len(metas) == 2
    assert [(meta["citations"], meta["citations_unresolved"]) for meta in metas] == [(1, 0), (1, 0)]


def test_citations_that_cannot_be_computed_leave_the_stream_completed_without_annotations(api, files_world, monkeypatch):
    def broken(text, prepared):
        raise RuntimeError("the citation parser fell over")

    monkeypatch.setattr(service, "annotate", broken)
    with api.stream("POST", "/v1/responses", headers=_auth(), json=_file_body(files_world["file_id"], stream=True)) as response:
        records = _sse_records("".join(response.iter_text()))
    names = [record["event"] for record in records]
    assert names[-1] == "response.completed"
    assert "response.output_text.annotation.added" not in names
    assert not records[-1]["data"]["response"]["output"][0]["content"][0].get("annotations")


def test_a_stream_without_files_never_sends_an_annotation_event_and_keeps_its_numbering(engine, measured):
    measured({"prompt_tokens": 3, "completion_tokens": 4})
    engine(_FakeEngine(["See [memo.txt §1]."]))
    records = events.parse_frames(_frames())
    names = [record["event"] for record in records]
    assert "response.output_text.annotation.added" not in names
    assert names[-1] == "response.completed"
    assert [record["data"]["sequence_number"] for record in records] == list(range(1, len(records) + 1))
    assert not records[-1]["data"]["response"]["output"][0]["content"][0].get("annotations")


def test_an_annotator_that_finds_nothing_sends_no_event_and_no_empty_list(engine, measured):
    measured({"prompt_tokens": 3, "completion_tokens": 4})
    engine(_FakeEngine(["Plain."]))
    records = events.parse_frames(_frames(annotate=lambda text: []))
    assert "response.output_text.annotation.added" not in [record["event"] for record in records]
    assert not records[-1]["data"]["response"]["output"][0]["content"][0].get("annotations")


def test_an_annotator_that_raises_still_ends_the_stream_completed_with_the_whole_text(engine, measured):
    """The stream's own guard, below `FileRun.note_output`'s: any annotator
    the launch is handed may fail, and a finished answer is never turned into
    a failed one by its citations."""
    measured({"prompt_tokens": 3, "completion_tokens": 4})
    engine(_FakeEngine(["Cited ", "[memo.txt §1]."]))

    def broken(text):
        raise RuntimeError("no citation index in this process")

    records = events.parse_frames(_frames(annotate=broken))
    assert records[-1]["event"] == "response.completed"
    assert records[-1]["data"]["response"]["output"][0]["content"][0]["text"] == "Cited [memo.txt §1]."
    assert "response.output_text.annotation.added" not in [record["event"] for record in records]


def test_a_very_long_answer_is_scanned_for_citations_off_the_event_loop(engine, measured, monkeypatch):
    monkeypatch.setattr(streaming, "ANNOTATE_OFF_LOOP_CHARS", 4)
    measured({"prompt_tokens": 3, "completion_tokens": 4})
    engine(_FakeEngine(["A long answer [memo.txt §1]."]))
    loop_thread = []
    scanned_on = []

    def annotate(text):
        scanned_on.append(threading.get_ident())
        return [{"type": "file_citation", "file_id": "file-" + "a" * 24, "filename": "memo.txt", "index": 14, "page": 1}]

    async def run():
        loop_thread.append(threading.get_ident())
        return "".join([frame async for frame in streaming.responses_sse(_spec_for_long(), annotate=annotate)])

    records = events.parse_frames(asyncio.run(run()))
    assert scanned_on and scanned_on[0] != loop_thread[0]
    assert "response.output_text.annotation.added" in [record["event"] for record in records]


def _spec_for_long() -> streaming.GenerationSpec:
    return streaming.GenerationSpec(
        response_id="resp_long", model="techsara-35b", messages=[{"role": "user", "content": "x"}],
        max_tokens=64, temperature=0.2, created_at=1789200000, item_id="msg_long",
    )


def test_the_emitter_refuses_an_annotation_before_the_text_is_final_or_after_the_terminal():
    annotation = {"type": "file_citation", "file_id": "file-" + "b" * 24, "filename": "a.txt", "index": 0, "page": 1}
    early = events.SequencedEvents(item_id="msg_1")
    early.created({"id": "resp_1", "status": "queued"})
    early.output_text_delta("hi")
    with pytest.raises(events.StreamProtocolError):
        early.annotation_added(0, annotation)

    late = events.SequencedEvents(item_id="msg_2")
    late.created({"id": "resp_2", "status": "queued"})
    late.output_text_done("hi [a.txt §1]")
    late.annotation_added(0, annotation)
    late.annotation_added(1, annotation)
    late.completed({"id": "resp_2", "status": "completed"})
    with pytest.raises(events.StreamProtocolError):
        late.annotation_added(2, annotation)
    assert late.sequence_number == 5



def test_the_stream_description_names_the_annotation_event_the_grammar_allows():
    description = openapi_module._stream_description()
    assert f"`{events.RESPONSE_OUTPUT_TEXT_ANNOTATION_ADDED}`" in description
