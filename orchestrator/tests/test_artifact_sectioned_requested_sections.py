"""The sectioned writer is told how many sections the request named.

The owner's complaint of 2026-09-22 — fifteen numbered sections, eight in
the file — was still live on 2026-09-27, and this is the layer it lives at.
"a detailed technical report" is an EXPLICIT size (3,000 words), explicit
alone buys the sectioned writer at every effort including Fast, and that
path builds its prompts in `outline()` and `_write_one_section()`. Both
called `_material_messages` WITHOUT `requested`, and `outline()` recomputed
`caps_for` without it too, so on the owner's request, measured in
sf-local-ai-orchestrator-1 on 2026-09-27:

    caps_for(fast, target, requested) = (17, 12)
    caps_for(fast, target) NO requested   = (8, 12)
    | ... Limits: at most 8 top-level sections, 12 slides, 3 sheets. ...
    | Plan 8 sections (at most 8), each worth about 375 words, ...
    'none skipped' line present? system: False | user: False
    SECTION WRITER prompt: limit line: 'at most 8 top-level sections'

The model was told EIGHT, twice, for a request that numbered fifteen — and
`_requested_line`, which carries "none skipped, none merged into another, none renamed"
and the fifteen names, never reached this path at all.

The second half of the file is the coverage repair. A sectioned draft that
misses sections used to be repaired by ONE whole-document `_compose_once`
call, which on a create does not carry the draft: measured on the same day,
an 8-section 1,621-word draft that had cost 12 calls asked for 7 missing
sections, got one blind 12,000-token rewrite, and `_worse()` refused it —
13 calls, nothing added, three warnings on the card.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app import llm
from app.artifacts import compose as C
from app.artifacts import spec as S
from app.artifacts import types as T
from tests.test_artifact_compose import FIFTEEN_SECTIONS

#: The owner's request with a size WORD in it, which is the shape that takes
#: the sectioned writer at Fast: "detailed" is explicit (3,000 words), and
#: 3,000 > SECTIONED_WRITER_WORDS.
EXPLICIT_FIFTEEN = (
    "Create a detailed technical report titled:\n"
    '"Enterprise Local AI Platform - Technical Overview"\n'
    "Requirements: 1. Executive Summary 2. Architecture Overview 3. Hardware Layer "
    "4. AI Inference Layer 5. Backend Architecture 6. Frontend Architecture "
    "7. Database Architecture 8. RAG Pipeline 9. Authentication and Authorization "
    "10. Security 11. Monitoring 12. Scaling Strategy 13. Failure Recovery "
    "14. Performance Optimization 15. Conclusion\n"
    "Do not skip any section."
)


def _req(instruction=EXPLICIT_FIFTEEN, *, effort="fast"):
    return C.ComposeRequest(kind="document", formats=["pdf"], template_id="generic", effort=effort,
                            instruction=instruction, material=C.Material(instruction=instruction))


def _section_answer(heading, words):
    return {"blocks": [{"type": "heading", "level": 1, "text": heading},
                       {"type": "paragraph", "text": " ".join([heading.split()[0].lower()] * words)}]}


class _Recorder:
    """A scripted `llm.json_completion` that keeps every prompt it was sent.

    `plan_sections` pins how many sections the outline answers with, so a
    test can hold the outline still and measure only the repair.
    """

    def __init__(self, *, plan_sections=None, section_words=200):
        self.plan_sections = plan_sections
        self.section_words = section_words
        self.calls = []

    def prompts(self, name):
        return [c["prompt"] for c in self.calls if c["name"] == name]

    def names(self):
        return [c["name"] for c in self.calls]

    async def __call__(self, messages, *, json_schema=None, schema_name="", temperature=0.0,
                       max_tokens=None, thinking=False, effort=None):
        system = messages[0]["content"]
        user = "\n\n".join(m["content"] for m in messages[1:])
        self.calls.append({"name": schema_name, "system": system, "user": user,
                           "prompt": system + "\n\n" + user, "max_tokens": max_tokens})
        if schema_name == "artifact_outline":
            want = self.plan_sections
            if want is None:
                want = 8
                for line in system.splitlines():
                    if line.strip().startswith("Plan ") and " sections (at most " in line:
                        want = int(line.strip().split()[1])
            return json.dumps({
                "title": "Enterprise Local AI Platform", "audience": "engineering", "purpose": "the platform",
                "sections": [{"heading": h, "purpose": f"what {h} covers", "elements": ["paragraphs"]}
                             for h in FIFTEEN_SECTIONS[:want]],
                "needs_current_facts": False, "assumptions": []})
        if schema_name == "artifact_section_write":
            heading = next((h for h in FIFTEEN_SECTIONS if f"“{h}”" in user), FIFTEEN_SECTIONS[0])
            return json.dumps(_section_answer(heading, self.section_words))
        if schema_name == "artifact_document":
            return json.dumps({"title": "Enterprise Local AI Platform", "template_id": "generic",
                               "blocks": [b for h in FIFTEEN_SECTIONS
                                          for b in _section_answer(h, 40)["blocks"]],
                               "sources": [], "assumptions": []})
        return json.dumps({"ok": True, "issues": []})


def _limit_line(prompt):
    """The "Limits: at most N top-level sections" clause, as the model reads it."""
    for line in prompt.splitlines():
        if "Limits: at most" in line:
            return line[line.index("Limits:"):].split(",")[0]
    return ""


# ------------------------------------------ what the sectioned path is told --


def test_explicit_alone_buys_the_sectioned_writer_at_fast(monkeypatch):
    """CORRECTING THE RECORD. An audit of 2026-09-27 closed this complaint
    with "Fast is excluded from the sectioned writer on purpose". It is not:
    only a DERIVED target is excluded at Fast. `target.explicit` alone
    satisfies the chooser at every effort, so the owner's Fast request was on
    the sectioned path the whole time — which is why the prompts below are
    the layer the defect lives at.

    This runs the chooser rather than restating it, so it fails if anyone
    ever "fixes the record" by excluding Fast for real.
    """
    fast = T.EFFORT_BUDGETS["fast"]
    assert fast.outline_pass is False, "Fast has no outline pass, which is what the derived guard reads"

    explicit = C.target_for(_req())
    assert explicit.explicit is True and explicit.words == 3_000 > C.SECTIONED_WRITER_WORDS

    model = _Recorder()
    monkeypatch.setattr(llm, "json_completion", model)
    monkeypatch.setattr(llm, "get_finish_reason", lambda: "stop")
    result = asyncio.run(C.compose(_req()))
    assert any(w.startswith(C.LONG_DOCUMENT_NOTE) for w in result.warnings), result.warnings
    assert "artifact_outline" in model.names() and "artifact_section_write" in model.names(), model.names()
    assert "artifact_document" not in model.names(), \
        "an explicit size at Fast is written section by section, not in one call"

    # And the derived case, which is the one that really is excluded at Fast:
    # the owner's request WITHOUT a size word takes the single call.
    from tests.test_artifact_length import OWNER_PROMPT

    derived = C.target_for(_req(OWNER_PROMPT))
    assert derived.explicit is False and derived.words > C.SECTIONED_WRITER_WORDS
    model2 = _Recorder()
    monkeypatch.setattr(llm, "json_completion", model2)
    result2 = asyncio.run(C.compose(_req(OWNER_PROMPT)))
    assert model2.names() == ["artifact_document"], model2.names()
    assert not any(w.startswith(C.LONG_DOCUMENT_NOTE) for w in result2.warnings), result2.warnings


def test_the_outline_of_a_sectioned_document_is_told_the_sections_the_request_named(monkeypatch):
    """The defect, at the outline. Before the fix this prompt said "at most
    8 top-level sections" and "Plan 8 sections (at most 8)" for a request
    that numbered fifteen, and carried none of their names."""
    req = _req()
    requested = C.requested_sections(req.instruction)
    assert len(requested) == 15
    target = C.target_for(req)
    budget = T.EFFORT_BUDGETS["fast"]
    assert C.caps_for(budget, target, requested) == (17, 12)
    assert C.caps_for(budget, target) == (8, 12), "the floor that was being quoted at the model"

    model = _Recorder()
    monkeypatch.setattr(llm, "json_completion", model)

    async def say(pct, detail):
        return None

    raw, plan, calls, warnings, stopped = asyncio.run(
        C.compose_sectioned(req, budget, target, say=say, requested=requested))

    outline_call = next(c for c in model.calls if c["name"] == "artifact_outline")
    assert _limit_line(outline_call["system"]) == "Limits: at most 17 top-level sections", outline_call["system"]
    plan_line = next(l.strip() for l in outline_call["system"].splitlines() if l.strip().startswith("Plan "))
    assert plan_line.startswith("Plan 15 sections (at most 17)"), plan_line
    assert "about 200 words" in plan_line, plan_line

    # The names, and the sentence that says none of them may be dropped —
    # in the USER message, never the system one (the boundary
    # test_artifact_length.py pins).
    assert "none skipped, none merged into another, none renamed" in outline_call["user"]
    assert "none skipped" not in outline_call["system"]
    for name in ("Failure Recovery", "Performance Optimization", "Conclusion"):
        assert name in outline_call["user"]

    # And a plan of fifteen is written as fifteen.
    assert len(plan["sections"]) == 15
    assert [b["text"] for b in raw["blocks"]
            if b.get("type") == "heading" and b.get("level") == 1] == FIFTEEN_SECTIONS


def test_every_section_call_of_a_sectioned_document_carries_the_requested_sections(monkeypatch):
    """The same defect at the other prompt: a call whose whole job is to
    write ONE section was quoting Fast's eight-section floor at the model."""
    req = _req()
    requested = C.requested_sections(req.instruction)
    target = C.target_for(req)
    model = _Recorder()
    monkeypatch.setattr(llm, "json_completion", model)

    async def say(pct, detail):
        return None

    asyncio.run(C.compose_sectioned(req, T.EFFORT_BUDGETS["fast"], target, say=say, requested=requested))

    section_calls = [c for c in model.calls if c["name"] == "artifact_section_write"]
    assert len(section_calls) == 15
    for c in section_calls:
        assert _limit_line(c["system"]) == "Limits: at most 17 top-level sections", _limit_line(c["system"])
        assert "none skipped, none merged into another, none renamed" in c["user"]
        assert "none skipped" not in c["system"], "the names stay out of the system message"
    # It still writes ONE section: the one-section instruction is in the
    # prompt and `_as_section` is the code that holds the answer to it.
    assert "YOU ARE WRITING ONE SECTION" in section_calls[0]["system"]
    assert "WRITE SECTION 9 OF 15: “Authentication and Authorization”" in section_calls[8]["user"]


