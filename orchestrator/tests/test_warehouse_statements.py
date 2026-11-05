"""What a statement may read, and what it may do with a withheld column.

A second review of the warehouse route found every text rule one spelling away from a
real credential or a wrong number: `warehouse.T`, `FROM 'T'`, `"query_table"('T')`,
`histogram_values('T', col)`, `AS "someone's code"`, `$$'$$`, `GROUP BY 1`. So what is
decided here is decided on DuckDB's own parser tree (core/sql_tree.py), and these tests
pin the labels it reads as well as what the reader gets.

Real DuckDB views, a fake writer, no model. Every name and value below is invented, and
every withheld value starts with ZX- so a leak is one search away.
"""
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import date, time as clock_time, timedelta
from decimal import Decimal
from uuid import UUID

import duckdb
import pytest

from app.config import settings
from app.core import schema_cache as schema_cache_module, sql_tree, warehouse as warehouse_reader
from app.core.schema_cache import schema_cache
from app.core.sf_intel import tools as sf_tools
from app.core.sql_guard import SQLGuardError, guard_sql, mask_literals, outside_literals
from app.engines import sql as sql_engine
from app.engines.sql import CREDENTIAL_LINE, _behind_the_mask, _hold_back, _tables_named
from tests.test_warehouse_answers import _create, _text, Model, Rec, ask

SECRET = "ZX-"
COULD_NOT_FIND = "I could not find that in the synced Salesforce copy"
COULD_NOT_WRITE = "I could not write a valid query"
#: Passes the check that a known table is read, and reads nothing withheld.
KNOWN = " WHERE EXISTS (SELECT 1 FROM Task)"


def _fill(path: str) -> None:
    con = duckdb.connect(path)
    con.execute("CREATE SCHEMA raw")
    _create(
        con,
        "Session__c",
        _text("Id", "Name", "External_Meeting_Passcode__c"),
        [(f"a2SXx00000{i:02d}AAAIA{i}", f"S-{i}", f"ZX-10{i}" if i < 6 else None) for i in range(8)],
    )
    _create(
        con,
        "Onboarding__c",
        _text("Id", "Name", "Passport_Number__c", "Account__c"),
        [
            *((f"a3OXx00000{i:02d}AAAIA{i}", f"O-{i}", f"ZX-20{i}1" if i % 2 else f"ZX-20{i}a", "001Xx0000001AAAIA1") for i in range(7)),
            ("a3OXx0000007AAAIA7", "O-7", "ZX-2061", "001Xx0000001AAAIA1"),
            ("a3OXx0000008AAAIA8", "O-8", None, None),
            ("a3OXx0000009AAAIA9", "O-9", "", None),
        ],
    )
    _create(
        con,
        "Account",
        _text("Id", "Name", "Passport_Number__c", "OwnerId"),
        [("001Xx0000001AAAIA1", "Northwind", "ZX-2011", "005Xx0000001AAAIA1"), ("001Xx0000002AAAIA2", "Contoso", None, None)],
    )
    _create(con, "Task", _text("Id", "Subject"), [("00TXx0000001AAAIA1", "Call"), ("00TXx0000002AAAIA2", "Mail")])
    _create(
        con,
        "Recruiter__c",
        {**_text("Id", "Name", "Email__c", "Status__c"), "Score__c": "BIGINT"},
        [
            ("a0RXx0000001AAAIA1", "Asha Varnok", "asha@example.test", "Active", "3"),
            ("a0RXx0000002AAAIA2", "Rovik Mendrell", "rovik@example.test", "Active", "4"),
        ],
    )
    # A lookup column can be called just what a withheld table is called.
    _create(con, "Log__c", _text("Id", "Session__c"), [("a4LXx0000001AAAIA1", "a2SXx0000000AAAIA0"), ("a4LXx0000002AAAIA2", "a2SXx0000000AAAIA0")])
    # Secrets inside text that is not held: a link's parameter, a label in free text, one in a
    # column whose name says nothing, one inside JSON. Each value starts ZX- as well.
    _create(
        con,
        "Meeting__c",
        _text("Id", "Name", "Meeting_Link__c", "Notes__c", "Work_History__c", "Reply_Json__c"),
        [
            (
                "a5MXx0000001AAAIA1",
                "M-1",
                "https://meet.example.test/j/1?pwd=ZX-301&x=1",
                "passcode: ZX-311",
                "token=ZX-321 then more",
                '{"start_url":"https://meet.example.test/s/1?zak=ZX-331"}',
            ),
            ("a5MXx0000002AAAIA2", "M-2", "https://meet.example.test/j/2?pwd=ZX-302", "no secret here", "clean", "{}"),
            ("a5MXx0000003AAAIA3", "M-3", None, None, None, None),
        ],
    )
    con.execute("CHECKPOINT")
    con.close()


@pytest.fixture(params=["direct", "cached"])
def wh(request, tmp_path, monkeypatch):
    """Both ways a statement reaches the file: opened per call, and the cached snapshot."""
    path = str(tmp_path / "warehouse.duckdb")
    _fill(path)
    if request.param == "cached":
        snapshot = str(tmp_path / "warehouse.read.duckdb")
        shutil.copyfile(path, snapshot)
        monkeypatch.setattr(settings, "_duckdb_live_path", path)
        monkeypatch.setattr(settings, "duckdb_snapshot_path", snapshot)
        monkeypatch.setattr(settings, "_prefer_snapshot", True)
        monkeypatch.delenv("WAREHOUSE_READER_CACHE", raising=False)
        warehouse_reader.reset()
    else:
        monkeypatch.setattr(settings, "duckdb_path", path)
    schema_cache.invalidate()
    sql_engine._stored_values_cache.clear()
    yield path
    warehouse_reader.reset()
    schema_cache.invalidate()
    sql_engine._stored_values_cache.clear()


@pytest.fixture()
def one(tmp_path, monkeypatch):
    """The open-per-call path alone, for what does not depend on how the file was opened."""
    path = str(tmp_path / "warehouse.duckdb")
    _fill(path)
    monkeypatch.setattr(settings, "duckdb_path", path)
    schema_cache.invalidate()
    sql_engine._stored_values_cache.clear()
    yield path
    schema_cache.invalidate()
    sql_engine._stored_values_cache.clear()


def leaks(model, meta, text) -> bool:
    prompts = json.dumps(model.narrative_prompts, default=str)
    return SECRET in json.dumps(meta, default=str) or SECRET in prompts or SECRET in text


def _steered(monkeypatch, statement):
    """A writer that keeps writing the statement it was steered to."""
    return Model(monkeypatch, statement, statement, statement)


# ── Every way of naming a table, and of reading one past the shadow ──────────


