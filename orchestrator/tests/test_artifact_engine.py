"""The artifact engine (engines/artifact.py): a chat turn that asks for a file.

Offline against the private test database: the composer is a stub the
pipeline calls, the render subprocess writes real bytes into the working
directory, and the engine is driven exactly as main.py drives it — so what
is pinned here is the turn's contract: steps with the fixed ids, ONE meta
carrying `artifacts[]`, a sentence and never the document, follow-ups that
resolve to the right artifact, refusals said plainly.
"""
from __future__ import annotations

import asyncio
import hashlib
import os

import pytest

from app import db, metrics
from app.artifacts import db as adb
from app.artifacts import intent as I
from app.artifacts import pipeline
from app.artifacts import spec as S
from app.artifacts import types as T
from app.config import settings
from app.engines import artifact as engine


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
    return int(db.create_user("engine-owner", "hash"))


def _spec(kind="document", title="Pricing Update"):
    if kind == "presentation":
        return S.parse_body("presentation", {"title": title, "slides": [{"layout": "title", "title": title}, {"layout": "bullets", "title": "Plan", "bullets": ["a", "b"]}]})
    if kind == "workbook":
        return S.parse_body("workbook", {"title": title, "sheets": [{"name": "Data", "columns": [{"name": "A"}], "rows": [["x"]]}]})
    return S.parse_body("document", {"title": title, "blocks": [{"type": "paragraph", "text": "Team tier to $59."}]})


def _install(monkeypatch, *, composer=None, seen=None):
    async def stub_composer(ctx):
        if seen is not None:
            seen.append({"operation": ctx.operation, "kind": ctx.kind, "formats": ctx.formats, "instruction": ctx.instruction,
                         "parent": ctx.parent_spec, "material": ctx.material, "effort": ctx.effort, "template": ctx.template_id})
        await ctx.progress_stage("intent", "done", "x")
        await ctx.progress_stage("gather", "done", "y")
        await ctx.progress_stage("outline", "skipped", "")
        if composer is not None:
            return await composer(ctx)
        if ctx.operation == "convert" and ctx.parent_spec is not None:
            return ctx.parent_spec
        return _spec(ctx.kind, title="Pricing Update" if ctx.operation == "create" else "Pricing Update (edited)")

    async def fake_render(work_dir, spec, formats, title_slug, version, effort):
        files = []
        for fmt in formats:
            name = f"{title_slug}-v{version}.{fmt}"
            body = f"{fmt} of {spec.title}".encode() * 8
            with open(os.path.join(work_dir, name), "wb") as fh:
                fh.write(body)
            files.append({"format": fmt, "filename": name, "size": len(body), "sha256": hashlib.sha256(body).hexdigest(), "pages": 2 if fmt == "pdf" else None})
        with open(os.path.join(work_dir, T.PREVIEW_PDF_NAME), "wb") as fh:
            fh.write(b"%PDF-1.7 preview")
        return {"files": files, "preview_pdf": T.PREVIEW_PDF_NAME, "preview_kind": "pages", "preview_pages": 2, "warnings": [], "validation": {"reopened": True}, "chart_files": [], "timings": {}}

    pipeline.set_composer(stub_composer)
    monkeypatch.setattr(pipeline, "_render_in_subprocess", fake_render)
    monkeypatch.setattr(pipeline, "_page_count", lambda pdf: 2)
    monkeypatch.setattr(pipeline, "_rasterise_page", lambda pdf, page, width: b"\x89PNG")


def _turn(owner, text, *, history=(), effort="fast", mode="assistant", conv="conv-e", gen="gen-1"):
    events = []

    async def emit(kind, data):
        events.append((kind, data))

    intent = I.decide(text, has_artifacts=bool(_artifacts(owner, conv)), artifact_hints=[a["title"] for a in _artifacts(owner, conv)],
                      has_assistant_answer=any(h.get("role") == "assistant" for h in history))
    answer = asyncio.run(engine.run_artifact_engine(text, list(history), emit, intent=intent, conversation_id=conv, user_id=owner, generation_id=gen, effort=effort, mode=mode))
    return answer, events


def _artifacts(owner, conv):
    return [a for a in adb.list_artifacts(owner, conv) if (a.get("current") or {}).get("status") in ("completed", "completed_with_warnings")]


def _meta(events):
    metas = [d for k, d in events if k == "meta"]
    assert len(metas) == 1, f"exactly one meta, got {len(metas)}"
    return metas[0]


# ------------------------------------------------------------------ create --


def test_a_request_for_a_file_produces_one_meta_with_the_reference(owner, monkeypatch):
    seen = []
    _install(monkeypatch, seen=seen)
    answer, events = _turn(owner, "Create a PDF about the pricing change.", history=[{"role": "user", "content": "prices are changing"}])
    meta = _meta(events)
    assert meta["route"] == "artifact" and len(meta["artifacts"]) == 1
    ref = meta["artifacts"][0]
    assert ref["status"] == "completed" and ref["kind"] == "document" and ref["version"] == 1
    assert [f["format"] for f in ref["files"]] == ["pdf"], "explicit PDF wins"
    # CONTRACT-2 §2: a file is addressed by its code-minted id (the
    # `/file/{format}` alias stays for refs persisted before ids existed).
    pdf = ref["files"][0]
    assert T.is_file_id(pdf["file_id"]) and pdf["role"] == "companion"
    assert pdf["download_url"] == f"/artifacts/{ref['artifact_id']}/v/1/f/{pdf['file_id']}?disposition=attachment"
    assert "report_files" not in meta, "artifacts do not ride the legacy key"
    # The sentence, not the document.
    assert answer.startswith("Created **Pricing Update** as PDF") and "Team tier" not in answer
    tokens = "".join(d["text"] for k, d in events if k == "token")
    assert tokens == answer
    # Steps carry the fixed ids, and the runner's stages appear after the composer's.
    steps = [d for k, d in events if k == "step"]
    ids = [s["id"] for s in steps]
    assert ids[0] == T.STEP_IDS["intent"] and T.STEP_IDS["render"] in ids and T.STEP_IDS["preview"] in ids
    assert all(s["title"] == T.STAGE_TITLES[[k for k, v in T.STEP_IDS.items() if v == s["id"]][0]] for s in steps)
    # What the composer was handed.
    assert seen[0]["operation"] == "create" and seen[0]["effort"] == "fast"
    assert "prices are changing" in seen[0]["material"]["history_text"]


