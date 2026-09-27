"""End to end: the transcript's own case, driven through the artifact engine.

    turn 1  "OK Make sheet for Me ??"      -> Created ... as Excel   (v1)
    turn 2  "Ok What This sheet have ??"   -> the sheets, the columns and the
                                             rows, AND STILL v1

Production answered turn 2 with a second version of the workbook and turn 3 —
"i want to Know ?? please tell me Only Not create d??" — with a third. So the
assertion that matters most in this file is the one nobody thinks to write:
`len(adb.list_versions(...)) == 1` AFTER the question.

The engine is driven exactly as main.py drives it, against the private test
database, with the composer and the renderer stubbed the way
tests/test_artifact_engine.py stubs them. The QUESTION verdict on the intent
is built by hand here: deciding that a turn is a question belongs to the
intent gate (track `question-not-edit`), and `describe.is_artifact_question`
is the seam between the two — these tests pin the seam, not the gate.
"""
from __future__ import annotations

import asyncio
import hashlib
import os

import pytest

from app import db, metrics
from app.artifacts import db as adb
from app.artifacts import describe as D
from app.artifacts import intent as I
from app.artifacts import pipeline
from app.artifacts import spec as S
from app.artifacts import types as T
from app.config import settings
from app.engines import artifact as engine

CONV = "conv-answer-spec"

_TRACKER = {
    "title": "TechSara AI Engineering Workflow Tracker",
    "sheets": [
        {"name": "Workflow",
         "columns": [{"name": n} for n in ("Stage", "Owner", "Status", "Start Date", "Due Date", "Notes")],
         "rows": [["Intake", "Naman", "Done", "2026-09-01", "2026-09-03", "signed off"]] * 5},
        {"name": "Summary", "columns": [{"name": "Metric"}, {"name": "Value"}], "rows": [["Open", 3], ["Done", 2]]},
    ],
}
HEADERS = ("Stage", "Owner", "Status", "Start Date", "Due Date", "Notes")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "reports_dir", str(tmp_path / "reports"))
    monkeypatch.setattr(settings, "artifact_lease_ttl_s", 60.0)
    monkeypatch.setattr(settings, "artifact_stage_timeout_s", 30.0)
    monkeypatch.setattr(settings, "artifact_render_timeout_s", 30.0)
    monkeypatch.setattr(settings, "artifact_min_free_mb", 1)
    monkeypatch.setattr(settings, "artifact_user_quota_mb", 1024)
    monkeypatch.setattr(settings, "video_pace_max_wait_s", 0.0)
    pipeline.reset_for_tests()
    pipeline.install_busy_probe(None)
    metrics.reset()
    yield
    pipeline.reset_for_tests()
    pipeline.set_composer(None)
    pipeline.install_busy_probe(None)
    metrics.reset()


@pytest.fixture()
def owner():
    return int(db.create_user("answer-spec-owner", "hash"))


def _install(monkeypatch, *, composed=None):
    """The workbook composer and renderer of tests/test_artifact_engine.py,
    returning the transcript's tracker. `composed` counts the calls, so a
    question turn can be shown to have made none."""
    async def stub_composer(ctx):
        if composed is not None:
            composed.append(ctx.operation)
        await ctx.progress_stage("intent", "done", "x")
        await ctx.progress_stage("gather", "done", "y")
        await ctx.progress_stage("outline", "skipped", "")
        return S.parse_body("workbook", _TRACKER)

    async def fake_render(work_dir, spec, formats, title_slug, version, effort):
        files = []
        for fmt in formats:
            name = f"{title_slug}-v{version}.{fmt}"
            body = f"{fmt} of {spec.title}".encode() * 8
            with open(os.path.join(work_dir, name), "wb") as fh:
                fh.write(body)
            files.append({"format": fmt, "filename": name, "size": len(body),
                          "sha256": hashlib.sha256(body).hexdigest(), "pages": 2 if fmt == "pdf" else None})
        with open(os.path.join(work_dir, T.PREVIEW_PDF_NAME), "wb") as fh:
            fh.write(b"%PDF-1.7 preview")
        return {"files": files, "preview_pdf": T.PREVIEW_PDF_NAME, "preview_kind": "pages", "preview_pages": 2,
                "warnings": [], "validation": {"reopened": True}, "chart_files": [], "timings": {}}

    pipeline.set_composer(stub_composer)
    monkeypatch.setattr(pipeline, "_render_in_subprocess", fake_render)
    monkeypatch.setattr(pipeline, "_page_count", lambda pdf: 2)
    monkeypatch.setattr(pipeline, "_rasterise_page", lambda pdf, page, width: b"\x89PNG")


def _events():
    out = []

    async def emit(kind, data):
        out.append((kind, data))

    return out, emit


def _meta(events):
    metas = [d for k, d in events if k == "meta"]
    assert len(metas) == 1, f"exactly one meta, got {len(metas)}"
    return metas[0]


def _tokens(events):
    return "".join(d["text"] for k, d in events if k == "token")


