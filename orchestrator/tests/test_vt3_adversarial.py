"""Track 3, adversarial pass: the honest-limits answer must not eat the turn.

Every case here was MEASURED failing on the merged tree (e640535) before the
fix in this commit, and each one is the same shape of mistake the 2026-09-16
incident was: the platform answering something other than what was asked.

  1. An attached IMAGE or PDF whose CONTENT is a map ("what does the map on
     page 2 show?") was answered with the drawing refusal and the vision /
     document engine was never called — the file was never read.
  2. The denial backstop's carve-out read the whole sentence around the
     match, so a REAL file denial that happens to mention a map
     ("I cannot create a PDF file of the map.") was silenced.
  3. The refusal quoted an offer — Ask for "Count (Approx) by State as a bar
     chart" — that the intent gate then answered with nothing
     (action=none rule=no-request), because it has no verb.
  4. A message naming a map AND a chart this platform CAN draw ("if a map is
     not possible, draw a pie chart of the states") was refused instead of
     drawing the pie chart.
  5. One all-blank measure killed the two columns that did have numbers.
  6. With no table in the conversation the refusal still pointed at "the
     table above".
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
from app.artifacts import visuals as V
from app.artifacts import pipeline
from app.artifacts.compose import DataTable
from app.config import settings
from app.engines import capability as cap
from app.main import _live_generations, app


# --------------------------------------------------- (1) the attachments --


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


def test_a_question_about_a_map_in_an_attached_image_still_reaches_vision(monkeypatch):
    from app.engines import vision as vision_engine

    called = {"n": 0}

    async def fake_vision(message, images, history, emit, **kw):
        called["n"] += 1
        await emit("token", {"text": "The map shows three sites."})
        await emit("meta", {"route": "vision"})
        return "The map shows three sites."

    monkeypatch.setattr(vision_engine, "run_vision_engine", fake_vision)
    with TestClient(app) as client:
        resp = client.post("/chat", json={
            "message": "can you show me what the map says?", "mode": "assistant",
            "conversation_id": "vt3-img", "intent_id": "int-vt3-img", "effort": "fast",
            "image_base64": base64.b64encode(b"ABC").decode(),
        })
        assert resp.status_code == 200
        tokens = "".join(d["text"] for k, d in _parse_sse(resp.text) if k == "token")
    assert called["n"] == 1, "the attached image must be READ, not answered with a drawing refusal"
    assert "I can't draw a map" not in tokens


def test_a_question_about_a_map_in_an_attached_pdf_still_reaches_the_document_engine(monkeypatch):
    from app.engines import document as document_engine

    called = {"n": 0}

    async def resolved(request, conv):
        return [("atlas.pdf", base64.b64encode(b"%PDF-1.7 map").decode())], [], None

    async def fake_doc(message, docs, history, emit, **kw):
        called["n"] += 1
        await emit("token", {"text": "Page 2 maps the depots."})
        await emit("meta", {"route": "vision"})
        return "Page 2 maps the depots."

    monkeypatch.setattr(app_main, "_resolve_document_refs", resolved)
    monkeypatch.setattr(document_engine, "run_pdf_engine_multi", fake_doc)
    with TestClient(app) as client:
        resp = client.post("/chat", json={
            "message": "what does the map on page 2 show?", "mode": "assistant",
            "conversation_id": "vt3-pdf", "intent_id": "int-vt3-pdf", "effort": "fast",
            "pdf_uploads": [{"upload_id": "0" * 32, "name": "atlas.pdf"}],
        })
        assert resp.status_code == 200
        tokens = "".join(d["text"] for k, d in _parse_sse(resp.text) if k == "token")
    assert called["n"] == 1, "the attached document must be READ, not answered with a drawing refusal"
    assert "I can't draw a map" not in tokens


# ------------------------------------------------- (2) the denial carve-out --


@pytest.mark.parametrize("answer", [
    "I cannot attach the excel sheet, but here is the map of the data.",
    "I cannot create a PDF file of the map.",
    "I can't generate a Word document with the map for you.",
    "I cannot create an Excel workbook of the map data for you.",
])
def test_a_real_file_denial_that_merely_mentions_a_map_is_still_a_denial(answer):
    """The carve-out is for "there is no map to give you", not for every
    denial that has the word map somewhere in the sentence."""
    assert cap.denial_in(answer) is True


def test_the_code_written_refusal_needs_no_carve_out(monkeypatch):
    """The sentence visuals.py writes is not a file denial in the first
    place — with the carve-out forced OFF it is still not one, so the
    carve-out can be as narrow as honesty needs."""
    monkeypatch.setattr(cap, "_honest_visual_refusal", lambda text, start, end: False)
    assert cap.denial_in(V.refusal_for("map", history=[])) is False
    assert cap.denial_in(V.refusal_for("venn", history=[])) is False


# ------------------------------------------- (3) the offer the gate accepts --

_HISTORY = [{"role": "user", "content": "how many records per state?"},
            {"role": "assistant", "content": """| Rank | State | Count (Approx) | % of Total Records |
| --- | --- | --- | --- |
| 1 | Texas | 21 | 13.3% |
| 2 | Missouri | 11 | 7.0% |
| 3 | Illinois | 9 | 5.7% |
"""}]


@pytest.mark.parametrize("history", [[], _HISTORY])
def test_the_phrase_the_refusal_quotes_is_one_the_gate_answers(history):
    """A person who copies the quoted phrase must get the chart. The quoted
    form had no verb, and decide() read it as no-request — the same "it does
    not do what it said" the incident was about."""
    said = V.refusal_for("map", history=history)
    quoted = said.split('Ask for "', 1)[1].split('"', 1)[0]
    got = I.decide(quoted)
    assert got.wants_file is True, f"the refusal quotes {quoted!r}, which the gate answers with {got.rule}"
    assert got.unsupported_visual == ""


