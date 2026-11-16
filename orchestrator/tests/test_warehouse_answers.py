"""The warehouse route gives right numbers or says it cannot.

This route is what every member gets in Salesforce mode, and owner's order
2026-11-04 took away the live fallback that used to hide its faults. A real-model
run on the read-only snapshot (stage 0, 2026-11-05) found them: 22 of 40 right.
Each test here pins one fix with the model replaced: real DuckDB views, a fake SQL
writer, and the writer's stored replies where the run left one. What the model
itself does with the new rules is for the model re-test to show.

Every name and record below is invented.
"""
import asyncio
import csv
import json
import pathlib
from datetime import datetime, timezone

import duckdb
import pytest

from app.config import settings
from app.core import net
from app.core import org_brief as ob
from app.core.schema_cache import own_words, relevant_schema, same_object, schema_cache
from app.core.sf_intel import interpret, planner
from app.core.sf_intel import state as sf_state
from app.core.sf_intel.models import (
    AgentDecision,
    ClarificationDraft,
    ClarificationOption,
    ClarificationRound,
    PendingIntent,
    humanize,
)
from app.engines import agent, live_sf
from app.engines import sql as sql_engine
from app.engines import url as url_engine
from app.engines.sql import (
    WarehouseBusy,
    WarehouseUnreadable,
    deterministic_summary,
    generate_and_run_sql,
    run_sql_engine,
)
from app.memory_recall import format_recall_block

COULD_NOT_FIND = "I could not find that in the synced Salesforce copy"
COULD_NOT_WRITE = "I could not write a valid query"
CANNOT_BE_READ = "cannot be read right now"

RT_INTERVIEW = "012Xx0000001AAAIA1"
RT_INITIAL = "012Xx0000002AAAIA2"
DEPT_SALES = "a28Xx0000011AAAIA1"
DEPT_OPS = "a28Xx0000022AAAIA2"
PERSON_ONE = "a0RXx0000001AAAIA1"
PERSON_TWO = "a0RXx0000002AAAIA2"


def _create(con, table, columns, rows=()):
    """A raw VARCHAR table and a typed view over it, the shape sync-worker builds."""
    quoted = ", ".join(f'"{name}" VARCHAR' for name in columns)
    con.execute(f'CREATE TABLE raw."{table}" ({quoted})')
    if rows:
        marks = ", ".join("?" for _ in columns)
        con.executemany(f'INSERT INTO raw."{table}" VALUES ({marks})', [list(r) for r in rows])
    cast = ", ".join(
        f'"{name}"' if dtype == "VARCHAR" else f'TRY_CAST("{name}" AS {dtype}) AS "{name}"'
        for name, dtype in columns.items()
    )
    con.execute(f'CREATE VIEW main."{table}" AS SELECT {cast} FROM raw."{table}"')


def _text(*names):
    return {name: "VARCHAR" for name in names}


@pytest.fixture()
def warehouse(tmp_path, monkeypatch):
    path = str(tmp_path / "warehouse.duckdb")
    con = duckdb.connect(path)
    con.execute("CREATE SCHEMA raw")
    _create(con, "RecordType", _text("Id", "Name"), [(RT_INTERVIEW, "Interview"), (RT_INITIAL, "Initial Call")])
    _create(con, "Department__c", _text("Id", "Name"), [(DEPT_SALES, "Sales"), (DEPT_OPS, "Operations")])
    _create(
        con,
        "Recruiter__c",
        _text(
            "Id", "Name", "Email__c", "Phone_No__c", "Slack_User_Id__c", "Department__c",
            "Designation__c", "Assigned_Sr_Recruiter__c", "Manager__c", "COMPASS_State_JSON__c",
            "Portal_Password__c", "Employment_Status__c",
        ),
        [
            (PERSON_ONE, "Asha Varnok", "asha@example.test", "+1 555 010 0001", "U01", DEPT_SALES,
             "Lead", PERSON_TWO, None, '{"a":1}', "hunter2", "Active"),
            (PERSON_TWO, "Rovik Mendrell", "rovik@example.test", "+1 555 010 0002", "U02", DEPT_OPS,
             "Head", None, None, '{"a":2}', "hunter3", "Active"),
        ],
    )
    _create(
        con,
        "Interview__c",
        {
            **_text("Id", "Name", "RecordTypeId", "Round__c", "Status__c", "Interview_Outcome__c"),
            "Scheduled_Minutes__c": "BIGINT",
            "Date_of_Interview__c": "DATE",
        },
        [
            (f"a0IXx00000{i:02d}AAAIA{i % 10}", f"INT-{i}", RT_INTERVIEW if i % 4 else RT_INITIAL,
             "First" if i % 2 else "Second", "Completed" if i % 3 else None, "Ghosted" if i % 5 == 0 else None,
             "30", "2026-05-05")
            for i in range(20)
        ],
    )
    _create(
        con,
        "Invoice__c",
        {**_text("Id", "Name", "Invoice_Status__c"), "Outstanding_Amount__c": "DECIMAL(18,2)"},
        [("a0VXx0000001AAAIA1", "INV-1", "Paid", "0"), ("a0VXx0000002AAAIA2", "INV-2", "Not Paid", "100"),
         ("a0VXx0000003AAAIA3", "INV-3", None, "250")],
    )
    _create(
        con,
        "Background_Check__c",
        _text("Id", "Name", "Candidate__c", "Background_Check_Status__c"),
        [(f"a1BXx00000{i:02d}AAAIA{i % 10}", f"BC-{i}", PERSON_ONE, "Pending") for i in range(47)],
    )
    _create(con, "Case", _text("Id", "CaseNumber", "Case_Reason__c"), [("500Xx0000001AAAIA1", "1", "Escalation")])
    _create(con, "Internal_Interview__c", _text("Id", "Name", "Human_Decision__c"), [("a0JXx0000001AAAIA1", "M-1", "Pass")])
    _create(con, "Account", _text("Id", "Name", "Industry"), [("001Xx0000001AAAIA1", "Northwind", "Staffing")])
    _create(con, "Task", _text("Id", "Subject"), [("00TXx0000001AAAIA1", "Call")])
    con.execute("CREATE TABLE main._sync_meta (object_name VARCHAR)")
    con.close()
    monkeypatch.setattr(settings, "duckdb_path", path)
    schema_cache.invalidate()
    sql_engine._stored_values_cache.clear()
    yield path
    schema_cache.invalidate()
    sql_engine._stored_values_cache.clear()


class Model:
    """Queued writer replies, and a narrative that records what it was given."""

    def __init__(self, monkeypatch, *replies, narrative=("An answer.",)):
        self.replies = list(replies)
        self.narrative = list(narrative)
        self.writer_prompts = []
        self.narrative_prompts = []

        async def chat_completion(messages, **_kwargs):
            self.writer_prompts.append(messages)
            assert self.replies, "the writer was asked again with nothing left to say"
            return self.replies.pop(0)

        async def stream_chat_completion(messages, **_kwargs):
            self.narrative_prompts.append(messages)
            for token in self.narrative:
                yield token

        monkeypatch.setattr(sql_engine.llm, "chat_completion", chat_completion)
        monkeypatch.setattr(sql_engine.llm, "stream_chat_completion", stream_chat_completion)

    def writer_user(self, index=-1):
        return self.writer_prompts[index][-1]["content"]

    def narrative_user(self):
        return self.narrative_prompts[-1][-1]["content"]


class Rec:
    def __init__(self):
        self.events = []

    async def emit(self, event, data):
        self.events.append((event, data))

    def meta(self):
        return [d for e, d in self.events if e == "meta"][-1]


def ask(question, history=()):
    rec = Rec()
    text = asyncio.run(run_sql_engine(question, list(history), rec.emit))
    assert "".join(d["text"] for e, d in rec.events if e == "token") == text
    return text, rec.meta()


# ── F1: the retry gets the real columns on a view ────────────────────────────


def test_a_binder_error_on_a_view_carries_the_real_column_list(warehouse, monkeypatch):
    with pytest.raises(duckdb.BinderException) as caught:
        sql_engine._execute("SELECT r.Title__c FROM Recruiter__c r", 5)
    assert 'Values list "r"' in str(caught.value), "the fixture must really read a view"
    model = Model(
        monkeypatch,
        "SELECT r.Title__c FROM Recruiter__c r",
        "SELECT r.Designation__c FROM Recruiter__c r",
    )
    ask("what is the designation of every recruiter?")
    retry = model.writer_user()
    assert "COMPLETE column list of Recruiter__c" in retry
    assert "Designation__c" in retry.split("COMPLETE column list of Recruiter__c")[1]


def test_a_qualified_table_still_resolves_the_failing_alias():
    out = sql_engine._enriched_error(
        'Binder Error: Values list "r" does not have a column named "Foo"',
        "SELECT r.Foo FROM main.Recruiter__c r",
        {"Recruiter__c": [("Id", "VARCHAR"), ("Designation__c", "VARCHAR")]},
    )
    assert "Designation__c" in out


# ── F2: the person's own object is in the slice ──────────────────────────────


def test_the_object_a_person_lives_on_is_pinned_into_the_slice(warehouse, monkeypatch):
    real = schema_cache.get(settings.duckdb_path)
    filler = {f"Filler_{i:02d}__c": [(f"Col_{c}", "VARCHAR") for c in range(60)] for i in range(60)}
    monkeypatch.setattr(sql_engine.schema_cache, "get", lambda path: {**real, **filler})
    model = Model(monkeypatch, "SELECT Name FROM Recruiter__c")
    ask("what does Asha Varnok do?")
    assert "\nRecruiter__c(" in "\n" + model.writer_user(0)


# ── F3: an object with no copy gets no number ────────────────────────────────


def test_the_writer_is_told_how_to_say_nothing_holds_this():
    assert "reply with exactly NO_SUCH_OBJECT" in sql_engine._SQL_SYSTEM
    assert "Never substitute another object" in sql_engine._SQL_SYSTEM


@pytest.mark.parametrize(
    "reply",
    [
        "NO_SUCH_OBJECT",
        "```\nNO_SUCH_OBJECT\n```",
        # Reads as a known table to the check on statements, so only the sentinel stops it.
        "NO_SUCH_OBJECT: nothing from Account holds these.",
    ],
)
def test_the_sentinel_is_could_not_find_with_no_number(warehouse, monkeypatch, reply):
    Model(monkeypatch, reply)
    text, meta = ask("How many flux capacitors do we have?")
    assert COULD_NOT_FIND in text
    assert "data" not in meta


def test_the_sentinel_on_the_retry_is_could_not_find_too(warehouse, monkeypatch):
    Model(monkeypatch, "SELECT r.Title__c FROM Recruiter__c r", "NO_SUCH_OBJECT")
    text, _meta = ask("how many flux capacitors does each recruiter have?")
    assert COULD_NOT_FIND in text


#: The five no-copy questions of the real-model run. `reply` is the writer's stored
#: first reply, except n01: its stored reply counted Internal_Interview__c verdicts,
#: which no code rule can refuse without refusing real mock questions, so that one
#: depends on the writer using the new sentinel, and the model re-test decides it.
#: `general`: the planner handed the question back as needing no records, in its
#: own words where it left any.
NO_COPY = {
    "n01": dict(
        question="How many training mock results are there?",
        reply="NO_SUCH_OBJECT",
        general=None,
    ),
    "n02": dict(
        question="How many QMS escalation rules are configured?",
        reply="SELECT COUNT(*) AS QMS_Escalation_Rules_Count\nFROM \"Case\"\nWHERE Case_Reason__c ILIKE '%escalation%'",
        general=[
            "QMS (Quality Management System) escalation rules are not a standard Salesforce object or "
            "feature accessible via SOQL queries on the provided schema."
        ],
    ),
    "n03": dict(
        question="What is the maximum number of reminders in the Follow Up Setting?",
        reply="SELECT MAX(TRY_CAST(Scheduled_Minutes__c AS DOUBLE)) AS max_reminders\nFROM Interview__c",
        general=[],
    ),
    "n04": dict(
        question="How many content document links do we have in Salesforce?",
        reply="SELECT COUNT(*) AS total_content_document_links\nFROM ContentDocumentLink\nWHERE Id IS NOT NULL",
        general=None,
    ),
    "n05": dict(
        question="How many Salesforce reports does the org have?",
        reply=(
            'The schema provided does not include a standard Salesforce "Report" or "ReportDefinition" table. '
            "Therefore, I cannot query the number of Salesforce reports from the data."
        ),
        general=[],
    ),
}


@pytest.mark.parametrize("name", sorted(NO_COPY))
def test_the_five_no_copy_questions_replay_as_could_not_find(warehouse, monkeypatch, name):
    case = NO_COPY[name]
    model = Model(monkeypatch, case["reply"])

    async def turn():
        if case["general"] is not None:
            sql_engine.note_planner_handback(case["general"], not_a_record_question=True)
        rec = Rec()
        return await run_sql_engine(case["question"], [], rec.emit), rec.meta()

    text, meta = asyncio.run(turn())
    assert COULD_NOT_FIND in text
    assert "data" not in meta
    assert len(model.writer_prompts) == 1, "no second model call on an object nobody synced"


def test_the_planners_note_reaches_the_writer(warehouse, monkeypatch):
    model = Model(monkeypatch, "NO_SUCH_OBJECT")
    note = "Escalation rules are not accessible via SOQL on the provided schema."

    async def turn():
        sql_engine.note_planner_handback([note], not_a_record_question=True)
        return await run_sql_engine("How many escalation rules are configured?", [], Rec().emit)

    asyncio.run(turn())
    assert note in model.writer_user(0)
    assert "reply NO_SUCH_OBJECT" in model.writer_user(0)


def test_a_plan_with_a_not_accessible_assumption_passes_the_note_only(warehouse, monkeypatch):
    model = Model(monkeypatch, "SELECT COUNT(*) AS n FROM \"Case\"")

    async def turn():
        sql_engine.note_planner_handback(["Report objects are not in the provided schema.", "All time."])
        return await run_sql_engine("how many widgets are there?", [], Rec().emit)

    text = asyncio.run(turn())
    assert "Report objects are not in the provided schema." in model.writer_user(0)
    assert "All time." not in model.writer_user(0)
    assert COULD_NOT_FIND not in text, "only a hand-back as general refuses in code"


def test_the_note_is_for_one_turn_only(warehouse, monkeypatch):
    async def turn():
        sql_engine.note_planner_handback(["x is not accessible"], not_a_record_question=True)

    asyncio.run(turn())
    assert sql_engine._planner_handback.get() is None


def test_a_hand_back_still_answers_what_the_person_named(warehouse, monkeypatch):
    """The refusal is for a stand-in, not for every question the planner misjudged."""
    Model(monkeypatch, 'SELECT COUNT(*) AS n FROM "Case"')

    async def turn():
        sql_engine.note_planner_handback([], not_a_record_question=True)
        return await run_sql_engine("how many cases are there?", [], Rec().emit)

    assert COULD_NOT_FIND not in asyncio.run(turn())


def test_the_real_engine_carries_the_hand_back_to_the_writer(warehouse, monkeypatch):
    """Through sf_intel.run, the way main.py calls it, in one task."""
    from app.core.sf_intel import planner, tools
    from app.core.sf_intel.models import AgentDecision
    from app.engines import sf_intel

    tools.clear_caches()
    monkeypatch.setattr(settings, "salesforce_intelligence_enabled", True)
    monkeypatch.setattr(settings, "salesforce_planner_tool_calling", False)
    note = "Escalation rules are not accessible on the provided schema."

    async def plan(*_a, **_k):
        return AgentDecision(action="ANSWER_GENERAL", assumptions=[note])

    async def schema_summary(*_a, **_k):
        return "Case — fields: Id", ["Case"]

    monkeypatch.setattr(planner, "plan", plan)
    monkeypatch.setattr(tools, "get_salesforce_schema", schema_summary)
    model = Model(monkeypatch, 'SELECT COUNT(*) AS n FROM "Case"')

    async def turn():
        question = "How many QMS escalation rules are configured?"
        outcome = await sf_intel.run(question, [], Rec().emit, conversation_id="conv-answers")
        assert outcome.handled is False
        # main.py awaits sf_intel.run, then the graph runs the engine in a task of its own.
        return await asyncio.create_task(run_sql_engine(outcome.resolved_text, [], Rec().emit))

    text = asyncio.run(turn())
    assert note in model.writer_user(0)
    assert COULD_NOT_FIND in text


# ── F4: a zero is not an answer, and filters use what is stored ──────────────


@pytest.mark.parametrize(
    "columns, row",
    [(["total_interviews"], [0]), (["a_count", "b_count"], [0, 0])],
)
def test_a_one_row_figure_of_nothing_carries_the_warning(columns, row):
    computed = deterministic_summary(columns, [row], aggregated=True)
    assert computed["zero_figure"] is True
    assert "NOT evidence" in computed["what_zero_rows_means"]
    assert sql_engine.zero_rows_prefix(computed).startswith("The query ran and the figure came back 0")


def test_a_one_row_result_with_no_figure_says_nothing_came_back():
    computed = deterministic_summary(["total"], [[None]], aggregated=True)
    assert computed["nothing_back"] is True
    assert "zero_figure" not in computed
    assert "NOT evidence" in computed["what_zero_rows_means"]
    assert sql_engine.zero_rows_prefix(computed).startswith("The query ran and no figure came back")


@pytest.mark.parametrize(
    "columns, row",
    [(["n"], [5]), (["total", "passed"], [10, 0]), (["Name", "n"], ["x", 0])],
)
def test_a_real_figure_carries_no_warning(columns, row):
    assert sql_engine.zero_rows_prefix(deterministic_summary(columns, [row])) == ""


def test_a_count_of_zero_is_warned_about_on_the_route(warehouse, monkeypatch):
    Model(monkeypatch, "SELECT COUNT(*) AS n FROM Interview__c WHERE Date_of_Interview__c > DATE '2030-01-01'")
    text, _meta = ask("how many interviews after 2030?")
    assert text.startswith("The query ran and the figure came back 0")


def test_stored_values_reach_the_writer_for_a_column_the_question_names(warehouse, monkeypatch):
    model = Model(monkeypatch, "SELECT COUNT(*) AS n FROM Interview__c WHERE Round__c = 'First'")
    ask("how many first-round interviews?")
    assert "Interview__c.Round__c [First | Second]" in model.writer_user(0)


def test_stored_values_never_offer_a_person_or_a_way_to_reach_one(warehouse, monkeypatch):
    model = Model(monkeypatch, "SELECT COUNT(*) AS n FROM Recruiter__c")
    ask("how many recruiters by email, name and phone and status?")
    prompt = model.writer_user(0)
    assert "Recruiter__c.Employment_Status__c [Active]" in prompt
    for private in ("asha@example.test", "+1 555 010 0001", "Asha Varnok", PERSON_ONE):
        assert private not in prompt.split("Stored values")[1]


def test_a_filter_on_a_value_the_column_does_not_hold_gets_the_retry(warehouse, monkeypatch):
    model = Model(
        monkeypatch,
        "SELECT COUNT(*) AS n FROM Interview__c WHERE Round__c = '1'",
        "SELECT COUNT(*) AS n FROM Interview__c WHERE Round__c = 'First'",
    )
    text, meta = ask("how many first-round interviews?")
    retry = model.writer_user()
    assert "Round__c has no stored value '1'" in retry
    assert "First | Second" in retry
    assert meta["data"] == [{"n": 10}]
    assert "figure came back 0" not in text


def test_a_value_it_will_not_correct_is_named_beside_the_zero(warehouse, monkeypatch):
    statement = "SELECT COUNT(*) AS n FROM Interview__c WHERE Round__c = '1'"
    model = Model(monkeypatch, statement, statement)
    text, _meta = ask("how many round 1 interviews?")
    assert len(model.writer_prompts) == 2
    assert text.startswith("The query ran and the figure came back 0")
    assert "Round__c has no stored value '1'. It holds: First | Second." in text


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT COUNT(*) FROM Interview__c WHERE Round__c = 'First'",
        "SELECT COUNT(*) FROM Interview__c WHERE Round__c IN ('First', 'Second')",
        "SELECT COUNT(*) FROM Interview__c i WHERE i.Round__c ILIKE 'fir%'",
        "SELECT COUNT(*) FROM Interview__c WHERE Name = 'Nobody'",
    ],
)
def test_a_value_the_column_does_hold_costs_no_retry(warehouse, statement):
    schema = schema_cache.get(settings.duckdb_path)
    assert sql_engine._literal_misses(statement, schema) == []


# ── F6: no TO_CHAR ───────────────────────────────────────────────────────────


def test_duckdb_really_has_no_to_char_and_strftime_is_the_answer():
    con = duckdb.connect()
    for name in ("to_char", "to_varchar"):
        with pytest.raises(duckdb.CatalogException):
            con.execute(f"SELECT {name}(DATE '2026-05-05', 'YYYY-MM')")
    assert con.execute("SELECT strftime(DATE '2026-05-05', '%Y-%m')").fetchone()[0] == "2026-05"


def test_the_writer_is_told_before_and_after_it_fails():
    assert "NO TO_CHAR" in sql_engine._SQL_SYSTEM
    assert "strftime(<date col>, '%Y-%m')" in sql_engine._SQL_SYSTEM
    error = 'Catalog Error: Scalar Function with name to_char does not exist!\nDid you mean "to_hours"?'
    out = sql_engine._enriched_error(error, "SELECT TO_CHAR(x, 'YYYY') FROM t", {})
    assert "strftime(<date col>, '%Y-%m')" in out
    assert "to_hours" not in out


# ── F7: NULLs do not vanish silently ─────────────────────────────────────────


def test_not_equal_is_retried_when_it_really_drops_rows(warehouse, monkeypatch):
    """v03: `Invoice_Status__c != 'Paid'` lost the unpaid rows with no status."""
    model = Model(
        monkeypatch,
        "SELECT SUM(Outstanding_Amount__c) AS unpaid FROM Invoice__c WHERE Invoice_Status__c != 'Paid'",
        "SELECT SUM(Outstanding_Amount__c) AS unpaid FROM Invoice__c WHERE Invoice_Status__c IS DISTINCT FROM 'Paid'",
    )
    _text, meta = ask("how much is unpaid?")
    assert "also drops the 1 rows where Invoice_Status__c is NULL" in model.writer_user()
    assert float(meta["data"][0]["unpaid"]) == 350.0


def test_is_not_null_before_a_group_by_is_retried(warehouse, monkeypatch):
    """c02: `Status__c IS NOT NULL` lost the blank rows and the total left them out."""
    model = Model(
        monkeypatch,
        "SELECT Status__c, COUNT(*) AS n FROM Interview__c WHERE Status__c IS NOT NULL GROUP BY Status__c",
        "SELECT COALESCE(Status__c, '(blank)') AS status, COUNT(*) AS n FROM Interview__c GROUP BY 1",
    )
    _text, meta = ask("how many interviews by status?")
    assert "COALESCE(Status__c, '(blank)')" in model.writer_user()
    assert {row["status"] for row in meta["data"]} == {"Completed", "(blank)"}


@pytest.mark.parametrize(
    "question, statement",
    [
        (
            "top 5 statuses by interviews",
            "SELECT Status__c, COUNT(*) AS n FROM Interview__c WHERE Status__c IS NOT NULL GROUP BY 1 ORDER BY 2 DESC LIMIT 5",
        ),
        (
            "how many interviews by status, excluding blank ones?",
            "SELECT Status__c, COUNT(*) AS n FROM Interview__c WHERE Status__c IS NOT NULL GROUP BY 1",
        ),
        (
            "what are the mock outcomes?",
            "SELECT Human_Decision__c, COUNT(*) AS n FROM Internal_Interview__c WHERE Human_Decision__c IS NOT NULL GROUP BY 1",
        ),
        ("count interviews without a blank name", "SELECT COUNT(*) FROM Interview__c WHERE Name != ''"),
    ],
)
def test_a_filter_the_person_or_the_definition_asked_for_costs_no_retry(warehouse, question, statement):
    schema = schema_cache.get(settings.duckdb_path)
    columns, _rows = sql_engine._execute(statement, 50)
    assert sql_engine._null_drops(statement, columns, question, schema) == []


def test_a_group_with_no_label_is_named_in_the_figures():
    computed = deterministic_summary(["Status__c", "n"], [["Completed", 13], [None, 7]])
    assert computed["row_breakdown"] == {"Completed": 13, "(blank)": 7}


def test_the_writer_is_told_about_nulls():
    assert "IS DISTINCT FROM" in ob.SQL_HARD_RULES
    assert "COALESCE(col, '(blank)')" in ob.SQL_HARD_RULES


# ── F8 + M4: a LIMIT nobody asked for is not the total ───────────────────────


def test_a_limit_nobody_asked_for_says_first_30_of_47(warehouse, monkeypatch):
    model = Model(monkeypatch, "SELECT Name FROM Background_Check__c WHERE Background_Check_Status__c = 'Pending' LIMIT 30")
    text, meta = ask("which background checks are pending?")
    assert text.startswith("These are the first 30 of 47 matching rows.")
    assert len(meta["data"]) == 30
    assert '"true_total_rows": 47' in model.narrative_user()
    assert "the FIRST 30 rows of a larger result of 47 total rows" in model.narrative_user()


@pytest.mark.parametrize(
    "question, limit",
    [
        ("top 5 background checks", 5),
        ("show me 5 background checks", 5),
        ("which is the latest background check?", 1),
    ],
)
def test_a_cut_the_person_asked_for_is_the_answer(warehouse, monkeypatch, question, limit):
    Model(monkeypatch, f"SELECT Name FROM Background_Check__c LIMIT {limit}")
    text, _meta = ask(question)
    assert "These are the first" not in text


def test_a_limit_that_cut_nothing_says_nothing(warehouse, monkeypatch):
    Model(monkeypatch, "SELECT Name FROM Background_Check__c LIMIT 100")
    assert "These are the first" not in ask("which background checks are pending?")[0]


def test_the_stale_30_row_rule_is_gone():
    assert "first 30" not in ob.SQL_HARD_RULES
    assert "only 30" not in ob.grounding_for("give me details for this candidate's training")
    assert "the first 120 rows reach" in ob.SQL_HARD_RULES
    assert "Never add a LIMIT" in ob.SQL_HARD_RULES


# ── F9: names, not record ids ────────────────────────────────────────────────

GROUPED_BY_ID = "SELECT Department__c AS Department, COUNT(*) AS n FROM Recruiter__c GROUP BY 1 ORDER BY 2"


