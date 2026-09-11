"""The artifact contracts: spec validation, format policy, stage vocabulary.

Offline. These are the rules the renderers, the job runner and the chat
engine all build against, so a change here is a change of contract.
"""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.artifacts import formats, spec as S, types as T


# ------------------------------------------------------------ vocabulary --


def test_stage_ids_are_fixed_and_titled():
    assert T.STAGES == ("intent", "gather", "outline", "compose", "render", "validate", "preview")
    assert [T.STEP_IDS[s] for s in T.STAGES] == list(range(1, 8))
    assert set(T.STAGE_TITLES) == set(T.STAGES)


def test_every_kind_maps_to_real_formats_only():
    for kind, fmts in T.FORMATS_FOR_KIND.items():
        assert kind in T.KINDS
        for f in fmts:
            assert f in T.FORMATS and f in T.MIME_TYPES


def test_effort_decides_depth_never_availability():
    for level in ("fast", "think", "max"):
        assert level in T.EFFORT_BUDGETS
    assert T.EFFORT_BUDGETS["fast"].research is False and T.EFFORT_BUDGETS["fast"].visual_qa is False
    assert T.EFFORT_BUDGETS["max"].visual_qa is True
    assert T.EFFORT_BUDGETS["fast"].max_corrections == 1
    # The product wording for Max never claims the model is AGI.
    assert "AGI" not in T.MAX_EFFORT_DESCRIPTION
    assert "orchestrated" in T.MAX_EFFORT_DESCRIPTION


def test_storage_paths_are_id_keyed_and_owner_scoped():
    aid = "a" * 32
    assert T.artifact_dir("/reports", 7, aid) == f"/reports/artifacts/7/{aid}"
    assert T.version_dir("/reports/", 7, aid, 2) == f"/reports/artifacts/7/{aid}/v2"
    for bad in ("../x", "A" * 32, "a" * 31, "", "a" * 32 + "/"):
        with pytest.raises(ValueError):
            T.artifact_dir("/reports", 7, bad)


def test_download_names_are_safe_and_versioned():
    assert T.download_name("Q3 Review: Sales & Pipeline!", 2, "pptx") == "q3-review-sales-pipeline-v2.pptx"
    assert T.download_name("../../etc/passwd", 1, "pdf") == "etc-passwd-v1.pdf"
    assert T.download_name("", 1, "pdf") == "document-v1.pdf"
    assert len(T.slug_for("x" * 500)) <= 60


def test_artifact_ref_builds_relative_urls_by_code():
    ref = T.ArtifactRef(
        artifact_id="b" * 32, version=1, job_id="c" * 32, title="Brief", kind="document",
        status="completed", files=[T.FileRef("pdf", "brief-v1.pdf", T.MIME_TYPES["pdf"], 1234, "sha", pages=2)],
        preview_kind="pages", preview_pages=2,
    )
    j = ref.to_json()
    assert j["files"][0]["download_url"] == f"/artifacts/{'b' * 32}/v/1/file/pdf?disposition=attachment"
    assert j["preview_url"] == f"/artifacts/{'b' * 32}/v/1/preview"
    assert j["thumbnail_url"].endswith("/preview/1.png?w=240")
    assert j["status_url"] == f"/artifacts/jobs/{'c' * 32}"
    assert "http" not in json.dumps(j), "no host ever appears in a reference"


# ------------------------------------------------------------------ spec --


