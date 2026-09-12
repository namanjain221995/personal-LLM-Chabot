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
    # CONTRACT-2 §1 (2026-09-12): csv is a format; a workbook is delivered
    # as xlsx, csv, and a tabular Word/PDF; zip is a route, not a format.
    assert "csv" in T.FORMATS and T.MIME_TYPES["csv"] == "text/csv; charset=utf-8"
    assert T.FORMATS_FOR_KIND["workbook"] == ("xlsx", "csv", "docx", "pdf")
    assert T.FORMATS_FOR_KIND["document"] == ("docx", "pdf") and T.FORMATS_FOR_KIND["presentation"] == ("pptx", "pdf")
    assert T.MIME_TYPES["zip"] == "application/zip" and "zip" not in T.FORMATS
    assert T.MAX_ZIP_BYTES == 200 * 1024 * 1024
    assert set(T.PAGE_FORMATS) | set(T.GRID_FORMATS) == set(T.FORMATS)


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
    # A part — the per-sheet CSV of a multi-sheet workbook (CONTRACT-2 §2).
    assert T.download_name("IR Session Audit", 2, "csv", part="Pipeline Q3") == "ir-session-audit-v2-pipeline-q3.csv"
    assert T.download_name("IR Session Audit", 2, "csv", part="../x") == "ir-session-audit-v2-x.csv"
    assert T.download_name("IR Session Audit", 2, "csv", part="") == "ir-session-audit-v2.csv"
    assert T.download_name("IR Session Audit", 2, "csv", part="   ") == "ir-session-audit-v2.csv"
    assert T.download_name("IR Session Audit", 2, "csv", None) == "ir-session-audit-v2.csv"


def test_file_ids_are_deterministic_and_shaped():
    aid = "a" * 32
    fid = T.file_id_for(aid, 1, "primary", "xlsx")
    assert T.is_file_id(fid) and len(fid) == 16
    assert fid == T.file_id_for(aid, 1, "primary", "xlsx") == T.file_id_for(aid, 1, "primary", "xlsx", "")
    # Every ingredient changes the id; the sheet keeps two CSVs apart.
    assert len({
        fid, T.file_id_for(aid, 2, "primary", "xlsx"), T.file_id_for(aid, 1, "companion", "xlsx"),
        T.file_id_for(aid, 1, "primary", "csv"), T.file_id_for("b" * 32, 1, "primary", "xlsx"),
        T.file_id_for(aid, 1, "data", "csv", "Pipeline"), T.file_id_for(aid, 1, "data", "csv", "Summary"),
    }) == 7
    import hashlib
    assert fid == hashlib.sha1(f"{aid}:1:primary:xlsx:".encode()).hexdigest()[:16]
    for bad in ("", "A" * 16, "g" * 16, "a" * 15, "a" * 17, "a" * 32, "legacy:x.pdf"):
        assert not T.is_file_id(bad)


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
    # A legacy ref (no file_id): the alias URLs, the default role, no id on
    # the wire, no bundle for one file. CONTRACT-2 §2.
    f = j["files"][0]
    assert "file_id" not in f and f["role"] == "primary" and "rows" not in f and "columns" not in f
    assert f["inline_url"] == f"/artifacts/{'b' * 32}/v/1/file/pdf?disposition=inline"
    assert f["preview_url"] == f"/artifacts/{'b' * 32}/v/1/preview"
    assert "download_all_url" not in j and "package" not in j


