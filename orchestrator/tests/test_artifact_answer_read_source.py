"""A question about a file the PERSON sent is not answered from OUR spec.

THE DEFECT THESE PIN (verifier, 2026-09-27). The artifact read-back branch
(app/main.py) sits ABOVE the video route, the document route and the two
image routes, and the first version of it claimed every turn the intent gate
called a question:

    elif artifact_intent is not None and (
        artifact_intent.wants_file
        or _as3_describe.is_artifact_question(artifact_intent)
    ):

Composed with the intent gate that records the verdict (track
`question-not-edit`), measured on 2026-09-27 with has_artifacts=True and no
upload in the current turn, "what does the chart in that photo say?", "what
does that image contain?" and "what does the map on page 2 show?" all come
back action=none, rule=answer-artifact:contents, answer_about_artifact=True.
Each was then claimed by that branch; `wants_file` is False so main.py's own
image read is skipped; and the answer was read back from a stored WORKBOOK's
spec.json — "**TechSara AI Engineering Workflow Tracker** (v1) is a workbook
with 2 sheets…" to someone asking about a photo. The attachment was never
opened and the route that would have opened it never ran.

This is the same failure `_asks_about_an_attachment` was written for on
2026-09-16, one branch below in the same elif chain, whose comment quotes
"what does the map on page 2 show?" verbatim.
"""
from __future__ import annotations

import inspect

import pytest

from app.artifacts import describe as D


def _request(text: str = "", **kw):
    from app.main import ChatRequest

    return ChatRequest(message=text, **kw)


def _intent_that_asks():
    """The verdict the gate records: action stays "none", the flag is set."""
    from app.artifacts.intent import ArtifactIntent

    intent = ArtifactIntent("none", rule="answer-artifact:contents")
    intent.answer_about_artifact = True
    return intent


# ------------------------------------------------- the rule, in one place --


def test_a_question_turn_that_holds_a_read_source_is_not_answered_from_a_spec():
    """`answers_from_spec` is the whole rule: the gate's verdict AND no file of
    the person's own to read. The engine's read-back cannot open a photo, a PDF
    page or a video, so a turn that holds one belongs to the engine that can."""
    intent = _intent_that_asks()
    assert D.is_artifact_question(intent) is True
    assert D.answers_from_spec(intent, has_read_source=False) is True
    assert D.answers_from_spec(intent, has_read_source=True) is False
    # A file request is untouched by the carve-out — it is not a question at
    # all, so the branch above still claims it whatever the turn carries.
    assert D.answers_from_spec(None, has_read_source=False) is False


def test_main_reaches_the_read_back_only_through_answers_from_spec():
    """The structural guard. The carve-out lives in `answers_from_spec`, so a
    later edit that calls `is_artifact_question` directly at that branch would
    re-open the defect silently. main.py must not name it."""
    from app import main

    source = inspect.getsource(main)
    assert "_as3_describe.answers_from_spec(" in source
    assert "is_artifact_question" not in source


# ------------------------------------- what counts as a file to read, and not --


@pytest.mark.parametrize("text", [
    "what does the chart in that photo say?",
    "what does that image contain?",
    "what does the map on page 2 show?",
    "what does this document say?",
])
def test_an_earlier_image_makes_the_turn_the_image_routes(text):
    """`image_memory.followup` has already read the words and produced bytes;
    that IS the words test, and the route two branches below opens the
    picture."""
    from app.main import _carries_a_file_to_read

    assert _carries_a_file_to_read(text, _request(text), False, True) is True


@pytest.mark.parametrize("text", [
    "what does the map on page 2 show?",
    "what is written in the pdf i sent?",
    "what does the diagram in the attachment mean?",
    "read the file i uploaded",
])
def test_an_earlier_document_needs_the_words_and_these_have_them(text):
    """A document uploaded in an EARLIER turn has no route of its own — it
    rides as a pinned system block — so it takes the words. They are narrow on
    purpose: `visuals.asks_about_attachment_content` is True for 13 of the 49
    labelled artifact questions (measured 2026-09-27: q03 "what is in this
    sheet", q17 "list the headings in the document", …), so using it here
    would take a quarter of the read-back class away in the commonest flow of
    all — upload a PDF, make a file from it, ask about the file."""
    from app.main import _carries_a_file_to_read

    assert _carries_a_file_to_read(text, _request(text), False, False,
                                   stored_documents=True) is True
    # …and with no document in the conversation the same words read back the
    # artifact, because there is nothing else to read.
    assert _carries_a_file_to_read(text, _request(text), False, False,
                                   stored_documents=False) is False


def test_a_video_or_an_attached_file_is_a_read_source_without_any_words_test():
    from app.main import _carries_a_file_to_read

    text = "Ok What This sheet have ??"
    assert _carries_a_file_to_read(text, _request(text), True, False) is True
    assert _carries_a_file_to_read(
        text, _request(text, pdf_uploads=[{"upload_id": "a" * 32, "name": "q3.pdf"}]),
        False, False) is True


