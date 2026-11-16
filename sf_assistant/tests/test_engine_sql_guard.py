"""sf_assistant.engine.sql_guard: quote_ident and check_sql (parser-based allowlist)."""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import pytest

ROOT = Path(__file__).resolve().parents[1]

from sf_assistant.engine.sql_guard import GuardError, check_sql, quote_ident  # noqa: E402

ALLOWED = frozenset({("main", "Interview__c"), ("main", "Recruiter__c"), ("raw", "_sync_meta")})
BLOCKED = frozenset({"zoom_passcode__c", "passport_number__c"})


@pytest.fixture(scope="module")
def cur():
    con = duckdb.connect()
    yield con.cursor()
    con.close()


def test_quote_ident():
    assert quote_ident("Status__c") == '"Status__c"'
    for bad in ('a"b', "a\x00b", ""):
        with pytest.raises(GuardError):
            quote_ident(bad)


@pytest.mark.parametrize("sql", [
    'SELECT 1 AS a; SELECT 2 AS b',
    'DROP TABLE main."Interview__c"',
    "ATTACH 'x.db' AS x",
    "SELECT a.x FROM read_csv('/etc/passwd') AS a",
    "SELECT q.x FROM query('select 1') AS q",
    "SELECT query('select 1') AS q",
    'SELECT t0."Id" FROM raw."Interview__c" AS t0',
    'SELECT t0."Id" FROM main."Account" AS t0',
    'SELECT * FROM main."Interview__c"',
    'SELECT t0."Zoom_Passcode__c" FROM main."Interview__c" AS t0',
    'SELECT COUNT(*) FROM main."Interview__c" AS t0 WHERE t0."Zoom_Passcode__c" IS NULL',
    'SELECT t0."Id" FROM warehouse.main."Interview__c" AS t0',
    'SELECT t0."Id" FROM "Interview__c" AS t0',
    "COPY (SELECT 1) TO 'x.csv'",
    "PRAGMA version",
    "SELECT r.x FROM range(10) AS r",
    "SELECT getenv('HOME') AS h",
    "",
    # whole-row references: DuckDB turns a table or alias used as a column into a STRUCT of every column
    'SELECT c FROM main."Interview__c" AS c',
    'SELECT "Interview__c" FROM main."Interview__c"',
    'SELECT main."Interview__c" FROM main."Interview__c"',
    "SELECT struct_extract(c, 'Zoom_Passcode__c') AS x FROM main.\"Interview__c\" AS c",
    "SELECT c['Zoom_Passcode__c'] AS x FROM main.\"Interview__c\" AS c",
    'SELECT to_json(t0) AS j FROM main."Interview__c" AS t0',
    "SELECT COLUMNS('Zoom.*') FROM main.\"Interview__c\" AS t0",
    'SELECT #2 FROM main."Interview__c" AS t0',
])
def test_rejected(cur, sql):
    with pytest.raises(GuardError):
        check_sql(cur, sql, ALLOWED, BLOCKED)


@pytest.mark.parametrize("sql", [
    'SELECT COUNT(*) AS n FROM main."Interview__c" AS t0 WHERE EXISTS (SELECT 1 FROM main."Recruiter__c" AS c1 '
    'WHERE c1."Id" = t0."Recruiter__c")',
    'SELECT COUNT(*) FILTER (WHERE t0."Status__c" = ?) AS part, COUNT(*) AS base FROM main."Interview__c" AS t0',
    'SELECT object_name, updated_at FROM raw."_sync_meta"',
    "SELECT COUNT(*) AS n FROM main.\"Interview__c\" AS t0 WHERE list_contains(string_split(t0.\"Eval__c\", ';'), ?)",
    "SELECT t0.\"Name\" FROM main.\"Interview__c\" AS t0 WHERE t0.\"Name\" ILIKE ? ESCAPE '\\' AND t0.\"Note__c\" = 'a;b'",
    'SELECT t1."Id" AS gid, COALESCE(t1."Name", CASE WHEN t0."Recruiter__c" IS NULL THEN \'(blank)\' ELSE '
    '\'(unresolved id)\' END) AS grp, COUNT(*) AS value FROM main."Interview__c" AS t0 LEFT JOIN main."Recruiter__c" '
    'AS t1 ON t1."Id" = t0."Recruiter__c" GROUP BY 1, 2 ORDER BY value DESC, grp ASC LIMIT 1001',
])
def test_accepted(cur, sql):
    check_sql(cur, sql, ALLOWED, BLOCKED)


def test_a_qualified_column_may_share_a_table_name(cur):
    """Session__c.Candidate_Training__c next to the Candidate_Training__c table is a column, not a whole row."""
    check_sql(cur, 'SELECT COUNT(*) AS n FROM main."Recruiter__c" AS t0 WHERE EXISTS (SELECT 1 FROM main."Interview__c" '
                   'AS c1 WHERE c1."Recruiter__c" = t0."Id")', ALLOWED, BLOCKED)