def test_a_group_labelled_with_ids_gets_a_join_hint_read_from_the_data(warehouse, monkeypatch):
    model = Model(
        monkeypatch,
        GROUPED_BY_ID,
        "SELECT d.Name AS Department, COUNT(*) AS n FROM Recruiter__c r "
        "LEFT JOIN Department__c d ON r.Department__c = d.Id GROUP BY 1 ORDER BY 2",
    )
    _text, meta = ask("how many employees per department?")
    assert "Department holds raw record Ids" in model.writer_user()
    assert "LEFT JOIN Department__c" in model.writer_user()
    assert {row["Department"] for row in meta["data"]} == {"Sales", "Operations"}


def test_ids_the_retry_kept_are_replaced_by_names_in_code(warehouse, monkeypatch):
    model = Model(monkeypatch, GROUPED_BY_ID, GROUPED_BY_ID)
    text, meta = ask("how many employees per department?")
    assert {row["Department"] for row in meta["data"]} == {"Sales", "Operations"}
    assert DEPT_SALES not in model.narrative_user()
    assert DEPT_SALES not in text


def test_a_lookup_asked_for_is_retried_as_its_name(warehouse, monkeypatch):
    statement = "SELECT Name, Department__c FROM Recruiter__c WHERE Name = 'Asha Varnok'"
    model = Model(
        monkeypatch,
        statement,
        "SELECT r.Name, d.Name AS Department FROM Recruiter__c r LEFT JOIN Department__c d "
        "ON r.Department__c = d.Id WHERE r.Name = 'Asha Varnok'",
    )
    _text, meta = ask("which department is Asha Varnok in?")
    assert meta["data"] == [{"Name": "Asha Varnok", "Department": "Sales"}]
    assert len(model.writer_prompts) == 2


def test_a_lookup_nobody_asked_about_costs_no_retry(warehouse, monkeypatch):
    model = Model(monkeypatch, "SELECT Name, Department__c FROM Recruiter__c")
    _text, meta = ask("list the recruiters")
    assert len(model.writer_prompts) == 1
    assert list(meta["data"][0]) == ["Name"]


def test_the_reports_to_field_is_named():
    assert "Recruiter__c.Assigned_Sr_Recruiter__c" in ob.ORG_RULES
    assert "Manager__c is filled" in ob.ORG_RULES
    assert "Department__c                     -> Department__c.Name" in ob.SQL_HARD_RULES


# ── F11: a reserved alias ────────────────────────────────────────────────────


def test_the_alias_at_is_renamed_and_the_query_runs(warehouse, monkeypatch):
    Model(monkeypatch, "SELECT COUNT(at.Id) AS n FROM Interview__c at WHERE at.Round__c = 'First'")
    _text, meta = ask("how many first-round interviews?")
    assert meta["data"] == [{"n": 10}]


@pytest.mark.parametrize(
    "statement, expected",
    [
        ("SELECT at.Id FROM t AS at", "SELECT at_t.Id FROM t AS at_t"),
        ('SELECT to.Id FROM "t" to', 'SELECT to_t.Id FROM "t" to_t'),
        ("SELECT at.Id FROM t at WHERE at.x = 'at.y'", "SELECT at_t.Id FROM t at_t WHERE at_t.x = 'at.y'"),
        (
            "SELECT 1 FROM t at WHERE at.d = (now() AT TIME ZONE 'Asia/Kolkata')::date",
            "SELECT 1 FROM t at_t WHERE at_t.d = (now() AT TIME ZONE 'Asia/Kolkata')::date",
        ),
        # Nothing to rename: these are syntax, not aliases.
        ("SELECT a.Id FROM t a LEFT JOIN u b ON a.Id = b.Id", None),
        ("SELECT (now() AT TIME ZONE 'x')::date FROM t ORDER BY 1", None),
    ],
)
def test_only_a_declared_and_dotted_alias_is_renamed(statement, expected):
    assert sql_engine._rename_reserved_aliases(statement) == (expected or statement)


def test_any_other_syntax_error_tells_the_retry_about_aliases_and_quotes():
    out = sql_engine._enriched_error('Parser Error: syntax error at or near "Case"', "SELECT 1 FROM Case", {})
    assert "AT, TO" in out
    assert '"Case"' in out


# ── F12: the typed views ─────────────────────────────────────────────────────


def test_the_prompt_says_what_the_views_are():
    for rules in (ob.SQL_HARD_RULES, sql_engine._SQL_SYSTEM):
        assert "EVERY column" not in rules
        assert "Trust the column types the schema shows" in rules
    assert "never with != ''" in ob.SQL_HARD_RULES


def test_a_conversion_error_says_the_column_is_typed(warehouse):
    with pytest.raises(duckdb.ConversionException) as caught:
        sql_engine._execute("SELECT 1 FROM Interview__c WHERE Scheduled_Minutes__c != ''", 5)
    out = sql_engine._enriched_error(str(caught.value), "", {})
    assert "That column is typed" in out
    assert "IS NOT NULL" in out


# ── F14 and M1: the engine says what was counted ─────────────────────────────


def test_the_line_of_what_was_counted_comes_from_the_filters(warehouse, monkeypatch):
    model = Model(
        monkeypatch,
        "SELECT COUNT(*) AS n FROM Interview__c i JOIN RecordType rt ON i.RecordTypeId = rt.Id "
        "WHERE rt.Name = 'Interview' AND i.Round__c = 'First' AND TRY_CAST(i.Date_of_Interview__c AS DATE) "
        "BETWEEN DATE '2026-01-01' AND DATE '2026-12-31'",
    )
    text, _meta = ask("how many first-round interviews this year?")
    line = text.split("\n\n")[-1]
    assert line.startswith("Scope: Interview__c, where RecordType.Name = 'Interview' and Round__c = 'First'")
    assert "Date_of_Interview__c BETWEEN DATE '2026-01-01' AND DATE '2026-12-31'" in line
    assert "every Interview__c record type" not in line
    assert line in model.narrative_user()
    assert "describe no other filter" in model.narrative_user()


def test_no_round_filter_means_the_line_shows_no_round_filter(warehouse, monkeypatch):
    """d03: "N first-round client interviews" with no round filter in the SQL."""
    Model(monkeypatch, "SELECT COUNT(*) AS n FROM Interview__c", narrative=("10 first-round interviews.",))
    text, _meta = ask("how many first-round interviews?")
    assert text.endswith(
        "Scope: every row of Interview__c. That covers every Interview__c record type, initial calls included."
    )


def test_a_blank_dropping_filter_says_so(warehouse, monkeypatch):
    schema = schema_cache.get(settings.duckdb_path)
    line = sql_engine._counted_line("SELECT 1 FROM Invoice__c i WHERE i.Invoice_Status__c != 'Paid'", schema)
    assert line == "Scope: Invoice__c, where Invoice_Status__c != 'Paid' (rows with no Invoice_Status__c are left out too)."
    line = sql_engine._counted_line("SELECT 1 FROM Invoice__c WHERE Invoice_Status__c IS NOT NULL", schema)
    assert line == "Scope: Invoice__c, where Invoice_Status__c is filled in."


def test_an_aggregate_filter_and_a_cte_are_both_read():
    schema = {"Deliverable_Result__c": [("Id", "VARCHAR")]}
    line = sql_engine._counted_line(
        "WITH r AS (SELECT * FROM Deliverable_Result__c WHERE Result_Status__c IN ('Pass', 'Fail')) "
        "SELECT COUNT(*) FILTER (WHERE Result_Status__c = 'Pass') FROM r",
        schema,
    )
    assert "Result_Status__c IN ('Pass', 'Fail')" in line
    assert "Result_Status__c = 'Pass'" in line


def test_an_interview_count_follows_the_pack():
    """M1: one prompt held both rules and the model obeyed the wrong one."""
    pack = (pathlib.Path(__file__).resolve().parents[2] / "brain/packs/kb-external-interviews.yaml").read_text(encoding="utf-8")
    assert "NEVER filter interviews by record-type NAME" in pack
    assert "filter to record" not in ob.ORG_RULES
    assert "NEVER filter interviews by record-type name" in ob.ORG_RULES
    assert ob.METRICS[0]["name"] == "interviews conducted"
    assert "RecordType" not in ob.METRICS[0]["sql"]
    assert "every record type" in ob.METRICS[0]["caveat"]


def test_one_line_reverses_the_interview_decision():
    """The owner can still choose the other rule. This is that line."""
    source = pathlib.Path(ob.__file__).read_text(encoding="utf-8")
    line = 'INTERVIEW_COUNT_RECORD_TYPE = ""'
    assert source.count(line) == 1
    namespace = {"__name__": "org_brief_reversed"}
    exec(compile(source.replace(line, 'INTERVIEW_COUNT_RECORD_TYPE = "Interview"'), "org_brief", "exec"), namespace)
    assert "filter to record\n  type 'Interview'" in namespace["ORG_RULES"]
    assert "rt.Name = 'Interview'" in namespace["METRICS"][0]["sql"]
    assert "Excludes 'Initial Call'" in namespace["METRICS"][0]["caveat"]


# ── M2: labels do not rank tables ────────────────────────────────────────────


def _polluted_schema():
    schema = {f"Business_{i:02d}__c": [("Id", "VARCHAR"), ("Interview__c", "VARCHAR")] for i in range(5)}
    schema["Interview__c"] = [("Id", "VARCHAR"), ("Status__c", "VARCHAR")]
    for name in ("AIMetric", "EngagementMetric", "ChannelMetric", "DuplicateRecordItem", "IdentityResolution"):
        schema[name] = [("Id", "VARCHAR")]
    return schema


@pytest.mark.parametrize("line", ["metric: all", "record identity: yes", "metric: all; date range: all time"])
def test_a_clarified_line_ranks_nothing(line):
    schema = _polluted_schema()
    plain = relevant_schema(schema, "how many interviews are there?")
    with_line = relevant_schema(schema, f"how many interviews are there?\n\n(Clarified: {line})")
    assert list(with_line) == list(plain)
    assert "AIMetric" not in with_line


def test_the_label_is_cut_wherever_the_agent_route_puts_it():
    assert own_words("q one\n\n(Clarified: metric: all)\nstep two") == "q one\nstep two"
    assert own_words("q one") == "q one"


def test_a_real_table_named_by_the_line_is_still_pinned(warehouse, monkeypatch):
    real = schema_cache.get(settings.duckdb_path)
    filler = {f"Filler_{i:02d}__c": [(f"Col_{c}", "VARCHAR") for c in range(60)] for i in range(60)}
    zeta = {"Zeta_Object__c": [("Id", "VARCHAR")]}
    monkeypatch.setattr(sql_engine.schema_cache, "get", lambda path: {**real, **filler, **zeta})
    model = Model(monkeypatch, "SELECT Name FROM Department__c")
    ask("what do we have?\n\n(Clarified: object: Zeta_Object__c)")
    assert "\nZeta_Object__c(" in "\n" + model.writer_user(0)


def test_the_writer_still_reads_the_line(warehouse, monkeypatch):
    model = Model(monkeypatch, "SELECT COUNT(*) AS n FROM Interview__c")
    ask("how many interviews?\n\n(Clarified: metric: all interviews)")
    assert "(Clarified: metric: all interviews)" in model.writer_user(0)


# ── M3: the computed block means what it says ────────────────────────────────


def test_a_grouped_row_with_two_labels_gets_no_occurrence_counts():
    """c11: {"value": "OPT", "count": 1, "percent_of_non_empty": 14.29}, quoted as
    "each of the 7 statuses represents approximately 14.29%"."""
    rows = [["Submitted", f"Visa{i}", 10 + i] for i in range(7)]
    computed = deterministic_summary(["Status__c", "Visa__c", "count_forms"], rows)
    assert "value_counts" not in computed
    assert computed["numeric_totals"]["count_forms"]["sum"] == sum(10 + i for i in range(7))
    assert "carries its own figures" in computed["counts_cover"]


def test_one_row_gets_no_occurrence_counts():
    """v03: total_rows 1, quoted as "Number of Invoices: 1" when it was more."""
    computed = deterministic_summary(["Invoice_Status__c", "total_unpaid"], [["Not Paid", 1234.5]])
    assert "value_counts" not in computed
    assert "not a count of records" in computed["counts_cover"]
    assert "aggregate_result" not in computed


def test_a_list_of_records_keeps_its_counts():
    rows = [[f"c{i}", 100 + i, "Cleared" if i % 3 else "Failed"] for i in range(60)]
    computed = deterministic_summary(["Name", "Amount", "Outcome"], rows)
    assert computed["value_counts"]["Outcome"]["denominator"] == 60


def test_the_narrative_is_told_not_to_name_the_block():
    system = sql_engine._narrative_messages("q", ["a"], [["b"]], [])[0]["content"]
    assert "never name it to the reader" in system


@pytest.mark.parametrize(
    "tokens, expected",
    [
        (["Based on the computed figures, there were **70** interviews."], "There were **70** interviews."),
        (["Based on the comp", "uted fig", "ures, there", " were 70."], "There were 70."),
        (["The computed figures confirm that both are right."], "The figures confirm that both are right."),
        (["Summary Statistics (from Computed Figures):\n* 5"], "Summary Statistics:\n* 5"),
        (["Based on the computed figures for all-time interviews, 5."], "For all-time interviews, 5."),
        (["Nothing internal here, so nothing changes at all in this text."], "Nothing internal here, so nothing changes at all in this text."),
    ],
)
def test_the_streamed_answer_never_names_the_block(tokens, expected):
    async def stream():
        for token in tokens:
            yield token

    async def collect():
        return "".join([part async for part in sql_engine._without_block_wording(stream())])

    assert asyncio.run(collect()) == expected


# ── M5: an earlier answer is no evidence ─────────────────────────────────────


def _recall_block():
    return format_recall_block(
        [{"title": "Mocks", "snippet": "There are **19** training mock results.", "role": "assistant"}]
    )


def test_recalled_answers_reach_neither_the_writer_nor_the_narrative(warehouse, monkeypatch):
    model = Model(monkeypatch, "SELECT COUNT(*) AS n FROM Interview__c")
    history = [
        {"role": "system", "content": _recall_block()},
        {"role": "system", "content": "Facts the user saved: prefers tables."},
        {"role": "user", "content": "how many interviews last week?"},
        {"role": "assistant", "content": "Twelve."},
    ]
    ask("and this week?", history)
    for messages in (model.writer_prompts[0], model.narrative_prompts[0]):
        assert not any("19" in m["content"] and "training mock" in m["content"] for m in messages)
        assert any(m["content"] == "Twelve." for m in messages), "this conversation's turns stay"
        assert any("Facts the user saved" in m["content"] for m in messages), "other pinned blocks stay"


# ── M6: asked for by name, shown plainly ─────────────────────────────────────


def test_work_contact_fields_asked_for_by_name_may_be_shown():
    assert "give it plainly, with no\n  refusal wording" in ob.ANSWER_RULES
    # Credentials stay refused, in the same rule.
    assert "passwords" in ob.ANSWER_RULES and "SSN" in ob.ANSWER_RULES


def test_the_e_mail_and_phone_asked_for_reach_the_payload(warehouse, monkeypatch):
    model = Model(monkeypatch, "SELECT Email__c, Phone_No__c FROM Recruiter__c WHERE Name = 'Asha Varnok'")
    _text, meta = ask("can you give me Asha Varnok's email and phone number?")
    assert meta["data"] == [{"Email__c": "asha@example.test", "Phone_No__c": "+1 555 010 0001"}]
    assert "asha@example.test" in model.narrative_user()


# ── M7: what the question did not name does not leave the engine ─────────────

EVERYTHING = "SELECT * FROM Recruiter__c WHERE Name = 'Asha Varnok'"
PRIVATE = ("asha@example.test", "+1 555 010 0001", "U01", "hunter2", '"a":1', PERSON_ONE, PERSON_TWO)


def test_an_unasked_profile_keeps_no_contact_detail_and_no_record_id(warehouse, monkeypatch):
    model = Model(monkeypatch, EVERYTHING)
    text, meta = ask("what does Asha Varnok do?")
    row = meta["data"][0]
    assert set(row) == {"Name", "Designation__c", "Manager__c", "Employment_Status__c"}
    narrative = model.narrative_user().split("Result sample")[1]
    for private in PRIVATE:
        assert private not in str(row)
        assert private not in narrative
    assert "Contact details and record ids were left out" in text
    assert "Credential fields are never shown." in text


def test_what_is_left_out_is_gone_from_the_figures_too(warehouse, monkeypatch):
    model = Model(monkeypatch, "SELECT Name, Phone_No__c, Slack_User_Id__c FROM Recruiter__c")
    ask("list the recruiters")
    figures = model.narrative_user().split("Computed figures")[1].split("Result sample")[0]
    assert "555" not in figures and "U01" not in figures


def test_generate_and_run_sql_holds_back_for_every_caller(warehouse, monkeypatch):
    """The agent step, the report section and the artifact table all get this."""
    Model(monkeypatch, EVERYTHING, EVERYTHING)
    sql, columns, rows = asyncio.run(
        generate_and_run_sql("select the recruiter", grounding_question="what does Asha Varnok do?")
    )
    assert sql == EVERYTHING
    assert "Phone_No__c" not in columns and "Id" not in columns
    assert all(private not in str(rows) for private in PRIVATE)


def test_the_planners_words_do_not_unlock_contact_details(warehouse, monkeypatch):
    """The step text is the planner's; only the person's own words can ask."""
    Model(monkeypatch, "SELECT Name, Email__c FROM Recruiter__c")
    _sql, columns, _rows = asyncio.run(
        generate_and_run_sql("list names and email", grounding_question="who works here?")
    )
    assert columns == ["Name"]


def test_a_clarified_line_does_not_unlock_contact_details(warehouse, monkeypatch):
    """Up to now a planner guess can write this line, not only a click."""
    Model(monkeypatch, "SELECT Name, Email__c FROM Recruiter__c")
    _text, meta = ask("who works here?\n\n(Clarified: result format: email and phone)")
    assert list(meta["data"][0]) == ["Name"]


def test_an_export_carries_only_what_was_asked_for(warehouse, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "reports_dir", str(tmp_path))
    Model(monkeypatch, "SELECT Name, Email__c, Phone_No__c, Id FROM Recruiter__c")
    _text, meta = ask("export the recruiters to csv")
    exported = (tmp_path / meta["report_files"][0]["filename"]).read_text(encoding="utf-8")
    assert list(csv.reader(exported.splitlines()))[0] == ["Name"]
    assert "@" not in exported and "555" not in exported and PERSON_ONE not in exported


def test_a_chart_is_only_ever_built_from_what_is_left(warehouse, monkeypatch):
    seen = {}

    async def fake_attach(meta, message, columns, rows, title=""):
        seen["columns"], seen["rows"] = list(columns), list(rows)

    monkeypatch.setattr(sql_engine, "attach_chart", fake_attach)
    Model(monkeypatch, "SELECT Name, Email__c FROM Recruiter__c")
    ask("chart the recruiters")
    assert seen["columns"] == ["Name"]
    assert "asha@example.test" not in str(seen["rows"])


@pytest.mark.parametrize(
    "column, value",
    [
        ("Alt_Mobile__c", "9876500000"),
        ("Personal_EMail_2__c", "x@example.test"),
        ("WhatsApp_No__c", "12345"),
        ("LinkedIn_Handle__c", "someone"),
        ("Home_Address__c", "1 Example Road"),
        ("Emergency_Contact__c", "Sam"),
        # Nothing in these names says contact: their values do.
        ("Reach_Me_At__c", "x@example.test"),
        ("Night_Line__c", "+91 98765 43210"),
    ],
)
def test_a_new_contact_looking_column_fails_closed(column, value):
    result = sql_engine._hold_back("SELECT 1", ["Name", column], [["Asha", value]], "what does Asha do?", {})
    assert result[1] == ["Name"]
    assert result.left_out["contact"] == [column]


@pytest.mark.parametrize(
    "column, value, question",
    [
        ("Email__c", "x@example.test", "what is her email?"),
        ("Email__c", "x@example.test", "what is her e-mail?"),
        ("Email__c", "x@example.test", "give me her contact details"),
        ("Phone_No__c", "+91 98765 43210", "her phone number please"),
        ("Phone_No__c", "+91 98765 43210", "uska mobile number kya hai"),
        ("Slack_User_Id__c", "U01", "what is his slack id?"),
        ("Personal_Email__c", "x@example.test", "give me Personal_Email__c"),
        ("Telegram_Handle__c", "someone", "her telegram handle"),
    ],
)
def test_what_the_question_names_is_kept(column, value, question):
    assert sql_engine._hold_back("SELECT 1", ["Name", column], [["Asha", value]], question, {})[1] == ["Name", column]


@pytest.mark.parametrize(
    "column, value, question",
    [
        ("Email__c", "x@example.test", "what is her phone?"),
        ("Phone_No__c", "+91 98765 43210", "what is her email?"),
        ("Slack_User_Id__c", "U01", "what is her email?"),
    ],
)
def test_naming_one_kind_does_not_unlock_another(column, value, question):
    assert sql_engine._hold_back("SELECT 1", ["Name", column], [["Asha", value]], question, {})[1] == ["Name"]


@pytest.mark.parametrize(
    "column", ["Portal_Password__c", "SSN__c", "Passport_Number__c", "Bank_Account_No__c", "API_Key__c", "PAN_No__c"]
)
def test_a_credential_is_never_shown_even_by_name(column):
    result = sql_engine._hold_back("SELECT 1", ["Name", column], [["Asha", "s3cret"]], f"give me {column}", {})
    assert result[1] == ["Name"]
    assert result.left_out["credentials"] == [column]


@pytest.mark.parametrize(
    "column, value",
    [
        ("Email_Sent__c", True),
        ("Last_Contact_Date__c", "2026-05-05"),
        ("Contact_Count", 12),
        ("Candidate_Status__c", "Active"),
        ("Cancellation_Reason__c", "Client"),
        ("Company_Name__c", "Northwind"),
    ],
)
def test_what_only_sounds_like_contact_is_not_held_back(column, value):
    assert sql_engine._hold_back("SELECT 1", ["Name", column], [["Asha", value]], "show this", {})[1] == ["Name", column]


def test_a_record_id_is_kept_only_when_asked_for():
    row = [["Asha", PERSON_ONE]]
    assert sql_engine._hold_back("SELECT 1", ["Name", "Id"], row, "who is she?", {})[1] == ["Name"]
    assert sql_engine._hold_back("SELECT 1", ["Name", "Id"], row, "what is her record id?", {})[1] == ["Name", "Id"]
    assert sql_engine._hold_back("SELECT 1", ["Name", "Id"], row, "what is her slack id?", {})[1] == ["Name"]
    assert sql_engine._hold_back("SELECT 1", ["Name", "Id"], row, "what is her email id?", {})[1] == ["Name"]
    assert sql_engine._hold_back("SELECT 1", ["Name", "Owner__c"], row, "who is she?", {})[1] == ["Name"]


def test_a_wide_result_is_capped_on_the_columns_the_question_points_at():
    columns = [f"Col_{i}__c" for i in range(47)] + ["Designation__c"]
    row = [[f"v{i}" for i in range(48)]]
    result = sql_engine._hold_back("SELECT *", columns, row, "what is her designation?", {})
    assert len(result[1]) == sql_engine._MAX_RESULT_COLUMNS == 20
    assert "Designation__c" in result[1]
    assert len(result.left_out["wide"]) == 28
    assert len(result[2][0]) == 20


def test_an_export_is_not_capped_by_width():
    columns = [f"Col_{i}__c" for i in range(48)]
    result = sql_engine._hold_back("SELECT *", columns, [["v"] * 48], "export this to excel", {})
    assert len(result[1]) == 48


def test_a_result_of_nothing_but_contact_details_says_so(warehouse, monkeypatch):
    model = Model(monkeypatch, "SELECT Email__c FROM Recruiter__c WHERE Name = 'Asha Varnok'")
    text, meta = ask("what does Asha Varnok do?")
    assert "Contact details were left out" in text
    assert "matched no rows" not in text
    assert meta["data"] == []
    assert model.narrative_prompts == [], "nothing is left to narrate"


def test_nothing_held_back_leaves_the_result_untouched():
    columns, rows = ["Name", "n"], [["a", 1]]
    result = sql_engine._hold_back("SELECT 1", columns, rows, "q", {})
    assert result[1] is columns and result[2] is rows and result.left_out == {}


def test_a_result_still_unpacks_as_three():
    sql, columns, rows = sql_engine.SqlResult("s", ["a"], [[1]], {"contact": ["b"]})
    assert (sql, columns, rows) == ("s", ["a"], [[1]])


# ── M11: an identical retry is not a retry ───────────────────────────────────

FAILING = "SELECT r.Department FROM Recruiter__c r"


def _count_executions(monkeypatch):
    runs = []
    real = sql_engine._execute

    def counted(sql, cap, **kwargs):
        runs.append(sql)
        return real(sql, cap, **kwargs)

    monkeypatch.setattr(sql_engine, "_execute", counted)
    return runs


def test_a_repeated_statement_is_not_run_twice_and_the_retry_is_not_spent(warehouse, monkeypatch):
    runs = _count_executions(monkeypatch)
    model = Model(
        monkeypatch,
        FAILING,
        f"  {FAILING}\n;",
        "SELECT r.Department__c FROM Recruiter__c r",
    )
    _text, meta = ask("how many employees per department?")
    assert sum(1 for sql in runs if sql.endswith(FAILING)) == 1
    assert "repeated the failed statement exactly" in model.writer_user(2)
    assert meta.get("sql") == "SELECT r.Department__c FROM Recruiter__c r"


def test_a_writer_that_keeps_repeating_it_ends_the_turn_honestly(warehouse, monkeypatch):
    runs = _count_executions(monkeypatch)
    model = Model(monkeypatch, FAILING, FAILING, FAILING)
    text, meta = ask("how many employees per department?")
    assert sum(1 for sql in runs if sql.endswith(FAILING)) == 1
    assert len(model.writer_prompts) == 3
    assert COULD_NOT_WRITE in text
    assert meta["salesforce_error"] == sql_engine.QUERY_REFUSED
    assert meta["salesforce_error_kind"] == "Binder"


# ── A damaged warehouse is not a busy one ────────────────────────────────────


@pytest.mark.parametrize(
    "message, locked",
    [
        ('IO Error: Could not set lock on file "/w.duckdb": Conflicting lock is held in python (PID 1)', True),
        ("Conflicting lock is held in python", True),
        ("IO Error: Corrupt database file: computed checksum 1 does not match stored checksum 2 in block at location 12288", False),
        ('IO Error: The file "/w.duckdb" exists, but it is not a valid DuckDB database file!', False),
        ("Binder Error: column block not found", False),
    ],
)
def test_only_a_real_lock_reads_as_a_lock(message, locked):
    assert sql_engine._is_lock_error(RuntimeError(message)) is locked


