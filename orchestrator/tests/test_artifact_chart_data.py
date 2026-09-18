"""chart_data: every number on a chart is computed by code from a bound table
and checked against ground truth computed independently (plain Python in
tests/fixtures/charts/make_fixtures.py)."""
from __future__ import annotations

import asyncio
import math
import time

import numpy as np
import pytest

from app.artifacts import chart_data as CD
from app.artifacts import chart_spec as CS
from app.artifacts import spec as S
from app.artifacts.compose import DataTable
from tests.fixtures.charts import loader

GT = loader.ground_truth()


@pytest.fixture(scope="module")
def tables():
    return {name: loader.table(name) for name in loader.FILES}


def chart(**raw):
    raw.setdefault("title", "t")
    return CS.Chart.model_validate(raw)


def values(c: CS.Chart, k: int = 0) -> dict:
    return dict(zip(c.categories, c.series[k].values))


def resolve(tables, **raw):
    c, notes, msg = CD.resolve_chart(chart(**raw), list(tables.values()))
    assert c is not None, msg
    return c, notes


# ------------------------------------------------------------ exact values --


def test_pie_of_status_is_exact(tables):
    c, _ = resolve(tables, type="pie", data=dict(table_id="upload_tickets", x="Status", agg="count"))
    assert values(c) == {"Resolved": 24, "Open": 14, "In Progress": 11, "Closed": 8, "On Hold": 3}
    assert c.categories == ["Resolved", "Open", "In Progress", "Closed", "On Hold"]
    assert c.caption == "Count of rows by Status · tickets.xlsx · 60 rows"


def test_monthly_totals_are_exact_and_in_date_order(tables):
    c, _ = resolve(tables, type="line", data=dict(table_id="upload_sales", x="Date", y=["Amount"]))
    assert c.categories == ["Jan 2026", "Feb 2026", "Mar 2026", "Apr 2026", "May 2026", "Jun 2026", "Jul 2026"]
    assert c.series[0].values == [64514, 71319, 74708, 58936, 63092, 77350, 23072]
    assert values(c) == GT["sales"]["amount_by_month"]
    assert c.provenance.date_bucket == "month" and c.provenance.rows_used == 150
    assert c.caption == "Sum of Amount by month · sales_daily.csv · 150 rows"


def test_stacked_quarter_region_totals_are_exact(tables):
    c, _ = resolve(tables, type="stacked_bar", data=dict(table_id="upload_sales", x="Date", date_bucket="quarter", group_by="Region", y=["Amount"],
                                                        filters=[dict(column="Date", op="lt", value="2026-07-01")]))
    got = {f"{cat}|{s.name}": v for s in c.series for cat, v in zip(c.categories, s.values)}
    assert got == GT["sales"]["amount_by_quarter_region_h1"]
    assert "filtered: Date < 2026-07-01" in c.caption and c.provenance.rows_used == 135


@pytest.mark.parametrize("raw,truth", [
    (dict(type="bar", data=dict(table_id="upload_sales", x="Region", y=["Amount"])), ("sales", "amount_by_region")),
    (dict(type="horizontal_bar", data=dict(table_id="upload_sales", x="Product", y=["Units"])), ("sales", "units_by_product")),
    (dict(type="bar", data=dict(table_id="upload_sales", x="Region", y=["Amount"], agg="avg")), ("sales", "avg_amount_by_region")),
    (dict(type="bar", data=dict(table_id="upload_sales", x="Date", agg="count")), ("sales", "count_by_month")),
    (dict(type="bar", data=dict(table_id="upload_tickets", x="Owner", y=["Hours"])), ("tickets", "hours_by_owner")),
    (dict(type="bar", data=dict(table_id="upload_tickets", x="Owner", y=["Score"], agg="mean")), ("tickets", "avg_score_by_owner")),
    (dict(type="line", data=dict(table_id="upload_tickets", x="Created", agg="count", date_bucket="month")), ("tickets", "tickets_by_month")),
    (dict(type="bar", data=dict(table_id="upload_tickets", x="Priority", agg="count", filters=[dict(column="Status", op="eq", value="open")])), ("tickets", "open_by_priority")),
    (dict(type="funnel", data=dict(table_id="upload_funnel", x="Stage", y=["Count"])), ("funnel", None)),
    (dict(type="bar", data=dict(table_id="upload_employees", x="Department", agg="count")), ("employees", "count_by_department")),
])
def test_aggregations_match_ground_truth(tables, raw, truth):
    c, _ = resolve(tables, **raw)
    expected = GT[truth[0]] if truth[1] is None else GT[truth[0]][truth[1]]
    got = values(c)
    assert set(got) == set(expected)
    for k, v in expected.items():
        assert math.isclose(got[k], v, rel_tol=1e-9, abs_tol=1e-9), k


