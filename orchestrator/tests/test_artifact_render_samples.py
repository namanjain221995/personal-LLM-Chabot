"""Realistic sample specs the render tests share — one per template.

These are builders, not fixtures, so a test can take a sample and change one
field. They are also tests: every sample must validate against the contract
in app/artifacts/spec.py, so a contract change that breaks a sample fails
here first, with the sample's name, rather than deep inside a renderer test.
"""
from __future__ import annotations

from typing import List

import pytest

from app.artifacts import spec as S
from app.artifacts import types as T

SOURCES = [
    S.Citation(id="ledger", title="Finance ledger export", url="https://example.com/ledger", retrieved_at="2026-09-10"),
    S.Citation(id="crm", title="CRM pipeline report", url="https://example.com/crm", note="Q2 snapshot"),
]


def revenue_chart(**over) -> S.Chart:
    base = dict(
        type="bar", title="Revenue by quarter", categories=["Q1", "Q2", "Q3", "Q4"],
        series=[S.Series(name="FY25", values=[120, 140, 160, 190]), S.Series(name="FY26", values=[130, 150, 170, 210])],
        y_label="USD k", caption="Quarterly revenue, both years", sources=["ledger"],
    )
    base.update(over)
    return S.Chart(**base)


def mix_pie() -> S.Chart:
    return S.Chart(type="pie", title="Revenue mix", categories=["Services", "Licences", "Support"], series=[S.Series(name="Share", values=[55, 30, 15])])


def region_table() -> S.Table:
    return S.Table(
        columns=["Region", "Revenue", "Cost", "Margin %"],
        rows=[["North", 120000, 80000.5, 33.3], ["South", 90000, 61000, 32.2], ["West", 70000, 44000, 37.1], ["East", 45000, 30000, 33.3]],
        numeric_columns=[1, 2, 3], caption="Regional figures", sources=["crm"],
    )


def kpis() -> S.KPIRow:
    return S.KPIRow(items=[S.KPI(label="ARR", value="$4.2M", note="+18% YoY"), S.KPI(label="NRR", value="112%"), S.KPI(label="Churn", value="3.1%", note="down from 4.0%")])


def _sections(n: int) -> List[S.DocumentBlock]:
    """n headed sections with mixed blocks — the 10-section timing document."""
    out: List[S.DocumentBlock] = []
    for i in range(1, n + 1):
        out.append(S.Heading(level=1, text=f"Section {i}: {'Operations Financials Risks Outlook People Product Market Customers Systems Plan'.split()[(i - 1) % 10]}"))
        out.append(S.Paragraph(text=("This section explains what happened, why it matters, and what the team will do next. " * 6).strip(), sources=["ledger"] if i % 2 else []))
        out.append(S.Heading(level=2, text=f"Detail {i}.1"))
        out.append(S.Bullets(items=[f"Point {i}.{k}: a concrete observation with a number ({k * 7}%)" for k in range(1, 5)]))
        if i % 3 == 0:
            out.append(S.TableBlock(table=region_table()))
        if i % 4 == 0:
            out.append(S.ChartBlock(chart=revenue_chart(title=f"Chart for section {i}")))
        if i % 5 == 0:
            out.append(S.Callout(kind="warning", title="Risk", text="Concentration: the top three customers are 41% of revenue."))
    return out


def document(template_id: str = "generic", *, sections: int = 4, **over) -> S.ArtifactSpec:
    blocks: List[S.DocumentBlock] = [kpis()] + _sections(sections) + [S.Callout(kind="quote", text="We build what people need.")]
    base = dict(
        title=f"{template_id.replace('_', ' ').title()} sample <draft> & \"quoted\"", subtitle="A subtitle with <b>markup</b>", audience="Leadership team",
        author="Finance team", date="2026-09-11", template_id=template_id, cover=template_id in ("technical_report",), toc=True,
        confidential=True, blocks=blocks, sources=SOURCES, assumptions=["FX held at the 2025 average", "No change to headcount plan"],
        purpose="Decide the FY27 budget",
    )
    base.update(over)
    return S.ArtifactSpec(kind="document", document=S.DocumentSpec(**base))


