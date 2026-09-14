"""Streaming `file_citation` annotations (Files design §5.6; no-timeout
release gap 4, 2026-09-14).

A streamed answer about the caller's files announces each resolved citation
as `response.output_text.annotation.added` — after `response.output_text.done`,
before the part and the item close — and repeats the annotations on
`content_part.done`, `output_item.done` and the terminal Response. Chat
Completions carries them on the final chunk's `delta.annotations`.

On a DURABLE run the citation index travels with the spec, so a run that a
different process settles after a restart still annotates its answer: that
case is proven with two runtimes sharing only PostgreSQL.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, List

import pytest

from app import llm
from app.apifiles import citations
from app.publicapi import blobs, capacity, durable, durable_store, events, streaming
from tests.publicapi_fake_engine import FakeController, collect, fast_durable_settings, make_tenant, spec as make_spec

ANSWER = ["Revenue ", "rose 12% ", "[q3.pdf p.2]", " and ", "margins ", "held ", "[q3.pdf p.9]", "."]
TEXT = "".join(ANSWER)


def _index() -> citations.CitationIndex:
    index = citations.CitationIndex()
    index.register("q3.pdf", file_id="file-abc123", filename="q3.pdf", unit=citations.UNIT_PAGE)
    index.add_page("q3.pdf", 2)  # p.9 was never shown to the model
    index.register("calls.mp3", file_id="file-def456", filename="calls.mp3", unit=citations.UNIT_TIME)
    index.add_span("calls.mp3", 60.0, 90.0)
    index.register("sheet.xlsx", file_id="file-789", filename="sheet.xlsx", unit=citations.UNIT_ROWS)
    index.add_rows("sheet.xlsx", 1, 50, 1)
    return index


class TokenEngine:
    """`llm.stream_chat_events` answering ANSWER token by token; a
    continuation carries on after the tokens already in the final assistant
    message, as vLLM's `continue_final_message` does."""

    def __init__(self, delay_s: float = 0.0) -> None:
        self.delay_s = delay_s
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, messages, *, continue_final_message=False, max_tokens=None, **kwargs):
        start = 0
        if continue_final_message and messages and messages[-1].get("role") == "assistant":
            emitted = str(messages[-1].get("content") or "")
            while start < len(ANSWER) and emitted.startswith("".join(ANSWER[: start + 1])):
                start += 1
        self.calls.append({"start": start, "continue": continue_final_message})
        engine = self

        async def run():
            for token in ANSWER[start:]:
                if engine.delay_s:
                    await asyncio.sleep(engine.delay_s)
                yield ("token", token)
            llm._finish_reason.set("stop")
            llm._record_usage(21, len(ANSWER) - start)

        return run()


@pytest.fixture(autouse=True)
def _durable(monkeypatch, tmp_path):
    fast_durable_settings(monkeypatch)
    durable_store.ensure_schema()
    capacity.reset_for_tests()
    monkeypatch.setattr(blobs, "min_free_disk_bytes", lambda: 0)
    yield


async def _allow_all(keys):
    return set(keys)


def _runtime(tmp_path, owner: str) -> durable.Runtime:
    runtime = durable.Runtime()
    runtime.configure(
        owner=f"test-{owner}", view=FakeController(time.monotonic, state="READY"), authoriser=_allow_all,
        blob_store=blobs.BlobStore(tmp_path / "blobs"),
    )
    runtime.witnesses_enabled = False
    return runtime


EXPECTED_ANNOTATION = {"type": "file_citation", "file_id": "file-abc123", "filename": "q3.pdf",
                       "index": TEXT.index("[q3.pdf p.2]"), "page": 2}


def test_the_citation_index_survives_its_json_round_trip_and_resolves_the_same_annotations():
    index = _index()
    data = durable.citation_index_to_json(index)
    rebuilt = durable.citation_index_from_json(json.loads(json.dumps(data)))
    probe = TEXT + " [calls.mp3 1:10] [calls.mp3 9:00] [sheet.xlsx rows 10-12]"
    assert citations.annotate(probe, rebuilt).annotations == citations.annotate(probe, index).annotations
    assert len(citations.annotate(probe, index).annotations) == 3
    assert durable.citation_index_to_json(citations.CitationIndex()) is None


def _assert_annotated_grammar(names: List[str], datas: List[Dict[str, Any]]) -> None:
    done = names.index("response.output_text.done")
    assert names[done + 1:] == [
        "response.output_text.annotation.added", "response.content_part.done", "response.output_item.done",
        "response.completed",
    ]
    added = datas[done + 1]
    assert added["annotation"] == EXPECTED_ANNOTATION and added["annotation_index"] == 0
    assert added["item_id"] == datas[done]["item_id"] and added["content_index"] == 0
    assert datas[done + 2]["part"]["annotations"] == [EXPECTED_ANNOTATION]
    assert datas[done + 3]["item"]["content"][0]["annotations"] == [EXPECTED_ANNOTATION]
    assert datas[-1]["response"]["output"][0]["content"][0]["annotations"] == [EXPECTED_ANNOTATION]


