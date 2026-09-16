"""Track 3, recheck pass: the honest refusal must not be traded away cheaply.

The first fix round bought three things with more room than they needed, and
each one hands a map request back to the machinery that cannot answer it.
Every case below was MEASURED failing on the previous commit.

  1. `visuals.names_a_drawable_type` counted ANY chart-type word anywhere in
     the message, so "plot these counts on a map like the bar chart you drew
     last time" lost its refusal to a bar chart that was only being referred to.
     The guard exists for the person who OFFERS a drawable as the fallback
     ("...or a bar chart if you cannot"), and that is all it may read.
  2. The attachment carve-out in main.py skipped the refusal for any
     image/pdf/video turn, so "plot these on a map" with the table attached
     as a PDF went to the document engine, which cannot draw anything — the
     turn ends in prose again, which is the 2026-09-16 incident.
  3. chart_data dropped an all-blank measure even when it was the combo
     chart's only main-axis column, leaving a "combo" that is one line on
     the secondary axis beside an empty left axis.
"""
from __future__ import annotations

import base64
import json

import pytest
from fastapi.testclient import TestClient

from app import llm, metrics
from app import main as app_main
from app.artifacts import chart_data as CD
from app.artifacts import chart_spec as CS
from app.artifacts import intent as I
from app.artifacts import pipeline
from app.artifacts import visuals as V
from app.artifacts.compose import DataTable
from app.config import settings
from app.main import _live_generations, app


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "reports_dir", str(tmp_path / "reports"))
    monkeypatch.setattr(app_main, "_shutting_down", False)
    _live_generations.clear()
    pipeline.reset_for_tests()
    metrics.reset()

    async def no_chat(messages, **kwargs):
        yield ("token", "This is a text answer.")

    monkeypatch.setattr(llm, "stream_chat_events", no_chat)
    yield
    pipeline.reset_for_tests()
    _live_generations.clear()
    metrics.reset()


def _parse_sse(text: str):
    events = []
    for block in text.strip().split("\n\n"):
        lines = block.strip().split("\n")
        if len(lines) >= 2 and lines[0].startswith("event: "):
            events.append((lines[0][7:], json.loads(lines[1][6:])))
    return events


# ------------------------- (1) a chart word is not an offer of a chart --

#: A drawable type MENTIONED — referred to, compared with, complained about —
#: while the thing being asked for is still the map.
MENTIONED_ONLY = [
    "plot these counts on a map like the bar chart you drew last time",
    "we already have a bar chart; now plot these on a map",
    "show the offices on a map, the bar chart you sent was unreadable",
    "the pie chart is done, so put the rest of these on a map",
    "show the depots on a map — same colours as the line chart above",
    "plot these on a map if there is no bar chart already",
]

#: A drawable type OFFERED as the fallback: the person has said what to do
#: when the map cannot be drawn, so draw that instead of refusing.
OFFERED_AS_ALTERNATIVE = [
    ("if a map is not possible, draw a pie chart of the states", "pie"),
    ("plot this on a map or a bar chart if you can't", "bar"),
    ("plot these on a map, or a bar chart if you cannot", "bar"),
    ("show these on a map; otherwise draw a bar chart", "bar"),
    ("draw a pie chart of the states if a map is not supported", "pie"),
    ("if there is no map type, draw a bar chart", "bar"),
]


@pytest.mark.parametrize("text", MENTIONED_ONLY)
def test_a_chart_named_only_in_passing_does_not_cancel_the_refusal(text):
    assert V.names_a_drawable_type(text) is False, text
    got = I.decide(text)
    assert got.action == "none" and got.unsupported_visual == "map", got.rule


@pytest.mark.parametrize("text,_type_word", OFFERED_AS_ALTERNATIVE)
def test_a_chart_offered_as_the_alternative_is_drawn(text, _type_word):
    assert V.names_a_drawable_type(text) is True, text
    got = I.decide(text)
    assert got.action == "create" and got.unsupported_visual == "", got.rule


def test_a_chart_type_named_only_to_reject_it_is_still_not_an_offer():
    """"draw a map, not a bar chart" asks for the map — unchanged."""
    assert V.names_a_drawable_type("draw this on a map, not a bar chart") is False
    got = I.decide("draw this on a map, not a bar chart")
    assert got.action == "none" and got.unsupported_visual == "map"


# ------------------- (2) an attachment is not a licence to skip the answer --