def test_artifact_ref_with_file_ids_builds_per_file_urls_and_a_bundle():
    """CONTRACT-2 §2: every file carries file_id, role, title, rows and
    columns; URLs go by id; a grid format is previewed from its own file;
    two or more files get the zip route and a package count."""
    aid, jid = "b" * 32, "c" * 32
    xlsx_id = T.file_id_for(aid, 3, "primary", "xlsx")
    csv_id = T.file_id_for(aid, 3, "data", "csv", "Pipeline")
    pdf_id = T.file_id_for(aid, 3, "companion", "pdf")
    ref = T.ArtifactRef(
        artifact_id=aid, version=3, job_id=jid, title="IR Session Audit", kind="workbook", status="completed",
        files=[
            T.FileRef("xlsx", "ir-session-audit-v3.xlsx", T.MIME_TYPES["xlsx"], 4096, "s1", sheets=2, file_id=xlsx_id, role="primary", title="IR Session Audit", rows=30, columns=8),
            T.FileRef("csv", "ir-session-audit-v3-pipeline.csv", T.MIME_TYPES["csv"], 512, "s2", file_id=csv_id, role="data", title="IR Session Audit — Pipeline", rows=30, columns=8),
            T.FileRef("pdf", "ir-session-audit-v3.pdf", T.MIME_TYPES["pdf"], 8192, "s3", pages=2, file_id=pdf_id, role="companion", title="IR Session Audit"),
        ],
        preview_kind="grid",
    )
    j = ref.to_json()
    base = f"/artifacts/{aid}/v/3"
    x, c, p = j["files"]
    assert (x["file_id"], x["role"], x["title"], x["rows"], x["columns"], x["sheets"]) == (xlsx_id, "primary", "IR Session Audit", 30, 8, 2)
    assert x["download_url"] == f"{base}/f/{xlsx_id}?disposition=attachment" and x["inline_url"] == f"{base}/f/{xlsx_id}?disposition=inline"
    assert x["preview_url"] == f"{base}/grid?file={xlsx_id}"
    assert (c["role"], c["mime_type"], c["preview_url"]) == ("data", "text/csv; charset=utf-8", f"{base}/grid?file={csv_id}")
    assert c["filename"] == T.download_name("IR Session Audit", 3, "csv", part="Pipeline")
    # The PDF companion of a grid version is previewed as pages only when
    # the pipeline counted them (preview_pages); with none it is download-only.
    assert p["download_url"] == f"{base}/f/{pdf_id}?disposition=attachment" and p["preview_url"] == "" and p["pages"] == 2
    assert j["download_all_url"] == f"{base}/zip" and j["package"] == {"count": 3}
    ref.preview_pages = 2
    assert ref.to_json()["files"][2]["preview_url"] == f"{base}/preview"
    assert "http" not in json.dumps(j)
    # The version-level preview of a grid version is the legacy alias; a
    # pages version previews its page formats from the version.
    assert j["preview_url"] == f"{base}/sheets"
    ref.preview_kind, ref.preview_pages = "pages", 2
    j = ref.to_json()
    assert j["preview_url"] == f"{base}/preview" and j["files"][2]["preview_url"] == f"{base}/preview"
    # Positional construction (the original four-format contract) still works.
    legacy = T.FileRef("pdf", "x.pdf", T.MIME_TYPES["pdf"], 1, "sha", 2, None, None)
    assert legacy.file_id == "" and legacy.role == "primary" and legacy.title == "" and legacy.rows is None


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
    # Any bracketed phrase is a placeholder — a slide's KPI tile included —
    # but a citation mark like [1] is not.
    deck = S.parse_body("presentation", {"title": "Plans", "slides": [
        {"layout": "kpis", "title": "Numbers", "kpis": [{"label": "MRR", "value": "[Verified Current Monthly Revenue]"}]},
        {"layout": "bullets", "title": "Why", "bullets": ["Support costs rose [1].", "Rates justify it."]},
    ]})
    assert S.placeholders_in(deck) == ["[Verified Current Monthly Revenue]"]


def test_a_chart_of_zeros_is_refused():
    with pytest.raises(ValidationError) as exc:
        S.Chart.model_validate({"type": "bar", "categories": ["Old", "New"], "series": [{"name": "Revenue", "values": [0, 0]}]})
    assert "every value is 0" in str(exc.value)
    with pytest.raises(ValidationError):
        S.parse_body("document", _doc(blocks=[{"type": "chart", "chart": {"type": "bar", "categories": ["Old", "New"], "series": [{"name": "Revenue", "values": [0, 0]}]}}]))
    assert S.Chart.model_validate({"type": "bar", "categories": ["Old", "New"], "series": [{"name": "Revenue", "values": [0, 1]}]}).series[0].values == [0, 1]


