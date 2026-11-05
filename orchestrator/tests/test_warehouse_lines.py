"""What the engine says about a statement that ran, on every route that runs one.

A wrong number is worse than an honest "could not find", and a true number under a false
line is close behind: "every row" over a LIMIT, "where" over a LEFT JOIN's condition,
"first 10 of 47" over rows 11 to 20. Each test pins one such line, or a route that ended a
turn the wrong way. Real DuckDB views, a fake writer, no model. Every value is invented.
"""
import asyncio
import json
import os
import random
import shutil
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import duckdb
import pytest

from app import admission, db, resilience, sse
from app.artifacts import chart_data, derived
from app.artifacts import spec as artifact_spec
from app.artifacts.compose import DataTable
from app.config import settings
from app.continuity import LeaseLost, QueuedForRecovery
from app.core import exports
from app.core.schema_cache import schema_cache
from app.engines import agent, artifact, crawl, report
from app.engines import sql as sql_engine
from app.engines import url as url_engine
from app.engines.sql import _counted_line, _no_date_line, deterministic_summary, generate_and_run_sql, run_sql_engine
from tests.test_warehouse_answers import (  # noqa: F401 — `warehouse` and `the_org` are fixtures
    COULD_NOT_FIND,
    COULD_NOT_WRITE,
    ORG_LINKS,
    PERSON_ONE,
    AgentModel,
    Model,
    Rec,
    _add,
    _text,
    ask,
    ask_agent,
    run_step,
    sql_step,
    the_org,
    warehouse,
)

ZERO = "figure came back 0"
NOTHING = "no figure came back"


def schema():
    return schema_cache.get(settings.duckdb_path)


def scope(text):
    return text.rsplit("\n\n", 1)[-1]


# ── Scope: a join's condition, and what kind of join it is ───────────────────


RT = "JOIN RecordType rt ON i.RecordTypeId = rt.Id AND rt.Name = 'Interview'"


def test_a_left_joins_condition_is_no_filter_that_ran(warehouse, monkeypatch):
    Model(monkeypatch, f"SELECT COUNT(*) AS n FROM Interview__c i LEFT {RT}")
    text, meta = ask("how many interviews are there?")
    assert meta["data"] == [{"n": 20}]
    assert "where" not in scope(text)
    assert "RecordType.Name = 'Interview' only limits what was joined" in scope(text)
    assert "every row" not in scope(text)
    assert "record type" not in scope(text)


def test_the_inner_join_with_the_same_words_gets_another_line(warehouse, monkeypatch):
    Model(monkeypatch, f"SELECT COUNT(*) AS n FROM Interview__c i {RT}")
    text, meta = ask("how many interviews are there?")
    assert meta["data"] == [{"n": 15}]
    assert "where RecordType.Name = 'Interview'" in scope(text)
    assert "only limits" not in scope(text)


def test_a_figure_read_from_the_joined_table_says_no_every_row_either(warehouse, monkeypatch):
    Model(monkeypatch, f"SELECT COUNT(rt.Id) AS n FROM Interview__c i LEFT {RT}")
    text, meta = ask("how many interviews are there?")
    assert meta["data"] == [{"n": 15}]
    assert "every row" not in scope(text)
    assert "record type" not in scope(text)


def test_each_on_clause_is_read_for_its_own_join(warehouse, monkeypatch):
    statement = (
        f"SELECT COUNT(*) AS n FROM Interview__c i {RT} "
        "LEFT JOIN Department__c d ON i.Round__c = d.Id AND d.Name = 'Sales'"
    )
    Model(monkeypatch, statement)
    text, _meta = ask("how many interviews are there?")
    assert "where RecordType.Name = 'Interview'" in scope(text)
    assert "Department__c.Name = 'Sales' only limits what was joined" in scope(text)
    assert "rows with no match are left out" not in scope(text)


@pytest.mark.parametrize("word", ["ANTI", "RIGHT", "FULL"])
def test_a_join_that_changes_whose_rows_these_are_claims_nothing_about_them(warehouse, monkeypatch, word):
    Model(monkeypatch, f"SELECT COUNT(*) AS n FROM Interview__c i {word} {RT}")
    text, _meta = ask("how many interviews are there?")
    assert "every row" not in scope(text)
    assert "record type" not in scope(text)


def test_an_anti_join_says_what_it_keeps(warehouse, monkeypatch):
    Model(monkeypatch, "SELECT COUNT(*) AS n FROM Interview__c i ANTI JOIN Department__c d ON i.Round__c = d.Id")
    text, _meta = ask("how many interviews are there?")
    assert "only rows with no match are kept" in scope(text)
    assert "are left out" not in scope(text)


def test_a_mixed_statement_does_not_call_a_left_join_inner(warehouse, monkeypatch):
    statement = (
        "SELECT COUNT(*) AS n FROM Interview__c i JOIN RecordType rt ON i.RecordTypeId = rt.Id "
        "LEFT JOIN Department__c d ON i.Round__c = d.Id"
    )
    Model(monkeypatch, statement)
    text, _meta = ask("how many interviews are there?")
    assert "rows with no match are left out" not in scope(text)


def test_a_plain_left_join_keeps_every_row(warehouse, monkeypatch):
    Model(monkeypatch, "SELECT COUNT(*) AS n FROM Interview__c i LEFT JOIN Department__c d ON i.Round__c = d.Id")
    text, _meta = ask("how many interviews are there?")
    assert "Scope: every row of Interview__c, joined to Department__c." in scope(text)


@pytest.fixture()
def staff(warehouse):
    _add(
        warehouse,
        "Staff__c",
        {**_text("Id", "Name", "Interview__c", "Owner__c", "Boss__c"), "Is_Active__c": "BOOLEAN", "Joined__c": "DATE"},
        [("a5SXx0000001AAAIA1", "One", "a0IXx0000001AAAIA1", "x", "x", "true", "2026-05-05")],
    )
    return warehouse


@pytest.mark.parametrize(
    "condition",
    [
        "s.Is_Active__c = true",
        "s.Is_Active__c = TRUE",
        "s.Is_Active__c = False",
        "true = s.Is_Active__c",
        "s.Joined__c = current_date",
        "s.Owner__c = NULL",
        "s.Owner__c = s.Boss__c",
    ],
)
def test_a_filter_shaped_like_a_key_is_named(staff, monkeypatch, condition):
    Model(monkeypatch, f"SELECT COUNT(*) AS n FROM Interview__c i JOIN Staff__c s ON i.Id = s.Interview__c AND {condition}")
    text, _meta = ask("how many interviews are there?")
    assert condition.replace("s.", "Staff__c.") in scope(text), scope(text)


def test_a_true_on_a_left_join_is_no_every_row(staff, monkeypatch):
    Model(monkeypatch, "SELECT COUNT(s.Id) AS n FROM Interview__c i LEFT JOIN Staff__c s ON i.Id = s.Interview__c AND s.Is_Active__c = true")
    text, _meta = ask("how many interviews are there?")
    assert "every row" not in scope(text)
    assert "Staff__c.Is_Active__c = true only limits what was joined" in scope(text)


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT COUNT(*) AS n FROM Interview__c JOIN RecordType rt ON RecordTypeId = rt.Id",
        'SELECT COUNT(*) AS n FROM Interview__c i JOIN RecordType rt ON "i"."RecordTypeId" = "rt"."Id"',
    ],
)
def test_a_real_key_stays_a_key(warehouse, monkeypatch, statement):
    Model(monkeypatch, statement)
    text, _meta = ask("how many interviews are there?")
    assert "where" not in scope(text)
    assert "That covers every Interview__c record type" in scope(text)


# ── Scope: what a statement that dropped rows cannot say ─────────────────────


DROPS_ROWS = [
    "WITH t AS (SELECT Status__c FROM Interview__c LIMIT 10) SELECT Status__c, COUNT(*) AS n FROM t GROUP BY 1",
    "SELECT COUNT(*) AS n FROM (SELECT Status__c FROM Interview__c LIMIT 5) t",
    "SELECT COUNT(*) AS n FROM (SELECT Id FROM Interview__c OFFSET 10) t",
    "SELECT COUNT(*) AS n FROM (SELECT Id FROM Interview__c FETCH FIRST 5 ROWS ONLY) t",
]


@pytest.mark.parametrize("statement", DROPS_ROWS)
def test_a_cut_inside_the_statement_is_not_every_row(warehouse, monkeypatch, statement):
    Model(monkeypatch, statement)
    text, meta = ask("how many interviews are there?")
    assert "every row" not in scope(text)
    assert "record type" not in scope(text)
    assert "can have left rows out" in scope(text)
    assert "assumptions" not in meta
    assert meta["salesforce_sources"]["truncated"] is True


@pytest.mark.parametrize("statement", DROPS_ROWS)
def test_the_agent_route_says_the_same_of_it(warehouse, monkeypatch, statement):
    AgentModel(monkeypatch, [sql_step()], statement)
    text, meta = ask_agent("how many interviews are there?")
    assert "every row" not in text
    assert meta["salesforce_sources"]["truncated"] is True


def test_an_asked_for_cut_is_not_every_row_and_no_cap(warehouse, monkeypatch):
    Model(monkeypatch, "SELECT Name, Status__c FROM Interview__c LIMIT 5")
    text, meta = ask("Show me 5 interviews")
    assert "every row" not in scope(text)
    assert "record type" not in scope(text)
    assert "assumptions" not in meta
    assert meta["salesforce_sources"]["truncated"] is False
    assert "can have left rows out" not in text, "nothing says this LIMIT cut anything"


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT Status__c, COUNT(*) AS n FROM Interview__c GROUP BY 1 ORDER BY 2 DESC LIMIT 2",
        "WITH t AS (SELECT Status__c, COUNT(*) AS n FROM Interview__c GROUP BY 1 ORDER BY 2 DESC LIMIT 2) SELECT * FROM t",
    ],
)
def test_a_top_n_of_finished_figures_read_every_row(warehouse, monkeypatch, statement):
    Model(monkeypatch, statement)
    text, meta = ask("what are the top 2 statuses by interviews?")
    assert "Scope: every row of Interview__c" in scope(text)
    assert meta["assumptions"]
    assert meta["salesforce_sources"]["truncated"] is False


def test_a_pick_inside_a_condition_cut_nothing(warehouse, monkeypatch):
    Model(monkeypatch, "SELECT COUNT(*) AS n FROM Interview__c WHERE Status__c = (SELECT Status__c FROM Interview__c WHERE Status__c IS NOT NULL LIMIT 1)")
    _text_answer, meta = ask("how many interviews have that status?")
    assert meta["salesforce_sources"]["truncated"] is False


