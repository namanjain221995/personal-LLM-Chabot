"""ONE design for the document's size: the request decides the total, the
model decides the split, and main's sectioned writer executes it.

This file closes the collision between `feat/no-arbitrary-token-ceilings-r2`
(PR #83) and `feat/model-decides-document-size-r3`, which rewrote the same
region of compose.py and could not both merge. What is carried, and pinned
here:

  * from the ceilings branch — the section-count PARSE. The owner's recorded
    prompt shape ("...about 3000 words covering:" over fifteen numbered
    items) parsed as ZERO sections on ae25da28, measured in
    sf-local-ai-orchestrator-1 on 2026-09-28:

        requested_n=0 target.words=3000 explicit=True caps_for=(8, 12)
        plan_line='Plan 8 sections (at most 8), each worth about 375 words, ...'

    A bare numbered list with no heading word above it was invisible, a
    heading with a digit in it was discarded, a heading over five words was
    discarded, and a one-line list's last item ran into the sentence after it.

  * from document-size-r3 — the per-item word allocation: the outline says
    how long each section needs to be and the writer follows that SPLIT. The
    TOTAL stays the request's (`_section_targets` scales the figures to it),
    which is where that branch's own reading went wrong: it planned
    `target.section_count` = 8 sections where sectioned-caps plans 15.

Nothing here needs a database, a GPU or the network.
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

# The owner's recorded shape, in both of its plausible line forms. The
# section names are the fixture the rest of the suite already uses.
_HEAD = ('Create a technical report titled "Enterprise Local AI Platform - Technical Overview", '
         "about 3000 words covering:")
_TAIL = "Use professional Markdown. Do not skip any section."
OWNER_MULTI_LINE = _HEAD + "\n" + "\n".join(f"{i}. {n}" for i, n in enumerate(FIFTEEN_SECTIONS, 1)) + "\n" + _TAIL
OWNER_ONE_LINE = _HEAD + " " + " ".join(f"{i}. {n}" for i, n in enumerate(FIFTEEN_SECTIONS, 1)) + " " + _TAIL


def _req(instruction, *, effort="fast"):
    return C.ComposeRequest(kind="document", formats=["pdf"], template_id="generic", effort=effort,
                            instruction=instruction, material=C.Material(instruction=instruction))


# --------------------------------------------------------------- the parse --


@pytest.mark.parametrize("text", [OWNER_MULTI_LINE, OWNER_ONE_LINE], ids=["multi-line", "one-line"])
def test_the_owners_recorded_prompt_shape_names_all_fifteen_sections(text):
    """'covering:' is a heading word now, and the last item of the one-line
    form is 'Conclusion', not 'Conclusion Use professional Markdown'."""
    assert C.requested_sections(text) == FIFTEEN_SECTIONS


@pytest.mark.parametrize("text", [OWNER_MULTI_LINE, OWNER_ONE_LINE], ids=["multi-line", "one-line"])
def test_the_owners_prompt_plans_fifteen_sections_not_eight(text):
    """The number the outline is told, end to end through target_for and
    caps_for: 15 planned, room for 17, the explicit 3,000 words kept."""
    req = _req(text)
    requested = C.requested_sections(text)
    target = C.target_for(req)
    assert (target.words, target.explicit) == (3000, True)
    assert C.caps_for(T.EFFORT_BUDGETS["fast"], target, requested) == (17, 12)


def test_covering_is_a_heading_word_for_a_bulleted_list_too():
    """The ordinal-run fallback only reads NUMBERED items; the owner's word
    over a bulleted list needs the heading trigger itself."""
    text = "Write the platform report, about 3000 words covering:\n- Executive Summary\n- Security\n- Conclusion\nKeep it factual."
    assert C.requested_sections(text) == ["Executive Summary", "Security", "Conclusion"]


def test_a_bare_numbered_list_with_no_heading_word_is_a_table_of_contents():
    text = ("Write a technical report on our platform. 1. Executive Summary 2. Architecture "
            "3. Hardware Layer 4. Security 5. Conclusion")
    assert C.requested_sections(text) == [
        "Executive Summary", "Architecture", "Hardware Layer", "Security", "Conclusion"]
    assert C._ORDINAL_RUN_MIN == 3


def test_a_stray_ordinal_in_prose_is_not_a_table_of_contents():
    """Two items, or a run that does not start at one, name nothing."""
    assert C.requested_sections("See point 2. below and 3. as well, then write the report.") == []
    assert C.requested_sections("Write it up. 1. Scope 2. Risks") == []


@pytest.mark.parametrize("text,expected", [
    ("Sections:\n1. Q3 2026 Revenue\n2. Data Model\n3. Top 10 Accounts",
     ["Q3 2026 Revenue", "Data Model", "Top 10 Accounts"]),
    ("Requirements:\n1. How We Will Grow The Business Next Year\n2. Risks\n3. Roadmap",
     ["How We Will Grow The Business Next Year", "Risks", "Roadmap"]),
])
def test_a_heading_may_carry_a_digit_and_may_be_longer_than_five_words(text, expected):
    assert C.requested_sections(text) == expected
    assert C.MAX_SECTION_WORDS == 8


def test_a_count_in_front_of_a_file_part_is_still_not_a_section():
    """The digit rule was relaxed for headings, not for 'with 3 charts and 2
    tables': the word that makes a phrase a section has to be a word. On the
    ceilings branch the digit counted as content and this named two chapters."""
    assert C.requested_sections("Write a report on Q3 with 3 charts and 2 tables.") == []
    assert C.requested_sections("Make the doc with 2 tables, 3 charts and a summary section.") == []


def test_an_inline_clause_keeps_the_five_word_bound():
    """Eight words is for a MARKED item, which is a heading by construction.
    A clause after 'with' is prose, and widening it would read the second
    half of this sentence as a chapter."""
    assert C.INLINE_SECTION_WORDS == 5
    assert C.requested_sections(
        "A report with an executive summary and a detailed breakdown of every region we operate in.") == []


@pytest.mark.parametrize("text,last", [
    ("Requirements: 1. Executive Summary 2. Data Model 3. Conclusion Use professional Markdown.", "Conclusion"),
    ("1. Executive Summary 2. Data Model 3. Conclusion Use professional Markdown.", "Conclusion"),
    ("Requirements:\n1. Executive Summary\n2. Data Model\n3. Conclusion Use professional Markdown.", "Conclusion"),
    ("Requirements: 1. Executive Summary 2. Data Model 3. Threat Scope "
     "4. Conclusion and then some trailing prose about the file", "Conclusion"),
])
def test_the_last_item_of_a_list_that_ran_over_is_cut_back_to_its_heading(text, last):
    assert C.requested_sections(text)[-1] == last


@pytest.mark.parametrize("text,last", [
    ("Requirements:\n1. Executive Summary\n2. Data Model\n3. Acceptable Use Policy", "Acceptable Use Policy"),
    ("Requirements:\n1. Executive Summary\n2. Data Model\n3. How We Use Data", "How We Use Data"),
    ("Requirements:\n1. Intro\n2. Scope\n3. Security\n4. Output Format Standards", "Output Format Standards"),
    ("Requirements:\n1. Intro\n2. The Complete Regulatory Review Of Our Vendors",
     "Complete Regulatory Review Of Our Vendors"),
])
def test_a_last_heading_that_did_not_run_over_is_left_alone(text, last):
    """A newline or the end of the message already bounds it; the trim would
    only destroy it (measured on the ceilings branch before its own fix)."""
    assert C.requested_sections(text)[-1] == last


def test_more_than_twenty_named_sections_survive():
    names = [f"Chapter {w}" for w in (
        "Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot", "Golf", "Hotel", "India",
        "Juliett", "Kilo", "Lima", "Mike", "November", "Oscar", "Papa", "Quebec", "Romeo",
        "Sierra", "Tango", "Uniform", "Victor", "Whiskey", "Xray", "Yankee")]
    text = "Requirements:\n" + "\n".join(f"{i}. {n}" for i, n in enumerate(names, 1))
    assert len(C.requested_sections(text)) == 25
    assert C.MAX_REQUESTED_SECTIONS == 40


# --------------------------------------------------------------- the split --


def test_the_outline_schema_asks_how_long_each_section_needs_to_be():
    item = C._OUTLINE_SCHEMA["properties"]["sections"]["items"]
    assert "words" in item["required"]
    assert item["properties"]["words"] == {"type": "integer", "minimum": 80, "maximum": 6000}
    # The widened schema carries it too.
    assert "words" in C._outline_schema(30)["properties"]["sections"]["items"]["required"]


def test_the_plan_decides_the_split_and_the_request_decides_the_total():
    items = [{"heading": "Summary", "words": 300}, {"heading": "Detail", "words": 900},
             {"heading": "Close", "words": 300}]
    got = C._section_targets(items, 3000)
    assert got == [600, 1800, 600]
    assert sum(got) == 3000


def test_a_plan_with_a_figure_missing_falls_back_to_the_even_split():
    items = [{"heading": "A", "words": 900}, {"heading": "B"}, {"heading": "C", "words": 300}]
    assert C._section_targets(items, 3000) == [1000, 1000, 1000]
    assert C._section_targets([{"heading": "A"}], 0) == [120]


def test_a_mad_figure_is_bounded_before_it_is_scaled():
    items = [{"heading": "A", "words": 10 ** 9}, {"heading": "B", "words": 1}]
    assert C._planned_words(items[0]) == 6000 and C._planned_words(items[1]) == 80
    got = C._section_targets(items, 3000)
    # The 120-word floor binds on the tiny one and lifts the sum above the
    # total: a section is never asked for less than one real paragraph.
    assert got[1] == 120 and got[0] == 2961 and sum(got) >= 3000


class _Recorder:
    """A scripted `llm.json_completion` whose plan puts THREE TIMES the words
    on the first section, so the split is observable in the writer's asks."""

    def __init__(self, sections, *, weights=None):
        self.sections = sections
        self.weights = weights or {}
        self.calls = []

    async def __call__(self, messages, *, json_schema=None, schema_name="", temperature=0.0,
                       max_tokens=None, thinking=False, effort=None):
        system = messages[0]["content"]
        user = "\n\n".join(m["content"] for m in messages[1:])
        self.calls.append({"name": schema_name, "system": system, "user": user, "max_tokens": max_tokens})
        if schema_name == "artifact_outline":
            return json.dumps({
                "title": "Platform", "audience": "engineering", "purpose": "the platform",
                "sections": [{"heading": h, "purpose": f"what {h} covers", "elements": ["paragraphs"],
                              "words": self.weights.get(h, 200)} for h in self.sections],
                "needs_current_facts": False, "assumptions": []})
        if schema_name == "artifact_section_write":
            heading = next((h for h in self.sections if f"“{h}”" in user), self.sections[0])
            return json.dumps({"blocks": [
                {"type": "heading", "level": 1, "text": heading},
                {"type": "paragraph", "text": " ".join([heading.split()[0].lower()] * 200)}]})
        return json.dumps({"ok": True, "issues": []})

    def asks(self):
        out = []
        for c in self.calls:
            if c["name"] != "artifact_section_write":
                continue
            line = next(l for l in c["user"].splitlines() if "Write about" in l and "words in this section" in l)
            out.append(int(line.split("Write about ")[1].split(" words")[0].replace(",", "")))
        return out


