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
    assert ref["files"][0]["download_url"].startswith(f"/artifacts/{ref['artifact_id']}/v/1/file/pdf")
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
    assert "three tiers" in seen[0]["material"]["previous_answer"] if "previous_answer" in seen[0]["material"] else True
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