def test_a_column_called_sample_is_no_sample(warehouse, monkeypatch):
    Model(monkeypatch, "SELECT Status__c AS sample, COUNT(*) AS n FROM Interview__c GROUP BY 1")
    text, meta = ask("how many interviews per status?")
    assert "Scope: every row of Interview__c" in scope(text)
    assert meta["salesforce_sources"]["truncated"] is False


@pytest.mark.parametrize("sampled", ["USING SAMPLE 50%", "TABLESAMPLE 10%"])
def test_a_sample_gives_no_number(warehouse, monkeypatch, sampled):
    """Its count changes on every run."""
    statement = f"SELECT COUNT(*) AS n FROM Interview__c {sampled}"
    model = Model(monkeypatch, statement, statement, statement)
    text, meta = ask("how many interviews are there?")
    assert COULD_NOT_WRITE in text
    assert "data" not in meta
    assert "different number on every run" in model.writer_user(1)


# ── Reused aliases ───────────────────────────────────────────────────────────


REUSED = (
    "WITH x AS (SELECT COUNT(*) AS n FROM Step__c t WHERE t.Status__c = 'Not Started'), "
    "y AS (SELECT COUNT(*) AS m FROM Plan__c t WHERE t.Status__c = 'Active') SELECT n, m FROM x, y"
)


@pytest.fixture()
def reused(warehouse):
    _add(warehouse, "Step__c", _text("Id", "Status__c"), [("a", "Not Started"), ("b", "Not Started"), ("c", "Done")])
    _add(warehouse, "Plan__c", _text("Id", "Status__c"), [("a", "Active"), ("b", "Paused")])
    return warehouse


def test_an_alias_two_tables_share_claims_no_stored_value(reused, monkeypatch):
    model = Model(monkeypatch, REUSED)
    _text_answer, meta = ask("how many steps are not started and plans active?")
    assert meta["data"] == [{"n": 2, "m": 1}]
    assert len(model.writer_prompts) == 1


def test_one_table_read_twice_under_one_alias_is_still_checked(reused):
    statement = "SELECT 1 FROM Step__c t WHERE t.Status__c = 'Actve' UNION ALL SELECT 1 FROM step__c t WHERE t.Status__c = 'Done'"
    assert "Actve" in " ".join(sql_engine._literal_misses(statement, schema()))


def test_an_alias_two_tables_share_claims_no_blank_rows(reused):
    assert sql_engine._null_drops(REUSED.replace("= 'Not Started'", "!= 'Done'"), ["n", "m"], "how many steps?", schema()) == []


def test_a_scope_line_names_no_table_it_cannot_be_sure_of(reused):
    statement = "SELECT COUNT(*) AS n FROM Step__c t WHERE t.Status__c = 'Done' AND t.Id IN (SELECT t.Id FROM Plan__c t)"
    assert "Plan__c.Status__c" not in _counted_line(statement, schema())


def test_a_binder_error_lists_no_columns_of_the_wrong_table(reused):
    error = 'Binder Error: Table "t" does not have a column named "Nope__c"'
    assert "COMPLETE column list" not in sql_engine._enriched_error(error, REUSED, schema())


def test_a_column_the_statement_made_itself_is_not_the_stored_one(reused):
    statement = (
        "SELECT COUNT(*) AS n FROM (SELECT CASE WHEN p.Status__c = 'Active' THEN 'Open' ELSE 'Closed' END AS Status__c "
        "FROM Plan__c p) WHERE Status__c = 'Open'"
    )
    assert not sql_engine._literal_misses(statement, schema())


# ── A zero, a blank, and nothing at all ──────────────────────────────────────


@pytest.fixture()
def zeros(warehouse):
    _add(warehouse, "Fee__c", {**_text("Id", "Name"), "Amount__c": "DECIMAL(18,2)"}, [("a", "F-1", "0"), ("b", "F-2", "5")])
    return warehouse


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT MIN(Amount__c) AS lowest FROM Fee__c",
        "SELECT MIN(CAST(Amount__c AS DOUBLE)) AS lowest FROM Fee__c",
        "SELECT MIN(f.Amount__c) AS lowest FROM Fee__c f",
    ],
)
def test_a_stored_zero_is_no_warning(zeros, monkeypatch, statement):
    Model(monkeypatch, statement)
    text, _meta = ask("what is the lowest fee?")
    assert ZERO not in text and NOTHING not in text


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT MAX(Date_of_Interview__c) AS latest FROM Interview__c WHERE Name = 'nope'",
        "SELECT string_agg(Name, ', ') AS names FROM Recruiter__c WHERE Name = 'nope'",
    ],
)
def test_no_figure_is_not_called_a_zero(warehouse, monkeypatch, statement):
    Model(monkeypatch, statement)
    text, meta = ask("what was it?")
    assert text.startswith("The query ran and no figure came back")
    assert ZERO not in text
    assert list(meta["data"][0].values()) == [None]


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT COALESCE(MAX(Amount__c), 0) AS m FROM Fee__c WHERE Name = 'nope'",
        "SELECT IFNULL(MIN(Amount__c), 0) AS m FROM Fee__c WHERE Name = 'nope'",
        "SELECT AVG(CASE WHEN Name = 'nope' THEN 1 ELSE 0 END) AS rate FROM Fee__c",
        "SELECT MAX(CASE WHEN Name = 'nope' THEN 1 ELSE 0 END) AS rate FROM Fee__c",
        "SELECT MIN(Amount__c) AS low, COUNT(*) AS n FROM Fee__c WHERE Name = 'nope' AND Id = 'x' HAVING COUNT(*) = 0",
    ],
)
def test_a_made_zero_keeps_its_warning(zeros, monkeypatch, statement):
    Model(monkeypatch, statement)
    assert ZERO in ask("what is the figure?")[0]


def test_a_zero_of_a_rate_that_was_stored_on_a_table_with_no_rows_says_so(warehouse, monkeypatch):
    _add(warehouse, "Asset__c", {**_text("Id"), "Amount__c": "DATE"})
    Model(monkeypatch, "SELECT MAX(Amount__c) AS d FROM Asset__c")
    assert "holds no rows" in ask("what is the latest?")[0]


JOIN_BLANKS = [
    ("SELECT r.Name, d.Name AS Department FROM Recruiter__c r LEFT JOIN Department__c d ON r.Department__c = d.Name WHERE r.Name = 'Asha Varnok'", "Department"),
    ("SELECT r.Name, (SELECT d.Name FROM Department__c d WHERE d.Name = r.Department__c) AS Department__c FROM Recruiter__c r WHERE r.Name = 'Asha Varnok'", "Department__c"),
    ("SELECT Name, CASE WHEN 1 = 0 THEN Department__c END AS Department__c FROM Recruiter__c WHERE Name = 'Asha Varnok'", "Department__c"),
]


@pytest.mark.parametrize("statement, field", JOIN_BLANKS)
def test_a_blank_through_a_link_says_nothing_of_the_record(warehouse, monkeypatch, statement, field):
    model = Model(monkeypatch, statement)
    text, _meta = ask("which department is Asha Varnok in?")
    prompt = model.narrative_user()
    assert "not_found_through_this_link" in prompt
    assert "blank_on_this_record" not in prompt
    assert field in prompt
    assert "matched no rows" not in text and ZERO not in text


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT Name, Manager__c FROM Recruiter__c WHERE Name = 'Asha Varnok'",
        "SELECT r.Name, r.Manager__c FROM Recruiter__c r WHERE r.Name = 'Asha Varnok'",
        "SELECT * FROM Recruiter__c WHERE Name = 'Asha Varnok'",
    ],
)
def test_a_bare_blank_says_the_field_read_is_blank_and_no_more(warehouse, monkeypatch, statement):
    model = Model(monkeypatch, statement)
    ask("who is the manager of Asha Varnok?")
    prompt = model.narrative_user()
    assert "blank_on_this_record" in prompt
    assert "another field may hold it" in prompt
    assert "not_found_through_this_link" not in prompt


# ── How a rate with nothing to divide by leaves the engine ───────────────────


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT COUNT(*) * 1.0 / COUNT(*) FILTER (WHERE Status__c = 'zz') AS rate FROM Interview__c",
        "SELECT 0.0 / COUNT(*) FILTER (WHERE Status__c = 'zz') AS rate FROM Interview__c",
        "SELECT 100.0 * COUNT(*) FILTER (WHERE Status__c = 'Completed') / COUNT(*) AS rate FROM Interview__c WHERE Name = 'nope'",
    ],
)
def test_a_rate_with_nothing_to_divide_by_is_no_figure(warehouse, monkeypatch, statement):
    Model(monkeypatch, statement, statement)
    rec = Rec()
    text = asyncio.run(run_sql_engine("what is the completion rate?", [], rec.emit))
    meta = rec.meta()
    assert meta["data"] == [{"rate": None}]
    assert ZERO not in text
    assert NOTHING in text
    json.loads(sse.meta_event(meta).split("data: ", 1)[1], parse_constant=pytest.fail)


def test_a_group_with_nothing_to_divide_by_keeps_the_other_rows(warehouse, monkeypatch):
    model = Model(
        monkeypatch,
        "SELECT Round__c, COUNT(*) * 1.0 / COUNT(*) FILTER (WHERE Round__c = 'First') AS rate FROM Interview__c GROUP BY 1 ORDER BY 1",
    )
    _text_answer, meta = ask("what is the rate per round?")
    assert meta["data"] == [{"Round__c": "First", "rate": 1.0}, {"Round__c": "Second", "rate": None}]
    assert "Infinity" not in model.narrative_user()
    assert "NaN" not in model.narrative_user()


def test_a_count_named_rate_cannot_crash_the_figures(warehouse, monkeypatch):
    Model(monkeypatch, "SELECT 1.0 / COUNT(*) FILTER (WHERE Name = 'nope') AS discount_rate FROM Invoice__c")
    assert NOTHING in ask("what is the discount rate?")[0]


def test_text_saying_nan_is_text():
    computed = deterministic_summary(["a_count"], [["Nan"]], aggregated=True)
    assert "numeric_totals" not in computed


def test_a_cell_deep_in_a_list_is_made_finite():
    assert sql_engine._finite([1.5, float("inf"), {"a": float("nan")}]) == [1.5, None, {"a": None}]


@pytest.mark.parametrize(
    "dumps", [lambda value: sse.meta_event(value).split("data: ", 1)[1], db._dumps_jsonb], ids=["frame", "row"]
)
def test_nothing_writes_a_bare_infinity(dumps):
    said = dumps({"a": float("inf"), "b": [float("nan")], "c": "NaN"})
    assert json.loads(said, parse_constant=pytest.fail) == {"a": None, "b": [None], "c": "NaN"}