PASSCODE = "External_Meeting_Passcode__c"
PAST_THE_SHADOW = [
    f"SELECT Name, {PASSCODE} AS meeting_code FROM warehouse.Session__c",
    f'SELECT Name, {PASSCODE} AS meeting_code FROM "warehouse"."Session__c"',
    f"SELECT Name, {PASSCODE} AS meeting_code FROM WAREHOUSE.Session__c",
    f"SELECT Name, {PASSCODE} AS meeting_code FROM warehouse . Session__c",
    f"SELECT Name, {PASSCODE} AS meeting_code FROM warehouse.main.Session__c",
    f"SELECT Name, {PASSCODE} AS meeting_code FROM main.Session__c",
    f"SELECT Name, {PASSCODE} AS meeting_code FROM raw.Session__c",
    f"SELECT x.meeting_code FROM (SELECT {PASSCODE} AS meeting_code FROM warehouse.Session__c) x",
    f"SELECT s.{PASSCODE} AS meeting_code FROM Task t, warehouse.Session__c s",
    f"SELECT s.{PASSCODE} AS meeting_code FROM Task t JOIN warehouse.Session__c s ON 1 = 1",
    # Spaces DuckDB strips and Python's \s does not match.
    f"SELECT Name, {PASSCODE} AS meeting_code FROM main​.Session__c{KNOWN}",
    f"SELECT Name, {PASSCODE} AS meeting_code FROM main⁠.Session__c{KNOWN}",
    f"SELECT Name, {PASSCODE} AS meeting_code FROM main﻿.Session__c{KNOWN}",
    f"SELECT Name, {PASSCODE} AS meeting_code FROM raw​.Session__c{KNOWN}",
    f"SELECT Name, {PASSCODE} AS meeting_code FROM warehouse.main​.Session__c{KNOWN}",
    # A string is a table name to DuckDB.
    f"SELECT Name, {PASSCODE} AS meeting_code FROM 'Session__c'{KNOWN}",
    f"SELECT Name, {PASSCODE} AS meeting_code FROM 'Sess'\n'ion__c'{KNOWN}",
    f"WITH x AS (FROM 'Session__c') SELECT {PASSCODE} AS meeting_code FROM x{KNOWN}",
    f"SELECT {PASSCODE} AS meeting_code FROM (TABLE 'Session__c'){KNOWN}",
    f"SELECT * FROM (SELECT {PASSCODE} AS a FROM 'Session__c') UNPIVOT (v FOR k IN (a)){KNOWN}",
    # A function reads what a string names.
    f"SELECT Name, {PASSCODE} AS meeting_code FROM \"query_table\"('Session__c'){KNOWN}",
    f"SELECT Name, {PASSCODE} AS meeting_code FROM \"QUERY_TABLE\"('Session__c'){KNOWN}",
    f"SELECT Name, {PASSCODE} AS meeting_code FROM \"system\".\"main\".\"query_table\"('Session__c'){KNOWN}",
    f"SELECT Name, {PASSCODE} AS meeting_code FROM `query_table`('Session__c'){KNOWN}",
    f"SELECT meeting_code FROM \"query\"('SELECT {PASSCODE} AS meeting_code FROM Session__c'){KNOWN}",
    f"SELECT Name, {PASSCODE} AS meeting_code FROM \"query_table\"('Session__c') /* FROM Task */",
    f"SELECT bin AS meeting_code, count FROM histogram_values('Session__c', {PASSCODE}, bin_count := 1000, technique := 'sample'){KNOWN}",
    f"SELECT bin AS meeting_code FROM histogram_values('Sess' || 'ion__c', {PASSCODE}, bin_count := 1000, technique := 'sample'){KNOWN}",
    f"SELECT bin AS note, count FROM histogram('Session__c', {PASSCODE}){KNOWN}",
    f"SELECT stats AS note FROM pragma_storage_info('raw.Session__c') WHERE column_name = '{PASSCODE}' AND EXISTS (SELECT 1 FROM Task)",
    f"SELECT * FROM json_execute_serialized_sql(json_serialize_sql('SELECT {PASSCODE} AS note FROM warehouse.main.Session__c')){KNOWN}",
    f"SELECT min, max FROM (SUMMARIZE 'Session__c'){KNOWN}",
    f"SELECT * FROM (PIVOT 'Session__c' ON Id USING first({PASSCODE}) GROUP BY Name){KNOWN}",
    f"SELECT unnest((SELECT list({PASSCODE}) FROM 'Session__c')) AS note{KNOWN}",
    # An apostrophe, a dollar quote or an E-string that the text rules read one way and DuckDB another.
    f"SELECT s.Name, s.{PASSCODE} AS \"Session's code\" FROM Session__c s WHERE s.Name <> 'x' AND EXISTS (SELECT 1 FROM Task)",
    f"SELECT s.Name, s.{PASSCODE} AS \"Session's code\" FROM main.Session__c s WHERE s.Name <> 'x'",
    f"SELECT s.Name, s.{PASSCODE} AS \"Session's code\" FROM \"main\".\"Session__c\" s WHERE s.Name <> 'x'",
    f"SELECT $$'$$ AS a, Name, {PASSCODE} AS meeting_code FROM Session__c WHERE Name <> 'y' AND EXISTS (SELECT 1 FROM Task)",
    f"SELECT E'\\'' AS a, Name, {PASSCODE} AS meeting_code FROM Session__c WHERE Name <> 'y' AND EXISTS (SELECT 1 FROM Task)",
    f"SELECT 1 AS \"'\", s.{PASSCODE} AS meeting_code FROM Session__c s JOIN (SELECT 1 AS \"'\") q ON 1 = 1 WHERE s.Id NOT IN (SELECT Id FROM Task)",
    f"SELECT 1 AS a$$b, q.{PASSCODE} AS meeting_code FROM query_table('Session__c') q, (SELECT 1 AS c$$d) z WHERE q.Id NOT IN (SELECT Id FROM Task)",
    f"SELECT 'éé' AS z, $$'$$ AS a, Name, {PASSCODE} AS meeting_code FROM Session__c WHERE Name <> 'y' AND EXISTS (SELECT 1 FROM Task)",
    f"SELECT $$'$$ AS a, Name, {PASSCODE} AS meeting_code FROM Session__c WHERE Name <> 'y' AND EXISTS (SELECT 1 FROM Task)",
    f"SELECT $$'$$ AS a, Name, {PASSCODE} AS meeting_code FROM query_table('Sess' || 'ion__c') WHERE EXISTS (SELECT 1 FROM Task)",
    # A second statement after a quote only DuckDB reads right.
    f"SELECT 1 FROM Task WHERE $$'$$ = 'x'; SELECT Name, {PASSCODE} AS meeting_code FROM Session__c WHERE Name <> 'y'",
    f"SELECT 1 FROM Task WHERE E'\\'' = 'x'; SELECT Name, {PASSCODE} AS meeting_code FROM Session__c WHERE Name <> 'y'",
    f"SELECT 1 AS x FROM Task LIMIT length($$'$$); SELECT Name, {PASSCODE} AS meeting_code FROM Session__c --'",
]


@pytest.mark.parametrize("statement", PAST_THE_SHADOW)
def test_no_spelling_reads_past_the_shadow(wh, monkeypatch, statement):
    model = _steered(monkeypatch, statement)
    text, meta = ask("show the codes for each session")
    assert not leaks(model, meta, text)


def test_the_attached_file_reads_past_nothing(wh, monkeypatch):
    con = sql_engine._connect_warehouse(duckdb)
    try:
        path = con.execute("SELECT path FROM duckdb_databases() WHERE NOT internal").fetchall()[0][0]
    finally:
        con.close()
    statement = f"SELECT Name, {PASSCODE} AS meeting_code FROM read_duckdb('{path}', table_name := 'Session__c'){KNOWN}"
    model = _steered(monkeypatch, statement)
    text, meta = ask("show the codes for each session")
    assert not leaks(model, meta, text)


def test_the_path_of_the_file_never_reaches_a_reader(wh, monkeypatch):
    model = _steered(monkeypatch, "SELECT d.path, t.Subject FROM duckdb_databases d, Task t")
    text, meta = ask("show the subjects")
    assert os.path.basename(wh).split(".")[0] not in json.dumps([text, meta.get("data"), model.narrative_prompts], default=str)


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT COUNT(*) AS n FROM warehouse.Session__c",
        "SELECT COUNT(*) AS n FROM warehouse.main.Session__c",
        "SELECT COUNT(*) AS n FROM main.Session__c",
        'SELECT COUNT(*) AS n FROM "warehouse"."Session__c"',
        "SELECT COUNT(*) AS n FROM Session__c",
    ],
)
def test_an_honest_qualifier_still_answers(wh, monkeypatch, statement):
    model = Model(monkeypatch, statement)
    _text_answer, meta = ask("how many sessions are there?")
    assert meta["data"] == [{"n": 8}]
    assert len(model.writer_prompts) == 1


def test_a_lookup_named_like_a_withheld_table_is_no_table(one, monkeypatch):
    """The reason no regex takes "any qualifier": x.Session__c is a column here."""
    Model(monkeypatch, "SELECT COUNT(*) AS n FROM Log__c x JOIN Log__c y ON x.Session__c = y.Session__c")
    _text_answer, meta = ask("how many log pairs share a session?")
    assert meta["data"] == [{"n": 4}]


def test_an_unknown_qualifier_is_refused_with_what_to_do(one, monkeypatch):
    model = Model(monkeypatch, f"SELECT {PASSCODE} AS a FROM elsewhere.Session__c", "SELECT COUNT(*) AS n FROM Session__c")
    _text_answer, meta = ask("how many sessions are there?")
    assert "bare name" in model.writer_user(1)
    assert meta["data"] == [{"n": 8}]


@pytest.mark.parametrize(
    "statement, refused",
    [
        ("SELECT * FROM query_table('x')", True),
        ('SELECT * FROM "query"(\'SELECT 1\')', True),
        ("SELECT * FROM (SUMMARIZE t)", True),
        ("SELECT * FROM (DESCRIBE t)", True),
        ("SELECT * FROM t PIVOT (sum(a) FOR b IN ('x'))", True),
        ("SELECT 1; SELECT 2", True),
        ("SELECT * FROM t USING SAMPLE 5", True),
        ("SELECT * FROM t TABLESAMPLE 10%", True),
        ("SELECT * FROM duckdb_databases", True),
        ("SELECT * FROM elsewhere.t", True),
        ("SELECT * FROM raw.t", True),
        ("SELECT * FROM warehouse.raw.t", True),
        ("SELECT current_setting('x')", True),
        ("SELECT pg_get_viewdef(1)", True),
        ("SELECT version()", True),
        ("SELECT encode(a) FROM t", True),
        ("SELECT hex(a) FROM t", True),
        ("SELECT a.to_base64() FROM t", True),
        ("SELECT CAST(a AS BLOB) FROM t", True),
        ("SELECT CAST(CAST([a] AS BLOB[]) AS BIT[]) FROM t", True),
        ("SELECT current_date()", False),
        ("SELECT current_localtimestamp()", False),
        ("SELECT * FROM t", False),
        ("SELECT * FROM main.t", False),
        ("SELECT * FROM warehouse.main.t", False),
        ("SELECT * FROM unnest([1, 2]) u(x), range(3), generate_series(1, 2)", False),
        ("SELECT * FROM (VALUES (1), (2)) v(a)", False),
    ],
)
def test_the_tree_labels_this_engine_reads_are_still_there(statement, refused):
    """That JSON is DuckDB's internal format. If a new version renamed these labels, the
    walker would silently pass everything: this fails instead."""
    tree = sql_tree.read(statement)
    assert tree is not None
    if refused:
        with pytest.raises(sql_tree.StatementRefused):
            sql_tree.refuse_unseen_reads(tree)
    else:
        sql_tree.refuse_unseen_reads(tree)


def test_a_statement_that_cannot_be_read_is_refused_and_a_typo_is_left_to_duckdb():
    assert sql_tree.read("SELEC 1") is None, "a parser error stays DuckDB's to word, for the retry"
    with pytest.raises(sql_tree.StatementRefused):
        sql_tree.read("SELECT * FROM (PIVOT 't' ON a USING first(b))")


def test_a_typo_keeps_duckdbs_own_words_for_the_retry(one, monkeypatch):
    model = Model(monkeypatch, "SELECT Name FROM Task WHRE Subject = 'Call'", "SELECT COUNT(*) AS n FROM Task")
    _text_answer, meta = ask("how many tasks are there?")
    assert "syntax error" in model.writer_user(1)
    assert meta["data"] == [{"n": 2}]