def test_unnamed_format_follows_the_policy_and_the_template_the_words(owner, monkeypatch):
    seen = []
    _install(monkeypatch, seen=seen)
    _, events = _turn(owner, "Prepare a one-page executive brief for the board.")
    ref = _meta(events)["artifacts"][0]
    assert [f["format"] for f in ref["files"]] == ["pdf", "docx"]
    assert seen[0]["template"] == "brief"


def test_a_deck_request_makes_a_presentation(owner, monkeypatch):
    seen = []
    _install(monkeypatch, seen=seen)
    _, events = _turn(owner, "Build a deck for the CEO.")
    ref = _meta(events)["artifacts"][0]
    assert ref["kind"] == "presentation" and [f["format"] for f in ref["files"]] == ["pptx", "pdf"]
    assert seen[0]["template"] == "ceo"


# --------------------------------------------------------------- follow-ups --


def test_an_edit_makes_version_two_of_the_same_artifact(owner, monkeypatch):
    seen = []
    _install(monkeypatch, seen=seen)
    _, first = _turn(owner, "Create a PDF about the pricing change.", gen="g1")
    ref1 = _meta(first)["artifacts"][0]
    answer, second = _turn(owner, "Make it shorter.", gen="g2")
    ref2 = _meta(second)["artifacts"][0]
    assert ref2["artifact_id"] == ref1["artifact_id"] and ref2["version"] == 2 and ref2["parent_version"] == 1
    assert ref2["operation"] == "edit" and [f["format"] for f in ref2["files"]] == ["pdf"], "an edit keeps the formats"
    assert seen[1]["operation"] == "edit" and seen[1]["parent"] is not None and seen[1]["parent"].title == "Pricing Update"
    assert seen[1]["instruction"] == "Make it shorter."
    assert answer.startswith("Updated **Pricing Update (edited)**")
    # Version 1 is still there, untouched.
    v1 = adb.get_version(ref1["artifact_id"], 1, owner)
    assert v1["status"] == "completed" and os.path.isfile(os.path.join(settings.reports_dir, "artifacts", str(owner), ref1["artifact_id"], "v1", v1["files"][0]["filename"]))


def test_a_conversion_renders_the_stored_content_without_the_model(owner, monkeypatch):
    seen = []
    _install(monkeypatch, seen=seen)
    _, first = _turn(owner, "Create a PDF about the pricing change.", gen="g1")
    ref1 = _meta(first)["artifacts"][0]
    answer, second = _turn(owner, "Also as Word.", gen="g2")
    ref2 = _meta(second)["artifacts"][0]
    assert ref2["artifact_id"] == ref1["artifact_id"] and ref2["version"] == 2 and ref2["operation"] == "convert"
    assert [f["format"] for f in ref2["files"]] == ["docx"]
    assert seen[1]["operation"] == "convert" and seen[1]["parent"].title == "Pricing Update"
    assert answer.startswith("Converted **Pricing Update** to Word")


def test_a_conversion_the_kind_cannot_take_is_refused_in_a_sentence(owner, monkeypatch):
    _install(monkeypatch)
    _turn(owner, "Build a deck for the CEO.", gen="g1")
    answer, events = _turn(owner, "Convert it to Excel.", gen="g2")
    assert "cannot be converted to Excel" in answer and "PowerPoint or PDF" in answer
    meta = _meta(events)
    assert "artifacts" not in meta
    assert len(_artifacts(owner, "conv-e")) == 1, "nothing new was made"


def test_asking_for_a_workbook_after_a_deck_makes_a_new_workbook(owner, monkeypatch):
    """The first e2e run (2026-09-11): "Turn this into an Excel tracker…"
    after a deck was read as CONVERT the deck and refused. The content is
    the conversation's; a new workbook is what was asked for. Only an
    explicit "convert it to Excel" is refused."""
    _install(monkeypatch)
    _turn(owner, "Build a deck for the CEO.", gen="g1")
    answer, events = _turn(owner, "Turn this into an Excel tracker with the three plans and a total row.", gen="g2")
    ref = _meta(events)["artifacts"][0]
    assert ref["kind"] == "workbook" and ref["operation"] == "create" and [f["format"] for f in ref["files"]] == ["xlsx"]
    assert len(_artifacts(owner, "conv-e")) == 2
    answer, events = _turn(owner, "Also give me this as Excel.", gen="g3")
    assert _meta(events)["artifacts"][0]["kind"] == "workbook", "a wish for Excel after a deck is a new workbook, not a refusal"
    answer, events = _turn(owner, "Convert the deck to Excel.", gen="g4")
    assert "cannot be converted to Excel" in answer


def test_the_named_artifact_is_edited_not_the_latest(owner, monkeypatch):
    seen = []

    async def composer(ctx):
        return _spec(ctx.kind, title="Pricing SOP" if "sop" in ctx.instruction.lower() else ("Q3 deck" if ctx.kind == "presentation" else "Pricing SOP (edited)"))

    _install(monkeypatch, composer=composer, seen=seen)
    _turn(owner, "Create an SOP document for pricing changes.", gen="g1")
    _turn(owner, "Build a Q3 deck for the board.", gen="g2")
    sop, deck = sorted(_artifacts(owner, "conv-e"), key=lambda a: a["kind"], reverse=True)[1], None
    answer, events = _turn(owner, "Make the Pricing SOP shorter.", gen="g3")
    ref = _meta(events)["artifacts"][0]
    assert ref["kind"] == "document" and ref["version"] == 2, "the SOP, not the newer deck"
    assert seen[-1]["parent"].title == "Pricing SOP"


def test_an_edit_with_nothing_to_edit_creates(owner, monkeypatch):
    _install(monkeypatch)
    intent = I.ArtifactIntent("edit", reference="latest", rule="edit", instruction="Make it shorter.")
    events = []

    async def emit(kind, data):
        events.append((kind, data))

    asyncio.run(engine.run_artifact_engine("Make it shorter.", [], emit, intent=intent, conversation_id="conv-e", user_id=owner, generation_id="g1"))
    assert _meta(events)["artifacts"][0]["operation"] == "create"


# ---------------------------------------------------------------- refusals --