@pytest.mark.parametrize(
    "dumps", [lambda value: sse.meta_event(value).split("data: ", 1)[1], db._dumps_jsonb], ids=["frame", "row"]
)
@pytest.mark.parametrize("key", [float("inf"), float("-inf"), float("nan")], ids=["inf", "-inf", "nan"])
@pytest.mark.parametrize("wrap", [lambda m: m, lambda m: [m], lambda m: (m,), lambda m: {"inner": m}], ids=["map", "list", "tuple", "nested"])
def test_a_map_keyed_by_a_non_finite_figure_loses_that_entry_not_the_turn(dumps, key, wrap):
    said = json.loads(dumps({"h": wrap({key: 1, 2.5: 3})}), parse_constant=pytest.fail)
    assert "Infinity" not in json.dumps(said) and "NaN" not in json.dumps(said)
    assert "2.5" in json.dumps(said)


def test_a_float_keyed_map_goes_back_to_the_writer_with_what_to_write(warehouse, monkeypatch):
    """DuckDB's histogram merges a 0/0 row into a finite bucket, so its counts are wrong however they are kept."""
    model = Model(
        monkeypatch,
        "SELECT histogram(Scheduled_Minutes__c / 0.0) AS h FROM Interview__c",
        "SELECT Scheduled_Minutes__c, COUNT(*) AS n FROM Interview__c GROUP BY 1",
    )
    _text_answer, meta = ask("what is the spread of minutes?")
    assert len(model.writer_prompts) == 2
    assert "GROUP BY" in model.writer_user(1)
    assert all("h" not in row and "n" in row for row in meta["data"])


def test_a_writer_that_keeps_writing_a_float_keyed_map_gets_the_honest_line(warehouse, monkeypatch):
    statement = "SELECT histogram(Scheduled_Minutes__c / 0.0) AS h FROM Interview__c"
    Model(monkeypatch, statement, statement, statement)
    text, meta = ask("what is the spread of minutes?")
    assert COULD_NOT_WRITE in text
    assert "data" not in meta


def test_a_finite_frame_comes_out_as_before():
    value = {"a": 1.5, "b": "é", "c": [1, None], "d": datetime(2026, 1, 1, tzinfo=timezone.utc)}
    assert sse.strict_json(value) == json.dumps(value, ensure_ascii=False, default=str)


# ── Two labels, distinct values, and what a row is ───────────────────────────


TWO_LABELS = "SELECT Round__c, Status__c, COUNT(*) AS interviews FROM Interview__c GROUP BY 1, 2"


@pytest.mark.parametrize(
    "statement",
    [
        TWO_LABELS,
        f"WITH t AS ({TWO_LABELS}) SELECT * FROM t",
        f"SELECT * FROM ({TWO_LABELS}) t",
        "SELECT Round__c, Status__c, MAX(Date_of_Interview__c) AS latest FROM Interview__c GROUP BY 1, 2",
    ],
)
def test_a_grouped_row_is_no_record_however_its_figure_is_named(warehouse, monkeypatch, statement):
    model = Model(monkeypatch, statement)
    ask("how many interviews per round and status?")
    figures = model.narrative_user().split("Computed figures")[1]
    assert "value_counts" not in figures
    assert "every row in the result" not in figures


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT Name, Round__c, Scheduled_Minutes__c AS minutes FROM Interview__c WHERE Round__c IN (SELECT Round__c FROM Interview__c GROUP BY 1 HAVING COUNT(*) > 5)",
        "SELECT Name, Round__c, COUNT(*) OVER (PARTITION BY Round__c) AS minutes FROM Interview__c",
    ],
)
def test_a_list_with_an_aggregate_elsewhere_is_still_records(warehouse, monkeypatch, statement):
    model = Model(monkeypatch, statement)
    ask("which interviews?")
    figures = model.narrative_user().split("Computed figures")[1]
    assert "value_counts" in figures
    assert "every row in the result" in figures
    assert "not a count of records" not in figures


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT DISTINCT Round__c FROM Interview__c",
        "SELECT DISTINCT Round__c, Status__c FROM Interview__c",
        "SELECT DISTINCT ON (Round__c) Round__c FROM Interview__c",
        "SELECT Round__c AS v FROM Interview__c UNION SELECT Status__c FROM Interview__c",
        "SELECT Name FROM Interview__c UNION ALL SELECT Name FROM Invoice__c",
        "WITH t AS (SELECT DISTINCT Round__c AS r FROM Interview__c) SELECT r FROM t",
        "SELECT r FROM (SELECT DISTINCT Round__c AS r FROM Interview__c) x",
    ],
)
def test_distinct_values_are_no_count_of_records(warehouse, monkeypatch, statement):
    Model(monkeypatch, statement)
    assert "record_count" not in ask("what rounds are there?")[1]["salesforce_sources"]
    AgentModel(monkeypatch, [sql_step()], statement)
    assert "record_count" not in ask_agent("what rounds are there?")[1]["salesforce_sources"]


def test_a_plain_row_is_still_a_record(warehouse, monkeypatch):
    Model(monkeypatch, "SELECT Round__c FROM Interview__c WHERE Name = 'INT-1'")
    assert ask("what round is INT-1?")[1]["salesforce_sources"]["record_count"] == 1


def test_a_distinct_value_gets_no_count_of_one_in_the_figures(warehouse, monkeypatch):
    model = Model(monkeypatch, "SELECT DISTINCT Round__c FROM Interview__c")
    ask("what rounds are there?")
    figures = model.narrative_user().split("Computed figures")[1]
    assert "value_counts" not in figures
    assert "one distinct value" in figures


def test_held_back_distinct_values_are_counted_as_rows(warehouse, monkeypatch):
    Model(monkeypatch, "SELECT DISTINCT Email__c FROM Recruiter__c")
    text = ask("list the staff")[0]
    assert text.startswith("2 rows came back.")
    assert "records matched" not in text


# ── Dates held as text ───────────────────────────────────────────────────────


@pytest.fixture()
def text_dates(warehouse):
    _add(warehouse, "Availability__c", _text("Id", "Date__c", "Status__c", "Candidate__c", "When_did_you_move_to_USA__c", "Joined_At__c"), [("a", "05-11-2026", "Open", "x", "2021", "Oct 2")])
    _add(warehouse, "Deliverable__c", _text("Id", "Due_Date_IST__c", "Start_Time_IST__c"), [("a", "10/05/2026 9:00 AM", "1/1/2026")])
    _add(warehouse, "Candidate_Training__c", _text("Id", "Application_Update_Status__c"), [("a", "Done"), ("b", "Open")])
    return warehouse


ROWS = [[1], [2]]


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT COUNT(*) AS n FROM Availability__c WHERE Date__c = '05-11-2026'",
        "SELECT COUNT(*) AS n FROM Deliverable__c WHERE Due_Date_IST__c LIKE '10/%/2026%'",
        "WITH t AS (SELECT Due_Date_IST__c AS d FROM Deliverable__c) SELECT COUNT(*) AS n FROM t WHERE d LIKE '10/%'",
        "SELECT COUNT(*) AS n FROM Deliverable__c WHERE Start_Time_IST__c LIKE '%/2026%'",
        "SELECT COUNT(*) AS n FROM Availability__c WHERE Joined_At__c LIKE 'Oct %'",
        "SELECT COUNT(*) AS n FROM Availability__c WHERE When_did_you_move_to_USA__c LIKE '%2021'",
    ],
)
def test_a_filter_on_a_date_held_as_text_is_a_date_filter(text_dates, statement):
    assert _no_date_line(statement, ROWS, schema()) == ""


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT COUNT(*) AS n FROM Availability__c WHERE Candidate__c = 'a0RXx0000001AAAIA1'",
        "SELECT COUNT(*) AS n FROM Candidate_Training__c",
        "SELECT COUNT(*) AS n FROM Candidate_Training__c WHERE Application_Update_Status__c = 'Done'",
        "SELECT COUNT(*) AS n FROM Availability__c WHERE Status__c = 'Open'",
    ],
)
def test_a_word_with_date_in_it_is_no_date(text_dates, statement):
    assert _no_date_line(statement, ROWS, schema()) == "No date filter was applied, so this covers all dates."


# ── First N of M: when, and of what ──────────────────────────────────────────


BG = "SELECT Name FROM Background_Check__c ORDER BY Name"
CUT_30 = "SELECT Name FROM Background_Check__c LIMIT 30"


@pytest.mark.parametrize(
    "question",
    [
        "Which candidates have some pending documents?",
        "Which background checks are pending after a 15 min call?",
        "Which background checks have at least one document pending?",
        "Which checks were raised in the last 7 days?",
        "Which checks are pending for over 30 days?",
        "Which check is on the highest qualification?",
        "List the checks, for example the pending ones",
    ],
)
def test_an_ordinary_word_does_not_turn_the_line_off(warehouse, monkeypatch, question):
    Model(monkeypatch, CUT_30)
    text, meta = ask(question)
    assert "These are the first 30 of 47 matching rows." in text
    assert meta["salesforce_sources"]["record_count"] == 47
    assert meta["salesforce_sources"]["truncated"] is True


@pytest.mark.parametrize(
    "question, statement",
    [
        ("Show me the top 5 checks", "SELECT Name FROM Background_Check__c LIMIT 5"),
        ("show me 5 checks", "SELECT Name FROM Background_Check__c LIMIT 5"),
        ("Find the three most common checks", "SELECT Name FROM Background_Check__c LIMIT 3"),
        ("Which is the latest check?", "SELECT Name FROM Background_Check__c LIMIT 1"),
    ],
)
def test_a_cut_the_person_asked_for_stays_silent(warehouse, monkeypatch, question, statement):
    Model(monkeypatch, statement)
    text, meta = ask(question)
    assert "These are" not in text
    assert meta["salesforce_sources"]["truncated"] is False


def test_a_word_that_counts_nothing_earns_a_true_line(warehouse, monkeypatch):
    Model(monkeypatch, "SELECT Name FROM Background_Check__c LIMIT 5")
    assert "These are the first 5 of 47 matching rows." in ask("Show me some checks")[0]


@pytest.mark.parametrize(
    "step_input", ["get the latest background checks", "list 30 background checks", "list them, limit 30"]
)
def test_the_planners_words_do_not_ask_for_a_cut(warehouse, monkeypatch, step_input):
    model = AgentModel(monkeypatch, [sql_step(input=step_input)], CUT_30)
    _text_answer, meta = ask_agent("How many background checks are there?")
    assert "These are the first 30 of 47" in model.step_results()
    assert meta["salesforce_sources"]["record_count"] == 47
    assert meta["salesforce_sources"]["truncated"] is True


