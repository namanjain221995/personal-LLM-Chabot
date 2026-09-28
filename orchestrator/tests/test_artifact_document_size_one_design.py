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


# ----------------------------------------------------- what a name IS --
#
# A section name is a NOUN PHRASE NAMING A PART OF THE DOCUMENT. The parse
# above was relaxed (a digit allowed, eight words, a bare list, a run-over
# trim) for the owner's fifteen, and each relaxation found things that are
# not sections. Two reached a real file on the live engine (2026-09-28,
# ea895477): "7. About 3000 words" became a level-1 heading whose paragraph
# read "This section serves as a structural placeholder required by the
# prompt", and "8. Use professional Markdown" a heading with a paragraph
# about Markdown under it.

_REQ = "Write a security report.\nRequirements:\n1. Executive Summary\n2. Data Model\n3. Threat Scope\n4. Conclusion\n5. "
_FOUR = ["Executive Summary", "Data Model", "Threat Scope", "Conclusion"]


@pytest.mark.parametrize("item", [
    "About 3000 words", "At least 3 diagrams", "At least three diagrams", "Around 4 pages long", "Minimum 10",
    "3 charts per", "5 tables", "Q3 2026 numbers only", "No more than 2 pages",
])
def test_a_quantity_or_a_constraint_in_a_numbered_list_is_not_a_section(item):
    """Decided from what the item IS, not from whether it carries a digit:
    once the numbers, quantifiers and units are set aside nothing is left
    that names a part of the document."""
    assert C.requested_sections(_REQ + item) == _FOUR
    assert not C._naming_words(item) or C._naming_words(item) <= {C._stem(w) for w in C._NOT_SECTION_WORDS}


@pytest.mark.parametrize("item", [
    "Q3 2026 Revenue", "Top 10 Accounts", "2026 Roadmap", "10-year plan", "2024 vs 2025 comparison",
    "Minimum Viable Product", "Long Term Vision", "3D Modelling", "Single Sign-On",
])
def test_a_name_that_carries_a_number_or_a_quantifier_word_is_still_a_section(item):
    assert C.requested_sections(_REQ + item) == _FOUR + [item]


@pytest.mark.parametrize("item", [
    "Use professional Markdown", "Include at least three diagrams", "Do not skip any section",
    "Keep the tone formal", "Cite every figure you use", "Provide a glossary",
])
def test_a_numbered_instruction_is_not_a_section(item):
    """An imperative — instruction verb, then a lowercase word — is a
    sentence about the file, not the name of a part of it."""
    assert C.requested_sections(_REQ + item) == _FOUR


def test_a_list_that_is_only_instructions_names_nothing():
    text = ("Write a report, about 3000 words.\nRequirements:\n1. Use professional markdown\n"
            "2. Include at least three diagrams\n3. Do not skip any\n4. Cite every figure you use\n5. Keep the tone formal")
    assert C.requested_sections(text) == []


def test_the_live_case_b_shape_names_six_sections_not_eight():
    """The shape driven live on ea895477: six names, one quantity, one
    instruction. The file carried eight level-1 headings."""
    text = ("Write a security report.\nRequirements:\n1. Executive Summary\n2. Data Model\n3. Threat Scope\n"
            "4. Access Control\n5. Monitoring\n6. Conclusion\n7. About 3000 words\n8. Use professional Markdown")
    assert C.requested_sections(text) == [
        "Executive Summary", "Data Model", "Threat Scope", "Access Control", "Monitoring", "Conclusion"]


@pytest.mark.parametrize("item", [
    "Use Cases", "Use of Funds", "Make or Buy Decision", "Do's and Don'ts", "Keep Warm Strategy",
    "Add-on Services",
])
def test_a_heading_that_starts_with_an_instruction_verb_is_a_heading(item):
    """The verb alone is not evidence; the Title Case word after it is."""
    assert C.requested_sections(_REQ + item) == _FOUR + [item]


@pytest.mark.parametrize("item,name", [
    ("2-page summary", "summary"),
    ("500-word risk register", "risk register"),
    ("Risk Register (about 200 words)", "Risk Register"),
    ("Risk Register - 200 words", "Risk Register"),
    ("Risks [2 pages]", "Risks"),
])
def test_a_size_beside_a_name_is_not_part_of_the_name(item, name):
    """Kept whole, the size tokens stayed in the phrase and no heading the
    model wrote could reach the 60% overlap, so the repair pass wrote a
    section CALLED "2-page summary"."""
    assert C.requested_sections(_REQ + item) == _FOUR + [name]
    assert C.requested_sections("Write a document with a 2-page summary and 3-year forecast.") == [
        "summary", "3-year forecast"]