def test_hollow_names_what_is_missing():
    assert S.hollow(S.parse_body("document", _doc())) == ""
    assert "no paragraphs" in S.hollow(S.parse_body("document", _doc(blocks=[{"type": "heading", "level": 1, "text": "Only a heading"}, {"type": "kpis", "items": [{"label": "ARR", "value": "$4.2M"}]}])))
    assert "fewer than two slides" in S.hollow(S.parse_body("presentation", {"title": "x", "slides": [{"layout": "title", "title": "x"}, {"layout": "bullets", "title": "one", "bullets": ["a"]}]}))
    assert "no sheet has any rows" in S.hollow(S.parse_body("workbook", {"title": "x", "sheets": [{"name": "A", "columns": [{"name": "c"}]}]}))
    assert S.part_count(S.parse_body("document", _doc())) == 8


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


def test_a_sheet_chart_written_over_the_header_gets_the_cells():
    """The e2e run of 2026-09-11, twice: `categories: ["Plan"]` — the header
    where the cells belong — with three values per series."""
    base = _tracker([])
    sheet = base["sheets"][0]
    sheet["charts"] = [{"type": "bar", "title": "Revenue by plan", "categories": ["Plan"], "series": [{"name": "Monthly Revenue", "values": [0, 2360, 1194]}]}]
    spec = S.parse_body("workbook", base)
    chart = spec.body.sheets[0].charts[0]
    assert chart.categories == ["Free", "Team", "Enterprise"] and chart.series[0].values == [0, 2360, 1194]
    # The header as a bare string, and a series named after a column with no values: filled from the cells.
    sheet["charts"] = [{"type": "bar", "categories": "plan", "series": [{"name": "monthly revenue"}, {"name": "Target Accounts", "values": [120, 40, 6]}]}]
    chart = S.parse_body("workbook", base).body.sheets[0].charts[0]
    assert chart.categories == ["Free", "Team", "Enterprise"]
    assert chart.series[0].values == [0.0, 2360.0, 1194.0] and chart.series[1].values == [120, 40, 6]
    # Cells that are not numbers cannot fill a series: still refused, still a repair.
    sheet["charts"] = [{"type": "bar", "categories": ["Plan"], "series": [{"name": "Seats"}]}]
    with pytest.raises(ValidationError):
        S.parse_body("workbook", base)
    # A chart that really is inconsistent is still refused.
    sheet["charts"] = [{"type": "bar", "categories": ["Free", "Team"], "series": [{"name": "Monthly Revenue", "values": [0, 2360, 1194]}]}]
    with pytest.raises(ValidationError):
        S.parse_body("workbook", base)
    assert "cells of the label column" in S.schema_for("workbook")["$defs"]["Sheet"]["properties"]["charts"]["description"]


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
    assert S.unsupported_figures(deck, "Team is $59 with 25 seats") == ["$1.2M"]
    assert S.unsupported_figures(deck, "Team is $59 with 25 seats; ARR 1,200,000") == []
    wb = S.parse_body("workbook", _tracker([]))
    assert S.unsupported_figures(wb, "Free $0, Team $59, Enterprise $199; accounts 120/40/6") == ["2360", "1194"]
    # 410k in the material is 410,000 in the draft; 1.2M is $1,200,000.
    doc = _doc(blocks=[{"type": "paragraph", "text": "Q1 revenue was $410,000 and ARR is $1,200,000; the plan is 3bn."}])
    assert S.unsupported_figures(S.parse_body("document", doc), "Q1 revenue 410k, ARR 1.2M") == ["3bn"]


# ---------------------------------------------------- code-made rows --


def _gen_sheet(**over):
    sheet = {
        "name": "Evaluations",
        "columns": [
            {"name": "candidate_id"}, {"name": "candidate_name"}, {"name": "status"},
            {"name": "technical_score", "type": "integer"}, {"name": "completion_time", "type": "date"}, {"name": "comment"},
        ],
        "generator": {"rows": 500, "seed": 7, "columns": [
            {"name": "candidate_id", "kind": "id", "pattern": "CAND-{n:04d}", "unique": True},
            {"name": "candidate_name", "kind": "name"},
            {"name": "status", "kind": "choice", "values": ["Completed", "In Progress"], "weights": [3, 1]},
            {"name": "technical_score", "kind": "int", "min": 0, "max": 100},
            {"name": "completion_time", "kind": "date", "start": "2026-01-01", "end": "2026-06-30", "only_when": {"column": "status", "in": ["Completed"]}},
            {"name": "comment", "kind": "text", "text": {"pool": ["Strong work", "Needs follow-up"]}},
        ]},
        "rewrite": [{"column": "comment", "instruction": "concise professional audit comment"}],
        "style": {"highlight": [{"column": "status", "color": "red"}], "orientation": "auto", "header_fill": "light"},
    }
    sheet.update(over)
    return sheet