def test_the_owners_prompt_is_written_in_fifteen_sections_through_the_real_compose(monkeypatch):
    """compose() end to end with the model scripted: one outline told to plan
    15, fifteen section writes, a card that counts fifteen, and the word
    asks summing to the 3,000 the request named."""
    model = _Recorder(FIFTEEN_SECTIONS, weights={"Executive Summary": 600})
    monkeypatch.setattr(llm, "json_completion", model)
    monkeypatch.setattr(C, "_pace", _no_pace)
    result = asyncio.run(C.compose(_req(OWNER_ONE_LINE)))
    names = [c["name"] for c in model.calls]
    assert names.count("artifact_outline") == 1
    assert names.count("artifact_section_write") == 15
    outline = next(c for c in model.calls if c["name"] == "artifact_outline")
    plan_line = next(l.strip() for l in outline["system"].splitlines() if l.strip().startswith("Plan "))
    assert plan_line.startswith("Plan 15 sections (at most 17)"), plan_line
    assert "put each section's own figure in its `words`" in plan_line
    headings = [b.text for b in result.spec.body.blocks if isinstance(b, S.Heading) and b.level == 1]
    assert headings == FIFTEEN_SECTIONS
    asks = model.asks()
    assert len(asks) == 15 and sum(asks) == 3000, asks
    # 600 against fourteen 200s: the summary is asked for 3x a plain section,
    # within the one-word rounding that keeps the sum exact.
    assert abs(asks[0] - 3 * asks[1]) <= 3 and max(asks[1:]) - min(asks[1:]) <= 1, asks
    note = next(w for w in result.warnings if w.startswith(C.LONG_DOCUMENT_NOTE))
    assert "written in 15 sections" in note, note


async def _no_pace():
    return 0.0
