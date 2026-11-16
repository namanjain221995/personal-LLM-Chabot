"""Automatic grading of one answered question: ``correct`` / ``partial`` / ``wrong`` / ``needs_review`` with reasons.

Input: the question's :class:`~sf_assistant.evaluation.sets.EvalItem` (its expected entry), the answer row (the JSONL
record ``python -m sf_assistant ask --file`` writes, :func:`sf_assistant.pipeline.result_row`) and the run mode.
No model call anywhere: every check is code over the answer text, the row's routing fields and the expected entry.

Graders (``sets.yaml`` ``grader``):

* ``golden`` - ``questions/records_golden.json``: every expected number in the answer (:mod:`score_records` rules:
  integers exactly, decimals within the entry's ``tol``); some found -> partial, none -> wrong. With a record
  connection, the golden tool call is replayed and an expected number that is no longer in its result makes the
  verdict ``needs_review`` (the data changed).
* ``lookup`` - the lookup set: the entry's ``score`` numbers (re-derived from its SQL, :mod:`expected_data`), the
  field when ``accept`` says "field named", none-claims ("none", a bare 0), the wrong-lookup numbers of the
  entry's ``wrong*`` facts and a different count in the answer's main statement (``wrong``); a required 0 stated in
  words ("none", "blank") counts only in the main statement and only when that statement gives no other count;
  ``expect_kind: not_found`` questions need a not-found answer without a count.
* ``people`` - the person set: every ``must`` item is split into atoms (numbers, dates, values of the expected facts
  that the item quotes, API names where the question asks for a field) and each atom must be in the answer; plus the
  set's wrong rules (no answer, a "not stored" / knowledge-only answer for a stored person, a yes/no against the
  expected one, numbers of the entry's ``wrong`` items).
* ``control`` - person-set controls: answered without records (route check), and the identifiers of the first
  ``must`` item (the field / rule / flow the question is about) in the answer.
* ``route`` - metadata questions: answered by the knowledge agent with records not needed.

Numbers inside ISO dates, times and record codes are no counts: "2026-08-15" holds no 8 or 15, "INT-0268" no 268
(:func:`answer_numbers`). A question that is not in the run (its group failed) is ``not_run``: no score counts it.

Every grader adds ``unsupported`` (numbers of a record answer that are not in the record results; a correct verdict
becomes ``needs_review``) and leaked working text (merge input labels such as "RECORD FACTS": ``needs_review``).
When an expected SQL statement fails on the data (``expected_data`` status ``error``), the verdict is
``needs_review``: the expectation cannot be checked.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from sf_assistant.agent.checks import numbers_in
from sf_assistant.evaluation import expected_data
from sf_assistant.evaluation.score_records import (_NOT_FOUND_RE, _found, first_api_name, footer_lookups,
                                                   none_claim, run_fields, without_source)

__all__ = ["Grade", "VERDICTS", "NOT_RUN", "grade", "grade_golden", "grade_lookup", "grade_people", "grade_control",
           "grade_route", "answer_text", "answer_numbers", "main_statement"]

VERDICTS = ("correct", "partial", "wrong", "needs_review")
#: the verdict of a question that is not in the run (its group failed): no score and no comparison counts it
NOT_RUN = "not_run"
#: decimals in an answer may be rounded to one place
NUMBER_TOL = 0.05
#: a lookup count this close to the required one is ``partial`` (a scope difference), a count further off ``wrong``
CLOSE_COUNT = 0.1


@dataclass
class Grade:
    """The verdict of one answer with its reasons (each a short sentence) and the raw checks behind it."""

    verdict: str
    reasons: list[str] = field(default_factory=list)
    checks: dict = field(default_factory=dict)
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# ------------------------------------------------------------------------------------------------ answer text helpers
_WORD_NUMBERS = {w: str(i) for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen "
    "eighteen nineteen twenty".split())}
_WORD_NUMBERS.update({"thirty": "30", "forty": "40", "fifty": "50", "single": "1", "twice": "2", "both": "2"})
_WORD_NUMBER_RE = re.compile(r"\b(" + "|".join(_WORD_NUMBERS) + r")\b", re.IGNORECASE)
_NON_ANSWER_RE = re.compile(
    r"^\s*\((?:no answer|no answer from the records)\)\s*$|\bcould not (?:be )?verif|\bmax(?:imum)?[- ]steps?\b|"
    r"\b(?:ran out of|reached the) (?:tool )?(?:steps|budget|limit)\b", re.IGNORECASE)
#: merge input labels and draft words that must never reach an answer. "Draft" is also a stored picklist value
#: (Resume__c, Program__c, ...): only the lower-case working phrases count ("in the draft", "the draft answer")
_LEAK_RE = re.compile(r"(?i:\bRECORD (?:FACTS|ANSWER)\b|\bKNOWLEDGE (?:ANSWER|FACTS)\b|\bmisattributed\b)|"
                      r"\b(?:in|from) (?:the|my) draft\b|\bdraft answer\b")
#: dates, times and record codes: their digits are no counts (year-months and dates of 19xx / 20xx)
_DATE_TIME_RE = re.compile(r"\b(?:19|20)\d{2}-\d{2}(?:-\d{2}(?:[T ]\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:\s*(?:UTC|Z)\b)?)?)?"
                           r"(?![\d-])|\b\d{1,2}:\d{2}(?::\d{2})?(?:\s*(?:UTC|[AaPp]\.?[Mm]\b\.?))?(?![\d:])")
_RECORD_CODE_RE = re.compile(r"\b[A-Z]{1,6}-\d{3,}\b")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+|\n+")
#: P-codes / R-codes of the record tools' people and record lists ("P4/", "(P1)", "R3")
_CODE_RE = re.compile(r"(?<![\w-])[PR]\d{1,2}(?![\w-])")


def answer_text(row: dict) -> str:
    """The answer to grade: ``answer_body`` (without the code footer) when the row has it, else ``answer``."""
    body = row.get("answer_body")
    return body if body is not None else (row.get("answer") or "")


def _without_dates(text: str) -> str:
    return _RECORD_CODE_RE.sub(" ", _DATE_TIME_RE.sub(" ", text or ""))


def answer_numbers(text: str) -> list[str]:
    """Canonical numbers of ``text`` without its Source line, number words included ("three" -> 3); the digits of
    ISO dates, times and record codes are left out ("2026-08-15", "14:30 UTC", "INT-0268")."""
    plain = _without_dates(without_source(text or ""))
    words = [_WORD_NUMBERS[m.group(1).lower()] for m in _WORD_NUMBER_RE.finditer(plain)]
    return numbers_in(plain) + words


def main_statement(text: str) -> str:
    """The answer's main statement: the first of its first three sentences (markdown emphasis removed) that gives a
    number or a none word ("" when none does)."""
    plain = re.sub(r"[*`>#|]+", " ", without_source(text or ""))
    for sentence in [x.strip() for x in _SENTENCE_RE.split(plain) if x.strip()][:3]:
        if answer_numbers(sentence) or _ZERO_WORDS_RE.search(sentence):
            return sentence
    return ""


def _digits(text: str) -> list[str]:
    """The numbers written in digits (no number words) of ``text``, dates, times and record codes left out."""
    return numbers_in(_without_dates(text))


def _nonzero(numbers: list[str]) -> list[str]:
    out = []
    for n in numbers:
        try:
            if Decimal(n) != 0:
                out.append(n)
        except InvalidOperation:
            continue
    return out


def _has_number(value: str, numbers: list[str], tol: float = NUMBER_TOL) -> bool:
    try:
        e = Decimal(value)
    except InvalidOperation:
        return False
    vals = []
    for n in numbers:
        try:
            vals.append(Decimal(n))
        except InvalidOperation:
            continue
    if e == e.to_integral_value():
        return e in vals
    return any(abs(v - e) <= Decimal(str(tol)) for v in vals)


_ZERO_WORDS_RE = re.compile(r"\b(?:no|none|zero|not any|never|blank|empty|no data|nobody|not (?:recorded|filled|set))\b",
                            re.IGNORECASE)


def _number_found(value: str, numbers: list[str], text: str, tol: float = NUMBER_TOL) -> bool:
    """A required number is in the answer; a required 0 is also stated by "none" / "no" / "blank" / "no data" in the
    answer's main statement, when that statement gives no other count ("There are 5 records. No other details are
    stored." does not state 0)."""
    if _has_number(value, numbers, tol):
        return True
    if value != "0":
        return False
    claim = main_statement(text)   # digits only: "None of them has one" states 0
    return _ZERO_WORDS_RE.search(claim) is not None and not _nonzero(_digits(claim))