def _gen_wb(**over):
    return {"title": "Evaluation dataset", "template_id": "data", "sheets": [_gen_sheet(**over)]}


def test_a_generator_sheet_validates_empty_and_filled():
    """CONTRACT-2 §4: the model's answer has no rows; the composer fills
    them and re-parses, and rows_from/generator stay as provenance — so
    both states validate, the empty one is not hollow, and the generated
    numbers are never "figures the material never gave"."""
    spec = S.parse_body("workbook", _gen_wb())
    sheet = spec.body.sheets[0]
    assert sheet.rows == [] and sheet.rows_are_code_made and sheet.generator.rows == 500 and sheet.generator.seed == 7
    assert sheet.generator.columns[4].only_when.in_ == ["Completed"]
    assert S.hollow(spec) == "" and S.part_count(spec) == 1
    assert S.unsupported_figures(spec, "500 rows") == []
    # The stored spec loads back, with `in` under its alias.
    again = S.load(json.loads(spec.model_dump_json()))
    assert again == spec
    assert json.loads(spec.body.sheets[0].generator.columns[4].only_when.model_dump_json(by_alias=True)) == {"column": "status", "in": ["Completed"]}
    # Filled: the rows are present, the generator stays, a chart over the
    # header is drawn over the first MAX_CHART_POINTS rows only.
    filled = json.loads(spec.body.model_dump_json())
    filled["sheets"][0]["rows"] = [[f"CAND-{i:04d}", "Asha Rao", "Completed", 40 + i % 60, "2026-02-02", "ok"] for i in range(1, 501)]
    filled["sheets"][0]["charts"] = [{"type": "bar", "categories": "candidate_id", "series": [{"name": "technical_score"}]}]
    spec2 = S.parse_body("workbook", filled)
    sheet2 = spec2.body.sheets[0]
    assert len(sheet2.rows) == 500 and sheet2.rows_are_code_made and sheet2.generator is not None
    assert S.part_count(spec2) == 501 and S.hollow(spec2) == ""
    chart = sheet2.charts[0]
    assert len(chart.categories) == T.MAX_CHART_POINTS == len(chart.series[0].values)
    assert chart.categories[0] == "CAND-0001" and chart.series[0].values[0] == 41.0
    assert S.unsupported_figures(spec2, "500 rows") == [], "code-made rows are not the model's figures"
    # A chart over a small sheet is unchanged (the pin from test_a_sheet_chart_written_over_the_header_gets_the_cells).
    small = _tracker([])
    small["sheets"][0]["charts"] = [{"type": "bar", "categories": ["Plan"], "series": [{"name": "Monthly Revenue"}]}]
    assert S.parse_body("workbook", small).body.sheets[0].charts[0].categories == ["Free", "Team", "Enterprise"]


def test_a_rows_from_sheet_validates_empty_and_names_its_table():
    wb = {"title": "IR Session Audit", "sheets": [{"name": "Audit", "columns": [{"name": "Host"}, {"name": "Finding"}], "rows_from": "paste1",
                                                   "rewrite": [{"column": "Finding", "instruction": "concise"}], "style": {"borders": "none", "wrap": True}}]}
    spec = S.parse_body("workbook", wb)
    sheet = spec.body.sheets[0]
    assert sheet.rows_from == "paste1" and sheet.rows == [] and sheet.rows_are_code_made
    assert S.hollow(spec) == "" and S.part_count(spec) == 1
    assert sheet.style.borders == "none" and sheet.style.wrap is True and sheet.style.header_bold is True and sheet.style.header_fill == "dark" and sheet.style.orientation == "auto"
    # Filled, the provenance stays.
    filled = json.loads(spec.body.model_dump_json())
    filled["sheets"][0]["rows"] = [["h1", "f1"], ["", "f2"]]
    spec2 = S.parse_body("workbook", filled)
    assert spec2.body.sheets[0].rows_from == "paste1" and len(spec2.body.sheets[0].rows) == 2
    # A blank rows_from is no rows_from; a sheet with neither and no rows is still hollow.
    wb["sheets"][0]["rows_from"] = "  "
    del wb["sheets"][0]["rewrite"]
    assert "no sheet has any rows" in S.hollow(S.parse_body("workbook", wb))
    with pytest.raises(ValidationError):
        S.parse_body("workbook", {**wb, "sheets": [{**wb["sheets"][0], "rows_from": "../x"}]})


