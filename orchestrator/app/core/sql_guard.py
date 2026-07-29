"""SQL guard for the sql engine (spec §8).

Accepts exactly ONE read-only statement: a single SELECT (WITH ... SELECT is
allowed). Rejects multi-statement input, every write/DDL/extension keyword
(INSERT / UPDATE / DELETE / ATTACH / COPY / PRAGMA / CREATE / DROP / ALTER /
INSTALL / LOAD / SET / CALL, and friends), and comment-smuggled variants such
as ``UPD/**/ATE`` or ``SELECT 1;--\\nDROP TABLE t``.

Also rejects DuckDB filesystem/network table functions (read_csv / read_blob /
read_text / glob / read_parquet / ...) which would otherwise let a plain
SELECT read arbitrary host files or reach the network. Defense in depth: the
engine additionally opens DuckDB with ``read_only=True`` and
``enable_external_access=false``.

Pure module: stdlib only.
"""
from __future__ import annotations

import re
from typing import Tuple


class SQLGuardError(ValueError):
    """Raised when a SQL string is rejected by the guard."""


# Write / DDL / extension / session keywords. Word-boundary matching means
# column names like `update_date` or `payload` do NOT false-positive; string
# literal contents are stripped before scanning.
_FORBIDDEN = re.compile(
    r"\b("
    r"insert|update|delete|attach|detach|copy|pragma|create|drop|alter|"
    r"install|load|set|call|export|import|truncate|vacuum|merge|grant|"
    r"revoke|checkpoint|use|begin|commit|rollback"
    r")\b",
    re.IGNORECASE,
)

_STARTS_OK = re.compile(r"^(select|with)\b", re.IGNORECASE)

# Defense in depth (§1/§12): DuckDB table functions that read the local
# filesystem or the network from inside a plain SELECT. The engine's primary
# protection is opening the connection with enable_external_access=false;
# this blocklist rejects such queries before they ever reach DuckDB.
_FORBIDDEN_TABLE_FUNCS = re.compile(
    r"\b("
    r"read_csv_auto|read_csv|read_parquet|parquet_scan|parquet_metadata|"
    r"parquet_schema|parquet_file_metadata|parquet_kv_metadata|"
    r"read_json_auto|read_json_objects_auto|read_json_objects|read_json|"
    r"read_ndjson_auto|read_ndjson_objects|read_ndjson|read_text|read_blob|"
    r"read_xlsx|glob|sniff_csv|delta_scan|iceberg_scan|st_read"
    r")\s*\(",
    re.IGNORECASE,
)


def _scan(sql: str) -> Tuple[str, str]:
    """Single-pass scanner.

    Returns:
        cleaned: comments replaced by a single space, strings/identifiers kept
                 (safe to execute).
        bare:    comments removed with NO separator (so split keywords like
                 ``UPD/**/ATE`` reassemble and get caught) AND the contents of
                 string literals / quoted identifiers removed (so 'DROP TABLE'
                 inside a string never false-positives).
    """
    cleaned: list[str] = []
    bare: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        # -- line comment (consume through end of line)
        if c == "-" and nxt == "-":
            j = sql.find("\n", i)
            i = n if j == -1 else j + 1
            cleaned.append(" ")
            continue
        # /* block comment */ (unterminated → consume to end)
        if c == "/" and nxt == "*":
            j = sql.find("*/", i + 2)
            i = n if j == -1 else j + 2
            cleaned.append(" ")
            continue
        # 'string literal' with '' escaping
        if c == "'":
            cleaned.append("'")
            bare.append("'")
            i += 1
            while i < n:
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        cleaned.append("''")
                        i += 2
                        continue
                    cleaned.append("'")
                    bare.append("'")
                    i += 1
                    break
                cleaned.append(sql[i])
                i += 1
            continue
        # "quoted identifier" with "" escaping — kept in cleaned, contents
        # stripped from bare (a column named "update_time" is not a threat).
        if c == '"':
            cleaned.append('"')
            bare.append('"')
            i += 1
            while i < n:
                if sql[i] == '"':
                    if i + 1 < n and sql[i + 1] == '"':
                        cleaned.append('""')
                        i += 2
                        continue
                    cleaned.append('"')
                    bare.append('"')
                    i += 1
                    break
                cleaned.append(sql[i])
                i += 1
            continue
        cleaned.append(c)
        bare.append(c)
        i += 1
    return "".join(cleaned), "".join(bare)


def guard_sql(sql: str) -> str:
    """Validate `sql` and return an executable, comment-stripped statement.

    Raises SQLGuardError when the input is anything other than exactly one
    SELECT (or WITH ... SELECT) statement.
    """
    if sql is None or not str(sql).strip():
        raise SQLGuardError("empty SQL statement")

    cleaned, bare = _scan(str(sql))
    core = bare.strip().rstrip(";").strip()
    if not core:
        raise SQLGuardError("empty SQL statement (comments only)")
    if ";" in core:
        raise SQLGuardError("multiple SQL statements are not allowed")
    if not _STARTS_OK.match(core):
        raise SQLGuardError("only a single SELECT (or WITH ... SELECT) statement is allowed")
    m = _FORBIDDEN.search(core)
    if m:
        raise SQLGuardError(f"forbidden SQL keyword: {m.group(1).upper()}")
    f = _FORBIDDEN_TABLE_FUNCS.search(core)
    if f:
        raise SQLGuardError(
            f"forbidden table function: {f.group(1).lower()} "
            "(filesystem/network access is not allowed)"
        )

    executable = cleaned.strip().rstrip(";").strip()
    return executable


def is_safe_select(sql: str) -> bool:
    """Convenience predicate wrapper around guard_sql()."""
    try:
        guard_sql(sql)
        return True
    except SQLGuardError:
        return False
