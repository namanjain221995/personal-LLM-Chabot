"""A picture request with no data behind it.

"I want plot" over a CSV the pipeline never read produced a job that could
only ever end in "The artifact has no charts to draw as images" — a sentence
that tells the person nothing they can act on, after a compose and a render
that were never going to make a file (owner report, 2026-09-17).

Two rules are pinned here:

  * with NO table in the material, a png/svg-only request opens no job and
    asks for the data instead (`pipeline.accept` is never reached);
  * when the job did run and every chart in it became a "could not be drawn"
    callout, the render failure carries THAT sentence, not the generic one.
"""
from __future__ import annotations

import asyncio

import pytest

from app import db
from app.artifacts import intent as I
from app.artifacts import material_in as M
from app.artifacts import pipeline
from app.artifacts import render as R
from app.artifacts import spec as S
from app.engines import artifact as engine


@pytest.fixture(autouse=True)
def clean():
    pipeline.reset_for_tests()
    yield
    pipeline.reset_for_tests()


def _turn(monkeypatch, text, *, gathered=None, conv="conv-png", history=()):
    """The engine driven as main.py drives it, with acceptance watched."""
    owner = int(db.create_user(f"png-owner-{conv}", "hash"))
    accepted: list = []

    def never(*a, **kw):
        accepted.append(kw)
        raise AssertionError("pipeline.accept was called")

    monkeypatch.setattr(pipeline, "accept", never)
    events: list = []

    async def emit(kind, data):
        events.append((kind, data))

    intent = I.decide(text, has_artifacts=False, has_assistant_answer=any(h.get("role") == "assistant" for h in history))
    answer = asyncio.run(engine.run_artifact_engine(
        text, list(history), emit, intent=intent, conversation_id=conv, user_id=owner, generation_id="gen-png",
        gathered=gathered))
    return answer, events, accepted


def test_a_png_request_with_no_table_answers_without_a_job(monkeypatch):
    answer, events, accepted = _turn(monkeypatch, "plot this as a chart image")
    assert accepted == [], "no job is opened for a picture there is no data for"
    assert "table" in answer and ("Attach" in answer or "paste" in answer), answer
    metas = [d for k, d in events if k == "meta"]
    assert len(metas) == 1 and "artifacts" not in metas[0], "no artifact card is offered"


def test_the_sentence_names_the_file_that_could_not_be_read(monkeypatch):
    gathered = M.GatheredInput(notes=["customers-100.csv could not be used: the upload is failed.",
                                      "resume.pages is not a readable document or table"])
    answer, _events, accepted = _turn(monkeypatch, "plot this as a chart image", gathered=gathered, conv="conv-png-2")
    assert accepted == []
    assert "customers-100.csv" in answer and "resume.pages" in answer, answer


def test_a_document_request_with_no_table_still_opens_a_job(monkeypatch):
    """GUARD. Only an IMAGE-only version is refused: a report with no table
    is a report written from the conversation, which is a real answer."""
    with pytest.raises(AssertionError, match="pipeline.accept was called"):
        _turn(monkeypatch, "write me a short report as a Word file", conv="conv-png-3")


def test_image_only_reads_the_formats_and_nothing_else():
    assert engine._image_only(["png"]) and engine._image_only(["png", "svg"])
    assert not engine._image_only([]) and not engine._image_only(["png", "pdf"]) and not engine._image_only(["docx"])


def _spec_with_callout(text: str) -> S.ArtifactSpec:
    return S.load({"spec_version": 1, "kind": "document", "document": {
        "title": "Customer Base", "blocks": [
            {"type": "paragraph", "text": "A hundred customers."},
            {"type": "callout", "kind": "note", "title": "Customers by country", "text": text}]}})


def test_a_png_job_whose_charts_became_callouts_fails_with_the_callout_sentence(tmp_path):
    said = "The chart could not be drawn: the table 'customers-100.csv' is not available."
    spec = _spec_with_callout(said)
    assert R._chart_refusals(spec) == [said]
    with pytest.raises(R.RenderError) as exc:
        R._render_images_only(spec, ["png"], tmp_path, title_slug="customer-base", version=1, effort="fast", transform=None)
    assert "the table 'customers-100.csv' is not available" in str(exc.value)
    assert "no charts to draw as images" not in str(exc.value)


def test_a_png_job_with_no_chart_at_all_still_says_so(tmp_path):
    spec = S.load({"spec_version": 1, "kind": "document", "document": {
        "title": "Customer Base", "blocks": [{"type": "paragraph", "text": "A hundred customers."}]}})
    assert R._chart_refusals(spec) == []
    with pytest.raises(R.RenderError, match="no charts to draw as images"):
        R._render_images_only(spec, ["png"], tmp_path, title_slug="customer-base", version=1, effort="fast", transform=None)


def test_a_slide_bullet_carries_the_reason_too():
    deck = S.load({"spec_version": 1, "kind": "presentation", "presentation": {
        "title": "Customers", "slides": [
            {"layout": "bullets", "title": "By country",
             "bullets": ["The chart could not be drawn: the table 'orders.csv' is not available."]}]}})
    assert R._chart_refusals(deck) == ["The chart could not be drawn: the table 'orders.csv' is not available."]


def test_a_conversation_that_carries_figures_still_gets_its_job(monkeypatch):
    """GUARD. `material.tables` is the strong signal, not the only one: a
    chart CAN be drawn from figures the conversation already states, and
    refusing those would take away a picture that used to work."""
    history = [{"role": "assistant", "content": "Q1 120, Q2 140, Q3 165, Q4 190"}]
    with pytest.raises(AssertionError, match="pipeline.accept was called"):
        _turn(monkeypatch, "plot this as a chart image", conv="conv-png-4", history=history)