def _canon(v: Any) -> str | None:
    return expected_data.canon_number(v)


def _common(row: dict, text: str, g: Grade) -> None:
    """Flags every grader adds: unsupported numbers, leaked merge text, record codes."""
    unsupported = list(row.get("unsupported_numbers") or [])
    if unsupported:
        g.checks["unsupported"] = unsupported
        g.reasons.append("numbers not found in the record results: " + ", ".join(map(str, unsupported[:8])))
        if g.verdict == "correct":
            g.verdict = "needs_review"
    leak = _LEAK_RE.search(text or "")
    if leak:
        g.checks["leak"] = leak.group(0)
        g.reasons.append(f"working text in the answer ({leak.group(0)!r})")
        if g.verdict in ("correct", "partial"):
            g.verdict = "needs_review"
    codes = sorted(set(_CODE_RE.findall(text or "")))
    if codes:
        g.checks["codes"] = codes
        g.reasons.append("record codes in the answer text: " + ", ".join(codes[:6]))


def _non_answer(row: dict, text: str) -> str | None:
    if not (text or "").strip():
        return "no answer" + (f" ({row.get('record_error')})" if row.get("record_error") else "")
    m = _NON_ANSWER_RE.search(text)
    if m and len(text.strip()) < 400:
        return f"non-answer ({m.group(0).strip()!r})"
    return None