@pytest.mark.parametrize("text", [
    "Ok What This sheet have ??",
    "what is in this sheet",
    "what did you put in the second sheet ??",
    "summarise the tracker you made",
    "list the headings in the document",
    "what data is in the excel you just made",
    "read back what the sheet has",
    "what formulas are in it?",
    "i want to Know ?? please tell me Only Not create d??",
])
def test_the_anchor_and_its_neighbours_keep_the_read_back(text):
    """The production anchor and eight of the 49 labelled turns, in a
    conversation that ALSO holds an uploaded document. None of them names the
    upload, so the artifact is still what is read back — otherwise this fix
    would cost the class it exists to serve."""
    from app.main import _carries_a_file_to_read

    assert _carries_a_file_to_read(text, _request(text), False, False,
                                   stored_documents=True) is False
    assert D.answers_from_spec(
        _intent_that_asks(),
        has_read_source=_carries_a_file_to_read(text, _request(text), False, False,
                                                stored_documents=True),
    ) is True


def test_every_labelled_artifact_question_survives_a_stored_document():
    """The whole class, not a sample: none of the 49 turns labelled
    `answer_about_artifact` in the programme's corpus may be diverted by the
    document words test. Measured 2026-09-27: 0 of 119."""
    import json
    import os

    from app.main import _carries_a_file_to_read

    path = os.environ.get("INTENT_CORPUS") or ""
    if not path or not os.path.isfile(path):
        pytest.skip("the programme corpus is not on this machine (set INTENT_CORPUS)")
    items = json.load(open(path, encoding="utf-8"))["items"]
    # Five items carry `text_build` instead of a literal text, so a pasted
    # table of ten thousand rows is not in the corpus file; the scorer builds
    # prefix + header + row_template * rows, and so does this.
    def _text_of(item):
        if (item.get("text") or "").strip():
            return item["text"]
        r = item.get("text_build") or {}
        rows = "".join(
            str(r.get("row_template", "")).format(i=i, owner=i % 7, day=1 + i % 28, pri=1 + i % 4)
            for i in range(1, int(r.get("rows", 0)) + 1)
        )
        return str(r.get("prefix", "")) + str(r.get("header", "")) + rows

    texts = [(t["id"], _text_of(t)) for t in items]
    # 117 of the 119, because s12 and s13 ARE empty on purpose ("an empty
    # message is not a question about anything").
    assert len(texts) == 119, "the corpus changed shape"
    assert len([t for t in texts if t[1].strip()]) == 117, "the corpus did not build"
    # A ChatRequest that carries NOTHING: the only thing under test here is the
    # words half, so the request is a placeholder and the text is the argument.
    bare = _request("x")
    diverted = [i for i, txt in texts
                if _carries_a_file_to_read(txt, bare, False, False, stored_documents=True)]
    assert diverted == [], diverted


# --------------------------------------------------- HELD-OUT neighbours --
#
# The programme's 119-turn corpus was authored the same day as the gate, so it
# is IN-SAMPLE for a rule written against it: `_NAMES_AN_UPLOAD_RE` scoring 0
# hits on it proves only that it does not contradict the turns it was checked
# against. These 32 sentences are NOT in that corpus and were written before
# the numbers below were measured (2026-09-27).

#: Questions about a file THIS PLATFORM made. Every one of them must keep the
#: read-back even in a conversation that also holds an uploaded document.
HELD_OUT_ABOUT_OUR_FILE = (
    "how many rows does the tracker have?",
    "which columns are on the summary tab?",
    "tell me the sheet names again",
    "is there a notes column?",
    "how many slides are in the deck?",
    "what headings does the report use?",
    "does the workbook have a totals row?",
    "remind me what's in the second tab",
    "what did you call the columns in the tracker?",
    "kitne rows hai is sheet me?",
    "what charts are in the report you made?",
    # The one the veto exists for: a PDF this platform produced HAS pages, and
    # `page 3` alone would have diverted this to the chat engine.
    "what's on page 3 of the report you made?",
    "what format did you save the tracker in?",
    "just tell me what the deck covers, no new file",
    "how big is the pdf you generated?",
    "શીટમાં કેટલી પંક્તિઓ છે?",
    "what is the title of the second slide?",
    "list the tabs",
    "does it have a due date column or not ??",
    "summarise what the workbook covers",
    "what are the headings in the report, only tell me",
    "how many columns did you end up with?",
)

#: Questions about a document the PERSON uploaded in an earlier turn. Every one
#: must reach the engine that has the document, not the artifact's spec.
HELD_OUT_ABOUT_THEIR_UPLOAD = (
    "what does the table on page 4 of the pdf say?",
    "what is in the document i uploaded?",
    "translate the text in the attachment",
    "what does the invoice i sent say?",
    "read page 7 for me",
    "summarise the report i shared earlier",
    "what's the total on page 2?",
    "what did the pdf i attached say about pricing?",
    "in the file i uploaded, what is the second column?",
    "what does the scan i sent show?",
)


