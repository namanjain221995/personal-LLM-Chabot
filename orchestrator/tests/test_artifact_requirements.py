"""artifacts/requirements.py: the checklist.

The recall/precision gates run against tests/fixtures/selfcheck/
requests_labelled.py (40 requests, 154 hand-written items, frozen before the
extractor existed — see its PROVENANCE note). The model proposer is a stub
here; the live ablation lives in scripts/as3_selfcheck_score.py.
"""
from __future__ import annotations

import asyncio

import pytest

from app.artifacts import requirements as RQ
from tests.fixtures.selfcheck import requests_labelled as L

SOURCE_MD = "# Audit\n\n## Findings\n\n| Area | Status |\n|---|---|\n| Keys | Open |\n"
PRECISION_CATEGORIES = {"style", "format", "layout", "chart"}


def _key(category, target, prop, expected):
    return (category, target, prop, RQ._norm_expected(expected))


def _build(r, **kw):
    return asyncio.run(RQ.build(None, instruction=r["instruction"], kind=r["kind"], formats=[], operation=r["operation"],
                                source_markdown=SOURCE_MD if r.get("previous_answer") else "", effort=kw.pop("effort", "fast"), **kw))


def _score(builder):
    hit = total = extra_ok = extra_all = 0
    per_language_miss = []
    for r in L.REQUESTS:
        cl = builder(r)
        got = {_key(i.category, i.target, i.property, i.expected) for i in cl.items}
        exp = {_key(*e[:4]) for e in r["expected"]}
        total += len(exp)
        hit += len(exp & got)
        for g in got:
            if g[0] in PRECISION_CATEGORIES:
                extra_all += 1
                extra_ok += g in exp
        if exp - got:
            per_language_miss.append((r["id"], sorted(exp - got)))
    return hit / total, extra_ok / max(1, extra_all), per_language_miss


def test_the_labelled_set_is_large_enough():
    assert len(L.REQUESTS) == 40
    assert L.TOTAL_EXPECTED_ITEMS >= 150


def test_rule_only_recall_and_precision_on_the_labelled_set():
    recall, precision, misses = _score(_build)
    assert recall >= 0.90, misses
    assert precision >= 0.90


def test_fast_never_calls_the_model_and_think_calls_it_once():
    calls = []

    async def proposer(instruction, summary, kind):
        calls.append(instruction)
        return []

    fast = asyncio.run(RQ.build(None, instruction="make the headings dark blue", kind="document", formats=["docx"], operation="edit", effort="fast", proposer=proposer))
    assert calls == [] and fast.model_calls == 0 and fast.model_skipped == "fast"
    think = asyncio.run(RQ.build(None, instruction="make the headings dark blue", kind="document", formats=["docx"], operation="edit", effort="think", proposer=proposer))
    assert len(calls) == 1 and think.model_calls == 1


def test_the_proposer_is_skipped_when_chat_is_busy_and_on_timeout():
    async def slow(instruction, summary, kind):
        await asyncio.sleep(5)
        return []

    busy = asyncio.run(RQ.build(None, instruction="pdf with red title", kind="document", formats=["pdf"], operation="create", effort="max", proposer=slow, busy=lambda: True))
    assert busy.model_calls == 0 and busy.model_skipped == "busy"
    timed = asyncio.run(RQ.build(None, instruction="pdf with red title", kind="document", formats=["pdf"], operation="create", effort="max", proposer=slow, timeout_s=0.05))
    assert timed.model_skipped == "timeout"
    assert any(i.target == "title" and i.property == "color" for i in timed.items), "the rule items stand alone"


def test_the_proposer_never_sees_the_spec_or_the_rule_items():
    seen = {}

    async def proposer(instruction, summary, kind):
        seen.update(instruction=instruction, summary=summary, kind=kind)
        return []

    asyncio.run(RQ.build(None, instruction="make the headings dark blue", kind="document", formats=["docx"], operation="edit",
                         parent_spec=object(), source_summary="x" * 5000, effort="think", proposer=proposer))
    assert set(seen) == {"instruction", "summary", "kind"}
    assert len(seen["summary"]) <= 1500