def _refused(mut, needle):
    wb = _gen_wb()
    mut(wb["sheets"][0])
    with pytest.raises(ValidationError) as exc:
        S.parse_body("workbook", wb)
    summary = S.validation_summary(exc.value)
    assert needle in summary, summary


def test_generator_recipes_are_checked_by_name():
    _refused(lambda s: s["generator"]["columns"].pop(), "no recipe for column 'comment'")
    _refused(lambda s: s["generator"]["columns"].append({"name": "ghost", "kind": "int"}), "recipe for 'ghost', which the sheet has no column for")
    _refused(lambda s: s["generator"].update(rows=0), "greater than or equal to 1")
    _refused(lambda s: s["generator"].update(rows=T.MAX_ROWS_PER_SHEET + 1), f"less than or equal to {T.MAX_ROWS_PER_SHEET}")
    _refused(lambda s: s["generator"]["columns"][2].pop("values"), "is a choice and needs `values`")
    _refused(lambda s: s["generator"]["columns"][2].update(weights=[1]), "has 1 weights for 2 values")
    _refused(lambda s: s["generator"]["columns"][2].update(weights=[0, 0]), "not all zero")
    _refused(lambda s: s["generator"]["columns"][4]["only_when"].update(column="nope"), "only_when names no column 'nope'")
    _refused(lambda s: s["generator"]["columns"][4]["only_when"].update(column="completion_time"), "only_when cannot read itself")
    _refused(lambda s: s["generator"]["columns"].__setitem__(5, {"name": "comment", "kind": "derived", "derived": {"op": "sum", "columns": ["nope"]}}), "derived names no column 'nope'")
    _refused(lambda s: s["generator"]["columns"].__setitem__(5, {"name": "comment", "kind": "derived", "derived": {"op": "sum", "columns": ["candidate_name"]}}), "which is name, not a number")
    _refused(lambda s: s["generator"]["columns"].__setitem__(5, {"name": "comment", "kind": "derived", "derived": {"op": "diff", "columns": ["technical_score"]}}), "diff takes exactly two columns")
    _refused(lambda s: s["generator"]["columns"].__setitem__(5, {"name": "comment", "kind": "derived"}), "needs `derived: {op, columns}`")
    _refused(lambda s: s["generator"]["columns"].__setitem__(5, {"name": "comment", "kind": "text"}), "needs `text: {pool: [...]}`")
    _refused(lambda s: s["generator"]["columns"][3].update(unique=True), "unique is only meaningful for id, name, email, text")
    _refused(lambda s: s["generator"]["columns"][0].update(pattern="{x}"), "must contain {n}")
    _refused(lambda s: s["generator"]["columns"][0].update(pattern="{n}-{x}"), "must use only {n}")
    _refused(lambda s: s["generator"]["columns"][4].update(start="soon"), "is not an ISO date")
    _refused(lambda s: s["generator"]["columns"][4].update(start="2026-07-01"), "is after end")
    _refused(lambda s: s["generator"]["columns"][3].update(min=5, max=1), "min 5 is above max 1")
    _refused(lambda s: s["generator"]["columns"][3].update(kind="hologram"), "kind")
    _refused(lambda s: s["generator"]["columns"].append({"name": "Candidate_ID", "kind": "int"}), "two generator columns are named")
    _refused(lambda s: s.update(rows_from="paste1"), "rows come from one place")
    # Recipes given in another order are put in the sheet's order.
    wb = _gen_wb()
    wb["sheets"][0]["generator"]["columns"].reverse()
    names = [c.name for c in S.parse_body("workbook", wb).body.sheets[0].generator.columns]
    assert names == [c["name"] for c in wb["sheets"][0]["columns"]]
    # A derived column over numbers, with concat over anything.
    wb = _gen_wb()
    wb["sheets"][0]["columns"].append({"name": "total", "type": "number"})
    wb["sheets"][0]["generator"]["columns"].append({"name": "total", "kind": "derived", "derived": {"op": "sum", "columns": ["technical_score"]}})
    wb["sheets"][0]["columns"].append({"name": "label"})
    wb["sheets"][0]["generator"]["columns"].append({"name": "label", "kind": "derived", "derived": {"op": "concat", "columns": ["candidate_id", "candidate_name"]}})
    assert len(S.parse_body("workbook", wb).body.sheets[0].generator.columns) == 8