def test_a_map_request_that_merely_carries_a_pdf_is_still_refused(monkeypatch):
    """The table arrives as a PDF and the ask is to DRAW it on a map. The
    document engine cannot draw anything, so sending the turn there ends in
    prose — the incident. The refusal is the answer."""
    from app.engines import document as document_engine

    called = {"n": 0}

    async def resolved(request, conv):
        return [("records.pdf", base64.b64encode(b"%PDF-1.7 table").decode())], [], None

    async def fake_doc(message, docs, history, emit, **kw):
        called["n"] += 1
        await emit("token", {"text": "prose about the table"})
        return "prose about the table"

    monkeypatch.setattr(app_main, "_resolve_document_refs", resolved)
    monkeypatch.setattr(document_engine, "run_pdf_engine_multi", fake_doc)
    with TestClient(app) as client:
        resp = client.post("/chat", json={
            "message": "plot these records on a map", "mode": "assistant",
            "conversation_id": "vt3r-pdf", "intent_id": "int-vt3r-pdf", "effort": "fast",
            "pdf_uploads": [{"upload_id": "1" * 32, "name": "records.pdf"}],
        })
        assert resp.status_code == 200
        tokens = "".join(d["text"] for k, d in _parse_sse(resp.text) if k == "token")
    assert called["n"] == 0, "a drawing request does not become readable by attaching a file"
    assert "I can't draw a map" in tokens


def test_a_map_request_that_merely_carries_an_image_is_still_refused(monkeypatch):
    from app.engines import vision as vision_engine

    called = {"n": 0}

    async def fake_vision(message, images, history, emit, **kw):
        called["n"] += 1
        await emit("token", {"text": "prose about the photo"})
        return "prose about the photo"

    monkeypatch.setattr(vision_engine, "run_vision_engine", fake_vision)
    with TestClient(app) as client:
        resp = client.post("/chat", json={
            "message": "plot these counts on a map", "mode": "assistant",
            "conversation_id": "vt3r-img", "intent_id": "int-vt3r-img", "effort": "fast",
            "image_base64": base64.b64encode(b"ABC").decode(),
        })
        assert resp.status_code == 200
        tokens = "".join(d["text"] for k, d in _parse_sse(resp.text) if k == "token")
    assert called["n"] == 0
    assert "I can't draw a map" in tokens


@pytest.mark.parametrize("text", [
    "can you show me what the map says?",
    "what does the map on page 2 show?",
    "read the map in this image and list the depots",
    "describe the map in the attached file",
    "what is this a map of?",
    "summarise the map in the pdf",
    "describe the map drawn on page 4",
])
def test_a_question_about_the_attachments_content_still_bypasses_the_refusal(text):
    """The narrowing must keep what round one bought: a question ABOUT the
    file goes to the engine that can read it."""
    assert V.asks_about_attachment_content(text) is True, text


@pytest.mark.parametrize("text", [
    "plot these records on a map",
    "plot these counts on a map",
    "show these states on a map",
    "can you put this on a map",
])
def test_a_drawing_request_is_not_a_question_about_the_attachment(text):
    assert V.asks_about_attachment_content(text) is False, text


# --------------------------- (3) a combo with nothing on its main axis --


def _combo_table() -> DataTable:
    return DataTable(
        id="upload1", title="months.csv", columns=["Month", "Revenue", "Margin %"],
        rows=[["Jan", None, 12], ["Feb", "", 15], ["Mar", None, 9]],
    )


def test_a_combo_whose_only_main_axis_column_is_blank_is_refused():
    chart = CS.Chart.model_validate({
        "title": "t", "type": "combo",
        "data": {"table_id": "upload1", "x": "Month", "y": ["Revenue"], "y2": ["Margin %"]}})
    got, _notes, message = CD.resolve_chart(chart, [_combo_table()])
    assert got is None, [(s.name, s.axis, s.kind) for s in got.series] if got else None
    assert "'Revenue'" in message and "empty in every row" in message


def test_a_combo_that_keeps_one_main_axis_column_still_draws():
    table = DataTable(
        id="upload1", title="months.csv", columns=["Month", "Revenue", "Refunds", "Margin %"],
        rows=[["Jan", 100, None, 12], ["Feb", 80, "", 15], ["Mar", 60, None, 9]],
    )
    chart = CS.Chart.model_validate({
        "title": "t", "type": "combo",
        "data": {"table_id": "upload1", "x": "Month", "y": ["Revenue", "Refunds"], "y2": ["Margin %"]}})
    got, notes, message = CD.resolve_chart(chart, [table])
    assert got is not None and message == "", message
    assert [s.name for s in got.series] == ["Revenue", "Margin %"]
    assert any("Refunds" in n and "empty in every row" in n for n in notes), notes


def test_a_plain_chart_still_drops_a_blank_column_and_draws_the_rest():
    """Only the combo's split axes make an empty y fatal: everywhere else a
    second column is drawn on the same axis, so the chart is still a chart."""
    table = DataTable(
        id="upload1", title="months.csv", columns=["Month", "Revenue", "Refunds"],
        rows=[["Jan", None, 3], ["Feb", "", 5], ["Mar", None, 4]],
    )
    chart = CS.Chart.model_validate({
        "title": "t", "type": "bar",
        "data": {"table_id": "upload1", "x": "Month", "y": ["Revenue"], "y2": ["Refunds"]}})
    got, notes, message = CD.resolve_chart(chart, [table])
    assert got is not None and message == "", message
    assert [s.name for s in got.series] == ["Refunds"]
    assert any("Revenue" in n and "empty in every row" in n for n in notes), notes