def deck(template_id: str = "generic", **over) -> S.ArtifactSpec:
    """A 12-slide deck using every layout at least once."""
    slides = [
        S.Slide(layout="title", title="FY26 Quarterly Review", subtitle="Q2 results & outlook"),
        S.Slide(layout="kpis", title="The quarter in numbers", kpis=[S.KPI(label="Revenue", value="$1.4M", note="+12% QoQ"), S.KPI(label="New logos", value="14"), S.KPI(label="NRR", value="112%"), S.KPI(label="Churn", value="3.1%")]),
        S.Slide(layout="section", title="Sales", subtitle="Pipeline and wins"),
        S.Slide(layout="bullets", title="What happened", bullets=[f"Bullet {k} about the quarter <with markup>" for k in range(1, 8)], notes="Speaker notes for slide four."),
        S.Slide(layout="chart", title="Pipeline trend", chart=revenue_chart(type="line", title="Pipeline by quarter"), bullets=["Creation up 2x", "Win rate steady"]),
        S.Slide(layout="chart", title="Revenue mix", chart=mix_pie()),
        S.Slide(layout="table", title="Regional performance", table=region_table()),
        S.Slide(layout="two_column", title="Wins and misses", left_title="Wins", right_title="Misses", left=["Enterprise", "Partnerships"], right=["SMB churn", "Support backlog"]),
        S.Slide(layout="comparison", title="Build vs buy", left_title="Build", right_title="Buy", left=["Full control", "Six months"], right=["Faster", "Licence cost"]),
        S.Slide(layout="timeline", title="Roadmap", steps=[("Q3", "Launch v2"), ("Q4", "EU expansion"), ("Q1 27", "Marketplace"), ("Q2 27", "Platform APIs")]),
        S.Slide(layout="chart", title="Deals by region", chart=revenue_chart(type="horizontal_bar", title="Deals by region", series=[S.Series(name="Deals", values=[12, 9, 7, 4])])),
        S.Slide(layout="closing", title="Thank you", subtitle="Questions?", bullets=["finance@example.com"]),
    ]
    base = dict(title="FY26 Quarterly Review", subtitle="Q2", author="Finance", date="2026-09-11", template_id=template_id, confidential=True, slides=slides, sources=SOURCES)
    base.update(over)
    return S.ArtifactSpec(kind="presentation", presentation=S.PresentationSpec(**base))


def workbook(template_id: str = "generic", *, rows: int = 40, **over) -> S.ArtifactSpec:
    """Three sheets: a typed pipeline with totals and charts, a small regions
    sheet with a chart over its columns, and a text-only notes sheet."""
    pipeline_rows = []
    for i in range(1, rows + 1):
        note = {1: '=HYPERLINK("http://evil.example/x")', 2: "+cmd|' /C calc'!A0", 3: "-2+3", 4: "@SUM(A1:A9)", 5: "\tTab lead", 6: "\rCR lead"}.get(i, "plain text")
        pipeline_rows.append([f"Deal {i}", f"2026-{1 + i % 9:02d}-{1 + i % 27:02d}", 1000 * i + 0.5, i * 3, round(0.01 * (i % 30), 3), note])
    pipeline = S.Sheet(
        name="Pipeline", columns=[S.Column(name="Deal"), S.Column(name="Close date", type="date"), S.Column(name="Amount", type="currency", width=14),
                                 S.Column(name="Seats", type="integer"), S.Column(name="Discount", type="percent"), S.Column(name="Note", width=30)],
        rows=pipeline_rows, totals=[S.Total(column=2, fn="sum"), S.Total(column=3, fn="sum"), S.Total(column=4, fn="average", label="Avg")],
        charts=[S.Chart(type="bar", title="Amount by deal", categories=[f"Deal {i}" for i in range(1, 6)], series=[S.Series(name="Amount", values=[1000.5, 2000.5, 3000.5, 4000.5, 5000.5])])],
    )
    regions = S.Sheet(
        name="Regions", columns=[S.Column(name="Region"), S.Column(name="Q1", type="number"), S.Column(name="Q2", type="number")],
        rows=[["North", 120, 140], ["South", 90, 110], ["West", 70, 95]], totals=[S.Total(column=1), S.Total(column=2)],
        charts=[S.Chart(type="line", title="By region", categories=["North", "South", "West"], series=[S.Series(name="Q1", values=[120, 90, 70]), S.Series(name="Q2", values=[140, 110, 95])]),
                S.Chart(type="pie", title="Q2 share", categories=["North", "South", "West"], series=[S.Series(name="Q2", values=[140, 110, 95])])],
    )
    notes = S.Sheet(name="Notes: [draft]", columns=[S.Column(name="Topic"), S.Column(name="Text")], rows=[["Scope", "Pipeline as of Friday"], ["Owner", "Finance"]], freeze_header=False, autofilter=False)
    base = dict(title="Sales tracker", purpose="Weekly pipeline review", template_id=template_id, sheets=[pipeline, regions, notes], sources=SOURCES, assumptions=["FX flat"])
    base.update(over)
    return S.ArtifactSpec(kind="workbook", workbook=S.WorkbookSpec(**base))


# ------------------------------------------------------------------ tests --


@pytest.mark.parametrize("template_id", T.DOCUMENT_TEMPLATES)
def test_document_samples_validate(template_id):
    spec = document(template_id, sections=10)
    assert spec.kind == "document" and spec.body.template_id == template_id
    assert S.placeholders_in(spec) == []


@pytest.mark.parametrize("template_id", T.PRESENTATION_TEMPLATES)
def test_deck_samples_validate(template_id):
    spec = deck(template_id)
    assert len(spec.body.slides) == 12
    assert {s.layout for s in spec.body.slides} == {"title", "section", "bullets", "two_column", "chart", "table", "comparison", "timeline", "kpis", "closing"}


@pytest.mark.parametrize("template_id", T.WORKBOOK_TEMPLATES)
def test_workbook_samples_validate(template_id):
    spec = workbook(template_id)
    # The spec replaces each of [ ] : with a space, so the name stays legal for Excel.
    assert [s.name for s in spec.body.sheets] == ["Pipeline", "Regions", "Notes   draft"]
    assert any(S.is_formula_like(cell) for row in spec.body.sheets[0].rows for cell in row)