# ------------------------------------------------------------------------------------------------ golden
def grade_golden(item, row: dict, mode: str, tools=None) -> Grade:
    """``questions/records_golden.json`` entry: ``expect`` numbers (``tol`` for decimals)."""
    entry = item.expected or {}
    text = answer_text(row)
    expected = [str(_canon(v)) for v in dict.fromkeys(entry.get("expect") or []) if _canon(v) is not None]
    tol = float(entry.get("tol") or 0)   # score_records: the golden entry's tol (0 = exact)
    g = Grade("wrong", checks={"expected": expected})
    na = _non_answer(row, text)
    if na:
        g.reasons.append(na)
        return g
    if not expected:
        g.verdict = "needs_review"
        g.reasons.append("the golden entry has no expected numbers")
        return g
    # score_records' rule (integers exactly, decimals within tol), on the answer without its Source line
    nums = answer_numbers(text)
    missing = [e for e in expected if not _found(float(e), nums, tol)]
    facts = numbers_in(json.dumps(row.get("record_facts") or [], ensure_ascii=False, default=str))
    g.checks["missing"] = missing
    g.checks["facts_ok"] = all(_found(float(e), facts, tol) for e in expected)   # the agent saw the numbers
    g.verdict = "correct" if not missing else "partial" if len(missing) < len(expected) else "wrong"
    if missing:
        g.reasons.append("expected numbers missing: " + ", ".join(missing))
    if tools is not None and entry.get("tool"):
        g.data = _golden_data(tools, entry, expected)
        if g.data.get("status") == "changed":
            g.reasons.append("data changed: expected numbers no longer in the tool result: "
                             + ", ".join(g.data.get("gone") or []))
            g.verdict = "needs_review"
    _common(row, text, g)
    return g