# ── The gate reads the statement as DuckDB will, comments gone ───────────────


@pytest.mark.parametrize(
    "reply",
    [
        "SELECT 0 AS record_count /* FROM Task */",
        "SELECT 0 AS record_count -- FROM Task",
        "/* FROM Task */ SELECT 0 AS record_count",
    ],
)
def test_a_table_named_only_in_a_comment_reads_nothing(one, monkeypatch, reply):
    Model(monkeypatch, reply)
    text, meta = ask("how many courses do we have?")
    assert COULD_NOT_FIND in text
    assert "data" not in meta


def test_a_prose_reply_stays_could_not_find_not_a_refusal(one, monkeypatch):
    model = Model(monkeypatch, "Sorry, nothing in this schema holds that.")
    text, meta = ask("how many courses do we have?")
    assert COULD_NOT_FIND in text
    assert "salesforce_error" not in meta
    assert len(model.writer_prompts) == 1


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT COUNT(*) AS \"Recruiter's count\" FROM Recruiter__c WHERE Status__c = 'Active'",
        "-- Recruiter's count\nSELECT COUNT(*) AS n FROM Recruiter__c WHERE Status__c = 'Active'",
        "/* Recruiter's count */ SELECT COUNT(*) AS n FROM Recruiter__c WHERE Status__c = 'Active'",
    ],
)
def test_an_apostrophe_outside_a_string_opens_no_string(one, monkeypatch, statement):
    Model(monkeypatch, statement)
    text, meta = ask("how many active recruiters are there?")
    assert COULD_NOT_FIND not in text
    assert list(meta["data"][0].values()) == [2]
    assert "Scope: Recruiter__c, where Status__c = 'Active'." in text


def test_an_apostrophe_cannot_move_the_scope_line_to_another_table(one, monkeypatch):
    statement = (
        "SELECT COUNT(*) AS \"Recruiter's count\" FROM Recruiter__c i "
        "WHERE i.Status__c = 'Active' AND i.Id IN (SELECT Id FROM Task)"
    )
    Model(monkeypatch, statement)
    text, _meta = ask("how many active recruiters are there?")
    assert "Scope: Recruiter__c, also reading Task" in text
    assert "every row of Task" not in text


@pytest.mark.parametrize(
    "sql, masked",
    [
        ("SELECT 'a' FROM t", "SELECT '_' FROM t"),
        ("SELECT 'it''s' FROM t", "SELECT '_____' FROM t"),
        ("SELECT 1 AS \"a's\" FROM t WHERE x = 'y'", "SELECT 1 AS \"___\" FROM t WHERE x = '_'"),
        ('SELECT "Plain" FROM t', 'SELECT "Plain" FROM t'),
        ("SELECT 1 -- it's\nFROM t WHERE x = 'y'", "SELECT 1 " + " " * 7 + "\nFROM t WHERE x = '_'"),
        ("SELECT /* it's */ 1 FROM t", "SELECT " + " " * 10 + " 1 FROM t"),
        ("SELECT DATE'2026-01-01' FROM t", "SELECT DATE'__________' FROM t"),
        ("SELECT $$it's$$ FROM t WHERE x = 'y'", "SELECT '______' FROM t WHERE x = '_'"),
        ("SELECT E'it\\'s' FROM t WHERE x = 'y'", "SELECT '______' FROM t WHERE x = '_'"),
    ],
)
def test_the_mask_reads_quotes_as_duckdb_does(sql, masked):
    assert mask_literals(sql) == masked
    assert len(mask_literals(sql)) == len(sql)


def test_a_rewrite_never_reaches_inside_a_string():
    said = outside_literals("SELECT main.T, 'main.T' FROM main.T -- main.T", lambda part: part.replace("main.", ""))
    assert said == "SELECT T, 'main.T' FROM T -- T"
    assert outside_literals("SELECT $$main.T$$, main.T", lambda part: part.replace("main.", "")) == "SELECT $$main.T$$, T"


# ── One statement, plain strings ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT $$x$$",
        "SELECT $tag$x$tag$",
        "SELECT 1 AS a$$b",
        "SELECT $1",
        "SELECT E'x'",
        "SELECT e'x'",
        "SELECT 1 FROM Task WHERE $$'$$ = 'x'; CREATE TEMP TABLE zz AS SELECT 1",
        "SELECT 1 FROM Task WHERE $$'$$ = 'x'; ATTACH ':memory:' AS zzdb",
    ],
)
def test_quotes_the_text_rules_cannot_read_are_refused(statement):
    with pytest.raises(SQLGuardError):
        guard_sql(statement)


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT DATE'2026-01-01'",
        "SELECT x'41'",
        "SELECT 'it''s'",
        "SELECT '$$ ''E'' é'",
        "SELECT \"Price$\" FROM t",
        "SELECT 1 AS \"Recruiter's count\" FROM t",
    ],
)
def test_ordinary_quoting_still_passes(statement):
    assert guard_sql(statement)


def test_a_new_statement_after_a_quote_changes_nothing_in_the_file(wh, monkeypatch):
    statement = "SELECT 1 FROM Task WHERE $$'$$ = 'x'; CREATE TEMP TABLE zz AS SELECT 1"
    model = Model(monkeypatch, statement, "SELECT COUNT(*) AS n FROM Task")
    _text_answer, meta = ask("how many tasks are there?")
    assert "plain 'string'" in model.writer_user(1)
    assert meta["data"] == [{"n": 2}]
    with pytest.raises(duckdb.Error):
        sql_engine._execute("SELECT * FROM zz", 2)


# ── A withheld column is shown or tested for presence, and nothing else ──────


