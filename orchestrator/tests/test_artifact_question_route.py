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
        # THE META CONTRACT, settled 2026-09-27 where this branch met
        # fix/answer-from-the-spec-r2. `route` names the ENGINE that handled the
        # turn, NOT whether a file came out of it: origin/dev's
        # engines/artifact.py already emits `{"route": "artifact"}` on seven
        # paths that produce nothing — the unmakeable-conversion refusal, the
        # which-file question, the bad-format refusal, the oversized paste, the
        # no-table-for-a-chart refusal. The key that means "a file was made" is
        # `artifacts`, and the read-back path never sets it.
        #
        # So this asserts what the docstring above says — no file, no job — and
        # is strictly STRONGER than the `final["route"] != "artifact"` it
        # replaces: that only said "some other engine took it", which a turn
        # that silently answered nothing would also satisfy. This says the turn
        # was ANSWERED, from the artifact that ALREADY EXISTS, at the version it
        # already had, without a model call. A future change that answers from a
        # newly built artifact keeps `route != "artifact"` false and fails here.
        assert "artifacts" not in final, final
        answered = final.get("artifact_answer") or {}
        assert answered, final
        assert str(answered.get("artifact_id")) == str(after_create[0]["id"]), (answered, after_create)
        assert int(answered.get("version") or 0) == 1, answered
        assert answered.get("grounded") is True, answered
        tokens = "".join(d["text"] for k, d in events if k == "token")
        assert not tokens.startswith(("Created ", "Updated ", "Converted ")), tokens[:120]

    # No second artifact, and the one that exists is still at version 1.
    rows = adb.list_artifacts(_owner_id(), conv)
    assert len(rows) == 1, rows
    assert int((rows[0].get("current") or {}).get("version") or rows[0].get("current_version") or 0) == 1, rows


# ---------------------------------------------------------------------------
# THE TURN'S SUBJECT IS NOT ALWAYS THE ARTIFACT (verifier, 2026-09-27).
#
# Two regressions that appear only when fix/question-not-edit-r2 (which makes
# a question `answer_about_artifact`) and fix/answer-from-the-spec-r2 (which
# answers it from the stored spec, in a branch ABOVE the dataset, GitHub,
# crawl and URL routes) are merged together. Neither shows them alone: on
# origin/dev there is no read-back to claim the turn, and on either branch
# alone half the mechanism is missing.
#
# D1 — a question about the person's own uploaded DATASET was answered from
#      the artifact's spec. The read-back holds the artifact's STRUCTURE and
#      never a cell value, so "what is the total spend?" came back as
#      "**Workflow Tracker** (v1) is a workbook with 1 sheet: `Tasks`" and the
#      real number was never computed.
# D2 — a pasted link or a GitHub URL was answered from the artifact's spec and
#      never fetched.
#
# Both are fixed where the ROUTE decides, because only the route knows what
# the conversation holds: main._carries_a_file_to_read gained `link_to_fetch`
# (code — a link a route below can fetch), and describe.answers_from_spec
# gained `has_dataset`, which reads the gate's `names_our_file` to ask whether
# the QUESTION points at the artifact at all.


def _add_dataset(conv: str, name: str = "customers.csv") -> None:
    """A real dataset row. `notes` is neither 'document' nor 'video', which is
    exactly what main.py counts as making this a dataset conversation."""
    import uuid

    db.save_upload(
        uuid.uuid4().hex, conv, name, 4096, "ready",
        profile=json.dumps({"columns": [{"name": "Country"}, {"name": "Spend"}], "rows": 100}),
        notes=None,
    )


def _make_artifact(client: TestClient, conv: str, tag: str):
    pipeline.set_composer(app_main._stub_composer_for_tests)
    first = _post(client, "OK Make sheet for Me", conv=conv, intent=tag + "-a")
    assert first.status_code == 200, first.text
    made = [d for k, d in _parse_sse(first.text) if k == "meta"][-1]
    assert made["route"] == "artifact", made
    rows = adb.list_artifacts(_owner_id(), conv)
    assert len(rows) == 1, rows
    return rows