def _doc(**over):
    base = {
        "title": "Quarterly Review",
        "template_id": "executive_report",
        "blocks": [
            {"type": "heading", "level": 1, "text": "Summary"},
            {"type": "paragraph", "text": "Revenue grew.", "sources": ["s1"]},
            {"type": "bullets", "items": ["One", "Two"]},
            {"type": "table", "table": {"columns": ["Region", "Revenue"], "rows": [["EMEA", 12.5], ["APAC", 9]], "numeric_columns": [1]}},
            {"type": "chart", "chart": {"type": "bar", "categories": ["EMEA", "APAC"], "series": [{"name": "Revenue", "values": [12.5, 9]}]}},
            {"type": "callout", "kind": "note", "text": "Figures are unaudited."},
            {"type": "kpis", "items": [{"label": "ARR", "value": "$4.2M"}]},
            {"type": "page_break"},
        ],
        "sources": [{"id": "s1", "title": "Finance export", "url": "https://example.com/x"}],
    }
    base.update(over)
    return base


def test_a_document_round_trips_and_the_schema_is_per_kind():
    spec = S.parse_body("document", _doc())
    assert spec.kind == "document" and spec.title == "Quarterly Review"
    assert len(spec.body.blocks) == 8
    schema = S.schema_for("document")
    assert schema["title"] == "DocumentSpec" and "blocks" in schema["properties"]
    # The envelope is stored, and loads back.
    again = S.load(json.loads(spec.model_dump_json()))
    assert again == spec


def test_unknown_block_types_and_fields_are_refused():
    with pytest.raises(ValidationError):
        S.parse_body("document", _doc(blocks=[{"type": "html", "text": "<b>x</b>"}]))
    with pytest.raises(ValidationError):
        S.parse_body("document", _doc(blocks=[{"type": "paragraph", "text": "x", "style": "red"}]))


def test_a_citation_to_nothing_is_refused():
    with pytest.raises(ValidationError) as exc:
        S.parse_body("document", _doc(blocks=[{"type": "paragraph", "text": "x", "sources": ["ghost"]}]))
    assert "not in the sources list" in S.validation_summary(exc.value)


def test_citation_urls_are_http_only():
    for bad in ("file:///etc/passwd", "javascript:alert(1)", "data:text/html,x"):
        with pytest.raises(ValidationError):
            S.parse_body("document", _doc(sources=[{"id": "s1", "title": "t", "url": bad}]))


def test_tables_must_be_rectangular_and_charts_consistent():
    with pytest.raises(ValidationError):
        S.parse_body("document", _doc(blocks=[{"type": "table", "table": {"columns": ["a", "b"], "rows": [["x"]]}}]))
    with pytest.raises(ValidationError):
        S.parse_body("document", _doc(blocks=[{"type": "chart", "chart": {"type": "line", "categories": ["a", "b"], "series": [{"name": "s", "values": [1]}]}}]))
    with pytest.raises(ValidationError):
        S.parse_body("document", _doc(blocks=[{"type": "chart", "chart": {"type": "pie", "categories": ["a"], "series": [{"name": "s", "values": [1]}, {"name": "t", "values": [2]}]}}]))
    with pytest.raises(ValidationError):
        S.parse_body("document", _doc(blocks=[{"type": "chart", "chart": {"type": "scatter3d", "categories": ["a"], "series": [{"name": "s", "values": [1]}]}}]))


def test_the_envelope_carries_exactly_one_body():
    with pytest.raises(ValidationError):
        S.ArtifactSpec(kind="document")
    doc = S.DocumentSpec.model_validate(_doc())
    with pytest.raises(ValidationError):
        S.ArtifactSpec(kind="workbook", document=doc)
    with pytest.raises(ValueError):
        S.load({"spec_version": 99, "kind": "document", "document": _doc()})


def test_placeholders_are_found():
    spec = S.parse_body("document", _doc(blocks=[{"type": "paragraph", "text": "Lorem ipsum dolor. [Insert chart]. Real text."}]))
    found = S.placeholders_in(spec)
    assert any(p.lower().startswith("lorem") for p in found) and any(p.lower().startswith("[insert") for p in found)
    assert S.placeholders_in(S.parse_body("document", _doc())) == []