def test_a_refused_acceptance_is_said_and_nothing_is_written(owner, monkeypatch):
    _install(monkeypatch)
    monkeypatch.setattr(settings, "artifact_user_quota_mb", 0)
    answer, events = _turn(owner, "Create a PDF about the pricing change.")
    assert "storage is full" in answer.lower()
    meta = _meta(events)
    assert "artifacts" not in meta and adb.list_artifacts(owner, "conv-e") == []


def test_a_failed_job_is_reported_with_its_safe_error_and_the_reference(owner, monkeypatch):
    async def composer(ctx):
        raise pipeline.StageFailure("model_failure", "The model could not produce a valid document structure.")

    _install(monkeypatch, composer=composer)
    answer, events = _turn(owner, "Create a PDF about the pricing change.")
    assert answer.startswith("I couldn't finish the file:") and "valid document structure" in answer
    ref = _meta(events)["artifacts"][0]
    assert ref["status"] == "failed" and ref["files"] == [] and ref["status_url"].startswith("/artifacts/jobs/")
    assert "Traceback" not in answer and "/reports" not in answer


# ---------------------------------------------------------------- material --


def test_export_of_the_previous_answer_carries_it_as_material(owner, monkeypatch):
    seen = []
    _install(monkeypatch, seen=seen)
    history = [{"role": "user", "content": "explain our pricing"}, {"role": "assistant", "content": "Our pricing has three tiers: Free, Team ($59) and Enterprise."}]
    _turn(owner, "Export the previous answer as PDF.", history=history)
    # `ctx.material` is read back from material.json — the round trip the
    # composer depends on, not the dict the engine handed to accept().
    material = seen[0]["material"]
    assert "three tiers" in material["previous_answer"]
    assert isinstance(material["notes"], list) and material["notes"], "the engine's notes (audience, mode, date) reach the composer"
    assert seen[0]["operation"] == "create"


def test_the_pipeline_composer_announces_its_stages_and_maps_compose(owner, monkeypatch):
    """`compose_for_pipeline` — the composer installed at startup — is driven
    with the real ComposeContext and a stubbed compose.compose."""
    from app.artifacts import compose as C

    seen = {}

    async def fake_compose(req, *, progress=None):
        seen["req"] = req
        if progress is not None:
            await progress(10.0, "outlining")
            await progress(30.0, "writing")
        return C.ComposeResult(spec=_spec("document"), warnings=["the deck was trimmed"], corrections=2)

    monkeypatch.setattr(C, "compose", fake_compose)
    stages = []

    async def render(work_dir, spec, formats, title_slug, version, effort):
        raise AssertionError("not reached")

    class _Ctx:
        pass

    # Drive the pipeline's real ComposeContext through a real job.
    pipeline.set_composer(engine.compose_for_pipeline)
    monkeypatch.setattr(pipeline, "_render_in_subprocess", render)
    job = pipeline.accept(user_id=owner, conversation_id="conv-e", generation_id="g1", operation="create", instruction="Write a brief", kind="document",
                          formats=["pdf"], effort="think", mode="assistant", template_id="brief", material={"history_text": "hello", "sources": [{"id": "s1", "title": "T", "text": "body", "url": "", "retrieved_at": "", "kind": "web"}]})
    q = pipeline.subscribe(job["id"])

    async def run():
        await pipeline.ensure_running(job["id"])
        row = await pipeline.wait_for(job["id"])
        while not q.empty():
            stages.append(q.get_nowait())
        return row

    row = asyncio.run(run())
    assert row["status"] == "failed"  # the render stub raises: fine — compose ran first
    req = seen["req"]
    assert req.kind == "document" and req.effort == "think" and req.template_id == "brief"
    assert req.material.history_text == "hello" and req.material.sources[0].id == "s1"
    names = [(e.get("stage"), e.get("status")) for e in stages]
    assert ("intent", "done") in names and ("gather", "done") in names and ("outline", "running") in names and ("outline", "done") in names


# --------------------------------------------------------------- visual QA --


def test_max_effort_runs_one_visual_correction_pass_and_no_more(owner, monkeypatch):
    """The reviewer sees rendered pages, returns a revised spec, and the
    render/validate/preview stages run again exactly once — bounded, counted,
    and recorded as a warning on the version. A reviewer that finds nothing
    changes nothing."""
    renders = []
    seen_pages = []

    async def composer(ctx):
        return _spec("document", title="Draft")

    def _install_here():
        _install(monkeypatch, composer=composer)
        real_render = pipeline._render_in_subprocess

        async def counting_render(work_dir, spec, formats, title_slug, version, effort):
            renders.append(spec.title)
            return await real_render(work_dir, spec, formats, title_slug, version, effort)

        monkeypatch.setattr(pipeline, "_render_in_subprocess", counting_render)
        monkeypatch.setattr(pipeline, "_page_count", lambda pdf: 3)
        monkeypatch.setattr(pipeline, "_rasterise_page", lambda pdf, page, width: b"\x89PNG-" + str(page).encode())

    _install_here()
    monkeypatch.setattr(settings, "artifact_qa_pages", 2)

    async def reviewer(ctx, spec, pages):
        seen_pages.append(list(pages))
        return _spec("document", title="Draft (fixed)")

    pipeline.set_visual_reviewer(reviewer)
    try:
        answer, events = _turn(owner, "Create a PDF about the pricing change.", effort="max")
    finally:
        pipeline.set_visual_reviewer(None)
    ref = _meta(events)["artifacts"][0]
    assert ref["status"] == "completed_with_warnings"
    assert renders == ["Draft", "Draft (fixed)"], "rendered twice: once to look, once with the correction"
    assert seen_pages == [[b"\x89PNG-1", b"\x89PNG-2"]], "the reviewer saw ARTIFACT_QA_PAGES pages"
    assert any("visual check" in w for w in ref["warnings"])
    assert answer.startswith("Created **Draft (fixed)**")
    # The published spec is the revised one.
    from app.artifacts import store

    published = store.read_spec(store.version_dir(owner, ref["artifact_id"], 1))
    assert published.title == "Draft (fixed)"


