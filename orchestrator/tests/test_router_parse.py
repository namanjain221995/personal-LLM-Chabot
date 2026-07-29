"""Router JSON parsing: valid outputs and garbage (spec §8)."""
from app.engines.router import ROUTES, parse_route


def test_plain_json():
    assert parse_route('{"route": "sql"}') == "sql"


def test_all_routes():
    for route in ROUTES:
        assert parse_route(f'{{"route": "{route}"}}') == route


def test_uppercase_route_value_normalized():
    assert parse_route('{"route": "REPORT"}') == "report"


def test_fenced_json():
    assert parse_route('```json\n{"route": "rag"}\n```') == "rag"


def test_json_with_surrounding_prose():
    assert parse_route('Sure! {"route": "vision"} hope that helps') == "vision"


def test_think_preamble_stripped():
    assert parse_route('<think>hmm, tables...</think>{"route": "sql"}') == "sql"


def test_whitespace_and_newlines():
    assert parse_route('  \n {"route":\n"report"}\n') == "report"


def test_garbage_returns_none():
    assert parse_route("banana") is None


def test_unknown_route_returns_none():
    assert parse_route('{"route": "sqll"}') is None


def test_wrong_key_returns_none():
    assert parse_route('{"path": "sql"}') is None


def test_empty_and_none_return_none():
    assert parse_route("") is None
    assert parse_route(None) is None
    assert parse_route(123) is None


def test_non_dict_json_returns_none():
    assert parse_route('["sql"]') is None
