"""Shared parts of the record tools: result size limits, budgets, ``_Call`` (per-call context) and small text helpers.

Moved verbatim from ``okrec/tools.py``. Used by :class:`sf_assistant.tools.record_tools.RecordTools` and its mixins;
it imports no mixin module.
"""

from __future__ import annotations

import dataclasses
import re
import time
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sf_assistant.config import LIMITS
from sf_assistant.engine.catalog import FieldSpec, PathRef, RecordError, hide_embedded_secrets
from sf_assistant.engine.query_builder import NUMBERED_RE, FilterOut, Frag, NameCheck, Scope
from sf_assistant.engine.types import Statement
from sf_assistant.engine.values import normalize
from sf_assistant.engine.warehouse import WarehouseError

if TYPE_CHECKING:
    from sf_assistant.tools.record_tools import RecordTools


MAX_RESULT_CHARS = LIMITS.record_result_chars   # 12_000

MAX_CELL = 200

#: Questions about named people (RECORDS_SPEC §22).
MAX_PEOPLE = 4            # names per question, and per about_people call

PROFILE_MAX = 10          # unused since evaluation fix 1 (2026-10-22): see tools.people.PROFILE_MAX_FILLED and
#                           engine.catalog.PROFILE_FIELDS_MAX (kept: the notebook's value, P0 value parity)

_POSSESSIVE_RE = re.compile(r"(\w)['’]s\b")

SCAN_BUDGET_S = 0.5

_ROLE_WORDS = ("role", "purpose")
#: English function and question words that are never read as a first name in the question (object hints).
_HINT_STOPWORDS = frozenset("""
a about above after again against all also am an and any are as at be been before being below between both but by
can could did do does doing done down during each either else ever every few for from further get gets got had has
have having he her here hers him his how however i if in into is it its itself just last least less let like many
may me might more most much must my next no nor not now of off on once one only or other our ours out over own per
please same she should show shows so some such than that the their theirs them then there these they this those
through to today too total tomorrow under until up upon us very was way we well were what when where which while who
whom whose why will with within without would yesterday yet you your yours wise type types kind kinds number numbers
count counts list lists give tell find many much week weeks month months year years day days quarter quarters
since till ago taken took take takes conducted conduct conducts held hosted host hosts trained train trains taught
attended attend attends joined join supported support assisted handled handles owns owned created create creates
added booked modified edited updated changed approved uploaded submitted evaluated assessed scored graded decided
referred invited managed assigned recruited breakdown wise
january february march april may june july august september october november december
monday tuesday wednesday thursday friday saturday sunday
""".split())

#: Words that mark the notes the Group B people fixes add (docs/people.md): the parent paths, the role that shares a
#: word, the copy of a child object's lookup and its source, also_held, child_people and the about_people keys note.
#: The record facts of the merge (agent/checks.facts_json) drop these notes, with the detail keys, first when the
#: facts are cut to size; tests/test_tools_groupb.py checks that every such note carries one of them.
DETAIL_NOTE_MARKS = (" through the parents of ", " also holds a role sharing a word with ", ", taken from the ",
                     ": through the source ", "the paths through other records to this ", "also_held: ",
                     "child_people: ", "(never add these counts to each other or to references)")

#: Which of several people with the same full name the hints scan first (object, person kind).
_PERSON_ORDER = {("Recruiter__c", "employee"): 0, ("User", "employee"): 1, ("Account", "candidate"): 2,
                 ("Lead", "candidate"): 3, ("Account", "other"): 4}


# ------------------------------------------------------------------------------------- helpers

def _cell(v: Any, kind: str | None = None) -> Any:
    """A stored value for a tool result: a datetime as 'YYYY-MM-DD HH:MM UTC', a text without the passcodes and tokens
    of its links (phase 1, ``engine.catalog.hide_embedded_secrets``) and cut at MAX_CELL, a float rounded."""
    if isinstance(v, str):
        if kind == "datetime" and re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", v):
            return v[:16] + " UTC"
        v = hide_embedded_secrets(v)
        return v if len(v) <= MAX_CELL else v[: MAX_CELL - 1] + "…"
    if isinstance(v, float):
        return round(v, 4)
    return v


def _round(v: Any, n: int = 2) -> Any:
    if isinstance(v, Decimal):
        v = float(v)
    if isinstance(v, float):
        r = round(v, n)
        return int(r) if r == int(r) and n == 0 else r
    return v