def test_a_presentation_validates_its_slides():
    pres = {
        "title": "Plan", "template_id": "ceo",
        "slides": [
            {"layout": "title", "title": "Plan", "subtitle": "FY27"},
            {"layout": "bullets", "title": "Goals", "bullets": ["Grow", "Retain"], "notes": "Say hello."},
            {"layout": "chart", "title": "Revenue", "chart": {"type": "line", "categories": ["Q1", "Q2"], "series": [{"name": "Rev", "values": [1, 2]}]}},
            {"layout": "timeline", "title": "Roadmap", "steps": [["Q1", "Launch"], ["Q2", "Scale"]]},
            {"layout": "kpis", "title": "Numbers", "kpis": [{"label": "ARR", "value": "$1M"}]},
            {"layout": "closing", "title": "Thank you"},
        ],
    }
    spec = S.parse_body("presentation", pres)
    assert len(spec.body.slides) == 6 and spec.body.slides[3].steps[0] == ("Q1", "Launch")
    with pytest.raises(ValidationError):
        S.parse_body("presentation", {**pres, "slides": [{"layout": "hologram", "title": "x"}]})


def test_a_slide_s_layout_follows_its_content_and_a_deck_opens_and_closes():
    """The first real run: every slide declared `bullets`; the one carrying
    the chart rendered blank and there was no title slide."""
    pres = {
        "title": "Pricing", "subtitle": "FY27", "template_id": "ceo",
        "slides": [
            {"layout": "bullets", "title": "Executive Summary", "bullets": ["a", "b"]},
            {"layout": "bullets", "title": "Revenue", "chart": {"type": "bar", "categories": ["old", "new"], "series": [{"name": "MRR", "values": [5880, 7080]}]}},
            {"layout": "bullets", "title": "Plans", "table": {"columns": ["Plan", "Price"], "rows": [["Team", 59]]}},
            {"layout": "bullets", "title": "Numbers", "kpis": [{"label": "ARR", "value": "$1M"}]},
            {"layout": "bullets", "title": "Closing", "bullets": ["Thank you"]},
        ],
    }
    spec = S.parse_body("presentation", pres)
    layouts = [s.layout for s in spec.body.slides]
    assert layouts == ["bullets", "chart", "table", "kpis", "closing"], "relabelled from content; nothing inserted"
    # A first slide that is only a title is the title slide whatever it was called.
    pres["slides"].insert(0, {"layout": "bullets", "title": "Pricing", "subtitle": "FY27"})
    spec = S.parse_body("presentation", pres)
    assert [s.layout for s in spec.body.slides][:3] == ["title", "bullets", "chart"]
    # A slide that already declares its layout is left alone.
    pres["slides"][2]["layout"] = "two_column"
    pres["slides"][2]["left"] = ["x"]
    spec = S.parse_body("presentation", pres)
    assert spec.body.slides[2].layout == "two_column"


def test_a_workbook_never_takes_a_formula_from_the_model():
    wb = {
        "title": "Budget", "template_id": "tracker",
        "sheets": [{
            "name": "Q1 / Plan?",
            "columns": [{"name": "Item"}, {"name": "Cost", "type": "currency"}],
            "rows": [["Rent", 1000], ["=HYPERLINK(\"http://x\")", 5]],
            "totals": [{"column": 1, "fn": "sum"}],
        }],
    }
    spec = S.parse_body("workbook", wb)
    sheet = spec.body.sheets[0]
    assert sheet.name == "Q1   Plan"  # forbidden characters stripped, not refused
    # The formula-shaped cell is DATA here; the renderer neutralises it.
    assert S.is_formula_like(sheet.rows[1][0])
    assert sheet.totals[0].fn == "sum"
    with pytest.raises(ValidationError):
        S.parse_body("workbook", {**wb, "sheets": [{**wb["sheets"][0], "totals": [{"column": 0, "fn": "sum"}]}]})
    with pytest.raises(ValidationError):
        S.parse_body("workbook", {**wb, "sheets": [wb["sheets"][0], {**wb["sheets"][0], "name": "q1 / plan?"}]})