# --------------------------------- (4) a map AND a chart that can be drawn --


@pytest.mark.parametrize("text,type_word", [
    ("if a map is not possible, draw a pie chart of the states", "pie"),
    ("plot this on a map or a bar chart if you can't", "bar"),
])
def test_a_message_that_also_names_a_drawable_chart_draws_that_chart(text, type_word):
    got = I.decide(text)
    assert got.action == "create" and got.unsupported_visual == "", got.rule


@pytest.mark.parametrize("text", [
    "plot this on a map",
    "can you show these counts on a map",
    "show me a choropleth of the states",
])
def test_a_map_request_with_no_drawable_type_named_is_still_refused(text):
    got = I.decide(text)
    assert got.action == "none" and got.unsupported_visual in ("map", "choropleth")


def test_a_chart_type_named_only_to_reject_it_does_not_count():
    """"draw a map, not a bar chart" asks for the map: answering with the
    bar chart would draw the very thing the person ruled out."""
    got = I.decide("draw this on a map, not a bar chart")
    assert got.action == "none" and got.unsupported_visual == "map"


# ------------------------------------------- (5) one blank measure of three --


def test_one_empty_measure_does_not_kill_the_columns_that_do_have_numbers():
    table = DataTable(
        id="upload1", title="contacts.csv",
        columns=["State", "Count", "Revenue", "PersonMailingLatitude"],
        rows=[["Texas", 21, 100, None], ["Missouri", 11, 50, ""], ["Illinois", 9, 25, None]],
    )
    chart = CS.Chart.model_validate(
        {"title": "t", "type": "bar", "data": {"table_id": "upload1", "x": "State", "y": ["Count", "Revenue", "PersonMailingLatitude"]}})
    got, notes, message = CD.resolve_chart(chart, [table])
    assert got is not None and message == "", message
    assert [s.name for s in got.series] == ["Count", "Revenue"]
    assert got.series[0].values == [21, 11, 9] and got.series[1].values == [100, 50, 25]
    assert any("PersonMailingLatitude" in n and "empty in every row" in n for n in notes), notes


def test_every_measure_blank_still_refuses_the_chart():
    table = DataTable(id="upload1", title="contacts.csv", columns=["State", "Lat", "Long"],
                      rows=[["Texas", None, ""], ["Missouri", "", None]])
    chart = CS.Chart.model_validate(
        {"title": "t", "type": "bar", "data": {"table_id": "upload1", "x": "State", "y": ["Lat", "Long"]}})
    got, _notes, message = CD.resolve_chart(chart, [table])
    assert got is None and "empty in every row" in message


# ------------------------------------ (6) no table above to point back at --


def test_the_refusal_does_not_claim_a_table_that_is_not_there():
    said = V.refusal_for("network", history=[])
    assert "network diagram" in said and "table above" not in said


def test_the_refusal_points_at_the_table_when_there_really_is_one():
    said = V.refusal_for("network", history=_HISTORY)
    assert "table above" in said


def test_a_process_flow_is_not_offered_a_bar_chart_of_values_that_do_not_exist():
    said = V.refusal_for("sankey", history=[])
    assert "the same values by category as a bar chart" not in said
    assert "Sankey" in said


def test_a_question_about_a_map_in_a_video_already_attached_reaches_the_video_engine(monkeypatch):
    """The same defect one route further down: `video_followup` turns are
    decided by the gate too (only a video attached THIS turn is excluded),
    so "what does the map at 2:10 show?" was answered with the drawing
    refusal and the analysis was never consulted."""
    from app import db
    from app.engines import video as video_engine

    called = {"n": 0}
    rows = [{"video_id": "v1", "filename": "walkthrough.mp4", "status": "done", "duration_s": 300,
             "language": "en", "understanding": {"summary": "A site walkthrough."}}]

    async def fake_video(message, videos, history, emit, **kw):
        called["n"] += 1
        await emit("token", {"text": "At 2:10 the map shows the north depot."})
        await emit("meta", {"route": "video"})
        return "At 2:10 the map shows the north depot."

    async def about(message, videos):
        return True

    monkeypatch.setattr(settings, "video_analysis_enabled", True)
    monkeypatch.setattr(db, "get_conversation_videos", lambda conv: rows)
    monkeypatch.setattr(video_engine, "is_about_video", about)
    monkeypatch.setattr(video_engine, "run_video_engine", fake_video)
    monkeypatch.setattr(app_main, "_resolve_video_refs", lambda request, conv, videos: _resolved_videos(rows))
    with TestClient(app) as client:
        resp = client.post("/chat", json={
            "message": "what does the map at 2:10 show?", "mode": "assistant",
            "conversation_id": "vt3-video", "intent_id": "int-vt3-video", "effort": "fast",
        })
        assert resp.status_code == 200
        tokens = "".join(d["text"] for k, d in _parse_sse(resp.text) if k == "token")
    assert called["n"] == 1, "a question about a video the conversation holds must reach the video engine"
    assert "I can't draw a map" not in tokens


async def _resolved_videos(rows):
    return rows, None