@pytest.mark.parametrize("text", HELD_OUT_ABOUT_OUR_FILE)
def test_held_out_questions_about_our_own_file_keep_the_read_back(text):
    from app.main import _carries_a_file_to_read

    assert _carries_a_file_to_read(text, _request(text), False, False,
                                   stored_documents=True) is False


@pytest.mark.parametrize("text", HELD_OUT_ABOUT_THEIR_UPLOAD)
def test_held_out_questions_about_their_upload_reach_the_document(text):
    from app.main import _carries_a_file_to_read

    assert _carries_a_file_to_read(text, _request(text), False, False,
                                   stored_documents=True) is True
    # …and with no document in the conversation there is nothing else to read,
    # so the same words read the artifact back rather than answering nothing.
    assert _carries_a_file_to_read(text, _request(text), False, False,
                                   stored_documents=False) is False


# ---------------------------------------------------------------------------
# A LINK AND A DATASET ARE THE OTHER TWO SUBJECTS (verifier, 2026-09-27).
#
# The same shape of defect as the photo above, two branches further down the
# same elif chain, and combination-only: neither fix/question-not-edit-r2 nor
# fix/answer-from-the-spec-r2 shows either alone.
#
#   D1  "what is the total spend?" over a conversation holding customers.csv
#       AND a workbook was answered "**Workflow Tracker** (v1) is a workbook
#       with 1 sheet: `Tasks`". The read-back holds the artifact's STRUCTURE
#       and never a cell value — of either file — so the real number was never
#       computed.
#   D2  "what does this page say? https://example.invalid/pricing" and "what is
#       in this repo? https://github.com/acme/widgets" got the same sentence,
#       and neither the page nor the repository was fetched.
#
# The two arms are deliberately different, because the two facts are:
#   * a LINK is carried BY the turn, so it is code — `link_to_fetch`, from the
#     route's own `github_ref` / `crawl_url` / `url_list`, no words test;
#   * a DATASET sits in the CONVERSATION, so `has_read_source` cannot see it.
#     What decides is whether the QUESTION points at the artifact, which the
#     gate records as `intent.names_our_file`.


def _intent_that_asks_about(text: str, **kw):
    """The gate's real verdict for `text` with an artifact in the room — the
    `names_our_file` these tests read is computed there, not here."""
    from app.artifacts import intent as I

    kw.setdefault("has_artifacts", True)
    kw.setdefault("last_turn_is_artifact", True)
    kw.setdefault("artifact_hints", ("TechSara AI Engineering Workflow Tracker",))
    return I.decide(text, **kw)


# ----------------------------------------------------------------- D2: links --


@pytest.mark.parametrize("text", [
    "what does this page say? https://example.invalid/pricing",
    "what is in this repo? https://github.com/acme/widgets",
    "what does it say? https://example.invalid/a https://example.invalid/b",
])
def test_a_link_this_turn_carries_is_a_read_source_without_any_words_test(text):
    """`link_to_fetch` is the route's `github_ref is not None`, `crawl_url is
    not None` or a non-empty `url_list` — the disjunction of the three route
    conditions below this branch, which is all the flag has to mean: a route
    under it will fetch a source for this turn. There is no second words test
    here to get wrong.

    What each of the three IS is pinned by the two tests below it, because the
    first version of this docstring said all three were links from this turn's
    text past `links_are_the_request` and that was true of two of them."""
    from app.main import _carries_a_file_to_read

    assert _carries_a_file_to_read(text, _request(text), False, False,
                                   link_to_fetch=True) is True
    # The same words with NO link the platform can fetch read the artifact
    # back, because then nothing below this branch can answer them either.
    assert _carries_a_file_to_read(text, _request(text), False, False,
                                   link_to_fetch=False) is False


#: A pasted DOCUMENT whose body happens to say "crawling <url>": 2,487
#: characters and 31 lines, against `links_are_the_request`'s 500 characters and
#: 15 lines.
A_PASTE_THAT_MENTIONS_A_CRAWL = (
    "Sprint 41 engineering notes\n\n"
    "Platform: the nightly job finished crawling https://docs.acme.invalid/guide and wrote 812 pages\n"
    "into the store. Ops raised two incidents, both resolved inside the hour.\n\n"
    + ("Detail line: throughput held at 41 pages per second across the whole window, with no retries.\n" * 24)
    + "\nWhat is in the sheet you made?\n"
)


def _link_to_fetch(text: str) -> bool:
    """The route's own expression, in the order the route computes it."""
    from app.config import settings
    from app.core.repo import detect_github
    from app.core.urls import extract_urls, links_are_the_request
    from app.engines.crawl import detect_crawl

    urls = extract_urls(text, limit=settings.url_max_pages)
    are_the_request = bool(urls) and links_are_the_request(text, urls)
    github = detect_github(text) is not None and are_the_request
    crawl = detect_crawl(text) is not None and are_the_request
    return bool(github or crawl or (urls and are_the_request))


