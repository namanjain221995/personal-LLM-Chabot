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
    words: `github_ref`/`crawl_url`/`url_list` — the disjunction of the three
    route conditions below it. All three are now past `links_are_the_request`;
    the crawl one was not until 2026-09-28, which is what
    `test_an_incidental_crawl_instruction_in_a_paste_is_not_a_crawl` pins."""
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


# ---------------------------------------------------------------------------
# ROUND 3 (2026-09-28). Three ways the read-back still claimed a turn whose
# subject was the person's own data, each measured through POST /chat with
# customers.csv and a workbook in ONE conversation, and each answered
# "**Workflow Tracker** (v1) is a workbook with 1 sheet: `Tasks`":
#
# N1  a REFUSAL was read as a pointer. "don't make a file, just tell me X"
#     says which OUTPUT the person wants; X says what the question is about.
# N2  the content-noun subtraction was applied to `the data` and not to `this
#     data` — one word apart — and `lexicon` rewrites `isme` / `is <noun>` to
#     `_this_`, so the Hinglish forms went the same way.
# N3  `_Q_SOV_RE` was read as a pointer although its first arm carries no file
#     reference at all, so EVERY Indian-language value question reached the
#     workbook while its English equivalent reached the dataset engine.
#
# The rules are pinned in tests/test_artifact_answer_read_source.py. These are
# the whole turns, because `meta.route` is what the person feels.

N1_A_REFUSAL_IS_NOT_A_POINTER = (
    "dont create a file, just tell me the total spend",
    "please tell me only, do not make a new file - what is the average spend per country?",
    "sirf bata do nayi file mat banao - total spend kitna hai",
)
N2_A_BARE_DEMONSTRATIVE_IS_NOT_A_POINTER = (
    "which countries are in this data?",
    "isme total spend kitna hai ??",
    "is data me total spend kitna hai ??",
)
N3_THE_SOV_ORDER_NEEDS_A_FILE_WORD = (
    "\u0921\u0947\u091f\u093e \u092e\u0947\u0902 \u0915\u0941\u0932 spend \u0915\u093f\u0924\u0928\u093e \u0939\u0948 ?",
    "data \u092e\u0947\u0902 \u0915\u0941\u0932 spend \u0915\u093f\u0924\u0928\u093e \u0939\u0948 ?",
    "rows \u092e\u0947\u0902 \u0915\u093f\u0924\u0928\u0947 countries \u0939\u0948\u0902 ?",
    "credit note \u092e\u0947\u0902 \u0915\u094d\u092f\u093e \u0939\u0948 ?",
)


@pytest.mark.parametrize("question", [
    *N1_A_REFUSAL_IS_NOT_A_POINTER,
    *N2_A_BARE_DEMONSTRATIVE_IS_NOT_A_POINTER,
    *N3_THE_SOV_ORDER_NEEDS_A_FILE_WORD,
])
def test_a_question_about_the_uploaded_dataset_reaches_the_dataset_engine(question):
    """The whole turn, not the rule: `meta.route` must be the engine that has
    the rows, and the reply must not be the workbook sentence."""
    conv = "art-r3-" + hashlib.md5(question.encode()).hexdigest()[:10]
    with TestClient(app) as client:
        before = _make_artifact(client, conv, conv)
        _add_dataset(conv)
        final, tokens = _answer_turn(client, conv, question)
        assert "artifact_answer" not in final, (question, final, tokens[:300])
        assert "Workflow Tracker" not in tokens, (question, tokens[:300])
        assert final.get("route") == "dataset", (question, final, tokens[:300])
    rows = adb.list_artifacts(_owner_id(), conv)
    assert len(rows) == 1 and str(rows[0]["id"]) == str(before[0]["id"]), (rows, before)


#: THE SAME QUESTION IN TWO LANGUAGES. This platform's own user base writes
#: Hindi, Hinglish and Gujlish, and before 2026-09-28 the English form of this
#: question reached the dataset engine while every Indian-language form of it
#: was answered from the workbook. The pair is the assertion: not "Hindi works"
#: but "Hindi and English agree".
SAME_QUESTION_TWO_LANGUAGES = (
    # the person's own data, both ways round
    ("\u0921\u0947\u091f\u093e \u092e\u0947\u0902 \u0915\u0941\u0932 spend \u0915\u093f\u0924\u0928\u093e \u0939\u0948 ?", "what is the total spend in the data ?"),
    ("is data me total spend kitna hai ??", "what is the total spend in this data ??"),
    ("rows \u092e\u0947\u0902 \u0915\u093f\u0924\u0928\u0947 countries \u0939\u0948\u0902 ?", "how many countries are in the rows ?"),
    # ...and OUR file, both ways round: the pair has to hold in both
    # directions, or "Hindi goes to the dataset engine" would be a way of
    # losing the read-back rather than a way of routing correctly.
    ("is sheet me kya hai ??", "what is in this sheet ??"),
    ("sheet me kitne columns hai ??", "how many columns are in the sheet ??"),
    ("report me kitne pages hai ??", "how many pages are in the report ??"),
    ("isme kitne rows hai ??", "how many rows are in it ??"),
)


@pytest.mark.parametrize("indic,english", SAME_QUESTION_TWO_LANGUAGES,
                         ids=[e[:40] for _i, e in SAME_QUESTION_TWO_LANGUAGES])
def test_the_indic_and_english_forms_of_one_question_take_the_same_route(indic, english):
    routes = {}
    for label, question in (("indic", indic), ("english", english)):
        conv = "art-r3-lang-" + label + "-" + hashlib.md5(question.encode()).hexdigest()[:8]
        with TestClient(app) as client:
            _make_artifact(client, conv, conv)
            _add_dataset(conv)
            final, tokens = _answer_turn(client, conv, question)
            routes[label] = (final.get("route"), "Workflow Tracker" in tokens)
    assert routes["indic"] == routes["english"], routes


def test_the_residual_of_the_bare_demonstrative_stated_rather_than_hidden():
    """WHERE THE TWO LANGUAGES STILL PART, measured and recorded (2026-09-28).

    A bare demonstrative with no noun of its own is a pointer at our file, and
    it has to be: "how many rows are in it ??" is a question about the workbook
    and so is "isme kitne rows hai ??". The subtraction that closed N2 is
    POSITIONAL — a demonstrative immediately followed by a content noun ("this
    data", "_this_ total") is not a pointer — and Hindi merges "in this" into
    one token that stands BEFORE the noun while English puts it AFTER. So
    "isme total spend kitna hai ??" reaches the dataset engine and "what is the
    total spend in this ??" does not.

    It is the residual this module already records for q13/q14/q21 and for "what
    is in the csv?": `totals?` and `rows?` are what a workbook and a CSV have in
    common, and no words rule separates a value question from a structure
    question when the only noun is one of those. Closing it needs the dataset's
    own column names at the route. Pinned here so that it is a known cost with a
    measurement beside it, not a surprise."""
    conv = "art-r3-residual"
    with TestClient(app) as client:
        _make_artifact(client, conv, conv)
        _add_dataset(conv)
        final, tokens = _answer_turn(client, conv, "what is the total spend in this ??")
        assert final.get("route") == "artifact", (final, tokens[:300])
        assert "Workflow Tracker" in tokens, tokens[:300]


#: A pasted DOCUMENT that happens to say "crawling <url>" in its body. 2,487
#: characters and 31 lines, which is what `links_are_the_request` is measured
#: against (500 characters / 15 lines).
A_PASTE_THAT_MENTIONS_A_CRAWL = (
    "Sprint 41 engineering notes\n\n"
    "Platform: the nightly job finished crawling https://docs.acme.invalid/guide and wrote 812 pages\n"
    "into the store. Ops raised two incidents, both resolved inside the hour.\n\n"
    + ("Detail line: throughput held at 41 pages per second across the whole window, with no retries.\n" * 24)
    + "\nWhat is in the sheet you made?\n"
)


def test_an_incidental_crawl_instruction_in_a_paste_is_not_a_crawl():
    """N4 (2026-09-28). `_carries_a_file_to_read` documented `link_to_fetch` as
    "all three past `links_are_the_request`, so an incidental URL inside a long
    paste is already excluded upstream and cannot cost the read-back a turn".
    That was false for `crawl_url`: `detect_crawl` asks only for a crawl word
    within 80 characters of a URL, and nothing applied the paste test to it.

    Measured through POST /chat before the guard: `meta.route` "crawl" and the
    whole answer was "I can't crawl docs.acme.invalid: its robots.txt could not
    be read, so I assume crawling is not welcome there." The 2,487-character
    paste and the question it ends with were never read. That is the 2026-08-11
    owner report reproduced in the crawl phase, and it cost the read-back this
    turn as well."""
    conv = "art-r3-paste"
    with TestClient(app) as client:
        _make_artifact(client, conv, conv)
        final, tokens = _answer_turn(client, conv, A_PASTE_THAT_MENTIONS_A_CRAWL)
        assert final.get("route") != "crawl", (final, tokens[:300])
        assert "robots.txt" not in tokens, tokens[:300]
        # The question the paste ENDS with is about the file we made, so this
        # turn is the read-back's; the assertion that matters is that a
        # whole-site walk nobody asked for did not claim it.
        assert final.get("route") == "artifact", (final, tokens[:300])
        assert "Workflow Tracker" in tokens, tokens[:300]


# ---------------------------------------------------------------------------
# ROUND 4 (2026-09-28). Two whole classes of turn the round-3 fix got wrong,
# both measured here through POST /chat with customers.csv and a workbook in ONE
# conversation, because unit assertions passed on two candidates while the
# product was broken:
#
# D1  a BARE FORMAT OR CONTAINER WORD is what the person's own upload is called.
#     The new SOV pointer ORed the whole file-word list into an arm with no
#     determiner in front of it, so "csv me total spend kitna hai ??" — a
#     question about customers.csv — was answered "**Workflow Tracker** (v1) is
#     a workbook with 1 sheet: `Tasks`". Ten turns, and not one guard, corpus
#     row or score moved, which is why the round-3 report did not see it.
# D2  N1 CLOSED ENGLISH WORD ORDER ONLY. "navi file na banavo, fakt kul spend
#     kaho" names its own subject, and Gujarati and Hindi put it before the verb,
#     inside the gap the ask-to-be-told phrase subtracted whole.
#
# And the reverse direction, which the round-3 report claimed as a win for one
# word out of six: a question that names our file in its OWN words reached the
# dataset engine, because the postposition was never written for `tracker`,
# `work book`, `deck` or `deliverable` and because the SOV shape wanted a
# determiner the turn does not have.

D1_A_BARE_FORMAT_WORD_IS_THEIR_UPLOAD = (
    "csv me total spend kitna hai ??",
    "csv me kitne rows hai ??",
    "csv me kitne columns hai ??",
    "csv me kya data hai ??",
    "excel me kitne rows hai ??",
    "csv में कितने rows हैं ?",
    "csv ma ketla rows che ??",
    "pdf me kitne pages hai ??",
    "file me kitne rows hai ??",
    "doc me kitne pages hai ??",
)
D2_A_REFUSAL_THAT_NAMES_ITS_SUBJECT_LAST = (
    "navi file na banavo, fakt kul spend kaho",
    "file mat banao bas countries bata do",
    "file mat banao sirf countries bata do",
    "nayi file mat banao khali total spend batao",
    "navi file na banavo fakt average spend kaho",
    "file na banavo, bas spend samjavo",
)


@pytest.mark.parametrize("question", [
    *D1_A_BARE_FORMAT_WORD_IS_THEIR_UPLOAD,
    *D2_A_REFUSAL_THAT_NAMES_ITS_SUBJECT_LAST,
])
def test_a_question_in_verb_final_order_reaches_the_dataset_engine(question):
    """The whole turn: `meta.route` must be the engine that has the rows, the
    reply must not be the workbook sentence, and nothing may be built."""
    conv = "art-r4-" + hashlib.md5(question.encode()).hexdigest()[:10]
    with TestClient(app) as client:
        before = _make_artifact(client, conv, conv)
        _add_dataset(conv)
        final, tokens = _answer_turn(client, conv, question)
        assert "artifact_answer" not in final, (question, final, tokens[:300])
        assert "Workflow Tracker" not in tokens, (question, tokens[:300])
        assert final.get("route") == "dataset", (question, final, tokens[:300])
    rows = adb.list_artifacts(_owner_id(), conv)
    assert len(rows) == 1 and str(rows[0]["id"]) == str(before[0]["id"]), (rows, before)


R4_OUR_FILE_IN_ITS_OWN_WORDS = (
    "tracker me kya hai ??",
    "workbook me kitni sheets hai ??",
    "work book me kya hai ??",
    "deck me kitne slides hai ??",
    "deliverable me kya hai ??",
    "spreadsheet me kitne rows hai ??",
)


@pytest.mark.parametrize("question", R4_OUR_FILE_IN_ITS_OWN_WORDS)
def test_a_question_that_names_our_file_in_its_own_words_keeps_the_read_back(question):
    """THE OTHER DIRECTION, and the reason D1's fix is a NARROWER word list and
    not a narrower pointer: nobody calls the CSV they just uploaded a tracker, a
    deck or a deliverable, so these keep the read-back with a dataset in the
    room. Every one of them reached the dataset engine on 780bdea2."""
    conv = "art-r4-ours-" + hashlib.md5(question.encode()).hexdigest()[:8]
    with TestClient(app) as client:
        made = _make_artifact(client, conv, conv)
        _add_dataset(conv)
        final, tokens = _answer_turn(client, conv, question)
        answered = final.get("artifact_answer") or {}
        assert answered, (question, final, tokens[:300])
        assert str(answered.get("artifact_id")) == str(made[0]["id"]), (answered, made)
        assert "Workflow Tracker" in tokens, tokens[:300]
        assert "artifacts" not in final, final


@pytest.mark.parametrize("question", (
    "dont create a file, just quickly tell me",
    "file mat banao bas bata do",
))
def test_a_refusal_whose_gap_holds_no_subject_keeps_the_read_back(question):
    """THE COST OF D2's FIX, paid for and pinned. The words between "just"/"bas"
    and the speech verb are kept rather than subtracted, so a turn whose gap
    holds an ADVERB — the English shape of that slot — must not read as a turn
    that named a subject. `_Q_NO_SUBJECT_FILLER_RE` carries the manner adverbs
    for exactly this."""
    conv = "art-r4-adv-" + hashlib.md5(question.encode()).hexdigest()[:8]
    with TestClient(app) as client:
        _make_artifact(client, conv, conv)
        _add_dataset(conv)
        final, tokens = _answer_turn(client, conv, question)
        assert final.get("route") == "artifact", (question, final, tokens[:300])
        assert "Workflow Tracker" in tokens, tokens[:300]


#: THE SAME QUESTION IN TWO LANGUAGES, round 4. The verifier read the Hinglish
#: demonstrative as word-order-fragile in a way English is not; measured, it is
#: not — English gives the SAME verdict for both members of each pair. What the
#: pair asserts is the contract this module already states: not "Hindi works" but
#: "Hindi and English agree".
R4_SAME_QUESTION_TWO_LANGUAGES = (
    ("isme kya data hai ??", "what data is in it ??"),
    ("isme kitna total spend hai ??", "what is the total spend in it ??"),
    ("tracker me kya hai ??", "what is in the tracker ??"),
    ("workbook me kitni sheets hai ??", "how many sheets are in the workbook ??"),
    ("csv me kitne rows hai ??", "how many rows are in the csv i uploaded ??"),
    ("file mat banao bas countries bata do", "dont make a file, just tell me the countries"),
)


@pytest.mark.parametrize("indic,english", R4_SAME_QUESTION_TWO_LANGUAGES,
                         ids=[e[:40] for _i, e in R4_SAME_QUESTION_TWO_LANGUAGES])
def test_the_round_four_pairs_take_the_same_route_in_both_languages(indic, english):
    routes = {}
    for label, question in (("indic", indic), ("english", english)):
        conv = "art-r4-lang-" + label + "-" + hashlib.md5(question.encode()).hexdigest()[:8]
        with TestClient(app) as client:
            _make_artifact(client, conv, conv)
            _add_dataset(conv)
            final, tokens = _answer_turn(client, conv, question)
            routes[label] = (final.get("route"), "Workflow Tracker" in tokens)
    assert routes["indic"] == routes["english"], routes


def test_a_determiner_in_front_of_a_bare_format_word_is_the_stated_residual():
    """WHERE D1's FIX STOPS, measured and recorded. "આ csv ma ketla rows che ??"
    is "THIS csv": the lexicon rewrites `આ` to `_this_`, so the turn takes
    `_Q_OUR_FILE`'s DETERMINER arm, which admits the whole file-word list — and
    must, because it is the arm that keeps "what is in the csv?" answering.

    That is the residual tests/test_artifact_answer_read_source.py already
    records: this platform publishes CSV artifacts, so "this csv" names either
    file and no words rule separates them. It is the ONE turn of the ten that
    does not return to the dataset engine, and it is the same decision the repo
    already took for English, not a new one."""
    question = "આ csv ma ketla rows che ??"
    conv = "art-r4-residual"
    with TestClient(app) as client:
        _make_artifact(client, conv, conv)
        _add_dataset(conv)
        final, tokens = _answer_turn(client, conv, question)
        assert final.get("route") == "artifact", (final, tokens[:300])
        assert "Workflow Tracker" in tokens, tokens[:300]
        # …and its English twin takes the same route, which is the point.
        conv_en = "art-r4-residual-en"
        _make_artifact(client, conv_en, conv_en)
        _add_dataset(conv_en)
        final_en, tokens_en = _answer_turn(client, conv_en, "how many rows are in this csv ??")
        assert final_en.get("route") == "artifact", (final_en, tokens_en[:300])


def test_output_is_the_one_file_word_the_postposition_list_cannot_take():
    """THE KNOWN COST of closing the reverse direction, stated rather than
    discovered. `outputs?` is a file word, but "output me" cannot be disambiguated
    by the noun: "output me a summary" is an English imperative with the PRONOUN,
    and lexicon.py's postposition list is a list of nouns after which `me` is
    certainly the postposition. So "output me kya hai ??" still reaches the
    dataset engine, and closing it needs the turn's LANGUAGE at the normaliser
    (lexicon.language_of), not another word in the list."""
    conv = "art-r4-output"
    with TestClient(app) as client:
        _make_artifact(client, conv, conv)
        _add_dataset(conv)
        final, tokens = _answer_turn(client, conv, "output me kya hai ??")
        assert final.get("route") == "dataset", (final, tokens[:300])
        # The English form is not affected: it has a determiner and a
        # containment word, so it keeps its read-back.
        conv_en = "art-r4-output-en"
        _make_artifact(client, conv_en, conv_en)
        _add_dataset(conv_en)
        final_en, tokens_en = _answer_turn(client, conv_en, "what is in the output ??")
        assert final_en.get("route") == "artifact", (final_en, tokens_en[:300])
        assert "Workflow Tracker" in tokens_en, tokens_en[:300]
