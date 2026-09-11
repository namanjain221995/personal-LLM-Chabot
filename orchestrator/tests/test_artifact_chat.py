"""POST /chat → the artifact branch, through the real app.

The engine and the API have their own tests; this one covers the wiring in
main.py that only a whole turn exercises — the intent gate deciding on the
resolved text, the branch sitting above plain chat, the busy-probe flag,
the ids the engine is handed (the e2e run of 2026-09-11 found
`gen.id` where the attribute is `generation_id`: a crash the unit tests
could not see), the single final meta, and the durable answer row carrying
`meta.artifacts` so a reload rebuilds the card.
"""
from __future__ import annotations

import hashlib
import json
import os

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
