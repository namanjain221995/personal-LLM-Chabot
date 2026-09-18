"""The owner's #1 chat, after release 1 (hotfix 1.1, 2026-09-19).

A CSV is uploaded, then three turns: "I want plot ??", "give Big report",
"also i want Plots on this docs". Replayed through the /chat code path
in-process against the live engine on main @ 4e7cf8e, it still failed:

  * D1 — `material_in.wants_conversation_datasets` returned False for a
    `previous_answer` target BEFORE it looked at `chart_request`. When turn 2
    was a text answer, turn 3 decides create / previous_answer / chart, the
    CSV was never read, and every chart became "the table
    'customers-100.csv' is not available".
  * D2 — "give Big report" was no-request to the rules, and the classifier
    said none about 5 times in 6: a chat answer, no document.
  * D3 — "I want plot ??" is `ambiguous` to the rules (the question marks),
    so it depended on a classifier that flipped between png and none; with
    none the dataset engine refused to draw.
  * D4 — the Fast classifier budget (2.5 s) was exceeded under load and the
    turn fell back to the rules in silence. So the RULES must route these.
  * D5 — the composer's internal notices were read to the person verbatim:
    "_Top 5 Countries: the table 'table1' is not available._" and
    "_figures not in the material (derived or assumed): 10 percent, …_".

Each rule here has its guard: a plain question about the data stays a chat
answer, "report" the verb is not a file, and with NO dataset in the
conversation the new rules do not fire.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

import pytest

from app.artifacts import compose as C
from app.artifacts import formats as F
from app.artifacts import intent as I
from app.artifacts import material_in as M
from app.artifacts import types as T

FIXTURE = Path(__file__).parent / "fixtures" / "artifacts" / "customers_100.csv"
UPLOAD_ID = "0a1b2c3d4e5f60718293a4b5c6d7e8f9"

#: The three states the owner's turns are sent in.
STATES = {
    "first turn": dict(),
    "after a text answer": dict(has_assistant_answer=True),
    "after a file card": dict(has_artifacts=True, artifact_hints=["Customer Data Report"], has_assistant_answer=True,
                              last_turn_is_artifact=True),
}

#: The words the task names, each of which must be routed by the rules alone.
PLOT_ASKS = ["I want plot ??", "I want plot", "plot this", "chart it", "plot ??", "i want graphs??"]
REPORT_ASKS = ["give Big report", "full report", "detailed report", "Big report please"]
DOC_PLOTS = "also i want Plots on this docs"


# --------------------------------------------------------------- the rules --


@pytest.mark.parametrize("state", list(STATES))
@pytest.mark.parametrize("text", PLOT_ASKS)
def test_a_plot_ask_over_a_dataset_is_a_chart_file(text, state):
    intent = I.decide(text, has_dataset=True, **STATES[state])
    assert intent.action == "create", (text, state, intent.action, intent.rule)
    assert intent.chart_request is True and not intent.ambiguous
    assert F.decide(intent.instruction, explicit_only=intent.formats or None, chart_request=True).formats == ["png"]
    # D1: whatever the target — the conversation, or the answer it points at —
    # the chart is drawn FROM the data, so the data is gathered.
    assert M.wants_conversation_datasets(intent) is True, (text, state, intent.target)


@pytest.mark.parametrize("state", list(STATES))
@pytest.mark.parametrize("text", REPORT_ASKS)
def test_a_report_ask_over_a_dataset_is_a_document_of_that_data(text, state):
    intent = I.decide(text, has_dataset=True, **STATES[state])
    assert (intent.action, intent.target) == ("create", "conversation"), (text, state, intent.action, intent.rule)
    assert intent.chart_request is False
    decision = F.decide(intent.instruction, explicit_only=intent.formats or None, chart_request=False)
    assert decision.kind == "document" and decision.formats == ["docx", "pdf"]
    assert M.wants_conversation_datasets(intent) is True


@pytest.mark.parametrize("state", list(STATES))
def test_plots_on_the_docs_always_get_the_data(state):
    """D1. After a TEXT answer the rules read the third sentence as a create
    whose source is that answer; after the report's card, as a conversion.
    Either way it asks for plots, so the CSV must be read."""
    intent = I.decide(DOC_PLOTS, **STATES[state])
    assert intent.wants_file and intent.chart_request is True, (state, intent.action, intent.rule)
    assert M.wants_conversation_datasets(intent) is True, (state, intent.action, intent.target)


@pytest.mark.parametrize("text", ["I want plot ??", "plot this", "chart it", "give Big report", "full report",
                                  "detailed report", DOC_PLOTS])
def test_the_rules_alone_route_them_when_the_classifier_times_out(text):
    """D4. The Fast classifier timed out under load and the turn fell back
    to the rules; the rules must already have the answer, so the hook is
    never even asked."""
    calls = []

    async def timed_out(*args, **kwargs):
        calls.append(args)
        raise asyncio.TimeoutError()

    for state, ctx in STATES.items():
        intent = asyncio.run(I.decide_with_hook(text, timed_out, has_dataset=True, **ctx))
        assert intent.wants_file, (text, state, intent.rule)
    assert calls == [], "the classifier was consulted for a sentence the rules must decide"


# ---------------------------------------------------------------- guards --


@pytest.mark.parametrize("state", list(STATES))
@pytest.mark.parametrize("text", ["what's the average age?", "how many customers are in Chile?",
                                  "which country has the most customers?", "summarise the data"])
def test_a_plain_question_about_the_data_stays_a_chat_answer(text, state):
    assert I.decide(text, has_dataset=True, **STATES[state]).action == "none"


@pytest.mark.parametrize("dataset", [True, False])
@pytest.mark.parametrize("state", list(STATES))
@pytest.mark.parametrize("text", ["I want to report a bug", "report a bug", "as I reported earlier, the numbers look off",
                                  "I need to report an issue with the upload", "we want to report it to the team"])
def test_report_the_verb_is_not_a_file(text, state, dataset):
    """"I want to report a bug" was a create on main (4e7cf8e): `i want`
    plus the noun `report` within six words. The infinitive is a verb."""
    intent = I.decide(text, has_dataset=dataset, **STATES[state])
    assert intent.action == "none", (text, state, intent.rule)


@pytest.mark.parametrize("text", ["give me a full report on Q3 revenue", "I want a detailed report", "make a big report",
                                  "create a report of the audit"])
def test_a_report_the_person_asks_for_with_a_verb_is_still_a_file_without_a_dataset(text):
    """GUARD for the verb rule: the noun after an article is untouched."""
    assert I.decide(text).action == "create", text


@pytest.mark.parametrize("state", ["first turn", "after a text answer"])
@pytest.mark.parametrize("text", ["I want plot ??", "give Big report", "full report", "detailed report", "plot ??"])
def test_with_no_dataset_and_no_artifact_the_new_rules_do_not_fire(text, state):
    intent = I.decide(text, has_dataset=False, **STATES[state])
    assert intent.action == "none", (text, state, intent.rule)
    assert not intent.rule.startswith("dataset-"), intent.rule


@pytest.mark.parametrize("text", ["should I plot this?", "the plot is wrong?", "I want to know if the plot is right?",
                                  "does a chart make sense here?"])
def test_a_question_about_a_plot_is_not_a_chart_ask_even_with_a_dataset(text):
    intent = I.decide(text, has_dataset=True, has_assistant_answer=True)
    assert intent.action == "none", (text, intent.rule)
    assert not intent.rule.startswith("dataset-"), intent.rule


# ---------------------------------------------------------- the material --


def _dataset_upload(workspace: Path, conv: str) -> None:
    """One upload as app/uploads._finalise_dataset leaves it."""
    from app import db

    root = workspace / "uploads" / conv / UPLOAD_ID / "extracted"
    root.mkdir(parents=True, exist_ok=True)
    (root / "customers-100.csv").write_bytes(FIXTURE.read_bytes())
    db.save_upload(UPLOAD_ID, conv, "customers-100.csv", 4096, "ready", None, None)


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    return tmp_path


@pytest.mark.parametrize("text", [DOC_PLOTS, "plot this", "chart it"])
def test_a_chart_asked_of_a_text_answer_is_drawn_from_the_conversations_csv(workspace, text):
    """D1, the reported string word for word: turn 2 was a text answer, so
    turn 3 is create / previous_answer / chart_request — and gather read no
    CSV, so every chart became "the table … is not available"."""
    conv = "conv-hotfix-plots-" + str(abs(hash(text)))
    _dataset_upload(workspace, conv)
    intent = I.decide(text, has_assistant_answer=True)
    assert (intent.action, intent.target, intent.chart_request) == ("create", "previous_answer", True), intent.rule
    history = [{"role": "user", "content": "give Big report"},
               {"role": "assistant", "content": "## Customer report\n\nThe file has 100 customers across 10 columns. " * 12}]
    g = asyncio.run(M.gather(history=history, conversation_id=conv, workspace=str(workspace), intent=intent, text=text,
                             save_documents=False))
    assert [t.id for t in g.upload_tables] == ["upload1"], "the dataset the plots must be drawn from"
    assert len(g.upload_tables[0].rows) == 100


def test_a_previous_answer_turned_into_a_file_without_a_chart_still_reads_no_dataset(workspace):
    """GUARD for D1: only a CHART needs the data. "put that in a pdf" is the
    answer as written, and a CSV pulled into it would be rows nobody asked for."""
    conv = "conv-hotfix-plots-guard"
    _dataset_upload(workspace, conv)
    intent = I.decide("put that in a pdf", has_assistant_answer=True)
    assert intent.chart_request is False
    assert M.wants_conversation_datasets(intent) is False
    g = asyncio.run(M.gather(history=[], conversation_id=conv, workspace=str(workspace), intent=intent,
                             text="put that in a pdf", save_documents=False))
    assert g.upload_tables == []


# ------------------------------------------------- what the person reads --


TABLE_NOTICE = "Top 5 Countries by Customer Count: the table 'table1' is not available"
FIGURES_NOTICE = C.FIGURES_WARNING + "10 percent, 50 percent, 12,507"


def _ref(warnings):
    return T.ArtifactRef(artifact_id="a" * 32, version=1, job_id="j" * 32, title="Customer Data Report", kind="document",
                         status="completed_with_warnings",
                         files=[T.FileRef(format="docx", filename="r.docx", mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document", size=10)], warnings=list(warnings))


def test_internal_notices_never_reach_the_card():
    """D5. The card lists `warnings`; the two composer notices are for
    operators. A chart that could not be drawn is still worth saying — in
    plain words, with no table id."""
    said = _ref([TABLE_NOTICE, "Top 5 First Names: the table 'customers-100.csv' is not available", FIGURES_NOTICE,
                 "the document is about 1,200 words against the 3,000 asked for"]).to_json()["warnings"]
    text = " ".join(said)
    assert "is not available" not in text and "table1" not in text and "figures not in the material" not in text, said
    assert "2 charts could not be drawn from your data" in said
    assert "the document is about 1,200 words against the 3,000 asked for" in said, "a plain note is kept as it was"


def test_internal_notices_never_reach_the_sentence():
    from app.engines import artifact as engine

    line = engine._sentence(_ref([TABLE_NOTICE, FIGURES_NOTICE]), "create", [TABLE_NOTICE, FIGURES_NOTICE])
    assert line.startswith("Created **Customer Data Report** as Word.")
    assert "not available" not in line and "table1" not in line and "figures not in the material" not in line, line
    assert "1 chart could not be drawn from your data" in line
    assert engine._warning_clause([FIGURES_NOTICE]) == "", "a figures check alone says nothing to the reader"


def test_internal_notices_are_logged_for_operators(caplog):
    from app.engines import artifact as engine

    ref = _ref([TABLE_NOTICE, FIGURES_NOTICE, "a plain note"])
    with caplog.at_level(logging.INFO, logger=engine.log.name):
        engine._log_operator_notes(ref, ref.warnings)
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "table1" in logged and "figures not in the material" in logged and ref.artifact_id in logged, logged
    assert "a plain note" not in logged


# ------------------------------------------------------------- the route --


def _parse_sse(text: str):
    events = []
    for block in text.strip().split("\n\n"):
        lines = block.strip().split("\n")
        if len(lines) >= 2 and lines[0].startswith("event: "):
            events.append((lines[0][7:], json.loads(lines[1][6:])))
    return events


@pytest.fixture()
def chat_app(tmp_path, monkeypatch, workspace):
    import hashlib

    from app import llm, metrics
    from app import main as app_main
    from app.artifacts import intent_llm, pipeline
    from app.artifacts import spec as S
    from app.config import settings
    from app.main import _live_generations

    monkeypatch.setattr(settings, "reports_dir", str(tmp_path / "reports"))
    monkeypatch.setattr(settings, "artifact_min_free_mb", 1)
    monkeypatch.setattr(settings, "artifact_user_quota_mb", 1024)
    monkeypatch.setattr(settings, "video_pace_max_wait_s", 0.0)
    monkeypatch.setattr(app_main, "_shutting_down", False)
    _live_generations.clear()
    pipeline.reset_for_tests()
    metrics.reset()

    async def composer(ctx):
        await ctx.progress_stage("intent", "done", "")
        ctx.warn(TABLE_NOTICE)
        ctx.warn(FIGURES_NOTICE)
        return S.parse_body("document", {"title": "Customer Data Report", "blocks": [{"type": "paragraph", "text": "100 customers."}]})

    async def render(work_dir, spec, formats, title_slug, version, effort):
        files = []
        for fmt in formats:
            name = f"{title_slug}-v{version}.{fmt}"
            body = f"{fmt} bytes".encode() * 50
            with open(os.path.join(work_dir, name), "wb") as fh:
                fh.write(body)
            files.append({"format": fmt, "filename": name, "size": len(body), "sha256": hashlib.sha256(body).hexdigest(),
                          "pages": 1 if fmt == "pdf" else None})
        with open(os.path.join(work_dir, T.PREVIEW_PDF_NAME), "wb") as fh:
            fh.write(b"%PDF-1.7 preview")
        return {"files": files, "preview_pdf": T.PREVIEW_PDF_NAME, "preview_kind": "pages", "preview_pages": 1, "warnings": [],
                "validation": {"reopened": True}, "chart_files": [], "timings": {}}

    monkeypatch.setattr(pipeline, "_render_in_subprocess", render)
    monkeypatch.setattr(pipeline, "_page_count", lambda pdf: 1)
    monkeypatch.setattr(pipeline, "_rasterise_page", lambda pdf, page, width: b"\x89PNG")

    async def text_answer(messages, **kwargs):
        yield ("token", "This is a text answer.")

    monkeypatch.setattr(llm, "stream_chat_events", text_answer)

    # D2/D4: what the classifier did in the live replay — "none", or a
    # timeout. Either way the turn must still reach the artifact branch.
    async def classifier_says_none(text, **kwargs):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(intent_llm, "classify", classifier_says_none)
    yield composer
    pipeline.reset_for_tests()
    pipeline.set_composer(None)
    _live_generations.clear()
    metrics.reset()


@pytest.mark.parametrize("text,formats", [("give Big report", ["docx", "pdf"]), ("I want plot ??", ["png"])])
def test_the_owners_turn_over_an_uploaded_csv_is_a_file_through_chat(chat_app, workspace, caplog, text, formats):
    from fastapi.testclient import TestClient

    from app.artifacts import pipeline
    from app.main import app

    conv = "conv-hotfix-route-" + formats[0]
    _dataset_upload(workspace, conv)
    with TestClient(app) as client, caplog.at_level(logging.INFO, logger="app.engines.artifact"):
        pipeline.set_composer(chat_app)
        resp = client.post("/chat", json={"message": text, "mode": "assistant", "conversation_id": conv,
                                          "intent_id": "int-" + conv, "effort": "fast"})
        assert resp.status_code == 200
    events = _parse_sse(resp.text)
    final = [d for k, d in events if k == "meta"][-1]
    tokens = "".join(d["text"] for k, d in events if k == "token")
    assert final.get("route") == "artifact", (final.get("route"), tokens[:300])
    ref = final["artifacts"][0]
    assert [f["format"] for f in ref["files"]] == formats
    # D5 through the whole turn: neither the sentence nor the card carries
    # the composer's notices; the operator log does.
    shown = tokens + " " + " ".join(ref["warnings"])
    assert "table1" not in shown and "figures not in the material" not in shown and "is not available" not in shown, shown
    assert "1 chart could not be drawn from your data" in tokens
    assert any("table1" in r.getMessage() for r in caplog.records)
