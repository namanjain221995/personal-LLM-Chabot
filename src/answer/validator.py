"""Does the answer say anything the result does not support?

The method is subtraction. Every supported value is removed from the answer's
text, longest first; whatever still looks like a fact in what remains was not
in the context. That is stricter than checking claims one at a time and it has
no list of phrasings to keep up to date -- a model that invents a name in a
sentence nobody anticipated still fails.

Every check errs toward rejecting a valid answer rather than accepting an
invented one. A rejected answer costs one regeneration; an accepted invention
costs a user acting on a record that does not exist.
"""
from __future__ import annotations

import re
from typing import Any

from .models import (GroundedAnswer, GroundingCode, GroundingReport,
                     InterpretedResult, ResultType)

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_DATE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}\b"
    r"|\b\d{1,2}/\d{1,2}/\d{2,4}\b"
    r"|\b(?:January|February|March|April|May|June|July|August|September"
    r"|October|November|December)\s+\d{1,2},?\s+\d{4}\b")
# Two or more capitalised words in a row: how a person or a record reads.
_PROPER_NOUN = re.compile(r"\b[A-Z][\w'-]+(?:\s+[A-Z][\w'-]+)+")

# Wording that asserts a total rather than a page of one.
_TOTAL_CLAIM = re.compile(
    r"\b(there (?:are|were)|a total of|in total|total of|exactly|all "
    r"\d+|only \d+)\b", re.IGNORECASE)
# Wording that describes the replica as live Salesforce.
_LIVE_CLAIM = re.compile(
    r"\b(salesforce (?:currently|now|presently)|live salesforce"
    r"|currently in salesforce|in salesforce right now|real[- ]time)\b",
    re.IGNORECASE)
# Wording that offers a reason. An empty result establishes a count, never a
# cause, so any of these in an empty answer is an invention.
_CAUSAL = re.compile(
    r"\b(because|due to|owing to|the reason|reasons?(?: for| why)"
    r"|likely (?:because|due)|probably|presumably|this (?:is|may be) "
    r"(?:because|due)|as (?:a|the) result of|caused by|since (?:there|no|all))"
    r"\b", re.IGNORECASE)

# Words that begin many sentences and would otherwise read as proper nouns.
_SENTENCE_OPENERS = {
    "showing", "data", "no", "there", "the", "additional", "matching",
    "further", "results", "result", "records", "record", "none", "this",
    "these", "assigned", "unassigned", "total", "however", "note", "only",
}


def _numbers_in(text: str) -> list[float]:
    out: list[float] = []
    for match in _NUMBER.finditer(text):
        try:
            out.append(float(match.group().replace(",", "")))
        except ValueError:
            continue
    return out


def _supported_number(value: float, supported: set[float]) -> bool:
    for candidate in supported:
        if abs(candidate - value) < 1e-9:
            return True
        # A note may round "17.4 minutes" to "17"; it may not round 8 to 9.
        if abs(round(candidate) - value) < 1e-9 and abs(candidate - value) < 1.0:
            return True
        if abs(round(candidate, 2) - value) < 1e-9:
            return True
    return False


def _strip_supported(text: str, supported: set[str]) -> str:
    """Remove every supported value, longest first.

    Longest first because "John" is a substring of "John Smith": removing the
    short one leaves "Smith" behind and the answer fails for a name it
    actually had.
    """
    remainder = text
    for value in sorted(supported, key=len, reverse=True):
        if len(value) < 2:
            continue
        remainder = re.sub(re.escape(value), " ", remainder,
                           flags=re.IGNORECASE)
    return remainder


def validate_grounded_answer(answer: GroundedAnswer,
                             interpreted: InterpretedResult,
                             ) -> GroundingReport:
    report = GroundingReport()
    texts = answer.texts()
    whole = " ".join(texts)
    if not whole.strip():
        report.add(GroundingCode.UNSUPPORTED_RECORD, "the answer is empty")
        return report

    remainder = _strip_supported(whole, interpreted.supported_strings)

    _check_numbers(remainder, interpreted, report)
    _check_emails(remainder, report)
    _check_dates(remainder, report)
    _check_records(remainder, report)
    _check_record_count(answer, interpreted, report)
    _check_truncation(whole, interpreted, report)
    _check_freshness(answer, interpreted, report)
    _check_live_claim(whole, report)
    _check_empty_explained(whole, interpreted, report)
    return report