def test_heatmap_counts_status_by_priority(tables):
    c, _ = resolve(tables, type="heatmap", data=dict(table_id="upload_tickets", x="Status", group_by="Priority", agg="count"))
    got = {f"{cat}|{s.name}": v for s in c.series for cat, v in zip(c.categories, s.values) if v}
    assert got == GT["tickets"]["status_priority_counts"]
    assert c.extra.heatmap_rows == [s.name for s in c.series]


def test_units_from_a_docx_table_and_combo_axes(tables):
    c, _ = resolve(tables, type="combo", data=dict(table_id="upload_units", x="Product", y=["Q1", "Q2"], y2=["Q4"]))
    assert c.categories == list(GT["units"])  # first-appearance order for a combo
    assert [s.name for s in c.series] == ["Q1", "Q2", "Q4"]
    assert c.series[2].axis == "secondary" and c.series[2].kind == "line" and c.series[0].kind == "bar"
    assert c.series[2].values == [q[3] for q in GT["units"].values()]


def test_scatter_trendline_equals_polyfit(tables):
    c, _ = resolve(tables, type="scatter", data=dict(table_id="upload_employees", x="Experience", y=["Salary"], trendline=True))
    xs, ys = GT["employees"]["experience"], GT["employees"]["salaries"]
    slope, intercept = np.polyfit(np.array(xs, float), np.array(ys, float), 1)
    tr = c.extra.trendlines[0]
    assert math.isclose(tr.slope, slope, rel_tol=1e-9) and math.isclose(tr.intercept, intercept, rel_tol=1e-9)
    gt = GT["employees"]["trend_salary_on_experience"]
    assert math.isclose(tr.slope, gt["slope"], rel_tol=1e-9) and math.isclose(tr.intercept, gt["intercept"], rel_tol=1e-9)
    assert 0.9 < tr.r2 <= 1.0
    assert c.series[0].x == [float(x) for x in xs] and c.series[0].values == [float(y) for y in ys]


def test_histogram_box_gantt_waterfall(tables):
    h, _ = resolve(tables, type="histogram", data=dict(table_id="upload_employees", y=["Salary"], bins=10))
    counts, edges = np.histogram(np.array(GT["employees"]["salaries"], float), bins=10)
    assert h.series[0].values == [float(x) for x in counts] and h.extra.bin_edges == [float(e) for e in edges]
    assert sum(h.series[0].values) == 80

    b, _ = resolve(tables, type="box", data=dict(table_id="upload_employees", x="Department", y=["Salary"]))
    rows = loader.table("employees").rows
    for box in b.extra.box:
        vals = np.array([r[3] for r in rows if r[1] == box.name], float)
        assert math.isclose(box.median, float(np.median(vals))) and box.n == len(vals)
        assert math.isclose(box.q1, float(np.percentile(vals, 25))) and box.whisker_low >= box.min

    g, _ = resolve(tables, type="gantt", data=dict(table_id="upload_projects", label="Task", start="Start", end="End"))
    assert dict(zip(g.categories, g.series[0].values)) == GT["projects"]
    assert g.extra.spans[0].start == "2026-01-05"  # 05-01-2026 read day-first

    w, _ = resolve(tables, type="waterfall", data=dict(table_id="upload_cashflow", x="Item", y=["Amount"]))
    assert values(w) == GT["cashflow"] and w.categories == [r[0] for r in loader.table("cashflow").rows]  # natural order kept


def test_pie_folds_to_seven_slices_and_other_keeps_the_total():
    rows = [[f"c{i}", i + 1] for i in range(12)]
    t = DataTable(id="paste1", title="pasted table", columns=["Cat", "N"], rows=rows)
    c, notes, _ = CD.resolve_chart(chart(type="pie", data=dict(table_id="paste1", x="Cat", y=["N"])), [t])
    assert len(c.categories) == 7 and c.categories[-1] == "Other"
    assert sum(c.series[0].values) == sum(r[1] for r in rows)
    assert any("at most 7 slices" in n for n in notes)
    assert c.caption.endswith("pasted table")
    # top_n with avg re-aggregates the folded rows (not a sum of averages).
    t2 = DataTable(id="paste2", title="p", columns=["Cat", "N"], rows=[["a", 10], ["a", 20], ["b", 5], ["c", 1], ["c", 3]])
    c2, _, _ = CD.resolve_chart(chart(type="bar", data=dict(table_id="paste2", x="Cat", y=["N"], agg="avg", top_n=2)), [t2])
    assert values(c2) == {"a": 15.0, "Other": 3.0}