def test_a_blank_filter_stays_with_its_own_word_list(warehouse):
    """`_null_drops` shares no rule with the cut: "top 5 recruiters" has no use for a blank one."""
    statement = "SELECT Status__c, COUNT(*) AS n FROM Interview__c WHERE Status__c IS NOT NULL GROUP BY 1"
    assert sql_engine._null_drops(statement, ["Status__c", "n"], "Which status has the most interviews?", schema()) == []


def test_a_cut_the_count_could_not_read_says_so(warehouse, monkeypatch):
    def fails(_statement):
        raise RuntimeError("no count today")

    monkeypatch.setattr(sql_engine, "_count_rows", fails)
    Model(monkeypatch, CUT_30)
    text, meta = ask("Which background checks are pending?")
    assert "These are the first 30 rows of a larger result." in text
    assert "-1" not in text
    assert meta["salesforce_sources"]["truncated"] is True
    assert "record_count" not in meta["salesforce_sources"]


PAGES = [
    (f"{BG} LIMIT 10 OFFSET 10", "These are rows 11 to 20 of 47 matching rows."),
    (f"{BG} OFFSET 10 LIMIT 10", "These are rows 11 to 20 of 47 matching rows."),
    (f"{BG} LIMIT 30 OFFSET 30", "These are rows 31 to 47 of 47 matching rows."),
    (f"{BG} OFFSET 30", "These are rows 31 to 47 of 47 matching rows."),
    (f"{BG} OFFSET 10 ROWS FETCH NEXT 10 ROWS ONLY", "These are rows 11 to 20 of 47 matching rows."),
    (f"{BG} LIMIT 10 OFFSET 50", "The statement skipped past all 47 matching rows, so none are shown."),
]


@pytest.mark.parametrize("statement, line", PAGES)
def test_a_later_page_is_called_a_later_page(warehouse, monkeypatch, statement, line):
    Model(monkeypatch, statement)
    text, meta = ask("show me the next background checks")
    assert line in text
    assert "first" not in text.split("\n\n")[0]
    assert "matched no rows" not in text
    assert meta["salesforce_sources"]["record_count"] == 47


@pytest.mark.parametrize("statement, line", PAGES)
def test_the_agent_route_calls_it_the_same(warehouse, monkeypatch, statement, line):
    AgentModel(monkeypatch, [sql_step(input="show the next background checks")], statement)
    text, _meta = ask_agent("show me the next background checks")
    assert line in text
    assert "matched no rows" not in text


def test_the_first_page_is_still_the_first(warehouse, monkeypatch):
    Model(monkeypatch, f"{BG} LIMIT 10 OFFSET 0")
    assert "These are the first 10 of 47 matching rows." in ask("show me the background checks")[0]


def test_next_ten_names_its_own_size(warehouse, monkeypatch):
    Model(monkeypatch, f"{BG} LIMIT 10 OFFSET 10")
    assert "These are" not in ask("show me the next 10 background checks")[0]


def test_a_single_pick_past_the_first_stays_a_pick(warehouse, monkeypatch):
    Model(monkeypatch, f"{BG} LIMIT 1 OFFSET 1")
    assert "These are" not in ask("which is the second background check?")[0]


def test_a_fetch_first_is_a_limit(warehouse, monkeypatch):
    Model(monkeypatch, "SELECT Name FROM Background_Check__c FETCH FIRST 30 ROWS ONLY")
    assert "These are the first 30 of 47 matching rows." in ask("which background checks are pending?")[0]


# ── The agent route: a line in code, not a request to the model ──────────────


def test_a_cut_is_written_under_a_synthesis_that_ignores_it(warehouse, monkeypatch):
    AgentModel(monkeypatch, [sql_step()], CUT_30, synthesis=("There are 30 background checks.",))
    text, meta = ask_agent("How many background checks are there?")
    assert "There are 30 background checks." in text
    assert "These are the first 30 of 47 matching rows." in text
    assert "first_line" not in meta


def test_a_zero_is_written_under_a_synthesis_that_ignores_it(warehouse, monkeypatch):
    AgentModel(
        monkeypatch,
        [sql_step()],
        "SELECT COUNT(*) AS n FROM Interview__c WHERE Round__c = '1'",
        "SELECT COUNT(*) AS n FROM Interview__c WHERE Round__c = '1'",
        synthesis=("There are no such interviews in the business.",),
    )
    text, meta = ask_agent("How many first round interviews are there?")
    assert ZERO in text
    assert "Round__c has no stored value '1'" in text
    assert "zero_line" not in meta


def test_no_rows_are_written_under_a_synthesis_that_ignores_them(warehouse, monkeypatch):
    statement = "SELECT Name FROM Interview__c WHERE Round__c = 'Third'"
    AgentModel(monkeypatch, [sql_step()], statement, statement, synthesis=("None.",))
    assert "matched no rows" in ask_agent("which third round interviews are there?")[0]


def test_two_steps_one_cut_carries_that_steps_title(warehouse, monkeypatch):
    AgentModel(
        monkeypatch,
        [sql_step(1, "Checks", "list the checks"), sql_step(2, "Invoices", "count invoices")],
        by_input={"list the checks": CUT_30, "count invoices": "SELECT COUNT(*) AS n FROM Invoice__c"},
    )
    text = ask_agent("list the checks and count the invoices")[0]
    assert "Checks: These are the first 30 of 47 matching rows." in text
    assert "Invoices: These are" not in text


def test_a_figure_and_an_asked_for_cut_get_no_line(warehouse, monkeypatch):
    AgentModel(monkeypatch, [sql_step()], "SELECT COUNT(*) AS n FROM Invoice__c")
    assert "These are" not in ask_agent("how many invoices?")[0]
    AgentModel(monkeypatch, [sql_step()], "SELECT Name FROM Background_Check__c LIMIT 5")
    assert "These are" not in ask_agent("show me 5 background checks")[0]


# ── A parked turn stays parked, and a dead model is not the database ─────────


class Goes:
    """A writer that answers `replies` and then fails on the next call."""

    def __init__(self, monkeypatch, *replies, fails_with):
        self.replies = list(replies)
        self.fails_with = fails_with
        self.calls = 0
        self.narrated = False

        async def chat_completion(_messages, **_kwargs):
            self.calls += 1
            if self.replies:
                return self.replies.pop(0)
            raise self.fails_with

        async def stream_chat_completion(_messages, **_kwargs):
            self.narrated = True
            yield "An answer."

        monkeypatch.setattr(sql_engine.llm, "chat_completion", chat_completion)
        monkeypatch.setattr(sql_engine.llm, "stream_chat_completion", stream_chat_completion)


PARKS = [QueuedForRecovery(600.0), LeaseLost("int-1", "gen-elsewhere")]
#: A first statement of each kind that sends the engine back to the writer.
BACK_TO_THE_WRITER = [
    pytest.param((), id="first-call"),
    pytest.param(("SELECT Nope__c FROM Invoice__c",), id="after-a-refused-statement"),
    pytest.param(("",), id="after-an-empty-reply"),
    pytest.param(("SELECT COUNT(*) AS n FROM Invoice__c WHERE Invoice_Status__c != 'Paid'",), id="after-advice"),
]


@pytest.mark.parametrize("parked", PARKS, ids=["queued", "lease"])
@pytest.mark.parametrize("replies", BACK_TO_THE_WRITER)
def test_a_parked_turn_leaves_the_engine(warehouse, monkeypatch, parked, replies):
    Goes(monkeypatch, *replies, fails_with=parked)
    with pytest.raises(type(parked)):
        ask("how many invoices are not paid?")


@pytest.mark.parametrize("parked", PARKS, ids=["queued", "lease"])
def test_a_parked_turn_leaves_a_report_section(warehouse, monkeypatch, tmp_path, parked):
    calls = {"n": 0}

    async def chat_completion(_messages, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return json.dumps({"title": "R", "sections": [{"title": "S", "kind": "sql", "instruction": "count them"}]})
        raise parked

    monkeypatch.setattr(sql_engine.llm, "chat_completion", chat_completion)
    monkeypatch.setattr(settings, "reports_dir", str(tmp_path))
    rec = Rec()
    with pytest.raises(type(parked)):
        asyncio.run(report.run_report_engine("report on the invoices", [], rec.emit))
    assert not list(tmp_path.glob("*.pdf")), "no report is made of what did not run"


@pytest.mark.parametrize("parked", PARKS, ids=["queued", "lease"])
def test_a_parked_turn_leaves_a_document(warehouse, monkeypatch, parked):
    Goes(monkeypatch, fails_with=parked)
    with pytest.raises(type(parked)):
        asyncio.run(artifact._salesforce_table("make a doc about the total invoices", []))


MODEL_FAILURES = [
    resilience.ModelUnavailable("http://model.invalid/v1", 1.0, 1, ConnectionError("refused")),
    admission.AdmissionRejected("main", "capacity", 0.0),
    asyncio.TimeoutError(),
    ConnectionError("refused"),
]


@pytest.mark.parametrize("failure", MODEL_FAILURES, ids=lambda f: type(f).__name__)
def test_a_dead_model_is_the_workers_to_word(warehouse, monkeypatch, failure):
    """The worker has the sentence and the code, and a turn that never ran is no answer."""
    Goes(monkeypatch, fails_with=failure)
    with pytest.raises(type(failure)):
        ask("how many invoices are there?")


def test_a_fault_nobody_named_blames_neither_database_nor_model(warehouse, monkeypatch):
    Goes(monkeypatch, fails_with=TypeError("a coding bug"))
    text, meta = ask("how many invoices are there?")
    assert "database" not in text and "model" not in text
    assert "could not finish" in text
    assert "database" not in meta["salesforce_error"]
    assert meta["salesforce_error_kind"] == "TypeError"


def test_two_real_refusals_keep_the_database_sentence(warehouse, monkeypatch):
    statement = "SELECT Nope__c FROM Invoice__c"
    Model(monkeypatch, statement, "SELECT Nor_This__c FROM Invoice__c")
    text, meta = ask("how many invoices are there?")
    assert COULD_NOT_WRITE in text
    assert meta["salesforce_error"] == sql_engine.QUERY_REFUSED
    assert meta["salesforce_error_kind"] == "Binder"


RAW_ID_ONLY = "SELECT Department__c AS Department, COUNT(*) AS n FROM Recruiter__c GROUP BY 1 ORDER BY 2"


def test_a_failed_retry_keeps_a_statement_only_ids_were_wrong_with(warehouse, monkeypatch):
    Goes(monkeypatch, RAW_ID_ONLY, fails_with=asyncio.TimeoutError())
    text, meta = ask("How many staff per department?")
    assert sorted(row["Department"] for row in meta["data"]) == ["Operations", "Sales"]
    assert "database" not in text


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT COUNT(*) AS n FROM Interview__c WHERE Round__c = '1'",
        "SELECT COUNT(*) AS n FROM Invoice__c WHERE Invoice_Status__c != 'Paid'",
        "SELECT Status__c, COUNT(*) AS n FROM Interview__c WHERE Status__c IS NOT NULL GROUP BY 1",
    ],
)
def test_a_failed_retry_does_not_let_a_doubted_figure_stand(warehouse, monkeypatch, statement):
    model = Goes(monkeypatch, statement, fails_with=asyncio.TimeoutError())
    with pytest.raises(asyncio.TimeoutError):
        ask("how many are there?")
    assert not model.narrated