def _golden_data(tools, entry: dict, expected: list[str]) -> dict:
    try:
        res = tools.call(entry["tool"], entry.get("args") or {})
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"[:200]}
    payload = res.get("result", res) if isinstance(res, dict) else res
    nums = numbers_in(json.dumps(payload, ensure_ascii=False, default=str))
    gone = [e for e in expected if not _has_number(e, nums, NUMBER_TOL)]
    return {"status": "changed" if gone else "same", "gone": gone}


# ------------------------------------------------------------------------------------------------ lookup
_ACCEPT_CORRECT_RE = re.compile(r"correct:\s*(.*?)(?=\b(?:partial|wrong):|$)", re.IGNORECASE | re.DOTALL)
_ACCEPT_API_RE = re.compile(r"(?:[A-Za-z][A-Za-z0-9_]*\.)*[A-Za-z][A-Za-z0-9_]*__[cr]\b|\b(?:CreatedById|OwnerId|"
                            r"LastModifiedById)\b")
_SUGGEST_RE = re.compile(r"\b(?:closest|did you mean|similar|spell|spelt|stored as|stored name|instead|nearest|"
                         r"close match|which one|do you mean)\w*", re.IGNORECASE)
_NOT_FOUND_EXTRA_RE = re.compile(
    r"\b(?:not a stored|is not stored|isn't stored|no stored|no person record|not in the (?:records|warehouse|data)|"
    r"does not record|doesn't record|is not recorded|not available|no (?:employee|user|account|lead|candidate|person|"
    r"record)s?\b[^.\n]{0,80}\b(?:named|called|matches|match))", re.IGNORECASE)


def _accept_fields(entry: dict) -> list[str] | None:
    """API names the correct answer must name, when ``accept``'s correct clause says "field named" (else None)."""
    accept = entry.get("accept") or ""
    m = _ACCEPT_CORRECT_RE.search(accept)
    # "(or <another reading>, field named)" asks for the field of that reading only
    clause = re.sub(r"\(\s*or\b[^()]*\)", "", m.group(1)) if m else ""
    if "field named" not in clause.lower():
        return None
    apis = [a.split(".")[-1] for a in _ACCEPT_API_RE.findall(clause)]
    if not apis:
        api = first_api_name(entry.get("field"))
        apis = [api] if api else []
    return list(dict.fromkeys(apis)) or None


def _api_in(api: str, text: str) -> bool:
    return re.search(r"(?<![A-Za-z0-9_])" + re.escape(api) + r"(?![A-Za-z0-9_])", text or "") is not None


def _says_not_found(text: str) -> bool:
    head = " ".join(re.split(r"(?<=[.!?])\s+", without_source(text).strip())[:3])
    return bool(_NOT_FOUND_RE.search(head) or _NOT_FOUND_EXTRA_RE.search(head))


def _wrong_numbers(entry: dict) -> list[str]:
    """Numbers of the entry's facts whose name starts with ``wrong`` (the count of a wrong lookup)."""
    out: list[str] = []
    for key, value in (entry.get("expected") or {}).items():
        if str(key).lower().startswith("wrong"):
            out += [n for n in expected_data.numbers_of(value) if n not in ("0", "1")]
    return list(dict.fromkeys(out))