@pytest.fixture()
def damaged(tmp_path, monkeypatch):
    path = tmp_path / "warehouse.duckdb"
    path.write_bytes(b"not a database, of any kind " * 500)
    monkeypatch.setattr(settings, "duckdb_path", str(path))
    schema_cache.invalidate()
    yield str(path)
    schema_cache.invalidate()


def _no_wait(_seconds):
    raise AssertionError("a damaged file is not worth waiting for")


CORRUPT_BLOCK = (
    "IO Error: Corrupt database file: computed checksum 1 does not match stored checksum 2 "
    "in block at location 12288"
)


def _connect_says_corrupt(*_args, **_kwargs):
    raise duckdb.IOException(CORRUPT_BLOCK)


@pytest.mark.parametrize("corrupt_block", [False, True], ids=["garbage file", "damaged block"])
def test_a_damaged_file_gets_one_plain_line_and_no_model_call(damaged, monkeypatch, corrupt_block):
    monkeypatch.setattr("time.sleep", _no_wait)
    if corrupt_block:
        monkeypatch.setattr(duckdb, "connect", _connect_says_corrupt)
    model = Model(monkeypatch)
    text, meta = ask("how many interviews?")
    assert CANNOT_BE_READ in text
    assert "being refreshed" not in text
    assert COULD_NOT_WRITE not in text
    assert model.writer_prompts == []
    assert meta["salesforce_error"], "the analytics page counts this as a failure"
    assert damaged not in text and damaged not in str(meta), "never the server's path"


def test_a_file_damaged_after_the_schema_was_cached_costs_no_retry(damaged, monkeypatch):
    monkeypatch.setattr(sql_engine.schema_cache, "get", lambda path: {"Interview__c": [("Id", "VARCHAR")]})
    monkeypatch.setattr(sql_engine, "resolve_people", lambda q: [])
    model = Model(monkeypatch, "SELECT COUNT(*) FROM Interview__c")
    text, _meta = ask("how many interviews?")
    assert CANNOT_BE_READ in text
    assert len(model.writer_prompts) == 1, "the IO error must not reach the model as a retry hint"


def test_a_real_corrupt_message_is_unreadable_not_busy():
    error = duckdb.IOException("IO Error: Corrupt database file: stored checksum 2 in block at location 12288")
    with pytest.raises(WarehouseUnreadable):
        sql_engine._raise_if_warehouse_fault(error)


def test_a_real_lock_message_is_busy():
    error = duckdb.IOException('IO Error: Could not set lock on file "/w": Conflicting lock is held in python')
    with pytest.raises(WarehouseBusy):
        sql_engine._raise_if_warehouse_fault(error)


def test_what_callers_pass_on_holds_no_path(damaged, monkeypatch):
    Model(monkeypatch)
    with pytest.raises(WarehouseUnreadable) as caught:
        asyncio.run(generate_and_run_sql("how many interviews?"))
    assert damaged not in str(caught.value)


def test_the_schema_read_does_not_wait_on_a_damaged_file(damaged, monkeypatch):
    """"block" holds the word "lock", which is how this waited 6 s on every question."""
    monkeypatch.setattr("time.sleep", _no_wait)
    monkeypatch.setattr(duckdb, "connect", _connect_says_corrupt)
    with pytest.raises(duckdb.IOException):
        schema_cache.get(damaged)


# ── Schema questions the regex does not catch ────────────────────────────────


@pytest.mark.parametrize(
    "question, statement, expected",
    [
        ("what columns does Account have?", "DESCRIBE Account", "Account has 3 synced fields"),
        ("what columns does Account have?", "SELECT column_name FROM information_schema.columns WHERE table_name = 'Account'", "Account has 3 synced fields"),
        ("how many tables are synced?", "SHOW TABLES", "holds 10 tables"),
        ("how many tables are synced?", "SELECT COUNT(*) FROM information_schema.tables", "holds 10 tables"),
        ("how many tables are synced?", "SELECT COUNT(*) FROM duckdb_tables()", "holds 10 tables"),
        ("how many tables are synced?", "SELECT COUNT(*) FROM pg_catalog.pg_tables", "holds 10 tables"),
        ("how many tables are synced?", "PRAGMA show_tables", "holds 10 tables"),
        ("describe the Account object", "DESC Account", "Account has 3 synced fields"),
        ("give me the fields of Account", "SELECT * FROM information_schema.columns", "Account has 3 synced fields"),
    ],
)
def test_a_statement_reading_the_catalogue_gets_the_catalogue(warehouse, monkeypatch, question, statement, expected):
    Model(monkeypatch, statement)
    assert expected in ask(question)[0]


@pytest.mark.parametrize(
    "question", ["show me a table of courses by status", "list course enrollments as a table", "show a column chart of courses"]
)
def test_no_bare_table_word_turns_a_record_question_into_the_catalogue(warehouse, monkeypatch, question):
    assert live_sf.is_schema_question(question) is False
    Model(monkeypatch, "SELECT COUNT(*) FROM Course__c")
    assert COULD_NOT_FIND in ask(question)[0]


# ── A synced object is not "could not find" ──────────────────────────────────


@pytest.mark.parametrize(
    "from_clause",
    ['main.Account', '"main"."Account"', "warehouse.main.Account", '"Account"'],
)
def test_a_qualified_name_reads_the_table(warehouse, monkeypatch, from_clause):
    model = Model(monkeypatch, f"SELECT COUNT(*) AS n FROM {from_clause}")
    _text, meta = ask("how many accounts?")
    assert meta["data"] == [{"n": 1}]
    assert len(model.writer_prompts) == 1


@pytest.mark.parametrize(
    "written, real",
    [("Cases", '"Case"'), ("Internal_Interviews__c", "Internal_Interview__c"), ("Intervew__c", "Interview__c")],
)
def test_a_misspelt_table_gets_one_retry_that_names_the_real_one(warehouse, monkeypatch, written, real):
    model = Model(monkeypatch, f"SELECT COUNT(*) AS n FROM {written}", f"SELECT COUNT(*) AS n FROM {real}")
    _text, meta = ask("how many of them are there?")
    assert meta["sql"].endswith(real)
    assert real.strip('"') in model.writer_user()
    assert "Do not read any other table in its place" in model.writer_user()


def test_the_retry_may_not_pick_any_other_table(warehouse, monkeypatch):
    Model(monkeypatch, "SELECT COUNT(*) AS n FROM Cases", "SELECT COUNT(*) AS n FROM Account")
    text, meta = ask("how many cases?")
    assert COULD_NOT_FIND in text
    assert "data" not in meta


def test_the_retry_may_say_it_is_not_there_after_all(warehouse, monkeypatch):
    Model(monkeypatch, "SELECT COUNT(*) AS n FROM Cases", "NO_SUCH_OBJECT")
    assert COULD_NOT_FIND in ask("how many legal cases?")[0]


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT COUNT(*) FROM Course__c",
        "SELECT COUNT(*) FROM Account a JOIN Course__c c ON c.Account__c = a.Id",
        "SELECT COUNT(*) FROM Course__c c JOIN Account a ON c.Account__c = a.Id",
    ],
)
def test_an_unsynced_object_costs_no_retry_that_could_pick_the_nearest_table(warehouse, monkeypatch, statement):
    model = Model(monkeypatch, statement)
    text, meta = ask("how many accounts have a course?")
    assert COULD_NOT_FIND in text
    assert "data" not in meta
    assert len(model.writer_prompts) == 1


@pytest.mark.parametrize(
    "written, table, same",
    [
        ("Cases", "Case", True),
        ("Interview", "Interview__c", True),
        ("Internal_Interviews__c", "Internal_Interview__c", True),
        ("Candidate_TrainingStep__c", "CandidateTrainingStep__c", True),
        ("Intervew__c", "Interview__c", True),
        ("Course__c", "Program__c", False),
        ("Course__c", "Case", False),
        ("ContentDocumentLink", "ContentDocument", False),
        ("Follow_Up_Setting__mdt", "Follow_Up__c", False),
        ("Lead", "Load", False),
    ],
)
def test_only_a_spelling_counts_as_the_same_object(written, table, same):
    assert same_object(written, table) is same


# ── Planned and follow-up turns keep "could not find" ────────────────────────


@pytest.mark.parametrize(
    "question, statement",
    [
        ("how many courses do we have?\n\n(Clarified: object: Course__c)", "SELECT COUNT(*) FROM Course__c"),
        ("how many courses do we have?\n\n(Clarified: object: Course__c)", "SELECT 0 AS record_count"),
        ("how many mocks today?\n\n(Clarified: object: Internal_Interview__c)", "SELECT COUNT(*) FROM Mock__c"),
        ("how many training courses?\n\n(Clarified: object: Account)", "SELECT COUNT(*) FROM Course__c"),
        ("show me the field sales team's widget orders", "SELECT * FROM Widget_Order__c"),
    ],
)
def test_a_planned_turn_about_nothing_synced_is_could_not_find(warehouse, monkeypatch, question, statement):
    Model(monkeypatch, statement)
    text, _meta = ask(question)
    assert COULD_NOT_FIND in text
    assert "synced fields" not in text


@pytest.mark.parametrize(
    "question, statement, expected",
    [
        ("how many objects do we have?", "SELECT 0 AS object_count", "holds 10 tables"),
        ("how many objects do we have?", "NO_SUCH_OBJECT", "holds 10 tables"),
        (
            "which fields does it have?\n\n(Clarified: object: Account)",
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'Account'",
            "Account has 3 synced fields: Id, Name, Industry",
        ),
    ],
)
def test_a_question_about_the_shape_still_gets_the_catalogue(warehouse, monkeypatch, question, statement, expected):
    Model(monkeypatch, statement)
    assert expected in ask(question)[0]


# ── The retry goes through the same check ────────────────────────────────────


def test_a_fabricated_zero_on_the_retry_is_refused(warehouse, monkeypatch):
    Model(monkeypatch, "SELECT r.Title__c FROM Recruiter__c r", "SELECT 0 AS record_count")
    text, meta = ask("how many titles do recruiters have?")
    assert COULD_NOT_FIND in text
    assert "data" not in meta


@pytest.mark.parametrize("retry", ["", "I am sorry, that column does not exist."])
def test_an_empty_or_prose_retry_stays_could_not_write(warehouse, monkeypatch, retry):
    Model(monkeypatch, "SELECT r.Title__c FROM Recruiter__c r", retry)
    assert COULD_NOT_WRITE in ask("what are the recruiters' titles?")[0]


def test_a_missing_table_on_the_retry_is_could_not_find(warehouse, monkeypatch):
    Model(monkeypatch, "SELECT r.Title__c FROM Recruiter__c r", "SELECT COUNT(*) FROM Account a JOIN Course__c c ON c.x = a.Id")
    assert COULD_NOT_FIND in ask("how many accounts have a course?")[0]


def test_a_missing_function_on_the_retry_stays_could_not_write(warehouse, monkeypatch):
    """A Catalog Error too, but this one is a query mistake and the object is there."""
    Model(monkeypatch, "SELECT r.Title__c FROM Recruiter__c r", "SELECT TO_CHAR(Name, 'x') FROM Recruiter__c")
    assert COULD_NOT_WRITE in ask("how are recruiter names written?")[0]


def test_a_retry_that_only_had_advice_keeps_the_good_first_result(warehouse, monkeypatch):
    Model(monkeypatch, GROUPED_BY_ID, "SELECT broken FROM Recruiter__c")
    _text, meta = ask("how many employees per department?")
    assert {row["n"] for row in meta["data"]} == {1}


# ── The catalogue line ───────────────────────────────────────────────────────

CATALOGUE = "SELECT column_name FROM information_schema.columns"


@pytest.mark.parametrize(
    "question, expected",
    [
        ("which fields does Task have?", "Task has 2 synced fields: Id, Subject"),
        ("what fields does account have?", "Account has 3 synced fields"),
        ("what fields does the Interview object have?", "Interview__c has 8 synced fields"),
        ("what fields does interview__c have?", "Interview__c has 8 synced fields"),
        ("which fields do invoices have?", "Invoice__c has 4 synced fields"),
    ],
)
def test_a_named_object_is_found_however_it_is_written(warehouse, monkeypatch, question, expected):
    Model(monkeypatch, CATALOGUE)
    text = ask(question)[0]
    assert expected in text
    assert "Name an object" not in text


@pytest.mark.parametrize("name", ["Course__c", "course__c", "Contact"])
def test_a_named_object_the_copy_does_not_hold_is_said_to_be_missing(warehouse, monkeypatch, name):
    Model(monkeypatch, CATALOGUE)
    text = ask(f"what fields does {name} have?")[0]
    assert f"{name} is not in the synced copy; an administrator can add it." in text
    assert "Name an object" not in text


def test_name_an_object_is_said_only_when_none_was_named(warehouse, monkeypatch):
    Model(monkeypatch, CATALOGUE)
    assert "holds 10 tables. Name an object to see its fields." in ask("how many objects do we have?")[0]


def test_a_common_word_does_not_crowd_out_the_object_that_was_meant():
    tables = {name: [("Id", "VARCHAR")] for name in ("Account", "User", "Group", "Order", "Note", "Interview__c")}
    question = "what is the name of the user and group fields in the note for the order on Account and interview__c"
    named, _missing = sql_engine._objects_named(question, tables)
    assert named[:2] == ["Account", "Interview__c"]
    assert len(named) == sql_engine._MAX_CATALOGUE_OBJECTS


def test_machine_labels_in_a_clarified_line_name_no_object():
    tables = {"Metric": [("Id", "VARCHAR")], "Account": [("Id", "VARCHAR")]}
    named, _missing = sql_engine._objects_named("how many objects?\n\n(Clarified: metric: all)", tables)
    assert named == []
    named, _missing = sql_engine._objects_named("which fields?\n\n(Clarified: object: Account)", tables)
    assert named == ["Account"]


# ── The agent route, written by the engine ───────────────────────────────────


class AgentModel:
    """A planner reply, queued writer replies, and a synthesis that records its prompt."""

    def __init__(self, monkeypatch, steps, *replies, synthesis=("An answer.",), by_input=None):
        self.steps = steps
        self.replies = list(replies)
        self.synthesis = list(synthesis)
        #: Steps run at once, so a reply per step cannot wait its turn in a queue.
        self.by_input = by_input or {}
        self.plan_kwargs = []
        self.writer_prompts = []
        self.synthesis_prompts = []

        async def chat_completion(messages, **kwargs):
            if "plan multi-step" in str(messages[0].get("content")):
                self.plan_kwargs.append(kwargs)
                return self.steps if isinstance(self.steps, str) else json.dumps({"steps": self.steps})
            self.writer_prompts.append(messages)
            for words, reply in self.by_input.items():
                if f"Question: {words}" in messages[-1]["content"]:
                    return reply
            assert self.replies, "the writer was asked again with nothing left to say"
            return self.replies.pop(0)

        async def stream_chat_events(messages, **_kwargs):
            self.synthesis_prompts.append(messages)
            for token in self.synthesis:
                yield "token", token

        monkeypatch.setattr(sql_engine.llm, "chat_completion", chat_completion)
        monkeypatch.setattr(sql_engine.llm, "stream_chat_events", stream_chat_events)

    def step_results(self):
        return self.synthesis_prompts[-1][-1]["content"]


def sql_step(id=1, title="Count them", input="count the interviews", kind="sql"):
    return {"id": id, "title": title, "kind": kind, "input": input}


def ask_agent(question, effort="think", salesforce=True):
    rec = Rec()
    text = asyncio.run(agent.run_agent_engine(question, [], rec.emit, effort=effort, salesforce=salesforce, web=False))
    assert "".join(d["text"] for e, d in rec.events if e == "token") == text
    return text, rec.meta()


def run_step(step_input="count them", kind="sql", message="count them"):
    step = agent.PlanStep(id=1, title="A step", kind=kind, input=step_input)
    return asyncio.run(agent._run_step_impl(step, [], True, "think", None, message))


OFFERS = "SELECT COUNT(*) AS n FROM Interview__c WHERE Interview_Outcome__c = 'Ghosted'"


# ── F5: the agent cannot claim a direct check, and the source is written by code ─


def test_an_agent_answer_with_a_sql_step_ends_with_the_source_written_by_code(warehouse, monkeypatch):
    AgentModel(monkeypatch, [sql_step()], OFFERS, synthesis=("There are 4.",))
    text, _meta = ask_agent("how many interviews ended ghosted?")
    assert text.startswith("There are 4.")
    assert agent.SOURCE_LINE in text
    assert text.index(agent.SOURCE_LINE) > text.index("There are 4.")


@pytest.mark.parametrize(
    "tokens, expected",
    [
        (("Based on the direct Salesforce check, there are 4."), "Based on the synced Salesforce copy, there are 4."),
        (("Based on the dir", "ect Sales", "force check, there", " are 4."), "Based on the synced Salesforce copy, there are 4."),
        (("A live Salesforce query shows 4."), "The synced Salesforce copy shows 4."),
        (("The figure comes directly from Salesforce: 4."), "The figure comes from the synced Salesforce copy: 4."),
    ],
)
def test_the_claim_of_a_direct_check_does_not_reach_the_reader(warehouse, monkeypatch, tokens, expected):
    synthesis = (tokens,) if isinstance(tokens, str) else tokens
    AgentModel(monkeypatch, [sql_step()], OFFERS, synthesis=synthesis)
    assert ask_agent("Check directly in Salesforce: how many ghosted?")[0].startswith(expected)


@pytest.mark.parametrize(
    "sentence",
    [
        "This comes from the local copy rather than a live Salesforce query.",
        "It is not a direct Salesforce check.",
    ],
)
def test_saying_it_was_not_a_live_check_is_left_alone(warehouse, monkeypatch, sentence):
    AgentModel(monkeypatch, [sql_step()], OFFERS, synthesis=(sentence,))
    assert ask_agent("how many ghosted?")[0].startswith(sentence)


def test_the_computed_block_is_not_named_on_the_agent_route_either(warehouse, monkeypatch):
    AgentModel(monkeypatch, [sql_step()], OFFERS, synthesis=("Based on the computed figures, there are 4."))
    text = ask_agent("how many ghosted?")[0]
    assert "computed figures" not in text.lower()


def test_a_plan_with_no_sql_step_gets_no_source_line(warehouse, monkeypatch):
    sentence = "Their docs describe a live Salesforce query tool."
    AgentModel(monkeypatch, [{"id": 1, "title": "Think", "kind": "llm", "input": "reason"}], "a reasoned line", synthesis=(sentence,))
    text, meta = ask_agent("explain what that vendor sells")
    assert text == sentence, "with no warehouse read there is nothing to correct"
    assert "salesforce_sources" not in meta


def test_the_merge_prompt_says_where_the_rows_came_from():
    assert "local synced copy" in agent._SYNTH_SYSTEM
    assert "live or direct check" in agent._SYNTH_SYSTEM


# ── F13: the scope of an agent step, written by code ─────────────────────────


def test_an_agent_answer_states_the_record_type_scope(warehouse, monkeypatch):
    AgentModel(monkeypatch, [sql_step()], OFFERS)
    text = ask_agent("how many interviews ended ghosted?")[0]
    assert "Scope: Interview__c, where Interview_Outcome__c = 'Ghosted'." in text
    assert "That covers every Interview__c record type, initial calls included." in text


def test_a_step_that_filters_the_record_type_shows_that_filter_instead(warehouse, monkeypatch):
    statement = (
        "SELECT COUNT(*) AS n FROM Interview__c i JOIN RecordType rt ON i.RecordTypeId = rt.Id "
        "WHERE rt.Name = 'Interview'"
    )
    AgentModel(monkeypatch, [sql_step()], statement)
    text = ask_agent("how many client interviews?")[0]
    assert "RecordType.Name = 'Interview'" in text
    assert "initial calls included" not in text


def test_the_synthesis_is_given_the_same_line_and_told_to_describe_no_other_filter(warehouse, monkeypatch):
    model = AgentModel(monkeypatch, [sql_step()], OFFERS)
    ask_agent("how many ghosted?")
    assert "Scope: Interview__c, where Interview_Outcome__c = 'Ghosted'." in model.step_results()
    assert "describe no other filter" in model.step_results()


def test_each_sql_step_states_its_own_scope(warehouse, monkeypatch):
    AgentModel(
        monkeypatch,
        [sql_step(1, "Ghosted"), sql_step(2, "Invoices", "count invoices")],
        by_input={"count the interviews": OFFERS, "count invoices": "SELECT COUNT(*) AS n FROM Invoice__c"},
    )
    text = ask_agent("compare ghosted interviews and invoices")[0]
    assert "Ghosted: Scope: Interview__c, where" in text
    assert "Invoices: Scope: every row of Invoice__c." in text
    assert text.count(agent.SOURCE_LINE) == 1


def test_an_agent_step_says_what_was_held_back(warehouse, monkeypatch):
    AgentModel(monkeypatch, [sql_step(input="who is asha varnok")], "SELECT Name, Email__c FROM Recruiter__c")
    text = ask_agent("who is asha varnok?")[0]
    assert "Contact details were left out: they are shown only when asked for by name" in text


def test_a_step_that_read_only_held_back_fields_says_so(warehouse, monkeypatch):
    model = AgentModel(monkeypatch, [sql_step()], "SELECT Portal_Password__c FROM Recruiter__c")
    text = ask_agent("list the recruiters")[0]
    assert "Credential fields are never shown." in text
    assert "SQL result (" not in model.step_results(), "no columns is not an empty result"


def test_a_cut_nobody_asked_for_is_told_to_the_synthesis(warehouse, monkeypatch):
    model = AgentModel(monkeypatch, [sql_step(input="list the background checks")], "SELECT Name FROM Background_Check__c LIMIT 30")
    _text, meta = ask_agent("which background checks are pending?")
    assert "These are the first 30 of 47 matching rows" in model.step_results()
    assert meta["salesforce_sources"]["record_count"] == 47
    assert meta["salesforce_sources"]["truncated"] is True


def test_a_result_past_the_summary_cap_is_not_the_population_on_the_agent_route(warehouse, monkeypatch):
    monkeypatch.setattr(settings, "sql_summary_row_cap", 10)
    model = AgentModel(monkeypatch, [sql_step(input="list the background checks")], "SELECT Name FROM Background_Check__c")
    _text, meta = ask_agent("list the background checks")
    assert "These are the first 10 of 47 matching rows" in model.step_results()
    assert meta["salesforce_sources"]["record_count"] == 47


def test_a_count_of_zero_carries_the_warning_on_the_agent_route(warehouse, monkeypatch):
    statement = "SELECT COUNT(*) AS n FROM Interview__c WHERE Round__c = '1'"
    model = AgentModel(monkeypatch, [sql_step()], statement, statement)
    ask_agent("how many first round interviews?")
    assert sql_engine.ZERO_FIGURE_LINE.strip() in model.step_results()
    assert "Round__c" in model.step_results().split("Computed figures")[0], "and names what the column holds"


# ── F15: the plan call cannot spend itself on reasoning ──────────────────────


@pytest.mark.parametrize("effort", ["fast", "think", "max"])
def test_the_plan_call_has_thinking_off_at_every_level(monkeypatch, effort):
    model = AgentModel(monkeypatch, [{"id": 1, "title": "t", "kind": "llm", "input": "i"}])
    asyncio.run(agent.make_plan("compare the programs", [], True, effort))
    assert [kwargs["thinking"] for kwargs in model.plan_kwargs] == [False]


@pytest.mark.parametrize(
    "question",
    [
        "Compare our top 3 training programs by number of candidate trainings and write a short summary for each.",
        "how many interviews ended ghosted?",
    ],
)
def test_a_failed_plan_on_a_data_question_is_a_sql_step(monkeypatch, question):
    model = AgentModel(monkeypatch, "")
    plan = asyncio.run(agent.make_plan(question, [], True, "think"))
    assert len(model.plan_kwargs) == 2, "one retry first"
    assert [(step.kind, step.input) for step in plan.steps] == [("sql", question)]


def test_a_failed_plan_on_a_data_question_reads_the_warehouse_end_to_end(warehouse, monkeypatch):
    model = AgentModel(monkeypatch, "", OFFERS)
    text = ask_agent("how many interviews ended ghosted?")[0]
    assert model.writer_prompts, "the fallback wrote a statement"
    assert agent.SOURCE_LINE in text


def test_a_failed_plan_stays_an_llm_step_when_it_is_no_data_question(monkeypatch):
    AgentModel(monkeypatch, "")
    plan = asyncio.run(agent.make_plan("write a friendly note to the team", [], True, "think"))
    assert [step.kind for step in plan.steps] == ["llm"]


def test_a_failed_plan_never_reaches_the_warehouse_with_salesforce_off(monkeypatch):
    AgentModel(monkeypatch, "")
    plan = asyncio.run(agent.make_plan("how many interviews?", [], False, "think"))
    assert [step.kind for step in plan.steps] == ["llm"]


# ── The agent's schema step and a warehouse it cannot read ───────────────────


def test_a_schema_step_answers_from_the_catalogue(warehouse, monkeypatch):
    AgentModel(monkeypatch, [], "SELECT column_name FROM information_schema.columns WHERE table_name = 'Account'")
    output, detail, meta = run_step("List the fields of Account", kind="salesforce", message="which fields does Account have?")
    assert output.startswith("The synced Salesforce copy holds")
    assert "Account has 3 synced fields: Id, Name, Industry" in output
    assert "not found" not in output
    assert detail == "from the synced catalogue"
    assert meta == {"salesforce_catalogue": True}, "no statement ran, so no source may be claimed"


def test_the_planners_words_are_enough_for_the_catalogue(warehouse, monkeypatch):
    AgentModel(monkeypatch, [], "DESCRIBE Account")
    output = run_step("List the fields of Account", message="compare the fields on Account and Task")[0]
    assert "Account has 3 synced fields" in output


def test_a_data_step_on_a_missing_object_is_still_not_found(warehouse, monkeypatch):
    AgentModel(monkeypatch, [], "SELECT COUNT(*) FROM Widget__c")
    output, detail, meta = run_step("count the widgets", message="how many widgets?")
    assert "was not found in the synced Salesforce copy" in output
    assert detail == "not in the synced copy"
    assert meta == {}


def test_an_unreadable_warehouse_holds_no_path_in_the_step(tmp_path, monkeypatch):
    path = tmp_path / "warehouse.duckdb"
    path.write_bytes(b"this is not a database at all, just bytes on a disk")
    monkeypatch.setattr(settings, "duckdb_path", str(path))
    schema_cache.invalidate()
    AgentModel(monkeypatch, [])
    output, detail, meta = run_step()
    assert "cannot be read" in output
    assert str(tmp_path) not in output and str(tmp_path) not in detail
    assert meta == {}
    schema_cache.invalidate()


