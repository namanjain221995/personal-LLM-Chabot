"""Time windows in words -> date ranges, worked out by code in the business time zone (pure Python).

``parse_period(text, today)`` returns a half-open :class:`Period` ``[start, end_exclusive)`` of local
dates (either side may be open). ``column_bounds`` turns it into values to bind: dates as they are
for DATE columns, local midnights converted to naive UTC for TIMESTAMP columns (the warehouse stores
UTC without a zone). Weeks start on Monday; quarters and years are calendar ones. SQL ``now()`` and
``current_date`` are never used.
"""

from __future__ import annotations

import calendar
import datetime as dt
import re
from dataclasses import dataclass
from zoneinfo import ZoneInfo

__all__ = ["Period", "PeriodError", "parse_period", "column_bounds", "bucket_start", "bucket_label",
           "bucket_sequence", "bucket_end", "today_in", "local_midnight_utc", "ACCEPTED_PERIODS"]

ACCEPTED_PERIODS = ("today, yesterday, tomorrow, this/last/next week|month|quarter|year, last N days|weeks|months|"
                    "quarters|years, next N days|weeks|months, year to date, 2026, Q2 2026, May 2026, 2026-05, "
                    "2026-05-03, since/after/before/until <date>, <date> to <date>, upcoming, past")

_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
                 "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "a": 1, "an": 1}
_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
_MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
_MONTHS["sept"] = 9
_WEEKDAYS = {d.lower() for d in calendar.day_name} | {d.lower() for d in calendar.day_abbr}
_UNITS = {"day": "day", "days": "day", "week": "week", "weeks": "week", "month": "month", "months": "month",
          "quarter": "quarter", "quarters": "quarter", "year": "year", "years": "year"}
_NO_LIMIT = {"currently", "now", "ever", "so far", "in total", "total", "all time", "overall", "to date", "all",
             "all records", "any time", "anytime", "right now", "at the moment", "as of now", "as of today"}

GRAIN_CAPS = {"day": 92, "week": 105, "month": 120, "quarter": 40, "year": 20}


class PeriodError(ValueError):
    """A period phrase could not be read; ``str(exc)`` is the full model-facing error (E5/E5b/E5d)."""

    def __init__(self, message: str, code: str = "E5") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Period:
    """Half-open local date range; ``None`` is an open side."""

    start: dt.date | None
    end_exclusive: dt.date | None
    label: str

    def contains_future(self, today: dt.date) -> bool:
        """True when the range runs past today."""
        return self.end_exclusive is None or self.end_exclusive > today + dt.timedelta(days=1)


def _e5(text: str) -> PeriodError:
    return PeriodError(f"cannot read period '{text}'. Use one of: {ACCEPTED_PERIODS}. Leave period out for all time.")


# ---------------------------------------------------------------------------------- calendar helpers