# -------------------------------------------------------------------- dates --


def test_dates_dd_mm_mm_dd_and_ambiguous(tables):
    for name, order, note in (("dates_dmy", "dmy", False), ("dates_mdy", "mdy", False), ("dates_ambiguous", "dmy", True)):
        c, notes = resolve(tables, type="bar", data=dict(table_id=f"upload_{name}", x="Date", y=["Amount"], date_bucket="month"))
        assert values(c) == GT[name]["amount_by_month"], name
        assert c.provenance.date_order == order
        assert any("day-first" in n for n in notes) == note, (name, notes)


def test_explicit_date_order_wins(tables):
    c, _ = resolve(tables, type="bar", data=dict(table_id="upload_dates_ambiguous", x="Date", y=["Amount"], date_bucket="month", date_order="mdy"))
    assert c.provenance.date_order == "mdy" and values(c) != GT["dates_ambiguous"]["amount_by_month"]


def test_to_number_and_to_date_forms():
    cases = {"1,20,000": 120000, "₹ 2.5 lakh": 250000, "(1,234)": -1234, "40%": 40, "१२,३४५": 12345, "૧૦": 10, "3.2 cr": 32000000,
             "$1,234.50": 1234.5, "12k": 12000, "−7": -7, "abc": None, "1,2": None, "2026-01-02": None}
    for text, want in cases.items():
        assert CD.to_number(text) == want, text
    assert CD.to_date("13-02-2026").isoformat() == "2026-02-13"
    assert CD.to_date("02-13-2026", "mdy").isoformat() == "2026-02-13"
    assert CD.to_date("Mar 2026").isoformat() == "2026-03-01" and CD.to_date("5 Jan 2026").isoformat() == "2026-01-05"


# ------------------------------------------------------- binding failures --


def test_missing_column_becomes_a_callout_and_close_names_match_with_a_note(tables):
    c, notes, msg = CD.resolve_chart(chart(type="bar", data=dict(table_id="upload_sales", x="Zone", y=["Amount"])), list(tables.values()))
    assert c is None and msg == "The column 'Zone' was not found in sales_daily.csv."
    c, notes, _ = CD.resolve_chart(chart(type="bar", data=dict(table_id="upload_sales", x="region", y=["Amont"])), list(tables.values()))
    assert c is not None and any("read as 'Amount'" in n for n in notes)
    c, _, msg = CD.resolve_chart(chart(type="bar", data=dict(table_id="upload9", x="a")), list(tables.values()))
    assert c is None and "not available" in msg


def test_literal_series_in_a_new_spec_are_blocked(tables):
    """10 literal charts the model typed: none survives resolve_spec, each
    becomes a note callout; allow_literal keeps them (old spec.json)."""
    blocks = [{"type": "heading", "level": 1, "text": "Numbers"}]
    for i in range(10):
        blocks.append({"type": "chart", "chart": {"type": "bar", "title": f"Typed {i}", "categories": ["a", "b"], "series": [{"name": "s", "values": [i + 1, 12]}]}})
    spec = S.ArtifactSpec.model_validate({"kind": "document", "document": {"title": "Doc", "blocks": blocks}})
    out, notes = CD.resolve_spec(spec, list(tables.values()))
    kinds = [b.type for b in out.document.blocks]
    assert kinds.count("chart") == 0 and kinds.count("callout") == 10
    assert all("not bound to a table" in n for n in notes) and len(notes) == 10
    kept, _ = CD.resolve_spec(spec, list(tables.values()), allow_literal=True)
    assert [b.type for b in kept.document.blocks].count("chart") == 10


def test_model_typed_numbers_on_a_bound_chart_are_overwritten(tables):
    for i in range(10):
        typed = {"type": "pie", "title": f"s{i}", "data": {"table_id": "upload_tickets", "x": "Status", "agg": "count"},
                 "categories": ["Resolved", "Open"], "series": [{"name": "Count", "values": [99 + i, 1]}]}
        c, _, _ = CD.resolve_chart(CS.Chart.model_validate(typed), list(tables.values()))
        assert values(c) == GT["tickets"]["status_counts"]