def test_the_three_detectors_the_route_composes_each_raise_the_flag():
    """`link_to_fetch` is one expression over three detectors, so each one is
    exercised here rather than trusted: a pasted page (`url_list`), a GitHub
    repo (`github_ref`) and a whole-site crawl (`crawl_url`). Pure — nothing
    is fetched; these are the same functions the route calls before the chain."""
    assert _link_to_fetch("what does this page say? https://example.invalid/pricing") is True
    assert _link_to_fetch("what is in this repo? https://github.com/acme/widgets") is True
    assert _link_to_fetch("crawl this site https://example.invalid and tell me what it says") is True
    # The anchor question carries no link at all.
    assert _link_to_fetch("Ok What This sheet have ??") is False


def test_an_incidental_crawl_instruction_in_a_paste_is_not_a_link_to_fetch():
    """N4 (2026-09-28): `crawl_url` did not satisfy the premise this branch was
    given. `detect_crawl` asks only for a crawl word within 80 characters of a
    URL, and nothing applied `links_are_the_request` to it — so a pasted sprint
    note whose body said "the nightly job finished crawling <url>" raised the
    flag, the read-back stood down, and the crawl route below claimed the turn.
    Measured through POST /chat before the guard (see
    tests/test_artifact_question_route.py): `meta.route` "crawl", and the whole
    answer was "I can't crawl docs.acme.invalid: its robots.txt could not be
    read". The paste and the question it ends with were never read."""
    from app.config import settings
    from app.core.urls import extract_urls, links_are_the_request
    from app.engines.crawl import detect_crawl

    paste = A_PASTE_THAT_MENTIONS_A_CRAWL
    urls = extract_urls(paste, limit=settings.url_max_pages)
    # The detector still fires — that is not what was fixed...
    assert detect_crawl(paste) is not None
    # ...the paste test says the links are not the request, and the route now
    # reads it for the crawl phase exactly as it does for the other two.
    assert links_are_the_request(paste, urls) is False
    assert _link_to_fetch(paste) is False


def test_continue_crawling_carries_no_link_from_this_turns_text():
    """…and the OTHER half of the corrected claim. `crawl_url` is not always
    from this turn's text: "continue crawling" is the phrase the capped-crawl
    message advertises, `engines/crawl.detect_resume` requires that the turn
    carry NO URL, and the dispatcher then takes the newest crawled site of this
    conversation. Such a turn still belongs to the crawl route, which is the
    only thing `link_to_fetch` has to be right about — so it counts, and the
    docstring now says so instead of claiming all three come from this turn."""
    from app.core.urls import extract_urls
    from app.engines.crawl import detect_crawl, detect_resume

    text = "continue crawling please"
    assert extract_urls(text, limit=5) == []
    assert detect_crawl(text) is None
    assert detect_resume(text) is True


def test_a_schemeless_host_reaches_no_fetch_route_on_this_tree_either():
    """The residual, pinned so it is not mistaken for a hole this fix opened.
    The platform's link surface is `https?://` only — `core/urls._URL_RE` and
    `core/repo._REPO_RE` both require the scheme — so a bare host reaches no
    fetch route here AND none on origin/dev (measured on both, 2026-09-27).
    Widening it is a change to what this platform will clone and GET, and it
    belongs with the routes that fetch, not with this branch."""
    from app.config import settings
    from app.core.repo import detect_github
    from app.core.urls import extract_urls
    from app.engines.crawl import detect_crawl

    text = "what is in this repo? github.com/acme/widgets"
    assert detect_github(text) is None
    assert extract_urls(text, limit=settings.url_max_pages) == []
    assert detect_crawl(text) is None


def test_the_anchor_question_is_not_diverted_by_a_link_flag_it_does_not_set():
    """The flag is per-turn: an artifact question in a conversation where an
    EARLIER turn pasted a link is unaffected, because `github_ref`, `crawl_url`
    and `url_list` all read this turn's words."""
    from app.main import _carries_a_file_to_read

    text = "Ok What This sheet have ??"
    assert _carries_a_file_to_read(text, _request(text), False, False,
                                   link_to_fetch=False) is False


# --------------------------------------------------------------- D1: datasets --


def test_answers_from_spec_reads_names_our_file_only_when_a_dataset_is_present():
    """The third arm, stated as its own rule. With no dataset in the room there
    is no other file the question could be about, so `names_our_file` is not
    read at all — which is why the 48 answered turns of the 119-case corpus are
    untouched by this fix."""
    pointing = _intent_that_asks_about("what is in this sheet")
    value = _intent_that_asks_about("what is the total spend?")
    assert pointing.answer_about_artifact is True and pointing.names_our_file is True
    assert value.answer_about_artifact is True and value.names_our_file is False

    # No dataset: both are answered from the spec, exactly as before.
    assert D.answers_from_spec(pointing, has_dataset=False) is True
    assert D.answers_from_spec(value, has_dataset=False) is True
    # A dataset in the room: only the one that points at the artifact.
    assert D.answers_from_spec(pointing, has_dataset=True) is True
    assert D.answers_from_spec(value, has_dataset=True) is False
    # The other two arms still stand on their own.
    assert D.answers_from_spec(pointing, has_read_source=True, has_dataset=False) is False
    assert D.answers_from_spec(value, has_read_source=True, has_dataset=True) is False


