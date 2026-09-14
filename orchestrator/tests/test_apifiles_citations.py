"""Files as model input — citations back into the document (design §5.6)."""
from __future__ import annotations

from app.apifiles import citations as cite


def _index() -> cite.CitationIndex:
    index = cite.CitationIndex()
    index.register("q3-report.pdf", file_id="file-" + "a" * 24, filename="q3-report.pdf", unit=cite.UNIT_PAGE)
    for page in (1, 137, 842):
        index.add_page("q3-report.pdf", page)
    index.register("deck.pptx", file_id="file-" + "b" * 24, filename="deck.pptx", unit=cite.UNIT_SLIDE)
    index.add_page("deck.pptx", 4)
    index.register("notes.docx", file_id="file-" + "c" * 24, filename="notes.docx", unit=cite.UNIT_SECTION)
    index.add_page("notes.docx", 3)
    index.register("data.xlsx", file_id="file-" + "d" * 24, filename="data.xlsx", unit=cite.UNIT_ROWS)
    index.add_rows("data.xlsx", 201, 400, 2)
    index.register("stand up.mp4", file_id="file-" + "e" * 24, filename="stand up.mp4", unit=cite.UNIT_TIME)
    index.add_span("stand up.mp4", 5460.0, 5520.0)
    index.add_span("stand up.mp4", 8045.0)
    return index


def test_a_marker_becomes_an_annotation_only_when_its_page_was_supplied():
    text = "Revenue rose 12% [q3-report.pdf p.842]. Costs fell [q3-report.pdf p.843]. See [other.pdf p.1]."
    got = cite.annotate(text, _index())
    assert got.annotations == [
        {"type": "file_citation", "file_id": "file-" + "a" * 24, "filename": "q3-report.pdf",
         "index": text.index("[q3-report.pdf p.842]"), "page": 842}
    ]
    assert (got.resolved, got.unresolved) == (1, 2)
    assert got.meta() == {"citations": 1, "citations_unresolved": 2}


def test_index_is_a_utf16_offset_so_astral_characters_before_a_marker_count_twice():
    text = "\U0001F4C8 收入 rose [q3-report.pdf p.137]"
    got = cite.annotate(text, _index())
    python_index = text.index("[")
    assert got.annotations[0]["index"] == python_index + 1, "one emoji is two UTF-16 code units"
    assert text.encode("utf-16-le")[: 2 * got.annotations[0]["index"]].decode("utf-16-le") == text[:python_index]


def test_slides_sections_and_row_ranges_resolve_by_their_own_units():
    text = (
        "[deck.pptx slide 4] [deck.pptx p.4] [deck.pptx slide 5] [notes.docx §3] [notes.docx §4] "
        "[data.xlsx rows 250-260] [data.xlsx rows 401-500] [q3-report.pdf slide 1]"
    )
    got = cite.annotate(text, _index())
    pages = [(a["filename"], a["page"]) for a in got.annotations]
    assert pages == [("deck.pptx", 4), ("deck.pptx", 4), ("notes.docx", 3), ("data.xlsx", 2)]
    assert got.unresolved == 4, "slide 5, §4, rows beyond the block and a slide marker on a pdf stay text"


def test_media_timestamps_resolve_within_five_seconds_of_a_supplied_span_and_carry_timestamp_s():
    text = "[stand up.mp4 1:31:05] [stand up.mp4 2:14:10] [stand up.mp4 2:14:11] [stand up.mp4 1:30:54] [stand up.mp4 1:30:55]"
    got = cite.annotate(text, _index())
    assert [a["timestamp_s"] for a in got.annotations] == [5465.0, 8050.0, 5455.0], "1:30:55 is 5 s before the span; 1:30:54 is 6 s"
    assert got.unresolved == 2
    assert all("page" not in a for a in got.annotations)


def test_a_resolved_marker_without_a_file_id_is_counted_but_not_annotated():
    index = cite.CitationIndex()
    index.register("pasted.txt", file_id=None, filename="pasted.txt", unit=cite.UNIT_SECTION)
    index.add_page("pasted.txt", 1)
    got = cite.annotate("As written [pasted.txt §1].", index)
    assert got.annotations == [] and got.resolved == 1 and got.unresolved == 0


def test_marker_text_round_trips_through_the_scanner_for_every_unit():
    index = _index()
    for label, unit, value in (
        ("q3-report.pdf", cite.UNIT_PAGE, 137),
        ("deck.pptx", cite.UNIT_SLIDE, 4),
        ("notes.docx", cite.UNIT_SECTION, 3),
        ("data.xlsx", cite.UNIT_ROWS, (201, 400)),
        ("stand up.mp4", cite.UNIT_TIME, 8045.0),
    ):
        assert len(cite.annotate(cite.marker(label, unit, value), index).annotations) == 1, label
    assert cite.fmt_ts(59) == "0:59" and cite.fmt_ts(3600) == "1:00:00" and cite.fmt_ts(8045.4) == "2:14:05"


