"""Charts never count a column that names a row: people's names, contacts, ids.

THE FAILURE THIS PINS (hotfix 1.1 live replay, 2026-09-19). customers-100.csv
has no numeric column. Asked for "Plots on this docs", code chose "Records by
First Name" and "Records by Last Name": the file repeats 25 first names over
100 rows, so neither column is near-unique and both passed as categories. A
count of how many customers are called Linda says nothing. The composer's own
report drew the same two charts. And the report already charted Subscription
Date by month and Country, so the third added chart repeated one of them.

Rules pinned here: a person-name part, an e-mail, phone or website, and a row
id are never suggested and never drawn as a count; a chart the document
already has is not suggested again (a date column charted by month may still
get its per-year view); a chart the person already accepted in an earlier
version is not taken away by an edit.
"""
from __future__ import annotations

import asyncio
import random
from datetime import date, timedelta

from app.artifacts import chart_choice as CC
from app.artifacts import chart_data as CD
from app.artifacts import chart_spec as CS
from app.artifacts import edits as E
from app.artifacts import spec as S
from app.artifacts.compose import DataTable

HEADER = ["Index", "Customer Id", "First Name", "Last Name", "Company", "City", "Country", "Phone", "Email",
          "Subscription Date", "Website"]


def _customers(seed: int = 11) -> DataTable:
    """The replay's shape: names drawn from a short list (so they repeat),
    near-unique companies and cities, ~40 countries, 2020-01 .. 2022-05."""
    rng = random.Random(seed)
    first = ["Sheryl", "Preston", "Roy", "Linda", "Joanna", "Aimee", "Darren", "Brett", "Jeffrey", "Kristy", "Dakota",
             "Mariah", "Jose", "Priya", "Kenji", "Amara", "Luca", "Noor", "Tomas", "Ingrid", "Omar", "Mei", "Rafael", "Zoe"]
    last = ["Baxter", "Lozano", "Berry", "Mcgee", "Serrano", "Burch", "Rojas", "Cantu", "Graves", "Hoover", "Ochoa",
            "Wall", "Dunn", "Riley", "Kapoor", "Tanaka", "Okafor", "Moretti", "Haddad", "Novak"]
    countries = [f"Country {i}" for i in range(40)]
    sites = [f"https://www.site{i}.example/" for i in range(30)]
    rows = []
    for i in range(1, 101):
        f, l = rng.choice(first), rng.choice(last)
        rows.append([str(i), f"{rng.getrandbits(60):015x}", f, l, f"{l} Holdings {i}", f"Town {i}", rng.choice(countries),
                     f"+1-555-{1000 + i}", f"{f.lower()}.{l.lower()}{i}@example.com",
                     (date(2020, 1, 1) + timedelta(days=rng.randint(0, 881))).isoformat(), rng.choice(sites)])
    return DataTable(id="upload1", title="customers-100.csv", columns=HEADER, rows=rows, source_id="upload")


ROW_NAMING = {"Index", "Customer Id", "First Name", "Last Name", "Phone", "Email", "Website"}


def test_the_replay_table_really_repeats_its_names():
    """GUARD on the fixture: the failure needs names that are NOT near-unique."""
    t = _customers()
    for col in ("First Name", "Last Name"):
        values = [r[t.columns.index(col)] for r in t.rows]
        assert len(set(values)) <= 25, col


def test_suggestions_never_count_a_column_that_names_a_row():
    charts, _reasons = CC.suggest_charts(_customers(), limit=3)
    xs = [c.data.x for c in charts]
    assert xs, "the date and Country are still worth drawing"
    assert not ROW_NAMING & set(xs), xs
    assert xs[0] == "Subscription Date" and charts[0].data.date_bucket == "month"
    assert "Country" in xs
    for name in ("First Name", "Last Name", "Website", "Email", "Phone"):
        assert name in CC.skipped_columns(_customers()), name