@pytest.mark.parametrize("case", L.MISPARSE_CASES, ids=[c["id"] for c in L.MISPARSE_CASES])
def test_a_rule_and_model_disagreement_on_the_element_is_contested(case, monkeypatch):
    """The seeded misparse: the rule reader is forced to read the misread
    element; an independent proposer reads the asked one. The item must be
    contested (never a must, never repaired)."""
    target, prop, expected = case["misread"]
    asked_target = case["asked"][0]

    def misreading(instruction, *, kind, operation, has_source_answer=False):
        return [RQ.ChecklistItem("", "style", target, prop, expected, must=True, phrase=instruction)]

    monkeypatch.setattr(RQ, "extract_rules", misreading)
    # The seeded premise is ONE rule reader that misreads. With the styling
    # track merged, its parser is a second rule reader that reads the asked
    # element; it is held out here so the rule/model disagreement is tested.
    monkeypatch.setattr(RQ, "_extra_from_style_module", lambda instruction, kind: [])

    async def proposer(instruction, summary, kind):
        value = expected if not isinstance(expected, str) or expected.startswith("#") else expected
        return [{"category": "style", "target": asked_target, "property": prop, "expected": str(value).lower() if isinstance(value, bool) else str(value)}]

    cl = asyncio.run(RQ.build(None, instruction=case["instruction"], kind="document", formats=["docx"], operation="edit", effort="think", proposer=proposer))
    style = [i for i in cl.items if i.category == "style"]
    assert style and all(i.contested and not i.must for i in style), [(i.target, i.contested, i.must) for i in style]


def test_agreement_makes_a_must_and_model_only_items_are_shoulds():
    rule = [RQ.ChecklistItem("", "style", "heading", "color", "#1F3864", must=False)]
    model = [RQ.ChecklistItem("", "style", "heading", "color", "#1F3864", source="model", must=False),
             RQ.ChecklistItem("", "layout", "page", "page_numbers", True, source="model", must=False)]
    merged = RQ.merge(rule, model)
    heading = next(i for i in merged if i.target == "heading")
    assert heading.must and heading.source == "both" and not heading.contested
    numbers = next(i for i in merged if i.property == "page_numbers")
    assert not numbers.must and numbers.source == "model"


def test_style_words_are_never_sections_and_source_mentions_are_not_formats():
    cl = asyncio.run(RQ.build(None, instruction="Make a Word report on onboarding with dark blue headings and Georgia body font, landscape",
                              kind="document", formats=["docx"], operation="create"))
    assert not [i for i in cl.items if i.category == "content"], "the P0 bug: 'dark blue headings' is not a chapter"
    cl2 = asyncio.run(RQ.build(None, instruction="the pdf about the red team was good, make a docx of the findings", kind="document", formats=["docx"], operation="create"))
    assert [i.expected for i in cl2.items if i.category == "format"] == ["docx"]


def test_named_colours_carry_their_name_and_shade_and_hex_is_exact():
    items = RQ.extract_rules("headings dark blue and the title #0A1D37", kind="document", operation="edit")
    heading = next(i for i in items if i.target == "heading")
    title = next(i for i in items if i.target == "title")
    assert heading.locator == {"color_name": "dark blue", "shade": "dark"}
    assert title.expected == "#0A1D37" and "color_name" not in title.locator


