"""What a long document costs, and who asked for one.

Three defects QA measured on the integration tree on 2026-09-18, all of
them about the sectioned writer's price — nine model calls on the TP=2
engine a person is chatting to:

  B-1  a single correction call regenerates the whole file, and `_worse()`
       only refuses it below HALF, so a 47% cut of a nine-call draft was
       accepted and two requested sections were lost with it.
  B-3  ordinary English ("how long does onboarding take?", "the long tail
       of small customers") asked for 3,000 words, which is over
       SECTIONED_WRITER_WORDS, so it bought the nine-call path.
  B-5  `pace()` is consulted once for the whole compose stage, before the
       composer runs; the section loop never yields to live chat again.

The model is stubbed: these pin the CALLS, the SIZE and the ANSWER, which
is what the composer decides.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app import llm
from app.artifacts import compose as C
from app.artifacts import length as L
from app.artifacts import pipeline as P
from app.artifacts import spec as S
from app.artifacts import types as T


class _Model:
    """A scripted `llm.json_completion`: answers in order, records calls."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    async def __call__(self, messages, *, json_schema=None, schema_name="", temperature=0.0, max_tokens=None,
                       thinking=False, effort=None):
        self.calls.append(schema_name)
        if not self.answers:
            raise AssertionError(f"the model was called more times than the script allows ({schema_name})")
        answer = self.answers.pop(0)
        return answer if isinstance(answer, str) else json.dumps(answer)


def _table():
    return C.DataTable(id="upload1", title="customers-100.csv", columns=["Country", "Spend"],
                       rows=[["India", 10], ["Germany", 20], ["Brazil", 30]])


def _req(instruction, *, effort="fast", tables=(), operation="create"):
    material = C.Material(instruction=instruction, tables=list(tables))
    return C.ComposeRequest(kind="document", formats=["docx", "pdf"], template_id="generic", effort=effort,
                            operation=operation, instruction=instruction, material=material)


def _prose(words, chunk=600):
    out = []
    while words > 0:
        out.append({"type": "paragraph", "text": " ".join(["spend"] * min(chunk, words))})
        words -= chunk
    return out


def _outline(headings):
    return {"title": "Customer Report", "audience": "the operations team", "purpose": "what the data shows",
            "sections": [{"heading": h, "purpose": f"what {h} covers", "elements": ["paragraphs"]} for h in headings],
            "needs_current_facts": False, "assumptions": []}


def _section(heading, words):
    return {"blocks": [{"type": "heading", "level": 1, "text": heading}] + _prose(words)}


def _doc(headings, words_each):
    blocks = []
    for h in headings:
        blocks.append({"type": "heading", "level": 1, "text": h})
        blocks.extend(_prose(words_each))
    return {"title": "Customer Report", "template_id": "generic", "blocks": blocks}


def _headings(spec):
    return [b.text for b in spec.body.blocks if getattr(b, "type", "") == "heading" and b.level == 1]


# ------------------------------------------- B-1: a correction may not halve --


def test_a_correction_cannot_halve_a_document_written_section_by_section(monkeypatch):
    """QA reproduced 6,005 words across Overview/Findings/Recommendations
    being replaced, in ONE call, by 3,205 words across Overview/Appendix —
    a 47% cut that also lost two sections the draft already had. _gutted()
    let it through because 3,205 is more than half of 6,005."""
    instruction = ("please give a big report on the customers file with sections Overview, Findings, "
                   "Recommendations and Appendix")
    model = _Model(
        [_outline(["Overview", "Findings", "Recommendations"])]
        + [_section(h, 2_000) for h in ("Overview", "Findings", "Recommendations")]
        # The one correction call that adds the missing "Appendix" — and
        # drops two sections while it is there.
        + [_doc(["Overview", "Appendix"], 1_600)]
    )
    monkeypatch.setattr(llm, "json_completion", model)
    result = asyncio.run(C.compose(_req(instruction, tables=[_table()])))

    words = len(S.text_of(result.spec).split())
    assert words == 6_005, f"the sectioned draft must survive the correction, got {words} words"
    assert _headings(result.spec) == ["Overview", "Findings", "Recommendations"]
    assert any("was not applied" in w and "6,005 words to 3,204" in w for w in result.warnings), result.warnings
    # The one section the model never wrote is still reported honestly.
    assert any(w.startswith("requested sections not found in the document: Appendix") for w in result.warnings)