def _create(owner, monkeypatch, *, composed=None, conv=CONV):
    _install(monkeypatch, composed=composed)
    events, emit = _events()
    text = "OK Make sheet for Me ??"
    intent = I.decide(text)
    answer = asyncio.run(engine.run_artifact_engine(
        text, [], emit, intent=intent, conversation_id=conv, user_id=owner, generation_id="g1", effort="fast"))
    return answer, events


def _ask(owner, text, *, conv=CONV, effort="fast", history=(), action="answer", **fields):
    """A turn whose intent carries the gate's QUESTION verdict."""
    events, emit = _events()
    intent = I.ArtifactIntent(action, rule="artifact-question", instruction=text, raw_text=text, **fields)
    assert D.is_artifact_question(intent) is True, "the seam this file is about"
    answer = asyncio.run(engine.run_artifact_engine(
        text, list(history), emit, intent=intent, conversation_id=conv, user_id=owner, generation_id="g2", effort=effort))
    return answer, events


def _versions(owner, artifact_id):
    return adb.list_versions(artifact_id, owner)


# ------------------------------------------------------------ the anchor case --


@pytest.mark.parametrize("question", [
    "Ok What This sheet have ??",
    "I said ??? what you create inside the sheet ??? i want to Know ?? please tell me Only Not create d??",
    "what does this sheet have?",
])
def test_the_question_is_answered_from_the_spec_and_no_new_version_is_written(owner, monkeypatch, question):
    composed = []
    _, first = _create(owner, monkeypatch, composed=composed)
    ref = _meta(first)["artifacts"][0]
    artifact_id = ref["artifact_id"]
    assert ref["version"] == 1 and len(_versions(owner, artifact_id)) == 1
    assert composed == ["create"]

    answer, events = _ask(owner, question)

    # 1. The answer says what the workbook holds.
    for name in ("Workflow", "Summary"):
        assert f"`{name}`" in answer
    for header in HEADERS:
        assert f"`{header}`" in answer, f"{header!r} missing"
    assert "2 sheets" in answer and "6 columns" in answer and "5 rows" in answer
    assert _tokens(events) == answer, "what was streamed is what was returned"

    # 2. NOTHING WAS MADE. The version count is the assertion the production
    #    defect would have failed three times over.
    assert len(_versions(owner, artifact_id)) == 1, "a question published a version"
    assert composed == ["create"], "the composer was not called for a question"
    assert adb.get_version(artifact_id, 2, owner) is None

    # 3. The meta does not put the file card back in the transcript.
    meta = _meta(events)
    assert meta["route"] == "artifact"
    assert "artifacts" not in meta and "report_files" not in meta
    assert meta["artifact_answer"] == {
        "artifact_id": artifact_id, "version": 1, "kind": "workbook",
        "topics": ["sheets"], "grounded": True, "model_used": False,
    }


def test_the_answer_turn_makes_no_model_call_at_all(owner, monkeypatch):
    """A fact about a stored spec is code's to state. Fast gains no extra
    call, because this path makes none."""
    from app import continuation

    calls = []
    monkeypatch.setattr(continuation, "stream_long_completion", lambda *a, **k: calls.append(k))
    _create(owner, monkeypatch)
    answer, _ = _ask(owner, "what columns does it have?")
    assert "`Due Date`" in answer
    assert calls == []


def test_three_questions_in_a_row_still_leave_one_version(owner, monkeypatch):
    """The transcript's shape: the person asked, was given a file, asked
    again, was given another. Asked three times here, the count never moves."""
    _, first = _create(owner, monkeypatch)
    artifact_id = _meta(first)["artifacts"][0]["artifact_id"]
    for question in ("Ok What This sheet have ??",
                     "I said ??? what you create inside the sheet ?? please tell me Only Not create d??",
                     "what columns does it have?"):
        answer, _ = _ask(owner, question)
        assert answer and "Created" not in answer and "Converted" not in answer
    assert len(_versions(owner, artifact_id)) == 1


def test_a_question_about_version_one_reads_that_versions_spec(owner, monkeypatch):
    """"what was in version 1?" answers from v1's own spec.json and its own
    files, not from the current version's."""
    _, first = _create(owner, monkeypatch)
    artifact_id = _meta(first)["artifacts"][0]["artifact_id"]
    answer, events = _ask(owner, "what columns did version 1 have?", version=1)
    assert _meta(events)["artifact_answer"]["version"] == 1
    assert "`Start Date`" in answer
    assert len(_versions(owner, artifact_id)) == 1


# --------------------------------------------------------------- judgement --