def test_no_warehouse_yet_is_said_with_no_model_call(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "duckdb_path", str(tmp_path / "absent.duckdb"))
    schema_cache.invalidate()
    model = AgentModel(monkeypatch, [])
    output, detail, _meta = run_step()
    assert "holds no data yet" in output
    assert detail == "no synced copy yet"
    assert model.writer_prompts == []
    schema_cache.invalidate()


# ── Source, scope and assumptions on the screen ──────────────────────────────


def test_a_direct_answer_carries_its_source(warehouse, monkeypatch):
    Model(monkeypatch, "SELECT Name FROM Invoice__c")
    _text, meta = ask("list the invoices")
    sources = meta["salesforce_sources"]
    assert sources["source"] == "synced"
    assert sources["objects"] == ["Invoice__c"]
    assert sources["record_count"] == 3
    assert sources["truncated"] is False
    expected = datetime.fromtimestamp(pathlib.Path(warehouse).stat().st_mtime, tz=timezone.utc)
    assert sources["query_timestamp"] == expected.isoformat(timespec="seconds")


def test_one_row_of_figures_is_no_record_count(warehouse, monkeypatch):
    Model(monkeypatch, "SELECT COUNT(*) AS n FROM Invoice__c")
    assert "record_count" not in ask("how many invoices?")[1]["salesforce_sources"]


def test_record_type_is_plumbing_and_stays_out_of_the_objects(warehouse, monkeypatch):
    Model(monkeypatch, "SELECT COUNT(*) AS n FROM Interview__c i JOIN RecordType rt ON i.RecordTypeId = rt.Id")
    assert ask("how many interviews?")[1]["salesforce_sources"]["objects"] == ["Interview__c"]


def test_a_cut_says_how_many_really_matched(warehouse, monkeypatch):
    Model(monkeypatch, "SELECT Name FROM Background_Check__c LIMIT 30")
    sources = ask("which background checks are pending?")[1]["salesforce_sources"]
    assert (sources["record_count"], sources["truncated"]) == (47, True)


def test_no_source_is_ever_sent_with_empty_objects(warehouse):
    assert sql_engine.synced_sources([]) is None
    assert "salesforce_sources" not in sql_engine._source_meta([])
    assert "salesforce_sources" not in agent.merge_step_meta(
        [{"step": agent.PlanStep(id=1, title="t", kind="sql", input="i"), "status": "done", "meta": {"sql": "SELECT 1", "objects": []}}]
    )


@pytest.mark.parametrize(
    "reply",
    ["NO_SUCH_OBJECT", "SELECT 0 AS n", "SELECT COUNT(*) FROM Widget__c"],
)
def test_a_could_not_find_claims_no_source(warehouse, monkeypatch, reply):
    Model(monkeypatch, reply)
    assert "salesforce_sources" not in ask("how many widgets?")[1]


def test_a_source_survives_a_warehouse_with_no_timestamp(warehouse, monkeypatch):
    monkeypatch.setattr(sql_engine.os.path, "getmtime", lambda _path: (_ for _ in ()).throw(OSError("gone")))
    assert "query_timestamp" not in sql_engine.synced_sources(["Case"])


def test_the_agent_meta_carries_every_object_it_read(warehouse, monkeypatch):
    AgentModel(
        monkeypatch,
        [sql_step(1, "One"), sql_step(2, "Two", "count invoices")],
        by_input={"count the interviews": OFFERS, "count invoices": "SELECT Name FROM Invoice__c"},
    )
    meta = ask_agent("compare them")[1]
    sources = meta["salesforce_sources"]
    assert sources["source"] == "synced"
    assert sources["objects"] == ["Interview__c", "Invoice__c"]
    assert "record_count" not in sources, "a figure per step adds up to nothing"
    assert "query_timestamp" in sources
    for internal in ("objects", "counted", "left_out_line", "no_date", "capped"):
        assert internal not in meta


def test_a_count_with_no_date_filter_says_it_covers_all_dates(warehouse, monkeypatch):
    model = Model(monkeypatch, "SELECT COUNT(*) AS n FROM Invoice__c")
    meta = ask("how many invoices?")[1]
    assert meta["assumptions"] == ["No date filter was applied, so this covers all dates."]
    assert meta["assumptions_applied"] is True
    assert "covers all dates" in model.narrative_user()


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT COUNT(*) AS n FROM Interview__c WHERE Date_of_Interview__c >= DATE '2026-05-01'",
        "SELECT COUNT(*) AS n FROM Interview__c WHERE strftime(Date_of_Interview__c, '%Y') = '2026'",
        "SELECT COUNT(*) AS n FROM Interview__c WHERE Date_of_Interview__c = (now() AT TIME ZONE 'UTC')::date",
        # No date literal or date function in sight: the column's type says it.
        "SELECT COUNT(*) AS n FROM Interview__c WHERE Date_of_Interview__c IS NOT NULL",
        # A date held as text: the column's type says nothing, the literal does.
        "SELECT COUNT(*) AS n FROM Invoice__c WHERE Name >= '2026-05-01'",
        # Not in a WHERE at all.
        "SELECT COUNT(*) AS n FROM Interview__c i JOIN RecordType r ON i.RecordTypeId = r.Id AND i.Date_of_Interview__c >= DATE '2026-05-01'",
    ],
)
def test_a_statement_with_a_date_filter_does_not_claim_all_dates(warehouse, monkeypatch, statement):
    Model(monkeypatch, statement)
    assert "assumptions" not in ask("how many interviews this month?")[1]


def test_a_single_record_has_no_period_to_speak_of(warehouse, monkeypatch):
    Model(monkeypatch, "SELECT Name FROM Invoice__c WHERE Name = 'INV-1'")
    assert "assumptions" not in ask("what is invoice INV-1?")[1]


def test_all_dates_stands_only_if_every_agent_step_shows_it(warehouse, monkeypatch):
    dated = "SELECT COUNT(*) AS n FROM Interview__c WHERE Date_of_Interview__c >= DATE '2026-05-01'"
    invoices = "SELECT COUNT(*) AS n FROM Invoice__c"
    steps = [sql_step(1, "One"), sql_step(2, "Two", "count invoices")]
    AgentModel(monkeypatch, steps, by_input={"count the interviews": dated, "count invoices": invoices})
    assert "assumptions" not in ask_agent("compare them")[1]
    AgentModel(monkeypatch, steps, by_input={"count the interviews": OFFERS, "count invoices": invoices})
    assert ask_agent("compare them")[1]["assumptions"] == ["No date filter was applied, so this covers all dates."]


def _hand_back(monkeypatch, decision):
    from app.engines import sf_intel

    async def nothing(*_a, **_k):
        return "", []

    async def plan(**_k):
        return decision

    monkeypatch.setattr(sf_intel.tools, "get_salesforce_schema", nothing)
    monkeypatch.setattr(planner, "plan", plan)
    return sf_intel


def test_what_the_planner_assumed_never_reaches_the_screen_on_its_own(warehouse, monkeypatch):
    """Asked about a course, the screen would state "Assumed date range: this
    quarter" over a statement with no date filter. The hand-back rides onto
    whichever engine answers, and only that engine knows what ran."""
    sf_intel = _hand_back(
        monkeypatch,
        AgentDecision(
            action="EXECUTE_SALESFORCE",
            assumptions=["Assumed date range: this quarter"],
            resolved_slots={"object": "Interview__c"},
        ),
    )
    rec = Rec()
    outcome = asyncio.run(sf_intel.run("how many interviews?", [], rec.emit, conversation_id="conv-handback"))
    assert not outcome.handled
    assert "assumptions" not in outcome.meta_extras
    assert "salesforce_scope" not in outcome.meta_extras
    assert outcome.assumptions == ["Assumed date range: this quarter"]


# ── F10, M8: no card on what the question states, and a click binds ──────────


def _intent(text="q"):
    return PendingIntent(intent_id="i1", conversation_id="c1", root_user_message_id="m1", original_user_text=text)


def _card(slot, *labels, question="Which one?", values=None):
    options = [
        ClarificationOption(id=f"o{i}", label=label, value=(values or {}).get(label, ""))
        for i, label in enumerate(labels)
    ]
    return AgentDecision(
        action="ASK_CLARIFICATION",
        missing_critical_slots=[slot],
        clarification_draft=ClarificationDraft(slot=slot, question=question, options=options),
    )


def _enforced(question, decision, **kwargs):
    return planner.enforce_policy(decision, _intent(question), reading=interpret.read(question), **kwargs)


#: Cards the model really asked (stage 0), with no period in the question.
@pytest.mark.parametrize(
    "question, labels",
    [
        ("How many session attendees were marked absent?", ("Today", "This Week", "All Time")),
        ("What percentage of internal interviews are completed?", ("All time", "This month", "Last 30 days", "Custom range")),
        ("How many interviews ended with an offer?", ("All time", "This month", "Today")),
    ],
)
def test_no_period_given_means_no_date_card(question, labels):
    out = _enforced(question, _card("date_range", *labels))
    assert out.action == "EXECUTE_SALESFORCE"
    assert out.internal_reason_code == "no_period_given"
    assert out.assumptions[-1] == "No date range was given, so this covers all dates."
    assert "date_range" not in out.resolved_slots, "'Today' came first on a card about a plain count"


#: The same, with the period in the words: asked as a card in stage 0.
@pytest.mark.parametrize(
    "question",
    [
        "ghosted interviews may vs june?",
        "april se june ke beech kitne first round client interviews ka outcome ghosted tha?",
        "Show the monthly trend of client interviews over the last six months.",
        "How many internal interviews were created in September 2026?",
    ],
)
def test_a_period_the_question_states_gets_no_card(question):
    out = _enforced(question, _card("date_range", "Option A", "Option B"))
    assert out.internal_reason_code == "answered_by_the_request"
    assert "date_range" not in out.resolved_slots


@pytest.mark.parametrize(
    "question, word",
    [
        ("How many internal interviews were created in September 2026?", "created"),
        ("april se june ke beech kitne interviews ka outcome ghosted tha?", "beech"),
        ("how many offers did candidates receive this year?", "receive"),
        ("how many session attendees attended the session?", "attended"),
    ],
)
def test_a_correct_word_is_not_repaired_into_a_different_one(question, word):
    """"were created" reached the planner as "were cleared": that note, not the
    question, is what put a completed-or-created card on the sentence."""
    reading = interpret.read(question)
    assert word in reading.text
    assert word not in [was for was, _now in reading.repairs]


def test_may_on_its_own_is_only_an_english_word():
    assert "date_range" not in interpret.satisfied_slots("what may have caused this")
    assert "date_range" in interpret.satisfied_slots("how many mocks in may")


def test_the_planner_is_told_not_to_ask_for_a_period_nobody_gave():
    from app.core.sf_intel.prompts import PLANNER_SYSTEM

    assert "is a period the request does not mention" in PLANNER_SYSTEM


def test_a_period_the_planner_filled_in_on_its_own_stays_out():
    question = "How many session attendees were marked absent?"
    intent = _intent(question)
    decision = AgentDecision(action="EXECUTE_SALESFORCE", resolved_slots={"date_range": "THIS_MONTH", "object": "Session__c"})
    sf_state.decision_slots(decision, intent, interpret.read(question))
    assert intent.resolved_slots == {"object": "Session__c"}


def test_a_period_stated_or_carried_stays_in():
    intent = _intent("how many mocks today")
    decision = AgentDecision(action="EXECUTE_SALESFORCE", resolved_slots={"date_range": "TODAY"})
    sf_state.decision_slots(decision, intent, interpret.read("how many mocks today"))
    assert intent.resolved_slots == {"date_range": "TODAY"}
    carried = _intent("and for leads?")
    carried.resolved_slots["date_range"] = "THIS_QUARTER"
    decision = AgentDecision(action="EXECUTE_SALESFORCE", resolved_slots={"date_range": "THIS_YEAR"})
    sf_state.decision_slots(decision, carried, interpret.read("and for leads?"))
    assert carried.resolved_slots == {"date_range": "THIS_YEAR"}


def test_the_writer_is_told_no_period_means_no_date_filter():
    assert "names no period gets NO date filter" in sql_engine._SQL_SYSTEM


#: Stage 0's card on a word the question already said, and its neighbours.
@pytest.mark.parametrize(
    "question, labels, taken",
    [
        ("How many internal interviews were created in September 2026?", ("Completed (Cleared)", "Created (Scheduled)"), "Created (Scheduled)"),
        ("How many interviews ended with an offer?", ("Count of interview rounds", "Count of unique candidates"), "Count of interview rounds"),
        ("Show the monthly trend of client interviews over the last six months.", ("Interview rounds held", "Unique candidates"), "Interview rounds held"),
    ],
)
def test_a_card_on_what_the_question_states_is_not_asked(question, labels, taken):
    out = _enforced(question, _card("metric", *labels))
    assert out.action == "EXECUTE_SALESFORCE"
    assert out.internal_reason_code == "answered_by_the_request"
    assert out.resolved_slots["metric"] == taken


#: These look the same and are not: the words on the card do not settle them.
@pytest.mark.parametrize(
    "question, labels",
    [
        ("What percentage of graded deliverable submissions passed?", ("AI Pass Verdicts", "Approved Deliverables")),
        ("For submitted forms, how many prospects are on each visa?", ("Count of submitted forms", "Count of unique prospects")),
        ("How many offers did candidates receive each month this year?", ("Distinct candidates", "Total offer records")),
        ("How many active candidate trainings have no sessions at all?", ("No sessions at all", "No Training sessions")),
        ("How many advanced mocks today?", ("Scheduled interviews", "Unique candidates")),
        ("Which 5 recruiters have the most interviews?", ("All interviews", "Client-facing only")),
    ],
)
def test_a_card_the_words_do_not_settle_is_still_asked(question, labels):
    assert _enforced(question, _card("metric", *labels)).action == "ASK_CLARIFICATION"


def test_a_record_card_is_never_settled_by_a_word():
    decision = _card("record_identity", "Asha Varnok", "Rovik Mendrell")
    assert planner._option_the_request_states(decision.clarification_draft, interpret.read("show asha's mocks")) is None


def test_an_unasked_question_folds_its_reading_for_the_writer(monkeypatch):
    monkeypatch.setattr(settings, "salesforce_max_clarification_rounds", 1)
    intent = _intent("q")
    intent.clarification_history.append(
        ClarificationRound(clarification_id="c", slot="object", question="Which?", question_fingerprint="f", answered=True)
    )
    out = planner.enforce_policy(_card("metric", "Rounds", "People", values={"Rounds": "count_rounds"}), intent)
    assert out.resolved_slots["metric"] == "Rounds (count_rounds)"


def test_invented_records_and_a_first_option_period_are_never_folded():
    out = planner.enforce_policy(_card("record_identity", "John D.", "John S."), _intent("show mocks for John"))
    assert "record_identity" not in out.resolved_slots
    assert planner.best_reading(_card("date_range", "Today", "All time").clarification_draft) is None


def _answered(slot="metric", value="all", label="All interviews"):
    intent = _intent("Which 5 recruiters have the most interviews?")
    intent.resolved_slots[slot] = value
    intent.clarification_history.append(
        ClarificationRound(
            clarification_id="c", slot=slot, question="Which?", question_fingerprint="f",
            answer=value, answer_label=label, answered=True,
        )
    )
    return intent


def test_a_click_is_spelled_out_in_what_the_person_read():
    text = _answered().resolved_text()
    assert '(Clarified: the person chose metric: "All interviews" (all). Do exactly this.)' in text
    assert "(Assumed:" not in text


def test_a_typed_answer_needs_no_second_spelling():
    assert 'metric: "last 90 days". Do exactly' in _answered(value="last 90 days", label="last 90 days").resolved_text()


def test_a_stored_round_from_before_this_change_still_reads():
    assert 'metric: "all"' in _answered(label="").resolved_text()


def test_a_real_click_reaches_the_resolved_text_in_the_words_on_the_card(warehouse, monkeypatch):
    from app.core.sf_intel.models import ClarificationResponse

    question = "Which 5 recruiters have the most interviews?"
    sf_intel = _hand_back(
        monkeypatch,
        _card("metric", "All interviews", "Client-facing only", values={"All interviews": "all"}),
    )
    first = Rec()
    asked = asyncio.run(sf_intel.run(question, [], first.emit, conversation_id="conv-click"))
    card = first.meta()["clarification"]
    assert asked.clarification is not None
    sf_intel = _hand_back(monkeypatch, AgentDecision(action="EXECUTE_SALESFORCE"))
    outcome = asyncio.run(
        sf_intel.run(
            "All interviews",
            [],
            Rec().emit,
            conversation_id="conv-click",
            clarification_response=ClarificationResponse(
                clarification_id=card["clarification_id"],
                conversation_id="conv-click",
                client_message_id="click-1",
                selected_option_ids=[card["options"][0]["id"]],
                resume_token=card["resume_token"],
            ),
        )
    )
    assert '(Clarified: the person chose metric: "All interviews" (all). Do exactly this.)' in outcome.resolved_text


def test_the_engine_gives_the_policy_the_tables_the_copy_holds(warehouse, monkeypatch):
    sf_intel = _hand_back(monkeypatch, _card("metric", "Rows", "People"))

    async def the_turn():
        outcome = await sf_intel.run("How many Training_Mock_Result__c are there?", [], Rec().emit, conversation_id="conv-missing")
        return outcome, sql_engine._planner_handback.get()

    outcome, handback = asyncio.run(the_turn())
    assert outcome.clarification is None, "no card about a table the copy does not hold"
    assert "Training_Mock_Result__c is not in the synced copy." in handback.note


def test_the_card_labels_and_values_stay_apart_in_a_mixed_answer():
    from app.core.sf_intel.models import ClarificationRequest, ClarificationResponse

    request = ClarificationRequest(
        clarification_id="c", conversation_id="c1", run_id="r", root_user_message_id="m", intent_id="i1",
        question="Which?", slot="metric", resume_token="t", question_fingerprint="f",
        options=[
            ClarificationOption(id="a", label="All interviews", value="all"),
            ClarificationOption(id="b", label="Client-facing only", value="client"),
        ],
    )
    response = ClarificationResponse(clarification_id="c", selected_option_ids=["a"], custom_text="and B2B")
    assert sf_state.answer_label(request, response) == "All interviews, and B2B"
    assert sf_state.answer_text(request, response) == "all, and B2B"
    assert sf_state.answer_label(request, ClarificationResponse(clarification_id="c", skipped=True)) == ""


def test_the_writer_is_told_a_click_is_a_hard_instruction(warehouse, monkeypatch):
    model = Model(monkeypatch, "SELECT COUNT(*) AS n FROM Interview__c")
    ask(_answered().resolved_text())
    prompt = model.writer_user()
    assert '(Clarified: the person chose metric: "All interviews" (all). Do exactly this.)' in prompt
    assert "It is a hard instruction" in prompt
    assert "picked themselves" not in prompt


def test_a_click_reaches_the_writer_on_the_agent_route(warehouse, monkeypatch):
    """A plan step's input is the planner's paraphrase: the click was only ever in
    the grounding text, which the knowledge layers read and the writer never did."""
    model = AgentModel(monkeypatch, [sql_step(input="count the interviews per recruiter")], "SELECT COUNT(*) AS n FROM Interview__c")
    ask_agent(_answered().resolved_text())
    assert '(Clarified: the person chose metric: "All interviews"' in model.writer_prompts[0][-1]["content"]


# ── M9: a slot nobody clicked is labelled as what it is ──────────────────────


def test_a_slot_with_no_card_behind_it_is_an_assumption():
    intent = _intent("what is the phone of asha varnok")
    intent.resolved_slots.update({"object": "Recruiter__c", "result_format": "email and phone"})
    text = intent.resolved_text()
    assert "(Assumed: object: Recruiter__c; result format: email and phone. Not said by the person.)" in text
    assert "(Clarified:" not in text


def test_a_click_and_an_assumption_keep_to_their_own_lines():
    intent = _answered()
    intent.resolved_slots["grouping"] = "Program__c"
    text = intent.resolved_text()
    assert text.index("(Clarified:") < text.index("(Assumed: grouping: Program__c")
    assert "grouping" not in text[: text.index("(Assumed:")]


def test_the_writer_is_told_an_assumption_is_not_the_persons_choice(warehouse, monkeypatch):
    model = Model(monkeypatch, "SELECT COUNT(*) AS n FROM Interview__c")
    intent = _intent("how many interviews?")
    intent.resolved_slots["grouping"] = "Program__c"
    ask(intent.resolved_text())
    assert "A line beginning '(Assumed:' is not the person's choice" in model.writer_user()


@pytest.mark.parametrize(
    "line",
    ['(Clarified: the person chose metric: "All". Do exactly this.)', "(Assumed: metric: all. Not said by the person.)"],
)
def test_neither_line_counts_as_the_persons_words(line):
    assert own_words(f"how many interviews?\n\n{line}") == "how many interviews?"


def test_both_lines_together_are_cut():
    assert own_words(_answered().resolved_text() + "\n\n(Assumed: x: y)") == _answered().original_user_text


def test_a_table_named_by_either_line_is_still_pinned():
    schema = {"Task": [("Id", "VARCHAR")], "Metric": [("Id", "VARCHAR")]}
    assert sql_engine._tables_in_clarified_line("q\n\n(Assumed: object: Task. Not said by the person.)", schema) == ["Task"]
    assert sql_engine._tables_in_clarified_line('q\n\n(Clarified: the person chose object: "Task".)', schema) == ["Task"]
    assert sql_engine._tables_in_clarified_line("Task metric", schema) == [], "the person's own words rank, they do not pin"


def test_the_lines_own_wording_is_not_looked_up_as_a_person():
    intent = _answered()
    intent.resolved_slots.update({"object": "Interview__c", "result_format": "count"})
    assert sql_engine.name_candidates(intent.resolved_text()) == []


def test_a_name_clicked_on_a_card_is_still_looked_up():
    intent = _answered(slot="record_identity", value="Asha Varnok", label="Asha Varnok")
    assert "Asha Varnok" in sql_engine.name_candidates(intent.resolved_text())


# ── M10: a model call leaves a line when it starts, and a document call a budget ─


def test_a_model_request_is_logged_before_any_reply(caplog):
    from app import llm

    seen_before_reply = []

    class Completions:
        async def create(self, **_request):
            seen_before_reply.extend(r.getMessage() for r in caplog.records if "model request" in r.getMessage())
            return "a reply"

    class Client:
        class chat:
            completions = Completions()

    request = {"model": "m", "max_tokens": 7, "messages": [{"role": "user", "content": "private words"}]}
    with caplog.at_level("INFO", logger="app.llm"):
        reply = asyncio.run(llm._primary_send(Client(), request, what="json_completion", base_url="http://sidecar.invalid/v1"))
    assert reply == "a reply"
    assert seen_before_reply == ["model request: what=json_completion model=m stream=False max_tokens=7 messages=1 chars=13"]
    assert "private words" not in caplog.text


def _read_timeout():
    import httpx
    import openai

    request = httpx.Request("POST", "http://engine.invalid/v1/chat/completions")
    try:
        try:
            raise httpx.ReadTimeout("slow", request=request)
        except httpx.ReadTimeout as cause:
            raise openai.APITimeoutError(request=request) from cause
    except openai.APITimeoutError as exc:
        return exc


def test_a_document_call_is_sent_with_its_budget(monkeypatch):
    from app.artifacts import compose

    seen = []

    async def json_completion(_messages, **_kwargs):
        seen.append(compose.llm._generation_budget.get())
        return '{"title": "A"}'

    monkeypatch.setattr(compose.llm, "json_completion", json_completion)
    monkeypatch.setattr(settings, "artifact_model_call_timeout_s", 42.0)

    async def the_call():
        await compose._json([], {}, "artifact_outline", thinking=True, max_tokens=10)
        return compose.llm._generation_budget.get()

    assert asyncio.run(the_call()) is None, "no later call in this turn inherits it"
    assert seen == [42.0]


def test_a_budget_of_zero_turns_it_off(monkeypatch):
    from app.artifacts import compose

    seen = []

    async def json_completion(_messages, **_kwargs):
        seen.append(compose.llm._generation_budget.get())
        return '{"title": "A"}'

    monkeypatch.setattr(compose.llm, "json_completion", json_completion)
    monkeypatch.setattr(settings, "artifact_model_call_timeout_s", 0.0)
    asyncio.run(compose._json([], {}, "artifact_outline", thinking=True, max_tokens=10))
    assert seen == [None]


def test_a_document_call_past_its_budget_is_a_plain_failure(monkeypatch):
    from app.artifacts import compose

    async def json_completion(_messages, **_kwargs):
        raise _read_timeout()

    monkeypatch.setattr(compose.llm, "json_completion", json_completion)
    with pytest.raises(compose.ComposeError) as failure:
        asyncio.run(compose._json([], {}, "artifact_outline", thinking=True, max_tokens=10))
    assert failure.value.category == "model_failure"
    assert "took too long" in str(failure.value)


def test_any_other_document_call_failure_is_not_called_a_timeout(monkeypatch):
    from app.artifacts import compose
    from app.resilience import ModelUnavailable

    async def json_completion(_messages, **_kwargs):
        raise ModelUnavailable("http://engine.invalid", 1.0, 1, RuntimeError("down"))

    monkeypatch.setattr(compose.llm, "json_completion", json_completion)
    with pytest.raises(ModelUnavailable):
        asyncio.run(compose._json([], {}, "artifact_outline", thinking=True, max_tokens=10))


def test_the_budget_starts_at_the_send_and_is_never_run_twice(monkeypatch):
    """The budget rides on the request, so time spent waiting for the engine to come
    back is not spent from it, and a read timeout is not re-run."""
    from app import llm
    from app.resilience import is_read_timeout

    calls = []

    class Completions:
        async def create(self, **request):
            calls.append(request)
            raise _read_timeout()

    class Client:
        class chat:
            completions = Completions()

    async def fit(messages, **kwargs):
        return list(messages), kwargs.get("requested_max_tokens")

    monkeypatch.setattr(llm, "_openai_client", lambda: Client())
    monkeypatch.setattr(llm, "_fit", fit)
    monkeypatch.setattr(llm._admission, "run", lambda create, **_kwargs: create())

    async def with_budget():
        with llm.generation_budget(42.0):
            await llm.json_completion([{"role": "user", "content": "x"}], max_tokens=5)

    with pytest.raises(Exception) as failure:
        asyncio.run(with_budget())
    assert is_read_timeout(failure.value)
    assert len(calls) == 1
    assert calls[0]["timeout"].read == 42.0