def _add_months(d: dt.date, n: int) -> dt.date:
    """First day of the month ``n`` months after ``d``'s month."""
    idx = d.year * 12 + (d.month - 1) + n
    return dt.date(idx // 12, idx % 12 + 1, 1)


def bucket_start(grain: str, d: dt.date) -> dt.date:
    """Start of the day/week/month/quarter/year that contains ``d``."""
    if grain == "day":
        return d
    if grain == "week":
        return d - dt.timedelta(days=d.weekday())
    if grain == "month":
        return d.replace(day=1)
    if grain == "quarter":
        return dt.date(d.year, 3 * ((d.month - 1) // 3) + 1, 1)
    if grain == "year":
        return dt.date(d.year, 1, 1)
    raise ValueError(f"unknown grain {grain}")


def bucket_end(grain: str, d: dt.date) -> dt.date:
    """Exclusive end of the bucket that contains ``d``."""
    s = bucket_start(grain, d)
    if grain == "day":
        return s + dt.timedelta(days=1)
    if grain == "week":
        return s + dt.timedelta(days=7)
    if grain == "month":
        return _add_months(s, 1)
    if grain == "quarter":
        return _add_months(s, 3)
    return dt.date(s.year + 1, 1, 1)


def _shift(grain: str, s: dt.date, n: int) -> dt.date:
    """Bucket start ``n`` buckets after the bucket starting at ``s``."""
    if grain == "day":
        return s + dt.timedelta(days=n)
    if grain == "week":
        return s + dt.timedelta(days=7 * n)
    if grain == "month":
        return _add_months(s, n)
    if grain == "quarter":
        return _add_months(s, 3 * n)
    return dt.date(s.year + n, 1, 1)


def bucket_label(grain: str, d: dt.date) -> str:
    """'2026-10-13' (day), 'week of 2026-10-12', '2026-05', '2026-Q2', '2026'."""
    s = bucket_start(grain, d)
    if grain == "day":
        return s.isoformat()
    if grain == "week":
        return f"week of {s.isoformat()}"
    if grain == "month":
        return s.strftime("%Y-%m")
    if grain == "quarter":
        return f"{s.year}-Q{(s.month - 1) // 3 + 1}"
    return str(s.year)


def bucket_sequence(grain: str, start: dt.date, end_exclusive: dt.date) -> list[dt.date]:
    """Bucket starts from the bucket of ``start`` up to (not including) ``end_exclusive``."""
    out: list[dt.date] = []
    cur = bucket_start(grain, start)
    while cur < end_exclusive:
        out.append(cur)
        cur = _shift(grain, cur, 1)
        if len(out) > 100_000:
            break
    return out


def today_in(tz: str) -> dt.date:
    """Today's date in time zone ``tz``."""
    return dt.datetime.now(ZoneInfo(tz)).date()


def local_midnight_utc(d: dt.date, tz: str) -> dt.datetime:
    """Local midnight of ``d`` in ``tz`` as a naive UTC datetime."""
    return dt.datetime.combine(d, dt.time(0), ZoneInfo(tz)).astimezone(dt.timezone.utc).replace(tzinfo=None)


def column_bounds(period: Period, kind: str, tz: str) -> tuple[object | None, object | None]:
    """Values to bind for ``c >= lo`` and ``c < hi`` (None = open side).

    ``kind`` is ``date`` (local dates as they are) or ``datetime`` (local midnights in naive UTC).
    """
    if kind == "datetime":
        lo = local_midnight_utc(period.start, tz) if period.start else None
        hi = local_midnight_utc(period.end_exclusive, tz) if period.end_exclusive else None
        return lo, hi
    return period.start, period.end_exclusive


# ---------------------------------------------------------------------------------- parsing

def _num(tok: str) -> int | None:
    if tok.isdigit():
        return int(tok)
    return _NUMBER_WORDS.get(tok)


def _year_range(y: int) -> tuple[dt.date, dt.date]:
    return dt.date(y, 1, 1), dt.date(y + 1, 1, 1)


def _parse_day(t: str) -> dt.date | None:
    """An unambiguous calendar day, or None."""
    m = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", t)
    if m:
        try:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)? ([a-z]+),? (\d{4})", t)
    if m and m.group(2) in _MONTHS:
        try:
            return dt.date(int(m.group(3)), _MONTHS[m.group(2)], int(m.group(1)))
        except ValueError:
            return None
    m = re.fullmatch(r"([a-z]+) (\d{1,2})(?:st|nd|rd|th)?,? (\d{4})", t)
    if m and m.group(1) in _MONTHS:
        try:
            return dt.date(int(m.group(3)), _MONTHS[m.group(1)], int(m.group(2)))
        except ValueError:
            return None
    return None


def _atom(t: str, today: dt.date, original: str) -> tuple[dt.date | None, dt.date | None] | None:
    """One closed range (or an open one for upcoming/past). None when ``t`` is not understood."""
    t = t.strip()
    for prefix in ("the ", "in the ", "in ", "during the ", "during ", "for the ", "for ", "of ", "on "):
        if t.startswith(prefix) and len(t) > len(prefix):
            got = _atom(t[len(prefix):], today, original)
            if got is not None:
                return got
    one = dt.timedelta(days=1)
    if t == "today":
        return today, today + one
    if t == "yesterday":
        return today - one, today
    if t == "tomorrow":
        return today + one, today + 2 * one
    if t in ("upcoming", "future", "in the future", "coming", "scheduled ahead"):
        return today, None
    if t in ("past", "in the past", "before today"):
        return None, today
    if t in ("year to date", "ytd"):
        return dt.date(today.year, 1, 1), today + one
    if t in ("month to date", "mtd"):
        return today.replace(day=1), today + one
    if t in ("quarter to date", "qtd"):
        return bucket_start("quarter", today), today + one
    if t in ("week to date", "wtd"):
        return bucket_start("week", today), today + one
    m = re.fullmatch(r"(this|current|last|previous|prior|past|next|coming) (day|week|month|quarter|year)", t)
    if m:
        grain = m.group(2)
        cur = bucket_start(grain, today)
        shift = {"this": 0, "current": 0, "last": -1, "previous": -1, "prior": -1, "past": -1,
                 "next": 1, "coming": 1}[m.group(1)]
        s = _shift(grain, cur, shift)
        return s, _shift(grain, s, 1)
    m = re.fullmatch(r"(last|past|previous|prior|next|coming) (\w+) (days?|weeks?|months?|quarters?|years?)", t)
    if m:
        n = _num(m.group(2))
        if n is None or n < 1:
            return None
        grain = _UNITS[m.group(3)]
        if m.group(1) in ("next", "coming"):
            if grain == "day":
                return today, today + dt.timedelta(days=n)
            first = _shift(grain, bucket_start(grain, today), 1)
            return first, _shift(grain, first, n)
        if grain == "day":
            return today - dt.timedelta(days=n - 1), today + one
        cur = bucket_start(grain, today)
        return _shift(grain, cur, -(n - 1)), _shift(grain, cur, 1)
    m = re.fullmatch(r"(?:fy ?|year )?(\d{4})", t)
    if m:
        return _year_range(int(m.group(1)))
    m = re.fullmatch(r"q([1-4])(?: of)?(?: (\d{4}))?", t) or re.fullmatch(r"(\d{4})[- ]?q([1-4])", t)
    if m:
        if t.startswith("q"):
            q, y = int(m.group(1)), int(m.group(2)) if m.group(2) else today.year
        else:
            y, q = int(m.group(1)), int(m.group(2))
        s = dt.date(y, 3 * (q - 1) + 1, 1)
        return s, _add_months(s, 3)
    m = re.fullmatch(r"(\d{4})-(\d{1,2})", t)
    if m and 1 <= int(m.group(2)) <= 12:
        s = dt.date(int(m.group(1)), int(m.group(2)), 1)
        return s, _add_months(s, 1)
    m = re.fullmatch(r"([a-z]+),? (\d{4})", t)
    if m and m.group(1) in _MONTHS:
        s = dt.date(int(m.group(2)), _MONTHS[m.group(1)], 1)
        return s, _add_months(s, 1)
    if t in _MONTHS:
        mo = _MONTHS[t]
        y = today.year if mo <= today.month else today.year - 1
        s = dt.date(y, mo, 1)
        return s, _add_months(s, 1)
    d = _parse_day(t)
    if d is not None:
        return d, d + one
    if re.fullmatch(r"\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}", t):
        raise PeriodError(f"'{t}' is ambiguous; write dates as YYYY-MM-DD.", "E5b")
    return None


def _clean(text: str) -> str:
    t = " ".join(str(text or "").lower().replace("–", " - ").replace("—", " - ").split())
    return t.strip(" .")


def parse_period(text: str, today: dt.date) -> Period:
    """Read a period phrase (see RECORDS_SPEC §11.5). Raises :class:`PeriodError`."""
    original = str(text or "").strip()
    t = _clean(original)
    if not t:
        raise _e5(original)
    if t in _NO_LIMIT:
        raise PeriodError(f"'{original}' means no time limit; leave period out.", "E5d")
    if any(w in _WEEKDAYS for w in re.findall(r"[a-z]+", t)):
        raise _e5(original)
    clip = False
    for suffix in (" so far", " to date", " till now", " until now", " until today"):
        if t.endswith(suffix) and t not in ("year to date", "month to date", "quarter to date", "week to date"):
            t = t[: -len(suffix)].strip()
            clip = True
            break
    start, end = _range(t, today, original)
    if clip:
        cap = today + dt.timedelta(days=1)
        if end is None or end > cap:
            end = cap
    if start is not None and end is not None and end <= start:
        raise PeriodError(f"period '{original}' is empty (it ends before it starts). Use one of: {ACCEPTED_PERIODS}.")
    return Period(start, end, original)


def _range(t: str, today: dt.date, original: str) -> tuple[dt.date | None, dt.date | None]:
    m = re.fullmatch(r"between (.+) and (.+)", t) or re.fullmatch(r"from (.+?) (?:to|until|till|through) (.+)", t) \
        or re.fullmatch(r"(.+?) (?:to|until|till|through) (.+)", t) or re.fullmatch(r"(.+?) - (.+)", t) \
        or re.fullmatch(r"(.+?) ?\.\. ?(.+)", t)
    if m and not t.startswith(("until ", "till ", "up to ")):
        a = _atom(m.group(1), today, original)
        b = _atom(m.group(2), today, original)
        if a is not None and b is not None and a[0] is not None and b[1] is not None:
            return a[0], b[1]
    for prefix, side in (("since ", "start"), ("from ", "start"), ("starting ", "start"), ("after ", "after"),
                         ("before ", "before"), ("until ", "until"), ("till ", "until"), ("up to ", "until"),
                         ("up until ", "until"), ("through ", "until"), ("on or after ", "start"),
                         ("on or before ", "until"), ("prior to ", "before")):
        if t.startswith(prefix):
            a = _atom(t[len(prefix):], today, original)
            if a is None or a[0] is None or a[1] is None:
                raise _e5(original)
            if side == "start":
                return a[0], None
            if side == "after":
                return a[1], None
            if side == "before":
                return None, a[0]
            return None, a[1]
    a = _atom(t, today, original)
    if a is None:
        raise _e5(original)
    return a
