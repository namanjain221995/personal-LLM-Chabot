"""Charts chosen by CODE from a table's own shape (chart_choice.suggest_charts).

WHY THIS EXISTS. "I want plot" over an uploaded customer list failed with
"The artifact has no charts to draw as images": the composer had written no
chart, and nothing downstream could look at the table and say what was worth
drawing. These tests pin what the chooser picks and — just as important —
what it refuses to pick: an e-mail address per row is not a chart, and a
column the person NAMED is never silently swapped for something else.

Every value asserted here is recounted independently from the fixture CSV, so
a chart that matched the wrong rows fails the test rather than passing it.
"""
from __future__ import annotations

import asyncio
import collections
import csv
import datetime as dt
from pathlib import Path

import pytest

from app.artifacts import chart_choice as CC
from app.artifacts import chart_data as CD
from app.artifacts import chart_spec as CS
from app.artifacts.compose import DataTable

FIXTURE = Path(__file__).parent / "fixtures" / "artifacts" / "customers_100.csv"


def _fixture_table(table_id: str = "upload1") -> DataTable:
    with FIXTURE.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    return DataTable(id=table_id, title="customers-100.csv", columns=rows[0],
                     rows=[[c if c != "" else None for c in r] for r in rows[1:]], source_id="upload")


def _column(table: DataTable, name: str) -> list:
    return [r[table.columns.index(name)] for r in table.rows]


def test_customer_table_suggestions_rank_dates_then_low_cardinality_and_skip_ids():
    table = _fixture_table()
    charts, reasons = CC.suggest_charts(table)

    first = charts[0]
    assert first.type == "line", "a date axis is a line, not a bar"
    assert first.data.x == "Subscription Date" and first.data.agg == "count" and first.data.y == []
    assert first.data.date_bucket == "month", "the span is under three years, so the rows are counted per month"
    assert "counted per month" in reasons[0]

    second = charts[1]
    assert second.data.x == "Country" and second.data.agg == "count"
    assert second.type in ("bar", "horizontal_bar")
    assert second.data.other_bucket is True

    bound = {c.data.x for c in charts}
    for never in ("Index", "Customer Id", "Email", "Phone 1", "Phone 2", "Website", "First Name", "City"):
        assert never not in bound, f"{never} names a row; it is not a category"


def test_the_suggested_charts_compute_the_numbers_an_independent_recount_gives():
    table = _fixture_table()
    charts, _ = CC.suggest_charts(table)

    by_country, _notes, msg = CD.resolve_chart(charts[1], [table])
    assert by_country is not None, msg
    counted = collections.Counter(_column(table, "Country"))
    assert list(by_country.categories) == [c for c, _n in counted.most_common()]
    assert [int(v) for v in by_country.series[0].values] == [n for _c, n in counted.most_common()]

    by_month, _notes, msg = CD.resolve_chart(charts[0], [table])
    assert by_month is not None, msg
    months = collections.Counter(dt.date.fromisoformat(v).strftime("%b %Y") for v in _column(table, "Subscription Date"))
    assert sum(int(v) for v in by_month.series[0].values) == len(table.rows) == sum(months.values())
    assert dict(zip(by_month.categories, (int(v) for v in by_month.series[0].values))) == dict(months)


def _many_category_table(n_categories: int = 24, rows_each: int = 4) -> DataTable:
    rows = [[f"Team {i:02d}", str(i * 10 + j)] for i in range(n_categories) for j in range(rows_each)]
    return DataTable(id="upload1", title="teams.csv", columns=["Team", "Score"], rows=rows)


def test_a_category_axis_over_15_values_folds_into_other_unless_the_person_named_top_n():
    table = _many_category_table()
    chart = CS.Chart(type="bar", title="Rows by Team", data=CS.Binding(table_id="upload1", x="Team", agg="count"))

    folded, note = CC.fold_long_tail(chart, table)
    assert folded is not None and folded.top_n == CC.TAIL_TOP_N and folded.other_bucket is True
    assert "24 values" in note and "9 largest" in note and "Other" in note

    theirs, their_note = CC.fold_long_tail(chart, table, "show the top 20 teams")
    assert theirs is not None and theirs.top_n == 20, "their own tail wins"
    assert "19 largest" in their_note

    already = chart.model_copy(update={"data": chart.data.model_copy(update={"top_n": 5})})
    assert CC.fold_long_tail(already, table) == (None, ""), "a binding that already has a tail is left alone"

    short = _many_category_table(n_categories=6)
    assert CC.fold_long_tail(chart, short) == (None, ""), "six bars read fine"

    # A DATE axis is bucketed before it is drawn, so 150 daily timestamps are
    # not 150 bars and must not be folded.
    import datetime as _dt

    daily = DataTable(id="upload1", title="sales.csv", columns=["Date", "Amount"],
                      rows=[[(_dt.date(2025, 1, 1) + _dt.timedelta(days=i)).isoformat(), str(i)] for i in range(150)])
    dated = CS.Chart(type="bar", title="Rows per day", data=CS.Binding(table_id="upload1", x="Date", agg="count"))
    assert CC.fold_long_tail(dated, daily) == (None, "")


