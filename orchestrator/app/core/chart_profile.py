"""Safe column profiling for chart selection.

This module answers "what shape is this result set?" from the values the
database returned, and produces ONLY aggregate metadata: column names,
inferred kind, counts, cardinality, numeric range, label lengths.

That distinction is the point. The chart-selection model — when it is
consulted at all — is shown a `ColumnProfile`, never a row. Salesforce
record values are data, not instructions: a Case subject reading "ignore
previous instructions and ..." can never reach a prompt through this path,
because no cell value leaves this module. `min`/`max` of a *numeric*
column are the only value-derived numbers that escape, and a number cannot
carry an instruction.

Pure module: stdlib only.
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import List, Optional, Sequence

#: Column kinds, in the order the inference falls through.
ColumnKind = str  # "numeric" | "date" | "boolean" | "identifier" | "categorical" | "text"

# Salesforce checkboxes arrive in DuckDB as the lowercase TEXT 'true'/'false'
# (the same quirk that makes `WHERE IsWon = 'True'` silently match nothing).
# They must never be treated as a numeric metric.
_BOOL_TOKENS = frozenset({"true", "false"})

# A Salesforce record Id is 15 or 18 case-sensitive alphanumerics. Charting
# one as a "category" produces 500 unreadable ticks; as a metric it is
# meaningless. Detect and exclude.
_SF_ID_RE = re.compile(r"^[a-zA-Z0-9]{15}(?:[a-zA-Z0-9]{3})?$")
_ID_NAME_RE = re.compile(r"(^|_)id$|Id$", re.I)

_DATE_RE = re.compile(
    r"^\d{4}-\d{2}(-\d{2})?"          # 2026-07 or 2026-07-29
    r"([T ]\d{2}:\d{2}(:\d{2})?)?"    # optional time
    r"(\.\d+)?(Z|[+-]\d{2}:?\d{2})?$" # optional fraction / offset
)
# Truncated period labels the SQL prompt tends to produce: 2026-Q3, 2026W31.
_PERIOD_RE = re.compile(r"^\d{4}[-/ ]?(Q[1-4]|W\d{1,2})$", re.I)

_TIME_NAME_RE = re.compile(
    r"date|day|week|month|quarter|year|period|created|closed|modified|stamp|_at$",
    re.I,
)

# Column names that carry a Salesforce stage/status semantic. Used only to
# *consider* a funnel; the trusted order still has to match (chart_decision).
_STAGE_NAME_RE = re.compile(r"stage|status|phase|step", re.I)


def _is_missing(v: object) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


#: "1,234" / "12,345,678.90" — separators every three digits, nothing else.
_THOUSANDS_RE = re.compile(r"-?\d{1,3}(,\d{3})+(\.\d+)?")


def _as_number(v: object) -> Optional[float]:
    """Return v as a float, or None. Booleans and bool-ish text are NOT numbers."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, Decimal):
        try:
            return float(v)
        except (InvalidOperation, ValueError):
            return None
    if isinstance(v, str):
        s = v.strip()
        if not s or s.lower() in _BOOL_TOKENS:
            return None
        # Salesforce currency and formatted numbers arrive as TEXT — the
        # warehouse stores every column as VARCHAR, and "$1,234.56" made an
        # Amount column profile as CATEGORICAL, so a revenue breakdown had "no
        # metric to plot" and never charted. Strict shape checks, not a
        # general de-formatter: "1,23" and "12,34,56" stay non-numeric.
        if s[:1] in "$€£₹":
            s = s[1:].strip()
        if _THOUSANDS_RE.fullmatch(s):
            s = s.replace(",", "")
        if s.endswith("%") and len(s) > 1:
            s = s[:-1].strip()
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _is_datelike(v: object) -> bool:
    if isinstance(v, (_dt.date, _dt.datetime)):
        return True
    if isinstance(v, str):
        s = v.strip()
        return bool(_DATE_RE.match(s) or _PERIOD_RE.match(s))
    return False