def test_export_faithfulness_needs_a_source_and_classy_words_make_house_style_musts():
    prod = "just give it in docs in a standard and classy format, provide a dox file"
    with_source = asyncio.run(RQ.build(None, instruction=prod, kind="document", formats=["docx"], operation="create", source_markdown=SOURCE_MD))
    faith = [i for i in with_source.items if i.category == "faithfulness"]
    assert {i.property for i in faith} == {"headings_covered", "table_cells_covered"} and all(i.must for i in faith)
    assert with_source.source_structure == {"headings": ["Audit", "Findings"], "cells": ["Area", "Status", "Keys", "Open"]}
    house = {i.property: i.must for i in with_source.items if i.category == "house_style"}
    assert house["title_block"] and house["fill_present"] and house["page_numbers"]
    without = asyncio.run(RQ.build(None, instruction=prod, kind="document", formats=["docx"], operation="create"))
    assert not [i for i in without.items if i.category == "faithfulness"]
    plain = asyncio.run(RQ.build(None, instruction="make a docx about onboarding", kind="document", formats=["docx"], operation="create"))
    assert not any(i.must for i in plain.items if i.category == "house_style")


def test_the_checklist_is_capped_and_the_category_enum_is_closed():
    long = ", ".join(f"row {n} yellow background" for n in range(1, 80))
    cl = asyncio.run(RQ.build(None, instruction=f"excel sheet, {long}", kind="workbook", formats=["xlsx"], operation="create"))
    assert len(cl.items) <= RQ.MAX_ITEMS
    assert {i.category for i in cl.items} <= set(RQ.CATEGORIES)
    assert RQ._normalize_model_item({"category": "vibes", "target": "x", "property": "y", "expected": "z"}) is None


def test_edits_always_carry_a_preservation_must():
    cl = asyncio.run(RQ.build(None, instruction="change the title", kind="document", formats=["docx"], operation="edit"))
    assert any(i.category == "preservation" and i.must for i in cl.items)


# ------------------------------------------- verifier cases (2026-09-15) --


def _rule(instruction, kind="document", operation="create", source=False):
    return [(i.category, i.target, i.property, i.expected, i.must)
            for i in RQ.extract_rules(instruction, kind=kind, operation=operation, has_source_answer=source)
            if i.category not in ("security", "house_style")]


def test_a_fruit_is_not_a_series_colour_but_coloured_bars_are():
    assert not any(c[2] == "series_color" for c in _rule("Make a bar chart of apple vs orange sales", "workbook"))
    got = _rule("bar chart with blue bars titled Regional Sales", "presentation")
    assert ("chart", "chart", "series_color", "#2F6FB2", True) in got
    assert not any(c[0] == "style" and c[1] == "title" for c in got), "'titled' names no title element"
    assert {c[3] for c in _rule("pie chart, slices in red and green", "workbook") if c[2] == "series_color"} == {"#C62828", "#3F8F4F"}


def test_line_chart_words():
    assert ("chart", "chart", "type", "line", True) in _rule("plot sales by month as a line in excel", "workbook")
    assert not any(c[2] == "type" for c in _rule("a word document explaining what a line of credit is, with a chart of rates"))
    assert [c[3] for c in _rule("scatter plot with trend line", "workbook") if c[2] == "type"] == ["scatter"]


def test_a_requested_column_name_is_read_whole():
    assert ("data", "column:joining date", "present", True, True) in _rule("employee list with a column for joining date", "workbook")
    assert ("data", "column:status", "present", True, True) in _rule("add a column for status in the sheet", "workbook")


@pytest.mark.parametrize("instruction,kind", [
    ("give me a one page summary of this as pdf", "document"),
    ("turn the above into a short brief in word", "document"),
    ("isko saransh me pdf bana do", "document"),
    ("turn the above into a deck", "presentation"),
])
def test_a_condensed_export_does_not_require_every_cell(instruction, kind):
    items = [i for i in RQ.extract_rules(instruction, kind=kind, operation="create", has_source_answer=True) if i.category == "faithfulness"]
    assert items and not any(i.must for i in items)


def test_a_whole_export_still_requires_every_cell():
    items = [i for i in RQ.extract_rules("just give it in docs, provide a dox file", kind="document", operation="create", has_source_answer=True)
             if i.category == "faithfulness"]
    assert len(items) == 2 and all(i.must for i in items)
