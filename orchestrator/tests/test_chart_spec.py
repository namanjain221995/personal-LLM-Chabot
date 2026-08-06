"""ChartSpec pydantic validation: good and bad specs (spec §8, wire shape §10)."""
import pytest
from pydantic import ValidationError

from app.core.chart_spec import ChartSpec, parse_chart_spec


def test_good_bar_spec():
    spec = ChartSpec(type="bar", x_key="stage", y_keys="total")
    assert spec.type == "bar"
    assert spec.y_keys == ["total"]
    assert spec.stacked is False


def test_good_multi_series_line():
    spec = ChartSpec(type="line", x_key="month", y_keys=["won", "lost"], title="Trend")
    assert spec.y_keys == ["won", "lost"]


def test_wire_dump_matches_section_10_wire_shape():
    """THE compatibility test.

    `wire_dump()` — not `model_dump()` — is the wire boundary. Optional
    fields added for the ECharts migration are emitted only when they are
    off their defaults, so a chart of one of the five original types
    serializes to exactly the five keys it always did. Every conversation
    already persisted was written in this shape and must keep rendering.
    """
    spec = ChartSpec(type="bar", x_key="month", y_keys=["created", "closed"], title="Cases")
    assert spec.wire_dump() == {
        "type": "bar",
        "x_key": "month",
        "y_keys": ["created", "closed"],
        "title": "Cases",
        "stacked": False,
    }


def test_model_dump_carries_the_new_optional_fields():
    """model_dump() is the full object; wire_dump() is the payload. Keeping
    them different is what lets the spec grow without a wire change."""
    spec = ChartSpec(type="bar", x_key="month", y_keys=["created"])
    assert spec.model_dump() == {
        "type": "bar",
        "x_key": "month",
        "y_keys": ["created"],
        "title": "",
        "stacked": False,
        "bins": None,
        "show_legend": True,
        "show_values": False,
    }


def test_legacy_x_y_aliases_accepted_but_not_emitted():
    spec = parse_chart_spec('{"type": "bar", "x": "stage", "y": "total"}')
    assert spec is not None
    dumped = spec.model_dump()
    assert dumped["x_key"] == "stage" and dumped["y_keys"] == ["total"]
    assert "x" not in dumped and "y" not in dumped


def test_bad_type_rejected():
    with pytest.raises(ValidationError):
        ChartSpec(type="hologram", x_key="a", y_keys="b")


def test_extra_fields_forbidden():
    with pytest.raises(ValidationError):
        ChartSpec(type="bar", x_key="a", y_keys="b", command="rm -rf /")


def test_empty_x_rejected():
    with pytest.raises(ValidationError):
        ChartSpec(type="bar", x_key="  ", y_keys="b")


def test_empty_y_list_rejected():
    with pytest.raises(ValidationError):
        ChartSpec(type="bar", x_key="a", y_keys=[])


# --- parse_chart_spec: model output is parsed/validated, never executed -----

def test_parse_valid_json_string():
    spec = parse_chart_spec('{"type": "bar", "x_key": "stage", "y_keys": ["total"]}')
    assert isinstance(spec, ChartSpec)


def test_parse_fenced_json():
    spec = parse_chart_spec('```json\n{"type": "pie", "x_key": "stage", "y_keys": "n"}\n```')
    assert spec is not None and spec.type == "pie"


def test_parse_garbage_returns_none():
    assert parse_chart_spec("not a chart at all") is None


def test_parse_invalid_spec_returns_none():
    assert parse_chart_spec('{"type": "bar"}') is None  # missing x_key/y_keys
    assert parse_chart_spec('{"type": "nope", "x_key": "a", "y_keys": "b"}') is None
    assert parse_chart_spec('{"type": "bar", "x_key": "a", "y_keys": "b", "z": 1}') is None


def test_parse_non_dict_returns_none():
    assert parse_chart_spec('["bar", "a", "b"]') is None
    assert parse_chart_spec(42) is None
    assert parse_chart_spec(None) is None


def test_columns_membership_enforced():
    good = parse_chart_spec(
        '{"type": "bar", "x_key": "stage", "y_keys": "total"}', columns=["stage", "total"]
    )
    assert good is not None
    bad_x = parse_chart_spec(
        '{"type": "bar", "x_key": "missing", "y_keys": "total"}', columns=["stage", "total"]
    )
    assert bad_x is None
    bad_y = parse_chart_spec(
        '{"type": "line", "x_key": "stage", "y_keys": ["total", "ghost"]}',
        columns=["stage", "total"],
    )
    assert bad_y is None


def test_stacked_flag_parsed():
    spec = parse_chart_spec(
        '{"type": "bar", "x_key": "month", "y_keys": ["a", "b"], "stacked": true}'
    )
    assert spec is not None and spec.stacked is True