def test_a_call_with_no_budget_is_sent_as_ever(monkeypatch):
    from app import llm

    calls = []

    class Message:
        content = "{}"

    class Choice:
        message = Message()
        finish_reason = "stop"

    class Reply:
        choices = [Choice()]
        usage = None

    class Completions:
        async def create(self, **request):
            calls.append(request)
            return Reply()

    class Client:
        class chat:
            completions = Completions()

    async def fit(messages, **kwargs):
        return list(messages), kwargs.get("requested_max_tokens")

    monkeypatch.setattr(llm, "_openai_client", lambda: Client())
    monkeypatch.setattr(llm, "_fit", fit)
    monkeypatch.setattr(llm._admission, "run", lambda create, **_kwargs: create())
    asyncio.run(llm.json_completion([{"role": "user", "content": "x"}], max_tokens=5))
    assert "timeout" not in calls[0]


# ── M12: no card about an object the copy does not hold, and a card that reads ───


def test_the_card_that_lost_its_field_name_reads_as_a_sentence():
    question = (
        "Do you want the total number of ContentDocument records, or the number of unique ContentDocument "
        "records that are currently linked to other records (ParentId is not null)?"
    )
    cleaned = humanize(question)
    assert "( is not null)" not in cleaned
    assert "null" not in cleaned
    assert cleaned.endswith("linked to other records?")


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Filter on ParentId first", "Filter on parent first"),
        ("Which RecordTypeId do you mean?", "Which record type do you mean?"),
        ("Group by RecordType.Name please", "Group by record type please"),
        ("Internal_Interview__c rows", "internal interview rows"),
        ("The SOQL query", "The query"),
        ("Records (all of them)", "Records (all of them)"),
        ("Fully Paid invoices", "Fully Paid invoices"),
    ],
)
def test_a_field_name_becomes_words_rather_than_a_hole(text, expected):
    assert humanize(text) == expected


KNOWN = ["Interview__c", "ContentDocument", "ContentVersion", "Internal_Interview__c", "Account"]


def test_a_card_on_a_custom_object_the_copy_lacks_is_not_asked():
    question = "How many Training_Mock_Result__c are there?"
    out = _enforced(question, _card("metric", "Rows", "People"), known_objects=KNOWN)
    assert out.action == "EXECUTE_SALESFORCE"
    assert out.internal_reason_code == "object_not_synced"
    assert out.assumptions[-1] == "Training_Mock_Result__c is not in the synced copy."


def test_that_note_reaches_the_writer_as_not_accessible():
    sql_engine._planner_handback.set(None)
    sql_engine.note_planner_handback(["Training_Mock_Result__c is not in the synced copy."])
    assert "Training_Mock_Result__c" in sql_engine._planner_handback.get().note
    sql_engine._planner_handback.set(None)


def test_an_object_a_card_option_names_is_checked_too():
    out = _enforced(
        "how many results are there?",
        _card("object", "Mock results", "Verdicts", values={"Mock results": "Training_Mock_Result__c"}),
        known_objects=KNOWN,
    )
    assert out.internal_reason_code == "object_not_synced"


@pytest.mark.parametrize(
    "labels",
    [("All ContentDocument records", "Only linked ContentDocuments"), ("Content Documents", "Content Versions", "Content Document Links")],
)
def test_the_stored_card_on_content_document_links_is_not_asked(labels):
    out = _enforced("How many content document links do we have in Salesforce?", _card("metric", *labels), known_objects=KNOWN)
    assert out.internal_reason_code == "object_not_synced"
    assert "not in the synced copy" not in out.assumptions[-1], "a shape only costs the card, never the answer"


def test_a_word_shaped_like_an_object_costs_the_card_and_not_the_answer():
    out = _enforced(
        "How many candidates came from each source?",
        _card("metric", "From LinkedIn", "From referrals", question="LinkedIn or referrals?"),
        known_objects=KNOWN,
    )
    assert out.action == "EXECUTE_SALESFORCE"
    sql_engine._planner_handback.set(None)
    sql_engine.note_planner_handback(out.assumptions)
    assert sql_engine._planner_handback.get() is None, "the writer must not be told LinkedIn is missing"


@pytest.mark.parametrize("known", [None, []])
def test_an_unreadable_catalogue_changes_nothing(known):
    question = "How many Training_Mock_Result__c are there?"
    assert _enforced(question, _card("metric", "Rows", "People"), known_objects=known).action == "ASK_CLARIFICATION"


def test_a_card_on_an_object_the_copy_holds_is_still_asked():
    question = "How many Internal_Interview__c are there?"
    assert _enforced(question, _card("metric", "Rows", "People"), known_objects=KNOWN).action == "ASK_CLARIFICATION"


# ── M13: the failure line says what happened ─────────────────────────────────


def test_two_refused_statements_are_not_called_nothing_run(warehouse, monkeypatch):
    bad = "SELECT Nope__c FROM Invoice__c"
    Model(monkeypatch, bad, "SELECT Nope2__c FROM Invoice__c")
    text = ask("how many invoices?")[0]
    assert "nothing was run" not in text
    assert "database refused both attempts" in text
    assert "Naming the object usually helps" in text


def test_the_object_advice_is_dropped_once_the_person_is_resolved(warehouse, monkeypatch):
    bad = "SELECT Nope__c FROM Recruiter__c"
    Model(monkeypatch, bad, "SELECT Nope2__c FROM Recruiter__c")
    text = ask("what is the department of Asha Varnok?")[0]
    assert "database refused both attempts" in text
    assert "Naming the object" not in text


def test_could_not_find_about_a_person_gives_no_object_advice(warehouse, monkeypatch):
    Model(monkeypatch, "NO_SUCH_OBJECT")
    text = ask("how many widgets does Asha Varnok hold?")[0]
    assert COULD_NOT_FIND in text
    assert "Naming the object" not in text
    Model(monkeypatch, "NO_SUCH_OBJECT")
    assert "Naming the object usually helps" in ask("how many widgets are there?")[0]


# ── A pasted link to the org is not opened ───────────────────────────────────

ORG_LINKS = [
    "https://techsara.lightning.force.com/lightning/r/Lead/00QXx0000001AAA/view",
    "https://techsara--dev9.sandbox.lightning.force.com/lightning/r/Lead/00QXx0000001AAA/view",
    "https://techsara--dev9.sandbox.my.salesforce.com/lightning/r/Lead/00QXx0000001AAA/view",
    "https://techsara.my.salesforce.com/secur/frontdoor.jsp?sid=abc",
    "https://techsara.file.force.com/sfc/servlet.shepherd/version/download/068Xx",
    "https://techsara.my.site.com/portal/s/",
    "https://techsara--c.visualforce.com/apex/Page",
    "https://TECHSARA.Lightning.Force.com/x",
]
OPEN_LINKS = [
    "https://help.salesforce.com/s/articleView?id=1",
    "https://www.salesforce.com/products/",
    "https://developer.salesforce.com/docs",
    "https://othercompany.my.site.com/portal/",
    "https://othercompany.lightning.force.com/x",
    "https://techsara.example.com/",
    "https://example.com/techsara.lightning.force.com",
]


@pytest.fixture()
def the_org(monkeypatch):
    monkeypatch.setattr(settings, "sf_lightning_base_url", "https://techsara.lightning.force.com")
    monkeypatch.delenv("SF_LOGIN_URL", raising=False)


@pytest.mark.parametrize("link", ORG_LINKS)
def test_every_host_of_the_org_is_refused_without_a_lookup(the_org, monkeypatch, link):
    def no_lookup(_host):
        raise AssertionError("a refused host must not even be looked up")

    monkeypatch.setattr(net, "resolve_public_ips", no_lookup)
    assert net.is_org_host(link)
    with pytest.raises(net.UnsafeURLError):
        net.assert_url_is_fetchable(link)


@pytest.mark.parametrize("link", OPEN_LINKS)
def test_the_rest_of_the_web_stays_fetchable(the_org, monkeypatch, link):
    monkeypatch.setattr(net, "resolve_public_ips", lambda _host: ["93.184.216.34"])
    assert not net.is_org_host(link)
    assert net.assert_url_is_fetchable(link) == link


def test_the_login_host_is_refused_exactly_and_names_the_org(monkeypatch):
    monkeypatch.setattr(settings, "sf_lightning_base_url", "https://techsara.lightning.force.com")
    monkeypatch.setenv("SF_LOGIN_URL", "https://login.salesforce.com")
    assert net.is_org_host("https://login.salesforce.com/services/oauth2/token")
    assert not net.is_org_host("https://help.salesforce.com/")
    assert not net.is_org_host("https://login.my.site.com/"), "'login' names no org"
    monkeypatch.setenv("SF_LOGIN_URL", "https://acme--uat.sandbox.my.salesforce.com")
    assert net.is_org_host("https://acme.lightning.force.com/x")


def test_a_short_link_that_redirects_to_the_org_is_refused_at_the_hop(the_org, monkeypatch):
    import httpx

    monkeypatch.setattr(net, "resolve_public_ips", lambda _host: ["93.184.216.34"])
    wire = []

    def handler(request):
        wire.append(str(request.url))
        return httpx.Response(302, headers={"location": ORG_LINKS[0]})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        net.httpx, "AsyncClient",
        lambda *args, **kwargs: real(*args, **{**kwargs, "transport": httpx.MockTransport(handler)}),
    )
    with pytest.raises(net.UnsafeURLError):
        asyncio.run(net.safe_fetch("https://short.example/abc", timeout_ms=1000, max_bytes=1000))
    assert wire == ["https://short.example/abc"]


def _url_engine(monkeypatch, stored=()):
    calls = {"model": 0, "fetched": []}

    async def no_robots(_url):
        raise AssertionError("robots.txt on the org host is a request to the org too")

    async def fetch_and_store(_conversation, url, _emit, user_id=None):
        calls["fetched"].append(url)
        return {"url": url, "title": "A page", "text": "text", "site_crawl": None}

    async def stream_chat_events(_messages, **_kwargs):
        calls["model"] += 1
        yield "token", "From the page."

    async def in_thread(function, *_args, **_kwargs):
        return list(calls["fetched"]) and [
            {"url": u, "title": "A page", "text": "text"} for u in calls["fetched"]
        ] if function is url_engine.db.get_url_documents else list(stored)

    monkeypatch.setattr(url_engine, "fetch_and_store", fetch_and_store)
    monkeypatch.setattr(url_engine.robots, "allowed", no_robots)
    monkeypatch.setattr(url_engine.llm, "stream_chat_events", stream_chat_events)
    monkeypatch.setattr(url_engine.db, "run_in_thread", in_thread)
    return calls


def test_a_crawl_of_the_org_is_not_started(the_org, monkeypatch):
    from app.engines import crawl

    async def no_row(*_a, **_k):
        raise AssertionError("no crawl row, no fetch, no model call")

    monkeypatch.setattr(crawl.db, "run_in_thread", no_row)
    rec = Rec()
    text = asyncio.run(crawl.run_crawl_engine("crawl it", ORG_LINKS[0], "c", [], rec.emit))
    assert text == url_engine.ORG_LINK_LINE
    assert rec.meta()["route"] == "crawl"


def test_a_link_to_the_org_alone_gets_the_plain_line_and_no_model_call(the_org, monkeypatch):
    calls = _url_engine(monkeypatch)
    rec = Rec()
    text = asyncio.run(url_engine.run_url_engine(ORG_LINKS[0], [ORG_LINKS[0]], "c", [], rec.emit))
    assert text == url_engine.ORG_LINK_LINE
    assert "not fetched" in text
    assert "".join(d["text"] for e, d in rec.events if e == "token") == text
    assert calls == {"model": 0, "fetched": []}
    assert rec.meta()["fetch_failed"] is True


def test_another_link_beside_it_is_still_read(the_org, monkeypatch):
    calls = _url_engine(monkeypatch)
    rec = Rec()
    links = [ORG_LINKS[0], "https://example.com/page"]
    text = asyncio.run(url_engine.run_url_engine("compare " + " ".join(links), links, "c", [], rec.emit))
    assert calls["fetched"] == ["https://example.com/page"]
    assert text.startswith(url_engine.ORG_LINK_LINE)
    assert text.endswith("From the page.")
    assert "".join(d["text"] for e, d in rec.events if e == "token") == text


# ─────────────────────────────────────────────────────────────────────────────
# Review of this build: faults found in its own fixes, each pinned below
# ─────────────────────────────────────────────────────────────────────────────


def _add(warehouse, table, columns, rows=()):
    con = duckdb.connect(warehouse)
    _create(con, table, columns, rows)
    con.close()
    schema_cache.invalidate()


def _figures(model):
    return model.narrative_user().split("Computed figures")[1].split("Result sample")[0]


# ── "No table holds this" on the retry stands ────────────────────────────────


@pytest.mark.parametrize(
    "first",
    [
        "SELECT COUNT(*) AS n FROM Invoice__c WHERE Invoice_Status__c != 'Paid'",
        "SELECT COUNT(*) AS n FROM Interview__c WHERE Status__c = 'Mock Result'",
    ],
)
def test_the_sentinel_on_a_retry_overrules_a_first_figure_that_only_had_advice(warehouse, monkeypatch, first):
    Model(monkeypatch, first, "NO_SUCH_OBJECT")
    text, meta = ask("how many flux capacitors are configured?")
    assert COULD_NOT_FIND in text
    assert "data" not in meta


def test_a_retry_refused_only_by_the_hand_back_keeps_the_first_result(warehouse, monkeypatch):
    """The first statement read what was asked. Only the retry wandered off."""
    Model(
        monkeypatch,
        "SELECT COUNT(*) AS n FROM Invoice__c WHERE Invoice_Status__c != 'Paid'",
        "SELECT COUNT(*) AS n FROM Account",
    )

    async def turn():
        rec = Rec()
        sql_engine.note_planner_handback([], not_a_record_question=True)
        return await run_sql_engine("how many invoices are not paid?", [], rec.emit), rec.meta()

    text, meta = asyncio.run(turn())
    assert COULD_NOT_FIND not in text
    assert meta["data"] == [{"n": 1}]


# ── The credential rule reads a value's kind and a count's statement ─────────


@pytest.mark.parametrize(
    "statement, column, value",
    [
        ("SELECT COUNT(*) AS question_bank_count FROM x", "question_bank_count", 70),
        ("SELECT COUNT(*) AS Question_Bank_Count FROM x", "Question_Bank_Count", 70),
        ("SELECT d, COUNT(*) AS question_bank_questions FROM x GROUP BY 1", "question_bank_questions", 50),
        ("SELECT COUNT(*) AS total_question_banks FROM x", "total_question_banks", 4),
        ("SELECT COUNT(Passport_Number__c) AS passport_count FROM x", "passport_count", 3),
        ("SELECT COUNT(Passport_Number__c) AS passport_on_file FROM x", "passport_on_file", 3),
        ("SELECT COUNT(*) AS login_count FROM x", "login_count", 9),
        ("SELECT COUNT(*) AS Candidate_Portal_Credential_Count FROM x", "Candidate_Portal_Credential_Count", 5),
        ("SELECT COUNT(*) FILTER (WHERE a) AS Onboarding_Token_Count FROM x", "Onboarding_Token_Count", 7),
        ("SELECT COUNT(*) AS contact_records FROM x", "contact_records", 2),
        ("SELECT SUM(Failed_Login_Count__c) AS failed_logins FROM x", "failed_logins", 6),
        ("SELECT Failed_Login_Count__c FROM x", "Failed_Login_Count__c", 2),
        ("SELECT Last_Login_At__c FROM x", "Last_Login_At__c", "2026-05-05T10:00:00"),
        ("SELECT last_login FROM x", "last_login", datetime(2026, 5, 5)),
        ("SELECT Name AS Question_Bank FROM x", "Question_Bank", "Apex Basics"),
        ("SELECT Question_Bank__c FROM x", "Question_Bank__c", "Flows"),
        ("SELECT Password_Changed_Date__c FROM x", "Password_Changed_Date__c", "2026-05-05"),
        ("SELECT LastPasswordChangeDate FROM x", "LastPasswordChangeDate", datetime(2026, 5, 5)),
        ("SELECT LinkedIn_Password_Updated__c FROM x", "LinkedIn_Password_Updated__c", "true"),
    ],
)
def test_what_only_sounds_like_a_credential_is_kept(statement, column, value):
    result = sql_engine._hold_back(statement, ["Name", column], [["Asha", value]], "how many are there?", {})
    assert result[1] == ["Name", column]
    assert "credentials" not in result.left_out


@pytest.mark.parametrize(
    "statement, column, value",
    [
        ("SELECT Passport_Number__c FROM x", "Passport_Number__c", "ZX9988776"),
        ("SELECT Passport_Number__c FROM x", "Passport_Number__c", 998877),
        ("SELECT Last_4_digits_of_SSN__c FROM x", "Last_4_digits_of_SSN__c", 1234),
        ("SELECT Last_4_digits_of_SSN__c AS ssn_last4 FROM x", "ssn_last4", 1234),
        ("SELECT SUM(Last_4_digits_of_SSN__c) AS ssn_total FROM x", "ssn_total", 1234),
        ("SELECT MAX(Passport_Number__c) AS passport_max FROM x", "passport_max", "ZX9988776"),
        ("SELECT Passport_Number__c AS passport_no FROM x", "passport_no", "ZX9988776"),
        ("SELECT COUNT(*) AS passport_count FROM x", "passport_count", "ZX9988776"),
        ("SELECT Passport_Expiry__c FROM x", "Passport_Expiry__c", "2026-05-05"),
        ("SELECT Bank_Account_Last4__c FROM x", "Bank_Account_Last4__c", "1234"),
        ("SELECT Bank_Setup_Summary__c FROM x", "Bank_Setup_Summary__c", "text"),
        ("SELECT DS_Bank_Name__c FROM x", "DS_Bank_Name__c", "A Bank"),
        ("SELECT Username__c FROM x", "Username__c", "someone"),
        ("SELECT Portal_Login_Id__c FROM x", "Portal_Login_Id__c", "someone"),
        ("SELECT Portal_Login__c FROM x", "Portal_Login__c", "someone"),
        # The owner's list, and what this rule did not hold until now.
        ("SELECT External_Meeting_Passcode__c FROM x", "External_Meeting_Passcode__c", "12345"),
        ("SELECT Zoom_Passcode_IS__c FROM x", "Zoom_Passcode_IS__c", "12345"),
        ("SELECT DS_ACH_Identification_Number__c FROM x", "DS_ACH_Identification_Number__c", "ACH4242"),
        ("SELECT DS_Service_Identification_Number__c FROM x", "DS_Service_Identification_Number__c", "SVC4242"),
        ("SELECT Session_Id__c FROM x", "Session_Id__c", "abc123"),
        ("SELECT AccessToken FROM x", "AccessToken", "abc123"),
        ("SELECT DeleteToken FROM x", "DeleteToken", "abc123"),
        ("SELECT Account_Number__c FROM x", "Account_Number__c", "4242"),
        ("SELECT Security_Answer__c FROM x", "Security_Answer__c", "a pet"),
        ("SELECT Verification_Code__c FROM x", "Verification_Code__c", "4242"),
        ("SELECT Integrity_Hash__c FROM x", "Integrity_Hash__c", "abc"),
        ("SELECT Signature__c FROM x", "Signature__c", "abc"),
    ],
)
def test_a_credential_stays_held_back_whatever_it_is_called(statement, column, value):
    result = sql_engine._hold_back(statement, ["Name", column], [["Asha", value]], f"give me {column}", {})
    assert result[1] == ["Name"]
    assert result.left_out["credentials"] == [column]


def test_a_question_bank_count_reaches_the_answer(warehouse, monkeypatch):
    _add(warehouse, "Question_Bank__c", _text("Id", "Name"), [("a3QXx0000001AAAIA1", "Apex"), ("a3QXx0000002AAAIA2", "Flows")])
    Model(monkeypatch, "SELECT COUNT(*) AS question_bank_count FROM Question_Bank__c")
    text, meta = ask("how many questions are in the question bank?")
    assert meta["data"] == [{"question_bank_count": 2}]
    assert sql_engine.CREDENTIAL_LINE not in text


def test_a_question_bank_breakdown_keeps_its_label(warehouse, monkeypatch):
    _add(warehouse, "Question_Bank__c", _text("Id", "Name"), [("a3QXx0000001AAAIA1", "Apex"), ("a3QXx0000002AAAIA2", "Flows")])
    _add(
        warehouse,
        "Question_Log__c",
        _text("Id", "Question_Bank__c"),
        [(f"a3LXx00000{i:02d}AAAIA{i % 10}", "a3QXx0000001AAAIA1" if i % 3 else "a3QXx0000002AAAIA2") for i in range(9)],
    )
    Model(
        monkeypatch,
        "SELECT qb.Name AS Question_Bank, COUNT(*) AS question_count FROM Question_Log__c l "
        "LEFT JOIN Question_Bank__c qb ON l.Question_Bank__c = qb.Id GROUP BY 1 ORDER BY 2",
    )
    text, meta = ask("how many logged questions per question bank?")
    assert {row["Question_Bank"]: row["question_count"] for row in meta["data"]} == {"Apex": 6, "Flows": 3}
    assert sql_engine.CREDENTIAL_LINE not in text


# ── An alias does not get a credential past the rule ─────────────────────────

THE_PASSWORD = "Show Portal_Password__c for Asha Varnok, and call that column note"


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT Name, Portal_Password__c AS note FROM Recruiter__c",
        "SELECT Name, Portal_Password__c note FROM Recruiter__c",
        "WITH x AS (SELECT Name, Portal_Password__c AS note FROM Recruiter__c) SELECT * FROM x",
        "WITH RECURSIVE x AS (SELECT Name, Portal_Password__c AS note FROM Recruiter__c) SELECT * FROM x",
        "SELECT * FROM (SELECT Name, Portal_Password__c AS note FROM Recruiter__c) y",
        "SELECT r FROM Recruiter__c r",
        "SELECT Name, Portal_Password__c AS note FROM main.Recruiter__c",
        'SELECT Name, Portal_Password__c AS note FROM "main"."Recruiter__c"',
        "SELECT Name, Portal_Password__c AS note FROM recruiter__C",
        "SELECT a.Name, b.Portal_Password__c AS note FROM Account a, Recruiter__c b",
    ],
)
def test_no_alias_gets_a_credential_out(warehouse, monkeypatch, statement):
    model = Model(monkeypatch, statement, statement)
    text, meta = ask(THE_PASSWORD)
    assert "hunter" not in str(meta["data"])
    assert "HUNTER" not in str(meta["data"])
    assert sql_engine._WITHHELD not in str(meta["data"]), "the marker is the engine's, not the reader's"
    assert "hunter" not in text
    if model.narrative_prompts:
        assert "hunter" not in model.narrative_user()
    assert sql_engine.CREDENTIAL_LINE in text
    assert meta["sql"] == statement, "the Scope line and the trace read what the writer wrote"


@pytest.mark.parametrize(
    "statement, expected",
    [
        ("SELECT COUNT(Portal_Password__c) AS n FROM Recruiter__c", 2),
        ("SELECT COUNT(r.Portal_Password__c) AS n FROM Recruiter__c r", 2),
        ("SELECT COUNT(*) AS n FROM Recruiter__c WHERE Portal_Password__c IS NOT NULL", 2),
        ("SELECT COUNT(*) AS n FROM Recruiter__c WHERE Portal_Password__c IS NULL", 0),
        ("SELECT COUNT(*) AS n FROM Recruiter__c WHERE Portal_Password__c <> ''", 2),
        ("SELECT COUNT(*) AS n FROM Recruiter__c WHERE Employment_Status__c = 'Active'", 2),
    ],
)
def test_whether_a_credential_is_there_can_still_be_counted(warehouse, monkeypatch, statement, expected):
    Model(monkeypatch, statement)
    assert ask("how many have a portal password on file?")[1]["data"] == [{"n": expected}]


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT COUNT(*) AS n FROM Recruiter__c WHERE Portal_Password__c = 'hunter2'",
        "SELECT COUNT(*) AS n FROM Recruiter__c WHERE Portal_Password__c LIKE 'hunter%'",
        "SELECT COUNT(*) AS n FROM Recruiter__c WHERE lower(r.Portal_Password__c) = 'hunter2'",
        "SELECT COUNT(DISTINCT Portal_Password__c) AS n FROM Recruiter__c",
        "SELECT DISTINCT Name, Portal_Password__c FROM Recruiter__c",
        "SELECT Portal_Password__c AS p, COUNT(*) AS n FROM Recruiter__c GROUP BY Portal_Password__c",
        "SELECT COUNT(*) AS n FROM Recruiter__c a JOIN Recruiter__c b ON a.Portal_Password__c = b.Portal_Password__c",
        "SELECT Name || Portal_Password__c AS who FROM Recruiter__c",
        "SELECT upper(Portal_Password__c) AS note FROM Recruiter__c",
        "SELECT Name, COLUMNS('Portal_.*') AS note FROM Recruiter__c",
        "SELECT Name, note FROM Recruiter__c AS r(a, Name, c, d, e, f, g, h, i, j, note, l)",
    ],
)
def test_a_statement_that_compares_a_credential_is_refused_with_no_number(warehouse, monkeypatch, statement):
    """The mask would answer these with a wrong number, and unmasked they test a guess."""
    model = Model(monkeypatch, statement)
    text, meta = ask("how many use the password hunter2?")
    assert text == sql_engine.CREDENTIAL_LINE
    assert "data" not in meta
    assert len(model.writer_prompts) == 1, "no retry can change what the person asked"


def test_the_agent_route_refuses_it_the_same_way(warehouse, monkeypatch):
    AgentModel(monkeypatch, [], "SELECT COUNT(*) AS n FROM Recruiter__c WHERE Portal_Password__c = 'hunter2'")
    output, _detail, meta = run_step("count them", message="how many use the password hunter2?")
    assert output.startswith(sql_engine.CREDENTIAL_LINE)
    assert meta == {}


def test_a_statement_in_a_string_cannot_read_past_the_engine():
    from app.core.sql_guard import SQLGuardError, guard_sql

    for statement in ("SELECT * FROM query_table('Recruiter__c')", "SELECT * FROM query('SELECT * FROM raw.Recruiter__c')"):
        with pytest.raises(SQLGuardError):
            guard_sql(statement)


# ── Secrets the rule did not name, and secrets inside a link ─────────────────