def test_names_our_file_is_set_on_the_question_verdict_and_nowhere_else():
    """It is a fact about a QUESTION, so a create, an edit and a convert must
    not carry it — a later reader must not be able to mistake it for "this turn
    mentions a file"."""
    for text in ("make me a tracker for the team",
                 "make slide 4 shorter",
                 "give me that as a pdf"):
        decided = _intent_that_asks_about(text)
        assert decided.answer_about_artifact is False, (text, decided.rule)
        assert decided.names_our_file is False, (text, decided.rule)


#: Questions about a file THIS PLATFORM made that must keep the read-back even
#: when the conversation ALSO holds an uploaded dataset. Held out: written
#: before the rule was measured, and not in the programme's 119-case corpus.
HELD_OUT_OURS_DESPITE_A_DATASET = (
    "what is in this sheet",
    "Ok What This sheet have ??",
    "what columns does it have?",
    "how many rows are in it?",
    "tell me what is inside the workbook",
    "what does the tracker contain ??",
    "just tell me what the tracker has, don't create anything",
    "summarise the tracker you made",
    "which sheets did you create?",
    "what did you put in the second sheet ??",
    "what format did you save it in ??",
    "what is on slide 3?",
    "what sections does the report have?",
    "list the headings in the document",
    "kya hai is sheet me ??",
    "isme kya kya columns hai?",
    "sheet ma su su che ae kaho, navi file na banavo",
    "what data is in the excel you just made",
    "read back what the sheet has",
    "why did you add a priority column?",
    "did you include the due dates?",
    "explain what you created",
    "what formulas are in it?",
    "is the tracker two sheets or one ??",
    "what's in it 🤔📊",
    "does it have a status column?",
)

#: Questions about the person's own DATA whose answer is a real value only the
#: dataset engine can compute. Every one must come out of the route WITHOUT a
#: read-back when a dataset is in the room.
#:
#: Measured 2026-09-27: the gate calls the first four `answer_about_artifact`
#: (a content noun behind a determiner is "this file" to `_Q_THIS_FILE`), and
#: declines the last four as `no-request` before this fix is reached at all —
#: which is why the assertion below is about the OUTCOME and names the gate's
#: verdict only where there is one to name.
HELD_OUT_THEIR_DATA = (
    "what is the total spend?",
    "what is the date range?",
    "which countries are in the data?",
    "what are the top five values?",
    "what is the average order value?",
    "how many distinct customers are there?",
    "which month had the highest revenue?",
    "what is the sum of the amounts?",
)
#: …and the four of them the gate itself calls a question, so the ROUTE is the
#: only thing standing between them and the wrong answer.
HELD_OUT_THEIR_DATA_THE_GATE_CLAIMS = HELD_OUT_THEIR_DATA[:4]


@pytest.mark.parametrize("text", HELD_OUT_OURS_DESPITE_A_DATASET)
def test_held_out_questions_about_our_file_survive_a_dataset(text):
    decided = _intent_that_asks_about(text, has_dataset=True)
    assert decided.answer_about_artifact is True, (text, decided.rule)
    assert D.answers_from_spec(decided, has_dataset=True) is True, (text, decided.rule)


@pytest.mark.parametrize("text", HELD_OUT_THEIR_DATA)
def test_held_out_questions_about_their_data_stand_the_read_back_down(text):
    decided = _intent_that_asks_about(text, has_dataset=True)
    assert D.answers_from_spec(decided, has_dataset=True) is False, (text, decided.rule)


@pytest.mark.parametrize("text", HELD_OUT_THEIR_DATA_THE_GATE_CLAIMS)
def test_the_route_is_what_stops_the_data_questions_the_gate_claims(text):
    """These four the gate DOES call a question about the artifact — that
    verdict is about SHAPE and it is not wrong, because the same words with no
    dataset uploaded have nothing else to be about. The route settles the
    subject, which is the whole point of `answers_from_spec`."""
    decided = _intent_that_asks_about(text, has_dataset=True)
    assert decided.answer_about_artifact is True, (text, decided.rule)
    assert decided.names_our_file is False, (text, decided.rule)
    assert D.answers_from_spec(decided, has_dataset=True) is False, (text, decided.rule)
    # …and with no dataset uploaded there is nothing else to answer from, so
    # the same words read the artifact back rather than answering nothing.
    assert D.answers_from_spec(decided, has_dataset=False) is True, (text, decided.rule)


#: Questions that name the person's OWN file as theirs. `csv`, `excel` and
#: `spread sheet` are all in `_Q_FILE_WORD` — and they have to be, because
#: formats.py's `data` template makes our artifacts CSVs too — so the pointer
#: alone cannot say whose file is meant. "i uploaded", "i sent", "the
#: attachment" can, and they veto it.
HELD_OUT_THEY_NAMED_IT_AS_THEIRS = (
    "what is in the csv i uploaded?",
    "what is in the sheet i sent?",
    "what does the attachment contain?",
    "how many rows are in the file i uploaded?",
    "what columns does the spreadsheet i shared have?",
    "what is in the uploaded csv?",
)