# ------------------------------------------------- the pasted document --

_TOC = "\n".join(f"{i}. {n}" for i, n in enumerate([
    "Introduction", "Scope and Definitions", "Methodology", "Results", "Discussion", "Limitations",
    "Conclusion", "Appendix A"], 1))
_BODY = "\n".join(["This document describes the vendor assessment programme carried out in the second quarter."] * 4)


@pytest.mark.parametrize("text", [
    "Summarise this in one page:\n" + _TOC,
    "Turn the document below into a two-page brief.\n\n" + _TOC + "\n" + _BODY,
    "Write a report from the attached notes. 1. Findings 2. Risks 3. Roadmap",
], ids=["151-char paste", "535-char ask, blank line, TOC + body", "bare list after 'attached'"])
def test_an_unfenced_pasted_table_of_contents_is_not_the_requests_sections(text):
    """The fence heuristic does not wrap either paste (pinned: this is the
    hole), so `_own_words` cannot draw the line; the bare-list fallback
    draws it from the words before the list, which point at material. On
    ea895477 the brief was told to carry the document's eight sections,
    spent a correction call and warned 'requested sections not found'."""
    from app.core import pasted
    fenced = pasted.fenced(text)
    assert pasted.OPEN_TAG not in fenced
    assert C.requested_sections(fenced) == []


def test_a_bare_list_under_a_plain_request_and_a_heading_word_over_pasted_material_still_read():
    """The guard is only for the BARE list: a heading word is the person's
    own statement that what follows is their list."""
    assert C.requested_sections(
        "Create a technical report on our platform, about 3000 words.\n1. Executive Summary\n2. Architecture\n"
        "3. Hardware Layer\n4. Security\n5. Conclusion\nUse professional Markdown.") == [
        "Executive Summary", "Architecture", "Hardware Layer", "Security", "Conclusion"]
    assert C.requested_sections(
        "Write a report from the attached notes covering: 1. Findings 2. Risks 3. Roadmap") == [
        "Findings", "Risks", "Roadmap"]


# ------------------------------------------------- the neighbouring item --


@pytest.mark.parametrize("text", [
    "Requirements: 1. Executive Summary 2. Architecture including hardware, software and network 3. Security 4. Conclusion",
    "Requirements: 1. Executive Summary 2. Architecture with hardware, software and network 3. Security 4. Conclusion",
])
def test_an_inline_clause_stops_at_the_next_items_ordinal_and_names_only_the_item(text):
    """On ea895477 this named 'network 3' (never coverable: 0.5 overlap, so
    the repair and the Max-loop contract counted it missing forever) and the
    seven-word run 'Architecture including hardware, software and network'."""
    got = C.requested_sections(text)
    assert got == ["Executive Summary", "Architecture", "Security", "Conclusion", "hardware", "software", "network"]
    assert not any(any(ch.isdigit() for ch in name) for name in got)


def test_with_cuts_an_item_only_when_a_list_follows_it():
    assert C.requested_sections("Requirements:\n1. Comparison with Competitors\n2. Pricing\n3. Roadmap") == [
        "Comparison with Competitors", "Pricing", "Roadmap"]


# --------------------------------------------- the run-over trim, bounded --


@pytest.mark.parametrize("text,last", [
    ("Requirements: 1. Executive Summary 2. Data Model 3. Threat Scope 4. Acceptable Use Policy", "Acceptable Use Policy"),
    ("Requirements: 1. Executive Summary 2. Data Model 3. How We Use Data", "How We Use Data"),
    ("Requirements: 1. Collection 2. Storage 3. Data Use", "Data Use"),
    ("Requirements: 1. Scope 2. Design 3. Security 4. Use Cases", "Use Cases"),
    ("requirements: 1. executive summary 2. data model 3. acceptable use policy", "acceptable use policy"),
])
def test_a_one_line_heading_with_a_verb_in_it_is_not_cut(text, last):
    """Main kept every one of these; ea895477 cut them to 'Acceptable',
    'How We' and dropped 'Data Use'. The verb is followed by a Title Case
    word (or nothing), or the list is not Title Case at all."""
    assert C.requested_sections(text)[-1] == last