@pytest.fixture()
def session_rows(warehouse):
    _add(
        warehouse,
        "Session__c",
        _text("Id", "Name", "External_Meeting_Passcode__c", "Meeting_Link__c", "Notes__c"),
        [("a4SXx0000001AAAIA1", "S-1", "998877", "https://zoom.example/j/1234?pwd=abc123&uname=x", "Passcode: 554433 for the call")],
    )
    return warehouse


def test_a_passcode_is_never_shown(session_rows, monkeypatch):
    Model(monkeypatch, "SELECT Name, External_Meeting_Passcode__c FROM Session__c")
    text, meta = ask("What is the passcode of session S-1?")
    assert "998877" not in str(meta["data"]) and "998877" not in text
    assert sql_engine.CREDENTIAL_LINE in text


@pytest.mark.parametrize(
    "statement", ["SELECT Name, Meeting_Link__c FROM Session__c", "SELECT Name, Meeting_Link__c AS meeting_link FROM Session__c"]
)
def test_a_secret_inside_a_link_goes_and_the_link_stays(session_rows, monkeypatch, statement):
    model = Model(monkeypatch, statement)
    _text, meta = ask("What is the meeting link of session S-1?")
    link = next(value for key, value in meta["data"][0].items() if key != "Name")
    assert link == "https://zoom.example/j/1234?pwd=[hidden]&uname=x"
    assert "abc123" not in model.narrative_user()


def test_a_labelled_secret_in_free_text_goes_even_with_nothing_dropped(session_rows, monkeypatch):
    Model(monkeypatch, "SELECT Name, Notes__c FROM Session__c")
    _text, meta = ask("What are the notes of session S-1?")
    assert meta["data"] == [{"Name": "S-1", "Notes__c": "Passcode: [hidden] for the call"}]


def test_no_stored_value_hint_prints_an_identity_number(warehouse, monkeypatch):
    _add(warehouse, "Pay_Detail__c", _text("Id", "Name", "DS_ACH_Identification_Number__c"), [("a5PXx0000001AAAIA1", "P-1", "ACH4242")])
    schema = schema_cache.get(settings.duckdb_path)
    statement = "SELECT Name FROM Pay_Detail__c WHERE DS_ACH_Identification_Number__c = 'X999'"
    assert sql_engine._literal_misses(statement, schema) == []
    block = sql_engine._stored_values_block(
        {"Pay_Detail__c": schema["Pay_Detail__c"]}, "Which pay detail has ACH identification number X999?"
    )
    assert "ACH4242" not in block
    Model(monkeypatch, statement)
    text = ask("Which pay detail has ACH identification number X999?")[0]
    assert "ACH4242" not in text and "It holds" not in text


def test_a_credential_object_withholds_its_text_whatever_a_column_is_called(warehouse, monkeypatch):
    _add(
        warehouse,
        "Candidate_Portal_Session__c",
        {**_text("Id", "Name", "Candidate_Credential__c", "IP_Address__c", "Device_Note__c"), "Is_Active__c": "BOOLEAN"},
        [("a6SXx0000001AAAIA1", "PS-1", "a6CXx0000001AAAIA1", "10.0.0.9", "a laptop", "true")],
    )
    schema = schema_cache.get(settings.duckdb_path)
    columns = dict(schema["Candidate_Portal_Session__c"])
    held = {c for c, dtype in columns.items() if sql_engine._is_secret_column("Candidate_Portal_Session__c", c, dtype)}
    assert held == {"IP_Address__c", "Device_Note__c"}
    model = Model(monkeypatch, "SELECT Name, IP_Address__c AS from_where, Device_Note__c AS what FROM Candidate_Portal_Session__c")
    text, meta = ask("where do the portal sessions come from?")
    assert meta["data"] == [{"Name": "PS-1"}]
    assert "10.0.0.9" not in model.narrative_user()
    assert sql_engine.CREDENTIAL_LINE in text
    Model(monkeypatch, "SELECT COUNT(*) AS n FROM Candidate_Portal_Session__c WHERE Is_Active__c = true")
    assert ask("how many portal sessions are active?")[1]["data"] == [{"n": 1}]


# ── A contact word unlocks its own kind, and no other word does ──────────────


@pytest.mark.parametrize(
    "column, value, question",
    [
        ("Candidate_Phone_Number__c", "+1 555 010 0003", "Which candidate was on each interview? What's the status?"),
        ("Interviewer_s_Email__c", "x@example.test", "Which candidate was on each interview? What's the status?"),
        ("Candidate_Phone_Number__c", "+1 555 010 0003", "Show the interview details for candidate Jane Roe"),
        ("Interviewer_s_Email__c", "x@example.test", "What's the status of Jane Roe's interview?"),
        ("Candidate_Email__c", "x@example.test", "Show the candidate training record for Jane Roe"),
        ("Company_Email__c", "x@example.test", "Which company does she work for?"),
        ("Current_Address__Street__s", "1 Example Road", "What's the current status?"),
        ("Recruiter_s_Email__c", "x@example.test", "Who is the recruiter on this interview?"),
        ("Recruiter_Contact__c", "Sam", "Who is the recruiter on this interview?"),
        ("Contact_Phone__c", "+1 555 010 0003", "List the contacts"),
        ("Candidate_Phone_Number__c", "+1 555 010 0003", "Who is the candidate on this interview?"),
    ],
)
def test_a_word_of_the_columns_name_is_no_request_for_it(column, value, question):
    result = sql_engine._hold_back("SELECT 1", ["Name", column], [["Asha", value]], question, {})
    assert result[1] == ["Name"]
    assert result.left_out["contact"] == [column]


@pytest.mark.parametrize(
    "column, value, question",
    [
        ("Candidate_Email__c", "x@example.test", "candidate email"),
        ("Candidate_Phone_Number__c", "+1 555 010 0003", "what is the candidate phone number"),
        ("Home_Address__c", "1 Example Road", "what is her home address"),
        ("Company_Email__c", "x@example.test", "what is her company email"),
        ("Interviewer_s_Email__c", "x@example.test", "What's the interviewer's email?"),
        ("Interviewer_s_Email__c", "x@example.test", "give me Interviewer_s_Email__c"),
    ],
)
def test_the_contact_word_or_the_exact_name_still_asks(column, value, question):
    assert sql_engine._hold_back("SELECT 1", ["Name", column], [["Asha", value]], question, {})[1] == ["Name", column]


def test_the_exact_name_of_a_standard_field_still_asks():
    schema = {"Account": [("Name", "VARCHAR"), ("PersonEmail", "VARCHAR")]}
    result = sql_engine._hold_back("SELECT Name, PersonEmail FROM Account", ["Name", "PersonEmail"], [["Asha", "x@example.test"]], "give me PersonEmail", schema)
    assert result[1] == ["Name", "PersonEmail"]


def test_an_unasked_phone_is_gone_from_every_payload(warehouse, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "reports_dir", str(tmp_path))
    _add(warehouse, "Mock_Round__c", _text("Id", "Name", "Candidate_Phone_Number__c", "Status__c"), [("a7MXx0000001AAAIA1", "R-1", "+1 555 010 0003", "Done")])
    statement = "SELECT Name, Candidate_Phone_Number__c, Status__c FROM Mock_Round__c"
    model = Model(monkeypatch, statement)
    _text_answer, meta = ask("Which candidate was on each round? What's the status?")
    assert meta["data"] == [{"Name": "R-1", "Status__c": "Done"}]
    assert "555" not in model.narrative_user()
    Model(monkeypatch, statement)
    exported = ask("Export the candidate list for each round to csv")[1]["report_files"][0]["filename"]
    assert "555" not in (tmp_path / exported).read_text(encoding="utf-8")


# ── What a column holds is judged on all of it ───────────────────────────────

_SPARSE_LOOKUP = [(None if i < 200 else f"a8KXx00000{i % 100:02d}AAAIA{i % 10}") for i in range(300)]
_LATE_EMAILS = [(None if i < 200 else f"person{i}@example.test") for i in range(300)]


@pytest.fixture()
def sparse_rows(warehouse):
    _add(
        warehouse,
        "Long_List__c",
        _text("Id", "Name", "Lead_Link__c", "Notes__c", "Remark__c"),
        [
            (f"a9LXx000{i:04d}AAAIA{i % 10}", f"L-{i}", _SPARSE_LOOKUP[i], _LATE_EMAILS[i], "plain" if i < 200 else f"person{i}@example.test")
            for i in range(300)
        ],
    )
    return warehouse


def test_ids_and_e_mails_past_row_200_are_held_back_like_any_others(sparse_rows, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "reports_dir", str(tmp_path))
    statement = "SELECT Name, Lead_Link__c, Notes__c FROM Long_List__c"
    model = Model(monkeypatch, statement)
    text, meta = ask("list these")
    assert list(meta["data"][0]) == ["Name"]
    assert "Contact details and record ids were left out" in text
    assert "@" not in model.narrative_user()
    Model(monkeypatch, statement)
    exported = ask("export these to csv")[1]["report_files"][0]["filename"]
    assert "@" not in (tmp_path / exported).read_text(encoding="utf-8")


def test_e_mails_past_clean_text_do_not_reach_the_payload(sparse_rows, monkeypatch):
    Model(monkeypatch, "SELECT Name, Remark__c FROM Long_List__c")
    _text_answer, meta = ask("list these")
    remarks = [row["Remark__c"] for row in meta["data"]]
    assert remarks[:200] == ["plain"] * 200
    assert not any("@" in remark for remark in remarks)


def test_a_sparse_lookup_the_question_names_is_retried_as_its_name(sparse_rows, monkeypatch):
    model = Model(
        monkeypatch,
        "SELECT Name, Lead_Link__c FROM Long_List__c",
        "SELECT Name FROM Long_List__c",
    )
    ask("what is the lead link of each one?")
    assert len(model.writer_prompts) == 2
    assert "Lead_Link__c holds raw record Ids" in model.writer_user()


def _names_with_one_email(at=7, size=50):
    return [[f"name-{i}" if i != at else "stray@example.test", "Open"] for i in range(size)]


LEADS = {"Lead": [("Name", "VARCHAR"), ("Status", "VARCHAR")]}


@pytest.mark.parametrize("question", ["List the leads and their status", "List the lead names and their status"])
def test_one_stray_e_mail_costs_that_cell_and_not_the_column(question):
    rows = _names_with_one_email()
    result = sql_engine._hold_back("SELECT Name, Status FROM Lead", ["Name", "Status"], rows, question, LEADS)
    assert result[1] == ["Name", "Status"]
    assert result[2][7][0] == "[hidden]"
    assert [row[0] for row in result[2][:3]] == ["name-0", "name-1", "name-2"]
    assert result.left_out == {"masked": ["Name"]}
    assert "were hidden" in sql_engine._left_out_line(result.left_out)
    assert "left out" not in sql_engine._left_out_line(result.left_out)


def test_a_stray_e_mail_past_row_200_is_hidden_too():
    rows = _names_with_one_email(at=250, size=300)
    result = sql_engine._hold_back("SELECT Name, Status FROM Lead", ["Name", "Status"], rows, "list the leads", LEADS)
    assert result[2][250][0] == "[hidden]"
    assert result[2][249][0] == "name-249"


def test_masked_is_not_said_to_be_held_back(warehouse, monkeypatch):
    _add(warehouse, "Lead", _text("Id", "Name", "Status"), [(f"00QXx00000{i:02d}AAAIA{i % 10}", "stray@example.test" if i == 3 else f"lead-{i}", "Open") for i in range(9)])
    model = Model(monkeypatch, "SELECT Name, Status FROM Lead")
    ask("list the leads")
    assert "Held back from these rows" not in model.narrative_user()


def test_the_columns_own_word_keeps_it_plural_or_not():
    schema = {"Interview__c": [("Position__c", "VARCHAR")]}
    rows = [["x@example.test"]]
    assert sql_engine._hold_back("SELECT Position__c FROM Interview__c", ["Position__c"], rows, "Show the positions", schema)[1] == ["Position__c"]
    assert sql_engine._hold_back("SELECT Position__c FROM Interview__c", ["Position__c"], rows, "Show the position", schema)[1] == ["Position__c"]
    assert sql_engine._hold_back("SELECT Position__c FROM Interview__c", ["Position__c"], rows, "Show them", schema)[1] == []


def test_a_words_alias_proves_nothing():
    """`list(Email__c) AS people` for "list people by status" is no request for an e-mail."""
    schema = {"Recruiter__c": [("Email__c", "VARCHAR")]}
    result = sql_engine._hold_back("SELECT Email__c AS people FROM Recruiter__c", ["people"], [["x@example.test"]], "list people", schema)
    assert result[1] == []


@pytest.mark.parametrize(
    "column, value",
    [("Date__c", "31-12-2026"), ("Salary_Compensation__c", "900000-1200000"), ("Salary_Compensation__c", "900000 - 1200000")],
)
def test_what_only_looks_like_a_phone_is_kept_and_unmasked(column, value):
    rows = [["a", value], ["b", value]]
    schema = {"T__c": [("Name", "VARCHAR"), (column, "VARCHAR")]}
    result = sql_engine._hold_back(f"SELECT Name, {column} FROM T__c", ["Name", column], rows, "show these", schema)
    assert result[1] == ["Name", column]
    assert result[2] == rows
    assert result.left_out == {}


@pytest.mark.parametrize("value", ["98765-43210", "+91 98765 43210", "9876543210"])
def test_a_real_phone_shape_stays_a_phone(value):
    assert sql_engine._looks_like_phone(value)


def test_the_value_check_does_not_stall_on_a_long_string():
    import time

    long_text = "a" * 50_000
    rows = [[long_text, f"n{i}"] for i in range(300)]
    started = time.monotonic()
    sql_engine._hold_back("SELECT 1", ["Notes", "Name"], rows, "show these", {})
    assert time.monotonic() - started < 5


@pytest.mark.parametrize(
    "columns, row, question, kept",
    [
        (["Phone_No__c"], ["+1 555 010 0001"], "What is the number for Asha Varnok?", ["Phone_No__c"]),
        (["Phone_No__c"], ["+1 555 010 0001"], "What is Asha Varnok's number?", ["Phone_No__c"]),
        (["Name", "Phone_No__c", "cnt"], ["Asha", "+1 555 010 0001", 4], "What is the number of interviews for Asha Varnok?", ["Name", "cnt"]),
        (["Name", "Email__c"], ["Asha", "x@example.test"], "What is the number for Asha Varnok?", ["Name"]),
    ],
)
def test_the_number_for_someone_is_their_phone(columns, row, question, kept):
    assert sql_engine._hold_back("SELECT 1", columns, [row], question, {})[1] == kept


@pytest.mark.parametrize(
    "statement, column",
    [
        ("SELECT Employment_Status__c AS st, list(Email__c) AS members FROM Recruiter__c GROUP BY 1", "members"),
        ("SELECT Employment_Status__c AS st, string_agg(Phone_No__c, ', ') AS members FROM Recruiter__c GROUP BY 1", "members"),
        ("SELECT Employment_Status__c AS st, array_agg(Phone_No__c) AS members FROM Recruiter__c GROUP BY 1", "members"),
        ("SELECT Name, Phone_No__c || ' ext' AS line_info FROM Recruiter__c", "line_info"),
        ("SELECT Name, Name || ' (' || Phone_No__c || ')' AS member FROM Recruiter__c", "member"),
        ("SELECT Name, struct_pack(e := Email__c) AS info FROM Recruiter__c", "info"),
        ("SELECT Employment_Status__c AS st, list(Id) AS members FROM Recruiter__c GROUP BY 1", "members"),
    ],
)
def test_contact_details_in_a_list_or_a_longer_cell_are_held_back(warehouse, monkeypatch, statement, column):
    model = Model(monkeypatch, statement)
    _text_answer, meta = ask("List people by status")
    assert column not in meta["data"][0]
    assert "example.test" not in str(meta["data"]) and "555 010" not in str(meta["data"])
    assert "example.test" not in model.narrative_user() and "555 010" not in model.narrative_user()
    assert PERSON_ONE not in str(meta["data"])


def test_a_phone_in_free_text_is_hidden_and_the_text_stays(warehouse, monkeypatch):
    _add(warehouse, "Call_Log__c", _text("Id", "Subject"), [("a9CXx0000001AAAIA1", "SMS from Invented Person +15550100001")])
    model = Model(monkeypatch, "SELECT Subject FROM Call_Log__c")
    text, meta = ask("show recent call logs")
    assert meta["data"] == [{"Subject": "SMS from Invented Person [hidden]"}]
    assert "15550100001" not in model.narrative_user()
    assert "were hidden" in text


@pytest.mark.parametrize(
    "value",
    [
        "a0BXx0000001AAAIA1_2026-05-05",
        "https://zoom.example/j/98765432101?pwd=abc",
        "https://files.example/resume/1234567890/cv.pdf",
        "INV-2026-000123",
        "2026-05-05 10:30:00",
        "31-12-2026 10:30",
        "paid 900000 - 1200000 last year",
        "1. 12-08-2019 2. 01-02-2020",
        "8.5)(2016-2020",
        "item 123456.\n98765",
        "0.12345678901234",
    ],
)
def test_what_holds_digits_and_no_phone_finds_none(value):
    assert sql_engine._phone_spans(value) == []


# ── A label the writer was told to write does not switch the Id check off ────

BLANK_BUCKET = "SELECT COALESCE(Department__c, '(blank)') AS dept, COUNT(*) AS n FROM Staff__c GROUP BY 1"


@pytest.fixture()
def staff(warehouse):
    _add(
        warehouse,
        "Staff__c",
        _text("Id", "Name", "Department__c"),
        [("a9TXx0000001AAAIA1", "Invented One", DEPT_SALES), ("a9TXx0000002AAAIA2", "Invented Two", DEPT_OPS), ("a9TXx0000003AAAIA3", "Invented Three", None)],
    )
    return warehouse


@pytest.mark.parametrize(
    "statement",
    [BLANK_BUCKET, BLANK_BUCKET + " ORDER BY dept"],
    ids=["as it comes", "the label first"],
)
def test_a_blank_bucket_keeps_the_names_of_the_rest(staff, monkeypatch, statement):
    model = Model(monkeypatch, statement, statement)
    text, meta = ask("how many employees per dept?")
    assert {row["dept"] for row in meta["data"]} == {"Sales", "Operations", "(blank)"}
    assert "raw record Ids" in model.writer_user()
    assert DEPT_SALES not in text and DEPT_SALES not in model.narrative_user()


@pytest.mark.parametrize("label", ["Unassigned", "No Department"])
def test_any_one_label_beside_ids_is_no_reason_to_show_them(staff, monkeypatch, label):
    statement = f"SELECT COALESCE(Department__c, '{label}') AS dept, COUNT(*) AS n FROM Staff__c GROUP BY 1"
    Model(monkeypatch, statement, statement)
    assert {row["dept"] for row in ask("how many employees per dept?")[1]["data"]} == {"Sales", "Operations", label}


# ── Every group keeps a label of its own ─────────────────────────────────────

CASE_ONE = "a2PXx0000001AAAIA1"
CASE_TWO = "a2PXx0000002AAAIA2"
TICKETS_PER_CASE = "SELECT Case_Parent__c, COUNT(*) AS n FROM Ticket__c GROUP BY 1"


def _tickets(warehouse, column, ids):
    _add(warehouse, "Ticket__c", _text("Id", column), [(f"a2TXx000{i:04d}AAAIA{i % 10}", value) for i, value in enumerate(ids)])


def test_a_parent_with_no_name_is_called_by_its_record_number(warehouse, monkeypatch):
    _add(warehouse, "Case_Parent__c", _text("Id", "CaseNumber"), [(CASE_ONE, "00001"), (CASE_TWO, "00002")])
    _tickets(warehouse, "Case_Parent__c", [CASE_ONE] * 6 + [CASE_TWO] * 3)
    model = Model(monkeypatch, TICKETS_PER_CASE, TICKETS_PER_CASE)
    _text_answer, meta = ask("how many tickets per case?")
    assert {row["Case_Parent__c"]: row["n"] for row in meta["data"]} == {"00001": 6, "00002": 3}
    assert "Case_Parent__c.CaseNumber" in model.writer_user(), "the advice must name a column that binds"
    assert "Case_Parent__c.Name" not in model.writer_user()


def test_a_parent_with_no_label_at_all_leaves_the_groups_apart(warehouse, monkeypatch):
    _add(warehouse, "Case_Parent__c", _text("Id", "Status__c"), [(CASE_ONE, "Open"), (CASE_TWO, "Open")])
    _tickets(warehouse, "Case_Parent__c", [CASE_ONE] * 6 + [CASE_TWO] * 3)
    Model(monkeypatch, TICKETS_PER_CASE, TICKETS_PER_CASE)
    labels = [row["Case_Parent__c"] for row in ask("how many tickets per case?")[1]["data"]]
    assert len(set(labels)) == 2
    assert all(label.startswith("(unnamed record") for label in labels)


def test_a_lookup_to_two_objects_reads_each_ids_own_parent(warehouse, monkeypatch):
    user, queue_a, queue_b = "005Xx0000001AAAIA1", "00GXx0000001AAAIA1", "00GXx0000002AAAIA2"
    _add(warehouse, "User", _text("Id", "Name"), [(user, "Invented Owner")])
    _add(warehouse, "Group", _text("Id", "Name"), [(queue_a, "Queue A"), (queue_b, "Queue B")])
    _tickets(warehouse, "OwnerId", [user] * 6 + [queue_a] * 3 + [queue_b] * 2)
    statement = "SELECT OwnerId, COUNT(*) AS n FROM Ticket__c GROUP BY 1"
    Model(monkeypatch, statement, statement)
    labels = {row["OwnerId"]: row["n"] for row in ask("how many tickets per owner?")[1]["data"]}
    assert labels == {"Invented Owner": 6, "Queue A": 3, "Queue B": 2}


def test_a_row_that_is_gone_does_not_cost_the_others_their_names(warehouse, monkeypatch):
    gone_one, gone_two = "a28Xx0000099AAAIA1", "a28Xx0000098AAAIA2"
    _tickets(warehouse, "Department__c", [gone_one] * 6 + [gone_two] * 4 + [DEPT_SALES] * 3 + [DEPT_OPS] * 2)
    statement = "SELECT Department__c, COUNT(*) AS n FROM Ticket__c GROUP BY 1 ORDER BY 2 DESC"
    Model(monkeypatch, statement, statement)
    data = ask("how many tickets per department?")[1]["data"]
    labels = {row["n"]: row["Department__c"] for row in data}
    assert labels[3] == "Sales" and labels[2] == "Operations"
    assert labels[6].startswith("(unnamed record") and labels[4].startswith("(unnamed record")
    assert labels[6] != labels[4]


# ── An alias is not an API name ──────────────────────────────────────────────

ALIASED_LOOKUP = "SELECT Name, Department__c AS Department FROM Recruiter__c WHERE Name = 'Asha Varnok'"


@pytest.mark.parametrize("retry", ["SELECT broken FROM Recruiter__c", ALIASED_LOOKUP], ids=["a failing retry", "a repeated retry"])
def test_an_alias_the_question_uses_does_not_keep_a_raw_id(warehouse, monkeypatch, retry):
    model = Model(monkeypatch, ALIASED_LOOKUP, retry)
    text, meta = ask("Which department is Asha Varnok in?")
    assert meta["data"] == [{"Name": "Asha Varnok"}]
    assert DEPT_SALES not in model.narrative_user()
    assert "Record ids were left out" in text


@pytest.mark.parametrize("name", ["Manager__c", "OwnerId"])
def test_a_lookup_asked_for_by_its_api_name_still_shows_its_id(name):
    result = sql_engine._hold_back("SELECT 1", ["Name", name], [["Asha", PERSON_ONE]], f"give me {name}", {})
    assert result[1] == ["Name", name]


def test_a_plain_text_alias_is_kept():
    assert sql_engine._hold_back("SELECT 1", ["Department"], [["Sales"]], "which department?", {})[1] == ["Department"]


# ── The label of a group is what its numbers mean ────────────────────────────


@pytest.mark.parametrize(
    "column, value",
    [("PersonMailingState", "CA"), ("MailingCity", "Springfield"), ("Contact_Name", "Sam"), ("ContactSource", "Web")],
)
def test_a_group_keeps_a_label_that_reaches_nobody(column, value):
    rows = [[value, 8], [value + "2", 4]]
    result = sql_engine._hold_back("SELECT 1", [column, "n"], rows, "how many per person?", {})
    assert result[1] == [column, "n"]
    assert result.left_out == {}


def test_a_group_keyed_on_a_contact_lookup_shows_the_parents_name(warehouse, monkeypatch):
    _add(warehouse, "Contact", _text("Id", "Name"), [("003Xx0000001AAAIA1", "Invented One"), ("003Xx0000002AAAIA2", "Invented Two")])
    _add(warehouse, "Ticket__c", _text("Id", "ContactId"), [(f"a9KXx00000{i:02d}AAAIA{i % 10}", "003Xx0000001AAAIA1" if i % 2 else "003Xx0000002AAAIA2") for i in range(6)])
    statement = "SELECT ContactId, COUNT(*) AS n FROM Ticket__c GROUP BY 1"
    Model(monkeypatch, statement, statement)
    assert {row["ContactId"] for row in ask("how many tickets per person?")[1]["data"]} == {"Invented One", "Invented Two"}


def test_the_city_asked_about_is_kept():
    result = sql_engine._hold_back("SELECT 1", ["Name", "MailingCity"], [["Asha", "Springfield"]], "which city does each person live in?", {})
    assert result[1] == ["Name", "MailingCity"]


@pytest.mark.parametrize(
    "column, value",
    [("Email__c", "x@example.test"), ("Slack_User_Id__c", "U01"), ("Home_Address__c", "1 Example Road"), ("MailingStreet", "1 Example Road")],
)
def test_bare_numbers_are_no_answer_when_their_label_is_held_back(column, value):
    rows = [[value, 8], [value + "2", 4]]
    result = sql_engine._hold_back("SELECT 1", [column, "n"], rows, "how many duplicates?", {})
    assert result[1] == []
    assert result.left_out == {"contact": [column]}


def test_one_row_of_a_figure_keeps_it():
    result = sql_engine._hold_back("SELECT 1", ["Email__c", "n"], [["x@example.test", 8]], "how many duplicates?", {})
    assert result[1] == ["n"]


def test_a_record_ids_figures_stay_when_no_group_was_asked_for():
    columns = ["Id", "Owner__c", "Amount"]
    rows = [[PERSON_ONE, PERSON_TWO, 5], [PERSON_TWO, PERSON_ONE, 6]]
    assert sql_engine._hold_back("SELECT Id, Owner__c, Amount FROM Invoice__c", columns, rows, "list the amounts", {})[1] == ["Amount"]
    grouped = "SELECT Id, Owner__c, COUNT(*) AS Amount FROM Invoice__c GROUP BY 1, 2"
    assert sql_engine._hold_back(grouped, columns, rows, "list the amounts", {})[1] == []