def test_the_stream_gets_annotation_added_events_before_the_terminal_event_with_renumbered_sequences():
    annotations = cite.annotate("x [q3-report.pdf p.842] y [q3-report.pdf p.137]", _index()).annotations
    events = [
        {"type": "response.created", "sequence_number": 0},
        {"type": "response.output_text.delta", "sequence_number": 1, "item_id": "msg_1", "delta": "x"},
        {"type": "response.output_text.done", "sequence_number": 2, "item_id": "msg_1", "output_index": 0, "content_index": 0},
        {"type": "response.content_part.done", "sequence_number": 3, "item_id": "msg_1", "output_index": 0, "content_index": 0,
         "part": {"type": "output_text", "text": "x", "annotations": []}},
        {"type": "response.output_item.done", "sequence_number": 4, "output_index": 0,
         "item": {"id": "msg_1", "type": "message", "content": [{"type": "output_text", "text": "x", "annotations": []}]}},
        {"type": "response.completed", "sequence_number": 5,
         "response": {"output": [{"type": "message", "content": [{"type": "output_text", "text": "x", "annotations": []}]}]}},
    ]
    out = cite.splice_annotation_events(events, annotations)
    types = [e["type"] for e in out]
    assert types == [
        "response.created", "response.output_text.delta", "response.output_text.done",
        "response.output_text.annotation.added", "response.output_text.annotation.added",
        "response.content_part.done", "response.output_item.done", "response.completed",
    ]
    assert [e["sequence_number"] for e in out] == list(range(8))
    added = [e for e in out if e["type"] == "response.output_text.annotation.added"]
    assert [e["annotation_index"] for e in added] == [0, 1]
    assert added[0]["item_id"] == "msg_1" and added[0]["annotation"]["page"] == 842
    assert out[-1]["response"]["output"][0]["content"][0]["annotations"] == annotations
    # Clients that build the item from the `.done` events see the same annotations.
    assert out[5]["part"]["annotations"] == annotations
    assert out[6]["item"]["content"][0]["annotations"] == annotations
    assert events[3]["part"]["annotations"] == [] and events[4]["item"]["content"][0]["annotations"] == []
    assert events[5]["response"]["output"][0]["content"][0]["annotations"] == [], "the input events are not mutated"
    assert cite.splice_annotation_events(events, []) == events
    assert cite.chat_message_annotations(annotations) == annotations


def test_markers_are_found_anywhere_in_a_line_with_any_spacing_and_malformed_ones_are_not_markers():
    found = list(cite.iter_markers("a [q3-report.pdf  p. 137] b [ deck.pptx slide 4 ] c [ p.3] [x p.] [x 1:2] [[q3-report.pdf p.1]"))
    assert [(m.label, m.unit, m.value) for m in found] == [
        ("q3-report.pdf", cite.UNIT_PAGE, 137), ("deck.pptx", cite.UNIT_SLIDE, 4), ("q3-report.pdf", cite.UNIT_PAGE, 1),
    ]
    assert [(m.label, m.value) for m in cite.iter_markers("[call 2 1:05] [a 1:23 4:56] [x p.3 p.5] [clip 1:02:03]")] == [
        ("call 2", 65.0), ("a 1:23", 296.0), ("x p.3", 5), ("clip", 3723.0)]


def test_scanning_hostile_text_for_markers_is_linear_not_quadratic():
    """2026-09-13 review: a lazy label next to `\\s+` cost ~255² steps per `[`.
    412,800 chars of `[` + 254 spaces + `x]` took 2.06 s to annotate (on the
    event loop); the two-pass scan takes ~20 ms on this box. 0.5 s is a
    bound a slow CI host keeps and the quadratic pattern cannot."""
    import time

    hostile = ("[" + " " * 254 + "x] ") * 1600 + ("[" + " " * 254 + "x p.1\n") * 1600
    started = time.perf_counter()
    got = cite.annotate(hostile, _index())
    elapsed = time.perf_counter() - started
    assert got.annotations == [] and elapsed < 0.5, elapsed


def test_a_label_cannot_be_registered_for_two_different_files():
    index = cite.CitationIndex()
    index.register("report (2).pdf", file_id="file-x1", filename="report (2).pdf", unit=cite.UNIT_PAGE)
    index.register("report (2).pdf", file_id="file-x1", filename="report (2).pdf", unit=cite.UNIT_PAGE)
    import pytest

    with pytest.raises(ValueError):
        index.register("report (2).pdf", file_id="file-x3", filename="report.pdf", unit=cite.UNIT_PAGE)