def test_a_clean_visual_review_renders_once_and_fast_never_looks(owner, monkeypatch):
    renders = []

    async def composer(ctx):
        return _spec("document")

    _install(monkeypatch, composer=composer)
    real_render = pipeline._render_in_subprocess

    async def counting_render(work_dir, spec, formats, title_slug, version, effort):
        renders.append(effort)
        return await real_render(work_dir, spec, formats, title_slug, version, effort)

    monkeypatch.setattr(pipeline, "_render_in_subprocess", counting_render)
    calls = {"n": 0}

    async def reviewer(ctx, spec, pages):
        calls["n"] += 1
        return None

    pipeline.set_visual_reviewer(reviewer)
    try:
        _, events = _turn(owner, "Create a PDF about the pricing change.", effort="max", gen="g-max")
        assert _meta(events)["artifacts"][0]["status"] == "completed" and calls["n"] == 1 and renders == ["max"]
        _, events = _turn(owner, "Create a PDF about the other change.", effort="fast", gen="g-fast", conv="conv-fast")
        assert _meta(events)["artifacts"][0]["status"] == "completed" and calls["n"] == 1, "Fast never asks for a visual review"
    finally:
        pipeline.set_visual_reviewer(None)


# ----------------------------------------------------- the review's fixes --


def test_a_failing_visual_correction_keeps_the_good_version(owner, monkeypatch):
    """The revised spec is rendered beside the good files and replaces them
    only once it succeeds; when its render fails the version that already
    passed is published, with a note — never a failed job."""
    renders = []

    async def composer(ctx):
        return _spec("document", title="Good")

    _install(monkeypatch, composer=composer)
    real_render = pipeline._render_in_subprocess

    async def render(work_dir, spec, formats, title_slug, version, effort):
        renders.append(spec.title)
        if spec.title == "Revised":
            raise pipeline.RenderFailed("validation_failure", "The document is over the page limit.")
        return await real_render(work_dir, spec, formats, title_slug, version, effort)

    monkeypatch.setattr(pipeline, "_render_in_subprocess", render)

    async def reviewer(ctx, spec, pages):
        return _spec("document", title="Revised")

    pipeline.set_visual_reviewer(reviewer)
    try:
        answer, events = _turn(owner, "Create a PDF about the pricing change.", effort="max")
    finally:
        pipeline.set_visual_reviewer(None)
    ref = _meta(events)["artifacts"][0]
    assert ref["status"] == "completed_with_warnings" and renders == ["Good", "Revised"]
    assert any("could not be applied" in w for w in ref["warnings"])
    from app.artifacts import store

    assert store.read_spec(store.version_dir(owner, ref["artifact_id"], 1)).title == "Good"
    assert answer.startswith("Created **Good**")


def test_a_resumed_turn_finds_its_job_instead_of_making_a_second_artifact(owner, monkeypatch):
    """A restart mid-turn gives the retry a new generation id under the same
    intent id; the acceptance is keyed on the intent, so one send is one
    artifact."""
    _install(monkeypatch)
    events = []

    async def emit(kind, data):
        events.append((kind, data))

    intent = I.decide("Create a PDF about the pricing change.")
    first = asyncio.run(engine.run_artifact_engine("Create a PDF about the pricing change.", [], emit, intent=intent, conversation_id="conv-e", user_id=owner, generation_id="gen-A", intent_id="intent-1"))
    events.clear()
    asyncio.run(engine.run_artifact_engine("Create a PDF about the pricing change.", [], emit, intent=intent, conversation_id="conv-e", user_id=owner, generation_id="gen-B", intent_id="intent-1"))
    refs = _meta(events)["artifacts"]
    assert len(_artifacts(owner, "conv-e")) == 1, "one send, one artifact"
    assert refs[0]["version"] == 1


def test_open_jobs_are_capped_per_person(owner, monkeypatch):
    _install(monkeypatch)
    monkeypatch.setattr(settings, "artifact_max_open_jobs_per_user", 1)
    from app.artifacts import db as adb

    # One job parked in the queue (never started), then a second send.
    pipeline.accept(user_id=owner, conversation_id="conv-e", generation_id="parked", operation="create", instruction="park", kind="document", formats=["pdf"], effort="fast", mode="assistant", template_id="generic", material={})
    answer, events = _turn(owner, "Create a PDF about the pricing change.", gen="g2")
    assert "being built" in answer and "artifacts" not in _meta(events)
    assert adb.count_open_jobs(owner) == 1


# ------------------------------------------- CONTRACT-2 scenarios (wave 2c) --
#
# Scenario A: "Create a CSV of 500 sample customers" → a generator recipe,
# 500 code-made rows, a CSV validated by reopening, the dataset sentence.
# Scenario C: the 34-row audit table pasted into the turn → material.tables
# with every blank kept, four files from one spec, the transform sentence.
# Both run the REAL renderer in-process (the pipeline's subprocess step is
# the one thing stubbed), so the CSV the pipeline counts is the one the
# writer wrote.

from pathlib import Path  # noqa: E402

from app.artifacts import compose as C  # noqa: E402
from app.artifacts import render as R  # noqa: E402

_AUDIT = Path(__file__).parent / "fixtures" / "audit_paste.txt"
_AUDIT_COLUMNS = ["Host", "Candidate", "Date", "Session ID", "Meeting ID", "Interview Duration (min)",
                  "Ratio of Interview Post-Session", "Outcome", "Audit Comments"]


def _real_render(monkeypatch):
    """The render stage through render_version in a thread — real files,
    real validation — instead of the fake bytes the other tests use."""
    async def render(work_dir, spec, formats, title_slug, version, effort):
        report = await asyncio.to_thread(R.render_version, spec, formats, work_dir, title_slug=title_slug, version=version, effort=effort)
        return report.to_json()

    monkeypatch.setattr(pipeline, "_render_in_subprocess", render)


def _fill(ctx, body: dict) -> S.ArtifactSpec:
    """What the real composer does after the model answers with rows: []
    — copy or generate the rows, then validate (compose._fill_code_made_rows
    is the same function; the model call is the only thing left out)."""
    material = engine._material_from_dict(ctx.material)
    req = C.ComposeRequest(kind=ctx.kind, formats=ctx.formats, template_id=ctx.template_id, effort=ctx.effort, material=material, instruction=ctx.instruction)
    problems = C._fill_code_made_rows(body, req, [])
    assert problems == [], problems
    return S.parse_body("workbook", body)


