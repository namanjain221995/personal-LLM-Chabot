"""What the person reads when they ask for a breakdown by two things at once.

The figures are proved exact in tests/test_profile_cross.py. These are about
the ANSWER and the PDF: the table is there, the gap sentence that used to
stand in its place is gone, what the cross leaves out rides with it, and no
internal field name reaches the page.
"""
from __future__ import annotations

import csv
import json
import random
from decimal import Decimal

import pytest

from app.core import profile as profiler
from app.engines import dataset, dataset_report as report

REGIONS = ("North", "South", "East", "West", "Central")
CHANNELS = ("direct", "partner", "online")
ASK = "Generate a PDF report of monthly revenue by region"


def _sales(path, n=600, *, seed=17, blanks=0):
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        m = rng.randrange(12)
        rows.append({
            "order_id": 100000 + i,
            "order_date": f"2024-{m + 1:02d}-{rng.randrange(1, 28):02d}",
            "region": REGIONS[rng.randrange(len(REGIONS))],
            "channel": CHANNELS[rng.randrange(len(CHANNELS))],
            "revenue": f"{rng.randrange(100, 900000) / 100:.2f}",
        })
    for i in rng.sample(range(n), blanks) if blanks else ():
        rows[i]["order_date"] = ""
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return rows


def _upload(path, message, **kwargs):
    """The upload as the engine hands it on: profiled once with no cross (the
    upload path), then again with the pair this message names (_with_cross).
    Through JSON, as Postgres stores and returns it."""
    stored = profiler.profile_tabular(str(path), **kwargs)
    spec = report.cross_request(stored, report.request_text(message))
    prof = profiler.profile_tabular(str(path), cross=spec, **kwargs) if spec else stored
    return {
        "filename": path.name,
        "bytes": path.stat().st_size,
        "status": "ready",
        "notes": None,
        "profile": [json.loads(json.dumps(prof, default=str))],
    }


# ---------------------------------------------------------------------------
# The PDF
# ---------------------------------------------------------------------------


def test_the_pdf_holds_the_asked_for_cross_instead_of_a_note_about_it(tmp_path):
    """Release-2 wrote: "A breakdown of revenue by region for each month is
    not computed for this report; the two breakdowns above are." It is
    computed now, so the report shows it."""
    path = tmp_path / "sales.csv"
    rows = _sales(path)
    up = _upload(path, ASK)
    md = report.build_report_markdown("Data Report", [up], "", "2026-09-21 12:00", message=ASK)

    assert "not computed for this report" not in md
    assert "### Total revenue by region for each month" in md
    # The one-dimensional breakdowns it reconciles against are still there.
    assert "### Total revenue by region" in md and "by month (order_date)" in md

    table = md.split("### Total revenue by region for each month")[1].split("###")[0]
    header = [c.strip() for c in table.strip().splitlines()[0].strip("|").split("|")]
    # The longer axis goes DOWN the page: twelve months of rows, five regions
    # of columns. The other way round the table ran off the right margin.
    assert header[0] == "Month" and sorted(header[1:]) == sorted(REGIONS)
    truth = {}
    for r in rows:
        truth.setdefault((r["region"], r["order_date"][:7]), Decimal(0))
        truth[(r["region"], r["order_date"][:7])] += Decimal(r["revenue"])
    body = [line for line in table.strip().splitlines()[2:] if line.startswith("|")]
    assert len(body) == 12
    for line in body:
        cells = [c.strip() for c in line.strip("|").split("|")]
        month = cells[0]
        for region, shown in zip(header[1:], cells[1:]):
            want = truth[(region, month)].quantize(Decimal("0.01"))
            assert shown == f"{want:,.2f}", (region, month, shown)


def test_the_pdf_says_what_the_cross_leaves_out(tmp_path):
    path = tmp_path / "sales.csv"
    rows = _sales(path, 1200, blanks=97)
    md = report.build_report_markdown(
        "Data Report", [_upload(path, ASK)], "", "2026-09-21 12:00", message=ASK
    )
    gap = sum((Decimal(r["revenue"]) for r in rows if not r["order_date"]), Decimal(0))
    note = md.split("### Total revenue by region for each month")[1].split("###")[0]
    assert "97 row(s) have no date" in note
    assert str(gap.quantize(Decimal("0.01"))) in note


def test_a_cross_too_wide_for_the_page_is_split_not_cut_off(tmp_path):
    """Measured 2026-09-21: a 5 x 8 table rendered three columns off the right
    edge of the A4 page, "2024-1" and "186,4" cut mid-cell. Every figure still
    reaches the reader — in as many tables as it takes."""
    global REGIONS
    # Eight regions over twelve months is exactly CROSS_MAX_CELLS, so the
    # profile keeps every pair and the only question is the page.
    wide = tuple(f"Region{i:02d}" for i in range(8))
    was, REGIONS = REGIONS, wide
    try:
        path = tmp_path / "sales.csv"
        rows = _sales(path, 3000, seed=21)
    finally:
        REGIONS = was
    md = report.build_report_markdown(
        "Data Report", [_upload(path, ASK)], "", "2026-09-21 12:00", message=ASK
    )
    table = md.split("### Total revenue by region for each month")[1].split("###")[0]
    heads = [line for line in table.splitlines() if line.startswith("| Month |")]
    assert len(heads) == 2, "eight regions do not fit one A4 column"
    shown = [c.strip() for line in heads for c in line.strip("|").split("|")[1:]]
    assert sorted(shown) == sorted(wide), "and no region is dropped to make them fit"
    for line in heads:
        assert len(line.strip("|").split("|")) - 1 <= report.REPORT_CROSS_COLUMNS
    truth = {}
    for r in rows:
        truth.setdefault((r["region"], r["order_date"][:7]), Decimal(0))
        truth[(r["region"], r["order_date"][:7])] += Decimal(r["revenue"])
    seen = 0
    for chunk in table.split("| Month |")[1:]:
        lines = [l for l in chunk.splitlines() if l.startswith("|")]
        head = [c.strip() for c in ("|" + chunk.splitlines()[0]).strip("|").split("|")]
        for line in lines[1:]:
            cells = [c.strip() for c in line.strip("|").split("|")]
            if set(cells[0]) <= set("- :"):
                continue
            for region, got in zip(head, cells[1:]):
                want = truth[(region, cells[0])].quantize(Decimal("0.01"))
                assert got == f"{want:,.2f}", (region, cells[0], got)
                seen += 1
    assert seen == 8 * 12