def test_a_failed_retry_keeps_its_rows_on_the_agent_route(warehouse, monkeypatch):
    calls = {"n": 0}

    async def chat_completion(_messages, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return RAW_ID_ONLY
        raise asyncio.TimeoutError()

    monkeypatch.setattr(sql_engine.llm, "chat_completion", chat_completion)
    output, _detail, sub_meta = run_step("how many staff per department?")
    assert sorted(row["Department"] for row in sub_meta["data"]) == ["Operations", "Sales"]
    assert "Step failed" not in output


def test_a_failed_step_does_not_show_the_models_address(warehouse, monkeypatch):
    async def chat_completion(messages, **_kwargs):
        if "plan multi-step" in str(messages[0].get("content")):
            return json.dumps({"steps": [sql_step()]})
        raise resilience.ModelUnavailable("http://model.invalid/v1", 1.0, 1, ConnectionError("refused"))

    async def stream_chat_events(_messages, **_kwargs):
        yield "token", "It failed."

    monkeypatch.setattr(sql_engine.llm, "chat_completion", chat_completion)
    monkeypatch.setattr(sql_engine.llm, "stream_chat_events", stream_chat_events)
    rec = Rec()
    asyncio.run(agent.run_agent_engine("count them", [], rec.emit, effort="think", salesforce=True, web=False))
    assert "model.invalid" not in json.dumps(rec.events, default=str)


def test_a_report_section_does_not_print_the_models_address(warehouse, monkeypatch, tmp_path):
    assert sql_engine.failure_reason(resilience.ModelUnavailable("http://model.invalid/v1", 1.0, 1, ConnectionError("refused"))) == "the model could not be reached"
    assert sql_engine.failure_reason(ValueError("plain words")) == "plain words"


# ── Time: one statement cannot stop every member ─────────────────────────────


#: `range` is allowed, so this is what a fan-out join costs without needing one.
FOREVER = "SELECT COUNT(*) AS n FROM Interview__c a, range(100000000000) b WHERE b.range % 7 = 1"
CUT_FOREVER = "SELECT a.Name FROM Interview__c a, range(100000000000) b WHERE b.range % 7 = 1 LIMIT 5"


def _with_the_loop_watched(coroutine):
    """(result, longest gap between ticks of a task sharing the event loop)."""

    async def main():
        gaps = [0.0]

        async def tick():
            last = time.monotonic()
            while True:
                await asyncio.sleep(0.01)
                now = time.monotonic()
                gaps.append(now - last)
                last = now

        ticker = asyncio.create_task(tick())
        try:
            return await coroutine, max(gaps)
        finally:
            ticker.cancel()

    return asyncio.run(main())


def test_a_statement_past_its_time_is_stopped_and_the_loop_keeps_going(warehouse, monkeypatch):
    monkeypatch.setattr(settings, "sql_statement_seconds", 0.5)
    model = Model(monkeypatch, FOREVER)
    rec = Rec()
    text, gap = _with_the_loop_watched(run_sql_engine("how many?", [], rec.emit))
    assert text.startswith("That query took too long")
    assert "database" not in text
    assert len(model.writer_prompts) == 1, "no model call on a statement that was only slow"
    assert rec.meta()["salesforce_error"] == sql_engine.QUERY_TOO_SLOW
    assert gap < 0.45


def test_a_count_past_its_time_is_unknown_and_never_every_row(warehouse, monkeypatch):
    monkeypatch.setattr(sql_engine, "_COUNT_SECONDS", 0.3)
    model = Model(monkeypatch, CUT_FOREVER)
    rec = Rec()
    text, gap = _with_the_loop_watched(run_sql_engine("list them", [], rec.emit))
    assert "These are the first 5 rows of a larger result." in text
    assert "-1" not in text
    sources = rec.meta()["salesforce_sources"]
    assert sources["truncated"] is True
    assert "record_count" not in sources
    assert "every row in the result" not in model.narrative_user()
    assert gap < 0.28


def test_a_statement_past_its_time_ends_an_agent_step_in_words(warehouse, monkeypatch):
    monkeypatch.setattr(settings, "sql_statement_seconds", 0.3)
    AgentModel(monkeypatch, [], FOREVER)
    output, detail, _sub_meta = run_step("how many?")
    assert "took too long" in output
    assert detail == "query took too long"


def test_a_cancelled_request_stops_its_statement(warehouse, monkeypatch):
    stopped = []
    real = sql_engine._interrupt
    monkeypatch.setattr(sql_engine, "_interrupt", lambda con: (stopped.append(con), real(con)))

    async def main():
        task = asyncio.create_task(sql_engine._execute_off_the_loop(lambda: sql_engine._execute(FOREVER, 2)))
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert stopped


def test_a_late_timer_on_a_closed_statement_is_nothing():
    con = duckdb.connect()
    cursor = con.cursor()
    cursor.close()
    sql_engine._interrupt(cursor)
    con.close()


# ── A LIKE pattern, and a long stored text ───────────────────────────────────


def test_the_like_reader_agrees_with_duckdb():
    random.seed(11)
    con = duckdb.connect()
    alphabet = "ab%_"
    for _ in range(3000):
        pattern = "".join(random.choice(alphabet) for _ in range(random.randint(0, 6)))
        value = "".join(random.choice("ab") for _ in range(random.randint(0, 6)))
        expected = con.execute("SELECT ? LIKE ?", [value, pattern]).fetchone()[0]
        assert sql_engine._like_matches(pattern, value) == expected, (pattern, value)


@pytest.mark.parametrize("pattern", ["%" * 40 + "zz", "%_" * 12 + "zz", "%e" * 20 + "%zz"])
def test_no_like_pattern_is_slow_to_read(warehouse, pattern):
    _add(warehouse, "Reason__c", _text("Id", "Why__c"), [(str(i), "e" * 59 + chr(97 + i)) for i in range(25)])
    started = time.monotonic()
    misses = sql_engine._literal_misses(f"SELECT 1 FROM Reason__c WHERE Why__c LIKE '{pattern}'", schema())
    assert time.monotonic() - started < 1.0
    assert misses


def test_ilike_still_reads_either_case(warehouse):
    assert not sql_engine._literal_misses("SELECT 1 FROM Interview__c WHERE Round__c ILIKE 'fir%'", schema())
    assert sql_engine._literal_misses("SELECT 1 FROM Interview__c WHERE Round__c LIKE 'fir%'", schema())


@pytest.mark.parametrize(
    "text, said",
    [
        ("sms-otp: 482910", "sms-otp: [hidden]"),
        ("candidate-passport: A1234567", "candidate-passport: [hidden]"),
        ("password: Hunter2", "password: [hidden]"),
        ("api-key = abc123", "api-key = [hidden]"),
    ],
)
def test_a_labelled_secret_is_still_hidden(text, said):
    assert sql_engine._hide_embedded_secrets(text) == said


@pytest.mark.parametrize(
    "text",
    ["Company note " + "-" * 50000, "token-" * 1500, "pwd" * 20000, "passport:" * 5000, "a-" * 10000],
    ids=["hyphens", "tokens", "pwd", "passport", "pairs"],
)
def test_no_stored_text_is_slow_to_read(text):
    started = time.monotonic()
    sql_engine._hide_embedded_secrets(text)
    assert time.monotonic() - started < 2.0


# ── A swap of the copy ───────────────────────────────────────────────────────


def _publish(path, statement, stamp=None):
    """A new whole file onto the same path, as publish_snapshot does."""
    staging = path + ".next"
    shutil.copyfile(path, staging)
    con = duckdb.connect(staging)
    con.execute(statement)
    con.close()
    if stamp:
        os.utime(staging, (stamp, stamp))
    os.replace(staging, path)


THIRD = """INSERT INTO raw."Interview__c" (Id, Name, Round__c) VALUES ('a', 'INT-T', 'Third')"""


def test_a_new_value_in_a_new_copy_is_no_miss(warehouse):
    statement = "SELECT 1 FROM Interview__c WHERE Round__c = 'Third'"
    assert sql_engine._literal_misses(statement, schema()), "not there yet, and the cache has it now"
    _publish(warehouse, THIRD)
    assert sql_engine._literal_misses(statement, schema()) == []


def test_the_prompt_block_sees_a_new_copy_too(warehouse):
    sliced = {"Interview__c": schema()["Interview__c"]}
    assert "Third" not in sql_engine._stored_values_block(sliced, "how many third round interviews?")
    _publish(warehouse, THIRD)
    assert "Third" in sql_engine._stored_values_block(sliced, "how many third round interviews?")


def test_an_old_copys_values_are_not_kept_after_a_swap(warehouse):
    statement = "SELECT 1 FROM Interview__c WHERE Round__c = 'First'"
    sql_engine._literal_misses(statement, schema())
    for number in range(5):
        _publish(warehouse, f"""INSERT INTO raw."Interview__c" (Id, Name, Round__c) VALUES ('{number}', 'T', 'First')""")
        sql_engine._literal_misses(statement, schema())
    assert len(sql_engine._stored_values_cache) == 1


OLD, NEW = 1_000_000_000, 1_800_000_000


def _iso(stamp):
    return datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat(timespec="seconds")


NINTH = """INSERT INTO raw."Invoice__c" VALUES ('a0VXx0000009AAAIA9', 'INV-9', 'Not Paid', '900')"""
ADVISED = "SELECT SUM(Outstanding_Amount__c) AS unpaid FROM Invoice__c WHERE Invoice_Status__c != 'Paid'"


def test_rows_kept_after_a_swap_keep_their_own_time(warehouse, monkeypatch):
    os.utime(warehouse, (OLD, OLD))
    model = Model(monkeypatch, ADVISED, ADVISED)
    real = sql_engine.llm.chat_completion

    async def chat(messages, **kwargs):
        if model.writer_prompts:
            _publish(warehouse, NINTH, NEW)
        return await real(messages, **kwargs)

    monkeypatch.setattr(sql_engine.llm, "chat_completion", chat)
    _text_answer, meta = ask("how much is unpaid?")
    assert float(meta["data"][0]["unpaid"]) == 100.0
    assert meta["salesforce_sources"]["query_timestamp"] == _iso(OLD)


def test_an_agent_answer_keeps_the_time_of_what_it_read(warehouse, monkeypatch):
    os.utime(warehouse, (OLD, OLD))
    AgentModel(monkeypatch, [sql_step(1, "One", "count invoices")], by_input={"count invoices": "SELECT COUNT(*) AS n FROM Invoice__c"})
    real = sql_engine.llm.stream_chat_events

    async def events(messages, **kwargs):
        _publish(warehouse, NINTH, NEW)
        async for item in real(messages, **kwargs):
            yield item

    monkeypatch.setattr(sql_engine.llm, "stream_chat_events", events)
    meta = ask_agent("how many invoices are there?")[1]
    assert meta["data"] == [{"n": 3}]
    assert meta["salesforce_sources"]["query_timestamp"] == _iso(OLD)
    assert "snapshot_time" not in meta


# ── Report sections ──────────────────────────────────────────────────────────


@pytest.fixture()
def many(warehouse):
    _add(
        warehouse,
        "Application__c",
        {**_text("Id", "Name", "Stage__c"), "Amount__c": "DECIMAL(18,2)"},
        [(f"a{i:04d}", f"APP-{i}", "Open" if i < 500 else "Closed", "10") for i in range(1200)],
    )
    return warehouse


def _section(tmp_path, instruction="list the applications", request="report"):
    section = {"title": "Applications", "instruction": instruction, "chart": False}
    return "\n".join(asyncio.run(report._sql_section(section, 1, tmp_path, request)))


def test_a_cut_section_says_the_true_total(many, monkeypatch, tmp_path):
    Model(monkeypatch, "SELECT Name, Stage__c FROM Application__c LIMIT 30", "Prose.")
    assert "_Showing first 20 of 1200 rows._" in _section(tmp_path)


def test_a_section_under_twenty_rows_still_says_it_was_cut(many, monkeypatch, tmp_path):
    Model(monkeypatch, "SELECT Name, Stage__c FROM Application__c LIMIT 10", "Prose.")
    assert "_Showing first 10 of 1200 rows._" in _section(tmp_path)


def test_a_section_over_every_cap_counts_what_it_could_not_fetch(many, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "sql_summary_row_cap", 100)
    model = Model(monkeypatch, "SELECT Name, Amount__c FROM Application__c", "Prose.")
    text = _section(tmp_path)
    assert "_Showing first 20 of 1200 rows._" in text
    assert "10001" not in text and "101 rows" not in text
    assert "FIRST 100 rows" in model.writer_prompts[-1][-1]["content"]


def test_a_section_whose_total_could_not_be_counted_prints_no_minus_one(many, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "sql_summary_row_cap", 100)
    monkeypatch.setattr(report, "count_every_row", lambda _sql: -1)
    Model(monkeypatch, "SELECT Name, Stage__c FROM Application__c", "Prose.")
    text = _section(tmp_path)
    assert "the total could not be counted" in text
    assert "of -1" not in text


def test_a_zero_section_writes_the_warning_and_the_scope(many, monkeypatch, tmp_path):
    model = Model(monkeypatch, "SELECT COUNT(*) AS n FROM Application__c WHERE Stage__c = 'Clsoed'", "SELECT COUNT(*) AS n FROM Application__c WHERE Stage__c = 'Clsoed'", "Prose.")
    text = _section(tmp_path)
    assert ZERO in text
    assert "Stage__c has no stored value 'Clsoed'" in text
    assert "Scope: Application__c" in text
    assert "came back 0" in model.writer_prompts[-1][-1]["content"]


def test_an_empty_section_warns_and_prints_no_empty_table(many, monkeypatch, tmp_path):
    statement = "SELECT Name FROM Application__c WHERE Stage__c = 'Clsoed'"
    Model(monkeypatch, statement, statement, "Prose.")
    text = _section(tmp_path)
    assert "matched no rows" in text
    assert "| Name |" not in text


def test_the_prose_is_given_the_figures_and_what_was_read(many, monkeypatch, tmp_path):
    model = Model(monkeypatch, "SELECT Name, Amount__c FROM Application__c LIMIT 30", "Prose.")
    _section(tmp_path)
    prompt = model.writer_prompts[-1][-1]["content"]
    assert "Computed figures" in prompt
    assert "FIRST 30 rows of a larger result of 1200 total rows" in prompt
    assert "Scope: Application__c" in prompt


def test_the_words_of_the_whole_request_do_not_silence_a_section(many, monkeypatch, tmp_path):
    Model(monkeypatch, "SELECT Name, Stage__c FROM Application__c LIMIT 30", "Prose.")
    assert "of 1200 rows" in _section(tmp_path, request="Report on our most active recruiters and the last 30 days")


def test_an_asked_for_top_in_a_section_stays_as_asked(many, monkeypatch, tmp_path):
    Model(monkeypatch, "SELECT Name, Stage__c FROM Application__c LIMIT 30", "Prose.")
    assert "_Showing first 20 of 30 rows._" in _section(tmp_path, instruction="list the top 30 applications")


def test_a_section_with_a_schema_that_fails_survives(many, monkeypatch, tmp_path):
    Model(monkeypatch, "SELECT Name, Stage__c FROM Application__c WHERE Stage__c = 'Open' LIMIT 2", "Prose.")
    real = schema_cache.get
    calls = {"n": 0}

    def flaky(path):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("locked")
        return real(path)

    monkeypatch.setattr(schema_cache, "get", flaky)
    assert "Prose." in _section(tmp_path, instruction="list 2 applications")


def test_a_held_back_section_counts_past_its_limit(warehouse, monkeypatch, tmp_path):
    Model(monkeypatch, "SELECT Id FROM Background_Check__c LIMIT 10")
    assert "47 records matched." in _section(tmp_path)


# ── Files: a cut table is said to be one, and nothing is worked out from it ──


def _table(instruction="make a doc about the total applications"):
    return asyncio.run(artifact._salesforce_table(instruction, []))


def test_a_file_table_at_the_fetch_cap_says_it_is_part(many, monkeypatch):
    Model(monkeypatch, "SELECT Name FROM Application__c")
    tables, _sources, notes = _table()
    assert "These are the first 500 of 1200 matching rows." in " ".join(notes)
    assert tables[0].partial


def test_a_file_table_with_an_unasked_limit_says_it_is_part(many, monkeypatch):
    Model(monkeypatch, "SELECT Name FROM Application__c LIMIT 30")
    tables, _sources, notes = _table()
    assert "These are the first 30 of 1200 matching rows." in " ".join(notes)
    assert tables[0].partial


def test_a_limit_past_the_fetch_cap_counts_without_it(many, monkeypatch):
    Model(monkeypatch, "SELECT Name FROM Application__c LIMIT 1000")
    _tables, _sources, notes = _table()
    assert "These are the first 500 of 1200 matching rows." in " ".join(notes)


def test_a_table_of_exactly_the_fetch_cap_is_whole(warehouse, monkeypatch):
    _add(warehouse, "Five__c", _text("Id", "Name"), [(str(i), f"N-{i}") for i in range(500)])
    Model(monkeypatch, "SELECT Name FROM Five__c")
    tables, _sources, notes = _table()
    assert not tables[0].partial
    assert "These are" not in " ".join(notes)


def test_a_count_that_fails_says_a_larger_result(many, monkeypatch):
    monkeypatch.setattr(sql_engine, "_count_rows", lambda _statement: (_ for _ in ()).throw(RuntimeError("no")))
    Model(monkeypatch, "SELECT Name FROM Application__c")
    tables, _sources, notes = _table()
    assert "first 500 rows of a larger result" in " ".join(notes)
    assert "-1" not in " ".join(notes)
    assert tables[0].partial


def test_an_asked_for_top_in_a_file_is_whole(many, monkeypatch):
    Model(monkeypatch, "SELECT Name FROM Application__c LIMIT 30")
    tables, _sources, notes = _table("make a doc with the top 30 applications and the total")
    assert not tables[0].partial
    assert "These are" not in " ".join(notes)


@pytest.mark.parametrize(
    "statement, said",
    [
        ("SELECT COUNT(*) AS n FROM Application__c WHERE Stage__c = 'Clsoed'", ZERO),
        ("SELECT Name FROM Application__c WHERE Stage__c = 'Clsoed'", "matched no rows"),
    ],
)
def test_nothing_read_is_not_called_empty_data(many, monkeypatch, statement, said):
    Model(monkeypatch, statement, statement)
    _tables, _sources, notes = _table()
    assert said in notes[0]
    assert "must not call the data empty" in notes[0]


def test_an_empty_table_is_called_what_it_is(warehouse, monkeypatch):
    _add(warehouse, "Asset__c", _text("Id", "Name"))
    Model(monkeypatch, "SELECT Name FROM Asset__c")
    _tables, _sources, notes = _table()
    assert "holds no rows" in notes[0]


def test_a_held_back_file_table_counts_past_its_limit(warehouse, monkeypatch):
    Model(monkeypatch, "SELECT Id FROM Background_Check__c LIMIT 10")
    _tables, _sources, notes = _table()
    assert "47 records matched." in notes[0]


def test_the_reader_is_told_as_well_as_the_model():
    assert "first 500 of 1200" in artifact._warning_clause([_cut_table().partial])


def _cut_table():
    return DataTable(
        id="t1",
        title="Salesforce query result",
        columns=["Stage__c", "Amount__c"],
        rows=[["Open", 10]] * 500,
        source_id="sf1",
        partial="These are the first 500 of 1200 matching rows.",
    )


def test_no_chart_is_computed_over_a_cut_table():
    with pytest.raises(chart_data.ChartDataError):
        chart_data.compute(SimpleNamespace(data=object()), _cut_table())


def test_no_total_is_computed_over_a_cut_table():
    spec = artifact_spec.parse_body(
        "workbook",
        {
            "title": "T",
            "sheets": [
                {
                    "name": "Summary",
                    "columns": [{"name": "Metric", "type": "text"}, {"name": "Value", "type": "number"}],
                    "rows": [["Total Amount__c", 5000], ["Number of records", 500]],
                }
            ],
        },
    )
    out, _notes, report_ = derived.enforce(spec, [_cut_table()])
    assert "Summary" in report_["dropped"]
    assert not [sheet for sheet in out.body.sheets if sheet.name == "Summary"]
    assert not any(cell == 5000 for sheet in out.body.sheets for row in sheet.rows for cell in row)


def test_a_cut_survives_the_round_trip_of_a_job():
    material = SimpleNamespace(
        history_text="", previous_answer="", uploads_text="", sources=[], tables=[_cut_table()], notes=[], transform={}, row_count=None
    )
    kept = artifact._material_from_dict(artifact._material_dict(material))
    assert kept.tables[0].partial


# ── Exports ──────────────────────────────────────────────────────────────────


def _export_table(warehouse, cell="ok"):
    _add(warehouse, "Note__c", _text("Id", "Name", "Reason__c"), [("a", "One", cell), ("b", "Two", "plain")])


def test_a_control_character_in_a_cell_costs_only_that_character(warehouse, monkeypatch, tmp_path):
    from openpyxl import load_workbook

    monkeypatch.setattr(settings, "reports_dir", str(tmp_path))
    _export_table(warehouse, "bad\x13cell")
    Model(monkeypatch, "SELECT Name, Reason__c FROM Note__c ORDER BY Name")
    _text_answer, meta = ask("export the notes to excel")
    sheet = load_workbook(tmp_path / meta["report_files"][0]["filename"]).active
    assert [cell.value for cell in sheet[2]] == ["One", "badcell"]


@pytest.mark.parametrize("cell", ["bad￾cell", "bad￿cell"])
def test_what_openpyxl_misses_cannot_break_the_file(tmp_path, cell):
    from openpyxl import load_workbook

    path, _cut = exports.export_xlsx(["a"], [[cell]], tmp_path, slug="x")
    assert load_workbook(path).active["A2"].value == "badcell"


def test_a_header_with_a_control_character_is_written_too(tmp_path):
    from openpyxl import load_workbook

    path, _cut = exports.export_xlsx(["a\x13b"], [["x"]], tmp_path, slug="x")
    assert load_workbook(path).active["A1"].value == "ab"


def test_a_failed_file_still_answers_and_says_so(warehouse, monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(settings, "reports_dir", str(tmp_path))
    _export_table(warehouse, "cell-text-of-a-person")

    def fails(*_args, **_kwargs):
        raise ValueError("Cannot convert cell-text-of-a-person")

    monkeypatch.setattr(sql_engine, "export_xlsx", fails)
    model = Model(monkeypatch, "SELECT Name, Reason__c FROM Note__c")
    text, meta = ask("export the notes to excel")
    assert "report_files" not in meta
    assert len(meta["data"]) == 2
    assert "The file could not be made, so none is attached." in text
    assert "No file could be made" in model.narrative_prompts[-1][0]["content"]
    assert "cell-text-of-a-person" not in caplog.text


def test_a_made_file_is_told_to_the_narrative(warehouse, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "reports_dir", str(tmp_path))
    model = Model(monkeypatch, "SELECT Subject FROM Task")
    ask("export the tasks to excel")
    assert "A file of this result is attached" in model.narrative_prompts[-1][0]["content"]


def test_a_failed_save_leaves_no_part_of_a_file(tmp_path, monkeypatch):
    from openpyxl.workbook.workbook import Workbook

    def half(_self, path):
        open(path, "wb").write(b"PK")
        raise OSError("disk full")

    monkeypatch.setattr(Workbook, "save", half)
    with pytest.raises(OSError):
        exports.export_xlsx(["a"], [["x"]], tmp_path, slug="x")
    assert not list(tmp_path.iterdir())


# ── Contact parts and words ──────────────────────────────────────────────────


@pytest.fixture()
def addresses(warehouse):
    columns = _text(
        "Id", "Name", "Current_Address__City__s", "Current_Address__StateCode__s", "Current_Address__CountryCode__s",
        "Current_Address__Street__s", "Current_Address__PostalCode__s", "Complete_Address__c", "Slack_User_Id__c",
    )
    _add(
        warehouse,
        "Applicant__c",
        columns,
        [
            ("a", "Invented One", "Northtown", "NJ", "US", "1 Elm Road", "99999", "1 Elm Road, Northtown", "U01"),
            ("b", "Invented Two", "Southtown", "NY", "US", "2 Oak Road", "99998", "2 Oak Road, Southtown", "U02"),
        ],
    )
    return warehouse


@pytest.mark.parametrize("part", ["Current_Address__City__s", "Current_Address__StateCode__s", "Current_Address__CountryCode__s"])
def test_a_count_per_place_keeps_its_label(addresses, monkeypatch, part):
    Model(monkeypatch, f"SELECT {part}, COUNT(*) AS n FROM Applicant__c GROUP BY 1")
    _text_answer, meta = ask("how many candidates per location?")
    assert list(meta["data"][0]) == [part, "n"]


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT Current_Address__Street__s, COUNT(*) AS n FROM Applicant__c GROUP BY 1",
        "SELECT Current_Address__PostalCode__s, COUNT(*) AS n FROM Applicant__c GROUP BY 1",
        "SELECT Complete_Address__c, COUNT(*) AS n FROM Applicant__c GROUP BY 1",
        "SELECT Slack_User_Id__c, COUNT(*) AS n FROM Applicant__c GROUP BY 1",
    ],
)
def test_a_street_stays_held_even_as_a_label(addresses, monkeypatch, statement):
    Model(monkeypatch, statement)
    assert ask("how many per location?")[1]["data"] == []