DOC = "Passport_Number__c"
#: Every one of these counted the marker: a figure with no warning on it, and wrong.
COUNTS_THE_MARKER = [
    f"SELECT COUNT(*) AS n FROM (SELECT {DOC} FROM Onboarding__c WHERE {DOC} IS NOT NULL GROUP BY 1 HAVING COUNT(*) > 1)",
    f"SELECT COUNT(*) AS n FROM (SELECT {DOC} FROM Onboarding__c WHERE {DOC} IS NOT NULL GROUP BY ALL HAVING COUNT(*) > 1)",
    f"SELECT approx_count_distinct({DOC}) AS n FROM Onboarding__c",
    f"SELECT len(list_distinct(list({DOC}))) AS n FROM Onboarding__c",
    f"SELECT COUNT(*) AS n FROM (SELECT {DOC} FROM Onboarding__c UNION SELECT {DOC} FROM Account)",
    f"SELECT COUNT(*) AS n FROM (SELECT {DOC} FROM Onboarding__c INTERSECT SELECT {DOC} FROM Account)",
    f"SELECT COUNT(*) AS n FROM (SELECT {DOC} FROM Onboarding__c EXCEPT SELECT {DOC} FROM Account)",
    f"SELECT MAX(c) AS n FROM (SELECT COUNT(*) OVER (PARTITION BY {DOC}) AS c FROM Onboarding__c)",
    f"SELECT Name FROM Onboarding__c QUALIFY ROW_NUMBER() OVER (PARTITION BY {DOC} ORDER BY Id) > 1",
    f"SELECT SUM(CASE WHEN {DOC} LIKE '%a%' THEN 1 ELSE 0 END) AS n FROM Onboarding__c",
    f"SELECT COUNT_IF({DOC} LIKE '%a%') AS n FROM Onboarding__c",
    f"SELECT AVG(LENGTH({DOC})) AS n FROM Onboarding__c",
    f"SELECT CASE WHEN {DOC} LIKE '%a%' THEN 'A' ELSE 'B' END AS kind, COUNT(*) AS n FROM Onboarding__c GROUP BY 1",
    f"SELECT {DOC} AS k, COUNT(*) AS n FROM Onboarding__c WHERE {DOC} IS NOT NULL GROUP BY 1",
    f"SELECT {DOC} AS k, COUNT(*) AS n FROM Onboarding__c GROUP BY k HAVING COUNT(*) > 1",
    f"SELECT {DOC} AS k, LENGTH(k) AS l FROM Onboarding__c",
    f"WITH t AS (SELECT {DOC} AS k FROM Onboarding__c) SELECT COUNT(DISTINCT k) AS n FROM t",
    f"WITH t(k) AS (SELECT {DOC} FROM Onboarding__c) SELECT COUNT(DISTINCT k) AS n FROM t",
    f"SELECT COUNT(*) AS n FROM (SELECT {DOC} AS p FROM Onboarding__c) a JOIN (SELECT {DOC} AS p FROM Account) b ON a.p = b.p",
    f"SELECT COUNT(*) AS n FROM (SELECT {DOC} FROM Onboarding__c) NATURAL JOIN (SELECT {DOC} FROM Account)",
    f"SELECT COUNT(*) AS n FROM (SELECT {DOC} AS k FROM Onboarding__c) a WHERE a.k LIKE '%a%'",
    f"SELECT COUNT(DISTINCT m) AS n FROM (SELECT {DOC} FROM Onboarding__c) AS r(m)",
    f"SELECT COUNT(DISTINCT m) AS n FROM (SELECT * FROM Onboarding__c) AS r(a, b, m)",
    f"SELECT COUNT(DISTINCT COLUMNS('Passport_Number.*')) AS n FROM Onboarding__c",
    f"SELECT AVG(LENGTH(COLUMNS('Passport_Number.*'))) AS n FROM Onboarding__c",
    f"SELECT Name FROM Onboarding__c ORDER BY {DOC} LIMIT 3",
    f"SELECT {DOC} AS k, Name FROM Onboarding__c ORDER BY 1 LIMIT 3",
    f"SELECT {DOC} AS k, Name FROM Onboarding__c ORDER BY ALL LIMIT 3",
    f"SELECT DISTINCT ON ({DOC}) Name FROM Onboarding__c",
    "SELECT DISTINCT * FROM Onboarding__c",
    "SELECT COUNT(*) AS n FROM (SELECT DISTINCT * FROM Onboarding__c)",
    f"SELECT COUNT(*) AS n FROM Task t WHERE t.Id IN (SELECT {DOC} FROM Onboarding__c)",
    f"SELECT (SELECT {DOC} FROM Onboarding__c LIMIT 1) AS n FROM Task",
    f"SELECT COUNT(DISTINCT t) AS n FROM Onboarding__c t",
    f"SELECT COUNT(*) AS n FROM Onboarding__c t WHERE t['{DOC}'] LIKE '%1%'",
    f"SELECT COUNT(*) AS n FROM Onboarding__c t WHERE struct_extract(t, '{DOC}') LIKE '%1%'",
    f"SELECT SUM(CASE WHEN t['{DOC}'] LIKE '%1%' THEN 1 ELSE 0 END) AS n FROM Onboarding__c t",
    f"SELECT * RENAME ({DOC} AS k) FROM Onboarding__c",
    f"SELECT * REPLACE ({DOC} || 'x' AS {DOC}) FROM Onboarding__c",
    # Pieces of the marker, which read as values: "eld]", "[", "withheld]".
    f"SELECT Name, RIGHT({DOC}, 4) AS last_four FROM Onboarding__c WHERE {DOC} IS NOT NULL LIMIT 5",
    f"SELECT LEFT({DOC}, 1) AS kind, COUNT(*) AS n FROM Onboarding__c GROUP BY 1",
    f"SELECT substr({DOC}, 2) AS tail FROM Onboarding__c",
    f"SELECT {DOC}[-4:] AS tail FROM Onboarding__c",
    f"SELECT histogram({DOC}) AS note FROM Onboarding__c",
    f"SELECT upper({DOC}) AS note FROM Onboarding__c",
    f"SELECT Name || {DOC} AS note FROM Onboarding__c",
    f"SELECT {DOC} LIKE 'Z%' AS flag, COUNT(*) AS n FROM Onboarding__c GROUP BY 1",
    # By place, which names nothing. Passport_Number__c is column 3.
    "SELECT COUNT(DISTINCT #3) AS n FROM Onboarding__c",
    "SELECT COUNT(*) AS n FROM Onboarding__c GROUP BY #3",
    "SELECT Name, substr(#3,2,4) AS code FROM Onboarding__c",
    # The parser writes that same node for ARRAY(SELECT ...), with no # in the text.
    f"SELECT len(list_distinct(ARRAY(SELECT {DOC} FROM Onboarding__c))) AS n",
    # A relation named like a real column, and the names DuckDB gives one with no alias.
    f"SELECT COUNT(DISTINCT Name) AS n FROM (SELECT {PASSCODE} FROM Session__c) AS Name",
    f"SELECT COUNT(DISTINCT Id) AS n FROM (SELECT {PASSCODE} FROM Session__c) AS Id",
    f"WITH c AS (SELECT {PASSCODE} FROM Session__c) SELECT COUNT(DISTINCT Name) AS n FROM c AS Name",
    f"SELECT COUNT(DISTINCT unnamed_subquery) AS n FROM (SELECT {PASSCODE} FROM Session__c)",
    f"SELECT COUNT(*) AS n FROM (SELECT {PASSCODE} FROM Session__c) AS Name GROUP BY Name",
    f"WITH Name AS (SELECT {PASSCODE} FROM Session__c) SELECT COUNT(DISTINCT Name) AS n FROM Name",
    f"SELECT COUNT(DISTINCT unnamed_subquery2) AS n FROM (SELECT 1 AS x), (SELECT {PASSCODE} FROM Session__c)",
    f"SELECT COUNT(DISTINCT t.s) AS n FROM (SELECT s FROM (SELECT {PASSCODE} FROM Session__c) AS s) AS t",
    "SELECT COUNT(*) AS n FROM Session__c WHERE CAST(main.Session__c AS VARCHAR) LIKE '%ZX%'",
    "SELECT (SELECT MAX(LENGTH(CAST(Session__c AS VARCHAR))) AS n FROM Session__c) AS n FROM Log__c LIMIT 1",
    f"SELECT COUNT(DISTINCT #1) AS n FROM (SELECT {PASSCODE} FROM Session__c)",
    f"SELECT COUNT(*) AS n FROM (SELECT {PASSCODE} FROM Session__c) GROUP BY #1",
]


@pytest.mark.parametrize("statement", COUNTS_THE_MARKER)
def test_a_figure_that_would_count_the_marker_is_refused(one, monkeypatch, statement):
    model = _steered(monkeypatch, statement)
    text, meta = ask("what about their documents?")
    assert text == CREDENTIAL_LINE
    assert "data" not in meta
    assert not model.narrative_prompts


@pytest.mark.parametrize(
    "condition",
    [
        '"{c}" LIKE \'%1%\'',
        'o."{c}" LIKE \'%a%\'',
        '"o"."{c}" LIKE \'%a%\'',
        '"{c}" = \'ZX-2011\'',
        '"{c}" IN (\'ZX-2011\')',
        '"{c}" NOT LIKE \'%1%\'',
        'lower("{c}") = \'x\'',
        '"{c}"::VARCHAR = \'x\'',
        '"{c}" IS DISTINCT FROM \'x\'',
    ],
)
def test_a_quoted_column_is_the_same_column(one, monkeypatch, condition):
    Model(monkeypatch, f'SELECT COUNT(*) AS n FROM "Onboarding__c" o WHERE {condition.format(c=DOC)}')
    text, _meta = ask("how many records match?")
    assert text == CREDENTIAL_LINE


@pytest.mark.parametrize(
    "statement",
    [
        f'SELECT COUNT(DISTINCT "{DOC}") AS n FROM "Onboarding__c"',
        f'SELECT COUNT(*) AS n FROM Onboarding__c o JOIN Account a ON o."{DOC}" = a."{DOC}"',
        f'SELECT COUNT(*) AS n FROM Onboarding__c o JOIN Account a USING ("{DOC}")',
        f'SELECT COUNT(*) AS n FROM (SELECT 1 FROM Onboarding__c GROUP BY "{DOC}" HAVING COUNT(*) > 1)',
    ],
)
def test_a_quoted_column_is_refused_in_every_clause(one, monkeypatch, statement):
    Model(monkeypatch, statement)
    text, _meta = ask("how many different documents do we hold?")
    assert text == CREDENTIAL_LINE


@pytest.mark.parametrize(
    "statement, truth",
    [
        (f"SELECT COUNT({DOC}) AS n FROM Onboarding__c", 9),
        (f'SELECT COUNT("{DOC}") AS n FROM Onboarding__c', 9),
        (f"SELECT COUNT(*) AS n FROM Onboarding__c WHERE {DOC} IS NOT NULL", 9),
        (f'SELECT COUNT(*) AS n FROM Onboarding__c WHERE "{DOC}" IS NOT NULL', 9),
        (f'SELECT COUNT(*) AS n FROM Onboarding__c o WHERE "o"."{DOC}" IS NULL', 1),
        (f"SELECT COUNT(*) AS n FROM Onboarding__c WHERE {DOC} <> ''", 8),
        (f"SELECT COUNT(*) AS n FROM Onboarding__c WHERE COALESCE({DOC}, '') <> ''", 8),
        (f"SELECT COUNT(*) AS n FROM Onboarding__c WHERE COALESCE({DOC}, '') = ''", 2),
        (f"SELECT COUNT(*) AS n FROM Onboarding__c WHERE NULLIF({DOC}, '') IS NOT NULL", 8),
        (f"SELECT COUNT(*) AS n FROM Onboarding__c WHERE NULLIF({DOC}, '') IS NULL", 2),
        (f"SELECT COUNT(*) AS n FROM Onboarding__c WHERE LENGTH({DOC}) > 0", 8),
        (f"SELECT COUNT(*) AS n FROM Onboarding__c WHERE LENGTH({DOC}) = 0", 1),
        (f"SELECT SUM(CASE WHEN {DOC} IS NOT NULL THEN 1 ELSE 0 END) AS n FROM Onboarding__c", 9),
        (f"SELECT COUNT(*) FILTER (WHERE COALESCE({DOC}, '') <> '') AS n FROM Onboarding__c", 8),
        (f"SELECT COUNT(NULLIF({DOC}, '')) AS n FROM Onboarding__c", 8),
        ("SELECT COUNT(*) AS n FROM Onboarding__c", 10),
        ("SELECT COUNT(*) AS n FROM Task WHERE EXISTS (SELECT * FROM Onboarding__c)", 2),
    ],
)
def test_whether_it_is_filled_in_stays_right(one, monkeypatch, statement, truth):
    model = Model(monkeypatch, statement)
    _text_answer, meta = ask("how many onboarding records have a passport number on file?")
    assert meta["data"] == [{"n": truth}]
    assert len(model.writer_prompts) == 1