def test_no_internal_field_name_reaches_the_page(tmp_path):
    path = tmp_path / "sales.csv"
    _sales(path)
    md = report.build_report_markdown(
        "Data Report", [_upload(path, ASK)], "", "2026-09-21 12:00", message=ASK
    )
    for word in ("by_cross", "by_group", "by_month", "aggregates", "full_rows", "profile"):
        assert word not in md, word


def test_a_figure_the_cross_holds_is_a_figure_the_summary_may_state(tmp_path):
    """unsupported_figures drops a summary stating a number no profile holds.
    A cross cell is now such a number, so quoting one must not cost the
    summary."""
    path = tmp_path / "sales.csv"
    _sales(path)
    up = _upload(path, ASK)
    entry = up["profile"][0]["aggregates"]["by_cross"][0]
    cell = entry["rows"][0]["cells"][0]
    said = f"{entry['rows'][0]['value']} took {cell['sum']:,.2f} in {cell['month']}."
    assert report.unsupported_figures(said, [up]) == []
    assert report.unsupported_figures("Revenue reached 8,675,309.01 that month.", [up])


# ---------------------------------------------------------------------------
# The chat answer
# ---------------------------------------------------------------------------


def test_the_answer_is_no_longer_told_the_figure_is_not_computed(tmp_path):
    """dataset._crossed wrote "- NOT COMPUTED: revenue by month for each
    region (a breakdown by two things at once)" into the data block. With the
    cross computed, that line is a lie."""
    path = tmp_path / "sales.csv"
    _sales(path)
    ask = "Which region grew fastest, month by month?"
    without = _upload(path, "What is the total revenue?")   # no cross computed
    with_cross = _upload(path, "monthly revenue by region")

    lines = dataset.question_figures(ask, [without])
    assert any("NOT COMPUTED" in line for line in lines), "premise: it used to say so"
    assert not any("NOT COMPUTED" in line for line in dataset.question_figures(ask, [with_cross]))


def test_the_data_block_carries_every_pair_with_its_labels_first(tmp_path):
    path = tmp_path / "sales.csv"
    rows = _sales(path, 600, blanks=41)
    up = _upload(path, "monthly revenue by region")
    block = dataset.format_profile([up], dataset.question_figures("monthly revenue by region", [up]))

    entry = up["profile"][0]["aggregates"]["by_cross"][0]
    for row in entry["rows"]:
        for cell in row["cells"]:
            assert f'"{cell["month"]}"' in block
    # A row's label is read BEFORE its figures (JSONB hands keys back
    # shortest-first, which put a month after the sum it belongs to).
    rendered = json.loads(block.split("\n", 1)[1].rsplit("\n", 1)[0].split("\n\n")[0].split("\n", 1)[1])
    first = (rendered[0] if isinstance(rendered, list) else rendered)["aggregates"]["by_cross"][0]
    assert list(first)[:4] == ["group", "across", "measure", "rows"]
    assert list(first["rows"][0]) == ["value", "cells"]
    assert list(first["rows"][0]["cells"][0]) == ["month", "count", "sum"]

    # And what the cross leaves out rides with the figures for this question.
    gap = sum((Decimal(r["revenue"]) for r in rows if not r["order_date"]), Decimal(0))
    figures = "\n".join(dataset.question_figures("monthly revenue by region", [up]))
    assert "- NOTE: the breakdown of region for each order_date: 41 row(s) have no date" in figures
    assert str(gap.quantize(Decimal("0.01"))) in figures


def test_the_model_is_never_shown_the_words_it_must_not_repeat(tmp_path):
    path = tmp_path / "sales.csv"
    _sales(path)
    up = _upload(path, "monthly revenue by region")
    words = dataset.PlainWords([up])
    said = words.feed("The by_cross section lists revenue for every pair.\n")
    assert "by_cross" not in said and "two-way breakdown" in said


def test_the_pdf_summary_is_put_into_plain_words_too(tmp_path):
    """Live, 2026-09-21: the summary of a PDF said "a revenue discrepancy of
    575900.39 in the monthly aggregates" — a field name in the copy that gets
    forwarded. The chat answer had been guarded since 2026-09-19."""
    path = tmp_path / "sales.csv"
    _sales(path)
    up = _upload(path, ASK)
    said = "137 rows are excluded from the by_month aggregates and the by_cross section."
    plain = report._plain_words(said, [up])
    for word in ("by_month", "by_cross", "aggregates"):
        assert word not in plain, plain