# ---------------------------------------------- the coverage repair --------


def _compose_with_a_short_plan(monkeypatch, *, plan_sections=8, effort="fast"):
    """A sectioned run whose outline answers with only `plan_sections` of the
    fifteen, so the coverage repair is what the test measures."""
    req = _req(effort=effort)
    model = _Recorder(plan_sections=plan_sections)
    monkeypatch.setattr(llm, "json_completion", model)
    monkeypatch.setattr(llm, "get_finish_reason", lambda: "stop")
    seen = {}
    original = C._write_missing_sections

    async def spy(*a, **kw):
        spec = a[3]
        seen["before"] = [b.model_dump(mode="json") for b in spec.body.blocks]
        seen["missing"] = list(a[5])
        return await original(*a, **kw)

    monkeypatch.setattr(C, "_write_missing_sections", spy)
    return req, model, seen, asyncio.run(C.compose(req))


def test_a_sectioned_draft_that_misses_sections_is_repaired_one_section_at_a_time(monkeypatch):
    """The repair writes only what is missing, and the draft it repairs
    survives byte-for-byte. Before the fix this was ONE whole-document call
    that could not see the draft at all."""
    req, model, seen, result = _compose_with_a_short_plan(monkeypatch)

    assert "artifact_document" not in model.names(), \
        "the blind whole-document rewrite must not be spent on a sectioned draft"
    assert seen["missing"] == FIFTEEN_SECTIONS[8:], seen["missing"]

    final = [b.model_dump(mode="json") for b in result.spec.body.blocks]
    before = seen["before"]
    assert json.dumps(final[: len(before)]) == json.dumps(before), \
        "every block the sectioned writer wrote must survive the repair byte-for-byte"

    assert [b.text for b in result.spec.body.blocks
            if isinstance(b, S.Heading) and b.level == 1] == FIFTEEN_SECTIONS
    assert not any("requested sections not found" in w for w in result.warnings), result.warnings
    assert not any("dropped most of the content" in w for w in result.warnings), result.warnings
    # One call per missing section: eight planned + three extensions + seven.
    assert model.names().count("artifact_section_write") == 8 + 3 + 7, model.names()