def test_blocked_fields_are_per_table(cur):
    """Text fields of a sensitive object are blocked in that table only (qualified or not)."""
    fields = frozenset({("recruiter__c", "email__c")})
    for sql in ('SELECT r."Email__c" FROM main."Recruiter__c" AS r',
                'SELECT "Email__c" FROM main."Recruiter__c" AS r',
                'SELECT COUNT(r."Email__c") AS n FROM main."Recruiter__c" AS r',
                'SELECT t0."Id" FROM main."Interview__c" AS t0 LEFT JOIN main."Recruiter__c" AS t1 ON t1."Id" = '
                't0."Recruiter__c" WHERE t1."Email__c" IS NULL'):
        with pytest.raises(GuardError):
            check_sql(cur, sql, ALLOWED, BLOCKED, fields)
    check_sql(cur, 'SELECT t0."Email__c" FROM main."Interview__c" AS t0', ALLOWED, BLOCKED, fields)


#: Statement shapes added for the lookup disambiguation (RECORDS_SPEC §21.4-21.7); the guard itself is unchanged.
_N = "lower(trim(regexp_replace(replace(x.\"Name\", chr(160), ' '), '\\s+', ' ', 'g')))"


@pytest.mark.parametrize("sql", [
    # name grounding G1 and G2
    f'SELECT x."Id" AS id, trim(x."Name") AS name, CAST(NULL AS VARCHAR) AS detail FROM main."Recruiter__c" AS x '
    f'WHERE {_N} = ANY(?) ORDER BY 2, 1 LIMIT 51',
    f"SELECT trim(x.\"Name\") AS name, CASE WHEN split_part({_N}, ?, 1) = ? THEN 1 WHEN x.\"Name\" ILIKE ? ESCAPE '\\' "
    f"THEN 2 ELSE 3 END AS how FROM main.\"Recruiter__c\" AS x WHERE split_part({_N}, ?, 1) = ? OR x.\"Name\" ILIKE ? "
    f"ESCAPE '\\' OR greatest(jaro_winkler_similarity({_N}, ?), jaro_winkler_similarity(split_part({_N}, ?, 1), ?)) "
    "> 0.88 GROUP BY 1, 2 ORDER BY 2, 1 LIMIT 13",
    # lookup check: FILTER with an uncorrelated subquery
    f'SELECT COUNT(*) FILTER (WHERE t0."Recruiter__c" IN (SELECT x."Id" FROM main."Recruiter__c" AS x WHERE {_N} = ?)) '
    'AS "n0", COUNT(*) FILTER (WHERE t0."Recruiter__c" = ANY(?)) AS "n1" FROM main."Interview__c" AS t0',
    # reference scan: one derived table per child object, cross-joined
    'SELECT a1."n1", a1."n2", a2."n3" FROM (SELECT COUNT(*) FILTER (WHERE r1."Recruiter__c" = ?) AS "n1", COUNT(*) '
    'FILTER (WHERE r1."Owner__c" = ?) AS "n2" FROM main."Interview__c" AS r1) AS a1 CROSS JOIN (SELECT COUNT(*) FILTER '
    '(WHERE r2."Manager__c" = ?) AS "n3" FROM main."Recruiter__c" AS r2) AS a2',
    # unresolved targets at open and the shared-name numbering of group labels
    'SELECT COUNT(*) FILTER (WHERE t0."Recruiter__c" IS NOT NULL AND t0."Recruiter__c" NOT IN (SELECT x."Id" FROM '
    'main."Recruiter__c" AS x)) AS "Recruiter__c__u" FROM main."Interview__c" AS t0',
    f'SELECT {_N} AS n, list(x."Id" ORDER BY x."Id") AS ids FROM main."Recruiter__c" AS x WHERE {_N} = ANY(?) GROUP BY 1 '
    'HAVING COUNT(*) > 1',
])
def test_accepted_lookup_statements(cur, sql):
    check_sql(cur, sql, ALLOWED, BLOCKED)


@pytest.mark.parametrize("sql", [
    'SELECT a1 FROM (SELECT COUNT(*) AS "n1" FROM main."Interview__c" AS r1) AS a1',
    'SELECT a1."n1" FROM (SELECT COUNT(*) FILTER (WHERE r1."Zoom_Passcode__c" = ?) AS "n1" FROM main."Interview__c" '
    'AS r1) AS a1',
    'SELECT a1."n1" FROM (SELECT COUNT(*) AS "n1" FROM main."Account" AS r1) AS a1',
])
def test_rejected_lookup_statements(cur, sql):
    with pytest.raises(GuardError):
        check_sql(cur, sql, ALLOWED, BLOCKED)