@pytest.mark.parametrize(
    "condition",
    [
        f"TRIM({DOC}) <> ''",
        f"LENGTH(TRIM({DOC})) > 0",
        f"LENGTH({DOC}) > 05",
        f"LENGTH({DOC}) > 0 + 4",
        f"COALESCE({DOC}, '') <> '' || 'x'",
        f"Name = '{DOC}'",
        f"COLUMNS('{DOC}') LIKE '%1%'",
    ],
)
def test_what_only_looks_like_a_blank_check_stays_refused(one, monkeypatch, condition):
    Model(monkeypatch, f"SELECT COUNT(*) AS n FROM Onboarding__c WHERE {condition}")
    text, _meta = ask("how many onboarding records are there?")
    assert text == CREDENTIAL_LINE


def test_showing_it_still_answers_with_the_rest_and_the_line(one, monkeypatch):
    Model(monkeypatch, f"SELECT Name, {DOC} AS doc FROM Onboarding__c LIMIT 2")
    text, meta = ask("show 2 onboarding records")
    assert [list(row) for row in meta["data"]] == [["Name"], ["Name"]]
    assert CREDENTIAL_LINE in text


def test_a_cte_called_query_does_not_make_the_function_harmless(one, monkeypatch):
    Model(monkeypatch, "WITH query (a) AS (SELECT 1) SELECT * FROM query('SELECT 1')")
    _text_answer, meta = ask("how many tasks are there?")
    assert "data" not in meta


# ── A CTE called what a withheld table is called ─────────────────────────────


RENAME = "SELECT COUNT(*) AS n FROM Session__c"


@pytest.mark.parametrize(
    "statement",
    [
        "WITH Session__c AS (SELECT Id FROM main.Session__c) SELECT COUNT(*) AS n FROM Session__c s JOIN Task t ON 1 = 1",
        "WITH session__c AS (SELECT Id FROM Task) SELECT COUNT(*) AS n FROM session__c",
        'WITH "Session__c" AS (SELECT Id FROM Task) SELECT COUNT(*) AS n FROM "Session__c"',
        "WITH x AS (SELECT 1 AS a), Session__c AS (SELECT Id FROM Task) SELECT COUNT(*) AS n FROM Session__c",
        "WITH Session__c AS (SELECT Id FROM Session__c) SELECT COUNT(*) AS n FROM Session__c",
    ],
)
def test_the_retry_is_told_the_true_reason_to_rename(one, monkeypatch, statement):
    model = Model(monkeypatch, statement, RENAME)
    _text_answer, meta = ask("how many sessions are there?")
    assert "A WITH name cannot be the name of a table: Session__c" in model.writer_user(1)
    assert "Duplicate CTE" not in model.writer_user(1)
    assert meta["data"] == [{"n": 8}]


def test_a_writer_that_will_not_rename_gets_the_honest_line(one, monkeypatch):
    statement = "WITH session__c AS (SELECT Id FROM Task) SELECT COUNT(*) AS n FROM session__c"
    model = Model(monkeypatch, statement, statement, statement)
    text, _meta = ask("how many sessions are there?")
    assert COULD_NOT_WRITE in text
    assert len(model.writer_prompts) == 3


@pytest.mark.parametrize(
    "statement",
    [
        # Each gave a false figure under a wrapper or nested: main.T read the writer's own CTE.
        "SELECT * FROM (WITH Session__c AS (SELECT Id FROM main.Session__c WHERE Name LIKE 'S-1%') "
        "SELECT (SELECT COUNT(*) FROM Session__c) AS filtered, (SELECT COUNT(*) FROM main.Session__c) AS total) t "
        "WHERE EXISTS (SELECT 1 FROM Task)",
        "SELECT COUNT(*) AS n FROM Task t WHERE t.Id IN (WITH Session__c AS (SELECT Id FROM main.Session__c WHERE Name LIKE 'S-1%') "
        "SELECT m.Id FROM main.Session__c m LEFT JOIN Session__c a ON a.Id = m.Id WHERE a.Id IS NULL)",
    ],
)
def test_a_nested_cte_of_that_name_gives_no_figure(one, monkeypatch, statement):
    model = Model(monkeypatch, statement, statement, statement)
    text, meta = ask("how many sessions are there?")
    assert COULD_NOT_WRITE in text
    assert "data" not in meta
    assert "A WITH name cannot" in model.writer_user(1)


@pytest.mark.parametrize(
    "statement",
    [
        f"WITH session__c AS (SELECT {PASSCODE} AS note FROM Session__c) SELECT note FROM session__c WHERE note IS NOT NULL",
        f"WITH session__c AS (SELECT 1 AS x) SELECT {PASSCODE} AS note FROM main.Session__c",
        f"WITH other AS (SELECT {PASSCODE} AS note FROM Session__c), session__c AS (SELECT 1 AS x) SELECT note FROM other",
        f"SELECT * FROM (WITH session__c AS (SELECT 1 AS x) SELECT {PASSCODE} AS note FROM main.Session__c)",
        f"WITH RECURSIVE session__c AS (SELECT {PASSCODE} AS note FROM main.Session__c UNION ALL SELECT note FROM session__c WHERE FALSE) SELECT note FROM session__c",
    ],
)
def test_a_cte_of_that_name_reads_no_real_value(one, monkeypatch, statement):
    model = Model(monkeypatch, statement, statement, statement)
    text, meta = ask("show the codes")
    assert not leaks(model, meta, text)


@pytest.mark.parametrize(
    "statement, read",
    [
        ("WITH a AS (SELECT Id FROM T) SELECT COUNT(*) FROM a", ["T"]),
        ("WITH t AS (SELECT Id FROM T) SELECT COUNT(*) FROM t", ["T"]),
        ("WITH t AS (SELECT 0 AS n) SELECT n FROM t", []),
        ("WITH RECURSIVE t AS (SELECT 0 AS n UNION ALL SELECT n FROM t WHERE FALSE) SELECT n FROM t", []),
        ("WITH t AS (SELECT Id FROM U) SELECT COUNT(*) FROM main.T", ["U", "T"]),
        ("WITH a AS (SELECT 1 FROM b), b AS (SELECT 1) SELECT * FROM a", ["b"]),
        ("WITH a AS (SELECT 1), b AS (SELECT * FROM a) SELECT * FROM b", []),
        ("SELECT 1 FROM t WHERE a IS DISTINCT FROM b", ["t"]),
    ],
)
def test_a_name_is_the_statements_own_only_where_its_cte_can_be_seen(statement, read):
    assert _tables_named(statement) == read


def test_a_cte_over_the_table_it_is_named_after_reaches_the_table(one, monkeypatch):
    """Not "could not find": the table is synced, and only the name is in the way. The retry
    says so, because every table read has a shadow of its name now."""
    statement = "WITH task AS (SELECT Id FROM Task) SELECT COUNT(*) AS n FROM task"
    model = Model(monkeypatch, statement, "WITH t AS (SELECT Id FROM Task) SELECT COUNT(*) AS n FROM t")
    text, meta = ask("how many tasks are there?")
    assert "Rename your WITH table" in model.writer_user(1)
    assert COULD_NOT_FIND not in text
    assert meta["data"] == [{"n": 2}]


def test_a_count_of_the_real_table_is_not_labelled_as_its_namesake(one, monkeypatch):
    Model(
        monkeypatch,
        "WITH recruiter__c AS (SELECT Id FROM Task) SELECT COUNT(*) AS n FROM main.Recruiter__c",
        "WITH t AS (SELECT Id FROM Task) SELECT COUNT(*) AS n FROM main.Recruiter__c",
    )
    text, meta = ask("how many recruiters are there?")
    assert meta["data"] == [{"n": 2}]
    assert "Recruiter__c" in text
    assert "every row of" not in text, "no JOIN joins these two"


# ── A reserved alias with a star ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "statement, renamed",
    [
        ("SELECT at.* FROM t at", "SELECT at_t.* FROM t at_t"),
        ("SELECT at.*, at.Id AS i FROM t at", "SELECT at_t.*, at_t.Id AS i FROM t at_t"),
        ("SELECT t.* FROM t at WHERE x = 'at.*'", None),
    ],
)
def test_a_reserved_alias_is_renamed_before_a_star_too(statement, renamed):
    assert sql_engine._rename_reserved_aliases(statement) == (renamed or statement)


def test_a_reserved_alias_with_a_star_does_not_spend_the_retry(one, monkeypatch):
    model = Model(monkeypatch, "SELECT at.* FROM Task at WHERE Subject = 'Call'")
    _text_answer, meta = ask("show the call tasks")
    assert [row["Subject"] for row in meta["data"]] == ["Call"]
    assert len(model.writer_prompts) == 1


# ── Keys of a cell are a place values can sit ────────────────────────────────


def test_the_marker_is_seen_as_a_key_too():
    assert sql_engine._holds_withheld([[{"[withheld]": 3}]], 0)
    assert not sql_engine._holds_withheld([[{"Open": 3}]], 0)


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT histogram(Email__c) AS note FROM Recruiter__c",
        "SELECT map([Email__c], [1]) AS note FROM Recruiter__c",
    ],
)
def test_a_contact_value_held_as_a_key_is_held_back(one, monkeypatch, statement):
    model = Model(monkeypatch, statement)
    text, meta = ask("list these records please")
    assert "example.test" not in json.dumps([text, meta.get("data"), model.narrative_prompts], default=str)


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT histogram(Id) AS note FROM Recruiter__c",
        "SELECT map([Id], [1]) AS note FROM Recruiter__c",
        "SELECT struct_pack(a := Id, b := Id) AS note FROM Recruiter__c",
    ],
)
def test_a_record_id_held_as_a_key_is_held_back(one, monkeypatch, statement):
    model = Model(monkeypatch, statement)
    text, meta = ask("list these records please")
    assert "a0RXx" not in json.dumps([text, meta.get("data"), model.narrative_prompts], default=str)