def test_a_non_durable_stream_announces_each_resolved_citation_before_the_parts_close(monkeypatch):
    monkeypatch.setattr(llm, "stream_chat_events", TokenEngine())
    spec = make_spec(max_tokens=100)

    async def scenario() -> str:
        return "".join([frame async for frame in streaming.responses_sse(
            spec, annotate=lambda text: citations.annotate(text, _index()).annotations,
        )])

    records = events.parse_frames(asyncio.run(scenario()))
    names = [r["event"] for r in records]
    _assert_annotated_grammar(names, [r["data"] for r in records])
    assert [r["data"]["sequence_number"] for r in records] == list(range(1, len(records) + 1))


def test_a_chat_stream_carries_the_annotations_on_its_final_chunk(monkeypatch):
    monkeypatch.setattr(llm, "stream_chat_events", TokenEngine())

    async def scenario() -> str:
        return "".join([frame async for frame in streaming.chat_completions_sse(
            make_spec(max_tokens=100), completion_id="chatcmpl_x",
            annotate=lambda text: citations.annotate(text, _index()).annotations,
        )])

    chunks = [json.loads(line[6:]) for line in asyncio.run(scenario()).splitlines()
              if line.startswith("data: {")]
    final = next(c for c in chunks if c["choices"] and c["choices"][0]["finish_reason"])
    assert final["choices"][0]["delta"] == {"annotations": [EXPECTED_ANNOTATION]}
    assert all("annotations" not in c["choices"][0]["delta"] for c in chunks if c is not final and c["choices"])


def test_a_durable_run_settled_by_another_process_still_annotates_its_answer(monkeypatch, tmp_path):
    engine = TokenEngine(delay_s=0.05)
    monkeypatch.setattr(llm, "stream_chat_events", engine)
    tenant = make_tenant()
    extra = {durable.FILE_CITATIONS_KEY: durable.citation_index_to_json(_index())}

    async def scenario():
        a = _runtime(tmp_path, "A")
        await a.start()
        handle = await a.launch(make_spec(max_tokens=100), caller=tenant.caller(), streamed=True, extra=extra)
        seen = []
        try:
            async for item in handle.follow(0, heartbeat=0.05):
                if item is durable.HEARTBEAT:
                    continue
                seen.append(item)
                if sum(r[1] == events.RESPONSE_OUTPUT_TEXT_DELTA for r in seen) >= 2:
                    await a.suspend_all("restart")
        except durable.FollowerAborted:
            pass
        await a.stop()
        b = _runtime(tmp_path, "B")
        await b.start()
        try:
            everything = await collect(await b.attach(handle.id, caller=tenant.caller()), 0)
            chat = durable.ChatRenderer(completion_id="chatcmpl_y", model="techsara-35b", created=1, include_usage=False)
            rendered = "".join(chat.render(record) for record in everything)
            return everything, rendered
        finally:
            await b.stop()

    everything, rendered = asyncio.run(scenario())
    assert len(engine.calls) == 2 and engine.calls[1]["continue"] is True
    names = [r[1] for r in everything]
    datas = [durable.public_data(r[2]) for r in everything]
    assert "".join(d["delta"] for n, d in zip(names, datas) if n == events.RESPONSE_OUTPUT_TEXT_DELTA) == TEXT
    _assert_annotated_grammar(names, datas)
    final = [json.loads(line[6:]) for line in rendered.splitlines() if line.startswith("data: {")]
    assert final[-1]["choices"][0]["delta"] == {"annotations": [EXPECTED_ANNOTATION]}


def test_annotation_events_may_repeat_but_the_text_never_resumes_after_them_nor_they_after_the_part_closes():
    emitter = events.SequencedEvents(item_id="msg_1")
    emitter.created({"id": "r", "status": "queued", "output": []})
    emitter.output_item_added()
    emitter.content_part_added()
    emitter.output_text_delta("x")
    emitter.output_text_done("x")
    emitter.annotation_added(0, EXPECTED_ANNOTATION)
    emitter.annotation_added(1, EXPECTED_ANNOTATION)
    with pytest.raises(events.StreamProtocolError):
        emitter.output_text_delta("more")
    emitter.content_part_done("x", [EXPECTED_ANNOTATION])
    with pytest.raises(events.StreamProtocolError):
        emitter.annotation_added(2, EXPECTED_ANNOTATION)