def _is_boolish(v: object) -> bool:
    if isinstance(v, bool):
        return True
    return isinstance(v, str) and v.strip().lower() in _BOOL_TOKENS


@dataclass
class ColumnProfile:
    """Aggregate metadata for one result column. Carries no cell values."""

    name: str
    kind: ColumnKind
    total: int = 0
    non_null: int = 0
    unique: int = 0
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    has_negative: bool = False
    max_label_len: int = 0
    #: True when every non-null value is distinct — a key, not a dimension.
    all_distinct: bool = False
    #: Set for `kind == "date"`: the ordered distinct labels are monotonic.
    monotonic: bool = False
    #: Name matches a date/period naming convention.
    time_named: bool = False
    #: Name matches a stage/status naming convention.
    stage_named: bool = False

    @property
    def is_numeric(self) -> bool:
        return self.kind == "numeric"

    @property
    def is_date(self) -> bool:
        return self.kind == "date"

    @property
    def is_categorical(self) -> bool:
        return self.kind in ("categorical", "boolean")

    @property
    def nulls(self) -> int:
        return self.total - self.non_null

    def to_prompt_dict(self) -> dict:
        """The ONLY shape allowed into a model prompt (no cell values)."""
        d = {
            "name": self.name,
            "kind": self.kind,
            "rows": self.total,
            "non_null": self.non_null,
            "distinct": self.unique,
        }
        if self.kind == "numeric":
            d["min"] = self.minimum
            d["max"] = self.maximum
            d["has_negative"] = self.has_negative
        else:
            d["max_label_len"] = self.max_label_len
        return d


def _column_values(rows: Sequence[Sequence], index: int) -> List[object]:
    out: List[object] = []
    for r in rows:
        try:
            out.append(r[index])
        except (IndexError, KeyError, TypeError):
            out.append(None)
    return out


def profile_column(name: str, values: Sequence[object]) -> ColumnProfile:
    total = len(values)
    present = [v for v in values if not _is_missing(v)]
    non_null = len(present)
    distinct = {str(v) for v in present}
    prof = ColumnProfile(
        name=name,
        kind="text",
        total=total,
        non_null=non_null,
        unique=len(distinct),
        max_label_len=max((len(str(v)) for v in present), default=0),
        all_distinct=non_null > 1 and len(distinct) == non_null,
        time_named=bool(_TIME_NAME_RE.search(name)),
        stage_named=bool(_STAGE_NAME_RE.search(name)),
    )
    if not present:
        prof.kind = "categorical"
        return prof

    if all(_is_boolish(v) for v in present):
        prof.kind = "boolean"
        return prof

    # Ids first: an 18-char Id is not a number, but a 15-char all-digit one
    # would parse as a float and become a bogus metric.
    id_named = bool(_ID_NAME_RE.search(name))
    if id_named and all(isinstance(v, str) and _SF_ID_RE.match(v.strip()) for v in present):
        prof.kind = "identifier"
        return prof

    if all(_is_datelike(v) for v in present):
        prof.kind = "date"
        labels = [str(v) for v in present]
        prof.monotonic = labels == sorted(labels)
        return prof

    numbers = [_as_number(v) for v in present]
    if all(n is not None for n in numbers):
        nums: List[float] = [n for n in numbers if n is not None]
        prof.kind = "identifier" if id_named else "numeric"
        prof.minimum = min(nums)
        prof.maximum = max(nums)
        prof.has_negative = any(n < 0 for n in nums)
        return prof

    # Free text vs. a dimension: a column where nearly every value is unique
    # and labels are long is prose, not a category axis.
    if prof.all_distinct and prof.max_label_len > 40:
        prof.kind = "text"
    else:
        prof.kind = "categorical"
    return prof


def profile_columns(
    columns: Sequence[str], rows: Sequence[Sequence]
) -> List[ColumnProfile]:
    """Profile every column of a (columns, rows) result set."""
    return [
        profile_column(name, _column_values(rows, i))
        for i, name in enumerate(columns)
    ]


def profile_index(profiles: Sequence[ColumnProfile]) -> dict:
    return {p.name: p for p in profiles}