def _tracker(totals):
    return {
        "title": "Plans", "template_id": "tracker",
        "sheets": [{
            "name": "Pricing Overview",
            "columns": [{"name": "Plan"}, {"name": "Price", "type": "currency"}, {"name": "Seats"},
                        {"name": "Target Accounts", "type": "integer"}, {"name": "Monthly Revenue", "type": "currency"}],
            "rows": [["Free", 0, "5", 120, 0], ["Team", 59, "25", 40, 2360], ["Enterprise", 199, "unlimited", 6, 1194]],
            "totals": totals,
        }],
    }


def test_a_total_names_its_column_by_header_text():
    """The e2e failure of 2026-09-11: five columns, totals over 4 and 5 —
    the model counted from 1, was told 5 was out of range, and did it again.
    A header name has no base to get wrong; the renderer still gets an index."""
    spec = S.parse_body("workbook", _tracker([{"column": "Monthly Revenue"}, {"column": " target  accounts ", "fn": "sum"}]))
    assert [t.column for t in spec.body.sheets[0].totals] == [4, 3]
    # Positions from 1 are recognisable as a SET when one is exactly one
    # past the end and none is 0 — and are shifted together.
    spec = S.parse_body("workbook", _tracker([{"column": 4, "label": "Total Accounts"}, {"column": 5, "label": "Total Revenue"}]))
    assert [t.column for t in spec.body.sheets[0].totals] == [3, 4]
    # Plain 0-based positions are untouched.
    spec = S.parse_body("workbook", _tracker([{"column": 3}, {"column": 4}]))
    assert [t.column for t in spec.body.sheets[0].totals] == [3, 4]
    # A validated spec round-trips (its indexes are resolved, never shifted twice).
    again = S.parse_body("workbook", json.loads(spec.body.model_dump_json()))
    assert [t.column for t in again.body.sheets[0].totals] == [3, 4]
    with pytest.raises(ValidationError) as exc:
        S.parse_body("workbook", _tracker([{"column": "Revenue"}]))
    assert "names no column" in S.validation_summary(exc.value) and "'Monthly Revenue'" in S.validation_summary(exc.value)
    with pytest.raises(ValidationError) as exc:
        S.parse_body("workbook", _tracker([{"column": 9}]))
    assert "name the column by its header text" in S.validation_summary(exc.value)
    with pytest.raises(ValidationError):
        S.parse_body("workbook", _tracker([{"column": "Plan", "fn": "sum"}]))  # summing text
    with pytest.raises(ValidationError):
        S.parse_body("workbook", _tracker([{"column": True}]))
    assert "header text" in S.schema_for("workbook")["$defs"]["Total"]["properties"]["column"]["description"]