def test_the_repair_puts_a_missing_section_where_the_request_asked_for_it(monkeypatch):
    """A section missing from the MIDDLE goes back in its own place, not at
    the end — and the blocks around it keep their order and their bytes."""
    req = _req()
    requested = C.requested_sections(req.instruction)
    target = C.target_for(req)
    keep = [h for h in FIFTEEN_SECTIONS if h != "RAG Pipeline"]

    model = _Recorder()
    monkeypatch.setattr(llm, "json_completion", model)
    monkeypatch.setattr(llm, "get_finish_reason", lambda: "stop")

    real_outline = C.outline

    async def short_outline(*a, **kw):
        plan = await real_outline(*a, **kw)
        plan["sections"] = [s for s in plan["sections"] if s["heading"] != "RAG Pipeline"]
        return plan

    monkeypatch.setattr(C, "outline", short_outline)
    seen = {}
    original = C._write_missing_sections

    async def spy(*a, **kw):
        seen["before"] = [b.model_dump(mode="json") for b in a[3].body.blocks]
        return await original(*a, **kw)

    monkeypatch.setattr(C, "_write_missing_sections", spy)
    result = asyncio.run(C.compose(req))

    heads = [b.text for b in result.spec.body.blocks if isinstance(b, S.Heading) and b.level == 1]
    assert heads == FIFTEEN_SECTIONS, heads
    final = [b.model_dump(mode="json") for b in result.spec.body.blocks]
    before = seen["before"]
    # Same objects, same order, nothing rewritten: the draft's blocks are an
    # ordered subsequence of the delivered document, byte for byte.
    it = iter(final)
    assert all(any(json.dumps(f) == json.dumps(b) for f in it) for b in before), \
        "the draft's blocks must survive the repair in order, byte for byte"