def test_a_correction_that_keeps_the_document_is_still_applied(monkeypatch):
    """The floor is a floor, not a ban: a correction that adds the missing
    section and keeps the rest is applied exactly as before."""
    instruction = ("please give a big report on the customers file with sections Overview, Findings, "
                   "Recommendations and Appendix")
    model = _Model(
        [_outline(["Overview", "Findings", "Recommendations"])]
        + [_section(h, 2_000) for h in ("Overview", "Findings", "Recommendations")]
        + [_doc(["Overview", "Findings", "Recommendations", "Appendix"], 1_500)]
    )
    monkeypatch.setattr(llm, "json_completion", model)
    result = asyncio.run(C.compose(_req(instruction, tables=[_table()])))
    assert _headings(result.spec) == ["Overview", "Findings", "Recommendations", "Appendix"]
    assert not any("was not applied" in w for w in result.warnings), result.warnings


def test_an_edit_asked_to_be_shorter_may_still_shrink(monkeypatch):
    """`allow_shrink` corrections — shortening, the hollow repair, a
    reviewer asking for a cut — are untouched by the size floor, and an
    edit has no size target at all."""
    parent = S.parse_body("document", _doc(["Overview"], 1_200))
    req = C.ComposeRequest(kind="document", formats=["docx"], template_id="generic", effort="fast",
                           operation="edit", instruction="make this much shorter, half the length",
                           material=C.Material(instruction="make this much shorter, half the length"),
                           parent_spec=parent)
    model = _Model([_doc(["Overview"], 1_400), _doc(["Overview"], 300)])
    monkeypatch.setattr(llm, "json_completion", model)
    result = asyncio.run(C.compose(req))
    assert len(S.text_of(result.spec).split()) == 303, "the shortening correction must still be applied"


# ------------------------------- B-3: ordinary English is not a size word --


@pytest.mark.parametrize("instruction, why", [
    ("how long does onboarding take? write a note", "a question about duration"),
    ("write a report on the long tail of small customers", "a noun phrase"),
    ("a note on why the queue is no longer draining", "an adverb"),
    ("a report on our extended warranty programme", "a product name"),
    ("write up the expanded team onboarding", "a programme name"),
    ("how long is the report?", "a question about the file, not an order for a big one"),
])
def test_ordinary_english_does_not_buy_a_three_thousand_word_document(instruction, why):
    """Each of these measured 3,000 words on the integration tree — over
    SECTIONED_WRITER_WORDS, so each bought one outline call plus eight
    section calls for a request nobody sized."""
    target = L.parse_size(instruction, "document")
    assert target.words == 0, f"{why}: {instruction!r} asked for {target.words} words ({target.phrase!r})"
    assert target.words <= C.SECTIONED_WRITER_WORDS


@pytest.mark.parametrize("instruction, words", [
    ("write me a long report on churn", 3_000),
    ("I want a longer document about the migration", 3_000),
    ("an expanded write-up of the incident", 3_000),
    ("give me an extended report on the queue", 3_000),
    ("a long detailed report on churn", 3_000),
    ("give me a big report on the customers file", 3_000),
    ("write a 5,000-word article on churn", 5_000),
])
def test_a_size_word_next_to_the_deliverable_still_asks_for_a_big_document(instruction, words):
    assert L.parse_size(instruction, "document").words == words


def test_a_growth_word_far_from_the_deliverable_no_longer_beats_a_shrink_word():
    """`shrink_asked` reads the same guard, so "summarise the long weekend
    incident" is a summary again rather than a 3,000-word report."""
    assert L.shrink_asked("summarise the long weekend incident") is True
    assert L.shrink_asked("a short note on the long tail of small customers") is True
    # A real growth word still beats a shrink word in the same sentence.
    assert L.shrink_asked("a long report with an executive summary") is False


