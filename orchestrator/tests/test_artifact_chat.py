"""POST /chat → the artifact branch, through the real app.

The engine and the API have their own tests; this one covers the wiring in
main.py that only a whole turn exercises — the intent gate deciding on the
resolved text, the branch sitting above plain chat, the busy-probe flag,
the ids the engine is handed (the e2e run of 2026-09-11 found
`gen.id` where the attribute is `generation_id`: a crash the unit tests
could not see), the single final meta, the durable answer row carrying
`meta.artifacts` so a reload rebuilds the card — and, since CONTRACT-2 §8,
that a request for a file is never remembered as a fact (no
`memory_updated` on the meta, no `user_facts` row) while a plain turn
still is, and that only a PUBLISHED artifact makes a follow-up an edit.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import time

import pytest
from fastapi.testclient import TestClient

from app import db, llm, metrics
from app import main as app_main
from app.artifacts import pipeline
from app.artifacts import spec as S
from app.artifacts import types as T
from app.config import settings
from app.main import _live_generations, app


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
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
        return S.parse_body("document", {"title": "Pricing Update", "blocks": [{"type": "paragraph", "text": "Team tier to $59."}]})

    async def render(work_dir, spec, formats, title_slug, version, effort):
        files = []
        for fmt in formats:
            name = f"{title_slug}-v{version}.{fmt}"
            body = f"{fmt} bytes".encode() * 50
            with open(os.path.join(work_dir, name), "wb") as fh:
                fh.write(body)
            files.append({"format": fmt, "filename": name, "size": len(body), "sha256": hashlib.sha256(body).hexdigest(), "pages": 1 if fmt == "pdf" else None})
        with open(os.path.join(work_dir, T.PREVIEW_PDF_NAME), "wb") as fh:
            fh.write(b"%PDF-1.7 preview")
        return {"files": files, "preview_pdf": T.PREVIEW_PDF_NAME, "preview_kind": "pages", "preview_pages": 1, "warnings": [], "validation": {"reopened": True}, "chart_files": [], "timings": {}}

    # The lifespan installs the real composer; the test wants the stub, so
    # it is installed AFTER the app starts (see the fixture order below).
    monkeypatch.setattr(pipeline, "_render_in_subprocess", render)
    monkeypatch.setattr(pipeline, "_page_count", lambda pdf: 1)
    monkeypatch.setattr(pipeline, "_rasterise_page", lambda pdf, page, width: b"\x89PNG")
    monkeypatch.setattr(app_main, "_stub_composer_for_tests", composer, raising=False)

    async def no_chat(messages, **kwargs):
        yield ("token", "This is a text answer.")

    monkeypatch.setattr(llm, "stream_chat_events", no_chat)
    yield
    pipeline.reset_for_tests()
    pipeline.set_composer(None)
    pipeline.set_visual_reviewer(None)
    _live_generations.clear()
    metrics.reset()


def _parse_sse(text: str):
    events = []
    for block in text.strip().split("\n\n"):
        lines = block.strip().split("\n")
        if len(lines) >= 2 and lines[0].startswith("event: "):
            events.append((lines[0][7:], json.loads(lines[1][6:])))
    return events


def _post(client: TestClient, message: str, *, conv: str, intent: str, effort: str = "fast"):
    return client.post("/chat", json={"message": message, "mode": "assistant", "conversation_id": conv, "intent_id": intent, "effort": effort})


def test_a_request_for_a_file_through_chat_ends_in_one_meta_with_artifacts():
    with TestClient(app) as client:
        pipeline.set_composer(app_main._stub_composer_for_tests)
        resp = _post(client, "Create a PDF about the pricing change.", conv="art-chat-1", intent="int-art-1")
        assert resp.status_code == 200
        events = _parse_sse(resp.text)
        kinds = [k for k, _ in events]
        assert kinds[0] == "meta" and kinds[-1] == "done"
        finals = [d for k, d in events if k == "meta"][1:]
        assert len(finals) == 1, "exactly one engine meta after the leading id meta"
        final = finals[0]
        assert final["route"] == "artifact"
        ref = final["artifacts"][0]
        assert ref["status"] == "completed" and [f["format"] for f in ref["files"]] == ["pdf"]
        assert ref["title"] == "Pricing Update"
        steps = [d for k, d in events if k == "step"]
        assert steps and steps[0]["id"] == T.STEP_IDS["intent"]
        tokens = "".join(d["text"] for k, d in events if k == "token")
        assert tokens.startswith("Created **Pricing Update** as PDF") and "This is a text answer" not in tokens

    # Durable: the answer row carries the reference, so a reload rebuilds the card.
    row = db.get_chat_request("int-art-1")
    assert row is not None and row["status"] == "completed"
    stored = app_main._persisted_answer("art-chat-1", row["generation_id"])
    assert stored is not None and stored["meta"]["artifacts"][0]["artifact_id"] == ref["artifact_id"]
    # The job recorded the generation it belongs to.
    from app.artifacts import db as adb

    job = adb.get_job(ref["job_id"], int(db.get_user_by_username(_owner_name())["id"]))
    assert job is not None and job["generation_id"] == row["generation_id"]


def test_a_question_about_pdfs_is_still_a_text_answer():
    with TestClient(app) as client:
        pipeline.set_composer(app_main._stub_composer_for_tests)
        resp = _post(client, "What is a PDF?", conv="art-chat-2", intent="int-art-2")
        events = _parse_sse(resp.text)
        final = [d for k, d in events if k == "meta"][-1]
        assert final["route"] != "artifact" and "artifacts" not in final
        assert "This is a text answer" in "".join(d["text"] for k, d in events if k == "token")


def test_a_map_request_is_answered_in_chat_by_code_and_opens_no_job():
    """2026-09-16: "plot this on a map", twice, came back as a Word file and a
    PDF. There is no geographic type in chart_spec.CHART_TYPES, so the turn is
    one sentence written by artifacts/visuals.py — and the model's own text
    answer is not used either, because it was the model that invented the
    document."""
    from app.artifacts import db as adb

    with TestClient(app) as client:
        pipeline.set_composer(app_main._stub_composer_for_tests)
        resp = _post(client, "plot this on a map", conv="art-chat-map", intent="int-art-map")
        assert resp.status_code == 200
        events = _parse_sse(resp.text)
        final = [d for k, d in events if k == "meta"][-1]
        assert final["route"] != "artifact" and "artifacts" not in final
        tokens = "".join(d["text"] for k, d in events if k == "token")
        assert tokens.startswith("I can\'t draw a map")
        assert "no geographic chart type" in tokens and "bar chart" in tokens
        assert "This is a text answer" not in tokens

    assert adb.list_artifacts(int(db.get_user_by_username(_owner_name())["id"]), "art-chat-map") == []


def test_documents_off_for_the_deployment_means_text(monkeypatch):
    monkeypatch.setattr(settings, "artifacts_enabled", False)
    with TestClient(app) as client:
        pipeline.set_composer(app_main._stub_composer_for_tests)
        resp = _post(client, "Create a PDF about the pricing change.", conv="art-chat-3", intent="int-art-3")
        final = [d for k, d in _parse_sse(resp.text) if k == "meta"][-1]
        assert final["route"] != "artifact"


def _owner_name() -> str:
    # The ambient identity conftest.as_user resolves a cookie-less client to.
    with db.connection() as con:
        row = con.execute("SELECT username FROM users ORDER BY id LIMIT 1").fetchone()
    return str(row["username"])


# ------------------------------------------------------ memory (CONTRACT-2 §8) --


def _user_facts() -> list:
    with db.connection() as con:
        return [dict(r) for r in con.execute("SELECT user_id, fact FROM user_facts ORDER BY id").fetchall()]


def _fact_stub(monkeypatch, fact: str = "The user's pricing team ships a Team tier at $59.") -> dict:
    """The extractor's model call, answering with one durable fact every
    time it is asked. `calls` counts the asks — an artifact turn must
    never make one."""
    from app import facts

    monkeypatch.setattr(settings, "fact_extraction_enabled", True)
    calls = {"n": 0}

    async def complete(messages, **kw):
        # The router model serves other callers on a turn (the title, the
        # orchestration classifier); only the extractor's own prompt counts.
        if any(facts._EXTRACT_SYSTEM[:40] in str(m.get("content") or "") for m in messages if isinstance(m, dict)):
            calls["n"] += 1
            return json.dumps({"add": [fact], "replace": []})
        return "{}"

    monkeypatch.setattr(llm, "router_chat_completion", complete)
    return calls


def test_an_artifact_turn_never_writes_a_fact_or_shows_the_memory_chip(monkeypatch):
    """The extractor is a prompt ("do not store requests"), not a
    guarantee; with a stub that ALWAYS returns a fact, an artifact turn
    must still end with no `memory_updated` on its meta and no user_facts
    row — the extractor is not even asked."""
    calls = _fact_stub(monkeypatch)
    with TestClient(app) as client:
        pipeline.set_composer(app_main._stub_composer_for_tests)
        resp = _post(client, "Create a PDF about the pricing change we discussed.", conv="art-mem-1", intent="int-mem-1")
        assert resp.status_code == 200
        events = _parse_sse(resp.text)
        final = [d for k, d in events if k == "meta"][-1]
        assert final["route"] == "artifact" and final["artifacts"][0]["status"] == "completed"
        assert "memory_updated" not in final
        # Let a straggler land if one were going to (none should).
        time.sleep(0.3)
    assert _user_facts() == []
    assert calls["n"] == 0, "the extractor was never asked about a file request"
    row = db.get_chat_request("int-mem-1")
    stored = app_main._persisted_answer("art-mem-1", row["generation_id"])
    assert "memory_updated" not in stored["meta"]


def test_a_plain_turn_with_the_same_stub_still_remembers(monkeypatch):
    """The control: the identical stub on a plain chat turn writes the row
    and the chip rides the meta (extraction runs concurrently with the
    answer; the answer here waits for it, so the chip is deterministic)."""
    calls = _fact_stub(monkeypatch)

    async def patient_chat(messages, **kwargs):
        # Wait for the extraction to land, then one more tick so the task's
        # done-callback (which fills memory_state) has run.
        for _ in range(100):
            if _user_facts():
                break
            await asyncio.sleep(0.03)
        await asyncio.sleep(0.05)
        yield ("token", "This is a text answer.")

    monkeypatch.setattr(llm, "stream_chat_events", patient_chat)
    with TestClient(app) as client:
        pipeline.set_composer(app_main._stub_composer_for_tests)
        resp = _post(client, "My pricing team ships a Team tier at $59, remember that.", conv="art-mem-2", intent="int-mem-2")
        assert resp.status_code == 200
        final = [d for k, d in _parse_sse(resp.text) if k == "meta"][-1]
        assert final["route"] != "artifact"
        assert final.get("memory_updated") == ["The user's pricing team ships a Team tier at $59."]
    facts = _user_facts()
    assert len(facts) == 1 and facts[0]["fact"] == "The user's pricing team ships a Team tier at $59."
    assert facts[0]["user_id"] == int(db.get_user_by_username(_owner_name())["id"])
    assert calls["n"] == 1


# ------------------------------------------- has_artifacts from published rows --


def test_a_failed_first_attempt_does_not_turn_the_next_request_into_an_edit(monkeypatch):
    """Wave 3 (CONTRACT-2 §5/§8): the intent gate sees only PUBLISHED
    artifacts. With a failed row in the conversation, "Make the report a
    PDF about …" — an edit when an artifact exists — is decided as a
    create: a NEW artifact, a sentence that starts "Created"."""
    from app.artifacts import db as adb
    from app.artifacts import intent as intent_rules

    with TestClient(app) as client:
        pipeline.set_composer(app_main._stub_composer_for_tests)
        # The ambient identity materialises its user row on the first
        # request that needs one.
        assert client.get("/artifacts", params={"conversation_id": "art-fail-1"}).status_code == 200
        owner = int(db.get_user_by_username(_owner_name())["id"])
        failed = pipeline.accept(user_id=owner, conversation_id="art-fail-1", generation_id="g-failed", operation="create", instruction="Create a PDF",
                                 kind="document", formats=["pdf"], effort="fast", mode="assistant", template_id="generic", material={}, title="Pricing Update")
        adb.set_job_status(failed["id"], "failed", error="boom", failure_category="renderer_failure")
        assert adb.get_artifact(failed["artifact_id"], owner)["current_version"] == 0
        assert adb.list_artifacts(owner, "art-fail-1"), "the failed artifact IS in the listing"

        seen = {}
        real = intent_rules.decide_with_hook

        async def spy(text, hook, **kw):
            seen.update(kw)
            return await real(text, hook, **kw)

        monkeypatch.setattr(intent_rules, "decide_with_hook", spy)
        resp = _post(client, "Make the report a PDF about the pricing change.", conv="art-fail-1", intent="int-fail-1")
        assert resp.status_code == 200
        events = _parse_sse(resp.text)
        final = [d for k, d in events if k == "meta"][-1]
        assert seen["has_artifacts"] is False and seen["artifact_hints"] == []
        assert final["route"] == "artifact"
        ref = final["artifacts"][0]
        assert ref["artifact_id"] != failed["artifact_id"] and ref["operation"] == "create" and ref["status"] == "completed"
        tokens = "".join(d["text"] for k, d in events if k == "token")
        assert tokens.startswith("Created") and "Updated" not in tokens

        # Once a version is PUBLISHED, the same words are an edit of it.
        seen.clear()
        resp = _post(client, "Make the report a PDF about the pricing change.", conv="art-fail-1", intent="int-fail-2")
        assert resp.status_code == 200
        assert seen["has_artifacts"] is True and seen["artifact_hints"] == ["Pricing Update"]


# ================================================================ AS3 ==
# intent-capability (2026-09-15): the production shape through /chat, the
# Salesforce planner's refusals of a file request, the denial backstop, and
# the UI's artifact_id. Messages and answers are authored for the tests.

_AS3_REPORT = (
    "# Sales Onboarding Audit\n\n## 1. Summary\nOnboarding takes 19 working days; the target is 10.\n\n"
    "## 2. Findings\n| # | Area | Finding | Severity |\n|---|---|---|---|\n| 1 | Access | Laptop on day 6 | High |\n"
    "| 2 | Training | Modules unsequenced | Medium |\n\n## 3. Recommendations\n"
    + "\n".join(f"{i}. Recommendation {i} with an owner and a due date." for i in range(1, 12))
)


def _as3_post(client, message, *, conv, intent, history=(), mode="assistant", effort="fast", **extra):
    messages = [*history, {"role": "user", "content": message}]
    body = {"messages": messages, "mode": mode, "conversation_id": conv, "intent_id": intent, "effort": effort, **extra}
    return client.post("/chat", json=body)


@pytest.fixture
def as3_capture(monkeypatch):
    """The stub composer, recording what it was handed."""
    seen = {}

    async def composer(ctx):
        seen["material"] = dict(ctx.material)
        seen["operation"] = ctx.operation
        seen["formats"] = list(ctx.formats)
        await ctx.progress_stage("intent", "done", "")
        return S.parse_body("document", {"title": "Sales Onboarding Audit", "blocks": [{"type": "paragraph", "text": "Report."}]})

    monkeypatch.setattr(settings, "artifact_intent_llm_enabled", False)
    return composer, seen


@pytest.mark.parametrize("thanks", [False, True])
def test_as3_the_production_shape_through_chat_makes_the_docx_of_the_report(as3_capture, thanks):
    composer, seen = as3_capture
    history = [{"role": "user", "content": "audit our sales onboarding and write a detailed report"},
               {"role": "assistant", "content": _AS3_REPORT}]
    if thanks:
        history += [{"role": "user", "content": "thanks"}, {"role": "assistant", "content": "You're welcome!"}]
    with TestClient(app) as client:
        pipeline.set_composer(composer)
        resp = _as3_post(client, "can u just give it in docs.. in standard n classy formatt? provide dox",
                         conv=f"as3-prod-{thanks}", intent=f"int-as3-prod-{thanks}", history=history)
        assert resp.status_code == 200
        events = _parse_sse(resp.text)
    final = [d for k, d in events if k == "meta"][-1]
    assert final["route"] == "artifact", "the gate, not the chat model, answered"
    assert [f["format"] for f in final["artifacts"][0]["files"]] == ["docx"]
    tokens = "".join(d["text"] for k, d in events if k == "token")
    assert tokens.startswith("Created **Sales Onboarding Audit**") and "cannot" not in tokens.lower()
    assert seen["material"]["previous_answer"].startswith("# Sales Onboarding Audit"), "the report, never the pleasantry"


def test_as3_a_same_turn_upload_reaches_the_artifact_material(as3_capture, tmp_path, monkeypatch):
    composer, seen = as3_capture

    async def resolved(request, conv):
        return [("handbook.md", base64.b64encode(b"# Supplier Handbook\n\n## Chapter 1\nObligations.").decode())], [], None

    monkeypatch.setattr(app_main, "_resolve_document_refs", resolved)
    with TestClient(app) as client:
        pipeline.set_composer(composer)
        resp = _as3_post(client, "is file ko professional docx me bana do", conv="as3-upload", intent="int-as3-upload",
                         pdf_uploads=[{"upload_id": "0" * 32, "name": "handbook.md"}])
        events = _parse_sse(resp.text)
    final = [d for k, d in events if k == "meta"][-1]
    assert final["route"] == "artifact"
    # AS3 integration: the engine takes `gathered=`, so the attachment's text
    # rides in the material's uploads_text (the history is no longer padded).
    assert "Supplier Handbook" in seen["material"]["uploads_text"] + seen["material"]["history_text"]
    assert [d["filename"] for d in db.get_documents("as3-upload")] == ["handbook.md"]


def _as3_sf_stub(monkeypatch, *, route: str, text: str, data=None):
    from app.engines import sf_intel

    calls = {"n": 0}

    async def run(message, history, emit, **kw):
        calls["n"] += 1
        await emit("status", {"text": "Planning…"})
        await emit("token", {"text": text})
        meta = {"route": route, "salesforce_mode": "intelligence"}
        if data is not None:
            meta["data"] = data
        await emit("meta", meta)
        return sf_intel.Outcome(handled=True, answer=text)

    monkeypatch.setattr(sf_intel, "run", run)
    monkeypatch.setattr(settings, "salesforce_intelligence_enabled", True)
    return calls


@pytest.mark.parametrize("route,said", [
    ("chat", "I can't help with that request."),                  # DENY
    ("chat", "That is not something the planner supports."),      # UNSUPPORTED
    ("clarify", "Which period should I use?"),                    # ASK_CLARIFICATION
])
def test_as3_salesforce_refusals_of_a_file_request_reach_the_artifact_branch(as3_capture, monkeypatch, route, said):
    composer, _ = as3_capture
    calls = _as3_sf_stub(monkeypatch, route=route, text=said)
    history = [{"role": "user", "content": "audit the pipeline"}, {"role": "assistant", "content": _AS3_REPORT}]
    with TestClient(app) as client:
        pipeline.set_composer(composer)
        resp = _as3_post(client, "give it in docs", conv=f"as3-sf-{route}-{len(said)}", intent=f"int-as3-sf-{route}-{len(said)}",
                         history=history, mode="salesforce")
        events = _parse_sse(resp.text)
    assert calls["n"] == 1, "the planner kept its place"
    final = [d for k, d in events if k == "meta"][-1]
    tokens = "".join(d["text"] for k, d in events if k == "token")
    assert final["route"] == "artifact" and said not in tokens
    assert 'artifact_sf_fallthrough_total{route="%s"} 1' % route in metrics.render()


def test_as3_a_salesforce_sql_export_keeps_its_path(as3_capture, monkeypatch):
    composer, _ = as3_capture
    _as3_sf_stub(monkeypatch, route="sql", text="Here are 12 open opportunities (export attached).", data={"rows": [[1]]})
    with TestClient(app) as client:
        pipeline.set_composer(composer)
        resp = _as3_post(client, "export the open opportunities to excel", conv="as3-sf-sql", intent="int-as3-sf-sql", mode="salesforce")
        events = _parse_sse(resp.text)
    final = [d for k, d in events if k == "meta"][-1]
    assert final["route"] == "sql" and "artifacts" not in final
    assert "12 open opportunities" in "".join(d["text"] for k, d in events if k == "token")


def test_as3_a_salesforce_refusal_of_a_non_file_question_streams_as_before(as3_capture, monkeypatch):
    composer, _ = as3_capture
    _as3_sf_stub(monkeypatch, route="chat", text="I can't show salaries.")
    with TestClient(app) as client:
        pipeline.set_composer(composer)
        resp = _as3_post(client, "show me the salary report of every employee", conv="as3-sf-no", intent="int-as3-sf-no", mode="salesforce")
        events = _parse_sse(resp.text)
    final = [d for k, d in events if k == "meta"][-1]
    assert final["route"] == "chat" and "I can't show salaries." in "".join(d["text"] for k, d in events if k == "token")


def _as3_denying_chat(monkeypatch):
    from app.artifacts import intent as intent_rules

    async def denial(messages, **kwargs):
        yield ("token", "I cannot directly create a .docx file because I am a text-based AI. Copy the text into Word.")

    monkeypatch.setattr(llm, "stream_chat_events", denial)
    real = intent_rules.decide_with_hook

    async def gate_misses(text, hook, **kw):  # the gate misses; the backstop is what is tested
        return intent_rules.ArtifactIntent("none", rule="no-request")

    monkeypatch.setattr(intent_rules, "decide_with_hook", gate_misses)
    return real


def test_as3_the_denial_backstop_makes_the_file_when_the_classifier_is_sure(as3_capture, monkeypatch):
    from app.artifacts import intent_llm

    composer, seen = as3_capture
    _as3_denying_chat(monkeypatch)
    asked = []

    async def sure(text, **kw):
        asked.append(kw)
        return intent_llm.IntentVerdict(action="export", formats=["docx"], target="previous_answer", confidence=0.9)

    monkeypatch.setattr(intent_llm, "classify", sure)
    history = [{"role": "user", "content": "audit onboarding"}, {"role": "assistant", "content": _AS3_REPORT}]
    with TestClient(app) as client:
        pipeline.set_composer(composer)
        resp = _as3_post(client, "hmm word version?", conv="as3-backstop", intent="int-as3-backstop", history=history)
        events = _parse_sse(resp.text)
    metas = [d for k, d in events if k == "meta"]
    final = metas[-1]
    assert final["route"] == "chat" and final["artifacts"][0]["status"] == "completed" and final["artifact_backstop"] is True
    tokens = "".join(d["text"] for k, d in events if k == "token")
    assert "I cannot directly create" in tokens and "Here is the file. Created **Sales Onboarding Audit**" in tokens
    assert len(asked) == 1 and asked[0]["last_answer_head"].startswith("# Sales Onboarding Audit")
    assert 'artifact_denial_rerouted_total{engine="chat"} 1' in metrics.render()
    row = db.get_chat_request("int-as3-backstop")
    stored = app_main._persisted_answer("as3-backstop", row["generation_id"])
    assert stored["meta"]["artifacts"] and "Here is the file." in stored["content"]


def test_as3_the_denial_backstop_only_counts_when_the_classifier_is_not_sure(as3_capture, monkeypatch):
    from app.artifacts import intent_llm

    composer, _ = as3_capture
    _as3_denying_chat(monkeypatch)

    async def unsure(text, **kw):
        return None

    monkeypatch.setattr(intent_llm, "classify", unsure)
    with TestClient(app) as client:
        pipeline.set_composer(composer)
        resp = _as3_post(client, "hmm word version?", conv="as3-backstop-2", intent="int-as3-backstop-2",
                         history=[{"role": "assistant", "content": _AS3_REPORT}])
        events = _parse_sse(resp.text)
    final = [d for k, d in events if k == "meta"][-1]
    assert "artifacts" not in final
    assert 'artifact_denial_seen_total{engine="chat"} 1' in metrics.render()


def test_as3_an_artifact_id_that_is_not_the_viewers_is_ignored(as3_capture, monkeypatch):
    from app.artifacts import intent as intent_rules

    composer, _ = as3_capture
    seen = {}
    real = intent_rules.decide_with_hook

    async def spy(text, hook, **kw):
        seen.update(kw)
        return await real(text, hook, **kw)

    monkeypatch.setattr(intent_rules, "decide_with_hook", spy)
    with TestClient(app) as client:
        pipeline.set_composer(composer)
        resp = _as3_post(client, "make it shorter", conv="as3-aid", intent="int-as3-aid", artifact_id="not-mine-123")
        assert resp.status_code == 200
    assert seen["artifact_id"] is None


# Verifier 2026-09-15: the backstop runs after the answer streamed. A failure
# there keeps the answer and completes the turn; a job without a file card
# never says "Here is the file."
def test_verifier_a_backstop_failure_keeps_the_streamed_answer(as3_capture, monkeypatch):
    from app.artifacts import intent_llm
    from app.engines import artifact as artifact_engine

    composer, _ = as3_capture
    _as3_denying_chat(monkeypatch)

    async def sure(text, **kw):
        return intent_llm.IntentVerdict(action="export", formats=["docx"], target="previous_answer", confidence=0.95)

    async def broken(*a, **kw):
        raise RuntimeError("artifact store down")

    monkeypatch.setattr(intent_llm, "classify", sure)
    monkeypatch.setattr(artifact_engine, "run_artifact_engine", broken)
    history = [{"role": "user", "content": "audit onboarding"}, {"role": "assistant", "content": _AS3_REPORT}]
    with TestClient(app) as client:
        pipeline.set_composer(composer)
        resp = _as3_post(client, "hmm word version?", conv="as3-backstop-fail", intent="int-as3-backstop-fail", history=history)
        events = _parse_sse(resp.text)
    kinds = [k for k, _ in events]
    assert "done" in kinds and "error" not in kinds
    tokens = "".join(d["text"] for k, d in events if k == "token")
    assert "I cannot directly create" in tokens and "Here is the file." not in tokens
    assert db.get_chat_request("int-as3-backstop-fail")["status"] == "completed"
    assert 'artifact_denial_seen_total{engine="chat"}' in metrics.render()


def test_verifier_a_backstop_job_without_a_card_does_not_say_here_is_the_file(as3_capture, monkeypatch):
    from app.artifacts import intent_llm
    from app.engines import artifact as artifact_engine

    composer, _ = as3_capture
    _as3_denying_chat(monkeypatch)

    async def sure(text, **kw):
        return intent_llm.IntentVerdict(action="export", formats=["docx"], target="previous_answer", confidence=0.95)

    async def refused(text, history, emit, **kw):
        line = "That file could not be made right now."
        await emit("token", {"text": line})
        await emit("meta", {"route": "artifact", "effort": "fast"})
        return line

    monkeypatch.setattr(intent_llm, "classify", sure)
    monkeypatch.setattr(artifact_engine, "run_artifact_engine", refused)
    history = [{"role": "user", "content": "audit onboarding"}, {"role": "assistant", "content": _AS3_REPORT}]
    with TestClient(app) as client:
        pipeline.set_composer(composer)
        resp = _as3_post(client, "hmm word version?", conv="as3-backstop-ref", intent="int-as3-backstop-ref", history=history)
        events = _parse_sse(resp.text)
    tokens = "".join(d["text"] for k, d in events if k == "token")
    assert "That file could not be made right now." in tokens and "Here is the file." not in tokens
    assert "artifacts" not in [d for k, d in events if k == "meta"][-1]