def test_prompt_typed_figures_chart_through_a_prompt_table():
    prompts = [
        "plot sales by month as a line: Jan 10, Feb 12, Mar 15",
        "pie chart: Rent 40%, Food 25%, Travel 15%, Savings 20%",
        "north 120, south 95, east 130, west 80 bar chart banao",
        "x = 1,2,3,4 and y = 2,4,5,8 scatter with trend line",
        "(1,52) (2,55) (3,61) (4,70)",
        "Jan: १०\nFeb: १२\nMar: १५",
        "revenue by quarter Q1 1.2 lakh, Q2 1.5 lakh, Q3 90k",
        "sheet me chart do: Mumbai 1,20,000; Delhi 95,000; Pune 60,000",
        "bar chart with blue bars and a title: Alpha 30, Beta 45, Gamma 12",
        "ગ્રાફ બનાવો: Surat 40, Rajkot 25, Vadodara 35",
    ]
    for i, text in enumerate(prompts, start=1):
        t = CD.parse_prompt_data(text, index=i)
        assert t is not None and t.id == f"prompt{i}", text
        x, y = t.columns[0], t.columns[1]
        typ = "scatter" if t.columns == ["x", "y"] else "bar"
        c, notes, msg = CD.resolve_chart(chart(type=typ, data=dict(table_id=t.id, x=x, y=[y])), [t])
        assert c is not None, (text, msg)
        assert c.provenance.table_provenance == "prompt" and "figures typed in the request" in c.caption
        if typ == "bar":
            assert sorted(c.series[0].values) == sorted(r[1] for r in t.rows)


def test_month_names_keep_calendar_order_on_a_bar():
    t = CD.parse_prompt_data("sales: Mar 15, Jan 10, Apr 11, Feb 12")
    c, _, _ = CD.resolve_chart(chart(type="bar", data=dict(table_id=t.id, x="Month", y=[t.columns[1]])), [t])
    assert c.categories == ["Jan", "Feb", "Mar", "Apr"] and c.series[0].values == [10, 12, 15, 11]


def test_answer_tables_carry_provenance():
    md = "Findings\n\n| Region | Revenue |\n|---|---:|\n| North | 1,200 |\n| South | 950 |\n\nMore text.\n"
    (t,) = CD.tables_from_markdown(md)
    assert t.id == "answer1" and t.rows == [["North", 1200.0], ["South", 950.0]]
    c, _, _ = CD.resolve_chart(chart(type="bar", caption="Revenue split", data=dict(table_id="answer1", x="Region", y=["Revenue"])), [t])
    assert c.caption == "Revenue split (from the assistant's earlier answer)"


# ------------------------------------------------------------ engines, caps --


def test_duckdb_path_equals_pandas_path(tables, monkeypatch):
    raw = dict(type="stacked_bar", data=dict(table_id="upload_sales", x="Date", group_by="Region", y=["Amount"], agg="median"))
    pandas_chart, _ = resolve(tables, **raw)
    assert pandas_chart.provenance.engine == "pandas"
    monkeypatch.setattr(CD, "PANDAS_ROW_CAP", 10)
    duck_chart, _ = resolve(tables, **raw)
    assert duck_chart.provenance.engine == "duckdb"
    assert duck_chart.categories == pandas_chart.categories
    for a, b in zip(duck_chart.series, pandas_chart.series):
        assert a.name == b.name and all(math.isclose(x, y, rel_tol=1e-9) for x, y in zip(a.values, b.values))


def test_rows_past_the_cap_are_sampled_with_a_note(tables, monkeypatch):
    monkeypatch.setattr(CD, "DUCKDB_ROW_CAP", 100)
    c, notes = resolve(tables, type="bar", data=dict(table_id="upload_sales", x="Region", agg="count"))
    assert c.provenance.sampled and c.provenance.rows_used == 100 and sum(c.series[0].values) == 100
    assert any("uses the first 100" in n for n in notes) and "first 100 of 150 rows" in c.caption


def test_recompute_matches_detects_a_tampered_value(tables):
    c, _ = resolve(tables, type="line", data=dict(table_id="upload_sales", x="Date", y=["Amount"]))
    ok, diffs = CD.recompute_matches(c, list(tables.values()))
    assert ok and diffs == []
    bad = c.model_copy(update={"series": [c.series[0].model_copy(update={"values": [64515] + c.series[0].values[1:]})]})
    ok, diffs = CD.recompute_matches(bad, list(tables.values()))
    assert not ok and "64515" in diffs[0]
    literal = CS.Chart(type="bar", categories=["a"], series=[{"name": "s", "values": [1]}])
    assert CD.recompute_matches(literal, [])[0] is False