@pytest.mark.parametrize("text", HELD_OUT_THEY_NAMED_IT_AS_THEIRS)
def test_naming_the_file_as_their_own_vetoes_the_pointer(text):
    decided = _intent_that_asks_about(text, has_dataset=True)
    assert decided.names_our_file is False, (text, decided.rule)
    assert D.answers_from_spec(decided, has_dataset=True) is False, (text, decided.rule)


def test_the_upload_veto_cannot_reach_a_question_about_our_own_file():
    r"""It is narrow on purpose: the same vocabulary app.main._NAMES_AN_UPLOAD_RE
    uses one layer up, MINUS the `page \d+` and timestamp arms, because a PDF
    this platform made HAS pages. Measured 2026-09-27: 0 of the corpus's 119
    turns match it."""
    from app.artifacts import intent as I

    for text in ("what's on page 3 of the report you made?",
                 "what is on slide 3?",
                 "what did you put in the second sheet ??",
                 "i pasted the source rows below, what columns does this sheet have?"):
        assert I._Q_THEIR_UPLOAD_RE.search(text.lower()) is None, text
        decided = _intent_that_asks_about(text, has_dataset=True)
        assert decided.names_our_file is True, (text, decided.rule)


def test_a_bare_format_word_shared_by_both_files_is_a_stated_residual():
    """THE RESIDUAL, pinned rather than left to be discovered. "what is in the
    csv?" with a CSV uploaded AND a workbook made still reaches the read-back:
    this platform publishes CSV artifacts (artifacts/formats.py's `data`
    template), so the word names either file and no words rule separates them.
    Closing it needs the picked artifact's KIND at the route, which the route
    does not have until the engine picks it — the same limit
    `_carries_a_file_to_read` already records for "what does this document
    say?". The mitigation is in the reply: the read-back opens with the
    artifact's own title and kind, so the person can see which file was read."""
    for text in ("what is in the csv?", "how many rows does the csv have?"):
        decided = _intent_that_asks_about(text, has_dataset=True)
        assert decided.names_our_file is True, (text, decided.rule)
        assert D.answers_from_spec(decided, has_dataset=True) is True, (text, decided.rule)


def test_the_measured_cost_of_the_dataset_arm_over_the_whole_labelled_class():
    """The class, not a sample: of the 49 turns the programme's corpus labels
    `answer_about_artifact`, exactly THREE lose the read-back when a dataset is
    in the room. Each names a content noun and no file, and `columns`/`rows`
    are what a workbook and a CSV have in common, so no words rule separates
    them; they go where origin/dev sent them, to the dataset engine.

    This is a CEILING, asserted so the cost cannot grow unnoticed."""
    import json
    import os

    path = os.environ.get("INTENT_CORPUS") or ""
    if not path or not os.path.isfile(path):
        pytest.skip("the programme corpus is not on this machine (set INTENT_CORPUS)")
    items = json.load(open(path, encoding="utf-8"))["items"]

    def _text_of(item):
        if (item.get("text") or "").strip():
            return item["text"]
        r = item.get("text_build") or {}
        rows = "".join(
            str(r.get("row_template", "")).format(i=i, owner=i % 7, day=1 + i % 28, pri=1 + i % 4)
            for i in range(1, int(r.get("rows", 0)) + 1)
        )
        return str(r.get("prefix", "")) + str(r.get("header", "")) + rows

    labelled = [(it["id"], _text_of(it)) for it in items
                if it.get("want") == "answer_about_artifact"]
    assert len(labelled) == 49, len(labelled)
    lost = []
    for case_id, text in labelled:
        decided = _intent_that_asks_about(text, has_dataset=True)
        if not D.is_artifact_question(decided):
            # q39 (Arabic) is the one turn of the 49 the gate does not answer
            # even alone — the corpus score is 48/49, not 49/49 — so it is not
            # this arm's cost.
            continue
        if not D.answers_from_spec(decided, has_dataset=True):
            lost.append(case_id)
    assert lost == ["q13", "q14", "q21"], lost


# ---------------------------------------------------------------------------
# ROUND 3 (2026-09-28). Three more ways `names_our_file` said "our file" about a
# question whose subject was the person's own data. Each was measured through
# POST /chat with customers.csv and a workbook in one conversation, and each was
# answered "**Workflow Tracker** (v1) is a workbook with 1 sheet: `Tasks`"; the
# whole turns are in tests/test_artifact_question_route.py.