def test_a_key_that_is_only_a_label_is_kept(one, monkeypatch):
    Model(monkeypatch, "SELECT histogram(Status__c) AS statuses FROM Recruiter__c")
    _text_answer, meta = ask("how are the statuses spread?")
    assert meta["data"] == [{"statuses": {"Active": 2}}]


def test_a_withheld_marker_under_a_key_empties_the_cell_for_any_caller():
    """_hold_back on its own, so a route that fetched rows another way is covered too."""
    held = _hold_back("SELECT x", ["x"], [[{"[withheld]": 2}]], "show it", {})
    assert held[1] == []
    assert held.left_out["credentials"] == ["x"]


def test_the_mask_stays_out_of_a_statement_that_reads_no_table(one):
    schema = schema_cache.get(settings.duckdb_path)
    statement = "SELECT 1 AS n"
    assert _behind_the_mask(statement, schema) == statement


def test_a_table_with_nothing_held_still_reads_through_the_hide_and_its_id_does_not(one):
    masked = _behind_the_mask("SELECT Subject FROM Task", schema_cache.get(settings.duckdb_path))
    assert 'regexp_replace("Subject"' in masked
    assert 'regexp_replace("Id"' not in masked


# ── A secret in a text that is not held, seen by the statement itself ────────


def _reads(meta) -> str:
    return json.dumps(meta.get("data"), default=str)


#: The finished cell looks harmless after each of these, so its hide finds nothing.
TAKEN_OUT_OF_TEXT = [
    "SELECT Name, split_part(Notes__c, ':', 2) AS ref FROM Meeting__c",
    "SELECT Name, regexp_extract(Notes__c, '[Pp]asscode[^0-9A-Za-z]+(.+)', 1) AS ref FROM Meeting__c",
    "SELECT Name, substr(Notes__c, 11) AS ref FROM Meeting__c",
    "SELECT Name, reverse(Notes__c) AS ref FROM Meeting__c",
    "SELECT Name, replace(Notes__c, 'passcode', 'p') AS ref FROM Meeting__c",
    "SELECT Name, split_part(Work_History__c, '=', 2) AS ref FROM Meeting__c",
    "SELECT Name, regexp_extract(Reply_Json__c, 'zak=([^\"]+)', 1) AS ref FROM Meeting__c",
    "WITH c AS (SELECT Notes__c AS n FROM Meeting__c) SELECT reverse(n) AS ref FROM c",
    "SELECT Name, reverse(m.Notes__c) AS ref FROM main.Meeting__c m",
]


@pytest.mark.parametrize("statement", TAKEN_OUT_OF_TEXT)
def test_a_string_function_sees_free_text_with_the_secret_already_gone(wh, monkeypatch, statement):
    model = Model(monkeypatch, statement)
    text, meta = ask("show the reference for each record")
    assert "data" in meta
    said = _reads(meta) + json.dumps(model.narrative_prompts, default=str) + text
    assert not re.search(r"ZX-|-XZ|\d{3}-XZ", said)


def test_free_text_cannot_be_guessed_at_through_a_count(one, monkeypatch):
    Model(monkeypatch, "SELECT COUNT(*) AS n FROM Meeting__c WHERE Notes__c LIKE '%ZX-%'")
    _text_answer, meta = ask("how many notes mention that?")
    assert meta["data"] == [{"n": 0}]


def test_free_text_stays_searchable_on_what_is_not_secret(one, monkeypatch):
    Model(monkeypatch, "SELECT COUNT(*) AS n FROM Meeting__c WHERE Notes__c LIKE '%passcode%'")
    _text_answer, meta = ask("how many notes mention a passcode?")
    assert meta["data"] == [{"n": 1}]


#: A hidden value makes one link of many: these came back with wrong figures or a value.
ONLY_SHOWN_OR_COUNTED = [
    "SELECT Name, split_part(Meeting_Link__c, '=', 2) AS ref FROM Meeting__c",
    "SELECT Name, regexp_extract(Meeting_Link__c, 'pwd=([^&]+)', 1) AS ref FROM Meeting__c",
    "SELECT Name, replace(Meeting_Link__c, '?pwd=', '?p=') AS ref FROM Meeting__c",
    "SELECT Name, reverse(Meeting_Link__c) AS ref FROM Meeting__c",
    "SELECT COUNT(DISTINCT Meeting_Link__c) AS n FROM Meeting__c",
    "SELECT Meeting_Link__c, COUNT(*) AS n FROM Meeting__c GROUP BY 1",
    "SELECT COUNT(*) AS n FROM Meeting__c a JOIN Meeting__c b ON a.Meeting_Link__c = b.Meeting_Link__c",
    "SELECT COUNT(*) AS n FROM Meeting__c WHERE Meeting_Link__c LIKE '%pwd=Z%'",
    "SELECT COUNT(DISTINCT m) AS n FROM Meeting__c m",
    "SELECT to_json(m) AS whole FROM Meeting__c m",
]


@pytest.mark.parametrize("statement", ONLY_SHOWN_OR_COUNTED)
def test_a_link_can_only_be_shown_counted_or_tested_for_being_filled(wh, monkeypatch, statement):
    model = _steered(monkeypatch, statement)
    text, meta = ask("what about the links?")
    assert COULD_NOT_WRITE in text
    assert "data" not in meta
    assert "link or URL column" in model.writer_user(1), "the retry is told what to do instead"
    assert not leaks(model, meta, text)


@pytest.mark.parametrize(
    "statement, truth",
    [
        ("SELECT COUNT(Meeting_Link__c) AS n FROM Meeting__c", 2),
        ("SELECT COUNT(*) AS n FROM Meeting__c WHERE Meeting_Link__c IS NOT NULL", 2),
        ("SELECT COUNT(*) AS n FROM Meeting__c WHERE Meeting_Link__c IS NULL", 1),
    ],
)
def test_a_links_figures_that_stay_right_still_answer(one, monkeypatch, statement, truth):
    Model(monkeypatch, statement)
    _text_answer, meta = ask("how many meetings have a link?")
    assert meta["data"] == [{"n": truth}]


def test_a_link_shown_as_it_is_keeps_all_but_its_secret(wh, monkeypatch):
    Model(monkeypatch, "SELECT Name, Meeting_Link__c FROM Meeting__c")
    _text_answer, meta = ask("show the link for each meeting")
    assert "https://meet.example.test/j/1?pwd=[hidden]&x=1" in _reads(meta)
    assert SECRET not in _reads(meta)


@pytest.mark.parametrize(
    "cell",
    [
        "https://meet.example.test/j/1?pwd=ZX-1&x=2",
        "https://meet.example.test/j/1?a=1&host_token=ZX-1",
        "https://meet.example.test/s/1?zak=ZX-1",
        "https://store.example.test/f?sig=ZX-1&X-Amz-Credential=ZX-2",
        "see https://meet.example.test/j/1?pwd=ZX-1 and https://meet.example.test/j/2?pwd=ZX-2 here",
        "Passcode: ZX-1",
        "passcode ZX-1",
        "password=ZX-1,next",
        "my_api_key : 'ZX-1'",
        '{"host_token":"ZX-1","other":1}',
        "line one\npwd=ZX-1\nline three",
        "otp=ZX-1;ssn:ZX-2",
        "passport number: ZX12",
        "Passport ZX12 and more",
        "passport pending",
        "PAN no AB12",
        "bank account: 1234",
        "account 1234",
        "a/c no 1234",
        "routing number=12",
        "aadhaar 1234",
        "x-secret: already [hidden]",
        "nothing to see",
        "a key without a label",
        "",
    ],
)
def test_the_statement_and_the_finished_cell_hide_the_same(cell):
    """Two engines with one set of rules: if they part, a value passes one of them."""
    in_sql = duckdb.connect(":memory:").execute(f"SELECT {sql_engine._hide_in_sql('c')} FROM (SELECT ? AS c)", [cell]).fetchone()[0]
    assert in_sql == sql_engine._hide_embedded_secrets(cell)


def test_a_column_added_to_the_rules_needs_no_name_list(one):
    """Work_History__c says nothing of secrets or links, and it still reads through the hide."""
    masked = _behind_the_mask("SELECT Work_History__c FROM Meeting__c", schema_cache.get(settings.duckdb_path))
    assert 'regexp_replace("Work_History__c"' in masked


# ── Bytes, tuples and encoders: what the text rules cannot read ──────────────


ENCODED = [
    "SELECT Name, encode(Email__c) AS who FROM Recruiter__c",
    "SELECT Name, hex(Email__c) AS who FROM Recruiter__c",
    "SELECT Name, to_base64(encode(Email__c)) AS who FROM Recruiter__c",
    "SELECT Name, url_encode(Email__c) AS who FROM Recruiter__c",
    "SELECT Name, CAST(Email__c AS BLOB) AS who FROM Recruiter__c",
    "SELECT Name, CAST(CAST(Email__c AS BLOB) AS BIT) AS who FROM Recruiter__c",
    "SELECT Name, CAST(CAST([Email__c] AS BLOB[]) AS BIT[]) AS who FROM Recruiter__c",
    "SELECT Name, [encode(Email__c)] AS who FROM Recruiter__c",
    "SELECT Name, struct_pack(a := encode(Email__c)) AS who FROM Recruiter__c",
    "SELECT Name, map(['a'], [encode(Email__c)]) AS who FROM Recruiter__c",
    'SELECT Name, "hex"(Email__c) AS who FROM Recruiter__c',
    "SELECT Name, Email__c.hex() AS who FROM Recruiter__c",
    "SELECT Name, encode(Id) AS who FROM Recruiter__c",
    "SELECT Name, hex(Meeting_Link__c) AS who FROM Meeting__c",
]


