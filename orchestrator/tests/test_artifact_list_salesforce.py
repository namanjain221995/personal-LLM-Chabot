"""A toggle-off turn never reaches a file a Salesforce-mode job made
(app/artifacts/db.py `list_artifacts(exclude_salesforce=)`, design_v2 G5/C4).

The rule is `artifact_jobs.mode`, written by the server from the request, on
ANY job of the artifact: every version counts, and no message timestamp is
read (a client PUT resets them all).
"""
from __future__ import annotations

import asyncio
import uuid

from app import db
from app.artifacts import db as adb


def _job(user_id: int, conv: str, *, mode: str, artifact_id: str = "", operation: str = "create", parent=None) -> str:
    artifact_id = artifact_id or uuid.uuid4().hex
    row = adb.create_artifact_job(
        job_id=uuid.uuid4().hex, artifact_id=artifact_id, user_id=user_id, conversation_id=conv,
        generation_id=uuid.uuid4().hex, operation=operation, instruction="make a sheet", kind="workbook",
        requested_formats=["xlsx"], selected_formats=["xlsx"], format_reason="asked", effort="fast",
        mode=mode, template_id="generic", idempotency_key=uuid.uuid4().hex, parent_version=parent,
        title=f"{mode} sheet",
    )
    assert row is not None and row["created"]
    return artifact_id


def test_an_artifact_with_any_salesforce_mode_job_is_left_out_on_request():
    uid = db.create_user("art-owner", "hash")
    db.create_conversation(uid, "art-chat", "Files")
    assistant = _job(uid, "art-chat", mode="assistant")
    salesforce = _job(uid, "art-chat", mode="salesforce")
    mixed = _job(uid, "art-chat", mode="assistant")
    _job(uid, "art-chat", mode="salesforce", artifact_id=mixed, operation="edit")
    every = {a["id"] for a in adb.list_artifacts(uid, "art-chat")}
    assert every == {assistant, salesforce, mixed}, "the default keeps every file (the UI listing)"
    kept = {a["id"] for a in adb.list_artifacts(uid, "art-chat", exclude_salesforce=True)}
    assert kept == {assistant}
    # Across chats too: the filter is per artifact, not per conversation.
    assert {a["id"] for a in adb.list_artifacts(uid, None, exclude_salesforce=True)} == {assistant}


def test_the_artifact_engine_lists_without_salesforce_files_on_a_toggle_off_turn(monkeypatch):
    """C4: the engine reads the list itself; `pick_artifact` falls back to the
    newest and a question about a sheet is answered from its stored spec."""
    from app.artifacts.intent import decide
    from app.engines import artifact as engine

    seen: list = []

    def spy(user_id, conversation_id, limit=100, **kwargs):
        seen.append(kwargs)
        return []

    monkeypatch.setattr(engine.adb, "list_artifacts", spy)

    async def emit(event, data):
        pass

    intent = decide("what does this sheet have?", has_artifacts=True)
    for mode in ("assistant", "salesforce"):
        try:
            asyncio.run(engine.run_artifact_engine(
                "what does this sheet have?", [], emit, intent=intent, conversation_id="c", user_id=1,
                generation_id="g", mode=mode,
            ))
        except Exception:  # noqa: BLE001 — only the list call is under test
            pass
    assert seen[0] == {"exclude_salesforce": True}
    assert seen[1] == {}, "a Salesforce-mode turn lists exactly as before"