def test_scenario_a_a_500_row_dataset_is_generated_validated_and_said(owner, monkeypatch):
    pytest.importorskip("weasyprint")
    seen = []

    async def composer(ctx):
        seen.append(engine._material_from_dict(ctx.material))
        assert seen[-1].row_count == 500, "the count reaches the composer as a hard requirement"
        return _fill(ctx, {
            "title": "Sample customers", "template_id": "data",
            "sheets": [{"name": "Customers", "columns": [{"name": "Customer ID"}, {"name": "Name"}, {"name": "Email"}, {"name": "Plan"}, {"name": "Seats", "type": "integer"}],
                        "rows": [], "generator": {"rows": 500, "seed": 3, "columns": [
                            {"name": "Customer ID", "kind": "id", "pattern": "CUST-{n:04d}"}, {"name": "Name", "kind": "name", "unique": True},
                            {"name": "Email", "kind": "email"}, {"name": "Plan", "kind": "choice", "values": ["Free", "Team"]},
                            {"name": "Seats", "kind": "int", "min": 1, "max": 50, "only_when": {"column": "Plan", "in": ["Team"]}}]}}],
        })

    _install(monkeypatch, composer=composer)
    _real_render(monkeypatch)
    answer, events = _turn(owner, "Create a CSV of 500 sample customers with name, email, plan and seats.")
    ref = _meta(events)["artifacts"][0]
    assert ref["status"] == "completed", ref
    assert ref["kind"] == "workbook" and [f["format"] for f in ref["files"]] == ["csv"]
    csv_file = ref["files"][0]
    assert csv_file["rows"] == 500 and csv_file["columns"] == 5 and csv_file["role"] == "data" and csv_file["filename"] == "sample-customers-v1.csv"
    assert csv_file["preview_url"] == f"/artifacts/{ref['artifact_id']}/v/1/grid?file={csv_file['file_id']}"
    assert answer == "Created the CSV dataset with 500 validated records."
    # The bytes on disk: a header and exactly 500 records.
    from app.artifacts import store

    text = Path(store.version_dir(owner, ref["artifact_id"], 1), csv_file["filename"]).read_bytes().decode("utf-8")
    assert text.count("\r\n") == 501 and text.startswith("Customer ID,Name,Email,Plan,Seats\r\nCUST-0001,")
    assert "\r\n,\r\n" not in text


def test_scenario_c_a_pasted_audit_table_is_preserved_across_four_files_and_the_sentence_says_so(owner, monkeypatch):
    pytest.importorskip("weasyprint")
    text = ("Share XLSX, Word, PDF and CSV of this audit. Humanise the comments and highlight the Audit Comments column in red.\n\n"
            + _AUDIT.read_text(encoding="utf-8"))
    seen = []

    async def composer(ctx):
        material = engine._material_from_dict(ctx.material)
        seen.append(material)
        return _fill(ctx, {
            "title": "IR Session Audit", "template_id": "data",
            "sheets": [{"name": "Audit", "columns": [{"name": c} for c in _AUDIT_COLUMNS], "rows": [], "rows_from": "paste1",
                        "rewrite": [{"column": "Audit Comments", "instruction": "concise professional audit comment"}],
                        "style": {"highlight": [{"column": "Audit Comments", "color": "red"}], "wrap": True}}],
        })

    _install(monkeypatch, composer=composer)
    _real_render(monkeypatch)
    answer, events = _turn(owner, text)
    ref = _meta(events)["artifacts"][0]
    assert ref["status"] == "completed", ref
    # The material the composer saw: the table, parsed from the RAW text,
    # 34 rows with every blank kept and the hosts forward-filled.
    material = seen[0]
    assert [t.id for t in material.tables] == ["paste1"]
    table = material.tables[0]
    assert table.columns == _AUDIT_COLUMNS and len(table.rows) == 34
    assert table.rows[1][0] == "Ravi Sharma" and table.rows[2][2] is None and table.rows[8][3] is None
    assert sum(1 for r in table.rows for c in r if c is None) == 19
    assert material.transform["rows"] == 34 and material.transform["blanks"] == 19 and material.transform["forward_filled"] == 25 and material.transform["forward_filled_column"] == "Host"
    assert any("TABLE paste1 was pasted as text: 34 rows × 9 columns" in n for n in material.notes)
    # Four files from one spec, one card each, a zip for all.
    assert [(f["role"], f["format"]) for f in ref["files"]] == [("primary", "xlsx"), ("data", "csv"), ("companion", "docx"), ("companion", "pdf")]
    assert ref["package"] == {"count": 4} and ref["download_all_url"].endswith("/zip")
    csv_file = next(f for f in ref["files"] if f["format"] == "csv")
    # A one-sheet workbook's CSV is the workbook, not "Book — Sheet" (the
    # suffix is for a multi-sheet book only; pipeline._validate_files).
    assert csv_file["rows"] == 34 and csv_file["title"] == "IR Session Audit"
    assert ref["preview_kind"] == "grid" and ref["preview_pages"] >= 1, "the PDF companion gives the grid version its pages"
    # The sentence: the transform, never "Updated", the data-only clause.
    assert answer.startswith("Done — I preserved 34 audit rows and created four files. 19 blank source fields stay blank; "
                             "25 host names were filled from the row above; comments were rewritten for clarity without changing the findings."), answer
    assert "The CSV carries the data only" in answer and "Updated" not in answer
    # The CSV on disk keeps the blanks blank and the timestamps as written.
    from app.artifacts import store

    body = Path(store.version_dir(owner, ref["artifact_id"], 1), csv_file["filename"]).read_bytes().decode("utf-8")
    lines = body.split("\r\n")
    assert lines[0] == ",".join(_AUDIT_COLUMNS) and len(lines) == 36 and lines[-1] == ""
    assert lines[3].startswith("Ravi Sharma,Sneha Iyer,,S-1043,") and "00:40:02" in lines[3]