def test_a_named_city_question_answers_with_no_false_line(addresses, monkeypatch):
    Model(monkeypatch, "SELECT Current_Address__City__s FROM Applicant__c WHERE Name = 'Invented One'")
    text, meta = ask("which city does Invented One live in?")
    assert meta["data"] == [{"Current_Address__City__s": "Northtown"}]
    assert "were left out" not in text


def test_a_plain_profile_keeps_the_city_back(addresses, monkeypatch):
    Model(monkeypatch, "SELECT Name, Current_Address__City__s FROM Applicant__c WHERE Name = 'Invented One'")
    assert list(ask("who is Invented One?")[1]["data"][0]) == ["Name"]


def test_a_city_beside_a_persons_name_is_their_address(addresses, monkeypatch):
    Model(monkeypatch, "SELECT Name, Current_Address__City__s, COUNT(*) AS n FROM Applicant__c GROUP BY 1, 2")
    assert list(ask("how many per person?")[1]["data"][0]) == ["Name", "n"]


def test_a_contact_typed_into_a_city_stays_held(warehouse, monkeypatch):
    _add(warehouse, "Odd__c", _text("Id", "Current_Address__City__s"), [("a", "someone@example.test"), ("b", "+1 555 010 0003")])
    Model(monkeypatch, "SELECT Current_Address__City__s, COUNT(*) AS n FROM Odd__c GROUP BY 1")
    said = json.dumps(ask("how many per place?")[1].get("data"))
    assert "example.test" not in said and "555" not in said