@pytest.mark.parametrize("text,last", [
    ("Requirements:\n1. Executive Summary.\n2. Data Model.\n3. Threat Scope.\n4. Incident Response Plan Overview.",
     "Incident Response Plan Overview"),
    ("Requirements:\n1. Executive Summary\n2. Data Model\n3. Threat Scope\n4. Vendor Risk Assessment Framework: cover all vendors.",
     "Vendor Risk Assessment Framework"),
])
def test_a_multi_line_list_never_cuts_its_last_heading_by_sibling_length(text, last):
    """Each line bounds its own item; the sibling rule is for a one-line
    list only. ea895477 gave 'Incident Response' and 'Vendor Risk'."""
    got = C.requested_sections(text)
    assert got[3] == last


def test_the_run_over_cut_needs_a_lowercase_word_after_the_verb():
    """'Conclusion Use professional Markdown' is cut; the same list with the
    tail in Title Case is a heading the parser cannot tell from one, and is
    left to the sibling rule."""
    one_line = "Requirements: 1. Executive Summary 2. Data Model 3. Threat Scope 4. Conclusion Use professional Markdown."
    assert C.requested_sections(one_line)[-1] == "Conclusion"
    multi = "Requirements:\n1. Executive Summary\n2. Data Model\n3. Conclusion Keep it factual."
    assert C.requested_sections(multi)[-1] == "Conclusion"


def test_the_live_case_b_shape_is_written_in_six_sections_through_the_real_compose(monkeypatch):
    """compose() end to end with the model scripted: the outline is told the
    six names, six section writes, and neither the quantity nor the
    instruction is a heading in the file."""
    names = ["Executive Summary", "Data Model", "Threat Scope", "Access Control", "Monitoring", "Conclusion"]
    text = ("Write a security report, about 3000 words.\nRequirements:\n" +
            "\n".join(f"{i}. {n}" for i, n in enumerate(names, 1)) +
            "\n7. About 3000 words\n8. Use professional Markdown")
    model = _Recorder(names)
    monkeypatch.setattr(llm, "json_completion", model)
    monkeypatch.setattr(C, "_pace", _no_pace)
    result = asyncio.run(C.compose(_req(text)))
    outline = next(c for c in model.calls if c["name"] == "artifact_outline")
    told = outline["user"].split("The request names these ", 1)[1].split(".", 1)[0]
    assert told.startswith("6 sections"), told
    assert "About 3000 words" not in told and "Use professional Markdown" not in told, told
    # The sections the writer was asked for are the six names and nothing
    # else (the extension pass re-asks for short ones, so the set, not the
    # count: the recorder's 200-word sections are short against 3,000).
    asked = {c["user"].split("“", 1)[1].split("”", 1)[0]
             for c in model.calls if c["name"] == "artifact_section_write"}
    assert asked == set(names), asked
    headings = [b.text for b in result.spec.body.blocks if isinstance(b, S.Heading) and b.level == 1]
    assert headings == names
    assert not any("not found" in w for w in result.warnings), result.warnings


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


# ---- the four over-reaches the narrower rules brought, 2026-09-28 ---------
#
# Every rule in this file was measured against ea895477 and main before it
# landed, and four of them then took real section names away that BOTH of those
# trees kept. Each case below was driven through `requested_sections` on all
# three trees; the comment names which tree kept what.

SENTENCE_CASE_LIST = (
    "Write a spec. Requirements: 1. Use cases 2. Keep warm strategy "
    "3. Provide feedback 4. Give back program 5. Write once run anywhere "
    "6. Follow up actions 7. Add to cart flow"
)


def test_a_sentence_case_list_is_still_a_list_of_names():
    """`_IMPERATIVE_ITEM_RE` reads "verb then lowercase word" as an
    instruction, which is too wide on its own: a person who writes their list
    in sentence case still writes names. All seven vanished on cb3753f5;
    ea895477 and main keep every one.

    "Follow up actions" and "Add to cart flow" need the particle rule as well
    -- "up" and "to" are stop words, so the word after the particle decides."""
    assert C.requested_sections(SENTENCE_CASE_LIST) == [
        "Use cases", "Keep warm strategy", "Provide feedback", "Give back program",
        "Write once run anywhere", "Follow up actions", "Add to cart flow",
    ]