def test_the_content_only_nouns_are_the_difference_of_the_two_lists():
    """`_Q_CONTENT_ONLY_NOUN` is the content nouns MINUS the file words, and it
    is computed from them rather than restated. This pins the difference: today
    exactly one noun is in both lists, `sheets?`, and it stays a pointer because
    "this sheet" names a file. Adding an overlapping word to either list changes
    what points at our file, so it should fail here and be decided on purpose."""
    from app.artifacts import intent as I

    overlap = sorted(set(I._Q_CONTENT_NOUN_WORDS) & set(I._Q_FILE_WORD_WORDS))
    assert overlap == ["sheets?"], overlap
    assert I._Q_CONTENT_NOUN == I._alt(I._Q_CONTENT_NOUN_WORDS)
    assert I._Q_FILE_WORD == I._alt(I._Q_FILE_WORD_WORDS)
    assert I._Q_CONTENT_ONLY_NOUN == I._alt(
        w for w in I._Q_CONTENT_NOUN_WORDS if w not in I._Q_FILE_WORD_WORDS)
    # The one that stays a pointer, and one that does not.
    assert I._Q_CONTENT_ONLY_NOUN_RE.search("sheet") is None
    assert I._Q_CONTENT_ONLY_NOUN_RE.search("data") is not None


#: N1 — A REFUSAL IS NOT A POINTER. "Don't make a file, just tell me X" says
#: which OUTPUT the person wants; X says what the question is about.
HELD_OUT_A_REFUSAL_IS_NOT_A_POINTER = (
    "dont create a file, just tell me the total spend",
    "please tell me only, do not make a new file - what is the average spend per country?",
    "sirf bata do nayi file mat banao - total spend kitna hai",
    "don't create anything, what is the average order value?",
    "no new file please, which country spent the most?",
)
#: …and the same refusal when the message names NOTHING ELSE. Then the file card
#: the last assistant turn showed is the only thing left for it to be about, and
#: the read-back is right. The first two are the production transcript's own
#: turns, which is why this arm exists at all.
HELD_OUT_A_REFUSAL_THAT_NAMES_NOTHING_ELSE = (
    "i want to Know ?? please tell me Only Not create d??",
    "please tell me Only Not create",
    "i dont want a new one, just tell me",
    "just tell me",
)


@pytest.mark.parametrize("text", HELD_OUT_A_REFUSAL_IS_NOT_A_POINTER)
def test_a_refusal_of_a_new_file_does_not_point_at_our_file(text):
    decided = _intent_that_asks_about(text, has_dataset=True)
    assert decided.names_our_file is False, (text, decided.rule)
    assert D.answers_from_spec(decided, has_dataset=True) is False, (text, decided.rule)


@pytest.mark.parametrize("text", HELD_OUT_A_REFUSAL_THAT_NAMES_NOTHING_ELSE)
def test_a_refusal_that_names_nothing_else_still_points_at_our_file(text):
    decided = _intent_that_asks_about(text, has_dataset=True)
    assert decided.names_our_file is True, (text, decided.rule)
    assert D.answers_from_spec(decided, has_dataset=True) is True, (text, decided.rule)


def test_the_refusal_rule_is_read_on_the_unblanked_text():
    """The view is the rule's other half. `_rule_view` blanks negated clauses so
    the rest of the message can decide, and on these turns that blanks the
    question: "sirf bata do nayi file mat banao - total spend kitna hai" reduces
    to a single space there. A first attempt at this rule read that view and let
    a turn naming a total through."""
    from app.artifacts import intent as I
    from app.artifacts import lexicon as LX

    text = "sirf bata do nayi file mat banao - total spend kitna hai"
    blanked = I._rule_view(I._clean(I._own_prose(text)))
    assert blanked.strip() == "", repr(blanked)
    unblanked = LX.normalize(I._clean(I._own_prose(text)).lower())
    assert "total" in unblanked, repr(unblanked)
    assert I._names_nothing_but_the_ask(blanked) is True
    assert I._names_nothing_but_the_ask(unblanked) is False


#: N2 — THE SUBTRACTION WAS APPLIED TO ONE ARM AND NOT THE OTHER. `the data`
#: was not a pointer and `this data` was, one word apart; and
#: `lexicon` rewrites `isme` and `is <noun>` to `_this_`, so both Hinglish
#: forms took the bare-demonstrative arm.
HELD_OUT_A_BARE_DEMONSTRATIVE_IS_NOT_A_POINTER = (
    "which countries are in this data?",
    "which countries are in the data?",
    "what is in that data?",
    "isme total spend kitna hai ??",
    "is data me total spend kitna hai ??",
)
#: …and the bare demonstrative that IS a pointer, because nothing a dataset owns
#: follows it. Losing these would be the other way of getting N2 wrong, and it
#: is the verifier's measured warning: a lookahead on ANY following word costs
#: six more of the 49.
HELD_OUT_A_BARE_DEMONSTRATIVE_THAT_STILL_POINTS = (
    "what is in it?",
    "what is in this sheet",
    "is sheet me kya kya hai ??",
    "how many rows are in it?",
    "what is on slide 3?",
    "does it have a status column?",
)