def test_a_sectioned_writer_that_ran_out_of_time_is_not_asked_for_more(monkeypatch):
    """The repair is a model call per missing section. A writer that already
    hit its wall clock does not buy another round of them."""
    req = _req()
    model = _Recorder(plan_sections=15)
    monkeypatch.setattr(llm, "json_completion", model)
    monkeypatch.setattr(llm, "get_finish_reason", lambda: "stop")
    # Two sections' worth of stage budget: the writer stops early and says so.
    monkeypatch.setattr(C, "_stage_budget_s", lambda: 1.0)
    called = []
    original = C._write_missing_sections

    async def spy(*a, **kw):
        called.append(True)
        return await original(*a, **kw)

    monkeypatch.setattr(C, "_write_missing_sections", spy)
    result = asyncio.run(C.compose(req))
    assert any("the time this job is allowed ran out" in w for w in result.warnings), result.warnings
    assert called == [], "no repair calls after the writer ran out of time"
    assert "artifact_document" not in model.names(), model.names()


def test_place_sections_adds_and_never_moves_or_rewrites(monkeypatch):
    """`_place_sections` on its own: the front, the middle and the tail. The
    existing blocks come out as the SAME objects in the same relative order —
    identity, not equality, because a repair that rewrote a block would still
    be equal to itself after a round trip through JSON."""
    def head(text):
        return S.Heading(level=1, text=text)

    def para(text):
        return S.Paragraph(text=text)

    requested = ["Alpha", "Beta", "Gamma", "Delta"]
    lead = para("an opening line before any heading")
    beta, beta_body = head("Beta"), para("beta body")
    delta, delta_body = head("Delta"), para("delta body")
    existing = [lead, beta, beta_body, delta, delta_body]

    alpha = [S.Heading(level=1, text="Alpha"), S.Paragraph(text="alpha body")]
    gamma = [S.Heading(level=1, text="Gamma"), S.Paragraph(text="gamma body")]
    out = C._place_sections(existing, requested, {0: alpha, 2: gamma})

    assert [b.text for b in out if isinstance(b, S.Heading)] == ["Alpha", "Beta", "Gamma", "Delta"]
    # The preamble stays first, ahead of the section that was put at the front.
    assert out[0] is lead
    # Every existing block is the same object, in its original order.
    kept = [b for b in out if any(b is e for e in existing)]
    assert [id(b) for b in kept] == [id(b) for b in existing]

    # A tail section, and a document with no level-1 heading at all.
    tail = [S.Heading(level=1, text="Delta"), S.Paragraph(text="delta again")]
    assert C._place_sections([lead], requested, {3: tail})[0] is lead
    assert [b.text for b in C._place_sections([lead], requested, {3: tail})
            if isinstance(b, S.Heading)] == ["Delta"]