@pytest.mark.parametrize("item", [
    "Use professional Markdown",
    "Include at least three diagrams",
    "Do not skip any section",
    "Keep the tone formal",
    "Write about 3000 words",
])
def test_an_instruction_is_still_not_a_section(item):
    """The other side of the same rule. The word after the verb is a stop word,
    a quantifier or a unit, so nothing is being named."""
    req = ("Write a report. 1. Executive Summary 2. Architecture 3. Security "
           "4. Conclusion 5. " + item)
    assert C.requested_sections(req) == ["Executive Summary", "Architecture", "Security", "Conclusion"]


@pytest.mark.parametrize("prompt,want", [
    # A unit word is a section name when nothing is being measured.
    ("Write an outline. 1. Premise 2. Characters 3. Setting 4. Plot",
     ["Premise", "Characters", "Setting", "Plot"]),
    ("Write the agenda. 1. Attendees 2. Minutes 3. Action Items",
     ["Attendees", "Minutes", "Action Items"]),
    ("Write the itinerary. 1. Day 1 2. Day 2 3. Day 3",
     ["Day 1", "Day 2", "Day 3"]),
    ("Write the plan. 1. Q1 2. Q2 3. Q3 4. Q4", ["Q1", "Q2", "Q3", "Q4"]),
    ("Write the review. 1. H1 2026 2. H2 2026 3. Outlook",
     ["H1 2026", "H2 2026", "Outlook"]),
])
def test_a_bare_unit_or_a_period_names_a_section(prompt, want):
    """"Characters" in a novel outline, "Minutes" in an agenda, "Day 1" in an
    itinerary and "Q1" in a plan are names, not measurements. The flat measure
    set dropped all of them on cb3753f5 while ea895477 and main kept them; a
    unit is a measurement only when a quantity comes BEFORE it."""
    assert C.requested_sections(prompt) == want


@pytest.mark.parametrize("item", [
    "About 3000 words", "At least 3 diagrams", "Around 4 pages long",
    "3 charts per section", "Q3 2026 numbers only",
])
def test_a_quantity_is_still_not_a_section_after_the_unit_rule(item):
    """And the quantities the unit rule must not let back in. "Q3 2026 numbers
    only" is the one that needs the pair of rules together: a period names a
    section on its own, but beside a unit it IS the quantity."""
    req = ("Write a report. 1. Executive Summary 2. Architecture 3. Security "
           "4. Conclusion 5. " + item)
    assert C.requested_sections(req) == ["Executive Summary", "Architecture", "Security", "Conclusion"]


OWN_LIST_SHAPES = [
    "Write a report with the structure below. 1. Executive Summary 2. Architecture 3. Security 4. Conclusion",
    "Write the document for our client. 1. Executive Summary 2. Architecture 3. Security 4. Conclusion",
    "Start with a summary of the findings. 1. Executive Summary 2. Architecture 3. Security 4. Conclusion",
    "Write up the notes from today's call as a report: 1. Executive Summary 2. Architecture 3. Security 4. Conclusion",
]


@pytest.mark.parametrize("prompt", OWN_LIST_SHAPES)
def test_the_persons_own_bare_list_is_read_even_when_the_words_before_it_sound_like_material(prompt):
    """`_MATERIAL_AHEAD_RE` matched a bare "below", "the document", "the
    notes", "summary of" and "the paper", so five requests that carry the
    person's OWN table of contents read ZERO sections on cb3753f5 -- and with
    them went the size, the names in the prompt, the coverage check and the
    repair pass. ea895477 read every one.

    A pointer at material needs the material NOUN and a pointer WORD beside
    it: "the document below", "the text above", "attached notes"."""
    assert C.requested_sections(prompt) == ["Executive Summary", "Architecture", "Security", "Conclusion"]


def test_the_persons_own_bare_list_after_a_topic_sentence():
    assert C.requested_sections(
        "Write the paper on the topic. 1. Abstract 2. Introduction 3. Method 4. Results"
    ) == ["Abstract", "Introduction", "Method", "Results"]


@pytest.mark.parametrize("prompt", [
    "Summarise this. 1. Background 2. Findings 3. Recommendations 4. Appendix",
    "Turn it into a brief. 1. Background 2. Findings 3. Recommendations 4. Appendix",
    "Rewrite the document below. 1. Background 2. Findings 3. Recommendations 4. Appendix",
    "A report from the attached notes. 1. Background 2. Findings 3. Recommendations 4. Appendix",
])
def test_a_pasted_documents_own_contents_are_still_not_the_request(prompt):
    """The rule the narrowing must not lose: when the words before a bare list
    point at MATERIAL, the list is the material's table of contents."""
    assert C.requested_sections(prompt) == []
