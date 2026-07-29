"""SQL guard: single SELECT only; writes/DDL/multi-statement/comment tricks
rejected (spec §8)."""
import pytest

from app.core.sql_guard import SQLGuardError, guard_sql, is_safe_select


# --- accepted -------------------------------------------------------------

def test_simple_select():
    assert guard_sql("SELECT * FROM accounts") == "SELECT * FROM accounts"


def test_select_with_trailing_semicolon():
    assert guard_sql("SELECT 1;") == "SELECT 1"


def test_cte_with_select():
    sql = (
        "WITH t AS (SELECT stage, SUM(amount) AS total FROM opportunities "
        "GROUP BY 1) SELECT * FROM t ORDER BY total DESC"
    )
    assert guard_sql(sql).startswith("WITH t AS")


def test_keyword_inside_string_literal_is_allowed():
    sql = "SELECT id FROM notes WHERE body = 'DROP TABLE accounts'"
    assert is_safe_select(sql)


def test_column_names_containing_keywords_are_allowed():
    assert is_safe_select("SELECT update_date, payload, offset_value FROM t")


def test_comments_are_allowed_and_stripped():
    sql = "-- top accounts\nSELECT name FROM accounts /* inline note */ LIMIT 5"
    cleaned = guard_sql(sql)
    assert "--" not in cleaned and "/*" not in cleaned
    assert cleaned.startswith("SELECT")


def test_lowercase_select():
    assert is_safe_select("select 1")


# --- rejected: plain write / DDL statements --------------------------------

@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE accounts SET name = 'x'",
        "update accounts set name = 'x'",
        "INSERT INTO accounts VALUES (1)",
        "DELETE FROM accounts",
        "DROP TABLE accounts",
        "CREATE TABLE x (a INT)",
        "ALTER TABLE accounts ADD COLUMN x INT",
        "ATTACH 'other.db' AS other",
        "COPY accounts TO 'out.csv'",
        "PRAGMA database_list",
        "  pragma version",
        "INSTALL httpfs",
        "LOAD httpfs",
        "SET memory_limit = '1GB'",
        "CALL pragma_version()",
    ],
)
def test_forbidden_statements_rejected(sql):
    with pytest.raises(SQLGuardError):
        guard_sql(sql)


# --- rejected: multi-statement ----------------------------------------------

@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1; SELECT 2",
        "SELECT 1; DROP TABLE accounts",
        "SELECT 1; DROP TABLE accounts;",
        "SELECT 1 ; ; SELECT 2",
    ],
)
def test_multi_statement_rejected(sql):
    with pytest.raises(SQLGuardError):
        guard_sql(sql)


# --- rejected: comment-smuggled variants ------------------------------------

@pytest.mark.parametrize(
    "sql",
    [
        "DR/**/OP TABLE accounts",          # block comment splits DROP
        "UPD/**/ATE accounts SET a = 1",    # block comment splits UPDATE
        "UPD--x\nATE accounts SET a = 1",   # line comment splits UPDATE
        "SELECT 1;--\nDROP TABLE accounts", # comment hides second statement
        "SELECT/**/1;/**/DELETE FROM t",
        "SELECT 1 /*..*/; INSERT INTO t VALUES (1)",
    ],
)
def test_comment_smuggling_rejected(sql):
    with pytest.raises(SQLGuardError):
        guard_sql(sql)


# --- rejected: filesystem/network table functions (§1/§12) ------------------

@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM read_blob('/etc/hostname')",
        "SELECT * FROM read_csv('/etc/passwd')",
        "SELECT * FROM read_csv('https://attacker.example/x.csv')",
        "SELECT * FROM read_csv_auto('/data/warehouse.duckdb')",
        "SELECT content FROM read_text('/root/.ssh/id_rsa')",
        "SELECT * FROM glob('/etc/*')",
        "SELECT * FROM read_parquet('/data/parquet/*.parquet')",
        "SELECT * FROM read_json_auto('/etc/os-release')",
        "SELECT * FROM parquet_scan('/data/x.parquet')",
        "select * from READ_BLOB ('/etc/hostname')",  # case + space before (
    ],
)
def test_file_and_network_table_functions_rejected(sql):
    with pytest.raises(SQLGuardError):
        guard_sql(sql)


def test_columns_named_like_table_functions_are_allowed():
    # Only a call — name followed by ( — is rejected, not identifiers.
    assert is_safe_select("SELECT glob, read_text FROM t")


# --- rejected: keywords hidden in subclauses --------------------------------

def test_cte_wrapping_insert_rejected():
    with pytest.raises(SQLGuardError):
        guard_sql("WITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x")


# --- rejected: junk ----------------------------------------------------------

@pytest.mark.parametrize("sql", ["", "   ", None, "-- only a comment", "EXPLAIN SELECT 1"])
def test_junk_rejected(sql):
    with pytest.raises(SQLGuardError):
        guard_sql(sql)
