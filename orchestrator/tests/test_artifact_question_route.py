"""The anchor transcript through POST /chat: the second turn makes no file.

The rules are tested in tests/test_artifact_question_not_edit.py. This is the
whole turn — main.py's wiring, the intent gate on the resolved text, the
artifact branch that sits above plain chat — because the production symptom
was a SECOND and a THIRD version of the workbook, and only a whole turn can
show that no job was opened.

Offline: the composer, the renderer and the model are the same stubs
tests/test_artifact_chat.py uses. No engine call, no GPU.
"""
from __future__ import annotations

import hashlib
import json
import os

import pytest
from fastapi.testclient import TestClient

from app import db, llm, metrics
from app import main as app_main
from app.artifacts import db as adb
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
    monkeypatch.setattr(app_main, "_shutting_down", False)
    _live_generations.clear()
    pipeline.reset_for_tests()
    metrics.reset()

    async def composer(ctx):
        await ctx.progress_stage("intent", "done", "")
        return S.parse_body("workbook", {"title": "Workflow Tracker", "sheets": [
            {"name": "Tasks",
             "columns": [{"name": "Task"}, {"name": "Owner"}, {"name": "Status"}],
             "rows": [["Ship", "Ops", "Open"]]},
        ]})

    async def render(work_dir, spec, formats, title_slug, version, effort):
        files = []
        for fmt in formats:
            name = f"{title_slug}-v{version}.{fmt}"
            body = f"{fmt} bytes".encode() * 50
            with open(os.path.join(work_dir, name), "wb") as fh:
                fh.write(body)
            files.append({"format": fmt, "filename": name, "size": len(body),
                          "sha256": hashlib.sha256(body).hexdigest(), "pages": 1 if fmt == "pdf" else None})
        with open(os.path.join(work_dir, T.PREVIEW_PDF_NAME), "wb") as fh:
            fh.write(b"%PDF-1.7 preview")
        return {"files": files, "preview_pdf": T.PREVIEW_PDF_NAME, "preview_kind": "pages", "preview_pages": 1,
                "warnings": [], "validation": {"reopened": True}, "chart_files": [], "timings": {}}

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


def _post(client: TestClient, message: str, *, conv: str, intent: str):
    return client.post("/chat", json={"message": message, "mode": "assistant",
                                      "conversation_id": conv, "intent_id": intent, "effort": "fast"})


def _owner_id() -> int:
    with db.connection() as con:
        row = con.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
    return int(row["id"])


@pytest.mark.parametrize("question,intent_id", [
    ("Ok What This sheet have ??", "int-q-1"),
    ("I said ??? what you create inside the sheet ??? i want to Know ?? please tell me Only Not create d??", "int-q-2"),
])
def test_the_question_after_a_workbook_opens_no_second_job(question, intent_id):
    conv = "art-q-" + intent_id
    with TestClient(app) as client:
        pipeline.set_composer(app_main._stub_composer_for_tests)
        first = _post(client, "OK Make sheet for Me", conv=conv, intent=intent_id + "-a")
        assert first.status_code == 200
        made = [d for k, d in _parse_sse(first.text) if k == "meta"][-1]
        assert made["route"] == "artifact", made
        after_create = adb.list_artifacts(_owner_id(), conv)
        assert len(after_create) == 1, after_create

        second = _post(client, question, conv=conv, intent=intent_id)
        assert second.status_code == 200
        events = _parse_sse(second.text)
        final = [d for k, d in events if k == "meta"][-1]
        assert final["route"] != "artifact", final
        assert "artifacts" not in final, final
        tokens = "".join(d["text"] for k, d in events if k == "token")
        assert not tokens.startswith(("Created ", "Updated ", "Converted ")), tokens[:120]

    # No second artifact, and the one that exists is still at version 1.
    rows = adb.list_artifacts(_owner_id(), conv)
    assert len(rows) == 1, rows
    assert int((rows[0].get("current") or {}).get("version") or rows[0].get("current_version") or 0) == 1, rows
