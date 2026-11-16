"""sf_assistant.engine.periods: the period grammar, column bounds and trend buckets (pure Python, today = 2026-10-13)."""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

from sf_assistant.engine.periods import (Period, PeriodError, bucket_label, bucket_sequence, bucket_start,  # noqa: E402
                           column_bounds, parse_period)

TODAY = dt.date(2026, 10, 13)  # a Tuesday
D = dt.date

GRAMMAR = [
    ("today", D(2026, 10, 13), D(2026, 10, 14)),
    ("yesterday", D(2026, 10, 12), D(2026, 10, 13)),
    ("tomorrow", D(2026, 10, 14), D(2026, 10, 15)),
    ("this week", D(2026, 10, 12), D(2026, 10, 19)),
    ("last week", D(2026, 10, 5), D(2026, 10, 12)),
    ("next week", D(2026, 10, 19), D(2026, 10, 26)),
    ("this month", D(2026, 10, 1), D(2026, 11, 1)),
    ("last month", D(2026, 9, 1), D(2026, 10, 1)),
    ("next month", D(2026, 11, 1), D(2026, 12, 1)),
    ("this quarter", D(2026, 10, 1), D(2027, 1, 1)),
    ("last quarter", D(2026, 7, 1), D(2026, 10, 1)),
    ("next quarter", D(2027, 1, 1), D(2027, 4, 1)),
    ("this year", D(2026, 1, 1), D(2027, 1, 1)),
    ("last year", D(2025, 1, 1), D(2026, 1, 1)),
    ("next year", D(2027, 1, 1), D(2028, 1, 1)),
    ("last 7 days", D(2026, 10, 7), D(2026, 10, 14)),
    ("last 6 months", D(2026, 5, 1), D(2026, 11, 1)),
    ("last 3 weeks", D(2026, 9, 28), D(2026, 10, 19)),
    ("last 2 quarters", D(2026, 7, 1), D(2027, 1, 1)),
    ("last 2 years", D(2025, 1, 1), D(2027, 1, 1)),
    ("past 6 months", D(2026, 5, 1), D(2026, 11, 1)),
    ("next 3 days", D(2026, 10, 13), D(2026, 10, 16)),
    ("next 2 weeks", D(2026, 10, 19), D(2026, 11, 2)),
    ("next 2 months", D(2026, 11, 1), D(2027, 1, 1)),
    ("next 1 quarter", D(2027, 1, 1), D(2027, 4, 1)),
    ("year to date", D(2026, 1, 1), D(2026, 10, 14)),
    ("month to date", D(2026, 10, 1), D(2026, 10, 14)),
    ("2026", D(2026, 1, 1), D(2027, 1, 1)),
    ("Q2 2026", D(2026, 4, 1), D(2026, 7, 1)),
    ("Q2", D(2026, 4, 1), D(2026, 7, 1)),
    ("May 2026", D(2026, 5, 1), D(2026, 6, 1)),
    ("2026-05", D(2026, 5, 1), D(2026, 6, 1)),
    ("2026-05-03", D(2026, 5, 3), D(2026, 5, 4)),
    ("3 May 2026", D(2026, 5, 3), D(2026, 5, 4)),
    ("May 3 2026", D(2026, 5, 3), D(2026, 5, 4)),
    ("May 3, 2026", D(2026, 5, 3), D(2026, 5, 4)),
    ("since 2026-08-01", D(2026, 8, 1), None),
    ("from 2026-08-01", D(2026, 8, 1), None),
    ("after 2026-06-30", D(2026, 7, 1), None),
    ("before 2026-01-01", None, D(2026, 1, 1)),
    ("until 2026-03-31", None, D(2026, 4, 1)),
    ("up to 2026-03-31", None, D(2026, 4, 1)),
    ("2026-06-01 to 2026-06-30", D(2026, 6, 1), D(2026, 7, 1)),
    ("2026-06-01 - 2026-06-30", D(2026, 6, 1), D(2026, 7, 1)),
    ("between 2026-06-01 and 2026-06-30", D(2026, 6, 1), D(2026, 7, 1)),
    ("from 2026-06-01 to 2026-06-30", D(2026, 6, 1), D(2026, 7, 1)),
    ("2026-06-01..2026-06-30", D(2026, 6, 1), D(2026, 7, 1)),
    ("on 2026-05-03", D(2026, 5, 3), D(2026, 5, 4)),
    ("upcoming", D(2026, 10, 13), None),
    ("future", D(2026, 10, 13), None),
    ("past", None, D(2026, 10, 13)),
    ("  This Month  ", D(2026, 10, 1), D(2026, 11, 1)),
]