def grade_lookup(item, row: dict, mode: str, con=None) -> Grade:
    """The lookup set (``okeval_run/lookup_eval/expected.json``)."""
    entry = item.expected or {}
    text = answer_text(row)
    g = Grade("wrong")
    na = _non_answer(row, text)
    if na:
        g.reasons.append(na)
        return g
    chk = expected_data.check_entry(con, entry, "lookup")
    g.data = chk.summary()
    if entry.get("expect_kind") == "not_found":
        accept = (entry.get("accept") or "").lower()
        nf = _says_not_found(text)
        suggests = _SUGGEST_RE.search(text) is not None
        g.checks.update(not_found=nf, suggests=suggests)
        if nf:
            if "no suggestion" in accept and not suggests:
                g.verdict = "partial"
                g.reasons.append("says not found but suggests no stored match")
            else:
                g.verdict = "correct"
        elif none_claim(text, run_fields(row)) or re.search(r"(?<![\w.])0\s+\w+", without_source(text)):
            g.reasons.append("gives a zero count instead of saying the name / field is not found")
        else:
            g.verdict = "needs_review"
            g.reasons.append("no not-found statement found")
        _common(row, text, g)
        return g
    want = [str(_canon(v)) for v in (entry.get("score") or []) if _canon(v) is not None]
    if not want:   # score_records.expected_numbers: the main* facts
        from sf_assistant.evaluation.score_records import expected_numbers

        want = [str(_canon(d)) for d in (expected_numbers(entry) or [])]
    want, stale, notes = expected_data.refresh_numbers(list(dict.fromkeys(want)), chk)
    g.reasons += notes
    nums = answer_numbers(text)
    missing = [w for w in want if not _number_found(w, nums, text)]
    fields = _accept_fields(entry)
    footer = footer_lookups(row)
    field_ok = None if fields is None else any(_api_in(f, text) or _api_in(f, footer) for f in fields)
    wrong_nums = [n for n in _wrong_numbers(entry) if n not in want and _has_number(n, nums)]
    # the main statement gives another count than every required one ("53" asked, "70" answered); a count within
    # CLOSE_COUNT of a required one is a scope difference to review (partial: "25 of 26, one of another record type")
    given = [n for n in dict.fromkeys(_nonzero(_digits(main_statement(text)))) if n not in want]
    close = [n for n in given if any(_close(n, w) for w in missing)]
    other = [n for n in given if n not in close]
    g.checks.update(numbers=want, missing=missing, fields=fields, field_ok=field_ok, stale=stale, other=other,
                    close=close)
    if stale:
        g.verdict = "needs_review"
        g.reasons.append(_stale_reason(chk, stale))
    elif not missing and field_ok is not False:
        g.verdict = "correct"
    elif missing and len(missing) == len(want) and (none_claim(text, run_fields(row)) or wrong_nums or other):
        g.verdict = "wrong"
        g.reasons.append("expected numbers missing: " + ", ".join(missing))
        if wrong_nums:
            g.reasons.append("gives the count of a wrong lookup: " + ", ".join(wrong_nums))
        elif other:
            g.reasons.append(f"gives {', '.join(other)} instead of {', '.join(missing)}")
        else:
            g.reasons.append("a none-claim / zero instead of the count")
    else:
        g.verdict = "partial"
        if missing:
            g.reasons.append("expected numbers missing: " + ", ".join(missing))
        if close and missing:
            g.reasons.append(f"gives {', '.join(close)}, within {CLOSE_COUNT:.0%} of {', '.join(missing)}: check the "
                             "scope")
        if field_ok is False:
            g.reasons.append("field not named (accept: field named): " + " / ".join(fields or []))
    _common(row, text, g)
    return g


def _close(given: str, wanted: str) -> bool:
    """``given`` is within :data:`CLOSE_COUNT` of the non-zero count ``wanted`` (26 asked: 24 to 28)."""
    try:
        g, w = Decimal(given), Decimal(wanted)
    except InvalidOperation:
        return False
    return w != 0 and abs(g - w) <= Decimal(str(CLOSE_COUNT)) * abs(w)


def _stale_reason(chk, stale: list[str]) -> str:
    """Why an expectation cannot be checked: its SQL failed on the data, or the data changed in a way that cannot
    be mapped."""
    if chk is not None and chk.status == "error":
        return "expected SQL failed on the data: " + (chk.errors[0] if chk.errors else "?")
    return "data changed: cannot re-derive " + ", ".join(stale)


# ------------------------------------------------------------------------------------------------ route / controls
def _records_used(row: dict) -> bool:
    """True when the answer rests on record data (merged or record answer, or record queries ran)."""
    return row.get("answer_kind") in ("merged", "records")