def test_resolve_spec_on_every_container_before_the_chart_swap(tables):
    """spec.py still carries the legacy Chart: resolved legacy-type charts
    keep their computed numbers; other types become a note."""
    spec = S.ArtifactSpec.model_validate({"kind": "document", "document": {"title": "Doc", "blocks": [
        {"type": "heading", "level": 1, "text": "Status"},
        {"type": "chart", "chart": {"type": "pie", "title": "Status", "categories": ["x"], "series": [{"name": "s", "values": [1]}]}},
    ]}})
    data = spec.model_dump(mode="python")
    data["document"]["blocks"][1]["chart"] = {"type": "pie", "title": "Status", "data": {"table_id": "upload_tickets", "x": "Status", "agg": "count"}}
    out, notes = CD.resolve_spec(data, list(tables.values()))
    assert out["document"]["blocks"][1]["chart"]["series"][0]["values"] == [24, 14, 11, 8, 3]
    deck = S.ArtifactSpec.model_validate({"kind": "presentation", "presentation": {"title": "D", "slides": [{"layout": "title", "title": "D"}]}}).model_dump(mode="python")
    deck["presentation"]["slides"].append({"layout": "chart", "title": "Box", "chart": {"type": "box", "title": "Salary", "data": {"table_id": "upload_employees", "x": "Department", "y": ["Salary"]}}})
    model_out, notes = CD.resolve_spec(S.ArtifactSpec.model_validate({**deck, "presentation": {**deck["presentation"], "slides": deck["presentation"]["slides"][:1]}}), [])
    assert isinstance(model_out, S.ArtifactSpec)
    wb = {"kind": "workbook", "workbook": {"title": "W", "sheets": [{"name": "Data", "columns": [{"name": "Team"}, {"name": "Score"}],
          "rows": [["A", 3], ["B", 5], ["A", 4]], "charts": [{"type": "bar", "title": "By team", "data": {"table_id": "", "x": "Team", "y": ["Score"]}}]}]}}
    out, _ = CD.resolve_spec(wb, [])
    ch = out["workbook"]["sheets"][0]["charts"][0]
    assert dict(zip(ch["categories"], ch["series"][0]["values"])) == {"A": 7.0, "B": 5.0}
    assert ch["provenance"]["table_provenance"] == "sheet"


def test_downgrade_keeps_legacy_types_and_notes_the_rest(tables):
    blocks = [{"type": "heading", "level": 1, "text": "x"}, {"type": "paragraph", "text": "y"},
              {"type": "chart", "chart": {"type": "heatmap", "title": "H", "data": {"table_id": "upload_tickets", "x": "Status", "group_by": "Priority", "agg": "count"}}},
              {"type": "chart", "chart": {"type": "bar", "title": "B", "data": {"table_id": "upload_sales", "x": "Region", "y": ["Amount"]}}}]
    resolved, _ = CD.resolve_spec({"title": "Doc", "blocks": blocks}, list(tables.values()))
    notes = CD._downgrade_to_legacy(resolved)
    validated = S.DocumentSpec.model_validate(resolved)
    assert [b.type for b in validated.blocks] == ["heading", "paragraph", "callout", "chart"]
    assert validated.blocks[3].chart.series[0].values == sorted(GT["sales"]["amount_by_region"].values(), reverse=True)
    assert len(notes) == 1


def test_resolve_spec_async_keeps_the_event_loop_free():
    """200,000 rows through the async wrapper: the loop's heartbeat never
    stalls past 100 ms while pandas works in the worker thread."""
    import random

    rng = random.Random(7)
    regions = ["North", "South", "East", "West"]
    rows = [[f"2026-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}", regions[i % 4], rng.randint(1, 999)] for i in range(200_000)]
    table = DataTable(id="upload1", title="big.csv", columns=["Date", "Region", "Amount"], rows=rows)
    spec = {"kind": "document", "document": {"title": "Big", "blocks": [
        {"type": "chart", "chart": {"type": "stacked_bar", "title": "Big", "data": {"table_id": "upload1", "x": "Date", "group_by": "Region", "y": ["Amount"]}}}]}}

    async def main():
        gaps = []
        done = asyncio.Event()

        async def heartbeat():
            last = time.perf_counter()
            while not done.is_set():
                await asyncio.sleep(0.01)
                now = time.perf_counter()
                gaps.append(now - last)
                last = now

        hb = asyncio.create_task(heartbeat())
        loop = asyncio.get_running_loop()
        loop.slow_callback_duration = 0.1
        started = time.perf_counter()
        out, notes = await CD.resolve_spec_async(spec, [table], timeout_s=120)
        elapsed = time.perf_counter() - started
        done.set()
        await hb
        return out, max(gaps), elapsed

    out, worst_gap, elapsed = asyncio.run(main())
    ch = out["document"]["blocks"][0]["chart"]
    assert sum(sum(s["values"]) for s in ch["series"]) == sum(r[2] for r in rows)
    assert worst_gap < 0.1, f"event loop stalled {worst_gap * 1000:.0f} ms (compute took {elapsed:.1f} s)"