def test_a_table_in_the_previous_turn_is_material_and_a_paste_too_big_is_refused(owner, monkeypatch):
    fixture = _AUDIT.read_text(encoding="utf-8")
    tables_, transform, notes = engine._pasted_tables("Now make it an Excel file.", [{"role": "user", "content": fixture}, {"role": "assistant", "content": "ok"}])
    assert [t.id for t in tables_] == ["paste1"] and len(tables_[0].rows) == 34 and transform["rows"] == 34
    # Two tables: the turn's own first, then the older one.
    small = "Name\tScore\nA\t1\nB\t2\n"
    tables_, transform, _ = engine._pasted_tables(small, [{"role": "user", "content": fixture}])
    assert [t.id for t in tables_] == ["paste1", "paste2"] and len(tables_[0].rows) == 2 and len(tables_[1].rows) == 34
    assert transform["rows"] == 2, "the transform reports the first table"
    # Nothing table-shaped: nothing.
    assert engine._pasted_tables("Create a brief.", [{"role": "user", "content": "hello"}]) == ([], {}, [])
    # Past the paste cap: refused in a sentence, no job.
    from app.artifacts import tables as X

    monkeypatch.setattr(X, "MAX_PASTE_BYTES", 2000)
    _install(monkeypatch)
    answer, events = _turn(owner, "Create a CSV of this audit.\n\n" + fixture)
    assert answer.startswith("The pasted table has") and "Attach it as a file" in answer
    assert "artifacts" not in _meta(events) and adb.list_artifacts(owner, "conv-e") == []


def test_the_material_round_trip_keeps_the_transform_and_the_row_count():
    """Through the engine's own dict AND through pipeline._material, which
    whitelists keys (wave 3) — the count rides as a note and the transform
    on the pasted table's dict until the pipeline passes them through."""
    m = C.Material(instruction="x", tables=[C.DataTable("paste1", "t", ["a"], [["1"], [None]])], transform={"rows": 2, "blanks": 1}, row_count=500,
                   notes=[engine._ROW_COUNT_NOTE.format(n=500)])
    back = engine._material_from_dict(engine._material_dict(m))
    assert back.transform == {"rows": 2, "blanks": 1} and back.row_count == 500 and back.tables[0].rows == [["1"], [None]]
    through_pipeline = engine._material_from_dict(pipeline._material(engine._material_dict(m)))
    assert through_pipeline.transform == {"rows": 2, "blanks": 1} and through_pipeline.row_count == 500
    assert engine._material_from_dict({}).row_count is None and engine._material_from_dict({"row_count": True}).row_count is None


def test_upload_tables_read_a_csv_from_the_workspace_or_the_profiles_full_rows(tmp_path, monkeypatch):
    conv = "conv-up"
    up_id = "0123456789abcdef0123456789abcdef"
    root = tmp_path / "uploads" / conv / up_id / "extracted"
    root.mkdir(parents=True)
    # Two blank lines: csv.reader yields [] for them, and until 2026-09-12
    # they became all-None rows that the XLSX validator could not count
    # (a reopened sheet has no trailing empty rows) — a refused render.
    (root / "leads.csv").write_text("Id,Name,Score\n007,Asha,9\n\n008,,7\n\n", encoding="utf-8")
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    profiles = [
        {"file": "leads.csv", "kind": "table", "rows": 2, "columns": [{"name": "Id"}, {"name": "Name"}, {"name": "Score"}]},
        {"file": "gone.csv", "kind": "table", "rows": 1, "columns": [{"name": "k"}, {"name": "v"}], "full_content": True, "full_rows": [{"k": "a", "v": ""}]},
        {"file": "big.csv", "kind": "table", "rows": 5000, "columns": [{"name": "k"}], "sample_rows": [{"k": "x"}]},
        {"file": "notes.txt", "kind": "other"},
    ]
    monkeypatch.setattr(db, "get_uploads", lambda c: [{"id": up_id, "filename": "leads.csv", "status": "ready", "profile": profiles}] if c == conv else [])
    tables_ = engine._upload_tables(conv)
    assert [t.id for t in tables_] == ["upload1", "upload2"]
    assert tables_[0].columns == ["Id", "Name", "Score"] and tables_[0].rows == [["007", "Asha", "9"], ["008", None, "7"]], "from the file: leading zeros and blanks kept"
    assert tables_[1].rows == [["a", None]], "the file is gone: the profile's full rows stand in"
    assert not any(t.title == "big.csv" for t in tables_), "a sampled profile is not a dataset"


def _ref(files, *, kind="workbook", title="IR Session Audit"):
    return T.ArtifactRef(artifact_id="a" * 32, version=1, job_id="j" * 32, title=title, kind=kind, status="completed", files=files)


def test_the_sentence_follows_the_contract_and_never_says_updated_on_a_create():
    """CONTRACT-2 §7: create / edit / convert verbs; the dataset, the
    multi-file, the transform and the data-only clauses."""
    csv = T.FileRef(format="csv", filename="x.csv", mime_type="text/csv", size=1, role="data", rows=500)
    xlsx = T.FileRef(format="xlsx", filename="x.xlsx", mime_type="m", size=1, role="primary", rows=500)
    docx = T.FileRef(format="docx", filename="x.docx", mime_type="m", size=1, role="companion")
    pdf = T.FileRef(format="pdf", filename="x.pdf", mime_type="m", size=1, role="companion")
    assert engine._sentence(_ref([csv]), "create", [], dataset=True) == "Created the CSV dataset with 500 validated records."
    assert engine._sentence(_ref([csv, xlsx]), "create", [], dataset=True) == "Created the dataset with 500 validated records as CSV and Excel."
    assert engine._sentence(_ref([xlsx, csv, docx, pdf]), "create", []) == "Created **IR Session Audit** in Excel, CSV, Word and PDF."
    assert engine._sentence(_ref([pdf], kind="document", title="Pricing Update"), "create", []) == "Created **Pricing Update** as PDF."
    assert engine._sentence(_ref([pdf], kind="document", title="Pricing Update"), "edit", []) == "Updated **Pricing Update** as PDF."
    assert engine._sentence(_ref([docx], kind="document", title="Pricing Update"), "convert", []) == "Converted **Pricing Update** to Word."
    transform = {"rows": 34, "blanks": 34, "forward_filled": 25, "forward_filled_column": "Host", "rewritten": True, "rewrite_columns": ["Audit Comments"]}
    assert engine._sentence(_ref([xlsx, csv, docx, pdf]), "create", [], transform=transform, instruction="share xlsx, word, pdf and csv of this audit") == (
        "Done — I preserved 34 audit rows and created four files. 34 blank source fields stay blank; "
        "25 host names were filled from the row above; comments were rewritten for clarity without changing the findings."
    )
    assert engine._sentence(_ref([csv]), "create", [], transform={"rows": 12}, instruction="csv of these rows") == "Done — I preserved 12 rows and created one file."
    # The data-only clause, and the warnings after it; an edit is never re-worded as a create.
    line = engine._sentence(_ref([csv, xlsx]), "create", ["one cell was neutralised"], dataset=True, data_only_note="the CSV carries the data only; the formatting is in the Excel file")
    assert line == "Created the dataset with 500 validated records as CSV and Excel. The CSV carries the data only; the formatting is in the Excel file. _one cell was neutralised._"
    assert engine._sentence(_ref([xlsx, csv]), "edit", [], transform=transform, dataset=True) == "Updated **IR Session Audit** as Excel and CSV."
    # A per-cell note stays on the card; the sentence carries only warnings worth a sentence.
    noisy = ["sheet 'Audit', column 'Audit Comments': row 21: kept the original (rewrite is 80 characters for an original of 26)", "1 rewritten cell kept the original wording"]
    assert engine._sentence(_ref([xlsx, csv, docx, pdf]), "create", noisy) == "Created **IR Session Audit** in Excel, CSV, Word and PDF. _1 rewritten cell kept the original wording._"
    # A format the words table does not know is said by its id, never a KeyError.
    odd = T.FileRef(format="ods", filename="x.ods", mime_type="m", size=1)
    assert engine._sentence(_ref([odd]), "create", []) == "Created **IR Session Audit** as ODS."