def test_a_judgement_question_spends_one_model_call_over_the_fenced_digest(owner, monkeypatch):
    """"is this any good?" is not a fact the spec holds, so the model answers
    it FROM the spec: one call, the digest as its material, the fencing
    intact — and still no version."""
    from app import continuation

    seen = {}

    async def fake_stream(messages, *, on_delta, **kw):
        seen["messages"] = list(messages)
        seen["kw"] = dict(kw)
        seen["calls"] = seen.get("calls", 0) + 1
        await on_delta("token", "It covers the stages but has no owner for two rows.")
        return type("R", (), {"truncated": False, "segment_count": 1, "stop_reason": ""})()

    _, first = _create(owner, monkeypatch)
    artifact_id = _meta(first)["artifacts"][0]["artifact_id"]
    monkeypatch.setattr(continuation, "stream_long_completion", fake_stream)

    answer, events = _ask(owner, "is this tracker any good?")
    assert answer == "It covers the stages but has no owner for two rows."
    assert seen["calls"] == 1, "one call, not two"
    assert seen["kw"]["max_segments"] == 1
    system, user = seen["messages"]
    assert system["role"] == "system" and D.SECURITY_NOTE in system["content"]
    assert D.DATA_START in user["content"] and D.DATA_END in user["content"]
    assert "Workflow | columns (6): Stage, Owner, Status, Start Date, Due Date, Notes | rows: 5" in user["content"]
    assert user["content"].index(D.DATA_END) < user["content"].index("The question:")
    assert "signed off" not in user["content"], "the digest is structure, not cell values"
    assert _meta(events)["artifact_answer"]["model_used"] is True
    assert len(_versions(owner, artifact_id)) == 1


def test_a_judgement_question_falls_back_to_the_facts_when_the_model_cannot_be_reached(owner, monkeypatch):
    """The model is the only thing that can give an opinion; when it is not
    there the facts are still true, and the turn still writes nothing."""
    from app import continuation

    async def boom(*a, **k):
        raise RuntimeError("engine unavailable")

    _, first = _create(owner, monkeypatch)
    artifact_id = _meta(first)["artifacts"][0]["artifact_id"]
    monkeypatch.setattr(continuation, "stream_long_completion", boom)

    answer, events = _ask(owner, "what do you think of it?")
    assert "`Workflow`" in answer and "`Summary`" in answer
    assert _meta(events)["artifact_answer"]["model_used"] is False
    assert len(_versions(owner, artifact_id)) == 1


# ------------------------------------------------------------------- seams --


def test_a_question_with_no_artifact_in_the_conversation_says_so_and_makes_nothing(owner, monkeypatch):
    _install(monkeypatch)
    answer, events = _ask(owner, "what does this sheet have?", conv="conv-empty")
    assert "no file in this conversation" in answer
    assert "artifacts" not in _meta(events)
    assert adb.list_artifacts(owner, "conv-empty") == []


def test_an_ambiguous_question_asks_which_file_without_promising_a_change(owner, monkeypatch):
    """pick_artifact's own clarification reads "Which one should I change" —
    a promise this turn must not make."""
    _install(monkeypatch)
    for gen in ("ga", "gb"):
        events, emit = _events()
        asyncio.run(engine.run_artifact_engine(
            "Make sheet for Me ??", [], emit,
            intent=I.ArtifactIntent("create", rule="create", instruction="Make sheet for Me ??", new_artifact=True),
            conversation_id="conv-two", user_id=owner, generation_id=gen, effort="fast"))
    assert len(adb.list_artifacts(owner, "conv-two")) == 2

    answer, events = _ask(owner, "what does the tracker have?", conv="conv-two",
                          reference="named", reference_hint="tracker")
    assert answer.startswith("Which one do you mean — ")
    assert "change" not in answer
    assert "artifacts" not in _meta(events)


def test_the_flag_only_verdict_reaches_the_answer_too(owner, monkeypatch):
    """The gate may record the verdict as `action="answer"` or as a boolean
    beside `action="none"`. Both must reach this path, because which one lands
    is the other track's choice."""
    _create(owner, monkeypatch)
    for flag in D.ANSWER_FLAGS:
        events, emit = _events()
        intent = I.ArtifactIntent("none", rule="no-request", instruction="what columns does it have?")
        setattr(intent, flag, True)
        answer = asyncio.run(engine.run_artifact_engine(
            "what columns does it have?", [], emit, intent=intent, conversation_id=CONV,
            user_id=owner, generation_id="gf", effort="fast"))
        assert "`Due Date`" in answer, flag


def test_an_unreadable_spec_still_answers_from_the_row(owner, monkeypatch):
    """The spec.json is gone from the volume. The formats on the row are
    still true; "it has no sheets" would be a fabricated fact."""
    _, first = _create(owner, monkeypatch)
    ref = _meta(first)["artifacts"][0]
    os.unlink(os.path.join(settings.reports_dir, "artifacts", str(owner), ref["artifact_id"], "v1", T.SPEC_NAME))

    answer, events = _ask(owner, "what does this sheet have?")
    assert "can't read its contents back" in answer
    assert "XLSX" in answer
    assert _meta(events)["artifact_answer"]["grounded"] is False
    assert len(_versions(owner, ref["artifact_id"])) == 1


def test_the_outcome_is_counted_under_a_label_that_survives_the_allowlist(owner, monkeypatch):
    """metrics._ALLOWED bounds `result` to one shared vocabulary and folds
    anything else to "other", so these outcomes travel as `answered`."""
    _create(owner, monkeypatch)
    _ask(owner, "what columns does it have?")
    lines = [l for l in metrics.render().splitlines() if l.startswith("artifact_answers_total{")]
    assert lines == ['artifact_answers_total{answered="fact",kind="workbook"} 1']
