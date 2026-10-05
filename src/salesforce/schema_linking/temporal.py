"""A date expression as the user said it -> a half-open date range.

Code, not the model. "May 2026" is May 1 up to (not including) June 1, and
getting that wrong -- an off-by-one on a month end, a December that rolls into
the wrong year -- produces a count that is plausible and wrong. The model only
reports the words; this file does the calendar.

Half-open ranges throughout: `start <= date < end_exclusive`. That is the only
form that is correct for both DATE and TIMESTAMP columns -- an inclusive end of
"May 31" silently drops every record created on May 31 after midnight.

`today` is the server's local date. Salesforce stores DateTime in UTC, so a
record created at 23:30 local on the last day of a month may land in the next
month's UTC range. Stated rather than hidden; a per-org timezone belongs in
configuration once one is needed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

MONTHS = {name: index for index, name in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], start=1)}
MONTHS.update({name[:3]: index for name, index in list(MONTHS.items())})
MONTHS["sept"] = 9


@dataclass(frozen=True)
class DateRange:
    kind: str                 # day | week | month | quarter | year | days | open
    start: date | None        # None: unbounded before ("before May 2026")
    end_exclusive: date | None  # None: unbounded after ("upcoming")
    expression: str
    # The natural bucket for a trend over this range: "last six months" ->
    # month. None when the expression implies none.
    grain: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {"kind": self.kind,
                "start": self.start.isoformat() if self.start else None,
                "end_exclusive": self.end_exclusive.isoformat() if self.end_exclusive else None,
                "expression": self.expression, "grain": self.grain}


def _month(year: int, month: int) -> tuple[date, date]:
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start, end


def _week(day: date) -> tuple[date, date]:
    start = day - timedelta(days=day.weekday())      # Monday
    return start, start + timedelta(days=7)


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


NUMBER_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4,
                "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
                "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20,
                "thirty": 30, "sixty": 60, "ninety": 90}
UNIT_GRAIN = {"day": "day", "week": "week", "month": "month", "quarter": "quarter",
              "year": "year"}


def _number(token: str) -> int | None:
    if token.isdigit():
        return int(token)
    return NUMBER_WORDS.get(token)


def _add_months(day: date, months: int) -> date:
    year, month = _shift_month(day.year, day.month, months)
    return date(year, month, 1)


_LEAD = ("in the ", "during the ", "over the ", "within the ", "for the ",
         "in ", "during ", "over ", "within ", "for ")


def parse(expression: str | None, today: date | None = None) -> DateRange | None:
    # "in the last 30 days" -> "the last 30 days": the preposition is not
    # part of the period.
    if expression:
        text = str(expression).strip()
        lowered = text.lower()
        for lead in _LEAD:
            if lowered.startswith(lead) and len(text) > len(lead):
                expression = text[len(lead):]
                break
    """The range an expression names, or None when it names none.

    The ONE place dates are understood (§10). Intent extraction reports the
    words, schema linking picks the field, this decides the calendar.
    "latest", "earliest", "most recent" are orderings, not ranges, and return
    None here on purpose.
    """
    base = _parse_one(expression, today)
    if base is not None:
        return base
    return _parse_open(expression, today)


def _parse_open(expression: str | None, today: date | None) -> DateRange | None:
    """Open-ended ranges: before/after/since/until X, upcoming, past,
    older/newer than N days, between X and Y."""
    if not expression:
        return None
    today = today or date.today()
    text = " ".join(str(expression).lower().replace(",", " ").split())

    if text in ("upcoming", "future", "in the future", "from today", "from now on"):
        return DateRange("open", today, None, str(expression))
    if text in ("past", "in the past", "before today", "previously"):
        return DateRange("open", None, today, str(expression))

    older = re.fullmatch(r"(?:older than|more than)\s+(\w+)\s+(day|week|month|year)s?(?:\s+old|\s+ago)?", text)
    if older and _number(older.group(1)):
        n, unit = _number(older.group(1)), older.group(2)
        cutoff = _back(today, n, unit)
        return DateRange("open", None, cutoff, str(expression))
    newer = re.fullmatch(r"(?:newer than|less than|within)\s+(\w+)\s+(day|week|month|year)s?(?:\s+old)?", text)
    if newer and _number(newer.group(1)):
        n, unit = _number(newer.group(1)), newer.group(2)
        return DateRange("open", _back(today, n, unit), today + timedelta(days=1),
                         str(expression), grain=UNIT_GRAIN[unit])

    between = re.fullmatch(r"(?:between|from)\s+(.+?)\s+(?:and|to|until|till)\s+(.+)", text)
    if between:
        first, second = _parse_one(between.group(1), today), _parse_one(between.group(2), today)
        if first and second:
            return DateRange("open", first.start, second.end_exclusive, str(expression))
    bound = re.fullmatch(r"(before|until|till|prior to|after|since|on or after|on or before|on)\s+(.+)", text)
    if bound:
        inner = _parse_one(bound.group(2), today)
        if inner:
            word = bound.group(1)
            if word in ("before", "prior to"):
                return DateRange("open", None, inner.start, str(expression))
            if word in ("until", "till", "on or before"):
                return DateRange("open", None, inner.end_exclusive, str(expression))
            if word == "after":
                return DateRange("open", inner.end_exclusive, None, str(expression))
            if word in ("since", "on or after"):
                return DateRange("open", inner.start, None, str(expression))
            return inner                                   # "on <day>"
    return None


def _back(today: date, n: int, unit: str) -> date:
    if unit == "day":
        return today - timedelta(days=n)
    if unit == "week":
        return today - timedelta(weeks=n)
    if unit == "month":
        year, month = _shift_month(today.year, today.month, -n)
        day = min(today.day, 28)
        return date(year, month, day)
    return date(today.year - n, today.month, min(today.day, 28))


def _parse_one(expression: str | None, today: date | None = None) -> DateRange | None:
    """Closed ranges: a day, a week, a month, a quarter, a year, last/next N."""
    if not expression:
        return None
    text = " ".join(str(expression).lower().replace(",", " ").split())
    text = re.sub(r"^(in|on|during|for|of|the)\s+", "", text)
    text = re.sub(r"\s+(month|year)$", "", text) if re.search(r"\d{4}", text) else text
    today = today or date.today()

    def result(kind: str, start: date, end: date) -> DateRange:
        return DateRange(kind, start, end, str(expression))

    if text in ("today",):
        return result("day", today, today + timedelta(days=1))
    if text in ("yesterday",):
        return result("day", today - timedelta(days=1), today)
    if text in ("tomorrow",):
        return result("day", today + timedelta(days=1), today + timedelta(days=2))

    relative = re.fullmatch(r"(this|current|last|previous|past|next)\s+(week|month|year|quarter)", text)
    if relative is None:
        relative = re.fullmatch(r"(?:the\s+)?(this|current|last|previous|next)\s+(week|month|year|quarter)", text)
    if relative:
        which, unit = relative.groups()
        step = {"this": 0, "current": 0, "last": -1, "previous": -1,
                "past": -1, "next": 1}[which]
        if unit == "week":
            start, end = _week(today + timedelta(weeks=step))
            return result("week", start, end)
        if unit == "month":
            year, month = _shift_month(today.year, today.month, step)
            return result("month", *_month(year, month))
        if unit == "quarter":
            quarter = (today.month - 1) // 3
            year, month = _shift_month(today.year, quarter * 3 + 1, step * 3)
            start = date(year, month, 1)
            ey, em = _shift_month(year, month, 3)
            return result("quarter", start, date(ey, em, 1))
        year = today.year + step
        return result("year", date(year, 1, 1), date(year + 1, 1, 1))

    # last/past/previous/next N days|weeks|months|quarters|years, with digits
    # or number words ("last six months"). Whole calendar units for months and
    # longer: "the last six months" asked on Sept 29 is April 1 to Sept 30,
    # the six months a monthly trend shows as six buckets.
    span = re.fullmatch(r"(last|past|previous|next|coming)\s+(\w+)\s+(day|week|month|quarter|year)s?", text)
    if span and _number(span.group(2)):
        n, unit = _number(span.group(2)), span.group(3)
        forward = span.group(1) in ("next", "coming")
        grain = UNIT_GRAIN[unit]
        if unit == "day":
            if forward:
                return DateRange("days", today + timedelta(days=1),
                                 today + timedelta(days=n + 1), str(expression), grain)
            return DateRange("days", today - timedelta(days=n - 1),
                             today + timedelta(days=1), str(expression), grain)
        if unit == "week":
            this_start, _ = _week(today)
            if forward:
                return DateRange("weeks", this_start + timedelta(weeks=1),
                                 this_start + timedelta(weeks=n + 1), str(expression), grain)
            return DateRange("weeks", this_start - timedelta(weeks=n - 1),
                             this_start + timedelta(weeks=1), str(expression), grain)
        months = {"month": 1, "quarter": 3, "year": 12}[unit] * n
        this_month = date(today.year, today.month, 1)
        if forward:
            start = _add_months(this_month, 1)
            return DateRange("months", start, _add_months(start, months),
                             str(expression), grain)
        return DateRange("months", _add_months(this_month, -(months - 1)),
                         _add_months(this_month, 1), str(expression), grain)

    iso_day = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", text)
    if iso_day:
        try:
            day = date(*map(int, iso_day.groups()))
        except ValueError:
            return None
        return result("day", day, day + timedelta(days=1))

    iso_month = re.fullmatch(r"(\d{4})-(\d{2})", text)
    if iso_month:
        year, month = map(int, iso_month.groups())
        if 1 <= month <= 12:
            return result("month", *_month(year, month))
        return None

    quarter = re.fullmatch(r"q([1-4])\s+(\d{4})", text)
    if quarter:
        q, year = int(quarter.group(1)), int(quarter.group(2))
        start = date(year, (q - 1) * 3 + 1, 1)
        ey, em = _shift_month(year, start.month, 3)
        return result("quarter", start, date(ey, em, 1))

    month_year = re.fullmatch(r"([a-z]+)\s+(\d{4})", text)
    if month_year and month_year.group(1) in MONTHS:
        return result("month", *_month(int(month_year.group(2)),
                                       MONTHS[month_year.group(1)]))

    day_month_year = re.fullmatch(r"(\d{1,2})\s+([a-z]+)\s+(\d{4})|([a-z]+)\s+(\d{1,2})\s+(\d{4})", text)
    if day_month_year:
        groups = day_month_year.groups()
        if groups[0]:
            day, name, year = int(groups[0]), groups[1], int(groups[2])
        else:
            name, day, year = groups[3], int(groups[4]), int(groups[5])
        if name in MONTHS:
            try:
                start = date(year, MONTHS[name], day)
            except ValueError:
                return None
            return result("day", start, start + timedelta(days=1))

    month_only = re.fullmatch(r"([a-z]+)", text)
    if month_only and month_only.group(1) in MONTHS:
        # A bare month means the most recent one: "May" asked in September is
        # this year's May, asked in February it is last year's.
        month = MONTHS[month_only.group(1)]
        year = today.year if month <= today.month else today.year - 1
        return result("month", *_month(year, month))

    year_only = re.fullmatch(r"(\d{4})", text)
    if year_only:
        year = int(year_only.group(1))
        return result("year", date(year, 1, 1), date(year + 1, 1, 1))

    return None


# -- structured periods (the planning stage's temporal semantics, §16) --------
PERIOD_KINDS = ("relative", "last_n", "next_n", "older_than", "within_last", "calendar",
                "between", "before", "after", "since", "until", "upcoming", "past")
_UNITS = ("day", "week", "month", "quarter", "year")


def from_semantics(period: Any, today: date | None = None,
                   expression: str | None = None) -> DateRange | None:
    """The model's structured reading of WHEN -> a half-open range, by code.

    The model says what kind of period it means ({"kind": "relative", "unit":
    "month", "offset": -1}); the calendar is done here, so no month end, leap
    year or year boundary depends on the model's arithmetic. Unknown shapes
    return None -- the caller fails the plan rather than guessing.
    """
    if not isinstance(period, dict):
        return None
    today = today or date.today()
    kind = str(period.get("kind") or "").lower()
    unit = str(period.get("unit") or "").lower().rstrip("s")
    text = expression or describe(period)

    def n_of() -> int | None:
        value = period.get("n")
        return int(value) if isinstance(value, (int, float)) and 0 < value <= 10000 else None

    if kind == "relative" and unit in _UNITS:
        offset = period.get("offset", 0)
        offset = int(offset) if isinstance(offset, (int, float)) else 0
        if unit == "day":
            start = today + timedelta(days=offset)
            return DateRange("day", start, start + timedelta(days=1), text)
        if unit == "week":
            start, end = _week(today + timedelta(weeks=offset))
            return DateRange("week", start, end, text)
        if unit == "month":
            year, month = _shift_month(today.year, today.month, offset)
            return DateRange("month", *_month(year, month), text)
        if unit == "quarter":
            quarter = (today.month - 1) // 3
            year, month = _shift_month(today.year, quarter * 3 + 1, offset * 3)
            ey, em = _shift_month(year, month, 3)
            return DateRange("quarter", date(year, month, 1), date(ey, em, 1), text)
        return DateRange("year", date(today.year + offset, 1, 1),
                         date(today.year + offset + 1, 1, 1), text)
    if kind in ("last_n", "next_n") and unit in _UNITS and n_of():
        word = "last" if kind == "last_n" else "next"
        found = _parse_one(f"{word} {n_of()} {unit}s", today)
        return _renamed(found, text)
    if kind in ("older_than", "within_last") and unit in ("day", "week", "month", "year") \
            and n_of():
        word = "older than" if kind == "older_than" else "within"
        return _renamed(_parse_open(f"{word} {n_of()} {unit}s", today), text)
    if kind == "calendar":
        year = period.get("year")
        if not isinstance(year, int) or not 1900 <= year <= 2200:
            return None
        month, quarter, day = period.get("month"), period.get("quarter"), period.get("day")
        try:
            if isinstance(month, int) and isinstance(day, int):
                start = date(year, month, day)
                return DateRange("day", start, start + timedelta(days=1), text)
            if isinstance(month, int) and 1 <= month <= 12:
                return DateRange("month", *_month(year, month), text)
            if isinstance(quarter, int) and 1 <= quarter <= 4:
                start = date(year, (quarter - 1) * 3 + 1, 1)
                ey, em = _shift_month(year, start.month, 3)
                return DateRange("quarter", start, date(ey, em, 1), text)
        except ValueError:
            return None
        return DateRange("year", date(year, 1, 1), date(year + 1, 1, 1), text)
    if kind == "between":
        first = from_semantics(period.get("start"), today)
        second = from_semantics(period.get("end"), today)
        if first and second:
            return DateRange("open", first.start, second.end_exclusive, text)
        return None
    if kind in ("before", "after", "since", "until"):
        anchor = from_semantics(period.get("anchor"), today)
        if anchor is None:
            return None
        if kind == "before":
            return DateRange("open", None, anchor.start, text)
        if kind == "until":
            return DateRange("open", None, anchor.end_exclusive, text)
        if kind == "after":
            return DateRange("open", anchor.end_exclusive, None, text)
        return DateRange("open", anchor.start, None, text)
    if kind == "upcoming":
        return DateRange("open", today, None, text)
    if kind == "past":
        return DateRange("open", None, today, text)
    return None


def _renamed(found: DateRange | None, text: str) -> DateRange | None:
    if found is None:
        return None
    return DateRange(found.kind, found.start, found.end_exclusive, text, found.grain)


def describe(period: Any) -> str:
    """Words for a structured period, for traces and the answer's own wording."""
    if not isinstance(period, dict):
        return ""
    kind = str(period.get("kind") or "")
    unit = str(period.get("unit") or "").rstrip("s")
    if kind == "relative":
        offset = period.get("offset", 0)
        if unit == "day":
            return {0: "today", -1: "yesterday", 1: "tomorrow"}.get(
                offset, f"{abs(offset)} days {'ago' if offset < 0 else 'from now'}")
        return {0: f"this {unit}", -1: f"last {unit}", 1: f"next {unit}"}.get(
            offset, f"{abs(offset)} {unit}s {'ago' if offset < 0 else 'from now'}")
    if kind in ("last_n", "next_n"):
        return f"{'last' if kind == 'last_n' else 'next'} {period.get('n')} {unit}s"
    if kind == "older_than":
        return f"older than {period.get('n')} {unit}s"
    if kind == "within_last":
        return f"within the last {period.get('n')} {unit}s"
    if kind == "calendar":
        parts = [str(period.get("year") or "")]
        if period.get("quarter"):
            parts.insert(0, f"Q{period['quarter']}")
        if isinstance(period.get("month"), int) and 1 <= period["month"] <= 12:
            import calendar
            parts.insert(0, calendar.month_name[period["month"]])
        if period.get("day"):
            parts.insert(0, str(period["day"]))
        return " ".join(p for p in parts if p)
    if kind == "between":
        return f"between {describe(period.get('start'))} and {describe(period.get('end'))}"
    if kind in ("before", "after", "since", "until"):
        return f"{kind} {describe(period.get('anchor'))}"
    return kind