@pytest.mark.parametrize("text", HELD_OUT_A_BARE_DEMONSTRATIVE_IS_NOT_A_POINTER)
def test_a_demonstrative_followed_by_a_content_noun_is_not_a_pointer(text):
    decided = _intent_that_asks_about(text, has_dataset=True)
    assert decided.names_our_file is False, (text, decided.rule)
    assert D.answers_from_spec(decided, has_dataset=True) is False, (text, decided.rule)


@pytest.mark.parametrize("text", HELD_OUT_A_BARE_DEMONSTRATIVE_THAT_STILL_POINTS)
def test_a_demonstrative_that_names_no_dataset_noun_after_it_still_points(text):
    decided = _intent_that_asks_about(text, has_dataset=True)
    assert decided.names_our_file is True, (text, decided.rule)
    assert D.answers_from_spec(decided, has_dataset=True) is True, (text, decided.rule)


#: N3 — `_Q_SOV_RE` CANNOT BE A POINTER. Its first arm is <wh> … `_in_` with no
#: file reference in it at all, and its second ORs `_Q_CONTENT_NOUN` back in —
#: the exact set the pointer subtracts. So every Indian-language value question
#: reached the workbook while its English equivalent reached the dataset engine.
HELD_OUT_THE_SOV_ORDER_WITHOUT_A_FILE_WORD = (
    "\u0921\u0947\u091f\u093e \u092e\u0947\u0902 \u0915\u0941\u0932 spend \u0915\u093f\u0924\u0928\u093e \u0939\u0948 ?",
    "data \u092e\u0947\u0902 \u0915\u0941\u0932 spend \u0915\u093f\u0924\u0928\u093e \u0939\u0948 ?",
    "rows \u092e\u0947\u0902 \u0915\u093f\u0924\u0928\u0947 countries \u0939\u0948\u0902 ?",
    # `\b` at the front of the pattern: `it` closes ordinary words, so without
    # it "credit note _in_ kya hai" matched through the `it` inside "credit".
    "credit note \u092e\u0947\u0902 \u0915\u094d\u092f\u093e \u0939\u0948 ?",
    "profit summary \u092e\u0947\u0902 \u0915\u093f\u0924\u0928\u093e \u0939\u0948 ?",
)
#: …and the SOV order that DOES carry a file word, which is what the pointer is
#: for: corpus q36 "sheet _in_ su su che" and its neighbours.
HELD_OUT_THE_SOV_ORDER_WITH_A_FILE_WORD = (
    "sheet me su su che ??",
    "is sheet me kya kya hai ??",
    "report me kitne pages hai ??",
    "sheet ma su su che ae kaho, navi file na banavo",
)


@pytest.mark.parametrize("text", HELD_OUT_THE_SOV_ORDER_WITHOUT_A_FILE_WORD)
def test_the_sov_order_without_a_file_word_is_not_a_pointer(text):
    decided = _intent_that_asks_about(text, has_dataset=True)
    assert decided.names_our_file is False, (text, decided.rule)
    assert D.answers_from_spec(decided, has_dataset=True) is False, (text, decided.rule)


@pytest.mark.parametrize("text", HELD_OUT_THE_SOV_ORDER_WITH_A_FILE_WORD)
def test_the_sov_order_with_a_file_word_still_points_at_our_file(text):
    decided = _intent_that_asks_about(text, has_dataset=True)
    assert decided.names_our_file is True, (text, decided.rule)
    assert D.answers_from_spec(decided, has_dataset=True) is True, (text, decided.rule)


def test_the_word_boundary_at_the_front_of_the_sov_pointer_is_load_bearing():
    """Measured 2026-09-28: without the leading `\b`, `credit`, `profit` and
    `deposit` all end in `it`, which is the pointer's first alternative, so any
    Hindi turn that put one of them before a postposition pointed a question
    about the person's own credit note at our workbook."""
    import re

    from app.artifacts import intent as I

    without = re.compile(
        rf"(?:{I._Q_OUR_FILE}|\b{I._Q_FILE_WORD})(?:\W+\w+){{0,4}}?\W+_in_\b", re.I)
    for norm in ("credit note _in_ kya hai", "profit summary _in_ kitna hai",
                 "deposit slip _in_ kya hai"):
        assert without.search(norm) is not None, norm
        assert I._Q_SOV_OUR_FILE_RE.search(norm) is None, norm
    # …and the pointer it exists for is untouched.
    for norm in ("sheet _in_ su su che", "_this_ sheet _in_ kya kya hai",
                 "report _in_ kitne pages hai"):
        assert I._Q_SOV_OUR_FILE_RE.search(norm) is not None, norm


def test_the_first_arm_of_the_sov_shape_carries_no_file_reference():
    """Why `_Q_SOV_RE` could not be the pointer, asserted rather than asserted
    in prose: its first alternative is a question word and a postposition, with
    nothing between them that names a file. It stays what it is — the shape test
    that says "this is a question" — and the pointer is its own pattern."""
    from app.artifacts import intent as I

    norm = "kitna _in_ hai"
    assert I._Q_SOV_RE.search(norm) is not None, norm
    assert I._Q_SOV_OUR_FILE_RE.search(norm) is None, norm