def test_repair_binding_folds_the_tail_and_the_note_reaches_the_version():
    table = _many_category_table()
    chart = CS.Chart(type="bar", title="Rows by Team", data=CS.Binding(table_id="upload1", x="Team", agg="count"))
    fixed, notes = CD.repair_binding(chart, [table], "chart the teams")
    assert fixed.data.top_n == CC.TAIL_TOP_N
    assert any("9 largest" in n and "Other" in n for n in notes), notes
    drawn, _n, msg = CD.resolve_chart(fixed, [table])
    assert drawn is not None, msg
    # top_n is the category CAP and Other takes one of its slots.
    assert len(drawn.categories) == CC.TAIL_TOP_N and drawn.categories[-1] == CD.OTHER


def test_a_near_unique_column_is_not_charted_and_says_why():
    table = _fixture_table()
    emails = CS.Chart(type="bar", title="Customers by e-mail", data=CS.Binding(table_id="upload1", x="Email", agg="count"))
    drawn, notes, msg = CD.resolve_chart(emails, [table])
    assert drawn is None, "one bar per row is not a chart"
    assert "Email" in msg and "100 different values in 100 rows" in msg, msg
    assert notes and "Email" in notes[0]

    # …and a chart with a real measure over the same shape is NOT refused:
    # one row per product is how a product table looks.
    products = DataTable(id="upload2", title="products.csv", columns=["Product", "Revenue"],
                         rows=[[f"SKU-{i}", str(100 + i)] for i in range(20)])
    chart = CS.Chart(type="bar", title="Revenue by product",
                     data=CS.Binding(table_id="upload2", x="Product", y=["Revenue"], agg="sum"))
    kept, _n, msg = CD.resolve_chart(chart, [products])
    assert kept is not None, msg


def test_a_top_count_of_two_is_not_charted_and_says_why():
    table = DataTable(id="upload1", title="pairs.csv", columns=["Owner"],
                      rows=[[f"Person {i // 2}"] for i in range(24)])
    chart = CS.Chart(type="bar", title="Rows by owner", data=CS.Binding(table_id="upload1", x="Owner", agg="count"))
    drawn, _notes, msg = CD.resolve_chart(chart, [table])
    assert drawn is None
    assert "Owner" in msg and "same height" in msg, msg


def test_a_named_type_or_column_that_fails_to_bind_is_explained_not_replaced():
    """GUARD. Suggestions answer "draw something"; they must never answer
    "draw Country" with a chart of something else."""
    from app.engines import artifact as engine

    table = _fixture_table()
    assert engine._named_column("pie of Country please", [table]) == "Country"
    assert engine._named_column("just plot it", [table]) == ""

    warnings: list = []
    spec = _empty_document()
    same = asyncio.run(engine._suggest_charts_for_images(spec, [table], warnings.append, "pie chart of Country"))
    assert same is spec, "their request is not replaced by a suggestion"
    assert any("Country" in w and "customers-100.csv" in w for w in warnings), warnings
    assert any("Subscription Date" in w for w in warnings), "the sentence lists the table's columns"


def _empty_document():
    from app.artifacts import spec as S

    return S.load({"spec_version": 1, "kind": "document",
                   "document": {"title": "Customer Base", "blocks": [{"type": "paragraph", "text": "A hundred customers."}]}})


def test_a_png_request_whose_composer_wrote_no_chart_gets_suggested_charts_bound_to_upload1():
    from app.engines import artifact as engine

    table = _fixture_table()
    warnings: list = []
    out = asyncio.run(engine._suggest_charts_for_images(_empty_document(), [table], warnings.append, "I want plot"))
    charts = [b.chart for b in out.body.blocks if getattr(b, "type", "") == "chart"]
    assert charts, "a picture request with a table in hand always has something to draw"
    assert {c.data.table_id for c in charts} == {"upload1"}
    assert any("chosen from the data" in w for w in warnings)

    resolved, notes = CD.resolve_spec(out, [table])
    drawn = [b.chart for b in resolved.body.blocks if getattr(b, "type", "") == "chart"]
    assert len(drawn) == len(charts), notes
    counted = collections.Counter(_column(table, "Country"))
    country = next(c for c in drawn if c.data.x == "Country")
    assert [int(v) for v in country.series[0].values] == [n for _c, n in counted.most_common()]


def test_suggestions_are_empty_when_nothing_in_the_table_groups_rows():
    table = DataTable(id="upload1", title="ids.csv", columns=["Customer Id", "Email"],
                      rows=[[f"C{i:04d}", f"p{i}@example.test"] for i in range(40)])
    charts, reasons = CC.suggest_charts(table)
    assert charts == [] and reasons == []


def test_a_numeric_measure_is_suggested_last_and_totalled_by_the_category():
    table = DataTable(id="upload1", title="sales.csv", columns=["Region", "Amount"],
                      rows=[[r, str(10 + i)] for i, r in enumerate(["North", "South", "East"] * 6)])
    charts, reasons = CC.suggest_charts(table, limit=3)
    assert [c.data.x for c in charts] == ["Region", "Region"]
    assert charts[0].data.y == [] and charts[0].data.agg == "count"
    assert charts[1].data.y == ["Amount"] and charts[1].data.agg == "sum"
    assert "totalled by Region" in reasons[1]


@pytest.mark.parametrize("text,expected", [("show the top 10", 10), ("top 3 countries", 3), ("no tail here", None),
                                           ("top 99 rows", None)])
def test_the_persons_own_top_n_is_read_from_their_words(text, expected):
    assert CC.top_n_named(text) == expected
