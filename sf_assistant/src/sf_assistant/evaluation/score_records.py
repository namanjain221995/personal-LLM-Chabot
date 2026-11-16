#!/usr/bin/env python3
"""Score an ``ask.sh --file`` run: the records golden set, or (``--lookup``) the lookup set.

    .venv/bin/python -m sf_assistant.evaluation.score_records .private/runs/ask_records_<time>.jsonl \
        [--golden .private/questions/records_golden.json]
    .venv/bin/python -m sf_assistant.evaluation.score_records \
        --lookup .private/okeval_run/lookup_eval/expected.json .private/runs/<file>.jsonl [-v]

The JSONL sidecar is written next to the transcript by ``./ask.sh --file``. Both modes score the answer
without the code footer (``answer_body``) when the sidecar has it, else ``answer``, so the footer can never
make a wrong answer pass.

Default mode: questions are matched by exact text. A question PASSES when every expected number appears
in the final answer (integers exactly, each distinct value at least once; decimals within the golden
``tol``). ``facts_ok`` says whether the numbers appear in the record results the agent saw (tool
correctness); ``unsupported`` counts numbers of the final answer that the audit could not find in the
record results. Nothing is printed except the expected numbers.

``--lookup`` (RECORDS_SPEC §21.13) assists the manual grading of the lookup set. Each run line is matched
to ``expected[].q`` by exact text (a question asked several times is scored each time) and gets:
``nums`` (every number of the entry's ``score`` list, else of its ``main*`` expected keys, is in the
answer), ``field`` (the first API name of ``field`` is in the answer), ``none`` (a none-claim in a
sentence that names no queried field while an expected number is above 0: an object name such as
`Internal_Interview__c` does not scope a claim, a 0 in a breakdown such as "Completed 4, Cancelled 0"
is no claim), ``not_found`` (for
``expect_kind: not_found``: no number beyond the question's and the source line's, and the answer says
the person or field is not found or not recorded), ``unsupported`` and ``auto`` (correct / partial /
wrong). ``-v`` prints ``accept`` and the answer. The output holds real names: it goes to the terminal
only; write grades to .private/okeval_run/lookup_eval/. Nothing from the expected file is hard-coded here.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from decimal import Decimal, InvalidOperation

from sf_assistant import config
from sf_assistant.agent.record_session import numbers_in

#: Tolerance for a decimal expected value in --lookup mode (an answer may round to one place).
LOOKUP_TOL = 0.05
_API_RE = re.compile(r"(?:[A-Za-z][A-Za-z0-9_]*\.)*(?:[A-Za-z][A-Za-z0-9_]*__[cr]|CreatedById|LastModifiedById|OwnerId|"
                     r"RecordTypeId)\b")
_BACKTICKED_RE = re.compile(r"`([^`\n]*)`")
_NONE_RE = re.compile(r"\b(?:none|no records|not associated|has not|did not)\b", re.IGNORECASE)
_ZERO_RE = re.compile(r"(?<![\w.:/,-])0(?!\d|[.,]\d|[\w:/-])")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+|\n+")
_NOT_FOUND_RE = re.compile(
    r"\b(?:not found|no such|not recorded|not synced|not in the warehouse|does not exist|doesn't exist|"
    r"not (?:available|documented|queryable)|cannot be queried|no record (?:is|was) named|"
    r"no (?:employee|user|candidate|account|lead|person|one|record)s?\b[^.\n]{0,60}\b(?:named|called|with (?:the|this|that) name))",
    re.IGNORECASE)


def _found(expected: float, numbers: list[str], tol: float) -> bool:
    values = [Decimal(n) for n in numbers]
    e = Decimal(str(expected))
    if e == e.to_integral_value():
        return e in values
    return any(abs(v - e) <= Decimal(str(tol)) for v in values)


def _fmt(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def _answer(run: dict) -> str:
    """The answer to score: ``answer_body`` (without the code footer) when present, else ``answer``."""
    body = run.get("answer_body")
    return body if body is not None else (run.get("answer") or "")


def score(run_path: str, golden_path: str) -> tuple[list[dict], int, int]:
    """Rows of the score table, passed count, count with unsupported numbers."""
    with open(golden_path, encoding="utf-8") as fh:
        golden = json.load(fh)
    runs: dict[str, dict] = {}
    with open(run_path, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                rec = json.loads(line)
                runs[rec.get("question", "")] = rec
    rows, passed, with_unsupported = [], 0, 0
    for g in golden:
        run = runs.get(g["question"])
        expected = list(dict.fromkeys(g["expect"]))
        if run is None:
            rows.append({"n": g["n"], "ok": False, "expected": expected, "missing": expected, "facts_ok": False,
                         "unsupported": None, "question": g["question"] + "  (not in this run)"})
            continue
        answer_numbers = numbers_in(_answer(run))
        fact_numbers = numbers_in(json.dumps(run.get("record_facts") or [], ensure_ascii=False))
        missing = [e for e in expected if not _found(e, answer_numbers, g.get("tol", 0))]
        facts_ok = all(_found(e, fact_numbers, g.get("tol", 0)) for e in expected)
        unsupported = len(run.get("unsupported_numbers") or [])
        ok = not missing
        passed += ok
        with_unsupported += unsupported > 0
        rows.append({"n": g["n"], "ok": ok, "expected": expected, "missing": missing, "facts_ok": facts_ok,
                     "unsupported": unsupported, "question": g["question"]})
    return rows, passed, with_unsupported


# ------------------------------------------------------------------------------------ --lookup

def _number(v) -> Decimal | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        return Decimal(str(v))
    except InvalidOperation:
        return None


def expected_numbers(entry: dict) -> list[Decimal] | None:
    """The numbers a correct answer must contain: the entry's ``score`` list, else the values of its
    expected keys whose name starts with ``main`` (a number as is, a dict's number values; lists are
    skipped). None when there are neither."""
    if isinstance(entry.get("score"), list):
        return [d for d in (_number(v) for v in entry["score"]) if d is not None]
    out: list[Decimal] = []
    found_main = False
    for key, value in (entry.get("expected") or {}).items():
        if not str(key).startswith("main"):
            continue
        found_main = True
        values = value.values() if isinstance(value, dict) else [value]
        out += [d for d in (_number(v) for v in values) if d is not None]
    return out if found_main else None


def _has_number(expected: Decimal, numbers: list[str]) -> bool:
    values = [Decimal(n) for n in numbers]
    if expected == expected.to_integral_value():
        return expected in values
    return any(abs(v - expected) <= Decimal(str(LOOKUP_TOL)) for v in values)


def first_api_name(field: str | None) -> str | None:
    """The first API name in an expected ``field`` text (``Interview__c.Recruiter__c (client) / ...`` ->
    ``Recruiter__c``: the last part of a dotted path), or None."""
    m = _API_RE.search(field or "")
    return m.group(0).split(".")[-1] if m else None


#: A lookup named right after "as <role> (", "through", "via" or "by", or followed by "lookup" / "field".
_FIELD_BEFORE_RE = re.compile(r"(?:\bas\b[^.;:`()]{0,30}\(|\b(?:through|via|by)\s+\(?)\s*$", re.IGNORECASE)
_FIELD_AFTER_RE = re.compile(r"^\s*(?:lookup|field)\b", re.IGNORECASE)
_OBJECTS: set[str] | None = None


def object_names() -> set[str]:
    """Object API names of the knowledge files next to this script (empty when the folder is missing)."""
    global _OBJECTS
    if _OBJECTS is None:
        folder = str(config.KNOWLEDGE_DIR)
        try:
            _OBJECTS = {name[:-5] for name in os.listdir(folder) if name.endswith(".json")}
        except OSError:
            _OBJECTS = set()
    return _OBJECTS


def _scoped(sentence: str, objects: set[str], fields: set[str]) -> bool:
    """True when the sentence names a queried field in backticks: a dotted path, an API name that is not an object
    name, a name the run used as a field (``fields``), or an object-named lookup in a field context ("as recruiter
    (`Recruiter__c`)"). An object name alone (`Internal_Interview__c`) scopes nothing."""
    for m in _BACKTICKED_RE.finditer(sentence):
        for api in _API_RE.findall(m.group(1)):
            if "." in api or api not in objects or api in fields:
                return True
            if _FIELD_BEFORE_RE.search(sentence[:m.start()]) or _FIELD_AFTER_RE.match(sentence[m.end():]):
                return True
    return False


#: A zero scoped by a stored value: "0 internal interviews have a "Completed" status", "0 with status 'Cancelled'".
_VALUE_SCOPE_RE = re.compile(r"[\"'\u201c\u2018][^\"'\u201d\u2019\n]{1,60}[\"'\u201d\u2019]\s+(?:status|type|stage|outcome|purpose|role|"
                             r"value|round)\b|\b(?:status|type|stage|outcome|purpose|role|round)\s*(?:=|of|is)?\s*[\"'\u201c\u2018]",
                             re.IGNORECASE)


def _bucket_zero(sentence: str, m: re.Match, item: bool | None = None) -> bool:
    """A 0 that is one value of a breakdown: "Completed 4, Cancelled 0." (after a label, before a comma, a semicolon,
    a bracket or the end, in a sentence with another number above 0), a list item "- August 2026: 0" or
    "- August 2026: 0 interviews" (after a label and a colon), a per-record value "0 for #1 and 16 for #2", or a 0
    scoped by a stored value ("0 have a "Completed" status"). ``sentence`` has its markdown emphasis removed;
    ``item`` says whether the sentence as written is a list item (the emphasis removal also removes a "*" bullet)."""
    before, after = sentence[:m.start()], sentence[m.end():]
    other = any(n != "0" for n in numbers_in(sentence))
    if item is None:
        item = re.match(r"\s*(?:[-*\u2022]|\d+[.)])\s", sentence) is not None
    if re.search(r"[A-Za-z0-9)'\"`]\s*:\s*$", before) and (item or (other and re.match(r"\s*(?:[,;(]|\.?\s*$)", after))):
        return True
    if other and re.search(r"#\d", sentence):
        return True   # per-record numbers of records that share a name
    if _VALUE_SCOPE_RE.search(sentence):
        return True
    return (re.search(r"[A-Za-z)'\"`]\s*$", before) is not None and re.match(r"\s*(?:[,;)]|\.?\s*$)", after) is not None
            and other)


def none_claim(answer: str, fields: set[str] | None = None, objects: set[str] | None = None) -> bool:
    """True when a sentence of ``answer`` claims none / no records / not associated / has not / did not or a
    standalone 0 (not a breakdown value), and does not name a queried field (:func:`_scoped`; a scoped zero such
    as "0 as recruiter (`Recruiter__c`)" is not a none-claim). ``fields``: API names the run used as fields (its
    lookup checks and filters); ``objects``: object API names (default: the knowledge files). The "Source:" line
    is skipped."""
    objs = object_names() if objects is None else objects
    used = fields or set()
    for sentence in _SENTENCE_RE.split(answer or ""):
        s = sentence.strip()
        if not s or s.lstrip("*_> ").lower().startswith("source:"):
            continue
        plain = re.sub(r"[*_]+", "", s)   # markdown emphasis (API names are matched on the sentence as written)
        item = re.match(r"\s*(?:[-*\u2022]|\d+[.)])\s", s) is not None
        zero = any(not _bucket_zero(plain, m, item) for m in _ZERO_RE.finditer(plain))
        if (_NONE_RE.search(s) or zero) and not _scoped(s, objs, used):
            return True
    return False


def run_fields(run: dict) -> set[str]:
    """API names a run used as fields: its lookup checks (the lookup, the others, the references elsewhere) and the
    fields of its record results' applied filters."""
    out: set[str] = set()
    for lc in run.get("lookup_checks") or []:
        if not isinstance(lc, dict):
            continue
        if lc.get("field"):
            out.add(str(lc["field"]))
        for key in ("others", "elsewhere"):
            out.update(str(o["field"]) for o in lc.get(key) or [] if isinstance(o, dict) and o.get("field"))
    for fact in run.get("record_facts") or []:
        applied = (fact.get("result") or {}).get("applied") if isinstance(fact, dict) else None
        for text in (applied or {}).get("filters") or [] if isinstance(applied, dict) else []:
            out.update(api.split(".")[0] for api in _API_RE.findall(str(text)))
    return out


def without_source(answer: str) -> str:
    """``answer`` without its "Source: ..." lines (their sync date and time are no counts)."""
    return "\n".join(ln for ln in (answer or "").split("\n") if not ln.strip().lstrip("*_> ").lower().startswith("source:"))


_LOOKUP_USED_RE = re.compile(r"Lookup used:\s*(.*?)(?:\.(?:\s|$)|$)", re.MULTILINE)


def footer_lookups(run: dict) -> str:
    """The "Lookup used: ..." clause of the code footer of the printed answer ('' when there is none): it names the
    lookup the answer's numbers came from, so it counts for the field check (never for numbers or none-claims)."""
    full, body = run.get("answer") or "", run.get("answer_body")
    if body is None or full == body:
        return ""
    m = _LOOKUP_USED_RE.search(full)
    return m.group(1) if m else ""


def not_found_answer(answer: str, question: str) -> bool:
    """True when the answer has no number apart from the question's and the "Source:" line's, and says the
    person or field is not found or not recorded."""
    lines = (answer or "").split("\n")
    source = "\n".join(ln for ln in lines if ln.strip().lstrip("*_> ").lower().startswith("source:"))
    allowed = set(numbers_in(question)) | set(numbers_in(source))
    if any(n not in allowed for n in numbers_in(answer)):
        return False
    return _NOT_FOUND_RE.search(answer or "") is not None


def score_lookup(run_path: str, expected_path: str) -> tuple[list[dict], dict]:
    """One row per run line that matches an expected question, and the totals."""
    with open(expected_path, encoding="utf-8") as fh:
        expected = json.load(fh)
    by_q = {e["q"]: e for e in expected}
    rows: list[dict] = []
    seen: set[str] = set()
    with open(run_path, encoding="utf-8") as fh:
        runs = [json.loads(line) for line in fh if line.strip()]
    for run in runs:
        entry = by_q.get(run.get("question", ""))
        if entry is None:
            continue
        seen.add(entry["q"])
        answer = _answer(run)
        numbers = numbers_in(without_source(answer))
        want = expected_numbers(entry)
        nums = "-" if want is None else all(_has_number(d, numbers) for d in want)
        api = first_api_name(entry.get("field"))
        api_re = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(api) + r"(?![A-Za-z0-9_])") if api else None
        field_ok = "-" if api_re is None else (api_re.search(answer) is not None
                                               or api_re.search(footer_lookups(run)) is not None)
        positive = bool(want) and any(d > 0 for d in want)
        none = positive and none_claim(answer, run_fields(run))
        nf = not_found_answer(answer, entry["q"]) if entry.get("expect_kind") == "not_found" else "-"
        first = nf if entry.get("expect_kind") == "not_found" else nums
        holds = [first is not False, field_ok is not False]
        if none:
            auto = "wrong"
        elif all(holds):
            auto = "correct"
        elif any(holds):
            auto = "partial"
        else:
            auto = "wrong"
        rows.append({"n": entry.get("n"), "question": entry["q"], "auto": auto, "nums": nums, "field": field_ok,
                     "none": none, "not_found": nf, "unsupported": len(run.get("unsupported_numbers") or []),
                     "accept": entry.get("accept", ""), "answer": answer, "missing_numbers":
                     [] if want is None else [str(d) for d in want if not _has_number(d, numbers)]})
    totals = {k: sum(r["auto"] == k for r in rows) for k in ("correct", "partial", "wrong")}
    totals["scored"] = len(rows)
    totals["not_in_run"] = len(by_q) - len(seen)
    totals["with_unsupported"] = sum(r["unsupported"] > 0 for r in rows)
    return rows, totals