def test_a_count_of_a_row_naming_column_is_not_drawn_even_when_it_repeats():
    t = _customers()
    for col in ("First Name", "Last Name", "Website"):
        chart = CS.Chart(type="horizontal_bar", title=f"Top {col}", data=CS.Binding(table_id="upload1", x=col, y=[], agg="count"))
        why = CC.not_worth_drawing(chart, t)
        assert col in why, (col, why)
    country = CS.Chart(type="horizontal_bar", title="By country", data=CS.Binding(table_id="upload1", x="Country", y=[], agg="count"))
    assert CC.not_worth_drawing(country, t) == ""
    # A real measure per person is a comparison, not a count of names.
    paid = DataTable(id="t", title="t", columns=["Customer Name", "Revenue"],
                     rows=[[n, v] for n, v in [("Ann", 5), ("Bo", 7), ("Cy", 2)] * 5])
    by_name = CS.Chart(type="bar", title="Revenue", data=CS.Binding(table_id="t", x="Customer Name", y=["Revenue"], agg="sum"))
    assert CC.not_worth_drawing(by_name, paid) == ""


def test_a_product_code_or_country_code_stays_a_category():
    """GUARD. The suggestion filter's id words ("code", "number") are too wide
    to REFUSE a chart someone bound: rows per product code is a comparison."""
    t = DataTable(id="t", title="t", columns=["Product Code", "Qty"],
                  rows=[[c, 1] for c in ["A1", "A1", "A1", "B2", "B2", "B2", "C3", "C3", "C3", "C3", "D4", "D4"]])
    chart = CS.Chart(type="bar", title="Rows per product", data=CS.Binding(table_id="t", x="Product Code", y=[], agg="count"))
    assert CC.not_worth_drawing(chart, t) == ""


def _report_with(charts):
    blocks = [{"type": "heading", "level": 1, "text": "Overview"}, {"type": "paragraph", "text": "One row per customer."}]
    for c in charts:
        blocks += [{"type": "heading", "level": 1, "text": c["title"]}, {"type": "chart", "chart": c}]
    blocks += [{"type": "heading", "level": 1, "text": "Recommendations"}, {"type": "paragraph", "text": "Keep going."}]
    return S.parse_body("document", {"title": "Customer Subscription Analysis", "blocks": blocks})


MONTHLY = {"type": "line", "title": "Monthly Subscription Volume",
           "data": {"table_id": "upload1", "x": "Subscription Date", "y": [], "agg": "count", "date_bucket": "month"}}
BY_COUNTRY = {"type": "horizontal_bar", "title": "Customer Distribution by Country",
              "data": {"table_id": "upload1", "x": "Country", "y": [], "agg": "count"}}


def test_adding_plots_does_not_repeat_a_chart_the_report_has_or_count_names():
    parent = _report_with([MONTHLY, BY_COUNTRY])
    ask = "also i want Plots on this docs"
    out = E.apply(parent, E.preplan(ask, parent), tables=[_customers()], instruction=ask)
    assert out.changed, out.not_applied
    added = [b.chart for b in out.spec.body.blocks if getattr(b, "type", "") == "chart"][2:]
    assert added, "a per-year view of the date is a new picture"
    keys = [(c.data.x, c.data.date_bucket) for c in added]
    assert ("Subscription Date", "month") not in keys and ("Country", None) not in keys, keys
    assert not ROW_NAMING & {c.data.x for c in added}, keys
    assert ("Subscription Date", "year") in keys, keys


def test_an_edit_keeps_a_chart_the_parent_already_drew():
    """An older version may hold a First Name chart drawn before this rule.
    Adding plots must not turn it into a "not drawn" note: that section was
    not named (hotfix 1.1 fix 1)."""
    from app.engines import artifact as engine

    table = _customers()
    names = {"type": "horizontal_bar", "title": "Top 10 Most Common First Names",
             "data": {"table_id": "upload1", "x": "First Name", "y": [], "agg": "count", "top_n": 10, "other_bucket": True}}
    drawn, _ = CD.resolve_spec(_report_with([names]), [table], accepted=[CD.binding_key(names["data"])])
    parent_block = [b for b in drawn.body.blocks if getattr(b, "type", "") == "chart"]
    assert parent_block and parent_block[0].chart.series, "the parent version drew it"

    fresh, _ = CD.resolve_spec(_report_with([names]), [table])
    assert not [b for b in fresh.body.blocks if getattr(b, "type", "") == "chart"], "a new report does not draw it"

    ask = "also i want Plots on this docs"
    child = E.apply(drawn, E.preplan(ask, drawn), tables=[table], instruction=ask).spec
    warned = []
    out = asyncio.run(engine._post_process(child, [table], warned.append, ask, parent=drawn, model_wrote=False))
    kept = [b.chart for b in out.body.blocks if getattr(b, "type", "") == "chart" and b.chart.data.x == "First Name"]
    assert kept and kept[0].series, warned
