"""Intent operators -> DuckDB SQL, and values -> bound parameters.

The mapping is a table, not a prompt. A model names an operator from a closed
set; this decides what that means in SQL. Nothing here ever builds a fragment
out of a user value -- values leave as parameters, always.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from .models import QueryError


class OperatorError(RuntimeError):
    def __init__(self, error: QueryError, detail: str) -> None:
        super().__init__(detail)
        self.error = error
        self.detail = detail


# intent operator -> (SQL operator, number of bound parameters)
OPERATORS: dict[str, tuple[str, int]] = {
    "equals": ("=", 1),
    "not_equals": ("!=", 1),
    "greater_than": (">", 1),
    "greater_than_or_equal": (">=", 1),
    "less_than": ("<", 1),
    "less_than_or_equal": ("<=", 1),
    "contains": ("LIKE", 1),
    "not_contains": ("NOT LIKE", 1),
    "starts_with": ("LIKE", 1),
    "ends_with": ("LIKE", 1),
    "in": ("IN", -1),           # -1: as many parameters as values
    "not_in": ("NOT IN", -1),
    "is_null": ("IS NULL", 0),
    "is_not_null": ("IS NOT NULL", 0),
    "between": ("BETWEEN", 2),
    "before": ("<", 1),
    "after": (">", 1),
    "on_or_before": ("<=", 1),
    "on_or_after": (">=", 1),
}

AGGREGATIONS = {"count", "sum", "avg", "min", "max"}

# Aggregations that need a number. COUNT/MIN/MAX work on anything.
NUMERIC_AGGREGATIONS = {"sum", "avg"}
NUMERIC_TYPES = {"BIGINT", "INTEGER", "DOUBLE", "DECIMAL", "FLOAT", "HUGEINT",
                 "SMALLINT", "TINYINT", "REAL", "NUMERIC"}

_LIKE_SPECIAL = ("\\", "%", "_")


def escape_like(value: str) -> str:
    """Neutralise LIKE wildcards inside a user's text.

    A search for "50%" must not silently become "starts with 50". The value is
    still bound, so this is about correctness, not injection.
    """
    out = str(value)
    for character in _LIKE_SPECIAL:
        out = out.replace(character, "\\" + character)
    return out


def sql_operator(operator: str) -> tuple[str, int]:
    key = (operator or "").strip().lower()
    if key not in OPERATORS:
        raise OperatorError(QueryError.UNSUPPORTED_OPERATOR,
                            f"operator {operator!r} is not supported")
    return OPERATORS[key]


def coerce(value: Any, duckdb_type: str) -> Any:
    """Shape a value for the column it is compared against.

    The `main` schema is typed, so a DATE column compared against the string
    '2026-09-26' would compare wrongly or not at all. Conversion happens here,
    once, rather than being hoped for at the driver.
    """
    if value is None:
        return None
    upper = (duckdb_type or "").upper()
    if upper.startswith("DATE") and not upper.startswith("DATETIME"):
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            return value
    if upper.startswith("TIMESTAMP"):
        if isinstance(value, datetime):
            return value
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return value
    if upper.startswith("BOOLEAN"):
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("true", "1", "yes", "y")
    if upper in NUMERIC_TYPES:
        if isinstance(value, (int, float)):
            return value
        try:
            text = str(value)
            return float(text) if "." in text else int(text)
        except ValueError:
            return value
    return value


def build_binds(operator: str, value: Any, duckdb_type: str) -> list[Any]:
    """The parameters one filter binds, already coerced."""
    key = (operator or "").strip().lower()
    _, arity = sql_operator(key)

    if arity == 0:
        return []
    if key in ("in", "not_in"):
        values = value if isinstance(value, (list, tuple, set)) else [value]
        if not values:
            raise OperatorError(QueryError.INVALID_QUERY_PLAN,
                                f"{key} needs at least one value")
        return [coerce(v, duckdb_type) for v in values]
    if key == "between":
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise OperatorError(QueryError.INVALID_QUERY_PLAN,
                                "between needs exactly two values")
        return [coerce(v, duckdb_type) for v in value]
    if key == "contains":
        return [f"%{escape_like(value)}%"]
    if key == "not_contains":
        return [f"%{escape_like(value)}%"]
    if key == "starts_with":
        return [f"{escape_like(value)}%"]
    if key == "ends_with":
        return [f"%{escape_like(value)}"]
    return [coerce(value, duckdb_type)]


def value_type_name(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, datetime):
        return "timestamp"
    if isinstance(value, date):
        return "date"
    if isinstance(value, (list, tuple, set)):
        return "list"
    return "string"


def check_aggregation(function: str, duckdb_type: str) -> str:
    key = (function or "").strip().lower()
    if key not in AGGREGATIONS:
        raise OperatorError(QueryError.UNSUPPORTED_AGGREGATION,
                            f"aggregation {function!r} is not supported")
    if key in NUMERIC_AGGREGATIONS:
        upper = (duckdb_type or "").upper().split("(")[0]
        if upper not in NUMERIC_TYPES:
            raise OperatorError(
                QueryError.UNSUPPORTED_AGGREGATION,
                f"{key.upper()} needs a numeric column, got {duckdb_type}")
    return key.upper()