def test_figures_the_material_never_gave_are_named():
    """The first Fast brief of the 2026-09-11 e2e run: asked for '$59 a month'
    and '120 team accounts', it wrote a $49 current price, competitors at
    $55-$65, 1,000 active teams and a 95% retention rate. The check names
    what the material does not contain; it does not judge it."""
    material = "Create a brief about moving our team plan to $59 a month for the CEO. We have 120 team accounts."
    doc = _doc(
        blocks=[
            {"type": "kpis", "items": [{"label": "Proposed", "value": "$59/month"}, {"label": "Current", "value": "$49/month"}, {"label": "Increase", "value": "+20.4%"}]},
            {"type": "paragraph", "text": "Competitors are priced between $55 and $65. For a base of 1,000 active teams this is $10,000 a month, or $120,000 a year, over 30 days and 60 days. In 2026 we have 120 accounts, 3 tiers and 5 seats."},
            {"type": "chart", "chart": {"type": "bar", "categories": ["Now", "Proposed"], "series": [{"name": "MRR", "values": [5880, 7080]}]}},
        ],
        assumptions=["Customer retention rate remains at or above 95%."],
    )
    spec = S.parse_body("document", doc)
    figures = S.unsupported_figures(spec, material)
    # Money, percentages, thousands and the chart's values — not day counts,
    # small counts, the year, the figures given, or the declared assumption.
    assert figures == ["$49", "20.4%", "$55", "$65", "1,000", "$10,000", "$120,000", "5880", "7080"]
    assert "95%" not in figures and "120" not in figures and "$59" not in figures and "30" not in figures and "2026" not in figures and "3" not in figures
    assert {"$49", "$55", "$65", "$10,000", "$120,000", "1,000", "5880", "7080"} <= set(figures)
    # Given the figures, nothing is named.
    assert S.unsupported_figures(spec, material + " current price $49; competitors $55-$65; 1,000 teams; $10,000/month = $120,000/year; MRR 5,880 vs 7,080; +20.4%") == []
    # A deck and a workbook are checked the same way (slide kpis, table cells, sheet rows).
    deck = S.parse_body("presentation", {"title": "Plans", "slides": [
        {"layout": "kpis", "title": "Numbers", "kpis": [{"label": "ARR", "value": "$1.2M"}]},
        {"layout": "table", "title": "Tiers", "table": {"columns": ["Tier", "Seats"], "rows": [["Team", "25"], ["Enterprise", "unlimited"]]}},
    ]})
    assert S.unsupported_figures(deck, "Team is $59 with 25 seats") == ["$1.2"]
    wb = S.parse_body("workbook", _tracker([]))
    assert S.unsupported_figures(wb, "Free $0, Team $59, Enterprise $199; accounts 120/40/6") == ["2360", "1194"]


def test_text_of_covers_every_prose_field():
    spec = S.parse_body("document", _doc())
    text = S.text_of(spec)
    for needle in ("Quarterly Review", "Revenue grew.", "One", "EMEA", "Figures are unaudited.", "ARR"):
        assert needle in text


# --------------------------------------------------------------- formats --


@pytest.mark.parametrize("text,kind,fmts,explicit", [
    ("Create a professional PDF about this.", "document", ["pdf"], True),
    ("Make a Word document from this conversation.", "document", ["docx"], True),
    ("Generate a PowerPoint presentation for the CEO.", "presentation", ["pptx", "pdf"], True),
    ("Turn this CSV into an Excel dashboard.", "workbook", ["xlsx"], True),
    ("Create an SOP document.", "document", ["docx", "pdf"], False),
    ("Prepare a one-page executive brief.", "document", ["pdf", "docx"], False),
    ("Build a deck for the board.", "presentation", ["pptx", "pdf"], False),
    ("Make a budget tracker.", "workbook", ["xlsx"], False),
    ("Give this to me as a document.", "document", ["docx", "pdf"], False),
    ("Make a printable handout.", "document", ["pdf"], False),
    ("Write a proposal I can edit later.", "document", ["docx", "pdf"], False),
])
def test_format_policy(text, kind, fmts, explicit):
    d = formats.decide(text)
    assert (d.kind, d.formats, d.explicit) == (kind, fmts, explicit), d


def test_template_follows_the_task_words():
    assert formats.decide("Create an SOP document.").template_id == "sop"
    assert formats.decide("Prepare a one-page executive brief.").template_id == "brief"
    assert formats.decide("Generate a PowerPoint presentation for the CEO.").template_id == "ceo"
    assert formats.decide("Summarise the meeting notes into a document").template_id == "meeting_summary"
    assert formats.decide("Make a KPI dashboard spreadsheet").template_id == "dashboard"


def test_an_impossible_explicit_format_is_dropped_with_a_warning():
    d = formats.decide("Make an Excel version of the presentation as xlsx", explicit_only=["xlsx"])
    assert d.kind == "workbook"
    ok, bad = formats.formats_for_conversion("presentation", ["xlsx", "pdf"])
    assert (ok, bad) == (["pdf"], ["xlsx"])