def test_parse_prompt_data_accuracy_on_fifty_authored_strings():
    from tests.fixtures.chart_requests import PROMPT_DATA_CASES

    assert len(PROMPT_DATA_CASES) >= 50
    exact = 0
    misses = []
    for text, want in PROMPT_DATA_CASES:
        t = CD.parse_prompt_data(text)
        got = None if t is None else [tuple(r) for r in t.rows]
        if got == (None if want is None else [tuple(r) for r in want]):
            exact += 1
        else:
            misses.append((text, got, want))
    assert exact / len(PROMPT_DATA_CASES) >= 0.95, misses


# ------------------------------------------------ repairing a model binding --


def test_repair_binding_fills_a_named_second_dimension_and_drops_stray_fields(tables):
    tl = list(tables.values())
    stacked = chart(type="stacked_bar", title="Q1 and Q2 Sales by Region",
                    data=dict(table_id="upload_sales", x="Date", date_bucket="quarter", y2=["Amount"], label="Amount", size="Units", trendline=True,
                              filters=[dict(column="Date", op="lt", value="2026-07-01")]))
    fixed, notes = CD.repair_binding(stacked, tl, "stacked bar of q1 q2 sales by region")
    assert fixed.data.group_by == "Region" and fixed.data.y == ["Amount"] and fixed.data.y2 == []
    assert fixed.data.label is None and fixed.data.size is None and fixed.data.trendline is False and notes
    c, _, _ = CD.resolve_chart(fixed, tl)
    got = {f"{cat}|{s.name}": v for s in c.series for cat, v in zip(c.categories, s.values)}
    assert got == GT["sales"]["amount_by_quarter_region_h1"]
    heat = chart(type="heatmap", title="Tickets by Status and Priority", data=dict(table_id="upload_tickets", x="Status", y=["Priority"], agg="count"))
    fixed, _ = CD.repair_binding(heat, tl, "Heatmap of tickets by status and priority.")
    assert fixed.data.group_by == "Priority" and fixed.data.y == [] and fixed.data.agg == "count"
    # One dimension stays one dimension; two candidate columns are left alone.
    bar = chart(type="bar", title="Amount by region", data=dict(table_id="upload_sales", x="Region", y=["Amount"]))
    assert CD.repair_binding(bar, tl, "bar chart of amount by region")[0].data.group_by is None
    line = chart(type="line", title="Monthly", data=dict(table_id="upload_sales", x="Date", y=["Amount"]))
    assert CD.repair_binding(line, tl, "monthly sales by region and product")[0].data.group_by is None
    assert CD.repair_binding(line, tl, "monthly sales for each region")[0].data.group_by == "Region"


def test_chart_from_model_drops_a_bad_style_value_not_the_chart():
    c, notes = CS.chart_from_model({"type": "pie", "title": "x", "data": {"table_id": "t", "x": "S"}, "style": {"color": "pie", "font_family": "Comic; }", "legend_position": "top"}})
    assert c.style.color is None and c.style.font_family is None and c.style.legend_position == "top" and len(notes) == 2
    with pytest.raises(Exception):
        CS.chart_from_model({"type": "pie", "title": "x", "data": {"table_id": "../x"}})


def test_guided_schema_with_tables_enumerates_real_columns(tables):
    schema = CS.guided_schema(tables=[tables["sales"]])
    b = schema["$defs"]["Binding"]["properties"]
    assert b["table_id"]["enum"] == ["upload_sales"]
    assert b["x"]["enum"] == ["Date", "Region", "Product", "Units", "Amount"]
    assert b["y"]["items"]["enum"] == ["Units", "Amount"]
    assert list(b)[:4] == ["table_id", "x", "group_by", "y"]
    assert set(schema["$defs"]["ChartStyle"]["properties"]) <= set(CS.GUIDED_STYLE_FIELDS)
    assert {"type", "title", "data"} <= set(schema["required"])


# ------------------------------------------------ verifier cases 2026-09-15 --


def test_a_total_row_is_not_counted_twice():
    """A markdown answer table ends in **Total**: a pie with a Total slice
    counts every figure twice."""
    md = "| Item | Cost |\n|---|---|\n| Rent | 40 |\n| Food | 30 |\n| Travel | 30 |\n| **Total** | **100** |\n"
    t = CD.tables_from_markdown(md)[0]
    c, notes, msg = CD.resolve_chart(chart(type="pie", data={"table_id": "answer1", "x": "Item", "y": ["Cost"]}), [t])
    assert values(c) == {"Rent": 40.0, "Food": 30.0, "Travel": 30.0}
    assert c.provenance.rows_used == 3 and any("total row" in n for n in notes)
    # Asking for the Total row by a filter on x keeps it.
    c2, _, _ = CD.resolve_chart(chart(type="bar", data={"table_id": "answer1", "x": "Item", "y": ["Cost"], "filters": [{"column": "Item", "op": "eq", "value": "Total"}]}), [t])
    assert values(c2) == {"Total": 100.0}
    for label in ("Grand Total", "Sub-total", "Total (INR)", "कुल", "કુલ"):
        assert CD._is_total_label(label), label
    for label in ("Total cost", "All", "Sum insured"):
        assert not CD._is_total_label(label), label