def test_the_answer_says_when_a_long_document_was_chosen(monkeypatch):
    """The sectioned writer is the expensive path. The person is told their
    wording bought it, which number it was read as, and what it cost."""
    model = _Model([_outline([f"Section {i}" for i in range(3)])]
                   + [_section(f"Section {i}", 1_100) for i in range(3)])
    monkeypatch.setattr(llm, "json_completion", model)
    result = asyncio.run(C.compose(_req("give me a big report on the customers file", tables=[_table()])))
    note = next((w for w in result.warnings if w.startswith(C.LONG_DOCUMENT_NOTE)), None)
    assert note is not None, result.warnings
    assert "“big”" in note and "3,000 words" in note and "3 sections" in note and "4 model calls" in note, note
    assert result.warnings[0] is note, "the answer carries at most two warnings; this one goes first"

    # A document nobody sized says nothing of the sort.
    plain = _Model([{"title": "Note", "template_id": "generic",
                     "blocks": [{"type": "heading", "level": 1, "text": "Note"}] + _prose(300)}])
    monkeypatch.setattr(llm, "json_completion", plain)
    quiet = asyncio.run(C.compose(_req("write a note about the price change")))
    assert not any(w.startswith(C.LONG_DOCUMENT_NOTE) for w in quiet.warnings), quiet.warnings


# ---------------------------------- B-5: the section loop yields to chat --


def test_the_sectioned_writer_yields_to_live_chat_between_sections(monkeypatch):
    """`pipeline.pace()` was consulted 0 times across a three-section run.
    It is consulted once per section call now, so a person chatting to the
    same TP=2 engine is not stuck behind fourteen back-to-back prefills."""
    seen = {"n": 0}

    async def _pace():
        seen["n"] += 1
        return 0.0

    monkeypatch.setattr(P, "pace", _pace)
    model = _Model([_outline(["One", "Two", "Three"])] + [_section(h, 900) for h in ("One", "Two", "Three")])
    monkeypatch.setattr(llm, "json_completion", model)
    req = _req("please write a big report on the customers file", tables=[_table()])
    asyncio.run(C.compose_sectioned(req, T.EFFORT_BUDGETS["fast"], C.target_for(req),
                                    say=lambda *a, **k: asyncio.sleep(0)))
    assert seen["n"] == 3, f"one pace() per section call, got {seen['n']} for {len(model.calls)} calls"


def test_waiting_for_chat_is_said_on_the_card(monkeypatch):
    async def _pace():
        return 4.0

    monkeypatch.setattr(P, "pace", _pace)
    model = _Model([_outline(["One", "Two"])] + [_section(h, 900) for h in ("One", "Two")])
    monkeypatch.setattr(llm, "json_completion", model)
    req = _req("please write a big report on the customers file", tables=[_table()])
    _raw, _plan, _calls, warnings, _stopped = asyncio.run(
        C.compose_sectioned(req, T.EFFORT_BUDGETS["fast"], C.target_for(req),
                            say=lambda *a, **k: asyncio.sleep(0)))
    assert any("waited 8s between sections so chat could answer" in w for w in warnings), warnings


def test_a_pipeline_that_cannot_pace_is_not_a_failed_job(monkeypatch):
    """Pacing is advisory: the composer keeps working in a tool or a test
    that never imports the pipeline, and a broken probe never fails a job."""
    def _boom():
        raise RuntimeError("no pipeline here")

    monkeypatch.setattr(P, "pace", _boom)
    model = _Model([_outline(["One", "Two"])] + [_section(h, 900) for h in ("One", "Two")])
    monkeypatch.setattr(llm, "json_completion", model)
    req = _req("please write a big report on the customers file", tables=[_table()])
    _raw, _plan, calls, warnings, _stopped = asyncio.run(
        C.compose_sectioned(req, T.EFFORT_BUDGETS["fast"], C.target_for(req),
                            say=lambda *a, **k: asyncio.sleep(0)))
    assert calls == 3 and not any("waited" in w for w in warnings)