def test_an_address_part_is_no_stored_value_for_the_writer():
    assert not sql_engine._is_value_column("Current_Address__City__s", "VARCHAR")


@pytest.mark.parametrize(
    "question",
    [
        "What is the telephone of Asha Varnok?",
        "telephone number of Asha Varnok",
        "landline of Asha Varnok",
        "uska mob no kya hai Asha Varnok",
    ],
)
def test_every_way_of_asking_for_a_phone_gets_it(warehouse, monkeypatch, question):
    Model(monkeypatch, "SELECT Name, Phone_No__c FROM Recruiter__c WHERE Name = 'Asha Varnok'")
    assert "Phone_No__c" in ask(question)[1]["data"][0]


@pytest.mark.parametrize("question", ["Which recruiters are in Tel Aviv?", "who is the recruiter for the tel company account"])
def test_a_town_is_no_request_for_a_phone(warehouse, monkeypatch, question):
    Model(monkeypatch, "SELECT Name, Phone_No__c FROM Recruiter__c")
    assert "Phone_No__c" not in ask(question)[1]["data"][0]


def test_the_line_claims_nothing_about_what_was_asked(warehouse, monkeypatch):
    Model(monkeypatch, "SELECT Name, Phone_No__c FROM Recruiter__c")
    text = ask("what are the phn details of them?")[0]
    assert "Contact details were left out: they are shown only when asked for by name, such as phone or e-mail." in text
    assert "did not ask" not in text


# ── Wording held back from a stream ──────────────────────────────────────────


SIZES = [0, 1, 2, 3, 4, 6, 8, 12, 20]


def _held(text, size):
    hold = sql_engine.WordingHold()
    pieces = [text] if not size else [text[i : i + size] for i in range(0, len(text), size)]
    return "".join(hold.feed(piece) for piece in pieces) + hold.flush()


LIVE_CLAIMS = [
    "The direct Salesforce check found no matching interviews.",
    "A live Salesforce check shows no open cases.",
    "According to the live Salesforce data there are no open cases.",
    "To confirm, the direct Salesforce check shows 145 open cases.",
    "Based on the direct Salesforce check, the figures differ by department.",
    "Based on the live Salesforce data, recruiters are behind target by 12.",
    "Based on the **direct Salesforce check**, there are 145 open cases.",
    "The live Salesforce data shows more offers than rejections.",
    "The direct Salesforce check shows fewer than 10 open cases.",
    "A live Salesforce query returned no rows.",
    "There are no open cases according to the direct Salesforce check.",
    "According to the live Salesforce data, I cannot find any open case.",
]


def test_a_claim_of_a_live_check_goes_whatever_stands_around_it():
    for sentence in LIVE_CLAIMS:
        for size in SIZES:
            said = _held(sentence, size).lower()
            assert "synced salesforce copy" in said, (sentence, size)
            assert "live salesforce" not in said and "direct salesforce" not in said, (sentence, size)


TRUE_REMARKS = [
    "If it were from the live Salesforce data, it would be newer.",
    "If this were based on the live Salesforce data, the count would be newer.",
    "The sync runs every 30 minutes, so a live Salesforce check shows newer data.",
    "If a live Salesforce check shows otherwise, trust that.",
    "Please see if a live Salesforce check confirms this.",
    "You can ask an admin whether a live Salesforce query shows the same.",
    "Only a live Salesforce check confirms the figure.",
    "To confirm, see what a live Salesforce check shows.",
    "To confirm, ask an admin whether a live Salesforce query shows the same.",
    "The figures may differ, since a live Salesforce check shows newer data.",
    "The figures may differ, because a live Salesforce query returns the current values.",
    "It can lag, and a live Salesforce check shows the current value.",
    "The copy can lag behind, as the live Salesforce data shows changes first.",
    "The copy is thirty minutes behind, while the live Salesforce data shows the current value.",
    "No live Salesforce check shows this.",
    "Not from the live Salesforce data.",
    "This answer isn't from a live Salesforce query.",
    "I cannot say, based on a live Salesforce check.",
]