class _Call:
    """Per-call context: statements, tables, notes and the deadline."""

    def __init__(self, tools: "RecordTools", name: str) -> None:
        self.tools = tools
        self.name = name
        self.statements: list[Statement] = []
        self.tables: list[str] = []
        self.notes: list[str] = []
        self.applied: dict[str, Any] = {}
        self.fields_used: list[tuple[str, str]] = []
        self.object: str | None = None
        self.summary = ""
        self.detail: str | None = None
        # "also stored in" notes, completed after the handler: (note, scope, filter, conditions of the result the
        # filter belongs to, what that result is called). The condition list may be filled after the filter.
        self.also: list[tuple[str, Scope, FilterOut, list[Frag], str]] = []
        self.deadline = time.monotonic() + tools.rcfg.call_budget_s
        self.max_chars = MAX_RESULT_CHARS   # the result size limit (_fit); about_people with links raises it
        self.ctx = tools.build_ctx()
        self.ctx.names = lambda objs, norm: tools._name_rows(self, objs, norm)
        self.ctx.person_names = tools._person_names
        # Name checks of every filter: (check, filter output, is a base filter of a tool with lookup checks).
        self.name_checks: list[tuple[NameCheck, FilterOut, bool]] = []
        self.main_scope: Scope | None = None   # scope and conditions of the main statement (lookup check)
        self.main_conds: list[Frag] = []
        self.before_having = False             # group_records min_value: the lookup check counts before HAVING
        self.no_data: list[str] = []           # "<Obj>.<Field>" lookups filtered on a name that are blank on every record
        # names a failed call found no record for (E4h, E15a): {"name", "target", "object", "field"} (QueryEvidence)
        self.not_found: list[dict[str, Any]] = []
        # the spelling help (docs/resolver_integration.md (3)): the call's correction channel (engine.catalog.READ_AS;
        # None when it is off) and the did-you-mean of a failed call (QueryEvidence.read_as / did_you_mean)
        self.read_as: list[dict[str, Any]] | None = None
        self.did_you_mean: list[dict[str, Any]] = []
        # list_records (C8): the listed columns and order, for the rows of the other lookups that hold the filter's
        # person (``also_held``, filled by the lookup check)
        self.list_view: dict[str, Any] | None = None
        self.also_held: list[dict[str, Any]] = []
        # about_people: profile keys with a direction text -> the same key without it (the result-size budget)
        self.profile_plain: dict[str, str] = {}
        # about_people: each person's linked line -> the line without how it is linked (the result-size budget)
        self.linked_plain: dict[str, str] = {}
        # about_people: (profile, its keys of the values past the profile core, people._core_value): values the
        # result-size budget leaves out first
        self.profile_details: list[tuple[dict[str, Any], list[str]]] = []
        # the lookup check of a filter on a child copy (item 9): copy field -> the sentence on its source's count,
        # added to the copy's note by record_tools._mirror_notes (one fact, one note)
        self.copy_sources: dict[str, str] = {}
        # the business-term contracts (tools/term_enforce.py, 2026-11-24): the plan of this call (None: no term
        # applied), the arguments the handler ran with when the plan changed them (QueryEvidence.enforced_arguments)
        # and whether its added filters were left out (the join cap)
        self.term_plan: Any = None
        self.enforced: dict[str, Any] | None = None
        self.terms_dropped = False

    def note(self, text: str) -> None:
        if text and text not in self.notes:
            self.notes.append(text)

    def table(self, api: str) -> None:
        if api and api not in self.tables and api != "User|Group":
            self.tables.append(api)

    def use(self, ref: PathRef | None) -> None:
        if ref is None:
            return
        for h in ref.hops:
            self.fields_used.append((h.src, h.field.api))
        self.fields_used.append((ref.obj, ref.field.api))

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def run(self, frag: Frag, *, max_rows: int = 1000, timeout: float | None = None,
            max_bytes: int | None = None) -> Statement:
        remaining = self.deadline - time.monotonic()
        budget = self.tools.rcfg.call_budget_s
        if remaining <= 0:
            raise RecordError(f"query stopped after {budget:g} s. Narrow it: add filters or a shorter period, or use "
                              "group_records instead of list_records.", "E10")
        timeout = max(0.5, min(self.tools.rcfg.timeout_s, remaining)) if timeout is None else \
            max(0.1, min(self.tools.rcfg.timeout_s, remaining, timeout))
        try:
            st = self.tools.warehouse.run(frag.sql, list(frag.params), max_rows=max_rows, timeout_s=timeout,
                                          **({"max_bytes": int(max_bytes)} if max_bytes else {}))
        except WarehouseError as exc:
            if exc.kind == "guard":
                self.detail = f"guard: {exc.detail} | sql: {frag.sql[:500]}"
                raise RecordError("internal query check failed (a bug in the record tools, not in your call); try a "
                                  "simpler query.", "E12") from None
            if exc.kind == "timeout":
                raise RecordError(f"query stopped after {timeout:g} s. Narrow it: add filters or a shorter period, or use "
                                  "group_records instead of list_records.", "E10") from None
            self.detail = f"{exc} | sql: {frag.sql[:500]}"
            raise RecordError(f"the query failed in DuckDB: {str(exc)[:300]}. Try a simpler query.", "E13") from None
        self.statements.append(st)
        return st

    def main(self, st: Statement) -> None:
        """Move ``st`` to the front: evidence ``statements[0]`` is the main statement."""
        if st in self.statements:
            self.statements.remove(st)
        self.statements.insert(0, st)

    def cached(self, st: Statement | None) -> None:
        if st is not None and all(s is not st for s in self.statements):
            self.statements.append(st)

    def reuse(self, st: Statement | None) -> None:
        """Add a statement that ran earlier in this question (per-question cache) as cached evidence."""
        if st is None or any(s is st or (s.sql == st.sql and s.params == st.params) for s in self.statements):
            return
        self.statements.append(dataclasses.replace(st, cached=True, reused=True))


