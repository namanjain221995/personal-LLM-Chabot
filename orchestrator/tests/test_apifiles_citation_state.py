"""The citation data a durable run stores, so an answer resumed in ANOTHER
process after a deploy annotates exactly as the live request would have
(`citations` module docstring, DURABLE RUNS; 2026-09-14)."""
from __future__ import annotations

import json
import math

import pytest

from app.apifiles import citations as cite
from app.apifiles import context as file_context
from app.apifiles import service
from tests.test_apifiles_citations import _index

ANSWER = (
    "\U0001F4C8 Revenue rose [q3-report.pdf p.842], not [q3-report.pdf p.843]; see [deck.pptx slide 4], "
    "[notes.docx §3], [data.xlsx rows 250-260] and the stand-up at [stand up.mp4 1:31:05]. "
    "Unsupplied: [stand up.mp4 1:30:54] [other.pdf p.1]."
)


def _stored(index: cite.CitationIndex) -> dict:
    """What a durable store hands back: the state through JSON text."""
    return json.loads(json.dumps(index.to_state()))


def test_a_citation_state_read_back_from_json_annotates_a_resumed_answer_exactly_like_the_live_index():
    live = cite.annotate(ANSWER, _index())
    resumed = cite.annotate_from_state(ANSWER, _stored(_index()))
    assert live.annotations and resumed.annotations == live.annotations
    assert (resumed.resolved, resumed.unresolved) == (live.resolved, live.unresolved) == (5, 3)
    assert {a["filename"] for a in resumed.annotations} == {"q3-report.pdf", "deck.pptx", "notes.docx", "data.xlsx", "stand up.mp4"}


def test_the_state_carries_labels_ids_and_supplied_pairs_but_never_file_text():
    index = _index()
    state = _stored(index)
    assert state["v"] == cite.STATE_VERSION
    report = next(f for f in state["files"] if f["label"] == "q3-report.pdf")
    assert report == {
        "label": "q3-report.pdf", "file_id": "file-" + "a" * 24, "filename": "q3-report.pdf", "unit": "page",
        "numbers": [[1, 1], [137, 137], [842, 842]], "row_blocks": [], "spans": [],
    }
    assert set(state) == {"v", "files"}


def test_a_resumed_stream_gets_its_annotation_events_right_after_output_text_done_and_in_sequence():
    resumed = cite.annotate_from_state(ANSWER, _stored(_index()))
    done_sequence = 41  # the stored `response.output_text.done`
    added = cite.annotation_added_events(
        resumed.annotations, item_id="msg_1", first_sequence_number=done_sequence + 1
    )
    assert [e["sequence_number"] for e in added] == list(range(42, 42 + len(resumed.annotations)))
    assert [e["annotation_index"] for e in added] == list(range(len(resumed.annotations)))
    assert all(e["type"] == "response.output_text.annotation.added" and e["item_id"] == "msg_1" for e in added)


@pytest.mark.parametrize("state", [None, {}, {"v": 2, "files": []}, "not a state", {"v": 1, "files": "x"}])
def test_a_lost_or_foreign_citation_state_annotates_nothing_counts_every_marker_unresolved_and_never_raises(state):
    got = cite.annotate_from_state(ANSWER, state)
    assert got.annotations == [] and got.resolved == 0 and got.unresolved == 8


def _one_file(**overrides) -> dict:
    item = {"label": "a.pdf", "file_id": "file-" + "a" * 24, "filename": "a.pdf", "unit": "page",
            "numbers": [[1, 1]], "row_blocks": [], "spans": []}
    item.update(overrides)
    return {"v": 1, "files": [item]}


@pytest.mark.parametrize("state", [
    _one_file(unit="chapter"),
    _one_file(label=""),
    _one_file(file_id=7),
    _one_file(numbers=[[True, 1]]),
    _one_file(numbers=[[1]]),
    _one_file(numbers=[["1", 1]]),
    _one_file(spans=[[math.nan, 1.0]]),
    _one_file(spans=[[0, 10**400]]),  # float() of it raises OverflowError, not ValueError (review, 2026-09-14)
    _one_file(unit="time", numbers=[], spans=[[-(10**400), 5]]),
    _one_file(row_blocks=[[1, 2]]),
    {"v": 1, "files": [_one_file()["files"][0], _one_file(filename="b.pdf")["files"][0]]},
    {"v": 1, "files": [_one_file()["files"][0]] * (cite.MAX_STATE_FILES + 1)},
])
def test_a_malformed_citation_state_is_refused_rather_than_turned_into_annotations(state):
    with pytest.raises(ValueError):
        cite.CitationIndex.from_state(state)
    assert cite.annotate_from_state("[a.pdf p.1]", state).annotations == []


def test_the_run_state_of_a_prepared_request_is_json_and_restores_its_citations_and_usage_meta():
    context = file_context.FileContext(
        system_addendum="cite pages", blocks={"k": [{"type": "text", "text": "SECRET FILE TEXT"}]}, citations=_index(),
        estimated_tokens=900, bounded_tokens=1200, image_count=0, mode_used={"file-a": "full"},
        meta={"file_ids": ["file-" + "a" * 24], "file_context_mode": "full", "file_context_tokens": 900,
              "retrieval_hits": 0, "rerank": "not_needed", "retrieval": "none", "frames": 0, "page_renders": 0, "images": 0},
    )
    model_input = service.ModelInput(
        context=context, lifted=None, files={}, audio_blocks={}, audio_seconds=0.0, files_wait_s=1.25,  # type: ignore[arg-type]
    )
    state = json.loads(json.dumps(service.citation_state(model_input)))
    assert "SECRET FILE TEXT" not in json.dumps(state)
    annotated = service.annotate_from_run_state(ANSWER, state)
    assert annotated.annotations == cite.annotate(ANSWER, _index()).annotations
    meta = service.usage_meta_from_run_state(state, annotated)
    assert meta["file_context_tokens"] == 900 and meta["files_wait_s"] == 1.25
    assert (meta["citations"], meta["citations_unresolved"]) == (5, 3)
    assert service.citation_state(None) is None
    assert service.annotate_from_run_state(ANSWER, {"v": 99}).annotations == []
    assert service.usage_meta_from_run_state({"v": 99}) == {}