def test_an_edit_of_a_pasted_table_workbook_copies_the_rows_again_from_the_parent(owner, monkeypatch):
    """The paste was in the CREATE turn; the edit turn ("rename the sheet")
    carries no table, and the engine gathers pastes only at a create. The
    parent version's sheet IS the table, so compose_for_pipeline puts it
    back under its id (`_tables_from_parent`) and the fill copies the 34
    rows again after the model answers rows: [] — the edit that used to
    fail with "rows_from names no material table"."""
    pytest.importorskip("weasyprint")
    seen = []

    async def composer(ctx):
        # What compose_for_pipeline does before the model is called: the
        # stub composer reads ctx.material the way _fill does, so the
        # parent's table is put into it here.
        material = engine._material_from_dict(ctx.material)
        if ctx.operation == "edit" and ctx.parent_spec is not None:
            engine._tables_from_parent(material, ctx.parent_spec)
            ctx.material["tables"] = [t.__dict__ for t in material.tables]
        seen.append(material)
        name = "Sessions" if ctx.operation == "edit" else "Audit"
        return _fill(ctx, {
            "title": "IR Session Audit", "template_id": "data",
            "sheets": [{"name": name, "columns": [{"name": c} for c in _AUDIT_COLUMNS], "rows": [], "rows_from": "paste1"}],
        })

    _install(monkeypatch, composer=composer)
    _real_render(monkeypatch)
    first, events = _turn(owner, "Create a CSV of this audit.\n\n" + _AUDIT.read_text(encoding="utf-8"), gen="g1")
    ref1 = _meta(events)["artifacts"][0]
    assert ref1["status"] == "completed" and ref1["files"][0]["rows"] == 34
    # The edit turn: no paste in the text, none in the history it is given.
    answer, events = _turn(owner, "Rename the sheet to Sessions.", history=[{"role": "assistant", "content": first}], gen="g2")
    ref2 = _meta(events)["artifacts"][0]
    assert ref2["status"] == "completed", ref2
    assert ref2["artifact_id"] == ref1["artifact_id"] and ref2["version"] == 2 and ref2["operation"] == "edit"
    assert [t.id for t in seen[1].tables] == ["paste1"] and len(seen[1].tables[0].rows) == 34, "the parent's copied sheet is the table again"
    assert ref2["files"][0]["rows"] == 34 and answer.startswith("Updated **IR Session Audit**")
    from app.artifacts import store

    published = store.read_spec(store.version_dir(owner, ref2["artifact_id"], 2))
    sheet = published.body.sheets[0]
    assert sheet.name == "Sessions" and sheet.rows_from == "paste1" and len(sheet.rows) == 34 and sheet.rows[8][3] is None
    # A table the edit turn pasted afresh wins over the parent's.
    fresh = C.Material(instruction="x", tables=[C.DataTable("paste1", "new", ["a"], [["1"]])])
    engine._tables_from_parent(fresh, published)
    assert len(fresh.tables) == 1 and fresh.tables[0].rows == [["1"]]


def test_compose_for_pipeline_puts_the_parents_copied_sheet_back_as_the_table_on_an_edit(monkeypatch):
    """The real composer entry point, driven with a context shaped like the
    pipeline's: on an edit whose material has no tables, the parent's
    rows_from sheet reaches compose() as `paste1`; on a create nothing is
    added."""
    parent = S.parse_body("workbook", {"title": "Audit", "template_id": "data", "sheets": [
        {"name": "Audit", "columns": [{"name": "Host"}, {"name": "Note"}], "rows": [["a", None], ["b", "x"]], "rows_from": "paste1"},
    ]})
    captured = {}

    async def fake_compose(req, *, progress=None):
        captured["req"] = req
        return C.ComposeResult(spec=parent)

    monkeypatch.setattr(C, "compose", fake_compose)

    class Ctx:
        def __init__(self, operation):
            self.material = {"history_text": "", "tables": [], "notes": []}
            self.instruction = "Rename the sheet."
            self.kind, self.formats, self.template_id, self.effort = "workbook", ["csv"], "data", "fast"
            self.operation = operation
            self.parent_spec = parent
            self.budget = T.EFFORT_BUDGETS["fast"]
            self.warnings = []

        async def progress_stage(self, *a):
            pass

        async def progress(self, *a):
            pass

        def warn(self, text):
            self.warnings.append(text)

    asyncio.run(engine.compose_for_pipeline(Ctx("edit")))
    req = captured["req"]
    assert req.operation == "edit" and [t.id for t in req.material.tables] == ["paste1"]
    assert req.material.tables[0].columns == ["Host", "Note"] and req.material.tables[0].rows == [["a", None], ["b", "x"]]
    asyncio.run(engine.compose_for_pipeline(Ctx("create")))
    assert captured["req"].material.tables == []


# ------------------------------------------ security review 2026-09-12 --