def _short(text: Any, n: int) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _disp(name: Any) -> str:
    """A stored name for display: non-breaking and repeated spaces read as one space."""
    return " ".join(str(name or "").replace("\xa0", " ").split())


def _and_more(names: list[str], n: int = 5) -> str:
    return ", ".join(names[:n]) + (", and more" if len(names) > n else "")


def _and(items: list[str]) -> str:
    items = list(items)
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1] if items else ""


def _role(f: FieldSpec) -> str:
    """The role key of a lookup: ``ref_role`` with spaces, else its role text."""
    return f.role or f.role_text()


def _cut_text(text: Any, n: int) -> str:
    return _short(text, n)


def _without_words(question: str, names: list[str]) -> str:
    """The question without the words of the names it is filtered on (a name is never a role word)."""
    drop = {w for n in names for w in _q_tokens(NUMBERED_RE.sub(lambda m: m["name"], str(n)))}
    return " ".join(w for w in _q_tokens(question) if w not in drop)


def _close_surname(word: str, surname: list[str]) -> bool:
    """True when ``word`` is a close but not exact spelling of the first word of a stored surname (similarity at least
    0.8, same first letter): "persn" for "person", never "really" for "one"."""
    if not surname or not word:
        return False
    s0 = surname[0]
    if word == s0 or word[0] != s0[0]:
        return False
    import difflib
    return difflib.SequenceMatcher(None, word, s0).ratio() >= 0.8


#: Characters that separate two names glued together ("a,b", "[a]", "a&b"): read as a space before
#: :func:`okrec.values.normalize`, which would delete some of them and glue the names. "." and the apostrophe keep
#: their meaning ("J.K.", possessives).
_NAME_SEP_RE = re.compile(r"[,;:!?()\[\]{}<>\"&+|]")


def name_key(text: Any) -> str:
    """The key of a person name, for stored names and question words alike: :func:`okrec.values.normalize` with a
    curly apostrophe read as a straight one (so "x’s" loses it like "x's") and separators read as spaces
    (:data:`_NAME_SEP_RE`: "a,b" is two words)."""
    return normalize(_NAME_SEP_RE.sub(" ", str(text if text is not None else "").replace("’", "'")))


def _q_tokens(text: Any) -> list[str]:
    """Words of a question as :func:`name_key` writes them, possessives removed ("x's" and "x’s" -> "x") and without
    sentence punctuation around them ("person?" -> "person"), so a name at the end of a question or before 's matches
    the name index."""
    t = _POSSESSIVE_RE.sub(r"\1", str(text if text is not None else ""))
    return [w for w in (x.strip("?!;:") for x in name_key(t).split()) if w]