def test_rewrite_and_style_name_existing_columns():
    _refused(lambda s: s["rewrite"][0].update(column="nope"), "rewrite names no column 'nope'")
    _refused(lambda s: s["style"]["highlight"][0].update(column="nope"), "highlight names no column 'nope'")
    _refused(lambda s: s["style"]["highlight"][0].update(color="purple"), "color")
    _refused(lambda s: s["style"].update(orientation="sideways"), "orientation")
    _refused(lambda s: s["style"].update(borders="thick"), "borders")
    _refused(lambda s: s["rewrite"][0].update(instruction=""), "instruction")
    # Header text is matched like a total's: case and inner whitespace do not count.
    wb = _gen_wb()
    wb["sheets"][0]["rewrite"][0]["column"] = " COMMENT "
    wb["sheets"][0]["style"]["highlight"][0]["column"] = "Status"
    spec = S.parse_body("workbook", wb)
    assert spec.body.sheets[0].style.highlight[0].color == "red" and spec.body.sheets[0].style.header_fill == "light"


def test_the_workbook_schema_describes_the_new_fields():
    schema = S.schema_for("workbook")
    sheet = schema["$defs"]["Sheet"]["properties"]
    for name in ("rows_from", "generator", "rewrite", "style"):
        assert name in sheet and sheet[name].get("description"), name
    assert "never retype the rows" in sheet["rows_from"]["description"]
    assert "Leave `rows` empty" in sheet["generator"]["description"]
    gen = schema["$defs"]["Generator"]["properties"]
    assert gen["rows"]["maximum"] == T.MAX_ROWS_PER_SHEET and gen["seed"]["default"] == 42
    col = schema["$defs"]["GenColumn"]["properties"]
    assert set(col) >= {"name", "kind", "pattern", "values", "weights", "min", "max", "decimals", "start", "end", "unique", "only_when", "derived", "text"}
    assert "CAND-{n:04d}" in col["pattern"]["description"]
    assert set(col["kind"]["enum"]) == {"id", "name", "email", "choice", "int", "float", "date", "datetime", "text", "derived"}
    assert list(schema["$defs"]["OnlyWhen"]["properties"]) == ["column", "in"], "the model writes `in`, as the contract says"
    assert set(schema["$defs"]["Derived"]["properties"]["op"]["enum"]) == {"sum", "mean", "min", "max", "diff", "concat"}
    style = schema["$defs"]["SheetStyle"]["properties"]
    assert style["borders"]["default"] == "thin" and style["header_bold"]["default"] is True and style["orientation"]["default"] == "auto"
    assert set(schema["$defs"]["Highlight"]["properties"]["color"]["enum"]) == {"red", "amber", "green", "blue"}
    assert "Rewrite" in schema["$defs"] and "column" in schema["$defs"]["Rewrite"]["properties"]


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
    # A workbook may now be converted to Word/PDF (CONTRACT-2 §1); a
    # document still cannot become a csv.
    assert formats.formats_for_conversion("workbook", ["docx", "pdf", "csv"]) == (["docx", "pdf", "csv"], [])
    assert formats.formats_for_conversion("document", ["csv"]) == ([], ["csv"])


def test_an_id_pattern_is_read_before_it_is_ever_formatted():
    """Security review 2026-09-12: `{n:0999999999d}` is a valid format that
    allocates a gigabyte per call; the spec's own probe was the first one."""
    with pytest.raises(ValidationError) as exc:
        S.GenColumn.model_validate({"name": "id", "kind": "id", "pattern": "X-{n:0999999999d}"})
    assert "width" in str(exc.value)
    assert S.GenColumn.model_validate({"name": "id", "kind": "id", "pattern": "X-{n:06d}"}).pattern == "X-{n:06d}"