@pytest.mark.parametrize("statement", ENCODED)
def test_an_encoded_value_is_refused_with_what_to_do(one, monkeypatch, statement):
    model = _steered(monkeypatch, statement)
    text, meta = ask("show who each person is")
    assert COULD_NOT_WRITE in text
    assert "data" not in meta
    assert "as it is stored" in model.writer_user(1)
    assert "example.test" not in json.dumps(meta, default=str)


@pytest.mark.parametrize(
    "cell, unreadable",
    [
        (b"x", True),
        (("a", 1), True),
        ([("a", 1)], True),
        ([b"x"], True),
        ({"a": b"x"}, True),
        ({b"a": 1}, True),
        ({("a",): 1}, True),
        ({"a": [{"b": (1,)}]}, True),
        ({"https://meet.example.test/j/1?pwd=ZX-1": 1}, True),
        ({"passcode: ZX-1": 1}, True),
        (None, False),
        ("x", False),
        (1, False),
        (True, False),
        (1.5, False),
        (Decimal("1.5"), False),
        (date(2026, 1, 1), False),
        (clock_time(10, 0), False),
        (timedelta(days=1), False),
        (UUID(int=1), False),
        (["a", 1, None], False),
        ({"a": [1, {"b": "c"}]}, False),
    ],
)
def test_only_what_the_text_rules_can_read_is_readable(cell, unreadable):
    assert sql_engine._is_unreadable(cell) is unreadable


def test_every_row_is_looked_at_not_a_sample():
    rows = [["fine"]] * 500 + [[("a", 1)]]
    held = _hold_back("SELECT x FROM t", ["x"], rows, "show them", {})
    assert held.left_out["credentials"] == ["x"]


@pytest.mark.parametrize(
    "statement, kept",
    [
        ("SELECT Name, row(Email__c, 1) AS who FROM Recruiter__c", ["Name"]),
        ("SELECT Name, array_value(row(Email__c, 1)) AS who FROM Recruiter__c", ["Name"]),
        ("SELECT Name, map([row(Email__c, 1)], [1]) AS who FROM Recruiter__c", ["Name"]),
    ],
)
def test_a_tuple_is_dropped_however_it_is_wrapped(one, monkeypatch, statement, kept):
    Model(monkeypatch, statement)
    _text_answer, meta = ask("show who each person is")
    assert "example.test" not in _reads(meta)
    assert list(meta["data"][0]) == kept


def test_a_name_in_the_question_does_not_keep_a_tuple(one, monkeypatch):
    Model(monkeypatch, "SELECT Name, row(Email__c, 1) AS Email__c FROM Recruiter__c")
    _text_answer, meta = ask("show the email of each recruiter")
    assert "example.test" not in _reads(meta)


def test_a_map_keyed_by_free_text_shows_no_secret_in_its_key(one, monkeypatch):
    Model(monkeypatch, "SELECT Name, map([Notes__c], [1]) AS note FROM Meeting__c WHERE Notes__c LIKE 'pass%'")
    _text_answer, meta = ask("show the notes")
    assert SECRET not in _reads(meta)


def test_a_time_an_interval_and_a_uuid_still_answer(one, monkeypatch):
    Model(
        monkeypatch,
        "SELECT Name, TIME '10:00:00' AS at_time, INTERVAL 1 DAY AS span, uuid() AS tag FROM Recruiter__c",
    )
    _text_answer, meta = ask("show the recruiters")
    assert {"at_time", "span", "tag"} <= set(meta["data"][0])


# ── A shape the walker does not know is refused ──────────────────────────────


def test_a_place_in_the_select_list_is_the_label_this_reads():
    node = sql_tree.read("SELECT #1 FROM t")["statements"][0]["node"]["select_list"][0]
    assert node["class"] == "POSITIONAL_REFERENCE"


def test_the_default_name_of_a_subquery_binds_as_a_whole_row_as_this_expects():
    con = duckdb.connect(":memory:")
    assert con.execute("SELECT unnamed_subquery FROM (SELECT 1 AS x)").fetchone()[0] == {"x": 1}
    assert con.execute("SELECT unnamed_subquery2 FROM (SELECT 1 AS x), (SELECT 2 AS y)").fetchone()[0] == {"y": 2}


def test_a_place_on_a_table_with_nothing_held_still_answers(one, monkeypatch):
    Model(monkeypatch, "SELECT COUNT(DISTINCT #2) AS n FROM Task")
    _text_answer, meta = ask("how many task subjects are there?")
    assert meta["data"] == [{"n": 2}]


def test_a_parameter_still_gets_its_retry(one, monkeypatch):
    model = Model(
        monkeypatch,
        "SELECT COUNT(*) AS n FROM Session__c WHERE Name = ?",
        "SELECT COUNT(*) AS n FROM Session__c",
    )
    _text_answer, meta = ask("how many sessions are there?")
    assert len(model.writer_prompts) == 2
    assert meta["data"] == [{"n": 8}]


@pytest.mark.parametrize(
    "statement, truth",
    [
        ("SELECT COUNT(*) AS n FROM Log__c JOIN Session__c ON Session__c = Session__c.Id", 2),
        ("SELECT COUNT(*) AS n FROM Log__c WHERE Session__c IN (SELECT Id FROM Session__c)", 2),
        ("SELECT COUNT(*) AS n FROM Session__c WHERE Id IN (SELECT Session__c FROM Log__c)", 1),
        ("SELECT COUNT(DISTINCT Name) AS n FROM Session__c AS Name", 8),
        (f"SELECT COUNT({PASSCODE}) AS n FROM (SELECT {PASSCODE} FROM Session__c)", 6),
    ],
)
def test_a_lookup_or_an_alias_named_like_a_relation_stays_right(one, monkeypatch, statement, truth):
    Model(monkeypatch, statement)
    _text_answer, meta = ask("how many are there?")
    assert meta["data"] == [{"n": truth}]


# ── Nothing about the server comes back, and raw is not read ─────────────────


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT current_setting('temp_directory') AS note, Subject FROM Task LIMIT 1",
        "SELECT \"CURRENT_SETTING\"('secret_directory') AS note, Subject FROM Task LIMIT 1",
        "SELECT Subject, ('temp_directory').current_setting() AS note FROM Task LIMIT 1",
        "SELECT pg_get_viewdef(g) AS note, Subject FROM Task, range(5000) r(g) LIMIT 1",
        "SELECT version() AS note, Subject FROM Task LIMIT 1",
    ],
)
def test_a_scalar_function_says_nothing_about_the_server(one, monkeypatch, statement):
    model = _steered(monkeypatch, statement)
    text, meta = ask("what is this?")
    assert "data" not in meta
    assert COULD_NOT_WRITE in text
    assert "is not allowed" in model.writer_user(1)


def test_the_date_functions_keep_working(one, monkeypatch):
    Model(monkeypatch, "SELECT current_date() AS d, COUNT(*) AS n FROM Task GROUP BY 1")
    _text_answer, meta = ask("how many tasks as of today?")
    assert meta["data"][0]["n"] == 2


def test_the_sync_workers_plumbing_is_not_read(one, monkeypatch):
    con = duckdb.connect(one)
    con.execute("CREATE TABLE raw._rag_index_pending (object_name VARCHAR, last_error VARCHAR)")
    con.execute("INSERT INTO raw._rag_index_pending VALUES ('Task', 'ConnectError: http://svc.example.test:8000')")
    con.execute("CHECKPOINT")
    con.close()
    schema_cache.invalidate()
    model = _steered(monkeypatch, f"SELECT object_name, last_error FROM raw._rag_index_pending{KNOWN}")
    text, meta = ask("what errors are pending?")
    assert "data" not in meta
    assert "svc.example.test" not in json.dumps(meta, default=str) + text
    assert "bare name" in model.writer_user(1)


@pytest.mark.parametrize(
    "qualified", ["raw.Recruiter__c", '"raw"."Recruiter__c"', "warehouse.raw.Recruiter__c"]
)
def test_raw_is_refused_where_its_text_columns_would_answer_wrong(one, monkeypatch, qualified):
    """Every raw column is VARCHAR, so MAX compares strings: '9' beats '250'."""
    _steered(monkeypatch, f"SELECT MAX(Score__c) AS n FROM {qualified}")
    _text_answer, meta = ask("what is the highest score?")
    assert "data" not in meta


# ── How deep a statement may be ──────────────────────────────────────────────


def _nested(levels: int) -> str:
    inner = "1"
    for _ in range(levels):
        inner = f"({inner}+1)"
    return inner


@pytest.mark.parametrize(
    "figure",
    [_nested(250), _nested(450), _nested(700), "+".join(["1"] * 300), "||".join(["'x'"] * 300)],
    ids=["250", "450", "700", "plus-300", "concat-300"],
)
def test_a_statement_nested_too_deep_is_refused_before_it_runs(one, monkeypatch, figure):
    """Past the limit it ran, and then a walk of its tree raised out of the engine."""
    model = _steered(monkeypatch, f"SELECT {figure} AS n, Subject FROM Task LIMIT 1")
    text, meta = ask("what is this?")
    assert COULD_NOT_WRITE in text
    assert "data" not in meta
    assert "nested too deeply" in model.writer_user(1)