def _flag(v) -> str:
    return v if isinstance(v, str) else ("yes" if v else "no")


def main_lookup(expected_path: str, run_path: str, verbose: bool) -> int:
    rows, totals = score_lookup(run_path, expected_path)
    print(f"{'#':>3}  {'auto':7}  {'nums':4}  {'field':5}  {'none':4}  {'not_found':9}  {'unsupported':>11}  question")
    for r in rows:
        print(f"{r['n'] if r['n'] is not None else '?':>3}  {r['auto']:7}  {_flag(r['nums']):4}  {_flag(r['field']):5}  "
              f"{_flag(r['none']):4}  {_flag(r['not_found']):9}  {r['unsupported']:>11}  {r['question']}")
        if verbose:
            if r["missing_numbers"]:
                print(f"       missing : {', '.join(r['missing_numbers'])}")
            print(f"       accept  : {r['accept']}")
            print("       answer  : " + r["answer"].replace("\n", "\n                 "))
    print(f"\n{totals['scored']} answers scored: {totals['correct']} correct, {totals['partial']} partial, "
          f"{totals['wrong']} wrong (auto; check by hand against accept), {totals['with_unsupported']} with "
          f"unsupported numbers, {totals['not_in_run']} expected questions not in this run")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("run", help="JSONL sidecar of an ask.sh --file run")
    p.add_argument("--golden", default=str(config.QUESTIONS_DIR / "records_golden.json"))
    p.add_argument("--lookup", metavar="EXPECTED", help="score the lookup set against this expected.json instead")
    p.add_argument("-v", "--verbose", action="store_true", help="--lookup: print accept and the answer")
    a = p.parse_args(argv)
    if a.lookup:
        return main_lookup(a.lookup, a.run, a.verbose)
    rows, passed, with_unsupported = score(a.run, a.golden)
    print(f"{'#':>3}  {'result':6}  {'expected':34}  {'missing':20}  {'facts_ok':8}  {'unsupported':>11}  question")
    for r in rows:
        exp = ", ".join(_fmt(e) for e in r["expected"])
        miss = ", ".join(_fmt(e) for e in r["missing"]) or "-"
        uns = "-" if r["unsupported"] is None else str(r["unsupported"])
        print(f"{r['n']:>3}  {'PASS' if r['ok'] else 'FAIL':6}  {exp[:34]:34}  {miss[:20]:20}  "
              f"{'yes' if r['facts_ok'] else 'no':8}  {uns:>11}  {r['question']}")
    print(f"\n{passed}/{len(rows)} passed, {with_unsupported} with unsupported numbers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