@pytest.mark.parametrize("phrase,start,end", GRAMMAR)
def test_grammar(phrase, start, end):
    p = parse_period(phrase, TODAY)
    assert (p.start, p.end_exclusive) == (start, end)
    assert p.label == phrase.strip()


@pytest.mark.parametrize("phrase,start,end", [
    ("last six months", D(2026, 5, 1), D(2026, 11, 1)),
    ("last twelve months", D(2025, 11, 1), D(2026, 11, 1)),
    ("past three weeks", D(2026, 9, 28), D(2026, 10, 19)),
])
def test_number_words(phrase, start, end):
    p = parse_period(phrase, TODAY)
    assert (p.start, p.end_exclusive) == (start, end)


def test_bare_month_is_the_most_recent_one_not_after_this_month():
    assert parse_period("May", TODAY).start == D(2026, 5, 1)
    assert parse_period("October", TODAY).start == D(2026, 10, 1)
    assert parse_period("November", TODAY).start == D(2025, 11, 1)


def test_so_far_clips_to_today():
    assert parse_period("this year so far", TODAY).end_exclusive == D(2026, 10, 14)
    assert parse_period("this month to date", TODAY).end_exclusive == D(2026, 10, 14)
    assert parse_period("last 6 months so far", TODAY).end_exclusive == D(2026, 10, 14)
    assert parse_period("last month so far", TODAY).end_exclusive == D(2026, 10, 1)  # already before today


@pytest.mark.parametrize("phrase,code", [
    ("03-07-2026", "E5b"), ("3/7/2026", "E5b"),
    ("so far", "E5d"), ("currently", "E5d"), ("now", "E5d"), ("ever", "E5d"), ("in total", "E5d"),
    ("all time", "E5d"), ("overall", "E5d"),
    ("last Monday", "E5"), ("banana", "E5"), ("", "E5"),
])
def test_rejected(phrase, code):
    with pytest.raises(PeriodError) as exc:
        parse_period(phrase, TODAY)
    assert exc.value.code == code
    if code == "E5":
        assert "Use one of" in str(exc.value) and "last N days" in str(exc.value)
    if code == "E5b":
        assert "YYYY-MM-DD" in str(exc.value)
    if code == "E5d":
        assert "leave period out" in str(exc.value)


def test_timestamp_bounds_convert_local_midnight_to_utc():
    p = Period(D(2026, 10, 1), D(2026, 11, 1), "this month")
    lo, hi = column_bounds(p, "datetime", "America/New_York")
    assert lo == dt.datetime(2026, 10, 1, 4, 0)
    assert hi == dt.datetime(2026, 11, 1, 4, 0)
    lo, _ = column_bounds(p, "datetime", "Asia/Kolkata")
    assert lo == dt.datetime(2026, 9, 30, 18, 30)
    assert column_bounds(p, "date", "America/New_York") == (D(2026, 10, 1), D(2026, 11, 1))
    assert column_bounds(Period(None, D(2026, 1, 1), "x"), "date", "UTC") == (None, D(2026, 1, 1))


def test_bucket_labels_and_sequences():
    d = D(2026, 10, 13)
    assert bucket_label("day", d) == "2026-10-13"
    assert bucket_label("week", d) == "week of 2026-10-12"
    assert bucket_label("month", d) == "2026-10"
    assert bucket_label("quarter", d) == "2026-Q4"
    assert bucket_label("year", d) == "2026"
    assert bucket_start("week", d) == D(2026, 10, 12)
    seq = bucket_sequence("month", D(2026, 5, 1), D(2026, 11, 1))
    assert [bucket_label("month", s) for s in seq] == ["2026-05", "2026-06", "2026-07", "2026-08", "2026-09", "2026-10"]
    assert len(bucket_sequence("day", D(2025, 1, 1), D(2027, 1, 1))) == 730
    assert [bucket_label("quarter", s) for s in bucket_sequence("quarter", D(2026, 1, 1), D(2027, 1, 1))] == \
        ["2026-Q1", "2026-Q2", "2026-Q3", "2026-Q4"]