@pytest.mark.parametrize(
    "column, question",
    [("Emergency_Contact_Name__c", "who is this?"), ("MailingStreet", "who is this?"), ("MailingPostalCode", "who is this?"), ("PersonMailingLatitude", "who is this?"), ("PersonEmail", "who is this person?")],
)
def test_what_does_reach_somebody_stays_held_back(column, question):
    assert sql_engine._hold_back("SELECT 1", ["Name", column], [["Asha", "someone@example.test"]], question, {})[1] == ["Name"]


# ── A person who is not there is said not to be there ────────────────────────


@pytest.mark.parametrize("question", ["tell me about Zed Quill", "how do I get hold of Zed Quill?"])
def test_an_empty_result_holds_nothing_back(warehouse, monkeypatch, question):
    Model(monkeypatch, "SELECT Email__c, Phone_No__c FROM Recruiter__c WHERE Name ILIKE '%quill%'")
    text, _meta = ask(question)
    assert sql_engine.ZERO_ROWS_LINE in text
    assert "left out" not in text


def test_a_partly_contact_empty_result_has_no_left_out_footer(warehouse, monkeypatch):
    Model(monkeypatch, "SELECT Name, Email__c FROM Recruiter__c WHERE Name ILIKE '%quill%'")
    assert "left out" not in ask("tell me about Zed Quill")[0]


def test_an_empty_result_holds_nothing_back_on_the_agent_route(warehouse, monkeypatch):
    model = AgentModel(monkeypatch, [sql_step(input="who is zed quill")], "SELECT Email__c, Phone_No__c FROM Recruiter__c WHERE Name ILIKE '%quill%'")
    text = ask_agent("tell me about Zed Quill")[0]
    assert "SQL result (0 row(s))" in model.step_results()
    assert sql_engine.ZERO_ROWS_LINE in model.step_results()
    assert "left out" not in text


def test_a_person_who_is_there_still_gets_the_left_out_line(warehouse, monkeypatch):
    Model(monkeypatch, "SELECT Email__c, Phone_No__c FROM Recruiter__c WHERE Name ILIKE '%varnok%'")
    text = ask("tell me about Asha Varnok")[0]
    assert "Contact details were left out" in text
    assert sql_engine.ZERO_ROWS_LINE not in text


# ── Held back is not empty, on any route ─────────────────────────────────────

ONLY_IDS = "SELECT Id FROM Background_Check__c WHERE Background_Check_Status__c = 'Pending'"
ONLY_CONTACT = "SELECT Email__c, Phone_No__c FROM Recruiter__c"
ONLY_INTERNAL = "SELECT COMPASS_State_JSON__c FROM Recruiter__c"
NOT_ASKED = "The query read only fields that are not shown unless asked for by name."


def test_every_column_held_back_still_counts_the_records_directly(warehouse, monkeypatch):
    Model(monkeypatch, ONLY_IDS)
    text, meta = ask("Which background checks have pending items?")
    assert text.startswith("47 records matched. Record ids were left out")
    assert meta["data"] == []
    assert meta["salesforce_sources"]["record_count"] == 47


def test_a_limit_does_not_pass_for_how_many_matched(warehouse, monkeypatch):
    Model(monkeypatch, "SELECT Id FROM Background_Check__c LIMIT 30")
    text, meta = ask("Which background checks have pending items?")
    assert text.startswith("47 records matched.")
    assert meta["salesforce_sources"]["record_count"] == 47


@pytest.mark.parametrize(
    "statement, says",
    [(ONLY_IDS, "47 records matched. Record ids were left out"), (ONLY_CONTACT, "2 records matched. Contact details were left out"), (ONLY_INTERNAL, f"2 records matched. {NOT_ASKED}")],
)
def test_the_agent_route_sends_no_rows_of_nothing(warehouse, monkeypatch, statement, says):
    model = AgentModel(monkeypatch, [sql_step(input="list them")], statement)
    text, meta = ask_agent("Which ones have pending items?")
    assert meta["data"] == []
    assert says in model.step_results()
    assert says in text
    assert "SQL result (" not in model.step_results()


def test_a_contact_count_is_a_figure_and_not_a_contact_field(warehouse, monkeypatch):
    Model(monkeypatch, "SELECT COUNT(*) AS contact_records FROM Recruiter__c")
    text, meta = ask("How many people are there?")
    assert meta["data"] == [{"contact_records": 2}]
    assert "left out" not in text


@pytest.mark.parametrize(
    "statement, instruction, says",
    [
        (ONLY_IDS, "make a doc about the total background checks", "47 records matched."),
        (ONLY_CONTACT, "make a doc about the total staff", "2 records matched. Contact details were left out"),
        (ONLY_INTERNAL, "make a doc about the total staff", NOT_ASKED),
    ],
)
def test_a_document_is_never_told_held_back_data_is_empty(warehouse, monkeypatch, statement, instruction, says):
    from app.engines import artifact

    Model(monkeypatch, statement)
    tables, sources, notes = asyncio.run(artifact._salesforce_table(instruction, []))
    assert tables == [] and sources == []
    assert says in notes[0]
    assert "returned no rows" not in notes[0]
    assert "must not call it empty" in notes[0]


def test_a_document_says_what_was_cut_from_its_table(warehouse, monkeypatch):
    from app.engines import artifact

    Model(monkeypatch, "SELECT * FROM Recruiter__c")
    tables, _sources, notes = asyncio.run(artifact._salesforce_table("make a word doc of the top 10 staff with all fields", []))
    assert "Phone_No__c" not in tables[0].columns
    assert any("Contact details and record ids were left out" in note for note in notes)


def test_a_document_with_nothing_read_is_still_told_so(warehouse, monkeypatch):
    from app.engines import artifact

    Model(monkeypatch, "SELECT Name FROM Recruiter__c WHERE Name = 'Nobody'")
    _tables, _sources, notes = asyncio.run(artifact._salesforce_table("make a doc about the total staff", []))
    assert "matched no rows" in notes[0]
    assert "must not call the data empty" in notes[0]


@pytest.mark.parametrize(
    "statement, says",
    [(ONLY_IDS, "47 records matched."), (ONLY_CONTACT, "2 records matched. Contact details were left out"), (ONLY_INTERNAL, NOT_ASKED)],
)
def test_a_report_section_never_writes_no_data_over_held_back_rows(warehouse, monkeypatch, tmp_path, statement, says):
    from app.engines import report

    model = Model(monkeypatch, statement)
    section = {"title": "Staff", "instruction": "report on them", "chart": False}
    parts = asyncio.run(report._sql_section(section, 1, tmp_path, "report on them"))
    assert "_No data._" not in parts
    assert says in parts[0]
    assert len(model.writer_prompts) == 1, "rows of nothing get no prose written about them"


def test_a_report_section_says_what_was_cut_from_its_table(warehouse, monkeypatch, tmp_path):
    from app.engines import report

    Model(monkeypatch, "SELECT * FROM Recruiter__c", "Some prose.")
    section = {"title": "Staff", "instruction": "report on them", "chart": False}
    parts = asyncio.run(report._sql_section(section, 1, tmp_path, "report on them"))
    assert any("Contact details and record ids were left out" in part for part in parts)
    assert not any("Phone_No__c" in part for part in parts)


# ── The Scope line says what ran, part by part ───────────────────────────────

SCOPE_SCHEMA = {
    table: [("Id", "VARCHAR")]
    for table in (
        "Interview__c", "Invoice__c", "Deliverable__c", "Candidate_Training__c", "Session__c", "Account", "RecordType",
        "Recruiter__c", "Program__c", "Program_Version__c", "Contact", "Pre_Enrolment_Request__c", "Lead",
    )
}
SCOPE_SCHEMA["Interview__c"] = [("Id", "VARCHAR"), ("Date_of_Interview__c", "DATE")]


def _scope(statement):
    return sql_engine._counted_line(statement, SCOPE_SCHEMA)


def test_a_figures_own_filter_is_not_said_of_the_whole_result():
    line = _scope(
        "SELECT COUNT(*) AS total_interviews, COUNT(*) FILTER (WHERE Interview_Outcome__c = 'Ghosted') AS ghosted FROM Interview__c"
    )
    assert line.startswith("Scope: Interview__c. Some figures count only rows where Interview_Outcome__c = 'Ghosted'.")
    assert "every row" not in line


def test_the_ghosting_rate_definition_says_its_filters_are_per_figure():
    metric = next(m for m in ob.METRICS if m["name"] == "ghosting rate")
    line = _scope(metric["sql"])
    assert "where Interview_Outcome__c" not in line.split("Some figures")[0]
    assert "Some figures count only rows where Interview_Outcome__c = 'Ghosted', or only where Interview_Outcome__c is filled in." in line


def test_a_figures_own_filter_says_nothing_of_blank_rows():
    line = _scope("SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE Invoice_Status__c != 'Paid') AS not_paid FROM Invoice__c")
    assert "left out too" not in line
    assert "Invoice_Status__c != 'Paid'" in line


def test_a_filter_on_the_record_type_of_one_figure_leaves_the_rest_every_type():
    line = _scope(
        "SELECT COUNT(*) AS all_rows, COUNT(*) FILTER (WHERE rt.Name = 'Interview') AS client FROM Interview__c i "
        "JOIN RecordType rt ON i.RecordTypeId = rt.Id"
    )
    assert "That covers every Interview__c record type" in line


def test_figures_own_filters_do_not_crowd_out_the_real_one():
    filters = ", ".join(f"COUNT(*) FILTER (WHERE Round__c = '{i}') AS r{i}" for i in range(7))
    line = _scope(f"SELECT {filters} FROM Interview__c i WHERE i.Status__c = 'Completed'")
    assert line.startswith("Scope: Interview__c, where Status__c = 'Completed'.")


def test_the_stored_monthly_ghosting_statement_keeps_its_real_filters_apart():
    """d02, first attempt. Its total_interviews is not ghosted-only."""
    line = _scope(
        "WITH interview_counts AS (SELECT date_trunc('month', TRY_CAST(i.Date_of_Interview__c AS DATE)) AS interview_month, "
        "COUNT(*) AS total_interviews, COUNT(*) FILTER (WHERE i.Interview_Outcome__c = 'Ghosted') AS ghosted_interviews, "
        "COUNT(*) FILTER (WHERE i.Interview_Outcome__c IS NOT NULL) AS outcome_known_interviews "
        "FROM Interview__c i JOIN RecordType rt ON i.RecordTypeId = rt.Id WHERE rt.Name = 'Interview' "
        "AND TRY_CAST(i.Date_of_Interview__c AS DATE) >= DATE '2026-01-01' GROUP BY 1) "
        "SELECT total_interviews, ghosted_interviews FROM interview_counts"
    )
    head, figures = line.split("Some figures")
    assert "RecordType.Name = 'Interview'" in head and "Date_of_Interview__c >= DATE '2026-01-01'" in head
    assert "Ghosted" not in head and "Ghosted" in figures


#: a03: three sibling CTEs, and a line nothing could ever match.
SIBLING_CTES = (
    "WITH status_counts AS (SELECT Status__c, COUNT(*) AS n FROM Deliverable__c WHERE Status__c IS NOT NULL GROUP BY Status__c), "
    "locked AS (SELECT SUM(n) AS t FROM status_counts WHERE Status__c = 'Locked'), "
    "done AS (SELECT SUM(n) AS t FROM status_counts WHERE Status__c IN ('Completed', 'Reviewed')) "
    "SELECT sc.Status__c, sc.n, l.t, d.t FROM status_counts sc CROSS JOIN locked l CROSS JOIN done d"
)
#: c15, first attempt: the count is of trainings with NO session.
NO_SESSION_JOIN = (
    "WITH active_trainings AS (SELECT ct.Id FROM Candidate_Training__c ct WHERE ct.Status__c = 'Active'), "
    "trainings_with_sessions AS (SELECT DISTINCT s.Candidate_Training__c FROM Session__c s "
    "WHERE s.Candidate_Training__c IS NOT NULL AND s.Candidate_Training__c != '') "
    "SELECT count(at.Id) AS n FROM active_trainings at LEFT JOIN trainings_with_sessions tws "
    "ON at.Id = tws.Candidate_Training__c WHERE tws.Candidate_Training__c IS NULL"
)
#: c15, the attempt that ran.
NO_SESSION_NOT_IN = (
    "SELECT count(*) AS n FROM Candidate_Training__c ct WHERE ct.Status__c = 'Active' AND ct.Id NOT IN "
    "(SELECT s.Candidate_Training__c FROM Session__c s WHERE s.Candidate_Training__c IS NOT NULL AND s.Candidate_Training__c != '')"
)
UNION_WINDOWS = (
    "SELECT COUNT(*) AS n FROM Interview__c WHERE Date_of_Interview__c >= '2025-01-01' AND Date_of_Interview__c < '2026-01-01' "
    "UNION ALL SELECT COUNT(*) AS n FROM Interview__c WHERE Date_of_Interview__c >= '2026-01-01'"
)
NOT_GHOSTED = (
    "SELECT COUNT(*) AS n FROM Account WHERE Id NOT IN (SELECT Candidate__c FROM Interview__c "
    "WHERE Interview_Outcome__c = 'Ghosted' AND Candidate__c IS NOT NULL)"
)


@pytest.mark.parametrize("statement", [SIBLING_CTES, NO_SESSION_JOIN, NO_SESSION_NOT_IN, UNION_WINDOWS, NOT_GHOSTED])
def test_filters_of_different_parts_are_never_joined_with_and(statement):
    line = _scope(statement)
    assert "with filters that differ by part of the query" in line
    for wrong in (" and ", "left out too", "filled in", "every row", "Ghosted", "2026-01-01"):
        assert wrong not in line


def test_a_table_only_a_subquery_reads_is_not_joined():
    line = _scope(NOT_GHOSTED)
    assert line.startswith("Scope: Account, also reading Interview__c,")
    assert "joined to" not in line
    assert "every Interview__c record type" not in line, "the rows counted are not interviews"


def test_a_single_where_keeps_its_exact_line():
    assert _scope("SELECT 1 FROM Invoice__c i WHERE i.Name = 'x' AND i.Invoice_Status__c != 'Paid'") == (
        "Scope: Invoice__c, where Name = 'x' and Invoice_Status__c != 'Paid' (rows with no Invoice_Status__c are left out too)."
    )


def test_filters_that_stack_up_a_chain_of_ctes_are_still_listed():
    """c11, the attempt that ran: each part reads only the one before it."""
    line = _scope(
        "WITH submitted AS (SELECT p.Lead__c, l.IsConverted FROM Pre_Enrolment_Request__c p LEFT JOIN Lead l ON p.Lead__c = l.Id "
        "WHERE p.Status__c = 'Submitted'), prospects AS (SELECT * FROM submitted WHERE IsConverted = 'false' OR IsConverted IS NULL) "
        "SELECT COUNT(*) AS n FROM prospects"
    )
    assert "Status__c = 'Submitted'" in line
    assert "(IsConverted = 'false' OR IsConverted IS NULL)" in line
    assert "differ" not in line


def test_a_record_type_filter_inside_a_cte_is_no_every_type_line():
    line = _scope(
        "WITH a AS (SELECT Id FROM Interview__c WHERE RecordTypeId = '012Xx0000001AAA'), b AS (SELECT Id FROM Interview__c) "
        "SELECT COUNT(*) AS n FROM a JOIN b ON a.Id = b.Id"
    )
    assert "every Interview__c record type" not in line


#: c14, first pass: the rows with no program gone.
INNER_JOINED = (
    "SELECT Program__c.Name AS Program_Name, COUNT(*) AS Training_Count FROM Candidate_Training__c ct "
    "JOIN Program__c Program__c ON ct.Program__c = Program__c.Id GROUP BY Program__c.Name"
)


@pytest.mark.parametrize(
    "statement, expected",
    [
        (INNER_JOINED, "Scope: Candidate_Training__c, joined to Program__c (rows with no match are left out)."),
        (
            "SELECT COUNT(*) AS n FROM Interview__c i JOIN RecordType rt ON i.RecordTypeId = rt.Id AND rt.Name = 'Interview'",
            "Scope: Interview__c, where RecordType.Name = 'Interview'.",
        ),
        (
            "WITH t AS (SELECT i.Id, rt.Name AS kind FROM Interview__c i JOIN RecordType rt ON i.RecordTypeId = rt.Id) "
            "SELECT COUNT(*) AS n FROM t WHERE kind = 'Interview'",
            "Scope: Interview__c, where kind = 'Interview'.",
        ),
        (
            "SELECT r.Name, COUNT(*) AS n FROM Interview__c i JOIN Recruiter__c r ON i.Recruiter__c = r.Id GROUP BY r.Name HAVING COUNT(*) > 100",
            "Scope: Interview__c, joined to Recruiter__c (rows with no match are left out), where COUNT(*) > 100. "
            "That covers every Interview__c record type, initial calls included.",
        ),
        (
            "SELECT COUNT(*) AS n FROM RecordType rt JOIN Interview__c i ON i.RecordTypeId = rt.Id WHERE rt.Name = 'Interview'",
            "Scope: Interview__c, where RecordType.Name = 'Interview'.",
        ),
        (
            "SELECT p.Name, COUNT(ct.Id) AS n FROM Candidate_Training__c ct LEFT JOIN Program__c p ON ct.Program__c = p.Id GROUP BY p.Name",
            "Scope: every row of Candidate_Training__c, joined to Program__c.",
        ),
        (
            "SELECT COUNT(*) AS n FROM Interview__c",
            "Scope: every row of Interview__c. That covers every Interview__c record type, initial calls included.",
        ),
    ],
)
def test_every_row_is_said_only_when_no_row_can_have_dropped(statement, expected):
    assert _scope(statement) == expected


TOP_LEVEL_OR = "SELECT COUNT(*) AS n FROM Interview__c WHERE Status__c = 'Completed' OR Round__c = 'First' AND Interview_Outcome__c != 'Ghosted'"


def test_a_top_level_or_is_one_condition():
    assert sql_engine._filters(TOP_LEVEL_OR) == ["Status__c = 'Completed' OR Round__c = 'First' AND Interview_Outcome__c != 'Ghosted'"]


def test_a_top_level_or_says_nothing_false_and_costs_no_retry(warehouse, monkeypatch):
    model = Model(monkeypatch, TOP_LEVEL_OR)
    text, _meta = ask("how many interviews?")
    assert "left out too" not in text
    assert "Status__c = 'Completed' OR Round__c = 'First'" in text
    assert len(model.writer_prompts) == 1


def test_the_stored_or_with_two_not_equals_draws_no_advice(warehouse):
    """c09, pass 2."""
    statement = (
        "SELECT COUNT(*) AS n FROM Interview__c WHERE Round__c IS NULL OR Round__c = '' OR Round__c NOT IN (SELECT Id FROM Interview__c) "
        "AND Status__c != 'Rescheduled' AND Status__c != 'Cancelled'"
    )
    schema = schema_cache.get(settings.duckdb_path)
    assert sql_engine._null_drops(statement, ["n"], "how many?", schema) == []
    assert "left out too" not in sql_engine._counted_line(statement, schema)


def test_an_or_in_parentheses_is_still_read_as_a_filter(warehouse, monkeypatch):
    statement = "SELECT COUNT(*) AS n FROM Interview__c WHERE (Status__c = 'Completed' OR Round__c = 'First') AND Interview_Outcome__c != 'Ghosted'"
    model = Model(monkeypatch, statement, statement)
    text, _meta = ask("how many interviews?")
    assert "(rows with no Interview_Outcome__c are left out too)" in text
    assert "also drops the" in model.writer_user()


@pytest.mark.parametrize(
    "statement, expected",
    [
        ("SELECT COUNT(*) FROM Contact c WHERE c.Email = 'c.varnok@example.com'", "Email = 'c.varnok@example.com'"),
        ("SELECT COUNT(*) FROM Contact c WHERE c.Name = 'C.Orvane'", "Name = 'C.Orvane'"),
        ("SELECT COUNT(*) FROM Candidate_Training__c ct JOIN Program__c p ON ct.Program__c = p.Id WHERE p.Name = 'P.G. Diploma'", "Program__c.Name = 'P.G. Diploma'"),
    ],
)
def test_a_literal_is_never_spelled_out_as_an_alias(statement, expected):
    assert expected in _scope(statement)


@pytest.mark.parametrize("operator", ["IS DISTINCT FROM", "IS NOT DISTINCT FROM", "IS NOT\n  DISTINCT   FROM"])
def test_distinct_from_reads_no_table(operator):
    statement = (
        "SELECT COUNT(*) AS n FROM Candidate_Training__c x JOIN Program_Version__c y ON x.Program_Version__c = y.Id "
        f"WHERE x.Program__c {operator} y.Program__c"
    )
    assert sql_engine.objects_read(statement, SCOPE_SCHEMA) == ["Candidate_Training__c", "Program_Version__c"]


def test_distinct_from_a_literal_stays_a_filter():
    assert "Invoice_Status__c IS DISTINCT FROM 'Paid'" in _scope("SELECT COUNT(*) FROM Invoice__c WHERE Invoice_Status__c IS DISTINCT FROM 'Paid'")


def test_extract_reads_no_table():
    assert sql_engine._tables_named("SELECT EXTRACT(YEAR FROM c.CreatedDate) AS y FROM Contact c") == ["Contact"]


# ── "No date filter" is only said of a statement with none ───────────────────


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT COUNT(*) AS n FROM Period_Row__c WHERE Is_Current_Month__c = true",
        "SELECT COUNT(*) AS n FROM Period_Row__c WHERE Leave_Year__c = 2026",
        "SELECT COUNT(*) AS n FROM Period_Row__c WHERE Week_Number__c = 3",
        "WITH t AS (SELECT Date_of_Interview__c AS d FROM Interview__c) SELECT COUNT(*) AS n FROM t WHERE d >= today() - 30",
        "WITH t AS (SELECT Date_of_Interview__c AS d FROM Interview__c) SELECT COUNT(*) AS n FROM t WHERE d >= today() - INTERVAL 30 DAY",
        "WITH t AS (SELECT Date_of_Interview__c AS d FROM Interview__c) SELECT COUNT(*) AS n FROM t WHERE d IS NOT NULL",
        "SELECT COUNT(*) AS n FROM Period_Row__c p JOIN RecordType r ON p.Name = r.Name AND p.Is_Current_Month__c = true",
        "SELECT Leave_Year__c, COUNT(*) AS n FROM Period_Row__c GROUP BY Leave_Year__c HAVING Leave_Year__c = 2026",
    ],
)
def test_a_period_by_any_name_does_not_claim_all_dates(warehouse, monkeypatch, statement):
    _add(
        warehouse,
        "Period_Row__c",
        {**_text("Id", "Name"), "Is_Current_Month__c": "BOOLEAN", "Leave_Year__c": "BIGINT", "Week_Number__c": "BIGINT"},
        [("a1PXx0000001AAAIA1", "P-1", "true", "2026", "3")],
    )
    Model(monkeypatch, statement)
    assert "assumptions" not in ask("how many this month?")[1]


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT COUNT(*) AS n FROM Invoice__c",
        "SELECT COUNT(*) AS n FROM Invoice__c WHERE Invoice_Status__c = 'Paid'",
        "SELECT COUNT(*) AS n FROM Invoice__c WHERE Name = 'Week 3 month'",
    ],
)
def test_no_period_anywhere_still_says_all_dates(warehouse, monkeypatch, statement):
    Model(monkeypatch, statement, statement)
    assert ask("how many invoices?")[1]["assumptions"] == ["No date filter was applied, so this covers all dates."]


# ── A single ordered pick is what was asked ──────────────────────────────────

ORDERED_PICKS = [
    ("Who is the top round by interviews?", "SELECT Round__c, COUNT(*) AS n FROM Interview__c GROUP BY 1 ORDER BY n DESC LIMIT 1"),
    ("What was the last invoice raised?", "SELECT Name FROM Invoice__c ORDER BY Name DESC LIMIT 1"),
    ("Which interview is next?", "SELECT Name FROM Interview__c ORDER BY Date_of_Interview__c LIMIT 1"),
]


@pytest.mark.parametrize("question, statement", ORDERED_PICKS)
def test_an_ordered_pick_is_not_the_first_of_something_larger(warehouse, monkeypatch, question, statement):
    Model(monkeypatch, statement)
    text, meta = ask(question)
    assert "These are the first" not in text
    assert meta["salesforce_sources"]["truncated"] is False


@pytest.mark.parametrize("question, statement", ORDERED_PICKS)
def test_an_ordered_pick_is_not_cut_on_the_agent_route_either(warehouse, monkeypatch, question, statement):
    model = AgentModel(monkeypatch, [sql_step(input=question)], statement)
    _text_answer, meta = ask_agent(question)
    assert "the answer must say so" not in model.step_results()
    assert meta["salesforce_sources"]["truncated"] is False


@pytest.mark.parametrize(
    "question, statement",
    [
        ("list the background checks raised last month", "SELECT Name FROM Background_Check__c LIMIT 30"),
        ("show this background check", "SELECT Name FROM Background_Check__c LIMIT 1"),
        ("show this background check", "SELECT Name FROM (SELECT Name FROM Background_Check__c ORDER BY Name) LIMIT 1"),
    ],
)
def test_a_cut_nobody_asked_for_is_still_said(warehouse, monkeypatch, question, statement):
    Model(monkeypatch, statement)
    assert ask(question)[0].startswith("These are the first")


def test_last_month_is_no_request_to_drop_blanks(warehouse):
    schema = schema_cache.get(settings.duckdb_path)
    statement = "SELECT Status__c, COUNT(*) AS n FROM Interview__c WHERE Status__c IS NOT NULL GROUP BY Status__c"
    assert len(sql_engine._null_drops(statement, ["Status__c", "n"], "how many interviews by status last month?", schema)) == 1


# ── A misspelling is of one table, and never of a different thing ────────────