# ------------------------------- what the repair costs, and what it says ----


def test_the_card_counts_the_sections_delivered_not_the_ones_planned(monkeypatch):
    """The long-document note is written before the repair runs, so it has to
    be re-read from the repaired file afterwards.

    Measured on the owner's request 2026-09-27, with the repair in and this
    guard out: eight sections planned, FIFTEEN delivered, nineteen model
    calls — and the card said "written in 8 sections over 12 model calls".
    His complaint of 2026-09-22 was our own arithmetic quoted back at him as
    if it were his request; a number nobody can check against the file is
    the same defect with a better answer behind it.
    """
    req, model, seen, result = _compose_with_a_short_plan(monkeypatch)

    delivered = [b.text for b in result.spec.body.blocks
                 if isinstance(b, S.Heading) and b.level == 1]
    assert delivered == FIFTEEN_SECTIONS, delivered
    note = next(w for w in result.warnings if w.startswith(C.LONG_DOCUMENT_NOTE))
    assert "written in 15 sections over 19 model calls" in note, note
    assert result.model_calls == 19, result.model_calls
    # 8 planned + 3 extensions + 7 repaired = 18 writes, plus the outline.
    assert model.names().count("artifact_section_write") == 18, model.names()
    assert result.warnings[0] is note, "the answer carries two warnings; this one goes first"
    # And the tail the reply strips is still the shape types.py matches.
    assert T._MODEL_CALLS_RE.search(note) is not None, note


def test_the_repair_yields_to_live_chat_between_its_sections(monkeypatch):
    """The repair is up to seven more back-to-back section calls on the same
    TP=2 engine somebody is chatting to. `compose_sectioned` consults
    `pipeline.pace()` once per section call; this loop is the same loop and
    consults it the same way."""
    from app.artifacts import pipeline as P

    seen = {"n": 0}

    async def _pace():
        seen["n"] += 1
        return 0.0

    monkeypatch.setattr(P, "pace", _pace)
    req, model, spied, result = _compose_with_a_short_plan(monkeypatch)
    # 8 planned + 3 extensions are the writer's; the 7 repaired are this loop's.
    assert model.names().count("artifact_section_write") == 18, model.names()
    assert seen["n"] == 18, f"one pace() per section call, got {seen['n']}"


def test_the_repair_stops_at_the_ceiling_the_file_format_allows(monkeypatch):
    """A repair may only ADD, so it stops at the document ceiling instead of
    dropping sections off the end the way the sectioned writer does.

    It matters because the splice assigns straight to `body.blocks`, and
    pydantic does not re-validate on assignment: without this the document
    handed to the renderer can be one `S.DocumentSpec` would refuse.
    """
    # Four blocks a section, so the ceiling falls in the middle of the repair.
    monkeypatch.setattr(C, "DOCUMENT_BLOCK_CEILING", 20)
    req, model, seen, result = _compose_with_a_short_plan(monkeypatch)

    blocks = list(result.spec.body.blocks)
    assert len(blocks) < 20 + 8, f"the repair ran past the ceiling: {len(blocks)} blocks"
    assert any("the document is already as long as the file format allows" in w
               for w in result.warnings), result.warnings
    # What it could not add is still named, not silently dropped.
    left = next(w for w in result.warnings if w.startswith("requested sections not found"))
    assert "Conclusion" in left, left
    # And what it delivered is a document the schema still accepts.
    S.DocumentSpec.model_validate(result.spec.body.model_dump(mode="json"))