def test_year_numbers_are_labelled_without_a_thousands_separator():
    t = DataTable(id="upload1", title="years.csv", columns=["Year", "Revenue"], rows=[[2022, 10], [2023, 14], [2024, 19]])
    c, _, _ = CD.resolve_chart(chart(type="bar", data={"table_id": "upload1", "x": "Year", "y": ["Revenue"]}), [t])
    assert c.categories == ["2022", "2023", "2024"]
    t2 = DataTable(id="upload2", title="sizes.csv", columns=["Size", "N"], rows=[[1000, 1], [25000, 2]])
    c2, _, _ = CD.resolve_chart(chart(type="bar", data={"table_id": "upload2", "x": "Size", "y": ["N"]}), [t2])
    assert c2.categories == ["1,000", "25,000"]


def test_lone_signs_and_currency_symbols_are_not_numbers_and_do_not_raise():
    for cell in ("-", "+", "₹", "$ ", "(-)", "₹ -", "%", "-%"):
        assert CD.to_number(cell) is None, cell
    t = DataTable(id="upload1", title="dash.csv", columns=["K", "V"], rows=[["a", "-"], ["b", 3], ["c", "₹"]])
    assert "upload1" in CS.prompt_guide("document", [t])


def test_na_tokens_are_blanks_not_a_reason_to_refuse_the_column():
    rows = [["North", 10], ["North", "N/A"], ["South", 30], ["South", "-"], ["East", 5], ["East", "#N/A"], ["West", 7]]
    t = DataTable(id="upload1", title="na.csv", columns=["Region", "Amount"], rows=rows)
    c, _, msg = CD.resolve_chart(chart(type="bar", data={"table_id": "upload1", "x": "Region", "y": ["Amount"], "agg": "avg"}), [t])
    assert c is not None, msg
    assert values(c) == {"South": 30.0, "North": 10.0, "West": 7.0, "East": 5.0}


def test_an_average_over_no_rows_is_called_out_not_silently_zero():
    rows = [["2026-01-05", "North", 10], ["2026-02-05", "North", 12], ["2026-01-05", "South", 30], ["2026-03-05", "South", 34], ["2026-03-05", "North", 14]]
    t = DataTable(id="upload1", title="gaps.csv", columns=["Month", "Region", "Temp"], rows=rows)
    c, notes, _ = CD.resolve_chart(chart(type="line", data={"table_id": "upload1", "x": "Month", "y": ["Temp"], "agg": "avg", "group_by": "Region", "date_bucket": "month"}), [t])
    assert any("South in Feb 2026" in n and "not a measured value" in n for n in notes), notes
    c2, notes2, _ = CD.resolve_chart(chart(type="line", data={"table_id": "upload1", "x": "Month", "y": ["Temp"], "agg": "sum", "group_by": "Region", "date_bucket": "month"}), [t])
    assert not any("not a measured value" in n for n in notes2)


def test_colour_keys_match_categories_and_series_case_insensitively(tables):
    c, notes = resolve(tables, type="pie", data={"table_id": "upload_tickets", "x": "Status", "agg": "count"},
                       style={"category_colors": {"open": "red", " resolved ": "green", "Nope": "blue"}})
    assert c.style.category_colors["Open"] == "#C62828" and c.style.category_colors["Resolved"] == "#3F8F4F"
    assert any("'Nope'" in n for n in notes)


def test_prose_with_numbers_is_not_a_table():
    for text in ("I have 3 kids and 2 dogs, can you help me plan a weekend?", "meeting at 10 and lunch at 1, make a doc",
                 "iPhone 15 vs iPhone 16 comparison doc"):
        assert CD.parse_prompt_data(text) is None, text
    t = CD.parse_prompt_data("north 1,20,000; south 95k; east 2 lakh")
    assert [tuple(r) for r in t.rows] == [("north", 120000.0), ("south", 95000.0), ("east", 200000.0)]