def test_a_true_remark_about_the_live_data_stands():
    for sentence in TRUE_REMARKS:
        for size in SIZES:
            assert _held(sentence, size) == sentence, (sentence, size)


THE_BLOCKS_NAME = [
        ("Net balance per the computed figures: -450.00.", "-450.00"),
        ("Based on the computed figures, -450.00 is the net balance.", "-450.00"),
        ("Net balance, according to the computed figures, -450.00.", "-450.00"),
        ("Here is the breakdown per the computed figures:\n\n| Status | n |\n|---|---|\n| Open | 80 |", "\n\n| Status | n |"),
        ("Based on the computed figures:\n- Open: 80\n- Closed: 20", "\n- Open: 80"),
        ("There were 80 interviews, according to the computed figures.", "There were 80 interviews, according to the figures."),
        ("There were 80 interviews (per the computed figures).", "(per the figures)"),
        ("The total of 80 comes from the computed figures.", "comes from the figures."),
        ("**Based on the computed figures**, there were 80.", "**Based on the figures**, there were 80."),
        ("Based on the computed figures.", "Based on the figures."),
        ("Computed figures: 80 rows.", "Figures: 80 rows."),
        ("The computed block says 80.", "The figures says 80."),
        ("The card on file is ****1234", "The card on file is ****1234"),
        ("There was 1 row. Based on the computed figures, the contact details are:", "There was 1 row. Based on the figures, the contact details are:"),
        ("Based on the computed figures for all-time interviews, 5.", "For all-time interviews, 5."),
        ("based on the computed figures over all rows, 80.", "Over all rows, 80."),
        ("Based on the computed figures, a0IXx0000001AbCd is the record.", "a0IXx0000001AbCd is the record."),
        ("Based on the computed figures, x.y@example.test is the address.", "x.y@example.test is the address."),
]


def test_the_blocks_name_goes_and_nothing_else_does():
    for said, kept in THE_BLOCKS_NAME:
        for size in SIZES:
            out = _held(said, size)
            assert kept in out, (said, size, out)
            assert "computed" not in out.lower(), (said, size, out)


LONG_CLAIM = "Based on the direct Salesforce check, " + "the matching records are record 1 of the list, " * 40


def test_a_long_sentence_starts_to_show_and_is_rewritten_first():
    for size in (4, 20):
        hold = sql_engine.WordingHold()
        first = ""
        for start in range(0, len(LONG_CLAIM), size):
            first += hold.feed(LONG_CLAIM[start : start + size])
            if first:
                break
        assert first, "nothing shown for the whole of a long sentence"
        assert first.startswith("Based on the synced Salesforce copy")
        assert "direct" not in _held(LONG_CLAIM, size).lower()


def test_a_short_sentence_is_still_held_until_it_ends():
    hold = sql_engine.WordingHold()
    assert hold.feed("A live Salesforce check is ") == ""
    assert hold.feed("not available.") + hold.flush() == "A live Salesforce check is not available."


# ── The blank bucket on a typed column ───────────────────────────────────────


@pytest.fixture()
def typed(warehouse):
    _add(
        warehouse,
        "Slot__c",
        {**_text("Id"), "Minutes__c": "BIGINT", "Day__c": "DATE", "Ratio__c": "DECIMAL(5,2)", "Label__c": "VARCHAR"},
        [("a", "30", "2026-05-05", "1.5", "x"), ("b", "30", "2026-06-05", "1.5", "y"), ("c", None, None, None, None)],
    )
    return warehouse


def test_the_rule_gives_the_writer_a_form_that_runs_on_every_type(typed):
    assert "COALESCE(CAST(col AS VARCHAR), '(blank)')" in sql_engine.org_brief.SQL_HARD_RULES
    assert "GROUP BY col ORDER BY col" in sql_engine.org_brief.SQL_HARD_RULES
    for form in (
        "SELECT COALESCE(CAST(Minutes__c AS VARCHAR), '(blank)') AS m, COUNT(*) AS n FROM Slot__c GROUP BY Minutes__c ORDER BY Minutes__c",
        "SELECT COALESCE(strftime(date_trunc('month', Day__c), '%Y-%m'), '(blank)') AS month, COUNT(*) AS n FROM Slot__c "
        "GROUP BY date_trunc('month', Day__c) ORDER BY date_trunc('month', Day__c)",
    ):
        rows = sql_engine._execute(form, 10)[1]
        assert sum(row[1] for row in rows) == 3
        assert rows[-1][0] == "(blank)"


@pytest.mark.parametrize(
    "column, form",
    [
        ("Minutes__c", "COALESCE(CAST(Minutes__c AS VARCHAR), '(blank)') AS <label> ... GROUP BY Minutes__c ORDER BY Minutes__c"),
        ("Day__c", "COALESCE(CAST(Day__c AS VARCHAR), '(blank)') AS <label> ... GROUP BY Day__c ORDER BY Day__c"),
        ("Label__c", "COALESCE(Label__c, '(blank)')"),
    ],
)
def test_the_engines_own_hint_fits_the_type(typed, column, form):
    statement = f"SELECT {column}, COUNT(*) AS n FROM Slot__c WHERE {column} IS NOT NULL GROUP BY 1"
    advice = sql_engine._null_drops(statement, [column, "n"], "how many per slot?", schema())
    assert form in advice[0]


@pytest.mark.parametrize(
    "column", ["Minutes__c", "Day__c", "Ratio__c", "date_trunc('month', Day__c)"], ids=["bigint", "date", "decimal", "month"]
)
def test_the_retry_after_a_failed_label_is_told_the_form_that_runs(typed, monkeypatch, column):
    failed = f"SELECT COALESCE({column}, '(blank)') AS slot, COUNT(*) AS n FROM Slot__c GROUP BY 1"
    model = Model(monkeypatch, failed, "SELECT COUNT(*) AS n FROM Slot__c")
    _text_answer, meta = ask("how many per slot?")
    told = model.writer_user(1)
    assert "COALESCE(CAST(<col> AS VARCHAR), '(blank)')" in told
    assert "IS NOT NULL" not in told.split("Error:", 1)[1].split("Write a corrected")[0]
    assert meta["data"] == [{"n": 3}]


# ── A link to the org ────────────────────────────────────────────────────────


def _url_model(monkeypatch, *, answer="An answer.", stored=()):
    calls = {"prompts": []}

    async def stream_chat_events(messages, **_kwargs):
        calls["prompts"].append(messages)
        yield "token", answer

    async def in_thread(function, *_args, **_kwargs):
        return list(stored)

    monkeypatch.setattr(url_engine.llm, "stream_chat_events", stream_chat_events)
    monkeypatch.setattr(url_engine.db, "run_in_thread", in_thread)
    return calls


LINK = ORG_LINKS[0]
PASTE = "Please turn these into three action items. We agreed to move the launch, call the vendor and ship the notes."
EARLIER = [
    {"role": "user", "content": "how many open positions?"},
    {"role": "assistant", "content": "The third quarter hiring summary shows 12 open positions."},
]


def _ask_link(message, history=(), **kwargs):
    rec = Rec()
    text = asyncio.run(url_engine.run_url_engine(message, [LINK], "c", list(history), rec.emit, **kwargs))
    assert "".join(d["text"] for e, d in rec.events if e == "token") == text, "the stored text is what the stream said"
    return text, rec.meta()


@pytest.mark.parametrize(
    "message",
    [
        f"How do I reset my password on {LINK} ?",
        f"What does the Insufficient Privileges error on {LINK} usually mean?",
        f"{LINK} {PASTE}",
    ],
)
@pytest.mark.parametrize("salesforce", [True, False], ids=["salesforce", "assistant"])
def test_a_message_with_more_in_it_than_the_link_is_answered(the_org, monkeypatch, message, salesforce):
    calls = _url_model(monkeypatch)
    text, meta = _ask_link(message, EARLIER, salesforce=salesforce)
    assert len(calls["prompts"]) == 1
    prompt = json.dumps(calls["prompts"][0])
    assert "force.com" not in prompt, "the address is not repeated to the model"
    assert "12 open positions" not in prompt, "an earlier number must not answer about this link"
    assert "give no status, number, name or date" in prompt
    assert text.endswith("An answer.")
    assert text.startswith(url_engine.org_link_line(salesforce))
    assert meta["fetch_failed"] is True


def test_the_pasted_text_reaches_the_model(the_org, monkeypatch):
    calls = _url_model(monkeypatch)
    _ask_link(f"{LINK} {PASTE}")
    assert PASTE in calls["prompts"][0][-1]["content"]


@pytest.mark.parametrize(
    "message",
    [
        LINK,
        f"{LINK} , {LINK}",
        f"summarise this {LINK}",
        f"How many open positions does this report show? {LINK}",
        f"what is the status of this lead {LINK}",
        f"lead Acme Corp {LINK}",
    ],
)
@pytest.mark.parametrize("salesforce", [True, False], ids=["salesforce", "assistant"])
def test_a_message_about_the_record_behind_the_link_gets_only_the_line(the_org, monkeypatch, message, salesforce):
    calls = _url_model(monkeypatch)
    text, _meta = _ask_link(message, EARLIER, salesforce=salesforce)
    assert not calls["prompts"]
    assert text == url_engine.org_link_line(salesforce)


def test_the_line_offers_only_what_this_chat_can_do(the_org, monkeypatch):
    _url_model(monkeypatch)
    assert "I will look there" in _ask_link(LINK, salesforce=True)[0]
    said = _ask_link(LINK, salesforce=False)[0]
    assert "I will look there" not in said
    assert "turn on" not in said.lower(), "a member with the feature off has no such row in the menu"


def test_a_crawl_of_the_org_says_the_same_on_assistant_mode(the_org, monkeypatch):
    rec = Rec()
    said = asyncio.run(crawl.run_crawl_engine("crawl it", LINK, "c", [], rec.emit, salesforce=False))
    assert "I will look there" not in said


def test_earlier_pages_are_not_what_this_link_asked_about(the_org, monkeypatch):
    stored = [{"url": "https://example.com/a", "title": "A page", "text": "from the page"}]
    calls = _url_model(monkeypatch, stored=stored)
    text, _meta = _ask_link(f"How do I reset my password on {LINK} ?")
    assert "from the page" not in json.dumps(calls["prompts"])
    assert text.endswith("An answer.")
