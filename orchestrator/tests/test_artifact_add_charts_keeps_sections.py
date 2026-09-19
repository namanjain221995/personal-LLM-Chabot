"""Adding charts to a report inserts charts and changes nothing else.

THE FAILURE THIS PINS (hotfix 1.1 live replay, 2026-09-19). The owner's third
turn, "also i want Plots on this docs", after "give Big report" over
customers-100.csv, is a deterministic `add_chart` edit (rule
convert-artifact-turn -> edit, zero model calls). It still changed sections the
request never named:

- fast2: 'Company and Contact Information Analysis' and 'Website and Digital
  Presence Insights' each held the composer's "The chart was not drawn:
  Company has 80 different values in 100 rows" callout. `add_chart` replaced
  both callouts IN PLACE with unrelated charts ("Records by Subscription
  Date" under the company section), so the explanation disappeared and the
  self-check reported the two sections as changed.
- fast1: the new "Charts" section was appended after 'Assumptions and
  Limitations', a closing section the placement rule did not know.

A refusal on the data's SHAPE stays true after the edit, so it stays. Only a
"the table ... is not available" callout is a stale failure the edit's data
now answers (test_artifact_edit_add_chart pins that replacement).
"""
from __future__ import annotations

import csv
from pathlib import Path

from app.artifacts import edits as E
from app.artifacts import selfcheck as SC
from app.artifacts import spec as S
from app.artifacts.compose import DataTable

FIXTURES = Path(__file__).parent / "fixtures" / "artifacts"
ASK = "also i want Plots on this docs"


def _customers() -> DataTable:
    with (FIXTURES / "customers_100.csv").open(newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    return DataTable(id="upload1", title="customers-100.csv", columns=rows[0],
                     rows=[[c if c != "" else None for c in r] for r in rows[1:]], source_id="upload")


def _refusal(title: str, column: str, n: int) -> dict:
    return {"type": "callout", "kind": "note", "title": title,
            "text": f"The chart was not drawn: {column} has {n} different values in 100 rows, so counting them draws one bar per row."}


def _report(closing: str) -> S.ArtifactSpec:
    """The shape of the fast2 v1 report: H1 sections, two drawn charts, two
    shape refusals, a closing section last."""
    blocks = [
        {"type": "heading", "level": 1, "text": "Executive Summary"},
        {"type": "paragraph", "text": "The file holds one row per customer."},
        {"type": "heading", "level": 1, "text": "Geographic Distribution of Customers"},
        {"type": "paragraph", "text": "Customers are spread across twelve countries."},
        {"type": "chart", "chart": {"type": "horizontal_bar", "title": "Customer Count by Country",
                                    "data": {"table_id": "upload1", "x": "Country", "y": [], "agg": "count"}}},
        {"type": "heading", "level": 1, "text": "Company and Contact Information Analysis"},
        {"type": "paragraph", "text": "Company names rarely repeat."},
        _refusal("Top 10 Company Entities by Customer Count", "Company", 80),
        {"type": "heading", "level": 1, "text": "Website and Digital Presence Insights"},
        {"type": "paragraph", "text": "Every customer lists a website."},
        _refusal("Distribution of Website Top-Level Domains", "Website", 98),
        {"type": "heading", "level": 1, "text": closing},
        {"type": "bullets", "items": ["Keep the country field required.", "Review the oldest records."]},
    ]
    return S.parse_body("document", {"title": "Customer Subscription Analysis Report", "blocks": blocks})


def _blocks(spec: S.ArtifactSpec) -> list:
    return spec.body.model_dump(mode="json", exclude_none=True)["blocks"]


def _without_inserted_charts(parent: S.ArtifactSpec, child: S.ArtifactSpec) -> list:
    """The child's blocks with every chart the parent did not have removed,
    and the heading of a section that holds nothing but those charts."""
    had = [b for b in _blocks(parent) if b["type"] == "chart"]
    out, i = [], 0
    blocks = _blocks(child)
    while i < len(blocks):
        b = blocks[i]
        if b["type"] == "heading":
            j = i + 1
            while j < len(blocks) and blocks[j]["type"] != "heading":
                j += 1
            body = blocks[i + 1:j]
            if body and all(x["type"] == "chart" and x not in had for x in body) and b not in _blocks(parent):
                i = j
                continue
        if not (b["type"] == "chart" and b not in had):
            out.append(b)
        i += 1
    return out


def _apply(parent: S.ArtifactSpec) -> E.EditOutcome:
    plan = E.preplan(ASK, parent)
    assert plan is not None and [o.op for o in plan.ops] == ["add_chart"], plan
    return E.apply(parent, plan, tables=[_customers()], instruction=ASK)


def test_adding_charts_keeps_every_existing_section_verbatim():
    parent = _report("Strategic Recommendations and Assumptions")
    out = _apply(parent)
    assert out.changed and out.applied_ops == ["add_chart"], out.not_applied
    new_charts = [b for b in _blocks(out.spec) if b["type"] == "chart" and b not in _blocks(parent)]
    assert new_charts, "the request added at least one chart"
    assert _without_inserted_charts(parent, out.spec) == _blocks(parent), \
        "every section the request did not name is the parent's, block for block; only charts were inserted"


def test_a_shape_refusal_stays_where_it_was():
    parent = _report("Strategic Recommendations and Assumptions")
    out = _apply(parent)
    texts = [b.get("text", "") for b in _blocks(out.spec) if b["type"] == "callout"]
    assert any("Company has 80 different values" in t for t in texts)
    assert any("Website has 98 different values" in t for t in texts)


def test_the_self_check_finds_no_unnamed_section_changed():
    parent = _report("Strategic Recommendations and Assumptions")
    out = _apply(parent)
    before, after = SC._section_blocks(parent), SC._section_blocks(out.spec)
    assert SC._unexplained_changes(before, after, ASK) == [], "the version record would say sections disappeared"


def test_the_charts_go_before_a_closing_section_with_a_lead_word_or_named_for_assumptions():
    for closing in ("Strategic Recommendations and Assumptions", "Assumptions and Limitations", "8. Key Takeaways"):
        parent = _report(closing)
        out = _apply(parent)
        heads = [b["text"] for b in _blocks(out.spec) if b["type"] == "heading"]
        assert "Charts" in heads, (closing, heads)
        assert heads.index("Charts") == heads.index(closing) - 1, (closing, heads)
        assert heads[-1] == closing, "the closing section stays last"


def test_a_closing_word_early_in_the_document_does_not_pull_the_charts_up():
    """GUARD. Only the run of closing sections at the END counts: a report
    that opens with "Summary and Recommendations" keeps its charts after the
    body, not before the first section."""
    parent = _report("Appendix")
    blocks = _blocks(parent)
    blocks[0] = {"type": "heading", "level": 1, "text": "Summary and Recommendations"}
    parent = S.parse_body("document", {"title": "T", "blocks": blocks})
    out = _apply(parent)
    heads = [b["text"] for b in _blocks(out.spec) if b["type"] == "heading"]
    assert heads.index("Charts") == heads.index("Appendix") - 1, heads