def test_describing_a_huge_date_table_is_cheap_on_the_event_loop():
    """prompt_guide/describe_table/guided_schema run inline in the composer;
    the dd-mm decision there reads an even sample, compute reads every row."""
    import datetime as dt

    base = dt.date(2020, 1, 1)
    rows = [[(base + dt.timedelta(days=i // 300)).strftime("%d-%m-%Y"), i % 97] for i in range(600_000)]
    t = DataTable(id="upload1", title="big.csv", columns=["Date", "Amount"], rows=rows)
    started = time.perf_counter()
    text = CS.prompt_guide("document", [t])
    CS.guided_schema(tables=[t])
    elapsed = time.perf_counter() - started
    assert "about 2020-01-01 to 2025-06-22" in text, text
    assert elapsed < 0.5, f"{elapsed:.2f} s"
    assert CD.infer_column(t, 0, full_scan=True).date_order == "dmy"


def test_chunked_arrays_equal_the_values_and_yield_the_gil():
    arr = CD._chunked_array((float(i) for i in range(100_003)), 100_003, "float64")
    assert arr.shape == (100_003,) and arr[0] == 0.0 and arr[-1] == 100_002.0
    assert CD._chunked_array(iter(()), 0, "int64").shape == (0,)


def test_duckdb_path_keeps_the_event_loop_free():
    """Above PANDAS_ROW_CAP (duckdb): building the frame from Python lists in
    one call held the GIL for seconds at 2.1M rows (3.8 s measured)."""
    rows = [[("North", "South", "East", "West")[i % 4], (i * 7919) % 1000] for i in range(450_000)]
    table = DataTable(id="upload1", title="big.csv", columns=["Region", "Amount"], rows=rows)
    spec = {"kind": "document", "document": {"title": "Big", "blocks": [
        {"type": "chart", "chart": {"type": "bar", "title": "Big", "data": {"table_id": "upload1", "x": "Region", "y": ["Amount"]}}}]}}
    import duckdb  # noqa: F401  (import once: a first import holds the GIL)

    async def main():
        gaps = []
        done = asyncio.Event()

        async def heartbeat():
            last = time.perf_counter()
            while not done.is_set():
                await asyncio.sleep(0.01)
                now = time.perf_counter()
                gaps.append(now - last)
                last = now

        hb = asyncio.create_task(heartbeat())
        out, _ = await CD.resolve_spec_async(spec, [table], timeout_s=120)
        done.set()
        await hb
        return out, max(gaps)

    out, worst = asyncio.run(main())
    ch = out["document"]["blocks"][0]["chart"]
    assert ch["provenance"]["engine"] == "duckdb"
    assert sum(ch["series"][0]["values"]) == sum(r[1] for r in rows)
    assert worst < 0.1, f"event loop stalled {worst * 1000:.0f} ms"


def test_a_pie_of_negative_values_is_refused_with_a_reason(tables):
    c, notes, msg = CD.resolve_chart(chart(type="pie", data={"table_id": "upload_cashflow", "x": "Item", "y": ["Amount"]}), list(tables.values()))
    assert c is None and "negative" in msg and "waterfall" in msg
    w, _, _ = CD.resolve_chart(chart(type="waterfall", data={"table_id": "upload_cashflow", "x": "Item", "y": ["Amount"]}), list(tables.values()))
    assert w is not None


def test_find_table_matches_a_title_without_its_extension():
    """The model binds to the file the person NAMED; the person drops the
    extension. Until 2026-09-17 "customers-100" found nothing and the chart
    became a "the table … is not available" callout (owner report)."""
    table = CD._make_table("upload1", "customers-100.csv", ["Country"], [["Aurelia"], ["Borovia"], ["Aurelia"]])
    assert CD._find_table("customers-100", [table])[0] is table
    assert CD._find_table("customers-100.csv", [table])[0] is table
    assert CD._find_table("Customers-100", [table])[0] is table
    assert CD._find_table("upload1", [table])[0] is table
    assert CD._find_table("orders", [table])[0] is None, "a different name is still a different table"

    c, _notes, msg = CD.resolve_chart(
        chart(type="bar", data={"table_id": "customers-100", "x": "Country", "agg": "count"}), [table])
    assert c is not None, msg
    assert list(c.categories) == ["Aurelia", "Borovia"] and [int(v) for v in c.series[0].values] == [2, 1]


def test_a_sheet_title_with_its_workbook_name_is_not_matched_by_the_stem():
    """GUARD. read_xlsx titles a sheet "tickets.xlsx · Tickets"; stripping a
    trailing extension must not make that equal to "tickets"."""
    sheet = CD._make_table("upload1", "tickets.xlsx · Tickets", ["Status"], [["Open"]])
    assert CD._find_table("tickets", [sheet])[0] is None
    assert CD._find_table("tickets.xlsx · Tickets", [sheet])[0] is sheet
