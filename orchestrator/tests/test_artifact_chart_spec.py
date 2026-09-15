"""Chart v2 (artifacts/chart_spec.py): a superset of spec.Chart, a binding the
model writes, output fields only code fills, and style values that are
validated before any renderer sees them."""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.artifacts import chart_spec as CS
from app.artifacts import spec as S


def bound(**over):
    base = dict(type="bar", title="Sales", data=dict(table_id="upload1", x="Region", y=["Amount"]))
    base.update(over)
    return CS.Chart.model_validate(base)


def test_every_legacy_chart_loads_as_v2_and_goes_back():
    for kind in ("bar", "horizontal_bar", "line", "pie"):
        old = S.Chart(type=kind, title="t", categories=["a", "b"], series=[S.Series(name="s", values=[1, 2])], y_label="y", caption="c")
        v2 = CS.Chart.model_validate(old.model_dump())
        assert v2.is_literal and not v2.is_resolved
        back = CS.to_legacy(v2)
        assert back == old


def test_a_chart_needs_a_binding_or_literal_numbers():
    with pytest.raises(ValidationError, match="data binding"):
        CS.Chart(type="bar", title="empty")
    assert bound().data.table_id == "upload1"
    # A literal pie still needs exactly one series; all-zero literal refused like spec.Chart.
    with pytest.raises(ValidationError):
        CS.Chart(type="pie", categories=["a"], series=[{"name": "x", "values": [1]}, {"name": "y", "values": [2]}])
    with pytest.raises(ValidationError, match="every value is 0"):
        CS.Chart(type="bar", categories=["a"], series=[{"name": "x", "values": [0]}])


def test_twenty_types_and_the_tiers():
    assert len(CS.CHART_TYPES) == 20 and len(set(CS.CHART_TYPES)) == 20
    assert set(CS.TIER1_TYPES) | set(CS.TIER2_TYPES) == set(CS.CHART_TYPES)
    for alias, canonical in (("doughnut", "donut"), ("Box Plot", "box"), ("dual-axis", "combo"), ("timeline", "gantt"), ("column", "bar")):
        assert bound(type=alias).type == canonical


def test_guided_schema_never_asks_for_numbers():
    schema = CS.guided_schema()
    props = schema["properties"]
    for field in ("categories", "series", "extra", "provenance"):
        assert field not in props
    assert "data" in schema["required"]
    text = json.dumps(schema)
    assert '"values"' not in text
    full = CS.Chart.model_json_schema()["properties"]
    assert all(full[f].get("readOnly") for f in ("categories", "series", "extra", "provenance"))
    # strip_output_fields works on any schema embedding a chart.
    nested = {"properties": {"chart": CS.Chart.model_json_schema()}}
    stripped = CS.strip_output_fields(nested)
    assert "series" not in stripped["properties"]["chart"]["properties"]


@pytest.mark.parametrize("name,hex_", [
    ("dark blue", "#1F3864"), ("neela", "#2F6FB2"), ("नीला", "#2F6FB2"), ("વાદળી", "#2F6FB2"), ("#abc", "#AABBCC"),
    ("1f3864", "#1F3864"), ("लाल", "#C62828"), ("ઘેરો વાદળી", "#1F3864"),
])
def test_colour_names_in_three_scripts(name, hex_):
    assert CS.resolve_color(name) == hex_


def test_style_rejects_unknown_colours_and_fonts_outside_the_allowlist():
    ok = bound(style={"color": "blue", "font_family": "calibri", "series_colors": {"Amount": "gehra neela"}})
    assert ok.style.color == "#2F6FB2" and ok.style.font_family == "Calibri" and ok.style.series_colors["Amount"] == "#1F3864"
    for bad in ({"color": "bed"}, {"font_family": "Georgia; } @import url(x)"}, {"title": {"font_family": "x</style>"}}, {"width_in": 40}, {"dpi": 2000}, {"y_min": float("inf")}):
        with pytest.raises(ValidationError):
            bound(style=bad)