def _answer_turn(client: TestClient, conv: str, question: str):
    second = _post(client, question, conv=conv, intent=conv + "-q")
    assert second.status_code == 200, second.text
    events = _parse_sse(second.text)
    final = [d for k, d in events if k == "meta"][-1]
    tokens = "".join(d["text"] for k, d in events if k == "token")
    return final, tokens


@pytest.mark.parametrize("question", [
    "what is the total spend?",
    "what is the date range?",
    "which countries are in the data?",
])
def test_a_value_question_over_an_uploaded_dataset_is_not_answered_from_our_spec(question):
    """D1. Each of these is an artifact question BY SHAPE — `the total`, `the
    date` and `the data` are content nouns, so `_Q_THIS_FILE` reads them as
    pointing at this file — and a DATASET question by subject. The read-back
    cannot hold a cell value of either file, so it must not claim the turn."""
    conv = "art-val-" + hashlib.md5(question.encode()).hexdigest()[:8]
    with TestClient(app) as client:
        before = _make_artifact(client, conv, conv)
        _add_dataset(conv)
        final, tokens = _answer_turn(client, conv, question)
        assert "artifact_answer" not in final, (final, tokens[:300])
        assert "artifacts" not in final, final
        # The DESTINATION, not merely "not the read-back": the engine that has
        # the rows. Measured on this tree — route `dataset`, with the `datasets`
        # key the dataset engine's meta carries.
        assert final.get("route") == "dataset", (final, tokens[:300])
        assert "datasets" in final, final
    # …and nothing was built or re-rendered on the way out.
    rows = adb.list_artifacts(_owner_id(), conv)
    assert len(rows) == 1, rows
    assert str(rows[0]["id"]) == str(before[0]["id"]), (rows, before)


@pytest.mark.parametrize("question,label,route", [
    ("what does this page say? https://example.invalid/pricing", "url", "url"),
    ("what is in this repo? https://github.com/acme/widgets", "repo", "repo"),
])
def test_a_link_the_person_pasted_is_not_answered_from_our_spec(question, label, route):
    """D2. The read-back branch sits ABOVE the GitHub route, the crawl routes
    and the URL route, and a link is not a file, so the six file checks in
    `_carries_a_file_to_read` could not see one. `link_to_fetch` is code, not
    words: `github_ref`/`crawl_url`/`url_list`, all three already past
    `links_are_the_request`, so an incidental URL in a long paste cannot cost
    the read-back a turn."""
    conv = "art-link-" + label
    with TestClient(app) as client:
        _make_artifact(client, conv, conv)
        final, tokens = _answer_turn(client, conv, question)
        assert "artifact_answer" not in final, (final, tokens[:300])
        # The DESTINATION: the engine that can open the link. Measured on this
        # tree — `url` for a pasted page, `repo` for a GitHub URL. (The page
        # fetch itself fails offline, which is why `url` also carries
        # `fetch_failed`; that it was ATTEMPTED is the assertion.)
        assert final.get("route") == route, (final, tokens[:300])


@pytest.mark.parametrize("question", [
    "Ok What This sheet have ??",
    "what is in this sheet",
    "what did you put in the second sheet ??",
    "summarise the tracker you made",
    "i want to Know ?? please tell me Only Not create d??",
    "kya hai is sheet me ??",
])
def test_the_anchor_questions_still_answer_with_a_dataset_in_the_room(question):
    """THE POSITIVE CONTROL, and the reason D1's fix is a pointer test and not
    `dataset_ready` alone. Upload a CSV, make a workbook from it, ask about the
    workbook is the COMMONEST flow there is: every one of these points at the
    artifact — a determiner and a file word, a question about what you did, the
    SOV order, or a refusal of a new file — so the read-back still answers it
    with customers.csv sitting in the same conversation."""
    conv = "art-ds-ok-" + hashlib.md5(question.encode()).hexdigest()[:8]
    with TestClient(app) as client:
        made = _make_artifact(client, conv, conv)
        _add_dataset(conv)
        final, tokens = _answer_turn(client, conv, question)
        answered = final.get("artifact_answer") or {}
        assert answered, (final, tokens[:300])
        assert str(answered.get("artifact_id")) == str(made[0]["id"]), (answered, made)
        assert int(answered.get("version") or 0) == 1, answered
        assert "artifacts" not in final, final