@pytest.mark.parametrize(
    "written, table, known, same",
    [
        ("Internal_Interview_Action_Log__c", "Internal_Interview_Question_Log__c", (), False),
        ("Internal_Interview_Section_Log__c", "Internal_Interview_Question_Log__c", (), False),
        ("ContactLineItem", "ContractLineItem", ("contact",), False),
        ("ContactHistory", "ContractHistory", ("contact",), False),
        ("Application", "AIApplication", ("application",), False),
        ("Application", "Application__c", (), True),
        ("Education_Histories__c", "Education_History__History", (), False),
        ("Education_Histories__c", "Education_History__c", (), True),
        ("ProductCategory", "ProductCategoryProduct", (), False),
        ("Employee_Leaves__c", "Employee_Leave__c", (), True),
        ("Intervew__c", "Interview__c", ("interview",), True),
        ("Case_Cost__c", "Case_Coat__c", (), False),
    ],
)
def test_a_word_of_a_name_is_compared_as_a_word(written, table, known, same):
    assert same_object(written, table, known) is same


@pytest.fixture()
def lookalikes(warehouse):
    _add(warehouse, "Invoice", _text("Id", "Name"))
    _add(warehouse, "AIApplication", _text("Id", "Name"))
    _add(warehouse, "Application__c", _text("Id", "Name"), [("a1AXx0000001AAAIA1", "A-1")])
    _add(warehouse, "Education_History__History", _text("Id", "Field"))
    _add(warehouse, "Education_History__c", _text("Id", "Name"), [("a1EXx0000001AAAIA1", "E-1")])
    _add(warehouse, "Internal_Interview_Question_Log__c", _text("Id", "Name"), [("a1QXx0000001AAAIA1", "Q-1")])
    return warehouse


def test_a_different_word_gets_no_retry_at_all(lookalikes, monkeypatch):
    model = Model(monkeypatch, "SELECT COUNT(*) AS n FROM Internal_Interview_Action_Log__c")
    text, meta = ask("how many action logs?")
    assert COULD_NOT_FIND in text
    assert "data" not in meta
    assert len(model.writer_prompts) == 1


@pytest.mark.parametrize(
    "written, real",
    [("Application", "Application__c"), ("Invoices__c", "Invoice__c"), ("Education_Histories__c", "Education_History__c")],
)
def test_a_plural_or_bare_name_reads_the_object_it_means(lookalikes, monkeypatch, written, real):
    model = Model(monkeypatch, f"SELECT COUNT(*) AS n FROM {written}", f"SELECT COUNT(*) AS n FROM {real}")
    _text_answer, meta = ask("how many of them are there?")
    assert f"spelled {real}" in model.writer_user()
    assert meta["data"][0]["n"] > 0, "never the count of its empty look-alike"


def test_two_tables_it_could_mean_is_could_not_find(lookalikes, monkeypatch):
    model = Model(monkeypatch, "SELECT COUNT(*) AS n FROM Invoices")
    assert COULD_NOT_FIND in ask("how many of them are there?")[0]
    assert len(model.writer_prompts) == 1


# ── The database's own words stay on the server ──────────────────────────────

BAD_CAST = "SELECT Name FROM Recruiter__c WHERE CAST(Email__c AS INT) < 5"


def test_a_refused_statement_quotes_no_value_on_the_direct_route(warehouse, monkeypatch, caplog):
    Model(monkeypatch, BAD_CAST, BAD_CAST, BAD_CAST)
    text, meta = ask("how many people?")
    assert COULD_NOT_WRITE in text
    assert meta["salesforce_error"] == sql_engine.QUERY_REFUSED
    assert meta["salesforce_error_kind"] == "Conversion"
    assert "example.test" not in str(meta) and "example.test" not in text
    assert "both SQL attempts failed" in caplog.text
    assert "example.test" not in caplog.text


def test_a_refused_statement_quotes_no_value_on_the_agent_route(warehouse, monkeypatch):
    model = AgentModel(monkeypatch, [sql_step(input="count them")], BAD_CAST, BAD_CAST, BAD_CAST)
    rec = Rec()
    text = asyncio.run(agent.run_agent_engine("how many people?", [], rec.emit, effort="think", salesforce=True, web=False))
    steps = [data for event, data in rec.events if event == "step" and data.get("status") == "failed"]
    assert steps and "example.test" not in str(steps)
    assert steps[0]["detail"] == sql_engine.QUERY_REFUSED
    assert "example.test" not in model.step_results()
    assert "example.test" not in text


def test_a_refused_statement_quotes_no_value_in_a_report(warehouse, monkeypatch, tmp_path):
    from app.engines import report

    Model(monkeypatch, BAD_CAST, BAD_CAST, BAD_CAST)
    section = {"title": "People", "instruction": "count them", "chart": False}
    with pytest.raises(sql_engine.QueryRefused) as caught:
        asyncio.run(report._sql_section(section, 1, tmp_path, "count them"))
    assert str(caught.value) == sql_engine.QUERY_REFUSED
    assert "example.test" not in caught.value.detail
    assert caught.value.category == "Conversion"


def test_the_retry_still_gets_the_real_error(warehouse, monkeypatch):
    model = Model(monkeypatch, BAD_CAST, "SELECT Name FROM Recruiter__c")
    ask("how many people?")
    assert "Could not convert string" in model.writer_user()


# ── A pasted link is read as the dial reads it ───────────────────────────────

SPELT_ORG_LINKS = [
    "https://techsara。my。salesforce。com/services/data",
    "https://techsara．my．salesforce．com/services/data",
    "https://techsara｡my｡salesforce｡com/services/data",
    "https://techsara.my.salesforce。com/x",
    "https://techsara.my.salesforce.com。/x",
    "https://techsara.my.salesforce-scrt.com/x",
    "https://techsara.builder.salesforce-experience.com/x",
    "https://techsara.builder.salesforce-communities.com/x",
]


@pytest.mark.parametrize("link", SPELT_ORG_LINKS)
def test_every_spelling_of_the_org_is_refused_without_a_lookup(the_org, monkeypatch, link):
    def no_lookup(_host):
        raise AssertionError("a refused host must not even be looked up")

    monkeypatch.setattr(net, "resolve_public_ips", no_lookup)
    assert net.is_org_host(link)
    with pytest.raises(net.OrgHostRefused):
        net.assert_url_is_fetchable(link)


def test_a_spelling_of_the_org_is_never_dialled(the_org, monkeypatch):
    import httpx

    monkeypatch.setattr(net, "resolve_public_ips", lambda _host: ["93.184.216.34"])
    wire = []

    def handler(request):
        wire.append(str(request.url))
        return httpx.Response(200)

    real = httpx.AsyncClient
    monkeypatch.setattr(
        net.httpx, "AsyncClient",
        lambda *args, **kwargs: real(*args, **{**kwargs, "transport": httpx.MockTransport(handler)}),
    )
    with pytest.raises(net.UnsafeURLError):
        asyncio.run(net.safe_fetch(SPELT_ORG_LINKS[0], timeout_ms=1000, max_bytes=1000))
    assert wire == []


@pytest.mark.parametrize("link", ["https://[::1/x", "https://", "not a url", "https://exa mple.com/"])
def test_what_cannot_be_parsed_is_not_the_orgs_and_does_not_raise(the_org, link):
    assert net.is_org_host(link) is False


# ── A webhook cannot be pointed at the org ───────────────────────────────────


@pytest.fixture()
def webhook_wire(the_org, monkeypatch):
    import socket

    import httpx

    from app.apiplatform.webhooks import ssrf

    looked_up = []

    def fake_dns(host, *_a, **_k):
        looked_up.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0))]

    seen = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.host == "hooks.example.com":
            return httpx.Response(302, headers={"location": ORG_LINKS[3]})
        return httpx.Response(200)

    monkeypatch.setattr(socket, "getaddrinfo", fake_dns)
    monkeypatch.setattr(ssrf, "_pinned_client", lambda _b: httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False))
    monkeypatch.setattr(ssrf, "_new_backend", lambda: type("Backend", (), {"pin": lambda self, _k, _v: None})())
    return ssrf, seen, looked_up


@pytest.mark.parametrize(
    "url",
    [
        "https://techsara.my.salesforce.com/services/apexrest/hook",
        "https://techsara.lightning.force.com/x",
        "https://techsara。my.salesforce.com/x",
    ],
)
def test_a_webhook_endpoint_on_the_org_is_refused_with_no_lookup(webhook_wire, url):
    ssrf, seen, looked_up = webhook_wire
    with pytest.raises(ssrf.UnsafeWebhookURL):
        ssrf.validate_url(url)
    with pytest.raises(ssrf.UnsafeWebhookURL):
        asyncio.run(ssrf.post_json(url, b"{}", {}))
    assert seen == [] and looked_up == []


def test_a_webhook_redirect_to_the_org_is_refused_at_the_hop(webhook_wire):
    ssrf, seen, looked_up = webhook_wire
    with pytest.raises(ssrf.UnsafeWebhookURL):
        asyncio.run(ssrf.post_json("https://hooks.example.com/in", b"{}", {}))
    assert seen == ["https://hooks.example.com/in"]
    assert looked_up == ["hooks.example.com"]


@pytest.mark.parametrize("url", ["https://help.salesforce.com/x", "https://othercompany.my.salesforce.com/x", "https://example.com/x"])
def test_any_other_webhook_endpoint_is_still_allowed(webhook_wire, url):
    ssrf, _seen, _looked_up = webhook_wire
    assert ssrf.validate_url(url).url == url


# ── A list of the org's fields is not for a public link ──────────────────────


@pytest.mark.parametrize(
    "question, reply",
    [("audit the Account object: which fields does it have?", "DESCRIBE Account"), ("how many objects are there?", "DESCRIBE")],
)
def test_an_agent_answer_from_the_catalogue_is_not_publicly_shareable(warehouse, monkeypatch, question, reply):
    from app import sharing

    AgentModel(monkeypatch, [sql_step(input=question)], reply, synthesis=("The fields are Id, Name and Industry.",))
    text, meta = ask_agent(question)
    assert meta["salesforce_catalogue"] is True
    assert "salesforce_sources" not in meta, "no statement ran, so no source is claimed"
    verdict = sharing.evaluate(
        [{"id": 1, "role": "user", "content": question, "meta": {}}, {"id": 2, "role": "assistant", "content": text, "meta": meta}],
        policy={"public_enabled": True, "workspace_enabled": True},
    )
    assert verdict.public_allowed is False
    assert verdict.workspace_allowed is True


# ── A 0 is said to be a 0 only of a figure ───────────────────────────────────


@pytest.mark.parametrize(
    "columns, row, aggregated",
    [
        (["Manager__c"], [None], None),
        (["Manager__c"], [None], False),
        (["Manager__c"], [""], False),
        (["Manager__c", "Assigned_Sr_Recruiter__c"], [None, None], False),
        (["No_of_Assigned_Candidates__c"], [0], False),
    ],
)
def test_a_blank_field_or_a_stored_zero_is_no_figure_of_nothing(columns, row, aggregated):
    computed = deterministic_summary(columns, [row], aggregated=aggregated)
    assert "zero_figure" not in computed
    assert sql_engine.zero_rows_prefix(computed) == ""


def test_a_blank_field_on_a_record_is_said_to_be_blank():
    computed = deterministic_summary(["Name", "Manager__c"], [["Asha", None]], aggregated=False)
    assert computed["blank_on_this_record"]["fields"] == ["Manager__c"]
    assert "blank_on_this_record" not in deterministic_summary(["n"], [[0]], aggregated=True)


@pytest.mark.parametrize(
    "statement, question, said",
    [
        (
            "SELECT Manager__c FROM Recruiter__c WHERE Name = 'Asha Varnok'",
            "who is the manager of Asha Varnok?",
            "blank_on_this_record",
        ),
        (
            "SELECT m.Name AS reports_to FROM Recruiter__c r LEFT JOIN Recruiter__c m ON r.Assigned_Sr_Recruiter__c = m.Id WHERE r.Name ILIKE '%mendrell%'",
            "who does Rovik Mendrell report to?",
            "not_found_through_this_link",
        ),
    ],
)
def test_a_record_with_a_blank_field_gets_no_warning(warehouse, monkeypatch, statement, question, said):
    model = Model(monkeypatch, statement)
    text, _meta = ask(question)
    assert "figure came back 0" not in text
    assert "figure came back 0" not in model.narrative_user()
    assert said in model.narrative_user()


def test_a_stored_zero_and_a_blank_contact_get_no_warning(warehouse, monkeypatch):
    _add(
        warehouse,
        "Staff_Load__c",
        {**_text("Id", "Name", "Email__c", "Phone_No__c"), "No_of_Assigned_Candidates__c": "BIGINT"},
        [("a2SXx0000001AAAIA1", "Invented One", None, None, "0")],
    )
    Model(monkeypatch, "SELECT No_of_Assigned_Candidates__c FROM Staff_Load__c WHERE Name = 'Invented One'")
    assert "figure came back 0" not in ask("how many candidates is Invented One assigned?")[0]
    Model(monkeypatch, "SELECT Email__c, Phone_No__c FROM Staff_Load__c WHERE Name = 'Invented One'")
    assert "figure came back 0" not in ask("what is the email and phone of Invented One?")[0]


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT COUNT(*) AS n FROM Invoice__c WHERE Name = 'nope'",
        "SELECT SUM(Outstanding_Amount__c) AS total FROM Invoice__c WHERE Name = 'nope'",
        "SELECT MIN(Date_of_Interview__c) AS earliest FROM Interview__c WHERE Name = 'nope'",
    ],
)
def test_a_figure_of_nothing_still_warns_on_both_routes(warehouse, monkeypatch, statement):
    said = "figure came back 0" if "COUNT" in statement else "no figure came back"
    Model(monkeypatch, statement)
    assert ask("what is the figure?")[0].startswith(f"The query ran and {'the ' if 'COUNT' in statement else ''}{said}")
    AgentModel(monkeypatch, [], statement)
    assert said in run_step("what is the figure?")[0]


def test_a_blank_field_gets_no_warning_on_the_agent_route(warehouse, monkeypatch):
    AgentModel(monkeypatch, [], "SELECT Manager__c FROM Recruiter__c WHERE Name = 'Asha Varnok'")
    assert "figure came back 0" not in run_step("who is the manager?", message="who is the manager of Asha Varnok?")[0]


# ── An empty table is said to be one ─────────────────────────────────────────


@pytest.fixture()
def empty_table(warehouse):
    _add(warehouse, "Asset", _text("Id", "Name"))
    return warehouse


@pytest.mark.parametrize("statement", ['SELECT COUNT(*) AS n FROM "Asset"', 'SELECT Id, Name FROM "Asset"'])
def test_an_empty_table_is_plainly_said_to_hold_no_rows(empty_table, monkeypatch, statement):
    model = Model(monkeypatch, statement)
    text, _meta = ask("How many assets do we have?")
    assert text.startswith("The synced copy of Asset holds no rows")
    assert "wrong join" not in text
    figures = _figures(model)
    assert "wrong join" not in figures and "never state" not in figures.lower()
    assert "holds no rows" in figures


def test_an_empty_table_is_said_on_the_agent_route_too(empty_table, monkeypatch):
    AgentModel(monkeypatch, [], 'SELECT COUNT(*) AS n FROM "Asset"')
    output = run_step("how many assets?")[0]
    assert "The synced copy of Asset holds no rows" in output
    assert "wrong join" not in output


@pytest.mark.parametrize(
    "statement",
    [
        # No filter and no join, on a table with rows: the column is what is blank.
        "SELECT COUNT(Blank_Field__c) AS n FROM Roster__c",
        # c06, the stored statement: records exist on other dates.
        "SELECT count(*) AS n FROM Roster__c r WHERE r.Attendance_Status__c = 'Absent' AND TRY_CAST(r.Scheduled_Date__c AS DATE) = (now() AT TIME ZONE 'Asia/Kolkata')::date",
        "SELECT COUNT(*) AS n FROM Roster__c WHERE Name = 'Nobody Stored'",
    ],
)
def test_a_table_with_rows_keeps_the_warning(warehouse, monkeypatch, statement):
    _add(
        warehouse,
        "Roster__c",
        _text("Id", "Name", "Blank_Field__c", "Attendance_Status__c", "Scheduled_Date__c"),
        [("a2RXx0000001AAAIA1", "R-1", None, "Absent", "2020-01-01")],
    )
    Model(monkeypatch, statement)
    assert ask("how many?")[0].startswith("The query ran and the figure came back 0")


# ── Rows of records are counted as records ───────────────────────────────────


def _record_rows():
    return [[f"Person {i}", "Active" if i % 3 else "Inactive", 10 + i] for i in range(60)]


@pytest.mark.parametrize("column", ["Total_Target__c", "Attempt_Count__c"])
def test_record_rows_keep_their_value_counts_whatever_a_number_is_called(column):
    computed = deterministic_summary(["Name", "Status__c", column], _record_rows(), aggregated=False)
    assert computed["value_counts"]["Status__c"]["denominator"] == 60
    assert computed["counts_cover"] == "every row in the result"
    assert computed["numeric_totals"][column]["non_empty"] == 60


def test_a_grouped_statement_still_loses_its_occurrence_counts():
    """c11, pass 2."""
    rows = [["Submitted", f"Visa{i}", 10 + i] for i in range(7)]
    columns = ["Status__c", "Current_Visa_Status__c", "count_forms"]
    statement = "SELECT Status__c, Current_Visa_Status__c, COUNT(*) AS count_forms FROM Pre_Enrolment_Request__c GROUP BY 1, 2"
    computed = deterministic_summary(columns, rows, aggregated=sql_engine.is_aggregate(statement))
    assert "value_counts" not in computed
    assert "carries its own figures" in computed["counts_cover"]


def test_unique_labels_with_a_number_still_get_a_breakdown():
    rows = [[f"Person {i}", 10 + i] for i in range(5)]
    computed = deterministic_summary(["Name", "Amount"], rows, aggregated=False)
    assert "value_counts" not in computed
    assert computed["row_breakdown"]["Person 0"] == 10


def test_a_record_list_with_a_measure_named_number_is_counted_on_the_route(warehouse, monkeypatch):
    _add(
        warehouse,
        "Deliverable_Row__c",
        {**_text("Id", "Name", "Status__c"), "Attempt_Count__c": "BIGINT"},
        [(f"a2DXx00000{i:02d}AAAIA{i % 10}", f"D-{i}", "Approved" if i % 3 else "Open", "2") for i in range(9)],
    )
    model = Model(monkeypatch, "SELECT Name, Status__c, Attempt_Count__c FROM Deliverable_Row__c")
    ask("show deliverables with status and attempt count: how many are approved?")
    assert '"value": "Approved", "count": 6' in _figures(model)
    assert "not a count of records" not in _figures(model)


# ── A sum of money is a number ───────────────────────────────────────────────


def test_grouped_money_is_paired_with_its_label_and_totalled():
    from decimal import Decimal

    computed = deterministic_summary(["Invoice_Status__c", "total_unpaid"], [["Not Paid", Decimal("100.50")], ["Paid", Decimal("0.00")]])
    assert "value_counts" not in computed
    assert computed["row_breakdown"] == {"Not Paid": Decimal("100.50"), "Paid": Decimal("0.00")}
    assert computed["numeric_totals"]["total_unpaid"]["sum"] == 100.5


def test_a_sum_of_money_of_zero_is_a_figure_of_nothing():
    from decimal import Decimal

    computed = deterministic_summary(["total_unpaid"], [[Decimal("0.00")]], aggregated=True)
    assert computed["aggregate_result"] == {"total_unpaid": 0.0}
    assert computed["zero_figure"] is True


def test_a_sum_of_money_is_no_record_count_and_a_count_still_is():
    from decimal import Decimal

    assert "record_count" not in deterministic_summary(["total"], [[Decimal("1234.50")]], aggregated=True)
    assert deterministic_summary(["total"], [[57]], aggregated=True)["record_count"] == 57


def test_money_on_the_typed_view_reaches_the_figures(warehouse, monkeypatch):
    statement = "SELECT Invoice_Status__c, SUM(Outstanding_Amount__c) AS total_unpaid FROM Invoice__c WHERE Invoice_Status__c IS NOT NULL GROUP BY 1"
    model = Model(monkeypatch, statement, statement)
    ask("how much is unpaid by status?")
    figures = _figures(model)
    assert "row_breakdown" in figures and "numeric_totals" in figures
    assert "value_counts" not in figures


# ── A figure of any aggregate is no record ───────────────────────────────────


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT count_if(Interview_Outcome__c = 'Ghosted') AS ghosted FROM Interview__c",
        "SELECT median(Scheduled_Minutes__c) AS middle FROM Interview__c",
        "SELECT mode(Round__c) AS commonest FROM Interview__c",
        "SELECT quantile_cont(Scheduled_Minutes__c, 0.9) AS p90 FROM Interview__c",
        "SELECT stddev(Scheduled_Minutes__c) AS spread FROM Interview__c",
    ],
)
def test_one_row_of_any_aggregate_is_no_record_count(warehouse, monkeypatch, statement):
    Model(monkeypatch, statement)
    meta = ask("what is the figure?")[1]
    assert "record_count" not in meta["salesforce_sources"]
    assert meta["assumptions"] == ["No date filter was applied, so this covers all dates."]


def test_a_one_record_lookup_is_still_one_record(warehouse, monkeypatch):
    Model(monkeypatch, "SELECT Scheduled_Minutes__c FROM Interview__c WHERE Name = 'INT-1'")
    assert ask("how long is INT-1?")[1]["salesforce_sources"]["record_count"] == 1
    Model(monkeypatch, "SELECT Round__c FROM Interview__c WHERE Name = 'INT-1'")
    assert ask("which round is INT-1?")[1]["salesforce_sources"]["record_count"] == 1
    assert not sql_engine.is_aggregate("SELECT list_contains(x, 1) FROM t")


# ── A stream reads as the whole text does ────────────────────────────────────


def _streamed(tokens):
    async def stream():
        for token in tokens:
            yield token

    async def collect():
        return "".join([part async for part in sql_engine._without_block_wording(stream())])

    return asyncio.run(collect())


def _held(tokens):
    hold = sql_engine.WordingHold()
    return "".join(hold.feed(token) for token in tokens) + hold.flush()


def _sizes(text):
    return [[text[i : i + size] for i in range(0, len(text), size)] for size in (1, 2, 3, 4, 6, 8, 12, 20)]


#: Stored token boundaries, as the model really sent them.
STORED_TOKENS = [
    ["Based on the", " computed", " figures", ",", " there", " were", " **70**", " interviews."],
    ["Based on the", " computed", " figures", " for", " all-time", " interviews,", " 5."],
    ["**Summary Statistics (from", " Com", "puted", " Figures", "):**", "\n* 5"],
    ["We", " counted", " 5,", " based on the", " computed", " figures", ",", " so", " that", " stands."],
    ["Based on the", " live", " Salesforce", " check", "s", ",", " there", " are", " 4."],
]


@pytest.mark.parametrize("tokens", STORED_TOKENS)
def test_a_phrase_split_across_tokens_comes_out_as_the_whole_text_does(tokens):
    whole = _streamed(["".join(tokens)])
    assert _streamed(tokens) == whole
    for split in _sizes("".join(tokens)):
        assert _streamed(split) == whole
    assert "computed" not in whole.lower()


@pytest.mark.parametrize(
    "tokens, expected",
    [
        (STORED_TOKENS[0], "There were **70** interviews."),
        (STORED_TOKENS[1], "For all-time interviews, 5."),
        (STORED_TOKENS[2], "**Summary Statistics:**\n* 5"),
    ],
)
def test_an_opening_clause_goes_whole_and_leaves_a_capital(tokens, expected):
    assert _streamed(tokens) == expected
    assert _held(tokens) == expected


def test_the_agent_route_reads_a_split_phrase_the_same(warehouse, monkeypatch):
    AgentModel(monkeypatch, [sql_step()], OFFERS, synthesis=tuple(STORED_TOKENS[0]))
    assert ask_agent("how many ghosted?")[0].startswith("There were **70** interviews.")


# ── Only a claim of a live check is reworded ─────────────────────────────────

ABOUT_THE_LIVE_DATA = [
    "I do not have access to live Salesforce data, so this comes from the synced copy.",
    "This is not based on a live Salesforce query.",
    "A live Salesforce check is not available here.",
    "You asked for a direct Salesforce check, but this comes from the copy.",
    "I cannot perform a direct Salesforce check.",
    "These figures may differ from live Salesforce data until the next sync.",
    "It can lag behind live Salesforce data by up to 30 minutes.",
    "This may not reflect the latest live Salesforce data.",
    "To confirm, run a live Salesforce query yourself.",
    "This comes from the local copy rather than a live Salesforce query, and the copy was refreshed about twenty minutes ago, so it should be close.",
    "It is not a direct Salesforce check, because this reply reads the synced copy that is refreshed about every thirty minutes.",
    "Instead of a direct Salesforce check, this reads the synced copy that is refreshed about every thirty minutes.",
    "This did not come directly from Salesforce, and it reflects the copy from about thirty minutes ago at most.",
    "These are the counts from the copy, not live Salesforce data, so they are about thirty minutes behind at most.",
    "I don't have direct access to your live Salesforce data.",
    "x " * 40 + "rather than a live Salesforce query was used, as expected here.",
]


@pytest.mark.parametrize("sentence", ABOUT_THE_LIVE_DATA)
def test_a_sentence_about_the_live_data_is_left_alone_however_it_arrives(sentence):
    assert _held([sentence]) == sentence
    for split in _sizes(sentence):
        assert _held(split) == sentence


@pytest.mark.parametrize(
    "said, expected",
    [
        ("Based on the direct Salesforce check, there are 12 open cases.", "Based on the synced Salesforce copy, there are 12 open cases."),
        ("Based on the direct Salesforce check, there are no open cases.", "Based on the synced Salesforce copy, there are no open cases."),
        ("A live Salesforce query shows 4.", "The synced Salesforce copy shows 4."),
        ("The live Salesforce check confirms that 4 are open.", "The synced Salesforce copy confirms that 4 are open."),
        ("The figure comes directly from Salesforce: 4.", "The figure comes from the synced Salesforce copy: 4."),
        ("According to live Salesforce data, 4 are open.", "According to the synced Salesforce copy, 4 are open."),
        ("The figure comes from a live Salesforce query.", "The figure comes from the synced Salesforce copy."),
    ],
)
def test_a_claim_of_a_live_check_is_reworded_however_it_arrives(said, expected):
    assert _held([said]) == expected
    for split in _sizes(said):
        assert _held(split) == expected


def test_a_denial_said_before_the_hold_counts(warehouse, monkeypatch):
    """The stream has already let the "not" go by the time the claim comes along."""
    sentence = "This answer, and I am being careful here as this took a while to work out, is not based on the live Salesforce data."
    assert _held(_sizes(sentence)[0]) == sentence


def test_text_with_no_mention_of_salesforce_still_flows():
    hold = sql_engine.WordingHold()
    assert hold.feed("A long sentence with nothing in it to hold back at all, and it runs on and on and on") != ""