def grade_route(item, row: dict, mode: str) -> Grade:
    """A metadata question: the knowledge agent answers, records not needed."""
    text = answer_text(row)
    g = Grade("wrong", checks={"answer_kind": row.get("answer_kind"),
                               "route": (row.get("triage") or {}).get("route")})
    na = _non_answer(row, text)
    if na:
        g.reasons.append(na)
        return g
    if _records_used(row):
        g.reasons.append(f"answered from records ({row.get('answer_kind')}); records are not needed")
        return g
    if row.get("record_error"):
        g.verdict = "needs_review"
        g.reasons.append(f"record error: {row['record_error']}")
        return g
    g.verdict = "correct"
    _common(row, text, g)
    return g


_IDENT_RE = re.compile(r"(?:[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]*[A-Za-z0-9]|[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+){2,})"
                       r"(?:\.[A-Za-z][A-Za-z0-9_]*)*")


def _identifiers(text: str) -> list[str]:
    """API names (``Session__c.Host_User__c``, ``Lock_Assigned_Trainer``) and CamelCase class names
    (``CandidateTrainingDuplicateGuard``) of a must item; a trailing '...' cut is dropped."""
    out = []
    for m in _IDENT_RE.finditer(text or ""):
        if text[m.end():m.end() + 3] == "...":
            continue
        out.append(m.group(0))
    return list(dict.fromkeys(out))


def _ident_in(ident: str, text: str) -> bool:
    """An identifier is in the answer: a dotted ``Object.Field`` also counts when the answer names both the field and
    the object (``Interviewer__c`` ... ``Internal_Interview__c``); a loose name without ``__`` also as words."""
    if _api_in(ident, text):
        return True
    if "." in ident:
        obj, fld = ident.split(".", 1)
        return _ident_in(fld, text) and _ident_in(obj, text)
    if "__" not in ident and "_" in ident:   # a loose name ("Session_Attendee rows"): the object, or as words
        return _api_in(ident + "__c", text) or re.search(
            r"\b" + re.escape(ident.replace("_", " ")) + r"\b", text or "", re.IGNORECASE) is not None
    return False


def grade_control(item, row: dict, mode: str) -> Grade:
    """A person-set control: no records, and the identifiers of the first ``must`` item named."""
    g = grade_route(item, row, mode)
    if g.verdict != "correct":
        return g
    text = answer_text(row)
    must = (item.expected or {}).get("must") or []
    # the first clause of the first must item names what the question is about; later clauses are side notes
    idents = _identifiers(must[0].split(";")[0]) if must else []
    missing = [i for i in idents if not _ident_in(i, text)]
    g.checks.update(identifiers=idents, missing=missing)
    if missing:
        g.verdict = "partial"
        g.reasons.append("metadata answer misses: " + ", ".join(missing))
    return g


# ------------------------------------------------------------------------------------------------ dispatcher
def grade(item, row: dict | None, mode: str, con=None, tools=None) -> Grade:
    """Grade one answer row of ``item`` (``item.grader`` picks the grader). ``con``: a read-only warehouse
    connection for the SQL re-run (:func:`expected_data.open_readonly`); ``tools``: record tools for the golden
    replay. A missing row is :data:`NOT_RUN` ("not in the run"): no score counts it."""
    if row is None:
        return Grade(NOT_RUN, ["not in the run"])
    if item.grader == "golden":
        return grade_golden(item, row, mode, tools)
    if item.grader == "lookup":
        return grade_lookup(item, row, mode, con)
    if item.grader == "people":
        from sf_assistant.evaluation.people_grader import grade_people as _gp

        return _gp(item, row, mode, con)
    if item.grader == "control":
        return grade_control(item, row, mode)
    if item.grader == "route":
        return grade_route(item, row, mode)
    raise ValueError(f"unknown grader {item.grader!r}")


def grade_people(item, row: dict, mode: str, con=None) -> Grade:
    """See :mod:`sf_assistant.evaluation.people_grader`."""
    from sf_assistant.evaluation.people_grader import grade_people as _gp

    return _gp(item, row, mode, con)