def _check_numbers(remainder: str, interpreted: InterpretedResult,
                   report: GroundingReport) -> None:
    for value in _numbers_in(remainder):
        if not _supported_number(value, interpreted.supported_numbers):
            report.add(GroundingCode.UNSUPPORTED_NUMBER,
                       f"{_render(value)} is not a value in the result",
                       _render(value))


def _check_emails(remainder: str, report: GroundingReport) -> None:
    for email in _EMAIL.findall(remainder):
        report.add(GroundingCode.UNSUPPORTED_EMAIL,
                   f"{email} is not an address in the result", email)


def _check_dates(remainder: str, report: GroundingReport) -> None:
    for match in _DATE.finditer(remainder):
        report.add(GroundingCode.UNSUPPORTED_DATE,
                   f"{match.group()} is not a date in the result",
                   match.group())


def _check_records(remainder: str, report: GroundingReport) -> None:
    for match in _PROPER_NOUN.finditer(remainder):
        name = match.group().strip()
        if name.split()[0].lower() in _SENTENCE_OPENERS:
            continue
        report.add(GroundingCode.UNSUPPORTED_RECORD,
                   f"{name!r} is not a record in the result", name)


def _check_record_count(answer: GroundedAnswer, interpreted: InterpretedResult,
                        report: GroundingReport) -> None:
    """More detail lines than records is a record the model made up.

    Only for the types whose details are records. A grouped aggregate's lines
    are groups, and a count's summary line is not a record at all.
    """
    if interpreted.result_type not in (ResultType.SINGLE_RECORD,
                                       ResultType.MULTI_RECORD,
                                       ResultType.TRUNCATED_RESULT,
                                       ResultType.EMPTY_RESULT):
        return
    if len(answer.details) > len(interpreted.records):
        report.add(
            GroundingCode.INVENTED_RECORD_COUNT,
            f"the answer lists {len(answer.details)} records and the result "
            f"holds {len(interpreted.records)}",
            len(answer.details))


def _check_truncation(whole: str, interpreted: InterpretedResult,
                      report: GroundingReport) -> None:
    """A partial view must not be described as a total.

    Only when the total is genuinely unknown. With `total_count` established
    the model may say it, and saying it is the better answer.
    """
    if not interpreted.truncated or interpreted.total_count is not None:
        return
    match = _TOTAL_CLAIM.search(whole)
    if match:
        report.add(
            GroundingCode.TRUNCATION_MISSTATED,
            f"the result was truncated and the total is unknown, but the "
            f"answer asserts a total ({match.group().strip()!r})",
            match.group().strip())


def _check_freshness(answer: GroundedAnswer, interpreted: InterpretedResult,
                     report: GroundingReport) -> None:
    note = answer.freshness_note
    if not note:
        return
    age = interpreted.freshness.age_minutes
    stated = _numbers_in(note)
    if age is None:
        if stated:
            report.add(GroundingCode.FRESHNESS_MISSTATED,
                       "the answer states a data age and none was measured",
                       stated[0])
        return
    for value in stated:
        if abs(value - age) < 1e-9 or abs(value - round(age)) < 1e-9:
            continue
        report.add(
            GroundingCode.FRESHNESS_MISSTATED,
            f"the answer says {_render(value)} where the measured age is "
            f"{_render(age)} minutes", _render(value))


def _check_live_claim(whole: str, report: GroundingReport) -> None:
    for match in _LIVE_CLAIM.finditer(whole):
        # "a synchronised copy, not live Salesforce" denies the claim.
        before = whole[max(0, match.start() - 20):match.start()].lower()
        if re.search(r"\b(not|no|never|rather than|instead of)\s+(?:\w+\s+)?$", before):
            continue
        report.add(GroundingCode.LIVE_DATA_CLAIMED,
                   f"the answer describes a synchronised copy as live "
                   f"({match.group().strip()!r})", match.group().strip())
        return


def _check_empty_explained(whole: str, interpreted: InterpretedResult,
                           report: GroundingReport) -> None:
    if interpreted.result_type is not ResultType.EMPTY_RESULT:
        return
    match = _CAUSAL.search(whole)
    if match:
        report.add(
            GroundingCode.EMPTY_RESULT_EXPLAINED,
            f"nothing matched, which establishes no reason, but the answer "
            f"offers one ({match.group().strip()!r})", match.group().strip())


def _render(value: float) -> Any:
    return int(value) if float(value).is_integer() else value