def test_the_classifier_hook_and_the_engines_regexes_see_the_decisions_view_of_the_text(owner, monkeypatch):
    """#8: classify_hook returned an intent whose `instruction` was the
    untruncated message, and run_artifact_engine ran formats.decide (a
    regex with a word gap, quadratic over a comma-separated digit run) on
    it on the event loop: 60 KB took seven seconds, a megabyte half an
    hour. The hook bounds its verdict like the rules do, and the engine
    bounds every instruction it runs a regex over."""
    async def sure(text):
        return {"wants_file": True, "kind": "workbook", "confidence": 0.95}

    monkeypatch.setattr(C, "classify_intent", sure)
    huge = "Build a hiring tracker with columns candidate, stage, owner, next step - that is what I need, ok? Data follows: " + "1," * 30000
    verdict = asyncio.run(engine.classify_hook(huge))
    assert verdict is not None and verdict.action == "create"
    assert len(verdict.instruction) <= I._DECIDE_CHARS and verdict.instruction.startswith("Build a hiring tracker")

    seen = {}
    real = engine.F.decide

    def recording(text, **kw):
        seen["len"] = len(text)
        return real(text, **kw)

    monkeypatch.setattr(engine.F, "decide", recording)
    composed = []
    _install(monkeypatch, seen=composed)
    events = []

    async def emit(kind, data):
        events.append((kind, data))

    # An intent that still carries the whole text (an older hook, a
    # caller that built one by hand) is bounded by the engine itself.
    intent = I.ArtifactIntent("create", rule="classifier:model", instruction=huge, raw_text=huge)
    asyncio.run(engine.run_artifact_engine(huge, [], emit, intent=intent, conversation_id="conv-e", user_id=owner, generation_id="gen-h"))
    assert seen["len"] <= I._DECIDE_CHARS
    assert len(composed[0]["instruction"]) <= I._DECIDE_CHARS, "the job's instruction is the decision's view too"
    assert _meta(events)["artifacts"][0]["status"] == "completed"


def test_pasted_tables_are_parsed_off_the_event_loop_and_within_a_byte_budget(owner, monkeypatch):
    """#10: _pasted_tables parsed the current turn plus three history
    turns synchronously on the event loop — four worst-case 5 MB pastes
    held every stream, heartbeat and health probe for 43 seconds. The
    parse now runs in a thread, a history turn over the paste cap is
    never parsed, and the turns are read newest first until the byte
    budget (one paste cap) is spent."""
    import time as _time

    from app.artifacts import tables as X

    real = X.parse_table
    parsed = []

    def slow(text):
        parsed.append(len(text.encode("utf-8")))
        _time.sleep(0.3)
        return real(text)

    monkeypatch.setattr(X, "parse_table", slow)
    _install(monkeypatch)
    small = "Name\tScore\nA\t1\nB\t2\n"
    fixture = _AUDIT.read_text(encoding="utf-8")
    history = [{"role": "user", "content": fixture}, {"role": "assistant", "content": "ok"}, {"role": "user", "content": small}]
    events = []

    async def emit(kind, data):
        events.append((kind, data))

    async def run():
        gaps = []

        async def heartbeat():
            last = _time.monotonic()
            while True:
                await asyncio.sleep(0.01)
                now = _time.monotonic()
                gaps.append(now - last)
                last = now

        beat = asyncio.create_task(heartbeat())
        text = "Now make it an Excel file."
        intent = I.decide(text)
        await engine.run_artifact_engine(text, history, emit, intent=intent, conversation_id="conv-e", user_id=owner, generation_id="gen-p")
        beat.cancel()
        return max(gaps, default=float("inf"))

    stall = asyncio.run(run())
    assert len(parsed) == 3, "the turn and both user turns were looked at"
    assert stall < 0.25, f"the event loop stalled {stall:.2f}s while the tables were parsed"
    assert _meta(events)["artifacts"][0]["status"] == "completed"

    # The byte budget: with the paste cap at 2,000 bytes, the 3 KB fixture
    # in history is never parsed; two 1,100-byte turns spend the budget
    # after the first (the newest).
    parsed.clear()
    monkeypatch.setattr(X, "parse_table", lambda text: (parsed.append(len(text.encode("utf-8"))), real(text))[1])
    monkeypatch.setattr(X, "MAX_PASTE_BYTES", 2000)
    tables_, transform, _ = engine._pasted_tables("Now make it an Excel file.", [{"role": "user", "content": fixture}, {"role": "user", "content": small}])
    assert [t.id for t in tables_] == ["paste1"] and len(tables_[0].rows) == 2
    assert len(fixture.encode("utf-8")) > 2000 and len(fixture.encode("utf-8")) not in parsed, "a history turn over the cap is skipped, not parsed"
    parsed.clear()
    older = "Name\tScore\n" + "\n".join(f"row{i}\t{i}" for i in range(120))
    newer = "Name\tScore\n" + "\n".join(f"new{i}\t{i}" for i in range(120))
    assert 1000 < len(older.encode("utf-8")) < 2000 and len(older) == len(newer)
    tables_, _, _ = engine._pasted_tables("Now make it an Excel file.", [{"role": "user", "content": older}, {"role": "user", "content": newer}])
    assert [t.id for t in tables_] == ["paste1"] and tables_[0].rows[0][0] == "new0", "the newest turn is read first"
    assert parsed == [len("Now make it an Excel file."), len(newer.encode("utf-8"))], "the budget was spent before the older turn"


def test_the_sentence_says_which_rows_the_model_typed_beside_the_preserved_ones():
    """#1: a workbook built from a paste can carry a sheet the model typed
    (a Bonus sheet with an invented row, a dashboard's summary); the
    sentence said only 'preserved N rows'. It names the typed sheets."""
    xlsx = T.FileRef(format="xlsx", filename="x.xlsx", mime_type="m", size=1, role="primary", rows=3)
    transform = {"rows": 3, "blanks": 0, "typed_rows": 1, "typed_sheets": ["Bonus"]}
    line = engine._sentence(_ref([xlsx]), "create", [], transform=transform, instruction="make an excel of this audit")
    assert line == "Done — I preserved 3 audit rows and created one file. 1 row on the Bonus sheet was written by the model, not copied from the paste."
    transform = {"rows": 34, "typed_rows": 5, "typed_sheets": ["Dashboard", "Summary"]}
    line = engine._sentence(_ref([xlsx]), "create", [], transform=transform, instruction="make an excel of these rows")
    assert line.endswith("5 rows on the Dashboard and Summary sheets were written by the model, not copied from the paste.")
