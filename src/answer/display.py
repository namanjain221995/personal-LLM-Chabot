"""Raw column values -> values a person reads, before any model sees them.

Every conversion here is one the model must not be asked to make. A date it
reformats is a date it can get wrong; a null it interprets is a null it can
explain away. Doing it in code means the model repeats a string rather than
deciding what a value means.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any

UNAVAILABLE = "unavailable"

_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")

# t0, t1 ... and the sanitised aliases the planner builds out of business
# words. The model is shown business language, not the query's plumbing.
_ALIAS = re.compile(r"^t\d+$")


def label_for(column: str) -> str:
    """A column name as business words.

    The planner already names output columns after the business attribute the
    question used, sanitising anything that is not alphanumeric to an
    underscore. Undoing that is enough; nothing here invents a label.
    """
    if not column or _ALIAS.match(column):
        return column
    text = column
    for suffix in ("__c", "__r"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
    text = text.replace("_", " ").strip()
    return text or column


def display_date(value: date | datetime) -> str:
    return f"{_MONTHS[value.month - 1]} {value.day}, {value.year}"


def display_datetime(value: datetime) -> str:
    hour = value.hour % 12 or 12
    meridiem = "AM" if value.hour < 12 else "PM"
    return (f"{_MONTHS[value.month - 1]} {value.day}, {value.year} "
            f"at {hour}:{value.minute:02d} {meridiem}")


def normalise(value: Any) -> Any:
    """One column value, shaped for the model.

    A scalar stays a scalar so a number survives as a number and the validator
    can compare it arithmetically. Anything whose printed form differs from its
    stored form becomes {"raw", "display"}, so the model has a string to repeat
    and the trace still holds what the database actually returned.
    """
    if value is None:
        return {"raw": None, "display": UNAVAILABLE, "available": False}
    if isinstance(value, bool):
        return {"raw": value, "display": "Yes" if value else "No"}
    if isinstance(value, datetime):
        return {"raw": value.isoformat(), "display": display_datetime(value)}
    if isinstance(value, date):
        return {"raw": value.isoformat(), "display": display_date(value)}
    if isinstance(value, Decimal):
        number = float(value)
        return int(number) if number.is_integer() else number
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, (int, float, str)):
        return value
    return str(value)


def display_of(value: Any) -> str:
    """The string a person would read for an already-normalised value."""
    if isinstance(value, dict):
        return str(value.get("display", ""))
    if value is None:
        return UNAVAILABLE
    return str(value)


def normalise_row(row: dict[str, Any]) -> dict[str, Any]:
    """One result row, keyed by business label rather than column name."""
    out: dict[str, Any] = {}
    for column, value in row.items():
        out[label_for(column)] = normalise(value)
    return out
