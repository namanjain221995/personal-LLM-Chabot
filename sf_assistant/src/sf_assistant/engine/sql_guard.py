"""SQL guard: every statement the record tools build is parsed by DuckDB and checked before it runs.

``check_sql`` accepts exactly one SELECT whose tables are all in the allowlist, with no table
functions, no ``*``, no positional (``#n``) or whole-row references (a table or alias used as a
column, which DuckDB turns into a STRUCT of every column), no catalog-qualified tables, no blocked
columns and no file/meta functions. A failure is a builder bug (the model never writes SQL); tools
show the model error E12.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = ["GuardError", "quote_ident", "check_sql", "MAX_SQL_CHARS"]

MAX_SQL_CHARS = 20_000

# Scalar functions that could reach files, settings or other statements (table functions are
# rejected separately, whatever their name).
_DENIED_FUNCTIONS = frozenset({
    "query", "query_table", "glob", "getenv", "current_setting", "sniff_csv", "delta_scan",
    "load_extension", "install_extension", "read_text", "read_blob",
})
_DENIED_PREFIXES = ("read_", "duckdb_", "pragma_", "parquet_", "iceberg_", "sqlite_", "postgres_", "mysql_")


class GuardError(Exception):
    """A statement failed the guard; ``str(exc)`` is the developer detail."""


def quote_ident(name: str) -> str:
    """Double-quote an identifier taken from the catalog; refuses quotes and NUL."""
    s = str(name)
    if not s or '"' in s or "\x00" in s:
        raise GuardError(f"identifier {s!r} cannot be quoted safely")
    return f'"{s}"'


def _has_semicolon(sql: str) -> bool:
    """True when ``;`` appears outside string literals and quoted identifiers."""
    quote: str | None = None
    for ch in sql:
        if quote:
            if ch == quote:
                quote = None  # a doubled quote re-opens on the next character, which is harmless
        elif ch in ("'", '"'):
            quote = ch
        elif ch == ";":
            return True
    return False


def _walk(node: Any):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _relations(statement: dict) -> tuple[dict[str, set[str]], set[str]]:
    """``{alias_or_table_lower: {table_lower, ...}}`` of every FROM item, and every base table (lower)."""
    names: dict[str, set[str]] = {}
    tables: set[str] = set()
    for n in _walk(statement):
        typ = n.get("type")
        if typ == "BASE_TABLE":
            table = str(n.get("table_name") or "").lower()
            tables.add(table)
            names.setdefault(table, set()).add(table)
            if n.get("alias"):
                names.setdefault(str(n["alias"]).lower(), set()).add(table)
        elif typ == "SUBQUERY" and n.get("alias"):
            names.setdefault(str(n["alias"]).lower(), set())
    return names, tables


def check_sql(cur: Any, sql: str, allowed: frozenset[tuple[str, str]], blocked_columns: frozenset[str],
              blocked_fields: frozenset[tuple[str, str]] = frozenset()) -> None:
    """Raise :class:`GuardError` unless ``sql`` is one safe SELECT over allowed tables.

    Args:
        cur: a DuckDB cursor (used only for ``json_serialize_sql``).
        sql: the statement text (parameters stay ``?``).
        allowed: ``{(schema_lower, table_name)}`` that may be read.
        blocked_columns: lower-cased column names that must never be referenced (in any table).
        blocked_fields: lower-cased ``(table, column)`` pairs that must never be referenced, e.g. the
            text fields of a sensitive object. An unqualified column counts for every table of the statement.
    """
    if not isinstance(sql, str) or not sql.strip():
        raise GuardError("empty statement")
    if len(sql) > MAX_SQL_CHARS:
        raise GuardError(f"statement longer than {MAX_SQL_CHARS} characters")
    if _has_semicolon(sql):
        raise GuardError("';' outside a string literal")
    try:
        j = json.loads(cur.execute("SELECT json_serialize_sql(?)", [sql]).fetchone()[0])
    except Exception as exc:  # parser crash or unexpected output
        raise GuardError(f"could not parse the statement: {type(exc).__name__}: {str(exc)[:200]}") from None
    if j.get("error") is not False:
        raise GuardError(f"not a plain SELECT: {str(j.get('error_message') or j.get('error_type') or 'parse error')[:200]}")
    statements = j.get("statements") or []
    if len(statements) != 1:
        raise GuardError(f"expected exactly 1 statement, got {len(statements)}")
    node = statements[0].get("node") or {}
    if node.get("type") != "SELECT_NODE":
        raise GuardError(f"top node is {node.get('type')}, not SELECT_NODE")
    relations, tables = _relations(statements[0])
    for n in _walk(statements[0]):
        typ = n.get("type")
        cls = n.get("class")
        if typ == "BASE_TABLE":
            schema = str(n.get("schema_name") or "").lower()
            table = str(n.get("table_name") or "")
            if n.get("catalog_name"):
                raise GuardError(f"catalog-qualified table {n.get('catalog_name')}.{schema}.{table}")
            if (schema, table) not in allowed:
                raise GuardError(f"table {schema or '<none>'}.{table} is not allowed")
        elif typ == "TABLE_FUNCTION":
            raise GuardError("table functions are not allowed")
        if cls == "STAR" or typ == "STAR":
            raise GuardError("* is not allowed")
        if cls == "POSITIONAL_REFERENCE" or typ == "POSITIONAL_REFERENCE":
            raise GuardError("positional column references (#n) are not allowed")
        if cls == "COLUMN_REF" or typ == "COLUMN_REF":
            names = [str(x) for x in (n.get("column_names") or [])]
            if not names:
                continue
            last = names[-1].lower()
            qualifier = names[-2].lower() if len(names) > 1 else None
            if last in relations and (qualifier is None or qualifier not in relations):
                raise GuardError(f"whole-row reference {'.'.join(names)} is not allowed; name the columns")
            if last in blocked_columns:
                raise GuardError(f"column {names[-1]} is blocked")
            owners = tables if qualifier is None else relations.get(qualifier, set())
            if any((t, last) in blocked_fields for t in owners):
                raise GuardError(f"column {names[-1]} is blocked")
        if cls == "FUNCTION" or typ == "FUNCTION":
            fname = str(n.get("function_name") or "").lower()
            if fname in _DENIED_FUNCTIONS or fname.startswith(_DENIED_PREFIXES):
                raise GuardError(f"function {fname} is not allowed")
