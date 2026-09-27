"""THE SEVEN THINGS LIFTING THE CEILINGS BROKE, each one pinned here.

feat/no-arbitrary-token-ceilings did the thing it set out to do — the owner's
fifteen-section request goes from 1,098 words in ONE model call to 6,033 words
in sixteen — and on the way it reinstated two of his own complaints and added
five more defects. Every test below FAILS on 2b23de4c and passes on this
branch; each names what was measured, so a future change that brings the
defect back says which one it is.

Nothing here needs a database, a GPU or the network.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import re

import pytest

from app import continuation, llm
from app.artifacts import compose as C
from app.artifacts import length as L
from app.artifacts import spec as S
from app.artifacts import types as T
from app.config import settings
from app.core import answer_sampling, best_of, rewrite_shape
from app.engines import chat as chat_engine


# ---------------------------------------------------------------------------
# 1 — a newline-delimited list's last heading is not cut
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text,expected",
    [
        ("Requirements:\n1. Executive Summary\n2. Data Model\n3. Acceptable Use Policy",
         ["Executive Summary", "Data Model", "Acceptable Use Policy"]),
        ("Requirements:\n1. Executive Summary\n2. Data Model\n3. How We Use Data",
         ["Executive Summary", "Data Model", "How We Use Data"]),
        ("Requirements:\n1. Overview\n2. Data Model\n3. Output Format Standards",
         ["Overview", "Data Model", "Output Format Standards"]),
    ],
)
def test_a_newline_delimited_table_of_contents_keeps_its_last_heading(text, expected):
    """`_trim_runover` was applied by `_list_items` to EVERY list.

    It exists for the ONE-LINE list, whose last item has no newline to end it.
    A newline-delimited list's last item is already bounded by `_ITEM_END_RE`,
    so the instruction-verb cut and the sibling backstop could only destroy a
    real heading. Measured on 2b23de4c: 'Acceptable Use Policy' ->
    'Acceptable', 'How We Use Data' -> 'How We', 'Output Format Standards' ->
    'Output' — a wrong heading in the section-writer prompt AND a false
    "requested sections not found in the document" warning on the card, which
    is the defect the trim was added to remove.
    """
    assert C.requested_sections(text) == expected


def test_the_one_line_runover_this_trim_exists_for_is_still_cut():
    """The other half of the same gate: nothing was given back."""
    one_line = ("Requirements: 1. Executive Summary 2. Data Model 3. Conclusion "
                "Use professional Markdown.")
    assert C.requested_sections(one_line) == [
        "Executive Summary", "Data Model", "Conclusion"]


def test_a_bare_ordinal_run_that_runs_over_is_still_cut():
    bare = "1. Executive Summary 2. Data Model 3. Conclusion Use professional Markdown."
    assert C.requested_sections(bare) == [
        "Executive Summary", "Data Model", "Conclusion"]


def test_a_multiline_list_whose_last_line_carries_an_instruction_is_still_cut():
    """The last ITEM has no newline of its own here, so it did run over."""
    text = ("Requirements:\n1. Executive Summary\n2. Data Model\n"
            "3. Conclusion Use professional Markdown.")
    assert C.requested_sections(text) == [
        "Executive Summary", "Data Model", "Conclusion"]


def test_a_one_line_runover_with_no_punctuation_at_all_is_still_cut():
    """The case the smallest fix got wrong, found by sweeping it (r2).

    Keying the trim on "the terminator was not a newline" skips a one-line list
    whose last item runs to the END of the message with no punctuation: the
    terminator is None, so nothing was trimmed, and the whole runover phrase
    then failed the one-to-eight-word filter and the section was dropped
    ENTIRELY - a missing heading instead of a wrong one. A one-line list has no
    newline anywhere, so its last item never had anything to end it; that, not
    the terminator, is what the trim keys on now.
    """
    text = ("Requirements: 1. Executive Summary 2. Data Model 3. Threat Scope "
            "4. Conclusion and then some trailing prose about the file")
    assert C.requested_sections(text) == [
        "Executive Summary", "Data Model", "Threat Scope", "Conclusion and"]


def test_a_newline_list_whose_last_heading_ends_the_message_is_left_alone():
    """The end of the message bounds a multi-line list's last item, so the
    sibling backstop must not reach it even though the terminator is None."""
    text = "Requirements:\n1. Intro\n2. Scope\n3. Security\n4. Acceptable Use Policy"
    assert C._list_items(
        "1. Intro\n2. Scope\n3. Security\n4. Acceptable Use Policy"
    )[-1].strip() == "Acceptable Use Policy"
    assert C.requested_sections(text)[-1] == "Acceptable Use Policy"


# ---------------------------------------------------------------------------
# 2 — a row count in a DOCUMENT request does not switch the derived size off
# ---------------------------------------------------------------------------

FIFTEEN = (
    "Requirements: 1. Executive Summary 2. Architecture Overview 3. Hardware Layer "
    "4. AI Inference Layer 5. Backend Architecture 6. Frontend Architecture "
    "7. Database Architecture 8. RAG Pipeline 9. Authentication and Authorization "
    "10. Security 11. Monitoring 12. Scaling Strategy 13. Failure Recovery "
    "14. Performance Optimization 15. Conclusion"
)


def _doc_request(instruction, effort="fast"):
    return C.ComposeRequest(
        kind="document", formats=["docx", "pdf"], template_id="generic",
        effort=effort, operation="create", instruction=instruction,
        material=C.Material(instruction=instruction),
    )


@pytest.mark.parametrize(
    "phrase",
    ["the 20 rows in the table", "the 12 entries", "the 30 records",
     "the 8 line items"],
)
def test_an_ordinary_row_phrase_does_not_undo_the_derived_document_size(phrase):
    """`parse_size`'s document return carried `explicit=bool(... shape_phrase)`
    where `shape_phrase` comes from `_ROWS_RE`/`_SHEETS_RE`, which match
    rows|records|entries|line items in ANY request — and
    `compose.target_for` derives a document's sections target only `if
    req.kind == "document" and not target.explicit`.

    So one ordinary phrase switched the entire headline fix off. Measured on
    2b23de4c: "Write a technical report about the 20 rows in the table" plus
    fifteen numbered sections -> words=0, explicit=True, sectioned=False,
    against words=6000, explicit=False, sectioned=True on origin/dev. That is
    the owner's four-page one-call document, back.
    """
    text = f"Write a technical report about {phrase}. {FIFTEEN}"
    target = L.parse_size(text, "document")
    assert target.explicit is False
    assert target.rows == 0 and target.sheets == 0

    derived = C.target_for(_doc_request(text))
    assert derived.words == 6_000
    assert derived.words > C.SECTIONED_WRITER_WORDS


def test_a_workbook_still_carries_the_rows_and_sheets_it_named():
    """The branch's real gain. `_shape_tokens` reads rows and sheets for a
    workbook ONLY, which is why keeping them off a document costs nothing."""
    wb = L.parse_size("Build a comprehensive workbook, 5000 rows, 8 sheets", "workbook")
    assert (wb.rows, wb.sheets, wb.explicit) == (5_000, 8, True)


def test_a_deck_still_carries_the_slides_it_named():
    deck = L.parse_size("Make a 30 slide deck", "presentation")
    assert (deck.slides, deck.explicit) == (30, True)


# ---------------------------------------------------------------------------
# 7 — a document target that named no document size is falsy
# ---------------------------------------------------------------------------

def test_a_document_target_that_named_no_document_size_is_falsy():
    """`__bool__` was widened to `words or slides or rows or sheets`, so
    'Summarise these 40 line items' produced a TRUTHY document target whose
    own `phrase` ('Summarise') was not even the shape it matched. No caller
    reads it today, which is exactly why it had to be fixed before one does.
    """
    target = L.parse_size("Summarise these 40 line items", "document")
    assert bool(target) is False


# ---------------------------------------------------------------------------
# 3 — a document written in many calls is not warned about ONE call
# ---------------------------------------------------------------------------

class _Model:
    """Every composer call, in the shape the composer asked for."""

    def __init__(self, names):
        self.names = names
        self.calls = []

    async def __call__(self, messages, *, json_schema=None, schema_name="",
                       temperature=0.0, max_tokens=None, thinking=False,
                       effort=None, **kw):
        self.calls.append(schema_name)
        if schema_name == "artifact_outline":
            return json.dumps({
                "title": "Platform", "audience": "engineers", "purpose": "an overview",
                "sections": [{"heading": h, "purpose": f"what {h} covers",
                              "elements": ["paragraphs"]} for h in self.names],
                "needs_current_facts": False, "assumptions": []})
        if schema_name == "artifact_section_write":
            return json.dumps({"blocks": [
                {"type": "heading", "level": 1, "text": _asked_heading(messages)},
                *_prose(_asked_words(messages))]})
        if schema_name == "artifact_document":
            blocks = []
            for h in self.names:
                blocks.append({"type": "heading", "level": 1, "text": h})
                blocks.extend(_prose(71))
            return json.dumps({"title": "Platform", "template_id": "generic",
                               "blocks": blocks})
        if schema_name == "artifact_review":
            return json.dumps({"verdict": "ok", "problems": []})
        return json.dumps({})


def _prose(words, chunk=600):
    out = []
    while words > 0:
        out.append({"type": "paragraph", "text": " ".join(["spend"] * min(chunk, words))})
        words -= chunk
    return out


def _asked_heading(messages):
    text = "\n".join(m.get("content", "") for m in messages)
    found = re.findall(r"WRITE SECTION \d+ OF \d+: “(.+?)”", text)
    return found[-1].strip() if found else "S"


def _asked_words(messages):
    text = "\n".join(m.get("content", "") for m in messages)
    found = re.findall(r"Write about ([\d,]+) words in this section", text)
    if found:
        return int(found[-1].replace(",", ""))
    found = re.findall(r"about ([\d,]+) words", text)
    return int(found[-1].replace(",", "")) if found else 400


NAMES = ["Executive Summary", "Architecture Overview", "Hardware Layer",
         "AI Inference Layer", "Backend Architecture", "Frontend Architecture",
         "Database Architecture", "RAG Pipeline", "Authentication and Authorization",
         "Security", "Monitoring", "Scaling Strategy", "Failure Recovery",
         "Performance Optimization", "Conclusion"]


def test_a_sectioned_document_is_not_told_it_needs_one_call(monkeypatch):
    """`one_call_shortfall` was evaluated BEFORE the `sectioned` decision, so a
    document delivered in sixteen scoped calls carried "the size asked for
    needs about 60,000 tokens in one model call and this job's time allows
    about 45,360" directly above the true note "written as a long document …
    written in 15 sections over 16 model calls". The two sentences contradict
    each other, and at this stage budget EVERY document over about 11,340
    words (45,360 / TOKENS_PER_WORD) got it.
    """
    model = _Model(NAMES)
    monkeypatch.setattr(llm, "json_completion", model)
    out = asyncio.run(C.compose(_doc_request("Write a 15,000 word report on our platform")))
    warnings = list(getattr(out, "warnings", []) or [])
    spec = getattr(out, "spec", out)

    assert [w for w in warnings if "in one model call" in w] == [], warnings
    assert any("model calls" in w for w in warnings), warnings
    # The document itself is unchanged: the note was false, not the delivery.
    assert len(S.text_of(spec).split()) > 14_000
    assert model.calls.count("artifact_section_write") == 15


def test_a_workbook_that_really_cannot_fit_one_call_is_still_told_so():
    """A workbook has no per-part write path, so for it the ceiling is a REAL
    bound and the note is the honest answer. Measured: 2,400,000 tokens wanted
    against 45,360 one call can decode."""
    budget = T.EFFORT_BUDGETS["fast"]
    target = L.parse_size("Build a comprehensive workbook, 5000 rows, 8 sheets", "workbook")
    want, ceiling = C.one_call_shortfall(
        "workbook", "fast", target, thinking=budget.thinking, budget=budget)
    assert want > ceiling > 0


# ---------------------------------------------------------------------------
# 4 — the chat path's derived FLOOR is not padding for an ordinary question
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "question",
    [
        "Which should I use: 1. Postgres 2. MySQL 3. SQLite",
        "What is the difference between 1. a container 2. a VM 3. a unikernel",
        "Rank these for me: 1. speed 2. cost 3. accuracy",
        "Which should I use: 1. Postgres 2. MySQL 3. SQLite 4. DuckDB 5. Mongo 6. Redis",
        "Pick one for me: 1. Kafka 2. Flink 3. Spark 4. Beam",
        "Is 1. Postgres 2. MySQL or 3. SQLite the fastest?",
    ],
)
def test_an_inline_enumeration_in_a_question_derives_no_word_floor(question):
    """`requested_shape_words`, wired at engines/chat.py, plus
    DERIVED_TARGET_MIN_SECTIONS dropping 6 -> 3 and the new bare-ordinal-run
    trigger, made any three inline numbered items in an ordinary question
    derive a word FLOOR. Measured on 2b23de4c: 1,200 words for each of the
    first three above. 1,200 is over continuation._TARGET_MIN_WORDS = 800, so
    the target was live — `_length_plan` told the FIRST call to plan three
    sections of 400 words and to reach 1,200 before it ended, and a short
    normal stop bought another segment.

    A one-line comparison question got a three-section essay whose size was
    `length.WORDS_PER_SECTION` x 3: a constant deciding the size, which is the
    thing this branch exists to end. Its own outline prompt says "nothing is
    padded to meet a number".
    """
    assert answer_sampling.requested_shape_words(question) is None


@pytest.mark.parametrize(
    "ask,words",
    [
        # The owner's request, in both of its recorded forms.
        (FIFTEEN, 6_000),
        ("Requirements:\n1. Executive Summary\n2. Data Model\n3. Security\n4. Conclusion", 1_600),
        # A table of contents with no heading word at all: still a document.
        ("1. Executive Summary\n2. Data Model\n3. Security\n4. Conclusion", 1_600),
        # An inline list that ASKS for written output.
        ("Give me a detailed document covering 1. Scope 2. Risks 3. Mitigations", 1_200),
        ("Write an essay on 1. Causes 2. Effects 3. Remedies", 1_200),
    ],
)
def test_a_request_that_asks_for_written_output_keeps_its_derived_floor(ask, words):
    """The headline must survive the fix above: what is removed is the floor on
    a QUESTION, not the floor on an ask for a written piece."""
    assert answer_sampling.requested_shape_words(ask) == words


def test_an_explicit_word_count_still_beats_a_derived_one():
    ask = "Write a 2,000 word report. 1. Scope 2. Risks 3. Mitigations"
    assert answer_sampling.requested_words(ask) == 2_000


# ---------------------------------------------------------------------------
# 5 — a continued Max answer is seeded with everything the reader has seen
# ---------------------------------------------------------------------------

WINNER = ("**Summary:** I build systems.\n"
          "**Experience:** I led the platform team and then I")
CONTINUED = " kept going to the end."


def test_a_continued_max_answer_is_seeded_with_the_text_that_was_shown(monkeypatch):
    """`seed=guard.shown` was taken while both holders were still holding.

    `guard.shown` is only what has been RELEASED: `rewrite_shape.Shaper` keeps
    `self._partial` — everything after the last newline — and a winner the
    engine CUT ends mid-line by definition, while `AnswerGuard.feed` can
    return [] holding up to HOLD_CAP_CHARS with no verdict. Measured on
    2b23de4c through the real run_chat_engine: an 80-character winner gave the
    continuation a 30-character seed, so it was told less text existed than
    the reader had been shown and regenerated the missing sentence.

    The fix is to flush both holders BEFORE taking the seed; both finishes are
    re-entrant, so the closing flush still releases whatever the continuation
    leaves held.
    """
    from tests.test_rewrite_shape import REWRITE

    assert rewrite_shape.for_message(REWRITE) is not None, "this test needs a Shaper"
    seen = {}
    emitted = []

    async def fake_candidates(prompt, *, n, temperature, max_tokens):
        return [best_of.Candidate(index=1, reasoning="", answer=WINNER,
                                  finish_reason="length")]

    async def fake_select(question, cands):
        return cands[0], "only one"

    async def fake_long(messages, *, on_delta=None, seed=None, **kw):
        seen["seed"] = seed
        if on_delta is not None:
            await on_delta("token", CONTINUED)
        return continuation.LongResult(text=CONTINUED, stop_reason="complete")

    async def emit(kind, payload):
        if kind == "token":
            emitted.append(payload["text"])

    monkeypatch.setattr(best_of, "generate_candidates", fake_candidates)
    monkeypatch.setattr(best_of, "select_best", fake_select)
    monkeypatch.setattr(continuation, "stream_long_completion", fake_long)
    monkeypatch.setattr(settings, "extra_high_samples", 3, raising=False)

    asyncio.run(chat_engine.run_chat_engine(
        REWRITE, [], emit, mode="assistant", model_choice="smart", effort="max"))

    assert "seed" in seen, "the truncated winner was not continued at all"
    seed = seen["seed"]
    # THE WHOLE WINNER, not only the lines the shaper had released.
    assert "I led the platform team and then I" in seed, seed
    # And the reader saw it before the continuation, not after.
    streamed = "".join(emitted)
    assert streamed.index("I led the platform team") < streamed.index("kept going")


# ---------------------------------------------------------------------------
# 6 — the stored-document budget has a total, not just a per-item share
# ---------------------------------------------------------------------------

def test_the_stored_document_budget_is_shared_between_the_documents():
    """`settings.document_context_chars` was applied PER stored document and
    `db.get_documents` runs its SELECT with no LIMIT, so nothing named a
    total: ten uploads in one conversation was ten times the budget in one
    prompt, where the inline 8000 it replaced gave ten times 8,000.
    `context.fit_request` stops that 400ing but its trim is generic, so WHICH
    document loses text is arbitrary and may be the relevant one.

    The branch's own judge already shares one budget between candidates
    (`best_of._JUDGE_PROMPT_CHARS // max(1, len(usable))`); this is the same
    shape.
    """
    from app.main import _shared_context_chars, MIN_SHARED_CONTEXT_CHARS

    one = _shared_context_chars(1)
    assert one == settings.document_context_chars

    # More documents, a smaller share each, and a floor nobody falls through.
    assert _shared_context_chars(4) <= one
    assert _shared_context_chars(1000) == MIN_SHARED_CONTEXT_CHARS
    # The total is BOUNDED where before it grew with the number of uploads.
    per_item = _shared_context_chars(10)
    assert 10 * per_item < 10 * settings.document_context_chars


def test_both_follow_up_blocks_in_chat_share_their_budget():
    """The stored-PAGE block beside the document one has the same shape and the
    same missing total, so it gets the same fix. Read off the source because
    the handler needs a request, a session and a database to run."""
    src = inspect.getsource(__import__("app.main", fromlist=["x"]))
    body = src[src.index("async def chat"):]
    assert body.count("_shared_context_chars(len(") == 2, (
        "a /chat follow-up block still spends the whole per-item budget on "
        "each item")