def test_a_deep_read_of_a_held_column_reads_nothing(one, monkeypatch):
    _steered(monkeypatch, f"SELECT {_nested(300)} AS n, {PASSCODE} AS code FROM Session__c")
    text, meta = ask("what is this?")
    assert "data" not in meta and SECRET not in text


def test_a_nesting_nobody_would_refuse_still_answers(one, monkeypatch):
    Model(monkeypatch, f"SELECT {_nested(40)} AS n, Subject FROM Task LIMIT 1")
    _text_answer, meta = ask("what is this?")
    assert meta["data"][0]["n"] == 41


def test_the_depth_is_measured_without_recursion():
    tree = current = {}
    for _ in range(50_000):
        current["x"] = [{}]
        current = current["x"][0]
    with pytest.raises(sql_tree.StatementRefused):
        sql_tree.refuse_too_deep(tree)
    sql_tree.refuse_too_deep(None)


# ── The DuckDB whose trees this module reads ─────────────────────────────────


def test_another_duckdb_refuses_every_statement_and_says_why(one, monkeypatch, caplog):
    Model(monkeypatch, "SELECT COUNT(*) AS n FROM Task", "SELECT COUNT(*) AS n FROM Task", "SELECT COUNT(*) AS n FROM Task")
    monkeypatch.setattr(duckdb, "__version__", "2.0.0")
    text, meta = ask("how many tasks are there?")
    assert "data" not in meta
    assert COULD_NOT_WRITE in text
    assert "sql_tree refuses every statement" in caplog.text


def test_the_tree_gate_names_the_duckdb_the_tests_run():
    assert duckdb.__version__.startswith(sql_tree._READS_TREES_OF)
    pins = open(os.path.join(os.path.dirname(__file__), "..", "requirements-dev.txt"), encoding="utf-8").read()
    pin = re.search(r"^duckdb==(\S+)", pins, re.M).group(1)
    assert pin.startswith(sql_tree._READS_TREES_OF)


# ── The mask is built from the file the statement reads ──────────────────────


def _publish(build) -> None:
    """What sync-worker does: a new file under the same name."""
    target = settings.duckdb_path
    fresh = f"{target}.new"
    shutil.copyfile(target, fresh)
    con = duckdb.connect(fresh)
    build(con)
    con.execute("CHECKPOINT")
    con.close()
    os.replace(fresh, target)


def _adds_a_held_column(con) -> None:
    con.execute('ALTER TABLE raw."Task" ADD COLUMN "Bank_Account_Number__c" VARCHAR')
    con.execute("""UPDATE raw."Task" SET "Bank_Account_Number__c" = 'ZX-901'""")
    con.execute('CREATE OR REPLACE VIEW main."Task" AS SELECT * FROM raw."Task"')


def _adds_a_held_table(con) -> None:
    _create(con, "Pay__c", _text("Id", "Task__c", "Passport_Number__c"), [("a6PXx0000001AAAIA1", "00TXx0000001AAAIA1", "ZX-902")])


def _adds_a_row(con) -> None:
    con.execute("""INSERT INTO raw."Task" VALUES ('00TXx0000003AAAIA3', 'Visit')""")


ADDED_BY_A_PUBLISH = [
    (_adds_a_held_column, "SELECT Subject, Bank_Account_Number__c AS note FROM Task"),
    (_adds_a_held_column, 'SELECT Subject, Bank_Account_Number__c AS "Account Number" FROM Task'),
    (_adds_a_held_column, "SELECT Subject, Bank_Account_Number__c || 'x' AS note FROM Task"),
    (_adds_a_held_column, "WITH c AS (SELECT Bank_Account_Number__c AS note FROM Task) SELECT note FROM c"),
    (_adds_a_held_column, "SELECT t AS row_of_it FROM Task t"),
    (_adds_a_held_column, "SELECT COUNT(*) AS n FROM Task WHERE Bank_Account_Number__c LIKE 'ZX%'"),
    (_adds_a_held_table, "SELECT t.Subject, p.Passport_Number__c AS note FROM Task t JOIN Pay__c p ON p.Task__c = t.Id"),
]


@pytest.mark.parametrize("build, statement", ADDED_BY_A_PUBLISH)
def test_a_publish_before_the_question_reads_nothing_with_a_warm_cache(wh, monkeypatch, build, statement):
    schema_cache.get(settings.duckdb_path)
    _publish(build)
    model = _steered(monkeypatch, statement)
    text, meta = ask("what is on the task?")
    assert not leaks(model, meta, text)
    assert not any(row.get("n") == 2 for row in meta.get("data") or []), "a filter on it is an oracle"


@pytest.mark.parametrize("build, statement", ADDED_BY_A_PUBLISH[:2] + ADDED_BY_A_PUBLISH[-1:])
def test_a_publish_during_the_writers_call_reads_nothing(wh, monkeypatch, build, statement):
    model = _steered(monkeypatch, statement)
    writer = sql_engine.llm.chat_completion
    done = []

    async def publishing(messages, **kwargs):
        if not done:
            done.append(_publish(build))
        return await writer(messages, **kwargs)

    monkeypatch.setattr(sql_engine.llm, "chat_completion", publishing)
    text, meta = ask("what is on the task?")
    assert not leaks(model, meta, text)


@pytest.mark.parametrize("build, statement", ADDED_BY_A_PUBLISH[:2] + ADDED_BY_A_PUBLISH[-1:])
def test_a_publish_just_before_the_cursor_opens_reads_nothing(wh, monkeypatch, build, statement):
    model = _steered(monkeypatch, statement)
    real = sql_engine._execute
    done = []

    def publishing(sql, cap, *args, **kwargs):
        if not done:
            done.append(_publish(build))
        return real(sql, cap, *args, **kwargs)

    monkeypatch.setattr(sql_engine, "_execute", publishing)
    text, meta = ask("what is on the task?")
    assert not leaks(model, meta, text)


def test_a_new_text_field_on_a_credential_object_is_held_on_a_plain_star(wh, monkeypatch):
    def first(con):
        _create(con, "Candidate_Portal_Credential__c", _text("Id", "Name"), [("a7CXx0000001AAAIA1", "C-1")])

    def second(con):
        con.execute('ALTER TABLE raw."Candidate_Portal_Credential__c" ADD COLUMN "Portal_Pin__c" VARCHAR')
        con.execute("""UPDATE raw."Candidate_Portal_Credential__c" SET "Portal_Pin__c" = 'ZX-903'""")
        con.execute('CREATE OR REPLACE VIEW main."Candidate_Portal_Credential__c" AS SELECT * FROM raw."Candidate_Portal_Credential__c"')

    _publish(first)
    schema_cache.invalidate()
    schema_cache.get(settings.duckdb_path)
    _publish(second)
    model = Model(monkeypatch, "SELECT * FROM Candidate_Portal_Credential__c")
    text, meta = ask("show the portal credentials")
    assert not leaks(model, meta, text)


def test_an_ordinary_publish_during_the_writers_call_costs_nothing(wh, monkeypatch):
    model = Model(monkeypatch, "SELECT COUNT(*) AS n FROM Task")
    writer = sql_engine.llm.chat_completion

    async def publishing(messages, **kwargs):
        _publish(_adds_a_row)
        return await writer(messages, **kwargs)

    monkeypatch.setattr(sql_engine.llm, "chat_completion", publishing)
    _text_answer, meta = ask("how many tasks are there?")
    assert len(model.writer_prompts) == 1
    assert meta["data"] == [{"n": 3}]


def test_a_failed_read_of_the_columns_runs_nothing(wh, monkeypatch):
    def fails(_con):
        raise RuntimeError("invented failure")

    monkeypatch.setattr(sql_engine, "columns_on", fails)
    statement = f"SELECT Name, {PASSCODE} AS note FROM Session__c"
    model = _steered(monkeypatch, statement)
    text, meta = ask("what is the code?")
    assert "data" not in meta
    assert "invented failure" not in text
    assert not leaks(model, meta, text)


# ── A locked file freezes one member, not all of them ────────────────────────


_HOLDS_THE_LOCK = "import duckdb, sys; con = duckdb.connect(sys.argv[1]); print('held', flush=True); sys.stdin.read()"


@pytest.fixture()
def locked(one, monkeypatch):
    """The sync worker's write lock, from another process, and a schema past its time."""
    monkeypatch.setattr(schema_cache_module, "LOCK_WAIT_SECONDS", 1.5)
    schema = schema_cache.get(one)
    schema_cache._cache[one] = (time.monotonic() - 10 * schema_cache.ttl_seconds, schema)
    holder = subprocess.Popen([sys.executable, "-c", _HOLDS_THE_LOCK, one], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    assert holder.stdout.readline().strip() == "held"
    yield one
    holder.communicate()


async def _longest_stall(work) -> float:
    loop = asyncio.get_running_loop()
    task = asyncio.ensure_future(work)
    stall, last = 0.0, loop.time()
    while not task.done():
        await asyncio.sleep(0.01)
        now = loop.time()
        stall, last = max(stall, now - last), now
    await task
    return stall


def test_a_schema_read_under_the_lock_does_not_stop_the_loop(locked, monkeypatch):
    monkeypatch.setattr(sf_tools, "_dictionary_summary", lambda _question: "")
    assert asyncio.run(_longest_stall(sf_tools.get_salesforce_schema("how many tasks?"))) < 1.0


def test_a_question_under_the_lock_does_not_stop_the_loop(locked, monkeypatch):
    _steered(monkeypatch, "SELECT COUNT(*) AS n FROM Task")
    rec = Rec()
    assert asyncio.run(_longest_stall(sql_engine.run_sql_engine("how many tasks?", [], rec.emit))) < 1.0