def test_filters_are_bounded():
    with pytest.raises(ValidationError):
        bound(data={"table_id": "t", "x": "a", "filters": [{"column": "a", "value": "x" * 101}]})
    with pytest.raises(ValidationError):
        bound(data={"table_id": "t", "x": "a", "filters": [{"column": "a", "op": "gt", "value": float("nan")}]})
    with pytest.raises(ValidationError):
        bound(data={"table_id": "t", "x": "a", "filters": [{"column": "a"}] * 6})
    with pytest.raises(ValidationError):
        bound(data={"table_id": "../etc/passwd", "x": "a"})
    assert bound(data={"table_id": "t", "x": "a", "agg": "mean", "date_bucket": "monthly"}).data.agg == "avg"


def test_number_format_is_a_closed_enum_with_aliases():
    assert bound(style={"number_format": "INR"}).style.number_format == "currency_INR"
    assert bound(style={"number_format": "percentage"}).style.number_format == "percent"
    with pytest.raises(ValidationError):
        bound(style={"number_format": "#,##0;[Red]"})


def test_apply_patch_style_keeps_values_binding_change_clears_them():
    resolved = bound().model_copy(update={"categories": ["N", "S"], "series": [CS.Series(name="Amount", values=[1, 2])],
                                          "provenance": CS.Provenance(table_id="upload1")})
    styled = CS.apply_patch(resolved, CS.ChartPatch(style={"color": "red", "legend_position": "top"}, title="New"))
    assert styled.series[0].values == [1, 2] and styled.style.color == "#C62828" and styled.style.legend_position == "top" and styled.title == "New"
    rebound = CS.apply_patch(resolved, CS.ChartPatch(data={"agg": "avg"}))
    assert rebound.series == [] and rebound.provenance is None and rebound.data.agg == "avg"
    retyped = CS.apply_patch(resolved, CS.ChartPatch(type="pie"))
    assert retyped.series == []


def test_container_support_declares_tiers():
    for fmt in ("xlsx", "pptx"):
        for t in CS.TIER1_TYPES:
            assert CS.support_for(fmt, t) == "native", (fmt, t)
        for t in ("box", "heatmap", "waterfall", "funnel", "gantt"):
            assert CS.support_for(fmt, t) == "image"
    for fmt in ("docx", "pdf", "png", "svg"):
        assert all(CS.support_for(fmt, t) == "image" for t in CS.CHART_TYPES)


def test_prompt_guide_lists_tables_and_the_rule():
    from app.artifacts.compose import DataTable

    t = DataTable(id="upload1", title="tickets.xlsx", columns=["Status", "Hours", "Created"],
                  rows=[["Open", 2, "2026-01-02"], ["Closed", 3.5, "2026-01-05"], ["Open", 1, "2026-02-01"]])
    guide = CS.prompt_guide("document", [t])
    assert 'upload1 "tickets.xlsx" (3 rows)' in guide
    assert "Status (text: Open, Closed)" in guide and "Hours (number)" in guide and "Created (date, 2026-01-02 to 2026-02-01)" in guide
    assert "never write `categories`, `series` or any number" in guide
    assert "no table" in CS.prompt_guide("document", [])


def test_find_charts_by_index_title_and_slide():
    doc = {"kind": "document", "document": {"blocks": [
        {"type": "heading", "level": 1, "text": "A"},
        {"type": "chart", "chart": {"type": "bar", "title": "Revenue by month"}},
        {"type": "chart", "chart": {"type": "pie", "title": "Status mix"}},
    ]}}
    assert [p for p, _ in CS.find_charts(doc, CS.ChartRef(index=2))] == [("blocks", 2, "chart")]
    assert [p for p, _ in CS.find_charts(doc, CS.ChartRef(title="revenue by mnth"))] == [("blocks", 1, "chart")]
    assert [p for p, _ in CS.find_charts(doc, CS.ChartRef(type="pie"))] == [("blocks", 2, "chart")]
    deck = {"slides": [{"title": "x"}, {"chart": {"type": "line", "title": "t"}}]}
    assert [p for p, _ in CS.find_charts(deck, CS.ChartRef(slide=2))] == [("slides", 1, "chart")]
